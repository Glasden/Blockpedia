"""A slow Node query must leave the MCP protocol loop responsive."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get("BLOCKPEDIA_NODE") or (
    "/opt/node-v24.21.0-linux-arm64/bin/node" if Path("/opt/node-v24.21.0-linux-arm64/bin/node").is_file() else shutil.which("node") or "node"
))


def test_slow_query_does_not_block_ping_and_invalid_result_fails_closed(tmp_path: Path) -> None:
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    fixtures = ROOT / "tests" / "schema" / "fixtures" / "mcp"
    info = json.loads((fixtures / "mcp-index-info-output.v1.valid.json").read_text())
    info["data"]["built_at"] = "invalid timestamp"
    search = json.loads((fixtures / "mcp-search-blocks-output.v1.valid.json").read_text())
    del search["request_id"]
    details = json.loads((fixtures / "mcp-block-details-output.v1.valid.json").read_text())
    details["data"]["block_facts"]["has_item"] = "invalid boolean"
    payloads = {"index_info": info, "search_blocks": search, "get_block_details": details, "compare_blocks": {"schema_version": "mcp-error.v1"}}
    worker = tmp_path / "slow-worker.mjs"
    worker.write_text(
        """import { parentPort } from 'node:worker_threads';
const payloads = """ + json.dumps(payloads, ensure_ascii=False) + """;
parentPort.on('message', ({ id, name }) => {
  if (name === 'index_info') Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 1200);
  parentPort.postMessage({ id, result: {
    structuredContent: payloads[name], images: [], isError: name === 'compare_blocks'
  }});
});
""",
        encoding="utf-8",
    )
    entry = tmp_path / "entry.mjs"
    entry.write_text(
        f"""import {{ createServer }} from '{(ROOT / 'mcp-node/server.mjs').as_uri()}';
import {{ StdioServerTransport }} from '{(ROOT / 'mcp-node/node_modules/@modelcontextprotocol/server/dist/stdio.mjs').as_uri()}';
await createServer('{tmp_path.as_posix()}', new URL('./slow-worker.mjs', import.meta.url)).connect(new StdioServerTransport());
""",
        encoding="utf-8",
    )
    process = subprocess.Popen([str(NODE), str(entry)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin and process.stdout and process.stderr

    def send(message: dict) -> None:
        process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
        process.stdin.flush()

    with ThreadPoolExecutor(max_workers=1) as reader:
        def receive() -> dict:
            line = reader.submit(process.stdout.readline).result(timeout=6)
            assert line
            return json.loads(line)

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "worker-check", "version": "1"}}})
            assert receive()["id"] == 1
            send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "index_info", "arguments": {}}})
            send({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": {}})
            assert receive()["id"] == 3
            slow = receive()
            assert slow["id"] == 2
            assert slow["result"]["isError"] is True
            assert slow["result"]["structuredContent"]["error_code"] == "MCP_INTERNAL_ERROR"
            for id, name, args in [
                (4, "search_blocks", {"keywords": ["stone"]}),
                (5, "get_block_details", {"block_id": "minecraft:stone"}),
                (6, "compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"]}),
            ]:
                send({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args}})
                rejected = receive()
                assert rejected["id"] == id
                assert rejected["result"]["structuredContent"]["error_code"] == "MCP_INTERNAL_ERROR"
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    assert process.returncode == 0, process.stderr.read().decode("utf-8", "replace")
    assert process.stdout.read() == b""


def test_worker_exit_fails_current_and_later_requests(tmp_path: Path) -> None:
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    (tmp_path / "dead-worker.mjs").write_text(
        "import { parentPort } from 'node:worker_threads'; parentPort.once('message', () => process.exit(17));\n",
        encoding="utf-8",
    )
    entry = tmp_path / "entry.mjs"
    entry.write_text(
        f"""import {{ createServer }} from '{(ROOT / 'mcp-node/server.mjs').as_uri()}';
import {{ StdioServerTransport }} from '{(ROOT / 'mcp-node/node_modules/@modelcontextprotocol/server/dist/stdio.mjs').as_uri()}';
await createServer('{tmp_path.as_posix()}', new URL('./dead-worker.mjs', import.meta.url)).connect(new StdioServerTransport());
""",
        encoding="utf-8",
    )
    process = subprocess.Popen([str(NODE), str(entry)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin and process.stdout and process.stderr
    with ThreadPoolExecutor(max_workers=1) as reader:
        def exchange(message: dict) -> dict:
            process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            process.stdin.flush()
            line = reader.submit(process.stdout.readline).result(timeout=6)
            assert line
            return json.loads(line)

        try:
            assert exchange({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "worker-exit", "version": "1"}}})["id"] == 1
            process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized","params":{}}\n')
            process.stdin.flush()
            for id in (2, 3):
                result = exchange({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": "index_info", "arguments": {}}})
                assert result["id"] == id
                assert result["result"]["structuredContent"]["error_code"] == "MCP_INTERNAL_ERROR"
            assert exchange({"jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}})["id"] == 4
        finally:
            process.stdin.close()
            try:
                process.wait(timeout=6)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    assert process.returncode == 0, process.stderr.read().decode("utf-8", "replace")
    assert process.stdout.read() == b""
