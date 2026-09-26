# 本地边界、秘密与分发

[D-054 重构](export-build-refactor-spec.md) 明确采用本地个人项目的信任模型：不检测已存本地导出、workspace 或 release 是否被篡改，不复算历史 checksum、快照指纹和文件 identity，不通过重放整个历史输入证明当前结果。

## 仍然保护的边界

- Web 只绑定 127.0.0.1:8765，不提供 host/port 覆盖、账号系统、CORS 或 CSRF。
- HTTP/provider 输入继续严格验证；文件路径必须留在允许的数据根内，拒绝明显链接、junction/reparse、hardlink 和非法文件类型，避免越界读写。
- 文件实际可读、JSON/PNG 可解析、业务记录齐全、引用合法和原子写入属于运行正确性检查，不使用历史 hash 判断未修改。
- 一个 data-root 只有一个可写 Studio。运行锁保护应用并发，SQLite 事务保护结果与任务状态，私有 staging/原子 rename 保护可见性。
- release 生成后应用不再原地修改；发布与回滚只切 current。失去终态审计不能否定已发生的原子替换，需如实返回收尾状态。
- 不承诺防御同账户进程同时改写数据库、产物、报告和校验信息；不声称新流程与旧检测链等价。

## 秘密与外部模型

API Key 使用 OS Keyring 或读取环境变量，Keyring 优先。Keyring 服务名 blockpedia，账户为 profile ID；仅不可逆 secret_reference 可以进入持久数据。Key 不进入 SQLite、普通配置、提示词、异常、日志、截图、前端或 release。

既有显式 OpenAI adapter、图片输入、strict structured output、批准范围和一次总重试预算保持。Responses 请求 store=false，Chat Completions 省略 store；均不能证明远端 retention 或第三方实际模型身份。MCP 不创建 provider、不读取 Keyring/active profile 作查询。

构建仅消费本地已提交业务结果及冻结来源，不执行网络探测或历史请求重放。发布也不依赖秘密和 provider。

## 分发与验证

公开内容只允许源码、文档、真实 JSON Schema、空结构及原创 fixture 生成器；不得提交原版 JAR、纹理、模型、截图、真实索引、导出包、人工本地数据或秘密。测试生成物只放临时目录。

取消本地业务数据的篡改检测不等于取消下载依赖的精确锁。继续按 requirements.lock 和 Gradle 依赖配置验证下载输入；开发机所需平台产物的新增记录须明确说明来源与验证范围，不把开发机验证说成正式跨平台发布验收。

依赖锁按消费边界分开：Studio/WebUI 使用 `requirements.lock`；[从源码构建的 MCP 启动器](mcp-api.md) 使用 `requirements-mcp.lock`，只含 MCP 进程实际导入的依赖，不含 `keyring`/`SecretStorage`/`jeepney` 等凭据存储包，因为 MCP 运行时不初始化 provider、不读取 Keyring。两个 lock 都是精确 hash 锁，各自使用 `pip install --require-hashes` 安装，不合并、不互相回退。

MCP stdout 只输出协议，诊断写 stderr，不写数据根或本地日志。发布审计是 WebUI 的职责，位于 logs 下的单个追加文件，不能写回 release。
