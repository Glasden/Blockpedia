# OpenAI Provider 契约

## 文档状态、优先级与关联规范

本文是 Blockpedia MVP 的 protocol-neutral OpenAI provider 行为契约。精确 wire/profile 字段形状唯一由 `schemas/provider/` 下的真实 Schema 文件拥有；本文示例仅用于说明行为。正文使用简体中文；`OpenAIProvider`、`openai_responses`、`openai_chat_completions`、字段名、Schema 标识、状态、错误码和命令保持英文。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 使用规范性含义。

本文件服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md) 和 [`decisions.md`](decisions.md)，并与 [`product-scope.md`](product-scope.md) 和 [`architecture.md`](architecture.md) 保持一致。原始设计稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。数据来源和发布边界还必须遵守 [`data-and-schemas.md`](data-and-schemas.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)、[`export-contract.md`](export-contract.md) 与 [`state-policy-and-rendering.md`](state-policy-and-rendering.md)。

相关实现契约：

- [`search-and-ranking.md`](search-and-ranking.md)：`QuerySpec`、本地召回、视觉重排和在线降级；
- [`mcp-api.md`](mcp-api.md)：MCP 工具对 provider 失败的外部表现；
- [`webui-and-operations.md`](webui-and-operations.md)：profile 配置、能力探测、任务和审核写入口；
- [`quality-and-testing.md`](quality-and-testing.md)：provider contract tests 和发布门；
- [`security-and-distribution.md`](security-and-distribution.md)：秘密、最小披露和分发禁入物。

## 1. 冻结范围

### 1.1 唯一 provider、显式 adapter 和活动 profile

MVP **MUST** 只实现 protocol-neutral `OpenAIProvider`，并且每个 profile 的 `adapter` **MUST** 显式取 `openai_responses` 或 `openai_chat_completions`。两个值分别绑定明确的 wire codec/adapter：Responses 使用 `/responses`，Chat 使用 `/chat/completions`。MVP **MUST NOT** 实现 Anthropic Messages、其他 provider adapter、Embedding、独立视觉模型、模型投票、隐式 protocol fallback 或隐式 model 切换；选定 adapter 失败时不能自动改用另一个 adapter。

系统可以保存多个非活动 profile，以便用户保留不同的非秘密配置；全局 **MUST** 至多有一个 `enabled=true` 的活动 profile。该约束仅适用于 Studio 新任务、配置管理和构建新 release。活动 profile 及其同一个 configured/requested `model_id` 只控制 Studio 的新写任务和新 release，并必须同时用于以下三个阶段：

```text
offline_annotation
query_spec
visual_rerank
```

同一 release 的离线标注、QuerySpec 和视觉重排必须使用同一 release-bound configured/requested `model_id` 和同一 `adapter`。`model_id` 与 `adapter` 是运行时配置，不得硬编码；每个 run 的非秘密配置快照、每个 AI 产物和 `manifest.json` **MUST** 记录 requested `model_id`、`adapter`、`profile_id`、`base_url_stable_id`、`secret_reference`、prompt/schema/search 版本。成功响应中的 string `model` 只是 untrusted informational echo；它可以不同于 requested `model_id`，不得作为已验证远端模型身份、持久化值或替换来源。非活动 profile 不得用于 Studio 新写任务或新 release；但 release-bound MCP 必须使用 `manifest.json` 冻结的 provider snapshot，不读取或比较可变的 active profile。切换 Studio active profile、adapter 或 endpoint 不影响旧 release，release snapshot 也不算另一个 active profile。除合法 offline-concurrency-only profile edit 外，改变其中任一值后，旧缓存不得被当作新输入的成功结果；尚未完成的旧 cache/workspace 必须失效并重新运行，不做协议迁移。`secret_reference` 只保存引用，不保存 key。既有 `openai_responses` profile 和 release 继续有效。

### 1.2 兼容 `base_url`

`base_url` 是所选 adapter 的用户批准 endpoint 配置：`openai_responses` 追加 `/responses`，`openai_chat_completions` 追加 `/chat/completions`。用户可以保存并明确批准兼容所选协议语义的 endpoint；这不是第二 provider，也不是协议 fallback。兼容 endpoint **MUST NOT** 因“看起来像 OpenAI”而跳过完整能力门。

`base_url` 不能携带 query、fragment、用户名、密码或 API key。系统必须规范化并保存非秘密稳定标识 `base_url_stable_id`：保留 scheme、host、显式 port 和规范化 path，去掉尾随 `/`，拒绝凭证与 query；稳定标识不得包含秘密。原始本地绝对路径和 Authorization 不得进入 provider 请求或 `base_url_stable_id`。

两种协议都不能证明远端保留或不保留数据，也不能验证第三方实际执行了哪个模型；第三方服务策略、路由和模型身份由用户负责。`openai_responses` 请求和 probe **MUST** 发送 `store=false`，但不要求响应回显 `store`；缺少回显不得导致 probe 失败。`openai_chat_completions` 请求和 probe **MUST** 省略 `store`；probe 只检查实际发出的请求没有该字段。成功响应必须包含 string `model`，缺失或非 string 仍为 `PROVIDER_MODEL_UNAVAILABLE` 或等价 fail-closed structural error；不同于 requested `model_id` 的 string 不失败、不持久化、不展示为 verified actual model。能力 gate 只验证所选 adapter 的图片输入、strict structured output、稳定错误分类、requested model/auth 和协议 wire 形状，**MUST NOT** 声称存在 storage-verified 或 remote-model-identity-verified 能力，也不得提供确认或豁免字段。

## 2. Provider profile

### 2.1 `ProviderProfile` strict Schema

Provider profile 配置对象使用 JSON Schema Draft 2020-12，严格对象必须 `additionalProperties: false`。Provider profile 不是 D-030 冻结的持久业务 Schema ID；它的字段由 WebUI 配置契约校验。以下是实现必须支持的字段：

```json
{
  "profile_id": "default",
  "adapter": "openai_responses",
  "base_url": "https://api.openai.com/v1",
  "base_url_stable_id": "https://api.openai.com/v1",
  "model_id": "configured-at-runtime",
  "secret_reference": "keyring:blockpedia/default",
  "enabled": false,
  "capability_status": "unverified",
  "prompt_version": "prompt.v1",
  "annotation_output_schema_id": "annotation-batch-output.v1",
  "query_spec_output_schema_id": "query-spec-output.v1",
  "rerank_output_schema_id": "rerank-output.v1",
  "search_ranking_version": "search-ranking.v1",
  "request_timeout_ms": 60000,
  "stages": {
    "offline_annotation": {"batch_size": 12, "concurrency": 1},
    "query_spec": {"batch_size": 1, "concurrency": 1},
    "visual_rerank": {"batch_size": 1, "concurrency": 1}
  }
}
```

字段约束如下：

| 字段 | 约束 |
|---|---|
| `profile_id` | `^[a-z][a-z0-9_-]{0,63}$`，同一项目唯一；是 Keyring account 的值 |
| `adapter` | 只能是 `openai_responses` 或 `openai_chat_completions`；必须显式选择 |
| `base_url` | 绝对 HTTPS URL；本地测试 endpoint 可以使用受控的 `http://127.0.0.1`，不得使用非 loopback 明文远端 |
| `model_id` | 非空、长度不超过 200；不允许以 `latest` 或范围表达；由 profile 精确配置并原样发送；响应必须有 string echo，但不要求 echo 相等 |
| `secret_reference` | 只能是 `keyring:blockpedia/<profile_id>` 或 `env:OPENAI_API_KEY` 形式，不是 key 值 |
| `capability_status` | `draft`、`unverified`、`verified`、`failed`；不存在可绕过能力门的 `warning` |
| `annotation_output_schema_id` | 固定为 `annotation-batch-output.v1` |
| `query_spec_output_schema_id` | 固定为 `query-spec-output.v1` |
| `rerank_output_schema_id` | 固定为 `rerank-output.v1` |
| `search_ranking_version` | 固定为当前搜索排序配置，例如 `search-ranking.v1` |
| `request_timeout_ms` | 1000–600000 的整数 |
| `batch_size` | `offline_annotation` 为 8–16；在线阶段固定为 1 |
| `concurrency` | `offline_annotation` 为整数 `1..5`、默认 `1`；`query_spec`/`visual_rerank` 必须为 `1`；计数 logical batch，不改变每 batch 最多两次总尝试 |

profile 必须满足以下启用不变量：`adapter` 为两个显式枚举值之一、`enabled=true`、三个阶段使用同一 configured/requested `model_id`、能力状态严格为 `verified`、秘密可读取、实际 wire Schema ID/name 固定、Schema/prompt/search 版本存在，并且所选 adapter 的 endpoint/wire 能力门通过。成功响应的 model echo 不要求与 requested `model_id` 相等；缺失或非 string 仍必须 fail closed。保存非活动 profile 不需要探测，但不得用于 Studio 新写任务或新 release；release-bound MCP 例外使用已解析 release 冻结的 provider snapshot，不读取或比较可变 active profile。一个 profile 被启用后不得再启用第二个；变更活动 profile、adapter 或 endpoint 必须停止新 AI job，并在新 run 中使用新快照。只改变合法 `offline_annotation.concurrency` 的 profile edit 是唯一例外：保留 `verified`/`enabled` 且不需要 reprobe；`query_spec`/`visual_rerank` 仍固定为 `1`，其它配置变化继续沿用既有 invalidation。

### 2.2 配置来源和秘密

非秘密配置的合并优先级固定为：

```text
CLI startup arguments > environment variables > profile/project configuration > built-in defaults
```

provider profile 的具体保存和 WebUI 路由见 [`webui-and-operations.md`](webui-and-operations.md)。API key 的读取顺序固定为：

1. 操作系统 Keyring，service=`blockpedia`，account=`<profile_id>`；
2. Keyring 没有值时只读环境变量 `OPENAI_API_KEY`；
3. SQLite 只保存 `secret_reference`，不保存 key、可逆密文或环境变量值；
4. WebUI 只返回 `configured`、来源类别和安全掩码。

API key、Authorization、完整 header、完整 provider request/response、图片内容、本机绝对路径和 Token usage **MUST NOT** 写入 SQLite、缓存、任务快照、日志、异常、截图、release 或前端响应。

## 3. 请求契约

### 3.1 两种协议的请求和 wire codec

Studio 三类新写调用都必须使用同一个活动 profile、同一个 `adapter`、configured/requested `model_id` 和 `base_url`；release-bound MCP 的 QuerySpec/视觉重排必须使用该 release 冻结的同一个 provider snapshot/adapter/requested model，不读取或比较可变 active profile。两种协议都使用同一组 wire Schema ID/name、strict 本地校验、ID/机器事实校验、最小披露和两次总尝试预算。下方 `schema` 不是任意 Draft 2020-12 的声明：本地完整 Schema 可使用 Draft 2020-12，实际 wire Schema 必须是所选 endpoint 支持的 strict 子集。

```json
{
  "model": "<runtime model_id>",
  "store": false,
  "input": [
    {"role": "user", "content": [
      {"type": "input_text", "text": "<minimal instruction and untrusted data sections>"},
      {"type": "input_image", "image_url": "data:image/png;base64,<cropped local PNG>"}
    ]}
  ],
  "text": {
    "format": {
      "type": "json_schema",
      "name": "<annotation_batch_output_v1|query_spec_output_v1|rerank_output_v1>",
      "strict": true,
      "schema": "<Responses-supported strict subset; all fields required; objects additionalProperties=false>"
    }
  }
}
```

请求 **MUST** 为 `POST <base_url>/responses`；user content 使用 `input_text` 和 `input_image`。`store` 必须为 JSON boolean `false`。strict schema 位于 `text.format`，并使用 `type=json_schema`、固定 `name`、`strict=true` 和对应 Schema。响应是否回显 `store` 不参与成功判定，也不构成远端保留证明。

#### `openai_chat_completions`

```json
{
  "model": "<runtime model_id>",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "<minimal instruction and untrusted data sections>"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,<cropped local PNG>"}}
  ]}],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "<annotation_batch_output_v1|query_spec_output_v1|rerank_output_v1>",
      "strict": true,
      "schema": "<Chat-supported strict subset; all fields required; objects additionalProperties=false>"
    }
  }
}
```

请求 **MUST** 为 `POST <base_url>/chat/completions`，并且 **MUST NOT** 包含 `store`。strict schema 位于 `response_format.json_schema`，并使用固定 `name`、`strict=true` 和对应 Schema。请求为 non-streaming；只解析 `choices[0]`。`message.refusal` 非 null 是 `PROVIDER_REFUSAL`；只有 `finish_reason=stop` 且 `message.content` 为 JSON string 才可解析。`length`、`content_filter`、`tool_calls`、`function_call`、未知 finish reason、缺失或非字符串 content 均为 `PROVIDER_INCOMPLETE`/失败，不得走正常 fallback。

实现可以使用 SDK 等价的结构化参数，但发出的协议语义必须等价。三类请求 **MUST** 使用对应独立 Schema ID、对应 `name` 和 `strict=true`；Responses 使用 `text.format`，Chat 使用 `response_format.json_schema`。`name` 与 Schema ID 分离，且只能匹配 `[A-Za-z0-9_-]{1,64}`：

| wire Schema ID | structured-output `name`（两种协议相同） |
|---|---|
| `annotation-batch-output.v1` | `annotation_batch_output_v1` |
| `query-spec-output.v1` | `query_spec_output_v1` |
| `rerank-output.v1` | `rerank_output_v1` |

`json_object`、普通文本、Markdown JSON、正则解析自由文本、协议 fallback 或“strict 失败后自由文本”均不得作为正常路径。严格 Schema 不可用时，所选 adapter 能力探测失败，不能启用 profile；不得自动切换另一个 adapter。端点不需要、也不被要求接受任意本地 Draft 2020-12 关键字；实现必须只发送探测已证明支持的 Structured Outputs 子集。

图片只能是为当前阶段裁剪的 PNG 或联系表；请求文本只能包含当前查询、短编号、当前 `prompt_version` 允许的必要机器元数据、Schema 允许的 bounded semantic fields 和约束。不得发送 SQLite、完整导出包、无关方块、文件系统路径、日志、人工秘密或 API key。`prompt.v1` 和其它历史 prompt version string 保持既有 legacy text；只有 exact `prompt.v2` 使用本节后述 slim model-visible projection。

### 3.2 `offline_annotation`

输入是 8–16 个已生成且可读的视觉变体联系表及其紧凑元数据。每个 tile 必须预先由本地生成唯一 `tile_id` 与 `variant_id` 映射。真实 wire 输出使用 `annotation-batch-output.v1`；其中 `items` 的元素语义结构为 `annotation-wire-item.v1`，落库后的持久记录为 `annotation-record.v1`。它们是 provider envelope，不是持久记录本身，只允许返回请求集合内每个 `variant_id` 恰好一次的语义对象：

```json
{
  "schema_id": "annotation-batch-output.v1",
  "items": [
    {
      "variant_id": "minecraft:yellow_carpet",
      "synonyms_zh": ["黄色薄层"],
      "synonyms_en": ["yellow thin layer"],
      "summary_zh": "黄色且非常薄，适合覆盖水平表面。",
      "summary_en": "A yellow, very thin layer for horizontal surfaces.",
      "color_terms": ["yellow"],
      "shape_terms": ["horizontal_thin_sheet"],
      "material_impressions": ["textile_like"],
      "building_roles": ["floor_covering"],
      "style_tags": ["simple"],
      "avoid_for": ["load_bearing_wall"],
      "confidence": 0.88,
      "reason": "受控视觉描述与已给出的机器几何一致。"
    }
  ]
}
```

`block_id`、`variant_id` 之外的 ID 不得由模型创建；`block_id`、状态、几何、透明度、发光、支撑、红石、发布状态和 `candidate_qualification` 不属于该输出 Schema。真实 wire Schema 的每个对象字段都必须在 `required` 中，所有 object 都必须 `additionalProperties=false`，只使用探测通过的 endpoint Structured Outputs 子集。消费端必须再次执行 wire/local Schema、tile 映射、bounded string/array、长度、重复和机器事实冲突校验；模型返回的越权字段即使服务端返回也必须拒绝并创建 high review。

#### 3.2.1 Prompt version compatibility and `prompt.v2` projection

现有 frozen run/release 的 `prompt_version` 和 exact legacy prompt behavior 必须保持不变；`prompt.v1` 可 replay，其他已存在的历史 version string 继续走 legacy behavior。禁止原地修改、自动迁移或为当前 pending job re-sign；选择 `prompt.v2` 必须使用新的 run/profile snapshot。D-042 不改变 wire Schema、output codec 或 local validation。

`prompt.v2` 的 model-visible text 必须保留 contact sheet 和 tile labels，并包含等价的 trusted instruction：

```text
Annotate only the existing labeled tiles. For every tile, copy its exact existing
variant_id. Never create or modify IDs or machine facts.
```

其输入 projection 只允许以下信息：

```text
tiles: [{"tile_id": "<existing tile id>", "variant_id": "<existing variant id>"}]
tile_metadata: [{"tile_id": "<existing tile id>",
                "geometry_classes": ["<deduplicated bounded class>", ...]}]
```

`geometry_classes` 必须去重且有界。v2 model text 不得包含 `image_sha256`、`machine_metadata_sha256`、`block_id`、`canonical_state_id`、exact dimensions/volume、任何 behavior boolean/emission、`machine_tags`、feature metrics、`feature_extractor_version`、feature `input_sha256` 或重复的 feature geometry/tags。完整 machine metadata、hashes、source images、envelope/cache/signature/release lineage 仍只保留本地并继续完整校验；local `schema_id` injection、`tile_id` codec 和 semantic-field reduction 保持 deferred，除非 diagnostics 足以支持另一个 owner decision 并物化相应 Schema change。

### 3.3 `query_spec`

`query_spec` 的输出 Schema 和语义由 [`search-and-ranking.md`](search-and-ranking.md) 定义。真实 wire Schema 固定为 `query-spec-output.v1`，不作为持久记录。provider 只负责返回严格 `QuerySpec`，不得返回候选 `block_id`、SQL、路径或发布事实。输出的 `source` 必须标识 `llm`；本地合并和硬过滤由应用完成。所有 wire 字段 required，所有 object `additionalProperties=false`，只使用探测通过的子集。

### 3.4 `visual_rerank`

视觉重排输入是本地已经召回并编号的 8–12 个候选联系表、原始查询、已校验 QuerySpec 和必要语义。真实 wire 输出固定为 `rerank-output.v1`，不是持久记录：

```json
{
  "schema_id": "rerank-output.v1",
  "ranking": [
    {"candidate_id": "A1", "fit": 0.93, "reason": "颜色和水平薄片形状匹配。"}
  ],
  "needs_user_choice": true,
  "ambiguity_points": ["材质观感未指定"],
  "suggested_followups": ["是否偏好木材、织物或金属观感？"]
}
```

`ranking[].candidate_id` 集合必须与本地候选集合完全相同、每项恰好一次；模型不得新增、删除、换写或重新解释 candidate 的 `block_id`、状态、图片、机器事实、硬约束或资格。`fit` 只能是 0–1；理由是解释文本，不是事实来源。真实 wire Schema 的每个字段都必须 required，所有 object `additionalProperties=false`，只使用探测通过的子集。候选最终顺序由本地 ID 映射重建，不能直接信任模型传回的对象。

### 3.5 三个 wire Schema 的共同子集规则

本地完整 Schema 可以使用 JSON Schema Draft 2020-12；实际发送到所选 endpoint 的三个 Schema **MUST** 使用能力探测已证明支持的 Structured Outputs 子集。实现不得把“本地 validator 能校验”当作“endpoint 能接受”。三个 wire Schema 的固定 ID、顶层字段和 required 字段如下：

| wire Schema ID | structured-output `name`（两种协议相同） | 顶层 required 字段 | 嵌套约束 |
|---|---|---|---|
| `annotation-batch-output.v1` | `annotation_batch_output_v1` | `schema_id`,`items` | `items` 的元素为 `annotation-wire-item.v1`；元素所有语义字段 required；每个 object `additionalProperties=false` |
| `query-spec-output.v1` | `query_spec_output_v1` | 由真实 `query-spec-output.v1` Schema 定义 | 精确字段、required 集合、枚举和 nested object 约束只以该 Schema 为准；`source_model_id`、`source_prompt_version` 不属于 wire output |
| `rerank-output.v1` | `rerank_output_v1` | `schema_id`,`ranking`,`needs_user_choice`,`ambiguity_points`,`suggested_followups` | ranking 元素的 `candidate_id`,`fit`,`reason` 全部 required；每个 object `additionalProperties=false` |

这些 wire Schema 只允许原始类型、数组、object、`enum`、数值/字符串边界等已由探测证明的关键字；本地 Schema 中的字符串 `const` 在 wire projection 中使用等价的 singleton `enum` 表示，不是移除约束；但仅对 `openai_chat_completions` 的 `query_spec`，wire projection 会把字符串 `enum`（包括由字符串 `const` 生成的 singleton enum）降为 `type: "string"`，Responses QuerySpec 以及 Chat annotation/rerank 保留 enum。该局部兼容来自 bukun/Gemini Chat QuerySpec 的实际观测：保留 enum 时 generic `HTTP 400`，去除 enum 但保留其它结构和边界后为 `HTTP 200`/`finish_reason=stop`；这不是所有 Chat gateway 的要求。布尔 `const` 不下发 `const` 或布尔 `enum`，wire 保留或补出 `type: "boolean"`；具体值及被降级的 enum 约束仍由响应返回后的完整本地 Schema 强制校验。不得发送任意 `$ref`、`patternProperties`、动态表达式、自由附加字段或未探测关键字。已观察到一次 bukun Chat probe 的 `HTTP 400 upstream_error`（Gemini translation gateway）因不支持 `uniqueItems` 而拒绝请求，因此该关键字可以仅在 wire projection 中省略；响应返回后仍必须使用完整本地 Schema 校验并拒绝重复数组。`minItems`/`maxItems`、`type`、`required`、`additionalProperties` 和其它 endpoint 支持的约束仍必须保留在实际 wire Schema 中，不能以 `json_object`、自由文本或“服务端自动修复”代替。本条只是 endpoint Structured Outputs 子集的 gateway 兼容记录，不是 fallback 或 Schema-ID 变化，也不改变 retention 或 adapter 边界。`annotation-record.v1` 是持久语义记录，`annotation-wire-item.v1` 是 batch 内元素；二者都不能冒充 batch wire Schema。

## 4. 能力探测和启用状态

### 4.1 探测内容

`POST /api/provider/probe` 必须使用本地原创最小 PNG fixture，并只按 profile 选定的 adapter 实际发送三份与生产相同形态的 strict Schema/name（`annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`），按顺序确认：

1. secret 可读取且认证成功；
2. `model_id` 可用；
3. endpoint 接受所选协议的图片输入（Responses 为 `input_image`，Chat 为 `image_url`）；
4. endpoint 接受所选 adapter 的 strict structured output 位置（Responses `text.format` 或 Chat `response_format.json_schema`）、对应 Schema ID、对应 `name` 和 `strict=true`，且返回严格实例；
5. 认证、权限、能力、拒绝、incomplete 和 Schema 错误可以被本地稳定分类；
6. Responses probe 实际发送 `store=false`；Chat probe 实际不发送 `store`。控制性 malformed request 必须保持所选协议的 body family：Chat 使用空 `messages`，Responses 可使用无效 strict 值。不验证 response store echo，也不把 probe 描述为远端 retention 证明。

探测不得记录完整 response 或 usage。返回字段至少为 `profile_id`、所选 `adapter`、`capability_status`、图片/Structured Outputs/错误分类/model-auth 能力、非秘密 `base_url_stable_id`、脱敏 `request_id`、`probed_at` 和 `error_code`。三阶段成功响应都必须有 string `model`；缺失或非 string 必须 fail closed，model string 与 requested `model_id` 不同则不失败，且该 echo 不得进入返回的 verified identity、持久化或 lineage。任一所选 adapter 能力失败必须 `capability_status=failed`，不得把 profile 置为 enabled。

### 4.2 retention 边界

协议字段约束不是远端保留策略证明：Responses 的 `store=false` 只表示请求契约，Chat 的省略 `store` 只表示该协议契约。两者都不得返回或展示 `storage_verified`，也不得因缺少 store echo 单独失败；第三方 retention policy 与信任由用户负责。

## 5. 重试、响应分类和最终状态

### 5.1 总重试预算

每个逻辑请求的总尝试次数最多为两次：首次尝试加一次重试。SDK 内置 retry、HTTP client retry、应用层 repair request 和 worker retry **必须共用同一预算**；实现必须关闭 SDK 默认无限/额外重试，或用 request context 计数器扣除 SDK 已使用次数。人工重新执行是新的逻辑请求，必须有新 attempt 和审计记录，不能绕过原请求的上限。

### 5.2 错误分类

| 分类 | 错误码 | 是否自动重试 | 离线最终状态 | 在线最终行为 |
|---|---|---:|---|---|
| 网络/超时 | `PROVIDER_NETWORK_ERROR`、`PROVIDER_TIMEOUT` | 最多一次 | high `needs_review` | 保留本地结果并 warning，离线 batch 继续 |
| 429/5xx | `PROVIDER_RATE_LIMITED`、`PROVIDER_SERVER_ERROR` | 最多一次 | high `needs_review` | 保留本地结果并 warning，离线 batch 继续 |
| 可修复 Schema 结果 | `PROVIDER_SCHEMA_INVALID_REPAIRABLE` | 发一次 strict 修复请求 | high `needs_review` | 本地降级并 warning |
| 最终 Schema 错误 | `PROVIDER_SCHEMA_INVALID` | 不再重试 | high `needs_review` | 本地降级并 warning |
| `refusal` | `PROVIDER_REFUSAL` | 不盲重试 | high `needs_review` | 本地降级并 warning |
| `incomplete` | `PROVIDER_INCOMPLETE` | 不盲重试 | high `needs_review` | 本地降级并 warning |
| 认证/权限/模型不可用 | `PROVIDER_AUTH_FAILED`、`PROVIDER_PERMISSION_DENIED`、`PROVIDER_MODEL_UNAVAILABLE` | 不重试 | `failed` | atomically fail job/stage/run，停止 later sends |
| 配置/能力/未配置 | `PROVIDER_NOT_CONFIGURED`、`PROVIDER_CONFIG_INVALID`、`PROVIDER_CAPABILITY_MISSING` | 不重试 | `failed` | atomically persist evidence/review/audit，停止 later sends |
| 请求非法/超限 | `PROVIDER_REQUEST_INVALID`、`PROVIDER_PAYLOAD_TOO_LARGE` | 不重试 | high `needs_review` | 本地降级并 warning |
| 用户取消 | `PROVIDER_CANCELLED` | 不重试 | control | 停止 future sends，不进入 bulk retry |

服务端返回 `refusal` 或 `incomplete` 时必须保留分类和脱敏原因，不得当作空 JSON、成功 annotation 或“模型已完成”。本地 JSON Schema 失败必须记录 `schema_error_class`、字段路径和 stage，但不得记录完整响应。5xx 默认仍是 `PROVIDER_SERVER_ERROR`/retryable；只有 non-2xx JSON `error` object 中 `code` 或 `type` 精确大小写归一化为 `invalid_request` 或 `invalid_request_error` 时，才分类为 non-retryable `PROVIDER_REQUEST_INVALID`，不读取 message。认证、权限、无能力和拒绝均不得盲重试或隐式更换 model/provider。

本地校验失败时，provider 可以把最多四条有界、确定性的校验反馈与上一次 artifact 的有界前缀作为 untrusted repair context，仅发送给现有的第二次请求；该反馈和原始响应不得持久化或进入 result、warning、数据库、日志或 UI，也不得规范化、截断后当作结果、覆盖无效 artifact 或触发 fallback。第二次仍失败时保持 `PROVIDER_SCHEMA_INVALID` 并进入审核。

离线 `offline_annotation` 在预算用尽后必须创建 high priority review；不能把空语义发布。在线 `query_spec` 或 `visual_rerank` 最终失败时，搜索必须继续使用不放宽硬约束的确定性路径，并返回 `reranked_by_llm=false`；如果 QuerySpec 无法解析，使用本地 bounded semantic fields 和用户显式词解析，未知词只作为 soft keyword，不能猜成硬条件。

### 5.2.1 FINAL annotation validation diagnostic

只有 `offline_annotation` 的 FINAL annotation validation 在总 retry budget 用尽后仍失败时，才允许产生一条 sanitized diagnostic。其字段必须且只能是：

```text
stage            = offline_annotation
phase            = json_parse | output_shape | wire_schema
path             = bounded JSON path; `$` for parse/shape
keyword          = bounded stable validator/parse keyword
observed_type    = array | boolean | number | null | object | string | missing
observed_length  = bounded non-negative integer | null
```

禁止 raw/prefix/value、provider message、exception text、repair context、prompt/image/secret 和 response/value hash。第一次 repairable failure 在第二次成功时不产生、不持久化 diagnostic。diagnostic 通过 internal `ProviderResult` 传递，并追加到既有 `PROVIDER_FAILURE` review task 的 `evidence_json`，保留现有 job/provider request refs；provider envelope、`provider_requests` column、table/report、migration 和 Schema ID 均不增加。Review/API/UI 只能以 ordinary labels 暴露这六个 allowlisted fields，不能从 `path` 派生或渲染 raw value。

该 diagnostic 只用于安全分类，不放宽 Provider-side full wire validation、Worker-side full validation、ID/hash/cache/annotation-record/variant/`VALIDATE`/release boundaries、local `uniqueItems` 或 max-one-retry。只移除 freshly produced by `_hash_json` hash 上的 tautological regex check；其它 diagnostic merge/move 只有在 externally observable classification 不变时允许。

### 5.3 批次授权、顺序提交与 Provider retry generation

手动 per-batch approval 是默认。WebUI 的一次明确 confirmation 只授权 unchanged frozen remaining batch plan；确认前每个 planned batch 都必须可 inspect，计划绑定 D-040 的 immutable plan hash、run-frozen provider 和 requested `model_id`。plan hash 精确只包含 `run_id`、`effective_config_hash` 和按顺序排列的 `job_id`、`logical_key`、recomputed payload signature；transaction 必须重新计算并执行 TOCTOU all-or-none，approve none 或批准全部 included pending jobs，写 one plan audit 和 per-job approval audits。Worker 在每次 send 前立即复核，lineage 变化时不发送。不得增加 auto-mode field、stage cursor、config snapshot 或改变 retry budget。

Automatic plan submission 的 send concurrency 由 D-044 bounded scheduler 约束：`offline_annotation` 为每个 run 冻结的整数 `1..5`（默认 `1`），`query_spec`/`visual_rerank` 固定为 `1`；它计数 logical batches，不计 HTTP attempts。一个 Python 进程只有一个 process-lifetime in-process executor，最多 `5` 个 worker slots，所有 runs 共享；global active sends `<=5`，每个 run `<=` 其 frozen offline bound。Worker request 必须使用 run-frozen profile；mutable global active profile 仅适用于新的 Studio work/profile management，不得替换既有 run 的 adapter、requested model 或 base URL。item-local error 只创建 high `needs_review` 并继续 drain；fatal `PROVIDER_NOT_CONFIGURED`、`PROVIDER_CONFIG_INVALID`、`PROVIDER_CAPABILITY_MISSING`、`PROVIDER_AUTH_FAILED`、`PROVIDER_PERMISSION_DENIED`、`PROVIDER_MODEL_UNAVAILABLE` 必须 atomically persist request evidence/review/job/stage/run failure/audit，并在 later sends 前停止。低置信度的有效结果和 item-local failure 仍进入 `VALIDATE`/`HUMAN_REVIEW`。

Provider retry 的 source 是 terminal `needs_review|failed` 的 AI job，且必须有 eligible item-local Provider error；fatal、`PROVIDER_CANCELLED` 和没有 Provider error 的 job 不 eligible。variant review 不能单独作为 source。source 必须是 leaf；child cursor 包含 `retry_of_job_id`，nonce 由 source `job_id + input_signature` 确定性生成，每个 source 一个 child，failed child 可作为下一次显式 generation。row/bulk retry 在同一 transaction resolve source 的 open provider-review siblings，同时保留 source job/evidence/provider request；重复 POST 幂等，legacy retry rows 只兼容读取、不重写。bulk action 只处理 eligible failed leaf batches 并 auto-approve retry wave；generic `retry-failed` 排除 fatal/provider AI jobs，不能让同一 logical request 超过两次总尝试。`PROVIDER_CANCELLED` 是 control，不是 bulk retry。

#### 5.3.1 D-044 发送线性化、恢复与 pristine reconfiguration

Worker 必须按 frozen plan order 使用 ordered contiguous approved claim barrier：只有从当前未完成位置开始的连续 approved prefix 可以 claim，后续 batch 不得越过未 approved、失效或停止 barrier。每个 batch 在 HTTP 前必须完成 full payload/contact sheet/prompt/machine metadata rebuild、full signature、approval/lineage、run/stage、停止信号和 active-send bound 的最终 gate。HTTP 不得处于 SQLite transaction 内；DB connection/transaction、provider client 和 provider mutable state 不得跨线程共享。

通过 final gate、占用 slot 并进入 provider HTTP call 的瞬间是 send-started linearization。pause/cancel/fatal 只阻止 claimed-unsent/later sends；already-started call 可以完成并持久化 request evidence 与 item terminal state，但不能 revive 已 failed/cancelled 的 run/stage。fatal supersedes paused，不 supersede 已 durable cancelled；不得 fake in-flight cancellation。没有 durable pending provider request reservation，也没有 remote exactly-once claim；hard crash after send before commit 最多留下 frozen concurrency 数量的 unknown outcomes。startup 不自动 resend，显式 `recover` 仍是必要恢复入口。

该唯一 executor 在进程生命周期内复用；stop 必须等待 live futures，live futures 或 DB work 存在时不能 stale-recover 或报告完成，completion 要求二者均为零。调度 concurrency 只属于 profile/run runtime scheduling，不能进入 release snapshot、`release-manifest.v1` 或 provider wire/record Schema。除合法 offline-concurrency-only profile edit（保留 `verified`/`enabled`、无需 reprobe）外，其它 invalidation 规则不变。

仅当 run/stage paused at `AI_ANNOTATE`、没有 live future/provider request、没有 provider-request evidence/annotation/AI artifact/provider or AI review/send/result/retry/cancel evidence，且每个 AI job 都是 pending、unapproved、ownerless、clean 时，才允许 strict pristine same-run reconfiguration。检查无法证明时 fail closed；通过后原子替换 frozen config/pending jobs，保留 R2/machine evidence，写 `R3_RUN_RECONFIGURED`，invalidate old plan，不重用 approval。不得新增服务、队列、per-run executor、adaptive concurrency、SQL/Schema/migration/status/dependency/CLI/fallback/retry 语义或伪造 cancellation。

### 5.4 响应保留边界

系统只可保留：脱敏 provider `request_id`（稳定截断或哈希）、stage、attempt、错误分类、耗时 bucket、输入哈希、输出规范化 artifact 哈希和 validated AI artifact。系统 **MUST NOT** 保留完整 provider response、原始文本、usage、prompt、图片或 Authorization。AI artifact 是按 Schema 解析后的最小字段，不是 response dump。

## 6. 缓存和版本冻结

### 6.1 AI cache key

每个 AI 逻辑请求的缓存键必须同时包含以下字段，缺一不可：

```text
image_hash
machine_metadata_hash
adapter
prompt_version
model_id
schema_version
base_url_stable_id
stage
```

`stage` 只能是 `offline_annotation`、`query_spec` 或 `visual_rerank`。实现应对按字段排序的 canonical JSON 计算 `sha256:<64 lowercase hex>` 作为 `cache_key`，并额外记录 `minecraft_version`、候选集合哈希和 resolved release manifest hash 作为防跨版本引用的上下文。`base_url_stable_id` 不是 secret，也不得用完整 URL query 携带租户 key。

缓存命中条件是 key 相同、artifact Schema 通过、候选映射一致、输入图片和机器 metadata hash 再验证通过。缓存只能保存 validated artifact、版本字段、hash、脱敏 request ID 和状态；不得保存完整 response 或未校验模型文本。失败响应不构成成功缓存。

### 6.2 发布冻结

发布前必须把每个 AI artifact 与以下版本/哈希绑定并写入 release manifest 或发布索引：`adapter`、`profile_id`、`model_id`、`base_url_stable_id`、`secret_reference`、`prompt_version`、对应 stage 的 wire `schema_id`、`search_ranking_version`、`image_hash`、`machine_metadata_hash`、`cache_key` 和 `artifact_hash`。发布后这些字段只能读取；任一变化必须构建新的不可变 release。发布门和 `quality_report.json` 见 [`quality-and-testing.md`](quality-and-testing.md)。

`prompt.v2` 通过 frozen `prompt_version` 改变 signature/cache identity，必须使用 fresh run/profile snapshot；当前 pending 的 v1 jobs 保持 untouched/paused，不自动 cancel、delete 或 re-sign。旧 release 和其它历史 prompt version string 继续按 legacy behavior replay。

## 7. Provider 错误码和返回对象

统一内部 `ProviderResult` 必须是严格 allowlist 对象，不含 usage 或完整响应；`validation_diagnostic` 只在 D-042 的 final offline annotation failure 中携带 allowlisted object，且不是 provider envelope 或持久化 Schema 字段：

```json
{
  "status": "succeeded|retryable_failure|needs_review|failed",
  "adapter": "openai_responses|openai_chat_completions",
  "stage": "offline_annotation|query_spec|visual_rerank",
  "wire_schema_id": "annotation-batch-output.v1|query-spec-output.v1|rerank-output.v1",
  "parsed_artifact": "validated object or null",
  "request_id_redacted": "req_…abcd",
  "attempts_used": 1,
  "error_code": null,
  "error_class": null,
  "validation_diagnostic": "one allowlisted object or null",
  "cache_key": "sha256:<64 lowercase hex>",
  "artifact_hash": null,
  "warnings": []
}
```

稳定错误码至少包括：`PROVIDER_NOT_CONFIGURED`、`PROVIDER_CONFIG_INVALID`、`PROVIDER_CAPABILITY_MISSING`、`PROVIDER_STORAGE_UNSUPPORTED`（仅旧兼容诊断码，不是 retention 能力门）、`PROVIDER_AUTH_FAILED`、`PROVIDER_PERMISSION_DENIED`、`PROVIDER_MODEL_UNAVAILABLE`、`PROVIDER_NETWORK_ERROR`、`PROVIDER_TIMEOUT`、`PROVIDER_RATE_LIMITED`、`PROVIDER_SERVER_ERROR`、`PROVIDER_REQUEST_INVALID`、`PROVIDER_PAYLOAD_TOO_LARGE`、`PROVIDER_REFUSAL`、`PROVIDER_INCOMPLETE`、`PROVIDER_SCHEMA_INVALID_REPAIRABLE`、`PROVIDER_SCHEMA_INVALID`、`PROVIDER_OUTPUT_ID_MISMATCH`、`PROVIDER_MACHINE_FACT_CONFLICT`、`PROVIDER_UNKNOWN`、`PROVIDER_CACHE_KEY_INVALID`、`IDEMPOTENCY_CONFLICT`、`PROVIDER_CANCELLED`。`PROVIDER_STORAGE_UNSUPPORTED` 若意外出现在当前流程中，必须按 `PROVIDER_UNKNOWN` 的 high `needs_review` 处理；`PROVIDER_CANCELLED` 仍是 control signal。

错误消息必须可操作、脱敏且不回显 provider body。未知错误使用 `PROVIDER_UNKNOWN`，离线进入审核，在线走本地降级；绝不切换 provider 或 model。

## 8. 可执行验收

实现可提供 fake selected-adapter endpoint 或脱敏协议 fixture 验证请求形状和客户端分类，但 fake endpoint 本身不能证明生产 endpoint 的 retention policy；还必须验证：

1. 三阶段 × 两种 adapter 的六种请求形状均有覆盖：Responses `POST /responses`、`store=false`、`input_text/input_image`、`text.format`；Chat `POST /chat/completions`、省略 `store`、`text/image_url`、`response_format`。
2. 两种 adapter 都使用同一个 configured/requested `model_id`、同三份 Schema/name、图片输入、strict output 和本地 ID/机器事实校验；`offline_annotation`、`query_spec`、`visual_rerank` 三个阶段均必须发送非空 PNG。成功响应必须有 string `model`，但不同 echo 不失败、不持久化且不替换 requested `model_id`；不存在 `json_object`、自由文本或协议 fallback 正常路径。
3. Probe 只验证选定 adapter：Responses 发送 `store=false` 但不检查 store/model echo equality；Chat 检查请求没有 `store` 且不检查 model echo equality；两者都验证 image、strict、错误分类、requested model/auth，并不宣称 retention 或远端模型身份已验证。
4. Chat 仅按 `choices[0]`、`refusal`、`finish_reason=stop` 和 JSON string content 解析；其它 incomplete/failure 分支不得正常 fallback。SDK retry 加应用 retry 的总尝试数不超过 2。
5. profile 只有一个活动 model/adapter；改变 adapter、model、base URL/Schema/semantic constraints 会改变 cache key 和 run snapshot；协议不得自动切换。
6. cache key 缺任一字段即失败：`image_hash`、`machine_metadata_hash`、`adapter`、`prompt_version`、`model_id`、`schema_version`、`base_url_stable_id`、`stage`；缓存不含完整 response 或 usage。
7. 既有 `openai_responses` profile、release 和 fixture 仍可读取；协议变更前的 in-flight cache/workspace 必须失效并 rerun，不迁移；keyring 优先于环境变量，SQLite/快照/日志/前端只出现 `secret_reference` 或掩码。
8. provider artifact 在 release freeze 后能由 manifest 中的 adapter、版本和哈希复核，且不产生 Token、费用、预算、secret 或完整 response 字段。
9. 两种 adapter 的 `prompt.v2` model-visible text 只含 trusted instruction、contact-sheet tile labels、`tile_id`/`variant_id` 和去重有界 `geometry_classes`；local envelope/hash checks 仍使用 full metadata。`prompt.v1` 与其它历史 version string 保持当前 legacy behavior，source change 触发 TOCTOU，v2 使用 fresh run。
10. malformed JSON、missing required、wrong type、additional property 和 duplicate-array 的 final failure 只通过 internal `ProviderResult`/existing review evidence 保留六个 allowlisted diagnostic fields；successful repair 不保留 diagnostic，DB/API/UI 不出现 raw output/value/prefix/secret/path-like value。
11. Provider-side/Worker-side full validation、ID/hash/cache/record/variant/`VALIDATE`/release gates、local uniqueItems、max-one-retry 和现有 Provider/Worker/release acceptance 不因 v2 或 diagnostic 放宽；prompt size comparison 仅作为 evidence。

精确测试分层和目标平台命令以 [`quality-and-testing.md`](quality-and-testing.md) 为准；本文件不把不存在的测试报告、真实数据或 provider 响应宣称为已完成。
