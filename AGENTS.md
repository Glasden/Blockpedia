# Blockpedia 仓库硬限制

本文件是本仓库最高优先级工程规范。所有开发、测试、数据生成和发布工作都必须遵守这里的 `MUST` / `MUST NOT` 规则。

## 开始工作与规范优先级

1. 开发者开始任何实现前 **MUST 先完整阅读** [`docs/roadmap.md`](docs/roadmap.md) 和 [`docs/decisions.md`](docs/decisions.md)，随后阅读 [`docs/product-scope.md`](docs/product-scope.md)、[`docs/architecture.md`](docs/architecture.md) 以及路线图列出的具体契约文档。
2. 规范优先级固定为：本文件 > [`docs/roadmap.md`](docs/roadmap.md) 与 [`docs/decisions.md`](docs/decisions.md) > 具体设计文档 > [`docs/minecraft_vanilla_block_index_mcp_design.md`](docs/minecraft_vanilla_block_index_mcp_design.md)。
3. 移入的原始设计稿仅是历史背景和最低优先级参考，不能与新文档一起作为执行规范；冲突内容禁止实现。发现冲突时 **MUST 先更新高优先级文档并留下影响记录**，**MUST NOT 静默偏离**。
4. 所有复选框 **MUST** 以可复核路径、精确命令输出、哈希、测试报告或发布清单为依据；没有证据 **MUST NOT** 标记完成。当前仓库没有实现、真实导出数据、发布索引或测试报告，因此实现、测试、数据和发布相关复选框保持未勾选。

## 冻结技术基线与替换控制

- 正式支持平台 **MUST** 是 Windows 11 x86_64 和 Linux x86_64，Linux wheel/ABI 基线为 `manylinux_2_17` / glibc `>=2.17`。
- Minecraft 基线 **MUST** 固定为：Minecraft Java `26.2`、Java `25`、Fabric Loader `0.19.3`、Fabric API `0.157.0+26.2`、Loom `1.17.19`、Gradle `9.5.1`；Minecraft 26.2 使用 native Mojang names/unobfuscated，不解析外部 mappings artifact。
- Python 基线 **MUST** 固定为 CPython `3.14.7`。
- Fabric 导出模组 **MUST** 仅使用上述固定基线；导出包 **MUST** 记录完整运行时清单。
- R0 退出前只要求为 R0 tooling 实际引入的 Python 依赖锁定精确版本和 hashes；后续任何依赖在使用前 **MUST** 精确/hash 锁定，并在 Windows 11 x86_64 与 Linux x86_64 目标上重新验证。
- 任何等价技术替换在编码前 **MUST** 在 [`docs/decisions.md`](docs/decisions.md) 写入影响记录，逐项证明不破坏本地单机运行、可复现构建、无额外服务、MCP 只读边界和既有数据契约，并获得项目所有者明确书面批准。没有这两项证据 **MUST NOT** 替换。

## 本地工具链选择与最小实现纪律

- 在 Windows 执行 Fabric/Gradle 命令前，代理 **MUST** 先使用已安装的 Azul/Zulu Java 25。当前可用安装为 `C:\Users\Glasden\.jdks\azul-25.0.2`；若该精确目录变化，应先在 `C:\Users\Glasden\.jdks\azul-25*` 中定位现有 Java 25，再为命令设置 `JAVA_HOME` 和 `PATH`。**MUST NOT** 因系统默认 `java` 指向 Java 17 就反复运行已知会失败的 Gradle 命令、降低 Java 基线或修改工程配置；Java 17 最多只可用于一次环境诊断。
- 实现 **MUST** 选择满足当前阶段验收的最小方案。**MUST NOT** 为未开始的后续阶段预建通用框架、扩展点、冗余字段、重复契约、通用规则引擎或多层验证体系；同一事实不得同时在 Markdown、Schema 和实现中维护三份逐字段定义。只有当前需求、已观察失败或明确验收项需要时，才允许增加抽象或校验。
- 审查发现非阻断性的命名、数组上限、内部视图或未来扩展细节时，**MUST NOT** 阻塞当前阶段或开启反复设计轮次；优先采用现有最简单一致实现，并用测试覆盖当前可观察行为。
- **NO Over-Engineering, NO Over-Testing**：已有可复核文件、哈希、测试或成功构建已经证明的事实 **MUST** 直接复用；相关输入未变化且没有失败时，**MUST NOT** 重复派发代理、重复评审、重复执行同类验证，或仅为“更完整”而新增报告、证据层、平台矩阵和阻断门。每个阶段只保留证明该阶段交付物所需的最小一次验收。
- R0 只冻结契约、最小 Schema/fixture 验收、依赖锁和可构建工具链骨架。真实 Minecraft 运行/导出属于 R1，Python 产品运行时属于 R2，双平台端到端复现属于对应实现阶段和 R5；这些后续事实 **MUST NOT** 倒灌为 R0 blocker。R0 已有最小验收证据后必须关闭并进入下一阶段，不得继续加固设计。

## MVP 边界与组件职责

- MVP **MUST** 形成 R0 契约冻结、R1 确定性导出、R2 Index Studio/存储/任务、R3 OpenAI 标注与审核、R4 MCP 查询、R5 完整性收敛与首发的端到端闭环；路线图 **MUST NOT** 增加 MVP 以后的编号阶段。
- 默认架构 **MUST** 使用 Fabric + Python/FastAPI/Jinja2/HTMX + SQLite + 本地文件 + 进程内 Worker；**MUST NOT** 引入 Redis、Celery、Kafka、微服务、向量数据库、对象存储或其他额外运行服务。
- Fabric exporter 是注册表枚举、代表状态选择和 Minecraft 内渲染的唯一执行者。其内部顺序冻结为：`EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS`。
- Python Index Studio 只负责导入/验证 exporter 已产生的 variants/renders、提取离线特征、AI/审核以及构建 release；Python **MUST NOT** 重新选择 variants 或重新渲染。Studio 的阶段顺序冻结为：`PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE`。
- Python 命令行 **只允许** `block-index web` 和 `block-index mcp`。导入、恢复、审核、发布、回滚等写操作 **MUST** 全部从 WebUI 发起；**MUST NOT** 增加对应的 Python 子命令。
- MVP **只实现** OpenAI Responses、一个 `OpenAIResponsesProvider` adapter 和 strict JSON Schema；可以保存多个 provider profile，但 **MUST NOT** 实现 OpenAI Chat Completions、Anthropic Messages 或其他 provider adapter。
- 兼容 OpenAI Responses 语义的 `base_url` 是同一 `OpenAIResponsesProvider` 的用户批准配置，不是第二个 provider adapter；它 **MUST** 通过同一协议能力门并实际使用 `store=false`。
- 每个外部模型请求 **MUST** 最多进行一次总重试；重试后仍失败必须进入可审核失败状态，**MUST NOT** 无限重试或隐式切换模型。
- MVP **MUST NOT** 记录或展示 Token usage，**MUST NOT** 实现费用、预算或价格估算功能。
- MVP **MUST NOT** 引入通用 SQLite migration framework；数据库使用一份经 R0 冻结的 schema，结构变化须先更新高优先级契约并重新建立数据库。
- 黄金查询集、Top-5 指标和排序权重调优是后置质量工作，**MUST NOT** 作为 MVP 路线图的必做退出条件或虚假验收结果。
- MVP **MUST NOT** 制作安装包、容器、系统服务或自动更新；源码和精确锁依赖必须支持可复现的本地运行。

## 数据覆盖、事实分层与 Schema 命名空间

- 导出器 **MUST** 登记 `minecraft` 命名空间注册表中的 100% 方块；不得只登记看起来适合建筑的方块。
- 每个注册表方块 **MUST** 有一条 `Block` 记录，并且必须有至少一个可发布视觉变体，或有经过审核、带原因和证据的可审计跳过记录。
- BlockState、合法属性值、`block_id`、默认状态、状态字符串、几何、碰撞、透明度、发光、支撑、渲染哈希等机器事实 **MUST** 由 Minecraft 运行时和确定性 exporter 提供；这些字段在 WebUI 中 **MUST** 只读。
- 数据 **MUST** 分为不可变机器事实、AI 语义建议和人工覆盖三层。人工覆盖 **MUST** 以独立记录保存并可重放，不能把覆盖值伪装成机器事实。
- 候选资格 **MUST** 使用受控等级 `eligible`、`conditional`、`excluded`。`conditional` 必须附带警告；`excluded` 和无变体的方块必须附带审核者、时间、原因码和备注。
- LLM **MUST NOT** 创建、改写或猜测 `block_id`、合法状态、状态字符串、几何、机器行为、发布状态或候选资格；LLM **只允许** 生成受控 Schema 内的语义字段和候选解释/排序理由。
- Schema ID **MUST** 按边界分命名空间，并冻结为以下当前 ID：exporter 使用 `export-manifest.v1`、`export-block.v1`、`export-state.v1`、`export-variant.v1`、`export-failure.v1`、`render-metadata.v1`；workspace/release 使用 `block-record.v1`、`state-record.v1`、`visual-variant-record.v1`、`annotation-record.v1`、`manual-override.v1`、`skip-review.v1`、`qualification-review.v1`、`release-manifest.v1`、`release.v1`、`current-pointer.v1`；provider 使用 `provider-batch-envelope.v1`、`annotation-batch-output.v1`、`annotation-wire-item.v1`、`query-spec-output.v1`、`rerank-output.v1`；MCP 使用 `mcp-index-info-output.v1`、`mcp-search-blocks-output.v1`、`mcp-block-details-output.v1`、`mcp-compare-blocks-output.v1`，可共享 `mcp-error.v1`。旧 ID `block.v1`、`state.v1`、`variant.v1`、`annotation.v1`、`annotation-item.v1`、`query-spec.v1`、`visual-rerank.v1`、`manifest.v1`、`override.v1` **MUST NOT** 作为新规范当前 ID；原始历史稿例外且不改。R0 **MUST** 把 Markdown 契约物化为真实 JSON Schema 文件并验收，未有文件和证据前不得勾选。provider wire Schema ID 与 Responses `text.format.name` 分开；固定 name 分别为 `annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`，只含字母数字下划线且长度不超过 64。
- 禁止把 Minecraft 原版 JAR、纹理、模型、截图或其他原版资产提交到仓库。运行时只可由本地合法游戏环境生成导出数据和预览。

## 数据根、release、版本与发布门

高层唯一数据根目录树冻结为：

```text
<data-root>/
├── exports/{minecraft_version}/{export_id}/
├── workspace/{minecraft_version}/{run_id}/
├── cache/
├── releases/{minecraft_version}/{release_id}/
├── logs/
└── current.json
```

不得使用 `work/` 或 `published/` 作为高层目录名。`MCP MUST NOT` 写 `logs/`；MCP 不得产生任何本地持久化写入。

唯一 release layout 高层引用为：

```text
release.json
manifest.json
index.sqlite3
previews/
quality_report.json
manual-overrides.json
schemas.sha256
checksums.sha256
```

不得使用 `release.sha256`、YAML override 或 `contact-sheets/` 作为 release 契约名称。JSON、Schema 字段、metadata、manifest、current 和 release 中的 hash 字符串统一表示为 `sha256:<64 lowercase hex>`；唯一文本例外是 `checksums.sha256` 与 `schemas.sha256` 的行首 digest，均不带前缀。`checksums.sha256` 排除自身，格式为 `<64hex><two spaces><release-relative-posix-path>\n`，按路径排序；manifest 只哈希功能输入/产物且不自引用，`release.json` 可以保存 manifest hash。`schemas.sha256` 是 Schema inventory，格式为 `<64hex><two spaces><schema-id><two spaces><canonical-repository-relative-posix-path>\n`，按 schema-id UTF-8 字节序排序，不声称这些路径位于 release；细节由 pipeline 具体契约定义。

- 多个 Minecraft 版本 **MUST** 共存；WebUI 的导入、任务、构建和发布 **MUST** 显式选择 `minecraft_version`，不得用隐式“最新版本”。
- `current-pointer.v1` **MUST** 是根 `current.json` 的唯一指针格式；严格顶层字段冻结为 `schema_version`、`versions` map、`default_minecraft_version` 和 WebUI 激活/回滚时更新的 `updated_at`。MCP 省略版本时使用 default version 对应的 current；显式版本时使用该版本 current；未知或未发布的精确版本必须失败且不得回退。MCP **MUST NOT** 支持显式历史 `release_id` selector；历史切换只能由 WebUI rollback 完成。
- `current.json` 只能由 WebUI publish/rollback 原子更新；发布目录生成后 **MUST NOT** 原地修改。回滚只切换到已有完整不可变 release，不修改 release 内容或删除审计证据。
- 首次激活首个 Minecraft 版本时 `set_as_default=true` **MUST**；后续 apply 必须显式提供 `set_as_default` 决定是否切换 `default_minecraft_version`。激活时间只写 workspace activation audit 和 current `updated_at`，不得写回不可变 release；release 使用 `built_at`，不使用 `published_at`。
- `candidate-build gate` 只检查内容完整性，不包含 MCP smoke、`TWO_INDEPENDENT_RELEASES` 或 current 切换。R3 **MAY** 构建至少一个不可变、未激活 candidate 供 R4 测试；R4 使用临时测试 data-root/current fixture 做 MCP 测试，不激活生产 current。R5 先构建至少两个独立通过 candidate-build gate 的 release，再执行 activation gate（四工具 MCP smoke、两个 release、原子 current），最后由用户人工激活。
- candidate check/build 的前置只要求 R0-R3 与 candidate-build gate；activation-check/apply 的前置才要求 R0-R4、activation gate 和用户确认。不得把 R4 要求倒灌到 candidate check/build。
- `excluded` 的 `qualification-review.v1` 完整性属于 candidate-build gate；不可变 candidate 构建前必须齐全。activation gate 只复核 candidate 报告及其 hash，不首次补做资格内容审计。
- 发布完整性门 **MUST** 检查 100% 注册表覆盖、变体或审计跳过、图片可读、合法状态、机器与 AI Schema、人工覆盖引用、无未解决高优先级审核和 FTS；activation gate 另检查四工具 MCP smoke、两个独立 release 和原子 current。任一项失败 **MUST NOT** 激活。

## WebUI、秘密与恢复

- WebUI **MUST** 且只能绑定 `127.0.0.1:8765`。**MUST NOT** 通过 `--host`、`--port` 或环境变量覆盖 host/port；启动 CLI 只能覆盖 data root、日志等级等非冻结项。**MUST NOT** 添加账号系统、CORS 或 CSRF，也不得绑定局域网或公网地址。
- 启动时只能检测 stale 任务并展示，不得写回任务状态；状态变更必须由 WebUI `recover` 操作触发。成功任务不得重跑。
- API Key **MUST** 优先存放操作系统 Keyring；允许仅从环境变量读取。推荐环境变量为 `OPENAI_API_KEY`，Keyring 服务名为 `blockpedia`、账户为 profile ID；Keyring 优先于环境变量。
- 允许保存多个非活动 provider profile 便于切换，但全局任一时刻最多一个 active profile；该约束仅适用于 Studio 新任务、配置管理和构建新 release。每个 release **MUST** 冻结其离线标注时的完整非秘密 provider snapshot，包括 `profile_id`、`model_id`、`base_url_stable_id`、不可逆 `secret_reference` 及相关版本；MCP **MUST NOT** 读取可变 active 状态或 workspace 数据库，只能按 resolved release snapshot 使用同一 `model_id` 执行 QuerySpec 与 rerank。secret 无法解析或能力不再通过时，MCP 本地降级并返回 warning。release-bound snapshot 不算第二个 active profile，也不能用于 Studio 新写任务。
- API Key **MUST NOT** 写入 SQLite、普通配置文件、导出包、提示词、异常、日志、截图或前端响应；数据库和 release 只能保存不可逆的 `secret_reference`，WebUI 只能显示掩码。
- OpenAI Responses 每次请求 **MUST** 使用 strict JSON Schema、最小必要披露和 `store=false`。能力探测必须证明 endpoint 支持并实际接受/使用 `store=false`；不能证明支持或实际使用时探测失败并禁止 enable，任何 warning/ack 都不得绕过该硬门。
- `POST /api/runs` 创建 run 时不需要 `release_build_id`；后续 release check 根据 `run_id` 创建并返回 `release_build_id`。WebUI 的渲染修复操作必须命名为 `request_reexport` 或 `request_exporter_rerender`，不得暗示 Python rerender。
- 持久化状态机中 run 与 stage 只允许 `pending|running|paused|needs_review|failed|succeeded|cancelled`，item 可额外使用 `skipped`；`draft`、`ready`、`pause_requested`、`cancel_requested` 不得作为持久状态，暂停/取消是命令或事件。

## MCP 边界、输出与公开白名单

- `block-index mcp` **MUST** 只提供 `stdio`，且只提供四个工具：`index_info`、`search_blocks`、`get_block_details`、`compare_blocks`。
- MCP 查询 **MUST** 解析到不可变 release 后再读取；除读取 release、`current.json` 和必要的 Keyring 秘密引用外不得产生本地写入，尤其不得写数据库、文件、cache、logs 或 release 指针。
- MCP 的 stdout **MUST** 只输出 MCP 协议消息；日志、诊断和堆栈 **MUST** 输出 stderr，**MUST NOT** 污染 stdout，也不得写本地日志文件。
- MCP 返回的 `block_id`、状态、图片映射和 release 元数据 **MUST** 来自 release；模型不能新增候选或改写这些字段。模型不可用时可返回确定性本地候选并明确 `reranked_by_llm=false`，不得伪装为已重排。
- 公开白名单只允许源码、文档、真实 JSON Schema、空数据库和 fixture 生成器源码；不得提交生成后的 PNG、非空数据库、真实索引、预览、导出包、人工覆盖或秘密。任何真实数据只能在本地生成或保存。

## 证据、测试与路线图纪律

- 每个阶段的 `[x]` **MUST** 对应仓库路径、精确命令输出、SHA-256、测试报告或发布清单；设计意图、口头承诺和空目录不能作为证据。
- R0 之后每个阶段 **MUST** 先满足前一阶段退出门，**MUST NOT** 绕过依赖门直接进入下一阶段或发布。
- 公开分发内容不得包含真实索引、预览、导出包、人工覆盖中的本地数据和秘密。
- 任何声称“完成”的说明 **MUST** 能由路径和证据复核；**MUST NOT** 为通过审查而伪造数据、测试、哈希、API 响应或发布记录。
