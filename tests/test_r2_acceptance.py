from __future__ import annotations

import json
import re
from pathlib import Path

from tools import validate_r2


def test_absolute_path_regex_has_boundary_without_matching_loopback_url() -> None:
    assert validate_r2.ABSOLUTE_RE.search("GET http://127.0.0.1:8765/") is None
    assert validate_r2.ABSOLUTE_RE.search(r"C:\Users\tester") is not None
    assert validate_r2.ABSOLUTE_RE.search(r"\\server\share") is not None
    assert validate_r2.ABSOLUTE_RE.search("/home/tester") is not None


def test_r2_acceptance_report_is_blocked_only_by_python_environment_and_safe(monkeypatch) -> None:
    monkeypatch.setattr(validate_r2.platform, "python_version", lambda: "3.14.3")
    report = validate_r2.validate_repository(Path(__file__).resolve().parents[1])
    assert report["status"] == "blocked"
    assert all(check["status"] == "passed" for check in report["checks"].values())
    assert report["environment"]["python_baseline_passed"] is False
    assert report["environment"]["linux_r2_evidence"] is False
    assert {issue["code"] for issue in report["issues"]} == {"PYTHON_RUNTIME_BASELINE_MISMATCH"}
    assert report["environment"]["linux_validation_stage"] == "R5"
    assert "LINUX_R2_PYTHON_RUNTIME_AND_WEB_R5" in report["deferred"]
    serialized = json.dumps(report, ensure_ascii=False, sort_keys=True)
    assert re.search(r"(?i)(?:[A-Z]:[\\/]|\\\\|/(?:home|Users|tmp|var|data|mnt|opt|root)/)", serialized) is None
    assert re.search(r"(?i)(authorization|api[_-]?key|secret|token|usage|cost|budget)", serialized) is None


def test_r2_acceptance_report_passes_on_frozen_python_without_linux_gate(monkeypatch) -> None:
    monkeypatch.setattr(validate_r2.platform, "python_version", lambda: "3.14.7")
    report = validate_r2.validate_repository(Path(__file__).resolve().parents[1])
    assert report["status"] == "passed"
    assert report["issues"] == []
    assert all(check["status"] == "passed" for check in report["checks"].values())
    assert report["environment"]["python_baseline_passed"] is True
    assert report["environment"]["linux_r2_evidence"] is False
    assert report["environment"]["linux_validation_stage"] == "R5"
    assert report["deferred"]
