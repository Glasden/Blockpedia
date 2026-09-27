"""Windows directory entries must identify the exact path being opened."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get("BLOCKPEDIA_NODE") or (
    "/opt/node-v24.21.0-linux-arm64/bin/node" if Path("/opt/node-v24.21.0-linux-arm64/bin/node").is_file() else shutil.which("node") or "node"
))


def test_case_sensitive_directory_entry_selection() -> None:
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    source = f"""import assert from 'node:assert/strict';
import {{ selectDirectoryEntry }} from '{(ROOT / 'mcp-node/release.mjs').as_uri()}';
const ordinary = {{ name: 'CURRENT.JSON', isSymbolicLink: () => false }};
const reparse = {{ name: 'current.json', isSymbolicLink: () => true }};
for (const items of [[ordinary, reparse], [reparse, ordinary]]) {{
  assert.equal(selectDirectoryEntry(items, 'current.json'), reparse);
  assert.equal(selectDirectoryEntry(items, 'CURRENT.JSON'), ordinary);
  assert.equal(selectDirectoryEntry(items, 'Current.Json'), undefined);
}}
assert.equal(selectDirectoryEntry([ordinary], 'current.json'), ordinary);
"""
    result = subprocess.run([str(NODE), "--input-type=module", "-e", source], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
