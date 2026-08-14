"""Consistent, read-only public run snapshots used by HTTP and SSE."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .paths import DataRoot, safe_relative_posix_ref, validate_minecraft_version
from .storage import WorkspaceDatabase
from .worker import _is_stale


_ALLOWED_AUDIT_EVENTS = {
    "STAGE_STARTED",
    "STAGE_SUCCEEDED",
    "STAGE_FAILED",
    "FEATURE_ITEM_SUCCEEDED",
    "FEATURE_ITEM_FAILED",
    "RUN_PAUSED_REQUESTED",
    "RUN_PAUSED_AFTER_ITEM",
    "RUN_RESUMED",
    "RUN_CANCELLED",
    "RUN_RETRY_FAILED",
    "WORKER_RECOVERED_STALE_RUNNING",
    "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING",
    "IMPORT_CHECKED_AND_PROJECTED",
}
_SAFE_HASH_PREFIX = "sha256:"
_AUDIT_STEP_LABELS = {
    "STAGE_STARTED": "阶段开始",
    "STAGE_SUCCEEDED": "阶段完成",
    "STAGE_FAILED": "阶段失败",
    "FEATURE_ITEM_SUCCEEDED": "条目完成",
    "FEATURE_ITEM_FAILED": "条目失败",
    "RUN_PAUSED_REQUESTED": "已请求暂停",
    "RUN_PAUSED_AFTER_ITEM": "条目完成后暂停",
    "RUN_RESUMED": "继续运行",
    "RUN_CANCELLED": "运行已取消",
    "RUN_RETRY_FAILED": "重试失败项",
    "WORKER_RECOVERED_STALE_RUNNING": "已恢复 stale 项",
    "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING": "到达 R3 边界",
    "IMPORT_CHECKED_AND_PROJECTED": "导入检查完成",
}


class RunSnapshotService:
    """Open one read-only connection and one transaction for each snapshot."""

    def __init__(self, data_root: DataRoot, *, stale_after_seconds: int = 300):
        self.data_root = data_root
        self.stale_after_seconds = stale_after_seconds

    def snapshot(self, run_id: str) -> dict[str, Any]:
        version, path = self._locate(run_id)
        database = WorkspaceDatabase.open(path, read_only=True)
        try:
            with database.read_transaction() as connection:
                run = connection.execute(
                    "SELECT run_id,import_id,minecraft_version,status,current_stage,boundary_event,config_snapshot_json,created_at,started_at,finished_at FROM runs WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                if run is None:
                    raise KeyError(run_id)
                stages = connection.execute(
                    "SELECT stage,ordinal,status,cursor_json,recovery_attempt,pause_after_item,heartbeat_at FROM stage_runs WHERE run_id=? ORDER BY ordinal",
                    (run_id,),
                ).fetchall()
                jobs = connection.execute(
                    "SELECT job_id,stage,logical_key,status,auto_attempt,heartbeat_at,output_hash,error_code,error_message FROM jobs WHERE run_id=? ORDER BY stage,logical_key",
                    (run_id,),
                ).fetchall()
                counts = connection.execute(
                    "SELECT status,COUNT(*) AS count FROM jobs WHERE run_id=? GROUP BY status",
                    (run_id,),
                ).fetchall()
                stage_counts = connection.execute(
                    "SELECT stage,status,COUNT(*) AS count FROM jobs WHERE run_id=? GROUP BY stage,status ORDER BY stage,status",
                    (run_id,),
                ).fetchall()
                audits = connection.execute(
                    "SELECT event_type,created_at FROM audit_events WHERE run_id=? ORDER BY created_at DESC LIMIT 8",
                    (run_id,),
                ).fetchall()
                stale = self._stale_diagnostics(connection, run_id)
                snapshot = self._shape(
                    run,
                    stages,
                    jobs,
                    counts,
                    stage_counts,
                    audits,
                    stale,
                    version,
                )
        finally:
            database.close()
        return snapshot

    def _locate(self, run_id: str) -> tuple[str, Path]:
        if not isinstance(run_id, str) or not run_id or "/" in run_id or "\\" in run_id:
            raise KeyError(run_id)
        if not self.data_root.workspace.is_dir():
            raise KeyError(run_id)
        matches: list[tuple[str, Path]] = []
        for version_dir in self.data_root.workspace.iterdir():
            if not version_dir.is_dir() or version_dir.is_symlink():
                continue
            try:
                validate_minecraft_version(version_dir.name)
                safe_relative_posix_ref(run_id)
            except ValueError:
                continue
            candidate = version_dir / run_id / "work.sqlite3"
            if candidate.is_file() and not candidate.is_symlink():
                matches.append((version_dir.name, candidate))
        if len(matches) != 1:
            raise KeyError(run_id)
        return matches[0]

    def _stale_diagnostics(self, connection: Any, run_id: str) -> list[dict[str, Any]]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.stale_after_seconds)
        markers: list[dict[str, Any]] = []
        stale_jobs = connection.execute(
            "SELECT job_id,stage,logical_key,heartbeat_at FROM jobs WHERE run_id=? AND status='running'",
            (run_id,),
        ).fetchall()
        for row in stale_jobs:
            if _is_stale(row["heartbeat_at"], cutoff):
                markers.append(
                    {
                        "job_id": _safe_id(row["job_id"]),
                        "stage": _safe_stage(row["stage"]),
                        "logical_key": _safe_id(row["logical_key"]),
                        "heartbeat_at": _safe_text(row["heartbeat_at"]),
                        "stale": True,
                    }
                )
        stale_stages = connection.execute(
            "SELECT stage,heartbeat_at FROM stage_runs WHERE run_id=? AND status='running'",
            (run_id,),
        ).fetchall()
        for row in stale_stages:
            if _is_stale(row["heartbeat_at"], cutoff) and not any(marker["stage"] == row["stage"] for marker in markers):
                markers.append(
                    {
                        "job_id": None,
                        "stage": _safe_stage(row["stage"]),
                        "logical_key": None,
                        "heartbeat_at": _safe_text(row["heartbeat_at"]),
                        "stale": True,
                    }
                )
        return markers

    def _shape(self, run: Any, stages: Any, jobs: Any, counts: Any, stage_counts: Any, audits: Any, stale: Any, version: str) -> dict[str, Any]:
        config = _safe_config(run["config_snapshot_json"])
        item_counts: dict[str, Any] = {
            str(row["status"]): int(row["count"])
            for row in counts
            if str(row["status"]) in {"pending", "running", "succeeded", "needs_review", "failed", "skipped"}
        }
        item_counts["total"] = sum(item_counts.values())
        by_stage: dict[str, dict[str, int]] = {}
        for row in stage_counts:
            stage = _safe_stage(row["stage"])
            status = str(row["status"])
            if status not in {"pending", "running", "succeeded", "needs_review", "failed", "skipped"}:
                continue
            by_stage.setdefault(stage, {})[status] = int(row["count"])
        item_counts["by_stage"] = by_stage
        public_jobs = []
        for row in jobs:
            public_jobs.append(
                {
                    "job_id": _safe_id(row["job_id"]),
                    "stage": _safe_stage(row["stage"]),
                    "logical_key": _safe_id(row["logical_key"]),
                    "status": _safe_item_status(row["status"]),
                    "auto_attempt": _safe_int(row["auto_attempt"]),
                    "heartbeat_at": _safe_text(row["heartbeat_at"]),
                    "output_hash": _safe_hash(row["output_hash"]),
                    "error_code": _safe_code(row["error_code"]),
                    "error_message": "任务失败；请按错误码处理。" if row["error_message"] else None,
                }
            )
        latest_steps = [
            {
                "event_type": str(row["event_type"]),
                "created_at": _safe_text(row["created_at"]),
            }
            for row in audits
            if str(row["event_type"]) in _ALLOWED_AUDIT_EVENTS
        ]
        latest_presentation = [
            {
                "code": step["event_type"],
                "label": _AUDIT_STEP_LABELS.get(step["event_type"], step["event_type"]),
                "status": _audit_step_status(step["event_type"]),
                "created_at": step["created_at"],
            }
            for step in latest_steps
        ]
        current_stage = _safe_stage(run["current_stage"])
        current_heartbeat = next(
            (_safe_text(row["heartbeat_at"]) for row in stages if _safe_stage(row["stage"]) == current_stage),
            None,
        )
        public_stages = [
            {
                "stage": _safe_stage(row["stage"]),
                "ordinal": _safe_int(row["ordinal"]),
                "status": _safe_run_status(row["status"]),
                "error_code": _cursor_error_code(row["cursor_json"]),
                "error_present": _cursor_has_error(row["cursor_json"]),
                "recovery_attempt": _safe_int(row["recovery_attempt"]),
                "pause_after_item": bool(row["pause_after_item"]),
                "heartbeat_at": _safe_text(row["heartbeat_at"]),
            }
            for row in stages
        ]
        completed_stages = sum(stage["status"] == "succeeded" for stage in public_stages)
        r2_stages = public_stages[:6]
        r2_completed = sum(stage["status"] == "succeeded" for stage in r2_stages)
        return {
            "run_id": _safe_id(run["run_id"]),
            "import_id": _safe_id(run["import_id"]),
            "minecraft_version": version,
            "status": _safe_run_status(run["status"]),
            "current_stage": current_stage,
            "heartbeat_at": current_heartbeat,
            "boundary_event": _safe_code(run["boundary_event"]),
            "created_at": _safe_text(run["created_at"]),
            "started_at": _safe_text(run["started_at"]),
            "finished_at": _safe_text(run["finished_at"]),
            "config_snapshot": config,
            "stages": public_stages,
            "jobs": public_jobs,
            "item_counts": item_counts,
            "item_progress": {
                "total": item_counts["total"],
                "completed": item_counts.get("succeeded", 0) + item_counts.get("skipped", 0),
                "by_status": {key: value for key, value in item_counts.items() if key != "by_stage"},
                "has_items": item_counts["total"] > 0,
            },
            "latest_audit_steps": latest_steps,
            "latest_steps": latest_presentation,
            "stale": stale,
            "progress": {
                "completed_stages": completed_stages,
                "total_stages": len(public_stages),
                "percent": round(completed_stages / len(public_stages) * 100) if public_stages else 0,
                "r2_completed_stages": r2_completed,
                "r2_total_stages": min(6, len(public_stages)),
                "r2_percent": round(r2_completed / len(r2_stages) * 100) if r2_stages else 0,
            },
        }


def _safe_config(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    allowed = {"effective_config_hash", "feature_extractor_version", "force_normalized_like", "minecraft_version", "schema_version", "search_mode", "workspace_schema_version"}
    return {key: value[key] for key in allowed if key in value and _safe_public_value(value[key])}


def _cursor_error_code(raw: Any) -> str | None:
    try:
        value = json.loads(raw or "{}") if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    return _safe_code(value.get("error_code"))


def _cursor_has_error(raw: Any) -> bool:
    return _cursor_error_code(raw) is not None


def _safe_public_value(value: Any) -> bool:
    if isinstance(value, (bool, int, float)) or value is None:
        return True
    if isinstance(value, str):
        return "/" not in value and "\\" not in value and len(value) <= 500
    return False


def _safe_id(value: Any) -> str | None:
    text = str(value) if value is not None else ""
    return text if text and "/" not in text and "\\" not in text and len(text) <= 256 else None


def _safe_text(value: Any) -> str | None:
    return None if value is None else (str(value)[:128] if "/" not in str(value) and "\\" not in str(value) else None)


def _safe_code(value: Any) -> str | None:
    text = str(value) if value is not None else ""
    return text if text.isascii() and text.replace("_", "").isalnum() and text.isupper() and len(text) <= 128 else None


def _safe_hash(value: Any) -> str | None:
    text = str(value) if value is not None else ""
    return text if text.startswith(_SAFE_HASH_PREFIX) and len(text) == 71 and all(character in "0123456789abcdef" for character in text[7:]) else None


def _safe_stage(value: Any) -> str:
    from .stages import STUDIO_STAGES

    text = str(value) if value is not None else ""
    return text if text in STUDIO_STAGES else STUDIO_STAGES[0]


def _safe_run_status(value: Any) -> str:
    from .stages import RUN_STATES

    text = str(value) if value is not None else "pending"
    return text if text in RUN_STATES else "pending"


def _safe_item_status(value: Any) -> str:
    from .stages import ITEM_STATES

    text = str(value) if value is not None else "pending"
    return text if text in ITEM_STATES else "pending"


def _safe_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _audit_step_status(code: str) -> str:
    if code.endswith("FAILED"):
        return "failed"
    if code.endswith("SUCCEEDED") or code == "WORKER_RECOVERED_STALE_RUNNING":
        return "succeeded"
    if code == "RUN_CANCELLED":
        return "cancelled"
    if code in {"RUN_PAUSED_REQUESTED", "RUN_PAUSED_AFTER_ITEM"}:
        return "paused"
    return "running"


__all__ = ["RunSnapshotService"]
