"""Environment-backed application settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from backend.core.orchestration import IndexOrchestrationMode


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
    snapshot_repository_root: Path = Path("data/repos")
    snapshot_materialization_root: Path = Path("data/snapshots")
    snapshot_git_command_timeout_seconds: float = Field(default=60.0, gt=0)
    snapshot_collection_sync_lease_seconds: int = Field(default=300, ge=60, le=3600)
    snapshot_commit_catalog_max_commits: int = Field(default=10_000, ge=1, le=1_000_000)
    snapshot_commit_catalog_batch_size: int = Field(default=500, ge=1, le=5_000)
    snapshot_commit_catalog_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    snapshot_commit_catalog_lease_seconds: int = Field(default=600, ge=60, le=7200)
    snapshot_commit_subject_max_length: int = Field(default=256, ge=32, le=512)
    snapshot_change_request_collection_enabled: bool = False
    snapshot_tag_collection_enabled: bool = False
    snapshot_tag_max_count: int = Field(default=5_000, ge=1, le=100_000)
    snapshot_github_api_url: HttpUrl = "https://api.github.com"
    snapshot_github_api_version: str = Field(
        default="2026-03-10",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    snapshot_github_api_token: SecretStr | None = None
    snapshot_gitlab_api_url: HttpUrl = "https://gitlab.com/api/v4"
    snapshot_gitlab_api_token: SecretStr | None = None
    snapshot_change_request_max_pages: int = Field(default=10, ge=1, le=100)
    snapshot_change_request_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    snapshot_change_request_read_timeout_seconds: float = Field(default=15.0, gt=0)
    snapshot_recovery_on_startup: bool = True
    snapshot_recovery_batch_size: int = Field(default=100, ge=1, le=500)
    snapshot_index_orchestration_mode: IndexOrchestrationMode = "module_push"
    snapshot_admin_service_token: SecretStr | None = None
    snapshot_admin_identity_secret: SecretStr | None = None
    snapshot_admin_signature_max_age_seconds: int = Field(default=30, ge=5, le=300)
    vss_base_url: HttpUrl = "http://127.0.0.1:8200"
    vss_token: SecretStr | None = None
    snapshot_vss_api_token: SecretStr | None = None
    snapshot_vss_api_token_config_path: str = Field(
        default="/etc/vss-snapshot/module.env",
        min_length=1,
        max_length=1024,
    )
    vss_connect_timeout_seconds: float = Field(default=2.0, gt=0)
    vss_read_timeout_seconds: float = Field(default=10.0, gt=0)
    vss_expected_source_revision: str | None = None
    ollama_base_url: HttpUrl = "http://127.0.0.1:11434"
    ollama_connect_timeout_seconds: float = Field(default=1.0, gt=0)
    ollama_read_timeout_seconds: float = Field(default=2.0, gt=0)

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

    @field_validator(
        "database_url",
        "vss_token",
        "snapshot_vss_api_token",
        "snapshot_github_api_token",
        "snapshot_gitlab_api_token",
        "snapshot_admin_service_token",
        "snapshot_admin_identity_secret",
        mode="before",
    )
    @classmethod
    def empty_database_url_is_unset(cls, value):
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("snapshot_github_api_url", "snapshot_gitlab_api_url")
    @classmethod
    def provider_api_url_must_not_embed_credentials(cls, value: HttpUrl) -> HttpUrl:
        if value.username or value.password or value.query or value.fragment:
            raise ValueError("provider API URL must not contain credentials, query, or fragment")
        return value

    @field_validator("snapshot_admin_service_token")
    @classmethod
    def validate_admin_service_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 24:
            raise ValueError("snapshot_admin_service_token must contain at least 24 characters")
        return value

    @field_validator("snapshot_admin_identity_secret")
    @classmethod
    def validate_admin_identity_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("snapshot_admin_identity_secret must contain at least 32 characters")
        return value

    @model_validator(mode="after")
    def admin_credentials_must_be_distinct(self) -> Settings:
        service_token = self.snapshot_admin_service_token
        identity_secret = self.snapshot_admin_identity_secret
        if (
            service_token is not None
            and identity_secret is not None
            and service_token.get_secret_value() == identity_secret.get_secret_value()
        ):
            raise ValueError(
                "snapshot_admin_service_token and snapshot_admin_identity_secret "
                "must be distinct"
            )
        return self

    @model_validator(mode="after")
    def commit_catalog_lease_must_cover_scan_timeout(self) -> Settings:
        if (
            self.snapshot_commit_catalog_lease_seconds
            <= self.snapshot_commit_catalog_timeout_seconds
        ):
            raise ValueError(
                "snapshot_commit_catalog_lease_seconds must exceed "
                "snapshot_commit_catalog_timeout_seconds"
            )
        return self

    @field_validator("snapshot_repository_root", "snapshot_materialization_root")
    @classmethod
    def snapshot_roots_must_not_be_filesystem_root(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("snapshot filesystem roots must not be a filesystem root")
        return resolved

    @model_validator(mode="after")
    def repository_and_materialization_roots_must_be_separate(self) -> Settings:
        repository_root = self.snapshot_repository_root
        materialization_root = self.snapshot_materialization_root
        if (
            repository_root == materialization_root
            or repository_root.is_relative_to(materialization_root)
            or materialization_root.is_relative_to(repository_root)
        ):
            raise ValueError(
                "snapshot_repository_root and snapshot_materialization_root must be "
                "separate non-nested directories"
            )
        return self

    @field_validator("snapshot_vss_api_token_config_path")
    @classmethod
    def validate_vss_token_config_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.startswith("/"):
            raise ValueError("snapshot_vss_api_token_config_path must be an absolute POSIX path")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("snapshot_vss_api_token_config_path contains control characters")
        return normalized

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
