"""FastAPI application for the independent Admin Web on port 4180."""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from admin_web.auth import AdminUser, LoginRateLimiter, UserAuthenticator, load_users
from admin_web.config import AdminWebSettings
from admin_web.proxy import required_role, role_allows, signed_headers

STATIC_DIR = Path(__file__).parent.resolve()
MUTATION_METHODS = {"POST", "PATCH", "DELETE", "PUT"}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


def _error(
    status_code: int,
    reason: str,
    detail: str,
    *,
    retryable: bool = False,
    request_id: UUID | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "ok": False,
        "reason": reason,
        "detail": detail,
        "retryable": retryable,
    }
    if request_id is not None:
        body["request_id"] = str(request_id)
    return JSONResponse(body, status_code=status_code, headers=headers)


def _session_identity(request: Request, users_file: Path) -> AdminUser | None:
    username = request.session.get("username")
    csrf_token = request.session.get("csrf_token")
    if not all(isinstance(value, str) and value for value in (username, csrf_token)):
        return None
    try:
        user = load_users(users_file).get(username)
    except ValueError:
        return None
    if user is None or not user.active:
        request.session.clear()
        return None
    request.session["role"] = user.role
    return user


def _verify_mutation(request: Request, settings: AdminWebSettings) -> JSONResponse | None:
    origin = request.headers.get("origin")
    if origin not in settings.allowed_origins:
        return _error(
            403,
            "REQUEST_ORIGIN_REJECTED",
            "The request origin is not allowed.",
        )
    expected = request.session.get("csrf_token", "")
    supplied = request.headers.get("x-csrf-token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return _error(403, "CSRF_REJECTED", "The CSRF token is invalid.")
    return None


def _raw_target(request: Request) -> str:
    raw_path = request.scope.get("raw_path", request.url.path.encode("ascii"))
    path = raw_path.decode("ascii")
    raw_query = request.scope.get("query_string", b"").decode("ascii")
    return f"{path}?{raw_query}" if raw_query else path


def create_app(
    settings: AdminWebSettings | None = None,
    *,
    backend_transport: httpx2.AsyncBaseTransport | httpx2.BaseTransport | None = None,
    clock: Callable[[], float] = time.time,
    request_id_factory: Callable[[], UUID] = uuid4,
) -> FastAPI:
    settings = settings or AdminWebSettings.from_environment()
    load_users(settings.users_file)
    rate_limiter = LoginRateLimiter(
        max_attempts=settings.login_max_attempts,
        window_seconds=settings.login_window_seconds,
        clock=clock,
    )
    backend_client = httpx2.AsyncClient(
        base_url=settings.backend_url,
        transport=backend_transport,
        timeout=httpx2.Timeout(settings.backend_timeout_seconds),
        trust_env=False,
        follow_redirects=False,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.backend_client = backend_client
        yield
        await backend_client.aclose()

    app = FastAPI(
        title="Vision Snapshot Admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="snapshot_admin_session",
        max_age=settings.session_max_age_seconds,
        same_site="strict",
        https_only=settings.secure_cookies,
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        request.state.request_id = request_id_factory()
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'self'"
        )
        if request.url.path.startswith(("/api/", "/v1/admin/")):
            response.headers["Cache-Control"] = "no-store"
            response.headers.setdefault(
                "X-Request-ID",
                str(request.state.request_id),
            )
        elif request.url.path in {"/", "/app.js", "/styles.css"}:
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/health")
    async def health() -> dict[str, object]:
        return {"ok": True, "service": "snapshot-admin-web", "port": 4180}

    @app.post("/api/auth/login")
    async def login(request: Request, payload: LoginRequest) -> Response:
        client_host = request.client.host if request.client is not None else "unknown"
        rate_key = client_host
        retry_after = rate_limiter.check(rate_key)
        if retry_after is not None:
            return _error(
                429,
                "LOGIN_RATE_LIMITED",
                "Too many login attempts. Try again later.",
                headers={"Retry-After": str(retry_after)},
            )

        try:
            users = load_users(settings.users_file)
        except ValueError:
            return _error(
                503,
                "USER_REGISTRY_UNAVAILABLE",
                "The Admin user registry is unavailable.",
                retryable=True,
            )
        user = UserAuthenticator(users).authenticate(
            payload.username,
            payload.password,
        )
        if user is None:
            rate_limiter.record_failure(rate_key)
            return _error(
                401,
                "INVALID_CREDENTIALS",
                "Invalid username or password.",
            )

        rate_limiter.clear(rate_key)
        csrf_token = secrets.token_urlsafe(32)
        request.session.clear()
        request.session.update(
            {
                "username": user.username,
                "role": user.role,
                "csrf_token": csrf_token,
            }
        )
        return JSONResponse(
            {
                "authenticated": True,
                "username": user.username,
                "role": user.role,
                "csrf_token": csrf_token,
            }
        )

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request) -> Response:
        if _session_identity(request, settings.users_file) is None:
            return _error(401, "AUTHENTICATION_REQUIRED", "Authentication is required.")
        rejected = _verify_mutation(request, settings)
        if rejected is not None:
            return rejected
        request.session.clear()
        return Response(status_code=204)

    @app.get("/api/auth/session")
    async def session(request: Request) -> JSONResponse:
        user = _session_identity(request, settings.users_file)
        if user is None:
            return JSONResponse({"authenticated": False})
        return JSONResponse(
            {
                "authenticated": True,
                "username": user.username,
                "role": user.role,
                "csrf_token": request.session["csrf_token"],
            }
        )

    @app.api_route(
        "/v1/admin/{admin_path:path}",
        methods=["GET", "POST", "PATCH", "DELETE", "PUT", "OPTIONS", "HEAD"],
    )
    async def proxy_admin(request: Request, admin_path: str) -> Response:
        user = _session_identity(request, settings.users_file)
        if user is None:
            return _error(401, "AUTHENTICATION_REQUIRED", "Authentication is required.")
        actor, role = user.username, user.role

        minimum_role, path_known = required_role(admin_path, request.method)
        if not path_known:
            return _error(404, "ADMIN_ROUTE_NOT_ALLOWED", "Admin route is not allowed.")
        if minimum_role is None:
            return _error(405, "ADMIN_METHOD_NOT_ALLOWED", "Admin method is not allowed.")
        if request.method in MUTATION_METHODS:
            rejected = _verify_mutation(request, settings)
            if rejected is not None:
                return rejected
        if not role_allows(role, minimum_role):
            return _error(403, "ROLE_FORBIDDEN", "This role cannot perform the action.")

        request_id = request.state.request_id
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                declared_length = settings.max_request_body_bytes + 1
            if declared_length < 0 or declared_length > settings.max_request_body_bytes:
                return _error(
                    413,
                    "REQUEST_BODY_TOO_LARGE",
                    "The Admin request body is too large.",
                    request_id=request_id,
                    headers={"X-Request-ID": str(request_id)},
                )
        body = await request.body()
        if len(body) > settings.max_request_body_bytes:
            return _error(
                413,
                "REQUEST_BODY_TOO_LARGE",
                "The Admin request body is too large.",
                request_id=request_id,
                headers={"X-Request-ID": str(request_id)},
            )
        target = _raw_target(request)
        outbound_headers = signed_headers(
            method=request.method,
            path_with_raw_query=target,
            body=body,
            actor=actor,
            role=role,
            timestamp=int(clock()),
            request_id=request_id,
            service_token=settings.backend_service_token,
            signing_secret=settings.backend_signing_secret,
        )
        for header_name in ("accept", "content-type"):
            value = request.headers.get(header_name)
            if value:
                outbound_headers[header_name] = value

        try:
            upstream = await backend_client.request(
                request.method,
                target,
                headers=outbound_headers,
                content=body,
            )
        except httpx2.HTTPError:
            return _error(
                503,
                "BACKEND_UNAVAILABLE",
                "The Snapshot Backend is unavailable.",
                retryable=True,
                request_id=request_id,
                headers={"X-Request-ID": str(request_id)},
            )

        if upstream.status_code >= 400:
            try:
                upstream_error = upstream.json()
            except (ValueError, UnicodeError):
                upstream_error = {}
            if not isinstance(upstream_error, dict):
                upstream_error = {}
            reason = upstream_error.get("reason")
            detail = upstream_error.get("detail")
            retryable = upstream_error.get("retryable")
            return _error(
                upstream.status_code,
                reason if isinstance(reason, str) else "BACKEND_REQUEST_FAILED",
                detail if isinstance(detail, str) else "The Snapshot Backend rejected the request.",
                retryable=retryable if isinstance(retryable, bool) else upstream.status_code >= 500,
                request_id=request_id,
                headers={"X-Request-ID": str(request_id)},
            )

        response_headers: dict[str, str] = {}
        for header_name in ("content-type", "etag", "last-modified"):
            value = upstream.headers.get(header_name)
            if value:
                response_headers[header_name] = value
        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            headers={**response_headers, "X-Request-ID": str(request_id)},
        )

    @app.get("/styles.css", include_in_schema=False)
    async def styles() -> FileResponse:
        return FileResponse(STATIC_DIR / "styles.css", media_type="text/css")

    @app.get("/app.js", include_in_schema=False)
    async def script() -> FileResponse:
        return FileResponse(STATIC_DIR / "app.js", media_type="text/javascript")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

    return app


def create_app_from_environment() -> FastAPI:
    return create_app(AdminWebSettings.from_environment())
