"""Read-only Git provider clients for Pull Request and Merge Request metadata."""

from backend.integrations.change_requests.github import GitHubChangeRequestClient
from backend.integrations.change_requests.gitlab import GitLabChangeRequestClient

__all__ = ["GitHubChangeRequestClient", "GitLabChangeRequestClient"]
