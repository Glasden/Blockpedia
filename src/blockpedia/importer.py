"""R1 handoff checking and exporter-to-workspace projection."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .features import build_visual_variant_record
from .directory_chooser import DirectoryChooser, DirectoryRefNotFound, DirectoryRefStale, DirectoryPathUnsafe
from .paths import DataRoot, EXPORT_ID_RE, ExportPathError, safe_relative_posix_ref
from .schema import RecordSchemaError, validate_record
from .stages import STUDIO_STAGES
from .storage import WorkspaceDatabase, utc_now


class ImportErrorBase(RuntimeError):
    """Stable service error for import operations."""


class ImportCheckNotFound(ImportErrorBase):
    pass


class ImportNotAllowed(ImportErrorBase):
    pass


class ImportCheckInProgress(ImportErrorBase):
    code = "IMPORT_CHECK_IN_PROGRESS"


class ImportCheckProgressPersistFailed(ImportErrorBase):
    code = "IMPORT_CHECK_PROGRESS_PERSIST_FAILED"


@dataclass(frozen=True, slots=True)
class ImportCheck:
    check_id: str
    minecraft_version: str
    export_id: str
    source_directory_ref: str
    manifest_sha256: str | None
    checksum_sha256: str | None
    snapshot_ref: str
    snapshot_root_sha256: str | None
    metadata_sha256: str | None
    expected_files: tuple[dict[str, str], ...]
    status: str
    issues: tuple[dict[str, str], ...]
    can_import: bool
    phase: str = "FINALIZE"
    progress: dict[str, Any] | None = None
    error_code: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    progress_subphase: str | None = None
    workspace: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "minecraft_version": self.minecraft_version,
            "export_id": self.export_id,
            "source_directory_ref": self.source_directory_ref,
            "manifest_sha256": self.manifest_sha256,
            "checksum_sha256": self.checksum_sha256,
            "snapshot_ref": self.snapshot_ref,
            "snapshot_root_sha256": self.snapshot_root_sha256,
            "metadata_sha256": self.metadata_sha256,
            "expected_files": [dict(item) for item in self.expected_files],
            "status": self.status,
            "issues": [dict(issue) for issue in self.issues],
            "can_import": self.can_import,
            "phase": self.phase,
            "progress": dict(self.progress or {}),
            "error_code": self.error_code,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "progress_subphase": self.progress_subphase,
            "workspace": dict(self.workspace or {"status": "absent", "import_id": None, "run_id": None, "error_code": None}),
        }


@dataclass(frozen=True, slots=True)
class ImportCheckStart:
    check: ImportCheck
    reused: bool
    response_status: int

    @property
    def check_id(self) -> str:
        return self.check.check_id

    @property
    def status(self) -> str:
        return self.check.status

    @property
    def can_import(self) -> bool:
        return self.check.can_import

    def to_dict(self) -> dict[str, Any]:
        return self.check.to_dict()


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _unsafe_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        value = path.lstat()
        return bool(stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def _unsafe_directory_entry(path: Path) -> bool:
    return _unsafe_reparse(path) or not path.is_dir()


def _unsafe_file_entry(path: Path) -> bool:
    return _unsafe_reparse(path) or not path.is_file()


def _snapshot_root_sha256(export_id: str, expected_files: Sequence[Mapping[str, str]], checksum_sha256: str) -> str:
    payload = {
        "export_id": export_id,
        "checksum_sha256": checksum_sha256,
        "files": [dict(item) for item in expected_files],
    }
    return "sha256:" + hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _snapshot_export(
    source: Path,
    destination: Path | None = None,
    *,
    on_progress: Any | None = None,
) -> tuple[tuple[dict[str, str], ...], str, str]:
    """Inventory and hash one export without following links.

    This is separate from the R1 validator: it creates the immutable handoff
    expectation used by the later copy pass, so import never trusts a stale
    check merely because the directory name is unchanged.
    """

    expected: dict[str, str] = {}
    checksum_digest: str | None = None
    completed = 0
    bytes_completed = 0
    if destination is not None:
        if destination.exists():
            if _unsafe_directory_entry(destination):
                raise ImportNotAllowed("snapshot destination is invalid")
        else:
            destination.mkdir(parents=True, exist_ok=False)
    for root, directories, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        accepted_directories: list[str] = []
        for directory in sorted(directories):
            directory_path = root_path / directory
            if _unsafe_directory_entry(directory_path):
                raise ImportNotAllowed("export contains a symlinked or invalid directory")
            accepted_directories.append(directory)
        directories[:] = accepted_directories
        for filename in sorted(filenames):
            file_path = root_path / filename
            relative = file_path.relative_to(source).as_posix()
            safe_relative_posix_ref(relative)
            if _unsafe_file_entry(file_path):
                raise ImportNotAllowed("export contains a symlinked or invalid file")
            if file_path.stat().st_nlink != 1:
                raise ImportNotAllowed("export contains a hardlink")
            data = file_path.read_bytes()
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            completed += 1
            bytes_completed += len(data)
            if relative == "checksums.sha256":
                checksum_digest = digest
            else:
                expected[relative] = digest
            if destination is not None:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() and (_unsafe_file_entry(target) or target.stat().st_nlink != 1):
                    raise ImportNotAllowed("snapshot destination contains a link or invalid file")
                target.write_bytes(data)
            if on_progress is not None:
                on_progress(completed, 0, "files", bytes_completed)
    if checksum_digest is None:
        raise ImportNotAllowed("checksums.sha256 is missing")
    expected_files = tuple({"relative_ref": ref, "sha256": expected[ref]} for ref in sorted(expected))
    return expected_files, checksum_digest, _snapshot_root_sha256(source.name, expected_files, checksum_digest)


def _copy_verified_snapshot(
    source: Path,
    destination: Path,
    expected_files: Sequence[Mapping[str, str]],
    expected_checksum_sha256: str,
) -> None:
    """Copy and hash source files once, rejecting every TOCTOU difference."""

    expected = {str(item["relative_ref"]): str(item["sha256"]) for item in expected_files}
    seen: set[str] = set()
    checksum_seen = False
    destination.mkdir(parents=True, exist_ok=True)
    for root, directories, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        accepted_directories: list[str] = []
        for directory in sorted(directories):
            directory_path = root_path / directory
            if _unsafe_directory_entry(directory_path):
                raise ImportNotAllowed("export changed: symlinked or invalid directory")
            accepted_directories.append(directory)
        directories[:] = accepted_directories
        for filename in sorted(filenames):
            file_path = root_path / filename
            relative = file_path.relative_to(source).as_posix()
            safe_relative_posix_ref(relative)
            if _unsafe_file_entry(file_path) or file_path.stat().st_nlink != 1:
                raise ImportNotAllowed("export changed: symlink or hardlink detected")
            data = file_path.read_bytes()
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if relative == "checksums.sha256":
                checksum_seen = True
                if digest != expected_checksum_sha256:
                    raise ImportNotAllowed("checksums.sha256 changed after check")
            else:
                if relative not in expected:
                    raise ImportNotAllowed("export changed: extra file")
                if digest != expected[relative]:
                    raise ImportNotAllowed("export changed: file hash mismatch")
                seen.add(relative)
            target_relative = (Path("export") / relative) if "/" not in relative else Path(relative)
            target = destination / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    if not checksum_seen or seen != set(expected):
        raise ImportNotAllowed("export changed: missing expected file")


def copy_to_workspace(
    source: Path,
    workspace_dir: Path,
    *,
    expected_files: Sequence[Mapping[str, str]] | None = None,
    checksum_sha256: str | None = None,
) -> None:
    """Copy a checked exporter snapshot; no projection occurs here."""

    if expected_files is None or checksum_sha256 is None:
        expected_files, checksum_sha256, _ = _snapshot_export(source)
    _copy_verified_snapshot(source, workspace_dir, expected_files, checksum_sha256)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    return records


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _safe_issue(value: Any, source: Path) -> Any:
    """Remove validator location fields and redact absolute path fragments."""

    if isinstance(value, dict):
        return {str(key): _safe_issue(item, source) for key, item in value.items() if key not in {"repo_root", "export_dir"}}
    if isinstance(value, list):
        return [_safe_issue(item, source) for item in value]
    if isinstance(value, str):
        candidates = {str(source), str(source.absolute()), source.as_posix()}
        result = value
        for candidate in sorted(candidates, key=len, reverse=True):
            result = result.replace(candidate, "<source>")
        if re.search(r"^[A-Za-z]:[\\/]", result) or result.startswith("/"):
            return "<redacted>"
        return result
    return value


def _safe_report(report: Mapping[str, Any], source: Path) -> tuple[dict[str, str], ...]:
    del source
    issues = report.get("issues", [])
    safe: list[dict[str, str]] = []
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, Mapping):
                continue
            code = issue.get("code")
            if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", code):
                safe.append({"code": code})
    return tuple(safe)


def _safe_check_id(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"check_[0-9a-f]{32}", value))


def _raw_anchor_hashes(source: Path) -> dict[str, str | None]:
    anchors: dict[str, str | None] = {}
    for name in ("manifest.json", "checksums.sha256"):
        path = source / name
        try:
            if _unsafe_file_entry(path) or path.stat().st_nlink != 1:
                anchors[name] = None
            else:
                anchors[name] = _sha256(path)
        except OSError:
            anchors[name] = None
    return anchors


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    payload = (_json(value) + "\n").encode("utf-8")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    for attempt in range(5):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02 * (attempt + 1))


def _safe_check_summary(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    check_id = payload.get("check_id")
    version = payload.get("minecraft_version")
    export_id = payload.get("export_id")
    if not _safe_check_id(check_id) or not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?", version):
        return None
    if not isinstance(export_id, str) or not EXPORT_ID_RE.fullmatch(export_id):
        return None
    raw_status = payload.get("status")
    status = raw_status if isinstance(raw_status, str) and raw_status in {"pending", "running", "passed", "failed"} else "failed"
    raw_phase = payload.get("phase")
    phase = raw_phase if isinstance(raw_phase, str) and raw_phase in {"QUEUED", "SNAPSHOT_EXPORT", "VALIDATE_EXPORT", "FINALIZE"} else "FINALIZE"
    raw_progress_value = payload.get("progress")
    raw_progress: Mapping[str, Any] = raw_progress_value if isinstance(raw_progress_value, Mapping) else {}
    try:
        completed = max(0, int(raw_progress.get("completed", 0)))
    except (TypeError, ValueError, OverflowError):
        completed = 0
    try:
        total = max(0, int(raw_progress.get("total", 0)))
    except (TypeError, ValueError, OverflowError):
        total = 0
    if total:
        completed = min(completed, total)
    raw_unit = raw_progress.get("unit")
    unit = raw_unit if isinstance(raw_unit, str) else "items"
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", unit):
        unit = "items"
    subphase = payload.get("progress_subphase") or raw_progress.get("subphase")
    if not isinstance(subphase, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", subphase):
        subphase = None
    error_code = payload.get("error_code")
    if not isinstance(error_code, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", error_code):
        error_code = None
    workspace_value = payload.get("workspace")
    workspace: Mapping[str, Any] = workspace_value if isinstance(workspace_value, Mapping) else {}
    raw_workspace_status = workspace.get("status")
    workspace_status = raw_workspace_status if isinstance(raw_workspace_status, str) and raw_workspace_status in {"absent", "creating", "created", "failed"} else "absent"
    raw_workspace_error = workspace.get("error_code")
    workspace_error = raw_workspace_error if isinstance(raw_workspace_error, str) else None
    workspace_result = {
        "status": workspace_status,
        "import_id": _safe_check_identifier(workspace.get("import_id")),
        "run_id": _safe_check_identifier(workspace.get("run_id")),
        "error_code": workspace_error if workspace_error is not None and re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", workspace_error) else None,
    }
    created_at = _safe_check_timestamp(payload.get("created_at"))
    updated_at = _safe_check_timestamp(payload.get("updated_at")) or created_at
    return {
        "check_id": check_id,
        "minecraft_version": version,
        "export_id": export_id,
        "status": status,
        "phase": phase,
        "subphase": subphase,
        "progress": {"completed": completed, "total": total, "unit": unit},
        "error_code": error_code,
        "created_at": created_at,
        "updated_at": updated_at,
        "workspace": workspace_result,
        "can_import": status == "passed",
        "check_url": f"/imports/checks/{check_id}",
    }


def _safe_check_identifier(value: Any) -> str | None:
    text = str(value) if value is not None else ""
    return text if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", text) else None


def _safe_check_timestamp(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    return text if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", text) else ""


def _safe_progress_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _check_sort_key(summary: Mapping[str, Any]) -> tuple[int, str, str, str]:
    active = 1 if summary.get("status") in {"pending", "running"} else 0
    return active, str(summary.get("updated_at", "")), str(summary.get("created_at", "")), str(summary.get("check_id", ""))


class ImportService:
    """Persistent asynchronous export checks and snapshot handoff."""

    CHECK_PHASES = ("QUEUED", "SNAPSHOT_EXPORT", "VALIDATE_EXPORT", "FINALIZE")

    def __init__(
        self,
        data_root: DataRoot,
        *,
        repo_root: Path | None = None,
        force_normalized_like: bool = False,
        chooser: DirectoryChooser | None = None,
    ):
        self.data_root = data_root
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.force_normalized_like = force_normalized_like
        self.chooser = chooser or DirectoryChooser(data_root)
        self._checks: dict[str, ImportCheck] = {}
        self._checks_lock = threading.RLock()
        self._active_checks: dict[tuple[str, str], str] = {}
        self._last_progress_write: dict[str, float] = {}
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blockpedia-import-check")
        self._closed = False
        self._mark_interrupted_checks()
        self._reconcile_creating_states()

    @property
    def executor(self) -> ThreadPoolExecutor:
        return self._executor

    def close(self) -> None:
        with self._checks_lock:
            if self._closed:
                return
            self._closed = True
        self._executor.shutdown(wait=True)

    def start_check(self, source_ref: str, minecraft_version: str) -> ImportCheckStart:
        """Reuse an active/unchanged passed check, or queue one new check."""

        if self._closed:
            raise ImportCheckProgressPersistFailed
        source = self.chooser.consume(source_ref, minecraft_version)
        export_id = source.name
        anchors = _raw_anchor_hashes(source)
        key = (minecraft_version, export_id)
        with self._checks_lock:
            active_id = self._active_checks.get(key)
            if active_id is not None:
                active = self._load_check_cache(active_id)
                if active.status in {"pending", "running"}:
                    return ImportCheckStart(active, reused=True, response_status=202)
                self._active_checks.pop(key, None)

            latest = self._latest_check_locked(minecraft_version, export_id)
            if latest is not None and latest.status in {"pending", "running"}:
                self._active_checks[key] = latest.check_id
                return ImportCheckStart(latest, reused=True, response_status=202)
            if (
                latest is not None
                and latest.status == "passed"
                and latest.manifest_sha256 is not None
                and latest.checksum_sha256 is not None
                and anchors.get("manifest.json") == latest.manifest_sha256
                and anchors.get("checksums.sha256") == latest.checksum_sha256
            ):
                return ImportCheckStart(latest, reused=True, response_status=200)

            check_id = _id("check")
            now = utc_now()
            pending = ImportCheck(
                check_id=check_id,
                minecraft_version=minecraft_version,
                export_id=export_id,
                source_directory_ref=source_ref,
                manifest_sha256=None,
                checksum_sha256=None,
                snapshot_ref=f"cache/import-checks/{check_id}/snapshot/{export_id}",
                snapshot_root_sha256=None,
                metadata_sha256=None,
                expected_files=(),
                status="pending",
                issues=(),
                can_import=False,
                phase="QUEUED",
                progress={"completed": 0, "total": 1, "unit": "check"},
                created_at=now,
                updated_at=now,
                progress_subphase=None,
                workspace={"status": "absent", "import_id": None, "run_id": None, "error_code": None},
            )
            self._store_result(pending)
            self._active_checks[key] = check_id
            self._write_state(pending)
            # Only the closure resolves the canonical absolute source Path.
            # No source path is retained in the future, check map, or cache.
            self._executor.submit(self._run_check, check_id, source_ref, minecraft_version)
            return ImportCheckStart(pending, reused=False, response_status=202)

    def check_import(self, source_directory: str | Path, minecraft_version: str) -> ImportCheck:
        """Compatibility service entry point; HTTP always uses ``start_check``.

        Existing service-level callers may still provide a Path.  It is turned
        into a process-local chooser ref and executed synchronously, while a
        string is treated as the new opaque ref contract.
        """

        if isinstance(source_directory, Path):
            source_ref = self.chooser.register_path(source_directory, minecraft_version)
            result = self.start_check(source_ref, minecraft_version)
            return self.wait_for_check(result.check_id)
        return self.start_check(str(source_directory), minecraft_version).check

    def wait_for_check(self, check_id: str, *, timeout: float | None = None) -> ImportCheck:
        # Futures are intentionally not retained: state.json is the sole
        # progress truth.  Polling here is only a compatibility convenience.
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            result = self.get_check(check_id)
            if result.status in {"passed", "failed"}:
                return result
            if deadline is not None and time.monotonic() >= deadline:
                return result
            time.sleep(0.01)

    def get_check(self, check_id: str) -> ImportCheck:
        if not _safe_check_id(check_id):
            raise ImportCheckNotFound(check_id)
        try:
            result = self._load_check_cache(check_id)
        except ImportCheckNotFound:
            with self._checks_lock:
                result = self._checks.get(check_id)
            if result is None:
                raise
        return result

    def list_checks(self, minecraft_version: str | None = None, *, limit: int = 20) -> list[dict[str, Any]]:
        """Scan the authoritative state files; never create a catalog index."""

        try:
            limit_value = max(1, min(100, int(limit)))
        except (TypeError, ValueError, OverflowError):
            limit_value = 20
        summaries: list[dict[str, Any]] = []
        for payload in self._scan_check_states():
            if minecraft_version is not None and payload.get("minecraft_version") != minecraft_version:
                continue
            summary = _safe_check_summary(payload)
            if summary is not None:
                summaries.append(summary)
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for summary in summaries:
            key = (summary["minecraft_version"], summary["export_id"])
            current = latest.get(key)
            if current is None or _check_sort_key(summary) > _check_sort_key(current):
                latest[key] = summary
        return sorted(latest.values(), key=_check_sort_key, reverse=True)[:limit_value]

    def _scan_check_states(self):
        checks_root = self.data_root.cache / "import-checks"
        try:
            if not checks_root.is_dir() or _unsafe_reparse(checks_root):
                return
            entries = sorted(checks_root.iterdir(), key=lambda item: item.name)
        except OSError:
            return
        for check_dir in entries:
            if not _safe_check_id(check_dir.name) or _unsafe_reparse(check_dir) or not check_dir.is_dir():
                continue
            state_path = check_dir / "state.json"
            try:
                if _unsafe_file_entry(state_path) or state_path.stat().st_nlink != 1:
                    continue
                payload = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(payload, Mapping) and payload.get("check_id") == check_dir.name:
                yield dict(payload)

    def _latest_check_locked(self, minecraft_version: str, export_id: str) -> ImportCheck | None:
        candidates = [
            summary
            for summary in self.list_checks(minecraft_version, limit=100)
            if summary.get("export_id") == export_id
        ]
        if not candidates:
            return None
        try:
            return self._load_check_cache(str(candidates[0]["check_id"]))
        except ImportCheckNotFound:
            return None

    def _reconcile_creating_states(self) -> None:
        with self._checks_lock:
            for summary in self._scan_check_states():
                workspace = summary.get("workspace")
                if not isinstance(workspace, Mapping) or workspace.get("status") != "creating":
                    continue
                try:
                    check = self._load_check_cache(str(summary["check_id"]))
                except ImportCheckNotFound:
                    continue
                if self._workspace_matches(check):
                    self._set_workspace(check, "created", None)
                else:
                    self._set_workspace(check, "failed", "IMPORT_WORKSPACE_RECONCILE_FAILED")

    def _workspace_matches(self, result: ImportCheck) -> bool:
        workspace = result.workspace or {}
        run_id = workspace.get("run_id")
        import_id = workspace.get("import_id")
        if not isinstance(run_id, str) or not isinstance(import_id, str) or not run_id or not import_id:
            return False
        try:
            path = self.data_root.workspace_dir(result.minecraft_version, run_id) / "work.sqlite3"
            if _unsafe_file_entry(path) or path.stat().st_nlink != 1:
                return False
            with WorkspaceDatabase.open(path, read_only=True) as database:
                row = database.fetchone(
                    "SELECT runs.run_id,runs.import_id,runs.minecraft_version,imports.export_id,imports.manifest_sha256,imports.checksum_sha256 "
                    "FROM runs JOIN imports ON imports.import_id=runs.import_id WHERE runs.run_id=? AND runs.import_id=?",
                    (run_id, import_id),
                )
                return bool(
                    row
                    and row["minecraft_version"] == result.minecraft_version
                    and row["export_id"] == result.export_id
                    and row["manifest_sha256"] == result.manifest_sha256
                    and row["checksum_sha256"] == result.checksum_sha256
                )
        except (OSError, KeyError, ValueError, sqlite3.Error):
            return False

    def _discover_existing_workspace(self, result: ImportCheck) -> tuple[str, str] | None:
        parent = self.data_root.workspace / result.minecraft_version
        if not parent.is_dir() or _unsafe_reparse(parent):
            return None
        matches: list[tuple[str, str]] = []
        try:
            directories = sorted(parent.iterdir(), key=lambda item: item.name)
        except OSError:
            return None
        for run_dir in directories:
            try:
                safe_run_id = safe_relative_posix_ref(run_dir.name)
            except (ExportPathError, ValueError):
                continue
            if _unsafe_reparse(run_dir) or not run_dir.is_dir() or safe_run_id != run_dir.name:
                continue
            path = run_dir / "work.sqlite3"
            if _unsafe_file_entry(path) or path.stat().st_nlink != 1:
                continue
            try:
                with WorkspaceDatabase.open(path, read_only=True) as database:
                    row = database.fetchone(
                        "SELECT runs.run_id,runs.import_id,runs.minecraft_version,imports.export_id,imports.manifest_sha256,imports.checksum_sha256 "
                        "FROM runs JOIN imports ON imports.import_id=runs.import_id WHERE runs.run_id=?",
                        (run_dir.name,),
                    )
                    if row and row["minecraft_version"] == result.minecraft_version and row["export_id"] == result.export_id and row["manifest_sha256"] == result.manifest_sha256 and row["checksum_sha256"] == result.checksum_sha256:
                        matches.append((str(row["import_id"]), str(row["run_id"])))
            except (OSError, ValueError, sqlite3.Error):
                continue
        return matches[0] if len(matches) == 1 else None

    def _set_workspace(self, result: ImportCheck, status: str, error_code: str | None, *, import_id: str | None = None, run_id: str | None = None) -> ImportCheck:
        association = dict(result.workspace or {})
        association.update(
            {
                "status": status,
                "import_id": import_id if import_id is not None else association.get("import_id"),
                "run_id": run_id if run_id is not None else association.get("run_id"),
                "error_code": error_code,
            }
        )
        updated = ImportCheck(**{**result.to_dict(), "workspace": association, "updated_at": utc_now()})
        self._store_result(updated)
        self._write_state(updated, force=True)
        return updated

    def import_checked(self, check_id: str, *, copy_mode: str = "copy_to_workspace") -> dict[str, Any]:
        if copy_mode != "copy_to_workspace":
            raise ImportNotAllowed("copy_mode must be copy_to_workspace")
        result = self.get_check(check_id)
        if result.status == "pending" or result.status == "running":
            raise ImportCheckInProgress(check_id)
        if not result.can_import:
            raise ImportNotAllowed("import check did not pass")
        # A live process still knows the chooser ref and must revalidate it on
        # use.  A process-restarted, already-passed check uses only its frozen
        # snapshot and never attempts to recover a source path.
        with self._checks_lock:
            in_memory = self._checks.get(check_id)
        if in_memory is not None and in_memory.source_directory_ref:
            self.chooser.validate_ref(in_memory.source_directory_ref, result.minecraft_version)
        snapshot = self.data_root.resolve_ref(result.snapshot_ref)
        if snapshot.name != result.export_id or not snapshot.is_dir() or _unsafe_directory_entry(snapshot):
            raise ImportNotAllowed("checked snapshot is missing or invalid")
        metadata_path = snapshot.parent.parent / "metadata.json"
        if result.metadata_sha256 is None or not metadata_path.is_file() or _sha256(metadata_path) != result.metadata_sha256:
            raise ImportNotAllowed("checked snapshot metadata changed")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ImportNotAllowed("checked snapshot metadata is invalid") from exc
        expected_root = _snapshot_root_sha256(result.export_id, result.expected_files, result.checksum_sha256 or "")
        if metadata.get("snapshot_root_sha256") != result.snapshot_root_sha256 or result.snapshot_root_sha256 != expected_root:
            raise ImportNotAllowed("checked snapshot metadata is inconsistent")
        self._checks_lock.acquire()
        reserved = False
        staging_dir: Path | None = None
        try:
            result = self._load_check_cache(check_id)
            association = result.workspace or {}
            existing_run_id = association.get("run_id")
            existing_import_id = association.get("import_id")
            if association.get("status") == "created" and isinstance(existing_run_id, str) and isinstance(existing_import_id, str) and self._workspace_matches(result):
                return {
                    "import_id": existing_import_id,
                    "run_id": existing_run_id,
                    "minecraft_version": result.minecraft_version,
                    "status": "pending",
                    "workspace_ref": self.data_root.relative_ref(self.data_root.workspace_dir(result.minecraft_version, existing_run_id)),
                    "source_directory_ref": result.source_directory_ref,
                    "reused": True,
                }

            discovered = self._discover_existing_workspace(result)
            if discovered is not None:
                import_id, run_id = discovered
                result = self._set_workspace(result, "created", None, import_id=import_id, run_id=run_id)
                return {
                    "import_id": import_id,
                    "run_id": run_id,
                    "minecraft_version": result.minecraft_version,
                    "status": "pending",
                    "workspace_ref": self.data_root.relative_ref(self.data_root.workspace_dir(result.minecraft_version, run_id)),
                    "source_directory_ref": result.source_directory_ref,
                    "reused": True,
                }

            import_id, run_id = _id("import"), _id("run")
            result = self._set_workspace(result, "creating", None, import_id=import_id, run_id=run_id)
            reserved = True
            workspace_parent = self.data_root.workspace / result.minecraft_version
            workspace_parent.mkdir(parents=True, exist_ok=True)
            workspace_dir = self.data_root.workspace_dir(result.minecraft_version, run_id)
            staging_dir = workspace_parent / f".{run_id}.staging"
            staging_dir.mkdir(parents=True, exist_ok=False)
            _copy_verified_snapshot(snapshot, staging_dir, result.expected_files, result.checksum_sha256 or "")
            database = WorkspaceDatabase.open(staging_dir / "work.sqlite3", force_normalized_like=self.force_normalized_like)
            try:
                _project_to_workspace(
                    database,
                    staging_dir,
                    result,
                    import_id=import_id,
                    run_id=run_id,
                    repo_root=self.repo_root,
                )
            finally:
                database.close()
            staging_dir.replace(workspace_dir)
            staging_dir = None
            result = self._set_workspace(result, "created", None, import_id=import_id, run_id=run_id)
        except Exception:
            if staging_dir is not None:
                shutil.rmtree(staging_dir, ignore_errors=True)
            if reserved:
                try:
                    self._set_workspace(self._current_check(check_id), "failed", "IMPORT_WORKSPACE_CREATE_FAILED")
                except ImportCheckProgressPersistFailed:
                    pass
            raise
        finally:
            self._checks_lock.release()
        return {
            "import_id": import_id,
            "run_id": run_id,
            "minecraft_version": result.minecraft_version,
            "status": "pending",
            "workspace_ref": self.data_root.relative_ref(workspace_dir),
            "source_directory_ref": result.source_directory_ref,
        }

    def _run_check(self, check_id: str, source_ref: str, minecraft_version: str) -> None:
        check_dir = self.data_root.cache / "import-checks" / check_id
        snapshot: Path | None = None
        expected_files: tuple[dict[str, str], ...] = ()
        checksum_digest: str | None = None
        snapshot_root_sha256: str | None = None
        metadata_sha256: str | None = None
        try:
            source = self.chooser.consume(source_ref, minecraft_version)
            current = self.get_check(check_id)
            self._update_state(
                current,
                phase="SNAPSHOT_EXPORT",
                status="running",
                progress={"completed": 0, "total": 0, "unit": "files", "bytes": 0},
                progress_subphase="SNAPSHOT_COPY_HASH",
                force=True,
            )

            def on_snapshot_progress(completed: int, total: int | None, unit: str, bytes_completed: int) -> None:
                current_snapshot = self._current_check(check_id)
                self._update_state(
                    current_snapshot,
                    phase="SNAPSHOT_EXPORT",
                    status="running",
                    progress={
                        "completed": max(0, int(completed)),
                        "total": max(0, int(total or 0)),
                        "unit": unit,
                        "bytes": max(0, int(bytes_completed)),
                    },
                    progress_subphase="SNAPSHOT_COPY_HASH",
                    force=True,
                )

            snapshot = check_dir / "snapshot" / source.name
            expected_files, checksum_digest, snapshot_root_sha256 = _snapshot_export(
                source, snapshot, on_progress=on_snapshot_progress
            )
            metadata = {
                "check_id": check_id,
                "minecraft_version": minecraft_version,
                "export_id": source.name,
                "snapshot_root_sha256": snapshot_root_sha256,
                "expected_files": [dict(item) for item in expected_files],
                "checksum_sha256": checksum_digest,
            }
            metadata_path = check_dir / "metadata.json"
            _write_json_atomic(metadata_path, metadata)
            metadata_sha256 = _sha256(metadata_path)
            current = self._current_check(check_id)
            self._update_state(
                current,
                phase="VALIDATE_EXPORT",
                status="running",
                progress={"completed": 0, "total": 0, "unit": "records"},
                progress_subphase="VALIDATE_EXPORT",
                force=True,
            )

            def on_progress(phase: str, completed: int, total: int | None, unit: str) -> None:
                subphase = phase if isinstance(phase, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", phase) else "VALIDATE_EXPORT"
                try:
                    completed_value = max(0, int(completed))
                except (TypeError, ValueError, OverflowError):
                    completed_value = 0
                if total is None:
                    total_value = 0
                else:
                    try:
                        total_value = max(0, int(total))
                    except (TypeError, ValueError, OverflowError):
                        total_value = 0
                if total_value:
                    completed_value = min(completed_value, total_value)
                unit_value = unit if isinstance(unit, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", unit) else "items"
                previous = self._current_check(check_id)
                if previous.progress_subphase == subphase:
                    previous_progress = previous.progress or {}
                    completed_value = max(completed_value, max(0, _safe_progress_int(previous_progress.get("completed"))))
                self._update_state(
                    self._current_check(check_id),
                    phase="VALIDATE_EXPORT",
                    status="running",
                    progress={"completed": completed_value, "total": total_value, "unit": unit_value},
                    progress_subphase=subphase,
                    force=True,
                )

            report = self._call_validator(snapshot, on_progress)
            digest_map = {item["relative_ref"]: item["sha256"] for item in expected_files}
            manifest_digest = digest_map.get("manifest.json")
            status = "passed" if str(report.get("status", "failed")) == "passed" else "failed"
            issues = _safe_report(report, snapshot)
            current = self._current_check(check_id)
            final = ImportCheck(
                check_id=check_id,
                minecraft_version=minecraft_version,
                export_id=source.name,
                source_directory_ref=source_ref,
                manifest_sha256=manifest_digest,
                checksum_sha256=checksum_digest,
                snapshot_ref=self.data_root.relative_ref(snapshot),
                snapshot_root_sha256=snapshot_root_sha256,
                metadata_sha256=metadata_sha256,
                expected_files=tuple(expected_files),
                status=status,
                issues=tuple(issues),
                can_import=status == "passed",
                phase="FINALIZE",
                progress={"completed": 1, "total": 1, "unit": "check"},
                error_code=None if status == "passed" else (issues[0].get("code") if issues else "IMPORT_INCOMPLETE"),
                created_at=current.created_at,
                updated_at=utc_now(),
                progress_subphase=None,
                workspace=current.workspace,
            )
            self._store_result(final)
            self._write_state(final, force=True)
            with self._checks_lock:
                self._active_checks.pop((minecraft_version, source.name), None)
        except DirectoryRefNotFound:
            self._finish_failed(check_id, "DIRECTORY_REF_NOT_FOUND")
        except DirectoryRefStale:
            self._finish_failed(check_id, "DIRECTORY_REF_STALE")
        except (DirectoryPathUnsafe, ExportPathError, ImportNotAllowed):
            self._finish_failed(check_id, "IMPORT_SNAPSHOT_INVALID")
        except ImportCheckProgressPersistFailed:
            self._finish_failed(check_id, "IMPORT_CHECK_PROGRESS_PERSIST_FAILED")
        except Exception:
            # The exception class is deliberately the only diagnostic retained.
            self._finish_failed(check_id, "IMPORT_CHECK_FAILED")

    def _call_validator(self, snapshot: Path, callback: Any) -> Mapping[str, Any]:
        validator_module = importlib.import_module("tools.validate_r1_export")
        report = validator_module.validate_export(self.repo_root, snapshot, on_progress=callback)
        return report if isinstance(report, Mapping) else {"status": "failed", "issues": []}

    def _finish_failed(self, check_id: str, code: str) -> None:
        try:
            current = self.get_check(check_id)
        except ImportCheckNotFound:
            return
        final = ImportCheck(
            **{
                **current.to_dict(),
                "issues": ({"code": code},),
                "status": "failed",
                "can_import": False,
                "phase": "FINALIZE",
                "progress": {"completed": 1, "total": 1, "unit": "check"},
                "error_code": code,
                "updated_at": utc_now(),
                "progress_subphase": None,
            }
        )
        self._store_result(final)
        try:
            self._write_state(final, force=True)
        except ImportCheckProgressPersistFailed:
            pass
        with self._checks_lock:
            self._active_checks.pop((final.minecraft_version, final.export_id), None)

    def _update_state(
        self,
        result: ImportCheck,
        *,
        phase: str,
        status: str,
        progress: dict[str, Any],
        progress_subphase: str | None = None,
        force: bool = False,
    ) -> None:
        if progress_subphase is not None:
            progress = {**progress, "subphase": progress_subphase}
        updated = ImportCheck(
            **{
                **result.to_dict(),
                "phase": phase,
                "status": status,
                "progress": progress,
                "progress_subphase": progress_subphase if progress_subphase is not None else result.progress_subphase,
                "updated_at": utc_now(),
            }
        )
        self._store_result(updated)
        now = time.monotonic()
        if force:
            self._write_state(updated, force=True)
            if status in {"passed", "failed"} or phase == "FINALIZE":
                self._last_progress_write[result.check_id] = now
            return
        if now - self._last_progress_write.get(result.check_id, 0.0) < 0.10:
            return
        self._last_progress_write[result.check_id] = now
        self._write_state(updated, force=force)

    def _store_result(self, result: ImportCheck) -> None:
        with self._checks_lock:
            self._checks[result.check_id] = result

    def _current_check(self, check_id: str) -> ImportCheck:
        with self._checks_lock:
            result = self._checks.get(check_id)
        if result is not None:
            return result
        return self._load_check_cache(check_id)

    def _write_state(self, result: ImportCheck, *, force: bool = False) -> None:
        del force
        payload = {
            "check_id": result.check_id,
            "minecraft_version": result.minecraft_version,
            "export_id": result.export_id,
            "snapshot_ref": result.snapshot_ref,
            "snapshot_root_sha256": result.snapshot_root_sha256,
            "metadata_sha256": result.metadata_sha256,
            "manifest_sha256": result.manifest_sha256,
            "checksum_sha256": result.checksum_sha256,
            "status": result.status,
            "phase": result.phase,
            "progress": dict(result.progress or {}),
            "issues": [dict(issue) for issue in result.issues],
            "error_code": result.error_code,
            "created_at": result.created_at,
            "updated_at": result.updated_at,
            "progress_subphase": result.progress_subphase,
            "workspace": dict(result.workspace or {"status": "absent", "import_id": None, "run_id": None, "error_code": None}),
        }
        try:
            _write_json_atomic(self.data_root.cache / "import-checks" / result.check_id / "state.json", payload)
        except OSError as exc:
            raise ImportCheckProgressPersistFailed from exc

    def _mark_interrupted_checks(self) -> None:
        checks_root = self.data_root.cache / "import-checks"
        if not checks_root.is_dir():
            return
        for check_dir in checks_root.iterdir():
            state_path = check_dir / "state.json"
            if not state_path.is_file():
                continue
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if state.get("status") not in {"pending", "running"}:
                continue
            state.update(
                {
                    "status": "failed",
                    "phase": "FINALIZE",
                    "progress": {"completed": 1, "total": 1, "unit": "check"},
                    "progress_subphase": None,
                    "updated_at": utc_now(),
                    "issues": [{"code": "IMPORT_CHECK_INTERRUPTED"}],
                    "error_code": "IMPORT_CHECK_INTERRUPTED",
                }
            )
            try:
                _write_json_atomic(state_path, state)
            except OSError:
                continue

    def _load_check_cache(self, check_id: str) -> ImportCheck:
        check_dir = self.data_root.cache / "import-checks" / check_id
        cache_path = check_dir / "state.json"
        metadata_path = check_dir / "metadata.json"
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.is_file() else {}
        except (OSError, ValueError, TypeError) as exc:
            raise ImportCheckNotFound(check_id) from exc
        if payload.get("check_id") != check_id:
            raise ImportCheckNotFound(check_id)
        expected_files = tuple(dict(item) for item in metadata.get("expected_files", []))
        with self._checks_lock:
            existing = self._checks.get(check_id)
        source_ref = existing.source_directory_ref if existing is not None else ""
        result = ImportCheck(
            check_id=check_id,
            minecraft_version=str(payload["minecraft_version"]),
            export_id=str(payload["export_id"]),
            source_directory_ref=source_ref,
            manifest_sha256=payload.get("manifest_sha256"),
            checksum_sha256=payload.get("checksum_sha256"),
            snapshot_ref=str(payload["snapshot_ref"]),
            snapshot_root_sha256=payload.get("snapshot_root_sha256"),
            metadata_sha256=payload.get("metadata_sha256"),
            expected_files=expected_files,
            status=str(payload["status"]),
            issues=tuple(dict(item) for item in payload.get("issues", [])),
            can_import=str(payload["status"]) == "passed",
            phase=str(payload.get("phase", "FINALIZE")),
            progress=dict(payload.get("progress") or {}),
            error_code=payload.get("error_code"),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
            progress_subphase=payload.get("progress_subphase") or (payload.get("progress") or {}).get("subphase"),
            workspace=dict(payload.get("workspace") or {"status": "absent", "import_id": None, "run_id": None, "error_code": None}),
        )
        with self._checks_lock:
            existing = self._checks.get(check_id)
            if existing is not None and existing.source_directory_ref:
                result = ImportCheck(**{**result.to_dict(), "source_directory_ref": existing.source_directory_ref})
            self._checks[check_id] = result
        return result


def _project_to_workspace(
    database: WorkspaceDatabase,
    source: Path,
    result: ImportCheck,
    *,
    import_id: str,
    run_id: str,
    repo_root: Path,
) -> None:
    export_records = source / "export"
    manifest = json.loads((export_records / "manifest.json").read_text(encoding="utf-8"))
    blocks = _read_jsonl(export_records / "blocks.jsonl")
    states = _read_jsonl(export_records / "states.jsonl")
    variants = _read_jsonl(export_records / "variants.jsonl")
    failures = _read_jsonl(export_records / "failures.jsonl")
    block_map = {record["block_id"]: record for record in blocks}
    variant_map = {record["variant_id"]: record for record in variants}
    failure_map = {record["failure_id"]: record for record in failures}
    _validate_projection_references(block_map, states, variant_map, failures)
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO imports(import_id,minecraft_version,export_id,source_directory_ref,manifest_sha256,checksum_sha256,expected_files_json,report_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            # Chooser refs are process-local and are never written to the
            # workspace database.  The frozen column remains populated with a
            # non-reference marker for the existing schema.
            (import_id, result.minecraft_version, result.export_id, "not-persisted", result.manifest_sha256 or "", result.checksum_sha256 or "", _json(result.expected_files), _json({"status": result.status, "issues": result.issues}), result.status, now),
        )
        connection.execute(
            "INSERT INTO runs(run_id,import_id,minecraft_version,status,current_stage,boundary_event,config_snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, import_id, result.minecraft_version, "pending", STUDIO_STAGES[0], None, "{}", now),
        )
        for ordinal, stage in enumerate(STUDIO_STAGES):
            connection.execute(
                "INSERT INTO stage_runs(run_id,stage,ordinal,status,cursor_json) VALUES (?,?,?,?,?)",
                (run_id, stage, ordinal, "pending", "{}"),
            )
        connection.execute(
            "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
            (_id("audit"), "IMPORT_CHECKED_AND_PROJECTED", run_id, _json({"import_id": import_id, "export_id": result.export_id}), now),
        )

        for record in blocks:
            block_record = _project_block(record)
            validate_record("block-record.v1", block_record, repo_root=repo_root)
            connection.execute(
                "INSERT INTO blocks(block_id,minecraft_version,record_json) VALUES (?,?,?)",
                (record["block_id"], result.minecraft_version, _json(block_record)),
            )

        for failure in failures:
            connection.execute(
                "INSERT INTO failures(failure_id,minecraft_version,block_id,state_id,variant_id,record_json) VALUES (?,?,?,?,?,?)",
                (failure["failure_id"], result.minecraft_version, failure.get("block_id"), failure.get("state_id"), failure.get("variant_id"), _json(failure)),
            )

        states_by_block: dict[str, list[Mapping[str, Any]]] = {}
        for record in states:
            block = block_map.get(record.get("block_id"))
            if block is None:
                raise ImportNotAllowed("state references an unknown block")
            _check_property_membership(block, record)
            failure_id = None
            if record["mapping_status"] == "skipped":
                failure_id = _failure_for_state(record, failures)
                if failure_id is None:
                    raise ImportNotAllowed("skipped state has no failure reference")
            state_record = _project_state(record, failure_id)
            validate_record("state-record.v1", state_record, repo_root=repo_root)
            states_by_block.setdefault(record["block_id"], []).append(record)
            connection.execute(
                "INSERT INTO states(state_id,block_id,minecraft_version,record_json,failure_id) VALUES (?,?,?,?,?)",
                (record["state_id"], record["block_id"], result.minecraft_version, _json(state_record), failure_id),
            )

        for failure in failures:
            if failure.get("scope") in {"variant", "render"} and failure.get("variant_id") in variant_map and variant_map[failure["variant_id"]].get("status") == "skipped":
                connection.execute(
                    "INSERT OR IGNORE INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_id("review"), result.minecraft_version, "variant", failure["variant_id"], failure.get("reason_code", "OTHER"), "high", "open", failure.get("message", ""), _json(failure.get("evidence", {})), now),
                )

        for record in variants:
            if record.get("status") != "selected":
                # A skipped exporter variant is represented by its machine
                # failure/review precursor, never as a visual workspace row.
                continue
            block_id = record["block_id"]
            if block_id not in block_map or record["variant_id"] != block_id:
                raise ImportNotAllowed("variant reference is inconsistent")
            render = record.get("render")
            if not isinstance(render, Mapping):
                raise ImportNotAllowed("selected variant has no render reference")
            expected_render_prefix = "renders/minecraft/" + block_id.removeprefix("minecraft:")
            expected_render_paths = (
                expected_render_prefix + "/preview.png",
                expected_render_prefix + "/mask.png",
                expected_render_prefix + "/render.json",
            )
            for key in ("preview_path", "mask_path", "render_metadata_path"):
                safe_relative_posix_ref(render[key])
                if not (source / render[key]).is_file() or (source / render[key]).is_symlink():
                    raise ImportNotAllowed("selected render reference is missing")
            if (render["preview_path"], render["mask_path"], render["render_metadata_path"]) != expected_render_paths:
                raise ImportNotAllowed("selected render reference does not match block identity")
            connection.execute(
                "INSERT INTO variants(variant_id,block_id,minecraft_version,status,source_json,record_json) VALUES (?,?,?,?,?,NULL)",
                (record["variant_id"], block_id, result.minecraft_version, "selected", _json(record)),
            )
            for ref_key, hash_key in (("preview_path", "image_sha256"), ("mask_path", "mask_sha256"), ("render_metadata_path", "render_metadata_sha256")):
                relative_ref = render[ref_key]
                artifact_hash = render[hash_key]
                connection.execute(
                    "INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)",
                    (_id("artifact"), None, "render", relative_ref, artifact_hash, _json({"variant_id": record["variant_id"], "hash_mode": "jcs" if ref_key == "render_metadata_path" else "bytes"})),
                )
        connection.execute(
            "INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)",
            (_id("artifact"), None, "source_export", "export/manifest.json", result.manifest_sha256 or "", _json({"export_id": result.export_id})),
        )


def _project_block(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "block-record.v1",
        "export_id": record["export_id"],
        "minecraft_version": record["minecraft_version"],
        "block_id": record["block_id"],
        "translation_key": record["translation_key"],
        "official_names": {"zh_cn": record["name_zh_cn"], "en_us": record["name_en_us"]},
        "default_state_id": record["default_state_id"],
        "properties": record["properties"],
        "tags": record["tags"],
        "machine_facts": {"has_item": record["has_item"], "has_block_entity": record["has_block_entity"]},
        "source": record["source"],
    }


def _project_state(record: Mapping[str, Any], failure_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": "state-record.v1",
        "export_id": record["export_id"],
        "minecraft_version": record["minecraft_version"],
        "state_id": record["state_id"],
        "block_id": record["block_id"],
        "properties": record["properties"],
        "is_default": record["is_default"],
        "legal_state": record["legal_state"],
        "shape": record["shape"],
        "collision": record["collision"],
        "behavior": record["behavior"],
        "variant_ids": record["variant_ids"],
        "mapping_status": record["mapping_status"],
        "failure_id": failure_id,
        "source": record["source"],
    }


def _check_property_membership(block: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    legal = block.get("properties", {})
    properties = state.get("properties", {})
    if set(properties) != set(legal):
        raise ImportNotAllowed("state properties do not match block properties")
    for name, value in properties.items():
        if value not in legal.get(name, []):
            raise ImportNotAllowed("state property value is outside block legal set")


def _failure_for_state(state: Mapping[str, Any], failures: Sequence[Mapping[str, Any]]) -> str | None:
    for failure in failures:
        if failure.get("scope") == "state" and failure.get("state_id") == state.get("state_id"):
            return str(failure["failure_id"])
    for failure in failures:
        if failure.get("block_id") == state.get("block_id") and failure.get("scope") in {"block", "variant", "render"}:
            return str(failure["failure_id"])
    return None


def _validate_projection_references(
    blocks: Mapping[str, Mapping[str, Any]],
    states: Sequence[Mapping[str, Any]],
    variants: Mapping[str, Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
) -> None:
    states_by_block: dict[str, set[str]] = {}
    for state in states:
        block_id = state.get("block_id")
        if block_id not in blocks:
            raise ImportNotAllowed("state references an unknown block")
        states_by_block.setdefault(str(block_id), set()).add(str(state["state_id"]))
        references = state.get("variant_ids", [])
        if state.get("mapping_status") == "mapped":
            if not references:
                raise ImportNotAllowed("mapped state has no variant reference")
            for variant_id in references:
                variant = variants.get(variant_id)
                if variant is None or variant.get("status") != "selected" or variant.get("block_id") != block_id:
                    raise ImportNotAllowed("state variant reference is not a selected same-block variant")
        elif references:
            raise ImportNotAllowed("skipped state has variant references")
    for variant_id, variant in variants.items():
        block_id = variant.get("block_id")
        if block_id not in blocks or variant_id != block_id:
            raise ImportNotAllowed("variant reference is inconsistent")
        if variant.get("status") == "selected":
            represented = set(variant.get("represented_state_ids", []))
            if represented != states_by_block.get(str(block_id), set()):
                raise ImportNotAllowed("selected variant state projection is incomplete")
            block_default = blocks[str(block_id)].get("default_state_id")
            if variant.get("canonical_state_id") != block_default or block_default not in represented:
                raise ImportNotAllowed("selected variant canonical state is not the block default")
        elif not any(failure.get("variant_id") == variant_id for failure in failures):
            raise ImportNotAllowed("skipped variant has no machine failure")
    for failure in failures:
        scope = failure.get("scope")
        block_id = failure.get("block_id")
        if scope in {"block", "state", "variant", "render"} and block_id not in blocks:
            raise ImportNotAllowed("failure block reference is invalid")
        if scope == "state" and (failure.get("state_id") not in {state.get("state_id") for state in states}):
            raise ImportNotAllowed("failure state reference is invalid")
        if scope in {"variant", "render"}:
            variant_id = failure.get("variant_id")
            variant = variants.get(variant_id) if isinstance(variant_id, str) else None
            if variant is None or variant.get("block_id") != block_id:
                raise ImportNotAllowed("failure variant reference is invalid")
