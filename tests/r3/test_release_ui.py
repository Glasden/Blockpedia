from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "src" / "blockpedia" / "templates"
STATIC = ROOT / "src" / "blockpedia" / "static"


def test_r3_release_ui_templates_compile_and_render_initial_boundary() -> None:
    environment = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    for path in sorted(TEMPLATES.rglob("*.html")):
        environment.get_template(path.relative_to(TEMPLATES).as_posix())

    rendered = environment.get_template("partials/release_candidate.html").render(
        run={
            "run_id": "run_fixture",
            "minecraft_version": "26.2",
            "current_stage": "BUILD_RELEASE",
            "boundary_event": "R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING",
        },
        run_identifier="run_fixture",
    )
    assert 'data-release-candidate' in rendered
    assert 'data-state="ready"' in rendered
    assert 'data-candidate-check-route="/api/releases/check"' in rendered
    assert 'data-candidate-build-route="/api/releases/build"' in rendered
    assert 'data-candidate-build disabled' in rendered
    assert 'data-candidate-build-action hidden' in rendered
    assert "未激活" in rendered
    assert "current" in rendered
    assert all(field in rendered for field in ("check_id", "release_build_id", "snapshot_fingerprint", "quality_report_sha256"))


def test_r3_release_ui_has_only_frozen_candidate_actions_and_safe_payload() -> None:
    partial = (TEMPLATES / "partials" / "release_candidate.html").read_text(encoding="utf-8")
    run_detail = (TEMPLATES / "run_detail.html").read_text(encoding="utf-8")
    javascript = (STATIC / "studio.js").read_text(encoding="utf-8")
    rendered = Environment(loader=FileSystemLoader(str(TEMPLATES))).get_template("partials/release_candidate.html").render(
        run={
            "run_id": "run_fixture",
            "minecraft_version": "26.2",
            "current_stage": "BUILD_RELEASE",
            "boundary_event": "R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING",
        },
        run_identifier="run_fixture",
    )

    assert 'include "partials/release_candidate.html"' in run_detail
    assert 'R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING' in run_detail
    assert 'postJsonEnvelope(panel.dataset.candidateCheckRoute' in javascript
    assert 'postJsonEnvelope(panel.dataset.candidateBuildRoute' in javascript
    assert 'confirm_immutable_release: true' in javascript
    assert 'run_id: panel.dataset.candidateRunId' in javascript
    assert 'minecraft_version: panel.dataset.candidateVersion' in javascript

    assert 'data-release-activation' not in rendered
    assert 'data-activation-check-form' not in rendered
    assert all(route not in (partial + javascript).lower() for route in (
        "/api/releases/rollback",
        "/api/releases/cleanup",
        "/api/mcp",
    ))
    assert all(term not in rendered.lower() for term in ("token", "usage", "cost", "budget"))
    assert "relative_path" not in rendered
    assert "type=\"text\"" not in rendered
    assert "<select" not in rendered
    assert "<textarea" not in rendered


def test_activation_controls_render_only_at_exact_activation_boundary() -> None:
    environment = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    template = environment.get_template("partials/release_candidate.html")
    activation = template.render(
        run={
            "run_id": "run_fixture",
            "minecraft_version": "26.2",
            "current_stage": "ACTIVATE_RELEASE",
            "boundary_event": "R3_CANDIDATE_BUILT_ACTIVATION_PENDING",
        },
        run_identifier="run_fixture",
    )
    wrong_stage = template.render(
        run={
            "run_id": "run_fixture",
            "minecraft_version": "26.2",
            "current_stage": "BUILD_RELEASE",
            "boundary_event": "R3_CANDIDATE_BUILT_ACTIVATION_PENDING",
        },
        run_identifier="run_fixture",
    )
    run_detail = (TEMPLATES / "run_detail.html").read_text(encoding="utf-8")

    assert 'data-release-activation' in activation
    assert 'data-activation-check-route="/api/releases/activation-check"' in activation
    assert 'data-activation-apply-route="/api/releases/apply"' in activation
    assert 'name="target_release_id"' in activation
    assert 'pattern="rel_[0-9a-f]{32}"' in activation
    assert 'data-release-activation' not in wrong_stage
    assert "run_view.get('current_stage') == 'ACTIVATE_RELEASE'" in run_detail
    assert "/api/releases/rollback" not in activation
    assert "data-rollback" not in activation


def test_activation_ui_requires_passed_check_and_explicit_apply_decisions() -> None:
    environment = Environment(loader=FileSystemLoader(str(TEMPLATES)))
    rendered = environment.get_template("partials/release_candidate.html").render(
        run={
            "run_id": "run_fixture",
            "minecraft_version": "26.2",
            "current_stage": "ACTIVATE_RELEASE",
            "boundary_event": "R3_CANDIDATE_BUILT_ACTIVATION_PENDING",
        },
        run_identifier="run_fixture",
    )
    javascript = (STATIC / "studio.js").read_text(encoding="utf-8")

    assert 'name="activation_check_id" value="" data-activation-check-id' in rendered
    assert re.search(r'<button[^>]*type="submit"[^>]*disabled[^>]*data-activation-apply', rendered)
    assert 'name="confirm_current_switch" type="checkbox" value="true" required' in rendered
    assert 'name="set_as_default" type="radio" value="true" checked required' in rendered
    assert 'name="set_as_default" type="radio" value="false"' in rendered
    assert 'data.status === "passed" && data.can_apply === true' in javascript
    assert '!passed || !confirmation?.checked || !selectedDefault' in javascript
    assert "尚未切换" in rendered
    assert "current 已切换" in rendered
    assert '[data-candidate-built-field="release_id"]' in javascript


def test_activation_ui_posts_only_frozen_payload_keys() -> None:
    javascript = (STATIC / "studio.js").read_text(encoding="utf-8")
    check_match = re.search(
        r"postJsonEnvelope\(panel\.dataset\.activationCheckRoute, \{(?P<body>.*?)\n\s*\}\);",
        javascript,
        re.DOTALL,
    )
    apply_match = re.search(
        r"postJsonEnvelope\(panel\.dataset\.activationApplyRoute, \{(?P<body>.*?)\n\s*\}\);",
        javascript,
        re.DOTALL,
    )
    assert check_match is not None
    assert apply_match is not None

    key_pattern = re.compile(r"^\s*([a-z_]+):", re.MULTILINE)
    assert key_pattern.findall(check_match.group("body")) == [
        "run_id",
        "minecraft_version",
        "target_release_id",
    ]
    assert key_pattern.findall(apply_match.group("body")) == [
        "activation_check_id",
        "confirm_current_switch",
        "set_as_default",
    ]


def test_r3_release_build_reads_required_top_level_hashes_in_stable_order() -> None:
    javascript = (STATIC / "studio.js").read_text(encoding="utf-8")

    manifest = javascript.index('["manifest_sha256", data.manifest_sha256]')
    quality = javascript.index('["quality_report_sha256", data.quality_report_sha256]')
    checksums = javascript.index('["checksums_sha256", data.checksums_sha256]')
    assert manifest < quality < checksums
    assert "renderCandidateHashes(panel, data);" in javascript
    assert "data.hashes" not in javascript
    assert 'throw { code: "RELEASE_BUILD_RESULT_INVALID", message: "候选构建摘要不完整。" }' in javascript
