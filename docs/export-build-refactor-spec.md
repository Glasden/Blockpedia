# Blockpedia 导出与构建事实闭环重构 Spec

状态：用户已批准实施，按第 8 节分阶段推进。实现基线：e3ffb55。日期：2026-09-26。

本规格依据用户目标和已经核对的实现行为制定。旧 AGENTS、决策与设计文档只用于识别需要替换的规则，不作为证明旧设计必要性的依据。实施由 D-054 记录授权，逐阶段替换相应规则，不改写历史证据、不发布 Issue、不操作真实用户数据。

## 1. 问题与目标

当前实现把导出输入、workspace 业务结果、阶段证据文件、artifacts 索引、check cache 和不可变 release 同时当作互相证明的对象。结果是一次快照多次读同一个文件、构建重复扫描整包，以及已构建 release 仍依赖当前 workspace。

已核实的例子：

- [构建快照](../src/blockpedia/releases.py)在同一轮分别为解析、checksum 和源文件摘要读取 JSONL；正常首次构建的 index 全文读取路径累计 15 次。该次数是源码推导，不是耗时测量。
- [激活候选选择](../src/blockpedia/activation.py)要求同 run、同当前 workspace 指纹，并在日常发布前复制整个 release、启动 MCP 子进程做 smoke。
- [特征任务](../src/blockpedia/worker.py)每处理一项都重新规划所有任务；[导入进度](../src/blockpedia/importer.py)逐项绕过节流并 fsync。
- [R1 validator](../tools/validate_r1_export.py)与 [Studio 图片模块](../src/blockpedia/features.py)分别维护 PNG 解码器，已复现对同一 CRC 损坏输入作出不同判断。

目标闭环为：

**Fabric 完整导出 → 一次导入验证 → SQLite 中的特征、标注与审核结果 → 一次构建不可变 release → 人工发布或回滚 → MCP 只读查询。**

本次成功标准是删除不必要的状态、文件副本、检查许可证和跨阶段重放，而不只是给原流程增加缓存。正确性由明确的数据所有者、输入验证、事务与原子提交保证；软件实现是否正确由聚焦测试验证。

## 2. 用户故事与保留能力

1. 作为本地用户，我选择一个导出后直接导入，能看到进度和明确错误，不必先取得检查 ID 再执行第二次导入。
2. 作为审核者，我完成标注与人工审核后可以一次构建，不必反复证明同一批历史输入。
3. 作为数据所有者，我已有的标注、人工覆盖和审核历史在重构后继续可用，不被迫重新付费调用 AI。
4. 作为发布者，我可以发布第一个合格 release；第二个 release 只在确有另一份内容或需要回滚时产生。
5. 作为发布者，我可以在原 workspace 被归档或不可访问时发布、回滚已有 release。
6. 作为用户，我遇到断电、响应丢失或磁盘错误后可以明确恢复，不产生重复 release，也不把已切换的 current 误报成未发布。
7. 作为 MCP 调用方，我继续得到同版本、同事实来源的只读查询结果，查询不依赖 provider 或 workspace。
8. 作为维护者，我只维护一份 PNG 解码规则、一份语义重放规则和一份搜索投影规则。

本次继续保留：完整 minecraft 注册表覆盖、合法状态与机器事实来源、机器/AI/人工三层分离、可审计跳过与排除、秘密保护、精确版本隔离、不可变 release、人工切换 current、MCP provider-free 和只读边界。保留这些能力是因为它们直接服务产品用途及数据保护，不是因为旧文档把它们写成了 MUST。

## 3. 数据所有者与检查边界

| 对象 | 唯一权威来源 | 验证发生的位置 |
|---|---|---|
| 原始方块、状态、几何和渲染事实 | Minecraft/Fabric 导出 | 导出提交的基本自检；Studio 首次输入验证 |
| workspace 当前业务结果 | SQLite 已提交的业务行 | 导入、特征或标注结果写入、人工编辑，以及构建所需的关系检查 |
| 任务是否完成 | 与结果同事务提交的 jobs/stage_runs | 提交时及显式恢复时 |
| 已构建内容 | release 目录自身 | 构建提交前的业务验收；发布/回滚仅检查身份、格式和必要文件可用性 |
| 当前对外版本 | current.json | 用户切换时的原子替换；MCP 每请求读取指针 |
| 操作历史 | workspace audit 与发布审计日志 | 对应业务操作记录；不构成另一份业务成功状态 |

### 3.1 本机信任模型

- 一个 data-root 只允许一个可写 Studio 实例，MCP 可以并发只读。复用 run lock；Web 启动在任何恢复或写入之前取得一个标准库实现的进程文件锁，文件位于现有 cache 目录，随进程退出释放。不引入锁服务或协调框架。
- 外部导出、provider 输出、HTTP 输入和路径引用仍是不可信输入。保留严格结构验证、引用验证和 symlink/junction/reparse/hardlink 等路径安全检查。
- 本地导出、应用提交的数据库和已生成 release 不执行篡改检测。删除整包 checksum 复算、历史内容摘要比对、文件 identity/mtime 前后比对、快照指纹和为了证明历史未变而重放输入的逻辑。
- 不验证本地文件“自生成后未被修改”，也不把 hash 声称为真实性或未修改证明。不能把同样的扫描改名为“损坏检测”后继续保留。
- 保留文件实际可读、JSON/PNG 可解析、必要记录齐全、合法状态与引用、SQLite 结构/外键、路径不越界和原子写入等正常业务正确性检查。这些检查不读取历史 hash 来判定内容有没有被改变。

用户已明确确认取消本地个人项目的篡改检测。导入、构建、恢复、发布和回滚均执行这一决定，不另留隐含的全包 hash 门。rename 后第二次全包复验和日常发布的程序级 MCP smoke 同时退出产品流程。

## 4. 导出、导入与 workspace

### 4.1 导出和一次导入

Fabric 继续负责完整枚举、代表状态、游戏内渲染和机器失败记录。保留 fresh staging、基本引用/计数/文件集检查、PNG 基础可读性、fsync 和一次原子提交；停止生成和复算纯防篡改 checksum 清单。确实用于现有内容身份的 digest 在生成时计算一次，不作为后续未修改检查。不在 exporter 内再建立一套完整外部 Schema 验证器。普通渲染修复和工具链版本不因本次重构改变。

Studio 把检查与导入合成一个操作：

1. 校验精确版本、opaque source ref、导出身份、实际文件集合与路径安全。
2. 从源读取文件；同一份读入 bytes 用于 Schema/关系验证和复制到 workspace staging，不核对历史 checksum/digest。图片逐张处理释放，不保留整个图片库的内存缓存。
3. 同一轮解析出的记录用于 SQLite 投影；不要再从 staging 全部读一遍 JSONL。对实际复制内容执行格式与业务检查，不读取 checksum 来证明源内容未被修改。
4. 验证成功后关闭并完成数据库及文件持久化，同卷原子提交 workspace；失败不暴露可运行 workspace。

具体删除：新导入的 cache/import-checks 完整快照副本、snapshot-root/metadata 二次 hash、passed-check 许可证、检查后第二次全包复制。旧缓存只读保留。

保留现有 workspace 的 export/、renders/ 布局；如验证器需要原导出目录名，在 staging 内整理目录用 rename 完成，不能复制第二份数据。

新导入在现有 imports.report_json 中保存最小来源信息：已验证的版本、导出身份、注册表计数及必要策略来源。不新增 registry 指纹或第二套逐记录快照。现有 imports 中纯历史 hash 列保留旧值；新数据不使用的 manifest_sha256/checksum_sha256 列写空字符串而非 SQL NULL，满足已有 NOT NULL 约束；不伪造摘要，也不让这些列参与成功判定。

构建工具链复现属于开发和交付验收；实际运行仍检查受支持的运行时及数据格式，但不再对每个 run 重做全套依赖安装/锁文件证明。

### 4.2 进度与重试

复用现有 importer executor。进度在内存中更新，沿用状态查询/SSE，最多按现有短间隔发送；不逐文件、逐记录持久化进度或 fsync。

一次动作使用稳定 run_id。最终 workspace 存在且身份吻合时，重复提交返回同一结果；仍在处理时返回同一操作。崩溃只剩 staging 时展示中断，用户显式重试后只清理该操作自己的 staging，从头导入。不为恢复进度增加通用 job 层或另一份快照账本。

### 4.3 共用 PNG 实现

提取无服务层依赖的 PNG 纯模块，供 R1 validator、特征提取、联系表和 MCP 图片读取共用。保留 CRC、尺寸、压缩格式、scanline 与 filter 的一致检查；质量判断留在 R1 validator。

去掉 package 初始化对 services/provider 的 eager import，调用方显式导入所属模块，不引入 lazy-import 框架。既有独立 R1 命令继续可用，不因复用 decoder 而要求加载 HTTP/provider 运行时。

只要求同一次验证共享读取与解码结果。后续特征计算或组图实际需要像素时仍可解码，不建设跨阶段图像缓存。

### 4.4 SQLite 作为已完成结果的事实源

不改变现有 workspace SQL 结构，不重建用户数据库。保留旧列是为降低迁移成本，不继续把所有旧列当作必要机制。

- 特征、variant 投影、job 成功状态和必要 audit 在同一事务提交。
- annotations、provider_requests、annotation refs、review tasks 和 job 终态在同一事务提交。
- 停止新写 generated/stages、generated/features、generated/ai 中的业务结果副本，停止为这些副本写 artifacts 行；旧文件和行不自动删除。
- stage cursor 只存恢复需要的小字段，不嵌入完整 evidence、所有 item 输出及其汇总 hash。
- 既有 feature/AI 输入身份和必填摘要按现有算法生成一次，用于重试、缓存或引用身份；不再借它们证明另一份 JSON 文件存在。
- 恢复以已提交结果为准，孤立文件不能晋升为成功事实。未知 AI 发送结果仍须人工处理，不自动重发。
- AI 的授权、发送前输入核对、输出 Schema 和最多一次外部请求重试继续保留。构建不再重新生成全部历史提示词、联系表和 wire payload。
- 构建只读取冻结的离线 provider 来源信息，不依赖当前 active profile、Keyring、网络或能力 probe。
- 语义覆盖重放、annotation 输入身份及搜索文本各共用一个纯计算实现，删除分别为“生产”和“证明生产结果”维护的第二套实现。

EXTRACT_FEATURES 在首次进入或显式恢复时一次性协调缺失 jobs；单 item 执行只领取自己的任务。任务规划总工作量为 O(N)。本地确定性计算可在显式恢复时重做未提交项；恢复计数字段只保留历史统计含义，不把耗尽恢复次数变成必须重新导入的门。

旧 stage 枚举不迁移：导入相关五个标签由一次成功导入完成；已有 pending 标签根据真实已提交 import 结果前进。独立 VALIDATE 收敛到结果写入和构建关系检查。HUMAN_REVIEW 保留真实人工决定。BUILD_RELEASE 表示构建，ACTIVATE_RELEASE 仅保留旧历史行，新发布状态从 current 派生。不得为这些接线增加可配置工作流或版本引擎。

## 5. 单次构建与独立发布

### 5.1 构建动作

一次构建持有现有 run lock，确认没有 live feature/AI 工作。所有修改同 run 的应用入口必须使用同一把锁。先记录本次 build/release ID，再取得一次 SQLite 一致读视图；不再通过两次或三次全 workspace 指纹检查证明期间没有应用写入。

构建仅检查发布实际需要的内容：

- 全部已导入 Block 被投影到 release，数量与导入结果一致，无重复 ID，状态和引用合法，机器记录通过其结构检查；不通过重放原始导出或比对 registry hash 检测数据库变化。
- 每个 Block 有有效视觉变体或有真实审核的 skip；excluded 有完整资格审核，conditional 有警告。
- AI/人工语义完整、目标和来源引用有效，必要审核已关闭。仅 workspace FTS 派生视图的失败不阻断新 release 自行建立 FTS。
- 图片实际可读、可解码且符合输出规格；输出路径、Schema、SQLite 完整性/外键和发布搜索索引可用。不将图片 bytes 与历史 digest 比较。

用同一个投影定义写出 fresh v2 index 和人工记录包。对 staging 做一次业务验收，形成一份构建报告，完成文件及目录 fsync 后执行不覆盖目标的同卷原子 rename。不为此生成全文件 hash 清单。

同一文件已得到的解析值和文件清单在本轮复用。报告 evidence 只引用本轮已检查集合，不因在五项检查中出现就全文读取五次。manifest/quality 的现有对外标识摘要在写出对应小型元数据时计算一次，不用于后续重读校验。

rename 成功即形成 candidate。后续只补 workspace cursor/audit，不再全包读取、hash 或重新生成完整投影期望值。磁盘/目录 fsync 失败必须报告持久化结果待核对，不能仅凭异常推断 final 不存在。

删除独立 release check API、最新 check 排序、check cache、immutable check report、snapshot_fingerprint、repeat build 对首个 candidate 的证明链，以及 rename 后全包复验。

### 5.2 构建身份与恢复

复用 release_build_id，仅作为一次动作的重试身份。前端为新动作生成 ID，响应丢失或重试沿用该 ID；用户明确再构建才生成新 ID。继续按该 ID 得到确定的 release_id，不再要求先生成 check_id。

已有 BUILD_RELEASE cursor 保存正在处理的 build ID。另一个新 ID 不能覆盖尚未收尾的构建，须先恢复它。身份相同且 final 已存在时读取其 build/run/version、构建成功标记和必要文件可用性并补事务，不扫描图片或复算 hash，不创建第二个 final。已存在但身份不匹配、必要元数据无法解析或 index 无法打开时拒绝，不覆盖也不静默分配新 ID。

没有 final 的失败构建可以用同 ID 显式重试；恢复或再次构建都不需要重新调用已成功的 AI。

### 5.3 release 及 hash 删留

新 release 保留六项：release.json、manifest.json、index.sqlite3、previews/、quality_report.json、manual-overrides.json。不再生成 checksums.sha256 和 schemas.sha256；旧 release 中的这两份文件原样保留，但新代码不依赖或验证它们。v2 SQLite 查询投影与 MCP 输出格式保留；scalar/index 列与完整 JSON 只有一份生成定义。

| 保留且有具体用途 | 停止产生或依赖 |
|---|---|
| 已被 feature/AI 内容身份实际使用的摘要 | 源导出/release 整包 checksum 清单及复算 |
| AI 重试/授权需要的输入身份 | stage evidence、snapshot-root、metadata 套娃 hash |
| MCP 输出仍引用的 manifest/quality 摘要，生成一次 | 同一 source/artifact 在多张 functional map 中重复列举 |
| 明文来源 ID、生产者/工具链/Schema 版本 | Schema 文件 hash inventory、provider/策略的重复摘要 |
| 历史记录中已有的 hash 值原样保留 | manifest/quality/图片的历史 hash 一致性 gate |
| current 的单个低成本并发比较 token | 为 UI 进度持久化的逐 item 证明 |

同步调整 exporter/release manifest 的本地 Schema：纯 checksum/inventory/functional hash map 描述不再 required，新生产者不写这些无消费者字段；旧包带有这些字段仍可读取，不建立双流水线。保留现有 Schema ID 和业务记录形状，这是对本地元数据要求的兼容性放宽；不是为旧 MUST 保留空壳清单。工具链与来源用既有明文版本/身份记录，D-045 provenance 保留原始来源数据，不再派生新摘要。

quality_report 只保留一份 release 报告：format_version 改为 2，移除 snapshot_fingerprint 和 hash 验证项目；保存 build/release/run/version 身份、built_at 与实际执行的业务检查结果。旧 format-1 报告按旧形状读取，不重新计算其 workspace 指纹，也不把旧 hash 项目冒充本轮验证。格式定义只在报告读写实现中维护，不再把逐字段列表复制到多份规范。

### 5.4 发布与回滚

用户对目标 release 执行一次确认后的发布操作，不再有 activation-check 和 apply 之间的许可状态。

持有 current-switch lock 后，比较页面提供的 current token，拒绝陈旧页面覆盖。目标检查仅为路径安全、release/version/build 身份匹配、必要元数据可解析、构建报告记录成功、index 格式受支持且可打开；不遍历全文件、不读全部 PNG、不做 checksum、manifest/quality hash 比对或全量 index 投影检查。

发布不打开 workspace，不重放语义或 FTS，不读取 provider，不枚举并验证所有候选。首次发布允许只有一个已成功构建的 release。回滚复用同一切换函数，目标必须是另一份已有且通过上述可用性检查的 release；两个 release 不再是首次上线的前置条件。

四工具 MCP smoke、复制到临时 data-root、启动测试子进程退出日常发布路径，归入本次集成验收。实际发布只保留上述可用性检查和原子 current，不声称验证了文件未变或重新验证了整套程序。

current 保留多版本语义；第一次发布必须设为默认，后续请求显式决定默认版本。其他版本指针不因切换被覆盖。MCP 下一请求观察新指针，仍不写本地文件。

### 5.5 发布审计与提交点

发布审计移到既有 logs 目录下一个追加文件 release-events.jsonl，不写入 immutable release，也不依赖来源 workspace。记录操作身份、操作者、原因和 old/new pointer；不建立 per-operation state 目录、收据文件或审计 hash 链。

先持久化意图，再写临时 current、fsync 并原子 replace，最后追加结果。意图写入失败时不切换。replace 已发生而结果日志失败时，必须返回“已切换，审计待完成”，不能声称未发布。

下次切换前先处理尚未完成的意图：current 匹配 new 则补记已执行，匹配 old 则补记未执行，两者均不匹配则报告人工核对；恢复不自动改指针。启动仅显示待处理项，用户显式恢复或下一次发布操作才执行审计收尾。

## 6. 接口与用户流程

下表定义产品接口变化，不新增 Python 写操作子命令。请求继续严格校验并要求精确 Minecraft 版本。ID 复用现有格式，source ref 必须是 chooser 发出的 opaque 引用。

| 接口 | 请求要点 | 结果 |
|---|---|---|
| POST /api/imports | run_id、minecraft_version、source_directory_ref | 异步导入；返回稳定 run_id，最终结果包含 import_id |
| GET /api/imports/{run_id} | 路径中的 run_id | 当前进度、成功 workspace 或中断/失败；无额外检查许可证 |
| POST /api/releases/build | run_id、minecraft_version、release_build_id | 一次检查并构建；返回 release_id、built_at 和结果 |
| GET /api/releases | minecraft_version | 轻量 release 列表及 current 比较 token，不扫描所有图片/index 内容 |
| POST /api/releases/publish | minecraft_version、target_release_id、expected_current_sha256、confirm=true、set_as_default、reviewer、reason | 验证目标并原子切换 current |
| POST /api/releases/rollback | 与 publish 相同 | 调用同一实现，仅审计动作不同 |

首次 current 不存在时 expected_current_sha256 为 null。该 token 只用于单个小指针的乐观并发控制，不再派生第二套发布身份。

首次 build 成功返回 201；同 ID 已提交结果返回 200；异步 import 返回 202。版本/身份/陈旧 current 或未收尾的并发动作冲突返回 409；输入形状错误沿用 422。内容门失败返回可定位的检查结果，不创建 final；基础设施错误保留稳定错误码并明确是否越过提交点。已经切换 current 但审计待完成的结果必须包含 applied=true 和明确 warning。

移除 imports/checks、releases/check、activation-check、旧 apply 及相应 HTMX 动作。旧请求明确失败，不静默转换成发布。UI 改为“导入”“构建”“发布/回滚”，用户不再操作 check ID、snapshot 指纹或派生证据文件。

## 7. 现有数据与范围边界

- 不清空数据库、不重新调用已成功 AI、不重写历史 annotations、overrides、provider_requests、reviews 或 audit。既有 generated 文件、artifact rows 和 check caches 保留为历史，新流程不再读取它们证明业务成功；清理由用户另行决定。
- 新导入的来源摘要在 imports.report_json 中随事务写入。旧 workspace 没有该摘要时，从已有 workspace export manifest 读取并核对最小来源信息，在本次操作内复用，不回扫全部 JSONL/图片，也不改写原有 D-045 provenance。不能把当前数据库自己的计数当成原始 registry 证据。缺失必要来源证据时明确报告，不能伪造通过。
- 旧 running 任务仍需显式 recover。已到构建/旧激活边界的 run 可直接进入新构建；旧阶段行和审计不删除。run 加工结束与是否发布分开显示。
- 已有 v2 release 可在 workspace 和旧 check cache 不存在时发布/回滚。v1 release 原样保留；其缺失当前查询所需投影时，应从保留的 workspace 构建新的 v2，不增加 v1 查询适配器。
- 旧 checked snapshot 如尚未导入，用户可先把其中完整 export 目录放入所选版本的 exports，再通过普通入口重新验证；不继续维护旧 check_id 协议，也不增加任意路径输入或自动改写旧 cache。
- 旧构建若已 rename、尚未完成 cursor/cache 收尾，只按已有确定性 build/release 身份恢复，不重建旧验证流水线。
- D-045 已完成 refresh 的混合来源、32 个目标结果及既有标注保留。继续检查来源身份、target exclusion 等必要关系；不在每次 build 重生成历史图片和 prompt，不改写历史 export ID 或请求签名。
- 已确认移除 banner-refresh 入口、对应 WebUI 动作、专用 exporter banner-repair 命令及其仅服务补救流程的实现。普通 exporter 的公共渲染修复保留：ExportPackage.renderVariantStep 调用 RenderExporter.render，后者的公共 renderState 对 BannerBlock/WallBannerBlock 应用中心缩放 0.72，普通 prepare 也使用 camera.v2/banner policy。修复不依赖 repair 模式。此次以当前源码调用链确认，未重跑 Minecraft。新的机器来源建立新 workspace，不在本次引入通用跨导出合并能力。存在未完成 refresh journal 时，先通过原恢复路径完成或回退，不能忽略它或删除 backup。
- 新程序写入后，不承诺旧程序能继续处理该 workspace。实施前保留用户数据备份；代码降级恢复备份，不能并发混跑两个写入者。

不在本次范围：重写 Minecraft 渲染、改变机器事实算法、重新设计 provider 协议/授权/并发、改变 MCP 工具与搜索语义、引入服务/依赖/迁移框架、泛化增量数据合并、重新生成真实索引、自动发布生产 current。

## 8. 实施顺序与验收

### 8.1 可独立验收的提交阶段

第一项已独立提交。实际接线显示第 2–4 项共享旧 check 身份、artifact 消费与 release 布局，分别提交会使中间版本不可用或需要新增过渡兼容层。因此这三项按下述依赖顺序实现，并作为一条完整新业务链集中验收/提交；各项验收范围保持，不以合并提交缩减目标。

1. **统一 PNG 与局部重复执行。** 共用 decoder、解除 eager import、修复 O(N²) 任务规划和逐项进度落盘；现有业务闭环继续工作。
2. **合并导入并统一 SQLite 结果权威。** 一次导入/复制、来源摘要、旧 stage 接线与恢复；同时更新现有构建器、AI/审核和 D-045 历史消费者，再停止派生 JSON 写入。不能让生产者先停写、消费者留到下一阶段才修复。
3. **替换单次构建。** 接通新 build API/UI、唯一 staging 业务验收、构建恢复；删除 check cache、全量 snapshot、纯 hash 清单与篡改检测，同步放宽相关元数据 Schema。完成后旧 workspace 能产生新 release。
4. **替换发布与回滚。** 单 candidate 发布、目标可用性检查、独立审计与恢复；移除整包 hash gate、日常 smoke 和旧 activation API，运行一次从新构建产物到 MCP 的集成验收。

每阶段同步替换直接相关的旧规则和测试，不保留新旧两套 MUST。历史 decisions 保留并标注适用范围被本规格替换；不改写旧证据，也不把重构完成推定为真实数据已重新构建。按仓库提交流程独立验证、Oracle 审核后提交，不 push。

### 8.2 最小证据路径

主验收入口使用真实 Web 应用和临时 data-root，调用真实 Studio 服务、使用既有小型 exporter fixture 与 fake provider。不能只用 HTTP stub 服务证明整条闭环。只有 PNG 和重复读取计数使用低层 focused checks。

| 要证明的行为 | 最小验收 |
|---|---|
| 输入安全与 PNG 判定一致 | 同一合法 PNG、CRC/截断/filter 错误在各消费者结论一致；缺失必需记录、非法引用、跨版本与不安全路径被拒绝；不测试“修改后 hash 不一致必须拒绝” |
| 导入无第二次复制与进度写放大 | 小型 fixture 记录实际 source 读取/复制/解码次数；重试得到同 run，进度回调不逐项 fsync |
| SQLite 提交决定结果 | 特征/AI 事务前失败与提交后响应丢失各一例；去掉派生 JSON 后仍使用已提交结果，不重复调用 provider；N 个特征只规划一轮 |
| 旧数据继续可用 | 旧已审核 workspace 加 D-045 mixed-lineage fixture；annotations/overrides/audit 保留，来源摘要可核对，缺来源不能假通过 |
| 一次构建与恢复 | 经 HTTP 构建有效 release；坏引用、未审核 skip/excluded 阻断；rename 前失败无 final，rename 后收尾失败同 ID 只补事务 |
| release 与 workspace 解耦且没有篡改检测 | 单个 candidate、workspace 不可访问时仍可发布；旧 checksum 改变或新包没有 checksum 文件不触发检测；必要元数据不可解析或 index 无法打开仍拒绝；回滚不改 release bytes |
| 移除 banner 补救不影响正常导出 | 保留公共 renderer 的 BannerBlock/WallBannerBlock 居中缩放与普通 export 调用链；删除 repair/refresh 后做一次相关构建及普通路径聚焦验证，不重做旧补救流程矩阵 |
| 原子 current 与审计诚实 | 陈旧 token、replace 前失败、replace 后 audit 失败各一例；current 保持旧值或完整新值，恢复不重复切换 |
| MCP 使用真实构建产物 | 对本轮构建出的临时 release 跑一次四工具 stdio，验证 provider-free、零持久写、下一请求观察指针切换；日常 publish 的 smoke 调用数为零 |

不新增真实基数 fixture、全量性能矩阵、持久化观测平台或多份证据报告。读取计数按输入对象验证，不以机器相关的耗时阈值代替正确性。复用未受影响的已有证据；受影响的原子文件提交与进程锁分别在 Windows/Linux 做聚焦验证，缺失平台证据如实标明。

### 8.3 已确认的产品决定

2026-09-26，用户明确确认取消本地个人项目的篡改检测，并允许在普通导出已经包含 banner 修复时移除专用补救功能。当前普通导出已经接入公共 banner 修复，因此两项均纳入本规格，不再作为待确认项。

其余设计已有明确默认：一个合格 release 可以首次发布；不保留 check 许可证；保留用户业务数据；保持当前 MCP 输出格式；不新增通用框架。用户随后明确要求开始执行并阶段性 commit；实施时统一替换对应旧规则，不逐条重复申请旧契约豁免。
