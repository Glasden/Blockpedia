# 状态策略与标准渲染设计

## 1. 范围、术语和关联文档

本文定义 Fabric exporter 如何从一个方块的全部合法 `BlockState` 选择最小 `VisualVariant`，以及如何在固定摄影棚中生成可复现图片。精确记录字段形状由 `schemas/exporter/` 下的真实 Schema 文件拥有；本文只定义选择/渲染行为。状态选择和 Minecraft 渲染只能由 exporter 执行；Python Studio 只验证 `export-variant.v1` 和 `render-metadata.v1` 结果。本文不改变注册表事实：所有合法状态仍必须进入 `states.jsonl`，R1 只为每个 block 选择唯一 default `BlockState` 作为 block-level visual representative；其余合法状态链接到该代表，或保留机器 skip/failure。

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

R1 状态策略必须同时满足以下不变量：

1. 目标版本为 Minecraft Java `26.2`，只处理 `minecraft` 命名空间的注册表方块。
2. 方块、合法状态、默认状态、形状/碰撞、透明/发光、支撑、方块实体、标签和版本是机器事实；LLM 和人工不能覆写或伪装这些事实。
3. 每个 `block_id` 只建立一个普通视觉代表，绝不跨方块 ID 合并实体。
4. 所有状态必须可追溯到同一 block 的代表，或有机器可读 skip/failure 并保持待审核。
5. R1 只使用固定 isolated context；不预建 block entity/NBT、任意流体、动画帧、组合邻接或通用 fixture 框架。
6. 渲染固定为 512×512、固定摄影棚和四视角；所有实际影响图片的输入进入版本和哈希。

`states.jsonl` 是完整状态真相，`variants.jsonl` 是 exporter 在 `SELECT_VARIANTS`/`RENDER_VARIANTS` 产生的策略结果。任何只在 `variants.jsonl` 出现的状态都是数据损坏；Python 不得生成新的代表状态。

## 3. 最小状态选择

R1 不读取外置 `state_policy.yaml`，不实现 override DSL、显著属性展开、方向折叠、邻接矩阵、pHash/IoU/alpha dedupe 或通用规则引擎。对每个按规范顺序处理的 `block_id`，exporter 读取运行时唯一 default `BlockState`，建立唯一普通 block-level representative，并把全部合法状态链接到该 representative；不修改或删除任何合法状态。代表状态不稳定可渲染时，保留 Block/State 并写机器 failure/skip，状态为 pending review。

Schema 中 required 的 policy/fixture version 只标识这套固定 R1 实现，不指向外置策略文件；R1 不提供用户可配置 override，也不因 `dedupe_policy_version` 执行图像去重。

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
    states = all_legal_states(block_id)
    representative = the unique runtime default BlockState
    emit every state with its runtime facts
    render representative once in the fixed isolated context
    if stable render succeeds: link every state to the one representative
    else: mark state mappings skipped and keep machine review pending
```

选择顺序仍属于 `EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS`；状态合法性、默认状态、属性和机器事实只来自 Minecraft runtime。所有状态仍保留在 `states.jsonl`，不得因代表策略删除或改写。

## 5. VisualVariant 边界

R1 不执行候选之间的合并或去重。每个 `block_id` 的唯一 default representative 生成一个 block-level variant；不同 `block_id` 永不合并。所有状态机器事实分别保留在 `states.jsonl`，代表链接不把一个状态事实推广为另一个状态事实。

## 6. 跳过和审核

无法稳定渲染不等于不存在。导出器必须保留 Block 和所有状态，并在 `variants.jsonl` 或 `failures.jsonl` 写入明确原因；精确 failure 字段和 reason 枚举以 [`schemas/exporter/export-failure.v1.json`](../schemas/exporter/export-failure.v1.json) 与 [`schemas/exporter/export-variant.v1.json`](../schemas/exporter/export-variant.v1.json) 为准。R1 的 skip 只由 exporter 产生并保持 pending，独立 workspace `skip-review.v1` 属于 R3 candidate-build 前置。

R1 的 `retry_count` 最多为 1，不要求必须重试；仅已观察的可恢复失败可重试一次。R1 不写人工审计字段，不产生已解决 skip。`excluded` 资格必须通过独立 `qualification-review.v1`，不能用 exporter record 替代。

R3 审核通过后 skip 仍计入注册表覆盖率，但不进入搜索视觉候选；“全部目标方块已登记”和“全部方块都有可搜索图片”是两个不同指标。

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

摄影棚坐标、观察对象中心、背景平面、背板材质、支撑块、光源方向/强度、阴影和曝光固定为 isolated context 的最小实现。实现不得读取用户当前世界的天气、时间、资源包或随机邻接来改变结果。资源 snapshot、相机、光照、背景/背板和支撑规则的哈希只写入 manifest；`render.json` 只写最小图片、视角、policy、fixture、tint 和 mask 语义，不重复环境或内容哈希。manifest/input signature 必须记录 OS、GPU、驱动、渲染后端和分辨率等完整环境；同一完整环境重复运行必须得到相同 PNG byte hash。Windows 对应阶段验证 canonical 机器字段、Schema、逻辑排序和构图规则；Linux 这些实际运行时/平台验证统一 deferred 到 R5，不声称 Linux 已通过；不同 GPU/驱动之间只要求 canonical 机器字段一致，不承诺 PNG byte hash 一致。

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

isolated context 使用固定中性背板和必要的中性支撑；支撑不进入对象蒙版。R1 不预建组合邻接或特殊对象夹具；普通 block model 无法在该 context 稳定渲染时写机器可读 skip/failure 并保持 pending review。

## 8. 动态、流体和方块实体边界

R1 不预建 block entity/NBT、任意流体、动画帧、组合邻接或通用 fixture 框架。此类对象如果不能在普通 block model 的 isolated context 中稳定渲染，写机器可读 skip/failure 并保持 pending review；不得退化为随手截图或占位图片。

## 9. 渲染流水线和自动重试

对每个 default representative 按以下顺序执行：

1. 校验代表状态合法、策略版本一致、夹具可构造。
2. 清空摄影棚中上一候选留下的对象和实体。
3. 在 isolated context 放置对象、固定背板和必要中性支撑。
4. 使用 Minecraft 26.2 普通模型链提交离屏渲染：`BlockModelResolver → BlockModelRenderState → SubmitNodeStorage → FeatureRenderDispatcher`；必须在 render thread 上将本次 scoped draw 的 color/depth output 显式路由到 Blaze3D `TextureTarget`，dispatcher 本身不接收 target，也不隐式绑定；在 `finally` 中恢复此前的 output routing 和资源状态，不能污染主 framebuffer；不得使用 raw OpenGL。
5. 输出四视角卡、对象蒙版和 `render.json`；完整渲染环境签名写入 manifest。
6. commit gate 验证尺寸、PNG 基础可读性、对象占比、出框、缺失纹理和文件集；外部 validator 复用同一次读取/解码完成 PNG 语义/质量和 artifact digest 复算。
7. 通过后再写 `variants.jsonl` 的 `selected` 记录；失败先写 exporter failure，再保持 pending review。

`retry_count` 只是最多 1 次的上限，不要求每次失败都重试；仅已观察的可恢复失败可用相同输入重试一次。重试后仍失败不得再调用渲染器，成功产物不得被失败尝试覆盖。

## 10. 验收条件

R1 只验证代表性普通 block model、全量状态链接、isolated 四视角渲染、失败 pending 语义和 fresh staging 原子提交；不建立 16 类回归矩阵或后续特殊对象夹具套件。

验收必须证明：

1. 全部合法状态都在 `states.jsonl`，默认状态合法且可追溯。
2. 每个 block 只有唯一 default representative，所有合法状态链接到该代表或有机器 skip/failure。
3. 不同 `block_id` 不合并；R1 不执行 phash、IoU、alpha 或通用去重。
4. R1 不展开显著属性或组合邻接；这些状态仍完整存在于机器状态导出中。
5. 每个成功图片均为 512×512、四视角、固定背景/支撑/相机/光照，三份 render artifact 哈希可复算。
6. 渲染失败最多重试一次，失败或跳过有机器可读原因并保持 pending review。
7. R1 不产生 block entity/NBT、任意流体、动画帧或组合邻接穷举。
8. 同一完整渲染环境重跑得到相同 `variant_id == block_id`、选择结果和 PNG byte hash；跨环境只比较 canonical 机器字段、Schema、逻辑排序和构图规则。

发布前的最终阻断条件、FTS、MCP 冒烟和原子切换见 [流水线、存储与发布](pipeline-storage-and-publishing.md)。
