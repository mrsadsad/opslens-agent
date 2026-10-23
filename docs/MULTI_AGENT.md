# 0.5 多 Agent 协作调查

新增 `multi-agent` 策略：三个专项 Agent 各自调用模型、操作受限工具，随后由协调 Agent 核对证据并提交最终报告。使用同一种模型也可形成多 Agent：角色、上下文、工具权限、预算和状态均独立。任务拓扑由程序固定，不宣称动态路由或自动规划团队。

```text
同一故障快照
  ├─ 指标 Agent ── query_metrics ──────────┐
  ├─ 日志 Agent ── search_logs ───────────┤ 并行，每个角色独立上下文
  └─ 变更 Agent ── get_changes / runbooks ┘
                 ↓ 结构化交接（含失败状态和分歧）
           协调 Agent：回取原始证据、补充查询
                 ↓ 原有引用与数值风险校验
               最终报告
```

## 运行

在本仓库根目录，Python 3.11+：

```powershell
# 不使用模型 Key、不调用收费 API；inc001 固定脚本验证工程闭环
python -m opslens demo --policy multi-agent

# 三轮专项调查后暂停，替换下方 run_id 为终端输出值
python -m opslens demo --policy multi-agent --stop-after 3
python -m opslens resume <run_id> --mode demo

# 启动工作台，在“调查策略”选择“多 Agent 协作”
python -m opslens serve --port 8770

# 使用你自己配置的模型，运行会产生 API 用量
python -m opslens run --policy multi-agent --max-steps 24 --max-tools 48 --prompt-key
python -m opslens serve --mode live --port 8770
```

工作台显示四个角色的状态、模型请求次数、专项结论及引用。右侧最终报告只来自协调 Agent。历史记录可以重新查看；恢复执行使用 CLI，先停止使用相同数据库的活动服务。命令行输出包括 `result.json`、全队 `trace.jsonl` 和 `report.md`。完整子 Agent 对话仅保存在 SQLite 内，不发送给浏览器或公共导出文件。

## 工程约束

- **工具权限**：各专项只能查询本范围的记录；`get_evidence` 同样检查记录类型，不能靠猜 ID 越过角色边界。协调者可访问全部原有只读工具。无修复执行权限。
- **结果整合**：交接包含状态、有限长度的结论、引用及有限的已观测 ID 索引。不同已诊断原因/组件会被标记为分歧。同行结论作为不可信资料，不能直接替代原始证据；协调者必须实际回取引用，才能通过现有报告校验。语义正确性仍需人工或独立评测。
- **总预算**：CLI/UI 中的 `max_steps`、`max_tools` 是全队总额，按固定配额分给四个角色，不是每人各有一份总预算。至少 12 次模型调用、16 次工具。模型预算为 12 时各专项 3 次、协调者 3 次；24 时各专项 4 次、协调者 12 次。工具配额为各专项 `max(3, total//5)`，余量留给协调者。未用配额不借用。
- **Token 与成本**：Token 配额按角色分配，每轮调度前也检查全队已报告总量；达到阈值后不再发起下一轮请求。并行在途请求可能使最终用量超出阈值；不是金额硬上限。逻辑模型请求与底层 HTTP 重试分别计数，未知用量不记作已知零成本。
- **持久化**：子 Agent 每次模型请求前和完整工具回合后，将独立状态复制到同一个父检查点；锁串行化 SQLite 保存，避免多线程覆盖。同一 run 不支持多个进程同时恢复。已完成专项不会在恢复时重跑；中断请求仍占用预算并标记用量不完整。
- **失败处理**：专项的 provider_error / budget_exhausted 会交给协调者，父状态显示 `partial`；不会把失败当作“未发现故障”，也不会无上限重试。协调者仍可补查或明确证据不足。
- **暂停与取消**：`--stop-after` 在多 Agent 下按调度轮数计数，一轮最多并行三个请求。工作台在轮次之间响应取消；已经发出的请求需等提供商超时/返回。最终状态可能为 cancelled，保留实际已消耗用量。

## 验证与边界

`tests/test_team.py` 覆盖真实线程并行、独立工具范围、暂停恢复、调用/用量累计、失败交接、伪造引用拒绝和未知请求恢复；`tests/test_server.py` 覆盖 HTTP 工作台、历史与取消。`scripts/verify_release.py` 包含安装后多 Agent 暂停恢复。可选浏览器验收：`python scripts/smoke_team_ui.py`（需 Playwright 和 Edge）。

本次验证使用脚本与本机环境，不调用收费模型。旧的 8 案例、32 次模型实验不包含此新策略，不能作为多 Agent 准确率或性能收益证据。原有 benchmark 四策略未改变；新策略可导出后通过 `evaluate` 评分，但尚未建立预算/观测配额对齐的多 Agent 配对实验。多个 Agent 可能增加调用成本、重复查询，也可能传播相关错误；需要后续同预算实验验证价值。
