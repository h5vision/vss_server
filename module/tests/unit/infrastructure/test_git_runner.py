"""Unit tests for low-level GitCommandRunner and security helpers."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from backend.infrastructure.git import (
    GitCommandRunner,
    assert_inside_root,
    is_sha,
)


def test_is_sha_validation():
    assert is_sha("a" * 40) is True
    assert is_sha("0123456789abcdefABCDEF0123456789abcdef12") is True
    assert is_sha("a" * 39) is False
    assert is_sha("a" * 41) is False
    assert is_sha("g" * 40) is False
    assert is_sha("") is False


def test_assert_inside_root_traversal_detection(tmp_path):
    root = tmp_path / "safe_root"
    root.mkdir()
    inside = root / "nested" / "dir"
    inside.mkdir(parents=True)
    outside = tmp_path / "other"
    outside.mkdir()

    # Safe inside
    assert_inside_root(inside, root)

    # Outside traversal
    with pytest.raises(ValueError, match="Path traversal detected"):
        assert_inside_root(outside, root)


def test_git_command_runner_output_success():
    runner = GitCommandRunner()
    out = runner.output(["git", "--version"])
    assert "git version" in out


def test_git_command_runner_environment_sanitization():
    runner = GitCommandRunner()
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "status"],
            returncode=0,
            stdout="clean",
            stderr="",
        )
        runner.run(["git", "status"])

        assert mock_run.called
        env = mock_run.call_args.kwargs["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"
        assert env["GCM_INTERACTIVE"] == "Never"


def test_git_command_runner_failure_override():
    runner = GitCommandRunner()

    class CustomError(Exception):
        pass

    with pytest.raises(CustomError):
        runner.run(["git", "nonexistent-command-xyz"], failure=CustomError("Command failed"))
