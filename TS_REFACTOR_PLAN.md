# MClaude TypeScript 重构技术方案

日期：2026-10-13。本文保留实现前的模块方案。按用户后续要求，语言目录采用 `python/` 和 `ts/`；TS 可以独立设计，不要求复制 Python 规范。实际实现、行为差异和待审核选项以 [ts/DESIGN.md](ts/DESIGN.md) 为准。

## 1. 现状与重构目标

当前项目是本地优先的 Python CLI 编程 Agent，通过 Anthropic Messages API 驱动工具循环。源码包含 22 个 Python 模块（含两个包入口），测试包含 21 个 `test_*.py` 模块、185 个测试函数；参数化测试的实际用例数更多。官网是独立的 HTML/CSS/JavaScript 静态站点。

重构目标：

- Python 与 TypeScript 各自可以独立安装、运行、测试和构建，分别置于 `python/` 与 `ts/`。
- TypeScript 完整覆盖现有 CLI 能力，保留参数、工具名称、默认限制、权限规则和恢复语义。
- 拆分模型循环、工具执行、状态存储和终端显示，通过明确接口连接，避免将 `agent.py` 与 `tools.py` 直接翻译为两个大型 TS 文件。
- 兼容已有用户配置、会话 JSONL 和编辑检查点；两种实现能够识别同一个会话锁。
- 先保证行为一致，再通过实测决定性能优化；不预设 TS 一定更快。

当前工作区已有用户对 `.gitignore` 的修改；实施时保留。`docs/` 与 `benchmarks/` 当前被忽略，因此本方案放在仓库根目录，已有本地基准材料不自动纳入版本控制。

## 2. 目录组织

```text
MClaude/
├── README.md                        # 总览、两种实现的入口与兼容说明
├── TS_REFACTOR_PLAN.md
├── .github/workflows/ci.yml          # 按语言分开的检查任务
├── contracts/                       # 语言无关的规范及固定输入/预期输出
│   ├── schemas/                     # 工具输入、配置、会话事件、检查点
│   └── fixtures/                    # Python/TS 共用的兼容样例
├── python/
│   ├── README.md
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── .python-version
│   ├── src/mclaude/
│   └── tests/
├── ts/
│   ├── README.md
│   ├── package.json
│   ├── package-lock.json
│   ├── tsconfig.json
│   ├── tsconfig.build.json
│   ├── tsconfig.test.json
│   ├── src/
│   │   ├── cli/                     # 参数、交互会话、登录、历史命令
│   │   ├── core/                    # Agent 循环、类型、预算、任务、事件
│   │   ├── providers/               # Anthropic 适配器
│   │   ├── tools/                   # 注册表与分类工具
│   │   ├── security/                # 权限、工作区路径、规划模式
│   │   ├── persistence/             # 配置、会话、锁、检查点
│   │   ├── integrations/            # Hooks、MCP、技能
│   │   ├── runtime/                 # 调度、取消、命令与进程树
│   │   ├── terminal/                # 终端状态、渲染、交互输入
│   │   └── index.ts                 # 显式公共导出
│   ├── tests/                       # 单元、集成、兼容性测试
│   └── dist/                        # 编译输出，不提交
└── website/                         # 独立官网，共用项目资产
```

`website/` 不是另一种 Agent 实现，因此保留独立产品目录。将来增加 Go/Rust 实现时，在根目录增加对应语言文件夹。共用目录只保存协议、样例和文档，TS 运行时不依赖 Python。

迁移 Python 时移动源码、测试及构建配置，更新 README、CI 和本地基准脚本中的路径；不移动虚拟环境、缓存与历史构建产物。仍以用户启动时的当前目录作为工作区，不因代码位于语言子目录而改变工作区范围。

## 3. 技术选型

| 部分 | 方案 | 选择原因 |
| --- | --- | --- |
| 运行时 | Node.js 24 LTS，第一版支持 Node 24 系列 | 使用稳定 LTS；可用原生异步文件、流、信号和测试运行器 |
| 编译 | TypeScript、ESM、`module/moduleResolution: NodeNext`，`tsc` 构建 | CLI 保留 Node 模块语义，不增加打包器 |
| 类型检查 | `strict`、`noUncheckedIndexedAccess`、`exactOptionalPropertyTypes` | 明确处理缺失字段、工具输入与取消结果 |
| 依赖管理 | npm，提交 `package-lock.json`，CI 使用 `npm ci` | 工具链简单，安装可重复；具体版本在实现阶段锁定 |
| 模型 SDK | `@anthropic-ai/sdk` | 官方 Messages API 与流式响应支持 |
| MCP | 官方 TypeScript SDK v2 客户端 `@modelcontextprotocol/client`，封装为独立适配器 | 使用标准传输，同时把旧协议兼容与现有安全限制留在适配层 |
| 参数与交互 | `node:util.parseArgs`、`node:readline`；隐藏输入封装为独立 TTY 服务 | 保持 CLI 轻量，并使测试不依赖真实终端 |
| 数据校验 | JSON Schema + Ajv；输入边界使用 `unknown` | 工具定义和运行时校验共用 schema；保留额外字段与数值校验 |
| 文件发现 | `fs` 遍历 + `ignore` + 独立 glob 匹配适配层 | 精确控制嵌套忽略规则、符号链接和稳定顺序 |
| 差异与终端宽度 | `diff`、`string-width` | 标准 unified diff 与终端列宽计算；输出样式通过兼容样例固定 |
| 测试 | `node:test`、`node:assert/strict`，测试先编译再运行 | 原生测试运行器，注入模型传输、时钟、文件与进程故障 |
| 质量检查 | ESLint + typescript-eslint、Prettier、`tsc --noEmit` | 分别负责错误检查、格式和类型检查 |

外部模型/MCP 的 JSON Schema 不作为可直接信任的程序：禁用远程 schema 自动加载，限制 schema 与响应体大小；内部业务规则在 schema 校验后执行。业务层保留显式类型与转换函数，类型断言不能替代运行时验证。

上述选型参考 [Node 发布列表](https://nodejs.org/en/about/previous-releases)、[TS 配置文档](https://www.typescriptlang.org/tsconfig/)、[Anthropic SDK](https://github.com/anthropics/anthropic-sdk-typescript)、[MCP v2 客户端文档](https://ts.sdk.modelcontextprotocol.io/v2/clients/connect)、[Ajv](https://github.com/ajv-validator/ajv)、[ignore](https://github.com/kaelzhang/node-ignore)、[diff](https://github.com/kpdecker/jsdiff)、[string-width](https://github.com/sindresorhus/string-width) 和 [Node 测试文档](https://nodejs.org/api/test.html)。依赖可用版本与兼容范围在实际安装时再次核对。

## 4. 分层与公共接口

依赖方向：CLI 组装运行时 → 核心 Agent → 抽象接口；模型、存储、工具与终端实现这些接口。核心层不读取 `process.env`、不直接打印、不调用 SDK，不从文件工具反向导入 Agent。

```ts
interface ModelProvider {
  createMessage(request: ModelRequest, signal: AbortSignal): Promise<ModelResponse>;
}

interface ToolDefinition<TInput> {
  name: string;
  inputSchema: JsonSchema;
  effect: 'read' | 'edit' | 'command' | 'state' | 'external';
  parse(input: unknown): TInput;
  execute(input: TInput, context: ToolContext): Promise<ToolResult>;
}

interface SessionJournal {
  append(event: SessionEvent): Promise<void>;
  close(): Promise<void>;
}
```

以上为接口示意，具体消息类型由现有协议确定。消息、工具调用、状态事件采用判别联合类型。TS 内部可用 camelCase，写入文件与请求 API 时显式转换为原有 snake_case 字段。

区分两种事件：持久化事件必须 `await` 成功，错误向上传递；仪表盘事件用于显示，不能替代历史记录。这样才能保证“工具调用已记录 → 执行 → 结果已记录”，并避免 UI 故障破坏会话。

## 5. 每个部分的技术方案

下表中的源文件均相对当前 `src/mclaude/`，目标目录均相对 `ts/src/`。

| 当前部分 | TS 目标 | 实现方案 | 主要验收标准 |
| --- | --- | --- | --- |
| `__init__.py`、`__main__.py` | `index.ts`、`cli/main.ts` | 公共 API 显式导出；npm `bin` 入口调用 `main()`，统一映射退出码 | 安装后可运行 CLI；导入库不会启动交互或产生文件 |
| `cli.py` | `cli/args.ts`、`commands/`、`conversation.ts` | 子命令与对话循环拆开；参数先验证，再组装服务；统一处理 `login/logout/sessions`、单次任务和交互模式 | 现有参数、互斥选项、`/tasks /plan /execute /exit /quit`、无凭据历史查询行为一致 |
| `config.py` | `core/config.ts`、`cli/config-loader.ts` | 不可变配置对象；校验 URL、正数、整数、有限值；保留参数 > 环境变量 > 已保存配置的优先级 | 空 API Key/模型环境变量与空 base URL 的区别一致；不内置默认模型；非法配置不会调用模型 |
| `auth.py` | `persistence/config-store.ts`、`cli/commands/auth.ts` | 沿用用户级目录和 v1 配置格式；隐藏输入仅在真实 TTY 启用；同目录临时文件、刷新后替换 | 登录不调用网络；取消/写入失败不破坏旧配置；POSIX 新目录 0700、文件 0600；无 TTY 时不回显密钥 |
| `provider.py` | `providers/anthropic.ts`、`stream.ts` | 归一化文本和工具块；SDK 自动重试设为 0，由适配器执行原有重试策略；每轮传入 AbortSignal | 文本输出开始后不重试；收到完整 `message_stop`、闭合内容块且工具参数 JSON 合法后才交给工具层 |
| `agent.py` | `core/agent.ts`、`turn.ts`、`events.ts` | 明确状态转换：准备上下文 → 请求模型 → 校验响应 → 调度工具 → 记录历史 → 下一轮；依赖注入替代长参数列表 | 最大迭代限制、重复工具 ID、异常 stop reason、截断文本、失败后的已执行历史均正确；取消后可继续下一轮 |
| `permissions.py` | `security/permission-gate.ts`、`policy.ts` | 统一 `allow/ask/deny` 判别联合；注册表提供工具身份，策略和用户确认分开；确认错误按拒绝处理 | 未知工具拒绝；文件修改和命令逐次确认；外部 MCP 即使标记只读仍确认；确认输入不会变成对话 |
| 规划模式（`agent.py`、`cli.py`） | `security/mode.ts` | 同时过滤暴露给模型的工具和执行入口；从执行模式切换规划模式时关闭已连接外部服务，切回按需建立连接 | `--plan` 与 `/plan` 均不能写文件、执行命令、更新任务、启动 Hooks/MCP；防止仅隐藏工具定义但仍可执行 |
| `tools.py`：读取、发现、搜索 | `tools/files/read.ts`、`discover.ts`、`search.ts` | 所有路径经 WorkspaceFS；严格 UTF-8 解码；遍历按层加载 `.gitignore`，实现否定规则与稳定排序；扫描过程中检查取消和结果限额 | 拒绝越界路径、跳过 `.git` 与外部链接；嵌套忽略规则正确；保留文件大小、行长、结果数限制与行号 |
| `tools.py`：创建、替换、批量编辑 | `tools/files/create.ts`、`replace.ts`、`batch.ts` | 创建使用独占写入；替换要求唯一旧文本；整批最多 20 个不同文件，预校验全部操作后执行；检查点、写入、失败回滚协调 | 不覆盖已有文件；拒绝陈旧/歧义匹配；保留原字节、换行和权限；后续文件写入失败能恢复已修改文件 |
| `tools.py`：Git 审阅 | `tools/git/review.ts` | `spawn('git', argv)`，不拼接 shell；读取 staged/unstaged diff，运行对应 diff 检查并列出未跟踪文件 | `diff/precommit` 行为、截断和失败信息一致；不执行提交或更改 Git 状态 |
| `tools.py`：命令执行 | `runtime/process-runner.ts`、`tools/command.ts` | 宿主机命令显式启用；跨平台 shell 执行用户确认的命令，工作目录受限；分别读取 stdout/stderr，限额后继续排空流 | 默认不可用；保留退出码与部分输出；超时、取消终止 shell 及后代；不把 MCP/Hooks argv 改成 shell 字符串 |
| `checkpoints.py` | `persistence/checkpoint-store.ts` | 兼容 v1 JSON、32 位十六进制 ID、Base64 原字节、SHA-256 和原文件模式；恢复前及提交前校验当前字节 | 外部修改时拒绝覆盖；新建文件检查点可删除未变文件；恢复不可重复；状态写入失败明确报告实际恢复结果 |
| `session.py` | `persistence/session-store.ts`、`journal.ts`、`recovery.ts` | 继续使用 append-only JSONL；单写入队列、独占锁、序号与刷新；保留工作区隔离及列表/搜索/命名 | 读取原 Python 会话；仅忽略不完整末尾记录，完整坏记录报错；补齐未知工具结果，不重放工具；两实现不能同时写同一会话 |
| `processes.py` | `runtime/process-platform.ts` | 平台适配器负责 PID 存活检查、陈旧锁处理与进程树终止；保留 PID + token 锁格式 | 当前进程和活子进程不会误判失效；删除锁前重新检查身份；Windows 对 EPERM 等结果保守处理 |
| `context.py`：项目规则 | `integrations/instructions.ts` | 按工作区根到当前目录加载规则，同层 `AGENTS.md` 后加载 `CLAUDE.md`；文件读取走统一路径边界 | 来源、顺序、作用范围、长度限制一致；符号链接规则文件与越界范围被拒绝 |
| `context.py`：预算与压缩 | `core/context-budget.ts`、`compaction.ts` | 保留 UTF-8 字节估算、工具 schema/系统提示计入、输出 token 预留；按完整工具交换组压缩；初版继续本地摘要 | 保留初始目标、最近要求与已有摘要；工具调用/结果不拆散；容不下时明确报错；任务板独立保存 |
| `scheduler.py` | `runtime/tool-scheduler.ts` | 有限并发 Promise 队列；仅连续的 read_file/find_files/search_text/git_review 可并发；其他工具形成顺序屏障 | 默认 4、范围 1–16；整组权限检查结束后才启动；按完成时间持久化结果，按原调用顺序组织下一次消息；Hooks 启用时串行 |
| `cancellation.py` | `runtime/turn-controller.ts`、`cleanup.ts` | 每轮独立 AbortController；连接网络、读取循环、重试等待与进程执行；区分用户取消、超时和运行错误 | SIGINT 执行中取消当前轮、等输入时退出；取消后收齐已完成结果；重要清理阶段屏蔽重复中断，最后释放连接、锁和终端 |
| `tasks.py` | `core/task-board.ts` | 状态联合为 pending/in_progress/completed/blocked；先校验、再持久化事件、最后替换内存快照 | 最多 100 项、ID 唯一、字段长度一致；日志失败不修改任务；恢复和压缩后任务仍存在 |
| `skills.py` | `integrations/skills.ts` | 发现 `.mclaude/skills/<name>/SKILL.md` 元数据；显式 load_skill 才读取正文；使用有界元数据解析 | 不自动注入全部正文；按原限制处理非法、变更、超长及越界技能文件 |
| `subagents.py` | `core/readonly-delegation.ts` | 独立 history、共享按模型请求扣减的预算、深度限制；复用核心 Agent 接口，注入只读工具白名单 | 只允许读取/发现/搜索；不能递归；每次子调用 1–4 轮、共享预算默认 8/范围 0–32；主会话不接收子会话全文 |
| `hooks.py` | `integrations/hooks/config.ts`、`runner.ts` | 显式配置；argv spawn；before/after JSON 交换；复用超时与进程清理；有界输出，不暴露密钥 | before 异常/非法响应阻止执行；after 接收结果；每阶段最多 20 个；规划模式零调用；取消时收尾明确 |
| `mcp.py` | `integrations/mcp/config.ts`、`registry.ts`、`client.ts` | 官方客户端提供 stdio/Streamable HTTP；适配层保留环境变量 header、分页限额、工具命名路由、响应限额与权限 | JSON/SSE、会话头、协议协商、分页与失败清理正确；外部调用失败不自动重放；旧服务器通过兼容测试后才替换 |
| `verification.py` | `core/verification.ts`、`tools/checks.ts` | 保存本进程检查命令与状态；最多 5 个检查按序运行，首个失败停止；retest 明确重新确认同组命令 | 未运行时拒绝复测；通过后再次编辑标记 stale；重启不伪装检查已通过；保留 attempts 与摘要 |
| `terminal.py` | `terminal/status-model.ts`、`renderer.ts`、`tty.ts` | 模型输出仍走 stdout、状态走 stderr；独立刷新/背压；ANSI alternate screen、resize 与字符串列宽处理 | `--plain` 和重定向自动回退；清理控制字符；中文/emoji 不破坏边框；确认时暂停刷新，退出恢复终端且重复关闭安全 |

## 6. 重点实现细节

### 6.1 工作区路径与文件提交

WorkspaceFS 统一实现 `path.resolve`、`realpath`、`path.relative` 和平台大小写规则；不能用字符串前缀判断包含关系。不存在的目标先验证最近存在的父目录，创建目录后、写入前再次检查，覆盖符号链接和 Windows junction 的实际指向。

修改文件使用同目录临时文件 → 刷新数据 → 检查原文件字节未改变 → 原子 rename；创建文件独占打开。保留权限和原始换行。批量编辑整体预检，失败时按逆序回滚；若外部程序已再次修改某文件，不强制回滚，而是返回冲突及检查点。

这是本地工具的工作区边界，不是操作系统沙箱。便携 Node 文件 API 的检查与提交之间仍有竞争窗口，不宣称能阻止恶意程序同时替换父目录。宿主机命令仍可能访问工作区外部，与现有 README 一致。

### 6.2 模型流与重试

保留现有可重试 HTTP 状态：429、500、502、503、504、529，以及连接错误/超时；指数退避可取消。已经向用户输出文本的请求不重试，防止重复正文。截断或不完整的工具 JSON 不执行，模型正文可保留。

所有错误通过映射生成可操作提示，不直接打印服务端响应体、SDK 原始错误对象或请求头。API token 用量使用真实响应字段；上下文估算在界面中单独标明。

### 6.3 取消与进程树

取消控制器仅管理一轮；下一轮必须创建新控制器。网络取消等待响应头和数据都应生效；读取/搜索需主动检查 signal，不能把 Promise 并发误当成 CPU 并行。

POSIX 使用独立进程组并终止组；Windows 沿用当前 `taskkill /PID <pid> /T /F` 的进程树策略，并检查实际终止结果。不能仅调用 `child.kill()` 后就认为后代已退出。平台差异依据 [Node 子进程文档](https://nodejs.org/api/child_process.html) 处理。

已完成工具结果在取消后仍保留；未启动工具记录取消，已启动且结果未知的工具标记需要检查工作区。清理和结果补齐执行完后才把控制权交还 CLI。

### 6.4 原有状态兼容

- 配置保持 `version/api_key/model/base_url`，检查点保持 v1 字段，会话保持原事件名称与消息块格式。
- 工作区 key 仍为规范化绝对路径 UTF-8 的 SHA-256 前 20 位；Windows 小写及反斜杠规则必须与 Python `normcase` 对齐。
- UUID 去除连字符，保留 32 位十六进制 ID。UTC 时间字段保留可解析格式；不用 JS 默认毫秒精度冒充 Python 的微秒精度。
- 锁继续使用 PID + 随机 token；独占创建、校验 token 后释放。跨实现并发测试覆盖陈旧锁、权限错误与损坏锁。
- 新增内部字段通过适配器映射，第一版不提升持久化版本、不批量重写历史日志。
- Node 的 JSON/UTF-8 默认容错不等价于 Python 的严格解码；用严格解码与显式数字/字段验证保持行为。

### 6.5 MCP SDK 的兼容关口

现实现支持协议 2025-06-18、2025-03-26、2024-11-05；不能因为官方 SDK 更新就直接失去这些服务器。用现有模拟 stdio/HTTP 服务先测协议协商与消息限制，再接入 v2 客户端。

若当前 v2 客户端不覆盖某个旧协议或现有限制，保留隔离的 legacy transport 适配器实现该部分，直到它通过同一组契约测试；不把它混入 Agent 循环。第一版不增加 OAuth、旧 HTTP+SSE 回退或外部工具自动重试。

### 6.6 两种语言的差异处理

Python 的 Unicode casefold、字符计数、glob 和正则与 JavaScript 并不完全一致。为中文、emoji、组合字符、嵌套忽略、大小写搜索和已有正则样例建立共享测试。字符上限按 Unicode code point 计数，文件/网络上限按字节，终端布局按显示列宽。

正则/匹配兼容层优先覆盖当前实现和测试所用语法；Python 专属语法若无法等价支持，显式拒绝并在兼容报告中列明，不能静默改变匹配结果。性能分析若发现同步匹配阻塞取消，再将该计算移入有超时的 worker；普通文件 I/O 不默认使用 worker_threads。

## 7. 测试、构建、CI 与官网

| 部分 | 技术方案 | 验收 |
| --- | --- | --- |
| Python 基线 | 迁移前跑原格式、lint、pytest、构建；迁移后在 `python/` 重跑 | 区分原有失败与目录迁移引入的失败；功能和 wheel 内容不变 |
| TS 单元测试 | 将现有 185 个测试函数作为行为清单；重点覆盖状态转换和失败分支，不要求机械地逐文件复制 | 所有模块有对应验证，尤其是权限、文件事务、流结束、日志恢复和取消 |
| TS 集成测试 | 注入模拟模型 fetch；启动本地 MCP/Hooks/命令子进程；临时状态目录隔离 | 无真实 API key、无模型费用；不写用户真实配置和会话 |
| 跨语言兼容 | 共用 fixtures，Python → TS 与 TS → Python 读取配置/会话/检查点；两进程竞争同一个锁 | 字段、ID、工作区 key、工具配对和冲突检测一致 |
| 故障注入 | 模拟半条 JSONL、磁盘写入失败、文件外部变更、子进程卡住、输出超限、响应中断 | 可恢复且不重复副作用；失败不能被标记成验证成功 |
| TS 构建 | `tsc` 生成 `dist/`、声明文件与 source map；npm `files` 限定发布内容，入口有 shebang | `npm pack` 后在隔离目录安装并验证命令、帮助和入口权限 |
| 共用 schema 打包 | 构建阶段将运行时所需 contracts 拷贝进发布目录，版本来自仓库契约；测试比较打包内容 | 安装后的 CLI 不引用仓库外部 `../contracts` |
| CI | 根工作流分 Python/TS 两个 job；先 Linux+Windows，兼容阶段加入 macOS；固定依赖与 Actions 引用 | 格式、lint、类型、测试、构建、安装冒烟均通过；无需模型凭据 |
| 本地性能基准 | 复用 `benchmarks/` 场景并更新路径；对同一工作负载比较启动、读工具、压缩、恢复、峰值内存与 p50/p95 | 记录机器、版本、输入规模和重复次数；性能收益以结果为准 |
| 官网 | 保留 HTML/CSS/SVG 和展示交互；更新安装路径及语言入口；如迁移官网 JS 到 TS，另列后续工作 | 演示、复制、移动布局、减少动画偏好正常；不引入 API key 或真实模型调用 |
| 文档与忽略规则 | 根 README 总览，语言子目录各自开发指南；增加 Node 构建/测试缓存忽略，保留官网 dist 例外 | 示例从正确目录运行，用户现有 `.gitignore` 改动完整保留 |

目标开发命令：

```powershell
# Python：从 python/ 运行
uv sync --locked --dev
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv build

# TypeScript：从 ts/ 运行
npm ci
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build
npm pack
node dist/cli/main.js --help
```

以上 npm 脚本在工程骨架阶段建立，不是当前已存在的命令。两种包可在各自环境提供 `mclaude`，开发期间显式从语言目录启动，避免全局命令来源不清楚；第一阶段不执行公开发布。

## 8. 实施顺序与完成条件

| 阶段 | 工作 | 进入下一阶段的条件 |
| --- | --- | --- |
| 0：技术方案 | 梳理模块、目录、接口与兼容清单 | 本文覆盖所有现有模块与配套工程设施 |
| 1：基线与分目录 | 验证 Python 基线，建立 contracts，迁移 Python 文件，更新路径和 CI | Python 迁移后运行/测试/构建通过，工作区根语义未变化 |
| 2：TS 基础 | 工程配置、类型/校验、CLI 参数、配置、登录、WorkspaceFS | 帮助/登录/历史基础命令和安全路径测试通过 |
| 3：最小 Agent 闭环 | 模型适配、流式响应、权限、读取/搜索、调度、取消、单次会话 | 模拟模型能够调用只读工具并完成任务，取消后能重新运行 |
| 4：写入与恢复 | 文件编辑、检查点、JSONL 会话、任务与压缩、验证流程 | 事务失败和崩溃恢复通过，跨语言数据兼容通过 |
| 5：完整能力 | 交互界面、进程树、技能、Hooks、MCP、只读子 Agent | 所有原能力在 TS 有可运行实现与对应验收 |
| 6：交付 | 多平台回归、包安装验证、基准、README/官网更新 | 两种实现可独立安装和运行，兼容差异已列明，CI 通过 |

完整重构的完成标准是上面所有阶段通过。只有目录迁移、TS 工程骨架或最小 Agent 闭环时，不能报告重构已完成。

Python 已迁入 `python/`，TS 实现位于 `ts/`。开发入口与检查命令见根 README；共用协议与双向兼容验证位于 `contracts/`。后续改变用户行为或数据协议的提案单独记录在 TS 设计文档中。
