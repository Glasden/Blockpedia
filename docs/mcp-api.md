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

以下是非特定客户端的说明性配置形状；`<local-data-root>` 只是用户本地数据根占位符：

```json
{
  "mcpServers": {
    "blockpedia": {
      "command": "block-index",
      "args": ["mcp", "--data-root", "<local-data-root>"]
    }
  }
}
```

使用现有默认数据根时，可省略 `--data-root` 及其参数。该示例不引入额外命令、host/port、HTTP 或 transport selector。

进程 **MUST** 只使用 `stdio` JSON-RPC/MCP transport。MVP **MUST NOT** 提供 Streamable HTTP、HTTP endpoint、MCP `resources`、MCP `prompts`、任意 SQL、任意文件读取、写入工具、索引修改工具或 provider 配置工具。Python 产品命令完整边界见 [`webui-and-operations.md`](webui-and-operations.md)；不得新增 `block-index search`、`block-index publish` 等命令。

stdout 从首字节开始 **MUST** 只输出 MCP 协议 JSON-RPC 消息；日志、诊断、堆栈、启动提示和调试信息 **MUST** 只输出 stderr，**MUST NOT** 写 data-root `logs/`、cache、临时文件或其他本地持久化位置。进程不能把图片 base64 直接写成独立 stdout 行，图片只能作为协议响应 content。

### 1.2 release 只读解析

D-052 冻结为“仅构建时验证”。release 完整性、Schema/checksum/manifest/quality/index/PNG 预验证由 WebUI build/activation gate 负责；MCP **MUST NOT** 在启动或查询时首次全量验证，也 **MUST NOT** 逐请求验证 current/manifest/checksum/schema/file identity/hash、复算质量门或全量验证 PNG/index projection。MCP 不得声称运行时验证 immutable、manifest hash、checksums、schema inventory、quality report 或 index format。

MCP 每次请求只执行以下最小解析和读取：

1. 从数据根读取并解析 `current.json` 的 `current-pointer.v1`，按省略/显式 `minecraft_version` 取得 default/精确版本对应的 pointer、`release_id` 和 `relative_path`；下一请求必须重新观察 pointer，pointer 指向变化时载入新 snapshot；
2. 严格校验 tool input/version 语义，拒绝 `release`/`release_id` 历史 selector；执行相对路径防逃逸、根外引用拒绝和明显 symlink/junction/reparse 拒绝。这些是安全边界，不是 release 完整性验证；
3. 只打开 pointer 指定的 index，并在生成实际响应时按需读取所需 PNG/metadata；不得主动扫描 release 目录、复算 hash 或全量验证 projection。current、pointer、路径、index/SQLite 或 PNG 读取失败必须 fail closed，不得回退其它 release；
4. 不读取 workspace、exports、Keyring、可变 active profile 或 provider snapshot。MCP runtime 完全不初始化或调用 AI provider；release 中的 provider snapshot/AI annotations 仅作为离线 lineage/搜索内容保留。

MCP 可在进程内按 `minecraft_version+release_id` 缓存 snapshot，但缓存不能持久化；下一请求观察到 pointer 指向变化时必须载入新 snapshot。MCP 进程 **MUST NOT** 写 SQLite、图片、release、cache、日志、data-root logs、临时文件、`current.json` 或任何任务状态。MCP 不读取 Keyring 或 provider snapshot 作在线查询。联系表必须在内存构造，图片以进程内 bytes 直接形成 `ImageContent`，查询失败不能写缓存或生成联系表到持久目录。

## 2. MCP 能力和响应 envelope

### 2.1 工具集合

`tools/list` 必须且只能返回以下四个工具，顺序固定：

```text
index_info
search_blocks
get_block_details
compare_blocks
```

每个工具必须暴露 strict `inputSchema`。advertised `outputSchema` 必须是 strict `oneOf`，组合该工具既有成功 Schema（分别为 `mcp-index-info-output.v1`、`mcp-search-blocks-output.v1`、`mcp-block-details-output.v1`、`mcp-compare-blocks-output.v1`）与既有 `mcp-error.v1`；这是协议广告组合，不新增 Schema ID 或修改任何真实 Schema。成功和工具错误的结构对象仍必须分别通过既有 Schema，`isError=false/true` 语义保持不变。四个成功 output object 都必须包含 `schema_version`、`request_id`、resolved `minecraft_version`、`resolved_release_id`、`manifest_sha256`、`warnings` 和工具专属 `data`；`structuredContent` 与 TextContent 必须由同一个经过 Schema 校验的对象序列化产生。MCP Schema 属于 MCP 命名空间，不得复用 provider envelope 或持久记录 ID。

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

当前 D-053 搜索不初始化 provider、不执行 QuerySpec、hard filtering 或 visual rerank。关键词无召回是 `isError=false` 的成功空结果，输出 `hard_filters=[]`、`reranked_by_llm=false`；未知 ID、release unavailable、图片读取失败等工具执行失败使用 `isError=true`，见第 6 节。

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

### 2.4 D-052 provider budget（历史语义，已由 D-053 supersede）

D-052 的 55/15/30 秒 provider deadline、provider retry、`auto`/`local_only`/`required`、provider/client/preview reuse 只作为历史契约保留，不是当前 MCP runtime 语义。D-053 的 `search_blocks` 完全不发送 provider 请求；D-052 保留的同步本地查询移出 event loop、snapshot cache、outputSchema `oneOf` 和 parity 仍有效。

## 3. 通用 inputSchema

四个工具的公共字段。`minecraft_version` 为可选；四个工具不接受 `release`、`release_id` 或其他历史 selector：

```json
{
  "type": "object",
  "additionalProperties": false,
  "required": [],
  "properties": {
    "minecraft_version": {"type": "string", "pattern": "^[0-9]{1,3}\\.[0-9]{1,3}(?:\\.[0-9]{1,3})?$"}
  }
}
```

省略 `minecraft_version` 时必须使用 default version；显式输入必须匹配上述严格版本格式并查找其 current pointer。格式非法、类型错误或其它 input shape 错误返回 JSON-RPC `-32602`；格式合法但未发布版本返回 `VERSION_NOT_AVAILABLE` 且不回退。任何 `release`、`release_id`、`selector` 输入均为未知字段并返回 `-32602`。该 pattern 不增加当前版本支持，Minecraft baseline 仍为 `26.2`；新增 Minecraft 版本需生成对应发布数据和契约收敛。

上述对象是四工具共享的公共输入部分；`search_blocks` 的完整 input 只按第 5.1 节增加 required `keywords` 和可选 `limit`。

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

`quality_gate`、`quality_report_sha256` 和 `manifest_sha256` 若出现在 release-provided metadata 中，只能原样作为已构建 release 的声明性元数据返回，不能解释为 MCP 在运行时重新验证。MCP 不因未执行这些预验证而声称完整性；只有 current/pointer、index/SQLite 或实际响应 PNG 的读取失败才 fail closed。产品身份声明必须原文出现，不得使用官方 logo/字体/素材。

## 5. `search_blocks`

### 5.1 搜索输入契约

`minecraft_version` 可省略；省略时使用 `default_minecraft_version` 对应的 current。MCP 输入不接受 `release`、`selector` 或 `release_id`。D-053 是破坏性的 input contract change：不提供旧字段兼容或自动转换。

```json
{
  "minecraft_version": "26.2",
  "keywords": ["yellow", "carpet", "roof trim"],
  "limit": 8
}
```

字段约束：

| 字段 | 约束 |
|---|---|
| `keywords` | 必填 Unicode string array，1–16 项；每项 trim 后 1–64 个 Unicode 字符；禁止空项和重复项 |
| `minecraft_version` | 可选；必须匹配严格版本 pattern |
| `limit` | 可选整数 1–12，默认 8；它不改变 Top-24 阶段 |
| 旧 `query`/`context`/`query_spec` | 不支持；作为未知字段返回 JSON-RPC `-32602`，不提供兼容转换 |

### 5.1.1 D-051 historical input (superseded)

本节保留 D-051 的历史输入契约说明。D-053 已整体 supersede 它；当前 MCP 不接收 `query_spec`，也不初始化/调用 provider。`query-spec-output.v1` 继续由 provider/历史 lineage 契约保留。

旧 `query_spec`、`QUERY_INVALID` 和 server-side QuerySpec generation 的输入语义只作为历史记录，不属于当前 D-053 MCP runtime。

上述 host/provider/rerank 行为已由 D-053 删除；当前输入只允许 `keywords`、可选 `minecraft_version` 和 `limit`。

自然语言解析、hard parser、hard filtering 和 soft/hard intent 合并已由 D-053 从 MCP 删除；宿主 LLM 负责这些对话职责并生成 keywords。

上述 D-051 semantic invariants 保留为历史行为记录，不是当前 MCP input/error contract。

当前 MCP 不构造 original/effective QuerySpec，也不执行 visual rerank；只对本地 trimmed keywords 做 deterministic recall/ranking。

### 5.2 处理和输出 `mcp-search-blocks-output.v1`

处理必须严格遵守 [`search-and-ranking.md`](search-and-ranking.md)：解析 release → trim/validate keywords → 对 eligible/conditional 做本地 FTS5 trigram 或 normalized `LIKE` 并集召回 → 确定性 local 排序 → 截取 Top-24 → 按 `limit` 生成联系表。MCP 不执行自然语言解析、QuerySpec、hard filter、provider call 或 visual rerank。`mcp-search-blocks-output.v1` 的 envelope 和 `data` **MUST** 严格使用闭合 Schema 的字段，不得加入任何未声明的输入、召回或解释性输出字段。输出 `data` 的完整 shape 以真实 Schema 为唯一 owner：

```json
{
  "schema_version": "mcp-search-blocks-output.v1",
  "request_id": "mcp_01J",
  "minecraft_version": "26.2",
  "resolved_release_id": "rel_01J",
  "manifest_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "warnings": [],
  "data": {
    "search_id": "S_01J",
    "query": "yellow carpet roof trim",
    "hard_filters": [],
    "exclusion_summary": [],
    "candidates": [],
    "contact_sheet": {"image_id": null, "tile_mapping": []},
    "images": [],
    "reranked_by_llm": false
  }
}
```

`data` 只能包含 `mcp-search-blocks-output.v1` 声明的成员；每个 candidate（如有）必须且只能包含 Schema 要求的 `candidate_id`、`variant_id`、`block_id`、`display_name`、`recommended_state_id`、`candidate_qualification`、`local_score`、`final_score`、`score_source`、`score_breakdown`、`reason`、`warnings` 和 `machine_fact_refs`；联系表和图片分别只能使用 Schema 定义的 `contact_sheet`、`images` 形状。任何 host input 都不得通过 output envelope 回显。

`Top-24` 只是本地召回的内部阶段，不是 output member；不得因 `limit` 把它写入响应。`search_id` 按规范化后的 keywords、版本和 release identity 稳定生成；`candidate_id` 只在本次响应内稳定，图片映射、结构对象和 TextContent 必须一一对应。每个 candidate 的 `score_source` 必须为 `local`，`reranked_by_llm` 必须为 `false`。

关键词本地召回为空是正常的成功业务结果：返回闭合 Schema 允许的空 `candidates`、空联系表和 `hard_filters=[]`；不得为正常空集生成 `mcp-error.v1` 或自动放宽/改写关键词。

## 6. `get_block_details`

### 6.1 详情输入契约

`minecraft_version` 可省略；省略时使用 `default_minecraft_version` 对应的 current。MCP 输入不接受 `release`、`selector` 或 `release_id`。

```json
{
  "minecraft_version": "26.2",
  "block_id": "minecraft:bamboo_trapdoor"
}
```

`block_id` 必须匹配 `^minecraft:[a-z0-9_./-]+$`，只能作为 release 查找键，不能拼接 SQL 或路径。格式非法属于 input shape 错误，必须在工具执行前返回 JSON-RPC `-32602`；格式合法但 ID 不在 release 时才返回 `BLOCK_NOT_FOUND`，不能让模型猜 ID。

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

`block_ids` 必须为 2–6 个不重复的合法 `minecraft:` ID；所有 ID 必须在同一 release。数量、重复项、ID 格式、类型或其它 input shape 不合法时，必须在工具执行前返回 JSON-RPC `-32602`，不得生成 `mcp-error.v1` 工具结果。`context` 可为 0–1000 字符；`compare_states=true` 时允许输出给定 block IDs 的多状态/多变体，但不引入 family 分组、限制或 metadata。格式合法但未知 ID 返回明确 `BLOCK_NOT_FOUND` 及所有无效 ID，不部分成功或替换候选。

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

compare 的结构差异只能由 release 机器/已审核语义产生。`compare_blocks` 的现有 `context` 仅作为本次本地比较的非-provider说明性输入；不得把它解释为 provider 配置或重排选项。compare 不调用 provider，不读取 Keyring、active profile 或 provider snapshot，也不得新增 block ID、状态、图片或事实。必须返回稳定编号 PNG 联系表 ImageContent，`tile_mapping` 覆盖全部给定的有效 block/variant；图片内容和结构数据共用同一 mapping。

## 8. 错误分层和稳定错误码

### 8.1 协议层错误

JSON-RPC/MCP 协议错误表示请求没有正确调用工具，使用标准 `error`，不使用工具结果 `isError`：

| code | 条件 |
|---:|---|
| `-32700` | JSON 无法解析 |
| `-32600` | JSON-RPC envelope 非法 |
| `-32601` | JSON-RPC method 不存在（不是已支持的协议 method） |
| `-32602` | inputSchema/参数非法、未知字段、类型或范围错误；合法 `tools/call` 中的 tool name 不存在 |
| `-32603` | 未分类 server 内部错误；详情写 stderr，不回显秘密 |

未知 JSON-RPC method 使用 `-32601`；method 为合法 `tools/call` 但 tool name 不是四个允许值时使用 Invalid Params `-32602`；参数不符合工具 inputSchema 也使用 `-32602`。协议错误不能伪装为 `isError=true` 工具业务对象。

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

稳定业务错误码（`mcp-error.v1.error_code` 的现有 enum）如下：

| `error_code` | 条件 | `retryable` |
|---|---|---:|
| `DATA_ROOT_INVALID` | data root 不存在/不安全 | 否 |
| `CURRENT_POINTER_MISSING` | current 缺失 | 否 |
| `CURRENT_POINTER_INVALID` | current JSON、版本/pointer shape、路径安全边界非法 | 否 |
| `VERSION_NOT_AVAILABLE` | 未知、未发布或未配置版本；列可用版本 | 否 |
| `RELEASE_NOT_FOUND` | current pointer 指向的 release 不存在 | 否 |
| `RELEASE_NOT_BUILT` | 仅由 build/activation gate 使用；MCP 运行时不推断 release 是否通过该门 | 否 |
| `RELEASE_INTEGRITY_FAILED` | 仅由 build/activation gate 或其报告使用；MCP 运行时不验证 manifest/checksum/quality/index format/immutable，因此不得以这些预验证失败声称运行时发现该错误 | 否 |
| `INDEX_OPEN_FAILED` | 只读数据库无法打开 | 否 |
| `INDEX_INFO_UNAVAILABLE` | index metadata 缺失 | 否 |
| `QUERY_INVALID` | 历史 QuerySpec 业务错误；当前 D-053 keywords path 不产生该错误 | 否 |
| `QUERY_PARSE_FAILED` | 历史自然语言/QuerySpec 路径错误；当前 MCP 不解析自然语言 | 否 |
| `HARD_CONSTRAINT_UNSUPPORTED` | 历史 hard-filter 路径错误；当前 MCP 不执行 hard filtering | 否 |
| `BLOCK_NOT_FOUND` | ID 不在 release | 否 |
| `IMAGE_READ_FAILED` | release PNG 不可读 | 否 |
| `IMAGE_MAPPING_INVALID` | 图片和结构 mapping 不一致 | 否 |
| `RERANK_REQUIRED_UNAVAILABLE` | 历史 `rerank=required` 路径错误；当前 MCP 不接受 rerank | 否 |
| `READ_ONLY_VIOLATION` | 代码尝试写 release/workspace/current | 否 |
| `MCP_INTERNAL_ERROR` | 未分类执行错误 | 否 |

`mcp-error.v1.error_code` 只能使用上表中现有 Schema enum 值；历史 QuerySpec/provider/rerank enum 保留但当前 D-053 MCP 不产生它们。`VERSION_REQUIRED` 不适用于 MCP，因为省略版本使用 default；`NO_CANDIDATES` 不是错误，正常关键词空搜索直接成功；非法 keywords、旧字段、`block_id`、compare 数量和其它 input shape 在工具执行前使用 JSON-RPC `-32602`。错误消息不能包含 API key、Authorization、完整 response、绝对路径或 SQL。

## 9. 只读和 stdout 验收

必须以子进程执行 `block-index mcp` 和原创 fixture 检查：

1. `tools/list` 严格只有四个工具；不存在 HTTP、resources、任意 SQL/文件写入接口；每个 advertised `outputSchema` 是成功 Schema 与 `mcp-error.v1` 的 strict `oneOf`，不新增 Schema ID。
2. 每行 stdout 可独立解析为 JSON-RPC/MCP 消息；stderr 可有诊断但不进入 stdout。
3. 省略版本使用 `default_minecraft_version`；malformed `minecraft_version` 返回 JSON-RPC `-32602`，格式合法但未发布版本返回 `VERSION_NOT_AVAILABLE` 且不回退；显式版本使用该版本 current；current/pointer、路径、index/SQLite 或实际 PNG 读取失败均按错误表 fail closed；MCP 不做 hash mismatch、v1/index format、quality 或 immutable 的运行时完整性声称。非法 keywords、旧 `query`/`context`/`query_spec`、`block_id`、compare 数量和其它 input shape 返回 JSON-RPC `-32602`；关键词空集成功，格式合法但未知 ID 才返回 `BLOCK_NOT_FOUND`；历史 release_id selector 被拒绝。
4. 四工具只读 pointer-resolved release；运行所有成功、失败、降级和图片路径分支后，SQLite、文件、cache、logs 和 current 不产生写入；允许进程内 snapshot cache，不得持久化。
5. `structuredContent` 与 TextContent JSON 深相等；成功降级 `isError=false`，工具执行错误 `isError=true`，协议错误使用标准 JSON-RPC error；未知 RPC method 返回 `-32601`，合法 `tools/call` 的未知 tool name 返回 Invalid Params `-32602`。
6. `search_blocks`/`compare_blocks` 返回稳定编号 PNG 联系表 ImageContent；`get_block_details` 返回四视角 PNG；`index_info` 无图片；PNG 只在实际响应需要时按需读取。
7. 图片 metadata 含 ID、MIME、尺寸、hash、purpose、content index 和映射，不含绝对路径；mapping 与 PNG 联系表、结构候选 100% 一致。
8. MCP 不初始化/调用 provider，不读取 Keyring、active profile 或 provider snapshot；candidate 始终为 local，`reranked_by_llm=false`。
9. focused fixture 只验证 pointer/default/显式版本切换、路径逃逸/明显链接或 reparse 拒绝、指定 index/按需 PNG 的读取失败 fail closed、keywords strict input、outputSchema strict `oneOf`、parity、snapshot refresh、event-loop isolation 和 zero writes；不以 fixture 声称 MCP 运行时验证 v2/index format、manifest/checksum/schema/quality/PNG 全量完整性。缺少真实本地 release 只能报告 `SKIPPED_LOCAL_RELEASE_MISSING`，不得伪造通过。
