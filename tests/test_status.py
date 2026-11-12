"""Status filtering, history bounds and read-only CLI regressions."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from review_watcher import cli
from review_watcher.console import Console
from review_watcher.state import Store
from review_watcher.status import status_overview


class TestStatus(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = Store(self.root/'state.sqlite3', 'infra-test/gctest1')
        statuses = ['baseline', 'done', 'declined', 'reviewed_only',
                    'awaiting_confirmation', 'queued', 'running', 'needs_attention', 'closed']
        with self.store.connect() as db:
            db.executemany('INSERT INTO jobs(number,status) VALUES (?,?)', enumerate(statuses, 1))
            db.executemany('INSERT INTO pr_visibility VALUES (?,?)',
                           ((n, int(n != 9)) for n in range(1, 10)))
        self.store.post('sent-marker', 2, 'sent', '33')
        self.store.post('pending-marker', 8, 'pending')

    def numbers(self, snapshot):
        return [row['number'] for row in snapshot['jobs']]

    def run_cli(self, *options, root=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(cli.sys, 'argv', ['watcher.py', '--status', *options]), \
             patch.object(cli, 'load_config', return_value=({'repo': 'infra-test/gctest1'}, root or self.root)), \
             patch.object(cli, 'Watcher') as watcher, \
             patch.object(cli, 'resolve_tool') as resolve, \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            cli.main()
        watcher.assert_not_called()
        resolve.assert_not_called()
        return json.loads(stdout.getvalue()), stderr.getvalue()

    def test_default_focuses_on_todo_with_attention_and_running_first(self):
        snapshot = self.store.status_summary()
        self.assertEqual(self.numbers(snapshot), [8, 7, 5, 6])
        self.assertEqual(snapshot['total'], 4)
        self.assertEqual(snapshot['counts']['baseline'], 1)
        self.assertEqual(snapshot['counts']['done'], 1)
        self.assertEqual(snapshot['counts']['closed'], 1)
        self.assertIn('待办：显示 4/4 条', status_overview(snapshot))

    def test_all_includes_completed_open_jobs_but_not_closed(self):
        self.assertEqual(self.numbers(self.store.status_summary('all')), [8, 7, 5, 6, 1, 2, 3, 4])

    def test_history_includes_skipped_completed_and_closed_with_limit(self):
        snapshot = self.store.status_summary('history', limit=2)
        self.assertEqual(self.numbers(snapshot), [9, 4])
        self.assertEqual(snapshot['total'], 5)
        self.assertIn('历史：显示 2/5 条（按 PR 编号倒序）', status_overview(snapshot))

    def test_default_history_limit_is_twenty(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO jobs(number,status) VALUES (?,'done')",
                           ((number,) for number in range(10, 40)))
        snapshot = self.store.status_summary('history')
        self.assertEqual(len(snapshot['jobs']), 20)
        self.assertEqual(snapshot['total'], 35)
        self.assertEqual(self.numbers(snapshot), list(range(39, 19, -1)))

    def test_visibility_overrides_job_status_without_modifying_it(self):
        with self.store.connect() as db:
            db.execute('UPDATE pr_visibility SET is_open=0 WHERE number IN (7,8)')
        self.assertEqual(self.numbers(self.store.status_summary()), [5, 6])
        self.assertEqual(self.numbers(self.store.status_summary('history'))[:3], [9, 8, 7])
        self.assertEqual(self.store.status_summary()['counts']['closed'], 3)
        self.assertEqual(self.store.summary()[6]['status'], 'running')

    def test_unknown_status_stays_visible_for_diagnosis(self):
        self.store.finish(1, 'unknown_future_state')
        snapshot = self.store.status_summary()
        self.assertIn(1, self.numbers(snapshot))
        self.assertIn('unknown_future_state 1', status_overview(snapshot))

    def test_legacy_database_without_visibility_table(self):
        with self.store.connect() as db:
            db.execute('DROP TABLE pr_visibility')
        self.assertEqual(self.numbers(self.store.status_summary()), [8, 7, 5, 6])
        self.assertEqual(self.numbers(self.store.status_summary('history')), [9, 4, 3, 2, 1])

    def test_cli_matches_console_query_and_preserves_database(self):
        before = self.store.path.read_bytes()
        for options, view in [((), 'todo'), (('--all',), 'all'),
                              (('--history',), 'history'), (('--history', '--limit', '2'), 'history')]:
            with self.subTest(options=options):
                jobs, overview = self.run_cli(*options)
                snapshot = self.store.status_summary(view, 2 if '--limit' in options else 20)
                self.assertEqual(jobs, snapshot['jobs'])
                self.assertEqual(overview.strip(), status_overview(snapshot))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(self.store.post_state('sent-marker'), 'sent')
        self.assertEqual(self.store.post_state('pending-marker'), 'pending')

    def test_cli_empty_database_path_does_not_create_database(self):
        empty = self.root/'empty'
        empty.mkdir()
        jobs, overview = self.run_cli(root=empty)
        self.assertEqual(jobs, [])
        self.assertIn('待办：显示 0/0 条', overview)
        self.assertFalse((empty/'state.sqlite3').exists())

    def test_cli_legacy_schema_remains_read_only(self):
        with self.store.connect() as db:
            db.execute('DROP TABLE pr_visibility')
        before = self.store.path.read_bytes()
        self.assertEqual([row['number'] for row in self.run_cli()[0]], [8, 7, 5, 6])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_cli_invalid_flags_fail_before_loading_configuration(self):
        cases = [['--all'], ['--history'], ['--limit', '2'],
                 ['--status', '--all', '--history'], ['--status', '--limit', '2'],
                 ['--status', '--history', '--limit', '0'],
                 ['--status', '--history', '--limit', '-1'],
                 ['--status', '--history', '--limit', 'invalid']]
        for options in cases:
            with self.subTest(options=options), \
                 patch.object(cli.sys, 'argv', ['watcher.py', *options]), \
                 patch.object(cli, 'load_config') as load, contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.main()
                self.assertEqual(error.exception.code, 2)
                load.assert_not_called()

    def test_console_views_fetch_titles_only_for_displayed_jobs(self):
        output = []
        watcher = SimpleNamespace(store=self.store, scheduler=SimpleNamespace(pr_details={}),
                                  gc=Mock(return_value={'title': 'Example'}))
        console = Console(watcher, writer=output.append)
        for command, expected in [('status', [8, 7, 5, 6]),
                                  ('status --all', [8, 7, 5, 6, 1, 2, 3, 4]),
                                  ('status --history --limit 2', [9, 4])]:
            with self.subTest(command=command), patch.object(console, 'pr_title', return_value='Title') as title:
                console.execute(command)
                self.assertEqual([call.args[0] for call in title.call_args_list], expected)
                self.assertIn('基线跳过 1', output[-1])

    def test_console_empty_todo_still_reports_hidden_history(self):
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='done' WHERE status!='closed'")
        output = []
        console = Console(SimpleNamespace(store=self.store), writer=output.append)
        console.execute('status')
        self.assertIn('暂无待办任务。', output[-1])
        self.assertIn('已完成 8', output[-1])

    def test_console_rejects_invalid_status_options(self):
        console = Console(SimpleNamespace(store=self.store))
        for options in ['--all --history', '--limit 2', '--history --limit 0',
                        '--history --limit -1', '--history --limit invalid', '--unknown']:
            with self.subTest(options=options), self.assertRaises(ValueError):
                console.execute('status ' + options)


if __name__ == '__main__':
    unittest.main()
