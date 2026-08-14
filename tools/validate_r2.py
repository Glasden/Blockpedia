"""Run the small, static R2 acceptance gate.

The gate intentionally reports repository facts only.  It does not run the
behavior suite, open a production data root, or validate a real export.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib
import io
import json
import platform
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


EXPECTED_PYTHON = "3.14.7"
EXPECTED_PROJECT_DEPS = {
    "fastapi==0.141.1",
    "uvicorn==0.52.3",
    "Jinja2==3.1.6",
    "jsonschema==4.26.0",
}
EXPECTED_PACKAGE_DATA = {
    "sql/*.sql",
    "sql/*.sha256",
    "templates/**/*.html",
    "static/**/*",
}
EXPECTED_STAGES = (
    "PREPARE",
    "IMPORT_EXPORT",
    "VALIDATE_REGISTRY",
    "VALIDATE_VARIANTS",
    "VALIDATE_RENDERS",
    "EXTRACT_FEATURES",
    "AI_ANNOTATE",
    "VALIDATE",
    "HUMAN_REVIEW",
    "BUILD_RELEASE",
    "ACTIVATE_RELEASE",
)
EXPECTED_R2_STAGES = EXPECTED_STAGES[:6]
HTMX_HASHES = {
    "src/blockpedia/static/vendor/htmx.min.js": "71ea67185bfa8c98c39d31717c6fce5d852370fcdfd129db4543774d3145c0de",
    "src/blockpedia/static/vendor/LICENSE.htmx": "d3d2456f76414f2456104660ebd65aff1c04cd7966b942bdabd63f3cdb316a38",
}
OFFICIAL_DISCLAIMER = "NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT."
SENSITIVE_RE = re.compile(r"(?i)(authorization|api[_-]?key|secret|token|usage|cost|budget)")
# Keep the drive/UNC/POSIX alternatives bounded.  Without the left boundary,
# the ``p:/`` suffix of ``http://`` is misread as a Windows drive path.
ABSOLUTE_RE = re.compile(
    r"(?:"
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"
    r"|(?<![A-Za-z0-9_])\\\\(?:[^\\\r\n]+)"
    r"|(?<![A-Za-z0-9_])/(?:home|Users|tmp|var|data|mnt|opt|root)/"
    r")"
)
PACKAGE_LINE_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s\\]+)")


def _sha256(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _check(status: str, *, details: Mapping[str, Any] | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status}
    if details:
        result["details"] = dict(details)
    return result


def _issue(code: str, detail: str) -> dict[str, str]:
    return {"code": code, "detail": detail}


def _load_toml(path: Path) -> Mapping[str, Any]:
    import tomllib

    return tomllib.loads(path.read_text(encoding="utf-8"))


def _direct_requirement_lines(path: Path) -> list[str]:
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line and re.fullmatch(r"[A-Za-z0-9_.-]+==[^\s]+", line):
            lines.append(line)
    return lines


def _locked_blocks(path: Path) -> dict[str, str]:
    blocks: dict[str, str] = {}
    current_name: str | None = None
    current: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = PACKAGE_LINE_RE.match(line)
        if match:
            if current_name is not None:
                blocks[current_name.casefold()] = "\n".join(current)
            current_name = match.group(1)
            current = [line]
        elif current_name is not None:
            current.append(line)
    if current_name is not None:
        blocks[current_name.casefold()] = "\n".join(current)
    return blocks


def _check_pyproject(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    try:
        document = _load_toml(root / "pyproject.toml")
        project = document["project"]
        deps = set(project.get("dependencies", []))
        scripts = project.get("scripts", {})
        package_data = set(document["tool"]["setuptools"]["package-data"]["blockpedia"])
        passed = (
            project.get("requires-python") == "==3.14.7"
            and deps == EXPECTED_PROJECT_DEPS
            and scripts == {"block-index": "blockpedia.cli:main"}
            and EXPECTED_PACKAGE_DATA <= package_data
        )
        if not passed:
            issues.append(_issue("PYPROJECT_CONTRACT_INVALID", "R2 project metadata is not frozen."))
        return _check("passed" if passed else "failed"), issues
    except Exception:
        return _check("failed"), [_issue("PYPROJECT_CONTRACT_INVALID", "R2 project metadata could not be read.")]


def _check_lock(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    try:
        direct = _direct_requirement_lines(root / "requirements.in")
        blocks = _locked_blocks(root / "requirements.lock")
        missing: list[str] = []
        unhashed: list[str] = []
        for requirement in direct:
            name, version = requirement.split("==", 1)
            block = blocks.get(name.casefold())
            if block is None or f"=={version}" not in block:
                missing.append(name)
            elif "--hash=sha256:" not in block:
                unhashed.append(name)
        passed = not missing and not unhashed
        if not passed:
            issues.append(_issue("DEPENDENCY_LOCK_INVALID", "Direct requirements are not exactly hash-locked."))
        return _check("passed" if passed else "failed", details={"direct_count": len(direct)}), issues
    except Exception:
        return _check("failed"), [_issue("DEPENDENCY_LOCK_INVALID", "Dependency lock could not be read.")]


def _check_sql(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    sql_path = root / "src/blockpedia/sql/workspace.v1.sql"
    hash_path = root / "src/blockpedia/sql/workspace.v1.sha256"
    try:
        sql = sql_path.read_text(encoding="utf-8")
        expected_hash = hash_path.read_text(encoding="ascii").strip()
        hash_ok = expected_hash == _sha256(sql.encode("utf-8"))
        statuses = ("pending", "running", "paused", "needs_review", "failed", "succeeded", "cancelled")
        status_ok = all(f"'{status}'" in sql for status in statuses)
        active_ok = "CREATE UNIQUE INDEX provider_profiles_one_active ON provider_profiles(active) WHERE active = 1;" in sql
        forbidden_columns = re.search(r"(?im)^\s*[A-Za-z_][A-Za-z0-9_]*(?:token|usage|cost|budget|api[_-]?key|authorization)[A-Za-z0-9_]*\s+", sql)
        no_sensitive_columns = forbidden_columns is None
        passed = hash_ok and status_ok and active_ok and no_sensitive_columns
        if not passed:
            issues.append(_issue("SQL_CONTRACT_INVALID", "Workspace schema hash or frozen constraints are invalid."))
        return _check("passed" if passed else "failed"), issues
    except Exception:
        return _check("failed"), [_issue("SQL_CONTRACT_INVALID", "Workspace schema could not be read.")]


def _check_cli(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    del root
    issues: list[dict[str, str]] = []
    try:
        cli = importlib.import_module("blockpedia.cli")
        parser = cli.build_parser()
        subparsers = next(action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction")
        choices = set(subparsers.choices)
        host_port_rejected = True
        for option in ("--host", "--port"):
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    parser.parse_args(["web", option, "x"])
                except SystemExit:
                    continue
            host_port_rejected = False
        source = __import__("inspect").getsource(cli._run_web)
        passed = choices == {"web", "mcp"} and host_port_rejected and cli.WEB_HOST == "127.0.0.1" and cli.WEB_PORT == 8765 and "access_log=False" in source
        if not passed:
            issues.append(_issue("CLI_CONTRACT_INVALID", "CLI command or loopback contract is invalid."))
        return _check("passed" if passed else "failed"), issues
    except Exception:
        return _check("failed"), [_issue("CLI_CONTRACT_INVALID", "CLI contract could not be inspected.")]


def _check_web(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    try:
        sys.path.insert(0, str(root / "src"))
        web = importlib.import_module("blockpedia.web")
        with tempfile.TemporaryDirectory() as temporary:
            from blockpedia.paths import DataRoot
            from blockpedia.services import StudioService

            data_root = DataRoot(Path(temporary))
            service = StudioService(data_root, repo_root=root)
            try:
                app = web.create_app(data_root=data_root, repo_root=root, service=service, start_worker=False)
                paths = {route.path for route in app.routes}
                middleware = {middleware.cls.__name__.lower() for middleware in app.user_middleware}
            finally:
                service.close()
        required = {"/api/imports/check", "/api/imports", "/api/runs", "/api/runs/{run_id}", "/api/runs/{run_id}/recover"}
        forbidden = ("/api/provider", "/api/releases", "/api/current", "/api/mcp", "/mcp")
        passed = required <= paths and not any(path.startswith(forbidden) for path in paths) and not any(token in name for name in middleware for token in ("cors", "auth", "csrf")) and getattr(web, "UNOFFICIAL_NOTICE", "") == OFFICIAL_DISCLAIMER
        if not passed:
            issues.append(_issue("WEB_CONTRACT_INVALID", "R2 Web routes or security boundary is invalid."))
        return _check("passed" if passed else "failed"), issues
    except Exception:
        return _check("failed"), [_issue("WEB_CONTRACT_INVALID", "R2 Web adapter could not be inspected.")]


def _check_stages() -> tuple[dict[str, Any], list[dict[str, str]]]:
    try:
        stages = importlib.import_module("blockpedia.stages")
        passed = tuple(stages.STUDIO_STAGES) == EXPECTED_STAGES and tuple(stages.R2_STAGES) == EXPECTED_R2_STAGES
    except Exception:
        passed = False
    return _check("passed" if passed else "failed"), ([] if passed else [_issue("STAGE_CONTRACT_INVALID", "Studio stage order or R2 boundary is invalid.")])


def _check_assets_and_templates(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    try:
        for relative, expected in HTMX_HASHES.items():
            path = root / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                issues.append(_issue("HTMX_ASSET_INVALID", "Pinned local HTMX bytes or hash are invalid."))
        template_root = root / "src/blockpedia/templates"
        external = any(bool(re.search(r"https?://", path.read_text(encoding="utf-8"))) for path in template_root.rglob("*.html"))
        web = importlib.import_module("blockpedia.web")
        if external or getattr(web, "UNOFFICIAL_NOTICE", "") != OFFICIAL_DISCLAIMER:
            issues.append(_issue("WEB_ASSET_OR_DISCLAIMER_INVALID", "Templates or official disclaimer contract is invalid."))
        return _check("passed" if not issues else "failed"), issues
    except Exception:
        return _check("failed"), [_issue("WEB_ASSET_OR_DISCLAIMER_INVALID", "Local Web assets could not be inspected.")]


def _public_files(root: Path) -> Iterable[Path]:
    roots = (root / "src/blockpedia", root / "tests/r2", root / "tools", root / "docs/r2-implementation.md")
    for candidate in roots:
        if candidate.is_file():
            yield candidate
        elif candidate.is_dir():
            yield from (path for path in candidate.rglob("*") if path.is_file())


def _check_public_surface(root: Path) -> tuple[dict[str, Any], list[dict[str, str]]]:
    issues: list[dict[str, str]] = []
    try:
        files = tuple(_public_files(root))
        forbidden_suffixes = {".png", ".jar", ".sqlite", ".sqlite3", ".db"}
        if any(path.suffix.casefold() in forbidden_suffixes for path in files):
            issues.append(_issue("PUBLIC_GENERATED_ASSET_FOUND", "Public R2 paths contain a generated asset."))
        for path in (root / "docs/r2-implementation.md", root / "docs/evidence/r2-validation-report.json"):
            if path.is_file():
                text = path.read_text(encoding="utf-8")
                if ABSOLUTE_RE.search(text) or SENSITIVE_RE.search(text):
                    issues.append(_issue("PUBLIC_SENSITIVE_OUTPUT_FOUND", "Acceptance documentation or evidence contains a sensitive path pattern."))
                    break
        return _check("passed" if not issues else "failed", details={"file_count": len(files)}), issues
    except Exception:
        return _check("failed"), [_issue("PUBLIC_SURFACE_INVALID", "Public R2 paths could not be inspected.")]


def validate_repository(repo_root: str | Path) -> dict[str, Any]:
    root = Path(repo_root).resolve()
    if str(root / "src") not in sys.path:
        sys.path.insert(0, str(root / "src"))
    checks: dict[str, dict[str, Any]] = {}
    issues: list[dict[str, str]] = []
    checkers: tuple[tuple[str, Callable[..., tuple[dict[str, Any], list[dict[str, str]]]], tuple[Any, ...]], ...] = (
        ("pyproject", _check_pyproject, (root,)),
        ("dependency_lock", _check_lock, (root,)),
        ("workspace_sql", _check_sql, (root,)),
        ("cli", _check_cli, (root,)),
        ("web", _check_web, (root,)),
        ("stages", _check_stages, ()),
        ("local_assets_and_templates", _check_assets_and_templates, (root,)),
        ("public_surface", _check_public_surface, (root,)),
    )
    for name, checker, arguments in checkers:
        result, found = checker(*arguments)
        checks[name] = result
        issues.extend(found)
    python_version = platform.python_version()
    python_ok = python_version == EXPECTED_PYTHON
    if not python_ok:
        issues.append(_issue("PYTHON_RUNTIME_BASELINE_MISMATCH", "The running Python version is not the frozen R2 baseline."))
    static_failed = any(result["status"] == "failed" for result in checks.values())
    status = "failed" if static_failed else ("blocked" if not python_ok else "passed")
    return {
        "status": status,
        "generated_at": _utc_now(),
        "environment": {
            "python_implementation": platform.python_implementation(),
            "python_version": python_version,
            "python_baseline": EXPECTED_PYTHON,
            "python_baseline_passed": python_ok,
            "os": platform.system(),
            "architecture": platform.machine(),
            "linux_r2_evidence": False,
            "linux_validation_stage": "R5",
        },
        "checks": checks,
        "issues": issues,
        "deferred": [
            "LINUX_R2_PYTHON_RUNTIME_AND_WEB_R5",
            "LINUX_R4_MCP_STDIO_R5",
            "LINUX_WHEEL_ABI_R5",
            "LINUX_JAVA_RUNTIME_EXPORTER_R5",
            "FINAL_DUAL_PLATFORM_REPRODUCTION_R5",
        ],
        "commands": {
            "executed": ["python -m tools.validate_r2 --repo-root . --report docs/evidence/r2-validation-report.json"],
            "recommended": [
                "PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider",
                "python -m pip check",
                "git diff --check",
            ],
        },
    }


def _write_report(path: Path, report: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    report = validate_repository(args.repo_root)
    _write_report(args.report, report)
    return {"passed": 0, "failed": 1, "blocked": 2}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
