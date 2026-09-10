"""Admin VSS integration routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import Administrator, DbSession, Viewer
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.admin.schemas import (
    AdminMutationResponse,
    AdminVssBriefingResponse,
    AdminVssBriefingStatusResponse,
    AdminVssIndexedFileItem,
    AdminVssProjectContentsResponse,
    AdminVssProjectItem,
    AdminVssProjectsResponse,
    AdminVssRequestFailureItem,
    AdminVssRequestFailureListResponse,
)
from backend.features.admin.store import AdminStore
from backend.integrations.vss.errors import (
    VssHttpRequestRejected,
    VssIntegrationError,
)

router = APIRouter()


def _briefing_status(value: Any) -> str | None:
    if isinstance(value, str):
        normalized = value.strip()
        return normalized or None
    if isinstance(value, dict):
        for key in ("status", "state"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _admin_vss_project(item) -> AdminVssProjectItem:
    return AdminVssProjectItem(
        project_id=item.project_id,
        state=item.state.value,
        commit=item.commit,
        head_commit=item.head_commit,
        chunks=item.chunks,
        indexed_at=item.indexed_at,
        dirty=item.dirty,
        stale=item.stale,
        current=item.current,
        chunker=item.chunker,
        use_bm25=item.use_bm25,
        bm25_docs=item.bm25_docs,
        context_header=item.context_header,
        briefing_status=_briefing_status(item.briefing),
    )


def _raise_vss_error(exc: VssIntegrationError, *, detail: str) -> None:
    raise ApiError(
        status_code=503 if exc.retryable else 502,
        reason=exc.reason,
        detail=detail,
        retryable=exc.retryable,
    ) from exc


def _raise_project_not_found(exc: VssHttpRequestRejected, *, detail: str) -> None:
    if exc.upstream_status_code == 404:
        raise ApiError(
            status_code=404,
            reason="VSS_PROJECT_NOT_FOUND",
            detail=detail,
            retryable=False,
        ) from exc
    _raise_vss_error(exc, detail=detail)


@router.get("/vss/projects", response_model=AdminVssProjectsResponse)
async def list_vss_projects(request: Request, _identity: Viewer) -> AdminVssProjectsResponse:
    try:
        response = await run_in_threadpool(request.app.state.vss_client.list_projects)
    except VssIntegrationError as exc:
        _raise_vss_error(exc, detail="VSS project catalog is unavailable.")
    return AdminVssProjectsResponse(items=[_admin_vss_project(item) for item in response.projects])


@router.get(
    "/vss/projects/{project_id}/contents",
    response_model=AdminVssProjectContentsResponse,
)
async def get_vss_project_contents(
    project_id: str,
    request: Request,
    _identity: Viewer,
    symbols: bool = Query(default=True),
) -> AdminVssProjectContentsResponse:
    try:
        response = await run_in_threadpool(
            request.app.state.vss_client.list_projects,
            project_id=project_id,
            include_files=True,
            include_symbols=symbols,
        )
    except VssHttpRequestRejected as exc:
        _raise_project_not_found(exc, detail="선택한 VSS project의 인덱스 내용을 찾을 수 없습니다.")
    except VssIntegrationError as exc:
        _raise_vss_error(exc, detail="VSS project index contents are unavailable.")
    index_id = response.index_id or project_id.strip()
    return AdminVssProjectContentsResponse(
        project_id=project_id.strip(),
        index_id=index_id,
        resolved_by=response.resolved_by,
        candidates=response.candidates,
        files=[
            AdminVssIndexedFileItem(
                path=item.path,
                type=item.type,
                chunks=item.chunks,
                line_max=item.line_max,
                symbols=item.symbols,
            )
            for item in response.files
        ],
    )


@router.get(
    "/vss/projects/{project_id}/briefing",
    response_model=AdminVssBriefingResponse,
)
async def get_vss_project_briefing(
    project_id: str,
    request: Request,
    _identity: Viewer,
) -> AdminVssBriefingResponse:
    try:
        response = await run_in_threadpool(request.app.state.vss_client.briefing, project_id)
    except VssHttpRequestRejected as exc:
        _raise_project_not_found(exc, detail="선택한 VSS project의 briefing이 아직 없습니다.")
    except VssIntegrationError as exc:
        _raise_vss_error(exc, detail="VSS project briefing is unavailable.")
    return AdminVssBriefingResponse(
        project_id=project_id.strip(),
        index_id=response.index_id or project_id.strip(),
        briefing=response.briefing,
        model=response.model,
        commit=response.commit,
        generated_at=response.generated_at,
        quality_status=response.quality_status,
        run_id=response.run_id,
        pipeline_version=response.pipeline_version,
        structure=response.structure,
        routes=response.routes,
        topics=response.topics,
        problems=response.problems,
        coverage=response.coverage,
        metrics=response.metrics,
        materials=response.materials,
        references=response.references,
        reference_files=response.reference_files,
        rag=response.rag,
        cited=response.cited,
        truncated=response.truncated,
    )


@router.get(
    "/vss/projects/{project_id}/briefing/status",
    response_model=AdminVssBriefingStatusResponse,
)
async def get_vss_project_briefing_status(
    project_id: str,
    request: Request,
    _identity: Viewer,
) -> AdminVssBriefingStatusResponse:
    try:
        response = await run_in_threadpool(request.app.state.vss_client.briefing_status, project_id)
    except VssHttpRequestRejected as exc:
        _raise_project_not_found(
            exc,
            detail="선택한 VSS project의 briefing 상태를 찾을 수 없습니다.",
        )
    except VssIntegrationError as exc:
        _raise_vss_error(exc, detail="VSS project briefing status is unavailable.")
    return AdminVssBriefingStatusResponse(
        project_id=project_id.strip(),
        index_id=project_id.strip(),
        state=response.state,
        stage=response.stage,
        run_id=response.run_id,
        calls=response.calls,
        reason=response.reason,
        cleanup=response.cleanup,
        elapsed_s=response.elapsed_s,
        quality_status=response.quality_status,
        problems=response.problems,
        updated_at=response.updated_at,
    )


@router.get(
    "/vss/request-failures",
    response_model=AdminVssRequestFailureListResponse,
)
async def list_vss_request_failures(
    session: DbSession,
    _identity: Administrator,
    limit: int = Query(default=100, ge=1, le=500),
    cursor: str | None = None,
) -> AdminVssRequestFailureListResponse:
    offset = decode_cursor(cursor)
    rows = await AdminStore(session).list_vss_request_failures(
        limit=limit + 1,
        offset=offset,
    )
    entries, next_cursor = paginate(rows, limit=limit, offset=offset)
    items: list[AdminVssRequestFailureItem] = []
    for entry in entries:
        details = entry.details if isinstance(entry.details, dict) else {}
        query = details.get("query") if isinstance(details.get("query"), dict) else {}
        status_code = details.get("status_code")
        if not isinstance(status_code, int):
            try:
                status_code = int(str(entry.reason or "").removeprefix("HTTP_"))
            except ValueError:
                status_code = 500
        items.append(
            AdminVssRequestFailureItem(
                audit_id=entry.audit_id,
                request_id=entry.request_id,
                created_at=entry.created_at,
                method=str(details.get("method") or "UNKNOWN"),
                path=entry.target_id,
                status_code=status_code,
                project_id=(
                    str(details["project_id"])
                    if details.get("project_id") is not None
                    else None
                ),
                reason=entry.reason or f"HTTP_{status_code}",
                outcome="denied" if entry.outcome == "denied" else "failed",
                query=query,
            )
        )
    return AdminVssRequestFailureListResponse(items=items, next_cursor=next_cursor)


@router.delete(
    "/vss/projects/{project_id}",
    response_model=AdminMutationResponse,
)
async def delete_vss_project(
    project_id: str,
    request: Request,
    session: DbSession,
    identity: Administrator,
    confirm: str = Query(..., min_length=1),
) -> AdminMutationResponse:
    normalized = project_id.strip()
    if not normalized or confirm != normalized:
        raise ApiError(
            status_code=409,
            reason="VSS_PROJECT_DELETE_CONFIRMATION_REQUIRED",
            detail="Vector 삭제에는 선택한 VSS project_id의 정확한 확인 값이 필요합니다.",
            retryable=False,
        )

    try:
        catalog = await run_in_threadpool(request.app.state.vss_client.list_projects)
    except VssIntegrationError as exc:
        _raise_vss_error(exc, detail="VSS project catalog is unavailable before deletion.")
    target = next((item for item in catalog.projects if item.project_id == normalized), None)
    if target is None:
        raise ApiError(
            status_code=404,
            reason="VSS_PROJECT_NOT_FOUND",
            detail="선택한 VSS vector project를 찾을 수 없습니다.",
            retryable=False,
        )

    before = _admin_vss_project(target).model_dump(mode="json")
    try:
        await run_in_threadpool(request.app.state.vss_client.delete_project, normalized)
    except VssHttpRequestRejected as exc:
        if exc.upstream_status_code == 404:
            raise ApiError(
                status_code=501,
                reason="VSS_PROJECT_DELETE_UNSUPPORTED",
                detail=(
                    "현재 VSS 배포본에는 project 삭제 HTTP contract가 없습니다. "
                    "VSS Store의 drop 기능을 노출한 뒤 다시 시도해야 합니다."
                ),
                retryable=False,
            ) from exc
        _raise_vss_error(exc, detail="VSS rejected the vector project deletion request.")
    except VssIntegrationError as exc:
        _raise_vss_error(exc, detail="VSS vector project deletion failed.")

    try:
        exists = await run_in_threadpool(request.app.state.vss_client.exists, normalized)
    except VssIntegrationError as exc:
        _raise_vss_error(exc, detail="VSS deletion could not be verified.")
    if exists.exists:
        raise ApiError(
            status_code=502,
            reason="VSS_PROJECT_DELETE_NOT_CONFIRMED",
            detail="VSS가 삭제 성공을 반환했지만 vector project가 여전히 조회됩니다.",
            retryable=True,
        )

    resource = {
        **before,
        "deleted": True,
    }
    await record_audit(
        session,
        request_id=identity.request_id,
        actor=identity.actor_id,
        action="delete_vss_project",
        target_type="vss_project",
        target_id=normalized,
        before_json=before,
        after_json=resource,
    )
    return AdminMutationResponse(
        reason="VSS_PROJECT_DELETED",
        detail="선택한 VSS vector project 삭제를 확인했습니다.",
        request_id=identity.request_id,
        resource=resource,
    )
