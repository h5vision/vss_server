"""Build the internal VSS indexing command after a Snapshot tree is materialized."""

from __future__ import annotations

from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest
from backend.integrations.vss.schemas import VssIndexCommand, VssIndexProfile


def to_vss_index_command(
    request: WorkspaceOverlayRequest,
    *,
    vss_project_id: str,
    materialized_project_root: str,
    snapshot_id: str,
    profile: VssIndexProfile | None = None,
    force: bool = False,
) -> VssIndexCommand:
    """Create a command without pretending VSS accepts the Frontend delta payload."""

    return VssIndexCommand(
        project_root=materialized_project_root,
        project_id=vss_project_id,
        expected_revision=request.target_revision,
        snapshot_id=snapshot_id,
        profile=profile,
        force=force,
    )
