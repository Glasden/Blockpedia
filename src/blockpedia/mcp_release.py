"""Read-only resolution and integrity checking for MCP releases.

The R4 reader is deliberately independent from the mutable Studio storage
layer.  It only opens a release after the current pointer, release metadata,
hash inventories, quality gate, and the fresh v2 SQLite projection have all
been checked.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import quote

from .features import FEATURE_EXTRACTOR_VERSION, DecodedPng, decode_rgba_png, validate_rgba_png
from .paths import DataRoot, safe_relative_posix_ref
from .releases import CHECK_CODES, _valid_release_report
from .schema import RecordSchemaError, schema_namespace, validate_record
from .search import SEMANTIC_LIST_FIELDS, SEMANTIC_SCALAR_FIELDS, normalize_text


MCP_VERSION_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$")
RELEASE_ID_RE = re.compile(r"^rel_[0-9a-f]{32}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
RAW_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
EXPECTED_RELEASE_ENTRIES = frozenset(
    {
        "release.json",
        "manifest.json",
        "index.sqlite3",
        "previews",
        "quality_report.json",
        "manual-overrides.json",
        "schemas.sha256",
        "checksums.sha256",
    }
)
EXPECTED_INDEX_TABLES = frozenset({"schema_meta", "blocks", "states", "visual_variants", "annotations"})
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal", ".wal", ".shm", ".journal")
FEATURE_KEYS = frozenset(
    {
        "input_sha256",
        "feature_extractor_version",
        "mask_coverage",
        "transparent_ratio",
        "average_rgb",
        "oklab",
        "lab",
        "brightness",
        "saturation",
        "edge_density",
        "directionality",
        "geometry_classes",
        "machine_tags",
    }
)
REQUIRED_RECORD_SCHEMA_IDS = frozenset({"block-record.v1", "state-record.v1", "visual-variant-record.v1"})


class MCPReleaseError(RuntimeError):
    """A stable MCP release-resolution failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        minecraft_version: str | None = None,
        details: Mapping[str, Any] | None = None,
        available_versions: list[str] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.minecraft_version = minecraft_version
        self.details = dict(details or {})
        self.available_versions = list(available_versions or [])
        super().__init__(message)


class MCPVersionInputError(ValueError):
    """A malformed MCP version, which belongs to JSON-RPC -32602."""


@dataclass(slots=True)
class ReleaseHandle:
    """A verified, immutable release and its read-only SQLite connection."""

    data_root: Path
    minecraft_version: str
    release_id: str
    release_path: Path
    release: dict[str, Any]
    manifest: dict[str, Any]
    quality_report: dict[str, Any]
    manifest_sha256: str
    connection: sqlite3.Connection
    verified_files: Mapping[str, tuple[str, tuple[int, int, int, int, int]]]

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ReleaseHandle":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    @property
    def provider_snapshot(self) -> Mapping[str, Any]:
        return self.manifest["provider_snapshot"]

    @property
    def fts_mode(self) -> str:
        return str(self.manifest["fts_mode"])

    def read_bytes(self, relative_ref: str) -> bytes:
        """Read one verified release-relative regular file without following links."""

        try:
            safe_relative_posix_ref(relative_ref)
        except ValueError as exc:
            raise MCPReleaseError(
                "RELEASE_INTEGRITY_FAILED",
                "The release contains an unsafe file reference.",
                minecraft_version=self.minecraft_version,
                details={"integrity_component": "manifest"},
            ) from exc
        path = _safe_child(self.release_path, relative_ref, directory=False)
        expected = self.verified_files.get(relative_ref)
        if expected is None:
            raise MCPReleaseError(
                "RELEASE_INTEGRITY_FAILED",
                "The requested release file is not in the verified checksum inventory.",
                minecraft_version=self.minecraft_version,
                details={"integrity_component": "checksums"},
            )
        payload, identity = _read_regular_with_identity(path, self.release_path)
        if identity != expected[1] or _hash_bytes(payload) != "sha256:" + expected[0]:
            raise MCPReleaseError(
                "RELEASE_INTEGRITY_FAILED",
                "A release file no longer matches its verified checksum inventory.",
                minecraft_version=self.minecraft_version,
                details={"integrity_component": "checksums"},
            )
        return payload

    def assert_index_current(self) -> None:
        """Fail closed if the immutable index path changed after resolution."""

        expected = self.verified_files.get("index.sqlite3")
        if expected is None:
            raise MCPReleaseError(
                "RELEASE_INTEGRITY_FAILED",
                "The release index is not in the verified checksum inventory.",
                minecraft_version=self.minecraft_version,
                details={"integrity_component": "index"},
            )
        payload, identity = _read_regular_with_identity(self.release_path / "index.sqlite3", self.release_path)
        if identity != expected[1] or _hash_bytes(payload) != "sha256:" + expected[0]:
            raise MCPReleaseError(
                "RELEASE_INTEGRITY_FAILED",
                "The release index changed after resolution.",
                minecraft_version=self.minecraft_version,
                details={"integrity_component": "index"},
            )

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Guard every deferred SQLite read against path replacement or mutation."""

        self.assert_index_current()
        try:
            return self.connection.execute(statement, parameters)
        except sqlite3.Error as exc:
            raise MCPReleaseError(
                "RELEASE_INTEGRITY_FAILED",
                "The release index could not be read safely.",
                minecraft_version=self.minecraft_version,
                details={"integrity_component": "index"},
            ) from exc

    def read_image(self, relative_ref: str) -> tuple[bytes, DecodedPng]:
        payload = self.read_bytes(relative_ref)
        try:
            decoded = decode_rgba_png(payload)
        except Exception as exc:
            raise MCPReleaseError(
                "IMAGE_READ_FAILED",
                "A release image could not be decoded.",
                minecraft_version=self.minecraft_version,
                details={"integrity_component": "index"},
            ) from exc
        return payload, decoded


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _lstat(path: Path, *, directory: bool | None = None) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise MCPReleaseError("RELEASE_NOT_FOUND", "The current release is not available.") from exc
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "Release links and reparse points are not allowed.")
    if directory is True and not stat.S_ISDIR(value.st_mode):
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A release directory is not a directory.")
    if directory is False and (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1):
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "Release files must be single-link regular files.")
    return value


def _safe_child(root: Path, relative_ref: str, *, directory: bool | None = None) -> Path:
    try:
        safe_relative_posix_ref(relative_ref)
        candidate = root.joinpath(*relative_ref.split("/"))
        candidate.absolute().relative_to(root.absolute())
    except (ValueError, OSError) as exc:
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A release path escapes its release directory.") from exc
    _safe_components(candidate, root, final_directory=directory)
    return candidate


def _safe_components(path: Path, root: Path, *, final_directory: bool | None = None) -> None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A release path escapes its release directory.") from exc
    _lstat(root, directory=True)
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        expected = final_directory if index == len(parts) - 1 else None
        _lstat(current, directory=expected)


def _read_regular(path: Path, root: Path) -> bytes:
    payload, _ = _read_regular_with_identity(path, root)
    return payload


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_nlink)


def _read_regular_with_identity(path: Path, root: Path) -> tuple[bytes, tuple[int, int, int, int, int]]:
    _safe_components(path, root, final_directory=False)
    before = _lstat(path, directory=False)
    try:
        with path.open("rb") as handle:
            payload = handle.read()
    except OSError as exc:
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A release file could not be read.") from exc
    after = _lstat(path, directory=False)
    identity = _file_identity(before)
    after_identity = _file_identity(after)
    if identity != after_identity:
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A release file changed while it was being read.")
    return payload, identity


def _json_file(path: Path, root: Path, *, component: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = _read_regular(path, root)
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, MCPReleaseError) as exc:
        if isinstance(exc, MCPReleaseError):
            raise
        raise MCPReleaseError(
            "RELEASE_INTEGRITY_FAILED",
            "A release metadata file is malformed.",
            details={"integrity_component": component},
        ) from exc
    if not isinstance(value, dict):
        raise MCPReleaseError(
            "RELEASE_INTEGRITY_FAILED",
            "A release metadata file is malformed.",
            details={"integrity_component": component},
        )
    return value, payload


def _validate_record(schema_id: str, value: Any, *, component: str, version: str | None = None) -> None:
    try:
        validate_record(schema_id, value)
    except (RecordSchemaError, TypeError, ValueError, KeyError) as exc:
        raise MCPReleaseError(
            "RELEASE_INTEGRITY_FAILED",
            "A release metadata record failed schema validation.",
            minecraft_version=version,
            details={"integrity_component": component},
        ) from exc


def _hash_bytes(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _reject_sidecar(name: str) -> None:
    if any(name.endswith(suffix) for suffix in SIDECAR_SUFFIXES) or name.startswith("index.sqlite3-"):
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "SQLite sidecar files are not allowed.")


def _walk_regular_files(root: Path) -> list[str]:
    files: list[str] = []

    def walk(directory: Path, prefix: str = "") -> None:
        _lstat(directory, directory=True)
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.encode("utf-8"))
        except OSError as exc:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release directory cannot be listed.") from exc
        for child in children:
            _reject_sidecar(child.name)
            _lstat(child, directory=None)
            relative = f"{prefix}/{child.name}" if prefix else child.name
            if child.is_dir():
                walk(child, relative)
            else:
                files.append(relative)

    walk(root)
    return sorted(files, key=lambda value: value.encode("utf-8"))


def _parse_checksums(payload: bytes) -> dict[str, str]:
    try:
        lines = payload.decode("ascii").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release checksum inventory is malformed.", details={"integrity_component": "checksums"}) from exc
    result: dict[str, str] = {}
    ordered: list[str] = []
    for line in lines:
        if not line.endswith("\n") or line.count("  ") != 1:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release checksum inventory is malformed.", details={"integrity_component": "checksums"})
        digest, relative = line[:-1].split("  ", 1)
        if RAW_HASH_RE.fullmatch(digest) is None or relative == "checksums.sha256":
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release checksum inventory is malformed.", details={"integrity_component": "checksums"})
        try:
            safe_relative_posix_ref(relative)
        except ValueError as exc:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release checksum inventory contains an unsafe path.", details={"integrity_component": "checksums"}) from exc
        if relative in result:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release checksum inventory contains duplicates.", details={"integrity_component": "checksums"})
        result[relative] = digest
        ordered.append(relative)
    if ordered != sorted(ordered, key=lambda value: value.encode("utf-8")):
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release checksum inventory is not sorted.", details={"integrity_component": "checksums"})
    return result


def _parse_schema_inventory(payload: bytes, repo_root: Path) -> None:
    try:
        lines = payload.decode("ascii").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory is malformed.", details={"integrity_component": "manifest"}) from exc
    ids: list[str] = []
    for line in lines:
        if not line.endswith("\n") or line.count("  ") != 2:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory is malformed.", details={"integrity_component": "manifest"})
        digest, schema_id, repository_path = line[:-1].split("  ")
        if RAW_HASH_RE.fullmatch(digest) is None or not schema_id or not repository_path:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory is malformed.", details={"integrity_component": "manifest"})
        try:
            safe_relative_posix_ref(repository_path)
            namespace = schema_namespace(schema_id)
        except ValueError as exc:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory contains an unsafe path.", details={"integrity_component": "manifest"}) from exc
        except Exception as exc:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory contains an unknown schema.", details={"integrity_component": "manifest"}) from exc
        expected_path = f"schemas/{namespace}/{schema_id}.json"
        if repository_path != expected_path:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory path is not canonical.", details={"integrity_component": "manifest"})
        schema_path = repo_root.joinpath(*repository_path.split("/"))
        try:
            schema_bytes = schema_path.read_bytes()
        except OSError as exc:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A schema inventory file is missing.", details={"integrity_component": "manifest"}) from exc
        if hashlib.sha256(schema_bytes).hexdigest() != digest:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A schema inventory hash does not match the repository schema.", details={"integrity_component": "manifest"})
        ids.append(schema_id)
    if ids != sorted(ids, key=lambda value: value.encode("utf-8")) or len(ids) != len(set(ids)):
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory is not sorted.", details={"integrity_component": "manifest"})
    if not REQUIRED_RECORD_SCHEMA_IDS.issubset(ids):
        raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory is missing required record schemas.", details={"integrity_component": "manifest"})


def _quality_passed(report: Mapping[str, Any]) -> bool:
    if report.get("status") != "passed":
        return False
    items = report.get("items")
    if not isinstance(items, list) or any(not isinstance(item, Mapping) or item.get("status") != "passed" for item in items):
        return False
    return True


def _immutable_uri(path: Path) -> str:
    # quote() keeps spaces and non-ASCII paths valid on Windows and POSIX.
    return "file:" + quote(path.absolute().as_posix(), safe="/:\\") + "?mode=ro&immutable=1"


class MCPReleaseResolver:
    """Resolve exactly one current release per request, without persistence."""

    def __init__(self, data_root: str | Path | DataRoot, *, repo_root: Path | None = None) -> None:
        self.data_root = data_root.root if isinstance(data_root, DataRoot) else Path(data_root).expanduser().absolute()
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]

    def available_versions(self) -> list[str]:
        current = self.data_root / "current.json"
        if not current.exists():
            return []
        try:
            value, _ = _json_file(current, self.data_root, component="current_pointer")
        except MCPReleaseError:
            return []
        versions = value.get("versions")
        return sorted(versions, key=lambda item: str(item).encode("utf-8")) if isinstance(versions, Mapping) else []

    def resolve(self, minecraft_version: str | None = None) -> ReleaseHandle:
        requested = minecraft_version
        if requested is not None and (not isinstance(requested, str) or MCP_VERSION_RE.fullmatch(requested) is None):
            raise MCPVersionInputError("minecraft_version must match the strict MCP version pattern")

        if not self.data_root.exists() or not self.data_root.is_dir():
            raise MCPReleaseError("DATA_ROOT_INVALID", "The configured data root is unavailable.")
        current_path = self.data_root / "current.json"
        try:
            for entry in self.data_root.iterdir():
                if entry.name.startswith("current.json.") or entry.name in {"current.json.tmp", "current.json.tmp.old"}:
                    raise MCPReleaseError("CURRENT_POINTER_INVALID", "Current pointer sidecars are not allowed.", details={"integrity_component": "current_pointer"})
        except OSError as exc:
            raise MCPReleaseError("DATA_ROOT_INVALID", "The configured data root cannot be listed.") from exc
        try:
            current, current_bytes = _json_file(current_path, self.data_root, component="current_pointer")
        except MCPReleaseError as exc:
            if not current_path.exists():
                raise MCPReleaseError("CURRENT_POINTER_MISSING", "The current release pointer is missing.") from exc
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer is invalid.", details={"integrity_component": "current_pointer"}) from exc
        try:
            _validate_record("current-pointer.v1", current, component="current_pointer")
        except MCPReleaseError as exc:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer is invalid.", details={"integrity_component": "current_pointer"}) from exc
        del current_bytes
        versions = current.get("versions")
        if not isinstance(versions, Mapping):
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer has no version map.", details={"integrity_component": "current_pointer"})
        available = sorted(versions, key=lambda value: str(value).encode("utf-8"))
        default = current.get("default_minecraft_version")
        if not isinstance(default, str) or default not in versions:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer has no valid default version.", details={"integrity_component": "current_pointer"})
        if TIMESTAMP_RE.fullmatch(str(current.get("updated_at"))) is None:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer timestamp is invalid.", details={"integrity_component": "current_pointer"})
        selected = default if requested is None else requested
        if selected not in versions:
            raise MCPReleaseError("VERSION_NOT_AVAILABLE", "The requested Minecraft version is not published.", minecraft_version=selected, available_versions=available)
        pointer = versions[selected]
        if not isinstance(pointer, Mapping):
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The selected current pointer is invalid.", minecraft_version=selected, details={"integrity_component": "current_pointer"})
        try:
            self._check_pointer_invariants(current, selected, pointer)
        except MCPReleaseError:
            raise
        release_id = str(pointer["release_id"])
        relative_path = str(pointer["relative_path"])
        release_path = _safe_child(self.data_root, relative_path, directory=True)
        if release_path.name != release_id:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer release path is not exact.", minecraft_version=selected, details={"integrity_component": "current_pointer"})
        if not release_path.exists():
            raise MCPReleaseError("RELEASE_NOT_FOUND", "The current release directory is missing.", minecraft_version=selected, details={"release_id": release_id})
        return self._verify_release(selected, release_id, release_path, str(pointer["manifest_sha256"]))

    def _check_pointer_invariants(self, current: Mapping[str, Any], selected: str, pointer: Mapping[str, Any]) -> None:
        versions = current.get("versions")
        if not isinstance(versions, Mapping):
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer is invalid.", details={"integrity_component": "current_pointer"})
        if not RELEASE_ID_RE.fullmatch(str(pointer.get("release_id", ""))):
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer release ID is invalid.", minecraft_version=selected, details={"integrity_component": "current_pointer"})
        if pointer.get("minecraft_version") != selected:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer version does not match its key.", minecraft_version=selected, details={"integrity_component": "current_pointer"})
        expected = f"releases/{selected}/{pointer['release_id']}"
        if pointer.get("relative_path") != expected or HASH_RE.fullmatch(str(pointer.get("manifest_sha256"))) is None:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer path or hash is invalid.", minecraft_version=selected, details={"integrity_component": "current_pointer"})

    def _verify_release(self, version: str, release_id: str, release_path: Path, pointer_manifest_hash: str) -> ReleaseHandle:
        try:
            entries = list(release_path.iterdir())
        except OSError as exc:
            raise MCPReleaseError("RELEASE_NOT_FOUND", "The current release directory cannot be listed.", minecraft_version=version) from exc
        for entry in entries:
            _reject_sidecar(entry.name)
            _lstat(entry, directory=None)
        if {entry.name for entry in entries} != EXPECTED_RELEASE_ENTRIES:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release layout is not exact.", minecraft_version=version, details={"integrity_component": "checksums"})

        release, _ = _json_file(release_path / "release.json", release_path, component="manifest")
        manifest, manifest_bytes = _json_file(release_path / "manifest.json", release_path, component="manifest")
        quality, quality_bytes = _json_file(release_path / "quality_report.json", release_path, component="quality_report")
        _validate_record("release.v1", release, component="manifest", version=version)
        _validate_record("release-manifest.v1", manifest, component="manifest", version=version)
        if release.get("release_id") != release_id or manifest.get("release_id") != release_id or release.get("minecraft_version") != version or manifest.get("minecraft_version") != version:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "Release metadata does not match the current pointer.", minecraft_version=version, details={"integrity_component": "manifest"})
        if TIMESTAMP_RE.fullmatch(str(release.get("built_at"))) is None:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release build timestamp is invalid.", minecraft_version=version, details={"integrity_component": "manifest"})
        actual_manifest_hash = _hash_bytes(manifest_bytes)
        if pointer_manifest_hash != actual_manifest_hash or release.get("manifest_sha256") != actual_manifest_hash:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release manifest hash is invalid.", minecraft_version=version, details={"integrity_component": "manifest"})
        if release.get("quality_report_path") != "quality_report.json" or manifest.get("quality_report_path") != "quality_report.json":
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release quality report reference is invalid.", minecraft_version=version, details={"integrity_component": "quality_report"})
        quality_hash = _hash_bytes(quality_bytes)
        quality_valid = _valid_release_report(quality)
        if quality_valid and (quality.get("release_id") != release_id or quality.get("minecraft_version") != version or quality.get("built_at") != release.get("built_at")):
            quality_valid = False
        if manifest.get("quality_report_sha256") != quality_hash or not quality_valid:
            code = "RELEASE_NOT_BUILT" if not quality_valid and quality.get("status") != "passed" else "RELEASE_INTEGRITY_FAILED"
            raise MCPReleaseError(code, "The release quality gate has not passed.", minecraft_version=version, details={"integrity_component": "quality_report"})
        if release.get("immutable") is not True:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release is not marked immutable.", minecraft_version=version, details={"integrity_component": "manifest"})
        if manifest.get("schemas_inventory_path") != "schemas.sha256":
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The schema inventory reference is invalid.", minecraft_version=version, details={"integrity_component": "manifest"})

        checksums_payload = _read_regular(release_path / "checksums.sha256", release_path)
        checksums = _parse_checksums(checksums_payload)
        actual_files = _walk_regular_files(release_path)
        actual_without_checksums = [ref for ref in actual_files if ref != "checksums.sha256"]
        if list(checksums) != actual_without_checksums:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release checksum inventory is not exact.", minecraft_version=version, details={"integrity_component": "checksums"})
        for item in quality.get("items", []):
            for evidence in item.get("evidence", []):
                if evidence not in actual_files:
                    raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The quality report references a missing evidence file.", minecraft_version=version, details={"integrity_component": "quality_report"})

        artifacts = manifest.get("functional_artifacts")
        if not isinstance(artifacts, Mapping) or "index.sqlite3" not in artifacts or not HASH_RE.fullmatch(str(artifacts["index.sqlite3"])):
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release index artifact hash is missing.", minecraft_version=version, details={"integrity_component": "index"})
        for relative, expected in artifacts.items():
            try:
                safe_relative_posix_ref(str(relative))
            except ValueError as exc:
                raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A functional artifact path is unsafe.", minecraft_version=version, details={"integrity_component": "manifest"}) from exc
            if not HASH_RE.fullmatch(str(expected)) or str(relative) not in actual_files:
                raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A functional artifact is invalid.", minecraft_version=version, details={"integrity_component": "manifest"})
            if _hash_bytes(_read_regular(release_path / str(relative), release_path)) != expected:
                component = "index" if relative == "index.sqlite3" else "manifest"
                raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A functional artifact hash is invalid.", minecraft_version=version, details={"integrity_component": component})

        verified_files: dict[str, tuple[str, tuple[int, int, int, int, int]]] = {}
        for relative, expected in checksums.items():
            payload, identity = _read_regular_with_identity(release_path / relative, release_path)
            if _hash_bytes(payload) != "sha256:" + expected:
                raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "A release checksum does not match its file.", minecraft_version=version, details={"integrity_component": "checksums"})
            verified_files[relative] = (expected, identity)
        _, checksums_identity = _read_regular_with_identity(release_path / "checksums.sha256", release_path)
        verified_files["checksums.sha256"] = (hashlib.sha256(checksums_payload).hexdigest(), checksums_identity)
        schemas_payload = _read_regular(release_path / "schemas.sha256", release_path)
        _parse_schema_inventory(schemas_payload, self.repo_root)

        connection = self._open_index(release_path / "index.sqlite3", version, str(manifest["fts_mode"]), verified_files["index.sqlite3"])
        try:
            manual_payload, _ = _read_regular_with_identity(release_path / "manual-overrides.json", release_path)
            manual = json.loads(manual_payload.decode("utf-8"))
            self._validate_index_projection(connection, version, release_path, manifest, manual, verified_files)
        except MCPReleaseError:
            connection.close()
            raise
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            connection.close()
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release record projection is invalid.", minecraft_version=version, details={"integrity_component": "index"}) from exc
        return ReleaseHandle(self.data_root, version, release_id, release_path, release, manifest, quality, actual_manifest_hash, connection, verified_files)

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _validate_semantic(value: Any, version: str) -> None:
        if not isinstance(value, dict):
            raise ValueError("semantic projection is not an object")
        allowed = set(SEMANTIC_LIST_FIELDS) | set(SEMANTIC_SCALAR_FIELDS) | {"confidence"}
        if set(value) - allowed:
            raise ValueError("semantic projection contains an unknown field")
        for key in SEMANTIC_LIST_FIELDS:
            if key in value and (not isinstance(value[key], list) or len(value[key]) != len(set(value[key])) or any(not isinstance(item, str) or not item for item in value[key])):
                raise ValueError("semantic list projection is invalid")
        for key in SEMANTIC_SCALAR_FIELDS:
            if key in value and (not isinstance(value[key], str) or not value[key]):
                raise ValueError("semantic scalar projection is invalid")
        if "confidence" in value and (isinstance(value["confidence"], bool) or not isinstance(value["confidence"], (int, float)) or not 0 <= value["confidence"] <= 1):
            raise ValueError("semantic confidence projection is invalid")

    @staticmethod
    def _validate_feature(value: Any, variant: Mapping[str, Any]) -> None:
        if not isinstance(value, dict) or set(value) != FEATURE_KEYS:
            raise ValueError("feature projection keys are invalid")
        if not isinstance(value["input_sha256"], str) or HASH_RE.fullmatch(value["input_sha256"]) is None:
            raise ValueError("feature input hash is invalid")
        if value["feature_extractor_version"] != FEATURE_EXTRACTOR_VERSION:
            raise ValueError("feature extractor version is invalid")
        numeric = ("mask_coverage", "transparent_ratio", "brightness", "saturation", "edge_density", "directionality")
        for key in numeric:
            if isinstance(value[key], bool) or not isinstance(value[key], (int, float)) or not math.isfinite(float(value[key])) or not 0 <= float(value[key]) <= 1:
                raise ValueError("feature numeric field is invalid")
        if not isinstance(value["average_rgb"], list) or len(value["average_rgb"]) != 3 or any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or not 0 <= float(item) <= 1 for item in value["average_rgb"]):
            raise ValueError("feature RGB vector is invalid")
        if not isinstance(value["oklab"], list) or len(value["oklab"]) != 3 or any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or (index == 0 and not 0 <= float(item) <= 1) or (index != 0 and not -1 <= float(item) <= 1) for index, item in enumerate(value["oklab"])):
            raise ValueError("feature color vector is invalid")
        if not isinstance(value["lab"], list) or len(value["lab"]) != 3 or any(isinstance(item, bool) or not isinstance(item, (int, float)) or not math.isfinite(float(item)) or (index == 0 and not 0 <= float(item) <= 100) or (index != 0 and not -150 <= float(item) <= 150) for index, item in enumerate(value["lab"])):
            raise ValueError("feature Lab vector is invalid")
        for key in ("geometry_classes", "machine_tags"):
            if not isinstance(value[key], list) or len(value[key]) != len(set(value[key])) or value[key] != sorted(value[key], key=lambda item: str(item).encode("utf-8")) or any(not isinstance(item, str) or not item for item in value[key]):
                raise ValueError("feature categorical field is invalid")
        geometry = variant.get("machine_facts", {}).get("geometry", {})
        if value["feature_extractor_version"] != geometry.get("feature_extractor_version") or value["input_sha256"] != geometry.get("feature_input_sha256"):
            raise ValueError("feature lineage does not match machine facts")
        if value["geometry_classes"] != geometry.get("geometry_classes") or value["machine_tags"] != variant.get("machine_facts", {}).get("machine_tags"):
            raise ValueError("feature machine categories do not match machine facts")

    @staticmethod
    def _validate_manual_package(manual: Any, version: str, release_id: str, blocks: Mapping[str, Mapping[str, Any]], states: Mapping[str, Mapping[str, Any]], variants: Mapping[str, Mapping[str, Any]]) -> None:
        if not isinstance(manual, dict) or set(manual) != {"format_version", "release_id", "version", "manual_overrides", "skip_reviews", "qualification_reviews"}:
            raise ValueError("manual package shape is invalid")
        if manual["format_version"] != 1 or manual["release_id"] != release_id or manual["version"] != version:
            raise ValueError("manual package identity is invalid")
        for key in ("manual_overrides", "skip_reviews", "qualification_reviews"):
            if not isinstance(manual[key], list):
                raise ValueError("manual package collection is invalid")
        for key, identifier_key in (("manual_overrides", "override_id"), ("skip_reviews", "review_id"), ("qualification_reviews", "review_id")):
            identifiers = [str(record.get(identifier_key, "")) for record in manual[key] if isinstance(record, Mapping)]
            if identifiers != sorted(identifiers, key=lambda value: value.encode("utf-8")) or len(identifiers) != len(set(identifiers)):
                raise ValueError("manual package ordering is invalid")
        seen: set[tuple[str, str]] = set()
        override_ids: set[str] = set()
        skip_ids: set[str] = set()
        qualification_ids: set[str] = set()
        for record in manual["manual_overrides"]:
            _validate_record("manual-override.v1", record, component="index", version=version)
            identifier = str(record["override_id"])
            if ("override", identifier) in seen or record["scope"]["variant_id"] not in variants:
                raise ValueError("manual override reference is invalid")
            seen.add(("override", identifier))
            override_ids.add(identifier)
            if identifier not in variants[record["scope"]["variant_id"]].get("override_refs", []):
                raise ValueError("manual override is not referenced by its variant")
        for record in manual["skip_reviews"]:
            _validate_record("skip-review.v1", record, component="index", version=version)
            identifier = str(record["review_id"])
            target_type = str(record["target_type"])
            target = str(record["target_id"])
            if target_type == "visual_variant":
                target_exists = target in variants or (target not in variants and target in blocks)
            else:
                targets = {"block": blocks, "state": states}.get(target_type)
                target_exists = targets is not None and target in targets
            if not target_exists or ("skip", identifier) in seen:
                raise ValueError("skip review reference is invalid")
            seen.add(("skip", identifier))
            skip_ids.add(identifier)
            if target_type == "visual_variant" and target in variants and identifier not in variants[target].get("override_refs", []):
                raise ValueError("skip review is not referenced by its variant")
        for record in manual["qualification_reviews"]:
            _validate_record("qualification-review.v1", record, component="index", version=version)
            identifier = str(record["review_id"])
            target = str(record["target_id"])
            if target not in variants or ("qualification", identifier) in seen:
                raise ValueError("qualification review reference is invalid")
            seen.add(("qualification", identifier))
            qualification_ids.add(identifier)
            variant = variants[target]
            if identifier not in variant.get("qualification_review_refs", []) or record["qualification"] != variant.get("candidate_qualification") or list(record["warnings"]) != list(variant.get("warnings", [])):
                raise ValueError("qualification review is not referenced by its variant")
        for variant in variants.values():
            if any(value not in override_ids | skip_ids for value in variant.get("override_refs", [])) or any(value not in qualification_ids for value in variant.get("qualification_review_refs", [])):
                raise ValueError("variant audit reference is missing from manual package")

    def _validate_index_projection(
        self,
        connection: sqlite3.Connection,
        version: str,
        release_path: Path,
        manifest: Mapping[str, Any],
        manual: Any,
        verified_files: Mapping[str, tuple[str, tuple[int, int, int, int, int]]],
    ) -> None:
        tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        base_tables = {"schema_meta", "blocks", "states", "visual_variants", "annotations"}
        expected_tables = base_tables | ({"search_fts", "search_fts_data", "search_fts_idx", "search_fts_content", "search_fts_docsize", "search_fts_config"} if manifest.get("fts_mode") == "trigram" else {"search_text"})
        if tables != expected_tables or [tuple(row) for row in connection.execute("SELECT format_version FROM schema_meta").fetchall()] != [(2,)]:
            raise ValueError("index table projection is invalid")

        blocks: dict[str, dict[str, Any]] = {}
        states: dict[str, dict[str, Any]] = {}
        variants: dict[str, dict[str, Any]] = {}
        features: dict[str, dict[str, Any]] = {}
        annotations: dict[str, dict[str, Any]] = {}
        for row in connection.execute("SELECT block_id,minecraft_version,translation_key,name_zh,name_en,default_state_id,machine_facts_json,record_json FROM blocks ORDER BY block_id"):
            block = json.loads(row[7])
            expected = (block["block_id"], block["minecraft_version"], block["translation_key"], block["official_names"]["zh_cn"], block["official_names"]["en_us"], block["default_state_id"], self._canonical(block["machine_facts"]), self._canonical(block))
            actual = (str(row[0]), str(row[1]), row[2], row[3], row[4], str(row[5]), row[6], row[7])
            if actual != expected or block["block_id"] in blocks:
                raise ValueError("block projection does not match its record")
            blocks[str(row[0])] = block
        for row in connection.execute("SELECT state_id,block_id,properties_json,is_default,record_json FROM states ORDER BY state_id"):
            state = json.loads(row[4])
            expected = (state["state_id"], state["block_id"], self._canonical(state["properties"]), int(state["is_default"] is True), self._canonical(state))
            actual = (str(row[0]), str(row[1]), row[2], int(row[3]), row[4])
            if actual != expected or state["state_id"] in states:
                raise ValueError("state projection does not match its record")
            states[str(row[0])] = state
        for row in connection.execute("SELECT variant_id,block_id,canonical_state_id,represented_state_ids_json,preview_path,mask_path,render_metadata_path,image_sha256,mask_sha256,render_metadata_sha256,candidate_qualification,warnings_json,record_json,feature_json FROM visual_variants ORDER BY variant_id"):
            variant = json.loads(row[12])
            feature = json.loads(row[13])
            self._validate_feature(feature, variant)
            suffix = str(row[0]).removeprefix("minecraft:")
            expected = (
                variant["variant_id"], variant["block_id"], variant["canonical_state_id"], self._canonical(variant["represented_state_ids"]),
                f"previews/minecraft/{suffix}/preview.png", f"previews/minecraft/{suffix}/mask.png", f"previews/minecraft/{suffix}/render.json",
                variant["render"]["image_sha256"], variant["render"]["mask_sha256"], variant["render"]["render_metadata_sha256"],
                variant["candidate_qualification"], self._canonical(variant.get("warnings", [])), self._canonical(variant), self._canonical(feature),
            )
            actual = tuple(row[index] for index in range(14))
            if actual != expected or variant["variant_id"] in variants:
                raise ValueError("variant projection does not match its record")
            variants[str(row[0])] = variant
            features[str(row[0])] = feature
        for row in connection.execute("SELECT variant_id,semantic_json FROM annotations ORDER BY variant_id"):
            semantic = json.loads(row[1])
            self._validate_semantic(semantic, version)
            if str(row[0]) in annotations:
                raise ValueError("duplicate annotation projection")
            annotations[str(row[0])] = semantic

        self._validate_manual_package(manual, version, str(manifest["release_id"]), blocks, states, variants)
        if set(annotations) != set(variants):
            raise ValueError("annotation projection coverage is invalid")
        default_state_counts: dict[str, int] = {}
        for state in states.values():
            if state["is_default"] is True:
                block_id = str(state["block_id"])
                default_state_counts[block_id] = default_state_counts.get(block_id, 0) + 1
        variant_counts: dict[str, int] = {}
        for variant in variants.values():
            block_id = str(variant["block_id"])
            variant_counts[block_id] = variant_counts.get(block_id, 0) + 1
        block_skip_targets = {str(record["target_id"]) for record in manual["skip_reviews"] if record["target_type"] == "block"}
        visual_variant_skip_targets = {str(record["target_id"]) for record in manual["skip_reviews"] if record["target_type"] == "visual_variant"}
        for block_id, block in blocks.items():
            default_state = states.get(str(block["default_state_id"]))
            if default_state is None or default_state["block_id"] != block_id or default_state["is_default"] is not True:
                raise ValueError("block default state reference is invalid")
            if default_state_counts.get(block_id, 0) != 1:
                raise ValueError("block default state cardinality is invalid")
            variant_count = variant_counts.get(block_id, 0)
            skipped = block_id in block_skip_targets or (variant_count == 0 and block_id in visual_variant_skip_targets)
            if variant_count == 0 and not skipped:
                raise ValueError("block has neither a visual variant nor an audited skip")
        for state_id, state in states.items():
            if len(state["variant_ids"]) != len(set(state["variant_ids"])) or state["block_id"] not in blocks or any(variant_id not in variants or variants[variant_id]["block_id"] != state["block_id"] for variant_id in state["variant_ids"]):
                raise ValueError("state variant reference is invalid")
        for variant_id, variant in variants.items():
            if variant["block_id"] not in blocks or variant["canonical_state_id"] not in states or states[variant["canonical_state_id"]]["block_id"] != variant["block_id"]:
                raise ValueError("variant state reference is invalid")
            represented = variant["represented_state_ids"]
            if len(represented) != len(set(represented)) or any(state_id not in states or states[state_id]["block_id"] != variant["block_id"] or variant_id not in states[state_id]["variant_ids"] for state_id in represented):
                raise ValueError("variant represented state reference is invalid")
            if variant_id not in states[variant["canonical_state_id"]]["variant_ids"]:
                raise ValueError("variant canonical state reference is invalid")
            render = variant["render"]
            suffix = variant_id.removeprefix("minecraft:")
            paths = {
                "preview": f"previews/minecraft/{suffix}/preview.png",
                "mask": f"previews/minecraft/{suffix}/mask.png",
                "render_metadata": f"previews/minecraft/{suffix}/render.json",
            }
            if render["preview_path"] != f"renders/minecraft/{suffix}/preview.png" or render["mask_path"] != f"renders/minecraft/{suffix}/mask.png" or render["render_metadata_path"] != f"renders/minecraft/{suffix}/render.json":
                raise ValueError("variant render path is invalid")
            for key, relative in paths.items():
                if relative not in verified_files:
                    raise ValueError("variant render artifact is missing")
            for key, relative in (("image_sha256", paths["preview"]), ("mask_sha256", paths["mask"])):
                payload, identity = _read_regular_with_identity(release_path / relative, release_path)
                expected_digest = "sha256:" + verified_files[relative][0]
                if identity != verified_files[relative][1] or _hash_bytes(payload) != expected_digest or render[key] != expected_digest:
                    raise ValueError("variant image hash is invalid")
                metadata = validate_rgba_png(payload)
                if metadata.width != 512 or metadata.height != 512:
                    raise ValueError("variant image dimensions are invalid")
            metadata_relative = paths["render_metadata"]
            metadata_path = release_path / metadata_relative
            metadata_payload, metadata_identity = _read_regular_with_identity(metadata_path, release_path)
            metadata = json.loads(metadata_payload.decode("utf-8"))
            _validate_record("render-metadata.v1", metadata, component="index", version=version)
            if metadata["variant_id"] != variant_id or metadata_identity != verified_files[metadata_relative][1] or _hash_bytes(metadata_payload) != "sha256:" + verified_files[metadata_relative][0] or render["render_metadata_sha256"] != "sha256:" + hashlib.sha256(self._canonical(metadata).encode("utf-8")).hexdigest():
                raise ValueError("render metadata hash is invalid")

        expected_fts: dict[str, str] = {}
        for variant_id, variant in variants.items():
            if variant["candidate_qualification"] not in {"eligible", "conditional"}:
                continue
            block = blocks[variant["block_id"]]
            text_parts: list[str] = []
            names = block.get("official_names", {})
            text_parts.extend(value for value in (names.get("zh_cn"), names.get("en_us")) if isinstance(value, str))
            facts = variant["machine_facts"]
            text_parts.extend(str(value) for value in facts.get("machine_tags", []))
            text_parts.extend(str(value) for value in facts.get("geometry", {}).get("geometry_classes", []))
            semantic = annotations[variant_id]
            for key in SEMANTIC_LIST_FIELDS + SEMANTIC_SCALAR_FIELDS:
                value = semantic.get(key)
                if isinstance(value, list):
                    text_parts.extend(str(item) for item in value)
                elif isinstance(value, str):
                    text_parts.append(value)
            expected_fts[variant_id] = normalize_text(" ".join(sorted(set(text_parts), key=lambda value: value.encode("utf-8"))))
        table = "search_fts" if manifest.get("fts_mode") == "trigram" else "search_text"
        fts_rows = [(str(row[0]), str(row[1])) for row in connection.execute(f"SELECT variant_id,normalized_text FROM {table}")]
        actual_fts = dict(fts_rows)
        if len(fts_rows) != len(actual_fts) or actual_fts != expected_fts:
            raise ValueError("search projection does not match release semantics")

    def _open_index(self, path: Path, version: str, fts_mode: str, expected_file: tuple[str, tuple[int, int, int, int, int]]) -> sqlite3.Connection:
        for sidecar in (Path(str(path) + suffix) for suffix in SIDECAR_SUFFIXES):
            if sidecar.exists() or sidecar.is_symlink():
                raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "SQLite sidecar files are not allowed.", minecraft_version=version, details={"integrity_component": "index"})
        before = _lstat(path, directory=False)
        if _file_identity(before) != expected_file[1]:
            raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release index changed before it was opened.", minecraft_version=version, details={"integrity_component": "index"})
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(_immutable_uri(path), uri=True, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA query_only").fetchone()[0] != 1 or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise sqlite3.DatabaseError("read-only pragmas were not applied")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise sqlite3.DatabaseError("SQLite integrity check failed")
            meta = connection.execute("SELECT format_version FROM schema_meta").fetchall()
            if [tuple(row) for row in meta] != [(2,)]:
                raise MCPReleaseError(
                    "RELEASE_INTEGRITY_FAILED",
                    "The release index is not the fresh v2 projection.",
                    minecraft_version=version,
                    details={"integrity_component": "index"},
                )
            tables = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not EXPECTED_INDEX_TABLES.issubset(tables):
                raise sqlite3.DatabaseError("release index tables are incomplete")
            if fts_mode == "trigram" and ("search_fts" not in tables or "search_text" in tables):
                raise sqlite3.DatabaseError("trigram search projection is missing")
            if fts_mode == "normalized_like" and ("search_text" not in tables or "search_fts" in tables):
                raise sqlite3.DatabaseError("LIKE search projection is missing")
            for table, column in (("blocks", "record_json"), ("states", "record_json"), ("visual_variants", "record_json"), ("visual_variants", "feature_json")):
                columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    raise sqlite3.DatabaseError("release index v2 columns are incomplete")
            payload, after_identity = _read_regular_with_identity(path, path.parent)
            if after_identity != expected_file[1] or _hash_bytes(payload) != "sha256:" + expected_file[0]:
                raise MCPReleaseError("RELEASE_INTEGRITY_FAILED", "The release index changed while it was being opened.", minecraft_version=version, details={"integrity_component": "index"})
            return connection
        except MCPReleaseError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            code = "RELEASE_INTEGRITY_FAILED" if connection is not None else "INDEX_OPEN_FAILED"
            raise MCPReleaseError(code, "The immutable release index could not be opened.", minecraft_version=version, details={"integrity_component": "index"}) from exc

    resolve_release = resolve


# Short aliases keep the boundary pleasant for callers without duplicating the
# resolver implementation.
ReleaseResolver = MCPReleaseResolver


__all__ = [
    "MCPReleaseError",
    "MCPReleaseResolver",
    "MCPVersionInputError",
    "ReleaseHandle",
    "ReleaseResolver",
]
