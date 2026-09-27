"""The Node PNG decoder matches the Python reference on every filter type."""

from __future__ import annotations

import base64
import json
import struct
import subprocess
import zlib

from blockpedia.png import decode_rgba_png
import pytest

from .test_node_mcp import NODE, ROOT


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    return a if pa <= pb and pa <= pc else b if pb <= pc else c


def _filtered_png(width: int, height: int, pixels: bytes, filters: list[int]) -> bytes:
    stride = width * 4
    raw = bytearray()
    previous = bytes(stride)
    for row in range(height):
        line = pixels[row * stride:(row + 1) * stride]
        kind = filters[row % len(filters)]
        out = bytearray()
        for i, value in enumerate(line):
            left = line[i - 4] if i >= 4 else 0
            up = previous[i]
            upper_left = previous[i - 4] if i >= 4 else 0
            predictor = [0, left, up, (left + up) >> 1, _paeth(left, up, upper_left)][kind]
            out.append((value - predictor) & 0xFF)
        raw += bytes([kind]) + out
        previous = line

    def chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", zlib.crc32(name + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b"")


def test_every_filter_type_in_first_and_later_rows() -> None:
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    width, height = 7, 12
    pixels = bytes((x * 37 + y * 91 + c * 53 + (x * y) % 13) % 256 for y in range(height) for x in range(width) for c in range(4))
    # Each filter type starts the image once (no row above) and recurs later.
    images = [_filtered_png(width, height, pixels, [kind, 4, 1, 3, 2, 0, 4, 3]) for kind in range(5)]
    script = f"""import {{ decodeRgbaPng }} from {json.dumps((ROOT / 'mcp-node/png.mjs').as_uri())};
const inputs = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(inputs.map((item) => decodeRgbaPng(Buffer.from(item, 'base64')).pixels.toString('base64'))));
"""
    script = script.replace("require('node:fs')", "(await import('node:fs'))")
    result = subprocess.run([str(NODE), "--input-type=module", "-e", script], input=json.dumps([base64.b64encode(item).decode() for item in images]).encode(), capture_output=True, check=True)
    decoded = [base64.b64decode(item) for item in json.loads(result.stdout)]
    for payload, pixels_out in zip(images, decoded, strict=True):
        assert decode_rgba_png(payload).pixels == pixels_out == pixels


def _run_png_module(body: str) -> object:
    script = f"import * as png from {json.dumps((ROOT / 'mcp-node/png.mjs').as_uri())};\n{body}"
    result = subprocess.run([str(NODE), "--input-type=module", "-e", script], capture_output=True, check=True)
    return json.loads(result.stdout)


def test_tile_labels_name_the_block_and_wrap_after_underscores() -> None:
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    labels = _run_png_module("""process.stdout.write(JSON.stringify({
  plain: png.tileLabel(2, 'minecraft:snow_block'),
  bare: png.tileLabel(0),
  other: png.tileLabel(0, 'other:thing'),
  wide: png.wrapLabel(png.tileLabel(4, 'minecraft:waxed_weathered_cut_copper_stairs'), 248, 2),
  narrow: png.wrapLabel(png.tileLabel(1, 'minecraft:polished_blackstone_pressure_plate'), 124, 2),
  unbroken: png.wrapLabel('T01 abcdefghijklmnopqrstuvwxyz', 60, 2),
  unbrokenWidths: png.wrapLabel('T01 abcdefghijklmnopqrstuvwxyz', 60, 2).map((line) => png.textWidth(line, 2)),
}));""")
    assert labels["plain"] == "T03 snow_block"
    assert labels["bare"] == "T01"
    assert labels["other"] == "T01 other:thing"
    assert labels["wide"] == ["T05 waxed_weathered_cut_", "copper_stairs"]
    assert labels["narrow"] == ["T02 polished_", "blackstone_", "pressure_plate"]
    # A word wider than the line is split mid-word; no text is dropped.
    assert "".join(labels["unbroken"]).replace(" ", "") == "T01abcdefghijklmnopqrstuvwxyz"
    assert len(labels["unbroken"]) > 2 and max(labels["unbrokenWidths"]) <= 60


def test_contact_sheet_labels_differ_by_block_and_keep_full_cards_square() -> None:
    if not NODE.is_file():
        pytest.skip("Node 24 is unavailable")
    sheets = _run_png_module("""const image = { width: 512, height: 512, pixels: Buffer.alloc(512 * 512 * 4, 0x80) };
const size = (sheet) => [sheet.width, sheet.height];
const bytes = (sheet) => Buffer.from(sheet.webp).toString('base64');
process.stdout.write(JSON.stringify({
  full: size(png.makeContactSheet([image, image], 4, 'full', ['minecraft:waxed_weathered_cut_copper_stairs', 'minecraft:stone'])),
  compactShort: size(png.makeContactSheet([image, image], 4, 'compact', ['minecraft:stone', 'minecraft:glass'])),
  compactLong: size(png.makeContactSheet([image, image], 4, 'compact', ['minecraft:stone', 'minecraft:polished_blackstone_pressure_plate'])),
  stone: bytes(png.makeContactSheet([image], 4, 'full', ['minecraft:stone'])),
  glass: bytes(png.makeContactSheet([image], 4, 'full', ['minecraft:glass'])),
}));""")
    assert sheets["full"] == [512, 256]
    # One 14px line (12px text + 2px padding each side) vs three wrapped lines.
    assert sheets["compactShort"] == [256, 64 + 16]
    assert sheets["compactLong"] == [256, 64 + 44]
    assert sheets["stone"] != sheets["glass"]
