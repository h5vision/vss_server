"""Frontend overlay를 완전한 Git base tree에 적용해 immutable revision으로 승격한다."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from backend.features.materialization.errors import MaterializationError
from backend.features.materialization.paths import MaterializationPaths
from backend.features.materialization.source import TreeSource
from backend.features.workspace_overlays.schemas import WorkspaceOverlayRequest


@dataclass(frozen=True, slots=True)
class MaterializedTree:
    project_root: Path
    locator: str
    source_type: str = "remote_clone"


class SnapshotMaterializer:
    def __init__(self, *, root: Path, source: TreeSource) -> None:
        self._paths = MaterializationPaths(root)
        self._source = source

    def materialize(
        self,
        request: WorkspaceOverlayRequest,
        *,
        binding_id: UUID,
        snapshot_id: UUID,
        remote_url: str,
        branch_ref: str,
    ) -> MaterializedTree:
        staging = self._paths.staging_path(binding_id, snapshot_id)
        revision = self._paths.revision_path(binding_id, request.target_revision)
        try:
            # root 권한 부족과 staging 생성 실패도 호출자가 동일한 구조화 오류로 처리할 수
            # 있도록 준비 단계부터 materialization 오류 경계 안에 둔다.
            self._paths.prepare_staging_parent(staging)
            self._source.populate(
                staging,
                remote_url=remote_url,
                branch_ref=branch_ref,
                base_revision=request.base_revision,
                target_revision=request.target_revision,
            )
            self._paths.assert_tree_has_no_links(staging)
            self._apply_overlay(staging, request)
            self._paths.assert_tree_has_no_links(staging)
            self._source.attest_target(staging, request.target_revision)
            self._paths.promote(staging, revision)
        except MaterializationError:
            self._paths.cleanup_staging(staging)
            raise
        except OSError as exc:
            self._paths.cleanup_staging(staging)
            raise MaterializationError(
                reason="SNAPSHOT_MATERIALIZATION_FAILED",
                detail="Snapshot 파일 tree를 생성하지 못했습니다.",
                status_code=500,
                retryable=True,
            ) from exc

        return MaterializedTree(
            project_root=revision,
            locator=self._paths.locator(revision),
        )

    def verify_existing(self, locator: str, target_revision: str) -> MaterializedTree:
        project_root = self._paths.path_from_locator(locator)
        if not project_root.is_dir():
            raise MaterializationError(
                reason="SNAPSHOT_MATERIALIZATION_FAILED",
                detail="재시도할 immutable revision 디렉터리를 찾을 수 없습니다.",
                status_code=409,
                retryable=False,
            )
        self._paths.assert_no_link_components(project_root)
        self._paths.assert_tree_has_no_links(project_root)
        self._source.verify_target(project_root, target_revision)
        return MaterializedTree(
            project_root=project_root,
            locator=self._paths.locator(project_root),
        )

    def _apply_overlay(self, root: Path, request: WorkspaceOverlayRequest) -> None:
        removal_paths = [*request.deleted_paths, *(rename.old_path for rename in request.renames)]
        for relative_path in removal_paths:
            target = self._paths.mutation_path(root, relative_path)
            if target.is_dir():
                raise MaterializationError(
                    reason="SNAPSHOT_PATH_UNSAFE",
                    detail="Frontend overlay는 디렉터리 전체 삭제를 요청할 수 없습니다.",
                    status_code=409,
                    retryable=False,
                )
            if target.exists():
                target.unlink()

        for file in request.files:
            target = self._paths.mutation_path(root, file.path)
            if target.is_dir():
                raise MaterializationError(
                    reason="SNAPSHOT_PATH_UNSAFE",
                    detail="파일 경로가 기존 디렉터리와 충돌합니다.",
                    status_code=409,
                    retryable=False,
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            self._paths.assert_no_link_components(target.parent, stop=root)
            target.write_text(file.content, encoding="utf-8", newline="")
