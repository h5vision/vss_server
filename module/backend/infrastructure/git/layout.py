"""Git cache layout and bare repository filesystem boundary."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4

from backend.features.repository_collection.errors import CollectionError
from backend.infrastructure.git.runner import (
    GitCommandRunner,
    assert_inside_root,
    remove_readonly,
)


@dataclass(frozen=True, slots=True)
class GitCacheLayout:
    """Manages secure bare Git repository cache directories on disk."""

    root: Path

    @property
    def cache_root(self) -> Path:
        resolved_root = self.root.expanduser().resolve()
        if resolved_root == Path(resolved_root.anchor):
            raise ValueError("repository cache root must not be a filesystem root")
        return resolved_root / ".repository-cache"

    def cache_path(self, repository_id: UUID) -> Path:
        candidate = self.cache_root / f"{repository_id.hex}.git"
        return self._inside_cache_root(candidate)

    def assert_safe(self, path: Path) -> None:
        try:
            assert_inside_root(path, self.cache_root)
        except ValueError as exc:
            raise CollectionError(
                reason="REPOSITORY_CACHE_FAILED",
                detail="Repository Git cache를 안전하게 준비하지 못했습니다.",
                retryable=True,
                status_code=500,
            ) from exc

    def ensure_cache(
        self,
        repository_id: UUID,
        runner: GitCommandRunner,
        remote_url: str | None = None,
    ) -> Path:
        cache = self.cache_path(repository_id)
        cache.parent.mkdir(parents=True, exist_ok=True)
        self.assert_safe(cache.parent)

        failure_err = CollectionError(
            reason="REPOSITORY_CACHE_FAILED",
            detail="Repository Git cache를 안전하게 준비하지 못했습니다.",
            retryable=True,
            status_code=500,
        )

        if cache.exists():
            self.assert_safe(cache)
            is_bare = runner.output(
                ["git", "-C", str(cache), "rev-parse", "--is-bare-repository"],
                failure=failure_err,
            )
            if is_bare != "true":
                raise failure_err
            if remote_url:
                runner.run(
                    ["git", "-C", str(cache), "remote", "set-url", "origin", remote_url],
                    failure=failure_err,
                )
            return cache

        staging = self._inside_cache_root(
            cache.parent / f".{repository_id.hex}-{uuid4().hex}.tmp"
        )
        try:
            runner.run(
                ["git", "init", "--bare", "--quiet", str(staging)],
                failure=failure_err,
            )
            if remote_url:
                runner.run(
                    ["git", "-C", str(staging), "remote", "add", "origin", remote_url],
                    failure=failure_err,
                )
            runner.run(
                ["git", "-C", str(staging), "config", "gc.auto", "0"],
                failure=failure_err,
            )
            staging.replace(cache)
            self.assert_safe(cache)
        finally:
            if staging.exists():
                shutil.rmtree(staging, onerror=remove_readonly)
        return cache

    def _inside_cache_root(self, path: Path) -> Path:
        root = self.cache_root
        candidate = path if path.is_absolute() else root / path
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise CollectionError(
                reason="REPOSITORY_CACHE_FAILED",
                detail="Repository Git cache를 안전하게 준비하지 못했습니다.",
                retryable=True,
                status_code=500,
            ) from exc
        return candidate
