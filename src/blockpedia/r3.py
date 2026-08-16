"""Small, dependency-free helpers for the R3 annotation/review lane.

This module deliberately contains no persistence and no HTTP client.  It owns
only deterministic values which are shared by the application service and the
in-process worker: canonical hashes, bounded prompt data and the in-memory
contact sheet used by the provider request.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
import zlib
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .features import DecodedPng, decode_rgba_png


_PROMPT_DATA_BUDGET = 12000
_PROMPT_MAX_STRING = 512
_PROMPT_MAX_LIST_ITEMS = 64
_PROMPT_MAX_MAPPING_ITEMS = 64
_PROMPT_MAX_DEPTH = 4
_PROMPT_MAX_METADATA_ITEMS = 256
_IDENTITY_KEYS = frozenset({"tile_id", "variant_id", "block_id", "canonical_state_id"})
_OMIT = object()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def short_tile_id(index: int) -> str:
    if index < 0:
        raise ValueError("tile index must be non-negative")
    return f"T{index + 1:02d}"


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_rgba_png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode an RGBA image using only the PNG format primitives in stdlib."""

    if width <= 0 or height <= 0 or len(pixels) != width * height * 4:
        raise ValueError("invalid RGBA image")
    rows = b"".join(b"\x00" + pixels[row * width * 4 : (row + 1) * width * 4] for row in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", header) + _chunk(b"IDAT", zlib.compress(rows, 9)) + _chunk(b"IEND", b"")


def _resize_nearest(image: DecodedPng, width: int = 512, height: int = 512) -> bytes:
    if image.width == width and image.height == height:
        return image.pixels
    result = bytearray(width * height * 4)
    for y in range(height):
        source_y = min(image.height - 1, y * image.height // height)
        for x in range(width):
            source_x = min(image.width - 1, x * image.width // width)
            source = (source_y * image.width + source_x) * 4
            target = (y * width + x) * 4
            result[target : target + 4] = image.pixels[source : source + 4]
    return bytes(result)


_GLYPHS: dict[str, tuple[str, ...]] = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "111", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "001", "001"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    "T": ("111", "010", "010", "010", "010"),
}


def _paint_label(pixels: bytearray, width: int, height: int, x: int, y: int, label: str) -> None:
    scale = 5
    glyph_width = sum(len(_GLYPHS[char][0]) + 1 for char in label) * scale
    glyph_height = 5 * scale
    x = min(max(0, x), max(0, width - glyph_width - 16))
    y = max(0, y)
    # A tiny opaque backing keeps the identifier visible without a font or
    # external asset.  It is part of the deterministic contact-sheet image.
    for row in range(glyph_height + 12):
        for column in range(glyph_width + 12):
            px, py = x + column, y + row
            if 0 <= px < width and 0 <= py < height:
                offset = (py * width + px) * 4
                pixels[offset : offset + 4] = b"\x18\x18\x18\xff"
    cursor = x + 6
    for char in label:
        glyph = _GLYPHS.get(char, _GLYPHS["0"])
        for gy, glyph_row in enumerate(glyph):
            for gx, bit in enumerate(glyph_row):
                if bit != "1":
                    continue
                for dy in range(scale):
                    for dx in range(scale):
                        px, py = cursor + gx * scale + dx, y + 6 + gy * scale + dy
                        if 0 <= px < width and 0 <= py < height:
                            offset = (py * width + px) * 4
                            pixels[offset : offset + 4] = b"\xff\xff\xff\xff"
        cursor += (len(glyph[0]) + 1) * scale


@dataclass(frozen=True, slots=True)
class ContactSheet:
    image_png: bytes
    tiles: tuple[dict[str, Any], ...]

    @property
    def image_sha256(self) -> str:
        return sha256_bytes(self.image_png)


def make_contact_sheet(images: Sequence[tuple[str, bytes]], *, columns: int = 4) -> ContactSheet:
    """Make a stable 512px-card grid and label every card with a short ID."""

    if not 1 <= len(images) <= 16:
        raise ValueError("contact sheets contain 1-16 images")
    columns = max(1, min(columns, len(images)))
    rows = (len(images) + columns - 1) // columns
    width, height = columns * 512, rows * 512
    pixels = bytearray(width * height * 4)
    tiles: list[dict[str, Any]] = []
    for index, (variant_id, image_bytes) in enumerate(images):
        decoded = decode_rgba_png(image_bytes)
        card = _resize_nearest(decoded)
        row, column = divmod(index, columns)
        x0, y0 = column * 512, row * 512
        for y in range(512):
            target = ((y0 + y) * width + x0) * 4
            source = y * 512 * 4
            pixels[target : target + 512 * 4] = card[source : source + 512 * 4]
        tile_id = short_tile_id(index)
        _paint_label(pixels, width, height, x0 + 12, y0 + 470, tile_id)
        tiles.append({"tile_id": tile_id, "variant_id": variant_id, "row": row, "column": column})
    return ContactSheet(encode_rgba_png(width, height, bytes(pixels)), tuple(tiles))


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    sensitive_parts = (
        "api_key",
        "authorization",
        "bearer",
        "credential",
        "directory",
        "file_path",
        "filepath",
        "filename",
        "password",
        "private_key",
        "secret",
        "token",
        "url",
    )
    if normalized in sensitive_parts or normalized.endswith("_path") or normalized.endswith("_ref"):
        return True
    return any(normalized.startswith(part + "_") for part in sensitive_parts)


def _bounded_value(
    value: Any,
    *,
    key: str | None = None,
    string_limit: int = _PROMPT_MAX_STRING,
    list_limit: int = _PROMPT_MAX_LIST_ITEMS,
    mapping_limit: int = _PROMPT_MAX_MAPPING_ITEMS,
    depth_limit: int = _PROMPT_MAX_DEPTH,
    depth: int = 0,
) -> Any:
    """Copy a JSON-like value into a deterministic, bounded prompt-safe value."""

    if key is not None and _is_sensitive_key(key):
        return _OMIT
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        # Keep valid machine identifiers intact, but never preserve an obvious
        # secret or path even when it arrived under an identity-looking key.
        if is_sensitive_review_text(value):
            return _OMIT
        if key in _IDENTITY_KEYS:
            return value
        return value[:string_limit]
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if depth >= depth_limit:
        return _OMIT
    if isinstance(value, Mapping):
        entries: list[tuple[str, Any]] = []
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                continue
            child_key = raw_key[:128]
            if _is_sensitive_key(child_key):
                continue
            bounded = _bounded_value(
                raw_value,
                key=child_key,
                string_limit=string_limit,
                list_limit=list_limit,
                mapping_limit=mapping_limit,
                depth_limit=depth_limit,
                depth=depth + 1,
            )
            if bounded is not _OMIT:
                entries.append((child_key, bounded))
        entries.sort(key=lambda item: (0 if item[0] in _IDENTITY_KEYS else 1, item[0].encode("utf-8")))
        mapping_result: dict[str, Any] = {}
        for child_key, bounded in entries[:mapping_limit]:
            mapping_result[child_key] = bounded
        return mapping_result
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        for item in value[:list_limit]:
            bounded = _bounded_value(
                item,
                string_limit=string_limit,
                list_limit=list_limit,
                mapping_limit=mapping_limit,
                depth_limit=depth_limit,
                depth=depth + 1,
            )
            if bounded is not _OMIT:
                result.append(bounded)
        return result
    return _OMIT


def _safe_metadata_item(value: Any, **limits: Any) -> Any:
    bounded = _bounded_value(value, **limits)
    if bounded is _OMIT:
        return {}
    return bounded


def _safe_tile_map(tile_map: Sequence[Mapping[str, Any]]) -> list[Any]:
    indexed: list[tuple[tuple[bytes, bytes, int], Any]] = []
    for index, tile in enumerate(tile_map):
        bounded = _safe_metadata_item(tile)
        if isinstance(bounded, Mapping):
            tile_id = bounded.get("tile_id", "")
            variant_id = bounded.get("variant_id", "")
            sort_key = (str(tile_id).encode("utf-8"), str(variant_id).encode("utf-8"), index)
        else:
            sort_key = (b"", b"", index)
        indexed.append((sort_key, bounded))
    indexed.sort(key=lambda item: item[0])
    return [item[1] for item in indexed]


def _metadata_candidates(value: Any) -> list[Any]:
    candidates: list[Any] = []
    for limits in (
        {},
        {"string_limit": 256, "list_limit": 32, "mapping_limit": 32, "depth_limit": 3},
        {"string_limit": 128, "list_limit": 16, "mapping_limit": 16, "depth_limit": 2},
        {"string_limit": 64, "list_limit": 8, "mapping_limit": 8, "depth_limit": 2},
    ):
        candidate = _safe_metadata_item(value, **limits)
        if candidate not in candidates:
            candidates.append(candidate)
    if isinstance(value, Mapping):
        identity_only: dict[str, Any] = {}
        for key in sorted(_IDENTITY_KEYS, key=lambda item: item.encode("utf-8")):
            if key not in value:
                continue
            bounded = _bounded_value(value[key], key=key)
            if bounded is not _OMIT:
                identity_only[key] = bounded
        if identity_only not in candidates:
            candidates.append(identity_only)
    return candidates


def _prompt_payload(tiles: Sequence[Any], metadata: Sequence[Any]) -> str:
    # Do not use canonical_json here: it sorts the top-level keys and would put
    # metadata before tiles.  The explicit order protects the mapping from any
    # later metadata budget reduction.
    tiles_json = json.dumps(tiles, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    return '{"tiles":' + tiles_json + ',"metadata":' + metadata_json + "}"


def _prompt_payload_v2(tiles: Sequence[Any], tile_metadata: Sequence[Any]) -> str:
    """Serialize the exact prompt.v2 top-level projection."""

    tiles_json = json.dumps(tiles, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    metadata_json = json.dumps(tile_metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c")
    return '{"tiles":' + tiles_json + ',"tile_metadata":' + metadata_json + "}"


def _safe_v2_geometry_classes(value: Any) -> list[str]:
    """Project only the bounded, repeat-free geometry labels for prompt.v2."""

    classes: list[str] = []
    seen: set[str] = set()
    if not isinstance(value, Mapping):
        return classes
    for source_key in ("geometry", "feature"):
        source = value.get(source_key)
        if not isinstance(source, Mapping):
            continue
        raw_classes = source.get("geometry_classes")
        if not isinstance(raw_classes, (list, tuple)):
            continue
        for raw_class in raw_classes[:_PROMPT_MAX_LIST_ITEMS]:
            if not isinstance(raw_class, str) or is_sensitive_review_text(raw_class):
                continue
            safe_class = raw_class[:128]
            if safe_class and safe_class not in seen:
                seen.add(safe_class)
                classes.append(safe_class)
            if len(classes) >= _PROMPT_MAX_LIST_ITEMS:
                return classes
    return classes


def _v2_prompt_projection(
    variant_metadata: Sequence[Mapping[str, Any]],
    tiles: Sequence[Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build the exact model-visible prompt.v2 object projection."""

    projected_tiles: list[dict[str, Any]] = []
    for tile in tiles:
        if not isinstance(tile, Mapping):
            continue
        projected_tiles.append({"tile_id": tile.get("tile_id"), "variant_id": tile.get("variant_id")})

    by_variant: dict[str, Mapping[str, Any]] = {}
    ordered_metadata = list(variant_metadata)
    for item in ordered_metadata:
        if not isinstance(item, Mapping):
            continue
        variant_id = item.get("variant_id")
        if isinstance(variant_id, str) and variant_id not in by_variant:
            by_variant[variant_id] = item

    projected_metadata: list[dict[str, Any]] = []
    for index, tile in enumerate(projected_tiles):
        variant_id = tile.get("variant_id")
        source = by_variant.get(variant_id) if isinstance(variant_id, str) else None
        if source is None and index < len(ordered_metadata) and isinstance(ordered_metadata[index], Mapping):
            source = ordered_metadata[index]
        projected_metadata.append(
            {
                "tile_id": tile.get("tile_id"),
                "geometry_classes": _safe_v2_geometry_classes(source),
            }
        )
    return projected_tiles, projected_metadata


def _safe_prompt_v2(variant_metadata: Sequence[Mapping[str, Any]], tiles: Sequence[Any]) -> str:
    projected_tiles, projected_metadata = _v2_prompt_projection(variant_metadata, tiles)
    accepted_metadata: list[dict[str, Any]] = []
    for item in projected_metadata:
        candidates = [
            item,
            {"tile_id": item["tile_id"], "geometry_classes": item["geometry_classes"][:16]},
            {"tile_id": item["tile_id"], "geometry_classes": item["geometry_classes"][:8]},
            {"tile_id": item["tile_id"], "geometry_classes": []},
        ]
        accepted = candidates[-1]
        for candidate in candidates:
            if len(_prompt_payload_v2(projected_tiles, accepted_metadata + [candidate])) <= _PROMPT_DATA_BUDGET:
                accepted = candidate
                break
        accepted_metadata.append(accepted)
    payload = _prompt_payload_v2(projected_tiles, accepted_metadata)
    return (
        "Annotate only the existing labeled tiles. For every tile, copy its exact existing\n"
        "variant_id. Never create or modify IDs or machine facts.\n"
        "<untrusted_data>\n"
        + payload
        + "\n</untrusted_data>"
    )


def safe_machine_metadata(variant: Mapping[str, Any], feature: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only bounded, non-path machine facts for a provider envelope."""

    raw_facts = variant.get("machine_facts")
    facts: Mapping[str, Any] = raw_facts if isinstance(raw_facts, Mapping) else {}
    raw_geometry = facts.get("geometry")
    geometry: Mapping[str, Any] = raw_geometry if isinstance(raw_geometry, Mapping) else {}
    raw_behavior_by_state = facts.get("behavior_by_state")
    behavior_by_state: Mapping[str, Any] = raw_behavior_by_state if isinstance(raw_behavior_by_state, Mapping) else {}
    state_id = variant.get("canonical_state_id")
    behavior = behavior_by_state.get(state_id, {}) if isinstance(state_id, str) else {}
    if not isinstance(behavior, Mapping):
        behavior = {}
    raw_tags = facts.get("machine_tags")
    tags = raw_tags if isinstance(raw_tags, list) else []
    result: dict[str, Any] = {
        "variant_id": _safe_metadata_item(variant.get("variant_id")),
        "block_id": _safe_metadata_item(variant.get("block_id")),
        "canonical_state_id": _safe_metadata_item(state_id),
        "geometry": _safe_metadata_item({
            key: geometry.get(key)
            for key in ("width", "height", "depth", "occupied_volume", "is_full_cube", "is_horizontal_sheet", "geometry_classes")
            if key in geometry
        }),
        "machine_tags": _safe_metadata_item(tags),
        "behavior": _safe_metadata_item({
            key: behavior.get(key)
            for key in ("transparent", "emissive", "emission_level", "passable", "waterloggable", "requires_support", "redstone_related")
            if key in behavior
        }),
    }
    if isinstance(feature, Mapping):
        result["feature"] = _safe_metadata_item({
            key: feature[key]
            for key in ("feature_extractor_version", "input_sha256", "mask_coverage", "transparent_ratio", "brightness", "saturation", "geometry_classes", "machine_tags")
            if key in feature
        })
    return result


def safe_prompt(
    variant_metadata: Sequence[Mapping[str, Any]],
    tile_map: Sequence[Mapping[str, Any]],
    prompt_version: str | None = None,
) -> str:
    """Build a short prompt with untrusted values in separate data sections."""

    tiles = _safe_tile_map(tile_map)
    if prompt_version == "prompt.v2":
        return _safe_prompt_v2(variant_metadata, tiles)
    metadata = [_safe_metadata_item(item) for item in list(variant_metadata)[:_PROMPT_MAX_METADATA_ITEMS]]
    accepted_metadata: list[Any] = []
    for item in metadata:
        for candidate in _metadata_candidates(item):
            trial = accepted_metadata + [candidate]
            if len(_prompt_payload(tiles, trial)) <= _PROMPT_DATA_BUDGET:
                accepted_metadata.append(candidate)
                break
    # The tile map is never sliced or dropped.  If an invalid input makes the
    # mapping itself larger than the normal budget, it still wins over optional
    # metadata so every tile/variant identity remains available and parseable.
    payload = _prompt_payload(tiles, accepted_metadata)
    return (
        "Annotate only the existing tile identifiers. Return one semantic item "
        "per tile using the supplied strict schema. Never create IDs, states, "
        "machine facts, paths, qualifications, or release facts.\n"
        "<untrusted_data>\n"
        + payload
        + "\n</untrusted_data>"
    )


def is_sensitive_review_text(value: str) -> bool:
    return bool(
        re.search(r"(?i)(api[_-]?key|authorization|bearer\s|secret|password|token\s*=|usage\s*=|cost\s*=|budget\s*=)", value)
        or re.search(r"(?:^[A-Za-z]:[\\/]|^/|\\\\)", value)
    )


__all__ = [
    "ContactSheet",
    "canonical_json",
    "encode_rgba_png",
    "is_sensitive_review_text",
    "make_contact_sheet",
    "safe_machine_metadata",
    "safe_prompt",
    "sha256_bytes",
    "sha256_json",
    "short_tile_id",
]
