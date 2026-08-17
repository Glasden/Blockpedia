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
- [OpenAI Provider 接口](openai-provider.md)
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

默认调度在一个 Python 进程内使用一个 process-lifetime 的有限 in-process executor；该 executor 最多 `5` 个 slots，所有 run 共享，不为每个 run 创建 executor。全局 active sends 不超过 `5`，每个 run 不超过其冻结的 `offline_annotation` concurrency。不得引入 Redis、Celery、Kafka、微服务或云对象存储。未来等价替换必须保留幂等键、游标、原子 release 和 MCP 只读边界；本项 D-044 不允许把该 executor 替换为服务或队列。

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
│   ├── provider-profiles.json          # 全局非秘密 profile authoritative file
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

全局非秘密 provider profiles 的唯一持久来源是 `<data-root>/cache/provider-profiles.json`，WebUI 必须以临时文件、flush/fsync 和原子替换保存它；文件不得包含 API key 或其它秘密。workspace 中的 `provider_profiles` 仅保存 run snapshot，供该 run 重放，不能作为全局 profile 配置来源。该生命周期收敛不引入新 Schema、服务或 migration。

所有图片来自用户本地合法安装和 exporter 渲染。导出包、cache、workspace 和 release 可以保存生成的 PNG、对象蒙版和特征，但只能保存机器元数据与渲染产物的哈希，不保存原始资源。测试 fixture 必须由程序生成原创图/伪数据；真实集成测试依赖本地导出，缺失时必须明确输出 `SKIPPED_LOCAL_EXPORT_MISSING`，不能静默改用真实资源或伪造通过。

### 3.1 Import check handoff and recent-check discovery

`cache/import-checks/{check_id}/state.json` 是 import check 的 authoritative state。首页、导出目录 listing 和 `GET /api/imports/checks?minecraft_version=&limit=` 只在请求时安全扫描严格 check ID 目录和 `state.json`，按 `(minecraft_version, export_id)` 选择最新 actionable check；扫描结果不落第二份索引。禁止 `index.json`、`owner_instance_id` 或其它 owner instance marker、额外 SQLite 表、通用 migration 或任意生成框架。目录 entry 必须拒绝 symlink/junction/reparse/link，summary 只允许脱敏的版本、export ID、状态、时间、anchor 状态、进度和稳定错误码。

同一 WebUI 进程的 `ImportService` 使用一个 coordinator `RLock`，协调键为 `(minecraft_version, export_id)`。不同 opaque chooser ref 解析到同一 export 不能产生两个 active check；exact active duplicate 返回 `202` 和相同 `check_id`/`reused=true`。passed check 的复用只比较 canonical source entry 当前 raw `manifest.json` 与 `checksums.sha256` 的 SHA-256 是否分别等于 passed state 的 declared anchors；这是轻量 declared-anchor comparison，不是对 live export 每个 artifact 未变的证明。immutable checked snapshot 和一次 validator pass 才是完整 integrity 依据。anchor 不匹配、failed 或 interrupted 时新建 `202` check；进程重启后的 active check 固定为 `IMPORT_CHECK_INTERRUPTED`，不自动 resume。

state 只做最小扩展：`created_at`、`updated_at`、validator subphase/progress 和 `workspace` association：`status=absent|creating|created|failed`、`import_id`、`run_id`、`error_code`。不得把绝对 source path 或 chooser ref/token 持久化。旧 state 没有关联时，可以扫描现有 workspace `work.sqlite3`，按精确 `minecraft_version`、`export_id`、manifest hash 发现既有 run；这是向后兼容的读取，不是新索引或数据库结构。

`POST /api/imports` 是严格的 `{check_id, copy_mode: "copy_to_workspace"}` 请求，**不含 `project_id`**，代码和 Schema 也不得加入该字段。请求在同一 coordinator lock 内先预留 `import_id`/`run_id` 并把 association 写为 `creating`，再由既有 workspace builder 从 immutable snapshot 建库。相同 passed check 是幂等入口：`creating` 重复请求返回 `202`/同一 `run_id`，有效 `created` 返回 `200`/同一 `run_id`，首次完成创建可返回 `201`；任何分支都不能分配第二 workspace。只有验证过最终 `work.sqlite3` 后 UI 才能 deep-link。重启发现 `creating` 时先 reconcile 原 reservation 的 final workspace；无有效 workspace 则保留原 ID，进入稳定 failure/retry，重试不得静默生成第二 run。

check progress 只展示 `snapshot → validate → finalize` 三个宏观阶段。snapshot callback 接既有 copy/hash loop；validator callback 接既有 inventory、Schema、JSONL、reference、render、checksum loops，不增加 scan/read/hash/decode。`completed` 在同一 validator subphase 内单调递增；只有已有总量时填写 `total`，否则为 `null`/`0`，UI 使用带 live count 的 indeterminate bar。state persistence 节流，phase transition/terminal 强制写入；SSE 发完整 snapshots，不逐 item 广播。

## 4. Python/SDK 锁定和 `PREPARE`

Fabric/Gradle 工具链由导出契约固定为 Minecraft Java `26.2`、Java `25`、Fabric Loader `0.19.3`、Fabric API `0.157.0+26.2`、Loom `1.17.19`、Gradle `9.5.1`；Minecraft 26.2 使用 native Mojang names/unobfuscated，不解析外部 mappings artifact。R0 只锁定实际引入的 Python tooling 依赖；不预锁未实现的 R2-R4 栈。后续依赖在使用前必须精确/hash 锁定，Windows 在对应阶段验证，Linux `manylinux_2_17` / glibc `>=2.17` 的安装、运行、wheel/ABI 和最终双平台复现统一在 R5 验证。candidate check/build 的前置只要求 R0-R3 和 candidate-build gate；activation-check/apply 的前置才要求 R0-R4、activation gate 和用户确认。

`PREPARE` 在任何外部模型请求或写入成功产物前必须检查：

1. 导出包存在、路径在 `<data_root>/exports/` 或经用户明确授权的位置，目录名等于 manifest `export_id` 且不是 `.<export_id>.staging`，并通过 [导出契约](export-contract.md) 校验。
2. manifest 的 MC/Fabric/Java/Loom/Gradle/mappings 精确值与当前项目 lock 一致；精确字段形状以真实 Schema 文件为准。
3. R0 tooling 的 Python lock 已存在，运行解释器和实际使用依赖哈希一致；否则报告 `TOOLCHAIN_NOT_LOCKED` 并停止。
4. exporter 已写入的策略版本和所有真实 JSON Schema 已加载并校验；Studio 只校验 exporter 已写入的策略版本，不执行状态选择或渲染。R1 外部 validator 的全量检查只执行一次。
5. 资源包只有 vanilla 标识/哈希，没有原始资源副本；导出 scope 是 `minecraft` block registry。
6. 计算 `run_id`、`input_signature`、工作库 Schema 版本和每阶段幂等键。
7. 同一输入签名已有成功阶段产物时，验证哈希后复用，不重新执行；不存在所选 adapter 的 wire 能力证明时停止，不允许 warning/ack 继续 provider 任务。Responses 的 `store=false` 和 Chat 的省略 `store` 都不是远端 retention 证明。

PREPARE 输出可恢复的 run spec、导出 manifest 快照、版本锁快照和阶段游标；失败时不创建可发布 release。

## 5. 阶段输入、输出和完成条件

### 5.1 `IMPORT_EXPORT`

Python 导入只接受 passed check 的 `check_id` 和固定 `copy_mode=copy_to_workspace`；它只消费 check 创建的 immutable snapshot，不读取 source chooser ref 作为导入输入，也不重新调用 R1 validator。check 已完成一次 strict Schema、关系、资源、PNG 语义/质量、checksum 和 artifact digest pass；导入过程只复制/校验 snapshot 到 `workspace/{minecraft_version}/{run_id}`，不重新枚举注册表、选择代表状态或渲染图片。请求不得包含 `project_id`。

输出：工作库中的只读机器事实投影、原始导出引用和导入完整性报告。

#### 5.1.1 D-045 banner-export refresh

Targeted refresh 是一个只由 WebUI 发起的窄操作，不是新的 Studio stage、import mode 或通用 patch/migration framework。它只接受 passed immutable full export check，并要求当前 run 在 `HUMAN_REVIEW/needs_review`、没有 live work、check 的 source export 与 `expected_base_export_id` 精确相等，且 target set 是稳定排序的 32 个 vanilla standing/wall banner IDs。输入 package 的 normalized semantic diff 必须严格为这 32 个 `skipped → selected` transitions 加上每个目标的三份 render files（共 96 个）；任何其它 record、state、artifact 或 source lineage 差异都 fail closed。

在既有 run lock 内，服务先把新 complete replacement source export 和精确目标 render/feature files 写入 workspace-local staging，并用窄 journal/backup 记录可恢复的 source/file/SQLite projection replacement。只有文件和投影均校验成功才提交；异常时恢复 source、files 和 SQLite 到 refresh 前状态。该路径复用既有 tables、columns、state enum、validator、feature extractor、review/audit 和 lock，不新增 table、column、status、migration framework、service、queue 或 Python product CLI；Python 仍不选择 variant 或渲染图片。

成功 refresh 保留早期已成功阶段；只将 `AI_ANNOTATE`、`VALIDATE`、`HUMAN_REVIEW` 置为 `pending`，并从 `AI_ANNOTATE` 继续。它保留全部既有 annotations、provider requests、jobs 和 reviews，只为新目标增加恰好三个未批准 AI jobs，按稳定目标顺序批量为 `12 + 12 + 8`，使用 `banner_refresh_*` logical keys。目标 feature files 可在该窄操作中确定性生成，但不把完整 `EXTRACT_FEATURES` stage 重新运行。

`imports.report_json` 复用现有 JSON storage，必须保存严格的 `banner-refresh.v1` provenance：base/new import、export、manifest、checksum hashes、精确 sorted targets 和 banner policy token。它不是新的 Schema ID，也不触发 SQL migration。Release check/build 的 functional inputs 必须包含该 provenance；对于 preserved requests，只有排除全部 32 个 targets 且在 historical envelope export ID 下 current input 重算完全一致时，才允许继续使用 historical base export ID。新的 banner requests 必须使用 replacement export ID。这样可以保留不相关历史请求，同时阻止混用未经证明的 source lineage。

### 5.2 `VALIDATE_REGISTRY`

复用 validator 已确认的 `registry_snapshot_sha256` 与排序后的 block ID 覆盖证据，并确认导入投影的版本和引用未改变；不重新对 exporter 全包执行同一 registry/Schema 扫描。缺记录、重复主键或投影不一致使阶段 `failed`，不得补造或跳过方块。

### 5.3 `VALIDATE_VARIANTS`

验证导入投影中的 canonical/default state、每个 block 唯一 default representative、所有 state 到同 block representative 或 pending skip 的映射、资格初始值、warnings 和引用；复用 validator 的跨记录结果，不重新选择、重新渲染或重复整包扫描。失败必须绑定导出记录或审核任务。

### 5.4 `VALIDATE_RENDERS`

验证导入投影仍引用 exporter 已生成的固定 512×512 摄影棚四视角 PNG、蒙版和 `render-metadata.v1`；复用 validator 已完成的单次读取/解码和 artifact digest 结果。Studio 不得重新渲染、裁剪、替换或补造图片；引用改变、缺图或投影不一致即失败/进入审核。

输出：只读图片资产、渲染元数据、图像哈希和失败/审核队列。

### 5.5 `EXTRACT_FEATURES`

由确定性 Python 脚本完成，不调用 LLM。基于对象蒙版和导出几何计算 Oklab/Lab 颜色摘要、亮度/饱和度、透明像素比例、几何类别、边缘密度、纹理方向性、动画/发光指标。特征记录必须携带 `feature_extractor_version` 和输入图片/机器元数据哈希；重复输入直接复用。

输出：可解释的颜色、几何、视觉特征和 deterministic machine tags。提取器不得新增 block ID、状态或事实。

### 5.6 `AI_ANNOTATE`

只把合格的可搜索变体、预览图和紧凑机器元数据发给唯一已配置的 `OpenAIProvider` model。导出器不调用 LLM，调用由 Worker 的所选 protocol adapter/codec 负责；`openai_responses` 请求必须使用 `/responses`、`store=false`、`input_text/input_image`、strict JSON Schema，`openai_chat_completions` 请求必须使用 `/chat/completions`、省略 `store`、`text/image_url`、strict JSON Schema；两者都最小披露且不得自动协议 fallback，规则见 [OpenAI Provider 接口](openai-provider.md)。兼容 `base_url` 仍是用户批准的所选协议 endpoint，不是第二 provider。能力探测只验证所选协议 wire 形状，不证明远端 retention。离线标注批次建议 8～16 个变体；每个逻辑请求使用独立且全局唯一的 `provider-batch-envelope.v1`，并以 `stage` 绑定 wire Schema。`offline_annotation` envelope 必须包含 tile-to-variant 映射；`query_spec` 的 `input_summary` 精确只有 `query_sha256`，不能含候选映射，使用 `query-spec-output.v1`；`visual_rerank` 只携带本地已召回候选的完整映射，使用 `rerank-output.v1`，三者不得混用输出 Schema。

#### 5.6.0 Prompt version 与诊断边界

`prompt.v1` 和其它历史 prompt version string 必须保持 exact legacy behavior，可 replay；只有 exact `prompt.v2` 使用 slim model-visible text，且必须来自新的 run/profile snapshot。v2 保留 contact sheet/tile labels，trusted instruction 只要求 annotate existing tiles、复制每个 tile 的 exact existing `variant_id`、不创建/修改 ID 或 machine facts；model-visible `tiles` 只含 `tile_id`/`variant_id`，per-tile metadata 只含 `tile_id` 和去重有界 `geometry_classes`。v2 移除 image/machine hashes、`block_id`、`canonical_state_id`、exact dimensions/volume、behavior booleans/emission、`machine_tags`、feature metrics/version/input hash 和重复 feature geometry/tags。完整 machine metadata、hash、source image、envelope/cache/signature/release lineage 仍在本地，校验不减弱；current pending v1 jobs 不迁移、re-sign、cancel 或 delete。

D-042 不改变 current output wire/Schema；模型仍返回 `schema_id`、`variant_id` 和全部 13 个 required item fields。local `schema_id` injection、`tile_id` codec 和 semantic-field reduction 保持 deferred，除非 diagnostics 足以支持另一个 owner-approved/materialized Schema decision。只有 FINAL `offline_annotation` validation 在总 retry budget 用尽后仍失败时，才将六字段 sanitized diagnostic 通过 internal `ProviderResult` 追加至既有 `PROVIDER_FAILURE` review task 的 `evidence_json`，保留 job/provider request refs；不新增 provider envelope field、`provider_requests` column、table/report、migration 或 Schema ID。Provider/Worker full validation、ID/hash/cache/record/variant/`VALIDATE`/release gates、local `uniqueItems` 和 max-one-retry 保持不变；只移除 freshly produced by `_hash_json` hash 上的 tautological regex check，classification 不得改变。

缓存键至少为（`schema_version` 随 stage 绑定）：

```text
image_hash
+ machine_metadata_hash
+ adapter
+ prompt_version
+ model_id
+ schema_version
+ base_url_stable_id
+ stage
```

已有通过 Schema 且 adapter、输入完全相同的结果不得再次请求。MVP **MUST NOT** 记录或展示 Token usage、费用、预算或价格字段；请求审计只保存 `provider-batch-envelope.v1` 的非秘密引用、adapter、stage、wire Schema、输入摘要、request ID（如契约允许）和结果哈希，不保存 Token 数值、完整 request/response 或 retention 判断。模型只能生成语义字段；Schema 冲突或一次修复失败进入高优先级审核。置信度门控固定为 `>=0.80` 自动通过、`0.65–<0.80` 普通审核、`<0.65` 高优先级；Schema 冲突/修复失败始终高优先级。

#### 5.6.1 批次授权、顺序提交和 drain 语义

手动 per-batch approval 是默认。用户确认前，WebUI 必须让全部 planned batches 保持可 inspect；确认只对 unchanged frozen remaining plan 生效，并绑定 D-040 定义的 immutable plan hash、run-frozen provider 和 requested `model_id`。plan hash 只包含 `run_id`、`effective_config_hash` 和按计划顺序排列的 `job_id`/`logical_key`/recomputed payload signature；精确 canonical 形状见 [`decisions.md`](decisions.md)。

一次 aggregate confirmation transaction 使用并验证已经持久化的 pending identity：`jobs.input_signature`、cursor `payload_signature`/`input_hash`、`tile_ids`/`variant_ids`、run `effective_config_hash` 和 frozen provider snapshot；aggregate path 不为全部 jobs rebuild images、contact sheets、prompt text 或 machine metadata。既有 plan-hash object/field name `recomputed_payload_signature` 保持不变，其 plan-time value 是 validated persisted payload signature。任一持久化 hash 缺失、冲突、无效，或 pending 集合/provider/config lineage 发生 TOCTOU mismatch 时 approve none；全部一致时，使用现有 job `cursor_json` 的 `approved` 标记批准所有 included pending jobs，并写 one plan audit 与 per-job approval audits。不得增加 auto-mode field、stage cursor、config snapshot、数据库字段或 Schema。

每个 planned batch 在 aggregate confirmation 前仍通过既有 safe preview lazy inspect；one-batch preview 可以重建其 bounded payload。Immediately before **every** actual external send，Worker 必须从 frozen run profile rebuild complete one-batch payload/contact sheet/prompt/machine metadata，recompute full signature，并与 approved job signature 比较。任一 mismatch 必须 revoke 该 job 的 approval、在任何 HTTP request 前 pause，并不得发送；不能用 aggregate persisted identity shortcut 绕过该 final TOCTOU gate。D-041 不改变 manual mode/default、D-044 当前 send concurrency、item-local continue、fatal stop、retry 或 audit。

确认后的 automatic submission 严格按冻结顺序运行：`offline_annotation` 的 send concurrency 是每个 run 冻结的整数 `1..5`（默认 `1`），`query_spec`/`visual_rerank` 固定为 `1`，并计数 logical batch 而非 HTTP attempt。一个 process-lifetime in-process executor（最多 `5` slots）共享所有 run；global active sends `<=5`，per-run active sends `<=` frozen offline bound。Worker 只能使用 run-frozen profile；mutable global active profile 仅适用于新的 Studio work/profile management，不能替换已有 run。item-local Provider failure 变成 high `needs_review` 后继续下一个 approved batch；fatal provider/config/auth/capability failure 必须 atomically 写入 request evidence、review、job/stage/run failure 和 audit，并在后续 send 前停止。`needs_review` item 不阻塞 AI_ANNOTATE drain：valid low-confidence 和 item-local failure 继续进入 `VALIDATE`、`HUMAN_REVIEW`；fatal 才停止 stage/run。

Provider retry source 必须是 terminal `needs_review|failed` 的 AI job，且 error 属于 eligible item-local Provider error；fatal、`PROVIDER_CANCELLED` 和没有 Provider error 的 job 不 eligible。variant review 不是 source。source 必须为 leaf；child cursor 包含 `retry_of_job_id`，nonce 由 source `job_id + input_signature` 确定性生成，每个 source 只能生成一个 child；failed child 可作为下一次显式 generation。row/bulk action 在同一 transaction 创建 child、resolve source 的 open provider-review siblings，并保留 source rows、evidence 和 provider request；重复 POST 幂等，legacy retry rows 只兼容读取、不重写。bulk action 只 retry eligible failed leaf batches 并 auto-approve retry wave；generic `retry-failed` 排除 fatal/provider AI jobs，不能绕过同一逻辑请求的两次总尝试预算。

#### 5.6.2 D-044 claim barrier、send-started linearization 与执行器生命周期

Worker 必须按 frozen plan order 使用 ordered contiguous approved claim barrier：只能 claim 从当前未完成位置开始的连续 approved prefix，遇到未 approved、approval/lineage 失效或 pause/cancel/fatal stop 时，后续 batch 不得越过 barrier。claim 不是 durable pending provider request reservation，也不向远端取得 exactly-once claim。

在任何 HTTP 前，Worker 必须对每个 batch 完成完整 one-batch payload/contact sheet/prompt/machine metadata rebuild、full signature recomputation、approval/plan lineage、run/stage state、停止信号和 active-send bound 的 final gate。通过 gate、占用 active-send slot 并进入 provider HTTP call 的瞬间定义为 send-started linearization。HTTP 不得包在 SQLite transaction 中；发送前/后的 DB work 各自使用本地 transaction，SQLite connection/transaction、provider client 和 provider mutable state 不得跨线程共享。

pause/cancel/fatal 只停止 claimed-unsent/later sends；already-started calls 可以完成并持久化既有 request evidence 与 item terminal state，但不得 revive 已 failed/cancelled 的 run/stage。fatal supersedes paused，不 supersede 已 durable cancelled；不得 fake in-flight cancellation。hard crash after send before commit 可能留下最多 frozen concurrency 数量的 unknown outcomes；没有 durable reservation 或 remote exactly-once claim，startup 不自动 resend，显式 `recover` 仍是必要入口。

唯一 executor 在一个进程生命周期内创建并共享所有 run，stop 必须等待 live futures；live futures 或 DB work 存在时，相关 run 不能 stale-recover 或报告 completion。只有无 DB work 且无 futures 时才可完成 drain。调度 concurrency 只在 profile/run runtime scheduling 中冻结，不进入 release snapshot、`release-manifest.v1` 或 provider Schema。合法 offline-concurrency-only profile edit 保留 `verified`/`enabled` 且无需 reprobe；其它 invalidation 不变。

同一 run 的 strict pristine reconfiguration 仅可在 paused `AI_ANNOTATE` 且无 live future/provider request、provider-request evidence/annotation/AI artifact/provider review/AI review/send/result/retry/cancel evidence，并且每个 AI job 都是 pending、unapproved、ownerless、clean 时执行。检查不能证明时 fail closed；通过后原子替换 frozen config/pending jobs，保留 R2/machine evidence，写 `R3_RUN_RECONFIGURED` 并 invalidate old plan，不重用 approval。不得增加服务、队列、per-run executor、adaptive concurrency、SQL/Schema/migration/status/dependency/CLI/fallback/retry 语义或 fake cancellation。

### 5.7 `VALIDATE`

按以下顺序执行：Schema → 版本/来源 → 引用完整性 → 机器事实不可覆盖 → 状态合法性 → 图片可读性 → 变体/状态覆盖 → AI/人工语义门控 → SQLite FTS 构建预检。任何失败都必须绑定到逻辑项和错误码，不能用人工文本掩盖机器失败。

输出：不可发布缺陷清单、审核任务、可发布候选计数和质量报告草稿。

### 5.8 `HUMAN_REVIEW`

只处理渲染异常、机器与 AI 冲突、低置信度/Schema 高优先级项、经配置的抽样质检以及特殊状态覆盖。审核员可以接受/编辑受控语义、声明跳过、重新请求 AI 或要求 exporter 按策略重新导出，但不能在 Studio 内重选状态或重渲染。人工语义修改写 `manual-override.v1`，资格修改写 `qualification-review.v1`，跳过写 `skip-review.v1`；默认按 `variant_id`，family/global 必须显式 scope 和批准。

AI_ANNOTATE 的 item `needs_review` 不会阻塞该阶段 drain，也不会阻止后续 `VALIDATE` 和 `HUMAN_REVIEW` 消费有效低置信度结果及 item-local provider failure；fatal 才使 stage/run 立即 `failed`。最终 `HUMAN_REVIEW`/candidate-build 仍要求所有高优先级任务为零、所有 skip/excluded 审计已审核、所有可搜索变体有合规 AI 或等价人工语义；存在待处理任务时不能进入 `BUILD_RELEASE`。

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

### 5.9.1 R3 Phase C cache、snapshot 和 candidate commit（本节为 pipeline owner）

R3 Phase C 的 release check cache 只使用以下布局；它不是 import check cache，也不建立第二个列表索引：

```text
<data_root>/cache/release-checks/<check_id>/
├── state.json
└── quality_report.json
```

Phase C owned ID 使用 `secrets.token_hex(16)` 的等价规则：前缀之后必须恰好 32 个小写十六进制字符，不得使用 UUID、ULID、时间戳或大写字符：

```text
check_id        = check_<32 lowercase hex>
release_build_id = build_<32 lowercase hex>
release_id      = rel_<32 lowercase hex>
staging_dir     = .rel_<32 lowercase hex>.staging
```

`state.json` 是严格的非 Schema JSON 文件，顶层字段必须且只能是：`format_version`（整数 `1`）、`check_id`、`release_build_id`、`run_id`、`minecraft_version`、`source_export_id`、`status`、`can_build`、`snapshot_fingerprint`、`quality_report_sha256`、`release_id`、`created_at`、`updated_at`、`error_code`。`status` 只能是 `passed|failed|stale|built`；`passed` 的 `can_build` 可为 true/false，`failed|stale` 必须为 false，`built` 必须为 true；尚未 build 时 `release_id=null`，无错误时 `error_code=null`。同步 check 不持久化 `pending`/`running`，build 成功才把同一 state 原子更新为 `built`；质量报告字段和 item 结构唯一由 [`quality-and-testing.md`](quality-and-testing.md) 拥有。`state.json` 与质量报告均不得包含绝对路径、chooser ref、secret、WAL bytes、worker ID、cursor 或完整 provider response。

`quality_report.json` 在 check 结束时写入一次临时文件、flush/fsync 后 atomic replace，随后视为不可修改。check 的报告永远留在原 check 目录；build 不回写它，而是从相同逻辑 snapshot 派生 release 内新的 `quality_report.json`。失败或 stale 只更新 `state.json` 的状态/时间/错误，不修改历史报告。

#### 最新 check、逻辑 snapshot fingerprint 与 TOCTOU

对精确 `(run_id, minecraft_version)`，最新 check 是所有合法 `state.json` 中按 `(created_at, check_id)` 的 UTF-8 字节序升序取最大值；损坏、越界、链接/reparse 或未知字段的目录不是可用 check。每次新的同步 check 都产生新的 `check_id`；build 只接受该键的最新 state，旧 check 即使 `can_build=true` 也返回 `RELEASE_CHECK_STALE`。已 `built` 的 check 仍是历史状态，不得再次 build。

`snapshot_fingerprint` 必须对逻辑数据做 canonical JSON（JCS）后计算 SHA-256，不得对 `work.sqlite3`、`-wal`、`-shm` 或任何 SQLite 文件 bytes 取 hash。输入固定包括：精确 `run_id`/`minecraft_version`/`source_export_id`、已验证 export manifest 与工具链锁定摘要、`workspace.v1.sql` 的已冻结版本标识、参与发布的 blocks/states/variants/features/annotations 的 allowlisted 逻辑列、完整人工三类记录、有效 provider snapshot、每个被引用普通 artifact 的规范相对路径与内容摘要，以及实际使用的 Schema ID inventory。数组按稳定主键排序，SQLite rowid、列返回顺序和 WAL checkpoint 状态不参与结果。

若 workspace 使用 D-045 refresh，`imports.report_json` 中严格 `banner-refresh.v1` provenance（包括 base/replacement import/export/manifest/checksum hashes、exact targets 和 policy token）是同一 functional input；historical base export ID 的保留只在 preserved request 的 target exclusion 与 historical envelope input recomputation 条件同时通过时成立，新增 banner request 一律绑定 replacement export ID。

fingerprint 明确排除：`work.sqlite3`/WAL/SHM 原始 bytes、mtime/ctime、worker heartbeat、job cursor/worker ID、日志、缓存文件、source absolute path/chooser token、临时目录名、质量报告、`release_id`/`release_build_id`、`built_at`、`current.json`、MCP 数据和任何后续 activation 输入。人工记录中的业务时间、原因、证据和 input signature 属于逻辑输入，不因其来自审核而排除。

check 读取 workspace 时必须在只读一致性事务中取得 allowlisted 逻辑行和 artifact 引用，并对每个 artifact 做安全 `lstat`/open/hash；每个文件在读取前后重新检查身份、大小和链接属性。build 在开始 staging 前、完成 staging hash 后各重算一次 fingerprint；任一结果不同、出现新 check、目标版本/源 export 改变或任何引用被替换，必须只清理本次 staging、将 state 标为 `stale`，且返回 `RELEASE_CHECK_STALE`，绝不 rename 半成品。check cache 报告不因 TOCTOU 重写。

#### 独立 release index、layout 与 preview mapping

release 使用独立的 `release-index.v1.sql` 投影契约；它不是 JSON Schema ID，不改变 `workspace.v1.sql`，也不通过通用 migration 从工作库升级。新建 `index.sqlite3` 时 `schema_meta` 必须恰有 `format_version=1`；该列/值是 release index 格式版本，不得写成 `schema_version` 或伪装成 D-030 Schema。最小表、列和索引固定为：

```sql
CREATE TABLE schema_meta (
  format_version INTEGER PRIMARY KEY CHECK (format_version = 1)
);
CREATE TABLE blocks (
  block_id TEXT PRIMARY KEY,
  minecraft_version TEXT NOT NULL,
  translation_key TEXT,
  name_zh TEXT,
  name_en TEXT,
  default_state_id TEXT NOT NULL,
  machine_facts_json TEXT NOT NULL
);
CREATE TABLE states (
  state_id TEXT PRIMARY KEY,
  block_id TEXT NOT NULL REFERENCES blocks(block_id),
  properties_json TEXT NOT NULL,
  is_default INTEGER NOT NULL CHECK (is_default IN (0, 1))
);
CREATE TABLE visual_variants (
  variant_id TEXT PRIMARY KEY,
  block_id TEXT NOT NULL REFERENCES blocks(block_id),
  canonical_state_id TEXT NOT NULL REFERENCES states(state_id),
  represented_state_ids_json TEXT NOT NULL,
  preview_path TEXT NOT NULL,
  mask_path TEXT NOT NULL,
  render_metadata_path TEXT NOT NULL,
  image_sha256 TEXT NOT NULL,
  mask_sha256 TEXT NOT NULL,
  render_metadata_sha256 TEXT NOT NULL,
  candidate_qualification TEXT NOT NULL,
  warnings_json TEXT NOT NULL
);
CREATE TABLE annotations (
  variant_id TEXT PRIMARY KEY REFERENCES visual_variants(variant_id),
  semantic_json TEXT NOT NULL
);
CREATE INDEX states_block_id_idx ON states(block_id);
CREATE INDEX visual_variants_block_id_idx ON visual_variants(block_id);
CREATE INDEX visual_variants_qualification_idx
  ON visual_variants(candidate_qualification);
```

FTS 只有一个实现分支：SQLite 支持时建立 `search_fts(variant_id UNINDEXED, normalized_text)` 的 FTS5 `trigram` virtual table；不支持时不建该 virtual table，改建 `search_text(variant_id PRIMARY KEY, normalized_text)` 和 `search_text_normalized_idx` 普通索引。两种分支都必须由 Gate C 的 `FTS_READY` 证明，不能增加 vector 列或外部服务。release index 只保存上述发布投影，不复制三类原始审核记录；原始记录包由 [`data-and-schemas.md`](data-and-schemas.md) 定义。

candidate staging 与最终 release 都只能拥有以下八类顶层项，`release-index.v1.sql` 是构建时的独立格式契约，不是 release 内第九个文件：

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

每个发布视觉变体的 `variant_id=minecraft:<suffix>` 必须映射为同一 release 内：`previews/minecraft/<suffix>/preview.png`、`previews/minecraft/<suffix>/mask.png`、`previews/minecraft/<suffix>/render.json`。`suffix` 保持 canonical block ID 的原始安全 segment，不做隐式 slug、冲突覆盖或外部映射表；index 中三列 preview ref 必须是这些 release-relative POSIX 路径。preview、mask、render metadata 是普通文件，不得是 symlink/hardlink，所有路径必须通过安全相对路径规则。

`schemas.sha256` 只列本次 candidate 实际使用的 Schema，按 Schema ID UTF-8 字节序排序；Phase C 的可用集合为 exporter 的 `export-manifest.v1`、`export-block.v1`、`export-state.v1`、`export-variant.v1`、`export-failure.v1`、`render-metadata.v1`，workspace/release 的 `block-record.v1`、`state-record.v1`、`visual-variant-record.v1`、`annotation-record.v1`、`manual-override.v1`、`skip-review.v1`、`qualification-review.v1`、`release-manifest.v1`、`release.v1`，以及实际离线标注使用的 provider `provider-batch-envelope.v1`、`annotation-batch-output.v1`、`annotation-wire-item.v1`。`query-spec-output.v1`、`rerank-output.v1`、全部 MCP Schema 和 `current-pointer.v1` 不属于 Phase C inventory；若某个实际输入未使用则不得列入。

#### hash DAG、checksum 和单 candidate 成功边界

发布 hash DAG 固定为：逻辑 workspace/export/provider 输入 → `snapshot_fingerprint` → index/preview/semantic/人工原始记录包等 functional artifacts → release `quality_report.json` → `manifest.json`（包含 functional inputs/artifacts 与 quality report hash）→ `release.json`（包含 manifest hash）→ `schemas.sha256` 与 `checksums.sha256`。`manifest.json` 不哈希自身、`release.json`、`schemas.sha256` 或 `checksums.sha256`；`release.json` 不哈希自身。`checksums.sha256` 必须按路径排序并覆盖 release 内除自身外的**全部普通文件**，包括 `release.json`、`manifest.json`、`index.sqlite3`、`previews/` 下文件、`quality_report.json`、`manual-overrides.json` 和 `schemas.sha256`，不列目录、symlink 或 hardlink。

一次 build 只能为一个 `release_build_id` 建立一个 staging 和一个最终 candidate：先确认最终目标不存在，再在同一版本 release 父目录创建 `.rel_<32hex>.staging`，写完并验证完整 layout、index、质量报告、manifest、schemas inventory 和 checksums，完成文件/目录 durability 后做一次同卷原子 rename 为 `rel_<32hex>`。目标已存在、hash/完整性复核失败或任意 TOCTOU 失败都不得覆盖目标；失败只清理本次精确 staging。成功边界是 rename 后重新打开/校验完整 release、state 原子更新为 `built` 并完成 WebUI audit/status transaction；此后 release 内容不可变，后续只允许更新 release 外 cache/audit，不得回写 release。

### 5.10 `ACTIVATE_RELEASE`（R4/R5 后续边界，不属于 Phase C）

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

启动时只检测 stale `running` 并在内存/展示层标示，不修改数据库状态；存在 live future 或未完成 DB work 时不能把该 run 当作可 recover 的 stale 工作。只有 WebUI `recover` 操作可以把 stale item 写回 `pending` 或 `needs_review`；成功 item 不重跑。auto-approved cursor 在重启后保持，不因 stale 检测被清除。自动 retry 只影响 item 的 `auto_attempt`，每个逻辑 provider request 最多一次总自动重试；D-040 的 retry child 是新的显式 generation，不是追加到 source 的第三次 attempt。状态变更必须在 SQLite transaction 中与产物引用、错误码、游标和 audit 记录一起提交。pause/cancel 只停止尚未 started 的 sends；already-started calls 可完成，但不能 revive failed/cancelled run。SSE/browser disconnect 不改变 Worker。

### 6.2 最小任务字段

`jobs` 表至少包含：

```text
job_id, run_id, stage, logical_key, input_signature,
status, auto_attempt, priority, worker_id, heartbeat_at,
cursor_json, output_hash, error_code, error_message,
created_at, started_at, finished_at
```

`runs`、`stage_runs` 和 `jobs` 分别保存三套状态；AI 任务只保存 provider、adapter、model、prompt/schema 版本、request ID（如可用）、响应哈希、响应缓存键和审计状态，**MUST NOT** 保存 Token usage、费用、预算或价格字段。任务唯一约束是 `(run_id, stage, logical_key, input_signature)`；成功产物的 `output_hash` 必须可从文件/数据库重算。

### 6.3 游标、幂等和缓存

每阶段保存 `stage_cursor`：已枚举输入的排序键、已完成数量、当前 logical key、策略/输入哈希和最后心跳。批次 approval 使用现有 job `cursor_json.approved`，不是新的 stage cursor 或持久 mode。Worker 领取任务使用 transaction lock；完成时先将临时产物 fsync/rename，再在同一 transaction 写成功状态和 hash。进程崩溃时没有成功 transaction 的产物视为未提交。启动检测 stale 只展示，不推进游标或写状态；Worker 仍须在每次 send 前立即复核 approval 和 recomputed payload signature。

幂等键命中且输出校验通过时直接复用，绝不重复请求或覆盖成功产物。命中但 hash 不一致时报告 Worker-local `IDEMPOTENCY_CONFLICT` 并停在 high `needs_review`/`failed`，不能自选一个文件继续。Provider retry child 使用 source job/input signature 的 deterministic nonce，重复 row/bulk action 只能命中既有 child；source 的原始 evidence/provider request 不被覆盖。

### 6.4 遗留 `running` 检测与 WebUI recover

应用启动或定时检查 heartbeat 超时的 `running` 任务，只生成内存诊断和 WebUI 展示标记 `stale=true`，并写 stderr/展示日志；不得自动修改 SQLite 的 run/stage/item 状态。live futures、provider request 或 DB work 尚未收敛时，不能把任务判定为可 recover 的 stale。用户调用 WebUI `recover` 后才执行：

1. 读取临时文件和外部请求记录；
2. 若完整输出和 hash 已存在，校验后在事务中补写 item `succeeded`，不重复执行；
3. 若没有完整输出且 `auto_attempt=0`，在事务中增加一次 `auto_attempt` 并置 item `pending`；
4. 若自动重试已用，置 item `needs_review` 或 `failed`，不再自动运行；
5. 写入 `WORKER_RECOVERED_STALE_RUNNING` 审计事件。

recover 不能删除成功产物，也不能把未知结果当作 AI 已返回或图片已成功；send 后 commit 前的 hard crash 最多可留下 frozen concurrency 数量的 unknown outcomes，startup 不自动 resend。run/stage 只有在其 item 结果汇总、没有 DB work 且没有 live futures 后才由 WebUI/Worker 事务更新。

### 6.5 provider 配置冻结

`AI_ANNOTATE` 和在线 query lane 使用同一个已启用的 protocol-neutral `OpenAIProvider`、`adapter` 和 `model_id`。release candidate 的 `manifest.json` 必须冻结以下非秘密 provider snapshot 引用和版本：`adapter`、`profile_id`、`model_id`、`base_url_stable_id`、`secret_reference`、`prompt_version`、各 wire/record Schema version、`search_ranking_version`。MCP 只能从 `manifest.json` 读取这些值，并按 `secret_reference` 从 Keyring 或允许的环境变量读取秘密；MCP **MUST NOT** 读取 workspace 数据库、可变 provider profile 或缓存；不得跨协议 fallback。compatible `base_url` 仍属于所选 OpenAI 协议 endpoint；能力探测必须按所选 adapter 通过，但不证明远端 retention。既有 `openai_responses` release 不迁移、不改写。

### 6.6 Import state and workspace reconciliation

`state.json` 的新增时间、validator progress 和 workspace association 是 check 的最小 durable handoff，不改变 `workspace.v1.sql` 或任一 SQLite 表。`workspace.status=creating` 表示 reservation 已经写入但最终 workspace 尚未验证；`created` 只有在目标 `work.sqlite3` 存在、可打开、版本/export/manifest hash 关联一致且最终目录不是 staging 后才能写入。UI 只有 `created` 才能进入 `/runs/{run_id}`。

同一 passed check 的 import 请求必须在 export lock 内完成“检查 association → 保留已有 reservation 或建立 reservation → 写 `creating` → 构建/验证 workspace”的顺序。`creating` 的 duplicate 不得重新 copy、创建新 run 或调用 validator。重启 reconcile 必须优先检查同一 `run_id` 的最终 workspace；旧 state 缺少 association 时再按版本、export ID 和 manifest hash 扫描既有数据库。无效或不完整结果保留原 reservation 和稳定 `error_code`，显式 retry 仍复用原 ID；不得自动 resume source check 或隐式分配第二 run。

check 的 progress callback 只观察既有循环：snapshot 阶段接 copy/hash loop，validate 阶段接 validator 已有 inventory/Schema/JSONL/reference/render/checksum loop，finalize 阶段接既有 atomic state/snapshot finalize。callback 不能新增读取、扫描、hash、解码或第二份报告；callback 开关前后报告、PNG read/decode 计数和最终 hash 必须一致。state 写入节流但 phase/terminal 强制，subphase `completed` 单调递增，未知 `total` 保持 null/0。

## 7. 错误、恢复和重试语义

| 错误 | 默认状态 | 恢复动作 |
|---|---|---|
| `TOOLCHAIN_NOT_LOCKED` | `failed` | 完成 R0 锁定后新建 run |
| `EXPORT_CONTRACT_INVALID` | `failed` | 修复导出包/导出器，重新导出 |
| `REGISTRY_INCOMPLETE` | `failed` | 不允许跳过，重新导出完整注册表 |
| `POLICY_INVALID` | `failed` | 修正策略并产生新输入签名 |
| `RENDER_*` | `needs_review` | 自动最多重试一次，随后人工审核或声明 skip |
| `SCHEMA_INVALID` | `needs_review` 高优先级 | 只允许一次修复尝试，之后人工覆盖或失败 |
| `PROVIDER_NETWORK_ERROR`、`PROVIDER_TIMEOUT`、`PROVIDER_RATE_LIMITED`、`PROVIDER_SERVER_ERROR` | high `needs_review` | 保留 evidence，继续下一个 approved batch；Provider retry 使用显式 child generation |
| `PROVIDER_SCHEMA_INVALID_REPAIRABLE`、`PROVIDER_SCHEMA_INVALID`、`PROVIDER_REQUEST_INVALID`、`PROVIDER_PAYLOAD_TOO_LARGE`、`PROVIDER_REFUSAL`、`PROVIDER_INCOMPLETE` | high `needs_review` | 不阻塞 AI_ANNOTATE drain，进入 `VALIDATE`/`HUMAN_REVIEW` |
| `PROVIDER_OUTPUT_ID_MISMATCH`、`PROVIDER_MACHINE_FACT_CONFLICT`、`PROVIDER_UNKNOWN`、`PROVIDER_STORAGE_UNSUPPORTED` | high `needs_review` | `PROVIDER_STORAGE_UNSUPPORTED` 按 unknown 处理；保留原始 evidence |
| `PROVIDER_CACHE_KEY_INVALID`、`IDEMPOTENCY_CONFLICT` | high `needs_review` | Worker-local failure；禁止覆盖或伪造成功，继续非 fatal drain |
| `PROVIDER_NOT_CONFIGURED`、`PROVIDER_CONFIG_INVALID`、`PROVIDER_CAPABILITY_MISSING`、`PROVIDER_AUTH_FAILED`、`PROVIDER_PERMISSION_DENIED`、`PROVIDER_MODEL_UNAVAILABLE` | `failed` | 同一 transaction 写 request evidence/review/job/stage/run/audit，并停止 later sends |
| `PROVIDER_CANCELLED` | control | 停止 future sends，不属于 bulk retry；不把取消伪装成 provider failure |
| `FTS_BUILD_FAILED` | `failed` | 修复数据库/Schema 后重建发布候选 |
| `MCP_SMOKE_FAILED` | `failed` | 不切换 current，修复 release 候选 |
| `PUBLISH_ATOMIC_REPLACE_FAILED` | `failed` | 保留旧 current，修复后重试切换 |

每个外部模型 request 最多一次总自动重试；人工/Provider retry generation 必须以 source lineage 和 audit 明确区分，不能把同一 source 重跑到两次之外。provider 超时、未知响应或进程崩溃不能默认视为成功，必须校验响应 Schema、缓存和 hash 后决定。fatal provider/config/auth/capability failure 必须停止 later sends；item-local provider failure 则继续当前 approved plan 的顺序 drain。

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

`index.sqlite3` 是为只读 MCP 构建的发布投影，包含 FTS 索引和仅 `eligible`/满足条件的 `conditional` 视觉候选；可以保留用于详情查看的完整 Block/状态事实，但不得把 skipped 或 `excluded` 项伪装成可搜索图片。release 不包含原版资源。MCP 所需 provider snapshot 必须冻结在同目录 `manifest.json` 的 `release-manifest.v1` 中：`adapter`、`profile_id`、`model_id`、`base_url_stable_id`、不可逆 `secret_reference`、prompt/Schema/search 版本；MCP 不读 workspace provider profile，也不把该 snapshot 视为新的 active profile。`adapter` 只允许 `openai_responses` 或 `openai_chat_completions`，并决定唯一 wire codec。

`release.json` 使用 `release.v1`，只保存该 Schema 允许的 release identity、`manifest_sha256`、record schema versions、quality report ref 和 immutable 标记；它不承载 provider snapshot。同目录 `manifest.json` 使用独立的 `release-manifest.v1`，并且是 provider snapshot、功能输入/产物和 Schema inventory 引用的唯一位置。精确字段形状由 `schemas/workspace/` 下的真实 Schema 文件拥有，以下仅为说明性示例。`release-manifest.v1` 的顶层 `schema_version` 必须为 `release-manifest.v1`，其功能哈希不得包含 `release.json`、`manifest.json`、`schemas.sha256` 或 `checksums.sha256`，避免自引用和循环；`release.json` 不保存自身或 checksum 的摘要：

```json
{
  "schema_version": "release.v1",
  "release_id": "rel_01J...",
  "minecraft_version": "26.2",
  "built_at": "2026-08-13T12:00:00Z",
  "source_export_id": "export_20260814T165501Z",
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
  "quality_report_path": "quality_report.json",
  "immutable": true
}
```

release 创建成功后目录内容和数据库权限/应用层均视为只读。所有普通文件必须复制到 staging 后逐文件计算 `sha256:<64 lowercase hex>`，不得使用 symlink/hardlink；完成 flush/fsync 后原子 rename 为 release ID，并由 `release.json` 的 `immutable: true` 记录不可变语义，不得额外写入契约外的 marker 文件。`manifest.json` 记录 provider snapshot、功能输入/产物和 Schema inventory，不记录自身、`release.json` 或 `checksums.sha256` hash；`release.json` 保存其 Schema 允许的 identity 字段和 `manifest_sha256`，而 `checksums.sha256` 独立列出并校验 release 内其它普通文件。`schemas.sha256` 是按 schema-id UTF-8 字节序排序的 Schema inventory，每行严格为 `<64hex><two spaces><schema-id><two spaces><canonical-repository-relative-posix-path>\n`，路径必须是仓库规范相对 POSIX 路径且不声称位于 release；它和 `checksums.sha256` 都不被自身或 release metadata 反向哈希。任何修订都新建 `release_id`，即使只改变 Schema、semantic constraints、prompt、模型、图片或人工覆盖也不能更新旧 release。

`manifest.json` 示例至少包含：

```json
{
  "schema_version": "release-manifest.v1",
  "release_id": "rel_01J...",
  "minecraft_version": "26.2",
  "source_export_id": "export_20260814T165501Z",
  "source_export_manifest_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "toolchain_lock_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "schemas_inventory_path": "schemas.sha256",
  "provider_snapshot": {
    "adapter": "openai_responses",
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
10. 同一 export 的并发 opaque refs 只产生一个 active check 和一次 validator；anchor 未变的 passed check 复用原 check，manifest 或 checksum anchor 改变时建立新 check。
11. 首页/目录 listing 的 Recent Checks 与 chooser marker 可从 `state.json` 重建；最多 5 个不同 export，active first，且没有第二持久索引、绝对路径或 chooser token。
12. 同一 passed check 的并发 import 只保留一个 `run_id` 和一个最终 `work.sqlite3`；`creating` duplicate 返回同一 run，重启能 reconcile 有效 workspace 或保留原 reservation 后稳定失败/重试。
13. progress snapshots 在每个 validator subphase 内单调递增；启用/关闭 observational callback 的 validator report、PNG reads/decodes 和 hash 相同，SSE 不逐 item 广播。

最终的导出字段和图片完整性由 [导出契约](export-contract.md) 验收，分层/来源由 [数据与 Schema](data-and-schemas.md) 验收；三者任何一项不一致都应阻断 release。
