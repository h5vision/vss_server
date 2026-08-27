"""Common success and error envelopes consumed by Admin Web."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class AdminErrorResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: Literal[False] = False
    reason: str
    detail: str
    retryable: bool
    request_id: UUID


class AdminMutationResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    reason: str
    detail: str
    request_id: UUID
    resource: dict[str, Any]
