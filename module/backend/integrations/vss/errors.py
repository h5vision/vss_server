"""Safe errors for the VSS HTTP integration boundary."""

from __future__ import annotations


class VssIntegrationError(RuntimeError):
    reason = "VSS_HTTP_ERROR"
    retryable = True

    def __init__(self, detail: str, *, upstream_status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.upstream_status_code = upstream_status_code


class VssHttpUnavailable(VssIntegrationError):
    reason = "VSS_HTTP_UNAVAILABLE"
    retryable = True


class VssAuthFailed(VssIntegrationError):
    reason = "VSS_AUTH_FAILED"
    retryable = False


class VssHttpRequestRejected(VssIntegrationError):
    reason = "VSS_HTTP_REQUEST_REJECTED"
    retryable = False


class VssHttpContractMismatch(VssIntegrationError):
    reason = "VSS_HTTP_CONTRACT_MISMATCH"
    retryable = False
