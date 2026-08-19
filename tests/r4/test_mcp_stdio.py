from __future__ import annotations

import asyncio
import base64
import importlib.metadata
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator
import pytest

from blockpedia import mcp_server
from blockpedia.mcp_server import TOOLS, TOOL_NAMES
from blockpedia.schema import load_schema

from .fixture_builder import build_fixture


def _tool_wire(tool: Any) -> dict[str, Any]:
    return tool.model_dump(by_alias=True, exclude_none=True)


def _advertised_output_schemas() -> dict[str, dict[str, Any]]:
    return {_tool_wire(tool)["name"]: _tool_wire(tool)["outputSchema"] for tool in TOOLS}


def test_declared_tools_and_search_keywords_schema_are_exact() -> None:
    assert tuple(tool.name for tool in TOOLS) == TOOL_NAMES
    for tool in TOOLS:
        wire = _tool_wire(tool)
        assert set(wire) >= {"name", "inputSchema", "outputSchema"}
        assert wire["inputSchema"]["type"] == "object"
        assert wire["inputSchema"]["additionalProperties"] is False
        output_schema = wire["outputSchema"]
        assert output_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert len(output_schema["oneOf"]) == 2
        assert {branch["properties"]["schema_version"]["const"] for branch in output_schema["oneOf"]} == {
            {"index_info": "mcp-index-info-output.v1", "search_blocks": "mcp-search-blocks-output.v1", "get_block_details": "mcp-block-details-output.v1", "compare_blocks": "mcp-compare-blocks-output.v1"}[wire["name"]],
            "mcp-error.v1",
        }
    search = next(_tool_wire(tool) for tool in TOOLS if tool.name == "search_blocks")
    assert search["inputSchema"]["required"] == ["keywords"]
    assert set(search["inputSchema"]["properties"]) == {"minecraft_version", "keywords", "limit"}
    assert search["inputSchema"]["properties"]["keywords"]["minItems"] == 1
    assert search["inputSchema"]["properties"]["keywords"]["maxItems"] == 16
    assert search["inputSchema"]["properties"]["keywords"]["items"] == {"type": "string", "minLength": 1, "maxLength": 64}
    assert "query" not in search["inputSchema"]["properties"]
    assert "context" not in search["inputSchema"]["properties"]
    assert "query_spec" not in search["inputSchema"]["properties"]


def test_locked_mcp_sdk_version() -> None:
    assert importlib.metadata.version("mcp") == "2.0.0"


async def _session(data_root: Path, messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], bytes, int]:
    env = os.environ.copy()
    source_root = str(Path(__file__).parents[2] / "src")
    env["PYTHONPATH"] = source_root + os.pathsep + env.get("PYTHONPATH", "")
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "from blockpedia.cli import main; raise SystemExit(main())", "mcp", "--data-root", str(data_root),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
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
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "blockpedia-d053-test", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
    ]


def test_stdio_lists_and_calls_all_tools_with_keywords(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    messages = _initialize() + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "index_info", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"keywords": ["yellow", "carpet"]}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_block_details", "arguments": {"block_id": "minecraft:stone"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "compare_blocks", "arguments": {"block_ids": ["minecraft:stone", "minecraft:glass"]}}},
    ]
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    responses, stderr, returncode = asyncio.run(_session(tmp_path, messages))
    assert returncode == 0 and stderr == b""
    assert [tool["name"] for tool in responses[1]["result"]["tools"]] == list(TOOL_NAMES)
    output_schemas = _advertised_output_schemas()
    for tool_name, response in zip(TOOL_NAMES, responses[2:], strict=True):
        result = response["result"]
        structured = result["structuredContent"]
        text = next(item["text"] for item in result["content"] if item["type"] == "text")
        assert json.loads(text) == structured
        assert result.get("isError", False) is False
        Draft202012Validator(output_schemas[tool_name]).validate(structured)
        image_items = [item for item in result["content"] if item["type"] == "image"]
        image_metadata = structured.get("data", {}).get("images", [])
        assert len(image_items) == len(image_metadata)
        for index, image in enumerate(image_items):
            assert base64.b64decode(image["data"], validate=True)
            assert image_metadata[index]["content_index"] == index + 1
    assert responses[3]["result"]["structuredContent"]["data"]["hard_filters"] == []
    assert responses[3]["result"]["structuredContent"]["data"]["reranked_by_llm"] is False
    after = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before


def test_stdio_old_search_fields_and_keyword_bounds_are_protocol_errors(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    messages = _initialize() + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone"}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"keywords": []}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"keywords": ["stone", " stone "]}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"keywords": ["x" * 65]}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_block_details", "arguments": {"block_id": "minecraft:not_in_release"}}},
    ]
    responses, stderr, returncode = asyncio.run(_session(tmp_path, messages))
    assert returncode == 0 and stderr == b""
    assert [responses[index]["error"]["code"] for index in range(1, 6)] == [-32602] * 5
    assert responses[6]["result"]["structuredContent"]["error_code"] == "BLOCK_NOT_FOUND"


def test_call_handler_keeps_asyncio_event_loop_isolated_from_service(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixture(tmp_path)
    event_threads: list[int] = []
    service_threads: list[int] = []

    def call_tool(_service: Any, _name: str, _arguments: dict[str, Any]) -> Any:
        service_threads.append(threading.get_ident())
        return mcp_server._internal_result()

    monkeypatch.setattr(mcp_server.MCPQueryService, "call_tool", call_tool)
    _list_tools, handler = mcp_server.make_handlers(tmp_path)

    async def invoke() -> Any:
        event_threads.append(threading.get_ident())
        return await handler(None, SimpleNamespace(name="index_info", arguments={}))

    result = asyncio.run(invoke())
    assert event_threads and service_threads and event_threads[0] != service_threads[0]
    assert result.is_error is True


def test_stdio_clean_eof_has_only_json_stdout(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    responses, stderr, returncode = asyncio.run(_session(tmp_path, _initialize() + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]))
    assert returncode == 0 and stderr == b""
    assert all(isinstance(response, dict) and response.get("jsonrpc") == "2.0" for response in responses)
