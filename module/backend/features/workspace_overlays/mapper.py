"""Build the internal VSS indexing command after a Snapshot tree is materialized."""

from __future__ import annotations

from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest
from backend.integrations.vss.schemas import (
    VssIndexProfile,
    VssIndexRequest,
    VssIndexSubmission,
)


def to_vss_index_command(
    request: WorkspaceOverlayRequest,
    *,
    vss_project_id: str,
    materialized_project_root: str,
    snapshot_id: str,
    profile: VssIndexProfile | None = None,
    force: bool = False,
) -> VssIndexSubmission:
    """Keep Backend metadata separate from the exact VSS HTTP request body."""

    index_request = VssIndexRequest(
        project_root=materialized_project_root,
        project_id=vss_project_id,
        profile=profile,
        force=force,
        briefing=True,
        note=f"snapshot {request.target_revision}",
    )
    return VssIndexSubmission(
        request=index_request,
        expected_revision=request.target_revision,
        snapshot_id=snapshot_id,
    )
