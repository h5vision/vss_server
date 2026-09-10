from __future__ import annotations

from backend.features.repositories.identity import canonical_vss_project_id


def test_canonical_vss_project_id_uses_repo_at_branch_once() -> None:
    assert (
        canonical_vss_project_id("h5vision/vss_server.git", "refs/heads/test-merge")
        == "vss_server@test-merge"
    )
    assert (
        canonical_vss_project_id("h5vision/vss_server", "refs/heads/module") == "vss_server@module"
    )


def test_canonical_vss_project_id_makes_slash_branch_collision_safe() -> None:
    project_id = canonical_vss_project_id("h5vision/vss_server", "refs/heads/feature/login")
    assert project_id.startswith("vss_server@feature-login-")
    assert "/" not in project_id
    assert canonical_vss_project_id("h5vision/vss_server", "refs/heads/feature/login") == project_id
    assert canonical_vss_project_id("h5vision/vss_server", "refs/heads/feature-login") != project_id


def test_canonical_vss_project_id_does_not_append_to_existing_project_id() -> None:
    # The ID is always re-derived from Repository + Branch, never from a previous VSS ID.
    assert (
        canonical_vss_project_id("h5vision/vss_server", "refs/heads/test-merge").count(
            "@test-merge"
        )
        == 1
    )
