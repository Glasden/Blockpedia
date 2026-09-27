> 历史归档（2026-09-27）：停止维护，不再作为当前执行规范。当前决定与进度统一见 [PROGRESS.md](../../../PROGRESS.md)。正文只做归档链接修正，旧状态不代表本轮完成。

# Blockpedia 产品范围

Blockpedia 是本地个人使用的 Minecraft 原版方块知识与检索工具，非 Mojang/Microsoft 官方产品。宿主 LLM 负责自然语言理解和关键词生成；MCP 只在已发布数据中做本地搜索、详情和比较。

当前交付由 [D-054 规格](export-build-refactor-spec.md) 定义。旧“契约冻结”不构成保留重复机制的理由。

## 用户闭环

1. 在 Minecraft Java 26.2 中用 Fabric 普通导出生成完整 registry/state/variant 与渲染数据。
2. 在 loopback WebUI 选择精确版本及导出，执行一次导入；不用先领取检查 ID。
3. Studio 提取特征，执行显式批准的 OpenAI 离线标注，并保存人工审核/覆盖。
4. 一次构建生成独立 release；用户可发布第一个合格 release，也可回滚到另一份已有 release。
5. MCP 从 current 指向的精确版本读取数据，返回事实、图片和警告，不调用模型或写本地状态。

## 保留能力

- minecraft 命名空间的全部 Block，不按建筑适用性漏登记；合法状态、几何和行为来自运行时。
- 每个 Block 有有效变体或有审核原因的 skip；机器事实、AI 建议、人工覆盖分层且可追溯。
- eligible、conditional、excluded 资格；conditional 有警告，excluded/skip 有真实人工记录。
- 稳定的离线语义、人工重放和本地 FTS5 trigram/normalized LIKE 搜索。
- 多版本目录与 current map；WebUI 明确版本，MCP 省略版本使用 default，未知精确版本不回退。
- 源码与锁依赖的本地运行；正式平台目标是 Windows 11 x86_64 和 Linux x86_64。实际平台证据分别报告。

## 收缩后的边界

SQLite 已提交结果是 workspace 事实源，release 自身是已构建事实，current 是已发布事实。取消本地篡改检测、check cache 许可、全量 snapshot 指纹、派生结果 JSON 证明、双 release 上线条件和每次发布时的 MCP smoke。

一次操作仍有输入/业务正确性检查、路径安全、事务和原子提交。发布仍需用户明确确认。应用不原地改写 release，不因为清理流程重跑已有 AI 或删除人工数据。

普通 exporter 已包含 banner 修复，专用 repair/refresh 退出产品路径；已有成果与历史 mixed lineage 保留。新导出创建新 workspace，本轮不实现通用跨导出合并。

MCP 仍只有 stdio 的 index_info、search_blocks、get_block_details、compare_blocks。search_blocks 接收 keywords，保持本地召回/排序、Top-24、limit/contact sheet 和正常空结果，不接收旧 query/context/query_spec，不假装模型重排。

## 不在本次范围

不增加服务、队列、向量数据库、provider 类型、通用迁移、公开部署、账号、CORS/CSRF、安装包、容器或自动更新。不记录 Token usage、费用或预算；黄金查询与相关性调优不作为本轮完成门。

WebUI 只绑定 127.0.0.1:8765；Python 产品 CLI 只有 web，MCP 由客户端以 Node stdio 子进程启动。真实导出、原版资产、索引、预览、人工本地数据和秘密不进入公共仓库。既有 provider 的授权、协议和重试预算保持。
