"""Structured API errors that never echo request content."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class ApiError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        detail: str,
        retryable: bool,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.reason = reason
        self.detail = detail
        self.retryable = retryable
        self.extra = extra or {}


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "unknown")


def _error_body(
    request: Request,
    *,
    reason: str,
    detail: str,
    retryable: bool,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "ok": False,
        "reason": reason,
        "detail": detail,
        "retryable": retryable,
        "request_id": _request_id(request),
    }
    if extra:
        body.update({key: value for key, value in extra.items() if key not in body})
    return body


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_body(
                request,
                reason=exc.reason,
                detail=exc.detail,
                retryable=exc.retryable,
                extra=exc.extra,
            ),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        errors = [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "message": error.get("msg", "Invalid value"),
                "type": error.get("type", "validation_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=_error_body(
                request,
                reason="REQUEST_VALIDATION_FAILED",
                detail="Request validation failed.",
                retryable=False,
                extra={"errors": errors},
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            detail_body = dict(exc.detail)
            reason = str(detail_body.pop("reason", "HTTP_ERROR"))
            detail = str(detail_body.pop("detail", "HTTP request failed."))
            retryable = bool(detail_body.pop("retryable", False))
        else:
            detail_body = {}
            reason = "HTTP_ERROR"
            detail = str(exc.detail)
            retryable = False
        return JSONResponse(
            status_code=exc.status_code,
            headers=exc.headers,
            content=_error_body(
                request,
                reason=reason,
                detail=detail,
                retryable=retryable,
                extra=detail_body,
            ),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled_request_error request_id=%s error_type=%s",
            _request_id(request),
            type(exc).__name__,
        )
        return JSONResponse(
            status_code=500,
            content=_error_body(
                request,
                reason="INTERNAL_SERVER_ERROR",
                detail="An unexpected internal error occurred.",
                retryable=True,
            ),
        )
