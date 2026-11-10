# 外部进程适配层：查找工具、执行命令、处理超时，并保存脱敏诊断。
"""External process execution, executable discovery and redacted diagnostics."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from .common import LOG, WatchError, utcnow, write_json

# 优先使用配置路径，其次搜索 PATH；Windows 补查常见安装位置。
def resolve_tool(name, configured=''):
    if configured:
        result = shutil.which(configured)
    else:
        result = shutil.which(name)
        if not result and os.name == 'nt' and name == 'gitcode':
            candidate = Path(os.environ.get('LOCALAPPDATA', '')) / 'gitcode-cli/bin/gitcode.exe'
            result = str(candidate) if candidate.is_file() else None
        if not result and os.name == 'nt' and name == 'codex':
            candidates = sorted((Path.home()/'.vscode/extensions').glob(
                'openai.chatgpt-*/bin/windows-x86_64/codex.exe'), key=lambda p: p.stat().st_mtime)
            result = str(candidates[-1]) if candidates else None
    if not result:
        raise WatchError(f'{name} not found; set its absolute path in config.json')
    return result


# 遮盖已知环境凭证、认证头和 URL 用户信息；不保证识别所有敏感内容。
def redact(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    text = value or ''
    for key, secret in os.environ.items():
        if any(word in key.upper() for word in ('TOKEN', 'SECRET', 'PASSWORD', 'API_KEY')) and secret:
            text = text.replace(secret, '[REDACTED]')
    text = re.sub(r'(?i)(authorization[\"\s:]+(?:bearer|basic)\s+)[^\s\"]+',
                  r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1[REDACTED]@', text)
    return text


# 每次命令保留脱敏输出和执行元数据；未指定目录时不落盘。
def save_diagnostics(directory, outcome, stdout, stderr):
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'stdout.log').write_text(redact(stdout), encoding='utf-8')
    (directory/'stderr.log').write_text(redact(stderr), encoding='utf-8')
    write_json(directory/'execution.json', outcome)


# 同步执行参数列表，捕获输出；超时保留部分输出，失败转换为 WatchError。
def run(args, cwd, timeout=180, stdin=None, env=None, diagnostics=None):
    # Log metadata only; arguments, prompts and environment values are not logged.
    tool = Path(args[0]).name
    started = time.monotonic()
    LOG.info('Command starting: tool=%s timeout=%ss diagnostics=%s', tool, timeout, diagnostics or '-')
    outcome = {'tool': tool, 'started_at': utcnow(), 'timeout_seconds': timeout}
    try:
        result = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True,
                                encoding='utf-8', errors='replace', timeout=timeout, env=env)
    except subprocess.TimeoutExpired as exc:
        outcome.update(status='timeout', elapsed_seconds=round(time.monotonic()-started, 3))
        save_diagnostics(diagnostics, outcome, exc.stdout, exc.stderr)
        LOG.error('Command timeout: tool=%s elapsed=%ss', tool, outcome['elapsed_seconds'])
        raise WatchError(f'{tool} timed out after {timeout}s; diagnostics={diagnostics}') from exc
    except OSError as exc:
        outcome.update(status='launch_failed', errno=exc.errno,
                       elapsed_seconds=round(time.monotonic()-started, 3))
        save_diagnostics(diagnostics, outcome, '', '')
        raise WatchError(f'{tool} could not start (errno={exc.errno})') from exc
    outcome.update(status='success' if result.returncode == 0 else 'failed',
                   exit_code=result.returncode, elapsed_seconds=round(time.monotonic()-started, 3))
    save_diagnostics(diagnostics, outcome, result.stdout, result.stderr)
    LOG.info('Command finished: tool=%s exit=%s elapsed=%ss',
             tool, result.returncode, outcome['elapsed_seconds'])
    if result.returncode:
        detail = redact(result.stderr)[-2000:].replace('\r', '').replace('\n', ' | ')
        raise WatchError(f'{tool} failed (exit {result.returncode}): {detail}')
    return result.stdout

