"""Application composition root and dependency container."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass

import httpx2
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from backend.core.config import Settings
from backend.core.errors import ApiError
from backend.features.commit_catalog.service import CommitCatalogService
from backend.features.indexing.index import SnapshotIndexService
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
from backend.infrastructure.database.engine import (
    create_sessionmaker,
    get_engine_from_settings,
)
from backend.infrastructure.git.runner import GitCommandRunner
from backend.integrations.ollama.client import OllamaRuntimeClient, OllamaRuntimeError
from backend.integrations.vss.chat_relay import VssChatRelayClient
from backend.integrations.vss.client import VssHttpClient

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ApplicationContainer:
    """Explicit Composition Root container holding all application singletons."""

    settings: Settings
    vss_client: VssHttpClient
    vss_chat_relay_client: VssChatRelayClient
    ollama_runtime_client: OllamaRuntimeClient
    snapshot_materializer: SnapshotMaterializer
    db_engine: AsyncEngine | None = None
    db_sessionmaker: async_sessionmaker[AsyncSession] | None = None
    repository_git_client: RepositoryGitClient | None = None
    collected_revision_materializer: CollectedRevisionMaterializer | None = None
    collected_snapshot_publisher: CollectedSnapshotPublisher | None = None
    commit_catalog_service: CommitCatalogService | None = None
    repository_collection_service: RepositoryCollectionService | None = None
    snapshot_index_service: SnapshotIndexService | None = None
    snapshot_retry_service: SnapshotRetryService | None = None
    snapshot_recovery_task: asyncio.Task[None] | None = None
    ollama_auto_up_task: asyncio.Task[None] | None = None

    async def dispose(self) -> None:
        """Gracefully shuts down all background tasks, clients, and database connections."""
        for task in (self.snapshot_recovery_task, self.ollama_auto_up_task):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


        if hasattr(self.ollama_runtime_client, "close"):
            self.ollama_runtime_client.close()

        if hasattr(self.vss_client, "close"):
            self.vss_client.close()

        await self.vss_chat_relay_client.aclose()

        if self.db_engine is not None:
            await self.db_engine.dispose()


def build_container(
    settings: Settings,
    *,
    vss_transport: httpx2.BaseTransport | None = None,
    ollama_transport: httpx2.BaseTransport | None = None,
    materialization_source: TreeSource | None = None,
    start_recovery: bool = True,
) -> ApplicationContainer:
    """Instantiates and wires all domain services, clients, and stores."""
    vss_client = VssHttpClient.from_settings(settings, transport=vss_transport)
    vss_chat_relay_client = VssChatRelayClient.from_settings(
        settings,
        transport=vss_transport,
    )
    ollama_runtime_client = OllamaRuntimeClient.from_settings(
        settings,
        transport=ollama_transport,
    )

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
    collection_materializer: CollectedRevisionMaterializer | None = None
    collection_publisher: CollectedSnapshotPublisher | None = None
    commit_catalog_service: CommitCatalogService | None = None
    collection_service: RepositoryCollectionService | None = None
    snapshot_index_service: SnapshotIndexService | None = None
    snapshot_retry_service: SnapshotRetryService | None = None

    if db_sessionmaker is not None:
        git_runner = GitCommandRunner(
            default_timeout_seconds=settings.snapshot_git_command_timeout_seconds
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

        collection_service = RepositoryCollectionService(
            sessionmaker=db_sessionmaker,
            git_client=repository_git_client,
            publisher=collection_publisher,
            sync_lease_seconds=settings.snapshot_collection_sync_lease_seconds,
            commit_catalog_service=commit_catalog_service,
        )

        snapshot_index_service = SnapshotIndexService(
            sessionmaker=db_sessionmaker,
            materializer=snapshot_materializer,
            vss_client=vss_client,
            index_orchestration_mode=settings.snapshot_index_orchestration_mode,
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

    auto_up_task: asyncio.Task[None] | None = None
    if start_recovery:
        async def monitor_ollama_auto_up() -> None:
            while True:
                await asyncio.sleep(settings.ollama_auto_up_interval_seconds)
                for model_name in ollama_runtime_client.auto_up_model_names():
                    try:
                        result = await asyncio.to_thread(
                            ollama_runtime_client.ensure_auto_up_model,
                            model_name,
                        )
                    except asyncio.CancelledError:
                        raise
                    except OllamaRuntimeError as exc:
                        logger.warning(
                            "ollama_auto_up_failed model=%s reason=%s retryable=%s",
                            model_name,
                            exc.reason,
                            exc.retryable,
                        )
                    except Exception as exc:
                        logger.error(
                            "ollama_auto_up_failed model=%s error_type=%s",
                            model_name,
                            type(exc).__name__,
                        )
                    else:
                        if result is not None:
                            logger.info("ollama_auto_up_restored model=%s", result.model_name)

        auto_up_task = asyncio.create_task(monitor_ollama_auto_up())

    return ApplicationContainer(
        settings=settings,
        vss_client=vss_client,
        vss_chat_relay_client=vss_chat_relay_client,
        ollama_runtime_client=ollama_runtime_client,
        db_engine=database_engine,
        db_sessionmaker=db_sessionmaker,
        snapshot_materializer=snapshot_materializer,
        repository_git_client=repository_git_client,
        collected_revision_materializer=collection_materializer,
        collected_snapshot_publisher=collection_publisher,
        commit_catalog_service=commit_catalog_service,
        repository_collection_service=collection_service,
        snapshot_index_service=snapshot_index_service,
        snapshot_retry_service=snapshot_retry_service,
        snapshot_recovery_task=recovery_task,
        ollama_auto_up_task=auto_up_task,
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
