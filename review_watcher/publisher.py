# 发布边界：确认 PR 仍有效，记录发送意图，发布并核验行内评论，防止重复发送。
"""Publish validated inline comments with durable deduplication and freshness checks."""
import hashlib
import json
from .common import Attention, Stale

# 受信任的发布组件；只通过传入客户端和状态仓库执行评论操作。
class Publisher:
    # 共享任务存储和仓库客户端，确保发布意图与结果使用同一份持久化记录。
    def __init__(self, repo, store, client):
        self.repo, self.store, self.gc = repo, store, client

    # 发布前重新读取 PR，确认仍开放、非草稿且 head/base 未变化。
    def ensure_current(self, original):
        current = self.gc('pr', 'view', str(original['number']))
        if current.get('state') != 'open' or current.get('merged') or current.get('draft'):
            raise Stale('PR closed, merged or returned to draft')
        if (current['head']['sha'], current['base']['sha']) != (original['head']['sha'], original['base']['sha']):
            raise Stale('PR head/base changed; review will restart')


    # 按稳定标记去重；发送结果不确定时保留 pending，防止重试产生重复评论。
    def publish_one(self, pr, job, item, known):
        payload = json.dumps([self.repo, pr['number'], pr['head']['sha'], item], sort_keys=True)
        marker = 'codereview-watcher:'+hashlib.sha256(payload.encode()).hexdigest()[:32]
        matching = next((c for c in known if marker in c.get('body','')), None)
        if matching:
            if matching.get('comment_type') != 'diff_comment':
                raise Attention('Existing marker is not an inline comment')
            self.store.post(marker, pr['number'], 'sent', matching.get('id',''))
            return matching.get('id')
        previous = self.store.post_state(marker)
        if previous == 'sent': return 'previously-verified'
        if previous == 'pending':
            raise Attention('Uncertain previous publish; inspect remote comments before any manual retry')
        self.ensure_current(pr)
        body = (f"[codereview] [{item['priority']}] {item['title']}\n\n{item['body']}\n\n"
                f"依据：{item['evidence']}"
                f"\n\n<!-- {marker} -->")
        bodyfile = job/(marker.split(':')[1]+'.md')
        bodyfile.write_text(body, encoding='utf-8')
        self.store.post(marker, pr['number'], 'pending')  # Durable intent BEFORE remote side effect.
        try:
            self.gc('pr','comment',str(pr['number']),'--path',item['path'],
                    '--position',str(item['line']),'--body-file',str(bodyfile))
            comments = self.gc('pr','comments',str(pr['number'])) or []
            posted = next((c for c in comments if marker in c.get('body','')), None)
            position = posted.get('diff_position') if posted else None
            line = position.get('start_new_line') if isinstance(position, dict) else position
            if not posted or posted.get('comment_type') != 'diff_comment' or line != item['line']:
                raise Attention('Posted comment could not be verified as the expected inline comment')
        except Exception as exc:
            raise Attention('Publish outcome uncertain; pending marker retained to prevent duplicates') from exc
        self.store.post(marker, pr['number'], 'sent', posted['id'])
        known.append(posted)
        return posted['id']

