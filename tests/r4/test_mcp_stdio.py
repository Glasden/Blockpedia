from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import json
import os
import sys
from pathlib import Path
from typing import Any

from blockpedia.mcp_server import TOOLS, TOOL_NAMES

from .fixture_builder import build_fixture


def _tool_wire(tool: Any) -> dict[str, Any]:
    return tool.model_dump(by_alias=True, exclude_none=True)


def test_declared_tool_inventory_is_exact_and_ordered() -> None:
    assert tuple(tool.name for tool in TOOLS) == TOOL_NAMES
    for tool in TOOLS:
        wire = _tool_wire(tool)
        assert set(wire) >= {"name", "inputSchema", "outputSchema"}
        assert wire["inputSchema"]["type"] == "object"
        assert wire["inputSchema"]["additionalProperties"] is False
        assert wire["outputSchema"]["additionalProperties"] is False


def test_locked_mcp_sdk_version() -> None:
    assert importlib.metadata.version("mcp") == "2.0.0"


async def _session(data_root: Path, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bytes, int]:
    env = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "from blockpedia.cli import main; raise SystemExit(main())",
        "mcp",
        "--data-root",
        str(data_root),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    responses: list[dict[str, Any]] = []
    for message in messages:
        process.stdin.write((json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        await process.stdin.drain()
        if "id" in message:
            line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
            assert line
            responses.append(json.loads(line.decode("utf-8")))
    process.stdin.close()
    await process.stdin.wait_closed()
    returncode = await asyncio.wait_for(process.wait(), timeout=10)
    stderr = await process.stderr.read()
    remaining = await process.stdout.read()
    assert not remaining
    return responses, stderr, returncode


def _initialize() -> list[dict[str, Any]]:
    return [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "blockpedia-r4-test", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    ]


def test_stdio_lists_exact_tools_and_calls_all_tools(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    messages = _initialize() + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "index_info", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "yellow carpet", "context": {"rerank": "local_only"}}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_block_details", "arguments": {"block_id": "minecraft:stone"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "compare_blocks", "arguments": {"block_ids": ["minecraft:stone", "minecraft:glass"]}}},
    ]
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    responses, stderr, returncode = asyncio.run(_session(tmp_path, messages))
    assert returncode == 0
    assert stderr == b""
    assert [tool["name"] for tool in responses[1]["result"]["tools"]] == list(TOOL_NAMES)
    for response in responses[2:]:
        result = response["result"]
        structured = result["structuredContent"]
        text = next(item["text"] for item in result["content"] if item["type"] == "text")
        assert json.loads(text) == structured
        assert result.get("isError", False) is False
        image_items = [item for item in result["content"] if item["type"] == "image"]
        image_metadata = structured.get("data", {}).get("images", [])
        assert len(image_items) == len(image_metadata)
        for index, image in enumerate(image_items):
            assert base64.b64decode(image["data"], validate=True)
            assert image_metadata[index]["content_index"] == index + 1
            assert "data" not in image_metadata[index]
    after = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_stdio_protocol_errors_and_business_error_layers(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    messages = _initialize() + [
        {"jsonrpc": "2.0", "id": 2, "method": "unknown/method", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "unknown_tool", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "x", "unknown": True}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_block_details", "arguments": {"block_id": "minecraft:not_in_release"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "context": None}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "context": {"rerank": []}}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "compare_blocks", "arguments": {"block_ids": [{}, "minecraft:stone"]}}},
    ]
    responses, stderr, returncode = asyncio.run(_session(tmp_path, messages))
    assert returncode == 0
    assert stderr == b""
    assert responses[1]["error"]["code"] == -32601
    assert responses[2]["error"]["code"] == -32602
    assert responses[3]["error"]["code"] == -32602
    assert responses[4]["result"]["isError"] is True
    assert responses[4]["result"]["structuredContent"]["error_code"] == "BLOCK_NOT_FOUND"
    assert responses[5]["error"]["code"] == -32602
    assert responses[6]["error"]["code"] == -32602
    assert responses[7]["error"]["code"] == -32602


def test_stdio_clean_eof_has_only_json_stdout(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    responses, stderr, returncode = asyncio.run(_session(tmp_path, _initialize() + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]))
    assert returncode == 0
    assert stderr == b""
    assert all(isinstance(response, dict) and response.get("jsonrpc") == "2.0" for response in responses)
