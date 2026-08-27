"""SQLAlchemy engine and connection management."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from backend.core.config import Settings


def create_engine_from_url(database_url: str, **kwargs: Any) -> AsyncEngine:
    """Create an asynchronous SQLAlchemy engine."""
    # Convert standard postgresql:// or sqlite:// URLs to async variants if needed
    normalized_url = database_url
    if normalized_url.startswith("postgresql://"):
        normalized_url = normalized_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    elif normalized_url.startswith("sqlite://") and not normalized_url.startswith("sqlite+aiosqlite://"):
        normalized_url = normalized_url.replace("sqlite://", "sqlite+aiosqlite://", 1)

    return create_async_engine(
        normalized_url,
        **kwargs,
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Create an async session factory bound to the given engine."""
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


def get_engine_from_settings(settings: Settings) -> AsyncEngine:
    """Create engine from application settings."""
    if not settings.database_url:
        raise ValueError("DATABASE_URL is not configured.")
    return create_engine_from_url(settings.database_url.get_secret_value())

