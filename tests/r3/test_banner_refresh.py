from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
from typing import Any

import pytest

from blockpedia.banner_refresh import BANNER_POLICY_TOKEN, BANNER_TARGET_IDS, BannerRefreshFailure, _checked_file_map, _compare_exports, _package, _workspace_file_map
from blockpedia.importer import _project_block, _project_state, _snapshot_export, copy_to_workspace
from blockpedia.paths import DataRoot
from blockpedia.provider import ProviderProfile, SecretResolver
from blockpedia.services import R3Error, StudioService
from blockpedia.storage import WorkspaceDatabase, utc_now
from blockpedia.web import create_app
from blockpedia.worker import _batch_input_signature


class _Keyring:
    def get_password(self, service: str, account: str) -> str:
        assert service == "blockpedia"
        return "fixture-secret"


class _Provider:
    def annotate(self, *_args, **_kwargs):
        raise AssertionError("banner refresh tests must not call the provider")

    def close(self) -> None:
        pass


def _fixture_module():
    path = Path(__file__).parents[1] / "r2" / "conftest.py"
    spec = importlib.util.spec_from_file_location("blockpedia_banner_fixture", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n" for record in records), encoding="utf-8")


def _replace_export_id(value: Any, old: str, new: str) -> Any:
    if isinstance(value, dict):
        return {key: _replace_export_id(item, old, new) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_export_id(item, old, new) for item in value]
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def _rechecksum(export: Path) -> tuple[tuple[dict[str, str], ...], str]:
    refs = sorted(path.relative_to(export).as_posix() for path in export.rglob("*") if path.is_file() and path.name != "checksums.sha256")
    checksums = "".join(hashlib.sha256((export / ref).read_bytes()).hexdigest() + "  " + ref + "\n" for ref in refs)
    (export / "checksums.sha256").write_text(checksums, encoding="utf-8")
    expected, checksum, _ = _snapshot_export(export)
    return expected, checksum


def _target_records(base_export: Path, export_id: str, *, selected: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    blocks = _jsonl(base_export / "blocks.jsonl")
    states = _jsonl(base_export / "states.jsonl")
    variants = _jsonl(base_export / "variants.jsonl")
    failures = _jsonl(base_export / "failures.jsonl")
    block_template = next(item for item in blocks if item["block_id"] == "minecraft:stone")
    state_template = next(item for item in states if item["state_id"] == "minecraft:stone")
    variant_template = next(item for item in variants if item["variant_id"] == "minecraft:stone")
    target_blocks: list[dict] = []
    target_states: list[dict] = []
    target_variants: list[dict] = []
    target_failures: list[dict] = []
    for target_id in BANNER_TARGET_IDS:
        block = _replace_export_id(deepcopy(block_template), block_template["export_id"], export_id)
        block.update({"block_id": target_id, "default_state_id": target_id})
        block["source"]["export_id"] = export_id
        target_blocks.append(block)
        state = _replace_export_id(deepcopy(state_template), state_template["export_id"], export_id)
        state.update({"state_id": target_id, "block_id": target_id, "mapping_status": "mapped" if selected else "skipped", "variant_ids": [target_id] if selected else []})
        state["source"]["export_id"] = export_id
        target_states.append(state)
        variant = _replace_export_id(deepcopy(variant_template), variant_template["export_id"], export_id)
        variant.update({"variant_id": target_id, "block_id": target_id, "canonical_state_id": target_id, "represented_state_ids": [target_id], "status": "selected" if selected else "skipped", "candidate_qualification": "eligible" if selected else "excluded"})
        variant["source"]["export_id"] = export_id
        if selected:
            suffix = target_id.removeprefix("minecraft:")
            source_render = variant_template["render"]
            variant["render"] = {
                **source_render,
                "preview_path": f"renders/minecraft/{suffix}/preview.png",
                "mask_path": f"renders/minecraft/{suffix}/mask.png",
                "render_metadata_path": f"renders/minecraft/{suffix}/render.json",
            }
        else:
            for field in ("canonical_state_id", "represented_state_ids", "context", "selection", "machine_facts", "render"):
                variant.pop(field, None)
            variant["skip_reason_code"] = "OBJECT_OFF_CANVAS"
            variant["skip_reason"] = "banner object is off canvas in the base render"
        target_variants.append(variant)
        if not selected:
            failure = deepcopy(failures[0])
            failure.update({"failure_id": f"failure_{target_id.removeprefix('minecraft:')}", "block_id": target_id, "state_id": target_id, "variant_id": target_id, "kind": "skip", "stage": "RENDER_VARIANTS", "scope": "render", "reason_code": "OBJECT_OFF_CANVAS", "review_status": "pending", "action": "needs_review", "message": "banner object is off canvas in the base render"})
            target_failures.append(failure)
    return target_blocks, target_states, target_variants, target_failures


def _make_package(source: Path, destination: Path, *, export_id: str, selected: bool) -> Path:
    shutil.copytree(source, destination)
    for path in destination.rglob("*.json"):
        value = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(_replace_export_id(value, source.name, export_id), ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    for name in ("blocks.jsonl", "states.jsonl", "variants.jsonl", "failures.jsonl"):
        records = [_replace_export_id(item, source.name, export_id) for item in _jsonl(destination / name)]
        blocks, states, variants, failures = _target_records(source, export_id, selected=selected)
        if name == "blocks.jsonl":
            records.extend(blocks)
        elif name == "states.jsonl":
            records.extend(states)
        elif name == "variants.jsonl":
            records.extend(variants)
        else:
            records.extend(failures)
        if selected and name == "failures.jsonl":
            records = [record for record in records if record.get("reason_code") != "OBJECT_OFF_CANVAS"]
        _write_jsonl(destination / name, records)
    if selected:
        source_preview = source / "renders/minecraft/stone/preview.png"
        source_mask = source / "renders/minecraft/stone/mask.png"
        source_metadata = source / "renders/minecraft/stone/render.json"
        variant_records = {item["variant_id"]: item for item in _jsonl(destination / "variants.jsonl")}
        for target_id in BANNER_TARGET_IDS:
            suffix = target_id.removeprefix("minecraft:")
            target_dir = destination / "renders/minecraft" / suffix
            target_dir.mkdir(parents=True)
            shutil.copy2(source_preview, target_dir / "preview.png")
            shutil.copy2(source_mask, target_dir / "mask.png")
            shutil.copy2(source_metadata, target_dir / "render.json")
            render = variant_records[target_id]["render"]
            render["image_sha256"] = "sha256:" + hashlib.sha256((target_dir / "preview.png").read_bytes()).hexdigest()
            render["mask_sha256"] = "sha256:" + hashlib.sha256((target_dir / "mask.png").read_bytes()).hexdigest()
            render["render_metadata_sha256"] = "sha256:" + hashlib.sha256(json.dumps(json.loads((target_dir / "render.json").read_text(encoding="utf-8")), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        _write_jsonl(destination / "variants.jsonl", list(variant_records.values()))
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    manifest["export_id"] = export_id
    (destination / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
    _rechecksum(destination)
    return destination


def _service_fixture(tmp_path: Path) -> tuple[StudioService, str, Path, Path, object]:
    fixture = _fixture_module()
    original = fixture.make_export(tmp_path)
    service = StudioService(
        DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        toolchain_probe=fixture.PassingToolchainProbe(),
        provider_factory=lambda _profile, **_kwargs: _Provider(),
        secret_resolver=SecretResolver(keyring_backend=_Keyring()),
    )
    check = service.check_import(original, "26.2")
    imported = service.import_checked(check.check_id)
    run_id = imported["run_id"]
    for _ in range(6):
        service.tick(run_id)
    profile = ProviderProfile(profile_id="default", model_id="fixture-model", base_url="http://127.0.0.1:8766/v1")
    service.save_profile(profile)
    service.profile_store.record_probe({"profile_id": "default", "adapter": profile.adapter, "capability_status": "verified", "image_input_supported": True, "structured_outputs_supported": True, "error_classification_supported": True, "store_false_supported": True, "base_url_stable_id": profile.base_url_stable_id})
    service.enable("default")
    service.configure_run(imported["import_id"], "26.2", profile_id="default")
    with service.worker.open_database(run_id) as database:
        workspace = database.path.parent
    return service, run_id, original, workspace, check


def _prepare_refresh(service: StudioService, run_id: str, original: Path, workspace: Path, tmp_path: Path):
    base_export = _make_package(original, tmp_path / "base-export", export_id=original.name, selected=False)
    replacement_id = "export_20260817T120001Z"
    replacement = _make_package(original, tmp_path / replacement_id, export_id=replacement_id, selected=True)
    base_expected, base_checksum = _rechecksum(base_export)
    new_expected, new_checksum = _rechecksum(replacement)
    copy_to_workspace(base_export, workspace, expected_files=base_expected, checksum_sha256=base_checksum)
    base_manifest = next(item["sha256"] for item in base_expected if item["relative_ref"] == "manifest.json")
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            connection.execute("UPDATE jobs SET status='succeeded',finished_at=? WHERE run_id=? AND stage='AI_ANNOTATE' AND status='pending'", (utc_now(), run_id))
            connection.execute("UPDATE imports SET manifest_sha256=?,checksum_sha256=?,expected_files_json=? WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)", (base_manifest, base_checksum, json.dumps(base_expected, sort_keys=True, separators=(",", ":")), run_id))
            blocks, states, variants, failures = _target_records(base_export, base_export.name, selected=False)
            for block in blocks:
                connection.execute("INSERT INTO blocks(block_id,minecraft_version,record_json) VALUES (?,?,?)", (block["block_id"], "26.2", json.dumps(_project_block(block), sort_keys=True, separators=(",", ":"))))
            for failure in failures:
                connection.execute("INSERT INTO failures(failure_id,minecraft_version,block_id,state_id,variant_id,record_json) VALUES (?,?,?,?,?,?)", (failure["failure_id"], "26.2", failure["block_id"], failure["state_id"], failure["variant_id"], json.dumps(failure, sort_keys=True, separators=(",", ":"))))
            for state in states:
                connection.execute("INSERT INTO states(state_id,block_id,minecraft_version,record_json,failure_id) VALUES (?,?,?,?,?)", (state["state_id"], state["block_id"], "26.2", json.dumps(_project_state(state, f"failure_{state['block_id'].removeprefix('minecraft:')}"), sort_keys=True, separators=(",", ":")), f"failure_{state['block_id'].removeprefix('minecraft:')}"))
            for target_id in BANNER_TARGET_IDS:
                connection.execute("INSERT INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (f"review_{target_id.removeprefix('minecraft:')}", "26.2", "variant", target_id, "OBJECT_OFF_CANVAS", "high", "open", "fixture", "[]", utc_now()))
            connection.execute("UPDATE runs SET status='needs_review',current_stage='HUMAN_REVIEW',boundary_event=NULL WHERE run_id=?", (run_id,))
            connection.execute("UPDATE stage_runs SET status=CASE WHEN ordinal <= 5 THEN 'succeeded' ELSE CASE WHEN stage='HUMAN_REVIEW' THEN 'needs_review' ELSE 'pending' END END,worker_id=NULL,heartbeat_at=NULL,finished_at=CASE WHEN ordinal <= 5 THEN COALESCE(finished_at,?) ELSE NULL END WHERE run_id=?", (utc_now(), run_id))
    from blockpedia.importer import ImportCheck
    replacement_manifest = next(item["sha256"] for item in new_expected if item["relative_ref"] == "manifest.json")
    replacement_check = ImportCheck(check_id="banner-check", minecraft_version="26.2", export_id=replacement.name, source_directory_ref="", manifest_sha256=replacement_manifest, checksum_sha256=new_checksum, snapshot_ref="", snapshot_root_sha256="", metadata_sha256="", expected_files=tuple(new_expected), status="passed", issues=(), can_import=True)
    return replacement, replacement_check


def test_banner_refresh_success_is_incremental_and_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, run_id, original, workspace, _base_check = _service_fixture(tmp_path)
    replacement, replacement_check = _prepare_refresh(service, run_id, original, workspace, tmp_path)
    monkeypatch.setattr(service.imports, "resolve_checked_snapshot", lambda _check_id: (replacement_check, replacement))
    preserved_variant_id = "minecraft:stone"
    try:
        with service.worker.open_database(run_id) as database:
            preserved_before_row = database.fetchone("SELECT source_json,record_json FROM variants WHERE variant_id=? AND status='selected'", (preserved_variant_id,))
            assert preserved_before_row is not None
            preserved_before = json.loads(preserved_before_row["record_json"])
        result = service.refresh_banner_export(run_id, check_id="banner-check", expected_base_export_id=original.name, target_ids=list(BANNER_TARGET_IDS), confirm=True)
        assert result["target_count"] == 32
        assert result["new_variant_count"] == 32
        assert result["new_feature_count"] == 32
        assert result["new_ai_job_count"] == 3
        assert result["current_stage"] == "AI_ANNOTATE"
        assert service.refresh_banner_export(run_id, check_id="banner-check", expected_base_export_id=original.name, target_ids=list(BANNER_TARGET_IDS), confirm=True)["idempotent"] is True
        with service.worker.open_database(run_id) as database:
            variant_count = database.fetchone("SELECT COUNT(*) AS count FROM variants WHERE variant_id LIKE 'minecraft:%_banner' OR variant_id LIKE 'minecraft:%_wall_banner'")
            feature_count = database.fetchone("SELECT COUNT(*) AS count FROM features WHERE variant_id LIKE 'minecraft:%_banner' OR variant_id LIKE 'minecraft:%_wall_banner'")
            failure_count = database.fetchone("SELECT COUNT(*) AS count FROM failures WHERE record_json LIKE '%OBJECT_OFF_CANVAS%'")
            provenance_row = database.fetchone("SELECT report_json FROM imports WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)", (run_id,))
            assert variant_count is not None and variant_count["count"] == 32
            assert feature_count is not None and feature_count["count"] == 32
            assert failure_count is not None and failure_count["count"] == 0
            jobs = database.fetchall("SELECT logical_key FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY logical_key", (run_id,))
            assert [row["logical_key"] for row in jobs if row["logical_key"].startswith("banner_refresh_")] == ["banner_refresh_0000", "banner_refresh_0001", "banner_refresh_0002"]
            assert provenance_row is not None
            provenance = json.loads(provenance_row["report_json"])
            assert set(provenance) == {"format", "base", "new", "check_id", "target_ids", "policy_token"}
            assert set(provenance["base"]) == {"import_id", "export_id", "manifest_sha256", "checksum_sha256"}
            assert set(provenance["new"]) == {"import_id", "export_id", "manifest_sha256", "checksum_sha256"}
            assert provenance["format"] == "banner-refresh.v1"
            assert provenance["policy_token"] == BANNER_POLICY_TOKEN
            source_artifact = database.fetchone("SELECT sha256,metadata_json FROM artifacts WHERE kind='source_export' AND relative_ref='export/manifest.json'")
            assert source_artifact is not None
            assert source_artifact["sha256"] == replacement_check.manifest_sha256
            assert json.loads(source_artifact["metadata_json"]) == {"export_id": replacement.name}
            preserved_after_row = database.fetchone("SELECT source_json,record_json FROM variants WHERE variant_id=? AND status='selected'", (preserved_variant_id,))
            assert preserved_after_row is not None
            preserved_after = json.loads(preserved_after_row["record_json"])
            assert preserved_after["export_id"] == replacement.name
            assert preserved_after["source"]["export_id"] == replacement.name
            assert preserved_after["annotation_refs"] == preserved_before["annotation_refs"]
            assert preserved_after["override_refs"] == preserved_before["override_refs"]
            assert preserved_after["qualification_review_refs"] == preserved_before["qualification_review_refs"]
            assert preserved_after["candidate_qualification"] == preserved_before["candidate_qualification"]
            assert preserved_after["warnings"] == preserved_before["warnings"]
            for key, value in preserved_before.items():
                if key == "export_id":
                    continue
                if key == "source":
                    assert {nested_key: nested_value for nested_key, nested_value in preserved_after["source"].items() if nested_key != "export_id"} == {nested_key: nested_value for nested_key, nested_value in value.items() if nested_key != "export_id"}
                else:
                    assert preserved_after[key] == value
            assert json.loads(preserved_after_row["source_json"])["export_id"] == replacement.name
            selected_records = database.fetchall("SELECT variant_id,record_json FROM variants WHERE status='selected'")
            for row in selected_records:
                record = json.loads(row["record_json"])
                assert record["export_id"] == replacement.name
                assert record["source"]["export_id"] == replacement.name
            profile = service.worker._run_profile(database, run_id)
            banner_jobs = database.fetchall("SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND logical_key LIKE 'banner_refresh_%' ORDER BY logical_key", (run_id,))
            assert len(banner_jobs) == 3
            for job in banner_jobs:
                cursor = json.loads(job["cursor_json"])
                rebuilt_signature, payload = _batch_input_signature(database, list(cursor["variant_ids"]), profile, run_id=run_id)
                preview = service.worker.build_ai_preview(database, job)
                assert rebuilt_signature == job["input_signature"] == cursor["input_hash"] == cursor["payload_signature"]
                assert preview["input_signature"] == rebuilt_signature
                assert preview["payload_signature"] == rebuilt_signature
                assert payload["export_id"] == replacement.name
        plan = service.preview_ai_plan(run_id)
        assert plan["count"] == 3
        assert [job["logical_key"] for job in plan["jobs"]] == ["banner_refresh_0000", "banner_refresh_0001", "banner_refresh_0002"]
    finally:
        service.close(timeout=5)


def test_banner_refresh_transaction_failure_restores_and_can_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, run_id, original, workspace, _base_check = _service_fixture(tmp_path)
    replacement, replacement_check = _prepare_refresh(service, run_id, original, workspace, tmp_path)
    monkeypatch.setattr(service.imports, "resolve_checked_snapshot", lambda _check_id: (replacement_check, replacement))
    original_transaction = WorkspaceDatabase.transaction
    monkeypatch.setattr(WorkspaceDatabase, "transaction", lambda self: (_ for _ in ()).throw(RuntimeError("injected transaction failure")))
    try:
        with pytest.raises(R3Error) as failure:
            service.refresh_banner_export(run_id, check_id="banner-check", expected_base_export_id=original.name, target_ids=list(BANNER_TARGET_IDS), confirm=True)
        assert failure.value.code == "BANNER_REFRESH_FAILED"
    finally:
        monkeypatch.setattr(WorkspaceDatabase, "transaction", original_transaction)
    with service.worker.open_database(run_id) as database:
        current_import = database.fetchone("SELECT export_id FROM imports WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)", (run_id,))
        assert current_import is not None and current_import["export_id"] == original.name
    try:
        assert service.refresh_banner_export(run_id, check_id="banner-check", expected_base_export_id=original.name, target_ids=list(BANNER_TARGET_IDS), confirm=True)["new_export_id"] == replacement.name
    finally:
        service.close(timeout=5)


def test_banner_refresh_schema_inventory_allows_only_manifest_schema_hash_transition(tmp_path: Path) -> None:
    service, run_id, original, workspace, _base_check = _service_fixture(tmp_path)
    replacement, replacement_check = _prepare_refresh(service, run_id, original, workspace, tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            current_import = database.fetchone("SELECT manifest_sha256,checksum_sha256,expected_files_json FROM imports WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)", (run_id,))
            assert current_import is not None
            from blockpedia.importer import ImportCheck
            base_check = ImportCheck(check_id="base", minecraft_version="26.2", export_id=original.name, source_directory_ref="", manifest_sha256=current_import["manifest_sha256"], checksum_sha256=current_import["checksum_sha256"], snapshot_ref="", snapshot_root_sha256="", metadata_sha256="", expected_files=tuple(json.loads(current_import["expected_files_json"])), status="passed", issues=(), can_import=True)
            base_package = _package(_workspace_file_map(workspace, base_check))
        replacement_package = _package(_checked_file_map(replacement, replacement_check))
        allowed = deepcopy(replacement_package)
        for item in allowed["manifest"]["schema_inventory"]:
            if item["schema_id"] == "export-manifest.v1":
                item["schema_sha256"] = "sha256:" + "a" * 64
        _compare_exports(base_package, allowed, BANNER_TARGET_IDS, original.name, replacement.name)
        rejected = deepcopy(allowed)
        for item in rejected["manifest"]["schema_inventory"]:
            if item["schema_id"] != "export-manifest.v1":
                item["schema_sha256"] = "sha256:" + "b" * 64
                break
        with pytest.raises(BannerRefreshFailure):
            _compare_exports(base_package, rejected, BANNER_TARGET_IDS, original.name, replacement.name)
    finally:
        service.close(timeout=5)


def test_banner_refresh_rejects_wrong_scope_non_target_diff_and_live_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, run_id, original, workspace, _base_check = _service_fixture(tmp_path)
    replacement, replacement_check = _prepare_refresh(service, run_id, original, workspace, tmp_path)
    monkeypatch.setattr(service.imports, "resolve_checked_snapshot", lambda _check_id: (replacement_check, replacement))
    try:
        with pytest.raises(R3Error) as wrong_base:
            service.refresh_banner_export(run_id, check_id="banner-check", expected_base_export_id="export_stale", target_ids=list(BANNER_TARGET_IDS), confirm=True)
        assert wrong_base.value.code == "BANNER_REFRESH_BASE_MISMATCH"
        with pytest.raises(R3Error) as wrong_targets:
            service.refresh_banner_export(run_id, check_id="banner-check", expected_base_export_id=original.name, target_ids=list(BANNER_TARGET_IDS[:-1]), confirm=True)
        assert wrong_targets.value.code == "BANNER_REFRESH_TARGET_SET_INVALID"
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT job_id FROM jobs WHERE run_id=? LIMIT 1", (run_id,)).fetchone()
                assert row is not None
                connection.execute("UPDATE jobs SET status='running' WHERE job_id=?", (row["job_id"],))
        with pytest.raises(R3Error) as live:
            service.refresh_banner_export(run_id, check_id="banner-check", expected_base_export_id=original.name, target_ids=list(BANNER_TARGET_IDS), confirm=True)
        assert live.value.code == "BANNER_REFRESH_LIVE_WORK"
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                connection.execute("UPDATE jobs SET status='pending' WHERE run_id=? AND status='running'", (run_id,))

        mutated = tmp_path / "export_20260817T120002Z"
        shutil.copytree(replacement, mutated)
        blocks = _jsonl(mutated / "blocks.jsonl")
        blocks[0]["translation_key"] = "minecraft:tampered"
        _write_jsonl(mutated / "blocks.jsonl", blocks)
        manifest = json.loads((mutated / "manifest.json").read_text(encoding="utf-8"))
        manifest["export_id"] = mutated.name
        (mutated / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        expected, checksum = _rechecksum(mutated)
        manifest_hash = next(item["sha256"] for item in expected if item["relative_ref"] == "manifest.json")
        from blockpedia.importer import ImportCheck
        mutated_check = ImportCheck(check_id="banner-check-mutated", minecraft_version="26.2", export_id=mutated.name, source_directory_ref="", manifest_sha256=manifest_hash, checksum_sha256=checksum, snapshot_ref="", snapshot_root_sha256="", metadata_sha256="", expected_files=tuple(expected), status="passed", issues=(), can_import=True)
        monkeypatch.setattr(service.imports, "resolve_checked_snapshot", lambda _check_id: (mutated_check, mutated))
        with pytest.raises(R3Error) as non_target:
            service.refresh_banner_export(run_id, check_id="banner-check-mutated", expected_base_export_id=original.name, target_ids=list(BANNER_TARGET_IDS), confirm=True)
        assert non_target.value.code == "BANNER_REFRESH_MACHINE_DIFF"
    finally:
        service.close(timeout=5)


def test_banner_refresh_api_body_is_strict(tmp_path: Path) -> None:
    service = StudioService(DataRoot(tmp_path))
    from fastapi.testclient import TestClient

    try:
        with TestClient(create_app(data_root=DataRoot(tmp_path), service=service, start_worker=False)) as client:  # type: ignore[arg-type]
            payload = {"check_id": "check", "expected_base_export_id": "export_20260817T120000Z", "target_ids": list(BANNER_TARGET_IDS), "confirm": True}
            unknown = client.post("/api/runs/run/banner-export-refresh", json={**payload, "unknown": True})
            assert unknown.status_code == 400
            assert unknown.json()["error_code"] == "INVALID_INPUT"
            wrong_type = client.post("/api/runs/run/banner-export-refresh", json={**payload, "confirm": "true"})
            assert wrong_type.status_code == 400
            assert wrong_type.json()["error_code"] == "INVALID_INPUT"
    finally:
        service.close(timeout=5)
