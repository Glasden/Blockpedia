"""Persistent in-process worker for the R2 pipeline boundary."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .features import axis_aligned_union, build_visual_variant_record, extract_features
from .importer import _check_property_membership, _read_jsonl, _validate_projection_references
from .paths import DataRoot, safe_relative_posix_ref
from .schema import RecordSchemaError, validate_record
from .search import WorkspaceQueryService, human_semantics_complete
from .stages import R2_STAGES, R3_BUILD_RELEASE_BOUNDARY_EVENT, R3_BOUNDARY_EVENT, R3_STAGES, RunStateConflict, STUDIO_STAGES
from .storage import WorkspaceDatabase, utc_now
from .toolchain import ToolchainProbe
from .provider import (
    OpenAIProvider,
    ProviderError,
    ProviderProfile,
    ProviderProfileStore,
    ProviderResult,
    SecretResolver,
    build_cache_key,
    build_provider_batch_envelope,
    sanitize_validation_diagnostic,
    validate_annotation_batch,
)
from .r3 import ContactSheet, canonical_json, make_contact_sheet, safe_machine_metadata, safe_prompt, sha256_bytes, sha256_json


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


class _AIClaimConflict(RuntimeError):
    pass


RunScope = tuple[str, str]
TaskKey = tuple[str, str, str]


@dataclass(slots=True)
class _RegisteredAITask:
    """In-memory ownership for one claimed AI batch."""

    root_key: str
    run_id: str
    job_id: str
    owner_worker_id: str
    future: Future[Any] | None = None
    send_started: bool = False
    completion_in_progress: bool = False

    @property
    def scope(self) -> RunScope:
        return (self.root_key, self.run_id)

    @property
    def task_key(self) -> TaskKey:
        return (self.root_key, self.run_id, self.job_id)


class _ProcessCoordinator:
    """The single process-wide AI executor, registry, and coordination lane."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.changed = threading.Condition(self.lock)
        self.executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="blockpedia-ai")
        self.registry: dict[TaskKey, _RegisteredAITask] = {}
        self.run_locks: dict[RunScope, threading.RLock] = {}
        self.owner_stops: dict[str, threading.Event] = {}
        self.process_stopping = False

    def shutdown(self) -> None:
        with self.lock:
            if self.process_stopping:
                return
            self.process_stopping = True
            self.changed.notify_all()
        self.executor.shutdown(wait=True, cancel_futures=False)


_PROCESS_COORDINATOR = _ProcessCoordinator()


def shutdown_process_ai_executor() -> None:
    """Idempotent terminal shutdown used only at Python process exit."""

    _PROCESS_COORDINATOR.shutdown()


atexit.register(shutdown_process_ai_executor)


_DERIVED_REVIEW_CODES = {
    "MISSING_SEMANTIC",
    "MISSING_VERIFIED_SEMANTIC",
    "QUALIFICATION_REVIEW_MISSING",
    "SKIP_REVIEW_MISSING",
    "FTS_BUILD_FAILED",
    "FTS_COVERAGE_MISSING",
}


# D-040 deliberately keeps this classification small and stable.  Keep the
# sets local to the worker boundary: provider implementations may grow more
# detailed diagnostics, but the persisted workflow decision must not change.
FATAL_PROVIDER_ERROR_CODES = frozenset(
    {
        "PROVIDER_NOT_CONFIGURED",
        "PROVIDER_CONFIG_INVALID",
        "PROVIDER_CAPABILITY_MISSING",
        "PROVIDER_AUTH_FAILED",
        "PROVIDER_PERMISSION_DENIED",
        "PROVIDER_MODEL_UNAVAILABLE",
    }
)

ITEM_LOCAL_PROVIDER_ERROR_CODES = frozenset(
    {
        "PROVIDER_NETWORK_ERROR",
        "PROVIDER_TIMEOUT",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_SERVER_ERROR",
        "PROVIDER_SCHEMA_INVALID_REPAIRABLE",
        "PROVIDER_SCHEMA_INVALID",
        "PROVIDER_REQUEST_INVALID",
        "PROVIDER_PAYLOAD_TOO_LARGE",
        "PROVIDER_REFUSAL",
        "PROVIDER_INCOMPLETE",
        "PROVIDER_OUTPUT_ID_MISMATCH",
        "PROVIDER_MACHINE_FACT_CONFLICT",
        "PROVIDER_UNKNOWN",
        "PROVIDER_CACHE_KEY_INVALID",
        "IDEMPOTENCY_CONFLICT",
    }
)


def classify_provider_error_code(error_code: str | None) -> str:
    """Return the stable D-040 workflow class for a provider error code."""

    if error_code in FATAL_PROVIDER_ERROR_CODES:
        return "fatal"
    if error_code == "PROVIDER_CANCELLED":
        return "control"
    # This old diagnostic is intentionally not a separate workflow class.
    if error_code == "PROVIDER_STORAGE_UNSUPPORTED":
        return "item_local"
    if error_code in ITEM_LOCAL_PROVIDER_ERROR_CODES or not error_code:
        return "item_local"
    return "item_local"


def normalize_provider_error_code(error_code: str | None) -> str | None:
    """Map the retired storage diagnostic to the stable unknown code."""

    if error_code == "PROVIDER_STORAGE_UNSUPPORTED":
        return "PROVIDER_UNKNOWN"
    return error_code


classify_provider_error = classify_provider_error_code


class WorkerService:
    def __init__(
        self,
        data_root: DataRoot,
        *,
        repo_root: Path | None = None,
        stale_after_seconds: int = 300,
        force_normalized_like: bool = False,
        toolchain_probe: Any | None = None,
        provider_factory: Any | None = None,
        profile_store: ProviderProfileStore | None = None,
        secret_resolver: SecretResolver | None = None,
    ):
        self.data_root = data_root
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.stale_after_seconds = stale_after_seconds
        self.force_normalized_like = force_normalized_like
        self.toolchain_probe = toolchain_probe or ToolchainProbe(self.repo_root)
        self.provider_factory = provider_factory
        self.profile_store = profile_store or ProviderProfileStore(path=self.data_root.cache / "provider-profiles.json")
        self.secret_resolver = secret_resolver or SecretResolver()
        self.worker_id = "worker_" + uuid.uuid4().hex
        self._root_key = os.path.normcase(str(self.data_root.root.resolve(strict=False)))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        self._closing = False
        self._stopping = False
        # These aliases deliberately point at the process-wide objects.  They
        # retain the narrow inspection surface used by existing tests without
        # creating per-worker executors, registries, or run locks.
        self._ai_registry = _PROCESS_COORDINATOR.registry
        self._ai_registry_lock = _PROCESS_COORDINATOR.lock
        self._ai_registry_changed = _PROCESS_COORDINATOR.changed
        with _PROCESS_COORDINATOR.lock:
            _PROCESS_COORDINATOR.owner_stops[self.worker_id] = self._stop

    @contextmanager
    def run_lock(self, run_id: str):
        scope = self._scope(run_id)
        # Lookup is protected, but the shared run lock is acquired only after
        # releasing the coordinator lock.  This keeps the lock order at
        # shared run lock -> SQLite transaction -> coordinator operation.
        with _PROCESS_COORDINATOR.lock:
            lock = _PROCESS_COORDINATOR.run_locks.setdefault(scope, threading.RLock())
        with lock:
            yield

    def _scope(self, run_id: str) -> RunScope:
        return (self._root_key, run_id)

    def _task_key(self, run_id: str, job_id: str) -> TaskKey:
        return (self._root_key, run_id, job_id)

    @property
    def _ai_executor(self) -> ThreadPoolExecutor:
        """Compatibility view of the one process-wide AI executor."""

        return _PROCESS_COORDINATOR.executor

    def _owner_has_live_entries_locked(self) -> bool:
        return any(entry.owner_worker_id == self.worker_id for entry in _PROCESS_COORDINATOR.registry.values())

    def _scope_entries_locked(self, scope: RunScope, *, exclude_entry_key: TaskKey | None = None) -> tuple[_RegisteredAITask, ...]:
        return tuple(
            entry
            for entry in _PROCESS_COORDINATOR.registry.values()
            if entry.scope == scope and entry.task_key != exclude_entry_key
        )

    def has_live_ai_futures(self, run_id: str) -> bool:
        """Return whether any worker owns a claimed AI task in this run."""

        with _PROCESS_COORDINATOR.lock:
            return bool(self._scope_entries_locked(self._scope(run_id)))

    has_registered_ai_jobs = has_live_ai_futures

    def registered_ai_jobs(self, run_id: str | None = None) -> tuple[dict[str, Any], ...]:
        """Return a narrow, thread-safe view of registered AI tasks."""

        with _PROCESS_COORDINATOR.lock:
            entries = (
                self._scope_entries_locked(self._scope(run_id))
                if run_id is not None
                else tuple(_PROCESS_COORDINATOR.registry.values())
            )
            return tuple(
                {
                    "run_id": entry.run_id,
                    "job_id": entry.job_id,
                    "owner_worker_id": entry.owner_worker_id,
                    "send_started": entry.send_started,
                    "completion_in_progress": entry.completion_in_progress,
                    "future_done": bool(entry.future is not None and entry.future.done()),
                }
                for entry in entries
            )

    def is_ai_job_send_started(self, run_id: str, job_id: str) -> bool:
        with _PROCESS_COORDINATOR.lock:
            entry = _PROCESS_COORDINATOR.registry.get(self._task_key(run_id, job_id))
            return bool(entry is not None and entry.send_started)

    def _reserve_ai_task(self, run_id: str, job_id: str, run_concurrency: int) -> _RegisteredAITask | None:
        scope = self._scope(run_id)
        key = self._task_key(run_id, job_id)
        with _PROCESS_COORDINATOR.lock:
            owner_stop = _PROCESS_COORDINATOR.owner_stops[self.worker_id]
            if (
                _PROCESS_COORDINATOR.process_stopping
                or owner_stop.is_set()
                or key in _PROCESS_COORDINATOR.registry
                or len(_PROCESS_COORDINATOR.registry) >= 5
                or len(self._scope_entries_locked(scope)) >= run_concurrency
            ):
                return None
            entry = _RegisteredAITask(self._root_key, run_id, job_id, self.worker_id)
            _PROCESS_COORDINATOR.registry[key] = entry
            _PROCESS_COORDINATOR.changed.notify_all()
            return entry

    def _set_ai_future(self, entry: _RegisteredAITask, future: Future[Any]) -> None:
        with _PROCESS_COORDINATOR.lock:
            if _PROCESS_COORDINATOR.registry.get(entry.task_key) is entry:
                entry.future = future
                _PROCESS_COORDINATOR.changed.notify_all()

    def _unregister_ai_task(self, entry: _RegisteredAITask, future: Future[Any] | None = None) -> None:
        with _PROCESS_COORDINATOR.lock:
            current = _PROCESS_COORDINATOR.registry.get(entry.task_key)
            if current is entry and (future is None or entry.future is future):
                _PROCESS_COORDINATOR.registry.pop(entry.task_key, None)
                _PROCESS_COORDINATOR.changed.notify_all()

    def _begin_ai_completion(self, entry: _RegisteredAITask, future: Future[Any]) -> bool:
        with _PROCESS_COORDINATOR.lock:
            current = _PROCESS_COORDINATOR.registry.get(entry.task_key)
            if current is not entry or entry.future is not future or entry.completion_in_progress:
                return False
            entry.completion_in_progress = True
            _PROCESS_COORDINATOR.changed.notify_all()
            return True

    def _try_mark_send_started(self, entry: _RegisteredAITask, run_concurrency: int) -> bool:
        """Linearize stop and the final pre-HTTP send decision."""

        with _PROCESS_COORDINATOR.lock:
            current = _PROCESS_COORDINATOR.registry.get(entry.task_key)
            owner_stop = _PROCESS_COORDINATOR.owner_stops[self.worker_id]
            scope_entries = self._scope_entries_locked(entry.scope)
            if (
                current is not entry
                or entry.owner_worker_id != self.worker_id
                or owner_stop.is_set()
                or _PROCESS_COORDINATOR.process_stopping
                or entry.completion_in_progress
                or entry.send_started
                or len(_PROCESS_COORDINATOR.registry) > 5
                or len(scope_entries) > run_concurrency
            ):
                return False
            entry.send_started = True
            _PROCESS_COORDINATOR.changed.notify_all()
            return True

    def _owner_is_stopping(self) -> bool:
        with _PROCESS_COORDINATOR.lock:
            return _PROCESS_COORDINATOR.owner_stops[self.worker_id].is_set() or _PROCESS_COORDINATOR.process_stopping

    def _submit_ai_task(self, entry: _RegisteredAITask, run_id: str, job_id: str) -> Future[Any] | None:
        """Submit one entry in order while sharing the coordinator lane."""

        with _PROCESS_COORDINATOR.lock:
            owner_stop = _PROCESS_COORDINATOR.owner_stops[self.worker_id]
            if (
                _PROCESS_COORDINATOR.process_stopping
                or owner_stop.is_set()
                or _PROCESS_COORDINATOR.registry.get(entry.task_key) is not entry
            ):
                return None
            try:
                future = _PROCESS_COORDINATOR.executor.submit(self._run_ai_task, run_id, job_id)
            except Exception:
                return None
            entry.future = future
            _PROCESS_COORDINATOR.changed.notify_all()
        future.add_done_callback(lambda completed, owned=entry: self._ai_task_done(owned, completed))
        return future

    def _wait_for_scope_empty(self, run_id: str) -> None:
        scope = self._scope(run_id)
        with _PROCESS_COORDINATOR.changed:
            while self._scope_entries_locked(scope):
                _PROCESS_COORDINATOR.changed.wait()

    def _ai_registry_counts(self, run_id: str | None = None) -> tuple[int, int]:
        with _PROCESS_COORDINATOR.lock:
            total = len(_PROCESS_COORDINATOR.registry)
            per_run = len(self._scope_entries_locked(self._scope(run_id))) if run_id is not None else 0
            return total, per_run

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

    def close(self, *, timeout: float | None = 2.0) -> bool:
        with self._lifecycle_lock:
            if self._closed:
                return True
            self._closing = True
        stopped = self.stop(timeout=timeout)
        with self._lifecycle_lock:
            if stopped:
                self._closed = True
            self._closing = False
        return stopped

    def start(self, *, interval_seconds: float = 0.05) -> bool:
        with self._lifecycle_lock:
            if self._closed or self._closing or self._stopping:
                return False
            if self._thread is not None:
                if self._thread.is_alive():
                    return False
                # A naturally exited thread is no longer an ownership barrier.
                self._thread = None
            with _PROCESS_COORDINATOR.lock:
                if _PROCESS_COORDINATOR.process_stopping or self._owner_has_live_entries_locked():
                    return False
                self._stop.clear()
            thread = threading.Thread(target=self._loop, args=(interval_seconds,), name="blockpedia-worker", daemon=True)
            self._thread = thread
            try:
                thread.start()
            except Exception:
                self._thread = None
                with _PROCESS_COORDINATOR.lock:
                    self._stop.set()
                raise
            return True

    def stop(self, *, timeout: float | None = 2.0) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._lifecycle_lock:
            thread = self._thread
            self._stopping = True
            self_join = thread is threading.current_thread()
        with _PROCESS_COORDINATOR.lock:
            self._stop.set()
            _PROCESS_COORDINATOR.changed.notify_all()
        if self_join:
            with self._lifecycle_lock:
                self._stopping = False
            return False

        owner_empty = self._wait_for_owner_entries(deadline)
        if not owner_empty:
            with self._lifecycle_lock:
                self._stopping = False
            return False

        remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
        if thread is not None:
            thread.join(timeout=remaining)
        thread_dead = thread is None or not thread.is_alive()
        with self._lifecycle_lock:
            if thread_dead and thread is not None and self._thread is thread:
                self._thread = None
            self._stopping = False
        return owner_empty and thread_dead

    def _wait_for_owner_entries(self, deadline: float | None) -> bool:
        with _PROCESS_COORDINATOR.changed:
            while any(entry.owner_worker_id == self.worker_id for entry in _PROCESS_COORDINATOR.registry.values()):
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                _PROCESS_COORDINATOR.changed.wait(timeout=remaining)
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
        serial_wait = False
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
            if stage not in R2_STAGES and stage not in R3_STAGES:
                self._stop_at_build_boundary(database, run_id, stage)
                return _row_dict(database.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
            if not self._begin_stage(database, run_id, stage):
                return _row_dict(database.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
            if stage == "EXTRACT_FEATURES":
                self._extract_one(database, run_id)
            elif stage == "AI_ANNOTATE":
                # Serialize only the bounded claim/submission section.  The
                # task body and its final send gate run after this lock is
                # released so pause/cancel and provider completion callbacks
                # can make progress concurrently.
                with self.run_lock(run_id):
                    self._annotate_one(database, run_id)
                with _PROCESS_COORDINATOR.lock:
                    serial_wait = bool(self._scope_entries_locked(self._scope(run_id)))
                # Direct service-level callers historically used a
                # default-concurrency tick as a completion boundary.  A
                # serial run may retain that compatibility without
                # changing the parallel scheduler for 2..5.
                try:
                    profile = self._run_profile(database, run_id)
                except StageFailure:
                    serial_wait = False
                else:
                    if int(getattr(profile.stages["offline_annotation"], "concurrency", 1)) != 1:
                        serial_wait = False
            elif stage == "VALIDATE":
                self._validate_r3_stage(database, run_id)
            elif stage == "HUMAN_REVIEW":
                self._human_review_stage(database, run_id)
            else:
                self._finish_simple_stage(database, run_id, stage)
            result = _row_dict(database.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,)))
        if serial_wait:
            self._wait_for_scope_empty(run_id)
            with self.open_database(run_id, minecraft_version) as database:
                refreshed = database.fetchone("SELECT * FROM runs WHERE run_id = ?", (run_id,))
                if refreshed is not None:
                    result = _row_dict(refreshed)
        return result

    def _run_is_stale(self, database: WorkspaceDatabase, run_id: str) -> bool:
        if self.has_live_ai_futures(run_id):
            return False
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

    def _stop_at_build_boundary(self, database: WorkspaceDatabase, run_id: str, stage: str) -> None:
        with self.run_lock(run_id):
            now = utc_now()
            with database.transaction() as connection:
                updated = connection.execute(
                    "UPDATE runs SET status='paused',current_stage='BUILD_RELEASE',boundary_event=? WHERE run_id=? AND status='running' AND boundary_event IS NULL",
                    (R3_BUILD_RELEASE_BOUNDARY_EVENT, run_id),
                )
                if updated.rowcount != 1:
                    return
                connection.execute(
                    "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                    (_id("audit"), R3_BUILD_RELEASE_BOUNDARY_EVENT, run_id, _json({"pending_stage": stage}), now),
                )

    def _annotate_one(self, database: WorkspaceDatabase, run_id: str) -> None:
        """Schedule a bounded approved prefix; task bodies receive IDs only."""

        try:
            self._ensure_ai_jobs_from_config(database, run_id)
        except StageFailure as exc:
            self._persist_stage_failure(database, run_id, "AI_ANNOTATE", exc)
            return
        self._heartbeat_registered_ai(database, run_id)
        try:
            profile = self._run_profile(database, run_id)
        except StageFailure as exc:
            pending = database.fetchone(
                "SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND status='pending' ORDER BY logical_key,job_id LIMIT 1",
                (run_id,),
            )
            if pending is None:
                self._persist_stage_failure(database, run_id, "AI_ANNOTATE", exc)
                return
            now = utc_now()
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE jobs SET status='running',worker_id=?,started_at=COALESCE(started_at,?),heartbeat_at=? WHERE job_id=? AND status='pending'",
                    (self.worker_id, now, now, pending["job_id"]),
                )
            claimed = database.fetchone("SELECT * FROM jobs WHERE job_id=?", (pending["job_id"],))
            if claimed is not None:
                self._commit_ai_failure(database, run_id, claimed, exc.error_code, exc.error_code, None, None, None)
            return
        limit = int(getattr(profile.stages["offline_annotation"], "concurrency", 1))
        total, per_run = self._ai_registry_counts(run_id)
        slots = min(5 - total, limit - per_run)
        if self._owner_is_stopping() or slots <= 0:
            return

        rows = database.fetchall(
            "SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY logical_key,job_id",
            (run_id,),
        )
        selected: list[Any] = []
        barrier: Any | None = None
        for row in rows:
            if row["status"] in {"succeeded", "needs_review", "failed", "skipped"}:
                continue
            if row["status"] != "pending":
                barrier = row
                break
            cursor = _load_object(row["cursor_json"])
            if cursor.get("approved") is not True:
                barrier = row
                break
            try:
                _validate_persisted_ai_identity(row)
            except (TypeError, ValueError):
                barrier = row
                break
            if len(selected) >= slots:
                break
            selected.append(row)

        entries: list[tuple[_RegisteredAITask, str]] = []
        if selected:
            now = utc_now()
            reserved: list[_RegisteredAITask] = []
            try:
                with database.transaction() as connection:
                    for row in selected:
                        entry = self._reserve_ai_task(run_id, str(row["job_id"]), limit)
                        if entry is None:
                            raise _AIClaimConflict("AI reservation changed before wave commit")
                        reserved.append(entry)
                        updated = connection.execute(
                            "UPDATE jobs SET status='running',worker_id=?,started_at=COALESCE(started_at,?),heartbeat_at=? WHERE job_id=? AND run_id=? AND stage='AI_ANNOTATE' AND status='pending'",
                            (self.worker_id, now, now, row["job_id"], run_id),
                        )
                        if updated.rowcount != 1:
                            raise _AIClaimConflict("AI claim changed before wave commit")
                entries = [(entry, str(row["job_id"])) for row, entry in zip(selected, reserved)]
            except _AIClaimConflict:
                for entry in reserved:
                    self._unregister_ai_task(entry)
                return
            except Exception:
                for entry in reserved:
                    self._unregister_ai_task(entry)
                raise

        for index, (entry, job_id) in enumerate(entries):
            future = self._submit_ai_task(entry, run_id, job_id)
            if future is None:
                suffix = entries[index:]
                try:
                    self._restore_claimed_unsent(run_id, [suffix_job_id for _suffix_entry, suffix_job_id in suffix])
                finally:
                    for suffix_entry, _suffix_job_id in suffix:
                        self._unregister_ai_task(suffix_entry)
                break

        if not entries and not self.has_live_ai_futures(run_id):
            if barrier is not None and barrier["status"] == "pending" and not _load_object(barrier["cursor_json"]).get("approved", False):
                self._pause_for_approval(database, run_id, barrier)
            else:
                self._finish_ai_stage(database, run_id)

    def _heartbeat_registered_ai(self, database: WorkspaceDatabase, run_id: str) -> None:
        entries = self.registered_ai_jobs(run_id)
        if not entries:
            return
        now = utc_now()
        with database.transaction() as connection:
            for item in entries:
                connection.execute(
                    "UPDATE jobs SET heartbeat_at=? WHERE run_id=? AND job_id=? AND status='running' AND worker_id=?",
                    (now, run_id, item["job_id"], self.worker_id),
                )
            connection.execute(
                "UPDATE stage_runs SET heartbeat_at=? WHERE run_id=? AND stage='AI_ANNOTATE' AND status='running' AND worker_id=?",
                (now, run_id, self.worker_id),
            )

    def _restore_claimed_unsent(self, run_id: str, job_ids: Iterable[str]) -> None:
        job_ids = tuple(job_ids)
        if not job_ids:
            return
        with self.run_lock(run_id):
            with self.open_database(run_id) as database:
                with database.transaction() as connection:
                    for job_id in job_ids:
                        connection.execute(
                            "UPDATE jobs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND job_id=? AND status='running' AND worker_id=?",
                            (run_id, job_id, self.worker_id),
                        )

    def _run_ai_task(self, run_id: str, job_id: str) -> None:
        payload: dict[str, Any] | None = None
        profile: ProviderProfile | None = None
        send_started = False
        job: Any = {"run_id": run_id, "job_id": job_id, "logical_key": job_id, "input_signature": None, "cursor_json": "{}"}
        try:
            with self.open_database(run_id) as database:
                row = database.fetchone("SELECT * FROM jobs WHERE run_id=? AND job_id=?", (run_id, job_id))
                if row is None:
                    return
                job = dict(row)
                profile = self._run_profile(database, run_id)
                payload = self._ai_payload(database, row, prompt_version=profile.prompt_version)
                payload_signature = _annotation_payload_signature(
                    payload,
                    profile,
                    retry_nonce=_load_object(row["cursor_json"]).get("retry_nonce"),
                )

            gated = self._final_ai_send_gate(run_id, job_id, payload_signature)
            if gated is None:
                return
            job, profile = gated
            send_started = True
            envelope = build_provider_batch_envelope(
                profile,
                request_id=_request_id(job["logical_key"], job["input_signature"], run_id=run_id, job_id=job_id),
                stage="offline_annotation",
                input_summary={"tile_variant_map": payload["tile_map"]},
                export_id=payload["export_id"],
            )
            provider = self._new_provider(profile)
            try:
                result = provider.annotate(
                    payload["prompt"],
                    image_png=payload["contact_sheet"].image_png,
                    image_hash=payload["contact_sheet"].image_sha256,
                    machine_metadata_hash=payload["machine_metadata_hash"],
                    envelope=envelope,
                    machine_metadata=payload["machine_metadata"],
                    source_images=payload["source_images"],
                    cache_parts={"preview_hash": payload["contact_sheet"].image_sha256, "feature_hash": payload["feature_hash"]},
                )
            finally:
                self._close_provider(provider)
            with self.open_database(run_id) as database:
                self._commit_ai_result(database, run_id, job, profile, envelope, payload, result)
        except StageFailure as exc:
            with self.open_database(run_id) as database:
                self._commit_ai_failure(database, run_id, job, exc.error_code, exc.error_code, None, None, payload)
        except Exception as exc:
            error_code = _provider_exception_code(exc)
            with self.open_database(run_id) as database:
                if not send_started and profile is not None:
                    self._commit_ai_input_failure(database, run_id, job, _safe_diagnostic(exc, error_code="AI_BATCH_INPUT_INVALID"), payload)
                else:
                    self._commit_ai_failure(database, run_id, job, _safe_diagnostic(exc, error_code=error_code), error_code, None, None, payload)

    def _final_ai_send_gate(self, run_id: str, job_id: str, payload_signature: str) -> tuple[dict[str, Any], ProviderProfile] | None:
        """Linearize the send only after a fresh, locked final read."""

        with self.run_lock(run_id):
            with self.open_database(run_id) as database:
                with database.transaction() as connection:
                    with _PROCESS_COORDINATOR.lock:
                        entry = _PROCESS_COORDINATOR.registry.get(self._task_key(run_id, job_id))
                    run = connection.execute("SELECT status,current_stage FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    stage = connection.execute("SELECT status,worker_id FROM stage_runs WHERE run_id=? AND stage='AI_ANNOTATE'", (run_id,)).fetchone()
                    job = connection.execute("SELECT * FROM jobs WHERE run_id=? AND job_id=?", (run_id, job_id)).fetchone()
                    profile: ProviderProfile | None = None
                    try:
                        profile = self._run_profile(database, run_id)
                    except StageFailure:
                        profile = None
                    cursor = _load_object(job["cursor_json"]) if job is not None else {}
                    valid = bool(
                        entry is not None
                        and run is not None
                        and run["status"] == "running"
                        and run["current_stage"] == "AI_ANNOTATE"
                        and stage is not None
                        and stage["status"] == "running"
                        and stage["worker_id"] == self.worker_id
                        and job is not None
                        and job["status"] == "running"
                        and job["worker_id"] == self.worker_id
                        and cursor.get("approved") is True
                        and cursor.get("payload_signature") == job["input_signature"]
                        and cursor.get("input_hash") == job["input_signature"]
                        and job["input_signature"] == payload_signature
                        and _is_sha256_hash(payload_signature)
                        and profile is not None
                    )
                    run_concurrency = int(getattr(profile.stages["offline_annotation"], "concurrency", 1)) if profile is not None else 1
                    if valid and entry is not None and profile is not None and job is not None and self._try_mark_send_started(entry, run_concurrency):
                        return ({key: job[key] for key in job.keys()}, profile)
                    if job is None or entry is None:
                        return None
                    if run is not None and run["status"] == "cancelled":
                        connection.execute("UPDATE jobs SET status='failed',worker_id=NULL,heartbeat_at=?,finished_at=?,error_code='RUN_CANCELLED',error_message='cancelled by operator' WHERE run_id=? AND job_id=? AND status='running' AND worker_id=?", (utc_now(), utc_now(), run_id, job_id, self.worker_id))
                    elif run is not None and run["status"] == "failed":
                        connection.execute("UPDATE jobs SET status='failed',worker_id=NULL,heartbeat_at=?,finished_at=?,error_code='PROVIDER_CANCELLED',error_message='run stopped' WHERE run_id=? AND job_id=? AND status='running' AND worker_id=?", (utc_now(), utc_now(), run_id, job_id, self.worker_id))
                    elif (run is not None and (run["status"] == "paused" or stage is None or stage["status"] != "running")) or self._owner_is_stopping():
                        connection.execute("UPDATE jobs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND job_id=? AND status='running' AND worker_id=?", (run_id, job_id, self.worker_id))
                    elif job["input_signature"] != payload_signature or cursor.get("approved") is not True:
                        cursor["approved"] = False
                        cursor["payload_signature"] = payload_signature
                        cursor["input_hash"] = payload_signature
                        now = utc_now()
                        connection.execute("UPDATE jobs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL,input_signature=?,cursor_json=? WHERE run_id=? AND job_id=? AND status='running' AND worker_id=?", (payload_signature, canonical_json(cursor), run_id, job_id, self.worker_id))
                        connection.execute("UPDATE stage_runs SET status='paused',worker_id=NULL,heartbeat_at=?,finished_at=? WHERE run_id=? AND stage='AI_ANNOTATE' AND status IN ('running','pending')", (now, now, run_id))
                        connection.execute("UPDATE runs SET status='paused',current_stage='AI_ANNOTATE',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND status IN ('pending','running')", (run_id,))
                    else:
                        connection.execute("UPDATE jobs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND job_id=? AND status='running' AND worker_id=?", (run_id, job_id, self.worker_id))
                    return None

    def _ai_task_done(self, entry: _RegisteredAITask, future: Future[Any]) -> None:
        if not self._begin_ai_completion(entry, future):
            return
        try:
            with self.run_lock(entry.run_id):
                with self.open_database(entry.run_id) as database:
                    self._finish_ai_stage(database, entry.run_id, exclude_entry_key=entry.task_key)
        except Exception as exc:
            print(f"blockpedia worker diagnostic: {_safe_diagnostic(exc)}", file=sys.stderr)
        finally:
            self._unregister_ai_task(entry, future)

    def _pause_for_approval(self, database: WorkspaceDatabase, run_id: str, job: Any) -> None:
        with self.run_lock(run_id):
            now = utc_now()
            with database.transaction() as connection:
                connection.execute("UPDATE stage_runs SET status='paused',worker_id=NULL,heartbeat_at=?,finished_at=? WHERE run_id=? AND stage='AI_ANNOTATE' AND status='running'", (now, now, run_id))
                connection.execute("UPDATE runs SET status='paused' WHERE run_id=? AND status='running'", (run_id,))
                if connection.execute("SELECT changes()").fetchone()[0] == 0:
                    return
                connection.execute(
                    "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                    (_id("audit"), "AI_BATCH_APPROVAL_REQUIRED", run_id, job["job_id"], _json({"logical_key": job["logical_key"], "input_signature": job["input_signature"]}), now),
                )

    def _finish_ai_stage(self, database: WorkspaceDatabase, run_id: str, *, exclude_entry_key: TaskKey | None = None) -> None:
        with self.run_lock(run_id):
            with _PROCESS_COORDINATOR.lock:
                if self._scope_entries_locked(self._scope(run_id), exclude_entry_key=exclude_entry_key):
                    return
            stage = database.fetchone("SELECT status,worker_id FROM stage_runs WHERE run_id=? AND stage='AI_ANNOTATE'", (run_id,))
            run = database.fetchone("SELECT status FROM runs WHERE run_id=?", (run_id,))
            remaining = database.fetchone("SELECT 1 FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND status IN ('pending','running')", (run_id,))
            if stage is None or run is None or stage["status"] != "running" or stage["worker_id"] != self.worker_id or remaining is not None:
                return
            outputs = [{"logical_key": row["logical_key"], "output_hash": row["output_hash"], "status": row["status"]} for row in database.fetchall("SELECT logical_key,output_hash,status FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY logical_key", (run_id,))]
            self._complete_r3_stage(database, run_id, "AI_ANNOTATE", {"outputs": outputs})

    def _commit_ai_result(self, database: WorkspaceDatabase, run_id: str, job: Any, profile: ProviderProfile, envelope: dict[str, Any], payload: dict[str, Any], result: Any) -> None:
        normalized = _provider_result(result)
        attempts = max(0, min(2, normalized["attempts_used"]))
        canonical_cache = _canonical_annotation_cache_key(payload, profile)
        supplied_cache = normalized["cache_key"]
        if supplied_cache is not None and supplied_cache != canonical_cache:
            self._commit_ai_failure(
                database,
                run_id,
                job,
                "PROVIDER_CACHE_KEY_INVALID",
                "PROVIDER_CACHE_KEY_INVALID",
                canonical_cache,
                None,
                payload,
                attempts=attempts,
                request_evidence=None,
            )
            return
        cache_key = canonical_cache
        artifact = normalized["parsed_artifact"]
        result_error = normalize_provider_error_code(normalized["error_code"])
        provider_validation_diagnostic = normalized["validation_diagnostic"]
        validation_diagnostic = None
        # Persist the deterministic local request identity.  A provider's
        # redacted response ID is not guaranteed to be unique across runs.
        request_id = envelope["request_id"]
        validation = None
        if normalized["status"] == "succeeded" and isinstance(artifact, dict):
            validation = validate_annotation_batch(
                artifact,
                [item["variant_id"] for item in payload["tile_map"]],
                profile,
                cache_key=cache_key,
                artifact_hash=normalized["artifact_hash"],
            )
            if validation.annotations:
                artifact_hash = validation.artifact_hash
            else:
                result_error = validation.error_code or "PROVIDER_SCHEMA_INVALID"
            if validation is not None and validation.annotations:
                previous = database.fetchone(
                    "SELECT status,validated_artifact_sha256 FROM provider_requests WHERE cache_key=? AND status='succeeded' ORDER BY attempt DESC LIMIT 1",
                    (cache_key,),
                )
                if previous is not None and previous["validated_artifact_sha256"] not in {None, validation.artifact_hash}:
                    self._commit_ai_failure(database, run_id, job, "IDEMPOTENCY_CONFLICT", "IDEMPOTENCY_CONFLICT", cache_key, validation.artifact_hash, payload)
                    return
        provider_request = _provider_request_evidence(
            request_id=request_id,
            profile=profile,
            job=job,
            envelope=envelope,
            attempts=attempts,
            cache_key=cache_key,
            validated_artifact_sha256=validation.artifact_hash if validation is not None and validation.annotations else normalized["artifact_hash"],
            error_code=result_error,
            error_class=normalized["error_class"],
            status=(
                "succeeded"
                if validation is not None and validation.annotations
                else "failed"
                if classify_provider_error_code(result_error) == "fatal" or normalized["status"] == "failed"
                else "needs_review"
            ),
        )
        if validation is None or not validation.annotations:
            failure_code = result_error or "PROVIDER_SCHEMA_INVALID"
            if validation is not None and validation.validation_diagnostic is not None:
                validation_diagnostic = validation.validation_diagnostic
            elif (
                normalized["status"] == "needs_review"
                and normalized["parsed_artifact"] is None
                and attempts == 2
                and result_error == "PROVIDER_SCHEMA_INVALID"
                and normalized["error_class"] == "validation"
            ):
                validation_diagnostic = provider_validation_diagnostic
            self._commit_ai_failure(
                database,
                run_id,
                job,
                failure_code,
                failure_code,
                cache_key,
                normalized["artifact_hash"],
                payload,
                attempts=attempts,
                request_evidence=provider_request,
                validation_diagnostic=validation_diagnostic,
            )
            return
        output_payload = {"schema_version": "annotation-batch-output.v1", "annotations": list(validation.annotations)}
        output_hash = sha256_json(output_payload)
        artifact_ref = f"generated/ai/{job['job_id']}.json"
        priority_by_variant = {item["variant_id"]: ("high" if item["confidence"] < 0.65 else "normal" if item["confidence"] < 0.80 else None) for item in artifact["items"]}
        with self.run_lock(run_id):
            with database.transaction() as connection:
                owner = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                current = connection.execute("SELECT status,worker_id FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                if owner is None or current is None or current["status"] != "running" or current["worker_id"] != self.worker_id:
                    return
                _insert_provider_request(connection, provider_request)
                _write_atomic(database.path.parent / artifact_ref, canonical_json(output_payload).encode("utf-8"))
                annotation_ids: list[str] = []
                for annotation in validation.annotations:
                    annotation_ids.append(annotation["annotation_id"])
                    connection.execute("INSERT OR REPLACE INTO annotations(annotation_id,subject_type,subject_id,minecraft_version,record_json) VALUES (?,?,?,?,?)", (annotation["annotation_id"], annotation["subject_type"], annotation["subject_id"], "26.2", canonical_json(annotation)))
                    variant_row = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (annotation["subject_id"],)).fetchone()
                    if variant_row is None:
                        raise StageFailure("PROVIDER_OUTPUT_ID_MISMATCH", "annotation target missing")
                    variant = json.loads(variant_row["record_json"])
                    refs = list(variant.get("annotation_refs", []))
                    if annotation["annotation_id"] not in refs:
                        refs.append(annotation["annotation_id"])
                    variant["annotation_refs"] = refs
                    validate_record("visual-variant-record.v1", variant, repo_root=self.repo_root)
                    connection.execute("UPDATE variants SET record_json=? WHERE variant_id=?", (canonical_json(variant), annotation["subject_id"]))
                    priority = priority_by_variant.get(annotation["subject_id"])
                    if priority:
                        self.create_review_task(
                            connection,
                            "variant",
                            annotation["subject_id"],
                            "LOW_CONFIDENCE",
                            priority,
                            "Annotation requires human review.",
                            [f"annotation:{annotation['annotation_id']}"],
                            dedupe_key=job["input_signature"],
                        )
                    elif annotation.get("confidence", 0) >= 0.80 and _sampled_quality_review(database, run_id, annotation["subject_id"]):
                        self.create_review_task(
                            connection,
                            "variant",
                            annotation["subject_id"],
                            "SAMPLED_QUALITY_REVIEW",
                            "normal",
                            "Deterministic sampled quality review.",
                            [f"annotation:{annotation['annotation_id']}"],
                            dedupe_key=job["input_signature"],
                        )
                status = "succeeded" if validation.priority == "normal" and validation.review_route == "auto_valid" else "needs_review"
                connection.execute("UPDATE jobs SET status=?,worker_id=NULL,heartbeat_at=?,finished_at=?,output_hash=?,cursor_json=?,error_code=?,error_message=? WHERE job_id=? AND status='running' AND worker_id=?", (status, utc_now(), utc_now(), output_hash, job["cursor_json"], None if status == "succeeded" else ("LOW_CONFIDENCE" if validation.priority in {"normal", "high"} else None), None, job["job_id"], self.worker_id))
                connection.execute("INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)", (_id("artifact"), job["job_id"], "ai_annotation", artifact_ref, output_hash, _json({"variant_ids": [item["variant_id"] for item in payload["tile_map"]]})))
                connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)", (_id("audit"), "AI_BATCH_SUCCEEDED", run_id, job["job_id"], _json({"output_hash": output_hash, "annotation_count": len(annotation_ids)}), utc_now()))

    def _commit_ai_failure(
        self,
        database: WorkspaceDatabase,
        run_id: str,
        job: Any,
        error_code: str,
        review_code: str,
        cache_key: str | None,
        artifact_hash: str | None,
        payload: dict[str, Any] | None = None,
        *,
        attempts: int = 0,
        request_evidence: dict[str, Any] | None = None,
        validation_diagnostic: Mapping[str, Any] | None = None,
    ) -> None:
        error_code = normalize_provider_error_code(error_code) or "PROVIDER_UNKNOWN"
        if error_code not in FATAL_PROVIDER_ERROR_CODES and error_code not in ITEM_LOCAL_PROVIDER_ERROR_CODES and error_code != "PROVIDER_CANCELLED":
            error_code = "PROVIDER_UNKNOWN"
        workflow_class = classify_provider_error_code(error_code)
        with self.run_lock(run_id):
            with database.transaction() as connection:
                current = connection.execute("SELECT status,worker_id FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                if current is None or current["status"] != "running" or current["worker_id"] != self.worker_id:
                    return
                cursor = _load_object(job["cursor_json"])
                variant_ids = payload.get("tile_map", []) if payload else []
                variant_values = [item.get("variant_id") for item in variant_ids if isinstance(item, dict)] if variant_ids else cursor.get("variant_ids", cursor.get("tile_ids", []))
                review_reason = "IDEMPOTENCY_CONFLICT" if error_code == "IDEMPOTENCY_CONFLICT" else "PROVIDER_FAILURE"
                evidence: list[Any] = [f"job:{job['job_id']}"]
                if request_evidence is not None:
                    evidence.append(f"provider_request:{request_evidence['request_id']}")
                safe_validation_diagnostic = sanitize_validation_diagnostic(validation_diagnostic)
                if review_reason == "PROVIDER_FAILURE" and safe_validation_diagnostic is not None:
                    evidence.append(safe_validation_diagnostic)
                if attempts > 0 and request_evidence is not None:
                    _insert_provider_request(connection, request_evidence)
                now = utc_now()
                if workflow_class != "control":
                    for variant_id in variant_values:
                        if isinstance(variant_id, str) and variant_id.startswith("minecraft:"):
                            self.create_review_task(connection, "variant", variant_id, review_reason, "high", "Provider result requires review.", evidence, dedupe_key=job["input_signature"], reopen=True)
                if workflow_class == "fatal":
                    connection.execute(
                        "UPDATE jobs SET status='failed',worker_id=NULL,heartbeat_at=?,finished_at=?,error_code=?,error_message=? WHERE job_id=? AND status='running' AND worker_id=?",
                        (now, now, error_code, "fatal provider failure", job["job_id"], self.worker_id),
                    )
                    connection.execute(
                        "UPDATE stage_runs SET status='failed',worker_id=NULL,heartbeat_at=?,finished_at=?,cursor_json=? WHERE run_id=? AND stage='AI_ANNOTATE' AND status IN ('running','paused') AND (worker_id=? OR worker_id IS NULL)",
                        (now, now, canonical_json({"error_code": error_code, "job_id": job["job_id"]}), run_id, self.worker_id),
                    )
                    connection.execute(
                        "UPDATE runs SET status='failed',finished_at=? WHERE run_id=? AND status IN ('running','paused')",
                        (now, run_id),
                    )
                    event_type = "AI_BATCH_FATAL_FAILED"
                elif workflow_class == "control":
                    connection.execute(
                        "UPDATE jobs SET status='needs_review',worker_id=NULL,heartbeat_at=?,finished_at=?,error_code=?,error_message=? WHERE job_id=? AND status='running' AND worker_id=?",
                        (now, now, error_code, "provider operation cancelled", job["job_id"], self.worker_id),
                    )
                    connection.execute(
                        "UPDATE stage_runs SET status='paused',worker_id=NULL,heartbeat_at=?,finished_at=? WHERE run_id=? AND stage='AI_ANNOTATE' AND status='running' AND worker_id=?",
                        (now, now, run_id, self.worker_id),
                    )
                    connection.execute(
                        "UPDATE runs SET status='paused',current_stage='AI_ANNOTATE',finished_at=NULL WHERE run_id=? AND status='running'",
                        (run_id,),
                    )
                    event_type = "AI_BATCH_CANCELLED"
                else:
                    connection.execute(
                        "UPDATE jobs SET status='needs_review',worker_id=NULL,heartbeat_at=?,finished_at=?,error_code=?,error_message=? WHERE job_id=? AND status='running' AND worker_id=?",
                        (now, now, error_code, "provider request requires review", job["job_id"], self.worker_id),
                    )
                    event_type = "AI_BATCH_FAILED"
                connection.execute(
                    "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                    (_id("audit"), event_type, run_id, job["job_id"], _json({"error_code": error_code, "evidence": evidence}), now),
                )

    def _commit_ai_input_failure(self, database: WorkspaceDatabase, run_id: str, job: Any, diagnostic: str, payload: dict[str, Any] | None = None) -> None:
        """Pause a malformed local input without constructing or calling a provider."""

        del payload
        with self.run_lock(run_id):
            with database.transaction() as connection:
                current = connection.execute("SELECT status,worker_id FROM jobs WHERE job_id=?", (job["job_id"],)).fetchone()
                if current is None or current["status"] != "running" or current["worker_id"] != self.worker_id:
                    return
                cursor = _load_object(job["cursor_json"])
                cursor["approved"] = False
                cursor["input_invalid"] = True
                cursor["input_error_code"] = "AI_BATCH_INPUT_INVALID"
                connection.execute(
                    "UPDATE jobs SET status='needs_review',worker_id=NULL,heartbeat_at=?,finished_at=?,error_code='AI_BATCH_INPUT_INVALID',error_message=?,cursor_json=? WHERE job_id=? AND status='running' AND worker_id=?",
                    (utc_now(), utc_now(), diagnostic, canonical_json(cursor), job["job_id"], self.worker_id),
                )
                now = utc_now()
                connection.execute(
                    "UPDATE stage_runs SET status='paused',worker_id=NULL,heartbeat_at=?,finished_at=? WHERE run_id=? AND stage='AI_ANNOTATE' AND status='running' AND worker_id=?",
                    (now, now, run_id, self.worker_id),
                )
                connection.execute(
                    "UPDATE runs SET status='paused',current_stage='AI_ANNOTATE',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND status='running'",
                    (run_id,),
                )
                connection.execute(
                    "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                    (_id("audit"), "AI_BATCH_INPUT_INVALID", run_id, job["job_id"], _json({"error_code": "AI_BATCH_INPUT_INVALID"}), now),
                )

    def _validate_r3_stage(self, database: WorkspaceDatabase, run_id: str) -> None:
        errors: list[tuple[str, str, str]] = []
        try:
            for row in database.fetchall("SELECT block_id,record_json FROM blocks ORDER BY block_id"):
                validate_record("block-record.v1", json.loads(row["record_json"]), repo_root=self.repo_root)
            for row in database.fetchall("SELECT variant_id,record_json FROM variants ORDER BY variant_id"):
                if row["record_json"] is None:
                    continue
                variant = json.loads(row["record_json"])
                try:
                    validate_record("visual-variant-record.v1", variant, repo_root=self.repo_root)
                except RecordSchemaError:
                    errors.append(("variant", row["variant_id"], "SCHEMA_INVALID"))
                refs = variant.get("annotation_refs", [])
                for ref in refs:
                    if database.fetchone("SELECT 1 FROM annotations WHERE annotation_id=?", (ref,)) is None:
                        errors.append(("variant", row["variant_id"], "ANNOTATION_REFERENCE_INVALID"))
                if variant.get("candidate_qualification") in {"eligible", "conditional"} and not refs and not human_semantics_complete(database.connection, str(row["variant_id"])) and not self._has_open_root_review(database.connection, str(row["variant_id"])):
                    errors.append(("variant", row["variant_id"], "MISSING_SEMANTIC"))
                if variant.get("candidate_qualification") == "excluded" and not variant.get("qualification_review_refs"):
                    errors.append(("variant", row["variant_id"], "QUALIFICATION_REVIEW_MISSING"))
            for row in database.fetchall("SELECT annotation_id,record_json FROM annotations ORDER BY annotation_id"):
                try:
                    validate_record("annotation-record.v1", json.loads(row["record_json"]), repo_root=self.repo_root)
                except RecordSchemaError:
                    errors.append(("variant", row["subject_id"], "SCHEMA_INVALID"))
        except Exception:
            errors.append(("run", run_id, "VALIDATE_FAILED"))
        with self.run_lock(run_id):
            with database.transaction() as connection:
                for target_type, target_id, code in errors:
                    self.create_review_task(connection, target_type, target_id, code, "high", "Validation requires human review.", [], dedupe_key=code, reopen=True)
        try:
            WorkspaceQueryService(database).rebuild_index()
        except Exception:
            with self.run_lock(run_id):
                with database.transaction() as connection:
                    self.create_review_task(connection, "run", run_id, "FTS_BUILD_FAILED", "high", "Search index requires review.", [], dedupe_key="fts", reopen=True)
        else:
            with self.run_lock(run_id):
                with database.transaction() as connection:
                    self._reconcile_derived_review_tasks(connection, database, run_id, fts_ready=True)
        self._complete_r3_stage(database, run_id, "VALIDATE", {"error_count": len(errors)})

    def _human_review_stage(self, database: WorkspaceDatabase, run_id: str) -> None:
        with self.run_lock(run_id):
            with database.transaction() as connection:
                self._reconcile_derived_review_tasks(connection, database, run_id)
            open_tasks = database.fetchone("SELECT 1 FROM review_tasks WHERE status='open' AND severity IN ('normal','high') LIMIT 1")
            if open_tasks is not None:
                with database.transaction() as connection:
                    now = utc_now()
                    connection.execute("UPDATE stage_runs SET status='needs_review',worker_id=NULL,finished_at=? WHERE run_id=? AND stage='HUMAN_REVIEW' AND status='running' AND worker_id=?", (now, run_id, self.worker_id))
                    connection.execute("UPDATE runs SET status='needs_review',finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), "HUMAN_REVIEW_REQUIRED", run_id, "{}", now))
                return
            try:
                WorkspaceQueryService(database).rebuild_index()
            except Exception:
                with database.transaction() as connection:
                    self.create_review_task(connection, "run", run_id, "FTS_BUILD_FAILED", "high", "Search index requires review.", [], dedupe_key="fts", reopen=True)
                    now = utc_now()
                    connection.execute("UPDATE stage_runs SET status='needs_review',worker_id=NULL,finished_at=? WHERE run_id=? AND stage='HUMAN_REVIEW' AND status='running' AND worker_id=?", (now, run_id, self.worker_id))
                    connection.execute("UPDATE runs SET status='needs_review',finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), "HUMAN_REVIEW_REQUIRED", run_id, _json({"closure_errors": ["FTS_BUILD_FAILED"]}), now))
                return
            with database.transaction() as connection:
                self._reconcile_derived_review_tasks(connection, database, run_id, fts_ready=True)
            open_tasks = database.fetchone("SELECT 1 FROM review_tasks WHERE status='open' AND severity IN ('normal','high') LIMIT 1")
            if open_tasks is not None:
                with database.transaction() as connection:
                    now = utc_now()
                    connection.execute("UPDATE stage_runs SET status='needs_review',worker_id=NULL,finished_at=? WHERE run_id=? AND stage='HUMAN_REVIEW' AND status='running' AND worker_id=?", (now, run_id, self.worker_id))
                    connection.execute("UPDATE runs SET status='needs_review',finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                return
            closure_errors = self._review_closure_errors(database, run_id)
            if closure_errors:
                with database.transaction() as connection:
                    for target_type, target_id, reason_code, evidence in closure_errors:
                        self.create_review_task(
                            connection,
                            target_type,
                            target_id,
                            reason_code,
                            "high",
                            "R3 closure evidence requires human review.",
                            evidence,
                            dedupe_key=f"closure:{reason_code}",
                            reopen=True,
                        )
                    now = utc_now()
                    connection.execute("UPDATE stage_runs SET status='needs_review',worker_id=NULL,finished_at=? WHERE run_id=? AND stage='HUMAN_REVIEW' AND status='running' AND worker_id=?", (now, run_id, self.worker_id))
                    connection.execute("UPDATE runs SET status='needs_review',finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), "HUMAN_REVIEW_REQUIRED", run_id, _json({"closure_errors": [item[2] for item in closure_errors]}), now))
                return
            self._complete_r3_stage(database, run_id, "HUMAN_REVIEW", {"open_tasks": 0}, boundary=True)

    def _review_closure_errors(self, database: WorkspaceDatabase, run_id: str) -> list[tuple[str, str, str, list[str]]]:
        errors: list[tuple[str, str, str, list[str]]] = []
        searchable: set[str] = set()
        for row in database.fetchall("SELECT variant_id,record_json FROM variants WHERE status='selected' ORDER BY variant_id"):
            variant_id = str(row["variant_id"])
            record = json.loads(row["record_json"] or "{}")
            qualification = record.get("candidate_qualification")
            if qualification in {"eligible", "conditional"}:
                searchable.add(variant_id)
                verified = human_semantics_complete(database.connection, variant_id)
                annotation_evidence: list[str] = []
                for annotation_id in record.get("annotation_refs", []):
                    annotation_row = database.fetchone("SELECT record_json FROM annotations WHERE annotation_id=? AND subject_id=?", (annotation_id, variant_id))
                    if annotation_row is None:
                        continue
                    annotation_evidence.append(f"annotation:{annotation_id}")
                    annotation = json.loads(annotation_row["record_json"] or "{}")
                    if annotation.get("source", {}).get("verified") is True:
                        verified = True
                        break
                if not verified:
                    errors.append(("variant", variant_id, "MISSING_VERIFIED_SEMANTIC", annotation_evidence))
            elif qualification == "excluded":
                valid_qualification = False
                for review_id in record.get("qualification_review_refs", []):
                    review_row = database.fetchone("SELECT record_json FROM overrides WHERE override_id=? AND target_id=?", (review_id, variant_id))
                    if review_row is None:
                        continue
                    review = json.loads(review_row["record_json"] or "{}")
                    try:
                        validate_record("qualification-review.v1", review, repo_root=self.repo_root)
                    except RecordSchemaError:
                        continue
                    if review.get("qualification") == "excluded" and review.get("target_id") == variant_id:
                        valid_qualification = True
                        break
                if not valid_qualification:
                    errors.append(("variant", variant_id, "QUALIFICATION_REVIEW_MISSING", []))

        for row in database.fetchall("SELECT failure_id,block_id,state_id,variant_id,record_json FROM failures ORDER BY failure_id"):
            failure_id = str(row["failure_id"])
            targets = [value for value in (row["variant_id"], row["state_id"], row["block_id"]) if isinstance(value, str)]
            valid_skip = False
            for target_id in targets:
                for review_row in database.fetchall("SELECT record_json FROM overrides WHERE target_id=?", (target_id,)):
                    review = json.loads(review_row["record_json"] or "{}")
                    if review.get("schema_version") != "skip-review.v1":
                        continue
                    try:
                        validate_record("skip-review.v1", review, repo_root=self.repo_root)
                    except RecordSchemaError:
                        continue
                    if review.get("machine_failure_ref") == failure_id and review.get("target_id") == target_id:
                        valid_skip = True
                        break
                if valid_skip:
                    break
            if not valid_skip:
                target_type = "variant" if row["variant_id"] else "state" if row["state_id"] else "block"
                target_id = row["variant_id"] or row["state_id"] or row["block_id"] or failure_id
                errors.append((target_type, str(target_id), "SKIP_REVIEW_MISSING", []))

        try:
            expected_documents = WorkspaceQueryService(database).expected_documents()
            actual_documents = {
                str(row["document_id"]): (str(row["block_id"]), str(row["content"]), str(row["normalized_content"]))
                for row in database.fetchall("SELECT document_id,block_id,content,normalized_content FROM search_documents")
            }
            expected_document_values = {
                document_id: (block_id, content, normalized)
                for document_id, (_document_id, block_id, content, normalized) in expected_documents.items()
            }
            if actual_documents != expected_document_values:
                errors.append(("run", run_id, "FTS_COVERAGE_MISSING", []))
            if database.fts_mode == "trigram":
                expected_blocks = {block_id: normalized for _document_id, block_id, _content, normalized in expected_documents.values()}
                actual_blocks = {str(row["block_id"]): str(row["content"]) for row in database.fetchall("SELECT block_id,content FROM fts_documents")}
                if actual_blocks != expected_blocks:
                    errors.append(("run", run_id, "FTS_COVERAGE_MISSING", []))
        except Exception:
            errors.append(("run", run_id, "FTS_BUILD_FAILED", []))
        return errors

    def _has_open_root_review(self, connection: Any, target_id: str) -> bool:
        rows = connection.execute("SELECT reason_code FROM review_tasks WHERE target_id=? AND status='open'", (target_id,)).fetchall()
        return any(str(row["reason_code"]) not in _DERIVED_REVIEW_CODES for row in rows)

    def _reconcile_derived_review_tasks(self, connection: Any, database: WorkspaceDatabase, run_id: str, *, fts_ready: bool = False) -> None:
        """Resolve only derived tasks whose current evidence is now complete."""

        now = utc_now()
        rows = connection.execute(
            "SELECT review_id,target_type,target_id,reason_code FROM review_tasks WHERE status='open' AND reason_code IN (?,?,?,?,?,?)",
            tuple(sorted(_DERIVED_REVIEW_CODES)),
        ).fetchall()
        for row in rows:
            target_id = str(row["target_id"])
            reason_code = str(row["reason_code"])
            satisfied = False
            if reason_code == "MISSING_SEMANTIC":
                variant = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
                record = json.loads(variant["record_json"] or "{}") if variant is not None else {}
                satisfied = (
                    variant is None
                    or record.get("candidate_qualification") not in {"eligible", "conditional"}
                    or self._has_open_root_review(connection, target_id)
                    or bool(record.get("annotation_refs"))
                    or human_semantics_complete(connection, target_id)
                )
            elif reason_code == "MISSING_VERIFIED_SEMANTIC":
                variant = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
                record = json.loads(variant["record_json"] or "{}") if variant is not None else {}
                satisfied = (
                    variant is None
                    or record.get("candidate_qualification") not in {"eligible", "conditional"}
                    or self._has_open_root_review(connection, target_id)
                    or human_semantics_complete(connection, target_id)
                )
                if not satisfied:
                    for annotation_id in record.get("annotation_refs", []):
                        annotation = connection.execute("SELECT record_json FROM annotations WHERE annotation_id=? AND subject_id=?", (annotation_id, target_id)).fetchone()
                        if annotation is not None and json.loads(annotation["record_json"]).get("source", {}).get("verified") is True:
                            satisfied = True
                            break
            elif reason_code == "QUALIFICATION_REVIEW_MISSING":
                variant = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
                record = json.loads(variant["record_json"] or "{}") if variant is not None else {}
                satisfied = variant is None or record.get("candidate_qualification") != "excluded"
                if not satisfied:
                    for review_id in record.get("qualification_review_refs", []):
                        review = connection.execute("SELECT record_json FROM overrides WHERE override_id=? AND target_id=?", (review_id, target_id)).fetchone()
                        if review is None:
                            continue
                        try:
                            value = json.loads(review["record_json"])
                            validate_record("qualification-review.v1", value, repo_root=self.repo_root)
                        except (RecordSchemaError, TypeError, ValueError):
                            continue
                        if value.get("qualification") == record.get("candidate_qualification"):
                            satisfied = True
                            break
            elif reason_code == "SKIP_REVIEW_MISSING":
                failure_rows = connection.execute("SELECT failure_id FROM failures WHERE variant_id=? OR state_id=? OR block_id=?", (target_id, target_id, target_id)).fetchall()
                failure_ids = {str(item["failure_id"]) for item in failure_rows}
                satisfied = not failure_ids
                for review in connection.execute("SELECT record_json FROM overrides WHERE target_id=?", (target_id,)).fetchall():
                    try:
                        value = json.loads(review["record_json"])
                        validate_record("skip-review.v1", value, repo_root=self.repo_root)
                    except (RecordSchemaError, TypeError, ValueError):
                        continue
                    if str(value.get("machine_failure_ref")) in failure_ids:
                        satisfied = True
                        break
            elif reason_code in {"FTS_BUILD_FAILED", "FTS_COVERAGE_MISSING"}:
                satisfied = fts_ready and self._fts_projection_current(database)
            if satisfied:
                connection.execute("UPDATE review_tasks SET status='resolved',resolved_at=? WHERE review_id=? AND status='open'", (now, row["review_id"]))

    def _fts_projection_current(self, database: WorkspaceDatabase) -> bool:
        expected_documents = WorkspaceQueryService(database).expected_documents()
        actual_documents = {
            str(row["document_id"]): (str(row["block_id"]), str(row["content"]), str(row["normalized_content"]))
            for row in database.fetchall("SELECT document_id,block_id,content,normalized_content FROM search_documents")
        }
        expected_values = {
            document_id: (block_id, content, normalized)
            for document_id, (_document_id, block_id, content, normalized) in expected_documents.items()
        }
        if actual_documents != expected_values:
            return False
        if database.fts_mode == "trigram":
            expected_blocks = {block_id: normalized for _document_id, block_id, _content, normalized in expected_documents.values()}
            actual_blocks = {str(row["block_id"]): str(row["content"]) for row in database.fetchall("SELECT block_id,content FROM fts_documents")}
            return actual_blocks == expected_blocks
        return True

    def _complete_r3_stage(self, database: WorkspaceDatabase, run_id: str, stage: str, evidence: dict[str, Any], *, boundary: bool = False) -> None:
        with self.run_lock(run_id):
            with database.transaction() as connection:
                stage_row = connection.execute("SELECT status,worker_id FROM stage_runs WHERE run_id=? AND stage=?", (run_id, stage)).fetchone()
                if stage_row is None or stage_row["worker_id"] != self.worker_id or stage_row["status"] != "running":
                    return
                now = utc_now()
                output_hash = sha256_json(evidence)
                next_stage = "BUILD_RELEASE" if boundary else STUDIO_STAGES[STUDIO_STAGES.index(stage) + 1]
                event = R3_BUILD_RELEASE_BOUNDARY_EVENT if boundary else "STAGE_SUCCEEDED"
                connection.execute("UPDATE stage_runs SET status='succeeded',worker_id=NULL,heartbeat_at=?,finished_at=?,cursor_json=? WHERE run_id=? AND stage=? AND status='running' AND worker_id=?", (now, now, canonical_json({"stage": stage, "output_hash": output_hash, "evidence": evidence}), run_id, stage, self.worker_id))
                connection.execute("UPDATE runs SET status=?,current_stage=?,boundary_event=? WHERE run_id=? AND status='running'", ("paused" if boundary else "running", next_stage, event if boundary else None, run_id))
                connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), event, run_id, _json({"stage": stage, "output_hash": output_hash}), now))

    def _run_profile(self, database: WorkspaceDatabase, run_id: str) -> ProviderProfile:
        run = database.fetchone("SELECT config_snapshot_json FROM runs WHERE run_id=?", (run_id,))
        config = _load_object(run["config_snapshot_json"] if run else "{}")
        snapshot = config.get("provider_snapshot", {})
        snapshot_adapter = snapshot.get("adapter") if isinstance(snapshot, dict) else None
        raw = snapshot.get("profile") if isinstance(snapshot, dict) else None
        if snapshot_adapter not in {"openai_responses", "openai_chat_completions"} or not isinstance(raw, dict):
            raise StageFailure("PROVIDER_CONFIG_INVALID", "run provider snapshot missing frozen profile")
        try:
            profile = ProviderProfile.from_dict(raw)
        except (ProviderError, TypeError, ValueError) as exc:
            raise StageFailure("PROVIDER_CONFIG_INVALID", "run provider snapshot has invalid lineage") from exc
        if profile.adapter != snapshot_adapter:
            raise StageFailure("PROVIDER_CONFIG_INVALID", "run provider snapshot adapter mismatch")
        return profile

    def _new_provider(self, profile: ProviderProfile) -> Any:
        if self.provider_factory is not None:
            try:
                return self.provider_factory(profile, profile_store=self.profile_store, repo_root=self.repo_root, secret_resolver=self.secret_resolver)
            except TypeError:
                return self.provider_factory(profile)
        # A Worker request is bound to the profile reconstructed from the run
        # snapshot.  Passing the mutable profile store here would let
        # OpenAIProvider._effective_profile replace that lineage between run
        # creation and send.  SecretResolver still resolves the secret at
        # request time, as required by the provider boundary.
        return OpenAIProvider(profile, profile_store=None, repo_root=self.repo_root, secret_resolver=self.secret_resolver)

    @staticmethod
    def _close_provider(provider: Any) -> None:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    def _ensure_ai_jobs_from_config(self, database: WorkspaceDatabase, run_id: str) -> None:
        if database.fetchone("SELECT 1 FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' LIMIT 1", (run_id,)) is not None:
            return
        run = database.fetchone("SELECT config_snapshot_json FROM runs WHERE run_id=?", (run_id,))
        config = _load_object(run["config_snapshot_json"] if run else "{}")
        snapshot = config.get("provider_snapshot", {})
        snapshot_adapter = snapshot.get("adapter") if isinstance(snapshot, dict) else None
        raw = snapshot.get("profile") if isinstance(snapshot, dict) else None
        if snapshot_adapter not in {"openai_responses", "openai_chat_completions"} or not isinstance(raw, dict):
            raise StageFailure("PROVIDER_CONFIG_INVALID", "run provider snapshot missing frozen profile")
        try:
            profile = ProviderProfile.from_dict(raw)
        except (ProviderError, TypeError, ValueError) as exc:
            raise StageFailure("PROVIDER_CONFIG_INVALID", "run provider snapshot has invalid lineage") from exc
        if profile.adapter != snapshot_adapter:
            raise StageFailure("PROVIDER_CONFIG_INVALID", "run provider snapshot adapter mismatch")
        specs = build_ai_batch_specs(database, run_id, profile, batch_size=int(config.get("batch_size", 12)), sample_rate=int(config.get("sample_rate", 100)))
        with database.transaction() as connection:
            for spec in specs:
                connection.execute("INSERT OR IGNORE INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (_stable_job_id(run_id, spec["logical_key"], spec["input_signature"]), run_id, "AI_ANNOTATE", spec["logical_key"], spec["input_signature"], "pending", 0, canonical_json({"approved": False, "tile_ids": spec["tile_ids"], "variant_ids": spec["variant_ids"], "input_hash": spec["input_signature"], "payload_signature": spec["payload_signature"]}), utc_now()))

    def build_ai_preview(self, database: WorkspaceDatabase, job: Any) -> dict[str, Any]:
        profile = self._run_profile(database, job["run_id"])
        payload = self._ai_payload(database, job, prompt_version=profile.prompt_version)
        payload_signature = _annotation_payload_signature(
            payload,
            profile,
            retry_nonce=_load_object(job["cursor_json"]).get("retry_nonce"),
        )
        return {
            "job_id": job["job_id"],
            "logical_key": job["logical_key"],
            "input_signature": payload_signature,
            "approved": bool(_load_object(job["cursor_json"]).get("approved")) and job["input_signature"] == payload_signature,
            "payload_signature": payload_signature,
            "tile_ids": [item["tile_id"] for item in payload["tile_map"]],
            "tiles": payload["contact_sheet"].tiles,
            "machine_metadata": payload["machine_metadata"],
            "prompt": payload["prompt"],
            "prompt_text": payload["prompt"],
            "contact_sheet_png": payload["contact_sheet"].image_png,
            "contact_sheet_bytes": payload["contact_sheet"].image_png,
        }

    def build_ai_plan(self, database: WorkspaceDatabase, run_id: str) -> dict[str, Any]:
        """Build a safe plan from persisted identities only.

        Full payload reconstruction remains deliberately confined to the
        single-batch preview and the final pre-send gate in ``_annotate_one``.
        """

        run = database.fetchone(
            "SELECT effective_config_hash,config_snapshot_json FROM runs WHERE run_id=?",
            (run_id,),
        )
        if run is None:
            raise KeyError(run_id)
        effective_config_hash = run["effective_config_hash"]
        if not _is_sha256_hash(effective_config_hash):
            raise ValueError("run effective config hash is invalid")
        config = _load_object(run["config_snapshot_json"])
        snapshot_hash = config.get("effective_config_hash")
        config_for_hash = dict(config)
        config_for_hash.pop("effective_config_hash", None)
        if snapshot_hash != effective_config_hash or sha256_json(config_for_hash) != effective_config_hash:
            raise ValueError("run config snapshot identity is invalid")
        profile = self._run_profile(database, run_id)
        jobs: list[dict[str, Any]] = []
        rows = database.fetchall(
            "SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND status='pending' ORDER BY logical_key,job_id",
            (run_id,),
        )
        for row in rows:
            identity = _validate_persisted_ai_identity(row)
            jobs.append(
                {
                    "job_id": row["job_id"],
                    "logical_key": row["logical_key"],
                    "input_signature": identity["input_signature"],
                    "tile_ids": identity["tile_ids"],
                    "variant_ids": identity["variant_ids"],
                }
            )
        hash_jobs = [
            {
                "job_id": job["job_id"],
                "logical_key": job["logical_key"],
                "recomputed_payload_signature": job["input_signature"],
            }
            for job in jobs
        ]
        plan_hash = build_ai_plan_hash(run_id, run["effective_config_hash"], hash_jobs)
        return {
            "run_id": run_id,
            "effective_config_hash": run["effective_config_hash"],
            "plan_hash": plan_hash,
            "count": len(jobs),
            "jobs": jobs,
            "profile_id": profile.profile_id,
            "adapter": profile.adapter,
            "requested_model_id": profile.model_id,
            "model_id": profile.model_id,
        }

    def current_ai_input_signature(self, database: WorkspaceDatabase, run_id: str, job: Any) -> str | None:
        try:
            profile = self._run_profile(database, run_id)
            return self._payload_signature(database, run_id, job, profile)
        except (KeyError, OSError, TypeError, ValueError, StageFailure):
            return None

    def _ai_payload(
        self,
        database: WorkspaceDatabase,
        job: Any,
        *,
        prompt_version: str | None = None,
    ) -> dict[str, Any]:
        cursor = _load_object(job["cursor_json"])
        variant_ids = cursor.get("variant_ids", [])
        if not isinstance(variant_ids, list) or not variant_ids:
            raise ValueError("AI batch has no tiles")
        return _build_annotation_payload(
            database,
            variant_ids,
            run_id=str(job["run_id"]),
            prompt_version=prompt_version,
        )

    def _payload_signature(self, database: WorkspaceDatabase, run_id: str, job: Any, profile: ProviderProfile) -> str:
        payload = self._ai_payload(database, job, prompt_version=profile.prompt_version)
        cursor = _load_object(job["cursor_json"])
        return _annotation_payload_signature(payload, profile, retry_nonce=cursor.get("retry_nonce"))

    def _reset_ai_approval(self, connection: Any, run_id: str, job: Any, signature: str) -> None:
        """Atomically invalidate approval after the send payload changes."""

        cursor = _load_object(job["cursor_json"])
        cursor["approved"] = False
        cursor["payload_signature"] = signature
        connection.execute(
            "UPDATE jobs SET input_signature=?,status='pending',worker_id=NULL,heartbeat_at=NULL,error_code=NULL,error_message=NULL,finished_at=NULL,cursor_json=? WHERE job_id=?",
            (signature, canonical_json(cursor), job["job_id"]),
        )
        now = utc_now()
        connection.execute(
            "UPDATE stage_runs SET status='paused',worker_id=NULL,heartbeat_at=?,finished_at=? WHERE run_id=? AND stage='AI_ANNOTATE' AND status IN ('running','pending')",
            (now, now, run_id),
        )
        connection.execute(
            "UPDATE runs SET status='paused',current_stage='AI_ANNOTATE',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND status IN ('pending','running','paused')",
            (run_id,),
        )

    def _refresh_ai_job_for_payload_change(self, database: WorkspaceDatabase, run_id: str, job: Any, signature: str) -> None:
        with self.run_lock(run_id):
            with database.transaction() as connection:
                self._reset_ai_approval(connection, run_id, job, signature)
                connection.execute(
                    "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                    (_id("audit"), "AI_BATCH_APPROVAL_REQUIRED", run_id, job["job_id"], _json({"logical_key": job["logical_key"], "input_signature": signature, "payload_changed": True}), utc_now()),
                )

    def create_review_task(self, connection: Any, target_type: str, target_id: str, reason_code: str, severity: str, note: str, evidence: list[Any], *, dedupe_key: str | None = None, reopen: bool = False) -> str:
        review_id = _review_task_id(target_type, target_id, reason_code, severity, dedupe_key)
        connection.execute("INSERT OR IGNORE INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (review_id, "26.2", target_type, target_id, reason_code, severity, "open", note[:500], canonical_json(evidence[:64]), utc_now()))
        if reopen:
            connection.execute(
                "UPDATE review_tasks SET status='open',note=?,evidence_json=?,resolved_at=NULL WHERE review_id=? AND status IN ('resolved','rejected')",
                (note[:500], canonical_json(evidence[:64]), review_id),
            )
        return review_id

    def _retry_child_for_source(self, connection: Any, run_id: str, source_job_id: str) -> Any | None:
        source = connection.execute("SELECT logical_key FROM jobs WHERE job_id=? AND run_id=?", (source_job_id, run_id)).fetchone()
        if source is None:
            return None
        legacy_prefix = f"{source['logical_key']}:retry:"
        for row in connection.execute("SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY created_at,job_id", (run_id,)).fetchall():
            cursor = _load_object(row["cursor_json"])
            if cursor.get("retry_of_job_id") == source_job_id or (
                isinstance(row["logical_key"], str) and row["logical_key"].startswith(legacy_prefix)
            ):
                return row
        return None

    @staticmethod
    def _provider_retry_eligible(job: Any, *, allow_legacy: bool = False) -> bool:
        del allow_legacy
        code = normalize_provider_error_code(job["error_code"])
        if job["status"] not in {"needs_review", "failed"}:
            return False
        return code in ITEM_LOCAL_PROVIDER_ERROR_CODES

    @staticmethod
    def _legacy_review_retry_eligible(job: Any) -> bool:
        code = normalize_provider_error_code(job["error_code"])
        return (job["status"] == "needs_review" and code == "LOW_CONFIDENCE") or (
            job["status"] == "skipped" and code == "AI_BATCH_CANCELLED"
        )

    def create_provider_retry_job(
        self,
        database: WorkspaceDatabase,
        run_id: str,
        source_job_id: str,
        *,
        approve: bool = False,
        allow_legacy: bool = False,
        variant_ids_override: list[str] | None = None,
    ) -> dict[str, Any]:
        """Create or return the deterministic child for one Provider job."""

        connection = database.connection
        source = connection.execute(
            "SELECT * FROM jobs WHERE run_id=? AND job_id=? AND stage='AI_ANNOTATE'",
            (run_id, source_job_id),
        ).fetchone()
        if source is None or not (
            self._provider_retry_eligible(source)
            or (allow_legacy and self._legacy_review_retry_eligible(source))
        ):
            raise ValueError("source job is not an eligible Provider retry")
        existing = self._retry_child_for_source(connection, run_id, source_job_id)
        if existing is not None:
            if approve:
                cursor = _load_object(existing["cursor_json"])
                cursor["approved"] = True
                connection.execute("UPDATE jobs SET cursor_json=? WHERE job_id=?", (canonical_json(cursor), existing["job_id"]))
            return {
                "source_job_id": source_job_id,
                "job_id": existing["job_id"],
                "input_signature": existing["input_signature"],
                "logical_key": existing["logical_key"],
                "idempotent": True,
                "approved": bool(_load_object(existing["cursor_json"]).get("approved")) or approve,
            }
        source_cursor = _load_object(source["cursor_json"])
        source_variant_ids = variant_ids_override or source_cursor.get("variant_ids", [])
        if not isinstance(source_variant_ids, list) or not source_variant_ids or any(not isinstance(item, str) for item in source_variant_ids):
            raise ValueError("source job has no variant lineage")
        nonce = "sha256:" + hashlib.sha256(f"{source_job_id}\0{source['input_signature']}".encode("utf-8")).hexdigest()
        logical_key = f"{source['logical_key']}:retry:{nonce[7:15]}"
        profile = self._run_profile(database, run_id)
        signature, payload = _batch_input_signature(
            database,
            list(source_variant_ids),
            profile,
            run_id=run_id,
            retry_nonce=nonce,
        )
        job_id = _stable_job_id(run_id, logical_key, signature)
        cursor = {
            "approved": bool(approve),
            "tile_ids": [item["tile_id"] for item in payload["tile_map"]],
            "variant_ids": list(source_variant_ids),
            "input_hash": signature,
            "payload_signature": signature,
            "retry_nonce": nonce,
            "retry_of_job_id": source_job_id,
        }
        connection.execute(
            "INSERT OR IGNORE INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job_id, run_id, "AI_ANNOTATE", logical_key, signature, "pending", 0, canonical_json(cursor), utc_now()),
        )
        source_input_signature = source["input_signature"]
        for review in connection.execute(
            "SELECT review_id,target_id,evidence_json FROM review_tasks WHERE minecraft_version='26.2' AND status='open' AND reason_code='PROVIDER_FAILURE' AND target_type='variant'",
        ).fetchall():
            evidence = _review_evidence_values(review["evidence_json"])
            explicitly_bound = f"job:{source_job_id}" in evidence
            legacy_id = _review_task_id(
                "variant",
                str(review["target_id"]),
                "PROVIDER_FAILURE",
                "high",
                source_input_signature,
            )
            legacy_bound = not evidence and review["review_id"] == legacy_id
            if review["target_id"] in source_variant_ids and (explicitly_bound or legacy_bound):
                connection.execute(
                    "UPDATE review_tasks SET status='resolved',resolved_at=? WHERE review_id=? AND status='open'",
                    (utc_now(), review["review_id"]),
                )
        connection.execute(
            "UPDATE stage_runs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND stage IN ('AI_ANNOTATE','VALIDATE','HUMAN_REVIEW') AND status != 'cancelled'",
            (run_id,),
        )
        connection.execute(
            "UPDATE runs SET status='pending',current_stage='AI_ANNOTATE',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND status NOT IN ('succeeded','cancelled')",
            (run_id,),
        )
        now = utc_now()
        connection.execute(
            "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
            (_id("audit"), "AI_PROVIDER_RETRY_CREATED", run_id, job_id, _json({"source_job_id": source_job_id, "retry_nonce": nonce, "approved": approve}), now),
        )
        return {
            "source_job_id": source_job_id,
            "job_id": job_id,
            "input_signature": signature,
            "logical_key": logical_key,
            "retry_nonce": nonce,
            "variant_ids": list(source_variant_ids),
            "idempotent": False,
            "approved": approve,
        }

    def provider_retry_spec(self, database: WorkspaceDatabase, run_id: str, source_job_id: str) -> dict[str, Any]:
        """Compute a retry child identity without writing persistence."""

        connection = database.connection
        source = connection.execute(
            "SELECT * FROM jobs WHERE run_id=? AND job_id=? AND stage='AI_ANNOTATE'",
            (run_id, source_job_id),
        ).fetchone()
        if source is None or not self._provider_retry_eligible(source):
            raise ValueError("source job is not an eligible Provider retry")
        existing = self._retry_child_for_source(connection, run_id, source_job_id)
        if existing is not None:
            return {
                "source_job_id": source_job_id,
                "child_job_id": existing["job_id"],
                "child_logical_key": existing["logical_key"],
                "child_input_signature": existing["input_signature"],
                "source_input_signature": source["input_signature"],
                "variant_ids": list(_load_object(source["cursor_json"]).get("variant_ids", [])),
                "existing": True,
            }
        source_cursor = _load_object(source["cursor_json"])
        variant_ids = source_cursor.get("variant_ids", [])
        if not isinstance(variant_ids, list) or not variant_ids or any(not isinstance(item, str) for item in variant_ids):
            raise ValueError("source job has no variant lineage")
        nonce = "sha256:" + hashlib.sha256(f"{source_job_id}\0{source['input_signature']}".encode("utf-8")).hexdigest()
        logical_key = f"{source['logical_key']}:retry:{nonce[7:15]}"
        profile = self._run_profile(database, run_id)
        signature, payload = _batch_input_signature(database, list(variant_ids), profile, run_id=run_id, retry_nonce=nonce)
        return {
            "source_job_id": source_job_id,
            "child_job_id": _stable_job_id(run_id, logical_key, signature),
            "child_logical_key": logical_key,
            "child_input_signature": signature,
            "source_input_signature": source["input_signature"],
            "retry_nonce": nonce,
            "variant_ids": list(variant_ids),
            "tile_ids": [item["tile_id"] for item in payload["tile_map"]],
            "existing": False,
        }

    def create_retry_ai_job(self, database: WorkspaceDatabase, run_id: str, target_id: str, review_id: str) -> dict[str, Any]:
        """Backward-compatible review action mapped to a source job."""

        connection = database.connection
        review = connection.execute("SELECT reason_code,evidence_json FROM review_tasks WHERE review_id=?", (review_id,)).fetchone()
        if review is None:
            raise ValueError("review task missing")
        source = None
        for item in _review_evidence_values(review["evidence_json"]):
            if isinstance(item, str) and item.startswith("job:"):
                source = connection.execute("SELECT * FROM jobs WHERE run_id=? AND job_id=? AND stage='AI_ANNOTATE'", (run_id, item[4:])).fetchone()
                if source is not None:
                    break
        if source is None:
            for job in connection.execute("SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY logical_key,job_id", (run_id,)).fetchall():
                cursor = _load_object(job["cursor_json"])
                if target_id in cursor.get("variant_ids", []) and (
                    self._provider_retry_eligible(job) or self._legacy_review_retry_eligible(job)
                ):
                    source = job
                    break
        if source is None:
            raise ValueError("review target is not an AI batch")
        legacy = not self._provider_retry_eligible(source)
        override = [target_id] if legacy and review["reason_code"] == "AI_BATCH_CANCELLED" else None
        return self.create_provider_retry_job(
            database,
            run_id,
            source["job_id"],
            allow_legacy=legacy,
            variant_ids_override=override,
        )

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
            if self.has_live_ai_futures(run_id):
                return []
            version = self._find_run_version(run_id)
            paths = [self.data_root.workspace_dir(version, run_id) / "work.sqlite3"]
        for path in paths:
            with WorkspaceDatabase.open(path, force_normalized_like=self.force_normalized_like, read_only=True) as database:
                identity = database.fetchone("SELECT run_id FROM runs LIMIT 1")
                if identity is not None and self.has_live_ai_futures(str(identity["run_id"])):
                    continue
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
            if self.has_live_ai_futures(run_id):
                raise RunStateConflict("live AI work cannot be recovered")
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


def _load_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _review_evidence_values(value: Any) -> list[Any]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return list(parsed)
    if parsed is None:
        return []
    return [parsed]


def _is_sha256_hash(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", value) is not None


def _validate_persisted_ai_identity(job: Any) -> dict[str, Any]:
    input_signature = job["input_signature"]
    cursor = _load_object(job["cursor_json"])
    payload_signature = cursor.get("payload_signature")
    input_hash = cursor.get("input_hash")
    tile_ids = cursor.get("tile_ids")
    variant_ids = cursor.get("variant_ids")
    if not _is_sha256_hash(input_signature) or not _is_sha256_hash(payload_signature) or not _is_sha256_hash(input_hash):
        raise ValueError("AI job persisted hash is invalid")
    if payload_signature != input_signature or input_hash != input_signature:
        raise ValueError("AI job persisted hashes disagree")
    if (
        not isinstance(tile_ids, list)
        or not isinstance(variant_ids, list)
        or not tile_ids
        or not variant_ids
        or len(tile_ids) != len(variant_ids)
        or any(not isinstance(value, str) or not value for value in tile_ids)
        or any(not isinstance(value, str) or not value for value in variant_ids)
        or len(set(tile_ids)) != len(tile_ids)
        or len(set(variant_ids)) != len(variant_ids)
    ):
        raise ValueError("AI job persisted tile/variant identity is invalid")
    return {
        "input_signature": input_signature,
        "tile_ids": list(tile_ids),
        "variant_ids": list(variant_ids),
    }


def _stable_job_id(run_id: str, logical_key: str, input_signature: str) -> str:
    return "job_" + hashlib.sha256(f"{run_id}\0{logical_key}\0{input_signature}".encode("utf-8")).hexdigest()[:32]


def _review_task_id(target_type: str, target_id: str, reason_code: str, severity: str, dedupe_key: str | None) -> str:
    return "review_" + hashlib.sha256(
        canonical_json(
            {
                "target_type": target_type,
                "target_id": target_id,
                "reason_code": reason_code,
                "severity": severity,
                "dedupe_key": dedupe_key,
            }
        ).encode("utf-8")
    ).hexdigest()[:32]


def build_ai_plan_hash(run_id: str, effective_config_hash: str | None, jobs: list[dict[str, str]]) -> str:
    """Hash exactly the D-040 remaining-plan identity."""

    return sha256_json(
        {
            "run_id": run_id,
            "effective_config_hash": effective_config_hash,
            "jobs": [
                {
                    "job_id": job["job_id"],
                    "logical_key": job["logical_key"],
                    "recomputed_payload_signature": job.get(
                        "recomputed_payload_signature", job.get("input_signature")
                    ),
                }
                for job in jobs
            ],
        }
    )


def build_retry_wave_hash(run_id: str, effective_config_hash: str | None, jobs: list[dict[str, str]]) -> str:
    return sha256_json(
        {
            "run_id": run_id,
            "effective_config_hash": effective_config_hash,
            "jobs": [
                {
                    "source_job_id": job["source_job_id"],
                    "child_job_id": job["child_job_id"],
                    "source_input_signature": job["source_input_signature"],
                    "child_input_signature": job["child_input_signature"],
                }
                for job in jobs
            ],
        }
    )


def _request_id(logical_key: str, input_signature: str, *, run_id: str = "", job_id: str = "") -> str:
    return "Req" + hashlib.sha256(f"{run_id}\0{job_id}\0{logical_key}\0{input_signature}".encode("utf-8")).hexdigest()[:32]


def _provider_result(value: Any) -> dict[str, Any]:
    if isinstance(value, ProviderResult):
        return {
            "status": value.status,
            "parsed_artifact": value.parsed_artifact,
            "attempts_used": value.attempts_used,
            "error_code": value.error_code,
            "error_class": value.error_class,
            "cache_key": value.cache_key,
            "artifact_hash": value.artifact_hash,
            "request_id": value.request_id_redacted,
            "validation_diagnostic": sanitize_validation_diagnostic(value.validation_diagnostic),
        }
    if isinstance(value, dict):
        artifact = value.get("parsed_artifact", value.get("artifact", value if value.get("schema_id") else None))
        return {
            "status": value.get("status", "succeeded" if artifact is not None else "needs_review"),
            "parsed_artifact": artifact,
            "attempts_used": int(value.get("attempts_used", 1) or 0),
            "error_code": value.get("error_code"),
            "error_class": value.get("error_class", "unknown"),
            "cache_key": value.get("cache_key"),
            "artifact_hash": value.get("artifact_hash"),
            "request_id": value.get("request_id_redacted") or value.get("request_id"),
            "validation_diagnostic": sanitize_validation_diagnostic(value.get("validation_diagnostic")),
        }
    return {
        "status": "needs_review",
        "parsed_artifact": None,
        "attempts_used": 0,
        "error_code": "PROVIDER_UNKNOWN",
        "error_class": "unknown",
        "cache_key": None,
        "artifact_hash": None,
        "request_id": None,
        "validation_diagnostic": None,
    }


def _provider_exception_code(exc: Exception) -> str:
    candidate = getattr(exc, "error_code", None) or getattr(exc, "code", None)
    if not isinstance(candidate, str) and exc.args and isinstance(exc.args[0], str):
        candidate = exc.args[0]
    if candidate in FATAL_PROVIDER_ERROR_CODES or candidate in ITEM_LOCAL_PROVIDER_ERROR_CODES or candidate in {
        "PROVIDER_STORAGE_UNSUPPORTED",
        "PROVIDER_CANCELLED",
    }:
        return normalize_provider_error_code(candidate) or "PROVIDER_UNKNOWN"
    return "PROVIDER_UNKNOWN"


def _provider_request_evidence(
    *,
    request_id: str,
    profile: ProviderProfile,
    job: Any,
    envelope: dict[str, Any],
    attempts: int,
    cache_key: str,
    validated_artifact_sha256: str | None,
    error_code: str | None,
    error_class: str | None,
    status: str,
) -> dict[str, Any]:
    allowed_classes = {"retryable", "non_retryable", "validation", "authentication", "capability", "unknown"}
    return {
        "request_id": request_id,
        "profile_id": profile.profile_id,
        "stage": "offline_annotation",
        "wire_schema_id": "annotation-batch-output.v1",
        "attempt": max(1, min(2, attempts)),
        "cache_key": cache_key,
        "input_sha256": job["input_signature"],
        "validated_artifact_sha256": validated_artifact_sha256,
        "error_code": error_code,
        "error_class": error_class if error_class in allowed_classes else "unknown",
        "envelope_json": canonical_json(envelope),
        "status": status if status in {"pending", "succeeded", "failed", "needs_review"} else "needs_review",
        "created_at": utc_now(),
    }


def _insert_provider_request(connection: Any, evidence: dict[str, Any]) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO provider_requests(request_id,profile_id,stage,wire_schema_id,attempt,cache_key,input_sha256,validated_artifact_sha256,error_code,error_class,envelope_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            evidence["request_id"],
            evidence["profile_id"],
            evidence["stage"],
            evidence["wire_schema_id"],
            evidence["attempt"],
            evidence["cache_key"],
            evidence["input_sha256"],
            evidence["validated_artifact_sha256"],
            evidence["error_code"],
            evidence["error_class"],
            evidence["envelope_json"],
            evidence["status"],
            evidence["created_at"],
        ),
    )


def _build_annotation_payload(
    database: WorkspaceDatabase,
    variant_ids: list[Any],
    *,
    run_id: str | None = None,
    prompt_version: str | None = None,
) -> dict[str, Any]:
    if not isinstance(variant_ids, list) or not variant_ids or any(not isinstance(value, str) for value in variant_ids):
        raise ValueError("AI batch has no variant IDs")
    variants: list[tuple[str, bytes, dict[str, Any], dict[str, Any]]] = []
    for variant_id in variant_ids:
        row = database.fetchone("SELECT record_json FROM variants WHERE variant_id=? AND status='selected'", (variant_id,))
        if row is None or row["record_json"] is None:
            raise ValueError("AI variant missing")
        record = json.loads(row["record_json"])
        if record.get("candidate_qualification") not in {"eligible", "conditional"}:
            raise ValueError("AI variant is not searchable")
        render = record.get("render", {})
        relative = safe_relative_posix_ref(render.get("preview_path", ""))
        image = (database.path.parent / relative).read_bytes()
        feature_row = database.fetchone("SELECT feature_json FROM features WHERE variant_id=?", (variant_id,))
        feature = json.loads(feature_row["feature_json"]) if feature_row is not None else {}
        metadata = safe_machine_metadata(record, feature)
        variants.append((variant_id, image, metadata, record))
    variants.sort(key=lambda item: item[0].encode("utf-8"))
    sheet = make_contact_sheet([(item[0], item[1]) for item in variants])
    tile_map: list[dict[str, Any]] = []
    metadata_list: list[dict[str, Any]] = []
    machine_metadata: dict[str, Any] = {}
    source_images: dict[str, bytes] = {}
    for index, (variant_id, image, metadata, _record) in enumerate(variants):
        tile_id = f"T{index + 1:02d}"
        machine_metadata[variant_id] = metadata
        source_images[tile_id] = image
        tile_map.append(
            {
                "tile_id": tile_id,
                "variant_id": variant_id,
                "image_sha256": sha256_bytes(image),
                "machine_metadata_sha256": sha256_json(metadata),
            }
        )
        metadata_list.append(metadata)
    if run_id is None:
        run = database.fetchone("SELECT run_id FROM runs LIMIT 1")
        run_id = str(run["run_id"]) if run is not None else ""
    import_row = database.fetchone(
        "SELECT export_id FROM imports WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)",
        (run_id,),
    )
    feature_values: dict[str, Any] = {}
    for variant_id, _, _, _ in variants:
        feature_row = database.fetchone("SELECT output_hash FROM features WHERE variant_id=?", (variant_id,))
        if feature_row is not None:
            feature_values[variant_id] = feature_row["output_hash"]
    return {
        "export_id": import_row["export_id"] if import_row is not None else None,
        "contact_sheet": sheet,
        "tile_map": tile_map,
        "machine_metadata": machine_metadata,
        "machine_metadata_hash": sha256_json(machine_metadata),
        "source_images": source_images,
        "feature_hash": sha256_json(feature_values),
        "prompt": safe_prompt(metadata_list, tile_map, prompt_version=prompt_version),
    }


def _annotation_payload_signature(
    payload: dict[str, Any],
    profile: ProviderProfile,
    *,
    retry_nonce: str | None = None,
) -> str:
    source_images = payload.get("source_images", {})
    source_hashes = {
        str(tile_id): sha256_bytes(bytes(image))
        for tile_id, image in sorted(source_images.items(), key=lambda item: str(item[0]).encode("utf-8"))
    }
    material = {
        "stage": "offline_annotation",
        "contact_sheet_sha256": payload["contact_sheet"].image_sha256,
        "tile_map": list(payload["tile_map"]),
        "machine_metadata_hash": payload["machine_metadata_hash"],
        "prompt_sha256": sha256_bytes(str(payload["prompt"]).encode("utf-8")),
        "source_image_hashes": source_hashes,
        "feature_hash": payload["feature_hash"],
        "export_id": payload.get("export_id"),
        "profile_id": profile.profile_id,
        "adapter": profile.adapter,
        "model_id": profile.model_id,
        "base_url_stable_id": profile.base_url_stable_id,
        "prompt_version": profile.prompt_version,
        "wire_schema_id": "annotation-batch-output.v1",
        "wire_format_name": "annotation_batch_output_v1",
        "retry_nonce": retry_nonce,
    }
    return sha256_json(material)


def _canonical_annotation_cache_key(payload: dict[str, Any], profile: ProviderProfile) -> str:
    contact_sheet = payload["contact_sheet"]
    return build_cache_key(
        {
            "adapter": profile.adapter,
            "image_hash": contact_sheet.image_sha256,
            "machine_metadata_hash": payload["machine_metadata_hash"],
            "prompt_version": profile.prompt_version,
            "model_id": profile.model_id,
            "schema_version": "annotation-batch-output.v1",
            "base_url_stable_id": profile.base_url_stable_id,
            "stage": "offline_annotation",
        },
        context={
            "preview_hash": contact_sheet.image_sha256,
            "feature_hash": payload["feature_hash"],
        },
    )


def _batch_input_signature(
    database: WorkspaceDatabase,
    variant_ids: list[str],
    profile: ProviderProfile,
    *,
    run_id: str | None = None,
    retry_nonce: str | None = None,
) -> tuple[str, dict[str, Any]]:
    payload = _build_annotation_payload(
        database,
        variant_ids,
        run_id=run_id,
        prompt_version=profile.prompt_version,
    )
    return _annotation_payload_signature(payload, profile, retry_nonce=retry_nonce), payload


def _sampled_quality_review(database: WorkspaceDatabase, run_id: str, variant_id: str) -> bool:
    run = database.fetchone("SELECT config_snapshot_json FROM runs WHERE run_id=?", (run_id,))
    config = _load_object(run["config_snapshot_json"] if run is not None else "{}")
    try:
        rate = int(config.get("sample_rate", 100))
    except (TypeError, ValueError):
        rate = 100
    rate = max(0, min(100, rate))
    if rate == 0:
        return False
    if rate == 100:
        return True
    digest = int(hashlib.sha256(f"{run_id}\0{variant_id}".encode("utf-8")).hexdigest()[:8], 16) % 10000
    return digest < rate * 100


def build_ai_batch_specs(
    database: WorkspaceDatabase,
    run_id: str,
    profile: ProviderProfile,
    *,
    batch_size: int,
    sample_rate: int,
    retry_nonce: str | None = None,
) -> list[dict[str, Any]]:
    """Create stable logical batch identities without writing persistence."""

    if not 8 <= batch_size <= 16 or not 0 <= sample_rate <= 100:
        raise ValueError("invalid R3 batch configuration")
    selected: list[str] = []
    for row in database.fetchall("SELECT variant_id,record_json FROM variants WHERE status='selected' ORDER BY variant_id"):
        variant_id = str(row["variant_id"])
        record = json.loads(row["record_json"] or "{}")
        if record.get("candidate_qualification") not in {"eligible", "conditional"}:
            continue
        selected.append(variant_id)
    specs: list[dict[str, Any]] = []
    for index in range(0, len(selected), batch_size):
        variant_ids = selected[index : index + batch_size]
        signature, payload = _batch_input_signature(database, variant_ids, profile, run_id=run_id, retry_nonce=retry_nonce)
        logical_key = f"ai_batch_{index // batch_size:04d}" if retry_nonce is None else f"ai_batch_retry_{index // batch_size:04d}_{retry_nonce[7:15]}"
        specs.append({"logical_key": logical_key, "input_signature": signature, "tile_ids": [item["tile_id"] for item in payload["tile_map"]], "variant_ids": variant_ids, "payload_signature": signature})
    return specs


__all__ = ["StaleMarker", "StageFailure", "WorkerService", "build_ai_batch_specs"]
