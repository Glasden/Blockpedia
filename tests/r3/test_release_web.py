from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from blockpedia.services import R3Error
from blockpedia.web import create_app


HASH = "sha256:" + "0" * 64
CHECK_DATA = {
    "check_id": "check_" + "a" * 32,
    "release_build_id": "build_" + "b" * 32,
    "run_id": "run_01J",
    "minecraft_version": "26.2",
    "status": "passed",
    "can_build": False,
    "snapshot_fingerprint": HASH,
    "quality_report_sha256": HASH,
    "created_at": "2026-08-15T12:00:00Z",
    "updated_at": "2026-08-15T12:00:00Z",
}
BUILD_DATA = {
    "check_id": CHECK_DATA["check_id"],
    "release_build_id": CHECK_DATA["release_build_id"],
    "release_id": "rel_" + "c" * 32,
    "run_id": "run_01J",
    "minecraft_version": "26.2",
    "relative_path": "releases/26.2/rel_" + "c" * 32,
    "status": "built",
    "manifest_sha256": HASH,
    "quality_report_sha256": HASH,
    "checksums_sha256": HASH,
    "built_at": "2026-08-15T12:00:00Z",
}


class StubReleaseService:
    worker = None

    def __init__(self) -> None:
        self.check_calls: list[tuple[str, str]] = []
        self.build_calls: list[tuple[str, bool]] = []
        self.check_result: dict[str, Any] = dict(CHECK_DATA)
        self.build_result: dict[str, Any] = dict(BUILD_DATA)
        self.check_error: str | None = None
        self.build_error: str | None = None

    def stale_markers(self) -> list[object]:
        return []

    def check_candidate_release(self, run_id: str, minecraft_version: str) -> dict[str, Any]:
        self.check_calls.append((run_id, minecraft_version))
        if self.check_error:
            raise R3Error(self.check_error, "C:\\private\\traceback.txt secret=hidden")
        return dict(self.check_result)

    def build_candidate_release(self, check_id: str, *, confirm_immutable_release: bool) -> dict[str, Any]:
        self.build_calls.append((check_id, confirm_immutable_release))
        if self.build_error:
            raise R3Error(self.build_error, "C:\\private\\traceback.txt secret=hidden")
        return dict(self.build_result)


@pytest.fixture
def release_context(tmp_path: Path):
    service = StubReleaseService()
    app = create_app(data_root=tmp_path, service=service, start_worker=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        yield client, service


def _assert_error(response, status: int) -> dict[str, Any]:
    assert response.status_code == status
    payload = response.json()
    assert payload["ok"] is False
    assert isinstance(payload["request_id"], str)
    assert isinstance(payload["error_code"], str)
    assert isinstance(payload["message"], str)
    assert isinstance(payload["field_errors"], dict)
    assert isinstance(payload["retryable"], bool)
    return payload


@pytest.mark.parametrize(
    "body",
    [
        {"run_id": "run_01J"},
        {"minecraft_version": "26.2"},
        {"run_id": 1, "minecraft_version": "26.2"},
        {"run_id": "run_01J", "minecraft_version": 26.2},
        {"run_id": "run_01J", "minecraft_version": "26.2", "extra": True},
    ],
)
def test_release_check_requires_exact_strict_body(release_context, body: dict[str, Any]) -> None:
    client, service = release_context
    response = client.post("/api/releases/check", json=body)
    payload = _assert_error(response, 400)
    assert payload["error_code"] == "INVALID_INPUT"
    assert service.check_calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"check_id": "check_" + "a" * 32},
        {"confirm_immutable_release": True},
        {"check_id": 1, "confirm_immutable_release": True},
        {"check_id": "check_" + "a" * 32, "confirm_immutable_release": 1},
        {"check_id": "check_" + "a" * 32, "confirm_immutable_release": "true"},
        {"check_id": "check_" + "a" * 32, "confirm_immutable_release": False},
        {"check_id": "check_" + "a" * 32, "confirm_immutable_release": True, "extra": True},
    ],
)
def test_release_build_requires_exact_true_strict_body(release_context, body: dict[str, Any]) -> None:
    client, service = release_context
    response = client.post("/api/releases/build", json=body)
    payload = _assert_error(response, 400)
    assert payload["error_code"] == "INVALID_INPUT"
    assert service.build_calls == []


def test_release_check_returns_200_for_blocked_gate_without_changing_backend_data(release_context) -> None:
    client, service = release_context
    service.check_result = dict(CHECK_DATA)
    response = client.post(
        "/api/releases/check",
        json={"run_id": "run_01J", "minecraft_version": "26.2"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert set(data) == set(CHECK_DATA)
    assert data == CHECK_DATA
    assert service.check_calls == [("run_01J", "26.2")]


def test_release_build_returns_201_and_passes_true_confirmation(release_context) -> None:
    client, service = release_context
    response = client.post(
        "/api/releases/build",
        json={"check_id": CHECK_DATA["check_id"], "confirm_immutable_release": True},
    )
    assert response.status_code == 201
    data = response.json()["data"]
    assert set(data) == set(BUILD_DATA)
    assert data == BUILD_DATA
    assert data["manifest_sha256"] == HASH
    assert data["quality_report_sha256"] == HASH
    assert data["checksums_sha256"] == HASH
    assert service.build_calls == [(CHECK_DATA["check_id"], True)]


@pytest.mark.parametrize(
    "code,status",
    [
        ("RUN_NOT_FOUND", 404),
        ("RELEASE_CHECK_NOT_READY", 409),
        ("RELEASE_VERSION_MISMATCH", 409),
        ("DATABASE_SCHEMA_MISMATCH", 422),
        ("RELEASE_CHECK_FAILED", 422),
        ("RELEASE_CHECK_STALE", 409),
    ],
)
def test_release_check_error_mapping(release_context, code: str, status: int) -> None:
    client, service = release_context
    service.check_error = code
    payload = _assert_error(
        client.post(
            "/api/releases/check",
            json={"run_id": "run_01J", "minecraft_version": "26.2"},
        ),
        status,
    )
    assert payload["error_code"] == code
    assert payload["retryable"] is False


@pytest.mark.parametrize(
    "code,status,retryable",
    [
        ("RELEASE_CHECK_NOT_FOUND", 404, False),
        ("RELEASE_CHECK_NOT_READY", 409, False),
        ("RELEASE_ALREADY_BUILT", 409, False),
        ("RELEASE_BUILD_INTEGRITY_FAILED", 422, False),
        ("WORKER_UNAVAILABLE", 503, True),
        ("RELEASE_BUILD_FAILED", 500, False),
    ],
)
def test_release_build_error_mapping(release_context, code: str, status: int, retryable: bool) -> None:
    client, service = release_context
    service.build_error = code
    payload = _assert_error(
        client.post(
            "/api/releases/build",
            json={"check_id": CHECK_DATA["check_id"], "confirm_immutable_release": True},
        ),
        status,
    )
    assert payload["error_code"] == code
    assert payload["retryable"] is retryable


def test_release_adapter_redacts_absolute_paths_secrets_and_trace(release_context) -> None:
    client, service = release_context
    service.check_result = {
        **CHECK_DATA,
        "absolute_path": r"C:\private\release\secret.txt",
        "secret": "sk-test-secret",
        "trace": "Traceback (most recent call last): query=secret",
        "relative_path": r"C:\private\release",
    }
    response = client.post(
        "/api/releases/check",
        json={"run_id": "run_01J", "minecraft_version": "26.2"},
    )
    assert response.status_code == 200
    serialized = response.text
    assert r"C:\private\release\secret.txt" not in serialized
    assert "sk-test-secret" not in serialized
    assert "Traceback" not in serialized
    assert "query=secret" not in serialized
    assert response.json()["data"] == CHECK_DATA

    service.build_error = "RELEASE_BUILD_FAILED"
    error = client.post(
        "/api/releases/build",
        json={"check_id": CHECK_DATA["check_id"], "confirm_immutable_release": True},
    )
    assert r"C:\private\traceback.txt" not in error.text
    assert "secret=hidden" not in error.text


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("check_id", "check_invalid"),
        ("release_build_id", "build_invalid"),
        ("run_id", r"C:\\private\\run"),
        ("minecraft_version", "not-a-version"),
        ("status", "blocked"),
        ("can_build", "false"),
        ("snapshot_fingerprint", "C:\\private\\trace"),
        ("quality_report_sha256", "sha256:bad"),
        ("created_at", "Traceback"),
    ],
)
def test_release_check_invalid_backend_field_fails_closed(
    release_context, field: str, bad_value: Any
) -> None:
    client, service = release_context
    service.check_result = {**CHECK_DATA, field: bad_value}
    response = client.post(
        "/api/releases/check",
        json={"run_id": "run_01J", "minecraft_version": "26.2"},
    )
    payload = _assert_error(response, 422)
    assert payload["error_code"] == "RELEASE_CHECK_FAILED"
    assert "C:\\private" not in response.text
    assert "Traceback" not in response.text


def test_release_check_missing_backend_field_fails_closed(release_context) -> None:
    client, service = release_context
    service.check_result = dict(CHECK_DATA)
    service.check_result.pop("quality_report_sha256")
    response = client.post(
        "/api/releases/check",
        json={"run_id": "run_01J", "minecraft_version": "26.2"},
    )
    payload = _assert_error(response, 422)
    assert payload["error_code"] == "RELEASE_CHECK_FAILED"


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("check_id", "check_invalid"),
        ("release_build_id", "build_invalid"),
        ("release_id", "rel_invalid"),
        ("run_id", r"C:\\private\\run"),
        ("minecraft_version", "not-a-version"),
        ("relative_path", r"C:\\private\\release"),
        ("status", "passed"),
        ("manifest_sha256", "sha256:bad"),
        ("quality_report_sha256", "Traceback"),
        ("checksums_sha256", "C:\\private\\checksums"),
        ("built_at", "not-a-time"),
    ],
)
def test_release_build_invalid_backend_field_fails_closed(
    release_context, field: str, bad_value: Any
) -> None:
    client, service = release_context
    service.build_result = {**BUILD_DATA, field: bad_value}
    response = client.post(
        "/api/releases/build",
        json={"check_id": CHECK_DATA["check_id"], "confirm_immutable_release": True},
    )
    payload = _assert_error(response, 500)
    assert payload["error_code"] == "RELEASE_BUILD_FAILED"
    assert "C:\\private" not in response.text
    assert "Traceback" not in response.text


def test_release_build_missing_backend_field_fails_closed(release_context) -> None:
    client, service = release_context
    service.build_result = dict(BUILD_DATA)
    service.build_result.pop("checksums_sha256")
    response = client.post(
        "/api/releases/build",
        json={"check_id": CHECK_DATA["check_id"], "confirm_immutable_release": True},
    )
    payload = _assert_error(response, 500)
    assert payload["error_code"] == "RELEASE_BUILD_FAILED"


def test_release_route_surface_has_only_phase_c_routes_and_no_activation_or_mcp(release_context) -> None:
    client, _ = release_context
    paths = {route.path for route in client.app.routes}
    assert {path for path in paths if path.startswith("/api/releases")} == {
        "/api/releases/check",
        "/api/releases/build",
    }
    for path in (
        "/api/releases/activation-check",
        "/api/releases/apply",
        "/api/releases/rollback",
        "/api/releases/cleanup",
        "/api/releases/unknown",
        "/api/current",
        "/api/search-tests",
        "/api/mcp",
    ):
        assert client.post(path, json={}).status_code == 404
