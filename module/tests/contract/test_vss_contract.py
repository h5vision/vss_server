from __future__ import annotations

import json
from pathlib import Path

from backend.integrations.vss.schemas import (
    VssIndexCommand,
    VssIndexState,
    VssIndexStatus,
    VssStartIndexResult,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "vss"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text("utf-8"))


def test_start_accepted_matches_vss_module_result() -> None:
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


def test_command_exports_only_current_start_index_arguments() -> None:
    command = VssIndexCommand(
        project_root="/srv/snapshots/vss-server--module/revision",
        project_id="vss-server--module",
        expected_revision="2" * 40,
        snapshot_id="snapshot-id",
    )

    kwargs = command.start_index_kwargs()

    assert kwargs["blocking"] is False
    assert kwargs["extra_meta"]["requested_revision"] == "2" * 40
    assert "expected_revision" not in kwargs
