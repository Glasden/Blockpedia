"""Public R2 application services for the Phase 2 adapters."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from .importer import ImportCheck, ImportService
from .paths import DataRoot, resolve_data_root
from .search import WorkspaceQueryService
from .stages import RunStateConflict, STUDIO_STAGES, require_transition
from .storage import WorkspaceDatabase, utc_now
from .worker import StaleMarker, WorkerService


class StudioService:
    """Coordinates import, run controls, worker recovery and workspace search."""

    def __init__(self, data_root: DataRoot | str | Path | None = None, *, repo_root: Path | None = None, force_normalized_like: bool = False, toolchain_probe: Any | None = None):
        self.data_root = data_root if isinstance(data_root, DataRoot) else resolve_data_root(data_root)
        self.data_root.ensure_layout()
        self.imports = ImportService(self.data_root, repo_root=repo_root, force_normalized_like=force_normalized_like)
        self.worker = WorkerService(self.data_root, repo_root=repo_root, force_normalized_like=force_normalized_like, toolchain_probe=toolchain_probe)
        self._close_lock = threading.RLock()
        self._closed = False

    def close(self) -> bool:
        with self._close_lock:
            if self._closed:
                return False
            self._closed = True
            self.worker.close()
            return True

    def check_import(self, source_directory: str | Path, minecraft_version: str) -> ImportCheck:
        return self.imports.check_import(source_directory, minecraft_version)

    def import_checked(self, check_id: str, *, copy_mode: str = "copy_to_workspace") -> dict[str, Any]:
        return self.imports.import_checked(check_id, copy_mode=copy_mode)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.worker.open_database(run_id) as database:
            run = database.fetchone("SELECT * FROM runs WHERE run_id=?", (run_id,))
            if run is None:
                raise KeyError(run_id)
            stages = database.fetchall("SELECT stage,ordinal,status,cursor_json,worker_id,recovery_attempt,pause_after_item,heartbeat_at FROM stage_runs WHERE run_id=? ORDER BY ordinal", (run_id,))
            jobs = database.fetchall("SELECT job_id,stage,logical_key,status,auto_attempt,worker_id,heartbeat_at,cursor_json,output_hash,error_code,error_message FROM jobs WHERE run_id=? ORDER BY stage,logical_key", (run_id,))
            payload = {key: run[key] for key in run.keys()}
            payload["stages"] = [{key: row[key] for key in row.keys()} for row in stages]
            payload["jobs"] = [
                {
                    **{key: row[key] for key in row.keys()},
                    "error_message": (str(row["error_message"]).replace(str(database.path.parent), "<workspace>") if row["error_message"] else None),
                }
                for row in jobs
            ]
            config_snapshot = json.loads(run["config_snapshot_json"] or "{}")
        payload["stale"] = [marker.to_dict() for marker in self.worker.detect_stale(run_id)]
        payload.pop("config_snapshot_json", None)
        payload["config_snapshot"] = config_snapshot
        return payload

    def list_runs(self, minecraft_version: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.data_root.workspace.is_dir():
            return rows
        versions = [minecraft_version] if minecraft_version else sorted(path.name for path in self.data_root.workspace.iterdir() if path.is_dir())
        for version in versions:
            version_dir = self.data_root.workspace / version
            if not version_dir.is_dir():
                continue
            for run_dir in sorted(version_dir.iterdir(), key=lambda path: path.name):
                db_path = run_dir / "work.sqlite3"
                if not db_path.is_file():
                    continue
                database = WorkspaceDatabase.open(db_path, read_only=True)
                try:
                    row = database.fetchone("SELECT run_id,minecraft_version,status,current_stage,boundary_event,created_at,started_at,finished_at FROM runs LIMIT 1")
                    if row:
                        rows.append({key: row[key] for key in row.keys()})
                finally:
                    database.close()
        return rows

    def pause(self, run_id: str) -> dict[str, Any]:
        return self._run_command(run_id, "pause")

    def resume(self, run_id: str) -> dict[str, Any]:
        return self._run_command(run_id, "resume")

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    row = connection.execute("SELECT status,current_stage FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if row is None:
                        raise KeyError(run_id)
                    require_transition(row["status"], "cancelled")
                    connection.execute("UPDATE jobs SET status='failed',error_code='RUN_CANCELLED',error_message='cancelled by operator',worker_id=NULL,finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    connection.execute("UPDATE stage_runs SET status='cancelled',worker_id=NULL,pause_after_item=0,finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    connection.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise RunStateConflict("running run changed during cancel")
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), "RUN_CANCELLED", run_id, "{}", now))
        return self.get_run(run_id)

    def retry_failed(self, run_id: str) -> dict[str, Any]:
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if row is None:
                        raise KeyError(run_id)
                    require_transition(row["status"], "pending")
                    connection.execute("UPDATE jobs SET status='pending',error_code=NULL,error_message=NULL,worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND status='failed'", (run_id,))
                    connection.execute("UPDATE stage_runs SET status='pending',worker_id=NULL,recovery_attempt=0,pause_after_item=0,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND status='failed'", (run_id,))
                    connection.execute("UPDATE runs SET status='pending',finished_at=NULL WHERE run_id=? AND status='failed'", (run_id,))
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise RunStateConflict("failed run changed during retry")
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), "RUN_RETRY_FAILED", run_id, "{}", now))
        return self.get_run(run_id)

    def recover(self, run_id: str, job_id: str | None = None, *, stage: str | None = None) -> dict[str, Any]:
        result = self.worker.recover(run_id, job_id, stage=stage)
        return {"recovered": result, "run": self.get_run(run_id)}

    def stale_markers(self, run_id: str | None = None) -> list[StaleMarker]:
        return self.worker.detect_stale(run_id)

    def tick(self, run_id: str) -> dict[str, Any]:
        return self.worker.tick(run_id)

    def query_workspace(self, run_id: str, query: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self.worker.open_database(run_id) as database:
            return [hit.to_dict() for hit in WorkspaceQueryService(database).query(query, limit=limit)]

    def _run_command(self, run_id: str, command: str) -> dict[str, Any]:
        pause_requested = False
        event = ""
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    row = connection.execute("SELECT status,current_stage,boundary_event FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if row is None:
                        raise KeyError(run_id)
                    if command == "pause":
                        require_transition(row["status"], "paused")
                        stage = connection.execute("SELECT stage,status FROM stage_runs WHERE run_id=? AND status='running' ORDER BY ordinal LIMIT 1", (run_id,)).fetchone()
                        running_job = connection.execute("SELECT 1 FROM jobs WHERE run_id=? AND status='running'", (run_id,)).fetchone()
                        if stage is not None and stage["stage"] == "EXTRACT_FEATURES" and running_job is not None:
                            connection.execute("UPDATE stage_runs SET pause_after_item=1 WHERE run_id=? AND stage=? AND status='running'", (run_id, stage["stage"]))
                            connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), "RUN_PAUSE_REQUESTED", run_id, json.dumps({"boundary": "after_item"}, sort_keys=True), now))
                            pause_requested = True
                            run_update = None
                        else:
                            run_update = connection.execute("UPDATE runs SET status='paused' WHERE run_id=? AND status='running'", (run_id,))
                            connection.execute("UPDATE stage_runs SET status='paused',worker_id=NULL,pause_after_item=0 WHERE run_id=? AND status='running'", (run_id,))
                            event = "RUN_PAUSED"
                    else:
                        if row["boundary_event"]:
                            raise RunStateConflict("R3 boundary has no Phase 1 resume handler")
                        if row["status"] != "paused":
                            raise RunStateConflict("resume requires ordinary paused run")
                        require_transition(row["status"], "running")
                        run_update = connection.execute("UPDATE runs SET status='running' WHERE run_id=? AND status='paused'", (run_id,))
                        connection.execute("UPDATE stage_runs SET status='pending',worker_id=NULL,pause_after_item=0 WHERE run_id=? AND status='paused'", (run_id,))
                        event = "RUN_RESUMED"
                    if not pause_requested:
                        if run_update is None or run_update.rowcount != 1:
                            raise RunStateConflict("run changed during state command")
                        connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), event, run_id, "{}", now))
        return self.get_run(run_id)


ApplicationService = StudioService


def _audit_id() -> str:
    return "audit_" + uuid.uuid4().hex


def check_import(service: StudioService, source_directory: str | Path, minecraft_version: str) -> ImportCheck:
    return service.check_import(source_directory, minecraft_version)


def import_checked(service: StudioService, check_id: str, *, copy_mode: str = "copy_to_workspace") -> dict[str, Any]:
    return service.import_checked(check_id, copy_mode=copy_mode)
