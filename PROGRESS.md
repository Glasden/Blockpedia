# Blockpedia：建筑选材工作流与新 release

更新：2026-09-27。代码调查基线：`44233c2`。本文件是当前决定、实施顺序和进度的唯一维护入口；旧文档已归档，不再与本文并行维护。

**用户已于 2026-09-27 授权开始实施，D1 exporter 颜色修复、C 特征多 worker、E1 推荐名单降权与 B 详情瘦身／256 无损 WebP 已实现。** 其余接口和参数仍是后续实施目标，并非当前已有能力。256×256 无损 WebP 决定已获用户批准；Windows 导出至构建进展见第 9 节。用户随后明确授权正式发布并上传本机，两端发布及本机 MCP 验证已完成，见第 10 节。

## 1. 已确认决定与范围

| 决定 | 本轮落实方式 |
|---|---|
| 新建 release，不考虑旧数据兼容性 | 新导出、新 workspace、新标注、新 release；不实现旧 Schema 适配、数据迁移或跨导出合并。保留旧数据文件，不原地改写历史 release。 |
| 明显技术方块降权，具体名单由用户审核 | 第 2 节的 18 项名单已获用户批准；不直接删除方块或批量伪造资格审核记录。 |
| 确定 details 的 8KB 口径，合理处理状态枚举；最终 MCP 输出采用 256×256 无损 WebP | 第 3 节采用紧凑摘要、真实状态分页；仅最终图片响应缩采样与转码，处理过程保留原图。 |
| Studio 增加可选多 worker | 第 4 节限定为同一 run 内特征计算并行；复用已有 AI 批次并发。 |
| 搜索结果合并 16 色系列（用户于 2026-09-27 授权，取代 D-050 的“不分组”） | 第 5.1 节：查询不含颜色时，同一 16 色系列只保留一条并列出其余颜色；含颜色或精确名称/ID 时照常逐条列出。 |
| 保持简单，归档其他旧文档 | `docs/` 原有 20 份 Markdown 统一归档；不新建规格、决策、路线图三套重复记录。 |

继续保留的产品边界：Minecraft Java 26.2；Fabric 导出运行时事实与原图；Python Studio 处理离线数据；Node MCP 只有四个只读 stdio 工具，在线不调用 provider。SQLite 已提交业务行、release 目录、`current.json` 分别表示工作结果、构建结果和发布结果。单 data-root 一个可写 Studio；保留外部输入和路径校验、事务、原子提交、provider 批准与未知发送结果人工处理。已取消的本地防篡改检测、专用 banner repair 和双 release 发布门不恢复。

新工作区按完整流程生成标注与审核结果，不假定旧审核或少量补标能自动满足新构建。实际 provider 批次沿用 Studio 的批准流程；切换 `current.json` 须有用户明确发布授权。本次后续授权及执行结果见第 9、10 节。新格式上线时配套更新 MCP 与 Schema，不要求新版读取旧 release；不承诺跨格式回滚，旧数据继续留存。

## 2. 推荐降权名单：技术方块与蠹虫方块（用户已批准，2026-09-27）

以下全部使用 `minecraft:` 命名空间。18 项均已在当前 26.2 注册表中核实存在；只有 9 项在当前 release 有视觉候选。

| 类别 | block ID（省略 `minecraft:`） | 理由 |
|---|---|---|
| 空气、不可见及辅助用途 | `air`、`cave_air`、`void_air`、`barrier`、`light`、`structure_void` | 不适合作为普通可见建筑材料推荐 |
| 结构与测试工具 | `structure_block`、`jigsaw`、`test_block`、`test_instance_block` | 结构生成、编辑或测试用途 |
| 命令工具 | `command_block`、`chain_command_block`、`repeating_command_block` | 命令执行用途 |
| 活塞内部组成 | `moving_piston`、`piston_head` | 移动过程或活塞组成部分 |
| 传送相关 | `nether_portal`、`end_portal`、`end_gateway` | 特殊传送用途 |

用户追加确认：7 种蠹虫方块也按同一系数降权：`infested_stone`、`infested_cobblestone`、`infested_stone_bricks`、`infested_mossy_stone_bricks`、`infested_cracked_stone_bricks`、`infested_chiseled_stone_bricks`、`infested_deepslate`。这些 ID 已在本次 26.2 真实导出中核对，均有视觉候选；它们属于虫蚀风险类别，不冒充技术方块分类。

E2（用户于 2026-09-27 改定）：结构生成或生存难以获取的特殊方块也按 `0.25` 降权：`bedrock`、`spawner`、`trial_spawner`、`vault`、`end_portal_frame`、`reinforced_deepslate`、`petrified_oak_slab`、`suspicious_sand`、`suspicious_gravel`，均已在当前 release 核对有视觉候选。此前“不默认纳入 bedrock 等”的决定作废；精确 ID／官方全名查询仍可找到。

E2 行为提示（仅 Node MCP 搜索与详情，不降权、不改 qualification；用户决定本轮不做 Studio 分级）：优先依据 release 方块记录中的 registry tags 推导，tag 无法表达的行为才用固定 ID 补充，提示文字注明来源（`依据 tag …` 或 `本地方块规则`）。当前 export 没有 Java 方块类别事实，`requires_support` 全为 unknown，故“需要支撑”本轮不提示；铜氧化等其他自变化也未覆盖。

| 行为 | tags | 补充 ID（无对应 tag） |
|---|---|---|
| 受重力下落 | `sand`、`concrete_powders`、`anvil` | `gravel`、`suspicious_gravel`、`dragon_egg` |
| 会融化 | —（`ice` tag 含不融化的浮冰和蓝冰，不用） | `ice`、`frosted_ice`、`snow` |
| 火蔓延／熄灭 | `fire` | — |
| 生长或扩展 | `saplings`、`crops`、`cave_vines`、`bee_growables` | `bamboo`、`bamboo_sapling`、`sugar_cane`、`cactus`、`kelp`、`vine`、`twisting_vines`、`weeping_vines`、`chorus_flower`、`nether_wart`、`cocoa`、`budding_amethyst`、`red_mushroom`、`brown_mushroom` |

E1 实现：Node MCP 与 Studio 工作区查询均接入以下固定规则；不改动导出机器事实、AI 标注或 qualification。

- 名单采用一个固定 ID 集合，不做可配置规则引擎。用户已批准该名单，后续实现可接入默认排序。
- 泛用途检索中，对名单内已召回候选的相关性分乘 `0.25`，在 Top-N 截断前执行；候选的 `score`（B 阶段起合并原先恒等的 `local_score`／`final_score`）返回降权后的分数，理由说明名单降权，原匹配分项保持其匹配含义。
- 精确完整 block ID 或官方中英文全名查询保留名称优先，不施加这项泛用途惩罚；子串和同义词命中不算精确查询。并列按稳定 `variant_id` 排序。
- 既有 `excluded` 过滤仍有效；精确查询也不恢复 excluded 或没有预览的搜索候选。不为名单中缺少预览的条目伪造图像。
- 搜索和详情分别提供固定的技术／特殊用途提示、虫蚀风险提示、结构生成特殊方块提示；提示是本地推荐规则，不冒充新增运行时行为事实、AI 结论或人工 qualification review。`get_block_details` 仍能查询已登记的这些方块。
- 验收同时覆盖泛用途降权、精确名称可找到、非名单候选分数不被误改、名单内无视觉候选不被新增，以及 FTS/LIKE 两条路径。

验证：Studio 核心 28 项、Web/构建/MCP 集成 46 项通过，涵盖 0.25 系数、Top-N 前排序、精确 ID／官方名称豁免、子串和同义词不豁免、稳定排序、其他风险与非名单方块不误降，以及 excluded／无视觉候选不恢复。实现不新增依赖、规则引擎或配置账本；提交前审核与 Windows 交付结果见会话。

## 3. 方块详情与图片方案

### 3.1 默认摘要：严格小于 8000 字节

输入增加 `detail: "summary" | "states"`，默认 `summary`。计量公式固定为：

```js
Buffer.byteLength(JSON.stringify(structuredContent), 'utf8') < 8000
```

这是十进制 8KB，测完整成功输出，包括警告、数据与图片元数据（B 阶段起输出不再带版本、release 身份与哈希外壳，见第 3.6 节）；不计协议中重复的 TextContent、JSON-RPC 外壳或 ImageContent/base64。状态页不受此 8KB 上限约束。传输总字节和图片字节在验收时另报，不能把排除图片解释为整个响应小于 8KB。

摘要保留：官方名称、默认及 canonical 完整状态 ID、属性定义、tags、状态总数、代表状态的几何与碰撞、资格与警告、完整有效语义投影、图片映射。完整语义指已发布的语义字段，不包含 provider 原始响应、提示词或任务历史。当前一个 block 对应一个默认代表；几何和图片明确关联该 canonical 状态，不声称代表所有状态的形状。

摘要不返回 `states`、`represented_state_ids` 或逐状态 `state_behaviors`，也不枚举全部几何签名分组。精确状态 ID 保持完整，不删除 `shape=straight` 等派生属性。尚未实现的支撑事实可以从 MCP 输出省略，不能转换成 false 或“无需支撑”；其他已知行为事实与警告保留。

不得通过静默截断语义或丢弃警告凑够字节。以楼梯、活板门为硬断言样本，并扫描整个新 release 的摘要尺寸；若完整有效语义导致其他样本超限，在发布前调整字段边界或标注生成约束并复验，不隐瞒超限。

### 3.2 按页读取真实状态

`detail="states"` 时增加 `offset`（整数，默认 0，非负）与 `limit`（整数，默认 8，范围 1–16）；summary 模式不接受这两个参数，避免静默忽略输入。

- 直接读取 release 的真实 `states`，按完整 `state_id` 的 UTF-8 字节序排序。每项保留属性、几何、碰撞、行为和必要的变体映射，不重复返回另一份 `state_behaviors`。
- 每页返回 `block_id`、`total`、`offset`、`next_offset` 与状态数组；末页及越界空页的 `next_offset=null`。状态页不附带图片，也不重复完整语义摘要。
- 每页返回 `release_id`（输出中唯一保留的 release 身份，专供分页一致性判断）。客户端跨页发现 release 改变，丢弃已收集页面并从第 0 页重取；不增加服务端游标、历史 release selector 或快照服务。
- 不从属性笛卡尔积推测合法状态，不截掉后续状态。验收读取所有页后与数据库真实状态集合一致，无重复、遗漏，并验证页边界、非法参数和发布切换。

### 3.3 图片：最终 MCP 输出采用 256×256 无损 WebP（用户已批准）

用户于 2026-09-27 明确批准同时缩分辨率和更换编码：每个方块的整张四视角预览由 `512×512` 缩为 `256×256`，每视角 `128×128`，采用 nearest 缩采样后编码为无损 WebP。无损指编码保留缩采样后的像素，不表示缩采样本身无损。该决定替代原 PNG 方案和第 3.5 节曾建议的 512 原尺寸 WebP。

转换只发生在 MCP 最终图片响应组装处，不新增 Studio 流水线阶段。Fabric 导出、Studio 导入、特征提取、AI 标注与 release 中存放的预览、mask 均继续使用原图；MCP 读取原图后，在发送前缩采样并编码，不改写原文件或落地一份中间缩图。

`get_block_details` 返回 256×256 的方块卡片；`search_blocks`、`compare_blocks` 的联系表按每张方块卡片 256×256 排列，不将多候选整张联系表挤成 256×256。联系表标签按最终尺寸绘制，组合完成后统一编码为一张无损 WebP，不逐卡片重复转码。状态页仍不附图。

输出 MIME 统一为 `image/webp`；Schema、实际 ImageContent、尺寸、image ID、摘要及映射均与最终 WebP 字节一致。采用满足目标平台的最小编码依赖，不手写 WebP 编码器，不增加质量滑块、格式协商或持久图片缓存服务。

图片仍独立于 8KB 文本预算。验收报告字节与编码耗时，检查客户端实际接收、透明度、联系表标签和四视角可辨识性；无损解码后的 alpha 与可见 RGB 应与缩采样结果一致，原文件不变。若发现 256 图细节问题，修正采样或排版并复验，不能静默回退到 512/PNG 或有损编码后宣称满足决定。已实施，见第 3.6 节。

### 3.4 目前的尺寸证据（不是功能验收）

在 `rel_d0204fb090764c94b4a868a29ec50894` 上只做了内存投影与缩图试算，未修改 release 或查询实现：

| 样本 | 当前结构 JSON | 去掉状态枚举和重复行为、保留完整语义后的摘要草样 | 原 PNG | 256 PNG |
|---|---:|---:|---:|---:|
| `oak_stairs` | 135581 B | 2917 B | 19442 B | 3962 B |
| `spruce_trapdoor` | 99979 B | 3054 B | 16188 B | 2401 B |

草样尺寸尚未覆盖最终字段接线、分页、Schema 校验或视觉验收；实现后重新测量。

### 3.5 JPEG/WebP 压缩实测（2026-09-27，选型已由第 3.3 节确定）

同一 release 的 12 个样本：`snow_block`、`white_wool`、`white_concrete`、`oak_stairs`、`spruce_trapdoor`、`stone`、`mossy_cobblestone`、`glass`、`glass_pane`、`iron_bars`、`oak_leaves`、`brown_banner`。使用环境中已有 Pillow/libwebp 1.3.2，仅在内存中转码；未增加项目依赖、替换原图或修改 MCP。

| 12 张图的总字节 | 原尺寸 512×512 | 256×256 nearest 缩图 |
|---|---:|---:|
| release 现有 PNG | 207267 B | — |
| Pillow 优化 PNG | 92938 B | 50974 B |
| JPEG Q85，白底合成，optimize=true | 97031 B | 49588 B |
| WebP 有损 Q85，method=6，保留透明度 | 35086 B | 17038 B |
| WebP 无损，quality=100、method=6 | 31304 B | 20070 B |

无损 WebP 在 512 样本总量上比现有 PNG 减少约 85%，比优化 PNG 减少约 66%，且小于本次有损 WebP；256 有损 WebP 比同尺寸无损 WebP 再省约 15%。这是该样本和编码参数的结果，不是全库收益承诺；不同编码器的 quality 数值不代表相同感知质量。256 试算使用 Pillow nearest，和第 3.4 节 Node 采样位置及 PNG 编码不同，不能将两节缩图字节作同像素编码器对比。

这轮测量曾支持优先评估 512 原尺寸无损 WebP，用户随后选择了 **256×256 无损 WebP**，按第 3.3 节实施；表中的其他格式仅保留作对照证据。JPEG 样本必须合成背景，且玻璃、细栅栏、树叶等样本未必比 PNG 小。[WebP 官方资料](https://developers.google.com/speed/webp/docs/compression)说明其有损/无损模式均支持透明度。

12 张原尺寸无损 WebP 解码检查通过：alpha 与所有 alpha 非零像素的 RGB 均保持原值；未要求保留全透明像素的隐藏 RGB。256 WebP 的最终 MCP 客户端接收、Schema/MIME 接线、编码时间及目标平台依赖仍待实施验证，不能由字节数推断视觉质量合格。原始渲染、mask 和特征计算继续使用原始数据；仅转换 MCP 响应节省的是传输体积，不等于已有 release 的磁盘占用减少。格式决定已批准，已按第 3.6 节实现。

### 3.6 实施与实测（2026-09-27）

B 阶段已在 Node MCP 实现，Schema 升为 `mcp-*-output.v2`／`mcp-error.v2`，不保留 v1 输出兼容。

- `get_block_details` 按第 3.1、3.2 节实现 `detail="summary"|"states"`；summary 下传 `offset`／`limit` 返回参数错误，工具输入 Schema 对二者不设默认值，避免客户端自动补默认导致误拒。摘要的代表状态取首个视觉变体的 canonical 状态，没有视觉变体时取默认状态并给出已审核的 `skip_reason`；变体警告与本地名单提示合并去重。
- 支撑事实在当前 release 全部为 unknown：输出省略值为 unknown 的 `requires_support` 与各方向 `support`，已知值原样保留，Schema 注明缺失即未知，不代表 false。
- 图片：`png.mjs` 读取 release 原 PNG，在内存中 nearest 缩为 256×256 卡片，联系表按每卡 256 排列并在最终尺寸绘制 T01 标签，整张只编码一次。编码依赖为 `@jsquash/webp` 1.5.0（libwebp 的 WebAssembly 构建，约 1 MB，无原生二进制，Windows/Linux 通用），参数为 libwebp 默认无损强度 `q=75,m=4` 并开启 `exact`。实测 `q=100,m=6` 单卡约 0.7–1.3 秒、字节几乎不变，故不用。

用户在实施时追加要求去除对流程无用的哈希、版本等信息，本次一并完成：

| 移除 | 说明 |
|---|---|
| `schema_version`、`request_id`、`manifest_sha256`、`resolved_release_id`、`minecraft_version` 回显及 `data` 外壳 | 所有成功输出改为扁平对象。release 身份只保留在 `index_info` 与状态分页的 `release_id`（第 3.2 节的跨页一致性需要）。 |
| 图片 `image_id`、`sha256`、`purpose`，以及与之重复的 `contact_sheet` | 图片元数据只剩 `content_index`、`mime_type`、宽高与 `tiles`（或详情卡的 `state_id`）。 |
| 几何／碰撞 `signature` 哈希、审核记录 ID（`audit`）、`translation_key` | 形状只返回 boxes；审核 ID 在 MCP 内无可用消费方。 |
| 搜索的 `search_id`、`query` 回显、恒空 `hard_filters`、恒 false `reranked_by_llm`、`exclusion_summary`、恒为 local 的 `score_source`、与 `block_id` 相同的 `variant_id`、仅复述已有 ID 的 `machine_fact_refs` | 原本恒等的 `local_score`／`final_score` 合并为 `score`。事实追溯改为以 `block_id` 与 `recommended_state_id` 调用详情。 |
| 比较的 `block_ids` 回显；错误输出的 `retryable`、`provider_error_code`、`field_errors`、`integrity_component`、`images` 等恒定或空字段 | 错误只保留 `error_code`、`message`，以及非空时的 `available_versions`／`invalid_block_ids`。 |

保留 `index_info` 的免责声明与 `built_at`；`score_breakdown`、`reason` 等搜索输出精简仍属第 5 节 P1 后续。

全 release 扫描（`rel_ec8397fe58cb4faba5fa15b1de406ca0`，1,196 blocks）：摘要 Schema 全部通过，**0 项 ≥ 8000 B**，最大 3,656 B（`powder_snow_cauldron`），中位 1,644 B；`oak_stairs` 1,835 B、`spruce_trapdoor` 1,815 B（改前 `oak_stairs` 为 135,626 B）。以 limit=16 读取 2,733 页后，每个 block 的状态集合与索引完全一致，无重复或遗漏。1,172 张卡片解码后 RGBA 与原 PNG 的 nearest 缩采样逐字节一致，WebP 共 2,043,800 B，原 PNG 为 19,965,482 B。详情平均 39.9 ms（含 512 PNG 解码）。

| 调用 | 文本 | 图片 | 尺寸 | 耗时 |
|---|---:|---:|---|---:|
| search `stone wall` 默认 limit 8 | 3,637 B（改前 7,934 B） | 12,958 B（改前 PNG 59,253 B） | 1024×512 | — |
| search `stone wall` limit 12 | 5,420 B | 18,358 B | 1024×768 | 243 ms |
| search `white` limit 8 | 3,758 B | 14,164 B | 1024×512 | 171 ms |
| compare stone/oak_planks | 521 B（改前 1,329 B） | 4,946 B（改前 21,453 B） | 512×256 | 46 ms |
| compare 6 种白色方块 | 1,339 B | 14,078 B | 1536×256 | 121 ms |

人工查看了搜索 12 卡、比较（玻璃板、铁栏杆、橡树树叶、棕色旗帜）与楼梯卡片：T 标签清晰、四视角可辨、透明正常；玻璃板在 512 原图中本就只有中心柱，256 卡片与之一致。

验证：`python -m pytest tests -q` **367 passed、6 skipped**；R4 新增用例覆盖摘要 < 8000 B、代表状态与 support 省略、带部分／全透明像素图案的卡片无损、20 个状态 8/8/4 分页与 UTF-8 顺序、越界空页、summary 下拒绝分页参数、非法 limit/offset、发布切换后页面 `release_id` 变化，以及三种带图工具的 WebP MIME／尺寸／标签位置；FTS/LIKE 两路径均执行。本机 Claude Code 会话通过真实 stdio 连接调用 `get_block_details`：摘要与状态页均为新结构，WebP 卡片在客户端正常显示。扫描脚本与报告在忽略目录 `build/mcp-slim-evidence/`。Windows 端需在 `mcp-node` 执行 `npm ci` 安装新依赖后才能运行本版本 MCP，尚未在 Windows 复验。

## 4. Studio 可选多 worker

已新增 run 配置 `feature_workers`，默认 `1`，允许整数 `1–5`，在 Studio 导入表单、`POST /api/imports`、run 配置快照和公开进度中接通。接受导入前在 staging 的原 SQLite run 快照中固定，复制或最终重命名中断后也能恢复；同一 run 的重试拒绝改值，缺失原值时拒绝猜测。后续 AI 配置保留该值，纳入已有 `effective_config_hash`，不增加第二套配置账本。

该参数控制同一个 run 的 `EXTRACT_FEATURES` 中独立 variant 计算，不是 Uvicorn worker 数，也不是同时跑多个 Studio。已有 `offline_annotation.concurrency` 继续负责 AI 批次并发，默认 1、上限 5，并继续受进程级共享 5 个请求槽约束；不新增同义 AI workers 参数，不将两个参数相乘。

实现与生命周期约束：

- `feature_workers=1` 在主进程的计算线程中每 run 串行处理，不阻塞协调线程；它与并行 run 共用五槽额度，不创建子进程或宣称线程加速。大于 1 时用标准库 `ProcessPoolExecutor` 和可跨 Windows/Linux 的 spawn 入口。
- 一个 Studio 共享有界特征计算池，总上限 5；每个 run 在途特征任务不超过其 `feature_workers`。只保留实际可派发的任务，不一次提交整个数据集，不引入队列服务或第二套 job 表。
- 主 worker 原子认领现有 jobs，校验路径、读取图片并准备可序列化的计算输入；子进程仅接收图片字节与机器事实、执行纯计算、返回结果，不接收 SQLite 连接、不写库、不读取 provider 凭据。
- 主 worker 在短事务内复核任务所有权和 run 状态，将结果与 job 完成状态一起提交。等待计算时不持有 SQLite 写事务或长时间占住 run lock；协调线程继续处理 heartbeat 和暂停请求。
- 暂停：停止派发，排空并提交已派发任务后进入暂停。取消：停止派发，丢弃尚未提交结果并收束任务状态。关闭：停止派发并等待子任务，未退出不能声称关闭成功。崩溃后的纯计算任务可按现有 stale 恢复重算；AI 的未知发送结果仍转人工处理，不自动重发。
- 阶段依赖保持顺序，首先只并行有独立输入的特征任务。导入、校验、数据库写入、构建与发布不盲目并行；有测量证据后再扩范围。

验收由该阶段实现者负责：1 与 2/5 workers 的同一输入产生相同规范化特征结果；确有同一 run 的计算进程重叠；任务不重复提交；暂停、取消、关闭、子进程失败和 stale 恢复正确；AI 并发上限不变。用代表性真实导出比较串行/并行墙钟时间并报告资源消耗，不预设加速倍数。默认 1 不因“可并行”自动上调。

源码依据：[特征计算](src/blockpedia/features.py)、[任务协调](src/blockpedia/worker.py)、[Studio 配置](src/blockpedia/services.py)、[Web 接线](src/blockpedia/web.py)、[run 投影](src/blockpedia/run_snapshots.py)。优先扩展现有 `tests/r2/test_phase1_core.py` 与 `tests/r3/test_pipeline_review.py` 的相关检查，不新建通用调度框架。

### 4.1 实施与实测（2026-09-27）

原串行点是 `_claim_pending_job` 的单个 running job 限制与 `_extract_one` 内同步 PNG 解码、逐像素颜色/边缘计算。真实 `stone` 的 cProfile 样本总耗时 2.12 秒，其中解码约 0.89 秒、颜色转换约 0.86 秒（含 profiler 开销，仅定位热点）。现改为有界共享 spawn 池，主进程保留路径检查、认领和事务提交；不改变特征算法、导入/校验/发布顺序或 AI 独立五槽上限。异常退出的池通过后台 shutdown 等待实际回收，回收前不能报告关闭成功或创建替代池。暂停以事务内剩余 running jobs 判定排空；失败后旧任务未排空时禁止重试，完成回写核对登记身份；派发中途失败也保留已提交任务的完成回调。

真实数据来自本机已有 `rel_d0204fb090764c94b4a868a29ec50894` 的 1,172 对原始 512×512 preview/mask，固定选取第 3.5 节 12 类常见方块及按 variant ID 均匀抽取的其余 36 项。每组在独立解释器和临时 workspace 中执行实际 `WorkerService.tick`，仅构造特征阶段输入；release 原图与记录保持不变。计时包含解释器/进程池冷启动、计算、事务提交、关闭和结果摘要，不包含导入校验或准备工作区。

| feature_workers | 墙钟时间 | CPU 时间（主进程与子进程合计） | 采样峰值进程树 RSS |
|---:|---:|---:|---:|
| 1 | 43.625 秒 | 41.450 秒 | 52,660 KiB |
| 2 | 24.459 秒 | 45.520 秒 | 126,256 KiB |
| 5 | 16.016 秒 | 45.020 秒 | 219,252 KiB |

三组均完成 48 个 jobs，规范化特征与最终 variant record 摘要完全相同；样本墙钟加速约 1.78× / 2.72×，默认仍为 1。环境为 Linux ARM64、CPython 3.14.7、4 个逻辑 CPU、约 23.4 GiB 内存；RSS 为每 20ms 采样的进程树内存和，可能漏过短峰值。此次是代表性抽样单次测量，不是全库、Windows 或整条流水线加速承诺，也不以历史 feature_json 为本轮正确性基准。

复现：`PYTHONPATH=src python tools/benchmark_feature_workers.py --release <release目录> --workers 1 2 5 --samples 48`。脚本与本轮规范化输出比较检查已保留；本地明细在忽略目录 `build/feature-workers-evidence/benchmark-20260927T104832Z.json`，含样本、输入/输出摘要、源码版本摘要及资源口径。生命周期回归在 `tests/r2/test_feature_parallel.py`，配置/API 与 R3 冻结回归在 `tests/r2/test_live_progress.py`、`tests/r3/test_pipeline_review.py`；最终 `python -m pytest tests/r2 tests/r3 -q` 为 **305 passed、1 skipped**（Windows junction 平台检查）。浏览器冒烟验证表单提交、冻结值展示、同 run 改值拒绝与输入范围，未出现 JS 异常。Oracle 审核结论与 commit 见本次会话。

## 5. 搜索、渲染与剩余建议的取舍

| 项目 | 本次新 release 的计划 |
|---|---|
| 渲染偏暗 | D1 已修复继承 GUI 方向光的问题，使用固定的原版 DEFAULT/LEVEL 双灯 diffuse 预设，详见第 8 节。本次 Windows 完整单次导出及特征重算已完成，见第 9 节；双次确定性验证仍待执行。旧稿统一 L*≥85 不作为门槛。 |
| 形状分类 | 优先将已有 stairs/slabs/walls/fences 等 registry tags 映射到统一分类；只有缺少事实的类别才补 exporter 最小运行时分类。查询词与实际分类对齐。 |
| 搜索相关性 | 保留现有 FTS/LIKE 宽召回，改进短语、词覆盖率与字段加权；英文按词边界，名称/同义词高于用途/材料/风格，再高于摘要。补中文颜色与常用用途映射，材料真正参与评分；不新增在线模型。 |
| `avoid_for` | 只从正向索引文本排除。暂不拆分标注字段或重做受控词表：没有旧兼容要求，也不意味着必须扩大本次 Schema 改造。保留共享语义投影/人工覆盖功能，不把含义混杂的历史字段直接用于负向惩罚。 |
| 资格与风险 | 技术名单、7 种蠹虫方块及 E2 特殊方块按第 2 节降权；重力、融化、火、生长提示按 E2 从 tags 推导；已有 conditional 警告保留。其他行为风险与名单分开，未知事实明确未知，不声称完成全量风险审核。 |
| 建筑查询集 | 约 20 条中英真实需求，覆盖光滑石墙、深木屋顶、百叶/窗框、白色墙面、旧苔墙、木梁、栏杆、窗台线脚、暖光。每条列可接受的前三候选集合及反例；实施时定稿，作为本轮相关性回归门，不锁死唯一名次。 |
| 比较字段 | 已实现（P1 第 5 项）：按方块分列全部字段并列出取值不同的字段；删除未使用的 `context`/`compare_states`。见第 5.2 节。 |
| 图上名称、搜索输出精简 | 搜索、比较、详情输出第二轮瘦身及搜索图片 `image` 参数已实现，见第 5.4 节；事实追溯仍经 `block_id`／`recommended_state_id` 调详情。 |
| 分面颜色、多主色、相似色检索、系列形状、3×3 平铺 | 分面颜色、多主色、`similar_to`、系列形状已在 Node MCP 实现，见第 5.2 节；3×3 平铺仍暂缓。16 色归并见第 5.1 节。 |
| 删减推荐状态属性 | 不采用；完整合法 canonical 状态 ID 保留。 |

### 5.1 E3 搜索召回与排序实施（2026-09-27，未提交）

实现位于 `mcp-node/query.mjs`，只改 Node MCP 在线排序，不改导出事实、标注或 release；当前正式 release 即可生效。改前在该 release 上复现了问题报告的全部现象，另发现未分词中文（`白色抹灰墙`、`百叶窗`、`暖光灯`）零结果。

- 召回：保留 FTS/LIKE，按词及其桥接词召回；含颜色、形状或发光意图时全部候选参与评分（这些维度来自机器事实，索引文本看不到）。得分为 0 且非精确命中的候选不返回。
- 文本分：每个查询词取最佳字段命中，字段权重 名称 1.0 > 同义词 0.9 > 用途/材料/风格/形状词/颜色词/机器标签 0.6 > 摘要 0.35；英文按整词（仅折叠复数）匹配，中文按子串。词覆盖率取平均，整句命中另计 10%；名称被查询词覆盖的比例（“Stone”优于“Stone Button”）与多字段一致命中各占小幅加成。`avoid_for` 不参与匹配；Studio 下次构建起也不再写入召回文本（`search.py`）。
- 中文：按“词表 + 官方中文名 + 至少 3 个名称共享的前缀（橡木、深色橡木、石砖）”最长匹配分词。词表只放机器事实意图（颜色、形状、发光）和中文/建筑行话到标注用词的桥接（抹灰→plaster/concrete、百叶→trapdoor、栏杆→fence/bars）；用途、材料、风格直接与标注字段比对。补齐白黑灰棕等中文颜色。
- 颜色：按预览 Lab 的明度/彩度/色相判定，阈值按带方向阴影的渲染值标定（白色方块均值 L* 约 64–75）；名称或标注写明颜色的额外加分。“暖光”只看标注，因预览显示的是灯体而非光色。
- 形状：按 registry tags 推导 stairs/slab/wall/fence/fence_gate/trapdoor/door/button/pressure_plate/carpet/sign/banner/bed/candle/rod；没有 tag 的玻璃板/栏杆（`_pane`、`_bars`）、苔藓地毯、末地烛、锁链暂按 ID 后缀，墙挂变体（`*_wall_*`）另行识别。未指定形状时派生形态 ×0.9、按钮/压力板/告示牌/旗帜/墙挂/杆类 ×0.8。`wall`/`墙` 有“墙面”和“墙方块”两义，不作形状意图：墙方块名称中的“墙”只按 0.6 计，墙挂变体中的“墙”不计。
- 发光：取该方块所有合法状态的最高亮度（红石灯的代表状态是未点亮）。
- 颜色补充：dark/pale 是相对说法（Dark Prismarine 实测 L* 25.9，比磨制黑石亮），明度只按实测，名称/标注写明只作 15% 加成。
- 同一个字段词只算给一个查询词：桥接词若是另一个查询词或已被前面的词占用（`mossy` 同属“苔藓”“旧”），后面的词不再用它。
- 排序：精确官方名/ID 优先且分数为 1；再按分数、标注置信度、（未指定形状时）基础方块优先、有楼梯/台阶/墙形态的建材优先（石头、平滑石头、石砖有，末地石没有）、eligible 先于 conditional、variant ID。技术/蠹虫/特殊方块 ×0.25 不变。
- 16 色系列合并：系列指 16 种染料前缀全部存在的同后缀方块（`*_wool`、`*_stained_glass_pane` 等；郁金香、红/棕蘑菇不算）。查询不含颜色意图时，系列只保留排名最高的一条（同分时白色优先），放在系列首次出现的位置，候选附 `color_series`（`block_id_pattern` 与 `other_colors`）；含颜色时不合并，精确名称/ID 命中的条目保持独立。
- 输出：`score_breakdown` 改为 `text`/`color`/`shape`/`light`，候选新增可选 `color_series`（Schema 仍为 v2，未单独升版），`reason` 写明命中字段与各项依据。排序本身约 30–45 ms/次，首次调用多出建索引开销。

验收：新增 `tests/r4/golden_queries.json`（23 条中英建筑需求，每条列可接受前三及前 8 名反例）与 `tests/r4/test_search_golden.py`，对 `BLOCKPEDIA_GOLDEN_DATA_ROOT`（默认本机数据目录）的正式 release 运行，无 release 时跳过，跳过不算通过。改前 23 条中 20 条失败；按用户“继续优化”要求未改期望，现 **25/25 通过**（新增“stained glass window”“彩色玻璃 窗”两条，要求前 8 名每个 16 色系列至多一条）。`red brick wall` 的可接受集在实测后补入 26.x 新方块 `cinnabar_brick*`。`tests/r4/test_node_mcp.py` 另以 FTS/LIKE 两路径覆盖系列合并、4 色非系列不合并、带颜色不合并与精确 ID 独立。

未做：`avoid_for` 拆分为 not_for/confusable_with 并降分、`building_roles` 受控词表（均需 Schema 与重新标注）；exporter 按 Java 方块类别输出形状（pane/bars 仍靠 ID 后缀）。

### 5.2 P1 建筑选材：分面颜色、系列形状、比较与相似色（2026-09-27，未提交）

用户于 2026-09-27 按推荐方案批准：颜色在 MCP 运行时由 release 原图计算并按 release 缓存在内存；删除比较的 `context`/`compare_states`；`search_blocks` 新增 `similar_to`，给出时 `keywords` 可省略且只作筛选，默认只比同一形状分类。只改 Node MCP，不改 exporter、Studio 特征（仍为 features.v1）、标注或 release，当前正式 release 即可使用；无新依赖，Windows 无需重新 `npm ci`。

- 分面颜色（`mcp-node/palette.mjs`）：预览四格依次为等轴、正面（北）、侧面（东）、顶面。顶面取顶面格；侧面合并正面、侧面两格；等轴格混合多个面，不用。lighting.v2 的面光照系数（顶 1.0、北 0.74、东 0.497，D1 实测）逐像素除回，得到贴图色；半透明像素的 RGB 是按 alpha 预乘的（白色染色玻璃存为 102,102,102,102），先除以 alpha 再除光照。每面输出平均色 hex、L*、逐像素 L* 标准差（混凝土约 0、石头 4、圆石 11），以及按 Oklab 分箱贪心聚类得到的最多 3 个主色（占比 ≥10%）。可见像素过少的面为 null；实际 release 中 1,172 个视觉候选里有 130 个有一面为 null（火把、平面类等）。
- 系列形状：按注册表命名推导（`oak_planks`→`oak_*`、`stone_bricks`→`stone_brick_*`、`quartz_block`→`quartz_*`、`white_wool`→`white_carpet`），形状包括 stairs/slab/wall/fence/fence_gate/door/trapdoor/button/pressure_plate/sign/hanging_sign/pane/bars/carpet/shelf，墙挂告示牌只归入系列、不单列。只有该材料的满方块本身或其形状才附系列，所以 `bamboo_block` 不挂竹板的系列。全 release 有 476 个方块带系列，已有 tag 的形状（楼梯、台阶、墙、栅栏、门、活板门、按钮、压力板）与 tag 全部一致。原来“有楼梯/台阶/墙的建材优先”的排序规则改用同一函数，结果变化只有 3 项且都是修正：`bricks` 由否变是，竹子植物和 `bamboo_block` 由是变否；建筑查询集仍为 25/25。
- `get_block_details` 摘要新增 `representative.shape_class`、`representative.colors` 与顶层 `family`（没有时省略）。全 release 1,196 个摘要全部通过 Schema，最大 3,991 B（`powder_snow_cauldron`），中位 2,043 B，没有 ≥8000 B 的。
- `compare_blocks` 输出改为 `mcp-compare-blocks-output.v3`：`blocks[]` 每项含 candidate_id、名称、资格、形状分类、几何类、透明、亮度等级、红石、分面颜色、语义（中英摘要、颜色词、材质、风格、用途）、系列、警告，另附 `differing_fields`，不再只输出取值不同的行。6 种木板为 8,989 B，6 种白色方块为 6,069 B，耗时约 90 ms。
- `similar_to`：只在同一形状分类中比较（tag 形状，否则按代表几何分为 full_cube/other）。距离取顶面、侧面 Oklab 平均色距离的均值，再加 0.005×L* 标准差之差作为纹理项（否则石头与圆石的均色几乎相同，距离只有 0.005）；相似度 = 1 − 距离/0.25，≤0 的不返回。名单方块仍 ×0.25（虫蚀石头与石头完全相同）；不做 16 色系列合并。候选新增 `color_delta_e`（Oklab ×100）；新错误码 `BLOCK_HAS_NO_PREVIEW`。每个 release 的首次调用要读取全部预览：本机 ARM 约 2.7–3.7 s，之后 0.1–0.5 s。
- PNG 解码：原先逐字节为 Paeth 分配数组，是主要瓶颈。改为按滤波类型分开的原地循环，1,172 张预览由 9.1 s 降到 4.0 s，输出逐字节一致；另有 Python 参照测试覆盖 5 种滤波在首行与后续行的情况。

验证：`python -m pytest tests -q` **373 passed、6 skipped**（Windows、GPU 与 PowerShell 相关）。新测试用按面光照系数生成的预览，检查贴图色还原、纹理标准差、双主色、预乘 alpha、系列、比较结构、similar_to 的排序、形状过滤、名单降权、关键词筛选与两种错误，FTS/LIKE 两条路径都执行。本机真实 stdio 连接正式 release 调用四个工具，输出均通过服务端 Schema 校验。本会话已连接的 blockpedia MCP 进程仍是旧代码，需要重载后才能用到新接口。

### 5.3 建材筛选、形状细分、铜氧化折叠与材料组（2026-09-27，未提交）

针对用户反馈的三类问题，只改 Node MCP 在线排序与输出，不改导出、标注或 release；当前正式 release 即可生效，无新依赖。改前在正式 release 上逐条复现了反馈中的全部现象。

- 非建材降权：矿石（`*_ores` tag、`_ore` 后缀、远古残骸）、资源存储块（`beacon_base_blocks` 与粗矿块、煤/青金石/红石块）、容器（潜影盒、铜箱子、蜂巢 tag 及箱子、木桶）、功能方块（铁砧、炼药锅、铁轨 tag，工作台、熔炉、侦测器、铜灯等固定 ID）与植物（花、盆栽、树苗、作物、珊瑚等 tag，蘑菇、竹子、甘蔗等固定 ID，死珊瑚按名称）×0.5。有形状分类的方块（铜门、苔藓地毯）与树叶不算。`similar_to` 中参照方块同类时不降权；关键词检索中查询词命中名称或同义词的中心词（末词，中文为词尾：`ore`→铜矿石，`copper` 不算）时视为点名，不降权。
- 形状细分：原 `other` 按代表状态几何细分为 `passable`（无碰撞：花草、火把、铁轨）、`sheet`（贴地薄片）、`small_fixture`（宽深都不超过半格：灯笼、花盆、头颅）、`partial_block`（其余：箱子、炼药锅、铁砧）；`full_cube` 还要求有碰撞，高草、藤蔓等外形满格但无碰撞的不再算满方块。无代表几何时仍为 `other`。
- 铜氧化系列：同一铜方块的 4 个氧化阶段及其涂蜡版为一个系列（`copper_block` 的阶段名省略 `_block`）。涂蜡版外观与未涂蜡相同，始终折叠进未涂蜡；查询不含颜色时各阶段也折叠为一条，候选新增 `oxidation_series.other_block_ids`。精确名称/ID 不折叠。`similar_to` 只折叠涂蜡版，并排除参照方块自己的涂蜡/未涂蜡孪生。
- 查询词：`red brick`/`红砖` 作为复合词，颜色改用“砖红”（实测 bricks L* 35、彩度 21、色相 41），不再以饱和红为准；暖色的命名词去掉 `gold`（名称中是金属：深板岩金矿石）；`shingle`/`瓦` 改为楼梯/台阶形状意图，去掉只命中带釉陶瓦和地毯的 `tile`。
- 相关性下限：关键词检索中，相关性低于最佳非精确候选 55% 的不返回（名单与非建材系数不参与此判断，降权方块不会因降权被删）。`glass` 因此只返回 5 种玻璃，不再以信标、灯笼、黑曜石（“volcanic glass”）补足。
- 材料组：详情与比较的 `family` 新增 `material_blocks`，按命名规则剥离表面处理前缀（stripped/polished/cut/chiseled/cracked/mossy/smooth/cobbled/packed）和种类后缀（planks/log/wood/stem/hyphae/bricks/tiles/block/pillar/mosaic/grate）得到材料根；铜的涂蜡与氧化阶段保留在根内。形态方块（楼梯、台阶等）仍挂在其满方块的 `forms` 下，植物与名单方块不入组。全 release 得 48 组，包括 11 种木材各 5 块（原木、木头、去皮原木、去皮木头、木板）、深板岩 8 块、石头 6 块、凝灰岩 5 块等。没有形态但有材料组的方块（`spruce_log`）也返回 `family`，`base_block` 为自身、`forms` 为空。

Schema：详情 v2 与比较 v3 的 `family.forms` 允许为空并新增 `material_blocks`，`shape_class` 说明更新；搜索 v2 新增可选 `oxidation_series`。均未升版。

复测（正式 release）：`similar_to` stone_bricks、white_concrete、bricks 的前 12 名不再有矿石、潜影盒、铁块、铜灯、粗矿块；lantern 前两名为灵魂灯笼、滴水石锥，其后是铜灯笼，盆栽仍在第 5、8 名（已 ×0.5，但该形状类中与灯笼同色的非植物方块很少）。`warm roof shingle` 前 12 名全是暖色楼梯/台阶；`red brick wall` 第 1 名为 bricks；`栏杆` 为铁栏杆、铜栏杆（折叠 7 个）后接栅栏；`copper` 不再出现铜矿石，`ore`/`矿石`/`chest` 仍正常返回对应方块。全 release 1,196 个详情摘要通过 Schema，最大 3,999 B（`powder_snow_cauldron`）；本机真实 stdio 调用四工具通过服务端 Schema 校验。

验证：建筑查询集由 25 条增至 32 条（新增 warm roof shingle、暖色瓦屋顶、glass 与 4 条 `similar_to`；`栏杆`/`railing` 新增 `require` 须含栅栏和栏杆，`red brick wall` 固定第 1 名 bricks），另加正式 release 上的材料组检查；新增条目在改前代码上 9 条失败，改后全部通过。`tests/r4/test_node_mcp.py` 新增夹具用例，FTS/LIKE 两路径覆盖降权与中心词点名、同类不降权、形状细分、氧化折叠（含带颜色时只折叠涂蜡、精确 ID 独立）与材料组。`python -m pytest tests -q` **376 passed、6 skipped**（Windows、GPU 与 PowerShell 相关）。

未做：`dark_prismarine` 与 `prismarine`、`cobblestone` 与 `stone` 等命名不同源的材料不入同组；非建材类别只影响搜索排序，未写入详情或比较输出；中文单字按子串匹配的既有问题（`花` 命中花岗岩）未处理；中心词按名称末词判断，“Block of Raw Copper”这类英文名会被 `copper` 视为点名，粗铜块在 `copper` 中仍排第 7。

### 5.4 MCP 输出第二轮瘦身（2026-09-27，未提交）

按用户转来的五条建议实施，只改 Node MCP 输出与 Schema，不改排序、导出、标注或 release；当前正式 release 即可生效，无新依赖。Schema 沿用第 5.3 节做法原地更新（搜索 v2、详情 v2、比较 v3 均未升版），夹具同步改写。

- 搜索图片：`search_blocks` 新增 `image`：`full`（默认，维持第 3.3 节已批准的 256 四视角卡片）、`compact`（每格 128×64，只放等轴与顶面两视角各 64px，nearest 取自原图对应象限，标签缩为 2 倍字形）、`none`（不附图，`images` 为空）。像素数：8 个候选由 1024×512 降到 512×128。比较与详情不变。
- 联系表映射：搜索与比较的 `images[].tiles` 改为 `columns`；格子按候选顺序行优先排列，第 n 个在 (n−1) div columns 行、(n−1) mod columns 列，格宽 = width / columns。
- 搜索候选：`score` 与 `score_breakdown` 输出保留 2 位小数（排序仍用全精度）；`score_breakdown` 只列非零项，全为零时省略；`candidate_qualification` 仅在 conditional 时出现；`recommended_state_id` 与 `block_id` 相同时省略（完整状态 ID 的决定不变，只是不重复）。
- 相似色：`reason` 只写 `ΔE 1.4, texture 0.0`（另有降权时附说明）；ΔE、纹理项和分数公式在顶层 `similarity`（`block_id`、`shape_class`、`basis`）写一次，带关键词时注明只作筛选。删除与 reason 重复的 `color_delta_e`，`score_breakdown` 不再带恒为 1 的 `shape`。
- 材料系列（详情与比较共用）：`family.forms` 由“形状→完整 ID”表改为形状名数组，另给 `form_id_pattern`（如 `minecraft:oak_{form}`），形态 ID 即替换 `{form}`；无形态时 `forms` 为空且不给模式。`material_blocks` 改为模板，另给 `material`（材料词）：`"material": "oak", "material_blocks": ["*_log", "*_planks", "*_wood", "stripped_*_log", "stripped_*_wood"]`，方块 ID = `minecraft:` + 模板中 `*` 换成 material。模板来自划分材料组的同一套前缀／后缀剥离，铜的涂蜡与氧化阶段留在模板里（`exposed_cut_*`）。正式 release 中 484 个带材料组的方块按模板还原后与原 ID 列表逐项一致（43 组）。
- 分面颜色（详情与比较共用）：顶面与侧面 Oklab 距离 ≤0.02（ΔE×100 ≤2）且 L* 标准差相差 ≤2 时合为一项 `top_and_side`（取顶面数值）。正式 release 1,172 个有色摘要中 600 个合并；原木、草方块、书架、TNT、砂岩、干草块等顶侧不同的仍分列，南瓜、石英柱因均色与纹理离散度相近而合并。
- 比较语义只保留 `summary_en`（与其余英文标注词一致）；详情仍保留中英两种摘要。
- 详情：代表状态与状态页中，碰撞箱与外形相同时省略 `collision`；缺失即与 `shape` 相同。正式 release 中 314 个代表状态保留碰撞箱。

实测（正式 release，真实 stdio 经服务端 Schema 校验，文本为 structuredContent 字节）：

| 调用 | 改前 | 改后 |
|---|---:|---:|
| search `stone wall` | 3,289 B | 1,677 B（图片 compact 7,706 B / 512×128，full 19,812 B / 1024×512） |
| search `white` | 3,332 B | 1,889 B |
| similar_to `stone_bricks` | 4,629 B | 1,980 B |
| similar_to `bricks` + `wall`，limit 12 | 7,154 B | 3,151 B |
| compare 6 种木板 | 9,927 B | 6,381 B |
| compare 6 种白色方块 | 6,359 B | 4,743 B |
| compare stone/oak_planks | 3,041 B | 2,097 B |
| details `oak_stairs` | 2,749 B | 2,179 B |
| details `powder_snow_cauldron` | 3,999 B | 2,867 B |
| states `oak_stairs` 第 1 页 | 6,184 B | 4,752 B |

6 种木板比较中材料组由完整 ID 的 824 B 降到模板的 502 B；剩余为 family 约 1.9 KB（主要是 6 份相同的 11 个形状名）、语义约 1.7 KB、颜色约 1.0 KB。全 release 1,196 个详情摘要 Schema 全部通过，最大 3,175 B（改前 3,999 B），中位 1,940 B（改前 2,043 B）。人工查看 compact 联系表：12 格标签清晰，楼梯形态与砖纹可辨。

验证：`tests/r4/test_node_mcp.py` 改写旧字段断言并新增：compact 两视角像素取自原图对应象限且标签在格内、`none` 不附图、非法 `image` 被拒、默认值省略、conditional 保留、2 位小数、similarity 顶层说明及关键词注明、形状名与 `form_id_pattern`、`top_and_side`、比较只含英文摘要、材料组模板、碰撞箱仅在不同时出现（摘要与状态页）；建筑查询集的材料组检查按模板还原 ID 后比对；FTS/LIKE 两路径执行。`python -m pytest tests -q` **376 passed、6 skipped**（Windows、GPU 与 PowerShell 相关）。本会话已连接的 blockpedia MCP 进程仍是旧代码，需重载后才生效。

新 release 产出链：渲染定位及修正 → 完整导出 → 新 workspace 导入 → 特征与新标注 → 审核 → 构建 → 四工具集成验收 → 用户发布。无需迁移旧语义和审核；新的非 excluded 候选仍须满足构建的语义完整性要求。

## 6. 实施阶段与当前状态

各阶段应形成独立可验收的提交；实现、验证、Oracle 提交前审核通过后才提交，不 push。下表是依赖关系，不要求为等候外部 GPU 或名单审核而阻塞无依赖的工作。

| 阶段 | 交付物与依赖 | 验收负责人及最小证据 | 状态 |
|---|---|---|---|
| A 文档收敛 | 本文件、旧文档归档、必要引用接线 | 主代理：归档完整性、有效链接、原稿和 evidence 保留、候选提交审核 | 文档已整理；18 项名单已获用户批准；提交状态见会话 |
| B 详情与 MCP 图片输出 | summary/states、分页；三种带图工具最终响应采用256卡片/无损WebP，处理过程使用原图 | 实现者：两个摘要尺寸断言、全 release 大小扫描、分页集合一致、Schema/MIME、图片元数据、无损解码、客户端及视觉检查 | 已实现，见第 3.6 节；Windows 端需 `npm ci` 后复验 |
| C 特征多 worker | 第 4 节配置与进程池；可与 B/D 独立开发 | 实现者：串并行结果一致、生命周期/故障检查、同 run 并行证据及真实计时 | 已实现；默认 1。Windows 本次已用 5 个实际子进程完成 1,172 项特征；Linux 生命周期与计时见第 4.1 节 |
| D 渲染与形状事实 | D1 颜色修复及完整单次导出已交付；形状分类继续待办 | 实现者：Java 构建、真实 GPU 样本、分类来源检查；主代理核对完整导出验证 | 定向 GPU 与 Windows 完整单次导入校验已完成，见第 8、9 节；形状分类及双次确定性验证未完成 |
| E 搜索与名单 | 相关性、中文映射、技术及虫蚀降权；名单已获用户批准，最终颜色/形状验收依赖 D | 实现者：约20条查询、FTS/LIKE、精确与泛用途查询对照、稳定排序 | E1、E2 已实现；E3 相关性、中文映射与 16 色系列合并已实现、未提交，建筑查询集 25/25 通过，见第 5.1 节；P1 分面颜色、系列形状、比较 v3 与 `similar_to` 已实现、未提交，见第 5.2 节；建材降权、形状细分、铜氧化折叠与材料组见第 5.3 节，查询集 32/32 |
| F 新 release | 整合 B–E，新 workspace 完整处理并构建 | 主代理：构建通过，真实产物 summary 扫描、查询集、四工具 stdio 和状态引用一致；provider 结果独立取证 | 当前已实现版本的 Windows 候选已构建并通过四工具验证，见第 9 节；B、D 形状分类及 E 剩余项未完成，因此不代表全部计划验收完成 |
| G 发布 | 当前候选验收后按用户明确授权发布 | 主代理：实际指针切换及下一次 MCP 查询指向新 release | 已按用户新授权在 Windows 与本机 Linux 发布同一 release；本机会话四工具读取新版本。B/D/E 其余计划仍未结项 |

聚焦测试、概念验证、真实 GPU 导出、provider 标注、Windows/Linux 平台证据和最终发布分别报告，不用其中一种替代其他证明。失败只修复对应范围并复验，不为追求全绿重跑外部请求或降低既有安全边界。

## 7. 归档与后续维护

旧文档统一位于 [docs/archive/2026-09-27/](docs/archive/2026-09-27/)，包括旧建议、路线图、决策、重构规格、接口/运维说明、阶段记录和原始设计稿。它们只保留当时背景和证据，不是新的执行门；存在目标的相对链接已机械修正，缺失的历史 AGENTS 引用改为纯文本。原始设计稿字节不变。

[docs/evidence/](docs/evidence/) 中历史 JSON 报告保持原路径和内容，不因归档改写测试结论。旧 R2 验证工具对阶段文档的两处引用随归档更新，避免搬迁导致检查静默漏读；这不是功能实施。

以后只在本文更新当前范围、未决事项、阶段状态和证据链接；不继续补写归档规格，也不把代码字段列表复制进多份 Markdown。精确接口、数据类型与校验以实际 Schema/实现为准；本轮尚未实施的目标不会因写入本文而自动生效。

## 8. D1 exporter 颜色修复（2026-09-27）

根因：`BlockModelResolver` 使用原版 item/special-model shader；`FULL_BRIGHT` 只指定 lightmap 坐标，不能关闭方向漫反射。原 `RenderExporter` 未绑定自己的 Lighting，继承了界面绘制的灯光；shader 的 0.4 环境光项使顶面、侧面约只剩原贴图的 40%。原先“lightmap 整体少了约 0.7 倍”未获证实，不按该猜测补亮。

修复：渲染期间用独立的原版 `Lighting`，调用 `updateLevel(CardinalLighting.Type.DEFAULT)` 与 `setupFor(Lighting.Entry.LEVEL)`，结束（含异常）恢复原 shader lights 并释放资源。保留 FULL_BRIGHT、camera.v3、tint、透明度和特殊模型路径；记录 `lighting.v2` 及对应环境摘要，manifest Schema 接通新策略。

真实证据来自反向 SSH 显卡机，复用 `D:\Code\blockpedia` 的依赖与测试世界副本，在独立的 `D:\Temp\blockpedia-color-20260927` 运行。Windows x86_64、RTX 4080 Laptop GPU、OpenGL、驱动 610.88。13 类样本包含三种白色方块、石英、木材、石头、玻璃、楼梯、旗帜、箱子、萤石及橡树叶；每类分别以前置 ITEMS_FLAT 与 ENTITY_IN_UI 灯光启动，两次预览字节相同。PNG 写入故障后的灯光恢复和下一次成功渲染也通过。

8 种可直接对照原纹理的材质，按正交视角 alpha=255 的对象像素统计 RGB 均值，与对应原贴图均值比较：

| 面 | 修复前比例 | 修复后比例 | 解释 |
|---|---:|---:|---|
| 正面 | 约 0.546 | 约 0.740 | 原版 DEFAULT 双灯 diffuse |
| 侧面 | 约 0.400 | 约 0.497 | 同一预设的方向阴影 |
| 顶面 | 约 0.400 | 1.000 | 恢复原贴图颜色 |

数值容差计入 8 位量化；方向阴影仍存在，整张联系图的平均 L* 不能解释为无阴影材质色。实际查看了雪块、楼梯、玻璃、旗帜与树叶的导出预览。

验证：Java 25 `./gradlew --no-daemon build renderLightingProbeJar` 通过（含 camera 检查）；指定本次 GPU 产物运行 `tests/test_render_lighting.py`、`tests/test_r0_schemas.py`、`tests/test_r1_export_validator.py`，共 **40 passed**；颜色回归检查对旧暗图按预期失败。生产 exporter JAR 不含测试探针。提交前 Oracle 结论与 commit 见会话。

复验入口：`renderLightingProbeJar` 生成独立测试 JAR；与 exporter 一起放进隔离 Fabric 游戏目录，指定 `-Dblockpedia.probe.output=<全新输出目录>` 后加载测试世界，探针运行完会退出客户端。必须检查 `PASS.txt` 存在且无 `FAIL.txt`，再以 `BLOCKPEDIA_RENDER_EVIDENCE=<该目录> python -m pytest -q tests/test_render_lighting.py` 检查真实 PNG。没有 GPU 产物时该项明确跳过，不算通过。原图、纹理及本地量测保留在忽略目录 `build/lighting-v2-evidence/{before,final}/`，不提交游戏资产；远端临时计划任务已移除。

后续 Windows 完整单次导出、特征重算、新标注与候选构建已完成，见第 9 节；正式发布见第 10 节。仍未完成完整导出的双次确定性验证、Linux/Vulkan GPU 验证、形状分类或详情压缩。旧 release 保留原始产物。

## 9. Windows 新导出与候选 release 验证（2026-09-27）

用户在 `D:\Code\blockpedia` 客户端手动完成 `export_20260927T110503Z`，使用 `camera.v3`、`lighting.v2`，记录 1,196 个方块、32,366 个合法状态、1,172 个视觉候选及 24 项跳过。该导出通过当前 Studio 完整导入校验；第 8 节“尚待完整导出”的限制已由本次单次导出更新，双次确定性和 Linux/Vulkan 验证仍未完成。

本次活动 run 为 `run_1e09cb424876425e8d376caa62395951`，import 为 `import_6b156523dea44fd1bac59e65072ea449`。用户改选 5 进程后另建此 run，未改写原 run 的冻结配置；原 1 进程 run 已取消。Windows 实测有 5 个计算子进程、1,172 个特征任务全部成功；与原 run 已完成的 9 项比较，规范化特征和输出 hash 一致。这证明本次 Windows spawn 全量运行与重叠样本一致性，不代替 Windows 各生命周期故障场景的测试。

用户已明确保留 24 项跳过：10 项空渲染、9 项越界、3 项过小、2 项场景不支持；通过正式 review API 写入各自机器失败引用及用户决定。标注沿用 `jianshang / gpt-6-sol`，98 批、并发 5；两批 ID 集合不匹配被拒绝入库，定向重试均成功。1,172 项标注已齐全，原失败 job 记录保留作历史，未重复发送其他成功批次。

质量核查由 Codex 查看实际预览并对照官方名称、选中状态及机器事实完成，记录的 reviewer 为 `Codex`，不称作人类逐项确认。修正了未连接的涂蜡斑驳铜栏杆被描述为多根镂空杆，以及黄色床脚部预览被加入白色颜色词两处语义；保留原始置信度与机器事实。包括既有跳过项及重试闭环，1,220 条 review 均已 resolved；`HUMAN_REVIEW` 已 succeeded，当前 run 为 `succeeded / BUILD_RELEASE / RELEASE_BUILT`。

构建使用已通过 Oracle 审核的 E1 源码提交 `d0251de`，Windows Studio 实际运行目录为 `D:\Code\blockpedia\build\studio-d0251de`。构建 ID 为 `build_ec8397fe58cb4faba5fa15b1de406ca0`，候选 release 为 **`rel_ec8397fe58cb4faba5fa15b1de406ca0`**，目录为 `D:\Code\blockpedia\run\blockpedia-data\releases\26.2\rel_ec8397fe58cb4faba5fa15b1de406ca0`。质量报告的 registry、合法状态、视觉候选、已审核语义与搜索准备五项均 passed；包含 1,196 blocks、32,366 states、1,172 visual variants 与 1,172 annotations。

Windows Node 24.14.0 的真实 stdio 客户端使用候选副本和隔离验证指针执行四工具检查，Schema 与图片返回通过。`infested` 查询的 7 个虫蚀候选分数均为 0.25；`command` 查询的 3 个命令方块分数均为 0.25；完整中英文官方名称免罚并优先，精确 ID 可定位；沙子与沙砾仍为原始分数且没有名单提示。详情返回本地虫蚀风险提示。验证同时修正测试夹具对 Windows `node.EXE` 标准 SQLite 实验性警告的识别；Linux MCP 测试 7 项通过，生产检索逻辑未因此更改。

本地证据在忽略目录 `build/windows-release-evidence/`，包含构建回执、质量报告、release 元信息与 MCP 验证报告。构建及隔离验证阶段未切换正式指针，其前后 SHA-256 均为 `633f6f309f4a95f6673a8e5e0379b1c3a98a36b4140e5ca09ea0348ac19f298d`；后续发布授权和指针变更见下节。

## 10. 正式发布与本机同步（2026-09-27）

用户明确要求“发布正式release并上传到本机”。正式版本为 `rel_ec8397fe58cb4faba5fa15b1de406ca0`，沿用第 9 节验收的同一不可变产物。Windows 通过 `POST /api/releases/publish` 发布，本机 Linux 通过相同 `ActivationService.publish` 在 Studio 写锁下发布；两端均校验原 current token，写入正式发布审计并将 26.2 设为默认版本。两端结果均为 `published`，无 warning 或未完成审计。

Windows release 目录见第 9 节；本机目录为 `/home/ubuntu/.local/share/blockpedia/releases/26.2/rel_ec8397fe58cb4faba5fa15b1de406ca0`。仅同步 release 产物，未复制 Windows 凭据或工作区。传输包 18,127,503 字节，两端 SHA-256 一致：`e60548c645305bc33d2e8446d04ebe04d5c30322063966876c778da1361b4c94`。本机先校验身份及质量报告，再原子安装目录；旧 release 仍保留。

Windows 发布于 `2026-09-27T13:39:47Z`，current token 为 `sha256:7f72b9f1cc5a0b395bbd89b1c91ef228d0eba1df03033c5f4dd469039a79af58`；本机发布于 `2026-09-27T13:40:26Z`，current token 为 `sha256:85f7d57c3c055f8eaad83cb5c86faa8f9a74aab389c45665f2f9a74a27b253aa`。两端均指向相同 release 和 manifest `sha256:e96c53126ee0431b33ba8e2178d6f63efb3041522d4173bb06bafb35e1abc762`。

本机会话最初仍缓存旧查询代码；定位该只读 MCP 进程并通过 App Server 的 MCP 配置重载恢复连接后，当前真实工具调用已验证：`index_info` 指向新 release，质量门通过，1,196 blocks／1,172 visual variants／24 audited skips；7 个虫蚀及 3 个命令方块泛用分数均为 0.25，完整中文官方名称查询为 1；详情含风险提示，搜索、详情、比较均返回图片。四工具均读取正式本机指针，未使用隔离验证指针。

发布回执、传输准备结果与本机真实 MCP 结果保存在忽略目录 `build/windows-release-evidence/publication/`。本次正式发布不表示 B、D 形状分类及 E 其余规划已完成。
