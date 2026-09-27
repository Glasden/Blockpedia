> 历史归档（2026-09-27）：停止维护，不再作为当前执行规范。当前决定与进度统一见 [PROGRESS.md](../../../PROGRESS.md)。正文只做归档链接修正，旧状态不代表本轮完成。

# WebUI 与本地操作

当前流程以 [D-054 规格](export-build-refactor-spec.md) 为准。WebUI 只绑定 127.0.0.1:8765，写操作由本机页面发起。HTTP 请求字段的精确类型、边界和未知字段拒绝由 web.py 的 StrictRequest 模型拥有。

## 导入

选择精确 Minecraft 版本和导出目录后，页面为本次操作创建 run_id，直接调用 POST /api/imports。不再先检查再导入，也不使用 check_id。

目录选择器只发 opaque ref，不向浏览器暴露绝对路径。它检查当前路径是否可安全读取，不比较目录 inode、mtime 或内容 hash。普通新导出不需要 checksum 文件。

GET /api/imports、GET /api/imports/{run_id} 和对应 /events 展示内存进度或已提交 workspace。重启时未提交 staging 显示 interrupted；详情页提供“重新选择来源并重试”，沿用原 run_id，重新取得本进程 source ref。响应丢失也沿用同一操作 ID，用户明确新建操作才换 ID。

历史 checked snapshot 若需要导入，用户先将其中的完整 export 目录放入选定版本的 exports 目录，再通过同一入口选择；新程序不管理旧 check 许可证，不自动改动旧 cache。

## 运行、AI 与审核

导入成功后进入特征计算，结果与 job 状态在 SQLite 事务提交。进度/SSE 断开不停止工作。启动只展示 stale；恢复由明确的 WebUI 操作触发。已提交成功项不重跑，孤立文件不算成功结果。

provider profile、能力探测、每批预览/批准、剩余计划批准、暂停/取消、失败批次显式重试及人工审核沿用既有入口。协议、批准范围、总重试预算和秘密边界见 [provider 文档](openai-provider.md)。未知 AI 发送结果须人工处理，不自动重发。

人工语义、资格覆盖和 skip/qualification 审核保存在独立记录中。完整人工语义可以替代 AI 语义；不能修改机器事实。workspace 搜索是可重建视图，其失效不成为 release 构建前置条件。

## 构建与发布

| 动作 | HTTP 入口 | 结果 |
|---|---|---|
| 一次导入 | POST /api/imports | 202；同 ID 已完成返回同一结果 |
| 一次构建 | POST /api/releases/build | run_id、精确版本、release_build_id；首次 201、同 ID 复用 200 |
| 候选列表 | GET /api/releases | 精确版本的轻量摘要、current 和比较 token；不扫描产物 |
| 发布 | POST /api/releases/publish | 目标 release、显式确认、默认版本选择、操作者与原因 |
| 回滚 | POST /api/releases/rollback | 与发布共用切换实现，指向已有另一 release |

构建按钮一次完成业务检查和不可变构建。release_build_id 随响应丢失/重试保留，明确开始新构建才更换。构建不自动切换 current。

独立 /releases 页面不依赖来源 workspace。首个合格 release 即可发布；没有双 candidate 或 MCP smoke 日常门。当前指针有变化时返回 409，页面要求重新读取列表确认。current token 是隐藏的并发控制值，不是检查许可证。

发布意图和结果写入 logs/release-events.jsonl。页面读取列表不会恢复或切换指针；有未完成审计时展示提示，下一次明确操作先收尾。current 已替换而审计/持久化确认失败时展示真实的已切换状态及警告，不暗示再次创建 release 或盲目切换。

旧 imports/checks、releases/check、activation-check、apply、banner-export-refresh 入口已移除，旧请求明确失败。普通 exporter 的 banner 渲染修复保留；未完成旧 refresh journal 的 workspace 需先用旧恢复路径处理。

## 边界和错误

非法输入拒绝、未就绪/并发冲突返回可定位错误；实际读取或写入失败关闭本次操作。语义、覆盖、引用、格式检查继续存在，本地 hash/identity 未修改检测不存在。

错误与页面不得回显 Key、provider 原始响应或本机绝对路径。诊断不改变业务成功事实。MCP 的标准输出仅为协议消息，不能被 WebUI 日志或恢复操作复用。
