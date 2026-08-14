from __future__ import annotations

import math
import os
import struct
import zlib
from pathlib import Path

import pytest

from tools.validate_r1_export import (
    EXPORT_ID_RE,
    _PngAnalysis,
    _check_image_quality,
    _jcs_canonical,
    _read_png,
    _render_reference_paths,
    Validator,
    validate_export,
)


def test_jcs_boundary_vectors() -> None:
    assert _jcs_canonical({"\ufb33": 1, "\ud834\udd1e": 2, "a": 3}) == '{"a":3,"𝄞":2,"דּ":1}'
    assert _jcs_canonical(-0.0) == "0"
    assert _jcs_canonical(1e-6) == "0.000001"
    assert _jcs_canonical(1e-7) == "1e-7"
    assert _jcs_canonical(1e20) == "100000000000000000000"
    assert _jcs_canonical(1e21) == "1e+21"
    assert _jcs_canonical(5e-324) == "5e-324"
    try:
        _jcs_canonical(math.inf)
    except ValueError:
        pass
    else:
        raise AssertionError("non-finite numbers must be rejected")


def test_r1_validator_rejects_a_checksum_tamper(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    (export_dir / "checksums.sha256").write_text("not a checksum\n", encoding="utf-8")

    report = validate_export(Path(__file__).resolve().parents[1], export_dir)

    assert report["status"] == "failed"
    assert any(issue["code"] == "CHECKSUM_LINE_INVALID" for issue in report["issues"])


def test_r1_validator_reports_helpful_missing_package_errors(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    report = validate_export(Path(__file__).resolve().parents[1], export_dir)

    assert report["status"] == "failed"
    codes = {issue["code"] for issue in report["issues"]}
    assert "MANIFEST_READ_FAILED" in codes
    assert "CHECKSUM_FILE_READ_FAILED" in codes


def test_r1_v1_export_identity_and_render_path_boundaries() -> None:
    assert EXPORT_ID_RE.fullmatch("export_20260814T165501Z")
    assert EXPORT_ID_RE.fullmatch("export_20260814T165501Z_01")
    assert EXPORT_ID_RE.fullmatch("export_20260814T165501Z_99")
    for value in (
        "export_20260814T165501Z_00",
        "export_20260814T165501Z_100",
        "exp_94e0e4b170a841d9a839b052a2c08e10",
        ".export_20260814T165501Z.staging",
    ):
        assert not EXPORT_ID_RE.fullmatch(value)

    assert _render_reference_paths("minecraft:stone") == (
        "renders/minecraft/stone/preview.png",
        "renders/minecraft/stone/mask.png",
        "renders/minecraft/stone/render.json",
    )
    assert _render_reference_paths("minecraft:foo/bar") == (
        "renders/minecraft/foo/bar/preview.png",
        "renders/minecraft/foo/bar/mask.png",
        "renders/minecraft/foo/bar/render.json",
    )
    for block_id in (
        "minecraft:../stone",
        "minecraft:foo/.",
        "minecraft:foo/CON",
        "minecraft:foo/bar.",
        "minecraft:foo\\bar",
    ):
        assert _render_reference_paths(block_id) is None


def test_r1_variant_id_must_equal_block_id() -> None:
    validator = Validator(Path.cwd(), Path.cwd())
    validator.manifest = {"export_id": "export_20260814T165501Z"}
    validator.records = {
        "blocks.jsonl": [],
        "states.jsonl": [],
        "variants.jsonl": [{
            "variant_id": "minecraft:stone",
            "block_id": "minecraft:dirt",
        }],
        "failures.jsonl": [],
    }

    validator._check_cross_record_invariants()

    assert any(issue.code == "VARIANT_ID_BLOCK_MISMATCH" for issue in validator.issues)


def test_r1_cross_record_references_require_existing_and_matching_blocks() -> None:
    validator = Validator(Path.cwd(), Path.cwd())
    validator.manifest = {"export_id": "export_20260814T165501Z"}
    validator.records = {
        "blocks.jsonl": [{"block_id": "minecraft:stone"}],
        "states.jsonl": [{"state_id": "minecraft:dirt", "block_id": "minecraft:dirt"}],
        "variants.jsonl": [{"variant_id": "minecraft:dirt", "block_id": "minecraft:dirt"}],
        "failures.jsonl": [{
            "scope": "state",
            "block_id": "minecraft:stone",
            "state_id": "minecraft:dirt",
        }, {
            "scope": "variant",
            "block_id": "minecraft:stone",
            "variant_id": "minecraft:dirt",
        }],
    }

    validator._check_cross_record_invariants()

    codes = {issue.code for issue in validator.issues}
    assert "STATE_BLOCK_REFERENCE_INVALID" in codes
    assert "VARIANT_BLOCK_REFERENCE_INVALID" in codes
    assert "FAILURE_STATE_BLOCK_MISMATCH" in codes
    assert "FAILURE_VARIANT_BLOCK_MISMATCH" in codes


def _rgba_png(width: int, height: int, pixels: bytes) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    scanlines = b"".join(b"\x00" + pixels[row * width * 4 : (row + 1) * width * 4] for row in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b"")


def test_r1_png_analysis_reuses_one_read_and_decode(monkeypatch, tmp_path: Path) -> None:
    from tools import validate_r1_export as validator_module

    png_path = tmp_path / "preview.png"
    pixels = bytearray(4 * 4 * 4)
    for index in (5, 6, 9, 10):
        pixels[index * 4 : index * 4 + 4] = bytes((80, 80, 80, 255))
    png_path.write_bytes(_rgba_png(4, 4, bytes(pixels)))
    validator = validator_module.Validator(tmp_path, tmp_path)
    read_count = 0
    decode_count = 0
    original_read_bytes = Path.read_bytes
    original_parse_png = validator_module._parse_png
    read_counts: dict[Path, int] = {}

    def counted_read(path: Path) -> bytes:
        nonlocal read_count
        read_count += 1
        read_counts[path] = read_counts.get(path, 0) + 1
        return original_read_bytes(path)

    def counted_parse(raw: bytes):
        nonlocal decode_count
        decode_count += 1
        return original_parse_png(raw)

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    monkeypatch.setattr(validator_module, "_parse_png", counted_parse)

    first = _read_png(png_path, validator)
    second = _read_png(png_path, validator)
    assert first is not None
    assert (first.width, first.height, first.pixel_format, first.has_object) == (4, 4, "RGBA", True)
    _check_image_quality(first, validator, "minecraft:test")
    digest = validator._sha256_prefixed(png_path)
    checksum_path = tmp_path / "checksums.sha256"
    checksum_path.write_bytes(f"{digest.removeprefix('sha256:')}  preview.png\n".encode("utf-8"))
    validator._files = {"preview.png": png_path}
    validator._check_checksums()

    assert isinstance(first, _PngAnalysis)
    assert second is first
    assert read_counts[png_path] == 1
    assert decode_count == 1
    assert png_path not in validator._bytes_cache
    assert png_path in validator._digest_cache
    assert not validator.issues


def test_r1_inventory_rejects_hardlinks(tmp_path: Path) -> None:
    export_dir = tmp_path / "export_20260814T165501Z"
    export_dir.mkdir()
    regular = export_dir / "regular.txt"
    regular.write_bytes(b"regular")
    hardlink = export_dir / "hardlink.txt"
    try:
        os.link(regular, hardlink)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support hardlinks")

    validator = Validator(tmp_path, export_dir)
    validator._index_files()

    codes = {issue.code for issue in validator.issues}
    assert "INVENTORY_HARDLINK_REJECTED" in codes
    assert "hardlink.txt" not in validator._files


def test_r1_inventory_rejects_file_symlinks(tmp_path: Path) -> None:
    export_dir = tmp_path / "export_20260814T165501Z"
    export_dir.mkdir()
    regular = export_dir / "regular.txt"
    regular.write_bytes(b"regular")
    symlink = export_dir / "symlink.txt"
    try:
        symlink.symlink_to(regular)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support file symlinks")

    validator = Validator(tmp_path, export_dir)
    validator._index_files()

    assert any(issue.code == "INVENTORY_SYMLINK_REJECTED" for issue in validator.issues)
    assert "symlink.txt" not in validator._files


def test_r1_inventory_does_not_follow_root_symlink(tmp_path: Path) -> None:
    real_dir = tmp_path / "real-export"
    real_dir.mkdir()
    link_dir = tmp_path / "export_20260814T165501Z"
    try:
        link_dir.symlink_to(real_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("filesystem does not support directory symlinks")

    validator = Validator(tmp_path, link_dir)
    validator._check_directories()

    assert any(issue.code == "INVENTORY_SYMLINK_REJECTED" for issue in validator.issues)
    assert not validator._files
