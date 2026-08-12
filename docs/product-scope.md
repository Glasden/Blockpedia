# Blockpedia MVP 产品范围

## 产品定位

Blockpedia 是面向建筑 Agent 的本地、只读 Minecraft 方块知识与检索系统。它把自然语言中的视觉、形状、用途和行为要求，映射到指定 Minecraft Java 版本中真实存在的原版方块，并返回可核对的图片、`block_id`、建议状态和警告。

Blockpedia **不是** Minecraft 官方产品、百科、资源包或整栋建筑生成器。公开说明和所有用户可见入口都必须明确“Blockpedia 非官方，与 Mojang 或 Microsoft 无关”。正式支持平台为 Windows 11 和 Linux x86_64。

本文件服从 [`../AGENTS.md`](../AGENTS.md) 和 [`decisions.md`](decisions.md)，执行顺序见 [`roadmap.md`](roadmap.md)。移入的原始设计稿仅是历史背景和最低优先级参考，不与新文档一起执行；冲突内容禁止实现。

## MVP 用户闭环

1. 用户在固定的 Minecraft Java `26.2` 环境中启动自制 Fabric exporter，生成绑定版本的导出包；exporter 在 Minecraft 内完成 `EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS`。
2. 用户在 loopback WebUI 中显式选择 `minecraft_version`，导入导出包；Python Index Studio 只验证 exporter 已生成的 variants/renders，并运行离线特征提取。
3. Studio/WebUI 使用唯一 active profile 的 OpenAI Responses 模型生成受控语义，并把异常、低置信度和冲突交给人工审核；release-bound MCP 只使用 resolved release snapshot。能力探测不能证明并实际使用 `store=false` 时不得 enable。
4. Studio 按 `PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS → VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE → HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE` 构建不可变 candidate/release；Python 不重选 variant 或重渲染。
5. WebUI 运行 candidate-build gate；R3 可形成至少一个未激活 candidate，R4 在临时测试 data-root/current fixture 上测试 MCP；R5 再构建两个独立 candidate 并运行 activation gate，最后由用户人工激活 `current.json`。
6. MCP 客户端通过 `stdio` 调用四个只读工具。省略 `minecraft_version` 时使用 `current-pointer.v1` 的 `default_minecraft_version`，显式版本只解析该版本 current；未知或未发布版本失败，不回退，不支持历史 `release_id` selector。

在实现完成前，上述步骤只是产品契约，不代表仓库已有可运行实现或真实数据。

## 范围内

- Minecraft Java Edition `26.2` 的原版 `minecraft` 命名空间；每个索引绑定一个精确版本。
- 完整注册表登记，包括不适合作为建筑候选的方块；每个方块必须有变体或可审核跳过记录。
- 合法 BlockState、默认状态、属性值、形状/碰撞摘要、透明度、发光、支撑、有限行为和标准预览。
- 中文/英文名称、受控同义词、颜色词、形状词、材质观感、建筑用途、风格和不适用场景。
- 默认原版资源包生成的本地预览；第三方资源包、模组和 Bedrock 不在范围内。
- Fabric 客户端 exporter、Python/FastAPI/Jinja2/HTMX 本地 WebUI、SQLite、本地图片和进程内 Worker。
- OpenAI Responses 的文本/图片输入和 strict JSON Schema；只实现一个 `OpenAIResponsesProvider` adapter，允许用户批准的兼容 Responses 语义 `base_url`，不实现第二个 provider adapter。wire Schema ID 与 Responses `text.format.name` 分开，固定 name 为 `annotation_batch_output_v1`、`query_spec_output_v1`、`rerank_output_v1`。
- `stdio` MCP 的 `index_info`、`search_blocks`、`get_block_details`、`compare_blocks` 四个工具。
- 多 Minecraft 版本和每个版本的 release 历史并存；WebUI 导入、任务、构建、发布必须显式选择版本；MCP 可省略版本以使用 default，但不支持隐式“最新”或显式历史 release selector。

## 明确不在 MVP 范围内

- OpenAI Chat Completions、Anthropic Messages、其他 provider adapter、独立视觉模型、Embedding、向量数据库和训练/微调。
- Streamable HTTP、MCP `resources`、任意 SQL、MCP 写入和远程部署。
- 账号、团队权限、多租户、云端集群、局域网 WebUI、CORS 和 CSRF。
- Python 的导入、恢复、审核、发布和回滚命令；这些操作只从 WebUI 执行。
- WebUI `--host`/`--port` 或环境变量覆盖；WebUI 固定只绑定 `127.0.0.1:8765`。
- Token usage 记录/展示、费用、预算、价格估算和成本仪表盘。
- 通用 SQLite migration framework。
- 安装包、容器、系统服务和自动更新。
- 公开提交 Minecraft 原版 JAR、纹理、模型、截图或含真实索引的数据库。

黄金查询、Top-5 指标和排序调优在 MVP 后置，既不是本文件的首发要求，也不是 [`roadmap.md`](roadmap.md) 的必做退出条件。

## 覆盖、跳过与候选资格

### 注册表覆盖

Fabric exporter 必须从目标运行时枚举 `minecraft` 命名空间的完整方块注册表，而不是依赖人工清单。发布前必须满足：

```text
release_block_ids == runtime_minecraft_block_ids
```

比较必须按规范化 `block_id` 集合完成并记录差集；差集非空时不能发布。每个 `block_id` 至少有一条 Block 记录。

### 可审核跳过

方块可以因为渲染不可用、只适合作为技术辅助、没有稳定的代表状态或其他明确原因而跳过，但跳过不能删除注册表记录。跳过记录必须包含：

```text
block_id
skip_reason_code
human_reviewer
reviewed_at
review_note
evidence_paths
```

没有上述字段的“跳过”不满足覆盖要求。后续重新导入时可以重新打开跳过项，但不能覆盖原审核记录。

### 候选资格等级

`candidate_qualification` 是机器规则和人工覆盖的发布字段，不是 LLM 字段：

| 等级 | 含义 | 查询行为 |
|---|---|---|
| `eligible` | 图片、机器 Schema、状态和基本建筑使用检查通过，没有已知硬排除 | 可作为普通候选返回 |
| `conditional` | 可以使用，但需要支撑、方向、邻接、含水或其他上下文 | 可以返回，但必须带 `warnings` |
| `excluded` | 已审核为不应作为建筑候选 | 默认不进入候选召回，但保留在详情和审计数据中 |

LLM 不得设置或修改这三个值。人工可以根据机器证据和审核结果写覆盖；覆盖必须说明原因。一个方块没有发布变体时，其注册表记录仍然存在，并以审计跳过状态满足覆盖检查。

## 版本与 release 模型

### 数据根目录

高层唯一数据根目录树为：

```text
<data-root>/
├── exports/{minecraft_version}/{export_id}/
├── workspace/{minecraft_version}/{run_id}/
├── cache/
├── releases/{minecraft_version}/{release_id}/
├── logs/
└── current.json
```

不得使用 `work/` 或 `published/` 作为高层目录名。MCP 不写 `logs/`，也不写任何本地持久化状态。`minecraft_version` 在 MVP 中为 `26.2`，目录结构仍为后续版本并存保留隔离。WebUI 的项目、导入、任务、构建和发布必须先选择版本；MCP 缺省版本时使用 default current，显式版本时使用对应 current。

### `current-pointer.v1`

根 `current.json` 使用严格 `current-pointer.v1`；顶层字段只能是 `schema_version`、`versions` map、`default_minecraft_version` 和 WebUI 激活/回滚时更新的 `updated_at`。首次激活首个 Minecraft 版本时 `set_as_default=true` 强制；后续 apply 必须显式提供 `set_as_default` 决定是否切换 default。激活时间只写 workspace activation audit 和 current `updated_at`，release 使用 `built_at`，不使用 `published_at`：

```json
{
  "schema_version": "current-pointer.v1",
  "default_minecraft_version": "26.2",
  "updated_at": "2026-08-13T12:30:00Z",
  "versions": {
    "26.2": {
      "release_id": "rel_01J...",
      "minecraft_version": "26.2",
      "relative_path": "releases/26.2/rel_01J...",
      "manifest_sha256": "sha256:<64 lowercase hex>"
    }
  }
}
```

MCP 省略 `minecraft_version` 时解析 default 版本对应 current；显式版本只解析该版本 current；未知、未发布或没有可用指针的精确版本失败且不回退。MCP 不接受显式历史 `release_id` selector；历史切换只能通过 WebUI rollback。`current.json` 只能由 WebUI publish/rollback 原子更新。

### release 要求与 layout

candidate-build gate 只检查内容完整性，不含 MCP smoke、双 release 或 current 切换。R3 可构建至少一个不可变未激活 candidate 供 R4 使用；R4 使用临时测试 data-root/current fixture，不激活生产 current。R5 先构建至少两个独立通过 candidate-build gate 的 release，再执行 activation gate（四工具 MCP smoke、两个 release、原子 current），最后由用户人工激活。

唯一 release layout 高层引用为：

```text
<data-root>/releases/{minecraft_version}/{release_id}/
├── release.json
├── manifest.json
├── index.sqlite3
├── previews/
├── quality_report.json
├── manual-overrides.json
├── schemas.sha256
└── checksums.sha256
```

不得使用 `release.sha256`、YAML override 或 `contact-sheets/` 作为 release 契约名称。JSON、Schema 字段、metadata、manifest、current 和 release 中的 hash 字符串统一表示为 `sha256:<64 lowercase hex>`；唯一文本例外是 `checksums.sha256` 与 `schemas.sha256` 的行首 digest，均不带前缀。`checksums.sha256` 排除自身，格式为 `<64hex><two spaces><release-relative-posix-path>\n`，按路径排序；manifest 只哈希功能输入/产物且不自引用；`release.json` 可保存 manifest hash。`schemas.sha256` 是 Schema inventory，格式为 `<64hex><two spaces><schema-id><two spaces><canonical-repository-relative-posix-path>\n`，按 schema-id UTF-8 字节序排序，不声称这些路径位于 release；细节由 pipeline 文档定义。

## 公开分发与本地数据

公开仓库只允许包含：

- 源码；
- 简体中文文档；
- 真实 JSON Schema、状态/字段契约和不含真实原版资产的空数据结构；
- fixture 生成器源码；
- 一个不含真实索引、图片、导出内容和秘密的空 SQLite 数据库（如需要）。

不得提交 fixture 生成后的 PNG、非空数据库、真实索引、预览、导出包、人工覆盖或秘密。真实 Minecraft 导出包、预览图片、contact sheet、SQLite 索引、人工本地覆盖、OpenAI Key 和 release 必须由用户在本地生成或保存，不能进入公开仓库。任何包含原版资产的测试输入也只能存在于本地测试目录。

发布说明、WebUI 首页和 MCP `index_info` 的可见元数据都必须带非官方声明或指向它的文档说明。公开分发不提供安装包、容器镜像、系统服务文件或自动更新器。

## 产品验收边界

MVP 的验收只判断契约和闭环是否成立：

1. 指定版本的完整注册表能够被 exporter 登记，缺少变体时有可审计跳过；Python 不重选或重渲染；
2. 机器事实、AI 语义、人工覆盖和资格等级在数据中可区分并可回放；
3. OpenAI Responses strict Schema 校验、`store=false` 硬能力门、一次重试和人工审核路径可执行；
4. candidate-build gate、至少两个独立 release、activation gate、`current.json` 原子切换和 WebUI 回滚可验证；
5. MCP 四工具只读不可变 release，默认/显式版本选择、ID、状态、图片和 JSON 映射一致；
6. WebUI/MCP 在目标平台按源码和锁依赖可复现启动，且公开白名单不包含生成资产或真实数据。

MVP 不预先承诺搜索相关性、Top-5 命中率、费用或 Token 数字；这些结果必须在后续质量工作中用真实证据测量。
