# MClaude

使用 Python 逐步实现的命令行编程 Agent，使用 uv 管理 Python、依赖和项目命令。

当前支持流式输出、当前轮取消、可恢复的终端多轮会话和 Agent 工具闭环：模型可以读取、发现、搜索和精确修改工作区文件，也可以运行测试、格式检查等开发命令，再将工具结果纳入消息历史后继续回答。所有工具调用在执行前统一经过权限判断。

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

当前提供只读的 `read_file`、`find_files`、`search_text`，修改文件的 `create_file`、`replace_text`，以及执行开发命令的 `run_command`。每个调用执行前都会经过统一权限入口；已知只读工具自动允许，文件修改和命令执行逐次询问，未知工具拒绝。权限询问不可用、策略异常或响应无效时默认拒绝。拒绝的工具不会执行，拒绝原因会作为错误工具结果回传模型。

所有路径解析后必须位于启动命令时的当前工作目录内；文件发现与文本搜索跳过 `.git` 和工作区外的符号链接，并限制结果数量、文件大小与单行长度。`create_file` 只创建新文件，不会覆盖已有路径；`replace_text` 要求旧文本精确且唯一，文本缺失、匹配歧义或写入前文件变化时拒绝覆盖。成功修改会向模型返回 unified diff。单个文件读取最多返回 100,000 个字符，单个待编辑文件最大 1 MB。Agent 每轮默认最多发起 8 次模型请求，可通过 `--max-iterations` 调整；继续提问时重新计算本轮请求次数，避免异常调用无限循环。

`run_command` 使用 shell 在工作区或指定的工作区子目录中运行命令，默认超时 120 秒，单次调用最大可设置为 600 秒；超时或取消后会终止命令进程树。命令标准输入关闭，避免子进程读取对话输入。工具分别捕获退出码、标准输出和标准错误，非零退出码与超时作为错误结果回传模型。命令输出最多返回 100,000 个字符，超出部分明确标记截断。当前工作目录限制不是操作系统沙箱；授权命令前应检查终端显示的完整命令参数。

默认每次模型回复的输出上限为 1024 tokens，网络超时为 60 秒，当前关闭 SDK 自动重试。回答写入标准输出，错误写入标准错误：正常完成退出码为 `0`，请求失败、超过循环上限或输出截断为 `1`，参数或配置错误为 `2`，中断请求为 `130`。发生模型输出截断时保留部分回答，并提示提高 `--max-tokens`。工具失败不会立即终止任务，而是作为带错误标记的结果回传模型，由模型继续处理。

交互会话中任意一轮发生请求失败、循环超限或输出截断，正常退出会话时返回 `1`，即使后续轮次成功。交互中的主动取消不记为请求失败；等待输入时按 Ctrl+C 或中断单次任务，退出码为 `130`。

## 开发检查

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Python 包位于 `src/mclaude`，测试位于 `tests`。运行时使用 Anthropic 官方 Python SDK。自动化测试使用模拟 HTTP 传输与可替换模型响应，验证真实 SDK 的请求、消息历史、多个工具调用、搜索范围与上限、错误回传、工作区边界和终止行为，不需要 API 凭据，也不会产生模型调用费用。真实 API 验收需要本地配置后执行上述提问命令。

API 接入参考 [Anthropic Python SDK 文档](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python)。

实施顺序见 [实施计划](docs/implementation-plan.md)，完成情况见 [实施记录](docs/progess.md)。每个新增机制独立提交，并附带对应的验证和说明。
