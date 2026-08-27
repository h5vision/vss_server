"""Frontend indexing status route."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, Request

from backend.core.errors import ApiError
from backend.features.indexing.schemas import IndexStatusResponse
from backend.features.indexing.service import IndexStatusService

router = APIRouter(tags=["index status"])


@router.get("/index/status", response_model=IndexStatusResponse)
async def get_index_status(
    request: Request,
    project_id: str = Query(min_length=1),
) -> IndexStatusResponse:
    sessionmaker = request.app.state.db_sessionmaker
    if sessionmaker is None:
        raise ApiError(
            status_code=503,
            reason="DATABASE_NOT_CONFIGURED",
            detail="Snapshot 데이터베이스가 구성되지 않았습니다.",
            retryable=False,
        )
    return await IndexStatusService(
        sessionmaker=sessionmaker,
        vss_client=request.app.state.vss_client,
    ).read_for_project(
        project_id,
        request_id=UUID(request.state.request_id),
    )
