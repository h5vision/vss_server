"""Snapshot Backend FastAPI 애플리케이션 구성."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from uuid import uuid4

import httpx2
from fastapi import FastAPI, Request
from sqlalchemy.exc import SQLAlchemyError

from backend import __version__
from backend.core.config import Settings, get_settings
from backend.core.errors import register_exception_handlers
from backend.core.logging import configure_logging
from backend.features.admin.audit import record_audit
from backend.features.admin.router import router as admin_router
from backend.features.change_requests.service import ChangeRequestCollectionService
from backend.features.commit_catalog.service import CommitCatalogService
from backend.features.frontend_proxy.router import router as frontend_proxy_router
from backend.features.health.router import router as health_router
from backend.features.indexing.recovery import SnapshotRecoveryCoordinator
from backend.features.indexing.retry import SnapshotRetryService
from backend.features.indexing.router import router as indexing_router
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.materialization.source import GitTreeSource, TreeSource
from backend.features.repository_collection.git_client import RepositoryGitClient
from backend.features.repository_collection.materializer import CollectedRevisionMaterializer
from backend.features.repository_collection.publisher import CollectedSnapshotPublisher
from backend.features.repository_collection.service import RepositoryCollectionService
from backend.features.repository_tags.service import RepositoryTagService
from backend.features.vss_sources.router import router as vss_sources_router
from backend.features.workspace_overlays.router import router as workspace_overlays_router
from backend.infrastructure.database.engine import (
    create_sessionmaker,
    get_engine_from_settings,
)
from backend.integrations.change_requests.github import GitHubChangeRequestClient
from backend.integrations.change_requests.gitlab import GitLabChangeRequestClient
from backend.integrations.vss.client import VssHttpClient

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    vss_transport: httpx2.BaseTransport | None = None,
    github_transport: httpx2.BaseTransport | None = None,
    gitlab_transport: httpx2.BaseTransport | None = None,
    materialization_source: TreeSource | None = None,
) -> FastAPI:
    """애플리케이션을 만들고 lifespan에서 DB/VSS client를 소유한다."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        vss_client = VssHttpClient.from_settings(
            resolved_settings,
            transport=vss_transport,
        )
        database_engine = None
        db_sessionmaker = None
        if resolved_settings.database_url:
            database_engine = get_engine_from_settings(resolved_settings)
            db_sessionmaker = create_sessionmaker(database_engine)

        app.state.vss_client = vss_client
        app.state.db_engine = database_engine
        app.state.db_sessionmaker = db_sessionmaker
        app.state.snapshot_materializer = SnapshotMaterializer(
            root=resolved_settings.snapshot_materialization_root,
            source=materialization_source
            or GitTreeSource(
                command_timeout_seconds=resolved_settings.snapshot_git_command_timeout_seconds
            ),
        )
        app.state.repository_collection_service = None
        app.state.commit_catalog_service = None
        app.state.change_request_service = None
        app.state.repository_tag_service = None
        app.state.snapshot_retry_service = None
        provider_clients = []
        if db_sessionmaker is not None:
            repository_git_client = RepositoryGitClient(
                root=resolved_settings.snapshot_materialization_root,
                command_timeout_seconds=(
                    resolved_settings.snapshot_git_command_timeout_seconds
                ),
            )
            if not hasattr(app.state, "repository_git_client"):
                app.state.repository_git_client = repository_git_client
            collection_materializer = CollectedRevisionMaterializer(
                root=resolved_settings.snapshot_materialization_root,
                git_client=repository_git_client,
            )
            collection_publisher = CollectedSnapshotPublisher(
                sessionmaker=db_sessionmaker,
                materializer=collection_materializer,
                vss_client=vss_client,
                index_orchestration_mode=(
                    resolved_settings.snapshot_index_orchestration_mode
                ),
            )
            commit_catalog_service = CommitCatalogService(
                sessionmaker=db_sessionmaker,
                git_client=repository_git_client,
                max_commits=resolved_settings.snapshot_commit_catalog_max_commits,
                batch_size=resolved_settings.snapshot_commit_catalog_batch_size,
                timeout_seconds=(
                    resolved_settings.snapshot_commit_catalog_timeout_seconds
                ),
                lease_seconds=resolved_settings.snapshot_commit_catalog_lease_seconds,
                subject_max_length=(
                    resolved_settings.snapshot_commit_subject_max_length
                ),
            )
            app.state.commit_catalog_service = commit_catalog_service
            change_request_service = None
            if resolved_settings.snapshot_change_request_collection_enabled:
                github_client = GitHubChangeRequestClient(
                    base_url=str(resolved_settings.snapshot_github_api_url),
                    token=(
                        resolved_settings.snapshot_github_api_token.get_secret_value()
                        if resolved_settings.snapshot_github_api_token
                        else None
                    ),
                    api_version=resolved_settings.snapshot_github_api_version,
                    max_pages=resolved_settings.snapshot_change_request_max_pages,
                    connect_timeout_seconds=(
                        resolved_settings.snapshot_change_request_connect_timeout_seconds
                    ),
                    read_timeout_seconds=(
                        resolved_settings.snapshot_change_request_read_timeout_seconds
                    ),
                    transport=github_transport,
                )
                gitlab_client = GitLabChangeRequestClient(
                    base_url=str(resolved_settings.snapshot_gitlab_api_url),
                    token=(
                        resolved_settings.snapshot_gitlab_api_token.get_secret_value()
                        if resolved_settings.snapshot_gitlab_api_token
                        else None
                    ),
                    max_pages=resolved_settings.snapshot_change_request_max_pages,
                    connect_timeout_seconds=(
                        resolved_settings.snapshot_change_request_connect_timeout_seconds
                    ),
                    read_timeout_seconds=(
                        resolved_settings.snapshot_change_request_read_timeout_seconds
                    ),
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
            app.state.change_request_service = change_request_service
            tag_service = None
            if resolved_settings.snapshot_tag_collection_enabled:
                tag_service = RepositoryTagService(
                    sessionmaker=db_sessionmaker,
                    git_client=repository_git_client,
                    max_tags=resolved_settings.snapshot_tag_max_count,
                )
            app.state.repository_tag_service = tag_service
            app.state.repository_collection_service = RepositoryCollectionService(
                sessionmaker=db_sessionmaker,
                git_client=repository_git_client,
                publisher=collection_publisher,
                sync_lease_seconds=(
                    resolved_settings.snapshot_collection_sync_lease_seconds
                ),
                commit_catalog_service=commit_catalog_service,
                change_request_service=change_request_service,
                tag_service=tag_service,
            )
            app.state.snapshot_retry_service = SnapshotRetryService(
                sessionmaker=db_sessionmaker,
                materializer=app.state.snapshot_materializer,
                vss_client=vss_client,
                index_orchestration_mode=(
                    resolved_settings.snapshot_index_orchestration_mode
                ),
            )
        recovery_task = None
        if db_sessionmaker is not None and resolved_settings.snapshot_recovery_on_startup:
            coordinator = SnapshotRecoveryCoordinator(
                engine=database_engine,
                sessionmaker=db_sessionmaker,
                vss_client=vss_client,
            )

            async def recover_snapshots() -> None:
                try:
                    summary = await coordinator.run_once(
                        limit=resolved_settings.snapshot_recovery_batch_size
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

            # HTTP 기동을 VSS 응답 지연에 묶지 않되, lifespan이 끝날 때는 task를 취소해
            # engine과 VSS client가 먼저 닫히는 종료 경쟁을 막는다.
            recovery_task = asyncio.create_task(recover_snapshots())
        app.state.snapshot_recovery_task = recovery_task
        try:
            yield
        finally:
            if recovery_task is not None and not recovery_task.done():
                recovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await recovery_task
            for provider_client in provider_clients:
                provider_client.close()
            vss_client.close()
            if database_engine is not None:
                await database_engine.dispose()

    app = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = str(uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()

        response = await call_next(request)
        identity = getattr(request.state, "admin_identity", None)
        is_admin_mutation = (
            request.url.path.startswith(f"{resolved_settings.api_prefix}/admin/")
            and request.method in {"POST", "PATCH", "PUT", "DELETE"}
        )
        if (
            is_admin_mutation
            and identity is not None
            and response.status_code >= 400
            and getattr(app.state, "db_sessionmaker", None) is not None
        ):
            try:
                async with app.state.db_sessionmaker() as audit_session:
                    await record_audit(
                        audit_session,
                        request_id=identity.request_id,
                        actor=identity.actor_id,
                        action=(
                            "admin_request_denied"
                            if response.status_code == 403
                            else "admin_request_failed"
                        ),
                        target_type="admin_route",
                        target_id=request.url.path,
                        outcome="denied" if response.status_code == 403 else "failed",
                        reason=f"HTTP_{response.status_code}",
                        detail="The authenticated Admin mutation was not completed.",
                        details={"method": request.method},
                    )
                    await audit_session.commit()
            except SQLAlchemyError:
                logger.exception(
                    "admin_failure_audit_write_failed method=%s path=%s request_id=%s",
                    request.method,
                    request.url.path,
                    identity.request_id,
                )
        final_request_id = str(getattr(request.state, "request_id", request_id))
        response.headers["X-Request-ID"] = final_request_id

        logger.info(
            "request_completed method=%s path=%s status=%s elapsed_ms=%.1f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            final_request_id,
        )
        return response

    register_exception_handlers(app)
    app.include_router(health_router, prefix=resolved_settings.api_prefix)
    app.include_router(frontend_proxy_router, prefix=resolved_settings.api_prefix)
    app.include_router(workspace_overlays_router, prefix=resolved_settings.api_prefix)
    app.include_router(indexing_router, prefix=resolved_settings.api_prefix)
    app.include_router(vss_sources_router, prefix=resolved_settings.api_prefix)
    app.include_router(admin_router, prefix=resolved_settings.api_prefix)
    return app


app = create_app()
