"""Read-only client for Ollama's resident-model process endpoint."""

from __future__ import annotations

from dataclasses import dataclass

import httpx2

from backend.core.config import Settings


@dataclass(frozen=True, slots=True)
class OllamaRuntimeSnapshot:
    """Safe runtime projection exposed to the Admin layer."""

    available: bool
    model_names: tuple[str, ...]


class OllamaRuntimeClient:
    """Queries Ollama process state without loading, unloading, or generating."""

    def __init__(
        self,
        *,
        base_url: str,
        connect_timeout_seconds: float = 1.0,
        read_timeout_seconds: float = 2.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        timeout = httpx2.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._client = httpx2.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Accept": "application/json"},
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
    ) -> OllamaRuntimeClient:
        return cls(
            base_url=str(settings.ollama_base_url),
            connect_timeout_seconds=settings.ollama_connect_timeout_seconds,
            read_timeout_seconds=settings.ollama_read_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def running_models(self) -> OllamaRuntimeSnapshot:
        """Returns only names currently resident according to Ollama `/api/ps`.

        Runtime observability must not make the Admin console unavailable. Network,
        protocol, or payload failures therefore collapse to an unavailable empty
        snapshot instead of propagating upstream details.
        """
        try:
            response = self._client.get("api/ps")
        except httpx2.HTTPError:
            return self._unavailable()

        if response.status_code != 200:
            return self._unavailable()

        try:
            payload = response.json()
        except ValueError:
            return self._unavailable()
        if not isinstance(payload, dict):
            return self._unavailable()

        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            return self._unavailable()

        names: list[str] = []
        seen: set[str] = set()
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            raw_name = item.get("name") or item.get("model")
            if not isinstance(raw_name, str):
                continue
            name = raw_name.strip()
            if name and name not in seen:
                seen.add(name)
                names.append(name)

        return OllamaRuntimeSnapshot(available=True, model_names=tuple(names))

    @staticmethod
    def _unavailable() -> OllamaRuntimeSnapshot:
        return OllamaRuntimeSnapshot(available=False, model_names=())
