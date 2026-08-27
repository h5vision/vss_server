"""Structured failures raised before a VSS indexing request is made."""

from __future__ import annotations


class MaterializationError(RuntimeError):
    def __init__(
        self,
        *,
        reason: str,
        detail: str,
        status_code: int,
        retryable: bool,
    ) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.status_code = status_code
        self.retryable = retryable


def unsafe_path(
    detail: str = "Snapshot 경로가 안전한 materialization 경계를 벗어납니다.",
) -> MaterializationError:
    return MaterializationError(
        reason="SNAPSHOT_PATH_UNSAFE",
        detail=detail,
        status_code=409,
        retryable=False,
    )
