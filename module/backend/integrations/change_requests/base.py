"""Shared HTTP and repository-path validation for Git provider clients."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

import httpx2

from backend.integrations.change_requests.errors import (
    ChangeRequestProviderAuthFailed,
    ChangeRequestProviderContractMismatch,
    ChangeRequestProviderRateLimited,
    ChangeRequestProviderRequestRejected,
    ChangeRequestProviderUnavailable,
)


class ChangeRequestProviderHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        headers: dict[str, str],
        connect_timeout_seconds: float = 2.0,
        read_timeout_seconds: float = 10.0,
        transport: httpx2.BaseTransport | None = None,
    ) -> None:
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

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        expected_statuses: Iterable[int] = (200,),
        **kwargs: Any,
    ) -> httpx2.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx2.RequestError as exc:
            raise ChangeRequestProviderUnavailable(
                "Git provider HTTP service is unavailable."
            ) from exc
        if response.status_code in set(expected_statuses):
            return response
        if response.status_code == 429 or (
            response.status_code == 403
            and response.headers.get("X-RateLimit-Remaining") == "0"
        ):
            raise ChangeRequestProviderRateLimited("Git provider rate limit was reached.")
        if response.status_code in {401, 403}:
            raise ChangeRequestProviderAuthFailed("Git provider authentication failed.")
        if response.status_code >= 500:
            raise ChangeRequestProviderUnavailable("Git provider returned a server error.")
        if 400 <= response.status_code < 500:
            raise ChangeRequestProviderRequestRejected("Git provider rejected the request.")
        raise ChangeRequestProviderContractMismatch(
            "Git provider returned an unexpected HTTP status."
        )

    @staticmethod
    def _repository_path(canonical_name: str, remote_url: str | None) -> str:
        candidate = canonical_name.strip().strip("/")
        if "/" not in candidate and remote_url:
            parsed = urlsplit(remote_url)
            candidate = parsed.path.strip("/")
            if candidate.endswith(".git"):
                candidate = candidate[:-4]
        parts = candidate.split("/")
        if len(parts) < 2 or any(
            not part
            or part in {".", ".."}
            or any(ord(character) < 32 or ord(character) == 127 for character in part)
            for part in parts
        ):
            raise ChangeRequestProviderContractMismatch(
                "Repository canonical name or remote URL has no safe provider path."
            )
        return "/".join(parts)
