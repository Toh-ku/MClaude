# MClaude

使用 Python 逐步实现的命令行编程 Agent，使用 uv 管理 Python、依赖和项目命令。

当前支持流式输出、当前轮取消、可恢复的终端多轮会话和 Agent 工具闭环：模型可以读取、发现、搜索和精确修改工作区文件，也可以运行测试、格式检查等开发命令，再将工具结果纳入消息历史后继续回答。所有工具调用在执行前统一经过权限判断。

启动时会加载当前工作区适用的 `AGENTS.md` 和 `CLAUDE.md`。规则按工作区根目录到当前工作目录的顺序加载，仅作用于规则文件所在目录及其子目录；同一目录中 `CLAUDE.md` 后加载。可在无需 API 配置的情况下查看实际来源：

```powershell
uv run mclaude --show-instructions
```

符号链接规则文件会被忽略，单个规则文件最多 100,000 个字符。规则来源、作用域和正文作为 system 内容发送给模型，不写入对话历史。

## 快速开始

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后在项目根目录执行：

```powershell
uv sync --locked
uv run mclaude --help
uv run mclaude --version
```

项目通过 `.python-version` 选择 Python 3.12；uv 会在需要时下载对应解释器，并创建 `.venv`。依赖版本由 `uv.lock` 锁定，`uv sync` 默认包含开发依赖。

也可以使用模块入口：

```powershell
uv run python -m mclaude --help
```

## 运行 Agent

在 PowerShell 中配置 API 密钥和你有权使用的模型 ID：

```powershell
$env:ANTHROPIC_API_KEY = "你的 API 密钥"
$env:ANTHROPIC_MODEL = "你的模型 ID"
uv run mclaude "读取 README.md 并用一句话总结"
```

模型 ID 必须显式配置，不内置默认模型。命令行参数优先于 `ANTHROPIC_MODEL`：

```powershell
uv run mclaude "读取 pyproject.toml 并总结依赖" --model "你的模型 ID" --max-tokens 2048 --timeout 90
```

也可以复制 `.env.example` 为本地 `.env`，程序启动时会自动加载：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写 API 密钥与模型 ID
uv run mclaude "你好"
```

程序读取当前工作目录下的 `.env`，但不会覆盖已有的系统环境变量。该文件已被 Git 忽略，勿提交真实密钥。可选的 `ANTHROPIC_BASE_URL` 由 SDK 读取，用于指定兼容 Anthropic Messages API 的服务；默认连接 Anthropic 官方 API。

传入问题时默认处理一个任务后退出。配置好模型后，在终端无参数运行，或显式使用 `-i` / `--interactive`，即可连续提问：

```powershell
uv run mclaude
uv run mclaude --interactive
uv run mclaude -i "读取 README.md 并总结"
```

交互模式下，每行输入是一轮问题，可以继续追问或纠正需求；当前会话保留用户输入、模型回答、工具调用和工具结果。空行会被忽略，输入 `/exit`、`/quit` 或 EOF（Windows 通常为 Ctrl+Z 后回车）退出；等待输入时按 Ctrl+C 退出整个程序。通过管道输入时需显式添加 `-i`，否则无参数运行仍显示帮助。

交互会话默认保存为用户本地状态目录中的追加式 JSONL 日志，启动时会在标准错误中显示 32 位 session ID。普通的单次任务不会保存。使用 `--continue` 恢复当前工作区最近的会话，或使用 `--resume SESSION_ID` 恢复指定会话；两个选项都会进入交互模式，也可以附带一个位置参数作为恢复后的首个问题。临时对话可使用 `--no-persist` 禁止保存：

```powershell
uv run mclaude --continue
uv run mclaude --resume 0123456789abcdef0123456789abcdef
uv run mclaude -i --no-persist
```

Windows 默认将会话写入 `%LOCALAPPDATA%\MClaude\sessions`，Linux/macOS 使用 `$XDG_STATE_HOME/mclaude/sessions` 或 `~/.local/state/mclaude/sessions`；可用 `MCLAUDE_STATE_DIR` 覆盖状态目录。会话与启动时的工作区绝对路径绑定，不能从另一个目录恢复；同一会话也不能被两个 MClaude 进程同时打开。日志包含对话、源码片段和命令输出等上下文，但不会保存 API 密钥、环境变量或工具授权回答。

恢复只重建发送给模型的消息历史，绝不会执行日志里的历史工具调用。工具调用会在执行前落盘，每项结果在执行后立即保存。如果进程在工具运行期间异常退出，恢复时会为缺失结果补充错误记录，提示该工具可能已产生部分副作用、应先检查工作区；该工具不会自动重跑。日志最后一条未完整写入的记录可以安全丢弃，中间记录损坏则拒绝恢复。

模型请求失败、循环超限或输出截断后仍可继续输入，已经完成的工具调用及结果会保留，不会因继续对话而自动重放。截断的文本会保留，未完成的工具调用不会执行或加入后续上下文。工具授权仍逐次询问，授权回答不作为对话消息发送给模型。提示符和错误写入标准错误，模型回答写入标准输出。

命令行默认流式显示模型文本，包括工具调用前的说明和最终回答，文本到达后立即刷新；不同响应之间分行显示，结束时不重复打印。工具参数由 SDK 汇总，接收完整消息并验证 JSON 后才进入权限判断和执行。流中断或响应不完整时保留已显示文本，但不执行该响应中的工具；错误信息不包含原始服务响应。

交互模式执行期间（包括等待模型、接收文本、询问工具权限或运行命令）按 Ctrl+C 取消当前轮，清理后返回 `You>`，可以直接输入新的要求。模型连接会关闭；命令取消会终止进程树、回收进程并收集已有输出；尚未执行的工具不会继续执行。历史保留已显示文本和已完成工具结果，并为中断、跳过的工具补齐错误结果，后续请求不会自动重放这些工具。取消不会撤销已经发生的文件或命令修改，结果未知时会明确提示先检查工作区。清理期间重复 Ctrl+C 会被暂时忽略，以便完成清理与历史修复。

当前提供只读的 `read_file`、`find_files`、`search_text`、`list_edit_checkpoints`，修改文件的 `create_file`、`replace_text`、`restore_edit_checkpoint`，以及执行开发命令的 `run_command`。每个调用执行前都会经过统一权限入口；已知只读工具自动允许，文件修改、恢复和命令执行逐次询问，未知工具拒绝。权限询问不可用、策略异常或响应无效时默认拒绝。拒绝的工具不会执行，拒绝原因会作为错误工具结果回传模型。

所有路径解析后必须位于启动命令时的当前工作目录内；文件发现与文本搜索跳过 `.git` 和工作区外的符号链接，并限制结果数量、文件大小与单行长度。`create_file` 只创建新文件，不会覆盖已有路径；`replace_text` 要求旧文本精确且唯一，文本缺失、匹配歧义或写入前文件变化时拒绝覆盖。成功修改会向模型返回 unified diff。单个文件读取最多返回 100,000 个字符，单个待编辑文件最大 1 MB。Agent 每轮默认最多发起 8 次模型请求，可通过 `--max-iterations` 调整；继续提问时重新计算本轮请求次数，避免异常调用无限循环。

每次请求都会估算 system 规则、工具定义、消息历史和预留回复的 token 占用，默认总预算为 100,000，可用 `--context-budget TOKENS` 调整，且必须大于 `--max-tokens`。工具输出在加入历史前按剩余预算裁剪并标记为错误，避免大文件或命令输出持续挤占后续请求；如果连当前请求和预留回复都无法容纳，则在调用 API 前明确失败。估算采用确定性的 UTF-8 启发式计数，不等同于服务端精确 tokenizer。

当较早历史使请求超过预算时，运行时会将最早的完整消息组压缩为本地生成的结构化摘要，保留目标、约束和工具活动概况，并尽量保留最近上下文。工具调用与其整批结果不可拆分，压缩不会执行模型请求或重放工具。交互会话会把压缩后的完整历史作为单独事件写入 JSONL，因此恢复时直接使用相同的紧凑上下文。

`run_command` 使用 shell 在工作区或指定的工作区子目录中运行命令，默认超时 120 秒，单次调用最大可设置为 600 秒；超时或取消后会终止命令进程树。命令标准输入关闭，避免子进程读取对话输入。工具分别捕获退出码、标准输出和标准错误，非零退出码与超时作为错误结果回传模型。命令输出最多返回 100,000 个字符，超出部分明确标记截断。当前工作目录限制不是操作系统沙箱；授权命令前应检查终端显示的完整命令参数。

Agent 通过 `create_file` 或 `replace_text` 修改文件前后，会在本地状态目录保存编辑检查点，并在结果中返回 32 位 checkpoint ID。`list_edit_checkpoints` 可查看当前工作区最近检查点；`restore_edit_checkpoint` 会恢复替换前的原始字节与权限，或删除由 Agent 新建且未再变化的文件。恢复前会比对编辑后 SHA-256；如果文件已被用户、命令或其他进程修改，则报告冲突并拒绝覆盖。检查点只能恢复一次。命令自身造成的文件变化不在检查点范围内。

默认每次模型回复的输出上限为 1024 tokens，单次网络超时为 60 秒。运行时对连接失败、超时、HTTP 429 和可恢复的 5xx 错误默认重试 2 次，初始退避为 0.5 秒并逐次翻倍；可用 `--request-retries 0..10` 和 `--retry-delay SECONDS` 调整。SDK 自身重试保持关闭，身份验证、参数等确定性错误不会重试；流式响应一旦已经显示文本也不会重试，避免重复输出。模型请求重试不包围工具执行，因此文件修改和命令不会被自动重跑。

回答写入标准输出，错误写入标准错误：正常完成退出码为 `0`，请求失败、超过循环上限或输出截断为 `1`，参数或配置错误为 `2`，中断请求为 `130`。发生模型输出截断时保留部分回答，并提示提高 `--max-tokens`。工具失败不会立即终止任务，而是作为带错误标记的结果回传模型，由模型继续处理。

交互会话中任意一轮发生请求失败、循环超限或输出截断，正常退出会话时返回 `1`，即使后续轮次成功。交互中的主动取消不记为请求失败；等待输入时按 Ctrl+C 或中断单次任务，退出码为 `130`。

## 任务进度

模型可通过 `update_tasks` 维护步骤（唯一 ID、说明、pending/in_progress/completed/blocked 状态），通过 `list_tasks` 查看。交互输入 `/tasks` 可直接查看，无需请求模型。最多 100 步；任务状态独立于对话摘要存入会话日志，恢复后仍保留。单次运行或 `--no-persist` 的任务仅保存在内存。

## 规划模式

`uv run mclaude --plan "分析项目并制定修改计划"` 只提供读取和搜索类工具。运行时也会拦截模型发出的修改、检查点恢复、任务更新和命令调用，即使自定义权限策略允许也不会执行。交互输入 `/plan`、`/execute` 切换模式；恢复会话时由本次启动的 `--plan` 决定模式。

## 生命周期 Hooks

通过 `--hooks path/to/hooks.json` 显式启用受信任的本地程序。配置示例：

```json
{"before_tool": [{"command": ["python", "check.py"], "tools": ["create_file"], "timeout": 10}], "after_tool": []}
```

`tools` 默认为 `["*"]`，匹配工具全名；`command` 是 argv 数组，不经过 shell。Hook 从 stdin 接收 JSON（`event`、`tool_name`、`tool_input`；after 还含 `result`），stdout 返回 JSON：before 可返回 `{"block": true, "reason": "原因"}` 阻止工具，after 可返回 `{"append": "补充说明"}`；空输出等同 `{}`。权限通过后才运行 before；被拒工具不触发 Hook。before 失败时工具不会执行，after 失败则保留已有结果并标记错误，不会撤销或重跑工具。默认超时 10 秒，可设置到 60 秒；输出上限 16 KiB，超时与取消清理进程树。Hook 具有普通本地程序权限，规划模式完全禁用 Hooks。

## 按需技能

技能位于工作区 `.mclaude/skills/<目录>/SKILL.md`。文件以 `---` 包围的元数据开头，要求 `name: lowercase-name` 与单行 `description: 简要说明`（支持普通文本或引号字符串），结束 `---` 后是正文。`--list-skills` 无需凭据即可查看描述和发现错误；模型仅在 `load_skill` 后获得正文。目录或文件符号链接被忽略，元数据最多 8,192 字符、目录最多 100 个技能、正文文件最多 100,000 字符。

## stdio MCP

使用 `--mcp-config path/to/mcp.json` 显式启用本地 MCP 服务，例如：

```json
{"mcpServers": {"demo": {"command": "python", "args": ["-u", "server.py"], "timeout": 30}}}
```

服务以 argv 启动，在工作区运行；显式配置意味着允许启动这些受信任程序。发现的工具命名为 `mcp_<服务名>_<工具名>`，每次调用经过现有权限询问与 Hooks；服务声明的只读提示不会免除权限检查。规划模式不启动 MCP，切到 `/plan` 时关闭现有连接。退出、超时、通信错误或取消时关闭连接并清理进程；不自动重试外部工具。

实现 [MCP 2025-06-18 stdio 生命周期](https://modelcontextprotocol.io/specification/2025-06-18/basic/lifecycle)、`tools/list` 分页和 `tools/call`；允许协商 2025-03-26、2024-11-05。只支持工具与文本/结构化 JSON 结果，非文本内容标记省略；不支持 HTTP、资源、提示模板、sampling 或 elicitation。服务名限 20 字符、工具名限 35 字符，仅字母、数字、下划线和连字符；最多 10 个服务、100 个工具。单条消息 1 MB，工具文本最多 100,000 字符，超出会截断并标错。Python 服务需使用 UTF-8 标准输入输出。

## 只读子 Agent

`delegate_readonly` 接收独立调查提示和可选 `max_iterations`（1–4），创建全新历史，仅允许 `read_file`、`find_files`、`search_text`。父对话、任务状态、Hooks 和 MCP 连接不传入子 Agent；子 Agent 仍加载工作区项目规则。父 Agent 只收到最多 16,000 字符的最终报告。禁止递归委派，写入和命令即使被模型请求也会在运行时拒绝。

`--subagent-budget` 控制每轮所有委派合计的模型请求预算，默认 8、范围 0–32；设为 0 禁用委派。单个子 Agent 最多 4 次请求，失败与预算耗尽作为工具错误返回父 Agent。预算按逻辑模型请求计数，已有 API 重试策略仍适用。Ctrl+C 取消会向父轮次传播，不遗留后台子 Agent。

## 只读并发

连续的 `read_file`、`find_files`、`search_text` 调用可并发执行，默认 4 个工作线程；`--read-workers 1` 切回串行，最多可设 16。权限判断和会话写入始终位于主线程，结果按 tool call ID 关联，在当前历史中按调用顺序排列。写入、命令、任务状态修改、技能加载、子 Agent 和 MCP 调用形成顺序边界，不会跨越它们并发；启用 Hooks 后所有工具串行执行。取消时停止排队任务、通知搜索线程退出、保存已完成结果，并为剩余调用补齐错误。

## 开发检查

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Python 包位于 `src/mclaude`，测试位于 `tests`。运行时使用 Anthropic 官方 Python SDK。自动化测试使用模拟 HTTP 传输与可替换模型响应，验证真实 SDK 的请求、消息历史、多个工具调用、搜索范围与上限、错误回传、工作区边界和终止行为，不需要 API 凭据，也不会产生模型调用费用。真实 API 验收需要本地配置后执行上述提问命令。

API 接入参考 [Anthropic Python SDK 文档](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python)。

实施顺序见 [实施计划](docs/implementation-plan.md)，完成情况见 [实施记录](docs/progess.md)。每个新增机制独立提交，并附带对应的验证和说明。
