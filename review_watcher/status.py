"""Read-only status views shared by the CLI and interactive console."""
from collections import Counter


HISTORY_STATUSES = {'baseline', 'done', 'declined', 'reviewed_only'}
PRIORITY = {'needs_attention': 0, 'running': 1, 'awaiting_confirmation': 2, 'queued': 3}
LABELS = {
    'needs_attention': '需处理', 'running': '执行中',
    'awaiting_confirmation': '待确认', 'queued': '排队中',
    'done': '已完成', 'reviewed_only': '仅评审', 'declined': '已拒绝',
    'baseline': '基线跳过', 'closed': '已关闭',
}
DEFAULT_HISTORY_LIMIT = 20


def read_status(db, view='todo', limit=DEFAULT_HISTORY_LIMIT):
    """Query one snapshot without creating tables or recovering running jobs."""
    if view not in ('todo', 'all', 'history'):
        raise ValueError('Unknown status view')
    if type(limit) is not int or limit <= 0:
        raise ValueError('Status limit must be a positive integer')
    has_visibility = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pr_visibility'"
    ).fetchone()
    visible = "status!='closed'"
    if has_visibility:
        visible += ' AND number NOT IN (SELECT number FROM pr_visibility WHERE is_open=0)'
    counts, rows = Counter(), []
    for number, status, attempts, error, is_open in db.execute(
            f'SELECT number,status,attempts,error,({visible}) FROM jobs'):
        counts[status if is_open else 'closed'] += 1
        historical = not is_open or status in HISTORY_STATUSES
        if ((view == 'todo' and not historical) or (view == 'all' and is_open)
                or (view == 'history' and historical)):
            rows.append(dict(number=number, status=status, attempts=attempts, error=error))
    if view == 'history':
        # Jobs have no update timestamp; PR number is an explicit, stable fallback.
        rows.sort(key=lambda row: row['number'], reverse=True)
    else:
        rows.sort(key=lambda row: (PRIORITY.get(row['status'], 4), row['number']))
    total = len(rows)
    return {'view': view, 'counts': dict(counts), 'total': total,
            'jobs': rows[:limit] if view == 'history' else rows}


def status_overview(snapshot):
    counts = snapshot['counts']
    summary = ' · '.join(f"{label} {counts.get(status, 0)}" for status, label in LABELS.items())
    unknown = sorted(counts.keys() - LABELS.keys())
    if unknown:
        summary += ' · ' + ' · '.join(f'{status} {counts[status]}' for status in unknown)
    shown, total = len(snapshot['jobs']), snapshot['total']
    view = {'todo': '待办', 'all': '开放 PR', 'history': '历史'}[snapshot['view']]
    detail = f'{view}：显示 {shown}/{total} 条'
    if snapshot['view'] == 'history':
        detail += '（按 PR 编号倒序）'
    return summary + '\n' + detail
