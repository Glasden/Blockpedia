from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

import blockpedia.releases as releases_module
import blockpedia.storage as storage_module
from blockpedia.paths import DataRoot
from blockpedia.provider import ProviderProfile, SecretResolver
from blockpedia.releases import ReleaseBuildFailure, _linux_rename_noreplace, _remove_exact_staging
from blockpedia.services import R3Error, StudioService
from blockpedia.schema import validate_record

from .test_pipeline_review import _FakeProvider, _Keyring, _approve_first, _r2_fixture_module, _service


def _ready(tmp_path: Path, confidence: float = 0.90):
    service, run_id, _ = _service(tmp_path, _FakeProvider(confidence=confidence))
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
    check = service.check_import(export, "26.2")
    imported = service.import_checked(check.check_id)
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


def _checksums(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (root / "checksums.sha256").read_text(encoding="ascii").splitlines():
        digest, relative = line.split("  ", 1)
        result[relative] = digest
    return result


def test_candidate_build_layout_index_hashes_and_boundary(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    current.write_bytes(b"current-fixture\n")
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        assert checked["status"] == "passed"
        assert len(report["items"]) == 12
        assert report["items"][-1]["status"] == "not_run"
        assert all(item["status"] == "passed" for item in report["items"][:11])

        built = service.build_candidate_release(checked["check_id"])
        release = tmp_path / built["relative_path"]
        assert {path.name for path in release.iterdir()} == {
            "release.json",
            "manifest.json",
            "index.sqlite3",
            "previews",
            "quality_report.json",
            "manual-overrides.json",
            "schemas.sha256",
            "checksums.sha256",
        }
        checksums = _checksums(release)
        actual = {
            path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in release.rglob("*")
            if path.is_file() and path.name != "checksums.sha256"
        }
        assert checksums == actual
        validate_record("release-manifest.v1", json.loads((release / "manifest.json").read_text(encoding="utf-8")))
        validate_record("release.v1", json.loads((release / "release.json").read_text(encoding="utf-8")))
        index = sqlite3.connect(release / "index.sqlite3")
        try:
            assert index.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert index.execute("PRAGMA foreign_key_check").fetchone() is None
            assert index.execute("SELECT format_version FROM schema_meta").fetchall() == [(1,)]
            tables = {row[0] for row in index.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            assert not tables.intersection({"jobs", "provider_requests", "logs", "review_tasks", "audit_events"})
            assert "search_fts" in tables or "search_text" in tables
        finally:
            index.close()
        assert not list(release.glob("index.sqlite3-*"))
        assert not list(release.parent.glob(f".{built['release_id']}.staging"))
        assert not list(release.parent.glob(f"{built['release_id']}.staging"))
        assert current.read_bytes() == b"current-fixture\n"
        assert service.get_run(run_id)["boundary_event"] == "R3_CANDIDATE_BUILT_ACTIVATION_PENDING"
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_ALREADY_BUILT"
    finally:
        service.close()


def test_blocked_check_is_completed_and_hash_manifest_not_run(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'").fetchone()
                record = json.loads(row["record_json"])
                record["candidate_qualification"] = "excluded"
                connection.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(record, sort_keys=True),))
        checked = service.check_candidate_release(run_id, "26.2")
        assert checked["status"] == "passed"
        assert checked["can_build"] is False
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        assert report["status"] == "blocked"
        assert report["items"][2]["status"] == "failed"
        assert report["items"][11]["status"] == "not_run"
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_CHECK_NOT_READY"
    finally:
        service.close()


def test_stale_mutation_and_pre_rename_failure_leave_no_final(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        with service.worker.open_database(run_id) as database:
            image = database.path.parent / "renders" / "minecraft" / "stone" / "preview.png"
        image.write_bytes(image.read_bytes() + b"mutation")
        with pytest.raises(R3Error) as stale:
            service.build_candidate_release(checked["check_id"])
        assert stale.value.code == "RELEASE_CHECK_STALE"
        assert not list((tmp_path / "releases" / "26.2").glob("rel_*"))
    finally:
        service.close()

    service, run_id = _ready(tmp_path / "pre-rename")
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        service.release_builder.pre_rename_hook = lambda _staging, _final: (_ for _ in ()).throw(RuntimeError("injected"))
        with pytest.raises(R3Error) as failure:
            service.build_candidate_release(checked["check_id"])
        assert failure.value.code == "RELEASE_BUILD_FAILED"
        assert not list((tmp_path / "pre-rename" / "releases" / "26.2").glob("rel_*"))
    finally:
        service.close()


def test_hardlink_artifact_is_rejected(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        with service.worker.open_database(run_id) as database:
            workspace = database.path.parent / "renders" / "minecraft" / "stone"
        mask = workspace / "mask.png"
        preview = workspace / "preview.png"
        mask.unlink()
        try:
            mask.hardlink_to(preview)
        except (OSError, NotImplementedError):
            pytest.skip("hardlink creation is unavailable")
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code in {"RELEASE_CHECK_STALE", "RELEASE_BUILD_INTEGRITY_FAILED"}
        assert not list((tmp_path / "releases" / "26.2").glob("rel_*"))
    finally:
        service.close()


def test_committed_release_recovers_after_cache_update_failure(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        original = service.release_builder._mark_built
        calls = {"count": 0}

        def fail_once(_state, _release_id):
            if calls["count"] == 0:
                calls["count"] += 1
                raise R3Error("RELEASE_BUILD_FAILED")
            return original(_state, _release_id)

        monkeypatch.setattr(service.release_builder, "_mark_built", fail_once)
        with pytest.raises(R3Error) as first:
            service.build_candidate_release(checked["check_id"])
        assert first.value.code == "RELEASE_BUILD_FAILED"
        candidates = list((tmp_path / "releases" / "26.2").glob("rel_*"))
        assert len(candidates) == 1

        monkeypatch.setattr(service.release_builder, "_mark_built", original)
        recovered = service.build_candidate_release(checked["check_id"])
        assert recovered["release_id"] == candidates[0].name
        assert service.get_run(run_id)["boundary_event"] == "R3_CANDIDATE_BUILT_ACTIVATION_PENDING"
        assert json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "state.json").read_text(encoding="utf-8"))["status"] == "built"
        assert len(list((tmp_path / "releases" / "26.2").glob("rel_*"))) == 1
    finally:
        service.close()


def test_machine_truth_projection_mismatch_blocks_candidate(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT record_json FROM blocks WHERE block_id='minecraft:stone'").fetchone()
                record = json.loads(row["record_json"])
                record["machine_facts"]["has_item"] = False
                connection.execute("UPDATE blocks SET record_json=? WHERE block_id='minecraft:stone'", (json.dumps(record, sort_keys=True),))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        item = next(item for item in report["items"] if item["code"] == "MACHINE_SCHEMA_VALID")
        assert item["status"] == "failed"
        assert item["error_code"] == "MACHINE_FACTS_MISMATCH"
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_CHECK_NOT_READY"
    finally:
        service.close()


def test_provider_lineage_store_and_artifact_inputs_are_gated(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT request_id,envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1").fetchone()
                envelope = json.loads(row["envelope_json"])
                envelope["store"] = True
                connection.execute("UPDATE provider_requests SET envelope_json=? WHERE request_id=?", (json.dumps(envelope, sort_keys=True), row["request_id"]))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed"
        assert item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_chat_candidate_preserves_adapter_lineage(tmp_path: Path) -> None:
    service, run_id = _ready_with_adapter(tmp_path, "openai_chat_completions")
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        assert checked["can_build"] is True
        built = service.build_candidate_release(checked["check_id"])
        release = tmp_path / built["relative_path"]
        manifest = json.loads((release / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["provider_snapshot"]["adapter"] == "openai_chat_completions"
        with service.worker.open_database(run_id) as database:
            request = database.fetchone("SELECT envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1")
            assert request is not None
            envelope = json.loads(request["envelope_json"])
        assert envelope["adapter"] == "openai_chat_completions"
        assert "store" not in envelope
    finally:
        service.close()


def test_prompt_v2_release_current_input_replay_requires_frozen_prompt_version(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready_with_adapter(tmp_path, "openai_responses", prompt_version="prompt.v2")
    try:
        with service.worker.open_database(run_id) as database:
            request = database.fetchone("SELECT envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1")
            assert request is not None
            envelope = json.loads(request["envelope_json"])
            assert envelope["prompt_version"] == "prompt.v2"

        matching = service.check_candidate_release(run_id, "26.2")
        assert matching["can_build"] is True

        original_safe_prompt = releases_module.safe_prompt

        def legacy_reconstruction(metadata, tiles, prompt_version=None):
            del prompt_version
            return original_safe_prompt(metadata, tiles, prompt_version="prompt.v1")

        monkeypatch.setattr(releases_module, "safe_prompt", legacy_reconstruction)
        legacy = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / legacy["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed" and item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_provider_envelope_adapter_mutation_is_rejected(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT request_id,envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1").fetchone()
                envelope = json.loads(row["envelope_json"])
                envelope["adapter"] = "openai_chat_completions"
                envelope.pop("store")
                connection.execute("UPDATE provider_requests SET envelope_json=? WHERE request_id=?", (json.dumps(envelope, sort_keys=True), row["request_id"]))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed" and item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_chat_envelope_with_store_is_rejected(tmp_path: Path) -> None:
    service, run_id = _ready_with_adapter(tmp_path, "openai_chat_completions")
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT request_id,envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1").fetchone()
                envelope = json.loads(row["envelope_json"])
                envelope["store"] = False
                connection.execute("UPDATE provider_requests SET envelope_json=? WHERE request_id=?", (json.dumps(envelope, sort_keys=True), row["request_id"]))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed" and item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_responses_envelope_without_store_is_rejected(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT request_id,envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1").fetchone()
                envelope = json.loads(row["envelope_json"])
                envelope.pop("store")
                connection.execute("UPDATE provider_requests SET envelope_json=? WHERE request_id=?", (json.dumps(envelope, sort_keys=True), row["request_id"]))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed" and item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_cross_adapter_snapshot_lineage_is_rejected(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT config_snapshot_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
                config = json.loads(row["config_snapshot_json"])
                config["provider_snapshot"]["adapter"] = "openai_chat_completions"
                config["provider_snapshot"]["profile"]["adapter"] = "openai_chat_completions"
                config["capabilities"]["adapter"] = "openai_chat_completions"
                connection.execute("UPDATE runs SET config_snapshot_json=? WHERE run_id=?", (json.dumps(config, sort_keys=True), run_id))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed" and item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_missing_adapter_lineage_is_not_defaulted(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT config_snapshot_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
                config = json.loads(row["config_snapshot_json"])
                config["provider_snapshot"].pop("adapter", None)
                config["provider_snapshot"]["profile"].pop("adapter", None)
                config["capabilities"].pop("adapter", None)
                connection.execute("UPDATE runs SET config_snapshot_json=? WHERE run_id=?", (json.dumps(config, sort_keys=True), run_id))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed" and item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


@pytest.mark.parametrize("mutation", ["missing_request", "mismatched_request", "noncanonical_cache", "missing_artifact", "mismatched_annotation", "current_image"])
def test_provider_request_artifact_annotation_and_current_input_lineage_are_exact(tmp_path: Path, mutation: str) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                if mutation == "missing_request":
                    connection.execute("DELETE FROM provider_requests")
                elif mutation == "mismatched_request":
                    connection.execute("UPDATE provider_requests SET cache_key=?", ("sha256:" + "0" * 64,))
                elif mutation == "noncanonical_cache":
                    job = connection.execute("SELECT job_id,input_signature FROM jobs WHERE stage='AI_ANNOTATE' LIMIT 1").fetchone()
                    material = json.dumps({"job": job["job_id"], "input": job["input_signature"]}, sort_keys=True, separators=(",", ":")).encode("utf-8")
                    fallback = "sha256:" + hashlib.sha256(material).hexdigest()
                    connection.execute("UPDATE provider_requests SET cache_key=?", (fallback,))
                elif mutation == "missing_artifact":
                    connection.execute("DELETE FROM artifacts WHERE kind='ai_annotation'")
                elif mutation == "mismatched_annotation":
                    row = connection.execute("SELECT annotation_id,record_json FROM annotations LIMIT 1").fetchone()
                    record = json.loads(row["record_json"])
                    record["summary_en"] = "tampered annotation"
                    connection.execute("UPDATE annotations SET record_json=? WHERE annotation_id=?", (json.dumps(record), row["annotation_id"]))
                else:
                    image = database.path.parent / "renders" / "minecraft" / "stone" / "preview.png"
                    image.write_bytes(image.read_bytes() + b"current-input-mutation")
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text())
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed"
        assert item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


@pytest.mark.parametrize("mutation", [
    "request_id", "profile_id", "stage", "wire_schema_id", "schema_version", "model_id",
    "base_url_stable_id", "secret_reference", "prompt_version", "search_ranking_version",
    "minecraft_version", "export_id", "input_summary", "request_row_id", "request_row_profile",
    "request_row_query_stage",
])
def test_provider_envelope_and_request_snapshot_fields_are_exact(tmp_path: Path, mutation: str) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                row = connection.execute("SELECT request_id,envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1").fetchone()
                envelope = json.loads(row["envelope_json"])
                if mutation == "request_id":
                    envelope["request_id"] = "Req_tampered"
                elif mutation == "profile_id":
                    envelope["profile_id"] = "other-profile"
                elif mutation == "stage":
                    envelope["stage"] = "query_spec"
                elif mutation == "wire_schema_id":
                    envelope["wire_schema_id"] = "query-spec-output.v1"
                elif mutation == "schema_version":
                    envelope["schema_version"] = "provider-batch-envelope.tampered"
                elif mutation == "model_id":
                    envelope["model_id"] = "other-model"
                elif mutation == "base_url_stable_id":
                    envelope["base_url_stable_id"] = "other-base-url"
                elif mutation == "secret_reference":
                    envelope["secret_reference"] = "env:OPENAI_API_KEY"
                elif mutation == "prompt_version":
                    envelope["prompt_version"] = "prompt.tampered"
                elif mutation == "search_ranking_version":
                    envelope["search_ranking_version"] = "ranking.tampered"
                elif mutation == "minecraft_version":
                    envelope["minecraft_version"] = "26.3"
                elif mutation == "export_id":
                    envelope["export_id"] = "export_20260815T120000Z"
                elif mutation == "input_summary":
                    envelope["input_summary"]["tile_variant_map"][0]["image_sha256"] = "sha256:" + "0" * 64
                elif mutation == "request_row_id":
                    connection.execute("UPDATE provider_requests SET request_id=? WHERE request_id=?", ("Req_row_tampered", row["request_id"]))
                    row = None
                elif mutation == "request_row_profile":
                    connection.execute(
                        "INSERT INTO provider_profiles(profile_id,model_id,base_url_stable_id,secret_reference,active,capability_status,profile_json) VALUES (?,?,?,?,0,'verified','{}')",
                        ("other-profile", "other-model", "other-base-url", "env:OPENAI_API_KEY"),
                    )
                    connection.execute("UPDATE provider_requests SET profile_id=? WHERE request_id=?", ("other-profile", row["request_id"]))
                    row = None
                else:
                    connection.execute("UPDATE provider_requests SET stage='query_spec',wire_schema_id='query-spec-output.v1' WHERE request_id=?", (row["request_id"],))
                    row = None
                if row is not None:
                    connection.execute("UPDATE provider_requests SET envelope_json=? WHERE request_id=?", (json.dumps(envelope, sort_keys=True), row["request_id"]))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text())
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed"
        assert item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_snapshot_fingerprint_covers_export_jsonl_and_ai_artifact(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        with service.worker.open_database(run_id) as database:
            export_states = database.path.parent / "export" / "states.jsonl"
            ai_row = database.fetchone("SELECT relative_ref FROM artifacts WHERE kind='ai_annotation' LIMIT 1")
            assert ai_row is not None
            ai_artifact = database.path.parent / str(ai_row["relative_ref"])
        export_states.write_bytes(export_states.read_bytes() + b"\n")
        ai_artifact.write_bytes(ai_artifact.read_bytes() + b"\n")
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_CHECK_STALE"
        assert not list((tmp_path / "releases" / "26.2").glob("rel_*"))
    finally:
        service.close()


def test_staging_revalidation_rejects_post_build_mutation(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")

        def mutate(staging: Path, _final: Path) -> None:
            manifest = staging / "manifest.json"
            manifest.write_bytes(manifest.read_bytes() + b" ")

        service.release_builder.pre_rename_hook = mutate
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_BUILD_INTEGRITY_FAILED"
        assert not list((tmp_path / "releases" / "26.2").glob("rel_*"))
        assert not list((tmp_path / "releases" / "26.2").glob(".rel_*.staging"))
    finally:
        service.close()


def test_release_quality_report_uses_gate_observations_and_hash_evidence(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        check_report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text(encoding="utf-8"))
        built = service.build_candidate_release(checked["check_id"])
        release = tmp_path / built["relative_path"]
        release_report = json.loads((release / "quality_report.json").read_text(encoding="utf-8"))
        assert all(item["status"] == "passed" for item in release_report["items"])
        assert release_report["items"][-1]["observed_count"] > 0
        assert release_report["items"][-1]["evidence"] == ["manifest.json", "checksums.sha256"]
        for check_item, release_item in zip(check_report["items"][:11], release_report["items"][:11]):
            assert release_item["observed_count"] == check_item["observed_count"]
        assert all((release / ref).is_file() for item in release_report["items"] for ref in item["evidence"])
    finally:
        service.close()


def test_deterministic_final_recovers_after_postrename_validation_failure(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        assert json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "state.json").read_text())["release_id"] is None
        original = service.release_builder._validate_final
        calls = {"count": 0}

        def fail_once(final, release_id, snapshot, state, *, root_for_components, after_commit=True):
            calls["count"] += 1
            if calls["count"] == 2 and after_commit:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=True)
            return original(final, release_id, snapshot, state, root_for_components=root_for_components, after_commit=after_commit)

        monkeypatch.setattr(service.release_builder, "_validate_final", fail_once)
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_BUILD_INTEGRITY_FAILED"
        expected_release = "rel_" + checked["release_build_id"].removeprefix("build_")
        assert list((tmp_path / "releases" / "26.2").glob("rel_*")) == [tmp_path / "releases" / "26.2" / expected_release]
        monkeypatch.setattr(service.release_builder, "_validate_final", original)
        recovered = service.build_candidate_release(checked["check_id"])
        assert recovered["release_id"] == expected_release
        state = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "state.json").read_text())
        assert state["status"] == "built" and state["release_id"] == expected_release
        with pytest.raises(R3Error) as extra:
            service.build_candidate_release(checked["check_id"])
        assert extra.value.code == "RELEASE_ALREADY_BUILT"
    finally:
        service.close()


def test_deterministic_final_recovers_after_read_only_failure(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        original = releases_module._make_read_only
        calls = {"count": 0}

        def fail_once(root):
            calls["count"] += 1
            if calls["count"] == 1:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=True)
            return original(root)

        monkeypatch.setattr(releases_module, "_make_read_only", fail_once)
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_BUILD_INTEGRITY_FAILED"
        recovered = service.build_candidate_release(checked["check_id"])
        assert recovered["release_id"] == "rel_" + checked["release_build_id"].removeprefix("build_")
        assert json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "state.json").read_text())["status"] == "built"
    finally:
        service.close()


def test_reconciliation_sqlite_failure_recovers_without_second_final(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        original = service._reconcile_candidate_workspace
        calls = {"count": 0}

        def fail_once(_database, _run_id, _result):
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("injected reconciliation failure")
            return original(_database, _run_id, _result)

        monkeypatch.setattr(service, "_reconcile_candidate_workspace", fail_once)
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_BUILD_FAILED"
        monkeypatch.setattr(service, "_reconcile_candidate_workspace", original)
        recovered = service.build_candidate_release(checked["check_id"])
        assert recovered["release_id"] == "rel_" + checked["release_build_id"].removeprefix("build_")
        assert len(list((tmp_path / "releases" / "26.2").glob("rel_*"))) == 1
    finally:
        service.close()


def test_workspace_commit_failure_recovers_from_existing_final(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        original_transaction = storage_module.WorkspaceDatabase.transaction
        calls = {"count": 0}

        @contextmanager
        def fail_once(database):
            with original_transaction(database) as connection:
                yield connection
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("injected workspace commit failure")

        monkeypatch.setattr(storage_module.WorkspaceDatabase, "transaction", fail_once)
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_BUILD_FAILED"
        monkeypatch.setattr(storage_module.WorkspaceDatabase, "transaction", original_transaction)
        recovered = service.build_candidate_release(checked["check_id"])
        assert recovered["release_id"] == "rel_" + checked["release_build_id"].removeprefix("build_")
        assert len(list((tmp_path / "releases" / "26.2").glob("rel_*"))) == 1
    finally:
        service.close()


def test_mark_built_postcommit_failure_recovers_as_internal_error(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        original = service.release_builder._mark_built
        calls = {"count": 0}

        def fail_once(_state, _release_id):
            calls["count"] += 1
            if calls["count"] == 1:
                raise sqlite3.OperationalError("injected cache failure")
            return original(_state, _release_id)

        monkeypatch.setattr(service.release_builder, "_mark_built", fail_once)
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_BUILD_FAILED"
        monkeypatch.setattr(service.release_builder, "_mark_built", original)
        recovered = service.build_candidate_release(checked["check_id"])
        assert recovered["release_id"] == "rel_" + checked["release_build_id"].removeprefix("build_")
    finally:
        service.close()


def test_corrupt_exact_final_fails_closed_without_second_release(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        release_id = "rel_" + checked["release_build_id"].removeprefix("build_")
        exact = tmp_path / "releases" / "26.2" / release_id
        exact.mkdir(parents=True)
        (exact / "quality_report.json").write_text("not-json", encoding="utf-8")
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_BUILD_INTEGRITY_FAILED"
        assert list((tmp_path / "releases" / "26.2").glob("rel_*")) == [exact]
        assert json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "state.json").read_text())["release_id"] is None
    finally:
        service.close()


@pytest.mark.parametrize("mutation", ["checksum", "referenced_file", "extra_file"])
def test_export_checksum_anchor_and_inventory_are_trusted_before_gate(tmp_path: Path, mutation: str) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            export_root = database.path.parent / "export"
        if mutation == "checksum":
            checksum = export_root / "checksums.sha256"
            checksum.write_bytes(checksum.read_bytes() + b"blocks.jsonl  " + b"0" * 64 + b"\n")
        elif mutation == "referenced_file":
            states = export_root / "states.jsonl"
            states.write_bytes(states.read_bytes() + b"\n")
        else:
            (export_root / "unlisted-extra.bin").write_bytes(b"extra")
        checked = service.check_candidate_release(run_id, "26.2")
        assert checked["can_build"] is False
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text())
        assert report["items"][0]["status"] == "failed"
        assert not list((tmp_path / "releases" / "26.2").glob("rel_*"))
    finally:
        service.close()


@pytest.mark.parametrize("mutation,expected_codes", [
    ("default", {"LEGAL_STATE_INVALID"}),
    ("non_default_canonical", {"VARIANT_STATE_RELATION_INVALID"}),
    ("canonical", {"VARIANT_STATE_RELATION_INVALID", "FALSE_ID_REFERENCE"}),
    ("cross_block", {"STATE_VARIANT_RELATION_INVALID", "FALSE_ID_REFERENCE"}),
    ("reciprocal", {"STATE_VARIANT_RELATION_INVALID", "VARIANT_STATE_RELATION_INVALID", "MACHINE_SCHEMA_INVALID"}),
    ("feature", {"FEATURE_TRUTH_INVALID", "FEATURE_ARTIFACT_INVALID"}),
])
def test_state_variant_relationships_and_feature_truth_are_checked(tmp_path: Path, mutation: str, expected_codes: set[str]) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                if mutation == "default":
                    row = connection.execute("SELECT record_json FROM states WHERE state_id='minecraft:stone'").fetchone()
                    record = json.loads(row["record_json"])
                    record["state_id"] = "minecraft:stone[extra=1]"
                    connection.execute(
                        "INSERT INTO states(state_id,block_id,minecraft_version,record_json,failure_id) VALUES (?,?,?,?,NULL)",
                        (record["state_id"], record["block_id"], record["minecraft_version"], json.dumps(record)),
                    )
                elif mutation == "non_default_canonical":
                    row = connection.execute("SELECT record_json FROM states WHERE state_id='minecraft:stone'").fetchone()
                    record = json.loads(row["record_json"])
                    record["state_id"] = "minecraft:stone[extra=1]"
                    record["is_default"] = False
                    connection.execute(
                        "INSERT INTO states(state_id,block_id,minecraft_version,record_json,failure_id) VALUES (?,?,?,?,NULL)",
                        (record["state_id"], record["block_id"], record["minecraft_version"], json.dumps(record)),
                    )
                    row = connection.execute("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'").fetchone()
                    variant = json.loads(row["record_json"])
                    variant["canonical_state_id"] = record["state_id"]
                    variant["represented_state_ids"] = ["minecraft:stone", record["state_id"]]
                    connection.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(variant),))
                elif mutation == "canonical":
                    row = connection.execute("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'").fetchone()
                    record = json.loads(row["record_json"])
                    record["canonical_state_id"] = "minecraft:glass"
                    connection.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(record),))
                elif mutation == "cross_block":
                    row = connection.execute("SELECT record_json FROM states WHERE state_id='minecraft:stone'").fetchone()
                    record = json.loads(row["record_json"])
                    record["variant_ids"] = ["minecraft:glass"]
                    connection.execute("UPDATE states SET record_json=? WHERE state_id='minecraft:stone'", (json.dumps(record),))
                elif mutation == "reciprocal":
                    row = connection.execute("SELECT record_json FROM states WHERE state_id='minecraft:stone'").fetchone()
                    record = json.loads(row["record_json"])
                    record["variant_ids"] = []
                    connection.execute("UPDATE states SET record_json=? WHERE state_id='minecraft:stone'", (json.dumps(record),))
                else:
                    row = connection.execute("SELECT feature_json FROM features WHERE variant_id='minecraft:stone'").fetchone()
                    feature = json.loads(row["feature_json"])
                    feature["geometry_classes"] = ["divergent"]
                    connection.execute("UPDATE features SET feature_json=? WHERE variant_id='minecraft:stone'", (json.dumps(feature),))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text())
        errors = {item["error_code"] for item in report["items"] if item["status"] == "failed"}
        assert errors.intersection(expected_codes)
    finally:
        service.close()


def test_pure_human_semantics_pass_without_provider_request(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        operations = {
            "add_synonyms_zh": ["石头"], "add_synonyms_en": ["stone"], "add_color_terms": ["gray"],
            "add_shape_terms": ["full_cube"], "add_material_impressions": ["stone"], "add_building_roles": ["wall"],
            "add_style_tags": ["simple"], "add_avoid_for": ["floor"], "set_summary_zh": "人工石头方块。",
            "set_summary_en": "A human-reviewed stone block.", "set_confidence": 1.0,
        }
        record = {
            "schema_version": "manual-override.v1", "override_id": "ov_human_complete", "minecraft_version": "26.2",
            "scope": {"level": "variant", "variant_id": "minecraft:stone"}, "operations": operations,
            "reason": "complete human semantic review", "author": "tester", "approved_by": "tester",
            "created_at": "2026-08-15T12:00:00Z", "input_signature": "sha256:" + "1" * 64,
        }
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                annotation = connection.execute("SELECT annotation_id,record_json FROM annotations WHERE subject_id='minecraft:stone'").fetchone()
                value = json.loads(annotation["record_json"])
                value["source"]["verified"] = False
                connection.execute("UPDATE annotations SET record_json=? WHERE annotation_id=?", (json.dumps(value), annotation["annotation_id"]))
                connection.execute("DELETE FROM provider_requests")
                variant = json.loads(connection.execute("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'").fetchone()["record_json"])
                variant["override_refs"] = ["ov_human_complete"]
                connection.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(variant),))
                connection.execute("INSERT INTO overrides(override_id,target_id,minecraft_version,record_json) VALUES (?,?,?,?)", ("ov_human_complete", "minecraft:stone", "26.2", json.dumps(record)))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text())
        assert report["items"][6]["status"] == "passed"
        assert checked["can_build"] is False
        assert report["items"][10]["error_code"] == "FTS_NOT_READY"
    finally:
        service.close()


def test_low_confidence_annotation_accept_transition_is_buildable(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path, confidence=0.70)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text())
        assert report["items"][6]["status"] == "passed"
        assert checked["can_build"] is True
        built = service.build_candidate_release(checked["check_id"])
        assert built["status"] == "built"
    finally:
        service.close()


def test_unreviewed_verified_transition_is_rejected(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path, confidence=0.70)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                annotation = connection.execute("SELECT annotation_id,record_json FROM annotations WHERE subject_id='minecraft:stone'").fetchone()
                record = json.loads(annotation["record_json"])
                record["source"]["verified"] = True
                connection.execute("UPDATE annotations SET record_json=? WHERE annotation_id=?", (json.dumps(record), annotation["annotation_id"]))
                connection.execute("UPDATE review_tasks SET reason_code='OTHER' WHERE target_id='minecraft:stone' AND status='resolved'")
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text())
        item = next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")
        assert item["status"] == "failed"
        assert item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_partial_human_semantics_without_verified_annotation_blocks(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                annotation = connection.execute("SELECT annotation_id,record_json FROM annotations WHERE subject_id='minecraft:stone'").fetchone()
                value = json.loads(annotation["record_json"])
                value["source"]["verified"] = False
                connection.execute("UPDATE annotations SET record_json=? WHERE annotation_id=?", (json.dumps(value), annotation["annotation_id"]))
                record = {"schema_version": "manual-override.v1", "override_id": "ov_partial", "minecraft_version": "26.2", "scope": {"level": "variant", "variant_id": "minecraft:stone"}, "operations": {"set_summary_zh": "只有部分人工语义。"}, "reason": "partial", "author": "tester", "approved_by": "tester", "created_at": "2026-08-15T12:00:00Z", "input_signature": "sha256:" + "2" * 64}
                variant = json.loads(connection.execute("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'").fetchone()["record_json"])
                variant["override_refs"] = ["ov_partial"]
                connection.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(variant),))
                connection.execute("INSERT INTO overrides(override_id,target_id,minecraft_version,record_json) VALUES (?,?,?,?)", ("ov_partial", "minecraft:stone", "26.2", json.dumps(record)))
        checked = service.check_candidate_release(run_id, "26.2")
        report = json.loads((tmp_path / "cache" / "release-checks" / checked["check_id"] / "quality_report.json").read_text())
        assert report["items"][6]["status"] == "failed"
    finally:
        service.close()


@pytest.mark.parametrize("mutation", ["search_content", "fts_branch", "duplicate_trigram", "duplicate_fallback"])
def test_final_search_projection_and_fts_branch_are_revalidated(tmp_path: Path, mutation: str) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")

        def mutate(staging: Path, _final: Path) -> None:
            index = sqlite3.connect(staging / "index.sqlite3")
            try:
                table_names = {row[0] for row in index.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                table = "search_fts" if "search_fts" in table_names else "search_text"
                if mutation == "search_content":
                    index.execute(f"UPDATE {table} SET normalized_text='tampered'")
                elif mutation == "duplicate_trigram":
                    duplicate = index.execute("SELECT variant_id,normalized_text FROM search_fts LIMIT 1").fetchone()
                    index.execute("INSERT INTO search_fts(variant_id,normalized_text) VALUES (?,?)", duplicate)
                elif mutation == "duplicate_fallback":
                    rows = index.execute("SELECT variant_id,normalized_text FROM search_fts").fetchall()
                    index.execute("DROP TABLE search_fts")
                    index.execute("CREATE TABLE search_text (variant_id TEXT, normalized_text TEXT)")
                    index.executemany("INSERT INTO search_text(variant_id,normalized_text) VALUES (?,?)", rows)
                    index.execute("INSERT INTO search_text(variant_id,normalized_text) VALUES (?,?)", rows[0])
                else:
                    manifest_path = staging / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["fts_mode"] = "normalized_like" if manifest["fts_mode"] == "trigram" else "trigram"
                    manifest_path.write_bytes(releases_module._canonical_bytes(manifest))
                    release_path = staging / "release.json"
                    release = json.loads(release_path.read_text(encoding="utf-8"))
                    release["manifest_sha256"] = releases_module.sha256_bytes(releases_module._canonical_bytes(manifest))
                    release_path.write_bytes(releases_module._canonical_bytes(release))
                if mutation == "duplicate_fallback":
                    manifest_path = staging / "manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["fts_mode"] = "normalized_like"
                    manifest_path.write_bytes(releases_module._canonical_bytes(manifest))
                    release_path = staging / "release.json"
                    release = json.loads(release_path.read_text(encoding="utf-8"))
                    release["manifest_sha256"] = releases_module.sha256_bytes(releases_module._canonical_bytes(manifest))
                    release_path.write_bytes(releases_module._canonical_bytes(release))
                index.commit()
            finally:
                index.close()
            (staging / "checksums.sha256").write_bytes(releases_module._checksum_bytes(staging))

        service.release_builder.pre_rename_hook = mutate
        with pytest.raises(R3Error) as error:
            service.build_candidate_release(checked["check_id"])
        assert error.value.code == "RELEASE_BUILD_INTEGRITY_FAILED"
        assert not list((tmp_path / "releases" / "26.2").glob("rel_*"))
    finally:
        service.close()


def test_linux_syscall_fallback_uses_raw_renameat2_noreplace(monkeypatch) -> None:
    calls = []

    def syscall(*args):
        calls.append(args)
        import ctypes
        ctypes.set_errno(38)
        return -1

    fake_libc = type("FakeLibc", (), {"syscall": staticmethod(syscall)})()
    monkeypatch.setattr(releases_module.platform, "machine", lambda: "x86_64")
    with pytest.raises(OSError):
        _linux_rename_noreplace(fake_libc, 7, "source", "target")
    assert calls == [(316, 7, b"source", 7, b"target", 1)]


def test_replaced_staging_is_not_deleted_by_cleanup(tmp_path: Path) -> None:
    original = tmp_path / (".rel_" + "0" * 32 + ".staging")
    original.mkdir()
    identity_stat = original.stat()
    identity = (identity_stat.st_dev, identity_stat.st_ino, stat.S_IFMT(identity_stat.st_mode))
    moved = tmp_path / "moved-original"
    original.rename(moved)
    original.mkdir()
    _remove_exact_staging(original, identity)
    assert original.is_dir()
