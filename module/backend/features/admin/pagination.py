"""Opaque cursor helpers shared by Admin list endpoints."""

from __future__ import annotations

import base64
from collections.abc import Sequence
from typing import TypeVar

from backend.core.errors import ApiError

T = TypeVar("T")


def _invalid_cursor() -> ApiError:
    return ApiError(
        status_code=422,
        reason="ADMIN_CURSOR_INVALID",
        detail="The Admin list cursor is invalid or expired.",
        retryable=False,
    )


def encode_cursor(offset: int) -> str:
    if offset < 0:
        raise ValueError("cursor offset must not be negative")
    payload = f"v1:{offset}".encode("ascii")
    return base64.urlsafe_b64encode(payload).decode("ascii")


def decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if not cursor:
        raise _invalid_cursor()
    try:
        payload = base64.b64decode(cursor, altchars=b"-_", validate=True).decode("ascii")
        version, separator, offset_text = payload.partition(":")
        offset = int(offset_text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise _invalid_cursor() from exc
    if not separator or version != "v1" or offset < 0 or encode_cursor(offset) != cursor:
        raise _invalid_cursor()
    return offset


def paginate(items: Sequence[T], *, limit: int, offset: int) -> tuple[list[T], str | None]:
    visible = list(items[:limit])
    next_cursor = encode_cursor(offset + limit) if len(items) > limit else None
    return visible, next_cursor
