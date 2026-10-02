# 第一次实践 从真实故障走到 Agent 证据

先运行系统，再读代码。目标是区分“故障确实发生”“工具返回有效观测”和“Agent 推断正确”。

## 采集实际故障

```powershell
python -m opslens lab --suite --seeds 17 --output runs/my-lab
```

打开生成的 provenance.json，核对 baseline、fault、recovery 的请求数、错误率和耗时。再从 requests.jsonl 抽取原始请求，检查指标来源。

控制面 provenance 含注入设置，只给实验操作者和评分器使用。Agent 唯一数据入口是 snapshot.json。不能把控制面塞进模型后声称它自主定位故障。

## 浏览一次调查

```powershell
python -m opslens serve
```

在本机网页创建 inc001 调查，检查工具参数、证据、预算和引用。默认固定脚本用于理解系统，不代表模型效果。

## 配置 Key 后调用 DeepSeek

```powershell
python -m opslens run --prompt-key --policy investigator --case <实验目录中的snapshot.json>
```

在终端输入 Key，不要发送到聊天。对同一快照另跑 --policy react，比较工具、证据和报告。单例差异用于定位问题，不能证明整体性能提升。

## 三个需要解释的问题

1. 两份报告引用同一日志，为什么仍可能得出不同根因？评分器能判断到哪一步？
2. 模型收到工具结果后、状态提交前进程退出，会重复什么？有副作用的工具能否沿用这个恢复协议？
3. Investigator 可能更慢，甚至更差。怎样设计公平实验决定是否保留它？

阅读顺序：lab.py 的采样出口 → tools.py → runtime.py → investigation.py → benchmark.py。先追踪一个任务，再深入统计与服务层。

## 第一项独立改进

选一次真实模型失败，判断属于工具选择、关键证据遗漏、错误因果推断还是停止条件。只改变一个因素，例如手册检索、工具描述或假设门控，在固定开发集上复跑。

记录所有结果及开销，也保留变差的案例。解释收益来自多看证据、更多调用，还是机制本身。这项实际实验将成为你能独立讲清楚的贡献。
