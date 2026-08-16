from __future__ import annotations

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

    assert 'include "partials/release_candidate.html"' in run_detail
    assert 'R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING' in run_detail
    assert 'postJsonEnvelope(panel.dataset.candidateCheckRoute' in javascript
    assert 'postJsonEnvelope(panel.dataset.candidateBuildRoute' in javascript
    assert 'confirm_immutable_release: true' in javascript
    assert 'run_id: panel.dataset.candidateRunId' in javascript
    assert 'minecraft_version: panel.dataset.candidateVersion' in javascript

    combined = (partial + javascript).lower()
    assert all(route not in combined for route in (
        "/api/releases/activation",
        "/api/releases/apply",
        "/api/releases/rollback",
        "/api/mcp",
    ))
    assert all(term not in partial.lower() for term in ("token", "usage", "cost", "budget"))
    assert "relative_path" not in partial
    assert "type=\"text\"" not in partial
    assert "<select" not in partial
    assert "<textarea" not in partial


def test_r3_release_build_reads_required_top_level_hashes_in_stable_order() -> None:
    javascript = (STATIC / "studio.js").read_text(encoding="utf-8")

    manifest = javascript.index('["manifest_sha256", data.manifest_sha256]')
    quality = javascript.index('["quality_report_sha256", data.quality_report_sha256]')
    checksums = javascript.index('["checksums_sha256", data.checksums_sha256]')
    assert manifest < quality < checksums
    assert "renderCandidateHashes(panel, data);" in javascript
    assert "data.hashes" not in javascript
    assert 'throw { code: "RELEASE_BUILD_RESULT_INVALID", message: "候选构建摘要不完整。" }' in javascript
