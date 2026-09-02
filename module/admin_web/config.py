"""Environment and security configuration for the independent Admin Web."""

from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


@dataclass(frozen=True, slots=True)
class AdminWebSettings:
    users_file: Path
    session_secret: str
    backend_url: str
    backend_service_token: str
    backend_signing_secret: str
    allowed_origins: tuple[str, ...] = field(
        default_factory=lambda: (
            "http://127.0.0.1:4180",
            "http://localhost:4180",
        )
    )
    secure_cookies: bool = True
    session_max_age_seconds: int = 30 * 60
    login_max_attempts: int = 5
    login_window_seconds: int = 60
    backend_timeout_seconds: float = 30.0
    max_request_body_bytes: int = 1024 * 1024

    def __post_init__(self) -> None:
        object.__setattr__(self, "users_file", Path(self.users_file).resolve())
        object.__setattr__(self, "backend_url", self.backend_url.rstrip("/"))
        self._validate_secret("session_secret", self.session_secret)
        self._validate_secret("backend_signing_secret", self.backend_signing_secret)
        if len(self.backend_service_token) < 24:
            raise ValueError("backend_service_token must contain at least 24 characters")
        credentials = (
            self.session_secret,
            self.backend_service_token,
            self.backend_signing_secret,
        )
        if any(
            hmac.compare_digest(left, right)
            for index, left in enumerate(credentials)
            for right in credentials[index + 1 :]
        ):
            raise ValueError("Admin Web secrets must all be different")
        if self.session_max_age_seconds != 1800:
            raise ValueError("session_max_age_seconds must be 1800")
        if self.login_max_attempts < 1 or self.login_window_seconds < 1:
            raise ValueError("login rate limit values must be positive")
        if self.max_request_body_bytes < 1:
            raise ValueError("max_request_body_bytes must be positive")
        self._validate_backend_url()
        self._validate_origins()

    @staticmethod
    def _validate_secret(name: str, value: str) -> None:
        if len(value.encode("utf-8")) < 32:
            raise ValueError(f"{name} must be at least 32 bytes")

    def _validate_backend_url(self) -> None:
        parsed = urlsplit(self.backend_url)
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or parsed.hostname is None
        ):
            raise ValueError("backend_url must be an HTTP loopback origin")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError as exc:
            raise ValueError("backend_url host must be a loopback IP address") from exc
        if not address.is_loopback:
            raise ValueError("backend_url host must be loopback")

    def _validate_origins(self) -> None:
        if not self.allowed_origins:
            raise ValueError("allowed_origins must not be empty")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowed_origins entries must be HTTP origins")

    @classmethod
    def from_environment(cls) -> AdminWebSettings:
        required = {
            "users_file": os.getenv("ADMIN_WEB_USERS_FILE"),
            "session_secret": os.getenv("ADMIN_WEB_SESSION_SECRET"),
            "backend_service_token": os.getenv("ADMIN_WEB_BACKEND_SERVICE_TOKEN"),
            "backend_signing_secret": os.getenv("ADMIN_WEB_BACKEND_SIGNING_SECRET"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            names = ", ".join(f"ADMIN_WEB_{name.upper()}" for name in missing)
            raise ValueError(f"Missing required Admin Web configuration: {names}")
        origins_raw = os.getenv(
            "ADMIN_WEB_ALLOWED_ORIGINS",
            "http://127.0.0.1:4180,http://localhost:4180",
        )
        return cls(
            users_file=Path(required["users_file"] or ""),
            session_secret=required["session_secret"] or "",
            backend_url=os.getenv("ADMIN_WEB_BACKEND_URL", "http://127.0.0.1:8000"),
            backend_service_token=required["backend_service_token"] or "",
            backend_signing_secret=required["backend_signing_secret"] or "",
            allowed_origins=tuple(
                origin.strip() for origin in origins_raw.split(",") if origin.strip()
            ),
            secure_cookies=_env_bool("ADMIN_WEB_SECURE_COOKIES", True),
            max_request_body_bytes=_env_int(
                "ADMIN_WEB_MAX_REQUEST_BODY_BYTES",
                1024 * 1024,
            ),
        )
