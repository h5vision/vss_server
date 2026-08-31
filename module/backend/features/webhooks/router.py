"""GitHub Webhook API router supporting /postrecive, /postreceive, and /v1/webhooks/github."""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import Settings, get_settings
from backend.core.errors import ApiError
from backend.features.collection.service import RepositoryCollectionService
from backend.infrastructure.database.session import get_db_session
from backend.features.webhooks.schemas import (
    GitHubPushWebhookPayload,
    WebhookResponse,
)
from backend.features.webhooks.service import (
    find_matching_repository,
    verify_github_signature,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


async def handle_github_webhook(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    x_github_event: str | None = None,
    x_hub_signature_256: str | None = None,
) -> WebhookResponse:
    raw_body = await request.body()

    # 1. 서명 검증
    secret = (
        settings.snapshot_webhook_secret.get_secret_value()
        if settings.snapshot_webhook_secret
        else (
            settings.github_webhook_secret.get_secret_value()
            if settings.github_webhook_secret
            else None
        )
    )
    verify_github_signature(
        payload_bytes=raw_body,
        signature_header=x_hub_signature_256,
        secret=secret,
    )

    # 2. JSON 파싱
    try:
        data = json.loads(raw_body.decode("utf-8") or "{}")
        payload = GitHubPushWebhookPayload.model_validate(data)
    except Exception as exc:
        raise ApiError(
            status_code=400,
            reason="INVALID_WEBHOOK_PAYLOAD",
            detail="JSON 페이로드를 파싱할 수 없습니다.",
            retryable=False,
        ) from exc

    event_type = x_github_event or ("ping" if payload.zen else "push")

    # 3. Ping 이벤트 처리
    if event_type == "ping":
        logger.info("github_webhook_ping_received zen=%s", payload.zen)
        return WebhookResponse(
            reason="PONG",
            detail=f"GitHub Webhook Ping 수신 성공 (zen: {payload.zen or 'OK'})",
            event="ping",
        )

    # 4. Push 이벤트 처리
    if event_type != "push":
        return WebhookResponse(
            reason="EVENT_IGNORED",
            detail=f"처리 대상이 아닌 이벤트 타입입니다: {event_type}",
            event=event_type,
        )

    matched_repo = await find_matching_repository(session, payload.repository)
    if not matched_repo:
        repo_name = payload.repository.full_name if payload.repository else "unknown"
        logger.info("github_webhook_unregistered_repo repo=%s", repo_name)
        return WebhookResponse(
            reason="REPOSITORY_NOT_TRACKED",
            detail=f"시스템에 등록된 활성 Repository와 매칭되지 않았습니다 ({repo_name}).",
            event="push",
            branch_ref=payload.ref,
            after=payload.after,
        )

    # 5. 수집 및 VSS 인덱싱 동기화 트리거
    service: RepositoryCollectionService = request.app.state.collection_service
    summary = await service.sync_repository(matched_repo.repository_id, trigger="webhook")

    summary_dict = {
        "sync_run_id": str(summary.sync_run_id),
        "observed_branches": summary.observed_branches,
        "changed_branches": summary.changed_branches,
        "snapshots_accepted": summary.snapshots_accepted,
        "snapshot_failures": summary.snapshot_failures,
    }

    return WebhookResponse(
        reason=(
            "SYNC_COMPLETED"
            if summary.snapshot_failures == 0
            else "SYNC_COMPLETED_WITH_FAILURES"
        ),
        detail=(
            f"Webhook 동기화 완료: 저장소 [{matched_repo.display_name}] "
            f"브랜치 [{payload.ref or 'N/A'}] "
            f"(관측: {summary.observed_branches}, "
            f"변경: {summary.changed_branches}, "
            f"접수: {summary.snapshots_accepted})"
        ),
        event="push",
        repository_id=matched_repo.repository_id,
        branch_ref=payload.ref,
        after=payload.after,
        summary=summary_dict,
    )


# 라우트 등록 (지정된 /postrecive, 오타 방지용 /postreceive, 표준 /v1/webhooks/github 모두 지원)
@router.post("/postrecive", response_model=WebhookResponse, status_code=status.HTTP_200_OK)
@router.post("/postreceive", response_model=WebhookResponse, status_code=status.HTTP_200_OK)
@router.post("/webhooks/github", response_model=WebhookResponse, status_code=status.HTTP_200_OK)
@router.post("/v1/webhooks/github", response_model=WebhookResponse, status_code=status.HTTP_200_OK)
async def github_webhook_endpoint(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_github_event: str | None = Header(default=None, alias="X-GitHub-Event"),
    x_hub_signature_256: str | None = Header(default=None, alias="X-Hub-Signature-256"),
) -> WebhookResponse:
    resolved_settings = getattr(request.app.state, "settings", None) or settings
    return await handle_github_webhook(
        request=request,
        session=session,
        settings=resolved_settings,
        x_github_event=x_github_event,
        x_hub_signature_256=x_hub_signature_256,
    )
