"""Git infrastructure adapters and low-level CLI execution."""

from backend.infrastructure.git.runner import (
    GitCommandRunner,
    assert_inside_root,
    is_link_or_junction,
    is_sha,
    remove_readonly,
)

__all__ = [
    "GitCommandRunner",
    "assert_inside_root",
    "is_link_or_junction",
    "is_sha",
    "remove_readonly",
]
