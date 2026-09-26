from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from blockpedia.importer import ImportNotFound, ImportConflict, ImportNotAllowed
from blockpedia.web import create_app

RUN = 'run_' + 'a' * 32
BODY = dict(run_id=RUN, source_directory_ref='dir_' + 'a' * 48, minecraft_version='26.2')

class ImportServiceStub:
    worker = None
    def __init__(self):
        self.calls = []
        self.status = 'running'
        self.error = None
    def stale_markers(self): return []
    def snapshot(self):
        return dict(run_id=RUN, minecraft_version='26.2', export_id='exp_fixture', import_id=None,
                    status=self.status, phase='VALIDATE_EXPORT', progress=dict(completed=2,total=5,unit='items'),
                    error_code=None, created_at='2026-09-26T00:00:00Z', updated_at='2026-09-26T00:00:00Z')
    def start_import(self, run_id, source_directory_ref, minecraft_version):
        self.calls.append((run_id, source_directory_ref, minecraft_version))
        if self.error: raise self.error('private path')
        return self.snapshot()
    def get_import(self, run_id):
        if run_id != RUN: raise ImportNotFound()
        return self.snapshot()
    def list_imports(self, minecraft_version=None, limit=20): return [self.snapshot()]
    def list_directories(self, minecraft_version, parent_ref): return {'entries': []}
    def list_runs(self): return []

@pytest.fixture
def context():
    service = ImportServiceStub()
    with TestClient(create_app(service=service, start_worker=False)) as client:
        yield client, service

def test_one_import_and_existing_success(context):
    client, service = context
    assert client.post('/api/imports', json=BODY).status_code == 202
    service.status = 'succeeded'
    response = client.post('/api/imports', json=BODY)
    assert response.status_code == 200
    assert service.calls == [(RUN, BODY['source_directory_ref'], '26.2')] * 2
    assert client.get('/api/imports/' + RUN).json()['data']['status'] == 'succeeded'
    assert client.get('/api/imports').json()['data']['imports'][0]['run_id'] == RUN
    assert client.get('/imports/' + RUN).status_code == 200
    assert '/runs/' + RUN in client.get('/imports/' + RUN).text
    stream = client.get('/api/imports/' + RUN + '/events')
    assert stream.status_code == 200
    assert 'data-import-fragment' in stream.text
    assert 'succeeded' in stream.text

@pytest.mark.parametrize('error,status', [(ImportNotFound,404), (ImportConflict,409), (ImportNotAllowed,422)])
def test_import_errors(context, error, status):
    client, service = context; service.error = error
    response = client.post('/api/imports', json=BODY)
    assert response.status_code == status
    assert response.json()['error_code'] == error.code
    assert 'private path' not in response.text

@pytest.mark.parametrize('body', [{}, {**BODY,'check_id':'old'}, {**BODY,'run_id':'invalid'}, {**BODY,'copy_mode':'copy_to_workspace'}])
def test_import_strict_body(context, body):
    client, service = context
    assert client.post('/api/imports', json=body).status_code == 422
    assert not service.calls

def test_import_home_and_chooser_do_not_read_checks(context):
    client, _ = context
    assert client.get('/').status_code == 200
    assert client.get('/api/directories?minecraft_version=26.2').json()['data'] == {'entries': []}
    assert client.get('/api/imports/run_unknown').status_code == 404
    assert client.get('/api/imports/checks/old').status_code == 404
