"""R3 Phase C candidate check/build implementation.

This module deliberately owns only the synchronous candidate boundary.  It
does not know about HTTP, activation, the current pointer, or MCP.  The
workspace remains the source of truth until a fully checked staging directory
has been committed as one immutable candidate.
"""

from __future__ import annotations

import ctypes
import platform
import hashlib
import importlib.resources
import json
import os
import re
import sqlite3
import stat
import struct
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .features import decode_rgba_png
from .paths import (
    DataRoot,
    RELEASE_BUILD_ID_RE,
    RELEASE_CHECK_ID_RE,
    RELEASE_ID_RE,
    safe_relative_posix_ref,
    validate_minecraft_version,
)
from .schema import RecordSchemaError, validate_record
from .search import SEMANTIC_LIST_FIELDS, SEMANTIC_SCALAR_FIELDS, WorkspaceQueryService, human_semantics_complete, normalize_text
from .storage import DatabaseSchemaMismatch, WorkspaceDatabase, packaged_release_index_schema
from .provider import build_cache_key
from .r3 import canonical_json, is_sensitive_review_text, make_contact_sheet, safe_machine_metadata, safe_prompt, sha256_bytes, sha256_json


CHECK_CODES = (
    "REGISTRY_COVERAGE_100",
    "BLOCK_VARIANT_OR_AUDITED_SKIP",
    "EXCLUDED_QUALIFICATION_REVIEW_VALID",
    "IMAGE_READABLE_AND_HASHED",
    "LEGAL_STATE_VALID",
    "MACHINE_SCHEMA_VALID",
    "AI_SCHEMA_VALID",
    "OVERRIDE_REFERENCES_VALID",
    "NO_FALSE_IDS",
    "HIGH_REVIEW_ZERO",
    "FTS_READY",
    "RELEASE_HASH_MANIFEST",
)
_CHECK_CODES_SET = frozenset(CHECK_CODES)
_CHECK_STATE_FIELDS = {
    "format_version",
    "check_id",
    "release_build_id",
    "run_id",
    "minecraft_version",
    "source_export_id",
    "status",
    "can_build",
    "snapshot_fingerprint",
    "quality_report_sha256",
    "release_id",
    "created_at",
    "updated_at",
    "error_code",
}
_CHECK_REPORT_FIELDS = {
    "format_version",
    "report_kind",
    "check_id",
    "release_build_id",
    "run_id",
    "minecraft_version",
    "status",
    "can_build",
    "snapshot_fingerprint",
    "items",
    "created_at",
    "updated_at",
}
_RELEASE_REPORT_FIELDS = {
    "format_version",
    "report_kind",
    "release_id",
    "release_build_id",
    "run_id",
    "minecraft_version",
    "status",
    "snapshot_fingerprint",
    "items",
    "built_at",
}
_MANUAL_PACKAGE_FIELDS = {
    "format_version",
    "release_id",
    "version",
    "manual_overrides",
    "skip_reviews",
    "qualification_reviews",
}
_PHASE_C_SCHEMA_IDS = (
    "export-manifest.v1",
    "export-block.v1",
    "export-state.v1",
    "export-variant.v1",
    "export-failure.v1",
    "render-metadata.v1",
    "block-record.v1",
    "state-record.v1",
    "visual-variant-record.v1",
    "annotation-record.v1",
    "manual-override.v1",
    "skip-review.v1",
    "qualification-review.v1",
    "release-manifest.v1",
    "release.v1",
)
_PROVIDER_SCHEMA_IDS = (
    "provider-batch-envelope.v1",
    "annotation-batch-output.v1",
    "annotation-wire-item.v1",
)
_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$")
_BLOCK_ID_RE = re.compile(r"^minecraft:[a-z0-9_./-]+$")
_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_UNPREFIXED_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_REPARSE_POINT = 0x400
_OPENAI_ADAPTERS = frozenset({"openai_responses", "openai_chat_completions"})
_BANNER_REFRESH_FORMAT = "banner-refresh.v1"
_BANNER_REFRESH_POLICY_TOKEN = (
    "banner-camera.v2;namespace=minecraft;types=BannerBlock,WallBannerBlock;"
    "colors=black,blue,brown,cyan,gray,green,light_blue,light_gray,lime,"
    "magenta,orange,pink,purple,red,white,yellow;forms=banner,wall_banner"
)
_BANNER_REFRESH_TARGETS = tuple(
    sorted(
        {
            f"minecraft:{color}_{form}"
            for color in (
                "black",
                "blue",
                "brown",
                "cyan",
                "gray",
                "green",
                "light_blue",
                "light_gray",
                "lime",
                "magenta",
                "orange",
                "pink",
                "purple",
                "red",
                "white",
                "yellow",
            )
            for form in ("banner", "wall_banner")
        },
        key=lambda value: value.encode("utf-8"),
    )
)
_BANNER_REFRESH_PROVENANCE_FIELDS = {
    "format",
    "base",
    "new",
    "check_id",
    "target_ids",
    "policy_token",
}
_BANNER_REFRESH_MARKER_KEYS = _BANNER_REFRESH_PROVENANCE_FIELDS | {
    "version",
    "format_version",
    "provenance",
}
_BANNER_REFRESH_LINEAGE_FIELDS = {
    "import_id",
    "export_id",
    "manifest_sha256",
    "checksum_sha256",
}


class ReleaseBuildFailure(RuntimeError):
    """Internal stable failure which is mapped to the existing R3Error."""

    def __init__(self, code: str, *, after_commit: bool = False):
        self.code = code
        self.after_commit = after_commit
        super().__init__(code)


class ReleaseCheckNotFound(ReleaseBuildFailure):
    def __init__(self) -> None:
        super().__init__("RELEASE_CHECK_NOT_FOUND")


@dataclass(frozen=True, slots=True)
class CheckState:
    value: dict[str, Any]
    directory: Path
    report: dict[str, Any]


@dataclass(slots=True)
class Snapshot:
    run: dict[str, Any]
    import_row: dict[str, Any]
    banner_refresh_provenance: dict[str, Any] | None
    banner_refresh_error: str | None
    config: dict[str, Any]
    manifest: dict[str, Any]
    blocks: list[dict[str, Any]]
    states: list[dict[str, Any]]
    variants: list[dict[str, Any]]
    variant_sources: list[dict[str, Any]]
    features: list[dict[str, Any]]
    failures: list[dict[str, Any]]
    annotations: list[dict[str, Any]]
    overrides: list[dict[str, Any]]
    reviews: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    provider_requests: list[dict[str, Any]]
    ai_jobs: list[dict[str, Any]]
    source_records: dict[str, list[dict[str, Any]]]
    source_file_hashes: dict[str, str]
    export_checksum_inventory: list[dict[str, str]]
    export_checksum_errors: tuple[str, ...]
    schema_ids: tuple[str, ...]
    schema_inventory: list[dict[str, str]]
    release_index_sql_sha256: str
    fingerprint: str
    artifact_errors: tuple[str, ...]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _token(prefix: str) -> str:
    import secrets

    return prefix + secrets.token_hex(16)


def _release_id_for_build_id(release_build_id: str) -> str:
    if RELEASE_BUILD_ID_RE.fullmatch(release_build_id) is None:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
    return "rel_" + release_build_id.removeprefix("build_")


def _canonical_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE_POINT)


def _identity(st: os.stat_result) -> tuple[int, int, int, int, int]:
    return (int(st.st_dev), int(st.st_ino), int(st.st_size), int(st.st_mtime_ns), int(st.st_nlink))


def _lstat(path: Path, *, directory: bool | None = None) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
    if directory is True and not stat.S_ISDIR(value.st_mode):
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
    if directory is False and (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1):
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
    return value


def _safe_components(path: Path, root: Path) -> None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc
    current = root.absolute()
    _lstat(current, directory=True)
    for component in relative.parts:
        current = current / component
        _lstat(current, directory=None)


def _read_regular(path: Path, root: Path) -> bytes:
    _safe_components(path, root)
    before = _lstat(path, directory=False)
    try:
        with path.open("rb") as handle:
            payload = handle.read()
    except OSError as exc:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc
    after = _lstat(path, directory=False)
    if _identity(before) != _identity(after):
        raise ReleaseBuildFailure("RELEASE_CHECK_STALE")
    return payload


def _ensure_dir(path: Path, root: Path) -> None:
    if path == root:
        _lstat(path, directory=True)
        return
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc
    current = root.absolute()
    _lstat(current, directory=True)
    for component in relative.parts:
        current = current / component
        try:
            _lstat(current, directory=True)
        except ReleaseBuildFailure:
            if current.exists() or current.is_symlink():
                raise
            try:
                current.mkdir()
            except OSError as exc:
                raise ReleaseBuildFailure("RELEASE_BUILD_FAILED") from exc
            _lstat(current, directory=True)


def _write_file(path: Path, payload: bytes, *, root: Path, exclusive: bool = True) -> None:
    _ensure_dir(path.parent, root)
    flags = os.O_WRONLY | os.O_CREAT | os.O_BINARY if hasattr(os, "O_BINARY") else os.O_WRONLY | os.O_CREAT
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc
    except OSError as exc:
        raise ReleaseBuildFailure("RELEASE_BUILD_FAILED") from exc
    _lstat(path, directory=False)


def _fsync_directory(path: Path) -> None:
    try:
        directory_flag = getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(path, os.O_RDONLY | directory_flag)
    except OSError:
        # Windows has no portable directory fd.  MoveFileExW below provides
        # the durable rename primitive there.
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: Mapping[str, Any], *, root: Path) -> None:
    _ensure_dir(path.parent, root)
    temporary = path.with_name(path.name + ".tmp")
    try:
        if temporary.exists() or temporary.is_symlink():
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        if path.exists() or path.is_symlink():
            _lstat(path, directory=False)
        _write_file(temporary, _canonical_bytes(value), root=root)
        _lstat(path.parent, directory=True)
        os.replace(temporary, path)
        _lstat(path, directory=False)
        _fsync_directory(path.parent)
    except ReleaseBuildFailure:
        try:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass
        raise
    except OSError as exc:
        try:
            if temporary.exists() and not temporary.is_symlink():
                temporary.unlink()
        except OSError:
            pass
        raise ReleaseBuildFailure("RELEASE_BUILD_FAILED") from exc


def _json_bytes(path: Path, root: Path) -> tuple[Any, bytes]:
    payload = _read_regular(path, root)
    try:
        return json.loads(payload.decode("utf-8")), payload
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc


def _hash_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _parse_jsonl(payload: bytes) -> list[dict[str, Any]]:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        return [{"__invalid_record__": "INVALID_UTF8"}]
    rows: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            rows.append({"__invalid_record__": "INVALID_JSON"})
            continue
        if not isinstance(value, dict):
            rows.append({"__invalid_record__": "INVALID_RECORD"})
            continue
        rows.append(value)
    return rows


def _stable_rows(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    return sorted(rows, key=lambda row: str(row.get(key, "")).encode("utf-8"))


def _safe_error(exc: BaseException, default: str = "RELEASE_BUILD_INTEGRITY_FAILED") -> str:
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", code) else default


def _record_ok(schema_id: str, value: Any, repo_root: Path) -> bool:
    if not isinstance(value, dict):
        return False
    try:
        validate_record(schema_id, value, repo_root=repo_root)
    except (RecordSchemaError, TypeError, ValueError, KeyError):
        return False
    return True


def _failure_for_source_state(state: Mapping[str, Any], failures: list[dict[str, Any]]) -> str | None:
    for failure in failures:
        if failure.get("scope") == "state" and failure.get("state_id") == state.get("state_id"):
            return str(failure.get("failure_id"))
    for failure in failures:
        if failure.get("block_id") == state.get("block_id") and failure.get("scope") in {"block", "variant", "render"}:
            return str(failure.get("failure_id"))
    return None


def _machine_variant_facts_equal(workspace: Any, source: Any) -> bool:
    if not isinstance(workspace, dict) or not isinstance(source, dict):
        return False
    geometry = workspace.get("geometry")
    source_geometry = source.get("shape")
    source_collision = source.get("collision")
    if not isinstance(geometry, dict) or geometry.get("shape") != source_geometry or geometry.get("collision") != source_collision:
        return False
    for key in ("geometry_signature", "collision_signature", "behavior_fingerprint", "behavior_by_state"):
        source_value = source.get(key)
        workspace_value = geometry.get(key) if key in {"geometry_signature", "collision_signature"} else workspace.get(key)
        if workspace_value != source_value:
            return False
    return True


def _read_export_checksum_inventory(export_root: Path, root: Path, trusted_anchor: str) -> list[dict[str, str]]:
    checksum_path = export_root / "checksums.sha256"
    payload = _read_regular(checksum_path, root)
    if sha256_bytes(payload) != trusted_anchor:
        raise ReleaseBuildFailure("EXPORT_CHECKSUM_ANCHOR_MISMATCH")
    expected: list[tuple[str, str]] = []
    try:
        for line in payload.decode("ascii").splitlines(keepends=True):
            if not line.endswith("\n") or line.count("  ") != 1:
                raise ValueError
            digest, relative = line[:-1].split("  ", 1)
            if not _UNPREFIXED_HASH_RE.fullmatch(digest) or relative == "checksums.sha256":
                raise ValueError
            safe_relative_posix_ref(relative)
            expected.append((relative, digest))
    except (UnicodeDecodeError, ValueError):
        raise ReleaseBuildFailure("EXPORT_CHECKSUM_INVENTORY_INVALID")
    if [ref for ref, _digest in expected] != sorted({ref for ref, _digest in expected}, key=lambda value: value.encode("utf-8")):
        raise ReleaseBuildFailure("EXPORT_CHECKSUM_INVENTORY_INVALID")
    actual_export_files: list[str] = []
    for path in export_root.rglob("*"):
        relative = path.relative_to(export_root).as_posix()
        if relative == "checksums.sha256":
            continue
        if path.is_dir():
            _lstat(path, directory=True)
        else:
            _lstat(path, directory=False)
            actual_export_files.append(relative)
    expected_export_files = sorted(
        (ref for ref, _digest in expected if (export_root / ref).exists() or (export_root / ref).is_symlink()),
        key=lambda value: value.encode("utf-8"),
    )
    if expected_export_files != sorted(actual_export_files, key=lambda value: value.encode("utf-8")):
        raise ReleaseBuildFailure("EXPORT_CHECKSUM_INVENTORY_MISMATCH")
    actual: list[tuple[str, str]] = []
    workspace_root = export_root.parent
    for relative, declared in expected:
        export_path = export_root / relative
        workspace_path = workspace_root / relative
        path = export_path if export_path.exists() or export_path.is_symlink() else workspace_path
        safe_relative_posix_ref(relative)
        try:
            digest = hashlib.sha256(_read_regular(path, root)).hexdigest()
        except ReleaseBuildFailure as exc:
            raise ReleaseBuildFailure("EXPORT_CHECKSUM_INVENTORY_MISMATCH") from exc
        if digest != declared:
            raise ReleaseBuildFailure("EXPORT_CHECKSUM_INVENTORY_MISMATCH")
        actual.append((relative, digest))
    return [{"path": ref, "sha256": digest} for ref, digest in actual]


def _has_sensitive_text(value: Any) -> bool:
    if isinstance(value, str):
        return is_sensitive_review_text(value)
    if isinstance(value, dict):
        return any(_has_sensitive_text(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_sensitive_text(child) for child in value)
    return False


def _record_list_json(database: Any, table: str, columns: str, order: str = "") -> list[dict[str, Any]]:
    rows = database.execute(f"SELECT {columns} FROM {table}{order}").fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        try:
            value = json.loads(row["record_json"])
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            result.append({"__invalid_record__": "INVALID_JSON"})
            continue
        if not isinstance(value, dict):
            result.append({"__invalid_record__": "INVALID_RECORD"})
            continue
        result.append(value)
    return result


def _provider_snapshot(config: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = config.get("provider_snapshot")
    if not isinstance(raw, dict):
        return None
    profile_value = raw.get("profile")
    profile: dict[str, Any] = profile_value if isinstance(profile_value, dict) else {}
    def choose(name: str, fallback: Any = None) -> Any:
        return raw.get(name, profile.get(name, fallback))
    snapshot = {
        "adapter": choose("adapter"),
        "profile_id": choose("profile_id"),
        "model_id": choose("model_id"),
        "base_url_stable_id": choose("base_url_stable_id"),
        "secret_reference": choose("secret_reference"),
        "prompt_version": choose("prompt_version"),
        "request_envelope_schema_id": "provider-batch-envelope.v1",
        "wire_schema_ids": {
            "offline_annotation": choose("annotation_output_schema_id", "annotation-batch-output.v1"),
            "query_spec": choose("query_spec_output_schema_id", "query-spec-output.v1"),
            "visual_rerank": choose("rerank_output_schema_id", "rerank-output.v1"),
        },
        "search_ranking_version": choose("search_ranking_version"),
    }
    if snapshot["adapter"] not in _OPENAI_ADAPTERS or snapshot["request_envelope_schema_id"] != "provider-batch-envelope.v1":
        return None
    if snapshot["wire_schema_ids"] != {
        "offline_annotation": "annotation-batch-output.v1",
        "query_spec": "query-spec-output.v1",
        "visual_rerank": "rerank-output.v1",
    }:
        return None
    if not all(isinstance(snapshot.get(key), str) and snapshot[key] for key in ("profile_id", "model_id", "base_url_stable_id", "secret_reference", "prompt_version", "search_ranking_version")):
        return None
    return snapshot


def _toolchain_lock_hash(repo_root: Path) -> str:
    files = (repo_root / "gradle" / "dependency-locks" / "lockfile", repo_root / "gradle" / "verification-metadata.xml")
    values: list[dict[str, str]] = []
    for path in files:
        if not path.is_file() or path.is_symlink():
            continue
        values.append({"path": path.relative_to(repo_root).as_posix(), "sha256": sha256_bytes(path.read_bytes())})
    return sha256_json(values)


def _safe_release_relative(ref: str) -> str:
    return safe_relative_posix_ref(ref)


def _workspace_path(data_root: DataRoot, minecraft_version: str, run_id: str) -> Path:
    path = data_root.workspace_dir(minecraft_version, run_id)
    _safe_components(path, data_root.root)
    return path


def _banner_refresh_provenance(
    report_json: Any,
    current_import: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the one supported D-045 refresh lineage, or leave legacy reports alone."""

    if not isinstance(report_json, dict):
        return None, None

    if not set(report_json).intersection(_BANNER_REFRESH_MARKER_KEYS):
        # This is the pre-D-045 legacy import report.  Preserve its historical
        # release behavior and do not validate unrelated fields.
        return None, None

    # D-045 deliberately has one direct, closed object.  A wrapper, alias,
    # legacy report field, or any unknown field is not a refresh report.
    if set(report_json) != _BANNER_REFRESH_PROVENANCE_FIELDS:
        return None, "AI_LINEAGE_INVALID"
    if report_json.get("format") != _BANNER_REFRESH_FORMAT:
        return None, "AI_LINEAGE_INVALID"
    base = report_json.get("base")
    new = report_json.get("new")
    if not isinstance(base, dict) or not isinstance(new, dict):
        return None, "AI_LINEAGE_INVALID"
    if set(base) != _BANNER_REFRESH_LINEAGE_FIELDS or set(new) != _BANNER_REFRESH_LINEAGE_FIELDS:
        return None, "AI_LINEAGE_INVALID"

    def valid_import_id(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(r"import_[0-9a-f]{32}", value) is not None

    def valid_export_id(value: Any) -> bool:
        return isinstance(value, str) and re.fullmatch(
            r"export_[0-9]{8}T[0-9]{6}Z(?:_(?:0[1-9]|[1-9][0-9]))?", value
        ) is not None

    def valid_lineage(value: Mapping[str, Any]) -> bool:
        return (
            valid_import_id(value.get("import_id"))
            and valid_export_id(value.get("export_id"))
            and _HASH_RE.fullmatch(str(value.get("manifest_sha256"))) is not None
            and _HASH_RE.fullmatch(str(value.get("checksum_sha256"))) is not None
        )

    targets = report_json.get("target_ids")
    if (
        not valid_lineage(base)
        or not valid_lineage(new)
        or base == new
        or base.get("import_id") == new.get("import_id")
        or base.get("export_id") == new.get("export_id")
        or base.get("manifest_sha256") == new.get("manifest_sha256")
        or base.get("checksum_sha256") == new.get("checksum_sha256")
        or not isinstance(report_json.get("check_id"), str)
        or RELEASE_CHECK_ID_RE.fullmatch(report_json["check_id"]) is None
        or not isinstance(targets, list)
        or len(targets) != len(_BANNER_REFRESH_TARGETS)
        or targets != list(_BANNER_REFRESH_TARGETS)
        or not isinstance(report_json.get("policy_token"), str)
        or report_json["policy_token"] != _BANNER_REFRESH_POLICY_TOKEN
    ):
        return None, "AI_LINEAGE_INVALID"

    if (
        new.get("import_id") != current_import.get("import_id")
        or new.get("export_id") != current_import.get("export_id")
        or new.get("manifest_sha256") != current_import.get("manifest_sha256")
        or new.get("checksum_sha256") != current_import.get("checksum_sha256")
    ):
        return None, "AI_LINEAGE_INVALID"

    # Keep the exact canonical direct object (with fresh nested containers) in
    # both the snapshot fingerprint and release functional inputs.
    return {
        "format": report_json["format"],
        "base": dict(base),
        "new": dict(new),
        "check_id": report_json["check_id"],
        "target_ids": list(targets),
        "policy_token": report_json["policy_token"],
    }, None


class ReleaseBuilder:
    """Build one candidate from a checked run without touching current.json."""

    def __init__(
        self,
        data_root: DataRoot,
        *,
        repo_root: Path | None = None,
        force_normalized_like: bool = False,
        pre_rename_hook: Callable[[Path, Path], None] | None = None,
    ) -> None:
        self.data_root = data_root
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.force_normalized_like = force_normalized_like
        self.pre_rename_hook = pre_rename_hook

    def get_check_state(self, check_id: str) -> CheckState:
        if not isinstance(check_id, str) or RELEASE_CHECK_ID_RE.fullmatch(check_id) is None:
            raise ReleaseCheckNotFound()
        root = self.data_root.cache / "release-checks"
        if not root.is_dir():
            raise ReleaseCheckNotFound()
        directory = root / check_id
        try:
            _safe_components(directory, self.data_root.root)
        except ReleaseBuildFailure as exc:
            raise ReleaseCheckNotFound() from exc
        state_path = directory / "state.json"
        report_path = directory / "quality_report.json"
        try:
            children = list(directory.iterdir())
            if {item.name for item in children} != {"state.json", "quality_report.json"}:
                raise ReleaseCheckNotFound()
            for item in children:
                _lstat(item, directory=False)
            state, state_bytes = _json_bytes(state_path, self.data_root.root)
            report, report_bytes = _json_bytes(report_path, self.data_root.root)
        except ReleaseBuildFailure as exc:
            raise ReleaseCheckNotFound() from exc
        if not _valid_check_state(state, check_id) or not _valid_check_report(report, check_id):
            raise ReleaseCheckNotFound()
        if state.get("quality_report_sha256") != sha256_bytes(report_bytes):
            raise ReleaseCheckNotFound()
        if report.get("snapshot_fingerprint") != state.get("snapshot_fingerprint"):
            raise ReleaseCheckNotFound()
        return CheckState(state, directory, report)

    def latest_check(self, run_id: str, minecraft_version: str) -> CheckState:
        root = self.data_root.cache / "release-checks"
        if not root.is_dir():
            raise ReleaseCheckNotFound()
        candidates: list[CheckState] = []
        _safe_components(root, self.data_root.root)
        for entry in sorted(root.iterdir(), key=lambda item: item.name):
            try:
                _lstat(entry)
            except ReleaseBuildFailure:
                continue
            if RELEASE_CHECK_ID_RE.fullmatch(entry.name) is None:
                continue
            try:
                value = self.get_check_state(entry.name)
            except ReleaseCheckNotFound:
                continue
            state = value.value
            if state["run_id"] == run_id and state["minecraft_version"] == minecraft_version:
                candidates.append(value)
        if not candidates:
            raise ReleaseCheckNotFound()
        return max(candidates, key=lambda item: (str(item.value["created_at"]).encode("utf-8"), item.value["check_id"].encode("utf-8")))

    def check(self, run_id: str, minecraft_version: str) -> dict[str, Any]:
        validate_minecraft_version(minecraft_version)
        workspace = _workspace_path(self.data_root, minecraft_version, run_id)
        database_path = workspace / "work.sqlite3"
        try:
            _safe_components(database_path, self.data_root.root)
            database = WorkspaceDatabase.open(database_path, force_normalized_like=self.force_normalized_like, read_only=True)
        except DatabaseSchemaMismatch as exc:
            raise ReleaseBuildFailure("DATABASE_SCHEMA_MISMATCH") from exc
        except ReleaseBuildFailure:
            raise ReleaseBuildFailure("RUN_NOT_FOUND")
        try:
            with database.read_transaction() as connection:
                run_row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if run_row is None:
                    raise ReleaseBuildFailure("RUN_NOT_FOUND")
                if str(run_row["minecraft_version"]) != minecraft_version:
                    raise ReleaseBuildFailure("RELEASE_VERSION_MISMATCH")
                self._require_check_precondition(connection, run_id)
                snapshot = self._snapshot(connection, workspace, run_row, minecraft_version)
                report, source_export_id = self._gate(snapshot, database)
        finally:
            database.close()

        check_id = _token("check_")
        release_build_id = _token("build_")
        created_at = _now()
        report.update({
            "check_id": check_id,
            "release_build_id": release_build_id,
            "run_id": run_id,
            "minecraft_version": minecraft_version,
            "snapshot_fingerprint": snapshot.fingerprint,
            "created_at": created_at,
            "updated_at": created_at,
        })
        report = _ordered_check_report(report)
        report_bytes = _canonical_bytes(report)
        state = {
            "format_version": 1,
            "check_id": check_id,
            "release_build_id": release_build_id,
            "run_id": run_id,
            "minecraft_version": minecraft_version,
            "source_export_id": source_export_id,
            # A completed Gate C operation is always a passed check.  The
            # report carries buildable/blocked; can_build is not a run state.
            "status": "passed",
            "can_build": bool(report["can_build"]),
            "snapshot_fingerprint": snapshot.fingerprint,
            "quality_report_sha256": sha256_bytes(report_bytes),
            "release_id": None,
            "created_at": created_at,
            "updated_at": created_at,
            "error_code": None,
        }
        cache_root = self.data_root.cache / "release-checks"
        _ensure_dir(cache_root, self.data_root.root)
        cache_dir = cache_root / check_id
        _ensure_dir(cache_dir, self.data_root.root)
        _atomic_json(cache_dir / "quality_report.json", report, root=self.data_root.root)
        _atomic_json(cache_dir / "state.json", state, root=self.data_root.root)
        return {
            "check_id": check_id,
            "release_build_id": release_build_id,
            "run_id": run_id,
            "minecraft_version": minecraft_version,
            "status": "passed",
            "can_build": bool(report["can_build"]),
            "snapshot_fingerprint": snapshot.fingerprint,
            "quality_report_sha256": state["quality_report_sha256"],
            "created_at": created_at,
            "updated_at": created_at,
        }

    def build(self, check_id: str) -> dict[str, Any]:
        checked = self.get_check_state(check_id)
        state = checked.value
        if state["status"] == "built":
            raise ReleaseBuildFailure("RELEASE_ALREADY_BUILT")
        if state["status"] == "stale":
            raise ReleaseBuildFailure("RELEASE_CHECK_STALE")
        if state["status"] != "passed" or state["can_build"] is not True:
            raise ReleaseBuildFailure("RELEASE_CHECK_NOT_READY")
        latest = self.latest_check(state["run_id"], state["minecraft_version"])
        if latest.value["check_id"] != check_id:
            raise ReleaseBuildFailure("RELEASE_CHECK_STALE")
        if latest.value["status"] == "built":
            raise ReleaseBuildFailure("RELEASE_ALREADY_BUILT")
        if not latest.value["can_build"]:
            raise ReleaseBuildFailure("RELEASE_CHECK_NOT_READY")

        version = str(state["minecraft_version"])
        run_id = str(state["run_id"])
        workspace = _workspace_path(self.data_root, version, run_id)
        database_path = workspace / "work.sqlite3"
        try:
            database = WorkspaceDatabase.open(database_path, force_normalized_like=self.force_normalized_like, read_only=True)
        except DatabaseSchemaMismatch as exc:
            raise ReleaseBuildFailure("DATABASE_SCHEMA_MISMATCH") from exc
        staging: Path | None = None
        staging_identity: tuple[int, int, int] | None = None
        final: Path | None = None
        committed = False
        try:
            with database.read_transaction() as connection:
                run_row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if run_row is None:
                    raise ReleaseBuildFailure("RUN_NOT_FOUND")
                if str(run_row["minecraft_version"]) != version:
                    raise ReleaseBuildFailure("RELEASE_VERSION_MISMATCH")
                current = self._snapshot(connection, workspace, run_row, version)
            if current.fingerprint != state["snapshot_fingerprint"]:
                self._mark_stale(state)
                raise ReleaseBuildFailure("RELEASE_CHECK_STALE")

            release_id = _release_id_for_build_id(str(state["release_build_id"]))
            committed_result = self._find_committed_release(state, current, check_id, release_id)
            if committed_result is not None:
                return committed_result

            # The release ID is the durable identity of this build.  It is
            # derived from the already persisted build ID, never reallocated
            # after a post-commit failure.
            release_parent = self.data_root.releases / version
            _ensure_dir(self.data_root.releases, self.data_root.root)
            _ensure_dir(release_parent, self.data_root.root)
            final = release_parent / release_id
            staging = release_parent / ("." + release_id + ".staging")
            _lstat(release_parent, directory=True)
            if final.exists() or final.is_symlink():
                raise ReleaseBuildFailure("RELEASE_ALREADY_BUILT")
            if staging.exists() or staging.is_symlink():
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
            staging.mkdir()
            staging_stat = _lstat(staging, directory=True)
            staging_identity = (int(staging_stat.st_dev), int(staging_stat.st_ino), stat.S_IFMT(staging_stat.st_mode))
            self._build_staging(staging, current, release_id, state, checked.report)

            with database.read_transaction() as connection:
                after = self._snapshot(connection, workspace, connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), version)
            if after.fingerprint != state["snapshot_fingerprint"]:
                self._mark_stale(state)
                raise ReleaseBuildFailure("RELEASE_CHECK_STALE")
            latest = self.latest_check(run_id, version)
            if latest.value["check_id"] != check_id:
                self._mark_stale(state)
                raise ReleaseBuildFailure("RELEASE_CHECK_STALE")

            if self.pre_rename_hook is not None:
                try:
                    self.pre_rename_hook(staging, final)
                except ReleaseBuildFailure:
                    raise
                except Exception as exc:
                    raise ReleaseBuildFailure("RELEASE_BUILD_FAILED") from exc
                with database.read_transaction() as connection:
                    hook_snapshot = self._snapshot(connection, workspace, connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone(), version)
                if hook_snapshot.fingerprint != state["snapshot_fingerprint"]:
                    self._mark_stale(state)
                    raise ReleaseBuildFailure("RELEASE_CHECK_STALE")
            # The hook is intentionally allowed to inject a last-moment
            # mutation.  Revalidate the complete package after it returns;
            # fingerprinting the workspace alone cannot detect a staged-file
            # replacement.
            self._validate_final(staging, release_id, current, state, root_for_components=staging, after_commit=False)
            _fsync_tree(staging)
            _fsync_directory(release_parent)
            _commit_directory(staging, final)
            committed = True
            self._validate_final(final, release_id, current, state, root_for_components=self.data_root.root)
            _make_read_only(final)
            _fsync_directory(release_parent)
            result = {
                "check_id": check_id,
                "release_build_id": state["release_build_id"],
                "release_id": release_id,
                "run_id": run_id,
                "minecraft_version": version,
                "relative_path": f"releases/{version}/{release_id}",
                "status": "built",
                "manifest_sha256": _hash_file(final / "manifest.json", final),
                "quality_report_sha256": _hash_file(final / "quality_report.json", final),
                "checksums_sha256": _hash_file(final / "checksums.sha256", final),
                "built_at": current.run.get("built_at", _read_release_built_at(final, self.data_root.root)),
            }
            # The caller completes the workspace transaction and then asks us
            # to atomically publish this state outside the immutable release.
            return result
        except ReleaseBuildFailure:
            if staging is not None and not committed:
                _remove_exact_staging(staging, staging_identity)
            raise
        except Exception as exc:
            if staging is not None and not committed:
                _remove_exact_staging(staging, staging_identity)
            raise ReleaseBuildFailure("RELEASE_BUILD_FAILED", after_commit=committed) from exc
        finally:
            database.close()

    def _find_committed_release(self, state: Mapping[str, Any], snapshot: Snapshot, check_id: str, release_id: str) -> dict[str, Any] | None:
        parent = self.data_root.releases / str(state["minecraft_version"])
        if not parent.is_dir():
            return None
        _safe_components(parent, self.data_root.root)
        exact = parent / release_id
        if exact.exists() or exact.is_symlink():
            try:
                _lstat(exact, directory=True)
                self._validate_final(exact, release_id, snapshot, state, root_for_components=self.data_root.root)
                _make_read_only(exact)
            except ReleaseBuildFailure as exc:
                if exc.code == "RELEASE_ALREADY_BUILT":
                    raise
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc
            except (OSError, sqlite3.Error) as exc:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc
            return self._result_from_final(exact, check_id, state)
        for entry in sorted(parent.iterdir(), key=lambda item: item.name):
            if RELEASE_ID_RE.fullmatch(entry.name) is None:
                continue
            if entry.name == release_id:
                continue
            try:
                _lstat(entry, directory=True)
                report, _ = _json_bytes(entry / "quality_report.json", self.data_root.root)
            except ReleaseBuildFailure:
                continue
            if isinstance(report, dict) and report.get("release_build_id") == state.get("release_build_id"):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=True)
        return None

    def _result_from_final(self, final: Path, check_id: str, state: Mapping[str, Any]) -> dict[str, Any]:
        release_json, _ = _json_bytes(final / "release.json", self.data_root.root)
        return {
            "check_id": check_id,
            "release_build_id": state["release_build_id"],
            "release_id": final.name,
            "run_id": state["run_id"],
            "minecraft_version": state["minecraft_version"],
            "relative_path": f"releases/{state['minecraft_version']}/{final.name}",
            "status": "built",
            "manifest_sha256": _hash_file(final / "manifest.json", final),
            "quality_report_sha256": _hash_file(final / "quality_report.json", final),
            "checksums_sha256": _hash_file(final / "checksums.sha256", final),
            "built_at": release_json.get("built_at"),
        }

    def _require_check_precondition(self, connection: Any, run_id: str) -> None:
        run = connection.execute("SELECT status,current_stage,boundary_event FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ReleaseBuildFailure("RUN_NOT_FOUND")
        if run["status"] != "paused" or run["current_stage"] != "BUILD_RELEASE" or run["boundary_event"] != "R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING":
            raise ReleaseBuildFailure("RELEASE_CHECK_NOT_READY")
        rows = connection.execute("SELECT stage,status FROM stage_runs WHERE run_id=? ORDER BY ordinal", (run_id,)).fetchall()
        required = {"PREPARE", "IMPORT_EXPORT", "VALIDATE_REGISTRY", "VALIDATE_VARIANTS", "VALIDATE_RENDERS", "EXTRACT_FEATURES", "AI_ANNOTATE", "VALIDATE", "HUMAN_REVIEW"}
        by_stage = {str(row["stage"]): str(row["status"]) for row in rows}
        if any(by_stage.get(stage) != "succeeded" for stage in required) or by_stage.get("BUILD_RELEASE") != "pending" or by_stage.get("ACTIVATE_RELEASE") != "pending":
            raise ReleaseBuildFailure("RELEASE_CHECK_NOT_READY")

    def _snapshot(self, connection: Any, workspace: Path, run_row: Any, minecraft_version: str) -> Snapshot:
        if run_row is None:
            raise ReleaseBuildFailure("RUN_NOT_FOUND")
        run = {key: run_row[key] for key in ("run_id", "import_id", "minecraft_version", "status", "current_stage", "boundary_event")}
        import_row_raw = connection.execute("SELECT import_id,minecraft_version,export_id,manifest_sha256,checksum_sha256,report_json FROM imports WHERE import_id=?", (run["import_id"],)).fetchone()
        if import_row_raw is None:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        import_row = {key: import_row_raw[key] for key in ("import_id", "minecraft_version", "export_id", "manifest_sha256", "checksum_sha256")}
        try:
            report_json = json.loads(import_row_raw["report_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            # Existing releases never consumed this field.  Keep malformed
            # legacy reports opaque; a D-045 marker can only be recognized in
            # a successfully decoded object.
            report_json = {}
        banner_refresh_provenance, banner_refresh_error = _banner_refresh_provenance(report_json, import_row)
        try:
            config = json.loads(connection.execute("SELECT config_snapshot_json FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()["config_snapshot_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            config = {}
        if not isinstance(config, dict):
            config = {}

        export_root = workspace / "export"
        _safe_components(export_root, self.data_root.root)
        manifest_path = export_root / "manifest.json"
        try:
            manifest, manifest_bytes = _json_bytes(manifest_path, self.data_root.root)
        except ReleaseBuildFailure:
            manifest, manifest_bytes = {"__invalid_record__": "INVALID_MANIFEST"}, b""
        if not isinstance(manifest, dict) or not _record_ok("export-manifest.v1", manifest, self.repo_root):
            manifest = {"__invalid_record__": "INVALID_MANIFEST"}
        elif manifest.get("export_id") != import_row["export_id"] or sha256_bytes(manifest_bytes) != import_row["manifest_sha256"]:
            manifest["__invalid_record__"] = "MANIFEST_HASH_MISMATCH"

        source_records: dict[str, list[dict[str, Any]]] = {}
        schema_files = {
            "blocks.jsonl": "export-block.v1",
            "states.jsonl": "export-state.v1",
            "variants.jsonl": "export-variant.v1",
            "failures.jsonl": "export-failure.v1",
        }
        for filename, schema_id in schema_files.items():
            try:
                payload = _read_regular(export_root / filename, self.data_root.root)
            except ReleaseBuildFailure:
                payload = b'{"__invalid_record__":"MISSING_FILE"}\n'
            rows = _parse_jsonl(payload)
            source_records[filename] = rows

        export_checksum_inventory: list[dict[str, str]] = []
        export_checksum_errors: tuple[str, ...] = ()
        try:
            export_checksum_inventory = _read_export_checksum_inventory(
                export_root,
                self.data_root.root,
                str(import_row.get("checksum_sha256") or ""),
            )
        except ReleaseBuildFailure as exc:
            export_checksum_errors = ("EXPORT_CHECKSUM_INVALID", _safe_error(exc))

        source_file_hashes: dict[str, str] = {}
        artifact_errors: list[str] = []
        for filename in ("manifest.json", "blocks.jsonl", "states.jsonl", "variants.jsonl", "failures.jsonl", "checksums.sha256"):
            try:
                source_file_hashes[f"export/{filename}"] = sha256_bytes(
                    _read_regular(export_root / filename, self.data_root.root)
                )
            except ReleaseBuildFailure as exc:
                artifact_errors.extend(("SOURCE_EXPORT_ARTIFACT_INVALID", _safe_error(exc)))
                break

        blocks = _record_list_json(connection, "blocks", "block_id,record_json", " ORDER BY block_id")
        states = _record_list_json(connection, "states", "state_id,record_json", " ORDER BY state_id")
        variants = _record_list_json(connection, "variants", "variant_id,record_json", " WHERE status='selected' AND record_json IS NOT NULL ORDER BY variant_id")
        variant_sources: list[dict[str, Any]] = []
        for row in connection.execute("SELECT variant_id,source_json FROM variants WHERE status='selected' ORDER BY variant_id").fetchall():
            try:
                source_value = json.loads(row["source_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                source_value = {"__invalid_record__": "INVALID_JSON"}
            variant_sources.append({"variant_id": row["variant_id"], "source": source_value})
        features: list[dict[str, Any]] = []
        for row in connection.execute("SELECT variant_id,input_sha256,feature_extractor_version,feature_json,output_hash FROM features ORDER BY variant_id").fetchall():
            try:
                feature_json = json.loads(row["feature_json"])
            except (TypeError, ValueError, json.JSONDecodeError):
                feature_json = {"__invalid_record__": "INVALID_JSON"}
            features.append({"variant_id": row["variant_id"], "input_sha256": row["input_sha256"], "feature_extractor_version": row["feature_extractor_version"], "feature": feature_json, "output_hash": row["output_hash"]})
        failures = _record_list_json(connection, "failures", "failure_id,record_json", " ORDER BY failure_id")
        annotations = _record_list_json(connection, "annotations", "annotation_id,record_json", " ORDER BY annotation_id")
        overrides = _record_list_json(connection, "overrides", "override_id,record_json", " ORDER BY override_id")
        review_rows = connection.execute("SELECT review_id,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at,resolved_at FROM review_tasks WHERE minecraft_version=? ORDER BY review_id", (minecraft_version,)).fetchall()
        reviews: list[dict[str, Any]] = []
        for row in review_rows:
            try:
                evidence = json.loads(row["evidence_json"] or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                evidence = None
            reviews.append({
                "review_id": row["review_id"],
                "target_type": row["target_type"],
                "target_id": row["target_id"],
                "reason_code": row["reason_code"],
                "severity": row["severity"],
                "status": row["status"],
                "note": row["note"],
                "evidence": evidence,
                "created_at": row["created_at"],
                "resolved_at": row["resolved_at"],
            })

        artifacts: list[dict[str, Any]] = []
        for row in connection.execute("SELECT relative_ref,sha256,kind,metadata_json FROM artifacts ORDER BY kind,relative_ref,sha256").fetchall():
            ref = str(row["relative_ref"])
            metadata: Any
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                metadata = {"__invalid_record__": "INVALID_JSON"}
            try:
                payload = _read_regular(workspace / ref, self.data_root.root)
                actual = sha256_bytes(payload)
                if metadata.get("hash_mode") == "jcs":
                    actual = _hash_json(json.loads(payload.decode("utf-8")))
                parsed_payload = _load_json_bytes(payload) if row["kind"] in {"ai_annotation", "feature_output"} else None
                artifacts.append({"relative_ref": ref, "declared_sha256": row["sha256"], "actual_sha256": actual, "kind": row["kind"], "metadata": metadata, "payload": parsed_payload})
                if actual != row["sha256"]:
                    artifact_errors.append("ARTIFACT_HASH_MISMATCH")
            except ReleaseBuildFailure as exc:
                artifact_errors.append(_safe_error(exc))
                artifacts.append({"relative_ref": ref, "declared_sha256": row["sha256"], "actual_sha256": None, "kind": row["kind"], "metadata": metadata, "payload": None})
            except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError):
                artifact_errors.append("ARTIFACT_JSON_INVALID")
                artifacts.append({"relative_ref": ref, "declared_sha256": row["sha256"], "actual_sha256": None, "kind": row["kind"], "metadata": metadata, "payload": None})
        provider_requests: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT request_id,profile_id,stage,wire_schema_id,attempt,cache_key,input_sha256,validated_artifact_sha256,error_code,error_class,envelope_json,status FROM provider_requests ORDER BY request_id,attempt"
        ).fetchall():
            try:
                envelope = json.loads(row["envelope_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                envelope = {"__invalid_record__": "INVALID_JSON"}
            provider_requests.append({
                "request_id": row["request_id"],
                "profile_id": row["profile_id"],
                "stage": row["stage"],
                "wire_schema_id": row["wire_schema_id"],
                "attempt": row["attempt"],
                "cache_key": row["cache_key"],
                "input_sha256": row["input_sha256"],
                "validated_artifact_sha256": row["validated_artifact_sha256"],
                "error_code": row["error_code"],
                "error_class": row["error_class"],
                "envelope": envelope,
                "status": row["status"],
            })
        ai_jobs: list[dict[str, Any]] = []
        for row in connection.execute(
            "SELECT job_id,logical_key,input_signature,status,output_hash,cursor_json FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY job_id",
            (run["run_id"],),
        ).fetchall():
            try:
                cursor = json.loads(row["cursor_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                cursor = {"__invalid_record__": "INVALID_JSON"}
            ai_jobs.append({
                "job_id": row["job_id"],
                "logical_key": row["logical_key"],
                "input_signature": row["input_signature"],
                "status": row["status"],
                "output_hash": row["output_hash"],
                "variant_ids": cursor.get("variant_ids", []) if isinstance(cursor, dict) else [],
                "retry_nonce": cursor.get("retry_nonce") if isinstance(cursor, dict) else None,
            })
        release_index_sql, release_index_sql_hash = packaged_release_index_schema()
        del release_index_sql
        provider_schema_ids = tuple(
            schema_id
            for schema_id in _PROVIDER_SCHEMA_IDS
            if schema_id == "provider-batch-envelope.v1" and provider_requests
            or schema_id == "annotation-batch-output.v1" and any(row["stage"] == "offline_annotation" for row in provider_requests)
            or schema_id == "annotation-wire-item.v1" and any(row["stage"] == "offline_annotation" for row in provider_requests)
        )
        schema_ids = tuple(dict.fromkeys((*_PHASE_C_SCHEMA_IDS, *provider_schema_ids)))
        schema_inventory = _schema_inventory_entries(self.repo_root, schema_ids)
        capabilities_value = config.get("capabilities")
        capabilities_map: dict[str, Any] = capabilities_value if isinstance(capabilities_value, dict) else {}
        provider_snapshot = _provider_snapshot(config)
        persisted_capability_adapter = capabilities_map.get("adapter")
        if persisted_capability_adapter is None and provider_snapshot is not None:
            raw_provider_snapshot = config.get("provider_snapshot")
            profile_snapshot = raw_provider_snapshot.get("profile") if isinstance(raw_provider_snapshot, dict) else None
            if isinstance(profile_snapshot, dict):
                persisted_capability_adapter = profile_snapshot.get("adapter")
        provider_capabilities: dict[str, Any] = {
            key: bool(value)
            for key, value in sorted(capabilities_map.items())
            if key in {"image_input_supported", "structured_outputs_supported", "error_classification_supported"}
        }
        provider_capabilities["adapter"] = persisted_capability_adapter
        fingerprint_ai_jobs = ai_jobs
        if banner_refresh_provenance is None and banner_refresh_error is None:
            # Keep legacy normal-import fingerprints byte-equivalent.  The
            # logical key is only needed to prove D-045 preserved rows.
            fingerprint_ai_jobs = [
                {key: value for key, value in job.items() if key != "logical_key"}
                for job in ai_jobs
            ]
        manifest_hash = sha256_bytes(manifest_bytes)
        snapshot_logic = {
            "run_id": run["run_id"],
            "minecraft_version": minecraft_version,
            "source_export_id": import_row["export_id"],
            "source_export_manifest_sha256": manifest_hash,
            "source_export_checksum_sha256": import_row["checksum_sha256"],
            "export_manifest": manifest,
            "source_file_hashes": source_file_hashes,
            "source_records": {
                filename: _stable_rows(rows, "block_id" if filename == "blocks.jsonl" else "state_id" if filename == "states.jsonl" else "variant_id" if filename == "variants.jsonl" else "failure_id")
                for filename, rows in sorted(source_records.items())
            },
            "workspace_schema": "workspace.v1",
            "blocks": _stable_rows(blocks, "block_id"),
            "states": _stable_rows(states, "state_id"),
            "variants": _stable_rows(variants, "variant_id"),
            "variant_sources": _stable_rows(variant_sources, "variant_id"),
            "features": _stable_rows(features, "variant_id"),
            "failures": _stable_rows(failures, "failure_id"),
            "annotations": _stable_rows(annotations, "annotation_id"),
            "manual_records": _stable_rows(overrides, "schema_version"),
            "reviews": _stable_rows(reviews, "review_id"),
            "artifacts": artifacts,
            "provider_requests": provider_requests,
            "ai_jobs": fingerprint_ai_jobs,
            "provider_snapshot": provider_snapshot,
            "provider_capabilities": provider_capabilities,
            "toolchain_lock_sha256": _toolchain_lock_hash(self.repo_root),
            "schema_ids": list(schema_ids),
            "schema_inventory": schema_inventory,
            "release_index_sql_sha256": release_index_sql_hash,
            "export_checksum_inventory": export_checksum_inventory,
            "export_checksum_errors": list(export_checksum_errors),
            "artifact_errors": sorted(set(artifact_errors)),
        }
        if banner_refresh_provenance is not None:
            snapshot_logic["banner_refresh_provenance"] = banner_refresh_provenance
        elif banner_refresh_error is not None:
            snapshot_logic["banner_refresh_error"] = banner_refresh_error
        fingerprint = sha256_json(snapshot_logic)
        return Snapshot(
            run=run,
            import_row=import_row,
            banner_refresh_provenance=banner_refresh_provenance,
            banner_refresh_error=banner_refresh_error,
            config=config,
            manifest=manifest,
            blocks=blocks,
            states=states,
            variants=variants,
            variant_sources=variant_sources,
            features=features,
            failures=failures,
            annotations=annotations,
            overrides=overrides,
            reviews=reviews,
            artifacts=artifacts,
            provider_requests=provider_requests,
            ai_jobs=ai_jobs,
            source_records=source_records,
            source_file_hashes=source_file_hashes,
            export_checksum_inventory=export_checksum_inventory,
            export_checksum_errors=export_checksum_errors,
            schema_ids=schema_ids,
            schema_inventory=schema_inventory,
            release_index_sql_sha256=release_index_sql_hash,
            fingerprint=fingerprint,
            artifact_errors=tuple(sorted(set(artifact_errors))),
        )

    def _gate(self, snapshot: Snapshot, database: WorkspaceDatabase) -> tuple[dict[str, Any], str]:
        values: dict[str, tuple[str, int, str | None]] = {}
        checks: tuple[Callable[[], tuple[str, int, str | None]], ...] = (
            lambda: self._gate_registry(snapshot),
            lambda: self._gate_variant_or_skip(snapshot),
            lambda: self._gate_excluded(snapshot),
            lambda: self._gate_images(snapshot),
            lambda: self._gate_states(snapshot),
            lambda: self._gate_machine(snapshot),
            lambda: self._gate_ai(snapshot, database),
            lambda: self._gate_overrides(snapshot),
            lambda: self._gate_ids(snapshot),
            lambda: self._gate_reviews(snapshot),
            lambda: self._gate_fts(snapshot, database),
        )
        for code, check in zip(CHECK_CODES[:11], checks):
            try:
                values[code] = check()
            except Exception:
                values[code] = ("failed", 0, "GATE_ITEM_INVALID")
        items: list[dict[str, Any]] = []
        for code in CHECK_CODES[:11]:
            status, count, error = values[code]
            items.append({"code": code, "status": status, "blocking": True, "observed_count": max(0, int(count)), "error_code": error, "evidence": ["state.json"]})
        items.append({"code": CHECK_CODES[11], "status": "not_run", "blocking": True, "observed_count": 0, "error_code": None, "evidence": ["state.json"]})
        can_build = all(item["status"] == "passed" for item in items[:11])
        report = {
            "format_version": 1,
            "report_kind": "candidate_check",
            "status": "buildable" if can_build else "blocked",
            "can_build": can_build,
            "items": items,
        }
        return report, str(snapshot.import_row["export_id"])

    def _gate_registry(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        if snapshot.export_checksum_errors:
            return "failed", len(snapshot.export_checksum_inventory), "EXPORT_CHECKSUM_INVALID"
        source_values = [str(row.get("block_id")) for row in snapshot.source_records["blocks.jsonl"]]
        source = set(source_values)
        workspace = {str(row.get("block_id")) for row in snapshot.blocks}
        expected = snapshot.manifest.get("counts", {}).get("registry_blocks")
        declared_hash = snapshot.manifest.get("scope", {}).get("registry_snapshot_sha256")
        actual_hash = sha256_bytes("\n".join(sorted(source, key=lambda value: value.encode("utf-8"))).encode("utf-8"))
        if len(source_values) == len(source) and source == workspace and expected == len(source) and declared_hash == actual_hash:
            return "passed", len(source), None
        return "failed", len(source), "REGISTRY_COVERAGE_INVALID"

    def _gate_variant_or_skip(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        block_ids = {str(row.get("block_id")) for row in snapshot.blocks}
        selected = {str(row.get("block_id")) for row in snapshot.variants}
        skip_records = [row for row in snapshot.overrides if row.get("schema_version") == "skip-review.v1"]
        missing = block_ids - selected
        skipped_by_block: dict[str, set[str]] = {}
        for record in snapshot.source_records["variants.jsonl"]:
            if record.get("status") == "skipped":
                skipped_by_block.setdefault(str(record.get("block_id")), set()).add(str(record.get("variant_id")))
        failures_by_block: dict[str, dict[str, set[str]]] = {}
        for failure in snapshot.source_records["failures.jsonl"]:
            block_id = failure.get("block_id")
            if isinstance(block_id, str):
                failures_by_block.setdefault(block_id, {}).setdefault(str(failure.get("failure_id")), set()).update(
                    value for value in (failure.get("block_id"), failure.get("state_id"), failure.get("variant_id")) if isinstance(value, str)
                )
        for block_id in missing:
            failure_map = failures_by_block.get(block_id, {})
            failure_ids = set(failure_map)
            failure_targets = {target for targets in failure_map.values() for target in targets}
            if not skipped_by_block.get(block_id) or not any(
                str(review.get("machine_failure_ref")) in failure_ids
                and str(review.get("target_id")) in {block_id} | skipped_by_block[block_id] | failure_targets
                for review in skip_records
            ):
                return "failed", len(block_ids), "BLOCK_VARIANT_OR_AUDITED_SKIP_INVALID"
        if any(str(row.get("variant_id")) not in block_ids for row in snapshot.variants):
            return "failed", len(block_ids), "BLOCK_VARIANT_OR_AUDITED_SKIP_INVALID"
        return "passed", len(block_ids), None

    def _gate_excluded(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        reviews = {str(row.get("review_id")): row for row in snapshot.overrides if row.get("schema_version") == "qualification-review.v1"}
        count = 0
        for variant in snapshot.variants:
            if variant.get("candidate_qualification") != "excluded":
                continue
            count += 1
            refs = set(variant.get("qualification_review_refs", []))
            if not any(ref in reviews and reviews[ref].get("target_id") == variant.get("variant_id") and reviews[ref].get("qualification") == "excluded" and _record_ok("qualification-review.v1", reviews[ref], self.repo_root) for ref in refs):
                return "failed", count, "EXCLUDED_QUALIFICATION_REVIEW_INVALID"
        return "passed", count, None

    def _gate_images(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        checked = 0
        workspace = self.data_root.workspace_dir(str(snapshot.run["minecraft_version"]), str(snapshot.run["run_id"]))
        artifacts = {(str(row.get("relative_ref")), str(row.get("declared_sha256")), str(row.get("actual_sha256"))) for row in snapshot.artifacts if row.get("kind") == "render"}
        for variant in snapshot.variants:
            render = variant.get("render")
            if not isinstance(render, dict):
                return "failed", checked, "IMAGE_ARTIFACT_INVALID"
            try:
                suffix = str(variant.get("variant_id", "")).removeprefix("minecraft:")
                for ref_key, hash_key in (("preview_path", "image_sha256"), ("mask_path", "mask_sha256")):
                    ref = str(render[ref_key])
                    _safe_release_relative(ref)
                    if ref != f"renders/minecraft/{suffix}/{'preview.png' if ref_key == 'preview_path' else 'mask.png'}":
                        return "failed", checked, "IMAGE_ARTIFACT_INVALID"
                    payload = _read_regular(workspace / ref, self.data_root.root)
                    decoded = decode_rgba_png(payload)
                    if decoded.width != 512 or decoded.height != 512 or sha256_bytes(payload) != render[hash_key]:
                        return "failed", checked, "IMAGE_ARTIFACT_INVALID"
                    if not any(ref == str(render[ref_key]) and declared == str(render[hash_key]) and actual == str(render[hash_key]) for ref, declared, actual in artifacts):
                        return "failed", checked, "IMAGE_ARTIFACT_INVALID"
                metadata_ref = str(render["render_metadata_path"])
                if metadata_ref != f"renders/minecraft/{suffix}/render.json":
                    return "failed", checked, "IMAGE_ARTIFACT_INVALID"
                metadata, metadata_bytes = _json_bytes(workspace / metadata_ref, self.data_root.root)
                if not _record_ok("render-metadata.v1", metadata, self.repo_root) or metadata.get("variant_id") != variant.get("variant_id") or _hash_json(metadata) != render.get("render_metadata_sha256"):
                    return "failed", checked, "IMAGE_ARTIFACT_INVALID"
                if not any(ref == metadata_ref and declared == str(render["render_metadata_sha256"]) and actual == str(render["render_metadata_sha256"]) for ref, declared, actual in artifacts):
                    return "failed", checked, "IMAGE_ARTIFACT_INVALID"
                checked += 1
            except ReleaseBuildFailure as exc:
                return "failed", checked, _safe_error(exc, "IMAGE_ARTIFACT_INVALID")
            except Exception:
                return "failed", checked, "IMAGE_ARTIFACT_INVALID"
        return "passed", checked, None

    def _gate_states(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        blocks = {str(row.get("block_id")): row for row in snapshot.blocks}
        states_by_id = {str(row.get("state_id")): row for row in snapshot.states}
        for block in snapshot.blocks:
            block_states = [state for state in snapshot.states if state.get("block_id") == block.get("block_id")]
            defaults = [state for state in block_states if state.get("is_default") is True]
            default_state = states_by_id.get(str(block.get("default_state_id")))
            if len(defaults) != 1 or default_state is None or default_state.get("block_id") != block.get("block_id") or defaults[0].get("state_id") != block.get("default_state_id"):
                return "failed", len(states_by_id), "LEGAL_STATE_INVALID"
        checked = 0
        selected_by_block: dict[str, set[str]] = {}
        for variant in snapshot.variants:
            selected_by_block.setdefault(str(variant.get("block_id")), set()).add(str(variant.get("variant_id")))
        for state in snapshot.states:
            checked += 1
            block = blocks.get(str(state.get("block_id")))
            if block is None or state.get("legal_state") is not True or not _record_ok("state-record.v1", state, self.repo_root):
                return "failed", checked, "LEGAL_STATE_INVALID"
            if state.get("state_id") == block.get("default_state_id") and state.get("is_default") is not True:
                return "failed", checked, "LEGAL_STATE_INVALID"
            references = state.get("variant_ids", [])
            if len(references) != len(set(references)) or any(str(reference) not in selected_by_block.get(str(block["block_id"]), set()) for reference in references):
                return "failed", checked, "STATE_VARIANT_RELATION_INVALID"
            expected_references = selected_by_block.get(str(block["block_id"]), set()) if state.get("mapping_status") == "mapped" else set()
            if set(references) != expected_references:
                return "failed", checked, "STATE_VARIANT_RELATION_INVALID"
        mapped_by_block: dict[str, set[str]] = {}
        for state in snapshot.states:
            if state.get("mapping_status") == "mapped":
                mapped_by_block.setdefault(str(state.get("block_id")), set()).add(str(state.get("state_id")))
        for variant in snapshot.variants:
            block_id = str(variant.get("block_id"))
            canonical = str(variant.get("canonical_state_id"))
            represented = variant.get("represented_state_ids", [])
            if len(represented) != len(set(represented)) or canonical != str(blocks.get(block_id, {}).get("default_state_id")) or canonical not in represented:
                return "failed", checked, "VARIANT_STATE_RELATION_INVALID"
            if any(str(state_id) not in states_by_id or states_by_id[str(state_id)].get("block_id") != block_id for state_id in represented):
                return "failed", checked, "VARIANT_STATE_RELATION_INVALID"
            if set(represented) != mapped_by_block.get(block_id, set()):
                return "failed", checked, "VARIANT_STATE_RELATION_INVALID"
        return "passed", checked, None

    def _gate_machine(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        count = 0
        source_blocks = snapshot.source_records["blocks.jsonl"]
        source_states = snapshot.source_records["states.jsonl"]
        source_variants = snapshot.source_records["variants.jsonl"]
        source_failures = snapshot.source_records["failures.jsonl"]
        for filename, schema_id in (("blocks.jsonl", "export-block.v1"), ("states.jsonl", "export-state.v1"), ("variants.jsonl", "export-variant.v1"), ("failures.jsonl", "export-failure.v1")):
            for record in snapshot.source_records[filename]:
                count += 1
                if not _record_ok(schema_id, record, self.repo_root):
                    return "failed", count, "MACHINE_SCHEMA_INVALID"
                if record.get("export_id") != snapshot.import_row["export_id"] or record.get("minecraft_version") != snapshot.run["minecraft_version"]:
                    return "failed", count, "MACHINE_FACTS_MISMATCH"
        for row in snapshot.variant_sources:
            count += 1
            if not _record_ok("export-variant.v1", row.get("source"), self.repo_root):
                return "failed", count, "MACHINE_SCHEMA_INVALID"
        for schema_id, records in (("block-record.v1", snapshot.blocks), ("state-record.v1", snapshot.states), ("visual-variant-record.v1", snapshot.variants)):
            for record in records:
                count += 1
                if not _record_ok(schema_id, record, self.repo_root):
                    return "failed", count, "MACHINE_SCHEMA_INVALID"
        feature_by_variant = {str(row.get("variant_id")): row for row in snapshot.features}
        workspace = self.data_root.workspace_dir(str(snapshot.run["minecraft_version"]), str(snapshot.run["run_id"]))
        feature_artifacts = {
            str(artifact.get("metadata", {}).get("variant_id")): artifact
            for artifact in snapshot.artifacts
            if artifact.get("kind") == "feature_output" and isinstance(artifact.get("metadata"), dict)
        }
        for variant in snapshot.variants:
            count += 1
            feature = feature_by_variant.get(str(variant.get("variant_id")))
            feature_value = feature.get("feature") if isinstance(feature, dict) else None
            if feature is None or not isinstance(feature_value, dict) or "__invalid_record__" in feature_value:
                return "failed", count, "MACHINE_SCHEMA_INVALID"
            if feature.get("feature_extractor_version") != feature_value.get("feature_extractor_version") or feature.get("input_sha256") != feature_value.get("input_sha256") or not _HASH_RE.fullmatch(str(feature.get("input_sha256"))) or not _HASH_RE.fullmatch(str(feature.get("output_hash"))):
                return "failed", count, "FEATURE_TRUTH_INVALID"
            if feature_value.get("feature_extractor_version") != "features.v1" or not isinstance(feature_value.get("geometry_classes"), list) or not isinstance(feature_value.get("machine_tags"), list):
                return "failed", count, "FEATURE_TRUTH_INVALID"
            machine_facts = variant.get("machine_facts", {})
            geometry = machine_facts.get("geometry", {}) if isinstance(machine_facts, dict) else {}
            if geometry.get("geometry_classes") != feature_value.get("geometry_classes") or geometry.get("feature_extractor_version") != feature_value.get("feature_extractor_version") or geometry.get("feature_input_sha256") != feature_value.get("input_sha256") or machine_facts.get("machine_tags") != feature_value.get("machine_tags"):
                return "failed", count, "FEATURE_TRUTH_INVALID"
            artifact = feature_artifacts.get(str(variant.get("variant_id")))
            if artifact is None or artifact.get("actual_sha256") != feature.get("output_hash") or artifact.get("declared_sha256") != feature.get("output_hash"):
                return "failed", count, "FEATURE_ARTIFACT_INVALID"
            try:
                artifact_payload = _load_json_bytes(_read_regular(workspace / str(artifact["relative_ref"]), self.data_root.root))
            except (ReleaseBuildFailure, KeyError, TypeError, ValueError):
                return "failed", count, "FEATURE_ARTIFACT_INVALID"
            if not isinstance(artifact_payload, dict) or artifact_payload.get("features") != feature_value or not isinstance(artifact_payload.get("record"), dict) or artifact_payload["record"].get("machine_facts") != machine_facts or sha256_bytes(canonical_json(artifact_payload).encode("utf-8")) != feature.get("output_hash"):
                return "failed", count, "FEATURE_ARTIFACT_INVALID"

        source_block_by_id = {str(row.get("block_id")): row for row in source_blocks}
        workspace_block_by_id = {str(row.get("block_id")): row for row in snapshot.blocks}
        if len(source_block_by_id) != len(source_blocks) or len(workspace_block_by_id) != len(snapshot.blocks) or set(source_block_by_id) != set(workspace_block_by_id):
            return "failed", count, "MACHINE_FACTS_MISMATCH"
        for block_id, source in source_block_by_id.items():
            expected = {
                "schema_version": "block-record.v1",
                "export_id": source.get("export_id"),
                "minecraft_version": source.get("minecraft_version"),
                "block_id": source.get("block_id"),
                "translation_key": source.get("translation_key"),
                "official_names": {"zh_cn": source.get("name_zh_cn"), "en_us": source.get("name_en_us")},
                "default_state_id": source.get("default_state_id"),
                "properties": source.get("properties"),
                "tags": source.get("tags"),
                "machine_facts": {"has_item": source.get("has_item"), "has_block_entity": source.get("has_block_entity")},
                "source": source.get("source"),
            }
            if workspace_block_by_id[block_id] != expected:
                return "failed", count, "MACHINE_FACTS_MISMATCH"

        source_state_by_id = {str(row.get("state_id")): row for row in source_states}
        workspace_state_by_id = {str(row.get("state_id")): row for row in snapshot.states}
        if len(source_state_by_id) != len(source_states) or len(workspace_state_by_id) != len(snapshot.states) or set(source_state_by_id) != set(workspace_state_by_id):
            return "failed", count, "MACHINE_FACTS_MISMATCH"
        for state_id, source in source_state_by_id.items():
            expected = {
                "schema_version": "state-record.v1",
                "export_id": source.get("export_id"),
                "minecraft_version": source.get("minecraft_version"),
                "state_id": source.get("state_id"),
                "block_id": source.get("block_id"),
                "properties": source.get("properties"),
                "is_default": source.get("is_default"),
                "legal_state": source.get("legal_state"),
                "shape": source.get("shape"),
                "collision": source.get("collision"),
                "behavior": source.get("behavior"),
                "variant_ids": source.get("variant_ids"),
                "mapping_status": source.get("mapping_status"),
                "failure_id": _failure_for_source_state(source, source_failures),
                "source": source.get("source"),
            }
            if workspace_state_by_id[state_id] != expected:
                return "failed", count, "MACHINE_FACTS_MISMATCH"
            block = source_block_by_id.get(str(source.get("block_id")))
            properties = source.get("properties", {})
            if block is None or set(properties) != set(block.get("properties", {})) or any(value not in block.get("properties", {}).get(name, []) for name, value in properties.items()):
                return "failed", count, "LEGAL_STATE_INVALID"

        selected_source = {str(row.get("variant_id")): row for row in source_variants if row.get("status") == "selected"}
        workspace_source = {str(row.get("variant_id")): row.get("source") for row in snapshot.variant_sources}
        if len(selected_source) != len(workspace_source) or set(selected_source) != set(workspace_source):
            return "failed", count, "MACHINE_FACTS_MISMATCH"
        workspace_variants = {str(row.get("variant_id")): row for row in snapshot.variants}
        for variant_id, source in selected_source.items():
            workspace = workspace_variants.get(variant_id)
            if workspace_source[variant_id] != source or workspace is None:
                return "failed", count, "MACHINE_FACTS_MISMATCH"
            context = workspace.get("context")
            source_context = source.get("context")
            context_equal = isinstance(context, dict) and isinstance(source_context, dict) and all(context.get(key) == source_context.get(key) for key in ("fixture_id", "fixture_version", "rotatable", "canonical_orientation"))
            if any(workspace.get(key) != source.get(key) for key in ("export_id", "minecraft_version", "variant_id", "block_id", "canonical_state_id", "represented_state_ids", "selection", "render")) or not context_equal or not _machine_variant_facts_equal(workspace.get("machine_facts"), source.get("machine_facts")):
                return "failed", count, "MACHINE_FACTS_MISMATCH"
        return "passed", count, None

    def _gate_ai(self, snapshot: Snapshot, database: WorkspaceDatabase) -> tuple[str, int, str | None]:
        count = 0
        if snapshot.banner_refresh_error is not None:
            return "failed", count, "AI_LINEAGE_INVALID"
        by_variant = {str(row.get("variant_id")): row for row in snapshot.variants}
        eligible = [variant for variant in snapshot.variants if variant.get("candidate_qualification") in {"eligible", "conditional"}]
        verified_by_variant: dict[str, list[dict[str, Any]]] = {}
        for annotation in snapshot.annotations:
            count += 1
            if not _record_ok("annotation-record.v1", annotation, self.repo_root):
                return "failed", count, "AI_SCHEMA_INVALID"
            subject = str(annotation.get("subject_id"))
            if subject not in by_variant and not any(str(block.get("block_id")) == subject for block in snapshot.blocks):
                return "failed", count, "AI_SCHEMA_INVALID"
            if annotation.get("source", {}).get("verified") is True:
                verified_by_variant.setdefault(subject, []).append(annotation)

        needs_provider = False
        for variant in eligible:
            variant_id = str(variant.get("variant_id"))
            verified = [row for row in verified_by_variant.get(variant_id, []) if row.get("source", {}).get("verified") is True]
            human_complete = human_semantics_complete(database.connection, variant_id)
            if not verified and not human_complete:
                return "failed", count, "AI_SEMANTIC_MISSING"
            if any(row.get("source", {}).get("type") == "llm" for row in verified):
                needs_provider = True
        provider = _provider_snapshot(snapshot.config)
        capabilities_value = snapshot.config.get("capabilities")
        capabilities: dict[str, Any] = capabilities_value if isinstance(capabilities_value, dict) else {}
        raw_provider_snapshot = snapshot.config.get("provider_snapshot")
        profile_snapshot = raw_provider_snapshot.get("profile") if isinstance(raw_provider_snapshot, dict) else None
        profile_snapshot = profile_snapshot if isinstance(profile_snapshot, dict) else {}
        persisted_capability_adapter = capabilities.get("adapter", profile_snapshot.get("adapter"))
        persisted_capability_status = capabilities.get("capability_status", profile_snapshot.get("capability_status"))
        if (
            provider is None
            or persisted_capability_status != "verified"
            or persisted_capability_adapter != provider.get("adapter")
            or any(
                capabilities.get(key) is not True
                for key in {"image_input_supported", "structured_outputs_supported", "error_classification_supported"}
            )
        ):
            return "failed", count, "AI_LINEAGE_INVALID"
        successful_requests = [
            request
            for request in snapshot.provider_requests
            if request.get("status") == "succeeded" and request.get("stage") == "offline_annotation"
        ]
        if not needs_provider and not successful_requests:
            return "passed", count, None
        artifacts_by_variant: dict[str, list[dict[str, Any]]] = {}
        for artifact in snapshot.artifacts:
            if artifact.get("kind") != "ai_annotation" or artifact.get("actual_sha256") != artifact.get("declared_sha256"):
                continue
            metadata = artifact.get("metadata")
            if not isinstance(metadata, dict) or not isinstance(metadata.get("variant_ids"), list):
                return "failed", count, "AI_LINEAGE_INVALID"
            for variant_id in metadata["variant_ids"]:
                artifacts_by_variant.setdefault(str(variant_id), []).append(artifact)

        request_by_variant: dict[str, list[dict[str, Any]]] = {}
        refresh_targets = set(snapshot.banner_refresh_provenance["target_ids"]) if snapshot.banner_refresh_provenance else set()
        current_export_id = str(snapshot.import_row["export_id"])
        base_export_id = (
            str(snapshot.banner_refresh_provenance["base"]["export_id"])
            if snapshot.banner_refresh_provenance
            else None
        )
        for request in snapshot.provider_requests:
            if request.get("status") != "succeeded" or request.get("stage") == "offline_annotation":
                continue
            envelope = request.get("envelope")
            if not isinstance(envelope, dict) or envelope.get("export_id") != current_export_id:
                return "failed", count, "AI_LINEAGE_INVALID"
        for request in successful_requests:
            envelope = request.get("envelope")
            if not isinstance(envelope, dict) or not _record_ok("provider-batch-envelope.v1", envelope, self.repo_root):
                return "failed", count, "AI_LINEAGE_INVALID"
            if envelope.get("adapter") != provider.get("adapter"):
                return "failed", count, "AI_LINEAGE_INVALID"
            if provider.get("adapter") == "openai_responses":
                if envelope.get("store") is not False:
                    return "failed", count, "AI_LINEAGE_INVALID"
            elif "store" in envelope:
                return "failed", count, "AI_LINEAGE_INVALID"
            provider_wire = provider.get("wire_schema_ids")
            if not isinstance(provider_wire, dict):
                return "failed", count, "AI_LINEAGE_INVALID"
            if (
                envelope.get("request_id") != request.get("request_id")
                or request.get("profile_id") != provider.get("profile_id")
                or request.get("stage") != "offline_annotation"
                or request.get("wire_schema_id") != "annotation-batch-output.v1"
                or envelope.get("schema_version") != provider.get("request_envelope_schema_id")
                or envelope.get("profile_id") != provider.get("profile_id")
                or envelope.get("model_id") != provider.get("model_id")
                or envelope.get("base_url_stable_id") != provider.get("base_url_stable_id")
                or envelope.get("secret_reference") != provider.get("secret_reference")
                or envelope.get("prompt_version") != provider.get("prompt_version")
                or envelope.get("search_ranking_version") != provider.get("search_ranking_version")
                or envelope.get("stage") != request.get("stage")
                or envelope.get("wire_schema_id") != request.get("wire_schema_id")
                or envelope.get("wire_schema_id") != provider_wire.get("offline_annotation")
                or envelope.get("wire_format_name") != "annotation_batch_output_v1"
                or envelope.get("minecraft_version") != snapshot.run.get("minecraft_version")
                or envelope.get("release_id") is not None
                or envelope.get("resolved_release_manifest_sha256") is not None
            ):
                return "failed", count, "AI_LINEAGE_INVALID"
            if not _HASH_RE.fullmatch(str(request.get("input_sha256"))) or not _HASH_RE.fullmatch(str(request.get("cache_key"))) or not _HASH_RE.fullmatch(str(request.get("validated_artifact_sha256"))):
                return "failed", count, "AI_LINEAGE_INVALID"
            tile_map = envelope.get("input_summary", {}).get("tile_variant_map", [])
            if not isinstance(tile_map, list) or not tile_map:
                return "failed", count, "AI_LINEAGE_INVALID"
            for item in tile_map:
                if not isinstance(item, dict) or not isinstance(item.get("variant_id"), str):
                    return "failed", count, "AI_LINEAGE_INVALID"
                request_by_variant.setdefault(item["variant_id"], []).append(request)
            tile_variants = {str(item["variant_id"]) for item in tile_map}
            envelope_export_id = envelope.get("export_id")
            if not isinstance(envelope_export_id, str):
                return "failed", count, "AI_LINEAGE_INVALID"
            if envelope_export_id == current_export_id:
                # New refresh batches are target-only.  A mixed target/non-
                # target batch would have no single D-045 lineage proof.
                if tile_variants.intersection(refresh_targets) and not tile_variants.issubset(refresh_targets):
                    return "failed", count, "AI_LINEAGE_INVALID"
            elif (
                snapshot.banner_refresh_provenance is None
                or envelope_export_id != base_export_id
                or tile_variants.intersection(refresh_targets)
                or not self._historical_provider_rows_preserved(snapshot, request, tile_variants)
            ):
                return "failed", count, "AI_LINEAGE_INVALID"
            if not self._provider_request_matches_current_input(
                snapshot,
                request,
                tile_map,
                provider,
                export_id=envelope_export_id,
            ):
                return "failed", count, "AI_LINEAGE_INVALID"
            if not _provider_artifact_matches_request(snapshot, request, tile_map, artifacts_by_variant):
                return "failed", count, "AI_LINEAGE_INVALID"

        if not successful_requests:
            return "failed", 0, "AI_LINEAGE_INVALID"
        for variant in eligible:
            variant_id = str(variant.get("variant_id"))
            verified = [row for row in verified_by_variant.get(variant_id, []) if row.get("source", {}).get("verified") is True]
            llm_annotations = [row for row in verified if row.get("source", {}).get("type") == "llm"]
            if not llm_annotations:
                continue
            for annotation in llm_annotations:
                source = annotation.get("source", {})
                if source.get("model_id") != provider.get("model_id") or source.get("prompt_version") != provider.get("prompt_version") or source.get("wire_schema_id") != "annotation-batch-output.v1" or annotation.get("subject_id") != variant_id:
                    return "failed", count, "AI_LINEAGE_INVALID"
                if not request_by_variant.get(variant_id):
                    return "failed", count, "AI_LINEAGE_INVALID"
        return "passed", count, None

    def _historical_provider_rows_preserved(
        self,
        snapshot: Snapshot,
        request: Mapping[str, Any],
        tile_variants: set[str],
    ) -> bool:
        """Allow only the pre-refresh job row for a historical request."""

        input_sha256 = request.get("input_sha256")
        matches = [
            job
            for job in snapshot.ai_jobs
            if job.get("input_signature") == input_sha256
            and set(job.get("variant_ids", [])) == tile_variants
        ]
        if len(matches) != 1:
            return False
        logical_key = matches[0].get("logical_key")
        return isinstance(logical_key, str) and not logical_key.startswith("banner_refresh_")

    def _provider_request_matches_current_input(
        self,
        snapshot: Snapshot,
        request: Mapping[str, Any],
        tile_map: list[dict[str, Any]],
        provider: Mapping[str, Any],
        *,
        export_id: str | None = None,
    ) -> bool:
        workspace = self.data_root.workspace_dir(str(snapshot.run["minecraft_version"]), str(snapshot.run["run_id"]))
        input_export_id = export_id or str(snapshot.import_row["export_id"])
        variants = {str(row.get("variant_id")): row for row in snapshot.variants}
        features = {str(row.get("variant_id")): row.get("feature", {}) for row in snapshot.features}
        images: list[tuple[str, bytes]] = []
        metadata: dict[str, Any] = {}
        feature_hash_values: dict[str, str] = {}
        ordered_tile_map = sorted(tile_map, key=lambda value: str(value.get("tile_id", "")).encode("utf-8"))
        if len({str(item.get("variant_id")) for item in ordered_tile_map}) != len(ordered_tile_map):
            return False
        if any(item.get("tile_id") != f"T{index + 1:02d}" for index, item in enumerate(ordered_tile_map)):
            return False
        if [str(item.get("variant_id")) for item in ordered_tile_map] != sorted((str(item.get("variant_id")) for item in ordered_tile_map), key=lambda value: value.encode("utf-8")):
            return False
        for item in sorted(tile_map, key=lambda value: str(value.get("tile_id", "")).encode("utf-8")):
            variant_id = str(item.get("variant_id"))
            variant = variants.get(variant_id)
            feature = features.get(variant_id)
            if variant is None or not isinstance(feature, dict):
                return False
            try:
                render = variant["render"]
                image = _read_regular(workspace / str(render["preview_path"]), self.data_root.root)
                machine = safe_machine_metadata(variant, feature)
            except (KeyError, TypeError, ReleaseBuildFailure):
                return False
            if item.get("image_sha256") != sha256_bytes(image) or item.get("machine_metadata_sha256") != sha256_json(machine):
                return False
            images.append((variant_id, image))
            metadata[variant_id] = machine
            feature_hash_values[variant_id] = str(next((row.get("output_hash") for row in snapshot.features if row.get("variant_id") == variant_id), ""))
        sheet = make_contact_sheet(images)
        machine_hash = sha256_json(metadata)
        feature_hash = sha256_json(feature_hash_values)
        current_tile_map = [
            {
                "tile_id": item["tile_id"],
                "variant_id": item["variant_id"],
                "image_sha256": sha256_bytes(images[index][1]),
                "machine_metadata_sha256": sha256_json(metadata[str(item["variant_id"])]),
            }
            for index, item in enumerate(ordered_tile_map)
        ]
        if current_tile_map != ordered_tile_map:
            return False
        try:
            expected_cache = build_cache_key(
                {
                    "adapter": provider["adapter"],
                    "image_hash": sheet.image_sha256,
                    "machine_metadata_hash": machine_hash,
                    "prompt_version": provider["prompt_version"],
                    "model_id": provider["model_id"],
                    "schema_version": "annotation-batch-output.v1",
                    "base_url_stable_id": provider["base_url_stable_id"],
                    "stage": "offline_annotation",
                },
                context={"preview_hash": sheet.image_sha256, "feature_hash": feature_hash},
            )
        except Exception:
            return False
        if request.get("cache_key") != expected_cache:
            return False
        source_images = {
            str(item["tile_id"]): image
            for item, (_, image) in zip(ordered_tile_map, images)
        }
        prompt = safe_prompt(
            [metadata[str(item["variant_id"])] for item in ordered_tile_map],
            ordered_tile_map,
            prompt_version=provider["prompt_version"],
        )
        source_hashes = {
            tile_id: sha256_bytes(image)
            for tile_id, image in sorted(source_images.items(), key=lambda pair: pair[0].encode("utf-8"))
        }
        expected_input_material = {
            "stage": "offline_annotation",
            "contact_sheet_sha256": sheet.image_sha256,
            "tile_map": ordered_tile_map,
            "machine_metadata_hash": machine_hash,
            "prompt_sha256": sha256_bytes(prompt.encode("utf-8")),
            "source_image_hashes": source_hashes,
            "feature_hash": feature_hash,
            "export_id": input_export_id,
            "profile_id": provider.get("profile_id"),
            "adapter": provider.get("adapter"),
            "model_id": provider.get("model_id"),
            "base_url_stable_id": provider.get("base_url_stable_id"),
            "prompt_version": provider.get("prompt_version"),
            "wire_schema_id": "annotation-batch-output.v1",
            "wire_format_name": "annotation_batch_output_v1",
        }
        expected_inputs = {sha256_json(expected_input_material)}
        for job in snapshot.ai_jobs:
            if set(job.get("variant_ids", [])) == {str(item.get("variant_id")) for item in ordered_tile_map}:
                material = dict(expected_input_material)
                material["retry_nonce"] = job.get("retry_nonce")
                expected_inputs.add(sha256_json(material))
        return str(request.get("input_sha256")) in expected_inputs and any(
            job.get("input_signature") == request.get("input_sha256")
            and set(job.get("variant_ids", [])) == {str(item.get("variant_id")) for item in ordered_tile_map}
            for job in snapshot.ai_jobs
        )

    def _gate_overrides(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        block_ids = {str(row.get("block_id")) for row in snapshot.blocks}
        state_ids = {str(row.get("state_id")) for row in snapshot.states}
        variant_ids = {str(row.get("variant_id")) for row in snapshot.variants}
        failures = {str(row.get("failure_id")): row for row in snapshot.source_records["failures.jsonl"] if row.get("export_id") == snapshot.import_row["export_id"]}
        variants_by_id = {str(row.get("variant_id")): row for row in snapshot.variants}
        count = 0
        for record in snapshot.overrides:
            count += 1
            schema_id = record.get("schema_version")
            if schema_id not in {"manual-override.v1", "skip-review.v1", "qualification-review.v1"} or _has_sensitive_text(record) or not _record_ok(str(schema_id), record, self.repo_root):
                return "failed", count, "OVERRIDE_REFERENCE_INVALID"
            target_id = str(record.get("target_id", record.get("scope", {}).get("variant_id", "")))
            if target_id not in block_ids | state_ids | variant_ids:
                return "failed", count, "OVERRIDE_REFERENCE_INVALID"
            if schema_id == "skip-review.v1" and str(record.get("machine_failure_ref")) not in failures:
                return "failed", count, "OVERRIDE_REFERENCE_INVALID"
            if schema_id == "qualification-review.v1" and target_id not in variant_ids:
                return "failed", count, "OVERRIDE_REFERENCE_INVALID"
            if target_id in variants_by_id:
                refs = variants_by_id[target_id].get("override_refs", [])
                if schema_id == "manual-override.v1" and record.get("override_id") not in refs:
                    return "failed", count, "OVERRIDE_REFERENCE_INVALID"
                if schema_id == "skip-review.v1" and record.get("review_id") not in refs:
                    return "failed", count, "OVERRIDE_REFERENCE_INVALID"
                if schema_id == "qualification-review.v1" and record.get("review_id") not in variants_by_id[target_id].get("qualification_review_refs", []):
                    return "failed", count, "OVERRIDE_REFERENCE_INVALID"
        return "passed", count, None

    def _gate_ids(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        blocks = {str(row.get("block_id")) for row in snapshot.blocks}
        states = {str(row.get("state_id")) for row in snapshot.states}
        variants = {str(row.get("variant_id")) for row in snapshot.variants}
        states_by_id = {str(row.get("state_id")): row for row in snapshot.states}
        annotations = {str(row.get("annotation_id")): row for row in snapshot.annotations}
        for variant in snapshot.variants:
            if variant.get("variant_id") != variant.get("block_id") or variant.get("block_id") not in blocks or variant.get("canonical_state_id") not in states:
                return "failed", len(variants), "FALSE_ID_REFERENCE"
            if any(ref not in annotations for ref in variant.get("annotation_refs", [])):
                return "failed", len(variants), "FALSE_ID_REFERENCE"
            if any(ref not in states or states_by_id.get(ref, {}).get("block_id") != variant.get("block_id") for ref in variant.get("represented_state_ids", [])):
                return "failed", len(variants), "FALSE_ID_REFERENCE"
        for state in snapshot.states:
            if state.get("block_id") not in blocks or any(ref not in variants for ref in state.get("variant_ids", [])):
                return "failed", len(states), "FALSE_ID_REFERENCE"
        if any(str(annotation.get("subject_id")) not in blocks | variants for annotation in snapshot.annotations):
            return "failed", len(annotations), "FALSE_ID_REFERENCE"
        return "passed", len(variants), None

    def _gate_reviews(self, snapshot: Snapshot) -> tuple[str, int, str | None]:
        open_reviews = [row for row in snapshot.reviews if row.get("status") == "open" and row.get("severity") in {"high", "normal"}]
        return ("passed", len(snapshot.reviews), None) if not open_reviews else ("failed", len(open_reviews), "OPEN_REVIEW_EXISTS")

    def _gate_fts(self, snapshot: Snapshot, database: WorkspaceDatabase) -> tuple[str, int, str | None]:
        try:
            expected = WorkspaceQueryService(database).expected_documents(database.connection)
            actual = {
                str(row["document_id"]): (str(row["block_id"]), str(row["content"]), str(row["normalized_content"]))
                for row in database.connection.execute("SELECT document_id,block_id,content,normalized_content FROM search_documents")
            }
            wanted = {key: (value[1], value[2], value[3]) for key, value in expected.items()}
            if actual != wanted:
                return "failed", len(actual), "FTS_NOT_READY"
            if database.fts_mode == "trigram":
                expected_fts = {value[1]: value[3] for value in expected.values()}
                actual_fts = {str(row["block_id"]): str(row["content"]) for row in database.connection.execute("SELECT block_id,content FROM fts_documents")}
                if actual_fts != expected_fts:
                    return "failed", len(actual_fts), "FTS_NOT_READY"
            return "passed", len(actual), None
        except Exception:
            return "failed", 0, "FTS_NOT_READY"

    def _build_staging(self, staging: Path, snapshot: Snapshot, release_id: str, state: Mapping[str, Any], check_report: Mapping[str, Any]) -> None:
        workspace = self.data_root.workspace_dir(str(snapshot.run["minecraft_version"]), str(snapshot.run["run_id"]))
        previews = staging / "previews"
        manual_records = _manual_package(snapshot, release_id, self.repo_root)
        manual_bytes = _canonical_bytes(manual_records)
        _write_file(staging / "manual-overrides.json", manual_bytes, root=staging)

        fts_mode = self._write_index(staging / "index.sqlite3", snapshot, release_id)
        selected_refs: dict[str, dict[str, str]] = {}
        for variant in _stable_rows(snapshot.variants, "variant_id"):
            variant_id = str(variant["variant_id"])
            suffix = variant_id.removeprefix("minecraft:")
            if not suffix or not re.fullmatch(r"[a-z0-9_./-]+", suffix):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
            safe_relative_posix_ref(f"previews/minecraft/{suffix}/preview.png")
            render = variant.get("render")
            if not isinstance(render, dict):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
            destination_root = previews / "minecraft" / suffix
            _ensure_dir(destination_root, staging)
            source_values = (("preview_path", "image_sha256", "preview.png"), ("mask_path", "mask_sha256", "mask.png"), ("render_metadata_path", "render_metadata_sha256", "render.json"))
            dest_refs: dict[str, str] = {}
            for source_key, hash_key, filename in source_values:
                source_ref = str(render.get(source_key, ""))
                safe_relative_posix_ref(source_ref)
                expected_source_ref = f"renders/minecraft/{suffix}/{filename}"
                if source_ref != expected_source_ref:
                    raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
                source = workspace / source_ref
                payload = _read_regular(source, self.data_root.root)
                if filename == "render.json":
                    metadata = _load_json_bytes(payload)
                    if not _record_ok("render-metadata.v1", metadata, self.repo_root) or metadata.get("variant_id") != variant_id or _hash_json(metadata) != render.get(hash_key):
                        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
                elif sha256_bytes(payload) != render.get(hash_key):
                    raise ReleaseBuildFailure("RELEASE_CHECK_STALE")
                destination = destination_root / filename
                _write_file(destination, payload, root=staging)
                dest_refs[source_key] = f"previews/minecraft/{suffix}/{filename}"
            selected_refs[variant_id] = dest_refs

        provider = _provider_snapshot(snapshot.config)
        if provider is None:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        functional_artifacts: dict[str, str] = {
            "index.sqlite3": _hash_file(staging / "index.sqlite3", staging),
            "manual-overrides.json": sha256_bytes(manual_bytes),
        }
        for value in selected_refs.values():
            for ref in value.values():
                functional_artifacts[ref] = _hash_file(staging / ref, staging)
        schemas_bytes = _schema_inventory(self.repo_root, snapshot.schema_ids)
        functional_inputs: dict[str, str] = {
            "snapshot/fingerprint": snapshot.fingerprint,
            "source_export/manifest.json": snapshot.import_row["manifest_sha256"],
            "source_export/checksums.anchor": snapshot.import_row["checksum_sha256"],
            "source_export/checksums.inventory": snapshot.source_file_hashes.get("export/checksums.sha256", ""),
            "toolchain/lock": snapshot.run.get("toolchain_lock_sha256", _toolchain_lock_hash(self.repo_root)),
            "provider/snapshot": _hash_json(provider),
            "provider/adapter": _hash_json({"adapter": provider["adapter"]}),
            "schema_inventory": sha256_bytes(schemas_bytes),
            "release_index/sql": snapshot.release_index_sql_sha256,
        }
        if snapshot.banner_refresh_provenance is not None:
            functional_inputs["source_import/banner_refresh_provenance"] = _hash_json(snapshot.banner_refresh_provenance)
        for key, value in snapshot.source_file_hashes.items():
            relative = key.removeprefix("export/")
            if relative == "manifest.json" or relative == "checksums.sha256":
                continue
            functional_inputs[f"source_export/{relative}"] = value
        for entry in snapshot.export_checksum_inventory:
            if entry["path"] not in {"manifest.json", "checksums.sha256"}:
                functional_inputs[f"source_export/inventory/{entry['path']}"] = "sha256:" + entry["sha256"]
        for request in snapshot.provider_requests:
            base = f"provider_requests/{request['request_id']}/{request['attempt']}"
            functional_inputs[f"{base}/envelope"] = _hash_json(request.get("envelope", {}))
            functional_inputs[f"{base}/input"] = str(request.get("input_sha256"))
            functional_inputs[f"{base}/cache_key"] = sha256_json({"cache_key": request.get("cache_key")})
            if request.get("validated_artifact_sha256") is not None:
                functional_inputs[f"{base}/validated_artifact"] = str(request["validated_artifact_sha256"])
        for artifact in snapshot.artifacts:
            actual = artifact.get("actual_sha256")
            ref = str(artifact.get("relative_ref", ""))
            if isinstance(actual, str) and _HASH_RE.fullmatch(actual):
                functional_inputs[f"workspace_artifacts/{ref}"] = actual
                if artifact.get("kind") == "ai_annotation":
                    functional_inputs[f"provider_artifacts/{ref}"] = actual
        built_at = _now()
        release_report = _release_quality_report(
            state,
            release_id,
            built_at,
            check_report,
            hash_observation_count=len(functional_inputs) + len(functional_artifacts),
            release_evidence=tuple(functional_artifacts),
        )
        quality_bytes = _canonical_bytes(release_report)
        _write_file(staging / "quality_report.json", quality_bytes, root=staging)
        quality_hash = sha256_bytes(quality_bytes)
        manifest = {
            "schema_version": "release-manifest.v1",
            "release_id": release_id,
            "minecraft_version": snapshot.run["minecraft_version"],
            "source_export_id": snapshot.import_row["export_id"],
            "source_export_manifest_sha256": snapshot.import_row["manifest_sha256"],
            "toolchain_lock_sha256": _toolchain_lock_hash(self.repo_root),
            "schemas_inventory_path": "schemas.sha256",
            "provider_snapshot": provider,
            "functional_inputs": functional_inputs,
            "functional_artifacts": functional_artifacts,
            "quality_report_path": "quality_report.json",
            "quality_report_sha256": quality_hash,
            "fts_mode": fts_mode,
        }
        if not _record_ok("release-manifest.v1", manifest, self.repo_root):
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        _write_file(staging / "manifest.json", _canonical_bytes(manifest), root=staging)
        release_json = {
            "schema_version": "release.v1",
            "release_id": release_id,
            "minecraft_version": snapshot.run["minecraft_version"],
            "built_at": built_at,
            "source_export_id": snapshot.import_row["export_id"],
            "manifest_sha256": sha256_bytes(_canonical_bytes(manifest)),
            "record_schema_versions": {
                "block": "block-record.v1",
                "state": "state-record.v1",
                "variant": "visual-variant-record.v1",
                "annotation": "annotation-record.v1",
                "manual_override": "manual-override.v1",
                "skip_review": "skip-review.v1",
                "qualification_review": "qualification-review.v1",
            },
            "quality_report_path": "quality_report.json",
            "immutable": True,
        }
        if not _record_ok("release.v1", release_json, self.repo_root):
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        _write_file(staging / "release.json", _canonical_bytes(release_json), root=staging)
        _write_file(staging / "schemas.sha256", schemas_bytes, root=staging)
        checksums = _checksum_bytes(staging)
        _write_file(staging / "checksums.sha256", checksums, root=staging)
        _validate_checksums(staging, root_for_components=staging)

    def _schema_ids_for_snapshot(self, snapshot: Snapshot) -> tuple[str, ...]:
        return snapshot.schema_ids

    def _write_index(self, path: Path, snapshot: Snapshot, release_id: str) -> str:
        sql, _ = packaged_release_index_schema()
        _ensure_dir(path.parent, path.parent)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path)
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(sql.decode("utf-8"))
            connection.execute("INSERT INTO schema_meta(format_version) VALUES (1)")
            blocks = {str(row["block_id"]): row for row in snapshot.blocks}
            states = {str(row["state_id"]): row for row in snapshot.states}
            for block_id in sorted(blocks, key=lambda value: value.encode("utf-8")):
                block = blocks[block_id]
                names = block.get("official_names", {})
                connection.execute("INSERT INTO blocks(block_id,minecraft_version,translation_key,name_zh,name_en,default_state_id,machine_facts_json) VALUES (?,?,?,?,?,?,?)", (block_id, snapshot.run["minecraft_version"], block.get("translation_key"), names.get("zh_cn"), names.get("en_us"), block["default_state_id"], canonical_json(block.get("machine_facts", {}))))
            for state_id in sorted(states, key=lambda value: value.encode("utf-8")):
                state = states[state_id]
                connection.execute("INSERT INTO states(state_id,block_id,properties_json,is_default) VALUES (?,?,?,?)", (state_id, state["block_id"], canonical_json(state.get("properties", {})), int(bool(state.get("is_default")))))

            annotations_by_subject: dict[str, list[dict[str, Any]]] = {}
            for annotation in snapshot.annotations:
                if annotation.get("source", {}).get("verified") is True:
                    annotations_by_subject.setdefault(str(annotation.get("subject_id")), []).append(annotation)
            variants_by_id = {str(row["variant_id"]): row for row in snapshot.variants}
            for variant_id in sorted(variants_by_id, key=lambda value: value.encode("utf-8")):
                variant = variants_by_id[variant_id]
                suffix = variant_id.removeprefix("minecraft:")
                connection.execute("INSERT INTO visual_variants(variant_id,block_id,canonical_state_id,represented_state_ids_json,preview_path,mask_path,render_metadata_path,image_sha256,mask_sha256,render_metadata_sha256,candidate_qualification,warnings_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (variant_id, variant["block_id"], variant["canonical_state_id"], canonical_json(variant["represented_state_ids"]), f"previews/minecraft/{suffix}/preview.png", f"previews/minecraft/{suffix}/mask.png", f"previews/minecraft/{suffix}/render.json", variant["render"]["image_sha256"], variant["render"]["mask_sha256"], variant["render"]["render_metadata_sha256"], variant["candidate_qualification"], canonical_json(variant.get("warnings", []))))
                semantic = _effective_semantics_from_rows(annotations_by_subject.get(variant_id, []) + annotations_by_subject.get(str(variant["block_id"]), []), snapshot.overrides, variant_id)
                connection.execute("INSERT INTO annotations(variant_id,semantic_json) VALUES (?,?)", (variant_id, canonical_json(semantic)))

            fts_mode = "trigram"
            try:
                connection.execute("CREATE VIRTUAL TABLE search_fts USING fts5(variant_id UNINDEXED, normalized_text, tokenize='trigram')")
            except sqlite3.OperationalError:
                fts_mode = "normalized_like"
                connection.execute("CREATE TABLE search_text (variant_id TEXT PRIMARY KEY, normalized_text TEXT NOT NULL)")
                connection.execute("CREATE INDEX search_text_normalized_idx ON search_text(normalized_text)")
            for variant_id, variant in sorted(variants_by_id.items(), key=lambda item: item[0].encode("utf-8")):
                if variant.get("candidate_qualification") not in {"eligible", "conditional"}:
                    continue
                annotation = annotations_by_subject.get(variant_id, []) + annotations_by_subject.get(str(variant["block_id"]), [])
                semantic = _effective_semantics_from_rows(annotation, snapshot.overrides, variant_id)
                block = blocks[str(variant["block_id"])]
                text_parts: list[str] = []
                names = block.get("official_names", {})
                text_parts.extend(value for value in (names.get("zh_cn"), names.get("en_us")) if isinstance(value, str))
                facts = variant.get("machine_facts", {})
                text_parts.extend(str(value) for value in facts.get("machine_tags", []))
                text_parts.extend(str(value) for value in facts.get("geometry", {}).get("geometry_classes", []))
                for key in SEMANTIC_LIST_FIELDS + SEMANTIC_SCALAR_FIELDS:
                    value = semantic.get(key)
                    text_parts.extend(str(item) for item in value) if isinstance(value, list) else text_parts.append(value) if isinstance(value, str) else None
                normalized = normalize_text(" ".join(sorted(set(text_parts), key=lambda value: value.encode("utf-8"))))
                if fts_mode == "trigram":
                    connection.execute("INSERT INTO search_fts(variant_id,normalized_text) VALUES (?,?)", (variant_id, normalized))
                else:
                    connection.execute("INSERT INTO search_text(variant_id,normalized_text) VALUES (?,?)", (variant_id, normalized))
            connection.commit()
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        except ReleaseBuildFailure:
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass
            raise
        except sqlite3.Error as exc:
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc
        finally:
            try:
                if connection is not None:
                    connection.close()
            except Exception:
                pass
        for sidecar in (Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
            if sidecar.exists() or sidecar.is_symlink():
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        _lstat(path, directory=False)
        return fts_mode

    def _validate_final(self, final: Path, release_id: str, snapshot: Snapshot, state: Mapping[str, Any], *, root_for_components: Path, after_commit: bool = True) -> None:
        _lstat(final, directory=True)
        expected_top = {"release.json", "manifest.json", "index.sqlite3", "previews", "quality_report.json", "manual-overrides.json", "schemas.sha256", "checksums.sha256"}
        entries = list(final.iterdir())
        for entry in entries:
            _lstat(entry, directory=None)
        actual_top = {entry.name for entry in entries}
        if actual_top != expected_top:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
        _validate_checksums(final, root_for_components=root_for_components)
        release_json, _ = _json_bytes(final / "release.json", final)
        manifest, _ = _json_bytes(final / "manifest.json", final)
        report, _ = _json_bytes(final / "quality_report.json", final)
        if not _record_ok("release.v1", release_json, self.repo_root) or not _record_ok("release-manifest.v1", manifest, self.repo_root) or not _valid_release_report(report):
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
        if release_json.get("release_id") != release_id or release_json.get("manifest_sha256") != _hash_file(final / "manifest.json", final) or manifest.get("release_id") != release_id or manifest.get("minecraft_version") != state.get("minecraft_version") or manifest.get("quality_report_sha256") != _hash_file(final / "quality_report.json", final) or report.get("release_id") != release_id or report.get("release_build_id") != state.get("release_build_id") or report.get("run_id") != state.get("run_id") or report.get("minecraft_version") != state.get("minecraft_version") or report.get("snapshot_fingerprint") != state.get("snapshot_fingerprint"):
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
        if report["items"][-1]["observed_count"] != len(manifest.get("functional_inputs", {})) + len(manifest.get("functional_artifacts", {})):
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
        for item in report["items"]:
            for ref in item["evidence"]:
                evidence = final / ref
                _safe_release_relative(ref)
                _read_regular(evidence, final)
        expected_schema_bytes = _schema_inventory(self.repo_root, snapshot.schema_ids)
        if _read_regular(final / "schemas.sha256", final) != expected_schema_bytes:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
        self._validate_index_projection(final / "index.sqlite3", snapshot, manifest, after_commit=after_commit)

    def _validate_index_projection(self, path: Path, snapshot: Snapshot, manifest: Mapping[str, Any], *, after_commit: bool) -> None:
        for sidecar in (Path(str(path) + "-wal"), Path(str(path) + "-shm"), Path(str(path) + "-journal")):
            if sidecar.exists() or sidecar.is_symlink():
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            table_names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
            required = {"schema_meta", "blocks", "states", "visual_variants", "annotations"}
            if not required.issubset(table_names) or not ("search_fts" in table_names or "search_text" in table_names):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            if manifest.get("fts_mode") == "trigram" and ("search_fts" not in table_names or "search_text" in table_names):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            if manifest.get("fts_mode") == "normalized_like" and ("search_text" not in table_names or "search_fts" in table_names):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            if connection.execute("SELECT format_version FROM schema_meta").fetchall() != [(1,)]:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            expected_blocks = {
                str(row["block_id"]): (
                    str(row["minecraft_version"]), row.get("translation_key"), row.get("official_names", {}).get("zh_cn"),
                    row.get("official_names", {}).get("en_us"), str(row["default_state_id"]), canonical_json(row.get("machine_facts", {}))
                ) for row in snapshot.blocks
            }
            actual_blocks = {
                str(row[0]): tuple(row[1:]) for row in connection.execute("SELECT block_id,minecraft_version,translation_key,name_zh,name_en,default_state_id,machine_facts_json FROM blocks")
            }
            if actual_blocks != expected_blocks:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            expected_states = {
                str(row["state_id"]): (str(row["block_id"]), canonical_json(row.get("properties", {})), int(bool(row.get("is_default"))))
                for row in snapshot.states
            }
            actual_states = {str(row[0]): tuple(row[1:]) for row in connection.execute("SELECT state_id,block_id,properties_json,is_default FROM states")}
            if actual_states != expected_states:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            expected_variants = {
                str(row["variant_id"]): (
                    str(row["block_id"]), str(row["canonical_state_id"]), canonical_json(row["represented_state_ids"]),
                    f"previews/minecraft/{str(row['variant_id']).removeprefix('minecraft:')}/preview.png",
                    f"previews/minecraft/{str(row['variant_id']).removeprefix('minecraft:')}/mask.png",
                    f"previews/minecraft/{str(row['variant_id']).removeprefix('minecraft:')}/render.json",
                    row["render"]["image_sha256"], row["render"]["mask_sha256"], row["render"]["render_metadata_sha256"],
                    row["candidate_qualification"], canonical_json(row.get("warnings", []))
                ) for row in snapshot.variants
            }
            actual_variants = {str(row[0]): tuple(row[1:]) for row in connection.execute("SELECT variant_id,block_id,canonical_state_id,represented_state_ids_json,preview_path,mask_path,render_metadata_path,image_sha256,mask_sha256,render_metadata_sha256,candidate_qualification,warnings_json FROM visual_variants")}
            if actual_variants != expected_variants:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            annotations_by_subject: dict[str, list[dict[str, Any]]] = {}
            for annotation in snapshot.annotations:
                if annotation.get("source", {}).get("verified") is True:
                    annotations_by_subject.setdefault(str(annotation.get("subject_id")), []).append(annotation)
            expected_annotations = {
                str(row["variant_id"]): canonical_json(_effective_semantics_from_rows(annotations_by_subject.get(str(row["variant_id"]), []) + annotations_by_subject.get(str(row["block_id"]), []), snapshot.overrides, str(row["variant_id"])))
                for row in snapshot.variants
            }
            actual_annotations = {str(row[0]): str(row[1]) for row in connection.execute("SELECT variant_id,semantic_json FROM annotations")}
            if actual_annotations != expected_annotations:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            expected_search = _expected_search_documents(snapshot)
            if manifest.get("fts_mode") == "trigram":
                raw_search_rows = connection.execute("SELECT variant_id,normalized_text FROM search_fts").fetchall()
            else:
                raw_search_rows = connection.execute("SELECT variant_id,normalized_text FROM search_text").fetchall()
            if len(raw_search_rows) != len(expected_search) or len({str(row[0]) for row in raw_search_rows}) != len(raw_search_rows):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
            actual_search = {str(row[0]): str(row[1]) for row in raw_search_rows}
            if actual_search != expected_search:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit)
        except sqlite3.Error as exc:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=after_commit) from exc
        finally:
            connection.close()

    def _mark_stale(self, state: Mapping[str, Any]) -> None:
        updated = dict(state)
        updated["status"] = "stale"
        updated["can_build"] = False
        updated["updated_at"] = _now()
        updated["error_code"] = "RELEASE_CHECK_STALE"
        directory = self.data_root.cache / "release-checks" / str(state["check_id"])
        _atomic_json(directory / "state.json", updated, root=self.data_root.root)

    def _mark_built(self, state: Mapping[str, Any], release_id: str) -> None:
        updated = dict(state)
        if updated.get("status") == "built":
            if updated.get("release_id") != release_id:
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
            return
        updated["status"] = "built"
        updated["can_build"] = True
        updated["release_id"] = release_id
        updated["updated_at"] = _now()
        updated["error_code"] = None
        directory = self.data_root.cache / "release-checks" / str(state["check_id"])
        _atomic_json(directory / "state.json", updated, root=self.data_root.root)


def _valid_check_state(value: Any, check_id: str) -> bool:
    if not isinstance(value, dict) or set(value) != _CHECK_STATE_FIELDS:
        return False
    if value.get("format_version") != 1 or value.get("check_id") != check_id or RELEASE_CHECK_ID_RE.fullmatch(str(value.get("check_id"))) is None or RELEASE_BUILD_ID_RE.fullmatch(str(value.get("release_build_id"))) is None:
        return False
    if not isinstance(value.get("run_id"), str) or not isinstance(value.get("minecraft_version"), str) or not isinstance(value.get("source_export_id"), str) or not re.fullmatch(r"^export_[0-9]{8}T[0-9]{6}Z(?:_(?:0[1-9]|[1-9][0-9]))?$", value["source_export_id"]):
        return False
    if value.get("status") not in {"passed", "failed", "stale", "built"} or not isinstance(value.get("can_build"), bool) or not _HASH_RE.fullmatch(str(value.get("snapshot_fingerprint"))) or not _HASH_RE.fullmatch(str(value.get("quality_report_sha256"))):
        return False
    if value["status"] in {"failed", "stale"} and value["can_build"] is not False:
        return False
    if value["status"] == "built" and value["can_build"] is not True:
        return False
    if value["status"] != "built" and value.get("release_id") is not None:
        return False
    if value["status"] == "built" and value.get("release_id") is None:
        return False
    if value.get("release_id") is not None and RELEASE_ID_RE.fullmatch(str(value.get("release_id"))) is None:
        return False
    return all(isinstance(value.get(key), str) and _TIMESTAMP_RE.fullmatch(value[key]) for key in ("created_at", "updated_at")) and (value.get("error_code") is None or re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", str(value.get("error_code"))) is not None)


def _valid_check_report(value: Any, check_id: str) -> bool:
    if not isinstance(value, dict) or set(value) != _CHECK_REPORT_FIELDS or value.get("format_version") != 1 or value.get("report_kind") != "candidate_check" or value.get("check_id") != check_id:
        return False
    if value.get("status") not in {"buildable", "blocked"} or not isinstance(value.get("can_build"), bool) or not _HASH_RE.fullmatch(str(value.get("snapshot_fingerprint"))) or RELEASE_BUILD_ID_RE.fullmatch(str(value.get("release_build_id"))) is None or not isinstance(value.get("run_id"), str) or not isinstance(value.get("minecraft_version"), str):
        return False
    if not isinstance(value.get("items"), list) or [item.get("code") for item in value["items"] if isinstance(item, dict)] != list(CHECK_CODES):
        return False
    for item in value["items"]:
        if not isinstance(item, dict) or set(item) != {"code", "status", "blocking", "observed_count", "error_code", "evidence"}:
            return False
        if item["status"] not in {"passed", "failed", "not_run"} or not isinstance(item["blocking"], bool) or isinstance(item["observed_count"], bool) or not isinstance(item["observed_count"], int) or item["observed_count"] < 0 or (item["error_code"] is not None and re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", str(item["error_code"])) is None):
            return False
        if not isinstance(item["evidence"], list) or not item["evidence"] or any(not isinstance(ref, str) or "://" in ref or _invalid_report_ref(ref) for ref in item["evidence"]):
            return False
    if value["items"][11]["status"] != "not_run":
        return False
    if value["status"] == "buildable" and any(item["status"] != "passed" for item in value["items"][:11]):
        return False
    if value["status"] == "blocked" and not any(item["status"] == "failed" for item in value["items"][:11]):
        return False
    if value["status"] == "buildable" and value["can_build"] is not True:
        return False
    if value["status"] == "blocked" and value["can_build"] is not False:
        return False
    return all(isinstance(value.get(key), str) and _TIMESTAMP_RE.fullmatch(value[key]) for key in ("created_at", "updated_at"))


def _invalid_report_ref(value: str) -> bool:
    try:
        safe_relative_posix_ref(value)
    except ValueError:
        return True
    return False


def _ordered_check_report(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value[key] for key in ("format_version", "report_kind", "check_id", "release_build_id", "run_id", "minecraft_version", "status", "can_build", "snapshot_fingerprint", "items", "created_at", "updated_at")}


def _valid_release_report(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != _RELEASE_REPORT_FIELDS or value.get("format_version") != 1 or value.get("report_kind") != "release" or value.get("status") != "passed":
        return False
    if not isinstance(value.get("release_id"), str) or RELEASE_ID_RE.fullmatch(value["release_id"]) is None or not isinstance(value.get("release_build_id"), str) or RELEASE_BUILD_ID_RE.fullmatch(value["release_build_id"]) is None or not isinstance(value.get("run_id"), str) or not isinstance(value.get("minecraft_version"), str) or not _HASH_RE.fullmatch(str(value.get("snapshot_fingerprint"))) or not _TIMESTAMP_RE.fullmatch(str(value.get("built_at"))):
        return False
    items = value.get("items")
    if not isinstance(items, list) or [item.get("code") for item in items if isinstance(item, dict)] != list(CHECK_CODES):
        return False
    return all(
        isinstance(item, dict)
        and set(item) == {"code", "status", "blocking", "observed_count", "error_code", "evidence"}
        and item["status"] == "passed"
        and isinstance(item["blocking"], bool)
        and isinstance(item["observed_count"], int)
        and not isinstance(item["observed_count"], bool)
        and item["observed_count"] >= 0
        and (item["error_code"] is None or re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", str(item["error_code"])) is not None)
        and isinstance(item["evidence"], list)
        and item["evidence"]
        and all(isinstance(ref, str) and not _invalid_report_ref(ref) for ref in item["evidence"])
        for item in items
    )


def _release_quality_report(
    state: Mapping[str, Any],
    release_id: str,
    built_at: str,
    check_report: Mapping[str, Any],
    *,
    hash_observation_count: int,
    release_evidence: tuple[str, ...],
) -> dict[str, Any]:
    evidence_by_code = {
        "REGISTRY_COVERAGE_100": ("index.sqlite3",),
        "BLOCK_VARIANT_OR_AUDITED_SKIP": ("manual-overrides.json", "index.sqlite3"),
        "EXCLUDED_QUALIFICATION_REVIEW_VALID": ("manual-overrides.json",),
        "IMAGE_READABLE_AND_HASHED": tuple(ref for ref in release_evidence if ref.startswith("previews/") and ref.endswith("/preview.png"))[:1] or ("index.sqlite3",),
        "LEGAL_STATE_VALID": ("index.sqlite3",),
        "MACHINE_SCHEMA_VALID": ("manifest.json",),
        "AI_SCHEMA_VALID": ("manifest.json",),
        "OVERRIDE_REFERENCES_VALID": ("manual-overrides.json",),
        "NO_FALSE_IDS": ("index.sqlite3",),
        "HIGH_REVIEW_ZERO": ("quality_report.json",),
        "FTS_READY": ("index.sqlite3",),
        "RELEASE_HASH_MANIFEST": ("manifest.json", "checksums.sha256"),
    }
    check_items = {str(item.get("code")): item for item in check_report.get("items", []) if isinstance(item, dict)}
    items: list[dict[str, Any]] = []
    for code in CHECK_CODES[:11]:
        source = check_items.get(code)
        if not isinstance(source, dict) or source.get("status") != "passed":
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        items.append({
            "code": code,
            "status": "passed",
            "blocking": bool(source.get("blocking")),
            "observed_count": int(source.get("observed_count", 0)),
            "error_code": None,
            "evidence": list(evidence_by_code[code]),
        })
    items.append({
        "code": "RELEASE_HASH_MANIFEST",
        "status": "passed",
        "blocking": True,
        "observed_count": max(1, int(hash_observation_count)),
        "error_code": None,
        "evidence": list(evidence_by_code["RELEASE_HASH_MANIFEST"]),
    })
    return {
        "format_version": 1,
        "report_kind": "release",
        "release_id": release_id,
        "release_build_id": state["release_build_id"],
        "run_id": state["run_id"],
        "minecraft_version": state["minecraft_version"],
        "status": "passed",
        "snapshot_fingerprint": state["snapshot_fingerprint"],
        "items": items,
        "built_at": built_at,
    }


def _manual_package(snapshot: Snapshot, release_id: str, repo_root: Path) -> dict[str, Any]:
    values: dict[str, list[dict[str, Any]]] = {"manual_overrides": [], "skip_reviews": [], "qualification_reviews": []}
    seen: dict[str, set[str]] = {key: set() for key in values}
    block_ids = {str(row.get("block_id")) for row in snapshot.blocks}
    state_ids = {str(row.get("state_id")) for row in snapshot.states}
    variant_ids = {str(row.get("variant_id")) for row in snapshot.variants}
    failure_ids = {str(row.get("failure_id")) for row in snapshot.source_records["failures.jsonl"] if row.get("export_id") == snapshot.import_row["export_id"]}
    variants_by_id = {str(row.get("variant_id")): row for row in snapshot.variants}
    failures_by_id = {str(row.get("failure_id")): row for row in snapshot.source_records["failures.jsonl"] if row.get("export_id") == snapshot.import_row["export_id"]}
    for record in snapshot.overrides:
        schema_id = record.get("schema_version")
        if schema_id == "manual-override.v1":
            key, id_key = "manual_overrides", "override_id"
        elif schema_id == "skip-review.v1":
            key, id_key = "skip_reviews", "review_id"
        elif schema_id == "qualification-review.v1":
            key, id_key = "qualification_reviews", "review_id"
        else:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        if not _record_ok(str(schema_id), record, repo_root) or _has_sensitive_text(record) or str(record.get("minecraft_version")) != str(snapshot.run["minecraft_version"]):
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        identifier = str(record.get(id_key))
        if identifier in seen[key]:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        seen[key].add(identifier)
        target = str(record.get("target_id", record.get("scope", {}).get("variant_id", "")))
        if target not in block_ids | state_ids | variant_ids:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        if schema_id == "skip-review.v1" and str(record.get("machine_failure_ref")) not in failure_ids:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        if schema_id == "skip-review.v1" and str(record.get("machine_failure_ref")) not in failures_by_id:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        if target in variants_by_id:
            if schema_id == "manual-override.v1" and record.get("override_id") not in variants_by_id[target].get("override_refs", []):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
            if schema_id == "skip-review.v1" and record.get("review_id") not in variants_by_id[target].get("override_refs", []):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
            if schema_id == "qualification-review.v1" and record.get("review_id") not in variants_by_id[target].get("qualification_review_refs", []):
                raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        values[key].append(record)
    for key, id_key in (("manual_overrides", "override_id"), ("skip_reviews", "review_id"), ("qualification_reviews", "review_id")):
        values[key].sort(key=lambda record: str(record[id_key]).encode("utf-8"))
    return {"format_version": 1, "release_id": release_id, "version": snapshot.run["minecraft_version"], **values}


def _schema_inventory(repo_root: Path, schema_ids: tuple[str, ...]) -> bytes:
    return "".join(
        f"{entry['sha256']}  {entry['schema_id']}  {entry['path']}\n"
        for entry in _schema_inventory_entries(repo_root, schema_ids)
    ).encode("utf-8")


def _schema_inventory_entries(repo_root: Path, schema_ids: tuple[str, ...]) -> list[dict[str, str]]:
    from .schema import schema_namespace

    entries: list[dict[str, str]] = []
    for schema_id in sorted(set(schema_ids), key=lambda value: value.encode("utf-8")):
        namespace = schema_namespace(schema_id)
        path = repo_root / "schemas" / namespace / f"{schema_id}.json"
        if not path.is_file() or path.is_symlink():
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
        entries.append({
            "schema_id": schema_id,
            "path": f"schemas/{namespace}/{schema_id}.json",
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return entries


def _load_json_bytes(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED") from exc


def _hash_file(path: Path, root: Path) -> str:
    return sha256_bytes(_read_regular(path, root))


def _checksum_bytes(root: Path) -> bytes:
    files: list[tuple[str, Path]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.name == "checksums.sha256":
            continue
        if path.is_dir():
            _lstat(path, directory=True)
            continue
        _lstat(path, directory=False)
        safe_relative_posix_ref(relative)
        files.append((relative, path))
    files.sort(key=lambda item: item[0].encode("utf-8"))
    return "".join(f"{hashlib.sha256(_read_regular(path, root)).hexdigest()}  {relative}\n" for relative, path in files).encode("utf-8")


def _validate_checksums(root: Path, *, root_for_components: Path | None = None) -> None:
    base = root_for_components or root
    checksum_path = root / "checksums.sha256"
    payload = _read_regular(checksum_path, base)
    expected: list[tuple[str, str]] = []
    try:
        for line in payload.decode("ascii").splitlines(keepends=True):
            if not line.endswith("\n") or "  " not in line:
                raise ValueError
            digest, relative = line[:-1].split("  ", 1)
            if not _UNPREFIXED_HASH_RE.fullmatch(digest):
                raise ValueError
            safe_relative_posix_ref(relative)
            expected.append((relative, digest))
    except (UnicodeDecodeError, ValueError):
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
    if [item[0] for item in expected] != sorted({item[0] for item in expected}, key=lambda value: value.encode("utf-8")):
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")
    actual: list[tuple[str, str]] = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            _lstat(path, directory=True)
            continue
        _lstat(path, directory=False)
        if relative == "checksums.sha256":
            continue
        actual.append((relative, hashlib.sha256(_read_regular(path, base)).hexdigest()))
    actual.sort(key=lambda item: item[0].encode("utf-8"))
    if expected != actual:
        raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED")


def _provider_artifact_matches_request(
    snapshot: Snapshot,
    request: Mapping[str, Any],
    tile_map: list[dict[str, Any]],
    artifacts_by_variant: Mapping[str, list[dict[str, Any]]],
) -> bool:
    variant_ids = {str(item.get("variant_id")) for item in tile_map}
    candidate_artifacts: list[dict[str, Any]] = []
    for variant_id in variant_ids:
        candidate_artifacts.extend(artifacts_by_variant.get(variant_id, []))
    unique_artifacts = {str(artifact.get("relative_ref")): artifact for artifact in candidate_artifacts}
    run_id = snapshot.run.get("run_id")
    if not isinstance(run_id, str):
        return False
    for artifact in unique_artifacts.values():
        metadata = artifact.get("metadata")
        if not isinstance(metadata, dict) or set(str(value) for value in metadata.get("variant_ids", [])) != variant_ids:
            continue
        if artifact.get("actual_sha256") != artifact.get("declared_sha256"):
            continue
        payload = artifact.get("payload")
        if not isinstance(payload, dict) or not isinstance(payload.get("annotations"), list):
            continue
        db_annotations = {
            str(row.get("annotation_id")): row
            for row in snapshot.annotations
            if row.get("subject_id") in variant_ids
        }
        wire_items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for annotation in payload["annotations"]:
            if not isinstance(annotation, dict):
                break
            annotation_id = str(annotation.get("annotation_id"))
            db_annotation = db_annotations.get(annotation_id)
            if db_annotation is None or not _annotation_matches_generated_artifact(snapshot, db_annotation, annotation):
                break
            variant_id = str(annotation.get("subject_id"))
            if variant_id in seen or variant_id not in variant_ids:
                break
            seen.add(variant_id)
            wire_item: dict[str, Any] = {"variant_id": variant_id}
            for key in (
                "synonyms_zh", "synonyms_en", "summary_zh", "summary_en", "color_terms", "shape_terms",
                "material_impressions", "building_roles", "style_tags", "avoid_for", "confidence", "reason",
            ):
                wire_item[key] = annotation.get(key)
            wire_items.append(wire_item)
        else:
            if seen == variant_ids:
                output = {"schema_id": "annotation-batch-output.v1", "items": wire_items}
                if _record_ok("annotation-batch-output.v1", output, Path(__file__).resolve().parents[2]) and sha256_json(output) == request.get("validated_artifact_sha256"):
                    return True
    return False


def _annotation_matches_generated_artifact(snapshot: Snapshot, database_annotation: Mapping[str, Any], generated_annotation: Mapping[str, Any]) -> bool:
    if database_annotation == generated_annotation:
        return True
    database_source = database_annotation.get("source")
    generated_source = generated_annotation.get("source")
    if not isinstance(database_source, dict) or not isinstance(generated_source, dict):
        return False
    if generated_source.get("verified") is not False or database_source.get("verified") is not True:
        return False
    database_copy = json.loads(canonical_json(database_annotation))
    database_copy["source"]["verified"] = False
    if database_copy != generated_annotation:
        return False
    annotation_id = str(database_annotation.get("annotation_id"))
    subject_id = str(database_annotation.get("subject_id"))
    return any(
        review.get("status") == "resolved"
        and review.get("resolved_at")
        and review.get("target_id") == subject_id
        and review.get("reason_code") in {"LOW_CONFIDENCE", "SAMPLED_QUALITY_REVIEW"}
        and f"annotation:{annotation_id}" in review.get("evidence", [])
        for review in snapshot.reviews
    )


def _expected_search_documents(snapshot: Snapshot) -> dict[str, str]:
    blocks = {str(row.get("block_id")): row for row in snapshot.blocks}
    annotations_by_subject: dict[str, list[dict[str, Any]]] = {}
    for annotation in snapshot.annotations:
        if annotation.get("source", {}).get("verified") is True:
            annotations_by_subject.setdefault(str(annotation.get("subject_id")), []).append(annotation)
    expected: dict[str, str] = {}
    for variant in snapshot.variants:
        if variant.get("candidate_qualification") not in {"eligible", "conditional"}:
            continue
        variant_id = str(variant.get("variant_id"))
        block = blocks.get(str(variant.get("block_id")))
        if block is None:
            continue
        semantic = _effective_semantics_from_rows(
            annotations_by_subject.get(variant_id, []) + annotations_by_subject.get(str(variant.get("block_id")), []),
            snapshot.overrides,
            variant_id,
        )
        text_parts: list[str] = []
        names = block.get("official_names", {})
        text_parts.extend(value for value in (names.get("zh_cn"), names.get("en_us")) if isinstance(value, str))
        facts = variant.get("machine_facts", {})
        text_parts.extend(str(value) for value in facts.get("machine_tags", []))
        text_parts.extend(str(value) for value in facts.get("geometry", {}).get("geometry_classes", []))
        for key in SEMANTIC_LIST_FIELDS + SEMANTIC_SCALAR_FIELDS:
            value = semantic.get(key)
            if isinstance(value, list):
                text_parts.extend(str(item) for item in value)
            elif isinstance(value, str):
                text_parts.append(value)
        expected[variant_id] = normalize_text(" ".join(sorted(set(text_parts), key=lambda value: value.encode("utf-8"))))
    return expected


def _effective_semantics(database: WorkspaceDatabase, variant_id: str) -> dict[str, Any]:
    try:
        row = database.connection.execute("SELECT block_id FROM variants WHERE variant_id=?", (variant_id,)).fetchone()
        block_id = str(row["block_id"]) if row is not None else variant_id
        return WorkspaceQueryService(database)._verified_semantics(database.connection, block_id, variant_id)
    except Exception:
        return {}


def _effective_semantics_from_rows(annotation: dict[str, Any] | list[dict[str, Any]] | None, overrides: list[dict[str, Any]], variant_id: str) -> dict[str, Any]:
    semantic: dict[str, Any] = {}
    annotation_rows = annotation if isinstance(annotation, list) else [annotation] if annotation is not None else []
    annotation_rows = sorted(annotation_rows, key=lambda value: str(value.get("annotation_id", "")).encode("utf-8"))
    for value in annotation_rows:
        if value.get("source", {}).get("verified") is not True:
            continue
        for key in SEMANTIC_LIST_FIELDS + SEMANTIC_SCALAR_FIELDS + ("confidence",):
            if key in value:
                if isinstance(value[key], list):
                    semantic.setdefault(key, [])
                    semantic[key].extend(value[key])
                else:
                    semantic[key] = value[key]
    for override in sorted(overrides, key=lambda row: str(row.get("override_id", "")).encode("utf-8")):
        if override.get("schema_version") != "manual-override.v1" or override.get("scope", {}).get("variant_id") != variant_id:
            continue
        for key, value in override.get("operations", {}).items():
            if key.startswith("add_") and isinstance(value, list):
                field = key[4:]
                semantic.setdefault(field, [])
                if isinstance(semantic[field], list):
                    semantic[field].extend(value)
            elif key.startswith("remove_") and isinstance(value, list):
                field = key[7:]
                semantic[field] = [item for item in semantic.get(field, []) if item not in value] if isinstance(semantic.get(field), list) else []
            elif key.startswith("set_"):
                semantic[key[4:]] = value
    for key, value in list(semantic.items()):
        if isinstance(value, list):
            semantic[key] = sorted(set(value), key=lambda item: str(item).encode("utf-8"))
    return semantic


def _hash_path_relative(path: Path, root: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().encode("utf-8")):
        if path.is_dir():
            _lstat(path, directory=True)
        else:
            _lstat(path, directory=False)
            descriptor = os.open(path, os.O_RDWR | (os.O_BINARY if hasattr(os, "O_BINARY") else 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    directories = [path for path in root.rglob("*") if path.is_dir()]
    for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        _lstat(directory, directory=True)
        _fsync_directory(directory)
    _fsync_directory(root)


def _commit_directory(staging: Path, final: Path) -> None:
    if final.exists() or final.is_symlink():
        raise ReleaseBuildFailure("RELEASE_ALREADY_BUILT")
    try:
        if os.name == "nt":
            move_file = ctypes.windll.kernel32.MoveFileExW
            move_file.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
            move_file.restype = ctypes.c_int
            if not move_file(str(staging), str(final), 0x00000008):
                raise OSError(ctypes.get_last_error(), "MoveFileExW")
        elif sys.platform.startswith("linux"):
            directory_flag = getattr(os, "O_DIRECTORY", 0)
            parent_fd = os.open(staging.parent, os.O_RDONLY | directory_flag)
            try:
                libc = ctypes.CDLL(None, use_errno=True)
                _linux_rename_noreplace(libc, parent_fd, staging.name, final.name)
            finally:
                os.close(parent_fd)
        else:
            raise OSError("atomic no-replace directory commit is unsupported on this platform")
    except ReleaseBuildFailure:
        raise
    except OSError as exc:
        if final.exists():
            raise ReleaseBuildFailure("RELEASE_ALREADY_BUILT") from exc
        raise ReleaseBuildFailure("RELEASE_BUILD_FAILED") from exc
    _lstat(final, directory=True)


def _linux_rename_noreplace(libc: Any, parent_fd: int, source_name: str, target_name: str) -> None:
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise OSError(38, "renameat2 is only supported for Linux x86_64")
    source = source_name.encode("utf-8")
    target = target_name.encode("utf-8")
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is not None:
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(parent_fd, source, parent_fd, target, 1)
    else:
        syscall = getattr(libc, "syscall", None)
        if syscall is None:
            raise OSError(38, "renameat2 syscall is unavailable")
        syscall.argtypes = [ctypes.c_long, ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        syscall.restype = ctypes.c_long
        result = syscall(316, parent_fd, source, parent_fd, target, 1)
    if result != 0:
        error = ctypes.get_errno()
        if error == 17:
            raise FileExistsError(error, "target exists")
        if error == 38:
            raise OSError(error, "renameat2 is unavailable")
        raise OSError(error, "renameat2")


def _remove_exact_staging(staging: Path, expected_identity: tuple[int, int, int] | None) -> None:
    staging_pattern = re.compile(r"^\.rel_[0-9a-f]{32}\.staging$")
    try:
        if staging_pattern.fullmatch(staging.name) is None:
            return
        if expected_identity is None:
            return
        _lstat(staging.parent, directory=True)
        before = _lstat(staging, directory=True)
        current_identity = (int(before.st_dev), int(before.st_ino), stat.S_IFMT(before.st_mode))
        if current_identity != expected_identity:
            return
        root_node = (int(before.st_dev), int(before.st_ino))
        _remove_tree_identity_checked(staging, root_node)
    except (OSError, ReleaseBuildFailure):
        pass


def _remove_tree_identity_checked(path: Path, expected_node: tuple[int, int]) -> None:
    before = _lstat(path, directory=True)
    if (int(before.st_dev), int(before.st_ino)) != expected_node:
        return
    for entry in list(path.iterdir()):
        entry_before = _lstat(entry, directory=None)
        entry_node = (int(entry_before.st_dev), int(entry_before.st_ino))
        if stat.S_ISDIR(entry_before.st_mode):
            _remove_tree_identity_checked(entry, entry_node)
            try:
                after = _lstat(entry, directory=True)
            except ReleaseBuildFailure:
                continue
            if (int(after.st_dev), int(after.st_ino)) != entry_node:
                continue
            entry.rmdir()
        else:
            try:
                after = _lstat(entry, directory=False)
            except ReleaseBuildFailure:
                continue
            if (int(after.st_dev), int(after.st_ino)) != entry_node:
                continue
            entry.unlink()
    try:
        after_root = _lstat(path, directory=True)
    except ReleaseBuildFailure:
        return
    if (int(after_root.st_dev), int(after_root.st_ino)) == expected_node:
        path.rmdir()


def _make_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            if path.is_dir():
                os.chmod(path, 0o555)
            else:
                os.chmod(path, 0o444)
        except OSError as exc:
            raise ReleaseBuildFailure("RELEASE_BUILD_INTEGRITY_FAILED", after_commit=True) from exc
    os.chmod(root, 0o555)


def _read_release_built_at(final: Path, root: Path) -> str:
    release, _ = _json_bytes(final / "release.json", root)
    return str(release.get("built_at"))


__all__ = [
    "CHECK_CODES",
    "ReleaseBuildFailure",
    "ReleaseBuilder",
    "ReleaseCheckNotFound",
]
