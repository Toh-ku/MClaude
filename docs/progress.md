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

下一步：02 `feat: add model client`，接入模型 API、凭据与模型配置、单次文本请求。
