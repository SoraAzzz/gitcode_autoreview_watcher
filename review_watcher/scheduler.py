# 主进程调度：发现新 PR、请求用户确认，并用单个工作线程执行评审。
"""Parent-process discovery, user confirmation and bounded worker scheduling."""
import concurrent.futures
import sys
import threading
import time
from .common import LOG, WatchError

# 仅负责发现、确认和调度；通过 process 回调执行单任务，不构造模型命令。
class Scheduler:
    # 绑定配置、状态、客户端及任务回调，调度器不依赖具体评审后端。
    def __init__(self, config, store, client, process):
        self.config, self.store, self.gc, self.process = config, store, client, process
        self.pr_details = {}

    # 完整获取开放 PR 列表再交给存储层；首次成功轮询只记录基线。
    def poll(self):
        prs = self.gc('pr', 'list', '--state', 'open', '--paginate', '--per-page', '100')
        if prs is None: prs = []  # Some CLI versions encode an empty Go slice as null.
        if not isinstance(prs, list): raise WatchError('PR list was not a JSON array')
        first = self.store.discover(prs, self.config['require_confirmation'])
        # 整批替换只读展示快照，控制台复用列表中的标题和描述，避免逐条请求。
        self.pr_details = {pr['number']: pr for pr in prs}
        LOG.info('%s: %d open PRs', 'Baseline saved; existing PRs skipped' if first else 'Poll OK', len(prs))


    # TTY 下逐个确认；非交互环境保留待确认状态，等待外部批准命令。
    def confirm_pending(self):
        pending = self.store.pending_confirmations()
        if not pending:
            return
        if not sys.stdin.isatty():
            LOG.info('%d PR(s) await confirmation; use --approve-pr/--decline-pr', len(pending))
            return
        for number in pending:
            pr = self.gc('pr', 'view', str(number))
            if pr.get('state') != 'open' or pr.get('merged'):
                self.store.finish(number, 'closed')
                continue
            draft = ' [draft]' if pr.get('draft') else ''
            title = str(pr.get('title') or '')
            url = str(pr.get('html_url') or '')
            print(f'New PR #{number}{draft}: {title}\n{url}')
            model = self.config.get('model') or 'configured Codex default'
            answer = input(f'Start Codex review with model {model}? [y/N] ').strip().lower()
            approved = answer in ('y', 'yes')
            self.store.decide(number, approved)
            LOG.info('PR %d %s by user', number, 'approved' if approved else 'declined')


    # 支持单次、仅发现和持续模式；持续模式由主线程轮询，单工作线程评审。
    def serve(self, once=False, discover_only=False, *, stop_event=None,
              poll_event=None, on_pending=None):
        if once or discover_only:
            self.poll()
            if not discover_only:
                if on_pending is None:
                    self.confirm_pending()
                else:
                    on_pending(self.store.pending_confirmations())
                while (number := self.store.next_job()) is not None:
                    self.process(number)
            return
        # 交互模式由控制台独占 stdin；调度线程仅报告待确认任务，不读取输入。
        stop_event = stop_event if stop_event is not None else threading.Event()
        poll_event = poll_event if poll_event is not None else threading.Event()
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future, next_poll = None, 0
            try:
                while not stop_event.is_set():
                    if time.monotonic() >= next_poll or poll_event.is_set():
                        poll_event.clear()
                        try:
                            self.poll()
                            if on_pending is None:
                                self.confirm_pending()
                            else:
                                on_pending(self.store.pending_confirmations())
                        except Exception as exc: LOG.error('Poll failed (%s); baseline/state preserved', type(exc).__name__)
                        next_poll = time.monotonic()+self.config['poll_seconds']
                    # 退出请求可能在网络轮询期间到达；此时不得再启动新的评审。
                    if stop_event.is_set():
                        break
                    if future is None or future.done():
                        if future is not None: future.result()
                        number = self.store.next_job()
                        future = pool.submit(self.process, number) if number is not None else None
                    stop_event.wait(1)
            except KeyboardInterrupt:
                LOG.info('Stopping discovery; waiting for current review to finish')

