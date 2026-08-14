# R2 Index Studio 实施说明

## 源码与入口

- 核心 Studio、导入、SQLite、FTS 与 Worker：`src/blockpedia/`。
- Web adapter：`src/blockpedia/web.py`；模板和本地静态资源位于 `src/blockpedia/templates/` 与 `src/blockpedia/static/`。
- Windows 源码一键部署/运行：[`windows-quickstart.md`](windows-quickstart.md)；wrapper 管理 runtime/venv/lock marker，动态复用已注册的 CPython base，不新增产品 CLI、installer 或 service。
- `block-index web` 启动 loopback WebUI，固定监听 `127.0.0.1:8765`。
- `block-index mcp` 当前只输出 `MCP_NOT_IMPLEMENTED_R4` 到 stderr 并以非零退出；MCP 属于 R4，尚未实现。Windows 3.14.7 下模块入口和已安装入口均为 exit `2`、stdout `0 bytes`，stderr 内容稳定，仅有 Windows CRLF 换行差异。

## R2 数据与流水线

应用使用用户选定的 `<data-root>`，并保持 `exports/`、`workspace/`、`cache/`、`releases/`、`logs/` 和根 `current.json` 的目录边界。导入先建立 check-owned snapshot，再由唯一一次 R1 validator 校验；后续只消费已验证 snapshot，不重新选择 variant 或渲染图片。

Studio 持久化完整阶段顺序：

```text
PREPARE → IMPORT_EXPORT → VALIDATE_REGISTRY → VALIDATE_VARIANTS
→ VALIDATE_RENDERS → EXTRACT_FEATURES → AI_ANNOTATE → VALIDATE
→ HUMAN_REVIEW → BUILD_RELEASE → ACTIVATE_RELEASE
```

R2 只执行前六阶段；完成后停在 `R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING`。Worker 支持持久状态、心跳、stale 只读检测、显式 WebUI recover、暂停/取消、失败收敛和一次自动恢复尝试。

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
