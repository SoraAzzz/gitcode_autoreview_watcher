# 持久化层：维护任务状态及评论去重记录；进程锁限制同一仓库只能运行一个监听器。
"""SQLite job transitions, publication intent and instance locking."""
import contextlib
import datetime as dt
import os
import sqlite3
import time
from .common import WatchError, utcnow

# SQLite 状态仓库；每次操作独立连接，可供调度线程和工作线程共同使用。
class Store:
    # 绑定数据库与仓库；普通启动恢复 running，外部批准命令须禁用恢复以免干扰在途任务。
    def __init__(self, path, repo, recover_running=True):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    number INTEGER PRIMARY KEY, status TEXT NOT NULL, attempts INTEGER DEFAULT 0,
                    due REAL DEFAULT 0, error TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS posts (
                    marker TEXT PRIMARY KEY, number INTEGER, status TEXT NOT NULL, comment_id TEXT);
                CREATE TABLE IF NOT EXISTS pr_visibility (
                    number INTEGER PRIMARY KEY, is_open INTEGER NOT NULL);
            ''')
            previous = db.execute("SELECT value FROM meta WHERE key='repo'").fetchone()
            if previous and previous[0] != repo:
                raise WatchError('State directory belongs to another repository')
            db.execute("INSERT OR IGNORE INTO meta VALUES ('repo', ?)", (repo,))
            if recover_running:
                db.execute("UPDATE jobs SET status='queued' WHERE status='running'")

    # 提供事务上下文：成功提交、异常回滚，结束后始终关闭连接。
    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    # 先校验整批数据再写入；已有 PR 标为 baseline，新 PR 默认等待确认。
    def discover(self, prs, require_confirmation=True):
        # Validate the entire page set before recording a baseline or adding jobs.
        for pr in prs:
            if type(pr.get('number')) is not int or pr['number'] <= 0:
                raise WatchError('Invalid PR number in list response')
            dt.datetime.fromisoformat(pr['created_at'].replace('Z', '+00:00'))
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT value FROM meta WHERE key='baseline'").fetchone()
            first = row is None
            baseline = utcnow() if first else row[0]
            if first:
                db.execute("INSERT INTO meta VALUES ('baseline', ?)", (baseline,))
            for pr in prs:
                previous = db.execute('SELECT is_open FROM pr_visibility WHERE number=?',
                                      (pr['number'],)).fetchone()
                old_job = db.execute('SELECT status FROM jobs WHERE number=?', (pr['number'],)).fetchone()
                created = dt.datetime.fromisoformat(pr['created_at'].replace('Z', '+00:00'))
                old = first or created <= dt.datetime.fromisoformat(baseline)
                db.execute('INSERT OR IGNORE INTO jobs(number,status) VALUES (?,?)',
                           (pr['number'], 'baseline' if old else
                            ('awaiting_confirmation' if require_confirmation else 'queued')))
                # 关闭后重新出现必须再次确认；保留 posts 防止重复评论。
                if (previous and previous[0] == 0) or (old_job and old_job[0] == 'closed'):
                    db.execute("UPDATE jobs SET status='awaiting_confirmation',attempts=0,due=0,error='' "
                               "WHERE number=? AND status!='running'", (pr['number'],))
                db.execute('INSERT OR REPLACE INTO pr_visibility VALUES (?,1)', (pr['number'],))
            # prs 是成功拉取的完整开放列表；失败或解析错误不得走到这里。
            open_numbers = {pr['number'] for pr in prs}
            for number, status in db.execute('SELECT number,status FROM jobs').fetchall():
                if number not in open_numbers:
                    db.execute('INSERT OR REPLACE INTO pr_visibility VALUES (?,0)', (number,))
                    if status != 'running':
                        db.execute("UPDATE jobs SET status='closed',error='',due=0 WHERE number=?", (number,))
            return first

    # 按 PR 编号返回等待用户决定的任务。
    def pending_confirmations(self):
        with self.connect() as db:
            return [row[0] for row in db.execute(
                "SELECT number FROM jobs WHERE status='awaiting_confirmation' "
                "AND number NOT IN (SELECT number FROM pr_visibility WHERE is_open=0) ORDER BY number")]

    # 只允许待确认或已拒绝任务改变批准状态，不干扰正在执行的任务。
    def decide(self, number, approved):
        with self.connect() as db:
            row = db.execute('SELECT status FROM jobs WHERE number=?', (number,)).fetchone()
            if not row or row[0] not in ('awaiting_confirmation', 'declined'):
                raise WatchError('Only awaiting_confirmation/declined jobs can be approved or declined')
            status = 'queued' if approved else 'declined'
            db.execute("UPDATE jobs SET status=?,error='',due=0 WHERE number=?", (status, number))

    # 查询最早编号且重试时间已到的 queued 任务；本方法不领取任务。
    def next_job(self):
        with self.connect() as db:
            row = db.execute("SELECT number FROM jobs WHERE status='queued' AND due<=? "
                             'AND number NOT IN (SELECT number FROM pr_visibility WHERE is_open=0) '
                             'ORDER BY number LIMIT 1', (time.time(),)).fetchone()
            return row[0] if row else None

    # 标记 running 并增加尝试次数，由单个工作线程在处理开始时调用。
    def begin(self, number):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='running',attempts=attempts+1 WHERE number=?", (number,))

    # 保存任务状态、错误和下次可执行时间；delay 单位为秒。
    def finish(self, number, status, error='', delay=0):
        with self.connect() as db:
            visible = db.execute('SELECT is_open FROM pr_visibility WHERE number=?', (number,)).fetchone()
            if visible and visible[0] == 0:
                status, error, delay = 'closed', '', 0
            db.execute('UPDATE jobs SET status=?,error=?,due=? WHERE number=?',
                       (status, error, time.time()+delay, number))

    # 未达次数上限时指数退避，达到上限后转人工处理。
    def fail(self, number, error, max_attempts):
        with self.connect() as db:
            attempts = db.execute('SELECT attempts FROM jobs WHERE number=?', (number,)).fetchone()[0]
        status = 'needs_attention' if attempts >= max_attempts else 'queued'
        self.finish(number, status, error, min(3600, 300 * 2**min(attempts-1, 4)))

    # 查询评论标记的发送状态，用于跨重启防重。
    def post_state(self, marker):
        with self.connect() as db:
            row = db.execute('SELECT status FROM posts WHERE marker=?', (marker,)).fetchone()
            return row[0] if row else None

    # 保存评论发送意图或确认结果；pending 必须先于远端发布落库。
    def post(self, marker, number, status, comment_id=''):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO posts VALUES (?,?,?,?)',
                       (marker, number, status, str(comment_id)))

    # 显式重试人工挂起或仅评审任务，重置次数但保留评论去重记录。
    def retry(self, number):
        with self.connect() as db:
            row = db.execute('SELECT status FROM jobs WHERE number=?', (number,)).fetchone()
            if not row or row[0] not in ('needs_attention', 'reviewed_only'):
                raise WatchError('Only needs_attention/reviewed_only jobs can be retried')
            db.execute("UPDATE jobs SET status='queued',attempts=0,due=0,error='' WHERE number=?", (number,))

    # 返回任务状态摘要，供状态命令或本地诊断使用。
    def summary(self, active_only=False):
        with self.connect() as db:
            query = 'SELECT number,status,attempts,error FROM jobs'
            if active_only:
                query += " WHERE status!='closed' AND number NOT IN (SELECT number FROM pr_visibility WHERE is_open=0)"
            return [dict(zip(('number','status','attempts','error'), row)) for row in
                    db.execute(query+' ORDER BY number')]


# 使用操作系统文件锁限制同目录实例；句柄关闭后释放锁。
@contextlib.contextmanager
def instance_lock(path):
    handle = path.open('a+b')
    try:
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise WatchError('Another watcher is using this state directory') from exc
    try:
        yield
    finally:
        handle.close()

