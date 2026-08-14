"""Validate one Minecraft 26.2 exporter package without modifying it.

This validator deliberately covers the R1 package boundary only: strict
exporter Schemas, JSONL references and counts, PNG dimensions/decoding,
resource-asset blacklist paths, and the package checksum file.  It does not
select variants or render images.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from jsonschema import Draft202012Validator

SCHEMA_IDS = (
    "export-block.v1",
    "export-failure.v1",
    "export-manifest.v1",
    "export-state.v1",
    "export-variant.v1",
    "render-metadata.v1",
)
SCHEMA_PATHS = {
    schema_id: Path("schemas/exporter") / f"{schema_id}.json"
    for schema_id in SCHEMA_IDS
}
JSONL_SCHEMAS = {
    "blocks.jsonl": "export-block.v1",
    "states.jsonl": "export-state.v1",
    "variants.jsonl": "export-variant.v1",
    "failures.jsonl": "export-failure.v1",
}
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
CHECKSUM_RE = re.compile(r"^([0-9a-f]{64})  ([^\r\n]+)\n$")
EXPORT_ID_RE = re.compile(r"^export_[0-9]{8}T[0-9]{6}Z(?:_(?:0[1-9]|[1-9][0-9]))?$")
STAGING_EXPORT_ID_RE = re.compile(r"^\.(export_[0-9]{8}T[0-9]{6}Z(?:_(?:0[1-9]|[1-9][0-9]))?)\.staging$")
BLOCK_ID_RE = re.compile(r"^minecraft:[a-z0-9_./-]+$")
ASSET_EXTENSIONS = {".jar", ".zip", ".mcpack", ".mcmeta", ".7z", ".rar"}
ASSET_PARTS = {
    "assets",
    "blockstates",
    "models",
    "textures",
    "font",
    "fonts",
    "sound",
    "sounds",
}
WINDOWS_DEVICE_NAMES = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


@dataclass(frozen=True)
class Issue:
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class _PngAnalysis:
    """Compact facts retained after one PNG decode and pixel traversal."""

    width: int
    height: int
    pixel_format: str
    has_object: bool
    alpha_bounds: tuple[int, int, int, int] | None
    nontransparent: int
    background_pixels: int
    magenta_pixels: int
    near_black_pixels: int
    quadrant_nontransparent: tuple[int, int, int, int]
    background_only: bool
    object_too_small: bool
    off_canvas: bool
    missing_texture: bool

    # Keep the old tuple-style observation usable for narrow callers while
    # retaining the richer single-pass facts for the validator.
    def __getitem__(self, index: int | slice) -> Any:
        values = (self.width, self.height, self.pixel_format, self.has_object)
        return values[index]

    def __iter__(self):
        yield self.width
        yield self.height
        yield self.pixel_format
        yield self.has_object


class Validator:
    def __init__(self, repo_root: Path, export_dir: Path) -> None:
        self.repo_root = repo_root.resolve()
        # Keep the supplied package directory un-resolved so inventory can
        # reject a symlink at the package root instead of silently following
        # it before lstat() sees it.
        self.export_dir = Path(os.path.abspath(export_dir))
        self.issues: list[Issue] = []
        self.schemas: dict[str, Mapping[str, Any]] = {}
        self.validators: dict[str, Draft202012Validator] = {}
        self.records: dict[str, list[Mapping[str, Any]]] = {}
        self.manifest: Mapping[str, Any] | None = None
        self._files: dict[str, Path] = {}
        self._directories: set[str] = set()
        self._bytes_cache: dict[Path, bytes] = {}
        self._digest_cache: dict[Path, str] = {}
        self._schema_digest_cache: dict[str, str] = {}
        self._schema_bytes_cache: dict[str, bytes] = {}
        # A failed decode is cached as None as well.  This keeps a malformed
        # artifact from being read/decoded again if more than one semantic
        # check reaches the same path.
        self._png_cache: dict[Path, _PngAnalysis | None] = {}
        self._early_reject = False

    def add(self, code: str, detail: str) -> None:
        self.issues.append(Issue(code, detail))

    def run(self) -> dict[str, Any]:
        self._check_directories()
        if self._early_reject:
            return self._report()
        self._load_schemas()
        self._read_manifest()
        self._read_jsonl()
        self._check_package_files()
        self._check_asset_blacklist()
        self._check_cross_record_invariants()
        self._check_renders()
        self._check_checksums()
        self._check_manifest_counts_and_status()
        return self._report()

    def _report(self) -> dict[str, Any]:
        return {
            "status": "passed" if not self.issues else "failed",
            "repo_root": self.repo_root.as_posix(),
            "export_dir": self.export_dir.as_posix(),
            "issues": [issue.__dict__ for issue in self.issues],
        }

    def _check_directories(self) -> None:
        try:
            root_metadata = self.export_dir.lstat()
        except OSError:
            self.add("EXPORT_DIR_MISSING", str(self.export_dir))
            return
        if stat.S_ISLNK(root_metadata.st_mode):
            self.add("INVENTORY_SYMLINK_REJECTED", ".")
            self._early_reject = True
            return
        if not stat.S_ISDIR(root_metadata.st_mode):
            self.add("EXPORT_DIR_MISSING", str(self.export_dir))
            return
        directory_name = self.export_dir.name
        if STAGING_EXPORT_ID_RE.fullmatch(directory_name) or (
            directory_name.startswith(".") and directory_name.endswith(".staging")
        ):
            self.add("EXPORT_DIR_STAGING", directory_name)
            self._early_reject = True
        elif not EXPORT_ID_RE.fullmatch(directory_name):
            self.add("EXPORT_DIR_NAME_INVALID", directory_name)
            # Known pre-v1 identities can be rejected before walking thousands
            # of render files.  A generic invalid name is still fully checked
            # so callers retain useful missing-package diagnostics.
            if directory_name.startswith(("export_", "exp_", "vv_")):
                self._early_reject = True

        if self._early_reject:
            return

        self._index_files()
        if "renders" not in self._directories:
            self.add("RENDERS_DIR_MISSING", "renders/")

    def _index_files(self) -> None:
        """Build the package file/directory inventory exactly once."""

        self._files.clear()
        self._directories.clear()
        for root, directories, filenames in os.walk(self.export_dir, followlinks=False):
            root_path = Path(root)
            accepted_directories: list[str] = []
            for directory in directories:
                path = root_path / directory
                relative = path.relative_to(self.export_dir).as_posix()
                try:
                    metadata = path.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    self.add("INVENTORY_SYMLINK_REJECTED", relative)
                    continue
                if not stat.S_ISDIR(metadata.st_mode):
                    continue
                self._directories.add(relative)
                accepted_directories.append(directory)
            # os.walk is non-following by default, but pruning the list makes
            # the no-descent rule explicit for directory symlinks too.
            directories[:] = accepted_directories
            for filename in filenames:
                path = root_path / filename
                relative = path.relative_to(self.export_dir).as_posix()
                try:
                    metadata = path.lstat()
                except OSError:
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    self.add("INVENTORY_SYMLINK_REJECTED", relative)
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue
                if metadata.st_nlink != 1:
                    self.add("INVENTORY_HARDLINK_REJECTED", relative)
                    continue
                self._files[relative] = path

    def _read_bytes(self, path: Path) -> bytes:
        if path in self._bytes_cache:
            return self._bytes_cache[path]
        raw = path.read_bytes()
        self._bytes_cache[path] = raw
        return raw

    def _sha256_hex(self, path: Path) -> str:
        cached = self._digest_cache.get(path)
        if cached is None:
            cached = hashlib.sha256(self._read_bytes(path)).hexdigest()
            self._digest_cache[path] = cached
        return cached

    def _sha256_prefixed(self, path: Path) -> str:
        return "sha256:" + self._sha256_hex(path)

    def _load_schemas(self) -> None:
        for schema_id, relative_path in SCHEMA_PATHS.items():
            path = self.repo_root / relative_path
            try:
                raw = self._read_bytes(path)
                value = json.loads(_decode_text(raw, path))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                self.add("SCHEMA_READ_FAILED", f"{relative_path.as_posix()}: {type(exc).__name__}")
                continue
            if not isinstance(value, Mapping):
                self.add("SCHEMA_NOT_OBJECT", relative_path.as_posix())
                continue
            self.schemas[schema_id] = value
            self._schema_bytes_cache[schema_id] = raw
            self._schema_digest_cache[schema_id] = "sha256:" + self._sha256_hex(path)
            try:
                self.validators[schema_id] = Draft202012Validator(value)
                Draft202012Validator.check_schema(value)
            except Exception as exc:  # jsonschema exposes several schema errors
                self.add("SCHEMA_INVALID", f"{schema_id}: {type(exc).__name__}")

    def _check_package_files(self) -> None:
        if not self.export_dir.is_dir():
            return
        required = {
            "manifest.json",
            "blocks.jsonl",
            "states.jsonl",
            "variants.jsonl",
            "failures.jsonl",
            "checksums.sha256",
            "exporter.log",
        }
        for filename in sorted(required):
            if filename not in self._files:
                self.add("REQUIRED_FILE_MISSING", filename)
        for relative in self._files:
            if relative in required:
                continue
            if not relative.startswith("renders/"):
                self.add("UNDECLARED_FILE", relative)
                continue
            if PurePosixPath(relative).name not in {"preview.png", "mask.png", "render.json"}:
                self.add("UNDECLARED_RENDER_FILE", relative)

    def _read_manifest(self) -> None:
        path = self.export_dir / "manifest.json"
        try:
            value = json.loads(_decode_text(self._read_bytes(path), path))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            self.add("MANIFEST_READ_FAILED", f"{path.name}: {type(exc).__name__}")
            return
        if not isinstance(value, Mapping):
            self.add("MANIFEST_NOT_OBJECT", path.name)
            return
        self.manifest = value
        self._validate_schema("export-manifest.v1", value, path.name)
        export_id = value.get("export_id")
        if isinstance(export_id, str) and self.export_dir.name != export_id:
            self.add(
                "EXPORT_DIR_NAME_MISMATCH",
                f"directory {self.export_dir.name!r} does not equal manifest export_id {export_id!r}",
            )
        if self.manifest.get("status") == "failed":
            self.add("FAILED_EXPORT_NOT_ACCEPTED", "failed export staging is diagnostic only")
        inventory = value.get("schema_inventory")
        if isinstance(inventory, list):
            expected = list(SCHEMA_IDS)
            actual = [item.get("schema_id") for item in inventory if isinstance(item, Mapping)]
            if actual != expected:
                self.add("SCHEMA_INVENTORY_ORDER", f"expected {expected!r}, got {actual!r}")
            for item in inventory:
                if not isinstance(item, Mapping):
                    continue
                schema_id = item.get("schema_id")
                if schema_id not in SCHEMA_PATHS:
                    continue
                expected_hash = self._schema_digest_cache.get(schema_id)
                if expected_hash is None:
                    continue
                if item.get("schema_sha256") != expected_hash:
                    self.add("SCHEMA_INVENTORY_HASH_MISMATCH", str(schema_id))

    def _read_jsonl(self) -> None:
        for filename, schema_id in JSONL_SCHEMAS.items():
            path = self.export_dir / filename
            records: list[Mapping[str, Any]] = []
            try:
                raw = self._read_bytes(path)
                text = _decode_text(raw, path)
                _require_lf_text(raw, path, self)
            except (OSError, UnicodeError) as exc:
                self.add("JSONL_READ_FAILED", f"{filename}: {type(exc).__name__}")
                self.records[filename] = records
                continue
            for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
                if line == "\n":
                    self.add("JSONL_EMPTY_LINE", f"{filename}:{line_number}")
                    continue
                if not line.endswith("\n"):
                    self.add("JSONL_MISSING_FINAL_LF", f"{filename}:{line_number}")
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.add("JSONL_PARSE_FAILED", f"{filename}:{line_number}: {exc.msg}")
                    continue
                if not isinstance(value, Mapping):
                    self.add("JSONL_RECORD_NOT_OBJECT", f"{filename}:{line_number}")
                    continue
                records.append(value)
                self._validate_schema(schema_id, value, f"{filename}:{line_number}")
            self.records[filename] = records

        if "failures.jsonl" in self._files:
            return
        self.add("REQUIRED_FILE_MISSING", "failures.jsonl")

    def _validate_schema(self, schema_id: str, value: Mapping[str, Any], location: str) -> None:
        validator = self.validators.get(schema_id)
        if validator is None:
            return
        errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
        for error in errors[:8]:
            path = ".".join(str(part) for part in error.path) or "$"
            self.add("SCHEMA_VALIDATION_FAILED", f"{location} {schema_id} {path}: {error.message}")
        if len(errors) > 8:
            self.add("SCHEMA_VALIDATION_TRUNCATED", f"{location} {schema_id}: {len(errors) - 8} more")

    def _check_asset_blacklist(self) -> None:
        if not self.export_dir.is_dir():
            return
        for posix in self._files:
            path = self._files[posix]
            parts = set(PurePosixPath(posix).parts)
            if path.suffix.casefold() in ASSET_EXTENSIONS or parts.intersection(ASSET_PARTS):
                if not posix.startswith("renders/") or path.suffix.casefold() not in {".png", ".json"}:
                    self.add("ORIGINAL_ASSET_BLACKLISTED", posix)

    def _check_checksums(self) -> None:
        path = self.export_dir / "checksums.sha256"
        try:
            raw = self._read_bytes(path)
            text = _decode_text(raw, path)
            _require_lf_text(raw, path, self)
        except (OSError, UnicodeError) as exc:
            self.add("CHECKSUM_FILE_READ_FAILED", type(exc).__name__)
            return
        listed: dict[str, str] = {}
        for line_number, line in enumerate(text.splitlines(keepends=True), start=1):
            match = CHECKSUM_RE.fullmatch(line)
            if match is None:
                self.add("CHECKSUM_LINE_INVALID", f"line {line_number}")
                continue
            digest, relative = match.groups()
            if not _safe_relative_path(relative):
                self.add("CHECKSUM_PATH_INVALID", relative)
                continue
            if relative in listed:
                self.add("CHECKSUM_PATH_DUPLICATE", relative)
                continue
            listed[relative] = digest

        expected: dict[str, str] = {}
        for relative, file in self._files.items():
            if relative == "checksums.sha256":
                continue
            expected[relative] = self._sha256_hex(file)
        if set(listed) != set(expected):
            missing = sorted(set(expected) - set(listed))
            extra = sorted(set(listed) - set(expected))
            if missing:
                self.add("CHECKSUM_FILES_MISSING", ", ".join(missing))
            if extra:
                self.add("CHECKSUM_FILES_EXTRA", ", ".join(extra))
        for relative, digest in listed.items():
            if relative in expected and expected[relative] != digest:
                self.add("CHECKSUM_MISMATCH", relative)
        if list(listed) != sorted(listed, key=lambda value: value.encode("utf-8")):
            self.add("CHECKSUM_ORDER_INVALID", "paths are not UTF-8 byte sorted")

    def _check_cross_record_invariants(self) -> None:
        manifest = self.manifest
        if manifest is None:
            return
        blocks = self.records.get("blocks.jsonl", [])
        states = self.records.get("states.jsonl", [])
        variants = self.records.get("variants.jsonl", [])
        failures = self.records.get("failures.jsonl", [])
        export_id = manifest.get("export_id")
        block_map: dict[str, Mapping[str, Any]] = {}
        for record in blocks:
            block_id = record.get("block_id")
            if not isinstance(block_id, str) or not BLOCK_ID_RE.fullmatch(block_id):
                self.add("BLOCK_ID_INVALID", repr(block_id))
                continue
            if block_id in block_map:
                self.add("BLOCK_ID_DUPLICATE", block_id)
            block_map[block_id] = record
            self._same_export_id(record, export_id)

        state_map: dict[str, Mapping[str, Any]] = {}
        states_by_block: dict[str, list[Mapping[str, Any]]] = {}
        for record in states:
            state_id = record.get("state_id")
            block_id = record.get("block_id")
            if not isinstance(block_id, str) or block_id not in block_map:
                self.add("STATE_BLOCK_REFERENCE_INVALID", repr(block_id))
            if isinstance(state_id, str):
                if state_id in state_map:
                    self.add("STATE_ID_DUPLICATE", state_id)
                state_map[state_id] = record
            if isinstance(block_id, str):
                states_by_block.setdefault(block_id, []).append(record)
            self._same_export_id(record, export_id)

        variant_map: dict[str, Mapping[str, Any]] = {}
        variants_by_block: dict[str, list[Mapping[str, Any]]] = {}
        for record in variants:
            variant_id = record.get("variant_id")
            block_id = record.get("block_id")
            if not isinstance(block_id, str) or block_id not in block_map:
                self.add("VARIANT_BLOCK_REFERENCE_INVALID", repr(block_id))
            if isinstance(variant_id, str) and isinstance(block_id, str) and variant_id != block_id:
                self.add("VARIANT_ID_BLOCK_MISMATCH", f"{variant_id}: expected {block_id}")
            if (
                record.get("status") == "selected"
                and isinstance(block_id, str)
                and _render_reference_paths(block_id) is None
            ):
                self.add("RENDER_PATH_INVALID", block_id)
            if isinstance(variant_id, str):
                if variant_id in variant_map:
                    self.add("VARIANT_ID_DUPLICATE", variant_id)
                variant_map[variant_id] = record
            if isinstance(block_id, str):
                variants_by_block.setdefault(block_id, []).append(record)
            self._same_export_id(record, export_id)

        failures_by_variant: dict[str, list[Mapping[str, Any]]] = {}
        for record in failures:
            variant_id = record.get("variant_id")
            if isinstance(variant_id, str):
                failures_by_variant.setdefault(variant_id, []).append(record)
            self._same_export_id(record, export_id)
            self._check_failure_reference(record, block_map, state_map, variant_map)

        for block_id, block in block_map.items():
            block_states = states_by_block.get(block_id, [])
            if not block_states:
                self.add("BLOCK_HAS_NO_STATES", block_id)
                continue
            legal_ids = {record.get("state_id") for record in block_states}
            default_id = block.get("default_state_id")
            defaults = [record for record in block_states if record.get("is_default") is True]
            if default_id not in legal_ids:
                self.add("DEFAULT_STATE_NOT_LEGAL", f"{block_id}: {default_id}")
            if len(defaults) != 1 or defaults[0].get("state_id") != default_id:
                self.add("DEFAULT_STATE_UNIQUE_INVALID", block_id)
            if len(variants_by_block.get(block_id, [])) != 1:
                self.add("BLOCK_VARIANT_COUNT_INVALID", block_id)
            for state in block_states:
                self._check_state_canonical_id(state)
                mapping = state.get("mapping_status")
                references = state.get("variant_ids")
                if mapping == "mapped":
                    if not isinstance(references, list) or not references:
                        self.add("MAPPED_STATE_WITHOUT_VARIANT", str(state.get("state_id")))
                    for variant_id in references or []:
                        variant = variant_map.get(variant_id)
                        if variant is None or variant.get("block_id") != block_id:
                            self.add("STATE_VARIANT_REFERENCE_INVALID", str(state.get("state_id")))
                elif mapping == "skipped" and references:
                    self.add("SKIPPED_STATE_HAS_VARIANT", str(state.get("state_id")))
            block_variants = variants_by_block.get(block_id, [])
            if block_variants:
                variant = block_variants[0]
                if variant.get("status") == "selected":
                    represented = set(variant.get("represented_state_ids", []))
                    if represented != legal_ids:
                        self.add("REPRESENTED_STATE_SET_INVALID", block_id)
                    if variant.get("canonical_state_id") != default_id:
                        self.add("REPRESENTATIVE_NOT_DEFAULT", block_id)
                    for state in block_states:
                        if state.get("mapping_status") != "mapped" or state.get("variant_ids") != [variant.get("variant_id")]:
                            self.add("SELECTED_STATE_MAPPING_INVALID", str(state.get("state_id")))
                else:
                    variant_id = variant.get("variant_id")
                    if not isinstance(variant_id, str) or not failures_by_variant.get(variant_id):
                        self.add("SKIPPED_VARIANT_FAILURE_MISSING", str(variant.get("variant_id")))
                    for state in block_states:
                        if state.get("mapping_status") != "skipped" or state.get("variant_ids") != []:
                            self.add("SKIPPED_BLOCK_STATE_MAPPING_INVALID", str(state.get("state_id")))

        self._check_registry_snapshot(block_map)
        self._check_manifest_registry_count(block_map)

    def _same_export_id(self, record: Mapping[str, Any], export_id: Any) -> None:
        if export_id is not None and record.get("export_id") != export_id:
            self.add("EXPORT_ID_MISMATCH", repr(record.get("export_id")))

    def _check_state_canonical_id(self, state: Mapping[str, Any]) -> None:
        state_id = state.get("state_id")
        block_id = state.get("block_id")
        properties = state.get("properties")
        if not isinstance(state_id, str) or not isinstance(block_id, str) or not isinstance(properties, Mapping):
            return
        names = sorted((str(key) for key in properties), key=lambda value: value.encode("utf-8"))
        expected = block_id
        if names:
            expected += "[" + ",".join(f"{name}={properties[name]}" for name in names) + "]"
        if state_id != expected:
            self.add("STATE_CANONICAL_ID_INVALID", f"{state_id}: expected {expected}")

    def _check_failure_reference(
        self,
        failure: Mapping[str, Any],
        blocks: Mapping[str, Mapping[str, Any]],
        states: Mapping[str, Mapping[str, Any]],
        variants: Mapping[str, Mapping[str, Any]],
    ) -> None:
        scope = failure.get("scope")
        block_id = failure.get("block_id")
        state_id = failure.get("state_id")
        variant_id = failure.get("variant_id")
        if scope in {"block", "state", "variant", "render"} and (
            not isinstance(block_id, str) or block_id not in blocks
        ):
            self.add("FAILURE_BLOCK_REFERENCE_INVALID", repr(block_id))
        if scope in {"state"} and (not isinstance(state_id, str) or state_id not in states):
            self.add("FAILURE_STATE_REFERENCE_INVALID", repr(state_id))
        elif scope == "state" and isinstance(block_id, str) and isinstance(state_id, str):
            state = states.get(state_id)
            if isinstance(state, Mapping) and state.get("block_id") != block_id:
                self.add("FAILURE_STATE_BLOCK_MISMATCH", repr(state_id))
        if scope in {"variant", "render"} and (
            not isinstance(variant_id, str) or variant_id not in variants
        ):
            self.add("FAILURE_VARIANT_REFERENCE_INVALID", repr(variant_id))
        elif scope in {"variant", "render"} and isinstance(block_id, str) and isinstance(variant_id, str):
            variant = variants.get(variant_id)
            if isinstance(variant, Mapping) and variant.get("block_id") != block_id:
                self.add("FAILURE_VARIANT_BLOCK_MISMATCH", repr(variant_id))
        if scope == "render" and isinstance(variant_id, str) and variants.get(variant_id, {}).get("status") != "selected":
            self.add("FAILURE_RENDER_TARGET_NOT_SELECTED", variant_id)

    def _check_registry_snapshot(self, blocks: Mapping[str, Mapping[str, Any]]) -> None:
        if self.manifest is None:
            return
        scope = self.manifest.get("scope")
        if not isinstance(scope, Mapping):
            return
        ids = sorted(blocks, key=lambda value: value.encode("utf-8"))
        expected = _sha256_prefixed("\n".join(ids).encode("utf-8"))
        if scope.get("registry_snapshot_sha256") != expected:
            self.add("REGISTRY_SNAPSHOT_HASH_MISMATCH", "scope.registry_snapshot_sha256")

    def _check_manifest_registry_count(self, blocks: Mapping[str, Mapping[str, Any]]) -> None:
        if self.manifest is None:
            return
        counts = self.manifest.get("counts")
        if not isinstance(counts, Mapping):
            return
        if counts.get("registry_blocks") != len(blocks):
            self.add("REGISTRY_COUNT_MISMATCH", "counts.registry_blocks")

    def _check_renders(self) -> None:
        variants = self.records.get("variants.jsonl", [])
        selected_render_directories: set[str] = set()
        skipped_render_directories = {
            PurePosixPath(paths[0]).parent.as_posix()
            for variant in variants
            if variant.get("status") == "skipped"
            and isinstance(variant.get("block_id"), str)
            and (paths := _render_reference_paths(variant["block_id"])) is not None
        }
        for variant in variants:
            if variant.get("status") != "selected":
                continue
            render = variant.get("render")
            if not isinstance(render, Mapping):
                continue
            variant_id = variant.get("variant_id")
            block_id = variant.get("block_id")
            paths = [render.get("preview_path"), render.get("mask_path"), render.get("render_metadata_path")]
            if not all(isinstance(path, str) for path in paths):
                continue
            expected_paths = _render_reference_paths(block_id) if isinstance(block_id, str) else None
            if expected_paths is None or tuple(paths) != expected_paths:
                self.add("RENDER_PATH_INVALID", str(variant_id))
                continue
            selected_render_directories.add(PurePosixPath(expected_paths[0]).parent.as_posix())
            preview_path, mask_path, metadata_path = (self.export_dir / str(path) for path in paths)
            relative_paths = [str(path) for path in paths]
            for relative in relative_paths:
                if relative not in self._files or not _inside(self.export_dir, self._files[relative]):
                    self.add("RENDER_FILE_MISSING", relative)
            if not all(relative in self._files for relative in relative_paths):
                continue
            preview_info = _read_png(preview_path, self)
            mask_info = _read_png(mask_path, self)
            if preview_info is not None:
                if render.get("image_sha256") != self._sha256_prefixed(preview_path):
                    self.add("PREVIEW_HASH_MISMATCH", str(variant_id))
                if preview_info[0:2] != (512, 512) or preview_info[2] != "RGBA":
                    self.add("PREVIEW_FORMAT_INVALID", str(variant_id))
                if not preview_info[3]:
                    self.add("PREVIEW_OBJECT_EMPTY", str(variant_id))
            if mask_info is not None:
                if render.get("mask_sha256") != self._sha256_prefixed(mask_path):
                    self.add("MASK_HASH_MISMATCH", str(variant_id))
                if mask_info[0:2] != (512, 512) or mask_info[2] != "RGBA":
                    self.add("MASK_FORMAT_INVALID", str(variant_id))
                if not mask_info[3]:
                    self.add("MASK_OBJECT_EMPTY", str(variant_id))
            if preview_info is not None:
                _check_image_quality(preview_info, self, str(variant_id))
            try:
                metadata = json.loads(_decode_text(self._read_bytes(metadata_path), metadata_path))
            except (UnicodeError, json.JSONDecodeError) as exc:
                self.add("RENDER_METADATA_PARSE_FAILED", f"{variant_id}: {type(exc).__name__}")
                continue
            if not isinstance(metadata, Mapping):
                self.add("RENDER_METADATA_NOT_OBJECT", str(variant_id))
                continue
            self._validate_schema("render-metadata.v1", metadata, str(paths[2]))
            if metadata.get("variant_id") != variant_id:
                self.add("RENDER_METADATA_VARIANT_MISMATCH", str(variant_id))
            if render.get("render_metadata_sha256") != _sha256_prefixed(_jcs_canonical_bytes(metadata)):
                self.add("RENDER_METADATA_HASH_MISMATCH", str(variant_id))
            self._check_render_metadata_environment(metadata, variant_id)
            if not isinstance(metadata.get("mask"), Mapping) or metadata["mask"].get("format") != "PNG-RGBA":
                self.add("MASK_METADATA_FORMAT_INVALID", str(variant_id))
            if isinstance(metadata.get("mask"), Mapping) and metadata["mask"].get("channel") != "alpha":
                self.add("MASK_METADATA_CHANNEL_INVALID", str(variant_id))

        actual_render_files = {
            relative for relative in self._files if relative.startswith("renders/")
        }
        actual_files_by_directory: dict[str, set[str]] = {}
        for relative in actual_render_files:
            directory = PurePosixPath(relative).parent.as_posix()
            actual_files_by_directory.setdefault(directory, set()).add(PurePosixPath(relative).name)
        render_directories = {
            directory for directory in self._directories if directory.startswith("renders/")
        }
        empty_leaf_directories = {
            directory
            for directory in render_directories
            if directory != "renders"
            and not any(
                other != directory and other.startswith(directory + "/")
                for other in render_directories
            )
            and directory not in actual_files_by_directory
        }
        actual_render_directories = set(actual_files_by_directory) | empty_leaf_directories
        if actual_render_directories != selected_render_directories:
            self.add(
                "RENDER_VARIANT_SET_MISMATCH",
                f"expected {sorted(selected_render_directories)!r}, got {sorted(actual_render_directories)!r}",
            )
        for directory, actual_files in actual_files_by_directory.items():
            if directory in selected_render_directories:
                if actual_files != {"preview.png", "mask.png", "render.json"}:
                    self.add("RENDER_FILE_SET_INVALID", directory)
            elif directory in skipped_render_directories:
                self.add("RENDER_SKIPPED_DIRECTORY_PRESENT", directory)

    def _check_manifest_counts_and_status(self) -> None:
        manifest = self.manifest
        if manifest is None:
            return
        counts = manifest.get("counts")
        if not isinstance(counts, Mapping):
            return
        variants = self.records.get("variants.jsonl", [])
        failures = self.records.get("failures.jsonl", [])
        actual = {
            "registry_blocks": len(self.records.get("blocks.jsonl", [])),
            "block_records": len(self.records.get("blocks.jsonl", [])),
            "state_records": len(self.records.get("states.jsonl", [])),
            "selected_variant_records": sum(record.get("status") == "selected" for record in variants),
            "skipped_variant_records": sum(record.get("status") == "skipped" for record in variants),
            "failure_records": len(failures),
            "pending_review_records": sum(record.get("review_status") == "pending" for record in failures),
        }
        for key, expected in actual.items():
            if counts.get(key) != expected:
                self.add("MANIFEST_COUNT_MISMATCH", f"{key}: expected {expected}, got {counts.get(key)!r}")
        if any(issue.code in {
            "CHECKSUM_MISMATCH",
            "CHECKSUM_FILES_MISSING",
            "CHECKSUM_FILES_EXTRA",
            "CHECKSUM_LINE_INVALID",
            "CHECKSUM_PATH_INVALID",
            "CHECKSUM_ORDER_INVALID",
            "REQUIRED_FILE_MISSING",
            "UNDECLARED_FILE",
            "UNDECLARED_RENDER_FILE",
            "MANIFEST_READ_FAILED",
            "JSONL_READ_FAILED",
            "RENDER_FILE_MISSING",
            "RENDER_PATH_INVALID",
            "RENDER_METADATA_PARSE_FAILED",
            "RENDER_FILE_SET_INVALID",
            "RENDER_SKIPPED_DIRECTORY_PRESENT",
            "RENDER_VARIANT_SET_MISMATCH",
            "VARIANT_ID_BLOCK_MISMATCH",
            "EXPORT_DIR_NAME_INVALID",
            "EXPORT_DIR_NAME_MISMATCH",
            "EXPORT_DIR_STAGING",
            "BACKGROUND_ONLY_RENDER",
            "OBJECT_TOO_SMALL",
            "OBJECT_OFF_CANVAS",
            "MISSING_TEXTURE",
            "REGISTRY_COUNT_MISMATCH",
            "INVENTORY_SYMLINK_REJECTED",
            "INVENTORY_HARDLINK_REJECTED",
            "STATE_BLOCK_REFERENCE_INVALID",
            "VARIANT_BLOCK_REFERENCE_INVALID",
            "FAILURE_STATE_BLOCK_MISMATCH",
            "FAILURE_VARIANT_BLOCK_MISMATCH",
        } for issue in self.issues):
            expected_status = "failed"
        elif any(issue.code in {"BLOCK_ID_DUPLICATE", "STATE_ID_DUPLICATE", "VARIANT_ID_DUPLICATE", "SCHEMA_VALIDATION_FAILED"} for issue in self.issues):
            expected_status = "failed"
        elif actual["pending_review_records"] > 0:
            expected_status = "needs_review"
        else:
            expected_status = "succeeded"
        if manifest.get("status") != expected_status:
            self.add("MANIFEST_STATUS_MISMATCH", f"expected {expected_status}, got {manifest.get('status')!r}")
        platform = manifest.get("platform")
        if isinstance(platform, Mapping) and platform.get("os_name") not in {"Windows", "Linux"}:
            self.add("PLATFORM_UNSUPPORTED", repr(platform.get("os_name")))
        if isinstance(platform, Mapping) and platform.get("architecture") != "x86_64":
            self.add("PLATFORM_ARCHITECTURE_UNSUPPORTED", repr(platform.get("architecture")))

    def _check_render_metadata_environment(self, metadata: Mapping[str, Any], variant_id: Any) -> None:
        tint_sensitive = metadata.get("tint_sensitive")
        baseline = metadata.get("baseline_biome")
        if tint_sensitive is True and baseline != "minecraft:plains":
            self.add("TINT_BASELINE_INVALID", str(variant_id))
        if tint_sensitive is False and baseline is not None:
            self.add("TINT_BASELINE_INVALID", str(variant_id))


def _decode_text(raw: bytes, path: Path) -> str:
    if raw.startswith(b"\xef\xbb\xbf"):
        raise UnicodeError(f"UTF-8 BOM: {path}")
    return raw.decode("utf-8")


def _require_lf_text(raw: bytes, path: Path, validator: Validator) -> None:
    if b"\r" in raw:
        validator.add("TEXT_NOT_LF", path.name)
    if raw and not raw.endswith(b"\n"):
        validator.add("TEXT_MISSING_FINAL_LF", path.name)


def _sha256_prefixed(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _jcs_canonical_bytes(value: Any) -> bytes:
    return _jcs_canonical(value).encode("utf-8")


def _jcs_canonical(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _jcs_quote(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("JCS rejects non-finite number")
        if value == 0:
            return "0"
        return _jcs_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_jcs_canonical(item) for item in value) + "]"
    if isinstance(value, Mapping):
        keys = sorted(value, key=_utf16_key)
        return "{" + ",".join(
            _jcs_canonical(str(key)) + ":" + _jcs_canonical(value[key]) for key in keys
        ) + "}"
    raise TypeError(f"unsupported JCS value: {type(value).__name__}")


def _utf16_key(value: str) -> bytes:
    return value.encode("utf-16-be", "surrogatepass")


def _jcs_quote(value: str) -> str:
    result: list[str] = ['"']
    index = 0
    while index < len(value):
        character = ord(value[index])
        if 0xD800 <= character <= 0xDBFF:
            if index + 1 >= len(value) or not 0xDC00 <= ord(value[index + 1]) <= 0xDFFF:
                raise ValueError("JCS cannot encode an unpaired high surrogate")
            character = 0x10000 + ((character - 0xD800) << 10) + (ord(value[index + 1]) - 0xDC00)
            index += 1
        elif 0xDC00 <= character <= 0xDFFF:
            raise ValueError("JCS cannot encode an unpaired low surrogate")
        if character == 0x22:
            result.append('\\"')
        elif character == 0x5C:
            result.append('\\\\')
        elif character == 0x08:
            result.append("\\b")
        elif character == 0x0C:
            result.append("\\f")
        elif character == 0x0A:
            result.append("\\n")
        elif character == 0x0D:
            result.append("\\r")
        elif character == 0x09:
            result.append("\\t")
        elif character < 0x20:
            result.append(f"\\u{character:04x}")
        else:
            result.append(chr(character))
        index += 1
    result.append('"')
    return "".join(result)


def _jcs_number(value: float) -> str:
    if abs(value) == 5e-324:
        return "-5e-324" if value < 0 else "5e-324"
    text = repr(value).lower()
    sign = ""
    if text.startswith("-"):
        sign, text = "-", text[1:]
    if "e" in text:
        significand, exponent_text = text.split("e", 1)
        exponent = int(exponent_text)
    else:
        significand, exponent = text, 0
    if "." in significand:
        whole, fraction = significand.split(".", 1)
        digits = whole + fraction
        decimal_position = len(whole) + exponent
        while digits.endswith("0"):
            digits = digits[:-1]
    else:
        digits = significand
        decimal_position = len(digits) + exponent
    absolute = abs(value)
    if 1e-6 <= absolute < 1e21:
        if decimal_position <= 0:
            result = "0." + "0" * (-decimal_position) + digits
        elif decimal_position >= len(digits):
            result = digits + "0" * (decimal_position - len(digits))
        else:
            result = digits[:decimal_position] + "." + digits[decimal_position:]
        if "." in result:
            result = result.rstrip("0").rstrip(".")
        return sign + result
    scientific_exponent = decimal_position - 1
    result = digits[0]
    if len(digits) > 1:
        result += "." + digits[1:]
    return sign + result + "e" + ("+" if scientific_exponent >= 0 else "") + str(scientific_exponent)


def _safe_relative_path(value: str) -> bool:
    if not value or value.startswith(("/", "\\")) or "\\" in value:
        return False
    parts = value.split("/")
    if any(not part or part in {".", ".."} or not _safe_render_segment(part) for part in parts):
        return False
    path = PurePosixPath(value)
    return not path.is_absolute()


def _safe_render_segment(value: str) -> bool:
    if (
        not value
        or value in {".", ".."}
        or value.endswith((".", " "))
        or ":" in value
        or any(ord(character) < 0x20 for character in value)
    ):
        return False
    device_name = value.split(".", 1)[0].casefold()
    return device_name not in WINDOWS_DEVICE_NAMES


def _render_reference_paths(block_id: str) -> tuple[str, str, str] | None:
    if not BLOCK_ID_RE.fullmatch(block_id):
        return None
    namespace, separator, block_path = block_id.partition(":")
    if namespace != "minecraft" or not separator:
        return None
    segments = block_path.split("/")
    if not all(_safe_render_segment(segment) for segment in segments):
        return None
    prefix = "renders/minecraft/" + "/".join(segments)
    paths = prefix + "/preview.png", prefix + "/mask.png", prefix + "/render.json"
    return paths if all(_safe_relative_path(path) for path in paths) else None


def _safe_render_reference(value: str, block_id: str) -> bool:
    expected = _render_reference_paths(block_id)
    return expected is not None and value in expected


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _relative_or_text(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _read_png(path: Path, validator: Validator) -> _PngAnalysis | None:
    if path in validator._png_cache:
        return validator._png_cache[path]
    try:
        raw = validator._read_bytes(path)
        if path not in validator._digest_cache:
            validator._digest_cache[path] = hashlib.sha256(raw).hexdigest()
        if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            validator.add("PNG_SIGNATURE_INVALID", path.name)
            validator._png_cache[path] = None
            return None
        width, height, bit_depth, color_type, interlace, idat = _parse_png(raw)
        if bit_depth != 8 or color_type != 6 or interlace != 0:
            validator.add("PNG_RGBA_REQUIRED", path.name)
            analysis = _PngAnalysis(
                width, height, "unknown", False, None, 0, 0, 0, 0,
                (0, 0, 0, 0), True, False, False, False,
            )
            validator._png_cache[path] = analysis
            return analysis
        decoded = zlib.decompress(idat)
        row_bytes = width * 4
        expected_length = height * (row_bytes + 1)
        if len(decoded) != expected_length:
            raise ValueError("decoded scanline length mismatch")
        analysis = _analyze_png_pixels(decoded, width, height, row_bytes)
        validator._png_cache[path] = analysis
        return analysis
    except (OSError, ValueError, zlib.error, IndexError) as exc:
        validator.add("PNG_DECODE_FAILED", f"{path.name}: {type(exc).__name__}")
        validator._png_cache[path] = None
        return None
    finally:
        validator._bytes_cache.pop(path, None)


def _analyze_png_pixels(decoded: bytes, width: int, height: int, row_bytes: int) -> _PngAnalysis:
    previous = bytearray(row_bytes)
    offset = 0
    alpha_bounds: tuple[int, int, int, int] | None = None
    nontransparent = 0
    background_pixels = 0
    magenta = 0
    near_black = 0
    quadrant = [0, 0, 0, 0]
    half_width = width // 2
    half_height = height // 2
    for y in range(height):
        filter_type = decoded[offset]
        offset += 1
        row = _unfilter_png_row(filter_type, decoded[offset : offset + row_bytes], previous, 4)
        offset += row_bytes
        for x in range(width):
            red, green, blue, alpha = row[x * 4 : x * 4 + 4]
            if alpha == 0:
                background_pixels += 1
                continue
            nontransparent += 1
            quadrant[(2 if y >= half_height else 0) + (1 if x >= half_width else 0)] += 1
            if alpha_bounds is None:
                alpha_bounds = (x, y, x, y)
            else:
                alpha_bounds = (
                    min(alpha_bounds[0], x), min(alpha_bounds[1], y),
                    max(alpha_bounds[2], x), max(alpha_bounds[3], y),
                )
            if red >= 180 and blue >= 180 and green <= 80:
                magenta += 1
            if red <= 64 and green <= 64 and blue <= 64:
                near_black += 1
        previous = row
    off_canvas = bool(
        alpha_bounds
        and (
            alpha_bounds[0] == 0
            or alpha_bounds[1] == 0
            or alpha_bounds[2] == width - 1
            or alpha_bounds[3] == height - 1
        )
    )
    minimum_object_pixels = max(1, (width * height) // 256)
    object_too_small = nontransparent < minimum_object_pixels
    missing_texture = (
        nontransparent >= 64
        and magenta >= 64
        and near_black >= 64
        and magenta / nontransparent >= 0.02
        and near_black / nontransparent >= 0.02
    )
    return _PngAnalysis(
        width,
        height,
        "RGBA",
        nontransparent > 0,
        alpha_bounds,
        nontransparent,
        background_pixels,
        magenta,
        near_black,
        (quadrant[0], quadrant[1], quadrant[2], quadrant[3]),
        nontransparent == 0,
        object_too_small,
        off_canvas,
        missing_texture,
    )


def _parse_png(raw: bytes) -> tuple[int, int, int, int, int, bytes]:
    if len(raw) < 33:
        raise ValueError("PNG is truncated")
    offset = 8
    header: tuple[int, int, int, int, int] | None = None
    idat_parts: list[bytes] = []
    while offset + 12 <= len(raw):
        length = int.from_bytes(raw[offset : offset + 4], "big")
        chunk_start = offset + 8
        chunk_end = chunk_start + length
        if chunk_end + 4 > len(raw):
            raise ValueError("PNG chunk is truncated")
        chunk_type = raw[offset + 4 : offset + 8]
        chunk_data = raw[chunk_start:chunk_end]
        if chunk_type == b"IHDR":
            if len(chunk_data) != 13:
                raise ValueError("invalid IHDR")
            header = (
                int.from_bytes(chunk_data[0:4], "big"),
                int.from_bytes(chunk_data[4:8], "big"),
                chunk_data[8],
                chunk_data[9],
                chunk_data[12],
            )
        elif chunk_type == b"IDAT":
            idat_parts.append(chunk_data)
        elif chunk_type == b"IEND":
            break
        offset = chunk_end + 4
    if header is None or not idat_parts:
        raise ValueError("PNG is missing IHDR or IDAT")
    return (*header, b"".join(idat_parts))


def _unfilter_png_row(filter_type: int, filtered: bytes, previous: bytearray, bytes_per_pixel: int) -> bytearray:
    row = bytearray(filtered)
    if filter_type == 0:
        return row
    if filter_type not in {1, 2, 3, 4}:
        raise ValueError(f"unsupported PNG filter {filter_type}")
    for index in range(len(row)):
        left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        above = previous[index]
        upper_left = previous[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
        if filter_type == 1:
            predictor = left
        elif filter_type == 2:
            predictor = above
        elif filter_type == 3:
            predictor = (left + above) // 2
        else:
            p = left + above - upper_left
            pa = abs(p - left)
            pb = abs(p - above)
            pc = abs(p - upper_left)
            predictor = left if pa <= pb and pa <= pc else above if pb <= pc else upper_left
        row[index] = (row[index] + predictor) & 0xFF
    return row


def _check_image_quality(analysis: _PngAnalysis, validator: Validator, variant_id: str) -> None:
    if analysis.background_only:
        validator.add("BACKGROUND_ONLY_RENDER", variant_id)
    if analysis.object_too_small:
        validator.add("OBJECT_TOO_SMALL", variant_id)
    if analysis.off_canvas:
        validator.add("OBJECT_OFF_CANVAS", variant_id)
    if analysis.missing_texture:
        validator.add("MISSING_TEXTURE", variant_id)


def validate_export(repo_root: str | Path, export_dir: str | Path) -> dict[str, Any]:
    """Return a report for one export package; never writes to the package."""

    return Validator(Path(repo_root), Path(export_dir)).run()


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True, help="repository root containing schemas/")
    parser.add_argument("--export-dir", type=Path, required=True, help="one local R1 export package")
    parser.add_argument("--report", type=Path, help="optional report destination; never inside export by default")
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    report = validate_export(args.repo_root, args.export_dir)
    if args.report is not None:
        try:
            args.report.resolve().relative_to(args.export_dir.resolve())
        except ValueError:
            pass
        else:
            print("--report must not be inside --export-dir", file=sys.stderr)
            return 2
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if report["status"] == "passed":
        print("R1 export validation passed")
        return 0
    print(f"R1 export validation failed: {len(report['issues'])} issue(s)", file=sys.stderr)
    for issue in report["issues"][:20]:
        print(f"- {issue['code']}: {issue['detail']}", file=sys.stderr)
    if len(report["issues"]) > 20:
        print(f"- ... {len(report['issues']) - 20} more issue(s)", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
