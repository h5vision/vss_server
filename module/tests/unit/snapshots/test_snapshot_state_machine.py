"""Unit tests for SnapshotStateMachine and transition validations."""

from __future__ import annotations

from uuid import uuid4

import pytest

from backend.features.snapshots.state_machine import (
    InvalidStateTransitionError,
    SnapshotStateMachine,
)
from backend.features.snapshots.store import SnapshotStore
from backend.infrastructure.database.models import Snapshot


def test_state_machine_valid_happy_path_transitions():
    assert SnapshotStateMachine.can_transition("received", "validated") is True
    assert SnapshotStateMachine.can_transition("validated", "materializing") is True
    assert SnapshotStateMachine.can_transition("materializing", "materialized") is True
    assert SnapshotStateMachine.can_transition("materialized", "submitting") is True
    assert SnapshotStateMachine.can_transition("submitting", "accepted") is True
    assert SnapshotStateMachine.can_transition("accepted", "indexing") is True
    assert SnapshotStateMachine.can_transition("indexing", "completed") is True


def test_state_machine_retry_and_terminal_rules():
    # Failed snapshot can transition to submitting or materializing for retries
    assert SnapshotStateMachine.can_transition("failed", "submitting") is True
    assert SnapshotStateMachine.can_transition("failed", "materializing") is True

    # Authenticated retry service historically accepts failed/rejected/aborted snapshots.
    assert SnapshotStateMachine.can_transition("rejected", "submitting") is True
    assert SnapshotStateMachine.can_transition("rejected", "completed") is True
    assert SnapshotStateMachine.can_transition("aborted", "submitting") is True
    assert SnapshotStateMachine.can_transition("aborted", "materializing") is True

    # Completed/already_indexed remain terminal and rejected cannot skip to accepted.
    assert SnapshotStateMachine.can_transition("completed", "submitting") is False
    assert SnapshotStateMachine.can_transition("already_indexed", "materializing") is False
    assert SnapshotStateMachine.can_transition("rejected", "accepted") is False

    # Self-transitions are idempotent
    assert SnapshotStateMachine.can_transition("completed", "completed") is True
    assert SnapshotStateMachine.can_transition("indexing", "indexing") is True


def test_state_machine_validate_transition_raises_on_invalid():
    with pytest.raises(InvalidStateTransitionError) as exc_info:
        SnapshotStateMachine.validate_transition("completed", "submitting")
    assert "Invalid snapshot state transition from 'completed' to 'submitting'" in str(
        exc_info.value
    )


@pytest.mark.anyio
async def test_snapshot_store_enforces_state_machine_on_set_state():
    class DummySession:
        async def flush(self):
            pass

    store = SnapshotStore(DummySession())  # type: ignore
    snapshot = Snapshot(
        snapshot_id=uuid4(),
        state="completed",
        base_revision="1" * 40,
        target_revision="2" * 40,
        vss_project_id="test",
        source_type="remote_clone",
    )

    # Invalid transition from terminal 'completed' to 'submitting'
    with pytest.raises(InvalidStateTransitionError):
        await store.set_state(snapshot, "submitting")

    # Valid self transition is allowed
    await store.set_state(snapshot, "completed")
    assert snapshot.state == "completed"
