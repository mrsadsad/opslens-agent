# 只读 MCP stdio 服务

OpsLens 可以将单个证据快照作为 MCP 工具服务交给外部客户端。它复用 `IncidentTools` 的读取和校验逻辑，只提供调查数据，不调用模型，也不替外部客户端执行诊断流程。

```powershell
python -m opslens mcp --case data/cases/inc001.json
```

这是等待客户端协议输入的服务，不会打印交互式菜单。客户端应使用 Python 的绝对可执行路径，并将参数设置为 `-m opslens mcp --case <快照绝对路径>`；工作目录设为仓库根目录，或先安装本项目。启动本机实验生成的 `snapshot.json` 同样适用。

Python 入口为 `serve_stdio(case_path: Path, input_stream=None, output_stream=None)`。默认直接使用二进制 stdin/stdout，以 UTF-8 编解码，避免 Windows 控制台字符集影响协议。关闭 stdin 后服务正常退出。

## 协议范围

实现参考官方 [2025-11-25 生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle)、[stdio 传输](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)及[工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)，支持以下子集：

- `initialize`：交换协议版本、能力和实现信息。
- `notifications/initialized`：客户端确认后才开放工具请求。
- `ping`：初始化期间及之后均可调用。
- `tools/list`：返回固定工具目录，一页返回全部工具。
- `tools/call`：调用一个只读工具。

支持的版本是 `2025-11-25`、`2025-06-18`。客户端请求支持的版本时原样回传；请求其他版本时返回本服务支持的最新版本 `2025-11-25`，由客户端判断是否继续。旧版本使用相同 stdio 及 structured-content 子集，见官方 [2025-06-18 tools](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)。没有声称获得官方认证或兼容全部 MCP 客户端。

服务只声明 `tools` 能力，`listChanged: false`。未实现 HTTP 传输、resources、prompts、sampling、elicitation、异步 tasks 或远程认证；客户端应仅使用已协商的能力。

## 工具及边界

固定白名单为 `list_services`、`query_metrics`、`search_logs`、`get_changes`、`search_runbooks`、`get_evidence`、`get_trace`。即使内部以后新增诊断/报告助手，也不会自动暴露为 MCP 工具。MCP `inputSchema` 直接来自这些工具已有参数定义；不接受任意用户传入的 schema。

每个工具标注：

```json
{"readOnlyHint": true, "destructiveHint": false, "idempotentHint": true, "openWorldHint": false}
```

调用结果同时返回 `content: [{"type":"text","text":"<JSON>"}]` 与 `structuredContent`；后者就是工具原始 `{ok, records, error?}` 对象，`isError` 等于 `!ok`。日志中的指令只会作为文本返回，不会触发 shell、文件读取、网络请求或新的工具调用。初始化只打开指定快照，MCP 请求不能切换文件，也无法读取标签、provenance、环境变量或任意路径。

## 错误和资源约束

每条消息必须是单行 UTF-8 JSON-RPC，以换行结尾；JSON 字符串中的换行使用转义。stdout 只输出协议消息，内部故障仅向 stderr 写固定诊断文字，不回显原始异常、路径或请求内容。通知没有响应，也不会通过通知形式执行工具。

| 情况 | 返回 |
| --- | --- |
| 非法 JSON、非法 UTF-8、重复 JSON 键 | `-32700` |
| 非法请求/ID、batch、重复请求 ID | `-32600` |
| 不支持的方法 | `-32601` |
| 非法协议参数、未知工具、非对象 `arguments` | `-32602` |
| 初始化未完成、会话请求数用尽 | `-32000` |
| 已知工具的参数边界、未知服务等执行校验失败 | 工具结果 `isError: true` |

工具输入校验失败与协议报文错误分开，以便客户端将可修正的工具错误交回模型。该区分遵循 2025-11-25 工具错误规则。

单行最大 1 MiB，包含终止换行，按 UTF-8 字节计。超过限制或 EOF 前收到未换行的残缺帧时，输出协议错误后退出；其余 JSON 解析错误不会污染下一行。每会话最多 4096 个请求，字符串请求 ID 最长 1024 字符。工具调用使用令牌桶限制为持续 60 次/秒、突发 120 次；超额返回工具错误。证据条数和文本长度继续遵守 `IncidentTools` 的上限。

`serve_stdio` 在正常 EOF/客户端断开时返回 `0`；启动失败、致命帧错误或传输失败时返回 `2`。服务不打印原始输入，客户端自身仍应限制读取和调用时间。

## 验证

```powershell
python -m unittest tests.test_mcp -v
```

测试通过真实 Python 子进程的 stdin/stdout 完成握手、列工具、调用工具及版本协商；还覆盖 UTF-8（主动设置宿主默认输出编码为 ASCII）、初始化屏障、未知工具、schema 参数错误、注入日志、batch/超大帧和错误信息脱敏。这是项目自身的协议集成测试，不是第三方客户端认证。
