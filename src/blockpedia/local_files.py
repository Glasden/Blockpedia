"""Local path safety and atomic file operations, without history/hash checks."""
from __future__ import annotations

import os
import stat
import threading
from pathlib import Path

from .paths import safe_relative_posix_ref


def safe_path(path: Path, root: Path, *, directory: bool | None = None, missing: bool = False) -> Path:
    path, root = Path(path).absolute(), Path(root).absolute()
    relative = path.relative_to(root)
    if relative.parts:
        safe_relative_posix_ref(relative.as_posix())
    current = root
    for part in (None, *relative.parts):
        if part is not None:
            current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if missing:
                return path
            raise
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked path is not allowed")
        if current != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("parent is not a directory")
        if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
            raise ValueError("hardlinked file is not allowed")
    if directory is True and not stat.S_ISDIR(info.st_mode):
        raise ValueError("directory required")
    if directory is False and not stat.S_ISREG(info.st_mode):
        raise ValueError("regular file required")
    return path


def write_bytes(path: Path, payload: bytes, root: Path) -> None:
    safe_path(path, root, missing=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return  # Windows directory descriptors are not portable.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        safe_path(path, root)
        if path.is_dir():
            directories.append(path)
        else:
            with path.open("r+b" if os.name == "nt" else "rb") as handle:
                os.fsync(handle.fileno())
    for directory in reversed(directories):
        sync_directory(directory)


# ponytail: one in-process commit lock; the Studio writer lease excludes other writers.
_COMMIT_LOCK = threading.Lock()


def commit_directory(staging: Path, final: Path) -> None:
    with _COMMIT_LOCK:
        if final.exists() or final.is_symlink():
            raise FileExistsError(final)
        if os.name == "nt":
            _windows_move(staging, final, replace=False)
        else:
            os.rename(staging, final)


class StudioLease:
    """One writable Studio per data root; OS releases the lock on process exit."""

    def __init__(self, data_root):
        path = data_root.cache / "studio.lock"
        safe_path(path, data_root.root, missing=True)
        self.handle = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if path.stat().st_size == 0:
                    self.handle.write(b"\0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("STUDIO_ALREADY_RUNNING") from None

    def close(self):
        self.handle.close()


def _windows_move(source: Path, target: Path, *, replace: bool) -> None:
    import ctypes
    from ctypes import wintypes
    move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    if not move(str(source), str(target), 8 | (1 if replace else 0)):
        raise ctypes.WinError(ctypes.get_last_error())


def replace_file(source: Path, target: Path) -> None:
    if os.name == "nt":
        _windows_move(source, target, replace=True)
    else:
        os.replace(source, target)
