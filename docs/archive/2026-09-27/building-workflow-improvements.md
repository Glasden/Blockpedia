> 历史归档（2026-09-27）：停止维护，不再作为当前执行规范。当前决定与进度统一见 [PROGRESS.md](../../../PROGRESS.md)。正文只做归档链接修正，旧状态不代表本轮完成。

# 面向建筑工作流的修改建议

> **文档状态**：建议稿，2026-09-27。尚未经 owner 批准，不是契约，不改变任何现有 Schema、决策或实现要求。其中涉及 Schema、D-050 或 AI 重新标注的条目，须先在 [`decisions.md`](decisions.md) 记录并获批准后再实施。

## 1. 背景与结论

本建议来自一次评估：Blockpedia MCP 能否作为 `mc_architect`（Minecraft Java 26.2 建筑生成与视觉迭代项目）的选材辅助工具。评估方式是直接以 stdio 启动 `mcp-node/server.mjs` 调用四个工具，并读取当前 release（`rel_d0204fb090764c94b4a868a29ec50894`，1196 blocks / 1172 visual variants / 24 audited skips）的 `index.sqlite3` 与预览 PNG。

Blockpedia 对建筑有明确价值：它在注册表事实之上提供了语义层（`building_roles`、`color_terms`、`style_tags`、`material_impressions`、中英同义词）、中文检索、四视角联系表和逐状态几何数据，恰好补足“选什么方块”这一步。

但就当前版本而言，颜色和排序会误导选材，详情接口也过于消耗上下文，暂时不适合接入。以下 P0 四项是接入前提，P1 是提升建筑可用性的改进。

## 2. P0：接入前必须解决

### 2.1 预览渲染偏暗，颜色特征随之失真

**现象**：直接读取 release 原始预览 PNG（512×512，四视角依次为等轴/正面/侧面/顶面），取各视角中心区域平均色：

| 方块 | 正面 RGB | 侧面 RGB | 顶面 RGB | `feature_json.lab` L* |
|---|---|---|---|---|
| `snow_block` | 135,138,138 | 99,101,101 | 99,101,101 | 47.9 |
| `white_wool` | 126,127,128 | 92,93,93 | 93,94,94 | 44.8 |
| `quartz_block` | 129,126,122 | 94,92,89 | 94,92,89 | 43.9 |
| `oak_planks` | 92,74,44 | 67,54,32 | 62,49,29 | 25.6 |

- 雪块 L*≈48，低于 `query.mjs` 中 `gray` 的 Lab 目标（53.6）；白色系与灰色系在颜色评分中难以区分，联系表中白羊毛、雪块、石英同为中灰。
- 反推整体亮度约为预期的 0.7 倍。
- 顶面与侧面同样暗。原版方向阴影约为顶 1.0、南北 0.8、东西 0.6；`stripped_spruce_log` 的“顶面”格确为年轮端面，说明取景正确但顶面被按侧面阴影压暗。
- 离线标注可能受到影响：`white_wool`、`white_concrete` 的 `color_terms` 均含 `light gray`。

**可能原因（待在 exporter 中核实）**：`ExporterConstants.FULL_BRIGHT` 指定了全亮 lightmap 坐标，但渲染时 lightmap 纹理本身可能未更新到全亮（整体约 0.7 倍）；方向阴影可能按相机空间而不是方块面朝向计算。

**建议**：修正 `RenderExporter` 的 lightmap 与面阴影 → 重新导出 → 重算 `feature_json` 颜色特征 → 重跑受影响的颜色标注（有 provider 费用，需批准）。

**验收**：
- 各面平均色 = 贴图平均色 × 原版面阴影系数，误差 ≤5%；顶面最亮。
- `snow_block`、`white_wool`、`white_concrete` 的 L* ≥ 85。
- `search_blocks(["white"])` 前 8 名不含明显灰色方块。

### 2.2 资格分级实际未生效

**现象**：1172 个视觉变体全部为 `eligible`，`conditional` / `excluded` 均为 0。以下方块都会被当作普通建材推荐：

- 管理/技术方块：`command_block`、`chain_command_block`、`repeating_command_block`、`structure_block`、`jigsaw`、`test_block`、`test_instance_block`、`piston_head`、`nether_portal`、`spawner`、`trial_spawner`。
- 有隐患或会自行变化的方块：`infested_*`（会生蠹虫）、`frosted_ice`（会融化）、`fire` / `soul_fire`、`budding_amethyst`。
- `infested_stone` 的标注只写“普通灰色石块”，没有任何提醒；搜 `stone` / `cobblestone` 时它与 `bedrock` 都以满分出现。

**建议**：用 Studio 已有的 qualification review 做一次审核：

- `excluded`：管理/技术方块、传送门、活塞头等。
- `conditional` + 警告：蠹虫方块、受重力方块、会融化/蔓延/生长的方块、需要支撑的方块。
- 行为警告优先由机器事实（方块类、tags）推导，不依赖 AI 标注。

**验收**：`search_blocks(["stone"])`、`search_blocks(["cobblestone"])` 默认结果中，`infested_*` 与 `bedrock` 不出现，或以 `conditional` 带警告排在后面。

### 2.3 搜索召回与排序

对应 `mcp-node/query.mjs` 的 `_recall`、`_rankRows`、`keywordIntent` 与 `src/blockpedia/search.py` 的检索文本。

**现象与原因**：

| 问题 | 原因 | 实例 |
|---|---|---|
| 召回过宽 | 关键词拆成单词后逐词取并集，任一词命中即召回 | `smooth stone wall` 前几名为 `acacia_wall_sign`、`bamboo_shelf` |
| 大量并列 | `containsAny` 只返回 0/1，不计命中比例，也不区分字段；并列按 `variant_id` 字母序 | `stone` 满分前列为 `andesite`、`bedrock`、`blackstone`…；`warm roof shingle` 第一名为 `waxed_lightning_rod` |
| 意图词表过小 | `USE_TERMS` 9 个、`STYLE_TERMS` 7 个；`materials` 已计算但未参与评分；中文颜色只有红/黄/蓝/绿 | `白色 墙` 颜色不起作用，前列为“墙上的…告示牌” |
| 中文用途不匹配 | `building_roles` 全为英文 | 中文用途词只能匹配名称 |
| `avoid_for` 进入召回 | `SEMANTIC_LIST_FIELDS` 包含 `avoid_for`，且其含义混杂 | 苔石的 `avoid_for` 为“smooth stone walls”，搜光滑石墙反而召回它；`quartz_block` 的 `avoid_for` 是易混方块，`calcite` 的是 `budding amethyst`、`cake` |
| 形状分失效 | `geometry_classes` 只有 `full_cube`/`partial_height`/`thin`/`horizontal_sheet`，269 个变体为空（含楼梯、栅栏）；`SHAPE_TERMS` 期待的 `slab_like`、`stair_like` 等从未导出 | `slab` 只靠名称命中，形状权重 0.35 空转 |
| 同系列刷屏 | 无系列归并 | `window shutter` 前 8 名全是彩色玻璃板，没有活板门 |

**建议**：

1. 从召回文本移除 `avoid_for`；标注字段拆为 `not_for`（不适合的用途）与 `confusable_with`（易混淆方块）；`not_for` 命中时降分。
2. 按命中程度打分：整短语命中 > 全部词命中 > 部分命中；字段加权：名称/同义词 > 用途/材料/风格 > 摘要；英文按词边界匹配。
3. 用途、风格、材料直接与标注字段比对，不再依赖小词表；补齐中文颜色词（白、黑、灰、棕、橙、紫、粉、青等）。`building_roles` 目前有 1413 个自由写法并含单复数重复（`step`/`steps`、`low platform`/`low platforms`），建议收敛为受控词表并附中文。
4. exporter 按运行时方块类导出形状分类：`stairs`、`slab`、`wall`、`fence`、`fence_gate`、`pane`、`trapdoor`、`door`、`carpet`、`button`、`pressure_plate` 等，与 `SHAPE_TERMS` 对齐。
5. 并列时依次比较：名称精确命中 → 标注置信度 → 基础方块优先于楼梯/台阶等形状（查询未指定形状时）→ `conditional` 置后。
6. 可选：未指定颜色时，把同系列 16 色变体归并为一条。此项与 D-050（不做 family 分组）冲突，需新决策。

**验收**：建立约 20 条来自真实建筑需求的查询黄金集，为每条标注期望前 3 名，作为本轮完成门。当前 [`product-scope.md`](product-scope.md) 写明黄金查询不作为完成门，此项需要调整。建议覆盖：光滑石墙、深色木屋顶、窗框/百叶、白色抹灰墙、苔藓旧墙、木梁、栏杆、窗台线脚、暖光灯，以及上述需求的中文写法。

### 2.4 `get_block_details` 响应过大

**现象**：`minecraft:oak_stairs` 响应约 135KB，`minecraft:spruce_trapdoor` 约 100KB。以 `oak_stairs` 为例：

- `states` 约 105KB，逐一列出 80 个状态的形状、碰撞和行为。
- `variants` 约 41KB，其中 `state_behaviors` 约 34KB，与 `states[].behavior` 重复。
- 全部 32,366 个状态的 `requires_support` 与 `support` 均为 `unknown`。
- 最有用的语义标注只返回 `summary_zh`、`summary_en` 和 `confidence`，用途、风格、材料、颜色词均未返回。

**建议**：增加 `detail: "summary" | "states"`，默认 `summary`：

- `summary`：名称、默认状态、属性定义、tags、完整语义标注、按形状签名分组的几何摘要（活板门 64 个状态只有少数几种形状）、同系列方块（见 §3.4）、图片。
- `states`：才逐一列出状态。
- 去掉重复的 `state_behaviors`；`support` 字段在真正实现前从输出移除。

需修改 `mcp-block-details-output.v1`（closed Schema）并记录决策。

**验收**：`summary` 模式下楼梯与活板门响应 < 8KB。

## 3. P1：提升建筑可用性

### 3.1 `compare_blocks` 比较字段

当前只输出取值不同的行，而候选字段仅有 qualification、geometry、transparent、emissive、emission_level、redstone_related 与 `summary_en`；比较四种木板时只剩 `summary_en` 一行。建议加入：各面平均色（hex + L*）、亮度、纹理噪声、`color_terms`、`material_impressions`、`style_tags`、`building_roles`、形状分类、同系列可用形状。`context` 参数当前未被使用，建议使用或删除。

### 3.2 颜色特征细化

当前每个变体只有一组 `lab`/`oklab`。建议顶面、侧面分开计算，并增加亮度标准差（区分光滑与斑驳）与 2–3 个主色，以支持墙面混搭与渐变配色。

### 3.3 近似色检索

给 `search_blocks` 增加 `similar_to: <block_id>`，按 Oklab 距离与同形状分类返回近似方块。属于新增输入字段，需决策。

### 3.4 同系列可用形状

设计时最常见的问题是“某材料有没有楼梯/台阶/墙/栅栏/门/活板门”。可由注册表命名规律与 tags（`minecraft:stairs`、`minecraft:slabs`、`minecraft:walls` 等）推导系列成员，在 details / compare 中返回。与 D-050 冲突，需决策。

### 3.5 联系表可读性

格子标签由 `T03` 改为 `T03 snow_block`，模型只看图也能对应。可选增加 3×3 平铺墙面预览，用于判断贴图重复后的效果。

### 3.6 搜索输出精简

8 条候选约 8KB。`machine_fact_refs` 与全零的 `score_breakdown` 项可省略。

### 3.7 推荐状态中的派生属性

`recommended_state_id` 含楼梯 `shape=straight` 等派生属性。下游若按原版规则忽略派生属性则无影响；只返回设计相关属性会更干净。优先级低。

## 4. 需要 owner 决策的事项

| 事项 | 涉及 |
|---|---|
| details / compare / search 输出字段变化 | `schemas/mcp/` closed Schema、[`mcp-api.md`](mcp-api.md) |
| 系列分组与同系列形状 | 放开 D-050 |
| `similar_to` 检索参数 | D-053 输入契约 |
| 标注字段拆分与受控词表、重跑颜色标注 | provider 费用、annotation Schema |
| 建筑查询黄金集作为完成门 | [`product-scope.md`](product-scope.md)、[`quality-and-testing.md`](quality-and-testing.md) |

## 5. 建议顺序

1. 渲染修正（§2.1）与资格审核（§2.2），二者都需要重新构建 release。
2. 搜索召回与排序（§2.3），同时建立黄金集。
3. 详情精简（§2.4）。
4. P1 中优先做比较字段（§3.1）与同系列形状（§3.4）。

## 6. 下游接入说明（mc_architect 侧，供参考）

以下由 `mc_architect` 仓库自行决策，不属于 Blockpedia 的实现范围：

- 作为独立 MCP 服务注册，不改变 `mc_architect` 固定的八个生产工具。
- 在其建筑 Skill 中作为可选步骤：定下 brief 与预算后用于挑选主材料、查看联系表定调色，评审时用于寻找替代材料。
- 方块状态以 `mc_architect` 自己的 26.2 注册表为准；Blockpedia 预览图只用于选材，不作为实机视觉验收证据。
- 状态字符串 `minecraft:name[key=value,...]` 与其解析器兼容，无需转换。
