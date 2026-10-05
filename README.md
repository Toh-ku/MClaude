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
- 按需技能、生命周期 Hooks、stdio/远程 HTTP MCP 和只读子 Agent
- 只读工具并发调度，以及实时刷新的交互式终端仪表盘

## 快速开始

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```powershell
git clone https://github.com/Toh-ku/MClaude.git
Set-Location MClaude
uv sync --locked
```

直接启动：

```powershell
uv run mclaude
```

首次在交互终端运行且缺少 API Key 或模型配置时，程序会引导你输入 API Key
（隐藏输入）、模型 ID 和可选 API 地址，保存后继续当前任务。API 地址留空使用官方 API。
配置保存在用户目录中，切换项目无需重复配置。

也可以先登录，再运行一个任务：

```powershell
uv run mclaude login
uv run mclaude "读取这个项目并总结其结构"
```

模型没有内置默认值，需要在登录时填写，或通过环境变量、`--model` 指定。
这里的登录是本地 API 凭据配置；保存时不请求 API，凭据有效性在首次模型调用时验证。

## 常用命令

| 目的 | 命令 |
| --- | --- |
| 登录 / 重新配置 | `uv run mclaude login` |
| 登出 | `uv run mclaude logout` |
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
在支持 ANSI 的交互终端中，仪表盘会固定展示会话、上下文、Token 用量、任务和
最近活动，模型输出在下方独立滚动；退出后会恢复原终端画面。使用 `--plain` 可关闭
仪表盘，重定向输入或输出时也会自动回退为普通文本模式。

## 配置

登录配置文件的位置：

- Windows：`%LOCALAPPDATA%\MClaude\config.json`
- Linux/macOS：`$XDG_CONFIG_HOME/mclaude/config.json` 或 `~/.config/mclaude/config.json`
- 可通过 `MCLAUDE_CONFIG_DIR` 指定配置目录，与 `MCLAUDE_STATE_DIR` 独立。

API Key 以明文保存在用户级配置文件中，请勿分享或提交此文件。POSIX 系统上新建配置目录
权限为 `0700`，配置文件权限为 `0600`。登录中按 `Ctrl+C` 会取消，保留已有配置。
`login --model MODEL --base-url URL` 可设置输入提示的默认值；API Key 始终隐藏输入。

`logout` 删除保存的登录配置，保留会话和编辑检查点。环境变量属于独立配置来源，
登出不会删除或修改它们；如已设置，它们仍可用于运行。

配置覆盖顺序为：`--model` > 环境变量 > 保存的登录配置。
API Key 和模型的空环境变量视为未配置；空的 `ANTHROPIC_BASE_URL` 会选择官方 API。
自动化环境不弹出登录输入框，缺少配置时会提示先运行 `mclaude login` 或设置环境变量。

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | 否 | 覆盖已保存的 API 密钥；无登录配置时必需 |
| `ANTHROPIC_MODEL` | 否 | 覆盖已保存的模型 ID，可被 `--model` 覆盖 |
| `ANTHROPIC_BASE_URL` | 否 | Anthropic Messages API 兼容端点 |
| `MCLAUDE_CONFIG_DIR` | 否 | 用户登录配置目录 |
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

Hooks 是受信任的本地程序；MCP 支持本地 stdio 和远程 Streamable HTTP 工具服务。
例如 `mcp.json` 可以同时配置两种服务：

```json
{
  "mcpServers": {
    "local": {"command": "python", "args": ["server.py"]},
    "remote": {
      "url": "https://mcp.example.com/mcp",
      "headers": {"Authorization": "Bearer ${MCP_TOKEN}"},
      "timeout": 30
    }
  }
}
```

远程服务使用 Streamable HTTP，支持 JSON 与 SSE 响应、会话 ID 和协议版本头。
`headers` 中的 `${变量名}` 从环境变量读取；令牌不要直接写入配置文件。
建议远程地址使用 HTTPS。当前不支持旧版 HTTP+SSE 传输或 OAuth 自动登录。
规划模式不会运行 Hooks 或连接 MCP 服务。配置格式和所有限制可通过源码及测试中的示例确认：
`src/mclaude/hooks.py`、`src/mclaude/mcp.py`、`tests/test_hooks.py` 和
`tests/test_mcp.py`。

## 安全边界

- 文件工具只能访问启动时工作区内的路径，并跳过工作区外的符号链接。
- 读取类工具默认允许；写文件、恢复检查点和运行命令会逐次询问。
- `create_file` 不覆盖已有文件；`replace_text` 要求旧文本精确且唯一。
- 写入前后会保存编辑检查点；文件后来发生变化时拒绝强制恢复。
- `--plan` 在运行时阻止写入、命令、任务修改、Hooks 和 MCP。
- `run_command` 的目录受限于工作区，但它不是操作系统沙箱；授权前请检查完整命令。

## 项目官网

官网源码位于 [`website/`](website/README.md)，无需安装前端依赖。
运行 `node website/serve.mjs` 后打开 `http://127.0.0.1:4173` 预览。

## 开发与测试

源码位于 `src/mclaude`，测试位于 `tests`。测试使用模拟传输，不需要 API 密钥，也不会产生模型调用费用。

```powershell
uv sync --locked
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv build
```

GitHub Actions 会在推送到 `main` 和 Pull Request 上执行同一组检查。
