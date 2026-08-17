from __future__ import annotations

from dataclasses import replace
import importlib.util
import hashlib
import json
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from blockpedia.features import decode_rgba_png
from blockpedia.paths import DataRoot
from blockpedia.provider import OpenAIProvider, ProviderProfile, SecretResolver, StageConfig
from blockpedia.r3 import encode_rgba_png
from blockpedia.services import R3Error, StudioService
from blockpedia.worker import build_ai_plan_hash


def _r2_fixture_module():
    path = Path(__file__).parents[1] / "r2" / "conftest.py"
    spec = importlib.util.spec_from_file_location("blockpedia_r2_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Keyring:
    def get_password(self, service: str, account: str) -> str:
        assert service == "blockpedia"
        return "fixture-secret"


class _FakeProvider:
    def __init__(self, confidence: float = 0.9, fail: bool = False) -> None:
        self.confidence = confidence
        self.fail = fail
        self.calls = 0

    def annotate(self, _prompt: str, **kwargs):
        self.calls += 1
        if self.fail:
            return {"status": "needs_review", "attempts_used": 2, "error_code": "PROVIDER_SERVER_ERROR", "error_class": "retryable"}
        ids = [item["variant_id"] for item in kwargs["envelope"]["input_summary"]["tile_variant_map"]]
        return {
            "status": "succeeded",
            "attempts_used": 1,
            "parsed_artifact": {
                "schema_id": "annotation-batch-output.v1",
                "items": [
                    {
                        "variant_id": variant_id,
                        "synonyms_zh": [],
                        "synonyms_en": ["approved stone"],
                        "summary_zh": "石头方块",
                        "summary_en": "An approved stone block.",
                        "color_terms": [],
                        "shape_terms": [],
                        "material_impressions": [],
                        "building_roles": ["wall"],
                        "style_tags": [],
                        "avoid_for": [],
                        "confidence": self.confidence,
                        "reason": "fixture annotation",
                    }
                    for variant_id in ids
                ],
            },
        }

    def close(self) -> None:
        pass


class _ScriptedProvider(_FakeProvider):
    def __init__(self, outcomes: list[str | None]) -> None:
        super().__init__(confidence=0.90)
        self.outcomes = list(outcomes)

    def annotate(self, _prompt: str, **kwargs):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if outcome is not None:
            return {"status": "needs_review" if outcome != "PROVIDER_UNKNOWN" else "failed", "attempts_used": 1, "error_code": outcome, "error_class": "authentication" if outcome == "PROVIDER_AUTH_FAILED" else "retryable"}
        ids = [item["variant_id"] for item in kwargs["envelope"]["input_summary"]["tile_variant_map"]]
        return {
            "status": "succeeded",
            "attempts_used": 1,
            "parsed_artifact": {
                "schema_id": "annotation-batch-output.v1",
                "items": [
                    {
                        "variant_id": variant_id,
                        "synonyms_zh": [],
                        "synonyms_en": ["approved stone"],
                        "summary_zh": "石头方块",
                        "summary_en": "An approved stone block.",
                        "color_terms": [],
                        "shape_terms": [],
                        "material_impressions": [],
                        "building_roles": ["wall"],
                        "style_tags": [],
                        "avoid_for": [],
                        "confidence": 0.90,
                        "reason": "fixture annotation",
                    }
                    for variant_id in ids
                ],
            },
        }


class _DiagnosticProvider(_FakeProvider):
    def __init__(
        self,
        diagnostic: dict[str, object] | None,
        *,
        status: str = "needs_review",
        attempts_used: int = 2,
        error_code: str = "PROVIDER_SCHEMA_INVALID",
        error_class: str = "validation",
        parsed_artifact: dict[str, object] | None = None,
    ) -> None:
        super().__init__()
        self.diagnostic = diagnostic
        self.status = status
        self.attempts_used = attempts_used
        self.error_code = error_code
        self.error_class = error_class
        self.parsed_artifact = parsed_artifact

    def annotate(self, _prompt: str, **_kwargs):
        self.calls += 1
        return {
            "status": self.status,
            "attempts_used": self.attempts_used,
            "error_code": self.error_code,
            "error_class": self.error_class,
            "parsed_artifact": self.parsed_artifact,
            "validation_diagnostic": self.diagnostic,
        }


class _ConcurrencyTracker:
    def __init__(self, target: int, *, release: bool = False, outcome: str = "PROVIDER_SERVER_ERROR") -> None:
        self.target = target
        self.release = threading.Event()
        if release:
            self.release.set()
        self.entered = threading.Event()
        self.outcome = outcome
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.calls = 0
        self.created = 0
        self.closed = 0


class _FreshBarrierProvider:
    def __init__(self, tracker: _ConcurrencyTracker) -> None:
        self.tracker = tracker
        with tracker.lock:
            tracker.created += 1

    def annotate(self, _prompt: str, **_kwargs):
        tracker = self.tracker
        with tracker.lock:
            tracker.active += 1
            tracker.calls += 1
            tracker.max_active = max(tracker.max_active, tracker.active)
            if tracker.active >= tracker.target:
                tracker.entered.set()
        tracker.release.wait()
        with tracker.lock:
            tracker.active -= 1
        return {
            "status": "failed" if tracker.outcome in {"PROVIDER_AUTH_FAILED", "PROVIDER_MODEL_UNAVAILABLE"} else "needs_review",
            "attempts_used": 1,
            "error_code": tracker.outcome,
            "error_class": "authentication" if tracker.outcome == "PROVIDER_AUTH_FAILED" else "retryable",
        }

    def close(self) -> None:
        with self.tracker.lock:
            self.tracker.closed += 1


def _duplicate_ai_job(service: StudioService, run_id: str) -> None:
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            source = connection.execute("SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' LIMIT 1", (run_id,)).fetchone()
            assert source is not None
            connection.execute(
                "INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("job_second", run_id, "AI_ANNOTATE", "ai_batch_0001", source["input_signature"], "pending", 0, source["cursor_json"], source["created_at"]),
            )


def test_d040_plan_hash_fixed_vector_uses_recomputed_payload_signature() -> None:
    assert build_ai_plan_hash(
        "run_vector",
        "sha256:config",
        [
            {"job_id": "job_a", "logical_key": "ai_batch_0000", "recomputed_payload_signature": "sha256:payload_a"},
            {"job_id": "job_b", "logical_key": "ai_batch_0001", "recomputed_payload_signature": "sha256:payload_b"},
        ],
    ) == "sha256:2beeab98edf9abce6be5feceb68f6ed13df570d0db18db34804dbe6f68a22ded"


def _service(tmp_path: Path, fake: _FakeProvider, *, adapter: str = "openai_responses") -> tuple[StudioService, str, dict[str, str]]:
    fixture = _r2_fixture_module()
    export = fixture.make_export(tmp_path)
    service = StudioService(
        DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        toolchain_probe=fixture.PassingToolchainProbe(),
        provider_factory=lambda profile, **_kwargs: fake,
        secret_resolver=SecretResolver(keyring_backend=_Keyring()),
    )
    check = service.check_import(export, "26.2")
    imported = service.import_checked(check.check_id)
    run_id = imported["run_id"]
    for _ in range(6):
        service.tick(run_id)
    profile = ProviderProfile(profile_id="default", model_id="fixture-model", adapter=adapter, base_url="http://127.0.0.1:8766/v1")
    service.save_profile(profile)
    service.profile_store.record_probe(
        {
            "profile_id": "default",
            "adapter": adapter,
            "capability_status": "verified",
            "image_input_supported": True,
            "structured_outputs_supported": True,
            "error_classification_supported": True,
            "store_false_supported": True,
            "base_url_stable_id": profile.base_url_stable_id,
        }
    )
    service.enable("default")
    service.configure_run(imported["import_id"], "26.2", profile_id="default")
    return service, run_id, imported


def _tracked_service(
    tmp_path: Path,
    tracker: _ConcurrencyTracker,
    *,
    concurrency: int,
    job_count: int,
    run_count: int = 1,
) -> tuple[StudioService, list[str], list[dict[str, str]]]:
    fixture = _r2_fixture_module()
    service = StudioService(
        DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        toolchain_probe=fixture.PassingToolchainProbe(),
        provider_factory=lambda _profile, **_kwargs: _FreshBarrierProvider(tracker),
        secret_resolver=SecretResolver(keyring_backend=_Keyring()),
    )
    imported: list[dict[str, str]] = []
    base_export = fixture.make_export(tmp_path)
    exports = [base_export]
    for index in range(1, run_count):
        old_id = base_export.name
        new_id = f"export_20260814T1200{index:02d}Z"
        export = tmp_path / "exports" / "26.2" / new_id
        shutil.copytree(base_export, export)
        for path in export.rglob("*"):
            if path.is_file() and path.name != "checksums.sha256":
                path.write_bytes(path.read_bytes().replace(old_id.encode(), new_id.encode()))
        files = sorted(path.relative_to(export).as_posix() for path in export.rglob("*") if path.is_file() and path.name != "checksums.sha256")
        (export / "checksums.sha256").write_bytes(
            "".join(hashlib.sha256((export / ref).read_bytes()).hexdigest() + "  " + ref + "\n" for ref in files).encode()
        )
        exports.append(export)
    for export in exports:
        check = service.check_import(export, "26.2")
        assert check.can_import, check.issues
        imported.append(service.import_checked(check.check_id))
    for run_index, item in enumerate(imported):
        for _ in range(6):
            service.tick(item["run_id"])
    profile = ProviderProfile(profile_id="default", model_id="fixture-model", base_url="http://127.0.0.1:8766/v1")
    stages = dict(profile.stages)
    stages["offline_annotation"] = StageConfig(batch_size=12, concurrency=concurrency)
    profile = replace(profile, stages=stages)
    service.save_profile(profile)
    service.profile_store.record_probe(
        {
            "profile_id": "default",
            "adapter": profile.adapter,
            "capability_status": "verified",
            "image_input_supported": True,
            "structured_outputs_supported": True,
            "error_classification_supported": True,
            "store_false_supported": True,
            "base_url_stable_id": profile.base_url_stable_id,
        }
    )
    service.enable("default")
    run_ids: list[str] = []
    for run_index, item in enumerate(imported):
        run_id = str(item["run_id"])
        service.configure_run(item["import_id"], "26.2", profile_id="default")
        run_ids.append(run_id)
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                source = connection.execute("SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' LIMIT 1", (run_id,)).fetchone()
                assert source is not None
                for job_index in range(1, job_count):
                    connection.execute(
                        "INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (
                            f"job_wave_{run_index}_{job_index}",
                            run_id,
                            "AI_ANNOTATE",
                            f"ai_batch_{job_index:04d}",
                            source["input_signature"],
                            "pending",
                            0,
                            source["cursor_json"],
                            source["created_at"],
                        ),
                    )
    return service, run_ids, imported


def _approve_plan(service: StudioService, run_id: str) -> dict[str, Any]:
    plan = service.preview_ai_plan(run_id)
    approved = service.approve_ai_plan(run_id, plan["plan_hash"])
    assert approved["approved"] is True
    return plan


def _wait_registry_empty(service: StudioService, run_id: str) -> None:
    with service.worker._ai_registry_changed:
        while service.worker.has_live_ai_futures(run_id):
            service.worker._ai_registry_changed.wait()


def _prepare_d044_reconfiguration(
    service: StudioService,
    run_id: str,
) -> tuple[dict[str, object], str]:
    service.tick(run_id)
    old_plan = service.preview_ai_plan(run_id)
    current = service.profile_store.load()["default"]
    stages = dict(current.stages)
    stages["offline_annotation"] = StageConfig(batch_size=12, concurrency=5)
    service.save_provider_profile(replace(current, stages=stages))
    return old_plan, str(old_plan["jobs"][0]["job_id"])


def test_chat_profile_configure_run_ai_path_and_protocol_lineage(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.90)
    service, run_id, _ = _service(tmp_path, fake, adapter="openai_chat_completions")
    try:
        config = service.get_run(run_id)["config_snapshot"]
        assert config["provider_snapshot"]["adapter"] == "openai_chat_completions"
        assert config["provider_snapshot"]["profile"]["adapter"] == "openai_chat_completions"
        assert config["capabilities"]["adapter"] == "openai_chat_completions"
        assert "store_false_supported" not in config["capabilities"]

        caps = service._public_capabilities("default")
        assert caps["adapter"] == "openai_chat_completions"
        assert "store_false_supported" not in caps

        _approve_first(service, run_id)
        service.tick(run_id)
        assert fake.calls == 1
        with service.worker.open_database(run_id) as database:
            request = database.fetchone("SELECT envelope_json FROM provider_requests")
            assert request is not None
            envelope = json.loads(request["envelope_json"])
        assert envelope["adapter"] == "openai_chat_completions"
        assert "store" not in envelope
    finally:
        service.close()


def test_configure_gate_requires_matching_adapter_but_not_store_capability(tmp_path: Path) -> None:
    service, run_id, imported = _service(tmp_path, _FakeProvider(), adapter="openai_chat_completions")
    try:
        capabilities = service.profile_store.capabilities("default") or {}
        capabilities["adapter"] = "openai_responses"
        service.profile_store.save_capabilities("default", capabilities)
        with pytest.raises(R3Error) as exc_info:
            service.configure_run(imported["import_id"], "26.2", profile_id="default")
        assert exc_info.value.code == "PROVIDER_CAPABILITY_MISSING"
    finally:
        service.close()


def test_worker_reloads_selected_adapter_and_rejects_unlineaged_snapshot(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(), adapter="openai_chat_completions")
    try:
        with service.worker.open_database(run_id) as database:
            profile = service.worker._run_profile(database, run_id)
        assert profile.adapter == "openai_chat_completions"

        service.worker.provider_factory = None
        provider = service.worker._new_provider(profile)
        try:
            assert isinstance(provider, OpenAIProvider)
            assert provider.profile.adapter == "openai_chat_completions"
        finally:
            provider.close()

        preview = service.preview_ai_batch(run_id)
        _approve_first(service, run_id)
        with service.worker.open_database(run_id) as database:
            row = database.fetchone("SELECT config_snapshot_json FROM runs WHERE run_id=?", (run_id,))
            assert row is not None
            config = json.loads(row["config_snapshot_json"])
            config["provider_snapshot"].pop("adapter", None)
            config["provider_snapshot"]["profile"].pop("adapter", None)
            database.execute("UPDATE runs SET config_snapshot_json=? WHERE run_id=?", (json.dumps(config, sort_keys=True), run_id))
        service.tick(run_id)
        assert service.get_run(run_id)["status"] == "failed"
        assert service.worker.provider_factory is None
        with service.worker.open_database(run_id) as database:
            job = database.fetchone("SELECT status,error_code FROM jobs WHERE logical_key=?", (preview["logical_key"],))
            stage = database.fetchone("SELECT status FROM stage_runs WHERE run_id=? AND stage='AI_ANNOTATE'", (run_id,))
        assert job is not None and job["status"] == "failed" and job["error_code"] == "PROVIDER_CONFIG_INVALID"
        assert stage is not None and stage["status"] == "failed"
    finally:
        service.close()


def test_responses_and_chat_signatures_caches_and_envelopes_diverge(tmp_path: Path) -> None:
    responses, responses_run, _ = _service(tmp_path / "responses", _FakeProvider(confidence=0.90), adapter="openai_responses")
    chat, chat_run, _ = _service(tmp_path / "chat", _FakeProvider(confidence=0.90), adapter="openai_chat_completions")
    try:
        response_preview = responses.preview_ai_batch(responses_run)
        chat_preview = chat.preview_ai_batch(chat_run)
        assert response_preview["input_signature"] != chat_preview["input_signature"]

        _approve_first(responses, responses_run)
        _approve_first(chat, chat_run)
        responses.tick(responses_run)
        chat.tick(chat_run)
        with responses.worker.open_database(responses_run) as database:
            response_request = database.fetchone("SELECT cache_key,envelope_json FROM provider_requests")
        with chat.worker.open_database(chat_run) as database:
            chat_request = database.fetchone("SELECT cache_key,envelope_json FROM provider_requests")
        assert response_request is not None and chat_request is not None
        assert response_request["cache_key"] != chat_request["cache_key"]
        response_envelope = json.loads(response_request["envelope_json"])
        chat_envelope = json.loads(chat_request["envelope_json"])
        assert response_envelope["adapter"] == "openai_responses"
        assert response_envelope["store"] is False
        assert chat_envelope["adapter"] == "openai_chat_completions"
        assert "store" not in chat_envelope
        for envelope in (response_envelope, chat_envelope):
            encoded = json.dumps(envelope, sort_keys=True)
            assert "fixture-secret" not in encoded
            assert "usage" not in encoded
            assert "raw_response" not in encoded
    finally:
        responses.close()
        chat.close()


def _approve_first(service: StudioService, run_id: str) -> None:
    preview = service.preview_ai_batch(run_id)
    assert preview["approved"] is False
    assert not any(token in preview["prompt"] for token in ("fixture-secret", "C:\\", "/data/"))
    assert decode_rgba_png(preview["contact_sheet_png"]).width >= 512
    service.approve_ai_batch(run_id, preview["logical_key"], preview["input_signature"])


def _set_sample_rate(service: StudioService, run_id: str, sample_rate: int) -> None:
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            row = connection.execute("SELECT config_snapshot_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
            config = json.loads(row["config_snapshot_json"] if row is not None else "{}")
            config["sample_rate"] = sample_rate
            connection.execute("UPDATE runs SET config_snapshot_json=? WHERE run_id=?", (json.dumps(config, sort_keys=True), run_id))


def _advance_to_human_review(service: StudioService, run_id: str) -> None:
    _approve_first(service, run_id)
    service.tick(run_id)
    service.tick(run_id)


def test_preview_approval_and_same_run_lifecycle(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.70)
    service, run_id, imported = _service(tmp_path, fake)
    assert len(list((tmp_path / "workspace" / "26.2").glob("*/work.sqlite3"))) == 1
    preview = service.preview_ai_batch(run_id)
    assert fake.calls == 0
    _approve_first(service, run_id)
    service.tick(run_id)
    assert fake.calls == 1
    with service.worker.open_database(run_id) as database:
        request = database.fetchone("SELECT envelope_json,validated_artifact_sha256 FROM provider_requests")
        assert request is not None
        assert "fixture-secret" not in request["envelope_json"]
        assert "usage" not in request["envelope_json"]
        assert "raw" not in request["envelope_json"]
    service.close()
    assert imported["run_id"] == run_id


def test_confidence_gate_failure_review_and_no_repeat(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.70)
    service, run_id, _ = _service(tmp_path, fake)
    _approve_first(service, run_id)
    for _ in range(3):
        service.tick(run_id)
    reviews = service.list_reviews(run_id)
    assert any(item["severity"] == "normal" for item in reviews)
    assert fake.calls == 1
    service.tick(run_id)
    assert fake.calls == 1
    service.close()


def test_provider_final_failure_is_high_review_without_raw_request(tmp_path: Path) -> None:
    fake = _FakeProvider(fail=True)
    service, run_id, _ = _service(tmp_path, fake)
    _approve_first(service, run_id)
    service.tick(run_id)
    reviews = service.list_reviews(run_id)
    assert any(item["reason_code"] == "PROVIDER_FAILURE" and item["severity"] == "high" for item in reviews)
    with service.worker.open_database(run_id) as database:
        request = database.fetchone("SELECT envelope_json,attempt FROM provider_requests")
        assert request is not None and request["attempt"] == 2
        assert "fixture-secret" not in request["envelope_json"]
        assert "usage" not in request["envelope_json"]
    service.close()


@pytest.mark.parametrize("malformed", [False, True])
def test_final_provider_diagnostic_review_evidence_is_safe_and_bounded(tmp_path: Path, malformed: bool) -> None:
    valid = {
        "stage": "offline_annotation",
        "phase": "wire_schema",
        "path": "$.items[0].reason",
        "keyword": "required",
        "observed_type": "missing",
        "observed_length": None,
    }
    diagnostic = {**valid, "raw_output": "RAW_PROVIDER_RESPONSE", "secret": "fixture-secret"} if malformed else valid
    fake = _DiagnosticProvider(diagnostic)
    service, run_id, _ = _service(tmp_path, fake)
    try:
        _approve_first(service, run_id)
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            review_rows = database.fetchall("SELECT evidence_json FROM review_tasks WHERE reason_code='PROVIDER_FAILURE'")
            request_row = database.fetchone("SELECT envelope_json FROM provider_requests WHERE error_code='PROVIDER_SCHEMA_INVALID'")
        assert review_rows and request_row is not None
        evidences = [json.loads(row["evidence_json"]) for row in review_rows]
        assert all(any(str(item).startswith("job:") for item in evidence) for evidence in evidences)
        assert all(any(str(item).startswith("provider_request:") for item in evidence) for evidence in evidences)
        mappings = [item for evidence in evidences for item in evidence if isinstance(item, dict)]
        if malformed:
            assert mappings == []
        else:
            assert mappings and all(item == valid for item in mappings)
        serialized_evidence = json.dumps(evidences, ensure_ascii=False)
        assert all(secret not in serialized_evidence for secret in ("RAW_PROVIDER_RESPONSE", "fixture-secret", "repair_context", "prompt"))
        serialized_request = json.dumps(json.loads(request_row["envelope_json"]), ensure_ascii=False)
        assert all(secret not in serialized_request for secret in ("RAW_PROVIDER_RESPONSE", "fixture-secret", "repair_context"))
        if not malformed:
            assert all(set(item) == set(valid) for item in mappings)
    finally:
        service.close()


@pytest.mark.parametrize(
    ("status", "attempts_used", "error_code", "error_class", "expected_diagnostic"),
    [
        ("needs_review", 1, "PROVIDER_SCHEMA_INVALID", "validation", False),
        ("needs_review", 2, "PROVIDER_NETWORK_ERROR", "retryable", False),
        ("failed", 2, "PROVIDER_AUTH_FAILED", "authentication", False),
        ("needs_review", 2, "PROVIDER_SCHEMA_INVALID", "validation", True),
    ],
)
def test_provider_diagnostic_requires_final_exhausted_validation_outcome(
    tmp_path: Path,
    status: str,
    attempts_used: int,
    error_code: str,
    error_class: str,
    expected_diagnostic: bool,
) -> None:
    diagnostic = {
        "stage": "offline_annotation",
        "phase": "wire_schema",
        "path": "$.items[0].reason",
        "keyword": "required",
        "observed_type": "missing",
        "observed_length": None,
    }
    service, run_id, _ = _service(
        tmp_path,
        _DiagnosticProvider(
            diagnostic,
            status=status,
            attempts_used=attempts_used,
            error_code=error_code,
            error_class=error_class,
        ),
    )
    try:
        _approve_first(service, run_id)
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            rows = database.fetchall("SELECT evidence_json FROM review_tasks WHERE reason_code='PROVIDER_FAILURE'")
        mappings = [item for row in rows for item in json.loads(row["evidence_json"]) if isinstance(item, dict)]
        assert bool(mappings) is expected_diagnostic
        if expected_diagnostic:
            assert mappings == [diagnostic]
        else:
            assert mappings == []
    finally:
        service.close()


def test_successful_provider_result_creates_no_diagnostic_review_evidence(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.90))
    try:
        _approve_first(service, run_id)
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            rows = database.fetchall("SELECT evidence_json FROM review_tasks")
        assert all(not any(isinstance(item, dict) for item in json.loads(row["evidence_json"])) for row in rows)
    finally:
        service.close()


def test_worker_validation_diagnostic_overrides_injected_success_diagnostic(tmp_path: Path) -> None:
    injected = {
        "stage": "offline_annotation",
        "phase": "wire_schema",
        "path": "$.injected",
        "keyword": "injected",
        "observed_type": "string",
        "observed_length": 8,
    }
    service, run_id, _ = _service(
        tmp_path,
        _DiagnosticProvider(
            injected,
            status="succeeded",
            attempts_used=1,
            error_code="PROVIDER_UNKNOWN",
            error_class="unknown",
            parsed_artifact={"schema_id": "annotation-batch-output.v1", "items": []},
        ),
    )
    try:
        _approve_first(service, run_id)
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            rows = database.fetchall("SELECT evidence_json FROM review_tasks WHERE reason_code='PROVIDER_FAILURE'")
        mappings = [item for row in rows for item in json.loads(row["evidence_json"]) if isinstance(item, dict)]
        assert mappings
        assert injected not in mappings
        assert all(item.get("path") != "$.injected" and item.get("keyword") != "injected" for item in mappings)
    finally:
        service.close()


def test_machine_fact_resolution_is_rejected_and_skip_is_independent(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.70)
    service, run_id, _ = _service(tmp_path, fake)
    _approve_first(service, run_id)
    for _ in range(3):
        service.tick(run_id)
    normal = next(item for item in service.list_reviews(run_id) if item["reason_code"] == "LOW_CONFIDENCE" or item["severity"] == "high" and item["target_id"] == "minecraft:stone")
    try:
        service.resolve_review(run_id, normal["review_id"], decision="edit_and_accept", reviewer="tester", reason_code="OTHER", note="safe edit", evidence=["fixture:review"], override={"operations": {"set_geometry": "bad"}})
    except R3Error as exc:
        assert exc.code == "MACHINE_FACT_READ_ONLY"
    else:
        raise AssertionError("machine fact edit was accepted")
    service.close()


def test_review_replay_stops_at_build_release_and_does_not_touch_current(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.70)
    service, run_id, _ = _service(tmp_path, fake)
    _approve_first(service, run_id)
    for _ in range(3):
        service.tick(run_id)
    reviews = service.list_reviews(run_id)
    semantic = next(item for item in reviews if item["target_id"] == "minecraft:stone")
    service.resolve_review(
        run_id,
        semantic["review_id"],
        decision="edit_and_accept",
        reviewer="tester",
        reason_code="CONTEXT_REQUIRED",
        note="manual semantic review",
        evidence=["fixture:semantic"],
        override={"operations": {"set_summary_en": "approved semantic"}, "qualification": "conditional", "warnings": ["needs support"]},
    )
    skip = next(item for item in service.list_reviews(run_id) if item["target_id"] == "minecraft:glass")
    service.resolve_review(run_id, skip["review_id"], decision="skip", reviewer="tester", reason_code="MISSING_TEXTURE", note="fixture skip", evidence=["fixture:render"])
    service.continue_review(run_id)
    service.tick(run_id)
    run = service.get_run(run_id)
    assert run["status"] == "paused"
    assert run["current_stage"] == "BUILD_RELEASE"
    assert run["boundary_event"] == "R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING"
    assert not (tmp_path / "current.json").exists()
    assert service.query_workspace(run_id, "approved semantic")
    with service.worker.open_database(run_id) as database:
        records = [json.loads(row["record_json"]) for row in database.fetchall("SELECT record_json FROM overrides ORDER BY override_id")]
    assert {record["schema_version"] for record in records} >= {"manual-override.v1", "qualification-review.v1", "skip-review.v1"}
    service.close()


def test_retry_ai_creates_unapproved_new_signature(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.70)
    service, run_id, _ = _service(tmp_path, fake)
    _approve_first(service, run_id)
    for _ in range(3):
        service.tick(run_id)
    review = next(item for item in service.list_reviews(run_id) if item["target_id"] == "minecraft:stone")
    service.resolve_review(run_id, review["review_id"], decision="retry_ai", reviewer="tester", reason_code="PROVIDER_FAILURE", note="retry annotation", evidence=["fixture:retry"])
    preview = service.preview_ai_batch(run_id)
    assert preview["approved"] is False
    assert ":retry:" in preview["logical_key"]
    assert preview["input_signature"] != review["review_id"]
    assert fake.calls == 1
    service.close()


def test_configure_run_is_idempotent_before_r3_starts(tmp_path: Path) -> None:
    service, run_id, imported = _service(tmp_path, _FakeProvider())
    first = service.configure_run(imported["import_id"], "26.2", profile_id="default")
    second = service.configure_run(imported["import_id"], "26.2", profile_id="default")
    assert first["run_id"] == second["run_id"] == run_id
    assert first["effective_config_hash"] == second["effective_config_hash"]
    assert first["batches"] == second["batches"]
    assert second["idempotent"] is True
    with service.worker.open_database(run_id) as database:
        count_row = database.fetchone("SELECT COUNT(*) AS count FROM jobs WHERE stage='AI_ANNOTATE'")
        assert count_row is not None and count_row["count"] == first["batch_count"]
    service.close()


def test_d044_pristine_same_run_reconfiguration_preserves_r2_evidence_and_invalidates_plan(tmp_path: Path) -> None:
    fake = _FakeProvider()
    service, run_id, imported = _service(tmp_path, fake)
    try:
        service.tick(run_id)
        old_plan = service.preview_ai_plan(run_id)
        with service.worker.open_database(run_id) as database:
            run_before = database.fetchone("SELECT started_at,effective_config_hash FROM runs WHERE run_id=?", (run_id,))
            r2_stages_before = [
                dict(row)
                for row in database.fetchall(
                    "SELECT stage,status,cursor_json,worker_id,heartbeat_at,started_at,finished_at FROM stage_runs WHERE run_id=? AND ordinal<6 ORDER BY ordinal",
                    (run_id,),
                )
            ]
            features_before = [dict(row) for row in database.fetchall("SELECT * FROM features ORDER BY variant_id")]
            artifacts_before = [dict(row) for row in database.fetchall("SELECT * FROM artifacts ORDER BY artifact_id")]
            reviews_before = [
                dict(row)
                for row in database.fetchall("SELECT * FROM review_tasks ORDER BY review_id")
            ]
        assert run_before is not None and run_before["started_at"] is not None

        current = service.profile_store.load()["default"]
        stages = dict(current.stages)
        stages["offline_annotation"] = StageConfig(batch_size=12, concurrency=5)
        service.save_provider_profile(replace(current, stages=stages))
        saved = service.profile_store.load()["default"]
        assert saved.enabled and saved.capability_status == "verified"
        assert service.profile_store.capabilities("default") is not None

        result = service.configure_run(imported["import_id"], "26.2", profile_id="default")
        assert result["run_id"] == run_id
        assert result["reconfigured"] is True
        assert result["effective_config_hash"] != old_plan["effective_config_hash"]

        with service.worker.open_database(run_id) as database:
            run_after = database.fetchone("SELECT started_at,effective_config_hash,status,current_stage FROM runs WHERE run_id=?", (run_id,))
            r2_stages_after = [
                dict(row)
                for row in database.fetchall(
                    "SELECT stage,status,cursor_json,worker_id,heartbeat_at,started_at,finished_at FROM stage_runs WHERE run_id=? AND ordinal<6 ORDER BY ordinal",
                    (run_id,),
                )
            ]
            features_after = [dict(row) for row in database.fetchall("SELECT * FROM features ORDER BY variant_id")]
            artifacts_after = [dict(row) for row in database.fetchall("SELECT * FROM artifacts ORDER BY artifact_id")]
            reviews_after = [dict(row) for row in database.fetchall("SELECT * FROM review_tasks ORDER BY review_id")]
            jobs = database.fetchall(
                "SELECT status,worker_id,started_at,heartbeat_at,output_hash,error_code,error_message,finished_at,cursor_json FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY logical_key",
                (run_id,),
            )
            audit = database.fetchone(
                "SELECT details_json FROM audit_events WHERE run_id=? AND event_type='R3_RUN_RECONFIGURED' ORDER BY created_at DESC",
                (run_id,),
            )
            provider_requests = database.fetchone("SELECT 1 FROM provider_requests")
            annotations = database.fetchone("SELECT 1 FROM annotations")
        assert run_after is not None
        assert run_after["started_at"] == run_before["started_at"]
        assert run_after["status"] == "pending" and run_after["current_stage"] == "AI_ANNOTATE"
        assert r2_stages_after == r2_stages_before
        assert features_after == features_before
        assert artifacts_after == artifacts_before
        assert reviews_after == reviews_before
        assert jobs and all(
            row["status"] == "pending"
            and row["worker_id"] is None
            and row["started_at"] is None
            and row["heartbeat_at"] is None
            and row["output_hash"] is None
            and row["error_code"] is None
            and row["error_message"] is None
            and row["finished_at"] is None
            and json.loads(row["cursor_json"])["approved"] is False
            and not {"retry_of_job_id", "retry_nonce", "input_invalid", "input_error_code"} & set(json.loads(row["cursor_json"]))
            for row in jobs
        )
        assert audit is not None
        details = json.loads(audit["details_json"])
        assert details["old_effective_config_hash"] == old_plan["effective_config_hash"]
        assert details["new_effective_config_hash"] == result["effective_config_hash"]
        assert details["old_concurrency"] == 1
        assert details["new_concurrency"] == 5
        assert details["job_count"] == len(jobs)
        assert fake.calls == 0 and provider_requests is None and annotations is None

        with pytest.raises(R3Error) as conflict:
            service.approve_ai_plan(run_id, old_plan["plan_hash"])
        assert conflict.value.code == "AI_BATCH_PLAN_CONFLICT"

        repeated = service.configure_run(imported["import_id"], "26.2", profile_id="default")
        assert repeated["run_id"] == run_id and repeated["idempotent"] is True
    finally:
        service.close()


def test_service_profile_save_keeps_concurrency_edit_but_invalidates_model_edit(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path, _FakeProvider())
    try:
        current = service.profile_store.load()["default"]
        stages = dict(current.stages)
        stages["offline_annotation"] = StageConfig(batch_size=12, concurrency=5)
        service.save_provider_profile(replace(current, stages=stages))
        preserved = service.profile_store.load()["default"]
        assert preserved.enabled and preserved.capability_status == "verified"
        assert service.profile_store.capabilities("default") is not None

        service.save_provider_profile(replace(preserved, model_id="model-v2"))
        invalidated = service.profile_store.load()["default"]
        assert not invalidated.enabled and invalidated.capability_status == "unverified"
        assert service.profile_store.capabilities("default") is None
    finally:
        service.close()


def test_d044_preserves_imported_machine_idempotency_review(tmp_path: Path) -> None:
    service, run_id, imported = _service(tmp_path, _FakeProvider())
    try:
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "imported_machine_idempotency",
                        "26.2",
                        "variant",
                        "minecraft:stone",
                        "IDEMPOTENCY_CONFLICT",
                        "high",
                        "open",
                        "Imported machine evidence",
                        json.dumps(["machine:export", "artifact:render"]),
                        "2026-08-16T00:00:00Z",
                    ),
                )
        old_plan = service.preview_ai_plan(run_id)
        current = service.profile_store.load()["default"]
        stages = dict(current.stages)
        stages["offline_annotation"] = StageConfig(batch_size=12, concurrency=5)
        service.save_provider_profile(replace(current, stages=stages))
        result = service.configure_run(imported["import_id"], "26.2", profile_id="default")
        assert result["run_id"] == run_id and result["reconfigured"] is True
        with service.worker.open_database(run_id) as database:
            review = database.fetchone(
                "SELECT reason_code,status,evidence_json FROM review_tasks WHERE review_id=?",
                ("imported_machine_idempotency",),
            )
        assert review is not None
        assert review["reason_code"] == "IDEMPOTENCY_CONFLICT"
        assert review["status"] == "open"
        assert json.loads(review["evidence_json"]) == ["machine:export", "artifact:render"]
        assert result["effective_config_hash"] != old_plan["effective_config_hash"]
    finally:
        service.close()


def test_d044_non_null_boundary_event_rejects_without_mutation(tmp_path: Path) -> None:
    service, run_id, imported = _service(tmp_path, _FakeProvider())
    try:
        _prepare_d044_reconfiguration(service, run_id)
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE runs SET boundary_event=? WHERE run_id=?",
                    ("R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING", run_id),
                )
            before_run = database.fetchone(
                "SELECT effective_config_hash,status,current_stage,boundary_event FROM runs WHERE run_id=?",
                (run_id,),
            )
            before_jobs = [
                dict(row)
                for row in database.fetchall(
                    "SELECT job_id,status,cursor_json FROM jobs WHERE run_id=? ORDER BY job_id",
                    (run_id,),
                )
            ]
            before_audits = [
                dict(row)
                for row in database.fetchall(
                    "SELECT event_id,event_type,details_json FROM audit_events WHERE run_id=? ORDER BY event_id",
                    (run_id,),
                )
            ]
        with pytest.raises(R3Error) as conflict:
            service.configure_run(imported["import_id"], "26.2", profile_id="default")
        assert conflict.value.code == "RUN_STATE_CONFLICT"
        with service.worker.open_database(run_id) as database:
            after_run = database.fetchone(
                "SELECT effective_config_hash,status,current_stage,boundary_event FROM runs WHERE run_id=?",
                (run_id,),
            )
            after_jobs = [
                dict(row)
                for row in database.fetchall(
                    "SELECT job_id,status,cursor_json FROM jobs WHERE run_id=? ORDER BY job_id",
                    (run_id,),
                )
            ]
            after_audits = [
                dict(row)
                for row in database.fetchall(
                    "SELECT event_id,event_type,details_json FROM audit_events WHERE run_id=? ORDER BY event_id",
                    (run_id,),
                )
            ]
        assert after_run == before_run
        assert after_jobs == before_jobs
        assert after_audits == before_audits
    finally:
        service.close()


@pytest.mark.parametrize(
    "dirty_kind",
    ["provider_request", "annotation", "ai_artifact", "ai_review", "ai_audit", "dirty_job", "live_future"],
)
def test_d044_pristine_reconfiguration_dirty_gates_fail_closed_without_mutation(
    tmp_path: Path,
    dirty_kind: str,
    monkeypatch,
) -> None:
    fake = _FakeProvider()
    service, run_id, imported = _service(tmp_path, fake)
    try:
        old_plan, job_id = _prepare_d044_reconfiguration(service, run_id)
        if dirty_kind == "live_future":
            monkeypatch.setattr(service.worker, "has_live_ai_futures", lambda _run_id: True)
        else:
            with service.worker.open_database(run_id) as database:
                with database.transaction() as connection:
                    job = connection.execute("SELECT input_signature FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                    assert job is not None
                    if dirty_kind == "provider_request":
                        connection.execute(
                            "INSERT INTO provider_requests(request_id,profile_id,stage,wire_schema_id,attempt,cache_key,input_sha256,validated_artifact_sha256,error_code,error_class,envelope_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("dirty_request", "default", "offline_annotation", "annotation-batch-output.v1", 1, "dirty-cache", job["input_signature"], None, None, None, "{}", "pending", "2026-08-16T00:00:00Z"),
                        )
                    elif dirty_kind == "annotation":
                        connection.execute(
                            "INSERT INTO annotations(annotation_id,subject_type,subject_id,minecraft_version,record_json) VALUES (?,?,?,?,?)",
                            ("dirty_annotation", "variant", "minecraft:stone", "26.2", "{}"),
                        )
                    elif dirty_kind == "ai_artifact":
                        connection.execute(
                            "INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)",
                            ("dirty_artifact", job_id, "ai_annotation", "generated/dirty.json", "dirty-hash", "{}"),
                        )
                    elif dirty_kind == "ai_review":
                        connection.execute(
                            "INSERT INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            ("dirty_review", "26.2", "variant", "minecraft:stone", "PROVIDER_FAILURE", "high", "open", "dirty", json.dumps([f"job:{job_id}"]), "2026-08-16T00:00:00Z"),
                        )
                    elif dirty_kind == "ai_audit":
                        connection.execute(
                            "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                            ("dirty_audit", "AI_BATCH_APPROVED", run_id, job_id, "{}", "2026-08-16T00:00:00Z"),
                        )
                    else:
                        cursor = json.loads(connection.execute("SELECT cursor_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()["cursor_json"])
                        cursor["input_invalid"] = True
                        connection.execute("UPDATE jobs SET cursor_json=? WHERE job_id=?", (json.dumps(cursor, sort_keys=True), job_id))
        with service.worker.open_database(run_id) as database:
            before = database.fetchone("SELECT config_snapshot_json,effective_config_hash,status,current_stage,started_at FROM runs WHERE run_id=?", (run_id,))
            job_count_row = database.fetchone("SELECT COUNT(*) AS count FROM jobs WHERE run_id=?", (run_id,))
            reconfigured_row = database.fetchone("SELECT COUNT(*) AS count FROM audit_events WHERE run_id=? AND event_type='R3_RUN_RECONFIGURED'", (run_id,))
            assert job_count_row is not None and reconfigured_row is not None
            job_count_before = job_count_row["count"]
            reconfigured_before = reconfigured_row["count"]
        with pytest.raises(R3Error) as conflict:
            service.configure_run(imported["import_id"], "26.2", profile_id="default")
        assert conflict.value.code == "RUN_STATE_CONFLICT"
        with service.worker.open_database(run_id) as database:
            after = database.fetchone("SELECT config_snapshot_json,effective_config_hash,status,current_stage,started_at FROM runs WHERE run_id=?", (run_id,))
            job_count_row = database.fetchone("SELECT COUNT(*) AS count FROM jobs WHERE run_id=?", (run_id,))
            reconfigured_row = database.fetchone("SELECT COUNT(*) AS count FROM audit_events WHERE run_id=? AND event_type='R3_RUN_RECONFIGURED'", (run_id,))
            assert job_count_row is not None and reconfigured_row is not None
            job_count_after = job_count_row["count"]
            reconfigured_after = reconfigured_row["count"]
        assert after == before
        assert job_count_after == job_count_before
        assert reconfigured_after == reconfigured_before
        assert fake.calls == 0
    finally:
        service.close()


def test_cancel_batch_creates_one_high_review_per_cursor_variant(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    logical_key = service.preview_ai_batch(run_id)["logical_key"]
    cancelled = service.cancel_ai_batch(run_id, logical_key, reason="operator cancelled")
    assert cancelled["status"] == "skipped"
    assert cancelled["variant_ids"]
    assert all(item.startswith("minecraft:") for item in cancelled["variant_ids"])
    assert len(cancelled["review_ids"]) == len(cancelled["variant_ids"])
    reviews = service.list_reviews(run_id)
    cancelled_reviews = [item for item in reviews if item["reason_code"] == "AI_BATCH_CANCELLED"]
    assert {item["target_id"] for item in cancelled_reviews} == set(cancelled["variant_ids"])
    assert all(item["severity"] == "high" for item in cancelled_reviews)
    assert logical_key not in {item["target_id"] for item in cancelled_reviews}
    assert service.get_run(run_id)["status"] == "pending"
    service.close()


def test_approval_rebinds_mutated_payload_without_provider_call(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.90)
    service, run_id, _ = _service(tmp_path, fake)
    preview = service.preview_ai_batch(run_id)
    old_signature = preview["input_signature"]
    with service.worker.open_database(run_id) as database:
        image_path = database.path.parent / "renders" / "minecraft" / "stone" / "preview.png"
    image_path.write_bytes(encode_rgba_png(512, 512, b"\x10\x20\x30\xff" * (512 * 512)))
    try:
        service.approve_ai_batch(run_id, preview["logical_key"], old_signature)
    except R3Error as exc:
        assert exc.code == "AI_BATCH_INPUT_CHANGED"
    else:
        raise AssertionError("mutated payload was approved")
    assert fake.calls == 0
    rebound = service.preview_ai_batch(run_id, preview["logical_key"])
    assert rebound["approved"] is False
    assert rebound["input_signature"] != old_signature
    service.approve_ai_batch(run_id, rebound["logical_key"], rebound["input_signature"])
    service.tick(run_id)
    assert fake.calls == 1
    service.close()


def test_cancelled_batch_is_not_previewable_and_reaches_human_review(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    logical_key = service.preview_ai_batch(run_id)["logical_key"]
    service.cancel_ai_batch(run_id, logical_key)
    try:
        service.preview_ai_batch(run_id, logical_key)
    except R3Error as exc:
        assert exc.code == "AI_BATCH_NOT_FOUND"
    else:
        raise AssertionError("terminal skipped batch was previewable")
    for _ in range(4):
        service.tick(run_id)
    run = service.get_run(run_id)
    assert run["current_stage"] == "HUMAN_REVIEW"
    assert run["status"] == "needs_review"
    service.close()


def test_sampling_does_not_reduce_annotation_cohort_and_excludes_excluded(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.90)
    service, run_id, _ = _service(tmp_path, fake)
    _set_sample_rate(service, run_id, 0)
    with service.worker.open_database(run_id) as database:
        jobs = database.fetchall("SELECT cursor_json FROM jobs WHERE stage='AI_ANNOTATE'")
        assert jobs
        assert all("minecraft:glass" not in (json.loads(row["cursor_json"]).get("variant_ids", [])) for row in jobs)
    _approve_first(service, run_id)
    service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        annotation_count = database.fetchone("SELECT COUNT(*) AS count FROM annotations")
        assert annotation_count is not None and annotation_count["count"] == 1
    assert not any(item["reason_code"] == "SAMPLED_QUALITY_REVIEW" for item in service.list_reviews(run_id))
    service.close()


def test_partial_sampling_creates_bound_quality_review(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.90)
    service, run_id, _ = _service(tmp_path, fake)
    digest = int(hashlib.sha256(f"{run_id}\0minecraft:stone".encode()).hexdigest()[:8], 16) % 10000
    sample_rate = 100 if digest >= 9900 else min(99, digest // 100 + 1)
    _set_sample_rate(service, run_id, sample_rate)
    _approve_first(service, run_id)
    service.tick(run_id)
    sampled = [item for item in service.list_reviews(run_id) if item["reason_code"] == "SAMPLED_QUALITY_REVIEW"]
    assert sampled
    with service.worker.open_database(run_id) as database:
        task_row = database.fetchone("SELECT evidence_json FROM review_tasks WHERE review_id=?", (sampled[0]["review_id"],))
        assert task_row is not None
        evidence = json.loads(task_row["evidence_json"])
    assert any(str(item).startswith("annotation:") for item in evidence)
    service.close()


def test_provider_request_ids_include_run_identity(tmp_path: Path) -> None:
    first_service, first_run, _ = _service(tmp_path / "first", _FakeProvider(confidence=0.90))
    second_service, second_run, _ = _service(tmp_path / "second", _FakeProvider(confidence=0.90))
    try:
        for service, run_id in ((first_service, first_run), (second_service, second_run)):
            _set_sample_rate(service, run_id, 0)
            _approve_first(service, run_id)
            service.tick(run_id)
        with first_service.worker.open_database(first_run) as database:
            first_request = database.fetchone("SELECT request_id FROM provider_requests")
        with second_service.worker.open_database(second_run) as database:
            second_request = database.fetchone("SELECT request_id FROM provider_requests")
        assert first_request is not None and second_request is not None
        assert first_request["request_id"] != second_request["request_id"]
    finally:
        first_service.close()
        second_service.close()


def test_source_repair_review_remains_open_and_blocking(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.70))
    _approve_first(service, run_id)
    for _ in range(3):
        service.tick(run_id)
    review = next(item for item in service.list_reviews(run_id) if item["target_id"] == "minecraft:stone")
    result = service.resolve_review(run_id, review["review_id"], decision="request_reexport", reviewer="tester", reason_code="OTHER", note="exporter repair", evidence=["fixture:repair"])
    assert result["status"] == "open"
    assert any(item["review_id"] == review["review_id"] for item in service.list_reviews(run_id))
    try:
        service.continue_review(run_id)
    except R3Error as exc:
        assert exc.code == "REVIEW_TASKS_OPEN"
    else:
        raise AssertionError("source repair review released the run")
    service.close()


def test_human_review_closure_rejects_unverified_semantics(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.90))
    _set_sample_rate(service, run_id, 0)
    _approve_first(service, run_id)
    service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            row = connection.execute("SELECT annotation_id,record_json FROM annotations LIMIT 1").fetchone()
            assert row is not None
            record = json.loads(row["record_json"])
            record["source"]["verified"] = False
            connection.execute("UPDATE annotations SET record_json=? WHERE annotation_id=?", (json.dumps(record, sort_keys=True), row["annotation_id"]))
            connection.execute("DELETE FROM review_tasks")
    service.tick(run_id)
    service.tick(run_id)
    assert service.get_run(run_id)["status"] == "needs_review"
    assert any(item["reason_code"] == "MISSING_VERIFIED_SEMANTIC" for item in service.list_reviews(run_id))
    with service.worker.open_database(run_id) as database:
        task = database.fetchone("SELECT evidence_json FROM review_tasks WHERE reason_code='MISSING_VERIFIED_SEMANTIC' AND status='open'")
        assert task is not None
        assert any(str(item).startswith("annotation:") for item in json.loads(task["evidence_json"]))
    service.close()


def test_human_review_closure_rejects_missing_skip_and_excluded_audit(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.90))
    _set_sample_rate(service, run_id, 0)
    _approve_first(service, run_id)
    service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            connection.execute("DELETE FROM review_tasks")
            row = connection.execute("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'").fetchone()
            assert row is not None
            record = json.loads(row["record_json"])
            record["candidate_qualification"] = "excluded"
            record["qualification_review_refs"] = []
            connection.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(record, sort_keys=True),))
    service.tick(run_id)
    service.tick(run_id)
    assert service.get_run(run_id)["status"] == "needs_review"
    reasons = {item["reason_code"] for item in service.list_reviews(run_id)}
    assert "QUALIFICATION_REVIEW_MISSING" in reasons or "SKIP_REVIEW_MISSING" in reasons
    service.close()


def test_human_review_closure_rejects_fts_failure(tmp_path: Path, monkeypatch) -> None:
    from blockpedia.search import WorkspaceQueryService

    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.90))
    _set_sample_rate(service, run_id, 0)
    _approve_first(service, run_id)
    monkeypatch.setattr(WorkspaceQueryService, "rebuild_index", lambda _self: (_ for _ in ()).throw(RuntimeError("fts")))
    service.tick(run_id)
    service.tick(run_id)
    service.tick(run_id)
    assert service.get_run(run_id)["status"] == "needs_review"
    assert any(item["reason_code"] == "FTS_BUILD_FAILED" for item in service.list_reviews(run_id))
    service.close()


def test_cancelled_target_can_retry_as_single_unapproved_batch(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.90)
    service, run_id, _ = _service(tmp_path, fake)
    logical_key = service.preview_ai_batch(run_id)["logical_key"]
    service.cancel_ai_batch(run_id, logical_key)
    for _ in range(4):
        service.tick(run_id)
    cancelled = [item for item in service.list_reviews(run_id) if item["reason_code"] == "AI_BATCH_CANCELLED"]
    assert len(cancelled) == 1
    target_id = cancelled[0]["target_id"]
    retry = service.resolve_review(
        run_id,
        cancelled[0]["review_id"],
        decision="retry_ai",
        reviewer="tester",
        reason_code="OTHER",
        note="retry cancelled target",
        evidence=["fixture:cancel-retry"],
    )
    assert retry["status"] == "resolved"
    preview = service.preview_ai_batch(run_id)
    assert preview["approved"] is False
    assert [tile["variant_id"] for tile in preview["tiles"]] == [target_id]
    assert fake.calls == 0
    assert any(item["reason_code"] == "AI_BATCH_CANCELLED" for item in service.list_reviews(run_id, status="resolved"))
    service.close()


def test_input_failure_pauses_without_provider_and_allows_reapproval(tmp_path: Path) -> None:
    fake = _FakeProvider(confidence=0.90)
    service, run_id, _ = _service(tmp_path, fake)
    preview = service.preview_ai_batch(run_id)
    old_token = preview["input_signature"]
    with service.worker.open_database(run_id) as database:
        image_path = database.path.parent / "renders" / "minecraft" / "stone" / "preview.png"
        repaired_image = encode_rgba_png(512, 512, b"\x10\x20\x30\xff" * (512 * 512))
    service.approve_ai_batch(run_id, preview["logical_key"], preview["input_signature"])
    image_path.unlink()
    try:
        service.tick(run_id)
    finally:
        image_path.write_bytes(repaired_image)
    assert fake.calls == 0
    run = service.get_run(run_id)
    assert run["status"] == "paused" and run["current_stage"] == "AI_ANNOTATE"
    with service.worker.open_database(run_id) as database:
        job = database.fetchone("SELECT status,error_code,cursor_json FROM jobs WHERE logical_key=?", (preview["logical_key"],))
        assert job is not None and job["status"] == "needs_review" and job["error_code"] == "AI_BATCH_INPUT_INVALID"
        assert json.loads(job["cursor_json"])["input_invalid"] is True
    repaired = service.preview_ai_batch(run_id, preview["logical_key"])
    assert repaired["input_signature"] != old_token
    assert repaired["approved"] is False
    for token in (old_token, "sha256:" + "0" * 64):
        try:
            service.approve_ai_batch(run_id, repaired["logical_key"], token)
        except R3Error as exc:
            assert exc.code == "AI_BATCH_INPUT_CHANGED"
        else:
            raise AssertionError("stale or arbitrary approval token was accepted")
    service.approve_ai_batch(run_id, repaired["logical_key"], repaired["input_signature"])
    with service.worker.open_database(run_id) as database:
        row = database.fetchone("SELECT input_signature,cursor_json FROM jobs WHERE logical_key=?", (repaired["logical_key"],))
        assert row is not None and row["input_signature"] == repaired["input_signature"]
        cursor = json.loads(row["cursor_json"])
        assert cursor["payload_signature"] == repaired["input_signature"]
        assert cursor["input_hash"] == repaired["input_signature"]
        assert "input_invalid" not in cursor and "input_error_code" not in cursor
    service.tick(run_id)
    assert fake.calls == 1
    service.close()


def test_provider_root_review_does_not_create_generic_missing_semantic(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(fail=True))
    _approve_first(service, run_id)
    for _ in range(3):
        service.tick(run_id)
    reviews = service.list_reviews(run_id)
    assert any(item["reason_code"] == "PROVIDER_FAILURE" for item in reviews)
    assert not any(item["reason_code"] == "MISSING_SEMANTIC" and item["target_id"] == "minecraft:stone" for item in reviews)
    service.close()


def test_full_manual_semantics_can_close_without_annotation(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.90))
    _set_sample_rate(service, run_id, 0)
    _approve_first(service, run_id)
    service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            row = connection.execute("SELECT annotation_id FROM annotations LIMIT 1").fetchone()
            assert row is not None
            connection.execute("DELETE FROM annotations WHERE annotation_id=?", (row["annotation_id"],))
            variant_row = connection.execute("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'").fetchone()
            assert variant_row is not None
            variant = json.loads(variant_row["record_json"])
            variant["annotation_refs"] = []
            connection.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(variant, sort_keys=True),))
            connection.execute("DELETE FROM review_tasks")
    service.tick(run_id)
    service.tick(run_id)
    review = next(item for item in service.list_reviews(run_id) if item["reason_code"] == "MISSING_SEMANTIC")
    full_override = {
        "operations": {
            "add_synonyms_zh": ["石头"],
            "add_synonyms_en": ["stone"],
            "set_summary_zh": "人工石头方块",
            "set_summary_en": "A manually reviewed stone block.",
            "add_color_terms": ["gray"],
            "add_shape_terms": ["cube"],
            "add_material_impressions": ["stone"],
            "add_building_roles": ["wall"],
            "add_style_tags": [],
            "add_avoid_for": [],
            "set_confidence": 1.0,
        }
    }
    try:
        service.resolve_review(run_id, review["review_id"], decision="edit_and_accept", reviewer="tester", reason_code="OTHER", note="partial human semantics", evidence=["fixture:partial"], override={"operations": {"set_summary_en": "only one field"}})
    except R3Error as exc:
        assert exc.code == "OVERRIDE_INVALID"
    else:
        raise AssertionError("partial manual semantics were accepted")
    with service.worker.open_database(run_id) as database:
        assert database.fetchone("SELECT 1 FROM overrides") is None
    result = service.resolve_review(run_id, review["review_id"], decision="edit_and_accept", reviewer="tester", reason_code="OTHER", note="complete human semantics", evidence=["fixture:manual"], override=full_override)
    assert result["status"] == "resolved"
    with service.worker.open_database(run_id) as database:
        assert database.fetchone("SELECT 1 FROM annotations") is None
    service.continue_review(run_id)
    service.tick(run_id)
    pending_skip = [item for item in service.list_reviews(run_id) if item["reason_code"] in {"MISSING_TEXTURE", "SKIP_REVIEW_MISSING"}]
    if pending_skip:
        service.resolve_review(run_id, pending_skip[0]["review_id"], decision="skip", reviewer="tester", reason_code="MISSING_TEXTURE", note="fixture failure skip", evidence=["fixture:render"])
        service.continue_review(run_id)
        service.tick(run_id)
    assert service.get_run(run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING"
    assert service.query_workspace(run_id, "人工石头")
    service.close()


def test_excluded_acceptance_needs_only_qualification_review(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.90))
    _set_sample_rate(service, run_id, 0)
    _approve_first(service, run_id)
    service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            row = connection.execute("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'").fetchone()
            assert row is not None
            record = json.loads(row["record_json"])
            record["candidate_qualification"] = "excluded"
            record["qualification_review_refs"] = []
            connection.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(record, sort_keys=True),))
            connection.execute("DELETE FROM review_tasks")
    service.tick(run_id)
    service.tick(run_id)
    review = next(item for item in service.list_reviews(run_id) if item["reason_code"] == "QUALIFICATION_REVIEW_MISSING")
    result = service.resolve_review(run_id, review["review_id"], decision="edit_and_accept", reviewer="tester", reason_code="NOT_A_BUILDING_CANDIDATE", note="excluded by human review", evidence=["fixture:qualification"], override={"qualification": "excluded", "warnings": []})
    assert result["status"] == "resolved"
    service.close()


def test_fts_accept_rebuilds_atomically_and_recovers(tmp_path: Path, monkeypatch) -> None:
    from blockpedia.search import WorkspaceQueryService

    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.90))
    _set_sample_rate(service, run_id, 0)
    _approve_first(service, run_id)
    original = WorkspaceQueryService.rebuild_index
    monkeypatch.setattr(WorkspaceQueryService, "rebuild_index", lambda _self: (_ for _ in ()).throw(RuntimeError("fts")))
    for _ in range(3):
        service.tick(run_id)
    fts = next(item for item in service.list_reviews(run_id) if item["reason_code"] == "FTS_BUILD_FAILED")
    monkeypatch.setattr(WorkspaceQueryService, "rebuild_index", original)
    accepted = service.resolve_review(run_id, fts["review_id"], decision="accept", reviewer="tester", reason_code="OTHER", note="retry FTS check", evidence=["fixture:fts"])
    assert accepted["status"] == "resolved"
    pending_skip = next(item for item in service.list_reviews(run_id) if item["reason_code"] == "MISSING_TEXTURE")
    service.resolve_review(run_id, pending_skip["review_id"], decision="skip", reviewer="tester", reason_code="MISSING_TEXTURE", note="fixture failure skip", evidence=["fixture:render"])
    continued = service.continue_review(run_id)
    assert continued["status"] == "pending" and continued["current_stage"] == "HUMAN_REVIEW"
    with service.worker.open_database(run_id) as database:
        assert database.fetchone("SELECT 1 FROM review_tasks WHERE reason_code='FTS_BUILD_FAILED' AND status='open'") is None
    service.close()


def test_d040_worker_uses_frozen_profile_after_global_profile_changes(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    try:
        with service.worker.open_database(run_id) as database:
            frozen = service.worker._run_profile(database, run_id)
        mutable = ProviderProfile(
            profile_id="default",
            model_id="mutable-model",
            adapter="openai_chat_completions",
            base_url="http://127.0.0.1:9999/v1",
            enabled=False,
            capability_status="unverified",
        )
        service.profile_store.save(mutable)
        service.worker.provider_factory = None
        provider = service.worker._new_provider(frozen)
        try:
            assert provider.profile.model_id == "fixture-model"
            assert provider.profile.adapter == frozen.adapter
            assert provider.profile.base_url_stable_id == frozen.base_url_stable_id
            assert provider._effective_profile() == frozen
        finally:
            provider.close()
    finally:
        service.close()


def test_d040_plan_confirm_is_toctou_safe_and_idempotent(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    try:
        plan = service.preview_ai_plan(run_id)
        assert plan["count"] == 1
        assert not {"prompt", "prompt_text", "contact_sheet_png", "contact_sheet_bytes"} & set(plan)
        assert {"tile_ids", "variant_ids"} <= set(plan["jobs"][0])
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT job_id,cursor_json FROM jobs WHERE stage='AI_ANNOTATE'").fetchone()
                assert row is not None
                changed_signature = "sha256:" + "1" * 64
                cursor = json.loads(row["cursor_json"])
                cursor["payload_signature"] = changed_signature
                cursor["input_hash"] = changed_signature
                connection.execute(
                    "UPDATE jobs SET input_signature=?,cursor_json=? WHERE job_id=?",
                    (changed_signature, json.dumps(cursor, sort_keys=True), row["job_id"]),
                )
        with pytest.raises(R3Error) as conflict:
            service.approve_ai_plan(run_id, plan["plan_hash"])
        assert conflict.value.code == "AI_BATCH_PLAN_CONFLICT"
        with service.worker.open_database(run_id) as database:
            cursor = database.fetchone("SELECT cursor_json FROM jobs WHERE stage='AI_ANNOTATE'")
            assert cursor is not None and json.loads(cursor["cursor_json"])["approved"] is False
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT job_id,cursor_json FROM jobs WHERE stage='AI_ANNOTATE'").fetchone()
                assert row is not None
                cursor = json.loads(row["cursor_json"])
                original_signature = plan["jobs"][0]["input_signature"]
                cursor["payload_signature"] = original_signature
                cursor["input_hash"] = original_signature
                connection.execute(
                    "UPDATE jobs SET input_signature=?,cursor_json=? WHERE job_id=?",
                    (original_signature, json.dumps(cursor, sort_keys=True), row["job_id"]),
                )
        current = service.preview_ai_plan(run_id)
        service.approve_ai_batch(run_id, current["jobs"][0]["logical_key"], current["jobs"][0]["input_signature"])
        approved = service.approve_ai_plan(run_id, current["plan_hash"])
        repeated = service.approve_ai_plan(run_id, current["plan_hash"])
        assert approved["approved"] is True and approved["idempotent"] is False
        assert repeated["approved"] is True and repeated["idempotent"] is True
        with service.worker.open_database(run_id) as database:
            count = database.fetchone("SELECT COUNT(*) AS count FROM audit_events WHERE event_type='AI_BATCH_PLAN_APPROVED'")
            assert count is not None and count["count"] == 1
    finally:
        service.close()


def test_d041_aggregate_plan_uses_persisted_identity_for_100_plus_jobs(tmp_path: Path, monkeypatch) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    calls = {"payload": 0, "signature": 0}

    def forbidden_payload(*_args, **_kwargs):
        calls["payload"] += 1
        raise AssertionError("aggregate plan rebuilt a payload")

    def forbidden_signature(*_args, **_kwargs):
        calls["signature"] += 1
        raise AssertionError("aggregate plan rebuilt a signature")

    monkeypatch.setattr("blockpedia.worker._build_annotation_payload", forbidden_payload)
    monkeypatch.setattr("blockpedia.worker._batch_input_signature", forbidden_signature)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                source = connection.execute("SELECT * FROM jobs WHERE stage='AI_ANNOTATE' LIMIT 1").fetchone()
                assert source is not None
                for index in range(1, 120):
                    connection.execute(
                        "INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (f"job_bulk_{index:03d}", run_id, "AI_ANNOTATE", f"ai_batch_{index:04d}", source["input_signature"], "pending", 0, source["cursor_json"], source["created_at"]),
                    )
        plan = service.preview_ai_plan(run_id)
        approved = service.approve_ai_plan(run_id, plan["plan_hash"])
        assert plan["count"] == 120
        assert approved["count"] == 120
        assert calls == {"payload": 0, "signature": 0}
    finally:
        service.close()


def test_d041_final_pre_send_rebuild_rejects_changed_source_after_aggregate_approval(tmp_path: Path) -> None:
    fake = _FakeProvider()
    service, run_id, _ = _service(tmp_path, fake)
    try:
        plan = service.preview_ai_plan(run_id)
        service.approve_ai_plan(run_id, plan["plan_hash"])
        with service.worker.open_database(run_id) as database:
            image = database.path.parent / "renders" / "minecraft" / "stone" / "preview.png"
            image.write_bytes(encode_rgba_png(512, 512, b"\x30\x40\x50\xff" * (512 * 512)))
        service.tick(run_id)
        assert fake.calls == 0
        assert service.get_run(run_id)["status"] == "paused"
        with service.worker.open_database(run_id) as database:
            job = database.fetchone("SELECT status,cursor_json FROM jobs WHERE stage='AI_ANNOTATE'")
            assert job is not None and job["status"] == "pending"
            assert json.loads(job["cursor_json"])["approved"] is False
    finally:
        service.close()


def test_d041_persisted_cursor_identity_mismatch_fails_closed(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    try:
        plan = service.preview_ai_plan(run_id)
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT job_id,cursor_json FROM jobs WHERE stage='AI_ANNOTATE'").fetchone()
                assert row is not None
                cursor = json.loads(row["cursor_json"])
                cursor["payload_signature"] = "sha256:" + "2" * 64
                connection.execute("UPDATE jobs SET cursor_json=? WHERE job_id=?", (json.dumps(cursor, sort_keys=True), row["job_id"]))
        with pytest.raises(R3Error) as preview_error:
            service.preview_ai_plan(run_id)
        assert preview_error.value.code == "AI_BATCH_INPUT_INVALID"
        with pytest.raises(R3Error) as confirm_error:
            service.approve_ai_plan(run_id, plan["plan_hash"])
        assert confirm_error.value.code == "AI_BATCH_PLAN_CONFLICT"
        with service.worker.open_database(run_id) as database:
            row = database.fetchone("SELECT cursor_json FROM jobs WHERE stage='AI_ANNOTATE'")
            assert row is not None and json.loads(row["cursor_json"])["approved"] is False
    finally:
        service.close()


def test_d040_sequential_item_failure_continues_but_fatal_stops(tmp_path: Path) -> None:
    item_local = _ScriptedProvider(["PROVIDER_SERVER_ERROR", None])
    service, run_id, _ = _service(tmp_path / "item", item_local)
    try:
        _duplicate_ai_job(service, run_id)
        plan = service.preview_ai_plan(run_id)
        assert plan["count"] == 2
        service.approve_ai_plan(run_id, plan["plan_hash"])
        service.tick(run_id)
        service.tick(run_id)
        assert item_local.calls == 2
        with service.worker.open_database(run_id) as database:
            rows = database.fetchall("SELECT status,error_code FROM jobs WHERE stage='AI_ANNOTATE' ORDER BY logical_key")
            assert rows[0]["status"] == "needs_review" and rows[0]["error_code"] == "PROVIDER_SERVER_ERROR"
            assert rows[1]["status"] == "succeeded"
    finally:
        service.close()

    fatal = _ScriptedProvider(["PROVIDER_AUTH_FAILED", None])
    service, run_id, _ = _service(tmp_path / "fatal", fatal)
    try:
        _duplicate_ai_job(service, run_id)
        plan = service.preview_ai_plan(run_id)
        service.approve_ai_plan(run_id, plan["plan_hash"])
        service.tick(run_id)
        assert fatal.calls == 1
        assert service.get_run(run_id)["status"] == "failed"
        with service.worker.open_database(run_id) as database:
            stage = database.fetchone("SELECT status FROM stage_runs WHERE run_id=? AND stage='AI_ANNOTATE'", (run_id,))
            jobs = database.fetchall("SELECT job_id,status,error_code FROM jobs WHERE stage='AI_ANNOTATE' ORDER BY logical_key")
            request = database.fetchone("SELECT error_code,attempt FROM provider_requests")
            reviews = database.fetchall("SELECT evidence_json FROM review_tasks WHERE reason_code='PROVIDER_FAILURE'")
            assert stage is not None and stage["status"] == "failed"
            assert jobs[0]["status"] == "failed" and jobs[0]["error_code"] == "PROVIDER_AUTH_FAILED"
            assert jobs[1]["status"] == "pending"
            assert request is not None and request["error_code"] == "PROVIDER_AUTH_FAILED" and request["attempt"] == 1
            assert reviews and all(f"job:{jobs[0]['job_id']}" in json.loads(row["evidence_json"]) for row in reviews)
        with pytest.raises(R3Error) as row_retry:
            service.retry_provider_job(run_id, jobs[0]["job_id"])
        assert row_retry.value.code == "PROVIDER_RETRY_NOT_ELIGIBLE"
        assert service.preview_provider_retry_wave(run_id)["count"] == 0
        assert service.bulk_retry_provider_jobs(run_id)["count"] == 0
    finally:
        service.close()


def test_d040_provider_retry_leaf_generation_siblings_bulk_and_generic_guard(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(fail=True))
    try:
        _approve_first(service, run_id)
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                source = connection.execute("SELECT * FROM jobs WHERE stage='AI_ANNOTATE'").fetchone()
                assert source is not None
                cursor = json.loads(source["cursor_json"])
                connection.execute("UPDATE jobs SET status='failed',error_code='PROVIDER_SERVER_ERROR' WHERE job_id=?", (source["job_id"],))
                for suffix in ("sibling_a", "sibling_b"):
                    service.worker.create_review_task(connection, "variant", cursor["variant_ids"][0], "PROVIDER_FAILURE", "high", "provider", [f"job:{source['job_id']}"], dedupe_key=suffix, reopen=True)
                source_id = source["job_id"]
        first = service.retry_provider_job(run_id, source_id)
        second = service.retry_provider_job(run_id, source_id)
        assert first["job_id"] == second["job_id"]
        with service.worker.open_database(run_id) as database:
            child = database.fetchone("SELECT cursor_json FROM jobs WHERE job_id=?", (first["job_id"],))
            siblings = database.fetchall("SELECT status,resolved_at FROM review_tasks WHERE reason_code='PROVIDER_FAILURE'")
            assert child is not None and json.loads(child["cursor_json"])["retry_of_job_id"] == source_id
            assert siblings and all(row["status"] == "resolved" and row["resolved_at"] for row in siblings)
            database.execute("UPDATE jobs SET status='failed',error_code='PROVIDER_SERVER_ERROR' WHERE job_id=?", (first["job_id"],))
        wave = service.bulk_retry_provider_jobs(run_id)
        repeated = service.bulk_retry_provider_jobs(run_id)
        assert wave["count"] == 1 and repeated["count"] == 0
        confirmed = service.confirm_provider_retry_wave(run_id, wave["wave_hash"])
        assert confirmed["approved"] is True and confirmed["idempotent"] is False
        with service.worker.open_database(run_id) as database:
            connection = database.connection
            connection.execute("UPDATE jobs SET status='failed',error_code='PROVIDER_SERVER_ERROR' WHERE job_id=?", (wave["jobs"][0]["job_id"],))
        next_generation = service.retry_provider_job(run_id, wave["jobs"][0]["job_id"])
        assert next_generation["job_id"] != wave["jobs"][0]["job_id"]
        with service.worker.open_database(run_id) as database:
            child_count = database.fetchone("SELECT COUNT(*) AS count FROM jobs WHERE stage='AI_ANNOTATE' AND cursor_json LIKE '%retry_of_job_id%'")
            assert child_count is not None and child_count["count"] == 3
            connection = database.connection
            connection.execute("UPDATE jobs SET status='failed',error_code='PROVIDER_AUTH_FAILED' WHERE job_id=?", (source_id,))
            connection.execute("UPDATE stage_runs SET status='failed' WHERE run_id=? AND stage='AI_ANNOTATE'", (run_id,))
            connection.execute("UPDATE runs SET status='failed' WHERE run_id=?", (run_id,))
        with pytest.raises(R3Error) as generic:
            service.retry_failed(run_id)
        assert generic.value.code == "PROVIDER_RETRY_REQUIRED"
    finally:
        service.close()


def test_d040_needs_review_drains_and_human_review_owns_closure(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=0.70))
    try:
        plan = service.preview_ai_plan(run_id)
        service.approve_ai_plan(run_id, plan["plan_hash"])
        service.tick(run_id)
        service.tick(run_id)
        service.tick(run_id)
        run = service.get_run(run_id)
        assert run["current_stage"] == "HUMAN_REVIEW"
        assert run["status"] == "needs_review"
        assert any(item["reason_code"] == "LOW_CONFIDENCE" for item in service.list_reviews(run_id))
    finally:
        service.close()


def test_d040_retry_source_rejects_low_confidence_and_null_error(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    try:
        with service.worker.open_database(run_id) as database:
            job = database.fetchone("SELECT job_id FROM jobs WHERE stage='AI_ANNOTATE' LIMIT 1")
            assert job is not None
            database.execute("UPDATE jobs SET status='needs_review',error_code=NULL WHERE job_id=?", (job["job_id"],))
            source_id = job["job_id"]
        with pytest.raises(R3Error) as null_error:
            service.retry_provider_job(run_id, source_id)
        assert null_error.value.code == "PROVIDER_RETRY_NOT_ELIGIBLE"
        with service.worker.open_database(run_id) as database:
            database.execute("UPDATE jobs SET error_code='LOW_CONFIDENCE' WHERE job_id=?", (source_id,))
        with pytest.raises(R3Error) as low_confidence:
            service.retry_provider_job(run_id, source_id)
        assert low_confidence.value.code == "PROVIDER_RETRY_NOT_ELIGIBLE"
    finally:
        service.close()


def test_d040_retry_wave_includes_needs_review_provider_error_but_not_null_error(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    try:
        _duplicate_ai_job(service, run_id)
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                rows = connection.execute("SELECT job_id FROM jobs WHERE stage='AI_ANNOTATE' ORDER BY logical_key").fetchall()
                assert len(rows) == 2
                connection.execute("UPDATE jobs SET status='needs_review',error_code='PROVIDER_NETWORK_ERROR' WHERE job_id=?", (rows[0]["job_id"],))
                connection.execute("UPDATE jobs SET status='needs_review',error_code=NULL WHERE job_id=?", (rows[1]["job_id"],))

        preview = service.preview_provider_retry_wave(run_id)
        assert preview["count"] == 1
        assert preview["jobs"][0]["source_job_id"] == rows[0]["job_id"]
        created = service.bulk_retry_provider_jobs(run_id)
        assert created["count"] == 1
        assert service.preview_provider_retry_wave(run_id)["count"] == 0
        confirmed = service.confirm_provider_retry_wave(run_id, created["wave_hash"])
        assert confirmed["approved"] is True and confirmed["count"] == 1
        repeated = service.confirm_provider_retry_wave(run_id, created["wave_hash"])
        assert repeated["idempotent"] is True and repeated["count"] == 1
        with service.worker.open_database(run_id) as database:
            children = database.fetchall("SELECT cursor_json FROM jobs WHERE stage='AI_ANNOTATE' AND cursor_json LIKE '%retry_of_job_id%'")
            assert len(children) == 1
            assert json.loads(children[0]["cursor_json"])["approved"] is True
    finally:
        service.close()


def test_d040_retry_review_resolution_requires_source_binding_or_exact_legacy_id(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(fail=True))
    try:
        _approve_first(service, run_id)
        _duplicate_ai_job(service, run_id)
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                source = connection.execute("SELECT * FROM jobs WHERE logical_key='ai_batch_0000'").fetchone()
                other = connection.execute("SELECT * FROM jobs WHERE job_id='job_second'").fetchone()
                assert source is not None and other is not None
                source_review = connection.execute(
                    "SELECT review_id FROM review_tasks WHERE reason_code='PROVIDER_FAILURE' AND target_id='minecraft:stone' AND evidence_json LIKE ?",
                    (f"%job:{source['job_id']}%",),
                ).fetchone()
                assert source_review is not None
                connection.execute("UPDATE review_tasks SET evidence_json='[]' WHERE review_id=?", (source_review["review_id"],))
                bound_source = service.worker.create_review_task(
                    connection,
                    "variant",
                    "minecraft:stone",
                    "PROVIDER_FAILURE",
                    "high",
                    "new source-bound review",
                    [f"job:{source['job_id']}"],
                    dedupe_key="new-source-bound",
                )
                other_review = service.worker.create_review_task(
                    connection,
                    "variant",
                    "minecraft:stone",
                    "PROVIDER_FAILURE",
                    "high",
                    "other job review",
                    [f"job:{other['job_id']}"],
                    dedupe_key="other-job",
                )
                source_id = source["job_id"]
        child = service.retry_provider_job(run_id, source_id)
        assert child["job_id"]
        with service.worker.open_database(run_id) as database:
            rows = database.fetchall(
                "SELECT review_id,status FROM review_tasks WHERE review_id IN (?,?,?)",
                (source_review["review_id"], bound_source, other_review),
            )
            statuses = {row["review_id"]: row["status"] for row in rows}
            assert statuses[source_review["review_id"]] == "resolved"
            assert statuses[bound_source] == "resolved"
            assert statuses[other_review] == "open"
    finally:
        service.close()


def test_d040_retry_wave_parent_is_non_leaf_for_every_child_status(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    try:
        _duplicate_ai_job(service, run_id)
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                source = connection.execute("SELECT * FROM jobs WHERE logical_key='ai_batch_0000'").fetchone()
                assert source is not None
                for index in range(2, 4):
                    connection.execute(
                        "INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (f"job_extra_{index}", run_id, "AI_ANNOTATE", f"ai_batch_000{index}", source["input_signature"], "pending", 0, source["cursor_json"], source["created_at"]),
                    )
                connection.execute("UPDATE jobs SET status='needs_review',error_code='PROVIDER_SERVER_ERROR' WHERE stage='AI_ANNOTATE'")
                source_ids = [row["job_id"] for row in connection.execute("SELECT job_id FROM jobs WHERE stage='AI_ANNOTATE' ORDER BY logical_key").fetchall()]
        children = [service.retry_provider_job(run_id, source_id)["job_id"] for source_id in source_ids]
        statuses = ("pending", "running", "succeeded", "failed")
        with service.worker.open_database(run_id) as database:
            for child_id, status in zip(children, statuses):
                database.execute("UPDATE jobs SET status=?,error_code=? WHERE job_id=?", (status, "PROVIDER_SERVER_ERROR" if status == "failed" else None, child_id))
        plan = service.preview_provider_retry_wave(run_id)
        assert plan["count"] == 1
        assert plan["jobs"][0]["source_job_id"] == children[-1]
        next_generation = service.retry_provider_job(run_id, children[-1])
        assert next_generation["job_id"] != children[-1]
    finally:
        service.close()


def test_d044_shared_executor_bounds_and_fresh_provider_instances(tmp_path: Path) -> None:
    serial_tracker = _ConcurrencyTracker(1, release=True)
    serial, serial_runs, _ = _tracked_service(tmp_path / "serial", serial_tracker, concurrency=1, job_count=2)
    try:
        _approve_plan(serial, serial_runs[0])
        serial.tick(serial_runs[0])
        serial.tick(serial_runs[0])
        assert serial_tracker.max_active == 1
        assert serial_tracker.calls == 2
    finally:
        serial.close()
    assert serial_tracker.created == serial_tracker.closed == 2

    tracker = _ConcurrencyTracker(5)
    service, run_ids, _ = _tracked_service(tmp_path / "shared", tracker, concurrency=3, job_count=3, run_count=2)
    try:
        for run_id in run_ids:
            _approve_plan(service, run_id)
        service.tick(run_ids[0])
        service.tick(run_ids[1])
        assert tracker.entered.wait()
        assert tracker.max_active == 5
        assert len(service.worker.registered_ai_jobs(run_ids[0])) == 3
        assert len(service.worker.registered_ai_jobs(run_ids[1])) == 2
        assert len(service.worker.registered_ai_jobs()) == 5
    finally:
        tracker.release.set()
        service.close()
    assert tracker.max_active <= 5
    assert tracker.created == tracker.closed == tracker.calls == 5


def test_d044_ordered_contiguous_approval_barrier(tmp_path: Path) -> None:
    tracker = _ConcurrencyTracker(2)
    service, run_ids, _ = _tracked_service(tmp_path, tracker, concurrency=5, job_count=2)
    run_id = run_ids[0]
    try:
        with service.worker.open_database(run_id) as database:
            rows = database.fetchall("SELECT job_id,logical_key FROM jobs WHERE stage='AI_ANNOTATE' ORDER BY logical_key,job_id")
        assert len(rows) == 2
        later = service.preview_ai_batch(run_id, str(rows[1]["logical_key"]))
        service.approve_ai_batch(run_id, later["logical_key"], later["input_signature"])
        service.tick(run_id)
        assert tracker.calls == 0
        assert service.get_run(run_id)["status"] == "paused"
        with service.worker.open_database(run_id) as database:
            assert all(row["status"] == "pending" for row in database.fetchall("SELECT status FROM jobs WHERE stage='AI_ANNOTATE'"))

        first = service.preview_ai_batch(run_id)
        service.approve_ai_batch(run_id, first["logical_key"], first["input_signature"])
        service.tick(run_id)
        assert tracker.entered.wait()
        registered = service.worker.registered_ai_jobs(run_id)
        assert [item["job_id"] for item in registered] == [row["job_id"] for row in rows]
    finally:
        tracker.release.set()
        service.close()
    assert tracker.calls == 2


def test_d044_final_gate_mutation_and_live_registry_control(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _ConcurrencyTracker(1, release=True)
    service, run_ids, _ = _tracked_service(tmp_path / "mutation", tracker, concurrency=5, job_count=1)
    run_id = run_ids[0]
    gate_seen = threading.Event()
    original_gate = service.worker._final_ai_send_gate

    def revoke_before_gate(gated_run_id: str, job_id: str, signature: str):
        gate_seen.set()
        with service.worker.open_database(gated_run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT cursor_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                assert row is not None
                cursor = json.loads(row["cursor_json"])
                cursor["approved"] = False
                connection.execute("UPDATE jobs SET cursor_json=? WHERE job_id=?", (json.dumps(cursor, sort_keys=True), job_id))
        return original_gate(gated_run_id, job_id, signature)

    monkeypatch.setattr(service.worker, "_final_ai_send_gate", revoke_before_gate)
    try:
        _approve_plan(service, run_id)
        service.tick(run_id)
        assert gate_seen.wait()
        _wait_registry_empty(service, run_id)
        assert tracker.calls == 0
        with service.worker.open_database(run_id) as database:
            job = database.fetchone("SELECT status,cursor_json FROM jobs WHERE run_id=?", (run_id,))
            assert job is not None
            assert job["status"] == "pending"
            assert json.loads(job["cursor_json"])["approved"] is False
        assert service.get_run(run_id)["status"] == "paused"
    finally:
        service.close()

    live_tracker = _ConcurrencyTracker(1)
    live, live_runs, imported = _tracked_service(tmp_path / "live", live_tracker, concurrency=5, job_count=1)
    live_run = live_runs[0]
    try:
        _approve_plan(live, live_run)
        live.tick(live_run)
        assert live_tracker.entered.wait()
        current = live.profile_store.load()["default"]
        stages = dict(current.stages)
        stages["offline_annotation"] = StageConfig(batch_size=12, concurrency=4)
        live.save_provider_profile(replace(current, stages=stages))
        with pytest.raises(R3Error) as reconfigure:
            live.configure_run(imported[0]["import_id"], profile_id="default")
        assert reconfigure.value.code == "RUN_STATE_CONFLICT"
        with pytest.raises(Exception):
            live.worker.recover(live_run)
        with live.worker.open_database(live_run) as database:
            live.worker._finish_ai_stage(database, live_run)
            stage = database.fetchone("SELECT status FROM stage_runs WHERE run_id=? AND stage='AI_ANNOTATE'", (live_run,))
            assert stage is not None and stage["status"] == "running"
        live.tick(live_run)
        live.tick(live_run)
        assert live_tracker.calls == 1
    finally:
        live_tracker.release.set()
        live.close()
    with live.worker.open_database(live_run) as database:
        completed = database.fetchone(
            "SELECT COUNT(*) AS count FROM audit_events WHERE run_id=? AND event_type='STAGE_SUCCEEDED' AND details_json LIKE '%AI_ANNOTATE%'",
            (live_run,),
        )
        assert completed is not None and completed["count"] == 1


def test_d044_pause_cancel_and_fatal_send_started_outcomes_do_not_revive_terminal_runs(tmp_path: Path) -> None:
    tracker = _ConcurrencyTracker(1, outcome="PROVIDER_SERVER_ERROR")
    service, run_ids, _ = _tracked_service(tmp_path / "cancel", tracker, concurrency=5, job_count=1)
    run_id = run_ids[0]
    try:
        preview = _approve_plan(service, run_id)["jobs"][0]
        service.tick(run_id)
        assert tracker.entered.wait()
        with pytest.raises(R3Error) as conflict:
            service.cancel_ai_batch(run_id, preview["logical_key"])
        assert conflict.value.code == "RUN_STATE_CONFLICT"
        service.cancel(run_id)
    finally:
        tracker.release.set()
        service.close()
    assert service.get_run(run_id)["status"] == "cancelled"

    fatal_tracker = _ConcurrencyTracker(1, outcome="PROVIDER_AUTH_FAILED")
    fatal, fatal_runs, _ = _tracked_service(tmp_path / "fatal", fatal_tracker, concurrency=5, job_count=1)
    fatal_run = fatal_runs[0]
    try:
        _approve_plan(fatal, fatal_run)
        fatal.tick(fatal_run)
        assert fatal_tracker.entered.wait()
        fatal.pause(fatal_run)
    finally:
        fatal_tracker.release.set()
        fatal.close()
    assert fatal.get_run(fatal_run)["status"] == "failed"


def test_d044_stop_waits_direct_tick_and_reuses_executor_until_close(tmp_path: Path) -> None:
    import blockpedia.worker as worker_module

    tracker = _ConcurrencyTracker(1)
    service, run_ids, _ = _tracked_service(tmp_path, tracker, concurrency=1, job_count=2)
    run_id = run_ids[0]
    executor = worker_module._PROCESS_COORDINATOR.executor
    tick_thread = threading.Thread(target=lambda: service.tick(run_id))
    stop_result: list[bool] = []
    try:
        _approve_plan(service, run_id)
        tick_thread.start()
        assert tracker.entered.wait()
        stopper = threading.Thread(target=lambda: stop_result.append(service.worker.stop()))
        stopper.start()
        tracker.release.set()
        stopper.join()
        tick_thread.join()
        assert stop_result == [True]
        assert worker_module._PROCESS_COORDINATOR.executor is executor
        assert worker_module._PROCESS_COORDINATOR.process_stopping is False
        tracker.entered.clear()
        assert service.worker.start() is True
        assert tracker.entered.wait()
        service.worker.stop()
        assert worker_module._PROCESS_COORDINATOR.executor is executor
        assert worker_module._PROCESS_COORDINATOR.process_stopping is False
    finally:
        tracker.release.set()
        service.close()
    assert worker_module._PROCESS_COORDINATOR.executor is executor
    assert worker_module._PROCESS_COORDINATOR.process_stopping is False
    assert service.worker.start() is False
    assert tracker.created == tracker.closed == tracker.calls == 2


def test_d044_process_coordinator_is_shared_across_workers_and_roots(tmp_path: Path) -> None:
    from blockpedia.stages import RunStateConflict

    tracker = _ConcurrencyTracker(3)
    service, run_ids, _ = _tracked_service(tmp_path, tracker, concurrency=3, job_count=3)
    fixture = _r2_fixture_module()
    observer = StudioService(
        DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        toolchain_probe=fixture.PassingToolchainProbe(),
        provider_factory=lambda _profile, **_kwargs: _FreshBarrierProvider(tracker),
        secret_resolver=SecretResolver(keyring_backend=_Keyring()),
    )
    run_id = run_ids[0]
    import blockpedia.worker as worker_module

    executor = worker_module._PROCESS_COORDINATOR.executor
    try:
        _approve_plan(service, run_id)
        service.tick(run_id)
        assert tracker.entered.wait(5)
        assert len(observer.worker.registered_ai_jobs(run_id)) == 3
        with pytest.raises(RunStateConflict):
            observer.worker.recover(run_id)
        assert observer.close(timeout=0.1) is True
        assert worker_module._PROCESS_COORDINATOR.executor is executor
        assert worker_module._PROCESS_COORDINATOR.process_stopping is False
    finally:
        tracker.release.set()
        service.close(timeout=5)
    assert tracker.created == tracker.closed == tracker.calls == 3
    assert worker_module._PROCESS_COORDINATOR.executor is executor


def test_d044_stop_wins_final_gate_without_provider_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _ConcurrencyTracker(1, release=True)
    service, run_ids, _ = _tracked_service(tmp_path, tracker, concurrency=1, job_count=1)
    run_id = run_ids[0]
    gate_entered = threading.Event()
    release_gate = threading.Event()
    stop_result: list[bool] = []
    original_gate = service.worker._try_mark_send_started

    def blocked_gate(entry, run_concurrency):
        gate_entered.set()
        assert release_gate.wait(5)
        return original_gate(entry, run_concurrency)

    monkeypatch.setattr(service.worker, "_try_mark_send_started", blocked_gate)
    tick_thread = threading.Thread(target=lambda: service.tick(run_id))
    try:
        _approve_plan(service, run_id)
        tick_thread.start()
        assert gate_entered.wait(5)
        stopper = threading.Thread(target=lambda: stop_result.append(service.worker.stop(timeout=0.05)))
        stopper.start()
        stopper.join(timeout=5)
        assert stop_result == [False]
        release_gate.set()
        tick_thread.join(timeout=5)
        assert not tick_thread.is_alive()
        assert service.worker.stop(timeout=5) is True
        assert tracker.calls == 0
        with service.worker.open_database(run_id) as database:
            job = database.fetchone("SELECT status FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE'", (run_id,))
            assert job is not None and job["status"] == "pending"
    finally:
        release_gate.set()
        tracker.release.set()
        service.close(timeout=5)


def test_d044_submit_failure_restores_only_unsubmitted_suffix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tracker = _ConcurrencyTracker(3, release=True)
    service, run_ids, _ = _tracked_service(tmp_path, tracker, concurrency=3, job_count=3)
    run_id = run_ids[0]
    import blockpedia.worker as worker_module

    executor = worker_module._PROCESS_COORDINATOR.executor
    original_submit = executor.submit
    submit_calls = 0

    def fail_first_submit(function, *args, **kwargs):
        nonlocal submit_calls
        submit_calls += 1
        if submit_calls == 1:
            raise RuntimeError("fixture submit failure")
        return original_submit(function, *args, **kwargs)

    monkeypatch.setattr(executor, "submit", fail_first_submit)
    try:
        _approve_plan(service, run_id)
        service.tick(run_id)
        _wait_registry_empty(service, run_id)
        assert submit_calls == 1
        assert tracker.calls == 0
        with service.worker.open_database(run_id) as database:
            statuses = database.fetchall("SELECT status FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY logical_key", (run_id,))
            assert statuses and all(row["status"] == "pending" for row in statuses)

        monkeypatch.setattr(executor, "submit", original_submit)
        service.tick(run_id)
        _wait_registry_empty(service, run_id)
        assert tracker.calls == 3
    finally:
        tracker.release.set()
        service.close(timeout=5)


def test_d044_completion_callback_retains_registry_until_stage_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from blockpedia.stages import RunStateConflict

    tracker = _ConcurrencyTracker(1, release=True)
    service, run_ids, _ = _tracked_service(tmp_path, tracker, concurrency=1, job_count=1)
    run_id = run_ids[0]
    finish_entered = threading.Event()
    release_finish = threading.Event()
    finish_calls = 0
    original_finish = service.worker._finish_ai_stage

    def blocked_finish(database, gated_run_id, **kwargs):
        nonlocal finish_calls
        if kwargs.get("exclude_entry_key") is not None:
            finish_calls += 1
            finish_entered.set()
            assert release_finish.wait(5)
        return original_finish(database, gated_run_id, **kwargs)

    monkeypatch.setattr(service.worker, "_finish_ai_stage", blocked_finish)
    tick_thread = threading.Thread(target=lambda: service.tick(run_id))
    try:
        _approve_plan(service, run_id)
        tick_thread.start()
        assert finish_entered.wait(5)
        entries = service.worker.registered_ai_jobs(run_id)
        assert len(entries) == 1 and entries[0]["completion_in_progress"] is True
        assert service.stale_markers(run_id) == []
        recover_done = threading.Event()
        recover_error: list[Exception] = []

        def blocked_recover() -> None:
            try:
                service.worker.recover(run_id)
            except Exception as exc:
                recover_error.append(exc)
            finally:
                recover_done.set()

        recover_thread = threading.Thread(target=blocked_recover)
        recover_thread.start()
        assert not recover_done.wait(0.05)
        assert service.worker.stop(timeout=0.05) is False
        release_finish.set()
        recover_thread.join(timeout=5)
        assert recover_done.is_set()
        assert recover_error and isinstance(recover_error[0], RunStateConflict)
        tick_thread.join(timeout=5)
        assert not tick_thread.is_alive()
        assert service.worker.stop(timeout=5) is True
        assert service.worker.registered_ai_jobs(run_id) == ()
        assert finish_calls == 1
    finally:
        release_finish.set()
        tracker.release.set()
        service.close(timeout=5)


def test_d044_serial_background_close_is_retryable_and_truthful(tmp_path: Path) -> None:
    tracker = _ConcurrencyTracker(1)
    service, run_ids, _ = _tracked_service(tmp_path, tracker, concurrency=1, job_count=1)
    run_id = run_ids[0]
    import blockpedia.worker as worker_module

    executor = worker_module._PROCESS_COORDINATOR.executor
    try:
        _approve_plan(service, run_id)
        assert service.worker.start(interval_seconds=0.01) is True
        assert tracker.entered.wait(5)
        assert service.close(timeout=0.05) is False
        assert service._closed is False
        assert service.worker._closed is False
        tracker.release.set()
        assert service.close(timeout=5) is True
        assert service._closed is True
        assert service.worker.start() is False
        assert worker_module._PROCESS_COORDINATOR.executor is executor
        assert worker_module._PROCESS_COORDINATOR.process_stopping is False
    finally:
        tracker.release.set()
        if not service._closed:
            service.close(timeout=5)
