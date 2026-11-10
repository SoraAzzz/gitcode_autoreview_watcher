# 交互评审结果通知验证

开放 PR 名单更新：88 项离线测试通过，覆盖关闭/合入隐藏、重新打开再次确认、
去重记录保留、失败轮询保留名单、旧数据库兼容及运行中关闭后不再入队。
未对真实 PR 执行关闭、重新打开或评论发布操作。

PR 描述展示更新：79 项离线测试通过，覆盖待确认/状态展示、轮询信息复用、详情缓存、
读取失败重试、空描述、草稿及长描述截断；未调用真实模型或发布远端评论。

- 75 项离线测试通过；覆盖完成事件传递、问题内容与代码行展示、无问题/仅评审状态。
- 通知回调异常时，已发布任务仍保持 done，不触发重新发布。
- 此次未调用真实模型、未发布远端评论、未启动监听或打包。

# Windows 沙箱参数修正验证（2026-09-15）

- 已确认 watcher 的 `--ignore-user-config` 会跳过个人配置，而本机个人配置选择了 elevated Windows 沙箱。
- Windows 启动参数现显式指定 `windows.sandbox="elevated"`；仍使用 read-only、approval_policy=never，不忽略规则或关闭沙箱。
- 64 项离线测试通过，新增覆盖 Windows 与非 Windows 参数选择及原有权限边界保留。
- 第一次真实只读文件探测超时；第二次标准 Codex 探测在连接重试及客户端自动切换 HTTPS 后返回 `You've hit your usage limit`，退出码 1。
- 真实探测未执行到文件读取，尚不能确认原先的命令策略拒绝已消除。需用量恢复后再次验证，不把本次参数修正视为端到端验证通过。
- 未重启监听器、修改 PR 状态、清理评审缓存或发布远端评论；未生成 dist。

# 持续交互终端验证（2026-09-15）

- 63 项离线测试通过，包括原有 51 项回归与 12 项交互测试。
- 覆盖连续命令、无效输入、批准/拒绝、重试保留发送意图、去重提示、EOF 退出、退出等待线程、后台故障以及轮询期间停止后不启动任务。
- 覆盖 TTY 默认进入控制台、显式交互拒绝非 TTY、无交互模式不读取确认输入。
- CLI help 和 PowerShell 参数解析验证通过；未调用真实模型或发布远端评论。
- 未启动用户的正式监听进程，未生成 dist 或执行打包。

# 0.4.0 验证记录（2026-09-15）

- 原有 46 项回归测试，加 5 项组件协作测试，全部通过。
- 覆盖自定义后端注入、仅发现模式不调用工作线程、单次模式调度、Codex 参数与提示词传递、缓存复用和 incomplete 拦截。
- `watcher.py --help` 和打包解压后的同一入口通过。
- 打包包含全部 `review_watcher/*.py`、测试和架构文档，未包含运行数据与 Python 缓存。
- 此次使用本地离线测试，无真实模型调用或远端评论发布；未重启用户监听进程。


# 验证记录

日期：2026-09-14

- Python 3.14 / Windows PowerShell：语法检查通过。
- `python -m unittest -q tests.test_watcher`：40 tests，全部通过。新增覆盖前台确认、非 TTY 等待、批准/拒绝状态、监听器运行中的外部批准以及模型参数透传。
- `python watcher.py --check`：GitCode 与 Codex 已登录，Git 可用。
- `python watcher.py --config smoke-config.json --discover-only`：只读访问 infra-test/gctest1 返回 CLI 退出码 4；未建立成功基线，后续 status 为 []，符合失败不推进状态预期。测试账号权限需由使用者另行处理。
- Codex CLI 0.153.4 实际非交互冒烟：read-only、ignore-user-config、ephemeral、output-schema、output-last-message；标准客户端经历 5 次 WebSocket 超时重试后自动改用 HTTPS，退出码 0，输出 {"ok":true}。没有手动更换连接方式或提取凭证。
- PowerShell 默认 UTF-8 BOM 导致 schema 初次加载失败；交付 schema.json 已改为无 BOM UTF-8，并验证成功。
- 模型输出/发布异常、过期提交、重复发送保护使用本地测试桩验证；未执行真实新 PR → AI 完整审查 → 新增远端评论的端到端流程。
- `package.ps1` 实际打包通过，ZIP 内容清单与 SHA-256 校验文件已核对；包内不包含 runtime、日志、Git 对象、模型结果或认证资料。
- 未启动后台服务，未安装计划任务，未向 Ascend/msprof 发布测试消息。
- 本工具为独立交付脚本，未修改 gitcode-cli 的产品源码、命令文档或主分支。
