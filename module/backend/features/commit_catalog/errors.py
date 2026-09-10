"""Structured commit catalog errors."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommitCatalogError(RuntimeError):
    reason: str
    detail: str
    retryable: bool
    status_code: int = 500

    def __str__(self) -> str:
        return self.detail
