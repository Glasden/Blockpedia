# 数据分层与 Schema 设计

## 1. 目的、规范词和关联文档

本文定义 `Block`、`VisualVariant`、`Annotation` 三层数据模型、来源隔离和人工覆盖。精确字段形状唯一由 `schemas/{exporter,workspace,provider,mcp}/` 下的 26 个真实 Schema 文件拥有；本文的业务行为和示例不重复穷举字段规范。所有实现必须遵守 `MUST`、`MUST NOT`、`SHOULD`、`MAY` 的规范含义。默认 SQLite 与本地文件实现可按记录影响和所有者批准替换，但不得破坏本文数据不变量。原始设计稿仅是历史背景，不与本契约共同作为规范。

关联文档：

- [导出契约](export-contract.md)
- [状态策略与渲染](state-policy-and-rendering.md)
- [流水线、存储与发布](pipeline-storage-and-publishing.md)
- [roadmap.md](roadmap.md)
- [decisions.md](decisions.md)
- [OpenAI Provider 接口](openai-provider.md)
- [WebUI 与运行接口](webui-and-operations.md)
- [搜索与排序接口](search-and-ranking.md)
- [MCP API 接口](mcp-api.md)
- [质量与测试接口](quality-and-testing.md)

## 2. 版本、严格 Schema 和通用引用

### 2.1 版本字段

每个持久化业务记录必须带其 Schema 要求的版本和上下文；导出/release 记录、current pointer、审核记录、人工覆盖和 provider 请求 envelope 的具体字段以对应真实 Schema 为准。Schema ID 全局唯一且用途分离；所有声明的 JSON Schema **MUST** 在 R0 物化为真实文件并完成 strict 验收。MVP 使用以下固定 Schema ID：

```text
export-manifest.v1
export-block.v1
export-state.v1
export-variant.v1
export-failure.v1
render-metadata.v1
block-record.v1
state-record.v1
visual-variant-record.v1
annotation-record.v1
manual-override.v1
skip-review.v1
qualification-review.v1
provider-batch-envelope.v1
annotation-batch-output.v1
annotation-wire-item.v1
query-spec-output.v1
rerank-output.v1
mcp-index-info-output.v1
mcp-search-blocks-output.v1
mcp-block-details-output.v1
mcp-compare-blocks-output.v1
mcp-error.v1
release-manifest.v1
release.v1
current-pointer.v1
```

以下是版本化策略标识，不是 Schema ID；R0 不引入独立词汇 artifact：

```text
state-policy.v1
render.v2 (current exporter policy)
render.v1 (historical record/run policy)
fixture.v1
dedupe.v1
```

`render.v1` 和 `render.v2` 都是现有 v1 Schema 中合法的 `render_policy_version`。未修改的历史 `render.v1` records、workspace/release data 在当前 v1 Schema ID 下保持 valid，并只在其 record/run context replay；新导出、current fixtures 和 workspace fixture 默认使用 `render.v2`。preserved old export package 在 repository Schema bytes 变化后不由 current external validator 重新验证；其 embedded `schemas.sha256`/`schema_inventory` 是 binding evidence，current validation 必须报告 `SCHEMA_INVENTORY_HASH_MISMATCH`。不得 bypass hash、自动迁移、增加 historical Schema snapshot layer 或使用 version-aware validator fallback；旧 package bytes/reports 只作为历史证据保留。Schema ID 不因本次 policy 修订而增加。

JSON Schema 使用 Draft 2020-12；每个 root object 拒绝未知字段，重要 nested objects 关闭未知字段。R0 只做 inventory、fixtures 和 provider wire 基础验证；不引入通用规则引擎。当前 ID 仅使用 exporter、workspace/release、provider 和 MCP 冻结命名空间中的标识；旧 ID 不作为新规范当前 ID。两种 OpenAI adapter 共用 `annotation-batch-output.v1`、`query-spec-output.v1` 和 `rerank-output.v1`，其 wire `name` 分别固定为 `annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`；Responses 使用 `text.format`，Chat 使用 `response_format.json_schema`；标注批次中的元素使用独立的 `annotation-wire-item.v1`。未知字段必须拒绝，不得静默发布。

Schema inventory 的规范仓库路径固定为 `schemas/<namespace>/<schema-id>.json`，其中 `<namespace>` 只能是 `exporter`、`workspace`、`provider` 或 `mcp`；路径使用相对仓库根的 POSIX 写法，不含 `./`、`..`、反斜杠或绝对路径。release 内的 `schemas.sha256` 每行必须严格为：

```text
<64 lowercase hex>  <schema-id>  <canonical-repository-relative-posix-path>\n
```

行首 digest 不带 `sha256:`，按 Schema ID 的 UTF-8 字节序排序；文件自身不列入自身 inventory。`schemas.sha256` 只证明仓库 Schema 文件摘要和路径，不声称这些路径位于 release；release 内普通文件的完整性仍由排除自身的 `checksums.sha256` 独立覆盖。

Schema 变更时，旧 release 只读不改；工作库以新版本从导出包重建。MVP **MUST NOT** 提供通用数据库迁移框架，也不能靠运行时 `ALTER` 把旧语义伪装成新 Schema。受控语义字段在具体使用前只需采用 bounded strings；词汇 membership 只有未来出现具体需求时才增加，不引入新服务或框架。

### 2.2 通用标识和引用

- `minecraft_version` 必须是精确字符串 `26.2`，不得写范围或别名。
- `block_id` 必须匹配 `^minecraft:[a-z0-9_./-]+$`，且存在于运行时注册表。
- `state_id` 就是 canonical state string；`properties` 必须逐项匹配该 block 的合法属性值。不能在某些记录使用 hash、在另一些记录使用字符串。
- R1 的 `variant_id` 等于其 `block_id`；它不是连续数组下标，也不再使用哈希身份。
- `export_id` 绑定一次导出包且等于导出目录名；不同导出包的数据不得混写。
- `release_id` 绑定一份不可变发布物；发布后记录、图片、索引和 manifest 不得原地修改。

引用不存在、版本不匹配、跨 block ID 引用或引用 skipped 变体的搜索条目必须被拒绝。

### 2.3 Canonical state string

所有四份文档统一使用同一 serializer：先验证 `block_id` 和每个属性名/值属于目标运行时合法集合，再验证允许字符；属性名按 UTF-8/ASCII codepoint 升序排序；无属性返回 `block_id`，有属性返回 `block_id + "[" + name=value 以逗号连接 + "]"`；不含空格，不转义或强制修复非法字符。属性名允许字符为 `[a-z0-9_]+`，属性值允许字符为 `[a-z0-9_.-]+`；目标运行时合法集合仍是第一道验证。实现不得依赖 JVM map/toString 顺序。此 canonical string 同时是 `state_id`、`default_state_id`、`canonical_state_id` 和所有 represented state 引用。

## 3. 三层模型

### 3.1 `Block` 层

`Block` 表示一个运行时注册表实体，只保存 block 级机器事实和官方名称，不保存 AI 语义：

```json
{
  "schema_version": "block-record.v1",
  "export_id": "export_20260814T165501Z",
  "minecraft_version": "26.2",
  "block_id": "minecraft:yellow_carpet",
  "translation_key": "block.minecraft.yellow_carpet",
  "official_names": {
    "zh_cn": "黄色地毯",
    "en_us": "Yellow Carpet"
  },
  "default_state_id": "minecraft:yellow_carpet",
  "properties": {},
  "tags": [],
  "machine_facts": {
    "has_item": true,
    "has_block_entity": false,
    "waterloggable": false,
    "redstone_related": false
  },
  "source": {
    "type": "runtime",
    "minecraft_version": "26.2",
    "exporter_version": "1.0.0",
    "verified": true
  }
}
```

官方 `zh_cn`/`en_us` 名称必须来自精确版本运行时语言资源。它们与 AI 同义词和人工同义词是不同字段、不同来源、不同审核状态；翻译缺失使用 `null` 并记录原因，不能让模型补写为官方名称。

### 3.2 `VisualVariant` 层

`VisualVariant` 是一个 block 内的稳定视觉/用途实体。R1 中它引用合法 `canonical_state_id`，列出全部 `represented_state_ids`、固定 isolated context、渲染资产和机器特征。机器部分只能由导出器和确定性特征提取器写入：

```json
{
  "schema_version": "visual-variant-record.v1",
  "export_id": "export_20260814T165501Z",
  "minecraft_version": "26.2",
  "variant_id": "minecraft:yellow_carpet",
  "block_id": "minecraft:yellow_carpet",
  "canonical_state_id": "minecraft:yellow_carpet",
  "represented_state_ids": ["minecraft:yellow_carpet"],
  "machine_facts": {
    "geometry": {
      "width": 1.0,
      "height": 0.0625,
      "depth": 1.0,
      "occupied_volume": 0.0625,
      "is_full_cube": false,
      "is_horizontal_sheet": true
    },
    "collision_signature": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "transparent": false,
    "emissive": false,
    "support": {"below": true},
    "machine_tags": ["shape:horizontal_thin_sheet", "support:below"]
  },
  "render": {
    "preview_path": "renders/minecraft/yellow_carpet/preview.png",
    "mask_path": "renders/minecraft/yellow_carpet/mask.png",
    "render_metadata_path": "renders/minecraft/yellow_carpet/render.json",
    "image_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "mask_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
    "render_metadata_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  },
  "annotation_refs": ["ann_..."],
  "override_refs": [],
  "candidate_qualification": "eligible",
  "warnings": []
}
```

`candidate_qualification` 只能是 `eligible`、`conditional` 或 `excluded`。`conditional` 必须有非空 warnings；`excluded` 不进入默认搜索候选，但保留详情和审计数据。不可稳定渲染的变体必须为 `excluded` 或 skipped，不得作为发布搜索视觉候选；这不删除 Block 或状态事实。

### 3.3 `Annotation` 层

`Annotation` 只存自然语言检索语义和来源，不存游戏事实：

```json
{
  "schema_version": "annotation-record.v1",
  "annotation_id": "ann_01J...",
  "subject_type": "visual_variant",
  "subject_id": "minecraft:yellow_carpet",
  "minecraft_version": "26.2",
  "source": {
    "type": "llm",
    "model_id": "configured-model-id",
    "prompt_version": "prompt.v1",
    "verified": false
  },
  "synonyms_zh": ["黄色薄毯", "黄色铺地层"],
  "synonyms_en": ["yellow floor covering"],
  "color_terms": ["yellow"],
  "shape_terms": ["horizontal_thin_sheet"],
  "material_impressions": ["textile_like"],
  "building_roles": ["floor_covering", "roof_detail"],
  "style_tags": ["simple"],
  "avoid_for": ["load_bearing_wall"],
  "confidence": 0.88,
  "summary_zh": "黄色、非常薄的铺地层，适合覆盖水平表面。",
  "reason": "图片和机器几何均显示为薄层。"
}
```

`subject_type` MVP 允许 `block` 或 `visual_variant`，但搜索发布的语义必须能解析到具体可发布 `variant_id`。同义词与官方名称严格分离；AI 与人工也分别作为不同 Annotation 或 override 层保存，人工值不能回写成 AI 结果。持久化语义记录使用 `annotation-record.v1`；provider 批次中的元素使用 `annotation-wire-item.v1`，只能对应请求中预先存在的 `tile_id`/`variant_id` 映射，不携带持久记录的 `annotation_id`、`source` 或审核字段，且不得创建新的 ID、状态或机器事实。

### 3.3.1 Prompt version 与最终诊断

frozen run/release 的 `prompt_version` 是 annotation 的 replay lineage。`prompt.v1` 和已有历史 version string 必须保持 exact legacy behavior；`prompt.v2` 只能由新的 run/profile snapshot 选择，改变 signature/cache identity，不能原地迁移、re-sign 或删除当前 pending v1 jobs。v2 的 model-visible projection 只包含 existing tile 的 trusted instruction、contact-sheet tile labels、`tile_id`/exact existing `variant_id` 和去重有界 `geometry_classes`；完整 machine metadata、hashes、source images、envelope/cache/signature/release lineage 仍是本地事实，不改变持久 Annotation 或 machine-fact Schema。

D-042 不改变 `annotation-batch-output.v1`、`annotation-wire-item.v1` 或 `annotation-record.v1`。模型仍返回 `schema_id`、`variant_id` 和当前 `annotation-wire-item.v1` 的全部 13 个 required item fields；local `schema_id` injection、`tile_id` codec 和 semantic-field reduction 保持 deferred，除非 diagnostics 足以支持另一个 owner-approved/materialized Schema decision。

最终 `offline_annotation` validation 在总 retry budget 用尽后失败时，sanitized diagnostic 只能作为既有 `PROVIDER_FAILURE` review task 的 `evidence_json` 子对象传递；它不是 provider envelope、`provider_requests` column、Schema、table 或报告。该对象只能包含 `stage`、`phase`、`path`、`keyword`、`observed_type`、`observed_length` 六个 allowlisted fields；不得保存 raw value/prefix、provider message、exception、repair context、prompt/image/secret 或 response/value hash。successful repair 不保存 diagnostic，既有 evidence rows 仍有效。

## 4. 事实来源边界

### 4.1 机器事实

以下字段只能由 Minecraft 运行时、导出器或确定性脚本产生，发布后不可由人工编辑：

```text
block_id
translation_key
合法 BlockState 及属性值
default_state_id
minecraft_version / loader / API / mappings 等版本
BlockState 的 shape 与 collision
占用体积、几何签名、对象蒙版和图片哈希
运行时透明/半透明、发光等级
可含水、可穿过、支撑测试
has_item、has_block_entity
运行时 tags
代表状态与状态映射
固定夹具、相机、光照、资源包哈希
实际 renderer options/environment identity，包括固定 resolver seed `42L`、atlas reload/freeze 控制和其它影响动画确定性的控制
```

`machine_facts` 的行为值统一使用 `true`、`false`、`unknown`。采集失败或证据不足时必须是 `unknown`，不能默认 `false`。`unknown` 不满足硬约束：要求 `true` 只接受 `true`；排除某行为时 `unknown` 也不能被当作安全的 `false`。

渲染材料的机器真相来自 Java resolved submission：whole-model/material missing、missing model、vanilla material/quads 和 Fabric mesh（通过 block-atlas missing sprite、`SpriteFinder` 与 missing-sprite UV bounds）均由 exporter 判断。`#F800F8`/`#000000` 四象限只描述 `minecraft:missingno` 的 source checker；渲染颜色不是 authority。Python 不使用宽松全局 magenta/black 比例，只能以严格 canonical checker 作 defense-in-depth，ambiguous pixels 不能单独形成 missing-material 事实。

### 4.2 AI 语义

LLM 只允许产生中文/英文受控同义词、视觉描述、颜色词、形状词、材质观感、建筑用途、风格关联、不适用场景和语义置信度。LLM 永远不得产生、修改、选择或覆盖：

```text
block_id
variant_id
合法状态或状态属性值
default_state_id
shape / collision / 几何签名
运行时透明、半透明或发光
支撑、可穿过、可含水、红石、方块实体等行为事实
运行时 tags
Minecraft、Fabric、资源包、Schema 或数据版本
图片路径、图片哈希、状态映射
发布状态或 candidate_qualification
```

提示词可携带机器元数据帮助描述，但消费端必须拒绝模型返回的事实字段；模型只能为请求中已有 `subject_id` 返回一次语义对象，不能新增 ID。Schema 冲突或修复失败必须进入高优先级审核。

### 4.3 人工声明式覆盖

人工修改保存为版本控制的 `manual-override.v1` 声明，不直接覆盖生成列。覆盖只允许改变 AI 语义、增加/删除受控标签或补充开放描述；资格覆盖必须单独使用 `qualification-review.v1`，跳过必须单独使用 `skip-review.v1`。任何人工记录都不允许改变机器事实。

默认 scope 是单个 `variant_id`。`family` 或 `global` scope 必须显式写出范围、匹配条件、影响字段、理由、作者、批准者和适用输入签名。重建时必须重新解析和校验所有目标引用；引用失效、版本不匹配、selector 为空或命中机器字段时，构建失败而非忽略。

## 5. 受控语义与开放文本

受控语义字段在 R0 只使用真实 Schema 中的 bounded strings/arrays；不引入独立词汇 artifact、词汇 hash 或通用 membership engine。未来只有在具体实现需要词汇 membership 时，才在对应 Schema 和行为契约中增加最小规则，不新增服务或框架。

### 5.2 开放文本

开放文本只允许用于描述或理由：`summary_zh`、`summary_en`、`description`、`reason`、`review_note`。单字段长度为 2～500 个 Unicode 字符；不得承载新 ID、状态语法、机器事实或未受控标签。开放文本不参与硬过滤；搜索只使用真实 Schema 允许的 bounded semantic fields 和官方名称/同义词索引。

## 6. AI 校验、置信度和审核

每个 Annotation 必须通过：

1. JSON Schema 和严格字段检查；
2. `subject_id`、`minecraft_version` 和批次映射检查；
3. 每个编号恰好一次、无重复、无新增对象检查；
4. 机器事实冲突检查；
5. 文本长度和数组去重检查。

AI 置信度门控固定如下：

```text
confidence >= 0.80       自动通过，可抽样
0.65 <= confidence < .80 普通审核
confidence < 0.65        高优先级审核
```

无有效置信度、Schema 冲突、一次修复后仍失败、机器事实冲突、描述与图片/几何明显矛盾，均直接进入高优先级审核。修复请求是该逻辑项唯一一次自动重试，不能无限调用或重复计费。

每个可搜索变体在发布前必须有通过 Schema 且审核状态允许发布的 AI Annotation，或等价的通过 Schema 的人工 Annotation/override（含作者和审核记录）。低置信度未审核语义、缺图变体和 `unknown` 事实不满足发布条件。

## 7. `manual-override.v1`、资格和跳过审核

```yaml
schema_version: manual-override.v1
override_id: ov_01J...
minecraft_version: "26.2"
scope:
  level: variant
  variant_id: minecraft:yellow_carpet
operations:
  add_building_roles: [roof_detail]
  remove_building_roles: [load_bearing_wall]
  set_summary_zh: "黄色薄层，适合作为屋檐细节而非承重结构。"
reason: "人工检查图片与用途。"
author: "local-user"
approved_by: "local-user"
created_at: "2026-08-13T11:00:00Z"
applies_to:
  export_contract_version: export-contract.v1
  input_signature: sha256:0000000000000000000000000000000000000000000000000000000000000000
```

`provider-batch-envelope.v1` 是 provider 请求审计/输入 envelope，不是 AI 语义记录。每个 envelope 必须绑定一个 `stage`：`offline_annotation`、`query_spec` 或 `visual_rerank`，并包含 required `adapter`（`openai_responses|openai_chat_completions`）、`request_id`、`profile_id`、`model_id`、`base_url_stable_id`、`secret_reference`、`prompt_version`、`wire_schema_id`、`minecraft_version`、适用的 `export_id` 或 `release_id` 和输入摘要。`adapter=openai_responses` 时 envelope 必须包含 `store: false`；`adapter=openai_chat_completions` 时 **MUST NOT** 包含 `store`；二者都不表示远端 retention 已验证。在线 stage 还必须绑定 resolved release manifest hash。`offline_annotation` 必须有唯一 `tile_id` 到已有 `variant_id` 的映射；`query_spec` 的 `input_summary` 精确只能是 `query_sha256`，不得含 `candidate_map`；`visual_rerank` 只能携带本地已召回候选的完整映射。`wire_schema_id` 必须分别为 `annotation-batch-output.v1`、`query-spec-output.v1` 或 `rerank-output.v1`，stage 与 Schema 不匹配、映射新增/缺失或出现秘密正文均拒绝。envelope 不保存完整 provider response、原始 prompt、图片内容、Authorization、Token usage、费用或预算。

provider snapshot 只属于 `release-manifest.v1` 的 `manifest.json`，并必须冻结 `adapter` 作为 protocol lineage；`release.v1` 对应的 `release.json` 只使用其真实 Schema 允许的 release identity、`manifest_sha256` 和其它 release 元数据，不复制 provider snapshot。既有 Responses snapshot/release 不迁移、不改写。

`operations` 只能是语义字段；下列操作名永远非法：`set_block_id`、`set_state`、`set_default_state`、`set_shape`、`set_collision`、`set_behavior`、`set_tags`、`set_minecraft_version`、`set_image`、`set_candidate_qualification`、`set_warnings`。发现机器事实错误必须修复 exporter/运行时探测并生成新导出包，不能用人工修正隐藏。

`family` scope 必须有固定 family ID 和成员快照哈希；`global` scope 必须有明确 `selector`、owner approval 和影响计数。应用顺序固定为：机器导出 → 确定性特征 → 合法 AI annotation → 按 scope 排序的人工 override；同层冲突报错，不以文件顺序取胜。所有 override 都进入 release 审计清单。

资格审核使用 `qualification-review.v1`，资格只允许 `eligible`、`conditional`、`excluded`；`conditional` 必须有 warnings，`excluded` 必须有理由和证据。跳过审核使用 `skip-review.v1`，其统一字段为：

```json
{
  "schema_version": "skip-review.v1",
  "target_id": "minecraft:example|state-string|variant-id",
  "minecraft_version": "26.2",
  "reviewer": "local-user",
  "reviewed_at": "2026-08-13T11:00:00Z",
  "reason_code": "MISSING_TEXTURE",
  "note": "固定夹具中没有可读纹理。",
  "evidence": ["evidence/render-failure.json"],
  "source_version": "export-contract.v1",
  "machine_failure_ref": "fail_01J..."
}
```

`qualification-review.v1` 复用同样的 `target_id`、`minecraft_version`、`reviewer`、`reviewed_at`、`reason_code`、`note`、`evidence`、`source_version` 字段，另要求 `qualification` 和 `warnings`；资格审核不能引用 skip 作为替代。两种审核记录必须逐字段 Schema 校验，发布后只读。

## 7.1 R3 Phase C `manual-overrides.json` 原始记录包（data owner）

release 根的 `manual-overrides.json` 是 Phase C 的记录归档文件，不是新的 JSON Schema。它的顶层字段必须且只能是：

```json
{
  "format_version": 1,
  "release_id": "rel_<32 lowercase hex>",
  "version": "26.2",
  "manual_overrides": [],
  "skip_reviews": [],
  "qualification_reviews": []
}
```

三个数组必须分别只包含原样、完整、通过对应真实 Schema 的 `manual-override.v1`、`skip-review.v1`、`qualification-review.v1` 记录；不得扁平化、删字段、改字段名、把有效值合并成机器列，或加入 release index 专用字段。数组稳定排序分别按 `override_id`、`review_id`、`review_id` 的 UTF-8 字节序；同 ID 的异常重复也必须阻断，不能以最后一条覆盖前一条。`format_version` 是文件格式版本，`version` 是精确 `minecraft_version` 的 owner 字段；这两个字段均不是 Schema ID。

写入 release 前必须重新逐条校验三类原始记录：未知字段、Schema 不通过、`minecraft_version` 不等于 `version`、目标不存在、跨版本目标、`machine_failure_ref` 不存在或不属于当前 export、以及任何 orphan review 都必须阻断 build。不得因为记录无法投影到 release index 就静默丢弃。人工记录的完整值只在本文件保留；release index 的有效语义/资格/搜索 projection 只能引用 [`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md) 的独立 `release-index.v1.sql` 边界，不在本文复制 SQL 表或列，也不能把 SQL projection 当作人工审计来源。

## 8. 数据不变量和发布视图

工作库可以保存未完成任务、失败和审核任务，但发布视图只能包含通过门禁的数据；`draft`、`ready`、`pause_requested`、`cancel_requested` 只能作为命令或事件，不能作为持久状态：

1. 每个 Block 恰好一个 `block_id`，属于 `minecraft` 注册表快照。
2. 每个 Block 的 `default_state_id` 在 states 集合中，所有 state 属性值合法。
3. 每个 state 的 `variant_ids` 只引用同 block 的变体；变体的 `represented_state_ids` 反向引用一致。
4. 每个发布视图中的 VisualVariant 都有可读 512×512 四视角图片和匹配 `image_sha256`。
5. 机器事实列、AI 语义列和人工覆盖来源可区分、可审计；人工值不能伪装为机器或 AI 来源。
6. R1 的 `variant_id` 必须等于其 `block_id`；不同 block ID 即使图片内容相同也不能合并实体。
7. `unknown` 不得被硬约束解释器当成 `true` 或安全的 `false`。
8. 每个可搜索变体有合规 AI 或人工语义；不可搜索/跳过变体有理由。
9. release 构建后数据和图片只读，Schema 改变必须产生新 release。

SQLite 的 FTS 只索引官方名称、已校验同义词、受控描述词和用途词，不索引未经审核开放文本作为硬匹配。没有向量列，也不允许用 LLM 临时改变索引中的事实。

## 9. Schema 验收

Schema 实现验收必须包含正例和拒绝例：

- 正确的 Block、完整合法状态、默认状态和变体双向引用可以通过。
- 非 `minecraft` ID、虚假 state、错误版本、重复 variant、跨 block 引用必须拒绝。
- `unknown` 是唯一允许的未知行为值；`null`、空字符串和任意第三态行为值必须拒绝。
- LLM 输出 block ID、状态、几何、行为、资格或事实标签必须拒绝并创建高优先级审核任务。
- 未知词、额外字段、重复数组值、超长开放文本和无置信度 Annotation 必须拒绝。
- 无效 variant/family/global override、越权机器字段或过期输入签名必须阻断重建。
- Schema 或语义字段约束变化不会修改现有 release，而是使新构建使用新版本并重新跑完整门禁。

具体字段、接口错误码和版本协商由 [OpenAI Provider 接口](openai-provider.md)、[WebUI 与运行接口](webui-and-operations.md)、[搜索与排序接口](search-and-ranking.md)、[MCP API 接口](mcp-api.md) 和 [质量与测试接口](quality-and-testing.md) 继续细化；本文件规定不可违背的分层和来源边界。
