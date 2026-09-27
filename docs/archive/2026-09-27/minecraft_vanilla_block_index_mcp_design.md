# 原版 Minecraft 方块多模态检索 MCP 设计文档

- **文档版本**：1.0
- **日期**：2026-08-13
- **状态**：方案敲定稿
- **适用范围**：Minecraft Java Edition，单个固定游戏版本，原版方块与原版资源包

---

## 1. 项目目标

实现一套面向 Minecraft 建筑 Agent 的方块知识与检索系统。模型可以用自然语言描述需要的方块，例如：

> 黄色的扁片方块，用在屋檐底部，不要会产生红石信号的方块。

系统自动完成：

1. 将自然语言转换为结构化检索条件；
2. 从预先编制的原版方块索引中召回候选；
3. 生成包含候选方块多视角预览的联系表；
4. 由同一个多模态 LLM 对候选进行视觉重排和解释；
5. 通过 MCP 返回候选图片、准确方块 ID、推荐状态和注意事项。

本项目不是 Minecraft 百科，也不负责生成整栋建筑。它只解决一个明确问题：

> **把自然语言中的视觉、形状、用途和行为要求，稳定地映射到当前目标版本中真实存在的原版方块。**

---

## 2. 已确定的范围与原则

### 2.1 范围内

- Minecraft Java Edition；
- 每份索引绑定一个准确游戏版本；
- 只覆盖 `minecraft` 命名空间中的原版方块；
- 使用原版默认资源包；
- 方块及其具有明显视觉差异的 BlockState；
- 中文和英文名称、同义描述、视觉描述、建筑用途；
- 方块颜色、形状、透明度、发光、开关、方向、支撑等检索信息；
- OpenAI Responses API；
- OpenAI Chat Completions API；
- Anthropic Messages API；
- 所配置模型必须支持图片输入。

### 2.2 范围外

首版不处理：

- 模组方块；
- 第三方资源包；
- Bedrock Edition；
- 向量数据库或独立 Embedding 模型；
- 独立视觉模型；
- 模型微调；
- 多租户、团队权限和云端集群；
- 对复杂红石电路行为进行完整模拟；
- 把每一种方向组合都作为独立语义条目；
- 让 LLM 决定方块 ID、合法状态或几何事实。

### 2.3 核心设计原则

1. **机器事实由脚本和游戏运行时提取。** 例如方块 ID、合法状态、轮廓、碰撞、发光等级等，不交给 LLM 猜测。
2. **LLM 只负责语义。** 例如“扁片”“粗糙石材”“适合作为窗板”等自然语言标签和候选重排。
3. **只使用一个可切换的多模态 LLM 提供商。** 不额外部署 Embedding、CLIP、分类器或其他模型。
4. **不用向量数据库。** 使用 SQLite、FTS5 Trigram、颜色距离、几何条件和规则评分完成初步召回。
5. **人工只审核异常。** 正常条目自动通过，渲染失败、规则冲突和低置信度条目进入审核队列。
6. **保持可复现。** 人工修改存为覆盖规则，索引可以从原始导出包重新构建。
7. **保持单机化。** 首版由一个 Minecraft 客户端导出模组和一个本地 Python 应用组成，不引入 Redis、Celery、Kafka 或微服务。

---

## 3. 总体架构

```text
┌──────────────────────────────────────────────┐
│ Minecraft 原版客户端 + Block Index Exporter │
│                                              │
│ 注册表 / BlockState / 形状 / 行为 / 标准渲染 │
└──────────────────────┬───────────────────────┘
                       │ 导出目录
                       ▼
┌──────────────────────────────────────────────┐
│ Block Index Studio                           │
│ FastAPI + Jinja/HTMX + 内置任务队列          │
│                                              │
│ 导入 → 特征提取 → AI 标注 → 审核 → 发布      │
└──────────────┬───────────────────┬───────────┘
               │                   │
               ▼                   ▼
       SQLite + 本地图片       多模态 LLM API
               │
               ├───────────────┐
               ▼               ▼
        WebUI 管理界面      MCP Server
                               │
                               ▼
                        建筑 Agent / LLM 客户端
```

首版可以使用同一个 Python 项目提供两种启动方式：

```text
block-index web       # 索引编制 WebUI
block-index mcp       # MCP stdio 服务
```

两者直接读取同一份 SQLite 数据库和图片目录，不再增加中间服务。

---

# 第一部分：索引编制

## 4. 索引最终包含什么

索引分为三层。

### 4.1 方块层 Block

表示一个注册表方块，例如：

```text
minecraft:bamboo_trapdoor
minecraft:yellow_carpet
minecraft:light_weighted_pressure_plate
```

保存：

- `block_id`；
- 中文名称；
- 英文名称；
- 默认状态；
- 所有状态属性及合法值；
- 方块标签；
- 是否存在对应物品；
- 是否具有方块实体；
- 基本行为特征；
- 所属简单家族，例如木材、石材、玻璃、铜制品。

### 4.2 视觉变体层 VisualVariant

一个方块可能有多个明显不同的视觉形态，例如：

- 活板门关闭且水平；
- 活板门打开且竖直；
- 楼梯下半部；
- 楼梯上半部；
- 灯亮起；
- 灯熄灭；
- 栅栏孤立；
- 栅栏连接成直线或十字。

视觉变体保存：

- `variant_id`；
- 所属 `block_id`；
- 代表性 BlockState；
- 渲染上下文；
- 多视角预览图；
- 轮廓与碰撞形状摘要；
- 主色、副色和透明度；
- 几何分类；
- 自动推导标签；
- AI 语义标签；
- 人工覆盖信息；
- 发布状态。

### 4.3 检索语义层 Annotation

保存适合自然语言检索的内容：

- 中文同义词；
- 英文同义词；
- 简短视觉描述；
- 颜色词；
- 形状词；
- 材质观感；
- 常见建筑用途；
- 风格关联；
- 不适用场景；
- AI 置信度；
- 数据来源。

机器事实和 AI 标签必须分开保存，AI 不能覆盖机器事实。

---

## 5. 索引编制的完整流水线

```text
PREPARE
  ↓
EXPORT_REGISTRY
  ↓
SELECT_VARIANTS
  ↓
RENDER_VARIANTS
  ↓
EXTRACT_FEATURES
  ↓
AI_ANNOTATE
  ↓
VALIDATE
  ↓
HUMAN_REVIEW
  ↓
PUBLISH
```

每个阶段都可以暂停和恢复。每个条目都有独立状态：

```text
pending / running / succeeded / needs_review / failed
```

### 5.1 自动化、AI 与人工的预期边界

以下比例是首版目标，不是未经实测的保证：

| 工作内容 | 主要执行者 | 预期方式 |
|---|---|---|
| 方块注册表、合法状态、名称、标签 | Minecraft 运行时与脚本 | 100% 自动 |
| 代表状态选择、标准渲染、颜色和几何 | 模组与离线脚本 | 约 95% 以上自动，例外进入队列 |
| 同义词、视觉描述、用途和风格 | 多模态 LLM | 对全部视觉变体批量自动生成 |
| JSON 修复和一致性检查 | 脚本 + 同一 LLM 一次重试 | 自动 |
| 渲染异常、规则冲突、低置信度语义 | 人工 | 目标只接触约 3%～10% 的视觉变体 |
| 抽样质检 | 人工 | 默认抽查自动通过条目的约 5% |

从字段构成看，约 70%～80% 是确定性机器事实，约 20%～30% 是 AI 语义字段。人工不是第三套数据来源，而是对异常 AI 字段和特殊渲染策略做覆盖。

---

## 6. 阶段 0：准备固定的游戏环境

每份索引只绑定一个准确的 Minecraft 版本。开始索引前创建独立游戏目录，例如：

```text
runtime/
└── mc-<version>-vanilla/
```

固定以下条件：

- 目标 Minecraft 版本；
- 对应版本的 Fabric Loader 与 Fabric API；
- 仅安装自制的导出模组；
- 禁用第三方资源包；
- 禁用 Shader；
- 固定语言为 `zh_cn`，同时读取 `en_us`；
- 固定分辨率、FOV、GUI 缩放和渲染距离；
- 固定游戏时间、天气和测试生物群系；
- 使用独立的本地测试世界。

生成环境清单：

```json
{
  "minecraft_version": "<target-version>",
  "loader_version": "<loader-version>",
  "resource_pack": "vanilla",
  "language_primary": "zh_cn",
  "language_secondary": "en_us",
  "exporter_version": "1.0.0",
  "state_policy_version": "1.0.0"
}
```

该清单成为索引版本的一部分。游戏版本变化后生成新索引，不在旧数据库上直接覆盖。

---

## 7. 自制客户端模组：Block Index Exporter

### 7.1 为什么需要自制模组

只解析客户端 JAR 中的 PNG 和 JSON，无法可靠得到最终状态对应的真实显示结果。方块外观由注册表、BlockState、模型、纹理、旋转、染色、相邻方块和部分特殊渲染共同决定。

因此首版实现一个仅客户端使用的 Fabric 模组，在资源加载完成后直接从游戏运行时导出数据，并使用真实 Minecraft 渲染器生成标准预览。

### 7.2 模组职责

模组只做四件事：

1. 枚举原版方块及全部合法状态；
2. 在受控世界中测量状态的形状与基本行为；
3. 选择需要渲染的代表性视觉变体；
4. 生成标准化多视角图片和 JSONL 数据。

模组不负责：

- 调用 LLM；
- 生成语义标签；
- 提供 WebUI；
- 提供 MCP；
- 管理数据库。

这样模组体积和维护范围保持较小。

### 7.3 启动方式

提供一个简单的模组内界面或命令：

```text
/blockindex export
```

可配置：

```text
输出目录
是否覆盖已有导出
从第几个方块继续
是否只导出指定 block_id
是否开启调试视图
```

执行时在屏幕显示：

```text
当前阶段：RENDER_VARIANTS
当前方块：minecraft:bamboo_trapdoor
完成：428 / 916
失败：2
```

数字仅表示当前版本运行时结果，不在代码中硬编码。

### 7.4 注册表与状态导出

对 `minecraft` 命名空间中的所有方块导出：

```json
{
  "block_id": "minecraft:bamboo_trapdoor",
  "translation_key": "block.minecraft.bamboo_trapdoor",
  "name_zh_cn": "竹活板门",
  "name_en_us": "Bamboo Trapdoor",
  "default_state": "minecraft:bamboo_trapdoor[facing=north,half=bottom,open=false,powered=false,waterlogged=false]",
  "properties": {
    "facing": ["north", "east", "south", "west"],
    "half": ["top", "bottom"],
    "open": ["false", "true"],
    "powered": ["false", "true"],
    "waterlogged": ["false", "true"]
  },
  "has_item": true,
  "has_block_entity": false,
  "tags": []
}
```

输出文件采用 JSONL，避免一次把全部状态保存在内存中：

```text
export/
├── manifest.json
├── blocks.jsonl
├── states.jsonl
├── variants.jsonl
├── renders/
├── failures.jsonl
└── exporter.log
```

### 7.5 基本行为探测

索引只记录与选材和放置有关的有限行为，不尝试模拟完整游戏机制。

对每个代表状态自动探测：

- 是否为完整立方体；
- 视觉轮廓；
- 碰撞轮廓；
- 占用体积；
- 是否可穿过；
- 是否透明或半透明；
- 发光等级；
- 是否可含水；
- 是否存在 `open`、`lit`、`powered`、`facing`、`axis`、`half` 等属性；
- 是否需要邻接或支撑；
- 是否存在方块实体；
- 是否属于明显的红石相关方块。

支撑要求不只靠类名判断，而是在测试世界中尝试六种支撑条件：

```text
下方支撑
上方支撑
北侧支撑
南侧支撑
东侧支撑
西侧支撑
无支撑
```

记录状态能否稳定存在。结果只用于检索提示和后续校验，不作为完整物理证明。

---

## 8. 视觉变体选择

### 8.1 不直接渲染所有状态组合

BlockState 属性可能形成大量组合。全部渲染会造成大量重复图片，因此在渲染前应用一份较小的状态策略文件：

```text
state_policy.yaml
```

策略按照属性类型处理。

### 8.2 属性分类

#### A. 方向变换属性

例如：

```text
facing
axis
rotation
```

通常只保留一个规范方向，例如朝北或 Y 轴，并记录它可以旋转。除非不同方向会导致非旋转等价的模型，否则不建立重复语义条目。

#### B. 明显改变形状或外观的布尔属性

例如：

```text
open
lit
enabled
extended
inverted
```

通常保留两种状态。

#### C. 上下或形态属性

例如：

```text
half
type
shape
face
```

保留所有会产生明显几何差异的值。

#### D. 数值阶段属性

例如：

```text
age
layers
bites
candles
pickles
```

默认保留：

- 最小值；
- 最大值；
- 当跨度较大时保留一个中间值。

对于雪层、蜡烛数量等对建筑使用有直接意义的属性，可以在覆盖规则中指定保留全部值。

#### E. 邻接属性

例如围墙、栅栏、玻璃板和红石线的连接属性，不枚举全部位组合，只建立标准上下文：

```text
isolated
straight
corner
cross
```

#### F. 主要不影响视觉的属性

例如多数方块的 `waterlogged`，只保留一个视觉代表状态，但在行为字段中保留其可含水信息。

### 8.3 小型人工覆盖表

原版范围有限，允许维护一个小型覆盖文件处理特殊方块：

```yaml
minecraft:oak_door:
  keep:
    - closed_bottom
    - open_bottom

minecraft:redstone_wire:
  contexts:
    - isolated_off
    - straight_off
    - cross_on

minecraft:snow:
  keep_all_values_of:
    - layers
```

覆盖表应保持“小而明确”，只处理通用策略无法正确表达的原版例外。

### 8.4 自动去重

候选状态渲染后，按以下信息计算签名：

```text
图像感知哈希
轮廓签名
碰撞签名
透明度摘要
代表状态属性
```

同一方块中视觉和形状都近似相同的状态合并为一个视觉变体，并保留其代表的状态集合。

---

## 9. 标准化渲染

### 9.1 测试场景

模组创建一个专用本地测试世界或固定坐标的摄影棚区域：

- 空旷背景；
- 固定正午；
- 固定晴天；
- 固定亮度；
- 固定 Plains 生物群系作为染色基线；
- 无粒子、无实体、无 UI 干扰；
- 固定相机参数；
- 每次只显示一个视觉变体及必要支撑方块。

对草、树叶、水等受生物群系影响的对象记录：

```text
tint_sensitive = true
baseline_biome = plains
```

首版不为每个生物群系重复建索引。

### 9.2 每个变体的预览卡

每个视觉变体输出一张统一尺寸的 PNG 卡片，包含：

```text
左上：等距视图
右上：正视图
左下：侧视图
右下：顶视图
```

建议尺寸：

```text
512 × 512 PNG
```

图片底部仅绘制短编号，例如：

```text
V00428
```

不要在图片中绘制过多小字。详细 ID 由 JSON 映射，避免多模态模型因为小字体而读错方块名称。

### 9.3 透明与连接方块

- 透明方块在背后放置中性深浅分区背板；
- 栅栏、墙和玻璃板根据变体上下文生成邻接方块；
- 门、按钮、火把等附着方块使用不抢眼的中性支撑块；
- 支撑块不计入颜色和形状提取区域；
- 必要时同时保存对象蒙版，用于离线颜色分析。

### 9.4 渲染失败处理

检测以下异常：

- 图片全透明或全背景；
- 对象超出画面；
- 对象占画面比例过小；
- 紫黑缺失纹理；
- 截图文件缺失；
- 连续多次帧结果不一致。

自动重试一次，仍失败则写入 `failures.jsonl` 并进入人工审核。

---

## 10. 离线自动化特征提取

这一步由 Python 脚本完成，不调用 LLM。

### 10.1 颜色特征

基于对象蒙版和不同视角分别计算：

- 主色；
- 副色；
- 颜色占比；
- 平均亮度；
- 平均饱和度；
- Lab/Oklab 颜色直方图；
- 顶面主色；
- 侧面主色；
- 透明像素比例。

检索时使用感知颜色空间中的距离，而不是直接使用 RGB 欧氏距离。

### 10.2 几何特征

从游戏导出的 VoxelShape 和模型边界计算：

```text
width
height
depth
occupied_volume
surface_ratio
thinnest_axis
component_count
is_full_cube
is_horizontal_sheet
is_vertical_sheet
is_slab_like
is_stair_like
is_column_like
is_cross_plane
is_fence_like
has_large_hole
```

“扁片”首先由几何规则识别，而不是由 AI 仅凭图片猜测。

### 10.3 纹理与视觉特征

只提取简单、可解释的指标：

- 边缘密度；
- 图案方向性；
- 明显网格或条纹；
- 视觉复杂度；
- 是否有动画纹理；
- 是否有明显发光区域。

不引入单独的视觉向量模型。

### 10.4 自动标签

根据确定性特征生成第一批不可变标签：

```text
shape:horizontal_thin_sheet
shape:vertical_pane
behavior:openable
behavior:lightable
behavior:waterloggable
behavior:redstone_related
visual:transparent
visual:emissive
support:below
```

这些标签的来源标记为：

```text
source = deterministic
```

---

## 11. 多模态 LLM 辅助标注

### 11.1 LLM 负责什么

LLM 只生成以下语义字段：

- 中文自然语言同义词；
- 英文同义词；
- 视觉描述；
- 人类常用颜色词；
- 人类常用形状词；
- 材质观感；
- 建筑用途；
- 风格关联；
- 不适用场景；
- 语义置信度。

例如：

```json
{
  "variant_id": "V00428",
  "synonyms_zh": ["竹制薄板", "黄褐色格栅板", "竹木扁片"],
  "color_terms": ["黄褐色", "浅棕色", "竹黄色"],
  "shape_terms": ["薄片", "格栅板", "可翻转面板"],
  "material_impression": ["竹材", "轻质木材"],
  "building_roles": ["窗板", "屋檐装饰", "货架", "家具面板"],
  "style_tags": ["东亚", "乡村", "热带"],
  "avoid_for": ["承重墙", "完全密封墙面"],
  "summary": "黄褐色竹制格栅薄板，可水平或竖直使用。",
  "confidence": 0.88
}
```

### 11.2 联系表批处理

不为每个方块单独发送一次请求。脚本将 12 个左右的视觉变体组合成一张联系表：

```text
A1 A2 A3 A4
B1 B2 B3 B4
C1 C2 C3 C4
```

每个格子只显示：

- 视觉变体预览；
- 短编号。

请求中另外附带紧凑的机器元数据：

```json
[
  {
    "tile": "A1",
    "variant_id": "V00428",
    "block_id": "minecraft:bamboo_trapdoor",
    "name_zh": "竹活板门",
    "machine_shape": ["horizontal_thin_sheet", "openable"],
    "dominant_colors": ["#C8A85A", "#765A2D"]
  }
]
```

联系表优先按方块家族或相近形状分组，使模型可以做相对比较。

### 11.3 输出约束

提示词要求模型：

1. 只为给出的 `variant_id` 生成结果；
2. 不修改 `block_id` 和机器特征；
3. 每个编号必须恰好返回一次；
4. 只能使用受控用途和风格词表中的值；
5. 不能确定时降低 `confidence`，不得编造游戏机制；
6. 只输出符合 JSON Schema 的 JSON。

### 11.4 校验与重试

每次返回后执行：

- JSON Schema 校验；
- 编号完整性校验；
- 枚举标签校验；
- 重复条目校验；
- 机器事实冲突校验；
- 描述长度校验。

失败时将错误信息附在修复提示中重试一次。第二次仍失败则进入人工审核，不进行无限重试。

### 11.5 置信度与冲突规则

置信度门控：

```text
confidence >= 0.80：自动通过，但可进入随机抽样
0.65 <= confidence < 0.80：普通审核队列
confidence < 0.65：高优先级审核队列
```

除置信度外，以下情况也自动进入审核队列：

- AI 未返回有效置信度；
- AI 声称“完整方块”，但机器形状是薄片；
- AI 声称“透明”，但透明度为零；
- AI 生成不在受控词表内的大量用途标签；
- AI 描述与方块名称或图片明显冲突；
- 同一家族中的标签出现异常离群；
- 请求经过一次修复仍不符合 Schema。

AI 标签始终标记：

```text
source = llm
model_id = <configured-model>
prompt_version = <prompt-version>
verified = false | true
```

---

## 12. 人工介入方法

### 12.1 人工不做什么

人工不需要：

- 逐个录入所有方块；
- 手工填写全部合法状态；
- 手工计算颜色；
- 手工判断碰撞形状；
- 手工制作每张预览图；
- 审核全部正常条目。

### 12.2 人工处理的任务

人工只处理四类队列：

1. **渲染异常**：缺失纹理、裁切错误、透明对象不可见；
2. **机器与 AI 冲突**：几何、透明度、行为描述矛盾；
3. **低置信度语义**：建筑用途或形状描述不稳定；
4. **抽样质检**：从自动通过条目中随机抽取少量样本。

### 12.3 审核操作

审核员可以：

- 接受 AI 结果；
- 编辑同义词、用途和风格标签；
- 删除错误标签；
- 标记“不需要作为建筑候选”；
- 修改状态策略并要求重新渲染；
- 要求 LLM 重新分析；
- 将当前修改应用到同一家族；
- 将任务暂时跳过并附加备注。

### 12.4 人工修改的保存方式

人工不直接永久修改生成字段，而是产生覆盖文件：

```yaml
variant_overrides:
  V00428:
    add_building_roles:
      - roof_detail
    remove_building_roles:
      - structural_wall
    summary_zh: "黄褐色竹制格栅薄板，适合作为窗板和屋檐细节。"
    reviewed_by: "local-user"
```

每次重新构建索引时，在自动结果之后应用覆盖文件，从而保持结果可复现。

---

## 13. 索引发布

发布前执行完整性检查：

- 每个原版方块都有一条 Block 记录；
- 每个 Block 至少有一个视觉变体，或有明确的跳过原因；
- 所有发布变体都有有效图片；
- 所有机器字段通过 Schema；
- 所有 AI 字段通过 Schema；
- 未解决的高优先级审核任务为零；
- FTS5 全文索引构建成功；
- 搜索冒烟测试通过。

发布产物：

```text
published/
└── <minecraft-version>/
    ├── index.sqlite3
    ├── manifest.json
    ├── previews/
    ├── contact-sheets/
    ├── manual_overrides.yaml
    └── quality_report.json
```

切换当前版本时使用原子替换：

```text
current -> published/<minecraft-version>
```

避免 MCP 在发布过程中读取半成品数据库。

---

## 14. 增量执行和缓存

即使原版范围不大，也应避免失败后全部重做。

每个视觉变体计算：

```text
state_signature
render_signature
image_hash
feature_extractor_version
prompt_version
model_id
```

AI 标注缓存键：

```text
image_hash
+ machine_metadata_hash
+ prompt_version
+ model_id
```

只有上述内容变化时才重新调用 LLM。暂停和重启应用后，任务从 SQLite 中的最后状态继续。

---

## 15. Token 使用策略

本设计不使用 Embedding API，因此 Token 只来自两类任务：

1. 离线语义标注；
2. 在线自然语言解析和视觉重排。

### 15.1 离线估算公式

设：

```text
N = 最终视觉变体数量
B = 每张联系表包含的变体数量，默认 12
R = 重试和异常系数，建议按 1.10～1.20 预算
```

则 AI 请求数约为：

```text
ceil(N / B) × R
```

实际视觉 Token 由提供商、图片尺寸和 detail 配置决定。WebUI 直接记录 API 返回的输入、输出和缓存 Token，不在代码中使用固定价格推算。

### 15.2 在线查询预算

一次完整查询默认包含：

```text
调用 1：自然语言 → QuerySpec
本地：SQL/FTS5 Trigram/颜色/形状召回
调用 2：候选联系表 → 多模态重排
```

当初步结果具有明显唯一答案时，可以跳过第二次 LLM 重排，但仍返回候选图供 Agent 或玩家确认。

### 15.3 控制手段

- 每张联系表放 8～16 个变体；
- 图片统一缩放，不上传原始全分辨率截图；
- 机器元数据只传递检索相关字段；
- 每次最多重试一次；
- 使用响应缓存键；
- WebUI 可设置并发数和单次最大输出；
- 记录每个任务的 Token，不只记录全局总量。

---

# 第二部分：MCP 顶层设计

## 16. MCP 的职责

MCP Server 只负责查询已经发布的索引，不负责重新编制索引。

首版提供四个工具：

```text
index_info
search_blocks
get_block_details
compare_blocks
```

不提供任意 SQL、不提供文件写入，也不允许 Agent 修改索引。

### 16.1 传输方式

- 本地使用默认 `stdio`；
- 需要供局域网或 Web Agent 调用时，可选 Streamable HTTP；
- 首版 WebUI 与 MCP 可以运行在同一台机器；
- Streamable HTTP 模式默认只绑定 `127.0.0.1`，远程部署不属于首版范围。

---

## 17. 主工具：search_blocks

### 17.1 输入

最小输入只有自然语言：

```json
{
  "query": "黄色的扁片方块"
}
```

可选上下文：

```json
{
  "query": "黄褐色薄片，用在日式屋檐底部，不要红石组件",
  "limit": 8,
  "context": {
    "build_role": "roof_detail",
    "orientation": "horizontal",
    "exclude_behaviors": ["redstone_related"]
  }
}
```

`context` 是增强项，主流程不能要求调用方必须懂内部标签。

### 17.2 自然语言解析

使用当前配置的同一个多模态 LLM，将自然语言转换为 QuerySpec。此调用可以只有文本输入，但模型本身必须支持多模态。

```json
{
  "color": {
    "terms": ["yellow", "yellow_brown"],
    "target_hex": "#D6B34A",
    "importance": 0.9
  },
  "shape": {
    "terms": ["thin_sheet"],
    "orientation": "horizontal",
    "importance": 1.0
  },
  "roles": ["roof_detail"],
  "require": [],
  "exclude": ["redstone_related"],
  "keywords": ["日式", "屋檐"],
  "ambiguities": []
}
```

QuerySpec 使用稳定的内部词表。LLM 不直接返回方块 ID。

### 17.3 本地候选召回

按照以下顺序执行：

#### 第一步：硬过滤

- 游戏版本；
- 是否为已发布条目；
- 明确排除的行为；
- 明确指定的方向或形状；
- 是否允许作为建筑候选。

#### 第二步：多路评分

使用可解释评分，不训练排序模型：

```text
形状匹配
颜色距离
FTS5 Trigram 名称与同义词匹配
建筑用途匹配
风格词匹配
行为匹配
```

建议初始权重：

```text
显式形状：0.35
显式颜色：0.30
用途语义：0.15
名称/同义词：0.10
风格：0.05
行为：0.05
```

若用户没有提及某个维度，将其权重重新分配给已出现的维度。

先取前 24 个候选，再做简单多样性去重：同一方块家族默认最多保留 2 个近似候选。

### 17.4 候选联系表

从初步候选中选出 8～12 个，生成一张带编号的图片：

```text
A1 A2 A3 A4
B1 B2 B3 B4
C1 C2 C3 C4
```

每格包含多视角预览和短编号。图片旁的机器元数据通过请求文本传给 LLM。

### 17.5 多模态重排

将以下内容发送给多模态 LLM：

- 用户原始描述；
- QuerySpec；
- 候选联系表；
- 候选的机器事实；
- 候选的 AI 语义标签。

模型只返回候选编号排序：

```json
{
  "ranking": [
    {
      "candidate_id": "A3",
      "fit": 0.93,
      "reason": "黄褐色、水平薄片，适合作为木质屋檐装饰。"
    },
    {
      "candidate_id": "B1",
      "fit": 0.82,
      "reason": "颜色和厚度匹配，但需要下方支撑。"
    }
  ],
  "needs_user_choice": true,
  "ambiguity": "用户没有说明希望方块是木质、织物还是金属。"
}
```

模型不能新增候选，也不能改写候选的 `block_id`。

### 17.6 MCP 输出

工具同时返回：

1. 简短文本说明；
2. 候选联系表图片；
3. 结构化 JSON。

结构化结果：

```json
{
  "search_id": "S-20260813-0001",
  "minecraft_version": "<target-version>",
  "query": "黄色的扁片方块",
  "parsed_query": {
    "colors": ["yellow"],
    "shapes": ["thin_sheet"],
    "orientation": "unspecified"
  },
  "needs_user_choice": true,
  "ambiguity": "横向和竖向薄片都符合描述。",
  "candidates": [
    {
      "candidate_id": "A1",
      "variant_id": "V00182",
      "block_id": "minecraft:yellow_carpet",
      "display_name": "黄色地毯",
      "recommended_state": "minecraft:yellow_carpet",
      "score": 0.91,
      "reason": "纯黄色且非常扁平，适合覆盖水平表面。",
      "warnings": ["requires_support_below"]
    },
    {
      "candidate_id": "A2",
      "variant_id": "V00428",
      "block_id": "minecraft:bamboo_trapdoor",
      "display_name": "竹活板门",
      "recommended_state": "minecraft:bamboo_trapdoor[half=bottom,open=false,facing=north,waterlogged=false]",
      "score": 0.86,
      "reason": "黄褐色格栅薄板，可作为屋檐或窗板。",
      "warnings": ["openable", "directional"]
    }
  ]
}
```

候选图的编号与结构化结果中的 `candidate_id` 一一对应。

---

## 18. 其他 MCP 工具

### 18.1 index_info

返回：

- 当前索引的 Minecraft 版本；
- 索引发布时间；
- 方块数量；
- 视觉变体数量；
- Prompt 版本；
- 是否通过质量检查。

### 18.2 get_block_details

输入：

```json
{
  "block_id": "minecraft:bamboo_trapdoor"
}
```

返回：

- 所有主要视觉变体；
- 状态属性；
- 推荐状态；
- 形状和行为；
- 建筑用途；
- 预览图片；
- 常见替代方块。

### 18.3 compare_blocks

输入 2～6 个候选：

```json
{
  "block_ids": [
    "minecraft:yellow_carpet",
    "minecraft:bamboo_trapdoor",
    "minecraft:light_weighted_pressure_plate"
  ],
  "context": "用于屋檐底部"
}
```

输出一张对比联系表，以及颜色、形状、行为和用途差异。

---

## 19. MCP 错误处理

工具执行错误返回可操作信息：

```json
{
  "isError": true,
  "error_code": "INDEX_NOT_PUBLISHED",
  "message": "目标版本的索引尚未发布，请先在 Block Index Studio 完成索引编制。"
}
```

常见错误：

```text
INDEX_NOT_FOUND
INDEX_NOT_PUBLISHED
PROVIDER_NOT_CONFIGURED
PROVIDER_RATE_LIMITED
QUERY_PARSE_FAILED
NO_CANDIDATES
IMAGE_RENDER_FAILED
INVALID_BLOCK_ID
```

当 LLM 暂时不可用时，可以降级为本地召回结果，并设置：

```text
reranked_by_llm = false
```

MCP 不应因为重排失败而完全拒绝返回已有的确定性候选。

---

# 第三部分：索引编制 WebUI

## 20. WebUI 定位

WebUI 是本地索引工作台，不是面向终端玩家的产品界面。目标是：

- 配置多模态 LLM；
- 导入 Minecraft 模组导出包；
- 启动、暂停和恢复索引编制；
- 查看进度、错误和 Token；
- 处理人工审核任务；
- 测试最终检索效果。

不做复杂用户系统、工作区、审批流或团队协作。

---

## 21. 技术实现

推荐：

```text
后端：Python + FastAPI
页面：Jinja2 + HTMX + 少量原生 JavaScript
数据库：SQLite
图片：本地文件系统
任务：进程内持久化任务队列
样式：轻量 CSS 组件库或自制样式
```

选择 Jinja2/HTMX 而不是大型 SPA，原因是页面数量少、交互简单，且不需要单独维护 Node 前端工程。

默认监听：

```text
127.0.0.1:8765
```

---

## 22. 页面设计

### 22.1 首页 / 当前项目

显示：

```text
目标版本
当前索引状态
上次运行时间
当前阶段
总体完成率
待审核数量
失败数量
累计请求数
累计输入/输出 Token
```

主要按钮：

```text
新建索引
继续运行
暂停
进入审核
测试搜索
```

### 22.2 AI 提供商配置

支持三种适配器：

```text
OpenAI Responses
OpenAI Chat Completions
Anthropic Messages
```

配置字段：

```text
适配器类型
Base URL
API Key
Model ID
请求超时
最大并发，默认 2
图片 detail/质量选项
单次最大输出 Token
失败重试次数，固定上限 1
```

提供“测试连接”按钮。测试内容使用一张内置小图片，要求模型返回固定 JSON，以同时验证：

- 鉴权；
- 模型存在；
- 图片输入；
- JSON 输出；
- Token 统计字段。

API Key：

- 优先保存到操作系统 Keyring；
- 也允许只从环境变量读取；
- 不在日志中打印；
- 前端只显示掩码；
- 数据库只保存密钥引用，不保存明文。

### 22.3 新建索引

表单字段：

```text
索引名称
Minecraft 版本
模组导出目录
输出目录
AI 提供商配置
Prompt 版本
联系表大小，默认 12
自动通过置信度，默认 0.80
人工审核阈值，默认 0.65
抽样质检比例，默认 5%
```

创建后先执行导出包完整性检查，再允许开始。

### 22.4 运行监控

用一条阶段进度条展示：

```text
导入 ■■■■■■■■■■ 100%
渲染 ■■■■■■■□□□  72%
特征 □□□□□□□□□□   0%
AI   □□□□□□□□□□   0%
审核 □□□□□□□□□□   0%
发布 □□□□□□□□□□   0%
```

同时显示：

- 当前处理的方块；
- 每分钟处理数；
- 成功、重试、失败；
- 最近一次 LLM 请求；
- Token 变化；
- 最近生成的联系表；
- 最近 20 条日志。

允许：

```text
暂停
继续
安全取消
只重试失败项
跳过当前项并进入审核
```

任务状态在每个条目完成后写入 SQLite，应用崩溃后可以继续。

### 22.5 人工审核队列

页面采用左右布局：

```text
左侧：方块多视角图、联系表、同家族候选
右侧：机器事实、AI 标签、冲突说明、编辑表单
```

机器事实只读，并使用不同底色区分：

```text
机器事实：锁定
AI 字段：可编辑
人工覆盖：高优先级
```

审核按钮：

```text
接受
编辑并接受
重新调用 AI
应用到同家族
标记无需索引
重新渲染
跳过
```

快捷键：

```text
A 接受
E 编辑
R 重试 AI
G 应用到家族
S 跳过
```

默认只显示异常。审核员可以切换到“自动通过抽样”查看随机样本。

### 22.6 搜索测试台

提供一个与 MCP 完全相同的输入框：

```text
黄色的扁片方块，用于屋檐，不要红石组件
```

展示：

- QuerySpec；
- 初步召回；
- 候选联系表；
- LLM 重排；
- MCP 最终 JSON；
- 两次 LLM 调用的 Token；
- 总耗时。

这既用于调试，也用于建立人工黄金查询集。

### 22.7 设置与日志

只保留必要设置：

- 数据目录；
- 并发数；
- 日志等级；
- 自动备份；
- 清理未引用联系表；
- 导出质量报告。

不单独建设复杂日志平台。

---

## 23. 内置任务队列

不使用 Celery 和 Redis。SQLite 保存任务，Python 进程内启动有限数量 Worker。

任务表核心字段：

```text
id
type
subject_id
status
attempt
priority
started_at
finished_at
error_code
error_message
input_tokens
output_tokens
```

任务类型：

```text
IMPORT_BLOCK
EXTRACT_FEATURES
BUILD_CONTACT_SHEET
AI_ANNOTATE
AI_REPAIR
CREATE_REVIEW_TASK
PUBLISH_INDEX
```

重启时：

- `running` 且长时间无心跳的任务重置为 `pending`；
- 已成功任务不重复执行；
- 已失败任务需要用户手动或规则化重试。

---

## 24. AI 提供商适配层

定义统一接口：

```python
class MultimodalProvider:
    def test_connection(self) -> ProviderTestResult: ...

    def analyze(
        self,
        images: list[ImageInput],
        prompt: str,
        output_schema: dict,
        task_id: str,
    ) -> ProviderResult: ...
```

统一返回：

```json
{
  "text": "...",
  "parsed_json": {},
  "input_tokens": 0,
  "output_tokens": 0,
  "cached_tokens": 0,
  "request_id": "...",
  "model": "...",
  "latency_ms": 0
}
```

### 24.1 OpenAI Responses 适配器

- 文本转换为 `input_text`；
- 图片转换为 `input_image`；
- 支持 URL、Base64 Data URL 或文件引用；
- 从 Response 中提取文本和 usage；
- 优先使用提供商支持的结构化输出，否则使用 JSON 文本校验。

### 24.2 OpenAI Chat Completions 适配器

- 文本使用 `text` content part；
- 图片使用 `image_url` content part；
- 本地图片转换为 Base64 Data URL；
- 输出统一转换为 ProviderResult。

### 24.3 Anthropic Messages 适配器

- 图片作为 `image` content block；
- 支持 Base64、URL 或文件引用；
- 图片放在文本指令之前；
- 解析 content blocks 和 usage；
- 输出统一转换为 ProviderResult。

### 24.4 兼容策略

不把整个系统绑定到任何提供商专用 Agent 功能。适配层只依赖：

```text
图片输入
文本输入
文本输出
Token usage
```

结构化输出、文件 API 和 Prompt Cache 可作为增强能力，但不是索引流程正确性的必要条件。

---

## 25. 最小数据库设计

首版 SQLite 保持五张核心表即可。

### 25.1 blocks

```text
block_id PRIMARY KEY
name_zh
name_en
default_state
properties_json
tags_json
behavior_json
family
created_at
```

### 25.2 visual_variants

```text
variant_id PRIMARY KEY
block_id
canonical_state
represented_states_json
context_json
preview_path
mask_path
geometry_json
color_json
visual_json
machine_tags_json
ai_annotation_json
manual_override_json
confidence
status
```

### 25.3 review_tasks

```text
id PRIMARY KEY
variant_id
reason
severity
status
resolution_json
created_at
resolved_at
```

### 25.4 jobs

保存任务与 Token，结构见前文。

### 25.5 provider_profiles

```text
id PRIMARY KEY
adapter_type
base_url
model_id
secret_reference
settings_json
enabled
```

另外为以下字段建立 FTS5 虚表。中文和混合语言字段优先使用 `trigram` tokenizer，以支持不依赖分词器的子串匹配；若目标 Python/SQLite 构建不支持该 tokenizer，则在原版小数据集上降级为规范化字符串 `LIKE` 和标签表查询：

```text
name_zh
name_en
synonyms
summary
shape_terms
color_terms
building_roles
style_tags
```

不增加向量列。

---

## 26. 质量验证

### 26.1 导出器测试

选择一组代表性方块作为固定回归集：

```text
完整方块
半砖
楼梯
门
活板门
栅栏
玻璃板
墙
火把
按钮
灯
作物
水
箱子
旗帜
红石线
```

每次适配新游戏版本时，先通过代表集，再执行全量导出。

### 26.2 索引完整性测试

- 注册表覆盖率为 100%；
- 每个发布变体图片可读取；
- 状态字符串属于导出的合法状态；
- 所有 JSON 字段通过 Schema；
- FTS5 中每个变体至少存在一个名称或描述；
- 人工覆盖指向有效 `variant_id`。

### 26.3 搜索黄金集

首版人工建立约 100 条查询，覆盖：

```text
颜色
形状
颜色 + 形状
用途
风格
行为排除
方向
模糊描述
```

示例：

```text
黄色的扁片方块
透明的蓝色竖向薄片
深灰色石质楼梯
不会输出红石的金色薄片
适合作为中世纪窗板的深色木质方块
带格栅纹理的浅色面板
```

每条查询标注：

```text
最佳候选
可接受候选
不可接受候选
硬约束
```

建议首版验收目标：

- Top-5 中出现可接受候选的比例不低于 90%；
- 硬约束违反率低于 2%；
- 不存在的方块 ID 返回率为 0；
- 候选图与结构化候选编号一致率为 100%。

这些是项目目标，需要通过黄金集实测，不视为预先保证的结果。

---

## 27. 部署与目录结构

```text
minecraft-block-index/
├── exporter-mod/              # Fabric 客户端导出模组
├── block_index/
│   ├── cli.py
│   ├── web/
│   ├── mcp/
│   ├── providers/
│   ├── pipeline/
│   ├── search/
│   ├── schemas/
│   └── prompts/
├── state_policy.yaml
├── manual_overrides.yaml
├── data/
│   ├── exports/
│   ├── work/
│   └── published/
├── tests/
└── pyproject.toml
```

推荐的本地运行方式：

```text
1. 启动带导出模组的 Minecraft 客户端
2. 执行 /blockindex export
3. 启动 block-index web
4. 在 WebUI 导入导出目录并运行流水线
5. 完成人工审核并发布
6. 在 MCP 客户端中配置 block-index mcp
```

---

## 28. MVP 实施顺序

### 里程碑 1：确定性导出

完成：

- Fabric 导出模组；
- 方块和状态 JSONL；
- 代表状态策略；
- 标准预览卡；
- SQLite 导入；
- 颜色和形状特征。

此时可以不接 LLM，通过结构化过滤测试“黄色、扁片、透明”等查询。

### 里程碑 2：AI 标注与 WebUI

完成：

- 三类提供商适配器；
- 联系表生成；
- 批量语义标注；
- Schema 校验；
- 任务监控；
- 人工审核队列；
- Token 统计。

### 里程碑 3：MCP 查询

完成：

- QuerySpec 解析；
- SQLite/FTS5 Trigram/颜色/形状召回；
- 多模态重排；
- `search_blocks`；
- `get_block_details`；
- 结构化内容和图片返回。

### 里程碑 4：质量收敛

完成：

- 100 条黄金查询；
- 排序权重调整；
- 原版特殊状态覆盖；
- 发布报告；
- 文档和安装包。

---

## 29. 明确不做的过度工程化设计

首版不引入：

```text
微服务拆分
Kubernetes
消息队列
Redis
Celery
对象存储
向量数据库
多模型投票
在线训练
自动微调
复杂权限系统
多租户数据库
实时协同审核
云端资源调度
```

只有在以下事实被实测证明后才考虑扩展：

- 单机导出速度无法接受；
- SQLite 查询成为瓶颈；
- 多用户确实需要共享索引；
- 原版范围扩展到大型模组包；
- 联系表批处理仍产生不可接受的 LLM 成本。

---

## 30. 关键风险与应对

| 风险 | 应对方式 |
|---|---|
| Minecraft 渲染 API 随版本变化 | 每个目标版本固定一套 exporter 分支；优先调用高层原版渲染接口，不直接操作底层 OpenGL/Vulkan |
| BlockState 组合过多 | 状态策略、标准邻接上下文和图像/形状去重 |
| LLM 标签不稳定 | 受控词表、JSON Schema、一次修复、低置信度人工审核 |
| 多模态模型看错编号 | 使用大号短编号；结构化映射不依赖模型读取完整 block ID |
| API 成本失控 | 联系表批处理、缓存、并发限制、Token 仪表盘、最多一次重试 |
| 透明或动态方块截图失败 | 中性背板、对象蒙版、固定 tick、失败队列和小型特殊规则表 |
| 语义标签主观 | 区分机器事实与 AI 建议；允许人工覆盖；用黄金查询评估实际检索效果 |
| 提供商 API 差异 | 统一适配接口，只依赖图片输入、文本输出和 usage；增强能力不作为必要条件 |
| MCP 重排时 LLM 不可用 | 返回本地确定性候选并标记未经过 LLM 重排 |

---

## 31. 最终决策摘要

本项目首版采用以下最终形态：

```text
一个 Fabric 客户端导出模组
+ 一个 Python 本地应用
+ SQLite 和本地 PNG
+ 一个可配置的多模态 LLM
+ 一个只读 MCP Server
```

索引编制的职责分配为：

```text
游戏运行时和脚本：方块 ID、状态、形状、颜色、行为、图片
多模态 LLM：同义词、视觉描述、用途、风格和候选重排
人工：异常渲染、规则冲突、低置信度语义和抽样质检
```

在线检索采用：

```text
自然语言
→ LLM 生成 QuerySpec
→ SQLite/FTS5 Trigram/颜色/形状召回
→ 生成候选联系表
→ 多模态 LLM 重排
→ MCP 返回图片和结构化候选
```

该方案保留了多模态 LLM 对模糊视觉语言的理解能力，同时让所有关键游戏事实由 Minecraft 本身和确定性脚本提供。它不需要向量数据库、独立视觉模型或复杂分布式基础设施，适合作为原版方块知识系统的首个可用版本。

---

## 32. 技术依据

本设计基于以下公开技术能力：

- Fabric 的方块注册表、BlockState、模型与客户端渲染机制；
- Minecraft 方块实体中保存 BlockState 之外附加数据的机制；
- MCP 工具返回文本、图片和结构化内容的能力；
- MCP 的 stdio 与 Streamable HTTP 传输；
- OpenAI Responses 与 Chat Completions 的图片输入能力；
- Anthropic Messages 的图片 content block 能力。

具体实现时应以目标 Minecraft 版本、所使用映射和当时的提供商 API 文档为准。
