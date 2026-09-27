> 历史归档（2026-09-27）：停止维护，不再作为当前执行规范。当前决定与进度统一见 [PROGRESS.md](../../../PROGRESS.md)。正文只做归档链接修正，旧状态不代表本轮完成。

# 质量与验收

本轮验收遵循 [D-054 规格](export-build-refactor-spec.md)，旧防篡改矩阵、检查许可证、双 release 上线门及逐发布 MCP smoke 不再是产品要求。历史 Windows/R0–R4 结果留在路线图，不据此声称本轮已复验或 R5 已完成。

## 业务检查

输入边界检查实际数据的版本、结构、合法引用、注册表覆盖、图片可解析及必要质量。结果写入保证事务一致性；构建检查必要人工审核、语义完整、skip/excluded 记录和新搜索投影。发布仅检查目标身份和必要文件可用性，然后原子切 current。

不要为“检测修改”新增 hash、fingerprint、文件 identity 前后比较或历史输入重放。PNG CRC/编码错误属于当前格式能否读取；它不比较图片与某份历史内容。

Schema 精确形状由 JSON 文件拥有，SQLite 结构由 SQL 拥有，业务条件由对应服务实现。测试检查外部结果和失败边界，不建立第三份字段定义或完整参考实现。

## 最小证据路径

- 使用真实 Web 应用、临时 data-root、现有原创 exporter fixture 和 fake provider，验证一次导入到发布的链路；不能只用 HTTP stub 证明业务闭环。
- 对 PNG 共享解析、源文件复制/解码次数及任务规划次数做 focused checks，不使用易波动的耗时阈值。
- 对事务提交前、目录 rename 前/后、current replace 前/后注入有意义的故障，核对真实提交点和同 ID 恢复。
- 验证旧 workspace 业务记录继续使用、generated/artifact 文件不再作为证据、D-045 mixed lineage 保留且不重放历史请求。
- 验证一个 candidate 可发布、workspace 不可访问仍可发布/回滚、陈旧 current token 不覆盖新指针；旧 checksum 改变不触发检测。
- 对本轮构建产物运行一次四工具 stdio，并验证 provider-free、数据根零写和下一请求观察新 pointer。日常 publish 不启动 smoke 子进程。
- Java 修改做冻结工具链构建及普通 banner 公共路径聚焦检查。编译和源码检查不冒充实际 Minecraft/GPU 渲染。

## 常用开发命令

```text
python -m pytest -q
python -m tools.validate_r0 --repo-root .
python -m tools.validate_r2 --repo-root .
gradlew --no-daemon build
```

运行时使用锁定的 CPython 3.14.7；Java 使用 25。验证工具只在明确运行时产生结果，应用不把这些静态报告作为每个业务 run 的 gate。

下载依赖继续按锁验证；本地业务数据取消篡改检测。不同开发架构产生的本地 Loom 合并产物或 native classifier 校验记录，应明确验证来源和环境，不扩大产品支持声明。

每个验收结论对应实际命令/输出和候选代码；未变化的输入复用现有证据。修复失败后只重跑受影响检查，候选快照变化后按需要复审。公开仓库不提交生成的 PNG、非空数据库、真实索引或秘密。

Windows 11 x86_64、Linux x86_64 是正式目标。Linux ARM64 开发机上的测试或编译结果只证明对应环境；缺少的平台、实际 Keyring/provider 和真实游戏运行证据须明确说明，不追加虚构通过项。
