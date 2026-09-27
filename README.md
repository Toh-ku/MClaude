# MClaude

使用 Python 逐步实现的命令行编程 Agent，使用 uv 管理 Python、依赖和项目命令。

当前已实现最小 Agent 闭环：模型可以调用 `read_file` 读取当前工作目录内的 UTF-8 文本文件，将结果纳入消息历史后继续回答。

## 快速开始

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，然后在项目根目录执行：

```powershell
uv sync --locked
uv run mclaude
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

也可以复制 `.env.example` 为本地 `.env`，填写配置后显式加载：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写 API 密钥与模型 ID
uv run --env-file .env mclaude "你好"
```

程序不会自动读取 `.env`。该文件已被 Git 忽略，勿提交真实密钥。可选的 `ANTHROPIC_BASE_URL` 由 SDK 读取，用于指定兼容 Anthropic Messages API 的服务；默认连接 Anthropic 官方 API。

每次运行处理一个任务。单次运行期间会保留模型回复、工具调用和工具结果，当前仅提供只读的 `read_file` 工具；路径解析后必须位于启动命令时的当前工作目录内。单个文件最多向模型返回 100,000 个字符，超出部分会明确标记截断。Agent 默认最多发起 8 次模型请求，可通过 `--max-iterations` 调整，避免异常调用无限循环。

默认每次模型回复的输出上限为 1024 tokens，网络超时为 60 秒，当前关闭 SDK 自动重试。回答写入标准输出，错误写入标准错误：正常完成退出码为 `0`，请求失败、超过循环上限或输出截断为 `1`，参数或配置错误为 `2`，中断请求为 `130`。发生模型输出截断时保留部分回答，并提示提高 `--max-tokens`。工具失败不会立即终止任务，而是作为带错误标记的结果回传模型，由模型继续处理。

## 开发检查

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Python 包位于 `src/mclaude`，测试位于 `tests`。运行时使用 Anthropic 官方 Python SDK。自动化测试使用模拟 HTTP 传输与可替换模型响应，验证真实 SDK 的请求、消息历史、多个工具调用、错误回传、工作区边界和终止行为，不需要 API 凭据，也不会产生模型调用费用。真实 API 验收需要本地配置后执行上述提问命令。

API 接入参考 [Anthropic Python SDK 文档](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python)。

实施顺序见 [实施计划](docs/implementation-plan.md)，完成情况见 [实施记录](docs/progress.md)。每个新增机制独立提交，并附带对应的验证和说明。
