from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from blockpedia.provider import OpenAIProvider, SecretResolver
from .test_provider_core import Keyring, annotation, profile, raw_response, request_args
from .test_pipeline_review import _FakeProvider, _approve_first, _service


ADAPTERS = ("openai_responses", "openai_chat_completions")


def sse(event: Any) -> bytes:
    # Multi-line data, CRLF and non-ASCII exercise real SSE framing/decoding.
    text = json.dumps(event, ensure_ascii=False, indent=2)
    return ("event: message\r\n" + "\r\n".join("data: " + line for line in text.split("\n")) + "\r\n\r\n").encode("utf-8")


def chat_chunk(content: str | None = None, *, finish: str | None = None, **delta: Any) -> dict[str, Any]:
    return {"model": "gateway-model", "choices": [{"index": 0, "delta": {"content": content, **delta}, "finish_reason": finish}]}


def complete_stream(adapter: str, artifact: dict[str, Any] | None = None) -> bytes:
    artifact = annotation() if artifact is None else artifact
    text = json.dumps(artifact, ensure_ascii=False)
    if adapter == "openai_responses":
        return (
            sse({"type": "response.created", "response": {"model": "gateway-model"}})
            + sse({"type": "response.output_text.delta", "delta": text[:20]})
            + sse({"type": "response.output_text.delta", "delta": text[20:]})
            + sse({"type": "response.completed", "response": raw_response(artifact, model="gateway-model")})
        )
    return (
        sse(chat_chunk(role="assistant"))
        + sse(chat_chunk(text[:20])) + sse(chat_chunk(text[20:]))
        + sse(chat_chunk(finish="stop"))
        + sse({"model": "gateway-model", "choices": [], "usage": {"total_tokens": 999}})
        + b"data: [DONE]\r\n\r\n"
    )


class ByteStream(httpx.SyncByteStream):
    def __init__(self, body: bytes, *, error: Exception | None = None, forbid_eof: bool = False) -> None:
        self.body = body
        self.error = error
        self.forbid_eof = forbid_eof
        self.closed = False

    def __iter__(self):
        for byte in self.body:
            yield bytes([byte])
        if self.error is not None:
            raise self.error
        if self.forbid_eof:
            raise AssertionError("The provider must stop at the terminal event without buffering to EOF")

    def close(self) -> None:
        self.closed = True


def run_annotation(adapter: str, handler):
    p = profile(adapter=adapter, enabled=True, capability_status="verified")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with OpenAIProvider(p, secret_resolver=SecretResolver(keyring_backend=Keyring("fixture-secret")), client=client) as provider:
            return provider.annotate("stream", **request_args(p=p))


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("line_ending", [b"\n", b"\r", b"\r\n"])
def test_annotation_streams_utf8_and_closes_at_terminal_without_eager_read(adapter: str, line_ending: bytes) -> None:
    body = (b"\xef\xbb\xbf: heartbeat\r\n\r\n" + complete_stream(adapter)).replace(b"\r\n", line_ending)
    stream = ByteStream(body, forbid_eof=True)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert request.headers["accept"] == "text/event-stream"
        if adapter == "openai_responses":
            assert body["store"] is False and body["text"]["format"]["strict"] is True
        else:
            assert "store" not in body and body["response_format"]["json_schema"]["strict"] is True
        return httpx.Response(200, headers={"content-type": "text/event-stream; charset=utf-8", "x-request-id": "req_stream_fixture"}, stream=stream)

    result = run_annotation(adapter, handler)
    assert result.status == "succeeded" and result.parsed_artifact == annotation()
    assert result.attempts_used == 1 and result.request_id_redacted
    assert stream.closed
    assert all(value not in json.dumps(result.to_dict()) for value in ("gateway-model", "fixture-secret", "total_tokens"))


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("failure", [httpx.ReadTimeout, httpx.ReadError])
def test_midstream_transport_failure_retries_without_reusing_partial_output(adapter: str, failure) -> None:
    streams: list[ByteStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        partial = sse({"type": "response.output_text.delta", "delta": "BROKEN_SECRET"}) if adapter == "openai_responses" else sse(chat_chunk("BROKEN_SECRET"))
        stream = ByteStream(partial, error=failure("untrusted transport detail")) if not streams else ByteStream(complete_stream(adapter), forbid_eof=True)
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    result = run_annotation(adapter, handler)
    assert result.status == "succeeded" and result.parsed_artifact == annotation()
    assert result.attempts_used == 2 and len(streams) == 2 and all(stream.closed for stream in streams)
    assert "BROKEN_SECRET" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize(("failure", "code"), [(httpx.ReadTimeout, "PROVIDER_TIMEOUT"), (httpx.ReadError, "PROVIDER_NETWORK_ERROR")])
def test_midstream_transport_failure_stays_within_retry_budget(adapter: str, failure, code: str) -> None:
    streams: list[ByteStream] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        stream = ByteStream(b": heartbeat\n\n", error=failure("UNTRUSTED_SECRET"))
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    result = run_annotation(adapter, handler)
    assert result.error_code == code and result.parsed_artifact is None and result.artifact_hash is None
    assert result.attempts_used == 2 and len(streams) == 2 and all(stream.closed for stream in streams)
    assert "UNTRUSTED_SECRET" not in json.dumps(result.to_dict())


@pytest.mark.parametrize(("adapter", "body", "code"), [
    ("openai_responses", sse({"type": "response.output_text.delta", "delta": json.dumps(annotation())}), "PROVIDER_INCOMPLETE"),
    ("openai_responses", sse({"type": "response.completed", "response": raw_response(annotation())}).rstrip(), "PROVIDER_INCOMPLETE"),
    ("openai_responses", sse({"type": "response.incomplete"}), "PROVIDER_INCOMPLETE"),
    ("openai_responses", sse({"type": "response.failed"}), "PROVIDER_INCOMPLETE"),
    ("openai_responses", sse({"type": "response.refusal.delta", "delta": "no"}), "PROVIDER_REFUSAL"),
    ("openai_responses", sse({"type": "error", "message": "UNTRUSTED_SECRET"}), "PROVIDER_REQUEST_INVALID"),
    ("openai_chat_completions", sse(chat_chunk(json.dumps(annotation()))) + b"data: [DONE]\n\n", "PROVIDER_INCOMPLETE"),
    ("openai_chat_completions", sse(chat_chunk(json.dumps(annotation()), finish="stop")), "PROVIDER_INCOMPLETE"),
    ("openai_chat_completions", sse(chat_chunk(refusal="no")), "PROVIDER_REFUSAL"),
    ("openai_chat_completions", sse(chat_chunk(tool_calls=[{"id": "call1"}])), "PROVIDER_INCOMPLETE"),
    ("openai_chat_completions", sse(chat_chunk(role="user")), "PROVIDER_INCOMPLETE"),
    ("openai_chat_completions", sse({"error": {"message": "UNTRUSTED_SECRET"}}), "PROVIDER_REQUEST_INVALID"),
])
def test_stream_failures_never_return_partial_annotations(adapter: str, body: bytes, code: str) -> None:
    streams: list[ByteStream] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        stream = ByteStream(body)
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    result = run_annotation(adapter, handler)
    assert result.error_code == code and result.parsed_artifact is None and result.artifact_hash is None
    assert result.attempts_used == 1 and len(streams) == 1 and streams[0].closed
    assert "UNTRUSTED_SECRET" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_malformed_sse_uses_only_one_streaming_repair(adapter: str) -> None:
    streams: list[ByteStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        stream = ByteStream(b"data: {UNTRUSTED_SECRET\n\n")
        streams.append(stream)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    result = run_annotation(adapter, handler)
    assert result.error_code == "PROVIDER_SCHEMA_INVALID" and result.parsed_artifact is None
    assert result.attempts_used == 2 and len(streams) == 2 and all(stream.closed for stream in streams)
    assert "UNTRUSTED_SECRET" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("adapter", ADAPTERS)
def test_non_streaming_response_is_not_an_annotation_fallback(adapter: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, json=raw_response(annotation()))

    result = run_annotation(adapter, handler)
    assert result.error_code == "PROVIDER_INCOMPLETE" and result.parsed_artifact is None
    assert result.attempts_used == 1


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_separators_inside_annotation_text_are_preserved(adapter: str, separator: str) -> None:
    artifact = annotation()
    artifact["items"][0]["summary_zh"] = "石" + separator + "块"
    if adapter == "openai_responses":
        event = {"type": "response.completed", "response": raw_response(artifact)}
    else:
        event = chat_chunk(json.dumps(artifact, ensure_ascii=False), finish="stop")
    body = ("data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n").encode("utf-8")
    if adapter == "openai_chat_completions":
        body += b"data: [DONE]\r\n\r\n"
    stream = ByteStream(body, forbid_eof=True)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    result = run_annotation(adapter, handler)
    assert result.status == "succeeded" and result.parsed_artifact == artifact
    assert result.attempts_used == 1 and stream.closed


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("separator", ["\u0085", "\u2028", "\u2029"])
def test_unicode_separators_cannot_terminate_an_sse_frame(adapter: str, separator: str) -> None:
    if adapter == "openai_responses":
        event = {"type": "response.completed", "response": raw_response(annotation())}
        body = ("data: " + json.dumps(event, ensure_ascii=False)).encode("utf-8")
    else:
        event = chat_chunk(json.dumps(annotation()), finish="stop")
        body = ("data: " + json.dumps(event) + "\r\n\r\ndata: [DONE]").encode("utf-8")
    stream = ByteStream(body + (separator * 2).encode("utf-8"))

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

    result = run_annotation(adapter, handler)
    assert result.error_code == "PROVIDER_INCOMPLETE" and result.parsed_artifact is None
    assert result.attempts_used == 1 and stream.closed


@pytest.mark.parametrize("adapter", ADAPTERS)
@pytest.mark.parametrize("complete", [True, False])
def test_worker_persists_only_a_complete_streamed_batch(tmp_path: Path, adapter: str, complete: bool) -> None:
    service, run_id, _ = _service(tmp_path, _FakeProvider(), adapter=adapter)
    clients: list[httpx.Client] = []
    streams: list[ByteStream] = []
    try:
        preview = service.preview_ai_batch(run_id)
        ids = [tile["variant_id"] for tile in preview["tiles"]]
        artifact = {"schema_id": "annotation-batch-output.v1", "items": [{**annotation()["items"][0], "variant_id": variant_id} for variant_id in ids]}

        def handler(request: httpx.Request) -> httpx.Response:
            assert json.loads(request.content)["stream"] is True
            if complete:
                body = complete_stream(adapter, artifact)
            else:
                text = json.dumps(artifact)
                body = sse({"type": "response.output_text.delta", "delta": text}) if adapter == "openai_responses" else sse(chat_chunk(text))
            stream = ByteStream(body, forbid_eof=complete)
            streams.append(stream)
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=stream)

        def factory(p, **_kwargs):
            client = httpx.Client(transport=httpx.MockTransport(handler))
            clients.append(client)
            return OpenAIProvider(p, secret_resolver=SecretResolver(keyring_backend=Keyring("fixture-secret")), client=client)

        service.worker.provider_factory = factory
        _approve_first(service, run_id)
        service.tick(run_id)
        with service.worker.open_database(run_id) as database:
            annotations = database.fetchall("SELECT subject_id,record_json FROM annotations")
            request = database.fetchone("SELECT status,error_code FROM provider_requests")
        assert streams and all(stream.closed for stream in streams)
        assert request is not None
        if complete:
            assert {row["subject_id"] for row in annotations} == set(ids)
            assert all(json.loads(row["record_json"])["summary_zh"] == "石块" for row in annotations)
            assert request["status"] == "succeeded" and request["error_code"] is None
        else:
            assert not annotations
            assert request["status"] == "needs_review" and request["error_code"] == "PROVIDER_INCOMPLETE"
    finally:
        service.close()
        for client in clients:
            client.close()
