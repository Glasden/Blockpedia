"""Validate the frozen R0 JSON Schemas and their repository fixtures.

This module deliberately stays a small static validation path.  It validates
JSON Schema syntax and fixture instances, but does not attempt to implement
cross-record or release-building rules.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError, ValidationError


SCHEMA_ROOT = Path("schemas")
REPORT_PATH = Path("docs/evidence/r0-schema-report.json")
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"

EXPECTED_SCHEMAS: Mapping[str, tuple[str, ...]] = {
    "exporter": (
        "export-manifest.v1",
        "export-block.v1",
        "export-state.v1",
        "export-variant.v1",
        "export-failure.v1",
        "render-metadata.v1",
    ),
    "workspace": (
        "block-record.v1",
        "state-record.v1",
        "visual-variant-record.v1",
        "annotation-record.v1",
        "manual-override.v1",
        "skip-review.v1",
        "qualification-review.v1",
        "release-manifest.v1",
        "release.v1",
        "current-pointer.v1",
    ),
    "provider": (
        "provider-batch-envelope.v1",
        "annotation-batch-output.v1",
        "annotation-wire-item.v1",
        "query-spec-output.v1",
        "rerank-output.v1",
    ),
    "mcp": (
        "mcp-index-info-output.v1",
        "mcp-search-blocks-output.v1",
        "mcp-block-details-output.v1",
        "mcp-compare-blocks-output.v1",
        "mcp-error.v1",
    ),
}

EXPECTED_IDS = frozenset(
    schema_id for schema_ids in EXPECTED_SCHEMAS.values() for schema_id in schema_ids
)
EXPECTED_PATHS = {
    Path("schemas") / namespace / f"{schema_id}.json": schema_id
    for namespace, schema_ids in EXPECTED_SCHEMAS.items()
    for schema_id in schema_ids
}


def _expected_schema_uri(namespace: str, schema_id: str) -> str:
    return f"urn:blockpedia:schema:{namespace}:{schema_id}"


EXPECTED_URIS = {
    Path("schemas") / namespace / f"{schema_id}.json": _expected_schema_uri(
        namespace, schema_id
    )
    for namespace, schema_ids in EXPECTED_SCHEMAS.items()
    for schema_id in schema_ids
}
PROVIDER_WIRE_IDS = frozenset(
    {"annotation-batch-output.v1", "query-spec-output.v1", "rerank-output.v1"}
)
OLD_IDS = frozenset(
    {
        "block.v1",
        "state.v1",
        "variant.v1",
        "annotation.v1",
        "annotation-item.v1",
        "query-spec.v1",
        "visual-rerank.v1",
        "manifest.v1",
        "override.v1",
    }
)

_FIXTURE_KIND_TOKENS = {
    "valid": "valid",
    "positive": "valid",
    "invalid": "invalid",
    "negative": "invalid",
    "rejected": "invalid",
    "reject": "invalid",
}
_FIXTURE_TOKEN_RE = re.compile(
    r"(?:^|[._-])(valid|positive|invalid|negative|rejected|reject)(?:$|[._-])",
    re.IGNORECASE,
)
_SCHEMA_TOKEN_RE = re.compile(r"[^a-z0-9]+", re.IGNORECASE)


class R0ValidationError(RuntimeError):
    """Raised when the repository does not satisfy the R0 checks."""

    def __init__(self, report: Mapping[str, Any]):
        self.report = dict(report)
        super().__init__("R0 schema validation failed")


def _sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _read_json(path: Path) -> tuple[Any | None, str | None]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, type(exc).__name__
    return value, None


def _iter_json_files(directory: Path) -> Iterator[Path]:
    if not directory.is_dir():
        return
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".json", ".jsonl"}:
            yield path


def _fixture_roots(repo_root: Path) -> tuple[Path, ...]:
    """Find conventional fixture roots without treating docs as fixtures."""

    roots: set[Path] = set()
    candidates = (
        repo_root / "fixtures",
        repo_root / "tests" / "fixtures",
        repo_root / "schemas" / "fixtures",
    )
    for candidate in candidates:
        if candidate.is_dir():
            roots.add(candidate.resolve())

    # Some schema/fixture writers use a nested ``r0/fixtures`` directory.
    # Keep this discovery bounded to repository paths and ignore generated
    # environments so they cannot accidentally become contract input.
    ignored_parts = {".git", ".venv", ".pytest_cache", "__pycache__", "wheelhouse"}
    for path in repo_root.rglob("fixtures"):
        if not path.is_dir() or any(part in ignored_parts for part in path.parts):
            continue
        if "docs" in path.relative_to(repo_root).parts:
            continue
        roots.add(path.resolve())
    return tuple(sorted(roots))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _schema_files(repo_root: Path, fixture_roots: Sequence[Path]) -> tuple[Path, ...]:
    schema_root = repo_root / SCHEMA_ROOT
    if not schema_root.is_dir():
        return ()
    return tuple(
        path
        for path in _iter_json_files(schema_root)
        if not any(_is_relative_to(path, fixture_root) for fixture_root in fixture_roots)
    )


def _declares_object(schema: Mapping[str, Any]) -> bool:
    schema_type = schema.get("type")
    if schema_type == "object":
        return True
    if isinstance(schema_type, list) and "object" in schema_type:
        return True
    return "properties" in schema or "additionalProperties" in schema


def _walk_schema(node: Any, location: str = "$", seen: set[int] | None = None) -> Iterator[tuple[str, Mapping[str, Any]]]:
    """Yield schema dictionaries and their JSON-ish locations."""

    if seen is None:
        seen = set()
    if isinstance(node, Mapping):
        identity = id(node)
        if identity in seen:
            return
        seen.add(identity)
        yield location, node
        for key, value in node.items():
            child_location = f"{location}.{key}"
            if isinstance(value, list):
                for index, item in enumerate(value):
                    yield from _walk_schema(item, f"{child_location}[{index}]", seen)
            elif isinstance(value, Mapping):
                yield from _walk_schema(value, child_location, seen)


def _old_id_values(node: Any) -> Iterator[str]:
    if isinstance(node, Mapping):
        for value in node.values():
            yield from _old_id_values(value)
    elif isinstance(node, list):
        for value in node:
            yield from _old_id_values(value)
    elif isinstance(node, str):
        for old_id in OLD_IDS:
            if node == old_id or node.endswith(f":{old_id}") or node.endswith(f"/{old_id}"):
                yield old_id


def _provider_static_errors(schema: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for location, node in _walk_schema(schema):
        for forbidden_key in ("$ref", "$defs", "patternProperties"):
            if forbidden_key in node:
                errors.append(f"{location}:{forbidden_key}")
        if _declares_object(node) and node.get("additionalProperties") is not False:
            errors.append(f"{location}:additionalProperties")
    return errors


def _schema_token(value: str) -> str:
    return _SCHEMA_TOKEN_RE.sub("", value.casefold())


def _schema_id_from_fixture(path: Path, instance: Any) -> tuple[str | None, str | None]:
    path_text = "/".join(path.parts).casefold()
    # Match the schema filename portion rather than the whole fixture path;
    # this keeps discovery stable for paths such as ``valid/foo.v1.json`` and
    # avoids relying on a particular namespace directory layout.
    file_stem = path.name.casefold()
    path_candidates = [
        schema_id
        for schema_id in EXPECTED_IDS
        if _schema_token(schema_id) in _schema_token(file_stem)
        or schema_id.casefold() in file_stem
    ]
    path_candidates = sorted(set(path_candidates))

    instance_id: str | None = None
    if isinstance(instance, Mapping):
        for key in ("schema_version", "schema_id"):
            value = instance.get(key)
            if isinstance(value, str) and value in EXPECTED_IDS:
                if instance_id is not None and instance_id != value:
                    return None, "fixture_schema_id_conflict"
                instance_id = value

    if instance_id is not None:
        if path_candidates and instance_id not in path_candidates:
            return None, "fixture_path_schema_id_conflict"
        return instance_id, None
    if len(path_candidates) == 1:
        return path_candidates[0], None
    if not path_candidates:
        return None, "fixture_schema_id_missing"
    return None, "fixture_schema_id_ambiguous"


def _fixture_kind(path: Path) -> tuple[str | None, str | None]:
    matches: set[str] = set()
    for part in path.parts:
        match = _FIXTURE_TOKEN_RE.search(part)
        if match:
            matches.add(_FIXTURE_KIND_TOKENS[match.group(1).casefold()])
        elif part.casefold() in _FIXTURE_KIND_TOKENS:
            matches.add(_FIXTURE_KIND_TOKENS[part.casefold()])
    if len(matches) == 1:
        return next(iter(matches)), None
    if len(matches) > 1:
        return None, "fixture_kind_ambiguous"
    return None, "fixture_kind_missing"


def _fixture_instances(path: Path) -> tuple[list[Any], str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return [], type(exc).__name__
    try:
        if path.suffix.casefold() == ".jsonl":
            instances: list[Any] = []
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    instances.append(json.loads(line))
                except json.JSONDecodeError:
                    return [], f"jsonl_parse_error_line_{line_number}"
            return instances, None
        value = json.loads(text)
    except (UnicodeError, json.JSONDecodeError) as exc:
        return [], type(exc).__name__
    if isinstance(value, list):
        return value, None
    return [value], None


def _validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    # The frozen schemas currently use local JSON Pointer references.  Let
    # jsonschema resolve those against the schema document itself; no schema
    # registry or cross-record validation framework is needed for R0.
    return Draft202012Validator(schema, format_checker=FormatChecker())


def _new_report() -> dict[str, Any]:
    return {
        "status": "failed",
        "schema_draft": SCHEMA_DRAFT,
        "schemas": {
            "expected_count": len(EXPECTED_PATHS),
            "discovered_count": 0,
            "valid_count": 0,
            "hashes": [],
        },
        "fixtures": {
            "roots": [],
            "files": 0,
            "cases": 0,
            "valid_cases": 0,
            "invalid_cases": 0,
            "coverage": {},
            "hashes": [],
        },
        "checks": {
            "schema_inventory": False,
            "schema_drafts": False,
            "schema_ids": False,
            "root_objects_closed": False,
            "old_ids_absent": False,
            "provider_wire_subset": False,
            "fixtures_validated": False,
            "invalid_fixtures_rejected": False,
            "vocab_artifact_absent": False,
        },
        "errors": [],
    }


def _add_error(report: dict[str, Any], code: str, **details: Any) -> None:
    error: dict[str, Any] = {"code": code}
    for key, value in details.items():
        if value is not None:
            error[key] = value
    report["errors"].append(error)


def _write_report(repo_root: Path, report: Mapping[str, Any]) -> Path:
    destination = repo_root / REPORT_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(destination)
    return destination


def validate_repository(
    repo_root: str | Path | None = None, *, report: bool = False
) -> dict[str, Any]:
    """Validate the repository and return a command-neutral report.

    ``R0ValidationError.report`` contains the same report when validation
    fails.  A report file is written only when ``report=True``.
    """

    root = Path(repo_root or Path(__file__).resolve().parents[1]).resolve()
    result = _new_report()
    fixture_roots = _fixture_roots(root)
    result["fixtures"]["roots"] = [
        _relative_path(path, root) for path in fixture_roots
    ]

    schema_root = root / SCHEMA_ROOT
    schema_paths = _schema_files(root, fixture_roots)
    result["schemas"]["discovered_count"] = len(schema_paths)
    parsed_schemas: dict[str, tuple[Path, Mapping[str, Any]]] = {}

    if not schema_root.is_dir():
        _add_error(result, "SCHEMA_ROOT_MISSING", path="schemas")

    expected_relative = set(EXPECTED_PATHS)
    actual_relative = {
        path.resolve().relative_to(root).as_posix(): path for path in schema_paths
    }
    expected_relative_text = {path.as_posix() for path in expected_relative}
    for missing in sorted(expected_relative_text - set(actual_relative)):
        _add_error(result, "SCHEMA_MISSING", path=missing)
    for extra in sorted(set(actual_relative) - expected_relative_text):
        _add_error(result, "SCHEMA_UNEXPECTED", path=extra)

    for path in schema_paths:
        relative = Path(_relative_path(path, root))
        try:
            raw = path.read_bytes()
        except OSError as exc:
            _add_error(
                result,
                "SCHEMA_READ_FAILED",
                path=relative.as_posix(),
                error=type(exc).__name__,
            )
            continue
        result["schemas"]["hashes"].append(
            {"path": relative.as_posix(), "sha256": _sha256_bytes(raw)}
        )
        value, parse_error = _read_json(path)
        if parse_error is not None:
            _add_error(
                result,
                "SCHEMA_PARSE_FAILED",
                path=relative.as_posix(),
                error=parse_error,
            )
            continue
        if not isinstance(value, Mapping):
            _add_error(result, "SCHEMA_NOT_OBJECT", path=relative.as_posix())
            continue

        expected_id = EXPECTED_PATHS.get(relative)
        actual_id = value.get("$id")
        expected_uri = EXPECTED_URIS.get(relative)
        if expected_uri is not None and actual_id != expected_uri:
            _add_error(
                result,
                "SCHEMA_ID_MISMATCH",
                path=relative.as_posix(),
                expected=expected_uri,
            )
        if isinstance(actual_id, str) and expected_id is not None:
            if expected_id in parsed_schemas:
                _add_error(result, "SCHEMA_ID_DUPLICATE", schema_id=expected_id)
            else:
                parsed_schemas[expected_id] = (path, value)
        if value.get("$schema") != SCHEMA_DRAFT:
            _add_error(
                result,
                "SCHEMA_DRAFT_MISMATCH",
                path=relative.as_posix(),
            )
        if value.get("additionalProperties") is not False:
            _add_error(
                result,
                "SCHEMA_ROOT_NOT_CLOSED",
                path=relative.as_posix(),
            )
        old_values = sorted(set(_old_id_values(value)))
        if old_values:
            _add_error(
                result,
                "OLD_SCHEMA_ID_PRESENT",
                path=relative.as_posix(),
                ids=old_values,
            )
        try:
            Draft202012Validator.check_schema(value)
            result["schemas"]["valid_count"] += 1
        except SchemaError:
            _add_error(
                result,
                "SCHEMA_INVALID",
                path=relative.as_posix(),
            )

    result["schemas"]["hashes"].sort(key=lambda item: item["path"])
    result["checks"]["schema_inventory"] = (
        len(schema_paths) == len(EXPECTED_PATHS)
        and not any(
            error["code"] in {"SCHEMA_MISSING", "SCHEMA_UNEXPECTED"}
            for error in result["errors"]
        )
    )
    result["checks"]["schema_drafts"] = (
        result["schemas"]["valid_count"] == len(schema_paths)
        and not any(
            error["code"]
            in {"SCHEMA_PARSE_FAILED", "SCHEMA_NOT_OBJECT", "SCHEMA_INVALID", "SCHEMA_DRAFT_MISMATCH"}
            for error in result["errors"]
        )
    )
    result["checks"]["schema_ids"] = not any(
        error["code"] in {"SCHEMA_ID_MISMATCH", "SCHEMA_ID_DUPLICATE"}
        for error in result["errors"]
    ) and set(parsed_schemas) == EXPECTED_IDS
    result["checks"]["root_objects_closed"] = not any(
        error["code"] == "SCHEMA_ROOT_NOT_CLOSED" for error in result["errors"]
    )
    result["checks"]["old_ids_absent"] = not any(
        error["code"] == "OLD_SCHEMA_ID_PRESENT" for error in result["errors"]
    )

    vocab_paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name.casefold() in {"vocab.json", "vocab.v1.json"}
        and ".git" not in path.parts
        and ".venv" not in path.parts
        and ".pytest_cache" not in path.parts
        and "__pycache__" not in path.parts
        and "wheelhouse" not in path.parts
    )
    if vocab_paths:
        for path in vocab_paths:
            _add_error(
                result,
                "VOCAB_ARTIFACT_PRESENT",
                path=_relative_path(path, root),
            )
    result["checks"]["vocab_artifact_absent"] = not vocab_paths

    provider_ok = True
    for wire_id in sorted(PROVIDER_WIRE_IDS):
        record = parsed_schemas.get(wire_id)
        if record is None:
            provider_ok = False
            _add_error(result, "PROVIDER_WIRE_SCHEMA_MISSING", schema_id=wire_id)
            continue
        provider_errors = _provider_static_errors(record[1])
        if provider_errors:
            provider_ok = False
            _add_error(
                result,
                "PROVIDER_WIRE_SCHEMA_UNSUPPORTED",
                schema_id=wire_id,
                locations=sorted(provider_errors),
            )
    result["checks"]["provider_wire_subset"] = provider_ok

    validators: dict[str, Draft202012Validator] = {}
    for schema_id in sorted(EXPECTED_IDS):
        record = parsed_schemas.get(schema_id)
        if record is None:
            continue
        try:
            validators[schema_id] = _validator(record[1])
        except (SchemaError, ValueError):
            _add_error(result, "SCHEMA_VALIDATOR_UNAVAILABLE", schema_id=schema_id)

    fixture_paths: list[Path] = []
    for fixture_root in fixture_roots:
        fixture_paths.extend(_iter_json_files(fixture_root))
    fixture_paths = sorted(set(path.resolve() for path in fixture_paths))
    result["fixtures"]["files"] = len(fixture_paths)
    fixture_coverage: dict[str, dict[str, int]] = {
        schema_id: {"valid": 0, "invalid": 0} for schema_id in EXPECTED_IDS
    }
    for path in fixture_paths:
        relative = _relative_path(path, root)
        try:
            result["fixtures"]["hashes"].append(
                {"path": relative, "sha256": _sha256_bytes(path.read_bytes())}
            )
        except OSError as exc:
            _add_error(result, "FIXTURE_READ_FAILED", path=relative, error=type(exc).__name__)
            continue
        instances, parse_error = _fixture_instances(path)
        if parse_error is not None:
            _add_error(result, "FIXTURE_PARSE_FAILED", path=relative, error=parse_error)
            continue
        kind, kind_error = _fixture_kind(path)
        if kind_error is not None:
            _add_error(result, "FIXTURE_KIND_INVALID", path=relative, error=kind_error)
            continue
        assert kind is not None
        for index, instance in enumerate(instances):
            result["fixtures"]["cases"] += 1
            schema_id, schema_error = _schema_id_from_fixture(path, instance)
            if schema_error is not None or schema_id is None:
                _add_error(
                    result,
                    "FIXTURE_SCHEMA_ID_INVALID",
                    path=relative,
                    case=index,
                    error=schema_error,
                )
                continue
            validator = validators.get(schema_id)
            if validator is None:
                _add_error(
                    result,
                    "FIXTURE_SCHEMA_UNAVAILABLE",
                    path=relative,
                    case=index,
                    schema_id=schema_id,
                )
                continue
            if kind == "valid":
                fixture_coverage[schema_id]["valid"] += 1
                result["fixtures"]["valid_cases"] += 1
                try:
                    validator.validate(instance)
                except ValidationError:
                    _add_error(
                        result,
                        "VALID_FIXTURE_REJECTED",
                        path=relative,
                        case=index,
                        schema_id=schema_id,
                    )
            else:
                fixture_coverage[schema_id]["invalid"] += 1
                result["fixtures"]["invalid_cases"] += 1
                try:
                    validator.validate(instance)
                except ValidationError:
                    pass
                else:
                    _add_error(
                        result,
                        "INVALID_FIXTURE_ACCEPTED",
                        path=relative,
                        case=index,
                        schema_id=schema_id,
                    )

    result["fixtures"]["coverage"] = {
        schema_id: fixture_coverage[schema_id]
        for schema_id in sorted(fixture_coverage)
    }
    result["fixtures"]["hashes"].sort(key=lambda item: item["path"])
    fixture_errors = {
        "FIXTURE_PARSE_FAILED",
        "FIXTURE_KIND_INVALID",
        "FIXTURE_SCHEMA_ID_INVALID",
        "FIXTURE_SCHEMA_UNAVAILABLE",
        "VALID_FIXTURE_REJECTED",
    }
    result["checks"]["fixtures_validated"] = (
        result["fixtures"]["files"] > 0
        and result["fixtures"]["valid_cases"] > 0
        and not any(error["code"] in fixture_errors for error in result["errors"])
    )
    result["checks"]["invalid_fixtures_rejected"] = (
        result["fixtures"]["files"] > 0
        and result["fixtures"]["invalid_cases"] > 0
        and not any(
            error["code"] in {"FIXTURE_PARSE_FAILED", "FIXTURE_KIND_INVALID", "FIXTURE_SCHEMA_ID_INVALID", "FIXTURE_SCHEMA_UNAVAILABLE", "INVALID_FIXTURE_ACCEPTED"}
            for error in result["errors"]
        )
    )

    for schema_id, coverage in fixture_coverage.items():
        if coverage["valid"] == 0 or coverage["invalid"] == 0:
            result["checks"]["fixtures_validated"] = False
            result["checks"]["invalid_fixtures_rejected"] = False

    required_checks = (
        "schema_inventory",
        "schema_drafts",
        "schema_ids",
        "root_objects_closed",
        "old_ids_absent",
        "provider_wire_subset",
        "fixtures_validated",
        "invalid_fixtures_rejected",
        "vocab_artifact_absent",
    )
    result["status"] = (
        "passed"
        if not result["errors"]
        and all(result["checks"][check] for check in required_checks)
        else "failed"
    )
    if report:
        _write_report(root, result)
    if result["status"] != "passed":
        raise R0ValidationError(result)
    return result


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root (for tests and local tooling)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=f"write {REPORT_PATH.as_posix()} after validation",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        result = validate_repository(args.repo_root, report=args.report)
    except R0ValidationError as exc:
        print(
            f"R0 validation failed: {len(exc.report['errors'])} issue(s)",
            file=sys.stderr,
        )
        return 1
    print(
        "R0 validation passed: "
        f"{result['schemas']['discovered_count']} schemas, "
        f"{result['fixtures']['cases']} fixture case(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
