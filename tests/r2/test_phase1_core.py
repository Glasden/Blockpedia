from __future__ import annotations

import json
import platform
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from blockpedia.features import axis_aligned_union, extract_features
from blockpedia.importer import ImportNotAllowed
from blockpedia.paths import DataRoot, ExportPathError, UnsafeReference, default_data_root
from blockpedia.search import WorkspaceQueryService
from blockpedia.services import StudioService
from blockpedia.stages import RunStateConflict
from blockpedia.storage import DatabaseSchemaMismatch, WorkspaceDatabase, utc_now
from blockpedia.toolchain import ToolchainProbe


def test_cross_platform_defaults_and_safe_refs(tmp_path: Path) -> None:
    assert default_data_root(platform="win32", environ={"LOCALAPPDATA": r"C:\Users\tester"}).as_posix().endswith("Blockpedia/data")
    assert default_data_root(platform="linux", environ={"HOME": "/home/tester"}).as_posix().endswith(".local/share/blockpedia")
    root = DataRoot(tmp_path)
    assert root.relative_ref(tmp_path / "workspace" / "26.2" / "run_x") == "workspace/26.2/run_x"
    with pytest.raises(UnsafeReference):
        root.resolve_ref("../outside")
    with pytest.raises(UnsafeReference):
        root.resolve_ref(r"workspace\26.2\run_x")


def test_import_projection_validator_once_and_feature_boundary(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_fixture: Path) -> None:
    calls = 0
    from tools import validate_r1_export

    original = validate_r1_export.validate_export

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(validate_r1_export, "validate_export", counted)
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    assert check.can_import
    assert calls == 1
    check_dir = tmp_path / "cache" / "import-checks" / check.check_id
    state_path = check_dir / "state.json"
    metadata_path = check_dir / "metadata.json"
    assert state_path.is_file()
    assert metadata_path.is_file()
    assert not list((tmp_path / "cache" / "import-checks").glob("*.json"))
    assert "repo_root" not in state_path.read_text(encoding="utf-8")
    assert "repo_root" not in metadata_path.read_text(encoding="utf-8")
    imported = service.import_checked(check.check_id)
    run_id = imported["run_id"]
    db = service.worker.database_for(run_id)
    block_count = db.fetchone("SELECT COUNT(*) AS n FROM blocks")
    variant_count = db.fetchone("SELECT COUNT(*) AS n FROM variants")
    skipped_state_count = db.fetchone("SELECT COUNT(*) AS n FROM states WHERE failure_id IS NOT NULL")
    assert block_count is not None and block_count["n"] == 2
    assert variant_count is not None and variant_count["n"] == 1
    assert skipped_state_count is not None and skipped_state_count["n"] == 1
    for _ in range(6):
        service.tick(run_id)
    run = service.get_run(run_id)
    assert run["status"] == "paused"
    assert run["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
    assert [stage["status"] for stage in run["stages"][:6]] == ["succeeded"] * 6
    assert [stage["status"] for stage in run["stages"][6:]] == ["pending"] * 5
    assert all(stage["error_code"] is None and not stage["error_present"] for stage in run["stages"][:6])
    assert "cursor_json" not in json.dumps(run)
    assert "details_json" not in json.dumps(run)
    assert service.query_workspace(run_id, "stone")
    assert str(tmp_path) not in json.dumps(check.to_dict())
    service.close()


def test_forced_fts_fallback_and_schema_mismatch(tmp_path: Path) -> None:
    database = WorkspaceDatabase.open(tmp_path / "workspace.sqlite3", force_normalized_like=True)
    assert database.fts_mode == "normalized_like"
    database.close()
    path = tmp_path / "tampered.sqlite3"
    database = WorkspaceDatabase.open(path)
    database.close()
    with path.open("ab") as handle:
        handle.write(b"not a database mutation")
    # The SQLite file remains readable, while the packaged schema check is
    # independently exercised by changing schema_meta in a fresh connection.
    raw = sqlite3.connect(path)
    raw.execute("UPDATE schema_meta SET schema_sha256='sha256:bad'")
    raw.commit()
    raw.close()
    with pytest.raises(DatabaseSchemaMismatch):
        WorkspaceDatabase.open(path)


def test_rejects_staging_and_cross_version(tmp_path: Path, export_fixture: Path) -> None:
    root = DataRoot(tmp_path)
    with pytest.raises(ExportPathError):
        root.export_source(tmp_path / "exports" / "26.2" / ".export_20260814T120000Z.staging", "26.2")
    with pytest.raises(ExportPathError):
        root.export_source(export_fixture, "1.20")


def test_feature_determinism(export_fixture: Path) -> None:
    preview = export_fixture / "renders/minecraft/stone/preview.png"
    mask = export_fixture / "renders/minecraft/stone/mask.png"
    first = extract_features(preview, mask, geometry={"is_full_cube": True, "height": 1})
    second = extract_features(preview, mask, geometry={"is_full_cube": True, "height": 1})
    assert first == second
    assert first["feature_extractor_version"] == "features.v1"
    assert 0 < first["mask_coverage"] < 1


def test_check_uses_immutable_snapshot_after_source_changes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_fixture: Path) -> None:
    calls = 0
    from tools import validate_r1_export
    original = validate_r1_export.validate_export

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(validate_r1_export, "validate_export", counted)
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    (export_fixture / "exporter.log").write_bytes(b"changed after check\n")
    imported = service.import_checked(check.check_id)
    assert imported["status"] == "pending"
    assert calls == 1
    assert list((tmp_path / "workspace" / "26.2").glob("*/work.sqlite3"))
    assert not list((tmp_path / "workspace" / "26.2").glob(".*.staging"))
    service.close()


def test_import_rejects_tampered_check_snapshot(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    snapshot = DataRoot(tmp_path).resolve_ref(check.snapshot_ref)
    (snapshot / "exporter.log").write_bytes(b"tampered snapshot\n")
    with pytest.raises(ImportNotAllowed):
        service.import_checked(check.check_id)
    assert not list((tmp_path / "workspace" / "26.2").glob("*/work.sqlite3"))
    assert not list((tmp_path / "workspace" / "26.2").glob(".*.staging"))
    service.close()


def test_default_prepare_matches_runtime_python(tmp_path: Path, export_fixture: Path) -> None:
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2])
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    result = service.tick(run_id)
    run = service.get_run(run_id)
    if platform.python_version() == "3.14.7":
        assert result["status"] == "running"
        assert run["stages"][0]["status"] == "succeeded"
    else:
        assert result["status"] == "failed"
        assert run["stages"][0]["status"] == "failed"
        assert run["stages"][0]["error_code"] == "TOOLCHAIN_NOT_LOCKED"
        assert run["stages"][0]["error_present"] is True
    service.close()


def test_prepare_rejects_injected_wrong_python(tmp_path: Path, export_fixture: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    probe = ToolchainProbe(repo_root, python_version_getter=lambda: "3.14.3")
    service = StudioService(DataRoot(tmp_path), repo_root=repo_root, toolchain_probe=probe)
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    result = service.tick(run_id)
    run = service.get_run(run_id)
    assert result["status"] == "failed"
    assert run["stages"][0]["status"] == "failed"
    assert run["stages"][0]["error_code"] == "TOOLCHAIN_NOT_LOCKED"
    assert run["stages"][0]["error_present"] is True
    service.close()


def test_worker_start_advances_pending_run_with_injected_probe(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    service.worker.start(interval_seconds=0.01)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = service.get_run(run_id)
        if status["status"] == "paused" and status["boundary_event"]:
            break
        time.sleep(0.02)
    service.worker.stop(timeout=2)
    assert service.get_run(run_id)["boundary_event"] == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING"
    service.close()


def test_feature_failure_converges_run_and_stage(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    imported = service.import_checked(check.check_id)
    run_id = imported["run_id"]
    for _ in range(5):
        service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        preview = database.path.parent / "renders/minecraft/stone/preview.png"
        preview.unlink()
    service.tick(run_id)
    run = service.get_run(run_id)
    assert run["status"] == "failed"
    assert run["stages"][5]["status"] == "failed"
    assert run["jobs"][0]["status"] == "failed"
    service.close()


def test_non_stale_recover_and_boundary_resume_are_conflicts(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    service.tick(run_id)
    with pytest.raises(RunStateConflict):
        service.resume(run_id)
    for _ in range(5):
        service.tick(run_id)
    run = service.get_run(run_id)
    assert run["boundary_event"]
    with pytest.raises(RunStateConflict):
        service.resume(run_id)
    service.close()


def test_geometry_union_is_not_bounding_volume() -> None:
    non_overlapping = axis_aligned_union([
        {"min_x": 0, "min_y": 0, "min_z": 0, "max_x": 1, "max_y": 0.5, "max_z": 1},
        {"min_x": 0, "min_y": 0.5, "min_z": 0, "max_x": 0.5, "max_y": 1, "max_z": 1},
    ])
    overlapping = axis_aligned_union([
        {"min_x": 0, "min_y": 0, "min_z": 0, "max_x": 1, "max_y": 0.5, "max_z": 1},
        {"min_x": 0.5, "min_y": 0.25, "min_z": 0, "max_x": 1, "max_y": 0.75, "max_z": 1},
    ])
    assert non_overlapping.occupied_volume == 0.75
    assert overlapping.occupied_volume == 0.625
    assert not non_overlapping.is_full_cube
    assert not overlapping.is_full_cube


def test_provider_constraints_and_failure_foreign_key(tmp_path: Path) -> None:
    database = WorkspaceDatabase.open(tmp_path / "workspace.sqlite3")
    connection = database.connection
    connection.execute("INSERT INTO provider_profiles(profile_id,model_id,base_url_stable_id,secret_reference,active,capability_status) VALUES ('p1','m','u','s',1,'verified')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO provider_profiles(profile_id,model_id,base_url_stable_id,secret_reference,active,capability_status) VALUES ('p2','m','u','s',1,'verified')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO provider_profiles(profile_id,model_id,base_url_stable_id,secret_reference,active,capability_status) VALUES ('p3','m','u','s',1,'unverified')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO provider_requests(request_id,profile_id,stage,wire_schema_id,attempt,cache_key,input_sha256,envelope_json,status,created_at) VALUES ('r','missing','query_spec','query-spec-output.v1',1,'k','sha256:x','{}','pending','now')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO provider_requests(request_id,profile_id,stage,wire_schema_id,attempt,cache_key,input_sha256,envelope_json,status,created_at) VALUES ('r','p1','query_spec','rerank-output.v1',1,'k','sha256:x','{}','pending','now')")
    connection.execute("INSERT INTO blocks(block_id,minecraft_version,record_json) VALUES ('minecraft:test','26.2','{}')")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("INSERT INTO states(state_id,block_id,minecraft_version,record_json,failure_id) VALUES ('minecraft:test','minecraft:test','26.2','{}','missing')")
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(provider_requests)")}
    assert "response_sha256" not in columns
    database.close()


def test_property_membership_is_rejected() -> None:
    from blockpedia.importer import _check_property_membership
    with pytest.raises(ImportNotAllowed):
        _check_property_membership(
            {"properties": {"facing": ["north", "south"]}},
            {"properties": {"facing": "east"}},
        )


def test_fts_excludes_skipped_and_excluded_candidates(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    for _ in range(6):
        service.tick(run_id)
    assert service.query_workspace(run_id, "glass") == []
    with service.worker.open_database(run_id) as database:
        row = database.fetchone("SELECT record_json FROM variants WHERE variant_id='minecraft:stone'")
        assert row is not None
        record = json.loads(row["record_json"])
        record["candidate_qualification"] = "excluded"
        database.execute("UPDATE variants SET record_json=? WHERE variant_id='minecraft:stone'", (json.dumps(record),))
        WorkspaceQueryService(database).rebuild_index()
        assert WorkspaceQueryService(database).query("stone") == []
        block_count = database.fetchone("SELECT COUNT(*) AS n FROM blocks")
        assert block_count is not None and block_count["n"] == 2
    service.close()


def test_worker_pause_resume_cancel_and_explicit_stale_recovery(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    service.tick(run_id)
    assert service.pause(run_id)["status"] == "paused"
    assert service.resume(run_id)["status"] == "running"
    for _ in range(4):
        service.tick(run_id)
    database = service.worker.database_for(run_id)
    service.worker._ensure_feature_jobs(database, run_id)
    job = database.fetchone("SELECT job_id FROM jobs WHERE stage='EXTRACT_FEATURES' LIMIT 1")
    assert job is not None
    with database.transaction() as connection:
        connection.execute("UPDATE jobs SET status='running',heartbeat_at=?,auto_attempt=0,output_hash=NULL WHERE job_id=?", (utc_now(), job["job_id"]))
    service.worker.stale_after_seconds = 300
    with pytest.raises(RunStateConflict):
        service.recover(run_id, job["job_id"])
    with database.transaction() as connection:
        connection.execute("UPDATE jobs SET status='running',heartbeat_at='2000-01-01T00:00:00Z',auto_attempt=0,output_hash=NULL WHERE job_id=?", (job["job_id"],))
    service.worker.stale_after_seconds = 0
    before = database.fetchone("SELECT status,auto_attempt FROM jobs WHERE job_id=?", (job["job_id"],))
    markers = service.stale_markers(run_id)
    after = database.fetchone("SELECT status,auto_attempt FROM jobs WHERE job_id=?", (job["job_id"],))
    assert markers and before is not None and after is not None
    assert (before["status"], before["auto_attempt"]) == (after["status"], after["auto_attempt"])
    assert service.recover(run_id, job["job_id"])["recovered"]["status"] == "pending"
    with database.transaction() as connection:
        connection.execute("UPDATE jobs SET status='running',heartbeat_at='2000-01-01T00:00:00Z',auto_attempt=1 WHERE job_id=?", (job["job_id"],))
    assert service.recover(run_id, job["job_id"])["recovered"]["status"] == "needs_review"
    assert database.fetchone("SELECT 1 FROM audit_events WHERE event_type='WORKER_RECOVERED_STALE_RUNNING'") is not None
    with pytest.raises(RunStateConflict):
        service.cancel(run_id)
    service.close()


def test_stage_lease_blocks_second_worker_and_pause_after_item(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            connection.execute("UPDATE stage_runs SET status='running',worker_id=?,heartbeat_at=? WHERE run_id=? AND stage='IMPORT_EXPORT'", (service.worker.worker_id, utc_now(), run_id))
    second = service.worker.__class__(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    with second.open_database(run_id) as database:
        assert second._begin_stage(database, run_id, "IMPORT_EXPORT") is False
    service.close()
    second.close()


def test_running_feature_job_blocks_new_pending_job(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe

    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    for _ in range(5):
        service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        service.worker._ensure_feature_jobs(database, run_id)
        old_job = database.fetchone("SELECT job_id FROM jobs WHERE run_id=? AND stage='EXTRACT_FEATURES' LIMIT 1", (run_id,))
        assert old_job is not None
        with database.transaction() as connection:
            connection.execute("UPDATE runs SET status='running',current_stage='EXTRACT_FEATURES' WHERE run_id=?", (run_id,))
            connection.execute("UPDATE stage_runs SET status='running',worker_id=?,heartbeat_at=? WHERE run_id=? AND stage='EXTRACT_FEATURES'", (service.worker.worker_id, utc_now(), run_id))
            connection.execute("UPDATE jobs SET status='running',worker_id='worker_dead',heartbeat_at='2000-01-01T00:00:00Z' WHERE job_id=?", (old_job["job_id"],))
            connection.execute(
                "INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                ("job_pending_probe", run_id, "EXTRACT_FEATURES", "minecraft:pending_probe", "input-pending-probe", "pending", 0, "{}", utc_now()),
            )
    service.tick(run_id)
    with service.worker.open_database(run_id) as database:
        old_status = database.fetchone("SELECT status FROM jobs WHERE job_id=?", (old_job["job_id"],))
        pending_status = database.fetchone("SELECT status FROM jobs WHERE job_id='job_pending_probe'")
    assert old_status is not None and old_status["status"] == "running"
    assert pending_status is not None and pending_status["status"] == "pending"
    service.close()


def test_pause_waits_for_inflight_feature_item(tmp_path: Path, export_fixture: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import PassingToolchainProbe
    import blockpedia.worker as worker_module

    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    for _ in range(5):
        service.tick(run_id)
    entered = threading.Event()
    release = threading.Event()
    original = worker_module.extract_features

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(worker_module, "extract_features", blocked)
    tick_thread = threading.Thread(target=service.tick, args=(run_id,))
    tick_thread.start()
    assert entered.wait(5)
    requested = service.pause(run_id)
    assert requested["status"] == "running"
    release.set()
    tick_thread.join(timeout=5)
    run = service.get_run(run_id)
    assert run["status"] == "paused"
    assert run["stages"][5]["status"] == "paused"
    assert run["jobs"][0]["status"] == "succeeded"
    assert any(event["event_type"] == "RUN_PAUSED_AFTER_ITEM" for event in _audit_events(service, run_id))
    service.close()


def test_cancel_discards_inflight_feature_result(tmp_path: Path, export_fixture: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from conftest import PassingToolchainProbe
    import blockpedia.worker as worker_module

    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    for _ in range(5):
        service.tick(run_id)
    entered = threading.Event()
    release = threading.Event()
    original = worker_module.extract_features

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(worker_module, "extract_features", blocked)
    tick_thread = threading.Thread(target=service.tick, args=(run_id,))
    tick_thread.start()
    assert entered.wait(5)
    assert service.cancel(run_id)["status"] == "cancelled"
    release.set()
    tick_thread.join(timeout=5)
    run = service.get_run(run_id)
    assert run["status"] == "cancelled"
    assert run["jobs"][0]["status"] == "failed"
    assert run["jobs"][0]["error_code"] == "RUN_CANCELLED"
    with service.worker.open_database(run_id) as database:
        assert not (database.path.parent / "generated/features/minecraft_stone.json").exists()
        assert database.fetchone("SELECT 1 FROM audit_events WHERE event_type='FEATURE_ITEM_SUCCEEDED'") is None
    service.close()


def test_stage_only_stale_marker_is_read_only_until_recovery(tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            connection.execute("UPDATE runs SET status='running',current_stage='IMPORT_EXPORT' WHERE run_id=?", (run_id,))
            connection.execute("UPDATE stage_runs SET status='running',worker_id='worker_dead',heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=? AND stage='IMPORT_EXPORT'", (run_id,))
        before = database.fetchone("SELECT COUNT(*) AS n FROM audit_events")
    markers = service.stale_markers(run_id)
    assert any(marker.job_id is None and marker.stage == "IMPORT_EXPORT" for marker in markers)
    with service.worker.open_database(run_id) as database:
        after = database.fetchone("SELECT COUNT(*) AS n FROM audit_events")
    assert before is not None and after is not None and before["n"] == after["n"]
    recovered = service.recover(run_id, stage="IMPORT_EXPORT")
    assert recovered["recovered"]["status"] == "pending"
    run = service.get_run(run_id)
    assert run["status"] == "pending"
    assert run["stages"][1]["recovery_attempt"] == 1
    service.close()


def _audit_events(service: StudioService, run_id: str) -> list[dict[str, object]]:
    with service.worker.open_database(run_id) as database:
        return [dict(row) for row in database.fetchall("SELECT event_type FROM audit_events WHERE run_id=?", (run_id,))]
