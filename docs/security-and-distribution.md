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

源码必须支持 Windows 11 和 Linux x86_64 的可复现本地运行，Python/SDK/其他依赖使用精确锁和 hash。MVP **MUST NOT** 制作安装包、容器、系统服务或自动更新，也不得引入额外运行服务；默认数据根必须在源码之外。

## 3. 网络边界和威胁模型

MVP 假设单用户本机运行：

- WebUI 只绑定 `127.0.0.1:8765`，不得提供 IPv6 监听选项；
- MCP 只使用 stdio；
- 不添加账号、CORS 或 CSRF，这是冻结决定而非遗漏；
- loopback + 本机操作系统访问控制是唯一网络边界，不能解释为适合 LAN/公网暴露；
- 能访问 loopback 的本机其他进程可能访问 WebUI，用户必须依赖 OS 账户、进程隔离和本机环境保护秘密。

任何非 loopback 部署都是架构变更，必须新增认证、CSRF、TLS、威胁模型、审计和项目所有者书面批准；不能只改变 `--host` 或隐藏环境变量。MVP host 校验必须拒绝 `0.0.0.0`、`::`、局域网和公网地址。

MCP stdout 只能是 JSON-RPC/MCP 消息，日志、诊断和堆栈只能到 stderr，不得写本地日志文件。MCP 查询不能写数据库、文件、cache 或 `current.json`，详细工具边界见 [`mcp-api.md`](mcp-api.md)。

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

### 4.2 OpenAI 数据保留

MVP 只实现 [`openai-provider.md`](openai-provider.md) 的 `OpenAI Responses`。三类请求必须使用 `annotation-batch-output.v1`/`annotation_batch_output_v1`、`query-spec-output.v1`/`query_spec_output_v1`、`rerank-output.v1`/`rerank_output_v1` 的 strict wire Schema/name；实际 `store=false` 是 enable 硬门，不能证明或不支持时 probe fail、禁止 enable，不提供可绕过硬门的确认或豁免路径。MVP 完全不记录、不展示、不计算 Token usage，不估价、不预算、不生成成本页面。

系统只保留脱敏 provider `request_id`、错误码/分类、内部 operation ID、输入 hash、validated artifact hash 和稳定耗时 bucket；不得保留完整 request/response、usage、图片、Authorization 或 key。

## 5. 最小披露和提示注入防护

发送给 OpenAI Responses 的最小集合只能是：

1. 当前任务所需的裁剪 PNG/联系表；
2. 已验证的公开 Minecraft metadata 和确定性机器事实；
3. 内部短编号（`variant_id`、`candidate_id`）及当前用户 query；
4. 当前阶段所需受控词表、Schema 和规则摘要。

不得发送本机绝对路径、文件名中的秘密、API key、Authorization、SQLite、日志、完整导出包、整库数据、无关方块、无关用户查询、字体/纹理/模型源文件或未审核人工秘密。AI cache key、模型版本和 base URL stable ID 不能成为披露本机路径/秘密的通道。

用户 query、family、context、人工 note、历史 annotation 和 provider 返回文本均视为不可信数据：

- 放在独立数据区（例如 `<untrusted_user_query>`），不能与系统指令混写；
- 不得改变 Schema、model、候选集合、release selector、hard constraints、工具名或安全规则；
- 输出必须经 strict Schema、词表、ID 集合、来源和机器冲突校验；
- LLM 不得创建/改写 `block_id`、合法状态、状态字符串、几何、collision、透明度、发光、支撑、行为、发布状态或资格事实；
- visual rerank 只能排列本地已召回候选，不能增候选、删 hard constraint 或让新 ID 成为事实。

WebUI 每次发送前必须展示待发送的文本、图片和 machine metadata 预览，预览只能显示短 ID，不显示路径、key、Authorization、完整 SQLite 或无关数据。用户取消后不能产生 provider request。发送前预览不是完整 request/response 日志。

## 6. 日志、错误和零遥测

默认零外发遥测。结构化本地日志可以记录：`timestamp`、`level`、`component`、`event_code`、内部 operation/run/job ID、stage、稳定耗时 bucket、错误码、脱敏 provider request ID 和 artifact hash。日志禁止记录：

- API key、Authorization、Cookie、环境变量实际值；
- 图片 base64、PNG 内容、完整 prompt、完整 provider request/response；
- 本机绝对路径、用户名、数据库 SQL 全文、无关用户数据；
- Token usage、费用、预算、价格估算。

路径必须替换为稳定 hash 或相对 artifact ID；用户 query 仅在本地任务必要时最小保存，不自动外发。前端错误使用稳定 `error_code` 和可操作消息，详细诊断写本地脱敏日志；不得回显 key、路径或 provider body。WebUI/Worker 日志在 data root `logs/`，默认本地滚动；MCP 是例外，只写 stderr，绝不写 data-root logs、cache 或临时文件。无远程日志服务；用户主动导出诊断包时必须再次脱敏并列出字段清单。

## 7. Release、current 和回滚保护

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

生成并写入完整 hash manifest 后，release **MUST NOT** 原地修改。candidate release 使用 `built_at`；激活时间只写 `current.json` 和 workspace activation audit，不写回 release metadata。模型、prompt、Schema、词表、图片、人工覆盖或任何语义变化都必须产生新的 build/release；不能改旧 SQLite、图片、override 或 quality report。每个精确 Minecraft version 首发前至少有两个独立、完整性通过、带 hash 的不可变 release。

release manifest 必须记录精确 `minecraft_version`、`release_id`、来源 export、工具链 lock hash、Schema/prompt/vocabulary/search 版本、provider `profile_id`、`model_id`、`base_url_stable_id`（非秘密）、`secret_reference`、AI artifact/cache hash、覆盖审计和 quality report hash；manifest 只哈希功能输入/产物，`checksums.sha256` 另行覆盖 release 内其它普通文件。JSON、Schema 字段、metadata、manifest、current 和 release 中的 hash 字符串必须使用 `sha256:<64 lowercase hex>`；`checksums.sha256`/`schemas.sha256` 的行首 digest 不带前缀。`checksums.sha256` 格式为 `<64hex><two ASCII spaces><release-relative-posix-path>\n`，排除自身并按路径排序；`schemas.sha256` 格式为 `<64hex><two ASCII spaces><schema-id><two ASCII spaces><canonical-repository-relative-posix-path>\n`，按 schema ID UTF-8 bytes 排序，路径不声称位于 release。AI 产物冻结字段见 [`openai-provider.md`](openai-provider.md)。

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
2. **AI 语义建议**：strict Schema 内的 synonym、颜色/形状词、材质观感、用途、风格和候选理由，保存 model/prompt/schema/vocabulary/base URL stable ID、输入/产物 hash；
3. **人工覆盖**：独立声明式记录，含 operator、time、reason、source version、target ID，可按固定顺序 replay。

资格只能是 `eligible`、`conditional`、`excluded`；`conditional` 必须带 warning。skip 审计使用 `skip-review.v1`，qualification 审计使用 `qualification-review.v1`；二者都必须包含 `target_id`、`minecraft_version`、`reviewer`、`reviewed_at`、`reason_code`、`note`、`evidence`、`source_version`，skip 另含 `machine_failure_ref`。人工可以编辑 AI 语义、qualification 和 skip，但不能把值写回机器层；发现机器事实错误必须修复 exporter/重新导出，不能 override 隐藏。

## 9. 依赖、运行和变更控制

- 支持平台仅 Windows 11/Linux x86_64；依赖必须精确锁定并带 hash；
- Minecraft/Java/Fabric/Gradle/Loom/mappings 使用 [`AGENTS.md`](../AGENTS.md) 固定基线；
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
4. 三类 Responses 请求使用独立 strict wire Schema，实际 `store=false` 不能证明即 probe fail/enable fail；没有 Chat Completions/Anthropic/第二 model 路径；
5. provider 最小披露、发送前预览、untrusted data 隔离、候选边界和机器事实保护测试通过；
6. current 原子替换故障注入、release immutable、rollback 只切 pointer、cleanup 不删审计测试通过；
7. 公开包只含代码、文档、Schema、空库和 fixture 生成器源码；真实图片/索引只存在用户本地 data root；
8. 每个目标 Minecraft version 在首发检查中有至少两个独立完整 release，且发布门记录 `TWO_INDEPENDENT_RELEASES`；candidate-build gate 的 `excluded` qualification 审计已通过，activation gate 只复核其报告和 hash。

详细测试命令和报告要求见 [`quality-and-testing.md`](quality-and-testing.md)；当前没有实现或报告时必须保持未完成状态。
