"""Service logic for GitHub Webhook verification and processing."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.errors import ApiError
from backend.features.webhooks.schemas import GitHubWebhookRepo
from backend.infrastructure.database.models import Repository

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def verify_github_signature(
    *,
    payload_bytes: bytes,
    signature_header: str | None,
    secret: str | None,
) -> None:
    """GitHub X-Hub-Signature-256 HMAC 검증."""
    if not secret:
        return

    if not signature_header or not signature_header.startswith("sha256="):
        logger.warning("webhook_signature_missing_or_malformed")
        raise ApiError(
            status_code=401,
            reason="INVALID_WEBHOOK_SIGNATURE",
            detail="X-Hub-Signature-256 헤더가 누락되었거나 형식이 올바르지 않습니다.",
            retryable=False,
        )

    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"), payload_bytes, hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature_header, expected):
        logger.warning("webhook_signature_mismatch")
        raise ApiError(
            status_code=401,
            reason="INVALID_WEBHOOK_SIGNATURE",
            detail="Webhook HMAC 서명 검증에 실패했습니다.",
            retryable=False,
        )


def _normalize_url(url: str | None) -> str:
    if not url:
        return ""
    cleaned = url.strip().rstrip("/").lower()
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    return cleaned


async def find_matching_repository(
    session: AsyncSession,
    repo_info: GitHubWebhookRepo | None,
) -> Repository | None:
    """Webhook 페이로드로부터 시스템에 등록된 활성 Repository를 매칭한다."""
    if not repo_info:
        return None

    statement = select(Repository).where(Repository.active.is_(True))
    repositories = list(await session.scalars(statement))

    candidate_urls = {
        _normalize_url(u)
        for u in (
            repo_info.clone_url,
            repo_info.html_url,
            repo_info.ssh_url,
            repo_info.git_url,
        )
        if u
    }
    candidate_names = {
        n.strip().lower()
        for n in (repo_info.full_name, repo_info.name)
        if n
    }

    # 1. URL 정확도 우선 매칭
    for repo in repositories:
        repo_url = _normalize_url(repo.remote_url)
        if repo_url and repo_url in candidate_urls:
            return repo

    # 2. Canonical Name / Display Name 매칭
    for repo in repositories:
        if (
            repo.canonical_name.strip().lower() in candidate_names
            or repo.display_name.strip().lower() in candidate_names
        ):
            return repo

    return None
