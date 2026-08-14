from __future__ import annotations

import hashlib
import json
import struct
import zlib
from pathlib import Path

import pytest

from tools.validate_r1_export import _jcs_canonical


def _hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _png(width: int = 512, height: int = 512, *, alpha: int = 255) -> bytes:
    pixels = bytearray(width * height * 4)
    for y in range(96, height - 96):
        for x in range(96, width - 96):
            offset = (y * width + x) * 4
            pixels[offset : offset + 4] = bytes((80, 140, 200, alpha))
    rows = b"".join(b"\x00" + bytes(pixels[y * width * 4 : (y + 1) * width * 4]) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


def _source(export_id: str, block_id: str, *, selected: bool, failure_id: str | None = None) -> dict:
    base = {
        "export_id": export_id,
        "minecraft_version": "26.2",
        "block_id": block_id,
        "source": {"type": "runtime", "minecraft_version": "26.2", "export_id": export_id, "exporter_version": "fixture", "stage": "EXPORT_REGISTRY"},
    }
    if selected:
        base.update({"name_zh_cn": "石头", "name_en_us": "Stone", "translation_key": "block.minecraft.stone", "default_state_id": block_id, "properties": {}, "has_item": True, "has_block_entity": False, "tags": [], "behavior": {"transparent": False, "emissive": False, "passable": False, "waterloggable": False, "redstone_related": False, "requires_support": False, "support": {"above": False, "below": True, "east": False, "north": False, "south": False, "west": False, "none": False}, "emission_level": 0}})
    else:
        base.update({"name_zh_cn": "玻璃", "name_en_us": "Glass", "translation_key": "block.minecraft.glass", "default_state_id": block_id, "properties": {}, "has_item": True, "has_block_entity": False, "tags": [], "behavior": {"transparent": True, "emissive": False, "passable": False, "waterloggable": False, "redstone_related": False, "requires_support": False, "support": {"above": False, "below": True, "east": False, "north": False, "south": False, "west": False, "none": False}, "emission_level": 0}})
    return base


def make_export(root: Path) -> Path:
    export_id = "export_20260814T120000Z"
    export = root / "exports" / "26.2" / export_id
    render = export / "renders" / "minecraft" / "stone"
    render.mkdir(parents=True)
    selected_block = _source(export_id, "minecraft:stone", selected=True)
    skipped_block = _source(export_id, "minecraft:glass", selected=False)
    blocks = [{"schema_version": "export-block.v1", **selected_block}, {"schema_version": "export-block.v1", **skipped_block}]
    shape = {"boxes": [{"min_x": 0, "min_y": 0, "min_z": 0, "max_x": 1, "max_y": 1, "max_z": 1}], "signature": _hash_bytes(b"shape")}
    collision = {"boxes": shape["boxes"], "signature": _hash_bytes(b"collision")}
    states = []
    variants = []
    failures = []
    for block, selected in ((selected_block, True), (skipped_block, False)):
        block_id = block["block_id"]
        state = {"schema_version": "export-state.v1", "export_id": export_id, "minecraft_version": "26.2", "state_id": block_id, "block_id": block_id, "properties": {}, "is_default": True, "legal_state": True, "shape": shape, "collision": collision, "behavior": block["behavior"], "variant_ids": [block_id] if selected else [], "mapping_status": "mapped" if selected else "skipped", "source": block["source"]}
        states.append(state)
        if selected:
            image = _png()
            mask = _png()
            metadata = {"schema_version": "render-metadata.v1", "variant_id": block_id, "width": 512, "height": 512, "format": "PNG-RGBA", "views": ["isometric", "front", "side", "top"], "fixture_id": "fixture", "fixture_version": "fixture.v1", "tint_sensitive": False, "baseline_biome": None, "mask": {"present": True, "format": "PNG-RGBA", "channel": "alpha", "threshold": 1}}
            (render / "preview.png").write_bytes(image)
            (render / "mask.png").write_bytes(mask)
            (render / "render.json").write_bytes((_jcs_canonical(metadata) + "\n").encode("utf-8"))
            variants.append({"schema_version": "export-variant.v1", "export_id": export_id, "minecraft_version": "26.2", "variant_id": block_id, "block_id": block_id, "canonical_state_id": block_id, "represented_state_ids": [block_id], "context": {"fixture_id": "fixture", "fixture_version": "fixture.v1", "rotatable": True, "canonical_orientation": None, "adjacency": []}, "selection": {"state_policy_version": "state-policy.v1", "reason": "default", "protected_dimensions": [], "folded_state_ids": [], "policy_override_id": None}, "status": "selected", "candidate_qualification": "eligible", "warnings": [], "machine_facts": {"geometry_signature": shape["signature"], "collision_signature": collision["signature"], "shape": shape, "collision": collision, "behavior_fingerprint": _hash_bytes(b"behavior"), "behavior_by_state": {block_id: block["behavior"]}}, "render": {"render_policy_version": "render.v1", "preview_path": "renders/minecraft/stone/preview.png", "mask_path": "renders/minecraft/stone/mask.png", "render_metadata_path": "renders/minecraft/stone/render.json", "image_sha256": _hash_bytes(image), "mask_sha256": _hash_bytes(mask), "render_metadata_sha256": _hash_bytes(_jcs_canonical(metadata).encode())}, "source": {"type": "machine", "minecraft_version": "26.2", "export_id": export_id, "producer_version": "fixture", "stage": "RENDER_VARIANTS"}})
        else:
            failure_id = "fail_glass"
            variants.append({"schema_version": "export-variant.v1", "export_id": export_id, "minecraft_version": "26.2", "variant_id": block_id, "block_id": block_id, "status": "skipped", "candidate_qualification": "excluded", "warnings": [], "skip_reason_code": "MISSING_TEXTURE", "skip_reason": "fixture skip", "source": {"type": "machine", "minecraft_version": "26.2", "export_id": export_id, "producer_version": "fixture", "stage": "RENDER_VARIANTS"}})
            failures.append({"schema_version": "export-failure.v1", "export_id": export_id, "minecraft_version": "26.2", "failure_id": failure_id, "kind": "skip", "stage": "RENDER_VARIANTS", "scope": "variant", "block_id": block_id, "variant_id": block_id, "logical_key": block_id, "reason_code": "MISSING_TEXTURE", "severity": "high", "retry_count": 0, "action": "needs_review", "review_status": "pending", "message": "fixture skip", "evidence": {"kind": "none", "paths": [], "hashes": [], "frame_hashes": []}, "input_signature": _hash_bytes(b"fixture-input"), "created_at": "2026-08-14T12:00:00Z"})
    def write_jsonl(name: str, rows: list[dict]) -> None:
        (export / name).write_bytes("".join(_jcs_canonical(row) + "\n" for row in rows).encode("utf-8"))
    write_jsonl("blocks.jsonl", blocks)
    write_jsonl("states.jsonl", states)
    write_jsonl("variants.jsonl", variants)
    write_jsonl("failures.jsonl", failures)
    (export / "exporter.log").write_bytes(b"fixture\n")
    schema_ids = ("export-block.v1", "export-failure.v1", "export-manifest.v1", "export-state.v1", "export-variant.v1", "render-metadata.v1")
    inventory = []
    for schema_id in schema_ids:
        schema_path = Path(__file__).resolve().parents[2] / "schemas" / "exporter" / f"{schema_id}.json"
        inventory.append({"schema_id": schema_id, "schema_sha256": _hash_bytes(schema_path.read_bytes()), "repository_path": f"schemas/exporter/{schema_id}.json"})
    manifest = {"schema_version": "export-manifest.v1", "export_contract_version": "export-contract.v1", "export_id": export_id, "logical_input_signature": _hash_bytes(b"logical"), "render_input_signature": _hash_bytes(b"render"), "status": "needs_review", "created_at": "2026-08-14T12:00:00Z", "completed_at": "2026-08-14T12:00:00Z", "toolchain": {"minecraft_edition": "Java", "minecraft_version": "26.2", "java_version": "25", "fabric_loader_version": "0.19.3", "fabric_api_version": "0.157.0+26.2", "loom_version": "1.17.19", "gradle_version": "9.5.1", "mappings": "Minecraft 26.2 native Mojang names (unobfuscated); no external mappings artifact", "exporter_mod_id": "fixture", "exporter_version": "fixture"}, "runtime": {"resource_pack_id": "vanilla", "resource_pack_sha256": _hash_bytes(b"vanilla"), "language_primary": "zh_cn", "language_secondary": "en_us", "shader": "disabled", "world_fixture_version": "fixture.v1", "biome": "minecraft:plains", "weather": "clear", "world_time": 6000, "fov": 70, "gui_scale": 2, "render_distance": 8}, "platform": {"os_name": "Windows", "os_version": "fixture", "architecture": "x86_64", "gpu_vendor": "fixture", "gpu_model": "fixture", "driver_version": "fixture", "render_backend": "fixture", "framebuffer_resolution": "512x512", "render_environment_sha256": _hash_bytes(b"environment")}, "render_environment": {"camera_policy_version": "camera.v1", "camera_sha256": _hash_bytes(b"camera"), "lighting_policy_version": "lighting.v1", "lighting_sha256": _hash_bytes(b"lighting"), "background_sha256": _hash_bytes(b"background"), "backboard_sha256": _hash_bytes(b"backboard"), "support_fixture_sha256": _hash_bytes(b"support")}, "policies": {"state_policy_version": "state-policy.v1", "render_policy_version": "render.v1", "fixture_policy_version": "fixture.v1", "dedupe_policy_version": "dedupe.v1"}, "schema_inventory": inventory, "scope": {"namespace": "minecraft", "registry": "block", "registry_snapshot_sha256": _hash_bytes(b"minecraft:glass\nminecraft:stone")}, "counts": {"registry_blocks": 2, "block_records": 2, "state_records": 2, "selected_variant_records": 1, "skipped_variant_records": 1, "failure_records": 1, "pending_review_records": 1}, "files": {"blocks.jsonl": {"required": True, "kind": "jsonl", "record_schema": "export-block.v1"}, "states.jsonl": {"required": True, "kind": "jsonl", "record_schema": "export-state.v1"}, "variants.jsonl": {"required": True, "kind": "jsonl", "record_schema": "export-variant.v1"}, "failures.jsonl": {"required": True, "kind": "jsonl", "record_schema": "export-failure.v1"}, "checksums.sha256": {"required": True, "kind": "checksum"}, "exporter.log": {"required": True, "kind": "log"}, "renders/": {"required": True, "kind": "render_directory"}}, "integrity": {"algorithm": "SHA-256", "checksum_file": "checksums.sha256", "canonical_json": "JCS-RFC8785", "jsonl_record_terminator": "LF"}}
    (export / "manifest.json").write_bytes((_jcs_canonical(manifest) + "\n").encode("utf-8"))
    files = sorted(path.relative_to(export).as_posix() for path in export.rglob("*") if path.is_file() and path.name != "checksums.sha256")
    (export / "checksums.sha256").write_bytes("".join(hashlib.sha256((export / ref).read_bytes()).hexdigest() + "  " + ref + "\n" for ref in files).encode("utf-8"))
    return export


@pytest.fixture
def export_fixture(tmp_path: Path) -> Path:
    return make_export(tmp_path)


class PassingToolchainProbe:
    def check(self) -> dict[str, object]:
        return {
            "python_version": "3.14.7",
            "expected_python_version": "3.14.7",
            "config_ok": True,
            "lock_ok": True,
            "schema_ok": True,
            "schema_sha256": _hash_bytes(b"workspace-schema"),
            "passed": True,
        }
