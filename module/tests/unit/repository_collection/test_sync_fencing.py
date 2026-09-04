"""Unit tests for repository sync fencing-token ownership semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.infrastructure.database.models import Repository, RepositorySyncRun


@pytest.mark.anyio
async def test_claim_sync_uses_monotonic_repository_generation():
    repo_id = uuid4()
    expired = RepositorySyncRun(
        sync_run_id=uuid4(),
        request_id=uuid4(),
        repository_id=repo_id,
        trigger="manual",
        state="running",
        reason="RUNNING",
        detail="detail",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        lease_generation=7,
    )

    class DummySession:
        def __init__(self):
            self.added = []
            self._calls = 0

        async def scalar(self, statement):
            self._calls += 1
            if self._calls == 1:
                return Repository(
                    repository_id=repo_id,
                    remote_url="https://example.com/repo.git",
                    default_branch_ref="refs/heads/main",
                    active=True,
                )
            if self._calls == 2:
                return expired
            if self._calls == 3:
                return 7
            raise AssertionError("unexpected scalar call")

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            pass

    store = RepositoryCollectionStore(DummySession())  # type: ignore[arg-type]
    _, sync_run = await store.claim_sync(
        repo_id,
        request_id=uuid4(),
        trigger="manual",
        lease_seconds=300,
    )

    assert expired.state == "failed"
    assert expired.reason == "COLLECTION_SYNC_LEASE_EXPIRED"
    assert sync_run.lease_generation == 8


@pytest.mark.anyio
async def test_refresh_lease_issues_next_token_and_rejects_failed_cas():
    class DummySession:
        def __init__(self, result):
            self.result = result

        async def scalar(self, statement):
            return self.result

    sync_run_id = uuid4()
    store = RepositoryCollectionStore(DummySession(5))  # type: ignore[arg-type]
    generation = await store.refresh_lease(
        sync_run_id,
        lease_seconds=300,
        expected_generation=4,
    )
    assert generation == 5

    stale_store = RepositoryCollectionStore(DummySession(None))  # type: ignore[arg-type]
    with pytest.raises(CollectionError) as exc_info:
        await stale_store.refresh_lease(
            sync_run_id,
            lease_seconds=300,
            expected_generation=4,
        )
    assert exc_info.value.reason == "COLLECTION_SYNC_FENCING_TOKEN_INVALID"


@pytest.mark.anyio
async def test_assert_sync_owner_and_finish_share_locked_owner_record():
    sync_run = RepositorySyncRun(
        sync_run_id=uuid4(),
        request_id=uuid4(),
        repository_id=uuid4(),
        trigger="manual",
        state="running",
        reason="RUNNING",
        detail="detail",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        lease_generation=9,
    )

    class DummySession:
        async def scalar(self, statement):
            return sync_run

        async def flush(self):
            pass

    store = RepositoryCollectionStore(DummySession())  # type: ignore[arg-type]
    owner = await store.assert_sync_owner(
        sync_run.sync_run_id,
        expected_generation=9,
    )
    assert owner is sync_run

    persisted = await store.finish_sync(
        sync_run.sync_run_id,
        state="succeeded",
        reason="DONE",
        detail="detail",
        retryable=False,
        result_json=[],
        finished_at=datetime.now(timezone.utc),
        expected_generation=9,
    )
    assert persisted.state == "succeeded"


@pytest.mark.anyio
async def test_assert_sync_owner_rejects_stale_or_expired_owner():
    class DummySession:
        async def scalar(self, statement):
            return None

    store = RepositoryCollectionStore(DummySession())  # type: ignore[arg-type]
    with pytest.raises(CollectionError) as exc_info:
        await store.assert_sync_owner(uuid4(), expected_generation=3)
    assert exc_info.value.reason == "COLLECTION_SYNC_FENCING_TOKEN_INVALID"
