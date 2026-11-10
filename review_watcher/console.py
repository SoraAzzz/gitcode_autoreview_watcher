# 持续交互终端：前台读取命令，后台调度轮询；退出时等待在途任务完成。
"""Interactive command loop for an already configured watcher."""
import shlex
import threading
import time

from .common import LOG, WatchError
from .commands import redact


class Console:
    """控制台仅改变持久化任务状态，不直接调用模型或发布评论。"""

    def __init__(self, watcher, reader=input, writer=print):
        self.watcher = watcher
        self.reader, self.write = reader, writer
        self.stop = threading.Event()
        self.poll_requested = threading.Event()
        self.notified = set()
        self.failure = None
        self.details_cache = {}
        self.details_lock = threading.Lock()

    def pr_title(self, number):
        """优先复用轮询标题，仅在缺少标题时查询详情并缓存。"""
        snapshot = getattr(self.watcher.scheduler, 'pr_details', {})
        pr = snapshot.get(number, {}) if isinstance(snapshot, dict) else {}
        if not isinstance(pr.get('title'), str) or not pr['title'].strip():
            with self.details_lock:
                cached = self.details_cache.get(number)
                if cached and time.monotonic() - cached[0] < 300:
                    pr = cached[1]
                else:
                    try:
                        detail = self.watcher.gc('pr', 'view', str(number))
                        if not isinstance(detail, dict):
                            raise ValueError('Invalid PR details')
                        pr = detail
                        self.details_cache[number] = (time.monotonic(), detail)
                    except (WatchError, OSError, ValueError) as exc:
                        LOG.warning('PR %d title unavailable (%s)', number, type(exc).__name__)
        title = str(pr.get('title') or '（标题暂不可用）').replace('\n', ' ')
        text = redact(title)
        return ''.join(c for c in text if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) <= 159))

    def result_notice(self, result):
        """一次输出完整结果，不从可能被滚动清理的报告目录重新读取。"""
        findings = result['findings']
        publication = (f"已发布或确认已有 {len(result['comment_ids'])} 条行内评论"
                       if result['published'] else '仅评审，未发布评论')
        lines = [f"\nPR #{result['number']} 评审成功：{len(findings)} 个问题；{publication}。",
                 f"https://gitcode.com/{result['repository']}/pull/{result['number']}"]
        if result.get('summary'):
            lines.append(f"摘要：{result['summary']}")
        if not findings:
            lines.append('未发现符合发布条件的 P1/P2 问题。')
        for index, item in enumerate(findings, 1):
            lines.extend([f"\n{index}. [{item['priority']}] {item['title']}",
                          f"位置：{item['path']}:{item['line']}", item['body'],
                          f"依据：{item['evidence']}"])
        lines.append('\n可继续输入命令（help 查看用法）。')
        # 模型输出与文件名不可信，过滤终端控制字符并沿用凭证脱敏。
        message = redact('\n'.join(lines))
        message = ''.join(c for c in message if c in '\n\t' or (ord(c) >= 32 and not 127 <= ord(c) <= 159))
        self.write(message)

    def pending_notice(self, numbers):
        """只提示新增的待确认编号，避免每轮轮询刷屏。"""
        current = set(numbers)
        fresh = current - self.notified
        self.notified = current
        if fresh:
            details = [f'待确认 PR: {number}  {self.pr_title(number)}' for number in sorted(fresh)]
            self.write('\n' + '\n\n'.join(details) +
                       '\n输入 approve <编号> 批准，或 decline <编号> 拒绝。')

    def background(self):
        """记录调度故障并通知用户，避免后台线程静默退出。"""
        try:
            self.watcher.scheduler.serve(stop_event=self.stop,
                                         poll_event=self.poll_requested,
                                         on_pending=self.pending_notice)
        except Exception as exc:
            self.failure = exc
            self.stop.set()
            LOG.error('Interactive scheduler stopped (%s)', type(exc).__name__)
            self.write('\n调度已停止，请按回车退出并检查 watcher.log。')

    def execute(self, line):
        """只接受白名单命令；返回 False 表示请求退出，不执行任意 shell 内容。"""
        words = shlex.split(line)
        if not words:
            return True
        command, *args = words
        if command in ('quit', 'exit') and not args:
            return False
        if command == 'help' and not args:
            self.write('help | status | pending | approve <PR> | decline <PR> | '
                       'retry <PR> | poll | quit\n'
                       'approve 表示确认开始评审；retry 复用已有报告缓存。'
                       ' quit 停止调度并等待在途任务完成。')
        elif command == 'status' and not args:
            rows = self.watcher.store.summary(active_only=True)
            self.write('\n\n'.join(f"#{r['number']}  {r['status']}  attempts={r['attempts']}  {r['error']}\n"
                                 f"{self.pr_title(r['number'])}"
                                 for r in rows) or '暂无任务。')
        elif command == 'pending' and not args:
            numbers = self.watcher.store.pending_confirmations()
            self.write('\n'.join(f'待确认 PR: {number}  {self.pr_title(number)}'
                                   for number in numbers) or '待确认: 无')
        elif command == 'poll' and not args:
            self.poll_requested.set()
            self.write('已请求轮询。')
        elif command in ('approve', 'decline', 'retry') and len(args) == 1:
            if not args[0].isdigit() or int(args[0]) <= 0:
                raise ValueError('PR 编号必须是正整数')
            number = int(args[0])
            if command == 'retry':
                self.watcher.store.retry(number)
            else:
                self.watcher.store.decide(number, command == 'approve')
            LOG.info('Console command: %s PR %d', command, number)
            self.write(f'PR {number}: {command} 已保存。')
        else:
            raise ValueError('未知命令或参数数量错误；输入 help 查看用法')
        return True

    def run(self):
        """主线程持续读取命令；EOF、Ctrl+C 与 quit 都执行有序停止。"""
        self.write(f"交互模式：{self.watcher.repo}；模型："
                   f"{self.watcher.config.get('model') or 'Codex 默认'}\n"
                   '输入 help 查看命令；运行日志见状态目录中的 watcher.log。')
        thread = threading.Thread(target=self.background, name='watcher-scheduler')
        previous_callback = getattr(self.watcher, 'on_result', None)
        self.watcher.on_result = self.result_notice
        thread.start()
        try:
            while not self.stop.is_set():
                try:
                    line = self.reader('watcher> ')
                    if self.stop.is_set() or not self.execute(line):
                        break
                except (WatchError, ValueError) as exc:
                    self.write(f'命令失败：{exc}')
        except (EOFError, KeyboardInterrupt):
            pass
        finally:
            self.stop.set()
            self.write('\n正在停止：等待当前轮询或评审完成，不再启动新任务……')
            # 保持实例锁，直到工作线程完成，避免退出期间启动第二个监听器。
            while thread.is_alive():
                try:
                    thread.join(timeout=0.2)
                except KeyboardInterrupt:
                    self.write('仍在等待在途任务完成。')
            self.watcher.on_result = previous_callback
        if self.failure is not None:
            raise WatchError('Interactive scheduler failed; inspect watcher.log') from self.failure
