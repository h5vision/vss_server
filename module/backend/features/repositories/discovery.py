"""Read-only GitHub repository metadata discovery for Admin registration UX."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlparse

import httpx2


@dataclass(frozen=True, slots=True)
class RepositoryDiscoveryError(Exception):
    status_code: int
    reason: str
    detail: str
    retryable: bool = False


def parse_github_repository_url(remote_url: str) -> tuple[str, str]:
    """Return ``(owner, repository)`` only for canonical github.com HTTPS URLs."""

    normalized = remote_url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise RepositoryDiscoveryError(
            400,
            "REPOSITORY_DISCOVERY_UNSUPPORTED_URL",
            "GitHub discovery requires an http(s) github.com Repository URL.",
        )
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RepositoryDiscoveryError(
            400,
            "REPOSITORY_DISCOVERY_UNSUPPORTED_URL",
            "Repository URL must not contain credentials, query parameters, or a fragment.",
        )
    host = (parsed.hostname or "").lower()
    if host not in {"github.com", "www.github.com"}:
        raise RepositoryDiscoveryError(
            400,
            "REPOSITORY_DISCOVERY_UNSUPPORTED_PROVIDER",
            "Automatic metadata discovery currently supports github.com only.",
        )
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) != 2:
        raise RepositoryDiscoveryError(
            400,
            "REPOSITORY_DISCOVERY_UNSUPPORTED_URL",
            "GitHub Repository URL must have the form https://github.com/<owner>/<repo>.git.",
        )
    owner, repository = parts
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    if not owner or not repository:
        raise RepositoryDiscoveryError(
            400,
            "REPOSITORY_DISCOVERY_UNSUPPORTED_URL",
            "GitHub Repository owner and name are required.",
        )
    return owner, repository


async def discover_github_repository(
    remote_url: str,
    *,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """Resolve canonical public GitHub metadata without following caller-controlled hosts."""

    owner, repository = parse_github_repository_url(remote_url)
    timeout = httpx2.Timeout(connect=2.0, read=5.0, write=5.0, pool=2.0)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "vision-snapshot-admin",
    }
    try:
        async with httpx2.AsyncClient(
            base_url="https://api.github.com/",
            headers=headers,
            timeout=timeout,
            transport=transport,
            trust_env=False,
        ) as client:
            response = await client.get(
                f"repos/{quote(owner, safe='')}/{quote(repository, safe='')}"
            )
    except httpx2.RequestError as exc:
        raise RepositoryDiscoveryError(
            503,
            "REPOSITORY_DISCOVERY_UNAVAILABLE",
            "GitHub repository metadata service is unavailable.",
            retryable=True,
        ) from exc

    if response.status_code == 404:
        raise RepositoryDiscoveryError(
            404,
            "REPOSITORY_DISCOVERY_NOT_FOUND",
            "GitHub Repository was not found or is not publicly readable.",
        )
    if response.status_code in {403, 429}:
        raise RepositoryDiscoveryError(
            503,
            "REPOSITORY_DISCOVERY_RATE_LIMITED",
            "GitHub repository metadata lookup is temporarily rate limited.",
            retryable=True,
        )
    if response.status_code >= 500:
        raise RepositoryDiscoveryError(
            503,
            "REPOSITORY_DISCOVERY_UNAVAILABLE",
            "GitHub repository metadata service returned a server error.",
            retryable=True,
        )
    if response.status_code != 200:
        raise RepositoryDiscoveryError(
            502,
            "REPOSITORY_DISCOVERY_REJECTED",
            "GitHub repository metadata lookup returned an unexpected response.",
        )

    try:
        payload = response.json()
        full_name = str(payload["full_name"]).strip()
        name = str(payload["name"]).strip()
        clone_url = str(payload["clone_url"]).strip()
        html_url = str(payload["html_url"]).strip()
        default_branch = str(payload["default_branch"]).strip()
        repository_id = int(payload["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RepositoryDiscoveryError(
            502,
            "REPOSITORY_DISCOVERY_CONTRACT_MISMATCH",
            "GitHub repository metadata response is missing required fields.",
        ) from exc

    if not all((full_name, name, clone_url, html_url, default_branch)):
        raise RepositoryDiscoveryError(
            502,
            "REPOSITORY_DISCOVERY_CONTRACT_MISMATCH",
            "GitHub repository metadata response contains blank required fields.",
        )

    visibility = str(
        payload.get("visibility")
        or ("private" if payload.get("private") else "public")
    )
    return {
        "provider": "github",
        "provider_repository_id": repository_id,
        "canonical_name": full_name,
        "display_name": name,
        "remote_url": clone_url,
        "html_url": html_url,
        "default_branch_ref": f"refs/heads/{default_branch}",
        "visibility": visibility,
    }
