"""Snapshot Backend FastAPI 애플리케이션 구성."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from uuid import uuid4

import httpx2
from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from backend import __version__
from backend.core.config import Settings, get_settings
from backend.core.errors import register_exception_handlers
from backend.core.logging import configure_logging
from backend.features.admin.router import admin_router
from backend.features.collection.git_client import GitCollectionClient
from backend.features.collection.materializer import CollectionMaterializer
from backend.features.collection.router import router as collection_router
from backend.features.collection.service import RepositoryCollectionService
from backend.features.frontend_proxy.router import router as frontend_proxy_router
from backend.features.health.router import router as health_router
from backend.features.indexing.recovery import SnapshotRecoveryCoordinator
from backend.features.indexing.retry import SnapshotRetryService
from backend.features.indexing.router import router as indexing_router
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.materialization.source import GitTreeSource, TreeSource
from backend.features.vss_sources.router import router as vss_sources_router
from backend.features.workspace_overlays.router import router as workspace_overlays_router
from backend.infrastructure.database.engine import (
    create_sessionmaker,
    get_engine_from_settings,
)
from backend.integrations.vss.client import VssHttpClient

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    vss_transport: httpx2.BaseTransport | None = None,
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
        recovery_task = None
        collection_service = None
        collection_sync_task = None
        retry_service = None
        if db_sessionmaker is not None:
            git_client = GitCollectionClient(
                command_timeout_seconds=resolved_settings.snapshot_git_command_timeout_seconds
            )
            collection_materializer = CollectionMaterializer(
                root=resolved_settings.snapshot_materialization_root,
                git=git_client,
            )
            collection_service = RepositoryCollectionService(
                sessionmaker=db_sessionmaker,
                git=git_client,
                materializer=collection_materializer,
                vss_client=vss_client,
                collection_root=resolved_settings.snapshot_collection_root,
            )
            retry_service = SnapshotRetryService(
                sessionmaker=db_sessionmaker,
                materializer=app.state.snapshot_materializer,
                vss_client=vss_client,
            )

            if resolved_settings.snapshot_collection_sync_interval_seconds > 0:
                async def run_periodic_collection_sync() -> None:
                    interval = resolved_settings.snapshot_collection_sync_interval_seconds
                    while True:
                        try:
                            await asyncio.sleep(interval)
                            await collection_service.sync_all(trigger="scheduled")
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            logger.warning(
                                "collection_periodic_sync_failed error=%s",
                                type(exc).__name__,
                            )

                collection_sync_task = asyncio.create_task(run_periodic_collection_sync())

            if resolved_settings.snapshot_recovery_on_startup:
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
                            "snapshot_recovery_completed lock_acquired=%s examined=%s "
                            "synchronized=%s unavailable=%s failed=%s",
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

        app.state.collection_service = collection_service
        app.state.collection_sync_task = collection_sync_task
        app.state.snapshot_recovery_task = recovery_task
        app.state.retry_service = retry_service
        try:
            yield
        finally:
            if collection_sync_task is not None and not collection_sync_task.done():
                collection_sync_task.cancel()
                with suppress(asyncio.CancelledError):
                    await collection_sync_task
            if recovery_task is not None and not recovery_task.done():
                recovery_task.cancel()
                with suppress(asyncio.CancelledError):
                    await recovery_task
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
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "request_completed method=%s path=%s status=%s elapsed_ms=%.1f request_id=%s",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
            request_id,
        )
        return response

    register_exception_handlers(app)
    app.include_router(health_router, prefix=resolved_settings.api_prefix)
    app.include_router(frontend_proxy_router, prefix=resolved_settings.api_prefix)
    app.include_router(workspace_overlays_router, prefix=resolved_settings.api_prefix)
    app.include_router(indexing_router, prefix=resolved_settings.api_prefix)
    app.include_router(vss_sources_router, prefix=resolved_settings.api_prefix)
    app.include_router(collection_router, prefix=resolved_settings.api_prefix)
    app.include_router(admin_router, prefix=resolved_settings.api_prefix)

    admin_static_dir = Path(__file__).resolve().parent.parent / "admin_web"
    if admin_static_dir.exists() and (admin_static_dir / "index.html").exists():
        app.mount(
            "/admin",
            StaticFiles(directory=str(admin_static_dir), html=True),
            name="admin_web",
        )

    return app


app = create_app()
