from __future__ import annotations

import shutil
import subprocess
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
WINDOWS = ROOT / "scripts" / "windows"
RUNTIME_MARKER = "blockpedia-runtime-root-v1"


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
