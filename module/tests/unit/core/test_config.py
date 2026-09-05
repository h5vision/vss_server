"""Settings validation without loading real credentials."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.core.config import Settings


def test_empty_optional_environment_values_are_unset() -> None:
    settings = Settings(database_url="", vss_token="", snapshot_vss_api_token="")

    assert settings.database_url is None
    assert settings.vss_token is None
    assert settings.snapshot_vss_api_token is None


def test_database_url_is_secret_in_settings_representation() -> None:
    database_url = "postgresql://db.example/vision"
    settings = Settings(database_url=database_url)

    assert settings.database_url is not None
    assert settings.database_url.get_secret_value() == database_url
    assert database_url not in repr(settings)


def test_api_prefix_and_log_level_are_normalized() -> None:
    settings = Settings(api_prefix="/v1/", log_level="warning")

    assert settings.api_prefix == "/v1"
    assert settings.log_level == "WARNING"


@pytest.mark.parametrize("revision", ["abc", "g" * 40, "1" * 39])
def test_vss_expected_source_revision_requires_full_git_sha(revision: str) -> None:
    with pytest.raises(ValidationError):
        Settings(vss_expected_source_revision=revision)


def test_empty_vss_expected_source_revision_is_unset_until_deployment_pins_it() -> None:
    settings = Settings(vss_expected_source_revision="")

    assert settings.vss_expected_source_revision is None


def test_repository_and_materialization_roots_are_resolved_and_separate(tmp_path) -> None:
    settings = Settings(
        snapshot_repository_root=tmp_path / "repos",
        snapshot_materialization_root=tmp_path / "snapshots",
    )

    assert settings.snapshot_repository_root.is_absolute()
    assert settings.snapshot_materialization_root.is_absolute()
    assert settings.snapshot_repository_root != settings.snapshot_materialization_root

    with pytest.raises(ValidationError):
        Settings(
            snapshot_repository_root=settings.snapshot_repository_root.anchor,
            snapshot_materialization_root=tmp_path / "snapshots",
        )

    with pytest.raises(ValidationError):
        Settings(
            snapshot_repository_root=tmp_path / "same",
            snapshot_materialization_root=tmp_path / "same",
        )

    with pytest.raises(ValidationError):
        Settings(
            snapshot_repository_root=tmp_path / "repos",
            snapshot_materialization_root=tmp_path / "repos" / "snapshots",
        )


@pytest.mark.parametrize("timeout", [0, -1])
def test_vss_http_timeouts_must_be_positive(timeout: float) -> None:
    with pytest.raises(ValidationError):
        Settings(vss_connect_timeout_seconds=timeout)


def test_vss_http_token_is_secret_and_base_url_is_normalized() -> None:
    token = "vss-secret-token"
    settings = Settings(vss_base_url="http://vss.example:8200", vss_token=token)

    assert str(settings.vss_base_url) == "http://vss.example:8200/"
    assert settings.vss_token is not None
    assert settings.vss_token.get_secret_value() == token
    assert token not in repr(settings)


def test_ollama_runtime_defaults_are_loopback_and_timeouts_are_positive() -> None:
    settings = Settings()

    assert str(settings.ollama_base_url) == "http://127.0.0.1:11434/"
    assert settings.ollama_connect_timeout_seconds > 0
    assert settings.ollama_read_timeout_seconds > 0

    with pytest.raises(ValidationError):
        Settings(ollama_connect_timeout_seconds=0)
    with pytest.raises(ValidationError):
        Settings(ollama_read_timeout_seconds=0)


def test_index_orchestration_mode_is_explicit_and_bounded() -> None:
    assert Settings().snapshot_index_orchestration_mode == "module_push"
    assert (
        Settings(snapshot_index_orchestration_mode="vss_pull").snapshot_index_orchestration_mode
        == "vss_pull"
    )

    with pytest.raises(ValidationError):
        Settings(snapshot_index_orchestration_mode="hybrid")


def test_vss_inbound_token_config_path_is_safe_and_absolute() -> None:
    settings = Settings()

    assert settings.snapshot_vss_api_token_config_path == "/etc/vss-snapshot/module.env"

    with pytest.raises(ValidationError):
        Settings(snapshot_vss_api_token_config_path="relative/module.env")


def test_commit_catalog_limits_are_bounded_and_lease_covers_timeout() -> None:
    settings = Settings()

    assert settings.snapshot_commit_catalog_max_commits == 10_000
    assert settings.snapshot_commit_catalog_batch_size == 500
    assert settings.snapshot_commit_catalog_timeout_seconds == 120
    assert settings.snapshot_commit_catalog_lease_seconds == 600
    assert settings.snapshot_commit_subject_max_length == 256

    with pytest.raises(ValidationError):
        Settings(
            snapshot_commit_catalog_timeout_seconds=600,
            snapshot_commit_catalog_lease_seconds=600,
        )


def test_change_request_provider_settings_are_optional_and_secret() -> None:
    settings = Settings(
        snapshot_change_request_collection_enabled=True,
        snapshot_github_api_token="github-secret",
        snapshot_gitlab_api_token="gitlab-secret",
    )

    assert str(settings.snapshot_github_api_url) == "https://api.github.com/"
    assert str(settings.snapshot_gitlab_api_url) == "https://gitlab.com/api/v4"
    assert settings.snapshot_change_request_max_pages == 10
    assert settings.snapshot_tag_collection_enabled is False
    assert settings.snapshot_tag_max_count == 5_000
    assert settings.snapshot_github_api_token is not None
    assert settings.snapshot_gitlab_api_token is not None
    assert "github-secret" not in repr(settings)
    assert "gitlab-secret" not in repr(settings)


def test_empty_change_request_provider_tokens_are_unset() -> None:
    settings = Settings(
        snapshot_github_api_token="",
        snapshot_gitlab_api_token="",
    )

    assert settings.snapshot_github_api_token is None
    assert settings.snapshot_gitlab_api_token is None
