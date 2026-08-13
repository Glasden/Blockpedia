# OpenAI Responses Provider 契约

## 文档状态、优先级与关联规范

本文是 Blockpedia MVP 的 OpenAI provider 行为契约。精确 wire/profile 字段形状唯一由 `schemas/provider/` 下的真实 Schema 文件拥有；本文示例仅用于说明行为。正文使用简体中文；`OpenAIResponsesProvider`、字段名、Schema 标识、状态、错误码和命令保持英文。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 使用规范性含义。

本文件服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md) 和 [`decisions.md`](decisions.md)，并与 [`product-scope.md`](product-scope.md) 和 [`architecture.md`](architecture.md) 保持一致。原始设计稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。数据来源和发布边界还必须遵守 [`data-and-schemas.md`](data-and-schemas.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)、[`export-contract.md`](export-contract.md) 与 [`state-policy-and-rendering.md`](state-policy-and-rendering.md)。

相关实现契约：

- [`search-and-ranking.md`](search-and-ranking.md)：`QuerySpec`、本地召回、视觉重排和在线降级；
- [`mcp-api.md`](mcp-api.md)：MCP 工具对 provider 失败的外部表现；
- [`webui-and-operations.md`](webui-and-operations.md)：profile 配置、能力探测、任务和审核写入口；
- [`quality-and-testing.md`](quality-and-testing.md)：provider contract tests 和发布门；
- [`security-and-distribution.md`](security-and-distribution.md)：秘密、最小披露和分发禁入物。

## 1. 冻结范围

### 1.1 唯一适配器和活动 profile

MVP **MUST** 只实现 `OpenAIResponsesProvider`，请求协议只能是 OpenAI Responses。MVP **MUST NOT** 实现或调用 OpenAI Chat Completions、Anthropic Messages、其他 provider adapter、Embedding、独立视觉模型、模型投票、隐式 provider fallback 或隐式模型切换。

系统可以保存多个非活动 profile，以便用户保留不同的非秘密配置；全局 **MUST** 至多有一个 `enabled=true` 的活动 profile。该约束仅适用于 Studio 新任务、配置管理和构建新 release。活动 profile 及其同一个运行时 `model_id` 只控制 Studio 的新写任务和新 release，并必须同时用于以下三个阶段：

```text
offline_annotation
query_spec
visual_rerank
```

同一 release 的离线标注、QuerySpec 和视觉重排必须使用同一 release-bound `model_id`。`model_id` 是运行时配置，不得硬编码；每个 run 的非秘密配置快照、每个 AI 产物和最终 release manifest **MUST** 记录实际使用的 `profile_id`、`model_id`、`base_url_stable_id`、`secret_reference`、prompt/schema/search 版本。非活动 profile 不得用于 Studio 新写任务或新 release；但 release-bound MCP 必须使用已解析 release 冻结的 provider snapshot，不读取或比较可变的 active profile。切换 Studio active profile 不影响旧 release，release snapshot 也不算另一个 active profile。改变其中任一值后，旧缓存不得被当作新输入的成功结果，必须产生新的输入签名和新的 release。`secret_reference` 只保存引用，不保存 key。

### 1.2 兼容 `base_url`

默认 `base_url` 为实现配置中的 OpenAI Responses endpoint。用户可以保存并明确批准一个兼容同一 OpenAI Responses 语义的 `base_url`；这不是第二 provider adapter，也不改变唯一的 `OpenAIResponsesProvider`。兼容 endpoint 只有同时通过图片输入、实际 Responses Structured Outputs strict Schema 和错误分类探测才可继续探测 `store=false`。兼容 endpoint **MUST NOT** 因“看起来像 OpenAI”而跳过完整能力门。

`base_url` 不能携带 query、fragment、用户名、密码或 API key。系统必须规范化并保存非秘密稳定标识 `base_url_stable_id`：保留 scheme、host、显式 port 和规范化 path，去掉尾随 `/`，拒绝凭证与 query；稳定标识不得包含秘密。原始本地绝对路径和 Authorization 不得进入 provider 请求或 `base_url_stable_id`。

`store=false` 是硬启用门。探测必须实际发送 `store=false`，并从响应/协议行为确认 endpoint 接受且按该语义处理。官方或兼容 endpoint 不能证明实际 `store=false` 时，probe **MUST** 为 `failed`、错误码为 `PROVIDER_STORAGE_UNSUPPORTED`，并且 **MUST NOT** enable；不存在可绕过该硬门的确认或豁免路径。

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
| `adapter` | 只能是 `openai_responses` |
| `base_url` | 绝对 HTTPS URL；本地测试 endpoint 可以使用受控的 `http://127.0.0.1`，不得使用非 loopback 明文远端 |
| `model_id` | 非空、长度不超过 200；不允许以 `latest` 或范围表达；由 endpoint 实际解析 |
| `secret_reference` | 只能是 `keyring:blockpedia/<profile_id>` 或 `env:OPENAI_API_KEY` 形式，不是 key 值 |
| `capability_status` | `draft`、`unverified`、`verified`、`failed`；不存在可绕过能力门的 `warning` |
| `annotation_output_schema_id` | 固定为 `annotation-batch-output.v1` |
| `query_spec_output_schema_id` | 固定为 `query-spec-output.v1` |
| `rerank_output_schema_id` | 固定为 `rerank-output.v1` |
| `search_ranking_version` | 固定为当前搜索排序配置，例如 `search-ranking.v1` |
| `request_timeout_ms` | 1000–600000 的整数 |
| `batch_size` | `offline_annotation` 为 8–16；在线阶段固定为 1 |
| `concurrency` | 1–4；不能借此绕过 provider 限流或重试上限 |

profile 必须满足以下启用不变量：`adapter=openai_responses`、`enabled=true`、三个阶段的 model 解析值相同、能力状态严格为 `verified`、秘密可读取、实际 wire Schema ID 固定、Schema/prompt/search 版本存在，并且 `store=false` 硬门通过。保存非活动 profile 不需要探测，但不得用于 Studio 新写任务或新 release；release-bound MCP 例外使用已解析 release 冻结的 provider snapshot，不读取或比较可变 active profile。一个 profile 被启用后不得再启用第二个；变更活动 profile 必须停止新 AI job，并在新 run 中使用新快照。

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

### 3.1 通用 Responses 请求

Studio 三类新写调用都必须使用同一个活动 profile、同一个 `model_id` 和 `base_url`；release-bound MCP 的 QuerySpec/视觉重排必须使用该 release 冻结的同一个 provider snapshot/model，不读取或比较可变 active profile。两者都使用以下语义。下方 `schema` 不是任意 Draft 2020-12 的声明：本地完整 Schema 可使用 Draft 2020-12，但真实 Responses Structured Outputs wire Schema 必须是端点支持的子集，且使用第 3.2–3.4 节独立 Schema ID。

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

实现可以使用 SDK 等价的结构化参数，但发出的协议语义必须等价。三类请求 **MUST** 设置 `text.format.type=json_schema`、对应独立 Schema ID、对应 `name` 和 `strict=true`。`name` 与 Schema ID 分离，且只能匹配 `[A-Za-z0-9_-]{1,64}`：

| wire Schema ID | Responses `text.format.name` |
|---|---|
| `annotation-batch-output.v1` | `annotation_batch_output_v1` |
| `query-spec-output.v1` | `query_spec_output_v1` |
| `rerank-output.v1` | `rerank_output_v1` |

`json_object`、普通文本、Markdown JSON、正则解析自由文本或“strict 失败后自由文本”均不得作为正常回退。严格 Schema 不可用时，能力探测失败，不能启用 profile。端点不需要、也不被要求接受任意本地 Draft 2020-12 关键字；实现必须只发送探测已证明支持的 Structured Outputs 子集。

图片只能是为当前阶段裁剪的 PNG 或联系表；请求文本只能包含当前查询、短编号、必要机器元数据、Schema 允许的 bounded semantic fields 和约束。不得发送 SQLite、完整导出包、无关方块、文件系统路径、日志、人工秘密或 API key。

### 3.2 `offline_annotation`

输入是 8–16 个已生成且可读的视觉变体联系表及其紧凑元数据。每个 tile 必须预先由本地生成唯一 `tile_id` 与 `variant_id` 映射。真实 wire 输出使用 `annotation-batch-output.v1`；其中 `items` 的元素语义结构为 `annotation-wire-item.v1`，落库后的持久记录为 `annotation-record.v1`。它们是 provider envelope，不是持久记录本身，只允许返回请求集合内每个 `variant_id` 恰好一次的语义对象：

```json
{
  "schema_id": "annotation-batch-output.v1",
  "items": [
    {
      "variant_id": "vv_7c5e",
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

本地完整 Schema 可以使用 JSON Schema Draft 2020-12；实际发送到 Responses endpoint 的三个 Schema **MUST** 使用端点能力探测已证明支持的 Structured Outputs 子集。实现不得把“本地 validator 能校验”当作“endpoint 能接受”。三个 wire Schema 的固定 ID、顶层字段和 required 字段如下：

| wire Schema ID | Responses `name` | 顶层 required 字段 | 嵌套约束 |
|---|---|---|---|
| `annotation-batch-output.v1` | `annotation_batch_output_v1` | `schema_id`,`items` | `items` 的元素为 `annotation-wire-item.v1`；元素所有语义字段 required；每个 object `additionalProperties=false` |
| `query-spec-output.v1` | `query_spec_output_v1` | `schema_id`,`source`,`source_model_id`,`source_prompt_version`,`hard`,`soft`,`ambiguities`,`needs_user_choice`,`suggested_followups`,`unknown_terms` | `hard`、`soft`、每个约束项、ambiguity 项和数组元素对象的字段全部 required；每个 object `additionalProperties=false` |
| `rerank-output.v1` | `rerank_output_v1` | `schema_id`,`ranking`,`needs_user_choice`,`ambiguity_points`,`suggested_followups` | ranking 元素的 `candidate_id`,`fit`,`reason` 全部 required；每个 object `additionalProperties=false` |

这些 wire Schema 只允许原始类型、数组、object、`enum`、`const`、数值/字符串边界等已由探测证明的关键字；不得发送任意 `$ref`、`patternProperties`、动态表达式、自由附加字段或未探测关键字。所有数组、字符串、数值和枚举的约束必须同时存在于本地 Schema 和实际 wire Schema；不能以 `json_object`、自由文本或“服务端自动修复”代替。`annotation-record.v1` 是持久语义记录，`annotation-wire-item.v1` 是 batch 内元素；二者都不能冒充 batch wire Schema。

## 4. 能力探测和启用状态

### 4.1 探测内容

`POST /api/provider/probe` 必须使用本地原创最小 PNG fixture，并实际发送三份与生产相同形态的 strict Responses Structured Outputs Schema、对应 `name`（`annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`）和实际 `store=false`，按顺序确认：

1. secret 可读取且认证成功；
2. `model_id` 可用；
3. endpoint 接受 `input_image`；
4. endpoint 接受 `text.format.type=json_schema`、对应 Schema ID、对应 `name` 和 `strict=true`，且返回严格实例；
5. 认证、权限、能力、拒绝、incomplete 和 Schema 错误可以被本地稳定分类；
6. endpoint 接受实际 `store=false`，并能由响应/协议行为证明未启用存储。

探测不得记录完整 response 或 usage。返回字段为 `profile_id`、`capability_status`、图片/Structured Outputs/错误分类/实际 `store=false` 四项能力、非秘密 `base_url_stable_id`、脱敏 `request_id`、`probed_at` 和 `error_code`。任一项失败必须 `capability_status=failed`，不得把 profile 置为 enabled。

### 4.2 `store=false` hard gate

若 endpoint 不支持、不能证明或拒绝实际 `store=false`，探测返回 `capability_status=failed`、`error_code=PROVIDER_STORAGE_UNSUPPORTED`。WebUI 必须拒绝 enable；不得提供可绕过硬门的确认或豁免路径。所有 AI 任务保持 `blocked`，不会偷偷发送请求。

## 5. 重试、响应分类和最终状态

### 5.1 总重试预算

每个逻辑请求的总尝试次数最多为两次：首次尝试加一次重试。SDK 内置 retry、HTTP client retry、应用层 repair request 和 worker retry **必须共用同一预算**；实现必须关闭 SDK 默认无限/额外重试，或用 request context 计数器扣除 SDK 已使用次数。人工重新执行是新的逻辑请求，必须有新 attempt 和审计记录，不能绕过原请求的上限。

### 5.2 错误分类

| 分类 | 错误码 | 是否自动重试 | 离线最终状态 | 在线最终行为 |
|---|---|---:|---|---|
| 网络/超时 | `PROVIDER_NETWORK_ERROR`、`PROVIDER_TIMEOUT` | 最多一次 | `needs_review` | 保留本地结果并 warning |
| 429/5xx | `PROVIDER_RATE_LIMITED`、`PROVIDER_SERVER_ERROR` | 最多一次 | `needs_review` | 保留本地结果并 warning |
| 可修复 Schema 结果 | `PROVIDER_SCHEMA_INVALID_REPAIRABLE` | 发一次 strict 修复请求 | `needs_review` | 本地降级并 warning |
| 最终 Schema 错误 | `PROVIDER_SCHEMA_INVALID` | 不再重试 | high `needs_review` | 本地降级并 warning |
| `refusal` | `PROVIDER_REFUSAL` | 不盲重试 | high `needs_review` | 本地降级并 warning |
| `incomplete` | `PROVIDER_INCOMPLETE` | 不盲重试 | high `needs_review` | 本地降级并 warning |
| 认证 | `PROVIDER_AUTH_FAILED` | 不重试 | `failed` | 本地降级并 warning |
| 权限/模型不可用 | `PROVIDER_PERMISSION_DENIED`、`PROVIDER_MODEL_UNAVAILABLE` | 不重试 | `failed` | 本地降级并 warning |
| 能力不支持 | `PROVIDER_CAPABILITY_MISSING` | 不重试 | `failed` | 本地降级并 warning |
| 请求非法/超限 | `PROVIDER_REQUEST_INVALID`、`PROVIDER_PAYLOAD_TOO_LARGE` | 不重试 | `needs_review` | 本地降级并 warning |
| 用户取消 | `PROVIDER_CANCELLED` | 不重试 | `failed` | 本地结果不变 |

服务端返回 `refusal` 或 `incomplete` 时必须保留分类和脱敏原因，不得当作空 JSON、成功 annotation 或“模型已完成”。本地 JSON Schema 失败必须记录 `schema_error_class`、字段路径和 stage，但不得记录完整响应。认证、权限、无能力和拒绝均不得盲重试或隐式更换 model/provider。

离线 `offline_annotation` 在预算用尽后必须创建 high priority review；不能把空语义发布。在线 `query_spec` 或 `visual_rerank` 最终失败时，搜索必须继续使用不放宽硬约束的确定性路径，并返回 `reranked_by_llm=false`；如果 QuerySpec 无法解析，使用本地 bounded semantic fields 和用户显式词解析，未知词只作为 soft keyword，不能猜成硬条件。

### 5.3 响应保留边界

系统只可保留：脱敏 provider `request_id`（稳定截断或哈希）、stage、attempt、错误分类、耗时 bucket、输入哈希、输出规范化 artifact 哈希和 validated AI artifact。系统 **MUST NOT** 保留完整 provider response、原始文本、usage、prompt、图片或 Authorization。AI artifact 是按 Schema 解析后的最小字段，不是 response dump。

## 6. 缓存和版本冻结

### 6.1 AI cache key

每个 AI 逻辑请求的缓存键必须同时包含以下字段，缺一不可：

```text
image_hash
machine_metadata_hash
prompt_version
model_id
schema_version
base_url_stable_id
stage
```

`stage` 只能是 `offline_annotation`、`query_spec` 或 `visual_rerank`。实现应对按字段排序的 canonical JSON 计算 `sha256:<64 lowercase hex>` 作为 `cache_key`，并额外记录 `minecraft_version`、候选集合哈希和 resolved release manifest hash 作为防跨版本引用的上下文。`base_url_stable_id` 不是 secret，也不得用完整 URL query 携带租户 key。

缓存命中条件是 key 相同、artifact Schema 通过、候选映射一致、输入图片和机器 metadata hash 再验证通过。缓存只能保存 validated artifact、版本字段、hash、脱敏 request ID 和状态；不得保存完整 response 或未校验模型文本。失败响应不构成成功缓存。

### 6.2 发布冻结

发布前必须把每个 AI artifact 与以下版本/哈希绑定并写入 release manifest 或发布索引：`profile_id`、`model_id`、`base_url_stable_id`、`secret_reference`、`prompt_version`、对应 stage 的 wire `schema_id`、`search_ranking_version`、`image_hash`、`machine_metadata_hash`、`cache_key` 和 `artifact_hash`。发布后这些字段只能读取；任一变化必须构建新的不可变 release。发布门和 `quality_report.json` 见 [`quality-and-testing.md`](quality-and-testing.md)。

## 7. Provider 错误码和返回对象

统一内部 `ProviderResult` 必须是严格 allowlist 对象，不含 usage 或完整响应：

```json
{
  "status": "succeeded|retryable_failure|needs_review|failed",
  "stage": "offline_annotation|query_spec|visual_rerank",
  "wire_schema_id": "annotation-batch-output.v1|query-spec-output.v1|rerank-output.v1",
  "parsed_artifact": "validated object or null",
  "request_id_redacted": "req_…abcd",
  "attempts_used": 1,
  "error_code": null,
  "error_class": null,
  "cache_key": "sha256:<64 lowercase hex>",
  "artifact_hash": null,
  "warnings": []
}
```

稳定错误码至少包括：`PROVIDER_NOT_CONFIGURED`、`PROVIDER_CONFIG_INVALID`、`PROVIDER_CAPABILITY_MISSING`、`PROVIDER_STORAGE_UNSUPPORTED`、`PROVIDER_AUTH_FAILED`、`PROVIDER_PERMISSION_DENIED`、`PROVIDER_MODEL_UNAVAILABLE`、`PROVIDER_NETWORK_ERROR`、`PROVIDER_TIMEOUT`、`PROVIDER_RATE_LIMITED`、`PROVIDER_SERVER_ERROR`、`PROVIDER_REQUEST_INVALID`、`PROVIDER_PAYLOAD_TOO_LARGE`、`PROVIDER_REFUSAL`、`PROVIDER_INCOMPLETE`、`PROVIDER_SCHEMA_INVALID_REPAIRABLE`、`PROVIDER_SCHEMA_INVALID`、`PROVIDER_OUTPUT_ID_MISMATCH`、`PROVIDER_MACHINE_FACT_CONFLICT`、`PROVIDER_CANCELLED`。

错误消息必须可操作、脱敏且不回显 provider body。未知错误使用 `PROVIDER_UNKNOWN`，离线进入审核，在线走本地降级；绝不切换 provider 或 model。

## 8. 可执行验收

实现必须提供 fake Responses endpoint 或脱敏协议 fixture，验证：

1. 三阶段都发送同一个 `model_id`、图片输入（QuerySpec 可无图但能力必须已探测）、`store=false` 和 `json_schema/strict=true`；不存在 `json_object` 或自由文本正常路径。
2. profile 只有一个活动 model；改变 model/base URL/Schema/semantic constraints 会改变 cache key 和 run snapshot。
3. `store=false` 不支持或不能证明时 probe fail、enable 被硬阻断；不存在可绕过硬门的确认或豁免路径。
4. SDK retry 加应用 retry 的总尝试数不超过 2；认证、权限、无能力、refusal、incomplete 不重试；可修复 Schema 只进行一次 strict 修复。
5. 离线最终失败创建 high review；在线最终失败返回本地结果、warning 和 `reranked_by_llm=false`，不放宽硬约束。
6. cache key 缺任一八项字段即测试失败；缓存不含完整 response 或 usage。
7. keyring 优先于环境变量，SQLite/快照/日志/前端只出现 `secret_reference` 或掩码。
8. provider artifact 在发布冻结后能由 manifest 中的版本和哈希复核，且不产生 Token、费用或预算字段。

精确测试分层和目标平台命令以 [`quality-and-testing.md`](quality-and-testing.md) 为准；本文件不把不存在的测试报告、真实数据或 provider 响应宣称为已完成。
