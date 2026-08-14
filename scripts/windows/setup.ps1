[CmdletBinding()]
param(
    [switch]$Plan,
    [switch]$ValidateLayout,
    [switch]$CheckPythonDiscovery,
    [string]$InstallRoot = $(Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Blockpedia\runtime"),
    [string]$InstallerPath,
    [string]$PythonPath,
    [switch]$RecreateVenv
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$script:Venv = Join-Path $script:InstallRoot "venv"
$script:Downloads = Join-Path $script:InstallRoot "downloads"
$script:RequirementsLock = Join-Path $script:RepoRoot "requirements.lock"
$script:PythonExe = $null
$script:PythonHome = $null
$script:PythonSource = $null
$script:DefaultInstalledPython = Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Programs\Python\Python314\python.exe"
$script:VenvPython = Join-Path $script:Venv "Scripts\python.exe"
$script:RequirementsMarker = Join-Path $script:Venv ".blockpedia-requirements.sha256"
$script:RuntimeMarker = Join-Path $script:InstallRoot ".blockpedia-runtime-root"
$script:PythonRuntimeMarker = Join-Path $script:InstallRoot ".blockpedia-python-runtime.json"
$script:RuntimeMarkerVersion = "blockpedia-runtime-root-v1"
$script:ExpectedPython = "3.14.7"
$script:ExpectedArchitecture = "AMD64"
$script:InstallerUrl = "https://www.python.org/ftp/python/3.14.7/python-3.14.7-amd64.exe"
$script:InstallerSha256 = "9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649"
$script:CachedInstaller = Join-Path $script:Downloads "python-3.14.7-amd64.exe"

function Get-CanonicalPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "路径不能为空。"
    }
    $full = [IO.Path]::GetFullPath($Path)
    if ($full.StartsWith("\\") -or $full.StartsWith("//")) {
        throw "拒绝 UNC 路径：$full。InstallRoot 和 managed downloads 必须位于本机磁盘。"
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
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        throw "此脚本只支持 Windows。"
    }
    $processArchitecture = $env:PROCESSOR_ARCHITECTURE
    if ($processArchitecture -ne "AMD64" -and $env:PROCESSOR_ARCHITEW6432 -ne "AMD64") {
        throw "此脚本只支持 Windows AMD64。"
    }
}

function Assert-NoReparseComponents {
    param([Parameter(Mandatory = $true)][string]$Path)

    $full = Get-CanonicalPath -Path $Path
    $root = [IO.Path]::GetPathRoot($full)
    if ([string]::IsNullOrWhiteSpace($root)) {
        throw "无法确定路径根：$full"
    }
    $current = $root
    $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
    if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "拒绝经过 reparse point 的路径：$current"
    }
    $remainder = $full.Substring($root.Length)
    foreach ($segment in ($remainder -split "[\\/]+")) {
        if ([string]::IsNullOrWhiteSpace($segment)) {
            continue
        }
        $current = Join-Path $current $segment
        if (-not (Test-Path -LiteralPath $current)) {
            break
        }
        $item = Get-Item -LiteralPath $current -Force -ErrorAction Stop
        if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "拒绝经过 reparse point 的路径：$current"
        }
    }
}

function Assert-NoReparseTree {
    param([Parameter(Mandatory = $true)][string]$Path)

    Assert-NoReparseComponents -Path $Path
    $full = Get-CanonicalPath -Path $Path
    if (-not (Test-Path -LiteralPath $full -PathType Container)) {
        throw "受控目录不存在或不是目录：$full"
    }
    $entries = @(Get-ChildItem -LiteralPath $full -Force -Recurse -ErrorAction Stop)
    foreach ($entry in $entries) {
        if (($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "受控目录包含 reparse point，拒绝继续：$($entry.FullName)"
        }
    }
}

function Write-AtomicText {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Value
    )

    $parent = Split-Path -Parent $Path
    $name = Split-Path -Leaf $Path
    $temporary = Join-Path $parent (".$name." + [guid]::NewGuid().ToString("N") + ".tmp")
    $backup = Join-Path $parent (".$name." + [guid]::NewGuid().ToString("N") + ".bak")
    try {
        $utf8 = New-Object System.Text.UTF8Encoding($false)
        [IO.File]::WriteAllText($temporary, $Value, $utf8)
        Assert-NoReparseComponents -Path $temporary
        if (Test-Path -LiteralPath $Path -PathType Leaf) {
            Assert-NoReparseComponents -Path $Path
            [IO.File]::Replace($temporary, $Path, $backup, $true)
        }
        else {
            [IO.File]::Move($temporary, $Path)
        }
    }
    finally {
        if (Test-Path -LiteralPath $temporary -PathType Leaf) {
            Remove-Item -LiteralPath $temporary -Force
        }
        if (Test-Path -LiteralPath $backup -PathType Leaf) {
            Remove-Item -LiteralPath $backup -Force
        }
    }
}

function Assert-Repository {
    if (-not (Test-Path -LiteralPath $script:RequirementsLock -PathType Leaf)) {
        throw "找不到 requirements.lock：$script:RequirementsLock。请从仓库根目录运行此脚本。"
    }
}

function Assert-ManagedRuntimeRoot {
    param([switch]$CreateIfMissing)

    $root = Get-CanonicalPath -Path $script:InstallRoot
    Assert-NoReparseComponents -Path $root
    if (-not (Test-Path -LiteralPath $root)) {
        if (-not $CreateIfMissing) {
            throw "managed runtime root 不存在：$root。请先运行 setup。"
        }
        New-Item -ItemType Directory -Path $root -Force | Out-Null
        Assert-NoReparseComponents -Path $root
    }
    $rootItem = Get-Item -LiteralPath $root -Force
    if (-not $rootItem.PSIsContainer) {
        throw "InstallRoot 不是目录：$root"
    }
    if (($rootItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "InstallRoot 是 reparse point，拒绝管理：$root"
    }

    $markerExists = Test-Path -LiteralPath $script:RuntimeMarker -PathType Leaf
    if ($markerExists) {
        Assert-NoReparseComponents -Path $script:RuntimeMarker
        $value = (Get-Content -LiteralPath $script:RuntimeMarker -Raw -ErrorAction Stop).Trim()
        if ($value -ne $script:RuntimeMarkerVersion) {
            throw "runtime identity marker 不匹配，拒绝管理或删除：$script:RuntimeMarker"
        }
    }
    else {
        $children = @(Get-ChildItem -LiteralPath $root -Force -ErrorAction Stop)
        if ($children.Count -eq 0 -and $CreateIfMissing) {
            Write-AtomicText -Path $script:RuntimeMarker -Value $script:RuntimeMarkerVersion
        }
        else {
            throw "InstallRoot 非空但缺少正确 runtime identity marker，拒绝管理或删除：$root"
        }
    }
    Assert-NoReparseComponents -Path $script:RuntimeMarker
}

function Ensure-ManagedDirectory {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$CreateIfMissing
    )

    $full = Get-CanonicalPath -Path $Path
    Assert-NoReparseComponents -Path $full
    if (-not (Test-Path -LiteralPath $full)) {
        if (-not $CreateIfMissing) {
            throw "受控目录不存在：$full"
        }
        New-Item -ItemType Directory -Path $full -Force | Out-Null
    }
    Assert-NoReparseTree -Path $full
}

function Assert-CanonicalVenvChild {
    $expected = Get-ComparePath -Path (Join-Path $script:InstallRoot "venv")
    $actual = Get-ComparePath -Path $script:Venv
    if ($actual -ne $expected) {
        throw "拒绝使用非 runtime root 直接子目录作为 venv：$script:Venv"
    }
}

function Get-PythonProbe {
    param([Parameter(Mandatory = $true)][string]$PythonPath)

    if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
        throw "找不到 Python：$PythonPath"
    }
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
    if ($exitCode -ne 0 -or [string]::IsNullOrWhiteSpace($probeText)) {
        throw "无法运行 isolated Python probe：$PythonPath（exit $exitCode）"
    }
    try { return ($probeText | ConvertFrom-Json) } catch { throw "isolated Python probe 返回了无法解析的 JSON" }
}

function Assert-BaseProbe {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)]$Probe
    )

    if ((Get-ComparePath -Path $Probe.executable) -ne (Get-ComparePath -Path $PythonPath)) {
        throw "base Python executable identity 不匹配：$($Probe.executable)"
    }
    if ((Get-ComparePath -Path $Probe.prefix) -ne (Get-ComparePath -Path $Probe.base_prefix)) {
        throw "base Python prefix 与 base_prefix 不一致"
    }
    if ([bool]$Probe.is_venv) {
        throw "发现的 Python 是 venv，不允许作为 base interpreter：$PythonPath"
    }
    if ($Probe.version -ne $script:ExpectedPython) {
        throw "Python 版本必须精确为 $script:ExpectedPython，实际为 $($Probe.version)：$PythonPath"
    }
    if ($Probe.architecture -ne $script:ExpectedArchitecture) {
        throw "只支持 Windows AMD64；Python 架构实际为 $($Probe.architecture)：$PythonPath"
    }
}

function Add-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][object]$Candidates,
        [Parameter(Mandatory = $true)][string]$CandidatePath,
        [Parameter(Mandatory = $true)][ValidateSet("explicit", "registered", "installed")][string]$Source
    )

    if ([string]::IsNullOrWhiteSpace($CandidatePath)) { return }
    try { $canonical = Get-CanonicalPath -Path $CandidatePath } catch { return }
    $key = Get-ComparePath -Path $canonical
    foreach ($candidate in $Candidates) {
        if ((Get-ComparePath -Path $candidate.Path) -eq $key) { return }
    }
    [void]$Candidates.Add([PSCustomObject]@{ Path = $canonical; Source = $Source })
}

function Get-RegisteredPythonCandidates {
    $candidates = New-Object System.Collections.ArrayList
    $registryKeys = @(
        "HKCU:\Software\Python\PythonCore\3.14\InstallPath",
        "HKLM:\Software\Python\PythonCore\3.14\InstallPath",
        "HKLM:\Software\WOW6432Node\Python\PythonCore\3.14\InstallPath"
    )
    foreach ($registryKey in $registryKeys) {
        if (-not (Test-Path -LiteralPath $registryKey)) { continue }
        try {
            $key = Get-Item -LiteralPath $registryKey -ErrorAction Stop
            $defaultValue = $key.GetValue("")
            if ($defaultValue -is [string] -and -not [string]::IsNullOrWhiteSpace($defaultValue)) {
                Add-PythonCandidate -Candidates $candidates -CandidatePath (Join-Path $defaultValue "python.exe") -Source registered
            }
            $properties = Get-ItemProperty -LiteralPath $registryKey -ErrorAction Stop
            if ($properties.PSObject.Properties.Name -contains "ExecutablePath") {
                Add-PythonCandidate -Candidates $candidates -CandidatePath ([string]$properties.ExecutablePath) -Source registered
            }
            if ($properties.PSObject.Properties.Name -contains "InstallPath") {
                Add-PythonCandidate -Candidates $candidates -CandidatePath (Join-Path ([string]$properties.InstallPath) "python.exe") -Source registered
            }
        }
        catch {
            continue
        }
    }
    $localAppData = [Environment]::GetFolderPath("LocalApplicationData")
    foreach ($standardPath in @(
        (Join-Path $localAppData "Programs\Python\Python314\python.exe"),
        (Join-Path $localAppData "Programs\Python\Python314-64\python.exe")
    )) {
        Add-PythonCandidate -Candidates $candidates -CandidatePath $standardPath -Source registered
    }
    return $candidates
}

function Find-BasePython {
    $candidates = New-Object System.Collections.ArrayList
    if (-not [string]::IsNullOrWhiteSpace($PythonPath)) {
        $explicit = (Resolve-Path -LiteralPath $PythonPath -ErrorAction Stop).Path
        Add-PythonCandidate -Candidates $candidates -CandidatePath $explicit -Source explicit
        if ($candidates.Count -eq 0) { throw "-PythonPath 不是可用的本机路径：$PythonPath" }
    }
    else {
        foreach ($candidate in (Get-RegisteredPythonCandidates)) {
            [void]$candidates.Add($candidate)
        }
    }

    foreach ($candidate in $candidates) {
        try {
            Assert-NoReparseComponents -Path $candidate.Path
            $probe = Get-PythonProbe -PythonPath $candidate.Path
            Assert-BaseProbe -PythonPath $candidate.Path -Probe $probe
            return [PSCustomObject]@{ Path = $candidate.Path; Prefix = $probe.prefix; Probe = $probe; Source = $candidate.Source }
        }
        catch {
            if ($candidate.Source -eq "explicit") {
                throw "显式 -PythonPath 未通过 isolated 3.14.7 AMD64 base identity 检查：$($candidate.Path)。原因：$($_.Exception.Message)"
            }
            continue
        }
    }
    return $null
}

function Set-SelectedBasePython {
    param([Parameter(Mandatory = $true)]$Selection)

    $script:PythonExe = $Selection.Path
    $script:PythonHome = $Selection.Prefix
    $script:PythonSource = $Selection.Source
    Assert-BaseProbe -PythonPath $script:PythonExe -Probe $Selection.Probe
    Assert-NoReparseComponents -Path $script:PythonExe
    Assert-NoReparseComponents -Path $script:PythonHome
}

function Warn-IfTemporaryPython {
    $tempRoots = @($env:TEMP, $env:TMP) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    $pythonCompare = Get-ComparePath -Path $script:PythonExe
    foreach ($tempRoot in $tempRoots) {
        $tempCompare = Get-ComparePath -Path $tempRoot
        if ($pythonCompare.StartsWith($tempCompare + "\") -or $pythonCompare -eq $tempCompare) {
            Write-Warning "已复用位于 TEMP 的 CPython：$script:PythonExe。该路径可能被清理；若失效，请重装或提供 setup.ps1 -PythonPath <稳定的 python.exe>。"
            return
        }
    }
}

function Assert-PythonIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$PythonPath,
        [Parameter(Mandatory = $true)][ValidateSet("base", "venv")][string]$Kind
    )

    Assert-NoReparseComponents -Path $PythonPath
    $probe = Get-PythonProbe -PythonPath $PythonPath
    $expectedExecutable = Get-ComparePath -Path $PythonPath
    if ((Get-ComparePath -Path $probe.executable) -ne $expectedExecutable) {
        throw "$Kind Python executable identity 不匹配：$($probe.executable)"
    }
    $expectedPrefix = if ($Kind -eq "base") { $script:PythonHome } else { $script:Venv }
    if ((Get-ComparePath -Path $probe.prefix) -ne (Get-ComparePath -Path $expectedPrefix)) {
        throw "$Kind Python prefix identity 不匹配：$($probe.prefix)"
    }
    if ((Get-ComparePath -Path $probe.base_prefix) -ne (Get-ComparePath -Path $script:PythonHome)) {
        throw "$Kind Python base_prefix identity 不匹配：$($probe.base_prefix)"
    }
    $expectedVenv = $Kind -eq "venv"
    if ([bool]$probe.is_venv -ne $expectedVenv) {
        throw "$Kind Python is_venv identity 不匹配"
    }
    if ($Kind -eq "base" -and (Get-ComparePath -Path $probe.prefix) -ne (Get-ComparePath -Path $probe.base_prefix)) {
        throw "base Python prefix 与 base_prefix 不一致"
    }
    if ($probe.version -ne $script:ExpectedPython) {
        throw "$Kind Python 版本必须精确为 $script:ExpectedPython，实际为 $($probe.version)"
    }
    if ($probe.architecture -ne $script:ExpectedArchitecture) {
        throw "只支持 Windows AMD64；$Kind Python 架构实际为 $($probe.architecture)"
    }
}

function Assert-Sha256 {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Expected
    )
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "找不到待校验文件：$Path"
    }
    $actual = Get-Sha256Hex -Path $Path
    if ($actual -ne $Expected) {
        throw "SHA-256 校验失败：$Path。请删除损坏缓存后重试，或提供正确的官方安装程序。"
    }
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

function Replace-FileAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Replacement,
        [Parameter(Mandatory = $true)][string]$Destination
    )
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        Assert-NoReparseComponents -Path $Destination
        $backup = "$Destination.$([guid]::NewGuid().ToString('N')).bak"
        try { [IO.File]::Replace($Replacement, $Destination, $backup, $true) }
        finally { if (Test-Path -LiteralPath $backup -PathType Leaf) { Remove-Item -LiteralPath $backup -Force } }
    }
    else {
        [IO.File]::Move($Replacement, $Destination)
    }
}

function Get-VerifiedInstaller {
    Assert-ManagedRuntimeRoot
    Ensure-ManagedDirectory -Path $script:Downloads -CreateIfMissing
    $source = $null
    if (-not [string]::IsNullOrWhiteSpace($InstallerPath)) {
        $source = (Resolve-Path -LiteralPath $InstallerPath).Path
        Assert-Sha256 -Path $source -Expected $script:InstallerSha256
    }
    elseif (Test-Path -LiteralPath $script:CachedInstaller -PathType Leaf) {
        Assert-NoReparseComponents -Path $script:CachedInstaller
        Assert-Sha256 -Path $script:CachedInstaller -Expected $script:InstallerSha256
        $source = $script:CachedInstaller
    }

    $staging = Join-Path $script:Downloads (".python-installer." + [guid]::NewGuid().ToString("N") + ".stage")
    $cacheStaging = Join-Path $script:Downloads (".python-installer." + [guid]::NewGuid().ToString("N") + ".cache")
    try {
        if ($null -ne $source) {
            Copy-Item -LiteralPath $source -Destination $staging -Force
        }
        else {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            Write-Host "正在下载官方 CPython 3.14.7 安装程序并校验 SHA-256。"
            Invoke-WebRequest -Uri $script:InstallerUrl -OutFile $staging -UseBasicParsing
        }
        Assert-NoReparseComponents -Path $staging
        Assert-Sha256 -Path $staging -Expected $script:InstallerSha256
        Copy-Item -LiteralPath $staging -Destination $cacheStaging -Force
        Assert-NoReparseComponents -Path $cacheStaging
        Assert-Sha256 -Path $cacheStaging -Expected $script:InstallerSha256
        Replace-FileAtomically -Replacement $cacheStaging -Destination $script:CachedInstaller
        Assert-NoReparseComponents -Path $script:CachedInstaller
        Assert-Sha256 -Path $script:CachedInstaller -Expected $script:InstallerSha256
        return $script:CachedInstaller
    }
    finally {
        foreach ($temporary in @($staging, $cacheStaging)) {
            if (Test-Path -LiteralPath $temporary -PathType Leaf) {
                Remove-Item -LiteralPath $temporary -Force
            }
        }
    }
}

function Install-Python {
    param([Parameter(Mandatory = $true)][string]$Installer)

    Assert-NoReparseComponents -Path $script:InstallRoot
    $targetHome = Split-Path -Parent $script:DefaultInstalledPython
    Assert-NoReparseComponents -Path $targetHome
    $arguments = @(
        "/quiet",
        "InstallAllUsers=0",
        ("TargetDir=" + [char]34 + $targetHome + [char]34),
        "PrependPath=0",
        "AppendPath=0",
        "Include_launcher=0",
        "InstallLauncherAllUsers=0",
        "Include_test=0",
        "Include_doc=0",
        "Include_pip=1",
        "Shortcuts=0",
        "AssociateFiles=0"
    )
    Assert-NoReparseComponents -Path $Installer
    Assert-Sha256 -Path $Installer -Expected $script:InstallerSha256
    Write-Host "正在以当前用户安装官方 CPython 3.14.7（不修改 PATH、文件关联、launcher 或系统服务）。"
    $process = Start-Process -FilePath $Installer -ArgumentList $arguments -Wait -PassThru
    if ($process.ExitCode -ne 0) {
        throw "CPython 安装失败（exit $($process.ExitCode)）。请检查安装程序、权限或代理设置。"
    }
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$Description
    )
    $previousHome = $env:PYTHONHOME
    $previousPath = $env:PYTHONPATH
    try {
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        & $FilePath @Arguments
        $exitCode = $LASTEXITCODE
    }
    finally {
        if ($null -eq $previousHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $previousHome }
        if ($null -eq $previousPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPath }
    }
    if ($exitCode -ne 0) {
        throw "$Description 失败（exit $exitCode）。"
    }
}

function Assert-VenvSafe {
    Assert-ManagedRuntimeRoot
    Assert-CanonicalVenvChild
    Assert-NoReparseTree -Path $script:Venv
    if (-not (Test-Path -LiteralPath (Join-Path $script:Venv "pyvenv.cfg") -PathType Leaf)) {
        throw "venv 缺少 pyvenv.cfg，拒绝自动管理：$script:Venv"
    }
    if (-not (Test-Path -LiteralPath $script:PythonExe -PathType Leaf)) {
        throw "受控 base Python 缺失，拒绝自动管理 venv：$script:PythonExe"
    }
    Assert-NoReparseComponents -Path $script:PythonHome
    Assert-NoReparseComponents -Path $script:PythonExe
    Assert-PythonIdentity -PythonPath $script:VenvPython -Kind venv
}

function Ensure-Venv {
    Assert-ManagedRuntimeRoot
    Assert-CanonicalVenvChild
    if (Test-Path -LiteralPath $script:Venv) {
        if ($RecreateVenv) {
            try {
                Assert-VenvSafe
            }
            catch {
                throw "拒绝自动删除不安全或身份不明的 venv。请人工检查后再处理：$script:Venv"
            }
            Remove-Item -LiteralPath $script:Venv -Recurse -Force
        }
        else {
            Assert-VenvSafe
            return
        }
    }
    Invoke-Checked -FilePath $script:PythonExe -Arguments @("-I", "-m", "venv", $script:Venv) -Description "venv 创建"
    Assert-VenvSafe
}

function Get-LockHash {
    return Get-Sha256Hex -Path $script:RequirementsLock
}

function Assert-LockMarker {
    if (-not (Test-Path -LiteralPath $script:RequirementsMarker -PathType Leaf)) {
        throw "venv 缺少 requirements lock marker，请重跑 setup。"
    }
    Assert-NoReparseComponents -Path $script:RequirementsMarker
    $actual = (Get-Content -LiteralPath $script:RequirementsMarker -Raw -ErrorAction Stop).Trim().ToLowerInvariant()
    $expected = Get-LockHash
    if ($actual -ne $expected) {
        throw "requirements.lock 已变化或 venv marker 不匹配，请重跑 setup。"
    }
}

function Write-LockMarker {
    Write-AtomicText -Path $script:RequirementsMarker -Value (Get-LockHash)
    Assert-LockMarker
}

function Write-PythonRuntimeMarker {
    if ([string]::IsNullOrWhiteSpace($script:PythonExe) -or [string]::IsNullOrWhiteSpace($script:PythonHome)) {
        throw "base Python 尚未选择，不能写 runtime marker。"
    }
    $document = [ordered]@{
        schema_version = "blockpedia-python-runtime.v1"
        base_executable = $script:PythonExe
        base_prefix = $script:PythonHome
        python_version = $script:ExpectedPython
        architecture = $script:ExpectedArchitecture
        source = $script:PythonSource
    }
    Write-AtomicText -Path $script:PythonRuntimeMarker -Value ($document | ConvertTo-Json -Compress)
    Assert-NoReparseComponents -Path $script:PythonRuntimeMarker
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
        if (-not ($document.PSObject.Properties.Name -contains $required) -or [string]::IsNullOrWhiteSpace([string]$document.$required)) {
            throw "runtime Python marker 缺少字段：$required"
        }
    }
    if ($document.schema_version -ne "blockpedia-python-runtime.v1") { throw "runtime Python marker schema_version 不支持。" }
    if ($document.python_version -ne $script:ExpectedPython -or $document.architecture -ne $script:ExpectedArchitecture) {
        throw "runtime Python marker 不是 CPython 3.14.7 AMD64。请重跑 setup。"
    }
    if (@("explicit", "registered", "installed") -notcontains $document.source) { throw "runtime Python marker source 不合法。" }
    $base = Get-CanonicalPath -Path ([string]$document.base_executable)
    $prefix = Get-CanonicalPath -Path ([string]$document.base_prefix)
    if (-not (Test-Path -LiteralPath $base -PathType Leaf)) { throw "runtime marker 的 base executable 已失效：$base。请重装或提供 setup.ps1 -PythonPath <稳定路径>。" }
    if (-not (Test-Path -LiteralPath $prefix -PathType Container)) { throw "runtime marker 的 base prefix 已失效：$prefix。请重装或提供 setup.ps1 -PythonPath <稳定路径>。" }
    Assert-NoReparseComponents -Path $base
    Assert-NoReparseComponents -Path $prefix
    $script:PythonExe = $base
    $script:PythonHome = $prefix
    $script:PythonSource = [string]$document.source
    $probe = Get-PythonProbe -PythonPath $script:PythonExe
    Assert-BaseProbe -PythonPath $script:PythonExe -Probe $probe
}

function Invoke-RepoPythonSmoke {
    $previousHome = $env:PYTHONHOME
    $previousPath = $env:PYTHONPATH
    try {
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        $code = "import sys; sys.path.insert(0,sys.argv[1]); sys.path.insert(0,sys.argv[2]); from blockpedia.cli import build_parser; p=build_parser(); assert set(next(a for a in p._actions if a.__class__.__name__ == '_SubParsersAction').choices) == {'web', 'mcp'}"
        Invoke-Checked -FilePath $script:VenvPython -Arguments @("-I", "-c", $code, (Join-Path $script:RepoRoot "src"), $script:RepoRoot) -Description "import/CLI parser smoke"
    }
    finally {
        if ($null -eq $previousHome) { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue } else { $env:PYTHONHOME = $previousHome }
        if ($null -eq $previousPath) { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue } else { $env:PYTHONPATH = $previousPath }
    }
}

function Write-Plan {
    Write-Output "Blockpedia Windows setup plan（不会写文件、不会联网、不会启动 WebUI）"
    Write-Output "仓库根目录：$script:RepoRoot"
    Write-Output "managed runtime root：$script:InstallRoot"
    Write-Output "base Python：动态发现（可用 -PythonPath 显式指定；不固定在 runtime root）"
    Write-Output "venv 目录：$script:Venv"
    Write-Output "下载缓存：$script:Downloads"
    Write-Output "数据目录（不会触碰）：$(Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Blockpedia\data")"
    Write-Output "requirements.lock：$script:RequirementsLock"
    Write-Output "步骤：验证 Windows AMD64；验证/创建 identity marker；校验官方 CPython 3.14.7 SHA-256；按需执行 per-user 安装；安全创建/校验 venv；按 requirements.lock hash-lock 安装、pip check 和 lock marker；执行 isolated import/CLI parser smoke。"
    Write-Output "Python URL：$script:InstallerUrl"
}

function Invoke-CheckPythonDiscovery {
    Assert-WindowsAmd64
    Assert-Repository
    $selection = Find-BasePython
    if ($null -eq $selection) {
        throw "未发现可用的 CPython 3.14.7 AMD64 base interpreter。请安装官方 per-user Python，或提供 setup.ps1 -PythonPath <稳定的 python.exe>。"
    }
    Set-SelectedBasePython -Selection $selection
    Write-Output "发现并验证 CPython $script:ExpectedPython：$script:PythonExe"
    Write-Output "base prefix：$script:PythonHome"
    Write-Output "source：$script:PythonSource"
    Warn-IfTemporaryPython
}

function Invoke-ValidateLayout {
    Assert-WindowsAmd64
    Assert-Repository
    Assert-ManagedRuntimeRoot
    Ensure-ManagedDirectory -Path $script:Downloads
    Read-PythonRuntimeMarker
    Assert-VenvSafe
    Assert-LockMarker
    Write-Output "managed runtime layout 校验通过；未删除、未联网、未修改数据目录。"
}

function Invoke-Setup {
    Assert-WindowsAmd64
    Assert-Repository
    if (($Plan -and $ValidateLayout) -or ($Plan -and $CheckPythonDiscovery) -or ($ValidateLayout -and $CheckPythonDiscovery)) {
        throw "-Plan、-ValidateLayout 和 -CheckPythonDiscovery 只能选择一个。"
    }
    if ($Plan) {
        Write-Plan
        return
    }
    if ($ValidateLayout) {
        Invoke-ValidateLayout
        return
    }
    if ($CheckPythonDiscovery) {
        Invoke-CheckPythonDiscovery
        return
    }

    Assert-ManagedRuntimeRoot -CreateIfMissing
    Ensure-ManagedDirectory -Path $script:Downloads -CreateIfMissing
    $selection = Find-BasePython
    $sourceAfterInstall = $false
    if ($null -eq $selection) {
        $installer = Get-VerifiedInstaller
        Install-Python -Installer $installer
        $selection = Find-BasePython
        if ($null -eq $selection) {
            throw "官方 installer 已完成但仍未发现可用 CPython 3.14.7 base interpreter。安装程序可能进入 maintenance/Modify 模式但未提供 Python 路径；请重装官方 per-user Python，或运行 setup.ps1 -PythonPath <稳定的 python.exe>。"
        }
        $sourceAfterInstall = $true
    }
    if ($sourceAfterInstall) { $selection.Source = "installed" }
    Set-SelectedBasePython -Selection $selection
    Write-PythonRuntimeMarker
    Write-Output "复用已验证的 CPython $script:ExpectedPython：$script:PythonExe"
    Write-Output "base prefix：$script:PythonHome（source=$script:PythonSource）"
    Warn-IfTemporaryPython

    Ensure-Venv
    Invoke-Checked -FilePath $script:VenvPython -Arguments @("-I", "-m", "pip", "install", "--require-hashes", "-r", $script:RequirementsLock) -Description "requirements.lock 安装"
    Invoke-Checked -FilePath $script:VenvPython -Arguments @("-I", "-m", "pip", "check") -Description "pip check"
    Write-LockMarker
    Invoke-RepoPythonSmoke

    Write-Output "Windows setup 完成。"
    Write-Output "runtime：$script:InstallRoot"
    Write-Output "data：$(Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Blockpedia\data")（setup 不修改）"
    Write-Output "下一步：双击 scripts\windows\run.cmd，或运行 scripts\windows\run.cmd -NoBrowser。"
}

try {
    Invoke-Setup
    exit 0
}
catch {
    Write-Error ("安装失败：" + $_.Exception.Message)
    exit 1
}
