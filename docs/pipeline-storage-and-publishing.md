# 流水线、存储与发布设计

## 1. 范围、规范词和关联文档

本文定义从导出包到不可变 release 的本地流水线、任务状态、恢复、存储边界、发布切换和验收门禁。Fabric exporter 和 Python Studio 的阶段顺序分离且固定：

```text
Fabric exporter:
  EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS

Python Studio:
  PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS
  → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE
  → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE
```

`MUST`、`MUST NOT`、`SHOULD`、`MAY` 按 RFC 2119 解释。默认架构可以在记录影响、回归范围和所有者批准后做等价替换；没有批准不得改变 SQLite、本地图片、进程内 Worker 或 MCP 只读语义。

关联文档：

- [导出契约](export-contract.md)
- [状态策略与渲染](state-policy-and-rendering.md)
- [数据与 Schema](data-and-schemas.md)
- [路线图](roadmap.md)
- [冻结决策](decisions.md)
- [OpenAI Responses 提供商接口](openai-provider.md)
- [WebUI 与运行接口](webui-and-operations.md)
- [MCP API 接口](mcp-api.md)
- [质量与测试接口](quality-and-testing.md)
- [安全与分发接口](security-and-distribution.md)

## 2. 默认运行架构和写入边界

MVP 默认使用一个本地 Python 应用、SQLite 工作库、本地图片文件和进程内有限 Worker。WebUI 和 Worker 是工作库唯一写入者；MCP **MUST NOT** 读取或写入工作库，只能读取已通过门禁的不可变 release。MCP 查询失败不能触发工作库写入、重新渲染或 AI 调用。

默认组件职责：

```text
Fabric client exporter → 唯一执行注册表枚举、代表状态选择和 Minecraft 渲染
WebUI                 → 创建运行、审核、显式 BUILD_RELEASE/ACTIVATE_RELEASE、清理/回滚
Worker                → 执行 Studio 阶段、写任务状态和工作产物
SQLite workspace      → 可变任务/未完成/审核索引
releases/              → 不可变发布 SQLite、图片和 manifest
MCP                   → 只读根 current.json 与指定版本 release
```

默认并发在一个 Python 进程内启动有限 Worker；不引入 Redis、Celery、Kafka、微服务或云对象存储。未来等价替换必须保留幂等键、游标、原子 release 和 MCP 只读边界。

## 3. 本地数据根和目录

应用数据根默认位于源码目录之外，由用户配置或操作系统应用数据目录解析为绝对路径。例如：

```text
<data_root>/
├── exports/{minecraft_version}/{export_id}/  # 原始导出包，只读输入
├── workspace/{minecraft_version}/{run_id}/   # 可变工作库和产物
│   ├── work.sqlite3
│   ├── generated/
│   └── overrides/
├── cache/
│   ├── renders/
│   ├── features/
│   └── ai/
├── releases/
│   └── 26.2/
│       └── <release_id>/
├── current.json
└── logs/
    └── <project_id>/
```

这是唯一 data-root 布局；不得使用旧版工作目录或旧版发布目录作为高层目录名。不同精确 Minecraft 版本的 exports、workspace 和 releases 必须分目录，禁止跨版本复用状态、图片或数据库行。`<data_root>` 不得默认指向源码仓库；程序必须拒绝把原版 JAR、资源包、纹理、模型、字体、声音复制进源码或公共发布目录。

所有图片来自用户本地合法安装和 exporter 渲染。导出包、cache、workspace 和 release 可以保存生成的 PNG、对象蒙版和特征，但只能保存机器元数据与渲染产物的哈希，不保存原始资源。测试 fixture 必须由程序生成原创图/伪数据；真实集成测试依赖本地导出，缺失时必须明确输出 `SKIPPED_LOCAL_EXPORT_MISSING`，不能静默改用真实资源或伪造通过。

## 4. Python/SDK 锁定和 `PREPARE`

Fabric/Gradle 工具链由导出契约固定为 Minecraft Java `26.2`、Java `25`、Fabric Loader `0.19.3`、Fabric API `0.157.0+26.2`、Loom `1.17.19`、Gradle `9.5.1`；Minecraft 26.2 使用 native Mojang names/unobfuscated，不解析外部 mappings artifact。R0 只锁定实际引入的 Python tooling 依赖；不预锁未实现的 R2-R4 栈。后续依赖在使用前必须精确/hash 锁定并在 Windows 11 x86_64 与 Linux x86_64 `manylinux_2_17` / glibc `>=2.17` 上重新验证。candidate check/build 的前置只要求 R0-R3 和 candidate-build gate；activation-check/apply 的前置才要求 R0-R4、activation gate 和用户确认。

`PREPARE` 在任何外部模型请求或写入成功产物前必须检查：

1. 导出包存在、路径在 `<data_root>/exports/` 或经用户明确授权的位置，且通过 [导出契约](export-contract.md) 校验。
2. manifest 的 MC/Fabric/Java/Loom/Gradle/mappings 精确值与当前项目 lock 一致；精确字段形状以真实 Schema 文件为准。
3. R0 tooling 的 Python lock 已存在，运行解释器和实际使用依赖哈希一致；否则报告 `TOOLCHAIN_NOT_LOCKED` 并停止。
4. exporter 已写入的策略版本和所有真实 JSON Schema 已加载并校验；Studio 只校验 exporter 已写入的策略版本，不执行状态选择或渲染。
5. 资源包只有 vanilla 标识/哈希，没有原始资源副本；导出 scope 是 `minecraft` block registry。
6. 计算 `run_id`、`input_signature`、工作库 Schema 版本和每阶段幂等键。
7. 同一输入签名已有成功阶段产物时，验证哈希后复用，不重新执行；不存在 `store=false` 能力证明时停止，不允许 warning/ack 继续 provider 任务。

PREPARE 输出可恢复的 run spec、导出 manifest 快照、版本锁快照和阶段游标；失败时不创建可发布 release。

## 5. 阶段输入、输出和完成条件

### 5.1 `IMPORT_EXPORT`

Python 导入并验证 exporter 的 `export-manifest.v1`、`export-block.v1`、`export-state.v1`、`export-variant.v1`、`export-failure.v1`、`render-metadata.v1` 和校验文件。Studio 只复制/引用导出结果到 `workspace/{minecraft_version}/{run_id}`，不重新枚举注册表、选择代表状态或渲染图片。

输出：工作库中的只读机器事实投影、原始导出引用和导入完整性报告。

### 5.2 `VALIDATE_REGISTRY`

比较 manifest 的 `registry_snapshot_sha256` 与排序后的 block ID，确认 100% 覆盖；检查 Block 记录唯一、所有 block ID 属于 `minecraft`、版本一致且不存在虚假 ID。缺记录、重复主键或校验不一致使阶段 `failed`，不得补造或跳过方块。

### 5.3 `VALIDATE_VARIANTS`

验证 exporter 已产生的 canonical state、默认状态、代表状态、状态集合、资格初始值、warnings 和状态映射。验证方向折叠、显著布尔/几何/阶段/邻接规则及同 block 内 dedupe 结果，但不得重新运行选择算法。失败必须绑定导出记录或审核任务。

### 5.4 `VALIDATE_RENDERS`

验证 exporter 已生成的固定 512×512 摄影棚四视角 PNG、蒙版和 `render-metadata.v1`。Studio 不得重新渲染、裁剪、替换或补造图片；缺图、不可读、hash 不符或环境元数据缺失即失败/进入审核。

输出：只读图片资产、渲染元数据、图像哈希和失败/审核队列。

### 5.5 `EXTRACT_FEATURES`

由确定性 Python 脚本完成，不调用 LLM。基于对象蒙版和导出几何计算 Oklab/Lab 颜色摘要、亮度/饱和度、透明像素比例、几何类别、边缘密度、纹理方向性、动画/发光指标。特征记录必须携带 `feature_extractor_version` 和输入图片/机器元数据哈希；重复输入直接复用。

输出：可解释的颜色、几何、视觉特征和 deterministic machine tags。提取器不得新增 block ID、状态或事实。

### 5.6 `AI_ANNOTATE`

只把合格的可搜索变体、预览图和紧凑机器元数据发给唯一已配置的 OpenAI Responses 模型。导出器不调用 LLM，调用由 Worker 的 provider adapter 负责；请求必须 `store=false`、strict JSON Schema 和最小披露，规则见 [OpenAI Responses 提供商接口](openai-provider.md)。兼容 `base_url` 仍是用户批准的同一 `OpenAIResponsesProvider`，不是第二 provider；其完整 Responses 能力和 `store=false` 必须通过探测，否则 profile 禁用，不能 warning/ack 降级。离线标注批次建议 8～16 个变体；每个逻辑请求使用独立且全局唯一的 `provider-batch-envelope.v1`，并以 `stage` 绑定 wire Schema。`offline_annotation` envelope 必须包含 tile-to-variant 映射；`query_spec` 是不带候选映射的单查询 stage，使用 `query-spec-output.v1`；`visual_rerank` 只携带本地已召回候选映射，使用 `rerank-output.v1`，三者不得混用输出 Schema。

缓存键至少为（`schema_version` 随 stage 绑定）：

```text
image_hash
+ machine_metadata_hash
+ prompt_version
+ model_id
+ schema_version
+ base_url_stable_id
+ stage
```

已有通过 Schema 且输入完全相同的结果不得再次请求。MVP **MUST NOT** 记录或展示 Token usage、费用、预算或价格字段；请求审计只保存 `provider-batch-envelope.v1` 的非秘密引用、stage、wire Schema、输入摘要、request ID（如契约允许）和结果哈希，不保存 Token 数值。模型只能生成语义字段；Schema 冲突或一次修复失败进入高优先级审核。置信度门控固定为 `>=0.80` 自动通过、`0.65–<0.80` 普通审核、`<0.65` 高优先级；Schema 冲突/修复失败始终高优先级。

### 5.7 `VALIDATE`

按以下顺序执行：Schema → 版本/来源 → 引用完整性 → 机器事实不可覆盖 → 状态合法性 → 图片可读性 → 变体/状态覆盖 → AI/人工语义门控 → SQLite FTS 构建预检。任何失败都必须绑定到逻辑项和错误码，不能用人工文本掩盖机器失败。

输出：不可发布缺陷清单、审核任务、可发布候选计数和质量报告草稿。

### 5.8 `HUMAN_REVIEW`

只处理渲染异常、机器与 AI 冲突、低置信度/Schema 高优先级项、经配置的抽样质检以及特殊状态覆盖。审核员可以接受/编辑受控语义、声明跳过、重新请求 AI 或要求 exporter 按策略重新导出，但不能在 Studio 内重选状态或重渲染。人工语义修改写 `manual-override.v1`，资格修改写 `qualification-review.v1`，跳过写 `skip-review.v1`；默认按 `variant_id`，family/global 必须显式 scope 和批准。

阶段只有在所有高优先级任务为零、所有 skip/excluded 审计已审核、所有可搜索变体有合规 AI 或等价人工语义后才是 `succeeded`。存在待处理任务时为 `needs_review`，不能进入 `BUILD_RELEASE`。

### 5.9 `BUILD_RELEASE`

Worker 在用户 WebUI 显式请求后，只能从工作库构建单个 release candidate。candidate-build gate 只检查该 release 的内容完整性：100% registry、合法状态、skip/excluded 审计、图片、全部 Schema、AI/人工语义、高优审核为零、FTS、功能输入/产物 hash、禁止 symlink/hardlink 和完整 release layout。它不检查 MCP smoke、两个 release 或 current 切换。candidate check/build 的前置只要求 R0-R3 与 candidate-build gate；通过后复制文件到 staging，逐文件 hash、flush/fsync，再原子 rename 为 `<data_root>/releases/{minecraft_version}/{release_id}` 并冻结；首次 release 可以构建但不能激活。`release_build_id` 在 release check 根据 `run_id` 创建并返回，不是 `POST /api/runs` 的前置输入。

唯一 release layout（`schemas.sha256` 必须列出实际使用的真实 Schema 文件摘要，`checksums.sha256` 再覆盖 release 内其它普通文件）：

```text
<data_root>/releases/{minecraft_version}/{release_id}/
├── release.json
├── manifest.json
├── index.sqlite3
├── previews/
├── quality_report.json
├── manual-overrides.json
├── schemas.sha256
└── checksums.sha256
```

禁止旧版发布目录、YAML override、旧版 release checksum 文件、contact sheet 目录契约名和任何 symlink/hardlink。JSON、Schema 字段、metadata、manifest、current 和 release 中的 hash 字符串采用 `sha256:<64 lowercase hex>`；`checksums.sha256` 与 `schemas.sha256` 文本行首 digest 是唯一无前缀例外。`checksums.sha256` 每行格式为 `<64hex><two spaces><release-relative-posix-path>\n`，按路径排序；`schemas.sha256` 每行格式为 `<64hex><two spaces><schema-id><two spaces><canonical-repository-relative-posix-path>\n`，按 schema-id UTF-8 字节序排序，且不声称这些路径位于 release。

### 5.10 `ACTIVATE_RELEASE`

activation gate 才检查目标版本已有至少两个独立、均通过 candidate-build gate 的不可变 release；activation-check/apply 的前置才要求 R0-R4、activation gate 和用户确认。它使用临时测试 data-root/current fixture 完成四工具 MCP smoke，复核 candidate 报告及其 hash，并检查 current 原子切换准备，但不把测试指针写入生产根，也不首次补做资格审计。通过后由 WebUI 用户人工确认 ACTIVATE；WebUI 才能原子更新根 `current.json`。R3 可以提供未激活 candidate 给 R4，R5 完成第二个 candidate、MCP smoke 和激活。

## 6. 任务模型、状态和游标

### 6.1 run/stage/item 三套状态机

`run` 和 `stage` 的状态只能是：

```text
pending | running | paused | needs_review | failed | succeeded | cancelled
```

`item` 的状态只能是：

```text
pending | running | succeeded | needs_review | failed | skipped
```

合法转换如下：

```text
run/stage: pending → running → paused → running
run/stage: running → succeeded | needs_review | failed | cancelled
run/stage: needs_review → running（人工解决后产生新 input_signature）
run/stage: failed → pending（WebUI 显式新 attempt 或新 input_signature）

item: pending → running
item: running → succeeded | needs_review | failed | skipped
item: needs_review → pending（WebUI 显式处理）
item: failed → pending（WebUI 显式重试）
```

启动时只检测 stale `running` 并在内存/展示层标示，不修改数据库状态。只有 WebUI `recover` 操作可以把 stale item 写回 `pending` 或 `needs_review`；成功 item 不重跑。自动重试只影响 item 的 `auto_attempt`，每个逻辑 item 最多一次总自动重试。状态变更必须在 SQLite 事务中与产物引用、错误码、游标和审计记录一起提交。

### 6.2 最小任务字段

`jobs` 表至少包含：

```text
job_id, run_id, stage, logical_key, input_signature,
status, auto_attempt, priority, worker_id, heartbeat_at,
cursor_json, output_hash, error_code, error_message,
created_at, started_at, finished_at
```

`runs`、`stage_runs` 和 `jobs` 分别保存三套状态；AI 任务只保存 provider、model、prompt/schema 版本、request ID（如可用）、响应哈希、响应缓存键和审计状态，**MUST NOT** 保存 Token usage、费用、预算或价格字段。任务唯一约束是 `(run_id, stage, logical_key, input_signature)`；成功产物的 `output_hash` 必须可从文件/数据库重算。

### 6.3 游标、幂等和缓存

每阶段保存 `stage_cursor`：已枚举输入的排序键、已完成数量、当前 logical key、策略/输入哈希和最后心跳。Worker 领取任务使用事务锁；完成时先将临时产物 fsync/rename，再在同一事务写成功状态和 hash。进程崩溃时没有成功事务的产物视为未提交。启动检测 stale 只展示，不推进游标或写状态。

幂等键命中且输出校验通过时直接复用，绝不重复请求或覆盖成功产物。命中但 hash 不一致时报告 `IDEMPOTENCY_CONFLICT` 并停在 `needs_review`/`failed`，不能自选一个文件继续。

### 6.4 遗留 `running` 检测与 WebUI recover

应用启动或定时检查 heartbeat 超时的 `running` 任务，只生成内存诊断和 WebUI 展示标记 `stale=true`，并写 stderr/展示日志；不得自动修改 SQLite 的 run/stage/item 状态。用户调用 WebUI `recover` 后才执行：

1. 读取临时文件和外部请求记录；
2. 若完整输出和 hash 已存在，校验后在事务中补写 item `succeeded`，不重复执行；
3. 若没有完整输出且 `auto_attempt=0`，在事务中增加一次 `auto_attempt` 并置 item `pending`；
4. 若自动重试已用，置 item `needs_review` 或 `failed`，不再自动运行；
5. 写入 `WORKER_RECOVERED_STALE_RUNNING` 审计事件。

recover 不能删除成功产物，也不能把未知结果当作 AI 已返回或图片已成功；run/stage 只有在其 item 结果汇总后才由 WebUI/Worker 事务更新。

### 6.5 provider 配置冻结

`AI_ANNOTATE` 和在线 query lane 使用同一个已启用的 `OpenAIResponsesProvider`。release candidate 必须冻结以下非秘密 provider 引用和版本：`profile_id`、`model_id`、`base_url_stable_id`、`secret_reference`、`prompt_version`、各 wire/record Schema version、`search_ranking_version`。MCP 只能从 release metadata 读取这些值，并按 `secret_reference` 从 Keyring 或允许的环境变量读取秘密；MCP **MUST NOT** 读取 workspace 数据库、可变 provider profile 或缓存。compatible `base_url` 仍属于同一 Responses provider；能力探测和 `store=false` 未完全通过时不得冻结为可用配置。

## 7. 错误、恢复和重试语义

| 错误 | 默认状态 | 恢复动作 |
|---|---|---|
| `TOOLCHAIN_NOT_LOCKED` | `failed` | 完成 R0 锁定后新建 run |
| `EXPORT_CONTRACT_INVALID` | `failed` | 修复导出包/导出器，重新导出 |
| `REGISTRY_INCOMPLETE` | `failed` | 不允许跳过，重新导出完整注册表 |
| `POLICY_INVALID` | `failed` | 修正策略并产生新输入签名 |
| `RENDER_*` | `needs_review` | 自动最多重试一次，随后人工审核或声明 skip |
| `SCHEMA_INVALID` | `needs_review` 高优先级 | 只允许一次修复尝试，之后人工覆盖或失败 |
| `PROVIDER_RATE_LIMITED` | `needs_review` | 依契约人工/显式新 attempt，不重复成功任务 |
| `PROVIDER_AUTH_FAILED` | `failed` | 修复 secret reference 后恢复未成功任务 |
| `FTS_BUILD_FAILED` | `failed` | 修复数据库/Schema 后重建发布候选 |
| `MCP_SMOKE_FAILED` | `failed` | 不切换 current，修复 release 候选 |
| `IDEMPOTENCY_CONFLICT` | `needs_review` | 检查输入/资产，禁止覆盖 |
| `PUBLISH_ATOMIC_REPLACE_FAILED` | `failed` | 保留旧 current，修复后重试切换 |

每个外部模型请求最多一次总自动重试；人工重新构建必须以新输入签名审计。provider 超时、未知响应或进程崩溃不能默认视为成功，必须校验响应 Schema、缓存和哈希后决定。

## 8. SQLite 工作库与 release 视图

### 8.1 工作库写入

默认工作库至少有 `runs`、`stage_runs`、`jobs`、`blocks`、`states`、`variants`、`features`、`annotations`、`overrides`、`review_tasks`、`artifacts`、`provider_requests` 和 `logs` 逻辑表。WebUI/Worker 可写；MCP 进程不得打开工作库路径。SQLite 写入使用事务和 WAL/锁策略；图片不存 BLOB，只存规范化相对路径、大小和 hash。

工作库可变，允许保存 `pending`、`running`、`failed` 和经 Schema 校验的最小 provider artifact；`draft`、`ready`、`pause_requested`、`cancel_requested` 只能作为命令或事件，不得作为持久状态。**MUST NOT** 保存完整 provider response、原始 prompt 或图片内容。这些内容不自动出现在 release。provider 请求表不得含 Token、费用、预算或明文 API key；密钥只能通过 `secret_reference` 解析。

### 8.2 release 内容

每个 release 目录必须且只能使用以下冻结 layout（目录内允许 `previews/` 下的普通 PNG 文件）：

```text
<data_root>/releases/{minecraft_version}/{release_id}/
├── release.json
├── manifest.json
├── index.sqlite3
├── previews/
├── quality_report.json
├── manual-overrides.json
├── schemas.sha256
└── checksums.sha256
```

`index.sqlite3` 是为只读 MCP 构建的发布投影，包含 FTS 索引和仅 `eligible`/满足条件的 `conditional` 视觉候选；可以保留用于详情查看的完整 Block/状态事实，但不得把 skipped 或 `excluded` 项伪装成可搜索图片。release 不包含原版资源。MCP 所需 provider snapshot 也必须冻结在 release metadata 中：`profile_id`、`model_id`、`base_url_stable_id`、不可逆 `secret_reference`、prompt/Schema/search 版本；MCP 不读 workspace provider profile，也不把该 snapshot 视为新的 active profile。

`release.json` 使用 `release.v1`，至少记录；同目录 `manifest.json` 使用独立的 `release-manifest.v1`，只记录功能输入/产物和 Schema inventory 引用。精确字段形状由 `schemas/workspace/` 下的真实 Schema 文件拥有，以下仅为说明性示例。`release-manifest.v1` 的顶层 `schema_version` 必须为 `release-manifest.v1`，其功能哈希不得包含 `release.json`、`manifest.json`、`schemas.sha256` 或 `checksums.sha256`，避免自引用和循环；`release.json` 仅保存 `manifest_sha256`，不保存自身或 checksum 的摘要：

```json
{
  "schema_version": "release.v1",
  "release_id": "rel_01J...",
  "minecraft_version": "26.2",
  "built_at": "2026-08-13T12:00:00Z",
  "source_export_id": "exp_01J...",
  "provider_snapshot": {
    "profile_id": "default",
    "model_id": "configured-model-id",
    "base_url_stable_id": "https://api.openai.com/v1",
    "secret_reference": "keyring:blockpedia/default",
    "prompt_version": "prompt.v1",
    "request_envelope_schema_id": "provider-batch-envelope.v1",
    "wire_schema_ids": {
      "offline_annotation": "annotation-batch-output.v1",
      "query_spec": "query-spec-output.v1",
      "visual_rerank": "rerank-output.v1"
    },
    "search_ranking_version": "search-ranking.v1"
  },
  "manifest_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "record_schema_versions": {
    "block": "block-record.v1",
    "state": "state-record.v1",
    "variant": "visual-variant-record.v1",
    "annotation": "annotation-record.v1",
    "manual_override": "manual-override.v1",
    "skip_review": "skip-review.v1",
    "qualification_review": "qualification-review.v1"
  },
  "toolchain_lock_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "quality_report_path": "quality_report.json",
  "immutable": true
}
```

release 创建成功后目录内容和数据库权限/应用层均视为只读。所有普通文件必须复制到 staging 后逐文件计算 `sha256:<64 lowercase hex>`，不得使用 symlink/hardlink；完成 flush/fsync 后原子 rename 为 release ID，并由 `release.json` 的 `immutable: true` 记录不可变语义，不得额外写入契约外的 marker 文件。manifest 只记录功能输入/产物 hash，不记录自身、`release.json` 或 `checksums.sha256` hash；`release.json` 只记录 `manifest_sha256`，而 `checksums.sha256` 独立列出并校验 release 内其它普通文件。`schemas.sha256` 是按 schema-id UTF-8 字节序排序的 Schema inventory，每行严格为 `<64hex><two spaces><schema-id><two spaces><canonical-repository-relative-posix-path>\n`，路径必须是仓库规范相对 POSIX 路径且不声称位于 release；它和 `checksums.sha256` 都不被自身或 release metadata 反向哈希。任何修订都新建 `release_id`，即使只改变 Schema、semantic constraints、prompt、模型、图片或人工覆盖也不能更新旧 release。

`manifest.json` 示例至少包含：

```json
{
  "schema_version": "release-manifest.v1",
  "release_id": "rel_01J...",
  "minecraft_version": "26.2",
  "source_export_id": "exp_01J...",
  "source_export_manifest_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "toolchain_lock_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "schemas_inventory_path": "schemas.sha256",
  "functional_inputs": {},
  "functional_artifacts": {},
  "quality_report_path": "quality_report.json",
  "quality_report_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
}
```

## 9. 发布阻断门禁

`candidate-build gate` 必须逐项产生可审计结果；任一失败不得生成可激活 candidate：

1. **100% 登记**：运行时 `minecraft` registry snapshot 与 Block 集合完全相等，差集为空，重复和虚假 ID 均为 0。
2. **状态合法**：每个 `export-state.v1` 的 `state_id`、默认状态、属性名和值均通过 canonical serializer 和运行时合法集合；每个状态映射引用同 block 的真实变体或失败记录。
3. **资格初始值**：每个 `export-variant.v1` 的 `candidate_qualification` 只能为 `eligible`/`conditional`/`excluded`，`source` 必须为 `machine`；`conditional` 的 warnings 非空，AI 输出不得出现或改变资格字段。
4. **skip/excluded 审计**：每个 skipped/excluded target 必须逐字段存在 `target_id`、`minecraft_version`、`reviewer`、`reviewed_at`、`reason_code`、`note`、`evidence`、`source_version`；skip 另必须使用独立 `skip-review.v1` 并存在 `machine_failure_ref`，引用有效 `export-failure.v1`；excluded 另必须使用独立 `qualification-review.v1` 并存在 `qualification` 与 `warnings`。该 excluded qualification 完整性属于 candidate-build gate，activation gate 只复核其报告及 hash，不首次补做内容审计。缺任一字段即阻断。
5. **图片可读**：所有 `eligible`/合规 `conditional` variant 的 PNG 可解码、512×512、四视角、hash 一致；支撑/背板不污染特征区域；Studio 不生成替代图片。
6. **Schema 全通过**：export、workspace/release record、`manual-override.v1`、`skip-review.v1`、`qualification-review.v1`、`release-manifest.v1`、release metadata 和 failure 引用均通过各自严格 Schema，Schema ID 不复用；`exporter.log` 仅按诊断日志格式校验，不伪装成业务 Schema。
7. **虚假 ID 为零**：block、state、variant、target、图片路径和 release 引用只能来自 exporter/workspace 数据，虚假 ID 为 0。
8. **高优先级审核为零**：包括低置信度、Schema 冲突/修复失败、机器事实冲突、未决渲染异常和未审计 skip/excluded。
9. **语义完整**：每个可搜索变体有合规 `annotation-record.v1` AI 语义或等价人工语义；`unknown` 不满足硬约束。
10. **FTS 成功**：FTS5/规范化 LIKE 降级索引构建成功，名称/同义词/用途引用无孤儿。
11. **功能 hash 完整**：manifest 记录功能输入/产物 hash；release.json 记录 manifest hash，`checksums.sha256` 独立覆盖其它普通文件，所有摘要可复算且无循环。
12. **release layout 完整**：只有冻结的八类文件/目录，普通文件全部列入 checksums，禁止 symlink/hardlink；MCP smoke、两个 release 和 current 不属于此 gate。

黄金查询集、Top-5 指标和排序权重调优不是 MVP 阻断条件，不能用未建立的黄金集冒充质量证据。

### 9.1 activation gate

`activation gate` 在 candidate-build gate 之上检查：

1. 目标版本已有至少两个独立 `release_id`，且两者都通过 candidate-build gate；复制目录不能冒充独立 release。
2. 用临时测试 data-root、临时根 `current.json` 和本地原创 fixture 执行四工具 MCP smoke；不写生产 data-root，不把测试 current 当作生产指针。
3. 四工具只读取 candidate release，stdout/stderr、图片映射、错误层和只读写入检查通过。
4. 生产 current 的临时文件、flush/fsync、manifest/checksums 校验和原子 replace 准备就绪。

通过 activation gate 后，只有 WebUI 用户人工确认才执行 `ACTIVATE_RELEASE`。首次 candidate 可以构建但必须返回 `ACTIVATION_BLOCKED_RELEASE_COUNT`，不能激活。

## 10. `current.json` 和原子切换

所有版本共用唯一的 `<data_root>/current.json`；它的 `versions` 对象按精确 `minecraft_version` 分隔当前指针。内容至少为：

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
      "manifest_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    }
  }
}
```

根对象必须 `additionalProperties: false`，且顶层字段只能是 `schema_version`、`versions`、`default_minecraft_version` 和 `updated_at`；`default_minecraft_version` 必须是 `versions` 中已存在的精确版本。`updated_at` 是 WebUI 激活/回滚最近一次切换 current 的时间，不写入不可变 release。首次激活首个 Minecraft 版本时 `set_as_default=true` 强制；后续 apply 必须显式提供 `set_as_default`，为 `true` 才切换 default、为 `false` 则保留原 default。每个版本值必须且只能按 `current-pointer.v1` 提供 `minecraft_version`、`release_id`、`relative_path`、`manifest_sha256`，hash 使用 `sha256:<64 lowercase hex>`。激活时间只写 workspace activation audit 和 current `updated_at`，不得写回 release；release 使用 `built_at`，不使用 `published_at`。省略 MCP 版本时只能解析 default；显式未知或未发布版本必须失败，不得回退。MCP **MUST NOT** 接受历史 `release_id` selector；历史切换只能由 WebUI rollback 完成。

切换步骤固定为：

1. 在 `<data_root>/` 同一目录创建 `current.json.tmp.<operation_id>`。
2. 读取并保留其它版本的指针，只替换目标精确版本的值；按显式 `set_as_default` 决定是否替换 `default_minecraft_version`，更新 `updated_at`，并重新校验 release ID、精确 MC 版本、relative path、manifest hash、checksums 和质量报告。
3. 写入完整 JSON、fsync 文件后，对临时文件执行原子 replace 为 `<data_root>/current.json`；不得跨文件系统 rename，不得先删除旧指针。
4. 读取回目标版本指针做一次验证，记录 `CURRENT_SWITCHED` 和 workspace activation audit（操作者、时间、目标版本、release、`set_as_default`、原因）；审计不写回 release。

失败时保留旧 current，临时文件可由 WebUI 清理；绝不允许 current 指向半成品。MCP 每次启动/打开索引都校验指定版本的 current 指针和 manifest hash；指定固定 release 或版本时同样只打开完整不可变目录。

## 11. 保留、清理和回滚

每个精确 Minecraft 版本至少保留最近两个成功 release。`current.json` 指向的 release、用户标记为 pinned 的 release、当前被 MCP 使用的 release 和保底两个 release 不得删除。未激活 workspace 可以重建和清理，但不可变 release 不可修改。

清理和回滚必须由 WebUI 人工执行并记录操作者、时间、目标 release 和原因：

- cleanup 只能由 WebUI 用户人工执行，删除不受保护且超过每版本至少两个成功 release 的旧 release；
- 清理只删除未被 current/pinned/正在使用/保底两个 release 保护的成功 release；
- 回滚只把 `current.json` 原子指向已通过历史门禁的 release，不修改该 release；
- 清理失败不影响 current；回滚失败保持原 current；
- 不得用清理旧 release 规避“至少两个成功 release”的保留要求。

## 12. 流水线验收

实现验收必须覆盖以下恢复和发布情形：

1. 运行中杀进程后，遗留 `running` 任务能从游标恢复，已成功图片和 AI 结果不重做、不重复请求。
2. 同一导出和输入签名重跑得到幂等成功；改变策略、prompt、model 或资源包会得到新输入签名，不覆盖旧产物。
3. exporter 渲染失败和外部模型请求每个逻辑 item 最多自动重试一次；第二次失败进入审核/失败而非无限循环。
4. 缺一条注册表记录、一个合法状态、一个 skip 原因、一个图片或一个 Schema 字段时，发布被阻断。
5. 人工只能通过 `manual-override.v1` 修改语义，通过 `qualification-review.v1` 修改资格，通过 `skip-review.v1` 确认跳过；尝试修改机器事实、无效 variant 或无 scope 的 family/global 覆盖会阻断重建。
6. MCP 在发布进行中仍读取旧 current，不会读工作库或半成品；原子切换失败仍保持旧 current。
7. 发布后修改 workspace、override 或 Schema 不改变已发布 release；新内容必须产生新 release。
8. 维护至少两个成功 release，current/pinned/正在使用/保底版本不能被清理，人工回滚只改变 current 指针。
9. candidate-build gate、activation gate、MCP 冒烟、FTS 构建、零虚假 ID、高优先级审核为零和原子切换均留下可审计日志。

最终的导出字段和图片完整性由 [导出契约](export-contract.md) 验收，分层/来源由 [数据与 Schema](data-and-schemas.md) 验收；三者任何一项不一致都应阻断 release。
