from __future__ import annotations

import asyncio
import hashlib
import importlib.resources
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from blockpedia.directory_chooser import DirectoryPathUnsafe, DirectoryRefInvalid, DirectoryRefStale, DirectoryChooser
from blockpedia.paths import DataRoot
from blockpedia.services import StudioService
from blockpedia.web import (
    _import_event_stream,
    _run_event_stream,
    sse_heartbeat_comment,
    sse_snapshot_event,
)


def _fake_validator(monkeypatch: pytest.MonkeyPatch, *, entered=None, release=None, progress=(1, 1, "files")):
    from tools import validate_r1_export

    calls: list[tuple[Path, Path]] = []

    def validate(repo_root, snapshot_dir, *, on_progress):
        calls.append((Path(repo_root), Path(snapshot_dir)))
        on_progress("VALIDATE_EXPORT", progress[0], progress[1], progress[2])
        if entered is not None:
            entered.set()
        if release is not None:
            assert release.wait(10)
        return {"status": "passed", "issues": []}

    monkeypatch.setattr(validate_r1_export, "validate_export", validate)
    return calls


def _wait_check(service: StudioService, check_id: str, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = service.get_import_check(check_id)
        if result.status in {"passed", "failed"}:
            return result
        time.sleep(0.02)
    raise AssertionError("import check did not reach a terminal state")


def test_directory_refs_are_opaque_and_stale_or_traversal_is_rejected(tmp_path: Path, export_fixture: Path) -> None:
    chooser = DirectoryChooser(DataRoot(tmp_path))
    listing = chooser.list_directories("26.2")
    entry = next(item for item in listing["entries"] if item["export_id"] == export_fixture.name)
    ref = entry["directory_ref"]
    assert ref.startswith("dir_")
    assert str(export_fixture) not in ref
    assert not Path(ref).is_absolute()

    with pytest.raises(DirectoryRefInvalid):
        chooser.list_directories("26.2", "../outside")

    export_fixture.rename(export_fixture.with_name("old_export"))
    export_fixture.mkdir()
    with pytest.raises(DirectoryRefStale):
        chooser.consume(ref, "26.2")


def test_directory_symlink_is_rejected_when_platform_allows_creation(tmp_path: Path, export_fixture: Path) -> None:
    link = export_fixture.parent / "export_20260814T120001Z"
    try:
        link.symlink_to(export_fixture, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable: {type(exc).__name__}")
    with pytest.raises(DirectoryPathUnsafe):
        DirectoryChooser(DataRoot(tmp_path)).list_directories("26.2")


@pytest.mark.skipif(os.name != "nt", reason="junctions are Windows reparse points")
def test_directory_junction_is_rejected_when_platform_allows_creation(tmp_path: Path, export_fixture: Path) -> None:
    junction = export_fixture.parent / "export_20260814T120002Z"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(export_fixture)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip("junction creation unavailable")
    with pytest.raises(DirectoryPathUnsafe):
        DirectoryChooser(DataRoot(tmp_path)).list_directories("26.2")


def test_async_check_refresh_sse_and_import_call_validator_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_fixture: Path
) -> None:
    # Threading events are required by the in-process executor.
    import threading

    entered_thread = threading.Event()
    release_thread = threading.Event()
    calls = _fake_validator(
        monkeypatch,
        entered=entered_thread,
        release=release_thread,
        progress=(7, 10, "records"),
    )
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2])
    app = __import__("blockpedia.web", fromlist=["create_app"]).create_app(
        data_root=DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], service=service, start_worker=False
    )
    try:
        with TestClient(app) as client:
            entry = next(
                item
                for item in client.get("/api/directories", params={"minecraft_version": "26.2"}).json()["data"]["entries"]
                if item["export_id"] == export_fixture.name
            )
            ref = entry["directory_ref"]
            started = client.post(
                "/api/imports/check",
                json={"source_directory": ref, "minecraft_version": "26.2"},
            )
            assert started.status_code == 202
            check_id = started.json()["data"]["check_id"]
            assert entered_thread.wait(5)
            refreshed = client.get(f"/api/imports/checks/{check_id}")
            assert refreshed.status_code == 200
            refreshed_data = refreshed.json()["data"]
            assert refreshed_data["status"] == "running"
            assert refreshed_data["progress"] == {"completed": 7, "total": 10, "unit": "records"}

            state_dir = tmp_path / "cache" / "import-checks" / check_id
            assert ref not in (state_dir / "state.json").read_text(encoding="utf-8")
            assert str(export_fixture) not in (state_dir / "state.json").read_text(encoding="utf-8")

            release_thread.set()
            final = _wait_check(service, check_id)
            assert final.status == "passed"
            page = client.get(f"/imports/checks/{check_id}")
            assert page.status_code == 200
            assert check_id in page.text
            assert f'name="check_id"' in page.text
            imported = client.post(
                "/api/imports",
                json={"check_id": check_id, "copy_mode": "copy_to_workspace"},
            )
            assert imported.status_code == 200
            assert len(calls) == 1

            with client.stream("GET", f"/api/imports/checks/{check_id}/events") as response:
                lines = list(response.iter_lines())
                assert response.headers["cache-control"] == "no-cache, no-transform"
                assert response.headers["x-accel-buffering"] == "no"
            assert "event: snapshot" in lines
            data_lines = [line for line in lines if line.startswith("data: ")]
            packet = json.loads(data_lines[0][len("data: ") :])
            assert set(packet) == {"snapshot", "html"}
            assert "data-import-fragment" in packet["html"]
    finally:
        release_thread.set()
        service.close()


def test_run_snapshot_exposes_safe_progress_and_latest_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_fixture: Path
) -> None:
    _fake_validator(monkeypatch)
    from conftest import PassingToolchainProbe

    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    try:
        ref = service.list_directories("26.2")["entries"][0]["directory_ref"]
        check = service.start_import_check(ref, "26.2")
        assert _wait_check(service, check.check_id).status == "passed"
        run_id = service.import_checked(check.check_id)["run_id"]
        for _ in range(6):
            service.tick(run_id)
        snapshot = service.get_run(run_id)
        assert snapshot["item_progress"]["total"] >= 1
        assert snapshot["item_progress"]["completed"] >= 0
        assert snapshot["progress"]["r2_total_stages"] == 6
        assert snapshot["latest_steps"]
        assert all(set(step) == {"code", "label", "status", "created_at"} for step in snapshot["latest_steps"])
        serialized = json.dumps(snapshot, ensure_ascii=False)
        assert "cursor_json" not in serialized
        assert "details_json" not in serialized
        assert str(tmp_path) not in serialized
    finally:
        service.close()


def test_sse_frame_heartbeat_reconnect_and_disconnect_are_read_only(tmp_path: Path) -> None:
    payload = {"run_id": "run_test", "status": "running"}
    frame = sse_snapshot_event(payload, '<section data-run-fragment></section>')
    assert "id:" not in frame
    assert "event: snapshot" in frame
    assert json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data:")))["snapshot"] == payload
    assert sse_heartbeat_comment() == ": heartbeat\n\n"

    service = StudioService(DataRoot(tmp_path))
    app = __import__("blockpedia.web", fromlist=["create_app"]).create_app(
        data_root=DataRoot(tmp_path), service=service, start_worker=False
    )
    templates = app.state.templates

    class Request:
        def __init__(self):
            self.disconnect_checks = 0

        async def is_disconnected(self):
            self.disconnect_checks += 1
            return self.disconnect_checks > 0

    initial = {
        "run_id": "run_test",
        "status": "running",
        "current_stage": "PREPARE",
        "stages": [],
        "jobs": [],
        "item_progress": {},
        "progress": {"r2_completed_stages": 0, "r2_total_stages": 6, "r2_percent": 0},
        "stale": [],
        "warnings": [],
    }

    class NoReadAfterInitial:
        def __init__(self):
            self.calls = 0

        def get_run(self, _run_id):
            self.calls += 1
            raise AssertionError("disconnect must not trigger another snapshot read")

    fake = NoReadAfterInitial()

    async def collect_once():
        request = Request()
        stream = _run_event_stream(cast(Any, fake), "run_test", cast(Any, request), templates, initial)
        retry = await anext(stream)
        snapshot = await anext(stream)
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        return retry, snapshot, request

    retry, snapshot, request = asyncio.run(collect_once())
    assert retry == "retry: 2000\n\n"
    assert "event: snapshot" in snapshot
    assert "data-run-fragment" in snapshot
    assert request.disconnect_checks == 1
    assert fake.calls == 0

    # A new generator starts with a complete snapshot again; no replay ID is
    # needed to recover after a disconnected client.
    retry_again, snapshot_again, _ = asyncio.run(collect_once())
    assert retry_again == retry
    assert snapshot_again == snapshot
    service.close()


def test_workspace_schema_and_packaged_hash_are_unchanged() -> None:
    sql = importlib.resources.files("blockpedia").joinpath("sql", "workspace.v1.sql").read_bytes()
    packaged = importlib.resources.files("blockpedia").joinpath("sql", "workspace.v1.sha256").read_text(encoding="ascii").strip()
    assert packaged == "sha256:" + hashlib.sha256(sql).hexdigest()
    assert packaged == "sha256:04b240e87a0650aa5f9a798f0d112e27496cdbeb6513b620c9f8e597210ac73b"


def test_active_check_deduplicates_and_passed_check_reuses_unchanged_anchors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_fixture: Path
) -> None:
    import threading

    entered = threading.Event()
    release = threading.Event()
    calls = _fake_validator(monkeypatch, entered=entered, release=release)
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2])
    try:
        ref = service.list_directories("26.2")["entries"][0]["directory_ref"]
        first = service.start_import_check(ref, "26.2")
        assert entered.wait(5)
        second = service.start_import_check(ref, "26.2")
        assert second.check_id == first.check_id
        assert getattr(second, "reused", False) is True
        assert getattr(second, "response_status", None) == 202
        release.set()
        assert _wait_check(service, first.check_id).status == "passed"

        reused = service.start_import_check(ref, "26.2")
        assert reused.check_id == first.check_id
        assert getattr(reused, "reused", False) is True
        assert getattr(reused, "response_status", None) == 200
        assert len(calls) == 1

        manifest = export_fixture / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b"\n")
        changed = service.start_import_check(ref, "26.2")
        assert changed.check_id != first.check_id
        assert _wait_check(service, changed.check_id).status == "passed"
        assert len(calls) == 2
    finally:
        release.set()
        service.close()


def test_import_uses_frozen_snapshot_after_chooser_ref_is_removed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_fixture: Path
) -> None:
    _fake_validator(monkeypatch)
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2])
    app = __import__("blockpedia.web", fromlist=["create_app"]).create_app(
        data_root=DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], service=service, start_worker=False
    )
    try:
        with TestClient(app) as client:
            entry = next(
                item
                for item in client.get("/api/directories", params={"minecraft_version": "26.2"}).json()["data"]["entries"]
                if item["export_id"] == export_fixture.name
            )
            ref = entry["directory_ref"]
            started = client.post(
                "/api/imports/check",
                json={"source_directory": ref, "minecraft_version": "26.2"},
            )
            check_id = started.json()["data"]["check_id"]
            assert _wait_check(service, check_id).status == "passed"

            with service.directory_chooser._lock:
                service.directory_chooser._refs.pop(ref, None)

            first = client.post("/api/imports", json={"check_id": check_id, "copy_mode": "copy_to_workspace"})
            assert first.status_code == 200
            first_data = first.json()["data"]
            run_id = first_data["run_id"]
            import_id = first_data["import_id"]
            workspace = tmp_path / "workspace" / "26.2" / run_id
            assert workspace.is_dir()
            assert (workspace / "work.sqlite3").is_file()
            assert len([path for path in (tmp_path / "workspace" / "26.2").iterdir() if (path / "work.sqlite3").is_file()]) == 1

            duplicate = client.post("/api/imports", json={"check_id": check_id, "copy_mode": "copy_to_workspace"})
            assert duplicate.status_code == 200
            assert duplicate.json()["data"]["run_id"] == run_id
            assert duplicate.json()["data"]["import_id"] == import_id
    finally:
        service.close()


def test_import_is_idempotent_and_catalog_exposes_safe_check_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_fixture: Path
) -> None:
    _fake_validator(monkeypatch)
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2])
    app = __import__("blockpedia.web", fromlist=["create_app"]).create_app(
        data_root=DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], service=service, start_worker=False
    )
    try:
        with TestClient(app) as client:
            entry = next(
                item
                for item in client.get("/api/directories", params={"minecraft_version": "26.2"}).json()["data"]["entries"]
                if item["export_id"] == export_fixture.name
            )
            assert entry["check_marker"] is None
            started = client.post(
                "/api/imports/check",
                json={"source_directory": entry["directory_ref"], "minecraft_version": "26.2"},
            )
            check_id = started.json()["data"]["check_id"]
            assert _wait_check(service, check_id).status == "passed"
            marked = client.get("/api/directories", params={"minecraft_version": "26.2"}).json()["data"]["entries"]
            marker = next(item["check_marker"] for item in marked if item["export_id"] == export_fixture.name)
            assert marker["status"] == "passed"
            assert marker["check_id"] == check_id
            assert str(tmp_path) not in json.dumps(marker)

            first = client.post("/api/imports", json={"check_id": check_id, "copy_mode": "copy_to_workspace"})
            second = client.post("/api/imports", json={"check_id": check_id, "copy_mode": "copy_to_workspace"})
            assert first.status_code == second.status_code == 200
            assert first.json()["data"]["import_id"] == second.json()["data"]["import_id"]
            assert first.json()["data"]["run_id"] == second.json()["data"]["run_id"]
            catalog = client.get("/api/imports/checks", params={"minecraft_version": "26.2"})
            assert catalog.status_code == 200
            assert catalog.json()["data"]["checks"][0]["check_id"] == check_id
    finally:
        service.close()
