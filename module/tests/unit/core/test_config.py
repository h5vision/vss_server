"""Settings validation without loading real credentials."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.core.config import Settings


def test_empty_optional_environment_values_are_unset() -> None:
    settings = Settings(database_url="")

    assert settings.database_url is None


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
def test_vss_source_revision_requires_full_git_sha(revision: str) -> None:
    with pytest.raises(ValidationError):
        Settings(vss_source_revision=revision)


def test_empty_vss_source_revision_is_unset_until_deployment_pins_it() -> None:
    assert Settings(vss_source_revision="").vss_source_revision is None


def test_materialization_root_is_resolved_and_not_filesystem_root(tmp_path) -> None:
    settings = Settings(snapshot_materialization_root=tmp_path / "snapshots")

    assert settings.snapshot_materialization_root.is_absolute()

    with pytest.raises(ValidationError):
        Settings(snapshot_materialization_root=settings.snapshot_materialization_root.anchor)


@pytest.mark.parametrize("module_name", ["", "vss-indexer", "vss..indexer"])
def test_vss_module_name_requires_dotted_python_identifiers(module_name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(vss_module_name=module_name)


def test_vss_environment_is_ready_for_import_and_redacts_pg_dsn(tmp_path) -> None:
    dsn = "postgresql://vss_rag:secret@db.example/vss"
    settings = Settings(vss_data_dir=tmp_path / "vss", vss_pg_dsn=dsn)

    environment = settings.vss_environment()

    assert environment["VSS_DATA_DIR"] == str((tmp_path / "vss").resolve())
    assert environment["VSS_PG_DSN"] == dsn
    assert dsn not in repr(settings)
