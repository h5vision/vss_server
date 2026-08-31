"""Independent Admin Web Server and BFF Proxy for Port 4180."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx2
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).parent.resolve()
BACKEND_BASE_URL = os.getenv("SNAPSHOT_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_PORT = int(os.getenv("ADMIN_WEB_PORT", "4180"))
ADMIN_HOST = os.getenv("ADMIN_WEB_HOST", "0.0.0.0")


def create_admin_web_app(
    *,
    backend_base_url: str = BACKEND_BASE_URL,
    backend_transport: httpx2.BaseTransport | None = None,
) -> FastAPI:
    """Create the standalone Admin Web FastAPI application."""
    app = FastAPI(title="Vision Snapshot Admin Web", docs_url=None, redoc_url=None)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # HTTP Client for proxying to Snapshot Backend
    http_client = httpx2.Client(
        base_url=backend_base_url,
        transport=backend_transport,
        timeout=httpx2.Timeout(connect=3.0, read=30.0, write=10.0, pool=3.0),
        trust_env=False,
    )

    @app.get("/health")
    async def health_check():
        return {"status": "ok", "service": "admin_web", "port": ADMIN_PORT}

    # BFF Reverse Proxy for /v1/admin/*
    @app.api_route("/v1/admin/{path:path}", methods=["GET", "POST", "PATCH", "DELETE", "PUT"])
    async def proxy_admin_api(request: Request, path: str):
        target_url = f"/v1/admin/{path}"
        headers = dict(request.headers)
        # Remove host header so httpx sets the proper backend host
        headers.pop("host", None)

        body = await request.body()
        params = list(request.query_params.multi_items())

        try:
            upstream_resp = http_client.request(
                method=request.method,
                url=target_url,
                params=params,
                headers=headers,
                content=body if body else None,
            )
            response_headers = dict(upstream_resp.headers)
            # Remove transfer-encoding and content-encoding if hop-by-hop
            response_headers.pop("content-length", None)
            response_headers.pop("content-encoding", None)

            return Response(
                content=upstream_resp.content,
                status_code=upstream_resp.status_code,
                headers=response_headers,
                media_type=upstream_resp.headers.get("content-type"),
            )
        except httpx2.HTTPError:
            err_payload = {
                "reason": "BACKEND_UNAVAILABLE",
                "detail": f"백엔드 서버({backend_base_url})에 연결할 수 없습니다.",
                "retryable": True,
            }
            return Response(
                content=json.dumps(err_payload, ensure_ascii=False),
                status_code=503,
                media_type="application/json",
            )

    # Serve Root index.html
    @app.get("/")
    async def serve_index():
        return FileResponse(STATIC_DIR / "index.html")

    # Serve Static Assets (styles.css, app.js)
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

    return app


app = create_admin_web_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("admin_web.server:app", host=ADMIN_HOST, port=ADMIN_PORT, reload=True)
