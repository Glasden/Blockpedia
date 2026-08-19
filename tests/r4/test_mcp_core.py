from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from blockpedia import mcp_release
from blockpedia.mcp_query import MCPInputError, MCPQueryService, MCPToolResult, _keyword_intent, deterministic_score
from blockpedia.mcp_release import MCPReleaseError, MCPReleaseResolver, ReleaseHandle
from blockpedia.schema import validate_record

from .fixture_builder import build_fixture


def _write_json(path: Path, value: object) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))


def _inventory(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


def test_four_tools_are_schema_valid_read_only_and_search_is_local(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    before = _inventory(tmp_path)
    service = MCPQueryService(tmp_path)
    info = service.index_info()
    search = service.search_blocks({"keywords": [" yellow ", "carpet"]})
    details = service.get_block_details({"block_id": "minecraft:stone"})
    compare = service.compare_blocks({"block_ids": ["minecraft:stone", "minecraft:glass"]})
    for schema_id, result in (
        ("mcp-index-info-output.v1", info),
        ("mcp-search-blocks-output.v1", search),
        ("mcp-block-details-output.v1", details),
        ("mcp-compare-blocks-output.v1", compare),
    ):
        validate_record(schema_id, result)
        assert isinstance(result, MCPToolResult)
        for index, payload in enumerate(result.images):
            image = result["data"]["images"][index]
            assert image["content_index"] == index + 1
            assert image["sha256"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert info["minecraft_version"] == "26.2"
    assert search["data"]["query"] == "yellow carpet"
    assert search["data"]["hard_filters"] == []
    assert search["data"]["reranked_by_llm"] is False
    assert search["data"]["candidates"]
    assert all(item["score_source"] == "local" and item["final_score"] == item["local_score"] for item in search["data"]["candidates"])
    assert details["data"]["variants"][0]["variant_id"] == "minecraft:stone"
    assert compare["data"]["contact_sheet"]["tile_mapping"][0]["candidate_id"] == "T01"
    assert _inventory(tmp_path) == before
    assert not list(tmp_path.rglob("*.sqlite3-wal"))
    assert not list(tmp_path.rglob("*.sqlite3-shm"))
    assert fixture.current.read_bytes() == (tmp_path / "current.json").read_bytes()


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"keywords": []},
        {"keywords": [" "]},
        {"keywords": ["stone", " stone "]},
        {"keywords": ["x"] * 17},
        {"keywords": ["x" * 65]},
        {"keywords": ["stone"], "query": "stone"},
        {"keywords": ["stone"], "context": {}},
        {"keywords": ["stone"], "query_spec": {}},
    ],
)
def test_search_keywords_input_rejects_old_fields_and_bad_bounds(tmp_path: Path, arguments: dict[str, object]) -> None:
    build_fixture(tmp_path)
    with pytest.raises(MCPInputError):
        MCPQueryService(tmp_path).search_blocks(arguments)


def test_search_keywords_item_and_array_limits_are_validated(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    service = MCPQueryService(tmp_path)
    assert not service.search_blocks({"keywords": ["x"], "limit": 12}).is_error
    with pytest.raises(MCPInputError):
        service.search_blocks({"keywords": ["x"], "limit": 0})
    with pytest.raises(MCPInputError):
        service.search_blocks({"keywords": ["x"], "limit": 13})


def test_keyword_intent_never_creates_hard_constraints() -> None:
    intent = _keyword_intent(["Must not redstone", "透明"])
    assert intent.keywords == ("must", "not", "redstone", "透明")
    assert intent.soft["keywords"] == intent.keywords
    assert not hasattr(intent, "hard")


def test_empty_keyword_recall_is_a_normal_success(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    result = MCPQueryService(tmp_path).search_blocks({"keywords": ["term-not-present"]})
    assert not result.is_error
    assert result["data"]["candidates"] == []
    assert result["data"]["hard_filters"] == []
    assert result["data"]["reranked_by_llm"] is False
    assert result.images == ()


def test_short_unicode_trigram_uses_search_fts_like_not_search_text(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixture(tmp_path)
    original = ReleaseHandle.execute
    statements: list[str] = []

    def execute(handle: ReleaseHandle, statement: str, parameters: tuple[object, ...] = ()):
        statements.append(statement)
        if "search_text" in statement:
            raise AssertionError("trigram short-token recall must not access search_text")
        return original(handle, statement, parameters)

    monkeypatch.setattr(ReleaseHandle, "execute", execute)
    result = MCPQueryService(tmp_path).search_blocks({"keywords": ["石"]})
    assert not result.is_error
    assert any("search_fts" in statement and "LIKE" in statement for statement in statements)


def test_normalized_like_recall_uses_search_text(tmp_path: Path) -> None:
    build_fixture(tmp_path, force_like=True)
    result = MCPQueryService(tmp_path).search_blocks({"keywords": ["stone"]})
    assert not result.is_error
    assert result["data"]["candidates"]


def test_snapshot_and_preview_caches_are_retained_without_provider(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixture(tmp_path)
    service = MCPQueryService(tmp_path)
    with service.resolver.resolve() as handle:
        first = service._snapshot(handle)
        second = service._snapshot(handle)
    assert first is second
    calls: list[str] = []
    original = ReleaseHandle.read_image

    def read_image(handle: ReleaseHandle, relative_ref: str):
        calls.append(relative_ref)
        return original(handle, relative_ref)

    monkeypatch.setattr(ReleaseHandle, "read_image", read_image)
    result = service.search_blocks({"keywords": ["stone"]})
    assert not result.is_error
    assert len(calls) == len(set(calls))


def test_pointer_switch_loads_a_new_snapshot(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    second_id = "rel_" + "2" * 32
    second_release = fixture.release.parent / second_id
    shutil.copytree(fixture.release, second_release)
    for name in ("release.json", "manifest.json"):
        value = json.loads((second_release / name).read_text(encoding="utf-8"))
        value["release_id"] = second_id
        _write_json(second_release / name, value)
    service = MCPQueryService(tmp_path)
    first = service.index_info()
    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    current["versions"]["26.2"].update({"release_id": second_id, "relative_path": f"releases/26.2/{second_id}"})
    _write_json(tmp_path / "current.json", current)
    second = service.index_info()
    assert first["resolved_release_id"] != second["resolved_release_id"]
    assert len(service._snapshots) == 2


def test_path_reparse_and_index_open_failures_remain_fail_closed(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    current["versions"]["26.2"]["relative_path"] = "releases/26.2/../escape"
    _write_json(tmp_path / "current.json", current)
    path_result = MCPQueryService(tmp_path).index_info()
    assert path_result.is_error and path_result["error_code"] == "CURRENT_POINTER_INVALID"

    class _ReparsePath:
        def lstat(self) -> object:
            return type("Stat", (), {"st_mode": 0o100000, "st_file_attributes": 0x400})()

    with pytest.raises(MCPReleaseError) as error:
        mcp_release._lstat(_ReparsePath(), directory=False)  # type: ignore[arg-type]
    assert error.value.code == "CURRENT_POINTER_INVALID"

    missing_root = tmp_path / "missing"
    missing = build_fixture(missing_root)
    release_id = "rel_" + "3" * 32
    release = missing.release.parent / release_id
    shutil.copytree(missing.release, release)
    (release / "index.sqlite3").unlink()
    pointer = json.loads((missing_root / "current.json").read_text(encoding="utf-8"))
    pointer["versions"]["26.2"].update({"release_id": release_id, "relative_path": f"releases/26.2/{release_id}"})
    _write_json(missing_root / "current.json", pointer)
    open_result = MCPQueryService(missing_root).index_info()
    assert open_result.is_error and open_result["error_code"] == "INDEX_OPEN_FAILED"


def test_deterministic_score_and_local_candidate_order_are_stable(tmp_path: Path) -> None:
    score, breakdown = deterministic_score({"shape": 1.0, "color": 0.0})
    assert score == round(0.35 / (0.35 + 0.30), 8)
    assert breakdown["shape"] == 1.0
    build_fixture(tmp_path)
    first = MCPQueryService(tmp_path).search_blocks({"keywords": ["stone"]})
    second = MCPQueryService(tmp_path).search_blocks({"keywords": ["stone"]})
    assert first["data"]["candidates"] == second["data"]["candidates"]
