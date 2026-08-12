# WebUI 与本地运行契约

## 文档状态与导航

本文定义 Blockpedia 本地 `Index Studio`、进程内任务、所有写操作入口、配置/秘密边界、稳定路由和两个允许的 Python 命令。正文使用简体中文；路由、字段、状态、错误码、Schema 和命令保持英文。`MUST`、`MUST NOT`、`SHOULD`、`MAY` 为规范性关键字。

本文服从 [`../AGENTS.md`](../AGENTS.md)、[`roadmap.md`](roadmap.md)、[`decisions.md`](decisions.md)、[`product-scope.md`](product-scope.md) 和 [`architecture.md`](architecture.md)。原始设计稿 [`minecraft_vanilla_block_index_mcp_design.md`](minecraft_vanilla_block_index_mcp_design.md) 仅作历史背景和最低优先级参考，不与本契约一起执行；冲突内容禁止实现。数据字段/来源见 [`data-and-schemas.md`](data-and-schemas.md)，导出与发布流水线见 [`export-contract.md`](export-contract.md)、[`state-policy-and-rendering.md`](state-policy-and-rendering.md)、[`pipeline-storage-and-publishing.md`](pipeline-storage-and-publishing.md)。

相关组件契约：

- [`openai-provider.md`](openai-provider.md)：唯一 `OpenAI Responses` profile、能力探测和严格重试；
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
  → repositories + OpenAIResponsesProvider + file/release store
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

来源冲突或类型不兼容必须返回 `CONFIG_PRECEDENCE_CONFLICT`，不得静默合并。CLI 只能覆盖 `data-root` 等非冻结启动项；host/port 不属于配置，永远固定为 `127.0.0.1:8765`。环境变量包括 `BLOCKPEDIA_DATA_ROOT` 和 `OPENAI_API_KEY`（后者只作为秘密回退）；profile/project 配置包含 `model_id`、非秘密 `base_url`、版本、超时、并发、Schema/词表/prompt 和搜索版本；内置默认只提供冻结值。

最终生效配置必须计算 `effective_config_hash` 并写入 run/job snapshot。snapshot 可以包含：`profile_id`、`model_id`、`base_url_stable_id`、`minecraft_version`、`resolved_release_id`、`manifest_sha256`、并发、超时、Schema/prompt/vocabulary/search 版本、重试策略和非秘密路径稳定标识；**MUST NOT** 包含 API key、Authorization、图片 bytes、完整 provider response、Token usage、成本或预算。

### 3.3 秘密

API key 必须优先从 OS Keyring 读取：`service=blockpedia`、`account=<profile_id>`；Keyring 没有值时才只读读取 `OPENAI_API_KEY`。SQLite 只能保存不可逆/不可还原的 `secret_reference`，如 `keyring:blockpedia/default` 或 `env:OPENAI_API_KEY`。前端只能收到：

```json
{"configured": true, "source": "keyring|environment|none", "masked": "••••••••abcd"}
```

无法安全掩码时只返回 `configured=true`。key 和 Authorization **MUST NOT** 出现在 profile/project 文件、SQLite、任务 snapshot、日志、异常、截图、prompt、图片 metadata、导出包、release、HTTP response 或浏览器存储。

### 3.4 SQLite schema 和写事务

MVP **MUST NOT** 使用通用 SQLite migration framework。schema 在 R0 冻结；结构变化必须先按 [`AGENTS.md`](../AGENTS.md) 和 [`decisions.md`](decisions.md) 更新契约、重建数据库并重跑完整性门。启动时 schema hash 不匹配必须停止写操作，返回 `DATABASE_SCHEMA_MISMATCH`。启动恢复流程只能读取并展示 stale `running` job；不得自动把它写回 `pending`、`failed` 或其他状态。只有用户显式 POST `/api/runs/{run_id}/recover` 后，服务才能在事务中改变超时且未完成的 job 状态；成功 job 永不重跑。

所有 WebUI 写操作必须在 SQLite transaction 中同时写状态和审计记录；写文件必须先校验版本、目标 ID、release 和输入，再采用临时文件、flush/fsync 和原子替换。失败时回滚数据库，不留下“成功”状态。SQLite 不能存图片 BLOB，只能存安全的相对 artifact ref、尺寸和 hash。

## 4. WebUI 页面和功能

页面必须覆盖下表；不得创建 Token/成本/预算页面或字段：

| 区域 | 必须能力 | 明确禁止 |
|---|---|---|
| `Provider` | 多个非活动 profile、唯一 active profile、Keyring/env 状态、能力探测、enable/disable、待发送预览 | Chat Completions、Anthropic、第二 model、usage/成本、`store=false` 硬门绕过 |
| `Import` | 版本选择、导出包 check、导入到 workspace、逐项错误 | 直接覆盖 release、CLI 导入 |
| `Pipeline` | run/pause/resume/status、heartbeat、失败恢复、单项显式重试 | 无限自动重试、无审计后台写入 |
| `Review` | normal/high 队列、机器事实只读、语义编辑、声明式 override、skip | 修改机器事实、无原因 skip、静默发布 |
| `Release` | check/build/activation-check/apply/rollback/cleanup、hash、审计 | 原地改 release、删除回滚证据 |
| `Search test` | QuerySpec、hard filter、Top-24、family 去重、联系表、降级结果 | 持久化测试 query、隐藏 warning、放宽 hard |
| `Settings/Logs` | data root、日志级别/保留、脱敏日志 | 远程遥测、秘密查看、Token/成本仪表盘 |

Provider 页面可以保存多个非活动 `openai_responses` profile，但全局最多一个 active profile；active 只控制 Studio 新写任务和新 release。release-bound MCP 使用已解析 release 冻结的 provider snapshot，不读取或比较可变 active profile。每个 profile 在 enable 前必须通过图片、实际 Responses Structured Outputs strict Schema、错误分类和实际 `store=false` 四项硬能力门。任一项失败都禁止 enable；不得用确认、豁免或其他路径绕过 `store=false`。具体字段见 [`openai-provider.md`](openai-provider.md)。

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

`PUT /api/provider/profile` 请求最少包含：`profile_id`、`base_url`、`model_id`、`secret_reference`、`request_timeout_ms`、三个 stage 的 `batch_size/concurrency`、`prompt_version`、`annotation-batch-output.v1`、`query-spec-output.v1`、`rerank-output.v1`、`vocabulary_version`、`search_ranking_version`。请求 Schema 不包含任何存储确认或绕过字段。出现 `api_key`、`provider` 非 `openai_responses`、第二 model 或未知字段必须返回 `PROVIDER_CONFIG_INVALID`。保存状态为非活动 `unverified`；改变 base URL、model、secret ref、Schema、词表、prompt 或排序版本必须使能力变为 `unverified` 并禁用该 profile 的新 AI job。`draft` 只能作为配置编辑命令/事件，不得作为持久化 run、stage 或 item 状态。

`POST /api/provider/probe` 请求只接受 profile 标识，不提供任何存储确认或绕过字段：

```json
{"profile_id": "default"}
```

服务使用原创最小 PNG，并实际发送生产使用的三个 strict wire Schema/name 对（`annotation-batch-output.v1`/`annotation_batch_output_v1`、`query-spec-output.v1`/`query_spec_output_v1`、`rerank-output.v1`/`rerank_output_v1`）探测图片、Structured Outputs、错误分类和实际 `store=false`，返回 `verified|failed`、能力布尔值、错误码、脱敏 request ID 和时间。任一能力不能证明时必须是 `failed`，包括 `PROVIDER_STORAGE_UNSUPPORTED`；不得提供确认、豁免或其他继续 enable 的路径。

`POST /api/provider/enable`/`disable` 只接受 `profile_id`。enable 只允许 capability status 为 `verified` 且四项硬能力门均通过；disable 停止新 AI job，不删除历史产物。Keyring/env 无法解析时不得 enable。

### 5.2 Import routes

`POST /api/imports/check` 请求：

```json
{"source_directory": "<local chooser ref>", "minecraft_version": "26.2"}
```

服务可读取用户选择目录，但 response、日志和数据库只能保存 `source_directory_ref`（稳定 hash/本地标签），不得保存绝对路径。检查 [`export-contract.md`](export-contract.md) 的 manifest、JSONL、PNG、hash、版本、完整 `minecraft` registry、合法状态和 exporter 清单。输出 `check_id`、版本、计数、检查数组和 `can_import`。

`POST /api/imports` 只能接受通过 check 的 `check_id`、`project_id` 和固定 `copy_mode=copy_to_workspace`；成功生成 `import_id`/`run_id` 和审计事件。禁止把来源目录直接作为 release。

### 5.3 Pipeline routes

Studio 阶段固定为：

```text
PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS
        → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE
        → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE
```

item 状态固定为 `pending|running|succeeded|needs_review|failed|skipped`；run/stage 状态固定为 `pending|running|paused|needs_review|failed|succeeded|cancelled`。合法持久转换为 `pending→running`、`running→paused`、`paused→running`、`running→needs_review|failed|succeeded|cancelled`；pause/cancel 只作为命令或 event，不作为持久状态。release 的 build/apply 独立于 run 状态；recover 只能恢复 heartbeat 超时且未完成的 job；成功 job 不重跑。

`POST /api/runs` 请求至少包含：`import_id`、`minecraft_version`、`profile_id`、联系表大小和置信度阈值；不得接受或要求 `release_build_id`。服务必须检查版本、导出契约、profile capability、Schema/词表、阈值和 R0–R3 前置门，返回 `run_id`、`status=pending`、`effective_config_hash` 和非秘密 snapshot。后续 `POST /api/releases/check` 以 `run_id` 创建并返回 `release_build_id`。

`GET /api/runs/{run_id}` 至少返回：`run_id`、精确版本、run status、stage、progress、heartbeat、非秘密 config snapshot 和 warnings。启动时 stale 只读展示，不改变这些状态；不得返回 Token usage、费用、图片 base64 或完整 provider response。

以下 routes 必须幂等或返回 `RUN_STATE_CONFLICT`：

```text
POST /api/runs/{run_id}/pause
POST /api/runs/{run_id}/resume
POST /api/runs/{run_id}/cancel
POST /api/runs/{run_id}/retry-failed
POST /api/runs/{run_id}/recover
```

Worker job 字段至少包括：`job_id`、`run_id`、`stage`、`logical_key`、`status`、`auto_attempt`、`priority`、`heartbeat_at`、`cursor_json`、`output_hash`、`error_code`、脱敏 `error_message`、创建/开始/完成时间。每个 provider 逻辑请求总尝试最多两次（包含 SDK），最终离线失败进入审核；详细分类见 [`openai-provider.md`](openai-provider.md)。

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

`override` 只能修改 AI 语义、受控 qualification、warnings 和 skip 决定；不得出现 `block_id`、state、合法状态、机器几何、颜色原始测量、透明度、发光、支撑事实、图片、版本或发布状态。普通 override 按 `manual-override.v1` 保存；skip 和 qualification 审计必须分别使用 `skip-review.v1` 与 `qualification-review.v1`，字段至少为 `target_id`、`minecraft_version`、`reviewer`、`reviewed_at`、`reason_code`、`note`、`evidence`、`source_version`，skip 另加 `machine_failure_ref`。`request_reexport` 和 `request_exporter_rerender` 只生成外部 Fabric exporter 重新导出要求，Python 不选择状态或渲染图片。每条 override/review 必须独立保存 `operator_id`/reviewer、时间、原因、来源版本和目标 ID。`family` 应展开为每个明确 target ID 的独立记录；无隐式全局魔法。

qualification 只能是 `eligible`、`conditional`、`excluded`；`conditional` 必须有 warning。没有视觉变体的 Block 只有在 `skip-review.v1` 同时有完整字段和 `machine_failure_ref` 后才能通过 candidate-build gate；`excluded` 必须有独立 `qualification-review.v1` 的完整字段才能通过 candidate-build gate。activation gate 只复核 candidate report 及其 hash，不首次补做资格内容审计。人工不能把 override 写回机器事实。

### 5.5 Release routes

`POST /api/releases/check`（`candidate-build gate`）：

```json
{"run_id": "run_01J", "minecraft_version": "26.2"}
```

返回 `check_id`、`release_build_id`、固定 candidate-build 检查数组、failure codes、artifact hashes 和 `can_build`。该 check 只接受 `run_id`，并创建对应的 `release_build_id`；不接受客户端传入的 `release_build_id`。candidate-build gate 只检查该单 release 的内容完整性，不包含 MCP smoke、`TWO_INDEPENDENT_RELEASES` 或 current 切换。它的前置只要求 R0–R3 和 candidate-build 前置，不要求 R4。检查项见 [`quality-and-testing.md`](quality-and-testing.md)。

`POST /api/releases/build`：

```json
{"check_id": "release_check_01J", "confirm_immutable_release": true}
```

build 只能引用最新且未修改的通过 candidate check；它的前置只要求 R0–R3 和 candidate-build 前置，不要求 R4。必须构建不可变但未激活的 `releases/<minecraft_version>/<release_id>/`，包括 `release.json`、`manifest.json`、`index.sqlite3`、`previews/`、`quality_report.json`、`manual-overrides.json`、`schemas.sha256` 和 `checksums.sha256`，完成 fsync/hash 后原子 rename，并以 `built_at` 记录构建时间。release 生成后不得原地修改。第一份 release 可以 build，但不能 apply。

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

响应必须显示 resolved `minecraft_version`、`resolved_release_id`、`manifest_sha256`、QuerySpec、来源、hard filter、Top-24、family 去重、contact sheet mapping、本地/LLM 排序、warning、`reranked_by_llm` 和 MCP 等价结构化对象。release 只能由服务解析 current，客户端不能指定历史 release。不得展示 Token/cost、完整 provider response、绝对路径或自动写生产索引；图片/联系表只在请求生命周期内构造，测试 API 不得持久化。

## 6. 发布前置和错误码

import/run/review service 必须验证各自的 R0–R3 前置；`/api/releases/check` 和 `/api/releases/build` 只要求 R0–R3 及 candidate-build 前置。`/api/releases/activation-check` 和 `/api/releases/apply` 才要求 R0–R4、activation gate 和用户确认。不能未冻结 Schema、未锁依赖或未完成对应前置门就执行操作。关键错误码：

| `error_code` | 条件 | `retryable` |
|---|---|---:|
| `INVALID_INPUT` | JSON/表单非法 | 否 |
| `LOOPBACK_ONLY` | host 非 loopback | 否 |
| `DATABASE_SCHEMA_MISMATCH` | schema hash 不匹配 | 否 |
| `CONFIG_PRECEDENCE_CONFLICT` | 配置来源冲突 | 否 |
| `PROVIDER_CONFIG_INVALID` | 非 Responses、第二 model、未知字段 | 否 |
| `PROVIDER_NOT_CONFIGURED` | 无 secret 或无可用 active profile | 否 |
| `PROVIDER_CAPABILITY_MISSING` | 图片/strict/错误分类未通过 | 否 |
| `PROVIDER_STORAGE_UNSUPPORTED` | endpoint 不支持或不能证明实际 `store=false` | 否 |
| `IMPORT_NOT_FOUND` | check/import 不存在 | 否 |
| `IMPORT_INCOMPLETE` | 导出包缺失/版本/hash/Schema 错 | 否 |
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
3. Provider 页面只允许一个活动 Responses profile；能力探测的 `store=false` 硬门和待发送预览生效。
4. `imports/check`、import、run、pause/resume/status、recover、normal/high review、override、release check/apply/rollback/cleanup、search test 的字段、状态和错误码稳定。
5. 机器事实修改、无审计 skip、未解决 high review、失败 release check、少于两个 release 均不能 apply。
6. current 只由 WebUI service 原子写；release 内容 immutable；MCP 和 search test 不产生写入。
7. 页面完全没有 Token/成本/预算能力；搜索 test 展示 QuerySpec、降级 warning 和 `reranked_by_llm=false`，不隐藏模型失败。

验证命令、证据路径和当前“未有实现/真实数据/测试报告”的状态必须遵守 [`quality-and-testing.md`](quality-and-testing.md) 与 [`roadmap.md`](roadmap.md)，不得用空目录或口头说明勾选路线图。
