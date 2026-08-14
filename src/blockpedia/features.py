"""Small deterministic RGBA PNG decoder and offline visual feature extractor."""

from __future__ import annotations

import hashlib
import json
import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

FEATURE_EXTRACTOR_VERSION = "features.v1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngDecodeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DecodedPng:
    width: int
    height: int
    pixels: bytes


@dataclass(frozen=True, slots=True)
class AxisAlignedUnion:
    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float
    width: float
    height: float
    depth: float
    occupied_volume: float
    is_full_cube: bool


def axis_aligned_union(boxes: Iterable[Mapping[str, Any]]) -> AxisAlignedUnion:
    """Compute exact union volume using deterministic coordinate cells."""

    normalized = []
    for box in boxes:
        values = tuple(float(box[key]) for key in ("min_x", "min_y", "min_z", "max_x", "max_y", "max_z"))
        if values[0] < values[3] and values[1] < values[4] and values[2] < values[5]:
            normalized.append(values)
    if not normalized:
        return AxisAlignedUnion(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, False)
    xs = sorted({value for box in normalized for value in (box[0], box[3])})
    ys = sorted({value for box in normalized for value in (box[1], box[4])})
    zs = sorted({value for box in normalized for value in (box[2], box[5])})
    volume = 0.0
    for x0, x1 in zip(xs, xs[1:]):
        for y0, y1 in zip(ys, ys[1:]):
            for z0, z1 in zip(zs, zs[1:]):
                cx, cy, cz = (x0 + x1) / 2, (y0 + y1) / 2, (z0 + z1) / 2
                if any(box[0] <= cx < box[3] and box[1] <= cy < box[4] and box[2] <= cz < box[5] for box in normalized):
                    volume += (x1 - x0) * (y1 - y0) * (z1 - z0)
    minimum = tuple(min(box[index] for box in normalized) for index in (0, 1, 2))
    maximum = tuple(max(box[index] for box in normalized) for index in (3, 4, 5))
    width, height, depth = maximum[0] - minimum[0], maximum[1] - minimum[1], maximum[2] - minimum[2]
    full = (
        abs(minimum[0]) <= 1e-12 and abs(minimum[1]) <= 1e-12 and abs(minimum[2]) <= 1e-12
        and abs(maximum[0] - 1) <= 1e-12 and abs(maximum[1] - 1) <= 1e-12 and abs(maximum[2] - 1) <= 1e-12
        and abs(volume - 1.0) <= 1e-12
    )
    return AxisAlignedUnion(
        minimum[0], minimum[1], minimum[2], maximum[0], maximum[1], maximum[2],
        width, height, depth, round(volume, 12), full,
    )


def decode_rgba_png(source: bytes | bytearray | memoryview | Path | str) -> DecodedPng:
    raw = Path(source).read_bytes() if isinstance(source, (Path, str)) else bytes(source)
    if not raw.startswith(PNG_SIGNATURE):
        raise PngDecodeError("PNG signature invalid")
    offset = 8
    header: tuple[int, int, int, int, int, int, int] | None = None
    idat: list[bytes] = []
    saw_iend = False
    while offset + 12 <= len(raw):
        length = int.from_bytes(raw[offset : offset + 4], "big")
        start = offset + 8
        end = start + length
        if end + 4 > len(raw):
            raise PngDecodeError("PNG chunk truncated")
        kind = raw[offset + 4 : offset + 8]
        data = raw[start:end]
        crc = int.from_bytes(raw[end : end + 4], "big")
        if zlib.crc32(kind + data) & 0xFFFFFFFF != crc:
            raise PngDecodeError("PNG CRC mismatch")
        if kind == b"IHDR":
            if len(data) != 13:
                raise PngDecodeError("PNG IHDR invalid")
            width, height, depth, color_type, compression, filter_method, interlace = struct.unpack(
                ">IIBBBBB", data
            )
            header = (width, height, depth, color_type, compression, filter_method, interlace)
        elif kind == b"IDAT":
            idat.append(data)
        elif kind == b"IEND":
            saw_iend = True
            break
        offset = end + 4
    if header is None or not idat or not saw_iend:
        raise PngDecodeError("PNG missing required chunks")
    width, height, depth, color_type, compression, filter_method, interlace = header
    if width <= 0 or height <= 0 or (depth, color_type, compression, filter_method, interlace) != (8, 6, 0, 0, 0):
        raise PngDecodeError("only non-interlaced 8-bit RGBA PNG is supported")
    try:
        decoded = zlib.decompress(b"".join(idat))
    except zlib.error as exc:
        raise PngDecodeError("PNG image data invalid") from exc
    row_bytes = width * 4
    if len(decoded) != height * (row_bytes + 1):
        raise PngDecodeError("PNG scanline length mismatch")
    previous = bytearray(row_bytes)
    pixels = bytearray(width * height * 4)
    cursor = 0
    output = 0
    for _ in range(height):
        filter_type = decoded[cursor]
        cursor += 1
        row = _unfilter(decoded[cursor : cursor + row_bytes], previous, filter_type, 4)
        cursor += row_bytes
        pixels[output : output + row_bytes] = row
        output += row_bytes
        previous = row
    return DecodedPng(width, height, bytes(pixels))


def _unfilter(filtered: bytes, previous: bytearray, filter_type: int, bpp: int) -> bytearray:
    row = bytearray(filtered)
    if filter_type == 0:
        return row
    if filter_type not in {1, 2, 3, 4}:
        raise PngDecodeError(f"unsupported PNG filter {filter_type}")
    for index in range(len(row)):
        left = row[index - bpp] if index >= bpp else 0
        above = previous[index]
        upper_left = previous[index - bpp] if index >= bpp else 0
        if filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = above
        elif filter_type == 3:
            predictor = (left + above) // 2
        else:
            estimate = left + above - upper_left
            distances = (abs(estimate - left), abs(estimate - above), abs(estimate - upper_left))
            predictor = (left, above, upper_left)[distances.index(min(distances))]
        row[index] = (row[index] + predictor) & 0xFF
    return row


def _linear(channel: float) -> float:
    value = channel / 255.0
    return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4


def _oklab_and_lab(red: float, green: float, blue: float) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    r, g, b = _linear(red), _linear(green), _linear(blue)
    l = 0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b
    m = 0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b
    s = 0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b
    l_root, m_root, s_root = math.copysign(abs(l) ** (1 / 3), l), math.copysign(abs(m) ** (1 / 3), m), math.copysign(abs(s) ** (1 / 3), s)
    oklab = (
        0.2104542553 * l_root + 0.7936177850 * m_root - 0.0040720468 * s_root,
        1.9779984951 * l_root - 2.4285922050 * m_root + 0.4505937099 * s_root,
        0.0259040371 * l_root + 0.7827717662 * m_root - 0.8086757660 * s_root,
    )
    x = 0.4124564 * r + 0.3575761 * g + 0.1804375 * b
    y = 0.2126729 * r + 0.7151522 * g + 0.0721750 * b
    z = 0.0193339 * r + 0.1191920 * g + 0.9503041 * b
    white = (0.95047, 1.0, 1.08883)
    def lab_f(value: float) -> float:
        delta = 6 / 29
        return value ** (1 / 3) if value > delta**3 else value / (3 * delta**2) + 4 / 29
    fx, fy, fz = lab_f(x / white[0]), lab_f(y / white[1]), lab_f(z / white[2])
    lab = (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))
    return (
        (round(oklab[0], 8), round(oklab[1], 8), round(oklab[2], 8)),
        (round(lab[0], 8), round(lab[1], 8), round(lab[2], 8)),
    )


def _normalized_hash(parts: Iterable[bytes | str | Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            payload = part
        elif isinstance(part, str):
            payload = part.encode("utf-8")
        else:
            payload = json.dumps(part, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return "sha256:" + digest.hexdigest()


def extract_features(
    preview: bytes | Path | str,
    mask: bytes | Path | str,
    *,
    geometry: Mapping[str, Any] | None = None,
    machine_tags: Iterable[str] = (),
) -> dict[str, Any]:
    """Extract only deterministic offline facts from an exporter image pair."""

    preview_raw = Path(preview).read_bytes() if isinstance(preview, (Path, str)) else bytes(preview)
    mask_raw = Path(mask).read_bytes() if isinstance(mask, (Path, str)) else bytes(mask)
    image, mask_image = decode_rgba_png(preview_raw), decode_rgba_png(mask_raw)
    if (image.width, image.height) != (mask_image.width, mask_image.height):
        raise PngDecodeError("preview and mask dimensions differ")
    total = image.width * image.height
    object_pixels = 0
    transparent_pixels = 0
    sums = [0.0, 0.0, 0.0]
    luminance_sum = 0.0
    saturation_sum = 0.0
    oklab_sum = [0.0, 0.0, 0.0]
    lab_sum = [0.0, 0.0, 0.0]
    object_coords: list[tuple[int, int]] = []
    for index in range(total):
        red, green, blue, alpha = image.pixels[index * 4 : index * 4 + 4]
        mask_alpha = mask_image.pixels[index * 4 + 3]
        if alpha == 0:
            transparent_pixels += 1
        if mask_alpha == 0:
            continue
        object_pixels += 1
        x, y = index % image.width, index // image.width
        object_coords.append((x, y))
        sums[0] += red
        sums[1] += green
        sums[2] += blue
        luminance_sum += (0.2126 * red + 0.7152 * green + 0.0722 * blue) / 255.0
        maximum, minimum = max(red, green, blue), min(red, green, blue)
        saturation_sum += 0.0 if maximum == 0 else (maximum - minimum) / maximum
        oklab, lab = _oklab_and_lab(red, green, blue)
        for channel in range(3):
            oklab_sum[channel] += oklab[channel]
            lab_sum[channel] += lab[channel]
    if object_pixels:
        divisor = float(object_pixels)
        average_rgb = tuple(round(value / divisor / 255.0, 8) for value in sums)
        oklab = tuple(round(value / divisor, 8) for value in oklab_sum)
        lab = tuple(round(value / divisor, 8) for value in lab_sum)
        brightness = round(luminance_sum / divisor, 8)
        saturation = round(saturation_sum / divisor, 8)
    else:
        average_rgb = (0.0, 0.0, 0.0)
        oklab = (0.0, 0.0, 0.0)
        lab = (0.0, 0.0, 0.0)
        brightness = saturation = 0.0

    mask_set = set(object_coords)
    edge_count = 0
    horizontal = vertical = 0
    for x, y in object_coords:
        if (x + 1, y) not in mask_set:
            edge_count += 1
            horizontal += 1
        if (x, y + 1) not in mask_set:
            edge_count += 1
            vertical += 1
    edge_density = round(edge_count / max(1, object_pixels * 2), 8)
    directionality = round(abs(horizontal - vertical) / max(1, horizontal + vertical), 8)
    geometry_classes = _geometry_classes(geometry or {})
    tags = sorted(set(str(tag) for tag in machine_tags) | set(geometry_classes), key=lambda value: value.encode("utf-8"))
    input_sha256 = _normalized_hash([preview_raw, mask_raw, geometry or {}])
    return {
        "input_sha256": input_sha256,
        "feature_extractor_version": FEATURE_EXTRACTOR_VERSION,
        "mask_coverage": round(object_pixels / max(1, total), 8),
        "transparent_ratio": round(transparent_pixels / max(1, total), 8),
        "average_rgb": average_rgb,
        "oklab": oklab,
        "lab": lab,
        "brightness": brightness,
        "saturation": saturation,
        "edge_density": edge_density,
        "directionality": directionality,
        "geometry_classes": geometry_classes,
        "machine_tags": tags,
    }


def _geometry_classes(geometry: Mapping[str, Any]) -> list[str]:
    classes: set[str] = set()
    if geometry.get("is_full_cube") is True and geometry.get("_union_proven") is True:
        classes.add("full_cube")
    if geometry.get("is_horizontal_sheet") is True:
        classes.add("horizontal_sheet")
    height = geometry.get("height")
    if isinstance(height, (int, float)):
        if height <= 0.125:
            classes.add("thin")
        elif height < 1:
            classes.add("partial_height")
    return sorted(classes)


def build_visual_variant_record(source: Mapping[str, Any], features: Mapping[str, Any]) -> dict[str, Any]:
    """Project a selected exporter variant after feature extraction."""

    source_machine = source["machine_facts"]
    shape = source_machine["shape"]
    collision = source_machine["collision"]
    geometry = _geometry_summary(shape, collision, features)
    source_info = source["source"]
    machine_tags = [tag for tag in features["machine_tags"] if tag != "full_cube" or geometry["is_full_cube"]]
    record = {
        "schema_version": "visual-variant-record.v1",
        "export_id": source["export_id"],
        "minecraft_version": source["minecraft_version"],
        "variant_id": source["variant_id"],
        "block_id": source["block_id"],
        "canonical_state_id": source["canonical_state_id"],
        "represented_state_ids": list(source["represented_state_ids"]),
        "context": {
            "fixture_id": source["context"]["fixture_id"],
            "fixture_version": source["context"]["fixture_version"],
            "rotatable": source["context"]["rotatable"],
            "canonical_orientation": source["context"]["canonical_orientation"],
        },
        "selection": source["selection"],
        "machine_facts": {
            "geometry": geometry,
            "behavior_fingerprint": source_machine["behavior_fingerprint"],
            "behavior_by_state": source_machine["behavior_by_state"],
            "machine_tags": machine_tags,
        },
        "render": source["render"],
        "annotation_refs": [],
        "override_refs": [],
        "qualification_review_refs": [],
        "candidate_qualification": source["candidate_qualification"],
        "warnings": list(source["warnings"]),
        "source": {
            "type": "machine",
            "minecraft_version": source_info["minecraft_version"],
            "export_id": source_info["export_id"],
            "producer_version": source_info["producer_version"],
            "stage": "EXTRACT_FEATURES",
        },
    }
    return record


def _geometry_summary(shape: Mapping[str, Any], collision: Mapping[str, Any], features: Mapping[str, Any]) -> dict[str, Any]:
    boxes = shape.get("boxes", [])
    union = axis_aligned_union(boxes)
    feature_classes = set(features["geometry_classes"])
    feature_classes.discard("full_cube")
    geometry_classes = sorted(feature_classes | set(_geometry_classes({"is_full_cube": union.is_full_cube, "_union_proven": True, "height": union.height})))
    return {
        "geometry_signature": shape["signature"],
        "collision_signature": collision["signature"],
        "shape": shape,
        "collision": collision,
        "width": union.width,
        "height": union.height,
        "depth": union.depth,
        "occupied_volume": union.occupied_volume,
        "is_full_cube": union.is_full_cube,
        "is_horizontal_sheet": union.height > 0 and union.height <= 0.125 and union.width >= 0.75 and union.depth >= 0.75,
        "geometry_classes": geometry_classes,
        "feature_extractor_version": features["feature_extractor_version"],
        "feature_input_sha256": features["input_sha256"],
    }
