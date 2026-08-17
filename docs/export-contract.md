# 导出契约（Block Index Export Contract）

## 1. 范围、规范词和关联文档

本文定义 `Block Index Exporter` 产生的可重放导出包。导出器是运行在 Minecraft 客户端中的 Fabric client mod，并且是代表状态选择与 Minecraft 渲染的唯一执行者。导出器在自身进程内按 `EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS` 顺序完成注册表导出、代表变体选择和渲染；它 **MUST NOT** 调用 LLM、WebUI、MCP、SQLite 或其它工作库。Python Studio 只能导入并验证导出结果，不能在导出包外重新选择代表状态或重新渲染。

本文中的 `MUST`、`MUST NOT`、`SHOULD`、`MAY` 使用 RFC 2119 语义。等价实现只有在按 [`decisions.md`](decisions.md) 记录影响、回归范围并取得项目所有者明确批准后才可以替换默认实现；不得静默偏离本契约。R1 的最小实现边界见 [`decisions.md`](decisions.md) 的“R1 最小化影响记录”。

关联文档：

- [状态策略与渲染](state-policy-and-rendering.md)
- [数据与 Schema](data-and-schemas.md)
- [流水线、存储与发布](pipeline-storage-and-publishing.md)
- [项目路线图](roadmap.md)
- [冻结决策](decisions.md)
- [OpenAI Responses 提供商接口](openai-provider.md)
- [质量与测试接口](quality-and-testing.md)
- [安全与分发接口](security-and-distribution.md)

## 2. 契约版本和固定工具链

导出包根级字段 `export_contract_version` 固定为 `export-contract.v1`；它是契约版本，不是某个记录 Schema 的别名。导出业务记录使用全局唯一且用途分离的 Schema ID：`export-manifest.v1`、`export-block.v1`、`export-state.v1`、`export-variant.v1`、`export-failure.v1` 和 `render-metadata.v1`。各业务 JSONL 记录必须携带自己的 `schema_version`；`exporter.log` 是不进入业务 Schema 命名空间的诊断事件流。精确字段形状唯一由 `schemas/exporter/` 下的真实 Schema 文件拥有；本文件的示例只说明 exporter 行为。Schema 使用 JSON Schema Draft 2020-12，严格对象必须设置 `additionalProperties: false`。Schema、状态策略和渲染策略变化时必须重建导出包，MVP **MUST NOT** 使用通用数据转换机制把旧数据伪装成新版本。

Fabric 客户端导出器的构建和运行环境必须完全固定如下；这些值既进入构建配置，也进入 `manifest.json`：

| 字段 | 固定值 |
|---|---|
| `minecraft_edition` | `Java` |
| `minecraft_version` | `26.2` |
| `java_version` | `25` |
| `fabric_loader_version` | `0.19.3` |
| `fabric_api_version` | `0.157.0+26.2` |
| `loom_version` | `1.17.19` |
| `gradle_version` | `9.5.1` |
| `mappings` | `Minecraft 26.2 native Mojang names (unobfuscated); no external mappings artifact` |

构建和 manifest **MUST NOT** 使用 `latest`、版本范围、动态依赖或未解析映射。任一固定值变化都必须执行代表性回归集和全量导出；发布门按精确字符串比较，不接受“兼容版本”。Python 和 SDK 不属于导出器运行时；R0 只锁定实际引入的 tooling 依赖，后续依赖在使用前精确/hash 锁定并按 [流水线、存储与发布](pipeline-storage-and-publishing.md) 重新验证。

客户端导出命令使用 `ClientCommandRegistrationCallback` + `ClientCommands` 注册；实现约束不使用已不存在的旧 `BlockRenderDispatcher` API。

## 3. 导出包目录、编码和资产边界

导出包必须具有以下结构；文件名和大小写固定，manifest、日志和校验文件中的路径统一使用 `/`：

```text
<data_root>/exports/<minecraft_version>/<export_id>/
├── manifest.json
├── blocks.jsonl
├── states.jsonl
├── variants.jsonl
├── failures.jsonl
├── checksums.sha256
├── exporter.log
└── renders/
    └── <namespace>/<block path>/
        ├── preview.png
        ├── mask.png
        └── render.json
```

每个 `selected` 变体必须有 `preview.png`、`mask.png` 和 `render.json`；跳过项可以没有图片，但必须有机器可读原因。所有文本文件必须是 UTF-8、LF、无 BOM；JSONL 每行一个 JSON 对象，末尾必须有 LF，空行和 JSON 数组均非法。JSON 字段顺序不影响语义，但规范化哈希遵循第 11 节。

### 3.1 `export_id`、目录名和提交

`export_id` 必须等于最终导出目录名，并严格匹配：

```text
^export_[0-9]{8}T[0-9]{6}Z(?:_(?:0[1-9]|[1-9][0-9]))?$
```

默认值是 UTC 秒级的 `export_YYYYMMDDTHHMMSSZ`；仅当同一秒的目录已存在时，才按顺序使用 `_01` 至 `_99`。不得使用随机 UUID、32 位十六进制值或另一个内部导出身份。写入目录固定为 `.<export_id>.staging`，成功后一次原子 rename 为 `<export_id>`；消费者只接受非 staging 且目录名等于 manifest `export_id` 的包。冲突超过 `_99` 或 rename 失败时导出失败，不覆盖既有目录。

### 3.2 `variant_id` 和 render 路径

R1 每个 block 只有一个 default representative，因此 `variant_id == block_id`。render 路径直接由已登记的 `block_id` 推导：

```text
minecraft:stone   → renders/minecraft/stone/{preview.png,mask.png,render.json}
minecraft:foo/bar → renders/minecraft/foo/bar/{preview.png,mask.png,render.json}
```

推导后的每个路径 segment 必须拒绝空值、`.`、`..`、尾点和 Windows 设备名；规范化后仍必须位于 `renders/` 下。不得使用 sanitizer、slug registry、映射文件或兼容层。unsafe `block_id` 仍保留 Block/State 机器事实，并写 machine skip；旧本地导出直接删除或忽略后重导，不迁移旧身份或旧路径。

导出器 **MUST NOT** 把原版 Minecraft JAR、资源包、纹理、模型、字体、声音或其副本写入导出包。图片只能由用户本地合法安装的运行时生成；导出包只保存项目机器元数据、渲染产物、机器事实和哈希。公开仓库只提交 fixture 生成器源码；fixture 生成物、真实导出和真实图片必须在运行时本地生成。

### 3.3 Targeted banner repair export

Fabric additionally provides the exact in-game operation `/blockindex export banner-repair <base_export_id>`. It accepts no arbitrary target filter. Its target set is the stable sorted derivation of the 16 vanilla dye colors `black, blue, brown, cyan, gray, green, light_blue, light_gray, lime, magenta, orange, pink, purple, red, white, yellow` crossed with `*_banner` and `*_wall_banner`, yielding exactly 32 `minecraft:` block IDs.

The operation MUST first verify that `base_export_id` names a complete valid export. It then reuses unchanged non-target records and render artifacts, rerenders exactly the 32 target variants in Minecraft, and emits a new ordinary complete package at `exports/26.2/<new_export_id>/`. The new package MUST rewrite export lineage, counts, manifest, logical/render signatures and `checksums.sha256`, and MUST pass the existing exporter commit and external validator/check flow. A partial package, overlay package or new Schema ID is invalid.

## 4. `manifest.json`

### 4.1 必填结构

`manifest.json` 是导出包的唯一入口；其精确字段、required 集合、枚举和嵌套对象由 [`schemas/exporter/export-manifest.v1.json`](../schemas/exporter/export-manifest.v1.json) 唯一拥有。manifest 必须记录完整工具链、运行时 effective resource snapshot、逻辑输入签名、渲染输入签名、平台/渲染环境、六份 exporter Schema inventory、注册表 scope、记录计数、文件清单和完整性算法；严格版本不得添加未定义字段。

实际 `registry_blocks` 不得硬编码，必须等于运行时 `Registry.BLOCK` 中命名空间为 `minecraft` 的登记项数量。`registry_snapshot_sha256` 是按字典序排列的完整 `block_id` 列表的 SHA-256，用于阻止部分注册表导出。

`schema_inventory` 必须恰好列出本导出包使用的 exporter JSON Schema，按 `schema_id` 的 UTF-8 字节序排序；每个 `repository_path` 必须是仓库根相对 POSIX 路径，且每个 `schema_sha256` 使用 `sha256:<64 lowercase hex>`。该 inventory 不包含自身、导出 manifest 或 release metadata 的摘要，避免哈希循环；release 构建另由 `schemas.sha256` 记录全量 Schema inventory。

`export_id` 是本次写入实例的唯一 ID，且必须遵守第 3.1 节的目录名规则。`logical_input_signature` 只包含逻辑机器输入；其固定 framing 在 `dedupe_policy_version` 之后、resource snapshot 与 registry hashes 之前依次写入 exact token `pre-render-skip.v1;reason=BLOCK_ENTITY_FIXTURE_UNSUPPORTED;ids=minecraft:end_gateway,minecraft:end_portal` 和 exact token `banner-camera.v2;namespace=minecraft;types=BannerBlock,WallBannerBlock;colors=black,blue,brown,cyan,gray,green,light_blue,light_gray,lime,magenta,orange,pink,purple,red,white,yellow;forms=banner,wall_banner`。两个 token 表示 logical selection，不是 graphics environment，**不得**写入 `renderer_options`；任何会影响输出的 selection policy 变化都必须修改相应 exact token。`render_input_signature` 还必须包含 OS、GPU、驱动、渲染后端、分辨率和完整渲染环境哈希，并因该 logical input 变化而 transitively 改变；二者均只在 manifest 保留。历史 replacement exports `055316`/`060151` 使用旧 logical/render signatures；后续 replacement/corrected export 必须具有不同的 `logical_input_signature` 和 `render_input_signature`。实际 renderer options/environment identity 还必须覆盖影响确定性的控制，包括 resolver 固定 seed `42L`、atlas reload/freeze 控制、banner camera correction 和其它实际启用的动画控制；banner correction 同时改变 camera hash。相同完整渲染环境下的相同 `render_input_signature` 必须得到相同 PNG byte hash；不同 GPU/驱动或 OS 之间只要求 canonical 机器字段一致，不承诺 PNG byte hash 一致。manifest 还必须保留 registry、resource、Schema 和 render-environment 证据；`exporter_version` 必须是实际模组版本，不得用工作区版本或用户可变文本代替。

R1 在导出前必须对活动 `ResourceManager` 检查影响范围内资源的完整 resource stacks/contributions：`minecraft` namespace 下路径前缀为 `blockstates/`、`models/`、`textures/`，以及精确的 `lang/en_us.json`、`lang/zh_cn.json`。必须证明这些 identifier 仅来自默认 vanilla/builtin pack；任何非默认 vanilla/builtin pack 或用户/第三方 pack 对其有 contribution/override，都必须 export failed 并提示禁用，不能仍声明 `resource_pack_id: "vanilla"`。对会合并 stack 的 `blockstates/` 和 lang 资源，必须检查完整 stack，不能只读取 winner 后忽略其他来源。该硬门通过后，`runtime.resource_pack_sha256` 才能按实际有效内容计算；它不是 pack ID/version 哈希，也不复制资源进导出包。有效 snapshot 按规范 `namespace:path` resource identifier 的 UTF-8 字节序排序，对每项依次送入 SHA-256：identifier UTF-8 bytes、NUL 分隔符、8-byte big-endian 原始内容 byte count、原始内容 bytes。任一 stack/contribution 或内容读取失败即 export failed，不得伪造 hash。

### 4.2 manifest 状态

`status` 只能是：

- `succeeded`：注册表和所有合法状态完整，所有失败均已解决或没有失败，所有选定变体都有可读渲染。
- `needs_review`：结构完整，但存在待人工确认的渲染跳过、失败或其它审核记录。它可以导入工作库，但 **MUST NOT** 直接发布。
- `failed`：注册表/合法状态不完整、契约文件缺失、校验失败、写入中断或发生无法定位到完整逻辑项的错误。该包只能诊断，不能进入后续发布。

manifest 计数必须由实际记录行计算：`registry_blocks = runtime minecraft registry count`；`block_records = blocks.jsonl` 行数；`state_records = states.jsonl` 行数；`selected_variant_records = variants.jsonl` 中 `status: "selected"` 行数；`skipped_variant_records = variants.jsonl` 中 `status: "skipped"` 行数；`failure_records = failures.jsonl` 全部行数；`pending_review_records = failures.jsonl` 中 `review_status: "pending"` 行数。variants skipped 的 pending 只通过对应 failure 行计入，不重复计数。结构、注册表、Schema 或 checksum 不完整优先为 `failed`；否则 `pending_review_records > 0` 为 `needs_review`；否则为 `succeeded`。

一个方块可以没有可发布图片，但必须在 `failures.jsonl` 有 `kind: "skip"` 的机器可读原因；该 pending 结果由 R3 workspace `skip-review.v1` 审核后才能让 candidate 流水线继续。导出器不得删除 Block 记录来隐藏渲染失败。

## 5. `blocks.jsonl`

目标版本的每个 `minecraft` 注册表方块必须恰好有一行 `export-block.v1`。禁止重复 `block_id`，禁止出现非 `minecraft:` 命名空间；精确字段、required 集合和嵌套机器事实结构由 [`schemas/exporter/export-block.v1.json`](../schemas/exporter/export-block.v1.json) 唯一拥有。

`properties` 必须包含运行时 `StateManager` 的全部属性及全部合法值，不能只放策略挑选值。属性名在 JSON 对象中无序；其规范化序列化和 `default_state_id` 格式由 [数据与 Schema](data-and-schemas.md) 固定，不能把 JVM/Map 迭代顺序当作契约。官方名称缺失只能写 `null` 并记录日志，不得让 LLM 补齐后写进本字段。

## 6. `states.jsonl`

每个合法 `BlockState` 必须恰好有一行 `export-state.v1`，所以 `state_records` 是所有目标 `minecraft` 方块状态数之和，而不是视觉变体数。`state_id` 就是 canonical state string，不能另用 hash 替代。状态字符串生成规则见 [数据与 Schema](data-and-schemas.md)；精确字段、required 集合和条件结构以 [`schemas/exporter/export-state.v1.json`](../schemas/exporter/export-state.v1.json) 为准。R1 的 `states.jsonl` 必须完整列出运行时合法状态；可渲染时 `mapping_status: "mapped"` 的 `variant_ids` 指向同一 block 的唯一 default representative，不可稳定渲染时使用 `mapping_status: "skipped"`、空 `variant_ids` 和对应 exporter failure。

`variant_ids` 由 exporter 的 `SELECT_VARIANTS`/`RENDER_VARIANTS` 阶段写入；每个引用必须在 `variants.jsonl` 存在且属于同一个 `block_id`。`legal_state` 永远由运行时产生，LLM 和人工覆盖都不能修改。

行为字段的布尔事实只能为 JSON `true`、`false` 或字符串 `"unknown"`，不能用 `null`、空字符串或猜测替代。`unknown` 不满足硬约束；例如查询要求“必须可穿过”时只接受 `passable: true`，`unknown` 必须排除或进入人工选择。

## 7. `variants.jsonl`

每行是 exporter 选出的一个 block-level visual representative 或一个机器跳过候选。R1 每个 block 只选择唯一 default `BlockState` 作为普通视觉代表；所有合法状态仍在 `states.jsonl` 中，并链接到同一 block 的代表或对应 skip。变体是 `Block` 下的实体，绝不跨 `block_id` 合并。Python Studio 只能验证这些结果，不能重选或重渲染。精确字段、required 集合和 selected/skipped 条件以 [`schemas/exporter/export-variant.v1.json`](../schemas/exporter/export-variant.v1.json) 为准。

`status`、selected/skipped 所需字段、初始资格枚举、机器 source 和 render 引用均以 `export-variant.v1` 为准。R1 selected 只表示该 block 的 default representative 在 isolated context 中成功渲染；skipped 只表示机器结果并保持 pending。AI 不得修改机器字段，人工资格或 skip 审核只能通过独立 workspace Schema 完成。

R1 的 `variant_id` 直接等于已登记的 `block_id`，不由哈希、随机值或渲染签名生成；同一 block 的唯一 default representative 重跑仍使用同一 ID。图片短编号只用于渲染卡，不是机器主键。R1 不跨 block 合并代表，也不执行 pHash、IoU、alpha 或通用去重规则。

selected variant 的 render reference 只保留 `preview`、`mask`、`render metadata` 三个文件路径及其 SHA-256；这些摘要只证明文件内容完整，不参与 `variant_id` 或路径生成。

`candidate_qualification` 的精确枚举和字段关系以 `export-variant.v1` 为准。R1 只保留 exporter 产生的机器值；LLM 不设置资格，人工资格修改和 `excluded` 审核属于后续独立 workspace/release Schema。跳过项不能作为发布搜索视觉候选。

## 8. `renders/` 资产契约

### 8.1 `preview.png`

每个 `selected` 变体必须有一张 512×512、RGBA、PNG 预览卡，四个固定视图按下列位置绘制：

```text
左上：等距视图（isometric）
右上：正视图（front）
左下：侧视图（side）
右下：顶视图（top）
```

相机、正交缩放、光照、背景、背板、支撑、游戏时间、天气、生物群系、资源快照和 isolated context 均由当前 `render.v2`/`fixture.v1` 定义；普通对象和未修改的历史记录继续使用 `camera.v1` 语义，精确 banner repair target 使用 `camera.v2` 的 banner-camera policy correction。未修改的历史 `render.v1` records、workspace/release data 在当前 v1 Schema ID 下保持 valid，并只在其 record/run context replay。preserved old export package 在 repository Schema bytes 变化后不由 current external validator 重新验证；其 embedded `schemas.sha256`/`schema_inventory` 是 binding evidence，current validation 必须报告 `SCHEMA_INVENTORY_HASH_MISMATCH`。不得 bypass hash、自动迁移、增加 historical Schema snapshot layer 或使用 version-aware validator fallback；旧 package bytes/reports 只作为历史证据保留。默认摄影棚使用 `minecraft:plains`、晴天、`world_time: 6000`、shader disabled 和固定中性背景；动态染色对象必须标记 `tint_sensitive: true` 与 `baseline_biome: minecraft:plains`。manifest 保存影响渲染的环境证据；`render.json` 只声明最小图片/视角/policy/fixture/tint/mask 语义，不重复 camera、lighting、background、backboard、support、resource、environment 或 render-input content hash。图片底部只允许短编号，不得把完整 ID 绘制成小字。

R1 的透明、附着和连接方块只使用固定 isolated context、背板或必要中性支撑；不预建组合邻接。一个或多个透明 edge-on quadrant 允许存在，只要 composite 非空；整个 composite 全透明仍失败，`nether_portal` 在 composite 非空时保留。支撑物不得进入对象蒙版；`mask.png` 是同尺寸单通道或 RGBA 对象蒙版，必须在 `render.json` 声明通道和阈值。

### 8.2 `render.json`

`render.json` 使用 `render-metadata.v1`；精确字段、required 集合、四视角顺序和 mask 对象结构以 [`schemas/exporter/render-metadata.v1.json`](../schemas/exporter/render-metadata.v1.json) 为准。R1 必须写入 512×512 RGBA、四视角、isolated fixture、最小 policy/fixture/tint/mask 语义和可读取 mask 的机器元数据；不重复 manifest 已有的环境证据或图片内容哈希。

R1 普通路径只渲染普通 block model 的固定 isolated context；D-045 的精确 `banner-repair` path 例外地调用既有 vanilla banner special renderer，并不因此预建通用 fixture。两条路径都不预建 block entity/NBT、任意流体、动画帧、组合邻接或通用 fixture 框架。`minecraft:end_portal` 与 `minecraft:end_gateway` 是 non-building 的 explicit machine pending skips：两个精确 block ID 及其全部合法 states 仍登记，exporter 在进入渲染前使用既有 `BLOCK_ENTITY_FIXTURE_UNSUPPORTED` 写入 ordinary auditable pending skip，不生成 preview、mask 或 render directory；所有 states 仍在 `states.jsonl` 中以现有 skipped mapping（空 `variant_ids`）保留，variant/failure 记录沿用 ordinary auditable pending skip 并要求后续 human review，绝不静默过滤。无法在该 context 中稳定渲染即写机器可读 skip/failure 并保持 pending review，不生成占位图片。当前范围预期保留 `43` 个 block-entity fixture skips 和 `10` 个 invisible/technical skips；`melon_stem`、`pumpkin_stem`、`tripwire` 的 `OBJECT_TOO_SMALL` 仍 reviewable，不由本 amendment 重新分类；既有 `152` 个 rerender events 只保留为历史审计证据，不执行为本次修复。

### 8.3 渲染失败

per-variant render acceptance 在写入 `variants.jsonl` record 前必须检查 entire composite 全透明/全背景、对象出框、对象过小、resolved material identity 和截图文件缺失。单个 edge-on quadrant 透明不构成失败。Java resolved submission 是 missing-material authority，覆盖 whole-model/material missing、missing model、vanilla material/quads 和 Fabric mesh；Fabric mesh 使用 block-atlas missing sprite 的 `SpriteFinder` 与 missing-sprite UV bounds。`minecraft:missingno` 的 source checker 精确为四象限 `#F800F8`/`#000000`，但 rendered colors 不是 authority。Python 不得使用宽松全局 magenta/black ratios；外部 validator 只能以严格 canonical checker 作 defense-in-depth，ambiguous pixels 不是 proof。`retry_count` 是最多 1 次的上限，不要求必须重试；仅已观察的可恢复失败可重试一次。第二次尝试仍失败，或无需重试的失败，必须写 `failures.jsonl` 并进入 `needs_review`/`pending`，不得无限重试、静默删除或生成占位图片冒充成功。

## 9. `failures.jsonl` 和失败语义

每行使用 `export-failure.v1`，既记录错误也记录显式跳过；精确字段、required 集合、scope 引用条件和 evidence 结构以 [`schemas/exporter/export-failure.v1.json`](../schemas/exporter/export-failure.v1.json) 为准。R1 failure/skip 只表达机器结果和 pending review，不携带 workspace 人工审核字段，也不携带 render identity/signature。

`scope` 只能是 `export`、`block`、`state`、`variant`、`render`；除 `scope: export` 外，对应引用必须存在。`export-failure.v1` 的精确 `reason_code` 枚举由 [`schemas/exporter/export-failure.v1.json`](../schemas/exporter/export-failure.v1.json) 唯一拥有；实现只使用与实际观察失败相符的既有枚举，包括 `REGISTRY_INCOMPLETE`、`INVALID_STATE`、`MISSING_TEXTURE`、`EMPTY_RENDER`、`BACKGROUND_ONLY_RENDER`、`OBJECT_OFF_CANVAS`、`OBJECT_TOO_SMALL`、`BLOCK_ENTITY_FIXTURE_UNSUPPORTED`、`IO_ERROR`、`SCHEMA_INVALID`、`CHECKSUM_MISMATCH` 和 `EXPORTER_EXCEPTION`。本 amendment 不增加 reason code、Schema ID 或 allowlist framework。

`kind: "failure"` 表示逻辑项未成功完成；`kind: "skip"` 表示明确不把该项作为可发布视觉候选。两者都不能缺少原因。R1 的 `action: "needs_review"`/`review_status: "pending"` 只表示 exporter failure/skip 待后续 workspace 审核；R1 不写人工 `skip-review.v1`。R3 candidate-build 前必须由独立 workspace `skip-review.v1` 解决无变体项；`excluded` 资格必须另有独立 `qualification-review.v1`，不能用 exporter failure 或 skip 记录替代。方块若没有可发布变体，必须保留 Block/State 和机器原因，否则不是覆盖而是导出失败。

错误不得通过伪造 `block_id`、合法状态、默认状态或图片恢复。R1 不实现逐逻辑项恢复游标或复杂幂等冲突体系；失败保留 staging 诊断，新的导出使用新的 `export_id`。

## 10. `exporter.log`

`exporter.log` 是诊断事件流，不是业务记录 Schema。它应记录三个冻结阶段的开始/结束、Schema/校验失败、实际发生的重试、skip/failure 和最终状态，不得写 API key、完整密钥、秘密或本地用户隐私；不要求预建游标、恢复或通用事件框架。

## 11. 校验和、规范化和文件完整性

JSON、manifest、日志事件和 metadata 中的摘要值统一使用 `sha256:<64 lowercase hex>`。`checksums.sha256` 是 UTF-8 文本，每行格式为：

```text
<64 个小写十六进制字符>  <相对路径>
```

它必须列出 release/export 目录中除自身外的全部普通文件，按路径 UTF-8 字节序升序；每行必须是 `<lowercase hex><两个 ASCII 空格><POSIX 相对路径>\n`，行首哈希不带 `sha256:` 前缀。不列目录、symlink 或 hardlink；重复路径、绝对路径、反斜杠和 `..` 均非法。哈希算法固定为 SHA-256。导出 manifest 只记录功能输入/产物 hash，不记录自身、release pointer 或 checksum 文件 hash，避免循环；release 的 `release.json` 只记录 manifest hash，checksums 文件自身不再由 release metadata 反向引用。校验后禁止修改任何文件。

JSON 对象哈希使用 JCS-RFC8785；JSONL 记录哈希使用规范化对象字节加一个 LF；PNG、日志和校验文件使用原始字节。写临时文件、完成 fsync、校验通过后才能提交导出目录。

## 12. 最小提交

### 12.1 最小 staging 和提交

R1 只要求一次 fresh staging 导出：写入 `.<export_id>.staging`，完成全部 JSONL、渲染文件、manifest 和 `checksums.sha256`。exporter commit gate 只检查最终引用/计数/状态、精确 render 路径与文件集、PNG 基础可读性和尺寸，生成 checksum，完成 fsync 后一次原子 rename 为 `<export_id>`；它不在 commit 前再次对全包逐记录跑完整 Schema，也不再次复算刚生成的 checksum。失败或不完整 staging 不得被消费者接受；新的导出使用新的 `export_id`。

外部 Python validator 只对非 staging 且目录名等于 manifest `export_id` 的包执行一次 strict Schema、跨记录/registry 关系、资源黑名单、PNG 语义/质量、一次 checksum 与 artifact digest 复算，并复用同一次文件读取/PNG 解码。exporter gate 和 validator 不做相同全量检查两遍；任一失败不得宣称 R1 验收通过。真实 validator 在 1000 renders 上曾超过 600 秒，验收必须采用单次读取/解码，不延长 timeout、不增加并行框架或磁盘缓存。

## 13. 导出包验收条件

导出包可以交给工作库导入，当且仅当外部 validator 对最终包完成以下检查且结果明确；exporter commit gate 不重复这些全量检查：

1. manifest 的固定工具链、精确平台/渲染环境、`minecraft_version`、策略版本和契约版本完整。
2. `blocks.jsonl` 恰好覆盖运行时 `minecraft` 注册表，覆盖率 100%，无虚假 ID。
3. `states.jsonl` 覆盖每个合法状态，`legal_state` 均由运行时确认，默认状态引用合法。
4. 每个状态有同一 block 的 default representative 映射，或有与之对应的 exporter 机器可读 skip/failure 并保持待审核。
5. 每个 `selected` 变体的代表状态、状态集合、行为事实、图片和 `render.json` 互相一致；不得跨 block ID 合并。
6. 每张 `preview.png` 和 `mask.png` 可读取、为 512×512，四视角和固定策略元数据齐全；透明 edge-on quadrant 允许存在但整个 composite 全透明必须拒绝；变体 render reference 的三个 artifact SHA-256 与实际文件一致。
7. 所有 JSONL 行通过各自严格 Schema，`failures.jsonl` 的引用和枚举合法；exporter 的状态/变体选择记录完整。
8. `checksums.sha256` 按 UTF-8 字节序覆盖所有必需普通文件并完成一次复算；JSON/metadata 摘要采用 `sha256:` 前缀格式，checksum 行首使用无前缀的 64 位小写十六进制格式。
9. 日志包含结束事件，manifest 状态与失败/审核计数一致。
10. 包内没有原版 JAR、资源包、纹理、模型、字体或声音。

这些是导出包门槛，不等同于发布门槛；发布还必须通过 [流水线、存储与发布](pipeline-storage-and-publishing.md) 中的 AI、审核、FTS、MCP 和原子切换门禁。
