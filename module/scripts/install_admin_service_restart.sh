#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "[FAIL] run this installer as root (for example: sudo bash $0)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
MODULE_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
ASSET_DIR="$MODULE_ROOT/ops/service_restart"
LIBEXEC_DIR=/usr/local/libexec/vss-module-ops
SYSTEMD_DIR=/etc/systemd/system
TMPFILES_DIR=/etc/tmpfiles.d
SNAPSHOT_DROPIN_DIR=/etc/systemd/system/vss-snapshot.service.d

required=(
  "$ASSET_DIR/restart_module_services.sh"
  "$ASSET_DIR/vss-module-ops.path"
  "$ASSET_DIR/vss-module-ops.service"
  "$ASSET_DIR/vss-module-ops.tmpfiles.conf"
  "$ASSET_DIR/vss-snapshot-ops-trigger.conf"
)
for path in "${required[@]}"; do
  if [[ ! -f "$path" ]]; then
    echo "[FAIL] required asset is missing: $path" >&2
    exit 1
  fi
done

snapshot_group="${SNAPSHOT_OPS_GROUP:-$(systemctl show -p Group --value vss-snapshot.service 2>/dev/null || true)}"
if [[ -z "$snapshot_group" ]]; then
  snapshot_user="$(systemctl show -p User --value vss-snapshot.service 2>/dev/null || true)"
  if [[ -n "$snapshot_user" ]]; then
    snapshot_group="$(id -gn "$snapshot_user" 2>/dev/null || true)"
  fi
fi
if [[ -z "$snapshot_group" ]] || ! getent group "$snapshot_group" >/dev/null; then
  echo "[FAIL] could not determine the vss-snapshot.service group" >&2
  echo "       set SNAPSHOT_OPS_GROUP to an existing group and retry" >&2
  exit 1
fi
if [[ ! "$snapshot_group" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
  echo "[FAIL] unsafe service group name: $snapshot_group" >&2
  exit 1
fi

install -d -o root -g root -m 0755 "$LIBEXEC_DIR"
install -o root -g root -m 0755 \
  "$ASSET_DIR/restart_module_services.sh" \
  "$LIBEXEC_DIR/restart_module_services.sh"
install -o root -g root -m 0644 \
  "$ASSET_DIR/vss-module-ops.path" \
  "$SYSTEMD_DIR/vss-module-ops.path"
install -o root -g root -m 0644 \
  "$ASSET_DIR/vss-module-ops.service" \
  "$SYSTEMD_DIR/vss-module-ops.service"
sed "s/@SNAPSHOT_GROUP@/$snapshot_group/g" \
  "$ASSET_DIR/vss-module-ops.tmpfiles.conf" > "$TMPFILES_DIR/vss-module-ops.conf"
chown root:root "$TMPFILES_DIR/vss-module-ops.conf"
chmod 0644 "$TMPFILES_DIR/vss-module-ops.conf"
install -d -o root -g root -m 0755 "$SNAPSHOT_DROPIN_DIR"
install -o root -g root -m 0644 \
  "$ASSET_DIR/vss-snapshot-ops-trigger.conf" \
  "$SNAPSHOT_DROPIN_DIR/ops-trigger.conf"

systemd-tmpfiles --create "$TMPFILES_DIR/vss-module-ops.conf"
systemctl daemon-reload
systemctl enable --now vss-module-ops.path

echo "[OK] fixed module service restart controller installed"
echo "[INFO] trigger directory: /run/vss-ops (root:$snapshot_group, mode 1730)"
echo "[INFO] restart status: /run/vss-ops/restart-status.json"
echo "[INFO] vss-module-ops.path is enabled and active"
echo "[NEXT] restart both Module services once so the new Backend permission and Admin Web proxy/UI code are loaded"
echo "       sudo systemctl restart vss-snapshot.service"
echo "       sudo systemctl restart vss-admin-web.service"
echo "[NOTE] this installer intentionally does not restart either service by itself"
