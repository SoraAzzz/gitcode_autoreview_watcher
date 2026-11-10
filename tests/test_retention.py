# 清理测试仅使用临时目录，验证保留顺序、状态保护与路径边界。
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from review_watcher.retention import prune_runs
from review_watcher.state import Store


class TestRetention(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root/'state.sqlite3', 'infra-test/gctest1')
        for number in range(1, 8):
            self.add_run(number)

    def add_run(self, number, suffix='a'):
        path = self.root/'runs'/f'{number}-{suffix*12}-{"b"*12}'
        path.mkdir(parents=True)
        (path/'review.json').write_text('{}')
        os.utime(path, (number, number))
        with self.store.connect() as db:
            db.execute("INSERT OR IGNORE INTO jobs(number,status) VALUES (?, 'done')", (number,))
        return path

    def remaining(self):
        return {int(p.name.split('-')[0]) for p in (self.root/'runs').iterdir() if p.name[0].isdigit()}

    def test_keeps_five_prs_and_durable_state(self):
        self.store.post('marker', 1, 'sent', 55)
        prune_runs(self.root, self.store)
        self.assertEqual(self.remaining(), {3, 4, 5, 6, 7})
        self.assertEqual(len(self.store.summary()), 7)
        self.assertEqual(self.store.post_state('marker'), 'sent')

    def test_same_pr_multiple_runs_count_once(self):
        self.add_run(1, 'c')
        self.add_run(7, 'c')
        prune_runs(self.root, self.store)
        self.assertEqual(self.remaining(), {3, 4, 5, 6, 7})
        self.assertEqual(len(list((self.root/'runs').glob('7-*'))), 2)

    def test_recently_retried_old_pr_is_retained(self):
        os.utime(next((self.root/'runs').glob('1-*')), (100, 100))
        prune_runs(self.root, self.store)
        self.assertEqual(self.remaining(), {1, 4, 5, 6, 7})

    def test_active_and_uncertain_publish_are_protected(self):
        self.store.finish(1, 'queued')
        self.store.post('pending-marker', 2, 'pending')
        prune_runs(self.root, self.store)
        self.assertEqual(self.remaining(), set(range(1, 8)))

    def test_terminal_failed_records_can_roll_off(self):
        self.store.finish(1, 'needs_attention')
        prune_runs(self.root, self.store)
        self.assertNotIn(1, self.remaining())

    def test_unrecognized_directory_is_not_deleted(self):
        extra = self.root/'runs'/'manual-backup'
        extra.mkdir()
        prune_runs(self.root, self.store)
        self.assertTrue(extra.exists())

    def test_unsafe_tree_is_not_removed(self):
        with patch('review_watcher.retention.safe_tree', return_value=False):
            prune_runs(self.root, self.store)
        self.assertEqual(len(self.remaining()), 7)

    def test_delete_error_does_not_abort_retention(self):
        with patch('review_watcher.retention.shutil.rmtree', side_effect=PermissionError):
            prune_runs(self.root, self.store)
        self.assertEqual(len(self.remaining()), 7)


if __name__ == '__main__':
    unittest.main()
