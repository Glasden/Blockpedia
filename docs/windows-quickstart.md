# Windows 一键部署与运行

## 快速开始

1. 在仓库根目录双击 `scripts\windows\setup.cmd`。
2. setup 完成后双击 `scripts\windows\run.cmd`。
3. 浏览器打开 `http://127.0.0.1:8765/`；run 会保留控制台日志，使用 `Ctrl+C` 停止。

脚本只支持 Windows AMD64，不请求管理员权限、不修改 `PATH`、文件关联、launcher 或系统服务，不生成 `block-index.exe`。官方 per-user installer 可能写入当前用户的安装/卸载元数据；脚本不会自行写注册表配置。默认 managed runtime、Python、venv、下载缓存和数据根彼此分离：

```text
%LOCALAPPDATA%\Blockpedia\runtime\
%LOCALAPPDATA%\Blockpedia\runtime\venv\
%LOCALAPPDATA%\Blockpedia\runtime\downloads\
%LOCALAPPDATA%\Blockpedia\runtime\.blockpedia-python-runtime.json
%LOCALAPPDATA%\Blockpedia\data\
```

setup 固定使用官方 CPython `3.14.7` AMD64 installer，并校验 SHA-256：

```text
9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649
```

base CPython 不固定放在 runtime root：setup 优先验证 `-PythonPath`、HKCU/HKLM `PythonCore\3.14` registry locations 和标准 per-user 路径；只有未发现合格的 CPython 时才执行已校验的官方 installer，目标为 `%LOCALAPPDATA%\Programs\Python\Python314`。发现已有合格解释器时会输出“复用已验证的 CPython”；若路径位于 TEMP，会给出持久性 warning。`-CheckPythonDiscovery` 可只发现/probe，不写 root、venv、不联网。

runtime root 会写入固定 identity marker 和 `.blockpedia-python-runtime.json`；UNC、reparse point、非空无 marker 的目录不会被管理。依赖只通过仓库 `requirements.lock` 使用 `pip install --require-hashes -r requirements.lock` 安装，再执行 `pip check`，并把当前 lock SHA-256 写入 venv marker。setup 可安全重跑；已有 Python/venv 会先使用 isolated probe 校验 executable、prefix、base_prefix 和 `is_venv`。`-RecreateVenv` 即使 venv 健康也会请求重建，但只有完整安全边界和身份 probe 通过时才允许删除受控 runtime root 直接子目录 `venv`；其他情况会拒绝自动删除并要求人工检查。

## 使用已有 R1 数据

PowerShell 运行入口会直接透传 `-DataRoot`，例如：

```powershell
scripts\windows\run.cmd -DataRoot "D:\Code\blockpedia\run\blockpedia-data"
```

也可以禁用自动打开浏览器，或只执行环境检查：

```powershell
scripts\windows\run.cmd -NoBrowser
scripts\windows\run.ps1 -Check
scripts\windows\setup.ps1 -Plan
scripts\windows\setup.ps1 -CheckPythonDiscovery
scripts\windows\setup.ps1 -PythonPath "D:\Tools\Python314\python.exe"
scripts\windows\run.ps1 -Plan -DataRoot "D:\Code\blockpedia\run\blockpedia-data"
```

`-Plan` 不写文件、不联网、不启动 WebUI；`-LogLevel` 仅接受 `critical`、`error`、`warning`、`info`、`debug`。host/port 不是可配置项，WebUI 始终绑定 `127.0.0.1:8765`。

需要只验证 managed runtime 边界时可运行：

```powershell
scripts\windows\setup.ps1 -ValidateLayout -InstallRoot "$env:LOCALAPPDATA\Blockpedia\runtime"
```

该模式不删除、不联网；未标记 root、reparse path、缺少合法 venv 或 lock marker 时返回失败。

## 网络失败与卸载

- 首次 setup 只有在没有可发现的合格 base Python 时才需要访问官方 Python URL；离线或代理环境下可先下载同一官方 installer，再传入：

  ```powershell
  scripts\windows\setup.ps1 -InstallerPath "D:\Downloads\python-3.14.7-amd64.exe"
  ```

- 无论缓存还是 `-InstallerPath`，脚本都会先校验固定 SHA-256；代理必须允许 HTTPS/TLS 1.2。网络失败不会安装未经校验的文件。
- 如果官方 installer 已进入维护/Modify 模式但没有在目标目录生成解释器，setup 会重新查询 registry；若仍不存在，请安装稳定的 per-user Python 或传入 `-PythonPath`。runtime 删除不会删除 registry 中的外部 base Python；TEMP 中的 base Python 被清理后需重新 setup 或重新指定稳定路径。
- 卸载前请停止 run 并备份数据；自动化边界只允许用户明确删除 `%LOCALAPPDATA%\Blockpedia\runtime`，不要删除父目录。`%LOCALAPPDATA%\Blockpedia\data` 必须单独备份后由用户明确决定是否删除，不能随 runtime 一起删除。

这些脚本是源码自动化 wrapper，不是单文件 EXE、安装包、系统服务或自动更新器；源码更新后 run 会通过当前进程源码路径直接使用最新源码，不持久化 `PYTHONPATH`。

## 产品声明

Blockpedia 不是官方 Minecraft 产品，未经 Mojang 或 Microsoft 批准或关联：

`NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.`
