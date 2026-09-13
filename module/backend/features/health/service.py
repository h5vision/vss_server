"""Liveness and runtime dependency readiness decisions."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.concurrency import run_in_threadpool

from backend import __version__
from backend.core.config import Settings
from backend.core.errors import ApiError
from backend.features.health.schemas import HealthResponse, ReadinessResponse
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssIntegrationError


def liveness(settings: Settings) -> HealthResponse:
    return HealthResponse(service=settings.app_name, version=__version__)


async def readiness(
    settings: Settings,
    *,
    database_engine: AsyncEngine | None,
    vss_client: VssHttpClient,
) -> ReadinessResponse:
    missing: list[str] = []
    if not settings.database_url:
        missing.append("DATABASE_URL")

    if missing:
        raise ApiError(
            status_code=503,
            reason="SERVICE_NOT_READY",
            detail="Required runtime configuration is incomplete.",
            retryable=False,
            extra={"missing": missing},
        )

    if database_engine is None:
        raise ApiError(
            status_code=503,
            reason="DATABASE_NOT_CONFIGURED",
            detail="Snapshot PostgreSQL 연결이 구성되지 않았습니다.",
            retryable=False,
        )

    try:
        async with database_engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except SQLAlchemyError as exc:
        raise ApiError(
            status_code=503,
            reason="DATABASE_UNAVAILABLE",
            detail="Snapshot PostgreSQL 연결을 확인할 수 없습니다.",
            retryable=True,
        ) from exc

    try:
        await run_in_threadpool(vss_client.health)
        await run_in_threadpool(vss_client.list_projects)
    except VssIntegrationError as exc:
        raise ApiError(
            status_code=503,
            reason=exc.reason,
            detail="VSS health/projects readiness 확인에 실패했습니다.",
            retryable=exc.retryable,
        ) from exc

    return ReadinessResponse(service=settings.app_name, version=__version__)
