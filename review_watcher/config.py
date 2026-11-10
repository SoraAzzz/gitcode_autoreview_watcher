# 配置加载：校验白名单与参数，按仓库隔离状态，并兼容旧状态目录。
"""Load and validate configuration; select isolated or legacy repository state."""
import contextlib
import hashlib
import json
import sqlite3
from .common import WatchError, repo_name

# 校验整个白名单，并要求选中的仓库精确匹配其中一项。
def select_repository(config, selected=None):
    allowed = config.get('repo_allowlist')
    if not isinstance(allowed, list) or not allowed:
        raise WatchError('repo_allowlist must be a non-empty list of owner/repo names')
    allowed = [repo_name(item) for item in allowed]
    if len(set(allowed)) != len(allowed):
        raise WatchError('repo_allowlist contains duplicate repositories')
    chosen = repo_name(selected if selected is not None else config['repo'])
    if chosen not in allowed:
        raise WatchError('Selected repository is not in repo_allowlist')
    return chosen


# 返回配置与绝对状态路径；旧数据库仅在记录的仓库一致时复用。
def load_config(args):
    config = json.loads(args.config.read_text(encoding='utf-8-sig'))
    config.setdefault('require_confirmation', True)
    config.setdefault('reasoning_effort', 'low')
    if config['reasoning_effort'] not in ('low', 'medium', 'high', 'xhigh'):
        raise WatchError('reasoning_effort must be low, medium, high or xhigh')
    config.setdefault('retain_pr_runs', 5)
    if type(config['retain_pr_runs']) is not int or config['retain_pr_runs'] <= 0:
        raise WatchError('retain_pr_runs must be a positive integer')
    if args.model is not None:
        config['model'] = args.model
    config['repo'] = select_repository(config, args.repo)
    for key in ('poll_seconds','git_timeout_seconds','review_timeout_seconds','max_attempts','max_findings','max_diff_bytes'):
        if type(config[key]) is not int or config[key] <= 0: raise WatchError(f'{key} must be positive')
    if type(config['publish']) is not bool: raise WatchError('publish must be a boolean')
    if type(config['require_confirmation']) is not bool: raise WatchError('require_confirmation must be a boolean')
    if not isinstance(config.get('model'), str): raise WatchError('model must be a string')
    state = (args.config.resolve().parent/config['state_dir']).resolve()
    # Preserve an existing single-repository database only for its recorded repository.
    legacy = state/'state.sqlite3'
    use_legacy = False
    if legacy.exists():
        with contextlib.closing(sqlite3.connect(legacy.as_uri()+'?mode=ro', uri=True)) as db:
            row = db.execute("SELECT value FROM meta WHERE key='repo'").fetchone()
            use_legacy = bool(row and row[0] == config['repo'])
    if not use_legacy:
        state = state/'repositories'/hashlib.sha256(config['repo'].encode()).hexdigest()
    state.mkdir(parents=True, exist_ok=True)
    return config, state

