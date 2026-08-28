"""DB Snapshot과 immutable Git tree를 VSS용 검증 descriptor로 변환한다."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.snapshots.store import SnapshotStore
from backend.features.vss_sources.schemas import (
    GitSourceVerification,
    VssRevisionItem,
    VssRevisionListResponse,
    VssSourceDescriptorResponse,
)
from backend.infrastructure.database.models import Repository
from backend.integrations.vss.schemas import VssIndexRequest


class VssSourceService:
    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker[AsyncSession],
        materializer: SnapshotMaterializer,
        git_timeout_seconds: float,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._materializer = materializer
        self._git_timeout_seconds = git_timeout_seconds

    async def describe(
        self,
        project_id: str,
        *,
        revision: str | None,
        request_id: UUID,
    ) -> VssSourceDescriptorResponse:
        normalized_project_id = project_id.strip()
        normalized_revision = revision.lower() if revision is not None else None
        async with self._sessionmaker() as session:
            try:
                snapshot = await SnapshotStore(session).source_for_vss_project(
                    normalized_project_id,
                    revision=normalized_revision,
                )
                if snapshot is None:
                    raise ApiError(
                        status_code=404,
                        reason="VSS_SOURCE_NOT_FOUND",
                        detail="요청한 VSS project와 revision에 게시 가능한 Snapshot이 없습니다.",
                        retryable=False,
                    )
                repository = await session.get(Repository, snapshot.repository_id)
            except ApiError:
                raise
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc

            if repository is None or not repository.active:
                raise ApiError(
                    status_code=409,
                    reason="VSS_SOURCE_REPOSITORY_INACTIVE",
                    detail="Snapshot의 Repository가 없거나 비활성 상태입니다.",
                    retryable=False,
                )
            if snapshot.materialized_locator is None:
                raise ApiError(
                    status_code=409,
                    reason="VSS_SOURCE_NOT_MATERIALIZED",
                    detail="Snapshot의 immutable revision 디렉터리가 준비되지 않았습니다.",
                    retryable=True,
                )

            try:
                tree = await run_in_threadpool(
                    self._materializer.verify_existing,
                    snapshot.materialized_locator,
                    snapshot.target_revision,
                )
                verification = await run_in_threadpool(
                    self._read_git_verification,
                    tree.project_root,
                    snapshot.target_revision,
                )
            except MaterializationError as exc:
                raise ApiError(
                    status_code=409,
                    reason=exc.reason,
                    detail="게시할 Snapshot의 Git 정합성 검증에 실패했습니다.",
                    retryable=exc.retryable,
                ) from exc

            index_request = VssIndexRequest(
                project_root=str(tree.project_root),
                project_id=snapshot.vss_project_id,
                force=False,
                briefing=True,
                note=f"snapshot {snapshot.target_revision}",
            )
            return VssSourceDescriptorResponse(
                detail="VSS가 독립 검증 후 인덱싱할 수 있는 Snapshot 소스입니다.",
                request_id=request_id,
                project_id=snapshot.vss_project_id,
                repository_id=repository.repository_id,
                repository_name=repository.canonical_name,
                branch_ref=snapshot.branch_ref,
                snapshot_id=snapshot.snapshot_id,
                snapshot_state=snapshot.state,
                source_type=snapshot.source_type,
                base_revision=snapshot.base_revision,
                target_revision=snapshot.target_revision,
                verification=verification,
                index_request=index_request,
            )

    async def revisions(
        self,
        project_id: str,
        *,
        limit: int,
        request_id: UUID,
    ) -> VssRevisionListResponse:
        normalized_project_id = project_id.strip()
        async with self._sessionmaker() as session:
            try:
                snapshots = await SnapshotStore(session).revisions_for_vss_project(
                    normalized_project_id,
                    limit=limit,
                )
            except SQLAlchemyError as exc:
                raise self._database_unavailable() from exc
        return VssRevisionListResponse(
            detail="VSS project에 연결된 Snapshot revision 이력입니다.",
            request_id=request_id,
            project_id=normalized_project_id,
            items=[
                VssRevisionItem(
                    snapshot_id=item.snapshot_id,
                    repository_id=item.repository_id,
                    branch_ref=item.branch_ref,
                    base_revision=item.base_revision,
                    target_revision=item.target_revision,
                    snapshot_state=item.state,
                    materialized=item.materialized_locator is not None,
                    vss_state=item.vss_state,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in snapshots
            ],
        )

    def _read_git_verification(
        self,
        project_root: Path,
        expected_revision: str,
    ) -> GitSourceVerification:
        commit = self._git_output(project_root, "rev-parse", "HEAD")
        tree = self._git_output(project_root, "rev-parse", "HEAD^{tree}")
        object_format = self._git_output(project_root, "rev-parse", "--show-object-format")
        status = self._git_output(
            project_root,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        )
        if commit.lower() != expected_revision.lower() or status or object_format != "sha1":
            raise MaterializationError(
                reason="SNAPSHOT_REVISION_MISMATCH",
                detail="immutable Git tree의 commit, object format 또는 working tree가 다릅니다.",
                status_code=409,
                retryable=False,
            )
        return GitSourceVerification(
            expected_commit_sha=commit.lower(),
            expected_tree_sha=tree.lower(),
            object_format="sha1",
            git_metadata_present=True,
            working_tree_clean=True,
            verified_at=datetime.now(UTC),
            verification_commands=[
                "git rev-parse HEAD",
                "git rev-parse HEAD^{tree}",
                "git status --porcelain=v1 --untracked-files=all",
            ],
        )

    def _git_output(self, project_root: Path, *arguments: str) -> str:
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        try:
            result = subprocess.run(
                ["git", "-C", str(project_root), *arguments],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._git_timeout_seconds,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MaterializationError(
                reason="SNAPSHOT_MATERIALIZATION_FAILED",
                detail="immutable Git tree의 검증 값을 읽지 못했습니다.",
                status_code=500,
                retryable=True,
            ) from exc
        return result.stdout.strip()

    @staticmethod
    def _database_unavailable() -> ApiError:
        return ApiError(
            status_code=503,
            reason="DATABASE_UNAVAILABLE",
            detail="Snapshot 데이터베이스를 사용할 수 없습니다.",
            retryable=True,
        )
