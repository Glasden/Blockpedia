# Blockpedia MVP 路线图

- **版本**：MVP 冻结版
- **日期**：2026-08-17
- **目标**：在 Windows 11 x86_64 和 Linux x86_64（`manylinux_2_17` / glibc `>=2.17`）上完成原版 Minecraft Java 26.2 的本地单机端到端闭环
- **当前状态**：R0 契约冻结、R1 确定性导出和 R2 Index Studio/存储/任务均已有 Windows、静态与 fixture 证据；D-043 Gate 3/R1 P0 corrected-export closure 已由最终 pair evidence 验证，R1 P0 不再 open。D-040 的批次授权、D-041 的可扩展 aggregate authorization、D-042 的 `prompt.v2`/安全诊断、D-044 的 bounded concurrency 以及 D-045 的 targeted banner rerender/refresh 均已有 focused implementation 与 Windows runtime evidence。D-045 已将精确 32 个 banner targets 刷新到当前 R3 run，并完成 3 个新增 AI 批次；用户随后接受全部 1172 个 annotation reviews，并审核跳过剩余 15 个机器渲染失败项。真实 candidate-build gate 已通过，未激活 candidate `rel_357c6104fe8ccfc0f4b7823a68ccc84e` 已构建且根 `current.json` 不存在；R3 Windows 退出证据已满足，R4 可以开始但尚未实现。Linux CPython/Web、Linux MCP stdio、Linux wheel/ABI、Linux Java/runtime/exporter 和最终双平台复现统一 deferred 到 R5；正式支持平台与 Linux 基线不变，当前不声称 Linux 已通过。

规范优先级和硬限制见 [`../AGENTS.md`](../AGENTS.md)；冻结决定集中见 [`decisions.md`](decisions.md)。冲突必须先更新高优先级文档，不能在实现中静默偏离。移入的原始设计稿仅是历史背景和最低优先级参考，不能与新文档一起作为执行规范；其中冲突内容禁止实现。

## 路线图文档索引

当前仓库已完成最小 R0 物化和验收，并保留了 R1、R2 的既有 Windows/静态/fixture 证据；D-043 Gate 3 已验证 corrected pair 并关闭 R1 渲染确定性/质量 P0 blocker，旧导出仍不作为 candidate 输入。R1 的 v1 身份、路径、哈希删留和 exporter/外部 validator 职责已冻结；所有 Linux 实际运行/安装/平台行为与最终双平台复现义务统一保留在 R5。R2 既有退出证据保持可追溯，R3 已通过真实 candidate-build gate 并形成一个未激活 candidate；R4 可以按依赖门开始。后续阶段只在实际实现需要时增加测试与平台证据，不重复设计或为未来阶段预建验证体系。

所有链接均相对于本文件所在的 `docs/` 目录。

| 文档 | 链接 | 本次状态 |
|---|---|---|
| 治理硬限制 | [`../AGENTS.md`](../AGENTS.md) | 已生成 |
| 冻结决定 | [`decisions.md`](decisions.md) | 已生成；R0 最小范围已记录 |
| 产品范围 | [`product-scope.md`](product-scope.md) | 已生成；字段形状引用真实 Schema |
| 总体架构 | [`architecture.md`](architecture.md) | 已生成；字段形状引用真实 Schema |
| 导出契约 | [`export-contract.md`](export-contract.md) | 已生成；字段形状由 exporter Schema 拥有 |
| 状态策略与渲染 | [`state-policy-and-rendering.md`](state-policy-and-rendering.md) | 已生成；字段形状由 exporter Schema 拥有 |
| 数据与 Schema | [`data-and-schemas.md`](data-and-schemas.md) | 已生成；R0 Schema owner 说明已记录 |
| 流水线、存储与发布 | [`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md) | 已生成；字段形状引用真实 Schema |
| OpenAI provider | [`openai-provider.md`](openai-provider.md) | 已生成；字段形状由 provider Schema 拥有 |
| 搜索与排序 | [`search-and-ranking.md`](search-and-ranking.md) | 已生成；字段形状引用真实 Schema |
| MCP API | [`mcp-api.md`](mcp-api.md) | 已生成；字段形状由 MCP Schema 拥有 |
| WebUI 与运维 | [`webui-and-operations.md`](webui-and-operations.md) | 已生成；字段形状引用真实 Schema |
| 质量与测试 | [`quality-and-testing.md`](quality-and-testing.md) | 已生成；R0 验证范围已记录 |
| 安全与分发 | [`security-and-distribution.md`](security-and-distribution.md) | 已生成；核心安全边界保留 |
| 原始设计稿（字节不变） | [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) | 历史参考；当前哈希已验证 |

索引列出的 13 份具体文档当前均可由路径找到；其 Markdown 业务行为保留，精确字段形状由真实 Schema 文件拥有。原始稿执行地位始终是历史背景/最低优先级，不与新文档一起执行；其中冲突内容禁止实现。不得以默认值、原始稿或本索引代替具体契约。未来若发现冲突，必须先遵循并更新本文件、`AGENTS.md` 和 `decisions.md`，再收敛具体契约，禁止在实现中静默偏离。

## 阶段依赖与退出门

依赖链为 `R0 → R1 → R2 → R3 → R4 → R5`。每一阶段只以该阶段已经定义的最小交付物和验收为退出条件；后续阶段的平台、运行时、数据和发布证据不得倒灌阻塞前一阶段。R0、R1、R2 已退出，R3 可以开始；Linux 实际验证统一由 R5 承接，R3-R5 其余未完成项保持未勾选。

### R0：契约冻结

**依赖**：无。

#### 任务

- [x] 写入治理硬限制、规范优先级、MVP 收缩、秘密规则和证据纪律。
- [x] 集中记录 2026-08-13 grill 后冻结的技术、范围、发布和 MCP 决定。
- [x] 写入产品范围与总体架构的可执行边界。
- [x] 具体文档已生成且零断链；当前证据为 16 个 Markdown/治理文件，本地链接目标零断链和逐路径审查。
- [x] 物化恰好 26 个最小 Draft 2020-12 JSON Schema；每个拒绝未知 root fields，重要 nested objects closed；每个 Schema 提供一个 valid 和一个 extra-field rejection fixture。
- [x] 运行轻量测试验证 Schema inventory、fixtures 和 provider wire 基础约束；不引入通用规则引擎或额外 Schema ID。
- [x] 建立固定基线的 Fabric/Gradle toolchain skeleton，并以 Java 25、Gradle 9.5.1、Loom 1.17.19 完成 Windows offline build。真实 Minecraft 运行/导出在 R1 验证。
- [x] 锁定 R0 tooling 实际引入的 Python 依赖及 hashes；未预锁未实现的 R2-R4 栈。

#### 交付物

- [x] `AGENTS.md`。
- [x] `docs/roadmap.md`、`docs/decisions.md`、`docs/product-scope.md`、`docs/architecture.md`。
- [x] 13 份具体文档均存在；链接路径审查为零断链。
- [x] 真实 JSON Schema 文件、Schema 哈希、fixtures 和轻量验收报告。

#### 验证与退出条件

- [x] Fabric 骨架声明 Minecraft Java `26.2`、Java `25`、Fabric Loader `0.19.3`、Fabric API `0.157.0+26.2`、Loom `1.17.19`、Gradle `9.5.1` 和 native Mojang names/unobfuscated（无外部 mappings artifact），并完成 Windows offline build；Minecraft runtime/export 验证归 R1。
- [x] R0 tooling 依赖已精确/hash 锁定并完成当前 Windows 开发环境安装/一致性检查；CPython `3.14.7` 产品运行和 Linux 验证不阻塞 R0，Linux 实际验证统一留到 R5。
- [x] 恰好 26 个 Schema 已物化，52 个 fixtures 和轻量 inventory/provider-wire 验收通过。

**证据区**

- 契约物化：`schemas/{exporter,workspace,provider,mcp}/` 恰好 26 个 Schema；`tests/schema/fixtures/` 恰好 52 个正反 fixtures。
- 轻量验收：`python -m tools.validate_r0 --repo-root . --report` 通过，输出 `R0 validation passed: 26 schemas, 52 fixture case(s)`，报告为 `docs/evidence/r0-schema-report.json`；`python -m pytest -q tests/test_r0_schemas.py` 输出 `1 passed`。
- Python 锁：`requirements.in` 与 `requirements.lock`；`python -m pip install --require-hashes -r requirements.lock` 和 `python -m pip check` 已通过。Windows CPython `3.14.7` 产品运行在 R2 验证，Linux 安装/运行验证统一留到 R5。
- Gradle/Fabric 骨架：`build.gradle`、`settings.gradle`、`gradle.properties`、`gradle/wrapper/`、`gradle/dependency-locks.lockfile`、`gradle/verification-metadata.xml` 和 `src/main/`。wrapper JAR SHA-256 为 `497c8c2a7e5031f6aa847f88104aa80a93532ec32ee17bdb8d1d2f67a194a9c7`，与 Gradle 官方 9.5.1 记录一致；Windows 使用 Zulu Java 25 执行 `gradlew.bat --offline build`，结果为 `BUILD SUCCESSFUL`。
- R0 于 2026-08-13 关闭。Windows 的真实 Minecraft runtime/export 已在 R1 以现有证据完成；Linux Java 25/runtime、Linux exporter 独立重跑和最终双平台源码/运行时复现保留至 R5，不再作为 R0 或 R1 blocker。

### R1：确定性导出

**依赖**：R0 退出门通过；不得以未冻结的版本或未锁定依赖开始导出。

#### 任务

- [x] 用冻结基线实现仅客户端 Fabric `Block Index Exporter`，登记 `minecraft` 命名空间 100% 注册表。
- [x] 冻结 exporter 内部顺序为 `EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS`：exporter 唯一负责注册表枚举、代表状态选择和 Minecraft 内渲染。
- [x] 导出每个注册表方块的合法 `BlockState`、默认状态、全部合法属性值和运行时机器事实；每个合法状态显式链接到该方块的 block-level visual representative。Python 不选择状态、不渲染图片。
- [x] 每个方块只选择唯一 default `BlockState` 作为普通视觉代表，并在固定 isolated context 中生成四视角预览和 mask；普通 block model 无法稳定渲染时保留 Block/State，写入机器可读 failure/skip 和 `pending` review。

#### 交付物

- [x] Fabric exporter 可复现构建产物和运行时清单。
- [x] `manifest.json`、`blocks.jsonl`、`states.jsonl`、`variants.jsonl`、`failures.jsonl`、预览和日志。
- [x] fresh staging 中由 exporter commit gate 完成最终引用/计数/状态、精确 render 路径与文件集、PNG 基础可读性和尺寸、checksum 生成、fsync 及一次原子提交；外部 Python validator 对最终包另行执行一次 strict Schema/关系/资源/PNG 语义与 checksum/artifact digest 校验；失败 staging 不得被消费者接受。
- [x] 导出契约、最小状态策略与渲染约束的实现和验证证据；R1 不产生 workspace 人工 `skip-review.v1`。

#### 验证与退出条件

- [x] 注册表覆盖率为 100%，每个 `block_id` 唯一且属于 `minecraft` 命名空间。
- [x] 所有导出状态均为运行时合法状态；每个状态都有该 block 的代表链接，或有 exporter failure/skip 且保持 `pending` review。
- [x] 预览、蒙版、几何、行为和资源快照字段可读取且哈希稳定；R1 不以不存在 exporter Schema owner 的颜色字段作为验收项。
- [x] Windows 11 x86_64 已以冻结 Java 25 基线完成构建并运行实际 Minecraft 26.2 exporter，导出包通过外部 validator；Linux Java 25/runtime、Linux exporter 独立重跑和最终双平台源码/运行时复现保留至 R5，未在 R1 宣称完成。
- [x] D-043 Gate 3 corrected-export closure：最终 pair 的两个导出均通过 validator，pairwise report 通过且全部 3420 个 render artifacts 一致；证据与哈希见下方 Gate 3 条目。

**证据区**

- Windows 当前 v1 真实导出：`run/blockpedia-data/exports/26.2/export_20260813T213208Z/`，exporter `0.1.3`，status=`needs_review`，1196 blocks、32366 states、1000 selected、196 skipped/pending；`variant_id == block_id`，抽查 `minecraft:stone` 的 preview 为 `renders/minecraft/stone/preview.png`。`manifest.json` SHA-256 为 `ea7ee7c144dd47244c1bc837a6c89c4d2100ac7eed94615021389ced75d54c82`，`checksums.sha256` SHA-256 为 `2f3541f331d5fe7cf6c72e625bba32819c597dfbe631aa531aa06c7cbf851026`。首次同版本尝试因外部 Notepad 占用 staging 内 `exporter.log` 导致 Windows rename 拒绝并仅保留 staging；关闭外部占用后 fresh 重跑于一次原子 rename 成功提交，失败 staging 未作为消费者输入。
- 构建与聚焦验收：使用 `C:\Users\Glasden\.jdks\azul-25.0.2` 执行 `./gradlew.bat --offline compileJava` 和 `./gradlew.bat --offline build` 均为 `BUILD SUCCESSFUL`；`blockpedia-exporter-0.1.3.jar` SHA-256 为 `fe6686998160df91a3b6b0d44a30ace9926cb41c0987157be576c6d262660a2f`，sources JAR 为 `8961897cd6584b10e146d5b964f24f037a9acc01d6a533bb774ff650b75a0584`。`python -m pytest -q tests/test_r0_schemas.py tests/test_r1_export_validator.py` 为 `9 passed, 2 skipped`；两个 skip 是当前 Windows 环境无 symlink 创建权限，hardlink 与其它检查实际执行。
- 优化后外部验收：`python tools/validate_r1_export.py --repo-root . --export-dir run/blockpedia-data/exports/26.2/export_20260813T213208Z --report run/blockpedia-data/reports/export_20260813T213208Z-validator.json` 输出 `R1 export validation passed`，实测 `516.836s`，未延长 600 秒上限；本地报告 status=`passed`、issues=`[]`，SHA-256 为 `7a839ba3f3b60ae87cbe4a55e1e4ddc3af6ce3c05f90a4f9f02f48404c5e1ec7`。Linux Java 25/runtime、Linux exporter 独立重跑和最终双平台源码/运行时复现尚无证据；按 owner 于 2026-08-14 批准的阶段门重分配，这些义务保留在 R5，不构成 R1 未完成条件。R1 已于 2026-08-14 关闭，R2 可以开始。
- 既有 Windows R1 evidence 曾满足当时退出门并允许 R2 开始；D-043 Gate 3 已以新的 corrected pair 关闭 R1 P0，但不关闭 R3 人工审核、candidate 或退出门。Linux 和最终双平台验证仍由 R5 的未勾选门负责。

#### D-043 Gate 3/R1 P0 closure（已验证；不代表 R3 完成）

Oracle Gate 3：`PASS`。

历史 Windows 26.2 exporter 的 P0 缺陷已由 D-043 Gate 3 corrected pair closure 验证：最终导出 `run/blockpedia-data/exports/26.2/export_20260816T091512Z/` 与 `run/blockpedia-data/exports/26.2/export_20260816T093009Z/` 均有 1196 blocks/variants、32366 states、1140 selected、56 pending skips；两个 validator report 分别通过，SHA-256 为 `d7c6c166695ac4b56ae3f2720aa972b749429f2ed4d89c1738a4293891c2aa3d` 和 `5ffaccdfb35e010bc2333504e4d223635b76e4d6afb7a88d3d8111a7c3d3904b`。pairwise report `run/blockpedia-data/reports/export_20260816T091512Z--export_20260816T093009Z-pairwise.json` SHA-256 为 `a328dc6e64ce3423995ec268d760d8108c2bf79dd0ff9d2ee7b8afe7d8254699`，status=`passed`；全部 3420 个 render artifacts match。pair 的 logical/render signatures 分别以 `f39a...` 与 `fbd3...` 报告，在 pair 内一致且不同于 pre-amendment signatures；Oracle Gate 3 判定 PASS。

未修改的历史 `render.v1` records、workspace/release data 在当前 v1 Schema ID 下保持 valid，并只在其 record/run context replay；preserved old export package 在 repository Schema bytes 变化后不由 current external validator 重新验证，embedded `schemas.sha256`/`schema_inventory` 仍是 binding evidence，current validation 必须报告 `SCHEMA_INVENTORY_HASH_MISMATCH`，不得 bypass hash、自动迁移、增加 historical Schema snapshot layer 或使用 version-aware validator fallback。旧 package bytes/reports 继续保留为历史证据。`minecraft:end_portal` 与 `minecraft:end_gateway` 仍登记全部合法 states，并在进入渲染前以既有 `BLOCK_ENTITY_FIXTURE_UNSUPPORTED` 作为 explicit machine pending skip，不生成 preview、mask 或 render directory；`nether_portal` 仍 renderable。3 个 `OBJECT_TOO_SMALL`（`melon_stem`、`pumpkin_stem`、`tripwire`）保持 ordinary reviewable R3 pending items，不是 R1 blocker；R3 人工审核、candidate 和退出复选框仍未完成。Linux 仍归 R5。

### R2：Index Studio、存储与任务

**依赖**：R1 退出门通过；只能导入经过 R1 完整性验证的导出包。

#### 任务

- [x] 实现 `FastAPI + Jinja2 + HTMX` 的 loopback Index Studio，并只提供 `block-index web` 启动方式。
- [x] 冻结 Studio 阶段为 `PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE`。
- [x] Studio 只导入/验证 exporter 已产生的 variants/renders、提取离线特征并完成 R2 的前六阶段边界；Python **MUST NOT** 重选 variants 或重渲染。AI/审核、release 构建与激活属于后续阶段，R2 未实现。
- [x] 实现冻结 SQLite schema、本地图片目录、FTS5/规范化字符串降级和数据分层存储。
- [x] 实现进程内持久化 Worker、暂停/恢复、失败处理、心跳和逐条状态落盘；启动只检测 stale，状态变化由 WebUI `recover` 触发。

#### 交付物

- [x] WebUI 页面、任务队列、SQLite 数据库和本地目录实现。
- [x] 机器事实、AI 字段、人工覆盖、任务和审核记录的 Schema。
- [x] 导入包完整性报告、任务恢复报告和操作日志。

#### 验证与退出条件

- [x] R2 导入、任务控制和 recover 只能通过 WebUI；产品 CLI 严格只有 `web`/`mcp`。审核、发布和回滚属于后续阶段，R2 未实现且不得新增 CLI。
- [x] 应用重启后只展示 stale 检测结果；显式 recover 后任务状态可恢复，成功任务不重跑。
- [x] SQLite 读写、图片引用、FTS 查询和跨平台路径检查通过。
- [x] WebUI 只监听 `127.0.0.1:8765`，没有 host/port CLI 或环境覆盖，没有账号、CORS 或 CSRF 功能。

**证据区**

- 源码与测试：`src/blockpedia/`、`tests/r2/test_phase1_core.py`、`tests/r2/test_phase2_web_cli.py`、`tests/test_r2_acceptance.py`。这些测试覆盖导入快照/单次 R1 validator、SQLite/FTS/路径安全、六阶段边界、Worker/stale/recover、固定 loopback CLI、Web smoke 契约和安全输出。
- Windows runtime evidence：`docs/evidence/r2-windows-runtime-report.json`，`evidence_type=windows_r2_runtime`、`status=passed`。环境为 Windows 11 build `10.0.26200` AMD64、CPython `3.14.7`；官方 installer SHA-256 为 `sha256:9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649`。
- Windows 依赖与测试：`requirements.lock` SHA-256 为 `sha256:6a551bb7be5f0ec1635bf9cfbc518898d8d2194724228a21f251e48e4cd13894`；3.14.7 venv 中 `pip install --no-cache-dir --require-hashes -r requirements.lock` exit `0`，`pip check` 无 broken；全量 `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider` 输出 `55 passed, 2 skipped, 1 warning in 21.28s`。warning 为已接受的 FastAPI 官方 HTTPX 路径 Starlette deprecation；两个 skip 是既有 Windows symlink 权限分支，不切换 `httpx2`。
- Windows package contents smoke（非 gate）：`docs/evidence/r2-windows-runtime-report.json` 的 `package_contents_smoke` 为 `status=observed`、`gate=false`，仅观察 wheel 包含 `templates/static/vendor/sql`，不证明可复现 build。模块和已安装 `block-index mcp` 均 exit `2`、stdout `0 bytes`、stderr 为稳定 `MCP_NOT_IMPLEMENTED_R4`（仅 Windows CRLF 换行）。
- Windows Web smoke：执行 `block-index web --data-root %TEMP%/opencode/r2-cpython-3.14.7/web-smoke-data --log-level warning` 后 `GET http://127.0.0.1:8765/` 返回 `200`，精确免责声明存在，stdout/stderr 均为 `0 bytes`；测试主动 terminate 后 process code `1` 是 Windows 终止结果，不是启动失败。
- Windows 重跑 `tools.validate_r2` 后，`docs/evidence/r2-validation-report.json` 的所有静态 checks 为 `passed`、`python_baseline_passed=true`、`status=passed`、`issues=[]`，`linux_r2_evidence=false` 且 Linux obligation 标记为 `deferred_to_r5_by_owner_decision`；该报告不声称 Linux 已通过。
- 导入/恢复报告和操作日志的证据来自 runtime 实现与原创临时/fixture 测试；按公开白名单不提交真实导出、非空数据库、真实预览或日志。Linux 实际安装/运行/平台行为未验证且统一 deferred 到 R5；本次不运行 WSL/Linux、不伪造双平台证据。
- R2 实现、Windows/静态/fixture 验证和退出门已完成，R2 blocker 清零，R3 可以开始。R5 必须承接 R2 Linux CPython hash-lock/Web 义务。

### R3：OpenAI 标注与审核

**依赖**：R2 退出门通过；Studio 新任务、配置管理和新 release 构建只能使用一个 active provider profile，其现有 `adapter` 字段必须显式取 `openai_responses` 或 `openai_chat_completions`，并在三阶段使用同一 configured/requested `model_id`；第三方 response model echo 不证明远端实际执行身份。

#### 任务

- [x] 实现 OpenAI Responses 图片/文本输入、`store=false`、strict JSON Schema 和最小披露。
- [x] 按所选显式 adapter 实现并探测 `POST /responses` 与 `POST /chat/completions`：两者均使用图片输入、strict JSON Schema structured output、稳定错误分类和本地校验；Responses 发送 `store=false`，Chat Completions 省略 `store`，成功响应要求 string `model` 但不要求与 requested `model_id` 相等，不自动切换协议或 model。
- [x] 允许多个非活动 profile，但 Studio 新任务、配置管理和新 release 构建全局最多一个 active profile；复用现有 `adapter` 字段并限制为 `openai_responses`/`openai_chat_completions`，同一 configured/requested `model_id` 用于离线标注、QuerySpec 和重排。真实 candidate 已冻结离线标注时的 provider snapshot；MCP 不读取可变 active 状态的实现与验证仍按阶段边界只属于 R4。
- [x] 批量生成受控语义字段；本地校验编号、Schema、枚举、描述长度和机器事实冲突。
- [x] 失败请求最多进行一次总重试，仍失败时创建审核任务，不切换 provider 或模型。
- [x] 实现 WebUI 异常审核、抽样审核、语义人工覆盖和可审核跳过。
- [x] 通过 candidate-build gate 构建至少一个不可变、未激活 candidate，供 R4 临时测试使用；candidate gate 已验证所需 skip/excluded 审核完整性。
- [x] R3 Phase C 高优先级契约已冻结；证据为 [`decisions.md`](decisions.md) 的 2026-08-15 owner 批准记录及 [`webui-and-operations.md`](webui-and-operations.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)、[`quality-and-testing.md`](quality-and-testing.md)、[`data-and-schemas.md`](data-and-schemas.md)、[`security-and-distribution.md`](security-and-distribution.md)、[`architecture.md`](architecture.md) 的 Phase C owner sections。
- [x] D-040 owner-approved workflow contract 已记录；证据为 [`decisions.md`](decisions.md) 的 D-040、[`webui-and-operations.md`](webui-and-operations.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)、[`openai-provider.md`](openai-provider.md)、[`security-and-distribution.md`](security-and-distribution.md) 与 [`quality-and-testing.md`](quality-and-testing.md) 的对应章节；此项只证明文档冻结，不证明实现、真实 provider 请求、candidate 或 R3 退出。
- [x] 实现 D-040 并提供 focused acceptance evidence：plan TOCTOU、sequential/fatal stop、stage semantics、retry generation/idempotency/sibling resolution、frozen lineage、generic retry guard、actionable UI rows、逐批安全预览和 strict request bodies。
- [x] 实现并验证 D-041：persisted aggregate identity、120-job zero-rebuild call-count path、persisted mismatch all-or-none、final pre-send full recompute/zero provider calls 和 lazy per-job safe preview。
- [x] D-042 owner-approved contract 已记录为文档冻结；证据为 [`decisions.md`](decisions.md) 的 D-042 及 [`openai-provider.md`](openai-provider.md)、[`data-and-schemas.md`](data-and-schemas.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)、[`security-and-distribution.md`](security-and-distribution.md)、[`quality-and-testing.md`](quality-and-testing.md) 的对应章节；此项不证明 implementation、真实 annotation 或 R3 退出。
- [x] 实现并验证 D-042：`prompt.v2` slim text、legacy prompt compatibility、六字段 final diagnostic、安全 API/UI disclosure、release lineage replay 和既有 full validation preservation。
- [x] D-044 owner-approved Phase 1 contract freeze 已记录；证据为 [`decisions.md`](decisions.md) 的 D-044 及 [`openai-provider.md`](openai-provider.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)、[`webui-and-operations.md`](webui-and-operations.md)、[`quality-and-testing.md`](quality-and-testing.md) 的对应章节；此项只证明文档冻结，不证明 implementation、tests、provider 请求、runtime 或 R3 退出。
- [x] 实现并提供 D-044 focused acceptance evidence：bounded logical-batch concurrency、shared process-lifetime executor、ordered contiguous claim barrier、final pre-HTTP gate、send-started race/recovery、profile invalidation 和 strict pristine same-run reconfiguration；真实 Windows run 以 frozen concurrency `5` 完成 1140/1140 annotations，证据见本节 D-044 context。
- [x] D-045 owner-approved targeted banner rerender/refresh contract/document/schema freeze 已记录；证据为 [`decisions.md`](decisions.md) 的 D-045 及 [`architecture.md`](architecture.md)、[`export-contract.md`](export-contract.md)、[`state-policy-and-rendering.md`](state-policy-and-rendering.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)、[`webui-and-operations.md`](webui-and-operations.md)、[`quality-and-testing.md`](quality-and-testing.md) 和 [`../schemas/exporter/export-manifest.v1.json`](../schemas/exporter/export-manifest.v1.json) 的对应章节。此项只证明冻结，不证明 Java/Python implementation、targeted export、WebUI refresh、AI jobs、candidate、runtime 或 R3 退出。
- [x] 实现并提供 D-045 最小 focused evidence：Azul Java 25 offline build 成功；CPython 3.14.7 的 service/HTTP/rollback/mixed-lineage 聚焦组合为 `14 passed, 1 warning`，Gate 2 remediation 为 `5 passed, 1 warning`；唯一真实 targeted export `export_20260816T210102Z` 通过 validator，normalized diff 为 32 个 skipped→selected 与 96 个 render files；passed WebUI check `check_776894d7092e43aebd7309be85b48470` 已用于当前 run 的一次真实 refresh。未增加 full matrix、第二次 targeted export 或重复 D-044 verification。

#### 交付物

- [x] OpenAI Responses provider 实现、请求/响应 Schema、提示词版本和缓存键。
- [x] OpenAIProvider 的 Chat Completions adapter/codec、协议条件请求行为和两种协议的真实能力 probe；未新增 profile required field、Schema ID、依赖、服务、CLI 或 SQL。
- [x] 审核队列、人工覆盖记录、资格等级和跳过原因报告。
- [x] 不含 Token usage、费用或预算字段的任务与设置页面。
- [x] 至少一个通过 candidate-build gate 的未激活 candidate；真实证据为 `rel_357c6104fe8ccfc0f4b7823a68ccc84e`，且根 `current.json` 不存在。

#### 验证与退出条件

- [x] 无效 JSON、错误编号、机器事实冲突和低置信度结果均按规则进入审核。
- [x] LLM 无法修改 ID、合法状态、几何、机器行为、发布状态或候选资格。
- [x] Keyring/`OPENAI_API_KEY` 读取、掩码和日志泄漏检查通过。
- [x] 1172 个 selected variants 的 annotation 与审核决定已完整回放进 candidate；全部 open reviews 已清零，没有未经审核的高优先级冲突。
- [x] candidate-build gate 通过且 candidate 未激活生产 current。

**证据区**

- 实现证据路径为 [`src/blockpedia/provider.py`](../src/blockpedia/provider.py)、[`src/blockpedia/r3.py`](../src/blockpedia/r3.py)、[`src/blockpedia/worker.py`](../src/blockpedia/worker.py)、[`src/blockpedia/services.py`](../src/blockpedia/services.py)、[`src/blockpedia/releases.py`](../src/blockpedia/releases.py)、[`src/blockpedia/web.py`](../src/blockpedia/web.py)、[`src/blockpedia/sql/release-index.v1.sql`](../src/blockpedia/sql/release-index.v1.sql) 及 [`tests/r3/`](../tests/r3/)；Phase A/B/C 的 Oracle gate 及最终双协议 provider 边界复审均已通过。
- Windows CPython `3.14.7` 最终代码级命令 `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q` 为 `327 passed, 3 skipped, 1 warning in 437.94s`；D-042 prompt/provider/pipeline/release/Web 聚焦组合测试为 `190 passed`，Oracle 最终复核为 `PASS`。真实本地 8-item fixture 的 prompt 从 legacy `8964` 字降至 v2 `1047` 字，请求 JSON 从 `79957` 降至 `71405` bytes；该数字只证明披露缩减，不声称质量提升。D-040/D-041 的 120-job zero-rebuild、source-change zero-provider-call 证据继续有效。唯一 warning 是既有 Starlette/httpx deprecation。`python -m tools.validate_r0 --repo-root .` 为 `26 schemas, 52 fixture case(s)`；`python -m tools.validate_r2 --repo-root . --report <temporary-report>` 退出码为 0；22 个 Jinja 模板编译、`node --check src/blockpedia/static/studio.js` 和 `git diff --check` 均通过。
- Windows Keyring 以原创临时凭据完成 WinVault set/get/delete smoke，未打印 secret；provider/profile、总尝试不超过 2、严格 Schema、机器事实只读、审核 replay、candidate Gate C、TOCTOU、不可变 hash/layout、无 `current.json` 写入均有 fixture/故障注入测试。fixture candidate 只存在于临时 data-root，不是可用于宣称 R3 退出的真实 candidate。
- 本地 data-root 中一个用户批准的 Chat Completions profile 已于 `2026-08-15T02:57:07Z` 完成真实能力 probe；另一个选择 `prompt.v2` 的用户批准 Responses profile 已于 `2026-08-15T15:56:19Z` 完成真实能力 probe。两者的图片输入、strict structured output、本地校验和稳定错误分类均通过；这些结果不进入公开 fixture，也不证明 retention 或远端实际模型身份。model mismatch 不是 equality blocker，也不构成远端模型身份证据；Responses response echo 不是 R3 退出 blocker；任何协议都不据此声称远端 storage 已验证，第三方 trust/policy 由用户负责。多个 profile、显式 adapter、单 active profile 和 release-bound provider snapshot 的 R3 evidence 已由真实 candidate 闭合；MCP 不读取可变 active 状态的代码与 runtime evidence 仍由 R4 清单验证。
- `2026-08-15` 新 exporter sibling `run/blockpedia-data/exports/26.2/export_20260815T162140Z/` 经 `python tools/validate_r1_export.py --repo-root . --export-dir run/blockpedia-data/exports/26.2/export_20260815T162140Z --report run/blockpedia-data/reports/export_20260815T162140Z-validator.json` 验证为 `passed`、`issues=[]`，随后只经 WebUI check/import 创建 fresh run 并冻结 Responses/`prompt.v2` lineage。125 个 8-item AI jobs 全部完成首轮：110 个 `succeeded`、15 个 `needs_review`、0 个 `failed`；`provider_requests` 的 125 条首轮终态记录中，95 条 attempt 1 成功、15 条 attempt 2 成功，余下为 9 个 `PROVIDER_TIMEOUT`、2 个 `PROVIDER_SERVER_ERROR` 和 4 个 `PROVIDER_OUTPUT_ID_MISMATCH`。经用户显式批准 15-job retry wave 后，13 个 child jobs 成功，2 个仍为 `PROVIDER_OUTPUT_ID_MISMATCH`；再经用户批准一次 2-job retry wave 后，1 个成功、1 个仍为 ID mismatch；人工检查确认最后批次返回 8 项但 exact ID set 不匹配后，用户又显式批准且只批准一次 1-job wave，最后 child 成功，所有 waves 均为 0 failed。最终重新 VALIDATE 后本地共有 125 个 `annotation-batch-output.v1` artifact 和 1000 个不同且非空的 `visual_variant` `subject_id`，open `PROVIDER_FAILURE` 为 0，144 个历史 provider-failure tasks 均已 resolved，run 安全停在 `HUMAN_REVIEW/needs_review`。当前 1196 个 open tasks 由 152 `EMPTY_RENDER`、3 `MISSING_TEXTURE`、41 `OBJECT_OFF_CANVAS` 和 1000 `SAMPLED_QUALITY_REVIEW` 组成；该结果证明真实 1000 selected annotation 完整，但不证明人工审核、高优清零、candidate 或 R3 退出。
- Linux wheel/ABI、实际安装/运行、`renameat2` 平台行为和双平台复现按冻结决策统一归 R5；本阶段没有伪称 Linux 已通过。R3 Windows 退出门已通过，R4 可以开始；生产 activation 仍属于 R5 gate，当前不得激活。
- D-040 workflow、D-041 scalable aggregate authorization 和 D-042 prompt/diagnostic 实现及 focused acceptance evidence 已完成；其早期 1000/1000 annotation 与 1196 open reviews 是历史中间状态，已由后续 D-043/D-044/D-045 corrected run、完整审核和真实 candidate evidence supersede，未对旧 run 原地迁移。
- D-043 进一步冻结：现有 R3 run 的渲染输入来自缺陷 exporter，保持 historical/superseded；其 `152` 个 rerender events 保留为审计证据且不得执行。除 D-045 对精确 32 个 banner targets 的一次显式 refresh 外，不得从两个旧导出或该 run 继续 candidate；candidate 只能等待 corrected exporter 的 targeted smoke、恰好两次同环境导出和既有 validator/Pairwise evidence。
- D-044 在 corrected export 的 fresh run `run_f50e48732ffc4005aadd45e90baeb6b9` 上完成真实 Windows 并发运行证据：CPython `3.14.7` 当前全量套件为 `367 passed, 3 skipped, 1` 个既有 Starlette/httpx deprecation warning；Oracle Gate 1/Gate 2 均最终 `PASS`。WebUI 只改 `jianshang` 的 `offline_annotation.concurrency` 为 `5` 且保留 active/verified/Keyring；同一 pristine run 由 `R3_RUN_RECONFIGURED` 将 effective config hash 从 `sha256:459d976166f13317bbd9e5cd4ab530e611e2fc50710326620783688b9e1ae2d5` 更新为 `sha256:3787f0c8a22b58de0341ee8e24769aaba7baa7e75bfa2b165b04296acbc20458`，保留六个 R2 succeeded stages 和原 `started_at`。用户显式批准 95-job plan `sha256:dbd89d352a44f670ef6924c416e4a0affb9d3f1c5d1dfadda9cbe6fe913aad77` 后，运行时观测到恰好 `5` 个并发 running jobs；首轮为 `84 succeeded / 11 needs_review`。用户随后分别批准 11-job wave `sha256:24cb3cdc555eadf541a65b714e2ebe4788fcd39f17e5384794e5d2d9fb5b556a` 与 1-job wave `sha256:e02633d379dd2410de5ad51ee01c2dacec84055333f6bb0fe38c2074c45d5b26`，最终 95 条成功 provider request 形成 1140 个互异 `visual_variant` annotations，覆盖 `1140/1140` selected variants，missing 为 `0`；12 条历史 item-local requests（7 timeout、2 network、2 output-ID mismatch、1 server）及 144 条 resolved provider reviews 均保留，open `PROVIDER_FAILURE=0`。run 当前安全停在 `HUMAN_REVIEW/needs_review`，1196 条 open reviews 由 1140 条 `SAMPLED_QUALITY_REVIEW`、2 条 `BLOCK_ENTITY_FIXTURE_UNSUPPORTED`、10 条 `EMPTY_RENDER`、41 条 `OBJECT_OFF_CANVAS`、3 条 `OBJECT_TOO_SMALL` 组成；该证据不证明人工审核、高优清零、candidate 或 R3 退出。
- D-045 真实 Windows evidence/context（不是 public contract default）：run `run_f50e48732ffc4005aadd45e90baeb6b9` 从 base import `import_252af05e5ba44d81aaec6d26cbea8048` / export `export_20260816T091512Z` 经唯一 targeted export `export_20260816T210102Z` 刷新为 import `import_a114c34344af419b9c33cc63abfc90fa`。外部 validator report `run/blockpedia-data/reports/export_20260816T210102Z-validator.json` 的 SHA-256 为 `sha256:d02cd6537fb39565c9a08d61b195d44af22dbc0cb342928049bc8ec0b4cfc5cf`；passed WebUI check 绑定 manifest `sha256:756f854cc12976eb79b6fa565b04d3fdf67cc077f082f2fd646ae0b48d9bfdff` 与 checksums `sha256:801649f4f051d398b518676063eeb9832478ef81ee3a889331f9173eda4cfd6d`。用户显式批准 3-job plan `sha256:13fae5ae898ba79ab790673caad14470f4b708c730b341370cc9dc3d73bcd132` 后，3 条新增 `offline_annotation` requests 全部成功，形成 32 个 banner annotations；最终 workspace 为 1196 blocks、32366 states、1172 selected variants/features/annotations。用户随后显式接受所有当前结果：1172 个 annotation reviews 以 `accept` 解决，15 个机器失败项以有 failure evidence 的 `skip` 解决，open reviews 清零；HUMAN_REVIEW 成功后 run 到达 `BUILD_RELEASE` 边界。candidate check `check_dc4c618b1a1004c57972674fbcb57e06` 以 snapshot `sha256:ac029aba05dbbfe256757e0ce161987750bc93aefeac123de479476c97d55be4` 和 quality report `sha256:beabf8de263b6153619397917a1e06a1071d5d961f2a77e518bdc8066ee7be9b` 通过；用户确认后构建未激活 release `rel_357c6104fe8ccfc0f4b7823a68ccc84e`。release layout 为冻结的八项，manifest `sha256:c3e0bd6af1b59748b944d170e373e6136b331d87ab583498aaecb220111249c5`、quality report `sha256:9566211dfa82c35f14e0aa39174e1ea12489635f2851e699f25f5ada94798528`、checksums `sha256:e83d3fc9474f4d8a51f4a7545c7e7159bc6c48fff0c27bdcb79e8552fedcff40`；`release.json` 只有 `built_at`、没有 `published_at`，根 `current.json` 不存在。run 现停在 `ACTIVATE_RELEASE/paused`，boundary 为 `R3_CANDIDATE_BUILT_ACTIVATION_PENDING`；R3 退出完成，但 production activation 仍未执行。

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
- [ ] Windows 11 x86_64 的 stdio 启动和关闭、四工具功能门通过。

**证据区**

- 尚无 MCP 源码、协议交互记录、临时 fixture、release candidate 或测试报告；本阶段所有项保持未勾选。
- 退出门未通过，R5 不得开始。

### R5：完整性收敛与首发

**依赖**：R0 至 R4 退出门全部通过；candidate check/build 不要求 R4，activation-check/apply 才要求 R0-R4、activation gate 和用户确认。

#### gate 定义

- **candidate-build gate**：只检查内容完整性，包括 100% 注册表覆盖、变体或审计跳过、图片可读、合法状态、机器与 AI Schema、人工覆盖引用、无未解决高优先级审核和 FTS。它 **不** 检查 MCP smoke、`TWO_INDEPENDENT_RELEASES` 或 current 切换。
- **activation gate**：在 candidate-build gate 之上检查四工具 MCP smoke、同一 Minecraft 版本至少两个独立通过 candidate-build gate 的不可变 release、manifest/checksums 可复算以及 `current.json` 原子切换。activation gate 通过后仍由用户人工激活。

#### 任务

- [ ] 承接并验证全部 deferred Linux 义务：R2 CPython `3.14.7` hash-lock 安装/`pip check`/Web smoke，R4 MCP stdio，Linux wheel/ABI，Linux Java/runtime/exporter，以及最终双平台源码锁依赖和运行时复现。
- [ ] 执行 candidate-build gate，并为每个目标版本生成至少两个独立且不可变的完整 release，写入 manifest、release metadata 和统一 `sha256:<64 lowercase hex>` 哈希。
- [ ] 执行 activation gate：四工具 MCP smoke、两个独立 release 和原子 current 均通过。
- [ ] 通过 WebUI 原子更新 `current.json`，验证默认版本、显式版本选择、切换和回滚不修改历史 release；最后由用户人工激活。
- [ ] 生成首发清单和可复现运行说明；不制作安装包、容器、系统服务或自动更新。

#### 交付物

- [ ] 通过 candidate-build gate 的 release 目录、manifest、质量报告和未激活 candidate 记录。
- [ ] activation gate 的四工具 smoke、两个 release、哈希、切换和回滚证据。
- [ ] 首发前文档、源码锁依赖和空数据库/fixture 生成器分发清单。
- [ ] Linux deferred obligations、Linux wheel/ABI、Java/runtime/exporter 和最终双平台复现报告。

#### 验证与退出条件

- [ ] 每个目标版本均有两个或以上独立完整 release，且通过 activation gate。
- [ ] 发布和回滚均为 WebUI 操作，release 内容在操作前后字节不变。
- [ ] MCP 四工具读取临时/当前 release 的冒烟检查通过，stdout 保持纯净。
- [ ] R5 完成 Linux x86_64 CPython `3.14.7` hash-lock 安装、`pip check`、Web smoke 和实际平台行为验证；完成 Linux wheel/ABI（`manylinux_2_17` / glibc `>=2.17`）验证、Linux MCP stdio 启动/关闭、Linux Java 25/runtime/exporter 独立重跑，并完成 Windows/Linux 最终双平台源码锁依赖和运行时复现；此项统一承接 R2/R4 及 R1 延后的 Linux 义务。
- [ ] 用户完成最终人工激活；在此之前不得把任何 candidate 说成首发。
- [ ] 未把黄金查询、Top-5 或排序调优结果冒充为本阶段验收；这些质量工作明确后置，不是路线图必做项。

**证据区**

- 尚无 release、manifest、`current.json`、回滚日志、首发清单或跨平台报告；本阶段所有项保持未勾选。
- 因实现、测试、真实数据和发布均未发生，MVP 尚未首发。

## 明确后置且不构成 MVP 退出条件的工作

黄金查询集、Top-5 命中率、硬约束质量统计和排序权重调优必须在 MVP 闭环之后另行定义、采样和实测；当前不预设通过率，不在本路线图中设为必做交付物，也不允许用目标数字代替实测证据。
