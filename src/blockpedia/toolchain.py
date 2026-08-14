"""Small injectable R2 PREPARE probe.

The default probe checks the real interpreter and repository lock/configuration.
Tests may inject a probe object; no CLI or environment value can override it.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .storage import packaged_schema


@dataclass(frozen=True, slots=True)
class ToolchainProbe:
    repo_root: Path
    python_version_getter: Callable[[], str] | None = None

    def check(self) -> dict[str, Any]:
        actual_python = self.python_version_getter() if self.python_version_getter else platform.python_version()
        pyproject = self.repo_root / "pyproject.toml"
        requirements_in = self.repo_root / "requirements.in"
        requirements_lock = self.repo_root / "requirements.lock"
        config_ok = pyproject.is_file() and 'requires-python = "==3.14.7"' in pyproject.read_text(encoding="utf-8")
        lock_ok = requirements_in.is_file() and requirements_lock.is_file() and _lock_contains_inputs(requirements_in, requirements_lock)
        try:
            schema_sql, schema_hash = packaged_schema()
            schema_ok = bool(schema_sql) and schema_hash.startswith("sha256:")
        except Exception:
            schema_ok, schema_hash = False, None
        passed = actual_python == "3.14.7" and config_ok and lock_ok and schema_ok
        return {
            "python_version": actual_python,
            "expected_python_version": "3.14.7",
            "platform": platform.system(),
            "config_ok": config_ok,
            "lock_ok": lock_ok,
            "schema_ok": schema_ok,
            "schema_sha256": schema_hash,
            "passed": passed,
        }


def _lock_contains_inputs(requirements_in: Path, requirements_lock: Path) -> bool:
    lock = requirements_lock.read_text(encoding="utf-8")
    for line in requirements_in.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        package = line.split("==", 1)[0].strip().lower()
        version = line.split("==", 1)[1].strip() if "==" in line else ""
        if f"{package}=={version}" not in lock.lower():
            return False
    return True
