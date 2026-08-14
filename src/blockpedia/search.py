"""Deterministic workspace search with trigram and LIKE modes."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Any

from .storage import WorkspaceDatabase


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
            for row in connection.execute("SELECT block_id, record_json FROM blocks ORDER BY block_id"):
                block = json.loads(row["record_json"])
                for variant in connection.execute(
                    "SELECT variant_id,record_json FROM variants WHERE block_id = ? AND record_json IS NOT NULL ORDER BY variant_id",
                    (row["block_id"],),
                ):
                    record = json.loads(variant["record_json"])
                    if record.get("candidate_qualification") not in {"eligible", "conditional"}:
                        continue
                    tags: list[str] = []
                    terms = [block.get("official_names", {}).get("zh_cn"), block.get("official_names", {}).get("en_us")]
                    for term in terms:
                        if isinstance(term, str):
                            tags.append(term)
                    facts = record.get("machine_facts", {})
                    tags.extend(str(tag) for tag in facts.get("machine_tags", []))
                    tags.extend(str(tag) for tag in facts.get("geometry", {}).get("geometry_classes", []))
                    content = " ".join(sorted(set(tags), key=lambda value: value.encode("utf-8")))
                    normalized = normalize_text(content)
                    document_id = "doc_" + variant["variant_id"]
                    connection.execute(
                        "INSERT INTO search_documents(document_id,block_id,content,normalized_content) VALUES (?,?,?,?)",
                        (document_id, row["block_id"], content, normalized),
                    )
                    if self.database.fts_mode == "trigram":
                        connection.execute("INSERT INTO fts_documents(block_id,content) VALUES (?,?)", (row["block_id"], normalized))

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
