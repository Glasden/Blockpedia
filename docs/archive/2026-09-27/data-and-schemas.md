> 历史归档（2026-09-27）：停止维护，不再作为当前执行规范。当前决定与进度统一见 [PROGRESS.md](../../../PROGRESS.md)。正文只做归档链接修正，旧状态不代表本轮完成。

# 数据、Schema 与历史兼容

当前业务边界见 [D-054 规格](export-build-refactor-spec.md)。精确字段由 schemas/ 下的 JSON Schema 和 src/blockpedia/sql/ 下的 SQL 拥有，不在多份 Markdown 中重复逐字段定义。

## 三层数据

机器事实由 Minecraft/Fabric 产生，Studio 只做验证、投影和确定性离线特征。AI 只给受控语义建议；人工覆盖与审核独立保存并稳定重放，不能改写机器事实或伪造原始来源。

workspace SQLite 的已提交行是业务结果事实源。features、annotations、provider_requests、overrides、review_tasks 与对应 job 终态按事务提交。进度、旧 generated JSON 和 artifacts 行不是成功证明，缺少这些派生文件不会重新调用已成功 AI。

资格仍为 eligible、conditional、excluded；conditional 有警告，excluded 和跳过有人工原因/证据。无图方块保留 Block/State，不从 registry 消失。

## Schema 和格式

保留现有 exporter、workspace/release、provider 和 MCP 命名空间及当前 ID。D-054 只放宽本地 manifest 中纯 checksum/inventory/functional hash 字段的必填要求；历史字段可以读取，但不再验证内容未修改。

workspace.v1.sql 不迁移；旧必填历史列保留，未再产生的 imports 摘要列写空字符串而非伪造 hash。SQLite 的显式 schema_version 仍用于格式支持判断，旧 schema_sha256 不再作运行时篡改门。

新 release 继续构建 release-index.v2.sql：scalar/index 列和完整 JSON 属于同一查询投影，来源是同一组已提交数据。语义重放、搜索文本生成与 workspace 查询共用纯计算逻辑，不维护第二份对照实现。

新 release 的六项为 release.json、manifest.json、index.sqlite3、previews/、quality_report.json、manual-overrides.json。没有 checksums.sha256 或 schemas.sha256。MCP 输出仍要求的 manifest/quality 摘要在元数据生成时计算一次，后续当作已有标识读取，不复算验证。

quality_report format 2 保存构建身份、时间和真实执行的业务检查，不含 snapshot 指纹或 hash gate；format 1 的历史报告只读兼容。manual-overrides.json 保留人工 override、skip 和 qualification 原始记录，字段仍由相应 Schema 拥有。

## 历史数据

旧 annotations、provider requests、overrides、reviews、audit、release 和 generated 文件不自动迁移或删除。旧 v2 release 可在 workspace/cache 不可用时发布。旧 v1 作为历史保留，不能伪装成具备 v2 投影。

已完成 D-045 的 base/replacement 来源和 target exclusion 关系继续可读，不重写历史 export ID、重新签名或重发 AI。未完成 refresh journal 必须先用旧恢复路径处理；新入口不再创建 refresh。

新程序写入后的 workspace 不承诺旧程序继续处理；降级恢复升级前备份，不混跑写入者。
