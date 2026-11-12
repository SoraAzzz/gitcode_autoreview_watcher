# 命令行入口：解析参数、处理批准等控制命令，初始化日志后启动调度。
"""CLI arguments, control commands, logging and application composition."""
import argparse
import contextlib
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sqlite3
import sys
from .common import ROOT, LOG, WatchError
from .commands import resolve_tool, run
from .config import load_config
from .state import Store, instance_lock
from .status import DEFAULT_HISTORY_LIMIT, read_status, status_overview
from .pipeline import Watcher
from .console import Console

# 控制命令可直接退出；正常监听在获得实例锁后才创建应用。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'config.json')
    parser.add_argument('--once', action='store_true', help='Poll and drain currently eligible jobs, then exit')
    parser.add_argument('--discover-only', action='store_true', help='Poll only; never run AI or publish')
    parser.add_argument('--status', action='store_true', help='Print actionable persisted jobs without polling')
    status_view = parser.add_mutually_exclusive_group()
    status_view.add_argument('--all', action='store_true', help='With --status: show all open PR jobs')
    status_view.add_argument('--history', action='store_true', help='With --status: show historical jobs')
    parser.add_argument('--limit', type=int, help='With --status --history: maximum rows (default: 20)')
    parser.add_argument('--retry-pr', type=int, help='Requeue a held job; preserve publication deduplication')
    parser.add_argument('--approve-pr', type=int, help='Approve one awaiting/declined/baseline PR and exit')
    parser.add_argument('--decline-pr', type=int, help='Decline one awaiting PR and exit')
    parser.add_argument('--repo', help='Select a repository from repo_allowlist')
    parser.add_argument('--model', help='Override config.json model for this process')
    parser.add_argument('--check', action='store_true', help='Check tools and authentication without polling')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--interactive', action='store_true', help='Keep accepting commands in a terminal')
    mode.add_argument('--no-interactive', action='store_true', help='Run continuous polling without a command console')
    args = parser.parse_args()
    if (args.all or args.history or args.limit is not None) and not args.status:
        parser.error('--all, --history and --limit require --status')
    if args.limit is not None and (not args.history or args.limit <= 0):
        parser.error('--limit requires --history and a positive integer')
    control = (args.once or args.discover_only or args.status or args.check or
               any(value is not None for value in (args.retry_pr, args.approve_pr, args.decline_pr)))
    if args.interactive and control:
        parser.error('--interactive cannot be combined with a one-shot command')
    if args.interactive and not sys.stdin.isatty():
        parser.error('--interactive requires a terminal')
    interactive = not control and not args.no_interactive and sys.stdin.isatty()
    config, state = load_config(args)
    if args.status:
        view = 'all' if args.all else 'history' if args.history else 'todo'
        database = state/'state.sqlite3'
        snapshot = {'view': view, 'counts': {}, 'total': 0, 'jobs': []}
        if database.exists():
            with contextlib.closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)) as db:
                snapshot = read_status(db, view, args.limit or DEFAULT_HISTORY_LIMIT)
        print(status_overview(snapshot), file=sys.stderr)
        print(json.dumps(snapshot['jobs'], indent=2))
        return
    if args.approve_pr is not None:
        Store(state/'state.sqlite3', config['repo'], recover_running=False).decide(args.approve_pr, True)
        print(f'PR {args.approve_pr} approved and queued')
        return
    if args.decline_pr is not None:
        Store(state/'state.sqlite3', config['repo'], recover_running=False).decide(args.decline_pr, False)
        print(f'PR {args.decline_pr} declined')
        return
    # 交互时详细日志只写文件，防止子进程日志淹没命令提示符。
    handlers = [RotatingFileHandler(state/'watcher.log', maxBytes=5*1024*1024, backupCount=3, encoding='utf-8')]
    if not interactive:
        handlers.insert(0, logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=handlers)
    with instance_lock(state/'watcher.lock'):
        if args.retry_pr is not None:
            Store(state/'state.sqlite3', config['repo']).retry(args.retry_pr)
            LOG.info('PR %d requeued; pending publication markers preserved', args.retry_pr)
            return
        tools = {name:resolve_tool(name, config.get(name+'_path','')) for name in ('gitcode','git','codex')}
        if args.check:
            for name, command in [('gitcode',['auth','status']),('codex',['login','status'])]:
                run([tools[name],*command], ROOT)
                LOG.info('%s authentication OK', name)
            run([tools['git'],'--version'], ROOT)
            return
        watcher = Watcher(config,state,tools)
        if interactive:
            Console(watcher).run()
        elif args.no_interactive:
            watcher.scheduler.serve(args.once, args.discover_only,
                                    on_pending=lambda numbers: LOG.info('Awaiting confirmation: %s', numbers))
        else:
            watcher.serve(args.once,args.discover_only)


# 统一处理启动异常并返回非零退出码，避免直接暴露未知异常内容。
def entrypoint():
    try:
        main()
    except (WatchError, OSError, ValueError, KeyError) as exc:
        LOG.error('Watcher stopped (%s): %s', type(exc).__name__,
                  str(exc) if isinstance(exc, WatchError) else 'check configuration/tools')
        raise SystemExit(1)

