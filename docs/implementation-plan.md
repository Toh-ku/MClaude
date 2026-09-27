# Python Agent 渐进式实施计划

## 项目目标

参考 Claude Code 公开的架构设计，使用 Python 实现自己的编程 Agent，并使用 uv 管理 Python 版本、依赖和项目命令。从最简可用版本开始，逐步加入新机制，每个新增功能机制单独提交一个 commit。

实施路线：**最小 Agent 闭环 → 编程能力 → 长任务管理 → 扩展机制**。

本文描述分阶段实施方案；完成情况见 [实施记录](progess.md)。制定计划时，项目目录为空，尚未初始化 Git。

## 设计原则

- 从单个 Python 包和命令行程序开始，使用 `pyproject.toml` 声明项目与依赖，并通过 uv 创建和维护虚拟环境；随功能增长拆分模块。
- 首先接入 Anthropic Messages API，模型名称通过配置指定，凭据不写入仓库。
- 自行实现 Agent 核心循环，使用结构化 tool calling，不解析自然语言来执行命令。
- 模型决定下一步，运行时执行工具并回传结果，循环完成信息收集、行动和验证。
- 每个 commit 都可运行、可演示，并通过格式检查、静态检查和测试；相关测试和说明随对应机制一起提交。
- Windows 优先，将路径和进程执行差异集中处理，为 Linux/macOS 留出空间。
- 以下模块划分和实施顺序是本项目的设计方案，参考公开机制，不假定 Claude Code 的内部实现细节。

## 第一阶段：最简可用 Agent

验收目标：输入“读取这个文件并总结”，Agent 自行调用读取工具，获得内容后给出回答。

| Commit | 新增内容 | 验收标准 |
| --- | --- | --- |
| 01 `chore: bootstrap python cli` | 初始化 Git、uv 管理的 Python 项目、命令行入口和基础说明 | 能通过 `uv run` 运行程序、查看帮助 |
| 02 `feat: add model client` | 接入模型 API，支持凭据、模型配置和单次文本请求 | 命令行输入问题，获得真实模型回答；请求失败有明确错误 |
| 03 `feat: add minimal agent loop` | 引入消息历史、工具调用与结果回传；仅提供 `read_file` 工具 | 完成“请求 → 读文件 → 回传 → 回答”；支持同一响应中的多个工具调用，先顺序执行 |

第 03 个 commit 构成第一个可用 Agent。这个闭环同时包含必要的边界：

- 只允许读取工作目录内的文件。
- 限制文件输出长度和最大循环次数。
- 将工具失败作为结果回传模型。
- 区分模型正常结束、输出截断和请求失败。

## 第二阶段：修改和验证代码

| Commit | 新增机制 | 验收标准 |
| --- | --- | --- |
| 04 `feat: add workspace search` | 文件发现、文本搜索 | 能根据符号或错误信息定位相关文件 |
| 05 `feat: add tool permission gate` | 工具执行前统一进行允许、询问、拒绝判断 | 拒绝后工具不执行，原因回传模型 |
| 06 `feat: add file editing tool` | 创建文件与精确文本替换，展示 diff | 能修改目标代码；匹配歧义或文件已变化时拒绝覆盖 |
| 07 `feat: add command execution tool` | 执行命令，捕获退出码和输出，支持超时 | 能运行 `uv run pytest`、`uv run ruff check .`，读取失败原因 |
| 08 `feat: add interactive conversation` | 终端多轮对话，保留当前会话上下文 | 用户能继续追问、纠正需求，Agent 接着处理 |

第 07 个 commit 的端到端验收任务：在示例项目中定位 bug、修改代码、运行测试，并根据测试结果决定继续修复或结束。

命令执行初期逐次确认。设置工作目录不等于操作系统沙箱，沙箱隔离留作独立后续机制。

## 第三阶段：稳定的持续工作

| Commit | 新增机制 | 验收标准 |
| --- | --- | --- |
| 09 `feat: stream model responses` | 流式接收模型输出 | 文本逐步显示；工具参数完整后才执行 |
| 10 `feat: add turn cancellation` | 取消当前模型请求或工具执行 | 中断后可继续对话；运行中的命令及子进程得到清理 |
| 11 `feat: persist and resume sessions` | JSONL 会话记录与恢复 | 重启后可继续；恢复不会自动重放已执行的修改或命令 |
| 12 `feat: load project instructions` | 按明确作用域加载 `AGENTS.md` 等项目约定 | Agent 能遵守项目规则，并可查看规则来源 |
| 13 `feat: enforce context budget` | 统计上下文占用，限制工具结果注入量 | 大文件、大输出不会无限挤占上下文 |
| 14 `feat: compact conversation context` | 对较早历史做摘要，保留目标、约束和近期调用 | 压缩后能继续任务，工具调用与结果仍完整配对 |
| 15 `feat: add request retry policy` | 对可恢复的 API 错误进行有限重试 | 模拟限流和暂时性错误后可恢复；有副作用的工具不自动重跑 |
| 16 `feat: add edit checkpoints` | 为文件编辑保存快照，提供恢复功能 | 可撤销 Agent 的文件修改；遇到后续外部修改时提示冲突 |

## 第四阶段：扩展机制

Skills、Hooks、MCP 和子 Agent 作为核心循环之上的扩展逐步加入。

| Commit | 新增机制 | 验收标准 |
| --- | --- | --- |
| 17 `feat: add task tracking` | 显式维护任务步骤和状态 | 多步骤任务可查看进度，恢复会话后仍保留 |
| 18 `feat: add read-only planning mode` | 只允许读取与搜索的规划模式 | 可以分析项目并输出计划，写入和命令执行被运行时阻止 |
| 19 `feat: load skills on demand` | 发现技能描述，按需加载 `SKILL.md` | 未使用的技能正文不进入上下文 |
| 20 `feat: add lifecycle hooks` | 在工具执行前后运行配置的 Hook | Hook 可阻止执行或补充结果，具有明确超时行为 |
| 21 `feat: connect mcp tools` | 首先支持 stdio MCP 服务发现与调用 | 外部工具通过现有权限入口执行 |
| 22 `feat: add isolated subagents` | 子 Agent 独立上下文、受限工具集合，向父 Agent 返回结果 | 可委派只读调查；限制递归深度和运行预算 |
| 23 `feat: schedule parallel read tools` | 并发执行相互独立的只读工具 | 结果正确关联调用 ID，写操作仍保持顺序 |

这里的子 Agent 是未来产品功能。

## Python 代码组织

采用 `src` 布局，起步只需要命令行入口、模型客户端和 Agent 循环几个模块。运行时依赖与开发依赖统一声明在 `pyproject.toml` 中并锁定到 `uv.lock`。功能增长后，逐渐形成以下职责划分，不提前创建空模块或复杂框架。

| 模块 | 职责 |
| --- | --- |
| `agent` | 循环状态、停止条件、任务执行 |
| `provider` | 模型协议转换和请求处理 |
| `tools` | 工具定义、参数校验、执行结果 |
| `permissions` | 执行前的权限判断 |
| `session` | 会话记录与恢复 |
| `context` | 项目规则、上下文预算和压缩 |
| `cli` | 输入、输出和交互 |

## 验证与提交约定

- 核心逻辑通过可替换的模型客户端测试，用固定响应验证多次工具调用、失败恢复和终止行为。
- 工具测试在临时目录和示例项目中运行，覆盖权限边界及副作用行为。
- 真实 API 用于小规模端到端验收，不作为常规自动化测试的必需依赖。
- 每次提交前通过 uv 运行格式检查、静态检查及与改动相关的测试，例如 `uv run ruff format --check .`、`uv run ruff check .` 和 `uv run pytest`。
- 每个新增机制独立提交，配套测试与文档放入该机制的 commit，不混入无关功能。
- 每一步完成后记录实际实现、验证结果和必要的计划调整。

## 实施起点与后续范围

开始编码后，先完成 01—03 的最小闭环，分别提交，再进入代码修改与验证阶段。

沙箱、多模型提供商、后台任务和图形界面等能力，等基础版本实际使用后再安排，届时继续按独立机制拆分 commit。

## 参考资料

- [Claude Code 工作原理](https://code.claude.com/docs/en/how-claude-code-works)
- [Claude 工具调用协议与循环](https://platform.claude.com/docs/en/agents-and-tools/tool-use/how-tool-use-works)
- [Claude Code 扩展机制](https://code.claude.com/docs/en/features-overview)
