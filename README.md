# MClaude

使用 Python 逐步实现的命令行编程 Agent，使用 uv 管理 Python、依赖和项目命令。

当前已实现 Agent 工具闭环：模型可以读取、发现、搜索和精确修改工作区文件，再将工具结果纳入消息历史后继续回答。所有工具调用在执行前统一经过权限判断。

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

也可以复制 `.env.example` 为本地 `.env`，程序启动时会自动加载：

```powershell
Copy-Item .env.example .env
# 编辑 .env，填写 API 密钥与模型 ID
uv run mclaude "你好"
```

程序读取当前工作目录下的 `.env`，但不会覆盖已有的系统环境变量。该文件已被 Git 忽略，勿提交真实密钥。可选的 `ANTHROPIC_BASE_URL` 由 SDK 读取，用于指定兼容 Anthropic Messages API 的服务；默认连接 Anthropic 官方 API。

每次运行处理一个任务。单次运行期间会保留模型回复、工具调用和工具结果。当前提供只读的 `read_file`、`find_files`、`search_text`，以及修改文件的 `create_file` 和 `replace_text`。每个调用执行前都会经过统一权限入口；已知只读工具自动允许，文件修改工具逐次询问，未知工具拒绝。权限询问不可用、策略异常或响应无效时默认拒绝。拒绝的工具不会执行，拒绝原因会作为错误工具结果回传模型。

所有路径解析后必须位于启动命令时的当前工作目录内；文件发现与文本搜索跳过 `.git` 和工作区外的符号链接，并限制结果数量、文件大小与单行长度。`create_file` 只创建新文件，不会覆盖已有路径；`replace_text` 要求旧文本精确且唯一，文本缺失、匹配歧义或写入前文件变化时拒绝覆盖。成功修改会向模型返回 unified diff。单个文件读取最多返回 100,000 个字符，单个待编辑文件最大 1 MB。Agent 默认最多发起 8 次模型请求，可通过 `--max-iterations` 调整，避免异常调用无限循环。

默认每次模型回复的输出上限为 1024 tokens，网络超时为 60 秒，当前关闭 SDK 自动重试。回答写入标准输出，错误写入标准错误：正常完成退出码为 `0`，请求失败、超过循环上限或输出截断为 `1`，参数或配置错误为 `2`，中断请求为 `130`。发生模型输出截断时保留部分回答，并提示提高 `--max-tokens`。工具失败不会立即终止任务，而是作为带错误标记的结果回传模型，由模型继续处理。

## 开发检查

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Python 包位于 `src/mclaude`，测试位于 `tests`。运行时使用 Anthropic 官方 Python SDK。自动化测试使用模拟 HTTP 传输与可替换模型响应，验证真实 SDK 的请求、消息历史、多个工具调用、搜索范围与上限、错误回传、工作区边界和终止行为，不需要 API 凭据，也不会产生模型调用费用。真实 API 验收需要本地配置后执行上述提问命令。

API 接入参考 [Anthropic Python SDK 文档](https://platform.claude.com/docs/en/cli-sdks-libraries/sdks/python)。

实施顺序见 [实施计划](docs/implementation-plan.md)，完成情况见 [实施记录](docs/progress.md)。每个新增机制独立提交，并附带对应的验证和说明。
