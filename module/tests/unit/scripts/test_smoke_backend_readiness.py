"""배포 후 읽기 전용 smoke 도구의 판정 규칙을 검증한다."""

from __future__ import annotations

import pytest

from scripts import smoke_backend_readiness as smoke


def test_smoke_checks_readiness_and_exact_completion_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SNAPSHOT_BACKEND_BASE_URL", "http://backend.example:8000")
    monkeypatch.setenv("SNAPSHOT_TEST_PROJECT_ID", "vision")
    seen: list[tuple[str, dict[str, str] | None]] = []

    def fake_read_json(
        _base_url: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
    ) -> dict:
        seen.append((path, query))
        if path == "/v1/health":
            return {"ok": True}
        if path == "/v1/health/ready":
            return {"ok": True, "status": "ready"}
        return {
            "reason": "VSS_INDEX_COMPLETED",
            "detail": "target revision과 일치합니다.",
            "retryable": False,
            "snapshot_id": "00000000-0000-4000-8000-000000000000",
            "state": "completed",
            "target_revision": "1" * 40,
        }

    monkeypatch.setattr(smoke, "read_json", fake_read_json)
    smoke.main()

    assert seen == [
        ("/v1/health", None),
        ("/v1/health/ready", None),
        ("/v1/index/status", {"project_id": "vision"}),
    ]
    assert "[PASS] Snapshot status 계약 확인" in capsys.readouterr().out


def test_smoke_rejects_completed_state_without_exact_revision_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SNAPSHOT_BACKEND_BASE_URL", "http://backend.example:8000")
    monkeypatch.setenv("SNAPSHOT_TEST_PROJECT_ID", "vision")

    def fake_read_json(
        _base_url: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
    ) -> dict:
        del query
        if path == "/v1/health":
            return {"ok": True}
        if path == "/v1/health/ready":
            return {"ok": True, "status": "ready"}
        return {
            "reason": "VSS_INDEX_IN_PROGRESS",
            "detail": "잘못된 완료 상태입니다.",
            "retryable": False,
            "snapshot_id": "00000000-0000-4000-8000-000000000000",
            "state": "completed",
            "target_revision": "1" * 40,
        }

    monkeypatch.setattr(smoke, "read_json", fake_read_json)
    with pytest.raises(SystemExit) as captured:
        smoke.main()

    assert captured.value.code == 1
