# WebUI 与本地运行契约

## 文档状态与导航

本文定义 Blockpedia 本地 `Index Studio`、进程内任务、所有写操作入口、配置/秘密边界、稳定路由和两个允许的 Python 命令。正文使用简体中文；路由、字段、状态、错误码、Schema 和命令保持英文。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 为规范性关键字。

本文服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md)、[`decisions.md`](decisions.md)、[`product-scope.md`](product-scope.md) 和 [`architecture.md`](architecture.md)。原始设计稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。数据字段/来源见 [`data-and-schemas.md`](data-and-schemas.md)，导出与发布流水线见 [`export-contract.md`](export-contract.md)、[`state-policy-and-rendering.md`](state-policy-and-rendering.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)。

相关组件契约：

- [`openai-provider.md`](openai-provider.md)：protocol-neutral `OpenAIProvider`、显式 adapter、能力探测和严格重试；
- [`search-and-ranking.md`](search-and-ranking.md)：搜索测试台、`QuerySpec`、硬过滤和降级；
- [`mcp-api.md`](mcp-api.md)：MCP 只读 release 与四工具协议；
- [`quality-and-testing.md`](quality-and-testing.md)：服务层/路由层验收和发布门；
- [`security-and-distribution.md`](security-and-distribution.md)：loopback、秘密、日志和分发边界。

## 1. 角色、分层和安全边界

WebUI 是本地单用户索引工作台，不是玩家账户系统、远程管理面板、LAN 服务、多租户服务或团队审批系统。默认架构 **MUST** 是 Python + FastAPI + Jinja2 + HTMX + SQLite + 本地文件 + 进程内有限 Worker。除非先更新 [`decisions.md`](decisions.md) 并取得项目所有者书面批准，**MUST NOT** 引入 Redis、Celery、Kafka、微服务、对象存储、向量数据库、独立搜索服务或其他常驻服务。

内部依赖方向必须是：

```text
HTTP/Jinja/HTMX routes
  → application services/use cases
  → repositories + OpenAIProvider + file/release store
  → SQLite/local files
```

路由层只能做 HTTP/JSON 解析、模板和错误映射；导入、run、审核、发布、回滚和 cleanup 规则 **MUST** 在可直接由 service-level tests 调用的 application service 中。应用服务与路由必须解耦，不能通过全局 HTTP client、模板状态或进程变量隐藏业务规则。MCP 只能读 release，不能复用会写工作库/文件的 WebUI use case。

### 1.1 loopback-only

`block-index web` **MUST** 且只能绑定 `127.0.0.1:8765`。配置必须拒绝 `0.0.0.0`、`::`、`[::1]`、局域网地址、公网地址和任意非 `127.0.0.1` host；不得提供 IPv6 监听开关。该约束遵循更高优先级的 [`../AGENTS.md`](../AGENTS.md) 与 [`decisions.md`](decisions.md)。

MVP **MUST NOT** 添加账号系统、CORS 或 CSRF 机制；loopback + 本机操作系统访问控制是安全边界，不应描述成一般网络服务防护。非 loopback 部署不是可配置选项，而是受控架构变更，必须新增认证、CSRF、TLS、威胁模型、审计和项目所有者书面批准；本 MVP 不实现。

## 2. Python 命令边界

Python 产品命令严格只有：

```text
block-index web [--data-root <path>]
block-index mcp [--data-root <path>]
```

不得交付 `block-index import`、`block-index resume`、`block-index review`、`block-index publish`、`block-index rollback`、`block-index cleanup` 或其他产品子命令。导入、恢复、审核、发布、回滚和清理 **MUST** 从 WebUI service 发起并记录审计。测试框架命令不是产品 CLI，但不得替代这些 WebUI 写入口。`block-index mcp` 只启动 stdio MCP，不监听 HTTP；详细协议见 [`mcp-api.md`](mcp-api.md)。

## 3. 数据根和配置

### 3.1 默认数据根

默认数据根必须在源码目录之外：

- Windows 11：`%LOCALAPPDATA%\Blockpedia\data`；
- Linux x86_64：`$XDG_DATA_HOME/blockpedia`，未设置时 `$HOME/.local/share/blockpedia`。

`--data-root` 的优先级高于 `BLOCKPEDIA_DATA_ROOT`。host/port 永远不接受 CLI、环境变量或 profile 配置，固定为 `127.0.0.1:8765`。数据根至少包含：

```text
<data-root>/
  exports/<minecraft_version>/<export_id>/
  workspace/<minecraft_version>/<run_id>/
  cache/
    provider-profiles.json
  releases/<minecraft_version>/<release_id>/
  current.json
  logs/
```

真实导出、图片、工作库、人工覆盖、缓存和 release 只能在用户本地数据根，不能进入源码或公开分发。数据目录字段必须使用跨平台路径 API；响应和日志不得暴露本机绝对路径。

### 3.2 配置优先级

非秘密、非冻结运行配置解析顺序固定为：

```text
CLI startup arguments > environment variables > profile/project configuration > built-in defaults
```

来源冲突或类型不兼容必须返回 `CONFIG_PRECEDENCE_CONFLICT`，不得静默合并。CLI 只能覆盖 `data-root` 等非冻结启动项；host/port 不属于配置，永远固定为 `127.0.0.1:8765`。环境变量包括 `BLOCKPEDIA_DATA_ROOT` 和 `OPENAI_API_KEY`（后者只作为秘密回退）；profile/project 配置包含 `model_id`、非秘密 `base_url`、版本、超时、并发、Schema/prompt 和搜索版本；精确字段形状由真实 Schema 文件拥有，内置默认只提供冻结值。

最终生效配置必须计算 `effective_config_hash` 并写入 run/job snapshot。snapshot 可以包含 configured/requested `model_id`、`adapter`、`profile_id`、`base_url_stable_id`、`minecraft_version`、`resolved_release_id`、`manifest_sha256`、`offline_annotation` 的冻结调度并发、超时、Schema/prompt/search 版本、重试策略和非秘密路径稳定标识；该调度并发只属于 run/runtime scheduling，**MUST NOT** 进入 release snapshot、`release-manifest.v1` 或 provider wire/record Schema。**MUST NOT** 包含 API key、Authorization、图片 bytes、完整 provider response、response model echo、Token usage、成本或预算。

全局非秘密 profiles 的唯一持久来源是 `<data-root>/cache/provider-profiles.json`。WebUI 必须以临时文件、flush/fsync 和原子替换保存该文件；文件不得包含 API key 或其它秘密。workspace 的 `provider_profiles` 仅作为 run snapshot，不能作为全局配置库；该收敛不新增服务、migration 或 Schema ID。

### 3.3 秘密

API key 必须优先从 OS Keyring 读取：`service=blockpedia`、`account=<profile_id>`；Keyring 没有值时才只读读取 `OPENAI_API_KEY`。SQLite 只能保存不可逆/不可还原的 `secret_reference`，如 `keyring:blockpedia/default` 或 `env:OPENAI_API_KEY`。前端只能收到：

```json
{"configured": true, "source": "keyring|environment|none", "masked": "••••••••abcd"}
```

无法安全掩码时只返回 `configured=true`。key 和 Authorization **MUST NOT** 出现在 profile/project 文件、SQLite、任务 snapshot、日志、异常、截图、prompt、图片 metadata、导出包、release、HTTP response 或浏览器存储。

### 3.4 SQLite schema 和写事务

MVP **MUST NOT** 使用通用 SQLite migration framework。schema 在 R0 冻结；结构变化必须先按 [`AGENTS.md`](../AGENTS.md) 和 [`decisions.md`](decisions.md) 更新契约、重建数据库并重跑完整性门。启动时 schema hash 不匹配必须停止写操作，返回 `DATABASE_SCHEMA_MISMATCH`。启动恢复流程只能读取并展示 stale `running` job；不得自动把它写回 `pending`、`failed` 或其他状态。只有用户显式 POST `/api/runs/{run_id}/recover` 后，服务才能在事务中改变超时且未完成的 job 状态；成功 job 永不重跑。

所有 WebUI 写操作必须在 SQLite transaction 中同时写状态和审计记录；写文件必须先校验版本、目标 ID、release 和输入，再采用临时文件、flush/fsync 和原子替换。失败时回滚数据库，不留下“成功”状态。SQLite 不能存图片 BLOB，只能存安全的相对 artifact ref、尺寸和 hash。

### 3.5 导出目录 chooser 与来源引用

目录 chooser 的可见根严格是 `<data-root>/exports/<minecraft_version>`。`GET /api/directories?minecraft_version=26.2&parent_ref=<optional opaque ref>` 只能列出该精确版本根下的目录；省略 `parent_ref` 表示该根。chooser ref 必须是进程内生成的高熵 opaque ref，只在当前进程有效。任何 token、response、log、cache、SQLite、HTML 或 URL 都不得包含本机绝对路径；ref 不能反解为路径。

服务在列出目录和每次消费 ref（包括创建 check、读取 snapshot 和 import）时，必须重新验证 data root、精确版本、目录身份以及路径的每个 component。必须拒绝 traversal、`.`/`..`、symlink、Windows junction/reparse point、意外 mount crossing、snapshot 中的 hardlink 和 chooser 后的 stale replacement；不能仅依赖列目录时的结果或字符串规范化。ref 失效时要求重新选择并返回稳定的 path/ref 错误码。消费闭包之外不得保留 source `Path`。

目录列表和首页的导入检查摘要必须按需安全扫描 `<data-root>/cache/import-checks/` 下严格匹配 `^check_[0-9a-f]{32}$` 的 check 目录及其 `state.json`。扫描必须拒绝 symlink、junction、reparse point 和其它链接/reparse entry，只读取允许的 `state.json`，并对导出 ID、状态、时间和错误摘要做 allowlist/sanitization；损坏、越界或未知文件只作为不可操作摘要，不得成为导航或路径输入。按 `(minecraft_version, export_id)` 选择最新 actionable check 时以 `created_at`/`updated_at` 的稳定排序和持久 `state.json` 为准。该扫描是派生视图，**MUST NOT** 写入第二个持久索引（包括 `index.json`）、数据库表或 cache marker。

## 4. WebUI 页面和功能

页面必须覆盖下表；不得创建 Token/成本/预算页面或字段：

| 区域 | 必须能力 | 明确禁止 |
|---|---|---|
| `Provider` | 多个非活动 profile、唯一 active profile、显式 `openai_responses`/`openai_chat_completions` selector、Keyring/env 状态、协议感知能力探测、enable/disable、待发送预览、requested model warning | 协议自动切换、Anthropic、第二 model、usage/成本、retention/远端模型身份验证或绕过字段 |
| `Import` | 版本选择、导出包 check、导入到 workspace、逐项错误 | 直接覆盖 release、CLI 导入 |
| `Pipeline` | run/pause/resume/status、heartbeat、失败恢复、手动逐批默认、计划确认后的有界顺序提交、单项/批量 Provider retry | 无限自动重试、无审计后台写入、越过 approved barrier 或超过冻结/全局上限的并发发送 |
| `Review` | normal/high 队列、机器事实只读、语义编辑、声明式 override、skip | 修改机器事实、无原因 skip、静默发布 |
| `Release` | check/build/activation-check/apply/rollback/cleanup、hash、审计 | 原地改 release、删除回滚证据 |
| `Search test` | QuerySpec、hard filter、Top-24、`context.family` 的 R4 no-op/校验、联系表、降级结果 | 持久化测试 query、隐藏 warning、放宽 hard 或展示不存在的 family metadata |
| `Settings/Logs` | data root、日志级别/保留、脱敏日志 | 远程遥测、秘密查看、Token/成本仪表盘 |

Provider 页面可以保存多个非活动 profile，但全局最多一个 active profile；每个 profile 必须显式选择 `openai_responses` 或 `openai_chat_completions`，并显示所选 endpoint、请求字段和隐私边界。页面必须显示简短警告：`model_id` 是发送的 requested identity，第三方 gateway 可能改写 response model；Blockpedia 不验证或声称远端实际模型身份。active 只控制 Studio 新写任务和新 release；既有 Responses profile/release 继续有效。release-bound MCP 使用已解析 release 冻结的 provider snapshot，不读取或比较可变 active profile。每个 profile 在 enable 前必须通过所选 adapter 的图片、strict Structured Outputs、错误分类、requested model/auth 和协议 wire 形状能力门；Responses 发送 `store=false`、Chat 省略 `store`，两者都不证明远端 retention。任一能力失败都禁止 enable；不得提供协议 fallback、retention/模型身份确认或绕过字段。具体字段见 [`openai-provider.md`](openai-provider.md)。

页面初始 HTML 必须由服务端渲染。状态 hero 的可滚动区域必须展示完整的 11-stage timeline（顺序仍为 `PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE`）。下方使用统一 live work 区域展示 heartbeat、item aggregates、current step、recent steps 和 latest allowlisted audit projection。浏览器刷新后以持久化的 `check_id`/run snapshot 恢复；不要求 `localStorage`。EventSource 是 live DOM 更新的权威来源，HTMX/普通页面刷新不得与其维护第二套状态。

首页必须始终显示 `Recent Checks` 入口，即使没有可用条目；首页扫描结果最多显示 5 个不同 `(minecraft_version, export_id)`，active check 优先，其余按最近 actionable 状态/时间排序。导出目录 chooser 的每个安全目录条目可显示 checked/passed/imported/changed 等 marker，但 marker 只是 state 扫描的派生展示，不是新的身份或索引。进入首页、目录列表或 canonical check 页面都不得把绝对路径或 chooser ref 回显到 HTML、URL 或脚本。

## 5. HTTP route 契约

页面 GET 可以返回 HTML；`/api/` JSON 必须 UTF-8、拒绝未知字段并使用稳定 `error_code`。通用成功/失败 envelope：

```json
{
  "ok": true,
  "request_id": "web_01J",
  "data": {},
  "warnings": []
}
```

```json
{
  "ok": false,
  "request_id": "web_01J",
  "error_code": "INVALID_INPUT",
  "message": "字段不合法。",
  "field_errors": {},
  "retryable": false
}
```

`request_id` 是本地诊断 ID；provider ID 只能以脱敏字段出现。HTTP status 映射固定为 `400` 输入、`404` 不存在、`409` 状态冲突、`422` Schema/完整性、`503` Worker/provider 暂不可用、`500` 未分类错误。页面必须显示稳定错误码和修复动作，不能只显示“失败”。

### 5.1 Provider routes

`GET /api/provider/profile` 返回无秘密 `ProviderProfile`。

`PUT /api/provider/profile` 的精确字段由真实 Schema 文件拥有；请求必须显式提供合法 `adapter`，且不包含任何存储确认、协议 fallback 或绕过字段。出现 `api_key`、未知 adapter、第二 model 或未知字段必须返回 `PROVIDER_CONFIG_INVALID`。保存状态为非活动 `unverified`，并原子更新 `<data-root>/cache/provider-profiles.json`；改变 adapter、base URL、model、secret ref、Schema、prompt 或排序版本必须使能力变为 `unverified` 并禁用该 profile 的新 AI job。只改变合法 `offline_annotation.concurrency`（`1..5`）时保留 `verified`/`enabled` 且不需要 reprobe；`query_spec`/`visual_rerank` concurrency 固定为 `1`。`draft` 只能作为配置编辑命令/事件，不得作为持久化 run、stage 或 item 状态。

`POST /api/provider/probe` 请求只接受 profile 标识，不提供任何存储确认或绕过字段：

```json
{"profile_id": "default"}
```

服务使用原创最小 PNG，并只按 profile 选定的 adapter 实际发送生产使用的三个 strict wire Schema/name 对（`annotation-batch-output.v1`/`annotation_batch_output_v1`、`query-spec-output.v1`/`query_spec_output_v1`、`rerank-output.v1`/`rerank_output_v1`），探测图片、Structured Outputs、稳定错误分类、requested model/auth 和协议 wire 形状。Responses 请求/probe 发送 `store=false`，不检查 store 或 response model echo equality；Chat 请求/probe 省略 `store`、检查请求没有该字段，也不检查 model echo equality。成功响应仍必须有 string `model`，缺失/非 string 必须 fail closed；不同 echo 不进入 verified identity。返回 `adapter`、`verified|failed`、能力布尔值、错误码、脱敏 request ID 和时间；不返回或声称 `storage_verified` 或 `remote_model_identity_verified`。任一所选 adapter 能力不能证明时必须是 `failed`，不得提供协议 fallback、确认或豁免路径。

`POST /api/provider/enable`/`disable` 只接受 `profile_id`。enable 只允许 capability status 为 `verified` 且所选 adapter 的协议感知能力门均通过；disable 停止新 AI job，不删除历史产物。Keyring/env 无法解析时不得 enable。

### 5.2 Import routes

`POST /api/imports/check` 请求：

```json
{"source_directory": "<opaque chooser ref>", "minecraft_version": "26.2"}
```

`source_directory` 现在只接受 opaque chooser ref。服务必须返回 HTTP `202` 和 `check_id`/`status=pending`，随后由同一进程内的 check 执行器启动一次检查。`cache/import-checks/{check_id}/state.json` 是唯一 authoritative check state；snapshot 仍写入 `cache/import-checks/{check_id}/snapshot/{export_id}/`，现有其它 check-owned metadata 不是列表索引。写 JSON 必须 atomic replace。state 的持久内容限于 check identity、版本/export、非秘密阶段/状态/计数、声明式 anchor hash、创建/更新时间、validator 进度和 workspace association；不得持久化 source 绝对路径或 chooser ref/token，source `Path` 只能存在于本次执行的 in-memory closure。

check 阶段固定为 `QUEUED → SNAPSHOT_EXPORT → VALIDATE_EXPORT → FINALIZE`；对外 progress 的宏观阶段是 `snapshot|validate|finalize`，check 状态只能是 `pending|running|passed|failed`。浏览器刷新通过 canonical `GET /imports/checks/{check_id}` 恢复页面，数据接口为 `GET /api/imports/checks/{check_id}`；摘要查询为 `GET /api/imports/checks?minecraft_version=&limit=`，只返回脱敏 summary，不返回路径或 token。若 server 在 source snapshot 期间重启，check 不得续跑，必须变为 `failed`、错误码 `IMPORT_CHECK_INTERRUPTED` 并要求重新选择。snapshot 完成后，passed check 保持可 import；`POST /api/imports` 不得重跑 validator。

同一 WebUI 进程使用一个 coordinator `RLock`，其协调键为 `(minecraft_version, export_id)`。不同 opaque chooser ref 解析到同一 export 时，不能排入第二个 active check；命中 exact active check 返回 `202`、相同 `check_id`、`reused=true`。已有 passed check 只有在 canonical source entry 的当前 raw `manifest.json` SHA-256 和 `checksums.sha256` SHA-256 都与 passed state 的 declared anchors 相同时才可复用，返回 `200`、相同 `check_id`、`reused=true`，且不得调用 validator。anchor 比较是轻量的 declared-anchor comparison，不证明每个 live artifact 未改变；完整性只来自 immutable checked snapshot 和该 check 的一次 validator pass。failed/interrupted check 或任一 anchor 改变时创建新的 `202` check。进程重启后 active check 必须收敛为 `IMPORT_CHECK_INTERRUPTED`，不得自动 resume。

validator 只允许由该 check 调用一次 `Validator.run`；只能接收 observational progress callback，不得因进度而增加扫描、读取、PNG decode 或 hash。callback 只挂接既有 inventory、Schema、JSONL、reference、render、checksum 循环；snapshot callback 只挂接既有 copy/hash loop。validator subphase 的 `completed` 在该 subphase 内单调递增；`total` 仅在已有确定总量时写入，否则保持 `null`/`0`，UI 使用带 live count 的 indeterminate bar。进度持久化应节流，phase transition 和 terminal state 必须强制写入；SSE 发送完整 snapshot，不逐 item 广播。既有 default/CLI report 的内容、调用次数和 PNG reads/decodes 必须保持不变；进度持久化失败本身必须收敛为稳定失败。`GET /api/imports/checks/{check_id}/events` 提供该 check 的 live snapshot。

`state.json` 仅增补 `created_at`、`updated_at`、validator subphase/progress，以及以下 workspace association；不改 workspace SQLite schema，也不新增 migration：

```text
workspace: {
  status: absent|creating|created|failed,
  import_id,
  run_id,
  error_code
}
```

对旧 state 缺少 association 的情况，服务可以按精确版本、`export_id` 和 manifest hash 扫描现有 workspace 数据库做兼容发现；发现只用于恢复关联，不生成第二索引，不改变数据库字节。

`POST /api/imports` 的严格请求只有 `check_id` 和 `copy_mode=copy_to_workspace`；**不接受 `project_id`，也不得把它加入代码或 Schema**。它按 passed check 幂等：第一次在同一 `(minecraft_version, export_id)` coordinator lock 内先预留 `import_id`/`run_id`、将 association 写为 `creating`，再从 immutable snapshot 构建现有 workspace。重复 `creating` 返回 `202` 和同一 `run_id`，不得创建第二 workspace；已验证 `created` 返回 `200` 和同一 `run_id`；首次完成创建可返回 `201`。导入只消费 snapshot，不重跑 validator，不从 source directory 复制到 release；UI 只有在最终 `work.sqlite3` 已验证后才能 deep-link。

若重启时 association 为 `creating`，服务先 reconcile 该 reservation 对应的最终 workspace；有效 `work.sqlite3` 可收敛为 `created`，否则保留原 `import_id`/`run_id` reservation 并返回稳定失败/可重试状态，重试复用 reservation，绝不静默分配第二个 run。`/ui/imports` 成功后使用 `HX-Redirect: /runs/{run_id}`。

UI 状态必须明确区分：`unchecked`；`checking`（动作 `View Progress`）；`passed_not_imported`（显示 checked marker，动作 `Import` 与 `Enter Run`）；`imported`（动作 `Enter Existing Run`）；`failed`/`interrupted`（必须用 fresh chooser ref `Retry`）；以及 anchor 不匹配的派生状态 `changed_since_check`（动作 `Run New Check`，不把旧 passed state 伪装成当前 source 未变）。

### 5.3 Pipeline routes

Studio 阶段固定为：

```text
PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS
        → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE
        → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE
```

item 状态固定为 `pending|running|succeeded|needs_review|failed|skipped`；run/stage 状态固定为 `pending|running|paused|needs_review|failed|succeeded|cancelled`。合法持久转换为 `pending→running`、`running→paused`、`paused→running`、`running→needs_review|failed|succeeded|cancelled`；pause/cancel 只作为命令或 event，不作为持久状态。release 的 build/apply 独立于 run 状态；recover 只能恢复 heartbeat 超时且未完成的 job；成功 job 不重跑。

`POST /api/runs` 请求至少包含：已预留 import 的 `import_id`、`minecraft_version`、`profile_id`、联系表大小和置信度阈值；不得接受或要求 `release_build_id`。服务必须在配置并启动该 import 时复用 import 已预留的同一 `run_id`/workspace，返回该 `run_id`、`status=pending`、`effective_config_hash` 和非秘密 snapshot，绝不分配第二 workspace/run。后续 `POST /api/releases/check` 以同一 `run_id` 创建并返回 `release_build_id`。

`GET /api/runs/{run_id}` 至少返回：`run_id`、精确版本、run status、stage、progress、heartbeat、非秘密 config snapshot 和 warnings。启动时 stale 只读展示，不改变这些状态；不得返回 Token usage、费用、图片 base64 或完整 provider response。

#### D-045 targeted banner export refresh

WebUI 只增加严格 `POST /api/runs/{run_id}/banner-export-refresh` 及对应 run page 的 HTMX action。HTMX action 必须提交同一严格 JSON body，不建立第二个业务 endpoint、队列或后台 refresh job：

```json
{
  "check_id": "check_<32 lowercase hex>",
  "expected_base_export_id": "export_YYYYMMDDTHHMMSSZ",
  "target_ids": ["<exact sorted 32 minecraft banner IDs>"],
  "confirm": true
}
```

`target_ids` 的值必须是精确 UTF-8 稳定排序结果：16 个颜色 `black, blue, brown, cyan, gray, green, light_blue, light_gray, lime, magenta, orange, pink, purple, red, white, yellow` 各生成 `minecraft:<color>_banner` 与 `minecraft:<color>_wall_banner`，不接受别的排序或集合。未知字段、缺字段、错误类型、重复/未排序/非精确 32 个 target ID 或 `confirm` 非 JSON `true` 必须拒绝。服务只消费 passed immutable full export check，并且只允许当前 run/stage 为 `HUMAN_REVIEW/needs_review`、没有 live AI future 或其它 live work、check source export 精确等于 `expected_base_export_id`、当前恰有 32 个 open `OBJECT_OFF_CANVAS` skipped targets。checked replacement package 的 normalized semantic diff 必须严格为 32 个 skipped→selected transitions 和 96 个 render files；其它 diff、版本、policy、manifest/checksum 或 source lineage mismatch 都 fail closed。

操作必须在既有 run lock 内调用 refresh service，使用 pipeline 契约定义的 workspace-local staging/journal/backup，并以原子 recover/commit 同步 complete source export、目标 render/feature files 和 SQLite projection。成功后只保留早期 succeeded stages，把 `AI_ANNOTATE`、`VALIDATE`、`HUMAN_REVIEW` reset 为 `pending`，并从 `AI_ANNOTATE` 继续；既有 1140 annotations、provider requests、jobs 和 reviews 不得被更新、删除或重排。只创建三个 unapproved target AI jobs，批次固定为 `12 + 12 + 8`，使用 `banner_refresh_*` keys。该 operation 不新增 table/column/status/migration/service/queue/CLI；已有 `request_reexport`/`request_exporter_rerender` 仍只是外部 exporter 请求，不得被当作成功 refresh。

#### 5.3.1 SSE live snapshots

run 和 import check 的事件接口分别为 `GET /api/runs/{run_id}/events` 与 `GET /api/imports/checks/{check_id}/events`。实现必须使用 FastAPI 原生 `StreamingResponse` 和浏览器原生 `EventSource`，不引入依赖、服务、消息总线或 SQLite/Schema migration；这是现有 FastAPI/Jinja2/HTMX/SQLite/进程内 Worker 栈上的增量能力。

响应必须使用 `Content-Type: text/event-stream`、`Cache-Control: no-cache, no-transform`，连接建立后立即发送完整 `event: snapshot`（并发送 `retry: 2000`）。不得提供 `id` 或 replay；重连重新发送当前完整 snapshot。每 15 秒发送 SSE comment heartbeat。可选的 `snapshot_error` 只能包含稳定、脱敏的错误码和 message。客户端断开不得取消、暂停或改变后台工作。

run snapshot 每次必须在一个一致的只读 SQLite transaction 中读取，随后关闭 transaction 后再 sleep；只发送发生变化的 snapshot，heartbeat 不改变或写入任何状态，也不能把 stale 检测写回数据库。snapshot 只能暴露 allowlisted 的 item aggregate 与 latest audit projection（包括当前/最近步骤的非秘密状态与稳定代码），不得暴露 raw `details_json`、`cursor`、`worker_id`、exception、绝对路径或 secret。

import-check SSE 只发送 `state.json` 的完整脱敏快照；它不宣布每个 validator item，也不把 `completed/total` 解释为跨 subphase 的全局百分比。未知总量保持 indeterminate，phase 和 terminal 快照必须可见；客户端断开、刷新或重连不改变 check/import 工作。

以下 routes 必须幂等或返回 `RUN_STATE_CONFLICT`：

```text
POST /api/runs/{run_id}/pause
POST /api/runs/{run_id}/resume
POST /api/runs/{run_id}/cancel
POST /api/runs/{run_id}/retry-failed
POST /api/runs/{run_id}/recover
```

Worker job 字段至少包括：`job_id`、`run_id`、`stage`、`logical_key`、`status`、`auto_attempt`、`priority`、`heartbeat_at`、`cursor_json`、`output_hash`、`error_code`、脱敏 `error_message`、创建/开始/完成时间。现有 `cursor_json` 可保存 per-job `approved` 和 retry lineage；这不是新的 state、column 或 schema field。每个 provider 逻辑请求总尝试最多两次（包含 SDK），显式 retry wave 创建新的 child generation，不增加单个逻辑请求的自动 retry budget；最终离线失败进入审核，详细分类见 [`openai-provider.md`](openai-provider.md)。发送并发由 D-044 约束为 `offline_annotation` 每 run 冻结 `1..5`（默认 `1`）、在线阶段固定 `1`，一个 process-lifetime shared in-process executor 最多 `5` slots 且所有 run 共用；global active sends `<=5`、per-run `<=` frozen offline bound。

#### 5.3.1 `AI_ANNOTATE` 批次授权、顺序 drain 和 Provider retry

手动 per-batch approval 是默认行为。WebUI 可以在用户确认前展示 unchanged frozen remaining batch plan；只有当每个计划 batch 都可 inspect、计划绑定 immutable plan hash、run-frozen provider 和 requested `model_id` 时，一次明确 confirmation 才能授权该计划自动顺序提交。没有该确认时，不能把多个 pending batch 当作自动波次发送。

计划 hash 必须只对 `run_id`、`effective_config_hash` 和按冻结顺序排列的 `job_id`、`logical_key`、recomputed payload signature 计算；canonical 形状和 TOCTOU all-or-none 语义以 [`decisions.md`](decisions.md) 的 D-040/D-041 为准。D-041 下 aggregate preview/confirmation 使用已持久化且已验证的 `jobs.input_signature`、cursor `payload_signature`/`input_hash`、`tile_ids`/`variant_ids`、run effective config 和 frozen provider snapshot；`recomputed_payload_signature` 名称保留，其 plan-time value 是 validated persisted payload signature。aggregate operation 不为所有 pending jobs 重建图片、contact sheet、prompt 或 machine metadata。确认 transaction 只验证 persisted identity；任一计划、pending 集合、持久化 hash、config/provider/requested model 或 hash 不一致时 approve none，否则只把所有 included pending jobs 的现有 cursor `approved` 置为可发送，并写一个 plan audit 和每个 job 的 approval audit。不存在 auto-mode field、stage cursor 或 config snapshot。

每个 planned batch 在 aggregate confirmation 前仍必须通过既有 safe preview lazy inspect；该 one-batch preview 可以重建其 bounded payload。Immediately before **every** actual external send，Worker 必须使用 frozen run profile 重建该 batch 的完整 payload/contact sheet/prompt/machine metadata，重新计算 full signature 并与 approved job signature 比较。任一 mismatch 必须 revoke that job approval、在任何 HTTP request 前 pause，且不得发送；该 final TOCTOU gate 不得被 aggregate persisted identity shortcut 绕过。D-041 不改变 manual mode/default、D-044 当前 send concurrency、item-local continue、fatal stop、retry 或 audit。

自动计划必须按 frozen order 使用 ordered contiguous approved claim barrier；`offline_annotation` 的 send concurrency 是每 run 冻结的整数 `1..5`（默认 `1`），`query_spec`/`visual_rerank` 固定为 `1`，并计数 logical batch 而不是 HTTP attempt。一个 process-lifetime shared in-process executor 最多 `5` slots，global active sends `<=5`，每 run `<=` 自己的 frozen offline bound；只能使用 run-frozen profile。global active profile 的变化只影响新的 Studio work/profile management，不能替换已有 run 的 adapter、model 或 base URL。item-local Provider failure 会记录 high `needs_review`，但继续下一个 approved batch；fatal Provider/config/auth/capability failure 必须在后续 send 前停止。`needs_review` item 不阻塞 AI_ANNOTATE drain：valid low-confidence item 和 item-local failure 继续到 `VALIDATE`、再到 `HUMAN_REVIEW`；只有 fatal 才终止 stage/run。

Provider retry 必须从 terminal `needs_review|failed` 的 AI job 行发起，且该 job 有 eligible item-local Provider error；fatal、`PROVIDER_CANCELLED` 和无 Provider error 的 job 不 eligible，variant review 不是 retry source。source 必须为 leaf；child cursor 包含 `retry_of_job_id`，nonce 由 source `job_id` 与 `input_signature` 确定性生成，每个 source 只有一个 child，failed child 可作为下一次显式 generation。row retry 与 bulk action 都必须在同一 transaction 内保持 source/evidence/provider request 可见并 resolve source 的 open provider-review siblings；重复 POST 返回同一结果，不产生第二个 child。bulk action 只覆盖 eligible failed leaf batches，且自动 approve 该 retry wave；succeeded 和无 Provider error 的 low-confidence review 不进入 wave。generic `retry-failed` 不得重跑 fatal/provider AI jobs。

页面必须在同一可滚动 work area 内展示全部 actionable `running`、`failed`、`needs_review` rows；pending/recent summary 可以 bounded。Provider error row 必须有 row retry，bulk action 必须明确确认；原始 request evidence、review 和错误分类继续可见。startup 只读检测 stale；auto-approved cursor 持久保留，只有显式 `recover` 改变 stale state。pause/cancel/fatal 只停止 claimed-unsent/later sends；already-started calls 可以完成并持久化 evidence/item terminal state，但不能 revive failed/cancelled run；fatal supersedes paused，不 supersede cancelled。SSE/browser disconnect 不改变后台工作。

每次 HTTP 前都必须通过 final full payload/signature/approval/run-stage gate，并先占用全局及 per-run active-send slot；HTTP 不得包在 SQLite transaction 中。发送线程不得共享 DB connection/transaction、provider client 或 provider mutable state。通过 final gate、占用 slot 并进入 HTTP call 的瞬间定义 send-started linearization；没有 durable pending provider request reservation，也没有 remote exactly-once claim。hard crash after send before commit 最多留下 frozen concurrency 数量的 unknown outcomes，startup 不自动 resend，显式 `recover` 仍是必要入口。唯一 shared executor 贯穿进程生命周期，stop waits；live futures 或 DB work 阻止 stale recovery，completion 必须同时满足 no DB work + no futures，不得 fake in-flight cancellation。

WebUI 可以提供 strict pristine same-run reconfiguration，但只在 run/stage paused at `AI_ANNOTATE` 时允许：不得有 live future/provider request、provider-request evidence、annotation/AI artifact/provider review/AI review、send/result/retry/cancel evidence；每个 AI job 必须 pending、unapproved、ownerless、clean。任一条件无法证明即 fail closed。成功后在原子本地操作中替换 frozen config/pending jobs，保留 R2/machine evidence，写 `R3_RUN_RECONFIGURED`，invalidate old plan，且不重用 approval。该操作不创建新 run，不引入新 status、SQL/Schema/migration、dependency 或 CLI；不得引入服务、队列、per-run executor、adaptive concurrency、fallback 或 retry 变化。

### 5.4 Review routes 和声明式 override

`GET /api/reviews?severity=high&status=open&limit=50` 返回 normal/high 任务、机器事实只读标记、AI 建议、冲突、图片安全引用和 task metadata。渲染缺失、机器/AI 冲突、低置信度、Schema 修复失败、provider 最终失败和无变体 skip 必须为 `high`。

`POST /api/reviews/{review_id}/resolve` 请求：

```json
{
  "decision": "accept|edit_and_accept|skip|request_reexport|request_exporter_rerender|retry_ai",
  "reason_code": "RENDER_FAILURE|MACHINE_AI_CONFLICT|LOW_CONFIDENCE|PROVIDER_FAILURE|NOT_A_BUILDING_CANDIDATE|OTHER",
  "note": "至少一个字符的审核说明。",
  "override": {
    "add_terms": {"building_roles": ["roof_detail"]},
    "remove_terms": {"building_roles": ["structural_wall"]},
    "replace_semantic": {"summary_zh": "黄褐色格栅薄板。"},
    "qualification": "conditional"
  }
}
```

`override` 只能修改 AI 语义、受控 qualification、warnings 和 skip 决定；不得出现 `block_id`、state、合法状态、机器几何、颜色原始测量、透明度、发光、支撑事实、图片、版本或发布状态。普通 override 按 `manual-override.v1` 保存；skip 和 qualification 审计必须分别使用 `skip-review.v1` 与 `qualification-review.v1`，字段至少为 `target_id`、`minecraft_version`、`reviewer`、`reviewed_at`、`reason_code`、`note`、`evidence`、`source_version`，skip 另加 `machine_failure_ref`。`request_reexport` 和 `request_exporter_rerender` 只生成外部 Fabric exporter 重新导出要求，Python 不选择状态或渲染图片。每条 override/review 必须独立保存 `operator_id`/reviewer、时间、原因、来源版本和目标 ID。多目标人工操作必须展开为每个明确 target ID 的独立记录；R4 `context.family` 不得成为持久化 target 或隐式全局规则。

qualification 只能是 `eligible`、`conditional`、`excluded`；`conditional` 必须有 warning。没有视觉变体的 Block 只有在 `skip-review.v1` 同时有完整字段和 `machine_failure_ref` 后才能通过 candidate-build gate；`excluded` 必须有独立 `qualification-review.v1` 的完整字段才能通过 candidate-build gate。activation gate 只复核 candidate report 及其 hash，不首次补做资格内容审计。人工不能把 override 写回机器事实。

### 5.5 Release routes

`POST /api/releases/check` 与 `POST /api/releases/build` 的 R3 Phase C 契约如下；本节是这两个路由的 authoritative WebUI owner。两者都必须同步完成，不得返回 `202`、排队后台 job 或让客户端轮询替代本次响应。Phase C 只依赖 R0–R3 与 candidate-build 前置。

#### `POST /api/releases/check`

请求 JSON 必须严格等于以下两个字段，未知字段、缺字段、错误类型和额外 query/body 输入均返回 `400 INVALID_INPUT`：

```json
{"run_id": "run_01J", "minecraft_version": "26.2"}
```

成功始终返回 HTTP `200`，包括门禁阻断但 check 本身完成的情况。成功 envelope 的 `data` 字段必须且只能是：

```json
{
  "check_id": "check_<32 lowercase hex>",
  "release_build_id": "build_<32 lowercase hex>",
  "run_id": "run_01J",
  "minecraft_version": "26.2",
  "status": "passed",
  "can_build": true,
  "snapshot_fingerprint": "sha256:<64 lowercase hex>",
  "quality_report_sha256": "sha256:<64 lowercase hex>",
  "created_at": "2026-08-15T12:00:00Z",
  "updated_at": "2026-08-15T12:00:00Z"
}
```

Provider retry 不由一个 variant review 单独授权或创建；review 只保留 evidence/审核关联，实际 retry source 必须是满足 D-040 的 terminal AI job。`retry_ai` 不能绕过该 job、leaf、generation、sibling-resolution 和总尝试预算约束。

`can_build=false` 是合法的 HTTP `200` 结果；具体 12 项与 `quality_report.json` 由 [`quality-and-testing.md`](quality-and-testing.md) 定义。响应不内嵌质量报告，不返回绝对路径或 workspace 数据。check cache 的精确持久化字段、最新 check 和 stale/TOCTOU 规则由 [`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md) 定义。

#### `POST /api/releases/build`

请求 JSON 必须严格等于以下两个字段；`confirm_immutable_release` 不是可选确认，必须是 JSON boolean `true`。未知字段、缺字段、错误类型或 `false` 一律返回 HTTP `400 INVALID_INPUT`：

```json
{"check_id": "check_<32 lowercase hex>", "confirm_immutable_release": true}
```

首次成功同步返回 HTTP `201`。成功 envelope 的 `data` 字段必须且只能是：

```json
{
  "check_id": "check_<32 lowercase hex>",
  "release_build_id": "build_<32 lowercase hex>",
  "release_id": "rel_<32 lowercase hex>",
  "run_id": "run_01J",
  "minecraft_version": "26.2",
  "relative_path": "releases/26.2/rel_<32 lowercase hex>",
  "status": "built",
  "manifest_sha256": "sha256:<64 lowercase hex>",
  "quality_report_sha256": "sha256:<64 lowercase hex>",
  "checksums_sha256": "sha256:<64 lowercase hex>",
  "built_at": "2026-08-15T12:00:00Z"
}
```

build 只能引用该 `(run_id, minecraft_version)` 的最新、未修改、`can_build=true` check；首次成功只构建一个新的不可变 candidate。check 的 `quality_report.json` 不得修改，release 内的 `quality_report.json` 必须由同一 snapshot 派生。成功完成后必须在同一 WebUI 操作边界内：将 `BUILD_RELEASE` 标为 `succeeded`，将 run 标为 `paused`，将 `current_stage` 设为 `ACTIVATE_RELEASE`，将 `ACTIVATE_RELEASE` 设为 `pending`，并写入边界标记 `R3_CANDIDATE_BUILT_ACTIVATION_PENDING`。Phase C **MUST NOT** 写 `current.json`。

这两个路由的 Phase C 精确错误映射为：

`RELEASE_CHECK_NOT_READY` 只表示 run 尚未满足 Gate C 前置，或 build 请求引用的 check 已完成但 `can_build=false`；`RELEASE_CHECK_FAILED` 只表示 check 执行本身以失败终止；`RELEASE_CHECK_STALE` 只表示该 check 不再是最新 check 或 fingerprint/TOCTOU 复核不一致。

| `error_code` | HTTP status | `retryable` |
|---|---:|---:|
| `INVALID_INPUT`（未知字段或 `false` confirm） | 400 | 否 |
| `RUN_NOT_FOUND` | 404 | 否 |
| `RELEASE_CHECK_NOT_READY` | 409 | 否 |
| `RELEASE_VERSION_MISMATCH` | 409 | 否 |
| `DATABASE_SCHEMA_MISMATCH` | 422 | 否 |
| `RELEASE_CHECK_NOT_FOUND` | 404 | 否 |
| `RELEASE_CHECK_FAILED` | 422 | 否 |
| `RELEASE_CHECK_STALE` | 409 | 否 |
| `RELEASE_ALREADY_BUILT` | 409 | 否 |
| `RELEASE_BUILD_INTEGRITY_FAILED` | 422 | 否 |
| `WORKER_UNAVAILABLE` | 503 | 是 |
| `RELEASE_BUILD_FAILED` | 500 | 否 |

错误 envelope 继续使用本文第 5 节的严格 `error_code`/`message`/`field_errors`/`retryable` 字段；上述列表不得用 `INTERNAL_ERROR` 或其它泛化码替代，`RELEASE_CHECK_FAILED` 只用于 check 执行失败而不是可审核的 `can_build=false` 结果。

#### 后续 R4/R5 activation routes（不属于 R3 Phase C）

`POST /api/releases/activation-check`：请求包含已 build 的 release 和精确 `minecraft_version`，执行 activation gate；其前置要求 R0–R4。它检查该版本至少两个独立 candidate release、四工具 MCP smoke、原子 current 准备和 candidate report/hash 引用；只复核 `excluded`/skip 审计报告及其 hash，不首次补做资格内容审计。返回 `activation_check_id`、检查数组和 `can_apply`。

`POST /api/releases/apply`：只接受最新未修改且通过 activation-check 的 `activation_check_id`、`confirm_current_switch=true` 和必填 boolean `set_as_default`。apply 的前置要求 R0–R4；首次激活首个 Minecraft 版本时 `set_as_default` 必须为 `true`，之后每次 apply 都必须由调用方显式决定是否切换 `default_minecraft_version`。apply 只切 `current.json`，写入 `updated_at` 并记录 workspace activation audit，不重新生成或修改 release；activation 时间不得写回 release，release 只使用 `built_at`。第一 release 或未通过 activation gate 返回 `RELEASE_ACTIVATION_GATE_FAILED`。

切换 `current.json` 必须：写临时 pointer → flush/fsync → 原子替换 → 重新读取校验。只有 WebUI publish/rollback service 能写 current。详见 [`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md) 与 [`security-and-distribution.md`](security-and-distribution.md)。

`POST /api/releases/rollback`：

```json
{"minecraft_version": "26.2", "target_release_id": "rel_01J", "reason": "回滚到已验证 release", "confirm_existing_release": true}
```

rollback 只切换到已有完整不可变 release，不修改内容、不删除审计证据、不接受 workspace/build 路径。

`POST /api/releases/cleanup`：

```json
{"minecraft_version": "26.2", "remove_unreferenced_workspace": true, "remove_unreferenced_search_artifacts": true, "confirm_release_protection": true}
```

cleanup 可以由 WebUI 人工删除未受保护的旧 release、workspace 临时产物和搜索产物，但每个精确 `minecraft_version` 必须至少保留两个成功、完整、不可变 release。`current`、pinned release、active-reader 正在使用的 release 和保底两个 release 不得删除；不得删除导出源、人工覆盖或审计记录。删除必须记审计；不足两个或目标受保护时返回 `CLEANUP_PROTECTED`。

### 5.6 Search test route

`POST /api/search-tests` 使用与 [`mcp-api.md`](mcp-api.md) `search_blocks` 等价的 input，加固定 `persist=false`；WebUI/API 必须显式提供 `minecraft_version`，release 只能解析 current：

```json
{
  "minecraft_version": "26.2",
  "query": "黄色的扁片方块，用于屋檐",
  "limit": 8,
  "context": {"rerank": "auto"},
  "persist": false
}
```

响应必须显示 resolved `minecraft_version`、`resolved_release_id`、`manifest_sha256`、QuerySpec、来源、hard filter、Top-24、`context.family` 的 R4 no-op/校验结果、contact sheet mapping、本地/LLM 排序、warning、`reranked_by_llm` 和 MCP 等价结构化对象。`context.family=null` 或任意通过 input schema 的 string 都不改变候选顺序、不分组、不限额且不产生 family warning/metadata；非 string 且非 null 返回 JSON-RPC `-32602`，不展示或创建 family metadata。release 只能由服务解析 current，客户端不能指定历史 release。不得展示 Token/cost、完整 provider response、绝对路径或自动写生产索引；图片/联系表只在请求生命周期内构造，测试 API 不得持久化。

## 6. 发布前置和错误码

import/run/review service 必须验证各自的 R0–R3 前置；`/api/releases/check` 和 `/api/releases/build` 只要求 R0–R3 及 candidate-build 前置。`/api/releases/activation-check` 和 `/api/releases/apply` 才要求 R0–R4、activation gate 和用户确认。不能未冻结 Schema、未锁依赖或未完成对应前置门就执行操作。关键错误码：

| `error_code` | 条件 | `retryable` |
|---|---|---:|
| `INVALID_INPUT` | JSON/表单非法 | 否 |
| `LOOPBACK_ONLY` | host 非 loopback | 否 |
| `DATABASE_SCHEMA_MISMATCH` | schema hash 不匹配 | 否 |
| `CONFIG_PRECEDENCE_CONFLICT` | 配置来源冲突 | 否 |
| `PROVIDER_CONFIG_INVALID` | adapter 非 `openai_responses`/`openai_chat_completions`、第二 model、未知字段 | 否 |
| `PROVIDER_NOT_CONFIGURED` | 无 secret 或无可用 active profile | 否 |
| `PROVIDER_CAPABILITY_MISSING` | 图片/strict/错误分类未通过 | 否 |
| `PROVIDER_STORAGE_UNSUPPORTED` | 旧兼容诊断码；不得用于声称或要求远端 retention 已验证 | 否 |
| `IMPORT_NOT_FOUND` | check/import 不存在 | 否 |
| `IMPORT_INCOMPLETE` | 导出包缺失/版本/hash/Schema 错 | 否 |
| `IMPORT_CHECK_IN_PROGRESS` | import 引用的 check 仍为 `running` | 否 |
| `IMPORT_CHECK_INTERRUPTED` | server 在 source snapshot 期间重启或 check 无法续跑 | 否 |
| `IMPORT_CHECK_PROGRESS_PERSIST_FAILED` | import check 的进度状态无法安全持久化 | 否 |
| `RUN_STATE_CONFLICT` | 状态不允许操作 | 否 |
| `RUN_NOT_FOUND` | run 不存在 | 否 |
| `REVIEW_NOT_FOUND` | review 不存在 | 否 |
| `MACHINE_FACT_READ_ONLY` | 请求改机器事实 | 否 |
| `OVERRIDE_INVALID` | 越权字段/缺 reason/目标无效 | 否 |
| `RELEASE_CHECK_FAILED` | 发布门失败 | 否 |
| `RELEASE_COUNT_INSUFFICIENT` | 少于两个独立 release | 否 |
| `RELEASE_IMMUTABLE` | 尝试改旧 release | 否 |
| `ROLLBACK_TARGET_INVALID` | rollback 目标不存在/不完整 | 否 |
| `CLEANUP_PROTECTED` | 删除受保护产物/证据 | 否 |
| `CURRENT_ATOMIC_REPLACE_FAILED` | current 原子切换失败 | 是 |
| `WORKER_UNAVAILABLE` | 内置 Worker 不可用 | 是 |
| `RELEASE_VERSION_MISMATCH` | 请求版本与输入/release 不同 | 否 |
| `INTERNAL_ERROR` | 未分类本地错误 | 否 |

## 7. 验收

必须以 service-level tests 和 HTTP/HTML adapter tests 验证：

1. 只注册 `block-index web`/`block-index mcp`；非法 host 失败，默认 `127.0.0.1:8765`；MCP 不监听 HTTP。
2. CLI/env/profile/default 优先级可观测；Keyring 优先、环境回退；key/Authorization/path/图片/完整 response/usage 不进入 SQLite、snapshot、日志、响应。
3. Provider 页面只允许一个活动 profile；`adapter` 只能显式选择 `openai_responses`/`openai_chat_completions`，能力探测只验证所选协议的 wire 形状，Responses 发送 `store=false`、Chat 省略 `store`，且不声称远端 retention 已验证；待发送预览生效。
4. `imports/check`、import、run、pause/resume/status、recover、normal/high review、override、release check/apply/rollback/cleanup、search test 的字段、状态和错误码稳定。
5. 机器事实修改、无审计 skip、未解决 high review、失败 release check、少于两个 release 均不能 apply。
6. current 只由 WebUI service 原子写；release 内容 immutable；MCP 和 search test 不产生写入。
7. 页面完全没有 Token/成本/预算能力；搜索 test 展示 QuerySpec、降级 warning 和 `reranked_by_llm=false`，不隐藏模型失败。

验证命令、证据路径和当前“未有实现/真实数据/测试报告”的状态必须遵守 [`quality-and-testing.md`](quality-and-testing.md) 与 [`roadmap.md`](roadmap.md)，不得用空目录或口头说明勾选路线图。
