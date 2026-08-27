"""FastAPI application assembly."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx2
from fastapi import FastAPI, Request

from backend import __version__
from backend.core.config import Settings, get_settings
from backend.core.errors import register_exception_handlers
from backend.core.logging import configure_logging
from backend.features.frontend_proxy.router import router as frontend_proxy_router
from backend.features.health.router import router as health_router
from backend.features.materialization.service import SnapshotMaterializer
from backend.features.materialization.source import GitTreeSource, TreeSource
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
    """Create the application and lazily own its DB/VSS clients."""

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
        try:
            yield
        finally:
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
    return app


app = create_app()
