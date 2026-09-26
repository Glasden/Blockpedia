"""Focused checks for the source-built MCP runtime boundary."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import build_mcp  # noqa: E402


def test_supported_build_platforms() -> None:
    assert build_mcp.target_platform("win32", "AMD64") == "windows-x86_64"
    assert build_mcp.target_platform("linux", "x86_64") == "linux-x86_64"
    assert build_mcp.target_platform("linux", "aarch64") == "linux-aarch64"
    with pytest.raises(build_mcp.BuildError):
        build_mcp.target_platform("win32", "ARM64")


def test_mcp_lock_excludes_studio_credentials() -> None:
    root = Path(__file__).resolve().parents[2]
    packages = set(re.findall(r"^([a-z][a-z0-9-]*)==", (root / build_mcp.LOCK).read_text(), re.M))
    assert {"mcp", "jsonschema", "uvicorn"} <= packages
    assert packages.isdisjoint({"keyring", "secretstorage", "jeepney", "fastapi"})


def test_silent_mcp_child_times_out() -> None:
    with pytest.raises(TimeoutError):
        build_mcp.smoke([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.1)


def test_archive_uses_captured_revision_after_head_moves(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "blockpedia").mkdir(parents=True)
    (repo / "schemas").mkdir()
    marker = repo / "src" / "blockpedia" / "marker.py"
    marker.write_text("first", encoding="utf-8")
    (repo / "schemas" / "marker.json").write_text("{}", encoding="utf-8")
    (repo / build_mcp.LOCK).write_text("mcp==2.0.0", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    identity = ["-c", "user.name=Blockpedia Test", "-c", "user.email=test@localhost"]
    subprocess.run(["git", "-C", str(repo), *identity, "commit", "-qm", "first"], check=True)
    first = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    marker.write_text("second", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), *identity, "commit", "-qm", "second"], check=True)

    build_mcp.archive_revision(repo, first, tmp_path / "snapshot")

    assert (tmp_path / "snapshot" / "src" / "blockpedia" / "marker.py").read_text() == "first"
