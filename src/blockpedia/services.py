"""Public R2 application services for the Phase 2 adapters."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .directory_chooser import DirectoryChooser
from .importer import ImportCheck, ImportCheckInProgress, ImportCheckStart, ImportService
from .paths import DataRoot, resolve_data_root
from .search import WorkspaceQueryService, human_semantics_complete
from .stages import R3_BUILD_RELEASE_BOUNDARY_EVENT, R3_BOUNDARY_EVENT, R3_CANDIDATE_BUILT_BOUNDARY_EVENT, RunStateConflict, STUDIO_STAGES, require_transition
from .storage import DatabaseSchemaMismatch, WorkspaceDatabase, utc_now
from .worker import (
    FATAL_PROVIDER_ERROR_CODES,
    ITEM_LOCAL_PROVIDER_ERROR_CODES,
    StageFailure,
    StaleMarker,
    WorkerService,
    build_retry_wave_hash,
    normalize_provider_error_code,
)
from .run_snapshots import RunSnapshotService
from .provider import (
    OpenAIProvider,
    ProbeResult,
    ProviderError,
    ProviderProfile,
    ProviderProfileError,
    ProviderProfileStore,
    SecretResolver,
    sanitize_validation_diagnostic,
)
from .schema import RecordSchemaError, validate_record
from .r3 import (
    canonical_json,
    encode_rgba_png,
    is_sensitive_review_text,
    make_contact_sheet,
    safe_machine_metadata,
    safe_prompt,
    sha256_bytes,
    sha256_json,
)
from .releases import ReleaseBuildFailure, ReleaseBuilder, ReleaseCheckNotFound


class R3Error(RuntimeError):
    def __init__(self, code: str, message: str = "R3 operation is not allowed") -> None:
        self.code = code
        super().__init__(message)


class StudioService:
    """Coordinates import, run controls, worker recovery and workspace search."""

    def __init__(
        self,
        data_root: DataRoot | str | Path | None = None,
        *,
        repo_root: Path | None = None,
        force_normalized_like: bool = False,
        toolchain_probe: Any | None = None,
        provider_factory: Any | None = None,
        probe_factory: Any | None = None,
        secret_resolver: SecretResolver | None = None,
        release_pre_rename_hook: Any | None = None,
    ):
        self.data_root = data_root if isinstance(data_root, DataRoot) else resolve_data_root(data_root)
        self.data_root.ensure_layout()
        self.profile_store = ProviderProfileStore(path=self.data_root.cache / "provider-profiles.json")
        self.secret_resolver = secret_resolver or SecretResolver()
        self.provider_factory = provider_factory
        self.probe_factory = probe_factory
        self.directory_chooser = DirectoryChooser(self.data_root)
        self.imports = ImportService(
            self.data_root,
            repo_root=repo_root,
            force_normalized_like=force_normalized_like,
            chooser=self.directory_chooser,
        )
        self.worker = WorkerService(
            self.data_root,
            repo_root=repo_root,
            force_normalized_like=force_normalized_like,
            toolchain_probe=toolchain_probe,
            provider_factory=provider_factory,
            profile_store=self.profile_store,
            secret_resolver=self.secret_resolver,
        )
        self.run_snapshots = RunSnapshotService(self.data_root, stale_after_seconds=self.worker.stale_after_seconds)
        self.release_builder = ReleaseBuilder(
            self.data_root,
            repo_root=self.worker.repo_root,
            force_normalized_like=force_normalized_like,
            pre_rename_hook=release_pre_rename_hook,
        )
        self._close_lock = threading.RLock()
        self._closed = False

    def close(self) -> bool:
        with self._close_lock:
            if self._closed:
                return False
            self._closed = True
            self.worker.close()
            self.imports.close()
            return True

    # ---- R3 Phase C candidate check/build --------------------------------------

    def check_candidate_release(self, run_id: str, minecraft_version: str) -> dict[str, Any]:
        """Synchronously execute Gate C and persist its immutable check cache."""

        try:
            with self.worker.run_lock(run_id):
                try:
                    actual_version = self.worker._find_run_version(run_id)
                except KeyError as exc:
                    raise R3Error("RUN_NOT_FOUND") from exc
                except DatabaseSchemaMismatch as exc:
                    raise R3Error("DATABASE_SCHEMA_MISMATCH") from exc
                if actual_version != minecraft_version:
                    raise R3Error("RELEASE_VERSION_MISMATCH")
                return self.release_builder.check(run_id, minecraft_version)
        except R3Error:
            raise
        except ReleaseBuildFailure as exc:
            code = exc.code if exc.code in {"RUN_NOT_FOUND", "RELEASE_VERSION_MISMATCH", "RELEASE_CHECK_NOT_READY", "DATABASE_SCHEMA_MISMATCH"} else "RELEASE_CHECK_FAILED"
            raise R3Error(code) from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise R3Error("RUN_NOT_FOUND") from exc

    def build_candidate_release(self, check_id: str, confirm_immutable_release: bool = True) -> dict[str, Any]:
        """Build exactly one immutable, not-yet-activated candidate."""

        if confirm_immutable_release is not True:
            raise R3Error("INVALID_INPUT")
        try:
            checked = self.release_builder.get_check_state(check_id)
        except ReleaseCheckNotFound as exc:
            raise R3Error("RELEASE_CHECK_NOT_FOUND") from exc
        except ReleaseBuildFailure as exc:
            raise R3Error(exc.code) from exc
        run_id = str(checked.value["run_id"])
        minecraft_version = str(checked.value["minecraft_version"])
        try:
            with self.worker.run_lock(run_id):
                result = self.release_builder.build(check_id)
                with WorkspaceDatabase.open(
                    self.data_root.workspace_dir(minecraft_version, run_id) / "work.sqlite3",
                    force_normalized_like=True,
                ) as database:
                    self._reconcile_candidate_workspace(database, run_id, result)
                self.release_builder._mark_built(checked.value, result["release_id"])
                return result
        except R3Error:
            raise
        except ReleaseCheckNotFound as exc:
            raise R3Error("RELEASE_CHECK_NOT_FOUND") from exc
        except ReleaseBuildFailure as exc:
            raise R3Error(exc.code) from exc
        except DatabaseSchemaMismatch as exc:
            raise R3Error("DATABASE_SCHEMA_MISMATCH") from exc
        except (sqlite3.Error, OSError) as exc:
            raise R3Error("RELEASE_BUILD_FAILED") from exc
        except (KeyError, ValueError, TypeError) as exc:
            raise R3Error("RELEASE_BUILD_FAILED") from exc
        except Exception as exc:
            raise R3Error("RELEASE_BUILD_FAILED") from exc

    def _reconcile_candidate_workspace(self, database: WorkspaceDatabase, run_id: str, result: dict[str, Any]) -> None:
        with database.transaction() as connection:
            run = connection.execute("SELECT status,current_stage,boundary_event FROM runs WHERE run_id=?", (run_id,)).fetchone()
            build_stage = connection.execute("SELECT status FROM stage_runs WHERE run_id=? AND stage='BUILD_RELEASE'", (run_id,)).fetchone()
            activate_stage = connection.execute("SELECT status FROM stage_runs WHERE run_id=? AND stage='ACTIVATE_RELEASE'", (run_id,)).fetchone()
            if run is None or build_stage is None or activate_stage is None:
                raise R3Error("RELEASE_BUILD_FAILED")
            now = result["built_at"]
            cursor = canonical_json({"release_id": result["release_id"], "release_build_id": result["release_build_id"], "completed": True})
            if run["boundary_event"] == R3_CANDIDATE_BUILT_BOUNDARY_EVENT:
                if run["status"] != "paused" or run["current_stage"] != "ACTIVATE_RELEASE" or build_stage["status"] != "succeeded" or activate_stage["status"] != "pending":
                    raise R3Error("RELEASE_BUILD_FAILED")
                existing_cursor = connection.execute("SELECT cursor_json FROM stage_runs WHERE run_id=? AND stage='BUILD_RELEASE'", (run_id,)).fetchone()
                if existing_cursor is None or json.loads(existing_cursor["cursor_json"] or "{}").get("release_id") != result["release_id"]:
                    raise R3Error("RELEASE_BUILD_INTEGRITY_FAILED")
            elif run["status"] == "paused" and run["current_stage"] == "BUILD_RELEASE" and build_stage["status"] == "pending" and activate_stage["status"] == "pending":
                connection.execute("UPDATE stage_runs SET status='succeeded',worker_id=NULL,heartbeat_at=?,finished_at=?,cursor_json=? WHERE run_id=? AND stage='BUILD_RELEASE' AND status='pending'", (now, now, cursor, run_id))
                connection.execute("UPDATE runs SET status='paused',current_stage='ACTIVATE_RELEASE',boundary_event=?,finished_at=NULL WHERE run_id=? AND status='paused' AND current_stage='BUILD_RELEASE'", (R3_CANDIDATE_BUILT_BOUNDARY_EVENT, run_id))
                connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), R3_CANDIDATE_BUILT_BOUNDARY_EVENT, run_id, canonical_json({"release_id": result["release_id"], "release_build_id": result["release_build_id"]}), now))
            else:
                raise R3Error("RELEASE_BUILD_FAILED")

    # ---- Provider/profile application service ---------------------------------

    def list_provider_profiles(self) -> list[dict[str, Any]]:
        return [self._public_profile(profile) for profile in self.profile_store.load().values()]

    list_profiles = list_provider_profiles

    def get_provider_profile(self, profile_id: str) -> dict[str, Any]:
        profile = self.profile_store.load().get(profile_id)
        if profile is None:
            raise R3Error("PROVIDER_PROFILE_NOT_FOUND")
        return self._public_profile(profile)

    get_profile = get_provider_profile

    def save_provider_profile(self, profile: ProviderProfile | dict[str, Any]) -> dict[str, Any]:
        if isinstance(profile, dict) and any(key in profile for key in ("api_key", "authorization", "key")):
            raise R3Error("PROVIDER_CONFIG_INVALID")
        try:
            normalized = profile if isinstance(profile, ProviderProfile) else ProviderProfile.from_dict(profile)
            # A configuration write is never an enable operation.  Even an
            # unchanged profile must be explicitly re-probed before it can
            # become active again.
            normalized = normalized.with_capability("unverified", enabled=False)
            self.profile_store.save(normalized)
        except (ProviderProfileError, ProviderError, TypeError, ValueError) as exc:
            raise R3Error("PROVIDER_CONFIG_INVALID") from exc
        return self._public_profile(self.profile_store.load()[normalized.profile_id])

    save_profile = save_provider_profile

    def provider_secret_status(self, profile_id: str) -> dict[str, Any]:
        profile = self.profile_store.load().get(profile_id)
        if profile is None:
            raise R3Error("PROVIDER_PROFILE_NOT_FOUND")
        info = self.secret_resolver.resolve(profile)
        return {"configured": info.configured, "source": info.source, "masked": info.masked}

    secret_status = provider_secret_status

    def probe_provider(self, profile_id: str) -> dict[str, Any]:
        profile = self.profile_store.load().get(profile_id)
        if profile is None:
            raise R3Error("PROVIDER_PROFILE_NOT_FOUND")
        provider = self._new_provider(profile, probe=True)
        try:
            if self.probe_factory is not None:
                result = self._call_factory(self.probe_factory, profile, provider=provider)
                if isinstance(result, ProbeResult):
                    probe_result = result
                elif isinstance(result, dict):
                    probe_result = ProbeResult(**result)
                else:
                    probe_result = provider.probe(_probe_png())
            else:
                probe_result = provider.probe(_probe_png())
            if not isinstance(probe_result, ProbeResult):
                raise R3Error("PROVIDER_CAPABILITY_MISSING")
            saved = self.profile_store.record_probe(probe_result)
            return {"profile": self._public_profile(saved), "capabilities": self._public_capabilities(profile_id)}
        except R3Error:
            raise
        except (ProviderError, ProviderProfileError, TypeError, ValueError) as exc:
            raise R3Error("PROVIDER_CAPABILITY_MISSING") from exc
        finally:
            self._close_provider(provider)

    probe = probe_provider
    probe_profile = probe_provider

    def enable_provider(self, profile_id: str) -> dict[str, Any]:
        profile = self.profile_store.load().get(profile_id)
        if profile is None:
            raise R3Error("PROVIDER_PROFILE_NOT_FOUND")
        if not self.secret_resolver.resolve(profile).configured:
            raise R3Error("PROVIDER_NOT_CONFIGURED")
        try:
            enabled = self.profile_store.enable(profile_id)
        except ProviderError as exc:
            raise R3Error("PROVIDER_CAPABILITY_MISSING") from exc
        return self._public_profile(enabled)

    enable = enable_provider
    enable_profile = enable_provider

    def disable_provider(self, profile_id: str) -> dict[str, Any]:
        try:
            return self._public_profile(self.profile_store.disable(profile_id))
        except ProviderProfileError as exc:
            raise R3Error("PROVIDER_PROFILE_NOT_FOUND") from exc

    disable = disable_provider
    disable_profile = disable_provider

    @staticmethod
    def _public_profile(profile: ProviderProfile) -> dict[str, Any]:
        value = profile.to_dict()
        value.pop("api_key", None)
        value.pop("authorization", None)
        return value

    def _public_capabilities(self, profile_id: str) -> dict[str, Any]:
        capabilities = self.profile_store.capabilities(profile_id) or {}
        return {
            key: capabilities.get(key)
            for key in (
                "adapter",
                "image_input_supported",
                "structured_outputs_supported",
                "error_classification_supported",
                "capability_status",
                "base_url_stable_id",
                "probed_at",
                "request_id_redacted",
                "error_code",
            )
            if key in capabilities
        }

    def _new_provider(self, profile: ProviderProfile, *, probe: bool = False) -> Any:
        if self.provider_factory is not None:
            return self._call_factory(
                self.provider_factory,
                profile,
                profile_store=self.profile_store,
                repo_root=self.worker.repo_root,
                secret_resolver=self.secret_resolver,
                probe=probe,
            )
        return OpenAIProvider(
            profile,
            profile_store=self.profile_store,
            repo_root=self.worker.repo_root,
            secret_resolver=self.secret_resolver,
        )

    @staticmethod
    def _call_factory(factory: Any, profile: ProviderProfile, **kwargs: Any) -> Any:
        try:
            return factory(profile, **kwargs)
        except TypeError:
            return factory(profile)

    @staticmethod
    def _close_provider(provider: Any) -> None:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    # ---- R3 run configuration --------------------------------------------------

    def configure_run(
        self,
        import_id: str,
        minecraft_version: str = "26.2",
        *,
        profile_id: str | None = None,
        version: str | None = None,
        enabled_profile_id: str | None = None,
        batch_size: int | None = None,
        normal_threshold: float = 0.80,
        high_threshold: float = 0.65,
        sample_rate: int = 100,
        deterministic_sample_rate: int | None = None,
    ) -> dict[str, Any]:
        if version is not None:
            minecraft_version = version
        if enabled_profile_id is not None:
            profile_id = enabled_profile_id
        if deterministic_sample_rate is not None:
            sample_rate = deterministic_sample_rate
        if not isinstance(profile_id, str):
            raise R3Error("PROVIDER_PROFILE_NOT_FOUND")
        if minecraft_version != "26.2":
            raise R3Error("RELEASE_VERSION_MISMATCH")
        if normal_threshold != 0.80 or high_threshold != 0.65:
            raise R3Error("R3_THRESHOLD_FIXED")
        if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 0 <= sample_rate <= 100:
            raise R3Error("INVALID_INPUT")
        profile = self.profile_store.load().get(profile_id)
        capabilities = self.profile_store.capabilities(profile_id) or {}
        if profile is None:
            raise R3Error("PROVIDER_PROFILE_NOT_FOUND")
        required_capabilities = {
            "image_input_supported",
            "structured_outputs_supported",
            "error_classification_supported",
        }
        if (
            not profile.enabled
            or profile.capability_status != "verified"
            or capabilities.get("capability_status") != "verified"
            or capabilities.get("adapter") != profile.adapter
            or any(capabilities.get(key) is not True for key in required_capabilities)
            or not self.secret_resolver.resolve(profile).configured
        ):
            raise R3Error("PROVIDER_CAPABILITY_MISSING")
        stage_config = profile.stages["offline_annotation"]
        configured_batch_size = batch_size if batch_size is not None else int(getattr(stage_config, "batch_size", 12))
        if isinstance(configured_batch_size, bool) or not isinstance(configured_batch_size, int) or not 8 <= configured_batch_size <= 16:
            raise R3Error("INVALID_INPUT")
        run_id = self._find_run_for_import(import_id, minecraft_version)
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id, minecraft_version) as database:
                run = database.fetchone("SELECT * FROM runs WHERE run_id=? AND import_id=?", (run_id, import_id))
                if run is None:
                    raise R3Error("IMPORT_NOT_FOUND")
                if run["minecraft_version"] != minecraft_version:
                    raise R3Error("RELEASE_VERSION_MISMATCH")
                r2 = database.fetchall("SELECT stage,status FROM stage_runs WHERE run_id=? ORDER BY ordinal LIMIT 6", (run_id,))
                if len(r2) != 6 or any(row["status"] != "succeeded" for row in r2):
                    raise R3Error("R2_PREREQUISITE_NOT_MET")
                import_row = database.fetchone("SELECT export_id FROM imports WHERE import_id=?", (import_id,))
                if import_row is None:
                    raise R3Error("IMPORT_NOT_FOUND")
                provider_snapshot = {
                    "adapter": profile.adapter,
                    "profile_id": profile.profile_id,
                    "model_id": profile.model_id,
                    "base_url_stable_id": profile.base_url_stable_id,
                    "secret_reference": profile.secret_reference,
                    "prompt_version": profile.prompt_version,
                    "annotation_output_schema_id": profile.annotation_output_schema_id,
                    "query_spec_output_schema_id": profile.query_spec_output_schema_id,
                    "rerank_output_schema_id": profile.rerank_output_schema_id,
                    "search_ranking_version": profile.search_ranking_version,
                    "profile": profile.to_dict(),
                }
                config = {
                    "schema_version": "workspace.v1",
                    "minecraft_version": minecraft_version,
                    "provider_snapshot": provider_snapshot,
                    "capabilities": {
                        "adapter": profile.adapter,
                        **{key: bool(capabilities.get(key)) for key in sorted(required_capabilities)},
                    },
                    "batch_size": configured_batch_size,
                    "normal_threshold": 0.80,
                    "high_threshold": 0.65,
                    "sample_rate": sample_rate,
                    "max_total_attempts": 2,
                    "feature_extractor_version": "features.v1",
                    "workspace_schema_version": "workspace.v1",
                }
                effective_hash = sha256_json(config)
                config["effective_config_hash"] = effective_hash

                # Configuration is a one-time R2-boundary command.  A retry
                # with the exact same effective input is safe only while no
                # R3 work has started; never delete jobs to make a retry look
                # successful after provider or review data exists.
                existing_hash = run["effective_config_hash"]
                existing_jobs = database.fetchall(
                    "SELECT job_id,logical_key,input_signature,status,cursor_json FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' ORDER BY logical_key",
                    (run_id,),
                )
                r3_stage_rows = database.fetchall(
                    "SELECT stage,status FROM stage_runs WHERE run_id=? AND ordinal>=6 ORDER BY ordinal",
                    (run_id,),
                )
                progressed = (
                    database.fetchone("SELECT 1 FROM provider_requests LIMIT 1") is not None
                    or database.fetchone("SELECT 1 FROM annotations LIMIT 1") is not None
                    or any(row["status"] in {"succeeded", "running", "needs_review"} for row in existing_jobs)
                    or any(row["status"] != "pending" for row in r3_stage_rows)
                )
                if existing_hash is not None or existing_jobs:
                    if existing_hash == effective_hash and existing_jobs and not progressed:
                        return self._configured_run_result(
                            run,
                            import_id,
                            minecraft_version,
                            self._public_config(_load_object(run["config_snapshot_json"])),
                            existing_jobs,
                            idempotent=True,
                        )
                    raise R3Error("RUN_STATE_CONFLICT")
                if (
                    run["status"] != "paused"
                    or run["current_stage"] != "AI_ANNOTATE"
                    or run["boundary_event"] != R3_BOUNDARY_EVENT
                ):
                    raise R3Error("RUN_STATE_CONFLICT")

                from .worker import build_ai_batch_specs

                try:
                    batch_specs = build_ai_batch_specs(
                        database,
                        run_id,
                        profile,
                        batch_size=configured_batch_size,
                        sample_rate=sample_rate,
                    )
                except (OSError, ValueError, KeyError) as exc:
                    raise R3Error("AI_BATCH_INPUT_INVALID") from exc
                with database.transaction() as connection:
                    connection.execute(
                        "UPDATE provider_profiles SET active=0 WHERE active=1"
                    )
                    connection.execute(
                        "INSERT OR REPLACE INTO provider_profiles(profile_id,model_id,base_url_stable_id,secret_reference,active,capability_status,profile_json) VALUES (?,?,?,?,?,?,?)",
                        (profile.profile_id, profile.model_id, profile.base_url_stable_id, profile.secret_reference, 1, "verified", canonical_json(provider_snapshot)),
                    )
                    connection.execute(
                        "UPDATE runs SET status='pending',current_stage='AI_ANNOTATE',boundary_event=NULL,config_snapshot_json=?,effective_config_hash=?,finished_at=NULL WHERE run_id=?",
                        (canonical_json(config), effective_hash, run_id),
                    )
                    connection.execute(
                        "UPDATE stage_runs SET status='pending',worker_id=NULL,pause_after_item=0,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND ordinal>=6",
                        (run_id,),
                    )
                    connection.execute("DELETE FROM jobs WHERE run_id=? AND stage IN ('AI_ANNOTATE','VALIDATE','HUMAN_REVIEW')", (run_id,))
                    for spec in batch_specs:
                        connection.execute(
                            "INSERT INTO jobs(job_id,run_id,stage,logical_key,input_signature,status,priority,cursor_json,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                            (
                                _stable_id("job", run_id, spec["logical_key"], spec["input_signature"]),
                                run_id,
                                "AI_ANNOTATE",
                                spec["logical_key"],
                                spec["input_signature"],
                                "pending",
                                0,
                                canonical_json({"approved": False, "tile_ids": spec["tile_ids"], "variant_ids": spec["variant_ids"], "input_hash": spec["input_signature"], "payload_signature": spec["payload_signature"]}),
                                utc_now(),
                            ),
                        )
                    connection.execute(
                        "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                        (_audit_id(), "R3_RUN_CONFIGURED", run_id, canonical_json({"profile_id": profile_id, "batch_count": len(batch_specs), "effective_config_hash": effective_hash}), utc_now()),
                    )
                batches = [
                    {
                        "job_id": _stable_id("job", run_id, spec["logical_key"], spec["input_signature"]),
                        "logical_key": spec["logical_key"],
                        "input_signature": spec["input_signature"],
                        "status": "pending",
                        "variant_ids": list(spec["variant_ids"]),
                    }
                    for spec in batch_specs
                ]
                return {
                    "run_id": run_id,
                    "import_id": import_id,
                    "minecraft_version": minecraft_version,
                    "status": "pending",
                    "effective_config_hash": effective_hash,
                    "config_snapshot": self._public_config(config),
                    "batch_count": len(batch_specs),
                    "batches": batches,
                    "idempotent": False,
                }

    @staticmethod
    def _configured_run_result(
        run: Any,
        import_id: str,
        minecraft_version: str,
        config_snapshot: dict[str, Any],
        jobs: Any,
        *,
        idempotent: bool,
    ) -> dict[str, Any]:
        batches = []
        for row in jobs:
            cursor = _load_object(row["cursor_json"])
            batches.append(
                {
                    "job_id": row["job_id"],
                    "logical_key": row["logical_key"],
                    "input_signature": row["input_signature"],
                    "status": row["status"],
                    "variant_ids": [
                        value for value in cursor.get("variant_ids", []) if isinstance(value, str)
                    ],
                }
            )
        return {
            "run_id": run["run_id"],
            "import_id": import_id,
            "minecraft_version": minecraft_version,
            "status": run["status"],
            "effective_config_hash": run["effective_config_hash"],
            "config_snapshot": config_snapshot,
            "batch_count": len(batches),
            "batches": batches,
            "idempotent": idempotent,
        }

    start_r3 = configure_run

    def _find_run_for_import(self, import_id: str, minecraft_version: str) -> str:
        parent = self.data_root.workspace / minecraft_version
        if not parent.is_dir():
            raise R3Error("IMPORT_NOT_FOUND")
        matches: list[str] = []
        for run_dir in sorted(parent.iterdir(), key=lambda path: path.name):
            path = run_dir / "work.sqlite3"
            if not path.is_file() or path.is_symlink():
                continue
            try:
                with WorkspaceDatabase.open(path, read_only=True) as database:
                    row = database.fetchone("SELECT run_id FROM runs WHERE import_id=? AND minecraft_version=?", (import_id, minecraft_version))
                    if row is not None:
                        matches.append(str(row["run_id"]))
            except Exception:
                continue
        if len(matches) != 1:
            raise R3Error("IMPORT_NOT_FOUND")
        return matches[0]

    @staticmethod
    def _public_config(config: dict[str, Any]) -> dict[str, Any]:
        result = json.loads(canonical_json(config))
        snapshot = result.get("provider_snapshot", {})
        if isinstance(snapshot, dict):
            profile = snapshot.pop("profile", None)
            if isinstance(profile, dict):
                profile.pop("api_key", None)
                profile.pop("authorization", None)
        return result

    # ---- Preview and approval ---------------------------------------------------

    def preview_ai_batch(self, run_id: str, logical_key: str | None = None) -> dict[str, Any]:
        with self.worker.open_database(run_id) as database:
            row = self._find_ai_job(database, run_id, logical_key, unapproved=logical_key is None)
            if row is None:
                raise R3Error("AI_BATCH_NOT_FOUND")
            try:
                return self.worker.build_ai_preview(database, row)
            except (OSError, KeyError, TypeError, ValueError) as exc:
                raise R3Error("AI_BATCH_INPUT_INVALID") from exc

    get_ai_batch_preview = preview_ai_batch
    preview_current_batch = preview_ai_batch

    def approve_ai_batch(self, run_id: str, logical_key: str, input_signature: str) -> dict[str, Any]:
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    row = connection.execute(
                        "SELECT run_id,job_id,logical_key,input_signature,cursor_json,status FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND logical_key=?",
                        (run_id, logical_key),
                    ).fetchone()
                    if row is None:
                        raise R3Error("AI_BATCH_NOT_FOUND")
                    try:
                        current_signature = self.worker.current_ai_input_signature(database, run_id, row)
                    except Exception as exc:
                        raise R3Error("AI_BATCH_INPUT_CHANGED") from exc
                    if current_signature is None:
                        raise R3Error("AI_BATCH_INPUT_CHANGED")
                    if input_signature != current_signature:
                        raise R3Error("AI_BATCH_INPUT_CHANGED")
                    cursor = _load_object(row["cursor_json"])
                    if cursor.get("approved") is True and row["input_signature"] == current_signature and not cursor.get("input_invalid") and cursor.get("input_error_code") is None:
                        return {"run_id": run_id, "logical_key": logical_key, "approved": True, "input_signature": current_signature}
                    if row["status"] not in {"pending", "needs_review"} and not (cursor.get("approved") is True and row["input_signature"] == current_signature and not cursor.get("input_invalid")):
                        raise R3Error("RUN_STATE_CONFLICT")
                    input_repaired = row["input_signature"] != current_signature or cursor.get("input_invalid") is True or cursor.get("input_error_code") is not None
                    cursor["approved"] = True
                    cursor["payload_signature"] = current_signature
                    cursor["input_hash"] = current_signature
                    cursor.pop("input_invalid", None)
                    cursor.pop("input_error_code", None)
                    connection.execute(
                        "UPDATE jobs SET input_signature=?,status='pending',worker_id=NULL,heartbeat_at=NULL,error_code=NULL,error_message=NULL,finished_at=NULL,cursor_json=? WHERE job_id=?",
                        (current_signature, canonical_json(cursor), row["job_id"]),
                    )
                    connection.execute("UPDATE stage_runs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND stage='AI_ANNOTATE' AND status NOT IN ('succeeded','failed','cancelled')", (run_id,))
                    connection.execute("UPDATE runs SET status='pending',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND current_stage='AI_ANNOTATE' AND status NOT IN ('succeeded','failed','cancelled')", (run_id,))
                    connection.execute(
                        "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                        (_audit_id(), "AI_BATCH_APPROVED", run_id, row["job_id"], canonical_json({"logical_key": logical_key, "input_signature": current_signature, "input_repaired": input_repaired}), now),
                    )
                return {"run_id": run_id, "logical_key": logical_key, "approved": True, "input_signature": current_signature}

    approve_batch = approve_ai_batch
    approve = approve_ai_batch

    def preview_ai_plan(self, run_id: str) -> dict[str, Any]:
        """Return the frozen, non-sensitive remaining AI submission plan."""

        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                try:
                    with database.read_transaction():
                        return self.worker.build_ai_plan(database, run_id)
                except KeyError as exc:
                    raise R3Error("RUN_NOT_FOUND") from exc
                except (OSError, TypeError, ValueError, StageFailure) as exc:
                    raise R3Error("AI_BATCH_INPUT_INVALID") from exc

    preview_remaining_ai_plan = preview_ai_plan
    preview_ai_batch_plan = preview_ai_plan

    def approve_ai_plan(
        self,
        run_id: str,
        submitted_plan_hash: str | None = None,
        *,
        plan_hash: str | None = None,
        submitted_hash: str | None = None,
    ) -> dict[str, Any]:
        """Approve all unchanged pending AI jobs in one run transaction."""

        submitted = submitted_plan_hash or plan_hash or submitted_hash
        if not isinstance(submitted, str) or not submitted:
            raise R3Error("INVALID_INPUT")
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                try:
                    with database.transaction() as connection:
                        run = connection.execute(
                            "SELECT status,current_stage FROM runs WHERE run_id=?",
                            (run_id,),
                        ).fetchone()
                        stage = connection.execute(
                            "SELECT status FROM stage_runs WHERE run_id=? AND stage='AI_ANNOTATE'",
                            (run_id,),
                        ).fetchone()
                        if run is None:
                            raise R3Error("RUN_NOT_FOUND")
                        if stage is None or run["status"] not in {"pending", "running", "paused"} or stage["status"] not in {"pending", "running", "paused"}:
                            raise R3Error("RUN_STATE_CONFLICT")
                        current = self.worker.build_ai_plan(database, run_id)
                        if current["plan_hash"] != submitted:
                            raise R3Error("AI_BATCH_PLAN_CONFLICT")
                        rows = connection.execute(
                            "SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND status='pending' ORDER BY logical_key,job_id",
                            (run_id,),
                        ).fetchall()
                        by_id = {job["job_id"]: job for job in current["jobs"]}
                        if {row["job_id"] for row in rows} != set(by_id):
                            raise R3Error("AI_BATCH_PLAN_CONFLICT")
                        already_approved = True
                        for row in rows:
                            cursor = _load_object(row["cursor_json"])
                            if cursor.get("approved") is not True or row["input_signature"] != by_id[row["job_id"]]["input_signature"]:
                                already_approved = False
                                break
                        if already_approved:
                            if _has_plan_audit(connection, run_id, current["plan_hash"]):
                                return {**current, "approved": True, "idempotent": True}
                            now = utc_now()
                            connection.execute(
                                "UPDATE stage_runs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND stage='AI_ANNOTATE' AND status IN ('pending','paused','running')",
                                (run_id,),
                            )
                            connection.execute(
                                "UPDATE runs SET status='pending',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND status IN ('pending','paused','running')",
                                (run_id,),
                            )
                            connection.execute(
                                "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                                (_audit_id(), "AI_BATCH_PLAN_APPROVED", run_id, canonical_json({"plan_hash": current["plan_hash"], "job_count": len(rows)}), now),
                            )
                            for row in rows:
                                connection.execute(
                                    "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                                    (_audit_id(), "AI_BATCH_APPROVED", run_id, row["job_id"], canonical_json({"plan_hash": current["plan_hash"], "independently_approved": True}), now),
                                )
                            return {**current, "approved": True, "idempotent": False}
                        now = utc_now()
                        for row in rows:
                            item = by_id[row["job_id"]]
                            cursor = _load_object(row["cursor_json"])
                            cursor["approved"] = True
                            cursor["payload_signature"] = item["input_signature"]
                            cursor["input_hash"] = item["input_signature"]
                            cursor.pop("input_invalid", None)
                            cursor.pop("input_error_code", None)
                            connection.execute(
                                "UPDATE jobs SET input_signature=?,cursor_json=?,error_code=NULL,error_message=NULL,finished_at=NULL WHERE job_id=? AND status='pending'",
                                (item["input_signature"], canonical_json(cursor), row["job_id"]),
                            )
                        connection.execute(
                            "UPDATE stage_runs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND stage='AI_ANNOTATE' AND status IN ('pending','paused','running')",
                            (run_id,),
                        )
                        connection.execute(
                            "UPDATE runs SET status='pending',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND status IN ('pending','paused','running')",
                            (run_id,),
                        )
                        connection.execute(
                            "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                            (_audit_id(), "AI_BATCH_PLAN_APPROVED", run_id, canonical_json({"plan_hash": current["plan_hash"], "job_count": len(rows)}), now),
                        )
                        for row in rows:
                            connection.execute(
                                "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                                (_audit_id(), "AI_BATCH_APPROVED", run_id, row["job_id"], canonical_json({"plan_hash": current["plan_hash"]}), now),
                            )
                        return {**current, "approved": True, "idempotent": False}
                except R3Error:
                    raise
                except (OSError, KeyError, TypeError, ValueError) as exc:
                    raise R3Error("AI_BATCH_PLAN_CONFLICT") from exc

    approve_remaining_ai_plan = approve_ai_plan
    confirm_ai_plan = approve_ai_plan

    def cancel_ai_batch(self, run_id: str, logical_key: str, *, reason: str = "operator cancelled") -> dict[str, Any]:
        if is_sensitive_review_text(reason):
            raise R3Error("INVALID_INPUT")
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                with database.transaction() as connection:
                    row = connection.execute("SELECT job_id,status,cursor_json FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND logical_key=?", (run_id, logical_key)).fetchone()
                    if row is None:
                        raise R3Error("AI_BATCH_NOT_FOUND")
                    if row["status"] in {"succeeded", "failed", "skipped"}:
                        return {"run_id": run_id, "logical_key": logical_key, "status": row["status"]}
                    cursor = _load_object(row["cursor_json"])
                    variant_ids = cursor.get("variant_ids")
                    if not isinstance(variant_ids, list) or not variant_ids or any(
                        not isinstance(variant_id, str) or not variant_id.startswith("minecraft:")
                        for variant_id in variant_ids
                    ):
                        raise R3Error("AI_BATCH_INPUT_INVALID")
                    now = utc_now()
                    connection.execute("UPDATE jobs SET status='skipped',error_code='AI_BATCH_CANCELLED',error_message='cancelled by operator',finished_at=? WHERE job_id=?", (now, row["job_id"]))
                    # The cancelled batch is an item-level terminal skip.  The
                    # AI stage must remain runnable so the worker can finish
                    # the remaining batches and route these reviews to the
                    # normal HUMAN_REVIEW closure.
                    connection.execute("UPDATE stage_runs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND stage='AI_ANNOTATE'", (run_id,))
                    connection.execute("UPDATE runs SET status='pending',current_stage='AI_ANNOTATE',boundary_event=NULL,finished_at=NULL WHERE run_id=?", (run_id,))
                    review_ids = [
                        self.worker.create_review_task(
                            connection,
                            "variant",
                            variant_id,
                            "AI_BATCH_CANCELLED",
                            "high",
                            reason,
                            [],
                            dedupe_key=logical_key,
                        )
                        for variant_id in variant_ids
                    ]
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)", (_audit_id(), "AI_BATCH_CANCELLED", run_id, row["job_id"], canonical_json({"variant_ids": variant_ids}), now))
                    return {"run_id": run_id, "logical_key": logical_key, "status": "skipped", "run_status": "pending", "review_status": "open", "variant_ids": variant_ids, "review_ids": review_ids}

    cancel_batch = cancel_ai_batch
    cancel_ai = cancel_ai_batch

    def _find_ai_job(self, database: WorkspaceDatabase, run_id: str, logical_key: str | None, *, unapproved: bool = False) -> Any:
        query = "SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND status NOT IN ('succeeded','failed','skipped')"
        params: list[Any] = [run_id]
        if logical_key is not None:
            query += " AND logical_key=?"
            params.append(logical_key)
        query += " ORDER BY logical_key"
        for row in database.fetchall(query, tuple(params)):
            cursor = _load_object(row["cursor_json"])
            if unapproved and cursor.get("approved") is not False:
                continue
            return row
        return None

    # ---- HUMAN_REVIEW application service --------------------------------------

    def list_reviews(self, run_id: str, *, severity: str | None = None, status: str = "open", limit: int = 50) -> list[dict[str, Any]]:
        if status not in {"open", "resolved", "rejected"}:
            raise R3Error("INVALID_INPUT")
        limit = max(1, min(200, int(limit)))
        with self.worker.open_database(run_id) as database:
            query = "SELECT * FROM review_tasks WHERE minecraft_version='26.2' AND status=?"
            params: list[Any] = [status]
            if severity is not None:
                if severity not in {"normal", "high"}:
                    raise R3Error("INVALID_INPUT")
                query += " AND severity=?"
                params.append(severity)
            query += " ORDER BY CASE severity WHEN 'high' THEN 0 ELSE 1 END, created_at, review_id LIMIT ?"
            params.append(limit)
            return [self._safe_review(database, row) for row in database.fetchall(query, tuple(params))]

    list_review_tasks = list_reviews

    def resolve_review(
        self,
        run_id: str,
        review_id: str,
        *,
        decision: str,
        reviewer: str,
        reason_code: str | None = None,
        reason: str | None = None,
        note: str,
        evidence: list[str] | tuple[str, ...],
        override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"accept", "edit_and_accept", "skip", "request_reexport", "request_exporter_rerender", "retry_ai"}:
            raise R3Error("INVALID_INPUT")
        if not isinstance(reviewer, str) or not 1 <= len(reviewer) <= 128:
            raise R3Error("INVALID_INPUT")
        reason_value = reason_code or reason or "OTHER"
        if not isinstance(reason_value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{1,127}", reason_value):
            raise R3Error("INVALID_INPUT")
        if not isinstance(note, str) or len(note) < 2 or len(note) > 500:
            raise R3Error("INVALID_INPUT")
        if is_sensitive_review_text(reviewer) or is_sensitive_review_text(note) or is_sensitive_review_text(reason_value):
            raise R3Error("INVALID_INPUT")
        evidence_values = list(evidence) if isinstance(evidence, (list, tuple)) else []
        if not evidence_values or len(evidence_values) > 64 or any(not isinstance(item, str) or not item or len(item) > 512 or is_sensitive_review_text(item) for item in evidence_values):
            raise R3Error("INVALID_INPUT")
        override = override or {}
        if not isinstance(override, dict):
            raise R3Error("OVERRIDE_INVALID")
        if _contains_machine_field(override):
            raise R3Error("MACHINE_FACT_READ_ONLY")
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    task = connection.execute("SELECT * FROM review_tasks WHERE review_id=? AND status='open'", (review_id,)).fetchone()
                    if task is None:
                        raise R3Error("REVIEW_NOT_FOUND")
                    target_id = str(task["target_id"])
                    is_fts_retry = task["target_type"] == "run" and task["target_id"] == run_id and task["reason_code"] == "FTS_BUILD_FAILED"
                    variant = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
                    if decision in {"accept", "edit_and_accept"} and variant is None and not is_fts_retry:
                        raise R3Error("OVERRIDE_INVALID")
                    bound_annotation_ids = _review_annotation_ids(task["evidence_json"])
                    if is_fts_retry:
                        if decision != "accept":
                            raise R3Error("OVERRIDE_INVALID")
                    elif decision == "accept":
                        if bound_annotation_ids:
                            self._accept_annotation(connection, target_id, bound_annotation_ids)
                        elif not self._qualification_review_complete(connection, target_id) and not human_semantics_complete(connection, target_id):
                            raise R3Error("REVIEW_REQUIRES_ANNOTATION")
                    elif decision == "edit_and_accept":
                        if bound_annotation_ids:
                            self._accept_annotation(connection, target_id, bound_annotation_ids)
                        self._save_review_override(connection, target_id, reviewer, reason_value, note, evidence_values, override, now)
                        if not bound_annotation_ids:
                            updated_variant = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
                            updated_record = json.loads(updated_variant["record_json"] or "{}") if updated_variant is not None else {}
                            if updated_record.get("candidate_qualification") != "excluded" and not human_semantics_complete(connection, target_id):
                                raise R3Error("OVERRIDE_INVALID")
                    elif decision == "skip":
                        self._save_skip_review(connection, task, reviewer, reason_value, note, evidence_values, now)
                    elif decision in {"request_reexport", "request_exporter_rerender"}:
                        event_type = "REVIEW_REEXPORT_REQUESTED" if decision == "request_reexport" else "REVIEW_RERENDER_REQUESTED"
                        connection.execute(
                            "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                            (_audit_id(), event_type, run_id, canonical_json({"review_id": review_id, "target_id": target_id, "reviewer": reviewer, "reason_code": reason_value}), now),
                        )
                    elif decision == "retry_ai":
                        try:
                            self._unverify_review_annotations(connection, target_id, bound_annotation_ids)
                            new_job = self.worker.create_retry_ai_job(database, run_id, target_id, review_id)
                        except (KeyError, ValueError, OSError) as exc:
                            raise R3Error("AI_BATCH_INPUT_INVALID") from exc
                        connection.execute(
                            "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                            (_audit_id(), "REVIEW_AI_RETRY_REQUESTED", run_id, canonical_json({"review_id": review_id, "target_id": target_id, "new_input_signature": new_job["input_signature"]}), now),
                        )
                    preserved_evidence = _merge_review_evidence(task["evidence_json"], evidence_values)
                    final_status = "open" if decision in {"request_reexport", "request_exporter_rerender"} else "resolved"
                    connection.execute(
                        "UPDATE review_tasks SET status=?,note=?,evidence_json=?,resolved_at=? WHERE review_id=? AND status='open'",
                        (final_status, note, canonical_json(preserved_evidence), None if final_status == "open" else now, review_id),
                    )
                    if decision in {"request_reexport", "request_exporter_rerender"}:
                        # The source repair request is an unresolved blocker;
                        # the old task stays in HUMAN_REVIEW until a fresh
                        # exporter handoff is imported.
                        return {"review_id": review_id, "status": "open", "pending": True, "decision": decision}
                    if decision not in {"retry_ai"}:
                        connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), {"accept": "REVIEW_ACCEPTED", "edit_and_accept": "REVIEW_EDITED", "skip": "REVIEW_SKIPPED"}[decision], run_id, canonical_json({"review_id": review_id, "reviewer": reviewer, "reason_code": reason_value}), now))
                return {"review_id": review_id, "status": "resolved", "decision": decision}

    resolve_review_task = resolve_review

    def continue_review(self, run_id: str) -> dict[str, Any]:
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                run = database.fetchone("SELECT status,current_stage FROM runs WHERE run_id=?", (run_id,))
                if run is None:
                    raise R3Error("RUN_NOT_FOUND")
                if database.fetchone("SELECT 1 FROM review_tasks WHERE minecraft_version='26.2' AND status='open'") is not None:
                    raise R3Error("REVIEW_TASKS_OPEN")
                if run["current_stage"] != "HUMAN_REVIEW" or run["status"] != "needs_review":
                    raise R3Error("RUN_STATE_CONFLICT")
                try:
                    WorkspaceQueryService(database).rebuild_index()
                except Exception as exc:
                    with database.transaction() as connection:
                        self.worker.create_review_task(connection, "run", run_id, "FTS_BUILD_FAILED", "high", "Search index requires review.", [], dedupe_key="fts", reopen=True)
                        now = utc_now()
                        connection.execute("UPDATE stage_runs SET status='needs_review',worker_id=NULL,finished_at=? WHERE run_id=? AND stage='HUMAN_REVIEW'", (now, run_id))
                        connection.execute("UPDATE runs SET status='needs_review',finished_at=? WHERE run_id=?", (now, run_id))
                        connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), "HUMAN_REVIEW_REQUIRED", run_id, canonical_json({"closure_errors": ["FTS_BUILD_FAILED"]}), now))
                    raise R3Error("FTS_BUILD_FAILED") from exc
                with database.transaction() as connection:
                    self.worker._reconcile_derived_review_tasks(connection, database, run_id, fts_ready=True)
                    if connection.execute("SELECT 1 FROM review_tasks WHERE minecraft_version='26.2' AND status='open'").fetchone() is not None:
                        raise R3Error("REVIEW_TASKS_OPEN")
                    now = utc_now()
                    connection.execute("UPDATE stage_runs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND stage='HUMAN_REVIEW'", (run_id,))
                    connection.execute("UPDATE runs SET status='pending',finished_at=NULL WHERE run_id=?", (run_id,))
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), "HUMAN_REVIEW_CONTINUED", run_id, "{}", now))
                return self.get_run(run_id)

    def _safe_review(self, database: WorkspaceDatabase, row: Any) -> dict[str, Any]:
        target_id = str(row["target_id"])
        variant_row = database.fetchone("SELECT record_json FROM variants WHERE variant_id=?", (target_id,))
        bound_annotation_ids = _review_annotation_ids(row["evidence_json"])
        if bound_annotation_ids:
            placeholders = ",".join("?" for _ in bound_annotation_ids)
            annotation_rows = database.fetchall(
                f"SELECT record_json FROM annotations WHERE subject_id=? AND annotation_id IN ({placeholders}) ORDER BY annotation_id",
                (target_id, *bound_annotation_ids),
            )
        else:
            annotation_rows = []
        machine: dict[str, Any] = {}
        preview: dict[str, Any] = {"variant_id": target_id, "purpose": "human_review"}
        if variant_row is not None:
            record = json.loads(variant_row["record_json"])
            machine = {
                "block_id": record.get("block_id"),
                "canonical_state_id": record.get("canonical_state_id"),
                "represented_state_ids": record.get("represented_state_ids", []),
                "machine_facts": record.get("machine_facts", {}),
                "candidate_qualification": record.get("candidate_qualification"),
                "warnings": record.get("warnings", []),
            }
            render = record.get("render", {})
            if isinstance(render, dict):
                preview.update({"image_sha256": render.get("image_sha256"), "dimensions": {"width": 512, "height": 512}})
        annotations = []
        for annotation_row in annotation_rows:
            value = json.loads(annotation_row["record_json"])
            annotations.append(value)
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
        except (TypeError, ValueError):
            evidence = []
        validation_diagnostic: dict[str, Any] | None = None
        if isinstance(evidence, list):
            for candidate in evidence:
                validation_diagnostic = sanitize_validation_diagnostic(candidate)
                if validation_diagnostic is not None:
                    break
        failure_row = database.fetchone("SELECT failure_id FROM failures WHERE variant_id=? OR state_id=? ORDER BY failure_id LIMIT 1", (target_id, target_id))
        return {
            "review_id": row["review_id"],
            "minecraft_version": row["minecraft_version"],
            "target_type": row["target_type"],
            "target_id": target_id,
            "reason_code": row["reason_code"],
            "severity": row["severity"],
            "status": row["status"],
            "note": row["note"],
            "evidence_count": len(evidence) if isinstance(evidence, list) else 0,
            "validation_diagnostic": validation_diagnostic,
            "machine_failure_ref": failure_row["failure_id"] if failure_row is not None else None,
            "machine_facts_read_only": True,
            "machine": machine,
            "annotations": annotations,
            "preview": preview,
            "created_at": row["created_at"],
            "resolved_at": row["resolved_at"],
        }

    def _qualification_review_complete(self, connection: Any, target_id: str) -> bool:
        row = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
        if row is None:
            return False
        variant = json.loads(row["record_json"] or "{}")
        qualification = variant.get("candidate_qualification")
        if qualification not in {"eligible", "conditional", "excluded"}:
            return False
        for review_id in variant.get("qualification_review_refs", []):
            review_row = connection.execute("SELECT record_json FROM overrides WHERE override_id=? AND target_id=?", (review_id, target_id)).fetchone()
            if review_row is None:
                continue
            try:
                review = json.loads(review_row["record_json"])
                validate_record("qualification-review.v1", review, repo_root=self.worker.repo_root)
            except (RecordSchemaError, TypeError, ValueError):
                continue
            if review.get("qualification") == qualification:
                return True
        return False

    def _accept_annotation(self, connection: Any, target_id: str, annotation_ids: list[str]) -> None:
        if not annotation_ids:
            raise R3Error("REVIEW_REQUIRES_ANNOTATION")
        placeholders = ",".join("?" for _ in annotation_ids)
        rows = connection.execute(
            f"SELECT annotation_id,record_json FROM annotations WHERE subject_id=? AND annotation_id IN ({placeholders})",
            (target_id, *annotation_ids),
        ).fetchall()
        if len(rows) != len(set(annotation_ids)):
            raise R3Error("REVIEW_REQUIRES_ANNOTATION")
        if not rows:
            raise R3Error("REVIEW_REQUIRES_ANNOTATION")
        for row in rows:
            record = json.loads(row["record_json"])
            record["source"]["verified"] = True
            try:
                validate_record("annotation-record.v1", record, repo_root=self.worker.repo_root)
            except RecordSchemaError as exc:
                raise R3Error("OVERRIDE_INVALID") from exc
            connection.execute("UPDATE annotations SET record_json=? WHERE annotation_id=?", (canonical_json(record), row["annotation_id"]))

    def _unverify_review_annotations(self, connection: Any, target_id: str, annotation_ids: list[str]) -> None:
        if not annotation_ids:
            return
        placeholders = ",".join("?" for _ in annotation_ids)
        rows = connection.execute(
            f"SELECT annotation_id,record_json FROM annotations WHERE subject_id=? AND annotation_id IN ({placeholders})",
            (target_id, *annotation_ids),
        ).fetchall()
        for row in rows:
            record = json.loads(row["record_json"])
            source = record.get("source")
            if isinstance(source, dict) and source.get("verified") is True:
                source["verified"] = False
                validate_record("annotation-record.v1", record, repo_root=self.worker.repo_root)
                connection.execute("UPDATE annotations SET record_json=? WHERE annotation_id=?", (canonical_json(record), row["annotation_id"]))

    def _save_review_override(self, connection: Any, target_id: str, reviewer: str, reason_code: str, note: str, evidence: list[str], override: dict[str, Any], now: str) -> None:
        operations = dict(override.get("operations", override))
        if not isinstance(operations, dict):
            raise R3Error("OVERRIDE_INVALID")
        qualification = operations.pop("qualification", override.get("qualification")) if isinstance(operations, dict) else None
        warnings = operations.pop("warnings", override.get("warnings")) if isinstance(operations, dict) else None
        allowed = {"add_synonyms_zh", "remove_synonyms_zh", "add_synonyms_en", "remove_synonyms_en", "set_summary_zh", "set_summary_en", "add_color_terms", "remove_color_terms", "add_shape_terms", "remove_shape_terms", "add_material_impressions", "remove_material_impressions", "add_building_roles", "remove_building_roles", "add_style_tags", "remove_style_tags", "add_avoid_for", "remove_avoid_for", "set_confidence"}
        if set(operations) - allowed or any(is_sensitive_review_text(str(value)) for value in operations.values()):
            raise R3Error("OVERRIDE_INVALID")
        if operations:
            override_id = _stable_id("ov", target_id, now, canonical_json(operations))
            record = {
                "schema_version": "manual-override.v1",
                "override_id": override_id,
                "minecraft_version": "26.2",
                "scope": {"level": "variant", "variant_id": target_id},
                "operations": operations,
                "reason": note,
                "author": reviewer,
                "approved_by": reviewer,
                "created_at": now,
                "input_signature": self._target_input_signature(connection, target_id),
            }
            try:
                validate_record("manual-override.v1", record, repo_root=self.worker.repo_root)
            except RecordSchemaError as exc:
                raise R3Error("OVERRIDE_INVALID") from exc
            connection.execute("INSERT OR IGNORE INTO overrides(override_id,target_id,minecraft_version,record_json) VALUES (?,?,?,?)", (override_id, target_id, "26.2", canonical_json(record)))
            self._append_variant_ref(connection, target_id, "override_refs", override_id)
        if qualification is not None:
            if qualification not in {"eligible", "conditional", "excluded"}:
                raise R3Error("OVERRIDE_INVALID")
            warning_values = list(warnings or [])
            if any(not isinstance(item, str) or is_sensitive_review_text(item) for item in warning_values):
                raise R3Error("OVERRIDE_INVALID")
            if qualification == "conditional" and not warning_values:
                raise R3Error("OVERRIDE_INVALID")
            if qualification != "conditional":
                warning_values = []
            review_id = _stable_id("qualification", target_id, now, qualification)
            record = {
                "schema_version": "qualification-review.v1",
                "review_id": review_id,
                "target_type": "visual_variant",
                "target_id": target_id,
                "minecraft_version": "26.2",
                "reviewer": reviewer,
                "reviewed_at": now,
                "reason_code": reason_code if reason_code in {"QUALIFICATION_CONFIRMED", "CONTEXT_REQUIRED", "NOT_A_BUILDING_CANDIDATE", "MANUAL_REINCLUSION", "OTHER"} else "OTHER",
                "note": note,
                "evidence": evidence,
                "source_version": "workspace.v1",
                "qualification": qualification,
                "warnings": warning_values,
            }
            try:
                validate_record("qualification-review.v1", record, repo_root=self.worker.repo_root)
            except RecordSchemaError as exc:
                raise R3Error("OVERRIDE_INVALID") from exc
            connection.execute("INSERT OR IGNORE INTO overrides(override_id,target_id,minecraft_version,record_json) VALUES (?,?,?,?)", (review_id, target_id, "26.2", canonical_json(record)))
            self._append_variant_ref(connection, target_id, "qualification_review_refs", review_id)
            row = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
            if row is not None:
                variant = json.loads(row["record_json"])
                variant["candidate_qualification"] = qualification
                variant["warnings"] = warning_values
                validate_record("visual-variant-record.v1", variant, repo_root=self.worker.repo_root)
                connection.execute("UPDATE variants SET record_json=? WHERE variant_id=?", (canonical_json(variant), target_id))
        if not operations and qualification is None:
            raise R3Error("OVERRIDE_INVALID")

    def _save_skip_review(self, connection: Any, task: Any, reviewer: str, reason_code: str, note: str, evidence: list[str], now: str) -> None:
        target_id = str(task["target_id"])
        failure = connection.execute("SELECT failure_id,record_json FROM failures WHERE variant_id=? OR state_id=? ORDER BY failure_id LIMIT 1", (target_id, target_id)).fetchone()
        if failure is None:
            raise R3Error("SKIP_REQUIRES_MACHINE_FAILURE")
        failure_record = json.loads(failure["record_json"])
        allowed_reason = {"REGISTRY_INCOMPLETE", "INVALID_STATE", "MISSING_TRANSLATION", "MISSING_TEXTURE", "EMPTY_RENDER", "BACKGROUND_ONLY_RENDER", "OBJECT_OFF_CANVAS", "OBJECT_TOO_SMALL", "FRAME_INCONSISTENT", "ANIMATED_FIXTURE_UNSUPPORTED", "FLUID_FIXTURE_UNSUPPORTED", "BLOCK_ENTITY_FIXTURE_UNSUPPORTED", "IO_ERROR", "SCHEMA_INVALID", "CHECKSUM_MISMATCH", "IDEMPOTENCY_CONFLICT", "EXPORTER_EXCEPTION"}
        failure_reason = reason_code if reason_code in allowed_reason else str(failure_record.get("reason_code", "EXPORTER_EXCEPTION"))
        record = {
            "schema_version": "skip-review.v1",
            "review_id": _stable_id("skip", target_id, now),
            "target_type": "visual_variant" if str(task["target_type"]) == "variant" else str(task["target_type"]),
            "target_id": target_id,
            "minecraft_version": "26.2",
            "reviewer": reviewer,
            "reviewed_at": now,
            "reason_code": failure_reason,
            "note": note,
            "evidence": evidence,
            "source_version": "export-contract.v1",
            "machine_failure_ref": str(failure["failure_id"]),
        }
        try:
            validate_record("skip-review.v1", record, repo_root=self.worker.repo_root)
        except RecordSchemaError as exc:
            raise R3Error("OVERRIDE_INVALID") from exc
        connection.execute("INSERT OR IGNORE INTO overrides(override_id,target_id,minecraft_version,record_json) VALUES (?,?,?,?)", (record["review_id"], target_id, "26.2", canonical_json(record)))
        variant = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
        if variant is not None:
            value = json.loads(variant["record_json"])
            refs = list(value.get("override_refs", []))
            if record["review_id"] not in refs:
                refs.append(record["review_id"])
            value["override_refs"] = refs
            validate_record("visual-variant-record.v1", value, repo_root=self.worker.repo_root)
            connection.execute("UPDATE variants SET record_json=? WHERE variant_id=?", (canonical_json(value), target_id))

    def _append_variant_ref(self, connection: Any, target_id: str, field: str, ref: str) -> None:
        row = connection.execute("SELECT record_json FROM variants WHERE variant_id=?", (target_id,)).fetchone()
        if row is None:
            raise R3Error("OVERRIDE_INVALID")
        record = json.loads(row["record_json"])
        refs = list(record.get(field, []))
        if ref not in refs:
            refs.append(ref)
        record[field] = refs
        validate_record("visual-variant-record.v1", record, repo_root=self.worker.repo_root)
        connection.execute("UPDATE variants SET record_json=? WHERE variant_id=?", (canonical_json(record), target_id))

    def _target_input_signature(self, connection: Any, target_id: str) -> str:
        row = connection.execute("SELECT input_sha256,output_hash FROM features WHERE variant_id=?", (target_id,)).fetchone()
        return sha256_json({"target_id": target_id, "feature": dict(row) if row is not None else {}})

    def check_import(self, source_directory: str | Path, minecraft_version: str) -> ImportCheck:
        return self.imports.check_import(source_directory, minecraft_version)

    def start_import_check(self, source_directory_ref: str, minecraft_version: str) -> ImportCheckStart:
        # Preserve the importer start envelope so WebUI callers retain
        # duplicate/reuse status and the authoritative HTTP response code.
        return self.imports.start_check(source_directory_ref, minecraft_version)

    def get_import_check(self, check_id: str) -> ImportCheck:
        return self.imports.get_check(check_id)

    def list_directories(self, minecraft_version: str, parent_ref: str | None = None) -> dict[str, Any]:
        return self.directory_chooser.list_directories(minecraft_version, parent_ref)

    def import_checked(self, check_id: str, *, copy_mode: str = "copy_to_workspace") -> dict[str, Any]:
        return self.imports.import_checked(check_id, copy_mode=copy_mode)

    def get_run(self, run_id: str) -> dict[str, Any]:
        self.run_snapshots.stale_after_seconds = self.worker.stale_after_seconds
        return self.run_snapshots.snapshot(run_id)

    def list_runs(self, minecraft_version: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        if not self.data_root.workspace.is_dir():
            return rows
        versions = [minecraft_version] if minecraft_version else sorted(path.name for path in self.data_root.workspace.iterdir() if path.is_dir())
        for version in versions:
            version_dir = self.data_root.workspace / version
            if not version_dir.is_dir():
                continue
            for run_dir in sorted(version_dir.iterdir(), key=lambda path: path.name):
                db_path = run_dir / "work.sqlite3"
                if not db_path.is_file():
                    continue
                database = WorkspaceDatabase.open(db_path, read_only=True)
                try:
                    row = database.fetchone("SELECT run_id,minecraft_version,status,current_stage,boundary_event,created_at,started_at,finished_at FROM runs LIMIT 1")
                    if row:
                        rows.append({key: row[key] for key in row.keys()})
                finally:
                    database.close()
        return rows

    def pause(self, run_id: str) -> dict[str, Any]:
        return self._run_command(run_id, "pause")

    def resume(self, run_id: str) -> dict[str, Any]:
        return self._run_command(run_id, "resume")

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    row = connection.execute("SELECT status,current_stage FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if row is None:
                        raise KeyError(run_id)
                    require_transition(row["status"], "cancelled")
                    connection.execute("UPDATE jobs SET status='failed',error_code='RUN_CANCELLED',error_message='cancelled by operator',worker_id=NULL,finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    connection.execute("UPDATE stage_runs SET status='cancelled',worker_id=NULL,pause_after_item=0,finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    connection.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE run_id=? AND status='running'", (now, run_id))
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise RunStateConflict("running run changed during cancel")
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), "RUN_CANCELLED", run_id, "{}", now))
        return self.get_run(run_id)

    def retry_failed(self, run_id: str) -> dict[str, Any]:
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if row is None:
                        raise KeyError(run_id)
                    require_transition(row["status"], "pending")
                    provider_failed = connection.execute(
                        "SELECT error_code FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND status='failed'",
                        (run_id,),
                    ).fetchall()
                    if any(
                        normalize_provider_error_code(item["error_code"]) in FATAL_PROVIDER_ERROR_CODES
                        or normalize_provider_error_code(item["error_code"]) in ITEM_LOCAL_PROVIDER_ERROR_CODES
                        or item["error_code"] in {"PROVIDER_STORAGE_UNSUPPORTED", "PROVIDER_CANCELLED"}
                        for item in provider_failed
                    ):
                        raise R3Error("PROVIDER_RETRY_REQUIRED")
                    connection.execute("UPDATE jobs SET status='pending',error_code=NULL,error_message=NULL,worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND status='failed'", (run_id,))
                    connection.execute("UPDATE stage_runs SET status='pending',worker_id=NULL,recovery_attempt=0,pause_after_item=0,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND status='failed'", (run_id,))
                    connection.execute("UPDATE runs SET status='pending',finished_at=NULL WHERE run_id=? AND status='failed'", (run_id,))
                    if connection.execute("SELECT changes()").fetchone()[0] != 1:
                        raise RunStateConflict("failed run changed during retry")
                    connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), "RUN_RETRY_FAILED", run_id, "{}", now))
        return self.get_run(run_id)

    def preview_provider_retry_wave(self, run_id: str) -> dict[str, Any]:
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                with database.read_transaction():
                    run = database.fetchone("SELECT effective_config_hash FROM runs WHERE run_id=?", (run_id,))
                    if run is None:
                        raise R3Error("RUN_NOT_FOUND")
                    plans: list[dict[str, Any]] = []
                    for row in database.fetchall(
                        "SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND status IN ('needs_review','failed') ORDER BY logical_key,job_id",
                        (run_id,),
                    ):
                        cursor = _load_object(row["cursor_json"])
                        existing_child = self.worker._retry_child_for_source(database.connection, run_id, row["job_id"])
                        if existing_child is not None:
                            continue
                        code = normalize_provider_error_code(row["error_code"])
                        if code not in ITEM_LOCAL_PROVIDER_ERROR_CODES:
                            continue
                        try:
                            plans.append(self.worker.provider_retry_spec(database, run_id, row["job_id"]))
                        except (OSError, KeyError, TypeError, ValueError, StageFailure) as exc:
                            raise R3Error("AI_BATCH_INPUT_INVALID") from exc
                    jobs = [
                        {
                            "source_job_id": item["source_job_id"],
                            "child_job_id": item["child_job_id"],
                            "source_input_signature": item["source_input_signature"],
                            "child_input_signature": item["child_input_signature"],
                        }
                        for item in plans
                    ]
                    return {
                        "run_id": run_id,
                        "effective_config_hash": run["effective_config_hash"],
                        "wave_hash": build_retry_wave_hash(run_id, run["effective_config_hash"], jobs),
                        "count": len(plans),
                        "jobs": plans,
                        "approved": False,
                    }

    preview_retry_wave = preview_provider_retry_wave
    preview_bulk_retry = preview_provider_retry_wave
    preview_retry_wave_plan = preview_provider_retry_wave

    def bulk_retry_provider_jobs(
        self,
        run_id: str,
        *,
        approve: bool = False,
        submitted_hash: str | None = None,
    ) -> dict[str, Any]:
        """Create one deterministic retry child per failed source job.

        Creation is separate from approval by default.  A caller that has
        shown the returned wave to the user may pass ``approve=True`` and the
        submitted hash to approve the unchanged wave atomically.
        """

        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                with database.transaction() as connection:
                    run = connection.execute("SELECT effective_config_hash FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if run is None:
                        raise R3Error("RUN_NOT_FOUND")
                    if approve and isinstance(submitted_hash, str):
                        approved_details = _find_retry_wave_audit(connection, run_id, "AI_PROVIDER_RETRY_WAVE_APPROVED", submitted_hash)
                        if approved_details is not None:
                            jobs = approved_details.get("jobs", []) if isinstance(approved_details.get("jobs", []), list) else []
                            return {
                                "run_id": run_id,
                                "wave_hash": submitted_hash,
                                "count": int(approved_details.get("job_count", len(jobs))),
                                "jobs": jobs,
                                "approved": True,
                                "idempotent": True,
                            }
                        created_details = _find_retry_wave_audit(connection, run_id, "AI_PROVIDER_RETRY_WAVE_CREATED", submitted_hash)
                        if created_details is not None:
                            jobs = created_details.get("jobs", []) if isinstance(created_details.get("jobs", []), list) else []
                            if not jobs or not _retry_wave_children_match(connection, run_id, jobs):
                                raise R3Error("AI_RETRY_WAVE_CONFLICT")
                            now = utc_now()
                            for item in jobs:
                                child = connection.execute("SELECT cursor_json FROM jobs WHERE run_id=? AND job_id=?", (run_id, item["child_job_id"])).fetchone()
                                cursor = _load_object(child["cursor_json"])
                                cursor["approved"] = True
                                connection.execute("UPDATE jobs SET cursor_json=? WHERE run_id=? AND job_id=?", (canonical_json(cursor), run_id, item["child_job_id"]))
                            connection.execute(
                                "UPDATE stage_runs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND stage IN ('AI_ANNOTATE','VALIDATE','HUMAN_REVIEW') AND status != 'cancelled'",
                                (run_id,),
                            )
                            connection.execute(
                                "UPDATE runs SET status='pending',current_stage='AI_ANNOTATE',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND status NOT IN ('succeeded','cancelled')",
                                (run_id,),
                            )
                            connection.execute(
                                "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                                (_audit_id(), "AI_PROVIDER_RETRY_WAVE_APPROVED", run_id, canonical_json({"wave_hash": submitted_hash, "job_count": len(jobs), "jobs": jobs}), now),
                            )
                            for item in jobs:
                                connection.execute(
                                    "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                                    (_audit_id(), "AI_BATCH_APPROVED", run_id, item["child_job_id"], canonical_json({"wave_hash": submitted_hash}), now),
                                )
                            return {"run_id": run_id, "wave_hash": submitted_hash, "count": len(jobs), "jobs": jobs, "approved": True, "idempotent": False}
                    plans: list[dict[str, Any]] = []
                    for row in connection.execute(
                        "SELECT * FROM jobs WHERE run_id=? AND stage='AI_ANNOTATE' AND status IN ('needs_review','failed') ORDER BY logical_key,job_id",
                        (run_id,),
                    ).fetchall():
                        cursor = _load_object(row["cursor_json"])
                        existing_child = self.worker._retry_child_for_source(connection, run_id, row["job_id"])
                        if existing_child is not None:
                            continue
                        code = normalize_provider_error_code(row["error_code"])
                        if code not in ITEM_LOCAL_PROVIDER_ERROR_CODES:
                            continue
                        plans.append(self.worker.provider_retry_spec(database, run_id, row["job_id"]))
                    hash_jobs = [
                        {
                            "source_job_id": item["source_job_id"],
                            "child_job_id": item["child_job_id"],
                            "source_input_signature": item["source_input_signature"],
                            "child_input_signature": item["child_input_signature"],
                        }
                        for item in plans
                    ]
                    wave_hash = build_retry_wave_hash(run_id, run["effective_config_hash"], hash_jobs)
                    if approve and submitted_hash != wave_hash:
                        raise R3Error("AI_RETRY_WAVE_CONFLICT")
                    created: list[dict[str, Any]] = []
                    for item in plans:
                        created.append(
                            self.worker.create_provider_retry_job(
                                database,
                                run_id,
                                item["source_job_id"],
                                approve=approve,
                            )
                        )
                    hash_jobs = [
                        {
                            "source_job_id": plan["source_job_id"],
                            "child_job_id": child["job_id"],
                            "source_input_signature": plan["source_input_signature"],
                            "child_input_signature": child["input_signature"],
                        }
                        for plan, child in zip(plans, created)
                    ]
                    if not approve and created:
                        connection.execute(
                            "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                            (_audit_id(), "AI_PROVIDER_RETRY_WAVE_CREATED", run_id, canonical_json({"wave_hash": wave_hash, "job_count": len(created), "jobs": hash_jobs}), utc_now()),
                        )
                    if approve:
                        now = utc_now()
                        connection.execute(
                            "UPDATE stage_runs SET status='pending',worker_id=NULL,heartbeat_at=NULL,finished_at=NULL WHERE run_id=? AND stage IN ('AI_ANNOTATE','VALIDATE','HUMAN_REVIEW') AND status != 'cancelled'",
                            (run_id,),
                        )
                        connection.execute(
                            "UPDATE runs SET status='pending',current_stage='AI_ANNOTATE',boundary_event=NULL,finished_at=NULL WHERE run_id=? AND status NOT IN ('succeeded','cancelled')",
                            (run_id,),
                        )
                        connection.execute(
                            "INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)",
                            (_audit_id(), "AI_PROVIDER_RETRY_WAVE_APPROVED", run_id, canonical_json({"wave_hash": wave_hash, "job_count": len(created), "jobs": hash_jobs}), now),
                        )
                        for child in created:
                            cursor = _load_object(connection.execute("SELECT cursor_json FROM jobs WHERE job_id=?", (child["job_id"],)).fetchone()["cursor_json"])
                            cursor["approved"] = True
                            connection.execute("UPDATE jobs SET cursor_json=? WHERE job_id=?", (canonical_json(cursor), child["job_id"]))
                            connection.execute(
                                "INSERT INTO audit_events(event_id,event_type,run_id,job_id,details_json,created_at) VALUES (?,?,?,?,?,?)",
                                (_audit_id(), "AI_BATCH_APPROVED", run_id, child["job_id"], canonical_json({"wave_hash": wave_hash}), now),
                            )
                    return {"run_id": run_id, "wave_hash": wave_hash, "count": len(created), "jobs": created, "approved": approve}

    bulk_retry = bulk_retry_provider_jobs
    retry_provider_jobs = bulk_retry_provider_jobs

    def confirm_provider_retry_wave(self, run_id: str, submitted_hash: str) -> dict[str, Any]:
        return self.bulk_retry_provider_jobs(run_id, approve=True, submitted_hash=submitted_hash)

    confirm_retry_wave = confirm_provider_retry_wave

    def retry_provider_job(self, run_id: str, source_job_id: str, *, approve: bool = False) -> dict[str, Any]:
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                with database.transaction():
                    try:
                        result = self.worker.create_provider_retry_job(database, run_id, source_job_id, approve=approve)
                    except (KeyError, OSError, TypeError, ValueError, StageFailure) as exc:
                        raise R3Error("PROVIDER_RETRY_NOT_ELIGIBLE") from exc
                    return result

    retry_ai_job = retry_provider_job

    def recover(self, run_id: str, job_id: str | None = None, *, stage: str | None = None) -> dict[str, Any]:
        result = self.worker.recover(run_id, job_id, stage=stage)
        return {"recovered": result, "run": self.get_run(run_id)}

    def stale_markers(self, run_id: str | None = None) -> list[StaleMarker]:
        return self.worker.detect_stale(run_id)

    def tick(self, run_id: str) -> dict[str, Any]:
        return self.worker.tick(run_id)

    def query_workspace(self, run_id: str, query: str, *, limit: int = 24) -> list[dict[str, Any]]:
        with self.worker.open_database(run_id) as database:
            return [hit.to_dict() for hit in WorkspaceQueryService(database).query(query, limit=limit)]

    def _run_command(self, run_id: str, command: str) -> dict[str, Any]:
        pause_requested = False
        event = ""
        with self.worker.run_lock(run_id):
            with self.worker.open_database(run_id) as database:
                now = utc_now()
                with database.transaction() as connection:
                    row = connection.execute("SELECT status,current_stage,boundary_event FROM runs WHERE run_id=?", (run_id,)).fetchone()
                    if row is None:
                        raise KeyError(run_id)
                    if command == "pause":
                        require_transition(row["status"], "paused")
                        stage = connection.execute("SELECT stage,status FROM stage_runs WHERE run_id=? AND status='running' ORDER BY ordinal LIMIT 1", (run_id,)).fetchone()
                        running_job = connection.execute("SELECT 1 FROM jobs WHERE run_id=? AND status='running'", (run_id,)).fetchone()
                        if stage is not None and stage["stage"] == "EXTRACT_FEATURES" and running_job is not None:
                            connection.execute("UPDATE stage_runs SET pause_after_item=1 WHERE run_id=? AND stage=? AND status='running'", (run_id, stage["stage"]))
                            connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), "RUN_PAUSE_REQUESTED", run_id, json.dumps({"boundary": "after_item"}, sort_keys=True), now))
                            pause_requested = True
                            run_update = None
                        else:
                            run_update = connection.execute("UPDATE runs SET status='paused' WHERE run_id=? AND status='running'", (run_id,))
                            connection.execute("UPDATE stage_runs SET status='paused',worker_id=NULL,pause_after_item=0 WHERE run_id=? AND status='running'", (run_id,))
                            event = "RUN_PAUSED"
                    else:
                        if row["boundary_event"]:
                            raise RunStateConflict("R3 boundary has no Phase 1 resume handler")
                        if row["status"] != "paused":
                            raise RunStateConflict("resume requires ordinary paused run")
                        require_transition(row["status"], "running")
                        run_update = connection.execute("UPDATE runs SET status='running' WHERE run_id=? AND status='paused'", (run_id,))
                        connection.execute("UPDATE stage_runs SET status='pending',worker_id=NULL,pause_after_item=0 WHERE run_id=? AND status='paused'", (run_id,))
                        event = "RUN_RESUMED"
                    if not pause_requested:
                        if run_update is None or run_update.rowcount != 1:
                            raise RunStateConflict("run changed during state command")
                        connection.execute("INSERT INTO audit_events(event_id,event_type,run_id,details_json,created_at) VALUES (?,?,?,?,?)", (_audit_id(), event, run_id, "{}", now))
        return self.get_run(run_id)


ApplicationService = StudioService


def _audit_id() -> str:
    return "audit_" + uuid.uuid4().hex


def _stable_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha256("\0".join(canonical_json(part) for part in parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _load_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _has_plan_audit(connection: Any, run_id: str, plan_hash: str) -> bool:
    for row in connection.execute(
        "SELECT details_json FROM audit_events WHERE run_id=? AND event_type='AI_BATCH_PLAN_APPROVED'",
        (run_id,),
    ).fetchall():
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(details, dict) and details.get("plan_hash") == plan_hash:
            return True
    return False


def _find_retry_wave_audit(connection: Any, run_id: str, event_type: str, wave_hash: str) -> dict[str, Any] | None:
    rows = connection.execute(
        "SELECT details_json FROM audit_events WHERE run_id=? AND event_type=? ORDER BY created_at DESC,event_id DESC",
        (run_id, event_type),
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(details, dict) and details.get("wave_hash") == wave_hash:
            return details
    return None


def _retry_wave_children_match(connection: Any, run_id: str, jobs: list[Any]) -> bool:
    for item in jobs:
        if not isinstance(item, dict):
            return False
        required = {"source_job_id", "child_job_id", "source_input_signature", "child_input_signature"}
        if not required <= set(item):
            return False
        child = connection.execute(
            "SELECT input_signature,status,cursor_json FROM jobs WHERE run_id=? AND job_id=? AND stage='AI_ANNOTATE'",
            (run_id, item["child_job_id"]),
        ).fetchone()
        source = connection.execute(
            "SELECT input_signature FROM jobs WHERE run_id=? AND job_id=? AND stage='AI_ANNOTATE'",
            (run_id, item["source_job_id"]),
        ).fetchone()
        if child is None or source is None or child["status"] != "pending":
            return False
        cursor = _load_object(child["cursor_json"])
        if (
            source["input_signature"] != item["source_input_signature"]
            or child["input_signature"] != item["child_input_signature"]
            or cursor.get("retry_of_job_id") != item["source_job_id"]
        ):
            return False
    return True


def _review_evidence_values(value: Any) -> list[Any]:
    try:
        parsed = json.loads(value or "[]") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return list(parsed)
    if parsed is None:
        return []
    return [parsed]


def _review_annotation_ids(value: Any) -> list[str]:
    return [
        item.removeprefix("annotation:")
        for item in _review_evidence_values(value)
        if isinstance(item, str) and item.startswith("annotation:") and len(item) > len("annotation:")
    ]


def _merge_review_evidence(existing: Any, human_evidence: list[str]) -> list[Any]:
    merged: list[Any] = []
    for item in _review_evidence_values(existing) + list(human_evidence):
        if item not in merged:
            merged.append(item)
    return merged[:64]


def _contains_machine_field(value: Any) -> bool:
    forbidden = {
        "block_id",
        "variant_id",
        "state_id",
        "legal_state",
        "default_state_id",
        "geometry",
        "shape",
        "collision",
        "machine_facts",
        "behavior",
        "behaviors",
        "transparent",
        "emissive",
        "support",
        "waterloggable",
        "redstone_related",
        "image",
        "image_path",
        "image_sha256",
        "minecraft_version",
        "publish_status",
        "release_status",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            name = str(key)
            normalized = name
            for prefix in ("set_", "add_", "remove_"):
                if normalized.startswith(prefix):
                    normalized = normalized[len(prefix) :]
            if name in forbidden or normalized in forbidden or _contains_machine_field(item):
                return True
        return False
    if isinstance(value, list):
        return any(_contains_machine_field(item) for item in value)
    return False


def _probe_png() -> bytes:
    return encode_rgba_png(1, 1, b"\xff\x00\x00\xff")


def check_import(service: StudioService, source_directory: str | Path, minecraft_version: str) -> ImportCheck:
    return service.check_import(source_directory, minecraft_version)


def import_checked(service: StudioService, check_id: str, *, copy_mode: str = "copy_to_workspace") -> dict[str, Any]:
    return service.import_checked(check_id, copy_mode=copy_mode)
