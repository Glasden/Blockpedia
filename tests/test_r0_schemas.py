import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from tools.validate_r0 import validate_repository


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_r0_schemas() -> None:
    validate_repository(REPO_ROOT)


def test_r0_dual_openai_adapter_conditionals() -> None:
    envelope_schema = json.loads(
        (REPO_ROOT / "schemas/provider/provider-batch-envelope.v1.json").read_text(
            encoding="utf-8"
        )
    )
    envelope = json.loads(
        (
            REPO_ROOT
            / "tests/schema/fixtures/provider/provider-batch-envelope.v1.valid.json"
        ).read_text(encoding="utf-8")
    )
    envelope_validator = Draft202012Validator(envelope_schema)

    assert envelope_validator.is_valid(envelope)

    chat_envelope = dict(envelope)
    chat_envelope["adapter"] = "openai_chat_completions"
    chat_envelope.pop("store")
    assert envelope_validator.is_valid(chat_envelope)

    chat_with_store = dict(chat_envelope)
    chat_with_store["store"] = False
    assert not envelope_validator.is_valid(chat_with_store)

    responses_without_store = dict(envelope)
    responses_without_store.pop("store")
    assert not envelope_validator.is_valid(responses_without_store)

    release_schema = json.loads(
        (REPO_ROOT / "schemas/workspace/release-manifest.v1.json").read_text(
            encoding="utf-8"
        )
    )
    release_manifest = json.loads(
        (
            REPO_ROOT
            / "tests/schema/fixtures/workspace/valid/release-manifest.v1.json"
        ).read_text(encoding="utf-8")
    )
    release_manifest["provider_snapshot"]["adapter"] = "openai_chat_completions"
    assert Draft202012Validator(release_schema).is_valid(release_manifest)


@pytest.mark.parametrize(
    ("schema_path", "fixture_path", "policy_container"),
    [
        (
            "schemas/exporter/export-manifest.v1.json",
            "tests/schema/fixtures/exporter/valid/export-manifest.v1.json",
            "policies",
        ),
        (
            "schemas/exporter/export-variant.v1.json",
            "tests/schema/fixtures/exporter/valid/export-variant.v1.json",
            "render",
        ),
        (
            "schemas/workspace/visual-variant-record.v1.json",
            "tests/schema/fixtures/workspace/valid/visual-variant-record.v1.json",
            "render",
        ),
    ],
)
def test_r0_render_policy_v1_v2_compatibility(
    schema_path: str, fixture_path: str, policy_container: str
) -> None:
    schema = json.loads((REPO_ROOT / schema_path).read_text(encoding="utf-8"))
    fixture = json.loads((REPO_ROOT / fixture_path).read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    assert fixture[policy_container]["render_policy_version"] == "render.v2"
    validator.validate(fixture)

    historical = copy.deepcopy(fixture)
    historical[policy_container]["render_policy_version"] = "render.v1"
    validator.validate(historical)
