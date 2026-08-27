"""Health HTTP routes."""

from __future__ import annotations

from fastapi import APIRouter, Request

from backend.core.config import Settings
from backend.features.health.schemas import HealthResponse, ReadinessResponse
from backend.features.health.service import liveness, readiness

router = APIRouter(tags=["health"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


@router.get("/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    return liveness(_settings(request))


@router.get("/health/ready", response_model=ReadinessResponse)
def get_readiness(request: Request) -> ReadinessResponse:
    return readiness(_settings(request))
