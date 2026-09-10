"""File-backed users, password verification, sessions, and login throttling."""

from __future__ import annotations

import json
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from pwdlib import PasswordHash

ROLES = ("viewer", "operator", "admin")


@dataclass(frozen=True, slots=True)
class AdminUser:
    username: str
    password_hash: str
    role: str
    active: bool


def load_users(path: Path) -> dict[str, AdminUser]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Admin users file could not be loaded") from exc
    entries = raw.get("users") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        raise ValueError("Admin users file must contain a JSON array")

    users: dict[str, AdminUser] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Each Admin user must be a JSON object")
        username = entry.get("username")
        password_hash = entry.get("password_hash")
        role = entry.get("role")
        active = entry.get("active")
        if not isinstance(username, str) or not username or username != username.strip():
            raise ValueError("Admin usernames must be non-blank trimmed strings")
        if not isinstance(password_hash, str) or not password_hash:
            raise ValueError("Admin password_hash must be a non-blank string")
        if role not in ROLES:
            raise ValueError("Admin role must be viewer, operator, or admin")
        if not isinstance(active, bool):
            raise ValueError("Admin active must be a boolean")
        if username in users:
            raise ValueError("Admin usernames must be unique")
        users[username] = AdminUser(username, password_hash, role, active)
    return users


class UserAuthenticator:
    def __init__(self, users: dict[str, AdminUser]) -> None:
        self._users = users
        self._passwords = PasswordHash.recommended()
        self._dummy_hash = self._passwords.hash(secrets.token_urlsafe(24))

    def authenticate(self, username: str, password: str) -> AdminUser | None:
        user = self._users.get(username)
        candidate_hash = user.password_hash if user is not None else self._dummy_hash
        try:
            valid = self._passwords.verify(password, candidate_hash)
        except Exception:
            valid = False
        if user is None or not user.active or not valid:
            return None
        return user


class LoginRateLimiter:
    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._max_attempts = max_attempts
        self._window_seconds = window_seconds
        self._clock = clock
        self._attempts: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()

    def check(self, key: str) -> int | None:
        now = self._clock()
        with self._lock:
            attempts = self._attempts[key]
            while attempts and attempts[0] <= now - self._window_seconds:
                attempts.popleft()
            if len(attempts) < self._max_attempts:
                return None
            return max(1, int(self._window_seconds - (now - attempts[0])))

    def record_failure(self, key: str) -> None:
        with self._lock:
            self._attempts[key].append(self._clock())

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)

