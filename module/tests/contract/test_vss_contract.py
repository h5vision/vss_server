from __future__ import annotations

import json
from pathlib import Path

from backend.integrations.vss.schemas import (
    VssIndexRequest,
    VssIndexState,
    VssIndexStatus,
    VssProjectsResponse,
    VssStartIndexResponse,
    VssStartIndexResult,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "vss"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def test_start_accepted_matches_vss_http_result() -> None:
    result = VssStartIndexResult.model_validate(load_fixture("start_accepted.json"))

    assert result.accepted is True
    assert result.state is VssIndexState.RUNNING
    assert result.fingerprint is not None


def test_already_running_preserves_vss_reason() -> None:
    result = VssStartIndexResult.model_validate(load_fixture("already_running.json"))

    assert result.accepted is False
    assert result.reason == "already_running"
    assert result.heartbeat_age_s == 1.4


def test_done_requires_exact_expected_revision() -> None:
    status = VssIndexStatus.model_validate(load_fixture("status_done.json"))

    assert status.completed_for("2" * 40)
    assert not status.completed_for("1" * 40)


def test_failed_status_preserves_error_and_incomplete_builds() -> None:
    status = VssIndexStatus.model_validate(load_fixture("status_failed.json"))

    assert status.state is VssIndexState.FAILED
    assert status.error == "RuntimeError: embedding failed"
    assert status.incomplete[0]["status"] == "failed"


def test_index_request_exports_only_supported_http_fields() -> None:
    request = VssIndexRequest(
        project_root="/srv/snapshots/vss-server--module/revision",
        project_id="vss-server--module",
        note="snapshot baseline",
    )

    body = request.model_dump(exclude_none=True)

    assert body == {
        "project_root": "/srv/snapshots/vss-server--module/revision",
        "project_id": "vss-server--module",
        "force": False,
        "briefing": True,
        "note": "snapshot baseline",
    }
    assert "expected_revision" not in body
    assert "snapshot_id" not in body


def test_start_response_preserves_accepted_http_status() -> None:
    response = VssStartIndexResponse(
        status_code=202,
        result=VssStartIndexResult.model_validate(load_fixture("start_accepted.json")),
    )

    assert response.status_code == 202
    assert response.result.accepted is True


def test_projects_response_uses_vss_wrapper_shape() -> None:
    response = VssProjectsResponse.model_validate(
        {
            "projects": [
                {
                    "project_id": "vss-server--module",
                    "chunks": 83,
                    "commit": "2" * 40,
                    "note": "snapshot baseline",
                }
            ],
            "incomplete": [],
        }
    )

    assert response.projects[0].project_id == "vss-server--module"
    assert response.projects[0].note == "snapshot baseline"
