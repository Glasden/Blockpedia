# Fabric 导出与 Studio 导入边界

当前执行 [D-054](decisions.md) 与 [重构规格](export-build-refactor-spec.md)。旧 checksum/inventory gate 和 banner 专用补救流程是历史记录，不是新导出的前置条件。

## 导出职责

游戏内普通命令仍为 /blockindex export。Fabric 按 EXPORT_REGISTRY → SELECT_VARIANTS → RENDER_VARIANTS 枚举 minecraft 命名空间全部 Block 和合法状态，每个 Block 使用默认状态作为代表。失败保留机器记录和待审核原因，不伪装成人工 skip。

代表状态、方块 ID、几何、碰撞、行为和图片来自运行时。Python 不补造这些事实，不重选、不重渲染。状态策略、动画控制、材料判断和标准相机见 [状态与渲染](state-policy-and-rendering.md)。

普通渲染器使用 camera.v3 修正公共四视图的前后与俯仰方向，并保留 BannerBlock/WallBannerBlock 的中心缩放 0.72。旧 camera.v1/v2 包继续可读；专用 banner-repair 命令和 base-export 修补实现移除。

## 包和身份

export_id 是最终目录名，继续使用现有 export_UTC 时间身份和同秒后缀。导出写入私有 staging，完成引用/数量/文件集合、PNG 基础格式、持久化后一次原子提交；失败 staging 不作为正常输入。

包保留 manifest.json、blocks.jsonl、states.jsonl、variants.jsonl、failures.jsonl、exporter.log 和 renders/。新包不生成纯防篡改 checksums 清单或 Schema hash inventory。历史包带有这些字段/文件仍可读取，不比较其值来判断文件是否被修改。

每个可视变体继续以 block_id 为身份，preview.png、mask.png、render.json 位于由安全 block ID 直接推导的 renders 路径。已有实际参与内容身份的 digest 可以生成一次，不能演变成后续逐阶段未修改证明。

字段形状由 schemas/exporter 下真实 JSON Schema 拥有。当前版本、生产者和必要策略信息使用既有字段；纯 checksum/inventory 描述不再 required，不增加新 Schema ID 或迁移旧包。

## 一次输入验证

Studio 仅消费用户选择的 opaque source ref。输入检查包括：精确版本、非 staging、普通文件/安全路径、结构 Schema、合法引用和状态、完整 registry 数量、PNG 可读性与既有质量规则。路径不得逃逸，不能跟随链接或复制原版 JAR/纹理等禁止的资产。

同一读取结果用于复制和投影；每张 PNG 只在本次验证中解码一次。R1 独立验证工具、特征提取及查询图片复用同一底层 PNG 解析规则；质量规则仍归 validator。

检查不会重新枚举游戏注册表、选择状态或执行渲染，也不会复算历史 checksum、Schema hash、图片 digest 或文件身份。独立工具命令仍可用于检查现有包，但不会恢复旧防篡改门。

## 本地与公开内容

真实导出、原版资产和生成后的预览留在本地，不提交到公开仓库。仓库只包含代码、文档、Schema 和原创 fixture 生成器。新代码的编译/聚焦验证不能冒充实际 Minecraft 渲染或跨平台运行证据。
