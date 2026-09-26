from __future__ import annotations
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from blockpedia.paths import DataRoot
from blockpedia.provider import ProviderProfile, SecretResolver
from blockpedia.services import R3Error, StudioService
from blockpedia.schema import validate_record
from tests.import_helpers import import_export
from .test_pipeline_review import _FakeProvider, _Keyring, _approve_first, _r2_fixture_module, _service

def _ready(tmp_path: Path, confidence: float = 0.90, *, export_path=None):
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=confidence), export_path=export_path)
    _approve_first(service, run_id)
    for _ in range(4):
        service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        failed_targets = {row[0] for row in database.fetchall("SELECT variant_id FROM failures")}
    for review in service.list_reviews(run_id):
        skipped = review["target_id"] in failed_targets
        service.resolve_review(
            run_id,
            review["review_id"],
            decision="skip" if skipped else "accept",
            reviewer="tester",
            reason_code="MISSING_TEXTURE" if skipped else "OTHER",
            note="fixture review",
            evidence=["fixture:review"],
        )
    service.continue_review(run_id)
    service.tick(run_id)
    return service, run_id

def _ready_with_adapter(tmp_path: Path, adapter: str, prompt_version: str = "prompt.v1"):
    fixture = _r2_fixture_module()
    export = fixture.make_export(tmp_path)
    service = StudioService(
        DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        toolchain_probe=fixture.PassingToolchainProbe(),
        provider_factory=lambda profile, **_kwargs: _FakeProvider(),
        secret_resolver=SecretResolver(keyring_backend=_Keyring()),
    )
    imported = import_export(service, export)
    run_id = imported["run_id"]
    for _ in range(6):
        service.tick(run_id)
    profile = ProviderProfile(
        profile_id="default",
        model_id="fixture-model",
        adapter=adapter,
        base_url="http://127.0.0.1:8766/v1",
        prompt_version=prompt_version,
    )
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
    _approve_first(service, run_id)
    for _ in range(4):
        service.tick(run_id)
    for review in service.list_reviews(run_id):
        skipped = review["target_id"] == "minecraft:glass"
        service.resolve_review(
            run_id,
            review["review_id"],
            decision="skip" if skipped else "accept",
            reviewer="tester",
            reason_code="MISSING_TEXTURE" if skipped else "OTHER",
            note="fixture review",
            evidence=["fixture:review"],
        )
    service.continue_review(run_id)
    service.tick(run_id)
    return service, run_id

BUILD = "build_" + "a" * 32


def test_database_results_build_without_generated_or_source_files(tmp_path, monkeypatch):
    service, run_id = _ready(tmp_path)
    try:
        workspace = service.data_root.workspace_dir("26.2", run_id)
        assert not list(workspace.glob("generated/**/*.json"))
        shutil.rmtree(workspace / "export")
        with service.worker.open_database(run_id) as db:
            db.execute("INSERT INTO artifacts(artifact_id,kind,relative_ref,sha256,metadata_json) VALUES ('legacy','feature_output','generated/missing.json','old','{}')")
            before = [dict(row) for row in db.fetchall("SELECT * FROM provider_requests")]
        service.disable("default")
        built = service.build_candidate_release(run_id, "26.2", BUILD)
        root = tmp_path / built["relative_path"]
        assert {p.name for p in root.iterdir()} == {"release.json", "manifest.json", "index.sqlite3", "previews", "quality_report.json", "manual-overrides.json"}
        assert not service.data_root.current.exists()
        assert not (service.data_root.cache / "release-checks").exists()
        validate_record("release.v1", json.loads((root / "release.json").read_text()))
        validate_record("release-manifest.v1", json.loads((root / "manifest.json").read_text()))
        report = json.loads((root / "quality_report.json").read_text())
        assert report["status"] == "passed" and "snapshot_fingerprint" not in report
        with sqlite3.connect(root / "index.sqlite3") as c:
            assert c.execute("SELECT COUNT(*) FROM blocks").fetchone() == (2,)
            assert c.execute("SELECT COUNT(*) FROM states").fetchone() == (2,)
            assert c.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
        with service.worker.open_database(run_id) as db:
            assert before == [dict(row) for row in db.fetchall("SELECT * FROM provider_requests")]
        again = service.build_candidate_release(run_id, "26.2", BUILD)
        assert again["reused"] and again["release_id"] == built["release_id"]
        with service.worker.open_database(run_id) as db:
            assert db.fetchone("SELECT COUNT(*) AS n FROM audit_events WHERE event_type='RELEASE_BUILT'")["n"] == 1
    finally:
        service.close()


@pytest.mark.parametrize("point", ["before_rename", "after_rename"])
def test_build_recovery_reuses_exact_final(tmp_path, monkeypatch, point):
    service, run_id = _ready(tmp_path)
    try:
        def fail(*args):
            raise OSError("injected failure")
        if point == "before_rename":
            monkeypatch.setattr(service.release_builder, "pre_rename_hook", fail)
        else:
            original = service.release_builder._finish
            monkeypatch.setattr(service.release_builder, "_finish", fail)
        with pytest.raises(R3Error):
            service.build_candidate_release(run_id, "26.2", BUILD)
        final = service.data_root.release_dir("26.2", "rel_" + "a" * 32)
        assert final.exists() == (point == "after_rename")
        if point == "before_rename":
            monkeypatch.setattr(service.release_builder, "pre_rename_hook", None)
        else:
            monkeypatch.setattr(service.release_builder, "_finish", original)
        result = service.build_candidate_release(run_id, "26.2", BUILD)
        assert result["release_id"] == final.name
        assert len(list(final.parent.glob("rel_*"))) == 1
        assert not list(final.parent.glob(".*.staging"))
    finally:
        service.close()


@pytest.mark.parametrize("problem", ["missing_skip", "excluded", "state", "review"])
def test_build_still_checks_business_content(tmp_path, problem):
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as db:
            if problem == "missing_skip":
                db.execute("DELETE FROM overrides WHERE target_id='minecraft:glass'")
            elif problem == "excluded":
                row = db.fetchone("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'")
                record = json.loads(row[0]);record["candidate_qualification"] = "excluded"
                db.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(record),))
            elif problem == "state":
                record = json.loads(db.fetchone("SELECT record_json FROM states WHERE state_id='minecraft:stone'")[0])
                record["properties"] = {"unknown": "true"}
                db.execute("UPDATE states SET record_json=? WHERE state_id='minecraft:stone'", (json.dumps(record),))
            else:
                db.execute("UPDATE review_tasks SET status='open' WHERE target_id='minecraft:glass'")
        with pytest.raises(R3Error):
            service.build_candidate_release(run_id, "26.2", BUILD)
        assert not service.data_root.current.exists()
        assert not list(service.data_root.releases.glob("*/rel_*"))
    finally:
        service.close()


@pytest.mark.parametrize("adapter", ["openai_responses", "openai_chat_completions"])
def test_frozen_provider_lineage_survives_build(tmp_path, adapter):
    service, run_id = _ready_with_adapter(tmp_path, adapter)
    try:
        result = service.build_candidate_release(run_id, "26.2", BUILD)
        manifest = json.loads((tmp_path / result["relative_path"] / "manifest.json").read_text())
        assert manifest["provider_snapshot"]["adapter"] == adapter
        assert manifest["provider_snapshot"]["model_id"] == "fixture-model"
    finally:
        service.close()


def test_interrupted_build_cannot_be_replaced_by_another_id(tmp_path, monkeypatch):
    service, run_id = _ready(tmp_path)
    try:
        service.build_candidate_release(run_id, '26.2', BUILD)
        original = service.release_builder._finish
        def interrupted(*args):
            raise OSError('after rename')
        monkeypatch.setattr(service.release_builder, '_finish', interrupted)
        second = 'build_' + 'b' * 32
        with pytest.raises(R3Error):
            service.build_candidate_release(run_id, '26.2', second)
        monkeypatch.setattr(service.release_builder, '_finish', original)
        with service.worker.open_database(run_id) as db:
            before = dict(db.fetchone("SELECT * FROM stage_runs WHERE stage='BUILD_RELEASE'"))
        assert service.build_candidate_release(run_id, '26.2', BUILD)['reused']
        with pytest.raises(R3Error) as busy:
            service.build_candidate_release(run_id, '26.2', 'build_' + 'c' * 32)
        assert busy.value.code == 'RELEASE_BUSY'
        with service.worker.open_database(run_id) as db:
            assert dict(db.fetchone("SELECT * FROM stage_runs WHERE stage='BUILD_RELEASE'")) == before
        assert service.build_candidate_release(run_id, '26.2', second)['reused']
    finally:
        service.close()
