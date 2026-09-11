#!/usr/bin/env bash
set -euo pipefail

TRIGGER_DIR=/run/vss-ops
LOCK_FILE="$TRIGGER_DIR/restart.lock"
STATUS_FILE="$TRIGGER_DIR/restart-status.json"
BACKEND_TRIGGER="$TRIGGER_DIR/restart-snapshot-backend.request"
ADMIN_TRIGGER="$TRIGGER_DIR/restart-admin-web.request"
STACK_TRIGGER="$TRIGGER_DIR/restart-module-stack.request"

mkdir -p "$TRIGGER_DIR"
exec 9<>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

umask 027
execution_started=0
finished=0
stage="initializing"
scope=""
services_csv=""
started_at=""
request_payload_b64=""

write_status() {
  local state="$1"
  local detail="$2"
  local completed_at="${3:-}"
  local group_id
  group_id="$(stat -c '%g' "$TRIGGER_DIR")"
  /usr/bin/python3 - \
    "$STATUS_FILE" "$request_payload_b64" "$state" "$scope" "$services_csv" \
    "$started_at" "$completed_at" "$detail" "$group_id" <<'PY'
import base64
import json
import os
import stat
import sys

(
    status_path,
    request_payload_b64,
    state,
    scope,
    services_csv,
    started_at,
    completed_at,
    detail,
    group_id,
) = sys.argv[1:]
request = {}
try:
    decoded = base64.urlsafe_b64decode(request_payload_b64.encode("ascii"))
    parsed = json.loads(decoded.decode("utf-8"))
    if isinstance(parsed, dict):
        request = parsed
except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
    request = {}

payload = {
    "state": state,
    "scope": scope,
    "services": [value for value in services_csv.split(",") if value],
    "request_id": request.get("request_id"),
    "actor": request.get("actor"),
    "scheduled_at": request.get("scheduled_at"),
    "started_at": started_at or None,
    "completed_at": completed_at or None,
    "git_head": request.get("git_head"),
    "detail": detail or None,
}
flags = os.O_WRONLY | os.O_TRUNC
if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW
descriptor = os.open(status_path, flags)
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0:
        raise RuntimeError("restart status path is not a root-owned regular file")
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    os.write(descriptor, encoded)
    os.fsync(descriptor)
finally:
    os.close(descriptor)
PY
}

cleanup() {
  local rc=$?
  if [[ "$execution_started" -eq 1 && "$finished" -ne 1 ]]; then
    local completed_at
    completed_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    write_status "failed" "Restart failed during ${stage} (exit ${rc})." "$completed_at" || true
  fi
  trap - EXIT
  exit "$rc"
}
trap cleanup EXIT

# Give the Backend enough time to persist its audit row and return HTTP 202 before
# a requested restart can terminate the process that accepted the request.
sleep 4

restart_backend=0
restart_admin=0
selected_trigger=""
if [[ -f "$STACK_TRIGGER" ]]; then
  scope="module_stack"
  services_csv="vss-snapshot.service,vss-admin-web.service"
  restart_backend=1
  restart_admin=1
  selected_trigger="$STACK_TRIGGER"
elif [[ -f "$BACKEND_TRIGGER" ]]; then
  scope="snapshot_backend"
  services_csv="vss-snapshot.service"
  restart_backend=1
  selected_trigger="$BACKEND_TRIGGER"
elif [[ -f "$ADMIN_TRIGGER" ]]; then
  scope="admin_web"
  services_csv="vss-admin-web.service"
  restart_admin=1
  selected_trigger="$ADMIN_TRIGGER"
else
  finished=1
  exit 0
fi

# Sanitize only bounded observability metadata from the Backend-owned marker. The
# filename, not the marker payload, selects the fixed restart scope and units.
request_payload_b64="$(/usr/bin/python3 - "$selected_trigger" <<'PY'
import base64
import json
import os
import stat
import sys

allowed = ("request_id", "actor", "scheduled_at", "git_head")
payload = {}
descriptor = -1
try:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(sys.argv[1], flags)
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 4096:
        raise OSError("unsafe restart marker")
    raw = os.read(descriptor, 4097)
    parsed = json.loads(raw.decode("utf-8"))
    if isinstance(parsed, dict):
        for key in allowed:
            value = parsed.get(key)
            if isinstance(value, str):
                payload[key] = value[:512]
except (OSError, UnicodeDecodeError, json.JSONDecodeError):
    payload = {}
finally:
    if descriptor >= 0:
        os.close(descriptor)
encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
print(base64.urlsafe_b64encode(encoded).decode("ascii"))
PY
)"

# Consume only the fixed marker names. Unknown files can never select a unit name.
rm -f "$STACK_TRIGGER" "$BACKEND_TRIGGER" "$ADMIN_TRIGGER"

started_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
execution_started=1
stage="recording running state"
write_status "running" "Restart controller accepted the request." ""

wait_active() {
  local unit="$1"
  local attempt
  for attempt in $(seq 1 30); do
    if systemctl is-active --quiet "$unit"; then
      return 0
    fi
    sleep 1
  done
  systemctl status --no-pager "$unit" || true
  return 1
}

wait_http() {
  local url="$1"
  local attempt
  for attempt in $(seq 1 30); do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

if [[ "$restart_backend" -eq 1 ]]; then
  stage="restarting vss-snapshot.service"
  systemctl restart vss-snapshot.service
  wait_active vss-snapshot.service
  stage="waiting for Snapshot Backend health"
  wait_http http://127.0.0.1:8000/v1/health
fi

if [[ "$restart_admin" -eq 1 ]]; then
  stage="restarting vss-admin-web.service"
  systemctl restart vss-admin-web.service
  wait_active vss-admin-web.service
  stage="waiting for Admin Web health"
  wait_http http://127.0.0.1:4180/
fi

stage="recording successful completion"
completed_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
write_status "succeeded" "Restart completed and health checks passed." "$completed_at"
finished=1
