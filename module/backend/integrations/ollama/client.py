"""Runtime observability and lifecycle control for locally managed Ollama models."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

import httpx2

from backend.core.config import Settings

logger = logging.getLogger(__name__)
_UPSTREAM_REASON_LIMIT = 240
_SENSITIVE_REASON_VALUE = re.compile(
    r"(?i)\b(token|password|secret|authorization|api[_-]?key)\s*[:=]\s*([^\s,;]+)"
)


def _sanitized_upstream_reason(response: httpx2.Response) -> str | None:
    """Extracts a bounded Ollama error reason without logging the full response body."""
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("error", "message", "detail"):
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        reason = " ".join(value.split())
        reason = _SENSITIVE_REASON_VALUE.sub(r"\1=<redacted>", reason)
        return reason[:_UPSTREAM_REASON_LIMIT]
    return None


@dataclass(frozen=True, slots=True)
class OllamaRuntimeSnapshot:
    """Safe runtime projection exposed to the Admin layer."""

    available: bool
    model_names: tuple[str, ...]
    installed_model_names: tuple[str, ...] = ()
    auto_up_model_names: tuple[str, ...] = ()

    @property
    def stopped_model_names(self) -> tuple[str, ...]:
        running = set(self.model_names)
        return tuple(name for name in self.installed_model_names if name not in running)


@dataclass(frozen=True, slots=True)
class OllamaModelLoadResult:
    """Result of an explicit model Up/preload request."""

    model_name: str
    already_running: bool


@dataclass(frozen=True, slots=True)
class OllamaModelUnloadResult:
    """Result of an explicit model Down/unload request."""

    model_name: str
    already_stopped: bool
    auto_up_disabled: bool


@dataclass(frozen=True, slots=True)
class OllamaModelReloadResult:
    """Result of an explicit model Reload request."""

    model_name: str
    was_running: bool


@dataclass(frozen=True, slots=True)
class OllamaAutoUpPolicyResult:
    """Result of changing the process-local Auto Up policy for one model."""

    model_name: str
    enabled: bool
    loaded_now: bool


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
    """Observes installed/resident models and controls their Ollama lifecycle."""

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
        # GPU lifecycle mutations must not race each other. Auto Up uses the same
        # lock, so a manual Down/Reload cannot be interleaved with an automatic Up.
        self._control_lock = threading.Lock()
        self._policy_lock = threading.Lock()
        self._auto_up_models: set[str] = set()

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
        """Returns installed/resident models and current process-local Auto Up policy."""
        installed = self._get_model_names("api/tags")
        if installed is None:
            return self._unavailable()
        running = self._get_model_names("api/ps")
        if running is None:
            return self._unavailable()
        with self._policy_lock:
            auto_up = tuple(name for name in installed if name in self._auto_up_models)
        return OllamaRuntimeSnapshot(
            available=True,
            model_names=running,
            installed_model_names=installed,
            auto_up_model_names=auto_up,
        )

    def auto_up_model_names(self) -> tuple[str, ...]:
        """Returns the current process-local Auto Up model policy."""
        with self._policy_lock:
            return tuple(sorted(self._auto_up_models))

    def load_model(self, model_name: str) -> OllamaModelLoadResult:
        """Up/preloads one installed model and pins it resident."""
        with self._control_lock:
            return self._load_model_locked(model_name)

    def unload_model(self, model_name: str) -> OllamaModelUnloadResult:
        """Down/unloads one installed model and disables Auto Up for that model."""
        name = self._normalized_model_name(model_name)
        with self._policy_lock:
            auto_up_disabled = name in self._auto_up_models
            self._auto_up_models.discard(name)

        with self._control_lock:
            runtime = self._require_installed_runtime(name)
            if name not in runtime.model_names:
                return OllamaModelUnloadResult(
                    model_name=name,
                    already_stopped=True,
                    auto_up_disabled=auto_up_disabled,
                )
            self._unload_model_locked(name)
            return OllamaModelUnloadResult(
                model_name=name,
                already_stopped=False,
                auto_up_disabled=auto_up_disabled,
            )

    def reload_model(self, model_name: str) -> OllamaModelReloadResult:
        """Reloads one installed model while preserving its Auto Up policy."""
        name = self._normalized_model_name(model_name)
        with self._control_lock:
            runtime = self._require_installed_runtime(name)
            was_running = name in runtime.model_names
            if was_running:
                self._unload_model_locked(name)
            self._load_model_locked(name)
            return OllamaModelReloadResult(model_name=name, was_running=was_running)

    def set_auto_up(self, model_name: str, enabled: bool) -> OllamaAutoUpPolicyResult:
        """Changes Auto Up policy; enabling first proves the model can be made resident."""
        name = self._normalized_model_name(model_name)
        if not enabled:
            with self._policy_lock:
                self._auto_up_models.discard(name)
            return OllamaAutoUpPolicyResult(model_name=name, enabled=False, loaded_now=False)

        loaded_now = False
        with self._control_lock:
            runtime = self._require_installed_runtime(name)
            if name not in runtime.model_names:
                self._load_model_locked(name)
                loaded_now = True
            with self._policy_lock:
                self._auto_up_models.add(name)
        return OllamaAutoUpPolicyResult(model_name=name, enabled=True, loaded_now=loaded_now)

    def ensure_auto_up_model(self, model_name: str) -> OllamaModelLoadResult | None:
        """Restores one Auto Up model if it is installed but no longer resident."""
        name = self._normalized_model_name(model_name)
        with self._policy_lock:
            if name not in self._auto_up_models:
                return None

        with self._control_lock:
            with self._policy_lock:
                if name not in self._auto_up_models:
                    return None
            runtime = self.runtime_models()
            if not runtime.available or name not in runtime.installed_model_names:
                return None
            if name in runtime.model_names:
                return None
            return self._load_model_locked(name)

    def _load_model_locked(self, model_name: str) -> OllamaModelLoadResult:
        name = self._normalized_model_name(model_name)
        runtime = self._require_installed_runtime(name)
        if name in runtime.model_names:
            return OllamaModelLoadResult(model_name=name, already_running=True)

        capabilities = self._model_capabilities(name)
        response: httpx2.Response
        try:
            if (
                capabilities is not None
                and "embedding" in capabilities
                and "completion" not in capabilities
            ):
                response = self._client.post(
                    "api/embed",
                    json={"model": name, "input": "", "keep_alive": -1},
                    timeout=self._load_timeout,
                )
            else:
                response = self._client.post(
                    "api/generate",
                    json={"model": name, "stream": False, "keep_alive": -1},
                    timeout=self._load_timeout,
                )
                # Older Ollama versions may omit capabilities from `/api/show`.
                # If generate explicitly rejects an embedding-only model, use the
                # embedding API as the capability-safe preload path.
                if response.status_code == 400 and self._generate_is_unsupported(response):
                    response = self._client.post(
                        "api/embed",
                        json={"model": name, "input": "", "keep_alive": -1},
                        timeout=self._load_timeout,
                    )
        except httpx2.HTTPError as exc:
            logger.warning(
                "Ollama model lifecycle request unavailable operation=up model=%s error_type=%s",
                name,
                type(exc).__name__,
            )
            raise OllamaRuntimeError(
                status_code=503,
                reason="OLLAMA_MODEL_LOAD_UNAVAILABLE",
                detail="Ollama could not be reached while loading the model.",
                retryable=True,
            ) from exc

        if response.status_code != 200:
            logger.warning(
                "Ollama model lifecycle request rejected operation=up model=%s "
                "upstream_status=%s upstream_reason=%s",
                name,
                response.status_code,
                _sanitized_upstream_reason(response) or "<unavailable>",
            )
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

    def _unload_model_locked(self, model_name: str) -> None:
        try:
            response = self._client.post(
                "api/generate",
                json={"model": model_name, "stream": False, "keep_alive": 0},
                timeout=self._load_timeout,
            )
        except httpx2.HTTPError as exc:
            logger.warning(
                "Ollama model lifecycle request unavailable operation=down model=%s error_type=%s",
                model_name,
                type(exc).__name__,
            )
            raise OllamaRuntimeError(
                status_code=503,
                reason="OLLAMA_MODEL_UNLOAD_UNAVAILABLE",
                detail="Ollama could not be reached while unloading the model.",
                retryable=True,
            ) from exc

        if response.status_code != 200:
            logger.warning(
                "Ollama model lifecycle request rejected operation=down model=%s "
                "upstream_status=%s upstream_reason=%s",
                model_name,
                response.status_code,
                _sanitized_upstream_reason(response) or "<unavailable>",
            )
            raise OllamaRuntimeError(
                status_code=502,
                reason="OLLAMA_MODEL_UNLOAD_FAILED",
                detail="Ollama rejected the model unload request.",
                retryable=response.status_code >= 500,
            )
        if not self._wait_until_stopped(model_name):
            raise OllamaRuntimeError(
                status_code=502,
                reason="OLLAMA_MODEL_STILL_RESIDENT",
                detail="Ollama accepted the unload request but the model is still resident.",
                retryable=True,
            )

    def _require_installed_runtime(self, model_name: str) -> OllamaRuntimeSnapshot:
        runtime = self.runtime_models()
        if not runtime.available:
            raise OllamaRuntimeError(
                status_code=503,
                reason="OLLAMA_UNAVAILABLE",
                detail="Ollama runtime is unavailable.",
                retryable=True,
            )
        if model_name not in runtime.installed_model_names:
            raise OllamaRuntimeError(
                status_code=404,
                reason="OLLAMA_MODEL_NOT_INSTALLED",
                detail="The requested Ollama model is not installed on this runtime.",
                retryable=False,
            )
        return runtime

    def _model_capabilities(self, model_name: str) -> frozenset[str] | None:
        try:
            response = self._client.post("api/show", json={"model": model_name})
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
        raw = payload.get("capabilities")
        if not isinstance(raw, list):
            return None
        return frozenset(
            item.strip().lower()
            for item in raw
            if isinstance(item, str) and item.strip()
        )

    @staticmethod
    def _generate_is_unsupported(response: httpx2.Response) -> bool:
        try:
            payload = response.json()
        except ValueError:
            return False
        if not isinstance(payload, dict):
            return False
        error = payload.get("error")
        return isinstance(error, str) and "does not support generate" in error.lower()

    def _wait_until_resident(self, model_name: str) -> bool:
        for attempt in range(5):
            names = self._get_model_names("api/ps")
            if names is not None and model_name in names:
                return True
            if attempt < 4:
                time.sleep(0.5)
        return False

    def _wait_until_stopped(self, model_name: str) -> bool:
        for attempt in range(5):
            names = self._get_model_names("api/ps")
            if names is not None and model_name not in names:
                return True
            if attempt < 4:
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
    def _normalized_model_name(model_name: str) -> str:
        return model_name.strip()

    @staticmethod
    def _unavailable() -> OllamaRuntimeSnapshot:
        return OllamaRuntimeSnapshot(available=False, model_names=(), installed_model_names=())
