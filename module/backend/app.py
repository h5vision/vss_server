"""FastAPI application assembly."""

from __future__ import annotations

import logging
import time
from uuid import uuid4

from fastapi import FastAPI, Request

from backend import __version__
from backend.core.config import Settings, get_settings
from backend.core.errors import register_exception_handlers
from backend.core.logging import configure_logging
from backend.features.health.router import router as health_router

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create the application without contacting external dependencies."""

    resolved_settings = settings or get_settings()
    configure_logging(resolved_settings.log_level)

    app = FastAPI(
        title=resolved_settings.app_name,
        version=__version__,
        docs_url="/docs" if resolved_settings.docs_enabled else None,
        redoc_url=None,
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
    return app


app = create_app()
