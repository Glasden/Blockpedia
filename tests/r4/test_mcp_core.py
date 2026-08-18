from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from blockpedia.mcp_query import MCPInputError, MCPQueryService, MCPToolResult, _Intent, deterministic_score, parse_query
from blockpedia.mcp_release import MCPReleaseError, MCPReleaseResolver
from blockpedia.schema import validate_record

from .fixture_builder import build_fixture


def _write_json(path: Path, value: object) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))


def _rehash_fixture(fixture: object, *, index_changed: bool) -> None:
    release = fixture.release  # type: ignore[union-attr]
    root = fixture.root  # type: ignore[union-attr]
    manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
    manifest["quality_report_sha256"] = "sha256:" + hashlib.sha256((release / "quality_report.json").read_bytes()).hexdigest()
    if index_changed:
        manifest["functional_artifacts"]["index.sqlite3"] = "sha256:" + hashlib.sha256((release / "index.sqlite3").read_bytes()).hexdigest()
    _write_json(release / "manifest.json", manifest)
    release_json = json.loads((release / "release.json").read_text(encoding="utf-8"))
    release_json["manifest_sha256"] = "sha256:" + hashlib.sha256((release / "manifest.json").read_bytes()).hexdigest()
    _write_json(release / "release.json", release_json)
    files = sorted(path.relative_to(release).as_posix() for path in release.rglob("*") if path.is_file() and path.name != "checksums.sha256")
    (release / "checksums.sha256").write_bytes("".join(f"{hashlib.sha256((release / relative).read_bytes()).hexdigest()}  {relative}\n" for relative in files).encode("ascii"))
    pointer = json.loads((root / "current.json").read_text(encoding="utf-8"))
    pointer["versions"]["26.2"]["manifest_sha256"] = release_json["manifest_sha256"]
    _write_json(root / "current.json", pointer)


def _inventory(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.rglob("*") if path.is_file()}


def test_four_tools_are_schema_valid_read_only_and_images_are_recomputed(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    before = _inventory(tmp_path)
    service = MCPQueryService(tmp_path)

    info = service.index_info()
    search = service.search_blocks({"query": "yellow carpet", "context": {"rerank": "local_only"}})
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
    assert search["data"]["candidates"][0]["block_id"] == "minecraft:yellow_carpet"
    assert details["data"]["variants"][0]["variant_id"] == "minecraft:stone"
    assert compare["data"]["contact_sheet"]["tile_mapping"][0]["candidate_id"] == "T01"
    assert _inventory(tmp_path) == before
    assert not list(tmp_path.rglob("*.sqlite3-wal"))
    assert not list(tmp_path.rglob("*.sqlite3-shm"))
    assert fixture.current.read_bytes() == (tmp_path / "current.json").read_bytes()


def test_current_default_explicit_unknown_and_malformed_version_boundaries(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    service = MCPQueryService(tmp_path)
    assert service.index_info()["minecraft_version"] == "26.2"
    assert service.index_info({"minecraft_version": "26.2"})["minecraft_version"] == "26.2"
    unknown = service.index_info({"minecraft_version": "26.3"})
    assert unknown.is_error and unknown["error_code"] == "VERSION_NOT_AVAILABLE"
    with pytest.raises(MCPInputError):
        service.index_info({"minecraft_version": "26"})
    with pytest.raises(MCPInputError):
        service.index_info({"release_id": "rel_11111111111111111111111111111111"})
    with pytest.raises(MCPInputError):
        service.get_block_details({"block_id": "stone"})
    with pytest.raises(MCPInputError):
        service.compare_blocks({"block_ids": ["minecraft:stone"]})
    missing = service.get_block_details({"block_id": "minecraft:not_in_release"})
    assert missing.is_error and missing["error_code"] == "BLOCK_NOT_FOUND"


def test_v1_index_hash_quality_and_sidecar_fail_closed(tmp_path: Path) -> None:
    v1 = tmp_path / "v1"
    build_fixture(v1, index_version=1)
    result = MCPQueryService(v1).index_info()
    assert result.is_error and result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"

    corrupt = tmp_path / "corrupt"
    build_fixture(corrupt)
    quality = corrupt / "releases" / "26.2" / "rel_11111111111111111111111111111111" / "quality_report.json"
    quality.write_bytes(quality.read_bytes() + b"x")
    result = MCPQueryService(corrupt).index_info()
    assert result.is_error and result["error_code"] == "RELEASE_INTEGRITY_FAILED"

    sidecar = tmp_path / "sidecar"
    build_fixture(sidecar)
    index = sidecar / "releases" / "26.2" / "rel_11111111111111111111111111111111" / "index.sqlite3-wal"
    index.write_bytes(b"sidecar")
    result = MCPQueryService(sidecar).index_info()
    assert result.is_error and result["error_code"] == "RELEASE_INTEGRITY_FAILED"


def test_like_branch_empty_success_and_unknown_hard_fact(tmp_path: Path) -> None:
    build_fixture(tmp_path, force_like=True)
    service = MCPQueryService(tmp_path)
    info = service.index_info()
    assert not info.is_error
    empty = service.search_blocks({"query": "term-not-present", "context": {"rerank": "local_only"}})
    assert not empty.is_error and empty["data"]["candidates"] == [] and empty.images == ()

    unknown_row = (
        "minecraft:unknown",
        {"machine_facts": {"behavior_by_state": {"minecraft:unknown": {"transparent": "unknown"}}, "geometry": {"geometry_classes": []}, "machine_tags": []}, "canonical_state_id": "minecraft:unknown"},
        {"behavior": {"transparent": "unknown"}},
        {},
    )
    assert not service._passes_hard(unknown_row, [{"field": "behavior.transparent", "operator": "equals", "value": True}])


def test_exact_weight_normalization_and_stable_order() -> None:
    score, breakdown = deterministic_score({"shape": 1.0, "color": 0.0})
    expected = 0.35 / (0.35 + 0.30)
    assert score == round(expected, 8)
    assert breakdown == {"shape": 1.0, "color": 0.0, "use": 0.0, "name_synonym": 0.0, "style": 0.0, "behavior": 0.0}
    score_again, _ = deterministic_score({"color": 0.0, "shape": 1.0})
    assert score_again == score


class _Reranker:
    @staticmethod
    def _query_spec() -> dict[str, object]:
        empty = lambda: []
        return {
            "schema_id": "query-spec-output.v1",
            "source": "llm",
            "hard": {
                "minecraft_version": {"value": "26.2", "source": "request", "required": True},
                "release_status": {"value": "current", "source": "system", "required": True},
                "legal_state": {"value": True, "source": "system", "required": True},
                "behaviors": empty(), "support": empty(), "transparency": empty(), "emission": empty(), "orientation": empty(), "shape": empty(),
            },
            "soft": {key: empty() for key in ("colors", "materials", "uses", "styles", "shape_terms", "avoid_for", "keywords")},
            "ambiguities": [], "needs_user_choice": False, "suggested_followups": [], "unknown_terms": [],
        }

    def query_spec(self, _query: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        return self._query_spec()

    def visual_rerank(self, _query: str, **kwargs: object) -> dict[str, object]:
        candidate_ids = list((kwargs["candidate_records"] or {}).keys())  # type: ignore[union-attr]
        return {"schema_id": "rerank-output.v1", "ranking": [{"candidate_id": candidate_id, "fit": 0.8, "reason": "fixture"} for candidate_id in reversed(candidate_ids)], "needs_user_choice": False, "ambiguity_points": [], "suggested_followups": []}


class _BadReranker:
    def query_spec(self, _query: str, **kwargs: object) -> dict[str, object]:
        return _Reranker._query_spec()

    def visual_rerank(self, _query: str, **kwargs: object) -> dict[str, object]:
        del kwargs
        return {"schema_id": "rerank-output.v1", "ranking": [{"candidate_id": "NOT_A_RELEASE_ID", "fit": 1.0, "reason": "bad"}], "needs_user_choice": False, "ambiguity_points": [], "suggested_followups": []}


def test_provider_injection_auto_required_and_id_mismatch_fallback(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    reranked = MCPQueryService(tmp_path, provider=_Reranker()).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not reranked.is_error and reranked["data"]["reranked_by_llm"] is True
    required = MCPQueryService(tmp_path).search_blocks({"query": "stone", "context": {"rerank": "required"}})
    assert required.is_error and required["error_code"] == "RERANK_REQUIRED_UNAVAILABLE"
    assert required["details"]["provider_error_code"] in {"PROVIDER_NOT_CONFIGURED", "PROVIDER_CONFIG_INVALID"}
    mismatch = MCPQueryService(tmp_path, provider=_BadReranker()).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not mismatch.is_error and mismatch["data"]["reranked_by_llm"] is False


def test_provider_query_spec_precedes_visual_rerank_and_family_is_strict_noop(tmp_path: Path) -> None:
    build_fixture(tmp_path)

    class _Ordered(_Reranker):
        def __init__(self) -> None:
            self.calls: list[str] = []

        def query_spec(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("query_spec")
            return super().query_spec(_query, **kwargs)

        def visual_rerank(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("visual_rerank")
            return super().visual_rerank(_query, **kwargs)

    provider = _Ordered()
    result = MCPQueryService(tmp_path, provider=provider).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not result.is_error
    assert provider.calls == ["query_spec", "visual_rerank"]

    family = MCPQueryService(tmp_path).search_blocks({"query": "stone", "context": {"family": "unknown", "rerank": "local_only"}})
    assert family.is_error and family["error_code"] == "QUERY_INVALID"


def test_provider_query_spec_hard_constraint_filters_before_rerank(tmp_path: Path) -> None:
    build_fixture(tmp_path)

    class _ConstraintProvider(_Reranker):
        def __init__(self) -> None:
            self.received: list[str] = []

        def query_spec(self, _query: str, **kwargs: object) -> dict[str, object]:
            spec = super().query_spec(_query, **kwargs)
            hard = spec["hard"]  # type: ignore[index]
            assert isinstance(hard, dict)
            hard["behaviors"] = [{"field": "transparent", "operator": "eq", "value": True, "source": "system", "required": True}]
            return spec

        def visual_rerank(self, _query: str, **kwargs: object) -> dict[str, object]:
            records = kwargs["candidate_records"]
            assert isinstance(records, dict)
            self.received = list(records)
            return super().visual_rerank(_query, **kwargs)

    provider = _ConstraintProvider()
    result = MCPQueryService(tmp_path, provider=provider).search_blocks({"query": "wall", "context": {"rerank": "auto"}})
    assert not result.is_error
    assert provider.received == ["T01"]
    assert result["data"]["candidates"][0]["candidate_id"] == "T01"
    assert result["data"]["candidates"][0]["block_id"] == "minecraft:glass"


def test_validated_lab_oklab_features_affect_deterministic_weighted_ranking(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    service = MCPQueryService(tmp_path)
    parsed = parse_query("yellow")
    intent = _Intent(tuple(parsed["hard"]), parsed["soft"], tuple(parsed["unknown_terms"]), tuple(parsed["unsupported"]))
    with service.resolver.resolve() as handle:
        snapshot = service._snapshot(handle)
        for annotation in snapshot.annotations.values():
            annotation["color_terms"] = ["yellow"]
        ranked = service._rank_rows(service._eligible_rows(snapshot), snapshot, intent)
    by_id = {item[0][0]: item for item in ranked}
    assert ranked[0][0][0] == "minecraft:yellow_carpet"
    assert by_id["minecraft:yellow_carpet"][2]["color"] > by_id["minecraft:stone"][2]["color"]


def test_audit_ids_require_explicit_targets() -> None:
    assert MCPQueryService._audit_ids({"skip_reviews": [{"review_id": "missing-target"}]}, "skip_reviews", "minecraft:stone") == []


def _remove_glass_variant(fixture: object) -> None:
    index = sqlite3.connect(fixture.release / "index.sqlite3")  # type: ignore[union-attr]
    try:
        state_row = index.execute("SELECT record_json FROM states WHERE state_id='minecraft:glass'").fetchone()
        state = json.loads(state_row[0])
        state["variant_ids"] = []
        state["mapping_status"] = "skipped"
        state["failure_id"] = "failure_glass"
        index.execute(
            "UPDATE states SET record_json=? WHERE state_id='minecraft:glass'",
            (json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")),),
        )
        index.execute("DELETE FROM annotations WHERE variant_id='minecraft:glass'")
        index.execute("DELETE FROM visual_variants WHERE variant_id='minecraft:glass'")
        index.execute("DELETE FROM search_fts WHERE variant_id='minecraft:glass'")
        index.commit()
    finally:
        index.close()
    for relative in (
        "previews/minecraft/glass/preview.png",
        "previews/minecraft/glass/mask.png",
        "previews/minecraft/glass/render.json",
    ):
        (fixture.release / relative).unlink()  # type: ignore[union-attr]


def test_audited_visual_variant_skip_without_published_variant_resolves(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    manual = json.loads((fixture.release / "manual-overrides.json").read_text(encoding="utf-8"))
    manual["skip_reviews"] = [{
        "schema_version": "skip-review.v1",
        "review_id": "skip-glass",
        "target_type": "visual_variant",
        "target_id": "minecraft:glass",
        "minecraft_version": "26.2",
        "reviewer": "fixture",
        "reviewed_at": "2026-08-18T12:00:00Z",
        "reason_code": "MISSING_TEXTURE",
        "note": "Fixture block has no publishable visual variant.",
        "evidence": ["index.sqlite3"],
        "source_version": "fixture.v1",
        "machine_failure_ref": "failure_glass",
    }]
    _write_json(fixture.release / "manual-overrides.json", manual)
    _remove_glass_variant(fixture)
    _rehash_fixture(fixture, index_changed=True)

    result = MCPQueryService(tmp_path).index_info()
    assert not result.is_error


def test_no_published_variant_without_exact_audited_skip_is_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    _remove_glass_variant(fixture)
    _rehash_fixture(fixture, index_changed=True)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


def test_orphan_visual_variant_skip_target_remains_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    manual = json.loads((fixture.release / "manual-overrides.json").read_text(encoding="utf-8"))
    manual["skip_reviews"] = [{
        "schema_version": "skip-review.v1",
        "review_id": "skip-missing",
        "target_type": "visual_variant",
        "target_id": "minecraft:missing",
        "minecraft_version": "26.2",
        "reviewer": "fixture",
        "reviewed_at": "2026-08-18T12:00:00Z",
        "reason_code": "MISSING_TEXTURE",
        "note": "Fixture orphan target.",
        "evidence": ["index.sqlite3"],
        "source_version": "fixture.v1",
        "machine_failure_ref": "failure_missing",
    }]
    _write_json(fixture.release / "manual-overrides.json", manual)
    _rehash_fixture(fixture, index_changed=False)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


def test_verified_handle_rejects_file_replacement_after_resolution(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    resolver = MCPReleaseResolver(tmp_path)
    with resolver.resolve() as handle:
        manual = fixture.release / "manual-overrides.json"
        manual.write_bytes(manual.read_bytes() + b" ")
        with pytest.raises(MCPReleaseError) as error:
            handle.read_bytes("manual-overrides.json")
        assert error.value.code == "RELEASE_INTEGRITY_FAILED"


def test_deferred_db_reads_fail_closed_after_index_mutation(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    resolver = MCPReleaseResolver(tmp_path)
    index = tmp_path / "releases" / "26.2" / "rel_11111111111111111111111111111111" / "index.sqlite3"
    with resolver.resolve() as handle:
        payload = bytearray(index.read_bytes())
        payload[-1] ^= 1
        index.write_bytes(payload)
        with pytest.raises(MCPReleaseError) as error:
            handle.execute("SELECT block_id FROM blocks")
        assert error.value.code == "RELEASE_INTEGRITY_FAILED"


def test_rehashed_quality_report_is_still_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    quality = json.loads((fixture.release / "quality_report.json").read_text(encoding="utf-8"))
    quality["unexpected"] = True
    _write_json(fixture.release / "quality_report.json", quality)
    _rehash_fixture(fixture, index_changed=False)
    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error and result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "quality_report"


@pytest.mark.parametrize("mutation", ["missing_machine_schema", "failed_ai_schema"])
def test_rehashed_quality_schema_evidence_is_required_and_passed(tmp_path: Path, mutation: str) -> None:
    fixture = build_fixture(tmp_path)
    quality = json.loads((fixture.release / "quality_report.json").read_text(encoding="utf-8"))
    if mutation == "missing_machine_schema":
        quality["items"] = [item for item in quality["items"] if item["code"] != "MACHINE_SCHEMA_VALID"]
    else:
        next(item for item in quality["items"] if item["code"] == "AI_SCHEMA_VALID")["status"] = "failed"
    _write_json(fixture.release / "quality_report.json", quality)
    _rehash_fixture(fixture, index_changed=False)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "quality_report"


def test_schema_inventory_requires_record_schema_entries(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    inventory = fixture.release / "schemas.sha256"
    lines = inventory.read_text(encoding="ascii").splitlines(keepends=True)
    inventory.write_text("".join(line for line in lines if "  block-record.v1  " not in line), encoding="ascii")
    _rehash_fixture(fixture, index_changed=False)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "manifest"


@pytest.mark.parametrize("projection", ["block", "state", "variant"])
def test_rehashed_canonical_row_record_mismatch_is_rejected(tmp_path: Path, projection: str) -> None:
    fixture = build_fixture(tmp_path)
    index = sqlite3.connect(fixture.release / "index.sqlite3")
    try:
        if projection == "block":
            index.execute("UPDATE blocks SET translation_key='corrupt' WHERE block_id='minecraft:stone'")
        elif projection == "state":
            index.execute("UPDATE states SET properties_json=? WHERE state_id='minecraft:stone'", (json.dumps({"tampered": True}, separators=(",", ":")),))
        else:
            index.execute("UPDATE visual_variants SET candidate_qualification='excluded' WHERE variant_id='minecraft:stone'")
        index.commit()
    finally:
        index.close()
    _rehash_fixture(fixture, index_changed=True)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


@pytest.mark.parametrize("mutation", ["missing_key", "root_container", "nested_container"])
def test_removed_record_schema_shapes_fail_as_release_integrity(tmp_path: Path, mutation: str) -> None:
    fixture = build_fixture(tmp_path)
    index = sqlite3.connect(fixture.release / "index.sqlite3")
    try:
        if mutation == "missing_key":
            row = index.execute("SELECT record_json FROM blocks WHERE block_id='minecraft:stone'").fetchone()
            record = json.loads(row[0])
            del record["default_state_id"]
            index.execute("UPDATE blocks SET record_json=? WHERE block_id='minecraft:stone'", (json.dumps(record, sort_keys=True, separators=(",", ":")),))
        elif mutation == "root_container":
            index.execute("UPDATE states SET record_json='[]' WHERE state_id='minecraft:stone'")
        else:
            row = index.execute("SELECT record_json FROM visual_variants WHERE variant_id='minecraft:stone'").fetchone()
            record = json.loads(row[0])
            record["machine_facts"] = []
            index.execute("UPDATE visual_variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(record, sort_keys=True, separators=(",", ":")),))
        index.commit()
    finally:
        index.close()
    _rehash_fixture(fixture, index_changed=True)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


@pytest.mark.parametrize("mode", ["zero", "multiple"])
def test_default_state_cardinality_is_rejected_after_grouping(tmp_path: Path, mode: str) -> None:
    fixture = build_fixture(tmp_path)
    index = sqlite3.connect(fixture.release / "index.sqlite3")
    try:
        row = index.execute("SELECT state_id,block_id,record_json FROM states WHERE state_id='minecraft:stone'").fetchone()
        state = json.loads(row[2])
        if mode == "zero":
            state["is_default"] = False
            index.execute("UPDATE states SET is_default=0,record_json=? WHERE state_id='minecraft:stone'", (json.dumps(state, sort_keys=True, separators=(",", ":")),))
        else:
            state["state_id"] = "minecraft:stone_extra"
            index.execute(
                "INSERT INTO states(state_id,block_id,properties_json,is_default,record_json) VALUES (?,?,?,?,?)",
                (state["state_id"], state["block_id"], "{}", 1, json.dumps(state, sort_keys=True, separators=(",", ":"))),
            )
        index.commit()
    finally:
        index.close()
    _rehash_fixture(fixture, index_changed=True)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


@pytest.mark.parametrize("mutation", ["missing_state", "cross_block"])
def test_missing_or_cross_block_variant_references_are_rejected(tmp_path: Path, mutation: str) -> None:
    fixture = build_fixture(tmp_path)
    index = sqlite3.connect(fixture.release / "index.sqlite3")
    try:
        row = index.execute("SELECT record_json FROM visual_variants WHERE variant_id='minecraft:stone'").fetchone()
        variant = json.loads(row[0])
        if mutation == "missing_state":
            variant["canonical_state_id"] = "minecraft:missing_state"
            index.execute("UPDATE visual_variants SET canonical_state_id=?,record_json=? WHERE variant_id='minecraft:stone'", (variant["canonical_state_id"], json.dumps(variant, sort_keys=True, separators=(",", ":"))))
        else:
            variant["block_id"] = "minecraft:glass"
            index.execute("UPDATE visual_variants SET block_id=?,record_json=? WHERE variant_id='minecraft:stone'", (variant["block_id"], json.dumps(variant, sort_keys=True, separators=(",", ":"))))
        index.commit()
    finally:
        index.close()
    _rehash_fixture(fixture, index_changed=True)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


@pytest.mark.parametrize("projection", ["fts", "feature", "render"])
def test_rehashed_projection_corruption_reaches_strict_validators(tmp_path: Path, projection: str) -> None:
    fixture = build_fixture(tmp_path)
    index = fixture.release / "index.sqlite3"
    connection = sqlite3.connect(index)
    try:
        if projection == "fts":
            connection.execute("UPDATE search_fts SET normalized_text='corrupt' WHERE variant_id='minecraft:stone'")
        elif projection == "feature":
            row = connection.execute("SELECT feature_json FROM visual_variants WHERE variant_id='minecraft:stone'").fetchone()
            feature = json.loads(row[0])
            feature["unexpected"] = True
            connection.execute("UPDATE visual_variants SET feature_json=? WHERE variant_id='minecraft:stone'", (json.dumps(feature, sort_keys=True, separators=(",", ":")),))
        else:
            connection.execute("UPDATE visual_variants SET render_metadata_path='previews/minecraft/stone/missing.json' WHERE variant_id='minecraft:stone'")
        connection.commit()
    finally:
        connection.close()
    _rehash_fixture(fixture, index_changed=True)
    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error and result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


def test_rehashed_malformed_png_is_rejected_by_bulk_image_validation(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    preview = fixture.release / "previews/minecraft/stone/preview.png"
    malformed = bytearray(preview.read_bytes())
    malformed[0] ^= 1
    preview.write_bytes(malformed)
    image_hash = "sha256:" + hashlib.sha256(malformed).hexdigest()
    index = sqlite3.connect(fixture.release / "index.sqlite3")
    try:
        row = index.execute("SELECT record_json FROM visual_variants WHERE variant_id='minecraft:stone'").fetchone()
        variant = json.loads(row[0])
        variant["render"]["image_sha256"] = image_hash
        index.execute(
            "UPDATE visual_variants SET image_sha256=?, record_json=? WHERE variant_id='minecraft:stone'",
            (image_hash, json.dumps(variant, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        )
        index.commit()
    finally:
        index.close()
    _rehash_fixture(fixture, index_changed=True)

    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error
    assert result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


def test_rehashed_manual_package_order_is_rejected(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    manual = json.loads((fixture.release / "manual-overrides.json").read_text(encoding="utf-8"))
    reviews = []
    for review_id in ("q2", "q1"):
        reviews.append({
            "schema_version": "qualification-review.v1",
            "review_id": review_id,
            "target_type": "visual_variant",
            "target_id": "minecraft:stone",
            "minecraft_version": "26.2",
            "reviewer": "fixture",
            "reviewed_at": "2026-08-18T12:00:00Z",
            "reason_code": "QUALIFICATION_CONFIRMED",
            "note": "Fixture qualification review.",
            "evidence": ["index.sqlite3"],
            "source_version": "fixture.v1",
            "qualification": "eligible",
            "warnings": [],
        })
    manual["qualification_reviews"] = reviews
    _write_json(fixture.release / "manual-overrides.json", manual)
    connection = sqlite3.connect(fixture.release / "index.sqlite3")
    try:
        row = connection.execute("SELECT record_json FROM visual_variants WHERE variant_id='minecraft:stone'").fetchone()
        variant = json.loads(row[0])
        variant["qualification_review_refs"] = ["q2", "q1"]
        connection.execute("UPDATE visual_variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(variant, ensure_ascii=False, sort_keys=True, separators=(",", ":")),))
        connection.commit()
    finally:
        connection.close()
    _rehash_fixture(fixture, index_changed=True)
    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error and result["error_code"] == "RELEASE_INTEGRITY_FAILED"
    assert result["details"]["integrity_component"] == "index"


def test_resolver_reads_current_each_request_and_rejects_current_corruption(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    resolver = MCPReleaseResolver(tmp_path)
    first = resolver.resolve()
    first.close()
    current = tmp_path / "current.json"
    value = json.loads(current.read_text(encoding="utf-8"))
    value["versions"]["26.2"]["manifest_sha256"] = "sha256:" + "f" * 64
    current.write_bytes((json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
    with pytest.raises(MCPReleaseError) as error:
        resolver.resolve()
    assert error.value.code == "RELEASE_INTEGRITY_FAILED"

    malformed = tmp_path / "malformed"
    build_fixture(malformed)
    (malformed / "current.json").write_bytes(b"not-json")
    malformed_result = MCPQueryService(malformed).index_info()
    assert malformed_result.is_error and malformed_result["error_code"] == "CURRENT_POINTER_INVALID"
