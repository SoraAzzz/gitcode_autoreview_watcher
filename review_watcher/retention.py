# 运行记录保留策略：按 PR 最近使用时间滚动清理，保留数据库中的状态和去重凭据。
"""Bound retained PR artifacts without removing active jobs or following links."""
import os
import re
import shutil
import stat

from .common import LOG

RUN_NAME = re.compile(r'([1-9][0-9]*)-[0-9a-f]{12}(?:[0-9a-f]{28})?-[0-9a-f]{12}(?:[0-9a-f]{28})?')
FINISHED = {'done', 'closed', 'declined', 'needs_attention', 'reviewed_only'}


def is_link(path):
    """拒绝符号链接和 Windows junction 等重解析点。"""
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def safe_tree(path, root):
    """递归删除前核验绝对边界和整棵目录树，避免访问 runs 之外的目标。"""
    if is_link(path) or path.resolve().parent != root:
        return False
    for current, directories, files in os.walk(path, followlinks=False):
        for name in directories + files:
            if is_link(type(path)(current)/name):
                return False
    return True


def remove_readonly(function, path, error):
    """Git 对象可能为只读文件；仅在删除文件失败时清除只读位后重试。"""
    if function not in (os.unlink, os.remove) or not isinstance(error[1], PermissionError):
        raise error[1]
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    function(path)


def prune_runs(state, store, keep=5):
    """保留最近 keep 个 PR（同 PR 多次运行算一条），另保护活跃任务及不确定发送。"""
    runs = state/'runs'
    if not runs.exists() or is_link(runs):
        return
    root = runs.resolve()
    groups = {}
    for path in runs.iterdir():
        match = RUN_NAME.fullmatch(path.name)
        if match and path.is_dir() and not is_link(path):
            groups.setdefault(int(match[1]), []).append(path)
    newest = sorted(groups, key=lambda n: (max(p.stat().st_mtime_ns for p in groups[n]), n), reverse=True)
    # 与外部 approve/retry 命令互斥，防止刚选中清理的任务同时重新入队。
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        statuses = dict(db.execute('SELECT number,status FROM jobs'))
        pending = {row[0] for row in db.execute("SELECT DISTINCT number FROM posts WHERE status='pending'")}
        for number in newest[keep:]:
            if statuses.get(number) not in FINISHED or number in pending:
                LOG.info('Retention preserved PR %d: active/unknown state or pending publication', number)
                continue
            for path in groups[number]:
                try:
                    if not safe_tree(path, root):
                        LOG.warning('Retention skipped unsafe directory: %s', path)
                        continue
                    shutil.rmtree(path, onerror=remove_readonly)
                    LOG.info('Retention removed PR %d artifacts: %s', number, path)
                except OSError as exc:
                    LOG.warning('Retention could not remove %s (%s)', path, type(exc).__name__)
