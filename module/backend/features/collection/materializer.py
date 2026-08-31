"""수집된 HEAD commit을 immutable revision 디렉터리로 승격한다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from backend.features.collection.errors import CollectionError
from backend.features.collection.git_client import GitCollectionClient
from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.paths import MaterializationPaths
from backend.features.materialization.source import GitTreeSource


@dataclass(frozen=True, slots=True)
class CollectedTree:
    project_root: Path
    locator: str


class CollectionMaterializer:
    """mirror의 exact commit을 base/overlay 없이 그대로 materialize한다.

    수집 Snapshot은 delta가 없으므로 checkout 결과가 곧 target tree다. 승격 전에
    overlay 흐름과 동일한 Git 증명(write-tree 비교, HEAD 일치, clean working tree)을
    적용해 새 HEAD의 immutable tree와 VSS source descriptor가 같은 commit/tree SHA를
    가리키도록 보장한다.
    """

    def __init__(
        self,
        *,
        root: Path,
        git: GitCollectionClient,
        attest: GitTreeSource,
    ) -> None:
        self._paths = MaterializationPaths(root)
        self._git = git
        self._attest = attest

    def materialize(
        self,
        *,
        owner_id: UUID,
        snapshot_id: UUID,
        mirror_dir: Path,
        revision: str,
    ) -> CollectedTree:
        staging = self._paths.staging_path(owner_id, snapshot_id)
        target = self._paths.revision_path(owner_id, revision.lower())
        try:
            self._paths.prepare_staging_parent(staging)
            self._git.checkout_tree(mirror_dir, revision, staging)
            self._paths.assert_tree_has_no_links(staging)
            self._attest.attest_target(staging, revision.lower())
            self._paths.promote(staging, target)
        except MaterializationError:
            self._paths.cleanup_staging(staging)
            raise
        except CollectionError as exc:
            self._paths.cleanup_staging(staging)
            raise MaterializationError(
                reason=exc.reason,
                detail=exc.detail,
                status_code=exc.status_code,
                retryable=exc.retryable,
            ) from exc
        except OSError as exc:
            self._paths.cleanup_staging(staging)
            raise MaterializationError(
                reason="SNAPSHOT_MATERIALIZATION_FAILED",
                detail="수집 Snapshot 파일 tree를 생성하지 못했습니다.",
                status_code=500,
                retryable=True,
            ) from exc

        return CollectedTree(
            project_root=target,
            locator=self._paths.locator(target),
        )
