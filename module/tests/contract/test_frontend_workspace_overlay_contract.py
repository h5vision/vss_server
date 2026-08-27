from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest

FIXTURES = Path(__file__).parents[1] / "fixtures"


def load_frontend_fixture() -> dict:
    return json.loads((FIXTURES / "frontend" / "workspace_overlay.json").read_text("utf-8"))


def test_current_frontend_payload_is_accepted_without_new_required_fields() -> None:
    payload = load_frontend_fixture()

    request = WorkspaceOverlayRequest.model_validate(payload)

    assert request.project_id == "h5vision/vision"
    assert set(request.model_dump()) == {
        "project_id",
        "base_revision",
        "target_revision",
        "files",
        "deleted_paths",
        "renames",
    }


def test_empty_git_commit_is_a_valid_revision_only_request() -> None:
    payload = load_frontend_fixture()
    payload.update(files=[], deleted_paths=[], renames=[])

    request = WorkspaceOverlayRequest.model_validate(payload)

    assert request.base_revision != request.target_revision
    assert request.files == []


@pytest.mark.parametrize("field", ["snapshot_id", "content_sha256", "size_bytes", "branch"])
def test_frontend_only_contract_does_not_silently_accept_backend_fields(field: str) -> None:
    payload = load_frontend_fixture()
    payload[field] = "not-a-frontend-field"

    with pytest.raises(ValidationError):
        WorkspaceOverlayRequest.model_validate(payload)


def test_local_unpushed_sha_shape_is_accepted_without_remote_lookup() -> None:
    payload = load_frontend_fixture()
    payload["target_revision"] = "ABCDEF0123456789ABCDEF0123456789ABCDEF01"

    request = WorkspaceOverlayRequest.model_validate(payload)

    assert request.target_revision == payload["target_revision"]


def test_invalid_revision_has_a_field_level_validation_error() -> None:
    payload = load_frontend_fixture()
    payload["base_revision"] = "not-a-git-sha"

    with pytest.raises(ValidationError) as captured:
        WorkspaceOverlayRequest.model_validate(payload)

    assert captured.value.errors()[0]["loc"] == ("base_revision",)
