# Blockpedia MVP 路线图

- **版本**：MVP 冻结版
- **日期**：2026-08-14
- **目标**：在 Windows 11 x86_64 和 Linux x86_64（`manylinux_2_17` / glibc `>=2.17`）上完成原版 Minecraft Java 26.2 的本地单机端到端闭环
- **当前状态**：R0 契约冻结、R1 确定性导出和 R2 Index Studio/存储/任务均已按 Windows、静态与 fixture 证据关闭；R3 可以开始。Linux CPython/Web、Linux MCP stdio、Linux wheel/ABI、Linux Java/runtime/exporter 和最终双平台复现统一 deferred 到 R5；正式支持平台与 Linux 基线不变，当前不声称 Linux 已通过。

规范优先级和硬限制见 [`../AGENTS.md`](../AGENTS.md)；冻结决定集中见 [`decisions.md`](decisions.md)。冲突必须先更新高优先级文档，不能在实现中静默偏离。移入的原始设计稿仅是历史背景和最低优先级参考，不能与新文档一起作为执行规范；其中冲突内容禁止实现。

## 路线图文档索引

当前仓库已完成最小 R0 物化和验收，并已按 Windows 证据关闭 R1、R2。R1 的当前 v1 身份、路径、哈希删留和 exporter/外部 validator 职责已冻结；所有 Linux 实际运行/安装/平台行为与最终双平台复现义务统一保留在 R5。R2 退出门已关闭，R3 可以开始；后续阶段只在实际实现需要时增加测试与平台证据，不重复设计或为未来阶段预建验证体系。

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

**证据区**

- Windows 当前 v1 真实导出：`run/blockpedia-data/exports/26.2/export_20260813T213208Z/`，exporter `0.1.3`，status=`needs_review`，1196 blocks、32366 states、1000 selected、196 skipped/pending；`variant_id == block_id`，抽查 `minecraft:stone` 的 preview 为 `renders/minecraft/stone/preview.png`。`manifest.json` SHA-256 为 `ea7ee7c144dd47244c1bc837a6c89c4d2100ac7eed94615021389ced75d54c82`，`checksums.sha256` SHA-256 为 `2f3541f331d5fe7cf6c72e625bba32819c597dfbe631aa531aa06c7cbf851026`。首次同版本尝试因外部 Notepad 占用 staging 内 `exporter.log` 导致 Windows rename 拒绝并仅保留 staging；关闭外部占用后 fresh 重跑于一次原子 rename 成功提交，失败 staging 未作为消费者输入。
- 构建与聚焦验收：使用 `C:\Users\Glasden\.jdks\azul-25.0.2` 执行 `./gradlew.bat --offline compileJava` 和 `./gradlew.bat --offline build` 均为 `BUILD SUCCESSFUL`；`blockpedia-exporter-0.1.3.jar` SHA-256 为 `fe6686998160df91a3b6b0d44a30ace9926cb41c0987157be576c6d262660a2f`，sources JAR 为 `8961897cd6584b10e146d5b964f24f037a9acc01d6a533bb774ff650b75a0584`。`python -m pytest -q tests/test_r0_schemas.py tests/test_r1_export_validator.py` 为 `9 passed, 2 skipped`；两个 skip 是当前 Windows 环境无 symlink 创建权限，hardlink 与其它检查实际执行。
- 优化后外部验收：`python tools/validate_r1_export.py --repo-root . --export-dir run/blockpedia-data/exports/26.2/export_20260813T213208Z --report run/blockpedia-data/reports/export_20260813T213208Z-validator.json` 输出 `R1 export validation passed`，实测 `516.836s`，未延长 600 秒上限；本地报告 status=`passed`、issues=`[]`，SHA-256 为 `7a839ba3f3b60ae87cbe4a55e1e4ddc3af6ce3c05f90a4f9f02f48404c5e1ec7`。Linux Java 25/runtime、Linux exporter 独立重跑和最终双平台源码/运行时复现尚无证据；按 owner 于 2026-08-14 批准的阶段门重分配，这些义务保留在 R5，不构成 R1 未完成条件。R1 已于 2026-08-14 关闭，R2 可以开始。
- 退出门已通过；R2 可以开始。Linux 和最终双平台验证仍由 R5 的未勾选门负责。

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
