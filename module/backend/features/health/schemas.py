"""Health response schemas."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    ok: Literal[True] = True
    service: str
    version: str
    status: Literal["alive"] = "alive"


class ReadinessResponse(BaseModel):
    ok: Literal[True] = True
    service: str
    version: str
    status: Literal["ready"] = "ready"
