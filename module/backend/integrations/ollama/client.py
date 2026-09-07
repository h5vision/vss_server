"""Runtime control client for locally managed Ollama models."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx2

from backend.core.config import Settings


@dataclass(frozen=True, slots=True)
class OllamaRuntimeSnapshot:
    """Safe runtime projection exposed to the Admin layer."""

    available: bool
    model_names: tuple[str, ...]
    installed_model_names: tuple[str, ...] = ()

    @property
    def stopped_model_names(self) -> tuple[str, ...]:
        running = set(self.model_names)
        return tuple(name for name in self.installed_model_names if name not in running)


@dataclass(frozen=True, slots=True)
class OllamaModelLoadResult:
    """Result of an explicit Admin preload request."""

    model_name: str
    already_running: bool


class OllamaRuntimeError(RuntimeError):
    """Sanitized Ollama runtime-control failure safe for the Admin API."""

    def __init__(
        self,
        *,
        status_code: int,
        reason: str,
        detail: str,
        retryable: bool,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.reason = reason
        self.detail = detail
        self.retryable = retryable


class OllamaRuntimeClient:
    """Observes installed/resident Ollama models and preloads installed models."""

    def __init__(
        self,
        *,
        base_url: str,
        connect_timeout_seconds: float = 1.0,
        read_timeout_seconds: float = 2.0,
        load_timeout_seconds: float = 180.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        timeout = httpx2.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._load_timeout = httpx2.Timeout(
            connect=connect_timeout_seconds,
            read=load_timeout_seconds,
            write=load_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._client = httpx2.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Accept": "application/json"},
            timeout=timeout,
            transport=transport,
            trust_env=False,
        )
        # Loading multiple large models concurrently can cause VRAM churn or
        # unexpected evictions. Serialize explicit Admin preloads per process.
        self._load_lock = threading.Lock()

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
            load_timeout_seconds=settings.ollama_load_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def running_models(self) -> OllamaRuntimeSnapshot:
        """Returns names currently resident according to Ollama `/api/ps`."""
        names = self._get_model_names("api/ps")
        if names is None:
            return self._unavailable()
        return OllamaRuntimeSnapshot(available=True, model_names=names)

    def runtime_models(self) -> OllamaRuntimeSnapshot:
        """Returns installed models together with the currently resident subset."""
        installed = self._get_model_names("api/tags")
        if installed is None:
            return self._unavailable()
        running = self._get_model_names("api/ps")
        if running is None:
            return self._unavailable()
        return OllamaRuntimeSnapshot(
            available=True,
            model_names=running,
            installed_model_names=installed,
        )

    def load_model(self, model_name: str) -> OllamaModelLoadResult:
        """Preloads one installed model and pins it resident until explicitly unloaded.

        Ollama documents an empty `/api/generate` request as the preload operation;
        a negative `keep_alive` keeps the model resident. No inference prompt is sent.
        Explicit loads are serialized so multiple Admin tabs cannot race GPU loading.
        """
        with self._load_lock:
            return self._load_model_locked(model_name)

    def _load_model_locked(self, model_name: str) -> OllamaModelLoadResult:
        name = model_name.strip()
        runtime = self.runtime_models()
        if not runtime.available:
            raise OllamaRuntimeError(
                status_code=503,
                reason="OLLAMA_UNAVAILABLE",
                detail="Ollama runtime is unavailable.",
                retryable=True,
            )
        if name not in runtime.installed_model_names:
            raise OllamaRuntimeError(
                status_code=404,
                reason="OLLAMA_MODEL_NOT_INSTALLED",
                detail="The requested Ollama model is not installed on this runtime.",
                retryable=False,
            )
        if name in runtime.model_names:
            return OllamaModelLoadResult(model_name=name, already_running=True)

        try:
            response = self._client.post(
                "api/generate",
                json={"model": name, "stream": False, "keep_alive": -1},
                timeout=self._load_timeout,
            )
        except httpx2.HTTPError as exc:
            raise OllamaRuntimeError(
                status_code=503,
                reason="OLLAMA_MODEL_LOAD_UNAVAILABLE",
                detail="Ollama could not be reached while loading the model.",
                retryable=True,
            ) from exc

        if response.status_code != 200:
            raise OllamaRuntimeError(
                status_code=502,
                reason="OLLAMA_MODEL_LOAD_FAILED",
                detail="Ollama rejected the model load request.",
                retryable=response.status_code >= 500,
            )

        if not self._wait_until_resident(name):
            raise OllamaRuntimeError(
                status_code=502,
                reason="OLLAMA_MODEL_NOT_RESIDENT",
                detail="Ollama accepted the load request but the model is not resident.",
                retryable=True,
            )
        return OllamaModelLoadResult(model_name=name, already_running=False)

    def _wait_until_resident(self, model_name: str) -> bool:
        # A large model can briefly delay `/api/ps` immediately after the preload
        # response. Retry a few bounded times rather than reporting a false 502.
        for attempt in range(3):
            names = self._get_model_names("api/ps")
            if names is not None and model_name in names:
                return True
            if attempt < 2:
                time.sleep(0.5)
        return False

    def _get_model_names(self, path: str) -> tuple[str, ...] | None:
        try:
            response = self._client.get(path)
        except httpx2.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            return None

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
        return tuple(names)

    @staticmethod
    def _unavailable() -> OllamaRuntimeSnapshot:
        return OllamaRuntimeSnapshot(available=False, model_names=(), installed_model_names=())
