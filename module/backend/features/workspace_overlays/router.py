"""Frontend workspace overlay ingestion route."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Request, Response

from backend.core.errors import ApiError
from backend.features.workspace_overlays.schemas import (
    WorkspaceOverlayRequest,
    WorkspaceOverlayResponse,
)
from backend.features.workspace_overlays.service import WorkspaceOverlayService

router = APIRouter(tags=["workspace overlays"])


@router.post(
    "/workspace-overlays",
    response_model=WorkspaceOverlayResponse,
    status_code=202,
)
async def submit_workspace_overlay(
    payload: WorkspaceOverlayRequest,
    request: Request,
    response: Response,
) -> WorkspaceOverlayResponse:
    sessionmaker = request.app.state.db_sessionmaker
    if sessionmaker is None:
        raise ApiError(
            status_code=503,
            reason="DATABASE_NOT_CONFIGURED",
            detail="Snapshot 데이터베이스가 구성되지 않았습니다.",
            retryable=False,
        )

    service = WorkspaceOverlayService(
        sessionmaker=sessionmaker,
        materializer=request.app.state.snapshot_materializer,
        vss_client=request.app.state.vss_client,
    )
    outcome = await service.execute(
        payload,
        request_id=UUID(request.state.request_id),
    )
    response.status_code = outcome.status_code
    return outcome.body
