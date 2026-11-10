# 架构与开发入口

`watcher.py` 仅保留启动入口和旧导入兼容。实现放在 `review_watcher/`，仅依赖 Python 标准库。

| 模块 | 职责 |
| --- | --- |
| `cli.py` | 参数解析、控制命令、日志初始化、启动应用 |
| `console.py` | 前台持续命令输入、后台调度生命周期、批准与重试命令分派 |
| `config.py` | 配置校验、仓库白名单、状态目录隔离及旧目录兼容 |
| `scheduler.py` | 主进程轮询、用户确认、单工作线程调度 |
| `pipeline.py` | 单个 PR 的处理步骤、阶段日志、任务结果和重试决策 |
| `state.py` | SQLite 状态转换、评论去重记录、进程锁 |
| `gitcode.py` | GitCode CLI 的 JSON 调用封装 |
| `workspace.py` | 获取 Git 对象、固定提交、生成 diff 和有效新增行 |
| `reviewer.py` | Reviewer 接口、Codex 命令、提示词、缓存及结果读取 |
| `validation.py` | 评审报告和行号校验 |
| `publisher.py` | 发布前提交校验、行内评论发布、去重和发布结果核验 |
| `commands.py` | 子进程执行、超时、可执行文件查找、DFX 日志脱敏 |
| `common.py` | 异常类型、标识校验、原子 JSON 写入 |

调用关系：`cli → scheduler → pipeline → workspace / reviewer / publisher`。
调度器只调用单 PR 处理函数，不启动模型命令；Codex 后端不持有评论发布组件。

## 扩展评审后端

实现 `Reviewer.review(pr, job, ancestor, locations)`，返回 `(report, findings)`，
使用 `validation.validate_report()` 校验结果，再通过
`Watcher(..., reviewer=your_reviewer)` 注入。当前命令行仍使用 Codex，
`--model` 和配置中的 `model` 行为保持不变；此重构不新增其他供应商配置。

`Watcher` 同时支持注入状态存储、GitCode 客户端、Git 工作区和发布器。
命令执行组件支持注入 `runner`，便于离线验证超时、错误和输出处理。
注入对象属于受信任的应用代码，不从 PR 内容动态加载。

## 兼容与验证

`python watcher.py`、`start.ps1` 的启动方式不变，SQLite 表结构、配置字段、
运行目录和报告格式不变。升级前停止旧进程，复制完整程序目录后重新启动，
保留自己的 `config.json` 和 `runtime/`。

运行 `python -m unittest discover -v`，覆盖原有状态、确认、去重、发布保护和 DFX，
以及组件注入、调度与后端调用边界。测试不调用真实模型或发布远端评论。
打包脚本收集 `review_watcher/*.py`，不收集缓存或运行记录。

## 非源码文件说明

JSON 和 VERSION 保持机器可读格式，说明集中记录在此，不向文件中插入非法注释。

| 文件 | 用途与修改注意事项 |
| --- | --- |
| `config.json` | 用户运行配置：仓库白名单、轮询周期、模型、确认开关和执行限制。字段用法见 README；不应填写真实凭证。 |
| `review-schema.json` | 约束模型输出的 JSON 结构；运行时还会由 `validation.py` 校验提交及有效代码行。 |
| `review-prompt.md` | 评审任务指令模板；后端读取后附加受信任的仓库、PR 编号和提交信息。修改会影响后续新评审，已有缓存不会自动失效。 |
| `VERSION` | 手动打包时使用的版本号，保持单行版本文本。 |
| `.gitignore` | 排除运行数据、缓存和打包产物，避免将本地评审记录纳入版本管理。 |
| `README.md` | 面向使用者的安装、配置、启动与排错说明。 |
| `ARCHITECTURE.md` | 面向维护者的模块职责、依赖关系及扩展入口。 |
| `VALIDATION.md` | 记录已执行的验证和未覆盖范围，不代表每次修改都运行了真实模型。 |

各 Python 模块和 PowerShell 脚本带中文文件说明，主要类和函数前的注释解释其职责及约束。

`retention.py`: PR run retention, active-job protection and bounded-directory deletion.
