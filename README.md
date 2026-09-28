# MClaude

[![CI](https://github.com/Toh-ku/MClaude/actions/workflows/ci.yml/badge.svg)](https://github.com/Toh-ku/MClaude/actions/workflows/ci.yml)

MClaude 是一个本地优先的 Python 命令行编程 Agent。它通过 Anthropic Messages API
完成推理，在工作区内读取、搜索、修改文件并运行开发命令；所有有副作用的工具调用都会先请求用户确认。

## 主要能力

- 流式输出、请求重试、当前轮取消，以及可恢复的交互式会话
- 工作区文件读取、发现、搜索、精确替换和新建文件
- 命令执行、编辑检查点与冲突检测
- 只读规划模式、任务跟踪和自动上下文压缩
- 分层加载 `AGENTS.md` / `CLAUDE.md` 项目规则
- 按需技能、生命周期 Hooks、stdio MCP 和只读子 Agent
- 只读工具并发调度，以及交互终端状态页

## 快速开始

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```powershell
git clone https://github.com/Toh-ku/MClaude.git
Set-Location MClaude
uv sync --locked
Copy-Item .env.example .env
```

编辑 `.env`，填入你有权使用的 API 密钥和模型 ID：

```dotenv
ANTHROPIC_API_KEY=your-api-key
ANTHROPIC_MODEL=your-model-id
```

然后运行一个任务：

```powershell
uv run mclaude "读取这个项目并总结其结构"
```

`ANTHROPIC_MODEL` 没有内置默认值，必须通过环境变量或 `--model` 显式指定。
`.env` 只在本地使用并已被 Git 忽略；不要提交真实密钥。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 单次任务 | `uv run mclaude "你的任务"` |
| 交互会话 | `uv run mclaude` 或 `uv run mclaude -i` |
| 恢复最近会话 | `uv run mclaude --continue` |
| 恢复指定会话 | `uv run mclaude --resume SESSION_ID` |
| 只读规划 | `uv run mclaude --plan "分析并制定计划"` |
| 临时、不持久化的会话 | `uv run mclaude -i --no-persist` |
| 查看项目规则来源 | `uv run mclaude --show-instructions` |
| 查看可用技能 | `uv run mclaude --list-skills` |
| 查看完整参数 | `uv run mclaude --help` |

交互模式中可使用：

- `/tasks`：查看当前任务板
- `/plan`：切换到只读规划模式
- `/execute`：切回执行模式
- `/exit` 或 `/quit`：退出
- `Ctrl+C`：执行期间取消当前轮；等待输入时退出程序

模型正文写入标准输出，状态与错误写入标准错误，方便重定向和脚本处理。

## 配置

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | 是 | Anthropic API 密钥 |
| `ANTHROPIC_MODEL` | 是 | 模型 ID，可被 `--model` 覆盖 |
| `ANTHROPIC_BASE_URL` | 否 | Anthropic Messages API 兼容端点 |
| `MCLAUDE_STATE_DIR` | 否 | 会话和编辑检查点的本地状态目录 |

常用运行参数包括 `--max-tokens`、`--timeout`、`--request-retries`、
`--max-iterations`、`--context-budget`、`--read-workers` 和
`--subagent-budget`。以 `uv run mclaude --help` 的输出为准。

### 会话

无参数启动或传入 `-i` 会进入交互模式，并默认持久化会话。Windows 的默认状态目录为
`%LOCALAPPDATA%\MClaude`；Linux/macOS 使用 `$XDG_STATE_HOME/mclaude` 或
`~/.local/state/mclaude`。日志可能包含对话、源码片段和命令输出，但不保存 API 密钥。

恢复会话只会重建消息历史，不会重放历史工具调用。若进程在工具运行中异常退出，恢复时会标记结果未知，要求先检查工作区。

### 项目规则与技能

MClaude 从工作区根目录到当前目录加载适用的 `AGENTS.md` 和 `CLAUDE.md`；同一目录中
`CLAUDE.md` 后加载。技能放在 `.mclaude/skills/<name>/SKILL.md`，只在模型调用
`load_skill` 后载入正文。

### Hooks 与 MCP

Hooks 和 MCP 服务只会在显式传入配置时启用：

```powershell
uv run mclaude --hooks path/to/hooks.json
uv run mclaude --mcp-config path/to/mcp.json
```

Hooks 是受信任的本地程序；MCP 当前支持 stdio 传输与工具调用。规划模式不会运行
Hooks 或启动 MCP 服务。配置格式和所有限制可通过源码及测试中的示例确认：
`src/mclaude/hooks.py`、`src/mclaude/mcp.py`、`tests/test_hooks.py` 和
`tests/test_mcp.py`。

## 安全边界

- 文件工具只能访问启动时工作区内的路径，并跳过工作区外的符号链接。
- 读取类工具默认允许；写文件、恢复检查点和运行命令会逐次询问。
- `create_file` 不覆盖已有文件；`replace_text` 要求旧文本精确且唯一。
- 写入前后会保存编辑检查点；文件后来发生变化时拒绝强制恢复。
- `--plan` 在运行时阻止写入、命令、任务修改、Hooks 和 MCP。
- `run_command` 的目录受限于工作区，但它不是操作系统沙箱；授权前请检查完整命令。

## 开发

源码位于 `src/mclaude`，测试位于 `tests`。测试使用模拟传输，不需要 API 密钥，也不会产生模型调用费用。

```powershell
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv build
```

GitHub Actions 会在推送到 `main` 和 Pull Request 上执行同一组检查。
