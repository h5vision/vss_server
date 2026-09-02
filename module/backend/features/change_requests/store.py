"""PR/MR current state and append-only revision observation persistence."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.features.change_requests.errors import ChangeRequestError
from backend.features.change_requests.schemas import ChangeRequestObservationRequest
from backend.infrastructure.database.models import (
    ChangeRequest,
    ChangeRequestRevision,
    Repository,
)


class ChangeRequestStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def observe(
        self,
        request: ChangeRequestObservationRequest,
    ) -> tuple[ChangeRequest, ChangeRequestRevision, bool]:
        repository = await self._session.get(Repository, request.repository_id)
        if repository is None:
            raise ChangeRequestError(
                reason="REPOSITORY_NOT_FOUND",
                detail="Change Request를 연결할 Repository를 찾을 수 없습니다.",
                retryable=False,
                status_code=404,
            )
        if not repository.active:
            raise ChangeRequestError(
                reason="REPOSITORY_INACTIVE",
                detail="비활성 Repository의 Change Request는 관측할 수 없습니다.",
                retryable=False,
                status_code=409,
            )
        if repository.provider.lower() != request.provider:
            raise ChangeRequestError(
                reason="CHANGE_REQUEST_PROVIDER_MISMATCH",
                detail="Repository provider와 Change Request provider가 일치하지 않습니다.",
                retryable=False,
                status_code=409,
            )

        change_request = await self._session.scalar(
            select(ChangeRequest).where(
                ChangeRequest.repository_id == request.repository_id,
                ChangeRequest.provider == request.provider,
                ChangeRequest.external_number == request.external_number,
            )
        )
        if change_request is None:
            change_request = ChangeRequest(
                repository_id=request.repository_id,
                provider=request.provider,
                external_number=request.external_number,
                kind=request.kind,
                state=request.state,
                title=request.title,
                base_ref=request.base_ref,
                head_ref=request.head_ref,
                current_base_sha=request.base_sha,
                current_head_sha=request.head_sha,
                current_merge_sha=request.merge_sha,
                last_observed_at=request.observed_at,
                provider_updated_at=request.provider_updated_at,
                merged_at=request.merged_at,
            )
            self._session.add(change_request)
            await self._session.flush()
        elif self._in_order(request.observed_at, change_request.last_observed_at):
            change_request.kind = request.kind
            change_request.state = request.state
            change_request.title = request.title
            change_request.base_ref = request.base_ref
            change_request.head_ref = request.head_ref
            change_request.current_base_sha = request.base_sha
            change_request.current_head_sha = request.head_sha
            change_request.current_merge_sha = request.merge_sha
            change_request.last_observed_at = request.observed_at
            change_request.provider_updated_at = request.provider_updated_at
            change_request.merged_at = request.merged_at

        observation_key = self._observation_key(request)
        revision = await self._session.scalar(
            select(ChangeRequestRevision).where(
                ChangeRequestRevision.change_request_id == change_request.change_request_id,
                ChangeRequestRevision.observation_key == observation_key,
            )
        )
        created = revision is None
        if revision is None:
            revision = ChangeRequestRevision(
                change_request_id=change_request.change_request_id,
                observation_key=observation_key,
                state=request.state,
                base_ref=request.base_ref,
                head_ref=request.head_ref,
                base_sha=request.base_sha,
                head_sha=request.head_sha,
                merge_sha=request.merge_sha,
                provider_updated_at=request.provider_updated_at,
                observed_at=request.observed_at,
            )
            self._session.add(revision)
        await self._session.flush()
        return change_request, revision, created

    @staticmethod
    def _in_order(observed_at: datetime, current_observed_at: datetime) -> bool:
        def normalized(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)

        return normalized(observed_at) >= normalized(current_observed_at)

    @staticmethod
    def _observation_key(request: ChangeRequestObservationRequest) -> str:
        payload = {
            "state": request.state,
            "base_ref": request.base_ref,
            "head_ref": request.head_ref,
            "base_sha": request.base_sha,
            "head_sha": request.head_sha,
            "merge_sha": request.merge_sha,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
