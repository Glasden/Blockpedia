# R2 Index Studio 实施说明

## 源码与入口

- 核心 Studio、导入、SQLite、FTS 与 Worker：`src/blockpedia/`。
- Web adapter：`src/blockpedia/web.py`；模板和本地静态资源位于 `src/blockpedia/templates/` 与 `src/blockpedia/static/`。
- Windows 源码一键部署/运行：[`windows-quickstart.md`](windows-quickstart.md)；wrapper 管理 runtime/venv/lock marker，动态复用已注册的 CPython base，不新增产品 CLI、installer 或 service。
- `block-index web` 启动 loopback WebUI，固定监听 `127.0.0.1:8765`。
- `block-index mcp` 当前只输出 `MCP_NOT_IMPLEMENTED_R4` 到 stderr 并以非零退出；MCP 属于 R4，尚未实现。Windows 3.14.7 下模块入口和已安装入口均为 exit `2`、stdout `0 bytes`，stderr 内容稳定，仅有 Windows CRLF 换行差异。

## R2 数据与流水线

应用使用用户选定的 `<data-root>`，并保持 `exports/`、`workspace/`、`cache/`、`releases/`、`logs/` 和根 `current.json` 的目录边界。`cache/import-checks/{check_id}/state.json` 是 check 的 authoritative state；导入先建立 check-owned immutable snapshot，再由唯一一次 R1 validator 校验，后续只消费已验证 snapshot，不重新选择 variant 或渲染图片。

Studio 持久化完整阶段顺序：

```text
PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS
→ VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE
→ HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE
```

R2 只执行前六阶段；完成后停在 `R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING`。Worker 支持持久状态、心跳、stale 只读检测、显式 WebUI recover、暂停/取消、失败收敛和一次自动恢复尝试。

## Import check UX optimization contract

这是在现有 check/snapshot 设计上的最小增量，不引入第二索引、`index.json`、`owner_instance_id`、SQLite 表/字段、通用 migration、依赖、服务、消息总线或任意生成框架。

- 首页和目录 listing 按需扫描严格命名的 check directories/state files，拒绝 links/reparse，并只显示 sanitized summaries；按 `(minecraft_version, export_id)` 选择 latest actionable check。`Recent Checks` 始终可见，最多 5 个 distinct exports，active first；chooser 条目显示派生 checked/imported/changed marker。`GET /api/imports/checks?minecraft_version=&limit=` 只读这些 state，不建立持久索引。
- `ImportService` 在单一 WebUI 进程内使用以 `(minecraft_version, export_id)` 为键的 coordinator `RLock`。同 export 的不同 opaque ref 不能排入重复 active check；active duplicate 返回同一 `check_id` 的 `202,reused=true`。passed check 只有在当前 raw `manifest.json` 与 `checksums.sha256` SHA-256 anchors 均匹配时复用，返回 `200,reused=true` 且不调用 validator；anchor 改变或 failed/interrupted 才创建新的 `202`。进程重启 active check 固定为 `IMPORT_CHECK_INTERRUPTED`，不自动 resume。
- `state.json` 最小增补 `created_at`、`updated_at`、validator subphase/progress 和 `workspace={status: absent|creating|created|failed, import_id, run_id, error_code}`。不持久化 absolute source path 或 chooser ref；旧 state 缺少 association 时按 version/export/manifest hash 扫描既有 workspace DB 做 backward-compatible discovery，不改 SQL。
- `POST /api/imports` 严格只接受 `check_id + copy_mode=copy_to_workspace`，不接受 `project_id`。第一次在同一 lock 内预留 import/run、写 `creating` 后构建现有 workspace；duplicate creating 返回 `202` 同一 run，valid created 返回 `200` 同一 run，首次完成可返回 `201`。只从 snapshot 导入；只有最终 `work.sqlite3` 验证后才 deep-link。restart 的 creating 优先 reconcile 原 reservation，否则保留原 ID 并稳定失败/重试，绝不分配第二 run/workspace。
- UI 状态为 `unchecked`、`checking→View Progress`、`passed_not_imported→Import and Enter Run`、`imported→Enter Existing Run`、`failed/interrupted→Retry after fresh chooser ref`、`changed_since_check→Run New Check`；成功 `/ui/imports` 使用 `HX-Redirect /runs/{run_id}`。
- 进度只有 `snapshot→validate→finalize` 宏观阶段，validator subphase 来自既有 inventory/Schema/JSONL/reference/render/checksum loops；snapshot callback 来自既有 copy/hash loop。callback 只观察，不增加 scan/read/hash/decode；subphase completed 单调，total 未知保持 null/0，UI 显示 live-count indeterminate bar，持久化节流且 phase/terminal 强制。SSE 发送完整 snapshots，不逐 item。

## Minimal R2 verification obligations

复用现有 fixture，至少验证同 export 并发 refs 只执行一个 check/validator、unchanged passed reuse、changed anchors 新 check、Recent Checks re-entry、duplicate import 只有一个 run/work.sqlite3、restart creating reconciliation、多个 progress snapshots 单调、callback/no-callback report 与 PNG reads 不变，以及无 absolute path/chooser ref、SQL/hash unchanged。上述验证不得生成真实 Minecraft 资产或扩大 R2 阶段边界。

## 验收命令

```text
PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider
python -m tools.validate_r2 --repo-root . --report docs/evidence/r2-validation-report.json
python -m pip check
git diff --check
```

`validate_r2` 只做小型静态实现门和环境证据记录，不重复行为测试、不运行真实导出或 R1 大型 validator。Windows 11 build `10.0.26200` AMD64 的 CPython `3.14.7` runtime evidence 已记录在 `docs/evidence/r2-windows-runtime-report.json`：官方 installer SHA-256 为 `sha256:9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649`，`requirements.lock` SHA-256 为 `sha256:6a551bb7be5f0ec1635bf9cfbc518898d8d2194724228a21f251e48e4cd13894`；hash-lock 安装 exit `0`、`pip check` 无 broken，全量测试为 `55 passed, 2 skipped, 1 warning in 21.28s`。wheel 只保留为 `package_contents_smoke`：status=`observed`、`gate=false`，仅观察安装包包含 `templates/static/vendor/sql`，不证明 build frontend 已 hash-lock 或 wheel 可复现。Web smoke 执行 `block-index web --data-root %TEMP%/opencode/r2-cpython-3.14.7/web-smoke-data --log-level warning` 后请求 `GET http://127.0.0.1:8765/` 返回 `200` 且精确免责声明存在，stdout/stderr 均为 `0 bytes`；主动 terminate 后的 process code `1` 是 Windows 终止结果，不是启动失败。

`docs/evidence/r2-validation-report.json` 是 Windows 3.14.7 重跑 `tools.validate_r2` 后的静态实现报告：所有 checks 为 `passed`、`python_baseline_passed=true`、`status=passed`、`issues=[]`，并保留 `linux_r2_evidence=false`、`linux_validation_stage=R5` 与 deferred obligations。Linux 未验证且没有被伪称通过；Linux CPython/Web、Linux MCP、Linux wheel/ABI、Linux Java/runtime/exporter 和最终双平台复现统一 deferred 到 R5。当前环境若使用非冻结 Python 版本，报告只因 Python mismatch 为 `blocked`，不产生 Linux issue。因此 R2 实现、Windows/静态/fixture 验证和退出门已完成，R3 可以开始。

## 产品声明

Blockpedia 不是官方 Minecraft 产品，未经 Mojang 或 Microsoft 批准或关联：

`NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.`

本文不将 R3 provider、R4 MCP 或 R5 release/activation 说成已实现；R2 的 Windows/静态/fixture 验证已记录，Linux obligation 仅标记为 R5 deferred，不声称 Linux 已通过。
