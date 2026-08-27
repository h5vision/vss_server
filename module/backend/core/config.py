"""Environment-backed application settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings with no embedded production credentials."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "Vision Snapshot Backend"
    api_prefix: str = "/v1"
    vision_environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    docs_enabled: bool = True

    database_url: SecretStr | None = None
    snapshot_materialization_root: Path = Path("data/snapshots")
    snapshot_git_command_timeout_seconds: float = Field(default=60.0, gt=0)
    snapshot_recovery_on_startup: bool = True
    snapshot_recovery_batch_size: int = Field(default=100, ge=1, le=500)
    vss_base_url: HttpUrl = "http://127.0.0.1:8200"
    vss_token: SecretStr | None = None
    vss_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    vss_read_timeout_seconds: float = Field(default=10.0, gt=0)
    vss_expected_source_revision: str | None = None

    @field_validator("api_prefix")
    @classmethod
    def validate_api_prefix(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        if not normalized.startswith("/") or normalized == "/":
            raise ValueError("api_prefix must be a non-root absolute path")
        return normalized

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("unsupported log level")
        return normalized

    @field_validator("database_url", "vss_token", mode="before")
    @classmethod
    def empty_database_url_is_unset(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("snapshot_materialization_root")
    @classmethod
    def materialization_root_must_not_be_filesystem_root(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("snapshot_materialization_root must not be a filesystem root")
        return resolved

    @field_validator("vss_expected_source_revision", mode="before")
    @classmethod
    def validate_vss_expected_source_revision(cls, value: str | None) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        normalized = value.strip().lower()
        invalid_character = any(
            character not in "0123456789abcdef" for character in normalized
        )
        if len(normalized) != 40 or invalid_character:
            raise ValueError(
                "vss_expected_source_revision must be a 40-character hexadecimal Git SHA"
            )
        return normalized


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
