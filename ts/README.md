# MClaude / TypeScript

独立的 TypeScript CLI 编程 Agent，要求 Node.js 24 LTS。Python 实现位于相邻的 `python/`，TS 运行时不调用 Python。

## 安装与启动

从此目录运行：

```powershell
npm ci
npm run build
node dist/cli/main.js login
node dist/cli/main.js
```

当前目录即工作区。从目标项目中使用编译入口的绝对路径，或先在此目录 `npm link`，然后在目标项目中运行 `mclaude`。

```powershell
mclaude "总结项目结构"
mclaude --plan "分析并制定计划"
mclaude --allow-host-commands "运行测试并修复失败"
mclaude --continue
mclaude --resume SESSION_ID
mclaude sessions list
mclaude sessions search "关键词"
mclaude sessions rename SESSION_ID "名称"
mclaude --list-skills
mclaude --show-instructions
mclaude --help
```

无参数时在真实终端进入交互模式，默认保存会话；`-i` 可显式启用，`--no-persist` 可关闭保存。交互命令有 `/tasks`、`/plan`、`/execute`、`/exit`、`/quit`。Ctrl+C 在执行中取消当前轮，等待输入时退出；`--plain` 关闭仪表盘。

## 配置

配置优先级：CLI `--model` > 环境变量 > 保存的登录设置。API Key 和模型的空环境变量视为未配置；空 `ANTHROPIC_BASE_URL` 明确选择官方 API。API Key 隐藏输入，登录保存时不请求网络，模型 ID 需要填写。

| 环境变量 | 作用 |
| --- | --- |
| `ANTHROPIC_API_KEY` | API Key |
| `ANTHROPIC_MODEL` | 模型 ID |
| `ANTHROPIC_BASE_URL` | Messages API 兼容端点 |
| `MCLAUDE_CONFIG_DIR` | 配置目录 |
| `MCLAUDE_STATE_DIR` | 会话与检查点目录 |

Windows 默认用户目录为 `%LOCALAPPDATA%/MClaude`。Linux/macOS 配置使用 `$XDG_CONFIG_HOME/mclaude` 或 `~/.config/mclaude`，状态使用 `$XDG_STATE_HOME/mclaude` 或 `~/.local/state/mclaude`。配置保存明文凭据，不应提交或分享。

`logout` 删除保存的登录设置，保留会话、检查点和独立环境变量。历史列表和搜索不需要模型凭据。

## 工具与扩展

文件工具只访问工作区内真实路径。新建不能覆盖已有文件；替换需要唯一精确匹配；一次批量编辑最多 20 个不同文件。检查点保存原始字节，恢复遇到外部修改时拒绝覆盖。

`--allow-host-commands` 启用命令工具，每次仍需确认；这是宿主机执行，不提供 OS 沙箱。`run_checks` 顺序运行最多 5 项，失败后 `retest_checks` 重新确认并执行原命令组。检查通过后尝试编辑会保守标记为 stale。

检查结果绑定工作区内容的 SHA-256 指纹，包含可发现文件的路径、权限和原始字节，支持没有 Git 的目录及未跟踪文件；遵守 `.gitignore`，排除 `.git`、Agent 会话、检查点及登录配置。忽略文件不属于验证版本范围。检查前后源码必须一致；命令、MCP、Hooks 和外部编辑造成版本变化后，下一次工具结果、模型请求或结束回答前会将旧检查标为 stale。无法读取完整版本时不会宣称通过。验证状态仍只保留在当前进程，恢复会话后需要重新运行检查。

规则从工作区根到当前目录加载 `AGENTS.md`、`CLAUDE.md`，同层后者后加载。技能放在 `.mclaude/skills/<name>/SKILL.md`，只有显式 `load_skill` 才加载正文。

读取、搜索和编辑会按目标文件或目录加载适用规则；新建文件沿已有父目录查找，批量编辑覆盖每个文件，检查点恢复覆盖被恢复的文件。嵌套规则只约束对应子树，兄弟目录的规则互不覆盖。编辑发现尚未提供给模型或已经改变的规则时，会先返回规则而不修改文件，下一次模型请求加载完整规则后才能重试；权限确认后也会再次核对。已访问路径的规则在每次模型请求前重新读取。

扩展显式启用：

```powershell
mclaude --hooks hooks.json
mclaude --mcp-config mcp.json
```

Hooks 配置包含 `before_tool`/`after_tool` 数组，每项 `command` 是 argv 数组，可附 `tools` 和 `timeout`。输入输出是 JSON；before hook 失败会阻止工具执行。TS 对 hook 的 stdout/stderr 合计限制为 16 KiB。

MCP 示例：

```json
{
  "mcpServers": {
    "local": { "command": "node", "args": ["server.mjs"] },
    "remote": {
      "url": "https://mcp.example.com/mcp",
      "headers": { "Authorization": "Bearer ${MCP_TOKEN}" },
      "timeout": 30
    }
  }
}
```

MCP 使用官方 v2 客户端的 legacy 协商模式，支持现有 stdio 与 Streamable HTTP JSON/SSE 服务器。外部工具均需确认，调用失败不自动重放。HTTP 禁止自动重定向；第一版不启用 OAuth 和旧 HTTP+SSE 回退。规划模式不连接 MCP、不执行 Hooks。

子 Agent 有独立历史，只开放读取/发现/搜索；默认共享 8 次模型请求预算，不递归。读取调度默认并发 4，写入等其他工具形成顺序屏障；启用 Hooks 后串行执行。

## 开发与验证

```powershell
npm run format
npm run format:check
npm run lint
npm run typecheck
npm test
npm run build
npm pack
node scripts/package-smoke.mjs
```

测试涵盖模型传输、CLI、权限、编辑回滚、恢复、取消、进程、Hooks、MCP 和终端布局。模型使用模拟传输；MCP 和 CLI 使用本地测试服务。真实终端隐藏输入与仪表盘外观仍应在使用的终端中检查。

构建产物包含所需工具协议，npm 包可以脱离此仓库安装。安装验证脚本在临时目录安装生成的包，检查公共导入、CLI 帮助、版本与无凭据历史列表。

架构与兼容差异见 [DESIGN.md](DESIGN.md)。
