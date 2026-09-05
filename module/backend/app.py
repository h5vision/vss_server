"""Snapshot Backend FastAPI 애플리케이션 구성."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx2
from fastapi import FastAPI, Request
from sqlalchemy.exc import SQLAlchemyError

from backend import __version__
from backend.bootstrap.container import build_container
from backend.core.config import Settings, get_settings
from backend.core.errors import register_exception_handlers
from backend.core.logging import configure_logging
from backend.features.admin.audit import record_audit
from backend.features.admin.router import router as admin_router
from backend.features.frontend_proxy.router import router as frontend_proxy_router
from backend.features.health.router import router as health_router
from backend.features.indexing.router import router as indexing_router
from backend.features.materialization.source import TreeSource
from backend.features.vss_sources.router import router as vss_sources_router
from backend.features.workspace_overlays.router import router as workspace_overlays_router

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    vss_transport: httpx2.BaseTransport | None = None,
    github_transport: httpx2.BaseTransport | None = None,
    gitlab_transport: httpx2.BaseTransport | None = None,
    materialization_source: TreeSource | None = None,
) -> FastAPI:
    """애플리케이션을 만들고 ApplicationContainer Composition Root를 lifespan에 연결한다."""
    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(
            resolved_settings,
            vss_transport=vss_transport,
            github_transport=github_transport,
            gitlab_transport=gitlab_transport,
            materialization_source=materialization_source,
            start_recovery=True,
        )
        app.state.container = container

        # 하위 호환성 유지: 기존 라우터/미들웨어/테스트에서 참조하는
        # app.state 속성 매핑 (사전 주입된 mock 보존)
        if not hasattr(app.state, "vss_client"):
            app.state.vss_client = container.vss_client
        if not hasattr(app.state, "db_engine"):
            app.state.db_engine = container.db_engine
        if not hasattr(app.state, "db_sessionmaker"):
            app.state.db_sessionmaker = container.db_sessionmaker
        if not hasattr(app.state, "snapshot_materializer"):
            app.state.snapshot_materializer = container.snapshot_materializer
        if not hasattr(app.state, "repository_git_client"):
            app.state.repository_git_client = container.repository_git_client
        if not hasattr(app.state, "collected_revision_materializer"):
            app.state.collected_revision_materializer = container.collected_revision_materializer
        if not hasattr(app.state, "repository_collection_service"):
            app.state.repository_collection_service = container.repository_collection_service
        if not hasattr(app.state, "commit_catalog_service"):
            app.state.commit_catalog_service = container.commit_catalog_service
        if not hasattr(app.state, "change_request_service"):
            app.state.change_request_service = container.change_request_service
        if not hasattr(app.state, "repository_tag_service"):
            app.state.repository_tag_service = container.repository_tag_service
        if not hasattr(app.state, "snapshot_index_service"):
            app.state.snapshot_index_service = container.snapshot_index_service
        if not hasattr(app.state, "snapshot_retry_service"):
            app.state.snapshot_retry_service = container.snapshot_retry_service
        if not hasattr(app.state, "snapshot_recovery_task"):
            app.state.snapshot_recovery_task = container.snapshot_recovery_task

        try:
            yield
        finally:
            await container.dispose()

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
