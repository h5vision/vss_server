"""Git adapter for comparing two revisions (diff stats and changed files)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from uuid import UUID

from backend.features.repository_collection.errors import CollectionError
from backend.infrastructure.git.layout import GitCacheLayout
from backend.infrastructure.git.runner import GitCommandRunner, is_sha
from backend.ports.git import (
    GitCompareFileChange,
    GitCompareResult,
    RevisionComparator,
)


@dataclass(frozen=True, slots=True)
class GitRevisionCompareAdapter(RevisionComparator):
    """Adapter implementing RevisionComparator using Git CLI diff-tree and rev-list."""

    layout: GitCacheLayout
    runner: GitCommandRunner = field(default_factory=GitCommandRunner)

    def compare_revisions(
        self,
        *,
        repository_id: UUID,
        base_revision: str,
        target_revision: str,
        max_changes: int = 10_000,
    ) -> GitCompareResult:
        base = base_revision.strip().lower()
        target = target_revision.strip().lower()

        if not is_sha(base) or not is_sha(target):
            raise CollectionError(
                reason="COMPARE_REVISION_INVALID",
                detail="비교 대상 revision이 유효한 Git SHA가 아닙니다.",
                retryable=False,
                status_code=400,
            )

        cache = self.layout.cache_path(repository_id)
        if not cache.is_dir():
            raise CollectionError(
                reason="REPOSITORY_CACHE_UNAVAILABLE",
                detail="비교를 수행할 Repository Git cache가 없습니다.",
                retryable=True,
                status_code=503,
            )

        # Check existence of revisions
        for rev in (base, target):
            self.runner.run(
                ["git", "-C", str(cache), "cat-file", "-e", f"{rev}^{{commit}}"],
                failure=CollectionError(
                    reason="COMPARE_REVISION_NOT_FOUND",
                    detail=f"비교 대상 revision({rev[:8]})이 Git cache에 존재하지 않습니다.",
                    retryable=False,
                    status_code=404,
                ),
                allowed_returncodes={0},
            )

        if base == target:
            return GitCompareResult(
                base_revision=base,
                target_revision=target,
                merge_base_revision=base,
                ahead_count=0,
                behind_count=0,
                files_changed=0,
                additions=0,
                deletions=0,
                changes=[],
            )

        # Merge base
        mb_res = self.runner.run(
            ["git", "-C", str(cache), "merge-base", base, target],
            failure=CollectionError(
                reason="COMPARE_GIT_FAILED",
                detail="Git 공통 조상(merge-base)을 계산하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
            allowed_returncodes={0, 1},
        )
        merge_base = (
            mb_res.stdout.strip().lower()
            if mb_res.returncode == 0 and mb_res.stdout.strip()
            else None
        )

        # Ahead / Behind count
        ahead_res = self.runner.run(
            ["git", "-C", str(cache), "rev-list", "--count", f"{base}..{target}"],
            failure=CollectionError(
                reason="COMPARE_GIT_FAILED",
                detail="Git ahead 커밋 수를 계산하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        ahead_count = int(ahead_res.stdout.strip()) if ahead_res.stdout.strip().isdigit() else 0

        behind_res = self.runner.run(
            ["git", "-C", str(cache), "rev-list", "--count", f"{target}..{base}"],
            failure=CollectionError(
                reason="COMPARE_GIT_FAILED",
                detail="Git behind 커밋 수를 계산하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        behind_count = int(behind_res.stdout.strip()) if behind_res.stdout.strip().isdigit() else 0

        # Diff shortstat
        stat_res = self.runner.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-C",
                str(cache),
                "diff",
                "--shortstat",
                base,
                target,
            ],
            failure=CollectionError(
                reason="COMPARE_GIT_FAILED",
                detail="Git diff 통계를 조회하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )
        stat_out = stat_res.stdout.strip()
        files_match = re.search(r"(\d+)\s+file", stat_out)
        files_changed = int(files_match.group(1)) if files_match else 0
        ins_match = re.search(r"(\d+)\s+insertion", stat_out)
        additions = int(ins_match.group(1)) if ins_match else 0
        del_match = re.search(r"(\d+)\s+deletion", stat_out)
        deletions = int(del_match.group(1)) if del_match else 0

        # Diff name-status with renames (-M)
        diff_res = self.runner.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-C",
                str(cache),
                "diff",
                "--name-status",
                "-M",
                base,
                target,
            ],
            failure=CollectionError(
                reason="COMPARE_GIT_FAILED",
                detail="Git diff 변경 파일 목록을 조회하지 못했습니다.",
                retryable=True,
                status_code=503,
            ),
        )

        changes: list[GitCompareFileChange] = []
        for line in diff_res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if not parts:
                continue
            code = parts[0][0]
            if code == "A" and len(parts) >= 2:
                path = parts[1].replace("\\", "/")
                old_path = None
                change_type = "added"
            elif code in {"M", "T"} and len(parts) >= 2:
                path = parts[1].replace("\\", "/")
                old_path = None
                change_type = "modified"
            elif code == "D" and len(parts) >= 2:
                path = parts[1].replace("\\", "/")
                old_path = None
                change_type = "deleted"
            elif code == "R" and len(parts) >= 3:
                old_path = parts[1].replace("\\", "/")
                path = parts[2].replace("\\", "/")
                change_type = "renamed"
            elif code == "C" and len(parts) >= 3:
                old_path = parts[1].replace("\\", "/")
                path = parts[2].replace("\\", "/")
                change_type = "copied"
            else:
                path = (parts[1] if len(parts) >= 2 else parts[0]).replace("\\", "/")
                old_path = None
                change_type = "modified"

            self._validate_safe_diff_path(path)
            if old_path:
                self._validate_safe_diff_path(old_path)

            changes.append(
                GitCompareFileChange(
                    path=path,
                    change_type=change_type,
                    old_path=old_path,
                )
            )
            if len(changes) > max_changes:
                raise CollectionError(
                    reason="COMPARE_CHANGES_LIMIT_EXCEEDED",
                    detail=f"비교 변경 파일 수가 최대 허용 개수({max_changes})를 초과했습니다.",
                    retryable=False,
                    status_code=409,
                )

        sorted_changes = sorted(changes, key=lambda item: item.path)

        return GitCompareResult(
            base_revision=base,
            target_revision=target,
            merge_base_revision=merge_base,
            ahead_count=ahead_count,
            behind_count=behind_count,
            files_changed=files_changed,
            additions=additions,
            deletions=deletions,
            changes=sorted_changes,
        )

    @staticmethod
    def _validate_safe_diff_path(path: str) -> None:
        normalized = path.replace("\\", "/").strip()
        if not normalized:
            raise CollectionError(
                reason="COMPARE_PATH_INVALID",
                detail="Diff 파일 경로가 비어 있습니다.",
                retryable=False,
                status_code=400,
            )
        parts = normalized.split("/")
        if ".." in parts or any(p == "." for p in parts) or normalized.startswith("/"):
            raise CollectionError(
                reason="COMPARE_PATH_TRAVERSAL",
                detail=f"안전하지 않은 Diff 파일 경로가 감지되었습니다: {path}",
                retryable=False,
                status_code=400,
            )
