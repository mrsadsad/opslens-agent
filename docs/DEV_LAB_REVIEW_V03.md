# v0.3 开发集与本地实验报告复核

复核范围是正式冻结实验 `v03-preregistered-paired` 的前 16 次尝试：两个手写开发案例 × 四策略，以及两个本地 HTTP 故障实验案例 × 四策略，各一次。依据公开 `state.json` 中的报告、显式假设和已观测证据，同目录 `trace.jsonl` 中的工具事件与被拒绝的提交、冻结案例及[冻结标签](../eval/results/v03/manifest.json)逐项检查；[原始自动评分](../eval/results/v03/attempts.json)保持不变。公开 `state.json` 不含 `tool_events`，下表分别链接状态与轨迹。这是看过标签的人工式语义复核，由开发助手执行，不是盲评、独立专家标注或新的模型能力分数。原始标签、报告和自动分数全部保留，不按本次意见追溯改分。

**主要结论：`wrong_cause` 不总表示没有找到故障机制；`task_success=true` 也不表示每句话正确。** 分类枚举混合了配置触发因素与池耗尽/依赖超时机制，五份 `wrong_cause` 报告均存在这种层级重叠；其中有的另外包含真实错误，不能全部直接改成成功。数值审计 1.0.0 存在明确否定句误报，也漏掉不在当前规则覆盖范围内的比较、指标名称及分母断言错误。

分开报告原始结果：手写开发案例成功 4/8，本地实测案例成功 4/8。这两个数字只描述这次冻结协议，不能用于估计生产准确率或认定某策略胜出；每类仅两个已用于开发的案例，没有重复采样。

## 手写开发案例

[`inc001` 冻结案例](../eval/results/v03/cases/case-0001.json)的原始证据为 checkout 的 DB_POOL_MAX 100→20，池已满、83 个等待者、获取连接 p95 2300ms，日志明确为 2000ms 获取超时，orders-db 本身正常。标签选择故障机制 `connection_pool_exhaustion`。[`inc006` 冻结案例](../eval/results/v03/cases/case-0002.json)的原始证据为 inventory 排队和延迟上升、checkout 3000ms deadline 超时；故障先于 checkout 发布，标签为 `dependency_timeout`。记录 `inc006-l2` 确实存在，是 inventory 的 WARN 日志，队列等待为 4110ms。

| 尝试 / 策略 | 冻结自动结论 | 逐份语义复核 |
| --- | --- | --- |
| [00001 · inc001 · single-shot](../eval/results/v03/attempts/attempt-00001/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00001/trace.jsonl) | `report_audit_failed`，未接受报告 | [被拒绝的提交](../eval/results/v03/attempts/attempt-00001/trace.jsonl)明确写了“不将组件 p95 相加推断端到端值”，却被 p95 相加规则拒绝，属于误报。正文正确定位配置缩池→池耗尽→获取连接超时；提交类别是 `bad_configuration`，所以即使不误拦，原有类别精确匹配仍不会通过。不能把 gate 修复等同于本次自动成功。 |
| [00002 · inc001 · fixed-workflow](../eval/results/v03/attempts/attempt-00002/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00002/trace.jsonl) | `missing_required_evidence`，关键引用 2/3 | 机制与组件正确，但未检索到标签要求的 `inc001-l1`；这是实际覆盖不足。另有明确错误：“获取连接 p95 达 2300 ms，接近但未达 2000 ms 的超时阈值”，2300 > 2000。数值审计通过没有验证这类大小比较。 |
| [00003 · inc001 · ReAct](../eval/results/v03/attempts/attempt-00003/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00003/trace.jsonl) | `wrong_cause`，关键引用 3/3、组件正确 | `bad_configuration` 与 `connection_pool_exhaustion` 的层级重叠：正文明确写出 100→20 导致“连接池耗尽”，不是定位错服务或机制。但把 `pool_acquire_wait_p95_ms=2300` 改称“锁等待中位数（2300 ms）”，同时混淆获取连接等待与锁等待、p95 与中位数，是独立的真实文本错误。 |
| [00004 · inc001 · investigator](../eval/results/v03/attempts/attempt-00004/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00004/trace.jsonl) | 成功，经历一次数值拒绝后修复 | 主要机制正确。第一次提交写“p95 数值亦未相加推断端到端 p95”，p95 拒绝项同样误报；另一项 count/rate 复核有理由，因为仍称 172 次/分钟与 18%“量级一致”。最终虽补了缺口径说明，仍保留“二者只作为定性的量级一致陈述”，缺分母时这种一致性没有成立。最终通过不能宣传为该论证已经被证明正确。 |
| [00005 · inc006 · fixed-workflow](../eval/results/v03/attempts/attempt-00005/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00005/trace.jsonl) | `missing_required_evidence`，关键引用 2/3 | 延迟、故障时间与发布反证支持主要诊断；未取到关键日志 `inc006-l1`。又把未检索到日志写成“本快照…无 inventory 日志”，与原始 `inc006-l2` 不符。 |
| [00006 · inc006 · ReAct](../eval/results/v03/attempts/attempt-00006/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00006/trace.jsonl) | 成功 | 主因、组件及 4110ms > 3000ms 的单请求机制成立。末段声称告警 HTTP 504 15% 与出站 RPC timeout_rate 15%“来自同一时间窗同一下游调用分母”，原始告警和 m1 没有给出共同分母，属凭空补齐统计口径。另在正文引用 `inc006-l3` 但未放入 evidence_ids，虽已观察到该记录，结构化引用清单不完整。 |
| [00007 · inc006 · investigator](../eval/results/v03/attempts/attempt-00007/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00007/trace.jsonl) | 成功 | 主因与发布反证正确，但“本快照无 inventory 日志”错误。事件显示仅执行 `search_logs(inventory, query='error')` 得空；原始日志为 WARN，应表述该查询未命中。假设账本也把正常池指标 m1 放入池耗尽假设的 supporting 字段，最后用 l3 作 contradicting；结构门控通过没有验证证据极性是否恰当。 |
| [00008 · inc006 · single-shot](../eval/results/v03/attempts/attempt-00008/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00008/trace.jsonl) | 成功 | 主要因果解释正确；“同窗口一致”的两种 15% 仍缺分母核实，宜保留为观察而非一致性证明。“order-preview…是否受同等影响，快照中无数据”措辞过宽：m2 已明确它也受影响，只是没有各调用方影响幅度，不能把定性证据说成缺失。 |

## 本地 HTTP 实测案例

本地测量是受控实验，不是生产事故。两例都记录 baseline、fault、recovery，每相调用方 24 个请求。故障注入的实现方式本身也是配置变化，因此与泛化的 `bad_configuration` 类别存在重叠。

池案例 [`case-29eb33d90a074bef863815dd0a54a89b`](../eval/results/v03/cases/case-0003.json)：`svc-ac0a5d8f79` 的 pool_max 8→1，fault 20/24 返回 503 / pool_acquire_timeout；恢复为 8 后 24/24 成功。故障期下游只接到 4 个请求，不能把没抵达的 20 个请求算作下游健康样本。下游已收到的请求仍为约 61ms，结合本地池超时错误码可以解释直接失败机制。

超时案例 [`case-a1d6812a389e45d794b530d2dc54d4e5`](../eval/results/v03/cases/case-0004.json)：`svc-a7adb501af` 的 response_delay_s 0.06→0.45，调用方 24/24 返回 504 / dependency_deadline_exceeded；同 trace 下游完成约 450ms，调用方约 260ms 提前失败。下游记录虽为 status=200，但 `response_delivered=false`；它不表示调用方成功收到 200。快照未直接提供调用方配置的 deadline 数值，约 260ms 只能是观测量级和推断。

| 尝试 / 策略 | 冻结自动结论 | 逐份语义复核 |
| --- | --- | --- |
| [00009 · 池 · ReAct](../eval/results/v03/attempts/attempt-00009/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00009/trace.jsonl) | `wrong_cause`，关键引用 4/4、组件正确 | 正文正确解释 8→1→获取等待→20/24 503→恢复；`bad_configuration` 与池耗尽标签主要是分类层级差异。没有发现会推翻这条直接机制的错误。 |
| [00010 · 池 · investigator](../eval/results/v03/attempts/attempt-00010/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00010/trace.jsonl) | `wrong_cause`，关键引用 3/4、组件正确 | 同样正确定位缩池与获取超时，但不只是类别问题：标签要求的恢复期 load_client 记录 `e-785d1e931ecf46948eb3d6ae3f358c11` 已观察到却未列入 evidence_ids；报告使用了恢复期 checkout 证据，不满足冻结的精确 ID 规则。报告直接称“误配”“应为 8”也超出有效配置变化能证明的动机范围，应区分有意注入与非预期变更。对下游 4 个成功样本的限制有明确说明，这是优点。 |
| [00011 · 池 · single-shot](../eval/results/v03/attempts/attempt-00011/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00011/trace.jsonl) | `wrong_cause`，关键引用 4/4、组件正确 | 正确解释池缩小导致超时，主要是类别层级差异。正文却把下游指标 ID 写成不存在的 `e-3202c98e06b41aaa88e919c0d28d802`，正确值多一个 c：`e-c3202c98e06b41aaa88e919c0d28d802`。错误 ID 没放进 evidence_ids，现有 citation_traceability 不会查到它。 |
| [00012 · 池 · fixed-workflow](../eval/results/v03/attempts/attempt-00012/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00012/trace.jsonl) | 成功 | 机制及组件正确，且明确不推断配置变化是误操作还是有意注入。但正文同样写错上述下游 ID；结构化 evidence_ids 则正确。自动引用可追溯率只检查清单，不保证正文每个 ID 正确。 |
| [00013 · 超时 · investigator](../eval/results/v03/attempts/attempt-00013/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00013/trace.jsonl) | 成功 | 正确串联变更、同 trace 的慢依赖和调用方 deadline；明确说明 260ms 是观测推断而非读取到的阈值，边界较好。小错误：写 acquire_wait_p95 范围 0.002–0.007ms，而三阶段聚合 p95 实际是 0.004、0.003、0.007ms，0.002 来自单条日志。结论不变，但样本值与分位数仍被混用。 |
| [00014 · 超时 · single-shot](../eval/results/v03/attempts/attempt-00014/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00014/trace.jsonl) | `wrong_cause`，关键引用 4/4、组件正确 | 根因正文准确描述延迟配置上调→调用方 deadline→504，确有配置触发因素与超时机制层级重叠；但不能简单认定全对。其“属于 bad_configuration 而非 dependency_timeout：故障期依赖并未失败或拒绝”是错误的排他理由：依赖超时发生在调用方，依赖稍后产生 200、不成功送达并不排除超时。 |
| [00015 · 超时 · fixed-workflow](../eval/results/v03/attempts/attempt-00015/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00015/trace.jsonl) | 成功 | 主要机制与跨服务 trace 正确，0.45/0.06=7.5 倍也正确。把观测约 259ms 称为“有效截止时间”应明确是推断。称基线日志的 62.648ms 为调用方 dependency 时延，实际该值是请求总 latency，dependency_latency 是 62.357ms；另把单条 acquire_wait 0.008ms 混入 p95 范围。属于量名/统计类型混淆，当前审计未覆盖。 |
| [00016 · 超时 · ReAct](../eval/results/v03/attempts/attempt-00016/state.json) · [轨迹](../eval/results/v03/attempts/attempt-00016/trace.jsonl) | 成功 | 同 trace 绑定、`response_delivered=false`、正常连接池和配置回退证据相互支持；明确 24 样本及测量口径限制。本次未发现会推翻直接机制的错误。“约 260ms deadline”仍应读作观测推断，不能当作已读取配置阈值。 |

## 解释分数时的边界

原评分按 `cause_code` 字面相等判断类别，按 evidence_ids 与 observed 的集合交集判断可追溯及关键证据覆盖。它不判解释语义，也不检查正文里的全部 ID。失败分类先看 wrong_cause，再看缺证据，因此 00010 的类别错误掩盖了其另一个引用缺口。不能只读一个 failure_category 就解释全部失败原因。

数值 gate 的两个具体风险应分别呈现：00001 / 00004 的 p95 否定句是误报；00002 的大小关系、00003 的指标类型、00006 的共同分母断言等是漏检。00004 的自动修复通过也未消除所有“量级一致”措辞。这个门控只能筛出已知模式，不能命名为“数值正确率”或“语义验证率”。

五份 wrong_cause（00003、00009、00010、00011、00014）不能描述为五次根因定位失败，也不能事后全部算成功。后续版本可以预先定义触发因素、故障机制与所属组件的判定层级，并独立报告精确类别分数和经盲评的机制/论证质量；新版本结果应独立记录。此次复核不证明 investigator 优于 ReAct 或单次诊断，也不证明固定流程更差。
