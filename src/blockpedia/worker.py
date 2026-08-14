"""Persistent in-process worker for the R2 pipeline boundary."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .features import axis_aligned_union, build_visual_variant_record, extract_features
from .importer import _check_property_membership, _read_jsonl, _validate_projection_references
from .paths import DataRoot, safe_relative_posix_ref
from .schema import validate_record
from .search import WorkspaceQueryService
from .stages import R2_STAGES, R3_BOUNDARY_EVENT, RunStateConflict, STUDIO_STAGES
from .storage import WorkspaceDatabase, utc_now
from .toolchain import ToolchainProbe


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: bytes | str) -> str:
    return "sha256:" + hashlib.sha256(value if isinstance(value, bytes) else value.encode("utf-8")).hexdigest()


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class StaleMarker:
    job_id: str | None
    run_id: str
    stage: str
    logical_key: str | None
    heartbeat_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "stage": self.stage,
            "logical_key": self.logical_key,
            "heartbeat_at": self.heartbeat_at,
            "stale": True,
        }


class StageFailure(RuntimeError):
    def __init__(self, error_code: str, message: str, evidence: dict[str, Any] | None = None):
        self.error_code = error_code
        self.evidence = evidence or {}
        super().__init__(message)


class LeaseLost(RuntimeError):
    pass


class WorkerService:
    def __init__(self, data_root: DataRoot, *, repo_root: Path | None = None, stale_after_seconds: int = 300, force_normalized_like: bool = False, toolchain_probe: Any | None = None):
        self.data_root = data_root
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.stale_after_seconds = stale_after_seconds
        self.force_normalized_like = force_normalized_like
        self.toolchain_probe = toolchain_probe or ToolchainProbe(self.repo_root)
        self.worker_id = "worker_" + uuid.uuid4().hex
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        self._run_locks: dict[str, threading.RLock] = {}
        self._run_locks_guard = threading.Lock()

    @contextmanager
    def run_lock(self, run_id: str):
        with self._run_locks_guard:
            lock = self._run_locks.setdefault(run_id, threading.RLock())
        with lock:
            yield

    def database_for(self, run_id: str, minecraft_version: str | None = None) -> WorkspaceDatabase:
        if minecraft_version is None:
            minecraft_version = self._find_run_version(run_id)
        path = self.data_root.workspace_dir(minecraft_version, run_id) / "work.sqlite3"
        return WorkspaceDatabase.open(path, force_normalized_like=self.force_normalized_like)

    @contextmanager
    def open_database(self, run_id: str, minecraft_version: str | None = None):
        database = self.database_for(run_id, minecraft_version)
        try:
            yield database
        finally:
            database.close()

    def _find_run_version(self, run_id: str) -> str:
        if not self.data_root.workspace.is_dir():
            raise KeyError(run_id)
        for version_dir in self.data_root.workspace.iterdir():
            candidate = version_dir / run_id / "work.sqlite3"
            if not candidate.is_file():
                continue
            database = WorkspaceDatabase.open(candidate, force_normalized_like=self.force_normalized_like, read_only=True)
            try:
                if database.fetchone("SELECT 1 FROM runs WHERE run_id=?", (run_id,)) is not None:
                    return version_dir.name
            finally:
                database.close()
        raise KeyError(run_id)

    def close(self, *, timeout: float = 2.0) -> bool:
        with self._lifecycle_lock:
            if self._closed:
                return self._thread is None or not self._thread.is_alive()
            self._closed = True
            return self.stop(timeout=timeout)

    def start(self, *, interval_seconds: float = 0.05) -> bool:
        with self._lifecycle_lock:
            if self._closed:
                return False
            if self._thread is not None:
                if self._thread.is_alive():
                    return False
                # A naturally exited thread is no longer an ownership barrier.
                self._thread = None
            self._stop.clear()
            thread = threading.Thread(target=self._loop, args=(interval_seconds,), name="blockpedia-worker", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                self._stop.set()
                raise
            return True

    def stop(self, *, timeout: float = 2.0) -> bool:
        with self._lifecycle_lock:
            self._stop.set()
            thread = self._thread
            if thread is None:
                return True
            if thread is threading.current_thread():
                return False
            thread.join(timeout=timeout)
            if thread.is_alive():
                # Keep both the thread reference and stop event.  A later
                # start must not clear the event while this thread can still
                # execute.
                return False
            if self._thread is thread:
                self._thread = None
            return True

    def _loop(self, interval_seconds: float) -> None:
        while not self._stop.is_set():
            try:
                paths = self._run_database_paths()
            except Exception as exc:
                print(f"blockpedia worker diagnostic: {_safe_diagnostic(exc, self.data_root.workspace)}", file=sys.stderr)
                self._stop.wait(interval_seconds)
                continue
            for path in paths:
                run_id: str | None = None
                minecraft_version: str | None = None
                try:
                    minecraft_version = path.parent.parent.name
                    should_tick = False
                    with WorkspaceDatabase.open(path, force_normalized_like=self.force_normalized_like, read_only=True) as database:
                        row = database.fetchone("SELECT run_id,status FROM runs LIMIT 1")
                        if row is None:
                            continue
                        run_id = str(row["run_id"])
                        should_tick = row["status"] == "pending" or (row["status"] == "running" and not self._run_is_stale(database, run_id))
                    if should_tick:
                        self.tick(run_id, minecraft_version)
                except Exception as exc:
                    self._record_infrastructure_failure(path, run_id, minecraft_version, exc)
            self._stop.wait(interval_seconds)

    def _run_database_paths(self) -> list[Path]:
        paths: list[Path] = []
        if not self.data_root.workspace.is_dir():
            return paths
        for version_dir in sorted(self.data_root.workspace.iterdir(), key=lambda item: item.name):
            if not version_dir.is_dir() or version_dir.is_symlink():
                continue
            for run_dir in sorted(version_dir.iterdir(), key=lambda item: item.name):
                if run_dir.name.startswith("."):
                    continue
                path = run_dir / "work.sqlite3"
                if run_dir.is_dir() and not run_dir.is_symlink() and path.is_file() and not path.is_symlink():
                    paths.append(path)
        return paths

    def _record_infrastructure_failure(self, path: Path, run_id: str | None, minecraft_version: str | None, exc: Exception) -> None:
        diagnostic = _safe_diagnostic(exc, path)
        if run_id and minecraft_version:
            try:
                with self.run_lock(run_id):
                    with self.open_database(run_id, minecraft_version) as database:
                        now = utc_now()
                        with database.transaction() as connection:
                            connection.execute("UPDATE jobs SET status='failed',error_code='WORKER_INFRASTRUCTURE_FAILED',error_message=?,finished_at=? WHERE run_id=? AND status='running' AND worker_id=?", (diagnostic, now, run_id, self.worker_id))
                            connection.execute("UPDATE stage_runs SET status='failed',worker_id=NULL,finished_at=? WHERE run_id=? AND status='running' AND worker_id=?", (now, run_id, self.worker_id))
                            connection.execute("UPDATE runs SET status='failed',finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                            connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), "WORKER_INFRASTRUCTURE_FAILED", run_id, _json({"error_code": "WORKER_INFRASTRUCTURE_FAILED"}), now))
                        return
            except Exception as persist_exc:
                diagnostic = _safe_diagnostic(persist_exc, path)
        print(f"blockpedia worker diagnostic: {diagnostic}", file=sys.stderr)

    def tick(self, run_id: str, minecraft_version: str | None = None) -> dict[str, Any]:
        with self.open_database(run_id, minecraft_version) as database:
            run = database.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,))
            if run is None:
                raise KeyError(run_id)
            if run["status"] in {"paused", "cancelled", "succeeded", "failed", "needs_review"}:
                return _row_dict(run)
            if run["status"] == "running" and self._run_is_stale(database, run_id):
                return _row_dict(run)
            stage_row = database.fetchone(
                "SELECT * FROM stage_runs WHERE run_id = ? AND status IN ('pending','running') ORDER BY ordinal LIMIT 1", (run_id,)
            )
            if stage_row is None:
                return _row_dict(run)
            stage = str(stage_row["stage"])
            if stage not in R2_STAGES:
                self._stop_at_r3_boundary(database, run_id, stage)
                return _row_dict(database.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
            if not self._begin_stage(database, run_id, stage):
                return _row_dict(database.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
            if stage == "EXTRACT_FEATURES":
                self._extract_one(database, run_id)
            else:
                self._finish_simple_stage(database, run_id, stage)
            return _row_dict(database.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,)))

    def _run_is_stale(self, database: WorkspaceDatabase, run_id: str) -> bool:
        row = database.fetchone(
            "SELECT heartbeat_at FROM stage_runs WHERE run_id=? AND status='running' ORDER BY ordinal LIMIT 1", (run_id,)
        )
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_after_seconds)
        if row and _is_stale(row["heartbeat_at"], cutoff):
            return True
        return any(_is_stale(job["heartbeat_at"], cutoff) for job in database.fetchall("SELECT heartbeat_at FROM jobs WHERE run_id=? AND status='running'", (run_id,)))

    def _begin_stage(self, database: WorkspaceDatabase, run_id: str, stage: str) -> bool:
        with self.run_lock(run_id):
            now = utc_now()
            with database.transaction() as connection:
                row = connection.execute("SELECT status,worker_id FROM stage_runs WHERE run_id=? AND stage=?", (run_id, stage)).fetchone()
                if row is None:
                    return False
                if row["status"] == "running" and row["worker_id"] != self.worker_id:
                    return False
                if row["status"] not in {"pending", "running"}:
                    return False
                stage_update = connection.execute(
                    "UPDATE stage_runs SET status='running',worker_id=?,started_at=COALESCE(started_at,?),heartbeat_at=? WHERE run_id=? AND stage=? AND status IN ('pending','running') AND (status='pending' OR worker_id=?)",
                    (self.worker_id, now, now, run_id, stage, self.worker_id),
                )
                if stage_update.rowcount != 1:
                    return False
                run_update = connection.execute(
                    "UPDATE runs SET status='running', current_stage=?, started_at=COALESCE(started_at,?) WHERE run_id=? AND status IN ('pending','running')",
                    (stage, now, run_id),
                )
                if run_update.rowcount != 1:
                    return False
                connection.execute(
                    "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                    (_id("audit"), "STAGE_STARTED", run_id, _json({"stage": stage, "worker_id": self.worker_id}), now),
                )
                return True

    def _finish_simple_stage(self, database: WorkspaceDatabase, run_id: str, stage: str) -> None:
        try:
            evidence = self._stage_evidence(database, run_id, stage)
        except StageFailure as exc:
            self._persist_stage_failure(database, run_id, stage, exc)
            return
        except Exception as exc:
            self._persist_stage_failure(database, run_id, stage, StageFailure("STAGE_CHECK_FAILED", _safe_diagnostic(exc, database.path)))
            return
        self._persist_stage_success(database, run_id, stage, evidence)

    def _stage_evidence(self, database: WorkspaceDatabase, run_id: str, stage: str) -> dict[str, Any]:
        if stage == "PREPARE":
            result = self.toolchain_probe.check()
            if not result.get("passed"):
                raise StageFailure("TOOLCHAIN_NOT_LOCKED", "R2 PREPARE toolchain probe failed", result)
            return {"stage": stage, "toolchain": result}
        if stage == "IMPORT_EXPORT":
            row = database.fetchone("SELECT import_id,manifest_sha256,checksum_sha256,expected_files_json FROM imports WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)", (run_id,))
            manifest = database.path.parent / "export" / "manifest.json"
            if row is None or not manifest.is_file() or _hash(manifest.read_bytes()) != row["manifest_sha256"]:
                raise StageFailure("IMPORT_INCOMPLETE", "workspace export handoff evidence is incomplete")
            return {"stage": stage, "import_id": row["import_id"], "manifest_sha256": row["manifest_sha256"], "checksum_sha256": row["checksum_sha256"], "expected_files": json.loads(row["expected_files_json"])}
        if stage == "VALIDATE_REGISTRY":
            manifest = _load_json(database.path.parent / "export" / "manifest.json")
            block_ids = sorted((row["block_id"] for row in database.fetchall("SELECT block_id FROM blocks")), key=lambda value: value.encode("utf-8"))
            actual_hash = _hash("\n".join(block_ids).encode("utf-8"))
            expected_count = manifest.get("counts", {}).get("registry_blocks")
            expected_hash = manifest.get("scope", {}).get("registry_snapshot_sha256")
            if expected_count != len(block_ids) or expected_hash != actual_hash:
                raise StageFailure("REGISTRY_INCOMPLETE", "registry manifest and workspace projection differ", {"actual_count": len(block_ids), "actual_hash": actual_hash, "expected_count": expected_count, "expected_hash": expected_hash})
            return {"stage": stage, "registry_count": len(block_ids), "registry_snapshot_sha256": actual_hash}
        if stage == "VALIDATE_VARIANTS":
            blocks = {row["block_id"]: _load_json_text(row["record_json"]) for row in database.fetchall("SELECT block_id,record_json FROM blocks")}
            states = [_load_json_text(row["record_json"]) for row in database.fetchall("SELECT record_json FROM states")]
            variants = {record["variant_id"]: record for record in _read_jsonl(database.path.parent / "export" / "variants.jsonl")}
            failures = [_load_json_text(row["record_json"]) for row in database.fetchall("SELECT record_json FROM failures")]
            _validate_projection_references(blocks, states, variants, failures)
            for state in states:
                _check_property_membership(blocks[state["block_id"]], state)
            return {"stage": stage, "block_count": len(blocks), "state_count": len(states), "selected_variant_count": len(variants), "failure_count": len(failures)}
        if stage == "VALIDATE_RENDERS":
            expected: set[str] = set()
            checked: list[dict[str, str]] = []
            for row in database.fetchall("SELECT variant_id,source_json FROM variants WHERE status='selected' ORDER BY variant_id"):
                source = _load_json_text(row["source_json"])
                render = source["render"]
                for ref_key, hash_key in (("preview_path", "image_sha256"), ("mask_path", "mask_sha256")):
                    ref = safe_relative_posix_ref(render[ref_key])
                    file_path = database.path.parent / ref
                    if file_path.is_symlink() or not file_path.is_file() or file_path.stat().st_nlink != 1 or _hash(file_path.read_bytes()) != render[hash_key]:
                        raise StageFailure("RENDER_REFERENCE_INVALID", "workspace render reference or hash mismatch", {"relative_ref": ref})
                    artifact = database.fetchone("SELECT sha256 FROM artifacts WHERE relative_ref=? AND sha256=? AND kind='render'", (ref, render[hash_key]))
                    if artifact is None:
                        raise StageFailure("RENDER_ARTIFACT_MISSING", "workspace render artifact evidence is missing", {"relative_ref": ref})
                    expected.add(ref)
                    checked.append({"relative_ref": ref, "sha256": render[hash_key]})
                metadata_ref = safe_relative_posix_ref(render["render_metadata_path"])
                metadata_path = database.path.parent / metadata_ref
                if metadata_path.is_symlink() or not metadata_path.is_file() or metadata_path.stat().st_nlink != 1:
                    raise StageFailure("RENDER_REFERENCE_INVALID", "workspace render metadata is missing", {"relative_ref": metadata_ref})
                metadata = _load_json(metadata_path)
                metadata_hash = _hash(_canonical_json(metadata).encode("utf-8"))
                if metadata_hash != render["render_metadata_sha256"]:
                    raise StageFailure("RENDER_REFERENCE_INVALID", "workspace render metadata hash mismatch", {"relative_ref": metadata_ref})
                artifact = database.fetchone("SELECT sha256 FROM artifacts WHERE relative_ref=? AND sha256=? AND kind='render'", (metadata_ref, metadata_hash))
                if artifact is None:
                    raise StageFailure("RENDER_ARTIFACT_MISSING", "workspace render metadata artifact evidence is missing", {"relative_ref": metadata_ref})
                expected.add(metadata_ref)
                checked.append({"relative_ref": metadata_ref, "sha256": metadata_hash})
            renders_root = database.path.parent / "renders"
            for path in renders_root.rglob("*"):
                if path.is_symlink() or (not path.is_file() and not path.is_dir()):
                    raise StageFailure("RENDER_FILE_SET_INVALID", "workspace render tree contains a link or invalid entry")
            actual = {path.relative_to(database.path.parent).as_posix() for path in renders_root.rglob("*") if path.is_file()}
            if actual != expected:
                raise StageFailure("RENDER_FILE_SET_INVALID", "workspace render file set differs", {"expected_count": len(expected), "actual_count": len(actual)})
            return {"stage": stage, "render_count": len(checked), "render_hashes": checked}
        return {"stage": stage}

    def _persist_stage_success(self, database: WorkspaceDatabase, run_id: str, stage: str, evidence: dict[str, Any]) -> None:
        output = _json(evidence)
        output_hash = _hash(output)
        relative_ref = f"generated/stages/{stage}.json"
        with self.run_lock(run_id):
            now = utc_now()
            cursor = _json({"stage": stage, "output_hash": output_hash, "completed": True, "evidence": evidence})
            try:
                with database.transaction() as connection:
                    stage_update = connection.execute("UPDATE stage_runs SET status='succeeded',worker_id=NULL,heartbeat_at=?,finished_at=?,cursor_json=? WHERE run_id=? AND stage=? AND status='running' AND worker_id=?", (now, now, cursor, run_id, stage, self.worker_id))
                    if stage_update.rowcount != 1:
                        raise RunStateConflict("stage lease lost before success")
                    _write_atomic(database.path.parent / relative_ref, output.encode("utf-8"))
                    next_stage = STUDIO_STAGES[STUDIO_STAGES.index(stage) + 1]
                    run_update = connection.execute("UPDATE runs SET current_stage=? WHERE run_id=? AND status='running'", (next_stage, run_id))
                    if run_update.rowcount != 1:
                        raise RunStateConflict("run state changed before stage success")
                    connection.execute("INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)", (_id("artifact"), None, "stage_output", relative_ref, output_hash, _json({"stage": stage})))
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), "STAGE_SUCCEEDED", run_id, _json({"stage": stage, "output_hash": output_hash}), now))
            except RunStateConflict:
                return

    def _persist_stage_failure(self, database: WorkspaceDatabase, run_id: str, stage: str, failure: StageFailure) -> None:
        now = utc_now()
        evidence = {"stage": stage, "error_code": failure.error_code, "error_message": _safe_diagnostic(failure, error_code=failure.error_code), "evidence": failure.evidence}
        output_hash = _hash(_json(evidence))
        cursor = _json({"stage": stage, "output_hash": output_hash, "completed": False, "error_code": failure.error_code})
        with self.run_lock(run_id):
            with database.transaction() as connection:
                stage_update = connection.execute("UPDATE stage_runs SET status='failed',worker_id=NULL,heartbeat_at=?,finished_at=?,cursor_json=? WHERE run_id=? AND stage=? AND status='running' AND worker_id=?", (now, now, cursor, run_id, stage, self.worker_id))
                if stage_update.rowcount != 1:
                    return
                connection.execute("UPDATE runs SET status='failed',finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), "STAGE_FAILED", run_id, _json(evidence), now))

    def _extract_one(self, database: WorkspaceDatabase, run_id: str) -> None:
        workspace_dir = database.path.parent
        with self.run_lock(run_id):
            self._ensure_feature_jobs(database, run_id)
            running = database.fetchone("SELECT 1 FROM jobs WHERE run_id=? AND stage='EXTRACT_FEATURES' AND status='running'", (run_id,))
            if running is not None:
                job = database.fetchone(
                    "SELECT * FROM jobs WHERE run_id=? AND stage='EXTRACT_FEATURES' AND status='running' AND worker_id=? ORDER BY logical_key LIMIT 1",
                    (run_id, self.worker_id),
                )
            else:
                job = self._claim_pending_job(database, run_id)
        if job is None:
            if running is None:
                self._finish_extract_stage(database, run_id)
            return
        now = utc_now()
        if job["status"] == "running":
            with self.run_lock(run_id):
                with database.transaction() as connection:
                    connection.execute("UPDATE jobs SET heartbeat_at=? WHERE job_id=? AND status='running' AND worker_id=?", (now, job["job_id"], self.worker_id))
                    connection.execute("UPDATE stage_runs SET heartbeat_at=? WHERE run_id=? AND stage='EXTRACT_FEATURES' AND status='running' AND worker_id=?", (now, run_id, self.worker_id))
        try:
            variant = database.fetchone("SELECT source_json FROM variants WHERE variant_id=?", (job["logical_key"],))
            if variant is None:
                raise ValueError("selected variant missing")
            source = json.loads(variant["source_json"])
            render = source["render"]
            preview_ref = safe_relative_posix_ref(render["preview_path"])
            mask_ref = safe_relative_posix_ref(render["mask_path"])
            preview_path = workspace_dir / preview_ref
            mask_path = workspace_dir / mask_ref
            geometry = _geometry_from_source(source)
            features = extract_features(preview_path, mask_path, geometry=geometry, machine_tags=_source_machine_tags(source))
            record = build_visual_variant_record(source, features)
            validate_record("visual-variant-record.v1", record, repo_root=self.repo_root)
            feature_json = _json(features)
            output_payload = _json({"record": record, "features": features})
            output_hash = _hash(output_payload)
            feature_ref = f"generated/features/{job['logical_key'].replace(':', '_')}.json"
            safe_relative_posix_ref(feature_ref)
            now = utc_now()
            with self.run_lock(run_id):
                with database.transaction() as connection:
                    owner = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    stage_owner = connection.execute("SELECT status,worker_id,pause_after_item FROM stage_runs WHERE run_id=? AND stage='EXTRACT_FEATURES'", (run_id,)).fetchone()
                    if owner is None or owner["status"] != "running" or stage_owner is None or stage_owner["status"] != "running" or stage_owner["worker_id"] != self.worker_id:
                        raise LeaseLost("run or stage lease lost before feature commit")
                    job_update = connection.execute(
                        "UPDATE jobs SET status='succeeded',heartbeat_at=?,finished_at=?,cursor_json=?,output_hash=? WHERE job_id=? AND status='running' AND worker_id=?",
                        (now, now, _json({"last_logical_key": job["logical_key"]}), output_hash, job["job_id"], self.worker_id),
                    )
                    if job_update.rowcount != 1:
                        raise LeaseLost("job lease lost before feature commit")
                    _write_atomic(workspace_dir / feature_ref, output_payload.encode("utf-8"))
                    connection.execute(
                        "INSERT OR REPLACE INTO features(variant_id,input_sha256,feature_extractor_version,feature_json,output_hash) VALUES (?,?,?,?,?)",
                        (job["logical_key"], features["input_sha256"], features["feature_extractor_version"], feature_json, output_hash),
                    )
                    connection.execute("UPDATE variants SET record_json=? WHERE variant_id=?", (_json(record), job["logical_key"]))
                    connection.execute(
                        "INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)",
                        (_id("artifact"), job["job_id"], "feature_output", feature_ref, output_hash, _json({"variant_id": job["logical_key"]})),
                    )
                    if stage_owner["pause_after_item"]:
                        connection.execute("UPDATE stage_runs SET status='paused',worker_id=NULL,pause_after_item=0,heartbeat_at=?,finished_at=? WHERE run_id=? AND stage='EXTRACT_FEATURES' AND status='running' AND worker_id=?", (now, now, run_id, self.worker_id))
                        connection.execute("UPDATE runs SET status='paused' WHERE run_id=? AND status='running'", (run_id,))
                        connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)", (_id("audit"), "RUN_PAUSED_AFTER_ITEM", run_id, job["job_id"], _json({"variant_id": job["logical_key"]}), now))
                    else:
                        connection.execute(
                            "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                            (_id("audit"), "FEATURE_ITEM_SUCCEEDED", run_id, job["job_id"], _json({"variant_id": job["logical_key"]}), now),
                        )
        except LeaseLost:
            return
        except Exception as exc:
            safe_message = _safe_diagnostic(exc, workspace_dir, error_code="FEATURE_EXTRACTION_FAILED")
            with self.run_lock(run_id):
                with database.transaction() as connection:
                    current = connection.execute("SELECT status,worker_id FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                    if current is None or current["status"] != "running" or current["worker_id"] != self.worker_id:
                        return
                    connection.execute(
                        "UPDATE jobs SET status='failed',error_code='FEATURE_EXTRACTION_FAILED',error_message=?,finished_at=?,heartbeat_at=? WHERE job_id=? AND status='running' AND worker_id=?",
                        (safe_message, utc_now(), utc_now(), job["job_id"], self.worker_id),
                    )
                    connection.execute("UPDATE stage_runs SET status='failed',worker_id=NULL,finished_at=? WHERE run_id=? AND stage='EXTRACT_FEATURES' AND status='running' AND worker_id=?", (utc_now(), run_id, self.worker_id))
                    connection.execute("UPDATE runs SET status='failed',finished_at=? WHERE run_id=? AND status='running'", (utc_now(), run_id))
                    connection.execute(
                        "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                        (_id("audit"), "FEATURE_ITEM_FAILED", run_id, job["job_id"], _json({"error_code": "FEATURE_EXTRACTION_FAILED"}), utc_now()),
                    )
            return
        self._finish_extract_stage(database, run_id)

    def _claim_pending_job(self, database: WorkspaceDatabase, run_id: str):
        now = utc_now()
        with database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE run_id=? AND stage='EXTRACT_FEATURES' AND status='pending' AND NOT EXISTS (SELECT 1 FROM jobs AS running_jobs WHERE running_jobs.run_id=jobs.run_id AND running_jobs.stage=jobs.stage AND running_jobs.status='running') ORDER BY logical_key LIMIT 1",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            cursor = connection.execute(
                "UPDATE jobs SET status='running',worker_id=?,started_at=COALESCE(started_at,?),heartbeat_at=? WHERE job_id=? AND status='pending'",
                (self.worker_id, now, now, row["job_id"]),
            )
            if cursor.rowcount != 1:
                return None
            return connection.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()

    def _ensure_feature_jobs(self, database: WorkspaceDatabase, run_id: str) -> None:
        rows = database.fetchall("SELECT variant_id,source_json FROM variants WHERE status='selected' ORDER BY variant_id")
        with database.transaction() as connection:
            for row in rows:
                source = row["source_json"]
                signature = _hash(source)
                connection.execute(
                    "INSERT OR IGNORE INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (_id("job"), run_id, "EXTRACT_FEATURES", row["variant_id"], signature, "pending", 0, "{}", utc_now()),
                )

    def _finish_extract_stage(self, database: WorkspaceDatabase, run_id: str) -> None:
        with self.run_lock(run_id):
            run = database.fetchone("SELECT status FROM runs WHERE run_id=?", (run_id,))
            stage = database.fetchone("SELECT status,worker_id FROM stage_runs WHERE run_id=? AND stage='EXTRACT_FEATURES'", (run_id,))
            remaining = database.fetchone("SELECT 1 FROM jobs WHERE run_id=? AND stage='EXTRACT_FEATURES' AND status NOT IN ('succeeded','skipped')", (run_id,))
            if run is None or run["status"] != "running" or stage is None or stage["status"] != "running" or stage["worker_id"] != self.worker_id or remaining is not None:
                return
            WorkspaceQueryService(database).rebuild_index()
            now = utc_now()
            outputs = [{"logical_key": row["logical_key"], "output_hash": row["output_hash"]} for row in database.fetchall("SELECT logical_key,output_hash FROM jobs WHERE run_id=? AND stage='EXTRACT_FEATURES' ORDER BY logical_key", (run_id,))]
            output_hash = _hash(_json(outputs))
            cursor = _json({"stage": "EXTRACT_FEATURES", "output_hash": output_hash, "completed": True, "items": outputs})
            with database.transaction() as connection:
                stage_update = connection.execute("UPDATE stage_runs SET status='succeeded',worker_id=NULL,heartbeat_at=?,finished_at=?,cursor_json=? WHERE run_id=? AND stage='EXTRACT_FEATURES' AND status='running' AND worker_id=?", (now, now, cursor, run_id, self.worker_id))
                if stage_update.rowcount != 1:
                    return
                connection.execute("UPDATE runs SET status='paused',current_stage='AI_ANNOTATE',boundary_event=? WHERE run_id=? AND status='running'", (R3_BOUNDARY_EVENT, run_id))
                connection.execute(
                    "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                    (_id("audit"), R3_BOUNDARY_EVENT, run_id, _json({"next_stage": "AI_ANNOTATE", "r2_complete": True}), now),
                )

    def _stop_at_r3_boundary(self, database: WorkspaceDatabase, run_id: str, stage: str) -> None:
        with self.run_lock(run_id):
            now = utc_now()
            with database.transaction() as connection:
                updated = connection.execute("UPDATE runs SET status='paused',current_stage='AI_ANNOTATE',boundary_event=? WHERE run_id=? AND status='running' AND boundary_event IS NULL", (R3_BOUNDARY_EVENT, run_id))
                if updated.rowcount != 1:
                    return
                connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), R3_BOUNDARY_EVENT, run_id, _json({"pending_stage": stage}), now))

    def heartbeat(self, run_id: str, job_id: str | None = None) -> None:
        with self.run_lock(run_id):
            with self.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    if job_id is None:
                        connection.execute("UPDATE stage_runs SET heartbeat_at=? WHERE run_id=? AND status='running' AND worker_id=?", (now, run_id, self.worker_id))
                        connection.execute("UPDATE runs SET started_at=COALESCE(started_at,?) WHERE run_id=? AND status='running'", (now, run_id))
                    else:
                        connection.execute("UPDATE jobs SET heartbeat_at=? WHERE job_id=? AND run_id=? AND status='running' AND worker_id=?", (now, job_id, run_id, self.worker_id))

    def detect_stale(self, run_id: str | None = None) -> list[StaleMarker]:
        """Read-only startup probe. It performs no UPDATE or INSERT."""

        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_after_seconds)
        markers: list[StaleMarker] = []
        paths = self._run_database_paths()
        if run_id:
            version = self._find_run_version(run_id)
            paths = [self.data_root.workspace_dir(version, run_id) / "work.sqlite3"]
        for path in paths:
            with WorkspaceDatabase.open(path, force_normalized_like=self.force_normalized_like, read_only=True) as database:
                query = "SELECT job_id,run_id,stage,logical_key,heartbeat_at FROM jobs WHERE status='running'"
                params: tuple[Any, ...] = ()
                if run_id:
                    query += " AND run_id=?"
                    params = (run_id,)
                for row in database.fetchall(query, params):
                    if _is_stale(row["heartbeat_at"], cutoff):
                        markers.append(StaleMarker(row["job_id"], row["run_id"], row["stage"], row["logical_key"], row["heartbeat_at"]))
                stage_query = "SELECT run_id,stage,heartbeat_at FROM stage_runs WHERE status='running'"
                stage_params: tuple[Any, ...] = ()
                if run_id:
                    stage_query += " AND run_id=?"
                    stage_params = (run_id,)
                for row in database.fetchall(stage_query, stage_params):
                    if _is_stale(row["heartbeat_at"], cutoff) and database.fetchone("SELECT 1 FROM jobs WHERE run_id=? AND stage=? AND status='running'", (row["run_id"], row["stage"])) is None:
                        markers.append(StaleMarker(None, row["run_id"], row["stage"], None, row["heartbeat_at"]))
        return markers

    def recover(self, run_id: str, job_id: str | None = None, *, stage: str | None = None) -> dict[str, Any]:
        """Recover one stale job, a stale stage-only lease, or the run target."""

        with self.run_lock(run_id):
            with self.open_database(run_id) as database:
                now = utc_now()
                cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_after_seconds)
                with database.transaction() as connection:
                    run = connection.execute("SELECT status,current_stage FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if run is None:
                        raise KeyError(run_id)
                    if run["status"] != "running" and not (job_id is not None and run["status"] == "pending"):
                        raise RunStateConflict("recover requires running run")
                    if stage is None:
                        stage = str(run["current_stage"])
                    stage_row = connection.execute("SELECT * FROM stage_runs WHERE run_id=? AND stage=?", (run_id, stage)).fetchone()
                    if stage_row is None or stage_row["status"] not in {"running", "pending"}:
                        raise RunStateConflict("stage is not stale")
                    if job_id is None and (stage_row["status"] != "running" or not _is_stale(stage_row["heartbeat_at"], cutoff)):
                        raise RunStateConflict("stage is not stale")
                    if stage_row["recovery_attempt"] >= 1:
                        stage_result = "needs_review"
                    else:
                        stage_result = "pending"
                    all_job_rows = connection.execute("SELECT * FROM jobs WHERE run_id=? AND stage=? AND status='running' ORDER BY logical_key", (run_id, stage)).fetchall()
                    target_only = False
                    if job_id is not None:
                        job_rows = [row for row in all_job_rows if row["job_id"] == job_id]
                        if not job_rows:
                            raise RunStateConflict("recover target is not a stale running job")
                        if not _is_stale(job_rows[0]["heartbeat_at"], cutoff):
                            raise RunStateConflict("job is not stale")
                        target_only = len(all_job_rows) > 1
                    else:
                        job_rows = [row for row in all_job_rows if _is_stale(row["heartbeat_at"], cutoff)]
                        if all_job_rows and not job_rows:
                            raise RunStateConflict("no stale running job in stale stage")
                        target_only = bool(all_job_rows and len(job_rows) < len(all_job_rows))
                    if stage_result == "needs_review":
                        for row in job_rows:
                            connection.execute("UPDATE jobs SET status='needs_review',worker_id=NULL,heartbeat_at=?,finished_at=? WHERE job_id=? AND status='running'", (now, now, row["job_id"]))
                    else:
                        for row in job_rows:
                            if row["output_hash"] and self._artifact_hash_is_complete(database, row["job_id"], row["output_hash"]):
                                recovered_status = "succeeded"
                            elif row["auto_attempt"] == 0:
                                recovered_status = "pending"
                            else:
                                recovered_status = "needs_review"
                            if recovered_status == "needs_review":
                                stage_result = "needs_review"
                            connection.execute("UPDATE jobs SET status=?,auto_attempt=CASE WHEN ?='pending' THEN 1 ELSE auto_attempt END,worker_id=NULL,heartbeat_at=NULL,finished_at=? WHERE job_id=? AND status='running'", (recovered_status, recovered_status, now, row["job_id"]))
                    if target_only:
                        connection.execute(
                            "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                            (_id("audit"), "WORKER_RECOVERED_STALE_RUNNING", run_id, job_id, _json({"stage": stage, "result": stage_result, "job_count": len(job_rows), "target_only": True}), now),
                        )
                        result = {"job_id": job_id, "stage": stage, "status": stage_result, "recovery_attempt": stage_row["recovery_attempt"]}
                        if job_id is not None:
                            recovered = connection.execute("SELECT status,auto_attempt FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                            result.update({"status": recovered["status"], "auto_attempt": recovered["auto_attempt"]})
                        return result
                    if stage_result == "needs_review":
                        connection.execute("UPDATE stage_runs SET status='needs_review',worker_id=NULL,recovery_attempt=1,heartbeat_at=?,finished_at=? WHERE run_id=? AND stage=? AND status IN ('running','pending')", (now, now, run_id, stage))
                        connection.execute("UPDATE runs SET status='needs_review',finished_at=? WHERE run_id=? AND status IN ('running','pending')", (now, run_id))
                    else:
                        connection.execute("UPDATE stage_runs SET status='pending',worker_id=NULL,recovery_attempt=1,heartbeat_at=NULL WHERE run_id=? AND stage=? AND status IN ('running','pending')", (run_id, stage))
                        connection.execute("UPDATE runs SET status='pending' WHERE run_id=? AND status IN ('running','pending')", (run_id,))
                    connection.execute(
                        "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                        (_id("audit"), "WORKER_RECOVERED_STALE_RUNNING", run_id, job_id, _json({"stage": stage, "result": stage_result, "job_count": len(job_rows)}), now),
                    )
                    result = {"job_id": job_id, "stage": stage, "status": stage_result, "recovery_attempt": 1}
                    if job_id is not None and job_rows:
                        recovered = connection.execute("SELECT status,auto_attempt FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                        result.update({"status": recovered["status"], "auto_attempt": recovered["auto_attempt"]})
                    return result

    def _artifact_hash_is_complete(self, database: WorkspaceDatabase, job_id: str, output_hash: str) -> bool:
        row = database.fetchone("SELECT relative_ref,sha256 FROM artifacts WHERE job_id=? AND sha256=?", (job_id, output_hash))
        if row is None:
            return False
        try:
            relative = safe_relative_posix_ref(row["relative_ref"])
            artifact = database.path.parent / relative
            return artifact.is_file() and _hash(artifact.read_bytes()) == output_hash
        except (OSError, TypeError, ValueError):
            return False


def _is_stale(value: str | None, cutoff: datetime) -> bool:
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return True
    return parsed < cutoff


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        import os

        os.fsync(handle.fileno())
    temporary.replace(path)


def _safe_diagnostic(exc: Exception, path: Path | None = None, *, error_code: str | None = None) -> str:
    """Return an allowlisted diagnostic without exception text or paths."""

    del path
    candidate = error_code or getattr(exc, "error_code", None) or getattr(exc, "code", None)
    if not isinstance(candidate, str) or not candidate.isascii() or not candidate.replace("_", "").isalnum() or not candidate.isupper():
        candidate = "INTERNAL_ERROR"
    return f"{candidate}:{type(exc).__name__}"


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_json_text(value: str) -> Any:
    return json.loads(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _row_dict(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _geometry_from_source(source: dict[str, Any]) -> dict[str, Any]:
    machine = source["machine_facts"]
    shape = machine["shape"]
    union = axis_aligned_union(shape.get("boxes", []))
    return {
        "is_full_cube": union.is_full_cube,
        "is_horizontal_sheet": union.height > 0 and union.height <= 0.125 and union.width >= 0.75 and union.depth >= 0.75,
        "height": union.height,
        "width": union.width,
        "depth": union.depth,
        "occupied_volume": union.occupied_volume,
        "_union_proven": True,
    }


def _source_machine_tags(source: dict[str, Any]) -> Iterable[str]:
    geometry = _geometry_from_source(source)
    tags = []
    if geometry["is_full_cube"]:
        tags.append("shape:full_cube")
    if geometry["is_horizontal_sheet"]:
        tags.append("shape:horizontal_sheet")
    behavior = source["machine_facts"].get("behavior_by_state", {}).get(source["canonical_state_id"], {})
    if behavior.get("transparent") is True:
        tags.append("behavior:transparent")
    if behavior.get("emissive") is True:
        tags.append("behavior:emissive")
    return tags
