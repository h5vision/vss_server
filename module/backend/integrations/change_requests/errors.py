"""Safe provider HTTP failures without token or response-body leakage."""

from __future__ import annotations


class ChangeRequestProviderError(RuntimeError):
    reason = "CHANGE_REQUEST_PROVIDER_ERROR"
    retryable = False


class ChangeRequestProviderAuthFailed(ChangeRequestProviderError):
    reason = "CHANGE_REQUEST_PROVIDER_AUTH_FAILED"


class ChangeRequestProviderUnavailable(ChangeRequestProviderError):
    reason = "CHANGE_REQUEST_PROVIDER_UNAVAILABLE"
    retryable = True


class ChangeRequestProviderRateLimited(ChangeRequestProviderError):
    reason = "CHANGE_REQUEST_PROVIDER_RATE_LIMITED"
    retryable = True


class ChangeRequestProviderRequestRejected(ChangeRequestProviderError):
    reason = "CHANGE_REQUEST_PROVIDER_REQUEST_REJECTED"


class ChangeRequestProviderContractMismatch(ChangeRequestProviderError):
    reason = "CHANGE_REQUEST_PROVIDER_CONTRACT_MISMATCH"


class ChangeRequestProviderPaginationLimit(ChangeRequestProviderError):
    reason = "CHANGE_REQUEST_PROVIDER_PAGINATION_LIMIT"
    retryable = True
