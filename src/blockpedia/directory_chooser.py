"""Process-local, path-free references for the export directory chooser.

The chooser is deliberately kept outside the persisted import/check model.  A
reference is useful only to this process and is backed by a directory identity
which is checked again every time the reference is consumed.
"""

from __future__ import annotations

import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .paths import DataRoot, EXPORT_ID_RE, validate_minecraft_version


class DirectoryChooserError(RuntimeError):
    code = "DIRECTORY_REF_INVALID"


class DirectoryRefNotFound(DirectoryChooserError):
    code = "DIRECTORY_REF_NOT_FOUND"


class DirectoryRefInvalid(DirectoryChooserError):
    code = "DIRECTORY_REF_INVALID"


class DirectoryRefStale(DirectoryChooserError):
    code = "DIRECTORY_REF_STALE"


class DirectoryPathUnsafe(DirectoryChooserError):
    code = "DIRECTORY_PATH_UNSAFE"


@dataclass(frozen=True, slots=True)
class DirectoryIdentity:
    device: int
    inode: int
    change_time_ns: int
    size: int


@dataclass(frozen=True, slots=True)
class DirectoryRef:
    ref: str
    minecraft_version: str
    root: Path
    path: Path
    identity: DirectoryIdentity
    expires_at: float


class DirectoryChooser:
    """List and consume directories below one exact version root."""

    def __init__(self, data_root: DataRoot, *, ttl_seconds: float = 900.0):
        self.data_root = data_root
        self.ttl_seconds = ttl_seconds
        self._refs: dict[str, DirectoryRef] = {}
        self._lock = threading.RLock()

    def list_directories(self, minecraft_version: str, parent_ref: str | None = None) -> dict[str, Any]:
        version = validate_minecraft_version(minecraft_version)
        root = self._version_root(version, create=True)
        parent = root
        if parent_ref is not None:
            parent_record = self._consume(parent_ref, version, require_export=False)
            parent = parent_record.path
        self._validate_directory(parent, root, version)

        entries: list[dict[str, Any]] = []
        try:
            children = sorted(parent.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise DirectoryPathUnsafe from exc
        for child in children:
            # A link in a chooser tree is an unsafe input, not an entry to
            # silently hide.  Ordinary files are simply not directories.
            if child.is_symlink() or _is_reparse_point(child):
                raise DirectoryPathUnsafe
            if not child.is_dir():
                continue
            self._validate_directory(child, root, version)
            if parent == root and not EXPORT_ID_RE.fullmatch(child.name):
                # The version root is an export namespace, so unrelated
                # directories are not selectable and are not exposed.
                continue
            ref = self._issue(version, root, child)
            preflight = _preflight(child, version)
            selectable = bool(preflight["export_id"] and preflight["readiness"] == "ready")
            entries.append(
                {
                    "label": child.name,
                    "name": child.name,
                    "ref": ref,
                    "directory_ref": ref,
                    "export_id": preflight["export_id"],
                    "minecraft_version": version,
                    "preflight": preflight,
                    "preflight_status": preflight["readiness"],
                    "selectable": selectable,
                    "can_enter": True,
                }
            )
        return {
            "minecraft_version": version,
            "parent_ref": parent_ref,
            "label": f"Minecraft {version} exports",
            "entries": entries,
        }

    def describe(self, ref: str, minecraft_version: str) -> dict[str, Any]:
        """Validate a ref without returning its path to an HTTP caller."""

        record = self._consume(ref, minecraft_version, require_export=True)
        return {
            "ref": record.ref,
            "export_id": record.path.name,
            "minecraft_version": record.minecraft_version,
            "preflight": _preflight(record.path, record.minecraft_version),
        }

    def consume(self, ref: str, minecraft_version: str) -> Path:
        """Return a verified source path for the short-lived worker closure."""

        return self._consume(ref, minecraft_version, require_export=True).path

    def register_path(self, path: str | Path, minecraft_version: str) -> str:
        """Register a trusted internal path for compatibility with service tests.

        HTTP routes never accept this form; they accept only refs produced by
        :meth:`list_directories`.
        """

        version = validate_minecraft_version(minecraft_version)
        root = self._version_root(version, create=False)
        candidate = Path(path).absolute()
        self._validate_directory(candidate, root, version)
        if not EXPORT_ID_RE.fullmatch(candidate.name):
            raise DirectoryPathUnsafe
        return self._issue(version, root, candidate)

    def validate_ref(self, ref: str, minecraft_version: str) -> None:
        self._consume(ref, minecraft_version, require_export=True)

    def _version_root(self, version: str, *, create: bool) -> Path:
        root = self.data_root.exports / version
        # Do not let mkdir or resolve follow a replaced exports component.
        for container in (self.data_root.root, self.data_root.exports):
            try:
                value = container.lstat()
            except OSError as exc:
                raise DirectoryPathUnsafe from exc
            if not stat.S_ISDIR(value.st_mode) or _is_reparse_stat(value):
                raise DirectoryPathUnsafe
        if create:
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise DirectoryPathUnsafe from exc
        if root.exists():
            value = _safe_lstat(root)
            if not stat.S_ISDIR(value.st_mode) or _is_reparse_stat(value):
                raise DirectoryPathUnsafe
        elif not create:
            raise DirectoryRefNotFound
        return root

    def _issue(self, version: str, root: Path, path: Path) -> str:
        self._validate_directory(path, root, version)
        try:
            canonical_root = root.resolve(strict=True)
            canonical_path = path.resolve(strict=True)
        except OSError as exc:
            raise DirectoryPathUnsafe from exc
        ref = "dir_" + secrets.token_urlsafe(32)
        record = DirectoryRef(
            ref=ref,
            minecraft_version=version,
            root=canonical_root,
            path=canonical_path,
            identity=_identity(canonical_path),
            expires_at=time.monotonic() + self.ttl_seconds,
        )
        with self._lock:
            self._refs[ref] = record
        return ref

    def _consume(self, ref: str, version: str, *, require_export: bool) -> DirectoryRef:
        if not isinstance(ref, str) or not ref.startswith("dir_") or len(ref) < 40:
            raise DirectoryRefInvalid
        with self._lock:
            record = self._refs.get(ref)
        if record is None or record.expires_at <= time.monotonic():
            with self._lock:
                self._refs.pop(ref, None)
            raise DirectoryRefNotFound
        try:
            requested_version = validate_minecraft_version(version)
        except ValueError as exc:
            raise DirectoryPathUnsafe from exc
        if record.minecraft_version != requested_version:
            raise DirectoryPathUnsafe
        try:
            root = self._version_root(requested_version, create=False)
        except DirectoryRefNotFound as exc:
            raise DirectoryRefStale from exc
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as exc:
            raise DirectoryRefStale from exc
        if canonical_root != record.root:
            raise DirectoryRefStale
        try:
            self._validate_directory(record.path, root, requested_version)
        except DirectoryRefNotFound as exc:
            raise DirectoryRefStale from exc
        if _identity(record.path) != record.identity:
            raise DirectoryRefStale
        if require_export and not EXPORT_ID_RE.fullmatch(record.path.name):
            raise DirectoryPathUnsafe
        # Return a fresh canonical path only after all checks have passed.
        try:
            canonical = record.path.resolve(strict=True)
        except OSError as exc:
            raise DirectoryRefStale from exc
        if canonical != record.path:
            raise DirectoryRefStale
        return record

    def _validate_directory(self, path: Path, root: Path, version: str) -> None:
        del version
        if not path.is_absolute():
            raise DirectoryPathUnsafe
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise DirectoryPathUnsafe from exc
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise DirectoryPathUnsafe
        try:
            root_stat = _safe_lstat(root)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise DirectoryPathUnsafe
            for index in range(len(relative.parts) + 1):
                current = root.joinpath(*relative.parts[:index])
                current_stat = _safe_lstat(current)
                if not stat.S_ISDIR(current_stat.st_mode) or _is_reparse_stat(current_stat) or _is_reparse_point(current):
                    raise DirectoryPathUnsafe
                # A directory crossing into another filesystem is not part of
                # the selected data-root namespace.
                if current_stat.st_dev != root_stat.st_dev:
                    raise DirectoryPathUnsafe
            canonical = path.resolve(strict=True)
            canonical.relative_to(root.resolve(strict=True))
        except DirectoryChooserError:
            raise
        except (OSError, ValueError) as exc:
            raise DirectoryPathUnsafe from exc


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise DirectoryRefNotFound from exc


def _is_reparse_stat(value: os.stat_result) -> bool:
    if stat.S_ISLNK(value.st_mode):
        return True
    return bool(getattr(value, "st_file_attributes", 0) & 0x400)


def _is_reparse_point(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if callable(is_junction) and is_junction():
            return True
        return _is_reparse_stat(path.lstat())
    except OSError:
        return True


def _identity(path: Path) -> DirectoryIdentity:
    try:
        value = path.lstat()
    except OSError as exc:
        raise DirectoryRefNotFound from exc
    if not stat.S_ISDIR(value.st_mode) or _is_reparse_stat(value):
        raise DirectoryPathUnsafe
    return DirectoryIdentity(
        device=int(value.st_dev),
        inode=int(value.st_ino),
        change_time_ns=int(getattr(value, "st_ctime_ns", 0)),
        size=int(value.st_size),
    )


def _preflight(path: Path, version: str) -> dict[str, Any]:
    """Report entry-point presence only; never hash or snapshot here."""

    required = {
        "manifest.json": _regular_file(path / "manifest.json"),
        "checksums.sha256": _regular_file(path / "checksums.sha256"),
    }
    return {
        "export_id": path.name if EXPORT_ID_RE.fullmatch(path.name) else None,
        "minecraft_version": version,
        "readiness": "ready" if all(required.values()) else "incomplete",
        "required_entry_points": required,
    }


def _regular_file(path: Path) -> bool:
    try:
        return path.is_file() and not _is_reparse_point(path) and path.lstat().st_nlink == 1
    except OSError:
        return False


__all__ = [
    "DirectoryChooser",
    "DirectoryChooserError",
    "DirectoryPathUnsafe",
    "DirectoryRefInvalid",
    "DirectoryRefNotFound",
    "DirectoryRefStale",
]
