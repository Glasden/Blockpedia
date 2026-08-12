# Blockpedia MVP 路线图

- **版本**：MVP 冻结版
- **日期**：2026-08-13
- **目标**：在 Windows 11 和 Linux x86_64 上完成原版 Minecraft Java 26.2 的本地单机端到端闭环
- **当前状态**：本仓库当前只有设计与治理文档，没有实现、测试报告、真实导出数据或发布索引。

规范优先级和硬限制见 [`../AGENTS.md`](../AGENTS.md)；冻结决定集中见 [`decisions.md`](decisions.md)。冲突必须先更新高优先级文档，不能在实现中静默偏离。移入的原始设计稿仅是历史背景和最低优先级参考，不能与新文档一起作为执行规范；其中冲突内容禁止实现。

## 路线图文档索引

当前索引列出的 13 份具体文档均已存在并完成语义终审。治理入口 `AGENTS.md` 与 `decisions.md` 另列为已生成的高优先级入口；原始设计稿虽列入索引，但仅是历史背景/最低优先级参考，不与新文档一起执行且保持字节不变。当前路径审查证据为 16 个 Markdown/治理文件、本地链接目标零断链；独立语义终审在修复全部 Critical/High 问题后复核为 0 Critical、0 High。该结果只证明 Markdown 契约已收敛，不代替真实 Schema、工具链、实现或跨平台验证。

所有链接均相对于本文件所在的 `docs/` 目录。

| 文档 | 链接 | 本次状态 |
|---|---|---|
| 治理硬限制 | [`../AGENTS.md`](../AGENTS.md) | 已生成 |
| 冻结决定 | [`decisions.md`](decisions.md) | 已生成；语义终审通过 |
| 产品范围 | [`product-scope.md`](product-scope.md) | 已生成；语义终审通过 |
| 总体架构 | [`architecture.md`](architecture.md) | 已生成；语义终审通过 |
| 导出契约 | [`export-contract.md`](export-contract.md) | 已生成；语义终审通过 |
| 状态策略与渲染 | [`state-policy-and-rendering.md`](state-policy-and-rendering.md) | 已生成；语义终审通过 |
| 数据与 Schema | [`data-and-schemas.md`](data-and-schemas.md) | 已生成；语义终审通过 |
| 流水线、存储与发布 | [`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md) | 已生成；语义终审通过 |
| OpenAI provider | [`openai-provider.md`](openai-provider.md) | 已生成；语义终审通过 |
| 搜索与排序 | [`search-and-ranking.md`](search-and-ranking.md) | 已生成；语义终审通过 |
| MCP API | [`mcp-api.md`](mcp-api.md) | 已生成；语义终审通过 |
| WebUI 与运维 | [`webui-and-operations.md`](webui-and-operations.md) | 已生成；语义终审通过 |
| 质量与测试 | [`quality-and-testing.md`](quality-and-testing.md) | 已生成；语义终审通过 |
| 安全与分发 | [`security-and-distribution.md`](security-and-distribution.md) | 已生成；语义终审通过 |
| 原始设计稿（字节不变） | [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) | 历史参考；当前哈希已验证 |

索引列出的 13 份具体文档当前均可由路径找到并已通过语义终审。原始稿执行地位始终是历史背景/最低优先级，不与新文档一起执行；其中冲突内容禁止实现。不得以默认值、原始稿或本索引代替具体契约。未来若发现冲突，必须先遵循并更新本文件、`AGENTS.md` 和 `decisions.md`，再收敛具体契约，禁止在实现中静默偏离。

## 阶段依赖与退出门

依赖链为 `R0 → R1 → R2 → R3 → R4 → R5`。每一阶段的退出条件全部满足并有证据后，才允许开始下一阶段；任何失败、未决高优先级审核或缺失契约都会阻断后续阶段。当前没有实现，因此除文档路径审查事实外，所有实现、测试、数据和发布复选框均保持未勾选。

### R0：契约冻结

**依赖**：无。

#### 任务

- [x] 写入治理硬限制、规范优先级、MVP 收缩、秘密规则和证据纪律。
- [x] 集中记录 2026-08-13 grill 后冻结的技术、范围、发布和 MCP 决定。
- [x] 写入产品范围与总体架构的可执行边界。
- [x] 具体文档已生成且零断链；当前证据为 16 个 Markdown/治理文件，本地链接目标零断链和逐路径审查。
- [x] 完成具体契约的语义终审并由主代理复审通过；最终复核为 0 Critical、0 High。
- [ ] 将 Markdown 契约物化为真实 JSON Schema 文件，并完成 strict Schema 验收；本路线图不以 Markdown 文字替代真实 Schema 文件。
- [ ] 在固定 Minecraft 基线环境中验证 Fabric/Gradle 工具链可用。
- [ ] 将 R0 之后的全部直接和间接依赖锁定到精确版本及完整校验信息。

#### 交付物

- [x] `AGENTS.md`。
- [x] `docs/roadmap.md`、`docs/decisions.md`、`docs/product-scope.md`、`docs/architecture.md`。
- [x] 13 份具体文档均存在；链接路径审查为零断链。
- [ ] 真实 JSON Schema 文件、Schema 哈希和正例/拒绝例验收报告。

#### 验证与退出条件

- [x] 高优先级文档与索引列出的 13 份具体文档语义终审通过；原始稿已按最低优先级历史背景完成冲突隔离。
- [ ] Minecraft Java `26.2`、Java `25`、Fabric Loader `0.19.3`、Fabric API `0.157.0+26.2`、Loom `1.17`、Gradle `9.5.1` 和 Mojang mappings 的运行验证记录齐全。
- [ ] 其他依赖已精确锁定，且 Windows 11 与 Linux x86_64 的复现安装验证通过。
- [ ] 所有声明的 JSON Schema 已物化为真实文件，strict Schema 验收通过并可由路径、命令和报告复核。

**证据区**

- 已有路径证据：`AGENTS.md`、4 份高优先级治理/总览文档、索引列出的 13 份具体文档和原始设计稿均可从索引路径找到；路径审查记录为 16 个 Markdown/治理文件，本地链接目标零断链。
- 已有语义证据：独立终审先后检查职责、Schema ID、发布门、provider 生命周期、MCP 只读、状态机、哈希和版本指针；最终复核结果为 0 Critical、0 High。
- 原始稿证据：当前文件 SHA 已验证、旧路径不存在；不声称仓库独立证明移动前哈希。
- R0 依赖锁证据仍缺失，必须形成并复核以下证据：
  - Gradle `gradle/wrapper/gradle-wrapper.properties`、wrapper JAR checksum、Gradle dependency locking 和 `gradle/verification-metadata.xml`；
  - Python `requirements.lock`，包含全部传递依赖及每个发行物 hash，以及生成该锁文件的输入/命令；
  - Windows 11 与 Linux x86_64 的 offline install/build 精确命令、完整输出、退出码和报告路径。
- 因工具链、真实 Schema、依赖锁和跨平台报告缺失，R0 退出门尚未通过。

### R1：确定性导出

**依赖**：R0 退出门通过；不得以未冻结的版本或未锁定依赖开始导出。

#### 任务

- [ ] 用冻结基线实现仅客户端 Fabric `Block Index Exporter`，登记 `minecraft` 命名空间 100% 注册表。
- [ ] 冻结 exporter 内部顺序为 `EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS`：exporter 唯一负责注册表枚举、代表状态选择和 Minecraft 内渲染。
- [ ] 导出合法 BlockState、默认状态、属性值、行为探测、代表性状态和标准化多视角预览。
- [ ] 为每个方块建立至少一个视觉变体，或建立带人工审计信息的跳过记录。

#### 交付物

- [ ] Fabric exporter 可复现构建产物和运行时清单。
- [ ] `manifest.json`、`blocks.jsonl`、`states.jsonl`、`variants.jsonl`、`failures.jsonl`、预览和日志。
- [ ] 导出契约、状态策略与渲染规则的实现和验证证据。

#### 验证与退出条件

- [ ] 注册表覆盖率为 100%，每个 `block_id` 唯一且属于 `minecraft` 命名空间。
- [ ] 所有导出状态均为运行时合法状态；代表集和全量导出均无未解释失败。
- [ ] 预览、蒙版、颜色、几何和行为字段可读取且哈希稳定。
- [ ] Windows 11 与 Linux x86_64 的同一输入复现检查通过。

**证据区**

- 尚无实现路径、导出包、运行日志、测试报告或哈希；本阶段所有实现和数据项保持未勾选。
- 退出门未通过，R2 不得开始。

### R2：Index Studio、存储与任务

**依赖**：R1 退出门通过；只能导入经过 R1 完整性验证的导出包。

#### 任务

- [ ] 实现 `FastAPI + Jinja2 + HTMX` 的 loopback Index Studio，并只提供 `block-index web` 启动方式。
- [ ] 冻结 Studio 阶段为 `PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE`。
- [ ] Studio 只导入/验证 exporter 已产生的 variants/renders、提取离线特征、执行 AI/审核和构建 release；Python **MUST NOT** 重选 variants 或重渲染。
- [ ] 实现冻结 SQLite schema、本地图片目录、FTS5/规范化字符串降级和数据分层存储。
- [ ] 实现进程内持久化 Worker、暂停/恢复、失败处理、心跳和逐条状态落盘；启动只检测 stale，状态变化由 WebUI `recover` 触发。

#### 交付物

- [ ] WebUI 页面、任务队列、SQLite 数据库和本地目录实现。
- [ ] 机器事实、AI 字段、人工覆盖、任务和审核记录的 Schema。
- [ ] 导入包完整性报告、任务恢复报告和操作日志。

#### 验证与退出条件

- [ ] 只能通过 WebUI 执行导入、恢复、审核、发布和回滚；Python 只有两个允许的命令。
- [ ] 应用重启后只展示 stale 检测结果；显式 recover 后任务状态可恢复，成功任务不重跑。
- [ ] SQLite 读写、图片引用、FTS 查询和跨平台路径检查通过。
- [ ] WebUI 只监听 `127.0.0.1:8765`，没有 host/port CLI 或环境覆盖，没有账号、CORS 或 CSRF 功能。

**证据区**

- 尚无源码、数据库、任务日志、WebUI 截图或测试报告；本阶段所有项保持未勾选。
- 退出门未通过，R3 不得开始。

### R3：OpenAI 标注与审核

**依赖**：R2 退出门通过；Studio 新任务、配置管理和新 release 构建只能使用一个 active OpenAI Responses profile。

#### 任务

- [ ] 实现 OpenAI Responses 图片/文本输入、`store=false`、strict JSON Schema 和最小披露。
- [ ] 能力探测必须实际证明 endpoint 接受并使用 `store=false`；不能证明时探测失败、禁止 enable，warning/ack 不得绕过硬门。
- [ ] 允许多个非活动 profile，但 Studio 新任务、配置管理和新 release 构建全局最多一个 active profile；同一 active `model_id` 用于离线标注、QuerySpec 和重排。每个 release 冻结离线标注时的 provider snapshot，MCP 不读取可变 active 状态。
- [ ] 批量生成受控语义字段；本地校验编号、Schema、枚举、描述长度和机器事实冲突。
- [ ] 失败请求最多进行一次总重试，仍失败时创建审核任务，不切换 provider 或模型。
- [ ] 实现 WebUI 异常审核、抽样审核、语义人工覆盖和可审核跳过。
- [ ] 通过 candidate-build gate 构建至少一个不可变、未激活 candidate，供 R4 临时测试使用；`excluded` 的 `qualification-review.v1` 在 candidate 构建前必须完整。

#### 交付物

- [ ] OpenAI provider 实现、请求/响应 Schema、提示词版本和缓存键。
- [ ] 审核队列、人工覆盖记录、资格等级和跳过原因报告。
- [ ] 不含 Token usage、费用或预算字段的任务与设置页面。
- [ ] 至少一个通过 candidate-build gate 的未激活 candidate（仅在实现和证据存在后勾选）。

#### 验证与退出条件

- [ ] 无效 JSON、错误编号、机器事实冲突和低置信度结果均按规则进入审核。
- [ ] LLM 无法修改 ID、合法状态、几何、机器行为、发布状态或候选资格。
- [ ] Keyring/`OPENAI_API_KEY` 读取、掩码和日志泄漏检查通过。
- [ ] 代表集标注和审核回放可复现；没有未经审核的高优先级冲突。
- [ ] candidate-build gate 通过且 candidate 未激活生产 current。

**证据区**

- 尚无 provider 请求记录、Schema 校验报告、审核记录、candidate 或测试报告；本阶段所有项保持未勾选。
- 退出门未通过，R4 不得开始。

### R4：MCP 查询

**依赖**：R3 退出门通过，并已有至少一个通过 candidate-build gate 的不可变、未激活 candidate；不得要求生产 current 已切换。

#### 任务

- [ ] 实现仅 `stdio` 的 `block-index mcp`，stdout 只输出 MCP 协议消息，诊断只写 stderr。
- [ ] 实现且仅实现 `index_info`、`search_blocks`、`get_block_details`、`compare_blocks` 四个工具。
- [ ] 实现 `current-pointer.v1` 解析：省略 `minecraft_version` 时使用 `default_minecraft_version` 对应 current，显式精确版本使用该版本 current；未知或未发布版本失败且不回退。
- [ ] 禁止 MCP 显式历史 `release_id` selector；历史切换只由 WebUI rollback 完成。
- [ ] 使用临时测试 data-root/current fixture 做 MCP 测试，不激活生产 current，不写数据库、文件、cache、logs 或 release 指针。
- [ ] 实现确定性硬过滤/评分；可选的同一 OpenAI Responses 模型解析和重排失败时返回 `reranked_by_llm=false`。

#### 交付物

- [ ] 四工具输入/输出 Schema、图片与结构化 JSON 映射、错误码和 MCP 客户端配置示例。
- [ ] 只读 release 访问层、默认版本和显式版本选择记录。
- [ ] 无 Streamable HTTP、无 MCP `resources`、无任意 SQL/文件写入的运行产物。

#### 验证与退出条件

- [ ] 四工具均只能读取不可变 release，不能写数据库、文件、日志、cache 或 `current.json`。
- [ ] 候选 ID、状态、图片编号和 release 元数据 100% 来自发布数据；模型不能增删改候选事实。
- [ ] stdout 纯净检查、默认版本、无效版本/ID/未发布版本错误检查和本地降级检查通过。
- [ ] Windows 11 与 Linux x86_64 的 stdio 启动和关闭检查通过。

**证据区**

- 尚无 MCP 源码、协议交互记录、临时 fixture、release candidate 或测试报告；本阶段所有项保持未勾选。
- 退出门未通过，R5 不得开始。

### R5：完整性收敛与首发

**依赖**：R0 至 R4 退出门全部通过；candidate check/build 不要求 R4，activation-check/apply 才要求 R0-R4、activation gate 和用户确认。

#### gate 定义

- **candidate-build gate**：只检查内容完整性，包括 100% 注册表覆盖、变体或审计跳过、图片可读、合法状态、机器与 AI Schema、人工覆盖引用、无未解决高优先级审核和 FTS。它 **不** 检查 MCP smoke、`TWO_INDEPENDENT_RELEASES` 或 current 切换。
- **activation gate**：在 candidate-build gate 之上检查四工具 MCP smoke、同一 Minecraft 版本至少两个独立通过 candidate-build gate 的不可变 release、manifest/checksums 可复算以及 `current.json` 原子切换。activation gate 通过后仍由用户人工激活。

#### 任务

- [ ] 执行 candidate-build gate，并为每个目标版本生成至少两个独立且不可变的完整 release，写入 manifest、release metadata 和统一 `sha256:<64 lowercase hex>` 哈希。
- [ ] 执行 activation gate：四工具 MCP smoke、两个独立 release 和原子 current 均通过。
- [ ] 通过 WebUI 原子更新 `current.json`，验证默认版本、显式版本选择、切换和回滚不修改历史 release；最后由用户人工激活。
- [ ] 生成首发清单和可复现运行说明；不制作安装包、容器、系统服务或自动更新。

#### 交付物

- [ ] 通过 candidate-build gate 的 release 目录、manifest、质量报告和未激活 candidate 记录。
- [ ] activation gate 的四工具 smoke、两个 release、哈希、切换和回滚证据。
- [ ] 首发前文档、源码锁依赖和空数据库/fixture 生成器分发清单。

#### 验证与退出条件

- [ ] 每个目标版本均有两个或以上独立完整 release，且通过 activation gate。
- [ ] 发布和回滚均为 WebUI 操作，release 内容在操作前后字节不变。
- [ ] MCP 四工具读取临时/当前 release 的冒烟检查通过，stdout 保持纯净。
- [ ] Windows 11 与 Linux x86_64 的源码锁依赖复现运行通过。
- [ ] 用户完成最终人工激活；在此之前不得把任何 candidate 说成首发。
- [ ] 未把黄金查询、Top-5 或排序调优结果冒充为本阶段验收；这些质量工作明确后置，不是路线图必做项。

**证据区**

- 尚无 release、manifest、`current.json`、回滚日志、首发清单或跨平台报告；本阶段所有项保持未勾选。
- 因实现、测试、真实数据和发布均未发生，MVP 尚未首发。

## 明确后置且不构成 MVP 退出条件的工作

黄金查询集、Top-5 命中率、硬约束质量统计和排序权重调优必须在 MVP 闭环之后另行定义、采样和实测；当前不预设通过率，不在本路线图中设为必做交付物，也不允许用目标数字代替实测证据。
