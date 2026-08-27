from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from backend.features.admin.schemas import AdminErrorResponse, AdminMutationResponse
from backend.features.repositories.schemas import (
    BranchBindingCreateRequest,
    BranchBindingResponse,
    RepositoryCreateRequest,
    RepositoryResponse,
    RepositoryUpdateRequest,
)
from backend.features.snapshots.schemas import (
    SnapshotListResponse,
    SnapshotRetryResponse,
    SnapshotState,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "admin"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def test_repository_response_contract() -> None:
    response = RepositoryResponse.model_validate(load_fixture("repository.json"))

    assert response.repository_id == UUID("55555555-5555-4555-8555-555555555555")
    assert response.canonical_name == "h5vision/vision"
    assert response.default_branch_ref == "refs/heads/main"


def test_repository_create_rejects_credentials_in_remote_url() -> None:
    payload = load_fixture("repository.json")
    payload.pop("created_at")
    payload.pop("updated_at")
    payload.pop("repository_id")
    payload["remote_url"] = "https://user:secret@github.com/h5vision/vision.git"

    with pytest.raises(ValidationError):
        RepositoryCreateRequest.model_validate(payload)


def test_repository_create_uses_server_generated_id_contract() -> None:
    payload = load_fixture("repository.json")
    payload.pop("created_at")
    payload.pop("updated_at")
    payload.pop("repository_id")

    request = RepositoryCreateRequest.model_validate(payload)

    assert request.canonical_name == "h5vision/vision"
    assert "repository_id" not in request.model_dump()


def test_repository_patch_requires_a_change() -> None:
    with pytest.raises(ValidationError):
        RepositoryUpdateRequest.model_validate({})


def test_repository_patch_rejects_blank_display_name() -> None:
    with pytest.raises(ValidationError):
        RepositoryUpdateRequest.model_validate({"display_name": "   "})


def test_branch_binding_contract_uses_full_ref_and_exact_ids() -> None:
    response = BranchBindingResponse.model_validate(load_fixture("branch_binding.json"))

    assert response.binding_id == UUID("11111111-1111-4111-8111-111111111111")
    assert response.frontend_workspace_name == "vision"
    assert response.branch_ref == "refs/heads/module"
    assert response.vss_project_id == "vss-server--module"


def test_branch_binding_create_rejects_short_branch_name() -> None:
    payload = load_fixture("branch_binding.json")
    for key in ("binding_id", "verified_at", "created_at", "updated_at"):
        payload.pop(key)
    payload["branch_ref"] = "feature/snapshot-ui"

    with pytest.raises(ValidationError):
        BranchBindingCreateRequest.model_validate(payload)


@pytest.mark.parametrize(
    "branch_ref",
    [
        "refs/heads/feature//snapshot",
        "refs/heads/feature/../snapshot",
        "refs/heads/feature snapshot",
        "refs/heads/release.lock",
    ],
)
def test_branch_binding_rejects_unsafe_full_ref(branch_ref: str) -> None:
    payload = load_fixture("branch_binding.json")
    for key in ("binding_id", "verified_at", "created_at", "updated_at"):
        payload.pop(key)
    payload["branch_ref"] = branch_ref

    with pytest.raises(ValidationError):
        BranchBindingCreateRequest.model_validate(payload)


def test_snapshot_list_keeps_repository_branch_and_reason() -> None:
    response = SnapshotListResponse.model_validate(load_fixture("snapshot_list.json"))

    snapshot = response.items[0]
    assert snapshot.branch_ref == "refs/heads/module"
    assert snapshot.state is SnapshotState.ACCEPTED
    assert snapshot.vss_reason == "VSS_INDEX_ACCEPTED"


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [
        (401, "ADMIN_AUTHENTICATION_REQUIRED"),
        (403, "ADMIN_PERMISSION_DENIED"),
        (409, "SNAPSHOT_DESTINATION_REQUIRED"),
    ],
)
def test_admin_errors_have_machine_and_human_readable_reasons(
    status_code: int, reason: str
) -> None:
    response = AdminErrorResponse.model_validate(
        {
            "ok": False,
            "reason": reason,
            "detail": f"Admin request failed with HTTP {status_code}.",
            "retryable": False,
            "request_id": "44444444-4444-4444-8444-444444444444",
            "status_code": status_code,
        }
    )

    assert response.reason == reason
    assert response.detail
    assert response.model_dump()["status_code"] == status_code


def test_admin_success_responses_explain_the_result() -> None:
    request_id = "44444444-4444-4444-8444-444444444444"
    mutation = AdminMutationResponse.model_validate(
        {
            "ok": True,
            "reason": "REPOSITORY_CREATED",
            "detail": "Repository를 등록했습니다.",
            "retryable": False,
            "request_id": request_id,
            "resource": {"repository_id": "55555555-5555-4555-8555-555555555555"},
        }
    )
    retry = SnapshotRetryResponse.model_validate(
        {
            "ok": True,
            "reason": "SNAPSHOT_RETRY_ACCEPTED",
            "detail": "같은 Snapshot에 새 VSS attempt를 생성했습니다.",
            "retryable": False,
            "request_id": request_id,
            "snapshot_id": "22222222-2222-4222-8222-222222222222",
            "state": "submitting",
            "attempt_count": 2,
        }
    )

    assert mutation.retryable is False
    assert retry.request_id == mutation.request_id
