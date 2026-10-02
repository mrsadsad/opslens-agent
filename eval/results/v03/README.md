# 真实模型对照实验

数据与源码摘要见 experiment.json；完整指标见 summary.json。

小样本探索性实验。来源与划分分开解释，不能据此宣称生产收益或全面语义正确。

| 案例 | 来源 / 划分 | 策略 | 自动检查 | 模型请求 | Tokens | 失败类别 |
| --- | --- | --- | --- | ---: | ---: | --- |
| [inc001](attempts/attempt-00001/state.json) | synthetic / dev | single-shot | 未通过 | 1 | 3400 | report_audit_failed |
| [inc001](attempts/attempt-00002/state.json) | synthetic / dev | fixed-workflow | 未通过 | 1 | 3363 | missing_required_evidence |
| [inc001](attempts/attempt-00003/state.json) | synthetic / dev | react | 未通过 | 5 | 15011 | wrong_cause |
| [inc001](attempts/attempt-00004/state.json) | synthetic / dev | investigator | 通过 | 6 | 25484 | — |
| [inc006](attempts/attempt-00005/state.json) | synthetic / dev | fixed-workflow | 未通过 | 1 | 3023 | missing_required_evidence |
| [inc006](attempts/attempt-00006/state.json) | synthetic / dev | react | 通过 | 6 | 20666 | — |
| [inc006](attempts/attempt-00007/state.json) | synthetic / dev | investigator | 通过 | 6 | 26695 | — |
| [inc006](attempts/attempt-00008/state.json) | synthetic / dev | single-shot | 通过 | 1 | 3568 | — |
| [case-29eb33d90a074bef863815dd0a54a89b](attempts/attempt-00009/state.json) | live_capture / dev | react | 未通过 | 6 | 38019 | wrong_cause |
| [case-29eb33d90a074bef863815dd0a54a89b](attempts/attempt-00010/state.json) | live_capture / dev | investigator | 未通过 | 5 | 38495 | wrong_cause |
| [case-29eb33d90a074bef863815dd0a54a89b](attempts/attempt-00011/state.json) | live_capture / dev | single-shot | 未通过 | 1 | 9883 | wrong_cause |
| [case-29eb33d90a074bef863815dd0a54a89b](attempts/attempt-00012/state.json) | live_capture / dev | fixed-workflow | 通过 | 1 | 9443 | — |
| [case-a1d6812a389e45d794b530d2dc54d4e5](attempts/attempt-00013/state.json) | live_capture / dev | investigator | 通过 | 6 | 38133 | — |
| [case-a1d6812a389e45d794b530d2dc54d4e5](attempts/attempt-00014/state.json) | live_capture / dev | single-shot | 未通过 | 1 | 9668 | wrong_cause |
| [case-a1d6812a389e45d794b530d2dc54d4e5](attempts/attempt-00015/state.json) | live_capture / dev | fixed-workflow | 通过 | 1 | 9796 | — |
| [case-a1d6812a389e45d794b530d2dc54d4e5](attempts/attempt-00016/state.json) | live_capture / dev | react | 通过 | 5 | 25542 | — |
| [sc_c9e0ef9527c9](attempts/attempt-00017/state.json) | synthetic / challenge | single-shot | 通过 | 1 | 4355 | — |
| [sc_c9e0ef9527c9](attempts/attempt-00018/state.json) | synthetic / challenge | fixed-workflow | 通过 | 1 | 4002 | — |
| [sc_c9e0ef9527c9](attempts/attempt-00019/state.json) | synthetic / challenge | react | 通过 | 6 | 23617 | — |
| [sc_c9e0ef9527c9](attempts/attempt-00020/state.json) | synthetic / challenge | investigator | 未通过 | 6 | 28734 | run_budget_exhausted |
| [sc_fc922dd2fbe0](attempts/attempt-00021/state.json) | synthetic / challenge | fixed-workflow | 通过 | 1 | 4034 | — |
| [sc_fc922dd2fbe0](attempts/attempt-00022/state.json) | synthetic / challenge | react | 通过 | 4 | 12896 | — |
| [sc_fc922dd2fbe0](attempts/attempt-00023/state.json) | synthetic / challenge | investigator | 通过 | 5 | 21841 | — |
| [sc_fc922dd2fbe0](attempts/attempt-00024/state.json) | synthetic / challenge | single-shot | 通过 | 1 | 4085 | — |
| [sc_4999df7e6986](attempts/attempt-00025/state.json) | synthetic / challenge | react | 未通过 | 5 | 16716 | missing_evidence_group |
| [sc_4999df7e6986](attempts/attempt-00026/state.json) | synthetic / challenge | investigator | 未通过 | 6 | 28448 | missing_evidence_group |
| [sc_4999df7e6986](attempts/attempt-00027/state.json) | synthetic / challenge | single-shot | 通过 | 1 | 4053 | — |
| [sc_4999df7e6986](attempts/attempt-00028/state.json) | synthetic / challenge | fixed-workflow | 未通过 | 1 | 3917 | missing_evidence_group |
| [sc_a7da59a02d8b](attempts/attempt-00029/state.json) | synthetic / challenge | investigator | 通过 | 5 | 21707 | — |
| [sc_a7da59a02d8b](attempts/attempt-00030/state.json) | synthetic / challenge | single-shot | 通过 | 1 | 4196 | — |
| [sc_a7da59a02d8b](attempts/attempt-00031/state.json) | synthetic / challenge | fixed-workflow | 通过 | 1 | 4324 | — |
| [sc_a7da59a02d8b](attempts/attempt-00032/state.json) | synthetic / challenge | react | 未通过 | 2 | 1846 | provider_error |
