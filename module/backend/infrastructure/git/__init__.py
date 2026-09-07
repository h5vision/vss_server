"""Git infrastructure adapters and low-level CLI execution."""

from backend.infrastructure.git.checkout import GitTreeCheckoutAdapter
from backend.infrastructure.git.comparison import (
    GitCompareFileChange,
    GitCompareResult,
    GitRevisionCompareAdapter,
)
from backend.infrastructure.git.graph import GitCommitGraphAdapter
from backend.infrastructure.git.layout import GitCacheLayout
from backend.infrastructure.git.objects import GitRemoteObjectAdapter
from backend.infrastructure.git.refs import GitRemoteRefAdapter
from backend.infrastructure.git.runner import (
    GitCommandRunner,
    assert_inside_root,
    is_link_or_junction,
    is_sha,
    remove_readonly,
)
from backend.infrastructure.git.workspace import RepositoryWorkspaceManager

__all__ = [
    "GitCacheLayout",
    "GitCommandRunner",
    "GitCommitGraphAdapter",
    "GitCompareFileChange",
    "GitCompareResult",
    "GitRemoteObjectAdapter",
    "GitRemoteRefAdapter",
    "GitRevisionCompareAdapter",
    "GitTreeCheckoutAdapter",
    "RepositoryWorkspaceManager",
    "assert_inside_root",
    "is_link_or_junction",
    "is_sha",
    "remove_readonly",
]
