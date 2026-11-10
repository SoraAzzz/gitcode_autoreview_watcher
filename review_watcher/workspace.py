# Git 工作区适配：只获取裸仓对象，计算 merge-base 和 diff，不检出或运行 PR 代码。
"""Prepare bare Git objects and pinned diffs without checking out PR code."""
import os
from .common import ROOT, Attention, repo_name, sha, write_json
from .commands import run
from .validation import changed_lines

# 裸仓和 diff 准备组件；runner 可替换以便离线测试 Git 调用。
class GitWorkspace:
    # 绑定 Git 可执行文件、目标仓库、超时限制及执行器。
    def __init__(self, executable, repo, config, runner=run):
        self.executable, self.repo, self.config, self.runner = executable, repo, config, runner

    # 禁用交互认证和仓库 hooks，执行限定裸仓目录中的 Git 命令。
    def git(self, source, *args):
        return self.runner([self.executable, '-c', 'core.hooksPath='+str(ROOT/'empty-hooks'),
                    '-c', 'core.quotePath=false', '--git-dir='+str(source), *args], ROOT,
                   timeout=self.config['git_timeout_seconds'], env={**os.environ, 'GIT_TERMINAL_PROMPT':'0'})


    # 生成固定提交对应的评审材料，返回共同祖先及各文件允许评论的新增行。
    def prepare(self, pr, job):
        source = job/'source.git'
        head, base = sha(pr['head']['sha']), sha(pr['base']['sha'])
        head_repo = repo_name(pr['head']['repo']['full_name'])
        if not source.exists():
            self.runner([self.executable, 'init', '--bare', str(source)], ROOT)
        # Full commit ancestry allows a real merge-base; no untrusted files are checked out.
        self.git(source, 'fetch', '--no-tags', '--no-recurse-submodules',
                 'https://gitcode.com/'+self.repo+'.git', base)
        self.git(source, 'fetch', '--no-tags', '--no-recurse-submodules',
                 'https://gitcode.com/'+head_repo+'.git', head)
        ancestor = sha(self.git(source, 'merge-base', base, head).strip())
        patch = self.git(source, 'diff', '--no-ext-diff', '--no-textconv', ancestor, head)
        if len(patch.encode('utf-8')) > self.config['max_diff_bytes']:
            raise Attention('PR diff exceeds configured limit; manual review needed')
        (job/'changes.diff').write_text(patch, encoding='utf-8')
        names = self.git(source, 'diff', '--name-only', '-z', ancestor, head).split('\0')
        locations = {}
        for name in filter(None, names):
            one = self.git(source, 'diff', '--no-ext-diff', '--no-textconv', '--unified=0',
                           ancestor, head, '--', name)
            locations[name] = changed_lines(one)
        write_json(job/'pr.json', pr)
        return ancestor, locations

