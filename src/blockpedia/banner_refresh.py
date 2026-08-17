"""The deliberately narrow D-045 banner workspace refresh operation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .features import build_visual_variant_record, extract_features
from .importer import (
    ImportCheck,
    ImportNotAllowed,
    _copy_verified_snapshot,
    _failure_for_state,
    _project_block,
    _project_state,
    _read_jsonl,
    _unsafe_directory_entry,
    _unsafe_file_entry,
)
from .paths import safe_relative_posix_ref
from .schema import validate_record
from .search import WorkspaceQueryService
from .storage import WorkspaceDatabase, utc_now
from .worker import (
    _annotation_payload_signature,
    _batch_input_signature,
    _geometry_from_source,
    _id,
    _json,
    _source_machine_tags,
    _stable_job_id,
)


BANNER_COLORS = (
    "black",
    "blue",
    "brown",
    "cyan",
    "gray",
    "green",
    "light_blue",
    "light_gray",
    "lime",
    "magenta",
    "orange",
    "pink",
    "purple",
    "red",
    "white",
    "yellow",
)
BANNER_TARGET_IDS = tuple(sorted({f"minecraft:{color}_{form}" for color in BANNER_COLORS for form in ("banner", "wall_banner")}))
BANNER_POLICY_TOKEN = (
    "banner-camera.v2;namespace=minecraft;types=BannerBlock,WallBannerBlock;"
    "colors=black,blue,brown,cyan,gray,green,light_blue,light_gray,lime,magenta,"
    "orange,pink,purple,red,white,yellow;forms=banner,wall_banner"
)
BANNER_REFRESH_SCHEMA = "banner-refresh.v1"
BANNER_RENDER_FILE_NAMES = ("preview.png", "mask.png", "render.json")
_LINEAGE_KEYS = {
    "export_id",
    "exporter_version",
    "producer_version",
    "created_at",
    "completed_at",
    "updated_at",
    "started_at",
    "finished_at",
    "logical_input_signature",
    "render_input_signature",
    "input_signature",
    "camera_policy_version",
    "camera_sha256",
    "render_environment_sha256",
}
_MANIFEST_DYNAMIC_COUNT_KEYS = {
    "selected_variants",
    "selected_variant_count",
    "selected_variant_records",
    "skipped_variants",
    "skipped_variant_count",
    "skipped_variant_records",
    "render_count",
    "rendered_count",
    "block_records",
    "state_records",
    "failure_count",
    "failure_records",
    "pending_review_records",
}
_VARIANT_TRANSITION_FIELDS = {
    "status",
    "candidate_qualification",
    "skip_reason_code",
    "skip_reason",
    "canonical_state_id",
    "represented_state_ids",
    "context",
    "selection",
    "machine_facts",
    "render",
}
_SELECTED_VARIANT_FIELDS = {
    "canonical_state_id",
    "represented_state_ids",
    "context",
    "selection",
    "machine_facts",
    "render",
}


class BannerRefreshFailure(RuntimeError):
    def __init__(self, code: str, message: str = "banner refresh is not allowed") -> None:
        self.code = code
        super().__init__(message)


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest_file(path: Path) -> str:
    return _digest_bytes(path.read_bytes())


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(_json(value), encoding="utf-8")
    os.replace(temporary, path)


def _assert_directory(path: Path, *, create: bool = False) -> None:
    if create and not path.exists():
        path.mkdir(parents=True, exist_ok=True)
    if _unsafe_directory_entry(path):
        raise BannerRefreshFailure("BANNER_REFRESH_PATH_UNSAFE")


def _assert_file(path: Path) -> None:
    if _unsafe_file_entry(path) or path.stat().st_nlink != 1:
        raise BannerRefreshFailure("BANNER_REFRESH_PATH_UNSAFE")


def _assert_tree_safe(root: Path) -> None:
    _assert_directory(root)
    for path in root.rglob("*"):
        if path.is_dir():
            _assert_directory(path)
        elif path.is_file():
            _assert_file(path)
        else:
            raise BannerRefreshFailure("BANNER_REFRESH_PATH_UNSAFE")


def _workspace_path(workspace: Path, relative_ref: str) -> Path:
    safe_relative_posix_ref(relative_ref)
    path = workspace / (Path("export") / relative_ref if "/" not in relative_ref else Path(relative_ref))
    if path != path.absolute() or workspace.absolute() not in path.absolute().parents:
        raise BannerRefreshFailure("BANNER_REFRESH_PATH_UNSAFE")
    return path


def _snapshot_path(snapshot: Path, relative_ref: str) -> Path:
    safe_relative_posix_ref(relative_ref)
    path = snapshot / relative_ref
    if snapshot.absolute() not in path.absolute().parents:
        raise BannerRefreshFailure("BANNER_REFRESH_PATH_UNSAFE")
    return path


def _checked_file_map(snapshot: Path, check: ImportCheck) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    expected = {str(item["relative_ref"]): str(item["sha256"]) for item in check.expected_files}
    for relative_ref, expected_hash in expected.items():
        path = _snapshot_path(snapshot, relative_ref)
        _assert_file(path)
        data = path.read_bytes()
        if _digest_bytes(data) != expected_hash:
            raise BannerRefreshFailure("BANNER_REFRESH_CHECK_CHANGED")
        result[relative_ref] = data
    checksum_path = _snapshot_path(snapshot, "checksums.sha256")
    _assert_file(checksum_path)
    if _digest_file(checksum_path) != check.checksum_sha256:
        raise BannerRefreshFailure("BANNER_REFRESH_CHECK_CHANGED")
    result["checksums.sha256"] = checksum_path.read_bytes()
    return result


def _workspace_file_map(workspace: Path, check: ImportCheck) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    expected = {str(item["relative_ref"]): str(item["sha256"]) for item in check.expected_files}
    for relative_ref, expected_hash in expected.items():
        path = _workspace_path(workspace, relative_ref)
        _assert_file(path)
        data = path.read_bytes()
        if _digest_bytes(data) != expected_hash:
            raise BannerRefreshFailure("BANNER_REFRESH_BASE_SOURCE_CHANGED")
        result[relative_ref] = data
    checksum_path = _workspace_path(workspace, "checksums.sha256")
    _assert_file(checksum_path)
    if _digest_file(checksum_path) != check.checksum_sha256:
        raise BannerRefreshFailure("BANNER_REFRESH_BASE_SOURCE_CHANGED")
    result["checksums.sha256"] = checksum_path.read_bytes()
    return result


def _json_document(files: Mapping[str, bytes], relative_ref: str) -> dict[str, Any]:
    try:
        value = json.loads(files[relative_ref].decode("utf-8"))
    except (KeyError, UnicodeDecodeError, ValueError, TypeError) as exc:
        raise BannerRefreshFailure("BANNER_REFRESH_EXPORT_INVALID") from exc
    if not isinstance(value, dict):
        raise BannerRefreshFailure("BANNER_REFRESH_EXPORT_INVALID")
    return value


def _jsonl_document(files: Mapping[str, bytes], relative_ref: str) -> list[dict[str, Any]]:
    try:
        lines = files[relative_ref].decode("utf-8").splitlines()
    except (KeyError, UnicodeDecodeError) as exc:
        raise BannerRefreshFailure("BANNER_REFRESH_EXPORT_INVALID") from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line:
            continue
        try:
            value = json.loads(line)
        except (ValueError, TypeError) as exc:
            raise BannerRefreshFailure("BANNER_REFRESH_EXPORT_INVALID") from exc
        if not isinstance(value, dict):
            raise BannerRefreshFailure("BANNER_REFRESH_EXPORT_INVALID")
        records.append(value)
    return records


def _record_map(records: Iterable[Mapping[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for record in records:
        value = record.get(key)
        if not isinstance(value, str) or value in result:
            raise BannerRefreshFailure("BANNER_REFRESH_EXPORT_INVALID")
        result[value] = dict(record)
    return result


def _normalize_lineage(value: Any, *, manifest: bool = False) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in _LINEAGE_KEYS:
                continue
            if manifest and key == "counts" and isinstance(item, dict):
                result[key] = {subkey: subvalue for subkey, subvalue in item.items() if subkey not in _MANIFEST_DYNAMIC_COUNT_KEYS}
            else:
                result[key] = _normalize_lineage(item, manifest=manifest)
        return result
    if isinstance(value, list):
        return [_normalize_lineage(item, manifest=manifest) for item in value]
    return value


def _assert_same_records(base: Mapping[str, Mapping[str, Any]], replacement: Mapping[str, Mapping[str, Any]], *, excluded: set[str], label: str) -> None:
    if set(base) - excluded != set(replacement) - excluded:
        raise BannerRefreshFailure(f"BANNER_REFRESH_{label.upper()}_DIFF")
    for key in sorted(set(base) - excluded):
        if _normalize_lineage(base[key]) != _normalize_lineage(replacement[key]):
            raise BannerRefreshFailure(f"BANNER_REFRESH_{label.upper()}_DIFF")


def _variant_render_refs(record: Mapping[str, Any]) -> tuple[str, str, str]:
    render = record.get("render")
    if not isinstance(render, Mapping):
        raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
    refs: list[str] = []
    for key in ("preview_path", "mask_path", "render_metadata_path"):
        value = render.get(key)
        if not isinstance(value, str):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        safe_relative_posix_ref(value)
        refs.append(value)
    return refs[0], refs[1], refs[2]


def _package(files: Mapping[str, bytes]) -> dict[str, Any]:
    return {
        "manifest": _json_document(files, "manifest.json"),
        "blocks": _record_map(_jsonl_document(files, "blocks.jsonl"), "block_id"),
        "states": _record_map(_jsonl_document(files, "states.jsonl"), "state_id"),
        "variants": _record_map(_jsonl_document(files, "variants.jsonl"), "variant_id"),
        "failures": _record_map(_jsonl_document(files, "failures.jsonl"), "failure_id"),
        "files": files,
    }


def _target_failure_records(package: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for failure in package["failures"].values():
        target_id = failure.get("variant_id")
        if target_id not in BANNER_TARGET_IDS:
            continue
        if failure.get("reason_code") != "OBJECT_OFF_CANVAS" or target_id in result:
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_SET_INVALID")
        result[str(target_id)] = failure
    if set(result) != set(BANNER_TARGET_IDS):
        raise BannerRefreshFailure("BANNER_REFRESH_TARGET_SET_INVALID")
    return result


def _target_render_paths(target_ids: Iterable[str], variants: Mapping[str, Mapping[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for target_id in target_ids:
        if target_id not in variants or variants[target_id].get("status") != "selected":
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        target_refs = _variant_render_refs(variants[target_id])
        suffix = target_id.removeprefix("minecraft:")
        expected = {
            f"renders/minecraft/{suffix}/preview.png",
            f"renders/minecraft/{suffix}/mask.png",
            f"renders/minecraft/{suffix}/render.json",
        }
        if set(target_refs) != expected:
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        refs.update(target_refs)
    if len(refs) != 96:
        raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
    return refs


def _compare_exports(base: Mapping[str, Any], replacement: Mapping[str, Any], target_ids: tuple[str, ...], base_export_id: str, new_export_id: str) -> set[str]:
    if base["manifest"].get("export_id") != base_export_id or replacement["manifest"].get("export_id") != new_export_id:
        raise BannerRefreshFailure("BANNER_REFRESH_LINEAGE_INVALID")
    if replacement["manifest"].get("minecraft_version") != base["manifest"].get("minecraft_version"):
        raise BannerRefreshFailure("BANNER_REFRESH_LINEAGE_INVALID")
    targets = set(target_ids)
    base_failures = _target_failure_records(base)
    for failure in replacement["failures"].values():
        if failure.get("variant_id") in targets:
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
    for target_id in targets:
        base_variant = base["variants"].get(target_id)
        replacement_variant = replacement["variants"].get(target_id)
        if not isinstance(base_variant, Mapping) or not isinstance(replacement_variant, Mapping):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        if base_variant.get("status") != "skipped" or base_variant.get("skip_reason_code") != "OBJECT_OFF_CANVAS":
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        if not isinstance(base_variant.get("skip_reason"), str) or not base_variant["skip_reason"].strip():
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        if any(field in base_variant for field in _SELECTED_VARIANT_FIELDS):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        if replacement_variant.get("status") != "selected":
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        if any(field in replacement_variant for field in ("skip_reason_code", "skip_reason")):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        if any(field not in replacement_variant for field in _SELECTED_VARIANT_FIELDS):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
    _assert_same_records(base["blocks"], replacement["blocks"], excluded=set(), label="machine")
    target_state_ids = {
        state_id for state_id, state in base["states"].items() if state.get("block_id") in targets
    }
    _assert_same_records(base["states"], replacement["states"], excluded=target_state_ids, label="machine")
    _assert_same_records(base["variants"], replacement["variants"], excluded=targets, label="machine")
    target_failure_ids = {str(failure["failure_id"]) for failure in base_failures.values()}
    _assert_same_records(base["failures"], replacement["failures"], excluded=target_failure_ids, label="machine")
    for target_id in targets:
        base_variant = dict(base["variants"][target_id])
        replacement_variant = dict(replacement["variants"][target_id])
        for record in (base_variant, replacement_variant):
            for field in _VARIANT_TRANSITION_FIELDS:
                record.pop(field, None)
        if _normalize_lineage(base_variant) != _normalize_lineage(replacement_variant):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        base_states = [state for state in base["states"].values() if state.get("block_id") == target_id]
        new_states = [state for state in replacement["states"].values() if state.get("block_id") == target_id]
        if not base_states or len(base_states) != len(new_states):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
        for old_state, state in zip(sorted(base_states, key=lambda item: str(item["state_id"])), sorted(new_states, key=lambda item: str(item["state_id"]))):
            if state.get("mapping_status") != "mapped" or target_id not in state.get("variant_ids", []):
                raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
            old_machine = dict(old_state)
            new_machine = dict(state)
            for record in (old_machine, new_machine):
                record.pop("mapping_status", None)
                record.pop("variant_ids", None)
            if _normalize_lineage(old_machine) != _normalize_lineage(new_machine):
                raise BannerRefreshFailure("BANNER_REFRESH_TARGET_DIFF")
    target_render_paths = _target_render_paths(target_ids, replacement["variants"])
    base_files = base["files"]
    new_files = replacement["files"]
    if set(new_files) - set(base_files) != target_render_paths or set(base_files) - set(new_files):
        raise BannerRefreshFailure("BANNER_REFRESH_RENDER_DIFF")
    for relative_ref in sorted(set(base_files) & set(new_files)):
        if relative_ref in target_render_paths:
            raise BannerRefreshFailure("BANNER_REFRESH_RENDER_DIFF")
        if relative_ref.startswith("renders/") and base_files[relative_ref] != new_files[relative_ref]:
            raise BannerRefreshFailure("BANNER_REFRESH_RENDER_DIFF")
    base_manifest = _normalize_lineage(base["manifest"], manifest=True)
    replacement_manifest = _normalize_lineage(replacement["manifest"], manifest=True)
    _assert_schema_inventory_transition(base["manifest"], replacement["manifest"])
    for manifest in (base_manifest, replacement_manifest):
        inventory = manifest.get("schema_inventory")
        if isinstance(inventory, list):
            for item in inventory:
                if isinstance(item, dict) and item.get("schema_id") == "export-manifest.v1":
                    item["schema_sha256"] = "<camera.v2-allowed-transition>"
    if base_manifest != replacement_manifest:
        raise BannerRefreshFailure("BANNER_REFRESH_MANIFEST_DIFF")
    return target_render_paths


def _validate_current_target_state(database: WorkspaceDatabase, package: Mapping[str, Any], target_ids: tuple[str, ...], run_id: str) -> None:
    targets = set(target_ids)
    failures = _target_failure_records(package)
    review_rows = database.fetchall(
        "SELECT review_id,target_id,reason_code,status,target_type FROM review_tasks WHERE target_type='variant' AND reason_code='OBJECT_OFF_CANVAS' AND status='open'"
    )
    target_reviews = [row for row in review_rows if str(row["target_id"]) in targets]
    if {str(row["target_id"]) for row in target_reviews} != targets or len(target_reviews) != len(targets):
        raise BannerRefreshFailure("BANNER_REFRESH_TARGET_REVIEWS_INVALID")
    for table, column in (("variants", "variant_id"), ("features", "variant_id"), ("annotations", "subject_id"), ("overrides", "target_id")):
        for target_id in targets:
            if database.fetchone(f"SELECT 1 FROM {table} WHERE {column}=? LIMIT 1", (target_id,)) is not None:
                raise BannerRefreshFailure("BANNER_REFRESH_TARGET_ALREADY_PROJECTED")
    for row in database.fetchall("SELECT logical_key,cursor_json FROM jobs WHERE run_id=?", (run_id,)):
        if row["logical_key"] in targets or any(target_id in str(row["cursor_json"]) for target_id in targets):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_ALREADY_PROJECTED")
    for row in database.fetchall("SELECT envelope_json FROM provider_requests"):
        if any(target_id in str(row["envelope_json"]) for target_id in targets):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_ALREADY_PROJECTED")
    for target_id, failure in failures.items():
        state_rows = database.fetchall("SELECT record_json,failure_id FROM states WHERE block_id=?", (target_id,))
        if not state_rows or any(json.loads(row["record_json"]).get("mapping_status") != "skipped" for row in state_rows):
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_STATE_INVALID")
        if database.fetchone("SELECT 1 FROM failures WHERE failure_id=?", (failure["failure_id"],)) is None:
            raise BannerRefreshFailure("BANNER_REFRESH_TARGET_STATE_INVALID")


def _copy_file_checked(source: Path, destination: Path) -> None:
    _assert_file(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _assert_directory(destination.parent)
    if destination.exists():
        _assert_file(destination)
    shutil.copy2(source, destination)


def _backup_optional_file(source: Path, backup: Path) -> bool:
    if not source.exists():
        return False
    _assert_file(source)
    _copy_file_checked(source, backup)
    return True


def _restore_optional_file(destination: Path, backup: Path, existed: bool) -> None:
    if destination.exists():
        _assert_file(destination)
        destination.unlink()
    if existed:
        _copy_file_checked(backup, destination)


def _journal_path(workspace: Path) -> Path:
    return workspace / "banner-refresh.v1.json"


def _cleanup_paths(*paths: Path) -> None:
    for path in paths:
        if not path.exists():
            continue
        if _unsafe_reparse(path):
            raise BannerRefreshFailure("BANNER_REFRESH_PATH_UNSAFE")
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()


def _unsafe_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        junction = getattr(path, "is_junction", None)
        if callable(junction) and junction():
            return True
        value = path.lstat()
        return bool(stat.S_ISLNK(value.st_mode) or getattr(value, "st_file_attributes", 0) & 0x400)
    except OSError:
        return True


def _restore_journal(workspace: Path, journal: Mapping[str, Any]) -> None:
    backup = workspace / str(journal["backup_ref"])
    staging = workspace / str(journal["staging_ref"])
    if backup.exists():
        _assert_directory(backup)
        for name in ("export", "renders"):
            current = workspace / name
            saved = backup / name
            if saved.exists():
                if current.exists():
                    _cleanup_paths(current)
                os.replace(saved, current)
        for item in journal.get("generated_files", []):
            if not isinstance(item, Mapping):
                continue
            if "existed" not in item:
                continue
            relative_ref = str(item["relative_ref"])
            destination = workspace / relative_ref
            saved = backup / relative_ref
            _restore_optional_file(destination, saved, bool(item.get("existed")))
    _cleanup_paths(staging, backup)


def _install_files(workspace: Path, staging: Path, backup: Path, generated_files: list[dict[str, Any]]) -> None:
    _assert_directory(workspace)
    _assert_directory(staging)
    if backup.exists():
        _assert_directory(backup)
    else:
        backup.mkdir(parents=True, exist_ok=False)
    for name in ("export", "renders"):
        current = workspace / name
        staged = staging / name
        _assert_tree_safe(current)
        _assert_tree_safe(staged)
        os.replace(current, backup / name)
        os.replace(staged, current)
    for item in generated_files:
        relative_ref = str(item["relative_ref"])
        destination = workspace / relative_ref
        staged = staging / relative_ref
        backup_file = backup / relative_ref
        if "existed" not in item:
            item["existed"] = _backup_optional_file(destination, backup_file)
        if bool(item.get("install", True)):
            if staged.exists():
                _copy_file_checked(staged, destination)
            elif destination.exists():
                destination.unlink()
        elif destination.exists():
            destination.unlink()


def _verify_installed_files(workspace: Path, check: ImportCheck) -> None:
    expected = {str(item["relative_ref"]): str(item["sha256"]) for item in check.expected_files}
    for relative_ref, digest in expected.items():
        path = _workspace_path(workspace, relative_ref)
        _assert_file(path)
        if _digest_file(path) != digest:
            raise BannerRefreshFailure("BANNER_REFRESH_INSTALL_VERIFY_FAILED")


def _build_target_features(worker: Any, staging: Path, variants: Mapping[str, Mapping[str, Any]], target_ids: tuple[str, ...]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target_id in target_ids:
        source = dict(variants[target_id])
        render = source["render"]
        preview = staging / render["preview_path"]
        mask = staging / render["mask_path"]
        _assert_file(preview)
        _assert_file(mask)
        features = extract_features(preview, mask, geometry=_geometry_from_source(source), machine_tags=_source_machine_tags(source))
        record = build_visual_variant_record(source, features)
        validate_record("visual-variant-record.v1", record, repo_root=worker.repo_root)
        payload = _json({"record": record, "features": features}).encode("utf-8")
        relative_ref = f"generated/features/{target_id.replace(':', '_')}.json"
        safe_relative_posix_ref(relative_ref)
        output = staging / relative_ref
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(payload)
        results.append({"variant_id": target_id, "features": features, "record": record, "output_hash": _digest_bytes(payload), "relative_ref": relative_ref})
    return results


def _refresh_result(run_id: str, new_import_id: str, new_export_id: str, *, idempotent: bool = False) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "new_import_id": new_import_id,
        "new_export_id": new_export_id,
        "target_count": 32,
        "new_variant_count": 32,
        "new_feature_count": 32,
        "new_ai_job_count": 3,
        "current_stage": "AI_ANNOTATE",
        "idempotent": idempotent,
    }


def _is_canonical_provenance(
    value: Any,
    *,
    check_id: str,
    expected_base_export_id: str,
    current_import: Mapping[str, Any],
) -> bool:
    if not isinstance(value, Mapping):
        return False
    if set(value) != {"format", "base", "new", "check_id", "target_ids", "policy_token"}:
        return False
    base = value.get("base")
    new = value.get("new")
    if not isinstance(base, Mapping) or not isinstance(new, Mapping):
        return False
    if set(base) != {"import_id", "export_id", "manifest_sha256", "checksum_sha256"}:
        return False
    if set(new) != {"import_id", "export_id", "manifest_sha256", "checksum_sha256"}:
        return False
    return (
        value.get("format") == BANNER_REFRESH_SCHEMA
        and value.get("check_id") == check_id
        and value.get("target_ids") == list(BANNER_TARGET_IDS)
        and value.get("policy_token") == BANNER_POLICY_TOKEN
        and base.get("export_id") == expected_base_export_id
        and new.get("import_id") == current_import["import_id"]
        and new.get("export_id") == current_import["export_id"]
        and new.get("manifest_sha256") == current_import["manifest_sha256"]
        and new.get("checksum_sha256") == current_import["checksum_sha256"]
    )


def _assert_schema_inventory_transition(base_manifest: Mapping[str, Any], replacement_manifest: Mapping[str, Any]) -> None:
    base_inventory = base_manifest.get("schema_inventory")
    replacement_inventory = replacement_manifest.get("schema_inventory")
    if not isinstance(base_inventory, list) or not isinstance(replacement_inventory, list):
        raise BannerRefreshFailure("BANNER_REFRESH_MANIFEST_DIFF")
    base_identity = [(item.get("schema_id"), item.get("repository_path")) for item in base_inventory if isinstance(item, Mapping)]
    replacement_identity = [(item.get("schema_id"), item.get("repository_path")) for item in replacement_inventory if isinstance(item, Mapping)]
    if len(base_identity) != len(base_inventory) or base_identity != replacement_identity:
        raise BannerRefreshFailure("BANNER_REFRESH_MANIFEST_DIFF")
    for base_item, replacement_item in zip(base_inventory, replacement_inventory):
        if not isinstance(base_item, Mapping) or not isinstance(replacement_item, Mapping):
            raise BannerRefreshFailure("BANNER_REFRESH_MANIFEST_DIFF")
        if base_item.get("schema_id") == "export-manifest.v1":
            continue
        if base_item.get("schema_sha256") != replacement_item.get("schema_sha256"):
            raise BannerRefreshFailure("BANNER_REFRESH_MANIFEST_DIFF")


def refresh_banner_workspace(
    *,
    imports: Any,
    worker: Any,
    run_id: str,
    check_id: str,
    expected_base_export_id: str,
    target_ids: list[str],
    confirm: bool,
) -> dict[str, Any]:
    requested_targets = tuple(target_ids)
    if not confirm or requested_targets != BANNER_TARGET_IDS:
        raise BannerRefreshFailure("BANNER_REFRESH_TARGET_SET_INVALID")
    if not isinstance(expected_base_export_id, str) or not expected_base_export_id:
        raise BannerRefreshFailure("BANNER_REFRESH_BASE_INVALID")
    check, replacement_snapshot = imports.resolve_checked_snapshot(check_id)
    if check.minecraft_version != "26.2":
        raise BannerRefreshFailure("BANNER_REFRESH_VERSION_INVALID")
    replacement_files = _checked_file_map(replacement_snapshot, check)
    replacement_package = _package(replacement_files)
    workspace = worker.data_root.workspace_dir(check.minecraft_version, run_id)
    _assert_directory(workspace)
    journal_path = _journal_path(workspace)
    if journal_path.exists():
        _assert_file(journal_path)
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise BannerRefreshFailure("BANNER_REFRESH_RECOVERY_REQUIRED") from exc
        if not isinstance(journal, Mapping) or journal.get("schema_version") != BANNER_REFRESH_SCHEMA or journal.get("run_id") != run_id:
            raise BannerRefreshFailure("BANNER_REFRESH_RECOVERY_REQUIRED")
        if journal.get("check_id") != check_id or journal.get("base_export_id") != expected_base_export_id or journal.get("target_ids") != list(BANNER_TARGET_IDS):
            raise BannerRefreshFailure("BANNER_REFRESH_RECOVERY_REQUIRED")
    else:
        journal = None
    with worker.open_database(run_id, check.minecraft_version) as database:
        run = database.fetchone("SELECT * FROM runs WHERE run_id=?", (run_id,))
        current_import = database.fetchone("SELECT * FROM imports WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)", (run_id,))
        if run is None or current_import is None:
            raise BannerRefreshFailure("RUN_NOT_FOUND")
        if journal is not None and current_import["import_id"] == journal.get("new_import_id"):
            _verify_installed_files(workspace, check)
            if journal.get("phase") != "COMMITTED":
                journal = {**journal, "phase": "COMMITTED"}
                _write_json_atomic(journal_path, journal)
            _cleanup_paths(workspace / str(journal["staging_ref"]), workspace / str(journal["backup_ref"]), journal_path)
            return _refresh_result(run_id, str(current_import["import_id"]), str(current_import["export_id"]), idempotent=True)
        if journal is not None:
            if current_import["import_id"] != journal.get("base_import_id"):
                raise BannerRefreshFailure("BANNER_REFRESH_RECOVERY_REQUIRED")
            _restore_journal(workspace, journal)
            journal_path.unlink(missing_ok=True)
        provenance: dict[str, Any] | None = None
        try:
            provenance = json.loads(current_import["report_json"])
        except (ValueError, TypeError):
            provenance = None
        if isinstance(provenance, Mapping) and provenance.get("format") == BANNER_REFRESH_SCHEMA:
            if _is_canonical_provenance(
                provenance,
                check_id=check_id,
                expected_base_export_id=expected_base_export_id,
                current_import=current_import,
            ):
                return _refresh_result(run_id, str(current_import["import_id"]), str(current_import["export_id"]), idempotent=True)
            raise BannerRefreshFailure("BANNER_REFRESH_ALREADY_APPLIED")
        if expected_base_export_id != current_import["export_id"]:
            raise BannerRefreshFailure("BANNER_REFRESH_BASE_MISMATCH")
        if check.export_id == expected_base_export_id:
            raise BannerRefreshFailure("BANNER_REFRESH_REPLACEMENT_INVALID")
        if run["status"] != "needs_review" or run["current_stage"] != "HUMAN_REVIEW":
            raise BannerRefreshFailure("BANNER_REFRESH_RUN_STATE_INVALID")
        if worker.has_live_ai_futures(run_id):
            raise BannerRefreshFailure("BANNER_REFRESH_LIVE_WORK")
        if database.fetchone("SELECT 1 FROM jobs WHERE run_id=? AND status='running' LIMIT 1", (run_id,)) is not None:
            raise BannerRefreshFailure("BANNER_REFRESH_LIVE_WORK")
        if database.fetchone("SELECT 1 FROM stage_runs WHERE run_id=? AND status='running' LIMIT 1", (run_id,)) is not None:
            raise BannerRefreshFailure("BANNER_REFRESH_LIVE_WORK")
        base_check = ImportCheck(
            check_id="base",
            minecraft_version=check.minecraft_version,
            export_id=str(current_import["export_id"]),
            source_directory_ref="",
            manifest_sha256=str(current_import["manifest_sha256"]),
            checksum_sha256=str(current_import["checksum_sha256"]),
            snapshot_ref="",
            snapshot_root_sha256=None,
            metadata_sha256=None,
            expected_files=tuple(json.loads(current_import["expected_files_json"])),
            status="passed",
            issues=(),
            can_import=True,
        )
        _assert_tree_safe(workspace / "export")
        _assert_tree_safe(workspace / "renders")
        base_files = _workspace_file_map(workspace, base_check)
        if _digest_bytes(base_files["manifest.json"]) != base_check.manifest_sha256 or _digest_bytes(base_files["checksums.sha256"]) != base_check.checksum_sha256:
            raise BannerRefreshFailure("BANNER_REFRESH_BASE_SOURCE_CHANGED")
        base_package = _package(base_files)
        _validate_current_target_state(database, base_package, BANNER_TARGET_IDS, run_id)
        target_render_paths = _compare_exports(base_package, replacement_package, BANNER_TARGET_IDS, expected_base_export_id, check.export_id)
        new_import_id = _id("import")
        operation_id = "banner_refresh_" + uuid.uuid4().hex[:12]
        staging = workspace / f".{operation_id}.staging"
        backup = workspace / f".{operation_id}.backup"
        if staging.exists() or backup.exists():
            raise BannerRefreshFailure("BANNER_REFRESH_RECOVERY_REQUIRED")
        staging.mkdir()
        generated_files: list[dict[str, Any]] = []
        try:
            _copy_verified_snapshot(replacement_snapshot, staging, check.expected_files, check.checksum_sha256 or "")
            features = _build_target_features(worker, staging, replacement_package["variants"], BANNER_TARGET_IDS)
            current_feature_jobs = database.fetchall("SELECT logical_key,output_hash FROM jobs WHERE run_id=? AND stage='EXTRACT_FEATURES' ORDER BY logical_key", (run_id,))
            outputs = [{"logical_key": str(row["logical_key"]), "output_hash": row["output_hash"]} for row in current_feature_jobs]
            outputs.extend({"logical_key": item["variant_id"], "output_hash": item["output_hash"]} for item in features)
            outputs.sort(key=lambda item: str(item["logical_key"]).encode("utf-8"))
            extract_cursor = {"stage": "EXTRACT_FEATURES", "output_hash": _digest_bytes(_json(outputs).encode("utf-8")), "completed": True, "items": outputs}
            extract_stage_ref = "generated/stages/EXTRACT_FEATURES.json"
            extract_stage_path = staging / extract_stage_ref
            extract_stage_path.parent.mkdir(parents=True, exist_ok=True)
            extract_stage_path.write_bytes(_json({"stage": "EXTRACT_FEATURES", "outputs": outputs}).encode("utf-8"))
            generated_files = [{"relative_ref": item["relative_ref"], "install": True} for item in features]
            generated_files.append({"relative_ref": extract_stage_ref, "install": True})
            for stage_name in ("AI_ANNOTATE", "VALIDATE", "HUMAN_REVIEW"):
                generated_files.append({"relative_ref": f"generated/stages/{stage_name}.json", "install": False})
            journal_data = {
                "schema_version": BANNER_REFRESH_SCHEMA,
                "operation_id": operation_id,
                "run_id": run_id,
                "check_id": check_id,
                "base_import_id": str(current_import["import_id"]),
                "new_import_id": new_import_id,
                "base_export_id": expected_base_export_id,
                "new_export_id": check.export_id,
                "target_ids": list(BANNER_TARGET_IDS),
                "staging_ref": staging.name,
                "backup_ref": backup.name,
                "generated_files": generated_files,
                "phase": "STAGED",
            }
            _write_json_atomic(journal_path, journal_data)
            backup.mkdir(parents=True, exist_ok=False)
            for item in generated_files:
                relative_ref = str(item["relative_ref"])
                item["existed"] = _backup_optional_file(workspace / relative_ref, backup / relative_ref)
            _write_json_atomic(journal_path, {**journal_data, "phase": "FILES_INSTALLING"})
            _install_files(workspace, staging, backup, generated_files)
            _write_json_atomic(journal_path, {**journal_data, "phase": "FILES_INSTALLED"})
            provenance = {
                "format": BANNER_REFRESH_SCHEMA,
                "base": {
                    "import_id": str(current_import["import_id"]),
                    "export_id": expected_base_export_id,
                    "manifest_sha256": base_check.manifest_sha256,
                    "checksum_sha256": base_check.checksum_sha256,
                },
                "new": {
                    "import_id": new_import_id,
                    "export_id": check.export_id,
                    "manifest_sha256": check.manifest_sha256,
                    "checksum_sha256": check.checksum_sha256,
                },
                "check_id": check_id,
                "target_ids": list(BANNER_TARGET_IDS),
                "policy_token": BANNER_POLICY_TOKEN,
            }
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO imports(import_id,minecraft_version,export_id,source_directory_ref,manifest_sha256,checksum_sha256,expected_files_json,report_json,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (new_import_id, check.minecraft_version, check.export_id, "not-persisted", check.manifest_sha256 or "", check.checksum_sha256 or "", _json(check.expected_files), _json(provenance), "passed", utc_now()),
                )
                source_export_artifacts = connection.execute(
                    "SELECT artifact_id FROM artifacts WHERE kind='source_export' AND relative_ref='export/manifest.json'"
                ).fetchall()
                if len(source_export_artifacts) != 1:
                    raise BannerRefreshFailure("BANNER_REFRESH_SOURCE_ARTIFACT_INVALID")
                connection.execute(
                    "UPDATE artifacts SET sha256=?,metadata_json=? WHERE artifact_id=?",
                    (check.manifest_sha256 or "", _json({"export_id": check.export_id}), source_export_artifacts[0]["artifact_id"]),
                )
                replacement_blocks = replacement_package["blocks"]
                replacement_states = replacement_package["states"]
                replacement_failures = replacement_package["failures"]
                for block_id, record in replacement_blocks.items():
                    projected = _project_block(record)
                    validate_record("block-record.v1", projected, repo_root=worker.repo_root)
                    connection.execute("UPDATE blocks SET minecraft_version=?,record_json=? WHERE block_id=?", (check.minecraft_version, _json(projected), block_id))
                for failure_id, failure in replacement_failures.items():
                    if connection.execute("SELECT 1 FROM failures WHERE failure_id=?", (failure_id,)).fetchone() is not None:
                        connection.execute("UPDATE failures SET block_id=?,state_id=?,variant_id=?,record_json=? WHERE failure_id=?", (failure.get("block_id"), failure.get("state_id"), failure.get("variant_id"), _json(failure), failure_id))
                    else:
                        connection.execute("INSERT INTO failures(failure_id,minecraft_version,block_id,state_id,variant_id,record_json) VALUES (?,?,?,?,?,?)", (failure_id, check.minecraft_version, failure.get("block_id"), failure.get("state_id"), failure.get("variant_id"), _json(failure)))
                target_failure_ids = tuple(failure["failure_id"] for failure in _target_failure_records(base_package).values())
                for target_failure_id in target_failure_ids:
                    connection.execute("UPDATE states SET failure_id=NULL WHERE failure_id=?", (target_failure_id,))
                    connection.execute("DELETE FROM failures WHERE failure_id=?", (target_failure_id,))
                for state_id, state in replacement_states.items():
                    failure_id = _failure_for_state(state, list(replacement_failures.values()))
                    projected = _project_state(state, failure_id)
                    validate_record("state-record.v1", projected, repo_root=worker.repo_root)
                    connection.execute("UPDATE states SET block_id=?,minecraft_version=?,record_json=?,failure_id=? WHERE state_id=?", (state["block_id"], check.minecraft_version, _json(projected), failure_id, state_id))
                preserved_rows = connection.execute(
                    "SELECT variant_id,record_json FROM variants WHERE status='selected' AND variant_id NOT IN ({})".format(",".join("?" for _ in BANNER_TARGET_IDS)),
                    BANNER_TARGET_IDS,
                ).fetchall()
                for row in preserved_rows:
                    variant_id = str(row["variant_id"])
                    replacement_variant = replacement_package["variants"].get(variant_id)
                    if not isinstance(replacement_variant, Mapping) or replacement_variant.get("status") != "selected":
                        raise BannerRefreshFailure("BANNER_REFRESH_VARIANT_RECORD_INVALID")
                    try:
                        preserved_record = json.loads(row["record_json"] or "")
                    except (TypeError, ValueError) as exc:
                        raise BannerRefreshFailure("BANNER_REFRESH_VARIANT_RECORD_INVALID") from exc
                    if not isinstance(preserved_record, dict) or not isinstance(preserved_record.get("source"), Mapping):
                        raise BannerRefreshFailure("BANNER_REFRESH_VARIANT_RECORD_INVALID")
                    rebased_record = dict(preserved_record)
                    rebased_record["export_id"] = check.export_id
                    rebased_source = dict(rebased_record["source"])
                    rebased_source["export_id"] = check.export_id
                    rebased_record["source"] = rebased_source
                    validate_record("visual-variant-record.v1", rebased_record, repo_root=worker.repo_root)
                    connection.execute(
                        "UPDATE variants SET source_json=?,record_json=?,minecraft_version=? WHERE variant_id=?",
                        (_json(replacement_variant), _json(rebased_record), check.minecraft_version, variant_id),
                    )
                for item in features:
                    target_id = item["variant_id"]
                    source = replacement_package["variants"][target_id]
                    connection.execute("INSERT INTO variants(variant_id,block_id,minecraft_version,status,source_json,record_json) VALUES (?,?,?,?,?,?)", (target_id, target_id, check.minecraft_version, "selected", _json(source), _json(item["record"])))
                    feature = item["features"]
                    connection.execute("INSERT INTO features(variant_id,input_sha256,feature_extractor_version,feature_json,output_hash) VALUES (?,?,?,?,?)", (target_id, feature["input_sha256"], feature["feature_extractor_version"], _json(feature), item["output_hash"]))
                    for ref_key, hash_key in (("preview_path", "image_sha256"), ("mask_path", "mask_sha256"), ("render_metadata_path", "render_metadata_sha256")):
                        render = source["render"]
                        connection.execute("INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)", (_id("artifact"), None, "render", render[ref_key], render[hash_key], _json({"variant_id": target_id, "hash_mode": "jcs" if ref_key == "render_metadata_path" else "bytes"})))
                    connection.execute("INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)", (_id("artifact"), None, "feature_output", item["relative_ref"], item["output_hash"], _json({"variant_id": target_id})))
                    connection.execute("INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,output_hash,created_at,finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", (_stable_job_id(run_id, target_id, _digest_bytes(_json(source).encode("utf-8"))), run_id, "EXTRACT_FEATURES", target_id, _digest_bytes(_json(source).encode("utf-8")), "succeeded", 0, _json({"last_logical_key": target_id}), item["output_hash"], utc_now(), utc_now()))
                run_config = json.loads(run["config_snapshot_json"])
                profile = worker._run_profile(database, run_id)
                batch_size = int(run_config.get("batch_size", 12))
                if batch_size != 12:
                    raise BannerRefreshFailure("BANNER_REFRESH_BATCH_CONFIG_INVALID")
                connection.execute("UPDATE runs SET import_id=? WHERE run_id=?", (new_import_id, run_id))
                for index, batch_ids in enumerate((list(BANNER_TARGET_IDS[:12]), list(BANNER_TARGET_IDS[12:24]), list(BANNER_TARGET_IDS[24:]))):
                    signature, payload = _batch_input_signature(database, batch_ids, profile, run_id=run_id)
                    logical_key = f"banner_refresh_{index:04d}"
                    connection.execute("INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)", (_stable_job_id(run_id, logical_key, signature), run_id, "AI_ANNOTATE", logical_key, signature, "pending", 0, _json({"approved": False, "tile_ids": [item["tile_id"] for item in payload["tile_map"]], "variant_ids": batch_ids, "input_hash": signature, "payload_signature": signature}), utc_now()))
                connection.execute("UPDATE stage_runs SET status='succeeded',cursor_json=?,worker_id=NULL,heartbeat_at=NULL WHERE run_id=? AND stage='EXTRACT_FEATURES'", (_json(extract_cursor), run_id))
                for stage_name in ("AI_ANNOTATE", "VALIDATE", "HUMAN_REVIEW"):
                    connection.execute("UPDATE stage_runs SET status='pending',cursor_json='{}',worker_id=NULL,recovery_attempt=0,pause_after_item=0,heartbeat_at=NULL,started_at=NULL,finished_at=NULL WHERE run_id=? AND stage=?", (run_id, stage_name))
                connection.execute("UPDATE runs SET status='pending',current_stage='AI_ANNOTATE',boundary_event=NULL,finished_at=NULL WHERE run_id=?", (run_id,))
                connection.execute("UPDATE review_tasks SET status='resolved',resolved_at=? WHERE target_type='variant' AND reason_code='OBJECT_OFF_CANVAS' AND status='open' AND target_id IN ({})".format(",".join("?" for _ in target_ids)), (utc_now(), *target_ids))
                connection.execute("DELETE FROM artifacts WHERE kind='stage_output' AND relative_ref IN ('generated/stages/AI_ANNOTATE.json','generated/stages/VALIDATE.json','generated/stages/HUMAN_REVIEW.json')")
                for stage_name in ("AI_ANNOTATE", "VALIDATE", "HUMAN_REVIEW"):
                    connection.execute("DELETE FROM artifacts WHERE kind='stage_output' AND relative_ref=?", (f"generated/stages/{stage_name}.json",))
                stage_ref = "generated/stages/EXTRACT_FEATURES.json"
                stage_hash = _digest_file(workspace / stage_ref)
                existing_stage_artifact = connection.execute("SELECT artifact_id FROM artifacts WHERE kind='stage_output' AND relative_ref=?", (stage_ref,)).fetchone()
                if existing_stage_artifact is not None:
                    connection.execute("UPDATE artifacts SET sha256=?,metadata_json=? WHERE artifact_id=?", (stage_hash, _json({"stage": "EXTRACT_FEATURES"}), existing_stage_artifact["artifact_id"]))
                else:
                    connection.execute("INSERT INTO artifacts(artifact_id,job_id,kind,relative_ref,sha256,metadata_json) VALUES (?,?,?,?,?,?)", (_id("artifact"), None, "stage_output", stage_ref, stage_hash, _json({"stage": "EXTRACT_FEATURES"})))
                docs = WorkspaceQueryService(database).expected_documents(connection)
                connection.execute("DELETE FROM search_documents")
                if database.fts_mode == "trigram":
                    connection.execute("DELETE FROM fts_documents")
                for document_id, block_id, content, normalized in docs.values():
                    connection.execute("INSERT INTO search_documents(document_id,block_id,content,normalized_content) VALUES (?,?,?,?)", (document_id, block_id, content, normalized))
                    if database.fts_mode == "trigram":
                        connection.execute("INSERT INTO fts_documents(block_id,content) VALUES (?,?)", (block_id, normalized))
                connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_id("audit"), "BANNER_EXPORT_REFRESHED", run_id, _json({"check_id": check_id, "new_import_id": new_import_id, "new_export_id": check.export_id, "target_count": 32, "policy_token": BANNER_POLICY_TOKEN}), utc_now()))
            _write_json_atomic(journal_path, {**journal_data, "phase": "DB_COMMITTED"})
            _verify_installed_files(workspace, check)
            _write_json_atomic(journal_path, {**journal_data, "phase": "COMMITTED"})
            _cleanup_paths(staging, backup, journal_path)
            return _refresh_result(run_id, new_import_id, check.export_id)
        except Exception as exc:
            current = database.fetchone("SELECT import_id FROM runs WHERE run_id=?", (run_id,))
            if current is not None and current["import_id"] == new_import_id:
                raise
            try:
                if backup.exists():
                    _restore_journal(workspace, {"backup_ref": backup.name, "staging_ref": staging.name, "generated_files": generated_files})
                else:
                    _cleanup_paths(staging)
                journal_path.unlink(missing_ok=True)
            except Exception as recovery_error:
                raise BannerRefreshFailure("BANNER_REFRESH_RECOVERY_REQUIRED") from recovery_error
            if isinstance(exc, BannerRefreshFailure):
                raise
            raise BannerRefreshFailure("BANNER_REFRESH_FAILED") from exc
