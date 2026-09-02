"""Git cache의 exact commit을 immutable VSS 입력 디렉터리로 승격한다."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from backend.features.materialization.paths import MaterializationPaths
from backend.features.materialization.service import MaterializedTree
from backend.features.repository_collection.git_client import RepositoryGitClient


class CollectedRevisionMaterializer:
    def __init__(self, *, root: Path, git_client: RepositoryGitClient) -> None:
        self._paths = MaterializationPaths(root)
        self._git_client = git_client

    def materialize(
        self,
        *,
        repository_id: UUID,
        tracked_branch_id: UUID,
        snapshot_id: UUID,
        target_revision: str,
    ) -> MaterializedTree:
        staging = self._paths.staging_path(tracked_branch_id, snapshot_id)
        revision = self._paths.revision_path(tracked_branch_id, target_revision)
        if revision.is_dir():
            self._paths.assert_tree_has_no_links(revision)
            self._git_client.verify_checkout(revision, target_revision)
            return MaterializedTree(
                project_root=revision,
                locator=self._paths.locator(revision),
                source_type="remote_clone",
            )
        try:
            # 이전 프로세스가 staging 작성 중 종료됐더라도 동일 Snapshot ID의 제한된
            # staging만 정리한다. 다른 Snapshot이나 완성 revision은 삭제하지 않는다.
            self._paths.cleanup_staging(staging)
            self._paths.prepare_staging_parent(staging)
            self._git_client.checkout_revision(
                repository_id=repository_id,
                revision=target_revision,
                destination=staging,
            )
            self._paths.assert_tree_has_no_links(staging)
            self._git_client.verify_checkout(staging, target_revision)
            self._paths.promote(staging, revision)
        except Exception:
            self._paths.cleanup_staging(staging)
            raise
        return MaterializedTree(
            project_root=revision,
            locator=self._paths.locator(revision),
            source_type="remote_clone",
        )
