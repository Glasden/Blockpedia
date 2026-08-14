"""Loopback Index Studio HTTP, template, and HTMX adapter for R2."""

from __future__ import annotations

import asyncio
import html
import json
import re
import math
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence
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
from .paths import ExportPathError, UnsafeReference, validate_minecraft_version
from .services import StudioService
from .stages import R2_STAGES, STUDIO_STAGES, RunStateConflict


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
    "AI_ANNOTATE": {"label": "AI 标注", "detail": "后续 R3 阶段", "phase": "R3", "future": True},
    "VALIDATE": {"label": "语义验证", "detail": "后续 R3 阶段", "phase": "R3", "future": True},
    "HUMAN_REVIEW": {"label": "人工审核", "detail": "后续 R3 阶段", "phase": "R3", "future": True},
    "BUILD_RELEASE": {"label": "构建候选", "detail": "后续 R3 阶段", "phase": "R3", "future": True},
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
) -> JSONResponse:
    return JSONResponse(
        jsonable_encoder(
            {
                "ok": True,
                "request_id": _request_id(request),
                "data": _safe_value(data),
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
        jobs.append(
            {
                "job_id": _safe_identifier(source.get("job_id"), optional=True),
                "stage": _safe_stage(source.get("stage")),
                "logical_key": _safe_identifier(source.get("logical_key")),
                "status": _safe_status(source.get("status")),
                "auto_attempt": _safe_int(source.get("auto_attempt"), 0),
                "heartbeat_at": _safe_optional_text(source.get("heartbeat_at")),
                "output_hash": _safe_hash(source.get("output_hash"), optional=True),
                "error_code": _safe_code(source.get("error_code"), optional=True),
                "error_message": "任务失败；请按错误码处理。" if source.get("error_message") else None,
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
    if boundary_event:
        warnings.append("R2 本地处理已到边界；AI 标注与发布属于后续阶段。")
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
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if _safe_mapping_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        return _clean_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if hasattr(value, "to_dict"):
        return _safe_value(value.to_dict())
    return "内容已隐藏。"


_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_SAFE_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_VERSION = re.compile(r"^[0-9]+\.[0-9]+(?:\.[0-9]+)?$")
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
    "sk-",
    "source-directory",
    "source_directory",
    "token",
)


def _clean_text(value: Any) -> str:
    """Return a whole public string or a stable replacement, never fragments."""

    text = str(value)
    folded = text.casefold()
    if "/" in text or "\\" in text or any(marker in folded for marker in _SENSITIVE_MARKERS):
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
    public_fields = {"action", "check_id", "copy_mode", "job_id", "limit", "minecraft_version", "query", "stage"}
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
