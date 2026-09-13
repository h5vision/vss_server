"""Application Use Case: Compare Revisions between two Git commits."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.audit import record_audit
from backend.features.admin.schemas import (
    AdminCommitCompareChangeItem,
    AdminCommitCompareResponse,
)
from backend.features.admin.store import AdminStore
from backend.features.repositories.store import RepositoryStore, StoreLookupError
from backend.features.repository_collection.git_client import GitCompareResult
from backend.ports.git import RevisionComparator


@dataclass(frozen=True, slots=True)
class CompareRevisionsUseCase:
    comparator: RevisionComparator
    session: AsyncSession

    async def execute(
        self,
        *,
        repository_id: UUID,
        base_revision: str,
        target_revision: str,
        actor_id: str,
        request_id: UUID,
    ) -> AdminCommitCompareResponse:
        try:
            await RepositoryStore(self.session).get(repository_id)
        except StoreLookupError as exc:
            raise ApiError(
                status_code=404,
                reason=exc.reason,
                detail=exc.detail,
                retryable=False,
            ) from exc

        try:
            compare_result: GitCompareResult = await run_in_threadpool(
                self.comparator.compare_revisions,
                repository_id=repository_id,
                base_revision=base_revision,
                target_revision=target_revision,
            )
        except Exception as exc:
            raise ApiError(
                status_code=400,
                reason="COMPARE_FAILED",
                detail=f"커밋 비교에 실패했습니다: {exc}",
                retryable=False,
            ) from exc

        admin_store = AdminStore(self.session)
        base_status = await admin_store.get_revision_status(repository_id, base_revision)
        target_status = await admin_store.get_revision_status(repository_id, target_revision)

        await record_audit(
            self.session,
            request_id=request_id,
            actor=actor_id,
            action="compare_commits",
            target_type="repository",
            target_id=str(repository_id),
            outcome="succeeded",
            details={
                "base_revision": base_revision,
                "target_revision": target_revision,
                "files_changed": compare_result.files_changed,
                "additions": compare_result.additions,
                "deletions": compare_result.deletions,
            },
        )

        return AdminCommitCompareResponse(
            ok=True,
            repository_id=repository_id,
            base_revision=compare_result.base_revision,
            target_revision=compare_result.target_revision,
            merge_base_revision=compare_result.merge_base_revision,
            ahead_count=compare_result.ahead_count,
            behind_count=compare_result.behind_count,
            files_changed=compare_result.files_changed,
            additions=compare_result.additions,
            deletions=compare_result.deletions,
            changes=[
                AdminCommitCompareChangeItem(
                    path=c.path,
                    change_type=c.change_type,
                    old_path=c.old_path,
                )
                for c in compare_result.changes
            ],
            base_status=base_status,
            target_status=target_status,
        )
