# GitCode 传输适配：统一 CLI 非交互参数和 JSON 解码，不自行管理凭证。
"""GitCode CLI JSON transport."""
import json
from .common import ROOT
from .commands import run

# 可调用的 JSON 客户端；runner 可注入，认证仍由 GitCode CLI 处理。
class GitCodeClient:
    # 绑定可执行文件、目标仓库和可替换的命令执行器。
    def __init__(self, executable, repo, runner=run):
        self.executable, self.repo, self.runner = executable, repo, runner

    # 执行仓库范围内的非交互 CLI 命令，返回解析后的 JSON 数据。
    def __call__(self, *args):
        output = self.runner([self.executable, '--no-interactive', '--no-update-check',
                              *args, '-R', self.repo, '--json'], ROOT)
        return json.loads(output)

