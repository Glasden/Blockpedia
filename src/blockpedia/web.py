"""Loopback Index Studio HTTP, template, and HTMX adapter for R2."""

from __future__ import annotations

import asyncio
import html
import json
import re
import math
import unicodedata
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence
from urllib.parse import parse_qs

from fastapi import FastAPI, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import http_exception_handler as default_http_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from .directory_chooser import DirectoryChooserError, DirectoryPathUnsafe, DirectoryRefNotFound, DirectoryRefStale
from .importer import ImportCheckInProgress, ImportCheckNotFound, ImportCheckProgressPersistFailed, ImportNotAllowed
from .paths import (
    ExportPathError,
    RELEASE_BUILD_ID_RE,
    RELEASE_CHECK_ID_RE,
    RELEASE_ID_RE,
    UnsafeReference,
    validate_minecraft_version,
)
from .provider import ProviderProfile, sanitize_validation_diagnostic
from .r3 import is_sensitive_review_text
from .services import R3Error, StudioService
from .stages import R2_STAGES, STUDIO_STAGES, RunStateConflict
from .worker import ITEM_LOCAL_PROVIDER_ERROR_CODES, normalize_provider_error_code


PACKAGE_ROOT = Path(__file__).resolve().parent
UNOFFICIAL_NOTICE = (
    "NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH "
    "MOJANG OR MICROSOFT."
)

STATUS_LABELS = {
    "pending": "等待中",
    "running": "进行中",
    "paused": "已暂停",
    "needs_review": "需处理",
    "failed": "失败",
    "succeeded": "已完成",
    "cancelled": "已取消",
    "skipped": "已跳过",
    "open": "待处理",
}

STAGE_META: dict[str, dict[str, str | bool]] = {
    "PREPARE": {"label": "准备环境", "detail": "核对锁定工具链与输入边界", "phase": "R2", "future": False},
    "IMPORT_EXPORT": {"label": "导入快照", "detail": "复制已检查的 exporter 产物", "phase": "R2", "future": False},
    "VALIDATE_REGISTRY": {"label": "核对注册表", "detail": "确认完整方块登记与版本一致", "phase": "R2", "future": False},
    "VALIDATE_VARIANTS": {"label": "核对视觉变体", "detail": "只验证 exporter 已选代表，不重新选择", "phase": "R2", "future": False},
    "VALIDATE_RENDERS": {"label": "核对渲染", "detail": "只验证已有图片与摘要，不重新渲染", "phase": "R2", "future": False},
    "EXTRACT_FEATURES": {"label": "提取离线特征", "detail": "生成确定性颜色、几何与检索特征", "phase": "R2", "future": False},
    "AI_ANNOTATE": {"label": "AI 标注", "detail": "预览并批准受控语义批次", "phase": "R3", "future": False},
    "VALIDATE": {"label": "语义验证", "detail": "校验 Schema、编号与机器事实边界", "phase": "R3", "future": False},
    "HUMAN_REVIEW": {"label": "人工审核", "detail": "处理异常、低置信度与人工覆盖", "phase": "R3", "future": False},
    "BUILD_RELEASE": {"label": "构建候选", "detail": "R3 在候选构建边界停止", "phase": "R3", "future": True},
    "ACTIVATE_RELEASE": {"label": "激活发布", "detail": "后续 R5 阶段", "phase": "R5", "future": True},
}

AUDIT_STEP_LABELS = {
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
    "R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING": "停在候选构建边界",
    "AI_BATCH_APPROVAL_REQUIRED": "等待 AI 批次批准",
    "AI_BATCH_PLAN_APPROVED": "AI 批次计划已批准",
    "AI_BATCH_SUCCEEDED": "AI 批次完成",
    "AI_BATCH_FAILED": "AI 批次失败",
    "AI_BATCH_CANCELLED": "AI 批次已取消",
    "AI_PROVIDER_RETRY_CREATED": "已创建 Provider 重试批次",
    "AI_PROVIDER_RETRY_WAVE_APPROVED": "Provider 重试波次已批准",
    "REVIEW_ACCEPTED": "审核已接受",
    "REVIEW_EDITED": "审核已编辑",
    "REVIEW_SKIPPED": "审核已跳过",
    "REVIEW_REEXPORT_REQUESTED": "已请求 exporter 重新导出",
    "REVIEW_RERENDER_REQUESTED": "已请求 exporter 重新渲染",
    "REVIEW_AI_RETRY_REQUESTED": "已请求 AI 重试",
    "HUMAN_REVIEW_REQUIRED": "等待人工审核",
    "HUMAN_REVIEW_SUCCEEDED": "人工审核完成",
    "IMPORT_CHECKED_AND_PROJECTED": "导入检查完成",
}


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ImportCheckRequest(StrictRequest):
    source_directory: str = Field(min_length=1, max_length=4096)
    minecraft_version: str = Field(min_length=3, max_length=32)

    @field_validator("minecraft_version")
    @classmethod
    def valid_minecraft_version(cls, value: str) -> str:
        return validate_minecraft_version(value)


class ImportRequest(StrictRequest):
    check_id: str = Field(min_length=1, max_length=160)
    copy_mode: Literal["copy_to_workspace"]


class ReleaseCheckRequest(StrictRequest):
    run_id: str = Field(min_length=1, max_length=160)
    minecraft_version: str = Field(min_length=3, max_length=32)

    @field_validator("minecraft_version")
    @classmethod
    def valid_minecraft_version(cls, value: str) -> str:
        return validate_minecraft_version(value)


class ReleaseBuildRequest(StrictRequest):
    check_id: str = Field(min_length=1, max_length=160)
    confirm_immutable_release: bool

    @field_validator("confirm_immutable_release")
    @classmethod
    def require_true_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("confirm_immutable_release must be true")
        return value


class RecoverRequest(StrictRequest):
    job_id: str | None = Field(default=None, min_length=1, max_length=160)
    stage: str | None = Field(default=None, min_length=1, max_length=64)

    @field_validator("stage")
    @classmethod
    def valid_stage(cls, value: str | None) -> str | None:
        if value is not None and value not in STUDIO_STAGES:
            raise ValueError("unknown Studio stage")
        return value


class EmptyRequest(StrictRequest):
    pass


class ProviderStageRequest(StrictRequest):
    batch_size: int = Field(ge=1, le=16)
    concurrency: int = Field(ge=1, le=4)


class ProviderProfileRequest(StrictRequest):
    profile_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    adapter: Literal["openai_responses", "openai_chat_completions"]
    model_id: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2048)
    base_url_stable_id: str | None = Field(default=None, max_length=2048)
    secret_reference: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^(?:keyring:blockpedia/[a-z][a-z0-9_-]{0,63}|env:OPENAI_API_KEY)$",
    )
    enabled: bool = False
    capability_status: Literal["draft", "unverified", "verified", "failed"] = "unverified"
    prompt_version: str | None = Field(default=None, min_length=1, max_length=128)
    annotation_output_schema_id: Literal["annotation-batch-output.v1"] = "annotation-batch-output.v1"
    query_spec_output_schema_id: Literal["query-spec-output.v1"] = "query-spec-output.v1"
    rerank_output_schema_id: Literal["rerank-output.v1"] = "rerank-output.v1"
    search_ranking_version: Literal["search-ranking.v1"] = "search-ranking.v1"
    request_timeout_ms: int | None = Field(default=None, ge=1000, le=600000)
    stages: dict[str, ProviderStageRequest] | None = None


class ProviderProfileIDRequest(StrictRequest):
    profile_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class ConfigureRunRequest(StrictRequest):
    import_id: str = Field(min_length=1, max_length=160)
    minecraft_version: str = Field(min_length=3, max_length=32)
    profile_id: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    batch_size: int | None = Field(default=None, ge=8, le=16)
    normal_threshold: float = Field(default=0.80)
    high_threshold: float = Field(default=0.65)
    sample_rate: int = Field(default=100, ge=0, le=100)

    @field_validator("minecraft_version")
    @classmethod
    def valid_minecraft_version(cls, value: str) -> str:
        return validate_minecraft_version(value)


class AIBatchApproveRequest(StrictRequest):
    input_signature: str = Field(min_length=1, max_length=200)


class AIPlanApproveRequest(StrictRequest):
    plan_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ProviderRetryWaveApproveRequest(StrictRequest):
    wave_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class ProviderJobRetryRequest(StrictRequest):
    confirm: bool

    @field_validator("confirm")
    @classmethod
    def require_true_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("confirm must be true")
        return value


class AIBatchCancelRequest(StrictRequest):
    reason: str = Field(default="operator cancelled", min_length=2, max_length=500)


class ReviewResolveRequest(StrictRequest):
    run_id: str | None = Field(default=None, min_length=1, max_length=160)
    decision: Literal[
        "accept",
        "edit_and_accept",
        "skip",
        "request_reexport",
        "request_exporter_rerender",
        "retry_ai",
    ]
    reviewer: str = Field(min_length=1, max_length=128)
    reason_code: str | None = Field(default=None, min_length=2, max_length=128)
    reason: str | None = Field(default=None, min_length=2, max_length=500)
    note: str = Field(min_length=2, max_length=500)
    evidence: list[str] = Field(min_length=1, max_length=64)
    override: dict[str, Any] | None = None


class WorkerUnavailable(RuntimeError):
    code = "WORKER_UNAVAILABLE"


def create_app(
    data_root: str | Path | None = None,
    *,
    repo_root: str | Path | None = None,
    service: StudioService | None = None,
    start_worker: bool = True,
) -> FastAPI:
    """Create the local Studio app without exposing host or port controls."""

    owns_service = service is None
    studio = service or StudioService(
        data_root,
        repo_root=Path(repo_root) if repo_root is not None else None,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # This probe is deliberately first and read-only.  Recovery remains a
        # separate, explicit WebUI action.
        markers = await run_in_threadpool(studio.stale_markers)
        app.state.stale_markers = [_marker_dict(marker) for marker in markers]
        app.state.worker_expected = start_worker
        app.state.worker_available = False
        app_started_worker = False
        app.state.app_started_worker = False
        if not owns_service:
            # Injected services are entirely caller-managed.  The app only
            # observes their worker state and never starts or owns it.
            app.state.worker_available = _worker_is_running(studio.worker)
        elif start_worker:
            try:
                app_started_worker = studio.worker.start() is True
                app.state.app_started_worker = app_started_worker
                app.state.worker_available = _worker_is_running(studio.worker)
            except Exception:
                # The read-only UI remains available and write routes report a
                # stable 503 rather than leaking a startup exception.
                app.state.worker_available = False
        else:
            app.state.worker_available = _worker_is_running(studio.worker)
        try:
            yield
        finally:
            if owns_service:
                if app_started_worker:
                    studio.worker.stop()
                studio.close()

    app = FastAPI(
        title="Blockpedia Index Studio",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.service = studio
    app.state.stale_markers = []
    app.state.worker_expected = start_worker
    app.state.worker_available = False
    app.state.app_started_worker = False

    templates = Jinja2Templates(directory=str(PACKAGE_ROOT / "templates"))
    templates.env.globals.update(
        status_labels=STATUS_LABELS,
        stage_meta=STAGE_META,
        stage_order=STUDIO_STAGES,
        r2_stages=R2_STAGES,
        unofficial_notice=UNOFFICIAL_NOTICE,
    )
    app.mount("/static", StaticFiles(directory=str(PACKAGE_ROOT / "static")), name="static")
    app.state.templates = templates

    @app.middleware("http")
    async def local_request_id(request: Request, call_next):
        request.state.request_id = "web_" + uuid.uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        if request.url.path == "/api/provider/profile":
            return _error_response(
                request,
                400,
                "PROVIDER_CONFIG_INVALID",
                "provider profile 配置不合法。",
                field_errors=_validation_fields(exc.errors()),
            )
        return _error_response(
            request,
            400,
            "INVALID_INPUT",
            "请求字段不合法，请检查后重试。",
            field_errors=_validation_fields(exc.errors()),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path == "/api" or request.url.path.startswith("/api/"):
            if exc.status_code == 404:
                return _error_response(request, 404, "API_NOT_FOUND", "找不到指定 API route。")
            if exc.status_code == 405:
                return _error_response(request, 405, "METHOD_NOT_ALLOWED", "该 API route 不接受此请求方法。")
            return _error_response(request, exc.status_code, "HTTP_ERROR", "API 请求未完成。")
        return await default_http_exception_handler(request, exc)

    @app.exception_handler(ImportCheckNotFound)
    async def import_not_found(request: Request, _exc: ImportCheckNotFound):
        if request.url.path.startswith("/api/"):
            return _error_response(request, 404, "IMPORT_NOT_FOUND", "导入检查不存在或已失效，请重新检查。")
        return HTMLResponse("导入检查不存在或已失效。", status_code=404)

    @app.exception_handler(ImportNotAllowed)
    async def import_incomplete(request: Request, _exc: ImportNotAllowed):
        code = getattr(_exc, "code", None) or "IMPORT_INCOMPLETE"
        status = 409 if code == "IMPORT_CHECK_IN_PROGRESS" else 422
        message = "完整性检查仍在进行，请等待完成。" if code == "IMPORT_CHECK_IN_PROGRESS" else "导出包未通过完整性检查，请修复后重新检查。"
        return _error_response(request, status, code, message)

    @app.exception_handler(ImportCheckInProgress)
    async def import_check_in_progress(request: Request, _exc: ImportCheckInProgress):
        return _error_response(request, 409, "IMPORT_CHECK_IN_PROGRESS", "完整性检查仍在进行，请等待完成。")

    @app.exception_handler(ImportCheckProgressPersistFailed)
    async def import_check_persist_failed(request: Request, _exc: ImportCheckProgressPersistFailed):
        return _error_response(request, 500, "IMPORT_CHECK_PROGRESS_PERSIST_FAILED", "导入检查状态无法安全保存。")

    @app.exception_handler(DirectoryRefNotFound)
    async def directory_ref_not_found(request: Request, _exc: DirectoryRefNotFound):
        return _error_response(request, 404, "DIRECTORY_REF_NOT_FOUND", "目录引用已失效，请重新选择目录。")

    @app.exception_handler(DirectoryRefStale)
    async def directory_ref_stale(request: Request, _exc: DirectoryRefStale):
        return _error_response(request, 409, "DIRECTORY_REF_STALE", "目录已发生变化，请重新选择目录。")

    @app.exception_handler(DirectoryPathUnsafe)
    async def directory_path_unsafe(request: Request, _exc: DirectoryPathUnsafe):
        return _error_response(request, 400, "DIRECTORY_PATH_UNSAFE", "目录不在允许的导出根目录内。")

    @app.exception_handler(DirectoryChooserError)
    async def directory_chooser_error(request: Request, exc: DirectoryChooserError):
        code = getattr(exc, "code", "DIRECTORY_REF_INVALID")
        return _error_response(request, 400, code, "目录引用不合法，请重新选择目录。")

    @app.exception_handler(RunStateConflict)
    async def run_conflict(request: Request, _exc: RunStateConflict):
        return _error_response(request, 409, "RUN_STATE_CONFLICT", "当前运行状态不允许此操作，请刷新后重试。")

    @app.exception_handler(R3Error)
    async def r3_error(request: Request, exc: R3Error):
        status, code, message, retryable, fields = _exception_details(exc)
        return _error_response(
            request,
            status,
            code,
            message,
            field_errors=fields,
            retryable=retryable,
        )

    @app.exception_handler(KeyError)
    async def run_not_found(request: Request, _exc: KeyError):
        return _error_response(request, 404, "RUN_NOT_FOUND", "找不到指定运行。")

    @app.exception_handler(WorkerUnavailable)
    async def worker_unavailable(request: Request, _exc: WorkerUnavailable):
        return _error_response(
            request,
            503,
            "WORKER_UNAVAILABLE",
            "内置 Worker 当前不可用。请重启 Index Studio 后再试。",
            retryable=True,
        )

    @app.exception_handler(ExportPathError)
    @app.exception_handler(UnsafeReference)
    @app.exception_handler(ValueError)
    async def invalid_input(request: Request, _exc: Exception):
        return _error_response(request, 400, "INVALID_INPUT", "输入不合法，请检查版本和本地目录后重试。")

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        if getattr(exc, "code", None) == "WORKER_UNAVAILABLE":
            return _error_response(
                request,
                503,
                "WORKER_UNAVAILABLE",
                "内置 Worker 当前不可用。请重启 Index Studio 后再试。",
                retryable=True,
            )
        return _error_response(request, 500, "INTERNAL_ERROR", "本地操作未完成。请按请求编号检查诊断信息。")

    # JSON API -----------------------------------------------------------

    @app.get("/api/directories")
    def api_directories(
        request: Request,
        minecraft_version: str = Query(min_length=3, max_length=32),
        parent_ref: str | None = Query(default=None, min_length=1, max_length=160),
    ):
        _allow_query_keys(request, {"minecraft_version", "parent_ref"})
        data = studio.list_directories(minecraft_version, parent_ref)
        checks = {
            (item.get("minecraft_version"), item.get("export_id")): item
            for item in studio.imports.list_checks(minecraft_version, limit=100)
        }
        for entry in data.get("entries", []):
            check = checks.get((minecraft_version, entry.get("export_id")))
            entry["check_marker"] = (
                {
                    "status": check.get("status"),
                    "check_id": check.get("check_id"),
                    "check_url": check.get("check_url"),
                    "updated_at": check.get("updated_at"),
                }
                if check is not None
                else None
            )
        return _success_response(request, data)

    @app.post("/api/imports/check")
    def api_check_import(payload: ImportCheckRequest, request: Request):
        _allow_query_keys(request, set())
        checked = studio.start_import_check(payload.source_directory, payload.minecraft_version)
        shaped = _shape_import_check(checked)
        shaped["reused"] = bool(getattr(checked, "reused", False))
        shaped["response_status"] = int(getattr(checked, "response_status", 202))
        return _success_response(request, shaped, status_code=shaped["response_status"])

    @app.post("/api/imports")
    def api_import(payload: ImportRequest, request: Request):
        _allow_query_keys(request, set())
        _require_worker(request)
        imported = studio.import_checked(payload.check_id, copy_mode=payload.copy_mode)
        return _success_response(request, _shape_import_result(imported))

    @app.get("/api/imports/checks/{check_id}")
    def api_import_check(check_id: str, request: Request):
        _allow_query_keys(request, set())
        return _success_response(request, _shape_import_check(studio.get_import_check(check_id)))

    @app.get("/api/imports/checks")
    def api_import_checks(
        request: Request,
        minecraft_version: str | None = Query(default=None, min_length=3, max_length=32),
        limit: int = Query(default=20, ge=1, le=100),
    ):
        _allow_query_keys(request, {"minecraft_version", "limit"})
        if minecraft_version is not None:
            validate_minecraft_version(minecraft_version)
        return _success_response(
            request,
            {
                "minecraft_version": minecraft_version,
                "checks": studio.imports.list_checks(minecraft_version, limit=limit),
            },
        )

    @app.get("/api/runs")
    def api_runs(
        request: Request,
        minecraft_version: str | None = Query(default=None, min_length=3, max_length=32),
    ):
        _allow_query_keys(request, {"minecraft_version"})
        if minecraft_version is not None:
            validate_minecraft_version(minecraft_version)
        runs = [_shape_run_summary(run) for run in studio.list_runs(minecraft_version)]
        return _success_response(request, {"minecraft_version": minecraft_version, "runs": runs})

    @app.post("/api/runs")
    def api_configure_run(payload: ConfigureRunRequest, request: Request):
        _allow_query_keys(request, set())
        configured = studio.configure_run(
            payload.import_id,
            payload.minecraft_version,
            profile_id=payload.profile_id,
            batch_size=payload.batch_size,
            normal_threshold=payload.normal_threshold,
            high_threshold=payload.high_threshold,
            sample_rate=payload.sample_rate,
        )
        return _success_response(request, configured, status_code=202)

    @app.post("/api/releases/check")
    def api_check_release(payload: ReleaseCheckRequest, request: Request):
        _allow_query_keys(request, set())
        checked = getattr(studio, "check_candidate_release")(payload.run_id, payload.minecraft_version)
        return _success_response(request, _shape_release_check(checked), data_sanitizer=_release_data_passthrough)

    @app.post("/api/releases/build")
    def api_build_release(payload: ReleaseBuildRequest, request: Request):
        _allow_query_keys(request, set())
        built = getattr(studio, "build_candidate_release")(
            payload.check_id,
            confirm_immutable_release=payload.confirm_immutable_release,
        )
        return _success_response(
            request,
            _shape_release_build(built),
            data_sanitizer=_release_data_passthrough,
            status_code=201,
        )

    @app.get("/api/provider/profile")
    def api_provider_profile(request: Request, profile_id: str | None = Query(default=None, min_length=1, max_length=64)):
        _allow_query_keys(request, {"profile_id"})
        profiles = []
        for profile in studio.list_provider_profiles():
            profiles.append(
                _provider_profile_view(
                    studio,
                    profile,
                    credential_status=studio.provider_secret_status(str(profile["profile_id"])),
                )
            )
        selected = None
        if profile_id is not None:
            selected = next((item for item in profiles if item.get("profile_id") == profile_id), None)
            if selected is None:
                raise R3Error("PROVIDER_PROFILE_NOT_FOUND")
        active = next((item for item in profiles if item.get("enabled") is True), None)
        if selected is None:
            selected = active or (profiles[0] if profiles else None)
        return _success_response(
            request,
            {
                "profile": selected,
                "profiles": profiles,
                "active_profile_id": active.get("profile_id") if active else None,
            },
        )

    @app.put("/api/provider/profile")
    def api_save_provider_profile(payload: ProviderProfileRequest, request: Request):
        _allow_query_keys(request, set())
        profile = _provider_profile_from_request(studio, payload)
        saved = studio.save_provider_profile(profile)
        shaped = _provider_profile_view(
            studio,
            saved,
            credential_status=studio.provider_secret_status(payload.profile_id),
        )
        return _success_response(request, {"profile": shaped})

    @app.post("/api/provider/probe")
    async def api_probe_provider(payload: ProviderProfileIDRequest, request: Request):
        _allow_query_keys(request, set())
        result = await run_in_threadpool(studio.probe_provider, payload.profile_id)
        return _success_response(request, _shape_provider_probe(studio, result))

    @app.post("/api/provider/enable")
    def api_enable_provider(payload: ProviderProfileIDRequest, request: Request):
        _allow_query_keys(request, set())
        result = studio.enable_provider(payload.profile_id)
        return _success_response(request, {"profile": _provider_profile_view(studio, result)})

    @app.post("/api/provider/disable")
    def api_disable_provider(payload: ProviderProfileIDRequest, request: Request):
        _allow_query_keys(request, set())
        result = studio.disable_provider(payload.profile_id)
        return _success_response(request, {"profile": _provider_profile_view(studio, result)})

    @app.get("/api/runs/{run_id}/ai-batches/plan")
    def api_ai_batch_plan(run_id: str, request: Request):
        _allow_query_keys(request, set())
        return _success_response(request, _shape_ai_plan(studio.preview_ai_plan(run_id)))

    @app.get("/api/runs/{run_id}/ai-batches/plan/{logical_key}/preview")
    def api_ai_batch_plan_preview(run_id: str, logical_key: str, request: Request):
        _allow_query_keys(request, set())
        preview = _shape_ai_preview(studio.preview_ai_batch(run_id, logical_key))
        safe_run_id = _safe_identifier(run_id, optional=True)
        safe_logical_key = _safe_identifier(logical_key, optional=True)
        preview["image_url"] = (
            f"/api/runs/{safe_run_id}/ai-batches/{safe_logical_key}/image"
            if safe_run_id and safe_logical_key
            else None
        )
        return _success_response(request, preview, data_sanitizer=_preview_data_passthrough)

    @app.post("/api/runs/{run_id}/ai-batches/plan/approve")
    def api_approve_ai_batch_plan(run_id: str, payload: AIPlanApproveRequest, request: Request):
        _allow_query_keys(request, set())
        approved = studio.approve_ai_plan(run_id, plan_hash=payload.plan_hash)
        return _success_response(request, _shape_ai_plan(approved, include_result_meta=True), status_code=202)

    @app.get("/api/runs/{run_id}/ai-batches/retry-wave")
    def api_provider_retry_wave(run_id: str, request: Request):
        _allow_query_keys(request, set())
        return _success_response(request, _shape_retry_wave(studio.preview_provider_retry_wave(run_id)))

    @app.post("/api/runs/{run_id}/ai-batches/retry-wave/approve")
    def api_approve_provider_retry_wave(run_id: str, payload: ProviderRetryWaveApproveRequest, request: Request):
        _allow_query_keys(request, set())
        approved = studio.confirm_provider_retry_wave(run_id, payload.wave_hash)
        return _success_response(request, _shape_retry_wave(approved, include_result_meta=True), status_code=202)

    @app.post("/api/runs/{run_id}/ai-batches/jobs/{source_job_id}/retry")
    def api_retry_provider_job(
        run_id: str,
        source_job_id: str,
        payload: ProviderJobRetryRequest,
        request: Request,
    ):
        _allow_query_keys(request, set())
        retried = studio.retry_provider_job(run_id, source_job_id, approve=True)
        return _success_response(
            request,
            _shape_retry_job(retried, run_id=run_id, include_result_meta=True),
            status_code=202,
        )

    @app.get("/api/runs/{run_id}/ai-batches/next")
    def api_next_ai_batch(run_id: str, request: Request):
        _allow_query_keys(request, set())
        preview = studio.preview_ai_batch(run_id)
        shaped = _shape_ai_preview(preview)
        shaped["image_url"] = f"/api/runs/{_safe_identifier(run_id)}/ai-batches/{_safe_identifier(preview.get('logical_key'))}/image"
        return _success_response(request, shaped, data_sanitizer=_preview_data_passthrough)

    @app.get("/api/runs/{run_id}/ai-batches/{logical_key}/image")
    def api_ai_batch_image(run_id: str, logical_key: str, request: Request):
        _allow_query_keys(request, set())
        preview = studio.preview_ai_batch(run_id, logical_key)
        image = preview.get("contact_sheet_png")
        if not isinstance(image, (bytes, bytearray)) or not image:
            raise R3Error("AI_BATCH_INPUT_INVALID")
        return StreamingResponse(iter((bytes(image),)), media_type="image/png", headers={"Cache-Control": "no-store"})

    @app.post("/api/runs/{run_id}/ai-batches/{logical_key}/approve")
    def api_approve_ai_batch(run_id: str, logical_key: str, payload: AIBatchApproveRequest, request: Request):
        _allow_query_keys(request, set())
        approved = studio.approve_ai_batch(run_id, logical_key, payload.input_signature)
        return _success_response(request, approved, status_code=202)

    @app.post("/api/runs/{run_id}/ai-batches/{logical_key}/cancel")
    def api_cancel_ai_batch(run_id: str, logical_key: str, request: Request, payload: AIBatchCancelRequest | None = None):
        _allow_query_keys(request, set())
        cancelled = studio.cancel_ai_batch(run_id, logical_key, reason=(payload.reason if payload else "operator cancelled"))
        return _success_response(request, cancelled)

    @app.get("/api/reviews")
    def api_reviews(
        request: Request,
        run_id: str | None = Query(default=None, min_length=1, max_length=160),
        severity: str | None = Query(default=None, min_length=1, max_length=16),
        status: str = Query(default="open", min_length=1, max_length=16),
        limit: int = Query(default=50, ge=1, le=200),
    ):
        _allow_query_keys(request, {"run_id", "severity", "status", "limit"})
        if run_id is not None:
            reviews = studio.list_reviews(run_id, severity=severity, status=status, limit=limit)
        else:
            reviews = []
            for candidate in _review_run_ids(studio):
                remaining = max(0, limit - len(reviews))
                if not remaining:
                    break
                reviews.extend(studio.list_reviews(candidate, severity=severity, status=status, limit=remaining))
        return _success_response(request, {"run_id": _safe_identifier(run_id, optional=True), "reviews": reviews[:limit], "limit": limit})

    @app.post("/api/reviews/{review_id}/resolve")
    def api_resolve_review(review_id: str, payload: ReviewResolveRequest, request: Request):
        _allow_query_keys(request, {"run_id"})
        run_id = payload.run_id or request.query_params.get("run_id")
        if run_id is None:
            run_id = _find_review_run(studio, review_id)
        if run_id is None or len(run_id) > 160:
            raise R3Error("INVALID_INPUT")
        resolved = studio.resolve_review(
            run_id,
            review_id,
            decision=payload.decision,
            reviewer=payload.reviewer,
            reason_code=payload.reason_code,
            reason=payload.reason,
            note=payload.note,
            evidence=payload.evidence,
            override=payload.override,
        )
        return _success_response(request, resolved)

    @app.post("/api/runs/{run_id}/reviews/continue")
    def api_continue_reviews(run_id: str, request: Request, _payload: EmptyRequest | None = None):
        _allow_query_keys(request, set())
        return _success_response(request, _shape_run(studio.continue_review(run_id)), status_code=202)

    @app.get("/api/runs/{run_id}/search")
    def api_workspace_search(
        run_id: str,
        request: Request,
        query: str = Query(min_length=1, max_length=240),
        limit: int = Query(default=24, ge=1, le=100),
    ):
        _allow_query_keys(request, {"query", "limit"})
        hits = studio.query_workspace(run_id, query, limit=limit)
        return _success_response(
            request,
            {"run_id": _safe_identifier(run_id), "limit": limit, "results": _shape_search_hits(hits)},
        )

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str, request: Request):
        _allow_query_keys(request, set())
        return _success_response(request, _shape_run(studio.get_run(run_id)))

    @app.get("/api/runs/{run_id}/events")
    async def api_run_events(run_id: str, request: Request):
        _allow_query_keys(request, set())
        # Resolve before constructing StreamingResponse so an unknown run is
        # an ordinary 404 envelope rather than a late generator failure.
        initial = _shape_run(studio.get_run(run_id))
        return StreamingResponse(
            _run_event_stream(studio, run_id, request, templates, initial),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/imports/checks/{check_id}/events")
    async def api_import_check_events(check_id: str, request: Request):
        _allow_query_keys(request, set())
        checked = _shape_import_check(studio.get_import_check(check_id))
        return StreamingResponse(
            _import_event_stream(studio, check_id, request, templates, checked),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/runs/{run_id}/recover")
    def api_recover(run_id: str, request: Request, payload: RecoverRequest | None = None):
        _allow_query_keys(request, set())
        _require_worker(request)
        payload = payload or RecoverRequest()
        result = studio.recover(run_id, payload.job_id, stage=payload.stage)
        _remove_stale_marker(app, run_id, payload.job_id, payload.stage)
        return _success_response(
            request,
            {
                "recovered": _shape_recovered(result.get("recovered", {})),
                "run": _shape_run(result.get("run", {})),
            },
        )

    @app.post("/api/runs/{run_id}/{action}")
    def api_run_action(
        run_id: str,
        action: Literal["pause", "resume", "cancel", "retry-failed"],
        request: Request,
        _payload: EmptyRequest | None = None,
    ):
        _allow_query_keys(request, set())
        _require_worker(request)
        updated = _run_action(studio, run_id, action)
        return _success_response(request, _shape_run(updated))

    # Full HTML pages ----------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index_page(request: Request):
        try:
            runs = [_shape_run_summary(run) for run in studio.list_runs()]
        except Exception as exc:
            return _page_exception(templates, request, exc)
        counts = _run_counts(runs)
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context=_page_context(
                request,
                app,
                page_title="导入工作台",
                current_page="home",
                runs=runs[:5],
                counts=counts,
                recent_checks=studio.imports.list_checks(limit=5),
            ),
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, minecraft_version: str | None = None):
        try:
            if minecraft_version:
                validate_minecraft_version(minecraft_version)
            runs = [_shape_run_summary(run) for run in studio.list_runs(minecraft_version)]
        except Exception as exc:
            return _page_exception(templates, request, exc)
        return templates.TemplateResponse(
            request=request,
            name="runs.html",
            context=_page_context(
                request,
                app,
                page_title="运行记录",
                current_page="runs",
                runs=runs,
                selected_version=minecraft_version or "",
                counts=_run_counts(runs),
            ),
        )

    @app.get("/imports/checks/{check_id}", response_class=HTMLResponse)
    def import_check_page(check_id: str, request: Request):
        checked = _shape_import_check(studio.get_import_check(check_id))
        context = _page_context(
            request,
            app,
            page_title=f"导入检查 {checked['check_id']}",
            current_page="home",
            check=checked,
        )
        return _template_or_fallback(
            templates,
            request,
            "import_check_detail.html",
            context,
            _fallback_import_page(checked),
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: str, request: Request):
        try:
            run = _shape_run(studio.get_run(run_id))
        except Exception as exc:
            return _page_exception(templates, request, exc)
        return templates.TemplateResponse(
            request=request,
            name="run_detail.html",
            context=_page_context(
                request,
                app,
                page_title=f"运行 {run['run_id']}",
                current_page="runs",
                run=run,
                run_identifier=run["run_id"],
                run_events_url=f"/api/runs/{run['run_id']}/events",
            ),
        )

    @app.get("/provider", response_class=HTMLResponse)
    def provider_page(request: Request):
        try:
            profiles = _provider_profile_views(studio)
        except Exception as exc:
            return _page_exception(templates, request, exc)
        context = _page_context(
            request,
            app,
            page_title="AI Provider",
            current_page="provider",
            profiles=profiles,
            provider_profiles=profiles,
            active_profile_id=next((item["profile_id"] for item in profiles if item.get("enabled")), None),
        )
        return _template_or_fallback(
            templates,
            request,
            "provider.html",
            context,
            _fallback_provider_page(profiles),
        )

    @app.get("/runs/{run_id}/review", response_class=HTMLResponse)
    def review_page(run_id: str, request: Request):
        try:
            run = _shape_run(studio.get_run(run_id))
            reviews = studio.list_reviews(run_id)
        except Exception as exc:
            return _page_exception(templates, request, exc)
        context = _page_context(
            request,
            app,
            page_title=f"审核 {run['run_id']}",
            current_page="runs",
            run=run,
            reviews=reviews,
            run_identifier=run["run_id"],
        )
        return _template_or_fallback(
            templates,
            request,
            "review.html",
            context,
            _fallback_review_page(run, reviews),
        )

    # HTMX partials.  Every write route calls the same StudioService used by
    # the JSON adapter; templates contain no business state transitions.

    @app.post("/ui/imports/check", response_class=HTMLResponse)
    async def ui_check_import(request: Request):
        try:
            form = await _form_payload(request, {"source_directory", "minecraft_version"})
            payload = ImportCheckRequest.model_validate(form)
            checked = studio.start_import_check(payload.source_directory, payload.minecraft_version)
            # Enqueue immediately; the canonical page owns the persistent
            # progress view and does not hold the HTMX request open.
            return HTMLResponse(
                content="",
                status_code=202,
                headers={"HX-Redirect": f"/imports/checks/{checked.check_id}"},
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "重新检查目录与版本后再试。")

    @app.post("/ui/imports", response_class=HTMLResponse)
    async def ui_import(request: Request):
        try:
            _require_worker(request)
            form = await _form_payload(request, {"check_id", "copy_mode"})
            payload = ImportRequest.model_validate(form)
            imported = await run_in_threadpool(
                studio.import_checked,
                payload.check_id,
                copy_mode=payload.copy_mode,
            )
            return templates.TemplateResponse(
                request=request,
                name="partials/import_complete.html",
                context={"request": request, "result": _shape_import_result(imported)},
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "重新执行完整性检查后再导入。")

    @app.post("/ui/runs/{run_id}/recover", response_class=HTMLResponse)
    async def ui_recover(run_id: str, request: Request):
        try:
            _require_worker(request)
            form = await _form_payload(request, {"job_id", "stage", "return_view"})
            return_view = form.pop("return_view", "stale")
            clean_form = {key: value for key, value in form.items() if value}
            payload = RecoverRequest.model_validate(clean_form)
            result = await run_in_threadpool(
                studio.recover,
                run_id,
                payload.job_id,
                stage=payload.stage,
            )
            _remove_stale_marker(app, run_id, payload.job_id, payload.stage)
            shaped_run = _shape_run(result.get("run", {}))
            if return_view == "detail":
                return templates.TemplateResponse(
                    request=request,
                    name="partials/run_panel.html",
                    context={"request": request, "run": shaped_run},
                )
            return templates.TemplateResponse(
                request=request,
                name="partials/stale_recovered.html",
                context={
                    "request": request,
                    "run": shaped_run,
                    "remaining_stale": len(app.state.stale_markers),
                },
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "刷新状态，确认项目仍为 stale 后再恢复。")

    @app.post("/ui/runs/{run_id}/{action}", response_class=HTMLResponse)
    async def ui_run_action(run_id: str, action: str, request: Request):
        try:
            _require_worker(request)
            if action not in {"pause", "resume", "cancel", "retry-failed"}:
                raise ValueError("unknown run action")
            await _form_payload(request, set())
            updated = await run_in_threadpool(_run_action, studio, run_id, action)
            return templates.TemplateResponse(
                request=request,
                name="partials/run_panel.html",
                context={"request": request, "run": _shape_run(updated)},
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "刷新运行状态后再试。")

    @app.post("/ui/provider/profile", response_class=HTMLResponse)
    async def ui_provider_profile(request: Request):
        try:
            form = await _form_payload(
                request,
                {"profile_id", "adapter", "model_id", "base_url", "secret_reference", "prompt_version", "request_timeout_ms"},
            )
            if not form.get("secret_reference"):
                form.pop("secret_reference", None)
            if not form.get("prompt_version"):
                form.pop("prompt_version", None)
            if not form.get("request_timeout_ms"):
                form.pop("request_timeout_ms", None)
            typed_form: dict[str, Any] = dict(form)
            if "request_timeout_ms" in typed_form:
                typed_form["request_timeout_ms"] = int(typed_form["request_timeout_ms"])
            payload = ProviderProfileRequest.model_validate(typed_form)
            saved = await run_in_threadpool(studio.save_provider_profile, _provider_profile_from_request(studio, payload))
            shaped = _provider_profile_view(
                studio,
                saved,
                credential_status=studio.provider_secret_status(payload.profile_id),
            )
            return _template_or_fallback(
                templates,
                request,
                "partials/provider_profile.html",
                {"request": request, "profile": shaped},
                _fallback_provider_result({"profile": shaped}),
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "确认 profile 字段和秘密引用后再保存。")

    @app.post("/ui/provider/probe", response_class=HTMLResponse)
    async def ui_provider_probe(request: Request):
        try:
            form = await _form_payload(request, {"profile_id"})
            payload = ProviderProfileIDRequest.model_validate(form)
            result = await run_in_threadpool(studio.probe_provider, payload.profile_id)
            shaped = _shape_provider_probe(studio, result)
            return _template_or_fallback(
                templates,
                request,
                "partials/provider_probe.html",
                {"request": request, "result": shaped},
                _fallback_provider_result(shaped),
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "确认所选协议的能力探测和秘密配置后再启用。")

    @app.post("/ui/provider/enable", response_class=HTMLResponse)
    async def ui_provider_enable(request: Request):
        try:
            form = await _form_payload(request, {"profile_id"})
            payload = ProviderProfileIDRequest.model_validate(form)
            result = await run_in_threadpool(studio.enable_provider, payload.profile_id)
            shaped = _provider_profile_view(studio, result)
            return _template_or_fallback(
                templates,
                request,
                "partials/provider_profile.html",
                {"request": request, "profile": shaped},
                _fallback_provider_result({"profile": shaped}),
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "先完成能力探测并确认秘密已配置。")

    @app.post("/ui/provider/disable", response_class=HTMLResponse)
    async def ui_provider_disable(request: Request):
        try:
            form = await _form_payload(request, {"profile_id"})
            payload = ProviderProfileIDRequest.model_validate(form)
            result = await run_in_threadpool(studio.disable_provider, payload.profile_id)
            shaped = _provider_profile_view(studio, result)
            return _template_or_fallback(
                templates,
                request,
                "partials/provider_profile.html",
                {"request": request, "profile": shaped},
                _fallback_provider_result({"profile": shaped}),
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "刷新 provider profile 状态后再试。")

    @app.post("/ui/runs/configure", response_class=HTMLResponse)
    async def ui_configure_run(request: Request):
        try:
            form = await _form_payload(
                request,
                {"import_id", "minecraft_version", "profile_id", "batch_size", "normal_threshold", "high_threshold", "sample_rate"},
            )
            typed_form: dict[str, Any] = dict(form)
            for key in ("batch_size", "sample_rate"):
                if not typed_form.get(key):
                    typed_form.pop(key, None)
                elif key in typed_form:
                    typed_form[key] = int(typed_form[key])
            for key in ("normal_threshold", "high_threshold"):
                if key in typed_form:
                    typed_form[key] = float(typed_form[key])
            payload = ConfigureRunRequest.model_validate(typed_form)
            result = await run_in_threadpool(
                studio.configure_run,
                payload.import_id,
                payload.minecraft_version,
                profile_id=payload.profile_id,
                batch_size=payload.batch_size,
                normal_threshold=payload.normal_threshold,
                high_threshold=payload.high_threshold,
                sample_rate=payload.sample_rate,
            )
            return _template_or_fallback(
                templates,
                request,
                "partials/run_panel.html",
                {"request": request, "run": _shape_run(studio.get_run(result["run_id"])), "configuration": result},
                _fallback_run_result(result),
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "确认导入已完成 R2 六阶段并选择已验证 profile。")

    @app.post("/ui/runs/{run_id}/ai-batches/{logical_key}/approve", response_class=HTMLResponse)
    async def ui_approve_ai_batch(run_id: str, logical_key: str, request: Request):
        try:
            form = await _form_payload(request, {"input_signature"})
            payload = AIBatchApproveRequest.model_validate(form)
            result = await run_in_threadpool(studio.approve_ai_batch, run_id, logical_key, payload.input_signature)
            return _template_or_fallback(
                templates,
                request,
                "partials/ai_batch.html",
                {"request": request, "batch": result},
                _fallback_batch_result(result),
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "刷新批次输入签名后再批准。")

    @app.post("/ui/runs/{run_id}/ai-batches/{logical_key}/cancel", response_class=HTMLResponse)
    async def ui_cancel_ai_batch(run_id: str, logical_key: str, request: Request):
        try:
            form = await _form_payload(request, {"reason"})
            payload = AIBatchCancelRequest.model_validate(form or {})
            result = await run_in_threadpool(studio.cancel_ai_batch, run_id, logical_key, reason=payload.reason)
            return _template_or_fallback(
                templates,
                request,
                "partials/review_task.html",
                {"request": request, "batch": result},
                _fallback_batch_result(result),
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "确认批次仍未完成后再取消。")

    @app.post("/ui/reviews/{review_id}/resolve", response_class=HTMLResponse)
    async def ui_resolve_review(review_id: str, request: Request):
        try:
            form = await _form_payload(
                request,
                {"run_id", "decision", "reviewer", "reason_code", "reason", "note", "evidence", "override"},
            )
            payload = _review_form_payload(form)
            review_run_id = payload.pop("run_id", None) or _find_review_run(studio, review_id)
            if review_run_id is None:
                raise R3Error("REVIEW_NOT_FOUND")
            result = await run_in_threadpool(
                studio.resolve_review,
                review_run_id,
                review_id,
                **payload,
            )
            return _template_or_fallback(
                templates,
                request,
                "partials/review_task.html",
                {"request": request, "review": result},
                _fallback_review_result(result),
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "补充审核者、理由和证据后再提交。")

    @app.post("/ui/runs/{run_id}/reviews/continue", response_class=HTMLResponse)
    async def ui_continue_reviews(run_id: str, request: Request):
        try:
            await _form_payload(request, set())
            result = await run_in_threadpool(studio.continue_review, run_id)
            return templates.TemplateResponse(
                request=request,
                name="partials/run_panel.html",
                context={"request": request, "run": _shape_run(result)},
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "确认所有高优先级审核已解决后再继续。")

    @app.get("/ui/runs/{run_id}/search", response_class=HTMLResponse)
    async def ui_workspace_search(
        run_id: str,
        request: Request,
        query: str = "",
        limit: int = 12,
    ):
        try:
            query = query.strip()
            if not query or len(query) > 240 or not 1 <= limit <= 100:
                raise ValueError("invalid search")
            hits = await run_in_threadpool(studio.query_workspace, run_id, query, limit=limit)
            return templates.TemplateResponse(
                request=request,
                name="partials/search_results.html",
                context={"request": request, "hits": _shape_search_hits(hits)},
            )
        except Exception as exc:
            return _partial_exception(templates, request, exc, "确认离线特征阶段完成后再搜索。")

    return app


def sse_snapshot_event(snapshot: Mapping[str, Any], fragment_html: str) -> str:
    """Encode the shared snapshot event without replay IDs."""

    data = json.dumps(
        {"snapshot": dict(snapshot), "html": fragment_html},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"event: snapshot\ndata: {data}\n\n"


def sse_heartbeat_comment() -> str:
    return ": heartbeat\n\n"


async def _run_event_stream(
    studio: StudioService,
    run_id: str,
    request: Request,
    templates: Jinja2Templates,
    initial: Mapping[str, Any],
):
    last: str | None = None
    last_heartbeat = asyncio.get_running_loop().time()
    yield "retry: 2000\n\n"
    while True:
        if last is not None and await request.is_disconnected():
            return
        try:
            snapshot = dict(initial) if last is None else _shape_run(studio.get_run(run_id))
            encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if encoded != last:
                last = encoded
                yield sse_snapshot_event(snapshot, _render_fragment(templates, "partials/run_panel.html", {"request": request, "run": snapshot}, "run", snapshot))
            if snapshot.get("status") in {"paused", "needs_review", "failed", "succeeded", "cancelled"}:
                return
        except KeyError:
            # The initial route has already validated existence.  A later
            # disappearance is a redacted snapshot error, never a traceback.
            yield _sse_snapshot_error("RUN_SNAPSHOT_NOT_FOUND", "运行快照暂时不可用。")
        except Exception:
            yield _sse_snapshot_error("RUN_SNAPSHOT_UNAVAILABLE", "运行快照暂时不可用。")
        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= 15.0:
            yield sse_heartbeat_comment()
            last_heartbeat = now
        await asyncio.sleep(0.25)


async def _import_event_stream(
    studio: StudioService,
    check_id: str,
    request: Request,
    templates: Jinja2Templates,
    initial: Mapping[str, Any],
):
    last: str | None = None
    last_heartbeat = asyncio.get_running_loop().time()
    yield "retry: 2000\n\n"
    while True:
        if last is not None and await request.is_disconnected():
            return
        try:
            snapshot = dict(initial) if last is None else _shape_import_check(studio.get_import_check(check_id))
            encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if encoded != last:
                last = encoded
                yield sse_snapshot_event(snapshot, _render_fragment(templates, "partials/import_check_progress.html", {"request": request, "check": snapshot}, "import", snapshot))
            if snapshot.get("status") in {"passed", "failed"}:
                return
        except ImportCheckNotFound:
            yield _sse_snapshot_error("IMPORT_NOT_FOUND", "导入检查暂时不可用。")
            return
        except Exception:
            yield _sse_snapshot_error("IMPORT_CHECK_SNAPSHOT_UNAVAILABLE", "导入检查暂时不可用。")
        now = asyncio.get_running_loop().time()
        if now - last_heartbeat >= 15.0:
            yield sse_heartbeat_comment()
            last_heartbeat = now
        await asyncio.sleep(0.25)


def _sse_snapshot_error(code: str, message: str) -> str:
    data = json.dumps({"error_code": code, "message": message}, ensure_ascii=False, separators=(",", ":"))
    return f"event: snapshot_error\ndata: {data}\n\n"


def _render_fragment(
    templates: Jinja2Templates,
    name: str,
    context: Mapping[str, Any],
    kind: str,
    snapshot: Mapping[str, Any],
) -> str:
    try:
        return templates.env.get_template(name).render(**dict(context))
    except Exception:
        if kind == "run":
            return (
                '<section class="run-panel" data-run-id="%s"><span>%s</span><code>%s</code></section>'
                % (
                    html.escape(str(snapshot.get("run_id", "")), quote=True),
                    html.escape(str(snapshot.get("status", "pending"))),
                    html.escape(str(snapshot.get("current_stage", "PREPARE"))),
                )
            )
        return (
            '<section class="import-check-progress" data-check-id="%s"><span>%s</span><strong>%s</strong></section>'
            % (
                html.escape(str(snapshot.get("check_id", "")), quote=True),
                html.escape(str(snapshot.get("status", "pending"))),
                html.escape(str(snapshot.get("phase", "QUEUED"))),
            )
        )


def _template_or_fallback(
    templates: Jinja2Templates,
    request: Request,
    name: str,
    context: Mapping[str, Any],
    fallback: str,
) -> HTMLResponse:
    try:
        template = templates.env.get_template(name)
    except Exception:
        return HTMLResponse(fallback)
    return HTMLResponse(template.render(**dict(context)))


_PROVIDER_ADAPTERS = frozenset({"openai_responses", "openai_chat_completions"})
_PROVIDER_CAPABILITY_FLAGS = (
    "image_input_supported",
    "structured_outputs_supported",
    "error_classification_supported",
)
_PROVIDER_PROFILE_FIELDS = (
    "profile_id",
    "adapter",
    "base_url",
    "base_url_stable_id",
    "model_id",
    "enabled",
    "capability_status",
    "prompt_version",
    "annotation_output_schema_id",
    "query_spec_output_schema_id",
    "rerank_output_schema_id",
    "search_ranking_version",
    "request_timeout_ms",
    "stages",
)
_PROVIDER_REQUEST_ID_RE = re.compile(r"^req_(?:…)?[A-Za-z0-9_-]{1,64}$")
_PROVIDER_SOURCES = frozenset({"keyring", "env", "environment", "none"})


def _provider_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _provider_adapter(value: Any, fallback: Any = None) -> str | None:
    candidate = value if isinstance(value, str) and value in _PROVIDER_ADAPTERS else fallback
    return candidate if isinstance(candidate, str) and candidate in _PROVIDER_ADAPTERS else None


def _provider_status(value: Any, fallback: str = "unverified") -> str:
    return value if isinstance(value, str) and value in {"draft", "unverified", "verified", "failed"} else fallback


def _provider_request_id(value: Any) -> str | None:
    if not isinstance(value, str) or _PROVIDER_REQUEST_ID_RE.fullmatch(value) is None:
        return None
    return value


def _provider_capability_view(
    value: Any,
    *,
    adapter: Any = None,
    capability_status: Any = None,
    base_url_stable_id: Any = None,
) -> dict[str, Any]:
    """Return the public capability allowlist, excluding legacy storage flags."""

    raw = _provider_mapping(value)
    result: dict[str, Any] = {
        "adapter": _provider_adapter(raw.get("adapter"), adapter),
        "capability_status": _provider_status(
            capability_status if capability_status is not None else raw.get("capability_status")
        ),
    }
    for key in _PROVIDER_CAPABILITY_FLAGS:
        result[key] = raw.get(key) if isinstance(raw.get(key), bool) else None
    error_code = raw.get("error_code")
    result["error_code"] = _safe_code(error_code, optional=True) if error_code is not None else None
    result["request_id_redacted"] = _provider_request_id(raw.get("request_id_redacted"))
    result["probed_at"] = _safe_optional_text(raw.get("probed_at")) if raw.get("probed_at") is not None else None
    stable_id = raw.get("base_url_stable_id")
    if stable_id is None:
        stable_id = base_url_stable_id
    result["base_url_stable_id"] = _clean_text(stable_id) if stable_id is not None else None
    return result


def _stored_provider_capabilities(studio: Any, profile_id: str) -> Mapping[str, Any]:
    store = getattr(studio, "profile_store", None)
    reader = getattr(store, "capabilities", None)
    if not callable(reader):
        return {}
    try:
        value = reader(profile_id)
    except Exception:
        return {}
    return _provider_mapping(value)


def _provider_credential_view(value: Any) -> dict[str, Any]:
    raw = _provider_mapping(value)
    configured = raw.get("configured") if isinstance(raw.get("configured"), bool) else False
    source = raw.get("source") if isinstance(raw.get("source"), str) and raw.get("source") in _PROVIDER_SOURCES else None
    masked = raw.get("masked")
    if configured:
        masked = _clean_text(masked) if isinstance(masked, str) else "********"
        if masked == "内容已隐藏。":
            masked = "********"
    else:
        masked = None
    return {"configured": configured, "source": source, "masked": masked}


def _provider_profile_view(
    studio: Any,
    profile: Any,
    *,
    credential_status: Any = None,
    capabilities: Any = None,
) -> dict[str, Any]:
    """Adapt a service profile to the non-secret provider UI/API view."""

    raw = _provider_mapping(profile)
    profile_id = _safe_identifier(raw.get("profile_id")) or "unavailable"
    adapter = _provider_adapter(raw.get("adapter"), "openai_responses") or "openai_responses"
    status = _provider_status(raw.get("capability_status"))
    cap_source = capabilities if isinstance(capabilities, Mapping) else _stored_provider_capabilities(studio, profile_id)
    view: dict[str, Any] = {
        key: raw.get(key)
        for key in _PROVIDER_PROFILE_FIELDS
        if key in raw and key not in {"base_url_stable_id", "capability_status", "adapter", "profile_id"}
    }
    view.update(
        {
            "profile_id": profile_id,
            "adapter": adapter,
            "capability_status": status,
            "base_url_stable_id": raw.get("base_url_stable_id"),
            "capabilities": _provider_capability_view(
                cap_source,
                adapter=adapter,
                capability_status=status,
                base_url_stable_id=raw.get("base_url_stable_id"),
            ),
        }
    )
    if credential_status is not None:
        view["credential_status"] = _provider_credential_view(credential_status)
    return view


def _shape_provider_probe(studio: Any, value: Any) -> dict[str, Any]:
    raw = _provider_mapping(value)
    raw_profile = raw.get("profile")
    raw_capabilities = raw.get("capabilities")
    profile = _provider_profile_view(studio, raw_profile, capabilities=raw_capabilities)
    capabilities = profile["capabilities"]
    return {"profile": profile, "capabilities": capabilities}


def _provider_profile_from_request(studio: StudioService, payload: ProviderProfileRequest) -> ProviderProfile:
    """Build a non-secret provider profile from the explicitly allowed form."""

    existing = studio.profile_store.load().get(payload.profile_id)
    if existing is None:
        value = ProviderProfile(
            profile_id=payload.profile_id,
            model_id=payload.model_id,
            adapter=payload.adapter,
            base_url=payload.base_url,
        ).to_dict()
    else:
        value = existing.to_dict()
    value.update(
        {
            "profile_id": payload.profile_id,
            "adapter": payload.adapter,
            "model_id": payload.model_id,
            "base_url": payload.base_url,
            # ProviderProfile derives this stable identifier from base_url.
            "base_url_stable_id": None,
            # Configuration writes always require a fresh capability probe.
            "enabled": False,
            "capability_status": "unverified",
            "annotation_output_schema_id": payload.annotation_output_schema_id,
            "query_spec_output_schema_id": payload.query_spec_output_schema_id,
            "rerank_output_schema_id": payload.rerank_output_schema_id,
            "search_ranking_version": payload.search_ranking_version,
        }
    )
    if payload.secret_reference is not None:
        value["secret_reference"] = payload.secret_reference
    if payload.prompt_version is not None:
        value["prompt_version"] = payload.prompt_version
    if payload.request_timeout_ms is not None:
        value["request_timeout_ms"] = payload.request_timeout_ms
    if payload.stages is not None:
        stages = dict(value.get("stages", {}))
        stages.update({name: config.model_dump() for name, config in payload.stages.items()})
        value["stages"] = stages
    return ProviderProfile.from_dict(value)


def _provider_profile_views(studio: StudioService) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for profile in studio.list_provider_profiles():
        views.append(
            _provider_profile_view(
                studio,
                profile,
                credential_status=studio.provider_secret_status(str(profile["profile_id"])),
            )
        )
    return views


def _review_run_ids(studio: StudioService) -> list[str]:
    return [
        str(row["run_id"])
        for row in studio.list_runs()
        if isinstance(row, Mapping) and isinstance(row.get("run_id"), str)
    ]


def _find_review_run(studio: StudioService, review_id: str) -> str | None:
    for run_id in _review_run_ids(studio):
        try:
            if any(item.get("review_id") == review_id for item in studio.list_reviews(run_id, limit=200)):
                return run_id
        except Exception:
            continue
    return None


def _shape_ai_preview(value: Mapping[str, Any]) -> dict[str, Any]:
    tiles = []
    raw_tiles = value.get("tiles", [])
    if not isinstance(raw_tiles, (list, tuple)):
        raw_tiles = []
    for tile in raw_tiles[:_MAX_PUBLIC_BATCH_IDS]:
        if not isinstance(tile, Mapping):
            continue
        tiles.append(
            {
                "tile_id": _safe_identifier(tile.get("tile_id")),
                "variant_id": _safe_identifier(tile.get("variant_id")),
                "row": _safe_int(tile.get("row"), 0),
                "column": _safe_int(tile.get("column"), 0),
            }
        )
    return {
        "job_id": _safe_identifier(value.get("job_id")),
        "logical_key": _safe_identifier(value.get("logical_key")),
        "input_signature": _safe_hash(value.get("input_signature"), optional=True)
        or _safe_identifier(value.get("input_signature")),
        "approved": bool(value.get("approved")),
        "tile_ids": _safe_identifier_list(value.get("tile_ids")),
        "tiles": tiles,
        "machine_metadata": _safe_preview_machine_metadata(value.get("machine_metadata", {})),
        "prompt": _safe_preview_prompt(value.get("prompt", "")),
        "image_url": f"/api/runs/{_safe_identifier(value.get('run_id'), optional=True)}/ai-batches/{_safe_identifier(value.get('logical_key'))}/image"
        if value.get("run_id")
        else None,
    }


_MAX_PUBLIC_AI_PLAN_JOBS = 2048
_MAX_PUBLIC_BATCH_IDS = 128
# r3.safe_prompt bounds its data section at 12,000 characters; this narrow
# preview limit covers that payload plus its fixed trusted instructions.
_SAFE_PREVIEW_PROMPT_MAX_CHARS = 16_000


def _preview_data_passthrough(value: Any) -> Any:
    """The preview shaper already applies its complete explicit allowlist."""

    return value


def _bounded_public_count(value: Any, *, maximum: int) -> int:
    return max(0, min(_safe_int(value, 0), maximum))


def _safe_identifier_list(value: Any, *, maximum: int = _MAX_PUBLIC_BATCH_IDS) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    result: list[str] = []
    for item in value[:maximum]:
        safe = _safe_identifier(item, optional=True)
        if safe is not None:
            result.append(safe)
    return result


_PREVIEW_FORBIDDEN_METADATA_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "base_url",
        "endpoint",
        "raw_response",
        "provider_response",
        "response_body",
        "full_response",
    }
)


def _safe_preview_machine_metadata(value: Any) -> Any:
    """Keep machine facts bounded while excluding provider/path material."""

    safe = _safe_value(value)
    if isinstance(safe, Mapping):
        return {
            str(key): _safe_preview_machine_metadata(item)
            for key, item in safe.items()
            if str(key).casefold() not in _PREVIEW_FORBIDDEN_METADATA_KEYS
        }
    if isinstance(safe, list):
        return [_safe_preview_machine_metadata(item) for item in safe[:_MAX_PUBLIC_BATCH_IDS]]
    return safe


def _safe_preview_prompt(value: Any) -> str:
    if not isinstance(value, str):
        raise R3Error("AI_BATCH_INPUT_INVALID")
    if len(value) > _SAFE_PREVIEW_PROMPT_MAX_CHARS:
        raise R3Error("AI_BATCH_INPUT_INVALID")
    if any(
        unicodedata.category(character) == "Cc" and character not in "\n\t"
        for character in value
    ):
        raise R3Error("AI_BATCH_INPUT_INVALID")
    if (
        is_sensitive_review_text(value)
        or any(marker in value.casefold() for marker in _SENSITIVE_MARKERS)
        or re.search(
            r"(?i)(?:https?://|file://|[a-z]:[\\/]|\\\\|(?:^|[\\s(])/(?!/)[^\s<>]+|base[_ -]?url|endpoint|raw[_ -]?response|provider response)",
            value,
        )
    ):
        raise R3Error("AI_BATCH_INPUT_INVALID")
    return value


def _shape_ai_plan(value: Any, *, include_result_meta: bool = False) -> dict[str, Any]:
    """Expose only the non-sensitive frozen plan projection."""

    raw = value if isinstance(value, Mapping) else {}
    jobs: list[dict[str, Any]] = []
    source_jobs = raw.get("jobs")
    if isinstance(source_jobs, (list, tuple)):
        for source in source_jobs[:_MAX_PUBLIC_AI_PLAN_JOBS]:
            if not isinstance(source, Mapping):
                continue
            job_id = _safe_identifier(source.get("job_id"), optional=True)
            logical_key = _safe_identifier(source.get("logical_key"), optional=True)
            run_id = _safe_identifier(raw.get("run_id"), optional=True)
            preview_url = (
                f"/api/runs/{run_id}/ai-batches/plan/{logical_key}/preview"
                if run_id and logical_key
                else None
            )
            image_url = (
                f"/api/runs/{run_id}/ai-batches/{logical_key}/image"
                if run_id and logical_key
                else None
            )
            jobs.append(
                {
                    "job_id": job_id,
                    "logical_key": logical_key,
                    "input_signature": _safe_hash(source.get("input_signature"), optional=True),
                    "tile_ids": _safe_identifier_list(source.get("tile_ids")),
                    "variant_ids": _safe_identifier_list(source.get("variant_ids")),
                    "preview_url": preview_url,
                    "image_url": image_url,
                }
            )
    result: dict[str, Any] = {
        "run_id": _safe_identifier(raw.get("run_id"), optional=True),
        "effective_config_hash": _safe_hash(raw.get("effective_config_hash"), optional=True),
        "plan_hash": _safe_hash(raw.get("plan_hash"), optional=True),
        "count": _bounded_public_count(raw.get("count", len(jobs)), maximum=_MAX_PUBLIC_AI_PLAN_JOBS),
        "profile_id": _safe_identifier(raw.get("profile_id"), optional=True),
        "adapter": _provider_adapter(raw.get("adapter")),
        "model_id": _safe_identifier(raw.get("requested_model_id", raw.get("model_id")), optional=True),
        "jobs": jobs,
    }
    if include_result_meta:
        result.update(
            {
                "approved": bool(raw.get("approved")),
                "idempotent": bool(raw.get("idempotent")),
            }
        )
    return result


def _shape_retry_job(value: Any, *, run_id: str | None = None, include_result_meta: bool = False) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    result: dict[str, Any] = {
        "run_id": _safe_identifier(raw.get("run_id", run_id), optional=True),
        "source_job_id": _safe_identifier(raw.get("source_job_id"), optional=True),
        "job_id": _safe_identifier(raw.get("job_id", raw.get("child_job_id")), optional=True),
        "logical_key": _safe_identifier(raw.get("logical_key", raw.get("child_logical_key")), optional=True),
        "input_signature": _safe_hash(raw.get("input_signature", raw.get("child_input_signature")), optional=True),
        "variant_ids": _safe_identifier_list(raw.get("variant_ids")),
    }
    if include_result_meta:
        result.update(
            {
                "approved": bool(raw.get("approved")),
                "idempotent": bool(raw.get("idempotent")),
            }
        )
    return result


def _shape_retry_wave(value: Any, *, include_result_meta: bool = False) -> dict[str, Any]:
    """Expose retry lineage without evidence, request bodies, or exceptions."""

    raw = value if isinstance(value, Mapping) else {}
    jobs: list[dict[str, Any]] = []
    source_jobs = raw.get("jobs")
    if isinstance(source_jobs, (list, tuple)):
        for source in source_jobs[:_MAX_PUBLIC_AI_PLAN_JOBS]:
            if not isinstance(source, Mapping):
                continue
            jobs.append(
                {
                    "source_job_id": _safe_identifier(source.get("source_job_id"), optional=True),
                    "child_job_id": _safe_identifier(source.get("child_job_id", source.get("job_id")), optional=True),
                    "source_input_signature": _safe_hash(source.get("source_input_signature"), optional=True),
                    "child_input_signature": _safe_hash(
                        source.get("child_input_signature", source.get("input_signature")), optional=True
                    ),
                    "source_logical_key": _safe_identifier(source.get("source_logical_key"), optional=True),
                    "child_logical_key": _safe_identifier(
                        source.get("child_logical_key", source.get("logical_key")), optional=True
                    ),
                    "variant_ids": _safe_identifier_list(source.get("variant_ids")),
                }
            )
    result: dict[str, Any] = {
        "run_id": _safe_identifier(raw.get("run_id"), optional=True),
        "wave_hash": _safe_hash(raw.get("wave_hash"), optional=True),
        "count": _bounded_public_count(raw.get("count", len(jobs)), maximum=_MAX_PUBLIC_AI_PLAN_JOBS),
        "jobs": jobs,
    }
    if include_result_meta:
        result["approved"] = bool(raw.get("approved"))
    return result


def _fallback_provider_page(profiles: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(_fallback_provider_profile_row(profile) for profile in profiles)
    return "<!doctype html><html><body><main id='provider'><h1>AI Provider</h1><ul>%s</ul></main></body></html>" % rows


def _fallback_review_page(run: Mapping[str, Any], reviews: Sequence[Mapping[str, Any]]) -> str:
    rows = "".join(
        "<li data-review-id='%s'>%s · %s</li>"
        % (
            html.escape(str(item.get("review_id", "")), quote=True),
            html.escape(str(item.get("reason_code", ""))),
            html.escape(str(item.get("severity", ""))),
        )
        for item in reviews
    )
    return "<!doctype html><html><body><main id='review'><h1>审核</h1><p>%s</p><ul>%s</ul></main></body></html>" % (
        html.escape(str(run.get("run_id", ""))),
        rows,
    )


def _fallback_provider_result(value: Mapping[str, Any]) -> str:
    candidate = value.get("profile")
    profile = candidate if isinstance(candidate, Mapping) else value
    capabilities = profile.get("capabilities") if isinstance(profile.get("capabilities"), Mapping) else value.get("capabilities")
    capability_view = _provider_capability_view(
        capabilities,
        adapter=profile.get("adapter"),
        capability_status=profile.get("capability_status", value.get("capability_status")),
        base_url_stable_id=profile.get("base_url_stable_id"),
    )
    attrs = [
        ("data-provider-status", profile.get("capability_status", "updated")),
        ("data-provider-adapter", profile.get("adapter", capability_view.get("adapter"))),
        ("data-capability-status", capability_view.get("capability_status")),
    ]
    for key in _PROVIDER_CAPABILITY_FLAGS:
        attrs.append((f"data-capability-{key}", capability_view.get(key)))
    for key in ("error_code", "request_id_redacted", "probed_at"):
        if capability_view.get(key) is not None:
            attrs.append((f"data-{key.replace('_', '-')}", capability_view[key]))
    attributes = " ".join(
        f"{name}='{html.escape('' if value is None else str(value), quote=True)}'" for name, value in attrs
    )
    items = "".join(
        "<li data-capability='%s'>%s</li>"
        % (html.escape(key, quote=True), html.escape(str(capability_view.get(key))))
        for key in _PROVIDER_CAPABILITY_FLAGS
    )
    return "<section %s><strong>%s</strong><ul>%s</ul></section>" % (
        attributes,
        html.escape(str(profile.get("profile_id", ""))),
        items,
    )


def _fallback_provider_profile_row(profile: Mapping[str, Any]) -> str:
    capability_view = _provider_capability_view(
        profile.get("capabilities"),
        adapter=profile.get("adapter"),
        capability_status=profile.get("capability_status"),
        base_url_stable_id=profile.get("base_url_stable_id"),
    )
    attrs = [
        ("data-profile-id", profile.get("profile_id", "")),
        ("data-provider-adapter", capability_view.get("adapter")),
        ("data-capability-status", capability_view.get("capability_status")),
    ]
    for key in _PROVIDER_CAPABILITY_FLAGS:
        attrs.append((f"data-capability-{key}", capability_view.get(key)))
    for key in ("error_code", "request_id_redacted", "probed_at"):
        if capability_view.get(key) is not None:
            attrs.append((f"data-{key.replace('_', '-')}", capability_view[key]))
    attributes = " ".join(
        f"{name}='{html.escape('' if value is None else str(value), quote=True)}'" for name, value in attrs
    )
    return (
        "<li %s>%s · %s · %s<ul>%s</ul></li>"
        % (
            attributes,
            html.escape(str(profile.get("profile_id", ""))),
            html.escape(str(profile.get("model_id", ""))),
            html.escape(str(capability_view.get("adapter", ""))),
            "".join(
                "<li data-capability='%s'>%s</li>"
                % (html.escape(key, quote=True), html.escape(str(capability_view.get(key))))
                for key in _PROVIDER_CAPABILITY_FLAGS
            ),
        )
    )


def _fallback_run_result(value: Mapping[str, Any]) -> str:
    return "<section data-run-id='%s'>%s</section>" % (
        html.escape(str(value.get("run_id", "")), quote=True),
        html.escape(str(value.get("status", "pending"))),
    )


def _fallback_batch_result(value: Mapping[str, Any]) -> str:
    return "<section data-batch-key='%s'>%s</section>" % (
        html.escape(str(value.get("logical_key", "")), quote=True),
        html.escape(str(value.get("status", "updated"))),
    )


def _fallback_review_result(value: Mapping[str, Any]) -> str:
    return "<section data-review-id='%s'>%s</section>" % (
        html.escape(str(value.get("review_id", "")), quote=True),
        html.escape(str(value.get("status", "resolved"))),
    )


def _review_form_payload(form: Mapping[str, str]) -> dict[str, Any]:
    value: dict[str, Any] = dict(form)
    for key in ("reason_code", "reason"):
        if not value.get(key):
            value[key] = None
    evidence_raw = value.get("evidence", "")
    try:
        evidence = json.loads(evidence_raw) if evidence_raw else []
    except (TypeError, ValueError, json.JSONDecodeError):
        evidence = [item.strip() for item in evidence_raw.splitlines() if item.strip()]
    value["evidence"] = evidence
    override_raw = value.get("override", "")
    if not override_raw:
        value["override"] = None
    else:
        try:
            override = json.loads(override_raw)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("override is invalid") from exc
        value["override"] = override
    payload = ReviewResolveRequest.model_validate(value)
    return payload.model_dump()


def _fallback_import_page(check: Mapping[str, Any]) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>导入检查</title></head>"
        "<body><main id='import-check'><h1>导入检查</h1><p data-status='%s'>%s</p><p>%s</p></main></body></html>"
        % (
            html.escape(str(check.get("status", "pending")), quote=True),
            html.escape(str(check.get("phase", "QUEUED"))),
            html.escape(str(check.get("export_id", ""))),
        )
    )


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "web_" + uuid.uuid4().hex)


def _success_response(
    request: Request,
    data: Any,
    warnings: list[str] | None = None,
    *,
    status_code: int = 200,
    data_sanitizer: Callable[[Any], Any] | None = None,
) -> JSONResponse:
    sanitizer = data_sanitizer or _safe_value
    return JSONResponse(
        jsonable_encoder(
            {
                "ok": True,
                "request_id": _request_id(request),
                "data": sanitizer(data),
                "warnings": warnings or [],
            }
        ),
        status_code=status_code,
    )


def _error_response(
    request: Request,
    status_code: int,
    error_code: str,
    message: str,
    *,
    field_errors: Mapping[str, str] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "ok": False,
            "request_id": _request_id(request),
            "error_code": error_code,
            "message": message,
            "field_errors": dict(field_errors or {}),
            "retryable": retryable,
        },
    )


def _shape_import_check(value: Any) -> dict[str, Any]:
    raw = value.to_dict() if hasattr(value, "to_dict") else dict(value)
    expected_files = raw.get("expected_files", [])
    source_ref = _safe_opaque_ref(raw.get("source_directory_ref"), optional=True)
    progress_raw = raw.get("progress")
    progress: Mapping[str, Any] = progress_raw if isinstance(progress_raw, Mapping) else {}
    completed = max(0, _safe_int(progress.get("completed"), 0))
    total = max(0, _safe_int(progress.get("total"), 0))
    if total:
        completed = min(completed, total)
    public_progress: dict[str, Any] = {
        "completed": completed,
        "total": total,
        "unit": _safe_unit(progress.get("unit")),
    }
    if "bytes" in progress:
        public_progress["bytes"] = max(0, _safe_int(progress.get("bytes"), 0))
    return {
        "check_id": _safe_identifier(raw.get("check_id")),
        "minecraft_version": _safe_minecraft_version(raw.get("minecraft_version")),
        "export_id": _safe_identifier(raw.get("export_id")),
        "source_directory_ref": source_ref,
        "manifest_sha256": _safe_hash(raw.get("manifest_sha256"), optional=True),
        "checksum_sha256": _safe_hash(raw.get("checksum_sha256"), optional=True),
        "status": _safe_status(raw.get("status"), fallback="failed"),
        "phase": str(raw.get("phase")) if str(raw.get("phase")) in {"QUEUED", "SNAPSHOT_EXPORT", "VALIDATE_EXPORT", "FINALIZE"} else "FINALIZE",
        "progress": public_progress,
        "progress_subphase": _safe_subphase(raw.get("progress_subphase") or progress.get("subphase")),
        "created_at": _safe_optional_text(raw.get("created_at")),
        "updated_at": _safe_optional_text(raw.get("updated_at")),
        "workspace": _shape_import_workspace(raw.get("workspace")),
        "check_url": f"/imports/checks/{_safe_identifier(raw.get('check_id'))}",
        "error_code": _safe_code(raw.get("error_code"), optional=True),
        "issues": _shape_import_issues(raw.get("issues", [])),
        "checked_file_count": len(expected_files) + (1 if expected_files else 0),
        "can_import": bool(raw.get("can_import")),
        "reused": bool(getattr(value, "reused", False)),
    }


def _shape_import_result(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "import_id": _safe_identifier(value.get("import_id")),
        "run_id": _safe_identifier(value.get("run_id")),
        "minecraft_version": _safe_minecraft_version(value.get("minecraft_version")),
        "status": _safe_status(value.get("status")),
        "source_directory_ref": _safe_opaque_ref(value.get("source_directory_ref"), optional=True),
    }


def _shape_run_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "run_id": _safe_identifier(value.get("run_id")),
        "minecraft_version": _safe_minecraft_version(value.get("minecraft_version")),
        "status": _safe_status(value.get("status")),
        "current_stage": _safe_stage(value.get("current_stage")),
        "heartbeat_at": _safe_optional_text(value.get("heartbeat_at")),
        "boundary_event": _safe_code(value.get("boundary_event"), optional=True),
        "created_at": _safe_optional_text(value.get("created_at")),
        "started_at": _safe_optional_text(value.get("started_at")),
        "finished_at": _safe_optional_text(value.get("finished_at")),
    }


def _shape_run(value: Mapping[str, Any]) -> dict[str, Any]:
    stages = []
    for source in value.get("stages", []) or []:
        stage_name = _safe_stage(source.get("stage"))
        stage = {
            "stage": stage_name,
            "ordinal": _safe_int(source.get("ordinal"), 0),
            "status": _safe_status(source.get("status")),
            "error_code": _safe_code(source.get("error_code"), optional=True),
            "error_present": bool(source.get("error_present") or source.get("error_code")),
            "recovery_attempt": _safe_int(source.get("recovery_attempt"), 0),
            "pause_after_item": bool(source.get("pause_after_item", 0)),
            "heartbeat_at": _safe_optional_text(source.get("heartbeat_at")),
        }
        stages.append(stage)
    stage_lookup = {str(stage.get("stage")): stage for stage in stages}
    # A fake or older service response may omit rows; the visible timeline is
    # still complete and clearly marks the missing rows as pending.
    ordered_stages = []
    for ordinal, name in enumerate(STUDIO_STAGES):
        ordered_stages.append(
            stage_lookup.get(
                name,
                {
                    "stage": name,
                    "ordinal": ordinal,
                    "status": "pending",
                    "error_code": None,
                    "error_present": False,
                    "recovery_attempt": 0,
                    "pause_after_item": False,
                    "heartbeat_at": None,
                },
            )
        )
    completed = sum(stage.get("status") == "succeeded" for stage in ordered_stages)
    r2_completed = sum(
        stage.get("status") == "succeeded" for stage in ordered_stages if stage.get("stage") in R2_STAGES
    )
    jobs = []
    for source in value.get("jobs", []) or []:
        job_stage = _safe_stage(source.get("stage"))
        job_status = _safe_status(source.get("status"))
        job_error_code = _safe_code(source.get("error_code"), optional=True)
        jobs.append(
            {
                "job_id": _safe_identifier(source.get("job_id"), optional=True),
                "stage": job_stage,
                "logical_key": _safe_identifier(source.get("logical_key")),
                "status": job_status,
                "auto_attempt": _safe_int(source.get("auto_attempt"), 0),
                "heartbeat_at": _safe_optional_text(source.get("heartbeat_at")),
                "output_hash": _safe_hash(source.get("output_hash"), optional=True),
                "error_code": job_error_code,
                "error_message": "任务失败；请按错误码处理。" if source.get("error_message") else None,
                "provider_retry_eligible": _provider_retry_eligible_view(job_stage, job_status, job_error_code),
            }
        )
    stale = [_marker_dict(marker) for marker in value.get("stale", []) or []]
    boundary_event = _safe_code(value.get("boundary_event"), optional=True)
    item_counts_raw = value.get("item_counts")
    item_counts: Mapping[str, Any] = item_counts_raw if isinstance(item_counts_raw, Mapping) else {}
    public_counts: dict[str, Any] = {
        str(key): _safe_int(item, 0) if str(key) != "by_stage" else _safe_counts_by_stage(item)
        for key, item in item_counts.items()
        if str(key) in {"total", "pending", "running", "succeeded", "needs_review", "failed", "skipped", "by_stage"}
    }
    audit_steps = []
    for item in value.get("latest_audit_steps", []) or []:
        if isinstance(item, Mapping):
            event_type = _safe_code(item.get("event_type"), optional=True)
            if event_type:
                audit_steps.append({"event_type": event_type, "created_at": _safe_optional_text(item.get("created_at"))})
    supplied_latest_steps = value.get("latest_steps")
    if isinstance(supplied_latest_steps, (list, tuple)):
        latest_steps = []
        for supplied in supplied_latest_steps:
            if not isinstance(supplied, Mapping):
                continue
            code = _safe_code(supplied.get("code"), optional=True)
            if code is None:
                continue
            latest_steps.append(
                {
                    "code": code,
                    "label": _clean_text(supplied.get("label", code)),
                    "status": _safe_status(supplied.get("status")),
                    "created_at": _safe_optional_text(supplied.get("created_at")),
                }
            )
    else:
        latest_steps = [
            {
                "code": step["event_type"],
                "label": AUDIT_STEP_LABELS.get(step["event_type"], step["event_type"]),
                "status": _audit_step_status(step["event_type"]),
                "created_at": step["created_at"],
            }
            for step in audit_steps
        ]
    warnings: list[str] = []
    if stale:
        warnings.append("检测到心跳超时项；系统没有自动改写状态。")
    if boundary_event == "R3_BOUNDARY_REACHED_AI_ANNOTATE_PENDING":
        warnings.append("R2 本地处理已到边界；AI 标注与发布属于后续阶段。")
    elif boundary_event == "R3_BOUNDARY_REACHED_BUILD_RELEASE_PENDING":
        warnings.append("R3 处理已到候选构建边界；发布与 current 激活尚未实现。")
    supplied_item_progress = value.get("item_progress")
    if isinstance(supplied_item_progress, Mapping):
        supplied_by_status = supplied_item_progress.get("by_status")
        supplied_by_status = supplied_by_status if isinstance(supplied_by_status, Mapping) else {}
        item_progress = {
            "total": _safe_int(supplied_item_progress.get("total"), _safe_int(public_counts.get("total"), 0)),
            "completed": _safe_int(supplied_item_progress.get("completed"), 0),
            "by_status": {
                str(key): _safe_int(item, 0)
                for key, item in supplied_by_status.items()
                if str(key) in {"pending", "running", "succeeded", "needs_review", "failed", "skipped", "total"}
            },
            "has_items": bool(supplied_item_progress.get("has_items", False)),
        }
        if item_progress["total"]:
            item_progress["percent"] = round(item_progress["completed"] / item_progress["total"] * 100)
    else:
        item_progress = {
            "total": _safe_int(public_counts.get("total"), 0),
            "completed": _safe_int(public_counts.get("succeeded"), 0) + _safe_int(public_counts.get("skipped"), 0),
            "by_status": {key: value for key, value in public_counts.items() if key != "by_stage"},
            "has_items": bool(public_counts.get("total", 0)),
        }
    return {
        "run_id": _safe_identifier(value.get("run_id")),
        "import_id": _safe_identifier(value.get("import_id"), optional=True),
        "minecraft_version": _safe_minecraft_version(value.get("minecraft_version")),
        "status": _safe_status(value.get("status")),
        "current_stage": _safe_stage(value.get("current_stage")),
        "boundary_event": boundary_event,
        "heartbeat_at": _safe_optional_text(value.get("heartbeat_at")),
        "created_at": _safe_optional_text(value.get("created_at")),
        "started_at": _safe_optional_text(value.get("started_at")),
        "finished_at": _safe_optional_text(value.get("finished_at")),
        "config_snapshot": _safe_config(value.get("config_snapshot", {})),
        "stages": ordered_stages,
        "jobs": jobs,
        "item_counts": public_counts,
        "item_progress": item_progress,
        "latest_audit_steps": audit_steps,
        "latest_steps": latest_steps,
        "stale": stale,
        "warnings": warnings,
        "progress": {
            "completed_stages": completed,
            "total_stages": len(STUDIO_STAGES),
            "percent": round(completed / len(STUDIO_STAGES) * 100),
            "r2_completed_stages": r2_completed,
            "r2_total_stages": len(R2_STAGES),
            "r2_percent": round(r2_completed / len(R2_STAGES) * 100),
        },
    }


def _safe_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {
        "effective_config_hash",
        "feature_extractor_version",
        "force_normalized_like",
        "minecraft_version",
        "schema_version",
        "search_mode",
        "workspace_schema_version",
    }
    return {
        str(key): _safe_value(item)
        for key, item in value.items()
        if str(key) in allowed
    }


def _safe_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name == "validation_diagnostic":
                result[name] = sanitize_validation_diagnostic(item)
            elif _safe_mapping_key(name):
                result[name] = _safe_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        return _clean_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "to_dict"):
        return _safe_value(value.to_dict())
    return "内容已隐藏。"


_RELEASE_RUN_ID_RE = re.compile(r"^run_[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_RELEASE_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
_RELEASE_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_RELEASE_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_RELEASE_RELATIVE_PATH_RE = re.compile(
    r"^releases/[0-9]+\.[0-9]+(?:\.[0-9]+)?/rel_[0-9a-f]{32}$"
)
_RELEASE_CHECK_FIELDS = frozenset(
    {
        "check_id",
        "release_build_id",
        "run_id",
        "minecraft_version",
        "status",
        "can_build",
        "snapshot_fingerprint",
        "quality_report_sha256",
        "created_at",
        "updated_at",
    }
)
_RELEASE_BUILD_FIELDS = frozenset(
    {
        "check_id",
        "release_build_id",
        "release_id",
        "run_id",
        "minecraft_version",
        "relative_path",
        "status",
        "manifest_sha256",
        "quality_report_sha256",
        "checksums_sha256",
        "built_at",
    }
)


def _release_raw_payload(value: Any, required: frozenset[str], error_code: str) -> Mapping[str, Any]:
    try:
        raw = value.to_dict() if hasattr(value, "to_dict") else value
        if not isinstance(raw, Mapping) or not required.issubset(set(raw)):
            raise ValueError("invalid release payload")
        return raw
    except Exception as exc:
        if isinstance(exc, R3Error):
            raise
        raise R3Error(error_code) from exc


def _release_string(
    raw: Mapping[str, Any],
    field: str,
    pattern: re.Pattern[str],
    error_code: str,
) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise R3Error(error_code)
    return value


def _release_status(raw: Mapping[str, Any], expected: str, error_code: str) -> str:
    value = raw.get("status")
    if value != expected:
        raise R3Error(error_code)
    return expected


def _release_bool(raw: Mapping[str, Any], field: str, error_code: str) -> bool:
    value = raw.get(field)
    if not isinstance(value, bool):
        raise R3Error(error_code)
    return value


def _shape_release_check(value: Any) -> dict[str, Any]:
    """Return only the frozen check response fields, failing closed."""

    error_code = "RELEASE_CHECK_FAILED"
    raw = _release_raw_payload(value, _RELEASE_CHECK_FIELDS, error_code)
    version = _release_string(raw, "minecraft_version", _RELEASE_VERSION_RE, error_code)
    result = {
        "check_id": _release_string(raw, "check_id", RELEASE_CHECK_ID_RE, error_code),
        "release_build_id": _release_string(raw, "release_build_id", RELEASE_BUILD_ID_RE, error_code),
        "run_id": _release_string(raw, "run_id", _RELEASE_RUN_ID_RE, error_code),
        "minecraft_version": version,
        "status": _release_status(raw, "passed", error_code),
        "can_build": _release_bool(raw, "can_build", error_code),
        "snapshot_fingerprint": _release_string(raw, "snapshot_fingerprint", _RELEASE_HASH_RE, error_code),
        "quality_report_sha256": _release_string(raw, "quality_report_sha256", _RELEASE_HASH_RE, error_code),
        "created_at": _release_string(raw, "created_at", _RELEASE_TIMESTAMP_RE, error_code),
        "updated_at": _release_string(raw, "updated_at", _RELEASE_TIMESTAMP_RE, error_code),
    }
    return result


def _shape_release_build(value: Any) -> dict[str, Any]:
    """Return only the frozen build response fields, failing closed."""

    error_code = "RELEASE_BUILD_FAILED"
    raw = _release_raw_payload(value, _RELEASE_BUILD_FIELDS, error_code)
    version = _release_string(raw, "minecraft_version", _RELEASE_VERSION_RE, error_code)
    release_id = _release_string(raw, "release_id", RELEASE_ID_RE, error_code)
    relative_path = _release_string(raw, "relative_path", _RELEASE_RELATIVE_PATH_RE, error_code)
    if relative_path != f"releases/{version}/{release_id}":
        raise R3Error(error_code)
    return {
        "check_id": _release_string(raw, "check_id", RELEASE_CHECK_ID_RE, error_code),
        "release_build_id": _release_string(raw, "release_build_id", RELEASE_BUILD_ID_RE, error_code),
        "release_id": release_id,
        "run_id": _release_string(raw, "run_id", _RELEASE_RUN_ID_RE, error_code),
        "minecraft_version": version,
        "relative_path": relative_path,
        "status": _release_status(raw, "built", error_code),
        "manifest_sha256": _release_string(raw, "manifest_sha256", _RELEASE_HASH_RE, error_code),
        "quality_report_sha256": _release_string(raw, "quality_report_sha256", _RELEASE_HASH_RE, error_code),
        "checksums_sha256": _release_string(raw, "checksums_sha256", _RELEASE_HASH_RE, error_code),
        "built_at": _release_string(raw, "built_at", _RELEASE_TIMESTAMP_RE, error_code),
    }


def _release_data_passthrough(value: Any) -> Any:
    """The explicit release shapers already returned validated safe data."""

    return value


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SAFE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_VERSION = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
_SAFE_LOCAL_REF = re.compile(r"^/(?:api|ui|static|provider|runs|imports)(?:/[A-Za-z0-9_.:%?=&{}:-]+)*$")
_SAFE_PUBLIC_URL = re.compile(
    r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?$"
    r"|^http://127\.0\.0\.1(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~%!$&'()*+,;=:@/-]*)?$"
)
_SENSITIVE_MARKERS = (
    "access_token",
    "api key",
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "bearer ",
    "data root",
    "data-root",
    "data_root",
    "openai_api_key",
    "refresh_token",
    "secret",
    "raw_response",
    "provider_response",
    "response_body",
    "full_response",
    "sk-",
    "source-directory",
    "source_directory",
    "token",
)


def _clean_text(value: Any) -> str:
    """Return a whole public string or a stable replacement, never fragments."""

    text = str(value)
    folded = text.casefold()
    if (
        ("/" in text and not _SAFE_LOCAL_REF.fullmatch(text) and not _SAFE_PUBLIC_URL.fullmatch(text))
        or "\\" in text
        or any(marker in folded for marker in _SENSITIVE_MARKERS)
    ):
        return "内容已隐藏。"
    return text[:500]


def _sensitive_key(value: str) -> bool:
    if value == "source_directory_ref":
        return False
    folded = value.casefold()
    return any(marker in folded for marker in _SENSITIVE_MARKERS)


def _safe_mapping_key(value: str) -> bool:
    if value in {"manifest.json", "checksums.sha256"}:
        return True
    return bool(re.fullmatch(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", value)) and not _sensitive_key(value)


def _safe_identifier(value: Any, *, optional: bool = False) -> str | None:
    if value is None:
        return None if optional else "unavailable"
    text = str(value)
    if _clean_text(text) == "内容已隐藏。" or not _SAFE_IDENTIFIER.fullmatch(text):
        return None if optional else "unavailable"
    return text


def _safe_code(value: Any, *, optional: bool = False) -> str | None:
    if value is None:
        return None if optional else "UNCLASSIFIED_ERROR"
    text = str(value)
    if _clean_text(text) == "内容已隐藏。" or not _SAFE_CODE.fullmatch(text):
        return None if optional else "UNCLASSIFIED_ERROR"
    return text


def _safe_hash(value: Any, *, optional: bool = False) -> str | None:
    if value is None:
        return None if optional else "unavailable"
    text = str(value)
    if not _SAFE_HASH.fullmatch(text):
        return None if optional else "unavailable"
    return text


_SAFE_OPAQUE_REF = re.compile(r"^dir_[A-Za-z0-9_-]{40,128}$")


def _safe_opaque_ref(value: Any, *, optional: bool = False) -> str | None:
    if value is None or value == "":
        return None if optional else "unavailable"
    text = str(value)
    if not _SAFE_OPAQUE_REF.fullmatch(text):
        return None if optional else "unavailable"
    return text


def _safe_unit(value: Any) -> str:
    text = str(value) if value is not None else "items"
    return text if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", text) else "items"


def _safe_subphase(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
        return None
    return value


def _shape_import_workspace(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, Mapping) else {}
    status = raw.get("status") if raw.get("status") in {"absent", "creating", "created", "failed"} else "absent"
    return {
        "status": status,
        "import_id": _safe_identifier(raw.get("import_id"), optional=True),
        "run_id": _safe_identifier(raw.get("run_id"), optional=True),
        "error_code": _safe_code(raw.get("error_code"), optional=True),
    }


def _safe_counts_by_stage(value: Any) -> dict[str, dict[str, int]]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, dict[str, int]] = {}
    for stage, statuses in value.items():
        stage_name = _safe_stage(stage)
        if not isinstance(statuses, Mapping):
            continue
        result[stage_name] = {
            str(status): _safe_int(count, 0)
            for status, count in statuses.items()
            if str(status) in {"pending", "running", "succeeded", "needs_review", "failed", "skipped"}
        }
    return result


def _audit_step_status(code: str) -> str:
    if code.endswith("FAILED"):
        return "failed"
    if code.endswith("SUCCEEDED") or code == "WORKER_RECOVERED_STALE_RUNNING":
        return "succeeded"
    if code in {"AI_BATCH_PLAN_APPROVED", "AI_PROVIDER_RETRY_CREATED", "AI_PROVIDER_RETRY_WAVE_APPROVED"}:
        return "succeeded"
    if code in {"RUN_CANCELLED"}:
        return "cancelled"
    if code in {"RUN_PAUSED_REQUESTED", "RUN_PAUSED_AFTER_ITEM"}:
        return "paused"
    return "running"


def _safe_minecraft_version(value: Any) -> str:
    text = str(value) if value is not None else ""
    return text if _SAFE_VERSION.fullmatch(text) else "unknown"


def _safe_status(value: Any, *, fallback: str = "pending") -> str:
    text = str(value) if value is not None else ""
    return text if text in {*STATUS_LABELS, "passed"} else fallback


def _provider_retry_eligible_view(stage: str, status: str, error_code: str | None) -> bool:
    if stage != "AI_ANNOTATE" or status not in {"needs_review", "failed"}:
        return False
    normalized = normalize_provider_error_code(error_code)
    return normalized in ITEM_LOCAL_PROVIDER_ERROR_CODES


def _safe_stage(value: Any) -> str:
    text = str(value) if value is not None else ""
    return text if text in STUDIO_STAGES else STUDIO_STAGES[0]


def _safe_optional_text(value: Any) -> str | None:
    return None if value is None else _clean_text(value)


def _safe_int(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return fallback


def _shape_import_issues(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, (list, tuple)):
        return []
    issues: list[dict[str, str]] = []
    for item in value:
        raw = item if isinstance(item, Mapping) else {}
        issues.append(
            {
                "code": str(_safe_code(raw.get("code"))),
                "message": "该项未通过完整性检查。",
            }
        )
    return issues


def _shape_search_hits(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    hits: list[dict[str, Any]] = []
    for item in value:
        raw = item.to_dict() if hasattr(item, "to_dict") else item
        if not isinstance(raw, Mapping):
            continue
        try:
            score = float(raw.get("score", 0.0))
        except (TypeError, ValueError, OverflowError):
            score = 0.0
        if not math.isfinite(score):
            score = 0.0
        hits.append(
            {
                "block_id": _safe_identifier(raw.get("block_id")),
                "score": max(0.0, min(score, 1.0)),
            }
        )
    return hits


def _shape_recovered(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        "job_id": _safe_identifier(value.get("job_id"), optional=True),
        "stage": _safe_stage(value.get("stage")),
        "status": _safe_status(value.get("status")),
        "recovery_attempt": _safe_int(value.get("recovery_attempt"), 0),
        "auto_attempt": _safe_int(value.get("auto_attempt"), 0),
    }


def _marker_dict(value: Any) -> dict[str, Any]:
    raw = value.to_dict() if hasattr(value, "to_dict") else dict(value)
    return {
        "job_id": _safe_identifier(raw.get("job_id"), optional=True),
        "run_id": _safe_identifier(raw.get("run_id")),
        "stage": _safe_stage(raw.get("stage")),
        "logical_key": _safe_identifier(raw.get("logical_key"), optional=True),
        "heartbeat_at": _safe_optional_text(raw.get("heartbeat_at")),
        "stale": True,
    }


def _remove_stale_marker(app: FastAPI, run_id: str, job_id: str | None, stage: str | None) -> None:
    remaining = []
    for marker in app.state.stale_markers:
        same_run = marker.get("run_id") == run_id
        same_job = job_id is None or marker.get("job_id") == job_id
        same_stage = stage is None or marker.get("stage") == stage
        if same_run and same_job and same_stage:
            continue
        remaining.append(marker)
    app.state.stale_markers = remaining


def _run_action(service: StudioService, run_id: str, action: str) -> dict[str, Any]:
    commands = {
        "pause": service.pause,
        "resume": service.resume,
        "cancel": service.cancel,
        "retry-failed": service.retry_failed,
    }
    try:
        command = commands[action]
    except KeyError as exc:
        raise ValueError("unknown run action") from exc
    return command(run_id)


def _worker_is_running(worker: Any) -> bool:
    """Read worker availability without changing caller-owned lifecycle."""

    state = getattr(worker, "is_running", None)
    try:
        if callable(state):
            return bool(state())
        if isinstance(state, bool):
            return state
        thread = getattr(worker, "_thread", None)
        is_alive = getattr(thread, "is_alive", None)
        return bool(is_alive()) if callable(is_alive) else False
    except Exception:
        return False


def _require_worker(request: Request) -> None:
    if request.app.state.worker_expected and not request.app.state.worker_available:
        raise WorkerUnavailable("worker did not start")


def _allow_query_keys(request: Request, allowed: set[str]) -> None:
    keys = set(request.query_params.keys())
    if keys - allowed:
        raise ValueError("unknown API query field")
    if any(len(request.query_params.getlist(key)) != 1 for key in keys):
        raise ValueError("repeated API query field")


async def _form_payload(request: Request, allowed_fields: set[str]) -> dict[str, str]:
    body = await request.body()
    if not body and not allowed_fields:
        return {}
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise ValueError("form encoding required")
    if len(body) > 65536:
        raise ValueError("form is too large")
    try:
        decoded = body.decode("utf-8", errors="strict")
        parsed = parse_qs(decoded, keep_blank_values=True, strict_parsing=False)
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("form is invalid") from exc
    if set(parsed) - allowed_fields or any(len(values) != 1 for values in parsed.values()):
        raise ValueError("unknown or repeated form field")
    return {key: values[0] for key, values in parsed.items()}


def _validation_fields(errors: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    fields: dict[str, str] = {}
    public_fields = {
        "action",
        "adapter",
        "base_url",
        "check_id",
        "confirm",
        "confirm_immutable_release",
        "copy_mode",
        "job_id",
        "limit",
        "model_id",
        "minecraft_version",
        "profile_id",
        "plan_hash",
        "query",
        "run_id",
        "stage",
        "wave_hash",
    }
    for error in errors:
        location = [str(part) for part in error.get("loc", ()) if part not in {"body", "query", "path"}]
        key = ".".join(location) or "request"
        if key not in public_fields:
            key = "request"
        fields[key] = "字段不合法。"
    return fields


def _exception_details(exc: Exception) -> tuple[int, str, str, bool, dict[str, str]]:
    if isinstance(exc, ValidationError):
        return 400, "INVALID_INPUT", "表单字段不合法，请检查后重试。", False, _validation_fields(exc.errors())
    if isinstance(exc, R3Error):
        code = str(getattr(exc, "code", "R3_OPERATION_INVALID"))
        not_found = {
            "PROVIDER_PROFILE_NOT_FOUND",
            "AI_BATCH_NOT_FOUND",
            "REVIEW_NOT_FOUND",
            "IMPORT_NOT_FOUND",
            "RELEASE_CHECK_NOT_FOUND",
            "RUN_NOT_FOUND",
        }
        conflict = {
            "RUN_STATE_CONFLICT",
            "AI_BATCH_INPUT_CHANGED",
            "R2_PREREQUISITE_NOT_MET",
            "REVIEW_TASKS_OPEN",
            "RELEASE_ALREADY_BUILT",
            "RELEASE_CHECK_NOT_READY",
            "RELEASE_CHECK_STALE",
            "RELEASE_VERSION_MISMATCH",
            "AI_BATCH_PLAN_CONFLICT",
            "AI_RETRY_WAVE_CONFLICT",
        }
        invalid = {
            "INVALID_INPUT",
            "R3_THRESHOLD_FIXED",
            "AI_BATCH_INPUT_INVALID",
        }
        unprocessable = {
            "PROVIDER_CONFIG_INVALID",
            "PROVIDER_CAPABILITY_MISSING",
            "PROVIDER_NOT_CONFIGURED",
            "PROVIDER_CAPABILITY_MISSING",
            "PROVIDER_STORAGE_UNSUPPORTED",
            "OVERRIDE_INVALID",
            "MACHINE_FACT_READ_ONLY",
            "SKIP_REQUIRES_MACHINE_FAILURE",
            "REVIEW_REQUIRES_ANNOTATION",
            "PROVIDER_SCHEMA_INVALID",
            "DATABASE_SCHEMA_MISMATCH",
            "RELEASE_BUILD_INTEGRITY_FAILED",
            "RELEASE_CHECK_FAILED",
            "PROVIDER_RETRY_NOT_ELIGIBLE",
        }
        if code == "RELEASE_BUILD_FAILED":
            status = 500
        elif code == "WORKER_UNAVAILABLE":
            status = 503
        elif code in not_found:
            status = 404
        elif code in conflict:
            status = 409
        elif code in invalid:
            status = 400
        elif code in unprocessable:
            status = 422
        else:
            status = 422
        messages = {
            "PROVIDER_PROFILE_NOT_FOUND": "找不到指定 provider profile。",
            "AI_BATCH_NOT_FOUND": "找不到指定 AI 批次。",
            "REVIEW_NOT_FOUND": "找不到指定审核任务。",
            "R2_PREREQUISITE_NOT_MET": "R2 六个阶段尚未全部完成。",
            "AI_BATCH_INPUT_CHANGED": "批次输入已变化，请重新预览。",
            "AI_BATCH_INPUT_INVALID": "AI 批次输入不完整，无法继续。",
            "PROVIDER_CAPABILITY_MISSING": "provider 尚未通过完整能力探测。",
            "PROVIDER_NOT_CONFIGURED": "provider 秘密尚未配置。",
            "PROVIDER_CONFIG_INVALID": "provider profile 配置不合法。",
            "DATABASE_SCHEMA_MISMATCH": "工作库 Schema 与当前契约不匹配。",
            "RELEASE_CHECK_NOT_READY": "运行尚未满足候选构建前置，或检查结果不可构建。",
            "RELEASE_VERSION_MISMATCH": "请求版本与候选构建输入版本不一致。",
            "RELEASE_CHECK_NOT_FOUND": "候选发布检查不存在或已失效。",
            "RELEASE_CHECK_FAILED": "候选发布检查执行失败。",
            "RELEASE_CHECK_STALE": "候选发布检查已过期，请重新执行检查。",
            "RELEASE_ALREADY_BUILT": "该候选发布检查已经构建过。",
            "RELEASE_BUILD_INTEGRITY_FAILED": "候选发布完整性校验未通过。",
            "RELEASE_BUILD_FAILED": "候选发布构建未完成。",
            "WORKER_UNAVAILABLE": "内置 Worker 当前不可用。请重启 Index Studio 后再试。",
            "OVERRIDE_INVALID": "人工覆盖内容不合法。",
            "MACHINE_FACT_READ_ONLY": "机器事实为只读，不能被人工覆盖。",
            "SKIP_REQUIRES_MACHINE_FAILURE": "只有存在机器失败证据时才能审核跳过。",
            "REVIEW_REQUIRES_ANNOTATION": "该审核任务尚无可接受的语义标注。",
            "REVIEW_TASKS_OPEN": "仍有未解决审核任务。",
            "RUN_STATE_CONFLICT": "当前运行状态不允许此操作，请刷新后重试。",
            "AI_BATCH_PLAN_CONFLICT": "AI 批次计划已变化，请重新预览。",
            "AI_RETRY_WAVE_CONFLICT": "Provider 重试波次已变化，请重新预览。",
            "PROVIDER_RETRY_NOT_ELIGIBLE": "该 Provider 批次当前不可重试。",
        }
        return status, code, messages.get(code, "R3 操作未完成，请检查当前状态。"), code == "WORKER_UNAVAILABLE", {}
    if isinstance(exc, ImportCheckNotFound):
        return 404, "IMPORT_NOT_FOUND", "导入检查不存在或已失效，请重新检查。", False, {}
    if isinstance(exc, ImportCheckInProgress):
        return 409, "IMPORT_CHECK_IN_PROGRESS", "完整性检查仍在进行，请等待完成。", False, {}
    if isinstance(exc, ImportCheckProgressPersistFailed):
        return 500, "IMPORT_CHECK_PROGRESS_PERSIST_FAILED", "导入检查状态无法安全保存。", False, {}
    if isinstance(exc, ImportNotAllowed):
        code = getattr(exc, "code", None) or "IMPORT_INCOMPLETE"
        return 422, code, "导出包未通过完整性检查，请修复后重新检查。", False, {}
    if isinstance(exc, DirectoryRefNotFound):
        return 404, "DIRECTORY_REF_NOT_FOUND", "目录引用已失效，请重新选择目录。", False, {}
    if isinstance(exc, DirectoryRefStale):
        return 409, "DIRECTORY_REF_STALE", "目录已发生变化，请重新选择目录。", False, {}
    if isinstance(exc, DirectoryChooserError):
        return 400, getattr(exc, "code", "DIRECTORY_REF_INVALID"), "目录引用不合法，请重新选择目录。", False, {}
    if isinstance(exc, RunStateConflict):
        return 409, "RUN_STATE_CONFLICT", "当前运行状态不允许此操作，请刷新后重试。", False, {}
    if isinstance(exc, KeyError):
        return 404, "RUN_NOT_FOUND", "找不到指定运行。", False, {}
    if isinstance(exc, WorkerUnavailable) or getattr(exc, "code", None) == "WORKER_UNAVAILABLE":
        return 503, "WORKER_UNAVAILABLE", "内置 Worker 当前不可用。请重启 Index Studio 后再试。", True, {}
    if isinstance(exc, (ExportPathError, UnsafeReference, ValueError)):
        return 400, "INVALID_INPUT", "输入不合法，请检查版本和本地目录后重试。", False, {}
    return 500, "INTERNAL_ERROR", "本地操作未完成。请按请求编号检查诊断信息。", False, {}


def _partial_exception(
    templates: Jinja2Templates,
    request: Request,
    exc: Exception,
    repair_hint: str,
) -> HTMLResponse:
    status, code, message, retryable, fields = _exception_details(exc)
    return templates.TemplateResponse(
        request=request,
        name="partials/error.html",
        context={
            "request": request,
            "error_code": code,
            "message": message,
            "field_errors": fields,
            "repair_hint": repair_hint,
            "request_id": _request_id(request),
            "retryable": retryable,
        },
        status_code=status,
    )


def _page_exception(templates: Jinja2Templates, request: Request, exc: Exception) -> HTMLResponse:
    status, code, message, retryable, fields = _exception_details(exc)
    context = {
        "request": request,
        "page_title": "无法打开页面",
        "current_page": "",
        "data_root_configured": True,
        "startup_stale": [],
        "worker_available": request.app.state.worker_available,
        "error_code": code,
        "message": message,
        "field_errors": fields,
        "request_id": _request_id(request),
        "retryable": retryable,
    }
    return templates.TemplateResponse(
        request=request,
        name="page_error.html",
        context=context,
        status_code=status,
    )


def _page_context(request: Request, app: FastAPI, **values: Any) -> dict[str, Any]:
    return {
        "request": request,
        "data_root_configured": True,
        "startup_stale": list(app.state.stale_markers),
        "worker_available": app.state.worker_available,
        **values,
    }


def _run_counts(runs: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    return {
        "all": len(runs),
        "active": sum(run.get("status") in {"pending", "running"} for run in runs),
        "attention": sum(run.get("status") in {"failed", "needs_review"} for run in runs),
    }
