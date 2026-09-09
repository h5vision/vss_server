"""완전한 revision tree를 확보하고 증명하는 읽기 전용 Git source."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from backend.features.materialization.errors import MaterializationError


class TreeSource(Protocol):
    """base tree를 준비하고 최종 정본 Git revision을 증명한다."""

    def populate(
        self,
        destination: Path,
        *,
        remote_url: str,
        branch_ref: str,
        base_revision: str,
        target_revision: str,
    ) -> None: ...

    def attest_target(self, project_root: Path, target_revision: str) -> None: ...

    def verify_target(self, project_root: Path, target_revision: str) -> None: ...


@dataclass(frozen=True, slots=True)
class GitTreeSource:
    command_timeout_seconds: float = 60.0

    def populate(
        self,
        destination: Path,
        *,
        remote_url: str,
        branch_ref: str,
        base_revision: str,
        target_revision: str,
    ) -> None:
        branch_name = branch_ref.removeprefix("refs/heads/")
        self._run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                "--single-branch",
                "--branch",
                branch_name,
                "--",
                remote_url,
                str(destination),
            ],
            failure=MaterializationError(
                reason="SNAPSHOT_SOURCE_UNAVAILABLE",
                detail="Repository의 전체 Git tree를 준비할 수 없습니다.",
                status_code=503,
                retryable=True,
            ),
        )
        self._require_commit(
            destination,
            base_revision,
            reason="SNAPSHOT_BASE_REVISION_UNAVAILABLE",
            detail="요청한 base revision을 Repository에서 찾을 수 없습니다.",
        )
        self._run(
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", base_revision],
            failure=MaterializationError(
                reason="SNAPSHOT_BASE_REVISION_UNAVAILABLE",
                detail="base revision 전체 tree를 checkout할 수 없습니다.",
                status_code=409,
                retryable=False,
            ),
        )
        self._require_commit(
            destination,
            target_revision,
            reason="VSS_REVISION_CONTRACT_UNSUPPORTED",
            detail=(
                "target revision Git object를 Repository에서 찾을 수 없습니다. "
                "현재 계약은 push되지 않은 로컬 commit을 보존할 수 없습니다."
            ),
        )

    def attest_target(self, project_root: Path, target_revision: str) -> None:
        self._run(
            ["git", "-C", str(project_root), "add", "--all"],
            failure=self._materialization_failure(),
        )
        actual_tree = self._output(
            ["git", "-C", str(project_root), "write-tree"],
            failure=self._materialization_failure(),
        )
        expected_tree = self._output(
            ["git", "-C", str(project_root), "rev-parse", f"{target_revision}^{{tree}}"],
            failure=MaterializationError(
                reason="VSS_REVISION_CONTRACT_UNSUPPORTED",
                detail="target revision tree를 검증할 수 없습니다.",
                status_code=409,
                retryable=False,
            ),
        )
        if actual_tree != expected_tree:
            raise MaterializationError(
                reason="SNAPSHOT_REVISION_MISMATCH",
                detail="적용된 전체 tree가 target revision의 Git tree와 일치하지 않습니다.",
                status_code=409,
                retryable=False,
            )

        self._run(
            ["git", "-C", str(project_root), "update-ref", "HEAD", target_revision],
            failure=self._materialization_failure(),
        )
        head = self._output(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            failure=self._materialization_failure(),
        )
        status = self._output(
            ["git", "-C", str(project_root), "status", "--porcelain=v1", "--untracked-files=all"],
            failure=self._materialization_failure(),
        )
        if head.lower() != target_revision.lower() or status:
            raise MaterializationError(
                reason="SNAPSHOT_REVISION_MISMATCH",
                detail="materialized Git HEAD 또는 working tree가 target revision과 다릅니다.",
                status_code=409,
                retryable=False,
            )

    def verify_target(self, project_root: Path, target_revision: str) -> None:
        # immutable 디렉터리도 운영 중 외부 변경 가능성을 배제할 수 없으므로 재시도 직전에
        # HEAD와 working tree를 다시 확인한다. 불일치는 새 commit으로 보정하지 않고 차단한다.
        head = self._output(
            ["git", "-C", str(project_root), "rev-parse", "HEAD"],
            failure=self._materialization_failure(),
        )
        status = self._output(
            ["git", "-C", str(project_root), "status", "--porcelain=v1", "--untracked-files=all"],
            failure=self._materialization_failure(),
        )
        if head.lower() != target_revision.lower() or status:
            raise MaterializationError(
                reason="SNAPSHOT_REVISION_MISMATCH",
                detail="재사용할 materialized tree가 target revision과 일치하지 않습니다.",
                status_code=409,
                retryable=False,
            )

    def _require_commit(self, root: Path, revision: str, *, reason: str, detail: str) -> None:
        self._run(
            ["git", "-C", str(root), "cat-file", "-e", f"{revision}^{{commit}}"],
            failure=MaterializationError(
                reason=reason,
                detail=detail,
                status_code=409,
                retryable=False,
            ),
        )

    def _output(self, command: list[str], *, failure: MaterializationError) -> str:
        return self._run(command, failure=failure).stdout.strip()

    def _run(
        self,
        command: list[str],
        *,
        failure: MaterializationError,
    ) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        try:
            return subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise failure from exc

    @staticmethod
    def _materialization_failure() -> MaterializationError:
        return MaterializationError(
            reason="SNAPSHOT_MATERIALIZATION_FAILED",
            detail="Git revision tree 검증 중 내부 오류가 발생했습니다.",
            status_code=500,
            retryable=True,
        )
