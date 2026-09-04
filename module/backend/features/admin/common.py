"""Shared dependencies and helper mappers for Admin sub-routers."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.errors import ApiError
from backend.features.admin.auth import AdminIdentity, require_admin_role
from backend.features.repositories.schemas import (
    BranchBindingResponse,
    RepositoryResponse,
)
from backend.features.repositories.store import StoreLookupError
from backend.features.repository_collection.errors import CollectionError
from backend.features.snapshots.schemas import (
    SnapshotAttemptResponse,
    SnapshotDetailResponse,
    SnapshotSummaryResponse,
)
from backend.infrastructure.database.models import (
    BranchBinding,
    Repository,
    Snapshot,
)
from backend.infrastructure.database.session import get_db_session

DbSession = Annotated[AsyncSession, Depends(get_db_session)]
Viewer = Annotated[AdminIdentity, Depends(require_admin_role("viewer"))]
Operator = Annotated[AdminIdentity, Depends(require_admin_role("operator"))]
Administrator = Annotated[AdminIdentity, Depends(require_admin_role("admin"))]

SAFE_VSS_RESULT_FIELDS = {
    "accepted",
    "project_id",
    "state",
    "reason",
    "commit",
    "chunks",
    "indexed_at",
}


def _repository_response(repository: Repository) -> RepositoryResponse:
    return RepositoryResponse.model_validate(
        {
            "repository_id": repository.repository_id,
            "canonical_name": repository.canonical_name,
            "display_name": repository.display_name,
            "provider": repository.provider,
            "remote_url": repository.remote_url,
            "default_branch_ref": repository.default_branch_ref,
            "active": repository.active,
            "created_at": repository.created_at,
            "updated_at": repository.updated_at,
        }
    )


def _binding_response(binding: BranchBinding) -> BranchBindingResponse:
    return BranchBindingResponse.model_validate(binding, from_attributes=True)


def _tracked_response(branch) -> object:
    from backend.features.admin.schemas import TrackedBranchAdminResponse

    return TrackedBranchAdminResponse.model_validate(branch, from_attributes=True)


def _safe_locator(snapshot: Snapshot) -> str | None:
    if snapshot.materialized_locator is None:
        return None
    return f"revision:{snapshot.target_revision}"


def _snapshot_summary(snapshot: Snapshot) -> SnapshotSummaryResponse:
    return SnapshotSummaryResponse(
        snapshot_id=snapshot.snapshot_id,
        request_id=snapshot.request_id,
        binding_id=snapshot.binding_id,
        tracked_branch_id=snapshot.tracked_branch_id,
        frontend_project_id=snapshot.frontend_project_id,
        repository_id=snapshot.repository_id,
        branch_ref=snapshot.branch_ref,
        vss_project_id=snapshot.vss_project_id,
        base_revision=snapshot.base_revision,
        target_revision=snapshot.target_revision,
        source_type=snapshot.source_type,
        state=snapshot.state,
        attempt_count=snapshot.attempt_count,
        materialized_locator=_safe_locator(snapshot),
        vss_state=snapshot.vss_state,
        vss_reason=snapshot.vss_reason,
        vss_detail=snapshot.vss_detail,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _safe_vss_result(value: dict | None) -> dict | None:
    if value is None:
        return None
    return {key: value[key] for key in SAFE_VSS_RESULT_FIELDS if key in value}


def _snapshot_detail(snapshot: Snapshot) -> SnapshotDetailResponse:
    summary = _snapshot_summary(snapshot).model_dump()
    attempts = [
        SnapshotAttemptResponse(
            attempt_id=attempt.attempt_id,
            snapshot_id=attempt.snapshot_id,
            request_id=attempt.request_id,
            attempt_number=attempt.attempt_number,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            upstream_status_code=attempt.upstream_status_code,
            vss_state=attempt.vss_state,
            vss_reason=attempt.vss_reason,
            vss_detail=attempt.vss_detail,
            retryable=attempt.retryable,
            latency_ms=attempt.latency_ms,
            vss_result_json=_safe_vss_result(attempt.vss_result_json),
        )
        for attempt in snapshot.attempts
    ]
    return SnapshotDetailResponse(
        **summary,
        attempts=attempts,
        changed_file_count=sum(
            item.status in {"added", "modified", "renamed"} for item in snapshot.deltas
        ),
        deleted_path_count=sum(item.status == "deleted" for item in snapshot.deltas),
        rename_count=sum(item.status == "renamed" for item in snapshot.deltas),
    )


def _not_found(error: StoreLookupError) -> ApiError:
    return ApiError(
        status_code=404,
        reason=error.reason,
        detail=error.detail,
        retryable=error.retryable,
    )


def _collection_error(error: CollectionError) -> ApiError:
    return ApiError(
        status_code=error.status_code,
        reason=error.reason,
        detail=error.detail,
        retryable=error.retryable,
    )


def _collection_service(request: Request):
    service = getattr(request.app.state, "repository_collection_service", None)
    if service is None:
        raise ApiError(
            status_code=503,
            reason="ADMIN_DATABASE_UNAVAILABLE",
            detail="Repository collection is unavailable because the database is not configured.",
            retryable=True,
        )
    return service
