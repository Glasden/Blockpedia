from __future__ import annotations

import shutil
import subprocess
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts" / "windows"
EXPECTED_URL = "https://www.python.org/ftp/python/3.14.7/python-3.14.7-amd64.exe"
EXPECTED_SHA256 = "9d9eb2709ef81bf5cd30db3c2096bdbc4ea10087c22e62f27d356b36f6ae9649"
RUNTIME_MARKER = "blockpedia-runtime-root-v1"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def _powershell() -> str:
    found = shutil.which("powershell.exe")
    if found:
        return found
    system_root = Path(__import__("os").environ.get("SystemRoot", r"C:\Windows"))
    fallback = system_root / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if fallback.is_file():
        return str(fallback)
    pytest.skip("powershell.exe is unavailable")


def _run_ps(powershell: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", *arguments],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def test_windows_script_surface_is_frozen_and_safe() -> None:
    names = ("setup.ps1", "setup.cmd", "run.ps1", "run.cmd")
    for name in names:
        assert (WINDOWS / name).is_file()

    setup = _text(WINDOWS / "setup.ps1")
    run = _text(WINDOWS / "run.ps1")
    setup_cmd = _text(WINDOWS / "setup.cmd")
    run_cmd = _text(WINDOWS / "run.cmd")

    assert "Blockpedia\\runtime" in setup
    assert "Blockpedia\\data" in run
    assert "PythonCore\\3.14" in setup
    assert "Get-RegisteredPythonCandidates" in setup and "Find-BasePython" in setup
    assert "$PythonPath" in setup and "$CheckPythonDiscovery" in setup
    assert "DefaultInstalledPython" in setup
    assert ".blockpedia-python-runtime.json" in setup and ".blockpedia-python-runtime.json" in run
    assert "blockpedia-python-runtime.v1" in setup and "blockpedia-python-runtime.v1" in run
    assert "Read-PythonRuntimeMarker" in run and "base_prefix" in run and "source" in run
    assert "maintenance/Modify" in setup
    assert "$script:ExpectedPython = \"3.14.7\"" in setup
    assert EXPECTED_URL in setup
    assert EXPECTED_SHA256 in setup
    assert "--require-hashes" in setup
    assert "pip\", \"check" in setup
    assert ".blockpedia-runtime-root" in setup and ".blockpedia-runtime-root" in run
    assert "blockpedia-runtime-root-v1" in setup and "blockpedia-runtime-root-v1" in run
    assert ".blockpedia-requirements.sha256" in setup and ".blockpedia-requirements.sha256" in run
    assert "Assert-NoReparseComponents" in setup and "Assert-NoReparseTree" in setup
    assert "Assert-NoReparseComponents" in run and "Assert-NoReparseTree" in run
    assert "Assert-VenvSafe" in setup and "Assert-VenvSafe" in run
    assert "Assert-CanonicalVenvChild" in setup
    assert "Assert-Sha256 -Path $script:CachedInstaller" in setup
    assert "[guid]::NewGuid" in setup and "Replace-FileAtomically" in setup
    assert "$RecreateVenv" in setup
    assert "Remove-Item -LiteralPath $script:Venv" in setup
    assert "Remove-Item -LiteralPath $script:InstallRoot" not in setup
    assert "Remove-Item -LiteralPath $script:DataRoot" not in (setup + run)
    assert "-I" in setup and "-I" in run
    assert "sys.path.insert" in setup and "sys.path.insert" in run
    assert '-c $code "--"' not in setup + run
    assert "$script:DataRoot $LogLevel" in run
    assert "PYTHONHOME" in setup and "PYTHONHOME" in run
    assert "PYTHONPATH" in setup and "PYTHONPATH" in run
    assert "RegisterPython=0" not in setup
    assert "runtime\\python-3.14.7\\python.exe" not in (setup + run)
    assert "--host" not in setup + run
    assert "--port" not in setup + run
    assert "setx" not in (setup + run).lower()
    assert "Environment]::SetEnvironmentVariable" not in setup + run
    assert "block-index.exe" not in setup + run
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*' in setup_cmd
    assert 'powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*' in run_cmd
    assert "endlocal & exit /b %exitCode%" in setup_cmd
    assert "endlocal & exit /b %exitCode%" in run_cmd


def test_windows_plan_modes_are_read_only_when_powershell_is_available(tmp_path: Path) -> None:
    powershell = _powershell()
    install_root = tmp_path / "install root"
    data_root = tmp_path / "data root"
    setup_result = _run_ps(
        powershell,
        "-File",
        str(WINDOWS / "setup.ps1"),
        "-Plan",
        "-InstallRoot",
        str(install_root),
    )
    assert setup_result.returncode == 0, setup_result.stderr
    run_result = _run_ps(
        powershell,
        "-File",
        str(WINDOWS / "run.ps1"),
        "-Plan",
        "-InstallRoot",
        str(install_root),
        "-DataRoot",
        str(data_root),
    )
    assert run_result.returncode == 0, run_result.stderr
    assert not install_root.exists()
    assert not data_root.exists()


def test_validate_layout_rejects_unmarked_and_incomplete_roots_without_deleting(tmp_path: Path) -> None:
    powershell = _powershell()

    unmarked = tmp_path / "unmarked runtime"
    unmarked.mkdir()
    sentinel = unmarked / "keep.txt"
    sentinel.write_text("keep-unmarked", encoding="utf-8")
    rejected_unmarked = _run_ps(
        powershell,
        "-File",
        str(WINDOWS / "setup.ps1"),
        "-ValidateLayout",
        "-InstallRoot",
        str(unmarked),
    )
    assert rejected_unmarked.returncode != 0
    assert sentinel.read_text(encoding="utf-8") == "keep-unmarked"

    marked = tmp_path / "marked runtime"
    marked.mkdir()
    (marked / ".blockpedia-runtime-root").write_text(RUNTIME_MARKER, encoding="utf-8")
    (marked / "downloads").mkdir()
    marked_sentinel = marked / "keep.txt"
    marked_sentinel.write_text("keep-marked", encoding="utf-8")
    rejected_incomplete = _run_ps(
        powershell,
        "-File",
        str(WINDOWS / "setup.ps1"),
        "-ValidateLayout",
        "-InstallRoot",
        str(marked),
    )
    assert rejected_incomplete.returncode != 0
    assert marked_sentinel.read_text(encoding="utf-8") == "keep-marked"
    assert not (marked / "venv").exists()


def test_python_discovery_check_uses_registered_or_explicit_base_without_writing(tmp_path: Path) -> None:
    powershell = _powershell()
    result = _run_ps(powershell, "-File", str(WINDOWS / "setup.ps1"), "-CheckPythonDiscovery")
    if result.returncode != 0:
        pytest.skip("当前 Windows 没有可发现的 CPython 3.14.7 registered base interpreter")
    assert "3.14.7" in result.stdout
    assert "source" in result.stdout
    assert not (tmp_path / "runtime").exists()

    candidates = re.findall(r"[A-Za-z]:\\[^\r\n]*?python\.exe", result.stdout, flags=re.IGNORECASE)
    assert candidates
    explicit = _run_ps(
        powershell,
        "-File",
        str(WINDOWS / "setup.ps1"),
        "-CheckPythonDiscovery",
        "-PythonPath",
        candidates[0],
    )
    assert explicit.returncode == 0, explicit.stderr
