"""Deterministic workspace search with trigram and LIKE modes."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

from .schema import RecordSchemaError, validate_record
from .storage import WorkspaceDatabase


SEMANTIC_LIST_FIELDS = (
    "synonyms_zh",
    "synonyms_en",
    "color_terms",
    "shape_terms",
    "material_impressions",
    "building_roles",
    "style_tags",
    "avoid_for",
)
SEMANTIC_SCALAR_FIELDS = ("summary_zh", "summary_en")
HUMAN_SEMANTIC_FIELDS = SEMANTIC_LIST_FIELDS + SEMANTIC_SCALAR_FIELDS + ("confidence",)
def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


@dataclass(frozen=True, slots=True)
class SearchHit:
    block_id: str
    score: float
    content: str

    def to_dict(self) -> dict[str, Any]:
        return {"block_id": self.block_id, "score": self.score, "content": self.content}


class WorkspaceQueryService:
    def __init__(self, database: WorkspaceDatabase):
        self.database = database

    def rebuild_index(self) -> None:
        connection = self.database.connection
        with self.database.transaction():
            connection.execute("DELETE FROM search_documents")
            if self.database.fts_mode == "trigram":
                connection.execute("DELETE FROM fts_documents")
            for document_id, block_id, content, normalized in self._expected_documents(connection).values():
                connection.execute(
                    "INSERT INTO search_documents(document_id,block_id,content,normalized_content) VALUES (?,?,?,?)",
                    (document_id, block_id, content, normalized),
                )
                if self.database.fts_mode == "trigram":
                    connection.execute("INSERT INTO fts_documents(block_id,content) VALUES (?,?)", (block_id, normalized))

    def expected_documents(self, connection: Any | None = None) -> dict[str, tuple[str, str, str, str]]:
        """Return the exact current search projection without mutating SQLite."""

        return self._expected_documents(connection or self.database.connection)

    def _expected_documents(self, connection: Any) -> dict[str, tuple[str, str, str, str]]:
        documents: dict[str, tuple[str, str, str, str]] = {}
        for row in connection.execute("SELECT block_id, record_json FROM blocks ORDER BY block_id"):
            block = json.loads(row["record_json"])
            for variant in connection.execute(
                "SELECT variant_id,record_json FROM variants WHERE block_id = ? AND record_json IS NOT NULL ORDER BY variant_id",
                (row["block_id"],),
            ):
                record = json.loads(variant["record_json"])
                if record.get("candidate_qualification") not in {"eligible", "conditional"}:
                    continue
                semantics = self._verified_semantics(connection, row["block_id"], variant["variant_id"])
                content = self._document_content(block, record, semantics)
                normalized = normalize_text(content)
                document_id = "doc_" + variant["variant_id"]
                documents[document_id] = (document_id, str(row["block_id"]), content, normalized)
        return documents

    @staticmethod
    def _document_content(block: dict[str, Any], record: dict[str, Any], semantics: dict[str, Any]) -> str:
        tags: list[str] = []
        terms = [block.get("official_names", {}).get("zh_cn"), block.get("official_names", {}).get("en_us")]
        for term in terms:
            if isinstance(term, str):
                tags.append(term)
        facts = record.get("machine_facts", {})
        tags.extend(str(tag) for tag in facts.get("machine_tags", []))
        tags.extend(str(tag) for tag in facts.get("geometry", {}).get("geometry_classes", []))
        for key in SEMANTIC_LIST_FIELDS + SEMANTIC_SCALAR_FIELDS:
            value = semantics.get(key)
            if isinstance(value, list):
                tags.extend(str(item) for item in value)
            elif isinstance(value, str):
                tags.append(value)
        return " ".join(sorted(set(tags), key=lambda value: value.encode("utf-8")))

    def _verified_semantics(self, connection: Any, block_id: str, variant_id: str) -> dict[str, Any]:
        """Replay only approved semantic layers in a stable order.

        Machine fields never come from this projection.  Qualification records
        are applied to the visual variant by the review service and therefore
        only act as the candidate filter above.
        """

        semantic: dict[str, Any] = {}
        rows = connection.execute(
            "SELECT record_json FROM annotations WHERE minecraft_version='26.2' AND subject_id IN (?,?) ORDER BY annotation_id",
            (block_id, variant_id),
        ).fetchall()
        for row in rows:
            annotation = json.loads(row["record_json"])
            source = annotation.get("source", {})
            if source.get("verified") is not True:
                continue
            for key in SEMANTIC_LIST_FIELDS:
                values = annotation.get(key)
                if isinstance(values, list):
                    semantic.setdefault(key, [])
                    semantic[key].extend(str(value) for value in values)
            for key in SEMANTIC_SCALAR_FIELDS:
                if isinstance(annotation.get(key), str):
                    semantic[key] = annotation[key]

        for override in manual_override_records(connection, variant_id):
            _apply_manual_operations(semantic, override["operations"])
        for key, value in list(semantic.items()):
            if isinstance(value, list):
                semantic[key] = sorted(set(value), key=lambda item: str(item).encode("utf-8"))
        return semantic

    def query(self, query: str, *, limit: int = 24) -> list[SearchHit]:
        normalized = normalize_text(query)
        if not normalized:
            return []
        limit = max(1, min(int(limit), 100))
        rows: list[sqlite3.Row]
        if self.database.fts_mode == "trigram" and len(normalized) >= 3:
            try:
                rows = self.database.fetchall(
                    "SELECT d.block_id, d.content FROM fts_documents f JOIN search_documents d ON d.block_id = f.block_id WHERE fts_documents MATCH ? ORDER BY d.block_id LIMIT ?",
                    (f'"{normalized.replace(chr(34), " ")}"', limit),
                )
            except sqlite3.OperationalError:
                rows = self._like_rows(normalized, limit)
        else:
            rows = self._like_rows(normalized, limit)
        hits = []
        seen_blocks: set[str] = set()
        for row in rows:
            if str(row["block_id"]) in seen_blocks:
                continue
            seen_blocks.add(str(row["block_id"]))
            content = str(row["content"])
            hits.append(SearchHit(str(row["block_id"]), _score(normalized, normalize_text(content)), content))
        return sorted(hits, key=lambda hit: (-hit.score, hit.block_id.encode("utf-8")))[:limit]

    def _like_rows(self, normalized: str, limit: int) -> list[sqlite3.Row]:
        return self.database.fetchall(
            "SELECT block_id, content FROM search_documents WHERE normalized_content LIKE ? ORDER BY block_id LIMIT ?",
            ("%" + normalized + "%", limit),
        )


def _score(query: str, content: str) -> float:
    if content == query:
        return 1.0
    if query in content:
        return round(0.5 + len(query) / max(1, len(content)) / 2, 8)
    return 0.0


def manual_override_records(connection: Any, variant_id: str) -> list[dict[str, Any]]:
    """Return structurally valid semantic overrides in replay order."""

    records: list[dict[str, Any]] = []
    rows = connection.execute(
        "SELECT record_json FROM overrides WHERE minecraft_version='26.2' AND target_id=? ORDER BY override_id",
        (variant_id,),
    ).fetchall()
    for row in rows:
        try:
            record = json.loads(row["record_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        try:
            validate_record("manual-override.v1", record)
        except (RecordSchemaError, TypeError):
            continue
        scope = record.get("scope")
        if not isinstance(scope, dict) or scope.get("variant_id") != variant_id:
            continue
        records.append(record)
    return records


def human_semantic_projection(connection: Any, variant_id: str) -> dict[str, Any] | None:
    """Replay only manual semantic overrides, without inventing AI evidence."""

    records = manual_override_records(connection, variant_id)
    if not records:
        return None
    semantic: dict[str, Any] = {}
    for record in records:
        _apply_manual_operations(semantic, record["operations"])
    for key, value in list(semantic.items()):
        if isinstance(value, list):
            semantic[key] = sorted(set(value), key=lambda item: str(item).encode("utf-8"))
    return semantic


def human_semantics_complete(connection: Any, variant_id: str) -> bool:
    semantic = human_semantic_projection(connection, variant_id)
    if semantic is None:
        return False
    for key in SEMANTIC_LIST_FIELDS:
        if not isinstance(semantic.get(key), list):
            return False
    for key in SEMANTIC_SCALAR_FIELDS:
        if not isinstance(semantic.get(key), str) or not semantic[key].strip():
            return False
    confidence = semantic.get("confidence")
    return isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1


def _apply_manual_operations(semantic: dict[str, Any], operations: dict[str, Any]) -> None:
    for key, value in operations.items():
        if key.startswith("add_") and isinstance(value, list):
            field = key[4:]
            semantic.setdefault(field, [])
            if isinstance(semantic[field], list):
                semantic[field].extend(str(item) for item in value)
        elif key.startswith("remove_") and isinstance(value, list):
            field = key[7:]
            current = semantic.get(field, [])
            if isinstance(current, list):
                semantic[field] = [item for item in current if item not in value]
        elif key.startswith("set_"):
            semantic[key[4:]] = value
