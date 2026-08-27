"""Resolve all mutable paths beneath one dedicated materialization root."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path, PurePosixPath
from uuid import UUID

from backend.features.materialization.errors import MaterializationError, unsafe_path


def _is_link_or_junction(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _remove_readonly(function, path: str, _error) -> None:
    """Remove Git's Windows read-only bit, then retry the scoped deletion."""

    os.chmod(path, stat.S_IWRITE)
    function(path)


class MaterializationPaths:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        if self.root == Path(self.root.anchor):
            raise ValueError("materialization root must not be a filesystem root")

    def staging_path(self, binding_id: UUID, snapshot_id: UUID) -> Path:
        return self._inside(
            self.root / binding_id.hex / "staging" / str(snapshot_id),
        )

    def revision_path(self, binding_id: UUID, revision: str) -> Path:
        return self._inside(
            self.root / binding_id.hex / "revisions" / revision.lower(),
        )

    def locator(self, path: Path) -> str:
        return self._inside(path).relative_to(self.root).as_posix()

    def path_from_locator(self, locator: str) -> Path:
        if not locator or "\\" in locator:
            raise unsafe_path("materialized locator가 안전한 POSIX 상대경로가 아닙니다.")
        pure = PurePosixPath(locator)
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
            raise unsafe_path("materialized locator가 안전한 POSIX 상대경로가 아닙니다.")
        return self._inside(self.root.joinpath(*pure.parts))

    def prepare_staging_parent(self, staging: Path) -> None:
        checked = self._inside(staging)
        self.root.mkdir(parents=True, exist_ok=True)
        if _is_link_or_junction(self.root):
            raise unsafe_path("materialization root는 symlink 또는 junction일 수 없습니다.")
        if checked.exists() or checked.is_symlink():
            raise MaterializationError(
                reason="SNAPSHOT_STAGING_ALREADY_EXISTS",
                detail="동일 Snapshot staging 경로가 이미 존재합니다.",
                status_code=409,
                retryable=False,
            )
        checked.parent.mkdir(parents=True, exist_ok=True)
        self.assert_no_link_components(checked.parent)

    def promote(self, staging: Path, revision: Path) -> None:
        checked_staging = self._inside(staging)
        checked_revision = self._inside(revision)
        if not checked_staging.is_dir():
            raise MaterializationError(
                reason="SNAPSHOT_MATERIALIZATION_FAILED",
                detail="승격할 Snapshot staging 디렉터리가 없습니다.",
                status_code=500,
                retryable=True,
            )
        if checked_revision.exists() or checked_revision.is_symlink():
            raise MaterializationError(
                reason="SNAPSHOT_REVISION_ALREADY_EXISTS",
                detail="같은 target revision의 immutable 디렉터리가 이미 존재합니다.",
                status_code=409,
                retryable=False,
            )
        checked_revision.parent.mkdir(parents=True, exist_ok=True)
        self.assert_no_link_components(checked_revision.parent)
        checked_staging.replace(checked_revision)

    def cleanup_staging(self, staging: Path) -> None:
        checked = self._inside(staging)
        if not checked.exists() and not checked.is_symlink():
            return
        if _is_link_or_junction(checked):
            checked.unlink()
            return
        shutil.rmtree(checked, onexc=_remove_readonly)

    def mutation_path(self, project_root: Path, relative_path: str) -> Path:
        checked_root = self._inside(project_root)
        candidate = checked_root.joinpath(*PurePosixPath(relative_path).parts)
        self._inside(candidate)
        self.assert_no_link_components(candidate, stop=checked_root)
        return candidate

    def assert_tree_has_no_links(self, project_root: Path) -> None:
        checked_root = self._inside(project_root)
        for candidate in checked_root.rglob("*"):
            if _is_link_or_junction(candidate):
                raise unsafe_path(
                    "materialized tree에 symlink 또는 junction이 있어 VSS 제출을 차단했습니다."
                )

    def assert_no_link_components(self, path: Path, *, stop: Path | None = None) -> None:
        checked = self._inside(path)
        checked_stop = self._inside(stop) if stop is not None else self.root
        current = checked
        while True:
            if (current.exists() or current.is_symlink()) and _is_link_or_junction(current):
                raise unsafe_path("Snapshot 경로에 symlink 또는 junction이 포함되어 있습니다.")
            if current == checked_stop:
                break
            if current == self.root or current.parent == current:
                raise unsafe_path()
            current = current.parent

    def _inside(self, path: Path) -> Path:
        candidate = path if path.is_absolute() else self.root / path
        candidate = Path(candidate)
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise unsafe_path() from exc
        return candidate
