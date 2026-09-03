"""GitHub PR and GitLab MR read-only provider client contracts."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx2
import pytest

from backend.integrations.change_requests.errors import (
    ChangeRequestProviderAuthFailed,
)
from backend.integrations.change_requests.github import GitHubChangeRequestClient
from backend.integrations.change_requests.gitlab import GitLabChangeRequestClient


def test_github_maps_open_and_merged_pull_requests_and_follows_link_header() -> None:
    repository_id = uuid4()
    requests: list[httpx2.Request] = []

    def transport(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer github-secret"
        assert request.headers["accept"] == "application/vnd.github+json"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        page = request.url.params.get("page")
        if page == "1":
            return httpx2.Response(
                200,
                headers={
                    "Link": (
                        '<https://api.github.com/repos/h5vision/vss_server/pulls?page=2>; '
                        'rel="next"'
                    )
                },
                json=[
                    {
                        "number": 10,
                        "title": "Open\ncontext\u001b work",
                        "state": "open",
                        "updated_at": "2026-09-02T01:00:00Z",
                        "merged_at": None,
                        "merge_commit_sha": "9" * 40,
                        "base": {"ref": "main", "sha": "1" * 40},
                        "head": {"ref": "feature/context", "sha": "2" * 40},
                    }
                ],
            )
        return httpx2.Response(
            200,
            json=[
                {
                    "number": 9,
                    "title": "Merged context work",
                    "state": "closed",
                    "updated_at": "2026-09-01T01:00:00Z",
                    "merged_at": "2026-09-01T02:00:00Z",
                    "merge_commit_sha": "3" * 40,
                    "base": {"ref": "main", "sha": "1" * 40},
                    "head": {"ref": "feature/merged", "sha": "4" * 40},
                }
            ],
        )

    with GitHubChangeRequestClient(
        base_url="https://api.github.com",
        token="github-secret",
        api_version="2026-03-10",
        max_pages=5,
        transport=httpx2.MockTransport(transport),
    ) as client:
        observations = client.list_change_requests(
            repository_id=repository_id,
            canonical_name="h5vision/vss_server",
        )

    assert len(requests) == 2
    assert [item.external_number for item in observations] == [10, 9]
    assert observations[0].base_ref == "refs/heads/main"
    assert observations[0].head_ref == "refs/heads/feature/context"
    assert observations[0].title == "Open context work"
    assert observations[0].merge_sha is None
    assert observations[0].state == "open"
    assert observations[1].state == "merged"
    assert observations[1].merge_sha == "3" * 40


def test_gitlab_fetches_detail_when_list_diff_refs_are_missing() -> None:
    repository_id = uuid4()
    requests: list[httpx2.Request] = []

    def transport(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        assert request.headers["private-token"] == "gitlab-secret"
        if request.url.path.endswith("/merge_requests"):
            assert "%2F" in str(request.url)
            return httpx2.Response(
                200,
                headers={"X-Next-Page": ""},
                json=[
                    {
                        "iid": 7,
                        "title": "MR context",
                        "state": "opened",
                        "target_branch": "main",
                        "source_branch": "feature/context",
                        "updated_at": "2026-09-02T01:00:00Z",
                        "merged_at": None,
                        "merge_commit_sha": None,
                        "diff_refs": None,
                    }
                ],
            )
        return httpx2.Response(
            200,
            json={
                "iid": 7,
                "title": "MR context",
                "state": "opened",
                "target_branch": "main",
                "source_branch": "feature/context",
                "updated_at": "2026-09-02T01:00:00Z",
                "merged_at": None,
                "merge_commit_sha": None,
                "diff_refs": {
                    "base_sha": "5" * 40,
                    "head_sha": "6" * 40,
                    "start_sha": "5" * 40,
                },
            },
        )

    with GitLabChangeRequestClient(
        base_url="https://gitlab.example/api/v4",
        token="gitlab-secret",
        max_pages=5,
        transport=httpx2.MockTransport(transport),
    ) as client:
        observations = client.list_change_requests(
            repository_id=repository_id,
            canonical_name="h5vision/vss_server",
        )

    assert len(requests) == 2
    assert observations[0].external_number == 7
    assert observations[0].kind == "merge_request"
    assert observations[0].state == "open"
    assert observations[0].base_sha == "5" * 40
    assert observations[0].head_sha == "6" * 40


def test_provider_auth_failure_never_exposes_token_or_response_body() -> None:
    def transport(_request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(401, content=json.dumps({"token": "upstream-secret"}))

    with GitHubChangeRequestClient(
        base_url="https://api.github.com",
        token="local-secret",
        api_version="2026-03-10",
        max_pages=1,
        transport=httpx2.MockTransport(transport),
    ) as client:
        with pytest.raises(ChangeRequestProviderAuthFailed) as error:
            client.list_change_requests(
                repository_id=uuid4(),
                canonical_name="h5vision/vss_server",
            )

    assert "local-secret" not in str(error.value)
    assert "upstream-secret" not in str(error.value)
