#!/usr/bin/env bash
set -Eeuo pipefail

umask 077

module_root="${SNAPSHOT_AWS_MODULE_ROOT:-/home/ubuntu/vss_server/module}"
module_env="${SNAPSHOT_AWS_MODULE_ENV:-/etc/vss-snapshot/module.env}"
admin_env="${SNAPSHOT_AWS_ADMIN_ENV:-/etc/vss-snapshot/admin-web.env}"
service_user="${SNAPSHOT_AWS_SERVICE_USER:-ubuntu}"
service_python="${SNAPSHOT_SERVICE_PYTHON:-${module_root}/.venv/bin/python}"
backend_url="${SNAPSHOT_AWS_BACKEND_URL:-http://127.0.0.1:8000}"
vss_url="${SNAPSHOT_AWS_VSS_URL:-http://127.0.0.1:8200}"
project_id=""
repository_id=""
poll_seconds=90
run_sync=false
apply_migration=false
restart_services=false
skip_sandbox=false

usage() {
    cat <<'EOF'
Usage: scripts/verify_aws_runtime.sh [options]

Default mode checks the real AWS systemd services, PostgreSQL/VSS readiness,
Alembic head, and an exact VSS project when --project-id is supplied.

Options:
  --project-id ID       Exact VSS project ID for pull/status checks.
  --repository-id ID    Exact Repository UUID required by --run-sync.
  --run-sync            Trigger one signed operator sync and poll VSS completion.
  --migrate             Run the real `alembic upgrade head`.
  --restart             Restart Backend and Admin systemd services.
  --poll-seconds N      VSS completion polling limit, 1-600 (default: 90).
  --skip-sandbox        Skip verify_module_sandbox.sh.
  --help                Show this help.

The script never measures sLLM/Ollama performance and never prints token, DSN,
provider response body, server-local project_root, or Git stderr.
EOF
}

fail() {
    printf '[FAIL] %s\n' "$1" >&2
    exit 1
}

pass() {
    printf '[PASS] %s\n' "$1"
}

wait_message() {
    printf '[WAIT] %s\n' "$1"
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

if [[ "${EUID}" -ne 0 ]]; then
    exec sudo -n env \
        "SNAPSHOT_AWS_MODULE_ROOT=${module_root}" \
        "SNAPSHOT_AWS_MODULE_ENV=${module_env}" \
        "SNAPSHOT_AWS_ADMIN_ENV=${admin_env}" \
        "SNAPSHOT_AWS_SERVICE_USER=${service_user}" \
        "SNAPSHOT_SERVICE_PYTHON=${service_python}" \
        "SNAPSHOT_AWS_BACKEND_URL=${backend_url}" \
        "SNAPSHOT_AWS_VSS_URL=${vss_url}" \
        bash "$0" "$@"
fi

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --project-id)
            [[ "$#" -ge 2 ]] || fail '--project-id 값이 없습니다.'
            project_id="$2"
            shift 2
            ;;
        --repository-id)
            [[ "$#" -ge 2 ]] || fail '--repository-id 값이 없습니다.'
            repository_id="$2"
            shift 2
            ;;
        --run-sync)
            run_sync=true
            shift
            ;;
        --migrate)
            apply_migration=true
            shift
            ;;
        --restart)
            restart_services=true
            shift
            ;;
        --poll-seconds)
            [[ "$#" -ge 2 ]] || fail '--poll-seconds 값이 없습니다.'
            poll_seconds="$2"
            shift 2
            ;;
        --skip-sandbox)
            skip_sandbox=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "지원하지 않는 인자입니다: $1"
            ;;
    esac
done

[[ "${poll_seconds}" =~ ^[0-9]+$ ]] || fail '--poll-seconds는 정수여야 합니다.'
(( poll_seconds >= 1 && poll_seconds <= 600 )) || fail '--poll-seconds는 1-600 범위여야 합니다.'
if [[ "${run_sync}" == true ]]; then
    [[ "${repository_id}" =~ ^[0-9a-fA-F-]{36}$ ]] || fail '--run-sync에는 Repository UUID가 필요합니다.'
    [[ -n "${project_id}" ]] || fail '--run-sync에는 --project-id가 필요합니다.'
fi

[[ -x "${service_python}" ]] || fail "service Python을 찾을 수 없습니다: ${service_python}"
command -v git >/dev/null 2>&1 || fail 'git 명령이 필요합니다.'
command -v curl >/dev/null 2>&1 || fail 'curl 명령이 필요합니다.'
command -v systemctl >/dev/null 2>&1 || fail 'systemctl 명령이 필요합니다.'
command -v ss >/dev/null 2>&1 || fail 'ss 명령이 필요합니다.'
[[ -r /etc/os-release ]] || fail '/etc/os-release를 읽을 수 없습니다.'
# shellcheck disable=SC1091
source /etc/os-release
[[ "${ID:-}" == ubuntu && "${VERSION_ID:-}" == 22.04 ]] || \
    fail "Ubuntu 22.04가 필요합니다: ${ID:-unknown} ${VERSION_ID:-unknown}"
pass "Ubuntu ${VERSION_ID}"

"${service_python}" - <<'PY'
import sys

if sys.version_info[:2] != (3, 10):
    raise SystemExit("AWS runtime test requires Python 3.10")
PY
pass 'module venv Python 3.10'

[[ -r "${module_env}" ]] || fail "module env를 읽을 수 없습니다: ${module_env}"
[[ -r "${admin_env}" ]] || fail "Admin env를 읽을 수 없습니다: ${admin_env}"

runtime_tmp="$(mktemp -d /tmp/vss-aws-runtime.XXXXXX)"
cleanup_runtime() {
    case "${runtime_tmp}" in
        /tmp/vss-aws-runtime.*) rm -rf -- "${runtime_tmp}" ;;
        *) printf '[WARN] 예상하지 못한 임시 경로라 정리를 건너뜁니다.\n' >&2 ;;
    esac
}
trap cleanup_runtime EXIT

if [[ "${skip_sandbox}" != true ]]; then
    runuser -u "${service_user}" -- bash "${module_root}/scripts/verify_module_sandbox.sh" --aws-contract
    pass 'module sandbox harness'
fi

service_state="$(systemctl is-active vss-snapshot.service)"
[[ "${service_state}" == active ]] || fail 'vss-snapshot.service가 active가 아닙니다.'
pass 'vss-snapshot.service active'
admin_state="$(systemctl is-active vss-admin-web.service)"
[[ "${admin_state}" == active ]] || fail 'vss-admin-web.service가 active가 아닙니다.'
pass 'vss-admin-web.service active'

unit_pre="$(systemctl show vss-snapshot.service --property=ExecStartPre --value)"
grep -q 'preflight_ubuntu_runtime.sh' <<<"${unit_pre}" || \
    fail 'vss-snapshot.service에 Ubuntu runtime preflight가 없습니다.'
pass 'systemd ExecStartPre preflight'

listen="$(ss -ltn)"
grep -Eq ':8000[[:space:]]' <<<"${listen}" || fail 'Backend 8000 listen socket이 없습니다.'
grep -Eq ':4180[[:space:]]' <<<"${listen}" || fail 'Admin Web 4180 listen socket이 없습니다.'
grep -Eq ':8200[[:space:]]' <<<"${listen}" || fail 'VSS 8200 listen socket이 없습니다.'
pass 'Backend/Admin/VSS listen sockets'

set -a
# shellcheck disable=SC1090
source "${module_env}"
# shellcheck disable=SC1090
source "${admin_env}"
set +a

curl --fail --silent --show-error --max-time 10 \
    "${backend_url%/}/v1/health" >"${runtime_tmp}/backend-health.json"
"${service_python}" - "${runtime_tmp}/backend-health.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("ok") is not True:
    raise SystemExit("Backend liveness is not healthy")
PY
pass 'Backend liveness'

curl --fail --silent --show-error --max-time 15 \
    "${backend_url%/}/v1/health/ready" >"${runtime_tmp}/backend-ready.json"
"${service_python}" - "${runtime_tmp}/backend-ready.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("ok") is not True or payload.get("status") != "ready":
    raise SystemExit("Backend readiness is not ready")
PY
pass 'Backend DB/VSS readiness'

vss_headers=(-H 'Accept: application/json')
if [[ -n "${VSS_TOKEN:-}" ]]; then
    vss_headers+=(-H "X-VSS-Token: ${VSS_TOKEN}")
fi
curl --fail --silent --show-error --max-time 15 \
    "${vss_headers[@]}" "${vss_url%/}/health" >"${runtime_tmp}/vss-health.json"
"${service_python}" - "${runtime_tmp}/vss-health.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("ok") is not True:
    raise SystemExit("VSS health is not ready")
for key in ("store", "ollama", "embed_model"):
    if not payload.get(key):
        raise SystemExit(f"VSS health field missing: {key}")
PY
pass 'VSS health/store dependency'

if [[ "${apply_migration}" == true ]]; then
    cd "${module_root}"
    "${service_python}" -m alembic upgrade head >"${runtime_tmp}/migration.log" 2>&1
    pass 'real Alembic upgrade head'
fi

cd "${module_root}"
current_migration="$(${service_python} -m alembic current 2>/dev/null)"
grep -q '0008_repository_tags' <<<"${current_migration}" || \
    fail 'Alembic 0008_repository_tags가 적용되지 않았습니다. --migrate를 사용하십시오.'
pass 'Alembic current 0008_repository_tags'

if [[ "${restart_services}" == true ]]; then
    systemctl restart vss-snapshot.service
    systemctl restart vss-admin-web.service
    sleep 3
    systemctl is-active --quiet vss-snapshot.service
    systemctl is-active --quiet vss-admin-web.service
    curl --fail --silent --show-error --max-time 15 \
        "${backend_url%/}/v1/health/ready" >"${runtime_tmp}/backend-ready-restart.json"
    pass 'systemd restart and readiness'
fi

if [[ -n "${project_id}" ]]; then
    [[ -n "${SNAPSHOT_VSS_API_TOKEN:-}" ]] || \
        fail "SNAPSHOT_VSS_API_TOKEN이 없습니다. 설정 경로: ${SNAPSHOT_VSS_API_TOKEN_CONFIG_PATH:-/etc/vss-snapshot/module.env}"
    project_query="$(${service_python} -c 'from urllib.parse import quote; import sys; print(quote(sys.argv[1], safe=""))' "${project_id}")"

    pull_revisions() {
        curl --fail --silent --show-error --max-time 15 \
            -H "X-Snapshot-Token: ${SNAPSHOT_VSS_API_TOKEN}" \
            "${backend_url%/}/v1/internal/vss/revisions?project_id=${project_query}&limit=500" \
            >"${runtime_tmp}/revisions.json"
        "${service_python}" - "${runtime_tmp}/revisions.json" <<'PY'
import json
import sys
from pathlib import Path

filename = Path(sys.argv[1])
payload = json.loads(filename.read_text(encoding="utf-8"))
items = payload.get("items", [])
if payload.get("ok") is not True or not items:
    raise SystemExit("VSS project has no readable Snapshot revisions")
latest = max(items, key=lambda item: item.get("updated_at", ""))
(filename.parent / "latest-target.txt").write_text(
    latest["target_revision"], encoding="ascii"
)
print(f"revisions={len(items)} latest_target={latest['target_revision']}")
PY
        pass 'VSS Snapshot revisions pull'
    }

    pull_revisions
    curl --fail --silent --show-error --max-time 15 \
        -H "X-Snapshot-Token: ${SNAPSHOT_VSS_API_TOKEN}" \
        "${backend_url%/}/v1/internal/vss/change-requests?project_id=${project_query}&limit=500" \
        >"${runtime_tmp}/change-requests.json"
    "${service_python}" - "${runtime_tmp}/change-requests.json" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("ok") is not True:
    raise SystemExit("PR/MR context pull failed")
print(f"change_requests={len(payload.get('items', []))}")
PY
    pass 'VSS PR/MR context pull'

    check_project() {
        curl --fail --silent --show-error --max-time 15 \
            "${vss_headers[@]}" \
            "${vss_url%/}/index/status?project_id=${project_query}" \
            >"${runtime_tmp}/vss-status.json"
        "${service_python}" - "${runtime_tmp}" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
status = json.loads((root / "vss-status.json").read_text(encoding="utf-8"))
target = (root / "latest-target.txt").read_text(encoding="ascii").strip()
state = status.get("state")
commit = ((status.get("index") or {}).get("commit"))
if state == "done" and commit == target:
    print(f"state=done exact_commit={commit}")
    raise SystemExit(0)
if state in {"none", "running", "indexing_lexical", "promoting"}:
    print(f"state={state} index_commit={commit or 'none'}")
    raise SystemExit(2)
print(f"state={state} index_commit={commit or 'none'}")
raise SystemExit(1)
PY
    }

    if [[ "${run_sync}" == true ]]; then
        "${service_python}" - "${backend_url}" "${repository_id}" <<'PY'
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

base_url, repository_id = sys.argv[1:]
method = "POST"
path = f"/v1/admin/repositories/{repository_id}/sync"
body = b""
actor = "aws-runtime-check"
role = "operator"
timestamp = str(int(time.time()))
request_id = str(uuid.uuid4())
content_sha256 = hashlib.sha256(body).hexdigest()
canonical = "\n".join(
    (method, path, content_sha256, actor, role, timestamp, request_id)
).encode("utf-8")
service_token = os.environ.get("SNAPSHOT_ADMIN_SERVICE_TOKEN") or os.environ.get(
    "ADMIN_WEB_BACKEND_SERVICE_TOKEN"
)
signing_secret = os.environ.get("SNAPSHOT_ADMIN_IDENTITY_SECRET") or os.environ.get(
    "ADMIN_WEB_BACKEND_SIGNING_SECRET"
)
if not service_token or not signing_secret:
    raise SystemExit("Admin signing environment is missing")
signature = hmac.new(
    signing_secret.encode("utf-8"), canonical, hashlib.sha256
).hexdigest()
request = urllib.request.Request(
    f"{base_url.rstrip('/')}{path}",
    data=body,
    method=method,
    headers={
        "Accept": "application/json",
        "Authorization": f"Bearer {service_token}",
        "X-Admin-Actor": actor,
        "X-Admin-Role": role,
        "X-Admin-Timestamp": timestamp,
        "X-Admin-Request-ID": request_id,
        "X-Admin-Content-SHA256": content_sha256,
        "X-Admin-Signature": signature,
    },
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
except urllib.error.HTTPError as exc:
    try:
        payload = json.load(exc)
    except (ValueError, json.JSONDecodeError):
        print(f"sync HTTP {exc.code}", file=sys.stderr)
        raise SystemExit(1)
    print(f"sync rejected reason={payload.get('reason', 'unknown')}", file=sys.stderr)
    raise SystemExit(1)
if payload.get("ok") is False:
    print(f"sync failed reason={payload.get('reason', 'unknown')}", file=sys.stderr)
    raise SystemExit(1)
resource = payload.get("resource") or {}
print(f"sync reason={payload.get('reason', 'unknown')} outcomes={len(resource.get('outcomes', []))}")
PY
        pass 'signed Repository sync request'
        pull_revisions
        deadline=$((SECONDS + poll_seconds))
        completed=false
        while (( SECONDS < deadline )); do
            if check_project; then
                pass 'VSS done + exact target commit'
                completed=true
                break
            else
                check_code=$?
            fi
            [[ "${check_code}" == 2 ]] || fail 'VSS failed or revision mismatch입니다.'
            sleep 2
            pull_revisions
        done
        [[ "${completed}" == true ]] || fail 'VSS completion polling timeout입니다.'
    else
        if check_project; then
            pass 'VSS done + exact target commit'
        else
            check_code=$?
            if [[ "${check_code}" == 2 ]]; then
                wait_message 'VSS index가 진행 중입니다. --run-sync로 완료 polling을 수행하십시오.'
            else
                fail 'VSS 상태가 done + exact target commit이 아닙니다.'
            fi
        fi
    fi

    if [[ -f "${runtime_tmp}/latest-target.txt" ]]; then
        if curl --fail --silent --show-error --max-time 15 \
            -H "X-Snapshot-Token: ${SNAPSHOT_VSS_API_TOKEN}" \
            "${backend_url%/}/v1/internal/vss/source?project_id=${project_query}" \
            >"${runtime_tmp}/source.json"; then
            "${service_python}" - "${runtime_tmp}" <<'PY'
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
payload = json.loads((root / "source.json").read_text(encoding="utf-8"))
target = (root / "latest-target.txt").read_text(encoding="ascii").strip()
if payload.get("ok") is not True:
    raise SystemExit("source descriptor is not ready")
if payload.get("target_revision") != target:
    raise SystemExit("source descriptor target revision mismatch")
if payload.get("verification", {}).get("expected_commit_sha") != target:
    raise SystemExit("source descriptor commit verification mismatch")
PY
            pass 'VSS exact source descriptor and Git verification'
        else
            [[ "${run_sync}" != true ]] || fail 'exact Snapshot source descriptor를 읽지 못했습니다.'
            wait_message 'latest Snapshot source descriptor가 아직 materialize되지 않았습니다.'
        fi
    fi
fi

echo 'AWS RUNTIME VERIFICATION: PASS'
