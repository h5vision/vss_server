"""Repository·Branch 수집 코어의 구조화 오류."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CollectionError(Exception):
    """수집 실패를 reason·detail·retryable로 표현한다.

    detail에는 Git stderr, remote URL, mirror 경로 같은 server-local 정보를 절대
    포함하지 않는다.
    """

    reason: str
    detail: str
    status_code: int = 503
    retryable: bool = True

    def __str__(self) -> str:
        return self.detail
