"""Strict validation for exporter and workspace records."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator


class RecordSchemaError(ValueError):
    def __init__(self, schema_id: str, errors: list[str]):
        self.schema_id = schema_id
        self.errors = errors
        super().__init__(f"{schema_id}: " + "; ".join(errors[:4]))


def repository_root() -> Path:
    # src/blockpedia/schema.py -> repository root in a source checkout.
    return Path(__file__).resolve().parents[2]


def load_schema(schema_id: str, *, repo_root: Path | None = None) -> dict[str, Any]:
    namespace = "exporter" if schema_id.startswith("export-") or schema_id == "render-metadata.v1" else "workspace"
    path = (repo_root or repository_root()) / "schemas" / namespace / f"{schema_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_record(schema_id: str, value: dict[str, Any], *, repo_root: Path | None = None) -> None:
    validator = Draft202012Validator(load_schema(schema_id, repo_root=repo_root))
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        messages = []
        for error in errors[:8]:
            location = ".".join(str(part) for part in error.path) or "$"
            messages.append(f"{location}: {error.message}")
        raise RecordSchemaError(schema_id, messages)
