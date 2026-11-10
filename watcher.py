#!/usr/bin/env python3
# 兼容启动入口及历史导入；实际逻辑在 review_watcher 包中。
"""Compatibility entry point. Implementation lives in review_watcher/."""
import os
import subprocess
import sys

from review_watcher.common import Attention, Stale, WatchError, repo_name, sha
from review_watcher.commands import redact, run
from review_watcher.config import select_repository
from review_watcher.state import Store, instance_lock
from review_watcher.validation import changed_lines, validate_report
from review_watcher.reviewer import codex_review_args
from review_watcher.pipeline import Watcher
from review_watcher.cli import main, entrypoint

if __name__ == '__main__':
    entrypoint()
