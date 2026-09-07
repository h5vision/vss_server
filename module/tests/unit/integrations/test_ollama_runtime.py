from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx2
import pytest

from backend.integrations.ollama.client import OllamaRuntimeClient, OllamaRuntimeError


def test_running_models_reports_unique_resident_model_names() -> None:
    seen_paths: list[str] = []

    def ollama(request: httpx2.Request) -> httpx2.Response:
        seen_paths.append(request.url.path)
        return httpx2.Response(
            200,
            json={
                "models": [
                    {"name": "bge-m3:latest", "model": "bge-m3:latest"},
                    {"name": "qwen3.8:27b", "model": "qwen3.8:27b"},
                    {"name": "bge-m3:latest", "model": "bge-m3:latest"},
                ]
            },
        )

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.running_models()
    finally:
        client.close()

    assert seen_paths == ["/api/ps"]
    assert result.available is True
    assert result.model_names == ("bge-m3:latest", "qwen3.8:27b")


def test_runtime_models_reports_running_stopped_and_auto_up_models() -> None:
    def ollama(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/tags":
            return httpx2.Response(
                200,
                json={
                    "models": [
                        {"name": "bge-m3:latest"},
                        {"name": "qwen3.8:27b"},
                        {"name": "qwen3.8:27b"},
                    ]
                },
            )
        if request.url.path == "/api/ps":
            return httpx2.Response(200, json={"models": [{"name": "bge-m3:latest"}]})
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion"]})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        client.set_auto_up("bge-m3:latest", True)
        result = client.runtime_models()
    finally:
        client.close()

    assert result.available is True
    assert result.installed_model_names == ("bge-m3:latest", "qwen3.8:27b")
    assert result.model_names == ("bge-m3:latest",)
    assert result.stopped_model_names == ("qwen3.8:27b",)
    assert result.auto_up_model_names == ("bge-m3:latest",)


def test_running_models_degrades_to_empty_when_ollama_is_unavailable() -> None:
    def unavailable(_request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("ollama is down")

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(unavailable),
    )
    try:
        result = client.running_models()
    finally:
        client.close()

    assert result.available is False
    assert result.model_names == ()


def test_running_models_degrades_to_empty_on_malformed_payload() -> None:
    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(
            lambda _request: httpx2.Response(200, json={"models": "not-a-list"})
        ),
    )
    try:
        result = client.running_models()
    finally:
        client.close()

    assert result.available is False
    assert result.model_names == ()


def test_up_completion_model_uses_generate_and_pins_it_resident() -> None:
    seen: list[tuple[str, str, dict | None]] = []
    running = False

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running
        payload = json.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, payload))
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3.8:27b"}] if running else []},
            )
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/generate":
            running = True
            return httpx2.Response(200, json={"model": "qwen3.8:27b", "done": True})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.load_model(" qwen3.8:27b ")
    finally:
        client.close()

    assert result.model_name == "qwen3.8:27b"
    assert result.already_running is False
    assert ("POST", "/api/show", {"model": "qwen3.8:27b"}) in seen
    assert (
        "POST",
        "/api/generate",
        {"model": "qwen3.8:27b", "stream": False, "keep_alive": -1},
    ) in seen


def test_up_embedding_only_model_uses_embed_instead_of_generate() -> None:
    running = False
    seen_paths: list[str] = []
    embed_payloads: list[dict] = []

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running
        seen_paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "bge-m3:latest"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "bge-m3:latest"}] if running else []},
            )
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["embedding"]})
        if request.url.path == "/api/embed":
            embed_payloads.append(json.loads(request.content))
            running = True
            return httpx2.Response(200, json={"embeddings": [[]]})
        return httpx2.Response(500)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.load_model("bge-m3:latest")
    finally:
        client.close()

    assert result.already_running is False
    assert "/api/generate" not in seen_paths
    assert embed_payloads == [
        {"model": "bge-m3:latest", "input": "", "keep_alive": -1}
    ]


def test_up_falls_back_to_embed_when_generate_rejects_embedding_model() -> None:
    running = False
    seen_paths: list[str] = []

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running
        seen_paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "legacy-embed:latest"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "legacy-embed:latest"}] if running else []},
            )
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={})
        if request.url.path == "/api/generate":
            return httpx2.Response(400, json={"error": "this model does not support generate"})
        if request.url.path == "/api/embed":
            running = True
            return httpx2.Response(200, json={"embeddings": [[]]})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.load_model("legacy-embed:latest")
    finally:
        client.close()

    assert result.already_running is False
    assert seen_paths.count("/api/generate") == 1
    assert seen_paths.count("/api/embed") == 1


def test_load_model_is_idempotent_when_model_is_already_running() -> None:
    seen_paths: list[str] = []

    def ollama(request: httpx2.Request) -> httpx2.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "bge-m3:latest"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(200, json={"models": [{"name": "bge-m3:latest"}]})
        return httpx2.Response(500)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.load_model("bge-m3:latest")
    finally:
        client.close()

    assert result.already_running is True
    assert seen_paths == ["/api/tags", "/api/ps"]


def test_load_model_rejects_models_that_are_not_installed() -> None:
    def ollama(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "bge-m3:latest"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(200, json={"models": []})
        return httpx2.Response(500)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        with pytest.raises(OllamaRuntimeError) as caught:
            client.load_model("missing:latest")
    finally:
        client.close()

    assert caught.value.status_code == 404
    assert caught.value.reason == "OLLAMA_MODEL_NOT_INSTALLED"
    assert caught.value.retryable is False


def test_down_running_model_unloads_and_disables_auto_up() -> None:
    running = True
    unload_payloads: list[dict] = []

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3.8:27b"}] if running else []},
            )
        if request.url.path == "/api/generate":
            payload = json.loads(request.content)
            if payload.get("keep_alive") == 0:
                unload_payloads.append(payload)
                running = False
                return httpx2.Response(200, json={"done": True})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        client.set_auto_up("qwen3.8:27b", True)
        result = client.unload_model("qwen3.8:27b")
        auto_up = client.auto_up_model_names()
    finally:
        client.close()

    assert result.already_stopped is False
    assert result.auto_up_disabled is True
    assert auto_up == ()
    assert unload_payloads == [
        {"model": "qwen3.8:27b", "stream": False, "keep_alive": 0}
    ]


def test_down_is_idempotent_when_model_is_already_stopped() -> None:
    generate_calls = 0

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal generate_calls
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(200, json={"models": []})
        if request.url.path == "/api/generate":
            generate_calls += 1
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.unload_model("qwen3.8:27b")
    finally:
        client.close()

    assert result.already_stopped is True
    assert generate_calls == 0


def test_reload_running_model_downs_then_ups_and_preserves_auto_up() -> None:
    running = True
    lifecycle_keep_alive: list[int] = []

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3.8:27b"}] if running else []},
            )
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/generate":
            payload = json.loads(request.content)
            lifecycle_keep_alive.append(payload["keep_alive"])
            running = payload["keep_alive"] != 0
            return httpx2.Response(200, json={"done": True})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        client.set_auto_up("qwen3.8:27b", True)
        result = client.reload_model("qwen3.8:27b")
        auto_up = client.auto_up_model_names()
    finally:
        client.close()

    assert result.was_running is True
    assert lifecycle_keep_alive == [0, -1]
    assert auto_up == ("qwen3.8:27b",)


def test_enable_auto_up_loads_stopped_model_before_policy_is_enabled() -> None:
    running = False

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3.8:27b"}] if running else []},
            )
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/generate":
            running = True
            return httpx2.Response(200, json={"done": True})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.set_auto_up("qwen3.8:27b", True)
        auto_up = client.auto_up_model_names()
    finally:
        client.close()

    assert result.enabled is True
    assert result.loaded_now is True
    assert auto_up == ("qwen3.8:27b",)


def test_auto_up_restores_model_after_external_dropout() -> None:
    running = True
    up_count = 0

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running, up_count
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3.8:27b"}] if running else []},
            )
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/generate":
            up_count += 1
            running = True
            return httpx2.Response(200, json={"done": True})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        client.set_auto_up("qwen3.8:27b", True)
        running = False
        restored = client.ensure_auto_up_model("qwen3.8:27b")
    finally:
        client.close()

    assert restored is not None
    assert restored.model_name == "qwen3.8:27b"
    assert restored.already_running is False
    assert up_count == 1


def test_auto_up_does_nothing_after_policy_is_disabled() -> None:
    running = True
    generate_count = 0

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal generate_count
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3.8:27b"}] if running else []},
            )
        if request.url.path == "/api/generate":
            generate_count += 1
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        client.set_auto_up("qwen3.8:27b", True)
        client.set_auto_up("qwen3.8:27b", False)
        result = client.ensure_auto_up_model("qwen3.8:27b")
    finally:
        client.close()

    assert result is None
    assert generate_count == 0


def test_load_model_retries_transient_post_load_residency_checks() -> None:
    running = False
    post_load_checks = 0

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running, post_load_checks
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            if not running:
                return httpx2.Response(200, json={"models": []})
            post_load_checks += 1
            if post_load_checks < 3:
                raise httpx2.ReadTimeout("transient ps delay")
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/generate":
            running = True
            return httpx2.Response(200, json={"done": True})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.load_model("qwen3.8:27b")
    finally:
        client.close()

    assert result.already_running is False
    assert post_load_checks == 3


def test_concurrent_load_requests_are_serialized_and_generate_only_once() -> None:
    running = False
    generate_count = 0

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running, generate_count
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            return httpx2.Response(
                200,
                json={"models": [{"name": "qwen3.8:27b"}] if running else []},
            )
        if request.url.path == "/api/show":
            return httpx2.Response(200, json={"capabilities": ["completion"]})
        if request.url.path == "/api/generate":
            generate_count += 1
            time.sleep(0.05)
            running = True
            return httpx2.Response(200, json={"done": True})
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(client.load_model, ["qwen3.8:27b", "qwen3.8:27b"]))
    finally:
        client.close()

    assert generate_count == 1
    assert sorted(result.already_running for result in results) == [False, True]
