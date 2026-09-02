from __future__ import annotations

import pytest

from backend.core.errors import ApiError
from backend.features.admin.pagination import decode_cursor, encode_cursor, paginate


def test_admin_cursor_is_opaque_and_round_trips_offset() -> None:
    cursor = encode_cursor(125)

    assert cursor != "125"
    assert decode_cursor(cursor) == 125


@pytest.mark.parametrize("cursor", ["", "125", "%%%", "djI6LTE="])
def test_admin_cursor_rejects_invalid_or_negative_values(cursor: str) -> None:
    with pytest.raises(ApiError) as captured:
        decode_cursor(cursor)
    assert captured.value.status_code == 422
    assert captured.value.reason == "ADMIN_CURSOR_INVALID"


def test_paginate_hides_probe_row_and_returns_next_cursor() -> None:
    items, next_cursor = paginate(["a", "b", "c"], limit=2, offset=10)

    assert items == ["a", "b"]
    assert decode_cursor(next_cursor) == 12
