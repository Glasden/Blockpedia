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

from blockpedia import mcp_server
from blockpedia.mcp_server import TOOLS, TOOL_NAMES
from blockpedia.schema import load_schema

from .fixture_builder import build_fixture
from .test_mcp_core import _host_spec


def _tool_wire(tool: Any) -> dict[str, Any]:
    return tool.model_dump(by_alias=True, exclude_none=True)


def _advertised_output_schemas() -> dict[str, dict[str, Any]]:
    return {_tool_wire(tool)["name"]: _tool_wire(tool)["outputSchema"] for tool in TOOLS}


def test_declared_tool_inventory_is_exact_and_ordered() -> None:
    assert tuple(tool.name for tool in TOOLS) == TOOL_NAMES
    for tool in TOOLS:
        wire = _tool_wire(tool)
        assert set(wire) >= {"name", "inputSchema", "outputSchema"}
        assert wire["inputSchema"]["type"] == "object"
        assert wire["inputSchema"]["additionalProperties"] is False
        output_schema = wire["outputSchema"]
        assert output_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert output_schema["type"] == "object"
        assert len(output_schema["oneOf"]) == 2
        branches = output_schema["oneOf"]
        assert all(branch["type"] == "object" and branch["additionalProperties"] is False for branch in branches)
        success_schema_ids = {
            "index_info": "mcp-index-info-output.v1",
            "search_blocks": "mcp-search-blocks-output.v1",
            "get_block_details": "mcp-block-details-output.v1",
            "compare_blocks": "mcp-compare-blocks-output.v1",
        }
        assert {branch["properties"]["schema_version"]["const"] for branch in branches} == {
            success_schema_ids[wire["name"]],
            "mcp-error.v1",
        }


def test_search_only_advertises_the_complete_strict_host_query_spec_schema() -> None:
    by_name = {_tool_wire(tool)["name"]: _tool_wire(tool) for tool in TOOLS}
    search_schema = by_name["search_blocks"]["inputSchema"]
    assert search_schema["properties"]["query_spec"] == load_schema("query-spec-output.v1")
    assert search_schema["properties"]["query_spec"]["additionalProperties"] is False
    assert search_schema["properties"]["query_spec"]["properties"]["hard"]["additionalProperties"] is False
    assert search_schema["properties"]["query_spec"]["properties"]["hard"]["properties"]["behaviors"]["items"]["additionalProperties"] is False
    assert "query_spec" not in by_name["index_info"]["inputSchema"]["properties"]
    assert "query_spec" not in by_name["get_block_details"]["inputSchema"]["properties"]
    assert "query_spec" not in by_name["compare_blocks"]["inputSchema"]["properties"]


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
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "yellow carpet", "context": {"family": "unknown", "rerank": "local_only"}}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_block_details", "arguments": {"block_id": "minecraft:stone"}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "compare_blocks", "arguments": {"block_ids": ["minecraft:stone", "minecraft:glass"]}}},
    ]
    before = {path.relative_to(tmp_path).as_posix(): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    responses, stderr, returncode = asyncio.run(_session(tmp_path, messages))
    assert returncode == 0
    assert stderr == b""
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
            assert "data" not in image_metadata[index]
    family_search = responses[3]["result"]
    assert family_search["isError"] is False
    assert family_search["structuredContent"]["schema_version"] == "mcp-search-blocks-output.v1"
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
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "context": {"family": []}}}},
        {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "context": {"rerank": []}}}},
        {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "compare_blocks", "arguments": {"block_ids": [{}, "minecraft:stone"]}}},
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "limit": 20}}},
    ]
    responses, stderr, returncode = asyncio.run(_session(tmp_path, messages))
    assert returncode == 0
    assert stderr == b""
    assert responses[1]["error"]["code"] == -32601
    assert responses[2]["error"]["code"] == -32602
    assert responses[3]["error"]["code"] == -32602
    business_result = responses[4]["result"]
    business_structured = business_result["structuredContent"]
    business_text = next(item["text"] for item in business_result["content"] if item["type"] == "text")
    assert business_result["isError"] is True
    assert json.loads(business_text) == business_structured
    assert business_structured["error_code"] == "BLOCK_NOT_FOUND"
    Draft202012Validator(_advertised_output_schemas()["get_block_details"]).validate(business_structured)
    assert responses[5]["error"]["code"] == -32602
    assert responses[6]["error"]["code"] == -32602
    assert responses[7]["error"]["code"] == -32602
    assert responses[8]["error"]["code"] == -32602
    assert responses[9]["error"]["code"] == -32602


def test_stdio_host_query_spec_shape_errors_are_protocol_errors_and_valid_reaches_execution(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    valid = _host_spec()
    partial = {}
    missing_required = json.loads(json.dumps(valid))
    del missing_required["unknown_terms"]
    unknown_nested = json.loads(json.dumps(valid))
    unknown_nested["hard"]["unexpected"] = True
    range_invalid = json.loads(json.dumps(valid))
    range_invalid["soft"]["colors"] = [{"term": "x" * 65, "source": "user_explicit", "weight": 1.0}]
    messages = _initialize() + [
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "query_spec": None}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "query_spec": partial}}},
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "query_spec": missing_required}}},
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "query_spec": unknown_nested}}},
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "query_spec": range_invalid}}},
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "query_spec": valid, "context": {"rerank": "local_only"}}}},
    ]
    responses, stderr, returncode = asyncio.run(_session(tmp_path, messages))
    assert returncode == 0
    assert stderr == b""
    assert [responses[index]["error"]["code"] for index in range(1, 6)] == [-32602] * 5
    assert responses[6]["result"]["isError"] is False
    assert responses[6]["result"]["structuredContent"]["data"]["candidates"]
    assert all(response.get("jsonrpc") == "2.0" for response in responses)


def test_stdio_clean_eof_has_only_json_stdout(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    responses, stderr, returncode = asyncio.run(_session(tmp_path, _initialize() + [{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}]))
    assert returncode == 0
    assert stderr == b""
    assert all(isinstance(response, dict) and response.get("jsonrpc") == "2.0" for response in responses)


def test_call_handler_passes_one_absolute_deadline_to_service(tmp_path: Path, monkeypatch) -> None:
    build_fixture(tmp_path)
    deadlines: list[float] = []
    service_threads: list[int] = []

    def call_tool(_service: Any, _name: str, _arguments: dict[str, Any], *, deadline: float | None = None) -> Any:
        assert deadline is not None
        deadlines.append(deadline)
        service_threads.append(threading.get_ident())
        return mcp_server._internal_result()

    monkeypatch.setattr(mcp_server.MCPQueryService, "call_tool", call_tool)
    _list_tools, call_tool_handler = mcp_server.make_handlers(tmp_path)
    event_loop_threads: list[int] = []

    async def invoke() -> Any:
        event_loop_threads.append(threading.get_ident())
        return await call_tool_handler(None, SimpleNamespace(name="index_info", arguments={}))

    result = asyncio.run(invoke())
    assert deadlines and 54.0 < deadlines[0] - mcp_server.time.monotonic() <= 55.0
    assert event_loop_threads and service_threads and event_loop_threads[0] != service_threads[0]
    assert result.is_error is True
    assert result.structured_content["error_code"] == "MCP_INTERNAL_ERROR"


def test_call_handler_maps_outer_timeout_to_safe_internal_result(tmp_path: Path, monkeypatch) -> None:
    build_fixture(tmp_path)
    seen: list[float] = []

    async def timeout(_awaitable: Any, timeout: float) -> Any:
        seen.append(timeout)
        _awaitable.close()
        raise asyncio.TimeoutError

    monkeypatch.setattr(mcp_server.asyncio, "wait_for", timeout)
    _list_tools, call_tool_handler = mcp_server.make_handlers(tmp_path)
    result = asyncio.run(call_tool_handler(None, SimpleNamespace(name="index_info", arguments={})))
    assert seen and 54.0 < seen[0] <= 55.0
    assert result.is_error is True
    assert result.structured_content["error_code"] == "MCP_INTERNAL_ERROR"
