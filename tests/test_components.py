# 组件协作测试：通过注入替身验证调度和评审调用边界，不发布远端评论。
"""Regression tests for component boundaries and dependency injection."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from review_watcher.common import ROOT, Attention
from review_watcher.pipeline import Watcher
from review_watcher.reviewer import CodexReviewer
from review_watcher.reviewer import codex_review_args
from review_watcher.scheduler import Scheduler
from review_watcher.config import load_config
from review_watcher.common import WatchError
from tests.test_watcher import PR, BASE, report


class TestComponents(unittest.TestCase):
    def test_configured_effort_reaches_codex_and_missing_defaults_low(self):
        for effort in (None, 'low', 'medium', 'high', 'xhigh'):
            config = {} if effort is None else {'reasoning_effort': effort}
            args = codex_review_args('codex', config, Path('job'), Path('report.json'))
            self.assertIn('model_reasoning_effort='+json.dumps(effort or 'low'), args)

    def test_effort_config_defaults_and_rejects_invalid_values(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads((ROOT/'config.json').read_text(encoding='utf-8-sig'))
            config['state_dir'] = 'runtime'
            path = Path(directory)/'config.json'
            args = SimpleNamespace(config=path, model=None, repo=None)
            config.pop('reasoning_effort', None)
            path.write_text(json.dumps(config), encoding='utf-8')
            self.assertEqual(load_config(args)[0]['reasoning_effort'], 'low')
            for value in ('', 'deep', None, True, 1, []):
                config['reasoning_effort'] = value
                path.write_text(json.dumps(config), encoding='utf-8')
                with self.assertRaises(WatchError):
                    load_config(args)

    def test_native_sandbox_explicit_without_weakening_policy(self):
        job, output = Path('job'), Path('review.json')
        for platform in ('nt', 'posix'):
            with self.subTest(platform=platform), patch('review_watcher.reviewer.os.name', platform):
                args = codex_review_args('codex', {'model': 'test-model'}, job, output)
            self.assertEqual('windows.sandbox="elevated"' in args, platform == 'nt')
            self.assertEqual(args[args.index('--sandbox') + 1], 'read-only')
            self.assertIn('approval_policy="never"', args)
            self.assertNotIn('--ignore-rules', args)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox', args)

    def test_injected_backend_flows_through_pipeline(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            config = {'repo': 'infra-test/gctest1', 'publish': True, 'max_attempts': 3}
            client = Mock(side_effect=[PR, []])
            workspace = Mock()
            workspace.prepare.return_value = (BASE, {'src/a.py': {4}})
            reviewer = Mock()
            reviewer.review.return_value = (report(), report()['findings'])
            publisher = Mock()
            publisher.publish_one.return_value = 33
            watcher = Watcher(config, state, {}, client=client, workspace=workspace,
                              reviewer=reviewer, publisher=publisher)
            notification = Mock()
            watcher.on_result = notification
            watcher.store.discover([])
            watcher.store.discover([{'number': 5, 'created_at': '2099-01-01T00:00:00Z'}], False)
            watcher.process(5)
            self.assertEqual(watcher.store.summary()[0]['status'], 'done')
            reviewer.review.assert_called_once()
            publisher.ensure_current.assert_called_once_with(PR)
            publisher.publish_one.assert_called_once()
            result = notification.call_args.args[0]
            self.assertEqual(result['findings'][0]['line'], 4)
            self.assertEqual(result['comment_ids'], [33])

    def test_discovery_only_never_invokes_worker(self):
        store, worker = Mock(), Mock()
        scheduler = Scheduler({'require_confirmation': True}, store, Mock(return_value=[]), worker)
        scheduler.serve(discover_only=True)
        store.discover.assert_called_once_with([], True)
        store.pending_confirmations.assert_not_called()
        worker.assert_not_called()

    def test_once_drains_only_eligible_jobs(self):
        store, worker = Mock(), Mock()
        store.pending_confirmations.return_value = []
        store.next_job.side_effect = [5, None]
        scheduler = Scheduler({'require_confirmation': True}, store, Mock(return_value=[]), worker)
        scheduler.serve(once=True)
        worker.assert_called_once_with(5)

    def test_codex_runner_receives_prompt_model_and_artifact_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)

            def execute(args, cwd, **kwargs):
                self.assertEqual(args[args.index('--model') + 1], 'test-model')
                self.assertEqual(Path(args[args.index('--output-schema') + 1]), ROOT/'review-schema.json')
                self.assertEqual(cwd, job)
                self.assertIn('Trusted task coordinates:', kwargs['stdin'])
                self.assertNotIn('GC_TOKEN', kwargs['env'])
                self.assertNotIn('GITCODE_TOKEN', kwargs['env'])
                self.assertEqual(kwargs['diagnostics'].parent, job/'diagnostics')
                (job/'review.json').write_text(json.dumps(report()), encoding='utf-8')

            runner = Mock(side_effect=execute)
            reviewer = CodexReviewer('codex', 'infra-test/gctest1',
                                     {'model': 'test-model', 'max_findings': 5,
                                      'review_timeout_seconds': 10}, runner=runner)
            _, findings = reviewer.review(PR, job, BASE, {'src/a.py': {4}})
            self.assertEqual(len(findings), 1)
            reviewer.review(PR, job, BASE, {'src/a.py': {4}})
            runner.assert_called_once()

    def test_cached_incomplete_report_is_held_without_backend_call(self):
        with tempfile.TemporaryDirectory() as directory:
            job = Path(directory)
            value = report()
            value['status'] = 'incomplete'
            (job/'review.json').write_text(json.dumps(value), encoding='utf-8')
            runner = Mock()
            reviewer = CodexReviewer('codex', 'infra-test/gctest1', {'max_findings': 5}, runner=runner)
            with self.assertRaises(Attention):
                reviewer.review(PR, job, BASE, {'src/a.py': {4}})
            runner.assert_not_called()


if __name__ == '__main__':
    unittest.main()
