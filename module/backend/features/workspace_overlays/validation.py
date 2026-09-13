"""Pure validation helpers shared by Snapshot transport schemas."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_GIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


def validate_git_revision(value: str) -> str:
    """Require a real-shaped Git commit SHA without inventing a replacement."""

    if not _GIT_SHA.fullmatch(value):
        raise ValueError("must be a 40-character hexadecimal Git commit SHA")
    return value


def validate_sha256(value: str) -> str:
    if not _SHA256.fullmatch(value):
        raise ValueError("must be a 64-character hexadecimal SHA-256")
    return value


def validate_posix_relative_path(value: str) -> str:
    """Keep materialized paths inside the Snapshot project root."""

    if not value or len(value) > 4096:
        raise ValueError("must contain between 1 and 4096 characters")
    if (
        "\x00" in value
        or "\\" in value
        or value.startswith(("/", "//"))
        or _WINDOWS_DRIVE.match(value)
    ):
        raise ValueError("must be a project-relative POSIX path")

    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("must not contain empty, '.' or '..' path segments")
    if any(part.casefold() == ".git" for part in parts):
        raise ValueError("must not address Git metadata")
    if PurePosixPath(value).as_posix() != value:
        raise ValueError("must already be normalized as a POSIX path")
    return value
