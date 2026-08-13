# 导出契约（Block Index Export Contract）

## 1. 范围、规范词和关联文档

本文定义 `Block Index Exporter` 产生的可重放导出包。导出器是运行在 Minecraft 客户端中的 Fabric client mod，并且是代表状态选择与 Minecraft 渲染的唯一执行者。导出器在自身进程内按 `EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS` 顺序完成注册表导出、代表变体选择和渲染；它 **MUST NOT** 调用 LLM、WebUI、MCP、SQLite 或其它工作库。Python Studio 只能导入并验证导出结果，不能在导出包外重新选择代表状态或重新渲染。

本文中的 `MUST`、`MUST NOT`、`SHOULD`、`MAY` 使用 RFC 2119 语义。等价实现只有在按 [`decisions.md`](decisions.md) 记录影响、回归范围并取得项目所有者明确批准后才可以替换默认实现；不得静默偏离本契约。

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
| `mappings` | `native_mojang_names_unobfuscated_no_external_artifact` |

构建和 manifest **MUST NOT** 使用 `latest`、版本范围、动态依赖或未解析映射。任一固定值变化都必须执行代表性回归集和全量导出；发布门按精确字符串比较，不接受“兼容版本”。Python 和 SDK 不属于导出器运行时；R0 只锁定实际引入的 tooling 依赖，后续依赖在使用前精确/hash 锁定并按 [流水线、存储与发布](pipeline-storage-and-publishing.md) 重新验证。

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
    └── <variant_id>/
        ├── preview.png
        ├── mask.png
        └── render.json
```

每个 `selected` 变体必须有 `preview.png`、`mask.png` 和 `render.json`；跳过项可以没有图片，但必须有机器可读原因。所有文本文件必须是 UTF-8、LF、无 BOM；JSONL 每行一个 JSON 对象，末尾必须有 LF，空行和 JSON 数组均非法。JSON 字段顺序不影响语义，但规范化哈希遵循第 11 节。

导出器 **MUST NOT** 把原版 Minecraft JAR、资源包、纹理、模型、字体、声音或其副本写入导出包。图片只能由用户本地合法安装的运行时生成；导出包只保存项目机器元数据、渲染产物、机器事实和哈希。公开仓库只提交 fixture 生成器源码；fixture 生成物、真实导出和真实图片必须在运行时本地生成。

## 4. `manifest.json`

### 4.1 必填结构

`manifest.json` 是导出包的唯一入口。`export-manifest.v1` 的必填结构如下；严格版本不得添加未定义字段：

```json
{
  "schema_version": "export-manifest.v1",
  "export_contract_version": "export-contract.v1",
  "export_id": "exp_01J...",
  "export_key": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "status": "succeeded",
  "created_at": "2026-08-13T10:00:00Z",
  "completed_at": "2026-08-13T10:34:12Z",
  "toolchain": {
    "minecraft_edition": "Java",
    "minecraft_version": "26.2",
    "java_version": "25",
    "fabric_loader_version": "0.19.3",
    "fabric_api_version": "0.157.0+26.2",
    "loom_version": "1.17.19",
    "gradle_version": "9.5.1",
    "mappings": "native_mojang_names_unobfuscated_no_external_artifact",
    "exporter_mod_id": "blockpedia-exporter",
    "exporter_version": "1.0.0"
  },
  "runtime": {
    "resource_pack_id": "vanilla",
    "resource_pack_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "language_primary": "zh_cn",
    "language_secondary": "en_us",
    "shader": "disabled",
    "world_fixture_version": "fixture.v1",
    "biome": "minecraft:plains",
    "weather": "clear",
    "world_time": 6000,
    "fov": 70,
    "gui_scale": 2,
    "render_distance": 8
  },
  "platform": {
    "os_name": "Windows",
    "os_version": "11",
    "architecture": "x86_64",
    "gpu_vendor": "local-runtime",
    "gpu_model": "local-runtime",
    "driver_version": "local-runtime",
    "render_backend": "local-runtime",
    "framebuffer_resolution": "512x512",
    "render_environment_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "render_environment": {
    "camera_policy_version": "camera.v1",
    "camera_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "lighting_policy_version": "lighting.v1",
    "lighting_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "background_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "backboard_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "support_fixture_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "policies": {
    "state_policy_version": "state-policy.v1",
    "render_policy_version": "render.v1",
    "fixture_policy_version": "fixture.v1",
    "dedupe_policy_version": "dedupe.v1"
  },
  "schema_inventory": [
    {
      "schema_id": "export-block.v1",
      "schema_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "repository_path": "schemas/exporter/export-block.v1.json"
    },
    {
      "schema_id": "export-failure.v1",
      "schema_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "repository_path": "schemas/exporter/export-failure.v1.json"
    },
    {
      "schema_id": "export-manifest.v1",
      "schema_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "repository_path": "schemas/exporter/export-manifest.v1.json"
    },
    {
      "schema_id": "export-state.v1",
      "schema_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "repository_path": "schemas/exporter/export-state.v1.json"
    },
    {
      "schema_id": "export-variant.v1",
      "schema_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "repository_path": "schemas/exporter/export-variant.v1.json"
    },
    {
      "schema_id": "render-metadata.v1",
      "schema_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "repository_path": "schemas/exporter/render-metadata.v1.json"
    }
  ],
  "scope": {
    "namespace": "minecraft",
    "registry": "block",
    "registry_snapshot_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "counts": {
    "registry_blocks": 0,
    "block_records": 0,
    "state_records": 0,
    "selected_variant_records": 0,
    "skipped_variant_records": 0,
    "failure_records": 0,
    "pending_review_records": 0
  },
  "files": {
    "blocks.jsonl": {"required": true, "kind": "jsonl", "record_schema": "export-block.v1"},
    "states.jsonl": {"required": true, "kind": "jsonl", "record_schema": "export-state.v1"},
    "variants.jsonl": {"required": true, "kind": "jsonl", "record_schema": "export-variant.v1"},
    "failures.jsonl": {"required": true, "kind": "jsonl", "record_schema": "export-failure.v1"},
    "checksums.sha256": {"required": true, "kind": "checksum", "line_hash_format": "64-lowercase-hex-without-prefix"},
    "exporter.log": {"required": true, "kind": "log"},
    "renders/": {"required": true, "kind": "render_directory"}
  },
  "integrity": {
    "algorithm": "SHA-256",
    "checksum_file": "checksums.sha256",
    "canonical_json": "JCS-RFC8785",
    "jsonl_record_terminator": "LF"
  }
}
```

实际 `registry_blocks` 不得硬编码，必须等于运行时 `Registry.BLOCK` 中命名空间为 `minecraft` 的登记项数量。`registry_snapshot_sha256` 是按字典序排列的完整 `block_id` 列表的 SHA-256，用于阻止部分注册表导出。

`schema_inventory` 必须恰好列出本导出包使用的 exporter JSON Schema，按 `schema_id` 的 UTF-8 字节序排序；每个 `repository_path` 必须是仓库根相对 POSIX 路径，且每个 `schema_sha256` 使用 `sha256:<64 lowercase hex>`。该 inventory 不包含自身、导出 manifest 或 release metadata 的摘要，避免哈希循环；release 构建另由 `schemas.sha256` 记录全量 Schema inventory。

`export_id` 是本次写入实例的唯一 ID。`export_key` 是目标版本、固定工具链、导出器版本、资源包哈希、状态策略、渲染/夹具策略和导出 scope 的规范化摘要。`logical_input_signature` 只包含逻辑机器输入，`render_input_signature` 还必须包含 OS、GPU、驱动、渲染后端、分辨率和完整渲染环境哈希。相同完整渲染环境下的相同 `render_input_signature` 必须得到相同 PNG byte hash；不同 GPU/驱动或 OS 之间只要求 canonical 机器字段一致，不承诺 PNG byte hash 一致。`exporter_version` 必须是实际模组版本，不得用工作区版本或用户可变文本代替。

### 4.2 manifest 状态

`status` 只能是：

- `succeeded`：注册表和所有合法状态完整，所有失败均已解决或没有失败，所有选定变体都有可读渲染。
- `needs_review`：结构完整，但存在待人工确认的渲染跳过、失败或其它审核记录。它可以导入工作库，但 **MUST NOT** 直接发布。
- `failed`：注册表/合法状态不完整、契约文件缺失、校验失败、写入中断或发生无法定位到完整逻辑项的错误。该包只能诊断，不能进入后续发布。

一个方块可以没有可发布图片，但必须在 `failures.jsonl` 有 `kind: "skip"` 的机器可读原因，并在人工审核后才能让流水线继续。导出器不得删除 Block 记录来隐藏渲染失败。

## 5. `blocks.jsonl`

目标版本的每个 `minecraft` 注册表方块必须恰好有一行 `export-block.v1`。禁止重复 `block_id`，禁止出现非 `minecraft:` 命名空间。字段如下：

| 字段 | 类型/约束 | 说明 |
|---|---|---|
| `schema_version` | 固定 `export-block.v1` | Schema 版本 |
| `export_id` | 字符串 | 必须等于 manifest |
| `minecraft_version` | 固定 `26.2` | 不得由外部配置覆盖 |
| `block_id` | `minecraft:` ID | 注册表事实 |
| `translation_key` | 字符串 | 运行时翻译键 |
| `name_zh_cn` | 字符串或 `null` | 官方 `zh_cn`，不是 AI 名称 |
| `name_en_us` | 字符串或 `null` | 官方 `en_us`，不是 AI 名称 |
| `default_state_id` | 合法 `state_id` | 默认状态 |
| `properties` | 对象，可为空 | 属性名到合法字符串值数组的映射；数组值顺序由运行时固定，规范化状态字符串的属性名顺序见 canonical serializer |
| `has_item` | `true/false/unknown` | 机器事实 |
| `has_block_entity` | `true/false/unknown` | 机器事实 |
| `tags` | `minecraft:` ID 数组 | 精确版本运行时标签 |
| `behavior` | `BehaviorFacts` | 只放机器可测行为 |
| `source` | `runtime` 对象 | 模组、版本和采集时间 |

`properties` 必须包含运行时 `StateManager` 的全部属性及全部合法值，不能只放策略挑选值。属性名在 JSON 对象中无序；其规范化序列化和 `default_state_id` 格式由 [数据与 Schema](data-and-schemas.md) 固定，不能把 JVM/Map 迭代顺序当作契约。官方名称缺失只能写 `null` 并记录日志，不得让 LLM 补齐后写进本字段。

## 6. `states.jsonl`

每个合法 `BlockState` 必须恰好有一行 `export-state.v1`，所以 `state_records` 是所有目标 `minecraft` 方块状态数之和，而不是视觉变体数。`state_id` 就是 canonical state string，不能另用 hash 替代。状态字符串生成规则见 [数据与 Schema](data-and-schemas.md)，示例：

```json
{
  "schema_version": "export-state.v1",
  "export_id": "exp_01J...",
  "minecraft_version": "26.2",
  "state_id": "minecraft:bamboo_trapdoor[facing=north,half=bottom,open=false,powered=false,waterlogged=false]",
  "block_id": "minecraft:bamboo_trapdoor",
  "properties": {
    "facing": "north",
    "half": "bottom",
    "open": "false",
    "powered": "false",
    "waterlogged": "false"
  },
  "is_default": true,
  "legal_state": true,
  "shape": {"boxes": [], "signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000"},
  "collision": {"boxes": [], "signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000"},
  "behavior": {
    "transparent": false,
    "emissive": false,
    "passable": false,
    "waterloggable": true,
    "requires_support": "unknown",
    "support": {
      "below": true,
      "above": false,
      "north": "unknown",
      "south": "unknown",
      "east": "unknown",
      "west": "unknown",
      "none": false
    }
  },
  "variant_ids": ["vv_..."],
  "mapping_status": "mapped"
}
```

`variant_ids` 由 exporter 的 `SELECT_VARIANTS`/`RENDER_VARIANTS` 阶段写入，可以引用多个上下文变体；每个引用必须在 `variants.jsonl` 存在且属于同一个 `block_id`。没有可稳定渲染的状态必须使用 `mapping_status: "skipped"`、空 `variant_ids`，并在 `failures.jsonl` 写入同一 `state_id` 的 skip 记录。`legal_state` 永远由运行时产生，LLM 和人工覆盖都不能修改。

行为字段的布尔事实只能为 JSON `true`、`false` 或字符串 `"unknown"`，不能用 `null`、空字符串或猜测替代。`unknown` 不满足硬约束；例如查询要求“必须可穿过”时只接受 `passable: true`，`unknown` 必须排除或进入人工选择。

## 7. `variants.jsonl`

每行是 exporter 选出的一个稳定视觉变体或一个被明确跳过的候选。变体是 `Block` 下的实体，绝不跨 `block_id` 合并。Python Studio 只能验证这些结果，不能重选或重渲染。完整选择和合并算法见 [状态策略与渲染](state-policy-and-rendering.md)。

```json
{
  "schema_version": "export-variant.v1",
  "export_id": "exp_01J...",
  "minecraft_version": "26.2",
  "variant_id": "vv_7c5e...",
  "block_id": "minecraft:bamboo_trapdoor",
  "canonical_state_id": "minecraft:bamboo_trapdoor[facing=north,half=bottom,open=false,powered=false,waterlogged=false]",
  "represented_state_ids": [
    "minecraft:bamboo_trapdoor[facing=north,half=bottom,open=false,powered=false,waterlogged=false]"
  ],
  "context": {
    "fixture_id": "isolated",
    "fixture_version": "fixture.v1",
    "rotatable": true,
    "canonical_orientation": "north"
  },
  "selection": {
    "state_policy_version": "state-policy.v1",
    "reason": "default_and_closed_bottom",
    "protected_dimensions": ["half"]
  },
  "status": "selected",
  "candidate_qualification": "conditional",
  "warnings": ["requires_support_below"],
  "machine_facts": {
    "geometry_signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "collision_signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "behavior_fingerprint": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "geometry_class": ["horizontal_thin_sheet"]
  },
  "render": {
    "render_policy_version": "render.v1",
    "render_signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "render_input_signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "preview_path": "renders/vv_7c5e.../preview.png",
    "mask_path": "renders/vv_7c5e.../mask.png",
    "render_metadata_path": "renders/vv_7c5e.../render.json",
    "image_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "source": {"type": "machine", "exporter_version": "1.0.0", "stage": "SELECT_VARIANTS"}
}
```

`status` 只能是 `selected` 或 `skipped`。`selected` 必须有合法代表状态、完整状态集合、可读图片、蒙版和渲染元数据；`skipped` 必须有 `skip_reason_code`、`skip_reason` 和对应 `failures.jsonl` 记录，且没有可发布图片。`candidate_qualification` 是 exporter 根据机器事实、渲染结果和确定性规则产生的初始资格，只能是 `eligible`、`conditional`、`excluded`；`conditional` 必须有非空 warnings，`excluded` 必须由后续独立 `qualification-review.v1` 审核后才能进入 release 详情。`source.type` 必须为 `machine`。AI 不得修改这三个字段；人工资格修改只能通过独立 `qualification-review.v1`。合并状态必须完整列在 `represented_state_ids`，不能因此丢失属性或行为信息。代表状态必须是合法状态，默认状态在可渲染时必须至少由一个变体代表。

变体 ID 使用以下输入的规范化 SHA-256 前缀生成：`block_id`、`canonical_state_id`、`normalized_context`、`state_policy_version`、`render_policy_version`、`resource_pack_sha256`。同一输入重跑不能生成顺序依赖的 ID；图片短编号只用于渲染卡，不是机器主键。

变体的 `candidate_qualification` 必须使用冻结枚举 `eligible`、`conditional`、`excluded`，且 **MUST NOT** 使用 `ineligible` 或 `unknown` 作为资格值：

- `eligible`：图片、机器 Schema、状态和基本候选检查通过，无已知硬排除。
- `conditional`：可以使用但需要支撑、方向、邻接、含水等上下文，必须有非空 `warnings`。
- `excluded`：经人工审核不作为建筑候选，仍保留 Block、状态、机器事实和审计记录。

LLM **MUST NOT** 设置资格；确定性规则和人工声明式覆盖共同决定资格。跳过项不能作为发布搜索视觉候选。

## 8. `renders/` 资产契约

### 8.1 `preview.png`

每个 `selected` 变体必须有一张 512×512、RGBA、PNG 预览卡，四个固定视图按下列位置绘制：

```text
左上：等距视图（isometric）
右上：正视图（front）
左下：侧视图（side）
右下：顶视图（top）
```

相机、正交缩放、光照、背景、背板、支撑、游戏时间、天气、生物群系、资源包和固定夹具均由 `render.v1`/`fixture.v1` 定义。默认摄影棚使用 `minecraft:plains`、晴天、`world_time: 6000`、shader disabled 和固定中性背景；动态染色对象必须标记 `tint_sensitive: true` 与 `baseline_biome: minecraft:plains`。图片底部只允许短编号，不得把完整 ID 绘制成小字。

透明、附着、连接方块必须使用固定背板、标准邻接上下文或中性支撑；支撑物不得进入对象颜色、轮廓和特征区域。`mask.png` 是同尺寸单通道或 RGBA 对象蒙版，必须在 `render.json` 声明通道和阈值。

### 8.2 `render.json`

`render.json` 使用 `render-metadata.v1`，至少包含：

```json
{
  "schema_version": "render-metadata.v1",
  "variant_id": "vv_...",
  "width": 512,
  "height": 512,
  "format": "PNG-RGBA",
  "views": ["isometric", "front", "side", "top"],
  "camera_policy_version": "camera.v1",
  "camera_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "lighting_policy_version": "lighting.v1",
  "lighting_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "background_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "backboard_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "support_fixture_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "fixture_id": "isolated",
  "fixture_version": "fixture.v1",
  "render_environment_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "render_input_signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "resource_pack_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "tint_sensitive": false,
  "mask_present": true,
  "image_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
}
```

动态、流体和方块实体只使用版本化固定夹具代表；不得穷举任意 NBT、容器内容、旗帜图案或动画帧。夹具选择必须可通过 `fixture_id` 重放。

### 8.3 渲染失败

导出器必须检查全透明/全背景、对象出框、对象过小、紫黑缺失纹理、截图文件缺失和连续帧不一致。每个逻辑渲染项自动最多重试一次；第二次失败必须写 `failures.jsonl`，然后进入 `needs_review` 或由有权限的人工声明式规则明确 `skipped`。不得无限重试、静默删除或生成占位图片冒充成功。

## 9. `failures.jsonl` 和失败语义

每行使用 `export-failure.v1`，既记录错误也记录显式跳过：

```json
{
  "schema_version": "export-failure.v1",
  "export_id": "exp_01J...",
  "minecraft_version": "26.2",
  "failure_id": "fail_01J...",
  "kind": "skip",
  "stage": "RENDER_VARIANTS",
  "scope": "variant",
  "block_id": "minecraft:example",
  "state_id": null,
  "variant_id": "vv_...",
  "logical_key": "variant:vv_...",
  "reason_code": "ANIMATED_FIXTURE_UNSUPPORTED",
  "severity": "high",
  "retry_count": 1,
  "action": "needs_review",
  "review_status": "pending",
  "message": "在 fixture.v1 中无法得到稳定代表帧。",
  "evidence": {"frame_hashes": ["sha256:0000000000000000000000000000000000000000000000000000000000000000", "sha256:0000000000000000000000000000000000000000000000000000000000000000"]},
  "input_signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "created_at": "2026-08-13T10:20:00Z"
}
```

`scope` 只能是 `export`、`block`、`state`、`variant`、`render`；除 `scope: export` 外，对应引用必须存在。`reason_code` 使用稳定机器枚举，至少包括：

```text
REGISTRY_INCOMPLETE
INVALID_STATE
MISSING_TRANSLATION
MISSING_TEXTURE
EMPTY_RENDER
BACKGROUND_ONLY_RENDER
OBJECT_OFF_CANVAS
OBJECT_TOO_SMALL
FRAME_INCONSISTENT
ANIMATED_FIXTURE_UNSUPPORTED
FLUID_FIXTURE_UNSUPPORTED
BLOCK_ENTITY_FIXTURE_UNSUPPORTED
IO_ERROR
SCHEMA_INVALID
CHECKSUM_MISMATCH
IDEMPOTENCY_CONFLICT
EXPORTER_EXCEPTION
```

`kind: "failure"` 表示逻辑项未成功完成；`kind: "skip"` 表示明确不把该项作为可发布视觉候选。两者都不能缺少原因。`action: "needs_review"` 必须产生审核任务；`action: "skipped"` 只有在独立 `skip-review.v1` 已逐字段记录 `target_id`、`minecraft_version`、`reviewer`、`reviewed_at`、`reason_code`、`note`、`evidence`、`source_version`、`machine_failure_ref` 后才算解决；`excluded` 资格必须另有独立 `qualification-review.v1`，不能用 skip 记录替代。方块若没有可发布变体，至少要有一个已审核的 block/state/variant skip 原因，否则不是覆盖而是发布失败。

错误不得通过伪造 `block_id`、合法状态、默认状态或图片恢复。恢复应从对应 `logical_key` 游标继续；不能恢复时保留原失败记录并以新记录说明决策。

## 10. `exporter.log`

日志文件是 JSONL 诊断事件流，不是业务记录 Schema；每行至少包含：

```json
{
  "timestamp": "2026-08-13T10:20:00.123Z",
  "level": "INFO",
  "export_id": "exp_01J...",
  "stage": "RENDER_VARIANTS",
  "event": "render_completed",
  "logical_key": "variant:vv_...",
  "attempt": 1,
  "message": "preview written",
  "duration_ms": 321,
  "artifact_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
}
```

`level` 只能是 `DEBUG`、`INFO`、`WARNING`、`ERROR`；失败事件必须带 `error_code` 和可操作 `message`。日志不得写 API key、完整密钥、秘密或本地用户隐私。日志至少记录阶段开始/结束、游标移动、重试、跳过、Schema 失败、校验失败和最终状态。

## 11. 校验和、规范化和文件完整性

JSON、manifest、日志事件和 metadata 中的摘要值统一使用 `sha256:<64 lowercase hex>`。`checksums.sha256` 是 UTF-8 文本，每行格式为：

```text
<64 个小写十六进制字符>  <相对路径>
```

它必须列出 release/export 目录中除自身外的全部普通文件，按路径 UTF-8 字节序升序；每行必须是 `<lowercase hex><两个 ASCII 空格><POSIX 相对路径>\n`，行首哈希不带 `sha256:` 前缀。不列目录、symlink 或 hardlink；重复路径、绝对路径、反斜杠和 `..` 均非法。哈希算法固定为 SHA-256。导出 manifest 只记录功能输入/产物 hash，不记录自身、release pointer 或 checksum 文件 hash，避免循环；release 的 `release.json` 只记录 manifest hash，checksums 文件自身不再由 release metadata 反向引用。校验后禁止修改任何文件。

JSON 对象哈希使用 JCS-RFC8785；JSONL 记录哈希使用规范化对象字节加一个 LF；PNG、日志和校验文件使用原始字节。写临时文件、完成 fsync、校验通过后才能提交导出目录。

## 12. 幂等、恢复和提交

### 12.1 逻辑键

以下逻辑键是导出器恢复和去重的最小粒度：

```text
export:<export_key>
block:<block_id>
state:<state_id>
variant:<variant_id>
render:<variant_id>:<render_signature>
```

每个逻辑项都有 `input_signature`，由完整输入、策略版本、工具链版本和资源包哈希计算。相同 `logical_key + input_signature` 已有合法成功产物时，重启必须复用它，不重新渲染、不重复写记录、不覆盖成功图片；缓存命中必须再次验证字节哈希和 Schema。

### 12.2 冲突和恢复

- 同一逻辑键且输入签名相同：校验通过后视为幂等成功。
- 同一逻辑键输入签名不同：不得覆盖，记录 `IDEMPOTENCY_CONFLICT`，要求新 `export_id` 或人工明确重建。
- 临时文件、半行 JSONL 或没有校验记录的资产不得当作成功；启动时隔离并从最后一个已提交游标继续。
- 导出器遗留的 `running` 标由调用方按心跳超时恢复；有成功记录的项绝不再次执行。
- 每个逻辑项自动最多一次重试（首次执行加一次重试）；超过上限只能进入审核或整体失败。

导出器应先写 `*.tmp.<export_id>`，完成文件级校验后再原子提交。多文件提交必须先完成全部数据和 manifest，随后计算并校验 `checksums.sha256`；两者都完成且校验通过后才能提交目录。若进程中断，消费者只能接受状态完整且校验通过的目录。发布构建禁止从可变目录创建 hardlink/symlink；release builder 必须复制到 staging，逐文件 hash、flush/fsync，再原子 rename 为 release ID 并冻结。

## 13. 导出包验收条件

导出包可以交给工作库导入，当且仅当以下检查全部可执行且结果明确：

1. manifest 的固定工具链、精确平台/渲染环境、`minecraft_version`、策略版本和契约版本完整。
2. `blocks.jsonl` 恰好覆盖运行时 `minecraft` 注册表，覆盖率 100%，无虚假 ID。
3. `states.jsonl` 覆盖每个合法状态，`legal_state` 均由运行时确认，默认状态引用合法。
4. 每个状态有变体映射，或有与之对应的机器可读 skip 原因；每个 skip 可追溯到审核或待审核任务。
5. 每个 `selected` 变体的代表状态、状态集合、行为事实、图片和 `render.json` 互相一致；不得跨 block ID 合并。
6. 每张 `preview.png` 和 `mask.png` 可读取、为 512×512，四视角和固定策略元数据齐全。
7. 所有 JSONL 行通过各自严格 Schema，`failures.jsonl` 的引用和枚举合法；exporter 的状态/变体选择记录完整。
8. `checksums.sha256` 按 UTF-8 字节序覆盖所有必需普通文件，重新计算结果一致；JSON/metadata 摘要采用 `sha256:` 前缀格式，checksum 行首使用无前缀的 64 位小写十六进制格式。
9. 日志包含结束事件，manifest 状态与失败/审核计数一致。
10. 包内没有原版 JAR、资源包、纹理、模型、字体或声音。

这些是导出包门槛，不等同于发布门槛；发布还必须通过 [流水线、存储与发布](pipeline-storage-and-publishing.md) 中的 AI、审核、FTS、MCP 和原子切换门禁。
