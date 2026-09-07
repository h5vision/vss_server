from __future__ import annotations

import httpx2

from backend.integrations.ollama.client import OllamaRuntimeClient


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
