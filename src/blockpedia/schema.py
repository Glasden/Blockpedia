"""Strict validation and namespace loading for Blockpedia records."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator


class RecordSchemaError(ValueError):
    def __init__(self, schema_id: str, errors: list[str], issues: list[dict[str, Any]] | None = None):
        self.schema_id = schema_id
        self.errors = errors
        # ``errors`` intentionally remains the bounded human/repair surface.
        # ``issues`` is the separately sanitized surface used for persisted
        # final diagnostics and never contains an instance value or message.
        self.issues = list(issues or [])
        super().__init__(f"{schema_id}: " + "; ".join(errors[:4]))

    @property
    def diagnostics(self) -> list[dict[str, Any]]:
        """Backward/forward compatible name for the safe issue list."""

        return self.issues

    @property
    def first_issue(self) -> dict[str, Any] | None:
        return self.issues[0] if self.issues else None


class UnknownSchemaError(ValueError):
    """Raised when a caller asks for a schema outside the frozen inventory."""


# This is deliberately an explicit inventory.  Prefix inference made it too
# easy for a typo or an old schema id to silently select the wrong namespace.
SCHEMA_NAMESPACES: dict[str, str] = {
    "export-manifest.v1": "exporter",
    "export-block.v1": "exporter",
    "export-state.v1": "exporter",
    "export-variant.v1": "exporter",
    "export-failure.v1": "exporter",
    "render-metadata.v1": "exporter",
    "block-record.v1": "workspace",
    "state-record.v1": "workspace",
    "visual-variant-record.v1": "workspace",
    "annotation-record.v1": "workspace",
    "manual-override.v1": "workspace",
    "skip-review.v1": "workspace",
    "qualification-review.v1": "workspace",
    "release-manifest.v1": "workspace",
    "release.v1": "workspace",
    "current-pointer.v1": "workspace",
    "provider-batch-envelope.v1": "provider",
    "annotation-batch-output.v1": "provider",
    "annotation-wire-item.v1": "provider",
    "query-spec-output.v1": "provider",
    "rerank-output.v1": "provider",
    "mcp-index-info-output.v1": "mcp",
    "mcp-search-blocks-output.v1": "mcp",
    "mcp-block-details-output.v1": "mcp",
    "mcp-compare-blocks-output.v1": "mcp",
    "mcp-error.v1": "mcp",
}


PROVIDER_WIRE_SCHEMA_IDS = frozenset(
    {
        "annotation-batch-output.v1",
        "query-spec-output.v1",
        "rerank-output.v1",
    }
)


def schema_namespace(schema_id: str) -> str:
    """Return the frozen namespace or fail deterministically."""

    try:
        return SCHEMA_NAMESPACES[schema_id]
    except (KeyError, TypeError) as exc:
        raise UnknownSchemaError(f"unknown schema id: {schema_id!r}") from exc


def repository_root() -> Path:
    # src/blockpedia/schema.py -> repository root in a source checkout.
    return Path(__file__).resolve().parents[2]


def load_schema(schema_id: str, *, repo_root: Path | None = None) -> dict[str, Any]:
    namespace = schema_namespace(schema_id)
    path = (repo_root or repository_root()) / "schemas" / namespace / f"{schema_id}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        # An inventory entry without a materialized file is a stable schema
        # loading failure, rather than an accidental namespace/path error.
        raise UnknownSchemaError(f"schema file is missing: {schema_id}") from exc


def load_provider_wire_schema(schema_id: str, *, repo_root: Path | None = None) -> dict[str, Any]:
    """Load one of the three production Responses wire schemas."""

    if schema_id not in PROVIDER_WIRE_SCHEMA_IDS:
        raise UnknownSchemaError(f"unknown provider wire schema id: {schema_id!r}")
    return load_schema(schema_id, repo_root=repo_root)


_SAFE_PATH_PART = re.compile(r"^[A-Za-z_][A-Za-z0-9_:-]{0,63}$")
_SAFE_KEYWORD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MISSING = object()
_MAX_DIAGNOSTIC_LENGTH = 4096


def _observed_type(value: Any) -> str:
    if value is _MISSING:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return "unknown"


def _observed_length(value: Any) -> int | None:
    if isinstance(value, (str, list, tuple, Mapping)):
        return min(len(value), _MAX_DIAGNOSTIC_LENGTH)
    return None


def _safe_path(path: Any) -> str:
    result = "$"
    for part in path:
        if isinstance(part, bool):
            return result
        if isinstance(part, int) and 0 <= part <= _MAX_DIAGNOSTIC_LENGTH:
            result += f"[{part}]"
        elif isinstance(part, str) and _SAFE_PATH_PART.fullmatch(part):
            result += "." + part
        else:
            # The path is a diagnostic label, not a place to echo an
            # attacker-controlled property name.
            return result
    return result


def _safe_keyword(value: Any) -> str:
    if isinstance(value, str) and _SAFE_KEYWORD.fullmatch(value):
        return value
    return "unknown"


def _structured_issue(error: Any) -> dict[str, Any]:
    instance = getattr(error, "instance", _MISSING)
    path: list[Any] = list(getattr(error, "path", ()))
    keyword = _safe_keyword(getattr(error, "validator", None))
    observed = instance
    if keyword == "required":
        required = getattr(error, "validator_value", ())
        missing_name: str | None = None
        if isinstance(required, (list, tuple)):
            for name in required:
                if isinstance(name, str) and (not isinstance(instance, Mapping) or name not in instance):
                    missing_name = name
                    break
        if isinstance(instance, Mapping) and isinstance(missing_name, str) and missing_name in instance:
            # Defensive only: a malformed validator error should not turn a
            # present value into a ``missing`` diagnostic.
            observed = instance.get(missing_name)
        else:
            observed = _MISSING
        if isinstance(missing_name, str) and _SAFE_PATH_PART.fullmatch(missing_name):
            path.append(missing_name)
    return {
        "stage": "offline_annotation",
        "phase": "wire_schema",
        "path": _safe_path(path),
        "keyword": keyword,
        "observed_type": _observed_type(observed),
        "observed_length": _observed_length(observed),
    }


def validate_record(schema_id: str, value: Any, *, repo_root: Path | None = None) -> None:
    validator = Draft202012Validator(load_schema(schema_id, repo_root=repo_root))
    errors = sorted(validator.iter_errors(value), key=lambda error: tuple(str(part) for part in error.path))
    if errors:
        messages = []
        for error in errors[:8]:
            location = ".".join(str(part) for part in error.path) or "$"
            messages.append(f"{location}: {error.message}")
        issues = [_structured_issue(error) for error in errors[:8]]
        raise RecordSchemaError(schema_id, messages, issues)
