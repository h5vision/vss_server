"""Unit tests for database engine and session factories."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from backend.core.config import Settings
from backend.infrastructure.database.engine import (
    create_engine_from_url,
    get_engine_from_settings,
)


def test_create_engine_from_url_normalizes_postgres_and_sqlite() -> None:
    pg_engine = create_engine_from_url("postgresql://user:pass@localhost:5432/db")
    assert "asyncpg" in str(pg_engine.url)

    sqlite_engine = create_engine_from_url("sqlite:///data/test.db")
    assert "aiosqlite" in str(sqlite_engine.url)


def test_get_engine_from_settings_raises_if_not_configured() -> None:
    settings = Settings(database_url=None)
    with pytest.raises(ValueError, match="DATABASE_URL is not configured"):
        get_engine_from_settings(settings)


def test_get_engine_from_settings_creates_engine_when_configured() -> None:
    settings = Settings(database_url=SecretStr("sqlite:///data/test.db"))
    engine = get_engine_from_settings(settings)
    assert engine is not None
    assert "aiosqlite" in str(engine.url)

