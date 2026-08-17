from __future__ import annotations

import json
from pathlib import Path

import pytest

from blockpedia.paths import DataRoot
from blockpedia.provider import ProviderProfile
from blockpedia.services import R3Error, StudioService

from .test_pipeline_review import _FakeProvider, _service


def _d044_stages(concurrency: int) -> dict[str, dict[str, int]]:
    return {
        "offline_annotation": {"batch_size": 12, "concurrency": concurrency},
        "query_spec": {"batch_size": 1, "concurrency": 1},
        "visual_rerank": {"batch_size": 1, "concurrency": 1},
    }


def _d044_profile_form(*, concurrency: int, model_id: str = "fixture-model") -> dict[str, str]:
    return {
        "profile_id": "default",
        "adapter": "openai_responses",
        "model_id": model_id,
        "base_url": "http://127.0.0.1:8766/v1",
        "secret_reference": "env:OPENAI_API_KEY",
        "prompt_version": "prompt.v1",
        "request_timeout_ms": "60000",
        "offline_annotation_batch_size": "12",
        "offline_annotation_concurrency": str(concurrency),
        "query_spec_batch_size": "1",
        "query_spec_concurrency": "1",
        "visual_rerank_batch_size": "1",
        "visual_rerank_concurrency": "1",
    }


def test_d044_provider_concurrency_ui_and_json_bounds(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).parents[2])
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(
        data_root=DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        service=service,
        start_worker=False,
    )
    payload = {
        "profile_id": "default",
        "adapter": "openai_responses",
        "model_id": "fixture-model",
        "base_url": "http://127.0.0.1:8766/v1",
        "stages": _d044_stages(5),
    }
    try:
        with TestClient(app) as client:
            new_profile_page = client.get("/provider")
            assert new_profile_page.status_code == 200
            assert 'name="offline_annotation_concurrency" type="number" value="1" required min="1" max="5" step="1"' in new_profile_page.text
            assert "并发 AI 批次" in new_profile_page.text
            assert "自动重试预算" in new_profile_page.text

            saved = client.put("/api/provider/profile", json=payload)
            assert saved.status_code == 200
            assert saved.json()["data"]["profile"]["stages"] == _d044_stages(5)

            current_page = client.get("/provider")
            assert 'name="offline_annotation_concurrency" type="number" value="5" required min="1" max="5" step="1"' in current_page.text
            assert 'name="query_spec_concurrency" value="1"' in current_page.text
            assert 'name="visual_rerank_concurrency" value="1"' in current_page.text

            too_high = client.put(
                "/api/provider/profile",
                json={**payload, "stages": _d044_stages(6)},
            )
            assert too_high.status_code == 400
            assert too_high.json()["error_code"] == "PROVIDER_CONFIG_INVALID"

            invalid_online = _d044_stages(5)
            invalid_online["query_spec"]["concurrency"] = 2
            online_response = client.put(
                "/api/provider/profile",
                json={**payload, "stages": invalid_online},
            )
            assert online_response.status_code == 422
            assert online_response.json()["error_code"] == "PROVIDER_CONFIG_INVALID"
    finally:
        service.close()


def test_d044_concurrency_only_ui_save_preserves_active_verification(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("OPENAI_API_KEY", "fixture-secret")
    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).parents[2])
    profile = ProviderProfile(
        profile_id="default",
        model_id="fixture-model",
        base_url="http://127.0.0.1:8766/v1",
        secret_reference="env:OPENAI_API_KEY",
    )
    service.profile_store.save(profile)
    service.profile_store.record_probe(
        {
            "profile_id": "default",
            "adapter": "openai_responses",
            "capability_status": "verified",
            "image_input_supported": True,
            "structured_outputs_supported": True,
            "error_classification_supported": True,
        }
    )
    service.enable_provider("default")
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(
        data_root=DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        service=service,
        start_worker=False,
    )
    try:
        with TestClient(app) as client:
            concurrency_only = client.post(
                "/ui/provider/profile",
                data=_d044_profile_form(concurrency=4),
            )
            assert concurrency_only.status_code == 200
            assert "唯一 active" in concurrency_only.text
            assert 'name="offline_annotation_concurrency" type="number" value="4" required min="1" max="5" step="1"' in concurrency_only.text
            preserved = service.profile_store.load()["default"]
            assert preserved.enabled is True
            assert preserved.capability_status == "verified"
            assert preserved.to_dict()["stages"] == _d044_stages(4)

            other_edit = client.post(
                "/ui/provider/profile",
                data=_d044_profile_form(concurrency=4, model_id="fixture-model-v2"),
            )
            assert other_edit.status_code == 200
            invalidated = service.profile_store.load()["default"]
            assert invalidated.enabled is False
            assert invalidated.capability_status == "unverified"
    finally:
        service.close()


def test_d044_run_and_plan_views_show_frozen_concurrency_without_provider_call(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    fake = _FakeProvider()
    service, run_id, _ = _service(tmp_path, fake)
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(
        data_root=DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        service=service,
        start_worker=False,
    )
    try:
        with TestClient(app) as client:
            run_page = client.get(f"/runs/{run_id}")
            assert run_page.status_code == 200
            assert "1 个并发 AI 批次" in run_page.text
            assert "已为此运行冻结" in run_page.text
            assert 'data-offline-concurrency="1"' in run_page.text
            assert "active profile 后续变化不会改写" in run_page.text

            plan = client.get(f"/api/runs/{run_id}/ai-batches/plan")
            assert plan.status_code == 200
            assert plan.json()["data"]["offline_annotation_concurrency"] == 1
            assert "运行冻结并发度" in run_page.text
            assert fake.calls == 0
    finally:
        service.close()


def test_d045_banner_refresh_ui_is_bounded_and_calls_the_single_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from blockpedia.banner_refresh import BANNER_TARGET_IDS

    fake_provider = _FakeProvider()
    service, run_id, _ = _service(tmp_path, fake_provider)
    captured: dict[str, object] = {}

    def refresh_banner_export(called_run_id: str, **kwargs: object) -> dict[str, object]:
        captured.update({"run_id": called_run_id, **kwargs})
        return {
            "run_id": called_run_id,
            "new_import_id": "import_banner_refresh",
            "new_export_id": "export_20260817T120000Z",
            "target_count": 32,
            "new_variant_count": 32,
            "new_feature_count": 32,
            "new_ai_job_count": 3,
            "current_stage": "AI_ANNOTATE",
            "idempotent": False,
        }

    monkeypatch.setattr(service, "refresh_banner_export", refresh_banner_export)
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(
        data_root=DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        service=service,
        start_worker=False,
    )
    try:
        with TestClient(app) as client:
            hidden = client.get(f"/runs/{run_id}")
            assert hidden.status_code == 200
            assert "data-banner-refresh" not in hidden.text

            with service.worker.open_database(run_id) as database:
                base = database.fetchone(
                    "SELECT imports.export_id FROM imports JOIN runs ON runs.import_id=imports.import_id WHERE runs.run_id=?",
                    (run_id,),
                )
                assert base is not None
                base_export_id = str(base["export_id"])
                with database.transaction() as connection:
                    connection.execute(
                        "UPDATE runs SET status='needs_review',current_stage='HUMAN_REVIEW',boundary_event=NULL WHERE run_id=?",
                        (run_id,),
                    )
                    connection.execute(
                        "UPDATE stage_runs SET status='needs_review' WHERE run_id=? AND stage='HUMAN_REVIEW'",
                        (run_id,),
                    )

            visible = client.get(f"/runs/{run_id}")
            assert visible.status_code == 200
            assert "data-banner-refresh" in visible.text
            assert base_export_id in visible.text
            assert "Banner 定向修复" in visible.text
            assert "Import Check" in visible.text and "banner-repair" in visible.text
            assert "32 个 Banner 变体与特征" in visible.text
            assert "三个未批准的 AI 批次" in visible.text
            assert 'name="check_id" type="text" required' in visible.text
            assert 'name="confirm" type="checkbox" value="true" required' in visible.text
            assert "data-explicit-confirmation-submit" in visible.text
            assert 'name="target_ids"' not in visible.text
            assert 'name="expected_base_export_id"' not in visible.text

            missing_confirmation = client.post(
                f"/ui/runs/{run_id}/banner-export-refresh",
                data={"check_id": "check_" + "a" * 32},
                headers={"HX-Request": "true"},
            )
            assert missing_confirmation.status_code == 400
            assert missing_confirmation.headers["HX-Retarget"] == "#banner-refresh-feedback"
            assert captured == {}

            applied = client.post(
                f"/ui/runs/{run_id}/banner-export-refresh",
                data={"check_id": "check_" + "a" * 32, "confirm": "true"},
                headers={"HX-Request": "true"},
            )
            assert applied.status_code == 200
            assert "Banner 定向修复已应用" in applied.text
            assert "32 个 Banner 变体与离线特征已加入" in applied.text
            assert "三个新的 AI 批次" in applied.text
            assert "预览并批准这三个批次" in applied.text
            assert "没有自动批准，也没有调用 provider" in applied.text
            assert captured == {
                "run_id": run_id,
                "check_id": "check_" + "a" * 32,
                "expected_base_export_id": base_export_id,
                "target_ids": list(BANNER_TARGET_IDS),
                "confirm": True,
            }
            assert fake_provider.calls == 0
    finally:
        service.close()


@pytest.mark.parametrize("adapter", ("openai_responses", "openai_chat_completions"))
def test_provider_profile_http_adapter_is_strict_and_non_secret(tmp_path: Path, adapter: str) -> None:
    from fastapi.testclient import TestClient

    service = StudioService(DataRoot(tmp_path), repo_root=Path(__file__).parents[2])
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(data_root=DataRoot(tmp_path), repo_root=Path(__file__).parents[2], service=service, start_worker=False)
    try:
        with TestClient(app) as client:
            saved = client.put(
                "/api/provider/profile",
                json={
                    "profile_id": "default",
                    "adapter": adapter,
                    "model_id": "fixture-model",
                    "base_url": "http://127.0.0.1:8766/v1",
                },
            )
            assert saved.status_code == 200
            assert "api_key" not in saved.text.lower()
            assert "authorization" not in saved.text.lower()
            assert "secret_reference" not in saved.text
            saved_profile = saved.json()["data"]["profile"]
            assert saved_profile["adapter"] == adapter
            assert saved_profile["capability_status"] == "unverified"
            assert set(saved_profile["capabilities"]) >= {
                "adapter",
                "image_input_supported",
                "structured_outputs_supported",
                "error_classification_supported",
                "capability_status",
                "error_code",
                "request_id_redacted",
                "probed_at",
            }
            assert "store_false_supported" not in saved.text

            unknown = client.put(
                "/api/provider/profile",
                json={
                    "profile_id": "default",
                    "model_id": "fixture-model",
                    "base_url": "http://127.0.0.1:8766/v1",
                    "api_key": "fixture-secret",
                },
            )
            assert unknown.status_code == 400
            assert unknown.json()["error_code"] == "PROVIDER_CONFIG_INVALID"

            unknown_adapter = client.put(
                "/api/provider/profile",
                json={
                    "profile_id": "default",
                    "adapter": "unknown_protocol",
                    "model_id": "fixture-model",
                    "base_url": "http://127.0.0.1:8766/v1",
                },
            )
            assert unknown_adapter.status_code == 400
            assert unknown_adapter.json()["error_code"] == "PROVIDER_CONFIG_INVALID"

            omitted_adapter = client.put(
                "/api/provider/profile",
                json={
                    "profile_id": "default",
                    "model_id": "fixture-model",
                    "base_url": "http://127.0.0.1:8766/v1",
                },
            )
            assert omitted_adapter.status_code == 400
            assert omitted_adapter.json()["error_code"] == "PROVIDER_CONFIG_INVALID"

            service.profile_store.record_probe(
                {
                    "profile_id": "default",
                    "adapter": adapter,
                    "capability_status": "verified",
                    "image_input_supported": True,
                    "structured_outputs_supported": True,
                    "error_classification_supported": True,
                    "error_code": None,
                    "request_id_redacted": "req_…fixture",
                    "probed_at": "2026-08-15T12:00:00Z",
                }
            )

            profile = client.get("/api/provider/profile", params={"profile_id": "default"})
            assert profile.status_code == 200
            assert profile.json()["data"]["profile"]["profile_id"] == "default"
            public_profile = profile.json()["data"]["profile"]
            assert public_profile["adapter"] == adapter
            assert public_profile["capabilities"]["image_input_supported"] is True
            assert public_profile["capabilities"]["structured_outputs_supported"] is True
            assert public_profile["capabilities"]["error_classification_supported"] is True
            assert public_profile["capabilities"]["capability_status"] == "verified"
            assert public_profile["capabilities"]["request_id_redacted"] == "req_…fixture"
            assert "store_false_supported" not in profile.text
    finally:
        service.close()


def test_chat_profile_htmx_save_and_probe_are_protocol_aware(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    def probe_factory(profile, **_kwargs):
        return {
            "profile_id": profile.profile_id,
            "adapter": profile.adapter,
            "capability_status": "verified",
            "image_input_supported": True,
            "structured_outputs_supported": True,
            "error_classification_supported": True,
            "base_url_stable_id": profile.base_url_stable_id,
            "request_id_redacted": "req_…chatprobe",
            "probed_at": "2026-08-15T12:00:00Z",
            "error_code": None,
        }

    service = StudioService(
        DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        probe_factory=probe_factory,
    )
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(data_root=DataRoot(tmp_path), repo_root=Path(__file__).parents[2], service=service, start_worker=False)
    try:
        with TestClient(app) as client:
            saved = client.post(
                "/ui/provider/profile",
                data={
                    "profile_id": "chat",
                    "adapter": "openai_chat_completions",
                    "model_id": "fixture-chat-model",
                    "base_url": "http://127.0.0.1:8766/v1",
                    "secret_reference": "env:OPENAI_API_KEY",
                    "prompt_version": "prompt.v1",
                    "request_timeout_ms": "60000",
                },
            )
            assert saved.status_code == 200
            assert 'data-provider-adapter="openai_chat_completions"' in saved.text
            assert "POST /chat/completions" in saved.text
            assert "store_false_supported" not in saved.text

            probed = client.post("/ui/provider/probe", data={"profile_id": "chat"})
            assert probed.status_code == 200
            assert "OPENAI_CHAT_COMPLETIONS" in probed.text
            assert "OpenAI Chat Completions" in probed.text
            assert "POST /chat/completions" in probed.text
            assert 'data-probe-capability="image_input_supported"' in probed.text
            assert 'data-probe-capability="structured_outputs_supported"' in probed.text
            assert 'data-probe-capability="error_classification_supported"' in probed.text
            assert "store_false_supported" not in probed.text
    finally:
        service.close()


def test_provider_fallback_is_protocol_and_capability_allowlisted() -> None:
    app_module = __import__("blockpedia.web", fromlist=["_fallback_provider_result"])
    fallback = app_module._fallback_provider_result(
        {
            "profile": {
                "profile_id": "chat",
                "adapter": "openai_chat_completions",
                "capability_status": "failed",
                "capabilities": {
                    "adapter": "openai_chat_completions",
                    "capability_status": "failed",
                    "image_input_supported": False,
                    "structured_outputs_supported": True,
                    "error_classification_supported": False,
                    "error_code": "PROVIDER_CAPABILITY_MISSING",
                    "request_id_redacted": "req_…fixture",
                    "probed_at": "2026-08-15T12:00:00Z",
                    "store_false_supported": True,
                },
            }
        }
    )
    assert "openai_chat_completions" in fallback
    assert "image_input_supported" in fallback
    assert "structured_outputs_supported" in fallback
    assert "error_classification_supported" in fallback
    assert "PROVIDER_CAPABILITY_MISSING" in fallback
    assert "req_…fixture" in fallback
    assert "2026-08-15T12:00:00Z" in fallback
    assert "store_false_supported" not in fallback
    assert "storage_verified" not in fallback


def test_ai_batch_http_adapter_is_idempotent_and_image_is_separate(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    service, run_id, imported = _service(tmp_path, _FakeProvider())
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(data_root=DataRoot(tmp_path), repo_root=Path(__file__).parents[2], service=service, start_worker=False)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/runs",
                json={"import_id": imported["import_id"], "minecraft_version": "26.2", "profile_id": "default"},
            )
            assert response.status_code == 202
            assert response.json()["data"]["idempotent"] is True
            assert response.json()["data"]["run_id"] == run_id

            preview = service.preview_ai_batch(run_id)
            logical_key = preview["logical_key"]
            next_batch = client.get(f"/api/runs/{run_id}/ai-batches/next")
            assert next_batch.status_code == 200
            assert "contact_sheet_png" not in next_batch.json()["data"]
            assert "image_url" in next_batch.json()["data"]

            image = client.get(f"/api/runs/{run_id}/ai-batches/{logical_key}/image")
            assert image.status_code == 200
            assert image.headers["content-type"].startswith("image/png")
            assert image.headers["cache-control"] == "no-store"
            assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        service.close()


def test_d040_plan_wave_and_row_retry_http_contracts_are_strict_and_safe(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    service, run_id, _ = _service(tmp_path, _FakeProvider())
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(
        data_root=DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        service=service,
        start_worker=False,
    )
    try:
        with TestClient(app) as client:
            plan_response = client.get(f"/api/runs/{run_id}/ai-batches/plan")
            assert plan_response.status_code == 200
            plan = plan_response.json()["data"]
            assert {"run_id", "effective_config_hash", "plan_hash", "count", "profile_id", "adapter", "model_id", "jobs"} <= set(plan)
            assert plan["jobs"] and {"job_id", "logical_key", "input_signature", "tile_ids", "variant_ids"} <= set(plan["jobs"][0])
            job = plan["jobs"][0]
            assert job["preview_url"] == f"/api/runs/{run_id}/ai-batches/plan/{job['logical_key']}/preview"
            assert job["image_url"] == f"/api/runs/{run_id}/ai-batches/{job['logical_key']}/image"
            detailed = client.get(job["preview_url"])
            assert detailed.status_code == 200
            detailed_data = detailed.json()["data"]
            assert detailed_data["image_url"] == job["image_url"]
            assert detailed_data["tile_ids"] and detailed_data["tiles"]
            assert isinstance(detailed_data["machine_metadata"], dict)
            assert isinstance(detailed_data["prompt"], str) and detailed_data["prompt"]
            invalid_detail = client.get(f"/api/runs/{run_id}/ai-batches/plan/not-a-plan-job/preview")
            assert invalid_detail.status_code == 404
            assert invalid_detail.json()["error_code"] == "AI_BATCH_NOT_FOUND"
            serialized_plan = json.dumps(plan, ensure_ascii=False)
            assert all(secret not in serialized_plan for secret in ("prompt", "contact_sheet", "base_url", "fixture-secret"))
            serialized_detail = json.dumps(detailed_data, ensure_ascii=False)
            assert all(secret not in serialized_detail for secret in ("fixture-secret", "raw_response", "base_url"))

            plan_url = f"/api/runs/{run_id}/ai-batches/plan/approve"
            assert client.post(plan_url, json={}).status_code == 400
            assert client.post(plan_url, json={"plan_hash": plan["plan_hash"], "extra": True}).status_code == 400
            assert client.post(plan_url, json={"plan_hash": "sha256:not-a-hash"}).status_code == 400
            approved = client.post(plan_url, json={"plan_hash": plan["plan_hash"]})
            assert approved.status_code == 202
            assert approved.json()["data"]["approved"] is True

            with service.worker.open_database(run_id) as database:
                source = database.fetchone("SELECT job_id FROM jobs WHERE stage='AI_ANNOTATE' ORDER BY job_id LIMIT 1")
                assert source is not None
            retry_url = f"/api/runs/{run_id}/ai-batches/jobs/{source['job_id']}/retry"
            not_eligible = client.post(retry_url, json={"confirm": True})
            assert not_eligible.status_code == 422
            assert not_eligible.json()["error_code"] == "PROVIDER_RETRY_NOT_ELIGIBLE"
            with service.worker.open_database(run_id) as database:
                database.execute(
                    "UPDATE jobs SET status='failed',error_code='PROVIDER_AUTH_FAILED' WHERE job_id=?",
                    (source["job_id"],),
                )
            # The core retry service rejects fatal sources; the HTTP adapter
            # must preserve that stable 422 without creating a child.
            with service.worker.open_database(run_id) as database:
                before_row = database.fetchone(
                    "SELECT COUNT(*) AS count FROM jobs WHERE run_id=? AND cursor_json LIKE '%retry_of_job_id%'",
                    (run_id,),
                )
                assert before_row is not None
                before_fatal = before_row["count"]
            original_retry = service.retry_provider_job
            monkeypatch.setattr(
                service,
                "retry_provider_job",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(R3Error("PROVIDER_RETRY_NOT_ELIGIBLE", "fatal")),
            )
            fatal_retry = client.post(retry_url, json={"confirm": True})
            assert fatal_retry.status_code == 422
            assert fatal_retry.json()["error_code"] == "PROVIDER_RETRY_NOT_ELIGIBLE"
            assert "fatal" not in fatal_retry.text
            monkeypatch.setattr(service, "retry_provider_job", original_retry)
            with service.worker.open_database(run_id) as database:
                after_row = database.fetchone(
                    "SELECT COUNT(*) AS count FROM jobs WHERE run_id=? AND cursor_json LIKE '%retry_of_job_id%'",
                    (run_id,),
                )
                assert after_row is not None
                after_fatal = after_row["count"]
            assert after_fatal == before_fatal
            with service.worker.open_database(run_id) as database:
                database.execute(
                    "UPDATE jobs SET status='needs_review',error_code='PROVIDER_SERVER_ERROR' WHERE job_id=?",
                    (source["job_id"],),
                )
            wave = client.get(f"/api/runs/{run_id}/ai-batches/retry-wave")
            assert wave.status_code == 200
            wave_data = wave.json()["data"]
            assert wave_data["count"] == 1
            assert wave_data["jobs"]
            assert {"source_job_id", "child_job_id", "source_input_signature", "child_input_signature", "child_logical_key", "variant_ids"} <= set(wave_data["jobs"][0])
            serialized_wave = json.dumps(wave_data, ensure_ascii=False)
            assert all(secret not in serialized_wave for secret in ("prompt", "image", "raw_response", "fixture-secret"))

            wave_url = f"/api/runs/{run_id}/ai-batches/retry-wave/approve"
            assert client.post(wave_url, json={"wave_hash": wave_data["wave_hash"], "extra": True}).status_code == 400
            # The service-layer generation/transaction contract is covered by
            # its own lane; this adapter check supplies its safe confirmation
            # result so the HTTP shape remains isolated from that core test.
            original_confirm = service.confirm_provider_retry_wave
            monkeypatch.setattr(
                service,
                "confirm_provider_retry_wave",
                lambda _run_id, submitted_hash: {
                    "run_id": run_id,
                    "wave_hash": submitted_hash,
                    "count": 1,
                    "jobs": [],
                    "approved": True,
                },
            )
            confirmed = client.post(wave_url, json={"wave_hash": wave_data["wave_hash"]})
            assert confirmed.status_code == 202
            assert confirmed.json()["data"]["approved"] is True
            monkeypatch.setattr(service, "confirm_provider_retry_wave", original_confirm)

            assert client.post(retry_url, json={}).status_code == 400
            assert client.post(retry_url, json={"confirm": False}).status_code == 400
            assert client.post(retry_url, json={"confirm": True, "extra": 1}).status_code == 400
            retried = client.post(retry_url, json={"confirm": True})
            assert retried.status_code == 202
            assert retried.json()["data"]["approved"] is True

            monkeypatch.setattr(
                service,
                "approve_ai_plan",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(R3Error("AI_BATCH_PLAN_CONFLICT", "secret exception text")),
            )
            conflict = client.post(plan_url, json={"plan_hash": plan["plan_hash"]})
            assert conflict.status_code == 409
            assert conflict.json()["error_code"] == "AI_BATCH_PLAN_CONFLICT"
            assert "secret exception text" not in conflict.text
    finally:
        service.close()


def test_d040_run_snapshot_keeps_all_jobs_and_only_flags_actionable_provider_failures(tmp_path: Path) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider())
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                source = connection.execute("SELECT * FROM jobs WHERE stage='AI_ANNOTATE' LIMIT 1").fetchone()
                assert source is not None
                for index in range(7):
                    connection.execute(
                        "INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at,error_code,error_message) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            f"actionable_{index}",
                            run_id,
                            "AI_ANNOTATE",
                            f"actionable_{index}",
                            source["input_signature"],
                            "failed",
                            0,
                            source["cursor_json"],
                            source["created_at"],
                            "PROVIDER_SERVER_ERROR",
                            "secret raw provider evidence",
                        ),
                    )
                connection.execute(
                    "UPDATE jobs SET status='failed',error_code='PROVIDER_AUTH_FAILED' WHERE job_id=?",
                    (source["job_id"],),
                )
                connection.execute("DELETE FROM audit_events")
                now = "2026-08-15T12:00:00Z"
                for event in ("AI_BATCH_PLAN_APPROVED", "AI_PROVIDER_RETRY_CREATED", "AI_PROVIDER_RETRY_WAVE_APPROVED"):
                    connection.execute(
                        "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                        (f"audit_{event}", event, run_id, json.dumps({"secret": "raw evidence"}), now),
                    )

        snapshot = service.get_run(run_id)
        actionable = [job for job in snapshot["jobs"] if job["provider_retry_eligible"]]
        assert len(actionable) >= 7
        assert all(job["stage"] == "AI_ANNOTATE" and job["status"] == "failed" for job in actionable)
        source_view = next(job for job in snapshot["jobs"] if job["job_id"] == source["job_id"])
        assert source_view["provider_retry_eligible"] is False
        assert {step["code"] for step in snapshot["latest_steps"]} >= {
            "AI_BATCH_PLAN_APPROVED",
            "AI_PROVIDER_RETRY_CREATED",
            "AI_PROVIDER_RETRY_WAVE_APPROVED",
        }
        labels = {step["code"]: step["label"] for step in snapshot["latest_steps"]}
        assert labels["AI_BATCH_PLAN_APPROVED"] == "AI 批次计划已批准"
        assert all(
            step["status"] == "succeeded"
            for step in snapshot["latest_steps"]
            if step["code"] in {"AI_BATCH_PLAN_APPROVED", "AI_PROVIDER_RETRY_CREATED", "AI_PROVIDER_RETRY_WAVE_APPROVED"}
        )
        serialized = json.dumps(snapshot, ensure_ascii=False)
        assert "secret raw provider evidence" not in serialized
        assert "raw evidence" not in serialized
        assert "cursor_json" not in serialized
    finally:
        service.close()


def test_ai_preview_preserves_complete_safe_prompt_and_rejects_unsafe_prompt(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    service, run_id, _ = _service(tmp_path, _FakeProvider())
    app_module = __import__("blockpedia.web", fromlist=["create_app"])
    app = app_module.create_app(
        data_root=DataRoot(tmp_path),
        repo_root=Path(__file__).parents[2],
        service=service,
        start_worker=False,
    )
    try:
        with TestClient(app) as client:
            base_preview = dict(service.preview_ai_batch(run_id))
            prompt = str(base_preview["prompt"])
            if len(prompt) <= 500:
                prompt += "\n" + ("safe tile metadata " * 64)
            assert len(prompt) > 500
            monkeypatch.setattr(service, "preview_ai_batch", lambda *_args: {**base_preview, "prompt": prompt})
            preview_url = f"/api/runs/{run_id}/ai-batches/plan/{base_preview['logical_key']}/preview"
            response = client.get(preview_url)
            assert response.status_code == 200
            returned_prompt = response.json()["data"]["prompt"]
            assert returned_prompt == prompt
            assert len(returned_prompt) == len(prompt)

            invalid_prompts = (
                "safe-prefix-" + ("x" * 16_001),
                "safe-prefix\nsecret=fixture-secret",
                "safe-prefix\x00suffix",
            )
            for invalid_prompt in invalid_prompts:
                monkeypatch.setattr(
                    service,
                    "preview_ai_batch",
                    lambda *_args, invalid_prompt=invalid_prompt: {**base_preview, "prompt": invalid_prompt},
                )
                rejected = client.get(preview_url)
                assert rejected.status_code == 400
                assert rejected.json()["error_code"] == "AI_BATCH_INPUT_INVALID"
                assert invalid_prompt[:32] not in rejected.text
    finally:
        service.close()


def test_review_validation_diagnostic_projection_is_allowlisted_and_api_safe(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    service, run_id, _ = _service(tmp_path, _FakeProvider())
    valid = {
        "stage": "offline_annotation",
        "phase": "wire_schema",
        "path": "$.items[0].reason",
        "keyword": "required",
        "observed_type": "missing",
        "observed_length": 7,
    }
    valid_null_length = {**valid, "observed_length": None}
    unsafe_values = (
        "RAW_OUTPUT_SENTINEL",
        "RAW_VALUE_SENTINEL",
        "HASH_SENTINEL",
        "SECRET_SENTINEL",
        "REPAIR_TEXT_SENTINEL",
        "MALFORMED_EXTRA_SENTINEL",
    )
    evidence = [
        "job:job_fixture",
        {
            **valid,
            "raw_output": unsafe_values[0],
            "value": unsafe_values[1],
            "response_hash": unsafe_values[2],
            "secret": unsafe_values[3],
            "repair_context": unsafe_values[4],
            "extra": unsafe_values[5],
        },
        valid,
    ]
    try:
        with service.worker.open_database(run_id) as database:
            with database.transaction() as connection:
                connection.execute(
                    "INSERT INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "review_d042_projection",
                        "26.2",
                        "variant",
                        "minecraft:stone",
                        "PROVIDER_FAILURE",
                        "high",
                        "open",
                        "Provider result requires review.",
                        json.dumps(evidence, ensure_ascii=False),
                        "2026-08-15T12:00:00Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "review_d042_null_length",
                        "26.2",
                        "variant",
                        "minecraft:stone",
                        "PROVIDER_FAILURE",
                        "high",
                        "open",
                        "Provider result requires review.",
                        json.dumps(["job:job_null_length", "provider_request:req_null_length", valid_null_length], ensure_ascii=False),
                        "2026-08-15T12:00:01Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO review_tasks(review_id,minecraft_version,target_type,target_id,reason_code,severity,status,note,evidence_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        "review_d042_without_diagnostic",
                        "26.2",
                        "variant",
                        "minecraft:stone",
                        "PROVIDER_FAILURE",
                        "high",
                        "open",
                        "Provider result requires review.",
                        json.dumps(["job:job_without_diagnostic", "provider_request:req_without_diagnostic"], ensure_ascii=False),
                        "2026-08-15T12:00:02Z",
                    ),
                )

        reviews = service.list_reviews(run_id)
        review = next(item for item in reviews if item["review_id"] == "review_d042_projection")
        assert review["validation_diagnostic"] == valid
        assert review["evidence_count"] == len(evidence)
        assert "raw_output" not in json.dumps(review, ensure_ascii=False)
        assert all(value not in json.dumps(review, ensure_ascii=False) for value in unsafe_values)
        null_review = next(item for item in reviews if item["review_id"] == "review_d042_null_length")
        assert null_review["validation_diagnostic"] == valid_null_length
        without_diagnostic = next(item for item in reviews if item["review_id"] == "review_d042_without_diagnostic")
        assert without_diagnostic["validation_diagnostic"] is None

        app_module = __import__("blockpedia.web", fromlist=["create_app"])
        app = app_module.create_app(
            data_root=DataRoot(tmp_path),
            repo_root=Path(__file__).parents[2],
            service=service,
            start_worker=False,
        )
        with TestClient(app) as client:
            response = client.get("/api/reviews", params={"run_id": run_id})
            page = client.get(f"/runs/{run_id}/review")
        assert response.status_code == 200
        api_reviews = {item["review_id"]: item for item in response.json()["data"]["reviews"]}
        assert api_reviews["review_d042_projection"]["validation_diagnostic"] == valid
        assert api_reviews["review_d042_null_length"]["validation_diagnostic"] == valid_null_length
        assert api_reviews["review_d042_without_diagnostic"]["validation_diagnostic"] is None
        for api_review in api_reviews.values():
            diagnostic = api_review["validation_diagnostic"]
            if diagnostic is not None:
                assert set(diagnostic) == set(valid)
        assert page.status_code == 200
        html = page.text
        serialized = response.text + html
        assert "job:job_fixture" not in serialized
        assert all(value not in serialized for value in unsafe_values)

        diagnostic_card_start = html.index('data-review-id="review_d042_projection"')
        diagnostic_card = html[diagnostic_card_start : html.index("</article>", diagnostic_card_start)]
        assert "模型输出校验诊断" in diagnostic_card
        assert all(value in diagnostic_card for value in ("offline_annotation", "wire_schema", "$.items[0].reason", "required", "missing", "7"))
        assert 'action="/ui/reviews/review_d042_projection/resolve"' in diagnostic_card
        assert 'method="post"' in diagnostic_card
        assert "data-review-form" in diagnostic_card
        assert 'name="decision"' in diagnostic_card
        assert 'name="note"' in diagnostic_card
        assert 'name="evidence"' in diagnostic_card
        assert 'type="submit"' in diagnostic_card

        null_card_start = html.index('data-review-id="review_d042_null_length"')
        null_card = html[null_card_start : html.index("</article>", null_card_start)]
        assert "模型输出校验诊断" in null_card
        assert "不适用" in null_card

        no_diagnostic_card_start = html.index('data-review-id="review_d042_without_diagnostic"')
        no_diagnostic_card = html[no_diagnostic_card_start : html.index("</article>", no_diagnostic_card_start)]
        assert "模型输出校验诊断" not in no_diagnostic_card
    finally:
        service.close()
