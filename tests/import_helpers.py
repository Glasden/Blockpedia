"""Use the real single-operation importer in pipeline fixtures."""
import uuid


def import_export(service, source, version="26.2", *, run_id=None):
    run_id = run_id or "run_" + uuid.uuid4().hex
    ref = service.directory_chooser.register_path(source, version)
    service.start_import(run_id, ref, version)
    result = service.imports.wait(run_id)
    assert result["status"] == "succeeded", result
    return result


import json
import hashlib
import shutil
import copy
from pathlib import Path
from typing import Any

def make_two_visual_export(tmp_path: Path, fixture: Any) -> Path:
    export = fixture.make_export(tmp_path)
    read_rows = lambda name: [json.loads(line) for line in (export / name).read_text(encoding="utf-8").splitlines() if line]
    states = read_rows("states.jsonl")
    variants = read_rows("variants.jsonl")
    glass_state = next(row for row in states if row["block_id"] == "minecraft:glass")
    glass_state["variant_ids"] = ["minecraft:glass"]
    glass_state["mapping_status"] = "mapped"
    stone_variant = next(row for row in variants if row["variant_id"] == "minecraft:stone")
    glass_variant = copy.deepcopy(stone_variant)
    glass_variant["variant_id"] = "minecraft:glass"
    glass_variant["block_id"] = "minecraft:glass"
    glass_variant["canonical_state_id"] = "minecraft:glass"
    glass_variant["represented_state_ids"] = ["minecraft:glass"]
    glass_variant["machine_facts"]["behavior_by_state"] = {"minecraft:glass": glass_state["behavior"]}
    source_render = export / "renders" / "minecraft" / "stone"
    target_render = export / "renders" / "minecraft" / "glass"
    target_render.mkdir(parents=True)
    for name in ("preview.png", "mask.png"):
        (target_render / name).write_bytes((source_render / name).read_bytes())
    metadata = json.loads((source_render / "render.json").read_text(encoding="utf-8"))
    metadata["variant_id"] = "minecraft:glass"
    metadata_bytes = (fixture._jcs_canonical(metadata) + "\n").encode("utf-8")
    (target_render / "render.json").write_bytes(metadata_bytes)
    glass_variant["render"]["preview_path"] = "renders/minecraft/glass/preview.png"
    glass_variant["render"]["mask_path"] = "renders/minecraft/glass/mask.png"
    glass_variant["render"]["render_metadata_path"] = "renders/minecraft/glass/render.json"
    glass_variant["render"]["render_metadata_sha256"] = fixture._hash_bytes(fixture._jcs_canonical(metadata).encode("utf-8"))
    variants = [stone_variant, glass_variant]
    (export / "states.jsonl").write_bytes("".join(fixture._jcs_canonical(row) + "\n" for row in states).encode("utf-8"))
    (export / "variants.jsonl").write_bytes("".join(fixture._jcs_canonical(row) + "\n" for row in variants).encode("utf-8"))
    (export / "failures.jsonl").write_bytes(b"")
    manifest_path = export / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["counts"] = {"block_records": 2, "failure_records": 0, "pending_review_records": 0, "registry_blocks": 2, "selected_variant_records": 2, "skipped_variant_records": 0, "state_records": 2}
    manifest["status"] = "succeeded"
    manifest_path.write_bytes((fixture._jcs_canonical(manifest) + "\n").encode("utf-8"))
    files = sorted((item for item in export.rglob("*") if item.is_file() and item.name != "checksums.sha256"), key=lambda item: item.relative_to(export).as_posix().encode("utf-8"))
    (export / "checksums.sha256").write_bytes("".join(hashlib.sha256(item.read_bytes()).hexdigest() + "  " + item.relative_to(export).as_posix() + "\n" for item in files).encode("ascii"))
    return export
