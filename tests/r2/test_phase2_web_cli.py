from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from blockpedia import cli
from blockpedia.paths import DataRoot
from blockpedia.services import StudioService
from blockpedia.worker import WorkerService, _safe_diagnostic


FORBIDDEN_COMMANDS = {"import", "resume", "review", "publish", "rollback", "cleanup", "search", "provider"}
OFFICIAL_DISCLAIMER = "NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT."
SENSITIVE_OUTPUT = re.compile(r"(?i)(authorization|api[_-]?key|token|usage|cost|budget)")


def _subparsers(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    action = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    return action


def test_cli_exposes_only_web_and_mcp() -> None:
    parser = cli.build_parser()
    assert set(_subparsers(parser).choices) == {"web", "mcp"}
    for command in FORBIDDEN_COMMANDS:
        with pytest.raises(SystemExit):
            parser.parse_args([command])


def test_cli_rejects_frozen_host_and_port_options() -> None:
    parser = cli.build_parser()
    for option in ("--host", "--port"):
        with pytest.raises(SystemExit):
            parser.parse_args(["web", option, "0.0.0.0"])


def test_cli_web_uses_fixed_loopback_even_with_host_port_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    created: dict[str, object] = {}
    web_module = types.ModuleType("blockpedia.web")

    def create_app(**kwargs: object) -> object:
        created.update(kwargs)
        return object()

    web_module.create_app = create_app  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "blockpedia.web", web_module)
    monkeypatch.setenv("HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9999")
    uvicorn_calls: list[tuple[object, dict[str, object]]] = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: uvicorn_calls.append((app, kwargs)))

    assert cli.main(["web", "--data-root", str(tmp_path), "--log-level", "debug"]) == 0
    assert created == {"data_root": str(tmp_path)}
    assert len(uvicorn_calls) == 1
    assert uvicorn_calls[0][1] == {"host": "127.0.0.1", "port": 8765, "log_level": "debug", "access_log": False}


def test_cli_mcp_runs_stdio_without_cli_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from blockpedia import mcp_server

    calls: list[str | None] = []
    monkeypatch.setattr(mcp_server, "run_stdio", lambda data_root: calls.append(data_root))

    assert cli.main(["mcp", "--data-root", str(tmp_path)]) == 0
    assert calls == [str(tmp_path)]
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_pyproject_declares_script_and_local_assets() -> None:
    import tomllib

    document = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert document["project"]["scripts"] == {"block-index": "blockpedia.cli:main"}
    package_data = document["tool"]["setuptools"]["package-data"]["blockpedia"]
    assert "sql/*.sql" in package_data
    assert "sql/*.sha256" in package_data
    assert "templates/**/*.html" in package_data
    assert "static/**/*" in package_data


def test_worker_lifecycle_does_not_overlap_threads_or_restart_after_timeout(tmp_path: Path) -> None:
    worker = WorkerService(DataRoot(tmp_path))
    entered = threading.Event()
    release = threading.Event()

    def blocked_loop(_interval_seconds: float) -> None:
        entered.set()
        release.wait(5)

    worker._loop = blocked_loop  # type: ignore[method-assign]
    assert worker.start() is True
    assert entered.wait(2)
    thread = worker._thread
    assert thread is not None and thread.is_alive()
    assert worker.start() is False
    assert worker.stop(timeout=0.01) is False
    assert worker._thread is thread
    assert thread.is_alive()
    assert worker.start() is False

    release.set()
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert worker.stop(timeout=0.1) is True
    assert worker.start() is True
    assert worker.stop(timeout=0.1) is True
    assert worker.close() is True
    assert worker.close() is True
    assert worker.start() is False


def test_studio_service_close_is_thread_safe_and_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service = StudioService(DataRoot(tmp_path))
    calls = 0
    guard = threading.Lock()

    def counted_close() -> bool:
        nonlocal calls
        with guard:
            calls += 1
        return True

    monkeypatch.setattr(service.worker, "close", counted_close)
    threads = [threading.Thread(target=service.close) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert calls == 1
    assert service.close() is False


def test_diagnostic_redacts_exception_text_paths_and_query() -> None:
    message = "C:\\Sensitive\\api-key.txt \\\\server\\share\\api-key.txt /data/private/key.txt query=secret"
    diagnostic = _safe_diagnostic(RuntimeError(message))
    assert diagnostic == "INTERNAL_ERROR:RuntimeError"
    assert all(value not in diagnostic for value in ("C:\\Sensitive", "\\\\server", "/data/private", "query=secret"))


def _new_injected_service(tmp_path: Path) -> StudioService:
    from conftest import PassingToolchainProbe

    return StudioService(DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())


def _web_import_check(client, export_fixture: Path) -> dict[str, object]:
    directories = client.get("/api/directories", params={"minecraft_version": "26.2"})
    assert directories.status_code == 200
    entries = directories.json()["data"]["entries"]
    entry = next(item for item in entries if item.get("export_id") == export_fixture.name and item.get("selectable"))
    ref = entry["directory_ref"]
    assert isinstance(ref, str)
    assert not Path(ref).is_absolute()
    started = client.post(
        "/api/imports/check",
        json={"source_directory": ref, "minecraft_version": "26.2"},
    )
    assert started.status_code in {200, 202}
    started_data = started.json()["data"]
    check_id = started_data["check_id"]
    deadline = time.monotonic() + 30
    latest = started_data
    while time.monotonic() < deadline:
        response = client.get(f"/api/imports/checks/{check_id}")
        assert response.status_code == 200
        latest = response.json()["data"]
        if latest["status"] in {"passed", "failed"}:
            break
        time.sleep(0.02)
    assert latest["status"] == "passed", latest
    return latest


@pytest.mark.parametrize("close_first", ("app_one", "app_two"))
def test_injected_service_is_caller_owned_for_both_lifespan_close_orders(web_module, tmp_path: Path, close_first: str) -> None:
    from fastapi.testclient import TestClient

    service = _new_injected_service(tmp_path)
    assert service.worker.start() is True
    caller_thread = service.worker._thread
    assert caller_thread is not None and caller_thread.is_alive()
    app_one = web_module.create_app(data_root=DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], service=service, start_worker=True)
    app_two = web_module.create_app(data_root=DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], service=service, start_worker=True)
    client_one = TestClient(app_one)
    client_two = TestClient(app_two)
    try:
        client_one.__enter__()
        client_two.__enter__()
        assert service.worker._thread is caller_thread
        assert caller_thread.is_alive()
        first, second = (client_one, client_two) if close_first == "app_one" else (client_two, client_one)
        first.__exit__(None, None, None)
        assert caller_thread.is_alive()
        second.__exit__(None, None, None)
        assert service.worker._thread is caller_thread
        assert caller_thread.is_alive()
        assert service.worker.stop(timeout=2) is True
        assert not caller_thread.is_alive()
    finally:
        # Explicit caller ownership is also responsible for final close.
        if client_one.portal is not None:
            client_one.__exit__(None, None, None)
        if client_two.portal is not None:
            client_two.__exit__(None, None, None)
        service.close()


def test_prestarted_injected_worker_survives_app_lifespan(web_module, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    service = _new_injected_service(tmp_path)
    assert service.worker.start() is True
    thread = service.worker._thread
    assert thread is not None and thread.is_alive()
    app = web_module.create_app(data_root=DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], service=service, start_worker=True)
    try:
        with TestClient(app):
            assert service.worker._thread is thread
            assert thread.is_alive()
        assert thread.is_alive()
        assert service.worker.stop(timeout=2) is True
        assert not thread.is_alive()
    finally:
        service.close()


def test_unstarted_injected_service_is_not_started_by_app_lifespan(web_module, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    service = _new_injected_service(tmp_path)
    app = web_module.create_app(data_root=DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], service=service, start_worker=True)
    try:
        with TestClient(app):
            assert service.worker._thread is None or not service.worker._thread.is_alive()
        assert service.worker._thread is None or not service.worker._thread.is_alive()
    finally:
        service.close()


def test_owned_service_lifespan_starts_and_stops_its_worker(web_module, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = web_module.create_app(data_root=DataRoot(tmp_path), repo_root=Path(__file__).resolve().parents[2], start_worker=True)
    owned_service = app.state.service
    with TestClient(app):
        owned_thread = owned_service.worker._thread
        assert owned_thread is not None and owned_thread.is_alive()
    assert owned_service.worker._thread is None or not owned_service.worker._thread.is_alive()
    owned_service.close()


@pytest.fixture
def web_module() -> types.ModuleType:
    return pytest.importorskip("blockpedia.web")


@pytest.fixture
def web_context(web_module: types.ModuleType, tmp_path: Path):
    from conftest import PassingToolchainProbe

    service = StudioService(
        DataRoot(tmp_path),
        repo_root=Path(__file__).resolve().parents[2],
        toolchain_probe=PassingToolchainProbe(),
    )
    app = web_module.create_app(
        data_root=DataRoot(tmp_path),
        repo_root=Path(__file__).resolve().parents[2],
        service=service,
        start_worker=False,
    )
    try:
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client, service, tmp_path
    finally:
        service.close()


def _assert_safe_payload(value: object, tmp_path: Path) -> None:
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert SENSITIVE_OUTPUT.search(serialized) is None
    urls = re.findall(r"https?://[^\s\"'<>]+", serialized)
    assert all(url.startswith("http://testserver/") for url in urls)


def _assert_error_envelope(response, allowed_statuses: set[int]) -> dict[str, object]:
    assert response.status_code in allowed_statuses
    payload = response.json()
    assert payload.get("ok") is False
    assert isinstance(payload.get("request_id"), str)
    assert isinstance(payload.get("error_code"), str)
    assert isinstance(payload.get("message"), str)
    assert isinstance(payload.get("retryable"), bool)
    return payload


def test_web_surface_is_loopback_without_auth_cors_csrf_or_r4_routes(web_context) -> None:
    client, _, tmp_path = web_context
    app = client.app
    middleware_names = {middleware.cls.__name__.lower() for middleware in app.user_middleware}
    assert not any(token in name for name in middleware_names for token in ("cors", "auth", "csrf"))
    paths = {route.path for route in app.routes}
    assert {path for path in paths if path.startswith("/api/releases")} == {
        "/api/releases/check",
        "/api/releases/build",
    }
    assert {path for path in paths if path.startswith("/api/provider")} == {
        "/api/provider/profile",
        "/api/provider/probe",
        "/api/provider/enable",
        "/api/provider/disable",
    }
    forbidden_prefixes = ("/api/current", "/api/search-tests", "/api/mcp", "/mcp")
    assert not any(path.startswith(forbidden_prefixes) for path in paths)

    response = client.get("/")
    assert response.status_code == 200
    assert OFFICIAL_DISCLAIMER in response.text
    _assert_safe_payload(response.text, tmp_path)
    for reference in re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", response.text, flags=re.IGNORECASE):
        assert reference.startswith(("/", "#")) or reference.startswith("http://testserver/")


def test_provider_probe_and_enable_accept_only_profile_id(web_context) -> None:
    client, _, tmp_path = web_context
    for route in ("/api/provider/probe", "/api/provider/enable", "/api/provider/disable"):
        response = client.post(route, json={"profile_id": "default", "storage_confirm": True})
        payload = _assert_error_envelope(response, {400})
        assert payload["error_code"] == "INVALID_INPUT"
        _assert_safe_payload(payload, tmp_path)

    htmx = client.post(
        "/ui/provider/probe",
        data={"profile_id": "default", "storage_confirm": "true"},
    )
    assert htmx.status_code == 400
    assert "store_false_supported" not in htmx.text
    assert "storage_confirm" not in htmx.text


def test_web_import_unknown_fields_use_stable_error_envelope(web_context, export_fixture: Path) -> None:
    client, _, tmp_path = web_context
    response = client.post(
        "/api/imports/check",
        json={"source_directory": str(export_fixture), "minecraft_version": "26.2", "unknown": True},
    )
    payload = _assert_error_envelope(response, {400, 422})
    assert payload["error_code"] in {"INVALID_INPUT", "IMPORT_CONTRACT_INVALID"}
    _assert_safe_payload(payload, tmp_path)


def test_web_import_run_actions_and_workspace_search_use_injected_service(web_context, export_fixture: Path) -> None:
    client, service, tmp_path = web_context
    check_data = _web_import_check(client, export_fixture)
    imported = client.post(
        "/api/imports",
        json={"check_id": check_data["check_id"], "copy_mode": "copy_to_workspace"},
    )
    assert imported.status_code == 200
    run_id = imported.json()["data"]["run_id"]
    read = client.get(f"/api/runs/{run_id}")
    assert read.status_code == 200
    _assert_safe_payload(read.json(), tmp_path)

    conflict = client.post(f"/api/runs/{run_id}/pause", json={})
    _assert_error_envelope(conflict, {409})

    service.tick(run_id)
    assert client.post(f"/api/runs/{run_id}/pause", json={}).status_code == 200
    assert client.post(f"/api/runs/{run_id}/resume", json={}).status_code == 200
    search = client.get(f"/api/runs/{run_id}/search", params={"query": "stone"})
    assert search.status_code == 200
    _assert_safe_payload(search.json(), tmp_path)


def test_web_cancel_and_retry_failed_actions(web_context, export_fixture: Path) -> None:
    client, service, tmp_path = web_context
    checked = _web_import_check(client, export_fixture)
    run_id = client.post("/api/imports", json={"check_id": checked["check_id"], "copy_mode": "copy_to_workspace"}).json()["data"]["run_id"]
    service.tick(run_id)
    cancelled = client.post(f"/api/runs/{run_id}/cancel", json={})
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"

    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            connection.execute("UPDATE runs SET status='failed' WHERE run_id=?", (run_id,))
    retried = client.post(f"/api/runs/{run_id}/retry-failed", json={})
    assert retried.status_code == 200
    assert retried.json()["data"]["status"] == "pending"
    _assert_safe_payload(retried.json(), tmp_path)


def test_htmx_write_action_uses_service_and_does_not_echo_search_query(web_context, export_fixture: Path) -> None:
    client, service, tmp_path = web_context
    checked = _web_import_check(client, export_fixture)
    run_id = client.post("/api/imports", json={"check_id": checked["check_id"], "copy_mode": "copy_to_workspace"}).json()["data"]["run_id"]
    service.tick(run_id)
    action = client.post(f"/ui/runs/{run_id}/pause", data={})
    assert action.status_code == 200
    _assert_safe_payload(action.text, tmp_path)
    search_query = "query-must-not-be-echoed"
    search = client.get(f"/ui/runs/{run_id}/search", params={"query": search_query})
    assert search.status_code == 200
    assert search_query not in search.text
    _assert_safe_payload(search.text, tmp_path)


def test_unknown_query_route_and_wrong_method_use_error_envelopes(web_context) -> None:
    client, _, _ = web_context
    unknown_query = client.get("/api/runs/run_missing/search", params={"query": "stone", "unknown": "field"})
    _assert_error_envelope(unknown_query, {400, 422})
    unknown_route = _assert_error_envelope(client.get("/api/not-a-real-route"), {404})
    assert unknown_route["error_code"] in {"API_NOT_FOUND", "NOT_FOUND", "RUN_NOT_FOUND"}
    wrong_method = _assert_error_envelope(client.get("/api/imports/check"), {400, 405, 422})
    assert wrong_method["error_code"] in {"INVALID_INPUT", "METHOD_NOT_ALLOWED"}


def test_sentinel_paths_do_not_cross_json_html_or_stderr(web_context, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
    client, service, tmp_path = web_context
    sentinels = (r"C:\Sensitive\api-key.txt", r"\\server\share\api-key.txt", "/data/private/key.txt")
    for sentinel in sentinels:
        response = client.post("/api/imports/check", json={"source_directory": sentinel, "minecraft_version": "26.2"})
        assert response.status_code in {400, 422}
        _assert_safe_payload(response.json(), tmp_path)

    monkeypatch.setattr(service, "get_run", lambda _run_id: (_ for _ in ()).throw(RuntimeError(" ".join(sentinels))))
    page = client.get("/runs/sentinel")
    assert page.status_code == 500
    assert all(sentinel not in page.text for sentinel in sentinels)
    worker = WorkerService(DataRoot(tmp_path))
    worker._record_infrastructure_failure(tmp_path / "sensitive", None, None, RuntimeError(" ".join(sentinels) + " query=secret"))
    diagnostics = capsys.readouterr().err
    assert all(sentinel not in diagnostics for sentinel in sentinels)
    assert "query=secret" not in diagnostics


def test_web_missing_run_maps_to_404_envelope(web_context) -> None:
    client, _, _ = web_context
    payload = _assert_error_envelope(client.get("/api/runs/run_missing"), {404})
    assert payload["error_code"] in {"RUN_NOT_FOUND", "NOT_FOUND"}


def test_startup_stale_detection_is_read_only_until_recover(web_module, tmp_path: Path, export_fixture: Path) -> None:
    from conftest import PassingToolchainProbe
    from fastapi.testclient import TestClient

    data_root = DataRoot(tmp_path)
    service = StudioService(data_root, repo_root=Path(__file__).resolve().parents[2], toolchain_probe=PassingToolchainProbe())
    check = service.check_import(export_fixture, "26.2")
    run_id = service.import_checked(check.check_id)["run_id"]
    with service.worker.open_database(run_id) as database:
        with database.transaction() as connection:
            connection.execute("UPDATE runs SET status='running',current_stage='IMPORT_EXPORT' WHERE run_id=?", (run_id,))
            connection.execute("UPDATE stage_runs SET status='running',worker_id='worker_dead',heartbeat_at='2000-01-01T00:00:00Z' WHERE run_id=? AND stage='IMPORT_EXPORT'", (run_id,))
        before = database.fetchone("SELECT status FROM runs WHERE run_id=?", (run_id,))
        schema_hash = database.fetchone("SELECT schema_sha256 FROM schema_meta")
    app = web_module.create_app(data_root=data_root, repo_root=Path(__file__).resolve().parents[2], service=service, start_worker=False)
    try:
        with TestClient(app) as client:
            read = client.get(f"/api/runs/{run_id}")
            assert read.status_code == 200
            assert read.json()["data"].get("stale")
            with service.worker.open_database(run_id) as database:
                after = database.fetchone("SELECT status FROM runs WHERE run_id=?", (run_id,))
                after_hash = database.fetchone("SELECT schema_sha256 FROM schema_meta")
            assert before is not None and after is not None and before["status"] == after["status"]
            assert schema_hash is not None and after_hash is not None and schema_hash["schema_sha256"] == after_hash["schema_sha256"]
            recovered = client.post(f"/api/runs/{run_id}/recover", json={"stage": "IMPORT_EXPORT"})
            assert recovered.status_code == 200
            with service.worker.open_database(run_id) as database:
                changed = database.fetchone("SELECT status FROM runs WHERE run_id=?", (run_id,))
            assert changed is not None and changed["status"] == "pending"
    finally:
        service.close()


def test_local_htmx_assets_are_versioned_and_hashable(web_module) -> None:
    package = importlib.import_module("blockpedia")
    assert package.__file__ is not None
    package_root = Path(package.__file__).resolve().parent
    static_root = package_root / "static"
    javascript = static_root / "vendor" / "htmx.min.js"
    license_path = static_root / "vendor" / "LICENSE.htmx"
    assert javascript.is_file()
    assert license_path.is_file()
    js_bytes = javascript.read_bytes()
    license_bytes = license_path.read_bytes()
    assert b"2.0.10" in js_bytes
    assert b"Zero-Clause BSD" in license_bytes
    assert hashlib.sha256(js_bytes).hexdigest() == "71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de"
    assert hashlib.sha256(license_bytes).hexdigest() == "d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38"
