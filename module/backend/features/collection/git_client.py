"""Repository·Branch 수집을 위한 안전한 Git command 경계."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from backend.features.collection.errors import CollectionError


@dataclass(frozen=True, slots=True)
class GitCollectionClient:
    """원격 catalog 조회와 server-local bare mirror 유지를 담당한다.

    모든 오류 detail은 remote stderr·URL·mirror 경로를 포함하지 않는다. Git stderr에는
    credential이 포함된 remote URL이 노출될 수 있으므로 구조화된 reason만 상위로
    전달한다.
    """

    command_timeout_seconds: float = 60.0

    def remote_heads(self, remote_url: str) -> dict[str, str]:
        """원격 브랜치 catalog를 exact ref → 40자리 commit SHA로 반환한다."""
        output = self._run(
            ["git", "ls-remote", "--heads", "--", remote_url],
            failure=CollectionError(
                reason="COLLECTION_REMOTE_UNAVAILABLE",
                detail="원격 Repository의 브랜치 catalog를 조회하지 못했습니다.",
                retryable=True,
            ),
        ).stdout
        heads: dict[str, str] = {}
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            sha, _, ref = stripped.partition("\t")
            sha = sha.strip().lower()
            ref = ref.strip()
            if len(sha) != 40 or not ref.startswith("refs/heads/"):
                continue
            heads[ref] = sha
        return heads

    def ensure_mirror(self, remote_url: str, mirror_dir: Path) -> None:
        """bare mirror가 없으면 clone하고, 있으면 브랜치 ref를 fetch·prune한다."""
        if mirror_dir.exists():
            self._run(
                [
                    "git",
                    "-C",
                    str(mirror_dir),
                    "fetch",
                    "--prune",
                    "--quiet",
                    "origin",
                    "+refs/heads/*:refs/heads/*",
                ],
                failure=CollectionError(
                    reason="COLLECTION_MIRROR_UNAVAILABLE",
                    detail="server-local Git mirror를 갱신하지 못했습니다.",
                    retryable=True,
                ),
            )
            return
        mirror_dir.parent.mkdir(parents=True, exist_ok=True)
        self._run(
            [
                "git",
                "clone",
                "--mirror",
                "--quiet",
                "--",
                remote_url,
                str(mirror_dir),
            ],
            failure=CollectionError(
                reason="COLLECTION_MIRROR_UNAVAILABLE",
                detail="server-local Git mirror를 생성하지 못했습니다.",
                retryable=True,
            ),
        )

    def head_sha(self, mirror_dir: Path, branch_ref: str) -> str:
        """mirror에서 exact branch ref의 현재 commit SHA를 조회한다."""
        output = self._run(
            [
                "git",
                "-C",
                str(mirror_dir),
                "rev-parse",
                "--verify",
                "--end-of-options",
                f"{branch_ref}^{{commit}}",
            ],
            failure=CollectionError(
                reason="COLLECTION_BRANCH_UNAVAILABLE",
                detail="추적 Branch의 commit을 mirror에서 확인하지 못했습니다.",
                retryable=True,
            ),
        ).stdout.strip()
        if len(output) != 40:
            raise CollectionError(
                reason="COLLECTION_BRANCH_UNAVAILABLE",
                detail="추적 Branch의 commit SHA가 유효하지 않습니다.",
                retryable=True,
            )
        return output

    def is_ancestor(self, mirror_dir: Path, ancestor: str, descendant: str) -> bool:
        """ancestor commit이 descendant에 도달 가능한지 확인한다.

        merge-base 실패(객체 부재 등)는 분류를 왜곡하지 않도록 rewind로 처리한다. 이전
        HEAD 객체가 mirror에 없다는 것 자체가 이어진 히스토리가 아니라는 뜻이다.
        """
        completed = self._try_run(
            [
                "git",
                "-C",
                str(mirror_dir),
                "merge-base",
                "--is-ancestor",
                ancestor.lower(),
                descendant.lower(),
            ]
        )
        if completed is None or completed.returncode not in (0, 1):
            return False
        return completed.returncode == 0

    def checkout_tree(self, mirror_dir: Path, revision: str, destination: Path) -> None:
        """mirror의 exact commit을 destination에 완전한 working tree로 checkout한다."""
        self._run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-checkout",
                "--no-hardlinks",
                "--",
                str(mirror_dir),
                str(destination),
            ],
            failure=CollectionError(
                reason="SNAPSHOT_MATERIALIZATION_FAILED",
                detail="수집 commit의 Git tree를 준비하지 못했습니다.",
                retryable=True,
            ),
        )
        self._run(
            ["git", "-C", str(destination), "checkout", "--quiet", "--detach", revision.lower()],
            failure=CollectionError(
                reason="VSS_REVISION_CONTRACT_UNSUPPORTED",
                detail="수집한 commit을 Repository mirror에서 찾을 수 없습니다.",
                status_code=409,
                retryable=False,
            ),
        )

    def _run(self, command: list[str], *, failure: CollectionError) -> subprocess.CompletedProcess:
        completed = self._try_run(command)
        if completed is None or completed.returncode != 0:
            raise failure
        return completed

    def _try_run(self, command: list[str]) -> subprocess.CompletedProcess | None:
        environment = os.environ.copy()
        # 대화형 credential prompt와 사용자 git config를 차단해 수집 경로가 임의
        # 인증·설정에 의존하지 않도록 한다.
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GCM_INTERACTIVE"] = "never"
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        try:
            return subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.command_timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            return None
