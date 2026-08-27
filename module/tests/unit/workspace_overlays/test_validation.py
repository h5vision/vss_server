from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest


def valid_payload() -> dict:
    return {
        "project_id": "h5vision/vision",
        "base_revision": "1" * 40,
        "target_revision": "2" * 40,
        "files": [
            {
                "status": "modified",
                "path": "vision/src/current.ts",
                "content": "const current = true;",
                "encoding": "utf-8",
            }
        ],
        "deleted_paths": [],
        "renames": [],
    }


@pytest.mark.parametrize(
    "path",
    [
        "/absolute.ts",
        "C:/windows.ts",
        "folder\\windows.ts",
        "folder/../escape.ts",
        "folder//empty.ts",
        "./relative.ts",
        "folder/\x00secret.ts",
    ],
)
def test_unsafe_paths_are_rejected(path: str) -> None:
    payload = valid_payload()
    payload["files"][0]["path"] = path

    with pytest.raises(ValidationError):
        WorkspaceOverlayRequest.model_validate(payload)


def test_changed_and_deleted_path_conflict_is_rejected() -> None:
    payload = valid_payload()
    payload["deleted_paths"] = ["vision/src/current.ts"]

    with pytest.raises(ValidationError, match="both changed and deleted"):
        WorkspaceOverlayRequest.model_validate(payload)


def test_rename_requires_destination_content() -> None:
    payload = valid_payload()
    payload["renames"] = [{"old_path": "vision/src/old.ts", "new_path": "vision/src/missing.ts"}]

    with pytest.raises(ValidationError, match="require final content"):
        WorkspaceOverlayRequest.model_validate(payload)


def test_duplicate_paths_are_rejected() -> None:
    payload = valid_payload()
    payload["files"].append(dict(payload["files"][0]))

    with pytest.raises(ValidationError, match="duplicate paths"):
        WorkspaceOverlayRequest.model_validate(payload)
