# MClaude

使用 Python 逐步实现的命令行编程 Agent，使用 uv 管理 Python、依赖和项目命令。

当前已完成项目初始化，提供命令行帮助和版本查询。模型 API 与 Agent 循环将在后续步骤实现。

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

## 开发检查

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
```

Python 包位于 `src/mclaude`，测试位于 `tests`。当前没有运行时第三方依赖，也不需要 API 凭据。后续接入模型时，通过本地配置提供凭据，避免写入仓库。

实施顺序见 [实施计划](docs/implementation-plan.md)，完成情况见 [实施记录](docs/progress.md)。每个新增机制独立提交，并附带对应的验证和说明。
