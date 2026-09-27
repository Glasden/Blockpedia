from __future__ import annotations
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from blockpedia.web import create_app
from blockpedia.services import R3Error
from tests.r3.test_release_builder import _ready
from tests.r3.test_pipeline_review import _r2_fixture_module
from tests.import_helpers import make_two_visual_export


def _body(release_id, token=None):
    return {"minecraft_version": "26.2", "target_release_id": release_id, "expected_current_sha256": token,
            "confirm": True, "set_as_default": True, "reviewer": "tester", "reason": "fixture publication"}


def test_real_http_build_publish_without_workspace_and_rollback(tmp_path):
    service, run_id = _ready(tmp_path)
    try:
        with TestClient(create_app(service=service, start_worker=False)) as client:
            first_body = {"run_id": run_id, "minecraft_version": "26.2", "release_build_id": "build_" + "a" * 32}
            response = client.post('/api/releases/build', json=first_body)
            assert response.status_code == 201, response.text
            first = response.json()['data']
            run_page = client.get('/runs/' + run_id)
            assert run_page.status_code == 200
            assert 'data-release-candidate data-build-complete="true"' in run_page.text
            assert 'data-new-build' in run_page.text
            assert client.post('/api/releases/build', json=first_body).status_code == 200
            assert not service.data_root.current.exists()
            invalid = client.post('/api/releases/publish', json={**_body(first['release_id']), 'confirm': False})
            assert invalid.status_code == 422
            assert not service.data_root.current.exists()
            published = client.post('/api/releases/publish', json=_body(first['release_id']))
            assert published.status_code == 200, published.text
            assert published.json()['data']['applied'] is True
            second = client.post('/api/releases/build', json={**first_body, 'release_build_id': 'build_' + 'b' * 32}).json()['data']
            workspace = service.data_root.workspace_dir('26.2', run_id)
            shutil.move(workspace, tmp_path / 'archived-workspace')
            listed = client.get('/api/releases?minecraft_version=26.2').json()['data']
            assert len(listed['releases']) == 2
            assert client.get('/releases').status_code == 200
            # Historical checksum data has no role in publication.
            release = tmp_path / second['relative_path']
            (release / 'checksums.sha256').write_text('obsolete data\n')
            response = client.post('/api/releases/publish', json=_body(second['release_id'], listed['current_sha256']))
            assert response.status_code == 200, response.text
            token = response.json()['data']['current_sha256']
            assert client.post('/api/releases/rollback', json=_body(first['release_id'], 'sha256:' + '0' * 64)).status_code == 409
            before = {p.relative_to(release): p.read_bytes() for p in release.rglob('*') if p.is_file()}
            rolled = client.post('/api/releases/rollback', json=_body(first['release_id'], token))
            assert rolled.status_code == 200, rolled.text
            assert rolled.json()['data']['current']['versions']['26.2']['release_id'] == first['release_id']
            assert before == {p.relative_to(release): p.read_bytes() for p in release.rglob('*') if p.is_file()}
    finally:
        service.close()


def test_invalid_build_receipt_fails_closed_without_leaking_path(tmp_path, monkeypatch):
    service, run_id = _ready(tmp_path)
    try:
        build_id = 'build_' + 'a' * 32
        built = service.build_candidate_release(run_id, '26.2', build_id)
        monkeypatch.setattr(service, 'build_candidate_release', lambda *_args: {**built, 'relative_path': '/private/path'})
        with TestClient(create_app(service=service, start_worker=False)) as client:
            response = client.post('/api/releases/build', json={'run_id': run_id, 'minecraft_version': '26.2', 'release_build_id': build_id})
            assert response.status_code == 500
            assert '/private/path' not in response.text
    finally:
        service.close()


@pytest.mark.parametrize('point', ['before_replace', 'after_replace'])
def test_publication_commit_point_and_audit_recovery(tmp_path, point):
    service, run_id = _ready(tmp_path)
    try:
        built = service.build_candidate_release(run_id, '26.2', 'build_' + 'a' * 32)
        def fail(*args):
            raise OSError('injected failure')
        if point == 'before_replace':
            service.activation.before_current_replace = fail
        else:
            service.activation.after_current_replace = fail
        with TestClient(create_app(service=service, start_worker=False)) as client:
            response = client.post('/api/releases/publish', json=_body(built['release_id']))
            if point == 'before_replace':
                assert response.status_code >= 400
                assert not service.data_root.current.exists()
            else:
                assert response.status_code == 200
                assert response.json()['data']['applied'] is True
                assert response.json()['data']['warnings'] == ['PUBLISH_AUDIT_PENDING']
                audit = service.activation.audit_path.read_bytes()
                assert client.get('/api/releases?minecraft_version=26.2').json()['data']['audit_pending'] is True
                assert service.activation.audit_path.read_bytes() == audit
            service.activation.before_current_replace = None
            service.activation.after_current_replace = None
            retried = client.post('/api/releases/publish', json=_body(built['release_id']))
            assert retried.status_code == 200, retried.text
            assert retried.json()['data']['warnings'] == []
            assert service.activation.audit_pending() is False
            events = [json.loads(line) for line in service.activation.audit_path.read_text().splitlines()]
            assert events[-1]['event'] == 'applied'
            assert service.activation.current()[0]['versions']['26.2']['release_id'] == built['release_id']
    finally:
        service.close()


def test_mcp_stdio_reads_new_build_and_observes_next_pointer(tmp_path):
    from tests.r4.test_node_mcp import call, node_session
    export = make_two_visual_export(tmp_path, _r2_fixture_module())
    service, run_id = _ready(tmp_path, export_path=export)
    try:
        first = service.build_candidate_release(run_id, '26.2', 'build_' + 'a' * 32)
        service.publish_release(**_body(first['release_id']))
        before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
        calls = [('index_info', {}), ('search_blocks', {'keywords': ['stone']}),
                 ('get_block_details', {'block_id': 'minecraft:stone'}), ('compare_blocks', {'block_ids': ['minecraft:stone', 'minecraft:glass']})]
        with node_session(tmp_path) as send:
            for index, (name, arguments) in enumerate(calls, start=2):
                assert call(send, index, name, arguments)['isError'] is False
            assert before == {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
            assert call(send, 6, 'index_info', {})['structuredContent']['resolved_release_id'] == first['release_id']
            second = service.build_candidate_release(run_id, '26.2', 'build_' + 'b' * 32)
            _, token = service.activation.current()
            service.publish_release(**_body(second['release_id'], token))
            assert call(send, 7, 'index_info', {})['structuredContent']['resolved_release_id'] == second['release_id']
    finally:
        service.close()


def test_failed_cleanup_releases_switch_lock(tmp_path, monkeypatch):
    from blockpedia.activation import CURRENT_SWITCH_LOCK
    service, run_id = _ready(tmp_path)
    try:
        built = service.build_candidate_release(run_id, '26.2', 'build_' + 'a' * 32)
        original = Path.unlink
        cleanup_attempts = []
        def no_cleanup(path, *args, **kwargs):
            if path.name.startswith('current.json.tmp.'):
                cleanup_attempts.append(path)
                raise OSError('cleanup denied')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'unlink', no_cleanup)
        def no_replace(*args):
            raise OSError('replace denied')
        service.activation.before_current_replace = no_replace
        with pytest.raises(R3Error) as failure:
            service.publish_release(**_body(built['release_id']))
        assert failure.value.code == 'PUBLISH_FAILED' and len(cleanup_attempts) == 1
        assert CURRENT_SWITCH_LOCK.acquire(blocking=False)
        CURRENT_SWITCH_LOCK.release()
        assert not service.data_root.current.exists()
        monkeypatch.setattr(Path, 'unlink', original)
        service.activation.before_current_replace = None
        assert service.publish_release(**_body(built['release_id']))['applied']
    finally:
        service.close()


def test_intent_directory_is_durable_before_current_replace(tmp_path, monkeypatch):
    import blockpedia.activation as activation
    service, run_id = _ready(tmp_path)
    try:
        built = service.build_candidate_release(run_id, '26.2', 'build_' + 'a' * 32)
        original = activation.sync_directory
        order = []
        def failed_sync(path):
            if path == service.data_root.logs:
                order.append('logs-failed')
                raise OSError('directory sync failed')
            return original(path)
        monkeypatch.setattr(activation, 'sync_directory', failed_sync)
        with pytest.raises(R3Error) as failure:
            service.publish_release(**_body(built['release_id']))
        assert failure.value.code == 'PUBLISH_FAILED'
        assert order == ['logs-failed'] and not service.data_root.current.exists()
        def observed_sync(path):
            if path == service.data_root.logs:
                order.append('logs-synced')
            return original(path)
        monkeypatch.setattr(activation, 'sync_directory', observed_sync)
        def before_replace(*args):
            assert order[-1] == 'logs-synced'
            order.append('replace')
        service.activation.before_current_replace = before_replace
        assert service.publish_release(**_body(built['release_id']))['applied']
        assert order.index('logs-synced') < order.index('replace')
    finally:
        service.close()
