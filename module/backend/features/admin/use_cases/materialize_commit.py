"""Application Use Case: Materialize a Git commit into an immutable Snapshot."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from backend.features.admin.audit import record_audit
from backend.features.admin.schemas import AdminCommitMaterializeResponse
from backend.features.admin.store import AdminStore
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.materializer import (
    CollectedRevisionMaterializer,
)


@dataclass(frozen=True, slots=True)
class MaterializeCommitUseCase:
    materializer: CollectedRevisionMaterializer
    git_client: RepositoryGitClient
    session: AsyncSession

    async def execute(
        self,
        *,
        repository_id: UUID,
        commit_sha: str,
        actor_id: str,
        request_id: UUID,
        vss_project_id: str | None = None,
        branch_ref: str | None = None,
    ) -> AdminCommitMaterializeResponse:
        admin_store = AdminStore(self.session)
        result = await admin_store.materialize_commit(
            repository_id=repository_id,
            commit_sha=commit_sha,
            request_id=request_id,
            vss_project_id=vss_project_id,
            branch_ref=branch_ref,
            materializer=self.materializer,
            git_client=self.git_client,
        )

        await record_audit(
            self.session,
            request_id=request_id,
            actor=actor_id,
            action="materialize_commit",
            target_type="repository",
            target_id=str(repository_id),
            outcome="succeeded",
            details={
                "commit_sha": commit_sha,
                "snapshot_id": str(result.snapshot_id),
                "created": result.created,
                "state": result.state,
                "materialized_locator": result.materialized_locator,
            },
        )

        return result
