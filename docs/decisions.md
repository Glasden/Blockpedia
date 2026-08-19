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
| D-006 | 只实现 OpenAI Responses、一个 `OpenAIResponsesProvider` adapter 和 strict JSON Schema（**已由 D-038 supersede**） | D-038 改为 protocol-neutral `OpenAIProvider` 与两个显式协议 adapter；其余 provider、自动 fallback/切换和多模型投票仍不实现；每次请求最多一次总重试 |
| D-007 | 兼容 Responses 语义的 `base_url` 是同一 provider 的用户批准配置（**已由 D-038 supersede**） | D-038 改为所选协议 adapter 的用户批准 endpoint 配置；不得把 `base_url` 当作协议 fallback 或第二 provider |
| D-008 | `store=false` 是硬能力门（**已由 D-038 supersede**） | D-038 保留 Responses 请求 `store=false`、Chat Completions 省略 `store`，但 response echo 不再是 enable gate，任何协议都不证明远端存储策略 |
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
| D-026 | OpenAI 请求必须 `store=false` 且最小披露（**已由 D-038 supersede**） | D-038 保留最小披露、图片、strict output、本地校验和错误分类；Responses 使用 `store=false`，Chat Completions 省略 `store`；不声称远端存储已验证，第三方信任与策略由用户负责 |
| D-027 | Provider profile 可保存多个但全局最多一个 active，且 active 约束只作用于 Studio 新写范围（**由 D-038 修订**） | 复用现有 `adapter` 字段并限制为 `openai_responses`/`openai_chat_completions`；每个 release 冻结包含 adapter 的完整非秘密 snapshot；MCP 不读可变 active 状态或 workspace 数据库，只按 resolved release snapshot 使用同一 model 和所选协议；release-bound snapshot 不算第二个 active，也不能用于 Studio 新写任务 |
| D-028 | Token usage、费用和预算不属于 MVP | 数据库、WebUI、日志和契约均不记录或展示 Token、价格、预算字段 |
| D-029 | 不使用通用 SQLite migration framework | R0 冻结一份 schema；结构变化先改高优先级契约并重建数据库，不能暗中迁移 |
| D-030 | Schema ID 按边界分命名空间并冻结当前 ID 集合 | exporter：`export-manifest.v1`、`export-block.v1`、`export-state.v1`、`export-variant.v1`、`export-failure.v1`、`render-metadata.v1`；workspace/release：`block-record.v1`、`state-record.v1`、`visual-variant-record.v1`、`annotation-record.v1`、`manual-override.v1`、`skip-review.v1`、`qualification-review.v1`、`release-manifest.v1`、`release.v1`、`current-pointer.v1`；provider：`provider-batch-envelope.v1`、`annotation-batch-output.v1`、`annotation-wire-item.v1`、`query-spec-output.v1`、`rerank-output.v1`；MCP：`mcp-index-info-output.v1`、`mcp-search-blocks-output.v1`、`mcp-block-details-output.v1`、`mcp-compare-blocks-output.v1`，可共享 `mcp-error.v1`。旧 ID 不得作为新规范当前 ID；wire ID 与各协议 structured-output format name 分开，固定 name 为 `annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`；R0 要求物化真实 JSON Schema 并验收 |
| D-031 | 不把原版 Minecraft 资产放入仓库 | 公开内容只能是源码、文档、真实 JSON Schema、空数据库和 fixture 生成器源码；不得提交生成后的 PNG、非空数据库、真实索引、预览、导出包、人工覆盖或秘密；真实数据只能本地生成/保存 |
| D-032 | 原始设计稿仅为历史背景/最低优先级 | 原始稿不与新文档一起执行；其中冲突内容禁止实现，发现冲突须在高优先级文档留下影响记录 |
| D-033 | 黄金查询集、Top-5 和排序调优后置 | 不作为路线图必做项、MVP 退出门或已达成的质量声明 |
| D-034 | 不制作安装包、容器、服务或自动更新 | 交付为源码锁依赖和本地运行说明；不增加部署层 |
| D-035 | 等价技术替换必须先记录影响并获项目所有者明确批准 | 记录必须证明单机、复现、无额外服务、MCP 只读和数据契约不受破坏；未经批准不得编码 |
| D-036 | R0 物化采用最小闭合：精确字段形状唯一由 `schemas/{exporter,workspace,provider,mcp}` 下的 26 个真实 Schema 文件拥有；Markdown 只拥有核心产品、组件和安全行为，示例仅为说明 | 不重复穷举字段形状；R0 只做轻量 inventory/fixture/provider wire 基础验证，不引入通用规则引擎、额外 Schema ID、词汇 artifact、服务或 R2-R4 内部视图设计；项目 owner 已于 2026-08-13 在本会话批准该简化 |
| D-037 | R3 Phase C candidate check/build 契约按 owner 批准的 Recommended Oracle 方案冻结 | 只增加 WebUI 同步 check/build、check cache、逻辑 snapshot fingerprint、独立 `release-index.v1.sql`、Gate C 质量报告、release 内人工记录包和安全提交边界；保留 R3 v1 candidate 作为有效、不可变的 R3 证据，但不作为 R4 MCP 或 activation 输入；不改变 `workspace.v1.sql`、26 个 JSON Schema、依赖、服务或 Python CLI |
| D-038 | 2026-08-15 owner-approved：protocol-neutral `OpenAIProvider` 与显式 `openai_responses`/`openai_chat_completions` adapter；Responses `POST /responses`+`store=false`，Chat `POST /chat/completions` 且省略 `store`；不自动 fallback/switch，response echo 不再是 enable gate | Supersedes/revises D-006/D-007/D-008/D-026/D-027；复用现有 `adapter` 字段，不新增 required field、依赖、服务、CLI、SQL column、migration 或 Schema ID；未来才将 adapter 纳入 envelope/cache/signature/release lineage；任何协议都不证明远端 retention，旧 `openai_responses` profiles/releases immutable，in-flight cache/workspace invalidated/rerun |
| D-039 | 2026-08-15 owner-approved：忽略第三方 gateway 返回的 model identity mismatch；仍要求成功响应存在 string `model`，但不与请求的 `model_id` 比较 | `ProviderProfile.model_id` 仍是每阶段原样发送并用于 cache/run/envelope/release lineage 的 requested identity；返回 model 只作不可信结构字段，不持久化、不展示为已验证实际模型、不替换配置值；不自动切换/fallback，不新增字段、依赖、Schema、SQL、服务、CLI、migration 或 release rewrite |
| D-040 | 2026-08-15 owner-approved：手动逐批默认；一次明确 WebUI 确认只授权 unchanged frozen remaining batch plan 的自动顺序提交，send concurrency 后由 D-044 修订；item-local provider failure 继续，fatal provider/config/auth/capability failure 立即停止；支持一次 WebUI bulk retry wave | 复用现有 per-job cursor 的 `approved`、payload signature、audit、`cursor_json` 和 job lineage；D-044 只替换其 send concurrency=`1` 部分，其余不新增 state、DB column/table/migration、config schema、JSON Schema ID/field、dependency、service、CLI、protocol/model fallback 或 retry budget；retry 使用 terminal AI job 的 child cursor/generation，并保持原始 evidence/request rows 可见 |
| D-041 | 2026-08-15 owner-approved：aggregate plan preview/confirmation 使用已持久化 pending job identity，完整 one-batch rebuild 延迟到每次实际 send 前；仅 supersede D-040 的 aggregate preview/confirmation 全量重算要求 | 复用现有 `job_id`、`logical_key`、`input_signature`、`cursor_json` 中的 payload/hash/tile/variant 数据、`effective_config_hash` 和 frozen provider snapshot；保留 `recomputed_payload_signature` 名称及既有 plan-hash object；不新增 state、SQL、migration、config field、Schema/version、dependency、service、CLI、protocol 或 retry budget |
| D-042 | 2026-08-15 owner-approved：`prompt.v2` 的首阶段 slim model-visible annotation text、兼容 legacy prompt、最终 Schema diagnostic 和现有 evidence/API/UI 脱敏边界 | 既有 wire/Schema、envelope、cache/signature/release lineage、full local validation 和一次 retry budget 不变；`prompt.v1` 与其它历史 prompt version 原样 replay，只有 exact `prompt.v2` 选择新 text；不新增 provider field/column/table/migration/Schema ID、依赖、服务、CLI 或自动迁移 |
| D-043 | 2026-08-16 owner-approved：R1 Phase 1 最小渲染纠错、`render.v2` 当前策略、透明 edge-on quadrant/材料真相/动画确定性和历史证据边界 | 当前 exporter 使用 `render.v2`；既有 `render.v1` Schema ID 同时接受 v1/v2，历史导出与 R3 run 不原地修改；仅修正 exporter 运行时与其必要契约，保留无 block-entity fixture 范围、两次同环境导出证据门和 Linux→R5，不继续旧导出 candidate 工作；stable pre-render selection token 进入 `logical_input_signature`，replacement exports 不复用旧签名 |
| D-044 | 2026-08-16 owner-approved：Phase 1 有界批次并发、发送线性化、进程级共享 executor 与 pristine same-run reconfiguration | 仅 supersede D-040/D-041 的 send concurrency=`1`；`offline_annotation` 为整数 `1..5`、默认 `1`，`query_spec`/`visual_rerank` 固定 `1`；保留 logical-batch 两次总尝试、无 fallback、顺序授权、TOCTOU、审计、恢复和既有数据契约；不新增服务、队列、per-run executor、adaptive concurrency、SQL/Schema/migration、状态、依赖、CLI 或 retry 语义 |
| D-045 | 2026-08-17 owner-approved：精确 32 个 standing/wall banner 的 targeted complete replacement export 与当前 run refresh；`camera.v2`/banner-camera policy；混合 export lineage 与最小增量 AI 工作 | 仅为本次精确操作 supersede D-043 的 camera category-invariance 与 historical-run no-refresh boundary；其它 D-043/D-044 边界、`render.v2`、完整导出校验和既有数据契约均保持有效；不新增 partial package、Schema ID、迁移、服务、队列、产品 CLI 或通用 patch framework |
| D-046 | 2026-08-18 owner-approved：纠正 MCP JSON-RPC 错误分类；未知 RPC method 为 `-32601`，合法 `tools/call` 中的未知 tool name 为 Invalid Params `-32602` | 只修正协议错误映射和验收表述；不改变工具集合、transport、Schema、服务、CLI、持久化、release 语义或只读边界 |
| D-047 | 2026-08-18 owner-approved：保持 `mcp-error.v1` 既有 `error_code` enum 不变，纠正 MCP 输入、空结果和 provider 降级的错误分层 | 输入 shape 错误使用 JSON-RPC `-32602`；正常空搜索和 `rerank=auto` provider failure 为成功 warning；`rerank=required` 仅用顶层 `RERANK_REQUIRED_UNAVAILABLE`，provider code 仅在 `details.provider_error_code`；不新增 Schema 或 fixture |
| D-048 | 2026-08-18 owner-approved：为未来 R4/R5 candidate 增加 fresh-only `release-index.v2.sql`，保留 R3 `release-index.v1.sql` 历史证据但禁止 MCP/activation 使用 | v2 保留 v1 scalar/indexed columns 并增加 validated record/feature JSON columns，`schema_meta.format_version=2`；只 fresh build、不迁移/改写旧 release；MCP 遇 v1 返回 `RELEASE_INTEGRITY_FAILED` 且 `details.integrity_component="index"`；不新增 JSON Schema、服务、CLI 或状态 |
| D-049 | 2026-08-18 owner-approved：MCP tool input 的 `minecraft_version` 改用严格版本格式 pattern，不再用 `const: 26.2` | 格式非法返回 JSON-RPC `-32602`；格式合法但未发布版本返回 `VERSION_NOT_AVAILABLE` 且不回退；不增加当前版本支持或改变 Minecraft 26.2 baseline |
| D-050 | 2026-08-18 owner-approved；2026-08-19 owner-approved amendment：当前 R4 的 `context.family` 没有 schema-owned `family_id`/family catalog；family dedupe 为确定性 no-op | `context.family=null` 或任何通过 input schema 的 string 都不分组、不限额且保持 Top-24 后稳定顺序；不产生 family warning/metadata；不得推断/创建 family ID；非 string/null 仍是 JSON-RPC `-32602` input shape error；不新增 Schema、字段、数据、服务、CLI 或 migration |
| D-051 | 2026-08-19 owner-approved：MCP `search_blocks` 可接收可选、顶层、host-supplied `query_spec`，作为不可信且临时的 QuerySpec 输入 | 输入必须完整符合既有 `query-spec-output.v1`；严格/未知字段失败使用 JSON-RPC `-32602`，语义/不变量失败保持 `QUERY_INVALID`；有效输入只抑制服务端 QuerySpec 生成，不持久化、不选择 provider identity；本地 hard 约束、`local_only`、release-bound visual rerank、四工具/stdio/只读边界和既有重排降级不变 |

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

## R3 provider 契约纠错与生命周期收敛记录

### 2026-08-14 — owner 批准的 R3 契约纠错

项目 owner 明确批准以下 R3 纠错与生命周期收敛，作为当前契约执行，不新增兼容层、服务、migration 或 Schema ID：

1. `provider-batch-envelope.v1` 删除 `vocabulary_version` 和 `vocabulary_sha256` 的 required/properties；不新增 vocabulary artifact，继续保持 26 个 Schema 和 52 个 fixtures。`query_spec` stage 的 `input_summary` 必须精确为 `query_sha256` 单字段；`offline_annotation` 仍只接受 tile-to-variant 映射，`visual_rerank` 仍只接受完整候选映射。
2. `QuerySpec` 的精确字段、required 集合和枚举以真实 `query-spec-output.v1` Schema 为唯一 owner。文档不再复制字段定义；`source_model_id`、`source_prompt_version`、`values`、`exclude_behaviors` 和 `require_behaviors` 不属于当前 wire Schema。可信 model/prompt 只来自 profile、provider envelope 或 release manifest snapshot。
3. provider snapshot 只写入 `release-manifest.v1` 对应的 `manifest.json`；`release.v1` 对应的 `release.json` 只使用其 Schema 允许的 release identity、`manifest_sha256` 及其它 release 元数据，不承载 provider snapshot。
4. `store=false` probe 只有原始 provider response 顶层正式 `store: false` 回显才算通过；请求体、HTTP 2xx、fake endpoint、warning/ack 均不足以证明能力。无回显必须 fail closed；用户批准的兼容 endpoint 使用完全相同的门。
5. 全局非秘密 profiles 原子保存于 `<data-root>/cache/provider-profiles.json`；workspace 的 `provider_profiles` 只保存 run snapshot。`POST /api/runs` 配置并启动 import 已预留的同一 run，不分配第二 workspace/run。

上述变更不破坏本地单机运行、可复现构建、MCP 只读边界、data-root 高层布局或既有 Schema ID；项目 owner 已于 2026-08-14 在当前会话明确批准。

## R3 Phase C 契约补充影响记录

### 2026-08-15 — owner 批准的 R3 Phase C freeze (Recommended)

项目所有者明确回复 **“批准 R3 Phase C freeze (Recommended)”**。本记录冻结 R3 Phase C 的高优先级契约，依据为 [`webui-and-operations.md`](webui-and-operations.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)、[`quality-and-testing.md`](quality-and-testing.md)、[`data-and-schemas.md`](data-and-schemas.md)、[`security-and-distribution.md`](security-and-distribution.md) 和 [`architecture.md`](architecture.md)。这是已批准的契约收敛，**不是技术替换**，不触发兼容层、迁移或旧实现保留义务。

Phase C 只冻结两个同步 WebUI 操作：严格 `POST /api/releases/check` 与严格 `POST /api/releases/build`。check 的 authoritative cache 为 `cache/release-checks/{check_id}/state.json` 和不可修改的同目录 `quality_report.json`；build 只接受最新、未 stale 且 `can_build=true` 的 check，并从该逻辑 snapshot 派生新的 release quality report。发布目录使用独立的 `release-index.v1.sql` 投影契约；`schema_meta.format_version=1` 不是 JSON Schema ID。`manual-overrides.json` 只保存原样完整的三类已审核记录，quality/manual 文件均不新增 Schema ID。`schemas.sha256` 只列 Phase C 实际使用的 Schema，不列 `current-pointer.v1` 或 MCP Schema；`checksums.sha256` 除自身外覆盖 release 中全部普通文件。

本补充明确不改变 `workspace.v1.sql` 和现有 26 个 JSON Schema，不新增 Python 依赖、运行服务、产品 CLI、数据库 migration framework、R4 MCP 实现或 R5 验证要求。Phase C 不实现、不测试 activation、`current.json`、MCP 或第二个 release；这些后续边界只由路线图和对应后续阶段契约引用。该收敛保持本地单机运行、可复现构建、无额外服务、MCP 只读边界和既有数据分层不变。

## D-038：双 OpenAI 协议 adapter 与隐私表述修订

### 2026-08-15 — owner-approved replacement

项目所有者于 **2026-08-15** 在本会话明确批准以下替换；本记录显式 supersede/revise D-006、D-007、D-008、D-026 和 D-027，并作为 D-035 要求的影响记录。它是冻结 MVP provider 边界的治理修订，不代表实现、Schema、测试或 R3 退出证据已经存在。

1. Provider 层使用 protocol-neutral `OpenAIProvider`。每个 profile 复用现有 `adapter` 字段，且只能取显式枚举 `openai_responses` 或 `openai_chat_completions`；每个值对应明确的 wire adapter/codec。禁止自动 protocol fallback/switch、Anthropic/其他 provider adapter 和 model voting。
2. `openai_responses` 使用 `POST /responses`、图片输入、strict JSON Schema structured output 和 `store=false`；`openai_chat_completions` 使用 `POST /chat/completions`、图片输入、strict JSON Schema structured output，并省略 `store`。两种协议使用同一 model 覆盖 offline annotation、QuerySpec 和 visual rerank 三阶段，使用同一总 retry budget、local validation、stable error classification 和 minimal disclosure。
3. Responses response echo 不再验证，也不再是 enable gate。任何协议都不能证明远端 retention；规范和实现不得声称 storage 已验证，第三方服务的 trust、retention 和 policy 由用户负责。能力探测仍须按所选协议证明 endpoint、图片输入、strict output 和错误分类能力，但不能依赖 storage echo 推导远端策略，也不能自动改用另一协议。
4. 不新增 required profile field、dependency、service、CLI、SQL column、migration framework 或 Schema ID。现有 `openai_responses` profiles/releases 仍然有效且不可变；变更前的 in-flight caches/workspaces 不迁移，必须 invalidated/rerun。未来实现才把 adapter 纳入 envelope、cache、signature 和 release lineage，并按协议使用 conditional `store`；当前记录不宣称这些代码已存在。

### D-035 impact proof

- **Local single-machine operation**：仍是现有 Python/FastAPI/SQLite/local files/in-process Worker；两个协议只替换同一 provider boundary，不增加运行服务或远程状态依赖。
- **Reproducibility**：adapter、wire codec、endpoint、model、prompt/schema versions、retry budget 和 protocol-conditional store 行为进入未来的 request/cache/signature/release lineage；旧 immutable release 不原地修改，旧 cache/workspace 通过 invalidation/rerun 重新获得确定性输入。
- **No extra service**：不新增 package、daemon、queue、database 或 CLI；复用现有 `adapter` profile field、provider schemas 的现有 IDs 和 data-root。
- **MCP read-only**：MCP 继续只读 resolved immutable release snapshot，不读可变 active profile/workspace，不写 database、files、cache、logs 或 current；协议 adapter 不扩大 MCP tool 或 selector 边界。
- **Data layering and release immutability**：机器事实、AI semantic suggestions、manual overrides 仍分层；release-bound provider snapshot 增加 adapter 语义但 release 生成后仍 immutable，旧 `openai_responses` release 不迁移、不重写。

## D-039：第三方 gateway model identity mismatch 信任边界

### 2026-08-15 — owner-approved amendment

项目所有者于 **2026-08-15** 明确批准本项修订。观察到第三方 gateway 可能返回与请求不同的 string `model`；Blockpedia 继续把 profile 中精确配置的 `model_id` 原样发送到三个 stage，并把它作为唯一 requested identity。成功的 Responses/Chat 响应仍必须包含 string `model` 才满足结构有效性；缺失或非 string 仍按既有 `PROVIDER_MODEL_UNAVAILABLE` 或等价 fail-closed structural error 处理，但不同 string 不再使 probe/request 失败。

返回的 model 是不可信 informational echo：不得持久化、展示为已验证的远端实际模型，或替换 annotation、cache、run、provider envelope、release lineage 中的 configured `model_id`。Blockpedia 不声称第三方确实执行了 requested model；第三方路由和模型身份与 retention 一样属于用户负责的 trust/policy 边界。Blockpedia 仍不自动 model switching/fallback，仍只发送 configured `model_id`。

### D-035 impact proof

- **Local single-machine operation**：仍使用现有 Python/FastAPI/SQLite/local files/in-process Worker；仅放宽不可信 response echo 的 equality check，不增加服务或远程状态依赖。
- **Reproducibility**：可复现性限定为 configured/requested `model_id`、所选 adapter/wire、输入和本地 validated output 的可复现；不能宣称或验证远端实际执行的模型身份。requested `model_id` 继续进入 cache/run/envelope/release lineage，旧 immutable release 不改写。
- **No extra service**：不新增 profile field、依赖、服务、CLI、SQL、migration、Schema ID、cache 字段或 release rewrite。
- **MCP read-only**：MCP 继续只读 resolved immutable release snapshot，不读取可变 active profile/workspace，不写 database、files、cache、logs 或 current。
- **Data layering and release immutability**：返回 echo 不进入 AI semantic、machine-fact 或 manual-override 层；配置的 requested `model_id` 继续作为 release-bound lineage，既有 release 保持 immutable。

## D-040：批次授权、顺序 drain、致命停止与 Provider 重试波次

### 2026-08-15 — owner-approved workflow contract

项目所有者于 **2026-08-15** 明确批准本项持久工作流契约：手动 per-batch mode 仍是默认；一次明确的 WebUI confirmation 可以授权 unchanged frozen remaining batch plan 自动 sequential submission；send concurrency 的当前边界由 D-044 取代；item-local failure 继续到下一个 approved batch；fatal provider/config/auth/capability failure 在后续 send 前停止；一个 WebUI action 可以为全部 eligible Provider-failed AI batch records 创建一个 bulk retry wave。本记录冻结语义，不代表实现、迁移、真实 provider 请求或测试证据已经存在。

1. **不扩张契约面**：不新增 state、DB column/table/migration、config schema、JSON Schema ID/field、dependency、service、CLI、protocol/model fallback 或 retry budget。manual per-batch approval 仍可单独使用；auto mode 不是新的持久 mode、stage、cursor 或 config snapshot。
2. **计划授权**：计划只使用现有每个 job 的 cursor `approved`、recomputed payload signature 和 audit。计划 hash 精确为对以下 JCS canonical JSON 的 SHA-256：

   ```json
   {
     "run_id": "<run_id>",
     "effective_config_hash": "<effective_config_hash>",
     "jobs": [
       {
         "job_id": "<job_id>",
         "logical_key": "<logical_key>",
         "recomputed_payload_signature": "<signature>"
       }
     ]
   }
   ```

   `jobs` 必须按冻结计划顺序排列，不得加入其它参与 hash 的字段。确认前所有计划 batch 必须可 inspect，并且计划绑定 immutable plan hash、run-frozen provider 和 requested `model_id`。一次 SQLite transaction 必须重新计算所有 payload signature；任一 TOCTOU、pending 集合、config/provider/requested model 或 hash 不一致时 approve none；全部仍为 included `pending` 且一致时，设置所有 included jobs 的 cursor `approved`，写入 one plan audit 和每个 job 的 approval audit。Worker 在每次 send 前立即再次检查。
3. **顺序和 lineage**：确认只授权确认时仍 unchanged 的 frozen remaining plan；Worker 的 send concurrency 遵循 D-044。Worker 必须使用 run-frozen profile；可变 global active profile 只作用于新的 Studio work/profile management，绝不能替换已有 run 的 adapter、model 或 base URL。任一 payload/config lineage 改变都会使 approval 对 send 无效；startup 不清除持久化 auto-approved cursor，只有显式 WebUI `recover` 才能改变 stale state。
4. **逐项与致命错误**：以下 item-local Provider errors 都是 high `needs_review`，但不阻塞 AI_ANNOTATE drain：`PROVIDER_NETWORK_ERROR`、`PROVIDER_TIMEOUT`、`PROVIDER_RATE_LIMITED`、`PROVIDER_SERVER_ERROR`、`PROVIDER_SCHEMA_INVALID_REPAIRABLE`、`PROVIDER_SCHEMA_INVALID`、`PROVIDER_REQUEST_INVALID`、`PROVIDER_PAYLOAD_TOO_LARGE`、`PROVIDER_REFUSAL`、`PROVIDER_INCOMPLETE`、`PROVIDER_OUTPUT_ID_MISMATCH`、`PROVIDER_MACHINE_FACT_CONFLICT`、`PROVIDER_UNKNOWN`，以及 Worker-local `PROVIDER_CACHE_KEY_INVALID`、`IDEMPOTENCY_CONFLICT`。旧 `PROVIDER_STORAGE_UNSUPPORTED` 若意外出现，按 `PROVIDER_UNKNOWN` high review 处理。`PROVIDER_CANCELLED` 是 control signal，不属于 bulk retry。Fatal codes 为 `PROVIDER_NOT_CONFIGURED`、`PROVIDER_CONFIG_INVALID`、`PROVIDER_CAPABILITY_MISSING`、`PROVIDER_AUTH_FAILED`、`PROVIDER_PERMISSION_DENIED`、`PROVIDER_MODEL_UNAVAILABLE`；fatal 必须 atomically persist request evidence、review、job `failed`、stage `failed`、run `failed` 和 audit，并在 later sends 前停止。
5. **阶段语义**：item `needs_review` 不会停止 AI_ANNOTATE drain；valid low-confidence item 和上述 item-local failure 都继续进入 `VALIDATE`，再进入 `HUMAN_REVIEW`。只有 fatal 才立即终止 AI_ANNOTATE 和后续发送；其余审核仍由 candidate gate 阻断，直到人工处理。
6. **Provider retry generation**：retry source 必须是 terminal `needs_review|failed` 的 AI job，且具有 eligible item-local Provider error；fatal codes、`PROVIDER_CANCELLED` 和没有 Provider error 的 job 不 eligible。不能以单个 variant review 作为 source。source 必须是 leaf（没有 child）；新 child cursor 必须包含 `retry_of_job_id`，nonce 必须由 source `job_id + input_signature` 确定性生成，每个 source 只能生成一个 child；failed child 可作为下一次显式 generation 的 source。创建时在同一 transaction resolve source 的所有 open provider-review siblings，同时保留原 job、evidence 和 provider request rows。重复 row/bulk POST 必须 idempotent；legacy retry rows 只兼容读取，不重写。Generic `retry-failed` 必须排除 fatal/provider AI jobs，不能把同一 logical request 重跑超过两次总尝试预算。
7. **UI 与控制**：所有 actionable `running`/`failed`/`needs_review` rows 必须在同一 scrollable work area 内可见；pending/recent summary 可以 bounded。Provider error row 提供 row retry；一次 confirmed bulk action retry all eligible failed leaf batches 并 auto-approve that retry wave。succeeded 和没有 Provider error 的 low-confidence `needs_review` 不得进入该 wave；原始 evidence 始终可见。pause/cancel 只停止 future sends；SSE/browser disconnect 不停止工作。

### D-035 impact proof

- **Local single-machine operation**：只复用现有 WebUI、SQLite transaction、`cursor_json`、进程内 Worker 和 audit；不会引入队列服务、后台常驻服务或远程授权服务。
- **Reproducibility and lineage**：计划 hash 只绑定 run、effective config 和 ordered recomputed payload signatures；run-frozen provider/requested model 与 immediate pre-send recheck 防止 active profile 或 TOCTOU 改写既有 run。retry child 以 source job/input signature 生成确定性 identity，旧 evidence/request rows 保持可回放。
- **No extra service or schema**：`approved`、payload signature、audit、existing job cursor/lineage 足以表达授权和 generation；不增加 SQL、migration、JSON Schema、config field、dependency、CLI 或 retry budget。
- **MCP read-only and release immutability**：该契约只作用于 WebUI/Worker workspace workflow；MCP 仍只读 resolved immutable release，不读取可变 active profile/workspace，也不写 current、release 或日志。
- **Security and recovery**：一次确认不是永久授权；计划/lineage mismatch fail closed，fatal failure 以同一 transaction 写齐证据并停止 later sends；startup stale detection 仍 read-only，只有显式 `recover` 改变 stale state。项目所有者已于 2026-08-15 明确批准，实际实现和 focused acceptance evidence 仍待对应 R3 工作完成。

## D-041：聚合计划预览的持久身份与最终发送前重建

### 2026-08-15 — owner-approved amendment

项目所有者于 **2026-08-15** 明确批准本项 amendment。动机是一次真实运行中有 `118` 个 pending batches；只读 aggregate plan preview 超过 `180` 秒，而单个 per-batch preview/send 仍然有界。本记录不保存 endpoint、profile、local secret 或本地数据细节。D-041 **只 supersede D-040 中“aggregate plan preview/confirmation 必须为全部 jobs 重新生成 contact sheet/prompt/payload signature”的要求**；D-040 的 manual default、D-044 当前 send concurrency、item-local continue、fatal stop、retry、审计和其余授权语义全部保持有效。本 amendment 是契约冻结，不代表实现或测试已经完成。

1. **Aggregate plan identity**：aggregate plan preview/confirmation 使用已经持久化的 pending job identity：`job_id`、`logical_key`、`jobs.input_signature`、cursor 中的 `payload_signature`/`input_hash`、cursor 中的 `tile_ids`/`variant_ids`、run `effective_config_hash` 和 frozen provider snapshot。系统必须验证这些持久化 hash 彼此一致且有效；任何缺失、冲突或无效都 fail closed，并在 confirmation transaction 中 approve none。aggregate path **MUST NOT** 为全部 pending jobs rebuild images、contact sheets、prompt text 或 machine metadata。
2. **Plan-hash compatibility**：既有 canonical plan-hash object 和 field name `recomputed_payload_signature` 必须保留，不引入另一种 format、Schema 或 version；D-041 定义该字段在 plan time 的值为“通过持久化 hash 校验的 persisted payload signature”。它不是对所有 jobs 重新构建后得到的临时 signature。
3. **Lazy individual inspection**：aggregate confirmation 前，每个 planned batch 仍必须可以通过既有 safe preview 完整 inspect；该 one-batch preview 可以重建其有界 payload、图片/联系表、prompt 和 machine metadata，不得把所有 batch 的重建合并进 aggregate operation。
4. **Final TOCTOU gate**：在 **每一次** actual external send 前，Worker 仍必须依据 frozen run profile rebuild complete one-batch payload/contact sheet/prompt/machine metadata，recompute full signature，并与 approved job signature 比较。任一 mismatch 必须 revoke that job approval、在任何 HTTP request 前 pause，并不得发送该 batch；该 gate 不被 aggregate persisted identity shortcut 绕过。
5. **Unchanged remainder and scope**：一次 confirmation 仍只授权 displayed unchanged persisted plan；confirmation 前 DB job、persisted signature、pending identity 或 config/provider lineage 改变时，沿用 D-040 all-or-none conflict。manual mode/default、D-044 当前 send concurrency、item-local continue、fatal stop、retry generation/idempotency/sibling resolution、图片与 structured output、no fallback 和 audit 不变。
6. **No expansion**：不新增 schema、SQL column/table、migration、persisted state、config field、dependency、service、CLI、protocol/model fallback 或 retry budget；不改 `recomputed_payload_signature` 的格式或字段名。
7. **Focused evidence**：验收使用 call-count/monkeypatch evidence，而不是 flaky wall-clock threshold：aggregate preview/confirm 对 `100+` pending jobs 不调用 per-job contact-sheet/prompt rebuild；persisted signature mismatch approve zero；pre-send full recompute mismatch 产生 zero provider calls；per-job preview 仍展示 exact safe text/image/metadata。

### D-035 impact proof

- **Local single-machine operation and performance**：aggregate operation 只读取并验证已有 SQLite job/cursor identity，完整重建保留在单个 batch 的 bounded preview/send；不引入服务、并发队列或远程依赖。
- **Reproducibility**：plan hash 的 canonical object 和 `recomputed_payload_signature` 名称不变；plan-time value 明确来自 validated persisted signature，实际发送前仍由 frozen profile 完整重建和复算，避免以性能优化削弱最终 TOCTOU gate。
- **No schema or data-contract change**：复用现有 job/cursor/config/provider snapshot 和 audit；不增加 SQL、migration、state/config field、Schema/version、依赖、CLI 或 retry budget。
- **Security and recovery**：每个 batch 的 lazy safe preview 保留用户可检视性；persisted mismatch fail closed，pre-send mismatch 在 HTTP 前 revoke/pause；D-040 的 all-or-none confirmation、fatal stop、stale read-only 和 explicit recover 语义不变。

## D-042：`prompt.v2` 首阶段 slim annotation text 与最终失败诊断

### 2026-08-15 — owner-approved amendment

项目所有者于 **2026-08-15** 批准本项第一阶段简化。当前运行证据摘要为 `8` 个 terminal batches（`4` transport、`4` final schema invalid）、`0` artifacts、`118` pending；本记录不推断错误成因，不记录 endpoint、profile 或本地 ID。D-042 只冻结新 prompt version、最终诊断保留和兼容边界，不声称实现、重跑或 R3 退出已经完成。

1. **Prompt version compatibility**：现有 frozen runs/releases 必须保留其 `prompt_version` 和精确 legacy prompt behavior；`prompt.v1` 必须可 replay。禁止原地修改、自动迁移或为当前 pending jobs 自动 re-sign。只有 exact `prompt.v2` 选择新行为；其它已有历史 version string 继续使用 legacy behavior。使用 `prompt.v2` 必须创建新的 run/profile snapshot。
2. **`prompt.v2` model-visible input**：trusted instruction 必须要求 annotate existing tiles、逐个复制 tile 已有的 exact `variant_id`、绝不创建/修改 ID 或 machine facts；contact sheet 和 tile labels 保留。模型可见 `tiles` 只含 `tile_id`、`variant_id`；每 tile metadata 只含 `tile_id` 和一份去重、有界的 `geometry_classes`。模型 text 移除 `image_sha256`、`machine_metadata_sha256`、`block_id`、`canonical_state_id`、exact dimensions/volume、全部 behavior booleans/emission、`machine_tags`、feature metrics、`feature_extractor_version`、feature `input_sha256` 以及重复的 feature geometry/tags。完整 machine metadata、hashes、source images、envelope/cache/signature/release lineage 仍只在本地，校验规则不变。
3. **Wire compatibility**：D-042 不修改 current output wire/Schema；模型仍返回 `schema_id`、`variant_id` 和当前 `annotation-wire-item.v1` 要求的全部 13 个 item fields。local `schema_id` injection、`tile_id` codec 和 semantic-field reduction 不在本项实现；只有 diagnostics 足以支持另一个 owner decision 并物化相应 Schema change 后，才能改变它们。
4. **Final diagnostic**：只有 FINAL annotation validation 在总 retry budget 用尽后仍失败时，才可产生一条 sanitized diagnostic。allowlist 严格为：`stage`（`offline_annotation`）、`phase`（`json_parse`、`output_shape`、`wire_schema`）、`path`（有界 JSON path，parse/shape 使用 `$`）、`keyword`（有界稳定 validator/parse keyword）、`observed_type`（allowlisted JSON type 或 `missing`）、`observed_length`（非负有界 integer 或 `null`）。禁止 raw/prefix/value、provider message、exception text、repair context、prompt/image/secret 和 response/value hash。第一次 repairable failure 在第二次成功时不得持久化。
5. **Diagnostic path and retention**：diagnostic 通过 internal `ProviderResult` 传递，并追加到既有 `PROVIDER_FAILURE` `review_tasks.evidence_json`，保留现有 job/provider request refs。provider envelope、`provider_requests` column、table/report、migration 和 Schema ID 均不增加；既有 evidence rows 继续有效。Review/API/UI 只可用普通 labels 展示这六个字段，不能从 `path` 派生或渲染 raw value。
6. **Validation boundaries**：Provider-side full wire validation 继续用于 repair；Worker-side full validation 继续用于 persistence/custom providers；ID/hash/cache/annotation-record/variant/`VALIDATE`/release boundaries、local `uniqueItems`、max-one-retry 全部保留。只移除 freshly produced by `_hash_json` hash 上的 tautological regex check；其它 diagnostic merge/move 只有在 externally observable classification 不变时允许。
7. **Lineage and pending jobs**：`prompt.v2` 通过 frozen `prompt_version` 改变 signature/cache identity，必须使用 fresh run。当前 `118` pending v1 jobs 保持 untouched/paused，不自动 cancel、delete 或 re-sign。
8. **Focused evidence**：两种 adapter 都须以 v2 text 发送 exact allowlisted model-visible fields，同时 local envelope/hash checks 仍使用 full metadata；验证 v1 byte-for-byte/current behavior compatibility 与 source-change TOCTOU；malformed JSON、missing required、wrong type、additional property、duplicate-array 的 final failure 只保留六个安全诊断字段，successful repair 不保留诊断；DB/API/UI 不出现 raw output/value/prefix/secret/path-like value；现有 Provider/Worker/release validation 继续通过。prompt size comparison 只能作为 evidence，不得写成 quality claim。

### D-035 impact proof

- **Local single-machine operation**：只调整选定 prompt version 的模型可见输入和已有 review evidence 传递，不增加服务、依赖、CLI、数据库结构或远程状态。
- **Compatibility and reproducibility**：`prompt.v1` 与其它历史 version string 保持 byte-for-byte legacy replay；`prompt.v2` 通过新 run/profile snapshot 进入现有 signature/cache/release lineage，旧 release、旧 pending jobs 和旧 evidence 不原地改写。
- **Data contract and validation**：wire Schema、13 个 item fields、provider envelope、full local validation、ID/hash/cache/release gates 不变；diagnostic 是既有 review evidence 的六字段 allowlist，不是新的 Schema 或 provider payload。
- **Security and disclosure**：v2 仅减少 model-visible metadata；完整机器事实、hash、图片和 lineage 留在本地。最终失败只保留六个有界字段，成功 repair 不落诊断，API/UI 不回显 raw value、secret、prompt、image 或 provider body。

## D-043：R1 Phase 1 exporter 渲染纠错与历史兼容

### 2026-08-16 — owner-approved Phase 1 contract

项目所有者批准本项最小 exporter 纠错范围；本项随后由 Gate 3 closure 条目记录了最终 corrected runtime evidence，但不代表 R3、candidate 或人工审核退出证据已经存在。两个旧导出、各自 validator 报告和现有 R3 run 必须原样保留为历史证据；不得从它们继续 candidate 工作。现有 R3 run 虽有 `1000` 个已验证 annotation，其渲染输入来自缺陷 exporter，必须视为历史/被 supersede 的 run；其中 `152` 个 rerender events 继续作为审计证据，不能执行为本次修复，也不能删除或静默解决。

上述 historical-run no-refresh boundary 仅由 D-045 对其明确的 32 个 banner targets 和一次显式 `banner-export-refresh` operation supersede；D-043 的其它历史证据、candidate 边界和 renderer evidence 要求不变。

1. **最小渲染纠错**：当前 exporter policy 固定为 `render.v2`。四视角 composite 中允许一个或多个透明的 edge-on quadrant；只要 composite 非空就保留该变体，`nether_portal` 也按此规则保留。整个 composite 全透明仍然失败。不得用本项引入 block-entity/NBT fixture、邻接组合、任意流体或其它超出当前范围的夹具；预期仍保留 `43` 个 block-entity fixture skips 和 `10` 个 invisible/technical skips。
2. **Schema 与 replay 兼容**：现有 `export-manifest.v1`、`export-variant.v1` 和 `visual-variant-record.v1` Schema ID 不变，其 active `render_policy_version` 接受 `render.v1` 与 `render.v2`。未修改的历史 `render.v1` records、workspace/release data 在当前 v1 Schema ID 下必须保持 valid，并可在其 record/run context replay；新的/current fixtures 和后续 exporter 默认使用 `render.v2`。preserved old export package 不在 repository Schema bytes 变化后由 current external validator 重新验证；其 embedded `schemas.sha256`/`schema_inventory` 继续是 binding evidence，任何 current validation 必须报告 `SCHEMA_INVENTORY_HASH_MISMATCH`。不得 bypass hash、自动迁移、增加 historical Schema snapshot layer 或使用 version-aware validator fallback；必须保留旧 package bytes 和 reports，不伪造新 policy。
3. **缺失材料真相**：Java 对 resolved submission 的 material identity 是 authoritative，覆盖 whole-model/material missing、整个 missing model、vanilla material/quads，以及 Fabric mesh；Fabric mesh 使用 block-atlas missing sprite 的 `SpriteFinder` 和 missing-sprite UV bounds 判定。`minecraft:missingno` 的精确 source checker 事实是四象限 `#F800F8`/`#000000`，但渲染后的颜色不具权威性。Python 不再使用宽松的全局 magenta/black 比例；只能以严格 canonical-checker 检查作 defense-in-depth，ambiguous pixels 不能单独证明 missing texture。不得新增第二个 authoritative material 规则。
4. **动画与随机性确定性**：使用 scoped client-only gate；导出前调用并 await resource reload 以重置 atlas，导出期间只取消 block atlas (`TextureAtlas.LOCATION_BLOCKS`) 的 `cycleAnimationFrames`，成功和失败路径都清除 gate；不得修改 private field。保留 resolver 的固定 seed `42L`，不引入 world-position seed；实际动画/seed 控制必须进入现有 renderer options/environment identity，成为 render input 的一部分。
5. **最小运行时证据**：实现完成后必须先有 targeted smoke，覆盖恢复的 thin geometry、保留的真实 skips、`nether_portal`、bubble-coral-like missing-material 判定和延迟动画重复；replacement exports `055316`/`060151` 虽通过 validator，但存在 `2` 个 dynamic preview mismatches，不是最终 pairwise evidence，必须保留。随后必须在同一冻结 Windows 环境恰好生成两次新的 corrected export；两次新导出都必须通过既有 R1 validator，且 pairwise 的所有 `preview.png`、`mask.png`、`render.json` 以及规范化 records/masks 与现有 validator 结果一致。Linux Java/runtime/exporter 和最终双平台证据仍统一 deferred 到 R5。

### D-035 impact proof

- **Local single-machine operation**：只调整现有 Fabric exporter 的渲染接受条件、resolved-material 判定和客户端动画控制；不增加服务、依赖、CLI、SQLite 或 Python 重渲染职责。
- **Reproducibility**：`render.v2`、固定 resolver seed `42L`、atlas reload/freeze 控制和实际 renderer options/environment identity 进入既有 render input lineage；历史 `render.v1` records/workspace/release data 和旧 reports、旧 R3 run 不改写，preserved old export package 不在 repository Schema bytes 变化后由 current external validator 重新验证，corrected export 使用新 policy 和 fresh lineage。
- **Data contract and scope**：复用现有三个 v1 Schema ID，仅扩展 policy enum；保留 `nether_portal` 非空 composite、43 个 block-entity skips、10 个 invisible/technical skips 和既有 failure/review 语义，不增加 fixture/schema namespace。
- **Material and security boundary**：Java resolved submission 保持机器事实 authority；Python canonical checker 只作防御性校验，不能从模糊像素推断缺失材料；不记录原版资源、秘密或额外运行时状态。
- **Platform boundary**：Windows targeted smoke 与两次同环境导出属于当前修复证据；Linux 保持 R5 义务，不以缺失 Linux 证据阻塞本 Phase 1 contract freeze。

### 2026-08-16 — owner-approved dynamic-render scope amendment

项目所有者停止 dynamic-render research，并批准 `minecraft:end_portal` 与 `minecraft:end_gateway` 作为 non-building 的 explicit machine pending skips。两个精确 block ID 必须继续登记并保留全部合法 states；exporter 必须在进入渲染前使用现有 `BLOCK_ENTITY_FIXTURE_UNSUPPORTED` 写入 ordinary auditable pending skip：不生成 preview、mask 或 render directory，failure/variant/state mapping 仍沿用现有 skip 关系并要求后续 human review，绝不能静默过滤。不得新增 reason code、Schema ID 或 allowlist framework；`nether_portal` 仍按 D-043 原批准规则 renderable。

这项 scope amendment 将 baseline 从 `41` 个 block-entity + `10` 个 invisible/technical skips（`51`）更新为 `43` + `10`（`53`）。单独观察到的 `melon_stem`、`pumpkin_stem`、`tripwire` `OBJECT_TOO_SMALL` 项仍保持 reviewable，本 amendment 不重新分类它们。replacement exports `055316`/`060151` 已通过 validator，但有 `2` 个 dynamic preview mismatches，不是最终 pairwise evidence，必须保留；corrected evidence 仍要求同一环境下恰好两次新的 exports。本 amendment 不代表实现、pairwise PASS、R3 退出或 candidate 完成。

### 2026-08-16 — owner-approved logical selection-policy identity amendment

`minecraft:end_portal`/`minecraft:end_gateway` 的稳定逻辑 selection-policy token 精确冻结为：`pre-render-skip.v1;reason=BLOCK_ENTITY_FIXTURE_UNSUPPORTED;ids=minecraft:end_gateway,minecraft:end_portal`。该 token 必须在 `logical_input_signature` 中按固定 framing 顺序写入：紧接 `dedupe_policy_version` 之后、resource snapshot 与 registry hashes 之前；它是 logical selection，不是 graphics environment，**不得**写入 `renderer_options`。因此该 token 的变化会 transitively 改变 `render_input_signature`；任何会影响输出的 pre-render skip policy 变化都必须修改这个 exact token，不能静默改变语义或引入 generic framework。

既有 `render.v2`、Schema ID 和 `BLOCK_ENTITY_FIXTURE_UNSUPPORTED` reason enum 保持不变。replacement exports `055316`/`060151` 使用旧 logical/render signatures；后续 replacement/corrected exports 必须具有不同的 `logical_input_signature` 和 `render_input_signature`。本 amendment 冻结 identity；Gate 3 closure 条目已记录相应 runtime evidence，但不代表 R3、candidate 或人工审核完成。

### 2026-08-16 — Oracle Gate 3/R1 P0 closure evidence

Oracle Gate 3 判定 PASS。最终 pair 为 `run/blockpedia-data/exports/26.2/export_20260816T091512Z/` 与 `run/blockpedia-data/exports/26.2/export_20260816T093009Z/`；两者均为 `1196` blocks/variants、`32366` states、`1140` selected、`56` pending skips。两个 validator reports 均通过，SHA-256 分别为 `d7c6c166695ac4b56ae3f2720aa972b749429f2ed4d89c1738a4293891c2aa3d` 和 `5ffaccdfb35e010bc2333504e4d223635b76e4d6afb7a88d3d8111a7c3d3904b`。

Pairwise report `run/blockpedia-data/reports/export_20260816T091512Z--export_20260816T093009Z-pairwise.json` 的 SHA-256 为 `a328dc6e64ce3423995ec268d760d8108c2bf79dd0ff9d2ee7b8afe7d8254699`，status=`passed`；全部 `3420` 个 render artifacts match。pair 的 logical/render signatures 以 `f39a...` 与 `fbd3...` 报告，在 pair 内一致且不同于 pre-amendment signatures。该证据只关闭 D-043/R1 P0；3 个 `OBJECT_TOO_SMALL`（`melon_stem`、`pumpkin_stem`、`tripwire`）保持 ordinary reviewable R3 pending items，不重新分类为 R1 blocker。旧 exports/runs/reports 继续作为历史证据保留；本条不代表 R3、candidate 或人工审核完成。

## D-044：有界批次并发、发送线性化与 pristine same-run reconfiguration

### 2026-08-16 — owner-approved Phase 1 contract freeze

项目所有者批准本项 Phase 1 contract freeze。本项**仅** supersede D-040/D-041 中关于 send concurrency=`1` 的部分；D-040/D-041 的手动逐批默认、明确计划授权、ordered frozen plan、aggregate persisted identity、final pre-send full recompute、item-local continue、fatal stop、retry generation/idempotency/sibling resolution、audit、no fallback 和每个 logical request 最多两次总尝试均保持有效。本记录只冻结文档契约，不代表实现、测试、真实 provider 请求、运行时或 R3 退出证据已经完成。

1. **Scheduling scope and bounds**：`offline_annotation` 的 `concurrency` 是整数 `1..5`，默认值为 `1`；`query_spec` 和 `visual_rerank` 的 `concurrency` 必须精确为 `1`。该数值计数的是并发中的 logical batch，不是 HTTP attempt；每个 logical batch 继续使用现有“首次尝试加一次 retry”的最多两次总尝试预算，不能因为并发而增加 retry，也不能 protocol/model/provider fallback。
2. **One shared executor**：一个 Python 进程生命周期内只能有一个进程内 executor，容量最多 `5`，由所有 run 共享；不得为每个 run 创建 executor。任何时刻全局 active sends `<=5`，且同一 run 的 active sends `<=` 该 run 冻结的 `offline_annotation` concurrency。executor 是本地实现细节，不是服务、队列或远程调度器。
3. **Ordered contiguous approved claim barrier**：Worker 只能按 frozen plan order 领取 logical batch；可领取集合必须是从当前未完成位置开始的连续 approved prefix。遇到未 approved、已失效、lineage 不一致、pause/cancel/fatal stop 或其它 gate failure 时，后续 batch 不得越过 barrier 被 claim。claim 不是 durable pending provider request reservation，也不向远端申请 exactly-once claim。
4. **Final gate and thread boundary**：每个 batch 在任何 HTTP 之前必须通过完整 one-batch payload/contact sheet/prompt/machine metadata rebuild、full signature recomputation、approval/plan lineage、run/stage state、停止信号和 active-send bound 的最终 gate。HTTP 不得包在 SQLite transaction 中；DB transaction 只用于发送前/后的本地状态和审计提交。不同线程不得共享 SQLite connection、transaction、provider client 或 provider mutable state；线程只传递必要的 immutable payload/result。
5. **Send-started linearization**：当一个 worker 已通过上述 final gate、占用 active-send slot 并进入 provider HTTP call 时，send 线性化为 started。pause、cancel 或 fatal stop 只阻止尚未 started 的 claimed sends 及之后的 sends；已经 started 的 call 可以完成，并可以持久化其 request evidence、item terminal state 和其它既有证据，但不得把已经 `failed` 或 `cancelled` 的 run/stage 复活为可运行或成功。fatal stop 覆盖 paused 结果，但不能覆盖已经 durable 的 cancelled 结果。不得伪造 in-flight cancellation；停止动作必须等待已 started 的 futures 收敛。
6. **Crash and recovery boundary**：系统不持久化 pending provider request reservation，也不声称远端 exactly-once claim。send 已发出但本地 commit 前发生 hard crash 时，最多可有该时刻冻结并发上限数量的 unknown outcomes。启动只检测并展示 stale，不自动 resend；仍须由现有显式 WebUI `recover`/审核恢复路径决定下一步，未知结果不得当作成功或安全重发。
7. **Executor lifetime and completion**：该唯一 executor 在进程生命周期内创建并复用，停止操作必须等待其 live futures；live future 或该 executor 上尚未完成的 DB work 存在时，相关 run 不能被判定为 stale 可恢复或 completed。只有没有 DB work 且没有 futures 时，send drain/Worker 才能报告完成。SSE/browser disconnect 不改变上述生命周期。
8. **Profile edit exception**：只改变 `offline_annotation` concurrency 且新值仍为 `1..5` 的 profile edit，必须保留该 profile 的 `verified`/`enabled`，不需要重新 probe；`adapter`、model、base URL、secret reference、Schema、prompt、search/ranking 或其它配置变化继续沿用既有 invalidation、disable、fresh snapshot 和 rerun 规则。`query_spec`/`visual_rerank` concurrency 不可通过 profile edit 改变，仍固定为 `1`。
9. **Strict pristine same-run reconfiguration**：允许在同一 run 原地重新配置 frozen config 和 pending jobs，但仅当 run/stage 已暂停在 `AI_ANNOTATE`，且同时满足：没有 live future；没有 live provider request；没有 provider-request evidence、annotation、AI artifact、provider review、AI review、send/result/retry/cancel evidence；每个 AI job 都是 pending、unapproved、ownerless、无脏 lineage 的 clean job。任一条件无法证明时必须 fail closed。通过检查后，必须在一个原子本地操作中替换 frozen config 和 pending jobs，保留全部 R2/machine evidence，写入既有 audit 流程的 `R3_RUN_RECONFIGURED`，并使旧 plan 失效；不得伪造清理 evidence、重用旧 approval 或绕过新的 final gate。该操作不创建新 run，不改变持久状态枚举，也不新增 SQL/Schema。
10. **Release/schema boundary**：scheduling concurrency 只属于 profile/run 的运行时调度配置；它不得进入 release snapshot、`release-manifest.v1`、任何 provider wire/record Schema 或其它发布身份/语义字段。每个 run 仍必须冻结实际使用的 offline bound 供调度和审计复核，但 release 的可复现语义、AI artifact 和 provider snapshot 不因调度并发变化而改写。
11. **Explicitly forbidden**：本项不得引入 Redis/Celery/Kafka/其它服务、持久队列、per-run executor、adaptive concurrency、动态限流算法、新 SQL/table/column、migration framework、Schema ID/field、持久 status、依赖、CLI、协议/model/provider fallback、额外 retry 或 fake in-flight cancellation；也不得以并发重构改变 D-040/D-041 的 approval、lineage、audit、recover 或 max-two-attempt 边界。

### D-035 impact proof

- **Local single-machine operation**：仍使用 Python/FastAPI/SQLite/local files/进程内 Worker；唯一共享 executor 只是同一进程内的有限线程调度，不增加服务、队列、daemon 或远程协调器。
- **Reproducibility and lineage**：logical batch order、frozen per-run offline bound、payload/signature/approval gate、既有 plan hash 和 request lineage 继续决定可发送输入；attempt budget、无 fallback、send-started outcome 和 crash recovery 均可审计，不能把未知结果伪装成成功。
- **No extra service and data contract**：并发只改变调度时机，不改变 provider wire、workspace/release Schema、release snapshot 或 SQLite 结构；不新增 dependency、CLI、status、migration 或 retry budget。
- **MCP read-only and release immutability**：D-044 只作用于 WebUI/Worker workspace。MCP 仍只读 resolved immutable release，不读取 live workspace/active profile，不写数据库、文件、cache、logs 或 current。
- **Configuration safety**：只有合法 offline concurrency-only edit 免于 reprobe；其它 profile/config lineage 变化仍 fail closed、invalidate 和 rerun。pristine same-run reconfiguration 保留 R2 machine evidence、使旧 plan 失效并写审计，不能改写已产生的 AI/provider evidence。

本项的 focused acceptance requirements 记录于 [`quality-and-testing.md`](quality-and-testing.md) 的 D-044 小节；该记录是后续验收清单，不是实现或测试完成声明。

## D-045：targeted banner rerender 与当前 run 增量 refresh

### 2026-08-17 — owner-approved contract/document/schema freeze

项目所有者于 **2026-08-17** 明确批准本项 contract/document/schema freeze。本项只冻结实现边界，不代表 Java/Python implementation、targeted runtime export、WebUI refresh、AI jobs、candidate 或 R3 退出已经完成。D-045 **仅**为这次精确操作 supersede D-043 的两条边界：相机不得按类别变化的限制，以及历史 run 不 refresh 的限制；D-043 的其它渲染、完整导出、证据和历史 immutability 边界全部保留。

1. **精确 exporter 操作**：Fabric 增加游戏内命令 `/blockindex export banner-repair <base_export_id>`，不接受任意 target filter。目标集合固定为 `minecraft` 命名空间中 16 个 vanilla dye colors（`black, blue, brown, cyan, gray, green, light_blue, light_gray, lime, magenta, orange, pink, purple, red, white, yellow`）分别对应 `*_banner` 与 `*_wall_banner` 的 32 个 ID；实现按该集合稳定派生并排序，不能扩大或缩小。
2. **完整替换导出**：命令先验证完整 base export，复用所有非目标 records/render artifacts 的不变内容，只重新渲染上述 32 个目标，并在 `exports/26.2/<new_export_id>/` 生成新的普通完整导出。新包必须重写 export lineage、counts、manifest、logical/render signatures 和 checksums，并通过既有 exporter commit/validator/check flow；不得产生 partial package 或新的 Schema ID。
3. **camera/render identity**：保留历史 `camera.v1`，新增 `camera.v2`，继续使用 `render.v2`。新 replacement manifest 使用 `camera.v2` 作为 effective camera policy identity；复用的非目标 artifacts 保持 byte-identical，不因本项重新渲染。对精确运行时类型 `BannerBlock`/`WallBannerBlock`，在既有 vanilla special renderer transform 之外应用共同的 parent center-pivot correction：`translate(0.5,0.5,0.5)` → `scale(0.72,0.72,0.72)` → `translate(-0.5,-0.5,-0.5)`，随后保持既有 submit path。banner-camera logical policy token 精确为 `banner-camera.v2;namespace=minecraft;types=BannerBlock,WallBannerBlock;colors=black,blue,brown,cyan,gray,green,light_blue,light_gray,lime,magenta,orange,pink,purple,red,white,yellow;forms=banner,wall_banner`，必须紧接既有 PRE_RENDER_SKIP token、位于 resource/registry hashes 之前；camera hash 及 renderer options/environment identity 也必须改变。普通非目标内容不因本项重新渲染。
4. **Studio refresh 入口**：WebUI 只增加严格 `POST /api/runs/{run_id}/banner-export-refresh` 及同一业务入口的 HTMX 操作。请求必须包含 `check_id`、`expected_base_export_id`、精确排序的 32 个 `target_ids` 和 `confirm=true`。它只消费 passed immutable full export check，并且只允许当前 run 位于 `HUMAN_REVIEW/needs_review`、无 live work、base export 精确匹配、恰好 32 个 open `OBJECT_OFF_CANVAS` skipped targets；normalized semantic diff 必须严格为 32 个 skipped→selected 转换和 96 个 render files。
5. **原子 workspace replacement**：在既有 run lock 下，先 staging，并以 narrow local journal/backup 保护 complete replacement source export、精确目标 render/feature files 和 SQLite projection 的 recover/commit；任何部分失败都恢复到原状态。不得新增 table、column、status、migration framework、service、queue 或 Python product CLI；Python 仍不渲染或选择 variant。
6. **既有工作保留与增量 AI**：既有 `1140` annotations、provider requests、jobs 和 reviews 必须保持不变。只创建恰好 3 个未批准的 target AI jobs，按稳定目标顺序分成 `12 + 12 + 8`，使用 distinct `banner_refresh_*` logical keys；只将 `AI_ANNOTATE`、`VALIDATE`、`HUMAN_REVIEW` reset 为 `pending`，早期成功阶段不重跑，run 从 `AI_ANNOTATE` 继续。
7. **mixed lineage**：现有 `imports.report_json` 复用既有 JSON storage，写入严格 `banner-refresh.v1` provenance，包含 base/new import、export、manifest/checksum hashes、精确 targets 和 policy token；不新增 SQL migration。release checks 只可为 preserved requests 保留 historical base export ID，且这些 requests 必须排除全部 32 个 targets，并在 historical envelope export ID 下 current input 重算完全一致；新的 banner requests 必须使用 replacement export ID。该 provenance 必须进入 functional inputs。
8. **最小证据**：只要求 Java 25 focused build/targeted export、既有 validator、focused service/HTTP/rollback/mixed-lineage tests 和一次真实 WebUI refresh；不新增 full test matrix，不要求第二次重复 targeted export，也不重复 D-044 verification。验证必须能证明 32 个目标和 96 个 render files 的范围、3 个新 jobs/批次与 1140 条既有工作保留。

### D-035 impact proof

- **Local single-machine operation**：继续使用 Fabric、现有导出目录、FastAPI/WebUI、SQLite、本地文件和进程内 Worker；targeted exporter command 是 Minecraft 内命令，不是 Python product CLI，不增加服务或远程协调器。
- **Reproducibility and lineage**：base export 必须是 immutable complete checked input；精确 target set、banner-camera token、camera/renderer identity、new export ID、manifest/checksum 和 `banner-refresh.v1` provenance 均形成可复核的新 lineage。非目标 records/artifacts 与既有 AI/provider/review evidence 不被重写。
- **No extra service and data contract**：复用现有 exporter/record Schemas、workspace JSON storage、SQLite schema、状态枚举、锁和 validator；只把 `camera_policy_version` 从单一 `camera.v1` 放宽为 `camera.v1|camera.v2`，不新增 Schema ID、表、列、migration、queue、dependency 或 generic patch layer。
- **MCP read-only and release immutability**：refresh 只改变可变 workspace 和后续新 release 输入；MCP 仍只读 resolved immutable release，不读取 workspace、不写 current、release、cache 或 logs。任何发布修订仍生成新的 immutable release，不原地修改历史 release。
- **Recovery and safety**：passed immutable check、exact base/targets/diff、run lock、no-live-work gate 和 narrow journal/backup 共同确保文件与 SQLite 可恢复提交；失败不得解析为成功或部分 refresh。D-043 的其它历史证据与 D-044 的并发/lineage/retry 边界继续有效。

## D-046：MCP JSON-RPC 错误分类纠正

### 2026-08-18 — owner-approved correction

项目所有者于 **2026-08-18** 明确批准本项纠正：未知 JSON-RPC method 继续使用标准 `-32601`（Method Not Found）；请求 method 为合法 `tools/call` 但 tool name 不在四个允许工具中时，使用 Invalid Params `-32602`。该分类适用于协议响应和 R4 验收，不改变工具业务错误的 `isError` 分层。

**影响记录**：本项只更正 MCP 协议错误映射及对应文档/测试表述；不改变工具集合、stdio transport、任何数据 Schema、服务、Python CLI、持久化行为、release 语义或 MCP 只读边界。

## D-047：MCP 输入与工具执行错误分层纠正

### 2026-08-18 — owner-approved correction

项目所有者于 **2026-08-18** 明确批准本项纠正，并确认现有 `mcp-error.v1` 的 `error_code` enum 是唯一权威且保持不变：非法 `block_id` 格式、compare 数量和其它 input shape 错误在工具执行前返回 JSON-RPC `-32602`；格式合法但 release 中不存在的 block ID 仍返回 `BLOCK_NOT_FOUND`。正常空搜索是 `isError=false` 成功；`rerank=auto` 的 provider failure 返回 warning 和 `reranked_by_llm=false`；`rerank=required` 失败返回顶层 `RERANK_REQUIRED_UNAVAILABLE`，具体 provider code 只写入 `details.provider_error_code`。

**影响记录**：本项只收敛文档和 R4 验收的错误分层；不修改 `mcp-error.v1` 或其它 Schema、fixtures、roadmap、代码、工具集合、transport、服务、CLI、持久化、release 语义或 MCP 只读边界。`VERSION_REQUIRED`、`NO_CANDIDATES`、`BLOCK_ID_INVALID`、`COMPARE_COUNT_INVALID` 及 provider-specific codes 均不得作为顶层 `mcp-error.v1.error_code`。

## D-048：R4/R5 fresh-only `release-index.v2.sql` 投影

### 2026-08-18 — owner-approved correction

项目所有者于 **2026-08-18** 明确批准为未来 R4/R5 candidate 增加 fresh-only `release-index.v2.sql`。现有 R3 Phase C `release-index.v1.sql` candidate 保持有效、不可变并继续作为 R3 evidence，但不具备 MCP 或 activation 资格；不得迁移、原地改写或将其伪装成 v2。v2 保留 v1 的 scalar/indexed columns 和 indexes，新增经过校验的 `blocks.record_json TEXT NOT NULL`、`states.record_json TEXT NOT NULL`、`visual_variants.record_json TEXT NOT NULL`、`visual_variants.feature_json TEXT NOT NULL`，并固定 `schema_meta.format_version=2`。R4 fixture 与未来 R4/R5 candidates 只使用 v2；MCP/activation 遇 v1 必须以 `RELEASE_INTEGRITY_FAILED` 且 `details.integrity_component="index"` fail closed。

**影响记录**：本项只增加未来 release index 的 fresh projection format，不改变现有 JSON Schema inventory、`workspace.v1.sql`、R3 evidence、工具集合、transport、服务、CLI、持久状态、release immutability 或 MCP 只读边界；不提供通用 migration，不重写旧 release。

## D-049：MCP Minecraft version input pattern

### 2026-08-18 — owner-approved correction

项目所有者于 **2026-08-18** 明确批准 MCP tool input 的 `minecraft_version` 使用现有严格版本格式 pattern `^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$`，不再使用 `const: 26.2`。格式非法的输入在协议层返回 JSON-RPC `-32602`；格式合法但没有发布 current 的版本返回 `VERSION_NOT_AVAILABLE`，列出可用版本且不得回退。该输入校验放宽的是请求格式，不增加当前 Minecraft 版本支持，冻结 baseline 仍为 Java 26.2。

**影响记录**：本项只修正文档中的 MCP input/resolver/test 语义，不改变持久记录的精确版本约束、current-pointer/release Schema、JSON Schema inventory、fixture 资产、服务、CLI、状态、release 语义或 MCP 只读边界。

## D-050：R4 family context 确定性 no-op

### 2026-08-18 — owner-approved correction

项目所有者于 **2026-08-18** 明确批准当前 R4 的 family 语义收敛：现有 release projection 没有 schema-owned `family_id` 或 family catalog，`context.family` 不能引用不存在的发布元数据。原决定曾将 `context.family=null` 作为当前唯一可执行值，并规定非 null 值在 input shape 有效后返回 `QUERY_INVALID`；该非 null 业务错误语义已由下述 2026-08-19 amendment supersede。本项不允许实现推断 family、由模型创建 family ID 或把 family 元数据加入响应。

搜索顺序固定为 Top-24 后保持既有稳定顺序，再生成联系表；不执行 family 分组、默认最多 2 个限制、放宽或任何 family warning/metadata。`compare_states=true` 与 `compare_blocks` 的显式 block ID 比较不引入 family 分组、限制或 metadata。

**影响记录**：本项只收敛当前 R4 文档和验收语义，不新增或修改 JSON Schema、release/workspace 数据字段、family 数据 owner、服务、Python CLI、SQLite migration、fixture 资产或 MCP 工具边界；未来若需要 family metadata，必须另行取得 owner 批准并更新相应数据契约。

### 2026-08-19 — owner-approved amendment

项目所有者于 **2026-08-19** 批准本项 amendment，明确 supersede D-050 原先“非 null `context.family` 返回 `QUERY_INVALID`”的业务分支：`family=null` 或任意通过现有 input schema 的 string（包括未知 string，如 `"unknown"`）均为确定性 no-op，继续正常搜索。两者都不执行 family 分组、不应用 family 限额、不推断或创建 family、不产生 family warning 或 metadata，也不改变候选顺序；Top-24 后继续保持既有稳定顺序并生成联系表。只有非 string 且非 null 的值仍属于 input shape error，必须在协议层返回 JSON-RPC `-32602`，不得生成 `mcp-error.v1`。

**影响记录**：本 amendment 只删除当前 `search_blocks` 的非 null string `QUERY_INVALID` 业务分支并同步文档/验收；保留现有 string/null 类型校验和 deterministic search 路径。不新增 D-052、Schema、字段、服务、CLI、SQLite migration、family catalog 或 family metadata；本地单机运行、可复现性、MCP stdio/只读边界、release/workspace 数据契约和既有候选排序均不变。

## D-051：MCP host-supplied QuerySpec 窄例外

### 2026-08-19 — owner-approved Phase 1 contract

项目所有者在本会话明确批准以下窄例外。它只允许 MCP `search_blocks` 接收一个可选的顶层 `query_spec`，作为调用方提供的不可信、临时 QuerySpec；不改变四工具、stdio、release-only、只读或 provider identity 边界。本项是文档契约冻结，不代表代码、测试或新的 Schema 已增加。

1. **输入与 Schema owner**：`search_blocks` 的 `query_spec` 只能是一个完整对象，精确使用现有 `query-spec-output.v1` 的字段、required 集合、枚举、边界和所有 nested object 约束。它不是新的 Schema ID、持久记录或 MCP 输出字段，也不增加新工具。省略该顶层字段才表示请求既有的 server-side QuerySpec 路径；不得以 `null` 或部分对象表示省略。
2. **严格校验与错误分层**：输入及所有嵌套对象均必须 strict/fail closed，未知 nested fields 也必须拒绝。Schema、类型、缺失字段、额外字段和边界错误返回 JSON-RPC `-32602`；Schema 已通过但语义或不变量不成立的查询条件继续使用既有 `QUERY_INVALID`。提供了无效 `query_spec` 时不得隐式退回旧路径；调用方必须省略它才请求旧路径。
3. **来源诚实性**：`query_spec.source` 仍必须为 Schema 要求的 `llm`。该值只表示语义由 LLM 生产，不证明 Blockpedia 验证了 provider/model，也不证明本次由 server-side provider 调用产生。不得增加 host/model metadata，或从输入推断、持久化 provider identity。
4. **调用抑制范围**：通过校验的 host spec 只抑制 server-side QuerySpec generation。`context.rerank=local_only` 仍禁止所有 provider call 和 visual rerank；`auto`/`required` 仍可仅为 visual rerank 使用 resolved release-bound provider snapshot。secret、capability 和 visual-rerank failure 继续沿用既有 warning、确定性 local downgrade 及 `required` fail-closed 规则，不得跨协议或改用别的 profile/model/base URL。
5. **合并和硬约束**：本地 deterministic parsing 对 query 原文中的显式 hard constraints 拥有最终权威，不能被 host spec 弱化。host hard 值只有经本地解析确认后才能保留为 hard；未确认的 host hard 不得进入 hard filtering。存在未解决歧义时，只能应用安全的 soft intent，不能执行未经确认的 semantic hard。host soft intent 可以在既有 bounded、去重规则内与本地 soft intent 合并；不得创建新的候选身份或 machine facts。
6. **`avoid_for`**：因为完整 Schema 包含 `soft.avoid_for`，输入必须接受并校验该字段；但当前 deterministic search 没有权威 negative dimension。因此它不参与 positive recall、hard exclusion 或 ranking，只通过现有输出 `warnings` 机制发出既有的“未应用/未验证语义约束”提示；不得发明评分规则、负向维度或 Schema 字段。
7. **身份与结果诚实性**：提供 host spec 时，`search_id` 必须按既有 request/query identity 约定绑定其已校验 canonical representation/hash，使不同 host intent 不共享同一 identity；未提供 host spec 时保持既有 identity 路径。不在公开输出中增加或解释 hash 实现细节。仅消费 host QuerySpec 永远不能设置 `reranked_by_llm=true` 或 `score_source=llm_rerank`；这两个值只在实际 visual rerank 成功后产生，也不得声称 Blockpedia 调用或验证了某个 model。
8. **边界保持**：MCP 仍只读取已解析的不可变 release、current pointer 和必要的 release-bound secret reference；不读取 workspace/可变 active profile，不写数据库、文件、cache、logs、release 或 current。host spec 不得选择或改写 server-side provider profile、requested `model_id`、`base_url` 或 release snapshot；MCP 仍只提供四个工具、只使用 stdio，并保持精确版本解析和历史 release selector 禁止。
9. **有限语义不变量**：Schema/type/range/unknown-field 错误仍返回 JSON-RPC `-32602`。完成 Schema normalization 后，若 `hard.minecraft_version.value` 非 null，其规范化后的精确值必须等于已解析请求版本；不等则 `QUERY_INVALID`。将 `hard.behaviors` 的 `transparent`/`emissive` 分别规范化为 `behavior.transparent`/`behavior.emissive`，并将 `hard.transparency`/`hard.emission` 规范化到同一字段；同一 canonical boolean fact 的 `eq`/`not_eq` 与 boolean `value` 转换为 `{true,false}` 的允许集合，交集为空即 `QUERY_INVALID`（例如 `behaviors.transparent=eq true` 与 `transparency=eq false`）。同一规则适用于同一 `behaviors` field 和同一 `support.direction`；不对不同 soft terms 或未定义的形状关系臆测矛盾。`needs_user_choice` 与 `ambiguities` 采用最小确定规则：`ambiguities` 非空必须为 `true`，为空必须为 `false`；`suggested_followups` 只须满足 Schema 要求的数组约束，不因该规则强制非空。仅仅未被本地解析确认的 host hard 不构成 `QUERY_INVALID`，而是从 effective spec 删除并可沿用现有 warning；soft disagreement 也不构成 `QUERY_INVALID`。
10. **Original/effective 分离**：Schema 和上述不变量通过后，保留原始 validated canonical host object 仅在内存中用于 `search_id` identity 和 warning 生成；对象本身不得发送给 visual rerank，不得持久化、记录日志、写 cache 或 output。另构造 effective sanitized QuerySpec：保留经本地确认的 host hard、合并本地 explicit hard（本地权威不可弱化），删除全部未确认 host hard，并在任何 recall、filter、scoring、contact-sheet 或 rerank 输入前将 `soft.avoid_for` 设为 `[]`。deterministic recall/filtering 和 visual rerank 只能接收 effective spec；仅实际成功 visual rerank 才能设置 `reranked_by_llm=true` 或 `score_source=llm_rerank`。

### D-035 impact proof

- **Local single-machine and data contract**：只在 `search_blocks` 的内存输入路径增加一个既有 provider Schema 的可选入口；不新增服务、依赖、CLI、Schema ID、持久字段或输出 Schema，Studio/provider 请求规则不变。
- **MCP read-only and provider safety**：host spec 是不可信临时输入，不进入 release、workspace、cache 或日志，也不能选择 provider identity；只有既有 release-bound snapshot 可用于 visual rerank，`local_only` 与 `required` 语义不变。
- **Reproducibility and honesty**：严格校验、确定性本地 hard/soft 合并、`avoid_for` 忽略和既有 `search_id` identity 约定保持可重放；visual-rerank 标志只由真实成功调用产生，不能把 host input 当作 provider evidence。
