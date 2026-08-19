from __future__ import annotations

import hashlib
import json
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from blockpedia.mcp_query import MCPInputError, MCPQueryService, MCPToolResult, _Intent, deterministic_score, parse_query
from blockpedia import mcp_release
from blockpedia.mcp_release import MCPReleaseError, MCPReleaseResolver, ReleaseHandle
from blockpedia.schema import validate_record

from .fixture_builder import build_fixture


def _write_json(path: Path, value: object) -> None:
    path.write_bytes((json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))


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


def test_current_pointer_rejects_malformed_version_keys_and_default(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    current["versions"]["26"] = current["versions"]["26.2"]
    _write_json(tmp_path / "current.json", current)
    result = MCPQueryService(tmp_path).index_info()
    assert result.is_error and result["error_code"] == "CURRENT_POINTER_INVALID"

    build_fixture(tmp_path / "default")
    default_current = json.loads((tmp_path / "default" / "current.json").read_text(encoding="utf-8"))
    default_current["default_minecraft_version"] = "26"
    _write_json(tmp_path / "default" / "current.json", default_current)
    result = MCPQueryService(tmp_path / "default").index_info()
    assert result.is_error and result["error_code"] == "CURRENT_POINTER_INVALID"


def test_runtime_does_not_prevalidate_index_quality_or_sidecars(tmp_path: Path) -> None:
    v1 = tmp_path / "v1"
    build_fixture(v1, index_version=1)
    result = MCPQueryService(v1).index_info()
    assert not result.is_error

    corrupt = tmp_path / "corrupt"
    build_fixture(corrupt)
    quality = corrupt / "releases" / "26.2" / "rel_11111111111111111111111111111111" / "quality_report.json"
    quality.write_bytes(quality.read_bytes() + b"x")
    result = MCPQueryService(corrupt).index_info()
    assert not result.is_error

    sidecar = tmp_path / "sidecar"
    build_fixture(sidecar)
    index = sidecar / "releases" / "26.2" / "rel_11111111111111111111111111111111" / "index.sqlite3-wal"
    index.write_bytes(b"sidecar")
    result = MCPQueryService(sidecar).index_info()
    assert not result.is_error


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


def _host_spec() -> dict[str, object]:
    return json.loads(json.dumps(_Reranker._query_spec()))


def test_provider_injection_auto_required_and_id_mismatch_fallback(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    reranked = MCPQueryService(tmp_path, provider=_Reranker()).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not reranked.is_error and reranked["data"]["reranked_by_llm"] is True
    required = MCPQueryService(tmp_path).search_blocks({"query": "stone", "context": {"rerank": "required"}})
    assert required.is_error and required["error_code"] == "RERANK_REQUIRED_UNAVAILABLE"
    assert required["details"]["provider_error_code"] in {"PROVIDER_NOT_CONFIGURED", "PROVIDER_CONFIG_INVALID"}
    mismatch = MCPQueryService(tmp_path, provider=_BadReranker()).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not mismatch.is_error and mismatch["data"]["reranked_by_llm"] is False


def test_provider_query_spec_precedes_visual_rerank_and_family_string_is_noop(tmp_path: Path) -> None:
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

    without_family = MCPQueryService(tmp_path).search_blocks({"query": "stone", "context": {"rerank": "local_only"}})
    with_family = MCPQueryService(tmp_path).search_blocks({"query": "stone", "context": {"family": "unknown", "rerank": "local_only"}})
    assert not without_family.is_error and not with_family.is_error
    assert with_family["data"] == without_family["data"]
    assert with_family["warnings"] == without_family["warnings"]
    assert not any("family" in warning.casefold() for warning in with_family["warnings"])


def test_search_reuses_owned_provider_and_closes_it_once(tmp_path: Path) -> None:
    build_fixture(tmp_path)

    class _Owned(_Reranker):
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.closed = 0

        def query_spec(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("query_spec")
            return super().query_spec(_query, **kwargs)

        def visual_rerank(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("visual_rerank")
            return super().visual_rerank(_query, **kwargs)

        def close(self) -> None:
            self.closed += 1

    provider = _Owned()
    created: list[object] = []

    def factory(*_args: object) -> _Owned:
        created.append(provider)
        return provider

    result = MCPQueryService(tmp_path, provider_factory=factory).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not result.is_error
    assert created == [provider]
    assert provider.calls == ["query_spec", "visual_rerank"]
    assert provider.closed == 1


def test_search_reuses_preview_bytes_and_decoded_image_across_stages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_fixture(tmp_path)
    calls: list[str] = []
    original = ReleaseHandle.read_image

    def read_image(handle: ReleaseHandle, relative_ref: str) -> tuple[bytes, object]:
        calls.append(relative_ref)
        return original(handle, relative_ref)

    monkeypatch.setattr(ReleaseHandle, "read_image", read_image)
    result = MCPQueryService(tmp_path, provider=_Reranker()).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not result.is_error
    assert len(calls) == len(set(calls))


def test_snapshot_is_cached_for_same_release_handle(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    service = MCPQueryService(tmp_path)
    with service.resolver.resolve() as handle:
        first = service._snapshot(handle)
        second = service._snapshot(handle)
    assert first is second
    assert len(service._snapshots) == 1


def test_search_does_not_start_provider_request_inside_final_five_seconds(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    provider = _Reranker()
    calls: list[str] = []
    original_query_spec = provider.query_spec

    def query_spec(query: str, **kwargs: object) -> dict[str, object]:
        calls.append("query_spec")
        return original_query_spec(query, **kwargs)

    provider.query_spec = query_spec  # type: ignore[method-assign]
    clock = iter((0.0, 0.0, 50.1))
    result = MCPQueryService(tmp_path, provider=provider, monotonic=lambda: next(clock)).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not result.is_error
    assert calls == []
    assert any("deterministic local ranking" in warning for warning in result["warnings"])


def test_stage_timeout_factory_enforces_stage_caps_profile_cutoff_and_minimum() -> None:
    now = 100.0
    service = MCPQueryService(".", monotonic=lambda: now)
    provider = SimpleNamespace(profile=SimpleNamespace(request_timeout_ms=60000))
    query_factory = service._stage_timeout_factory(provider, 155.0, 100.0, 15.0, (10.0, 5.0))
    assert query_factory(1) == 10.0
    now = 106.0
    assert query_factory(2) == 5.0
    now = 114.2
    assert query_factory(2) is None

    rerank_factory = service._stage_timeout_factory(provider, 155.0, 100.0, 30.0, (20.0, 10.0))
    now = 100.0
    assert rerank_factory(1) == 20.0
    now = 110.0
    assert rerank_factory(2) == 10.0
    now = 129.2
    assert rerank_factory(2) is None

    limited = SimpleNamespace(profile=SimpleNamespace(request_timeout_ms=7000))
    now = 100.0
    assert service._stage_timeout_factory(limited, 155.0, 100.0, 15.0, (10.0, 5.0))(1) == 7.0
    now = 149.2
    assert service._stage_timeout_factory(provider, 155.0, 100.0, 15.0, (10.0, 5.0))(1) is None


def test_local_only_does_not_load_provider(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    created = 0

    def factory(*_args: object) -> object:
        nonlocal created
        created += 1
        raise AssertionError("local_only must not create a provider")

    result = MCPQueryService(tmp_path, provider_factory=factory).search_blocks({"query": "stone", "context": {"rerank": "local_only"}})
    assert not result.is_error
    assert created == 0


def test_provider_type_error_is_not_retried_without_kwargs(tmp_path: Path) -> None:
    build_fixture(tmp_path)

    class _TypeErrorProvider:
        def __init__(self) -> None:
            self.calls = 0

        def query_spec(self, _query: str, **_kwargs: object) -> dict[str, object]:
            self.calls += 1
            raise TypeError("fixture provider failure")

    provider = _TypeErrorProvider()
    result = MCPQueryService(tmp_path, provider=provider).search_blocks({"query": "stone", "context": {"rerank": "auto"}})
    assert not result.is_error
    assert provider.calls == 1
    assert result["data"]["reranked_by_llm"] is False


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


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("contradictory_alias", "contradictory"),
        ("ambiguity_flag", "needs_user_choice"),
    ],
)
def test_host_query_spec_semantic_invariants_are_query_invalid(tmp_path: Path, mutation: str, message: str) -> None:
    build_fixture(tmp_path)
    spec = _host_spec()
    hard = spec["hard"]
    assert isinstance(hard, dict)
    if mutation == "contradictory_alias":
        hard["behaviors"] = [{"field": "transparent", "operator": "eq", "value": True, "source": "system", "required": True}]
        hard["transparency"] = [{"operator": "eq", "value": False, "source": "system", "required": True}]
    else:
        spec["ambiguities"] = [{"point": "shape", "candidates": ["thin", "full"]}]
        spec["needs_user_choice"] = False
    result = MCPQueryService(tmp_path).search_blocks({"query": "stone", "query_spec": spec, "context": {"rerank": "local_only"}})
    assert result.is_error and result["error_code"] == "QUERY_INVALID"
    assert message in result["message"]


def test_host_query_spec_drops_unconfirmed_hard_and_preserves_local_explicit_hard(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    unconfirmed = _host_spec()
    hard = unconfirmed["hard"]
    assert isinstance(hard, dict)
    hard["behaviors"] = [{"field": "transparent", "operator": "eq", "value": True, "source": "system", "required": True}]
    dropped = MCPQueryService(tmp_path).search_blocks({"query": "stone", "query_spec": unconfirmed, "context": {"rerank": "local_only"}})
    assert not dropped.is_error
    assert any("Unconfirmed host hard" in warning for warning in dropped["warnings"])
    assert [item["block_id"] for item in dropped["data"]["candidates"]] == ["minecraft:stone"]

    ambiguous = _host_spec()
    ambiguous_hard = ambiguous["hard"]
    assert isinstance(ambiguous_hard, dict)
    ambiguous_hard["behaviors"] = [{"field": "transparent", "operator": "eq", "value": True, "source": "system", "required": True}]
    ambiguous["ambiguities"] = [{"point": "material", "candidates": ["glass", "stone"]}]
    ambiguous["needs_user_choice"] = True
    unresolved = MCPQueryService(tmp_path).search_blocks({"query": "stone", "query_spec": ambiguous, "context": {"rerank": "local_only"}})
    assert not unresolved.is_error
    assert [item["block_id"] for item in unresolved["data"]["candidates"]] == ["minecraft:stone"]

    weakened = _host_spec()
    hard = weakened["hard"]
    assert isinstance(hard, dict)
    hard["behaviors"] = [{"field": "transparent", "operator": "eq", "value": False, "source": "system", "required": True}]
    result = MCPQueryService(tmp_path).search_blocks({"query": "必须透明 wall", "query_spec": weakened, "context": {"rerank": "local_only"}})
    assert not result.is_error
    assert [item["block_id"] for item in result["data"]["candidates"]] == ["minecraft:glass"]

    class _Captured(_Reranker):
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.effective_spec: dict[str, object] | None = None

        def query_spec(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("query_spec")
            raise AssertionError("host QuerySpec must suppress server-side generation")

        def visual_rerank(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("visual_rerank")
            value = kwargs["query_spec"]
            assert isinstance(value, dict)
            self.effective_spec = value
            return super().visual_rerank(_query, **kwargs)

    provider = _Captured()
    reranked = MCPQueryService(tmp_path, provider=provider).search_blocks({"query": "必须透明 wall", "query_spec": weakened, "context": {"rerank": "auto"}})
    assert not reranked.is_error
    assert provider.calls == ["visual_rerank"]
    assert provider.effective_spec is not None
    effective_behaviors = provider.effective_spec["hard"]["behaviors"]  # type: ignore[index]
    assert any(item["field"] == "transparent" and item["operator"] == "eq" and item["value"] is True for item in effective_behaviors)
    assert not any(item["field"] == "transparent" and item["value"] is False for item in effective_behaviors)
    assert any("Unconfirmed host hard" in warning for warning in reranked["warnings"])

    unknown_intent = _Intent(({"field": "behavior.transparent", "operator": "unknown_internal", "value": True},), {}, (), ())
    unknown_effective, _ = MCPQueryService._effective_host_spec(_host_spec(), unknown_intent)
    assert unknown_effective["hard"]["behaviors"] == []  # type: ignore[index]


def test_host_avoid_for_is_warning_only_and_is_not_sent_to_reranker(tmp_path: Path) -> None:
    build_fixture(tmp_path)

    class _Captured(_Reranker):
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.specs: list[dict[str, object]] = []

        def query_spec(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("query_spec")
            raise AssertionError("host QuerySpec must suppress server-side generation")

        def visual_rerank(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("visual_rerank")
            value = kwargs["query_spec"]
            assert isinstance(value, dict)
            self.specs.append(value)
            return super().visual_rerank(_query, **kwargs)

    without = _host_spec()
    with_avoid = _host_spec()
    soft = with_avoid["soft"]
    assert isinstance(soft, dict)
    soft["avoid_for"] = [{"term": "redstone", "source": "user_explicit", "weight": 1.0}]
    first_provider = _Captured()
    second_provider = _Captured()
    first = MCPQueryService(tmp_path, provider=first_provider).search_blocks({"query": "stone", "query_spec": without, "context": {"rerank": "auto"}})
    second = MCPQueryService(tmp_path, provider=second_provider).search_blocks({"query": "stone", "query_spec": with_avoid, "context": {"rerank": "auto"}})
    assert first_provider.calls == ["visual_rerank"]
    assert second_provider.calls == ["visual_rerank"]
    assert second["warnings"] and "avoid_for" in " ".join(second["warnings"])
    assert second_provider.specs[0]["soft"]["avoid_for"] == []  # type: ignore[index]
    assert [item["block_id"] for item in first["data"]["candidates"]] == [item["block_id"] for item in second["data"]["candidates"]]
    assert first["data"]["reranked_by_llm"] is True and second["data"]["reranked_by_llm"] is True


def test_host_query_spec_provider_matrix_only_calls_visual_rerank(tmp_path: Path) -> None:
    build_fixture(tmp_path)

    class _Matrix(_Reranker):
        def __init__(self, fail: bool = False) -> None:
            self.calls: list[str] = []
            self.fail = fail

        def query_spec(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("query_spec")
            raise AssertionError("host QuerySpec must suppress generation")

        def visual_rerank(self, _query: str, **kwargs: object) -> dict[str, object]:
            self.calls.append("visual_rerank")
            if self.fail:
                return {}
            return super().visual_rerank(_query, **kwargs)

    for mode, expected in (("local_only", []), ("auto", ["visual_rerank"]), ("required", ["visual_rerank"])):
        provider = _Matrix()
        result = MCPQueryService(tmp_path, provider=provider).search_blocks({"query": "stone", "query_spec": _host_spec(), "context": {"rerank": mode}})
        assert provider.calls == expected
        assert result.is_error is False
        assert result["data"]["reranked_by_llm"] is (mode != "local_only")

    provider = _Matrix(fail=True)
    result = MCPQueryService(tmp_path, provider=provider).search_blocks({"query": "stone", "query_spec": _host_spec(), "context": {"rerank": "required"}})
    assert provider.calls == ["visual_rerank"]
    assert result.is_error and result["error_code"] == "RERANK_REQUIRED_UNAVAILABLE"


def test_host_query_spec_identity_is_canonical_and_output_is_closed(tmp_path: Path) -> None:
    build_fixture(tmp_path)
    before = _inventory(tmp_path)
    spec = _host_spec()
    reordered = json.loads(json.dumps(spec, sort_keys=True))
    first = MCPQueryService(tmp_path).search_blocks({"query": "stone", "query_spec": spec, "context": {"rerank": "local_only"}})
    second = MCPQueryService(tmp_path).search_blocks({"query": "stone", "query_spec": reordered, "context": {"rerank": "local_only"}})
    assert first["data"]["search_id"] == second["data"]["search_id"]
    different = _host_spec()
    soft = different["soft"]
    assert isinstance(soft, dict)
    soft["keywords"] = [{"term": "distinct", "source": "user_explicit", "weight": 1.0}]
    third = MCPQueryService(tmp_path).search_blocks({"query": "stone", "query_spec": different, "context": {"rerank": "local_only"}})
    assert first["data"]["search_id"] != third["data"]["search_id"]
    validate_record("mcp-search-blocks-output.v1", first)
    assert "query_spec" not in first and "query_spec_sha256" not in json.dumps(first)
    assert _inventory(tmp_path) == before


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


def test_pointer_switch_loads_a_new_snapshot(tmp_path: Path) -> None:
    import shutil

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


def test_path_and_index_open_failures_are_mapped_without_integrity_validation(tmp_path: Path) -> None:
    import shutil

    fixture = build_fixture(tmp_path)
    current = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
    current["versions"]["26.2"]["relative_path"] = "releases/26.2/../escape"
    _write_json(tmp_path / "current.json", current)
    path_result = MCPQueryService(tmp_path).index_info()
    assert path_result.is_error and path_result["error_code"] == "CURRENT_POINTER_INVALID"

    missing = build_fixture(tmp_path / "missing")
    missing_release_id = "rel_" + "3" * 32
    missing_release = missing.release.parent / missing_release_id
    shutil.copytree(missing.release, missing_release)
    (missing_release / "index.sqlite3").unlink()
    missing_current = json.loads((missing.root / "current.json").read_text(encoding="utf-8"))
    missing_current["versions"]["26.2"].update({"release_id": missing_release_id, "relative_path": f"releases/26.2/{missing_release_id}"})
    _write_json(missing.root / "current.json", missing_current)
    open_result = MCPQueryService(missing.root).index_info()
    assert open_result.is_error and open_result["error_code"] == "INDEX_OPEN_FAILED"


def test_malformed_png_fails_only_when_requested(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    preview = fixture.release / "previews/minecraft/stone/preview.png"
    payload = bytearray(preview.read_bytes())
    payload[0] ^= 1
    preview.write_bytes(payload)
    assert not MCPQueryService(tmp_path).index_info().is_error
    details = MCPQueryService(tmp_path).get_block_details({"block_id": "minecraft:stone"})
    assert details.is_error and details["error_code"] == "IMAGE_READ_FAILED"


def test_reparse_point_is_rejected_before_release_read() -> None:
    class _ReparsePath:
        def lstat(self) -> object:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400)

    with pytest.raises(MCPReleaseError) as error:
        mcp_release._lstat(_ReparsePath(), directory=False)  # type: ignore[arg-type]
    assert error.value.code == "CURRENT_POINTER_INVALID"
