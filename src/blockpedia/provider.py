"""Small, strict OpenAI Responses provider core.

The module intentionally owns only the provider boundary.  Web routes, worker
state, review persistence, and release construction belong to later phases.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import posixpath
import re
import threading
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx

from .schema import (
    RecordSchemaError,
    load_provider_wire_schema,
    validate_record,
)


DEFAULT_BASE_URL = "https://api.openai.com/v1"
WIRE_NAMES: dict[str, str] = {
    "annotation-batch-output.v1": "annotation_batch_output_v1",
    "query-spec-output.v1": "query_spec_output_v1",
    "rerank-output.v1": "rerank_output_v1",
}
STAGES = frozenset({"offline_annotation", "query_spec", "visual_rerank"})
STAGE_SCHEMAS = {
    "offline_annotation": "annotation-batch-output.v1",
    "query_spec": "query-spec-output.v1",
    "visual_rerank": "rerank-output.v1",
}
_CACHE_PARTS_MISSING = object()
_PROFILE_STORE_LOCK = threading.RLock()
ERROR_CODES = frozenset(
    {
        "PROVIDER_NOT_CONFIGURED",
        "PROVIDER_CONFIG_INVALID",
        "PROVIDER_CAPABILITY_MISSING",
        "PROVIDER_STORAGE_UNSUPPORTED",
        "PROVIDER_AUTH_FAILED",
        "PROVIDER_PERMISSION_DENIED",
        "PROVIDER_MODEL_UNAVAILABLE",
        "PROVIDER_NETWORK_ERROR",
        "PROVIDER_TIMEOUT",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_SERVER_ERROR",
        "PROVIDER_REQUEST_INVALID",
        "PROVIDER_PAYLOAD_TOO_LARGE",
        "PROVIDER_REFUSAL",
        "PROVIDER_INCOMPLETE",
        "PROVIDER_SCHEMA_INVALID_REPAIRABLE",
        "PROVIDER_SCHEMA_INVALID",
        "PROVIDER_OUTPUT_ID_MISMATCH",
        "PROVIDER_MACHINE_FACT_CONFLICT",
        "PROVIDER_CANCELLED",
        "PROVIDER_UNKNOWN",
    }
)
_PROFILE_FIELDS = frozenset(
    {
        "profile_id",
        "adapter",
        "base_url",
        "base_url_stable_id",
        "model_id",
        "secret_reference",
        "enabled",
        "capability_status",
        "prompt_version",
        "annotation_output_schema_id",
        "query_spec_output_schema_id",
        "rerank_output_schema_id",
        "search_ranking_version",
        "request_timeout_ms",
        "stages",
    }
)
_CAPABILITY_FIELDS = frozenset(
    {
        "adapter",
        "image_input_supported",
        "structured_outputs_supported",
        "error_classification_supported",
        # Legacy persisted field; current probe output and enablement ignore it.
        "store_false_supported",
        "capability_status",
        "error_code",
        "base_url_stable_id",
        "probed_at",
        "request_id_redacted",
    }
)
_ERROR_CLASSES = frozenset(
    {"retryable", "non_retryable", "validation", "authentication", "capability", "unknown"}
)
_ERROR_CLASS_BY_CODE = {
    "PROVIDER_NETWORK_ERROR": "retryable",
    "PROVIDER_TIMEOUT": "retryable",
    "PROVIDER_RATE_LIMITED": "retryable",
    "PROVIDER_SERVER_ERROR": "retryable",
    "PROVIDER_SCHEMA_INVALID_REPAIRABLE": "validation",
    "PROVIDER_SCHEMA_INVALID": "validation",
    "PROVIDER_OUTPUT_ID_MISMATCH": "validation",
    "PROVIDER_MACHINE_FACT_CONFLICT": "validation",
    "PROVIDER_AUTH_FAILED": "authentication",
    "PROVIDER_NOT_CONFIGURED": "capability",
    "PROVIDER_CONFIG_INVALID": "capability",
    "PROVIDER_CAPABILITY_MISSING": "capability",
    "PROVIDER_STORAGE_UNSUPPORTED": "capability",
    "PROVIDER_MODEL_UNAVAILABLE": "capability",
    "PROVIDER_REFUSAL": "non_retryable",
    "PROVIDER_INCOMPLETE": "non_retryable",
    "PROVIDER_PERMISSION_DENIED": "non_retryable",
    "PROVIDER_REQUEST_INVALID": "non_retryable",
    "PROVIDER_PAYLOAD_TOO_LARGE": "non_retryable",
    "PROVIDER_CANCELLED": "non_retryable",
}
_INVALID_REQUEST_MARKERS = frozenset({"invalid_request", "invalid_request_error"})
_MACHINE_FACT_KEYS = frozenset(
    {
        "block_id",
        "state_id",
        "state",
        "legal_state",
        "geometry",
        "machine_facts",
        "behavior",
        "behaviors",
        "transparent",
        "emissive",
        "emission",
        "support",
        "waterloggable",
        "redstone_related",
        "candidate_qualification",
        "publish_status",
        "release_status",
        "minecraft_version",
        "image_path",
        "image_sha256",
    }
)


class ProviderError(ValueError):
    """A profile or provider input violates the frozen provider contract."""


class ProviderProfileError(ProviderError):
    """A persisted or constructed profile is invalid."""


def _profile_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value):
        raise ProviderProfileError("invalid profile_id")
    return value


def _normalize_base_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ProviderProfileError("base_url must be a URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ProviderProfileError("invalid base_url") from exc
    if not parsed.scheme or not hostname or parsed.username is not None or parsed.password is not None:
        raise ProviderProfileError("base_url must not contain userinfo")
    if parsed.query or parsed.fragment:
        raise ProviderProfileError("base_url must not contain query or fragment")
    scheme = parsed.scheme.lower()
    host = hostname.lower()
    if scheme == "http" and host != "127.0.0.1":
        raise ProviderProfileError("remote HTTP base_url is not allowed")
    if scheme not in {"https", "http"}:
        raise ProviderProfileError("base_url must use HTTPS")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = host if port is None else f"{host}:{port}"
    path = parsed.path or ""
    if path:
        path = posixpath.normpath("/" + path.lstrip("/"))
        if path == "/":
            path = ""
    return urlunsplit((scheme, netloc, path.rstrip("/"), "", ""))


@dataclass(frozen=True, slots=True)
class StageConfig:
    batch_size: int
    concurrency: int

    def __post_init__(self) -> None:
        if not isinstance(self.batch_size, int) or isinstance(self.batch_size, bool):
            raise ProviderProfileError("batch_size must be an integer")
        if not isinstance(self.concurrency, int) or isinstance(self.concurrency, bool):
            raise ProviderProfileError("concurrency must be an integer")
        if not 1 <= self.concurrency <= 5:
            raise ProviderProfileError("concurrency must be between 1 and 5")

    @classmethod
    def from_value(cls, value: Mapping[str, Any]) -> "StageConfig":
        if not isinstance(value, Mapping) or set(value) != {"batch_size", "concurrency"}:
            raise ProviderProfileError("stage configuration has unknown or missing fields")
        return cls(batch_size=value["batch_size"], concurrency=value["concurrency"])

    def to_dict(self) -> dict[str, int]:
        return {"batch_size": self.batch_size, "concurrency": self.concurrency}


def _default_stages() -> dict[str, StageConfig]:
    return {
        "offline_annotation": StageConfig(batch_size=12, concurrency=1),
        "query_spec": StageConfig(batch_size=1, concurrency=1),
        "visual_rerank": StageConfig(batch_size=1, concurrency=1),
    }


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    profile_id: str
    model_id: str
    adapter: str = "openai_responses"
    base_url: str = DEFAULT_BASE_URL
    base_url_stable_id: str | None = None
    secret_reference: str | None = None
    enabled: bool = False
    capability_status: str = "unverified"
    prompt_version: str = "prompt.v1"
    annotation_output_schema_id: str = "annotation-batch-output.v1"
    query_spec_output_schema_id: str = "query-spec-output.v1"
    rerank_output_schema_id: str = "rerank-output.v1"
    search_ranking_version: str = "search-ranking.v1"
    request_timeout_ms: int = 60000
    stages: Mapping[str, StageConfig | Mapping[str, Any]] = field(default_factory=_default_stages)

    def __post_init__(self) -> None:
        profile_id = _profile_id(self.profile_id)
        object.__setattr__(self, "profile_id", profile_id)
        if self.adapter not in {"openai_responses", "openai_chat_completions"}:
            raise ProviderProfileError("adapter must be openai_responses or openai_chat_completions")
        if not isinstance(self.model_id, str) or not self.model_id or len(self.model_id) > 200:
            raise ProviderProfileError("invalid model_id")
        if self.model_id.lower() == "latest" or re.search(r"[<>=*^~]", self.model_id):
            raise ProviderProfileError("model_id must be an exact model id")
        base_url = _normalize_base_url(self.base_url)
        object.__setattr__(self, "base_url", base_url)
        if self.base_url_stable_id is not None:
            stable_id = _normalize_base_url(self.base_url_stable_id)
            if stable_id != base_url:
                raise ProviderProfileError("base_url_stable_id does not match base_url")
        object.__setattr__(self, "base_url_stable_id", base_url)
        secret_reference = self.secret_reference or f"keyring:blockpedia/{profile_id}"
        if not isinstance(secret_reference, str) or not re.fullmatch(
            rf"(?:keyring:blockpedia/{re.escape(profile_id)}|env:OPENAI_API_KEY)", secret_reference
        ):
            raise ProviderProfileError("invalid secret_reference")
        object.__setattr__(self, "secret_reference", secret_reference)
        if self.capability_status not in {"draft", "unverified", "verified", "failed"}:
            raise ProviderProfileError("invalid capability_status")
        if not isinstance(self.enabled, bool):
            raise ProviderProfileError("enabled must be boolean")
        if self.enabled and self.capability_status != "verified":
            raise ProviderProfileError("enabled profile must be verified")
        if not isinstance(self.prompt_version, str) or not 1 <= len(self.prompt_version) <= 128:
            raise ProviderProfileError("invalid prompt_version")
        if self.annotation_output_schema_id != "annotation-batch-output.v1":
            raise ProviderProfileError("invalid annotation schema id")
        if self.query_spec_output_schema_id != "query-spec-output.v1":
            raise ProviderProfileError("invalid query schema id")
        if self.rerank_output_schema_id != "rerank-output.v1":
            raise ProviderProfileError("invalid rerank schema id")
        if self.search_ranking_version != "search-ranking.v1":
            raise ProviderProfileError("invalid search_ranking_version")
        if not isinstance(self.request_timeout_ms, int) or isinstance(self.request_timeout_ms, bool) or not 1000 <= self.request_timeout_ms <= 600000:
            raise ProviderProfileError("request_timeout_ms must be between 1000 and 600000")
        if not isinstance(self.stages, Mapping) or set(self.stages) != STAGES:
            raise ProviderProfileError("all three stage configurations are required")
        normalized: dict[str, StageConfig] = {}
        for stage, config in self.stages.items():
            normalized[stage] = config if isinstance(config, StageConfig) else StageConfig.from_value(config)
            if stage == "offline_annotation" and not 8 <= normalized[stage].batch_size <= 16:
                raise ProviderProfileError("offline_annotation batch_size must be 8-16")
            if stage != "offline_annotation" and normalized[stage].batch_size != 1:
                raise ProviderProfileError(f"{stage} batch_size must be 1")
            if stage != "offline_annotation" and normalized[stage].concurrency != 1:
                raise ProviderProfileError(f"{stage} concurrency must be 1")
        object.__setattr__(self, "stages", MappingProxyType(normalized))

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderProfile":
        if not isinstance(value, Mapping) or set(value) != _PROFILE_FIELDS:
            raise ProviderProfileError("provider profile has unknown or missing fields")
        return cls(**dict(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "adapter": self.adapter,
            "base_url": self.base_url,
            "base_url_stable_id": self.base_url_stable_id,
            "model_id": self.model_id,
            "secret_reference": self.secret_reference,
            "enabled": self.enabled,
            "capability_status": self.capability_status,
            "prompt_version": self.prompt_version,
            "annotation_output_schema_id": self.annotation_output_schema_id,
            "query_spec_output_schema_id": self.query_spec_output_schema_id,
            "rerank_output_schema_id": self.rerank_output_schema_id,
            "search_ranking_version": self.search_ranking_version,
            "request_timeout_ms": self.request_timeout_ms,
            "stages": {
                stage: (config.to_dict() if isinstance(config, StageConfig) else StageConfig.from_value(config).to_dict())
                for stage, config in self.stages.items()
            },
        }

    def with_capability(self, status: str, *, enabled: bool | None = None) -> "ProviderProfile":
        return replace(self, capability_status=status, enabled=self.enabled if enabled is None else enabled)


def profile_differs_only_in_offline_concurrency(
    previous: ProviderProfile,
    current: ProviderProfile,
) -> bool:
    """Return whether the effective profile edit only changes offline bound."""

    if previous.profile_id != current.profile_id:
        return False
    old = previous.to_dict()
    new = current.to_dict()
    for value in (old, new):
        value.pop("enabled", None)
        value.pop("capability_status", None)
        stages = value.get("stages")
        if isinstance(stages, dict):
            offline = stages.get("offline_annotation")
            if isinstance(offline, dict):
                offline.pop("concurrency", None)
    previous_concurrency = getattr(previous.stages["offline_annotation"], "concurrency", None)
    current_concurrency = getattr(current.stages["offline_annotation"], "concurrency", None)
    return old == new and previous_concurrency != current_concurrency


class ProviderProfileStore:
    """Authoritative, non-secret profile file under ``cache``."""

    def __init__(self, cache_dir: str | Path | None = None, *, path: str | Path | None = None) -> None:
        if path is not None and cache_dir is not None:
            raise ProviderProfileError("specify cache_dir or path, not both")
        self.path = Path(path) if path is not None else Path(cache_dir or ".") / "provider-profiles.json"
        self._lock = _PROFILE_STORE_LOCK

    def _read_document(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"profiles": [], "capabilities": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderProfileError("provider profile file is unreadable") from exc
        if not isinstance(value, dict) or set(value) != {"profiles", "capabilities"}:
            raise ProviderProfileError("provider profile file has unknown fields")
        raw_profiles = value["profiles"]
        raw_capabilities = value["capabilities"]
        if not isinstance(raw_profiles, list) or not isinstance(raw_capabilities, dict):
            raise ProviderProfileError("provider profile file has invalid containers")
        profiles: list[ProviderProfile] = []
        ids: set[str] = set()
        for raw in raw_profiles:
            profile = ProviderProfile.from_dict(raw)
            if profile.profile_id in ids:
                raise ProviderProfileError("duplicate profile_id")
            ids.add(profile.profile_id)
            profiles.append(profile)
        self._validate_enabled(profiles)
        capabilities: dict[str, dict[str, Any]] = {}
        for profile_id, raw in raw_capabilities.items():
            if profile_id not in ids or not isinstance(raw, dict) or not set(raw) <= _CAPABILITY_FIELDS:
                raise ProviderProfileError("invalid persisted capabilities")
            if any(not isinstance(raw[key], bool) for key in {
                "image_input_supported",
                "structured_outputs_supported",
                "error_classification_supported",
                "store_false_supported",
            } if key in raw):
                raise ProviderProfileError("capability flags must be boolean")
            if "adapter" in raw and raw["adapter"] not in {"openai_responses", "openai_chat_completions"}:
                raise ProviderProfileError("invalid persisted capability adapter")
            capabilities[profile_id] = dict(raw)
        return {"profiles": profiles, "capabilities": capabilities}

    @staticmethod
    def _validate_enabled(profiles: Sequence[ProviderProfile]) -> None:
        enabled = [profile for profile in profiles if profile.enabled]
        if len(enabled) > 1:
            raise ProviderProfileError("at most one provider profile may be enabled")
        if enabled and enabled[0].capability_status != "verified":
            raise ProviderProfileError("enabled profile must be verified")

    def load(self) -> dict[str, ProviderProfile]:
        with self._lock:
            document = self._read_document()
            return {profile.profile_id: profile for profile in document["profiles"]}

    load_profiles = load

    @staticmethod
    def _config_fingerprint(profile: ProviderProfile) -> str:
        value = profile.to_dict()
        for key in ("enabled", "capability_status"):
            value.pop(key)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    def _write_document(self, profiles: Sequence[ProviderProfile], capabilities: Mapping[str, Mapping[str, Any]]) -> None:
        self._validate_enabled(profiles)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "profiles": [profile.to_dict() for profile in profiles],
            "capabilities": {key: dict(value) for key, value in sorted(capabilities.items())},
        }
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def save(self, profiles: ProviderProfile | Mapping[str, ProviderProfile] | Iterable[ProviderProfile]) -> None:
        with self._lock:
            if isinstance(profiles, ProviderProfile):
                current = self._read_document()
                values = {profile.profile_id: profile for profile in current["profiles"]}
                values[profiles.profile_id] = profiles
                values = list(values.values())
            elif isinstance(profiles, Mapping):
                values = list(profiles.values())
            else:
                values = list(profiles)
            if any(not isinstance(profile, ProviderProfile) for profile in values):
                raise ProviderProfileError("profiles must be ProviderProfile objects")
            current = self._read_document()
            old = {profile.profile_id: profile for profile in current["profiles"]}
            normalized: list[ProviderProfile] = []
            capabilities = dict(current["capabilities"])
            for profile in sorted(values, key=lambda item: item.profile_id):
                previous = old.get(profile.profile_id)
                concurrency_only = previous is not None and profile_differs_only_in_offline_concurrency(previous, profile)
                if previous is not None and concurrency_only:
                    profile = profile.with_capability(previous.capability_status, enabled=previous.enabled)
                elif previous is not None and self._config_fingerprint(previous) != self._config_fingerprint(profile):
                    profile = profile.with_capability("unverified", enabled=False)
                    capabilities.pop(profile.profile_id, None)
                normalized.append(profile)
            self._write_document(normalized, capabilities)

    def upsert(self, profile: ProviderProfile) -> None:
        self.save(profile)

    def save_capabilities(self, profile_id: str, capabilities: Mapping[str, Any]) -> None:
        with self._lock:
            document = self._read_document()
            profiles = {profile.profile_id: profile for profile in document["profiles"]}
            if profile_id not in profiles:
                raise ProviderProfileError("unknown profile_id")
            if set(capabilities) - _CAPABILITY_FIELDS:
                raise ProviderProfileError("unknown capability field")
            saved = dict(document["capabilities"])
            saved[profile_id] = dict(capabilities)
            self._write_document(list(profiles.values()), saved)

    def record_probe(self, result: "ProbeResult | Mapping[str, Any]") -> ProviderProfile:
        """Atomically persist probe status, profile disablement, and capabilities."""

        if isinstance(result, ProbeResult):
            profile_id = result.profile_id
            capability_status = result.capability_status
            capabilities = {
                **result.capabilities,
                "capability_status": result.capability_status,
                "error_code": result.error_code,
                "base_url_stable_id": result.base_url_stable_id,
                "probed_at": result.probed_at,
                "request_id_redacted": result.request_id_redacted,
            }
        elif isinstance(result, Mapping):
            profile_id = result.get("profile_id")
            capability_status = result.get("capability_status")
            capabilities = dict(result)
        else:
            raise ProviderProfileError("invalid probe result")
        if not isinstance(profile_id, str) or capability_status not in {"verified", "failed", "unverified", "draft"}:
            raise ProviderProfileError("invalid probe result")
        capabilities.pop("profile_id", None)
        capabilities.pop("store_false_supported", None)
        if set(capabilities) - _CAPABILITY_FIELDS:
            raise ProviderProfileError("invalid probe capabilities")
        with self._lock:
            document = self._read_document()
            profiles = {profile.profile_id: profile for profile in document["profiles"]}
            profile = profiles.get(profile_id)
            if profile is None:
                raise ProviderProfileError("unknown profile_id")
            capabilities.setdefault("adapter", profile.adapter)
            if capabilities.get("adapter") != profile.adapter:
                raise ProviderProfileError("probe adapter does not match profile")
            updated = profile.with_capability(capability_status, enabled=False)
            profiles[profile_id] = updated
            self._write_document(list(profiles.values()), {**document["capabilities"], profile_id: capabilities})
            return updated

    def enable(self, profile_id: str) -> ProviderProfile:
        with self._lock:
            document = self._read_document()
            profiles = {profile.profile_id: profile for profile in document["profiles"]}
            profile = profiles.get(profile_id)
            if profile is None:
                raise ProviderProfileError("unknown profile_id")
            caps = document["capabilities"].get(profile_id, {})
            required = {"image_input_supported", "structured_outputs_supported", "error_classification_supported"}
            if profile.capability_status != "verified" or caps.get("capability_status") != "verified" or caps.get("adapter") != profile.adapter or any(caps.get(key) is not True for key in required):
                raise ProviderError("PROVIDER_CAPABILITY_MISSING")
            if any(item.enabled and item.profile_id != profile_id for item in profiles.values()):
                raise ProviderProfileError("at most one provider profile may be enabled")
            updated = profile.with_capability("verified", enabled=True)
            profiles[profile_id] = updated
            self._write_document(list(profiles.values()), document["capabilities"])
            return updated

    def disable(self, profile_id: str) -> ProviderProfile:
        with self._lock:
            document = self._read_document()
            profiles = {profile.profile_id: profile for profile in document["profiles"]}
            profile = profiles.get(profile_id)
            if profile is None:
                raise ProviderProfileError("unknown profile_id")
            updated = profile.with_capability(profile.capability_status, enabled=False)
            profiles[profile_id] = updated
            self._write_document(list(profiles.values()), document["capabilities"])
            return updated

    save_profile = upsert

    def capabilities(self, profile_id: str) -> dict[str, Any] | None:
        with self._lock:
            document = self._read_document()
            value = document["capabilities"].get(profile_id)
            return dict(value) if value is not None else None

    def authoritative(self, profile_id: str) -> tuple[ProviderProfile | None, dict[str, Any]]:
        with self._lock:
            document = self._read_document()
            profiles = {profile.profile_id: profile for profile in document["profiles"]}
            return profiles.get(profile_id), dict(document["capabilities"].get(profile_id, {}))


@dataclass(frozen=True, slots=True)
class SecretInfo:
    configured: bool
    source: str | None
    masked: str | None


class SecretResolver:
    """Resolve a key without making the key part of a returned object."""

    def __init__(self, *, keyring_backend: Any | None = None, environ: Mapping[str, str] | None = None) -> None:
        self._keyring_backend = keyring_backend
        self._environ = os.environ if environ is None else environ

    def _keyring_value(self, profile: ProviderProfile) -> str | None:
        backend = self._keyring_backend
        if backend is None:
            try:
                import keyring as backend  # type: ignore[no-redef]
            except Exception:
                return None
        try:
            value = backend.get_password("blockpedia", profile.profile_id)
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    def _value(self, profile: ProviderProfile) -> tuple[str | None, str | None]:
        keyring_value = self._keyring_value(profile)
        if keyring_value:
            return keyring_value, "keyring"
        env_value = self._environ.get("OPENAI_API_KEY")
        if isinstance(env_value, str) and env_value:
            return env_value, "env"
        return None, None

    @staticmethod
    def _mask(value: str | None) -> str | None:
        if not value:
            return None
        return "********"

    def resolve(self, profile: ProviderProfile) -> SecretInfo:
        value, source = self._value(profile)
        return SecretInfo(configured=value is not None, source=source, masked=self._mask(value))

    describe = resolve

    def _secret_for_request(self, profile: ProviderProfile) -> str | None:
        return self._value(profile)[0]


@dataclass(frozen=True, slots=True)
class ProviderResult:
    status: str
    stage: str
    wire_schema_id: str
    parsed_artifact: dict[str, Any] | None
    request_id_redacted: str | None
    attempts_used: int
    error_code: str | None
    error_class: str | None
    cache_key: str | None
    artifact_hash: str | None
    warnings: tuple[str, ...] = ()
    validation_diagnostic: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        diagnostic = sanitize_validation_diagnostic(self.validation_diagnostic)
        object.__setattr__(self, "validation_diagnostic", diagnostic)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "stage": self.stage,
            "wire_schema_id": self.wire_schema_id,
            "parsed_artifact": self.parsed_artifact,
            "request_id_redacted": self.request_id_redacted,
            "attempts_used": self.attempts_used,
            "error_code": self.error_code,
            "error_class": self.error_class,
            "cache_key": self.cache_key,
            "artifact_hash": self.artifact_hash,
            "warnings": list(self.warnings),
            "validation_diagnostic": self.validation_diagnostic,
        }


@dataclass(frozen=True, slots=True)
class ProbeResult:
    profile_id: str
    adapter: str
    capability_status: str
    image_input_supported: bool
    structured_outputs_supported: bool
    error_classification_supported: bool
    base_url_stable_id: str
    request_id_redacted: str | None
    probed_at: str
    error_code: str | None = None

    @property
    def capabilities(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "image_input_supported": self.image_input_supported,
            "structured_outputs_supported": self.structured_outputs_supported,
            "error_classification_supported": self.error_classification_supported,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "adapter": self.adapter,
            "capability_status": self.capability_status,
            "image_input_supported": self.image_input_supported,
            "structured_outputs_supported": self.structured_outputs_supported,
            "error_classification_supported": self.error_classification_supported,
            "base_url_stable_id": self.base_url_stable_id,
            "request_id_redacted": self.request_id_redacted,
            "probed_at": self.probed_at,
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class _Attempt:
    artifact: dict[str, Any] | None = None
    request_id_redacted: str | None = None
    error_code: str | None = None
    error_class: str | None = None
    retryable: bool = False
    repairable: bool = False
    storage_unsupported: bool = False
    repair_context: str | None = None
    validation_diagnostic: Mapping[str, Any] | None = None


_VALIDATION_DIAGNOSTIC_FIELDS = frozenset(
    {"stage", "phase", "path", "keyword", "observed_type", "observed_length"}
)
_VALIDATION_DIAGNOSTIC_PHASES = frozenset({"json_parse", "output_shape", "wire_schema"})
_VALIDATION_DIAGNOSTIC_TYPES = frozenset({"missing", "null", "boolean", "number", "string", "array", "object"})
_VALIDATION_DIAGNOSTIC_PATH = re.compile(r"^\$(?:\.[A-Za-z_][A-Za-z0-9_:-]{0,63}|\[[0-9]{1,6}\]){0,32}$")
_VALIDATION_DIAGNOSTIC_KEYWORD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_MAX_VALIDATION_DIAGNOSTIC_LENGTH = 4096


def sanitize_validation_diagnostic(value: Mapping[str, Any] | Any) -> dict[str, Any] | None:
    """Accept exactly the safe final-diagnostic contract, or drop it."""

    if not isinstance(value, Mapping) or set(value) != _VALIDATION_DIAGNOSTIC_FIELDS:
        return None
    if value.get("stage") != "offline_annotation" or value.get("phase") not in _VALIDATION_DIAGNOSTIC_PHASES:
        return None
    path = value.get("path")
    keyword = value.get("keyword")
    observed_type = value.get("observed_type")
    observed_length = value.get("observed_length")
    if not isinstance(path, str) or _VALIDATION_DIAGNOSTIC_PATH.fullmatch(path) is None:
        return None
    if not isinstance(keyword, str) or _VALIDATION_DIAGNOSTIC_KEYWORD.fullmatch(keyword) is None:
        return None
    if observed_type not in _VALIDATION_DIAGNOSTIC_TYPES:
        return None
    if observed_length is not None and (
        not isinstance(observed_length, int)
        or isinstance(observed_length, bool)
        or not 0 <= observed_length <= _MAX_VALIDATION_DIAGNOSTIC_LENGTH
    ):
        return None
    return {
        "stage": "offline_annotation",
        "phase": value["phase"],
        "path": path,
        "keyword": keyword,
        "observed_type": observed_type,
        "observed_length": observed_length,
    }


def _observed_type(value: Any, *, missing: bool = False) -> str | None:
    if missing:
        return "missing"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, Mapping):
        return "object"
    return None


def _observed_length(value: Any, *, missing: bool = False) -> int | None:
    if missing or not isinstance(value, (str, list, tuple, Mapping)):
        return None
    return min(len(value), _MAX_VALIDATION_DIAGNOSTIC_LENGTH)


def _validation_diagnostic(
    phase: str,
    keyword: str,
    value: Any = None,
    *,
    missing: bool = False,
) -> dict[str, Any] | None:
    return sanitize_validation_diagnostic(
        {
            "stage": "offline_annotation",
            "phase": phase,
            "path": "$",
            "keyword": keyword,
            "observed_type": _observed_type(value, missing=missing),
            "observed_length": _observed_length(value, missing=missing),
        }
    )


def _redact_request_id(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    clean = re.sub(r"[^A-Za-z0-9_-]", "", value)
    if not clean:
        return None
    return "req_…" + hashlib.sha256(clean.encode()).hexdigest()[:12]


def _hash_json(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _stable_error_class(code: str, requested: str | None = None) -> str:
    if requested in _ERROR_CLASSES:
        return requested
    return _ERROR_CLASS_BY_CODE.get(code, "unknown")


def _wire_projection(value: Any, *, adapter: str | None = None, stage: str | None = None) -> Any:
    if isinstance(value, dict):
        if "const" in value and "enum" in value:
            raise ProviderError("wire schema cannot combine const and enum")
        drop_string_enums = adapter == "openai_chat_completions" and stage == "query_spec"
        if "enum" in value:
            enum_values = value["enum"]
            if not isinstance(enum_values, list) or not enum_values:
                raise ProviderError("wire schema enum must be a non-empty list")
            if any(not isinstance(enum_value, str) for enum_value in enum_values):
                raise ProviderError("wire schema enum values must be strings")
            existing_type = value.get("type")
            if existing_type is not None and existing_type != "string":
                raise ProviderError("wire string enum conflicts with type")
        if "const" in value and isinstance(value["const"], bool):
            existing_type = value.get("type")
            if existing_type is not None and existing_type != "boolean":
                raise ProviderError("wire boolean const conflicts with type")
        elif "const" in value and not isinstance(value["const"], str):
            raise ProviderError("wire schema has unsupported const type")
        result: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"$schema", "$id", "$comment", "$anchor", "$defs", "$dynamicAnchor", "$dynamicRef", "uniqueItems"}:
                continue
            if key == "const":
                if isinstance(item, bool):
                    result["type"] = "boolean"
                elif drop_string_enums:
                    result["type"] = "string"
                else:
                    result["enum"] = [item]
                continue
            if key == "enum" and drop_string_enums:
                result["type"] = "string"
                continue
            result[key] = _wire_projection(item, adapter=adapter, stage=stage)
        return result
    if isinstance(value, list):
        return [_wire_projection(item, adapter=adapter, stage=stage) for item in value]
    return value


def build_cache_key(
    parts: Mapping[str, Any] | None = None,
    *,
    image_hash: str | None = None,
    machine_metadata_hash: str | None = None,
    prompt_version: str | None = None,
    model_id: str | None = None,
    schema_version: str | None = None,
    base_url_stable_id: str | None = None,
    stage: str | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    required = dict(parts or {})
    # Existing internal Responses callers pass keyword components rather than
    # a parts mapping; keep that compatibility while requiring adapter in
    # explicit canonical material.
    if parts is None:
        required.setdefault("adapter", "openai_responses")
    explicit = {
        "image_hash": image_hash,
        "machine_metadata_hash": machine_metadata_hash,
        "prompt_version": prompt_version,
        "model_id": model_id,
        "schema_version": schema_version,
        "base_url_stable_id": base_url_stable_id,
        "stage": stage,
    }
    required.update({key: value for key, value in explicit.items() if value is not None})
    missing = [key for key in ("image_hash", "machine_metadata_hash", "adapter", "prompt_version", "model_id", "schema_version", "base_url_stable_id", "stage") if key not in required]
    if missing:
        raise ProviderError("cache key missing: " + ", ".join(missing))
    if required["stage"] not in STAGES:
        raise ProviderError("invalid cache stage")
    if required["adapter"] not in {"openai_responses", "openai_chat_completions"}:
        raise ProviderError("invalid adapter")
    for field_name in ("image_hash", "machine_metadata_hash"):
        if not isinstance(required[field_name], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", required[field_name]):
            raise ProviderError(f"invalid {field_name}")
    if context:
        for key, value in context.items():
            if key not in required:
                required[key] = value
    return _hash_json(required)


cache_key = build_cache_key
canonical_cache_key = build_cache_key


def _stage_schema_name(stage: str) -> tuple[str, str]:
    try:
        schema_id = STAGE_SCHEMAS[stage]
    except KeyError as exc:
        raise ProviderError("invalid provider stage") from exc
    return schema_id, WIRE_NAMES[schema_id]


def build_provider_batch_envelope(
    profile: ProviderProfile,
    *,
    request_id: str,
    stage: str,
    input_summary: Mapping[str, Any],
    export_id: str | None = None,
    release_id: str | None = None,
    resolved_release_manifest_sha256: str | None = None,
    minecraft_version: str = "26.2",
    created_at: str | None = None,
) -> dict[str, Any]:
    schema_id, format_name = _stage_schema_name(stage)
    envelope = {
        "schema_version": "provider-batch-envelope.v1",
        "adapter": profile.adapter,
        "request_id": request_id,
        "stage": stage,
        "profile_id": profile.profile_id,
        "model_id": profile.model_id,
        "base_url_stable_id": profile.base_url_stable_id,
        "secret_reference": profile.secret_reference,
        "prompt_version": profile.prompt_version,
        "wire_schema_id": schema_id,
        "wire_format_name": format_name,
        "minecraft_version": minecraft_version,
        "export_id": export_id,
        "release_id": release_id,
        "resolved_release_manifest_sha256": resolved_release_manifest_sha256,
        "search_ranking_version": profile.search_ranking_version,
        "created_at": created_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "input_summary": dict(input_summary),
    }
    if profile.adapter == "openai_responses":
        envelope["store"] = False
    validate_record("provider-batch-envelope.v1", envelope)
    return envelope


build_batch_envelope = build_provider_batch_envelope


class OpenAIProvider:
    """Protocol-neutral OpenAI provider with one explicit profile adapter."""

    def __init__(
        self,
        profile: ProviderProfile,
        *,
        secret_resolver: SecretResolver | None = None,
        client: httpx.Client | None = None,
        repo_root: Path | None = None,
        profile_store: ProviderProfileStore | None = None,
    ) -> None:
        self.profile = profile
        self.secret_resolver = secret_resolver or SecretResolver()
        self._client_owned = client is None
        self.client = client or httpx.Client(
            follow_redirects=False,
            trust_env=False,
            timeout=profile.request_timeout_ms / 1000,
            transport=httpx.HTTPTransport(retries=0),
        )
        self.repo_root = repo_root
        self.profile_store = profile_store

    def close(self) -> None:
        if self._client_owned:
            self.client.close()

    def __enter__(self) -> "OpenAIProvider":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.close()

    def _responses_body(
        self,
        stage: str,
        input_text: str,
        image_png: bytes | None,
        *,
        repair: bool = False,
        repair_context: str | None = None,
    ) -> dict[str, Any]:
        schema_id, format_name = _stage_schema_name(stage)
        if not isinstance(input_text, str):
            input_text = json.dumps(input_text, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if repair:
            bounded = (repair_context or "")[:2000]
            input_text += (
                "\n<untrusted_previous_output>\n"
                + bounded
                + "\n</untrusted_previous_output>\n"
                "Repair only this untrusted output and return one strict JSON object matching the supplied schema.\n"
                "Repair to the supplied schema and make the top-level `schema_id` exactly equal "
                "to the selected `schema_id`: `"
                + schema_id
                + "`."
            )
        content: list[dict[str, Any]] = [{"type": "input_text", "text": input_text}]
        if image_png is not None:
            if not isinstance(image_png, (bytes, bytearray)) or not image_png:
                raise ProviderError("image_png must be non-empty bytes")
            encoded = base64.b64encode(bytes(image_png)).decode("ascii")
            content.append({"type": "input_image", "image_url": f"data:image/png;base64,{encoded}"})
        return {
            "model": self.profile.model_id,
            "store": False,
            "input": [{"role": "user", "content": content}],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": format_name,
                    "strict": True,
                    "schema": _wire_projection(
                        load_provider_wire_schema(schema_id, repo_root=self.repo_root),
                        adapter=self.profile.adapter,
                        stage=stage,
                    ),
                }
            },
        }

    def _chat_body(
        self,
        stage: str,
        input_text: str,
        image_png: bytes | None,
        *,
        repair: bool = False,
        repair_context: str | None = None,
    ) -> dict[str, Any]:
        schema_id, format_name = _stage_schema_name(stage)
        if not isinstance(input_text, str):
            input_text = json.dumps(input_text, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if repair:
            bounded = (repair_context or "")[:2000]
            input_text += (
                "\n<untrusted_previous_output>\n"
                + bounded
                + "\n</untrusted_previous_output>\n"
                "Repair only this untrusted output and return one strict JSON object matching the supplied schema.\n"
                "Repair to the supplied schema and make the top-level `schema_id` exactly equal "
                "to the selected `schema_id`: `"
                + schema_id
                + "`."
            )
        content: list[dict[str, Any]] = [{"type": "text", "text": input_text}]
        if image_png is not None:
            if not isinstance(image_png, (bytes, bytearray)) or not image_png:
                raise ProviderError("image_png must be non-empty bytes")
            encoded = base64.b64encode(bytes(image_png)).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}})
        return {
            "model": self.profile.model_id,
            "messages": [{"role": "user", "content": content}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": format_name,
                    "strict": True,
                    "schema": _wire_projection(
                        load_provider_wire_schema(schema_id, repo_root=self.repo_root),
                        adapter=self.profile.adapter,
                        stage=stage,
                    ),
                },
            },
        }

    def _body(
        self,
        stage: str,
        input_text: str,
        image_png: bytes | None,
        *,
        repair: bool = False,
        repair_context: str | None = None,
    ) -> dict[str, Any]:
        if self.profile.adapter == "openai_responses":
            return self._responses_body(stage, input_text, image_png, repair=repair, repair_context=repair_context)
        return self._chat_body(stage, input_text, image_png, repair=repair, repair_context=repair_context)

    @staticmethod
    def _error(
        code: str,
        *,
        error_class: str | None = None,
        retryable: bool = False,
        repairable: bool = False,
        request_id: str | None = None,
        storage_unsupported: bool = False,
        repair_context: str | None = None,
        validation_diagnostic: Mapping[str, Any] | None = None,
    ) -> _Attempt:
        if code not in ERROR_CODES:
            code = "PROVIDER_UNKNOWN"
        return _Attempt(
            request_id_redacted=request_id,
            error_code=code,
            error_class=_stable_error_class(code, error_class),
            retryable=retryable,
            repairable=repairable,
            storage_unsupported=storage_unsupported,
            repair_context=repair_context[:2000] if repair_context else None,
            validation_diagnostic=sanitize_validation_diagnostic(validation_diagnostic),
        )

    @staticmethod
    def _refusal(payload: Mapping[str, Any]) -> bool:
        if payload.get("refusal"):
            return True
        output = payload.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, Mapping):
                    continue
                if item.get("type") == "refusal" or item.get("refusal"):
                    return True
                content = item.get("content")
                if isinstance(content, list) and any(
                    isinstance(part, Mapping) and (part.get("type") == "refusal" or part.get("refusal"))
                    for part in content
                ):
                    return True
        return False

    @staticmethod
    def _bounded(value: Any) -> str:
        if isinstance(value, str):
            return value[:2000]
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))[:2000]
        except (TypeError, ValueError):
            return str(value)[:2000]

    @classmethod
    def _assistant_output_text(cls, payload: Mapping[str, Any]) -> tuple[str | None, bool, str, bool, Mapping[str, Any] | None]:
        output = payload.get("output")
        if not isinstance(output, list):
            if "output" not in payload:
                diagnostic = _validation_diagnostic("output_shape", "output_shape", missing=True)
            else:
                diagnostic = _validation_diagnostic("output_shape", "output_shape", output)
            return None, False, cls._bounded(payload.get("output_text", payload)), False, diagnostic
        texts: list[str] = []
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message" or item.get("role") != "assistant":
                continue
            if item.get("status") != "completed":
                return None, False, cls._bounded(item), item.get("status") in {"incomplete", "in_progress", "failed", "cancelled"}, None
            content = item.get("content")
            if not isinstance(content, list):
                diagnostic = _validation_diagnostic("output_shape", "output_shape", content, missing="content" not in item)
                return None, False, cls._bounded(item), False, diagnostic
            for part in content:
                if not isinstance(part, Mapping):
                    return None, False, cls._bounded(part), False, _validation_diagnostic("output_shape", "output_shape", part)
                if part.get("type") == "refusal" or part.get("refusal"):
                    return None, True, cls._bounded(part.get("refusal", part)), False, None
                if part.get("type") == "output_text":
                    text = part.get("text")
                    if not isinstance(text, str):
                        diagnostic = _validation_diagnostic("output_shape", "output_shape", text, missing="text" not in part)
                        return None, False, cls._bounded(part), False, diagnostic
                    texts.append(text)
        if len(texts) != 1:
            return None, False, cls._bounded(output), False, _validation_diagnostic("output_shape", "output_shape", texts)
        return texts[0], False, texts[0][:2000], False, None

    @classmethod
    def _chat_output_text(cls, payload: Mapping[str, Any]) -> tuple[str | None, bool, str, Mapping[str, Any] | None]:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
            diagnostic = _validation_diagnostic("output_shape", "output_shape", choices, missing="choices" not in payload)
            return None, False, cls._bounded(payload.get("choices", payload)), diagnostic
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, Mapping):
            diagnostic = _validation_diagnostic("output_shape", "output_shape", message, missing="message" not in choice)
            return None, False, cls._bounded(choice), diagnostic
        if message.get("refusal") is not None:
            return None, True, cls._bounded(message.get("refusal")), None
        if choice.get("finish_reason") != "stop":
            return None, False, cls._bounded(choice), None
        content = message.get("content")
        if not isinstance(content, str):
            diagnostic = _validation_diagnostic("output_shape", "output_shape", content, missing="content" not in message)
            return None, False, cls._bounded(message), diagnostic
        return content, False, content[:2000], None

    @staticmethod
    def _is_allowlisted_invalid_request_response(response: httpx.Response) -> bool:
        if not 500 <= response.status_code <= 599:
            return False
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return False
        if not isinstance(payload, Mapping) or not isinstance(payload.get("error"), Mapping):
            return False
        error = payload["error"]
        return any(
            isinstance(error.get(field), str) and error[field].lower() in _INVALID_REQUEST_MARKERS
            for field in ("code", "type")
        )

    def _post(
        self,
        stage: str,
        input_text: str,
        image_png: bytes | None,
        *,
        repair: bool,
        repair_context: str | None = None,
        body_override: Mapping[str, Any] | None = None,
    ) -> _Attempt:
        secret = self.secret_resolver._secret_for_request(self.profile)
        if not secret:
            return self._error("PROVIDER_NOT_CONFIGURED", error_class="not_configured")
        body = dict(body_override) if body_override is not None else self._body(stage, input_text, image_png, repair=repair, repair_context=repair_context)
        request_id: str | None = None
        try:
            endpoint = "/responses" if self.profile.adapter == "openai_responses" else "/chat/completions"
            response = self.client.post(
                self.profile.base_url.rstrip("/") + endpoint,
                headers={"Authorization": f"Bearer {secret}", "Content-Type": "application/json"},
                json=body,
                follow_redirects=False,
            )
            request_id = _redact_request_id(response.headers.get("x-request-id"))
        except httpx.TimeoutException:
            return self._error("PROVIDER_TIMEOUT", error_class="timeout", retryable=True, request_id=request_id)
        except httpx.RequestError:
            return self._error("PROVIDER_NETWORK_ERROR", error_class="network", retryable=True, request_id=request_id)
        except Exception:
            return self._error("PROVIDER_UNKNOWN", error_class="transport", request_id=request_id)

        status = response.status_code
        if status == 401:
            return self._error("PROVIDER_AUTH_FAILED", request_id=request_id)
        if status == 403:
            return self._error("PROVIDER_PERMISSION_DENIED", request_id=request_id)
        if status == 404:
            return self._error("PROVIDER_MODEL_UNAVAILABLE", request_id=request_id)
        if status == 429:
            return self._error("PROVIDER_RATE_LIMITED", retryable=True, request_id=request_id)
        if status == 413:
            return self._error("PROVIDER_PAYLOAD_TOO_LARGE", request_id=request_id)
        if 500 <= status <= 599:
            if self._is_allowlisted_invalid_request_response(response):
                return self._error("PROVIDER_REQUEST_INVALID", request_id=request_id)
            return self._error("PROVIDER_SERVER_ERROR", retryable=True, request_id=request_id)
        if status < 200 or status >= 300:
            return self._error("PROVIDER_REQUEST_INVALID", request_id=request_id)
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError):
            return self._error(
                "PROVIDER_SCHEMA_INVALID_REPAIRABLE",
                repairable=True,
                request_id=request_id,
                repair_context=response.text,
            )
        if not isinstance(payload, Mapping):
            return self._error(
                "PROVIDER_SCHEMA_INVALID_REPAIRABLE",
                repairable=True,
                request_id=request_id,
                repair_context=response.text,
                validation_diagnostic=_validation_diagnostic("output_shape", "output_shape", payload) if stage == "offline_annotation" else None,
            )
        if not isinstance(payload.get("model"), str):
            return self._error("PROVIDER_MODEL_UNAVAILABLE", request_id=request_id)
        if self.profile.adapter == "openai_responses":
            output_text, refusal, response_context, nested_incomplete, shape_diagnostic = self._assistant_output_text(payload)
        else:
            output_text, refusal, response_context, shape_diagnostic = self._chat_output_text(payload)
            nested_incomplete = output_text is None and not refusal
        if refusal:
            return self._error("PROVIDER_REFUSAL", error_class="refusal", request_id=request_id)
        if nested_incomplete:
            return self._error(
                "PROVIDER_INCOMPLETE",
                error_class="incomplete",
                request_id=request_id,
                validation_diagnostic=shape_diagnostic if stage == "offline_annotation" else None,
            )
        if self.profile.adapter == "openai_responses":
            if payload.get("status") != "completed":
                return self._error("PROVIDER_INCOMPLETE", error_class="incomplete", request_id=request_id)
            if payload.get("incomplete_details") is not None:
                return self._error("PROVIDER_INCOMPLETE", error_class="incomplete", request_id=request_id)
        if payload.get("error") is not None:
            return self._error("PROVIDER_REQUEST_INVALID", request_id=request_id)
        if not isinstance(output_text, str):
            return self._error(
                "PROVIDER_SCHEMA_INVALID_REPAIRABLE",
                repairable=True,
                request_id=request_id,
                repair_context=response_context,
                validation_diagnostic=shape_diagnostic if stage == "offline_annotation" else None,
            )
        try:
            artifact = json.loads(output_text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return self._error(
                "PROVIDER_SCHEMA_INVALID_REPAIRABLE",
                repairable=True,
                request_id=request_id,
                repair_context=output_text,
                validation_diagnostic=_validation_diagnostic("json_parse", "json_parse", output_text) if stage == "offline_annotation" else None,
            )
        if not isinstance(artifact, dict):
            return self._error(
                "PROVIDER_SCHEMA_INVALID_REPAIRABLE",
                repairable=True,
                request_id=request_id,
                repair_context=self._bounded(artifact),
                validation_diagnostic=_validation_diagnostic("output_shape", "output_shape", artifact) if stage == "offline_annotation" else None,
            )
        schema_id, _ = _stage_schema_name(stage)
        try:
            validate_record(schema_id, artifact, repo_root=self.repo_root)
        except RecordSchemaError as exc:
            validation_summary = "\n".join(
                f"- {error[:200]}" for error in exc.errors[:4]
            )
            repair_context = (
                "Local validation errors:\n"
                + validation_summary[:800]
                + "\nPrevious artifact:\n"
                + self._bounded(artifact)[:1100]
            )
            return self._error(
                "PROVIDER_SCHEMA_INVALID_REPAIRABLE",
                repairable=True,
                request_id=request_id,
                repair_context=repair_context,
                validation_diagnostic=exc.first_issue if stage == "offline_annotation" else None,
            )
        return _Attempt(artifact=artifact, request_id_redacted=request_id)

    def _effective_profile(self) -> ProviderProfile | None:
        profile = self.profile
        if self.profile_store is not None:
            try:
                profile, capabilities = self.profile_store.authoritative(self.profile.profile_id)
            except ProviderProfileError:
                return None
            required = {
                "image_input_supported",
                "structured_outputs_supported",
                "error_classification_supported",
            }
            if profile is None or profile.enabled is not True or profile.capability_status != "verified":
                return None
            if capabilities.get("capability_status") != "verified" or capabilities.get("adapter") != profile.adapter or any(capabilities.get(key) is not True for key in required):
                return None
        if profile.enabled is not True or profile.capability_status != "verified":
            return None
        self.profile = profile
        return profile

    @staticmethod
    def _image_hash(image_png: bytes | bytearray | None) -> str:
        value = b"" if image_png is None else bytes(image_png)
        return "sha256:" + hashlib.sha256(value).hexdigest()

    @staticmethod
    def _text_hash(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _metadata_hash(value: Mapping[str, Any]) -> str:
        return _hash_json(value)

    @staticmethod
    def _candidate_records(value: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None) -> tuple[list[dict[str, Any]] | None, str | None]:
        if isinstance(value, Mapping):
            entries: list[tuple[Any, Any]] = list(value.items())
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            entries = [(None, item) for item in value]
        else:
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        records: dict[str, dict[str, Any]] = {}
        required = {"candidate_id", "variant_id", "block_id", "recommended_state_id"}
        for key, raw in entries:
            if not isinstance(raw, Mapping):
                return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            record = dict(raw)
            candidate_id = record.get("candidate_id", key)
            if not isinstance(candidate_id, str) or (key is not None and candidate_id != key) or candidate_id in records:
                return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            record["candidate_id"] = candidate_id
            if not required <= set(record):
                return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            for image_key in ("image_bytes", "image_png"):
                if image_key in record:
                    image = record.pop(image_key)
                    if not isinstance(image, (bytes, bytearray)) or not image:
                        return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            try:
                json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            except (TypeError, ValueError):
                return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            records[candidate_id] = record
        return [records[key] for key in sorted(records)], None

    def _validate_envelope_inputs(
        self,
        profile: ProviderProfile,
        stage: str,
        envelope: Mapping[str, Any] | None,
        *,
        input_text: str,
        query_text: str | None,
        query_spec: Mapping[str, Any] | None,
        machine_metadata: Mapping[str, Any] | None,
        candidate_records: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
        source_images: Mapping[str, bytes] | None,
    ) -> tuple[str | None, str | None]:
        """Validate the already-created audit envelope and its real inputs."""

        if not isinstance(envelope, Mapping):
            return None, "PROVIDER_SCHEMA_INVALID"
        try:
            validate_record("provider-batch-envelope.v1", dict(envelope), repo_root=self.repo_root)
        except RecordSchemaError:
            return None, "PROVIDER_SCHEMA_INVALID"
        schema_id, format_name = _stage_schema_name(stage)
        trusted_fields = {
            "schema_version": "provider-batch-envelope.v1",
            "adapter": profile.adapter,
            "stage": stage,
            "profile_id": profile.profile_id,
            "model_id": profile.model_id,
            "base_url_stable_id": profile.base_url_stable_id,
            "secret_reference": profile.secret_reference,
            "prompt_version": profile.prompt_version,
            "wire_schema_id": schema_id,
            "wire_format_name": format_name,
            "minecraft_version": "26.2",
            "search_ranking_version": profile.search_ranking_version,
        }
        if profile.adapter == "openai_responses":
            trusted_fields["store"] = False
        elif "store" in envelope:
            return None, "PROVIDER_CONFIG_INVALID"
        if any(envelope.get(key) != value for key, value in trusted_fields.items()):
            return None, "PROVIDER_CONFIG_INVALID"
        if stage in {"offline_annotation", "visual_rerank"} and not isinstance(source_images, Mapping):
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        if source_images is not None and (
            not isinstance(source_images, Mapping)
            or any(not isinstance(value, (bytes, bytearray)) or not value for value in source_images.values())
        ):
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"

        summary = envelope["input_summary"]
        if stage == "query_spec":
            actual_query = query_text if query_text is not None else input_text
            if not isinstance(actual_query, str) or summary.get("query_sha256") != self._text_hash(actual_query):
                return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            if query_spec is not None:
                try:
                    validate_record("query-spec-output.v1", dict(query_spec), repo_root=self.repo_root)
                except RecordSchemaError:
                    return None, "PROVIDER_SCHEMA_INVALID"
            return self._text_hash(actual_query), None

        if stage == "offline_annotation" and not isinstance(machine_metadata, Mapping):
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        if stage == "offline_annotation":
            assert isinstance(machine_metadata, Mapping)
            metadata_by_variant: dict[str, Any] = {}
            for key, value in machine_metadata.items():
                variant_id = value.get("variant_id", key) if isinstance(value, Mapping) else key
                if not isinstance(variant_id, str) or variant_id in metadata_by_variant:
                    return None, "PROVIDER_MACHINE_FACT_CONFLICT"
                metadata_by_variant[variant_id] = value
            rows = summary.get("tile_variant_map", [])
            if {row["variant_id"] for row in rows} != set(metadata_by_variant):
                return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            for row in rows:
                variant_id = row["variant_id"]
                if row["machine_metadata_sha256"] != self._metadata_hash(metadata_by_variant[variant_id]):
                    return None, "PROVIDER_MACHINE_FACT_CONFLICT"
                if source_images is not None:
                    image = source_images.get(row["tile_id"], source_images.get(variant_id))
                    if image is None or self._image_hash(image) != row["image_sha256"]:
                        return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            return self._metadata_hash(machine_metadata), None

        normalized_candidates, candidate_error = self._candidate_records(candidate_records)
        if candidate_error or normalized_candidates is None:
            return None, candidate_error or "PROVIDER_MACHINE_FACT_CONFLICT"
        if query_spec is None:
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        try:
            validate_record("query-spec-output.v1", dict(query_spec), repo_root=self.repo_root)
        except RecordSchemaError:
            return None, "PROVIDER_SCHEMA_INVALID"
        actual_query = query_text if query_text is not None else input_text
        if not isinstance(actual_query, str):
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        if summary.get("query_sha256") != self._text_hash(actual_query):
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        if summary.get("query_spec_sha256") != _hash_json(dict(query_spec)):
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        if summary.get("candidate_set_sha256") != _hash_json(normalized_candidates):
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        candidate_ids = {record["candidate_id"] for record in normalized_candidates}
        rows = summary.get("candidate_map", [])
        if candidate_ids != {row["candidate_id"] for row in rows}:
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        actual_by_id = {record["candidate_id"]: record for record in normalized_candidates}
        for row in rows:
            actual = actual_by_id[row["candidate_id"]]
            for field in ("variant_id", "block_id", "recommended_state_id"):
                if row[field] != actual[field]:
                    return None, "PROVIDER_MACHINE_FACT_CONFLICT"
            image = source_images.get(row["candidate_id"]) if source_images is not None else None
            if image is None or self._image_hash(image) != row["image_sha256"]:
                return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        if machine_metadata is not None and not isinstance(machine_metadata, Mapping):
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        metadata_source = machine_metadata if isinstance(machine_metadata, Mapping) else {record["candidate_id"]: record for record in normalized_candidates}
        return self._metadata_hash(metadata_source), None

    def _cache_for_request(
        self,
        profile: ProviderProfile,
        stage: str,
        image_png: bytes | bytearray | None,
        *,
        machine_metadata_hash: str | None,
        actual_machine_metadata_hash: str,
        image_hash: str | None,
        context: Mapping[str, Any] | None,
        cache_parts: Mapping[str, Any] | object,
    ) -> tuple[str | None, str | None]:
        if not image_png:
            return None, "PROVIDER_REQUEST_INVALID"
        if image_png is not None and not isinstance(image_png, (bytes, bytearray)):
            return None, "PROVIDER_REQUEST_INVALID"
        if cache_parts is None or (cache_parts is not _CACHE_PARTS_MISSING and not isinstance(cache_parts, Mapping)):
            return None, "PROVIDER_REQUEST_INVALID"
        supplied: dict[str, Any] = {}
        if cache_parts is not _CACHE_PARTS_MISSING:
            assert isinstance(cache_parts, Mapping)
            supplied = dict(cache_parts)
        if context:
            supplied.update(context)
        actual_image_hash = self._image_hash(image_png)
        selected_machine_hash = machine_metadata_hash or supplied.get("machine_metadata_hash")
        if selected_machine_hash is not None and selected_machine_hash != actual_machine_metadata_hash:
            return None, "PROVIDER_MACHINE_FACT_CONFLICT"
        if image_hash is not None and image_hash != actual_image_hash:
            return None, "PROVIDER_REQUEST_INVALID"
        trusted = {
            "image_hash": actual_image_hash,
            "machine_metadata_hash": actual_machine_metadata_hash,
            "adapter": profile.adapter,
            "prompt_version": profile.prompt_version,
            "model_id": profile.model_id,
            "schema_version": STAGE_SCHEMAS[stage],
            "base_url_stable_id": profile.base_url_stable_id,
            "stage": stage,
        }
        for key, value in supplied.items():
            if key in trusted and value != trusted[key]:
                return None, "PROVIDER_REQUEST_INVALID"
            if key not in trusted:
                trusted[key] = value
        try:
            return build_cache_key(trusted), None
        except ProviderError:
            return None, "PROVIDER_REQUEST_INVALID"

    @staticmethod
    def _zero_result(stage: str, schema_id: str, code: str, *, cache_key_value: str | None = None) -> ProviderResult:
        status = "failed" if code in {"PROVIDER_CAPABILITY_MISSING", "PROVIDER_NOT_CONFIGURED", "PROVIDER_CONFIG_INVALID"} else "needs_review"
        return ProviderResult(
            status,
            stage,
            schema_id,
            None,
            None,
            0,
            code,
            _stable_error_class(code),
            cache_key_value,
            None,
            (code,),
        )

    def request(
        self,
        stage: str,
        *,
        input_text: str = "",
        image_png: bytes | None = None,
        machine_metadata_hash: str | None = None,
        image_hash: str | None = None,
        context: Mapping[str, Any] | None = None,
        envelope: Mapping[str, Any] | None = None,
        machine_metadata: Mapping[str, Any] | None = None,
        query_spec: Mapping[str, Any] | None = None,
        candidate_records: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        source_images: Mapping[str, bytes] | None = None,
        query_text: str | None = None,
        cache_parts: Mapping[str, Any] | object = _CACHE_PARTS_MISSING,
    ) -> ProviderResult:
        schema_id, _ = _stage_schema_name(stage)
        profile = self._effective_profile()
        if profile is None:
            return self._zero_result(stage, schema_id, "PROVIDER_CAPABILITY_MISSING")
        actual_machine_hash, input_error = self._validate_envelope_inputs(
            profile,
            stage,
            envelope,
            input_text=input_text,
            query_text=query_text,
            query_spec=query_spec,
            machine_metadata=machine_metadata,
            candidate_records=candidate_records,
            source_images=source_images,
        )
        if input_error or actual_machine_hash is None:
            return self._zero_result(stage, schema_id, input_error or "PROVIDER_MACHINE_FACT_CONFLICT")
        cache, cache_error = self._cache_for_request(
            profile,
            stage,
            image_png,
            machine_metadata_hash=machine_metadata_hash,
            actual_machine_metadata_hash=actual_machine_hash,
            image_hash=image_hash,
            context=context,
            cache_parts=cache_parts,
        )
        if cache_error:
            return self._zero_result(stage, schema_id, cache_error)
        secret_info = self.secret_resolver.resolve(self.profile)
        if not secret_info.configured:
            return self._zero_result(stage, schema_id, "PROVIDER_NOT_CONFIGURED", cache_key_value=cache)
        last: _Attempt | None = None
        for attempt_number in (1, 2):
            repair = bool(last and last.repairable)
            current = self._post(
                stage,
                input_text,
                image_png,
                repair=repair,
                repair_context=last.repair_context if repair and last else None,
            )
            last = current
            if current.artifact is not None:
                return ProviderResult(
                    "succeeded",
                    stage,
                    schema_id,
                    current.artifact,
                    current.request_id_redacted,
                    attempt_number,
                    None,
                    None,
                    cache,
                    _hash_json(current.artifact),
                )
            if current.storage_unsupported or (not current.retryable and not current.repairable) or attempt_number == 2:
                break
        assert last is not None
        if attempt_number == 2 and last.error_code == "PROVIDER_SCHEMA_INVALID_REPAIRABLE":
            last = replace(last, error_code="PROVIDER_SCHEMA_INVALID", error_class="validation", repairable=False)
        failed = last.error_code in {
            "PROVIDER_AUTH_FAILED",
            "PROVIDER_PERMISSION_DENIED",
            "PROVIDER_MODEL_UNAVAILABLE",
            "PROVIDER_CAPABILITY_MISSING",
            "PROVIDER_NOT_CONFIGURED",
            "PROVIDER_CANCELLED",
        }
        status = "failed" if failed else "needs_review"
        return ProviderResult(
            status,
            stage,
            schema_id,
            None,
            last.request_id_redacted,
            attempt_number,
            last.error_code,
            last.error_class,
            cache,
            None,
            (last.error_code,) if last.error_code else (),
            last.validation_diagnostic,
        )

    call = request

    def annotate(self, input_text: str, *, image_png: bytes | None = None, machine_metadata_hash: str | None = None, image_hash: str | None = None, context: Mapping[str, Any] | None = None, envelope: Mapping[str, Any] | None = None, machine_metadata: Mapping[str, Any] | None = None, query_spec: Mapping[str, Any] | None = None, candidate_records: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None, source_images: Mapping[str, bytes] | None = None, query_text: str | None = None, cache_parts: Mapping[str, Any] | object = _CACHE_PARTS_MISSING) -> ProviderResult:
        return self.request("offline_annotation", input_text=input_text, image_png=image_png, machine_metadata_hash=machine_metadata_hash, image_hash=image_hash, context=context, envelope=envelope, machine_metadata=machine_metadata, query_spec=query_spec, candidate_records=candidate_records, source_images=source_images, query_text=query_text, cache_parts=cache_parts)

    def query_spec(self, input_text: str, *, image_png: bytes | None = None, machine_metadata_hash: str | None = None, image_hash: str | None = None, context: Mapping[str, Any] | None = None, envelope: Mapping[str, Any] | None = None, machine_metadata: Mapping[str, Any] | None = None, query_spec: Mapping[str, Any] | None = None, candidate_records: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None, source_images: Mapping[str, bytes] | None = None, query_text: str | None = None, cache_parts: Mapping[str, Any] | object = _CACHE_PARTS_MISSING) -> ProviderResult:
        return self.request("query_spec", input_text=input_text, image_png=image_png, machine_metadata_hash=machine_metadata_hash, image_hash=image_hash, context=context, envelope=envelope, machine_metadata=machine_metadata, query_spec=query_spec, candidate_records=candidate_records, source_images=source_images, query_text=query_text, cache_parts=cache_parts)

    def visual_rerank(self, input_text: str, *, image_png: bytes | None = None, machine_metadata_hash: str | None = None, image_hash: str | None = None, context: Mapping[str, Any] | None = None, envelope: Mapping[str, Any] | None = None, machine_metadata: Mapping[str, Any] | None = None, query_spec: Mapping[str, Any] | None = None, candidate_records: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None, source_images: Mapping[str, bytes] | None = None, query_text: str | None = None, cache_parts: Mapping[str, Any] | object = _CACHE_PARTS_MISSING) -> ProviderResult:
        return self.request("visual_rerank", input_text=input_text, image_png=image_png, machine_metadata_hash=machine_metadata_hash, image_hash=image_hash, context=context, envelope=envelope, machine_metadata=machine_metadata, query_spec=query_spec, candidate_records=candidate_records, source_images=source_images, query_text=query_text, cache_parts=cache_parts)

    def probe(self, image_png: bytes, *, probed_at: str | None = None) -> ProbeResult:
        if not isinstance(image_png, (bytes, bytearray)) or not image_png:
            raise ProviderError("probe requires an original non-empty PNG")
        secret_info = self.secret_resolver.resolve(self.profile)
        timestamp = probed_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        if not secret_info.configured:
            result = ProbeResult(
                self.profile.profile_id,
                self.profile.adapter,
                "failed",
                False,
                False,
                False,
                self.profile.base_url_stable_id or self.profile.base_url,
                None,
                timestamp,
                "PROVIDER_NOT_CONFIGURED",
            )
            self._persist_probe(result)
            return result
        stages = (
            ("offline_annotation", '{"schema_id":"annotation-batch-output.v1","items":[{"variant_id":"minecraft:stone","synonyms_zh":[],"synonyms_en":[],"summary_zh":"石块","summary_en":"Stone block","color_terms":[],"shape_terms":[],"material_impressions":[],"building_roles":[],"style_tags":[],"avoid_for":[],"confidence":0.9,"reason":"probe"}]}'),
            ("query_spec", "Return exactly the following JSON object unchanged. Do not add terms or change any value. Output only this object:\n" + _probe_query_output()),
            ("visual_rerank", '{"schema_id":"rerank-output.v1","ranking":[{"candidate_id":"A1","fit":1,"reason":"probe"}],"needs_user_choice":false,"ambiguity_points":[],"suggested_followups":[]}'),
        )
        request_ids: list[str] = []
        first_error: str | None = None
        all_valid = True
        for stage, text in stages:
            attempt = self._post(stage, text, bytes(image_png), repair=False)
            if attempt.request_id_redacted:
                request_ids.append(attempt.request_id_redacted)
            if attempt.artifact is None:
                all_valid = False
                first_error = attempt.error_code or "PROVIDER_UNKNOWN"
                break
        error_classification_supported = False
        if all_valid:
            invalid_body = self._body("query_spec", "probe-invalid-request", bytes(image_png))
            if self.profile.adapter == "openai_responses":
                invalid_body["text"]["format"]["strict"] = "not-a-boolean"
            else:
                invalid_body["messages"] = []
            classification_attempt = self._post(
                "query_spec",
                "probe-invalid-request",
                bytes(image_png),
                repair=False,
                body_override=invalid_body,
            )
            if classification_attempt.request_id_redacted:
                request_ids.append(classification_attempt.request_id_redacted)
            error_classification_supported = (
                classification_attempt.error_code == "PROVIDER_REQUEST_INVALID"
                and classification_attempt.error_class == "non_retryable"
            )
            if not error_classification_supported:
                first_error = "PROVIDER_CAPABILITY_MISSING"
        probe_verified = all_valid and error_classification_supported
        result = ProbeResult(
            self.profile.profile_id,
            self.profile.adapter,
            "verified" if probe_verified else "failed",
            all_valid,
            all_valid,
            error_classification_supported,
            self.profile.base_url_stable_id or self.profile.base_url,
            request_ids[-1] if request_ids else None,
            timestamp,
            None if probe_verified else (first_error or "PROVIDER_CAPABILITY_MISSING"),
        )
        self._persist_probe(result)
        return result

    def _persist_probe(self, result: ProbeResult) -> None:
        if self.profile_store is not None:
            self.profile = self.profile_store.record_probe(result)
        else:
            self.profile = self.profile.with_capability(result.capability_status, enabled=False)

    def enable(self) -> ProviderProfile:
        """Enable this profile only after secret resolution and a verified probe."""

        if not self.secret_resolver.resolve(self.profile).configured:
            raise ProviderError("PROVIDER_NOT_CONFIGURED")
        if self.profile_store is not None:
            enabled = self.profile_store.enable(self.profile.profile_id)
        else:
            if self.profile.capability_status != "verified":
                raise ProviderError("PROVIDER_CAPABILITY_MISSING")
            enabled = self.profile.with_capability("verified", enabled=True)
        self.profile = enabled
        return enabled

    def disable(self) -> ProviderProfile:
        disabled = self.profile_store.disable(self.profile.profile_id) if self.profile_store is not None else self.profile.with_capability(self.profile.capability_status, enabled=False)
        self.profile = disabled
        return disabled


OpenAIResponsesProvider = OpenAIProvider


def _probe_query_output() -> str:
    empty_hard = {
        "minecraft_version": {"value": "26.2", "source": "request", "required": True},
        "release_status": {"value": "current", "source": "system", "required": True},
        "legal_state": {"value": True, "source": "system", "required": True},
        "behaviors": [],
        "support": [],
        "transparency": [],
        "emission": [],
        "orientation": [],
        "shape": [],
    }
    empty_soft = {key: [] for key in ("colors", "materials", "uses", "styles", "shape_terms", "avoid_for", "keywords")}
    return json.dumps(
        {
            "schema_id": "query-spec-output.v1",
            "source": "llm",
            "hard": empty_hard,
            "soft": empty_soft,
            "ambiguities": [],
            "needs_user_choice": False,
            "suggested_followups": [],
            "unknown_terms": [],
        },
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class AnnotationBatchValidation:
    status: str
    review_route: str
    priority: str
    annotations: tuple[dict[str, Any], ...]
    error_code: str | None = None
    warnings: tuple[str, ...] = ()
    cache_key: str | None = None
    artifact_hash: str | None = None
    validation_diagnostic: Mapping[str, Any] | None = None

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return self.annotations

    @property
    def review_priority(self) -> str:
        return self.priority

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "review_route": self.review_route,
            "priority": self.priority,
            "annotations": list(self.annotations),
            "error_code": self.error_code,
            "warnings": list(self.warnings),
            "cache_key": self.cache_key,
            "artifact_hash": self.artifact_hash,
            "validation_diagnostic": self.validation_diagnostic,
        }


def _invalid_annotation(
    error_code: str,
    *,
    warning: str | None = None,
    validation_diagnostic: Mapping[str, Any] | None = None,
) -> AnnotationBatchValidation:
    return AnnotationBatchValidation(
        status="needs_review",
        review_route="high",
        priority="high",
        annotations=(),
        error_code=error_code,
        warnings=(warning or error_code,),
        validation_diagnostic=sanitize_validation_diagnostic(validation_diagnostic),
    )


def validate_annotation_batch(
    output: Mapping[str, Any] | str,
    expected_variant_ids: Iterable[str] | Mapping[str, Any],
    profile: ProviderProfile,
    *,
    minecraft_version: str = "26.2",
    cache_key: str | None = None,
    artifact_hash: str | None = None,
) -> AnnotationBatchValidation:
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _invalid_annotation(
                "PROVIDER_SCHEMA_INVALID",
                validation_diagnostic=_validation_diagnostic("json_parse", "json_parse", output),
            )
    if not isinstance(output, Mapping):
        return _invalid_annotation(
            "PROVIDER_SCHEMA_INVALID",
            validation_diagnostic=_validation_diagnostic("output_shape", "output_shape", output),
        )
    try:
        validate_record("annotation-batch-output.v1", dict(output))
    except RecordSchemaError as exc:
        machine = any(key in _MACHINE_FACT_KEYS for item in output.get("items", []) if isinstance(item, Mapping) for key in item)
        return _invalid_annotation(
            "PROVIDER_MACHINE_FACT_CONFLICT" if machine else "PROVIDER_SCHEMA_INVALID",
            warning="PROVIDER_MACHINE_FACT_CONFLICT" if machine else "PROVIDER_SCHEMA_INVALID",
            validation_diagnostic=exc.first_issue,
        )
    expected_values = list(expected_variant_ids.keys() if isinstance(expected_variant_ids, Mapping) else expected_variant_ids)
    try:
        if len(expected_values) != len(set(expected_values)):
            return _invalid_annotation(
                "PROVIDER_OUTPUT_ID_MISMATCH",
                validation_diagnostic=_validation_diagnostic("output_shape", "expected_ids", expected_values, missing=False),
            )
    except TypeError:
        return _invalid_annotation("PROVIDER_OUTPUT_ID_MISMATCH")
    expected = set(expected_values)
    items = list(output["items"])
    actual = [item["variant_id"] for item in items]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        return _invalid_annotation(
            "PROVIDER_OUTPUT_ID_MISMATCH",
            validation_diagnostic=_validation_diagnostic("output_shape", "expected_ids", actual),
        )
    if cache_key is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", cache_key):
        return _invalid_annotation("PROVIDER_SCHEMA_INVALID")
    actual_artifact_hash = _hash_json(dict(output))
    if artifact_hash is not None and artifact_hash != actual_artifact_hash:
        return _invalid_annotation("PROVIDER_SCHEMA_INVALID")
    calculated_artifact_hash = actual_artifact_hash
    calculated_cache_key = cache_key or _hash_json(
        {
            "stage": "offline_annotation",
            "adapter": profile.adapter,
            "schema_version": "annotation-batch-output.v1",
            "minecraft_version": minecraft_version,
            "variant_ids": expected_values,
        }
    )
    annotations: list[dict[str, Any]] = []
    route = "auto_valid"
    priority = "normal"
    for item in items:
        confidence = item["confidence"]
        if confidence < 0.65:
            route, priority = "high", "high"
        elif confidence < 0.80 and route != "high":
            route, priority = "normal_review", "normal"
        annotation = dict(item)
        annotation.pop("variant_id")
        provenance = {
            "schema_version": "annotation-record.v1",
            "minecraft_version": minecraft_version,
            "variant_id": item["variant_id"],
            "profile_id": profile.profile_id,
            "model_id": profile.model_id,
            "prompt_version": profile.prompt_version,
            "cache_key": calculated_cache_key,
            "artifact_hash": calculated_artifact_hash,
        }
        annotation.update(
            {
                "schema_version": "annotation-record.v1",
                "annotation_id": "ann_" + hashlib.sha256(json.dumps(provenance, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:32],
                "subject_type": "visual_variant",
                "subject_id": item["variant_id"],
                "minecraft_version": minecraft_version,
                "source": {
                    "type": "llm",
                    "model_id": profile.model_id,
                    "prompt_version": profile.prompt_version,
                    "wire_schema_id": "annotation-batch-output.v1",
                    "verified": confidence >= 0.80,
                },
            }
        )
        try:
            validate_record("annotation-record.v1", annotation)
        except RecordSchemaError:
            return _invalid_annotation("PROVIDER_SCHEMA_INVALID")
        annotations.append(annotation)
    return AnnotationBatchValidation(
        status="succeeded" if route == "auto_valid" else "needs_review",
        review_route=route,
        priority=priority,
        annotations=tuple(annotations),
        cache_key=calculated_cache_key,
        artifact_hash=calculated_artifact_hash,
    )


validate_annotation_output = validate_annotation_batch


__all__ = [
    "AnnotationBatchValidation",
    "OpenAIProvider",
    "OpenAIResponsesProvider",
    "ProbeResult",
    "ProviderError",
    "ProviderProfile",
    "ProviderProfileError",
    "ProviderProfileStore",
    "ProviderResult",
    "SecretInfo",
    "SecretResolver",
    "StageConfig",
    "build_cache_key",
    "build_batch_envelope",
    "build_provider_batch_envelope",
    "canonical_cache_key",
    "cache_key",
    "sanitize_validation_diagnostic",
    "validate_annotation_batch",
    "validate_annotation_output",
]
