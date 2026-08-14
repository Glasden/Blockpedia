# R0 实现说明

## 范围

R0 只物化 `schemas/{exporter,workspace,provider,mcp}/` 下恰好 26 个 Draft 2020-12 Schema，并提供每个 Schema 的一个 valid fixture 与一个 extra-field rejection fixture；重要 nested objects 关闭未知字段。字段形状只由这 26 个真实 Schema 文件拥有，Markdown 只记录产品、组件、安全行为和说明性示例。

R0 还包括 Fabric/Gradle toolchain skeleton、实际引入的 R0 tooling Python 依赖的精确 hash lock，以及一个轻量检查 inventory、fixtures 和 provider wire 基础约束的测试。R0 不预锁未实现的 R2-R4 栈，不引入通用规则引擎、额外 Schema ID、词汇 artifact 或新服务。

## 固定基线与路径

- Minecraft Java `26.2`、Java `25`、Fabric Loader `0.19.3`、Fabric API `0.157.0+26.2`、Loom `1.17.19`、Gradle `9.5.1`。
- Minecraft 26.2 使用 native Mojang names/unobfuscated，不解析外部 mappings artifact。
- CPython `3.14.7`；Windows 11 x86_64；Linux x86_64 `manylinux_2_17` / glibc `>=2.17`。
- Schema 路径：`schemas/<namespace>/<schema-id>.json`。
- 本地 R0 报告/临时数据必须在仓库外或项目约定的报告目录，不提交生成资产、真实索引、秘密或测试输出。

## 最终验证命令

以下命令是 R0 的最小验收入口；当前开发环境已经执行 Windows 适用命令并通过：

```text
python --version
python -m pip install --require-hashes -r requirements.lock
python -m pip check
python -m tools.validate_r0 --repo-root .
python -m pytest -q tests/test_r0_schemas.py
gradlew.bat --offline build
./gradlew --offline build
```

Windows 已使用 `C:\Users\Glasden\.jdks\azul-25.0.2` 的 Java 25 执行 `gradlew.bat --offline build` 并得到 `BUILD SUCCESSFUL`；Schema validator 报告 26 个 Schema、52 个 fixture case，通过报告位于 [`evidence/r0-schema-report.json`](evidence/r0-schema-report.json)，pytest 为 `1 passed`。Windows 的真实 Minecraft runtime/export 已由 R1 现有证据覆盖；Linux Java 25/runtime、Linux exporter 独立重跑和最终双平台源码/运行时复现保留至 R5，CPython `3.14.7` 产品运行在 R2 执行，不重复作为 R0 门禁。

## 证据边界

R0 已按最小范围完成并关闭：26 个真实 Schema、52 个 fixtures、轻量 validator/pytest、R0 Python hash lock、Gradle dependency lock/verification metadata 和 Windows offline skeleton build 均存在。该结论不声称已有产品索引或 release，也不提前声称 Linux 或双平台端到端复现；Windows R1 exporter 证据、Linux/R5 义务和后续 release 证据分别按路线图阶段记录。
