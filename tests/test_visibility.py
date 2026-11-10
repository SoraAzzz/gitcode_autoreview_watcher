# PR 开放状态同步：关闭隐藏、重新打开待确认，失败轮询不更改名单。
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from review_watcher.common import WatchError
from review_watcher.state import Store
from review_watcher.scheduler import Scheduler


class TestVisibility(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(Path(temp.name)/'state.db', 'infra-test/gctest1')
        self.pr = {'number': 5, 'created_at': '2099-01-01T00:00:00Z'}
        self.store.discover([])
        self.store.discover([self.pr])

    def test_closed_or_merged_disappears_from_active_lists(self):
        self.store.discover([])
        self.assertEqual(self.store.pending_confirmations(), [])
        self.assertEqual(self.store.summary(active_only=True), [])
        self.assertEqual(len(self.store.summary()), 1)
        self.assertIsNone(self.store.next_job())

    def test_reopened_requires_confirmation_and_keeps_dedup(self):
        self.store.post('marker', 5, 'sent', '1')
        self.store.decide(5, True)
        self.store.begin(5)
        self.store.finish(5, 'done')
        self.store.discover([])
        self.store.discover([self.pr], False)
        self.assertEqual(self.store.pending_confirmations(), [5])
        self.assertIsNone(self.store.next_job())
        self.assertEqual(self.store.post_state('marker'), 'sent')
        self.store.decide(5, True)
        self.assertEqual(self.store.next_job(), 5)

    def test_failed_poll_preserves_open_records(self):
        scheduler = Scheduler({'require_confirmation': True}, self.store,
                              Mock(side_effect=WatchError('offline')), Mock())
        with self.assertRaises(WatchError):
            scheduler.poll()
        self.assertEqual(self.store.pending_confirmations(), [5])

    def test_invalid_page_does_not_hide_records(self):
        with self.assertRaises(ValueError):
            self.store.discover([{'number': 6, 'created_at': 'bad'}])
        self.assertEqual(self.store.pending_confirmations(), [5])

    def test_running_closed_job_cannot_requeue_on_failure(self):
        self.store.decide(5, True)
        self.store.begin(5)
        self.store.discover([])
        self.assertEqual(self.store.summary(active_only=True), [])
        self.store.fail(5, 'stale', 3)
        self.assertEqual(self.store.summary()[0]['status'], 'closed')
        self.assertIsNone(self.store.next_job())

    def test_legacy_closed_job_reopens(self):
        self.store.finish(5, 'closed')
        with self.store.connect() as db:
            db.execute('DELETE FROM pr_visibility')
        self.store.discover([self.pr])
        self.assertEqual(self.store.pending_confirmations(), [5])

    def test_open_done_job_not_requeued_each_poll(self):
        self.store.finish(5, 'done')
        self.store.discover([self.pr])
        self.assertEqual(self.store.summary(active_only=True)[0]['status'], 'done')
        self.assertEqual(self.store.pending_confirmations(), [])
