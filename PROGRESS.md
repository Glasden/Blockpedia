# Blockpedia：建筑选材工作流与新 release

更新：2026-09-27。代码调查基线：`44233c2`。本文件是当前决定、实施顺序和进度的唯一维护入口；旧文档已归档，不再与本文并行维护。

**用户已于 2026-09-27 授权开始实施，D1 exporter 颜色修复、C 特征多 worker 与 E1 推荐名单降权已实现。** 其余接口和参数仍是后续实施目标，并非当前已有能力。256×256 无损 WebP 决定已获用户批准；用户随后授权 Windows 导出至构建流程，实际进展见第 9 节，发布仍未获授权。

## 1. 已确认决定与范围

| 决定 | 本轮落实方式 |
|---|---|
| 新建 release，不考虑旧数据兼容性 | 新导出、新 workspace、新标注、新 release；不实现旧 Schema 适配、数据迁移或跨导出合并。保留旧数据文件，不原地改写历史 release。 |
| 明显技术方块降权，具体名单由用户审核 | 第 2 节的 18 项名单已获用户批准；不直接删除方块或批量伪造资格审核记录。 |
| 确定 details 的 8KB 口径，合理处理状态枚举；最终 MCP 输出采用 256×256 无损 WebP | 第 3 节采用紧凑摘要、真实状态分页；仅最终图片响应缩采样与转码，处理过程保留原图。 |
| Studio 增加可选多 worker | 第 4 节限定为同一 run 内特征计算并行；复用已有 AI 批次并发。 |
| 保持简单，归档其他旧文档 | `docs/` 原有 20 份 Markdown 统一归档；不新建规格、决策、路线图三套重复记录。 |

继续保留的产品边界：Minecraft Java 26.2；Fabric 导出运行时事实与原图；Python Studio 处理离线数据；Node MCP 只有四个只读 stdio 工具，在线不调用 provider。SQLite 已提交业务行、release 目录、`current.json` 分别表示工作结果、构建结果和发布结果。单 data-root 一个可写 Studio；保留外部输入和路径校验、事务、原子提交、provider 批准与未知发送结果人工处理。已取消的本地防篡改检测、专用 banner repair 和双 release 发布门不恢复。

计划建新 release 不代表本轮已获准执行付费标注或切换 `current.json`。新工作区按完整流程生成标注与审核结果，不假定旧审核或少量补标能自动满足新构建。实际 provider 批次沿用 Studio 的批准流程；构建后由用户明确发布。新格式上线时配套更新 MCP 与 Schema，不要求新版读取旧 release；不承诺跨格式回滚，旧数据继续留存。

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

不默认纳入 `bedrock`、`spawner`、`trial_spawner`、`vault`：它们仍可能被明确用于建筑或装饰。重力、融化、蔓延、支撑等其他行为风险继续作为事实与使用条件保留，本次不新增统一降权，也不把缺失事实推断为安全。旧稿“bedrock 必须不出现”的验收不采用。

E1 实现：Node MCP 与 Studio 工作区查询均接入以下固定规则；不改动导出机器事实、AI 标注或 qualification。

- 名单采用一个固定 ID 集合，不做可配置规则引擎。用户已批准该名单，后续实现可接入默认排序。
- 泛用途检索中，对名单内已召回候选的相关性分乘 `0.25`，在 Top-N 截断前执行；`local_score`、`final_score` 返回降权后的分数，理由说明名单降权，原匹配分项保持其匹配含义。
- 精确完整 block ID 或官方中英文全名查询保留名称优先，不施加这项泛用途惩罚；子串和同义词命中不算精确查询。并列按稳定 `variant_id` 排序。
- 既有 `excluded` 过滤仍有效；精确查询也不恢复 excluded 或没有预览的搜索候选。不为名单中缺少预览的条目伪造图像。
- 搜索和详情分别提供固定的技术／特殊用途提示、虫蚀风险提示；提示是本地推荐规则，不冒充新增运行时行为事实、AI 结论或人工 qualification review。`get_block_details` 仍能查询已登记的这些方块。
- 验收同时覆盖泛用途降权、精确名称可找到、非名单候选分数不被误改、名单内无视觉候选不被新增，以及 FTS/LIKE 两条路径。

验证：Studio 核心 28 项、Web/构建/MCP 集成 46 项通过，涵盖 0.25 系数、Top-N 前排序、精确 ID／官方名称豁免、子串和同义词不豁免、稳定排序、其他风险与非名单方块不误降，以及 excluded／无视觉候选不恢复。实现不新增依赖、规则引擎或配置账本；提交前审核与 Windows 交付结果见会话。

## 3. 方块详情与图片方案

### 3.1 默认摘要：严格小于 8000 字节

输入增加 `detail: "summary" | "states"`，默认 `summary`。计量公式固定为：

```js
Buffer.byteLength(JSON.stringify(structuredContent), 'utf8') < 8000
```

这是十进制 8KB，测完整成功 envelope，包括版本、release 身份、警告、数据与图片元数据；不计协议中重复的 TextContent、JSON-RPC 外壳或 ImageContent/base64。状态页不受此 8KB 上限约束。传输总字节和图片字节在验收时另报，不能把排除图片解释为整个响应小于 8KB。

摘要保留：官方名称、默认及 canonical 完整状态 ID、属性定义、tags、状态总数、代表状态的几何与碰撞、资格与警告、完整有效语义投影、图片映射。完整语义指已发布的语义字段，不包含 provider 原始响应、提示词或任务历史。当前一个 block 对应一个默认代表；几何和图片明确关联该 canonical 状态，不声称代表所有状态的形状。

摘要不返回 `states`、`represented_state_ids` 或逐状态 `state_behaviors`，也不枚举全部几何签名分组。精确状态 ID 保持完整，不删除 `shape=straight` 等派生属性。尚未实现的支撑事实可以从 MCP 输出省略，不能转换成 false 或“无需支撑”；其他已知行为事实与警告保留。

不得通过静默截断语义或丢弃警告凑够字节。以楼梯、活板门为硬断言样本，并扫描整个新 release 的摘要尺寸；若完整有效语义导致其他样本超限，在发布前调整字段边界或标注生成约束并复验，不隐瞒超限。

### 3.2 按页读取真实状态

`detail="states"` 时增加 `offset`（整数，默认 0，非负）与 `limit`（整数，默认 8，范围 1–16）；summary 模式不接受这两个参数，避免静默忽略输入。

- 直接读取 release 的真实 `states`，按完整 `state_id` 的 UTF-8 字节序排序。每项保留属性、几何、碰撞、行为和必要的变体映射，不重复返回另一份 `state_behaviors`。
- 每页返回 `block_id`、`total`、`offset`、`next_offset` 与状态数组；末页及越界空页的 `next_offset=null`。状态页不附带图片，也不重复完整语义摘要。
- 每页沿用 envelope 的版本与 `resolved_release_id`。客户端跨页发现 release 改变，丢弃已收集页面并从第 0 页重取；不增加服务端游标、历史 release selector 或快照服务。
- 不从属性笛卡尔积推测合法状态，不截掉后续状态。验收读取所有页后与数据库真实状态集合一致，无重复、遗漏，并验证页边界、非法参数和发布切换。

### 3.3 图片：最终 MCP 输出采用 256×256 无损 WebP（用户已批准）

用户于 2026-09-27 明确批准同时缩分辨率和更换编码：每个方块的整张四视角预览由 `512×512` 缩为 `256×256`，每视角 `128×128`，采用 nearest 缩采样后编码为无损 WebP。无损指编码保留缩采样后的像素，不表示缩采样本身无损。该决定替代原 PNG 方案和第 3.5 节曾建议的 512 原尺寸 WebP。

转换只发生在 MCP 最终图片响应组装处，不新增 Studio 流水线阶段。Fabric 导出、Studio 导入、特征提取、AI 标注与 release 中存放的预览、mask 均继续使用原图；MCP 读取原图后，在发送前缩采样并编码，不改写原文件或落地一份中间缩图。

`get_block_details` 返回 256×256 的方块卡片；`search_blocks`、`compare_blocks` 的联系表按每张方块卡片 256×256 排列，不将多候选整张联系表挤成 256×256。联系表标签按最终尺寸绘制，组合完成后统一编码为一张无损 WebP，不逐卡片重复转码。状态页仍不附图。

输出 MIME 统一为 `image/webp`；Schema、实际 ImageContent、尺寸、image ID、摘要及映射均与最终 WebP 字节一致。采用满足目标平台的最小编码依赖，不手写 WebP 编码器，不增加质量滑块、格式协商或持久图片缓存服务。

图片仍独立于 8KB 文本预算。验收报告字节与编码耗时，检查客户端实际接收、透明度、联系表标签和四视角可辨识性；无损解码后的 alpha 与可见 RGB 应与缩采样结果一致，原文件不变。若发现 256 图细节问题，修正采样或排版并复验，不能静默回退到 512/PNG 或有损编码后宣称满足决定。当前仅方案获批，功能尚未实施。

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

12 张原尺寸无损 WebP 解码检查通过：alpha 与所有 alpha 非零像素的 RGB 均保持原值；未要求保留全透明像素的隐藏 RGB。256 WebP 的最终 MCP 客户端接收、Schema/MIME 接线、编码时间及目标平台依赖仍待实施验证，不能由字节数推断视觉质量合格。原始渲染、mask 和特征计算继续使用原始数据；仅转换 MCP 响应节省的是传输体积，不等于已有 release 的磁盘占用减少。格式决定已批准，尚未实现。

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
| 渲染偏暗 | D1 已修复继承 GUI 方向光的问题，使用固定的原版 DEFAULT/LEVEL 双灯 diffuse 预设，详见第 8 节。不是统一补亮，也不宣称采用 terrain 的 0.8/0.6 面阴影系数。完整重新导出与特征重算尚待后续执行，旧稿统一 L*≥85 不作为门槛。 |
| 形状分类 | 优先将已有 stairs/slabs/walls/fences 等 registry tags 映射到统一分类；只有缺少事实的类别才补 exporter 最小运行时分类。查询词与实际分类对齐。 |
| 搜索相关性 | 保留现有 FTS/LIKE 宽召回，改进短语、词覆盖率与字段加权；英文按词边界，名称/同义词高于用途/材料/风格，再高于摘要。补中文颜色与常用用途映射，材料真正参与评分；不新增在线模型。 |
| `avoid_for` | 只从正向索引文本排除。暂不拆分标注字段或重做受控词表：没有旧兼容要求，也不意味着必须扩大本次 Schema 改造。保留共享语义投影/人工覆盖功能，不把含义混杂的历史字段直接用于负向惩罚。 |
| 资格与风险 | 技术名单及用户追加的 7 种蠹虫方块按第 2 节降权；已有 conditional 警告保留。其他行为风险与技术名单分开，未知事实明确未知，不声称完成全量风险审核。 |
| 建筑查询集 | 约 20 条中英真实需求，覆盖光滑石墙、深木屋顶、百叶/窗框、白色墙面、旧苔墙、木梁、栏杆、窗台线脚、暖光。每条列可接受的前三候选集合及反例；实施时定稿，作为本轮相关性回归门，不锁死唯一名次。 |
| 比较字段 | P1 后续：优先复用已有颜色、亮度、纹理和语义字段；届时同时处理目前未使用的 `context`/`compare_states`，不在本轮文档中宣称已支持。 |
| 图上名称、搜索输出精简 | P1 后续：名称标签需实际可读；输出先测量，保留可用的事实追溯方式。 |
| 分面颜色、多主色、相似色检索、系列形状、16 色归并、3×3 平铺 | 本次暂缓，不作为新 release 的完成门。 |
| 删减推荐状态属性 | 不采用；完整合法 canonical 状态 ID 保留。 |

新 release 产出链：渲染定位及修正 → 完整导出 → 新 workspace 导入 → 特征与新标注 → 审核 → 构建 → 四工具集成验收 → 用户发布。无需迁移旧语义和审核；新的非 excluded 候选仍须满足构建的语义完整性要求。

## 6. 实施阶段与当前状态

各阶段应形成独立可验收的提交；实现、验证、Oracle 提交前审核通过后才提交，不 push。下表是依赖关系，不要求为等候外部 GPU 或名单审核而阻塞无依赖的工作。

| 阶段 | 交付物与依赖 | 验收负责人及最小证据 | 状态 |
|---|---|---|---|
| A 文档收敛 | 本文件、旧文档归档、必要引用接线 | 主代理：归档完整性、有效链接、原稿和 evidence 保留、候选提交审核 | 文档已整理；18 项名单已获用户批准；提交状态见会话 |
| B 详情与 MCP 图片输出 | summary/states、分页；三种带图工具最终响应采用256卡片/无损WebP，处理过程使用原图 | 实现者：两个摘要尺寸断言、全 release 大小扫描、分页集合一致、Schema/MIME、图片元数据、无损解码、客户端及视觉检查 | 未实施；图片方案已批准 |
| C 特征多 worker | 第 4 节配置与进程池；可与 B/D 独立开发 | 实现者：串并行结果一致、生命周期/故障检查、同 run 并行证据及真实计时 | 已实现；默认 1。Windows 本次已用 5 个实际子进程完成 1,172 项特征；Linux 生命周期与计时见第 4.1 节 |
| D 渲染与形状事实 | D1 颜色修复先独立交付；形状分类、新完整导出继续待办 | 实现者：Java 构建、真实 GPU 样本、分类来源检查；主代理核对完整导出验证 | D1 代码及定向 GPU 验证完成，见第 8 节；阶段整体未完成 |
| E 搜索与名单 | 相关性、中文映射、技术及虫蚀降权；名单已获用户批准，最终颜色/形状验收依赖 D | 实现者：约20条查询、FTS/LIKE、精确与泛用途查询对照、稳定排序 | E1 技术及虫蚀降权已实现；广义相关性、中文映射与完整建筑查询集仍未实施 |
| F 新 release | 整合 B–E，新 workspace 完整处理并构建 | 主代理：构建通过，真实产物 summary 扫描、查询集、四工具 stdio 和状态引用一致；provider 结果独立取证 | 未执行；无新 release ID |
| G 发布 | F 验收后由用户明确发布 | 主代理：实际指针切换及下一次 MCP 查询指向新 release | 未授权执行，未发布 |

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

剩余边界：未完成全注册表新导出、完整导出的双次确定性验证、Linux/Vulkan GPU 验证或新 release 构建。旧 release 不会因代码修复自动变亮；后续仍需新导出、特征重算与新标注。本阶段没有顺带实施形状分类、搜索、详情压缩或多 worker。

## 9. Windows 新导出与构建准备（2026-09-27）

用户在 `D:\Code\blockpedia` 客户端手动完成 `export_20260927T110503Z`，使用 `camera.v3`、`lighting.v2`，记录 1,196 个方块、32,366 个合法状态、1,172 个视觉候选及 24 项跳过。该导出通过当前 Studio 完整导入校验；第 8 节“尚待完整导出”的限制已由本次单次导出更新，双次确定性和 Linux/Vulkan 验证仍未完成。

本次活动 run 为 `run_1e09cb424876425e8d376caa62395951`，import 为 `import_6b156523dea44fd1bac59e65072ea449`。用户改选 5 进程后另建此 run，未改写原 run 的冻结配置；原 1 进程 run 已取消。Windows 实测有 5 个计算子进程、1,172 个特征任务全部成功；与原 run 已完成的 9 项比较，规范化特征和输出 hash 一致。这证明本次 Windows spawn 全量运行与重叠样本一致性，不代替 Windows 各生命周期故障场景的测试。

用户已明确保留 24 项跳过：10 项空渲染、9 项越界、3 项过小、2 项场景不支持；通过正式 review API 写入各自机器失败引用及用户决定。标注沿用 `jianshang / gpt-6-sol`，98 批、并发 5；两批 ID 集合不匹配被拒绝入库，定向重试均成功。1,172 项标注已齐全，原失败 job 记录保留作历史，未重复发送其他成功批次。

质量核查由 Codex 查看实际预览并对照官方名称、选中状态及机器事实完成，记录的 reviewer 为 `Codex`，不称作人类逐项确认。修正了未连接的涂蜡斑驳铜栏杆被描述为多根镂空杆，以及黄色床脚部预览被加入白色颜色词两处语义；保留原始置信度与机器事实。包括既有跳过项及重试闭环，当前 1,220 条 review 均已 resolved；`HUMAN_REVIEW` 已 succeeded，运行停在 `R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING`。发布 `current.json` 未获授权，本次不切换发布指针。
