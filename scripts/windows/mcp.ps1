<#
.SYNOPSIS
    Starts the Blockpedia MCP server in the foreground.
.DESCRIPTION
    Configure this script as the command used by an MCP client.  Running it
    directly from a normal terminal starts a foreground stdio service that
    waits for JSON-RPC input; it cannot attach the new process to another
    client that is already running.
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = $(Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Blockpedia\runtime"),
    [string]$DataRoot
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$script:SourceRoot = Join-Path $script:RepoRoot "src"
$script:InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$script:VenvPython = [IO.Path]::GetFullPath((Join-Path $script:InstallRoot "venv\Scripts\python.exe"))
$script:RunScript = Join-Path $script:RepoRoot "scripts\windows\run.ps1"
$script:BootstrapMarker = "sys.argv=['blockpedia','mcp'"
$script:PythonBootstrap = "import runpy,sys; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv[1]); sys.path.insert(0,sys.argv[2]); sys.argv=['blockpedia','mcp','--data-root',sys.argv[3]]; runpy.run_module('blockpedia',run_name='__main__')"
$script:ImportPreflight = "import sys; sys.dont_write_bytecode=True; sys.path.insert(0,sys.argv[1]); sys.path.insert(0,sys.argv[2]); import blockpedia.cli,blockpedia.mcp_server"
$script:RunExitCode = 1

if ([string]::IsNullOrWhiteSpace($DataRoot)) {
    $script:DataRoot = [IO.Path]::GetFullPath((Join-Path ([Environment]::GetFolderPath("LocalApplicationData")) "Blockpedia\data"))
}
else {
    $script:DataRoot = [IO.Path]::GetFullPath($DataRoot)
}

function Write-Stderr {
    param([Parameter(Mandatory = $true)][string]$Message)
    [Console]::Error.WriteLine($Message)
}

function Invoke-EnvironmentPreflight {
    $powershell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
    if (-not (Test-Path -LiteralPath $powershell -PathType Leaf)) {
        throw "Windows PowerShell child process was not found."
    }
    if (-not (Test-Path -LiteralPath $script:RunScript -PathType Leaf)) {
        throw "scripts/windows/run.ps1 was not found."
    }

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = "Continue"
        $checkOutput = @(
            & $powershell -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass `
                -File $script:RunScript -Check -InstallRoot $script:InstallRoot -DataRoot $script:DataRoot 2>&1
        )
        $checkExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($checkExitCode -ne 0) {
        throw ("run.ps1 -Check preflight failed (exit {0})." -f $checkExitCode)
    }

    $hadPythonHome = Test-Path Env:PYTHONHOME
    $previousPythonHome = $env:PYTHONHOME
    $hadPythonPath = Test-Path Env:PYTHONPATH
    $previousPythonPath = $env:PYTHONPATH
    try {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = "Continue"
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        $importOutput = @(
            & $script:VenvPython -I -c $script:ImportPreflight $script:SourceRoot $script:RepoRoot 2>&1
        )
        $importExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
        if ($hadPythonHome) { $env:PYTHONHOME = $previousPythonHome }
        else { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue }
        if ($hadPythonPath) { $env:PYTHONPATH = $previousPythonPath }
        else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    }
    if ($importExitCode -ne 0) {
        throw ("controlled Python source import preflight failed (exit {0})." -f $importExitCode)
    }
}

function Test-BlockpediaMcpProcess {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Name,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$CommandLine
    )

    return (
        $Name -ieq "python.exe" -and
        (Test-CommandLineContainsRepositoryRoot -CommandLine $CommandLine) -and
        $CommandLine.IndexOf($script:BootstrapMarker, [StringComparison]::OrdinalIgnoreCase) -ge 0
    )
}

function Test-CommandLineContainsRepositoryRoot {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$CommandLine)

    $searchStart = 0
    while ($true) {
        $index = $CommandLine.IndexOf($script:RepoRoot, $searchStart, [StringComparison]::OrdinalIgnoreCase)
        if ($index -lt 0) { return $false }
        $afterRoot = $index + $script:RepoRoot.Length
        if ($afterRoot -ge $CommandLine.Length) { return $true }
        $nextCharacter = $CommandLine[$afterRoot]
        if ($nextCharacter -eq '\' -or $nextCharacter -eq '/' -or $nextCharacter -eq '"' -or [char]::IsWhiteSpace($nextCharacter)) {
            return $true
        }
        $searchStart = $index + 1
    }
}

function Get-BlockpediaMcpProcessSnapshot {
    $candidates = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe'")
    return @(
        foreach ($candidate in $candidates) {
            if (Test-BlockpediaMcpProcess -Name ([string]$candidate.Name) -CommandLine ([string]$candidate.CommandLine)) {
                [pscustomobject]@{
                    ProcessId   = [int]$candidate.ProcessId
                    Name        = [string]$candidate.Name
                    CommandLine = [string]$candidate.CommandLine
                }
            }
        }
    )
}

function Test-ProcessIdAlive {
    param([Parameter(Mandatory = $true)][int]$ProcessId)
    $process = @(Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f $ProcessId))
    return $process.Count -gt 0
}

function Stop-PreviousBlockpediaMcp {
    $snapshot = @(Get-BlockpediaMcpProcessSnapshot)
    foreach ($entry in $snapshot) {
        try {
            Stop-Process -Id $entry.ProcessId -Force -ErrorAction Stop
        }
        catch {
            if (Test-ProcessIdAlive -ProcessId $entry.ProcessId) {
                throw ("Could not terminate the previous MCP process PID {0}." -f $entry.ProcessId)
            }
        }
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ($true) {
        $remaining = @(
            foreach ($entry in $snapshot) {
                if (Test-ProcessIdAlive -ProcessId $entry.ProcessId) { $entry.ProcessId }
            }
        )
        if ($remaining.Count -eq 0) { return }
        if ([DateTime]::UtcNow -ge $deadline) {
            throw ("The previous MCP process did not exit: PID {0}." -f (($remaining | ForEach-Object { [string]$_ }) -join ", "))
        }
        Start-Sleep -Milliseconds 100
    }
}

try {
    Invoke-EnvironmentPreflight
    Stop-PreviousBlockpediaMcp

    $hadPythonHome = Test-Path Env:PYTHONHOME
    $previousPythonHome = $env:PYTHONHOME
    $hadPythonPath = Test-Path Env:PYTHONPATH
    $previousPythonPath = $env:PYTHONPATH
    try {
        Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        # Keep this invocation out of assignment/pipeline contexts: stdout is MCP transport.
        & $script:VenvPython -I -c $script:PythonBootstrap $script:SourceRoot $script:RepoRoot $script:DataRoot
        $script:RunExitCode = $LASTEXITCODE
    }
    finally {
        if ($hadPythonHome) { $env:PYTHONHOME = $previousPythonHome }
        else { Remove-Item Env:PYTHONHOME -ErrorAction SilentlyContinue }
        if ($hadPythonPath) { $env:PYTHONPATH = $previousPythonPath }
        else { Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue }
    }
    exit $script:RunExitCode
}
catch {
    Write-Stderr ("Blockpedia MCP startup failed: " + $_.Exception.Message)
    exit 1
}
