from __future__ import annotations

import httpx2
import pytest

from backend.features.repositories.discovery import (
    RepositoryDiscoveryError,
    discover_github_repository,
    parse_github_repository_url,
)


def test_parse_github_repository_url_accepts_canonical_variants() -> None:
    assert parse_github_repository_url("https://github.com/h5vision/vision.git") == (
        "h5vision",
        "vision",
    )
    assert parse_github_repository_url("http://www.github.com/h5vision/vss_server") == (
        "h5vision",
        "vss_server",
    )


def test_parse_github_repository_url_rejects_other_hosts_and_nested_paths() -> None:
    with pytest.raises(RepositoryDiscoveryError) as other_host:
        parse_github_repository_url("https://example.com/h5vision/vision.git")
    assert other_host.value.reason == "REPOSITORY_DISCOVERY_UNSUPPORTED_PROVIDER"

    with pytest.raises(RepositoryDiscoveryError) as nested:
        parse_github_repository_url("https://github.com/h5vision/vision/issues")
    assert nested.value.reason == "REPOSITORY_DISCOVERY_UNSUPPORTED_URL"


@pytest.mark.anyio
async def test_discover_github_repository_returns_canonical_metadata() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.host == "api.github.com"
        assert request.url.path == "/repos/h5vision/vision"
        return httpx2.Response(
            200,
            json={
                "id": 1300016567,
                "name": "vision",
                "full_name": "h5vision/vision",
                "clone_url": "https://github.com/h5vision/vision.git",
                "html_url": "https://github.com/h5vision/vision",
                "default_branch": "frontend",
                "visibility": "public",
                "private": False,
            },
        )

    result = await discover_github_repository(
        "https://github.com/h5vision/vision.git",
        transport=httpx2.MockTransport(handler),
    )

    assert result == {
        "provider": "github",
        "provider_repository_id": 1300016567,
        "canonical_name": "h5vision/vision",
        "display_name": "vision",
        "remote_url": "https://github.com/h5vision/vision.git",
        "html_url": "https://github.com/h5vision/vision",
        "default_branch_ref": "refs/heads/frontend",
        "visibility": "public",
    }


@pytest.mark.anyio
async def test_discover_github_repository_maps_not_found_without_leaking_body() -> None:
    def handler(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"message": "private upstream detail"})

    with pytest.raises(RepositoryDiscoveryError) as exc_info:
        await discover_github_repository(
            "https://github.com/h5vision/missing.git",
            transport=httpx2.MockTransport(handler),
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.reason == "REPOSITORY_DISCOVERY_NOT_FOUND"
    assert "private upstream detail" not in exc_info.value.detail
