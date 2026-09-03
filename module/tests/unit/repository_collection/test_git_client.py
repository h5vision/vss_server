"""선택 Branch만 fetch하고 관측한 HEAD object를 보존하는 Git client 검증."""

from __future__ import annotations

import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

from backend.features.commit_catalog.errors import CommitCatalogError
from backend.features.repository_collection.git_client import RepositoryGitClient


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def create_remote(root: Path) -> tuple[Path, Path, str, str]:
    remote = root / "remote.git"
    work = root / "work"
    remote.mkdir()
    work.mkdir()
    git(remote, "init", "--bare")
    git(work, "init", "-b", "main")
    git(work, "config", "user.email", "collector@example.invalid")
    git(work, "config", "user.name", "Collector Test")
    git(work, "remote", "add", "origin", str(remote))
    (work / "app.py").write_text("version = 1\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "first")
    first_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "-u", "origin", "main")

    git(work, "checkout", "-b", "feature")
    (work / "feature.py").write_text("enabled = True\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "feature")
    feature_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "-u", "origin", "feature")
    git(work, "checkout", "main")
    return remote, work, first_sha, feature_sha


def test_catalog_fetch_history_and_exact_checkout(tmp_path: Path) -> None:
    remote, work, first_sha, feature_sha = create_remote(tmp_path)
    repository_id = uuid4()
    tracked_branch_id = uuid4()
    client = RepositoryGitClient(root=tmp_path / "snapshots")

    catalog = client.list_remote_heads(str(remote))
    assert [(item.branch_ref, item.commit_sha) for item in catalog] == [
        ("refs/heads/feature", feature_sha),
        ("refs/heads/main", first_sha),
    ]

    assert client.fetch_branch(
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        remote_url=str(remote),
        branch_ref="refs/heads/main",
    ) == first_sha
    cache = tmp_path / "snapshots" / ".repository-cache" / f"{repository_id.hex}.git"
    assert git(cache, "for-each-ref", "--format=%(refname)", "refs/remotes/origin") == (
        "refs/remotes/origin/main"
    )

    (work / "app.py").write_text("version = 2\n", "utf-8")
    git(work, "add", "--all")
    git(work, "commit", "-m", "second")
    second_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "main")
    assert client.fetch_branch(
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        remote_url=str(remote),
        branch_ref="refs/heads/main",
    ) == second_sha
    assert client.is_ancestor(repository_id, first_sha, second_sha) is True

    git(work, "reset", "--hard", first_sha)
    git(work, "push", "--force", "origin", "HEAD:main")
    assert client.fetch_branch(
        repository_id=repository_id,
        tracked_branch_id=tracked_branch_id,
        remote_url=str(remote),
        branch_ref="refs/heads/main",
    ) == first_sha
    assert client.is_ancestor(repository_id, second_sha, first_sha) is False
    assert git(cache, "cat-file", "-t", second_sha) == "commit"
    assert git(
        cache,
        "show-ref",
        "--verify",
        f"refs/vss-history/{tracked_branch_id.hex}/{second_sha}",
    ).startswith(second_sha)

    checkout = tmp_path / "checkout"
    client.checkout_revision(
        repository_id=repository_id,
        revision=second_sha,
        destination=checkout,
    )
    assert git(checkout, "rev-parse", "HEAD") == second_sha
    assert git(checkout, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_commit_graph_scan_preserves_intermediate_and_merge_parents(tmp_path: Path) -> None:
    remote, work, first_sha, feature_sha = create_remote(tmp_path)
    git(work, "merge", "--no-ff", "feature", "-m", "merge feature")
    merge_sha = git(work, "rev-parse", "HEAD")
    git(work, "push", "origin", "main")

    repository_id = uuid4()
    client = RepositoryGitClient(root=tmp_path / "snapshots")
    client.fetch_branch(
        repository_id=repository_id,
        tracked_branch_id=uuid4(),
        remote_url=str(remote),
        branch_ref="refs/heads/main",
    )
    scan = client.scan_commit_graph(
        repository_id=repository_id,
        roots=[merge_sha, "f" * 40],
        max_commits=100,
        timeout_seconds=30,
        subject_max_length=256,
    )

    by_sha = {entry.commit_sha: entry for entry in scan.entries}
    assert set(by_sha) == {first_sha, feature_sha, merge_sha}
    assert by_sha[merge_sha].parent_shas == [first_sha, feature_sha]
    assert scan.unavailable_roots == ["f" * 40]
    assert scan.history_complete is False

    truncated = client.scan_commit_graph(
        repository_id=repository_id,
        roots=[merge_sha],
        max_commits=1,
        timeout_seconds=30,
        subject_max_length=256,
    )
    assert truncated.truncated is True
    assert len(truncated.entries) == 1
    assert truncated.entries[0].commit_sha == merge_sha
    assert truncated.entries[0].parent_shas == [first_sha, feature_sha]


def test_commit_graph_scan_rejects_invalid_database_root(tmp_path: Path) -> None:
    client = RepositoryGitClient(root=tmp_path / "snapshots")

    with pytest.raises(CommitCatalogError) as error:
        client.scan_commit_graph(
            repository_id=uuid4(),
            roots=["not-a-sha"],
            max_commits=10,
            timeout_seconds=30,
            subject_max_length=256,
        )

    assert error.value.reason == "COMMIT_CATALOG_ROOT_INVALID"


@pytest.mark.parametrize(
    ("provider", "provider_ref"),
    [
        ("github", "refs/pull/7/head"),
        ("gitlab", "refs/merge-requests/7/head"),
    ],
)
def test_change_request_fetch_uses_provider_owned_ref(
    tmp_path: Path,
    provider: str,
    provider_ref: str,
) -> None:
    remote, work, base_sha, head_sha = create_remote(tmp_path)
    git(work, "push", "origin", f"{head_sha}:{provider_ref}")
    repository_id = uuid4()
    client = RepositoryGitClient(root=tmp_path / "snapshots")

    client.fetch_change_request_revisions(
        repository_id=repository_id,
        remote_url=str(remote),
        provider=provider,
        external_number=7,
        base_ref="refs/heads/main",
        base_sha=base_sha,
        head_sha=head_sha,
        merge_sha=None,
    )

    cache = tmp_path / "snapshots" / ".repository-cache" / f"{repository_id.hex}.git"
    prefix = f"refs/vss-change-requests/{provider}/7"
    assert git(cache, "rev-parse", f"{prefix}/head^{{commit}}") == head_sha
    assert git(cache, "cat-file", "-t", base_sha) == "commit"
    assert git(cache, "show-ref", "--verify", f"{prefix}/revisions/{head_sha}").startswith(
        head_sha
    )


def test_lightweight_and_annotated_tags_resolve_to_commit_and_are_preserved(
    tmp_path: Path,
) -> None:
    remote, work, first_sha, feature_sha = create_remote(tmp_path)
    git(work, "tag", "v1.0.0", first_sha)
    git(work, "tag", "-a", "v2.0.0", feature_sha, "-m", "release v2")
    git(work, "push", "origin", "--tags")
    repository_id = uuid4()
    client = RepositoryGitClient(root=tmp_path / "snapshots")

    tags = client.list_remote_tags(str(remote))
    assert [(tag.tag_ref, tag.commit_sha) for tag in tags] == [
        ("refs/tags/v1.0.0", first_sha),
        ("refs/tags/v2.0.0", feature_sha),
    ]

    client.fetch_tag(
        repository_id=repository_id,
        remote_url=str(remote),
        tag_ref="refs/tags/v2.0.0",
        expected_commit_sha=feature_sha,
    )
    cache = tmp_path / "snapshots" / ".repository-cache" / f"{repository_id.hex}.git"
    assert git(cache, "cat-file", "-t", feature_sha) == "commit"
