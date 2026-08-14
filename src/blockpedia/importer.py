"""R1 handoff checking and exporter-to-workspace projection."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .features import build_visual_variant_record
from .paths import DataRoot, ExportPathError, safe_relative_posix_ref, source_directory_ref
from .schema import RecordSchemaError, validate_record
from .stages import STUDIO_STAGES
from .storage import WorkspaceDatabase, utc_now


class ImportErrorBase(RuntimeError):
    """Stable service error for import operations."""


class ImportCheckNotFound(ImportErrorBase):
    pass


class ImportNotAllowed(ImportErrorBase):
    pass


@dataclass(frozen=True, slots=True)
class ImportCheck:
    check_id: str
    minecraft_version: str
    export_id: str
    source_directory_ref: str
    manifest_sha256: str | None
    checksum_sha256: str | None
    snapshot_ref: str
    snapshot_root_sha256: str | None
    metadata_sha256: str | None
    expected_files: tuple[dict[str, str], ...]
    status: str
    issues: tuple[dict[str, str], ...]
    can_import: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "minecraft_version": self.minecraft_version,
            "export_id": self.export_id,
            "source_directory_ref": self.source_directory_ref,
            "manifest_sha256": self.manifest_sha256,
            "checksum_sha256": self.checksum_sha256,
            "snapshot_ref": self.snapshot_ref,
            "snapshot_root_sha256": self.snapshot_root_sha256,
            "metadata_sha256": self.metadata_sha256,
            "expected_files": [dict(item) for item in self.expected_files],
            "status": self.status,
            "issues": [dict(issue) for issue in self.issues],
            "can_import": self.can_import,
        }


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_root_sha256(export_id: str, expected_files: Sequence[Mapping[str, str]], checksum_sha256: str) -> str:
    payload = {
        "export_id": export_id,
        "checksum_sha256": checksum_sha256,
        "files": [dict(item) for item in expected_files],
    }
    return "sha256:" + hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _snapshot_export(source: Path, destination: Path | None = None) -> tuple[tuple[dict[str, str], ...], str, str]:
    """Inventory and hash one export without following links.

    This is separate from the R1 validator: it creates the immutable handoff
    expectation used by the later copy pass, so import never trusts a stale
    check merely because the directory name is unchanged.
    """

    expected: dict[str, str] = {}
    checksum_digest: str | None = None
    if destination is not None:
        if destination.exists():
            if destination.is_symlink() or not destination.is_dir():
                raise ImportNotAllowed("snapshot destination is invalid")
        else:
            destination.mkdir(parents=True, exist_ok=False)
    for root, directories, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        accepted_directories: list[str] = []
        for directory in sorted(directories):
            directory_path = root_path / directory
            if directory_path.is_symlink() or not directory_path.is_dir():
                raise ImportNotAllowed("export contains a symlinked or invalid directory")
            accepted_directories.append(directory)
        directories[:] = accepted_directories
        for filename in sorted(filenames):
            file_path = root_path / filename
            relative = file_path.relative_to(source).as_posix()
            safe_relative_posix_ref(relative)
            if file_path.is_symlink() or not file_path.is_file():
                raise ImportNotAllowed("export contains a symlinked or invalid file")
            if file_path.stat().st_nlink != 1:
                raise ImportNotAllowed("export contains a hardlink")
            data = file_path.read_bytes()
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if relative == "checksums.sha256":
                checksum_digest = digest
            else:
                expected[relative] = digest
            if destination is not None:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.is_symlink() or (target.exists() and (not target.is_file() or target.stat().st_nlink != 1)):
                    raise ImportNotAllowed("snapshot destination contains a link or invalid file")
                target.write_bytes(data)
    if checksum_digest is None:
        raise ImportNotAllowed("checksums.sha256 is missing")
    expected_files = tuple({"relative_ref": ref, "sha256": expected[ref]} for ref in sorted(expected))
    return expected_files, checksum_digest, _snapshot_root_sha256(source.name, expected_files, checksum_digest)


def _copy_verified_snapshot(
    source: Path,
    destination: Path,
    expected_files: Sequence[Mapping[str, str]],
    expected_checksum_sha256: str,
) -> None:
    """Copy and hash source files once, rejecting every TOCTOU difference."""

    expected = {str(item["relative_ref"]): str(item["sha256"]) for item in expected_files}
    seen: set[str] = set()
    checksum_seen = False
    destination.mkdir(parents=True, exist_ok=True)
    for root, directories, filenames in os.walk(source, followlinks=False):
        root_path = Path(root)
        accepted_directories: list[str] = []
        for directory in sorted(directories):
            directory_path = root_path / directory
            if directory_path.is_symlink() or not directory_path.is_dir():
                raise ImportNotAllowed("export changed: symlinked or invalid directory")
            accepted_directories.append(directory)
        directories[:] = accepted_directories
        for filename in sorted(filenames):
            file_path = root_path / filename
            relative = file_path.relative_to(source).as_posix()
            safe_relative_posix_ref(relative)
            if file_path.is_symlink() or not file_path.is_file() or file_path.stat().st_nlink != 1:
                raise ImportNotAllowed("export changed: symlink or hardlink detected")
            data = file_path.read_bytes()
            digest = "sha256:" + hashlib.sha256(data).hexdigest()
            if relative == "checksums.sha256":
                checksum_seen = True
                if digest != expected_checksum_sha256:
                    raise ImportNotAllowed("checksums.sha256 changed after check")
            else:
                if relative not in expected:
                    raise ImportNotAllowed("export changed: extra file")
                if digest != expected[relative]:
                    raise ImportNotAllowed("export changed: file hash mismatch")
                seen.add(relative)
            target_relative = (Path("export") / relative) if "/" not in relative else Path(relative)
            target = destination / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    if not checksum_seen or seen != set(expected):
        raise ImportNotAllowed("export changed: missing expected file")


def copy_to_workspace(
    source: Path,
    workspace_dir: Path,
    *,
    expected_files: Sequence[Mapping[str, str]] | None = None,
    checksum_sha256: str | None = None,
) -> None:
    """Copy a checked exporter snapshot; no projection occurs here."""

    if expected_files is None or checksum_sha256 is None:
        expected_files, checksum_sha256, _ = _snapshot_export(source)
    _copy_verified_snapshot(source, workspace_dir, expected_files, checksum_sha256)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
    return records


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _safe_issue(value: Any, source: Path) -> Any:
    """Remove validator location fields and redact absolute path fragments."""

    if isinstance(value, dict):
        return {str(key): _safe_issue(item, source) for key, item in value.items() if key not in {"repo_root", "export_dir"}}
    if isinstance(value, list):
        return [_safe_issue(item, source) for item in value]
    if isinstance(value, str):
        candidates = {str(source), str(source.absolute()), source.as_posix()}
        result = value
        for candidate in sorted(candidates, key=len, reverse=True):
            result = result.replace(candidate, "<source>")
        if re.search(r"^[A-Za-z]:[\\/]", result) or result.startswith("/"):
            return "<redacted>"
        return result
    return value


def _safe_report(report: Mapping[str, Any], source: Path) -> tuple[dict[str, str], ...]:
    issues = report.get("issues", [])
    safe: list[dict[str, str]] = []
    if isinstance(issues, list):
        for issue in issues:
            clean = _safe_issue(issue, source)
            if isinstance(clean, dict):
                safe.append({str(key): str(value) for key, value in clean.items()})
    return tuple(safe)


class ImportService:
    """Application service used by the future WebUI adapter."""

    def __init__(self, data_root: DataRoot, *, repo_root: Path | None = None, force_normalized_like: bool = False):
        self.data_root = data_root
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.force_normalized_like = force_normalized_like
        self._checks: dict[str, tuple[ImportCheck, Path]] = {}

    def check_import(self, source_directory: str | Path, minecraft_version: str) -> ImportCheck:
        self.data_root.ensure_layout()
        source = self.data_root.export_source(source_directory, minecraft_version)
        check_id = _id("check")
        check_dir = self.data_root.cache / "import-checks" / check_id
        snapshot = check_dir / "snapshot" / source.name
        snapshot_ref = self.data_root.relative_ref(snapshot)
        snapshot_root_sha256: str | None = None
        metadata_sha256: str | None = None
        try:
            expected_files, checksum_digest, snapshot_root_sha256 = _snapshot_export(source, snapshot)
        except ImportNotAllowed as exc:
            expected_files, checksum_digest = (), None
            snapshot_issue = {"code": "IMPORT_SNAPSHOT_INVALID", "detail": str(exc)}
            shutil.rmtree(check_dir, ignore_errors=True)
        else:
            snapshot_issue = None
            metadata = {
                "check_id": check_id,
                "export_id": source.name,
                "snapshot_root_sha256": snapshot_root_sha256,
                "expected_files": [dict(item) for item in expected_files],
                "checksum_sha256": checksum_digest,
            }
            metadata_path = check_dir / "metadata.json"
            metadata_path.write_bytes((_json(metadata) + "\n").encode("utf-8"))
            metadata_sha256 = _sha256(metadata_path)
        # This is intentionally the one and only R1 validator pass for a
        # check. Projection reuses its result and never calls the validator.
        validator_module = importlib.import_module("tools.validate_r1_export")
        report = validator_module.validate_export(self.repo_root, snapshot) if snapshot_issue is None else {"status": "failed", "issues": []}
        digest_map = {item["relative_ref"]: item["sha256"] for item in expected_files}
        manifest_digest = digest_map.get("manifest.json")
        export_id = source.name
        issues = list(_safe_report(report, snapshot))
        if snapshot_issue is not None:
            issues.append(snapshot_issue)
        status = str(report.get("status", "failed"))
        if snapshot_issue is not None:
            status = "failed"
        result = ImportCheck(
            check_id=check_id,
            minecraft_version=minecraft_version,
            export_id=export_id,
            source_directory_ref=source_directory_ref(source),
            manifest_sha256=manifest_digest,
            checksum_sha256=checksum_digest,
            snapshot_ref=snapshot_ref,
            snapshot_root_sha256=snapshot_root_sha256,
            metadata_sha256=metadata_sha256,
            expected_files=tuple(expected_files),
            status=status,
            issues=tuple(issues),
            can_import=status == "passed",
        )
        self._checks[check_id] = (result, source)
        self._write_check_cache(result)
        return result

    def import_checked(self, check_id: str, *, copy_mode: str = "copy_to_workspace") -> dict[str, Any]:
        if copy_mode != "copy_to_workspace":
            raise ImportNotAllowed("copy_mode must be copy_to_workspace")
        checked = self._checks.get(check_id)
        if checked is None:
            result = self._load_check_cache(check_id)
        else:
            result = checked[0]
        if not result.can_import:
            raise ImportNotAllowed("import check did not pass")
        snapshot = self.data_root.resolve_ref(result.snapshot_ref)
        if snapshot.name != result.export_id or not snapshot.is_dir() or snapshot.is_symlink():
            raise ImportNotAllowed("checked snapshot is missing or invalid")
        metadata_path = snapshot.parent.parent / "metadata.json"
        if result.metadata_sha256 is None or not metadata_path.is_file() or _sha256(metadata_path) != result.metadata_sha256:
            raise ImportNotAllowed("checked snapshot metadata changed")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        expected_root = _snapshot_root_sha256(result.export_id, result.expected_files, result.checksum_sha256 or "")
        if metadata.get("snapshot_root_sha256") != result.snapshot_root_sha256 or result.snapshot_root_sha256 != expected_root:
            raise ImportNotAllowed("checked snapshot metadata is inconsistent")
        import_id, run_id = _id("import"), _id("run")
        workspace_parent = self.data_root.workspace / result.minecraft_version
        workspace_parent.mkdir(parents=True, exist_ok=True)
        workspace_dir = self.data_root.workspace_dir(result.minecraft_version, run_id)
        staging_dir = workspace_parent / f".{run_id}.staging"
        staging_dir.mkdir(parents=True, exist_ok=False)
        try:
            _copy_verified_snapshot(snapshot, staging_dir, result.expected_files, result.checksum_sha256 or "")
            database = WorkspaceDatabase.open(staging_dir / "work.sqlite3", force_normalized_like=self.force_normalized_like)
            try:
                _project_to_workspace(
                    database,
                    staging_dir,
                    result,
                    import_id=import_id,
                    run_id=run_id,
                    repo_root=self.repo_root,
                )
            finally:
                database.close()
            staging_dir.replace(workspace_dir)
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            shutil.rmtree(workspace_dir, ignore_errors=True)
            raise
        return {
            "import_id": import_id,
            "run_id": run_id,
            "minecraft_version": result.minecraft_version,
            "status": "pending",
            "workspace_ref": self.data_root.relative_ref(workspace_dir),
            "source_directory_ref": result.source_directory_ref,
        }

    def _write_check_cache(self, result: ImportCheck) -> None:
        cache_dir = self.data_root.cache / "import-checks"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # Deliberately contains only the frozen check-cache fields; notably it
        # does not contain a source path or the validator's repository path.
        payload = {
            "check_id": result.check_id,
            "minecraft_version": result.minecraft_version,
            "export_id": result.export_id,
            "source_directory_ref": result.source_directory_ref,
            "manifest_sha256": result.manifest_sha256,
            "checksum_sha256": result.checksum_sha256,
            "snapshot_ref": result.snapshot_ref,
            "snapshot_root_sha256": result.snapshot_root_sha256,
            "metadata_sha256": result.metadata_sha256,
            "expected_files": [dict(item) for item in result.expected_files],
            "status": result.status,
            "issues": [dict(issue) for issue in result.issues],
        }
        check_dir = cache_dir / result.check_id
        check_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{result.check_id}.json").write_bytes((_json(payload) + "\n").encode("utf-8"))

    def _load_check_cache(self, check_id: str) -> ImportCheck:
        cache_path = self.data_root.cache / "import-checks" / f"{check_id}.json"
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise ImportCheckNotFound(check_id) from exc
        if payload.get("check_id") != check_id:
            raise ImportCheckNotFound(check_id)
        result = ImportCheck(
            check_id=check_id,
            minecraft_version=str(payload["minecraft_version"]),
            export_id=str(payload["export_id"]),
            source_directory_ref=str(payload["source_directory_ref"]),
            manifest_sha256=payload.get("manifest_sha256"),
            checksum_sha256=payload.get("checksum_sha256"),
            snapshot_ref=str(payload["snapshot_ref"]),
            snapshot_root_sha256=payload.get("snapshot_root_sha256"),
            metadata_sha256=payload.get("metadata_sha256"),
            expected_files=tuple(dict(item) for item in payload.get("expected_files", [])),
            status=str(payload["status"]),
            issues=tuple(dict(item) for item in payload.get("issues", [])),
            can_import=str(payload["status"]) == "passed",
        )
        self._checks[check_id] = (result, self.data_root.resolve_ref(result.snapshot_ref))
        return result


def _project_to_workspace(
    database: WorkspaceDatabase,
    source: Path,
    result: ImportCheck,
    *,
    import_id: str,
    run_id: str,
    repo_root: Path,
) -> None:
    export_records = source / "export"
    manifest = json.loads((export_records / "manifest.json").read_text(encoding="utf-8"))
    blocks = _read_jsonl(export_records / "blocks.jsonl")
    states = _read_jsonl(export_records / "states.jsonl")
    variants = _read_jsonl(export_records / "variants.jsonl")
    failures = _read_jsonl(export_records / "failures.jsonl")
    block_map = {record["block_id"]: record for record in blocks}
    variant_map = {record["variant_id"]: record for record in variants}
    failure_map = {record["failure_id"]: record for record in failures}
    _validate_projection_references(block_map, states, variant_map, failures)
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO imports(import_id,minecraft_version,export_id,source_directory_ref,manifest_sha256,checksum_sha256,expected_files_json,report_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (import_id, result.minecraft_version, result.export_id, result.source_directory_ref, result.manifest_sha256 or "", result.checksum_sha256 or "", _json(result.expected_files), _json({"status": result.status, "issues": result.issues}), result.status, now),
        )
        connection.execute(
            "INSERT INTO runs(run_id,import_id,minecraft_version,status,current_stage,boundary_event,config_snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, import_id, result.minecraft_version, "pending", STUDIO_STAGES[0], None, "{}", now),
        )
        for ordinal, stage in enumerate(STUDIO_STAGES):
            connection.execute(
                "INSERT INTO stage_runs(run_id,stage,ordinal,status,cursor_json) VALUES (?,?,?,?,?)",
                (run_id, stage, ordinal, "pending", "{}"),
            )
        connection.execute(
            "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
            (_id("audit"), "IMPORT_CHECKED_AND_PROJECTED", run_id, _json({"import_id": import_id, "export_id": result.export_id}), now),
        )

        for record in blocks:
            block_record = _project_block(record)
            validate_record("block-record.v1", block_record, repo_root=repo_root)
            connection.execute(
                "INSERT INTO blocks(block_id,minecraft_version,record_json) VALUES (?,?,?)",
                (record["block_id"], result.minecraft_version, _json(block_record)),
            )

        for failure in failures:
            connection.execute(
                "INSERT INTO failures(failure_id,minecraft_version,block_id,state_id,variant_id,record_json) VALUES (?,?,?,?,?,?)",
                (failure["failure_id"], result.minecraft_version, failure.get("block_id"), failure.get("state_id"), failure.get("variant_id"), _json(failure)),
            )

        states_by_block: dict[str, list[Mapping[str, Any]]] = {}
        for record in states:
            block = block_map.get(record.get("block_id"))
            if block is None:
                raise ImportNotAllowed("state references an unknown block")
            _check_property_membership(block, record)
            failure_id = None
            if record["mapping_status"] == "skipped":
                failure_id = _failure_for_state(record, failures)
                if failure_id is None:
                    raise ImportNotAllowed("skipped state has no failure reference")
            state_record = _project_state(record, failure_id)
            validate_record("state-record.v1", state_record, repo_root=repo_root)
            states_by_block.setdefault(record["block_id"], []).append(record)
            connection.execute(
                "INSERT INTO states(state_id,block_id,minecraft_version,record_json,failure_id) VALUES (?,?,?,?,?)",
                (record["state_id"], record["block_id"], result.minecraft_version, _json(state_record), failure_id),
            )

        for failure in failures:
            if failure.get("scope") in {"variant", "render"} and failure.get("variant_id") in variant_map and variant_map[failure["variant_id"]].get("status") == "skipped":
                connection.execute(
                    "INSERT OR IGNORE INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_id("review"), result.minecraft_version, "variant", failure["variant_id"], failure.get("reason_code", "OTHER"), "high", "open", failure.get("message", ""), _json(failure.get("evidence", {})), now),
                )

        for record in variants:
            if record.get("status") != "selected":
                # A skipped exporter variant is represented by its machine
                # failure/review precursor, never as a visual workspace row.
                continue
            block_id = record["block_id"]
            if block_id not in block_map or record["variant_id"] != block_id:
                raise ImportNotAllowed("variant reference is inconsistent")
            render = record.get("render")
            if not isinstance(render, Mapping):
                raise ImportNotAllowed("selected variant has no render reference")
            expected_render_prefix = "renders/minecraft/" + block_id.removeprefix("minecraft:")
            expected_render_paths = (
                expected_render_prefix + "/preview.png",
                expected_render_prefix + "/mask.png",
                expected_render_prefix + "/render.json",
            )
            for key in ("preview_path", "mask_path", "render_metadata_path"):
                safe_relative_posix_ref(render[key])
                if not (source / render[key]).is_file() or (source / render[key]).is_symlink():
                    raise ImportNotAllowed("selected render reference is missing")
            if (render["preview_path"], render["mask_path"], render["render_metadata_path"]) != expected_render_paths:
                raise ImportNotAllowed("selected render reference does not match block identity")
            connection.execute(
                "INSERT INTO variants(variant_id,block_id,minecraft_version,status,source_json,record_json) VALUES (?,?,?,?,?,NULL)",
                (record["variant_id"], block_id, result.minecraft_version, "selected", _json(record)),
            )
            for ref_key, hash_key in (("preview_path", "image_sha256"), ("mask_path", "mask_sha256"), ("render_metadata_path", "render_metadata_sha256")):
                relative_ref = render[ref_key]
                artifact_hash = render[hash_key]
                connection.execute(
                    "INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)",
                    (_id("artifact"), None, "render", relative_ref, artifact_hash, _json({"variant_id": record["variant_id"], "hash_mode": "jcs" if ref_key == "render_metadata_path" else "bytes"})),
                )
        connection.execute(
            "INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)",
            (_id("artifact"), None, "source_export", "export/manifest.json", result.manifest_sha256 or "", _json({"export_id": result.export_id})),
        )


def _project_block(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "block-record.v1",
        "export_id": record["export_id"],
        "minecraft_version": record["minecraft_version"],
        "block_id": record["block_id"],
        "translation_key": record["translation_key"],
        "official_names": {"zh_cn": record["name_zh_cn"], "en_us": record["name_en_us"]},
        "default_state_id": record["default_state_id"],
        "properties": record["properties"],
        "tags": record["tags"],
        "machine_facts": {"has_item": record["has_item"], "has_block_entity": record["has_block_entity"]},
        "source": record["source"],
    }


def _project_state(record: Mapping[str, Any], failure_id: str | None) -> dict[str, Any]:
    return {
        "schema_version": "state-record.v1",
        "export_id": record["export_id"],
        "minecraft_version": record["minecraft_version"],
        "state_id": record["state_id"],
        "block_id": record["block_id"],
        "properties": record["properties"],
        "is_default": record["is_default"],
        "legal_state": record["legal_state"],
        "shape": record["shape"],
        "collision": record["collision"],
        "behavior": record["behavior"],
        "variant_ids": record["variant_ids"],
        "mapping_status": record["mapping_status"],
        "failure_id": failure_id,
        "source": record["source"],
    }


def _check_property_membership(block: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    legal = block.get("properties", {})
    properties = state.get("properties", {})
    if set(properties) != set(legal):
        raise ImportNotAllowed("state properties do not match block properties")
    for name, value in properties.items():
        if value not in legal.get(name, []):
            raise ImportNotAllowed("state property value is outside block legal set")


def _failure_for_state(state: Mapping[str, Any], failures: Sequence[Mapping[str, Any]]) -> str | None:
    for failure in failures:
        if failure.get("scope") == "state" and failure.get("state_id") == state.get("state_id"):
            return str(failure["failure_id"])
    for failure in failures:
        if failure.get("block_id") == state.get("block_id") and failure.get("scope") in {"block", "variant", "render"}:
            return str(failure["failure_id"])
    return None


def _validate_projection_references(
    blocks: Mapping[str, Mapping[str, Any]],
    states: Sequence[Mapping[str, Any]],
    variants: Mapping[str, Mapping[str, Any]],
    failures: Sequence[Mapping[str, Any]],
) -> None:
    states_by_block: dict[str, set[str]] = {}
    for state in states:
        block_id = state.get("block_id")
        if block_id not in blocks:
            raise ImportNotAllowed("state references an unknown block")
        states_by_block.setdefault(str(block_id), set()).add(str(state["state_id"]))
        references = state.get("variant_ids", [])
        if state.get("mapping_status") == "mapped":
            if not references:
                raise ImportNotAllowed("mapped state has no variant reference")
            for variant_id in references:
                variant = variants.get(variant_id)
                if variant is None or variant.get("status") != "selected" or variant.get("block_id") != block_id:
                    raise ImportNotAllowed("state variant reference is not a selected same-block variant")
        elif references:
            raise ImportNotAllowed("skipped state has variant references")
    for variant_id, variant in variants.items():
        block_id = variant.get("block_id")
        if block_id not in blocks or variant_id != block_id:
            raise ImportNotAllowed("variant reference is inconsistent")
        if variant.get("status") == "selected":
            represented = set(variant.get("represented_state_ids", []))
            if represented != states_by_block.get(str(block_id), set()):
                raise ImportNotAllowed("selected variant state projection is incomplete")
            block_default = blocks[str(block_id)].get("default_state_id")
            if variant.get("canonical_state_id") != block_default or block_default not in represented:
                raise ImportNotAllowed("selected variant canonical state is not the block default")
        elif not any(failure.get("variant_id") == variant_id for failure in failures):
            raise ImportNotAllowed("skipped variant has no machine failure")
    for failure in failures:
        scope = failure.get("scope")
        block_id = failure.get("block_id")
        if scope in {"block", "state", "variant", "render"} and block_id not in blocks:
            raise ImportNotAllowed("failure block reference is invalid")
        if scope == "state" and (failure.get("state_id") not in {state.get("state_id") for state in states}):
            raise ImportNotAllowed("failure state reference is invalid")
        if scope in {"variant", "render"}:
            variant_id = failure.get("variant_id")
            variant = variants.get(variant_id) if isinstance(variant_id, str) else None
            if variant is None or variant.get("block_id") != block_id:
                raise ImportNotAllowed("failure variant reference is invalid")
