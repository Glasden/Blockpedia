"""Shared non-interlaced RGBA PNG parsing, with no Studio dependencies."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class PngDecodeError(ValueError):
    pass


@dataclass(frozen=True, slots=True)

class DecodedPng:
    width: int
    height: int
    pixels: bytes


@dataclass(frozen=True, slots=True)

class PngMetadata:
    width: int
    height: int


@dataclass(frozen=True, slots=True)

class _ParsedPng:
    width: int
    height: int
    scanlines: bytes


def decode_rgba_png(source: bytes | bytearray | memoryview | Path | str) -> DecodedPng:
    parsed = _parse_rgba_png(source)
    width, height, decoded = parsed.width, parsed.height, parsed.scanlines
    row_bytes = width * 4
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


def validate_rgba_png(source: bytes | bytearray | memoryview | Path | str) -> PngMetadata:
    parsed = _parse_rgba_png(source)
    return PngMetadata(parsed.width, parsed.height)


def _parse_rgba_png(source: bytes | bytearray | memoryview | Path | str) -> _ParsedPng:
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
    for row in range(height):
        filter_type = decoded[row * (row_bytes + 1)]
        if filter_type not in {0, 1, 2, 3, 4}:
            raise PngDecodeError(f"unsupported PNG filter {filter_type}")
    return _ParsedPng(width, height, decoded)


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
