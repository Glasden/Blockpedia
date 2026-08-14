# 冻结决策记录

- **记录日期**：2026-08-13
- **来源**：2026-08-13 grill 后确认的项目决定及本轮统一修复决策
- **状态**：冻结，供实现和审查使用；具体文档正在语义修复/复审
- **适用对象**：Blockpedia MVP 及其本地索引、WebUI、MCP 和发布流程

本文件集中记录本次确认的决定及其直接后果，不为每一项另建 ADR。规范优先级见 [`../AGENTS.md`](../AGENTS.md)，执行顺序和证据状态见 [`roadmap.md`](roadmap.md)，产品边界见 [`product-scope.md`](product-scope.md)，组件边界见 [`architecture.md`](architecture.md)。移入的原始设计稿仅是历史背景和最低优先级参考，不能与新文档一起执行；冲突内容禁止实现。

## 决定总表

| 编号 | 冻结决定 | 直接后果 |
|---|---|---|
| D-001 | MVP 必须走通 R0、R1、R2、R3、R4、R5 的端到端闭环 | 每阶段有依赖和退出门；不得先做 MCP 或跳过发布完整性门 |
| D-002 | 正式平台为 Windows 11 x86_64 与 Linux x86_64；Linux wheel/ABI 基线为 `manylinux_2_17` / glibc `>=2.17` | Windows 在对应阶段验证；Linux wheel/ABI、安装、实际运行、平台行为和最终双平台复现统一在 R5 验证；不承诺其他平台 |
| D-003 | Minecraft 基线为 Java 26.2、Java 25、Fabric Loader 0.19.3、Fabric API 0.157.0+26.2、Loom 1.17.19、Gradle 9.5.1；26.2 使用 native Mojang names/unobfuscated，不解析外部 mappings artifact；Python 基线为 CPython 3.14.7 | 导出包必须绑定完整环境清单；版本变化必须生成新索引，不得覆盖旧版本 |
| D-004 | 默认架构为 Fabric + Python/FastAPI/Jinja2/HTMX + SQLite + 本地文件 + 进程内 Worker | 保持单机、可恢复和可复现；不引入额外服务或大型前端工程 |
| D-005 | R0 退出前只 hash-lock R0 tooling 实际引入的 Python 依赖；后续依赖使用前必须精确/hash 锁定 | Windows 在对应阶段验证；Linux 依赖安装/运行、wheel/ABI 和最终双平台复现统一在 R5 验证；不预锁未实现的 R2-R4 栈，禁止浮动版本、未锁传递依赖和以最新版代替锁文件 |
| D-006 | 只实现 OpenAI Responses、一个 `OpenAIResponsesProvider` adapter 和 strict JSON Schema | 可以保存多个 provider profile，但不实现 Chat Completions、Anthropic、其他 provider adapter、provider fallback 或多模型投票；每次请求最多一次总重试 |
| D-007 | 兼容 Responses 语义的 `base_url` 是同一 provider 的用户批准配置 | 仍只实现 `OpenAIResponsesProvider`；兼容 endpoint 必须通过同一协议能力门并实际使用 `store=false`，不能借此引入其他 API/provider |
| D-008 | `store=false` 是硬能力门 | 能力探测必须证明 endpoint 支持并实际接受/使用 `store=false`；无法证明即探测失败并禁止 enable，warning/ack 不得绕过 |
| D-009 | 机器事实、AI 语义和人工覆盖必须分层 | ID、合法状态、几何、行为和发布事实不可被 AI 改写；人工覆盖必须可追溯、可重放 |
| D-010 | 必须登记 `minecraft` 命名空间 100% 注册表 | 不得只收录候选方块；R1 没有变体时先保留 Block/State 并写 exporter machine skip/failure，candidate-build 前必须有独立可审核跳过及原因 |
| D-011 | 候选资格使用 `eligible`、`conditional`、`excluded`，并允许人工审核跳过 | 资格不能由 LLM 生成；条件候选必须带警告；排除和跳过必须带审核证据 |
| D-012 | Fabric exporter 是注册表枚举、代表状态选择和 Minecraft 内渲染的唯一执行者 | R1 exporter 内部顺序固定为 `EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS`；每个 block 只选择唯一 default `BlockState` 作为普通视觉代表，在 isolated context 渲染；Python Studio 只导入/验证 variants/renders、提取离线特征、AI/审核和构建 release，不得重选或重渲染 |
| D-013 | Studio 阶段顺序固定 | `PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE`；不得重复 exporter 职责 |
| D-014 | 多 Minecraft 版本可以并存且 WebUI 操作必须显式选择版本 | 数据、导入、任务、构建和发布绑定精确 `minecraft_version`；不得使用隐式“最新版本” |
| D-015 | 数据根目录树唯一冻结 | 只有 `exports/{minecraft_version}/{export_id}/`、`workspace/{minecraft_version}/{run_id}/`、`cache/`、`releases/{minecraft_version}/{release_id}/`、`logs/` 和根 `current.json`；不得使用高层 `work/` 或 `published/`，MCP 不写 logs 或任何持久化状态 |
| D-016 | release layout 唯一冻结 | 高层文件/目录为 `release.json`、`manifest.json`、`index.sqlite3`、`previews/`、`quality_report.json`、`manual-overrides.json`、`schemas.sha256`、`checksums.sha256`；不得使用 `release.sha256`、YAML override 或 `contact-sheets/` 契约名 |
| D-017 | JSON、Schema 字段、metadata、manifest、current 和 release 中的 hash 字符串统一为 `sha256:<64 lowercase hex>`，校验文件行首 digest 是唯一无前缀例外 | `checksums.sha256` 排除自身，格式为 `<64hex><two spaces><release-relative-posix-path>\n` 并按路径排序；`schemas.sha256` 格式为 `<64hex><two spaces><schema-id><two spaces><canonical-repository-relative-posix-path>\n` 并按 schema-id UTF-8 字节序排序，不声称路径位于 release；manifest 只哈希功能输入/产物且不自引用；`release.json` 可保存 manifest hash |
| D-018 | candidate-build gate 与 activation gate 分离 | candidate-build gate 只检查内容完整性，不含 MCP smoke、`TWO_INDEPENDENT_RELEASES` 或 current 切换；R3 可构建至少一个未激活 candidate 供 R4 临时测试；R4 不激活生产 current；R5 先建至少两个独立 candidate，再以四工具 smoke、两个 release、原子 current 执行 activation gate，最后人工激活 |
| D-019 | `current-pointer.v1` 严格顶层字段为 `schema_version`、`versions` map、`default_minecraft_version` 和 WebUI 激活/回滚时更新的 `updated_at` | MCP 省略版本时使用 default 对应 current，显式版本使用该版本 current；未知/未发布精确版本失败且不回退；MCP 不支持显式历史 `release_id` selector，历史切换只能由 WebUI rollback 完成 |
| D-020 | `current.json` 是唯一当前指针，发布和回滚只能原子切换指针 | 只有 WebUI publish/rollback 写指针；回滚不修改历史 release 内容或删除审计证据 |
| D-021 | Python 只提供 `block-index web` 和 `block-index mcp` | 导入、恢复、审核、发布、回滚不能新增 CLI 子命令，全部从 WebUI 操作 |
| D-022 | WebUI 只绑定 `127.0.0.1:8765` | 不允许 `--host`、`--port` 或环境变量覆盖 host/port；启动 CLI 只能覆盖 data root、日志等级等非冻结项；不做账号、CORS、CSRF |
| D-023 | stale 恢复必须由 WebUI 显式触发 | 启动只检测并展示 stale，不写回任务状态；`recover` 操作才可改变状态；成功任务不得重跑 |
| D-024 | MCP 只使用 stdio 和四个工具 | 只允许 `index_info`、`search_blocks`、`get_block_details`、`compare_blocks`；无 Streamable HTTP、`resources`、任意 SQL 或写入 |
| D-025 | MCP stdout 必须纯净且查询只读不可变 release | 协议消息只能写 stdout，诊断只写 stderr；不写日志文件、数据库、文件、cache、logs 或 current；模型不能改变发布数据或候选事实 |
| D-026 | OpenAI 请求必须 `store=false` 且最小披露 | 不发送密钥、本地路径、整库或无关数据；不能以 warning/ack 绕过 `store=false` 硬门 |
| D-027 | Provider profile 可保存多个但全局最多一个 active，且 active 约束只作用于 Studio 新写范围 | 非活动 profile 可用于切换；每个 release 冻结离线标注时完整非秘密 provider snapshot（`profile_id`、`model_id`、`base_url_stable_id`、不可逆 `secret_reference` 及相关版本）；MCP 不读可变 active 状态或 workspace 数据库，只按 resolved release snapshot 用同一 model 执行 QuerySpec/重排；secret 无法解析或能力不再通过时本地降级并 warning；release-bound snapshot 不算第二个 active，也不能用于 Studio 新写任务 |
| D-028 | Token usage、费用和预算不属于 MVP | 数据库、WebUI、日志和契约均不记录或展示 Token、价格、预算字段 |
| D-029 | 不使用通用 SQLite migration framework | R0 冻结一份 schema；结构变化先改高优先级契约并重建数据库，不能暗中迁移 |
| D-030 | Schema ID 按边界分命名空间并冻结当前 ID 集合 | exporter：`export-manifest.v1`、`export-block.v1`、`export-state.v1`、`export-variant.v1`、`export-failure.v1`、`render-metadata.v1`；workspace/release：`block-record.v1`、`state-record.v1`、`visual-variant-record.v1`、`annotation-record.v1`、`manual-override.v1`、`skip-review.v1`、`qualification-review.v1`、`release-manifest.v1`、`release.v1`、`current-pointer.v1`；provider：`provider-batch-envelope.v1`、`annotation-batch-output.v1`、`annotation-wire-item.v1`、`query-spec-output.v1`、`rerank-output.v1`；MCP：`mcp-index-info-output.v1`、`mcp-search-blocks-output.v1`、`mcp-block-details-output.v1`、`mcp-compare-blocks-output.v1`，可共享 `mcp-error.v1`。旧 ID 不得作为新规范当前 ID；wire ID 与 Responses name 分开，固定 name 为 `annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`；R0 要求物化真实 JSON Schema 并验收 |
| D-031 | 不把原版 Minecraft 资产放入仓库 | 公开内容只能是源码、文档、真实 JSON Schema、空数据库和 fixture 生成器源码；不得提交生成后的 PNG、非空数据库、真实索引、预览、导出包、人工覆盖或秘密；真实数据只能本地生成/保存 |
| D-032 | 原始设计稿仅为历史背景/最低优先级 | 原始稿不与新文档一起执行；其中冲突内容禁止实现，发现冲突须在高优先级文档留下影响记录 |
| D-033 | 黄金查询集、Top-5 和排序调优后置 | 不作为路线图必做项、MVP 退出门或已达成的质量声明 |
| D-034 | 不制作安装包、容器、服务或自动更新 | 交付为源码锁依赖和本地运行说明；不增加部署层 |
| D-035 | 等价技术替换必须先记录影响并获项目所有者明确批准 | 记录必须证明单机、复现、无额外服务、MCP 只读和数据契约不受破坏；未经批准不得编码 |
| D-036 | R0 物化采用最小闭合：精确字段形状唯一由 `schemas/{exporter,workspace,provider,mcp}` 下的 26 个真实 Schema 文件拥有；Markdown 只拥有核心产品、组件和安全行为，示例仅为说明 | 不重复穷举字段形状；R0 只做轻量 inventory/fixture/provider wire 基础验证，不引入通用规则引擎、额外 Schema ID、词汇 artifact、服务或 R2-R4 内部视图设计；项目 owner 已于 2026-08-13 在本会话批准该简化 |

## 关键边界的执行解释

### 机器、AI 与人工

导出器和确定性 exporter/运行时负责 `block_id`、合法 BlockState、默认状态、属性值、几何/碰撞摘要、透明度、发光、支撑、渲染和哈希。AI 只负责受控 Schema 内的中文/英文同义词、视觉描述、颜色词、形状词、材质观感、建筑用途、风格、不适用场景和解释性排序理由。人工可以审核、编辑 AI 字段、设置资格覆盖或记录跳过，但不直接修改机器事实。

LLM 的输入包含完成候选映射所必需的短编号和最小机器元数据；LLM 输出必须经过 strict Schema、编号完整性、枚举和机器冲突校验。LLM 不得产生新的 `block_id`、状态、几何或资格；失败后最多再请求一次，仍失败就创建审核任务。

### exporter、Studio 与阶段顺序

Fabric exporter 在 Minecraft 内唯一执行：

```text
EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS
```

它枚举完整注册表、为每个 block 选择唯一 default `BlockState` 并在 isolated context 渲染 variants/renders。Python Index Studio 不重新选择 variant，不重新渲染图片，只执行：

```text
PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS
→ VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE
→ HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE
```

### 发布和版本

发布目录位于 `releases/{minecraft_version}/{release_id}/`，生成后内容不可变。release 使用 `built_at`，不使用 `published_at`。`current.json` 顶层严格使用 `schema_version`、`versions` map、`default_minecraft_version` 和 WebUI 激活/回滚时更新的 `updated_at`；首次激活首个 Minecraft 版本时 `set_as_default=true` 强制，后续 apply 必须显式提供 `set_as_default` 决定是否切换 default。激活时间只写 workspace activation audit 和 current `updated_at`，不得写回 release。WebUI 用临时文件、刷新和原子替换更新它。MCP 不指定版本时解析 default 版本，指定版本时只解析该版本；未知或未发布精确版本失败，不回退。MCP 不接受历史 `release_id` selector，历史切换只能由 WebUI rollback 执行。

candidate check/build 的前置只要求 R0-R3 和 candidate-build gate；activation-check/apply 的前置才要求 R0-R4、activation gate 和用户确认。`excluded` 的 `qualification-review.v1` 完整性属于 candidate-build gate；不可变 candidate 构建前必须齐全，activation gate 只复核 candidate 报告及其 hash，不首次补做资格内容审计。

`POST /api/runs` 创建 run 时不需要 `release_build_id`；后续 release check 根据 `run_id` 创建并返回 `release_build_id`。WebUI 渲染修复操作命名为 `request_reexport` 或 `request_exporter_rerender`，不得暗示 Python rerender。run/stage 持久状态只能是 `pending|running|paused|needs_review|failed|succeeded|cancelled`，item 可额外为 `skipped`；`draft`、`ready`、`pause_requested`、`cancel_requested` 只作为命令或事件。

candidate-build gate 不包含 MCP smoke、双 release 或 current 切换。R3 可产出一个未激活 candidate，R4 通过临时 data-root/current fixture 测试四工具；R5 先构建两个独立 candidate，再做 activation gate，最后人工激活。这样不要求 R4 先激活生产 current，也不把 `TWO_INDEPENDENT_RELEASES` 放进单个 candidate build 门。

### 原始稿冲突处理

移入 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 的原始稿必须字节不变，但只作为历史背景和最低优先级参考。其中允许多个 provider、Streamable HTTP、Token 统计、费用控制、黄金集验收和文档打包等旧内容不属于当前 MVP。实现必须遵循本文件、[`../AGENTS.md`](../AGENTS.md) 和 [`roadmap.md`](roadmap.md) 的收缩决定，而不是静默采用旧内容。

## 未来重大变更规则

未来只有在确实改变冻结边界、数据契约、运行拓扑、安全边界、MCP 工具集合或发布语义时，才拆出新的 ADR。新 ADR **MUST**：

1. 先说明旧决定、变更理由、受影响文件和迁移/回滚方式；
2. 给出对本地单机、可复现性、无额外服务、MCP 只读和数据契约的影响证明；
3. 获得项目所有者明确批准后，先更新本文件、路线图和相关设计文档；
4. 再开始实现，并在路线图中以可复核证据更新复选框。

未达到上述条件的想法是未批准的变更，不能进入代码或数据。

## R1 收敛影响记录

### 2026-08-13 — R1 最小化影响记录

按项目 owner 本轮明确要求，R1 采用最小可验收实现和最小测试范围：每个方块只选择唯一 default `BlockState` 作为普通视觉代表；成功渲染时所有合法状态仍完整导出并链接到该 block-level representative，无法稳定普通渲染的项则保留 Block/State，只写 exporter failure/skip 并保持 `pending` review。固定 isolated context 生成 512×512 四视角 preview/mask。R1 不引入外置 `state_policy.yaml`、override DSL、显著属性展开、邻接矩阵、pHash/IoU/alpha dedupe、通用规则引擎、预建 block entity/NBT、任意流体、动画帧、组合邻接或通用 fixture 框架；也不把 workspace `skip-review.v1` 人工审核倒灌到 exporter。

该收敛去除逐逻辑项恢复缓存/游标、复杂幂等冲突体系、release builder 细节、16 类回归矩阵、复杂跨环境图片比较以及尚不存在的 R2–R5 测试命令；R1 只要求 fresh staging、完整校验、checksum 和成功后的原子提交，失败 staging 不得被消费者接受。它不改变本地单机运行、可复现构建、无额外服务、MCP 只读边界或既有 Schema/数据边界，不新增 Schema ID、策略文件、依赖或框架。该项不是技术替换，而是对本轮严格最小实现/最小测试要求的明确落实；用户已于 2026-08-13 明确要求并批准。

### 2026-08-14 — R1 当前 v1 身份、路径、哈希与校验职责收敛记录

这是 owner 批准的**破坏性当前 v1 契约简化**，不是保留兼容的等价替换。`export_id` 就是最终导出目录名，格式为 `export_YYYYMMDDTHHMMSSZ`，同一秒冲突时仅追加 `_01` 至 `_99`；staging 使用 `.<export_id>.staging`，成功后只做一次到最终目录的原子 rename，不再保留第二个 opaque identity、随机 UUID 或 32 位十六进制身份。每个 R1 block 只有一个 default representative，`variant_id` 等于 `block_id`；render 目录由已登记 `block_id` 直接推导，unsafe segment 保留 Block/State 并写 machine skip，不做 sanitizer、slug registry、映射文件或兼容层。旧本地导出直接废弃并重导。

R1 只在 manifest 保留 `logical_input_signature`、`render_input_signature` 以及 registry、resource、Schema 和 render-environment 证据，并保留 `checksums.sha256`。变体 render reference 只保留 preview、mask、render metadata 三个文件的 SHA-256，用于内容完整性而不参与身份或路径；`render.json` 只保留最小图片、视角、policy、fixture、tint 和 mask 语义 metadata，不重复环境或内容哈希。release 阶段由 [`AGENTS.md`](../AGENTS.md) 强制的 checksum/hash 语义不受本次 R1 简化影响。

本记录中的“已删除”字段仅用于冻结删留边界：manifest 的 `export_key`；variant render reference 的 `render_signature` 和重复的 `render_input_signature`；failure 的 `render_signature`；render.json 中重复的 camera、lighting、background、backboard、support、resource、environment hash、重复 `render_input_signature` 以及重复 image/mask content hash。它们不再作为当前 R1 身份、路径或完整性字段。

职责冻结为两层且不重复做相同全量检查：exporter commit gate 只负责最终引用/计数/状态、精确 render 路径与文件集、PNG 基础可读性和尺寸、checksum 生成、fsync 及一次原子提交；外部 Python validator 只对目录名等于 `export_id` 且不是 staging 的包执行一次 strict Schema、跨记录/registry 关系、资源黑名单、PNG 语义/质量、一次 checksum 和 artifact digest 复算，并复用同一次文件读取和 PNG 解码。commit gate 不在提交前再次对全包逐记录跑完整 Schema，也不再次复算刚生成的 checksum；任一层失败都不能宣称 R1 验收通过。

真实 validator 在 1000 renders 上已超过 600 秒，原因为 preview 重复解码/扫描。验收应改为单次读取/解码，不延长 timeout、不增加并行框架、不增加磁盘缓存；当时尚缺 Linux Java 25/runtime 与同环境独立重跑证据，后由下述 2026-08-14 阶段门重分配决定将其归入 R5。上述收敛不破坏本地单机、可复现构建、无额外服务、MCP 只读、一次原子提交或后续不可变 release/current 语义；它只要求旧导出按新命名和路径重新生成。

## 等价替换影响记录

### 2026-08-13 — Loom、mappings 与 R0 简化影响记录

Loom `1.17` 替换为精确 `1.17.19`，并将 Minecraft 26.2 mappings 澄清为 native Mojang names/unobfuscated、无外部 mappings artifact；同时批准 CPython `3.14.7` 与 Windows 11 x86_64 / Linux x86_64 `manylinux_2_17` / glibc `>=2.17`。该记录和 D-036 不改变本地单机运行、可复现构建目标、无额外服务、MCP stdio 只读边界、不可变 release/current 语义或既有数据契约；也不新增 Schema ID、词汇 artifact、服务、框架或能力。项目 owner 已于 2026-08-13 在本会话明确批准。实际工具链、Schema、锁和双平台运行报告仍需验证，未据此宣称通过。

### 2026-08-14 — R1/R5 阶段门重分配记录

项目 owner 于 2026-08-14 在本会话明确批准直接关闭 R1：以当前 Windows 11 x86_64、冻结 Java 25 基线构建证据、实际 Minecraft Java 26.2 exporter 导出和已记录的外部 validator 通过证据作为 R1 退出依据。Linux Java 25/runtime、Linux exporter 独立重跑以及最终双平台源码锁/运行时复现重新归入 R5；R1 不宣称 Linux 已通过。该重分配不移除 Linux 正式支持、不放宽 R5 验证、不新增技术、依赖或服务，也不改变既有数据契约、MCP 只读边界或本地单机运行语义。

### 2026-08-14 — Linux 验证统一归属 R5

项目 owner 于 2026-08-14 在本会话明确批准将路线图中所有 Linux 实际验证统一重分配到 R5：包括 R2 CPython 依赖安装、`pip check`、产品 Web/平台行为，R4 MCP stdio，Linux wheel/ABI（`manylinux_2_17` / glibc `>=2.17`）、Java/runtime/exporter，以及最终双平台源码锁依赖和运行时复现。该记录只改变验证时点和阶段归属，不删除 Windows/Linux 正式支持、不放宽 Linux 基线、不声称 Linux 已通过；R0-R4 只要求其对应 Windows、静态和 fixture/功能证据，不得因缺少 Linux 证据阻塞退出。

该阶段门重分配不改变本地单机运行、可复现构建目标、无额外服务、MCP stdio 只读边界、既有 Schema/数据契约、release/current 语义或秘密安全边界。R5 必须完整承接上述 Linux 义务并在最终双平台门中验证；R0-R4 的报告可以记录 Linux `deferred`，但不得记录 Linux `passed`。
