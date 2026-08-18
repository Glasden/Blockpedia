"""Minimal R5 activation check, MCP smoke, and current-pointer apply."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .mcp_release import MCPReleaseError, MCPReleaseResolver, _hash_bytes, _json_file, _lstat as _mcp_lstat, _read_regular
from .paths import DataRoot, RELEASE_BUILD_ID_RE, RELEASE_CHECK_ID_RE, RELEASE_ID_RE, validate_minecraft_version
from .releases import (
    ReleaseBuildFailure,
    ReleaseBuilder,
    ReleaseCheckNotFound,
    _atomic_json,
    _fsync_directory,
    _hash_file,
    _release_id_for_build_id,
)
from .r3 import canonical_json
from .schema import RecordSchemaError, validate_record
from .storage import DatabaseSchemaMismatch, WorkspaceDatabase, utc_now


ACTIVATION_CHECK_ID_RE = re.compile(r"^activation_[0-9a-f]{32}$")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
ERROR_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
MCP_STDIO_LINE_LIMIT = 1024 * 1024
ACTIVATION_STATE_FIELDS = frozenset(
    {
        "format_version",
        "activation_check_id",
        "run_id",
        "minecraft_version",
        "target_release_id",
        "candidate_releases",
        "expected_current_sha256",
        "status",
        "can_apply",
        "created_at",
        "updated_at",
        "error_code",
    }
)
ACTIVATION_STATUSES = frozenset({"passed", "failed", "stale", "applied"})
EXPECTED_RELEASE_ENTRIES = frozenset(
    {
        "release.json",
        "manifest.json",
        "index.sqlite3",
        "previews",
        "quality_report.json",
        "manual-overrides.json",
        "schemas.sha256",
        "checksums.sha256",
    }
)
CURRENT_SWITCH_LOCK = threading.Lock()


class ActivationError(RuntimeError):
    """Stable activation operation failure."""

    def __init__(self, code: str, message: str = "activation operation is not allowed") -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Candidate:
    check_id: str
    release_build_id: str
    release_id: str
    checksums_sha256: str
    release_path: Path
    state: dict[str, Any]
    report: dict[str, Any]


class ActivationService:
    """Own the small mutable activation boundary without adding a framework."""

    def __init__(self, data_root: DataRoot, *, repo_root: Path, force_normalized_like: bool = False) -> None:
        self.data_root = data_root
        self.repo_root = repo_root
        self.force_normalized_like = force_normalized_like
        self.release_builder = ReleaseBuilder(
            data_root,
            repo_root=repo_root,
            force_normalized_like=force_normalized_like,
        )
        self.before_current_replace: Callable[[Path, Path], None] | None = None
        self.after_current_replace: Callable[[], None] | None = None

    def check(self, run_id: str, minecraft_version: str, target_release_id: str) -> dict[str, Any]:
        activation_check_id = "activation_" + uuid.uuid4().hex
        created_at = utc_now()
        candidates: list[Candidate] = []
        expected_current_sha256: str | None = None
        try:
            validate_minecraft_version(minecraft_version)
            current, current_bytes = self._read_current()
            del current
            expected_current_sha256 = _hash_bytes(current_bytes) if current_bytes is not None else None
            candidates = self._collect_candidates(run_id, minecraft_version)
            if len(candidates) < 2:
                raise ActivationError("ACTIVATION_CANDIDATES_INSUFFICIENT")
            selected = next((item for item in candidates if item.release_id == target_release_id), None)
            if selected is None:
                raise ActivationError("ACTIVATION_TARGET_INVALID")
            before_checksums = selected.checksums_sha256
            before_current = expected_current_sha256
            self._mcp_smoke(minecraft_version, selected)
            after_current, after_current_bytes = self._read_current()
            del after_current
            after_current_sha256 = _hash_bytes(after_current_bytes) if after_current_bytes is not None else None
            after_checksums = _hash_file(selected.release_path / "checksums.sha256", selected.release_path)
            if after_current_sha256 != before_current or after_checksums != before_checksums:
                raise ActivationError("ACTIVATION_INPUT_STALE")
            state = self._make_state(
                activation_check_id,
                run_id,
                minecraft_version,
                target_release_id,
                candidates,
                expected_current_sha256,
                created_at,
                status="passed",
                can_apply=True,
                error_code=None,
            )
        except ActivationError as exc:
            state = self._make_state(
                activation_check_id,
                run_id,
                minecraft_version,
                target_release_id,
                candidates,
                expected_current_sha256,
                created_at,
                status="failed",
                can_apply=False,
                error_code=exc.code,
            )
        self._write_state(state)
        return state

    def apply(self, activation_check_id: str, *, confirm_current_switch: bool, set_as_default: bool) -> dict[str, Any]:
        if confirm_current_switch is not True:
            raise ActivationError("ACTIVATION_CONFIRMATION_REQUIRED")
        if not isinstance(set_as_default, bool):
            raise ActivationError("ACTIVATION_DEFAULT_INVALID")
        if ACTIVATION_CHECK_ID_RE.fullmatch(activation_check_id) is None:
            raise ActivationError("ACTIVATION_CHECK_NOT_FOUND")
        if not CURRENT_SWITCH_LOCK.acquire(blocking=False):
            raise ActivationError("CURRENT_SWITCH_BUSY")
        try:
            state = self._read_state(activation_check_id)
            if state["status"] == "applied":
                return state
            if state["status"] != "passed" or state["can_apply"] is not True:
                raise ActivationError("ACTIVATION_CHECK_NOT_READY")
            latest = self._latest_state(state["minecraft_version"])
            if latest["activation_check_id"] != activation_check_id:
                self._mark_state(state, "stale", False, "ACTIVATION_CHECK_STALE")
                raise ActivationError("ACTIVATION_CHECK_STALE")

            current, current_bytes = self._read_current()
            current_sha256 = _hash_bytes(current_bytes) if current_bytes is not None else None
            completed_retry = self._completed_transition_proof(state, set_as_default=set_as_default)
            candidates = self._collect_candidates(state["run_id"], state["minecraft_version"], completed_retry=completed_retry)
            candidate_entries = self._candidate_entries(candidates)
            if candidate_entries != state["candidate_releases"]:
                self._mark_state(state, "stale", False, "ACTIVATION_CANDIDATES_STALE")
                raise ActivationError("ACTIVATION_CANDIDATES_STALE")
            target = next((item for item in candidates if item.release_id == state["target_release_id"]), None)
            if target is None:
                self._mark_state(state, "stale", False, "ACTIVATION_TARGET_INVALID")
                raise ActivationError("ACTIVATION_TARGET_INVALID")
            if current_sha256 != state["expected_current_sha256"]:
                intended_probe = self._pointer_for_target(
                    current,
                    state["minecraft_version"],
                    target,
                    set_as_default=set_as_default if completed_retry else True,
                )
                if current is None or not self._pointer_matches_intent(current, intended_probe):
                    self._mark_state(state, "stale", False, "ACTIVATION_CURRENT_STALE")
                    raise ActivationError("ACTIVATION_CURRENT_STALE")
            if current is None and set_as_default is not True:
                raise ActivationError("ACTIVATION_DEFAULT_REQUIRED")

            manifest = self._release_manifest(target.release_path)
            intended = self._pointer_for_target(current, state["minecraft_version"], target, manifest=manifest, set_as_default=set_as_default)
            pointer_matches = current is not None and self._pointer_matches_intent(current, intended)
            if completed_retry and not pointer_matches:
                raise ActivationError("ACTIVATION_CURRENT_STALE")
            if not pointer_matches:
                if current_sha256 != state["expected_current_sha256"]:
                    self._mark_state(state, "stale", False, "ACTIVATION_CURRENT_STALE")
                    raise ActivationError("ACTIVATION_CURRENT_STALE")
                self._replace_current(intended)
                self._verify_current_target(state["minecraft_version"], target.release_id)
            else:
                self._verify_current_target(state["minecraft_version"], target.release_id)
            if self.after_current_replace is not None and not pointer_matches:
                self.after_current_replace()

            self._complete_workspace_transition(state, set_as_default=set_as_default)
            applied = dict(state)
            applied["status"] = "applied"
            applied["can_apply"] = False
            applied["updated_at"] = utc_now()
            applied["error_code"] = None
            self._write_state(applied)
            return applied
        finally:
            CURRENT_SWITCH_LOCK.release()

    def _completed_transition_proof(self, state: Mapping[str, Any], *, set_as_default: bool) -> bool:
        workspace = self.data_root.workspace_dir(state["minecraft_version"], state["run_id"])
        try:
            database = WorkspaceDatabase.open(
                workspace / "work.sqlite3",
                force_normalized_like=self.force_normalized_like,
                read_only=True,
            )
        except DatabaseSchemaMismatch as exc:
            raise ActivationError("DATABASE_SCHEMA_MISMATCH") from exc
        try:
            with database.read_transaction() as connection:
                run = connection.execute("SELECT status,current_stage,boundary_event,minecraft_version FROM runs WHERE run_id=?", (state["run_id"],)).fetchone()
                if run is None or str(run["minecraft_version"]) != state["minecraft_version"]:
                    raise ActivationError("RUN_NOT_FOUND")
                if not (run["status"] == "succeeded" and run["current_stage"] == "ACTIVATE_RELEASE" and run["boundary_event"] is None):
                    return False
                stage = connection.execute("SELECT status,cursor_json FROM stage_runs WHERE run_id=? AND stage='ACTIVATE_RELEASE'", (state["run_id"],)).fetchone()
                if stage is None or stage["status"] != "succeeded":
                    raise ActivationError("ACTIVATION_RUN_STATE_INVALID")
                try:
                    cursor = json.loads(stage["cursor_json"] or "")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise ActivationError("ACTIVATION_RUN_STATE_INVALID") from exc
                expected_cursor = {"activation_check_id": state["activation_check_id"], "release_id": state["target_release_id"], "completed": True}
                if cursor != expected_cursor:
                    raise ActivationError("ACTIVATION_RUN_STATE_INVALID")

                expected_details = {
                    "activation_check_id": state["activation_check_id"],
                    "minecraft_version": state["minecraft_version"],
                    "target_release_id": state["target_release_id"],
                    "set_as_default": set_as_default,
                }
                matching = 0
                for row in connection.execute("SELECT details_json FROM audit_events WHERE run_id=? AND event_type='CURRENT_SWITCHED'", (state["run_id"],)):
                    try:
                        details = json.loads(row["details_json"] or "")
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ActivationError("ACTIVATION_AUDIT_INTEGRITY_FAILED") from exc
                    if not isinstance(details, dict):
                        raise ActivationError("ACTIVATION_AUDIT_INTEGRITY_FAILED")
                    if details.get("activation_check_id") == state["activation_check_id"]:
                        if details != expected_details:
                            raise ActivationError("ACTIVATION_AUDIT_INTEGRITY_FAILED")
                        matching += 1
                if matching != 1:
                    raise ActivationError("ACTIVATION_AUDIT_INTEGRITY_FAILED")
                return True
        finally:
            database.close()

    def _collect_candidates(self, run_id: str, minecraft_version: str, *, completed_retry: bool = False) -> list[Candidate]:
        workspace = self.data_root.workspace_dir(minecraft_version, run_id)
        database_path = workspace / "work.sqlite3"
        try:
            database = WorkspaceDatabase.open(
                database_path,
                force_normalized_like=self.force_normalized_like,
                read_only=True,
            )
        except DatabaseSchemaMismatch as exc:
            raise ActivationError("DATABASE_SCHEMA_MISMATCH") from exc
        try:
            with database.read_transaction() as connection:
                run_row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
                if run_row is None or str(run_row["minecraft_version"]) != minecraft_version:
                    raise ActivationError("RUN_NOT_FOUND")
                if not completed_retry:
                    try:
                        repeat = self.release_builder._require_check_precondition(connection, run_id)
                    except ReleaseBuildFailure as exc:
                        raise ActivationError("ACTIVATION_RUN_STATE_INVALID") from exc
                    if repeat is not True:
                        raise ActivationError("ACTIVATION_RUN_STATE_INVALID")
                snapshot = self.release_builder._snapshot(connection, workspace, run_row, minecraft_version)
        finally:
            database.close()

        cache_root = self.data_root.cache / "release-checks"
        if not cache_root.is_dir():
            raise ActivationError("ACTIVATION_CANDIDATES_INSUFFICIENT")
        candidates: list[Candidate] = []
        for entry in sorted(cache_root.iterdir(), key=lambda item: item.name.encode("utf-8")):
            if RELEASE_CHECK_ID_RE.fullmatch(entry.name) is None:
                continue
            try:
                checked = self.release_builder.get_check_state(entry.name)
            except (ReleaseCheckNotFound, ReleaseBuildFailure):
                continue
            state = checked.value
            report = checked.report
            if (
                state.get("status") != "built"
                or state.get("can_build") is not True
                or state.get("run_id") != run_id
                or state.get("minecraft_version") != minecraft_version
                or report.get("status") != "buildable"
                or report.get("can_build") is not True
                or report.get("run_id") != run_id
                or report.get("minecraft_version") != minecraft_version
                or report.get("release_build_id") != state.get("release_build_id")
                or state.get("snapshot_fingerprint") != snapshot.fingerprint
            ):
                continue
            check_id = str(state["check_id"])
            release_build_id = state.get("release_build_id")
            release_id = state.get("release_id")
            if not isinstance(release_build_id, str) or RELEASE_BUILD_ID_RE.fullmatch(release_build_id) is None or not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None:
                continue
            if _release_id_for_build_id(release_build_id) != release_id:
                continue
            release_path = self.data_root.release_dir(minecraft_version, release_id)
            try:
                self.release_builder._validate_final(
                    release_path,
                    release_id,
                    snapshot,
                    state,
                    root_for_components=self.data_root.root,
                )
                checksums_sha256 = _hash_file(release_path / "checksums.sha256", release_path)
            except (ReleaseBuildFailure, OSError, sqlite3.Error):
                continue
            if any(item.release_id == release_id or item.release_build_id == release_build_id for item in candidates):
                raise ActivationError("ACTIVATION_CANDIDATE_LINEAGE_INVALID")
            candidates.append(Candidate(check_id, release_build_id, release_id, checksums_sha256, release_path, dict(state), dict(report)))
        candidates.sort(key=lambda item: item.release_id.encode("utf-8"))
        if len(candidates) < 2 or len({item.check_id for item in candidates}) != len(candidates) or len({item.release_build_id for item in candidates}) != len(candidates) or len({item.release_id for item in candidates}) != len(candidates):
            raise ActivationError("ACTIVATION_CANDIDATES_INSUFFICIENT")
        return candidates

    def _mcp_smoke(self, minecraft_version: str, target: Candidate) -> None:
        block_ids = self._smoke_block_ids(target.release_path)
        if len(block_ids) != 2:
            raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED")
        manifest = self._release_manifest(target.release_path)
        temp_root = Path(tempfile.mkdtemp(prefix="blockpedia-activation-"))
        try:
            target_path = temp_root / "releases" / minecraft_version / target.release_id
            self._copy_release_tree(target.release_path, target_path)
            current_path = temp_root / "current.json"
            current_path.parent.mkdir(parents=True, exist_ok=True)
            pointer = {
                "schema_version": "current-pointer.v1",
                "versions": {
                    minecraft_version: {
                        "release_id": target.release_id,
                        "minecraft_version": minecraft_version,
                        "relative_path": f"releases/{minecraft_version}/{target.release_id}",
                        "manifest_sha256": manifest["manifest_sha256"],
                    }
                },
                "default_minecraft_version": minecraft_version,
                "updated_at": utc_now(),
            }
            self._validate_pointer(pointer)
            current_path.write_bytes((canonical_json(pointer) + "\n").encode("utf-8"))
            messages = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "blockpedia-activation", "version": "1"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "index_info", "arguments": {}}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_blocks", "arguments": {"query": "stone", "context": {"rerank": "local_only"}}}},
                {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_block_details", "arguments": {"block_id": block_ids[0]}}},
                {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "compare_blocks", "arguments": {"block_ids": block_ids}}},
            ]
            try:
                responses = asyncio.run(self._mcp_session(temp_root, messages))
            except ActivationError:
                raise
            except Exception as exc:
                raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED") from exc
            if len(responses) != 6 or any(not isinstance(item, dict) or item.get("jsonrpc") != "2.0" for item in responses):
                raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED")
            if "error" in responses[0] or "error" in responses[1] or "error" in responses[2]:
                raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED")
            tools = responses[1].get("result", {}).get("tools", [])
            if [tool.get("name") for tool in tools] != ["index_info", "search_blocks", "get_block_details", "compare_blocks"]:
                raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED")
            for response in responses[2:]:
                result = response.get("result")
                if not isinstance(result, dict) or result.get("isError") is True or not isinstance(result.get("structuredContent"), dict):
                    raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED")
        finally:
            self._remove_tree(temp_root)

    async def _mcp_session(self, data_root: Path, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "blockpedia",
            "mcp",
            "--data-root",
            str(data_root),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            limit=MCP_STDIO_LINE_LIMIT,
        )
        assert process.stdin is not None and process.stdout is not None and process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        responses: list[dict[str, Any]] = []
        try:
            for message in messages:
                process.stdin.write((json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
                await asyncio.wait_for(process.stdin.drain(), timeout=10)
                if "id" not in message:
                    continue
                line = await asyncio.wait_for(process.stdout.readline(), timeout=60)
                if not line:
                    raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED")
                try:
                    response = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED") from exc
                if not isinstance(response, dict) or response.get("jsonrpc") != "2.0" or response.get("id") != message["id"]:
                    raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED")
                responses.append(response)
            process.stdin.close()
            await asyncio.wait_for(process.stdin.wait_closed(), timeout=10)
            remaining = await asyncio.wait_for(process.stdout.read(), timeout=60)
            returncode = await asyncio.wait_for(process.wait(), timeout=10)
            await asyncio.wait_for(stderr_task, timeout=10)
            if remaining.strip() or returncode != 0:
                raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED")
            return responses
        except Exception:
            if process.returncode is None:
                process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except Exception:
                pass
            if not stderr_task.done():
                stderr_task.cancel()
            raise

    def _smoke_block_ids(self, release_path: Path) -> list[str]:
        connection = sqlite3.connect(release_path / "index.sqlite3")
        try:
            rows = connection.execute("SELECT DISTINCT block_id FROM visual_variants ORDER BY block_id LIMIT 2").fetchall()
        except sqlite3.Error as exc:
            raise ActivationError("ACTIVATION_MCP_SMOKE_FAILED") from exc
        finally:
            connection.close()
        return [str(row[0]) for row in rows]

    def _copy_release_tree(self, source: Path, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False)
        for path in sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix().encode("utf-8")):
            relative = path.relative_to(source)
            target = destination / relative
            try:
                stat_value = _mcp_lstat(path, directory=None)
            except MCPReleaseError as exc:
                raise ActivationError("ACTIVATION_RELEASE_INTEGRITY_FAILED") from exc
            if path.is_dir():
                target.mkdir()
                continue
            if not path.is_file() or stat_value.st_nlink != 1:
                raise ActivationError("ACTIVATION_RELEASE_INTEGRITY_FAILED")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())

    def _release_manifest(self, release_path: Path) -> dict[str, Any]:
        try:
            value, _ = _json_file(release_path / "release.json", release_path, component="release")
        except MCPReleaseError as exc:
            raise ActivationError("ACTIVATION_RELEASE_INTEGRITY_FAILED") from exc
        if not isinstance(value, dict) or not isinstance(value.get("manifest_sha256"), str) or not HASH_RE.fullmatch(value["manifest_sha256"]):
            raise ActivationError("ACTIVATION_RELEASE_INTEGRITY_FAILED")
        return value

    def _read_current(self) -> tuple[dict[str, Any] | None, bytes | None]:
        current_path = self.data_root.current
        if not current_path.exists() and not current_path.is_symlink():
            return None, None
        try:
            value, payload = _json_file(current_path, self.data_root.root, component="current_pointer")
        except MCPReleaseError as exc:
            raise ActivationError("CURRENT_POINTER_INVALID") from exc
        self._validate_pointer(value)
        return value, payload

    def _validate_pointer(self, value: Any) -> None:
        try:
            validate_record("current-pointer.v1", value)
        except (RecordSchemaError, TypeError, ValueError) as exc:
            raise ActivationError("CURRENT_POINTER_INVALID") from exc
        versions = value.get("versions")
        if not isinstance(versions, dict) or value.get("default_minecraft_version") not in versions:
            raise ActivationError("CURRENT_POINTER_INVALID")
        for version, pointer in versions.items():
            release_id = pointer.get("release_id")
            if pointer.get("minecraft_version") != version or not isinstance(release_id, str) or RELEASE_ID_RE.fullmatch(release_id) is None or pointer.get("relative_path") != f"releases/{version}/{release_id}":
                raise ActivationError("CURRENT_POINTER_INVALID")

    def _pointer_for_target(
        self,
        current: dict[str, Any] | None,
        minecraft_version: str,
        target: Candidate,
        *,
        manifest: dict[str, Any] | None = None,
        set_as_default: bool = True,
    ) -> dict[str, Any]:
        manifest = manifest or self._release_manifest(target.release_path)
        versions = dict(current.get("versions", {})) if current is not None else {}
        versions[minecraft_version] = {
            "release_id": target.release_id,
            "minecraft_version": minecraft_version,
            "relative_path": f"releases/{minecraft_version}/{target.release_id}",
            "manifest_sha256": manifest["manifest_sha256"],
        }
        default_version = minecraft_version if current is None or set_as_default else current["default_minecraft_version"]
        pointer = {
            "schema_version": "current-pointer.v1",
            "versions": versions,
            "default_minecraft_version": default_version,
            "updated_at": utc_now(),
        }
        self._validate_pointer(pointer)
        return pointer

    @staticmethod
    def _pointer_matches_intent(current: Mapping[str, Any], intended: Mapping[str, Any]) -> bool:
        return {key: value for key, value in current.items() if key != "updated_at"} == {key: value for key, value in intended.items() if key != "updated_at"}

    def _replace_current(self, pointer: dict[str, Any]) -> None:
        operation_id = uuid.uuid4().hex
        temp_path = self.data_root.current.with_name(f"current.json.tmp.{operation_id}")
        payload = (canonical_json(pointer) + "\n").encode("utf-8")
        replaced = False
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            descriptor = os.open(temp_path, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                descriptor = -1
                raise
            if self.before_current_replace is not None:
                self.before_current_replace(temp_path, self.data_root.current)
            os.replace(temp_path, self.data_root.current)
            replaced = True
            _fsync_directory(self.data_root.root)
            current, current_bytes = self._read_current()
            if current is None or current_bytes != payload:
                raise ActivationError("CURRENT_SWITCH_FAILED")
            del current
        except ActivationError:
            if not replaced:
                self._remove_exact_file(temp_path)
            raise
        except Exception as exc:
            if not replaced:
                self._remove_exact_file(temp_path)
            raise ActivationError("CURRENT_SWITCH_FAILED") from exc

    def _verify_current_target(self, minecraft_version: str, target_release_id: str) -> None:
        try:
            with MCPReleaseResolver(self.data_root, repo_root=self.repo_root).resolve(minecraft_version) as handle:
                if handle.release_id != target_release_id:
                    raise ActivationError("CURRENT_SWITCH_FAILED")
        except (MCPReleaseError, ActivationError) as exc:
            if isinstance(exc, ActivationError):
                raise
            raise ActivationError("CURRENT_SWITCH_FAILED") from exc

    def _complete_workspace_transition(self, state: Mapping[str, Any], *, set_as_default: bool) -> None:
        workspace = self.data_root.workspace_dir(state["minecraft_version"], state["run_id"])
        try:
            database = WorkspaceDatabase.open(workspace / "work.sqlite3", force_normalized_like=self.force_normalized_like)
        except DatabaseSchemaMismatch as exc:
            raise ActivationError("DATABASE_SCHEMA_MISMATCH") from exc
        try:
            with database.transaction() as connection:
                run = connection.execute("SELECT status,current_stage,boundary_event FROM runs WHERE run_id=?", (state["run_id"],)).fetchone()
                stage = connection.execute("SELECT status FROM stage_runs WHERE run_id=? AND stage='ACTIVATE_RELEASE'", (state["run_id"],)).fetchone()
                if run is None or stage is None:
                    raise ActivationError("ACTIVATION_RUN_STATE_INVALID")
                details = {
                    "activation_check_id": state["activation_check_id"],
                    "minecraft_version": state["minecraft_version"],
                    "target_release_id": state["target_release_id"],
                    "set_as_default": set_as_default,
                }
                matching = 0
                for row in connection.execute("SELECT details_json FROM audit_events WHERE run_id=? AND event_type='CURRENT_SWITCHED'", (state["run_id"],)):
                    try:
                        existing = json.loads(row["details_json"] or "")
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise ActivationError("ACTIVATION_AUDIT_INTEGRITY_FAILED") from exc
                    if isinstance(existing, dict) and existing.get("activation_check_id") == state["activation_check_id"]:
                        if existing != details:
                            raise ActivationError("ACTIVATION_AUDIT_INTEGRITY_FAILED")
                        matching += 1
                if matching > 1:
                    raise ActivationError("ACTIVATION_AUDIT_INTEGRITY_FAILED")
                if not (run["status"] == "succeeded" and stage["status"] == "succeeded"):
                    if run["status"] != "paused" or run["current_stage"] != "ACTIVATE_RELEASE" or run["boundary_event"] != "R3_CANDIDATE_BUILT_ACTIVATION_PENDING" or stage["status"] != "pending":
                        raise ActivationError("ACTIVATION_RUN_STATE_INVALID")
                    now = utc_now()
                    connection.execute(
                        "UPDATE stage_runs SET status='succeeded',worker_id=NULL,heartbeat_at=NULL,finished_at=?,cursor_json=? WHERE run_id=? AND stage='ACTIVATE_RELEASE' AND status='pending'",
                        (now, canonical_json({"activation_check_id": state["activation_check_id"], "release_id": state["target_release_id"], "completed": True}), state["run_id"]),
                    )
                    connection.execute(
                        "UPDATE runs SET status='succeeded',current_stage='ACTIVATE_RELEASE',boundary_event=NULL,finished_at=? WHERE run_id=? AND status='paused'",
                        (now, state["run_id"]),
                    )
                if matching == 0:
                    connection.execute(
                        "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                        ("audit_" + uuid.uuid4().hex, "CURRENT_SWITCHED", state["run_id"], canonical_json(details), utc_now()),
                    )
        except ActivationError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise ActivationError("ACTIVATION_APPLY_FAILED") from exc
        finally:
            database.close()

    def _make_state(
        self,
        activation_check_id: str,
        run_id: str,
        minecraft_version: str,
        target_release_id: str,
        candidates: list[Candidate],
        expected_current_sha256: str | None,
        created_at: str,
        *,
        status: str,
        can_apply: bool,
        error_code: str | None,
    ) -> dict[str, Any]:
        return {
            "format_version": 1,
            "activation_check_id": activation_check_id,
            "run_id": run_id,
            "minecraft_version": minecraft_version,
            "target_release_id": target_release_id,
            "candidate_releases": self._candidate_entries(candidates),
            "expected_current_sha256": expected_current_sha256,
            "status": status,
            "can_apply": can_apply,
            "created_at": created_at,
            "updated_at": created_at,
            "error_code": error_code,
        }

    @staticmethod
    def _candidate_entries(candidates: list[Candidate]) -> list[dict[str, str]]:
        return [{"release_id": item.release_id, "checksums_sha256": item.checksums_sha256} for item in sorted(candidates, key=lambda item: item.release_id.encode("utf-8"))]

    def _state_path(self, activation_check_id: str) -> Path:
        return self.data_root.cache / "activation-checks" / activation_check_id / "state.json"

    def _write_state(self, state: Mapping[str, Any]) -> None:
        self._validate_state(state)
        try:
            _atomic_json(self._state_path(str(state["activation_check_id"])), state, root=self.data_root.root)
        except (OSError, ReleaseBuildFailure) as exc:
            raise ActivationError("ACTIVATION_STATE_WRITE_FAILED") from exc

    def _read_state(self, activation_check_id: str) -> dict[str, Any]:
        path = self._state_path(activation_check_id)
        try:
            value, _ = _json_file(path, self.data_root.root, component="activation_state")
        except MCPReleaseError as exc:
            raise ActivationError("ACTIVATION_CHECK_NOT_FOUND") from exc
        self._validate_state(value, activation_check_id)
        return dict(value)

    def _latest_state(self, minecraft_version: str) -> dict[str, Any]:
        root = self.data_root.cache / "activation-checks"
        states: list[dict[str, Any]] = []
        if root.is_dir():
            for entry in sorted(root.iterdir(), key=lambda item: item.name.encode("utf-8")):
                if ACTIVATION_CHECK_ID_RE.fullmatch(entry.name) is None:
                    continue
                try:
                    value = self._read_state(entry.name)
                except ActivationError:
                    continue
                if value["minecraft_version"] == minecraft_version:
                    states.append(value)
        if not states:
            raise ActivationError("ACTIVATION_CHECK_NOT_FOUND")
        return max(states, key=lambda item: (item["created_at"].encode("utf-8"), item["activation_check_id"].encode("utf-8")))

    def _mark_state(self, state: Mapping[str, Any], status: str, can_apply: bool, error_code: str) -> None:
        updated = dict(state)
        updated["status"] = status
        updated["can_apply"] = can_apply
        updated["updated_at"] = utc_now()
        updated["error_code"] = error_code
        self._write_state(updated)

    @staticmethod
    def _validate_state(value: Any, activation_check_id: str | None = None) -> None:
        if not isinstance(value, dict) or set(value) != ACTIVATION_STATE_FIELDS:
            raise ActivationError("ACTIVATION_STATE_INVALID")
        if value.get("format_version") != 1 or not isinstance(value.get("activation_check_id"), str) or ACTIVATION_CHECK_ID_RE.fullmatch(value["activation_check_id"]) is None or (activation_check_id is not None and value["activation_check_id"] != activation_check_id):
            raise ActivationError("ACTIVATION_STATE_INVALID")
        if not isinstance(value.get("run_id"), str) or not value["run_id"].startswith("run_") or not isinstance(value.get("minecraft_version"), str):
            raise ActivationError("ACTIVATION_STATE_INVALID")
        if not isinstance(value.get("target_release_id"), str) or RELEASE_ID_RE.fullmatch(value["target_release_id"]) is None:
            raise ActivationError("ACTIVATION_STATE_INVALID")
        candidates = value.get("candidate_releases")
        if not isinstance(candidates, list):
            raise ActivationError("ACTIVATION_STATE_INVALID")
        previous = ""
        for candidate in candidates:
            if not isinstance(candidate, dict) or set(candidate) != {"release_id", "checksums_sha256"} or RELEASE_ID_RE.fullmatch(str(candidate.get("release_id"))) is None or not HASH_RE.fullmatch(str(candidate.get("checksums_sha256"))):
                raise ActivationError("ACTIVATION_STATE_INVALID")
            if str(candidate["release_id"]) <= previous:
                raise ActivationError("ACTIVATION_STATE_INVALID")
            previous = str(candidate["release_id"])
        expected = value.get("expected_current_sha256")
        if expected is not None and not HASH_RE.fullmatch(str(expected)):
            raise ActivationError("ACTIVATION_STATE_INVALID")
        if value.get("status") not in ACTIVATION_STATUSES or not isinstance(value.get("can_apply"), bool) or value.get("status") == "passed" and value["can_apply"] is not True or value.get("status") != "passed" and value["can_apply"] is not False:
            raise ActivationError("ACTIVATION_STATE_INVALID")
        if not all(isinstance(value.get(key), str) and TIMESTAMP_RE.fullmatch(value[key]) for key in ("created_at", "updated_at")):
            raise ActivationError("ACTIVATION_STATE_INVALID")
        if value.get("error_code") is not None and (not isinstance(value["error_code"], str) or ERROR_CODE_RE.fullmatch(value["error_code"]) is None):
            raise ActivationError("ACTIVATION_STATE_INVALID")

    @staticmethod
    def _remove_exact_file(path: Path) -> None:
        try:
            if path.is_symlink() or path.is_file():
                path.unlink()
        except OSError:
            pass

    @classmethod
    def _remove_tree(cls, path: Path) -> None:
        def onerror(function: Any, target: str, _exc_info: Any) -> None:
            try:
                os.chmod(target, 0o700)
                function(target)
            except OSError:
                pass

        shutil.rmtree(path, onerror=onerror, ignore_errors=True)


__all__ = ["ActivationError", "ActivationService", "ACTIVATION_CHECK_ID_RE"]
