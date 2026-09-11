"""Canonical branch-qualified project identifiers for Module-managed VSS indexes."""

from __future__ import annotations

import hashlib
import re

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_PROJECT_ID_LENGTH = 255
_MODULE_INDEX_VARIANT = "module"


def branch_name(branch_ref: str) -> str:
    prefix = "refs/heads/"
    if not branch_ref.startswith(prefix):
        raise ValueError("branch_ref must start with 'refs/heads/'")
    value = branch_ref[len(prefix) :].strip()
    if not value:
        raise ValueError("branch_ref must contain a branch name")
    return value


def repository_name(canonical_name: str) -> str:
    normalized = canonical_name.strip().replace("\\", "/").rstrip("/")
    value = normalized.rsplit("/", 1)[-1] if normalized else ""
    if value.endswith(".git"):
        value = value[:-4]
    if not value:
        raise ValueError("canonical_name must contain a repository name")
    return value


def _safe_component(value: str, *, fallback: str, max_length: int = 120) -> str:
    safe = _SAFE_COMPONENT.sub("-", value)
    safe = re.sub(r"-{2,}", "-", safe).strip("._-") or fallback
    collision_risk = safe != value or len(safe) > max_length
    if len(safe) > max_length:
        safe = safe[: max_length - 9].rstrip("._-") or fallback
    if collision_risk:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
        base = safe[: max_length - 9].rstrip("._-") or fallback
        safe = f"{base}-{digest}"
    return safe


def canonical_vss_project_id(canonical_name: str, branch_ref: str) -> str:
    """Return the stable physical ID for a Module-managed VSS index.

    VSS PR #57 reserves ``repo@branch`` as a logical selector that may resolve to
    multiple physical indexes. Module therefore keeps that logical prefix free and
    stores its stable incremental index as ``repo@branch--module``. Existing database
    IDs are reused by the stores and are not rewritten by this derivation rule.
    """
    repo = _safe_component(repository_name(canonical_name), fallback="repository")
    branch = _safe_component(branch_name(branch_ref), fallback="branch")
    project_id = f"{repo}@{branch}--{_MODULE_INDEX_VARIANT}"
    if len(project_id) <= _MAX_PROJECT_ID_LENGTH:
        return project_id

    digest = hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:12]
    prefix_length = _MAX_PROJECT_ID_LENGTH - len(digest) - 1
    prefix = project_id[:prefix_length].rstrip("._-@") or "repository"
    return f"{prefix}-{digest}"
