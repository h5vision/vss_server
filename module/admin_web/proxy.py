"""Strict Admin API allowlist and backend request signing."""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from uuid import UUID

ROLE_LEVEL = {"viewer": 0, "operator": 1, "admin": 2}
UUID_PATTERN = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True, slots=True)
class RouteRule:
    pattern: re.Pattern[str]
    methods: dict[str, str]


RULES = (
    RouteRule(re.compile(r"repositories"), {"GET": "viewer", "POST": "admin"}),
    RouteRule(
        re.compile(rf"repositories/{UUID_PATTERN}"),
        {"GET": "viewer", "PATCH": "admin", "DELETE": "admin"},
    ),
    RouteRule(
        re.compile(rf"repositories/{UUID_PATTERN}/branches"), {"GET": "viewer"}
    ),
    RouteRule(
        re.compile(rf"repositories/{UUID_PATTERN}/commits"), {"GET": "viewer"}
    ),
    RouteRule(
        re.compile(rf"repositories/{UUID_PATTERN}/commits/[0-9a-fA-F]{{40}}"),
        {"GET": "viewer"},
    ),
    RouteRule(
        re.compile(rf"repositories/{UUID_PATTERN}/compare"), {"GET": "operator"}
    ),
    RouteRule(
        re.compile(rf"repositories/{UUID_PATTERN}/sync"), {"POST": "operator"}
    ),
    RouteRule(re.compile(r"repository-sync-runs"), {"GET": "viewer"}),
    RouteRule(re.compile(r"tracked-branches"), {"GET": "viewer", "POST": "admin"}),
    RouteRule(
        re.compile(rf"tracked-branches/{UUID_PATTERN}"),
        {"PATCH": "admin", "DELETE": "admin"},
    ),
    RouteRule(
        re.compile(rf"tracked-branches/{UUID_PATTERN}/head-history"),
        {"GET": "viewer"},
    ),
    RouteRule(re.compile(r"branch-bindings"), {"GET": "viewer", "POST": "admin"}),
    RouteRule(
        re.compile(rf"branch-bindings/{UUID_PATTERN}"),
        {"PATCH": "admin", "DELETE": "admin"},
    ),
    RouteRule(re.compile(r"snapshots"), {"GET": "viewer"}),
    RouteRule(re.compile(rf"snapshots/{UUID_PATTERN}"), {"GET": "viewer"}),
    RouteRule(
        re.compile(rf"snapshots/{UUID_PATTERN}/retry"), {"POST": "operator"}
    ),
    RouteRule(re.compile(r"vss/projects"), {"GET": "viewer"}),
    RouteRule(re.compile(r"audit-logs"), {"GET": "admin"}),
)


def required_role(path: str, method: str) -> tuple[str | None, bool]:
    for rule in RULES:
        if rule.pattern.fullmatch(path):
            return rule.methods.get(method), True
    return None, False


def role_allows(actual: str, required: str) -> bool:
    return ROLE_LEVEL.get(actual, -1) >= ROLE_LEVEL[required]


def signed_headers(
    *,
    method: str,
    path_with_raw_query: str,
    body: bytes,
    actor: str,
    role: str,
    timestamp: int,
    request_id: UUID,
    service_token: str,
    signing_secret: str,
) -> dict[str, str]:
    content_hash = hashlib.sha256(body).hexdigest()
    canonical = "\n".join(
        (
            method.upper(),
            path_with_raw_query,
            content_hash,
            actor,
            role,
            str(timestamp),
            str(request_id),
        )
    )
    signature = hmac.new(
        signing_secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        "Authorization": f"Bearer {service_token}",
        "X-Admin-Actor": actor,
        "X-Admin-Role": role,
        "X-Admin-Timestamp": str(timestamp),
        "X-Admin-Request-ID": str(request_id),
        "X-Admin-Content-SHA256": content_hash,
        "X-Admin-Signature": signature,
    }
