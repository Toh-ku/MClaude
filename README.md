# MClaude

[![CI](https://github.com/Toh-ku/MClaude/actions/workflows/ci.yml/badge.svg)](https://github.com/Toh-ku/MClaude/actions/workflows/ci.yml)

MClaude 是本地优先的命令行编程 Agent，通过 Anthropic Messages API 推理，在工作区内读取、搜索和修改文件。项目提供可独立安装的 TypeScript 与 Python 实现。

| 实现 | 目录 | 环境 | 使用指南 |
| --- | --- | --- | --- |
| TypeScript | `ts/` | Node.js 24 LTS、npm | [TS 文档](ts/README.md) |
| Python | `python/` | Python 3.12+、uv | [Python 文档](python/README.md) |

## TypeScript 快速开始

```powershell
git clone https://github.com/Toh-ku/MClaude.git
Set-Location MClaude/ts
npm ci
npm run build
node dist/cli/main.js login
node dist/cli/main.js "读取这个项目并总结其结构"
```

工作区始终是启动时的当前目录。从目标项目目录使用 CLI 的绝对路径，或先在 `ts/` 运行 `npm link`，再在目标项目中执行 `mclaude`。

```powershell
Set-Location D:/your/project
node D:/Code/MClaude/ts/dist/cli/main.js
```

首次运行缺少配置时，真实交互终端会引导配置隐藏 API Key、模型 ID 和 API 地址。自动化环境使用 `ANTHROPIC_API_KEY`、`ANTHROPIC_MODEL`、`ANTHROPIC_BASE_URL`；不弹出登录输入框。模型 ID 没有内置默认值。

## Python 快速开始

```powershell
Set-Location MClaude/python
uv sync --locked
uv run mclaude
```

Python 的源码、依赖、锁文件和测试已放入 `python/`；从仓库根目录也可使用 `uv run --project python mclaude`。两种实现提供同名命令，开发时用明确入口选择实现。

## 能力

- 流式输出、有限重试、取消当前轮、交互式会话与恢复
- 工作区文件读取、Git 风格发现、文本/正则搜索，遵守嵌套 `.gitignore`
- 精确替换、创建和批量编辑，编辑检查点与冲突检测
- 只读 Git 审阅，顺序开发检查、失败修复与复测
- 规划模式、任务跟踪、本地上下文压缩与分层项目规则
- 按需技能、生命周期 Hooks、stdio/Streamable HTTP MCP、只读子 Agent
- 有限并发读取、顺序修改屏障与终端仪表盘

正文输出到 stdout，状态和错误输出到 stderr。`--plain` 关闭仪表盘；重定向时自动使用普通输出。运行 `mclaude --help` 查看完整参数。

## 权限与持久化

已知读取工具默认允许；修改文件、恢复检查点、外部 MCP 工具和宿主机命令逐次确认。命令工具需先传入 `--allow-host-commands`，它在宿主机执行，并非操作系统沙箱。规划模式同时限制工具定义和执行入口。

文件工具检查真实路径及工作区边界。批量编辑先校验所有文件，再创建检查点和提交改动；遇到外部修改冲突时拒绝覆盖。会话恢复重建消息历史，不重放历史工具调用。

两种实现沿用用户级配置、v1 JSONL 会话和检查点格式，使用相同工作区哈希与互斥锁。`MCLAUDE_CONFIG_DIR` 与 `MCLAUDE_STATE_DIR` 可覆盖目录。配置包含明文 API Key，请勿提交；POSIX 下新配置目录为 0700，文件为 0600。

TS 采用独立异步设计，可以按后续审核结果演进。行为差异和可审核选项见 [TS 设计决策](ts/DESIGN.md)，原始模块方案见 [重构方案](TS_REFACTOR_PLAN.md)。

## 开发

```powershell
# 从 ts/ 运行
npm ci
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build

# 从 python/ 运行
uv sync --locked --dev
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv build
```

测试使用模拟模型和本地临时服务，不需要真实 API 凭据。[contracts/](contracts/README.md) 提供协议与样例；先构建 TS，再从根目录运行双向数据/锁验证：

```powershell
uv run --project python python python/scripts/verify_ts_compatibility.py
```

GitHub Actions 分别检查两种实现及互操作性，覆盖 Linux、Windows 和 macOS。

官网仍位于 [website/](website/README.md)。在根目录运行 `node website/serve.mjs`，打开 `http://127.0.0.1:4173` 本地预览。
