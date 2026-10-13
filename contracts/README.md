# 共用协议与兼容样例

`schemas/tools.json` 保存现有公开工具定义。TypeScript 的工具注册与输入校验使用同一份定义；构建时拷贝到 npm 包内部，运行时不需要 Python。

`config.json`、`checkpoint.json`、`tasks.json` 和 `session-event.json` 描述持久化字段；会话使用 version 1 JSONL，事件依次包含 `seq`、UTC `timestamp` 和事件字段。事件包括 `session.created`、`message.appended`、`tool.result`、`turn.finished`、`tasks.updated`、`context.compacted` 和 `session.named`。工具结果必须与调用 ID 配对；恢复中结果未知的工具不能自动重放。

`fixtures/conversation.json` 是两种语言共用的中文、emoji、CRLF 和工具交换样例。它只包含虚构凭据。

工具定义可以从现有 Python API 导出，独立 TS 演进也可以直接维护协议文件：

```powershell
uv run --project python python python/scripts/export_contracts.py
```

双向兼容验证需要先构建 TS，在仓库根目录运行：

```powershell
npm --prefix ts run build
uv run --project python python python/scripts/verify_ts_compatibility.py
```

验证会创建临时状态目录，分别检查 Python → TS 和 TS → Python 的配置、会话、任务、检查点，并测试跨进程互斥锁。它不读取真实用户凭据或请求模型。
