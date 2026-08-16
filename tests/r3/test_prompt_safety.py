from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from blockpedia.r3 import safe_machine_metadata, safe_prompt


def _payload(prompt: str) -> tuple[str, dict[str, Any]]:
    start_marker = "<untrusted_data>\n"
    end_marker = "\n</untrusted_data>"
    start = prompt.index(start_marker) + len(start_marker)
    end = prompt.index(end_marker, start)
    raw = prompt[start:end]
    return raw, json.loads(raw)


def _tiles(count: int = 16) -> list[dict[str, str]]:
    return [
        {
            "tile_id": f"T{index:02d}",
            "variant_id": f"minecraft:block_{index:02d}",
            "image_sha256": "sha256:" + "a" * 64,
            "machine_metadata_sha256": "sha256:" + "b" * 64,
        }
        for index in range(1, count + 1)
    ]


def test_long_nested_metadata_stays_valid_json_and_bounded() -> None:
    tile_map = _tiles(1)
    metadata = [
        {
            "variant_id": tile_map[0]["variant_id"],
            "summary": "untrusted " * 10000,
            "nested": {"values": ["nested " * 1000 for _ in range(100)]},
        }
    ]

    raw, payload = _payload(safe_prompt(metadata, tile_map))

    assert len(raw) <= 12000
    assert payload["tiles"] == tile_map
    assert len(payload["metadata"]) == 1
    assert len(payload["metadata"][0]["summary"]) <= 512  # type: ignore[index]


def test_all_tile_mapping_is_complete_and_ordered_before_metadata() -> None:
    tile_map = _tiles()
    metadata = [{"variant_id": tile["variant_id"], "summary": "x" * 2000} for tile in reversed(tile_map)]

    raw, payload = _payload(safe_prompt(metadata, list(reversed(tile_map))))

    assert list(payload)[:2] == ["tiles", "metadata"]
    assert [(tile["tile_id"], tile["variant_id"]) for tile in payload["tiles"]] == [
        (tile["tile_id"], tile["variant_id"]) for tile in tile_map
    ]
    assert len(raw) <= 12000


def test_metadata_budget_reduction_does_not_remove_tile_identity() -> None:
    tile_map = _tiles()
    metadata = [
        {
            "variant_id": tile["variant_id"],
            "description": "z" * 10000,
            "values": ["value" * 1000] * 100,
        }
        for tile in tile_map
    ]

    raw, payload = _payload(safe_prompt(metadata, tile_map))

    assert len(raw) <= 12000
    assert len(payload["tiles"]) == 16
    assert {tile["variant_id"] for tile in payload["tiles"]} == {tile["variant_id"] for tile in tile_map}
    assert {tile["tile_id"] for tile in payload["tiles"]} == {tile["tile_id"] for tile in tile_map}


def test_prompt_and_machine_metadata_do_not_mutate_inputs_or_include_sensitive_fields() -> None:
    variant = {
        "variant_id": "minecraft:stone",
        "block_id": "minecraft:stone",
        "canonical_state_id": "minecraft:stone",
        "machine_facts": {
            "geometry": {"geometry_classes": ["full_cube"]},
            "machine_tags": ["shape:full_cube"],
            "behavior_by_state": {"minecraft:stone": {"transparent": False}},
        },
    }
    feature = {"feature_extractor_version": "fixture.v1", "machine_tags": ["shape:full_cube"]}
    tile_map = _tiles(1)
    metadata = [{"variant_id": "minecraft:stone", "api_key": "secret-value", "preview_path": "C:\\private\\preview.png"}]
    variant_before = copy.deepcopy(variant)
    feature_before = copy.deepcopy(feature)
    tile_map_before = copy.deepcopy(tile_map)
    metadata_before = copy.deepcopy(metadata)

    safe_machine_metadata(variant, feature)
    prompt = safe_prompt(metadata, tile_map)

    assert variant == variant_before
    assert feature == feature_before
    assert tile_map == tile_map_before
    assert metadata == metadata_before
    assert "secret-value" not in prompt
    assert "C:\\private\\preview.png" not in prompt


def test_prompt_versions_preserve_legacy_bytes_and_v2_projection_contract() -> None:
    tile_map = [
        {
            "tile_id": "T01",
            "variant_id": "minecraft:stone",
            "image_sha256": "sha256:" + "a" * 64,
            "machine_metadata_sha256": "sha256:" + "b" * 64,
        },
        {
            "tile_id": "T02",
            "variant_id": "minecraft:glass",
            "image_sha256": "sha256:" + "c" * 64,
            "machine_metadata_sha256": "sha256:" + "d" * 64,
        },
    ]
    metadata = [
        {"variant_id": "minecraft:stone", "summary": "Stone fixture"},
        {"variant_id": "minecraft:glass", "summary": "Glass fixture"},
    ]
    legacy = safe_prompt(metadata, tile_map)
    assert safe_prompt(metadata, tile_map, prompt_version="prompt.v1") == legacy
    assert safe_prompt(metadata, tile_map, prompt_version="prompt.legacy") == legacy
    assert hashlib.sha256(legacy.encode("utf-8")).hexdigest() == "deba21d606606ecbd6c5a655def75a8087b6b5f3c0ec2fd73758ee7d5080d53d"

    v2_metadata = [
        {
            "variant_id": "minecraft:stone",
            "block_id": "minecraft:stone",
            "canonical_state_id": "minecraft:stone",
            "image_sha256": "sha256:" + "a" * 64,
            "machine_metadata_sha256": "sha256:" + "b" * 64,
            "geometry": {"width": 1.0, "geometry_classes": ["full_cube", "full_cube"]},
            "behavior": {"emissive": False, "emission_level": 0},
            "machine_tags": ["shape:full_cube"],
            "feature": {"geometry_classes": ["full_cube", "cube"], "input_sha256": "sha256:" + "e" * 64},
        },
        {
            "variant_id": "minecraft:glass",
            "geometry": {"geometry_classes": ["full_cube"]},
            "feature": {"geometry_classes": ["full_cube", "transparent"], "brightness": 0.5},
        },
    ]
    v2_raw, v2 = _payload(safe_prompt(v2_metadata, list(reversed(tile_map)), prompt_version="prompt.v2"))
    assert set(v2) == {"tiles", "tile_metadata"}
    assert all(set(item) == {"tile_id", "variant_id"} for item in v2["tiles"])
    assert all(set(item) == {"tile_id", "geometry_classes"} for item in v2["tile_metadata"])
    assert [(item["tile_id"], item["variant_id"]) for item in v2["tiles"]] == [("T01", "minecraft:stone"), ("T02", "minecraft:glass")]
    assert [item["tile_id"] for item in v2["tile_metadata"]] == ["T01", "T02"]
    assert v2["tile_metadata"][0]["geometry_classes"] == ["full_cube", "cube"]
    assert v2["tile_metadata"][1]["geometry_classes"] == ["full_cube", "transparent"]
    assert len(v2_raw) <= 12000
    assert v2_raw != _payload(legacy)[0]
    assert all(
        field not in safe_prompt(v2_metadata, tile_map, prompt_version="prompt.v2")
        for field in (
            "image_sha256",
            "machine_metadata_sha256",
            "block_id",
            "canonical_state_id",
            "machine_tags",
            "feature_extractor_version",
            "input_sha256",
            "emission_level",
        )
    )
