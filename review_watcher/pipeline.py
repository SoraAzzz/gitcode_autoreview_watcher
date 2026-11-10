# 单 PR 流程编排：串联准备、评审、发布和状态更新；组件通过构造参数注入。
"""Coordinate one PR through preparation, review and publication."""
import os
import sqlite3
from .common import LOG, Attention, Stale, WatchError, repo_name, sha, utcnow, write_json
from .state import Store
from .gitcode import GitCodeClient
from .workspace import GitWorkspace
from .reviewer import CodexReviewer, Reviewer
from .publisher import Publisher
from .scheduler import Scheduler
from .retention import prune_runs

# 应用门面与单任务编排；默认组装组件，也允许测试或集成注入替代实现。
class Watcher:
    """Application facade; collaborators can be supplied independently in tests or integrations."""

    # 注入或组装各组件；动态转发客户端调用，保留兼容门面的替换能力。
    def __init__(self, config, state, tools, *, store=None, client=None,
                 workspace=None, reviewer: Reviewer = None, publisher=None):
        self.config, self.state, self.tools = config, state, tools
        self.on_result = None
        self.repo = repo_name(config['repo'])
        self.store = store if store is not None else Store(state/'state.sqlite3', self.repo)
        self.client = client if client is not None else GitCodeClient(tools.get('gitcode'), self.repo)
        self.workspace = workspace if workspace is not None else GitWorkspace(tools.get('git'), self.repo, config)
        self.reviewer = reviewer if reviewer is not None else CodexReviewer(tools.get('codex'), self.repo, config)
        self.publisher = publisher if publisher is not None else Publisher(self.repo, self.store, lambda *args: self.gc(*args))
        self.scheduler = Scheduler(config, self.store, lambda *args: self.gc(*args), self.process)
        self.cleanup_runs()

    def cleanup_runs(self):
        """清理失败仅记日志，不改变已完成任务的状态或触发重新发布。"""
        try:
            prune_runs(self.state, self.store, self.config.get('retain_pr_runs', 5))
        except (OSError, sqlite3.Error) as exc:
            LOG.warning('Retention failed (%s)', type(exc).__name__)

    def notify_result(self, result):
        """在结果持久化后通知界面；显示故障不得导致重新评审或重复发布。"""
        if self.on_result is not None:
            try:
                self.on_result(result)
            except Exception as exc:
                LOG.warning('Result notification failed (%s)', type(exc).__name__)

    # 委托 GitCode 客户端；集中保留调用入口以兼容已有集成。
    def gc(self, *args):
        return self.client(*args)

    # 生成固定提交对应的评审材料，返回共同祖先及各文件允许评论的新增行。
    def prepare(self, pr, job):
        return self.workspace.prepare(pr, job)

    # 返回评审报告与校验后的问题列表；不完整结果应交由人工处理。
    def review(self, pr, job, ancestor, locations):
        return self.reviewer.review(pr, job, ancestor, locations)

    # 发布前重新读取 PR，确认仍开放、非草稿且 head/base 未变化。
    def ensure_current(self, pr):
        return self.publisher.ensure_current(pr)

    # 按稳定标记去重；发送结果不确定时保留 pending，防止重试产生重复评论。
    def publish_one(self, pr, job, item, known):
        return self.publisher.publish_one(pr, job, item, known)

    # 完整获取开放 PR 列表再交给存储层；首次成功轮询只记录基线。
    def poll(self):
        return self.scheduler.poll()

    # TTY 下逐个确认；非交互环境保留待确认状态，等待外部批准命令。
    def confirm_pending(self):
        return self.scheduler.confirm_pending()

    # 支持单次、仅发现和持续模式；持续模式由主线程轮询，单工作线程评审。
    def serve(self, once=False, discover_only=False):
        return self.scheduler.serve(once, discover_only)

    # 推进一个 PR 并记录当前阶段；过期、人工介入和一般失败分别更新任务状态。
    def process(self, number):
        self.store.begin(number)
        LOG.info('Review starting: PR %d', number)
        stage = 'read PR metadata'
        try:
            pr = self.gc('pr','view',str(number))
            if pr.get('state') != 'open' or pr.get('merged'):
                self.store.finish(number, 'closed')
                return
            if pr.get('draft'):
                self.store.finish(number, 'queued', 'Waiting for ready PR', self.config['poll_seconds'])
                return
            head, base = sha(pr['head']['sha']), sha(pr['base']['sha'])
            job = self.state/'runs'/f'{number}-{head[:12]}-{base[:12]}'
            job.mkdir(parents=True, exist_ok=True)
            # 已有目录重试也更新时间，使保留顺序反映最近一次处理而非首次创建。
            os.utime(job, None)
            stage = 'prepare Git objects and diff'
            ancestor, locations = self.prepare(pr, job)
            stage = 'read existing comments'
            comments = self.gc('pr','comments',str(number)) or []
            write_json(job/'existing-comments.json', comments)
            stage = 'run reviewer'
            report, findings = self.review(pr, job, ancestor, locations)
            stage = 'verify and publish'
            self.ensure_current(pr)
            ids = []
            if self.config['publish']:
                for item in findings:
                    ids.append(self.publish_one(pr, job, item, comments))
            write_json(job/'summary.json', {'repository':self.repo,'number':number,'head':head,
                       'finished_at':utcnow(),'published':self.config['publish'],
                       'comment_ids':ids,'review':report})
            self.store.finish(number, 'done' if self.config['publish'] else 'reviewed_only')
            LOG.info('Review finished: PR %d; findings=%d; posted=%d', number, len(findings), len(ids))
            self.notify_result({'repository': self.repo, 'number': number,
                                'published': self.config['publish'], 'comment_ids': ids,
                                'findings': findings, 'summary': report.get('summary', '')})
        except Stale as exc:
            self.store.fail(number, str(exc), self.config['max_attempts'])
            LOG.info('PR %d changed; queued for retry', number)
        except Attention as exc:
            self.store.finish(number, 'needs_attention', str(exc))
            LOG.error('PR %d needs attention: %s', number, exc)
        except Exception as exc:
            # Use exception type only: arbitrary subprocess/JSON errors can contain source data.
            detail = str(exc) if isinstance(exc, WatchError) else type(exc).__name__
            detail = f'{stage}: {detail}'
            self.store.fail(number, detail, self.config['max_attempts'])
            LOG.error('PR %d failed: %s; retry policy applied', number, detail)
        finally:
            self.cleanup_runs()

