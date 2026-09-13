"""Repository 수집 경계의 안전한 구조화 오류."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CollectionError(RuntimeError):
    reason: str
    detail: str
    retryable: bool
    status_code: int = 500

    def __str__(self) -> str:
        return self.detail
