"""Build a self-contained release from committed SQLite results in one operation."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

from .local_files import safe_path, write_bytes, sync_tree, sync_directory, commit_directory
from .paths import DataRoot, RELEASE_BUILD_ID_RE, validate_minecraft_version, safe_relative_posix_ref
from .png import validate_rgba_png
from .r3 import canonical_json, is_sensitive_review_text
from .schema import validate_record, RecordSchemaError
from .search import WorkspaceQueryService, human_semantics_complete, normalize_text
from .storage import WorkspaceDatabase, packaged_release_index_schema, utc_now


class ReleaseBuildFailure(RuntimeError):
    def __init__(self, code, message="release operation failed", *, after_commit=False):
        self.code, self.after_commit = code, after_commit
        super().__init__(message)


def _bytes(value):
    return (canonical_json(value) + "\n").encode("utf-8")


def _hash(payload):
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _json_file(path, root):
    return json.loads(safe_path(path, root, directory=False).read_text(encoding="utf-8"))


def _need(condition, code):
    if not condition:
        raise ReleaseBuildFailure(code)


def inspect_release(data_root, minecraft_version, release_id, *, repo_root=None):
    """Read only the small identity/report and open the index. Never hash a release."""
    validate_minecraft_version(minecraft_version)
    root = data_root.release_dir(minecraft_version, release_id)
    safe_path(root, data_root.root, directory=True)
    release = _json_file(root / "release.json", data_root.root)
    manifest = _json_file(root / "manifest.json", data_root.root)
    report = _json_file(root / "quality_report.json", data_root.root)
    validate_record("release.v1", release, repo_root=repo_root)
    validate_record("release-manifest.v1", manifest, repo_root=repo_root)
    for value in (release, manifest, report):
        _need(value.get("release_id") == release_id and value.get("minecraft_version") == minecraft_version, "RELEASE_IDENTITY_INVALID")
    _need(report.get("format_version") in {1, 2} and report.get("status") == "passed", "RELEASE_REPORT_INVALID")
    _need(isinstance(report.get("run_id"), str) and RELEASE_BUILD_ID_RE.fullmatch(str(report.get("release_build_id"))) is not None, "RELEASE_REPORT_INVALID")
    safe_path(root / "manual-overrides.json", data_root.root, directory=False)
    safe_path(root / "previews", data_root.root, directory=True)
    index = safe_path(root / "index.sqlite3", data_root.root, directory=False)
    connection = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)
    try:
        _need(connection.execute("SELECT format_version FROM schema_meta").fetchall() == [(2,)], "RELEASE_FORMAT_UNSUPPORTED")
        connection.execute("SELECT block_id,record_json FROM blocks LIMIT 1").fetchall()
        connection.execute("SELECT variant_id,record_json,feature_json FROM visual_variants LIMIT 1").fetchall()
    finally:
        connection.close()
    return {"release": release, "manifest": manifest, "report": report, "path": root}


class ReleaseBuilder:
    def __init__(self, data_root, *, repo_root=None, force_normalized_like=False, pre_rename_hook=None):
        self.data_root = data_root
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.force_normalized_like = force_normalized_like
        self.pre_rename_hook = pre_rename_hook
        self._build_lock = threading.Lock()

    def list_releases(self, minecraft_version):
        validate_minecraft_version(minecraft_version)
        parent = self.data_root.releases / minecraft_version
        if not parent.exists():
            return []
        safe_path(parent, self.data_root.root, directory=True)
        result = []
        for root in parent.iterdir():
            if re.fullmatch(r"rel_[0-9a-f]{32}", root.name) is None:
                continue
            try:
                release = _json_file(root / "release.json", self.data_root.root)
                validate_record("release.v1", release, repo_root=self.repo_root)
                if release["release_id"] != root.name or release["minecraft_version"] != minecraft_version:
                    continue
                result.append({key: release[key] for key in ("release_id", "minecraft_version", "built_at", "source_export_id")})
            except (OSError, ValueError, RecordSchemaError):
                continue
        return sorted(result, key=lambda item: (item["built_at"], item["release_id"]), reverse=True)

    def build(self, run_id, minecraft_version, release_build_id):
        # ponytail: serialize local builds; parallelize only if this becomes limiting.
        if not self._build_lock.acquire(blocking=False):
            raise ReleaseBuildFailure("RELEASE_BUSY")
        try:
            return self._build(run_id, minecraft_version, release_build_id)
        finally:
            self._build_lock.release()

    def _build(self, run_id, minecraft_version, release_build_id):
        validate_minecraft_version(minecraft_version)
        _need(RELEASE_BUILD_ID_RE.fullmatch(release_build_id) is not None, "RELEASE_BUILD_ID_INVALID")
        release_id = "rel_" + release_build_id.removeprefix("build_")
        workspace = self.data_root.workspace_dir(minecraft_version, run_id)
        safe_path(workspace / "work.sqlite3", self.data_root.root, directory=False)
        _need(not (workspace / "banner-refresh.v1.json").exists(), "LEGACY_REFRESH_RECOVERY_REQUIRED")
        final = self.data_root.release_dir(minecraft_version, release_id)
        staging = final.with_name("." + release_id + ".staging")
        committed = False
        reserved = False
        with WorkspaceDatabase.open(workspace / "work.sqlite3") as database:
            try:
                if final.exists() or final.is_symlink():
                    info = inspect_release(self.data_root, minecraft_version, release_id, repo_root=self.repo_root)
                    _need(info["report"]["run_id"] == run_id and info["report"]["release_build_id"] == release_build_id, "RELEASE_IDENTITY_CONFLICT")
                    committed = True
                    self._finish(database, run_id, release_build_id, release_id, info["release"]["built_at"])
                    return self._result(info, reused=True)
                with database.transaction() as c:
                    run = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    _need(run is not None and run["minecraft_version"] == minecraft_version, "RUN_NOT_FOUND")
                    _need(c.execute("SELECT 1 FROM jobs WHERE run_id=? AND status IN ('pending','running')", (run_id,)).fetchone() is None, "RELEASE_BUSY")
                    stage = c.execute("SELECT status,cursor_json FROM stage_runs WHERE run_id=? AND stage='BUILD_RELEASE'", (run_id,)).fetchone()
                    _need(stage is not None, "RELEASE_NOT_READY")
                    cursor = json.loads(stage["cursor_json"])
                    _need(stage["status"] != "running" or cursor.get("release_build_id") == release_build_id, "RELEASE_BUSY")
                    owns_staging = cursor.get("release_build_id") == release_build_id
                    _need(not (owns_staging and cursor.get("completed")), "RELEASE_MISSING")
                    _need(not staging.exists() or owns_staging, "RELEASE_IDENTITY_CONFLICT")
                    c.execute("UPDATE stage_runs SET status='running',cursor_json=?,started_at=?,finished_at=NULL WHERE run_id=? AND stage='BUILD_RELEASE'", (canonical_json({"release_build_id": release_build_id, "release_id": release_id}), utc_now(), run_id))
                reserved = True
                safe_path(final.parent, self.data_root.root, missing=True)
                final.parent.mkdir(parents=True, exist_ok=True)
                if staging.exists():
                    safe_path(staging, self.data_root.root, directory=True)
                    shutil.rmtree(staging)
                staging.mkdir()
                (staging / "previews").mkdir()
                with database.read_transaction() as c:
                    run = c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    source = c.execute("SELECT * FROM imports WHERE import_id=?", (run["import_id"],)).fetchone()
                    _need(source is not None and source["status"] == "passed", "IMPORT_INCOMPLETE")
                    blocks = self._records(c, "blocks", "block_id", "block-record.v1")
                    states = self._records(c, "states", "state_id", "state-record.v1")
                    variants = self._records(c, "variants", "variant_id", "visual-variant-record.v1")
                    annotations = self._records(c, "annotations", "annotation_id", "annotation-record.v1")
                    overrides = [json.loads(row[0]) for row in c.execute("SELECT record_json FROM overrides ORDER BY override_id")]
                    failures = {row["failure_id"]: json.loads(row["record_json"]) for row in c.execute("SELECT failure_id,record_json FROM failures")}
                    features = {row["variant_id"]: json.loads(row["feature_json"]) for row in c.execute("SELECT variant_id,feature_json FROM features")}
                    provenance = json.loads(source["report_json"])
                    summary = provenance.get("source_summary")
                    if summary is None:
                        manifest = _json_file(workspace / "export" / "manifest.json", self.data_root.root)
                        summary = {"registry_blocks": manifest["counts"]["registry_blocks"]}
                    _need(len(blocks) == summary["registry_blocks"] and bool(blocks), "REGISTRY_INCOMPLETE")
                    _need(c.execute("SELECT 1 FROM review_tasks WHERE status='open' AND reason_code NOT IN ('FTS_BUILD_FAILED','FTS_COVERAGE_MISSING')").fetchone() is None, "REVIEW_TASKS_OPEN")
                    manual = self._validate_content(c, blocks, states, variants, annotations, overrides, failures, features, minecraft_version)
                    self._validate_lineage(c, provenance, source, variants)
                    query = WorkspaceQueryService(database)
                    semantics = {vid: query._verified_semantics(c, record["block_id"], vid) for vid, record in variants.items()}
                    fts_mode = self._write_index(staging / "index.sqlite3", blocks, states, variants, features, semantics, minecraft_version)
                    for vid, record in variants.items():
                        suffix = vid.removeprefix("minecraft:")
                        for name, ref_key in (("preview.png", "preview_path"), ("mask.png", "mask_path"), ("render.json", "render_metadata_path")):
                            ref = safe_relative_posix_ref(record["render"][ref_key])
                            _need(ref == f"renders/minecraft/{suffix}/{name}", "RENDER_REFERENCE_INVALID")
                            payload = safe_path(workspace / ref, self.data_root.root, directory=False).read_bytes()
                            if name.endswith(".png"):
                                image = validate_rgba_png(payload)
                                _need((image.width, image.height) == (512, 512), "IMAGE_INVALID")
                            else:
                                metadata = json.loads(payload)
                                validate_record("render-metadata.v1", metadata, repo_root=self.repo_root)
                                _need(metadata["variant_id"] == vid, "RENDER_REFERENCE_INVALID")
                            write_bytes(staging / "previews" / "minecraft" / suffix / name, payload, staging)
                    built_at = utc_now()
                    manual.update({"format_version": 1, "release_id": release_id, "version": minecraft_version})
                    write_bytes(staging / "manual-overrides.json", _bytes(manual), staging)
                    report = {"format_version": 2, "report_kind": "release", "release_id": release_id, "release_build_id": release_build_id, "run_id": run_id, "minecraft_version": minecraft_version, "status": "passed", "built_at": built_at, "items": [{"code": code, "status": "passed", "observed_count": count} for code, count in (("REGISTRY_COVERAGE", len(blocks)), ("LEGAL_STATES", len(states)), ("VISUAL_VARIANTS", len(variants)), ("REVIEWED_SEMANTICS", len(semantics)), ("SEARCH_READY", len(semantics)))]}
                    report_bytes = _bytes(report)
                    write_bytes(staging / "quality_report.json", report_bytes, staging)
                    manifest = {"schema_version": "release-manifest.v1", "release_id": release_id, "minecraft_version": minecraft_version, "source_export_id": source["export_id"], "quality_report_path": "quality_report.json", "quality_report_sha256": _hash(report_bytes), "fts_mode": fts_mode}
                    provider = _provider_snapshot(json.loads(run["config_snapshot_json"]))
                    _need(provider is not None or not any(a["source"]["type"] == "llm" for a in annotations.values()), "PROVIDER_LINEAGE_MISSING")
                    if provider is not None:
                        manifest["provider_snapshot"] = provider
                    manifest_bytes = _bytes(manifest)
                    validate_record("release-manifest.v1", manifest, repo_root=self.repo_root)
                    write_bytes(staging / "manifest.json", manifest_bytes, staging)
                    release = {"schema_version": "release.v1", "release_id": release_id, "minecraft_version": minecraft_version, "built_at": built_at, "source_export_id": source["export_id"], "manifest_sha256": _hash(manifest_bytes), "record_schema_versions": {"block": "block-record.v1", "state": "state-record.v1", "variant": "visual-variant-record.v1", "annotation": "annotation-record.v1", "manual_override": "manual-override.v1", "skip_review": "skip-review.v1", "qualification_review": "qualification-review.v1"}, "quality_report_path": "quality_report.json", "immutable": True}
                    validate_record("release.v1", release, repo_root=self.repo_root)
                    write_bytes(staging / "release.json", _bytes(release), staging)
                if self.pre_rename_hook is not None:
                    self.pre_rename_hook(staging, final)
                sync_tree(staging)
                commit_directory(staging, final)
                committed = True
                sync_directory(final.parent)
                self._finish(database, run_id, release_build_id, release_id, built_at)
                return self._result({"release": release, "manifest": manifest, "report": report, "path": final}, reused=False)
            except Exception as exc:
                if committed:
                    raise ReleaseBuildFailure("RELEASE_FINALIZE_PENDING", after_commit=True) from exc
                if reserved and staging.exists():
                    safe_path(staging, self.data_root.root, directory=True)
                    shutil.rmtree(staging)
                if reserved:
                    with database.transaction() as c:
                        c.execute("UPDATE stage_runs SET status='failed' WHERE run_id=? AND stage='BUILD_RELEASE' AND status='running'", (run_id,))
                if isinstance(exc, ReleaseBuildFailure):
                    raise
                raise ReleaseBuildFailure("RELEASE_BUILD_FAILED") from exc

    def _records(self, c, table, identifier, schema):
        result = {}
        for row in c.execute(f"SELECT {identifier},record_json FROM {table} ORDER BY {identifier}"):
            _need(row["record_json"] is not None, "RELEASE_NOT_READY")
            record = json.loads(row["record_json"])
            validate_record(schema, record, repo_root=self.repo_root)
            _need(record.get(identifier) == row[identifier], "RECORD_REFERENCE_INVALID")
            result[row[identifier]] = record
        return result

    def _validate_content(self, c, blocks, states, variants, annotations, overrides, failures, features, version):
        manual = {"manual_overrides": [], "skip_reviews": [], "qualification_reviews": []}
        groups = {"manual-override.v1": "manual_overrides", "skip-review.v1": "skip_reviews", "qualification-review.v1": "qualification_reviews"}
        manual_ids = {}
        for record in overrides:
            schema = record.get("schema_version")
            _need(schema in groups, "OVERRIDE_INVALID")
            validate_record(schema, record, repo_root=self.repo_root)
            _need(not any(is_sensitive_review_text(record.get(key, "")) for key in ("note", "reviewer")), "OVERRIDE_INVALID")
            target = record.get("target_id", record.get("scope", {}).get("variant_id"))
            _need(target in blocks or target in states or target in variants, "OVERRIDE_REFERENCE_INVALID")
            if schema == "skip-review.v1":
                failure = failures.get(record["machine_failure_ref"])
                _need(failure is not None and target in {failure.get("block_id"), failure.get("state_id"), failure.get("variant_id")}, "SKIP_REVIEW_INVALID")
            manual[groups[schema]].append(record)
            manual_ids[record.get("override_id", record.get("review_id"))] = record
        by_block = {}
        for sid, record in states.items():
            block = blocks.get(record["block_id"])
            _need(block is not None and record["legal_state"] is True, "LEGAL_STATE_INVALID")
            _need(set(record["properties"]) == set(block["properties"]) and all(value in block["properties"][key] for key, value in record["properties"].items()), "LEGAL_STATE_INVALID")
            by_block.setdefault(record["block_id"], set()).add(sid)
            refs = record["variant_ids"]
            _need(all(ref in variants and variants[ref]["block_id"] == record["block_id"] for ref in refs), "STATE_VARIANT_INVALID")
        for bid, block in blocks.items():
            default = states.get(block["default_state_id"])
            _need(default is not None and default["block_id"] == bid and default["is_default"], "DEFAULT_STATE_INVALID")
            _need(sum(states[sid]["is_default"] for sid in by_block[bid]) == 1, "DEFAULT_STATE_INVALID")
            if not any(v["block_id"] == bid for v in variants.values()):
                _need(any(failures[r["machine_failure_ref"]].get("block_id") == bid for r in manual["skip_reviews"]), "SKIP_REVIEW_MISSING")
        for aid, annotation in annotations.items():
            _need(annotation["subject_id"] in blocks or annotation["subject_id"] in variants, "ANNOTATION_REFERENCE_INVALID")
        for vid, record in variants.items():
            _need(vid == record["block_id"] and vid in blocks, "VARIANT_REFERENCE_INVALID")
            _need(record["canonical_state_id"] == blocks[vid]["default_state_id"] and set(record["represented_state_ids"]) == by_block[vid], "VARIANT_STATE_INVALID")
            _need(vid in features and isinstance(features[vid], dict), "FEATURE_MISSING")
            feature = features[vid]
            _need(feature.get("feature_extractor_version") == record["machine_facts"]["geometry"]["feature_extractor_version"], "FEATURE_INVALID")
            for aid in record["annotation_refs"]:
                _need(aid in annotations and annotations[aid]["subject_id"] in {vid, record["block_id"]}, "ANNOTATION_REFERENCE_INVALID")
            for mid in record["override_refs"] + record["qualification_review_refs"]:
                _need(mid in manual_ids, "OVERRIDE_REFERENCE_INVALID")
            qualification = record["candidate_qualification"]
            if qualification == "conditional":
                _need(bool(record["warnings"]), "CONDITIONAL_WARNING_MISSING")
            if qualification == "excluded":
                _need(any(r["target_id"] == vid and r["qualification"] == "excluded" and r["review_id"] in record["qualification_review_refs"] for r in manual["qualification_reviews"]), "QUALIFICATION_REVIEW_MISSING")
            else:
                _need(human_semantics_complete(c, vid) or any(annotations[aid]["source"]["verified"] for aid in record["annotation_refs"]), "VERIFIED_SEMANTICS_MISSING")
        return manual

    def _validate_lineage(self, c, provenance, source, variants):
        base = None
        targets = set()
        if provenance.get("format") == "banner-refresh.v1":
            base = provenance.get("base", {}).get("export_id")
            new = provenance.get("new", {})
            targets = set(provenance.get("target_ids", []))
            expected_targets = {f"minecraft:{color}_{form}" for color in "black blue brown cyan gray green light_blue light_gray lime magenta orange pink purple red white yellow".split() for form in ("banner", "wall_banner")}
            _need(base and new.get("export_id") == source["export_id"] and new.get("import_id") == source["import_id"] and targets == expected_targets and targets <= variants.keys(), "LEGACY_LINEAGE_INVALID")
        for row in c.execute("SELECT envelope_json FROM provider_requests WHERE stage='offline_annotation' AND status='succeeded'"):
            envelope = json.loads(row[0])
            validate_record("provider-batch-envelope.v1", envelope, repo_root=self.repo_root)
            ids = {item["variant_id"] for item in envelope["input_summary"]["tile_variant_map"]}
            _need(ids <= variants.keys(), "PROVIDER_REFERENCE_INVALID")
            if envelope["export_id"] != source["export_id"]:
                _need(base is not None and envelope["export_id"] == base and not ids.intersection(targets), "LEGACY_LINEAGE_INVALID")

    def _write_index(self, path, blocks, states, variants, features, semantics, version):
        sql, _ = packaged_release_index_schema()
        c = sqlite3.connect(path)
        try:
            c.executescript(sql.decode("utf-8"))
            c.execute("INSERT INTO schema_meta VALUES (2)")
            for bid, block in blocks.items():
                names = block["official_names"]
                c.execute("INSERT INTO blocks VALUES (?,?,?,?,?,?,?,?)", (bid, version, block["translation_key"], names["zh_cn"], names["en_us"], block["default_state_id"], canonical_json(block["machine_facts"]), canonical_json(block)))
            for sid, state in states.items():
                c.execute("INSERT INTO states VALUES (?,?,?,?,?)", (sid, state["block_id"], canonical_json(state["properties"]), int(state["is_default"]), canonical_json(state)))
            mode = "normalized_like"
            if not self.force_normalized_like:
                try:
                    c.execute("CREATE VIRTUAL TABLE search_fts USING fts5(variant_id UNINDEXED, normalized_text, tokenize='trigram')")
                    mode = "trigram"
                except sqlite3.OperationalError:
                    pass
            if mode == "normalized_like":
                c.execute("CREATE TABLE search_text(variant_id TEXT PRIMARY KEY, normalized_text TEXT NOT NULL)")
                c.execute("CREATE INDEX search_text_normalized_idx ON search_text(normalized_text)")
            for vid, record in variants.items():
                root = "previews/minecraft/" + vid.removeprefix("minecraft:")
                render = record["render"]
                c.execute("INSERT INTO visual_variants VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (vid, record["block_id"], record["canonical_state_id"], canonical_json(record["represented_state_ids"]), root + "/preview.png", root + "/mask.png", root + "/render.json", render["image_sha256"], render["mask_sha256"], render["render_metadata_sha256"], record["candidate_qualification"], canonical_json(record["warnings"]), canonical_json(record), canonical_json(features[vid])))
                c.execute("INSERT INTO annotations VALUES (?,?)", (vid, canonical_json(semantics[vid])))
                if record["candidate_qualification"] in {"eligible", "conditional"}:
                    text = normalize_text(WorkspaceQueryService._document_content(blocks[record["block_id"]], record, semantics[vid]))
                    table = "search_fts" if mode == "trigram" else "search_text"
                    c.execute(f"INSERT INTO {table} VALUES (?,?)", (vid, text))
            c.commit()
            _need(c.execute("PRAGMA integrity_check").fetchone()[0] == "ok" and c.execute("PRAGMA foreign_key_check").fetchone() is None, "INDEX_INVALID")
        finally:
            c.close()
        return mode

    def _finish(self, database, run_id, build_id, release_id, built_at):
        with database.transaction() as c:
            _need(c.execute("SELECT 1 FROM runs WHERE run_id=?", (run_id,)).fetchone() is not None, "RUN_NOT_FOUND")
            event_id = "release_" + build_id
            now = utc_now()
            c.execute("INSERT OR IGNORE INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (event_id, "RELEASE_BUILT", run_id, canonical_json({"release_id": release_id, "release_build_id": build_id, "built_at": built_at}), now))
            stage = c.execute("SELECT cursor_json FROM stage_runs WHERE run_id=? AND stage='BUILD_RELEASE'", (run_id,)).fetchone()
            cursor = json.loads(stage[0]) if stage else {}
            if cursor.get("release_build_id") not in {None, build_id} or cursor.get("completed"):
                return  # Reading an older result cannot replace a newer action's cursor.
            c.execute("UPDATE stage_runs SET status='succeeded',worker_id=NULL,finished_at=?,cursor_json=? WHERE run_id=? AND stage='BUILD_RELEASE'", (now, canonical_json({"release_build_id": build_id, "release_id": release_id, "completed": True}), run_id))
            c.execute("UPDATE runs SET status='succeeded',current_stage='BUILD_RELEASE',boundary_event='RELEASE_BUILT',finished_at=? WHERE run_id=?", (now, run_id))

    def _result(self, info, *, reused):
        release, report, manifest = info["release"], info["report"], info["manifest"]
        return {"release_build_id": report["release_build_id"], "release_id": release["release_id"], "run_id": report["run_id"], "minecraft_version": release["minecraft_version"], "relative_path": self.data_root.relative_ref(info["path"]), "status": "built", "manifest_sha256": release["manifest_sha256"], "quality_report_sha256": manifest["quality_report_sha256"], "built_at": release["built_at"], "reused": reused}


def _provider_snapshot(config: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = config.get("provider_snapshot")
    if not isinstance(raw, dict):
        return None
    profile_value = raw.get("profile")
    profile: dict[str, Any] = profile_value if isinstance(profile_value, dict) else {}
    def choose(name: str, fallback: Any = None) -> Any:
        return raw.get(name, profile.get(name, fallback))
    snapshot = {
        "adapter": choose("adapter"),
        "profile_id": choose("profile_id"),
        "model_id": choose("model_id"),
        "base_url_stable_id": choose("base_url_stable_id"),
        "secret_reference": choose("secret_reference"),
        "prompt_version": choose("prompt_version"),
        "request_envelope_schema_id": "provider-batch-envelope.v1",
        "wire_schema_ids": {
            "offline_annotation": choose("annotation_output_schema_id", "annotation-batch-output.v1"),
            "query_spec": choose("query_spec_output_schema_id", "query-spec-output.v1"),
            "visual_rerank": choose("rerank_output_schema_id", "rerank-output.v1"),
        },
        "search_ranking_version": choose("search_ranking_version"),
    }
    if snapshot["adapter"] not in {"openai_responses", "openai_chat_completions"} or snapshot["request_envelope_schema_id"] != "provider-batch-envelope.v1":
        return None
    if snapshot["wire_schema_ids"] != {
        "offline_annotation": "annotation-batch-output.v1",
        "query_spec": "query-spec-output.v1",
        "visual_rerank": "rerank-output.v1",
    }:
        return None
    if not all(isinstance(snapshot.get(key), str) and snapshot[key] for key in ("profile_id", "model_id", "base_url_stable_id", "secret_reference", "prompt_version", "search_ranking_version")):
        return None
    return snapshot
