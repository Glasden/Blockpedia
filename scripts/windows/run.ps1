[CmdletBinding()]
param(
    [switch]$Plan,
    [switch]$Check,
    [string]$InstallRoot = $(Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Blockpedia\runtime"),
    [string]$DataRoot,
    [ValidateSet("critical", "error", "warning", "info", "debug")]
    [string]$LogLevel = "info",
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$script:Venv = Join-Path $script:InstallRoot "venv"
$script:VenvPython = Join-Path $script:Venv "Scripts\python.exe"
$script:RequirementsLock = Join-Path $script:RepoRoot "requirements.lock"
$script:RequirementsMarker = Join-Path $script:Venv ".blockpedia-requirements.sha256"
$script:RuntimeMarker = Join-Path $script:InstallRoot ".blockpedia-runtime-root"
$script:PythonRuntimeMarker = Join-Path $script:InstallRoot ".blockpedia-python-runtime.json"
$script:RuntimeMarkerVersion = "blockpedia-runtime-root-v1"
$script:ExpectedPython = "3.14.7"
$script:ExpectedArchitecture = "AMD64"
$script:PythonExe = $null
$script:PythonHome = $null
$script:PythonSource = $null
$script:RunExitCode = 0

if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $script:DataRoot = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Blockpedia\data"
}
else {
    $script:DataRoot = [IO.Path]::GetFullPath($DataRoot)
}

function Get-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { throw "路径不能为空。" }
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith("\\") -or $full.StartsWith("//")) {
        throw "拒绝 UNC 路径：$full。runtime root 和 managed downloads 必须位于本机磁盘。"
    }
    return $full
}

function Get-ComparePath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    while ($full.Length -gt 3 -and ($full.EndsWith("\") -or $full.EndsWith("/"))) {
        $full = $full.Substring(0, $full.Length - 1)
    }
    return $full.ToLowerInvariant()
}

function Assert-WindowsAmd64 {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) { throw "此脚本只支持 Windows。" }
    if ($env:PROCESSOR_ARCHITECTURE -ne "AMD64" -and $env:PROCESSOR_ARCHITEW6432 -ne "AMD64") {
        throw "此脚本只支持 Windows AMD64。"
    }
}

function Assert-NoReparseComponents {
    param([Parameter(Mandatory = $true)][string]$Path)
    $full = Get-CanonicalPath -Path $Path
    $root = [IO.Path]::GetPathRoot($full)
    $current = $root
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "拒绝经过 reparse point 的路径：$current" }
    $remainder = $full.Substring($root.Length)
    foreach ($segment in ($remainder -split "[\\/]+")) {
        if ([string]::IsNullOrWhiteSpace($segment)) { continue }
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current)) { break }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "拒绝经过 reparse point 的路径：$current" }
    }
}

function Assert-NoReparseTree {
    param([Parameter(Mandatory = $true)][string]$Path)
    Assert-NoReparseComponents -Path $Path
    $full = Get-CanonicalPath -Path $Path
    if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw "受控目录不存在或不是目录：$full" }
    $entries = @(Get-ChildItem -LiteralPath $full -Force -Recurse -ErrorAction Stop)
    foreach ($entry in $entries) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "受控目录包含 reparse point，拒绝继续：$($entry.FullName)"
        }
    }
}

function Assert-ManagedRuntimeRoot {
    $root = Get-CanonicalPath -Path $script:InstallRoot
    Assert-NoReparseComponents -Path $root
    if (-not (Test-Path -LiteralPath $root -PathType Container)) { throw "managed runtime root 不存在：$root。请先运行 setup。" }
    $rootItem = Get-Item -LiteralPath $root -Force
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "InstallRoot 是 reparse point，拒绝管理：$root" }
    if (-not (Test-Path -LiteralPath $script:RuntimeMarker -PathType Leaf)) { throw "runtime identity marker 缺失，拒绝管理：$script:RuntimeMarker" }
    Assert-NoReparseComponents -Path $script:RuntimeMarker
    $value = (Get-Content -LiteralPath $script:RuntimeMarker -Raw -ErrorAction Stop).Trim()
    if ($value -ne $script:RuntimeMarkerVersion) { throw "runtime identity marker 不匹配，拒绝管理：$script:RuntimeMarker" }
}

function Assert-ManagedDownloads {
    $downloads = Join-Path $script:InstallRoot "downloads"
    Assert-NoReparseComponents -Path $downloads
    if (-not (Test-Path -LiteralPath $downloads -PathType Container)) { throw "managed downloads 不存在：$downloads。请先运行 setup。" }
    Assert-NoReparseTree -Path $downloads
}

function Assert-CanonicalVenvChild {
    $expected = Get-ComparePath -Path (Join-Path $script:InstallRoot "venv")
    if ((Get-ComparePath -Path $script:Venv) -ne $expected) { throw "拒绝使用非 runtime root 直接子目录作为 venv：$script:Venv" }
}

function Get-PythonProbe {
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) { throw "找不到 Python：$PythonPath" }
    $previousHome = $env:PYTHONHOME
    $previousPath = $env:PYTHONPATH
    try {
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        $code = "import json,os,platform,sys; print(json.dumps({'version': '.'.join(map(str, sys.version_info[:3])), 'architecture': platform.machine(), 'executable': os.path.abspath(sys.executable), 'prefix': os.path.abspath(sys.prefix), 'base_prefix': os.path.abspath(getattr(sys, 'base_prefix', sys.prefix)), 'is_venv': bool(getattr(sys, 'prefix', sys.base_prefix) != sys.base_prefix)}))"
        $probeText = (& $PythonPath -I -c $code 2>&1 | Out-String).Trim()
        $exitCode = $LASTEXITCODE
    }
    finally {
        if ($null -eq $previousHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $previousHome }
        if ($null -eq $previousPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPath }
    }
    if ($exitCode -ne 0 -or [string]::IsNullOrWhiteSpace($probeText)) { throw "无法运行 isolated venv Python probe（exit $exitCode）。请先运行 setup。" }
    try { return ($probeText | ConvertFrom-Json) } catch { throw "isolated venv Python probe 返回了无法解析的 JSON" }
}

function Assert-BaseProbe {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)]$Probe
    )
    if ((Get-ComparePath -Path $Probe.executable) -ne (Get-ComparePath -Path $PythonPath)) { throw "base Python executable identity 不匹配：$($Probe.executable)" }
    if ((Get-ComparePath -Path $Probe.prefix) -ne (Get-ComparePath -Path $Probe.base_prefix)) { throw "base Python prefix 与 base_prefix 不一致" }
    if ([bool]$Probe.is_venv) { throw "runtime marker 的 base Python 是 venv，拒绝运行。" }
    if ($Probe.version -ne $script:ExpectedPython) { throw "base Python 必须精确为 $script:ExpectedPython，实际为 $($Probe.version)" }
    if ($Probe.architecture -ne $script:ExpectedArchitecture) { throw "只支持 Windows AMD64；base Python 架构实际为 $($Probe.architecture)" }
}

function Read-PythonRuntimeMarker {
    if (-not (Test-Path -LiteralPath $script:PythonRuntimeMarker -PathType Leaf)) {
        throw "runtime Python marker 缺失：$script:PythonRuntimeMarker。请重跑 setup。"
    }
    Assert-NoReparseComponents -Path $script:PythonRuntimeMarker
    try { $document = Get-Content -LiteralPath $script:PythonRuntimeMarker -Raw -ErrorAction Stop | ConvertFrom-Json }
    catch { throw "runtime Python marker 不是有效 JSON，请重跑 setup。" }
    $allowed = @("schema_version", "base_executable", "base_prefix", "python_version", "architecture", "source")
    foreach ($property in $document.PSObject.Properties.Name) {
        if ($allowed -notcontains $property) { throw "runtime Python marker 包含未知字段：$property" }
    }
    foreach ($required in $allowed) {
        if (-not ($document.PSObject.Properties.Name -contains $required) -or [string]::IsNullOrWhiteSpace([string]$document.$required)) { throw "runtime Python marker 缺少字段：$required" }
    }
    if ($document.schema_version -ne "blockpedia-python-runtime.v1") { throw "runtime Python marker schema_version 不支持。" }
    if ($document.python_version -ne $script:ExpectedPython -or $document.architecture -ne $script:ExpectedArchitecture) { throw "runtime Python marker 不是 CPython 3.14.7 AMD64。请重跑 setup。" }
    if (@("explicit", "registered", "installed") -notcontains $document.source) { throw "runtime Python marker source 不合法。" }
    $base = Get-CanonicalPath -Path ([string]$document.base_executable)
    $prefix = Get-CanonicalPath -Path ([string]$document.base_prefix)
    if (-not (Test-Path -LiteralPath $base -PathType Leaf) -or -not (Test-Path -LiteralPath $prefix -PathType Container)) {
        throw "runtime marker 的 base Python 已失效：$base。请重装，或提供 setup.ps1 -PythonPath <稳定路径>。"
    }
    Assert-NoReparseComponents -Path $base
    Assert-NoReparseComponents -Path $prefix
    $script:PythonExe = $base
    $script:PythonHome = $prefix
    $script:PythonSource = [string]$document.source
    $probe = Get-PythonProbe -PythonPath $script:PythonExe
    Assert-BaseProbe -PythonPath $script:PythonExe -Probe $probe
}

function Assert-PythonIdentity {
    Read-PythonRuntimeMarker
    Assert-NoReparseComponents -Path $script:VenvPython
    $probe = Get-PythonProbe -PythonPath $script:VenvPython
    if ((Get-ComparePath -Path $probe.executable) -ne (Get-ComparePath -Path $script:VenvPython)) { throw "venv executable identity 不匹配：$($probe.executable)" }
    if ((Get-ComparePath -Path $probe.prefix) -ne (Get-ComparePath -Path $script:Venv)) { throw "venv prefix identity 不匹配：$($probe.prefix)" }
    if ((Get-ComparePath -Path $probe.base_prefix) -ne (Get-ComparePath -Path $script:PythonHome)) { throw "venv base_prefix 不匹配 runtime marker：$($probe.base_prefix)" }
    if (-not [bool]$probe.is_venv) { throw "受控 Python 不是 venv。" }
    if ($probe.version -ne $script:ExpectedPython) { throw "venv Python 必须精确为 $script:ExpectedPython，实际为 $($probe.version)" }
    if ($probe.architecture -ne $script:ExpectedArchitecture) { throw "只支持 Windows AMD64；venv Python 架构实际为 $($probe.architecture)" }
}

function Assert-VenvSafe {
    Assert-ManagedRuntimeRoot
    Assert-CanonicalVenvChild
    Assert-NoReparseTree -Path $script:Venv
    if (-not (Test-Path -LiteralPath (Join-Path $script:Venv "pyvenv.cfg") -PathType Leaf)) { throw "venv 缺少 pyvenv.cfg，拒绝管理：$script:Venv" }
    Assert-PythonIdentity
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $previousHome = $env:PYTHONHOME
    $previousPath = $env:PYTHONPATH
    try {
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        & $script:VenvPython @Arguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        if ($null -eq $previousHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $previousHome }
        if ($null -eq $previousPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPath }
    }
    if ($exitCode -ne 0) { throw "$Description 失败（exit $exitCode）。" }
}

function Get-LockHash {
    return Get-Sha256Hex -Path $script:RequirementsLock
}

function Get-Sha256Hex {
    param([Parameter(Mandatory = $true)][string]$Path)
    $algorithm = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($Path)
    try {
        $bytes = $algorithm.ComputeHash($stream)
    }
    finally {
        $stream.Dispose()
        $algorithm.Dispose()
    }
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Assert-LockMarker {
    if (-not (Test-Path -LiteralPath $script:RequirementsMarker -PathType Leaf)) { throw "venv 缺少 requirements lock marker，请重跑 setup。" }
    Assert-NoReparseComponents -Path $script:RequirementsMarker
    $actual = (Get-Content -LiteralPath $script:RequirementsMarker -Raw -ErrorAction Stop).Trim().ToLowerInvariant()
    if ($actual -ne (Get-LockHash)) { throw "requirements.lock 已变化或 venv marker 不匹配，请重跑 setup。" }
}

function Assert-DataRoot {
    $full = Get-CanonicalPath -Path $script:DataRoot
    Assert-NoReparseComponents -Path $full
    if (Test-Path -LiteralPath $full) {
        if (-not (Test-Path -LiteralPath $full -PathType Container)) { throw "DataRoot 不是目录：$full" }
        $item = Get-Item -LiteralPath $full -Force
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw "DataRoot 是 reparse point，拒绝使用：$full" }
    }
}

function Assert-RunEnvironment {
    if (-not (Test-Path -LiteralPath $script:RequirementsLock -PathType Leaf)) { throw "找不到 requirements.lock：$script:RequirementsLock" }
    Assert-ManagedRuntimeRoot
    Assert-ManagedDownloads
    Assert-VenvSafe
    Assert-LockMarker
    Invoke-Checked -Arguments @("-I", "-m", "pip", "check") -Description "pip check"
}

function Start-Browser {
    $url = "http://127.0.0.1:8765/"
    $childCommand = "Start-Sleep -Milliseconds 900; Start-Process -FilePath '$url'"
    try { Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", $childCommand) | Out-Null }
    catch { Write-Warning "无法自动打开浏览器，请手动访问 $url。" }
}

function Invoke-ProductWeb {
    $previousHome = $env:PYTHONHOME
    $previousPath = $env:PYTHONPATH
    try {
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        $env:PYTHONPATH = "$script:RepoRoot\src;$script:RepoRoot"
        # -I intentionally ignores PYTHONPATH; this bootstrap is the equivalent
        # of `python -I -m blockpedia web` with explicit source-tree insertion.
        $code = "import runpy,sys; sys.path.insert(0,sys.argv[1]); sys.path.insert(0,sys.argv[2]); sys.argv=['blockpedia','web','--data-root',sys.argv[3],'--log-level',sys.argv[4]]; runpy.run_module('blockpedia',run_name='__main__')"
        Write-Output "启动 Blockpedia WebUI：http://127.0.0.1:8765/"
        Write-Output "按 Ctrl+C 停止；日志保留在当前控制台，不写本地日志文件。"
        & $script:VenvPython -I -c $code (Join-Path $script:RepoRoot "src") $script:RepoRoot $script:DataRoot $LogLevel
        $script:RunExitCode = $LASTEXITCODE
    }
    finally {
        if ($null -eq $previousHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $previousHome }
        if ($null -eq $previousPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPath }
    }
}

function Write-Plan {
    Write-Output "Blockpedia Windows run plan（不会写文件、不会启动 Python/WebUI、不会打开浏览器）"
    Write-Output "仓库根目录：$script:RepoRoot"
    Write-Output "managed runtime root：$script:InstallRoot"
    Write-Output "venv Python：$script:VenvPython"
    Write-Output "数据根目录：$script:DataRoot"
    Write-Output "日志等级：$LogLevel"
    Write-Output "步骤：校验 runtime marker、reparse 边界、venv isolated identity、requirements lock marker 和 pip check；创建数据根目录；临时清空 PYTHONHOME/PYTHONPATH，设置源码路径并以 isolated module bootstrap 启动 WebUI。"
}

function Invoke-Run {
    Assert-WindowsAmd64
    if ($Plan) {
        Write-Plan
        $script:RunExitCode = 0
        return
    }
    Assert-RunEnvironment
    if ($Check) {
        Assert-DataRoot
        Write-Output "环境检查通过：managed runtime marker / CPython $script:ExpectedPython / AMD64 / venv identity / lock marker / pip check。"
        $script:RunExitCode = 0
        return
    }

    Assert-DataRoot
    New-Item -ItemType Directory -Path $script:DataRoot -Force | Out-Null
    if (-not $NoBrowser) { Start-Browser }
    Invoke-ProductWeb
}

try {
    Invoke-Run
    exit $script:RunExitCode
}
catch {
    Write-Error ("运行失败：" + $_.Exception.Message)
    exit 1
}
