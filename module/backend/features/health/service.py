"""Fast local health decisions with no external network calls."""

from __future__ import annotations

from backend import __version__
from backend.core.config import Settings
from backend.core.errors import ApiError
from backend.features.health.schemas import HealthResponse, ReadinessResponse


def liveness(settings: Settings) -> HealthResponse:
    return HealthResponse(service=settings.app_name, version=__version__)


def readiness(settings: Settings) -> ReadinessResponse:
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

    return ReadinessResponse(service=settings.app_name, version=__version__)
