"""Snapshot state machine governing valid lifecycle transitions."""

from __future__ import annotations

from collections.abc import Mapping


class InvalidStateTransitionError(ValueError):
    """Raised when a snapshot state transition violates lifecycle rules."""

    def __init__(self, from_state: str, to_state: str) -> None:
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"Invalid snapshot state transition from '{from_state}' to '{to_state}'"
        )


VALID_TRANSITIONS: Mapping[str, set[str]] = {
    "received": {
        "received",
        "validated",
        "failed",
        "rejected",
        "aborted",
    },
    "validated": {
        "validated",
        "binding_required",
        "materializing",
        "materialized",
        "submitting",
        "failed",
        "rejected",
        "aborted",
    },
    "binding_required": {
        "binding_required",
        "validated",
        "materializing",
        "failed",
        "rejected",
        "aborted",
    },
    "materializing": {
        "materializing",
        "materialized",
        "submitting",
        "failed",
        "rejected",
        "aborted",
    },
    "materialized": {
        "materialized",
        "submitting",
        "accepted",
        "already_indexed",
        "failed",
        "rejected",
        "aborted",
    },
    "submitting": {
        "submitting",
        "accepted",
        "already_indexed",
        "indexing",
        "completed",
        "failed",
        "rejected",
        "aborted",
    },
    "accepted": {
        "accepted",
        "indexing",
        "completed",
        "failed",
        "rejected",
        "aborted",
    },
    "indexing": {
        "indexing",
        "completed",
        "failed",
        "rejected",
        "aborted",
    },
    "failed": {
        "failed",
        "materializing",
        "submitting",
        "completed",
        "already_indexed",
        "aborted",
    },
    "already_indexed": {"already_indexed"},
    "completed": {"completed"},
    "rejected": {
        "rejected",
        "materializing",
        "submitting",
        "completed",
        "already_indexed",
        "aborted",
    },
    "aborted": {
        "aborted",
        "materializing",
        "submitting",
        "completed",
        "already_indexed",
    },
}


class SnapshotStateMachine:
    """Validates and enforces lifecycle state transitions for snapshots."""

    @classmethod
    def can_transition(cls, from_state: str, to_state: str) -> bool:
        allowed = VALID_TRANSITIONS.get(from_state)
        if allowed is None:
            return False
        return to_state in allowed

    @classmethod
    def validate_transition(cls, from_state: str, to_state: str) -> None:
        if not cls.can_transition(from_state, to_state):
            raise InvalidStateTransitionError(from_state, to_state)
