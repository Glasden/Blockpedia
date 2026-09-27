"""Compare the Node stdio reader with the Python reader on one release."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from blockpedia.mcp_query import MCPQueryService
from blockpedia.mcp_server import TOOLS
from blockpedia.png import decode_rgba_png
from .fixture_builder import build_fixture


ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get("BLOCKPEDIA_NODE") or (
    "/opt/node-v24.21.0-linux-arm64/bin/node" if Path("/opt/node-v24.21.0-linux-arm64/bin/node").is_file() else shutil.which("node") or "node"
))


def _inventory(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _send(process: subprocess.Popen[bytes], reader: ThreadPoolExecutor, message: dict) -> dict | None:
    assert process.stdin and process.stdout
    process.stdin.write((json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    process.stdin.flush()
    if "id" not in message:
        return None
    line = reader.submit(process.stdout.readline).result(timeout=20)
    assert line, "Node MCP exited before replying"
    return json.loads(line)


def _without_encoded_image_identity(value: dict) -> dict:
    result = copy.deepcopy(value)
    if result["schema_version"] in {"mcp-search-blocks-output.v1", "mcp-compare-blocks-output.v1"}:
        result["data"]["contact_sheet"]["image_id"] = None
        for image in result["data"]["images"]:
            image.pop("image_id")
            image.pop("sha256")
    return result


@pytest.mark.parametrize("force_like", [False, True], ids=["fts5", "like"])
def test_node_stdio_four_tools_match_python_and_write_nothing(tmp_path: Path, force_like: bool) -> None:
    if not NODE.is_file():
        pytest.skip("Node 24.21.0 is unavailable")
    fixture = build_fixture(tmp_path, force_like=force_like)
    with sqlite3.connect(fixture.release / "index.sqlite3") as db:
        row = db.execute("SELECT record_json FROM blocks WHERE block_id=?", ("minecraft:stone",)).fetchone()
        record = json.loads(row[0])
        record["official_names"]["zh_cn"] = ""
        db.execute("UPDATE blocks SET record_json=? WHERE block_id=?", (json.dumps(record), "minecraft:stone"))
        table = "search_text" if force_like else "search_fts"
        db.execute(f"UPDATE {table} SET normalized_text=normalized_text || ? WHERE variant_id=?", (" 😀a", "minecraft:stone"))
    before = _inventory(tmp_path)
    python = MCPQueryService(tmp_path)
    process = subprocess.Popen(
        [str(NODE), str(ROOT / "mcp-node" / "server.mjs"), "--data-root", str(tmp_path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    reader = ThreadPoolExecutor(max_workers=1)
    try:
        response = _send(process, reader, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "parity", "version": "1"}}})
        assert response and "result" in response, response
        _send(process, reader, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        listed = _send(process, reader, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listed and [tool["name"] for tool in listed["result"]["tools"]] == [tool.name for tool in TOOLS]
        assert all(tool["annotations"]["readOnlyHint"] is True for tool in listed["result"]["tools"])
        calls = [
            ("index_info", {}),
            ("search_blocks", {"keywords": ["yellow", "carpet"]}),
            ("get_block_details", {"block_id": "minecraft:stone"}),
            ("compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"]}),
        ]
        for index, (name, args) in enumerate(calls, start=3):
            wire = _send(process, reader, {"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": args}})
            assert wire and "result" in wire, wire
            result = wire["result"]
            expected = python.call_tool(name, args)
            assert _without_encoded_image_identity(result["structuredContent"]) == _without_encoded_image_identity(dict(expected))
            assert result["isError"] is expected.is_error
            assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
            images = [base64.b64decode(item["data"], validate=True) for item in result["content"][1:]]
            assert len(images) == len(expected.image_bytes)
            for image, reference, metadata in zip(images, expected.image_bytes, result["structuredContent"].get("data", {}).get("images", []), strict=True):
                assert decode_rgba_png(image) == decode_rgba_png(reference)
                digest = hashlib.sha256(image).hexdigest()
                assert metadata["sha256"] == "sha256:" + digest
                assert metadata["image_id"] == "img_" + digest[:24]
            schema = next(tool.output_schema for tool in TOOLS if tool.name == name)
            Draft202012Validator(schema).validate(result["structuredContent"])
        invalid = _send(process, reader, {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"keywords": []}}})
        assert invalid and invalid["error"]["code"] == -32602
        missing = _send(process, reader, {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "get_block_details", "arguments": {"block_id": "minecraft:not_in_release"}}})
        assert missing and missing["result"]["structuredContent"]["error_code"] == "BLOCK_NOT_FOUND"
        for id, keyword in [(9, "ﬆone"), (10, "😀a")]:
            result = _send(process, reader, {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"keywords": [keyword]}}})
            reference = python.search_blocks({"keywords": [keyword]})
            assert result and "result" in result, result
            assert [item["block_id"] for item in result["result"]["structuredContent"]["data"]["candidates"]] == [item["block_id"] for item in reference["data"]["candidates"]] == ["minecraft:stone"]
            assert result["result"]["structuredContent"]["data"]["candidates"][0]["display_name"] == "Stone"
        for id, name, args in [
            (11, "search_blocks", {"keywords": ["\x85"]}),
            (12, "search_blocks", {"keywords": ["stone"], "limit": None}),
            (13, "compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"], "context": None}),
            (14, "compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"], "compare_states": None}),
        ]:
            rejected = _send(process, reader, {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args}})
            assert rejected and rejected["error"]["code"] == -32602, rejected
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        reader.shutdown(wait=True)
    assert process.returncode == 0
    assert process.stderr
    stderr = process.stderr.read().decode("utf-8", "replace")
    assert stderr == "" or (
        len(stderr.splitlines()) == 2
        and "ExperimentalWarning: SQLite is an experimental feature" in stderr
        and stderr.splitlines()[1].startswith("(Use `node --trace-warnings")
    ), stderr
    assert _inventory(tmp_path) == before
