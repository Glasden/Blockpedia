"""Data-root resolution and safe relative reference handling."""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

DATA_ROOT_ENV = "BLOCKPEDIA_DATA_ROOT"
MINECRAFT_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
EXPORT_ID_RE = re.compile(r"^export_[0-9]{8}T[0-9]{6}Z(?:_(?:0[1-9]|[1-9][0-9]))?$")


class UnsafeReference(ValueError):
    """A persisted or API path reference is not a safe POSIX relative ref."""


class ExportPathError(ValueError):
    """An export path is not the exact version-scoped R1 handoff location."""


def validate_minecraft_version(value: str) -> str:
    if not isinstance(value, str) or not MINECRAFT_VERSION_RE.fullmatch(value):
        raise ValueError("invalid minecraft_version")
    return value


def safe_relative_posix_ref(value: str) -> str:
    """Validate and return a portable persisted path reference."""

    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise UnsafeReference(value)
    if value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        raise UnsafeReference(value)
    parts = value.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise UnsafeReference(value)
    if PurePosixPath(value).is_absolute():
        raise UnsafeReference(value)
    return value


@dataclass(frozen=True, slots=True)
class DataRoot:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).expanduser().absolute())

    @property
    def current(self) -> Path:
        return self.root / "current.json"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    def export_dir(self, minecraft_version: str, export_id: str) -> Path:
        validate_minecraft_version(minecraft_version)
        if not EXPORT_ID_RE.fullmatch(export_id):
            raise ExportPathError("invalid export_id")
        return self.exports / minecraft_version / export_id

    def workspace_dir(self, minecraft_version: str, run_id: str) -> Path:
        validate_minecraft_version(minecraft_version)
        safe_relative_posix_ref(run_id)
        if "/" in run_id:
            raise UnsafeReference(run_id)
        return self.workspace / minecraft_version / run_id

    def relative_ref(self, path: Path) -> str:
        candidate = Path(path).absolute()
        try:
            relative = candidate.relative_to(self.root)
        except ValueError as exc:
            raise UnsafeReference(str(path)) from exc
        return safe_relative_posix_ref(relative.as_posix())

    def resolve_ref(self, ref: str) -> Path:
        safe_relative_posix_ref(ref)
        candidate = self.root.joinpath(*ref.split("/"))
        try:
            candidate.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise UnsafeReference(ref) from exc
        return candidate

    def ensure_layout(self) -> None:
        """Create only the frozen high-level data-root directories."""

        for forbidden in (self.root / "work", self.root / "published"):
            if forbidden.exists():
                raise ValueError("forbidden legacy data-root directory")
        for directory in (self.exports, self.workspace, self.cache, self.releases, self.logs):
            directory.mkdir(parents=True, exist_ok=True)

    def export_source(self, source: str | Path, minecraft_version: str) -> Path:
        """Accept only data-root/exports/<version>/<exact-export-id>.

        Symlinked directory components and staging directories are rejected so
        a check cannot silently cross a version or leave the data root.
        """

        validate_minecraft_version(minecraft_version)
        raw = Path(source).expanduser()
        try:
            candidate = raw.absolute()
            expected_parent = (self.exports / minecraft_version).absolute()
            if candidate.parent != expected_parent:
                raise ExportPathError("source must be directly under the selected version")
            if not EXPORT_ID_RE.fullmatch(candidate.name):
                raise ExportPathError("staging or invalid export directory")
            if not candidate.is_dir() or candidate.is_symlink():
                raise ExportPathError("export directory is missing or symlinked")
            # Every directory component after data-root must remain a real
            # directory. This blocks a symlinked version or exports component.
            for component in (self.exports, expected_parent, candidate):
                if component.is_symlink():
                    raise ExportPathError("export path contains a symlink")
            candidate.resolve().relative_to(self.root.resolve())
        except (OSError, ValueError) as exc:
            if isinstance(exc, ExportPathError):
                raise
            raise ExportPathError("export path is outside data-root") from exc
        return candidate


def default_data_root(*, platform: str | None = None, environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if environ is None else environ
    if DATA_ROOT_ENV in env and env[DATA_ROOT_ENV]:
        return Path(env[DATA_ROOT_ENV]).expanduser().absolute()
    platform_name = platform or sys.platform
    if platform_name.startswith("win"):
        local_app_data = env.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "Blockpedia" / "data"
        return (home or Path.home()) / "AppData" / "Local" / "Blockpedia" / "data"
    xdg = env.get("XDG_DATA_HOME")
    home_path = home or (Path(env["HOME"]) if env.get("HOME") else Path.home())
    return (Path(xdg) if xdg else home_path / ".local" / "share") / "blockpedia"


def resolve_data_root(data_root: str | Path | None = None, *, environ: Mapping[str, str] | None = None) -> DataRoot:
    env = os.environ if environ is None else environ
    selected = Path(data_root).expanduser() if data_root is not None else default_data_root(environ=env)
    return DataRoot(selected)
