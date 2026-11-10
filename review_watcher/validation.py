# 结果校验层：仅接受固定提交 diff 新增行上的有效 P1/P2 问题。
"""Validate pinned review findings before any publication."""
from pathlib import PurePosixPath
import re
from .common import WatchError, Attention

# 从单文件 unified diff 提取新侧实际新增行，不把上下文或删除行当评论位置。
def changed_lines(patch):
    """Return actual added new-side line numbers from a single-file unified diff."""
    result, current, active = set(), 0, False
    for line in patch.splitlines():
        match = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
        if match:
            current, active = int(match[1]), True
        elif active and line.startswith('+'):
            result.add(current)
            current += 1
        elif active and line.startswith(' '):
            current += 1
    return result


# 校验提交、完成状态、数量、路径、行号及敏感标志；不合格结果禁止发布。
def validate_report(report, head, locations, max_findings):
    if report.get('head_sha') != head or report.get('status') not in ('complete', 'incomplete'):
        raise WatchError('Invalid review result or mismatched head SHA')
    if report['status'] != 'complete':
        raise Attention('Reviewer could not finish; inspect review.json')
    findings = report.get('findings')
    if not isinstance(findings, list) or len(findings) > max_findings:
        raise Attention('Invalid finding count; inspect review.json')
    for item in findings:
        path, line = item.get('path'), item.get('line')
        if not isinstance(path, str) or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts:
            raise Attention('Invalid finding path')
        if type(line) is not int or line not in locations.get(path, set()):
            raise Attention('Finding must point to an added line of the pinned diff')
        if item.get('priority') not in ('P1', 'P2'):
            raise Attention('Only actionable P1/P2 findings can be auto-posted')
        if item.get('security_sensitive') is not False:
            raise Attention('Sensitive finding held for private human review')
        for key in ('title', 'body', 'evidence'):
            if not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 6000:
                raise Attention('Missing or oversized finding text')
    return findings

