# 质量、完整性与测试契约

## 文档状态与导航

本文定义 Blockpedia MVP 的质量范围、确定性完整性、strict Schema、provider/MCP/WebUI 测试、Windows 11/Linux x86_64 运行验收、降级和原子发布门。Linux 实际安装/运行/平台行为统一归 R5；正文使用简体中文；测试文件、字段、Schema、状态、错误码和命令保持英文。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 为规范性关键字。各项测试按 R1–R5 阶段归属执行，不把后续阶段命令倒灌为 R1-R4 blocker。

本文服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md)、[`decisions.md`](decisions.md)、[`product-scope.md`](product-scope.md) 和 [`architecture.md`](architecture.md)。原始设计稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。具体契约链接如下：

- [`export-contract.md`](export-contract.md)、[`state-policy-and-rendering.md`](state-policy-and-rendering.md)、[`data-and-schemas.md`](data-and-schemas.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)；
- [`openai-provider.md`](openai-provider.md)、[`search-and-ranking.md`](search-and-ranking.md)、[`mcp-api.md`](mcp-api.md)、[`webui-and-operations.md`](webui-and-operations.md)；
- [`security-and-distribution.md`](security-and-distribution.md)。

## D-053 focused MCP acceptance (current)

R4 MCP tests cover only the current local path: strict `keywords` array validation (1–16 items, trim 1–64 Unicode characters, no empty/duplicate items), rejection of legacy `query`/`context`/`query_spec` with JSON-RPC `-32602`, pointer-resolved eligible/conditional local FTS5 trigram or normalized-LIKE union recall, deterministic local ordering, Top-24/contact-sheet limits, `hard_filters=[]`, `reranked_by_llm=false`, `score_source=local`, and successful empty results. Tests also cover zero provider calls and no Keyring/provider-snapshot reads, pointer snapshot cache refresh, `to_thread`/event-loop isolation, strict outputSchema `oneOf` parity, and zero writes. D-052 55/15/30 provider deadline, provider retry/reuse, QuerySpec and visual-rerank cases are historical focused evidence only and MUST NOT be used as current MCP acceptance criteria.

当前仓库已完成 R0 契约、Schema/fixture 轻量验收、依赖锁和工具链骨架；R1、R2 已有 Windows 证据关闭。Linux CPython/Web、Linux MCP、Linux Java 25/runtime/exporter、Linux wheel/ABI 和最终双平台源码/运行时复现统一保留至 R5；产品 release 尚未开始。本文件后续验收只在对应实现阶段执行，不作为 R0-R4 的提前 blocker。

## 1. MVP 质量边界

MVP 必须退出的质量范围只有：

1. 确定性导出/导入、100% `minecraft` registry 登记、合法状态、图片和不可变 release 完整性；R1 只负责 exporter 产物完整性，release 完整性属于后续 candidate/activation gate；
2. 机器事实/AI 语义/人工 override 分层、strict JSON Schema 和引用边界；
3. 两种显式 OpenAI adapter 的三阶段请求、严格一次总重试、错误分类、离线审核和在线本地降级；
4. MCP stdio 四工具、结构化输出/等价 TextContent、PNG 联系表/四视角图片和 stdout 纯净；
5. WebUI loopback、任务 pause/resume/recover、普通/高优审核、声明式覆盖、publish/rollback/cleanup；
6. Windows 11 x86_64 与 Linux x86_64（`manylinux_2_17` / glibc `>=2.17`）的源码锁依赖可复现运行；其中 Linux 安装、运行和平台行为统一由 R5 验证。
7. 原子 `current.json` 切换、回滚只切指针、release 不可变和最小安全/分发检查。

MVP **MUST NOT** 把黄金查询集、Top-5 指标、硬约束统计目标、排序权重调优、安装包、容器、系统服务或自动更新作为 roadmap 必做退出条件。它们可以在 MVP 后开展真实质量工作，但不得用目标数字冒充已测结果。

### 阶段归属

- **R1**：以现有 Windows Java 25 构建、实际 Minecraft 26.2 exporter 导出和外部 validator 证据验收 exporter 的完整注册表/合法状态/机器事实、唯一 default representative、按 `block_id` 推导的 isolated 四视角 preview/mask、pending failure/skip、exporter commit gate 与外部 validator 的分工、checksum 和 fresh staging 原子提交。
- **R2**：验收 Index Studio、导入验证、SQLite、任务状态、WebUI loopback 和进程内 Worker。
- **R3**：验收所选 OpenAI adapter、协议条件 `store` 行为、strict wire Schema、人工审核/覆盖和 candidate-build gate；Responses 发送 `store=false`，Chat Completions 省略 `store`，不验证远端 retention 或第三方实际模型身份。
- **R4**：验收临时 candidate/current fixture 上的 MCP stdio 四工具、搜索/排序和本地降级；不激活生产 current。
- **R5**：统一验收 Linux CPython `3.14.7` hash-lock 安装/`pip check`/Web 和平台行为、Linux MCP stdio、Linux wheel/ABI、Linux Java 25/runtime/exporter 独立重跑和最终双平台源码锁依赖/运行时复现，以及双 release、activation gate、current 原子切换、回滚和首发清单。

R2–R5 的契约继续保留在本文后续章节，但其实现、命令和证据只在对应阶段开始后建立；它们不是 R1 blocker。

## 2. 固定基线和测试材料

Fabric exporter 和 Minecraft 必须使用精确基线：

```text
Minecraft Java       26.2
Java                  25
Fabric Loader         0.19.3
Fabric API            0.157.0+26.2
Loom                  1.17.19
Gradle                9.5.1
Mappings              Minecraft 26.2 native Mojang names/unobfuscated; no external mappings artifact
```

正式平台仅为 Windows 11 x86_64 和 Linux x86_64（Linux `manylinux_2_17` / glibc `>=2.17`）；Python 基线为 CPython `3.14.7`。R0 只锁定实际引入的 Python tooling 依赖及 hashes；后续依赖在使用前必须精确/hash 锁定，Windows 在对应阶段验证，Linux 依赖安装/运行和最终双平台验证统一在 R5，不预锁未实现的 R2-R4 栈。等价技术替换必须先按 [`decisions.md`](decisions.md) 留下影响与批准记录。

测试仓库只允许原创程序生成的 fixture 生成器源码：运行时可在临时目录生成最小 PNG 色块/几何/透明样例、人工构造 JSONL/Schema、空 SQLite、模拟 release/current 和不含原版内容的伪 metadata；不得提交生成后的 PNG、非空 SQLite、Minecraft JAR、资源包、纹理、模型、字体、声音、粒子、动画、截图或真实导出数据；详见 [`security-and-distribution.md`](security-and-distribution.md)。

## 3. 测试证据规则

每个通过声明必须至少有：

```text
repository path
exact command
exit code
test summary/report path
```

发布产物还必须有：`manifest.json`、`quality_report.json`、完整 SHA-256、版本、release ID 和原子切换报告。JSON、Schema 字段、metadata、manifest、current 和 release 中的 hash 字符串必须使用 `sha256:<64 lowercase hex>`；唯一文本例外是 `checksums.sha256` 与 `schemas.sha256` 的行首 digest，均不带前缀。`checksums.sha256` 排除自身，格式为 `<64hex><two ASCII spaces><release-relative-posix-path>\n`，按路径排序；`schemas.sha256` 格式为 `<64hex><two ASCII spaces><schema-id><two ASCII spaces><canonical-repository-relative-posix-path>\n`，按 schema ID UTF-8 bytes 排序，路径不声称位于 release。测试失败、缺本地合法导出或缺真实 release 时必须准确记录 `SKIPPED_LOCAL_EXPORT_MISSING` 或 `SKIPPED_LOCAL_RELEASE_MISSING`，不能改用假数据声称集成通过。没有证据不得更新路线图 `[x]`。

## 4. R0/R1 Schema、数据分层和确定性测试

### 4.1 strict Schema

静态测试必须区分本地完整 JSON Schema Draft 2020-12 与两种 OpenAI Structured Outputs wire 子集；以下本地 strict Schema 对象均拒绝未知字段：

```text
export-manifest.v1 / export-block.v1 / export-state.v1 / export-variant.v1
export-failure.v1 / render-metadata.v1
block-record.v1 / state-record.v1 / visual-variant-record.v1 / annotation-record.v1
manual-override.v1 / skip-review.v1 / qualification-review.v1
release-manifest.v1 / release.v1 / current-pointer.v1
provider-batch-envelope.v1 / annotation-batch-output.v1 / annotation-wire-item.v1
query-spec-output.v1 / rerank-output.v1
mcp-index-info-output.v1 / mcp-search-blocks-output.v1 / mcp-block-details-output.v1
mcp-compare-blocks-output.v1 / mcp-error.v1
```

`SearchRequest`、各 MCP tool input 和 WebUI 请求是输入契约，不另占用 D-030 当前 Schema ID；它们仍必须通过各自 strict 输入校验。

必须验证枚举、正则、长度、数值范围、`additionalProperties=false`、引用存在性、版本一致性和重复数组。两种 adapter 共用并必须测试 `annotation-batch-output.v1`（元素 `annotation-wire-item.v1`）、`query-spec-output.v1`、`rerank-output.v1`；wire `name` 分别为 `annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`，name 只能匹配 `[A-Za-z0-9_-]{1,64}`。Responses 请求位置必须是 `text.format`，Chat 请求位置必须是 `response_format.json_schema`；每个字段 required、每个 object `additionalProperties=false`，只使用所选 adapter probe 实际证明支持的关键字。不得把任意 Draft 2020-12 Schema 直接假定可发送。`QuerySpec` 必须检查 `source`、hard/soft 业务规则；`unknown` 不满足硬约束。LLM 输出 `block_id`、状态、几何、机器行为、透明/发光/支撑或发布状态必须被拒绝并创建 high review。machine/AI/override 三层不得互写；override 必须可重放并引用真实 target。

### 4.2 导出和导入

R1 exporter 验收只覆盖最小确定性边界：完整 `minecraft` registry 登记、全部合法状态和机器事实、唯一 default representative、按 `block_id` 推导的 isolated 四视角 preview/mask、failure/skip pending 语义、fresh staging 原子提交和 checksum。exporter commit gate 只检查最终引用/计数/状态、精确 render 路径与文件集、PNG 基础可读性和尺寸、checksum 生成、fsync 及一次原子提交；外部 Python validator 对最终非 staging 包执行一次 strict Schema、跨记录/registry 关系、资源黑名单、PNG 语义/质量、checksum 与 artifact digest 复算，并复用同一次文件读取/PNG 解码。两者不得重复相同全量检查。R1 不要求 workspace 人工 `skip-review.v1`；该审核属于 R3 candidate-build 前置。R1 不实现逐逻辑缓存/游标恢复、复杂幂等冲突、16 类回归矩阵、复杂跨环境图片比较或 R2–R5 测试命令。

导出包导入验收属于 R2；R1 仅验证同一 fixture 的重导出确定性。

原创 fixture 必须覆盖：

1. 每个输入 `minecraft` registry block 恰好一条 Block 记录，虚假/重复 ID 为 0；
2. R1 每个 Block 至少一个 exporter selected representative，或 exporter failure/skip 保持 pending；workspace `skip-review.v1` 的人工审核字段由 R3 candidate-build 另行验证；
3. default/canonical/represented state 均属于该 block 合法状态；
4. 机器事实由 fixture 确定性测量生成，AI 文本不能覆盖；
5. PNG 可读、MIME/尺寸/hash 与 render reference/manifest 一致；
6. 缺文件、错误版本、虚假引用、重复主键、非法状态和 hash mismatch 被拒绝；
7. 同一 fixture 重导出产生相同机器事实、排序键和 artifact hash（时间字段除外）。

R1 validator 对 1000 renders 的验收必须采用单次读取/解码；真实旧 validator 已超过 600 秒，不得延长 timeout、增加并行框架或磁盘缓存。目录名必须等于 `export_id` 且不是 staging；旧 `exp_...`/`vv_<hash>` 身份（已删除）的导出直接作废重导。

Java 的 material/framing/PNG semantic checks 属于 per-variant render acceptance，必须在写入 `variants.jsonl` record 前完成；package commit gate 仍只执行第 4.2 节规定的 structural/basic PNG/package checks，Java material identity 保持 authoritative，Python strict canonical checker 只作 defense-in-depth。

Oracle Gate 3：`PASS`。该 closure 只关闭 D-043/R1 P0，不代表 R3、candidate 或人工审核完成。

Phase 1 的当前渲染策略为 `render.v2`；未修改的历史 `render.v1` records、workspace/release data 在当前 v1 Schema ID 下保持 valid，并只在其 record/run context replay，new/current fixtures 默认使用 v2。preserved old export package 在 repository Schema bytes 变化后不由 current external validator 重新验证；其 embedded `schemas.sha256`/`schema_inventory` 是 binding evidence，current validation 必须报告 `SCHEMA_INVENTORY_HASH_MISMATCH`。不得 bypass hash、自动迁移、增加 historical Schema snapshot layer 或使用 version-aware validator fallback；旧 package bytes/reports 只作为历史证据保留。PNG 质量判断允许一个或多个透明 edge-on quadrant，但 entirely transparent composite 仍失败，`nether_portal` 在 composite 非空时保留。Java resolved submission 的 material identity 是 missing-texture authority，覆盖 whole-model/material missing、missing model、vanilla material/quads 和 Fabric mesh（`SpriteFinder` + block-atlas missing-sprite UV bounds）；`minecraft:missingno` source checker 的精确颜色为四象限 `#F800F8`/`#000000`，rendered colors 不具权威性。Python 移除宽松全局 magenta/black ratio，只保留严格 canonical-checker defense-in-depth，ambiguous pixels 不是 proof。

本项不增加 block-entity fixture；`minecraft:end_portal` 与 `minecraft:end_gateway` 必须继续登记全部合法 states，并在进入渲染前以既有 `BLOCK_ENTITY_FIXTURE_UNSUPPORTED` 写入 ordinary auditable pending skip，不生成 preview、mask 或 render directory，后续需要 human review，绝不静默过滤。D-043 Gate 3/R1 P0 closure 已验证最终 pair：`run/blockpedia-data/exports/26.2/export_20260816T091512Z/` 与 `run/blockpedia-data/exports/26.2/export_20260816T093009Z/` 均有 `1196` blocks/variants、`32366` states、`1140` selected、`56` pending skips；validator reports 均通过，SHA-256 分别为 `d7c6c166695ac4b56ae3f2720aa972b749429f2ed4d89c1738a4293891c2aa3d` 和 `5ffaccdfb35e010bc2333504e4d223635b76e4d6afb7a88d3d8111a7c3d3904b`。pairwise report `run/blockpedia-data/reports/export_20260816T091512Z--export_20260816T093009Z-pairwise.json` SHA-256 为 `a328dc6e64ce3423995ec268d760d8108c2bf79dd0ff9d2ee7b8afe7d8254699`、status=`passed`，全部 `3420` 个 render artifacts match；pair signatures 以 `f39a...` 与 `fbd3...` 报告，在 pair 内一致且不同于 pre-amendment signatures。预期仍为 `43` 个 block-entity fixture skips 与 `10` 个 invisible/technical skips；`melon_stem`、`pumpkin_stem`、`tripwire` 的 `OBJECT_TOO_SMALL` 仍是 ordinary reviewable R3 pending items，不是 R1 blocker。既有旧导出、validator reports、R3 run 和 `152` 个 rerender events 保持历史证据，不执行旧 rerender events；R3、candidate、human review 和 Linux R5 义务不因本 closure 改变。

动画确定性测试必须验证 scoped client-only gate：await resource reload 后只取消 `TextureAtlas.LOCATION_BLOCKS` 的 `cycleAnimationFrames`，成功/失败均 clear gate，不修改 private field；固定 resolver seed `42L` 保持不变，实际控制记录在 renderer options/environment identity 中。不得以记录观察到的动画相位代替控制。

## 5. R3 OpenAI provider contract tests

Provider 测试必须使用本地 fake endpoint 或脱敏协议 fixture，不发送真实 key，分别覆盖两种显式 adapter 的 wire 形状和共享语义：

1. `offline_annotation`、`query_spec`、`visual_rerank` 均使用同一个 Studio active profile 的同一个 configured/requested `model_id` 和所选 `adapter`；成功响应必须有 string `model`，但 model echo mismatch 不失败、不持久化且不替换 requested `model_id`；可保存多个非活动 profile，但全局最多一个 active；MCP 不读取或比较 provider snapshot、Keyring 或可变 active profile；
2. 两种 adapter 都发送图片、strict JSON Schema 和稳定错误分类；Schema ID/name 分别为 `annotation-batch-output.v1`/`annotation_batch_output_v1`、`query-spec-output.v1`/`query_spec_output_v1`、`rerank-output.v1`/`rerank_output_v1`，没有 `json_object`/自由文本正常 fallback。Responses 使用 `text.format`、`input_text`/`input_image`；Chat 使用 `response_format.json_schema`、`text`/`image_url`；
3. Responses wire 必须是 `POST /responses` 并发送 `store=false`；Chat wire 必须是 `POST /chat/completions` 且省略 `store`。能力 probe 只验证所选 adapter 的 endpoint、图片、strict output、错误分类和 string model structural validity；probe 不验证 model echo equality、第三方实际模型身份或远端 retention，不能自动改用另一协议；
4. R3 provider 请求的总尝试最多 2 次；仅已观察的可恢复错误最多补试一次，不能据此为 R1 exporter 预建通用重试框架；
5. Chat 只接受 `choices[0]` 的非流式 JSON 字符串内容、`finish_reason=stop` 和无 refusal；Responses/Chat 的 refusal、incomplete、认证、权限、能力缺失和不可修复请求错误不重试；
6. 离线最终失败创建 high `needs_review`；Studio 在线历史 lane 最终失败可保留本地结果；MCP 没有在线 provider failure/degrade path，始终 local。
7. usage、完整 response、图片、Authorization、key、绝对路径不进入返回对象、SQLite 或日志；只留脱敏 request ID；
8. cache key 缺任一字段即失败：`image_hash`、`machine_metadata_hash`、`adapter`、`prompt_version`、`model_id`、`schema_version`、`base_url_stable_id`、`stage`；
9. artifact 与 adapter/profile/requested model/prompt/wire schema/search/base URL stable ID/secret reference、input hash 和 cache key 在 release freeze 中可复核；response model echo 不进入 artifact、cache、run 或 release lineage；不出现 Token usage、费用或预算字段；MCP 不读取 Keyring/env，也不执行 provider 降级。

## 6. R4 搜索和排序 contract tests

使用原创小型 release fixture 验证：

1. R4 fixture 使用通过 build/activation gate 的 pointer-resolved release；MCP 运行时不检查 index format 或其它 build-time integrity。MCP 省略版本使用 default current；显式 malformed/unknown/unpublished version 按既有 `-32602`/`VERSION_NOT_AVAILABLE` 规则失败，不回退；历史 `release_id` selector 被拒绝；MCP 不读取 provider snapshot、Keyring 或 active profile；
2. `search_blocks` 严格验证 required keywords：1–16 项、trim 后 1–64 Unicode 字符、禁止空项/重复项；旧 `query`/`context`/`query_spec` 和未知字段为 JSON-RPC `-32602`；
3. pointer 解析出的 release 只对 `eligible`/`conditional` 候选执行 FTS5 `trigram` 或 normalized-LIKE keyword recall，并做确定性 local ordering；
4. MCP 不执行 `search-ranking.v1` QuerySpec 权重、hard filter、family、QuerySpec 或 provider rerank；Top-24 后按 limit 生成 contact sheet；
5. 相同 release、normalized keywords 和 fixture 的 local ordering、candidate ID、tile mapping 可重复；
6. provider 永不调用；candidate `score_source=local`、`reranked_by_llm=false`、`hard_filters=[]`；
7. 正常关键词空集是 `isError=false` 的成功结果；非法 input shape 使用 JSON-RPC `-32602`，格式合法但未知 block/release 等工具执行错误使用 `isError=true`；
9. MCP 只读取 release 中的 ID/state/image mapping，不新增、删除或改写候选、机器事实或图片映射。

## 7. R4 MCP protocol tests

必须以子进程运行 `block-index mcp` 并覆盖：

1. stdout 每条消息可作为 JSON-RPC/MCP 解析，stderr 诊断不污染 stdout；MCP 不写 data-root logs、cache、临时文件或其他持久化状态；
2. `tools/list` 只有 `index_info`、`search_blocks`、`get_block_details`、`compare_blocks`，不存在 HTTP/resources/任意 SQL/写工具；
3. 每项暴露 strict inputSchema/outputSchema；严格 version pattern、未知字段/其它非法参数为 `-32602`，合法 `tools/call` 中的未知 tool name 为 Invalid Params `-32602`，未知 JSON-RPC method 为 `-32601`；
4. `structuredContent` 与同一对象产生的 TextContent JSON 深相等；
5. `index_info` 无图；search/compare 有稳定编号 PNG 联系表 ImageContent；details 有四视角 PNG；
6. 图片 metadata 包含 ID、MIME、尺寸、hash、purpose、content index 和 mapping，不含绝对路径；
7. 工具错误 `isError=true` 与成功降级 `isError=false + warnings` 分层；协议错误使用 JSON-RPC error；
8. current/default version、strict version pattern、显式未发布版本、禁止历史 selector、未知 ID、keywords 空搜索、旧字段/未知字段 `-32602`、pointer/path/index/按需图片读取失败符合 [`mcp-api.md`](mcp-api.md)；不把 manifest/checksum/schema/quality/index format/PNG 全量验证加入 MCP runtime test；
9. MCP 进程执行全部路径后不写 SQLite、文件、cache、logs 或 current；联系表和 ImageContent 使用内存 bytes，provider 降级也成立；允许进程内 snapshot cache，但不产生持久化写入。

### 7.1 D-052 最小 focused acceptance

D-052 只允许最小 focused tests，不新增真实基数 fixture、全量矩阵或新的 evidence report。测试范围必须限于：

1. 四工具 advertised `outputSchema` 是 strict `oneOf`，分别组合既有成功 Schema 与 `mcp-error.v1`；成功/错误的 `isError` 分层、structuredContent/TextContent parity 和既有 Schema 校验保持一致。
2. MCP 读取/解析 `current.json`，正确执行 default/显式版本和严格 tool input/version 语义；下一请求观察 pointer 切换并载入新 `minecraft_version+release_id` snapshot；不接受历史 `release_id` selector。
3. 相对路径防逃逸、明显 symlink/junction/reparse 拒绝、指定 index 打开失败、实际响应 PNG 按需读取失败均 fail closed，且不回退其它 release；测试不得把这些安全边界写成 release 完整性证明。
4. 当前 D-053 MCP focused tests 不发送 provider request，因此不测试 55/15/30 deadline、provider retry/reuse 或 `auto`/`required`。
5. 测试 pointer snapshot refresh、同步本地查询移出 event loop、strict output parity 和 zero writes；这些是 D-052 保留的最小 focused boundary。

这些 focused tests 不验证、不声称 MCP 运行时验证 immutable、manifest hash、checksums、schema inventory、quality report、index format 或 PNG/index projection 全量完整性；构建后离线篡改/损坏只有实际打开、SQLite 查询或 PNG 读取时可能暴露。没有实现、命令和报告证据时，不得将本节写入路线图 `[x]`。

## 8. R2/R3/R5 WebUI、任务和安全测试

### 8.1 WebUI/Worker

必须覆盖：

- loopback host 拒绝和固定 `127.0.0.1:8765`；`--host`/`--port` 及其环境/profile 配置均被拒绝；Python 只有两个命令；
- CLI/env/profile/default 优先级；Keyring 优先、env 只读回退、SQLite `secret_reference`、前端掩码；
- 多个非活动 provider profile/唯一 active、profile/probe/enable/disable；显式 adapter 和同一个 configured/requested model 用于三阶段；Responses `store=false`、Chat 省略 `store`；不以 response model echo equality、远端模型身份或 retention 作为 enable gate；页面显示 requested identity 可能被 gateway 改写的简短 warning；待发送预览可取消；
- import check/import、版本匹配、run 创建、阶段进度、pause/resume/cancel、用户 POST recover、单项失败 retry；启动只读 stale running，不自动写状态；
- `running` 超时由显式 recover 才改变且只作用于未完成 job，成功 job 不重复；provider 不无限重试；
- normal/high review、机器事实只读、override allowlist、operator/time/reason/source version/target ID audit；
- candidate-build check/build 只验证 R0–R3 及 candidate 前置；activation-check/apply 才验证 R0–R4、activation gate、用户确认、`set_as_default` 和 current 原子替换；第一 release 可 build 不可 apply、不可变保护、每版本至少两个保底 release、rollback hash 不变；
- search test 使用显式版本和 keywords local-only path，与 MCP 输出保持结构化字段 parity；不持久化 query，不调用 provider，空结果成功并显示 `hard_filters=[]`、`reranked_by_llm=false`；
- 页面不存在 Token/usage/cost/budget 字段；日志为零遥测且脱敏 key、Authorization、图片、完整 response、绝对路径。

### 8.2 原子发布故障注入

测试必须验证：

1. 写临时 current 前旧 current 仍可读；
2. 临时文件完整写入、flush/fsync 和 hash 校验后才 replace；
3. 成功后 current 只指向完整、不可变、manifest hash 通过的 release；
4. 中断、flush 失败、replace 失败或进程崩溃时旧 current 仍有效，或下次启动可恢复完整临时文件，绝不出现半 JSON；
5. rollback 只改变 current 指针，旧 release 目录/manifest/图片/审计 hash 不变；
6. 发布中 MCP 只读旧 release，切换后只读新完整 release；
7. 并发 apply/rollback 串行化，第二请求返回明确状态冲突，不覆盖第一请求。

### 8.3 Import check、chooser 和 SSE 最小验证

在 R2 WebUI 验收中至少覆盖以下行为；这些是新增架构的最小门，不改变既有状态枚举、stage 顺序、loopback、无 CORS/CSRF 或 MCP 规则：

1. `POST /api/imports/check` 返回 `202` 和 `pending`，state 目录扫描只接受严格 check ID、无链接/reparse entry 的 `state.json`；`GET /api/imports/checks/{check_id}`、`GET /api/imports/checks?minecraft_version=&limit=` 和 `/events` 能恢复同一 authoritative state。首页始终有 `Recent Checks`，最多 5 个 distinct exports，active first，目录 chooser 显示派生 marker，不创建第二持久索引。
2. 两个并发 opaque refs 指向同一 `(minecraft_version, export_id)` 时，coordinator `RLock` 只保留一个 active check 和一次 validator；exact active duplicate 为 `202`/同一 `check_id`/`reused=true`。passed check 的 manifest/checksum raw SHA-256 anchors 未变时为 `200`/同一 check/`reused=true` 且 validator 调用数为 0；任一 anchor 改变、failed 或 interrupted 时才新建 `202` check。
3. initial snapshot、15 秒 heartbeat、reconnect 和 client disconnect 都不取消或改变工作；server 在 source snapshot 期间重启则稳定失败为 `IMPORT_CHECK_INTERRUPTED`，不得 automatic resume。chooser root 严格为 `<data-root>/exports/<minecraft_version>`；traversal、symlink、Windows junction/reparse、unexpected mount crossing、snapshot hardlink 和 stale ref replacement 均被拒绝。
4. 每个真正执行的 check 的 `Validator.run` 计数恰好为 1；observational snapshot/validator callback 不增加 scan/read/decode/hash，callback on/off 的 report、PNG read/decode 计数和最终 hash 相同。进度持久化失败产生稳定失败；`completed` 在每个 subphase 内单调递增，未知 total 保持 null/0，SSE 发完整 snapshot 而不是逐 item announcement。
5. `POST /api/imports` 严格只接受 `check_id + copy_mode=copy_to_workspace`，不接受 `project_id`；并发 duplicate import 只产生一个 `import_id`/`run_id` 和一个最终 `work.sqlite3`。`creating` duplicate 返回同一 run 的 `202`，valid `created` 返回同一 run 的 `200`，完成首次请求可为 `201`；未验证 `work.sqlite3` 前不得 deep-link。
6. 模拟 restart 时 `creating` association 能 reconcile 有效 final workspace；无效/缺失 final workspace 时保留原 reservation 和稳定 failure/retry，不静默创建第二 run。旧 state 缺少 association 时按 version/export/manifest hash 发现既有 workspace，且不改 SQL/schema/hash。
7. status hero/live work 同时呈现完整 11-stage timeline、heartbeat、item aggregate、current/recent/latest allowlisted projection；check UI 的 `unchecked`、`checking`、`passed_not_imported`、`imported`、`failed/interrupted`、`changed_since_check` 动作和 checked marker 正确。成功 `/ui/imports` 使用 `HX-Redirect /runs/{run_id}`；页面刷新不依赖 `localStorage`。
8. 绝对路径、chooser ref/token、secret、raw details、exception、worker ID、cursor 均不出现在 state/summary、response、URL、HTML、log、cache、SQLite 或 SSE；summary 中错误/ID/时间只经过 allowlist sanitization。
9. 验收确认 `workspace.v1.sql` 字节内容及其既有 hash 均 unchanged；本增强不新增 `index.json`、`owner_instance_id`、SQLite 表/字段、generic migration、Schema ID、依赖、服务、消息总线或 arbitrary generation framework。

### 8.4 D-040 focused acceptance evidence

D-040 只要求以下 focused evidence；不新增测试矩阵、报告层或平台矩阵：

1. **Plan TOCTOU all-or-none**：确认前所有 planned batches 可 inspect；对 payload signature、pending 集合、`effective_config_hash`、run-frozen provider/requested model 或 plan hash 的单项修改会 approve none；未修改计划一次 transaction 产生 one plan audit 和 per-job approval audits。
2. **Sequential continue vs fatal stop**：D-044 的 frozen bound 生效（offline default=`1`；上限为 `5`；online stages 固定=`1`）；item-local Provider error 后下一个 approved batch 仍发送；fatal code atomically 写 request evidence/review/job/stage/run failure/audit，后续 batch 不发送。
3. **Stage semantics**：valid low-confidence 与 item-local failure 不阻塞 `AI_ANNOTATE` drain，并进入 `VALIDATE`/`HUMAN_REVIEW`；fatal 立即停止 stage/run；最终 high review 仍阻断 candidate build。
4. **Retry idempotency/generation/siblings**：row/bulk POST 重复只产生一个 child；source 必须是 terminal AI job leaf，child 有 `retry_of_job_id` 和 deterministic nonce，failed child 可下一次 generation；同一 transaction resolve open provider-review siblings 且保留 source evidence/request rows。
5. **Frozen lineage**：切换 mutable active profile、adapter、requested model、base URL 或 payload/config lineage 后，既有 run 不替换 provider，pre-send recheck 拒绝旧 approval；auto-approved cursor 在 startup stale detection 中保持，只有显式 `recover` 改变 stale state。
6. **Generic retry guard**：generic `retry-failed` 不选择 fatal/provider AI job；同一 source logical request 不能绕过两次总尝试预算，`PROVIDER_CANCELLED` 不进入 bulk wave。
7. **UI visibility**：同一 scrollable work area 同时展示至少 6 个 actionable `running`/`failed`/`needs_review` rows，pending/recent summary 保持 bounded；Provider error row 有 row retry，confirmed bulk action 只 retry eligible failed leaf batches；succeeded、无 Provider error 的 low-confidence review 被排除，原始 evidence 可见。
8. **Strict request bodies**：Responses/Chat 的现有严格 body 分别保持 `/responses`+`store=false` 与 `/chat/completions` 且省略 `store`；batch confirmation、row retry 和 bulk retry action 拒绝未知字段及 auto-mode/config bypass 字段，不提供 protocol/model fallback。

### 8.5 D-041 focused acceptance evidence

D-041 只增加以下 call-count/monkeypatch evidence，不使用 flaky wall-clock threshold，也不新增测试矩阵或报告层：

1. **Aggregate bounded path**：对 `100+` pending jobs，aggregate plan preview/confirmation 只读取并验证已持久化 job/cursor identity；contact-sheet、prompt、payload 和 machine-metadata rebuild hooks 对每个 pending job 的调用数为零。
2. **Persisted mismatch fail closed**：任一 persisted `input_signature`、cursor `payload_signature`/`input_hash`、`tile_ids`/`variant_ids` 或 frozen config/provider snapshot mismatch 时，confirmation transaction approve zero jobs。
3. **Final pre-send gate**：每次 actual send 前调用 one-batch full rebuild/recompute；注入 signature mismatch 时 revokes that job approval、pauses before HTTP，provider call count 为零。
4. **Lazy individual preview**：单个 batch 的既有 safe preview 仍可重建 bounded payload，并展示 exact safe text/image/machine metadata；aggregate preview 不替代该 per-job inspection。

### 8.6 D-042 focused acceptance evidence

D-042 只要求以下 focused evidence；不新增 Schema、报告层或质量指标：

1. **Slim model-visible text**：两种 adapter 都发送 `prompt.v2` 的 exact allowlisted instruction/tiles/tile metadata，同时 local envelope、hash、cache、signature 和 lineage checks 仍使用 full metadata；prompt size comparison 只作为 evidence，不作为 quality claim。
2. **Legacy compatibility and TOCTOU**：`prompt.v1` 与其它历史 version string 保持 byte-for-byte/current behavior；source change 触发既有 TOCTOU，`prompt.v2` 只能由 fresh run/profile snapshot 选择，current pending v1 jobs 不被迁移、re-sign、cancel 或 delete。
3. **Final diagnostic allowlist**：malformed JSON、missing required、wrong type、additional property 和 duplicate-array 的 final failures 只持久化六个 safe diagnostic fields；successful repair 不持久化 diagnostic。验证 `phase/path/keyword/observed_type/observed_length` bounds 和 `$` parse/shape path。
4. **No unsafe disclosure**：DB/API/UI 不出现 raw output、value、prefix、provider message、exception、repair context、secret、prompt/image 或 path-like value；UI 只显示六字段 ordinary labels，不能由 `path` 渲染 raw value。
5. **Validation preservation**：Provider/Worker full wire/local validation、ID/hash/cache、`annotation-record`/variant/`VALIDATE`/release gates、local `uniqueItems`、max-one-retry 和现有 Provider/Worker/release validation 仍通过；只删除 `_hash_json` freshly produced hash 的 tautological regex check，分类保持 externally observable 等价。

### 8.7 D-044 Phase 1 focused acceptance requirements

D-044 当前只记录 owner-approved contract 和后续 focused acceptance requirements；以下不是 implementation、test、runtime 或 provider completion evidence。验收不得新增报告层、平台矩阵或 Schema/SQL 契约。

1. **Bounds and accounting**：profile validation 接受 `offline_annotation.concurrency` 的整数 `1..5`，默认 `1`；`query_spec`/`visual_rerank` 始终为 `1`。测试确认 concurrency 计数 logical batches 而不是 HTTP attempts；每个 logical batch 仍最多两次总尝试，且没有 protocol/model/provider fallback。
2. **Shared executor and global bounds**：在同一进程内所有 run 只使用一个 process-lifetime in-process executor，容量最多 `5`；不存在 per-run executor。并发注入必须证明 global active sends `<=5`，每个 run `<=` 其 frozen offline bound，且 executor 不因 run 结束而重建。
3. **Ordered contiguous claim barrier**：对交错 approval、不同 run 和 slot 竞争进行 focused scheduling test；只能 claim frozen order 中从当前未完成位置开始的 contiguous approved prefix，不能越过第一个未 approved/失效/停止项，也不能以 claim 伪造 durable pending provider reservation 或 remote exactly-once claim。
4. **Final pre-HTTP gate and thread isolation**：每次 send 前都调用 full payload/contact sheet/prompt/machine metadata rebuild、full signature、approval/lineage、run/stage 和 stop/slot gate；注入任一 mismatch 时 HTTP call count 为零。测试确认 HTTP 不在 SQLite transaction 中，且不同线程不共享 DB connection/transaction、provider client 或 provider mutable state。
5. **Send-started race semantics**：在 send-started linearization 前后分别注入 pause、cancel 和 fatal；claimed-unsent/later sends 必须停止，already-started call 可以完成并写 evidence/item terminal state，但不得 revive failed/cancelled run/stage。fatal 覆盖 paused，不覆盖 durable cancelled；测试不得以 fake in-flight cancellation 作为通过条件。
6. **Stop, crash and recovery**：stop waits for started futures；live futures 或 DB work 存在时 stale recovery 被阻断；completion 只在 no DB work + no futures 时成立。模拟 send-after-before-commit hard crash，确认 unknown outcomes 上限为当时 frozen concurrency，startup 不自动 resend，显式 `recover` 保持必要且未知结果不被当作成功。
7. **Profile invalidation**：只改变合法 offline concurrency 时保留 `verified`/`enabled` 且不调用 probe；改变 adapter、model、base URL、secret reference、Schema、prompt、search/ranking 或其它配置时，既有 invalidation/disable/fresh snapshot/rerun 规则仍生效。`query_spec`/`visual_rerank` 不可调度为其它值。
8. **Strict pristine same-run reconfiguration**：仅在 paused `AI_ANNOTATE` 且无 live future/provider request、provider-request evidence、annotation/AI artifact/provider review/AI review、send/result/retry/cancel evidence，同时每个 AI job pending、unapproved、ownerless、clean 时允许操作；任一 dirty/in-flight 条件都 fail closed。成功路径必须原子替换 frozen config/pending jobs，保留 R2/machine evidence，写 `R3_RUN_RECONFIGURED`，invalidate old plan，且不重用 approval/创建新 run。
9. **Release and forbidden-surface checks**：确认 scheduling concurrency 不出现在 release snapshot、`release-manifest.v1` 或 provider wire/record Schema；静态/fixture 检查拒绝 services、queues、per-run executors、adaptive concurrency、新 SQL/Schema/migration/status/dependency/CLI、fallback、retry budget change 和 fake cancellation。D-040/D-041 的既有 approval、lineage、audit、recover、sibling/generation 和 max-two-attempt acceptance 必须继续通过。

### 8.8 D-045 targeted banner refresh focused evidence

D-045 的本节只冻结最小验收预算，不代表 implementation、runtime export、WebUI refresh、AI annotation、candidate 或 R3 退出完成。只要求：

1. 使用 Java 25 完成 focused exporter build，并执行一次 exact `banner-repair` targeted complete export；existing exporter validator/check flow 必须通过，且验证 target set 恰为 32、normalized diff 恰为 32 个 skipped→selected 和 96 个 render files。
2. 使用 generated/artifact fixture 做 focused service、HTTP、rollback/recovery journal 和 mixed-lineage tests：passed immutable full check、exact base/targets、no-live-work gate、atomic source/file/SQLite replacement、failure recovery、`banner-refresh.v1` provenance 和 historical-envelope exclusion 均 fail closed/可回放。
3. 验证现有 1140 annotations、provider requests、jobs/reviews 不变，只增加 3 个 unapproved `banner_refresh_*` jobs（`12 + 12 + 8`），并且只 reset `AI_ANNOTATE`、`VALIDATE`、`HUMAN_REVIEW` 后从 `AI_ANNOTATE` 继续。
4. 在当前目标 run 上完成一次真实 WebUI refresh。不得要求新 full test matrix、第二次重复 targeted export 或重复 D-044 verification；D-043 已有 corrected pair evidence 不因本项重做。

## 9. R3 Phase C Gate C 质量报告（quality owner）

R3 Phase C 只有一个 Gate C：WebUI `POST /api/releases/check` 同步生成不可修改的 check report，`POST /api/releases/build` 在同一逻辑 snapshot 上生成派生 release report。两个报告都使用 `format_version=1`，但这是文件格式字段，不是新的 JSON Schema ID；quality report 不加入 D-030 Schema inventory。Phase C 不实现或要求 activation gate、`current.json`、MCP smoke 或第二个 release。

12 个 check item 的顺序和 code 固定如下，任何实现不得按字母序、失败优先级或 UI 顺序重排：

1. `REGISTRY_COVERAGE_100`
2. `BLOCK_VARIANT_OR_AUDITED_SKIP`
3. `EXCLUDED_QUALIFICATION_REVIEW_VALID`
4. `IMAGE_READABLE_AND_HASHED`
5. `LEGAL_STATE_VALID`
6. `MACHINE_SCHEMA_VALID`
7. `AI_SCHEMA_VALID`
8. `OVERRIDE_REFERENCES_VALID`
9. `NO_FALSE_IDS`
10. `HIGH_REVIEW_ZERO`
11. `FTS_READY`
12. `RELEASE_HASH_MANIFEST`

每个 `items[]` 元素必须且只能有以下字段：

```json
{
  "code": "REGISTRY_COVERAGE_100",
  "status": "passed",
  "blocking": true,
  "observed_count": 0,
  "error_code": null,
  "evidence": ["snapshot/manifest.json"]
}
```

`code` 只能是上述 12 个值；`status` 只能是 `passed|failed|not_run`；`blocking` 必须是 boolean；`observed_count` 必须是非负整数；`error_code` 必须是稳定错误码或 `null`；`evidence` 必须是安全相对路径数组。Gate C check 中第 1–11 项必须实际执行，`RELEASE_HASH_MANIFEST` 必须为 `not_run`；build 阶段的派生 release report 中 12 项必须全部为 `passed`。

check cache 的 `quality_report.json` 字段必须且只能是：`format_version`、`report_kind`（`candidate_check`）、`check_id`、`release_build_id`、`run_id`、`minecraft_version`、`status`（`buildable|blocked`）、`can_build`、`snapshot_fingerprint`、`items`、`created_at`、`updated_at`。`status=buildable` 当且仅当第 1–11 项全部 `passed` 且 `can_build=true`；任一 blocker 失败时为 `blocked`/`false`。`RELEASE_HASH_MANIFEST=not_run` 是合法的 buildable check 状态，不得被改写为 passed。

release 内派生的 `quality_report.json` 字段必须且只能是：`format_version`、`report_kind`（`release`）、`release_id`、`release_build_id`、`run_id`、`minecraft_version`、`status`（唯一值 `passed`）、`snapshot_fingerprint`、`items`、`built_at`。只有 12 项全为 `passed` 才能写入并进入 immutable release；它不复制 check report 的 `can_build`，也不接受 `blocked`/`not_run` 的发布状态。check report 内容不可修改，release report 是 build 派生物，二者不得通过互相写 hash 形成循环。

evidence 必须是相对于所属报告根的 POSIX 路径：非空、不能以 `/` 或盘符开头，不能包含 `\\`、空 segment、`.`、`..`、NUL、URL scheme 或绝对路径；路径必须通过同一 `lstat`/reparse/hardlink 安全检查，不能指向报告根之外或未列入相应 artifact 集合的内容。check report 只能引用其 check snapshot/cache root 内的允许文件，release report 只能引用 release 内已存在且由 `checksums.sha256` 覆盖的普通文件。evidence 只存路径，不存绝对路径、异常正文、key、usage、完整 provider response 或本地用户信息。

Gate C 的最小标准是：精确版本和 run 前置通过；上述 item 1–11 全部 passed；`excluded` 与无变体项的独立审核引用完整；机器/AI/人工引用和 FTS 投影均可重算；check report 可安全写入并 hash；`RELEASE_HASH_MANIFEST` 在 check 阶段保持 `not_run`。Gate C 不包含 MCP、`current.json`、activation 或 release 数量条件。build 只在 Gate C buildable check 与 pipeline 的 snapshot/TOCTOU 条件同时满足时继续；release report 写成 all-passed 后才生成 manifest、release metadata 和完整 checksums。

### 9.1 后续 activation gate 的边界引用

后续 activation gate、四工具 MCP smoke、两个独立 release、`current.json` 原子切换和回滚属于 R4/R5，不是 Gate C 的 item，也不得倒灌为 R3 Phase C 实现要求。后续质量契约只能引用本节的 release report/hash，不得首次补做 `excluded`/skip 内容审计。

## 10. 各阶段跨平台验证

R1 已由现有 Windows Java 25 构建、实际 Minecraft 26.2 exporter 导出和优化后的外部 validator 证据关闭；R2/R4 的 Windows 产品/功能证据按对应阶段执行。Linux CPython/Web、Linux MCP、Linux Java 25/runtime、Linux exporter、Linux wheel/ABI 和最终双平台源码/运行时复现统一由 R5 验证。正式支持平台仍为 Windows 11 x86_64 与 Linux x86_64；不宣称 Linux 已通过。跨平台验收仍只比较 canonical 机器字段、Schema、逻辑排序和构图规则；PNG byte hash 只有在完整渲染环境一致时才要求相同。各阶段仅在对应实现存在后执行其契约测试。

R0 已有的 tooling/schema 验收命令仍由路线图证据区记录；本节不把当前不存在的 R1–R5 精确命令伪装成现行命令。后续阶段应在实际实现后分别记录对应 provider、MCP、WebUI/release 和跨平台命令的路径、退出码、报告与锁哈希。

Gradle wrapper properties、wrapper JAR checksum、dependency lock 和 dependency verification metadata 必须进入可审计路径；R0 tooling Python lock 必须包含实际引入依赖的精确版本及 hashes，不能预锁未实现的 R2-R4 栈。MCP/R4 功能 gate 使用测试框架生成的临时 data-root/current fixture，不得依赖生产 current；Linux MCP stdio 和实际平台验证统一由 R5 执行，测试结束只清理临时测试目录，产品 MCP 仍零写。

若实现采用不同的锁定入口，必须先按 [`decisions.md`](decisions.md) 记录受控替换、影响证明和项目所有者批准，再以同等精确且带 hash 的命令替换；不得以未锁定安装命令声明复现。各阶段只在实际引入对应实现后执行一次所需验证。

## 11. 后置黄金查询质量工作（非 MVP 退出门）

MVP 之后建立不少于 100 条人工标注黄金查询，覆盖颜色、形状、组合、用途、风格、行为排除、方向和模糊描述；每条记录最佳、可接受、不可接受候选和硬约束。指标只有在真实报告存在时才可计算：

```text
Top5_acceptable_rate = queries_with_any_acceptable_in_top_5 / labeled_queries
hard_constraint_violation_rate = returned_candidates_violating_hard_constraints / audited_returned_candidates
candidate_mapping_consistency = exact_matching_image_tile_mapping / audited_image_tiles
nonexistent_id_rate = nonexistent_ids_in_structured_results / structured_ids
```

后置目标是 `Top5_acceptable_rate >= 0.90`、`hard_constraint_violation_rate < 0.02`、`candidate_mapping_consistency = 1.00`、`nonexistent_id_rate = 0`。这些数字不是当前结果，不得伪装为 MVP roadmap 必做退出条件或测试通过。
