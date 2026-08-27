from __future__ import annotations

from backend.features.workspace_overlays.mapper import to_vss_index_command
from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest
from backend.integrations.vss.schemas import VssIndexProfile


def test_mapper_preserves_full_content_paths_and_revisions() -> None:
    frontend = WorkspaceOverlayRequest.model_validate(
        {
            "project_id": "h5vision/vision",
            "base_revision": "A" * 40,
            "target_revision": "B" * 40,
            "files": [
                {
                    "status": "modified",
                    "path": "vision/src/file.ts",
                    "content": "전체 파일 문자열\n두 번째 줄",
                    "encoding": "utf-8",
                }
            ],
            "deleted_paths": [],
            "renames": [],
        }
    )

    command = to_vss_index_command(
        frontend,
        vss_project_id="vss-server--module",
        materialized_project_root="/srv/snapshots/vss-server--module/bbbbbbbb",
        snapshot_id="snapshot-internal-id",
        profile=VssIndexProfile(context_header=True, use_bm25=True),
    )

    assert command.project_id == "vss-server--module"
    assert command.expected_revision == frontend.target_revision
    assert command.snapshot_id == "snapshot-internal-id"
    assert "files" not in command.model_dump()
    assert command.start_index_kwargs() == {
        "project_root": "/srv/snapshots/vss-server--module/bbbbbbbb",
        "project_id": "vss-server--module",
        "profile": {"context_header": True, "use_bm25": True},
        "blocking": False,
        "force": False,
        "extra_meta": {
            "snapshot_id": "snapshot-internal-id",
            "requested_revision": "B" * 40,
        },
    }
