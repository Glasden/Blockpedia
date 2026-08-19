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

MCP 每次启动或打开查询都必须：

1. 从数据根读取 `current.json`，解析 `current-pointer.v1` 的 `default_minecraft_version` 和 `versions` map；
2. 校验 `minecraft_version`、`release_id`、相对路径安全性、`manifest_sha256`、release `checksums`、`index.sqlite3` 的 fresh v2 format 和质量门状态，并读取 release 冻结的 provider snapshot（包括 `adapter`）；host-supplied QuerySpec 只在内存中消费，不改变该 snapshot；
3. 只读打开不可变且 `schema_meta.format_version=2` 的 `releases/<minecraft_version>/<release_id>/index.sqlite3` 及同目录 PNG/metadata；v1 index 必须以 `RELEASE_INTEGRITY_FAILED`、`details.integrity_component="index"` 拒绝，不得迁移或降级读取；
4. 拒绝 `workspace`、cache、导出源目录、任意用户路径和未完整 release；
5. 在一次请求中固定解析结果，不能跨版本或在同一响应中混用 release。

MCP 请求的 `minecraft_version` 可以省略；省略时使用 `current-pointer.v1.default_minecraft_version`，显式输入必须匹配严格版本格式 `^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$`，格式非法返回 JSON-RPC `-32602`。格式合法但未知、未发布或没有 current 的版本返回 `VERSION_NOT_AVAILABLE`，列出可用精确版本且 **MUST NOT** 自动回退。哈希不匹配、index v1、index format 非 2 或不可变标志缺失时返回 `RELEASE_INTEGRITY_FAILED`；MCP input 不支持 `release`/`release_id` selector；历史 release 选择只能由 WebUI rollback 完成。响应只返回 `resolved_release_id` 和 manifest hash，不返回 selector 歧义。

MCP 进程 **MUST NOT** 写 SQLite、图片、release、cache、日志、data-root logs、临时文件、`current.json` 或任何任务状态。未提供 host-supplied QuerySpec 时，若调用 provider 生成 QuerySpec，也只能按已解析 release 冻结的 `adapter`、`profile_id`、`model_id`、`base_url_stable_id` 和 `secret_reference` 使用 snapshot；提供有效 host spec 后只可跳过该生成步骤。任何 visual rerank 仍只能使用同一 snapshot；`adapter` 只能是 `openai_responses` 或 `openai_chat_completions`，决定唯一 wire codec；不得读取或比较可变 active profile，也不得跨协议 fallback。provider 结果只能在内存中使用。secret 无法解析或运行时能力不满足 snapshot 时，必须返回 warning 并使用确定性本地降级。联系表必须在内存构造，图片以进程内 bytes 直接形成 `ImageContent`，在线查询失败不能写缓存或生成联系表到持久目录。

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

`rerank=auto` 中 provider 不可用、QuerySpec 未调用或视觉重排失败时仍为 `isError=false`，返回确定性本地结果、warning 和 `reranked_by_llm=false`；消费 host-supplied QuerySpec 本身不构成重排，也不能设置 `reranked_by_llm=true` 或 `score_source=llm_rerank`。`rerank=required` 无法重排时使用 `isError=true` 的 `RERANK_REQUIRED_UNAVAILABLE` 工具错误。硬过滤正常得到空集也是 `isError=false` 的成功空结果；未知 ID、release unavailable、图片读取失败等工具执行失败使用 `isError=true`，见第 6 节。

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
    "minecraft_version": {"type": "string", "pattern": "^[0-9]{1,3}\\.[0-9]{1,3}(?:\\.[0-9]{1,3})?$"}
  }
}
```

省略 `minecraft_version` 时必须使用 default version；显式输入必须匹配上述严格版本格式并查找其 current pointer。格式非法、类型错误或其它 input shape 错误返回 JSON-RPC `-32602`；格式合法但未发布版本返回 `VERSION_NOT_AVAILABLE` 且不回退。任何 `release`、`release_id`、`selector` 输入均为未知字段并返回 `-32602`。该 pattern 不增加当前版本支持，Minecraft baseline 仍为 `26.2`；新增 Minecraft 版本需生成对应发布数据和契约收敛。

上述对象是四工具共享的公共输入部分；`search_blocks` 的完整 input 另按第 5.1 节增加可选 `query_spec`，其嵌套对象不因公共字段 Schema 而放宽，仍必须完整符合既有 `query-spec-output.v1`。

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

`minecraft_version` 可省略；省略时使用 `default_minecraft_version` 对应的 current。MCP 输入不接受 `release`、`selector` 或 `release_id`。D-051 允许一个可选的顶层 `query_spec`；它不是 selector、provider 配置或 release identity。

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
| `context.family` | 当前 R4 可为 null 或任意 string；二者均为不分组、不限额、不推断、不产生 family warning/metadata 且不改变候选顺序的确定性 no-op；非 string 且非 null 返回 JSON-RPC `-32602` |
| `context.compare_states` | boolean，默认 false |
| `context.rerank` | `auto`、`local_only`、`required`，默认 `auto` |
| `query_spec` | 可选；必须是完整、严格通过既有 `query-spec-output.v1` 的对象 |

### 5.1.1 Host-supplied `query_spec`

`query_spec` 的完整 shape **MUST** 直接使用 [`schemas/provider/query-spec-output.v1.json`](../schemas/provider/query-spec-output.v1.json)，包括 `schema_id`、`source`、`hard`、`soft`、`ambiguities`、`needs_user_choice`、`suggested_followups` 和 `unknown_terms` 及其全部 nested constraints。不得在 MCP 文档或工具中复制为第二个 Schema，也不得接受部分对象、`null`、未知 nested fields 或 host/model metadata。`source` 必须仍为 `llm`；它只表示语义由 LLM 生产，不代表 Blockpedia 已验证 provider/model 或本次执行了 server-side provider call。

输入 shape、类型、required、枚举、边界和任意 nested unknown field 错误必须 strict/fail closed，返回 JSON-RPC `-32602`。Schema 通过但 query condition 的语义或 invariant 无效时，继续返回现有业务 `QUERY_INVALID`；不得因 supplied spec 无效而隐式走 server-side QuerySpec generation。调用方省略 `query_spec` 才请求既有生成路径。

有效 host spec 只抑制 server-side QuerySpec generation，并且只在本次请求内存中使用，绝不持久化或写入 cache、logs、workspace、release 或 current。它不能选择或改写 release-bound `profile_id`、`adapter`、`model_id`、`base_url`、`secret_reference` 或其它 provider snapshot。`local_only` 仍禁止所有 provider call 和 visual rerank；`auto`/`required` 仍可按既有 snapshot 仅执行 visual rerank，能力、secret、warning、local downgrade 和 `required` fail-closed 语义不变。

搜索合并仍由本地 deterministic parser 和过滤器拥有最终权威：query 原文明确的 hard constraints 不能被 host spec 弱化；host hard 只有经本地解析确认后才保留为 hard，未确认的 host hard 不得成为 hard filter。歧义未解决时只应用安全的 soft intent，不应用未经确认的 semantic hard；host soft intent 仅能在既有 bounded、去重范围内合并，不创建新的 candidate identity 或 machine fact。完整 Schema 接受的 `soft.avoid_for` 当前不参与 positive recall、hard exclusion 或 ranking；沿用现有 `warnings` 输出机制提示该语义约束未应用，不增加新评分规则或字段。

**有限语义不变量（D-051）**：Schema/type/range/unknown-field 错误仍返回 JSON-RPC `-32602`。Schema normalization 后，若 `hard.minecraft_version.value` 非 null，其规范化后的精确值必须等于已解析请求版本；不等即 `QUERY_INVALID`。将 `hard.behaviors` 的 `transparent`/`emissive` 分别规范化为 `behavior.transparent`/`behavior.emissive`，并将 `hard.transparency`/`hard.emission` 规范化到同一字段；同一 canonical boolean fact 的 `eq`/`not_eq` 与 boolean `value` 转换为 `{true,false}` 的允许集合，交集为空即 `QUERY_INVALID`，例如 `behaviors.transparent=eq true` 与 `transparency=eq false`。同一规则适用于同一 `behaviors` field 和同一 `support.direction`；不臆测不同 soft terms 或未定义的形状关系。`needs_user_choice` 与 `ambiguities` 采用确定规则：非空必须为 `true`，为空必须为 `false`；`suggested_followups` 只满足 Schema 数组约束，不强制非空。未被本地解析确认的 host hard 不是 `QUERY_INVALID`，而是从 effective spec 删除并可产生现有 warning；soft disagreement 也不转为 `QUERY_INVALID`。

**Original/effective 分离**：不变量通过后，原始 validated canonical host object 只在内存中用于 `search_id` identity 和 warning 生成；对象本身不得发送给 visual rerank，不得持久化、写日志、写 cache 或 output。系统另构造 effective sanitized QuerySpec：保留本地确认的 host hard、合并本地 explicit hard（本地权威不可弱化），删除全部未确认 host hard，并在 recall、filter、scoring、contact-sheet 和 rerank 之前将 `soft.avoid_for` 设为 `[]`。deterministic recall/filtering 和 visual rerank 只能接收 effective spec；host spec 本身不产生 `reranked_by_llm=true` 或 `score_source=llm_rerank`。

### 5.2 处理和输出 `mcp-search-blocks-output.v1`

处理必须严格遵守 [`search-and-ranking.md`](search-and-ranking.md)：解析 release →（缺少 host spec 时才生成 QuerySpec）→ 合并并验证 QuerySpec → 构造 effective sanitized QuerySpec（删除未确认 hard、`soft.avoid_for=[]`）→ hard filter → FTS/字段评分 → Top-24 → 保持稳定顺序 → 8–12 联系表 → 仅使用 effective sanitized QuerySpec 的 strict visual rerank。当前 R4 不执行 family 分组或限额。`mcp-search-blocks-output.v1` 的 envelope 和 `data` **MUST** 严格使用闭合 Schema 的字段，不得加入任何未声明的输入、召回或解释性输出字段；host spec 只存在于输入内存路径。输出 `data` 的完整 shape 以真实 Schema 为唯一 owner：

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
    "query": "黄色的扁片方块，用于屋檐，不要红石组件",
    "hard_filters": [
      {
        "field": "behavior.redstone_related",
        "operator": "exclude",
        "value": true,
        "source": "user_explicit",
        "reason": "用户明确要求不要红石组件。"
      }
    ],
    "exclusion_summary": [],
    "candidates": [],
    "contact_sheet": {"image_id": null, "tile_mapping": []},
    "images": [],
    "reranked_by_llm": false
  }
}
```

`data` 只能包含 `mcp-search-blocks-output.v1` 声明的成员；每个 candidate（如有）必须且只能包含 Schema 要求的 `candidate_id`、`variant_id`、`block_id`、`display_name`、`recommended_state_id`、`candidate_qualification`、`local_score`、`final_score`、`score_source`、`score_breakdown`、`reason`、`warnings` 和 `machine_fact_refs`；联系表和图片分别只能使用 Schema 定义的 `contact_sheet`、`images` 形状。任何 host input 都不得通过 output envelope 回显。

`Top-24` 只是本地召回的内部阶段，不是 output member；不得因 `limit` 把它写入响应。提供 host spec 时，`search_id` 必须按既有 request/query identity 约定绑定其已校验 canonical representation/hash，使不同 host intent 不共享同一 identity；未提供时保持既有 identity 路径，不在 MCP 输出中暴露 hash 实现细节。`candidate_id` 只在本次响应内稳定，图片映射、结构对象和 TextContent 必须一一对应。LLM 失败、未配置或不要求时，`rerank=auto` 必须返回 warning 和 `reranked_by_llm=false`；消费 host-supplied QuerySpec 本身也永远不能设置 `reranked_by_llm=true` 或 `score_source=llm_rerank`，只有实际成功的 visual rerank 才能设置这些值；不得伪装成模型已重排。

硬过滤后为空是正常的成功业务结果：返回闭合 Schema 允许的空 `candidates`、空联系表、已应用 `hard_filters` 和 `exclusion_summary`；不得返回通过放宽 hard constraint 得到的候选。不得为正常空集生成 `mcp-error.v1`，`NO_CANDIDATES` 也不是该 Schema 的顶层 `error_code`。

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

compare 的结构差异只能由 release 机器/已审核语义产生。若提供 `context`，可以使用 resolved release 冻结的 provider snapshot 中同一 `adapter`/`model_id` 做候选解释或重排，但不得读取可变 active profile、跨协议 fallback，也不得新增 block ID、状态、图片或事实。必须返回稳定编号 PNG 联系表 ImageContent，`tile_mapping` 覆盖全部给定的有效 block/variant；图片内容和结构数据共用同一 mapping。

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
| `CURRENT_POINTER_INVALID` | current JSON/路径/hash 非法 | 否 |
| `VERSION_NOT_AVAILABLE` | 未知、未发布或未配置版本；列可用版本 | 否 |
| `RELEASE_NOT_FOUND` | current pointer 指向的 release 不存在 | 否 |
| `RELEASE_NOT_BUILT` | release 未通过 candidate-build gate | 否 |
| `RELEASE_INTEGRITY_FAILED` | manifest/checksum/quality hash、index format 或 immutable release 校验失败；v1 index 的 `details.integrity_component` 为 `index` | 否 |
| `INDEX_OPEN_FAILED` | 只读数据库无法打开 | 否 |
| `INDEX_INFO_UNAVAILABLE` | index metadata 缺失 | 否 |
| `QUERY_INVALID` | input shape 有效但搜索业务规则非法，包括 host-supplied QuerySpec 的语义/不变量错误 | 否 |
| `QUERY_PARSE_FAILED` | 无法得到安全 QuerySpec | 否 |
| `HARD_CONSTRAINT_UNSUPPORTED` | 请求包含当前 release 不支持的硬约束 | 否 |
| `BLOCK_NOT_FOUND` | ID 不在 release | 否 |
| `IMAGE_READ_FAILED` | release PNG 不可读 | 否 |
| `IMAGE_MAPPING_INVALID` | 图片和结构 mapping 不一致 | 否 |
| `RERANK_REQUIRED_UNAVAILABLE` | `rerank=required` 但重排不可用 | 否 |
| `READ_ONLY_VIOLATION` | 代码尝试写 release/workspace/current | 否 |
| `MCP_INTERNAL_ERROR` | 未分类执行错误 | 否 |

`mcp-error.v1.error_code` 只能使用上表中现有 Schema enum 值。`VERSION_REQUIRED` 不适用于 MCP，因为省略版本使用 default；`NO_CANDIDATES` 不是错误，正常空搜索直接成功；非法 `block_id`、compare 数量和其它 input shape 在工具执行前使用 JSON-RPC `-32602`，不使用 `BLOCK_ID_INVALID` 或 `COMPARE_COUNT_INVALID` 工具错误。provider-specific code 也不得作为顶层 `error_code`：`rerank=auto` 的 provider failure 返回 `isError=false`、warning、本地结果和 `reranked_by_llm=false`；`rerank=required` 只能返回顶层 `RERANK_REQUIRED_UNAVAILABLE`，底层 provider code 只放在同一 `mcp-error.v1` 对象的 `details.provider_error_code`。错误消息不能包含 API key、Authorization、完整 response、绝对路径或 SQL。

## 9. 只读和 stdout 验收

必须以子进程执行 `block-index mcp` 和原创 fixture 检查：

1. `tools/list` 严格只有四个工具；不存在 HTTP、resources、任意 SQL/文件读写接口。
2. 每行 stdout 可独立解析为 JSON-RPC/MCP 消息；stderr 可有诊断但不进入 stdout。
3. 省略版本使用 `default_minecraft_version`；malformed `minecraft_version` 返回 JSON-RPC `-32602`，格式合法但未发布版本返回 `VERSION_NOT_AVAILABLE` 且不回退；显式版本使用该版本 current；未知/未发布版本和 hash mismatch 均符合错误表；非法 `block_id`、compare 数量和其它 input shape 返回 JSON-RPC `-32602`，`search_blocks.query_spec` 及其 nested unknown fields 也按完整 `query-spec-output.v1` strict 校验并返回 `-32602`；Schema 有效但 QuerySpec 语义/不变量无效返回 `QUERY_INVALID`，`context.family=null` 或任意 string 均以 `isError=false` 的 `mcp-search-blocks-output.v1` 成功 no-op，非 string 且非 null 的 family 返回 JSON-RPC `-32602`，格式合法但未知 ID 才返回 `BLOCK_NOT_FOUND`；host spec 无效不得隐式调用 server-side QuerySpec，历史 `release_id` selector 被拒绝。
4. 四工具只读不可变 release；运行所有成功、失败、降级和图片路径分支后，SQLite、文件、cache、logs 和 current hash 不变。
5. `structuredContent` 与 TextContent JSON 深相等；成功降级 `isError=false`，工具执行错误 `isError=true`，协议错误使用标准 JSON-RPC error；未知 RPC method 返回 `-32601`，合法 `tools/call` 的未知 tool name 返回 Invalid Params `-32602`。
6. `search_blocks`/`compare_blocks` 返回稳定编号 PNG 联系表 ImageContent；`get_block_details` 返回四视角 PNG；`index_info` 无图片。
7. 图片 metadata 含 ID、MIME、尺寸、hash、purpose、content index 和映射，不含绝对路径；mapping 与 PNG 联系表、结构候选 100% 一致。
8. provider 不可用不改变候选事实；`rerank=auto` 返回 `isError=false`、warning 和 `reranked_by_llm=false`，`rerank=required` 返回顶层 `RERANK_REQUIRED_UNAVAILABLE`，且 provider code 仅在 `details.provider_error_code`。host QuerySpec 仅抑制 QuerySpec generation，不得单独宣称 provider/model 已调用或验证；`local_only` 不得发生任何 provider call。
9. 仅使用 `schema_meta.format_version=2` 的原创 PNG/SQLite release fixture 通过协议测试；v1 fixture/release 返回 `RELEASE_INTEGRITY_FAILED` 且 `details.integrity_component="index"`；缺少真实本地 release 只能报告 `SKIPPED_LOCAL_RELEASE_MISSING`，不得伪造通过。
