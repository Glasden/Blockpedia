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
