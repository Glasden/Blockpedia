> 历史归档（2026-09-27）：停止维护，不再作为当前执行规范。当前决定与进度统一见 [PROGRESS.md](../../../PROGRESS.md)。正文只做归档链接修正，旧状态不代表本轮完成。

# 导入、工作结果、构建与发布

当前流程由 [D-054](decisions.md) 和 [重构规格](export-build-refactor-spec.md) 定义。旧 check cache、全量 snapshot 指纹、派生结果 JSON、双 candidate 发布门和发布时 MCP smoke 已退出当前设计；历史数据不改写。

## 数据所有者

- Fabric 拥有注册表、合法状态、代表状态和游戏内渲染。Studio 不重选、不重渲染。
- workspace SQLite 的已提交业务行拥有当前机器投影、特征、标注和人工审核结果；结果与 jobs 状态同事务提交。
- 已构建内容属于独立 release；发布不再重新读取来源 workspace。
- current.json 是当前对外版本的唯一事实。日志和工作阶段不能否定已经完成的原子替换。

高层目录仍为 exports/、workspace/、cache/、releases/、logs/ 和 current.json，按精确 Minecraft 版本隔离。MCP 只读 current 和其指向的 release。

## 一次导入

WebUI 从版本目录取得 opaque source ref，为一次操作创建稳定 run_id。导入执行器顺序完成：路径与格式检查、读取并复制、SQLite 投影、持久化、同卷原子提交 workspace。

同一份读入 bytes 用于校验和复制；解析出的记录直接用于投影。PNG 逐张释放，不缓存整个图片库。不生成 cache/import-checks 快照，不在检查后复制第二次，不比较历史 hash。旧 checksum/inventory 字段可以读取，但不成为未修改证明。

进度仅在内存中观察；请求、SSE 和列表读取同一操作状态。成功以最终 workspace 和其中 imports/runs 记录为准。崩溃只剩 staging 时展示 interrupted；用户显式重新选择来源并用同 run_id 重试，清理该操作自己的 staging。已有 final 不覆盖。

导入摘要保存来源 ID、精确版本、注册表计数及必要生产者版本。旧 workspace 可以读取原 manifest 的小型来源信息，不扫描原 JSONL/图片来证明数据库未变。缺少必要来源信息时报告真实错误，不从当前数据库反推原始覆盖证据。

## SQLite 结果与恢复

新操作不生成 generated/stages、generated/features、generated/ai 的结果副本，也不新增这些副本的 artifacts 行。历史文件和行保留，不再用于证明成功。

特征计划只在进入阶段或显式恢复时协调一次。完成 item 的业务行和 job 终态同事务提交。本地计算未提交时可显式重做；孤立文件不能把 running job 变为 succeeded。外部 AI 请求的未知结果进入人工处理，不自动重发，既有批准范围和总重试预算保持。

旧 stage 枚举不迁移：导入相关标签在一次导入中完成；VALIDATE 不再独立重扫所有结果；HUMAN_REVIEW 保留语义、skip、excluded 和资格引用的必要审核。workspace FTS 只是可重建视图，不阻断 release 自行建立搜索投影。

一个 data-root 只有一个可写 Studio，使用 OS 文件锁并在进程退出时释放。MCP 不取得或创建写锁。已存在未完成 banner refresh journal 的 workspace 须先用原恢复路径处理；新系统不删除该 journal 或备份。

## 一次构建

WebUI 提供 run_id、精确版本和稳定 release_build_id；不再先申请 check_id。服务持有 run 锁，构建器串行执行本地构建，防止不同请求共用 staging。

1. 记录 build/release 身份，拒绝覆盖尚未收尾的另一构建。
2. 取得一次 SQLite 一致读视图，读取发布需要的业务数据。
3. 检查覆盖、合法状态与引用、必要审核、机器/语义 Schema、人工记录和图片格式。
4. 使用共享语义重放及搜索文本函数建立 fresh v2 index、FTS 和人工记录包，复制预览。
5. 对产物做一次业务验收，持久化并原子 rename。rename 后不再全包重读、hash 或重建一套投影期望值。
6. 补记 workspace cursor/audit。final 已存在时同 build ID 只恢复收尾，不创建第二个 release。

构建不依赖 active profile、Keyring、网络或历史请求图片/prompt 重放。provider snapshot 仅记录离线来源；人工完整语义也可以参与构建。

新 release 的六项布局：

```text
release.json
manifest.json
index.sqlite3
previews/
quality_report.json
manual-overrides.json
```

文件结构由 [Schema](data-and-schemas.md) 与 release-index.v2.sql 拥有。新包不生成 checksums.sha256/schemas.sha256；旧包中的这些文件不读取、不验证。MCP 仍引用的 manifest/quality 摘要只在写出小型元数据时生成一次，作为标识使用。

quality_report format 2 只记录本次实际业务检查；没有 check 版本或 snapshot 指纹，也没有伪造的 hash 验证通过项。format 1 历史报告保持可读。

## 独立发布与回滚

发布页面读取轻量 release 列表和 current token。用户确认后，只核对目标 release 的身份、必要元数据可解析、构建成功记录、index 格式与可打开性；不扫描图片、不比较历史摘要、不读取 workspace。

首个合格 candidate 可以发布。回滚复用同一个切换函数，目标是另一份已有 release。四工具 stdio smoke 属于开发集成验收，不在日常发布动作中复制整包或启动子进程。

切换在 current-switch 锁内执行；陈旧 current token 返回冲突。首次发布必须设置默认版本，后续显式选择是否改变默认值，其余版本指针保留。写临时 current、flush/fsync、原子替换，再读取小指针；MCP 下一请求观察变化。

发布审计仅使用 logs/release-events.jsonl：替换前持久化意图，替换后追加结果。不创建 activation-check 目录、逐操作收据或 hash 链。current 已切换但收尾失败时如实返回已切换及警告；下一次明确操作比较意图的 old/new pointer 完成审计，不自动移动指针。

## 验证边界

保持文件可读、格式、引用、必要业务记录、路径安全和原子提交检查。取消的是本地数据未修改证明，不能以“损坏检测”名义恢复同样的全包 hash 扫描。新旧工作流由本规格的聚焦测试覆盖，实际结果见当前阶段验证与 commit；既有 R0–R5 历史证据不推定本轮完成。
