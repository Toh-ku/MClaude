# MClaude

使用 Python 逐步实现的命令行编程 Agent，使用 uv 管理 Python、依赖和项目命令。

当前支持通过 Anthropic Messages API 发起单次文本请求，以及命令行帮助和版本查询。Agent 循环与工具调用将在下一步实现。

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

## 向模型提问

在 PowerShell 中配置 API 密钥和你有权使用的模型 ID：

```powershell
$env:ANTHROPIC_API_KEY = "你的 API 密钥"
$env:ANTHROPIC_MODEL = "你的模型 ID"
uv run mclaude "请用一句话解释什么是编程 Agent"
```

模型 ID 必须显式配置，不内置默认模型。命令行参数优先于 `ANTHROPIC_MODEL`：

```powershell
uv run mclaude "解释 Python 的生成器" --model "你的模型 ID" --max-tokens 2048 --timeout 90
```

也可以复制 `.env.example` 为本地 `.env`，填写配置后显式加载：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写 API 密钥与模型 ID
uv run --env-file .env mclaude "你好"
```

程序不会自动读取 `.env`。该文件已被 Git 忽略，勿提交真实密钥。可选的 `ANTHROPIC_BASE_URL` 由 SDK 读取，用于指定兼容 Anthropic Messages API 的服务；默认连接 Anthropic 官方 API。

每次运行发送一条用户消息，不保留会话历史。默认输出上限为 1024 tokens，网络超时为 60 秒，当前关闭 SDK 自动重试。回答写入标准输出，错误写入标准错误：正常完成退出码为 `0`，请求失败或输出截断为 `1`，参数或配置错误为 `2`，中断请求为 `130`。发生输出截断时保留部分回答，并提示提高 `--max-tokens`。

## 开发检查

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Python 包位于 `src/mclaude`，测试位于 `tests`。运行时使用 Anthropic 官方 Python SDK。自动化测试使用模拟 HTTP 传输验证真实 SDK 的请求和响应处理，不需要 API 凭据，也不会产生模型调用费用。真实 API 验收需要本地配置后执行上述提问命令。

API 接入参考 [Anthropic Python SDK 文档](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python)。

实施顺序见 [实施计划](docs/implementation-plan.md)，完成情况见 [实施记录](docs/progress.md)。每个新增机制独立提交，并附带对应的验证和说明。
