"""배포된 Snapshot Backend의 읽기 전용 API를 점검한다."""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener


def fail(message: str) -> None:
    print(f"[FAIL] {message}", file=sys.stderr)
    raise SystemExit(1)


def read_json(base_url: str, path: str, *, query: dict[str, str] | None = None) -> dict[str, Any]:
    suffix = f"?{urlencode(query)}" if query else ""
    request = Request(f"{base_url}{path}{suffix}", headers={"Accept": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            if response.status != 200:
                fail(f"{path}가 HTTP 200을 반환하지 않았습니다.")
            payload = json.load(response)
    except HTTPError as exc:
        fail(f"{path}가 HTTP {exc.code}를 반환했습니다.")
    except (URLError, TimeoutError, ValueError):
        fail(f"{path} 응답을 안전하게 읽지 못했습니다.")
    if not isinstance(payload, dict):
        fail(f"{path} 응답이 JSON object가 아닙니다.")
    return payload


def main() -> None:
    raw_base_url = os.environ.get("SNAPSHOT_BACKEND_BASE_URL", "")
    parsed = urlsplit(raw_base_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        fail("SNAPSHOT_BACKEND_BASE_URL 환경변수에 HTTP(S) URL이 필요합니다.")
    if parsed.username is not None or parsed.password is not None:
        fail("SNAPSHOT_BACKEND_BASE_URL에 계정정보를 포함하지 마세요.")
    base_url = raw_base_url.rstrip("/")

    live = read_json(base_url, "/v1/health")
    if live.get("ok") is not True:
        fail("Backend liveness의 ok가 true가 아닙니다.")
    print("[PASS] Backend liveness")

    ready = read_json(base_url, "/v1/health/ready")
    if ready.get("ok") is not True or ready.get("status") != "ready":
        fail("Backend readiness가 준비 상태가 아닙니다.")
    print("[PASS] Backend DB/VSS readiness")

    project_id = os.environ.get("SNAPSHOT_TEST_PROJECT_ID", "").strip()
    if not project_id:
        print("[WAIT] SNAPSHOT_TEST_PROJECT_ID가 없어 status 조회를 건너뜁니다.")
        return

    status = read_json(base_url, "/v1/index/status", query={"project_id": project_id})
    required = {"reason", "detail", "retryable", "snapshot_id", "state", "target_revision"}
    if missing := required.difference(status):
        fail(f"index status 응답에 필수 필드가 없습니다: {', '.join(sorted(missing))}")
    if status.get("state") == "completed" and status.get("reason") not in {
        "VSS_INDEX_COMPLETED",
        "TARGET_ALREADY_INDEXED",
    }:
        fail("완료 상태의 reason이 exact revision 완료 사유가 아닙니다.")
    print(f"[PASS] Snapshot status 계약 확인: state={status['state']}, reason={status['reason']}")


if __name__ == "__main__":
    main()
