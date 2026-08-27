"""Safe integration errors for the embedded VSS module."""

from __future__ import annotations


class VssIntegrationError(RuntimeError):
    reason = "VSS_MODULE_ERROR"
    retryable = True


class VssModuleUnavailable(VssIntegrationError):
    reason = "VSS_MODULE_UNAVAILABLE"
    retryable = False


class VssModuleContractMismatch(VssIntegrationError):
    reason = "VSS_MODULE_CONTRACT_MISMATCH"
    retryable = False


class VssModuleCallFailed(VssIntegrationError):
    reason = "VSS_MODULE_CALL_FAILED"
    retryable = True
