"""GitLab REST Merge Request reader."""

from __future__ import annotations

from datetime import datetime, timezone
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


class _GitLabDiffRefs(BaseModel):
    model_config = ConfigDict(extra="ignore")

    base_sha: GitRevision
    head_sha: GitRevision
    start_sha: GitRevision | None = None


class _GitLabMergeRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    iid: int
    title: str
    state: str
    target_branch: str
    source_branch: str
    updated_at: datetime
    merged_at: datetime | None
    merge_commit_sha: GitRevision | None = None
    squash_commit_sha: GitRevision | None = None
    diff_refs: _GitLabDiffRefs | None = None


class GitLabChangeRequestClient(ChangeRequestProviderHttpClient):
    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        max_pages: int,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 10.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["PRIVATE-TOKEN"] = token
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
        encoded_project = quote(repository_path, safe="")
        path = f"projects/{encoded_project}/merge_requests"
        observed_at = datetime.now(timezone.utc)
        results: list[ChangeRequestObservationRequest] = []
        page = 1
        while True:
            response = self._request(
                "GET",
                path,
                params={
                    "state": "all",
                    "order_by": "updated_at",
                    "sort": "desc",
                    "per_page": 100,
                    "page": page,
                },
            )
            merges = self._validate_list(response)
            for merge in merges:
                if merge.diff_refs is None:
                    merge = self._detail(path, merge.iid)
                results.append(
                    self._observation(repository_id, merge, observed_at=observed_at)
                )
            next_page = response.headers.get("X-Next-Page", "").strip()
            if not next_page:
                return results
            if page >= self._max_pages or not next_page.isdigit():
                raise ChangeRequestProviderPaginationLimit(
                    "GitLab Merge Request pagination exceeded the configured limit."
                )
            page = int(next_page)

    def _detail(self, collection_path: str, external_number: int) -> _GitLabMergeRequest:
        response = self._request("GET", f"{collection_path}/{external_number}")
        try:
            merge = _GitLabMergeRequest.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ChangeRequestProviderContractMismatch(
                "GitLab returned invalid Merge Request detail JSON data."
            ) from exc
        if merge.diff_refs is None:
            raise ChangeRequestProviderContractMismatch(
                "GitLab Merge Request detail has no diff_refs."
            )
        return merge

    @staticmethod
    def _validate_list(response: httpx2.Response) -> list[_GitLabMergeRequest]:
        try:
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError
            return [_GitLabMergeRequest.model_validate(item) for item in payload]
        except (ValueError, ValidationError) as exc:
            raise ChangeRequestProviderContractMismatch(
                "GitLab returned invalid Merge Request JSON data."
            ) from exc

    @staticmethod
    def _observation(
        repository_id: UUID,
        merge: _GitLabMergeRequest,
        *,
        observed_at: datetime,
    ) -> ChangeRequestObservationRequest:
        if merge.diff_refs is None:
            raise ChangeRequestProviderContractMismatch(
                "GitLab Merge Request has no diff_refs."
            )
        state_map = {"opened": "open", "closed": "closed", "merged": "merged"}
        state = state_map.get(merge.state)
        if state is None:
            raise ChangeRequestProviderContractMismatch(
                "GitLab returned an unsupported Merge Request state."
            )
        merge_sha = merge.merge_commit_sha or merge.squash_commit_sha
        if state == "merged" and (merge_sha is None or merge.merged_at is None):
            raise ChangeRequestProviderContractMismatch(
                "Merged GitLab Merge Request has no final commit SHA."
            )
        return ChangeRequestObservationRequest(
            repository_id=repository_id,
            provider="gitlab",
            external_number=merge.iid,
            kind="merge_request",
            state=state,
            title=merge.title,
            base_ref=f"refs/heads/{merge.target_branch}",
            head_ref=f"refs/heads/{merge.source_branch}",
            base_sha=merge.diff_refs.base_sha,
            head_sha=merge.diff_refs.head_sha,
            merge_sha=merge_sha if state == "merged" else None,
            provider_updated_at=merge.updated_at,
            merged_at=merge.merged_at if state == "merged" else None,
            observed_at=observed_at,
        )
