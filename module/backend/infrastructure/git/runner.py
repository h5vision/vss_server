"""Low-level Git CLI subprocess runner enforcing security, timeouts, and isolation."""

from __future__ import annotations

import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def remove_readonly(function, path: str, _error: Any) -> None:
    """Windows-compatible rmtree error handler for clearing read-only file attributes."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def is_link_or_junction(path: Path) -> bool:
    """Detects symlinks or NTFS reparse point junctions to prevent directory escapes."""
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if is_junction is not None:
        return bool(is_junction())
    if os.name != "nt":
        return False
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (AttributeError, FileNotFoundError, OSError):
        return False


def is_sha(value: str) -> bool:
    """Validates whether a string is a 40-character hexadecimal Git SHA."""
    return len(value) == 40 and all(c in "0123456789abcdefABCDEF" for c in value)


def assert_inside_root(path: Path, root: Path) -> None:
    """Guards against directory traversal by verifying resolved containment and link safety."""
    resolved_path = path.resolve()
    resolved_root = root.resolve()
    if not (resolved_path == resolved_root or resolved_path.is_relative_to(resolved_root)):
        raise ValueError(f"Path traversal detected: {path} is not inside {root}")

    current = path
    while True:
        if (current.exists() or current.is_symlink()) and is_link_or_junction(current):
            raise ValueError(f"Path contains insecure symlink or junction: {current}")
        if current == root:
            break
        if current.parent == current:
            raise ValueError(f"Root path not encountered during hierarchy traversal: {path}")
        current = current.parent


@dataclass(frozen=True, slots=True)
class GitCommandRunner:
    """Centralized Git CLI process runner with sanitization and timeout enforcement."""

    default_timeout_seconds: float = 60.0

    def run(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
        allowed_returncodes: set[int] | None = None,
        input_text: str | None = None,
        env_extra: Mapping[str, str] | None = None,
        failure: Exception | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Runs a Git subprocess with secure isolated environment variables."""
        environment = os.environ.copy()
        environment["GIT_TERMINAL_PROMPT"] = "0"
        environment["GIT_CONFIG_NOSYSTEM"] = "1"
        environment["GIT_CONFIG_GLOBAL"] = os.devnull
        environment["GCM_INTERACTIVE"] = "Never"
        environment.pop("GIT_DIR", None)
        environment.pop("GIT_WORK_TREE", None)
        if env_extra:
            environment.update(env_extra)

        accepted = allowed_returncodes or {0}
        timeout = timeout_seconds or self.default_timeout_seconds

        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                env=environment,
                input=input_text,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if failure is not None:
                raise failure from exc
            raise

        if result.returncode not in accepted:
            if failure is not None:
                raise failure
            raise subprocess.CalledProcessError(
                result.returncode, command, output=result.stdout, stderr=result.stderr
            )
        return result

    def output(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout_seconds: float | None = None,
        failure: Exception | None = None,
    ) -> str:
        """Executes command and returns stripped stdout."""
        return self.run(
            command,
            cwd=cwd,
            timeout_seconds=timeout_seconds,
            failure=failure,
        ).stdout.strip()
