"""Deterministic, read-only MCP queries over the pointer-selected release."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .features import DecodedPng
from .mcp_release import MCPReleaseError, MCPReleaseResolver, MCPVersionInputError, ReleaseHandle, MCP_VERSION_RE
from .r3 import ContactSheet, _paint_label, _resize_nearest, encode_rgba_png, sha256_bytes
from .schema import RecordSchemaError, validate_record


BLOCK_ID_RE = re.compile(r"^minecraft:[a-z0-9_./-]+$")
WEIGHTS = {
    "shape": 0.35,
    "color": 0.30,
    "use": 0.15,
    "name_synonym": 0.10,
    "style": 0.05,
    "behavior": 0.05,
}
RANKING_VERSION = "search-ranking.v1"
OFFICIAL_DISCLAIMER = "NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT."
COLOR_LAB_TARGETS = {
    "red": (53.24, 80.09, 67.20), "红": (53.24, 80.09, 67.20), "红色": (53.24, 80.09, 67.20),
    "yellow": (80.0, 0.0, 93.0), "黄": (80.0, 0.0, 93.0), "黄色": (80.0, 0.0, 93.0),
    "blue": (32.3, 79.2, -107.9), "蓝": (32.3, 79.2, -107.9), "蓝色": (32.3, 79.2, -107.9),
    "green": (46.2, -51.7, 49.9), "绿": (46.2, -51.7, 49.9), "绿色": (46.2, -51.7, 49.9),
    "white": (100.0, 0.0, 0.0), "black": (0.0, 0.0, 0.0), "gray": (53.6, 0.0, 0.0), "grey": (53.6, 0.0, 0.0),
}
COLOR_OKLAB_TARGETS = {
    "red": (0.628, 0.225, 0.126), "红": (0.628, 0.225, 0.126), "红色": (0.628, 0.225, 0.126),
    "yellow": (0.968, -0.071, 0.199), "黄": (0.968, -0.071, 0.199), "黄色": (0.968, -0.071, 0.199),
    "blue": (0.452, -0.032, -0.312), "蓝": (0.452, -0.032, -0.312), "蓝色": (0.452, -0.032, -0.312),
    "green": (0.866, -0.234, 0.179), "绿": (0.866, -0.234, 0.179), "绿色": (0.866, -0.234, 0.179),
    "white": (1.0, 0.0, 0.0), "black": (0.0, 0.0, 0.0), "gray": (0.6, 0.0, 0.0), "grey": (0.6, 0.0, 0.0),
}
COLOR_TERMS = frozenset(COLOR_LAB_TARGETS)
MATERIAL_TERMS = frozenset({"stone", "wood", "brick", "glass", "metal", "石", "木", "砖", "玻璃"})
USE_TERMS = frozenset({"roof", "eave", "wall", "floor", "trim", "屋檐", "屋顶", "墙", "地板"})
STYLE_TERMS = frozenset({"modern", "classic", "simple", "rustic", "现代", "古典", "简单"})
SHAPE_TERMS = frozenset({
    "button_like", "cross_plane", "fence_like", "full_cube", "horizontal_thin_sheet", "irregular",
    "liquid_surface", "pane_like", "partial_cube", "post_like", "rod_like", "slab_like", "stair_like",
    "vertical_thin_sheet", "wall_like", "carpet", "stair", "slab", "pane", "wall", "fence",
})


class MCPInputError(ValueError):
    """Input shape error, mapped by the protocol layer to -32602."""


class MCPProtocolError(MCPInputError):
    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class MCPToolResult(dict[str, Any]):
    """A schema-valid envelope with ordered, non-persistent image bytes."""

    def __init__(self, envelope: Mapping[str, Any], *, images: Sequence[bytes] = (), is_error: bool = False) -> None:
        super().__init__(envelope)
        self.images = tuple(bytes(value) for value in images)
        self.is_error = bool(is_error)

    @property
    def envelope(self) -> dict[str, Any]:
        return dict(self)

    @property
    def image_bytes(self) -> tuple[bytes, ...]:
        return self.images


@dataclass(frozen=True, slots=True)
class _KeywordIntent:
    keywords: tuple[str, ...]
    soft: Mapping[str, tuple[str, ...]]


@dataclass(slots=True)
class _Snapshot:
    blocks: dict[str, dict[str, Any]]
    states: dict[str, dict[str, Any]]
    variants: dict[str, dict[str, Any]]
    features: dict[str, dict[str, Any]]
    annotations: dict[str, dict[str, Any]]
    manual: dict[str, Any]


@dataclass(slots=True)
class _RequestResources:
    preview_cache: dict[str, tuple[bytes, DecodedPng]]


def _request_id(value: str | None, counter: int) -> str:
    if value is not None:
        if not isinstance(value, str) or not re.fullmatch(r"^[A-Za-z][A-Za-z0-9_-]{0,127}$", value):
            raise MCPInputError("request_id must be an opaque identifier")
        return value
    return f"mcp_{counter}"


def _validate_version_input(arguments: Mapping[str, Any]) -> str | None:
    value = arguments.get("minecraft_version")
    if value is None:
        return None
    if not isinstance(value, str) or MCP_VERSION_RE.fullmatch(value) is None:
        raise MCPInputError("minecraft_version has an invalid format")
    return value


def _validate_object(arguments: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(arguments, Mapping) or any(not isinstance(key, str) for key in arguments):
        raise MCPInputError("tool arguments must be an object")
    value = dict(arguments)
    if set(value) - allowed:
        raise MCPInputError("tool arguments contain an unknown field")
    return value


def _error_details(error: MCPReleaseError, *, invalid_block_ids: Sequence[str] = ()) -> dict[str, Any]:
    details = {
        "release_id": error.details.get("release_id"),
        "available_versions": list(error.available_versions),
        "invalid_block_ids": list(invalid_block_ids),
        "field_errors": [],
        "provider_error_code": None,
        "integrity_component": error.details.get("integrity_component"),
    }
    details.update({key: value for key, value in error.details.items() if key in details})
    return details


def _error_result(error: MCPReleaseError, request_id: str, *, invalid_block_ids: Sequence[str] = ()) -> MCPToolResult:
    envelope = {
        "schema_version": "mcp-error.v1",
        "request_id": request_id,
        "error_code": error.code if error.code in {
            "DATA_ROOT_INVALID", "CURRENT_POINTER_MISSING", "CURRENT_POINTER_INVALID", "VERSION_NOT_AVAILABLE", "RELEASE_NOT_FOUND", "RELEASE_NOT_BUILT",
            "INDEX_INFO_UNAVAILABLE", "INDEX_OPEN_FAILED", "BLOCK_NOT_FOUND", "IMAGE_READ_FAILED", "IMAGE_MAPPING_INVALID",
            "READ_ONLY_VIOLATION", "MCP_INTERNAL_ERROR",
        } else "MCP_INTERNAL_ERROR",
        "message": error.message[:500],
        "retryable": False,
        "minecraft_version": error.minecraft_version,
        "details": _error_details(error, invalid_block_ids=invalid_block_ids),
        "warnings": [],
        "images": [],
    }
    try:
        validate_record("mcp-error.v1", envelope)
    except RecordSchemaError as exc:  # pragma: no cover
        raise RuntimeError("mcp-error.v1 construction failed") from exc
    return MCPToolResult(envelope, is_error=True)


def _validate_output(schema_id: str, envelope: dict[str, Any]) -> MCPToolResult:
    validate_record(schema_id, envelope)
    return MCPToolResult(envelope)


def _normalized(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.casefold().split())


def _keyword_tokens(keywords: Sequence[str]) -> tuple[str, ...]:
    tokens: list[str] = []
    for keyword in keywords:
        tokens.extend(_normalized(keyword).split())
    return tuple(tokens)


def _keyword_intent(keywords: Sequence[str]) -> _KeywordIntent:
    tokens = _keyword_tokens(keywords)
    dimensions = {
        "colors": tuple(token for token in tokens if token in COLOR_TERMS),
        "materials": tuple(token for token in tokens if token in MATERIAL_TERMS),
        "uses": tuple(token for token in tokens if token in USE_TERMS),
        "styles": tuple(token for token in tokens if token in STYLE_TERMS),
        "shape_terms": tuple(token for token in tokens if token in SHAPE_TERMS),
        "avoid_for": (),
        "keywords": tokens,
    }
    return _KeywordIntent(tokens, dimensions)


def _contains_any(texts: Sequence[str], terms: Sequence[str]) -> float:
    if not terms:
        return 0.0
    haystack = " ".join(_normalized(item) for item in texts)
    return 1.0 if any(_normalized(term) in haystack for term in terms) else 0.0


def _feature_color_score(feature: Mapping[str, Any], terms: Sequence[str]) -> float:
    lab = feature.get("lab")
    oklab = feature.get("oklab")
    if not isinstance(lab, list) or len(lab) != 3 or not isinstance(oklab, list) or len(oklab) != 3:
        return 0.0
    targets = [(COLOR_LAB_TARGETS[key], COLOR_OKLAB_TARGETS[key]) for term in terms for key in COLOR_LAB_TARGETS if key in _normalized(term)]
    if not targets:
        return 0.0
    distance = min(
        0.5 * math.sqrt(sum((float(lab[index]) - target_lab[index]) ** 2 for index in range(3))) / 181.0
        + 0.5 * math.sqrt(sum((float(oklab[index]) - target_oklab[index]) ** 2 for index in range(3))) / math.sqrt(3.0)
        for target_lab, target_oklab in targets
    )
    return round(max(0.0, min(1.0, 1.0 - distance)), 8)


def _behavior(variant: Mapping[str, Any], state: Mapping[str, Any]) -> Mapping[str, Any]:
    facts = variant.get("machine_facts")
    by_state = facts.get("behavior_by_state") if isinstance(facts, Mapping) else None
    state_id = variant.get("canonical_state_id")
    value = by_state.get(state_id) if isinstance(by_state, Mapping) else None
    if isinstance(value, Mapping):
        return value
    value = state.get("behavior")
    return value if isinstance(value, Mapping) else {}


def _semantic(annotation: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(annotation, Mapping):
        return {}
    return {key: value for key, value in annotation.items() if key in {"synonyms_zh", "synonyms_en", "summary_zh", "summary_en", "color_terms", "shape_terms", "material_impressions", "building_roles", "style_tags", "avoid_for", "confidence"}}


def deterministic_score(matches: Mapping[str, float]) -> tuple[float, dict[str, float]]:
    present = [key for key in WEIGHTS if key in matches]
    denominator = sum(WEIGHTS[key] for key in present)
    breakdown = {key: round(max(0.0, min(1.0, float(matches.get(key, 0.0)))), 8) for key in WEIGHTS}
    score = 0.0 if denominator == 0 else sum(breakdown[key] * WEIGHTS[key] for key in present) / denominator
    return round(max(0.0, min(1.0, score)), 8), breakdown


def _image_id(payload: bytes, variant_id: str, prefix: str = "img") -> str:
    del variant_id
    return f"{prefix}_{hashlib.sha256(payload).hexdigest()[:24]}"


def _make_cached_contact_sheet(images: Sequence[tuple[str, bytes, DecodedPng]], *, columns: int = 4) -> ContactSheet:
    if not 1 <= len(images) <= 16:
        raise ValueError("contact sheets contain 1-16 images")
    columns = max(1, min(columns, len(images)))
    rows = (len(images) + columns - 1) // columns
    width, height = columns * 512, rows * 512
    pixels = bytearray(width * height * 4)
    tiles: list[dict[str, Any]] = []
    for index, (variant_id, _payload, decoded) in enumerate(images):
        card = _resize_nearest(decoded)
        row, column = divmod(index, columns)
        x0, y0 = column * 512, row * 512
        for y in range(512):
            target = ((y0 + y) * width + x0) * 4
            source = y * 512 * 4
            pixels[target : target + 512 * 4] = card[source : source + 512 * 4]
        tile_id = f"T{index + 1:02d}"
        _paint_label(pixels, width, height, x0 + 12, y0 + 470, tile_id)
        tiles.append({"tile_id": tile_id, "variant_id": variant_id, "row": row, "column": column})
    return ContactSheet(encode_rgba_png(width, height, bytes(pixels)), tuple(tiles))


class MCPQueryService:
    """Four-tool query surface with no online provider boundary."""

    def __init__(self, data_root: str | Path, *, repo_root: Path | None = None) -> None:
        self.resolver = MCPReleaseResolver(data_root, repo_root=repo_root)
        self._counter = 0
        self._lock = threading.RLock()
        self._snapshots: dict[tuple[str, str, str], _Snapshot] = {}

    def _next_request_id(self, value: str | None) -> str:
        with self._lock:
            self._counter += 1
            return _request_id(value, self._counter)

    def _snapshot(self, handle: ReleaseHandle) -> _Snapshot:
        key = (handle.minecraft_version, handle.release_id, str(handle.release_path))
        with self._lock:
            cached = self._snapshots.get(key)
        if cached is not None:
            return cached
        blocks: dict[str, dict[str, Any]] = {}
        states: dict[str, dict[str, Any]] = {}
        variants: dict[str, dict[str, Any]] = {}
        features: dict[str, dict[str, Any]] = {}
        annotations: dict[str, dict[str, Any]] = {}
        try:
            for row in handle.execute("SELECT block_id, minecraft_version, default_state_id, record_json FROM blocks ORDER BY block_id"):
                blocks[str(row["block_id"])] = json.loads(row["record_json"])
            for row in handle.execute("SELECT state_id, block_id, record_json FROM states ORDER BY state_id"):
                states[str(row["state_id"])] = json.loads(row["record_json"])
            for row in handle.execute("SELECT variant_id, block_id, record_json, feature_json FROM visual_variants ORDER BY variant_id"):
                variants[str(row["variant_id"])] = json.loads(row["record_json"])
                features[str(row["variant_id"])] = json.loads(row["feature_json"])
            for row in handle.execute("SELECT variant_id, semantic_json FROM annotations ORDER BY variant_id"):
                value = json.loads(row["semantic_json"])
                if not isinstance(value, dict):
                    raise ValueError("annotation projection mismatch")
                annotations[str(row["variant_id"])] = value
            manual = json.loads(handle.read_bytes("manual-overrides.json").decode("utf-8"))
            if not isinstance(manual, dict):
                raise ValueError("manual record package is invalid")
        except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError, RecordSchemaError) as exc:
            raise MCPReleaseError("INDEX_INFO_UNAVAILABLE", "The release records could not be read.", minecraft_version=handle.minecraft_version, details={"integrity_component": "index"}) from exc
        snapshot = _Snapshot(blocks, states, variants, features, annotations, manual)
        with self._lock:
            return self._snapshots.setdefault(key, snapshot)

    def index_info(self, arguments: Mapping[str, Any] | None = None, *, request_id: str | None = None) -> MCPToolResult:
        request = self._next_request_id(request_id)
        try:
            args = _validate_object(arguments or {}, {"minecraft_version"})
            version = _validate_version_input(args)
            with self.resolver.resolve(version) as handle:
                snapshot = self._snapshot(handle)
                quality_hash = handle.manifest.get("quality_report_sha256")
                built_at = handle.release.get("built_at")
                if not isinstance(quality_hash, str) or not isinstance(built_at, str):
                    raise MCPReleaseError("INDEX_INFO_UNAVAILABLE", "Release metadata needed for index_info is unavailable.", minecraft_version=handle.minecraft_version)
                data = {
                    "product": "Blockpedia",
                    "official_disclaimer": OFFICIAL_DISCLAIMER,
                    "release_id": handle.release_id,
                    "built_at": built_at,
                    "counts": {"blocks": len(snapshot.blocks), "visual_variants": len(snapshot.variants), "audited_skips": len(snapshot.manual.get("skip_reviews", [])) if isinstance(snapshot.manual.get("skip_reviews", []), list) else 0},
                    "quality_gate": {"passed": True, "quality_report_sha256": quality_hash},
                }
                envelope = {"schema_version": "mcp-index-info-output.v1", "request_id": request, "minecraft_version": handle.minecraft_version, "resolved_release_id": handle.release_id, "manifest_sha256": handle.manifest_sha256, "warnings": [], "data": data}
                return _validate_output("mcp-index-info-output.v1", envelope)
        except MCPInputError:
            raise
        except MCPVersionInputError as exc:
            raise MCPInputError(str(exc)) from exc
        except MCPReleaseError as exc:
            return _error_result(exc, request)

    @staticmethod
    def _validate_keywords(value: Any) -> tuple[list[str], str]:
        if not isinstance(value, list) or not 1 <= len(value) <= 16:
            raise MCPInputError("keywords must contain 1-16 strings")
        trimmed: list[str] = []
        for item in value:
            if not isinstance(item, str) or not 1 <= len(item) <= 64:
                raise MCPInputError("each keyword must contain 1-64 Unicode characters")
            normalized = item.strip()
            if not 1 <= len(normalized) <= 64:
                raise MCPInputError("each keyword must contain 1-64 non-whitespace characters")
            trimmed.append(normalized)
        if len(trimmed) != len(set(trimmed)):
            raise MCPInputError("keywords must not contain trimmed duplicates")
        return trimmed, " ".join(trimmed)

    def search_blocks(self, arguments: Mapping[str, Any], *, request_id: str | None = None) -> MCPToolResult:
        resources = _RequestResources(preview_cache={})
        request = self._next_request_id(request_id)
        try:
            args = _validate_object(arguments, {"minecraft_version", "keywords", "limit"})
            version = _validate_version_input(args)
            keywords, joined_query = self._validate_keywords(args.get("keywords"))
            limit = args.get("limit", 8)
            if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 12:
                raise MCPInputError("limit must be an integer from 1 to 12")
            intent = _keyword_intent(keywords)
            with self.resolver.resolve(version) as handle:
                snapshot = self._snapshot(handle)
                rows = self._eligible_rows(snapshot)
                recalled = self._recall(handle, rows, intent.keywords)
                ranked = self._rank_rows(recalled, snapshot, intent)
                selected = ranked[:24][:limit]
                candidates = self._candidate_dicts(selected, snapshot, intent)
                if not selected:
                    data = {"search_id": self._search_id(handle, joined_query), "query": joined_query, "hard_filters": [], "exclusion_summary": self._exclusions(snapshot, rows, recalled), "candidates": [], "contact_sheet": {"image_id": None, "tile_mapping": []}, "images": [], "reranked_by_llm": False}
                    envelope = {"schema_version": "mcp-search-blocks-output.v1", "request_id": request, "minecraft_version": handle.minecraft_version, "resolved_release_id": handle.release_id, "manifest_sha256": handle.manifest_sha256, "warnings": [], "data": data}
                    return _validate_output("mcp-search-blocks-output.v1", envelope)
                _sheet, image_meta, image_bytes = self._search_sheet(handle, candidates, snapshot, resources)
                data = {"search_id": self._search_id(handle, joined_query), "query": joined_query, "hard_filters": [], "exclusion_summary": self._exclusions(snapshot, rows, recalled), "candidates": candidates, "contact_sheet": {"image_id": image_meta["image_id"], "tile_mapping": image_meta["mapping_tiles"]}, "images": [image_meta["image"]], "reranked_by_llm": False}
                envelope = {"schema_version": "mcp-search-blocks-output.v1", "request_id": request, "minecraft_version": handle.minecraft_version, "resolved_release_id": handle.release_id, "manifest_sha256": handle.manifest_sha256, "warnings": [], "data": data}
                return MCPToolResult(_validate_output("mcp-search-blocks-output.v1", envelope), images=(image_bytes,))
        except MCPInputError:
            raise
        except MCPVersionInputError as exc:
            raise MCPInputError(str(exc)) from exc
        except MCPReleaseError as exc:
            return _error_result(exc, request)

    def get_block_details(self, arguments: Mapping[str, Any], *, request_id: str | None = None) -> MCPToolResult:
        request = self._next_request_id(request_id)
        resources = _RequestResources(preview_cache={})
        try:
            args = _validate_object(arguments, {"minecraft_version", "block_id"})
            version = _validate_version_input(args)
            block_id = args.get("block_id")
            if not isinstance(block_id, str) or BLOCK_ID_RE.fullmatch(block_id) is None:
                raise MCPInputError("block_id has an invalid format")
            with self.resolver.resolve(version) as handle:
                snapshot = self._snapshot(handle)
                if block_id not in snapshot.blocks:
                    return _error_result(MCPReleaseError("BLOCK_NOT_FOUND", "The requested block is not in this release.", minecraft_version=handle.minecraft_version), request, invalid_block_ids=[block_id])
                data, images = self._details_data(handle, snapshot, block_id, resources)
                envelope = {"schema_version": "mcp-block-details-output.v1", "request_id": request, "minecraft_version": handle.minecraft_version, "resolved_release_id": handle.release_id, "manifest_sha256": handle.manifest_sha256, "warnings": [], "data": data}
                return MCPToolResult(_validate_output("mcp-block-details-output.v1", envelope), images=images)
        except MCPInputError:
            raise
        except MCPVersionInputError as exc:
            raise MCPInputError(str(exc)) from exc
        except MCPReleaseError as exc:
            return _error_result(exc, request)

    def compare_blocks(self, arguments: Mapping[str, Any], *, request_id: str | None = None) -> MCPToolResult:
        request = self._next_request_id(request_id)
        resources = _RequestResources(preview_cache={})
        try:
            args = _validate_object(arguments, {"minecraft_version", "block_ids", "context", "compare_states"})
            version = _validate_version_input(args)
            block_ids = args.get("block_ids")
            if not isinstance(block_ids, list) or not 2 <= len(block_ids) <= 6 or any(not isinstance(value, str) or BLOCK_ID_RE.fullmatch(value) is None for value in block_ids) or len(set(block_ids)) != len(block_ids):
                raise MCPInputError("block_ids must contain 2-6 unique valid block IDs")
            context = args.get("context", "")
            if not isinstance(context, str) or len(context) > 1000:
                raise MCPInputError("context must be a string of at most 1000 characters")
            compare_states = args.get("compare_states", False)
            if not isinstance(compare_states, bool):
                raise MCPInputError("compare_states must be boolean")
            with self.resolver.resolve(version) as handle:
                snapshot = self._snapshot(handle)
                invalid = [value for value in block_ids if value not in snapshot.blocks]
                if invalid:
                    return _error_result(MCPReleaseError("BLOCK_NOT_FOUND", "One or more requested blocks are not in this release.", minecraft_version=handle.minecraft_version), request, invalid_block_ids=invalid)
                data, images = self._compare_data(handle, snapshot, block_ids, resources)
                envelope = {"schema_version": "mcp-compare-blocks-output.v1", "request_id": request, "minecraft_version": handle.minecraft_version, "resolved_release_id": handle.release_id, "manifest_sha256": handle.manifest_sha256, "warnings": [], "data": data}
                return MCPToolResult(_validate_output("mcp-compare-blocks-output.v1", envelope), images=images)
        except MCPInputError:
            raise
        except MCPVersionInputError as exc:
            raise MCPInputError(str(exc)) from exc
        except MCPReleaseError as exc:
            return _error_result(exc, request)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None, *, request_id: str | None = None) -> MCPToolResult:
        if name == "index_info":
            return self.index_info(arguments, request_id=request_id)
        if name == "search_blocks":
            return self.search_blocks(arguments or {}, request_id=request_id)
        if name == "get_block_details":
            return self.get_block_details(arguments or {}, request_id=request_id)
        if name == "compare_blocks":
            return self.compare_blocks(arguments or {}, request_id=request_id)
        raise MCPProtocolError(-32602, "Unknown tool name")

    call = call_tool

    @staticmethod
    def _search_id(handle: ReleaseHandle, query: str) -> str:
        return "search_" + hashlib.sha256((handle.manifest_sha256 + "\0" + query).encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _eligible_rows(snapshot: _Snapshot) -> list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]]:
        rows = []
        for variant_id, variant in snapshot.variants.items():
            if variant.get("candidate_qualification") not in {"eligible", "conditional"}:
                continue
            state = snapshot.states.get(str(variant.get("canonical_state_id")))
            block = snapshot.blocks.get(str(variant.get("block_id")))
            if state is not None and block is not None:
                rows.append((variant_id, variant, state, block))
        return rows

    def _recall(self, handle: ReleaseHandle, rows: Sequence[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]], keywords: Sequence[str]) -> list[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]]:
        allowed = {row[0] for row in rows}
        tokens = _keyword_tokens(keywords)
        if not tokens:
            return list(rows)
        ids: set[str] = set()
        for token in tokens:
            if handle.fts_mode == "trigram" and len(token) >= 3:
                escaped = '"' + token.replace('"', " ") + '"'
                cursor = handle.execute("SELECT variant_id FROM search_fts WHERE search_fts MATCH ?", (escaped,))
            elif handle.fts_mode == "trigram":
                cursor = handle.execute("SELECT variant_id FROM search_fts WHERE normalized_text LIKE ?", (f"%{token}%",))
            else:
                cursor = handle.execute("SELECT variant_id FROM search_text WHERE normalized_text LIKE ?", (f"%{token}%",))
            ids.update(str(row[0]) for row in cursor)
        return [row for row in rows if row[0] in ids and row[0] in allowed]

    def _rank_rows(self, rows: Sequence[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]], snapshot: _Snapshot, intent: _KeywordIntent) -> list[tuple[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]], float, dict[str, float]]]:
        result = []
        for row in rows:
            variant_id, variant, _state, block = row
            semantic = _semantic(snapshot.annotations.get(variant_id))
            names = block.get("official_names", {})
            feature = snapshot.features[variant_id]
            geometry = [str(item) for item in feature["geometry_classes"]]
            terms = intent.soft
            matches: dict[str, float] = {}
            if terms.get("shape_terms"):
                matches["shape"] = _contains_any(geometry, terms["shape_terms"])
            if terms.get("colors"):
                matches["color"] = _feature_color_score(feature, terms["colors"])
            if terms.get("uses"):
                matches["use"] = _contains_any(semantic.get("building_roles", []), terms["uses"])
            if terms.get("keywords"):
                matches["name_synonym"] = _contains_any([names.get("zh_cn"), names.get("en_us")] + list(semantic.get("synonyms_zh", [])) + list(semantic.get("synonyms_en", [])), terms["keywords"])
            if terms.get("styles"):
                matches["style"] = _contains_any(semantic.get("style_tags", []), terms["styles"])
            score, breakdown = deterministic_score(matches)
            result.append((row, score, breakdown))
        return sorted(result, key=lambda item: (-item[1], item[0][0].encode("utf-8")))

    def _candidate_dicts(self, ranked: Sequence[tuple[tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]], float, dict[str, float]]], snapshot: _Snapshot, intent: _KeywordIntent) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for index, (row, score, breakdown) in enumerate(ranked):
            variant_id, variant, state, block = row
            names = block.get("official_names", {})
            semantic = _semantic(snapshot.annotations.get(variant_id))
            candidates.append({
                "candidate_id": f"T{index + 1:02d}",
                "variant_id": variant_id,
                "block_id": str(variant["block_id"]),
                "display_name": str(names.get("zh_cn") or names.get("en_us") or variant_id),
                "recommended_state_id": str(variant["canonical_state_id"]),
                "candidate_qualification": str(variant["candidate_qualification"]),
                "local_score": score,
                "final_score": score,
                "score_source": "local",
                "score_breakdown": breakdown,
                "reason": self._reason(breakdown, semantic),
                "warnings": list(variant.get("warnings", [])),
                "machine_fact_refs": [{"record_type": "state", "record_id": str(state["state_id"]), "field": "behavior"}, {"record_type": "visual_variant", "record_id": variant_id, "field": "machine_facts"}],
            })
        return candidates

    @staticmethod
    def _reason(breakdown: Mapping[str, float], semantic: Mapping[str, Any]) -> str:
        active = [key for key, value in breakdown.items() if value > 0]
        if active:
            return "Matches " + ", ".join(active) + "."
        summary = semantic.get("summary_en")
        return str(summary)[:500] if isinstance(summary, str) and summary else "Deterministic release candidate."

    @staticmethod
    def _exclusions(snapshot: _Snapshot, rows: Sequence[Any], recalled: Sequence[Any]) -> list[dict[str, Any]]:
        excluded = sum(1 for variant in snapshot.variants.values() if variant.get("candidate_qualification") == "excluded")
        recall_removed = max(0, len(rows) - len(recalled))
        result = []
        if excluded:
            result.append({"reason": "excluded qualification", "count": excluded})
        if recall_removed:
            result.append({"reason": "text recall", "count": recall_removed})
        return result

    def _search_sheet(self, handle: ReleaseHandle, candidates: list[dict[str, Any]], snapshot: _Snapshot, resources: _RequestResources) -> tuple[Any, dict[str, Any], bytes]:
        del snapshot
        source: list[tuple[str, bytes, DecodedPng]] = []
        mapping: list[dict[str, Any]] = []
        for candidate in candidates:
            payload, decoded = self._preview(handle, candidate["variant_id"], resources)
            source.append((candidate["variant_id"], payload, decoded))
            mapping.append({"candidate_id": candidate["candidate_id"], "variant_id": candidate["variant_id"], "block_id": candidate["block_id"]})
        sheet = _make_cached_contact_sheet(source, columns=4)
        tiles = [{**item, "row": index // 4, "column": index % 4} for index, item in enumerate(mapping)]
        image_id = _image_id(sheet.image_png, "contact")
        width = min(4, len(source)) * 512
        height = ((len(source) + min(4, len(source)) - 1) // min(4, len(source))) * 512
        image = {"image_id": image_id, "purpose": "search_contact_sheet", "mime_type": "image/png", "width": width, "height": height, "sha256": sha256_bytes(sheet.image_png), "content_index": 1, "mapping": mapping}
        return sheet, {"image_id": image_id, "mapping_tiles": tiles, "image": image}, sheet.image_png

    def _preview(self, handle: ReleaseHandle, variant_id: str, resources: _RequestResources) -> tuple[bytes, DecodedPng]:
        cached = resources.preview_cache.get(variant_id)
        if cached is not None:
            return cached
        row = handle.execute("SELECT preview_path,image_sha256 FROM visual_variants WHERE variant_id=?", (variant_id,)).fetchone()
        if row is None:
            raise MCPReleaseError("IMAGE_READ_FAILED", "A release preview reference is missing.", minecraft_version=handle.minecraft_version)
        value = handle.read_image(str(row[0]))
        resources.preview_cache[variant_id] = value
        return value

    def _details_data(self, handle: ReleaseHandle, snapshot: _Snapshot, block_id: str, resources: _RequestResources) -> tuple[dict[str, Any], tuple[bytes, ...]]:
        block = snapshot.blocks[block_id]
        states = sorted((state for state in snapshot.states.values() if state.get("block_id") == block_id), key=lambda value: str(value["state_id"]).encode("utf-8"))
        variants = sorted((variant for variant in snapshot.variants.values() if variant.get("block_id") == block_id), key=lambda value: str(value["variant_id"]).encode("utf-8"))
        property_definitions = [{"name": name, "allowed_values": list(values)} for name, values in sorted(block.get("properties", {}).items(), key=lambda item: str(item[0]).encode("utf-8"))]
        images: list[dict[str, Any]] = []
        image_bytes: list[bytes] = []
        variant_outputs = []
        for variant in variants:
            state = snapshot.states.get(str(variant["canonical_state_id"]))
            if state is None:
                raise MCPReleaseError("INDEX_INFO_UNAVAILABLE", "A variant references unavailable state data.", minecraft_version=handle.minecraft_version, details={"integrity_component": "index"})
            annotation_value = _semantic(snapshot.annotations.get(str(variant["variant_id"])))
            annotation = None
            if isinstance(annotation_value.get("summary_zh"), str) and isinstance(annotation_value.get("summary_en"), str) and isinstance(annotation_value.get("confidence"), (int, float)) and not isinstance(annotation_value.get("confidence"), bool) and 0 <= float(annotation_value["confidence"]) <= 1:
                annotation = {"summary_zh": annotation_value["summary_zh"], "summary_en": annotation_value["summary_en"], "confidence": annotation_value["confidence"]}
            image_ids: list[str] = []
            if isinstance(variant.get("render"), Mapping):
                payload, decoded = self._preview(handle, str(variant["variant_id"]), resources)
                image_id = _image_id(payload, str(variant["variant_id"]))
                image_ids.append(image_id)
                images.append({"image_id": image_id, "purpose": "block_variant_views", "mime_type": "image/png", "width": decoded.width, "height": decoded.height, "sha256": sha256_bytes(payload), "content_index": len(image_bytes) + 1, "mapping": [{"candidate_id": None, "variant_id": variant["variant_id"], "block_id": block_id}]})
                image_bytes.append(payload)
            geometry = variant["machine_facts"]["geometry"]
            variant_outputs.append({
                "variant_id": variant["variant_id"], "canonical_state_id": variant["canonical_state_id"], "represented_state_ids": list(variant["represented_state_ids"]), "candidate_qualification": variant["candidate_qualification"], "warnings": list(variant.get("warnings", [])),
                "variant_facts": {"geometry_summary": geometry["shape"], "geometry_signature": geometry["geometry_signature"], "collision_signature": geometry["collision_signature"], "geometry_classes": list(geometry["geometry_classes"]), "machine_tags": list(variant["machine_facts"]["machine_tags"]), "state_behaviors": [{"state_id": state_id, "behavior": behavior} for state_id, behavior in sorted(variant["machine_facts"]["behavior_by_state"].items(), key=lambda item: str(item[0]).encode("utf-8"))]},
                "annotation": annotation, "image_ids": image_ids,
            })
        default_state = snapshot.states.get(str(block["default_state_id"]))
        if default_state is None:
            raise MCPReleaseError("INDEX_INFO_UNAVAILABLE", "A block references unavailable default-state data.", minecraft_version=handle.minecraft_version, details={"integrity_component": "index"})
        property_values = lambda state: [{"name": name, "value": value} for name, value in sorted(state.get("properties", {}).items(), key=lambda item: str(item[0]).encode("utf-8"))]
        state_outputs = [{"state_id": state["state_id"], "is_default": state["is_default"], "properties": property_values(state), "shape": state["shape"], "collision": state["collision"], "behavior": state["behavior"], "variant_ids": list(state["variant_ids"]), "mapping_status": state["mapping_status"]} for state in states]
        data = {"block_id": block_id, "official_names": block["official_names"], "translation_key": block["translation_key"], "default_state_id": block["default_state_id"], "property_definitions": property_definitions, "states": state_outputs, "block_facts": {"has_item": block["machine_facts"]["has_item"], "has_block_entity": block["machine_facts"]["has_block_entity"], "tags": list(block.get("tags", [])), "default_state_behavior": default_state["behavior"]}, "variants": variant_outputs, "images": images, "audit": {"skip_records": self._audit_ids(snapshot.manual, "skip_reviews", block_id), "override_refs": self._audit_ids(snapshot.manual, "manual_overrides", block_id), "qualification_review_refs": self._audit_ids(snapshot.manual, "qualification_reviews", block_id)}}
        return data, tuple(image_bytes)

    @staticmethod
    def _audit_ids(manual: Mapping[str, Any], key: str, block_id: str) -> list[str]:
        values = manual.get(key, [])
        result = []
        if isinstance(values, list):
            for item in values:
                if not isinstance(item, Mapping):
                    continue
                target_id = item.get("scope", {}).get("variant_id") if key == "manual_overrides" and isinstance(item.get("scope"), Mapping) else item.get("target_id")
                if target_id != block_id and item.get("block_id") != block_id:
                    continue
                value = item.get("review_id") or item.get("override_id") or item.get("qualification_review_id")
                if isinstance(value, str):
                    result.append(value)
        return result

    def _compare_data(self, handle: ReleaseHandle, snapshot: _Snapshot, block_ids: Sequence[str], resources: _RequestResources) -> tuple[dict[str, Any], tuple[bytes, ...]]:
        rows = []
        for field, extractor, source in (
            ("candidate_qualification", lambda variant, state, block: variant.get("candidate_qualification"), "machine"),
            ("geometry_classes", lambda variant, state, block: ",".join(snapshot.features[str(variant["variant_id"])] ["geometry_classes"]), "machine"),
            ("transparent", lambda variant, state, block: _behavior(variant, state).get("transparent", "unknown"), "machine"),
            ("emissive", lambda variant, state, block: _behavior(variant, state).get("emissive", "unknown"), "machine"),
            ("emission_level", lambda variant, state, block: _behavior(variant, state).get("emission_level", "unknown"), "machine"),
            ("redstone_related", lambda variant, state, block: _behavior(variant, state).get("redstone_related", "unknown"), "machine"),
            ("summary_en", lambda variant, state, block: _semantic(snapshot.annotations.get(str(variant["variant_id"]))).get("summary_en"), "annotation"),
        ):
            values = []
            for block_id in block_ids:
                variants = sorted((variant for variant in snapshot.variants.values() if variant.get("block_id") == block_id), key=lambda value: str(value["variant_id"]).encode("utf-8"))
                if not variants:
                    continue
                variant = variants[0]
                state = snapshot.states.get(str(variant["canonical_state_id"]))
                if state is None:
                    continue
                value = extractor(variant, state, snapshot.blocks[block_id])
                if isinstance(value, (bool, int, float, str)):
                    values.append({"block_id": block_id, "value": value, "source": source})
            if len(values) >= 2 and len({json.dumps(item["value"], ensure_ascii=False, sort_keys=True) for item in values}) > 1:
                rows.append({"field": field, "values": values})
        source_images: list[tuple[str, bytes, DecodedPng]] = []
        mapping: list[dict[str, Any]] = []
        for index, block_id in enumerate(block_ids, start=1):
            variants = sorted((variant for variant in snapshot.variants.values() if variant.get("block_id") == block_id), key=lambda value: str(value["variant_id"]).encode("utf-8"))
            if not variants:
                continue
            variant = variants[0]
            payload, decoded = self._preview(handle, str(variant["variant_id"]), resources)
            source_images.append((str(variant["variant_id"]), payload, decoded))
            mapping.append({"candidate_id": f"T{index:02d}", "variant_id": variant["variant_id"], "block_id": block_id})
        if not source_images:
            return {"block_ids": list(block_ids), "rows": rows, "contact_sheet": {"image_id": None, "tile_mapping": []}, "images": []}, ()
        sheet = _make_cached_contact_sheet(source_images, columns=len(source_images))
        tiles = [{**item, "row": 0, "column": index} for index, item in enumerate(mapping)]
        image_id = _image_id(sheet.image_png, "compare")
        image = {"image_id": image_id, "purpose": "compare_contact_sheet", "mime_type": "image/png", "width": len(source_images) * 512, "height": 512, "sha256": sha256_bytes(sheet.image_png), "content_index": 1, "mapping": mapping}
        return {"block_ids": list(block_ids), "rows": rows, "contact_sheet": {"image_id": image_id, "tile_mapping": tiles}, "images": [image]}, (sheet.image_png,)


QueryService = MCPQueryService


__all__ = ["MCPInputError", "MCPProtocolError", "MCPQueryService", "MCPToolResult", "QueryService", "WEIGHTS", "deterministic_score"]
