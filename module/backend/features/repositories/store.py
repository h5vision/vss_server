"""Transactional Repository and Branch binding persistence operations."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.features.repositories.schemas import (
    BranchBindingCreateRequest,
    BranchBindingUpdateRequest,
    RepositoryCreateRequest,
    RepositoryUpdateRequest,
)
from backend.infrastructure.database.models import BranchBinding, Repository


@dataclass(frozen=True, slots=True)
class StoreLookupError(LookupError):
    reason: str
    detail: str
    retryable: bool = False

    def __str__(self) -> str:
        return self.detail


class RepositoryStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, request: RepositoryCreateRequest) -> Repository:
        repository = Repository(
            canonical_name=request.canonical_name,
            display_name=request.display_name,
            provider=request.provider,
            remote_url=request.remote_url.unicode_string(),
            default_branch_ref=request.default_branch_ref,
            active=request.active,
        )
        self._session.add(repository)
        await self._session.flush()
        return repository

    async def get(self, repository_id: UUID) -> Repository:
        repository = await self._session.get(Repository, repository_id)
        if repository is None:
            raise StoreLookupError(
                "REPOSITORY_NOT_FOUND",
                "요청한 Repository를 찾을 수 없습니다.",
            )
        return repository

    async def list(self, *, active: bool | None = None, limit: int = 100) -> list[Repository]:
        _validate_limit(limit)
        statement = select(Repository).order_by(Repository.created_at, Repository.repository_id)
        if active is not None:
            statement = statement.where(Repository.active.is_(active))
        result = await self._session.scalars(statement.limit(limit))
        return list(result)

    async def update(
        self,
        repository: Repository,
        request: RepositoryUpdateRequest,
    ) -> Repository:
        for field in request.model_fields_set:
            value = getattr(request, field)
            if field == "remote_url" and value is not None:
                value = value.unicode_string()
            setattr(repository, field, value)
        await self._session.flush()
        return repository

    async def deactivate(self, repository: Repository) -> Repository:
        repository.active = False
        await self._session.flush()
        return repository


class BranchBindingStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, request: BranchBindingCreateRequest) -> BranchBinding:
        binding = BranchBinding(**request.model_dump())
        self._session.add(binding)
        await self._session.flush()
        return binding

    async def get(self, binding_id: UUID) -> BranchBinding:
        binding = await self._session.get(BranchBinding, binding_id)
        if binding is None:
            raise StoreLookupError(
                "BRANCH_BINDING_NOT_FOUND",
                "요청한 Branch binding을 찾을 수 없습니다.",
            )
        return binding

    async def list(
        self,
        *,
        frontend_project_id: str | None = None,
        active: bool | None = None,
        limit: int = 100,
    ) -> list[BranchBinding]:
        _validate_limit(limit)
        statement = select(BranchBinding).order_by(
            BranchBinding.created_at,
            BranchBinding.binding_id,
        )
        if frontend_project_id is not None:
            statement = statement.where(
                BranchBinding.frontend_project_id == frontend_project_id.strip()
            )
        if active is not None:
            statement = statement.where(BranchBinding.active.is_(active))
        result = await self._session.scalars(statement.limit(limit))
        return list(result)

    async def resolve_active(self, frontend_project_id: str) -> BranchBinding:
        normalized_project_id = frontend_project_id.strip()
        if not normalized_project_id:
            raise ValueError("frontend_project_id must not be blank")
        statement = (
            select(BranchBinding)
            .where(
                or_(
                    BranchBinding.frontend_project_id == normalized_project_id,
                    BranchBinding.frontend_workspace_name == normalized_project_id,
                ),
                BranchBinding.active.is_(True),
            )
            .limit(2)
        )
        matches = list(await self._session.scalars(statement))
        if not matches:
            raise StoreLookupError(
                "SNAPSHOT_DESTINATION_REQUIRED",
                "활성 Repository/Branch/VSS binding이 없어 Snapshot 대상을 확정할 수 없습니다.",
            )
        if len(matches) > 1:
            raise StoreLookupError(
                "SNAPSHOT_DESTINATION_AMBIGUOUS",
                "활성 binding이 둘 이상이어서 Snapshot 대상을 하나로 확정할 수 없습니다.",
            )
        return matches[0]

    async def update(
        self,
        binding: BranchBinding,
        request: BranchBindingUpdateRequest,
    ) -> BranchBinding:
        for field in request.model_fields_set:
            setattr(binding, field, getattr(request, field))
        await self._session.flush()
        return binding

    async def deactivate(self, binding: BranchBinding) -> BranchBinding:
        binding.active = False
        await self._session.flush()
        return binding


def _validate_limit(limit: int) -> None:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
