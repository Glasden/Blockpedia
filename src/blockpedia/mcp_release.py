"""Read-only current-pointer resolution for MCP releases.

The MCP reader intentionally trusts the build/activation gates for release
completeness.  Runtime work is limited to pointer selection, path/reparse
safety, opening the selected SQLite file, and reading data needed by the
current response.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

from .features import DecodedPng, decode_rgba_png
from .paths import DataRoot, safe_relative_posix_ref


MCP_VERSION_RE = re.compile(r"^[0-9]{1,3}\.[0-9]{1,3}(?:\.[0-9]{1,3})?$")
RELEASE_ID_RE = re.compile(r"^rel_[0-9a-f]{32}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_CURRENT_FIELDS = frozenset({"schema_version", "versions", "default_minecraft_version", "updated_at"})
_POINTER_FIELDS = frozenset({"release_id", "minecraft_version", "relative_path", "manifest_sha256"})
SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal", ".wal", ".shm", ".journal")


class MCPReleaseError(RuntimeError):
    """A stable MCP release-resolution or read failure."""

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
    """A pointer-selected release and a request-local read-only connection."""

    data_root: Path
    minecraft_version: str
    release_id: str
    release_path: Path
    release: dict[str, Any]
    manifest: dict[str, Any]
    manifest_sha256: str
    connection: sqlite3.Connection

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ReleaseHandle":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    @property
    def provider_snapshot(self) -> Mapping[str, Any]:
        value = self.manifest.get("provider_snapshot", {})
        return value if isinstance(value, Mapping) else {}

    @property
    def fts_mode(self) -> str:
        value = self.manifest.get("fts_mode", "trigram")
        if value not in {"trigram", "normalized_like"}:
            raise MCPReleaseError(
                "INDEX_INFO_UNAVAILABLE",
                "The release search mode declaration is unavailable.",
                minecraft_version=self.minecraft_version,
            )
        return str(value)

    def read_bytes(self, relative_ref: str) -> bytes:
        """Read one release-relative regular file after path safety checks.

        This deliberately does not consult checksums, manifests, or a cached
        file identity.  Build/activation gates own release completeness.
        """

        try:
            path = _safe_child(self.release_path, relative_ref, directory=False)
            return _read_regular(path, self.release_path)
        except MCPReleaseError:
            raise
        except OSError as exc:
            raise MCPReleaseError(
                "RELEASE_NOT_FOUND",
                "The requested release file is unavailable.",
                minecraft_version=self.minecraft_version,
            ) from exc

    def execute(self, statement: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute one request-local read against the pointer-selected index."""

        try:
            return self.connection.execute(statement, parameters)
        except sqlite3.Error as exc:
            raise MCPReleaseError(
                "INDEX_INFO_UNAVAILABLE",
                "The release index could not be read.",
                minecraft_version=self.minecraft_version,
                details={"integrity_component": "index"},
            ) from exc

    def read_image(self, relative_ref: str) -> tuple[bytes, DecodedPng]:
        try:
            payload = self.read_bytes(relative_ref)
            decoded = decode_rgba_png(payload)
        except Exception as exc:
            if isinstance(exc, MCPReleaseError) and exc.code == "IMAGE_READ_FAILED":
                raise
            raise MCPReleaseError(
                "IMAGE_READ_FAILED",
                "A release image could not be read.",
                minecraft_version=self.minecraft_version,
            ) from exc
        return payload, decoded


def _is_reparse(value: os.stat_result) -> bool:
    return bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _lstat(path: Path, *, directory: bool | None = None) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as exc:
        raise MCPReleaseError("RELEASE_NOT_FOUND", "The selected release is unavailable.") from exc
    if stat.S_ISLNK(value.st_mode) or _is_reparse(value):
        raise MCPReleaseError("CURRENT_POINTER_INVALID", "Release links and reparse points are not allowed.")
    if directory is True and not stat.S_ISDIR(value.st_mode):
        raise MCPReleaseError("CURRENT_POINTER_INVALID", "A release directory is not a directory.")
    if directory is False and not stat.S_ISREG(value.st_mode):
        raise MCPReleaseError("CURRENT_POINTER_INVALID", "Release files must be regular files.")
    return value


def _safe_child(root: Path, relative_ref: str, *, directory: bool | None = None) -> Path:
    try:
        safe_relative_posix_ref(relative_ref)
        candidate = root.joinpath(*relative_ref.split("/"))
        candidate.absolute().relative_to(root.absolute())
    except (ValueError, OSError) as exc:
        raise MCPReleaseError("CURRENT_POINTER_INVALID", "A release path escapes its data root.") from exc
    _safe_components(candidate, root, final_directory=directory)
    return candidate


def _safe_components(path: Path, root: Path, *, final_directory: bool | None = None) -> None:
    try:
        relative = path.absolute().relative_to(root.absolute())
    except ValueError as exc:
        raise MCPReleaseError("CURRENT_POINTER_INVALID", "A release path escapes its data root.") from exc
    _lstat(root, directory=True)
    current = root
    parts = relative.parts
    for index, part in enumerate(parts):
        current = current / part
        expected = final_directory if index == len(parts) - 1 else None
        _lstat(current, directory=expected)


def _read_regular(path: Path, root: Path) -> bytes:
    _safe_components(path, root, final_directory=False)
    try:
        with path.open("rb") as handle:
            return handle.read()
    except OSError as exc:
        raise MCPReleaseError("RELEASE_NOT_FOUND", "A release file could not be read.") from exc


def _json_file(path: Path, root: Path, *, component: str) -> tuple[dict[str, Any], bytes]:
    try:
        payload = _read_regular(path, root)
        value = json.loads(payload.decode("utf-8"))
    except MCPReleaseError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise MCPReleaseError(
            "CURRENT_POINTER_INVALID" if component == "current_pointer" else "INDEX_INFO_UNAVAILABLE",
            "A required release JSON file is unavailable or malformed.",
            details={"integrity_component": component},
        ) from exc
    if not isinstance(value, dict):
        raise MCPReleaseError(
            "CURRENT_POINTER_INVALID" if component == "current_pointer" else "INDEX_INFO_UNAVAILABLE",
            "A required release JSON file is not an object.",
            details={"integrity_component": component},
        )
    return value, payload


def _hash_bytes(payload: bytes) -> str:
    """Hash helper retained for build/activation code; MCP does not call it."""

    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _immutable_uri(path: Path) -> str:
    return "file:" + quote(path.absolute().as_posix(), safe="/:\\") + "?mode=ro&immutable=1"


def _reject_sidecar(name: str) -> None:
    if any(name.endswith(suffix) for suffix in SIDECAR_SUFFIXES) or name.startswith("index.sqlite3-"):
        raise MCPReleaseError("INDEX_OPEN_FAILED", "SQLite sidecar files are not allowed.")


class MCPReleaseResolver:
    """Resolve exactly one current release per request, without persistence."""

    def __init__(self, data_root: str | Path | DataRoot, *, repo_root: Path | None = None) -> None:
        self.data_root = data_root.root if isinstance(data_root, DataRoot) else Path(data_root).expanduser().absolute()
        # Kept as a constructor-compatible argument for existing callers.  No
        # repository Schema is read by the MCP runtime.
        self.repo_root = repo_root

    def available_versions(self) -> list[str]:
        try:
            current, _ = self._read_current()
        except MCPReleaseError:
            return []
        versions = current.get("versions")
        if not isinstance(versions, Mapping):
            return []
        return sorted((str(value) for value in versions), key=lambda item: item.encode("utf-8"))

    def _read_current(self) -> tuple[dict[str, Any], bytes]:
        if not self.data_root.exists() or not self.data_root.is_dir():
            raise MCPReleaseError("DATA_ROOT_INVALID", "The configured data root is unavailable.")
        current_path = self.data_root / "current.json"
        try:
            return _json_file(current_path, self.data_root, component="current_pointer")
        except MCPReleaseError as exc:
            if exc.code == "RELEASE_NOT_FOUND":
                raise MCPReleaseError("CURRENT_POINTER_MISSING", "The current release pointer is missing.") from exc
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer is invalid.") from exc

    def resolve(self, minecraft_version: str | None = None) -> ReleaseHandle:
        if minecraft_version is not None and (
            not isinstance(minecraft_version, str) or MCP_VERSION_RE.fullmatch(minecraft_version) is None
        ):
            raise MCPVersionInputError("minecraft_version must match the strict MCP version pattern")

        current, _ = self._read_current()
        if set(current) != _CURRENT_FIELDS or current.get("schema_version") != "current-pointer.v1":
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer is invalid.")
        versions = current.get("versions")
        default = current.get("default_minecraft_version")
        if (
            not isinstance(versions, Mapping)
            or any(not isinstance(key, str) or MCP_VERSION_RE.fullmatch(key) is None for key in versions)
            or not isinstance(default, str)
            or MCP_VERSION_RE.fullmatch(default) is None
            or default not in versions
        ):
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer has no valid default version.")
        if not isinstance(current.get("updated_at"), str) or TIMESTAMP_RE.fullmatch(current["updated_at"]) is None:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current release pointer timestamp is invalid.")

        selected = default if minecraft_version is None else minecraft_version
        available = sorted((str(value) for value in versions), key=lambda item: item.encode("utf-8"))
        if selected not in versions:
            raise MCPReleaseError(
                "VERSION_NOT_AVAILABLE",
                "The requested Minecraft version is not published.",
                minecraft_version=selected,
                available_versions=available,
            )
        pointer = versions[selected]
        self._check_pointer_invariants(selected, pointer)
        release_id = str(pointer["release_id"])
        relative_path = str(pointer["relative_path"])
        release_path = _safe_child(self.data_root, relative_path, directory=True)
        if release_path.name != release_id:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer release path is not exact.", minecraft_version=selected)
        return self._load_release(selected, release_id, relative_path, release_path, str(pointer["manifest_sha256"]))

    def _check_pointer_invariants(self, selected: str, pointer: Any) -> None:
        if not isinstance(pointer, Mapping) or set(pointer) != _POINTER_FIELDS:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The selected current pointer is invalid.", minecraft_version=selected)
        release_id = pointer.get("release_id")
        if not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer release ID is invalid.", minecraft_version=selected)
        if pointer.get("minecraft_version") != selected:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer version does not match its key.", minecraft_version=selected)
        if pointer.get("relative_path") != f"releases/{selected}/{release_id}":
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer path is invalid.", minecraft_version=selected)
        if not isinstance(pointer.get("manifest_sha256"), str) or HASH_RE.fullmatch(pointer["manifest_sha256"]) is None:
            raise MCPReleaseError("CURRENT_POINTER_INVALID", "The current pointer manifest declaration is invalid.", minecraft_version=selected)

    def _load_release(
        self,
        version: str,
        release_id: str,
        relative_path: str,
        release_path: Path,
        pointer_manifest_hash: str,
    ) -> ReleaseHandle:
        del relative_path
        try:
            release, _ = _json_file(release_path / "release.json", release_path, component="release")
            manifest, _ = _json_file(release_path / "manifest.json", release_path, component="manifest")
            connection = self._open_index(release_path / "index.sqlite3", version)
        except MCPReleaseError:
            raise
        except Exception as exc:
            raise MCPReleaseError(
                "INDEX_OPEN_FAILED",
                "The pointer-selected release could not be opened.",
                minecraft_version=version,
                details={"integrity_component": "index"},
            ) from exc
        return ReleaseHandle(self.data_root, version, release_id, release_path, release, manifest, pointer_manifest_hash, connection)

    @staticmethod
    def _open_index(path: Path, version: str) -> sqlite3.Connection:
        connection: sqlite3.Connection | None = None
        try:
            _safe_components(path, path.parent, final_directory=False)
            _reject_sidecar(path.name)
            connection = sqlite3.connect(_immutable_uri(path), uri=True, isolation_level=None)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            return connection
        except MCPReleaseError as exc:
            if connection is not None:
                connection.close()
            if exc.code in {"CURRENT_POINTER_INVALID", "INDEX_OPEN_FAILED"}:
                raise
            raise MCPReleaseError(
                "INDEX_OPEN_FAILED",
                "The pointer-selected release index could not be opened.",
                minecraft_version=version,
                details={"integrity_component": "index"},
            ) from exc
        except Exception as exc:
            if connection is not None:
                connection.close()
            raise MCPReleaseError(
                "INDEX_OPEN_FAILED",
                "The pointer-selected release index could not be opened.",
                minecraft_version=version,
                details={"integrity_component": "index"},
            ) from exc

    resolve_release = resolve


ReleaseResolver = MCPReleaseResolver


__all__ = [
    "MCPReleaseError",
    "MCPReleaseResolver",
    "MCPVersionInputError",
    "ReleaseHandle",
    "ReleaseResolver",
]
