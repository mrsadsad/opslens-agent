# 0.4 可靠性改进与离线验收

日期：2026-10-03。方向：Agent 应用开发与后端工程。所有新增验证使用固定响应、合成压力输入或本机 HTTP；外部模型请求为 0。历史真实模型结果与冻结源码保持原样。

## 修复了哪些实际问题

| 问题与触发条件 | 0.4 行为 | 回归证据 |
| --- | --- | --- |
| 长对话压缩后，按 ID 重新取回的旧证据仍因字典顺序靠前而再次被省略 | 最近工具实际返回的证据优先；再保留假设引用、服务能力与历史记录；披露可见与省略数量，保留最近查询及分页位置 | `tests/test_context.py` 的暂停、SQLite 恢复、下一次模型输入集成测试 |
| 同类记录超过单次上限，缺少后续访问入口；再次裁剪可能跳过尚未交付的记录 | 5 类查询增加 offset；统一工具结果、运行时字符上限和评测观测预算的连续前缀与游标 | `tests/test_tools.py` 45 条记录逐页无遗漏/重复；`tests/test_benchmark.py` 字符裁剪与预算阻塞 |
| 模型参数含 NaN、Infinity 或 1e999，JSON 解析后无法保存严格 JSON 检查点 | 解析阶段拒绝，返回可修正的参数错误；已消耗步骤及用量正常持久化 | `tests/test_runtime.py` 非有限数值参数后暂停恢复；`tests/test_providers.py` 响应边界 |
| 工作台重启后 SQLite 中的已完成任务无法在页面读取 | 按案例 ID 和内容指纹读取有界历史；旧活动任务显示 interrupted；历史只读、不自动调用模型 | `tests/test_server.py` 真实 HTTP 重启、报告与轨迹一致、零新增 provider 构造 |
| wheel 没有默认案例，离开源码目录后 demo/serve/mcp 无法启动 | 默认示例随包提供；`--version` 可查询版本；隔离安装后实际调用三个入口 | `scripts/verify_release.py` 全新 venv、空工作目录、实际子进程与 HTTP |

## 实测结果与复现

在本机 Windows / Python 3.12 环境：

- `python -m unittest discover -s tests -q`：204 项通过。
- `python scripts/verify_archives.py`：4 份案例清单和 32 份冻结源码哈希通过。
- 构建 0.4.0 wheel，再执行 `verify_release.py`：5 项通过，包括离线安装、demo 暂停恢复、MCP 握手与查询、工作台调查、工作台重启历史读取。
- 页面内联 JavaScript 通过 Node 语法检查。

```powershell
python -m unittest discover -s tests -q
python scripts/verify_archives.py
python -m pip wheel . --no-deps --wheel-dir dist
python scripts/verify_release.py --wheel dist/opslens_agent-0.4.0-py3-none-any.whl
```

安装验收通过 `--no-index --no-deps` 在新的虚拟环境中安装指定 wheel，清除 PYTHONPATH 并验证实际导入来自虚拟环境，防止误用工作区代码造成假通过。模型供应商固定为 scripted-demo；HTTP 仅访问本机。结果默认写入忽略目录 `runs/release-check.json`。CI 配置在 Ubuntu/Windows 与 Python 3.11/3.12 上运行同样的测试、归档验证及安装验收；跨平台结果以对应 GitHub Actions 运行记录为准。

45 条压力记录的测试验证的是软件查询可达性。它没有让真实模型选择分页，也没有测量根因诊断正确率、token 降幅或生产排障时间。原有真实模型实验中的记录数量较少，不能把这些新发现归因成旧实验失败的原因。

## 三分钟演示顺序

1. `python -m opslens serve`，选择 inc001 创建固定脚本调查。先解释告警、实际工具结果和报告引用。
2. 关闭服务后用同样参数重启，刷新页面，展示原报告和事件仍可读取。强调浏览历史不会重新调用模型。
3. 运行 `python -m unittest tests.test_context tests.test_tools tests.test_benchmark -q`，展示证据回取与分页边界由自动回归覆盖。
4. 展示旧的真实模型实验及其中一次失败，说明软件可靠性通过与模型诊断正确之间的区别。

## 面试需要自己讲清楚的设计

- 工具先返回 20 条、运行时只装得下 7 条时，下一页为什么必须从第 8 条继续？预算耗尽为何不能返回普通空结果？
- 为什么仅更新 `observed[id]` 无法让旧证据重新变成“最近证据”？压缩后保留哪些事实，省略的内容怎样诚实披露？
- 模型请求前保存 pending 有什么价值？请求中断时，为什么不能把未知用量当成 0？
- 历史查询为什么校验案例内容指纹？为什么重启展示和自动恢复执行要分开？
- 为什么源码目录中运行成功，不代表 wheel 安装后成功？测试怎样避免源码通过 PYTHONPATH 混入？

这些改动由 AI 协助完成。求职材料应明确个人实际参与、验证和理解的部分；不要把本机实验写成线上项目，也不要承诺录用结果。

## 仍然存在的边界

当前数据仍是冻结快照；故障种类与拓扑有限，没有独立生产验证。单条证据保留的内容最多 1600 字符，搜索仅覆盖可见前缀，尾部分段回取尚未实现；`content_truncated` 与 `content_original_chars` 显式说明限制。历史分页是本机数据库的插入顺序游标，不是多租户鉴权或分布式队列。未新增真实模型实验，未证明策略优劣、抗提示注入能力或完整语义正确率。
