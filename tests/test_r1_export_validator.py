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
    _analyze_png_pixels,
    _check_image_quality,
    _jcs_canonical,
    _parse_png,
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


def _make_progress_fixture(export_dir: Path) -> None:
    export_dir.mkdir()
    (export_dir / "renders").mkdir()
    (export_dir / "manifest.json").write_text('{"schema_version":"export-manifest.v1"}\n', encoding="utf-8")
    (export_dir / "blocks.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    (export_dir / "states.jsonl").write_text("{}\n{}\n", encoding="utf-8")
    (export_dir / "variants.jsonl").write_text('{"status":"selected"}\n{"status":"selected"}\n', encoding="utf-8")
    (export_dir / "failures.jsonl").write_text("{}\n", encoding="utf-8")
    (export_dir / "checksums.sha256").write_text("", encoding="utf-8")
    (export_dir / "exporter.log").write_text("", encoding="utf-8")


def test_r1_validator_progress_is_monotonic_and_uses_stable_units(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    _make_progress_fixture(export_dir)
    events: list[tuple[str, int, int | None, str]] = []

    def record_progress(phase: str, completed: int, total: int | None, unit: str) -> None:
        events.append((phase, completed, total, unit))

    validate_export(
        Path(__file__).resolve().parents[1],
        export_dir,
        on_progress=record_progress,
    )

    expected_phases = {
        "INVENTORY",
        "SCHEMAS_MANIFEST",
        "JSONL_RECORDS",
        "CROSS_REFERENCES",
        "RENDERS",
        "CHECKSUMS",
        "FINALIZE",
    }
    assert {phase for phase, _, _, _ in events} == expected_phases
    events_by_phase = {
        phase: [event for event in events if event[0] == phase]
        for phase in expected_phases
    }
    assert len(events_by_phase["INVENTORY"]) == 8
    assert len(events_by_phase["SCHEMAS_MANIFEST"]) == 8
    assert len(events_by_phase["JSONL_RECORDS"]) == 8
    assert len(events_by_phase["CROSS_REFERENCES"]) == 8
    assert len(events_by_phase["RENDERS"]) == 3
    assert len(events_by_phase["CHECKSUMS"]) == 7
    assert events_by_phase["JSONL_RECORDS"][-1][1:] == (7, None, "records")
    assert {event[2] for event in events_by_phase["CROSS_REFERENCES"][1:]} == {7}
    assert {event[2] for event in events_by_phase["RENDERS"]} == {2}
    assert {event[2] for event in events_by_phase["CHECKSUMS"]} == {6}
    previous: dict[str, int] = {}
    totals: dict[str, int | None] = {}
    for phase, completed, total, unit in events:
        assert phase in expected_phases
        assert completed >= 0
        assert total is None or total >= completed
        assert unit in {"files", "records", "renders", "checks"}
        assert completed >= previous.get(phase, 0)
        if total is not None:
            assert totals.get(phase, total) == total
            totals[phase] = total
        previous[phase] = completed


def test_r1_validator_progress_does_not_change_default_report(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    repo_root = Path(__file__).resolve().parents[1]

    default_report = validate_export(repo_root, export_dir)
    callback_report = validate_export(repo_root, export_dir, on_progress=lambda *_: None)

    assert callback_report == default_report


def test_r1_validator_progress_callback_failure_propagates(tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    _make_progress_fixture(export_dir)

    class ProgressFailure(RuntimeError):
        pass

    def fail(phase: str, completed: int, total: int | None, unit: str) -> None:
        if phase == "JSONL_RECORDS" and completed == 1:
            raise ProgressFailure("progress persistence failed")

    with pytest.raises(ProgressFailure, match="progress persistence failed"):
        validate_export(Path(__file__).resolve().parents[1], export_dir, on_progress=fail)


def test_r1_validator_uses_one_validator_run(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    calls = 0
    original_run = Validator.run

    def counted_run(self: Validator, *, on_progress=None):
        nonlocal calls
        calls += 1
        return original_run(self, on_progress=on_progress)

    monkeypatch.setattr(Validator, "run", counted_run)
    validate_export(Path(__file__).resolve().parents[1], export_dir, on_progress=lambda *_: None)

    assert calls == 1


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


def test_r1_progress_does_not_add_png_reads_or_decodes(monkeypatch, tmp_path: Path) -> None:
    from tools import validate_r1_export as validator_module

    png_path = tmp_path / "progress.png"
    pixels = bytearray(4 * 4 * 4)
    for index in (5, 6, 9, 10):
        pixels[index * 4 : index * 4 + 4] = bytes((80, 80, 80, 255))
    png_path.write_bytes(_rgba_png(4, 4, bytes(pixels)))
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    repo_root = Path(__file__).resolve().parents[1]
    original_read_bytes = Path.read_bytes
    original_parse_png = validator_module._parse_png
    png_read_count = 0
    png_decode_count = 0

    def counted_read(path: Path) -> bytes:
        nonlocal png_read_count
        if path == png_path:
            png_read_count += 1
        return original_read_bytes(path)

    def counted_parse(raw: bytes):
        nonlocal png_decode_count
        png_decode_count += 1
        return original_parse_png(raw)

    def check_renders(validator: Validator) -> None:
        assert _read_png(png_path, validator) is not None
        assert _read_png(png_path, validator) is not None

    monkeypatch.setattr(Path, "read_bytes", counted_read)
    monkeypatch.setattr(validator_module, "_parse_png", counted_parse)
    monkeypatch.setattr(Validator, "_check_renders", check_renders)

    validate_export(repo_root, export_dir)
    validate_export(repo_root, export_dir, on_progress=lambda *_: None)

    assert png_read_count == 2
    assert png_decode_count == 2


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


def _analysis_from_pixels(width: int, height: int, pixels: bytes) -> _PngAnalysis:
    raw = _rgba_png(width, height, pixels)
    parsed_width, parsed_height, bit_depth, color_type, interlace, idat = _parse_png(raw)
    assert (parsed_width, parsed_height, bit_depth, color_type, interlace) == (width, height, 8, 6, 0)
    decoded = zlib.decompress(idat)
    return _analyze_png_pixels(decoded, width, height, width * 4)


def test_r1_transparent_edge_on_quadrant_is_accepted() -> None:
    width = height = 8
    pixels = bytearray(width * height * 4)
    for x, y in ((1, 1), (2, 1), (1, 2), (2, 2), (5, 1), (6, 1), (5, 2), (6, 2), (5, 5), (6, 5), (5, 6), (6, 6)):
        pixels[(y * width + x) * 4 : (y * width + x + 1) * 4] = bytes((80, 80, 80, 255))

    analysis = _analysis_from_pixels(width, height, bytes(pixels))
    validator = Validator(Path.cwd(), Path.cwd())

    _check_image_quality(analysis, validator, "minecraft:edge_on")

    assert analysis.quadrant_nontransparent == (4, 4, 0, 4)
    assert not validator.issues


def test_r1_entirely_transparent_composite_is_rejected() -> None:
    analysis = _analysis_from_pixels(8, 8, bytes(8 * 8 * 4))
    validator = Validator(Path.cwd(), Path.cwd())

    _check_image_quality(analysis, validator, "minecraft:empty")

    assert any(issue.code == "BACKGROUND_ONLY_RENDER" for issue in validator.issues)


def test_r1_canonical_missing_checker_is_strict() -> None:
    width = height = 8
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels.extend(bytes((0, 0, 0, 255) if ((x < 4) == (y < 4)) else (248, 0, 248, 255)))

    analysis = _analysis_from_pixels(width, height, bytes(pixels))

    assert analysis.missing_texture

    inverted = bytearray()
    for y in range(height):
        for x in range(width):
            inverted.extend(bytes((248, 0, 248, 255) if ((x < 4) == (y < 4)) else (0, 0, 0, 255)))

    assert not _analysis_from_pixels(width, height, bytes(inverted)).missing_texture


@pytest.mark.parametrize(
    "colors",
    [
        ((248, 0, 248, 255), (248, 0, 248, 255)),
        ((0, 0, 0, 255), (0, 0, 0, 255)),
        ((180, 48, 180, 255), (32, 24, 24, 255)),
        ((17, 93, 41, 255), (201, 19, 77, 255)),
    ],
)
def test_r1_ambiguous_or_nonchecker_colors_do_not_fail(colors: tuple[tuple[int, ...], tuple[int, ...]]) -> None:
    width = height = 8
    pixels = bytearray()
    for y in range(height):
        for x in range(width):
            pixels.extend(bytes(colors[0] if ((x < 4) == (y < 4)) else colors[1]))

    analysis = _analysis_from_pixels(width, height, bytes(pixels))

    assert not analysis.missing_texture


def test_r1_random_color_mixture_does_not_fail() -> None:
    width = height = 8
    pixels = bytearray()
    for index in range(width * height):
        pixels.extend(bytes(((index * 73) % 256, (index * 151) % 256, (index * 29) % 256, 255)))

    analysis = _analysis_from_pixels(width, height, bytes(pixels))

    assert not analysis.missing_texture


def test_r1_current_render_policy_is_v2_and_records_controls() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    constants = (repo_root / "src/main/java/com/blockpedia/exporter/ExporterConstants.java").read_text()
    environment = (repo_root / "src/main/java/com/blockpedia/exporter/RenderEnvironment.java").read_text()
    renderer = (repo_root / "src/main/java/com/blockpedia/exporter/RenderExporter.java").read_text()

    assert 'RENDER_POLICY_VERSION = "render.v2"' in constants
    assert "block_model_resolver_seed=42L" in environment
    assert "block_atlas_reload=awaited_before_prepare" in environment
    assert "block_atlas_animation=cycleAnimationFrames_cancelled_while_exporting" in environment
    assert "getAtlasOrThrow(AtlasIds.BLOCKS)" in renderer
    assert "getAtlasOrThrow(TextureAtlas.LOCATION_BLOCKS)" not in renderer


def test_r1_dynamic_block_entity_fixture_skips_are_exact_and_pre_render() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    constants_source = (repo_root / "src/main/java/com/blockpedia/exporter/ExporterConstants.java").read_text()
    package_source = (repo_root / "src/main/java/com/blockpedia/exporter/ExportPackage.java").read_text()

    token = "pre-render-skip.v1;reason=BLOCK_ENTITY_FIXTURE_UNSUPPORTED;ids=minecraft:end_gateway,minecraft:end_portal"
    assert f'PRE_RENDER_SKIP_POLICY_TOKEN = "{token}"' in constants_source
    assert '"minecraft:end_portal"' in package_source
    assert '"minecraft:end_gateway"' in package_source
    assert '"BLOCK_ENTITY_FIXTURE_UNSUPPORTED"' in package_source
    assert "TextureAtlas" not in package_source
    render_flow = package_source[package_source.index("boolean renderVariantStep()") :]
    assert render_flow.index("isUnsupportedDynamicBlockEntity") < render_flow.index("RenderPaths.forBlockId(block.blockId.toString())")
    signature = package_source[package_source.index("String logicalInputSignature = JsonCanonical.sha256Framed") :]
    assert signature.index("ExporterConstants.DEDUPE_POLICY_VERSION") < signature.index("ExporterConstants.PRE_RENDER_SKIP_POLICY_TOKEN") < signature.index("snapshot.hash()")
