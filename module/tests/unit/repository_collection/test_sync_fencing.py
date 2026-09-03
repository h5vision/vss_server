"""Unit tests for repository sync lease fencing token (lease_generation)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from backend.features.repository_collection.errors import CollectionError
from backend.features.repository_collection.store import RepositoryCollectionStore
from backend.infrastructure.database.models import Repository, RepositorySyncRun


@pytest.mark.anyio
async def test_claim_sync_initializes_lease_generation():
    class DummySession:
        def __init__(self):
            self.added = []
            self._calls = 0

        async def scalar(self, statement):
            self._calls += 1
            if self._calls == 1:
                return Repository(
                    repository_id=uuid4(),
                    remote_url="https://example.com/repo.git",
                    default_branch_ref="refs/heads/main",
                    active=True,
                )
            return None

        def add(self, item):
            self.added.append(item)

        async def flush(self):
            pass

    session = DummySession()
    store = RepositoryCollectionStore(session)  # type: ignore

    repo_id = uuid4()
    req_id = uuid4()
    _, sync_run = await store.claim_sync(
        repo_id,
        request_id=req_id,
        trigger="manual",
        lease_seconds=300,
    )
    assert sync_run.lease_generation == 1


@pytest.mark.anyio
async def test_refresh_lease_increments_and_validates_fencing_token():
    class DummySession:
        async def flush(self):
            pass

    session = DummySession()
    store = RepositoryCollectionStore(session)  # type: ignore

    sync_run = RepositorySyncRun(
        sync_run_id=uuid4(),
        request_id=uuid4(),
        repository_id=uuid4(),
        trigger="manual",
        state="running",
        reason="RUNNING",
        detail="detail",
        lease_expires_at=datetime.now(timezone.utc),
        lease_generation=1,
    )

    # Refresh with valid token -> generation increments to 2
    new_gen = await store.refresh_lease(
        sync_run,
        lease_seconds=300,
        expected_generation=1,
    )
    assert new_gen == 2
    assert sync_run.lease_generation == 2

    # Refresh with stale token -> raises COLLECTION_SYNC_FENCING_TOKEN_INVALID
    with pytest.raises(CollectionError) as exc_info:
        await store.refresh_lease(
            sync_run,
            lease_seconds=300,
            expected_generation=1,  # Stale: actual is 2
        )
    assert exc_info.value.reason == "COLLECTION_SYNC_FENCING_TOKEN_INVALID"


@pytest.mark.anyio
async def test_finish_sync_validates_fencing_token():
    class DummySession:
        async def flush(self):
            pass

    session = DummySession()
    store = RepositoryCollectionStore(session)  # type: ignore

    sync_run = RepositorySyncRun(
        sync_run_id=uuid4(),
        request_id=uuid4(),
        repository_id=uuid4(),
        trigger="manual",
        state="running",
        reason="RUNNING",
        detail="detail",
        lease_expires_at=datetime.now(timezone.utc),
        lease_generation=5,
    )

    # Finish with stale token -> rejected
    with pytest.raises(CollectionError) as exc_info:
        await store.finish_sync(
            sync_run,
            state="succeeded",
            reason="DONE",
            detail="detail",
            retryable=False,
            result_json=[],
            finished_at=datetime.now(timezone.utc),
            expected_generation=4,  # Stale: actual is 5
        )
    assert exc_info.value.reason == "COLLECTION_SYNC_FENCING_TOKEN_INVALID"

    # Finish with correct token -> succeeds
    await store.finish_sync(
        sync_run,
        state="succeeded",
        reason="DONE",
        detail="detail",
        retryable=False,
        result_json=[],
        finished_at=datetime.now(timezone.utc),
        expected_generation=5,
    )
    assert sync_run.state == "succeeded"
