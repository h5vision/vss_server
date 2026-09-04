"""AWS runtime harness가 pull 소유권 계약을 검증하는지 확인한다."""

from pathlib import Path


def test_aws_harness_discovers_pull_capabilities_and_refs() -> None:
    script = (
        Path(__file__).parents[3] / "scripts" / "verify_aws_runtime.sh"
    ).read_text(encoding="utf-8")

    assert "/v1/internal/vss/capabilities" in script
    assert "/v1/internal/vss/refs" in script
    assert 'orchestration_mode="$(' in script
    assert 'if [[ "${orchestration_mode}" == "module_push" ]]' in script
    assert "pull mode source readiness" in script
