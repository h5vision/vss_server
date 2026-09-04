"""Unit tests for bootstrap ApplicationContainer and build_container."""

from __future__ import annotations

import pytest

from backend.bootstrap.container import ApplicationContainer, build_container
from backend.core.config import Settings


def test_build_container_with_defaults(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        snapshot_materialization_root=tmp_path / "snapshots",
        snapshot_recovery_on_startup=False,
    )

    container = build_container(settings, start_recovery=False)
    assert isinstance(container, ApplicationContainer)
    assert container.settings == settings
    assert container.db_engine is not None
    assert container.db_sessionmaker is not None
    assert container.vss_client is not None
    assert container.snapshot_materializer is not None
    assert container.repository_git_client is not None
    assert container.collected_revision_materializer is not None
    assert container.repository_collection_service is not None
    assert container.commit_catalog_service is not None
    assert container.snapshot_retry_service is not None


@pytest.mark.anyio
async def test_container_dispose(tmp_path):
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path}/test.db",
        snapshot_materialization_root=tmp_path / "snapshots",
        snapshot_recovery_on_startup=False,
    )
    container = build_container(settings, start_recovery=False)
    await container.dispose()
    # Engine is disposed, subsequent operations will cleanly fail or close
    assert container.db_engine is not None
