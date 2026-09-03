"""GitHub REST Pull Request reader."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote
from uuid import UUID

import httpx2
from pydantic import BaseModel, ConfigDict, ValidationError

from backend.features.change_requests.schemas import ChangeRequestObservationRequest
from backend.features.workspace_overlays.schemas import GitRevision
from backend.integrations.change_requests.base import ChangeRequestProviderHttpClient
from backend.integrations.change_requests.errors import (
    ChangeRequestProviderContractMismatch,
    ChangeRequestProviderPaginationLimit,
)


class _GitHubRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ref: str
    sha: GitRevision


class _GitHubPullRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    number: int
    title: str
    state: Literal["open", "closed"]
    updated_at: datetime
    merged_at: datetime | None
    merge_commit_sha: GitRevision | None
    base: _GitHubRef
    head: _GitHubRef


class GitHubChangeRequestClient(ChangeRequestProviderHttpClient):
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        api_version: str,
        max_pages: int,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 10.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": api_version,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        super().__init__(
            base_url=base_url,
            headers=headers,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            transport=transport,
        )
        self._max_pages = max_pages

    def list_change_requests(
        self,
        *,
        repository_id: UUID,
        canonical_name: str,
        remote_url: str | None = None,
    ) -> list[ChangeRequestObservationRequest]:
        repository_path = self._repository_path(canonical_name, remote_url)
        parts = repository_path.split("/")
        if len(parts) != 2:
            raise ChangeRequestProviderContractMismatch(
                "GitHub repository path must contain exactly owner and repository."
            )
        owner, repository = parts
        path = f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}/pulls"
        observed_at = datetime.now(timezone.utc)
        results: list[ChangeRequestObservationRequest] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                path,
                params={
                    "state": "all",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            try:
                payload = response.json()
                if not isinstance(payload, list):
                    raise ValueError
                pulls = [_GitHubPullRequest.model_validate(item) for item in payload]
            except (ValueError, ValidationError) as exc:
                raise ChangeRequestProviderContractMismatch(
                    "GitHub returned invalid Pull Request JSON data."
                ) from exc
            results.extend(
                self._observation(repository_id, pull, observed_at=observed_at)
                for pull in pulls
            )
            if 'rel="next"' not in response.headers.get("Link", ""):
                return results
            if page >= self._max_pages:
                raise ChangeRequestProviderPaginationLimit(
                    "GitHub Pull Request pagination exceeded the configured limit."
                )
            page += 1

    @staticmethod
    def _observation(
        repository_id: UUID,
        pull: _GitHubPullRequest,
        *,
        observed_at: datetime,
    ) -> ChangeRequestObservationRequest:
        merged = pull.merged_at is not None
        if merged and pull.merge_commit_sha is None:
            raise ChangeRequestProviderContractMismatch(
                "Merged GitHub Pull Request has no merge commit SHA."
            )
        return ChangeRequestObservationRequest(
            repository_id=repository_id,
            provider="github",
            external_number=pull.number,
            kind="pull_request",
            state="merged" if merged else pull.state,
            title=pull.title,
            base_ref=f"refs/heads/{pull.base.ref}",
            head_ref=f"refs/heads/{pull.head.ref}",
            base_sha=pull.base.sha,
            head_sha=pull.head.sha,
            merge_sha=pull.merge_commit_sha if merged else None,
            provider_updated_at=pull.updated_at,
            merged_at=pull.merged_at,
            observed_at=observed_at,
        )
