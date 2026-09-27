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
from tests.import_helpers import import_export
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


@pytest.mark.parametrize("workers", [1, 2, 5])
def test_feature_workers_frozen_at_import_and_exposed_after_restart(tmp_path, export_fixture, workers):
    from blockpedia.web import create_app
    from blockpedia.importer import ImportConflict

    service = StudioService(DataRoot(tmp_path))
    run_id = "run_" + "a" * 32
    try:
        with TestClient(create_app(service=service, start_worker=False)) as client:
            assert 'name="feature_workers"' in client.get('/').text
            ref = service.directory_chooser.register_path(export_fixture, "26.2")
            body = dict(run_id=run_id, source_directory_ref=ref, minecraft_version="26.2", feature_workers=workers)
            assert client.post('/api/imports', json=body).status_code == 202
            assert service.imports.wait(run_id)["status"] == "succeeded"
            assert client.post('/api/imports', json=body).status_code == 200
            snapshot = client.get('/api/runs/' + run_id).json()['data']
            assert snapshot['config_snapshot']['feature_workers'] == workers
            assert f'{workers} 个特征计算进程' in client.get('/runs/' + run_id).text
            assert client.post('/api/imports', json={**body, 'feature_workers': 3}).status_code == 409
    finally:
        service.close()
    reopened = StudioService(DataRoot(tmp_path))
    try:
        assert reopened.get_import(run_id)['feature_workers'] == workers
        assert reopened.get_run(run_id)['config_snapshot']['feature_workers'] == workers
        with pytest.raises(ImportConflict):
            reopened.start_import(run_id, 'unused', '26.2', feature_workers=3)
    finally:
        reopened.close()


@pytest.mark.parametrize("workers", [0, 6, True, 2.0, "2", None])
def test_feature_workers_rejects_non_integer_or_out_of_range(tmp_path, workers):
    from blockpedia.web import create_app
    from blockpedia.importer import ImportNotAllowed

    service = StudioService(DataRoot(tmp_path))
    try:
        with TestClient(create_app(service=service, start_worker=False)) as client:
            body = dict(run_id="run_" + "b" * 32, source_directory_ref="unused", minecraft_version="26.2", feature_workers=workers)
            assert client.post('/api/imports', json=body).status_code == 422
            with pytest.raises(ImportNotAllowed):
                service.start_import(body['run_id'], 'unused', '26.2', feature_workers=workers)
    finally:
        service.close()


def test_feature_workers_default_is_serial(tmp_path, export_fixture):
    service = StudioService(DataRoot(tmp_path))
    try:
        imported = import_export(service, export_fixture)
        assert service.get_run(imported['run_id'])['config_snapshot']['feature_workers'] == 1
    finally:
        service.close()


@pytest.mark.parametrize("failure_point", ["copy", "rename"])
def test_feature_workers_frozen_before_import_survives_restart(tmp_path, export_fixture, monkeypatch, failure_point):
    from blockpedia import importer
    from tools.validate_r1_export import Validator
    from blockpedia.web import create_app

    target, attribute = (Validator, "run") if failure_point == "copy" else (importer, "commit_directory")
    original = getattr(target, attribute)
    def fail(*args, **kwargs):
        raise OSError("interrupted import")
    monkeypatch.setattr(target, attribute, fail)
    run_id = "run_" + "f" * 32
    service = StudioService(DataRoot(tmp_path))
    try:
        ref = service.directory_chooser.register_path(export_fixture, "26.2")
        service.start_import(run_id, ref, "26.2", feature_workers=5)
        assert service.imports.wait(run_id)["status"] == "failed"
        assert service.list_runs() == []
    finally:
        service.close()
    monkeypatch.setattr(target, attribute, original)
    reopened = StudioService(DataRoot(tmp_path))
    try:
        interrupted = reopened.get_import(run_id)
        assert interrupted["status"] == "interrupted"
        assert interrupted["feature_workers"] == 5
        assert reopened.list_runs() == []
        with TestClient(create_app(service=reopened, start_worker=False)) as client:
            ref = reopened.directory_chooser.register_path(export_fixture, "26.2")
            body = dict(run_id=run_id, source_directory_ref=ref, minecraft_version="26.2")
            assert client.post('/api/imports', json={**body, "feature_workers": 1}).status_code == 409
            assert client.post('/api/imports', json={**body, "feature_workers": 5}).status_code == 202
            assert reopened.imports.wait(run_id)["status"] == "succeeded"
            assert reopened.get_run(run_id)["config_snapshot"]["feature_workers"] == 5
    finally:
        reopened.close()


def test_interrupted_import_with_unknown_config_rejects_new_value(tmp_path):
    from blockpedia.importer import ImportConflict

    run_id = "run_" + "e" * 32
    (tmp_path / "workspace" / "26.2" / ("." + run_id + ".staging")).mkdir(parents=True)
    service = StudioService(DataRoot(tmp_path))
    try:
        assert service.get_import(run_id)["feature_workers"] is None
        with pytest.raises(ImportConflict):
            service.start_import(run_id, "unused", "26.2", feature_workers=1)
    finally:
        service.close()





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
    assert chooser.consume(ref, "26.2") == export_fixture


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




def test_run_snapshot_exposes_safe_progress_and_latest_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, export_fixture: Path
) -> None:
    from conftest import PassingToolchainProbe

    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    try:
        run_id = import_export(service, export_fixture)["run_id"]
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








def test_one_import_has_memory_progress_and_single_copy(monkeypatch, tmp_path, export_fixture):
    import threading
    from tools.validate_r1_export import Validator
    from blockpedia import local_files
    entered, release = threading.Event(), threading.Event()
    calls = []
    writes = []
    original = Validator.run
    write = local_files.write_bytes
    def observed_run(self, *, on_progress=None):
        calls.append(self.export_dir)
        def progress(phase, completed, total, unit):
            on_progress(phase, completed, total, unit)
            if phase == "JSONL_RECORDS" and completed == 1:
                entered.set()
                assert release.wait(5)
        return original(self, on_progress=progress)
    def observed_write(path, payload, root):
        writes.append(path)
        return write(path, payload, root)
    monkeypatch.setattr(Validator, "run", observed_run)
    monkeypatch.setattr(local_files, "write_bytes", observed_write)
    service = StudioService(DataRoot(tmp_path))
    try:
        app = __import__("blockpedia.web", fromlist=["create_app"]).create_app(service=service, start_worker=False)
        with TestClient(app) as client:
            ref = client.get('/api/directories?minecraft_version=26.2').json()['data']['entries'][0]['directory_ref']
            body = {'run_id': 'run_' + 'a' * 32, 'minecraft_version': '26.2', 'source_directory_ref': ref}
            assert client.post('/api/imports', json=body).status_code == 202
            assert entered.wait(5)
            value = client.get('/api/imports/' + body['run_id']).json()['data']
            assert value['status'] == 'running' and value['progress']['completed'] == 1
            assert client.post('/api/imports', json=body).status_code == 202
            assert not (tmp_path / 'cache' / 'import-checks').exists()
            release.set()
            result = service.imports.wait(body['run_id'])
            assert result['status'] == 'succeeded'
            assert client.post('/api/imports', json=body).status_code == 200
            assert len(calls) == 1 and len(writes) == len(set(writes))
            assert not any(path.name == 'checksums.sha256' for path in writes)
            assert 'succeeded' in client.get('/api/imports/' + body['run_id'] + '/events').text
    finally:
        release.set()
        service.close()
    reopened = StudioService(DataRoot(tmp_path))
    try:
        assert reopened.get_import(body['run_id'])['import_id'] == result['import_id']
    finally:
        reopened.close()


def test_interrupted_import_retries_same_identity_after_restart(tmp_path, export_fixture, monkeypatch):
    from blockpedia import importer
    original = importer.commit_directory
    def fail(*args):
        raise OSError('interrupted before rename')
    monkeypatch.setattr(importer, 'commit_directory', fail)
    run_id = 'run_' + 'd' * 32
    service = StudioService(DataRoot(tmp_path))
    try:
        ref = service.directory_chooser.register_path(export_fixture, '26.2')
        service.start_import(run_id, ref, '26.2')
        assert service.imports.wait(run_id)['status'] == 'failed'
    finally:
        service.close()
    monkeypatch.setattr(importer, 'commit_directory', original)
    service = StudioService(DataRoot(tmp_path))
    try:
        assert service.get_import(run_id)['status'] == 'interrupted'
        app = __import__('blockpedia.web', fromlist=['create_app']).create_app(service=service, start_worker=False)
        with TestClient(app) as client:
            assert '?retry_run_id=' + run_id in client.get('/imports/' + run_id).text
            new_ref = client.get('/api/directories?minecraft_version=26.2').json()['data']['entries'][0]['directory_ref']
            assert new_ref != ref
            assert client.post('/api/imports', json={'run_id': run_id, 'source_directory_ref': new_ref, 'minecraft_version': '26.2'}).status_code == 202
            assert service.imports.wait(run_id)['status'] == 'succeeded'
            assert len(list((tmp_path / 'workspace' / '26.2').glob('run_*/work.sqlite3'))) == 1
    finally:
        service.close()
