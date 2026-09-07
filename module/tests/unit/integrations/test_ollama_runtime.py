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


def test_runtime_models_reports_running_and_stopped_installed_models() -> None:
    seen_paths: list[str] = []

    def ollama(request: httpx2.Request) -> httpx2.Response:
        seen_paths.append(request.url.path)
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
        return httpx2.Response(404)

    client = OllamaRuntimeClient(
        base_url="http://ollama.test:11434",
        transport=httpx2.MockTransport(ollama),
    )
    try:
        result = client.runtime_models()
    finally:
        client.close()

    assert seen_paths == ["/api/tags", "/api/ps"]
    assert result.available is True
    assert result.installed_model_names == ("bge-m3:latest", "qwen3.8:27b")
    assert result.model_names == ("bge-m3:latest",)
    assert result.stopped_model_names == ("qwen3.8:27b",)


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


def test_load_model_preloads_installed_stopped_model_and_pins_it_resident() -> None:
    seen: list[tuple[str, str]] = []
    running = False

    def ollama(request: httpx2.Request) -> httpx2.Response:
        nonlocal running
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3.8:27b"}]})
        if request.url.path == "/api/ps":
            models = [{"name": "qwen3.8:27b"}] if running else []
            return httpx2.Response(200, json={"models": models})
        if request.url.path == "/api/generate":
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "model": "qwen3.8:27b",
                "stream": False,
                "keep_alive": -1,
            }
            running = True
            return httpx2.Response(200, json={"model": "qwen3.8:27b", "response": "", "done": True})
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
    assert seen == [
        ("GET", "/api/tags"),
        ("GET", "/api/ps"),
        ("POST", "/api/generate"),
        ("GET", "/api/ps"),
    ]


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
        if request.url.path == "/api/generate":
            running = True
            return httpx2.Response(200, json={"model": "qwen3.8:27b", "response": "", "done": True})
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
        if request.url.path == "/api/generate":
            generate_count += 1
            time.sleep(0.05)
            running = True
            return httpx2.Response(200, json={"model": "qwen3.8:27b", "response": "", "done": True})
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
