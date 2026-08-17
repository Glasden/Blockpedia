from __future__ import annotations

import json
from pathlib import Path

import blockpedia.releases as releases_module
from blockpedia.releases import (
    _BANNER_REFRESH_POLICY_TOKEN,
    _hash_json,
)
from .test_release_builder import _ready


def _refresh_report(service, run_id: str, *, provenance: dict | None = None) -> dict:
    with service.worker.open_database(run_id) as database:
        row = database.fetchone(
            "SELECT import_id,export_id,manifest_sha256,checksum_sha256 FROM imports "
            "WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)",
            (run_id,),
        )
        assert row is not None
        current = dict(row)
        value = provenance or {
            "format": "banner-refresh.v1",
            "base": {
                "import_id": "import_" + "1" * 32,
                "export_id": "export_20260816T091512Z",
                "manifest_sha256": "sha256:" + "1" * 64,
                "checksum_sha256": "sha256:" + "2" * 64,
            },
            "new": {
                "import_id": current["import_id"],
                "export_id": current["export_id"],
                "manifest_sha256": current["manifest_sha256"],
                "checksum_sha256": current["checksum_sha256"],
            },
            "check_id": "check_" + "3" * 32,
            "target_ids": list(releases_module._BANNER_REFRESH_TARGETS),
            "policy_token": _BANNER_REFRESH_POLICY_TOKEN,
        }
        with database.transaction() as connection:
            connection.execute(
                "UPDATE imports SET report_json=? WHERE import_id=?",
                (json.dumps(value), current["import_id"]),
            )
        return value


def _ai_item(data_root: Path, check: dict) -> dict:
    report = json.loads(
        (data_root / "cache" / "release-checks" / check["check_id"] / "quality_report.json").read_text(
            encoding="utf-8"
        )
    )
    return next(item for item in report["items"] if item["code"] == "AI_SCHEMA_VALID")


def test_legacy_import_behavior_is_unchanged(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        legacy = service.check_candidate_release(run_id, "26.2")
        assert legacy["can_build"] is True
    finally:
        service.close()


def test_refresh_provenance_is_fingerprinted_and_is_a_functional_input(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:

        provenance = _refresh_report(service, run_id)
        refreshed = service.check_candidate_release(run_id, "26.2")
        assert refreshed["can_build"] is True
        built = service.build_candidate_release(refreshed["check_id"])
        manifest = json.loads((tmp_path / built["relative_path"] / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["functional_inputs"]["source_import/banner_refresh_provenance"] == _hash_json(
            provenance
        )
    finally:
        service.close()


def test_valid_preserved_base_request_excludes_targets_and_recomputes_base_signature(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready(tmp_path)
    try:
        provenance = _refresh_report(service, run_id)
        base_export = provenance["base"]["export_id"]
        with service.worker.open_database(run_id) as database:
            with database.read_transaction() as connection:
                run = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                snapshot = service.release_builder._snapshot(connection, database.path.parent, run, "26.2")
                request = next(item for item in snapshot.provider_requests if item["status"] == "succeeded")
                provider = releases_module._provider_snapshot(snapshot.config)
                assert provider is not None
                tile_map = request["envelope"]["input_summary"]["tile_variant_map"]

                captured: dict[str, dict] = {}
                original_hash_json = releases_module.sha256_json

                def capture(value):
                    if isinstance(value, dict) and value.get("stage") == "offline_annotation" and "contact_sheet_sha256" in value:
                        captured["input"] = value
                    return original_hash_json(value)

                monkeypatch.setattr(releases_module, "sha256_json", capture)
                assert not service.release_builder._provider_request_matches_current_input(
                    snapshot, request, tile_map, provider, export_id=base_export
                )
                historical_input = original_hash_json(captured["input"])

            with database.transaction() as connection:
                envelope = dict(request["envelope"])
                envelope["export_id"] = base_export
                connection.execute(
                    "UPDATE provider_requests SET envelope_json=?,input_sha256=? WHERE request_id=?",
                    (json.dumps(envelope, sort_keys=True), historical_input, request["request_id"]),
                )
                connection.execute(
                    "UPDATE jobs SET input_signature=? WHERE run_id=? AND input_signature=?",
                    (historical_input, run_id, request["input_sha256"]),
                )

        checked = service.check_candidate_release(run_id, "26.2")
        assert _ai_item(tmp_path, checked)["status"] == "passed"
    finally:
        service.close()


def test_base_request_containing_refresh_target_fails_closed(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        provenance = _refresh_report(service, run_id)
        with service.worker.open_database(run_id) as database:
            request = database.fetchone(
                "SELECT request_id,envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1"
            )
            assert request is not None
            envelope = json.loads(request["envelope_json"])
            envelope["export_id"] = provenance["base"]["export_id"]
            tile_map = list(envelope["input_summary"]["tile_variant_map"])
            tile_map.append(
                {
                    "tile_id": "T99",
                    "variant_id": releases_module._BANNER_REFRESH_TARGETS[0],
                    "image_sha256": tile_map[0]["image_sha256"],
                    "machine_metadata_sha256": tile_map[0]["machine_metadata_sha256"],
                }
            )
            envelope["input_summary"]["tile_variant_map"] = tile_map
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE provider_requests SET envelope_json=? WHERE request_id=?",
                    (json.dumps(envelope, sort_keys=True), request["request_id"]),
                )
        checked = service.check_candidate_release(run_id, "26.2")
        item = _ai_item(tmp_path, checked)
        assert item["status"] == "failed"
        assert item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_changed_current_provider_input_fails_closed(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        _refresh_report(service, run_id)
        with service.worker.open_database(run_id) as database:
            request = database.fetchone(
                "SELECT request_id,envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1"
            )
            assert request is not None
            envelope = json.loads(request["envelope_json"])
            envelope["input_summary"]["tile_variant_map"][0]["image_sha256"] = "sha256:" + "0" * 64
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE provider_requests SET envelope_json=? WHERE request_id=?",
                    (json.dumps(envelope, sort_keys=True), request["request_id"]),
                )
        checked = service.check_candidate_release(run_id, "26.2")
        item = _ai_item(tmp_path, checked)
        assert item["status"] == "failed"
        assert item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_new_target_request_uses_replacement_export_id(tmp_path: Path, monkeypatch) -> None:
    service, run_id = _ready(tmp_path)
    try:
        with service.worker.open_database(run_id) as database:
            request = database.fetchone(
                "SELECT envelope_json FROM provider_requests WHERE status='succeeded' LIMIT 1"
            )
            assert request is not None
            tile_map = json.loads(request["envelope_json"])["input_summary"]["tile_variant_map"]
        # The generated R2 fixture has no banner rows.  Reusing its exact
        # tile set here exercises the D-045 replacement branch without adding
        # Minecraft assets to the repository; production keeps the frozen 32.
        target_ids = tuple(sorted({item["variant_id"] for item in tile_map}, key=lambda value: value.encode("utf-8")))
        monkeypatch.setattr(releases_module, "_BANNER_REFRESH_TARGETS", target_ids)
        _refresh_report(service, run_id)
        checked = service.check_candidate_release(run_id, "26.2")
        assert _ai_item(tmp_path, checked)["status"] == "passed"
    finally:
        service.close()


def test_malformed_refresh_provenance_is_not_treated_as_legacy_report(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        provenance = _refresh_report(service, run_id)
        provenance.pop("policy_token")
        _refresh_report(service, run_id, provenance=provenance)
        checked = service.check_candidate_release(run_id, "26.2")
        item = _ai_item(tmp_path, checked)
        assert item["status"] == "failed"
        assert item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()


def test_refresh_provenance_alias_wrapper_is_rejected(tmp_path: Path) -> None:
    service, run_id = _ready(tmp_path)
    try:
        value = _refresh_report(service, run_id)
        wrapped = {"status": "passed", "issues": [], "provenance": value}
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                connection.execute(
                    "UPDATE imports SET report_json=? WHERE import_id=(SELECT import_id FROM runs WHERE run_id=?)",
                    (json.dumps(wrapped), run_id),
                )
        checked = service.check_candidate_release(run_id, "26.2")
        item = _ai_item(tmp_path, checked)
        assert item["status"] == "failed"
        assert item["error_code"] == "AI_LINEAGE_INVALID"
    finally:
        service.close()
