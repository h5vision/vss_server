"""Async transparent relay client for the VSS-owned Chat HTTP contract."""

from __future__ import annotations

from typing import Any

import httpx2

from backend.core.config import Settings


class VssChatRelayClient:
    """Open VSS Chat responses without buffering SSE before the caller receives it."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 300.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["X-VSS-Token"] = token
        timeout = httpx2.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._client = httpx2.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        transport: httpx2.BaseTransport | None = None,
    ) -> VssChatRelayClient:
        token = settings.vss_token.get_secret_value() if settings.vss_token else None
        return cls(
            base_url=str(settings.vss_base_url),
            token=token,
            connect_timeout_seconds=settings.vss_connect_timeout_seconds,
            read_timeout_seconds=settings.snapshot_chat_stream_read_timeout_seconds,
            transport=transport,
        )

    async def open_chat(self, payload: dict[str, Any]) -> httpx2.Response:
        """Return the upstream response with its body left open for streaming."""
        accept = "text/event-stream" if payload.get("stream") else "application/json"
        request = self._client.build_request(
            "POST",
            "v1/chat",
            json=payload,
            headers={"Accept": accept},
        )
        return await self._client.send(request, stream=True)

    async def aclose(self) -> None:
        await self._client.aclose()
