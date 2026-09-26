from __future__ import annotations

from typing import Any
import pytest
from fastapi.testclient import TestClient
from blockpedia.services import R3Error
from blockpedia.web import create_app

HASH = 'sha256:' + '0' * 64
RUN = 'run_' + 'a' * 32
BUILD = 'build_' + 'b' * 32
RELEASE = 'rel_' + 'c' * 32
BUILD_DATA = dict(release_build_id=BUILD, release_id=RELEASE, run_id=RUN,
                  minecraft_version='26.2', relative_path='releases/26.2/' + RELEASE,
                  status='built', manifest_sha256=HASH, quality_report_sha256=HASH,
                  built_at='2026-09-26T12:00:00Z')
BUILD_BODY = dict(run_id=RUN, minecraft_version='26.2', release_build_id=BUILD)
PUBLISH_BODY = dict(minecraft_version='26.2', target_release_id=RELEASE,
                    expected_current_sha256=None, confirm=True, set_as_default=True,
                    reviewer='operator', reason='first release')

class StubReleaseService:
    worker = None
    def __init__(self):
        self.calls = []
        self.build_result = dict(BUILD_DATA)
        self.error = None
    def stale_markers(self): return []
    def build_candidate_release(self, run_id, minecraft_version, release_build_id):
        self.calls.append((run_id, minecraft_version, release_build_id))
        if self.error: raise R3Error(self.error, 'C:\\private\\secret.txt')
        return dict(self.build_result)
    def list_releases(self, minecraft_version):
        self.calls.append(('list', minecraft_version))
        return dict(minecraft_version=minecraft_version, releases=[BUILD_DATA], current=None, current_sha256=None)
    def publish_release(self, **kwargs):
        self.calls.append(kwargs)
        if self.error: raise R3Error(self.error, 'private diagnostic')
        return dict(applied=True, minecraft_version=kwargs['minecraft_version'], target_release_id=RELEASE,
                    current={'release_id': RELEASE}, current_sha256=HASH,
                    status='rolled_back' if kwargs['rollback'] else 'published', warnings=['AUDIT_PENDING'])

@pytest.fixture
def release_context():
    service = StubReleaseService()
    with TestClient(create_app(service=service, start_worker=False)) as client:
        yield client, service

@pytest.mark.parametrize('body', [dict(check_id='old', confirm_immutable_release=True), {},
    {**BUILD_BODY, 'check_id': 'old'}, {**BUILD_BODY, 'release_build_id': 'build_invalid'},
    {**BUILD_BODY, 'minecraft_version': 26.2}])
def test_build_strict_three_fields(release_context, body):
    client, service = release_context
    assert client.post('/api/releases/build', json=body).status_code == 422
    assert not service.calls

@pytest.mark.parametrize('reused,status', [(False, 201), (True, 200)])
def test_build_passes_client_identity_and_preserves_receipt(release_context, reused, status):
    client, service = release_context
    service.build_result['reused'] = reused
    response = client.post('/api/releases/build', json=BUILD_BODY)
    assert response.status_code == status
    assert response.json()['data'] == {**BUILD_DATA, 'reused': reused}
    assert service.calls == [(RUN, '26.2', BUILD)]

@pytest.mark.parametrize('field,value', [('relative_path', '/private/path'), ('manifest_sha256', 'bad'), ('release_build_id', 'bad')])
def test_build_receipt_fails_closed(release_context, field, value):
    client, service = release_context
    service.build_result[field] = value
    response = client.post('/api/releases/build', json=BUILD_BODY)
    assert response.status_code == 500
    assert '/private/path' not in response.text

@pytest.mark.parametrize('route', ['publish', 'rollback'])
def test_publish_confirmation_and_audit_warning_are_success(release_context, route):
    client, service = release_context
    response = client.post('/api/releases/' + route, json=PUBLISH_BODY)
    assert response.status_code == 200
    assert response.json()['data']['applied'] is True
    assert response.json()['data']['warnings'] == ['AUDIT_PENDING']
    assert service.calls == [{**PUBLISH_BODY, 'rollback': route == 'rollback'}]

@pytest.mark.parametrize('change', [dict(confirm=False), dict(confirm=1), dict(set_as_default='true'),
    dict(reviewer=' '), dict(reason='\n'), dict(expected_current_sha256='bad'), dict(check_id='old')])
def test_publish_rejects_invalid_input(release_context, change):
    client, service = release_context
    assert client.post('/api/releases/publish', json={**PUBLISH_BODY, **change}).status_code == 422
    assert not service.calls

@pytest.mark.parametrize('missing', ['set_as_default', 'expected_current_sha256'])
def test_publish_requires_explicit_default_and_current_token(release_context, missing):
    client, service = release_context
    body = dict(PUBLISH_BODY); body.pop(missing)
    assert client.post('/api/releases/publish', json=body).status_code == 422
    assert not service.calls

@pytest.mark.parametrize('code,status', [('RUN_NOT_FOUND',404), ('RELEASE_NOT_FOUND',404),
    ('RELEASE_BUILD_CONFLICT',409), ('RELEASE_FORMAT_UNSUPPORTED',409), ('CURRENT_SWITCH_BUSY',409)])
def test_release_stable_errors(release_context, code, status):
    client, service = release_context
    service.error = code
    response = client.post('/api/releases/build', json=BUILD_BODY)
    assert response.status_code == status
    assert response.json()['error_code'] == code
    assert 'private' not in response.text

def test_release_list_and_page_need_no_workspace(release_context):
    client, service = release_context
    assert client.get('/releases').status_code == 200
    assert service.calls == []
    assert client.get('/api/releases').status_code == 422
    assert client.get('/api/releases?minecraft_version=26.2&minecraft_version=1.0').status_code == 422
    response = client.get('/api/releases?minecraft_version=26.2')
    assert response.status_code == 200
    assert response.json()['data']['current_sha256'] is None
    assert service.calls == [('list', '26.2')]

def test_removed_endpoints_have_no_routes(release_context):
    client, _ = release_context
    paths = {route.path for route in client.app.routes}
    assert {path for path in paths if path.startswith('/api/releases')} == {
        '/api/releases', '/api/releases/build', '/api/releases/publish', '/api/releases/rollback'}
    for route in ['/api/releases/check', '/api/releases/activation-check', '/api/releases/apply', '/ui/imports/check', '/imports/checks/old']:
        response = client.get(route) if route.startswith('/imports/') else client.post(route, json={})
        assert response.status_code == 404
    assert not any('banner-export-refresh' in path or '/imports/checks' in path for path in paths)


def test_run_page_keeps_build_controls_after_new_terminal_state(release_context, monkeypatch):
    client, service = release_context
    run = dict(run_id=RUN, minecraft_version='26.2', current_stage='BUILD_RELEASE',
               status='pending', boundary_event='R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING')
    monkeypatch.setattr(service, 'get_run', lambda run_id: dict(run), raising=False)
    before = client.get('/runs/' + RUN)
    assert before.status_code == 200
    assert 'data-release-candidate' in before.text
    assert client.post('/api/releases/build', json=BUILD_BODY).status_code == 201
    # The new builder's _finish() writes this terminal state.
    run.update(status='succeeded', current_stage='BUILD_RELEASE', boundary_event='RELEASE_BUILT')
    refreshed = client.get('/runs/' + RUN)
    assert refreshed.status_code == 200
    assert 'data-release-candidate data-build-complete="true"' in refreshed.text
    assert 'data-candidate-build disabled' in refreshed.text
    assert 'data-new-build' in refreshed.text
    assert '该运行已构建候选' in refreshed.text
    assert 'data-ai-configure' not in refreshed.text
    # A retained client ID reads the same receipt, instead of making a new build.
    service.build_result['reused'] = True
    retried = client.post('/api/releases/build', json=BUILD_BODY)
    assert retried.status_code == 200
    assert retried.json()['data']['release_build_id'] == BUILD
