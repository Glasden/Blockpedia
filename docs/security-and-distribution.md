# 安全、隐私与分发契约

## 文档状态与导航

本文定义 Blockpedia 的品牌声明、本地 loopback 安全边界、秘密管理、最小披露、提示注入防护、零遥测、原版资产禁入、可复现源码分发、不可变 release 和 `current.json` 保护。正文使用简体中文；字段、状态、错误码、Schema 和命令保持英文。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 为规范性关键字。

本文服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md)、[`decisions.md`](decisions.md)、[`product-scope.md`](product-scope.md) 和 [`architecture.md`](architecture.md)。原始设计稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。实现交叉引用：[`openai-provider.md`](openai-provider.md)、[`search-and-ranking.md`](search-and-ranking.md)、[`mcp-api.md`](mcp-api.md)、[`webui-and-operations.md`](webui-and-operations.md)、[`quality-and-testing.md`](quality-and-testing.md)，以及 [`data-and-schemas.md`](data-and-schemas.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)。

## 1. 产品身份和品牌

产品主品牌必须是 **Blockpedia**。Minecraft 仅作为兼容目标、数据来源和描述性名称使用，不得使产品看起来是官方客户端、官方百科、官方服务、资源包或 Mojang/Microsoft 产品。

公开 WebUI、文档、源码发行页、release 说明和任何可见产品介绍必须显著展示以下原文（大小写、标点和单词顺序不得改变）：

```text
NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.
```

不得使用 Minecraft/Mojang/Microsoft 官方 logo、字体、宣传图、UI 素材、音频、截图或其他官方品牌素材作为 Blockpedia 品牌元素。Blockpedia 自有 logo、字体、图标、示例截图和宣传素材必须原创或有明确可再分发许可。

## 2. 公开分发和本地数据

公开仓库、源码压缩包、Release、镜像和包索引只允许包含：

```text
source code
documentation
JSON Schema
empty database
fixture generator source
```

以下内容 **MUST NOT** 进入仓库、Release、镜像、文档附件、测试提交或任何公开 artifact：

- Minecraft client/server JAR、Fabric/game runtime JAR、资源包和其副本；
- 原版纹理、模型 JSON、字体、声音、粒子、动画、截图、录屏或其他原版资产；
- 用户本地导出包、真实 PNG、contact sheet、索引数据库、发布 release、人工覆盖、缓存和日志；
- API key、Authorization、Keyring 导出、完整 provider response、prompt 中的秘密、Token usage、费用或预算数据。

真实图片、机器事实、索引和 release 必须由用户在本地合法安装的 Minecraft Java `26.2` 环境和自制 Fabric exporter 生成。测试只使用原创程序生成的 fixture，不能复制/裁剪原版资产“伪造”测试。

源码必须支持 Windows 11 x86_64 和 Linux x86_64（Linux `manylinux_2_17` / glibc `>=2.17`）的可复现本地运行；Python 基线为 CPython `3.14.7`。R0 只锁定实际引入的 tooling 依赖及 hashes，后续依赖在使用前精确/hash 锁定；Windows 在对应阶段验证，Linux wheel/ABI、安装/实际运行和最终双平台复现统一由 R5 验证。MVP **MUST NOT** 制作安装包、容器、系统服务或自动更新，也不得引入额外运行服务；默认数据根必须在源码之外。

## 3. 网络边界和威胁模型

MVP 假设单用户本机运行：

- WebUI 只绑定 `127.0.0.1:8765`，不得提供 IPv6 监听选项；
- MCP 只使用 stdio；
- 不添加账号、CORS 或 CSRF，这是冻结决定而非遗漏；
- loopback + 本机操作系统访问控制是唯一网络边界，不能解释为适合 LAN/公网暴露；
- 能访问 loopback 的本机其他进程可能访问 WebUI，用户必须依赖 OS 账户、进程隔离和本机环境保护秘密。

任何非 loopback 部署都是架构变更，必须新增认证、CSRF、TLS、威胁模型、审计和项目所有者书面批准；不能只改变 `--host` 或隐藏环境变量。MVP host 校验必须拒绝 `0.0.0.0`、`::`、局域网和公网地址。

MCP stdout 只能是 JSON-RPC/MCP 消息，日志、诊断和堆栈只能到 stderr，不得写本地日志文件。MCP 查询不能写数据库、文件、cache 或 `current.json`，详细工具边界见 [`mcp-api.md`](mcp-api.md)。

### 3.1 导出目录 chooser 与 import snapshot

WebUI 的目录选择范围严格限定为 `<data-root>/exports/<minecraft_version>`；chooser 只能传递进程本地、高熵 opaque ref，绝不把绝对路径放入 token、response、log、cache、SQLite 或 URL。消费 ref 时必须重新验证 root、精确版本、目录 identity 和每个 path component，拒绝 traversal、symlink、Windows junction/reparse point、意外 mount crossing、snapshot hardlink、以及 chooser 后的 stale replacement。source `Path` 只允许存在于当前 in-memory closure。

`cache/import-checks/{check_id}/state.json` 是 import check 的唯一 authoritative state；snapshot 仍位于 `cache/import-checks/{check_id}/snapshot/{export_id}/`，其它 check-owned metadata 不能作为列表索引。state 以 atomic replace 保存最小非秘密阶段、状态、anchor hash、时间、validator subphase/progress 和 workspace association；绝对 source path、chooser ref/token、API key 和其它秘密不得持久化。首页、目录 listing 和 Recent Checks 必须按需扫描严格匹配 `^check_[0-9a-f]{32}$` 的 check directories/state files，拒绝 symlink、junction、reparse point、hardlink 和未知路径 entry，并只展示经过 sanitization 的 summary。禁止 `index.json`、`owner_instance_id`、额外数据库表、generic migration、依赖、服务或任意生成框架。

通过 check 的再次使用只比较 canonical source entry 当前 raw `manifest.json` 与 `checksums.sha256` 的 SHA-256 declared anchors；这只是轻量变化提示，不是 live artifact 全量未变的安全证明。导入永远从 immutable checked snapshot 读取，完整性依据是 snapshot 与一次 validator pass；source chooser ref 失效、anchor 改变或 check failed/interrupted 时必须重新选择/新建 check。source snapshot 阶段发生进程重启时不得续跑，必须报告 `IMPORT_CHECK_INTERRUPTED`。该增强沿用 loopback、SQLite、本地文件和进程内 Worker，不改变 MCP 的 stdio/只读边界或引入新服务。

## 4. 秘密管理

### 4.1 Keyring/env 优先级

API key 读取顺序固定为：

```text
OS Keyring(service=blockpedia, account=<profile_id>)
  > environment variable OPENAI_API_KEY
  > none
```

Keyring 优先于环境变量；环境变量只读，不能写回 profile/config。SQLite 只能保存不可逆或不可还原的 `secret_reference`：

```json
{"secret_reference": "keyring:blockpedia/default"}
```

允许 `env:OPENAI_API_KEY` 作为引用，但禁止保存环境变量实际值。API key **MUST NOT** 出现在启动日志、异常、HTTP response、Jinja 页面、浏览器 localStorage、SQLite、task snapshot、cache、prompt、图片 metadata、导出包、release、截图、测试报告或 provider 诊断。

前端只能得到：

```json
{"configured": true, "source": "keyring|environment|none", "masked": "••••••••abcd"}
```

不能安全掩码时固定返回 `configured=true`，不返回 suffix。删除 key 只能删除 Keyring 条目/撤销引用，不留明文备份。

### 4.2 OpenAI 协议和数据保留

MVP 只实现 [`openai-provider.md`](openai-provider.md) 的 protocol-neutral `OpenAIProvider`，profile 必须显式选择 `openai_responses` 或 `openai_chat_completions`。三阶段各自的两种请求形状都必须使用 `annotation-batch-output.v1`/`annotation_batch_output_v1`、`query-spec-output.v1`/`query_spec_output_v1`、`rerank-output.v1`/`rerank_output_v1` 的 strict wire Schema/name；Responses 发送 `store=false`，Chat 省略 `store`，不得自动协议切换或自由文本 fallback。两种协议都不能证明远端 retention 或实际执行的模型身份；成功响应的 string `model` 只是 untrusted echo，不能替换 configured/requested `model_id`。第三方服务策略、路由和模型身份由用户负责。MVP 完全不记录、不展示、不计算 Token usage，不估价、不预算、不生成成本页面。

系统只保留脱敏 provider `request_id`、错误码/分类、内部 operation ID、输入 hash、validated artifact hash 和稳定耗时 bucket；不得保留完整 request/response、usage、图片、Authorization 或 key。

## 5. 最小披露和提示注入防护

发送给选定 OpenAI adapter 的最小集合只能是：

1. 当前任务所需的裁剪 PNG/联系表；
2. 已验证的公开 Minecraft metadata 和确定性机器事实；
3. 内部短编号（`variant_id`、`candidate_id`）及当前用户 query；
4. 当前阶段所需 Schema 和 bounded semantic rules 摘要。

不得发送本机绝对路径、文件名中的秘密、API key、Authorization、SQLite、日志、完整导出包、整库数据、无关方块、无关用户查询、字体/纹理/模型源文件或未审核人工秘密。AI cache key、adapter、模型版本和 base URL stable ID 不能成为披露本机路径/秘密的通道。

`prompt.v1` 与其它历史 prompt version string 保持 exact legacy behavior；`prompt.v2` 只能由新的 run/profile snapshot 选择。v2 model-visible text 保留 contact sheet/tile labels，trusted instruction 只要求 annotate existing tiles、复制 exact existing `variant_id`、不创建或修改 ID/machine facts；`tiles` 只含 `tile_id`/`variant_id`，per-tile metadata 只含 `tile_id`/去重有界 `geometry_classes`。v2 不发送 image/machine hashes、`block_id`、`canonical_state_id`、exact dimensions/volume、behavior booleans/emission、`machine_tags`、feature metrics/version/input hash 或重复 feature geometry/tags；完整 machine metadata、hash、source image 和 lineage 仍留在本地。

用户 query、family、context、人工 note、历史 annotation 和 provider 返回文本均视为不可信数据：

- 放在独立数据区（例如 `<untrusted_user_query>`），不能与系统指令混写；
- 不得改变 Schema、model、候选集合、release selector、hard constraints、工具名或安全规则；
- 输出必须经 strict Schema、bounded semantic fields、ID 集合、来源和机器冲突校验；
- LLM 不得创建/改写 `block_id`、合法状态、状态字符串、几何、collision、透明度、发光、支撑、行为、发布状态或资格事实；
- visual rerank 只能排列本地已召回候选，不能增候选、删 hard constraint 或让新 ID 成为事实。

WebUI 每次发送前必须展示待发送的文本、图片和 machine metadata 预览，预览只能显示短 ID，不显示路径、key、Authorization、完整 SQLite 或无关数据。用户取消后不能产生 provider request。发送前预览不是完整 request/response 日志。

### 5.1 批次授权和 run lineage

手动 per-batch approval 是默认。自动 sequential batch submission 只有一次明确 WebUI confirmation 才能启用，而且 confirmation 只能绑定仍可 inspect 的 unchanged frozen remaining plan、D-040/D-041 的 immutable plan hash、run-frozen provider 和 requested `model_id`；它不是永久授权、auto-mode 配置或新的持久 state。plan hash 只哈希 `run_id`、`effective_config_hash` 和 ordered `job_id`/`logical_key`/recomputed payload signature。aggregate confirmation 只读取并验证已经持久化的 `jobs.input_signature`、cursor `payload_signature`/`input_hash`、`tile_ids`/`variant_ids`、effective config 和 frozen provider snapshot，不为全部 pending jobs 重建图片、contact sheet、prompt 或 machine metadata；`recomputed_payload_signature` 在 plan time 表示 validated persisted payload signature。确认 transaction 遇到任何 persisted hash、TOCTOU 或 lineage mismatch 必须 approve none；成功时 one plan audit 和 per-job approval audits 都必须存在。

每个 batch 仍可通过既有 safe preview lazy inspect，且 one-batch preview 可以重建 bounded payload。Immediately before every actual external send，Worker 必须从 run snapshot 使用 frozen adapter、model 和 base URL，重建完整 one-batch payload/contact sheet/prompt/machine metadata，重算 full signature 并比较 approved job signature；任一 mismatch 必须 revoke approval、在任何 HTTP request 前 pause，且不得发送。可变 global active profile 不能替换已有 run。发送 concurrency 固定为 `1`，item-local failure 不能授权跳过后续审计或扩大计划，fatal failure 必须在同一 transaction 保存 request evidence/review/job/stage/run failure/audit 后阻止 later sends。原始 request evidence、provider request reference 和 retry source rows 不得被 bulk retry 覆盖；retry child 必须保留 `retry_of_job_id` lineage。WebUI 只显示脱敏 evidence，不能把计划 hash、requested model 或 audit 当作远端模型身份或 retention 证明。

startup stale detection 仍是 read-only；auto-approved cursors 可以持久保存，只有显式 WebUI `recover` 改变 stale state。pause/cancel 只停止 future sends，SSE 或 browser disconnect 不构成取消，也不能触发隐式 retry。Provider retry wave 仅面向 eligible failed leaf AI jobs；succeeded、无 Provider error 的 low-confidence review、fatal job 和 `PROVIDER_CANCELLED` 都不能加入 wave。

## 6. 日志、错误和零遥测

默认零外发遥测。结构化本地日志可以记录：`timestamp`、`level`、`component`、`event_code`、内部 operation/run/job ID、stage、稳定耗时 bucket、错误码、脱敏 provider request ID 和 artifact hash。日志禁止记录：

- API key、Authorization、Cookie、环境变量实际值；
- 图片 base64、PNG 内容、完整 prompt、完整 provider request/response；
- 本机绝对路径、用户名、数据库 SQL 全文、无关用户数据；
- Token usage、费用、预算、价格估算。

路径必须替换为稳定 hash 或相对 artifact ID；用户 query 仅在本地任务必要时最小保存，不自动外发。前端错误使用稳定 `error_code` 和可操作消息，详细诊断写本地脱敏日志；不得回显 key、路径或 provider body。WebUI/Worker 日志在 data root `logs/`，默认本地滚动；MCP 是例外，只写 stderr，绝不写 data-root logs、cache 或临时文件。无远程日志服务；用户主动导出诊断包时必须再次脱敏并列出字段清单。

D-042 的 final `offline_annotation` validation diagnostic 只有在总 retry budget 用尽后仍失败时才可写入既有 `PROVIDER_FAILURE` review task `evidence_json`，并通过 internal `ProviderResult` 携带。它只能包含 `stage`、`phase`、`path`、`keyword`、`observed_type`、`observed_length` 六个 allowlisted fields；`observed_type` 只允许 JSON type 或 `missing`，`observed_length` 只允许有界非负整数或 `null`。不得保存 raw output/value/prefix、provider message、exception、repair context、prompt/image/secret、response/value hash 或 path-like value。successful repair 不落诊断；Review/API/UI 只用 ordinary labels 展示 allowlisted fields，不能从 `path` 派生或渲染 raw value。provider envelope、provider_requests、Schema、SQL 和 release validation 边界不改变。

SSE snapshot 同样属于前端响应，必须使用与普通 API 相同的路径/秘密脱敏规则。run 只允许输出 item aggregate、current/recent/latest allowlisted audit projection、heartbeat 和稳定错误码；import check 允许输出 `state.json` 的完整脱敏快照（包括宏观 phase、validator subphase/progress 和 workspace association），但不逐 item 广播。两者都不得输出 raw `details_json`、cursor、worker ID、exception、绝对路径、chooser token、secret 或可 replay 的事件 ID。断开连接只能结束响应，不能取消或改变后台工作；heartbeat、stale 展示和 summary 扫描不得产生写入。

## 7. Release、current 和回滚保护

### 7.0 R3 Phase C 文件与原子提交安全边界（security owner）

Phase C 的 check/build 对 `<data-root>/workspace/`、`cache/release-checks/`、release staging 和 release final 的每个 path component 都必须使用不跟随链接的 `lstat`/等价句柄检查。普通文件和目录一律拒绝 symlink、Windows `FILE_ATTRIBUTE_REPARSE_POINT`、junction、mount/reparse crossing、hardlink（普通文件 `st_nlink != 1`）和检查后被替换的 stale identity；目录枚举结果不能替代消费时的再次检查。任何失败都返回稳定错误，不得继续 hash、复制或 rename。

workspace `work.sqlite3` 只能以只读连接读取逻辑 rows；WAL 模式下不得 checkpoint、truncate、改变 journal mode、写 `-wal`/`-shm` 或依据 SQLite/WAL 文件 bytes 计算 fingerprint。读取连接可以看见 WAL 中已提交的逻辑事务，但 check/build 不得让读取行为修改它。release `index.sqlite3` 必须以 `journal_mode=DELETE` 和 `synchronous=FULL` 创建并验证，完成后 release 目录不得留下 `-wal`、`-shm` 或其它 SQLite sidecar；sidecar 出现即阻断。

所有 check state、报告、release index、JSON、preview 和 checksum 文件都必须在提交前完成写入、flush 和文件 `fsync`；创建/rename 涉及的父目录也必须执行目录 `fsync`（Windows 使用同等 durable directory/rename 语义）。Windows release commit 必须在同一 volume 使用 `MoveFileExW` 的 write-through 语义，不能使用 `MOVEFILE_REPLACE_EXISTING`；final target 必须预先不存在，发生竞争时移动失败而不是覆盖目标。不得先删除目标再移动，不得跨 volume rename，不得把缓存 state 的原子更新误当成 release 覆盖许可。

一次 build 失败只允许清理本次操作创建且已按 identity 验证的精确 `.rel_<32hex>.staging` 目录；不得按 glob 删除其它 staging、历史 release、workspace、cache 或 audit。rename 成功后的 release 永远不因后续 audit/state 错误而删除或回写，应用层必须把它视为不可变候选并报告可恢复失败。

最终 release 在应用层以 `immutable=true` 拒绝写入，并在最终完整 hash 验证后将普通文件和目录设置为只读权限/ACL；权限设置不得产生 release 内 marker 或内容改写。rename 后只允许更新 release 外的 check state、cache 和 workspace audit/status；不得修改 release 内任何文件、目录内容、SQLite 数据、权限语义或 hash 文件。Phase C 不执行 activation/current/MCP。

### 7.1 不可变 release

发布目录固定为：

```text
<data-root>/releases/<minecraft_version>/<release_id>/
  release.json
  manifest.json
  index.sqlite3
  previews/
  quality_report.json
  manual-overrides.json
  schemas.sha256
  checksums.sha256
```

生成并写入完整 hash manifest 后，release **MUST NOT** 原地修改。candidate release 使用 `built_at`；激活时间只写 `current.json` 和 workspace activation audit，不写回 release metadata。configured/requested model_id、prompt、Schema、semantic constraints、图片、人工覆盖或任何语义变化都必须产生新的 build/release；不能改旧 SQLite、图片、override 或 quality report。response model echo mismatch 不产生远端身份证明，也不触发旧 release rewrite。每个精确 Minecraft version 首发前至少有两个独立、完整性通过、带 hash 的不可变 release。

release manifest 必须记录精确 `minecraft_version`、`release_id`、来源 export、工具链 lock hash、Schema/prompt/search 版本、provider `adapter`、`profile_id`、requested `model_id`、`base_url_stable_id`（非秘密）、`secret_reference`、AI artifact/cache hash、覆盖审计和 quality report hash；manifest 记录的是 requested identity，不是第三方 response model echo，也不声称远端实际执行身份。manifest 只哈希功能输入/产物，`checksums.sha256` 另行覆盖 release 内其它普通文件。`adapter` 是 protocol lineage 字段，允许 `openai_responses` 或 `openai_chat_completions`；MCP 必须按它使用冻结 codec，不得读取 active profile 或跨协议 fallback。JSON、Schema 字段、metadata、manifest、current 和 release 中的 hash 字符串必须使用 `sha256:<64 lowercase hex>`；`checksums.sha256`/`schemas.sha256` 的行首 digest 不带前缀。`checksums.sha256` 格式为 `<64hex><two ASCII spaces><release-relative-posix-path>\n`，排除自身并按路径排序；`schemas.sha256` 格式为 `<64hex><two ASCII spaces><schema-id><two ASCII spaces><canonical-repository-relative-posix-path>\n`，按 schema ID UTF-8 bytes 排序，路径不声称位于 release。AI 产物冻结字段见 [`openai-provider.md`](openai-provider.md)。

### 7.2 `current.json` 唯一指针

`current.json` 是唯一当前 release 指针，必须使用严格 `current-pointer.v1`。多版本结构可为：

```json
{
  "schema_version": "current-pointer.v1",
  "default_minecraft_version": "26.2",
  "versions": {
    "26.2": {
      "release_id": "rel_01J",
      "minecraft_version": "26.2",
      "relative_path": "releases/26.2/rel_01J",
      "manifest_sha256": "sha256:<64 lowercase hex>"
    }
  },
  "updated_at": "2026-08-13T12:03:00Z"
}
```

写入流程必须是：

1. WebUI publish/rollback service 验证目标 release 存在、不可变、完整性通过、精确版本匹配且 manifest hash 正确；
2. 在同一 data root 写完整临时 pointer；
3. flush 文件并按平台执行必要 durable flush；
4. 原子替换 `current.json`，不得先删除旧指针或跨文件系统 rename；
5. 重新读取并验证 pointer、release hash、quality report 和 checksums。

只有 WebUI 的 publish/rollback 能改变 current；MCP、搜索测试、Worker 普通任务和人工页面不能直接写。写入中断、flush/replace 失败或进程崩溃时必须保留旧有效 current 或可恢复的完整临时文件，绝不能出现半个 JSON。回滚只切换到已有完整 release，不改目录内容、不删除审计证据。cleanup 可以人工删除未受保护旧 release，但每个精确版本必须保留至少两个成功 release；`current`、pinned、active-reader 和保底两个不得删除。

## 8. 数据分层和人工审计

索引永远分三层：

1. **不可变机器事实**：runtime registry、legal state、state string、geometry、collision、transparency、emission、support、render hash 等，WebUI 只读；
2. **AI 语义建议**：strict Schema 内的 synonym、颜色/形状词、材质观感、用途、风格和候选理由，保存 requested model/prompt/schema/base URL stable ID、输入/产物 hash；不把 response model echo 作为 verified identity；
3. **人工覆盖**：独立声明式记录，含 operator、time、reason、source version、target ID，可按固定顺序 replay。

资格只能是 `eligible`、`conditional`、`excluded`；`conditional` 必须带 warning。skip 审计使用 `skip-review.v1`，qualification 审计使用 `qualification-review.v1`；二者都必须包含 `target_id`、`minecraft_version`、`reviewer`、`reviewed_at`、`reason_code`、`note`、`evidence`、`source_version`，skip 另含 `machine_failure_ref`。人工可以编辑 AI 语义、qualification 和 skip，但不能把值写回机器层；发现机器事实错误必须修复 exporter/重新导出，不能 override 隐藏。

## 9. 依赖、运行和变更控制

- 支持平台仅 Windows 11 x86_64/Linux x86_64（Linux `manylinux_2_17` / glibc `>=2.17`）；Python 基线为 CPython `3.14.7`；R0 只锁定实际引入的 tooling 依赖，后续依赖使用前必须精确/hash 锁定，Windows 在对应阶段验证，Linux wheel/ABI、安装/实际运行和最终双平台复现统一由 R5 验证；
- Minecraft/Java/Fabric/Gradle/Loom/mappings 使用 [`AGENTS.md`](../AGENTS.md) 固定基线；精确字段形状由真实 Schema 文件拥有；
- 默认数据根在源码外；真实数据和合法运行时生成本地保存；
- 不制作安装包、容器、系统服务、自动更新或额外服务；
- 不使用通用 SQLite migration framework；schema 变化先更新高优先级契约并重建数据库；
- 任何等价技术替换必须先在 [`decisions.md`](decisions.md) 写影响，证明不破坏单机、复现、无额外服务、MCP 只读和数据契约，并取得项目所有者书面批准；
- Provider 适配器范围、MCP transport/tool 集合、非 loopback、秘密来源、公开资产范围和 current 语义都是冻结边界，不能以 debug/compatibility/隐藏环境变量绕过。

## 10. 安全和分发验收

发布前必须有可复核证据：

1. 扫描源码、测试、Release 和镜像，确认无 JAR、资源包、纹理、模型、字体、声音、截图、真实导出和秘密；原创 fixture 清单可核验；
2. host 测试拒绝所有非 loopback，WebUI 默认 `127.0.0.1:8765`，MCP stdout 纯净；
3. Keyring 优先、环境只读回退、SQLite 仅 `secret_reference`、前端 mask、日志脱敏测试通过；
4. 三阶段 × 两种 adapter 的六类请求使用独立 strict wire Schema；Responses 发送 `store=false` 但不要求 store echo，Chat 省略 `store`；无 Chat/Responses fallback、Anthropic 或第二 model 路径，也不声称远端 retention 已验证；
5. provider 最小披露、发送前预览、untrusted data 隔离、候选边界和机器事实保护测试通过；
6. current 原子替换故障注入、release immutable、rollback 只切 pointer、cleanup 不删审计测试通过；
7. 公开包只含代码、文档、Schema、空库和 fixture 生成器源码；真实图片/索引只存在用户本地 data root；
8. 每个目标 Minecraft version 在首发检查中有至少两个独立完整 release，且发布门记录 `TWO_INDEPENDENT_RELEASES`；candidate-build gate 的 `excluded` qualification 审计已通过，activation gate 只复核其报告和 hash。

详细测试命令和报告要求见 [`quality-and-testing.md`](quality-and-testing.md)；R1–R5 项只在对应实现和最小验收证据存在后标记完成，不提前建设额外证据层。
