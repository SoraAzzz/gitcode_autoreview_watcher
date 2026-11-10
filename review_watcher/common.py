# 共享基础定义：异常类别、仓库与提交标识校验、UTC 时间和原子文件写入。
"""Shared errors, identifiers and atomic artifact writes."""
import datetime as dt
import json
import logging
import os
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("pr-watcher")

# 可预期的执行错误；流程层通常按次数上限安排重试。
class WatchError(Exception):
    pass


# PR 状态或提交已变化，当前评审不能继续发布。
class Stale(WatchError):
    pass


# 需要人工介入的结果；进入 needs_attention，不自动继续发布。
class Attention(WatchError):
    pass


# 生成带 UTC 时区的时间文本，供基线比较和诊断记录使用。
def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


# 先写同目录临时文件再原子替换，避免读到半写入的 JSON。
def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


# 限制为 owner/repo 格式，拒绝路径穿越片段及命令字符。
def repo_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', value):
        raise WatchError('Repository must be owner/repo')
    if any(x in ('.', '..') for x in value.split('/')):
        raise WatchError('Invalid repository name')
    return value


# 仅接受完整的 40 位小写提交 SHA，避免把任意输入当 Git 参数。
def sha(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value):
        raise WatchError('Missing or invalid commit SHA')
    return value

