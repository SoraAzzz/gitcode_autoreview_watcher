# GitCode 新 PR 自动评审脚本

独立工具，不修改 gitcode-cli 产品命令。Python 3.10+，仅使用标准库。

默认监听 `Ascend/msprof`，每 300 秒分页查询一次打开的 PR；首次成功查询只保存存量基线，后续发现新创建且仍打开的 PR 先进入 `awaiting_confirmation`。前台运行时由主进程显示 PR 信息并询问用户，只有明确输入 `y`/`yes` 才会进入评审队列。后台运行时不隐式调用模型，等待用户通过控制命令批准或拒绝。草稿也先确认，批准后等到就绪再评审。PR 在轮询之间创建又关闭不会评审；首次基线之前创建的旧 PR 后来重新打开也不会评审。已经完成评审的 PR 后续推送不自动触发；正在评审的 PR head/base 变化会重新排队。

## 开始使用

将整个目录放在固定位置，例如 `D:\tools\gitcode-pr-review-watcher`。不要长期从临时下载目录运行。复制时保留整个 `runtime` 目录，避免丢失去重状态。

```powershell
cd D:\tools\gitcode-pr-review-watcher
# 首次使用前由本人完成登录；脚本不读取或输出 Token。
gitcode auth login
codex login

# 检查工具与登录，不会调用模型、发现 PR 或发布评论
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Check

# 前台持续运行。第一次成功轮询只记录已有 PR；之后逐条询问新 PR。
powershell -ExecutionPolicy Bypass -File .\start.ps1

# 本次运行选择评审模型；省略时使用 config.json 或 Codex 默认模型。
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Model gpt-5.3-codex
```

按 Ctrl+C 停止接收任务，等待当前评审完成后退出（最长受 review_timeout_seconds 限制）。电脑休眠/关机时不工作。重启脚本会继续读取状态，不会重评已完成的 PR。

没有 codex 命令时，可在 config.json 的 `codex_path` 填已安装 codex.exe 的绝对路径；Windows 自动尝试 VS Code 扩展附带的 CLI。`gitcode_path`、`git_path` 同理。start.ps1 会刷新用户和系统 PATH。脚本使用已保存的 CLI 登录，不需要把 Token 或密码写进配置。

## Windows 后台运行与开机启动

建议用“任务计划程序”创建任务：在当前用户登录时触发，使用当前用户运行；“起始于”设为脚本固定目录。隐藏任务没有交互终端，新 PR 会保持 `awaiting_confirmation`，不会调用 Codex。

- 程序：`powershell.exe`
- 参数：`-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "D:\tools\gitcode-pr-review-watcher\start.ps1"`
- 任务已运行时：不启动新实例。
- 允许按需运行；按使用习惯配置“失败后重新启动”，取消默认的长运行自动停止限制。

脚本内部已经每 5 分钟轮询，无需每 5 分钟再启动一个常驻进程。本次交付没有安装计划任务或启动常驻后台服务。

监听器运行时可以从另一个终端查看并处理等待项：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 -Status
powershell -ExecutionPolicy Bypass -File .\start.ps1 -ApprovePr 508
powershell -ExecutionPolicy Bypass -File .\start.ps1 -DeclinePr 508
```

批准只把该 PR 放入队列，实际 Agent 仍由唯一的监听主进程启动和监管。

## 配置

config.json 默认：

- repo：`Ascend/msprof`，更换仓库请使用新的 state_dir。
- poll_seconds：300；轮询和单个评审工作线程并行。
- publish：true；false 时只保存 review.json，不发表评论。重新设为 true 后，之前 reviewed_only 的任务需显式 retry。
- require_confirmation：true；新 PR 默认等待用户确认。仅在调用方已有独立准入机制时才可设为 false。
- state_dir：runtime，相对于配置文件。存放 SQLite 状态、运行日志、Git 对象、评审正文、结果和发布记录。
- model：空字符串时使用 Codex CLI 默认模型。可填自己账号支持的模型，也可启动时用 `-Model`/`--model` 覆盖。
- reasoning_effort：推理强度，默认 `low`，工具接受 `low`、`medium`、`high`、`xhigh`；所选值需由当前模型支持。修改后重启 watcher，对后续新模型调用生效；已有评审缓存不会自动重跑。旧配置未填写此字段时也使用 `low`。
- review_timeout_seconds：1800；git_timeout_seconds：600。
- max_attempts：3，失败按 5/10/20 分钟退避，超过次数转 needs_attention；PR 持续更新也受重试上限约束。
- max_findings：5，最多自动发布五条有证据的 P1/P2 问题。
- max_diff_bytes：1000000，超过则转人工检查，不静默截断差异。

## 工作流程

1. `gitcode pr list --state open --paginate --per-page 100 --json` 查询完整 PR 列表；失败不推进基线。
2. 新 PR 写入 `awaiting_confirmation`。前台由主进程询问；后台等待 `--approve-pr` 或 `--decline-pr`，确认前不调用 Agent。
3. 批准后由主进程调用 `gitcode pr view` 固定 head/base SHA，读取源仓库信息；从 GitCode 拉取 Git 对象并计算 merge-base。
4. 仓库以 bare 形式保存，不检出 PR 文件，避免 PR 中的配置/AGENTS 自动成为评审进程指令。
5. `codex exec --model <model> --sandbox read-only --ignore-user-config --output-schema ...` 输出结构化报告。读取完整差异和相关上下文，结合已有评论去重；只静态审查，不执行仓库代码/测试，不安装依赖，不访问外部服务。
6. 脚本校验报告完整性、SHA、路径以及新增侧代码行；不完整、敏感结果或位置无效时保留本地结果，转 needs_attention。
7. 每条发布前重新核对 head/base，使用 UTF-8 正文文件调用 `gitcode pr comment --path ... --position ... --body-file ... --json`。评论统一以 `[codereview]` 开头，正文保留问题、影响和依据，不再追加评审提交与静态检查范围的固定尾注；提交信息和评审限制仍保存在本地报告中。
8. 回读验证 `diff_comment` 和新侧行号。无问题时只保留本地总结，不发送“无问题”评论，也不批准或合并 PR。

发布标记及“准备发送”状态先持久化。进程中断/请求超时时，不盲目重发：先用远端隐藏标记核对；结果不确定则暂停该 PR，防止重复评论。远端发布和本地记录不是一个事务，不宣称绝对 exactly-once。PR 在“检查版本”和“服务端处理评论”之间仍有很短的竞态窗口，评论正文中的完整 SHA 用于追溯。

本工具是自动静态评审助手，不等同于人工审查、Ascend 端到端验证或目标仓库的正式多角色批准流程。

## 检视意见要求

评审细则集中在 [review-prompt.md](review-prompt.md)。每条意见用简洁中文指出具体问题、触发条件、原因与实际影响，给出修复方案及最小代码样例；依据字段保留关键源码位置和调用关系，让作者理解为什么改以及如何改。

检查范围覆盖功能逻辑、结构设计、安全、性能及可靠性，仅报告本 PR 引入或加重且有代码证据的 P1/P2 缺陷。按根因与已有评论去重，排除纯规范复述、空泛咨询、模板评价和无实际影响的优化建议；不要求每个维度都产生意见，不设最低条数。超过 `max_findings` 时优先保留影响严重、证据充分的发现，同等条件下兼顾不同维度。

修改提示词后，后续新模型调用会使用新规则；已经生成的 `review.json` 仍按缓存复用，重试不会自动重新评审。程序升级后需重启监听器；重新评审已有任务的处理方式见下文“查看与恢复”。

## 查看与恢复

```powershell
# 运行中也可以查询
python watcher.py --status

# 监听器运行中批准或拒绝一个等待项
python watcher.py --approve-pr 123
python watcher.py --decline-pr 123

# 只轮询一次，不调用 Codex、不发评论；首次执行也会建立基线
python watcher.py --discover-only

# 轮询并处理当前可执行任务后退出，不等待延迟重试
python watcher.py --once

# 停止常驻进程后，重新排队一条 needs_attention 或 reviewed_only 任务
python watcher.py --retry-pr 123
```

`runtime/watcher.log` 只记任务状态和概括错误，不记录凭证或完整命令输出。`runtime/runs/<PR>-<head>-<base>/review.json` 保存模型报告，summary.json 保存最终结果及评论 ID。模型输出、源代码与评审材料可能含项目内部信息，整个 runtime 应作为本地工作资料保管，不上传或提交。

needs_attention 的处理：检查对应报告和远端评论。若是敏感问题，走私密报告流程；若是发布结果不确定，先核对隐藏标记是否已在远端存在，retry 会重新回读并恢复，不会无条件重发。若远端确认未收到但本地仍为 pending，需要人工处理该记录；脚本不提供“一键清除全部去重记录”。若报告本身无效，先修正原因，再移走对应的 review.json 后 retry。不要删除整个状态库来重试。

## 测试与本次验证

```powershell
python -m unittest -v test_watcher
```

测试覆盖首次基线、人工确认、非 TTY 等待、模型透传、旧 PR 重开、失败回滚、重启恢复、锁、重试上限、差异行定位、敏感报告拦截、过期提交拦截、发布前持久化、超时不重复发送、回读类型校验及只评审模式。

## 打包

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\package.ps1
```

产物写入 `dist/gitcode-pr-review-watcher-<version>.zip`，并生成对应 `.sha256` 文件。包内不包含 `runtime`、日志、Git 对象、模型结果或本地认证资料。

本机工具：GitCode CLI 认证正常，Codex CLI 0.153.4 认证正常。真实测试仓 infra-test/gctest1 轮询返回退出码 4（权限/认证类错误）；没有在生产仓发布测试评论。Codex 最小非交互调用的联网结果见 VALIDATION.md。默认 runtime 未建立生产仓基线，也未启动后台服务。

Codex 非交互调用依据：[官方文档](https://learn.chatgpt.com/docs/non-interactive-mode)。

## 仓库白名单

在 config.json 中配置允许操作的仓库（精确匹配，不支持通配符）：

```json
{
  "repo": "Ascend/msprof",
  "repo_allowlist": ["Ascend/msprof", "your-org/your-repo"]
}
```

这是配置片段，请保留原配置的其他字段。只将需要评审的仓库加入列表。

```powershell
python watcher.py --repo your-org/your-repo
python watcher.py --repo your-org/your-repo --status
python watcher.py --repo your-org/your-repo --approve-pr 123
# PowerShell 启动器等价参数
.\start.ps1 -Repo your-org/your-repo
```

未列入白名单、空白名单或缺少白名单，会在访问网络、启动 Agent 前报错。旧配置需补上 repo_allowlist。

每个进程监听一个选定仓库；多个仓库可启动多个进程。新状态按仓库隔离保存到 state_dir/repositories 下，已有旧版 state.sqlite3 仅供其记录的原仓库继续使用，不重建原仓库基线。

批准、拒绝、重试和查询也必须选择同一仓库。白名单不替代逐 PR 人工确认。配置在启动时读取，修改后需重启对应监听器；移除白名单不会删除已有状态或撤销远端评论。

## DFX 诊断日志（0.3.2）

各仓库状态目录中的 watcher.log 记录命令开始、结束、耗时、退出码，以及 PR 失败阶段。日志按 5 MiB 轮转，保留 3 个备份。命令参数、prompt 和环境变量不会作为元数据记录。

每次新 Codex 调用会在任务目录 diagnostics/调用编号/ 保存：

- execution.json：工具名称、开始时间、耗时、退出码或超时、启动失败状态。
- stdout.log：Codex 标准输出。
- stderr.log：Codex 执行过程输出，包括工具拒绝及沙箱错误；超时保留已捕获内容。

日志会替换已知凭证环境变量值、Authorization 认证头和 URL 中的用户名密码。这些文件仍可能包含源码、模型输出及其他未识别的敏感内容，仅供本地调试，分享前需检查。

当前在子进程结束或超时后保存诊断输出，不是实时流式日志。模型返回 incomplete 时，主日志会显示 limitations 和报告路径；复用缓存时会明确显示 no new model call。

旧的 PR 513 调用没有保存执行日志，升级不能补回。重新运行需要先停止监听器、归档对应 review.json，再执行 --retry-pr 513 并重启监听器；这会产生新的模型调用。

## 开发与架构

实现已拆分到 `review_watcher/`，`watcher.py` 保留兼容入口。
模块职责、调用关系及后端扩展方式见 [ARCHITECTURE.md](ARCHITECTURE.md)。
启动命令、配置和历史状态兼容；升级程序后需要重启监听进程。

## 持续交互终端

在 PowerShell 中运行 `./start.ps1`（或 `python watcher.py`），终端默认进入
`watcher>` 命令窗口。后台继续轮询及评审，前台可以持续输入命令，不需要重复启动进程。

```text
watcher> pending
watcher> approve 513
watcher> status
watcher> decline 514
watcher> retry 512
watcher> poll
watcher> help
watcher> quit
```

| 命令 | 含义 |
| --- | --- |
| `help` | 显示命令用法 |
| `status` | 显示全部任务状态、尝试次数和已记录错误 |
| `pending` | 显示等待确认的 PR 编号 |
| `approve <PR>` | 确认开始评审，将待确认或已拒绝任务加入队列 |
| `decline <PR>` | 拒绝待确认任务，不调用模型 |
| `retry <PR>` | 重试 needs_attention 或 reviewed_only 任务，保留去重记录及已有报告缓存 |
| `poll` | 请求后台尽快轮询；当前轮询尚未完成时等待其完成 |
| `quit` / `exit` | 停止调度，等待当前轮询和评审结束后退出 |

新 PR 会提示待确认编号，输入 `approve` 即为确认。任务信息可通过 PR 页面查看：
`https://gitcode.com/<owner>/<repo>/pull/<编号>`。交互模式的详细 DFX 日志写入当前
仓库状态目录的 `watcher.log`，避免刷屏；通过 `status` 查看任务处理结果。
确认行为仍遵循 `require_confirmation` 配置，关闭该配置会自动入队。

Ctrl+C 或终端输入结束也会有序退出；已有评审可能需要等到执行完成或配置超时。
命令输错会提示并继续等待输入。当前会话固定仓库和模型，切换时退出并带 `-Repo`、
`-Model` 重新启动。

`./start.ps1 -NoInteractive` 或 `python watcher.py --no-interactive` 可用于持续日志模式，
待确认任务使用另一个终端的 `--approve-pr` 等命令处理。
也可使用 `-Interactive` / `--interactive` 显式要求交互终端；非 TTY 会立即报错。
默认非 TTY 不进入命令窗口。现有 `--once`、`--status`、`--check` 等单次命令保持原用法，
不能与显式 `--interactive` 同用。

## Windows 只读评审权限

评审命令使用 `--ignore-user-config`，因此会跳过个人配置里的 Windows 沙箱设置。
Windows 下现显式传入 `-c windows.sandbox="elevated"`，同时保留
`--sandbox read-only` 和 `approval_policy="never"`。不忽略命令规则，也不自动降级为完全访问。

若本机尚未完成 elevated 沙箱安装，需先在 Codex 中完成原生 Windows 沙箱设置；
企业限制下应由管理员处理，不能通过关闭沙箱解决。
说明见 [OpenAI Windows 沙箱文档](https://learn.chatgpt.com/docs/windows/windows-sandbox)。

更新后应退出并重启旧 watcher。已经生成的 `incomplete` 报告仍会被缓存复用，
重新评审前需在停止监听后将对应任务目录的 `review.json` 改名保留，再执行 `retry <PR>`。

## 运行记录滚动清理

在 `config.json` 中设置 `"retain_pr_runs": 5`（默认值，必须为正整数）。
每个仓库独立保留最近处理的 5 个不同 PR 的 `runs` 记录，启动监听及每次任务结束时执行清理。
同一 PR 的多次运行目录算一个 PR，按目录最近更新时间排序，重试会刷新时间。

超出范围的已结束记录会递归删除，包括 Git 对象、diff、`review.json`、摘要和 diagnostics。
`needs_attention` 和 `reviewed_only` 也属于可清理记录；需要长期保留的问题报告应先自行备份。
尚在队列、运行中、未知状态，以及有 pending 评论发送意图的 PR 不删除，所以实际保留数量可能超过 5。
这按 PR 数量限制历史记录，不是磁盘字节配额。

SQLite 任务状态与评论去重记录保留；已清理 PR 后续显式重试时会重新准备材料并调用模型。
不清理 `watcher.log`（继续使用原有 5 MiB × 主文件及 3 个备份的轮转方式）。
只删除符合运行目录命名规则的目录，拒绝符号链接和 Windows junction；删除失败写 WARNING，
不改变任务结果或触发重复评论。更新后重启监听器生效。

### 交互界面的评审完成通知

PR 成功处理后会直接在终端输出编号、页面链接、摘要、问题数量及发布状态，
随后逐条显示问题优先级、标题、`文件路径:新增行号`、完整描述和依据。
无问题会明确提示未发现符合条件的 P1/P2 问题；关闭发布时显示“仅评审，未发布评论”。
通知在报告和任务状态保存成功后发出，退出等待期间完成的任务也会显示。
后台通知不会提交当前正在输入的命令；输出后可继续输入并按回车。
详细 DFX 日志仍写文件，失败任务仍可通过 `status` 查看原因。

### 批准前查看 PR 标题

每次成功获取完整开放 PR 列表后，自动将已经关闭或合入的 PR 从 `pending`、交互 `status`
和 `--status` 输出隐藏，停止这些任务的排队。数据库历史和评论去重记录仍保留。
关闭后重新打开、再次出现在开放列表中的 PR 会重新进入待确认状态，需要再次 `approve`；
重新确认后的处理继续遵循报告缓存规则，不保证重新调用模型。正在执行的任务由现有发布前检查保护。
网络或解析失败不修改名单。显示依据最后一次成功轮询；可输入 `poll` 请求刷新。
若关闭和重新打开都发生在两次轮询之间，工具无法观察到这次状态切换。

新 PR 待确认提示和 `pending` 显示编号与标题，`status` 额外保留任务状态和尝试次数。
这些列表不显示 PR 描述或链接。优先复用轮询标题，缺少标题时读取详情并缓存 5 分钟。
读取失败显示“标题暂不可用”，再次输入 `pending` 或 `status` 可重试。
