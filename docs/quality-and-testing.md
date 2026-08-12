# 质量、完整性与测试契约

## 文档状态与导航

本文定义 Blockpedia MVP 的质量范围、确定性完整性、strict Schema、provider/MCP/WebUI 测试、Windows 11/Linux x86_64 运行验收、降级和原子发布门。正文使用简体中文；测试文件、字段、Schema、状态、错误码和命令保持英文。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 为规范性关键字。

本文服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md)、[`decisions.md`](decisions.md)、[`product-scope.md`](product-scope.md) 和 [`architecture.md`](architecture.md)。原始设计稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。具体契约链接如下：

- [`export-contract.md`](export-contract.md)、[`state-policy-and-rendering.md`](state-policy-and-rendering.md)、[`data-and-schemas.md`](data-and-schemas.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)；
- [`openai-provider.md`](openai-provider.md)、[`search-and-ranking.md`](search-and-ranking.md)、[`mcp-api.md`](mcp-api.md)、[`webui-and-operations.md`](webui-and-operations.md)；
- [`security-and-distribution.md`](security-and-distribution.md)。

当前仓库只有设计/治理文档，没有实现、真实导出、发布索引或测试报告；因此本文件定义验收，不宣称任何运行结果已通过。路线图复选框只有在本文件第 10 节规定的路径、命令输出、退出码、报告和哈希存在后才能勾选。

## 1. MVP 质量边界

MVP 必须退出的质量范围只有：

1. 确定性导出/导入、100% `minecraft` registry 登记、合法状态、图片和不可变 release 完整性；
2. 机器事实/AI 语义/人工 override 分层、strict JSON Schema 和引用边界；
3. OpenAI Responses 三阶段请求、严格一次总重试、错误分类、离线审核和在线本地降级；
4. MCP stdio 四工具、结构化输出/等价 TextContent、PNG 联系表/四视角图片和 stdout 纯净；
5. WebUI loopback、任务 pause/resume/recover、普通/高优审核、声明式覆盖、publish/rollback/cleanup；
6. Windows 11 与 Linux x86_64 的源码锁依赖可复现运行；
7. 原子 `current.json` 切换、回滚只切指针、release 不可变和最小安全/分发检查。

MVP **MUST NOT** 把黄金查询集、Top-5 指标、硬约束统计目标、排序权重调优、安装包、容器、系统服务或自动更新作为 roadmap 必做退出条件。它们可以在 MVP 后开展真实质量工作，但不得用目标数字冒充已测结果。

## 2. 固定基线和测试材料

Fabric exporter 和 Minecraft 必须使用精确基线：

```text
Minecraft Java       26.2
Java                  25
Fabric Loader         0.19.3
Fabric API            0.157.0+26.2
Loom                  1.17
Gradle                9.5.1
Mappings              Mojang mappings
```

正式平台仅为 Windows 11 和 Linux x86_64。R0 验证后 Python、FastAPI、SQLite 构建、OpenAI SDK、Schema engine、模板和测试依赖必须精确锁定，并记录 hash 或等价完整锁定信息；不得使用浮动版本、范围、`latest` 或未锁传递依赖。等价技术替换必须先按 [`decisions.md`](decisions.md) 留下影响与批准记录。

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

## 4. Schema、数据分层和确定性测试

### 4.1 strict Schema

静态测试必须区分本地完整 JSON Schema Draft 2020-12 与 Responses Structured Outputs wire 子集；以下本地 strict Schema 对象均拒绝未知字段：

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

必须验证枚举、正则、长度、数值范围、`additionalProperties=false`、引用存在性、版本一致性和重复数组。真实 Responses wire Schema 只允许并必须测试 `annotation-batch-output.v1`（元素 `annotation-wire-item.v1`）、`query-spec-output.v1`、`rerank-output.v1`；同时校验对应 `text.format.name` 为 `annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`，name 只能匹配 `[A-Za-z0-9_-]{1,64}`。每个字段 required、每个 object `additionalProperties=false`，只使用 endpoint probe 实际证明支持的关键字。不得把任意 Draft 2020-12 Schema 直接假定可发送。`QuerySpec` 必须检查 `source`、hard/soft 业务规则；`unknown` 不满足硬约束。LLM 输出 `block_id`、状态、几何、机器行为、透明/发光/支撑或发布状态必须被拒绝并创建 high review。machine/AI/override 三层不得互写；override 必须可重放并引用真实 target。

### 4.2 导出和导入

原创 fixture 必须覆盖：

1. 每个输入 `minecraft` registry block 恰好一条 Block 记录，虚假/重复 ID 为 0；
2. 每个 Block 至少一个发布视觉变体，或完整 skip（reviewer、时间、reason code、note、evidence）；
3. default/canonical/represented state 均属于该 block 合法状态；
4. 机器事实由 fixture 确定性测量生成，AI 文本不能覆盖；
5. PNG 可读、MIME/尺寸/hash 与 metadata/manifest 一致；
6. 缺文件、错误版本、虚假引用、重复主键、非法状态和 hash mismatch 被拒绝；
7. 同一 fixture 重导入产生相同机器事实、排序键和 artifact hash（时间字段除外）。

## 5. OpenAI provider contract tests

Provider 测试必须使用本地 fake Responses endpoint 或脱敏协议 fixture，不发送真实 key，覆盖：

1. `offline_annotation`、`query_spec`、`visual_rerank` 均使用同一个 Studio active profile 的同一个 runtime `model_id`；可保存多个非活动 profile，但全局最多一个 active；release-bound MCP 使用 release 冻结 snapshot，不读取或比较可变 active profile；
2. 请求 strict `text.format.type=json_schema`、`strict=true`，Schema ID/name 分别为 `annotation-batch-output.v1`/`annotation_batch_output_v1`、`query-spec-output.v1`/`query_spec_output_v1`、`rerank-output.v1`/`rerank_output_v1`，没有 `json_object`/自由文本正常 fallback；
3. 能力 probe 实际发送上述三个 Schema、图片、错误分类和实际 `store=false`；任何能力不能证明（包括 `store=false`）都 probe fail 并禁止 enable，不得提供确认或豁免绕过硬门；
4. SDK 内置 retry 加应用层 retry 的总尝试最多 2 次；网络、timeout、429、5xx、可修复 Schema 各最多一次；
5. `refusal`、`incomplete`、认证、权限、能力缺失和不可修复请求错误不重试；
6. 离线最终失败创建 high `needs_review`；在线最终失败保留本地结果、warning 和 `reranked_by_llm=false`；
7. usage、完整 response、图片、Authorization、key、绝对路径不进入返回对象、SQLite 或日志；只留脱敏 request ID；
8. cache key 缺任一字段即失败：`image_hash`、`machine_metadata_hash`、`prompt_version`、`model_id`、`schema_version`、`vocabulary_version`、`base_url_stable_id`、`stage`；
9. artifact 与 profile/model/prompt/wire schema/vocabulary/search/base URL stable ID/secret reference、input hash 和 cache key 在 release freeze 中可复核；不出现 Token usage、费用或预算字段；Keyring/env 无法解析时在线只本地降级并 warning，不写状态。

## 6. 搜索和排序 contract tests

使用原创小型 release fixture 验证：

1. MCP 省略版本使用 `default_minecraft_version` 的 current，显式版本使用对应 current；WebUI/API 显式版本只查 current；历史 `release_id` selector 被拒绝；精确未知/未发布版本失败并列可用版本，绝不回退；release-bound provider snapshot 使用冻结 profile/model/base URL/secret reference，不能读取或比较可变 active profile；
2. 版本、current release 完整性、legal state、资格、明确排除行为、明确必须支撑/透明/发光/方向/形状先于评分；`unknown` 不能满足硬约束；
3. FTS5 `trigram` 与无 trigram 时 `normalized_like` fallback 均覆盖名称/同义词、颜色、几何、用途、风格和行为；
4. `search-ranking.v1` 权重精确为 shape `.35`、color `.30`、use `.15`、name-synonym `.10`、style `.05`、behavior `.05`，未出现维度按规则归一化；
5. Top-24 → family 默认最多 2 个 → 8–12 联系表；显式 family/state comparison 才可解除；
6. 相同 release、QuerySpec、config 和 fixture 的排序、candidate ID、tile mapping 可重复；
7. provider 不可用时本地降级、warning、`reranked_by_llm=false`，不放宽 hard；`required` 正确失败；
8. 正常硬过滤空集是 `isError=false` 的空成功结果，含硬过滤原因和建议追问；未知 block/release 才是 `isError=true`；歧义含 `needs_user_choice`、歧义点、建议追问；
9. 未验证视觉条件使用 warning/`visual_constraints_verified=false`，不伪称满足；
10. LLM 不能新增、删除、改写候选 ID、block/state、图片或机器事实。

## 7. MCP protocol tests

必须以子进程运行 `block-index mcp` 并覆盖：

1. stdout 每条消息可作为 JSON-RPC/MCP 解析，stderr 诊断不污染 stdout；MCP 不写 data-root logs、cache、临时文件或其他持久化状态；
2. `tools/list` 只有 `index_info`、`search_blocks`、`get_block_details`、`compare_blocks`，不存在 HTTP/resources/任意 SQL/写工具；
3. 每项暴露 strict inputSchema/outputSchema；未知字段/非法参数为 `-32602`，未知方法为 `-32601`；
4. `structuredContent` 与同一对象产生的 TextContent JSON 深相等；
5. `index_info` 无图；search/compare 有稳定编号 PNG 联系表 ImageContent；details 有四视角 PNG；
6. 图片 metadata 包含 ID、MIME、尺寸、hash、purpose、content index 和 mapping，不含绝对路径；
7. 工具错误 `isError=true` 与成功降级 `isError=false + warnings` 分层；协议错误使用 JSON-RPC error；
8. current/default version、显式版本、禁止历史 selector、未知版本/ID、空搜索、图片失败符合 [`mcp-api.md`](mcp-api.md)；
9. MCP 进程执行全部路径后不写 SQLite、文件、cache、logs 或 current；联系表和 ImageContent 使用内存 bytes，provider 降级也成立。

## 8. WebUI、任务和安全测试

### 8.1 WebUI/Worker

必须覆盖：

- loopback host 拒绝和固定 `127.0.0.1:8765`；`--host`/`--port` 及其环境/profile 配置均被拒绝；Python 只有两个命令；
- CLI/env/profile/default 优先级；Keyring 优先、env 只读回退、SQLite `secret_reference`、前端掩码；
- 多个非活动 provider profile/唯一 active、profile/probe/enable/disable；同一个 model 用于三阶段；`store=false` hard fail/enable gate；待发送预览可取消；
- import check/import、版本匹配、run 创建、阶段进度、pause/resume/cancel、用户 POST recover、单项失败 retry；启动只读 stale running，不自动写状态；
- `running` 超时由显式 recover 才改变且只作用于未完成 job，成功 job 不重复；provider 不无限重试；
- normal/high review、机器事实只读、override allowlist、operator/time/reason/source version/target ID audit；
- candidate-build check/build 只验证 R0–R3 及 candidate 前置；activation-check/apply 才验证 R0–R4、activation gate、用户确认、`set_as_default` 和 current 原子替换；第一 release 可 build 不可 apply、不可变保护、每版本至少两个保底 release、rollback hash 不变；
- search test 与 MCP 等价结构化对象、不持久化 query、显示 warning 和 `reranked_by_llm=false`；
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

## 9. 发布完整性门

发布检查必须拆成两个独立门：`candidate-build gate` 由 WebUI `POST /api/releases/check` 执行，生成不可编辑的 `quality_report.json` 并决定 `can_build`；`activation gate` 由 WebUI `POST /api/releases/activation-check` 执行，决定 `can_apply`。`/api/releases/check` 和 `/api/releases/build` 只要求 R0–R3 及 candidate-build 前置；`/api/releases/activation-check` 和 `/api/releases/apply` 才要求 R0–R4、activation gate 和用户确认。candidate-build gate 只检查单个 release 内容完整性，**MUST NOT** 包含 MCP smoke、`TWO_INDEPENDENT_RELEASES` 或 current 切换；activation gate 检查至少两个 candidate、四工具 MCP smoke、原子 current 和 candidate 报告/hash 复核。任一 blocker 失败，WebUI **MUST NOT** build（candidate）或 apply（activation）。

| 检查代码 | 通过条件 | 证据 |
|---|---|---|
| `REGISTRY_COVERAGE_100` | runtime `minecraft` registry 与 Block 集合完全相等，虚假/重复为 0 | exporter manifest、SQLite count、hash |
| `BLOCK_VARIANT_OR_AUDITED_SKIP` | 每 Block 有发布变体，或 `skip-review.v1` 有 target_id/minecraft_version/reviewer/reviewed_at/reason_code/note/evidence/source_version/machine_failure_ref | skip audit |
| `EXCLUDED_QUALIFICATION_REVIEW_VALID` | **candidate-build gate**：每个发布视图中的 `excluded` target 都有独立 `qualification-review.v1`，包含 target_id/minecraft_version/reviewer/reviewed_at/reason_code/note/evidence/source_version/qualification/warnings；不得以 skip audit 替代 | qualification audit |
| `IMAGE_READABLE_AND_HASHED` | 发布 PNG 可读、PNG、512×512、四视角、hash 匹配，无缺纹理/全透明 blocker | image report |
| `LEGAL_STATE_VALID` | recommended/canonical/represented state 属于同 block 合法集合 | state report |
| `MACHINE_SCHEMA_VALID` | machine facts strict Schema 全部通过 | Schema report |
| `AI_SCHEMA_VALID` | AI artifact strict Schema、词表、候选边界和版本 hash 全通过 | artifact report |
| `OVERRIDE_REFERENCES_VALID` | active override target 存在，可按序 replay，不越权机器层 | replay report |
| `NO_FALSE_IDS` | block/variant/state/tile/image refs 虚假 ID 为 0 | reference report |
| `HIGH_REVIEW_ZERO` | 未解决 high review 为 0 | review snapshot |
| `FTS_READY` | FTS5 trigram 或明确 fallback 成功，所有发布变体可检索 | FTS report |
| `MCP_SMOKE_PASS` | **activation gate** 的四工具、structured/Text、图片 mapping、错误层均通过 | protocol report |
| `CURRENT_ATOMIC_READY` | **activation gate** 的 temp/flush/replace/崩溃恢复测试通过 | atomicity report |
| `RELEASE_HASH_MANIFEST` | release 全文件 hash 与 manifest/checksums 一致 | SHA-256 manifest |
| `TWO_INDEPENDENT_RELEASES` | **activation gate** 中该 Minecraft version 至少两个独立完整不可变 candidate release | release registry |

`quality_report.json` 使用严格的 release 质量产物契约，只能记录非秘密计数/状态和 release 内相对 evidence ref，不得写绝对路径、key、usage 或完整 response；报告自身必须纳入 release hash。它不是 D-030 冻结的额外 Schema ID。candidate report 的 `can_build` 不得伪称 `can_apply`；其中必须包含 `excluded` qualification 审计完整性结果。activation report 只能引用已 build 的 release ID、candidate report hash 及其复核结果，不首次补做资格内容审计。

## 10. 跨平台精确命令

实现交付时必须在 Windows 11 和 Linux x86_64 各执行以下命令，完整 stdout/stderr、退出码和环境/lock hash 保存到项目规定的测试报告路径。每份报告必须记录 OS version、CPU architecture、GPU、driver、render backend、Python/Java/Gradle/Fabric 版本和 lock/verification metadata hash。跨平台比较只要求 canonical machine fields、Schema、几何/构图逻辑和排序逻辑一致；PNG 字节 hash 只有在同一完整渲染环境重复运行时才要求一致，不得把不同 GPU/driver/backend 的 PNG 字节差异判为失败：

```text
python -m pip install --require-hashes -r requirements.lock
python -m compileall -q .
python -m pytest -q
python -m pytest -q tests/test_provider_contract.py tests/test_search_contract.py
python -m pytest -q tests/test_mcp_stdio.py tests/test_release_atomicity.py
```

Gradle/Fabric 工具链锁定验收还必须执行并记录：

```text
Windows: gradlew.bat --offline build
Linux:   ./gradlew --offline build
```

Gradle wrapper properties、wrapper JAR checksum、dependency lock 和 dependency verification metadata 必须进入可审计路径；Python `requirements.lock` 必须包含直接和传递依赖的精确版本及 hashes。MCP/R4 和 candidate/activation gate 测试必须使用测试框架生成的临时 data-root/current fixture，不得依赖生产 current；测试结束只清理临时测试目录，产品 MCP 仍零写。

若实现采用不同的锁定入口，必须先按 [`decisions.md`](decisions.md) 记录受控替换、影响证明和项目所有者批准，再以同等精确且带 hash 的命令替换；不得以未锁定安装命令声明复现。当前尚无这些实现路径和报告，不能填写通过证据。

## 11. 后置黄金查询质量工作（非 MVP 退出门）

MVP 之后建立不少于 100 条人工标注黄金查询，覆盖颜色、形状、组合、用途、风格、行为排除、方向和模糊描述；每条记录最佳、可接受、不可接受候选和硬约束。指标只有在真实报告存在时才可计算：

```text
Top5_acceptable_rate = queries_with_any_acceptable_in_top_5 / labeled_queries
hard_constraint_violation_rate = returned_candidates_violating_hard_constraints / audited_returned_candidates
candidate_mapping_consistency = exact_matching_image_tile_mapping / audited_image_tiles
nonexistent_id_rate = nonexistent_ids_in_structured_results / structured_ids
```

后置目标是 `Top5_acceptable_rate >= 0.90`、`hard_constraint_violation_rate < 0.02`、`candidate_mapping_consistency = 1.00`、`nonexistent_id_rate = 0`。这些数字不是当前结果，不得伪装为 MVP roadmap 必做退出条件或测试通过。
