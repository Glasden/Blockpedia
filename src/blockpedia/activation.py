"""Publish or roll back a local release with one atomic pointer replacement."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
from pathlib import Path

from .local_files import safe_path, sync_directory, replace_file
from .paths import DataRoot, validate_minecraft_version
from .releases import inspect_release, ReleaseBuildFailure
from .r3 import canonical_json, is_sensitive_review_text
from .schema import validate_record, RecordSchemaError
from .storage import utc_now

CURRENT_SWITCH_LOCK = threading.Lock()


class ActivationError(RuntimeError):
    def __init__(self, code, message="publication failed"):
        self.code = code
        super().__init__(message)


def _bytes(value):
    return (canonical_json(value) + "\n").encode("utf-8")


def _hash(value):
    return "sha256:" + hashlib.sha256(value).hexdigest()


class ActivationService:
    def __init__(self, data_root, *, repo_root=None, force_normalized_like=False):
        self.data_root = data_root
        self.repo_root = repo_root or Path(__file__).resolve().parents[2]
        self.before_current_replace = None
        self.after_current_replace = None

    def current(self):
        path = self.data_root.current
        if not path.exists() and not path.is_symlink():
            return None, None
        try:
            raw = safe_path(path, self.data_root.root, directory=False).read_bytes()
            pointer = json.loads(raw)
            validate_record("current-pointer.v1", pointer, repo_root=self.repo_root)
            if pointer["default_minecraft_version"] not in pointer["versions"]:
                raise ValueError("missing default version")
            return pointer, _hash(raw)
        except (OSError, ValueError, RecordSchemaError) as exc:
            raise ActivationError("CURRENT_UNREADABLE") from exc

    @property
    def audit_path(self):
        return self.data_root.logs / "release-events.jsonl"

    def _append_audit(self, value):
        path = self.audit_path
        safe_path(path, self.data_root.root, missing=True)
        with path.open("ab") as handle:
            handle.write(_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        sync_directory(path.parent)
        if value.get("event") == "intent":
            sync_directory(self.data_root.root)

    def audit_pending(self):
        """Observe incomplete publication bookkeeping without changing it."""
        if not self.audit_path.exists():
            return False
        try:
            raw = safe_path(self.audit_path, self.data_root.root, directory=False).read_bytes()
            pending = set()
            for line in raw.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    return True
                record = json.loads(line)
                if record["event"] == "intent":
                    pending.add(record["operation_id"])
                else:
                    pending.discard(record["operation_id"])
            return bool(pending)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ActivationError("PUBLISH_AUDIT_UNREADABLE") from exc

    def _recover_audit(self, current):
        path = self.audit_path
        if not path.exists():
            return
        raw = safe_path(path, self.data_root.root, directory=False).read_bytes()
        # A crash may leave an incomplete last write; only complete records committed.
        complete = raw.rfind(b"\n") + 1
        if complete != len(raw):
            with path.open("r+b") as handle:
                handle.truncate(complete)
                handle.flush()
                os.fsync(handle.fileno())
        pending = {}
        try:
            for line in raw[:complete].splitlines():
                record = json.loads(line)
                operation = record["operation_id"]
                if record["event"] == "intent":
                    pending[operation] = record
                else:
                    pending.pop(operation, None)
        except (ValueError, KeyError, TypeError) as exc:
            raise ActivationError("PUBLISH_AUDIT_UNREADABLE") from exc
        for operation, record in pending.items():
            if current == record["new"]:
                outcome = "applied"
            elif current == record["old"]:
                outcome = "not_applied"
            else:
                raise ActivationError("PUBLISH_RECOVERY_REQUIRED")
            self._append_audit({"operation_id": operation, "event": outcome, "recovered": True, "at": utc_now()})

    def publish(self, minecraft_version, target_release_id, expected_current_sha256, *, confirm, set_as_default, reviewer, reason, rollback=False):
        if confirm is not True or not isinstance(set_as_default, bool):
            raise ActivationError("PUBLISH_CONFIRMATION_REQUIRED")
        if any(not isinstance(value, str) or not value.strip() or len(value) > 500 or is_sensitive_review_text(value) for value in (reviewer, reason)):
            raise ActivationError("PUBLISH_REASON_INVALID")
        validate_minecraft_version(minecraft_version)
        if not CURRENT_SWITCH_LOCK.acquire(blocking=False):
            raise ActivationError("CURRENT_SWITCH_BUSY")
        temporary = None
        applied = False
        try:
            current, token = self.current()
            self._recover_audit(current)
            existing = (current or {}).get("versions", {}).get(minecraft_version, {})
            already_applied = existing.get("release_id") == target_release_id and (not set_as_default or current["default_minecraft_version"] == minecraft_version)
            if token != expected_current_sha256 and not already_applied:
                raise ActivationError("CURRENT_CONFLICT")
            if current is None and not set_as_default:
                raise ActivationError("DEFAULT_VERSION_REQUIRED")
            info = inspect_release(self.data_root, minecraft_version, target_release_id, repo_root=self.repo_root)
            if already_applied:
                return self._result(current, token, minecraft_version, target_release_id, rollback, [])
            versions = dict((current or {}).get("versions", {}))
            versions[minecraft_version] = {"release_id": target_release_id, "minecraft_version": minecraft_version, "relative_path": self.data_root.relative_ref(info["path"]), "manifest_sha256": info["release"]["manifest_sha256"]}
            pointer = {"schema_version": "current-pointer.v1", "versions": versions, "default_minecraft_version": minecraft_version if set_as_default else current["default_minecraft_version"], "updated_at": utc_now()}
            validate_record("current-pointer.v1", pointer, repo_root=self.repo_root)
            operation = uuid.uuid4().hex
            self._append_audit({"operation_id": operation, "event": "intent", "action": "rollback" if rollback else "publish", "reviewer": reviewer, "reason": reason, "old": current, "new": pointer, "at": utc_now()})
            temporary = self.data_root.current.with_name("current.json.tmp." + operation)
            with temporary.open("xb") as handle:
                handle.write(_bytes(pointer))
                handle.flush()
                os.fsync(handle.fileno())
            if self.before_current_replace is not None:
                self.before_current_replace(temporary, self.data_root.current)
            replace_file(temporary, self.data_root.current)
            applied = True
            warnings = []
            try:
                sync_directory(self.data_root.root)
                actual, actual_token = self.current()
                if actual != pointer:
                    raise ActivationError("CURRENT_SWITCH_UNCERTAIN")
            except Exception:
                warnings.append("PUBLISH_FINALIZE_PENDING")
            try:
                if self.after_current_replace is not None:
                    self.after_current_replace()
                self._append_audit({"operation_id": operation, "event": "applied", "at": utc_now()})
            except Exception:
                warnings.append("PUBLISH_AUDIT_PENDING")
            return self._result(pointer, _hash(_bytes(pointer)), minecraft_version, target_release_id, rollback, warnings)
        except ActivationError:
            raise
        except (ReleaseBuildFailure, RecordSchemaError, OSError, ValueError, sqlite3.Error) as exc:
            raise ActivationError(exc.code if isinstance(exc, ReleaseBuildFailure) else "PUBLISH_FAILED") from exc
        finally:
            try:
                if not applied and temporary is not None and temporary.exists():
                    try:
                        safe_path(temporary, self.data_root.root, directory=False).unlink()
                    except (OSError, ValueError):
                        pass  # Keep the original failure; an orphan cannot hold the switch lock.
            finally:
                CURRENT_SWITCH_LOCK.release()

    @staticmethod
    def _result(pointer, token, version, target, rollback, warnings):
        return {"applied": True, "minecraft_version": version, "target_release_id": target, "current": pointer, "current_sha256": token, "status": "rolled_back" if rollback else "published", "warnings": warnings}
