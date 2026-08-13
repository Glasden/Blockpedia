# 状态策略与标准渲染设计

## 1. 范围、术语和关联文档

本文定义 Fabric exporter 如何从一个方块的全部合法 `BlockState` 选择 `VisualVariant`，以及如何在固定摄影棚中生成可复现图片。精确记录字段形状由 `schemas/exporter/` 下的真实 Schema 文件拥有；本文只定义选择/渲染行为和示例。状态选择和 Minecraft 渲染只能由 exporter 执行；Python Studio 只验证 `export-variant.v1` 和 `render-metadata.v1` 结果。本文不改变注册表事实：所有合法状态仍必须进入 `states.jsonl`，策略只决定哪些状态需要独立的视觉/用途代表。

规范性词汇 `MUST`、`MUST NOT`、`SHOULD`、`MAY` 按 RFC 2119 解释。默认 Fabric 客户端导出器可以被等价实现替换，但必须在 [`decisions.md`](decisions.md) 记录影响并取得所有者批准。

关联文档：

- [导出契约](export-contract.md)
- [数据与 Schema](data-and-schemas.md)
- [流水线、存储与发布](pipeline-storage-and-publishing.md)
- [路线图](roadmap.md)
- [冻结决策](decisions.md)
- [搜索与排序接口](search-and-ranking.md)
- [质量与测试接口](quality-and-testing.md)
- [安全与分发接口](security-and-distribution.md)

## 2. 设计目标和不变量

状态策略必须同时满足以下不变量：

1. 目标版本为 Minecraft Java `26.2`，只处理 `minecraft` 命名空间的注册表方块。
2. 方块、合法状态、默认状态、形状/碰撞、透明/发光、支撑、方块实体、标签和版本是机器事实；LLM 和人工不能覆写或伪装这些事实。
3. `VisualVariant` 只因稳定可见差异或用途显著差异独立建项；相同 `block_id` 内才允许合并，绝不跨方块 ID 合并实体。
4. 方向等价只保留规范朝向，同时明确记录可旋转性；方向造成非旋转等价外观时必须分别保留。
5. 图像/轮廓达到阈值且几何签名一致，才允许合并；合并必须保留所有被代表状态和行为差异。
6. 所有状态必须可追溯到一个或多个变体，或有机器可读且经审核的跳过原因。
7. 动态、流体、方块实体只通过固定版本化夹具代表，不穷举任意 NBT、容器内容、旗帜图案和动画帧。
8. 渲染固定为 512×512、固定摄影棚和四视角；所有影响图片的策略都进入版本和哈希。

`states.jsonl` 是完整状态真相，`variants.jsonl` 是 exporter 在 `SELECT_VARIANTS`/`RENDER_VARIANTS` 产生的策略结果。任何只在 `variants.jsonl` 出现的状态都是数据损坏；Python 不得生成新的代表状态。

## 3. 策略文件 `state_policy.yaml`

策略文件使用 `state-policy.v1`。它必须在导出开始前解析并做严格 Schema 校验；未知属性、未知 `block_id`、不存在的状态值和无效覆盖都必须使 `PREPARE` 或 `SELECT_VARIANTS` 失败，不能静默忽略。

规范结构如下：

```yaml
schema_version: state-policy.v1
minecraft_version: "26.2"
defaults:
  canonical_cardinal_direction: north
  canonical_axis: y
  boolean_visual_properties: [open, lit, enabled, extended, inverted]
  numeric_mode: min_max_mid
  adjacency_contexts: [isolated, straight, corner, cross]
  dedupe_policy_version: dedupe.v1
  render_policy_version: render.v1
overrides:
  minecraft:oak_door:
    keep_states:
      - "minecraft:oak_door[facing=north,half=lower,hinge=left,open=false,powered=false]"
      - "minecraft:oak_door[facing=north,half=lower,hinge=left,open=true,powered=false]"
    protect_properties: [half, open]
  minecraft:redstone_wire:
    contexts: [isolated_off, straight_off, cross_on]
  minecraft:snow:
    keep_all_values_of: [layers]
```

`canonical_cardinal_direction` 和 `canonical_axis` 只规定候选规范化方向，不修改状态事实；`boolean_visual_properties` 中存在于运行时的属性默认保留能稳定显示或直接改变用途的值；`numeric_mode: min_max_mid` 对有序数值取最小、最大，跨度大于 2 时增加确定性的中间值；`adjacency_contexts` 是标准邻接夹具，不是全部邻接位组合；`protect_properties` 禁止去重吞掉属性差异。

覆盖规则必须小而明确，并以 `policy_override_id` 写入选择结果。覆盖不能设置 `block_id`、默认状态、合法值、形状、碰撞或其它机器事实。`keep_states` 必须是该 block 的合法状态。

## 3.1 Canonical state serializer

`state_id` 就是 canonical state string，不使用另一个稳定 hash。serializer 必须由 exporter 明确定义并在导出与 Studio 验证器中复用同一规范：

```text
serialize(block_id, properties):
  validate block_id against the target runtime block registry
  validate every property name and value against that block's runtime legal set
  reject any name/value outside the allowed character set; do not escape or coerce
  names = sort(property_names, by UTF-8/ASCII code point ascending)
  if names is empty: return block_id
  return block_id + "[" + join(name + "=" + value, ",") + "]"
```

因此 `minecraft:oak_door` 的无属性状态不带 `[]`；有属性时不含空格，属性名按 UTF-8/ASCII codepoint 升序，值保持目标运行时的规范字符串。允许字符集必须在 Schema 中固定为 ASCII 标识字符（名称 `[a-z0-9_]+`，值 `[a-z0-9_.-]+`，必要的运行时枚举值必须先列入该集合）；不符合者直接拒绝。实现 **MUST NOT** 依赖 JVM `Map`、`toString()`、JSON object 或注册表迭代顺序。`state_id`、`canonical_state_id`、`represented_state_ids` 和所有状态引用在四份契约中都使用该完整 canonical string。

## 4. 候选选择算法

exporter 必须使用确定性顺序；以下伪代码是规范算法，改变排序或取值规则必须升级策略版本。Python Studio 不得实现其中任何选择或渲染步骤：

```text
for block_id in sorted(runtime_minecraft_block_ids):
    states = sorted(all_legal_states(block_id), key=canonical_state_string)
    classify every property using runtime values and policy
    candidates = expand_significant_values(states)
    candidates = normalize_rotation_equivalent_directions(candidates)
    candidates += required_adjacency_contexts(block_id)
    candidates += versioned_fixture_representatives(block_id)
    render candidates in deterministic order using Minecraft runtime
    group only within block_id and only when dedupe predicates pass
    choose canonical state (default first, then policy keep, then state_id)
    emit state -> variant mappings for every legal state
```

### 4.1 方向变换属性

`facing`、`axis`、`rotation` 等属性默认使用规范朝向：水平四向取 `north`，轴向取 `y`，离散旋转取 `0`。只有同时满足以下条件才可以省略其它方向：

- 其它方向在允许旋转变换后与规范方向的对象轮廓、碰撞、材质、透明/发光摘要等价；
- 方向没有造成用途或支撑语义的非旋转差异；
- 变体记录 `context.rotatable: true`、`canonical_orientation` 和被折叠方向集合。

若纹理、几何、邻接或用途使方向不等价，必须保留对应方向的独立变体，并将 `rotatable` 设为 `false` 或只对等价子集为 `true`。记录方向可旋转不等于允许从 `states.jsonl` 删除合法状态。

### 4.2 布尔、形态和数值属性

- `open`、`lit`、`enabled`、`extended`、`inverted` 等明显改变外观或用途的属性值必须分别保留。
- `half`、`type`、`shape`、`face` 等造成几何差异的值必须分别保留。
- `age`、`layers`、`bites`、`candles`、`pickles` 等有序属性默认取最小、最大和（跨度大于 2 时）中间值；雪层、蜡烛数量等对建筑使用有直接意义者通过覆盖保留全部值。
- `waterlogged` 等若渲染和用途均无可见差异，可以只保留一个视觉代表，但所有含水状态仍在 `states.jsonl` 和行为字段中保留；若含水改变渲染、支撑或搜索硬约束，必须分开。
- 任何属性差异只要带来稳定视觉差异或显著建筑用途差异，就必须独立成变体，即使名称相同。

### 4.3 邻接上下文

围墙、栅栏、玻璃板、铁栏杆和红石线等连接方块不得枚举所有邻接位组合。默认上下文为：

```text
isolated   无同类邻接
straight   直线连接
corner     直角连接
cross      十字连接
```

上下文是渲染夹具，不是新 `BlockState`。每个上下文必须写入 `context.fixture_id`、邻接 block/state 列表和 `fixture_version`。策略覆盖可以指定 `isolated_off`、`straight_off`、`cross_on` 等带行为含义的组合。

## 5. VisualVariant 的合并规则

### 5.1 合并前提

合并只在同一个 `block_id` 内进行，且以下条件全部满足：

1. 两候选具有相同 `minecraft_version`、策略版本、渲染策略、夹具版本和资源包哈希。
2. 64-bit `image_phash` 的 Hamming distance `<= 6`。
3. 对象蒙版 `silhouette_iou >= 0.98`；对象区域边界偏差不得超过 1 像素的策略容差。
4. `geometry_signature` 和 `collision_signature` 完全相同。
5. `alpha_ratio_delta <= 0.01`，透明/不透明分类相同。
6. 没有被 `protect_properties`、`keep_states` 或内置显著维度保护。

这些数值属于 `dedupe.v1`，不是代码中的隐式常量；阈值改变必须升级策略版本并重建。合并只比较同一 block 的候选；不同 block ID 即使图像完全相同也必须保留不同实体。

### 5.2 不可吞掉的差异

以下差异不得因为图片相似而被隐藏：

- `open`、`lit`、`enabled`、`extended` 等受保护的视觉/用途状态；
- 几何轮廓、碰撞、占用体积、支撑和可穿过性不同；
- 运行时透明/发光、方块实体或红石相关行为不同且该差异影响检索；
- 不同邻接上下文的连接结果；
- 不同 block ID。

图片相同但行为可安全共用视觉卡的状态可以进入同一个变体，但必须保存全部 `represented_state_ids`，在状态层分别保留行为事实，并在变体的 `behavior_by_state` 中按 `state_id` 保存差异；不能把一个状态事实推广给其它状态。

## 6. 跳过和审核

无法稳定渲染不等于不存在。导出器必须保留 Block 和所有状态，并在 `variants.jsonl` 或 `failures.jsonl` 写入明确原因。最小 skip reason 枚举为：

```text
MISSING_TEXTURE
EMPTY_RENDER
BACKGROUND_ONLY_RENDER
OBJECT_OFF_CANVAS
OBJECT_TOO_SMALL
FRAME_INCONSISTENT
ANIMATED_FIXTURE_UNSUPPORTED
FLUID_FIXTURE_UNSUPPORTED
BLOCK_ENTITY_FIXTURE_UNSUPPORTED
RENDERER_EXCEPTION
```

每个 skip 必须带统一审计字段 `target_id`、`minecraft_version`、`reviewer`、`reviewed_at`、`reason_code`、`note`、`evidence`、`source_version`，并额外带 `machine_failure_ref`。失败总共自动重试一次，第二次失败进入 `needs_review`；审核员只能通过声明式 `skip-review.v1` 确认跳过，不能编辑机器事实，也不能将不可读图片标为成功。`excluded` 资格则必须通过独立 `qualification-review.v1` 记录，不得把 skip 记录当资格覆盖。

审核通过后 skip 仍计入注册表覆盖率，但不进入搜索视觉候选；“全部目标方块已登记”和“全部方块都有可搜索图片”是两个不同指标。

## 7. 固定摄影棚和渲染参数

### 7.1 固定环境

`render.v1` 默认环境固定为：

```text
Minecraft Java 26.2
resource pack: vanilla（仅记录哈希，不复制资源）
shader: disabled
language: primary zh_cn, secondary en_us
world time: 6000
weather: clear
biome: minecraft:plains
entities/particles/UI: disabled
```

摄影棚坐标、观察对象中心、背景平面、背板材质、支撑块、光源方向/强度、阴影和曝光均由版本化 fixture 配置提供。实现不得读取用户当前世界的天气、时间、资源包或随机邻接来改变结果。资源包、相机、光照、背景/背板和支撑规则的哈希必须写入 manifest 和每个 `render.json`。manifest/input signature 必须记录 OS、GPU、驱动、渲染后端和分辨率等完整环境；同一完整环境重复运行必须得到相同 PNG byte hash。Windows 11 与 Linux x86_64 分别验证合法性、几何、构图和 Schema；不同 GPU/驱动之间只要求 canonical 机器字段一致，不承诺 PNG hash 一致。

### 7.2 四视角和构图

每张卡固定 512×512 RGBA PNG，四个视图占固定象限，短编号绘在卡底部固定区域。默认相机规范为：

```text
isometric: orthographic, yaw=45°, pitch=30°
front:     orthographic, yaw=0°,  pitch=0°
side:      orthographic, yaw=90°, pitch=0°
top:       orthographic, yaw=0°,  pitch=-90°
```

角度、正交缩放、象限边界和边距均属于 `camera.v1`；不得根据方块类别动态改变相机。对象必须在四视图中完整可见，并使用同一中心和尺度规则。对象过小、出框或只剩背景都算失败。

### 7.3 透明、附着和连接对象

- 透明/半透明对象后方使用固定中性深浅分区背板，背板不进入对象蒙版。
- 门、按钮、火把等附着对象使用不抢眼的固定支撑块；支撑块的颜色、轮廓和特征必须从对象区域排除。
- 栅栏、墙、玻璃板和红石线使用策略指定的标准邻接上下文。
- 草、树叶、水等受生物群系染色的对象设置 `tint_sensitive: true`，固定记录 `baseline_biome: minecraft:plains`；首版不为每个生物群系建立重复索引。

## 8. 动态、流体和方块实体夹具

`fixture.v1` 为每类特殊对象规定唯一的固定代表：

| 类型 | 默认夹具 | 禁止事项 |
|---|---|---|
| 流体 | 固定源块、固定 `tick`、无随机邻接 | 不枚举所有流体高度、流动路径或 NBT |
| 方块实体 | 空内容、固定朝向、固定关闭状态；只有用途显著且策略声明时增加一个固定开放状态 | 不枚举容器内容、随机 NBT、任意名称 |
| 旗帜/图案 | 版本化程序生成的固定图案 | 不枚举所有图案组合 |
| 动画纹理/动态方块 | 固定游戏 tick 和固定代表帧 | 不把每一帧当作状态或变体 |

夹具必须有 `fixture_id`、`fixture_version`、参数规范化摘要和可重放坐标。若固定夹具无法稳定得到可读图片，写机器可读 skip reason，不得退化为随手截图。

## 9. 渲染流水线和自动重试

对每个候选按以下顺序执行，并在阶段之间持久化结果：

1. 校验代表状态合法、策略版本一致、夹具可构造。
2. 清空摄影棚中上一候选留下的对象和实体。
3. 放置对象、必要支撑、背板和标准邻接上下文。
4. 固定 tick、相机、光照后连续取样，验证帧稳定性。
5. 输出四视角卡、对象蒙版和 `render.json`，并写入完整渲染环境签名。
6. 验证尺寸、PNG 解码、对象占比、出框、缺失纹理和哈希。
7. 通过后再写 `variants.jsonl` 的 `selected` 记录；失败先写 failure，再决定审核或跳过。

每个逻辑渲染项初次失败时只自动重试一次。重试使用相同 `input_signature` 并记录 `attempt: 2`；第二次失败不得再调用渲染器，成功产物不得被失败重试覆盖。

## 10. 验收条件

实现完成后，至少用完整方块、半砖、楼梯、门、活板门、栅栏、玻璃板、墙、火把、按钮、灯、作物、水、箱子、旗帜和红石线各一项做回归；每类覆盖一项有多个状态/邻接/夹具的对象。

验收必须证明：

1. 全部合法状态都在 `states.jsonl`，默认状态合法且可追溯。
2. 方向等价只保留规范朝向，并有 `rotatable` 和折叠状态记录；非等价方向不会被误合并。
3. 合并只发生在同一 `block_id`，且 phash、轮廓、几何、碰撞和透明阈值全部满足。
4. `open/lit/enabled`、明显几何、关键成长/数值阶段和标准邻接上下文的策略结果符合覆盖声明。
5. 每个成功图片均为 512×512、四视角、固定背景/支撑/相机/光照，哈希可复算。
6. 渲染失败只重试一次，失败或跳过有机器可读原因和审核状态。
7. 动态/流体/方块实体不产生 NBT、容器、旗帜或动画穷举。
8. 同一完整渲染环境重跑得到相同 `variant_id`、选择结果和 PNG byte hash；跨环境只比较 canonical 机器字段。策略、运行时或环境改变会产生新签名而不会覆盖旧成功产物。

发布前的最终阻断条件、FTS、MCP 冒烟和原子切换见 [流水线、存储与发布](pipeline-storage-and-publishing.md)。
