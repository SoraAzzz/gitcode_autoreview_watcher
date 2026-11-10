# 交互终端离线测试：验证命令状态转换、退出以及轮询与输入的隔离。
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from review_watcher.console import Console
from review_watcher.scheduler import Scheduler
from review_watcher.state import Store
from review_watcher.common import WatchError
from review_watcher import cli


class TestConsole(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/'state.sqlite3', 'infra-test/gctest1')
        self.store.discover([])
        self.store.discover([{'number': 5, 'created_at': '2099-01-01T00:00:00Z'}])
        self.watcher = SimpleNamespace(store=self.store, repo='infra-test/gctest1',
                                       config={'model': ''}, scheduler=Mock(),
                                       gc=Mock(return_value={'title': '修复采集异常', 'body': '处理空结果导致的崩溃'}))
        self.output = []
        self.console = Console(self.watcher, writer=self.output.append)

    def test_multiple_commands_use_same_state(self):
        for command in ('help', 'pending', 'status', 'decline 5', 'approve 5', 'poll'):
            self.assertTrue(self.console.execute(command))
        self.assertEqual(self.store.next_job(), 5)
        self.assertTrue(self.console.poll_requested.is_set())
        self.assertFalse(self.console.execute('quit'))

    def test_completion_displays_content_and_location(self):
        self.console.result_notice({'repository': 'infra-test/gctest1', 'number': 5,
                                    'published': True, 'comment_ids': [33], 'summary': 'Summary',
                                    'findings': [{'priority': 'P1', 'title': 'Title',
                                                  'path': 'src/a.py', 'line': 42,
                                                  'body': 'Condition and consequence', 'evidence': 'Caller'}]})
        text = self.output[-1]
        for expected in ('#5', 'src/a.py:42', 'Condition and consequence', 'Caller', '[P1]', 'Summary'):
            self.assertIn(expected, text)

    def test_no_findings_and_review_only_are_explicit(self):
        self.console.result_notice({'repository': 'infra-test/gctest1', 'number': 5,
                                    'published': False, 'comment_ids': [], 'findings': []})
        self.assertIn('未发布评论', self.output[-1])
        self.assertIn('未发现', self.output[-1])

    def test_invalid_commands_do_not_approve(self):
        for command in ('approve', 'approve -1', 'approve 5 extra', 'rm anything', 'approve "'):
            with self.assertRaises(ValueError):
                self.console.execute(command)
        self.assertEqual(self.store.pending_confirmations(), [5])

    def test_retry_keeps_publication_intent(self):
        self.store.finish(5, 'needs_attention')
        self.store.post('marker', 5, 'pending')
        self.console.execute('retry 5')
        self.assertEqual(self.store.next_job(), 5)
        self.assertEqual(self.store.post_state('marker'), 'pending')

    def test_pending_notice_is_not_repeated_each_poll(self):
        self.console.pending_notice([5])
        self.console.pending_notice([5])
        self.assertEqual(len(self.output), 1)

    def test_pending_and_status_show_only_title(self):
        self.console.pending_notice([5])
        self.console.execute('pending')
        self.console.execute('status')
        for text in self.output:
            self.assertIn('修复采集异常', text)
            self.assertNotIn('处理空结果导致的崩溃', text)
            self.assertNotIn('/infra-test/gctest1/pull/5', text)
        self.watcher.gc.assert_called_once()
        self.assertEqual(self.store.pending_confirmations(), [5])

    def test_poll_snapshot_avoids_extra_detail_request(self):
        self.watcher.scheduler.pr_details = {5: {'title': 'Snapshot'}}
        text = self.console.pr_title(5)
        self.assertIn('Snapshot', text)
        self.watcher.gc.assert_not_called()

    def test_failed_details_keep_status_and_can_retry(self):
        self.watcher.gc.side_effect = [WatchError('network'), {'title': 'Recovered', 'body': 'Details'}]
        self.console.execute('status')
        self.assertIn('awaiting_confirmation', self.output[-1])
        self.assertIn('标题暂不可用', self.output[-1])
        self.console.execute('pending')
        self.assertIn('Recovered', self.output[-1])

    def test_description_omitted_and_title_control_chars_removed(self):
        self.watcher.gc.return_value = {'title': '\x1bTitle', 'body': 'A' * 1000}
        text = self.console.pr_title(5)
        self.assertNotIn('\x1b', text)
        self.assertEqual(text, 'Title')

    def test_command_error_does_not_end_session_and_exit_joins_thread(self):
        finished = threading.Event()
        def serve(**kwargs):
            kwargs['stop_event'].wait(5)
            finished.set()
        self.watcher.scheduler.serve.side_effect = serve
        commands = iter(['unknown', 'approve 5', 'quit'])
        self.console.reader = lambda prompt: next(commands)
        self.console.run()
        self.assertTrue(finished.is_set())
        self.assertEqual(self.store.next_job(), 5)

    def test_eof_stops_background(self):
        self.watcher.scheduler.serve.side_effect = lambda **kw: kw['stop_event'].wait(5)
        self.console.reader = Mock(side_effect=EOFError)
        self.console.run()
        self.assertTrue(self.console.stop.is_set())

    def test_scheduler_failure_is_reported(self):
        self.watcher.scheduler.serve.side_effect = RuntimeError('fixture')
        self.console.reader = lambda prompt: self.console.stop.wait(5) or ''
        with self.assertRaises(WatchError):
            self.console.run()

    def test_stop_during_poll_does_not_start_job(self):
        stop, process = threading.Event(), Mock()
        def client(*args):
            stop.set()
            return []
        self.store.decide(5, True)
        scheduler = Scheduler({'require_confirmation': True, 'poll_seconds': 300},
                              self.store, client, process)
        scheduler.serve(stop_event=stop, on_pending=Mock())
        process.assert_not_called()

    def test_once_callback_never_reads_terminal(self):
        callback = Mock()
        scheduler = Scheduler({'require_confirmation': True}, self.store,
                              Mock(return_value=[{'number': 5, 'created_at': '2099-01-01T00:00:00Z'}]), Mock())
        scheduler.confirm_pending = Mock(side_effect=AssertionError('must not read stdin'))
        scheduler.serve(once=True, on_pending=callback)
        callback.assert_called_once_with([5])

    def test_tty_default_routes_to_console(self):
        with patch.object(cli.sys, 'argv', ['watcher.py']), \
             patch.object(cli.sys.stdin, 'isatty', return_value=True), \
             patch.object(cli, 'load_config', return_value=({'repo': 'infra-test/gctest1'}, Path(self.temp.name))), \
             patch.object(cli, 'resolve_tool', return_value='fixture'), \
             patch.object(cli, 'RotatingFileHandler'), \
             patch.object(cli.logging, 'basicConfig'), \
             patch.object(cli, 'Watcher') as watcher, patch.object(cli, 'Console') as console:
            cli.main()
            console.assert_called_once_with(watcher.return_value)
            console.return_value.run.assert_called_once()
            watcher.return_value.serve.assert_not_called()

    def test_explicit_console_rejects_non_tty_before_configuration(self):
        with patch.object(cli.sys, 'argv', ['watcher.py', '--interactive']), \
             patch.object(cli.sys.stdin, 'isatty', return_value=False), \
             patch.object(cli, 'load_config') as load, patch.object(cli.sys, 'stderr'):
            with self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(error.exception.code, 2)
            load.assert_not_called()

    def test_no_interactive_uses_non_prompting_scheduler(self):
        with patch.object(cli.sys, 'argv', ['watcher.py', '--no-interactive']), \
             patch.object(cli.sys.stdin, 'isatty', return_value=True), \
             patch.object(cli, 'load_config', return_value=({'repo': 'infra-test/gctest1'}, Path(self.temp.name))), \
             patch.object(cli, 'resolve_tool', return_value='fixture'), \
             patch.object(cli, 'RotatingFileHandler'), \
             patch.object(cli.logging, 'basicConfig'), \
             patch.object(cli, 'Watcher') as watcher, patch.object(cli, 'Console') as console:
            cli.main()
            console.assert_not_called()
            self.assertTrue(callable(watcher.return_value.scheduler.serve.call_args.kwargs['on_pending']))


if __name__ == '__main__':
    unittest.main()
