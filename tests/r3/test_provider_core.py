from __future__ import annotations

import base64
import copy
import hashlib
import json
import struct
import zlib
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest

from blockpedia.provider import (
    OpenAIProvider,
    OpenAIResponsesProvider,
    ProviderError,
    ProviderResult,
    ProviderProfile,
    ProviderProfileError,
    ProviderProfileStore,
    SecretResolver,
    _probe_query_output,
    build_provider_batch_envelope,
    build_cache_key,
    sanitize_validation_diagnostic,
    validate_annotation_batch,
)
from blockpedia.schema import RecordSchemaError, load_provider_wire_schema, validate_record
from blockpedia.storage import WorkspaceDatabase


def png_1x1() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    raw = zlib.compress(b"\x00\xff\x00\x00\xff")
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def profile(**changes: object) -> ProviderProfile:
    values: dict[str, object] = {
        "profile_id": "default",
        "model_id": "model-v1",
        "base_url": "http://127.0.0.1:8123/v1/",
    }
    values.update(changes)
    return ProviderProfile(**values)  # type: ignore[arg-type]


class Keyring:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def get_password(self, service: str, account: str) -> str | None:
        assert service == "blockpedia"
        assert account == "default"
        return self.value


def annotation() -> dict[str, object]:
    return {
        "schema_id": "annotation-batch-output.v1",
        "items": [
            {
                "variant_id": "minecraft:stone",
                "synonyms_zh": [],
                "synonyms_en": ["stone"],
                "summary_zh": "石块",
                "summary_en": "A stone block.",
                "color_terms": [],
                "shape_terms": [],
                "material_impressions": [],
                "building_roles": [],
                "style_tags": [],
                "avoid_for": [],
                "confidence": 0.9,
                "reason": "The generated test fixture is a stone-like block.",
            }
        ],
    }


def query() -> dict[str, object]:
    return {
        "schema_id": "query-spec-output.v1",
        "source": "llm",
        "hard": {
            "minecraft_version": {"value": "26.2", "source": "request", "required": True},
            "release_status": {"value": "current", "source": "system", "required": True},
            "legal_state": {"value": True, "source": "system", "required": True},
            "behaviors": [], "support": [], "transparency": [], "emission": [], "orientation": [], "shape": [],
        },
        "soft": {key: [] for key in ("colors", "materials", "uses", "styles", "shape_terms", "avoid_for", "keywords")},
        "ambiguities": [], "needs_user_choice": False, "suggested_followups": [], "unknown_terms": [],
    }


def rerank() -> dict[str, object]:
    return {
        "schema_id": "rerank-output.v1",
        "ranking": [{"candidate_id": "A1", "fit": 0.9, "reason": "The candidate matches."}],
        "needs_user_choice": False,
        "ambiguity_points": [],
        "suggested_followups": [],
    }


def raw_response(output: dict[str, object], *, model: object = "model-v1", store: object = False) -> dict[str, object]:
    return {
        "status": "completed",
        "model": model,
        "store": store,
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(output, ensure_ascii=False)}],
            }
        ],
    }


def chat_response(output: dict[str, object], *, model: object = "model-v1", finish_reason: str = "stop", content: object | None = None, refusal: object = None) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": json.dumps(output, ensure_ascii=False) if content is None else content}
    if refusal is not None:
        message["refusal"] = refusal
    return {"model": model, "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}


def nested_incomplete_response(status: str) -> dict[str, object]:
    response = raw_response(annotation())
    response["output"][0]["status"] = status  # type: ignore[index]
    return response


def missing_model_response() -> dict[str, object]:
    response = raw_response(annotation())
    response.pop("model")
    return response


def enabled_profile() -> ProviderProfile:
    return profile(enabled=True, capability_status="verified")


def hash_json(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def hash_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def request_args(stage: str = "offline_annotation", text: str = "minimal", p: ProviderProfile | None = None) -> dict[str, Any]:
    p = p or enabled_profile()
    image = png_1x1()
    if stage == "offline_annotation":
        machine = {"minecraft:stone": {"fixture": "stone"}}
        summary = {"tile_variant_map": [{"tile_id": "tile-1", "variant_id": "minecraft:stone", "image_sha256": hash_bytes(image), "machine_metadata_sha256": hash_json(machine["minecraft:stone"])}]}
        envelope = build_provider_batch_envelope(p, request_id="ReqOffline", stage=stage, input_summary=summary, export_id="export_20260815T000000Z")
        return {"image_png": image, "machine_metadata": machine, "machine_metadata_hash": hash_json(machine), "source_images": {"tile-1": image}, "envelope": envelope}
    if stage == "query_spec":
        summary = {"query_sha256": hash_bytes(text.encode("utf-8"))}
        envelope = build_provider_batch_envelope(p, request_id="ReqQuery", stage=stage, input_summary=summary, release_id="rel_test", resolved_release_manifest_sha256="sha256:" + "c" * 64)
        return {"input_text": text, "query_text": text, "image_png": image, "machine_metadata_hash": hash_bytes(text.encode("utf-8")), "envelope": envelope}
    machine = {"A1": {"fixture": "stone"}}
    query_spec = query()
    candidates = {"A1": {"candidate_id": "A1", "variant_id": "minecraft:stone", "block_id": "minecraft:stone", "recommended_state_id": "minecraft:stone"}}
    summary = {"query_sha256": hash_bytes(text.encode("utf-8")), "query_spec_sha256": hash_json(query_spec), "candidate_set_sha256": hash_json([candidates["A1"]]), "candidate_map": [{"candidate_id": "A1", "variant_id": "minecraft:stone", "block_id": "minecraft:stone", "recommended_state_id": "minecraft:stone", "image_sha256": hash_bytes(image)}]}
    envelope = build_provider_batch_envelope(p, request_id="ReqRerank", stage=stage, input_summary=summary, release_id="rel_test", resolved_release_manifest_sha256="sha256:" + "c" * 64)
    return {"image_png": image, "machine_metadata": machine, "machine_metadata_hash": hash_json(machine), "query_spec": query_spec, "candidate_records": candidates, "source_images": {"A1": image}, "query_text": text, "envelope": envelope}


def test_profile_store_strict_active_and_reload(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path)
    store.save(profile())
    store.save(profile(profile_id="second", model_id="model-v2"))
    assert set(store.load()) == {"default", "second"}
    with pytest.raises(ProviderProfileError):
        ProviderProfile(profile_id="bad", model_id="m", base_url="http://example.com")
    verified = profile(capability_status="verified", enabled=True)
    store.save(verified)
    with pytest.raises(ProviderProfileError):
        store.save(profile(profile_id="second", model_id="model-v2", capability_status="verified", enabled=True))
    assert ProviderProfileStore(path=tmp_path / "provider-profiles.json").load()["default"].enabled
    raw = (tmp_path / "provider-profiles.json").read_text(encoding="utf-8")
    assert "api-key" not in raw


def test_adapter_change_resets_capability_and_legacy_capability_is_not_trusted(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path)
    store.save(profile())
    store.save_capabilities(
        "default",
        {
            "adapter": "openai_responses",
            "image_input_supported": True,
            "structured_outputs_supported": True,
            "error_classification_supported": True,
            "store_false_supported": True,
            "capability_status": "verified",
        },
    )
    with pytest.raises(ProviderError, match="PROVIDER_CAPABILITY_MISSING"):
        store.enable("default")
    store.record_probe({
        "profile_id": "default",
        "adapter": "openai_responses",
        "capability_status": "verified",
        "image_input_supported": True,
        "structured_outputs_supported": True,
        "error_classification_supported": True,
    })
    store.enable("default")
    store.save(profile(adapter="openai_chat_completions"))
    changed, capabilities = store.authoritative("default")
    assert changed is not None and changed.adapter == "openai_chat_completions"
    assert not changed.enabled and changed.capability_status == "unverified" and capabilities == {}


def test_old_profile_and_legacy_capability_file_remain_readable_but_require_reprobe(tmp_path: Path) -> None:
    legacy = profile().to_dict()
    document = {"profiles": [legacy], "capabilities": {"default": {
        "image_input_supported": True,
        "structured_outputs_supported": True,
        "error_classification_supported": True,
        "store_false_supported": True,
        "capability_status": "verified",
    }}}
    (tmp_path / "provider-profiles.json").write_text(json.dumps(document), encoding="utf-8")
    store = ProviderProfileStore(tmp_path)
    assert store.load()["default"].adapter == "openai_responses"
    with pytest.raises(ProviderError, match="PROVIDER_CAPABILITY_MISSING"):
        store.enable("default")


def test_secret_keyring_precedes_environment_and_masks() -> None:
    p = profile()
    keyring = SecretResolver(keyring_backend=Keyring("keyring-secret"), environ={"OPENAI_API_KEY": "env-secret"})
    info = keyring.resolve(p)
    assert info.configured and info.source == "keyring" and info.masked == "********"
    fallback = SecretResolver(keyring_backend=Keyring(None), environ={"OPENAI_API_KEY": "env-secret"})
    assert fallback.resolve(p).source == "env"
    absent = SecretResolver(keyring_backend=Keyring(None), environ={})
    assert not absent.resolve(p).configured


def test_three_wire_shapes_and_probe_store_echo() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["text"]["format"]["strict"] is not True:
            return httpx.Response(400, json={"error": {"type": "invalid_request"}})
        name = body["text"]["format"]["name"]
        output = annotation() if name == "annotation_batch_output_v1" else query() if name == "query_spec_output_v1" else rerank()
        return httpx.Response(200, json=raw_response(output))

    provider = OpenAIResponsesProvider(
        profile(),
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret"), environ={"OPENAI_API_KEY": "other"}),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.probe(png_1x1())
    assert result.capability_status == "verified"
    assert result.adapter == "openai_responses"
    assert len(seen) == 4
    assert [body["text"]["format"]["name"] for body in seen[:3]] == [
        "annotation_batch_output_v1", "query_spec_output_v1", "rerank_output_v1"
    ]
    assert all(body["model"] == "model-v1" and body["store"] is False for body in seen)
    assert "store_false_supported" not in result.to_dict()
    assert all("$schema" not in body["text"]["format"]["schema"] for body in seen)
    assert base64.b64encode(png_1x1()).decode() in seen[0]["input"][0]["content"][1]["image_url"]
    assert load_provider_wire_schema("query-spec-output.v1")["$id"]


def test_chat_probe_uses_three_strict_shapes_without_store() -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append((str(request.url), body))
        assert "store" not in body
        assert body["response_format"]["type"] == "json_schema"
        if body["messages"] == []:
            return httpx.Response(500, json={"error": {"code": "invalid_request", "type": "new_api_error", "message": "probe diagnostic detail"}})
        if body["response_format"]["json_schema"]["strict"] is not True:
            return httpx.Response(400, json={"error": {"type": "invalid_request"}})
        name = body["response_format"]["json_schema"]["name"]
        output = annotation() if name == "annotation_batch_output_v1" else query() if name == "query_spec_output_v1" else rerank()
        return httpx.Response(200, json=chat_response(output))

    provider = OpenAIProvider(
        profile(adapter="openai_chat_completions"),
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.probe(png_1x1())
    assert result.capability_status == "verified"
    assert result.adapter == "openai_chat_completions"
    assert len(seen) == 4
    assert all(url.endswith("/chat/completions") for url, _body in seen)
    assert [body["response_format"]["json_schema"]["name"] for _url, body in seen[:3]] == [
        "annotation_batch_output_v1", "query_spec_output_v1", "rerank_output_v1"
    ]
    assert all(body["model"] == "model-v1" for _url, body in seen)
    assert seen[3][1]["messages"] == []
    assert "store" not in seen[3][1]
    assert seen[0][1]["messages"][0]["content"][1]["type"] == "image_url"
    assert base64.b64encode(png_1x1()).decode() in seen[0][1]["messages"][0]["content"][1]["image_url"]["url"]


@pytest.mark.parametrize("adapter", ["openai_responses", "openai_chat_completions"])
def test_probe_query_spec_uses_exact_return_fixture_instruction(adapter: str) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if adapter == "openai_responses":
            structured = body["text"]["format"]
            assert structured["type"] == "json_schema"
        else:
            assert "store" not in body
            assert body["response_format"]["type"] == "json_schema"
            structured = body["response_format"]["json_schema"]
            if body["messages"] == []:
                return httpx.Response(500, json={"error": {"code": "invalid_request", "message": "probe diagnostic detail"}})
        if structured["strict"] is not True:
            return httpx.Response(400, json={"error": {"type": "invalid_request"}})
        output = annotation() if structured["name"] == "annotation_batch_output_v1" else query() if structured["name"] == "query_spec_output_v1" else rerank()
        return httpx.Response(200, json=raw_response(output) if adapter == "openai_responses" else chat_response(output))

    provider = OpenAIProvider(
        profile(adapter=adapter),
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.probe(png_1x1())

    assert result.capability_status == "verified"
    assert len(seen) == 4
    query_body = seen[1]
    query_text = (
        query_body["input"][0]["content"][0]["text"]
        if adapter == "openai_responses"
        else query_body["messages"][0]["content"][0]["text"]
    )
    assert query_text == "Return exactly the following JSON object unchanged. Do not add terms or change any value. Output only this object:\n" + _probe_query_output()


@pytest.mark.parametrize("marker_field", ["code", "type"])
@pytest.mark.parametrize("marker", ["invalid_request", "INVALID_REQUEST_ERROR"])
def test_five_hundred_allowlisted_invalid_request_is_non_retryable(marker_field: str, marker: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {marker_field: marker, "message": "do-not-expose"}})

    provider = OpenAIProvider(
        profile(enabled=True, capability_status="verified"),
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    attempt = provider._post("offline_annotation", "probe", png_1x1(), repair=False)

    assert attempt.error_code == "PROVIDER_REQUEST_INVALID"
    assert attempt.error_class == "non_retryable" and not attempt.retryable
    assert "do-not-expose" not in repr(attempt)


@pytest.mark.parametrize("response_kind", ["generic", "unknown_code", "malformed"])
def test_five_hundred_unknown_or_malformed_error_remains_retryable(response_kind: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        if response_kind == "malformed":
            return httpx.Response(500, text="do-not-expose")
        code = "server_error" if response_kind == "generic" else "other_error"
        return httpx.Response(500, json={"error": {"code": code, "message": "do-not-expose"}})

    provider = OpenAIProvider(
        profile(enabled=True, capability_status="verified"),
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    attempt = provider._post("offline_annotation", "probe", png_1x1(), repair=False)

    assert attempt.error_code == "PROVIDER_SERVER_ERROR"
    assert attempt.error_class == "retryable" and attempt.retryable
    assert "do-not-expose" not in repr(attempt)


def test_wire_projection_omits_unique_items_but_local_validation_rejects_duplicates() -> None:
    for adapter in ("openai_responses", "openai_chat_completions"):
        provider = OpenAIProvider(profile(adapter=adapter))
        try:
            body = provider._body("offline_annotation", "probe", png_1x1())
        finally:
            provider.close()
        if adapter == "openai_responses":
            schema = body["text"]["format"]["schema"]
            assert body["text"]["format"]["strict"] is True
        else:
            schema = body["response_format"]["json_schema"]["schema"]
            assert body["response_format"]["json_schema"]["strict"] is True
        serialized = json.dumps(schema, sort_keys=True)
        assert "uniqueItems" not in serialized
        assert "const" not in serialized
        assert schema["properties"]["schema_id"]["enum"] == ["annotation-batch-output.v1"]
        assert "minItems" in serialized or "maxItems" in serialized

    duplicate = copy.deepcopy(annotation())
    duplicate["items"][0]["synonyms_en"] = ["stone", "stone"]  # type: ignore[index]
    result = validate_annotation_batch(duplicate, {"minecraft:stone"}, profile())
    assert result.status != "succeeded"
    assert result.error_code == "PROVIDER_SCHEMA_INVALID"
    const_violation = copy.deepcopy(annotation())
    const_violation["schema_id"] = "query-spec-output.v1"
    result = validate_annotation_batch(const_violation, {"minecraft:stone"}, profile())
    assert result.status != "succeeded"
    assert result.error_code == "PROVIDER_SCHEMA_INVALID"


def test_query_spec_wire_projection_is_adapter_specific_and_locally_enforced() -> None:
    projected_query_schemas: dict[str, dict[str, Any]] = {}

    def enum_values(value: Any) -> list[Any]:
        if isinstance(value, dict):
            values = list(value.get("enum", []))
            for child in value.values():
                values.extend(enum_values(child))
            return values
        if isinstance(value, list):
            values: list[Any] = []
            for child in value:
                values.extend(enum_values(child))
            return values
        return []

    for adapter in ("openai_responses", "openai_chat_completions"):
        provider = OpenAIProvider(profile(adapter=adapter))
        try:
            body = provider._body("query_spec", "probe", png_1x1())
        finally:
            provider.close()
        if adapter == "openai_responses":
            schema = body["text"]["format"]["schema"]
            assert body["text"]["format"]["strict"] is True
        else:
            schema = body["response_format"]["json_schema"]["schema"]
            assert body["response_format"]["json_schema"]["strict"] is True
        projected_query_schemas[adapter] = schema
        serialized = json.dumps(schema, sort_keys=True)
        assert "uniqueItems" not in serialized
        assert "const" not in serialized
        assert schema["properties"]["hard"]["properties"]["legal_state"]["properties"]["value"] == {"type": "boolean"}
        assert schema["properties"]["hard"]["properties"]["legal_state"]["properties"]["required"] == {"type": "boolean"}
        assert "minItems" in serialized or "maxItems" in serialized
        if adapter == "openai_responses":
            assert body["input"][0]["content"][1]["type"] == "input_image"
            assert base64.b64encode(png_1x1()).decode() in body["input"][0]["content"][1]["image_url"]
        else:
            assert body["messages"][0]["content"][1]["type"] == "image_url"
            assert base64.b64encode(png_1x1()).decode() in body["messages"][0]["content"][1]["image_url"]["url"]
        if adapter == "openai_responses":
            assert schema["properties"]["hard"]["properties"]["minecraft_version"]["properties"]["value"]["enum"] == ["26.2"]
            assert schema["properties"]["hard"]["properties"]["behaviors"]["items"]["properties"]["field"]["enum"]
            assert enum_values(schema)
        else:
            assert enum_values(schema) == []
            assert schema["properties"]["hard"]["properties"]["minecraft_version"]["properties"]["value"] == {"type": "string"}
            assert schema["properties"]["hard"]["properties"]["behaviors"]["items"]["properties"]["field"] == {"type": "string"}

    assert projected_query_schemas["openai_responses"] != projected_query_schemas["openai_chat_completions"]

    chat_provider = OpenAIProvider(profile(adapter="openai_chat_completions"))
    try:
        for stage in ("offline_annotation", "visual_rerank"):
            body = chat_provider._body(stage, "probe", png_1x1())
            schema = body["response_format"]["json_schema"]["schema"]
            assert enum_values(schema)
    finally:
        chat_provider.close()

    invalid_query = copy.deepcopy(query())
    invalid_query["hard"]["legal_state"]["value"] = False  # type: ignore[index]
    with pytest.raises(RecordSchemaError):
        validate_record("query-spec-output.v1", invalid_query)
    invalid_enum_query = copy.deepcopy(query())
    invalid_enum_query["hard"]["release_status"]["value"] = "not-current"  # type: ignore[index]
    with pytest.raises(RecordSchemaError):
        validate_record("query-spec-output.v1", invalid_enum_query)


def test_query_spec_missing_or_empty_image_fails_before_http() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    p = profile(adapter="openai_chat_completions", enabled=True, capability_status="verified")
    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    for image_png in (None, b""):
        args = request_args("query_spec", text="missing image", p=p)
        args["image_png"] = image_png
        result = provider.query_spec(**args)
        assert result.error_code == "PROVIDER_REQUEST_INVALID"
        assert result.attempts_used == 0

    assert calls == 0


def test_chat_query_spec_invalid_enum_is_rejected_locally() -> None:
    invalid_query = copy.deepcopy(query())
    invalid_query["hard"]["release_status"]["value"] = "not-current"  # type: ignore[index]
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=chat_response(invalid_query))

    p = profile(adapter="openai_chat_completions", enabled=True, capability_status="verified")
    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.query_spec(**request_args("query_spec", text="invalid enum", p=p))

    assert result.error_code == "PROVIDER_SCHEMA_INVALID"
    assert result.attempts_used == 2 and calls == 2


def test_chat_retry_never_switches_to_responses() -> None:
    urls: list[str] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        urls.append(str(request.url))
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=chat_response(annotation()))

    p = profile(adapter="openai_chat_completions", enabled=True, capability_status="verified")
    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("chat", **request_args(p=p))
    assert result.status == "succeeded" and result.attempts_used == 2
    assert calls == 2 and all(url.endswith("/chat/completions") for url in urls)


def test_probe_error_classification_must_be_real_and_non_retryable() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if body["text"]["format"]["strict"] is not True:
            return httpx.Response(429, json={"error": {"type": "rate_limit"}})
        name = body["text"]["format"]["name"]
        output = annotation() if name == "annotation_batch_output_v1" else query() if name == "query_spec_output_v1" else rerank()
        return httpx.Response(200, json=raw_response(output))

    provider = OpenAIResponsesProvider(
        profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.probe(png_1x1())
    assert result.capability_status == "failed" and not result.error_classification_supported
    assert result.error_code == "PROVIDER_CAPABILITY_MISSING" and len(seen) == 4


def test_missing_store_echo_does_not_block_responses_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["text"]["format"]["strict"] is not True:
            return httpx.Response(400, json={"error": {"type": "invalid_request"}})
        name = body["text"]["format"]["name"]
        output = annotation() if name == "annotation_batch_output_v1" else query() if name == "query_spec_output_v1" else rerank()
        payload = raw_response(output)
        payload.pop("store", None)
        return httpx.Response(200, json=payload)

    provider = OpenAIResponsesProvider(
        profile(),
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.probe(png_1x1())
    assert result.error_code is None
    assert result.capability_status == "verified"


def test_retry_budget_and_non_retry_refusal() -> None:
    calls = 0

    def retry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        return httpx.Response(200, json=raw_response(annotation()))

    provider = OpenAIResponsesProvider(
        enabled_profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(retry_handler)),
    )
    assert provider.annotate("minimal", **request_args()).status == "succeeded"
    assert calls == 2

    refusal_calls = 0

    def refusal_handler(request: httpx.Request) -> httpx.Response:
        nonlocal refusal_calls
        refusal_calls += 1
        response = raw_response(annotation())
        response["output"] = [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "no"}]}]
        return httpx.Response(200, json=response)

    refusing = OpenAIResponsesProvider(
        enabled_profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(refusal_handler)),
    )
    assert refusing.annotate("minimal", **request_args()).error_code == "PROVIDER_REFUSAL"
    assert refusal_calls == 1


@pytest.mark.parametrize(
    ("payload_kind", "expected"),
    [
        ("refusal", "PROVIDER_REFUSAL"),
        ("length", "PROVIDER_INCOMPLETE"),
        ("content_filter", "PROVIDER_INCOMPLETE"),
        ("tool_calls", "PROVIDER_INCOMPLETE"),
        ("function_call", "PROVIDER_INCOMPLETE"),
        ("unknown_finish", "PROVIDER_INCOMPLETE"),
        ("missing_content", "PROVIDER_INCOMPLETE"),
    ],
)
def test_chat_response_refusal_finish_reason_and_content_fail_closed(payload_kind: str, expected: str) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if payload_kind == "refusal":
            payload = chat_response(annotation(), refusal="not allowed")
        elif payload_kind in {"length", "content_filter", "unknown_finish"}:
            reason = "unknown" if payload_kind == "unknown_finish" else payload_kind
            payload = chat_response(annotation(), finish_reason=reason)
        elif payload_kind == "tool_calls":
            payload = chat_response(annotation(), content=[{"type": "tool_calls", "id": "call_1"}])
        elif payload_kind == "function_call":
            payload = chat_response(annotation(), content={"function_call": {"name": "bad"}})
        else:
            payload = {"model": "model-v1", "choices": [{"index": 0, "message": {"role": "assistant"}, "finish_reason": "stop"}]}
        return httpx.Response(200, json=payload)

    p = profile(adapter="openai_chat_completions", enabled=True, capability_status="verified")
    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("chat", **request_args(p=p))
    assert result.error_code == expected
    assert calls == 1


def test_annotation_routes_and_cache_key_changes() -> None:
    p = profile()
    valid = validate_annotation_batch(annotation(), {"minecraft:stone"}, p)
    assert valid.status == "succeeded" and valid.annotations[0]["source"]["model_id"] == "model-v1"
    low = json.loads(json.dumps(annotation()))
    low["items"][0]["confidence"] = 0.5
    assert validate_annotation_batch(low, {"minecraft:stone"}, p).priority == "high"
    extra = json.loads(json.dumps(annotation()))
    extra["items"][0]["geometry"] = {}
    assert validate_annotation_batch(extra, {"minecraft:stone"}, p).error_code == "PROVIDER_MACHINE_FACT_CONFLICT"
    key = {"image_hash": "sha256:" + "a" * 64, "machine_metadata_hash": "sha256:" + "b" * 64, "adapter": p.adapter, "prompt_version": "prompt.v1", "model_id": "model-v1", "schema_version": "annotation-batch-output.v1", "base_url_stable_id": p.base_url_stable_id, "stage": "offline_annotation"}
    assert build_cache_key(key) != build_cache_key({**key, "model_id": "model-v2"})


def test_adapter_changes_cache_and_envelope_wire_contract() -> None:
    responses = profile()
    chat = profile(adapter="openai_chat_completions")
    tile_map = {"tile_variant_map": [{
        "tile_id": "tile-1",
        "variant_id": "minecraft:stone",
        "image_sha256": "sha256:" + "a" * 64,
        "machine_metadata_sha256": "sha256:" + "b" * 64,
    }]}
    key = {
        "image_hash": "sha256:" + "a" * 64,
        "machine_metadata_hash": "sha256:" + "b" * 64,
        "adapter": responses.adapter,
        "prompt_version": "prompt.v1",
        "model_id": "model-v1",
        "schema_version": "annotation-batch-output.v1",
        "base_url_stable_id": responses.base_url_stable_id,
        "stage": "offline_annotation",
    }
    assert build_cache_key(key) != build_cache_key({**key, "adapter": chat.adapter})
    response_envelope = build_provider_batch_envelope(
        responses,
        request_id="ReqResponses",
        stage="offline_annotation",
        input_summary=tile_map,
        export_id="export_20260815T000000Z",
    )
    chat_envelope = build_provider_batch_envelope(
        chat,
        request_id="ReqChat",
        stage="offline_annotation",
        input_summary=tile_map,
        export_id="export_20260815T000000Z",
    )
    assert response_envelope["adapter"] == "openai_responses" and response_envelope["store"] is False
    assert chat_envelope["adapter"] == "openai_chat_completions" and "store" not in chat_envelope


def test_real_raw_nested_parser_rejects_sdk_only_output_text() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "completed", "model": "model-v1", "store": False, "usage": {"tokens": 9}, "cost": 3, "budget": 4, "output_text": json.dumps(annotation())})

    provider = OpenAIResponsesProvider(
        enabled_profile(),
        secret_resolver=SecretResolver(keyring_backend=Keyring("raw-secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("raw", **request_args())
    assert result.error_code == "PROVIDER_SCHEMA_INVALID"
    assert result.attempts_used == 2 and calls == 2
    assert "output_text" not in json.dumps(result.to_dict())
    assert all(value not in json.dumps(result.to_dict()) for value in ("raw-secret", "usage", "cost", "budget"))


@pytest.mark.parametrize(
    ("payload", "error_code"),
    [
        ({"status": "completed", "model": "model-v1", "store": False, "output": [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "no"}]}]}, "PROVIDER_REFUSAL"),
        ({"status": "incomplete", "model": "model-v1", "store": False, "incomplete_details": {"reason": "length"}, "output": []}, "PROVIDER_INCOMPLETE"),
        (nested_incomplete_response("in_progress"), "PROVIDER_INCOMPLETE"),
        (missing_model_response(), "PROVIDER_MODEL_UNAVAILABLE"),
    ],
)
def test_raw_refusal_incomplete_and_missing_model_fail_closed(payload: dict[str, object], error_code: str) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=payload)

    provider = OpenAIResponsesProvider(
        enabled_profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("raw", **request_args())
    assert result.error_code == error_code and result.error_class in {"non_retryable", "capability"}
    assert result.attempts_used == 1 and calls == 1


@pytest.mark.parametrize("adapter", ["openai_responses", "openai_chat_completions"])
def test_model_echo_mismatch_succeeds_without_entering_provenance(adapter: str) -> None:
    gateway_model = "gateway-routed-model"
    seen: list[dict[str, Any]] = []
    p = profile(adapter=adapter, enabled=True, capability_status="verified")

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        payload = (
            raw_response(annotation(), model=gateway_model)
            if adapter == "openai_responses"
            else chat_response(annotation(), model=gateway_model)
        )
        return httpx.Response(200, json=payload)

    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    request = request_args(p=p)
    result = provider.annotate("mismatch", **request)

    assert result.status == "succeeded" and result.error_code is None
    assert result.attempts_used == 1 and len(seen) == 1
    assert seen[0]["model"] == p.model_id
    assert request["envelope"]["model_id"] == p.model_id
    assert gateway_model not in json.dumps(result.to_dict())
    assert gateway_model not in json.dumps(request["envelope"])

    image = png_1x1()
    machine = {"minecraft:stone": {"fixture": "stone"}}
    expected_cache = build_cache_key(
        {
            "image_hash": hash_bytes(image),
            "machine_metadata_hash": hash_json(machine),
            "adapter": p.adapter,
            "prompt_version": p.prompt_version,
            "model_id": p.model_id,
            "schema_version": "annotation-batch-output.v1",
            "base_url_stable_id": p.base_url_stable_id,
            "stage": "offline_annotation",
        }
    )
    gateway_cache = build_cache_key(
        {
            "image_hash": hash_bytes(image),
            "machine_metadata_hash": hash_json(machine),
            "adapter": p.adapter,
            "prompt_version": p.prompt_version,
            "model_id": gateway_model,
            "schema_version": "annotation-batch-output.v1",
            "base_url_stable_id": p.base_url_stable_id,
            "stage": "offline_annotation",
        }
    )
    assert result.cache_key == expected_cache and result.cache_key != gateway_cache
    artifact = result.parsed_artifact
    assert artifact is not None and artifact == annotation()
    provenance = validate_annotation_batch(
        artifact,
        {"minecraft:stone"},
        p,
        cache_key=result.cache_key,
        artifact_hash=result.artifact_hash,
    )
    assert provenance.status == "succeeded"
    assert provenance.annotations[0]["source"]["model_id"] == p.model_id
    assert gateway_model not in json.dumps(provenance.to_dict())


@pytest.mark.parametrize("adapter", ["openai_responses", "openai_chat_completions"])
@pytest.mark.parametrize("model_shape", ["missing", "null", "number"])
def test_model_echo_must_be_a_string(adapter: str, model_shape: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        payload = raw_response(annotation()) if adapter == "openai_responses" else chat_response(annotation())
        if model_shape == "missing":
            payload.pop("model")
        else:
            payload["model"] = None if model_shape == "null" else 123
        return httpx.Response(200, json=payload)

    p = profile(adapter=adapter, enabled=True, capability_status="verified")
    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("invalid-model-echo", **request_args(p=p))

    assert result.status == "failed"
    assert result.error_code == "PROVIDER_MODEL_UNAVAILABLE"
    assert result.attempts_used == 1


def test_disabled_and_unverified_profile_make_zero_network_calls() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    provider = OpenAIResponsesProvider(
        profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("blocked", **request_args())
    assert result.error_code == "PROVIDER_CAPABILITY_MISSING" and result.attempts_used == 0 and calls == 0


def test_authoritative_capabilities_and_atomic_enable(tmp_path: Path) -> None:
    store = ProviderProfileStore(tmp_path)
    store.save(profile())
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=raw_response(annotation()))

    provider = OpenAIResponsesProvider(
        profile(), profile_store=store, secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    blocked = provider.annotate("blocked", **request_args())
    assert blocked.error_code == "PROVIDER_CAPABILITY_MISSING" and calls == 0
    store.record_probe({
        "profile_id": "default", "capability_status": "verified",
        "image_input_supported": True, "structured_outputs_supported": True,
        "error_classification_supported": True, "store_false_supported": True,
        "base_url_stable_id": profile().base_url_stable_id,
    })
    provider.enable()
    accepted = provider.annotate("accepted", **request_args())
    assert accepted.status == "succeeded" and calls == 1

    store.save(profile(profile_id="second", model_id="model-v2"))
    store.record_probe({
        "profile_id": "second", "capability_status": "verified",
        "image_input_supported": True, "structured_outputs_supported": True,
        "error_classification_supported": True, "store_false_supported": True,
        "base_url_stable_id": profile(profile_id="second", model_id="model-v2").base_url_stable_id,
    })
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def enable_second() -> None:
        try:
            barrier.wait()
            store.enable("second")
        except Exception as exc:  # one active profile must win atomically
            errors.append(exc)

    def update_second() -> None:
        barrier.wait()
        store.save(profile(profile_id="second", model_id="model-v3"))

    threads = [threading.Thread(target=enable_second), threading.Thread(target=update_second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(item.enabled for item in store.load().values()) == 1
    second = store.load()["second"]
    assert not second.enabled and second.capability_status == "unverified" and store.capabilities("second") is None
    store.disable("default")
    assert not store.load()["default"].enabled


def test_two_store_instances_share_profile_lock(tmp_path: Path) -> None:
    first = ProviderProfileStore(tmp_path)
    second = ProviderProfileStore(path=tmp_path / "provider-profiles.json")
    first.save(profile())
    first.save(profile(profile_id="second", model_id="model-v2"))
    for profile_id, model_id in (("default", "model-v1"), ("second", "model-v2")):
        first.record_probe({
            "profile_id": profile_id, "capability_status": "verified",
            "image_input_supported": True, "structured_outputs_supported": True,
            "error_classification_supported": True, "store_false_supported": True,
            "base_url_stable_id": profile(profile_id=profile_id, model_id=model_id).base_url_stable_id,
        })
    barrier = threading.Barrier(2)

    def enable(store: ProviderProfileStore, profile_id: str) -> None:
        barrier.wait()
        try:
            store.enable(profile_id)
        except ProviderProfileError:
            pass

    threads = [threading.Thread(target=enable, args=(first, "default")), threading.Thread(target=enable, args=(second, "second"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    loaded = ProviderProfileStore(path=tmp_path / "provider-profiles.json").load()
    assert sum(item.enabled for item in loaded.values()) == 1
    enabled_id = next(profile_id for profile_id, item in loaded.items() if item.enabled)
    second.disable(enabled_id)
    assert not ProviderProfileStore(path=tmp_path / "provider-profiles.json").load()[enabled_id].enabled


def test_repair_is_bounded_and_untrusted() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            broken = "x" * 5000
            response = raw_response(annotation())
            response["output"] = [{"type": "message", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": broken}]}]
            return httpx.Response(200, json=response)
        return httpx.Response(200, json=raw_response(annotation()))

    provider = OpenAIResponsesProvider(
        enabled_profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("repair", **request_args())
    repair_text = bodies[1]["input"][0]["content"][0]["text"]
    section = repair_text.split("<untrusted_previous_output>\n", 1)[1].split("\n</untrusted_previous_output>", 1)[0]
    assert result.status == "succeeded" and result.attempts_used == 2
    assert len(section) <= 2000 and "untrusted" in repair_text
    assert "x" * 2001 not in repair_text


def test_chat_schema_id_repair_uses_local_feedback_without_persisting_context() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            broken = annotation()
            broken["schema_id"] = "query-spec-output.v1"
            return httpx.Response(200, json=chat_response(broken))
        return httpx.Response(200, json=chat_response(annotation()))

    p = profile(adapter="openai_chat_completions", enabled=True, capability_status="verified")
    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("chat schema repair", **request_args(p=p))

    assert result.status == "succeeded" and result.attempts_used == 2
    assert len(bodies) == 2
    second_text = bodies[1]["messages"][0]["content"][0]["text"]
    assert "- schema_id: " in second_text
    assert "annotation-batch-output.v1" in second_text
    assert (
        "Repair to the supplied schema and make the top-level `schema_id` exactly equal "
        "to the selected `schema_id`: `annotation-batch-output.v1`."
    ) in second_text
    assert second_text.index("</untrusted_previous_output>") < second_text.index("Repair to the supplied schema")
    result_json = json.dumps(result.to_dict(), ensure_ascii=False)
    assert "repair_context" not in result.to_dict()
    assert "query-spec-output.v1" not in result_json
    assert "Local validation errors" not in result_json


@pytest.mark.parametrize("observed_type", ["unknown", object()])
def test_validation_diagnostic_rejects_unknown_or_non_json_observed_type(observed_type: object) -> None:
    diagnostic = {
        "stage": "offline_annotation",
        "phase": "wire_schema",
        "path": "$.items[0].reason",
        "keyword": "required",
        "observed_type": observed_type,
        "observed_length": None,
    }
    assert sanitize_validation_diagnostic(diagnostic) is None


@pytest.mark.parametrize("adapter", ["openai_responses", "openai_chat_completions"])
@pytest.mark.parametrize("failure", ["malformed_json", "output_shape", "missing_required", "wrong_type", "additional_property", "duplicate_array"])
def test_final_annotation_diagnostics_are_exactly_six_safe_fields(adapter: str, failure: str) -> None:
    p = profile(adapter=adapter, enabled=True, capability_status="verified")

    def invalid_output() -> object:
        if failure == "malformed_json":
            return "RAW_SECRET_OUTPUT {"
        if failure == "output_shape":
            return "[]"
        broken = copy.deepcopy(annotation())
        item = broken["items"][0]  # type: ignore[index]
        if failure == "missing_required":
            item.pop("reason")  # type: ignore[union-attr]
        elif failure == "wrong_type":
            item["confidence"] = "not-a-number"  # type: ignore[index]
        elif failure == "additional_property":
            item["raw_output"] = "RAW_SECRET_OUTPUT"  # type: ignore[index]
        else:
            item["synonyms_en"] = ["stone", "stone"]  # type: ignore[index]
        return json.dumps(broken, ensure_ascii=False)

    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        output = invalid_output()
        if adapter == "openai_responses":
            response = raw_response(annotation())
            response["output"][0]["content"][0]["text"] = output  # type: ignore[index]
        else:
            response = chat_response(annotation(), content=output)
        return httpx.Response(200, json=response)

    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("diagnostic", **request_args(p=p))
    diagnostic = result.validation_diagnostic
    assert result.error_code == "PROVIDER_SCHEMA_INVALID" and result.attempts_used == 2 and calls == 2
    assert diagnostic is not None
    assert set(diagnostic) == {"stage", "phase", "path", "keyword", "observed_type", "observed_length"}
    assert diagnostic["stage"] == "offline_annotation"
    assert diagnostic["phase"] in {"json_parse", "output_shape", "wire_schema"}
    diagnostic_text = json.dumps(diagnostic, ensure_ascii=False)
    assert all(secret not in diagnostic_text for secret in ("RAW_SECRET_OUTPUT", "sha256:", "message", "repair_context", "raw_output"))
    assert "RAW_SECRET_OUTPUT" not in json.dumps(result.to_dict(), ensure_ascii=False)
    provider.close()


@pytest.mark.parametrize("adapter", ["openai_responses", "openai_chat_completions"])
def test_successful_second_annotation_repair_has_no_diagnostic(adapter: str) -> None:
    calls = 0
    p = profile(adapter=adapter, enabled=True, capability_status="verified")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        output = "BROKEN_SECRET {" if calls == 1 else json.dumps(annotation(), ensure_ascii=False)
        if adapter == "openai_responses":
            response = raw_response(annotation())
            response["output"][0]["content"][0]["text"] = output  # type: ignore[index]
        else:
            response = chat_response(annotation(), content=output)
        return httpx.Response(200, json=response)

    provider = OpenAIProvider(
        p,
        secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    result = provider.annotate("repair-success", **request_args(p=p))
    assert result.status == "succeeded" and result.attempts_used == 2 and calls == 2
    assert result.validation_diagnostic is None
    assert "BROKEN_SECRET" not in json.dumps(result.to_dict(), ensure_ascii=False)
    provider.close()


def test_cache_caller_mismatch_makes_zero_network_calls() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=raw_response(annotation()))

    provider = OpenAIResponsesProvider(
        enabled_profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    mismatch_args = request_args()
    mismatch_args["image_hash"] = "sha256:" + "0" * 64
    result = provider.annotate("mismatch", **mismatch_args)
    assert result.error_code == "PROVIDER_REQUEST_INVALID" and result.attempts_used == 0 and calls == 0
    none_args = request_args()
    none_args["cache_parts"] = None
    explicit_none = provider.annotate("none", **none_args)
    assert explicit_none.error_code == "PROVIDER_REQUEST_INVALID" and calls == 0


def test_envelope_and_machine_inputs_are_verified_before_network() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=raw_response(annotation()))

    provider = OpenAIResponsesProvider(
        enabled_profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    valid = request_args()
    assert provider.annotate("valid", **valid).status == "succeeded"
    assert calls == 1

    bad_envelope = copy.deepcopy(valid)
    bad_envelope["envelope"]["model_id"] = "forged-model"
    assert provider.annotate("bad-envelope", **bad_envelope).error_code == "PROVIDER_CONFIG_INVALID"
    assert calls == 1

    bad_machine = request_args()
    bad_machine["machine_metadata"] = {"minecraft:stone": {"fixture": "forged"}}
    assert provider.annotate("bad-machine", **bad_machine).error_code == "PROVIDER_MACHINE_FACT_CONFLICT"
    assert calls == 1

    bad_image = request_args()
    bad_image["source_images"] = {"tile-1": b"different-source-image"}
    assert provider.annotate("bad-image", **bad_image).error_code == "PROVIDER_MACHINE_FACT_CONFLICT"
    assert calls == 1

    missing_images = request_args()
    missing_images["source_images"] = None
    assert provider.annotate("missing-images", **missing_images).error_code == "PROVIDER_MACHINE_FACT_CONFLICT"
    assert calls == 1

    query_args = request_args("query_spec", text="actual query")
    query_args["query_text"] = "different query"
    assert provider.query_spec(**query_args).error_code == "PROVIDER_MACHINE_FACT_CONFLICT"
    assert calls == 1


def test_visual_rerank_inputs_are_canonical_and_fully_verified() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=raw_response(rerank()))

    provider = OpenAIResponsesProvider(
        enabled_profile(), secret_resolver=SecretResolver(keyring_backend=Keyring("secret")),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    valid = request_args("visual_rerank", text="visual query")
    assert provider.visual_rerank("visual", **valid).status == "succeeded"
    assert calls == 1

    for field, value in (
        ("query_sha256", "sha256:" + "0" * 64),
        ("query_spec_sha256", "sha256:" + "1" * 64),
        ("candidate_set_sha256", "sha256:" + "2" * 64),
    ):
        bad = copy.deepcopy(valid)
        bad["envelope"]["input_summary"][field] = value
        assert provider.visual_rerank("visual", **bad).error_class == "validation"
        assert calls == 1

    for field in ("variant_id", "block_id", "recommended_state_id", "image_sha256"):
        bad = copy.deepcopy(valid)
        bad["envelope"]["input_summary"]["candidate_map"][0][field] = "minecraft:other" if field != "image_sha256" else "sha256:" + "3" * 64
        assert provider.visual_rerank("visual", **bad).error_class == "validation"
        assert calls == 1

    bad_source = copy.deepcopy(valid)
    bad_source["source_images"] = {"A1": b"different-image"}
    assert provider.visual_rerank("visual", **bad_source).error_class == "validation"
    assert calls == 1


def test_annotation_duplicate_ids_and_provenance() -> None:
    p = profile()
    duplicate = validate_annotation_batch(annotation(), ["minecraft:stone", "minecraft:stone"], p)
    assert duplicate.error_code == "PROVIDER_OUTPUT_ID_MISMATCH"
    base = annotation()
    changed = json.loads(json.dumps(base))
    changed["items"][0]["summary_en"] = "A changed stone block."
    base_hash = hash_json(base)
    changed_hash = hash_json(changed)
    first = validate_annotation_batch(base, ["minecraft:stone"], p, cache_key="sha256:" + "a" * 64, artifact_hash=base_hash)
    same = validate_annotation_batch(base, ["minecraft:stone"], p, cache_key="sha256:" + "a" * 64, artifact_hash=base_hash)
    changed_cache = validate_annotation_batch(base, ["minecraft:stone"], p, cache_key="sha256:" + "c" * 64, artifact_hash=base_hash)
    changed_artifact = validate_annotation_batch(changed, ["minecraft:stone"], p, cache_key="sha256:" + "a" * 64, artifact_hash=changed_hash)
    mismatch = validate_annotation_batch(base, ["minecraft:stone"], p, cache_key="sha256:" + "a" * 64, artifact_hash="sha256:" + "f" * 64)
    assert mismatch.error_code == "PROVIDER_SCHEMA_INVALID" and not mismatch.annotations
    assert first.annotations[0]["annotation_id"] == same.annotations[0]["annotation_id"]
    assert first.annotations[0]["annotation_id"] != changed_cache.annotations[0]["annotation_id"]
    assert first.annotations[0]["annotation_id"] != changed_artifact.annotations[0]["annotation_id"]


def test_error_classes_satisfy_frozen_sql_check(tmp_path: Path) -> None:
    allowed = ["retryable", "non_retryable", "validation", "authentication", "capability", "unknown"]
    db_path = tmp_path / "workspace.sqlite3"
    with WorkspaceDatabase.open(db_path) as database:
        with database.transaction() as connection:
            connection.execute(
                "INSERT INTO provider_profiles(profile_id, model_id, base_url_stable_id, secret_reference, profile_json) VALUES (?, ?, ?, ?, ?)",
                ("default", "model-v1", "https://api.openai.com/v1", "keyring:blockpedia/default", "{}"),
            )
            for number, error_class in enumerate(allowed):
                result = ProviderResult("needs_review", "offline_annotation", "annotation-batch-output.v1", None, None, 1, "PROVIDER_UNKNOWN", error_class, "sha256:" + "a" * 64, None)
                connection.execute(
                    "INSERT INTO provider_requests(request_id, profile_id, stage, wire_schema_id, attempt, cache_key, input_sha256, error_code, error_class, envelope_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"Req{number}", "default", result.stage, result.wire_schema_id, result.attempts_used, result.cache_key, "sha256:" + "b" * 64, result.error_code, result.error_class, "{}", result.status, "2026-08-15T00:00:00Z"),
                )
        row = database.fetchone("SELECT COUNT(*) AS count FROM provider_requests")
        assert row is not None and row["count"] == len(allowed)
