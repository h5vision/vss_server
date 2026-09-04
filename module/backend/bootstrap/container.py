"""Application composition root and dependency container."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

import httpx2
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.core.config import Settings
from backend.core.errors import ApiError
from backend.features.change_requests.service import ChangeRequestCollectionService
from backend.features.commit_catalog.service import CommitCatalogService
from backend.features.indexing.recovery import SnapshotRecoveryCoordinator
from backend.features.indexing.retry import SnapshotRetryService
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.materialization.source import GitTreeSource, TreeSource
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.materializer import (
    CollectedRevisionMaterializer,
)
from backend.features.repository_collection.publisher import CollectedSnapshotPublisher
from backend.features.repository_collection.service import RepositoryCollectionService
from backend.features.repository_tags.service import RepositoryTagService
from backend.infrastructure.database.engine import (
    create_sessionmaker,
    get_engine_from_settings,
)
from backend.infrastructure.git import RepositoryWorkspaceManager
from backend.infrastructure.git.runner import GitCommandRunner
from backend.integrations.change_requests.github import GitHubChangeRequestClient
from backend.integrations.change_requests.gitlab import GitLabChangeRequestClient
from backend.integrations.vss.client import VssHttpClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationContainer:
    """Explicit Composition Root container holding all application singletons."""

    settings: Settings
    vss_client: VssHttpClient
    snapshot_materializer: SnapshotMaterializer
    db_engine: AsyncEngine | None = None
    db_sessionmaker: async_sessionmaker[AsyncSession] | None = None
    repository_git_client: RepositoryGitClient | None = None
    repository_workspace_manager: RepositoryWorkspaceManager | None = None
    collected_revision_materializer: CollectedRevisionMaterializer | None = None
    collected_snapshot_publisher: CollectedSnapshotPublisher | None = None
    commit_catalog_service: CommitCatalogService | None = None
    change_request_service: ChangeRequestCollectionService | None = None
    repository_tag_service: RepositoryTagService | None = None
    repository_collection_service: RepositoryCollectionService | None = None
    snapshot_retry_service: SnapshotRetryService | None = None
    provider_clients: Sequence[Any] = field(default_factory=tuple)
    snapshot_recovery_task: asyncio.Task[None] | None = None

    async def dispose(self) -> None:
        """Gracefully shuts down all background tasks, clients, and database connections."""
        if self.snapshot_recovery_task is not None and not self.snapshot_recovery_task.done():
            self.snapshot_recovery_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.snapshot_recovery_task

        for client in self.provider_clients:
            if hasattr(client, "close"):
                client.close()

        if hasattr(self.vss_client, "close"):
            self.vss_client.close()

        if self.db_engine is not None:
            await self.db_engine.dispose()


def build_container(
    settings: Settings,
    *,
    vss_transport: httpx2.BaseTransport | None = None,
    github_transport: httpx2.BaseTransport | None = None,
    gitlab_transport: httpx2.BaseTransport | None = None,
    materialization_source: TreeSource | None = None,
    start_recovery: bool = True,
) -> ApplicationContainer:
    """Instantiates and wires all domain services, clients, and stores."""
    vss_client = VssHttpClient.from_settings(settings, transport=vss_transport)

    database_engine: AsyncEngine | None = None
    db_sessionmaker: async_sessionmaker[AsyncSession] | None = None
    if settings.database_url:
        database_engine = get_engine_from_settings(settings)
        db_sessionmaker = create_sessionmaker(database_engine)

    snapshot_materializer = SnapshotMaterializer(
        root=settings.snapshot_materialization_root,
        source=materialization_source
        or GitTreeSource(command_timeout_seconds=settings.snapshot_git_command_timeout_seconds),
    )

    repository_git_client: RepositoryGitClient | None = None
    repository_workspace_manager: RepositoryWorkspaceManager | None = None
    collection_materializer: CollectedRevisionMaterializer | None = None
    collection_publisher: CollectedSnapshotPublisher | None = None
    commit_catalog_service: CommitCatalogService | None = None
    change_request_service: ChangeRequestCollectionService | None = None
    tag_service: RepositoryTagService | None = None
    collection_service: RepositoryCollectionService | None = None
    snapshot_retry_service: SnapshotRetryService | None = None
    provider_clients: list[Any] = []

    if db_sessionmaker is not None:
        git_runner = GitCommandRunner(
            default_timeout_seconds=settings.snapshot_git_command_timeout_seconds
        )
        repository_workspace_manager = RepositoryWorkspaceManager(
            root=settings.snapshot_repository_root,
            runner=git_runner,
        )
        repository_git_client = RepositoryGitClient(
            root=settings.snapshot_repository_root,
            command_timeout_seconds=settings.snapshot_git_command_timeout_seconds,
            runner=git_runner,
        )
        collection_materializer = CollectedRevisionMaterializer(
            root=settings.snapshot_materialization_root,
            git_client=repository_git_client,
        )
        collection_publisher = CollectedSnapshotPublisher(
            sessionmaker=db_sessionmaker,
            materializer=collection_materializer,
            vss_client=vss_client,
            index_orchestration_mode=settings.snapshot_index_orchestration_mode,
        )
        commit_catalog_service = CommitCatalogService(
            sessionmaker=db_sessionmaker,
            git_client=repository_git_client,
            max_commits=settings.snapshot_commit_catalog_max_commits,
            batch_size=settings.snapshot_commit_catalog_batch_size,
            timeout_seconds=settings.snapshot_commit_catalog_timeout_seconds,
            lease_seconds=settings.snapshot_commit_catalog_lease_seconds,
            subject_max_length=settings.snapshot_commit_subject_max_length,
        )

        if settings.snapshot_change_request_collection_enabled:
            github_client = GitHubChangeRequestClient(
                base_url=str(settings.snapshot_github_api_url),
                token=(
                    settings.snapshot_github_api_token.get_secret_value()
                    if settings.snapshot_github_api_token
                    else None
                ),
                api_version=settings.snapshot_github_api_version,
                max_pages=settings.snapshot_change_request_max_pages,
                connect_timeout_seconds=settings.snapshot_change_request_connect_timeout_seconds,
                read_timeout_seconds=settings.snapshot_change_request_read_timeout_seconds,
                transport=github_transport,
            )
            gitlab_client = GitLabChangeRequestClient(
                base_url=str(settings.snapshot_gitlab_api_url),
                token=(
                    settings.snapshot_gitlab_api_token.get_secret_value()
                    if settings.snapshot_gitlab_api_token
                    else None
                ),
                max_pages=settings.snapshot_change_request_max_pages,
                connect_timeout_seconds=settings.snapshot_change_request_connect_timeout_seconds,
                read_timeout_seconds=settings.snapshot_change_request_read_timeout_seconds,
                transport=gitlab_transport,
            )
            provider_clients.extend((github_client, gitlab_client))
            change_request_service = ChangeRequestCollectionService(
                sessionmaker=db_sessionmaker,
                git_client=repository_git_client,
                provider_clients={
                    "github": github_client,
                    "gitlab": gitlab_client,
                },
            )

        if settings.snapshot_tag_collection_enabled:
            tag_service = RepositoryTagService(
                sessionmaker=db_sessionmaker,
                git_client=repository_git_client,
                max_tags=settings.snapshot_tag_max_count,
            )

        collection_service = RepositoryCollectionService(
            sessionmaker=db_sessionmaker,
            git_client=repository_git_client,
            publisher=collection_publisher,
            workspace_manager=repository_workspace_manager,
            sync_lease_seconds=settings.snapshot_collection_sync_lease_seconds,
            commit_catalog_service=commit_catalog_service,
            change_request_service=change_request_service,
            tag_service=tag_service,
        )

        snapshot_retry_service = SnapshotRetryService(
            sessionmaker=db_sessionmaker,
            materializer=snapshot_materializer,
            vss_client=vss_client,
            index_orchestration_mode=settings.snapshot_index_orchestration_mode,
        )

    recovery_task: asyncio.Task[None] | None = None
    if (
        start_recovery
        and database_engine is not None
        and db_sessionmaker is not None
        and settings.snapshot_recovery_on_startup
    ):
        coordinator = SnapshotRecoveryCoordinator(
            engine=database_engine,
            sessionmaker=db_sessionmaker,
            vss_client=vss_client,
        )

        async def recover_snapshots() -> None:
            try:
                summary = await coordinator.run_once(
                    limit=settings.snapshot_recovery_batch_size,
                )
                logger.info(
                    "snapshot_recovery_completed lock_acquired=%s examined=%s synchronized=%s "
                    "unavailable=%s failed=%s",
                    summary.lock_acquired,
                    summary.examined,
                    summary.synchronized,
                    summary.unavailable,
                    summary.failed,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    "snapshot_recovery_failed error_type=%s",
                    type(exc).__name__,
                )

        recovery_task = asyncio.create_task(recover_snapshots())

    return ApplicationContainer(
        settings=settings,
        vss_client=vss_client,
        db_engine=database_engine,
        db_sessionmaker=db_sessionmaker,
        snapshot_materializer=snapshot_materializer,
        repository_git_client=repository_git_client,
        repository_workspace_manager=repository_workspace_manager,
        collected_revision_materializer=collection_materializer,
        collected_snapshot_publisher=collection_publisher,
        commit_catalog_service=commit_catalog_service,
        change_request_service=change_request_service,
        repository_tag_service=tag_service,
        repository_collection_service=collection_service,
        snapshot_retry_service=snapshot_retry_service,
        provider_clients=tuple(provider_clients),
        snapshot_recovery_task=recovery_task,
    )


def get_container(request: Request) -> ApplicationContainer:
    """FastAPI Dependency for obtaining the ApplicationContainer."""
    container: ApplicationContainer | None = getattr(request.app.state, "container", None)
    if container is None:
        raise ApiError(
            status_code=500,
            reason="CONTAINER_NOT_INITIALIZED",
            detail="Application container is not initialized.",
            retryable=False,
        )
    return container
