"""Schemas for GitHub Webhook payloads and responses."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class GitHubWebhookRepo(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None
    full_name: str | None = None
    clone_url: str | None = None
    html_url: str | None = None
    ssh_url: str | None = None
    git_url: str | None = None
    default_branch: str | None = None


class GitHubPushWebhookPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    ref: str | None = None
    before: str | None = None
    after: str | None = None
    created: bool | None = None
    deleted: bool | None = None
    forced: bool | None = None
    repository: GitHubWebhookRepo | None = None
    zen: str | None = None
    hook_id: int | None = None


class WebhookResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    ok: Literal[True] = True
    reason: str
    detail: str
    event: str
    repository_id: UUID | None = None
    branch_ref: str | None = None
    after: str | None = None
    summary: dict[str, Any] | None = None

