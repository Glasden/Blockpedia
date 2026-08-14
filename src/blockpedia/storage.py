"""SQLite workspace storage with one checked, non-migrating schema."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence


class DatabaseSchemaMismatch(RuntimeError):
    """The immutable packaged schema and a workspace database disagree."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resource_bytes(name: str) -> bytes:
    return importlib.resources.files("blockpedia").joinpath("sql", name).read_bytes()


def packaged_schema() -> tuple[bytes, str]:
    sql = _resource_bytes("workspace.v1.sql")
    expected = _resource_bytes("workspace.v1.sha256").decode("ascii").strip()
    actual = "sha256:" + hashlib.sha256(sql).hexdigest()
    if expected != actual:
        raise DatabaseSchemaMismatch("packaged workspace schema hash mismatch")
    return sql, actual


class WorkspaceDatabase:
    """A checked connection to one version/run workspace database."""

    def __init__(self, connection: sqlite3.Connection, path: Path, schema_sha256: str, fts_mode: str, *, read_only: bool = False):
        self.connection = connection
        self.path = path
        self.schema_sha256 = schema_sha256
        self.fts_mode = fts_mode
        self.read_only = read_only

    @classmethod
    def open(cls, path: str | Path, *, force_normalized_like: bool = False, read_only: bool = False) -> "WorkspaceDatabase":
        sql, schema_sha256 = packaged_schema()
        db_path = Path(path)
        if not read_only:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(db_path, timeout=30, isolation_level=None)
        else:
            connection = sqlite3.connect(f"file:{db_path.absolute().as_posix()}?mode=ro", uri=True, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            if not read_only:
                connection.execute("PRAGMA foreign_keys = ON")
            meta_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
            ).fetchone()
            if meta_exists is None and read_only:
                raise DatabaseSchemaMismatch("workspace database schema is missing")
            if meta_exists is None:
                existing_table = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1"
                ).fetchone()
                if existing_table is not None:
                    raise DatabaseSchemaMismatch("workspace database schema metadata is missing")
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.executescript(sql.decode("utf-8"))
                connection.execute(
                    "INSERT INTO schema_meta(schema_version, schema_sha256, created_at) VALUES (?, ?, ?)",
                    ("workspace.v1", schema_sha256, utc_now()),
                )
            else:
                row = connection.execute(
                    "SELECT schema_version, schema_sha256 FROM schema_meta WHERE schema_version = 'workspace.v1'"
                ).fetchone()
                if row is None or row["schema_sha256"] != schema_sha256:
                    raise DatabaseSchemaMismatch("workspace database schema hash mismatch")
                if not read_only:
                    connection.execute("PRAGMA journal_mode = WAL")
                    connection.execute("PRAGMA synchronous = FULL")

            fts_mode = _configure_fts(connection, force_normalized_like=force_normalized_like, read_only=read_only)
            return cls(connection, db_path, schema_sha256, fts_mode, read_only=read_only)
        except Exception:
            connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "WorkspaceDatabase":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.read_only:
            raise sqlite3.OperationalError("read-only workspace connection")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    @contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        """One consistent read view for a public live snapshot."""

        self.connection.execute("BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def execute(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Cursor:
        return self.connection.execute(sql, parameters)

    def fetchone(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        return self.connection.execute(sql, parameters).fetchone()

    def fetchall(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, parameters).fetchall())

    @staticmethod
    def json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _configure_fts(connection: sqlite3.Connection, *, force_normalized_like: bool, read_only: bool = False) -> str:
    if force_normalized_like:
        return "normalized_like"
    if read_only:
        present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='fts_documents'").fetchone()
        return "trigram" if present is not None else "normalized_like"
    if not probe_fts5(connection):
        return "normalized_like"
    connection.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS fts_documents USING fts5(block_id UNINDEXED, content, tokenize='trigram')"
    )
    return "trigram"


def probe_fts5(connection: sqlite3.Connection) -> bool:
    """Probe the runtime SQLite build without changing workspace tables."""

    try:
        connection.execute("CREATE VIRTUAL TABLE temp.blockpedia_fts_probe USING fts5(content, tokenize='trigram')")
        connection.execute("DROP TABLE temp.blockpedia_fts_probe")
    except sqlite3.OperationalError:
        return False
    return True
