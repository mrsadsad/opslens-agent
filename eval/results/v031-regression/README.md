# 真实模型对照实验

数据与源码摘要见 experiment.json；完整指标见 summary.json。

小样本探索性实验。来源与划分分开解释，不能据此宣称生产收益或全面语义正确。

| 案例 | 来源 / 划分 | 策略 | 自动检查 | 模型请求 | Tokens | 失败类别 |
| --- | --- | --- | --- | ---: | ---: | --- |
| [inc001](attempts/attempt-00001/state.json) | synthetic / dev | single-shot | 未通过 | 1 | 3641 | report_audit_failed |
| [inc001](attempts/attempt-00002/state.json) | synthetic / dev | react | 通过 | 4 | 12479 | — |
| [inc001](attempts/attempt-00003/state.json) | synthetic / dev | investigator | 通过 | 6 | 26627 | — |
