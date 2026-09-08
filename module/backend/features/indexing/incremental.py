"""Plan and submit incremental VSS indexing without moving Git ownership into VSS."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from backend.features.repository_collection.errors import CollectionError
from backend.integrations.vss.client import VssHttpClient
from backend.integrations.vss.errors import VssHttpRequestRejected, VssHttpUnavailable
from backend.integrations.vss.schemas import (
    VssIncrementalChange,
    VssIncrementalIndexRequest,
    VssIndexProfile,
    VssIndexRequest,
    VssStartIncrementalIndexResponse,
    VssStartIndexResponse,
)
from backend.ports.git import RevisionComparator

SubmissionMode = Literal["full", "incremental", "full_fallback"]


@dataclass(frozen=True, slots=True)
class IncrementalPlan:
    request: VssIncrementalIndexRequest | None
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class VssSubmissionResult:
    upstream: VssStartIndexResponse | VssStartIncrementalIndexResponse
    mode: SubmissionMode
    planning_fallback_reason: str | None = None
    incremental_fallback: dict[str, object] | None = None


def build_incremental_plan(
    *,
    revision_comparator: RevisionComparator | None,
    repository_id: UUID,
    project_id: str,
    branch_ref: str,
    project_root: Path,
    base_revision: str | None,
    target_revision: str,
    profile: VssIndexProfile | None = None,
    briefing: bool = True,
) -> IncrementalPlan:
    """Return an incremental request only when exact Git ancestry is safe.

    Git comparison failures are deliberately converted into a full-index plan.  Full
    indexing already verifies the exact target checkout and remains the compatibility
    path for repositories that do not have a usable Module Git cache.
    """

    if revision_comparator is None:
        return IncrementalPlan(request=None, fallback_reason="REVISION_COMPARATOR_UNAVAILABLE")
    if base_revision is None:
        return IncrementalPlan(request=None, fallback_reason="ACTIVE_REVISION_UNAVAILABLE")

    base = base_revision.strip().lower()
    target = target_revision.strip().lower()
    if base == target:
        return IncrementalPlan(request=None, fallback_reason="TARGET_ALREADY_INDEXED")

    try:
        compared = revision_comparator.compare_revisions(
            repository_id=repository_id,
            base_revision=base,
            target_revision=target,
        )
    except CollectionError as exc:
        return IncrementalPlan(request=None, fallback_reason=exc.reason)

    if compared.base_tree_sha is None or compared.target_tree_sha is None:
        return IncrementalPlan(request=None, fallback_reason="TREE_SHA_UNAVAILABLE")
    if compared.merge_base_revision != base:
        return IncrementalPlan(request=None, fallback_reason="NON_FAST_FORWARD")

    changes = [
        VssIncrementalChange(
            status="added" if item.change_type == "copied" else item.change_type,
            path=item.path,
            old_path=item.old_path if item.change_type == "renamed" else None,
        )
        for item in compared.changes
    ]
    return IncrementalPlan(
        request=VssIncrementalIndexRequest(
            project_id=project_id,
            branch_ref=branch_ref,
            project_root=str(project_root),
            profile=profile or VssIndexProfile(),
            base_revision=base,
            target_revision=target,
            base_tree_sha=compared.base_tree_sha,
            target_tree_sha=compared.target_tree_sha,
            changes=changes,
            force=False,
            briefing=briefing,
            note=f"incremental {base} -> {target}",
        )
    )


def submit_index_with_incremental_fallback(
    *,
    vss_client: VssHttpClient,
    full_request: VssIndexRequest,
    incremental_plan: IncrementalPlan,
) -> VssSubmissionResult:
    """Submit incremental when possible, otherwise preserve the existing full path.

    A pre-rag incremental precondition failure is an explicit request for a full
    rebuild.  An older pre-rag deployment without ``/index/incremental`` also falls
    back to ``/index`` so Module can be deployed before the consumer implementation.
    Network/contract failures are *not* retried as full because the incremental
    request may already have been accepted upstream.
    """

    incremental_request = incremental_plan.request
    if incremental_request is None:
        return VssSubmissionResult(
            upstream=vss_client.start_index(full_request),
            mode="full",
            planning_fallback_reason=incremental_plan.fallback_reason,
        )

    try:
        incremental = vss_client.start_incremental_index(incremental_request)
    except VssHttpRequestRejected as exc:
        if exc.upstream_status_code not in {404, 405}:
            raise
        return VssSubmissionResult(
            upstream=vss_client.start_index(full_request),
            mode="full_fallback",
            incremental_fallback={
                "reason": "INCREMENTAL_ENDPOINT_UNAVAILABLE",
                "upstream_status_code": exc.upstream_status_code,
            },
        )
    except VssHttpUnavailable as exc:
        if exc.upstream_status_code != 501:
            raise
        return VssSubmissionResult(
            upstream=vss_client.start_index(full_request),
            mode="full_fallback",
            incremental_fallback={
                "reason": "INCREMENTAL_ENDPOINT_UNAVAILABLE",
                "upstream_status_code": exc.upstream_status_code,
            },
        )

    result = incremental.result
    if result.accepted or result.reason == "already_indexed":
        return VssSubmissionResult(upstream=incremental, mode="incremental")

    if result.full_reindex_required or result.reason == "incremental_precondition_failed":
        return VssSubmissionResult(
            upstream=vss_client.start_index(full_request),
            mode="full_fallback",
            incremental_fallback={
                "reason": result.reason,
                "detail": result.detail,
                "full_reindex_required": result.full_reindex_required,
                "fingerprint": result.fingerprint,
            },
        )

    return VssSubmissionResult(upstream=incremental, mode="incremental")
