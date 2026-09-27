"""Exercise the Node MCP process against a temporary published release."""

from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
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
    "index_info": "mcp-index-info-output.v2",
    "search_blocks": "mcp-search-blocks-output.v2",
    "get_block_details": "mcp-block-details-output.v2",
    "compare_blocks": "mcp-compare-blocks-output.v3",
}
CARD = 256
WEBP_DECODER = (ROOT / "mcp-node/node_modules/@jsquash/webp/codec/dec/webp_dec.js").as_uri()


def _inventory(root: Path) -> dict[str, bytes]:
    return {path.relative_to(root).as_posix(): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _pixel(image, x: int, y: int) -> bytes:
    start = (y * image.width + x) * 4
    return image.pixels[start:start + 4]


@dataclass(frozen=True)
class RGBA:
    width: int
    height: int
    pixels: bytes


def decode_webp(payloads: list[bytes]) -> list[RGBA]:
    """Decode WebP with the same libwebp wasm package the server ships."""
    script = f"""import {{ readFileSync }} from 'node:fs';
const url = new URL({json.dumps(WEBP_DECODER)});
const {{ default: factory }} = await import(url);
const wasm = new WebAssembly.Module(readFileSync(new URL(url.href.replace(/\\.js$/, '.wasm'))));
const dec = await factory({{ noInitialRun: true, instantiateWasm: (imports, done) => {{ const i = new WebAssembly.Instance(wasm, imports); done(i); return i.exports; }} }});
const out = JSON.parse(readFileSync(0, 'utf8')).map((item) => {{
  const image = dec.decode(Buffer.from(item, 'base64'));
  return {{ width: image.width, height: image.height, pixels: Buffer.from(image.data.buffer, image.data.byteOffset, image.data.length).toString('base64') }};
}});
process.stdout.write(JSON.stringify(out));
"""
    result = subprocess.run([str(NODE), "--input-type=module", "-e", script], input=json.dumps([base64.b64encode(item).decode() for item in payloads]).encode(), capture_output=True, check=True)
    return [RGBA(item["width"], item["height"], base64.b64decode(item["pixels"])) for item in json.loads(result.stdout)]


def nearest(image, size: int = CARD) -> bytes:
    out = bytearray(size * size * 4)
    for y in range(size):
        sy = min(image.height - 1, y * image.height // size)
        for x in range(size):
            sx = min(image.width - 1, x * image.width // size)
            start = (sy * image.width + sx) * 4
            out[(y * size + x) * 4:(y * size + x) * 4 + 4] = image.pixels[start:start + 4]
    return bytes(out)


def result_images(result: dict) -> list[RGBA]:
    items = result["content"][1:]
    assert all(item["type"] == "image" and item["mimeType"] == "image/webp" for item in items)
    return decode_webp([base64.b64decode(item["data"], validate=True) for item in items])


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


def _add_search_block(db: sqlite3.Connection, block_id: str, zh: str, en: str, *, qualification: str = "eligible", warning: str | None = None, visual: bool = True, tags: list[str] | None = None, geometry: dict | None = None) -> None:
    source = "minecraft:stone"
    block = json.loads(db.execute("SELECT record_json FROM blocks WHERE block_id=?", (source,)).fetchone()[0])
    block.update(block_id=block_id, default_state_id=block_id, translation_key="block." + block_id.removeprefix("minecraft:"))
    block["official_names"] = {"zh_cn": zh, "en_us": en}
    if tags is not None:
        block["tags"] = tags
    db.execute("INSERT INTO blocks SELECT ?,minecraft_version,?, ?, ?, ?,machine_facts_json,? FROM blocks WHERE block_id=?", (block_id, block["translation_key"], zh, en, block_id, json.dumps(block), source))
    state = json.loads(db.execute("SELECT record_json FROM states WHERE state_id=?", (source,)).fetchone()[0])
    state.update(state_id=block_id, block_id=block_id, variant_ids=[block_id] if visual else [])
    db.execute("INSERT INTO states SELECT ?,?,properties_json,is_default,? FROM states WHERE state_id=?", (block_id, block_id, json.dumps(state), source))
    if not visual:
        return
    variant = json.loads(db.execute("SELECT record_json FROM visual_variants WHERE variant_id=?", (source,)).fetchone()[0])
    variant.update(variant_id=block_id, block_id=block_id, canonical_state_id=block_id, represented_state_ids=[block_id], candidate_qualification=qualification, warnings=[warning] if warning else [])
    variant["machine_facts"]["behavior_by_state"] = {block_id: variant["machine_facts"]["behavior_by_state"][source]}
    variant["machine_facts"]["geometry"].update(geometry or {})
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
        _add_search_block(db, "minecraft:sand", "沙子", "Sand", tags=["minecraft:mineable/shovel", "minecraft:sand"])
        _add_search_block(db, "minecraft:gravel", "沙砾", "Gravel", tags=["minecraft:mineable/shovel"])
        _add_search_block(db, "minecraft:bedrock", "基岩", "Bedrock")
        _add_search_block(db, "minecraft:packed_ice", "浮冰", "Packed Ice", tags=["minecraft:ice"])
        _add_search_block(db, "minecraft:soul_fire", "灵魂火", "Soul Fire", tags=["minecraft:fire"])
        _add_search_block(db, "minecraft:light", "光源方块", "Light", qualification="excluded")
        _add_search_block(db, "minecraft:structure_void", "结构空位", "Structure Void", visual=False)
    with node_session(tmp_path) as send:
        def search(term: str, limit: int = 12) -> list[dict]:
            result = call(send, 50, "search_blocks", {"keywords": [term], "limit": limit})
            assert not result["isError"]
            Draft202012Validator(load_schema(SCHEMAS["search_blocks"])).validate(result["structuredContent"])
            return result["structuredContent"]["candidates"]

        wall = search("wall")
        by_id = {item["block_id"]: item for item in wall}
        assert [item["block_id"] for item in wall[:1]] == ["minecraft:glass"]
        assert search("wall", 1)[0]["block_id"] == "minecraft:glass"
        assert by_id["minecraft:barrier"]["score"] == by_id["minecraft:glass"]["score"] * 0.25
        assert by_id["minecraft:infested_stone"]["score"] == by_id["minecraft:stone"]["score"] * 0.25
        assert by_id["minecraft:bedrock"]["score"] == by_id["minecraft:stone"]["score"] * 0.25
        assert "结构生成" in " ".join(by_id["minecraft:bedrock"]["warnings"])
        assert by_id["minecraft:sand"]["warnings"] == ["行为提示（依据 tag minecraft:sand）：受重力影响，下方悬空时会下落。"]
        assert by_id["minecraft:gravel"]["warnings"] == ["行为提示（本地方块规则）：受重力影响，下方悬空时会下落。"]
        assert "minecraft:fire" in " ".join(by_id["minecraft:soul_fire"]["warnings"])
        assert by_id["minecraft:packed_ice"]["warnings"] == []
        assert by_id["minecraft:soul_fire"]["score"] == by_id["minecraft:packed_ice"]["score"] == by_id["minecraft:glass"]["score"]
        assert by_id["minecraft:barrier"]["score_breakdown"] == by_id["minecraft:glass"]["score_breakdown"]
        assert by_id["minecraft:sand"]["score"] == by_id["minecraft:gravel"]["score"] == by_id["minecraft:glass"]["score"]
        # Equal scores: eligible before conditional, then variant ID.
        assert [item["block_id"] for item in wall if item["block_id"] in {"minecraft:barrier", "minecraft:command_block"}] == ["minecraft:command_block", "minecraft:barrier"]
        assert "Local recommendation rule" in by_id["minecraft:infested_stone"]["reason"]
        assert "蠹虫风险" in " ".join(by_id["minecraft:infested_stone"]["warnings"])
        assert "existing review warning" in by_id["minecraft:barrier"]["warnings"]
        assert "本地推荐规则" in " ".join(by_id["minecraft:barrier"]["warnings"])
        assert not by_id["minecraft:stone"]["warnings"]
        assert "minecraft:light" not in by_id and "minecraft:structure_void" not in by_id

        for term in ("MINECRAFT:INFESTED_STONE", "  iNfEsTeD   StOnE  ", "  虫蚀石头  "):
            item = search(term, 1)[0]
            assert item["block_id"] == "minecraft:infested_stone"
            assert item["score"] == 1
            assert "Local recommendation rule" not in item["reason"]
        for term in ("infested", "bugstone"):
            item = next(item for item in search(term) if item["block_id"] == "minecraft:infested_stone")
            assert "Local recommendation rule" in item["reason"]
            assert 0 < item["score"] <= 0.25
        assert search("MINECRAFT:BARRIER", 1)[0]["block_id"] == "minecraft:barrier"
        assert search("  BARRIER  ", 1)[0]["score"] == 1
        assert search("屏障", 1)[0]["block_id"] == "minecraft:barrier"
        assert search("基岩", 1)[0]["score"] == 1
        assert search("minecraft:light") == []
        assert search("minecraft:structure_void") == []
        for block_id, expected in (("minecraft:barrier", "本地推荐规则"), ("minecraft:infested_stone", "蠹虫风险"), ("minecraft:structure_void", "本地推荐规则"), ("minecraft:bedrock", "结构生成"), ("minecraft:sand", "受重力影响")):
            details = call(send, 51, "get_block_details", {"block_id": block_id})["structuredContent"]
            Draft202012Validator(load_schema(SCHEMAS["get_block_details"])).validate(details)
            assert expected in " ".join(details["warnings"])
            assert len(details["warnings"]) == len(set(details["warnings"]))
        assert "existing review warning" in call(send, 53, "get_block_details", {"block_id": "minecraft:barrier"})["structuredContent"]["warnings"]
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
        annotation = json.loads(db.execute("SELECT semantic_json FROM annotations WHERE variant_id=?", ("minecraft:stone",)).fetchone()[0])
        annotation["synonyms_en"].append("😀a")
        db.execute("UPDATE annotations SET semantic_json=? WHERE variant_id=?", (json.dumps(annotation), "minecraft:stone"))
    before = _inventory(tmp_path)
    with node_session(tmp_path) as send:
        listed = send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listed and [tool["name"] for tool in listed["result"]["tools"]] == list(SCHEMAS)
        for tool in listed["result"]["tools"]:
            assert tool["annotations"]["readOnlyHint"] is True
            branches = tool["outputSchema"]["oneOf"]
            assert {branch["$id"].rsplit(":", 1)[1] for branch in branches} == {SCHEMAS[tool["name"]], "mcp-error.v2"}
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
            assert json.loads(result["content"][0]["text"]) == structured
            Draft202012Validator(load_schema(SCHEMAS[name])).validate(structured)
            assert not {"schema_version", "request_id", "manifest_sha256", "resolved_release_id"} & set(structured)
            images = result_images(result)
            metadata = structured.get("images", [])
            assert len(images) == len(metadata)
            for index, (image, meta) in enumerate(zip(images, metadata, strict=True), start=1):
                assert (meta["width"], meta["height"]) == (image.width, image.height)
                assert meta["mime_type"] == "image/webp"
                assert meta["content_index"] == index
            if name == "index_info":
                assert (structured["minecraft_version"], structured["release_id"]) == ("26.2", fixture.release.name)
            if name == "search_blocks":
                assert [(item["block_id"], item["score"]) for item in structured["candidates"]] == [("minecraft:yellow_carpet", 1)]
                assert metadata[0]["tiles"] == [{"candidate_id": "T01", "block_id": "minecraft:yellow_carpet", "row": 0, "column": 0}]
                assert (images[0].width, images[0].height) == (CARD, CARD)
                preview = decode_rgba_png((fixture.release / "previews/minecraft/yellow_carpet/preview.png").read_bytes())
                assert _pixel(images[0], 128, 128) == _pixel(preview, 256, 256)
            if name == "get_block_details":
                assert structured["block_id"] == "minecraft:stone"
                assert metadata[0]["state_id"] == structured["representative"]["state_id"] == "minecraft:stone"
                preview = decode_rgba_png((fixture.release / "previews/minecraft/stone/preview.png").read_bytes())
                assert images[0].pixels == nearest(preview)
            if name == "compare_blocks":
                assert [(item["candidate_id"], item["block_id"]) for item in structured["blocks"]] == [("T01", "minecraft:stone"), ("T02", "minecraft:glass")]
                assert "transparent" in structured["differing_fields"]
                assert [tile["candidate_id"] for tile in metadata[0]["tiles"]] == ["T01", "T02"]
                assert [(tile["candidate_id"], tile["block_id"], tile["column"]) for tile in metadata[0]["tiles"]] == [("T01", "minecraft:stone", 0), ("T02", "minecraft:glass", 1)]
                assert (images[0].width, images[0].height) == (2 * CARD, CARD)
                for x, name in [(128, "stone"), (384, "glass")]:
                    preview = decode_rgba_png((fixture.release / f"previews/minecraft/{name}/preview.png").read_bytes())
                    assert _pixel(images[0], x, 128) == _pixel(preview, 256, 256)
        missing = call(send, 7, "get_block_details", {"block_id": "minecraft:not_in_release"})
        assert missing["isError"] and missing["structuredContent"] == {
            "error_code": "BLOCK_NOT_FOUND", "message": "The requested block is not in this release.", "invalid_block_ids": ["minecraft:not_in_release"],
        }
        Draft202012Validator(load_schema("mcp-error.v2")).validate(missing["structuredContent"])
        for id, keyword in [(8, "ﬆone"), (9, "😀a")]:
            result = call(send, id, "search_blocks", {"keywords": [keyword]})
            candidates = result["structuredContent"]["candidates"]
            assert [item["block_id"] for item in candidates] == ["minecraft:stone"]
            assert candidates[0]["display_name"] == "Stone"
        ranked = call(send, 14, "search_blocks", {"keywords": ["yellow", "stone", "glass"]})
        ranked_data = ranked["structuredContent"]
        assert [(item["block_id"], item["score"]) for item in ranked_data["candidates"]] == [
            ("minecraft:yellow_carpet", 0.3375), ("minecraft:glass", 0.28125), ("minecraft:stone", 0.28125),
        ]
        assert [item["candidate_id"] for item in ranked_data["candidates"]] == ["T01", "T02", "T03"]
        assert [(item["candidate_id"], item["block_id"]) for item in ranked_data["images"][0]["tiles"]] == [(item["candidate_id"], item["block_id"]) for item in ranked_data["candidates"]]
        [sheet] = result_images(ranked)
        assert (sheet.width, sheet.height) == (3 * CARD, CARD)
        for index, candidate in enumerate(ranked_data["candidates"]):
            preview_name = candidate["block_id"].removeprefix("minecraft:")
            preview = decode_rgba_png((fixture.release / f"previews/minecraft/{preview_name}/preview.png").read_bytes())
            assert _pixel(sheet, index * CARD + 128, 128) == _pixel(preview, 256, 256)
            label = _pixel(sheet, index * CARD + 7, CARD - 5)
            assert label == bytes([0x18, 0x18, 0x18, 0xFF]), "T-label backing must be drawn inside its 256px card"
        empty = call(send, 15, "search_blocks", {"keywords": ["term-not-present"]})
        assert empty["isError"] is False
        assert empty["structuredContent"] == {"candidates": [], "images": []}
        assert len(empty["content"]) == 1
        for id, name, args in [
            (10, "search_blocks", {"keywords": ["\x85"]}),
            (11, "search_blocks", {"keywords": ["stone"], "limit": None}),
            (12, "compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"], "context": "roof"}),
            (13, "compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:glass"], "compare_states": False}),
            (26, "search_blocks", {}),
            (27, "search_blocks", {"similar_to": "stone"}),
            (28, "search_blocks", {"similar_to": None}),
            (16, "search_blocks", {"query": "stone"}),
            (17, "search_blocks", {"keywords": ["stone"], "limit": 0}),
            (18, "search_blocks", {"keywords": ["x"] * 17}),
            (19, "get_block_details", {"block_id": "minecraft:stone", "offset": 0}),
            (20, "get_block_details", {"block_id": "minecraft:stone", "limit": 8}),
            (21, "get_block_details", {"block_id": "minecraft:stone", "detail": "full"}),
            (22, "get_block_details", {"block_id": "minecraft:stone", "detail": "states", "limit": 17}),
            (23, "get_block_details", {"block_id": "minecraft:stone", "detail": "states", "limit": 0}),
            (24, "get_block_details", {"block_id": "minecraft:stone", "detail": "states", "offset": -1}),
            (25, "get_block_details", {"block_id": "minecraft:stone", "detail": "states", "offset": 1.5}),
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
        assert first["release_id"] != second["release_id"] == second_id
        page = call(send, 4, "get_block_details", {"block_id": "minecraft:stone", "detail": "states"})["structuredContent"]
        assert page["release_id"] == second_id


def test_unpublished_version_and_invalid_pointer_fail_closed(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    with node_session(tmp_path) as send:
        unpublished = call(send, 2, "index_info", {"minecraft_version": "99.9"})
        assert unpublished["isError"] and unpublished["structuredContent"]["error_code"] == "VERSION_NOT_AVAILABLE"
        assert unpublished["structuredContent"]["available_versions"] == ["26.2"]
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


def test_details_summary_budget_state_pages_and_lossless_card(tmp_path: Path) -> None:
    from blockpedia.r3 import encode_rgba_png

    fixture = build_fixture(tmp_path)
    # Patterned preview with partial and zero alpha (hidden RGB) to prove the
    # 256px card is the exact nearest resample of the untouched release PNG.
    pixels = bytearray()
    for y in range(512):
        for x in range(512):
            pixels += bytes([x % 256, y % 256, (x * y) % 256, 0 if (x // 16 + y // 16) % 5 == 0 else (x + y) % 256])
    preview_path = fixture.release / "previews/minecraft/stone/preview.png"
    preview_path.write_bytes(encode_rgba_png(512, 512, bytes(pixels)))
    block_id = "minecraft:stone"
    extra = [f"{block_id}[level={index}]" for index in range(19)]
    with sqlite3.connect(fixture.release / "index.sqlite3") as db:
        block = json.loads(db.execute("SELECT record_json FROM blocks WHERE block_id=?", (block_id,)).fetchone()[0])
        block["properties"] = {"level": [str(index) for index in range(19)]}
        db.execute("UPDATE blocks SET record_json=? WHERE block_id=?", (json.dumps(block), block_id))
        state = json.loads(db.execute("SELECT record_json FROM states WHERE state_id=?", (block_id,)).fetchone()[0])
        for index, state_id in enumerate(extra):
            state.update(state_id=state_id, is_default=False, properties={"level": str(index)}, variant_ids=[], mapping_status="skipped")
            db.execute("INSERT INTO states SELECT ?,block_id,?,0,? FROM states WHERE state_id=?", (state_id, json.dumps(state["properties"]), json.dumps(state), block_id))
    expected = sorted([block_id, *extra], key=lambda value: value.encode("utf-8"))
    before = _inventory(tmp_path)
    details_schema = Draft202012Validator(load_schema(SCHEMAS["get_block_details"]))
    with node_session(tmp_path) as send:
        result = call(send, 2, "get_block_details", {"block_id": block_id})
        summary = result["structuredContent"]
        details_schema.validate(summary)
        assert len(json.dumps(summary, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) < 8000
        assert summary["state_count"] == 20
        assert summary["properties"] == {"level": [str(index) for index in range(19)]}
        assert not {"states", "represented_state_ids", "state_behaviors", "variants"} & set(summary)
        assert summary["representative"]["state_id"] == block_id
        # Known support facts survive; unknown sides are omitted, never false.
        assert summary["representative"]["behavior"]["support"] == {"below": True}
        assert summary["representative"]["behavior"]["requires_support"] is False
        assert summary["semantics"]["summary_en"] == "Stone" and summary["semantics"]["building_roles"] == ["wall"]
        [card] = result_images(result)
        assert (card.width, card.height) == (CARD, CARD)
        assert card.pixels == nearest(decode_rgba_png(preview_path.read_bytes()))

        seen: list[str] = []
        offset: int | None = 0
        pages = []
        while offset is not None:
            response = call(send, 3 + len(pages), "get_block_details", {"block_id": block_id, "detail": "states", "offset": offset})
            page = response["structuredContent"]
            details_schema.validate(page)
            assert len(response["content"]) == 1 and "images" not in page
            assert (page["release_id"], page["total"], page["offset"]) == (fixture.release.name, 20, offset)
            pages.append(page)
            seen += [item["state_id"] for item in page["states"]]
            offset = page["next_offset"]
        assert [len(page["states"]) for page in pages] == [8, 8, 4]
        assert [page["next_offset"] for page in pages] == [8, 16, None]
        assert seen == expected
        level = next(item for page in pages for item in page["states"] if item["state_id"] == extra[1])
        assert (level["properties"], level["mapping_status"], level["is_default"]) == ({"level": "1"}, "skipped", False)
        wide = call(send, 10, "get_block_details", {"block_id": block_id, "detail": "states", "offset": 4, "limit": 16})["structuredContent"]
        assert [item["state_id"] for item in wide["states"]] == expected[4:20] and wide["next_offset"] is None
        beyond = call(send, 11, "get_block_details", {"block_id": block_id, "detail": "states", "offset": 25})["structuredContent"]
        assert (beyond["states"], beyond["next_offset"], beyond["total"]) == ([], None, 20)
    assert _inventory(tmp_path) == before


DYES = ("white", "light_gray", "gray", "black", "brown", "red", "orange", "yellow", "lime", "green", "cyan", "light_blue", "blue", "purple", "magenta", "pink")


@pytest.mark.parametrize("force_like", [False, True], ids=["fts5", "like"])
def test_color_series_merge(tmp_path: Path, force_like: bool) -> None:
    fixture = build_fixture(tmp_path, force_like=force_like)
    with sqlite3.connect(fixture.release / "index.sqlite3") as db:
        for dye in DYES:
            _add_search_block(db, f"minecraft:{dye}_wool", f"{dye}羊毛", f"{dye.replace('_', ' ').title()} Wool")
        # Four colours are not a 16-colour series and never merge.
        for dye in DYES[:4]:
            _add_search_block(db, f"minecraft:{dye}_tulip", f"{dye}郁金香", f"{dye.replace('_', ' ').title()} Tulip")
    schema = Draft202012Validator(load_schema(SCHEMAS["search_blocks"]))
    with node_session(tmp_path) as send:
        def search(keywords: list[str]) -> dict:
            result = call(send, 60, "search_blocks", {"keywords": keywords, "limit": 12})
            assert not result["isError"]
            schema.validate(result["structuredContent"])
            return result["structuredContent"]

        merged = search(["wool"])
        assert [item["block_id"] for item in merged["candidates"]] == ["minecraft:white_wool"]
        assert merged["candidates"][0]["color_series"] == {"block_id_pattern": "minecraft:{color}_wool", "other_colors": list(DYES[1:])}
        assert [tile["block_id"] for tile in merged["images"][0]["tiles"]] == ["minecraft:white_wool"]

        tulips = search(["tulip"])
        assert sorted(item["block_id"] for item in tulips["candidates"]) == sorted(f"minecraft:{dye}_tulip" for dye in DYES[:4])
        assert not any("color_series" in item for item in tulips["candidates"])

        # A colour in the query lists every colour separately.
        colored = search(["red", "wool"])
        assert len(colored["candidates"]) == 12
        assert not any("color_series" in item for item in colored["candidates"])
        # An exact block ID keeps its own entry.
        exact = search(["minecraft:red_wool"])
        assert exact["candidates"][0]["block_id"] == "minecraft:red_wool"
        assert "color_series" not in exact["candidates"][0]


# Face shading lighting.v2 bakes into the preview (RenderExporter.View order:
# isometric, front/north, side/east, top); palette.mjs divides it back out.
FACE_SHADES = ((0, 0, 0.6), (256, 0, 0.74), (0, 256, 0.497), (256, 256, 1.0))


def _shaded_preview(colors: list[tuple[int, int, int]], alpha: int = 255) -> bytes:
    """512px preview whose faces show `colors` as a 16px-texel checker, stored
    alpha-premultiplied like the exporter's translucent pixels."""
    from blockpedia.r3 import encode_rgba_png

    pixels = bytearray(512 * 512 * 4)
    for x0, y0, shade in FACE_SHADES:
        for y in range(256):
            for x in range(256):
                color = colors[(x // 16 + y // 16) % len(colors)]
                start = ((y0 + y) * 512 + x0 + x) * 4
                pixels[start:start + 4] = bytes([round(channel * shade * alpha / 255) for channel in color] + [alpha])
    return encode_rgba_png(512, 512, bytes(pixels))


def _set_preview(release: Path, db: sqlite3.Connection, block_id: str, colors: list[tuple[int, int, int]], alpha: int = 255) -> None:
    path = f"previews/minecraft/{block_id.removeprefix('minecraft:')}/preview.png"
    (release / path).parent.mkdir(parents=True, exist_ok=True)
    (release / path).write_bytes(_shaded_preview(colors, alpha))
    db.execute("UPDATE visual_variants SET preview_path=? WHERE variant_id=?", (path, block_id))


@pytest.mark.parametrize("force_like", [False, True], ids=["fts5", "like"])
def test_face_colours_family_compare_and_similar_to(tmp_path: Path, force_like: bool) -> None:
    fixture = build_fixture(tmp_path, force_like=force_like)
    gray, brown = (125, 125, 125), (160, 130, 80)
    with sqlite3.connect(fixture.release / "index.sqlite3") as db:
        _add_search_block(db, "minecraft:andesite", "安山岩", "Andesite")
        _add_search_block(db, "minecraft:cobblestone", "圆石", "Cobblestone")
        _add_search_block(db, "minecraft:oak_planks", "橡木木板", "Oak Planks")
        _add_search_block(db, "minecraft:infested_stone", "虫蚀石头", "Infested Stone")
        _add_search_block(db, "minecraft:stone_stairs", "石头楼梯", "Stone Stairs", tags=["minecraft:stairs"])
        _add_search_block(db, "minecraft:stone_slab", "石头台阶", "Stone Slab", tags=["minecraft:slabs"], visual=False)
        _add_search_block(db, "minecraft:structure_void", "结构空位", "Structure Void", visual=False)
        _add_search_block(db, "minecraft:white_stained_glass", "白色染色玻璃", "White Stained Glass", tags=["minecraft:impermeable"])
        _set_preview(fixture.release, db, "minecraft:white_stained_glass", [(255, 255, 255)], alpha=102)
        for block_id, colors in [("minecraft:stone", [gray]), ("minecraft:andesite", [(135, 135, 135)]), ("minecraft:cobblestone", [(100, 100, 100), (150, 150, 150)]),
                                 ("minecraft:oak_planks", [brown]), ("minecraft:infested_stone", [gray]), ("minecraft:stone_stairs", [gray])]:
            _set_preview(fixture.release, db, block_id, colors)
    search_schema = Draft202012Validator(load_schema(SCHEMAS["search_blocks"]))
    with node_session(tmp_path) as send:
        details = call(send, 2, "get_block_details", {"block_id": "minecraft:stone"})["structuredContent"]
        Draft202012Validator(load_schema(SCHEMAS["get_block_details"])).validate(details)
        colors = details["representative"]["colors"]
        # Top is unshaded; side divides the north/east shading back out.
        assert colors["top"] == {"hex": "#7d7d7d", "lightness": colors["top"]["lightness"], "lightness_std": 0, "dominant": [{"hex": "#7d7d7d", "share": 1}]}
        assert abs(int(colors["side"]["hex"][1:3], 16) - 125) <= 1
        assert details["representative"]["shape_class"] == "full_cube"
        assert details["family"] == {"base_block": "minecraft:stone", "forms": {"stairs": "minecraft:stone_stairs", "slab": "minecraft:stone_slab"}}
        stairs = call(send, 3, "get_block_details", {"block_id": "minecraft:stone_stairs"})["structuredContent"]
        assert stairs["representative"]["shape_class"] == "stairs" and stairs["family"] == details["family"]
        assert "family" not in call(send, 4, "get_block_details", {"block_id": "minecraft:glass"})["structuredContent"]
        # Premultiplied translucent pixels are divided back to texture colour.
        glass = call(send, 12, "get_block_details", {"block_id": "minecraft:white_stained_glass"})["structuredContent"]
        assert glass["representative"]["colors"]["top"]["hex"] == "#ffffff"

        compared = call(send, 5, "compare_blocks", {"block_ids": ["minecraft:stone", "minecraft:cobblestone", "minecraft:oak_planks", "minecraft:structure_void"]})
        data = compared["structuredContent"]
        Draft202012Validator(load_schema(SCHEMAS["compare_blocks"])).validate(data)
        assert [item.get("candidate_id") for item in data["blocks"]] == ["T01", "T02", "T03", None]
        cobble = data["blocks"][1]["colors"]["top"]
        assert sorted(item["hex"] for item in cobble["dominant"]) == ["#646464", "#969696"]
        assert [item["share"] for item in cobble["dominant"]] == [0.5, 0.5] and cobble["lightness_std"] > 9
        assert data["blocks"][3]["colors"] is None and data["blocks"][3]["semantics"] is None
        assert data["blocks"][0]["family"] == details["family"] and data["blocks"][1]["family"] is None
        assert {"colors", "family", "semantics"} <= set(data["differing_fields"]) and "shape_class" in data["differing_fields"]
        assert len(result_images(compared)) == 1

        def similar(id: int, arguments: dict) -> list[dict]:
            result = call(send, id, "search_blocks", arguments)
            assert not result["isError"], result
            search_schema.validate(result["structuredContent"])
            return result["structuredContent"]["candidates"]

        ranked = similar(6, {"similar_to": "minecraft:stone"})
        # Same shape class only (no stairs, glass or carpet); cobblestone shares
        # the mean grey but its texture spread puts andesite first; infested
        # stone is identical but keeps the ×0.25 recommendation penalty.  White
        # glass stored premultiplied (grey 102) would pass for stone; divided
        # back to white it is out of range.
        assert [item["block_id"] for item in ranked] == ["minecraft:andesite", "minecraft:cobblestone", "minecraft:oak_planks", "minecraft:infested_stone"]
        by_id = {item["block_id"]: item for item in ranked}
        assert by_id["minecraft:infested_stone"]["score"] == 0.25 and by_id["minecraft:infested_stone"]["color_delta_e"] == 0
        assert by_id["minecraft:cobblestone"]["color_delta_e"] < by_id["minecraft:andesite"]["color_delta_e"]
        assert all(item["score_breakdown"]["shape"] == 1 and "color_series" not in item for item in ranked)
        assert "minecraft:stone" in ranked[0]["reason"]
        assert [item["block_id"] for item in similar(7, {"similar_to": "minecraft:stone_stairs"})] == []
        assert [item["block_id"] for item in similar(8, {"similar_to": "minecraft:stone", "keywords": ["andesite"]})] == ["minecraft:andesite"]
        assert len(similar(9, {"similar_to": "minecraft:stone", "limit": 1})) == 1

        missing = call(send, 10, "search_blocks", {"similar_to": "minecraft:not_in_release"})
        assert missing["isError"] and missing["structuredContent"]["error_code"] == "BLOCK_NOT_FOUND"
        no_preview = call(send, 11, "search_blocks", {"similar_to": "minecraft:structure_void"})
        assert no_preview["isError"] and no_preview["structuredContent"] == {
            "error_code": "BLOCK_HAS_NO_PREVIEW", "message": "The similar_to block has no preview to compare colours with.", "invalid_block_ids": ["minecraft:structure_void"],
        }
        Draft202012Validator(load_schema("mcp-error.v2")).validate(no_preview["structuredContent"])


def _fixture_geometry(width: float, height: float, *, collision: bool = True) -> dict:
    boxes = [{"min_x": 0.5 - width / 2, "min_y": 0, "min_z": 0.5 - width / 2, "max_x": 0.5 + width / 2, "max_y": height, "max_z": 0.5 + width / 2}]
    return {
        "shape": {"boxes": boxes}, "collision": {"boxes": boxes if collision else []}, "width": width, "depth": width, "height": height,
        "is_full_cube": False, "is_horizontal_sheet": False, "geometry_classes": ["partial_height"],
    }


@pytest.mark.parametrize("force_like", [False, True], ids=["fts5", "like"])
def test_non_building_blocks_oxidation_series_and_material_groups(tmp_path: Path, force_like: bool) -> None:
    fixture = build_fixture(tmp_path, force_like=force_like)
    gray, amber = (125, 125, 125), (200, 150, 60)
    small = _fixture_geometry(0.375, 0.5625)
    with sqlite3.connect(fixture.release / "index.sqlite3") as db:
        _add_search_block(db, "minecraft:andesite", "安山岩", "Andesite")
        _add_search_block(db, "minecraft:copper_ore", "铜矿石", "Copper Ore", tags=["minecraft:copper_ores"])
        _add_search_block(db, "minecraft:white_shulker_box", "白色潜影盒", "White Shulker Box", tags=["minecraft:shulker_boxes"])
        for prefix, zh in [("", "切制铜块"), ("exposed_", "斑驳的切制铜块"), ("waxed_", "涂蜡切制铜块"), ("waxed_exposed_", "涂蜡斑驳的切制铜块")]:
            _add_search_block(db, f"minecraft:{prefix}cut_copper", zh, f"{prefix.replace('_', ' ').title()}Cut Copper")
        for block_id, zh, en in [("oak_log", "橡木原木", "Oak Log"), ("stripped_oak_log", "去皮橡木原木", "Stripped Oak Log"), ("oak_wood", "橡木", "Oak Wood"), ("oak_planks", "橡木木板", "Oak Planks")]:
            _add_search_block(db, f"minecraft:{block_id}", zh, en, tags=["minecraft:logs"] if block_id != "oak_planks" else ["minecraft:planks"])
        _add_search_block(db, "minecraft:oak_stairs", "橡木楼梯", "Oak Stairs", tags=["minecraft:stairs"])
        _add_search_block(db, "minecraft:lantern", "灯笼", "Lantern", tags=["minecraft:lanterns"], geometry=small)
        _add_search_block(db, "minecraft:soul_lantern", "灵魂灯笼", "Soul Lantern", tags=["minecraft:lanterns"], geometry=small)
        _add_search_block(db, "minecraft:potted_poppy", "虞美人盆栽", "Potted Poppy", tags=["minecraft:flower_pots"], geometry=_fixture_geometry(0.375, 0.375))
        _add_search_block(db, "minecraft:poppy", "虞美人", "Poppy", tags=["minecraft:small_flowers"], geometry=_fixture_geometry(0.375, 0.625, collision=False))
        _add_search_block(db, "minecraft:chest", "箱子", "Chest", geometry=_fixture_geometry(0.875, 0.875))
        for block_id, colors in [("minecraft:stone", [gray]), ("minecraft:andesite", [(135, 135, 135)]), ("minecraft:copper_ore", [gray]), ("minecraft:white_shulker_box", [gray]),
                                 ("minecraft:lantern", [amber]), ("minecraft:soul_lantern", [(190, 150, 70)]), ("minecraft:potted_poppy", [amber]), ("minecraft:poppy", [amber])]:
            _set_preview(fixture.release, db, block_id, colors)
    search_schema = Draft202012Validator(load_schema(SCHEMAS["search_blocks"]))
    details_schema = Draft202012Validator(load_schema(SCHEMAS["get_block_details"]))
    with node_session(tmp_path) as send:
        def search(id: int, arguments: dict) -> list[dict]:
            result = call(send, id, "search_blocks", {"limit": 12, **arguments})
            assert not result["isError"], result
            search_schema.validate(result["structuredContent"])
            return result["structuredContent"]["candidates"]

        def details(id: int, block_id: str) -> dict:
            result = call(send, id, "get_block_details", {"block_id": block_id})["structuredContent"]
            details_schema.validate(result)
            return result

        # An ore and a shulker box as grey as stone rank ×0.5 below andesite.
        ranked = {item["block_id"]: item for item in search(2, {"similar_to": "minecraft:stone"})}
        ids = list(ranked)
        assert ids.index("minecraft:andesite") < ids.index("minecraft:copper_ore") and ids.index("minecraft:andesite") < ids.index("minecraft:white_shulker_box")
        assert ranked["minecraft:copper_ore"]["score"] == pytest.approx(0.5, abs=1e-6)
        assert "ore ×0.5 (not a building material)" in ranked["minecraft:copper_ore"]["reason"]
        assert ranked["minecraft:white_shulker_box"]["score"] == pytest.approx(0.5, abs=1e-6)
        # The same holds for a keyword every block matches only by its role,
        # but "ore" names an ore by the head of its name while "copper" does not.
        wall = {item["block_id"]: item["score"] for item in search(3, {"keywords": ["wall"]})}
        assert max(wall.values()) > 0 and wall.get("minecraft:copper_ore", 0) <= 0.5 * max(wall.values()) + 1e-6
        ore = search(4, {"keywords": ["ore"]})[0]
        assert ore["block_id"] == "minecraft:copper_ore" and ore["score"] > 0.8 and "×0.5" not in ore["reason"]
        assert search(5, {"keywords": ["copper ore"]})[0]["block_id"] == "minecraft:copper_ore"
        copper_ore = next(item for item in search(6, {"keywords": ["copper"]}) if item["block_id"] == "minecraft:copper_ore")
        assert "ore ×0.5" in copper_ore["reason"]

        # Lantern: same small-fixture class; the potted plant ranks ×0.5 and
        # the poppy (no collision) is another class.
        assert details(7, "minecraft:lantern")["representative"]["shape_class"] == "small_fixture"
        assert details(8, "minecraft:poppy")["representative"]["shape_class"] == "passable"
        assert details(9, "minecraft:chest")["representative"]["shape_class"] == "partial_block"
        lantern = search(10, {"similar_to": "minecraft:lantern"})
        assert [item["block_id"] for item in lantern] == ["minecraft:soul_lantern", "minecraft:potted_poppy"]
        assert lantern[1]["score"] == pytest.approx(0.5, abs=1e-6)
        # Same kind as the reference: no demotion among plants.
        assert search(11, {"similar_to": "minecraft:potted_poppy"})[0]["score"] > 0.9

        # Oxidation stages and waxed versions fold into the unwaxed block.
        folded = [item for item in search(12, {"keywords": ["copper"]}) if "cut_copper" in item["block_id"]]
        assert [item["block_id"] for item in folded] == ["minecraft:cut_copper"]
        assert folded[0]["oxidation_series"] == {"other_block_ids": ["minecraft:exposed_cut_copper", "minecraft:waxed_cut_copper", "minecraft:waxed_exposed_cut_copper"]}
        # With a colour only the waxed twin folds; an exact ID keeps its entry.
        colored = {item["block_id"]: item for item in search(13, {"keywords": ["green", "copper"]}) if "cut_copper" in item["block_id"]}
        assert {key: value["oxidation_series"]["other_block_ids"] for key, value in colored.items()} == {
            "minecraft:cut_copper": ["minecraft:waxed_cut_copper"],
            "minecraft:exposed_cut_copper": ["minecraft:waxed_exposed_cut_copper"],
        }
        exact = search(14, {"keywords": ["minecraft:waxed_cut_copper"]})
        assert exact[0]["block_id"] == "minecraft:waxed_cut_copper" and "oxidation_series" not in exact[0]

        # Log, wood, stripped log and planks are one material.
        oak = ["minecraft:oak_log", "minecraft:oak_planks", "minecraft:oak_wood", "minecraft:stripped_oak_log"]
        assert details(15, "minecraft:stripped_oak_log")["family"] == {"base_block": "minecraft:stripped_oak_log", "forms": {}, "material_blocks": oak}
        assert details(16, "minecraft:oak_stairs")["family"] == {"base_block": "minecraft:oak_planks", "forms": {"stairs": "minecraft:oak_stairs"}, "material_blocks": oak}
        # Wax and oxidation stay part of the material: cut copper has no copper block here.
        assert "family" not in details(17, "minecraft:cut_copper")
        compared = call(send, 18, "compare_blocks", {"block_ids": ["minecraft:oak_log", "minecraft:oak_wood"]})["structuredContent"]
        Draft202012Validator(load_schema(SCHEMAS["compare_blocks"])).validate(compared)
        assert [item["family"]["material_blocks"] for item in compared["blocks"]] == [oak, oak]
