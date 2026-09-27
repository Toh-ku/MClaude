# 实施记录

## 01 — 初始化 Python CLI

- 日期：2026-09-27
- 初始化 Git 仓库，使用 `src` 包布局与 Hatchling 构建后端。
- 使用 uv 管理 Python 3.12、虚拟环境与依赖锁文件。
- 添加 `mclaude` 和 `python -m mclaude` 入口，支持帮助和版本查询；无参数运行展示帮助。
- 添加 Ruff 格式和静态检查配置，以及 CLI 冒烟测试。
- 添加 README、忽略规则和文本换行约定。
- 验证环境：Windows、uv 0.11.16、CPython 3.12.13。
- 验证结果：`uv sync` 成功；`uv run mclaude --help` 和 `uv run mclaude --version` 正常；`uv run ruff format --check .`、`uv run ruff check .` 全部通过；`uv run pytest` 的 4 项测试全部通过。

## 02 — 接入模型 API

- 日期：2026-09-27
- 接入 Anthropic 官方 Python SDK 1.8.0，通过 Messages API 发起单次文本请求。
- 添加配置模块：从 `ANTHROPIC_API_KEY` 读取密钥，从 `ANTHROPIC_MODEL` 或 `--model` 读取模型 ID；模型无默认值，命令行配置优先。
- CLI 支持提问、`--max-tokens` 和 `--timeout`；默认输出上限 1024 tokens、网络超时 60 秒。保留无参数帮助与版本查询。
- 区分参数错误、鉴权失败、权限错误、模型或端点不存在、限流、服务异常、连接失败与超时；错误输出不包含原始服务响应。空响应与异常停止不会被视为成功，截断时保留部分回答并返回非零退出码。
- 关闭 SDK 自动重试；重试策略仍留在计划第 15 步实现。
- 添加 `.env.example`、PowerShell 配置示例与退出码说明。`.env` 通过 `uv run --env-file .env` 显式加载。
- 验证结果：`uv run ruff format --check .`、`uv run ruff check .` 全部通过；`uv run pytest` 的 34 项测试全部通过。使用模拟 HTTP 传输验证真实 SDK 的请求格式、中文输入、文本块拼接、配置优先级、错误处理和截断行为。
- 验收限制：当前环境没有配置 `ANTHROPIC_API_KEY` 与 `ANTHROPIC_MODEL`，未执行真实模型调用；需按 README 本地配置后完成真实 API 验收。

下一步：03 `feat: add minimal agent loop`，引入消息历史、工具调用与结果回传，首先提供受工作目录边界限制的 `read_file` 工具。
