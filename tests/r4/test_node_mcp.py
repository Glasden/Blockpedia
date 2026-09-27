"""Exercise the Node MCP process against a temporary published release."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from blockpedia.png import decode_rgba_png
from blockpedia.schema import load_schema
from .fixture_builder import build_fixture


ROOT = Path(__file__).resolve().parents[2]
NODE = Path(os.environ.get("BLOCKPEDIA_NODE") or (
    "/opt/node-v24.21.0-linux-arm64/bin/node" if Path("/opt/node-v24.21.0-linux-arm64/bin/node").is_file() else shutil.which("node") or "node"
))
SCHEMAS = {
    "index_info": "mcp-index-info-output.v1",
    "search_blocks": "mcp-search-blocks-output.v1",
    "get_block_details": "mcp-block-details-output.v1",
    "compare_blocks": "mcp-compare-blocks-output.v1",
}


def _inventory(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _pixel(image, x: int, y: int) -> bytes:
    start = (y * image.width + x) * 4
    return image.pixels[start:start + 4]


def _send(process: subprocess.Popen[bytes], reader: ThreadPoolExecutor, message: dict) -> dict | None:
    assert process.stdin and process.stdout
    process.stdin.write((json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
    process.stdin.flush()
    if "id" not in message:
        return None
    line = reader.submit(process.stdout.readline).result(timeout=20)
    assert line, "Node MCP exited before replying"
    return json.loads(line)


@contextmanager
def node_session(data_root: Path):
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    process = subprocess.Popen(
        [str(NODE), str(ROOT / "mcp-node" / "server.mjs"), "--data-root", str(data_root)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    reader = ThreadPoolExecutor(max_workers=1)
    try:
        def send(message: dict) -> dict | None:
            return _send(process, reader, message)

        initialized = send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "blockpedia-test", "version": "1"}}})
        assert initialized and "result" in initialized, initialized
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        yield send
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
    assert process.stdout and process.stdout.read() == b""
    assert process.stderr
    stderr = process.stderr.read().decode("utf-8", "replace")
    assert stderr == "" or (
        len(stderr.splitlines()) == 2
        and "ExperimentalWarning: SQLite is an experimental feature" in stderr
        and stderr.splitlines()[1].startswith("(Use `node --trace-warnings")
    ), stderr


def call(send, id: int, name: str, arguments: dict) -> dict:
    response = send({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert response and "result" in response, response
    return response["result"]


@pytest.mark.parametrize("force_like", [False, True], ids=["fts5", "like"])
def test_four_tools_schemas_images_unicode_and_zero_writes(tmp_path: Path, force_like: bool) -> None:
    fixture = build_fixture(tmp_path, force_like=force_like)
    with sqlite3.connect(fixture.release / "index.sqlite3") as db:
        record = json.loads(db.execute("SELECT record_json FROM blocks WHERE block_id=?", ("minecraft:stone",)).fetchone()[0])
        record["official_names"]["zh_cn"] = ""
        db.execute("UPDATE blocks SET record_json=? WHERE block_id=?", (json.dumps(record), "minecraft:stone"))
        table = "search_text" if force_like else "search_fts"
        db.execute(f"UPDATE {table} SET normalized_text=normalized_text || ? WHERE variant_id=?", (" 😀a", "minecraft:stone"))
    before = _inventory(tmp_path)
    with node_session(tmp_path) as send:
        listed = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listed and [tool["name"] for tool in listed["result"]["tools"]] == list(SCHEMAS)
        for tool in listed["result"]["tools"]:
            assert tool["annotations"]["readOnlyHint"] is True
            branches = tool["outputSchema"]["oneOf"]
            assert {branch["properties"]["schema_version"]["const"] for branch in branches} == {SCHEMAS[tool["name"]], "mcp-error.v1"}
        calls = [
            ("index_info", {}),
            ("search_blocks", {"keywords": ["yellow", "carpet"]}),
            ("get_block_details", {"block_id": "minecraft:stone"}),
            ("compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"]}),
        ]
        for id, (name, args) in enumerate(calls, start=3):
            result = call(send, id, name, args)
            structured = result["structuredContent"]
            assert result["isError"] is False
            assert structured["schema_version"] == SCHEMAS[name]
            assert structured["minecraft_version"] == "26.2"
            assert structured["resolved_release_id"] == fixture.release.name
            assert json.loads(result["content"][0]["text"]) == structured
            Draft202012Validator(load_schema(SCHEMAS[name])).validate(structured)
            images = [base64.b64decode(item["data"], validate=True) for item in result["content"][1:]]
            metadata = structured["data"].get("images", [])
            assert len(images) == len(metadata)
            for index, (image, meta) in enumerate(zip(images, metadata, strict=True), start=1):
                decoded = decode_rgba_png(image)
                digest = hashlib.sha256(image).hexdigest()
                assert (meta["width"], meta["height"]) == (decoded.width, decoded.height)
                assert meta["sha256"] == "sha256:" + digest
                assert meta["image_id"] == "img_" + digest[:24]
                assert meta["content_index"] == index
            if name == "search_blocks":
                assert [(item["block_id"], item["local_score"]) for item in structured["data"]["candidates"]] == [("minecraft:yellow_carpet", 0.48389055)]
                assert structured["data"]["hard_filters"] == []
                assert structured["data"]["reranked_by_llm"] is False
                assert all(item["score_source"] == "local" for item in structured["data"]["candidates"])
                assert structured["data"]["contact_sheet"]["tile_mapping"] == [{"candidate_id": "T01", "variant_id": "minecraft:yellow_carpet", "block_id": "minecraft:yellow_carpet", "row": 0, "column": 0}]
                assert metadata[0]["mapping"] == [{"candidate_id": "T01", "variant_id": "minecraft:yellow_carpet", "block_id": "minecraft:yellow_carpet"}]
                preview = decode_rgba_png((fixture.release / "previews/minecraft/yellow_carpet/preview.png").read_bytes())
                assert _pixel(decode_rgba_png(images[0]), 256, 256) == _pixel(preview, 256, 256)
            if name == "get_block_details":
                assert structured["data"]["block_id"] == "minecraft:stone"
                assert images == [(fixture.release / "previews/minecraft/stone/preview.png").read_bytes()]
            if name == "compare_blocks":
                assert structured["data"]["block_ids"] == ["minecraft:stone", "minecraft:glass"]
                assert [tile["variant_id"] for tile in structured["data"]["contact_sheet"]["tile_mapping"]] == ["minecraft:stone", "minecraft:glass"]
                assert [item["variant_id"] for item in metadata[0]["mapping"]] == ["minecraft:stone", "minecraft:glass"]
                sheet = decode_rgba_png(images[0])
                for x, name in [(256, "stone"), (768, "glass")]:
                    preview = decode_rgba_png((fixture.release / f"previews/minecraft/{name}/preview.png").read_bytes())
                    assert _pixel(sheet, x, 256) == _pixel(preview, 256, 256)
        missing = call(send, 7, "get_block_details", {"block_id": "minecraft:not_in_release"})
        assert missing["isError"] and missing["structuredContent"]["error_code"] == "BLOCK_NOT_FOUND"
        Draft202012Validator(load_schema("mcp-error.v1")).validate(missing["structuredContent"])
        for id, keyword in [(8, "ﬆone"), (9, "😀a")]:
            result = call(send, id, "search_blocks", {"keywords": [keyword]})
            candidates = result["structuredContent"]["data"]["candidates"]
            assert [item["block_id"] for item in candidates] == ["minecraft:stone"]
            assert candidates[0]["display_name"] == "Stone"
        ranked = call(send, 14, "search_blocks", {"keywords": ["yellow", "stone", "glass"]})
        ranked_data = ranked["structuredContent"]["data"]
        assert [(item["block_id"], item["local_score"]) for item in ranked_data["candidates"]] == [
            ("minecraft:yellow_carpet", 0.90729479), ("minecraft:stone", 0.73314455), ("minecraft:glass", 0.63587496),
        ]
        assert [item["candidate_id"] for item in ranked_data["candidates"]] == ["T01", "T02", "T03"]
        assert [item["variant_id"] for item in ranked_data["contact_sheet"]["tile_mapping"]] == [item["variant_id"] for item in ranked_data["candidates"]]
        assert [item["variant_id"] for item in ranked_data["images"][0]["mapping"]] == [item["variant_id"] for item in ranked_data["candidates"]]
        sheet = decode_rgba_png(base64.b64decode(ranked["content"][1]["data"], validate=True))
        for index, candidate in enumerate(ranked_data["candidates"]):
            preview_name = candidate["variant_id"].removeprefix("minecraft:")
            preview = decode_rgba_png((fixture.release / f"previews/minecraft/{preview_name}/preview.png").read_bytes())
            assert _pixel(sheet, index * 512 + 256, 256) == _pixel(preview, 256, 256)
        empty = call(send, 15, "search_blocks", {"keywords": ["term-not-present"]})
        assert empty["isError"] is False
        assert empty["structuredContent"]["data"]["candidates"] == []
        assert empty["structuredContent"]["data"]["images"] == []
        for id, name, args in [
            (10, "search_blocks", {"keywords": ["\x85"]}),
            (11, "search_blocks", {"keywords": ["stone"], "limit": None}),
            (12, "compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"], "context": None}),
            (13, "compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"], "compare_states": None}),
            (16, "search_blocks", {"query": "stone"}),
            (17, "search_blocks", {"keywords": ["stone"], "limit": 0}),
            (18, "search_blocks", {"keywords": ["x"] * 17}),
        ]:
            rejected = send({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": args}})
            assert rejected and rejected["error"]["code"] == -32602, rejected
    assert _inventory(tmp_path) == before


def test_pointer_switch_is_seen_on_next_request(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    with node_session(tmp_path) as send:
        first = call(send, 2, "index_info", {})["structuredContent"]
        second_id = "rel_" + "2" * 32
        second_path = fixture.release.parent / second_id
        shutil.copytree(fixture.release, second_path)
        for name in ("release.json", "manifest.json"):
            file = second_path / name
            value = json.loads(file.read_text(encoding="utf-8"))
            value["release_id"] = second_id
            file.write_text(json.dumps(value), encoding="utf-8")
        current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
        current["versions"]["26.2"].update({"release_id": second_id, "relative_path": f"releases/26.2/{second_id}"})
        (tmp_path / "current.json").write_text(json.dumps(current), encoding="utf-8")
        second = call(send, 3, "index_info", {})["structuredContent"]
        assert first["resolved_release_id"] != second["resolved_release_id"] == second_id


def test_unpublished_version_and_invalid_pointer_fail_closed(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    with node_session(tmp_path) as send:
        unpublished = call(send, 2, "index_info", {"minecraft_version": "99.9"})
        assert unpublished["isError"] and unpublished["structuredContent"]["error_code"] == "VERSION_NOT_AVAILABLE"
        current_file = tmp_path / "current.json"
        current = json.loads(current_file.read_text(encoding="utf-8"))
        current["versions"]["26.2"]["relative_path"] = "releases/26.2/../escape"
        current_file.write_text(json.dumps(current), encoding="utf-8")
        invalid = call(send, 3, "index_info", {})
        assert invalid["isError"] and invalid["structuredContent"]["error_code"] == "CURRENT_POINTER_INVALID"


def test_missing_index_fails_closed_without_writes(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    (fixture.release / "index.sqlite3").unlink()
    before = _inventory(tmp_path)
    with node_session(tmp_path) as send:
        result = call(send, 2, "index_info", {})
        assert result["isError"] is True
        assert result["structuredContent"]["error_code"] == "INDEX_OPEN_FAILED"
    assert _inventory(tmp_path) == before
