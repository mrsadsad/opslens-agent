# 本机 HTTP 故障实验

本模块把“手写故障样本”推进到可审计的实际实验：启动两个只监听 `127.0.0.1` 的临时 HTTP 服务，发起并发请求，修改生效配置，再撤销修改并实际观测恢复。只使用 Python 3.11+ 标准库，支持 Windows/Linux，不需要 Docker、云账号或模型 API。

这是真实网络请求产生的**受控本机故障数据**，不代表生产故障数据、真实企业规模或大模型能力评测结果。

## 使用

Python API：

```python
from pathlib import Path
from opslens.lab import collect_case, collect_suite

result = collect_case(Path("artifacts/lab"), "pool_exhaustion", seed=17)
print(result["case_path"])
print(result["provenance"]["validation"])

suite = collect_suite(Path("artifacts/lab"), seeds=[17, 23])
print(suite["manifest_path"])
```

`collect_case(output_dir, scenario, seed=0)` 返回 `case_path`、`label`、`provenance`。所有路径使用本机绝对路径。`collect_suite(output_dir, seeds=None, scenarios=None)` 默认运行 4 个场景，返回 `manifest_path` 和 `cases`；每个 `cases` 项就是单次实验的返回值。suite 最多接受 10 个不同 seed 和 4 个不同场景。

CLI 入口由项目主命令集成：`python -m opslens lab --scenario pool_exhaustion`。具体输出目录参数见 `python -m opslens lab --help`。

## 实际发生的故障

| 场景 | 实际操作 | 请求层可观测现象 |
| --- | --- | --- |
| `healthy` | 保持基线配置，进行同样三阶段请求 | 正常 HTTP 响应，作为阴性对照 |
| `pool_exhaustion` | 调用端并发槽池由 8 缩至 1；每次调用占槽期间真实请求下游 | 槽获取等待超时，调用端 HTTP 503；下游已收到的请求仍正常完成 |
| `dependency_timeout` | 下游处理等待由 60 ms 增至 450 ms；调用端 deadline 250 ms 保持不变 | 真实 socket 读取超时，调用端 HTTP 504；下游稍后完成请求 |
| `bad_configuration` | 下游 URL 路径从有效 `/work` 改成不存在的路径 | 下游真实 HTTP 404，被调用端映射为 HTTP 502 |

池使用受限 `BoundedSemaphore` 模拟客户端连接/并发资源的获取与释放，并不声称运行了真实数据库连接池。下游延迟通过服务实际等待注入。坏配置场景是错误路径，而非伪造 DNS 故障。当前只有两个服务、一条调用链和四种预先定义场景；更复杂拓扑和组合故障是后续工作。

每个场景都运行 `baseline → fault → recovery`，每阶段 24 次请求，并发上限 8。`healthy` 的 `fault` 阶段是无操作对照。服务关闭前会等待超时调用对应的下游请求结束，避免将前一阶段残留请求算入下一阶段。单次客户端 socket 超时 2 秒，阶段内请求数固定；正常实验通常耗时数秒，宿主机调度会影响实测延迟。没有外网请求、shell 命令、自动扩容或生产变更。

## 审计文件和答案隔离

每次生成一个随机命名目录：

```text
case-<opaque UUID>/
  snapshot.json    # Agent 唯一允许读取的实验观测快照
  requests.jsonl   # 本次所有客户端、调用端和下游请求记录
  provenance.json # 控制面：场景、seed、注入配置、测量、质量门槛和文件哈希
  label.json      # 仅供 evaluator：cause_code、root_service、required_evidence、split
```

`snapshot.json` 设置 `synthetic: false` 和 `source: "local_fault_lab"`；它包含本次真实采样，不表示这是生产系统。模型可见的 case ID、文件名、服务名、证据 ID 和 alert 不包含场景名称。服务别名由 seed 稳定生成；case/证据/request/trace ID 每次生成新的随机值。日志含 `request_id`、`trace_id`，可跨调用端、下游和负载客户端关联。

指标从原始请求逐项计算：请求数、错误数/率、HTTP 状态分布、错误分布、实测 p50/p95、池峰值占用与等待数。快照日志从实际服务事件中采样，错误优先；全量数据保存在 `requests.jsonl`。日志和指标的 `content` 均标注 phase，即便工具只保留通用字段，也能区别基线、故障窗口和恢复阶段。`change` 记录是实际生效配置差异；`runbook` 仅提供通用排查知识。

`provenance.json` 和 `label.json` 不进入工具上下文；不能将场景控制面描述当作模型已经获得的证据。运行 Agent 时只传 `snapshot.json`，评分完成后再单独读取标签。provenance 记录 snapshot/raw requests 的 SHA-256，suite manifest 对 snapshot 原始字节另做哈希绑定。

## 质量门槛和测试

场景标签不能代替实验观测。生成器要求：基线和恢复错误率不超过 10%；故障场景实测错误率至少 25%，且恢复后错误率至少下降 20 个百分点；实际出现对应请求失败模式；超时场景还必须从下游请求测到至少 150 ms 的 p95 增长。若负载客户端自身出错或宿主机无法形成干净故障，这次实验不合格。

质量门槛失败时保留 snapshot/raw/provenance，抛出 `LabValidationError`，不生成可评分标签。不会将未恢复或未形成故障的运行悄悄标为成功。服务端口与工作线程在正常完成及异常退出时关闭。

```powershell
python -m unittest tests.test_lab -v
```

集成测试真实运行四个场景，验证故障与恢复、原始数据和指标一致、跨服务请求关联、证据及标签隔离，以及端口和线程释放。测试使用错误率差和宽松延迟差，不要求固定毫秒值。

suite manifest 格式为：

```json
{
  "schema_version": 1,
  "cases": [{
    "case_id": "case-<UUID>",
    "path": "case-<UUID>/snapshot.json",
    "sha256": "<snapshot 原始字节哈希>",
    "label": {"cause_code": "...", "root_service": "svc-<alias>", "required_evidence": ["e-<UUID>"], "split": "dev"},
    "group_id": "local-http-v1:<scenario family>",
    "source": "local_fault_lab",
    "provenance_path": "case-<UUID>/provenance.json"
  }]
}
```

故障标签还要求定位实际随机命名的根因服务：池与错误配置位于调用端，下游超时位于下游；healthy 的 `root_service` 为 `null`，不评分组件。仅猜对故障类别不足以满足这项要求。

第一批全部为 dev。不同 seed 可以复现配置和服务别名，不能复现操作系统调度、端口、随机 ID 或精确延迟。同一场景的不同 seed 共享 family `group_id`；随机改名、重新采样和同模板重跑，均不能宣称验证了未知故障泛化能力。
