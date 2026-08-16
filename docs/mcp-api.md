# MCP API 契约

## 文档状态、优先级与关联规范

本文定义 `block-index mcp` 的 stdio 进程、release 解析、四个且仅四个工具、只读边界和说明性响应示例。精确 input/output 字段形状唯一由 `schemas/mcp/` 下的真实 Schema 文件拥有；本文示例不构成重复的穷举规范。正文使用简体中文；MCP 方法、字段名、Schema 标识、状态、错误码和命令保持英文。MCP 当前输出 Schema ID 固定为 `mcp-index-info-output.v1`、`mcp-search-blocks-output.v1`、`mcp-block-details-output.v1`、`mcp-compare-blocks-output.v1`，错误可共享 `mcp-error.v1`。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 为规范性关键字。

本文服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md)、[`decisions.md`](decisions.md) 和 [`architecture.md`](architecture.md)，并与 [`product-scope.md`](product-scope.md) 保持一致。原始稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。检索语义见 [`search-and-ranking.md`](search-and-ranking.md)，provider 规则见 [`openai-provider.md`](openai-provider.md)，数据和发布边界见 [`data-and-schemas.md`](data-and-schemas.md)、[`export-contract.md`](export-contract.md) 与 [`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)，发布门见 [`quality-and-testing.md`](quality-and-testing.md)，WebUI 写边界见 [`webui-and-operations.md`](webui-and-operations.md)，安全边界见 [`security-and-distribution.md`](security-and-distribution.md)。

## 1. 传输、进程和只读边界

### 1.1 唯一传输和命令

MVP 的 MCP 只能由以下 Python 命令启动：

```text
block-index mcp [--data-root <path>]
```

进程 **MUST** 只使用 `stdio` JSON-RPC/MCP transport。MVP **MUST NOT** 提供 Streamable HTTP、HTTP endpoint、MCP `resources`、MCP `prompts`、任意 SQL、任意文件读取、写入工具、索引修改工具或 provider 配置工具。Python 产品命令完整边界见 [`webui-and-operations.md`](webui-and-operations.md)；不得新增 `block-index search`、`block-index publish` 等命令。

stdout 从首字节开始 **MUST** 只输出 MCP 协议 JSON-RPC 消息；日志、诊断、堆栈、启动提示和调试信息 **MUST** 只输出 stderr，**MUST NOT** 写 data-root `logs/`、cache、临时文件或其他本地持久化位置。进程不能把图片 base64 直接写成独立 stdout 行，图片只能作为协议响应 content。

### 1.2 release 只读解析

MCP 每次启动或打开查询都必须：

1. 从数据根读取 `current.json`，解析 `current-pointer.v1` 的 `default_minecraft_version` 和 `versions` map；
2. 校验 `minecraft_version`、`release_id`、相对路径安全性、`manifest_sha256`、release `checksums` 和质量门状态，并读取 release 冻结的 provider snapshot（包括 `adapter`）；
3. 只读打开不可变 `releases/<minecraft_version>/<release_id>/index.sqlite3` 及同目录 PNG/metadata；
4. 拒绝 `workspace`、cache、导出源目录、任意用户路径和未完整 release；
5. 在一次请求中固定解析结果，不能跨版本或在同一响应中混用 release。

MCP 请求的 `minecraft_version` 可以省略；省略时使用 `current-pointer.v1.default_minecraft_version`，显式版本时读取该版本的 current release。未知、未发布、哈希不匹配或不可变标志缺失时失败，并列出可用精确版本，**MUST NOT** 自动回退。MCP input 不支持 `release`/`release_id` selector；历史 release 选择只能由 WebUI rollback 完成。响应只返回 `resolved_release_id` 和 manifest hash，不返回 selector 歧义。

MCP 进程 **MUST NOT** 写 SQLite、图片、release、cache、日志、data-root logs、临时文件、`current.json` 或任何任务状态。即使调用 provider 进行 QuerySpec/重排，也只能按已解析 release 冻结的 `adapter`、`profile_id`、`model_id`、`base_url_stable_id` 和 `secret_reference` 使用 snapshot；`adapter` 只能是 `openai_responses` 或 `openai_chat_completions`，决定唯一 wire codec；不得读取或比较可变 active profile，也不得跨协议 fallback。provider 结果只能在内存中使用。secret 无法解析或运行时能力不满足 snapshot 时，必须返回 warning 并使用确定性本地降级。联系表必须在内存构造，图片以进程内 bytes 直接形成 `ImageContent`，在线查询失败不能写缓存或生成联系表到持久目录。

## 2. MCP 能力和响应 envelope

### 2.1 工具集合

`tools/list` 必须且只能返回以下四个工具，顺序固定：

```text
index_info
search_blocks
get_block_details
compare_blocks
```

每个工具必须暴露 strict `inputSchema` 和 strict `outputSchema`（JSON Schema Draft 2020-12，`additionalProperties=false`，未知字段拒绝）。四个工具的 output object 都必须包含 `schema_version`、`request_id`、resolved `minecraft_version`、`resolved_release_id`、`manifest_sha256`、`warnings` 和工具专属 `data`；`schema_version` 必须分别固定为 `mcp-index-info-output.v1`、`mcp-search-blocks-output.v1`、`mcp-block-details-output.v1` 或 `mcp-compare-blocks-output.v1`。`structuredContent` 与 TextContent 必须由同一个经过 Schema 校验的对象序列化产生。MCP Schema 属于 MCP 命名空间，不得复用 provider envelope 或持久记录 ID。

### 2.2 成功响应

工具成功时，`CallToolResult` 必须满足：

```json
{
  "isError": false,
  "structuredContent": {
    "schema_version": "mcp-index-info-output.v1",
    "request_id": "mcp_01J",
    "minecraft_version": "26.2",
    "resolved_release_id": "rel_01J",
    "manifest_sha256": "sha256:<64 lowercase hex>",
    "warnings": [],
    "data": {}
  },
  "content": [
    {"type": "text", "text": "<JSON serialization of the same envelope object>"}
  ]
}
```

当响应有图片时，`content` 还必须包含由同一 `structuredContent` 的 `images` metadata 顺序生成的 `ImageContent`；二者的 `content_index`、`image_id` 和 candidate/tile 映射必须一致。TextContent 必须是等价 JSON，不得只写摘要而省略结构化字段。`structuredContent` 本身不得包含绝对路径或图片 base64；图片 bytes 只出现在 ImageContent。

成功但 provider 不可用、QuerySpec 未调用或视觉重排失败时仍为 `isError=false`，返回确定性本地结果、warning 和 `reranked_by_llm=false`。硬过滤正常得到空集也是 `isError=false` 的成功空结果；未知 ID、release unavailable、图片读取失败等工具执行失败使用 `isError=true`，见第 6 节。

### 2.3 图片 metadata

`images` 是 output data 的稳定 metadata 数组，每项至少为：

```json
{
  "image_id": "img_A1",
  "purpose": "search_contact_sheet|compare_contact_sheet|block_variant_views",
  "mime_type": "image/png",
  "width": 512,
  "height": 512,
  "sha256": "sha256:<64 lowercase hex>",
  "content_index": 1,
  "mapping": [
    {"candidate_id": "A1", "variant_id": "minecraft:yellow_carpet", "block_id": "minecraft:yellow_carpet"}
  ]
}
```

`mapping` 中的 ID 必须全部来自 release，不能由模型产生。`purpose`、MIME、尺寸、hash 和 content index 必须可由实际 PNG 重算；图片读取失败不能返回占位图。

## 3. 通用 inputSchema

四个工具的公共字段。`minecraft_version` 为可选；四个工具不接受 `release`、`release_id` 或其他历史 selector：

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [],
  "properties": {
    "minecraft_version": {"type": "string", "const": "26.2"}
  }
}
```

省略 `minecraft_version` 时必须使用 default version；显式版本必须严格查找其 current pointer。任何 `release`、`release_id`、`selector` 输入均为未知字段并返回 `-32602`。未来版本不能通过把 `const` 改为范围来静默支持；新增 Minecraft 版本需生成对应契约/Schema 和显式发布数据。

## 4. `index_info`

### 4.1 输入

```json
{
  "minecraft_version": "26.2"
}
```

除公共字段外不得有字段。`index_info` 不调用 provider，不返回图片，不写入任何状态。

### 4.2 输出 `mcp-index-info-output.v1`

`data` 必须是：

```json
{
  "schema_version": "mcp-index-info-output.v1",
  "product": "Blockpedia",
  "official_disclaimer": "NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.",
  "minecraft_version": "26.2",
  "release_id": "rel_01J",
  "built_at": "2026-08-13T12:00:00Z",
  "counts": {
    "blocks": 0,
    "visual_variants": 0,
    "audited_skips": 0
  },
  "schema_versions": {
    "block": "block-record.v1",
    "state": "state-record.v1",
    "variant": "visual-variant-record.v1",
    "annotation": "annotation-record.v1",
    "release_manifest": "release-manifest.v1",
    "release": "release.v1",
    "current_pointer": "current-pointer.v1"
  },
  "prompt_version": "prompt.v1",
  "search_ranking_version": "search-ranking.v1",
  "fts_mode": "trigram|normalized_like",
  "quality_gate": {"passed": true, "quality_report_sha256": "sha256:<64 lowercase hex>"},
  "warnings": [],
  "images": []
}
```

没有完整当前 release、质量门未通过或 hash 不匹配时不得返回半可信统计，使用 `isError=true`。产品身份声明必须原文出现，不得使用官方 logo/字体/素材。

## 5. `search_blocks`

### 5.1 搜索输入契约

`minecraft_version` 可省略；省略时使用 `default_minecraft_version` 对应的 current。MCP 输入不接受 `release`、`selector` 或 `release_id`。

```json
{
  "minecraft_version": "26.2",
  "query": "黄色的扁片方块，用于屋檐，不要红石组件",
  "limit": 8,
  "context": {
    "family": null,
    "compare_states": false,
    "rerank": "auto"
  }
}
```

字段约束：

| 字段 | 约束 |
|---|---|
| `query` | 必填，1–2000 Unicode 字符；不得是 SQL 或路径指令 |
| `limit` | 可选整数 1–12，默认 8；它不改变 Top-24 阶段 |
| `context.family` | null 或 release 中已存在 family |
| `context.compare_states` | boolean，默认 false |
| `context.rerank` | `auto`、`local_only`、`required`，默认 `auto` |

### 5.2 处理和输出 `mcp-search-blocks-output.v1`

处理必须严格遵守 [`search-and-ranking.md`](search-and-ranking.md)：解析 release → hard filter → FTS/字段评分 → Top-24 → family 去重（默认最多 2 个）→ 8–12 联系表 → 同一个 `adapter`/`model_id` 的 strict visual rerank。输出 `data` 至少包含：

```json
{
  "schema_version": "mcp-search-blocks-output.v1",
  "search_id": "S_01J",
  "query": "黄色的扁片方块，用于屋檐，不要红石组件",
  "query_spec": {},
  "hard_filters": [
    {"field": "behavior.redstone_related", "operator": "exclude", "value": true, "source": "user_explicit"}
  ],
  "top_24": {"count": 0, "candidate_ids": []},
  "family_dedupe": {"max_per_family": 2, "relaxed": false, "reason": null},
  "candidates": [
    {
      "candidate_id": "A1",
      "variant_id": "minecraft:yellow_carpet",
      "block_id": "minecraft:yellow_carpet",
      "display_name": "黄色地毯",
      "recommended_state": "minecraft:yellow_carpet",
      "qualification": "eligible",
      "score": 0.91,
      "score_source": "local|llm_rerank",
      "score_breakdown": {"shape": 0.0, "color": 0.0, "use": 0.0, "name_synonym": 0.0, "style": 0.0, "behavior": 0.0},
      "reason": "",
      "warnings": [],
      "image_id": "img_A1"
    }
  ],
  "contact_sheet": {
    "image_id": "img_contact",
    "tile_mapping": [{"candidate_id": "A1", "variant_id": "minecraft:yellow_carpet", "block_id": "minecraft:yellow_carpet"}]
  },
  "images": [],
  "reranked_by_llm": false,
  "visual_constraints_verified": false,
  "needs_user_choice": false,
  "ambiguity_points": [],
  "suggested_followups": [],
  "warnings": [],
  "search_ranking_version": "search-ranking.v1"
}
```

`top_24` 是本地召回事实，不得因 `limit` 只记录最终候选。`candidate_id` 只在本次响应内稳定，图片映射、结构对象和 TextContent 必须一一对应。LLM 失败、未配置或不要求时，`reranked_by_llm=false`，并在 warnings 中给出 `PROVIDER_*`/`RERANK_SKIPPED`；不允许伪装成模型已重排。

硬过滤后为空是正常的成功业务结果：`isError=false`，返回空 `candidates`/联系表、已应用约束、排除摘要和建议追问；不得返回通过放宽 hard constraint 得到的候选。`NO_CANDIDATES` 只能作为兼容诊断字段，不能把正常空集升级为工具错误。

## 6. `get_block_details`

### 6.1 详情输入契约

`minecraft_version` 可省略；省略时使用 `default_minecraft_version` 对应的 current。MCP 输入不接受 `release`、`selector` 或 `release_id`。

```json
{
  "minecraft_version": "26.2",
  "block_id": "minecraft:bamboo_trapdoor"
}
```

`block_id` 必须匹配 `^minecraft:[a-z0-9_./-]+$`，只能作为 release 查找键，不能拼接 SQL 或路径。未知 ID 返回 `BLOCK_NOT_FOUND`，不能让模型猜 ID。

### 6.2 输出 `mcp-block-details-output.v1`

`data` 必须包含：

```json
{
  "schema_version": "mcp-block-details-output.v1",
  "block_id": "minecraft:bamboo_trapdoor",
  "official_names": {"zh_cn": "竹活板门", "en_us": "Bamboo Trapdoor"},
  "translation_key": "block.minecraft.bamboo_trapdoor",
  "default_state": "minecraft:bamboo_trapdoor[...]",
  "properties": {},
  "legal_states": [],
  "machine_facts": {},
  "variants": [
    {
      "variant_id": "minecraft:bamboo_trapdoor",
      "canonical_state": "minecraft:bamboo_trapdoor[...]",
      "represented_states": [],
      "qualification": "conditional",
      "warnings": ["requires_support_below"],
      "machine_facts": {},
      "annotation": {},
      "images": [
        {"image_id": "img_minecraft_bamboo_trapdoor", "purpose": "block_variant_views", "view_set": "isometric/front/side/top"}
      ]
    }
  ],
  "images": [],
  "audit": {"skip_records": [], "override_refs": []},
  "warnings": []
}
```

details 必须为每个主要可发布视觉变体返回四视角 PNG `ImageContent`（可将每个变体的四视角卡作为一张 512×512 PNG，或按冻结 render Schema 的单张四视角卡返回），结构化 metadata 必须记录 `image_id`、MIME、尺寸、hash、purpose 和映射。无发布图片的 skipped 项只返回审计状态和原因，不能返回占位图片。机器事实只来自 release，annotation/override 来源必须分开。

## 7. `compare_blocks`

### 7.1 比较输入契约

`minecraft_version` 可省略；省略时使用 `default_minecraft_version` 对应的 current。MCP 输入不接受 `release`、`selector` 或 `release_id`。

```json
{
  "minecraft_version": "26.2",
  "block_ids": [
    "minecraft:yellow_carpet",
    "minecraft:bamboo_trapdoor",
    "minecraft:light_weighted_pressure_plate"
  ],
  "context": "用于屋檐底部",
  "compare_states": false
}
```

`block_ids` 必须为 2–6 个不重复的合法 `minecraft:` ID；所有 ID 必须在同一 release。`context` 可为 0–1000 字符；`compare_states=true` 时允许输出多状态/多变体并解除 family 去重限制，但不能超出给定 block IDs。未知 ID 返回明确 `BLOCK_NOT_FOUND` 及所有无效 ID，不部分成功或替换候选。

### 7.2 输出 `mcp-compare-blocks-output.v1`

`data` 至少包含：

```json
{
  "schema_version": "mcp-compare-blocks-output.v1",
  "block_ids": ["minecraft:yellow_carpet", "minecraft:bamboo_trapdoor"],
  "rows": [
    {
      "field": "shape",
      "values": [
        {"block_id": "minecraft:yellow_carpet", "value": "horizontal_thin_sheet", "source": "machine"},
        {"block_id": "minecraft:bamboo_trapdoor", "value": "horizontal_thin_sheet", "source": "machine"}
      ]
    }
  ],
  "recommendations": [],
  "contact_sheet": {"image_id": "img_compare", "tile_mapping": []},
  "images": [],
  "reranked_by_llm": false,
  "warnings": []
}
```

compare 的结构差异只能由 release 机器/已审核语义产生。若提供 `context`，可以使用 resolved release 冻结的 provider snapshot 中同一 `adapter`/`model_id` 做候选解释或重排，但不得读取可变 active profile、跨协议 fallback，也不得新增 block ID、状态、图片或事实。必须返回稳定编号 PNG 联系表 ImageContent，`tile_mapping` 覆盖全部给定的有效 block/variant；图片内容和结构数据共用同一 mapping。

## 8. 错误分层和稳定错误码

### 8.1 协议层错误

JSON-RPC/MCP 协议错误表示请求没有正确调用工具，使用标准 `error`，不使用工具结果 `isError`：

| code | 条件 |
|---:|---|
| `-32700` | JSON 无法解析 |
| `-32600` | JSON-RPC envelope 非法 |
| `-32601` | 方法不存在；工具集合之外的调用 |
| `-32602` | inputSchema/参数非法、未知字段、类型或范围错误 |
| `-32603` | 未分类 server 内部错误；详情写 stderr，不回显秘密 |

`tools/call` 的工具名不是四个允许值时使用 `-32601`；参数不符合工具 inputSchema 使用 `-32602`。协议错误不能伪装为 `isError=true` 工具业务对象。

### 8.2 工具执行错误

参数结构有效但 release、数据或查询执行失败时，必须返回 `isError=true`，`structuredContent` 与 TextContent 仍来自同一个 `mcp-error.v1` 对象：

```json
{
  "schema_version": "mcp-error.v1",
  "request_id": "mcp_01J",
  "error_code": "VERSION_NOT_AVAILABLE",
  "message": "目标版本未发布。",
  "retryable": false,
  "minecraft_version": "26.2",
  "available_versions": ["26.2"],
  "details": {},
  "warnings": [],
  "images": []
}
```

稳定业务错误码至少包括：

| `error_code` | 条件 | `retryable` |
|---|---|---:|
| `DATA_ROOT_INVALID` | data root 不存在/不安全 | 否 |
| `CURRENT_POINTER_MISSING` | current 缺失 | 否 |
| `CURRENT_POINTER_INVALID` | current JSON/路径/hash 非法 | 否 |
| `VERSION_REQUIRED` | 保留为 WebUI/API 兼容错误；MCP 省略 `minecraft_version` 使用 default | 否 |
| `VERSION_NOT_AVAILABLE` | 未知、未发布或未配置版本；列可用版本 | 否 |
| `RELEASE_NOT_FOUND` | current pointer 指向的 release 不存在 | 否 |
| `RELEASE_NOT_BUILT` | release 未通过 candidate-build gate | 否 |
| `RELEASE_INTEGRITY_FAILED` | manifest/checksum/quality hash 失败 | 否 |
| `INDEX_OPEN_FAILED` | 只读数据库无法打开 | 否 |
| `INDEX_INFO_UNAVAILABLE` | index metadata 缺失 | 否 |
| `QUERY_INVALID` | 搜索输入业务非法 | 否 |
| `QUERY_PARSE_FAILED` | 无法得到安全 QuerySpec | 否 |
| `NO_CANDIDATES` | 兼容诊断码；正常 hard filter 空集返回成功空结果 | 否 |
| `BLOCK_NOT_FOUND` | ID 不在 release | 否 |
| `BLOCK_ID_INVALID` | ID 格式非法 | 否 |
| `COMPARE_COUNT_INVALID` | compare 少于 2 或多于 6 | 否 |
| `IMAGE_READ_FAILED` | release PNG 不可读 | 否 |
| `IMAGE_MAPPING_INVALID` | 图片和结构 mapping 不一致 | 否 |
| `PROVIDER_NOT_CONFIGURED` | release snapshot 的 secret 无法解析；仅 auto 搜索可降级 | 否 |
| `PROVIDER_CAPABILITY_MISSING` | release snapshot 要求的图片/strict 能力在运行时不满足；仅 auto 搜索可降级 | 否 |
| `PROVIDER_AUTH_FAILED` | release snapshot provider 认证失败；仅 auto 搜索可降级 | 否 |
| `PROVIDER_REFUSAL` | 所选 adapter refusal；仅 auto 搜索可降级 | 否 |
| `PROVIDER_INCOMPLETE` | 所选 adapter incomplete/non-stop；仅 auto 搜索可降级 | 否 |
| `PROVIDER_SCHEMA_INVALID` | strict 结果 Schema 失败；仅 auto 搜索可降级 | 否 |
| `RERANK_REQUIRED_UNAVAILABLE` | `rerank=required` 但重排不可用 | 否 |
| `READ_ONLY_VIOLATION` | 代码尝试写 release/workspace/current | 否 |
| `MCP_INTERNAL_ERROR` | 未分类执行错误 | 否 |

在线 `auto` 降级不是错误：应返回 `isError=false`、本地候选、warnings 和 `reranked_by_llm=false`。`required` 无法重排时必须 `isError=true`，不得把 warning 当成功。错误消息不能包含 API key、Authorization、完整 response、绝对路径或 SQL。

## 9. 只读和 stdout 验收

必须以子进程执行 `block-index mcp` 和原创 fixture 检查：

1. `tools/list` 严格只有四个工具；不存在 HTTP、resources、任意 SQL/文件读写接口。
2. 每行 stdout 可独立解析为 JSON-RPC/MCP 消息；stderr 可有诊断但不进入 stdout。
3. 省略版本使用 `default_minecraft_version`，显式版本使用该版本 current；未知/未发布版本和 hash mismatch 均符合错误表，不回退；历史 `release_id` selector 被拒绝。
4. 四工具只读不可变 release；运行所有成功、失败、降级和图片路径分支后，SQLite、文件、cache、logs 和 current hash 不变。
5. `structuredContent` 与 TextContent JSON 深相等；成功降级 `isError=false`，工具执行错误 `isError=true`，协议错误使用标准 JSON-RPC error。
6. `search_blocks`/`compare_blocks` 返回稳定编号 PNG 联系表 ImageContent；`get_block_details` 返回四视角 PNG；`index_info` 无图片。
7. 图片 metadata 含 ID、MIME、尺寸、hash、purpose、content index 和映射，不含绝对路径；mapping 与 PNG 联系表、结构候选 100% 一致。
8. provider 不可用不改变候选事实，返回 warnings 和 `reranked_by_llm=false`；`rerank=required` 正确报错。
9. 无真实 Minecraft 资产的原创 PNG/SQLite fixture 通过协议测试；缺少真实本地 release 只能报告 `SKIPPED_LOCAL_RELEASE_MISSING`，不得伪造通过。
