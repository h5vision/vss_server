"""Synchronous client for the pinned h5vision/vss_server HTTP API."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, TypeVar

import httpx2
from pydantic import BaseModel, ValidationError

from backend.core.config import Settings
from backend.integrations.vss.errors import (
    VssAuthFailed,
    VssHttpContractMismatch,
    VssHttpRequestRejected,
    VssHttpUnavailable,
)
from backend.integrations.vss.schemas import (
    VssExistsResult,
    VssHealthResponse,
    VssIndexRequest,
    VssIndexStatus,
    VssProjectsResponse,
    VssStartIndexResponse,
    VssStartIndexResult,
)

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)


class VssHttpClient:
    """Call only the public VSS HTTP routes used by Snapshot processing."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 10.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if token:
            headers["X-VSS-Token"] = token
        timeout = httpx2.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=read_timeout_seconds,
            pool=connect_timeout_seconds,
        )
        self._client = httpx2.Client(
            base_url=base_url.rstrip("/") + "/",
            headers=headers,
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
    ) -> VssHttpClient:
        token = settings.vss_token.get_secret_value() if settings.vss_token else None
        return cls(
            base_url=str(settings.vss_base_url),
            token=token,
            connect_timeout_seconds=settings.vss_connect_timeout_seconds,
            read_timeout_seconds=settings.vss_read_timeout_seconds,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> VssHttpClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def start_index(self, request: VssIndexRequest) -> VssStartIndexResponse:
        response = self._request(
            "POST",
            "index",
            expected_statuses=(202, 409),
            json=request.model_dump(exclude_none=True),
        )
        result = self._validate_json(response, VssStartIndexResult)
        if (response.status_code == 202) is not result.accepted:
            raise VssHttpContractMismatch(
                "VSS /index status and accepted flag disagree.",
                upstream_status_code=response.status_code,
            )
        return VssStartIndexResponse(status_code=response.status_code, result=result)

    def status(self, project_id: str) -> VssIndexStatus:
        response = self._request(
            "GET",
            "index/status",
            expected_statuses=(200,),
            params={"project_id": self._project_id(project_id)},
        )
        return self._validate_json(response, VssIndexStatus)

    def exists(self, project_id: str) -> VssExistsResult:
        response = self._request(
            "GET",
            "index/exists",
            expected_statuses=(200,),
            params={"project_id": self._project_id(project_id)},
        )
        return self._validate_json(response, VssExistsResult)

    def list_projects(self) -> VssProjectsResponse:
        response = self._request("GET", "projects", expected_statuses=(200,))
        return self._validate_json(response, VssProjectsResponse)

    def health(self) -> VssHealthResponse:
        response = self._request("GET", "health", expected_statuses=(200,))
        return self._validate_json(response, VssHealthResponse)

    @staticmethod
    def _project_id(project_id: str) -> str:
        normalized = project_id.strip()
        if not normalized:
            raise ValueError("project_id must not be blank")
        return normalized

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: Iterable[int],
        **kwargs: Any,
    ) -> httpx2.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx2.RequestError as exc:
            raise VssHttpUnavailable("VSS HTTP service is unavailable.") from exc

        expected = set(expected_statuses)
        if response.status_code in expected:
            return response
        if response.status_code in (401, 403):
            raise VssAuthFailed(
                "VSS authentication failed.", upstream_status_code=response.status_code
            )
        if response.status_code >= 500:
            raise VssHttpUnavailable(
                "VSS HTTP service returned a server error.",
                upstream_status_code=response.status_code,
            )
        if 400 <= response.status_code < 500:
            raise VssHttpRequestRejected(
                "VSS HTTP service rejected the request.",
                upstream_status_code=response.status_code,
            )
        raise VssHttpContractMismatch(
            "VSS HTTP service returned an unexpected status.",
            upstream_status_code=response.status_code,
        )

    @staticmethod
    def _validate_json(response: httpx2.Response, model: type[ResponseModel]) -> ResponseModel:
        try:
            payload = response.json()
            return model.model_validate(payload)
        except (ValueError, ValidationError) as exc:
            raise VssHttpContractMismatch(
                "VSS HTTP service returned invalid JSON data.",
                upstream_status_code=response.status_code,
            ) from exc
