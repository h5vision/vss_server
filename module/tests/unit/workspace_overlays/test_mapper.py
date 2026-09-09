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

    submission = to_vss_index_command(
        frontend,
        vss_project_id="vss-server--module",
        materialized_project_root="/srv/snapshots/vss-server--module/bbbbbbbb",
        snapshot_id="snapshot-internal-id",
        profile=VssIndexProfile(context_header=True, use_bm25=True),
    )

    assert submission.request.project_id == "vss-server--module"
    assert submission.expected_revision == frontend.target_revision
    assert submission.snapshot_id == "snapshot-internal-id"
    assert "files" not in submission.request.model_dump()
    assert submission.request.model_dump(exclude_none=True) == {
        "project_root": "/srv/snapshots/vss-server--module/bbbbbbbb",
        "project_id": "vss-server--module",
        "profile": {"context_header": True, "use_bm25": True},
        "force": False,
        "briefing": True,
        "note": f"snapshot {'B' * 40}",
    }
