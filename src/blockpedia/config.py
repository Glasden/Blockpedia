"""Non-HTTP configuration API reserved for the future CLI/WebUI adapters."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .paths import DataRoot, default_data_root, resolve_data_root


@dataclass(frozen=True, slots=True)
class AppConfig:
    data_root: DataRoot

    @classmethod
    def resolve(cls, data_root: str | Path | None = None, *, environ: Mapping[str, str] | None = None) -> "AppConfig":
        return cls(resolve_data_root(data_root, environ=environ))


__all__ = ["AppConfig", "DataRoot", "default_data_root", "resolve_data_root"]
