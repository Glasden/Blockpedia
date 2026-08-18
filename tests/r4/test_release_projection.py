from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from blockpedia.releases import canonical_json
from blockpedia.storage import packaged_release_index_schema

from tests.r3.test_release_builder import _ready


REPO_ROOT = Path(__file__).parents[2]


def test_v2_schema_has_fresh_projection_columns_and_preserves_v1_bytes(tmp_path: Path) -> None:
    sql, digest = packaged_release_index_schema()
    v2_sql_path = REPO_ROOT / "src" / "blockpedia" / "sql" / "release-index.v2.sql"
    v2_hash_path = REPO_ROOT / "src" / "blockpedia" / "sql" / "release-index.v2.sha256"
    assert sql == v2_sql_path.read_bytes()
    assert digest == v2_hash_path.read_text(encoding="ascii").strip()
    assert digest == "sha256:" + hashlib.sha256(sql).hexdigest()

    v1_sql_path = REPO_ROOT / "src" / "blockpedia" / "sql" / "release-index.v1.sql"
    v1_hash_path = REPO_ROOT / "src" / "blockpedia" / "sql" / "release-index.v1.sha256"
    v1_sql = v1_sql_path.read_bytes()
    assert v1_hash_path.read_text(encoding="ascii").strip() == "sha256:0b8e925a0674edd84beb85f3fcfc9bf52f5f6cec9c3f34c7ff8119c00de31e3d"
    assert "sha256:" + hashlib.sha256(v1_sql).hexdigest() == v1_hash_path.read_text(encoding="ascii").strip()
    assert v1_sql != sql

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(sql.decode("utf-8"))
        connection.execute("INSERT INTO schema_meta(format_version) VALUES (2)")
        columns = {
            table: [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            for table in ("blocks", "states", "visual_variants")
        }
        assert columns["blocks"] == [
            "block_id", "minecraft_version", "translation_key", "name_zh", "name_en",
            "default_state_id", "machine_facts_json", "record_json",
        ]
        assert columns["states"] == ["state_id", "block_id", "properties_json", "is_default", "record_json"]
        assert columns["visual_variants"][-2:] == ["record_json", "feature_json"]
        assert connection.execute("SELECT format_version FROM schema_meta").fetchall() == [(2,)]
        assert {
            row[1] for row in connection.execute("SELECT type,name FROM sqlite_master WHERE type='index'")
        } >= {"states_block_id_idx", "visual_variants_block_id_idx", "visual_variants_qualification_idx"}
    finally:
        connection.close()


def test_v2_builder_projects_canonical_records_and_features_consistently(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        checked = service.check_candidate_release(run_id, "26.2")
        built = service.build_candidate_release(checked["check_id"])
        release = tmp_path / built["relative_path"]
        with service.worker.open_database(run_id) as workspace:
            workspace_blocks = {
                str(row["block_id"]): canonical_json(json.loads(row["record_json"]))
                for row in workspace.fetchall("SELECT block_id,record_json FROM blocks")
            }
            workspace_states = {
                str(row["state_id"]): canonical_json(json.loads(row["record_json"]))
                for row in workspace.fetchall("SELECT state_id,record_json FROM states")
            }
            workspace_variants = {
                str(row["variant_id"]): canonical_json(json.loads(row["record_json"]))
                for row in workspace.fetchall("SELECT variant_id,record_json FROM variants WHERE status='selected'")
            }
            workspace_features = {
                str(row["variant_id"]): canonical_json(json.loads(row["feature_json"]))
                for row in workspace.fetchall("SELECT variant_id,feature_json FROM features")
            }

        index = sqlite3.connect(release / "index.sqlite3")
        index.row_factory = sqlite3.Row
        try:
            assert [tuple(row) for row in index.execute("SELECT format_version FROM schema_meta").fetchall()] == [(2,)]
            release_blocks = {str(row["block_id"]): row for row in index.execute("SELECT * FROM blocks")}
            release_states = {str(row["state_id"]): row for row in index.execute("SELECT * FROM states")}
            release_variants = {str(row["variant_id"]): row for row in index.execute("SELECT * FROM visual_variants")}
            assert {key: row["record_json"] for key, row in release_blocks.items()} == workspace_blocks
            assert {key: row["record_json"] for key, row in release_states.items()} == workspace_states
            assert {key: row["record_json"] for key, row in release_variants.items()} == workspace_variants
            assert {key: row["feature_json"] for key, row in release_variants.items()} == workspace_features

            for row in release_blocks.values():
                record = json.loads(row["record_json"])
                assert row["block_id"] == record["block_id"]
                assert row["default_state_id"] == record["default_state_id"]
                assert row["machine_facts_json"] == canonical_json(record["machine_facts"])
            for row in release_states.values():
                record = json.loads(row["record_json"])
                assert row["state_id"] == record["state_id"]
                assert row["block_id"] == record["block_id"]
                assert row["properties_json"] == canonical_json(record["properties"])
                assert row["is_default"] == int(record["is_default"] is True)
            for row in release_variants.values():
                record = json.loads(row["record_json"])
                assert row["variant_id"] == record["variant_id"]
                assert row["block_id"] == record["block_id"]
                assert row["canonical_state_id"] == record["canonical_state_id"]
                assert row["represented_state_ids_json"] == canonical_json(record["represented_state_ids"])
                assert row["candidate_qualification"] == record["candidate_qualification"]
                assert row["warnings_json"] == canonical_json(record["warnings"])
        finally:
            index.close()
    finally:
        service.close()


def test_v2_does_not_migrate_an_existing_v1_index(tmp_path: Path) -> None:
    v1_sql = (REPO_ROOT / "src" / "blockpedia" / "sql" / "release-index.v1.sql").read_bytes()
    v2_sql, _ = packaged_release_index_schema()
    path = tmp_path / "index.sqlite3"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(v1_sql.decode("utf-8"))
        connection.execute("INSERT INTO schema_meta(format_version) VALUES (1)")
        connection.commit()
        try:
            connection.executescript(v2_sql.decode("utf-8"))
        except sqlite3.OperationalError:
            pass
        else:
            raise AssertionError("v2 SQL unexpectedly migrated an existing v1 index")
        assert connection.execute("SELECT format_version FROM schema_meta").fetchall() == [(1,)]
        assert "record_json" not in {
            row[1] for row in connection.execute("PRAGMA table_info(blocks)")
        }
    finally:
        connection.close()
