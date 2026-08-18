from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import shutil
import copy
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import blockpedia.releases as releases_module
import blockpedia.activation as activation_module
from blockpedia.activation import ActivationError
from blockpedia.mcp_release import MCPReleaseResolver
from blockpedia.paths import DataRoot
from blockpedia.provider import ProviderProfile, SecretResolver
from blockpedia.schema import validate_record
from blockpedia.services import R3Error, StudioService
from blockpedia.web import create_app

from tests.r3.test_pipeline_review import _FakeProvider, _Keyring, _approve_first, _r2_fixture_module
from tests.r4.fixture_builder import build_fixture


def _two_candidates(tmp_path: Path, monkeypatch, *, smoke: bool = False):
    fixture = _r2_fixture_module()
    export = _make_two_visual_export(tmp_path, fixture)
    service = StudioService(
        DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        toolchain_probe=fixture.PassingToolchainProbe(),
        provider_factory=lambda profile, **_kwargs: _FakeProvider(),
        secret_resolver=SecretResolver(keyring_backend=_Keyring()),
    )
    check = service.check_import(export, "26.2")
    imported = service.import_checked(check.check_id)
    run_id = imported["run_id"]
    for _ in range(10):
        service.tick(run_id)
    service.save_profile(ProviderProfile(profile_id="default", model_id="fixture-model", adapter="openai_responses", base_url="http://127.0.0.1:8766/v1"))
    service.profile_store.record_probe({"profile_id": "default", "adapter": "openai_responses", "capability_status": "verified", "image_input_supported": True, "structured_outputs_supported": True, "error_classification_supported": True, "store_false_supported": True, "base_url_stable_id": "http://127.0.0.1:8766/v1"})
    service.enable("default")
    service.configure_run(imported["import_id"], "26.2", profile_id="default")
    _approve_first(service, run_id)
    for _ in range(4):
        service.tick(run_id)
    for review in service.list_reviews(run_id):
        service.resolve_review(run_id, review["review_id"], decision="accept", reviewer="tester", reason_code="OTHER", note="fixture review", evidence=["fixture:review"])
    service.continue_review(run_id)
    service.tick(run_id)
    first_check = service.check_candidate_release(run_id, "26.2")
    first = service.build_candidate_release(first_check["check_id"])
    monkeypatch.setattr(releases_module, "_now", lambda: "2099-01-01T00:00:00Z")
    second_check = service.check_candidate_release(run_id, "26.2")
    second = service.build_candidate_release(second_check["check_id"])
    if not smoke:
        service.activation._mcp_smoke = lambda minecraft_version, target: None
    return service, run_id, first, second


def _make_two_visual_export(tmp_path: Path, fixture: Any) -> Path:
    export = fixture.make_export(tmp_path)
    read_rows = lambda name: [json.loads(line) for line in (export / name).read_text(encoding="utf-8").splitlines() if line]
    states = read_rows("states.jsonl")
    variants = read_rows("variants.jsonl")
    glass_state = next(row for row in states if row["block_id"] == "minecraft:glass")
    glass_state["variant_ids"] = ["minecraft:glass"]
    glass_state["mapping_status"] = "mapped"
    stone_variant = next(row for row in variants if row["variant_id"] == "minecraft:stone")
    glass_variant = copy.deepcopy(stone_variant)
    glass_variant["variant_id"] = "minecraft:glass"
    glass_variant["block_id"] = "minecraft:glass"
    glass_variant["canonical_state_id"] = "minecraft:glass"
    glass_variant["represented_state_ids"] = ["minecraft:glass"]
    glass_variant["machine_facts"]["behavior_by_state"] = {"minecraft:glass": glass_state["behavior"]}
    source_render = export / "renders" / "minecraft" / "stone"
    target_render = export / "renders" / "minecraft" / "glass"
    target_render.mkdir(parents=True)
    for name in ("preview.png", "mask.png"):
        (target_render / name).write_bytes((source_render / name).read_bytes())
    metadata = json.loads((source_render / "render.json").read_text(encoding="utf-8"))
    metadata["variant_id"] = "minecraft:glass"
    metadata_bytes = (fixture._jcs_canonical(metadata) + "\n").encode("utf-8")
    (target_render / "render.json").write_bytes(metadata_bytes)
    glass_variant["render"]["preview_path"] = "renders/minecraft/glass/preview.png"
    glass_variant["render"]["mask_path"] = "renders/minecraft/glass/mask.png"
    glass_variant["render"]["render_metadata_path"] = "renders/minecraft/glass/render.json"
    glass_variant["render"]["render_metadata_sha256"] = fixture._hash_bytes(fixture._jcs_canonical(metadata).encode("utf-8"))
    variants = [stone_variant, glass_variant]
    (export / "states.jsonl").write_bytes("".join(fixture._jcs_canonical(row) + "\n" for row in states).encode("utf-8"))
    (export / "variants.jsonl").write_bytes("".join(fixture._jcs_canonical(row) + "\n" for row in variants).encode("utf-8"))
    (export / "failures.jsonl").write_bytes(b"")
    manifest_path = export / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"] = {"block_records": 2, "failure_records": 0, "pending_review_records": 0, "registry_blocks": 2, "selected_variant_records": 2, "skipped_variant_records": 0, "state_records": 2}
    manifest["status"] = "succeeded"
    manifest_path.write_bytes((fixture._jcs_canonical(manifest) + "\n").encode("utf-8"))
    files = sorted((item for item in export.rglob("*") if item.is_file() and item.name != "checksums.sha256"), key=lambda item: item.relative_to(export).as_posix().encode("utf-8"))
    (export / "checksums.sha256").write_bytes("".join(hashlib.sha256(item.read_bytes()).hexdigest() + "  " + item.relative_to(export).as_posix() + "\n" for item in files).encode("ascii"))
    return export


def _release_bytes(root: Path, release: dict[str, str]) -> dict[str, bytes]:
    path = root / release["relative_path"]
    return {item.relative_to(path).as_posix(): item.read_bytes() for item in path.rglob("*") if item.is_file()}


def _make_v1_release(release_path: Path, source_root: Path) -> None:
    v1_fixture = build_fixture(source_root, index_version=1)
    os.chmod(release_path / "index.sqlite3", 0o600)
    (release_path / "index.sqlite3").write_bytes((v1_fixture.release / "index.sqlite3").read_bytes())
    index_digest = hashlib.sha256((release_path / "index.sqlite3").read_bytes()).hexdigest()
    checksums = (release_path / "checksums.sha256").read_text(encoding="ascii").splitlines()
    rewritten = []
    for line in checksums:
        digest, relative = line.split("  ", 1)
        rewritten.append(f"{index_digest}  {relative}" if relative == "index.sqlite3" else f"{digest}  {relative}")
    os.chmod(release_path / "checksums.sha256", 0o600)
    (release_path / "checksums.sha256").write_bytes(("\n".join(rewritten) + "\n").encode("ascii"))


def test_activation_gate_rejects_historical_and_unbacked_release(tmp_path: Path, monkeypatch) -> None:
    service, run_id, first, second = _two_candidates(tmp_path, monkeypatch)
    try:
        _make_v1_release(tmp_path / first["relative_path"], tmp_path / "historical-v1")
        copied_id = "rel_" + "d" * 32
        shutil.copytree(tmp_path / second["relative_path"], tmp_path / "releases" / "26.2" / copied_id)
        state = service.check_activation(run_id, "26.2", second["release_id"])
        assert state["status"] == "failed"
        assert state["can_apply"] is False
        assert state["error_code"] == "ACTIVATION_CANDIDATES_INSUFFICIENT"
    finally:
        service.close()


def test_activation_check_runs_actual_four_tool_mcp_smoke(tmp_path: Path, monkeypatch) -> None:
    assert importlib.metadata.version("mcp") == "2.0.0"
    source_root = str(Path(__file__).parents[2] / "src")
    monkeypatch.setenv("PYTHONPATH", source_root + os.pathsep + os.environ.get("PYTHONPATH", ""))
    service, run_id, first, second = _two_candidates(tmp_path, monkeypatch, smoke=True)
    try:
        before = _release_bytes(tmp_path, second)
        state = service.check_activation(run_id, "26.2", second["release_id"])
        assert state["status"] == "passed"
        assert state["can_apply"] is True
        assert state["expected_current_sha256"] is None
        assert not (tmp_path / "current.json").exists()
        assert _release_bytes(tmp_path, second) == before
        assert first["release_id"] in {item["release_id"] for item in state["candidate_releases"]}
    finally:
        service.close()


def test_mcp_session_uses_bounded_large_line_limit(tmp_path: Path, monkeypatch) -> None:
    original_launcher = activation_module.asyncio.create_subprocess_exec
    observed: dict[str, Any] = {}
    child_script = (
        "import json,sys;"
        "request=json.loads(sys.stdin.readline());"
        "print(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':{'payload':'x'*100000}}, separators=(',',':')))"
    )

    async def launcher(*_args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        observed["limit"] = kwargs.get("limit")
        return await original_launcher(sys.executable, "-c", child_script, **kwargs)

    monkeypatch.setattr(activation_module.asyncio, "create_subprocess_exec", launcher)
    service = activation_module.ActivationService(DataRoot(tmp_path), repo_root=Path(__file__).parents[2])
    responses = asyncio.run(service._mcp_session(tmp_path, [{"jsonrpc": "2.0", "id": 1, "method": "fixture"}]))

    assert observed["limit"] == activation_module.MCP_STDIO_LINE_LIMIT == 1024 * 1024
    assert len(responses) == 1
    assert responses[0]["id"] == 1
    assert len(responses[0]["result"]["payload"]) == 100000


def test_activation_routes_require_confirmation_and_default(tmp_path: Path, monkeypatch) -> None:
    service, run_id, _first, second = _two_candidates(tmp_path, monkeypatch)
    try:
        state = service.check_activation(run_id, "26.2", second["release_id"])
        app = create_app(data_root=tmp_path, service=service, start_worker=False)
        with TestClient(app) as client:
            missing = client.post("/api/releases/apply", json={"activation_check_id": state["activation_check_id"], "set_as_default": True})
            assert missing.status_code == 400
            false_confirmation = client.post("/api/releases/apply", json={"activation_check_id": state["activation_check_id"], "confirm_current_switch": False, "set_as_default": True})
            assert false_confirmation.status_code == 400
            first_default = client.post("/api/releases/apply", json={"activation_check_id": state["activation_check_id"], "confirm_current_switch": True, "set_as_default": False})
            assert first_default.status_code == 400
            assert first_default.json()["error_code"] == "ACTIVATION_DEFAULT_REQUIRED"
    finally:
        service.close()


def test_successful_apply_is_atomic_and_resolves_with_mcp(tmp_path: Path, monkeypatch) -> None:
    service, run_id, _first, second = _two_candidates(tmp_path, monkeypatch)
    try:
        before = _release_bytes(tmp_path, second)
        state = service.check_activation(run_id, "26.2", second["release_id"])
        applied = service.apply_activation(state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert applied["status"] == "applied"
        pointer = json.loads((tmp_path / "current.json").read_text(encoding="utf-8"))
        validate_record("current-pointer.v1", pointer)
        with MCPReleaseResolver(tmp_path, repo_root=Path(__file__).parents[2]).resolve("26.2") as handle:
            assert handle.release_id == second["release_id"]
        assert _release_bytes(tmp_path, second) == before
        with service.worker.open_database(run_id) as database:
            run = database.fetchone("SELECT status,current_stage FROM runs WHERE run_id=?", (run_id,))
            audit_count = database.fetchone("SELECT COUNT(*) AS count FROM audit_events WHERE run_id=? AND event_type='CURRENT_SWITCHED'", (run_id,))
        assert run is not None and dict(run) == {"status": "succeeded", "current_stage": "ACTIVATE_RELEASE"}
        assert audit_count is not None and audit_count["count"] == 1
    finally:
        service.close()


def test_final_activation_state_write_failure_converges_on_retry(tmp_path: Path, monkeypatch) -> None:
    service, run_id, _first, second = _two_candidates(tmp_path, monkeypatch)
    try:
        state = service.check_activation(run_id, "26.2", second["release_id"])
        release_before = _release_bytes(tmp_path, second)
        failed = {"value": False}
        original_write_state = service.activation._write_state

        def fail_final_write(value: dict[str, Any]) -> None:
            if value["status"] == "applied" and not failed["value"]:
                failed["value"] = True
                raise ActivationError("ACTIVATION_STATE_WRITE_FAILED")
            original_write_state(value)

        monkeypatch.setattr(service.activation, "_write_state", fail_final_write)
        with pytest.raises(R3Error) as error:
            service.apply_activation(state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert error.value.code == "ACTIVATION_STATE_WRITE_FAILED"
        persisted = service.activation._read_state(state["activation_check_id"])
        assert persisted["status"] == "passed"
        current_after_first = (tmp_path / "current.json").read_bytes()

        monkeypatch.setattr(service.activation, "_write_state", original_write_state)
        applied = service.apply_activation(state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert applied["status"] == "applied"
        assert applied["can_apply"] is False
        assert applied["error_code"] is None
        assert (tmp_path / "current.json").read_bytes() == current_after_first
        assert _release_bytes(tmp_path, second) == release_before

        with MCPReleaseResolver(tmp_path, repo_root=Path(__file__).parents[2]).resolve("26.2") as handle:
            assert handle.release_id == second["release_id"]
        final_state = service.activation._read_state(state["activation_check_id"])
        assert final_state["status"] == "applied"
        with service.worker.open_database(run_id) as database:
            run = database.fetchone("SELECT status,current_stage,boundary_event FROM runs WHERE run_id=?", (run_id,))
            stage = database.fetchone("SELECT status,cursor_json FROM stage_runs WHERE run_id=? AND stage='ACTIVATE_RELEASE'", (run_id,))
            audits = database.fetchall("SELECT details_json FROM audit_events WHERE run_id=? AND event_type='CURRENT_SWITCHED'", (run_id,))
        assert run is not None and dict(run) == {"status": "succeeded", "current_stage": "ACTIVATE_RELEASE", "boundary_event": None}
        assert stage is not None and stage["status"] == "succeeded"
        assert json.loads(stage["cursor_json"]) == {"activation_check_id": state["activation_check_id"], "release_id": second["release_id"], "completed": True}
        assert len(audits) == 1
        assert json.loads(audits[0]["details_json"]) == {"activation_check_id": state["activation_check_id"], "minecraft_version": "26.2", "target_release_id": second["release_id"], "set_as_default": True}
    finally:
        service.close()


@pytest.mark.parametrize("mutation", ["cursor", "audit"])
def test_completed_retry_rejects_malformed_transition_evidence(tmp_path: Path, monkeypatch, mutation: str) -> None:
    service, run_id, _first, second = _two_candidates(tmp_path, monkeypatch)
    try:
        state = service.check_activation(run_id, "26.2", second["release_id"])
        original_write_state = service.activation._write_state
        failed = {"value": False}

        def fail_final_write(value: dict[str, Any]) -> None:
            if value["status"] == "applied" and not failed["value"]:
                failed["value"] = True
                raise ActivationError("ACTIVATION_STATE_WRITE_FAILED")
            original_write_state(value)

        monkeypatch.setattr(service.activation, "_write_state", fail_final_write)
        with pytest.raises(R3Error) as error:
            service.apply_activation(state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert error.value.code == "ACTIVATION_STATE_WRITE_FAILED"
        monkeypatch.setattr(service.activation, "_write_state", original_write_state)

        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                if mutation == "cursor":
                    connection.execute("UPDATE stage_runs SET cursor_json='[]' WHERE run_id=? AND stage='ACTIVATE_RELEASE'", (run_id,))
                else:
                    connection.execute("UPDATE audit_events SET details_json='[]' WHERE run_id=? AND event_type='CURRENT_SWITCHED'", (run_id,))

        with pytest.raises(R3Error) as retry_error:
            service.apply_activation(state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert retry_error.value.code in {"ACTIVATION_RUN_STATE_INVALID", "ACTIVATION_AUDIT_INTEGRITY_FAILED"}
        assert service.activation._read_state(state["activation_check_id"])["status"] == "passed"
    finally:
        service.close()


def test_pre_replace_failure_and_post_replace_retry_converge(tmp_path: Path, monkeypatch) -> None:
    service, run_id, _first, second = _two_candidates(tmp_path, monkeypatch)
    try:
        state = service.check_activation(run_id, "26.2", second["release_id"])
        service.activation.before_current_replace = lambda _temp, _current: (_ for _ in ()).throw(RuntimeError("before replace"))
        with pytest.raises(R3Error) as before_error:
            service.apply_activation(state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert before_error.value.code == "CURRENT_SWITCH_FAILED"
        assert not (tmp_path / "current.json").exists()
        service.activation.before_current_replace = None

        calls = {"count": 0}

        def fail_once() -> None:
            if calls["count"] == 0:
                calls["count"] += 1
                raise ActivationError("ACTIVATION_APPLY_FAILED")

        service.activation.after_current_replace = fail_once
        with pytest.raises(R3Error) as after_error:
            service.apply_activation(state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert after_error.value.code == "ACTIVATION_APPLY_FAILED"
        service.activation.after_current_replace = None
        applied = service.apply_activation(state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert applied["status"] == "applied"
        with service.worker.open_database(run_id) as database:
            audit_count = database.fetchone("SELECT COUNT(*) AS count FROM audit_events WHERE run_id=? AND event_type='CURRENT_SWITCHED'", (run_id,))
        assert audit_count is not None and audit_count["count"] == 1
    finally:
        service.close()


def test_nonlatest_activation_check_is_stale(tmp_path: Path, monkeypatch) -> None:
    service, run_id, _first, second = _two_candidates(tmp_path, monkeypatch)
    try:
        service.activation._mcp_smoke = lambda minecraft_version, target: None
        first_state = service.check_activation(run_id, "26.2", second["release_id"])
        monkeypatch.setattr(activation_module, "utc_now", lambda: "2100-01-01T00:00:00Z")
        second_state = service.check_activation(run_id, "26.2", second["release_id"])
        assert first_state["activation_check_id"] != second_state["activation_check_id"]
        with pytest.raises(R3Error) as error:
            service.apply_activation(first_state["activation_check_id"], confirm_current_switch=True, set_as_default=True)
        assert error.value.code == "ACTIVATION_CHECK_STALE"
    finally:
        service.close()
