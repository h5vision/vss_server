"""Admin-only queries over collection, Snapshot, and audit history."""

from __future__ import annotations

import base64
from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from backend.core.errors import ApiError
from backend.features.admin.schemas import (
    AdminCommitAssociatedRef,
    AdminCommitListItem,
    AdminCommitMaterializeResponse,
    AdminCommitStatus,
)
from backend.infrastructure.database.models import (
    AuditLog,
    BranchHeadHistory,
    ChangeRequest,
    Repository,
    RepositoryCommit,
    RepositorySyncRun,
    RepositoryTag,
    Snapshot,
    TrackedBranch,
)

if TYPE_CHECKING:
    from backend.features.repository_collection.git_client import RepositoryGitClient
    from backend.features.repository_collection.materializer import CollectedRevisionMaterializer


def _compute_commit_status(
    snapshot: Snapshot | None,
) -> tuple[AdminCommitStatus, bool, str | None]:
    if snapshot is None:
        return "git_only", False, None
    if snapshot.state == "completed" and snapshot.vss_state == "done":
        return "vss_indexed", True, None
    if snapshot.state in ("materialized", "accepted", "indexing") or snapshot.materialized_locator:
        return "materialized", False, None
    if snapshot.state == "failed" or snapshot.vss_state == "failed":
        reason = snapshot.vss_reason or snapshot.vss_detail or "스냅샷 처리 실패"
        return "unavailable", False, reason
    return "git_only", False, None


class AdminStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_tracked_branches(
        self,
        *,
        repository_id: UUID | None = None,
        tracked: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[TrackedBranch]:
        statement = select(TrackedBranch)
        if repository_id is not None:
            statement = statement.where(TrackedBranch.repository_id == repository_id)
        if tracked is not None:
            statement = statement.where(TrackedBranch.tracked.is_(tracked))
        statement = statement.order_by(
            TrackedBranch.repository_id,
            TrackedBranch.branch_ref,
            TrackedBranch.tracked_branch_id,
        ).offset(offset).limit(limit)
        return list(await self._session.scalars(statement))

    async def list_sync_runs(
        self,
        *,
        repository_id: UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[RepositorySyncRun]:
        statement = select(RepositorySyncRun)
        if repository_id is not None:
            statement = statement.where(RepositorySyncRun.repository_id == repository_id)
        statement = statement.order_by(
            RepositorySyncRun.started_at.desc(),
            RepositorySyncRun.sync_run_id.desc(),
        ).offset(offset).limit(limit)
        return list(await self._session.scalars(statement))

    async def list_head_history(
        self,
        tracked_branch_id: UUID,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[BranchHeadHistory]:
        statement = (
            select(BranchHeadHistory)
            .where(BranchHeadHistory.tracked_branch_id == tracked_branch_id)
            .order_by(
                BranchHeadHistory.observed_at.desc(),
                BranchHeadHistory.history_id.desc(),
            )
            .offset(offset)
            .limit(limit)
        )
        return list(await self._session.scalars(statement))

    async def list_snapshots(
        self,
        *,
        repository_id: UUID | None = None,
        tracked_branch_id: UUID | None = None,
        state: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Snapshot]:
        statement = select(Snapshot)
        if repository_id is not None:
            statement = statement.where(Snapshot.repository_id == repository_id)
        if tracked_branch_id is not None:
            statement = statement.where(Snapshot.tracked_branch_id == tracked_branch_id)
        if state is not None:
            statement = statement.where(Snapshot.state == state)
        statement = statement.order_by(
            Snapshot.created_at.desc(),
            Snapshot.snapshot_id.desc(),
        ).offset(offset).limit(limit)
        return list(await self._session.scalars(statement))

    async def get_snapshot_detail(self, snapshot_id: UUID) -> Snapshot | None:
        statement = (
            select(Snapshot)
            .where(Snapshot.snapshot_id == snapshot_id)
            .options(selectinload(Snapshot.attempts), selectinload(Snapshot.deltas))
        )
        return await self._session.scalar(statement)

    async def list_audit_logs(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        statement = select(AuditLog).order_by(
            AuditLog.created_at.desc(),
            AuditLog.audit_id.desc(),
        ).offset(offset).limit(limit)
        return list(await self._session.scalars(statement))

    async def list_repository_commits(
        self,
        repository_id: UUID,
        *,
        limit: int = 50,
        cursor: str | None = None,
        status: str | None = None,
        branch_ref: str | None = None,
        tag_ref: str | None = None,
        change_request: str | None = None,
    ) -> tuple[list[AdminCommitListItem], str | None, int]:
        stmt = (
            select(RepositoryCommit)
            .where(RepositoryCommit.repository_id == repository_id)
            .options(selectinload(RepositoryCommit.parents))
        )
        if cursor:
            try:
                decoded = base64.urlsafe_b64decode(cursor.encode()).decode("utf-8")
                ts_str, sha = decoded.split("|", 1)
                cur_dt = datetime.fromisoformat(ts_str)
                stmt = stmt.where(
                    or_(
                        RepositoryCommit.committed_at < cur_dt,
                        (RepositoryCommit.committed_at == cur_dt)
                        & (RepositoryCommit.commit_sha < sha),
                    )
                )
            except Exception:
                pass

        stmt = stmt.order_by(
            RepositoryCommit.committed_at.desc(),
            RepositoryCommit.commit_sha.desc(),
        ).limit(limit + 1)

        commits = list(await self._session.scalars(stmt))
        has_next = len(commits) > limit
        if has_next:
            commits = commits[:limit]

        shas = [c.commit_sha for c in commits]
        if not shas:
            return [], None, 0

        # Batch load Snapshots for these shas
        snap_stmt = select(Snapshot).where(
            Snapshot.repository_id == repository_id,
            Snapshot.target_revision.in_(shas),
        ).order_by(Snapshot.created_at.desc())
        snapshots_list = list(await self._session.scalars(snap_stmt))
        snapshot_by_sha: dict[str, Snapshot] = {}
        for s in snapshots_list:
            if s.target_revision not in snapshot_by_sha:
                snapshot_by_sha[s.target_revision] = s

        # Batch load TrackedBranches
        tb_stmt = select(TrackedBranch).where(TrackedBranch.repository_id == repository_id)
        branches = list(await self._session.scalars(tb_stmt))
        branch_map = {b.tracked_branch_id: b for b in branches}

        # Tracked branch current head mapping
        history_branches_by_sha: dict[str, list[TrackedBranch]] = {}
        for b in branches:
            if b.current_head_sha and b.current_head_sha in shas:
                history_branches_by_sha.setdefault(b.current_head_sha, []).append(b)

        # Batch load Head histories for observed commits
        hist_stmt = (
            select(BranchHeadHistory)
            .where(BranchHeadHistory.observed_head_sha.in_(shas))
            .order_by(BranchHeadHistory.observed_at.desc())
        )
        histories = list(await self._session.scalars(hist_stmt))
        for h in histories:
            if h.observed_head_sha and h.tracked_branch_id in branch_map:
                tb = branch_map[h.tracked_branch_id]
                history_branches_by_sha.setdefault(h.observed_head_sha, []).append(tb)

        # Batch load Tags
        tag_stmt = select(RepositoryTag).where(
            RepositoryTag.repository_id == repository_id,
            RepositoryTag.current_commit_sha.in_(shas),
        )
        tags = list(await self._session.scalars(tag_stmt))
        tags_by_sha: dict[str, list[RepositoryTag]] = {}
        for t in tags:
            tags_by_sha.setdefault(t.current_commit_sha, []).append(t)

        # Batch load ChangeRequests
        cr_stmt = select(ChangeRequest).where(
            ChangeRequest.repository_id == repository_id,
            or_(
                ChangeRequest.current_head_sha.in_(shas),
                ChangeRequest.current_merge_sha.in_(shas),
                ChangeRequest.current_base_sha.in_(shas),
            ),
        )
        crs = list(await self._session.scalars(cr_stmt))
        crs_by_sha: dict[str, list[ChangeRequest]] = {}
        for cr in crs:
            for rev in (
                cr.current_head_sha,
                cr.current_merge_sha,
                cr.current_base_sha,
            ):
                if rev and rev in shas:
                    crs_by_sha.setdefault(rev, []).append(cr)

        items: list[AdminCommitListItem] = []
        for c in commits:
            snap = snapshot_by_sha.get(c.commit_sha)
            c_status, eligible, unavail_reason = _compute_commit_status(snap)

            # Build associated refs
            associated_refs: list[AdminCommitAssociatedRef] = []
            seen_ref_keys: set[str] = set()
            for b in history_branches_by_sha.get(c.commit_sha, []):
                k = f"branch:{b.branch_ref}"
                if k not in seen_ref_keys:
                    seen_ref_keys.add(k)
                    associated_refs.append(
                        AdminCommitAssociatedRef(
                            ref_type="branch",
                            name=b.branch_ref,
                            detail=b.vss_project_id,
                        )
                    )
            for t in tags_by_sha.get(c.commit_sha, []):
                k = f"tag:{t.tag_name}"
                if k not in seen_ref_keys:
                    seen_ref_keys.add(k)
                    associated_refs.append(
                        AdminCommitAssociatedRef(
                            ref_type="tag",
                            name=t.tag_name,
                            detail=t.target_object_sha,
                        )
                    )
            for cr in crs_by_sha.get(c.commit_sha, []):
                k = f"cr:{cr.provider}:{cr.external_number}"
                if k not in seen_ref_keys:
                    seen_ref_keys.add(k)
                    associated_refs.append(
                        AdminCommitAssociatedRef(
                            ref_type="change_request",
                            name=f"{cr.provider}#{cr.external_number}",
                            detail=cr.title,
                        )
                    )

            if status is not None and c_status != status:
                continue
            if branch_ref is not None and not any(
                r.ref_type == "branch" and r.name == branch_ref for r in associated_refs
            ):
                continue
            if tag_ref is not None and not any(
                r.ref_type == "tag" and r.name == tag_ref for r in associated_refs
            ):
                continue
            if change_request is not None and not any(
                r.ref_type == "change_request" and r.name == change_request
                for r in associated_refs
            ):
                continue

            parent_shas = [
                p.parent_sha for p in sorted(c.parents, key=lambda p: p.parent_order)
            ]
            items.append(
                AdminCommitListItem(
                    commit_sha=c.commit_sha,
                    tree_sha=c.tree_sha,
                    author_name=c.author_name,
                    authored_at=c.authored_at,
                    committed_at=c.committed_at,
                    subject=c.subject,
                    parent_shas=parent_shas,
                    status=c_status,
                    snapshot_id=snap.snapshot_id if snap else None,
                    snapshot_state=snap.state if snap else None,
                    vss_state=snap.vss_state if snap else None,
                    eligible_for_answer=eligible,
                    unavailable_reason=unavail_reason,
                    associated_refs=associated_refs,
                )
            )

        next_cursor = None
        if has_next and commits:
            last = commits[-1]
            raw = f"{last.committed_at.isoformat()}|{last.commit_sha}"
            next_cursor = base64.urlsafe_b64encode(raw.encode()).decode("utf-8")

        return items, next_cursor, len(items)

    async def get_repository_commit(
        self,
        repository_id: UUID,
        commit_sha: str,
    ) -> AdminCommitListItem | None:
        stmt = (
            select(RepositoryCommit)
            .where(
                RepositoryCommit.repository_id == repository_id,
                RepositoryCommit.commit_sha == commit_sha,
            )
            .options(selectinload(RepositoryCommit.parents))
        )
        c = await self._session.scalar(stmt)
        if c is None:
            return None

        # Snapshot
        snap_stmt = (
            select(Snapshot)
            .where(
                Snapshot.repository_id == repository_id,
                Snapshot.target_revision == commit_sha,
            )
            .order_by(Snapshot.created_at.desc())
        )
        snap = await self._session.scalar(snap_stmt)
        c_status, eligible, unavail_reason = _compute_commit_status(snap)

        # Tags
        tag_stmt = select(RepositoryTag).where(
            RepositoryTag.repository_id == repository_id,
            RepositoryTag.current_commit_sha == commit_sha,
        )
        tags = list(await self._session.scalars(tag_stmt))

        associated_refs: list[AdminCommitAssociatedRef] = []
        for t in tags:
            associated_refs.append(
                AdminCommitAssociatedRef(
                    ref_type="tag",
                    name=t.tag_name,
                    detail=t.target_object_sha,
                )
            )

        parent_shas = [
            p.parent_sha for p in sorted(c.parents, key=lambda p: p.parent_order)
        ]
        return AdminCommitListItem(
            commit_sha=c.commit_sha,
            tree_sha=c.tree_sha,
            author_name=c.author_name,
            authored_at=c.authored_at,
            committed_at=c.committed_at,
            subject=c.subject,
            parent_shas=parent_shas,
            status=c_status,
            snapshot_id=snap.snapshot_id if snap else None,
            snapshot_state=snap.state if snap else None,
            vss_state=snap.vss_state if snap else None,
            eligible_for_answer=eligible,
            unavailable_reason=unavail_reason,
            associated_refs=associated_refs,
        )

    async def get_revision_status(
        self,
        repository_id: UUID,
        commit_sha: str,
    ) -> AdminCommitStatus:
        snap_stmt = (
            select(Snapshot)
            .where(
                Snapshot.repository_id == repository_id,
                Snapshot.target_revision == commit_sha,
            )
            .order_by(Snapshot.created_at.desc())
        )
        snap = await self._session.scalar(snap_stmt)
        status, _, _ = _compute_commit_status(snap)
        return status

    async def materialize_commit(
        self,
        repository_id: UUID,
        commit_sha: str,
        *,
        request_id: UUID,
        vss_project_id: str | None = None,
        branch_ref: str | None = None,
        materializer: CollectedRevisionMaterializer,
        git_client: RepositoryGitClient,
    ) -> AdminCommitMaterializeResponse:
        repo = await self._session.get(Repository, repository_id)
        if repo is None:
            raise ApiError(
                status_code=404,
                reason="REPOSITORY_NOT_FOUND",
                detail="등록된 Repository를 찾을 수 없습니다.",
                retryable=False,
            )

        # 1. Commit 존재 확인 (DB 카탈로그)
        commit_stmt = (
            select(RepositoryCommit)
            .where(
                RepositoryCommit.repository_id == repository_id,
                RepositoryCommit.commit_sha == commit_sha,
            )
            .options(selectinload(RepositoryCommit.parents))
        )
        commit = await self._session.scalar(commit_stmt)
        if commit is None:
            raise ApiError(
                status_code=404,
                reason="COMMIT_NOT_FOUND",
                detail="요청한 커밋을 찾을 수 없습니다.",
                retryable=False,
            )

        # 2. vss_project_id 및 branch_ref 결정
        resolved_branch_ref = branch_ref or repo.default_branch_ref
        target_tb: TrackedBranch | None = None
        tb_stmt = select(TrackedBranch).where(
            TrackedBranch.repository_id == repository_id,
            TrackedBranch.tracked.is_(True),
        )
        branches = list(await self._session.scalars(tb_stmt))
        for b in branches:
            if branch_ref and b.branch_ref == branch_ref:
                target_tb = b
                break
            if b.current_head_sha == commit_sha:
                target_tb = b
                break
        if target_tb is None and branches:
            target_tb = branches[0]

        resolved_vss_project_id = vss_project_id or (
            target_tb.vss_project_id
            if target_tb
            else f"{repo.canonical_name}-{commit_sha[:8]}"
        )

        # 3. 멱등성 검사: 이미 동일 (vss_project_id, target_revision)의 Snapshot이 존재하는가?
        snap_stmt = select(Snapshot).where(
            Snapshot.vss_project_id == resolved_vss_project_id,
            Snapshot.target_revision == commit_sha,
        )
        existing_snap = await self._session.scalar(snap_stmt)
        if existing_snap is not None and existing_snap.materialized_locator is not None:
            return AdminCommitMaterializeResponse(
                ok=True,
                repository_id=repository_id,
                commit_sha=commit_sha,
                snapshot_id=existing_snap.snapshot_id,
                state=existing_snap.state,
                vss_project_id=existing_snap.vss_project_id,
                materialized_locator=existing_snap.materialized_locator,
                created=False,
            )

        # 4. Git cache 검증
        if not git_client.has_commit(repository_id, commit_sha):
            raise ApiError(
                status_code=404,
                reason="COMMIT_OBJECT_UNAVAILABLE",
                detail="Git 캐시에서 커밋 object를 확인하지 못했습니다.",
                retryable=True,
            )

        # 5. Snapshot 레코드 준비
        base_revision = commit_sha
        if commit.parents:
            base_revision = commit.parents[0].parent_sha or commit_sha

        snapshot = existing_snap
        created_new = False
        if snapshot is None:
            created_new = True
            snapshot = Snapshot(
                snapshot_id=uuid4(),
                request_id=request_id,
                repository_id=repository_id,
                tracked_branch_id=target_tb.tracked_branch_id if target_tb else None,
                branch_ref=target_tb.branch_ref if target_tb else resolved_branch_ref,
                vss_project_id=resolved_vss_project_id,
                base_revision=base_revision,
                target_revision=commit_sha,
                source_type="remote_clone",
                state="materializing",
            )
            self._session.add(snapshot)
            await self._session.flush()

        # 6. Materialize 실행
        folder_id = snapshot.tracked_branch_id or repository_id
        try:
            materialized = await run_in_threadpool(
                materializer.materialize,
                repository_id=repository_id,
                tracked_branch_id=folder_id,
                snapshot_id=snapshot.snapshot_id,
                target_revision=commit_sha,
            )
        except Exception as exc:
            snapshot.state = "failed"
            snapshot.vss_state = "failed"
            snapshot.vss_reason = "MATERIALIZATION_FAILED"
            snapshot.vss_detail = str(exc)
            await self._session.commit()
            raise ApiError(
                status_code=500,
                reason="MATERIALIZATION_FAILED",
                detail=f"스냅샷 디렉터리 생성에 실패했습니다: {exc}",
                retryable=True,
            ) from exc

        # 7. 성공 상태 저장
        snapshot.materialized_locator = materialized.locator
        snapshot.source_type = materialized.source_type
        snapshot.state = "materialized"
        await self._session.commit()

        return AdminCommitMaterializeResponse(
            ok=True,
            repository_id=repository_id,
            commit_sha=commit_sha,
            snapshot_id=snapshot.snapshot_id,
            state=snapshot.state,
            vss_project_id=snapshot.vss_project_id,
            materialized_locator=snapshot.materialized_locator,
            created=created_new,
        )
