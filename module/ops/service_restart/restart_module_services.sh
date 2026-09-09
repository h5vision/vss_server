#!/usr/bin/env bash
set -euo pipefail

TRIGGER_DIR=/run/vss-ops
IN_PROGRESS="$TRIGGER_DIR/restart.in-progress"
LOCK_FILE="$TRIGGER_DIR/restart.lock"
BACKEND_TRIGGER="$TRIGGER_DIR/restart-snapshot-backend.request"
ADMIN_TRIGGER="$TRIGGER_DIR/restart-admin-web.request"
STACK_TRIGGER="$TRIGGER_DIR/restart-module-stack.request"

mkdir -p "$TRIGGER_DIR"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  exit 0
fi

umask 027
: > "$IN_PROGRESS"
cleanup() {
  rm -f "$IN_PROGRESS"
}
trap cleanup EXIT

# Give the Backend enough time to persist its audit row and return HTTP 202 before
# a requested restart can terminate the process that accepted the request.
sleep 4

restart_backend=0
restart_admin=0
if [[ -f "$STACK_TRIGGER" ]]; then
  restart_backend=1
  restart_admin=1
elif [[ -f "$BACKEND_TRIGGER" ]]; then
  restart_backend=1
elif [[ -f "$ADMIN_TRIGGER" ]]; then
  restart_admin=1
else
  exit 0
fi

# Consume only the fixed marker names. Unknown files can never select a unit name.
rm -f "$STACK_TRIGGER" "$BACKEND_TRIGGER" "$ADMIN_TRIGGER"

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

if [[ "$restart_backend" -eq 1 ]]; then
  systemctl restart vss-snapshot.service
  wait_active vss-snapshot.service
fi

if [[ "$restart_admin" -eq 1 ]]; then
  systemctl restart vss-admin-web.service
  wait_active vss-admin-web.service
fi
