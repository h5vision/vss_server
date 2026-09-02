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


def test_materialization_root_is_resolved_and_not_filesystem_root(tmp_path) -> None:
    settings = Settings(snapshot_materialization_root=tmp_path / "snapshots")

    assert settings.snapshot_materialization_root.is_absolute()

    with pytest.raises(ValidationError):
        Settings(snapshot_materialization_root=settings.snapshot_materialization_root.anchor)


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


def test_vss_inbound_token_config_path_is_safe_and_absolute() -> None:
    settings = Settings()

    assert settings.snapshot_vss_api_token_config_path == "/etc/vss-snapshot/module.env"

    with pytest.raises(ValidationError):
        Settings(snapshot_vss_api_token_config_path="relative/module.env")
