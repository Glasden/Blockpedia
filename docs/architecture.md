# Blockpedia MVP 总体架构

本架构服从 [`../AGENTS.md`](../AGENTS.md) 和 [`decisions.md`](decisions.md)，阶段执行见 [`roadmap.md`](roadmap.md)，产品边界见 [`product-scope.md`](product-scope.md)。精确数据字段形状唯一由 `schemas/{exporter,workspace,provider,mcp}` 下的 26 个 Schema 文件拥有；本文件只描述拓扑、职责和边界，示例不构成重复的完整字段规范。移入的 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 是历史背景和最低优先级原始参考；本文件明确收缩后的可执行实现边界，冲突内容禁止实现。

## 1. 固定运行拓扑

```text
Minecraft Java 26.2 + Fabric exporter
         │  本地导出目录
         ▼
Python Index Studio
  ├─ block-index web  ── FastAPI + Jinja2 + HTMX
  │                       ├─ SQLite workspace
  │                       ├─ 本地 previews/exports/workspace 文件
  │                       └─ 进程内持久化 Worker
  └─ block-index mcp  ── stdio，只读 release
                               │
                               └─ 四个 MCP tools

OpenAIProvider（Studio 新写任务使用唯一 active profile 的显式 adapter 和 configured/requested model_id；MCP 使用 resolved release snapshot，网络调用只用于受控语义/查询任务）
```

不部署 Redis、Celery、Kafka、消息队列、对象存储、向量数据库、独立搜索服务或其他常驻服务。WebUI 和 MCP 读取同一数据根目录，但 MCP 不读取可变 workspace 数据作为查询源，只读取经过门禁的不可变 release。MCP 不写数据库、文件、cache、logs 或 current。

正式平台为 Windows 11 x86_64 和 Linux x86_64（Linux `manylinux_2_17` / glibc `>=2.17`）；Python 基线为 CPython `3.14.7`。路径必须使用跨平台路径 API；不能依赖 Unix-only shell、Windows-only 绝对路径或未锁定系统组件。

## 2. 技术基线与锁定证据

| 组件 | 冻结版本/边界 |
|---|---|
| Minecraft | Java Edition `26.2` |
| Java | `25` |
| Fabric Loader | `0.19.3` |
| Fabric API | `0.157.0+26.2` |
| Loom | `1.17.19` |
| Gradle | `9.5.1` |
| mappings | Minecraft 26.2 native Mojang names/unobfuscated; no external mappings artifact |
| Web backend | Python + FastAPI |
| HTML/交互 | Jinja2 + HTMX，少量原生 JavaScript |
| storage | SQLite + 本地文件 |
| worker | Python 进程内有限 Worker，任务状态持久化到 SQLite |
| LLM | protocol-neutral `OpenAIProvider`；现有 profile `adapter` 显式取 `openai_responses` 或 `openai_chat_completions`，分别使用 `POST /responses`+`store=false` 或 `POST /chat/completions` 且省略 `store`；两者均为图片输入、strict JSON Schema、同一 configured/requested `model_id`/重试预算、稳定错误分类和本地校验；response model echo 不证明远端实际身份 |
| MCP transport | stdio |

R0 退出前只锁定 R0 tooling 实际引入的 Python 依赖；后续依赖在使用前必须精确/hash 锁定，Windows 在对应阶段验证，Linux 安装/运行、wheel/ABI 和最终双平台复现统一在 R5 验证，不预锁未实现的 R2-R4 栈。架构复现证据必须明确指向：

- `gradle/wrapper/gradle-wrapper.properties`、wrapper JAR checksum、Gradle dependency locking 和 `gradle/verification-metadata.xml`；
- R0 tooling 的 Python lock 输入、精确版本和 hashes，以及生成锁文件的输入/命令；不预锁未实现的 R2-R4 栈；
- 对应阶段 Windows 11 的 offline install/build 精确命令、完整 stdout/stderr、退出码、环境/锁 hash 和报告路径；R5 另行提供 Linux 与最终双平台证据。

上述 R0 路径、锁和 Windows offline skeleton build 已存在，足以关闭 R0。Windows 的真实 Minecraft runtime/export 在 R1、Windows Python 产品运行在 R2、Windows MCP/功能门在 R4 验证；Linux CPython/Web、Linux MCP、Linux Java 25/runtime/exporter、Linux wheel/ABI 和最终双平台端到端复现在 R5 验证，不倒灌为 R0-R4 blocker。

## 3. 组件职责和命令边界

### 3.1 Fabric exporter：唯一运行时选择/渲染者

Fabric exporter 只在受控 Minecraft 客户端内执行以下冻结阶段：

```text
EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS
```

它是注册表枚举、代表状态选择和 Minecraft 内渲染的唯一执行者：

1. 枚举 `minecraft` 命名空间全部方块和合法 BlockState；
2. 导出名称、属性、行为、几何和标准渲染事实；
3. 为每个 block 选择唯一 runtime default BlockState 作为代表，使用固定 isolated context，不做组合邻接或图像去重；完整合法状态仍全部导出；`variant_id` 等于 `block_id`，render 路径由 block ID 直接推导；
4. 在 Minecraft 内渲染并写出 JSONL、PNG、蒙版、失败记录和环境清单；无法普通 model 渲染时保留 Block/State，写机器 skip 并保持 pending review。旧导出身份和路径不迁移，按当前契约重导。

它不能调用 LLM、写 SQLite、提供 WebUI 或 MCP。游戏内导出命令仍为 `/blockindex export`；这不是 Python CLI。

### 3.2 Python Index Studio：只导入验证和构建

Python Studio **MUST NOT** 重新选择 variants，也 **MUST NOT** 重新渲染。它只导入并验证 exporter 已产生的 variants/renders，提取离线特征，执行 AI/审核并构建 release。Studio 阶段冻结为：

```text
PREPARE
  → IMPORT_EXPORT
  → VALIDATE_REGISTRY
  → VALIDATE_VARIANTS
  → VALIDATE_RENDERS
  → EXTRACT_FEATURES
  → AI_ANNOTATE
  → VALIDATE
  → HUMAN_REVIEW
  → BUILD_RELEASE
  → ACTIVATE_RELEASE
```

`PREPARE`/import 只接受 exporter 的非 staging 导出产物；`VALIDATE_VARIANTS` 和 `VALIDATE_RENDERS` 只检查选择与渲染结果，不重新执行选择/渲染；`EXTRACT_FEATURES` 是确定性离线提取；后续阶段负责语义、审核、candidate-build 和 activation。

### 3.3 Python CLI

Python CLI 只接受：

```text
block-index web [--data-root <path>]
block-index mcp [--data-root <path>]
```

`block-index web` 启动 loopback WebUI，固定监听 `127.0.0.1:8765`。不得提供 `--host`、`--port` 或环境变量 host/port 覆盖；`--data-root` 和日志等级等非冻结项可以覆盖。导入导出包、建立任务、暂停/恢复、人工审核、构建、发布、切换当前版本和回滚都必须通过 WebUI。WebUI 的渲染修复操作必须命名为 `request_reexport` 或 `request_exporter_rerender`，不得暗示 Python rerender。`block-index mcp` 启动 stdio MCP；它不得执行上述写操作。

### 3.4 WebUI 与 Worker

WebUI 页面最少包含：版本/项目选择、导入完整性检查、流水线进度、失败与审核队列、AI provider profile 配置、release candidate 完整性检查、发布/回滚选择和 MCP 等价搜索测试台。它只绑定 loopback，不做账号、CORS 或 CSRF。日志可由 WebUI/Worker 写入 data-root `logs/` 或 stderr；MCP 例外是只能写 stderr，不得写本地日志文件。

启动时只扫描并展示 stale `running` 任务，不写回状态；只有 WebUI 的 `recover` 操作才改变任务状态。成功任务不重跑。

## 4. 数据根目录和 release

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

不得使用高层 `work/` 或 `published/`。`workspace/{minecraft_version}/{run_id}/` 是可变工作范围；`cache/` 可保存经规则允许的本地缓存；`logs/` 只由本地 WebUI/Worker 使用，MCP 不写；release 目录生成后不可变。

唯一 release layout 高层引用为：

```text
<data-root>/releases/{minecraft_version}/{release_id}/
├── release.json
├── manifest.json
├── index.sqlite3
├── previews/
├── quality_report.json
├── manual-overrides.json
├── schemas.sha256
└── checksums.sha256
```

不得使用 `release.sha256`、YAML override 或 `contact-sheets/` 作为 release 契约名称。JSON、Schema 字段、metadata、manifest、current 和 release 中的 hash 字符串统一表示为 `sha256:<64 lowercase hex>`；唯一文本例外是 `checksums.sha256` 与 `schemas.sha256` 的行首 digest，均不带前缀。`checksums.sha256` 排除自身，格式为 `<64hex><two spaces><release-relative-posix-path>\n`，按路径排序；manifest 只哈希功能输入/产物且不自引用，`release.json` 可以保存 manifest hash。`schemas.sha256` 是 Schema inventory，格式为 `<64hex><two spaces><schema-id><two spaces><canonical-repository-relative-posix-path>\n`，按 schema-id UTF-8 字节序排序，不声称这些路径位于 release；细节由 pipeline 文档定义。

### 4.1 `current-pointer.v1`

唯一根 `current.json` 使用严格 `current-pointer.v1`；顶层字段冻结为 `schema_version`、`versions` map、`default_minecraft_version` 和 WebUI 激活/回滚时更新的 `updated_at`：

```json
{
  "schema_version": "current-pointer.v1",
  "default_minecraft_version": "26.2",
  "updated_at": "2026-08-13T12:30:00Z",
  "versions": {
    "26.2": {
      "release_id": "rel_01J...",
      "minecraft_version": "26.2",
      "relative_path": "releases/26.2/rel_01J...",
      "manifest_sha256": "sha256:<64 lowercase hex>"
    }
  }
}
```

`versions` 每个键是精确版本，值指向该版本 current release。MCP 省略 `minecraft_version` 时使用 default 版本对应 current；显式版本时只使用该版本 current；未知、未发布或没有指针的精确版本失败且不回退。MCP 不支持显式历史 `release_id` selector；历史切换只能由 WebUI rollback 完成。首次激活首个 Minecraft 版本时 `set_as_default=true` 强制，后续 apply 必须显式提供 `set_as_default`；激活时间只写 workspace activation audit 和 current `updated_at`，不得写回不可变 release；release 使用 `built_at`，不使用 `published_at`。只有 WebUI publish/rollback 能原子更新 current。

## 5. 数据分层和不变量

### 5.1 机器事实层

由 Minecraft runtime、Fabric exporter 和 Studio 的确定性验证/特征提取写入并在 WebUI 中只读：

```text
block_id
translation_key
name_zh
name_en
default_state
properties_json
legal_states
geometry_json
behavior_json
render_reference（preview/mask/render metadata 的路径和 SHA-256）
state_signature
feature_extractor_version
```

Python 只能验证和提取离线特征，不能重新选择代表状态或重渲染。机器事实必须能追溯到导出包和运行时清单。`block_id` 必须属于 `minecraft` 命名空间且在一个版本内唯一；状态字符串必须属于该方块导出的合法状态集合。

### 5.2 AI 语义层

Studio 新任务、配置管理和构建新 release 使用唯一 active profile 所选 adapter 的同一 configured/requested `model_id` 覆盖离线标注、QuerySpec 和 visual rerank 三阶段，只允许写入：

```text
synonyms_zh
synonyms_en
summary
color_terms
shape_terms
material_impression
building_roles
style_tags
avoid_for
confidence
model_id
prompt_version
source=llm
verified=false|true
```

返回必须通过本地 strict Schema、编号完整性、受控枚举、长度、重复和机器冲突检查。AI 失败或冲突只改变任务/审核状态，不能改变机器事实、`block_id`、合法状态、几何、发布状态或 `candidate_qualification`。

### 5.3 人工覆盖层

人工覆盖单独保存 `variant_id`、字段差异、操作者、时间、原因和来源版本。重建索引时按“机器事实 → 有效 AI 结果 → 人工覆盖”的固定顺序重放。人工可以编辑语义、设置 `eligible`/`conditional`/`excluded` 覆盖或建立跳过，但不能修改机器事实；跳过必须有 `skip_reason_code`、审核者、时间、备注和证据路径。

### 5.4 候选资格不变量

`candidate_qualification` 只能是 `eligible`、`conditional` 或 `excluded`。`conditional` 必须有非空警告列表；`excluded` 不进入默认搜索候选；所有资格覆盖必须可审计。一个方块即使为 `excluded` 或跳过，仍必须保留 Block 注册表记录。

### 5.5 Schema ID 命名空间

Schema ID 必须按边界分开，不能让一个命名空间的版本号伪装另一个边界的契约：

```text
exporter export schemas
workspace/release schemas
provider envelope schemas
MCP schemas
```

当前 ID 列表冻结为：exporter `export-manifest.v1`、`export-block.v1`、`export-state.v1`、`export-variant.v1`、`export-failure.v1`、`render-metadata.v1`；workspace/release `block-record.v1`、`state-record.v1`、`visual-variant-record.v1`、`annotation-record.v1`、`manual-override.v1`、`skip-review.v1`、`qualification-review.v1`、`release-manifest.v1`、`release.v1`、`current-pointer.v1`；provider `provider-batch-envelope.v1`、`annotation-batch-output.v1`、`annotation-wire-item.v1`、`query-spec-output.v1`、`rerank-output.v1`；MCP `mcp-index-info-output.v1`、`mcp-search-blocks-output.v1`、`mcp-block-details-output.v1`、`mcp-compare-blocks-output.v1`，可共享 `mcp-error.v1`。旧 ID `block.v1`、`state.v1`、`variant.v1`、`annotation.v1`、`annotation-item.v1`、`query-spec.v1`、`visual-rerank.v1`、`manifest.v1`、`override.v1` 不得作为新规范当前 ID。R0 必须把 Markdown 中的 Schema 契约物化为真实 JSON Schema 文件，使用 Draft 2020-12、strict `additionalProperties: false` 和可复核验收；在文件、哈希和验收证据存在前不得勾选完成。provider wire Schema ID 与各协议 structured-output format name 分开，固定 name 为 `annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`。

## 6. SQLite 和任务模型

MVP 使用一份在 R0 冻结的 SQLite schema，不使用 Alembic 或其他通用 migration framework。结构变化必须更新契约、重建数据库并重新执行完整性检查；不在运行时执行隐式迁移。run 与 stage 的持久状态只能是 `pending|running|paused|needs_review|failed|succeeded|cancelled`，item 可额外为 `skipped`；`draft`、`ready`、`pause_requested`、`cancel_requested` 只能作为命令或事件，不得持久化。

核心表和关键约束如下：

| 表 | 关键字段/约束 |
|---|---|
| `blocks` | `block_id` 主键；版本隔离；机器名称、默认状态、属性和行为均为 Schema 数据 |
| `visual_variants` | `variant_id` 主键；`block_id` 外键；机器 JSON、AI JSON、人工覆盖和资格分离 |
| `review_tasks` | `id` 主键；目标 ID、原因、严重度、状态、解决记录和证据 |
| `jobs` | `id` 主键；类型、目标、状态、attempt、优先级、心跳、错误；不含 Token usage |
| `provider_profiles` | profile ID、现有 `adapter`（`openai_responses` 或 `openai_chat_completions`）、对应协议 base URL/model ID、非秘密设置、`secret_reference`；不存明文 Key；最多一个 active |

SQLite 文件和图片路径必须位于选定版本的 `workspace` 或不可变 `releases` 目录。发布后的 MCP 连接以只读方式打开 release 数据库；不能连接可变 workspace 库。

任务状态为 `pending`、`running`、`succeeded`、`needs_review`、`skipped`、`failed`。应用启动只检测并展示超过心跳期限的 `running` 任务；WebUI `recover` 才能将未完成任务置回可恢复状态；成功任务绝不再次执行。每个请求/任务的自动重试总次数最多为一次。

FTS5 优先使用可用的 `trigram` tokenizer；若目标 SQLite 不支持，则使用规范化字符串 `LIKE` 和显式标签查询。两者都不能增加向量列或外部服务。

## 7. 流水线门与职责分离

### 7.0 R3 Phase C 边界引用

Phase C 的架构边界只有：WebUI 同步执行 `POST /api/releases/check` 和 `POST /api/releases/build`；check/build 的 cache、逻辑 snapshot fingerprint、staging、独立 `release-index.v1.sql`、hash DAG 和文件安全由 [`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md) 定义；12 项质量报告由 [`quality-and-testing.md`](quality-and-testing.md) 定义；原始人工记录包由 [`data-and-schemas.md`](data-and-schemas.md) 定义；文件 durability 和链接拒绝由 [`security-and-distribution.md`](security-and-distribution.md) 定义；路由 body/status/error 由 [`webui-and-operations.md`](webui-and-operations.md) 定义。本架构文档不复制这些字段、SQL 列或报告 item。

Phase C 只允许生成一个不可变 candidate，并在成功后留下 `R3_CANDIDATE_BUILT_ACTIVATION_PENDING` 边界。Phase C **MUST NOT** 实现或要求 activation、`current.json`、MCP 或第二个 release；这些仅是后续阶段的边界引用，不是本阶段拓扑、依赖或验收项。

```text
Fabric exporter:
  EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS

Python Studio:
  PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS
  → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE
  → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE
```

Fabric exporter 是唯一做注册表枚举、代表状态选择和 Minecraft 内渲染的组件。Studio 的验证阶段只能验证既有 exporter 产物；Studio 不可调用 Python 重选或重渲染。

### 7.1 candidate-build gate

candidate-build gate 检查内容完整性：

1. 运行时 `minecraft` 注册表与发布 Block 集合覆盖率为 100%；
2. 每个 Block 至少有一个有效变体，或有完整审计跳过；
3. 所有发布图片可读取且路径只指向当前 candidate；
4. 所有状态字符串属于导出的合法状态；
5. 机器事实、AI 结果和人工覆盖均通过冻结 Schema；
6. 人工覆盖和跳过记录指向有效目标；
7. 高优先级审核任务为零；
8. FTS/规范化搜索索引构建成功。

此门不检查 MCP smoke、`TWO_INDEPENDENT_RELEASES` 或 current 切换。R3 可以生成至少一个不可变、未激活 candidate 供 R4 测试。

### 7.2 临时 R4 测试和 activation gate

R4 必须把通过 candidate-build gate 的 candidate 放入临时测试 data-root，并生成临时 `current.json` fixture 做 MCP 四工具 smoke；不得激活生产 current，MCP 测试不得写任何持久化状态。

R5 先构建至少两个独立通过 candidate-build gate 的 release，再执行 activation gate：

1. MCP 四工具 smoke 通过；
2. 至少两个独立 release 内容完整、不可变、哈希可复算；
3. `current.json` 临时文件、flush/fsync 和原子替换验证通过；
4. default/显式版本解析和 rollback 只切指针验证通过。

activation gate 通过后仍需用户在 WebUI 中人工激活；activation gate 不是单个 candidate-build gate 的内容检查。

## 8. OpenAI 协议与隐私边界

provider 层使用 protocol-neutral `OpenAIProvider`，其现有 profile `adapter` 字段只能显式选择 `openai_responses` 或 `openai_chat_completions`，每个值对应一个 wire adapter/codec。它读取 Keyring 中服务名 `blockpedia`、账户为 profile ID 的秘密，或读取 `OPENAI_API_KEY`；数据库和 release 只存 `secret_reference`。用户批准的 `base_url` 属于所选协议 adapter，不是协议 fallback；不得实现 Anthropic/其他 provider adapter 或 model voting。

所有请求必须：

- 发送最小必要文本、短编号、必要机器元数据和本地预览图片；
- Responses 请求设置并实际发送 `store=false`；Chat Completions 请求省略 `store`；
- 使用 strict JSON Schema；
- 记录 requested model/prompt/schema 版本用于复现，但不记录 Token usage、费用或预算；成功 response 的 model echo 不作为已验证远端身份，也不替换 requested model；
- 出错后最多再请求一次，仍失败则转 `needs_review`/`failed`；
- 不将 API Key、本地绝对路径、原始导出包或无关索引内容发送给 provider。

能力探测必须按所选协议验证 endpoint、图片输入、strict Schema structured output、稳定错误分类和成功 response 的 string `model` structural validity；model echo 不再是 equality 验证条件或 enable gate，缺失/非 string 仍 fail closed。任何协议都不能证明远端 retention 或第三方实际执行的 requested model，文档、probe 和实现不得声称 storage/remote model identity 已验证，第三方服务 trust/policy 由用户负责；warning 或用户 ack 不得绕过显式 adapter 选择。

允许保存多个非活动 profile，但全局最多一个 active profile 的约束只适用于 Studio 新任务、配置管理和构建新 release。每个 release 冻结离线标注时的完整非秘密 provider snapshot，包括现有 `adapter`、`profile_id`、requested `model_id`、`base_url_stable_id`、不可逆 `secret_reference` 及相关版本；MCP 不读可变 active 状态或 workspace 数据库，只能按 resolved release snapshot 使用同一 requested `model_id` 和所选协议执行 QuerySpec 与 rerank。secret 无法解析或能力不再通过时，MCP 本地降级并返回 warning。release-bound snapshot 不算第二个 active profile，也不能用于 Studio 新写任务。未来实现才把 adapter 纳入 envelope、cache、signature 和 release lineage，并按协议使用 conditional `store`；现有 openai_responses release 保持有效且 immutable，变更前 in-flight cache/workspace invalidated/rerun。

## 9. MCP 只读契约

### 9.1 传输和 release 解析

`block-index mcp` 仅使用 stdio。启动后 stdout 第一字节起只能出现 MCP 协议消息；日志、诊断、异常详情写 stderr，不写日志文件。MCP 读取根 `current.json`，校验 pointer schema、default 版本、指定版本（如有）、manifest 哈希、release checksums 和质量门状态，再以只读方式打开 `releases/{minecraft_version}/{release_id}`。不读取 workspace、exports 或 cache。

省略 `minecraft_version` 时使用 `default_minecraft_version` 对应 current；显式版本时使用对应版本 current；未知、未发布或 hash 不匹配失败且不回退。MCP 不接受显式历史 `release_id` selector；历史 release 只能由 WebUI rollback 改变 current 指向。MCP 不写数据库、文件、cache、logs、release 或 current。

### 9.2 四个工具

只注册下列四个工具：

| 工具 | 最小输入 | 行为 |
|---|---|---|
| `index_info` | 可选 `minecraft_version` | 返回 default/指定版本的当前 release 信息和完整性摘要 |
| `search_blocks` | `query`，可选 `minecraft_version`、`limit`/`context` | 先硬过滤，再做可解释本地召回；可用同一所选 OpenAI adapter/model 做 QuerySpec/重排 |
| `get_block_details` | `block_id`，可选 `minecraft_version` | 返回该版本的状态、变体、机器事实、语义、资格、警告和图片 |
| `compare_blocks` | 2–6 个 `block_ids`，可选 `minecraft_version`/`context` | 只从 release 读取指定方块，返回结构化差异和对比图片 |

输入省略版本只影响 current 解析，不构成“最新兼容版”回退。`block_id` 必须经过 `minecraft:` 命名空间和 release 查找校验，不能拼接 SQL 或文件路径。图片由 server 从当前 release 读取并作为 MCP 图片内容返回，不向客户端暴露可写本地路径。

## 10. 版本选择、发布和回滚流程

1. WebUI 创建项目、导入、任务、构建和发布时必须显式选择 `minecraft_version`，并检查导出 manifest 的版本等于选择值。
2. exporter 负责选择状态和渲染；Studio 只验证这些产物并提取特征，不跨版本复用未经哈希验证的图片或状态。
3. Studio 在临时目录构建 release layout，执行 candidate-build gate，写 manifest、`release.json`、`schemas.sha256` 和 `checksums.sha256`。
4. 通过 candidate-build gate 后将临时目录原子移动为新的不可变 release；不通过则保留失败报告但不写 current。
5. R3 可留下至少一个未激活 candidate，R4 用临时 data-root/current fixture 做 MCP 测试；不激活生产 current。
6. R5 先生成两个独立 candidate-build 通过的 release，再执行 activation gate；通过后由用户在 WebUI 人工激活/切换 current。
7. current 切换只原子替换根 `current.json` 的目标版本指针，保留其他版本和 default；rollback 只指向已有完整 release，不修改 release 内容。
8. MCP 下一次请求重新读取并验证指针；在切换前已经打开的只读连接继续指向旧 release，不能读到半成品。

## 11. 复现、安全和公开白名单

- 构建必须使用冻结的 Java/Gradle/Fabric 基线和已使用依赖的精确/hash lock；R0 tooling 锁证据必须包含输入、版本、hash 和对应阶段 Windows offline install/build 报告，Linux wheel/ABI、安装/运行和最终双平台报告统一由 R5 提供；后续依赖在使用前更新 lock 并按阶段验证。
- 导出 manifest 必须记录版本、loader、API、mappings、资源包、语言、渲染设置和 exporter/schema 版本。
- 真实原版资产、导出包、预览、release、非空数据库、生成 PNG 和 Key 只能在本地数据目录；公开仓库只放源码、文档、真实 JSON Schema、空数据库和 fixture 生成器源码。
- WebUI 不做账号、CORS、CSRF，安全边界是 `127.0.0.1:8765` 和本机访问控制；绝不能提供 host/port 远程绑定选项。
- MCP 进程不得写任何本地状态；stdout 纯净、stderr 可诊断、不写文件，四工具集合固定。
- 所有复选框必须有路径、命令、哈希或报告证据；没有实现时不填写伪造报告。
