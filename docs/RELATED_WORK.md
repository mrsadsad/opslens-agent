# 相关研究与项目定位

OpsLens 是小规模可审查的工程实验平台，目前没有在以下公开基准上取得结果。

[OpenRCA 官方仓库](https://github.com/microsoft/OpenRCA)将自然语言问题与多源遥测分析结合，任务包含根因组件、发生时间和原因，并要求评测方案不得读取标准答案。对本项目的启发是同时评分组件、类别，并隔离标签和可访问观测。当前尚未适配其数据，本机两服务实验不能替代它的规模和复杂度。

[RCAEval 官方仓库](https://github.com/phamquiluan/RCAEval)提供多个微服务系统的故障案例及可复现基线。后续接入应核对数据格式、许可证、资源开销和任务定义，不能把自己的开发样例分数直接与论文指标比较。

工具互操作参考 [MCP 2025-11-25 工具规范](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。OpsLens 复用同一受限工具实现，为本地 Agent 和外部客户端提供入口；具体兼容边界见 MCP 文档。
