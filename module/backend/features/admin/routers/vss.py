"""Admin VSS integration routes."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.common import Administrator, DbSession, Viewer
from backend.features.admin.pagination import decode_cursor, paginate
from backend.features.admin.schemas import (
    AdminMutationResponse,
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


def _admin_vss_project(item) -> AdminVssProjectItem:
    return AdminVssProjectItem(
        project_id=item.project_id,
        state=item.state.value,
        commit=item.commit,
        chunks=item.chunks,
        indexed_at=item.indexed_at,
    )


def _raise_vss_error(exc: VssIntegrationError, *, detail: str) -> None:
    raise ApiError(
        status_code=503 if exc.retryable else 502,
        reason=exc.reason,
        detail=detail,
        retryable=exc.retryable,
    ) from exc


@router.get("/vss/projects", response_model=AdminVssProjectsResponse)
async def list_vss_projects(request: Request, _identity: Viewer) -> AdminVssProjectsResponse:
    try:
        response = await run_in_threadpool(request.app.state.vss_client.list_projects)
    except VssIntegrationError as exc:
        _raise_vss_error(exc, detail="VSS project catalog is unavailable.")
    return AdminVssProjectsResponse(items=[_admin_vss_project(item) for item in response.projects])


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
