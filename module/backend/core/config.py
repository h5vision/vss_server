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
    vss_module_name: str = "vss.indexer"
    vss_source_revision: str | None = None
    vss_job_stale_seconds: int = Field(default=300, gt=0)
    vss_store: Literal["chroma", "pgvector"] = "chroma"
    vss_data_dir: Path = Path("data/vss")
    vss_ollama_url: HttpUrl = "http://127.0.0.1:11434"
    vss_pg_dsn: SecretStr | None = None
    vss_pg_schema: str = "rag"
    vss_embed_model: str = "bge-m3:latest"

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

    @field_validator("database_url", "vss_pg_dsn", mode="before")
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

    @field_validator("vss_module_name")
    @classmethod
    def validate_vss_module_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(not part.isidentifier() for part in normalized.split(".")):
            raise ValueError("vss_module_name must be a dotted Python module name")
        return normalized

    @field_validator("vss_source_revision", mode="before")
    @classmethod
    def validate_vss_source_revision(cls, value: str | None) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        normalized = value.strip().lower()
        invalid_character = any(
            character not in "0123456789abcdef" for character in normalized
        )
        if len(normalized) != 40 or invalid_character:
            raise ValueError("vss_source_revision must be a 40-character hexadecimal Git SHA")
        return normalized

    @field_validator("vss_data_dir")
    @classmethod
    def vss_data_dir_must_not_be_filesystem_root(cls, value: Path) -> Path:
        resolved = value.expanduser().resolve()
        if resolved == Path(resolved.anchor):
            raise ValueError("vss_data_dir must not be a filesystem root")
        return resolved

    @field_validator("vss_pg_schema")
    @classmethod
    def validate_vss_pg_schema(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized.isidentifier():
            raise ValueError("vss_pg_schema must be a SQL identifier")
        return normalized

    @field_validator("vss_embed_model")
    @classmethod
    def validate_vss_embed_model(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("vss_embed_model must not be blank")
        return normalized

    def vss_environment(self) -> dict[str, str]:
        """Values that must exist in os.environ before vss.config is imported."""

        return {
            "VSS_STORE": self.vss_store,
            "VSS_DATA_DIR": str(self.vss_data_dir),
            "VSS_OLLAMA_URL": str(self.vss_ollama_url).rstrip("/"),
            "VSS_PG_DSN": self.vss_pg_dsn.get_secret_value() if self.vss_pg_dsn else "",
            "VSS_PG_SCHEMA": self.vss_pg_schema,
            "VSS_EMBED_MODEL": self.vss_embed_model,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
