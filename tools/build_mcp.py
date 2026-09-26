#!/usr/bin/env python3
"""Build a platform-local MCP runtime from the committed Blockpedia source."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCK = "requirements-mcp.lock"
SOURCE_PATHS = ("src/blockpedia", "schemas", LOCK)
TOOLS = ("index_info", "search_blocks", "get_block_details", "compare_blocks")

BOOTSTRAP = """import runpy
import sys
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent / "source" / "src"))
sys.argv = ["blockpedia", "mcp", *sys.argv[1:]]
runpy.run_module("blockpedia", run_name="__main__")
"""
LINUX_LAUNCHER = """#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec "$here/venv/bin/python3" -I "$here/bootstrap.py" "$@"
"""


class BuildError(RuntimeError):
    pass


def target_platform(system: str | None = None, machine: str | None = None) -> str:
    system = (system or sys.platform).lower()
    machine = (machine or platform.machine()).lower()
    if system == "win32" and machine in {"amd64", "x86_64"}:
        return "windows-x86_64"
    if system == "linux" and machine in {"x86_64", "amd64", "aarch64", "arm64"}:
        return "linux-aarch64" if machine in {"aarch64", "arm64"} else "linux-x86_64"
    raise BuildError(f"unsupported platform: {system} {machine}")


def check_python(executable: str) -> None:
    output = subprocess.check_output(
        [executable, "-I", "-c",
         "import platform; print(platform.python_implementation(), platform.python_version())"],
        text=True,
    ).strip()
    if output != "CPython 3.14.7":
        raise BuildError(f"CPython 3.14.7 is required; got {output}")


def archive_revision(repo_root: Path, revision: str, source: Path) -> None:
    """Extract exactly the captured commit even if HEAD changes during the build."""

    payload = subprocess.check_output(
        ["git", "archive", "--format=tar", revision, *SOURCE_PATHS], cwd=repo_root,
    )
    source.mkdir()
    with tarfile.open(fileobj=io.BytesIO(payload)) as packed:
        packed.extractall(source, filter="data")


def smoke(command: list[str], timeout: float = 30) -> None:
    """Check the built launcher over a real stdio session, including clean exit."""

    messages = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "blockpedia-build", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    assert process.stdin and process.stdout and process.stderr
    reader = ThreadPoolExecutor(max_workers=1)
    try:
        replies = []
        for message in messages:
            process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            process.stdin.flush()
            if "id" in message:
                raw = reader.submit(process.stdout.readline).result(timeout=timeout)
                if not raw:
                    raise BuildError("MCP exited before replying")
                replies.append(json.loads(raw))
        process.stdin.close()
        process.wait(timeout=timeout)
        extra = process.stdout.read()
        errors = process.stderr.read()
        if process.returncode != 0 or extra:
            raise BuildError(f"MCP exited {process.returncode} or wrote extra stdout: {errors[:200]!r}")
        if replies[0].get("id") != 1 or "result" not in replies[0] or replies[1].get("id") != 2:
            raise BuildError("MCP initialization or tools/list did not succeed")
        names = [tool["name"] for tool in replies[1]["result"]["tools"]]
        if names != list(TOOLS):
            raise BuildError(f"unexpected MCP tools: {names}")
    finally:
        if process.poll() is None:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                )
            else:
                process.kill()
            process.wait()
        reader.shutdown(wait=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


def build(base_python: str, out_root: Path, data_root: Path | None) -> Path:
    target_name = target_platform()
    check_python(base_python)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    changed = subprocess.run(
        ["git", "diff", "--quiet", revision, "--", *SOURCE_PATHS], cwd=ROOT, check=False,
    )
    if changed.returncode != 0:
        raise BuildError("commit MCP source, schemas and lock before building")
    target = out_root / f"{revision[:12]}-{target_name}"
    if target.exists():
        raise BuildError(f"build already exists: {target}")
    out_root.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".blockpedia-mcp-", dir=out_root))
    moved = False
    try:
        source = staging / "source"
        archive_revision(ROOT, revision, source)
        lock = source / LOCK
        if not lock.is_file():
            raise BuildError(f"committed source has no {LOCK}")
        if any(line.lower().startswith(("keyring==", "secretstorage==", "jeepney=="))
               for line in lock.read_text(encoding="utf-8").splitlines()):
            raise BuildError("the MCP lock includes Studio credential dependencies")
        subprocess.run([base_python, "-m", "venv", str(staging / "venv")], check=True)
        python = staging / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python3")
        subprocess.run(
            [str(python), "-I", "-m", "pip", "install", "--require-hashes",
             "--only-binary=:all:", "--no-input", "-r", str(lock)],
            check=True,
        )
        subprocess.run([str(python), "-I", "-m", "pip", "check"], check=True)
        subprocess.run(
            [str(python), "-I", "-c",
             "import importlib.util; assert all(importlib.util.find_spec(x) is None "
             "for x in ('keyring','secretstorage','jeepney'))"],
            check=True,
        )
        (staging / "bootstrap.py").write_text(BOOTSTRAP, encoding="utf-8")
        if os.name != "nt":
            launcher = staging / "blockpedia-mcp.sh"
            launcher.write_text(LINUX_LAUNCHER, encoding="utf-8")
            launcher.chmod(0o755)
        (staging / "revision.json").write_text(
            json.dumps({
                "revision": revision, "platform": target_name,
                "mcp_lock_sha256": hashlib.sha256(lock.read_bytes()).hexdigest(),
            }, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        staging.rename(target)
        moved = True
        command = (
            [str(target / "venv" / "Scripts" / "python.exe"), "-I", str(target / "bootstrap.py")]
            if os.name == "nt" else [str(target / "blockpedia-mcp.sh")]
        )
        if data_root is None:
            with tempfile.TemporaryDirectory(prefix="blockpedia-mcp-smoke-") as temporary:
                smoke([*command, "--data-root", temporary])
        else:
            if not data_root.is_dir():
                raise BuildError(f"smoke data root does not exist: {data_root}")
            smoke([*command, "--data-root", str(data_root)])
        return target
    except Exception:
        shutil.rmtree(target if moved else staging, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="CPython 3.14.7 executable")
    parser.add_argument("--out-root", type=Path, default=ROOT / "build" / "mcp")
    parser.add_argument("--data-root", type=Path, help="existing release data root for the stdio smoke")
    args = parser.parse_args()
    out_root = args.out_root if args.out_root.is_absolute() else ROOT / args.out_root
    try:
        artifact = build(args.python, out_root, args.data_root)
    except (BuildError, OSError, subprocess.CalledProcessError, tarfile.TarError, TimeoutError) as exc:
        parser.exit(1, f"MCP build failed: {exc}\n")
    if os.name == "nt":
        print(f"command: {artifact / 'venv' / 'Scripts' / 'python.exe'}")
        print(f"args: -I {artifact / 'bootstrap.py'} --data-root <path>")
    else:
        print(artifact / "blockpedia-mcp.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
