"""Safe file-trigger bridge for administrator-requested module service restarts.

The Backend never invokes ``systemctl`` or an arbitrary shell command.  It can only
create one of three fixed marker files in a pre-created runtime directory.  A
root-owned systemd path/oneshot pair consumes those markers outside the web
process privilege boundary.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

RestartScope = Literal["snapshot_backend", "admin_web", "module_stack"]

_TRIGGER_FILES: dict[RestartScope, str] = {
    "snapshot_backend": "restart-snapshot-backend.request",
    "admin_web": "restart-admin-web.request",
    "module_stack": "restart-module-stack.request",
}
_SERVICES: dict[RestartScope, tuple[str, ...]] = {
    "snapshot_backend": ("vss-snapshot.service",),
    "admin_web": ("vss-admin-web.service",),
    "module_stack": ("vss-snapshot.service", "vss-admin-web.service"),
}
_STATUS_FILE = "restart-status.json"
_EXECUTION_STATES = {"running", "succeeded", "failed"}


def _repository_git_head() -> str | None:
    """Return the checkout revision that a restart would load, without mutating Git."""
    repo_root = next(
        (parent for parent in Path(__file__).resolve().parents if (parent / ".git").exists()),
        None,
    )
    if repo_root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip().lower()
    valid_revision = (
        result.returncode == 0
        and len(revision) == 40
        and all(c in "0123456789abcdef" for c in revision)
    )
    if not valid_revision:
        return None
    return revision


class ServiceRestartScheduleError(RuntimeError):
    def __init__(self, *, reason: str, detail: str, retryable: bool) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ServiceRestartSchedule:
    scope: RestartScope
    services: tuple[str, ...]
    trigger_path: Path
    scheduled_at: datetime
    already_scheduled: bool
    created: bool


@dataclass(frozen=True, slots=True)
class ServiceRestartExecution:
    state: Literal["running", "succeeded", "failed"]
    scope: RestartScope
    services: tuple[str, ...]
    request_id: str | None
    actor: str | None
    scheduled_at: str | None
    started_at: str | None
    completed_at: str | None
    git_head: str | None
    detail: str | None


class ServiceRestartScheduler:
    """Create fixed restart markers without granting the web process service control."""

    def __init__(self, trigger_dir: Path) -> None:
        self._trigger_dir = trigger_dir.expanduser().resolve()

    @property
    def trigger_dir(self) -> Path:
        return self._trigger_dir

    def ready(self) -> bool:
        try:
            status_path = self._trigger_dir / _STATUS_FILE
            return (
                self._trigger_dir.is_dir()
                and not self._trigger_dir.is_symlink()
                and os.access(self._trigger_dir, os.W_OK | os.X_OK)
                and status_path.is_file()
                and not status_path.is_symlink()
                and os.access(status_path, os.R_OK)
            )
        except OSError:
            return False

    def in_progress(self) -> bool:
        execution = self.execution_status()
        return execution is not None and execution.state == "running"

    def pending_scope(self) -> RestartScope | None:
        for scope, filename in _TRIGGER_FILES.items():
            if (self._trigger_dir / filename).exists():
                return scope
        return None

    def execution_status(self) -> ServiceRestartExecution | None:
        """Read the root controller's bounded, fixed-name execution result."""
        path = self._trigger_dir / _STATUS_FILE
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except (FileNotFoundError, PermissionError, OSError):
            return None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 65536:
                return None
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                payload = json.loads(handle.read().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if not isinstance(payload, dict):
            return None
        state = payload.get("state")
        scope = payload.get("scope")
        if state not in _EXECUTION_STATES or scope not in _TRIGGER_FILES:
            return None

        def optional_text(key: str, *, limit: int = 512) -> str | None:
            value = payload.get(key)
            if not isinstance(value, str):
                return None
            normalized = value.strip()
            return normalized[:limit] or None

        return ServiceRestartExecution(
            state=state,
            scope=scope,
            services=_SERVICES[scope],
            request_id=optional_text("request_id", limit=64),
            actor=optional_text("actor", limit=255),
            scheduled_at=optional_text("scheduled_at", limit=64),
            started_at=optional_text("started_at", limit=64),
            completed_at=optional_text("completed_at", limit=64),
            git_head=optional_text("git_head", limit=40),
            detail=optional_text("detail"),
        )

    def schedule(
        self,
        scope: RestartScope,
        *,
        request_id: UUID,
        actor: str,
    ) -> ServiceRestartSchedule:
        if scope not in _TRIGGER_FILES:
            raise ValueError(f"unsupported restart scope: {scope}")
        if not self.ready():
            raise ServiceRestartScheduleError(
                reason="MODULE_SERVICE_RESTART_NOT_CONFIGURED",
                detail=(
                    "Module service restart controller is not configured or its trigger "
                    "directory is not writable by the Backend."
                ),
                retryable=False,
            )

        if self.in_progress():
            raise ServiceRestartScheduleError(
                reason="MODULE_SERVICE_RESTART_IN_PROGRESS",
                detail="A module service restart is already in progress.",
                retryable=True,
            )

        requested_path = self._trigger_dir / _TRIGGER_FILES[scope]
        scheduled_at = datetime.now(timezone.utc)
        if requested_path.exists():
            return ServiceRestartSchedule(
                scope=scope,
                services=_SERVICES[scope],
                trigger_path=requested_path,
                scheduled_at=scheduled_at,
                already_scheduled=True,
                created=False,
            )

        # Do not queue overlapping scopes. A stack restart and a single-service restart
        # at the same time can otherwise cause duplicate service bounces.
        for other_scope, filename in _TRIGGER_FILES.items():
            if other_scope != scope and (self._trigger_dir / filename).exists():
                raise ServiceRestartScheduleError(
                    reason="MODULE_SERVICE_RESTART_ALREADY_SCHEDULED",
                    detail="Another module service restart has already been scheduled.",
                    retryable=True,
                )

        payload = json.dumps(
            {
                "scope": scope,
                "request_id": str(request_id),
                "actor": actor,
                "scheduled_at": scheduled_at.isoformat(),
                "git_head": _repository_git_head(),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(requested_path, flags, 0o640)
        except FileExistsError:
            return ServiceRestartSchedule(
                scope=scope,
                services=_SERVICES[scope],
                trigger_path=requested_path,
                scheduled_at=scheduled_at,
                already_scheduled=True,
                created=False,
            )
        except OSError as exc:
            raise ServiceRestartScheduleError(
                reason="MODULE_SERVICE_RESTART_SCHEDULE_FAILED",
                detail="The fixed module service restart trigger could not be created.",
                retryable=True,
            ) from exc

        try:
            with os.fdopen(descriptor, "wb", closefd=True) as marker:
                marker.write(payload)
                marker.flush()
                os.fsync(marker.fileno())
        except OSError as exc:
            requested_path.unlink(missing_ok=True)
            raise ServiceRestartScheduleError(
                reason="MODULE_SERVICE_RESTART_SCHEDULE_FAILED",
                detail="The module service restart trigger could not be persisted.",
                retryable=True,
            ) from exc

        return ServiceRestartSchedule(
            scope=scope,
            services=_SERVICES[scope],
            trigger_path=requested_path,
            scheduled_at=scheduled_at,
            already_scheduled=False,
            created=True,
        )

    def cancel(self, schedule: ServiceRestartSchedule) -> None:
        """Best-effort cancellation used only if audit persistence fails before response."""
        if not schedule.created:
            return
        try:
            schedule.trigger_path.unlink(missing_ok=True)
        except OSError:
            # The root-owned controller may already have consumed the marker.  There is
            # nothing useful the unprivileged Backend can do at that point.
            return


def restart_services(scope: RestartScope) -> tuple[str, ...]:
    return _SERVICES[scope]
