from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from backend.features.admin.service_restart import (
    ServiceRestartScheduleError,
    ServiceRestartScheduler,
)


def test_service_restart_scheduler_writes_only_fixed_marker(tmp_path) -> None:
    trigger_dir = tmp_path / "ops"
    trigger_dir.mkdir()
    (trigger_dir / "restart-status.json").write_text("{}", encoding="utf-8")
    scheduler = ServiceRestartScheduler(trigger_dir)
    request_id = uuid4()

    scheduled = scheduler.schedule(
        "module_stack",
        request_id=request_id,
        actor="alice",
    )

    assert scheduled.created is True
    assert scheduled.already_scheduled is False
    assert scheduled.services == ("vss-snapshot.service", "vss-admin-web.service")
    assert scheduled.trigger_path.name == "restart-module-stack.request"
    payload = json.loads(scheduled.trigger_path.read_text(encoding="utf-8"))
    assert payload["scope"] == "module_stack"
    assert payload["request_id"] == str(request_id)
    assert payload["actor"] == "alice"
    assert set(trigger_dir.iterdir()) == {
        scheduled.trigger_path,
        trigger_dir / "restart-status.json",
    }


def test_same_restart_scope_is_idempotent(tmp_path) -> None:
    trigger_dir = tmp_path / "ops"
    trigger_dir.mkdir()
    (trigger_dir / "restart-status.json").write_text("{}", encoding="utf-8")
    scheduler = ServiceRestartScheduler(trigger_dir)

    first = scheduler.schedule("admin_web", request_id=uuid4(), actor="alice")
    second = scheduler.schedule("admin_web", request_id=uuid4(), actor="alice")

    assert first.created is True
    assert second.created is False
    assert second.already_scheduled is True
    assert second.trigger_path == first.trigger_path


def test_overlapping_restart_scope_is_rejected(tmp_path) -> None:
    trigger_dir = tmp_path / "ops"
    trigger_dir.mkdir()
    (trigger_dir / "restart-status.json").write_text("{}", encoding="utf-8")
    scheduler = ServiceRestartScheduler(trigger_dir)
    scheduler.schedule("snapshot_backend", request_id=uuid4(), actor="alice")

    with pytest.raises(ServiceRestartScheduleError) as captured:
        scheduler.schedule("module_stack", request_id=uuid4(), actor="alice")

    assert captured.value.reason == "MODULE_SERVICE_RESTART_ALREADY_SCHEDULED"
    assert captured.value.retryable is True


def test_in_progress_restart_is_rejected(tmp_path) -> None:
    trigger_dir = tmp_path / "ops"
    trigger_dir.mkdir()
    (trigger_dir / "restart-status.json").write_text(
        json.dumps({"state": "running", "scope": "snapshot_backend"}),
        encoding="utf-8",
    )
    scheduler = ServiceRestartScheduler(trigger_dir)

    with pytest.raises(ServiceRestartScheduleError) as captured:
        scheduler.schedule("snapshot_backend", request_id=uuid4(), actor="alice")

    assert captured.value.reason == "MODULE_SERVICE_RESTART_IN_PROGRESS"
    assert captured.value.retryable is True


def test_missing_restart_channel_is_not_configured(tmp_path) -> None:
    scheduler = ServiceRestartScheduler(tmp_path / "missing")

    assert scheduler.ready() is False
    with pytest.raises(ServiceRestartScheduleError) as captured:
        scheduler.schedule("snapshot_backend", request_id=uuid4(), actor="alice")

    assert captured.value.reason == "MODULE_SERVICE_RESTART_NOT_CONFIGURED"
    assert captured.value.retryable is False


def test_cancel_removes_only_marker_created_by_schedule(tmp_path) -> None:
    trigger_dir = tmp_path / "ops"
    trigger_dir.mkdir()
    (trigger_dir / "restart-status.json").write_text("{}", encoding="utf-8")
    scheduler = ServiceRestartScheduler(trigger_dir)
    schedule = scheduler.schedule("snapshot_backend", request_id=uuid4(), actor="alice")

    scheduler.cancel(schedule)

    assert not schedule.trigger_path.exists()


def test_execution_status_reads_root_controller_result(tmp_path) -> None:
    trigger_dir = tmp_path / "ops"
    trigger_dir.mkdir()
    (trigger_dir / "restart-status.json").write_text("{}", encoding="utf-8")
    request_id = uuid4()
    (trigger_dir / "restart-status.json").write_text(
        json.dumps(
            {
                "state": "succeeded",
                "scope": "module_stack",
                "services": ["ignored-by-reader"],
                "request_id": str(request_id),
                "actor": "alice",
                "scheduled_at": "2026-09-11T12:00:00+00:00",
                "started_at": "2026-09-11T12:00:04Z",
                "completed_at": "2026-09-11T12:00:08Z",
                "git_head": "a" * 40,
                "detail": "Restart completed and health checks passed.",
            }
        ),
        encoding="utf-8",
    )
    scheduler = ServiceRestartScheduler(trigger_dir)

    execution = scheduler.execution_status()

    assert execution is not None
    assert execution.state == "succeeded"
    assert execution.scope == "module_stack"
    assert execution.services == ("vss-snapshot.service", "vss-admin-web.service")
    assert execution.request_id == str(request_id)
    assert execution.git_head == "a" * 40
    assert scheduler.in_progress() is False
    assert scheduler.pending_scope() is None


def test_root_controller_contains_only_fixed_systemd_restart_targets() -> None:
    module_root = Path(__file__).resolve().parents[3]
    controller = (
        module_root / "ops" / "service_restart" / "restart_module_services.sh"
    ).read_text(encoding="utf-8")
    tmpfiles = (
        module_root / "ops" / "service_restart" / "vss-module-ops.tmpfiles.conf"
    ).read_text(encoding="utf-8")

    assert "systemctl restart vss-snapshot.service" in controller
    assert "systemctl restart vss-admin-web.service" in controller
    assert 'systemctl restart "$' not in controller
    assert "restart-snapshot-backend.request" in controller
    assert "restart-admin-web.request" in controller
    assert "restart-module-stack.request" in controller
    assert "restart-status.json" in controller
    assert "Restart completed and health checks passed." in controller
    assert "wait_http http://127.0.0.1:8000/v1/health" in controller
    assert "wait_http http://127.0.0.1:4180/" in controller
    assert "O_NOFOLLOW" in controller
    assert "restart.in-progress" not in controller
    assert "d /run/vss-ops 1730 root @SNAPSHOT_GROUP@" in tmpfiles
    assert "restart-status.json 0640 root @SNAPSHOT_GROUP@" in tmpfiles
    assert "restart.lock 0600 root root" in tmpfiles
