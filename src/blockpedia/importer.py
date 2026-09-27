"""One import operation: validate/copy once, then commit a SQLite workspace."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Sequence

from .directory_chooser import DirectoryChooser
from .paths import DataRoot, safe_relative_posix_ref, validate_minecraft_version
from .local_files import safe_path, sync_directory, sync_tree, commit_directory
from .schema import validate_record
from .stages import STUDIO_STAGES
from .storage import WorkspaceDatabase, DatabaseSchemaMismatch, utc_now


class ImportErrorBase(RuntimeError):
    code = "IMPORT_FAILED"
    def __init__(self, message="import failed", *, code=None):
        self.code = code or self.code
        super().__init__(message)


class ImportNotFound(ImportErrorBase):
    code = "IMPORT_NOT_FOUND"


class ImportNotAllowed(ImportErrorBase):
    code = "IMPORT_NOT_ALLOWED"


class ImportConflict(ImportErrorBase):
    code = "IMPORT_CONFLICT"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(prefix):
    return prefix + "_" + uuid.uuid4().hex


def _read_jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _run_id(value):
    if not isinstance(value, str) or re.fullmatch(r"run_[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None:
        raise ImportNotAllowed("invalid run id")
    return value


class ImportService:
    def __init__(self, data_root, *, repo_root=None, force_normalized_like=False, chooser=None):
        self.data_root = data_root
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.force_normalized_like = force_normalized_like
        self.chooser = chooser or DirectoryChooser(data_root)
        self._operations = {}
        self._sources = {}
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blockpedia-import")
        self._closed = False

    def close(self):
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=True)

    def start(self, run_id, source_directory_ref, minecraft_version, *, feature_workers=1):
        _run_id(run_id)
        validate_minecraft_version(minecraft_version)
        if type(feature_workers) is not int or not 1 <= feature_workers <= 5:
            raise ImportNotAllowed("feature_workers must be an integer from 1 to 5")
        with self._lock:
            if self._closed:
                raise ImportNotAllowed("importer is closed")
            try:
                existing = self.get(run_id)
            except ImportNotFound:
                existing = None
            if existing is not None:
                if existing["minecraft_version"] != minecraft_version:
                    raise ImportConflict("run belongs to a different version")
                if existing.get("feature_workers") != feature_workers:
                    raise ImportConflict("feature_workers is frozen for this run")
                if existing["status"] in {"pending", "running", "succeeded"}:
                    return existing
            source = self.chooser.consume(source_directory_ref, minecraft_version)
            if run_id in self._sources and self._sources[run_id] != source:
                raise ImportConflict("retry must use the same source")
            if existing and existing.get("export_id") and existing["export_id"] != source.name:
                raise ImportConflict("retry must use the same export")
            now = utc_now()
            if existing is None:
                staging = self.data_root.workspace_dir(minecraft_version, run_id).with_name("." + run_id + ".staging")
                safe_path(staging, self.data_root.root, missing=True)
                staging.mkdir(parents=True)
                # Persist the run's one config snapshot before acknowledging the
                # operation, including crashes before validation or projection.
                with WorkspaceDatabase.open(staging / "work.sqlite3", force_normalized_like=self.force_normalized_like) as database:
                    with database.transaction() as connection:
                        import_id = _id("import")
                        connection.execute("INSERT INTO imports(import_id,minecraft_version,export_id,source_directory_ref,manifest_sha256,checksum_sha256,expected_files_json,report_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)", (import_id, minecraft_version, source.name, "not-persisted", "", "", "[]", "{}", "failed", now))
                        connection.execute("INSERT INTO runs(run_id,import_id,minecraft_version,status,current_stage,config_snapshot_json,created_at) VALUES (?,?,?,?,?,?,?)", (run_id, import_id, minecraft_version, "pending", "IMPORT_EXPORT", _json({"feature_workers": feature_workers}), now))
                sync_directory(staging)
                sync_directory(staging.parent)
            state = {"run_id": run_id, "minecraft_version": minecraft_version, "export_id": source.name,
                     "feature_workers": feature_workers,
                     "import_id": None, "status": "pending", "phase": "IMPORT_EXPORT",
                     "progress": {"completed": 0, "total": 0, "unit": "files"}, "error_code": None,
                     "created_at": existing["created_at"] if existing else now, "updated_at": now}
            self._operations[run_id] = state
            self._sources[run_id] = source
            self._executor.submit(self._execute, run_id, source)
            return dict(state)

    def _workspace(self, run_id):
        matches = []
        if not self.data_root.workspace.is_dir():
            raise ImportNotFound(run_id)
        for version in self.data_root.workspace.iterdir():
            if not version.is_dir():
                continue
            safe_path(version, self.data_root.root, directory=True)
            final = version / run_id
            staging = version / ("." + run_id + ".staging")
            if final.exists():
                matches.append((final, False))
            elif staging.exists():
                matches.append((staging, True))
        if len(matches) > 1:
            raise ImportConflict("ambiguous run id")
        if not matches:
            raise ImportNotFound(run_id)
        path, interrupted = matches[0]
        safe_path(path, self.data_root.root, directory=True)
        if interrupted and not (path / "work.sqlite3").is_file():
            return {"run_id": run_id, "minecraft_version": path.parent.name, "export_id": "",
                    "feature_workers": None,
                    "import_id": None, "status": "interrupted", "phase": "IMPORT_EXPORT",
                    "progress": {"completed": 0, "total": 0, "unit": "files"},
                    "error_code": "IMPORT_INTERRUPTED", "created_at": "", "updated_at": ""}
        safe_path(path / "work.sqlite3", self.data_root.root, directory=False)
        with WorkspaceDatabase.open(path / "work.sqlite3", read_only=True) as database:
            row = database.fetchone("SELECT runs.run_id,runs.minecraft_version,runs.config_snapshot_json,imports.import_id,imports.export_id,imports.created_at FROM runs JOIN imports ON runs.import_id=imports.import_id WHERE runs.run_id=?", (run_id,))
            if row is None or row["minecraft_version"] != path.parent.name:
                raise ImportConflict("workspace identity is invalid")
            public = dict(row)
            public["feature_workers"] = json.loads(public.pop("config_snapshot_json")).get("feature_workers", None if interrupted else 1)
            if interrupted:
                return {**public, "status": "interrupted", "phase": "IMPORT_EXPORT",
                        "progress": {"completed": 0, "total": 0, "unit": "files"},
                        "error_code": "IMPORT_INTERRUPTED", "updated_at": row["created_at"]}
            return {**public, "status": "succeeded", "phase": "FINALIZE",
                    "progress": {"completed": 1, "total": 1, "unit": "imports"},
                    "error_code": None, "updated_at": row["created_at"]}

    def get(self, run_id):
        _run_id(run_id)
        with self._lock:
            if run_id in self._operations:
                value = self._operations[run_id]
                return {**value, "progress": dict(value["progress"])}
            return self._workspace(run_id)

    def list(self, minecraft_version=None, limit=20):
        if minecraft_version is not None:
            validate_minecraft_version(minecraft_version)
        with self._lock:
            ids = set(self._operations)
        for path in self.data_root.workspace.glob("*/*"):
            name = path.name.removeprefix(".").removesuffix(".staging")
            if re.fullmatch(r"run_[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", name):
                ids.add(name)
        results = []
        for run_id in ids:
            try:
                value = self.get(run_id)
            except (ImportErrorBase, OSError, ValueError, sqlite3.Error, DatabaseSchemaMismatch):
                continue
            if minecraft_version is None or value["minecraft_version"] == minecraft_version:
                results.append(value)
        return sorted(results, key=lambda row: (row["status"] in {"pending", "running"}, row["updated_at"], row["run_id"]), reverse=True)[:max(1, min(limit, 100))]

    def wait(self, run_id):
        while True:
            result = self.get(run_id)
            if result["status"] not in {"pending", "running"}:
                return result
            time.sleep(0.01)

    def _update(self, run_id, **values):
        with self._lock:
            self._operations[run_id] = {**self._operations[run_id], **values, "updated_at": utc_now()}

    def _execute(self, run_id, source):
        from tools.validate_r1_export import Validator
        state = self.get(run_id)
        version = state["minecraft_version"]
        final = self.data_root.workspace_dir(version, run_id)
        staging = final.with_name("." + run_id + ".staging")
        committed = False
        try:
            safe_path(final.parent, self.data_root.root, missing=True)
            final.parent.mkdir(parents=True, exist_ok=True)
            safe_path(staging, self.data_root.root, directory=True)
            with WorkspaceDatabase.open(staging / "work.sqlite3", read_only=True) as database:
                frozen = database.fetchone("SELECT import_id,current_stage FROM runs WHERE run_id=?", (run_id,))
                if frozen is None:
                    raise ImportConflict("staging run config is missing")
                import_id = frozen["import_id"]
                projected = frozen["current_stage"] == "EXTRACT_FEATURES"
            if projected:
                sync_tree(staging)
                commit_directory(staging, final)
                committed = True
                sync_directory(final.parent)
                self._update(run_id, status="succeeded", phase="FINALIZE", import_id=import_id, progress={"completed": 1, "total": 1, "unit": "imports"})
                return
            # Retain the sole SQLite config snapshot while clearing an
            # interrupted copy. Projection itself is a single transaction.
            for item in staging.iterdir():
                if item.name in {"work.sqlite3", "work.sqlite3-wal", "work.sqlite3-shm"}:
                    continue
                safe_path(item, self.data_root.root)
                if item.is_dir():
                    shutil.rmtree(item)
                else:
                    item.unlink()
            self._update(run_id, status="running")
            def progress(phase, completed, total, unit):
                self._update(run_id, phase="VALIDATE_EXPORT", progress={"completed": completed, "total": total or 0, "unit": unit})
            validator = Validator(self.repo_root, source, copy_to=staging)
            report = validator.run(on_progress=progress)
            if report["status"] != "passed":
                code = report["issues"][0]["code"] if report["issues"] else "IMPORT_INVALID"
                raise ImportNotAllowed(code=code)
            if validator.manifest["toolchain"]["minecraft_version"] != version:
                raise ImportNotAllowed(code="IMPORT_VERSION_MISMATCH")
            with WorkspaceDatabase.open(staging / "work.sqlite3", force_normalized_like=self.force_normalized_like) as database:
                _project_to_workspace(database, staging, validator.manifest, validator.records, import_id=import_id, run_id=run_id, repo_root=self.repo_root, feature_workers=state["feature_workers"])
            sync_tree(staging)
            commit_directory(staging, final)
            committed = True
            sync_directory(final.parent)
            self._update(run_id, status="succeeded", phase="FINALIZE", import_id=import_id, progress={"completed": 1, "total": 1, "unit": "imports"})
        except Exception as exc:
            if committed or final.exists():
                try:
                    self._update(run_id, **{key: value for key, value in self._workspace(run_id).items() if key != "run_id"})
                    return
                except Exception:
                    pass
            code = exc.code if isinstance(exc, ImportErrorBase) else "IMPORT_FAILED"
            self._update(run_id, status="failed", error_code=code, phase="FINALIZE")


def _project_to_workspace(
    database: WorkspaceDatabase,
    source: Path,
    manifest: Mapping[str, Any],
    records: Mapping[str, list[Mapping[str, Any]]],
    *,
    import_id: str,
    run_id: str,
    repo_root: Path,
    feature_workers: int = 1,
) -> None:
    version, export_id = manifest["toolchain"]["minecraft_version"], manifest["export_id"]
    blocks, states, variants, failures = (records[name] for name in ("blocks.jsonl", "states.jsonl", "variants.jsonl", "failures.jsonl"))
    block_map = {record["block_id"]: record for record in blocks}
    variant_map = {record["variant_id"]: record for record in variants}
    failure_map = {record["failure_id"]: record for record in failures}
    _validate_projection_references(block_map, states, variant_map, failures)
    now = utc_now()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO imports(import_id,minecraft_version,export_id,source_directory_ref,manifest_sha256,checksum_sha256,expected_files_json,report_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(import_id) DO UPDATE SET report_json=excluded.report_json,status=excluded.status",
            # Chooser refs are process-local and are never written to the
            # workspace database.  The frozen column remains populated with a
            # non-reference marker for the existing schema.
            (import_id, version, export_id, "not-persisted", "", "", "[]", _json({"status": "passed", "source_summary": {"export_id": export_id, "minecraft_version": version, "registry_blocks": manifest["counts"]["registry_blocks"], "policies": manifest.get("policies", {}), "toolchain": manifest.get("toolchain", {})}}), "passed", now),
        )
        connection.execute(
            "INSERT INTO runs(run_id,import_id,minecraft_version,status,current_stage,boundary_event,config_snapshot_json,created_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET current_stage=excluded.current_stage",
            (run_id, import_id, version, "pending", "EXTRACT_FEATURES", None, _json({"feature_workers": feature_workers}), now),
        )
        for ordinal, stage in enumerate(STUDIO_STAGES):
            connection.execute(
                "INSERT INTO stage_runs(run_id,stage,ordinal,status,cursor_json) VALUES (?,?,?,?,?)",
                (run_id, stage, ordinal, "succeeded" if ordinal < 5 else "pending", "{}"),
            )
        connection.execute(
            "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
            (_id("audit"), "IMPORT_CHECKED_AND_PROJECTED", run_id, _json({"import_id": import_id, "export_id": export_id}), now),
        )

        for record in blocks:
            block_record = _project_block(record)
            validate_record("block-record.v1", block_record, repo_root=repo_root)
            connection.execute(
                "INSERT INTO blocks(block_id,minecraft_version,record_json) VALUES (?,?,?)",
                (record["block_id"], version, _json(block_record)),
            )

        for failure in failures:
            connection.execute(
                "INSERT INTO failures(failure_id,minecraft_version,block_id,state_id,variant_id,record_json) VALUES (?,?,?,?,?,?)",
                (failure["failure_id"], version, failure.get("block_id"), failure.get("state_id"), failure.get("variant_id"), _json(failure)),
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
                (record["state_id"], record["block_id"], version, _json(state_record), failure_id),
            )

        for failure in failures:
            if failure.get("scope") in {"variant", "render"} and failure.get("variant_id") in variant_map and variant_map[failure["variant_id"]].get("status") == "skipped":
                connection.execute(
                    "INSERT OR IGNORE INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (_id("review"), version, "variant", failure["variant_id"], failure.get("reason_code", "OTHER"), "high", "open", failure.get("message", ""), _json(failure.get("evidence", {})), now),
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
                (record["variant_id"], block_id, version, "selected", _json(record)),
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
