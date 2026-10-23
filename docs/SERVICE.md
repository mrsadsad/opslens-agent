# 本地调查工作台

工作台提供任务创建、逐步轨迹、观测证据、诊断报告和协作取消。它使用 Python 标准库，默认绑定 `127.0.0.1`，只用于本地演示和开发。案例可以是合成快照，也可以是 `source=local_fault_lab` 的真实本机 HTTP 故障采集快照；后者仍是受控实验，不代表生产事故。`scripted-demo` 是固定脚本，不能用于证明模型诊断能力。

## 启动接口

CLI 可调用以下接口。`provider_factory` 由服务端注入，每个任务创建一个 provider；浏览器不能上传密钥、更换服务地址或指定文件路径。

```python
from pathlib import Path
from opslens.providers import DemoProvider
from opslens.server import create_server

with create_server(
    case_paths=[Path("data/cases/inc001.json")],
    db_path=Path("runs/workbench.sqlite3"),
    output_dir=Path("runs"),
    provider_factory=DemoProvider,
    port=8765,
) as server:
    print(f"http://127.0.0.1:{server.server_address[1]}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
```

`port=0` 可让操作系统选择空闲端口。只接受 `127.0.0.1` 或 `localhost`，实际绑定地址始终为 `127.0.0.1`。真实模型模式由 CLI 从本地环境构造 `DeepSeekProvider` 工厂，服务不会把凭证下发给页面。

服务端可将本机实验生成的 snapshot 路径加入 `case_paths` 白名单，配合真实模型 provider 调查；浏览器仍只选择 `case_id`。上面的固定脚本示例仅用于 `inc001` 的 `react` 演示，不适用于其他故障或 `investigator` 策略。

## API

| 方法与路径 | 内容 |
|---|---|
| `GET /` | 无外部 CDN 的中文工作台，支持浅色和深色 |
| `GET /api/config` | 返回案例白名单、容量限制和随机会话 CSRF token |
| `POST /api/runs` | 创建任务；请求包含 `case_id`，可选 `max_steps`、`max_tool_calls`、`policy` |
| `GET /api/runs?limit=50&before=N` | 当前数据库内匹配案例 ID 与内容指纹的任务历史；默认每页 50，最多 200，使用返回的 `next_before` 读取更早记录 |
| `GET /api/runs/{run_id}` | 公开状态、预算、用量、观测证据、报告和 `report_audit` 数值风险 |
| `GET /api/runs/{run_id}/events?after=N` | 返回 `seq > N` 的事件及 `next_cursor` |
| `POST /api/runs/{run_id}/cancel` | 以空对象 `{}` 请求协作取消 |

0.4 会在服务重启后只读展示历史，`historical` 表示任务不属于当前内存执行会话。旧的 queued/running/paused 显示为 interrupted，原始状态保留在 `checkpoint_status`；pending 请求的用量标记为不完整。浏览历史不会构造模型提供商、不会自动恢复执行，也不占新会话的 200 个任务名额。取消历史返回 `409 history_read_only`。不同案例内容、案例白名单外或其他数据库中的记录不可见；这些约束不等于多用户鉴权。旧页面的 CSRF token 过期时需要刷新页面，服务不会自动重复 POST。

写请求必须使用 `Content-Type: application/json`，携带与当前 `Host` 一致的 `Origin` 和 `/api/config` 返回的 `X-CSRF-Token`。创建成功返回 `202` 和 `run_id`；容量已满返回 `429 queue_full`；非白名单案例返回 `404 unknown_case`。错误只有稳定错误码，不包含原始模型响应、密钥或服务器路径。

容量为 2 个执行线程、4 个等待位置；请求正文最多 8 KiB，每次服务会话最多保存 200 个任务条目。默认预算为 12 个模型步骤和 24 次工具调用。API 的预算范围与 runtime 一致。

正文超限时先返回 `413 body_too_large` 和 `Connection: close`，再最多排空 64 KiB、总计最多等待 250 ms，以减少 Windows 上未读正文关闭连接导致的复位。不会按任意 `Content-Length` 无界读取，也不会为了缺失正文等待完整请求超时。

`policy` 可为 `react` 或 `investigator`。省略时，固定脚本演示采用 `react`，其他 provider 采用 `investigator`。工作台的“调查假设”展示模型显式提交的可验证解释、支持证据、反证和下一项检查，不展示隐藏推理。

模型可通过 `list_services` 查看从观测生成的各服务查询能力；对不可用阶段的查询返回 `unsupported_phase` 和可用选项，错误反馈会保留在轨迹中。每次模型调用都能看到剩余调用预算，普通上下文与压缩上下文都包含提交提示。

## 状态与取消语义

任务依次经过 `queued`、`running`，最终成为 `completed`、`budget_exhausted`、`provider_error`、`server_error` 或 `cancelled`。`completed` 表示报告通过结构校验及当前已实现的有限数值规则，不等于诊断或全部数值论证正确。每个任务只有一个执行者；浏览器没有并发恢复入口。运行时每完成一个模型回合便持久化 checkpoint，服务器在回合间检查取消标记。

报告区展示 `report_audit` 的问题清单，命中规则时提示“数值论证需要修正”；没有命中时提示“数值规则未检出已知风险；结论仍需审阅”。规则针对明确变化量、分母和统计口径、p95 相加等已知风险，不是完整语义验证。Agent 可在剩余预算内修正，被拒绝的提案不作为有效报告；修正后的清洁结果不会删除轨迹中的历史拒绝。评测另外用 `numeric_rejection_count` 汇总报告及假设的数值拒绝记录，详见[评测说明](EVALUATION.md)。

等待中的任务可以立即取消并释放队列位置。运行中的网络请求不会被强制终止；点击取消后，当前回合完成，再停止后续回合。若报告在当前回合已经被接受，任务仍为 `completed`。关闭服务会请求取消所有剩余任务，并等待已开始的网络操作按 provider 的超时配置结束。

每个终态任务在输出目录下生成 `{run_id}/result.json` 和 `{run_id}/trace.jsonl`。SQLite checkpoint 保留完整协议上下文，用于 CLI 恢复；工作台 API 和导出文件移除 `messages` 与私密协议字段。页面不展示模型的 `reasoning_content`。服务重新启动后，会话列表重新开始，既有 checkpoint 和导出文件保留。

## 浏览器与本地边界

服务校验 `Host`，阻止 DNS rebinding；写请求校验同源 `Origin`、JSON 类型及随机 CSRF token；跨站读取不开放 CORS。页面采用 nonce CSP，所有工具内容、日志、报告和错误使用 `textContent` 写入 DOM，禁止作为 HTML 执行。

这套控制只适用于本机实验。它没有用户认证、跨机器权限、生产级 HTTP 连接限流或分布式任务队列；不要映射到公网，也不要用反向代理绕过 loopback 限制。用量未知时 UI 显示“未知”或已知下限，不把缺失计费统计显示成免费。

## 验证

```powershell
python -m unittest tests.test_server -v
```

测试启动真实 loopback HTTP 服务，覆盖任务创建与报告、增量事件、案例和路径限制、跨域/Host/CSRF 检查、队列限额、运行中及等待中取消、预算停止、provider 错误、私密字段过滤和 CSP。超限正文测试包含实际发送的 9,000 字节正文、声明极大长度却不发送正文以及正文不完整的快速拒绝。模型全部为本地模拟，不发送真实 API 请求。
