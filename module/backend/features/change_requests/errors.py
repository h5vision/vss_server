"""Structured change request catalog errors."""

from __future__ import annotations


class ChangeRequestError(Exception):
    def __init__(
        self,
        *,
        reason: str,
        detail: str,
        retryable: bool,
        status_code: int,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.retryable = retryable
        self.status_code = status_code
