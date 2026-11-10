# 评审后端边界：定义 Reviewer 接口，并实现 Codex 提示词、命令和缓存处理。
"""Reviewer backend contract and Codex CLI implementation; no publication authority."""
import json
import os
import time
from typing import Protocol
from .common import ROOT, LOG, sha
from .commands import run, redact
from .validation import validate_report

# 后端协议；实现必须返回固定 PR 的报告及经过校验的问题列表。
class Reviewer(Protocol):
    # 返回评审报告与校验后的问题列表；不完整结果应交由人工处理。
    def review(self, pr, job, ancestor, locations):
        """Return a report and validated findings for the pinned PR."""
        ...


# 构造只读 Codex exec 参数，按配置添加模型并指定结构化输出文件。
def codex_review_args(codex, config, job, output):
    args = [codex, 'exec', '--ignore-user-config', '--ephemeral',
            '--sandbox', 'read-only', '-c', 'approval_policy="never"',
            '-c', 'web_search="disabled"',
            '-c', 'model_reasoning_effort='+json.dumps(config.get('reasoning_effort', 'low')),
            '--skip-git-repo-check', '--cd', str(job),
            '--output-schema', str(ROOT/'review-schema.json'),
            '--output-last-message', str(output), '--color', 'never', '-']
    if config.get('model'):
        args[2:2] = ['--model', config['model']]
    # ignore-user-config 也会跳过 Windows 沙箱设置；显式启用原生沙箱，
    # 保持 read-only/never，不依赖用户配置或降级为无沙箱执行。
    if os.name == 'nt':
        args[2:2] = ['-c', 'windows.sandbox="elevated"']
    return args


# Codex CLI 后端；复用已有 review.json，只有无缓存时才启动模型进程。
class CodexReviewer:
    # 绑定模型配置、目标仓库和执行器；凭证仍交给 CLI 管理。
    def __init__(self, executable, repo, config, runner=run):
        self.executable, self.repo, self.config, self.runner = executable, repo, config, runner

    # 返回评审报告与校验后的问题列表；不完整结果应交由人工处理。
    def review(self, pr, job, ancestor, locations):
        output = job/'review.json'
        head = sha(pr['head']['sha'])
        if not output.exists():
            prompt = (ROOT/'review-prompt.md').read_text(encoding='utf-8')
            prompt += '\nTrusted task coordinates:\n'+json.dumps({
                'repository':self.repo, 'pr_number':pr['number'], 'head_sha':head,
                'merge_base':ancestor, 'max_findings':self.config['max_findings']})
            (job/'prompt.txt').write_text(prompt, encoding='utf-8')
            args = codex_review_args(self.executable, self.config, job, output)
            # Authentication remains managed by the CLIs. Do not pass GitCode token env vars to the reviewer.
            env = {k:v for k,v in os.environ.items() if k.upper() not in ('GC_TOKEN','GITCODE_TOKEN')}
            diagnostics = job/'diagnostics'/str(time.time_ns())
            self.runner(args, job, timeout=self.config['review_timeout_seconds'], stdin=prompt, env=env,
                diagnostics=diagnostics)
        else:
            LOG.info('Using cached review report: %s (no new model call)', output)
        report = json.loads(output.read_text(encoding='utf-8-sig'))
        if report.get('status') == 'incomplete':
            LOG.warning('Reviewer incomplete: report=%s limitations=%s',
                        output, redact(str(report.get('limitations', '')))[:2000])
        findings = validate_report(report, head, locations, self.config['max_findings'])
        return report, findings

