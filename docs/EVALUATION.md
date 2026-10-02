# 可复现的策略评测

本项目把诊断质量、证据是否可追溯、执行成本分开记录。离线单元测试只验证协议和统计实现。测试用 provider 对任何输入都固定弃权；它不读取答案，不代表任何真实模型的能力。目前不能把这些测试生成的数字写成简历上的模型效果。

## 冻结实验，再执行

评测入口是 `opslens.benchmark.execute_benchmark`，CLI 为 `python -m opslens benchmark`。先执行不会调用模型的检查：

```powershell
python -m opslens benchmark --manifest eval/fixtures.manifest.json --policies single-shot fixed-workflow react investigator --repeats 1 --max-model-calls 60 --dry-run
```

真实模型实验需要可用的 provider 配置。删除 `--dry-run` 才会执行模型请求；使用 `--output runs/experiment-001` 指定一个空目录。不要复用已有结果目录覆盖失败结果。六个案例、四种策略、每种一次且 Agent 最多 12 步的理论调用上限为 `6 × (1 + 1 + 12 + 12) = 156`；60 次全局预算可能让实验中途停止，报告会明确标记不完整。这里给出预算关系，不建议为了填满报告而无条件扩大开销。

manifest 的路径相对 manifest 所在目录解析，例如：

```json
{
  "schema_version": 1,
  "dataset_version": "example-v1",
  "observation_budget": {"max_chars": 24000, "max_records": 100},
  "cases": [{
    "case_id": "example",
    "path": "../data/cases/example.json",
    "sha256": "此处填写原始文件字节的64位SHA-256十六进制摘要",
    "group_id": "family-a",
    "label": {
      "cause_code": "dependency_timeout",
      "root_service": "payments",
      "required_evidence": ["metric-id"],
      "evidence_groups": [["log-id-a", "log-id-b"], ["change-id"]],
      "split": "test"
    }
  }]
}
```

`split` 支持 `train/dev/test/challenge`。同一 `group_id` 不得跨 split；同一故障实例的改名、时间变化和不同 seed 变体应保持同组。把同一模板换一个服务名不构成独立未知故障。测试集必须在调 prompt 前冻结；工具只能检查所声明的分组，不能自动证明组与组之间独立。

`root_service` 可以省略或为 null，此时不评价组件定位。有标签时必须准确匹配组件，单凭选对有限原因类别不能获得任务成功。`required_evidence` 中的每条证据都必须被观察并引用。可选的 `evidence_groups` 表达 AND-of-OR：每个内层数组是可替代证据，命中其中至少一条即可；所有组都必须命中。例如上例要求 `metric-id`、`log-id-a` 或 `log-id-b` 中至少一条，以及 `change-id`。两类要求同时生效，所有 ID 都必须存在于 snapshot。标签不限定模型只能引用这些证据。

加载器先核验 SHA-256、案例 ID、标签引用和分组。执行时将实际 snapshot 字节及可选 provenance 复制到输出目录，同时保存包版本、实验设置、实现文件摘要与 manifest 摘要。manifest 标签只在验证和评分层使用，策略只获得工具对象，provider 只获得消息与工具 schema。标签不进入模型上下文。

## 四种策略比较的是什么

| 策略 | 检索策略 | 模型调用 |
| --- | --- | --- |
| `single-shot` | 先列服务，再对全部已发现服务做宽泛 metrics、logs、changes 查询，补充固定 runbook 查询 | 一次诊断 |
| `fixed-workflow` | 对全部已发现服务获取 metrics、changes，再按告警中确定性提取的最多两个关键词检索 logs、runbooks | 一次诊断 |
| `react` | 模型根据观测自主选择后续只读工具 | 最多 `max_steps` 次 |
| `investigator` | 在迭代工具调用中维护假设、支持与反证的结构化记录，并通过诊断提交门控 | 最多 `max_steps` 次 |

两种一次调用策略使用相同的源集合，但检索顺序与选择不同；它们没有被声称为“完全相同上下文”的对照。宽泛预取也受工具返回上限与预算限制，不能称为完整读入所有原始数据。固定关键词策略可能漏掉关键词未命中的日志；这种差异属于所比较的检索策略。Agent 可用 `get_evidence/get_trace` 等接口按需追查，baseline 不自适应调用；这同样是策略差异，不能把所有增益仅归因于多轮推理。

所有策略都经过同一个 `BudgetedTools`：默认累计最多 24,000 个证据字符、100 次记录交付；重复返回同一记录仍收费。字符按每条记录规范 JSON 的 Unicode 字符数计数，不是 token 估算。告警、工具 schema、控制信息不计入证据预算，但会进入真实模型 token 用量。每次工具结果另有限长，保留完整记录边界，未返回的记录不能当成已观察证据。

预取查询与 `submit_diagnosis` 共用 `max_tool_calls`；baseline 为最终提交预留一个工具名额。`investigator` 的假设工具也占工具预算。默认每次请求上下文上限 48,000 字符、每个 Agent run 已报告 token 总量达 60,000 后停止发起下一次请求；该阈值不是对单次生成 token 的精确预测。baseline 一次请求的上下文同样不能超过 48,000 字符。Agent 的上下文压缩与 baseline 的固定预取在实验元数据中可区分。

Agent 每次模型请求都附带剩余模型调用次数（含本次）和工具调用次数，并提示为假设更新和最终报告预留预算；普通上下文与压缩上下文均如此。提示本身计入上下文字符预算，不改变实际预算扣减规则，也不保证模型会合理规划调用。

查询能力由 snapshot 中实际记录生成，`list_services` 返回每个服务可用的记录类型、按类型区分的阶段和结构化 trace 能力。`query_metrics`、`search_logs` 请求该服务及记录类型不存在的阶段时返回 `unsupported_phase` 和可用阶段；没有阶段字段的旧 snapshot 也会明确提示。工具不会偷偷扩大到其他阶段，模型须显式改用可用阶段或省略 `phase`。合法查询没有匹配内容仍正常返回空记录。评测分别记录 `empty_retrieval_calls` 和 `unsupported_phase_calls`，用于识别检索浪费，能力信息不包含标签。

全局调用预算通过共享锁在 `complete()` 前原子预留；失败请求同样计次。内置 HTTP provider 在 benchmark 中禁用内部自动重试，以免绕过总预算。自定义 provider 必须遵守一次 `complete()` 至多请求一次模型的契约。没有 best-of、多次失败后只展示成功、或自动补跑。执行顺序在案例之间轮换策略，减少始终先执行某一种策略的偏差。

同一次实验的 `provider_factory` 必须持续返回相同 provider 身份。若身份发生变化，当前格子记录为 `experiment_invalid`，之后格子记录为 `skipped_invalid` 并继续保留在计划分母；不再构造 provider 或请求模型。汇总明确给出 `invalid_reason=provider_identity_changed`、预期与实际身份、`experiment_valid=false`、`experiment_complete=false`，整次实验的 Wilson 和配对区间全部撤销，包括违规前已完成的数据层。保存的索引也保留无效原因，重新汇总不能恢复这些区间。

## 评分口径

- `structural_task_success`：存在已完成的有效报告，原因类别正确，有组件标签时组件正确，全部必需证据和每个证据组均被观察并引用，且所有引用都可追溯。`insufficient_evidence` 案例同样要覆盖标签要求的证据。
- `task_success`：满足上述结构与标签要求，且最终报告未触发已实现的数值审计规则。这仍不是完整语义正确率。
- `cause_match`、`root_service_match`：字面类别与组件匹配，不判断解释的因果合理性。
- `required_evidence_recall`：关键证据中同时被观察并引用的比例。猜中未观察的 ID 不得分。标签没有关键证据时为 null。
- `evidence_groups_recall`：已覆盖证据组的比例，每组至少一条 ID 同时被观察和引用才算覆盖。没有证据组时为 null，缺少任意必需组都会阻止任务成功。
- `citation_traceability`：引用 ID 中实际被观察的比例。空引用为 null，不能因空集合被奖励 100%。这不是“引用语义正确率”。
- `abstention_correct`：仅对明确弃权报告判断弃权类别是否符合标签；普通诊断为 null。正确弃权但缺关键证据仍然任务失败。只有标签本来不要求任何证据时，空引用弃权才可能成功。

`investigator` 的假设门控验证结构、引用来源和流程要求，并对假设陈述应用有限数值规则；写了一个支持假设和一个反证假设不等于推理可靠，更不等于建立了因果关系。报告正文仍需要人工审阅，尤其要区分故障原因与相关现象。本版本没有把 LLM 自评分伪装成客观评测。

失败采用互斥分类，包括全局预算耗尽、单次运行预算耗尽、provider 错误、协议错误、工具/执行错误、无报告、无效报告、数值审计拒绝、错误弃权、原因错误、组件错误、关键证据或证据组缺失和不可追溯引用。先发生的执行状态可以优先于结果质量分类；完整 trace 用于进一步诊断。

## 有限数值审计与修正记录

`report_audit` 使用确定性规则检查三类已知风险：明确数值变化与“减半”等相对变化描述不符；数量与百分比之间缺少匹配的分母、统计总体或时间窗口；把多个 p95 相加推导端到端 p95。它只处理已实现的表达模式和单位，不读取答案、不调用模型，也不改写报告。跨语句关联等规则可能标记需要人工复核的风险；标记并不等于已经证明诊断错误。隐含数量、未覆盖单位和一般自然语言中的数学推理仍可能漏检，未命中规则不能称为语义正确。

四种策略提交最终报告时使用相同的数值审计门控，评分层再次审计最终报告。Agent 收到 `report_audit_failed` 后可以在原有预算内修正；一次调用 baseline 的提案被拒绝后不会额外获得模型重试。`investigator` 的假设陈述若命中规则，也会以携带问题清单的 `invalid_hypothesis` 拒绝。这些差异属于端到端策略设计，比较结果不能单独解释为纯推理能力差异。审计版本与源码摘要进入运行定义，避免规则改变后继续沿用旧检查点。

`numeric_audit_passed` 只描述最终报告，无最终报告时为 null。`numeric_rejection_count` 保留报告及假设的历史数值拒绝次数；后续修正成功不会抹掉此前问题。`numeric_repair_requests` 记录报告数值反馈次数，不等于成功修复次数。汇总的 `n_numeric_audit_flagged` 包含最终审计不通过或曾发生数值拒绝的运行，因此不能只凭最终成功率隐藏修正成本。

## 统计与成本

所有预先安排的 `case × policy × repeat` 均进入成功率分母。全局预算耗尽或用户中断之后尚未执行的格子，也会保存明确的失败状态。策略比较必须有完整配对，拒绝重复格子和选择性缺失。

结果按 `source_kind × split` 分层汇总，不把合成 fixture 和本地真实故障采集合并成一个效果数字。本地回环 HTTP 实验虽有真实测量，也不能代表生产事故的多样性。

每个策略同时报告成功率和 95% Wilson 区间。Wilson 的独立 Bernoulli 假设在存在重复或同组变体时不成立，因此区间只作描述，不能把重复试验数当独立样本数。`wilson_independent_trial_layout` 只表示没有已知的同组重复布局，不证明真实独立性。

如果某个 `source_kind × split` 层出现全局预算耗尽或中断造成的未观测结果，该层所有策略的 Wilson 区间均为 null，并记录 `inference_block_reason`；即使该层某一种策略恰好已完成全部案例，也不单独给出区间。成功率仍保留所有计划格子的分母，用于审计执行结果，不能把占位失败当作完整模型测量。

策略差值按相同案例、相同 repeat 配对。先在 `group_id` 内平均成功率差，再以 group 为单位做 2,000 次、固定 seed=0 的配对 bootstrap，报告 A−B 和 95% percentile 区间。估计量对组等权，所以组规模不同时，它可能不同于整体逐次成功率之差。少于两个组不输出区间；少量组即使能计算区间也不足以支持稳定泛化结论。若比较受到全局预算或中断截断，则不输出推断区间。所有差值都是描述性探索，不提供多重比较校正后的显著性结论。

延迟采用已开始执行的 run 的 wall time，失败 run 也纳入；完全未执行的预算占位不纳入延迟分位数。报告线性插值的 p50/p95、实际请求次数、工具调用次数、证据字符量，以及 provider 上报的 prompt/completion/total tokens。缺 usage 时保留 `usage_complete=false`，这些 token 合计只能视作已知部分。不设置模型价格表，因此不虚构货币成本。

## 输出与复核

- `manifest.json`、`cases/`：冻结的数据与 evaluator 标签；不要将标签接入调查工具。
- `experiment.json`：执行前保存的版本、代码摘要、策略及预算。
- `attempts/<id>/state.json`、`score.json`、`trace.jsonl`：每次结果、评分与工具级轨迹。
- `attempts.json`：每完成一个格子就更新的索引，失败也保留。
- `checkpoints.sqlite3`：内部恢复状态与完整消息；公共 `state.json` 不渲染 provider 的内部推理字段。
- `summary.json`：分层指标、策略差值、区间、成本与限制说明。

已有输出目录不能被覆盖。Ctrl+C 会停止后续模型调用并保存其余格子的中断状态；进程被强制终止或磁盘故障可能使最终 summary 不存在，此时只能检查已保存索引和 checkpoint，不能把部分成功结果当作完成的 benchmark。本入口不自动恢复整个实验并追加模型调用。

脚本或协议模拟 provider 必须设置 `is_scripted=True`；内置 `scripted-demo` 自动识别。只要含有这类运行，汇总就标记 `protocol_demo_only` 和 `DEMO ONLY`，不能据此报告模型成功率提升。

离线验证命令：

```powershell
python -m unittest tests.test_benchmark tests.test_evaluation -v
```

投递时可展示冻结 manifest、失败样例和测试覆盖范围。只有实际运行真实模型并审阅了独立数据集上的完整结果，才填写模型效果、成本和对照提升数字。
