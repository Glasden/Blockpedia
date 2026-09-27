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
        and stderr.splitlines()[1].lower().startswith(("(use `node --trace-warnings", "(use `node.exe --trace-warnings"))
    ), stderr


def call(send, id: int, name: str, arguments: dict) -> dict:
    response = send({"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert response and "result" in response, response
    return response["result"]


def _add_search_block(db: sqlite3.Connection, block_id: str, zh: str, en: str, *, qualification: str = "eligible", warning: str | None = None, visual: bool = True) -> None:
    source = "minecraft:stone"
    block = json.loads(db.execute("SELECT record_json FROM blocks WHERE block_id=?", (source,)).fetchone()[0])
    block.update(block_id=block_id, default_state_id=block_id, translation_key="block." + block_id.removeprefix("minecraft:"))
    block["official_names"] = {"zh_cn": zh, "en_us": en}
    db.execute("INSERT INTO blocks SELECT ?,minecraft_version,?, ?, ?, ?,machine_facts_json,? FROM blocks WHERE block_id=?", (block_id, block["translation_key"], zh, en, block_id, json.dumps(block), source))
    state = json.loads(db.execute("SELECT record_json FROM states WHERE state_id=?", (source,)).fetchone()[0])
    state.update(state_id=block_id, block_id=block_id, variant_ids=[block_id] if visual else [])
    db.execute("INSERT INTO states SELECT ?,?,properties_json,is_default,? FROM states WHERE state_id=?", (block_id, block_id, json.dumps(state), source))
    if not visual:
        return
    variant = json.loads(db.execute("SELECT record_json FROM visual_variants WHERE variant_id=?", (source,)).fetchone()[0])
    variant.update(variant_id=block_id, block_id=block_id, canonical_state_id=block_id, represented_state_ids=[block_id], candidate_qualification=qualification, warnings=[warning] if warning else [])
    variant["machine_facts"]["behavior_by_state"] = {block_id: variant["machine_facts"]["behavior_by_state"][source]}
    db.execute("INSERT INTO visual_variants SELECT ?,?,?,?,preview_path,mask_path,render_metadata_path,image_sha256,mask_sha256,render_metadata_sha256,?,?,?,feature_json FROM visual_variants WHERE variant_id=?", (block_id, block_id, block_id, json.dumps([block_id]), qualification, json.dumps(variant["warnings"]), json.dumps(variant), source))
    annotation = json.loads(db.execute("SELECT semantic_json FROM annotations WHERE variant_id=?", (source,)).fetchone()[0])
    annotation.update(synonyms_en=["bugstone"] if block_id == "minecraft:infested_stone" else [], summary_en=en)
    db.execute("INSERT INTO annotations VALUES (?,?)", (block_id, json.dumps(annotation)))
    table = "search_fts" if db.execute("SELECT name FROM sqlite_master WHERE name='search_fts'").fetchone() else "search_text"
    db.execute(f"INSERT INTO {table}(variant_id,normalized_text) VALUES (?,?)", (block_id, f"{zh} {en.lower()} wall {' '.join(annotation['synonyms_en'])}"))


@pytest.mark.parametrize("force_like", [False, True], ids=["fts5", "like"])
def test_e1_local_ranking_exact_names_and_warnings(tmp_path: Path, force_like: bool) -> None:
    fixture = build_fixture(tmp_path, force_like=force_like)
    with sqlite3.connect(fixture.release / "index.sqlite3") as db:
        assert bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='search_fts'").fetchone()) is not force_like
        _add_search_block(db, "minecraft:barrier", "屏障", "Barrier", qualification="conditional", warning="existing review warning")
        _add_search_block(db, "minecraft:command_block", "命令方块", "Command Block")
        _add_search_block(db, "minecraft:infested_stone", "虫蚀石头", "Infested Stone")
        _add_search_block(db, "minecraft:sand", "沙子", "Sand")
        _add_search_block(db, "minecraft:gravel", "沙砾", "Gravel")
        _add_search_block(db, "minecraft:light", "光源方块", "Light", qualification="excluded")
        _add_search_block(db, "minecraft:structure_void", "结构空位", "Structure Void", visual=False)
    with node_session(tmp_path) as send:
        def search(term: str, limit: int = 12) -> list[dict]:
            result = call(send, 50, "search_blocks", {"keywords": [term], "limit": limit})
            assert not result["isError"]
            Draft202012Validator(load_schema(SCHEMAS["search_blocks"])).validate(result["structuredContent"])
            return result["structuredContent"]["data"]["candidates"]

        wall = search("wall")
        by_id = {item["block_id"]: item for item in wall}
        assert [item["block_id"] for item in wall[:1]] == ["minecraft:glass"]
        assert search("wall", 1)[0]["block_id"] == "minecraft:glass"
        assert by_id["minecraft:barrier"]["local_score"] == by_id["minecraft:glass"]["local_score"] * 0.25
        assert by_id["minecraft:infested_stone"]["final_score"] == by_id["minecraft:stone"]["final_score"] * 0.25
        assert by_id["minecraft:barrier"]["score_breakdown"] == by_id["minecraft:glass"]["score_breakdown"]
        assert by_id["minecraft:sand"]["local_score"] == by_id["minecraft:gravel"]["local_score"] == by_id["minecraft:glass"]["local_score"]
        assert [item["block_id"] for item in wall if item["block_id"] in {"minecraft:barrier", "minecraft:command_block"}] == ["minecraft:barrier", "minecraft:command_block"]
        assert "Local recommendation rule" in by_id["minecraft:infested_stone"]["reason"]
        assert "蠹虫风险" in " ".join(by_id["minecraft:infested_stone"]["warnings"])
        assert "existing review warning" in by_id["minecraft:barrier"]["warnings"]
        assert "本地推荐规则" in " ".join(by_id["minecraft:barrier"]["warnings"])
        assert not by_id["minecraft:stone"]["warnings"]
        assert "minecraft:light" not in by_id and "minecraft:structure_void" not in by_id

        for term in ("MINECRAFT:INFESTED_STONE", "  iNfEsTeD   StOnE  ", "  虫蚀石头  "):
            item = search(term, 1)[0]
            assert item["block_id"] == "minecraft:infested_stone"
            assert item["local_score"] == item["final_score"]
            assert item["local_score"] == (0 if term.startswith("MINECRAFT:") else 1)
            assert "Local recommendation rule" not in item["reason"]
        for term in ("infested", "bugstone"):
            item = next(item for item in search(term) if item["block_id"] == "minecraft:infested_stone")
            assert "Local recommendation rule" in item["reason"]
            assert item["local_score"] == 0.25
        assert search("MINECRAFT:BARRIER", 1)[0]["block_id"] == "minecraft:barrier"
        assert search("  BARRIER  ", 1)[0]["local_score"] == 1
        assert search("屏障", 1)[0]["block_id"] == "minecraft:barrier"
        assert search("minecraft:light") == []
        assert search("minecraft:structure_void") == []
        for block_id, expected in (("minecraft:barrier", "本地推荐规则"), ("minecraft:infested_stone", "蠹虫风险"), ("minecraft:structure_void", "本地推荐规则")):
            details = call(send, 51, "get_block_details", {"block_id": block_id})["structuredContent"]
            Draft202012Validator(load_schema(SCHEMAS["get_block_details"])).validate(details)
            assert expected in " ".join(details["warnings"])
            if details["data"]["variants"]:
                assert expected in " ".join(details["data"]["variants"][0]["warnings"])
        assert call(send, 52, "get_block_details", {"block_id": "minecraft:stone"})["structuredContent"]["warnings"] == []


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
