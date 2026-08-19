"""Low-level stdio MCP transport for the four read-only Blockpedia tools."""

from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
try:
    from mcp.shared.exceptions import MCPError
except ImportError:  # pragma: no cover - compatibility with the legacy SDK spelling
    from mcp.shared.exceptions import McpError as MCPError
from mcp.types import (
    INVALID_PARAMS,
    CallToolRequestParams,
    CallToolResult,
    ImageContent,
    ListToolsResult,
    TextContent,
    Tool,
)

from .mcp_query import MCPInputError, MCPProtocolError, MCPQueryService, MCPToolResult
from .paths import resolve_data_root
from .schema import load_schema


PROJECT_VERSION = "0.0.0"
TOOL_NAMES = ("index_info", "search_blocks", "get_block_details", "compare_blocks")
VERSION_PATTERN = r"^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$"
BLOCK_ID_PATTERN = r"^minecraft:[a-z0-9_./-]+$"
STATE_ID_PATTERN = r"^minecraft:[a-z0-9_./-]+(?:\[[a-z0-9_]+=[a-z0-9_.-]+(?:,[a-z0-9_]+=[a-z0-9_.-]+)*\])?$"


def _common_input_schema() -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": [],
        "properties": {
            "minecraft_version": {"type": "string", "pattern": VERSION_PATTERN, "minLength": 3, "maxLength": 11},
        },
    }


def _input_schema(tool_name: str) -> dict[str, Any]:
    schema = _common_input_schema()
    properties = schema["properties"]
    if tool_name == "search_blocks":
        properties.update(
            {
                "query": {"type": "string", "minLength": 1, "maxLength": 2000},
                "limit": {"type": "integer", "minimum": 1, "maximum": 12, "default": 8},
                "query_spec": load_schema("query-spec-output.v1"),
                "context": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "family": {"type": ["string", "null"]},
                        "compare_states": {"type": "boolean", "default": False},
                        "rerank": {"enum": ["auto", "local_only", "required"], "default": "auto"},
                    },
                },
            }
        )
        schema["required"] = ["query"]
    elif tool_name == "get_block_details":
        properties["block_id"] = {"type": "string", "pattern": BLOCK_ID_PATTERN}
        schema["required"] = ["block_id"]
    elif tool_name == "compare_blocks":
        properties.update(
            {
                "block_ids": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 6,
                    "uniqueItems": True,
                    "items": {"type": "string", "pattern": BLOCK_ID_PATTERN},
                },
                "context": {"type": "string", "maxLength": 1000, "default": ""},
                "compare_states": {"type": "boolean", "default": False},
            }
        )
        schema["required"] = ["block_ids"]
    elif tool_name != "index_info":
        raise ValueError("unknown MCP tool")
    return schema


def _output_schema(tool_name: str) -> dict[str, Any]:
    success_schema = load_schema(
        {
            "index_info": "mcp-index-info-output.v1",
            "search_blocks": "mcp-search-blocks-output.v1",
            "get_block_details": "mcp-block-details-output.v1",
            "compare_blocks": "mcp-compare-blocks-output.v1",
        }[tool_name]
    )
    # The advertised result is a protocol-level union.  Each existing branch
    # remains closed and its schema_version const makes the oneOf branches
    # mutually exclusive; no new persisted/output Schema ID is introduced.
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "oneOf": [success_schema, load_schema("mcp-error.v1")],
    }


def _tool(name: str) -> Tool:
    return Tool(
        name=name,
        description=f"Read-only Blockpedia {name} query.",
        inputSchema=_input_schema(name),
        outputSchema=_output_schema(name),
    )


TOOLS = tuple(_tool(name) for name in TOOL_NAMES)


def _invalid_params(message: str) -> BaseException:
    safe_message = message[:500] or "Invalid MCP tool arguments."
    return MCPError(code=INVALID_PARAMS, message=safe_message)


def _internal_result() -> MCPToolResult:
    return MCPToolResult(
        {
            "schema_version": "mcp-error.v1",
            "request_id": "mcp_transport",
            "error_code": "MCP_INTERNAL_ERROR",
            "message": "The MCP tool failed without a safe business result.",
            "retryable": False,
            "minecraft_version": None,
            "details": {
                "release_id": None,
                "available_versions": [],
                "invalid_block_ids": [],
                "field_errors": [],
                "provider_error_code": None,
                "integrity_component": None,
            },
            "warnings": [],
            "images": [],
        },
        is_error=True,
    )


def _text_content(text: str) -> TextContent:
    return TextContent(type="text", text=text)


def _image_content(payload: bytes, mime_type: str) -> ImageContent:
    encoded = base64.b64encode(payload).decode("ascii")
    return ImageContent(type="image", data=encoded, mimeType=mime_type)


def _call_result(result: MCPToolResult) -> CallToolResult:
    structured = dict(result)
    text = json.dumps(structured, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    content: list[Any] = [_text_content(text)]
    image_metadata = structured.get("data", {}).get("images", []) if isinstance(structured.get("data"), Mapping) else []
    for index, payload in enumerate(result.image_bytes):
        metadata = image_metadata[index] if index < len(image_metadata) and isinstance(image_metadata[index], Mapping) else {}
        content.append(_image_content(payload, str(metadata.get("mime_type", "image/png"))))
    return CallToolResult(content=content, structuredContent=structured, isError=bool(result.is_error))


def make_handlers(data_root: str | Path | None) -> tuple[Any, Any]:
    service = MCPQueryService(resolve_data_root(data_root).root)

    async def list_tools(_ctx: Any, _params: Any) -> ListToolsResult:
        return ListToolsResult(tools=list(TOOLS))

    async def call_tool(_ctx: Any, params: CallToolRequestParams) -> CallToolResult:
        deadline = time.monotonic() + 55.0
        try:
            remaining = max(0.0, deadline - time.monotonic())
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    service.call_tool,
                    params.name,
                    params.arguments or {},
                    deadline=deadline,
                ),
                timeout=remaining,
            )
        except asyncio.TimeoutError:
            result = _internal_result()
        except (MCPInputError, MCPProtocolError) as exc:
            raise _invalid_params(str(exc))
        except Exception:
            result = _internal_result()
        return _call_result(result)

    return list_tools, call_tool


def create_server(data_root: str | Path | None = None) -> Server[Any, Any]:
    list_tools, call_tool = make_handlers(data_root)
    return Server("blockpedia", version=PROJECT_VERSION, on_list_tools=list_tools, on_call_tool=call_tool)


async def serve_stdio(data_root: str | Path | None = None) -> None:
    server = create_server(data_root)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def run_stdio(data_root: str | Path | None = None) -> None:
    asyncio.run(serve_stdio(data_root))


__all__ = ["PROJECT_VERSION", "TOOLS", "create_server", "make_handlers", "run_stdio", "serve_stdio"]
