# 检索与排序契约

## 文档状态、优先级与关联规范

本文定义 Blockpedia MVP 的 `QuerySpec`、硬过滤、FTS5 检索、确定性排序、联系表、同一个 release-bound OpenAI model/adapter 的视觉重排和降级语义。精确数据字段形状由 `schemas/{workspace,provider,mcp}/` 下的真实 Schema 文件拥有；本文示例和行为规则不重复穷举字段。正文使用简体中文；字段名、Schema、状态、错误码、权重键和命令保持英文。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 为规范性关键字。

本文服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md) 和 [`decisions.md`](decisions.md)，并与 [`product-scope.md`](product-scope.md) 和 [`architecture.md`](architecture.md) 保持一致。原始稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。字段事实来自 [`data-and-schemas.md`](data-and-schemas.md)、导出规则来自 [`export-contract.md`](export-contract.md)、工作/发布边界来自 [`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)。

关联实现契约：

- [`openai-provider.md`](openai-provider.md)：protocol-neutral `OpenAIProvider`、显式 adapter、strict Schema、重试和在线降级；
- [`mcp-api.md`](mcp-api.md)：四个工具的 release 选择和输出映射；
- [`webui-and-operations.md`](webui-and-operations.md)：搜索测试台、发布检查和写操作；
- [`quality-and-testing.md`](quality-and-testing.md)：搜索 contract tests、MVP 门和后置黄金集；
- [`security-and-distribution.md`](security-and-distribution.md)：最小披露、提示注入和本地数据边界。

## 1. 检索输入和版本边界

每次检索 **MUST** 解析到一个由 WebUI build/activation gate 产生并由 current pointer 指向的精确 release；MCP 运行时不重新证明该 release 的完整性。WebUI/API 搜索测试必须显式提供 `minecraft_version`；MCP 的 `minecraft_version` 可以省略，省略时使用 `current-pointer.v1.default_minecraft_version` 对应的 current release，显式输入必须匹配严格版本格式 `^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$`，格式非法返回 JSON-RPC `-32602`。格式合法但未知或未发布版本返回 `VERSION_NOT_AVAILABLE` 并列出可用精确版本，**MUST NOT** 回退到其他版本。MCP 不支持历史 `release_id` selector；历史选择只由 WebUI rollback 完成。release-index 的 fresh/v2 资格只由 build/activation gate 负责；MCP 不在运行时验证 index format、manifest/checksum/schema/quality/PNG 完整性。

搜索只读发布投影，不读取 `workspace`、草稿、未审核 annotation 或正在生成的图片。`excluded`、无发布变体和未审核高优先级记录不得进入默认候选；详情可在 release 允许时展示审计事实。所有返回的 `block_id`、`variant_id`、状态、图片映射和 release 元数据必须来自该 release。

### 1.1 输入字段

`SearchRequest` 使用 JSON Schema Draft 2020-12，严格对象 `additionalProperties=false`；它是 API/MCP 输入契约，不是 D-030 冻结的持久业务 Schema ID：

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

约束：`minecraft_version` 省略或匹配上述 pattern；`query` 为 1–2000 个 Unicode 字符；`limit` 为 1–12，默认 8；WebUI/API 的 release 固定为 resolved `current`，不得从查询输入选择历史 release；当前 R4 没有 schema-owned `family_id` 或 family catalog，`context.family` 可为 null 或任意通过 input schema 的 string，二者都是确定性 no-op：不分组、不限额、不推断 family、不产生 family warning/metadata且不改变候选顺序。非 string 且非 null 的 `context.family` 仍按 input shape 返回 JSON-RPC `-32602`，不得推断或创建 family ID；`context.compare_states` 默认 false；`rerank` 只能为 `auto`、`local_only` 或 `required`。MCP `search_blocks` 还可接收可选顶层 `query_spec`，其完整 shape 直接使用既有 `query-spec-output.v1`，不是新的 API/持久 Schema。未知字段、绝对路径、SQL、`block_id` 候选列表和未声明的硬规则必须拒绝并按输入契约返回 JSON-RPC `-32602`。

## 2. `QuerySpec` strict Schema

### 2.1 语义层和来源

自然语言解析由 Studio 新写任务的 active profile，或 release-bound MCP 冻结 snapshot 中的同一个 `adapter`/`model_id` 完成，调用见 [`openai-provider.md`](openai-provider.md)。MCP 也可按 D-051 接收 host-supplied QuerySpec；这只绕过 server-side QuerySpec generation，不改变 provider snapshot。模型只能生成检索意图，不得产生候选 ID、SQL、路径或机器事实。两种 adapter 共用 `query-spec-output.v1`，其精确字段、required 集合、枚举和 nested object 约束唯一由 [`schemas/provider/query-spec-output.v1.json`](../schemas/provider/query-spec-output.v1.json) 定义；本节不复制整份字段定义。Responses 使用 `text.format`，Chat 使用 `response_format.json_schema`；真实发送必须通过所选 adapter 的 strict `json_schema`。

provider wire 的 `source` 只能是 Schema 固定的 `llm`。对 host-supplied QuerySpec，`source=llm` 只说明语义生产来源，不是已验证的 provider/model 身份，也不表示 Blockpedia 本次调用了 server-side provider；不得增加 host/model metadata。provider 不可用时的 `local_parser` 仅是本地降级路径，不能伪装成 `query-spec-output.v1` provider artifact；结果必须在 warnings 中说明降级。缓存和审计使用 active profile、provider envelope 或 release manifest snapshot 的可信 adapter/model/prompt/schema 版本，不从模型输出或 host input 猜测这些值。wire Schema 的所有字段必须 required，所有 object `additionalProperties=false`，只使用所选 adapter 能力探测证明支持的 Structured Outputs 子集；不得跨协议 fallback。

### 2.2 hard/soft 规则

下列条件可以成为硬过滤，只有用户明确说“必须/一定/不要/排除/只能”等，或系统安全规则强制加入时才进入 `hard`：

- 精确 `minecraft_version` 和 resolved current release status；
- 合法状态/合法状态映射；
- 明确排除的行为，例如在 `hard.behaviors` 中使用 `field=redstone_related`、`operator=not_eq`、`value=true`；
- 用户显式要求必须/不得具备的支撑方向；
- 用户显式要求透明/不透明、发光/不发光；
- 用户显式方向，例如 `horizontal`、`vertical`、`north`；
- 用户显式形状，例如 `horizontal_thin_sheet`、`stair_like`。

颜色、材质观感、建筑用途、风格、模糊形状描述和普通关键词默认只能进入 `soft`。模型不得把“看起来像”“适合”“大概”“类似”提升为硬约束；只有请求文本中的明确强制词或系统安全规则能提升。`hard` 的每个约束项必须遵守真实 Schema 的字段结构并带 `required=true`；行为约束使用 `value` 单值，不使用 `values` 数组。硬约束值必须来自真实 Schema 允许的 bounded semantic fields 或机器事实枚举。

`unknown` 永远不能满足硬约束：要求 `true` 只接受 `true`，要求 `false` 只接受 `false`，排除行为时 `unknown` 不能当作安全的 `false`。视觉条件未由机器事实验证时，不能硬过滤为满足，必须保留候选并通过现有 `warnings` 说明该条件未经验证；不得增加输出 Schema 之外的验证标志。

### 2.3 明确排除与“必须”

应用必须在本地重新解析用户原文中的否定和强制表达，不能只信任模型的 `hard` 列表。若用户明确要求“不要红石”，候选的 `redstone_related=true` 和 `unknown` 都必须排除；若用户要求“必须可支撑于下方”，只接受 `support` 中对应的 `direction=below` 且 `value=true`，`false` 和 `unknown` 都排除。若用户要求“不发光”，只接受 `emissive=false`，`unknown` 不能作为安全候选。排除和必须的实际字段路径及理由必须写入现有 `hard_filters[].reason`。

模型没有权限把 `excluded` 变为可选、把 skipped 变成候选、把非法状态变成合法状态或改变 current 版本解析。

### 2.4 MCP host-supplied QuerySpec（D-051）

MCP `search_blocks` 的可选顶层 `query_spec` 是不可信、临时的输入。它必须完整通过既有 [`query-spec-output.v1`](../schemas/provider/query-spec-output.v1.json)；`schema_id`、`source`、全部 required 字段、枚举、数组/字符串边界以及所有 nested `additionalProperties=false` 约束均适用。未知 nested fields、部分对象、`null` 或 host/model metadata 都是输入 shape 错误，返回 JSON-RPC `-32602`。Schema 通过但语义或 invariant 不成立时保持 `QUERY_INVALID`；无效 supplied spec 不得隐式 fallback 到 server-side QuerySpec generation，省略字段才使用既有生成路径。

有效 host spec 只抑制 server-side QuerySpec generation，不改变版本/current/release 解析，也不选择或改写 release-bound provider snapshot。`context.rerank=local_only` 仍禁止所有 provider call 和 visual rerank；`auto`/`required` 仍可仅使用该 snapshot 执行 visual rerank，并分别保留现有 local warning downgrade 或 `RERANK_REQUIRED_UNAVAILABLE` fail-closed 语义。Host `source=llm` 只标识语义生产，不证明 Blockpedia 验证了 provider/model 或本次调用了模型。

合并顺序和权威性固定如下：

1. 本地 deterministic parser 重新解析原始 `query` 的显式 hard constraints；这些约束拥有最终权威，host spec 不能削弱它们。
2. host hard 只有经本地解析确认后才保留为 hard；未确认的 host hard 不得成为 hard filter。未解决歧义时只应用安全的 soft intent，不能执行未经确认的 semantic hard。
3. host soft intent 可以在既有 bounded、去重规则内与本地 soft intent 合并；不得由 host spec 创建新的 candidate identity、`block_id`、状态、图片映射或 machine fact。
4. `soft.avoid_for` 因属于完整 Schema 而接受并校验，但当前 deterministic search 没有权威 negative dimension；它不参与 positive recall、hard exclusion 或 ranking，只沿用现有 `warnings` 输出机制提示未应用，不新增评分规则或 Schema 字段。

提供 host spec 时，`search_id` 必须按既有 request/query identity 约定绑定其已校验 canonical representation/hash，使不同 host intent 不共享同一 identity；未提供时保持既有 identity 路径，不公开 hash 实现细节。消费 host spec 本身永远不能设置 `reranked_by_llm=true` 或 `score_source=llm_rerank`；这两个值只有实际 visual rerank 成功后才能产生。

**有限语义不变量（D-051）**：Schema/type/range/unknown-field 错误仍返回 JSON-RPC `-32602`。Schema normalization 后，若 `hard.minecraft_version.value` 非 null，其规范化后的精确值必须等于已解析请求版本；不等即 `QUERY_INVALID`。将 `hard.behaviors` 的 `transparent`/`emissive` 分别规范化为 `behavior.transparent`/`behavior.emissive`，并将 `hard.transparency`/`hard.emission` 规范化到同一字段；同一 canonical boolean fact 的 `eq`/`not_eq` 与 boolean `value` 转换为 `{true,false}` 的允许集合，交集为空即 `QUERY_INVALID`，例如 `behaviors.transparent=eq true` 与 `transparency=eq false`。同一规则适用于同一 `behaviors` field 和同一 `support.direction`；不臆测不同 soft terms 或未定义的形状关系。`needs_user_choice` 与 `ambiguities` 采用确定规则：非空必须为 `true`，为空必须为 `false`；`suggested_followups` 只满足 Schema 数组约束，不强制非空。未被本地解析确认的 host hard 不是 `QUERY_INVALID`，而是从 effective spec 删除并可产生现有 warning；soft disagreement 也不转为 `QUERY_INVALID`。

**Original/effective 分离**：Schema 和不变量通过后，原始 validated canonical host object 只在内存中用于 `search_id` identity 和 warning 生成；对象本身不得发送给 visual rerank，不得持久化、写日志、写 cache 或 output。effective sanitized QuerySpec 保留本地确认的 host hard、合并本地 explicit hard（本地权威不可弱化），删除全部未确认 host hard，并在任何 recall、filter、scoring、contact-sheet 或 rerank 输入前将 `soft.avoid_for` 设为 `[]`。deterministic recall/filtering 和 visual rerank 只能接收 effective spec；host spec 本身不产生 `reranked_by_llm=true` 或 `score_source=llm_rerank`。

## 3. 本地召回和过滤顺序

### 3.1 固定顺序

给定 release 和已合并 QuerySpec，本地算法必须按如下顺序执行，顺序不得由 LLM 或 host input 改变；存在有效 host-supplied QuerySpec 时跳过 server-side QuerySpec generation，但不跳过本地验证、hard ownership 或 merge：

```text
resolve current release
  → validate/obtain QuerySpec (host input or server-side generation)
  → merge local explicit hard constraints
  → construct effective sanitized QuerySpec (drop unconfirmed host hard; set soft.avoid_for=[])
  → version/release status filter
  → legal state filter
  → candidate qualification filter
  → explicit exclude behavior filter
  → explicit required support/transparency/emission/orientation/shape filter
  → FTS5/field recall
  → deterministic score
  → Top-24
  → preserve stable order
  → 8–12 contact-sheet candidates (stable order)
  → optional visual rerank
```

MCP 的 `search_blocks` 外层 hard deadline 为 **55 秒**；进入最后 **5 秒** 后不得启动新的 provider 请求。QuerySpec stage 总预算为 **15 秒**（首次 10 秒、最多一次重试 5 秒），visual rerank stage 总预算为 **30 秒**（首次 20 秒、最多一次重试 10 秒）。实际单次 timeout 取 profile 配置、stage 剩余预算和 request 剩余时间的最小值，少于 1 秒不得发送。每个 logical provider request 最多两次尝试且无 fallback；`auto` 失败必须确定性本地降级，`required` 失败使用顶层 `RERANK_REQUIRED_UNAVAILABLE`。同一请求复用 provider/client 与 preview bytes；同步 provider/SQLite 查询工作移出 event loop。

发布候选默认只允许 `eligible` 和带 warnings 的 `conditional`；`excluded` 不进入默认召回。`conditional` 必须随候选返回 warnings，不能被静默隐藏。硬过滤后为空时必须返回空结果，不得自动删除硬条件、把 `unknown` 当满足或换版本。

### 3.2 FTS5 和字段通道

SQLite 优先使用 FTS5 `trigram` tokenizer；不可用时必须在 release manifest 标记 `fts_mode=normalized_like`，使用确定性的规范化字符串 `LIKE` 与显式标签查询，不能引入向量列、Embedding 或外部服务。FTS 索引至少覆盖：

```text
name_zh
name_en
synonyms_zh
synonyms_en
summary
color_terms
shape_terms
material_impressions
building_roles
style_tags
avoid_for
machine_tags
behavior_terms
```

每个通道必须可单独诊断并在 `score_breakdown` 中返回。未经审核的开放文本不能成为硬匹配；AI 和人工语义必须先通过真实 Schema 的 bounded fields。行为、透明、发光、支撑、方向和几何事实以结构化列/JSON 过滤，不能依赖 FTS 文本猜测。

D-051 的 host-supplied `soft.avoid_for` 不把其 terms 投入 positive FTS recall，也不把现有 `avoid_for` 存储/索引通道解释为权威 negative dimension；该输入不会参与 hard exclusion 或 ranking，并只沿用现有 `warnings` 机制提示未应用。

### 3.3 颜色、几何、用途、风格和行为

本地评分必须分别读取：

- `shape`：确定性 geometry class、方向和占用体积；
- `color`：对象蒙版的 Oklab/Lab 摘要和受控 color terms；
- `use`：受控 `building_roles`；
- `name-synonym`：官方名称及已验证中英文同义词；
- `style`：受控 `style_tags`；
- `behavior`：机器 behavior facts 和受控行为标签。

AI 只可为已有候选解释语义，不能创造新的机器颜色、几何、用途事实或行为值。颜色/材质/用途/风格/模糊形状默认软排序；明确的方向、形状和行为要求按第 2 节硬过滤。

## 4. 确定性评分和版本化

初始排序配置标识为 `search-ranking.v1`，默认权重必须精确为：

```text
shape          0.35
color          0.30
use            0.15
name_synonym   0.10
style          0.05
behavior       0.05
```

只对 QuerySpec 中出现的软维度计算分数；未出现维度的权重必须按出现维度比例归一化，空维度不得贡献伪分数。硬过滤通过后，每个候选的总分和分项分数必须可解释、可重放；同分时按规范化 `block_id`、`variant_id` 稳定排序。不存在黄金集时，以上权重只是 MVP 可解释初始规则，**MUST NOT** 声称已经调优或达到相关性指标。

每个候选的排序结果至少包含现有输出 Schema 定义的 `local_score`、`score_breakdown` 和 `warnings`；请求级硬过滤解释写入 `hard_filters[].reason`。`search_ranking_version` 只属于内部算法版本和 release/config snapshot metadata，不是搜索输出字段。权重任何改变必须产生新 `search-ranking.vN`、新 release/配置快照和新的测试证据；不得在运行时悄悄调参。

## 5. Top-24、稳定顺序和联系表

### 5.1 Top-24

本地评分后必须先截取最多 24 个候选（少于 24 时返回实际数量），并以稳定排序键决定截断。Top-24 之前不得使用视觉 LLM；不得让 LLM 扩大召回集合。

### 5.2 family 参数当前 R4 为 no-op

当前 R4 release projection 没有 schema-owned `family_id` 或 family catalog，因此不执行 family 分组。`context.family=null` 或任意通过 input schema 的 string（包括未知 string）都是确定性 no-op：不应用 family 上限、不生成 family warning，也不改变候选顺序；非 string 且非 null 的值在协议层返回 JSON-RPC `-32602`。不得从文本、语义标签或模型输出推断/创建 family ID。

`context.compare_states=true` 和 `compare_blocks` 的显式 2–6 个 `block_id` 只影响给定状态/方块的比较范围，不引入 family 分组、限制或 metadata。当前 R4 不得新增 family dedupe 字段、响应 metadata 或持久化数据。

### 5.3 8–12 联系表

从 Top-24 的稳定顺序生成最多 8–12 个候选的联系表；候选少于 8 时使用实际数量，绝不填充虚假 ID。每个 tile 具有唯一 `candidate_id`，格式建议为 `A1`、`A2`、`B1`，但最终格式必须由 release/schema 固定；tile 映射必须是本地生成的：

```json
{
  "contact_sheet_id": "cs_sha256_prefix",
  "image_purpose": "search_candidates",
  "tiles": [
    {
      "candidate_id": "A1",
      "variant_id": "minecraft:yellow_carpet",
      "block_id": "minecraft:yellow_carpet",
      "image_ref": "previews/minecraft/yellow_carpet/preview.png",
      "image_sha256": "sha256:<64 lowercase hex>",
      "position": {"row": 0, "column": 0}
    }
  ]
}
```

联系表 PNG 必须来自当前 release 或为在线临时生成的本地候选图；传给模型的图片只能包含短编号，不得把完整 ID 依赖小字绘入图片。结构化 `candidate_id` 到 `variant_id`/`block_id` 映射是唯一事实来源。

## 6. 同一个模型视觉重排

若 `rerank=auto` 且 Studio 活动 profile 或 release-bound snapshot 的选定 adapter 能力通过，使用 [`openai-provider.md`](openai-provider.md) 中同一 `adapter`/`model_id` 的 strict `rerank-output.v1` 请求。有效 host-supplied QuerySpec 只省略前面的 QuerySpec generation，不能单独触发或证明 visual rerank；`rerank=local_only` 禁止所有 provider call。若 `rerank=required` 而 provider 不可用，返回顶层 `RERANK_REQUIRED_UNAVAILABLE`，底层 provider code 只放入 `details.provider_error_code`，不伪造重排；若 `auto` 失败，必须返回 `isError=false` 的本地排序结果、warning 并设置 `reranked_by_llm=false`，不得自动切换协议。

模型输入只能是原始 query、effective sanitized QuerySpec、8–12 个候选联系表和每个候选的最小已验证机器/语义 metadata。模型输出只能重排现有 `candidate_id` 并提供理由；它不得新增、删除或改写候选、`block_id`、状态、图片、硬过滤结果、candidate qualification、release metadata 或机器事实。消费端必须检查输出集合与本地集合完全一致；失败进入 `PROVIDER_OUTPUT_ID_MISMATCH`/warning，并走本地降级。

### 6.1 输出字段

MCP 等价在线搜索结构化结果必须严格使用 `mcp-search-blocks-output.v1` 的闭合 envelope 和 `data` 字段，不得加入任何未声明的输入、召回或解释性输出字段；host spec 只存在于输入内存路径：

```json
{
  "schema_version": "mcp-search-blocks-output.v1",
  "request_id": "mcp_01J",
  "minecraft_version": "26.2",
  "resolved_release_id": "rel_01J",
  "manifest_sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000000",
  "warnings": [],
  "data": {
    "search_id": "search_01J",
    "query": "黄色的扁片方块",
    "hard_filters": [],
    "exclusion_summary": [],
    "candidates": [],
    "contact_sheet": {"image_id": null, "tile_mapping": []},
    "images": [],
    "reranked_by_llm": false
  }
}
```

`data` 只能包含 `mcp-search-blocks-output.v1` 声明的成员。每个 candidate（如有）必须且只能包含 `candidate_id`、`variant_id`、`block_id`、`display_name`、`recommended_state_id`、`candidate_qualification`、`local_score`、`final_score`、`score_source`、`score_breakdown`、`reason`、`warnings` 和 `machine_fact_refs`；所有 ID、状态和图片 metadata 必须在本地 release 查找后生成，contact sheet/image mapping 只能使用闭合 Schema 形状。LLM 理由不能覆盖它们。只有实际成功的 visual rerank 才能使 `reranked_by_llm=true` 或 `score_source=llm_rerank`；消费 host input 永远不能设置这两个值，也不得声称 Blockpedia 调用或验证了某个 model。

## 7. 空结果、歧义和降级

### 7.1 空结果

硬过滤后零候选是业务上有效的成功结果，必须返回闭合 Schema 允许的空 `candidates`/联系表、实际应用的 `hard_filters`、`exclusion_summary` 和 `warnings`；不得把正常空集作为工具错误，也不得把任何原始输入、内部召回或解释对象写入 output。

系统 **MUST NOT** 静默放宽版本、发布状态、合法状态、明确排除、支撑、透明、发光、方向或形状条件。用户没有明确同意前，系统不得自动重试成 soft。

### 7.2 歧义

存在多个互斥但都通过硬过滤的解释时，输入 invariant 会要求 `needs_user_choice=true`；`ambiguities`/`suggested_followups` 只属于输入内存中的 validated/effective QuerySpec，不是 MCP output 字段。歧义只影响本地决策和现有 `warnings` 生成，不得修改机器事实、候选身份或闭合 output shape。

### 7.3 Provider 不可用

server-side `query_spec` generation 失败时，本地 parser 只能使用真实 Schema 允许的 bounded semantic fields 和用户显式词，未知内容进入 `unknown_terms`/soft keyword；无法安全解析硬约束时返回 `QUERY_PARSE_FAILED` 或明确未解析硬约束的安全空成功结果，不得猜测。有效 host-supplied QuerySpec 不经过该 server-side generation，但仍必须经过同一完整 Schema、local hard ownership、semantic/invariant validation 和 merge；它无效时不得回退旧路径。`visual_rerank` 失败时使用本地排序，保留 warnings、`reranked_by_llm=false` 和 provider 错误码。若 Studio active profile 或 release-bound snapshot 的 `secret_reference` 无法从 Keyring/env 解析，在线查询只可本地降级并 warning，不得写状态、修改 profile 或缓存。若运行时能力不满足 release snapshot 的要求，也必须本地降级并 warning，不得跨协议 fallback。降级不得增加候选、放宽过滤、改变 release 或写入数据。

## 8. 版本化缓存和可重复性

在线 QuerySpec/重排缓存必须遵循 [`openai-provider.md`](openai-provider.md) 的 key：`image_hash`、`machine_metadata_hash`、`adapter`、`prompt_version`、`model_id`、`schema_version`、`base_url_stable_id`、`stage`，并额外绑定 `minecraft_version`、resolved release manifest hash、原始 query hash、QuerySpec hash、候选集合 hash 和 `search-ranking_version`。host-supplied QuerySpec 不得写入缓存、workspace、release 或日志；其 canonical representation/hash 只在本次 request/query identity 和内存计算中使用，不能替代 provider snapshot 或 provider evidence。缓存只能保存通过 Schema 的最小 artifact，不保存完整 response、图片或 usage；MCP 进程不写此缓存。

同一 release、同一 effective sanitized QuerySpec、同一排序配置和同一 fixture 必须产生相同 Top-24、family no-op 结果、tile mapping、候选 ID 和本地顺序；Top-24 之后必须保持该稳定顺序。时间、request ID 和展示 `search_id` 不参与排序。

## 9. 错误码

| `error_code` | 条件 | 是否允许本地降级 |
|---|---|---:|
| `VERSION_REQUIRED` | WebUI/API 缺少 `minecraft_version`；MCP 省略不报错 | 否 |
| `VERSION_NOT_AVAILABLE` | 未知或未发布版本 | 否 |
| `RELEASE_NOT_FOUND` | current pointer 指向不存在 release | 否 |
| `RELEASE_INTEGRITY_FAILED` | 仅由 build/activation gate 或其报告使用；MCP 运行时不验证 manifest/hash/质量门/index format/immutable | 否 |
| `QUERY_INVALID` | input shape 已有效但 query/context/host QuerySpec 的语义或 invariant 非法 | 否 |
| `QUERY_PARSE_FAILED` | 无法得到安全 QuerySpec | 仅返回安全空结果 |
| `HARD_CONSTRAINT_UNSUPPORTED` | 请求硬条件不在机器或 bounded semantic fields 能力内 | 否，须追问 |
| `NO_CANDIDATES` | 保留为内部/兼容诊断码；正常硬过滤空集不得作为 MCP error | 否，保持空成功 |
| `PROVIDER_NOT_CONFIGURED` | Studio 无可用 active profile，或 release-bound snapshot 的 secret 无法解析 | 是，local parser/local rank |
| `PROVIDER_CAPABILITY_MISSING` | provider 图片/strict 能力缺失 | 是，local parser/local rank |
| `PROVIDER_REFUSAL` | Responses refusal | 是，local result |
| `PROVIDER_INCOMPLETE` | Responses incomplete | 是，local result |
| `PROVIDER_SCHEMA_INVALID` | QuerySpec/rerank Schema 错误 | 是，local result |
| `PROVIDER_OUTPUT_ID_MISMATCH` | 模型返回新增/缺失/错误 candidate ID | 是，local result |
| `IMAGE_READ_FAILED` | release 图片不可读 | 否，发布门应已阻断 |
| `BLOCK_NOT_FOUND` | 详情或比较 ID 不存在 | 否 |

本表中的 provider codes 是 search lane 的内部分类，不是 `mcp-error.v1.error_code` 的新增值；MCP 顶层错误和 `details.provider_error_code` 映射以 [`mcp-api.md`](mcp-api.md) 为准。错误消息必须说明修复动作，不回显路径、key、完整 provider response 或 SQL。

## 10. 可执行验收

实现必须用原创 SQLite/PNG fixture 验证：

1. `QuerySpec` 的 wire `query-spec-output.v1` 和本地 Schema 均拒绝未知字段、候选 ID、越界语义值、缺少来源和硬/软混淆；MCP host-supplied QuerySpec 的顶层和 nested unknown fields 均映射为 JSON-RPC `-32602`，Schema 有效但语义/invariant 无效映射为 `QUERY_INVALID`，无效 supplied spec 不 fallback；`unknown` 不满足任何硬约束。
2. 版本、发布状态、合法状态、明确排除行为、明确支撑/透明/发光/方向/形状先过滤；空结果为闭合 output Schema 允许的成功空集，不放宽约束。
3. FTS5 trigram 和 `normalized_like` fallback 都覆盖名称、同义词、颜色、几何、用途、风格和行为通道。
4. `search-ranking.v1` 权重精确为 `.35/.30/.15/.10/.05/.05`；未出现维度按规则归一化；结果可复现。
5. Top-24 后 family dedupe 必须是确定性 no-op：`context.family=null` 或任意通过 input schema 的 string 都不分组、不限额、保持稳定顺序并生成 8–12 联系表；非 string 且非 null 的 `context.family` 返回 JSON-RPC `-32602`。`compare_states`/`compare_blocks` 不引入 family 分组、限制、metadata 或虚假候选。
6. provider 重排只允许已有 candidate ID；只有实际成功的 visual rerank 才能设置 `reranked_by_llm=true`/`score_source=llm_rerank`，host QuerySpec consumption 不得设置，失败降级明确 `false`，不改变硬过滤。
7. 图像编号、结构化映射和 `block_id`/state 全部来自 pointer-resolved release，图片不可读或映射不一致时失败；不以此声称 MCP 做过 PNG 全量预验证。
8. 歧义按 input invariant 校验 `needs_user_choice`/`ambiguities` coherence；MCP output 仍只使用闭合 Schema 字段并可使用现有 `warnings`，不增加歧义、追问或视觉验证字段。

9. D-052 focused tests 只覆盖 55 秒外层 deadline、最后 5 秒不启动新 provider 请求、15 秒 QuerySpec/30 秒 visual rerank stage budgets、profile/stage/request timeout 取最小值、少于 1 秒不发送、最多两次尝试且无 fallback、同请求 provider/client/preview bytes 复用和同步查询移出 event loop；不新增真实基数 fixture、全量矩阵或 evidence report。

黄金查询集不少于 100 条、`Top-5>=90%`、硬约束违反率 `<2%`、映射一致率 `100%` 以及权重调优均属于 MVP 后置质量工作，定义和真实报告要求见 [`quality-and-testing.md`](quality-and-testing.md)，不得伪装成 MVP roadmap 必做退出条件。
