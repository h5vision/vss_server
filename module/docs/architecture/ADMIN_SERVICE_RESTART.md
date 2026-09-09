# Admin Module Service Restart

## Purpose

Admin Web can request a restart of the Module-owned services after a deployment or configuration change without giving the web process arbitrary shell or `systemctl` authority.

The managed services are intentionally limited to:

- `vss-snapshot.service`
- `vss-admin-web.service`

VSS/pre-rag is not part of this restart stack.

## Security boundary

The request path is:

```text
Admin browser
  -> Admin Web BFF (session + CSRF + admin role)
  -> Snapshot Backend Admin API (service token + signed admin identity + admin role)
  -> fixed marker under /run/vss-ops
  -> root-owned vss-module-ops.path
  -> root-owned vss-module-ops.service
  -> fixed restart_module_services.sh
  -> systemctl restart of fixed unit names only
```

The Backend never accepts a command, script path, unit name, or arbitrary argument from the browser. It does not execute `sudo`, `systemctl`, or a user-supplied `.sh` file. The only accepted scopes are:

- `snapshot_backend`
- `admin_web`
- `module_stack`

Each scope maps to a fixed marker filename and a fixed service list in source code.

## Admin API

Status:

```http
GET /v1/admin/runtime/services
```

Admin role is required. The response reports whether the Backend can write to the fixed trigger channel:

```json
{
  "ok": true,
  "trigger_ready": true,
  "scopes": ["snapshot_backend", "admin_web", "module_stack"],
  "services": ["vss-snapshot.service", "vss-admin-web.service"]
}
```

`trigger_ready` means the fixed `/run/vss-ops` request channel exists and is writable by the Backend. It is not a substitute for host-level systemd monitoring.

Restart request:

```http
POST /v1/admin/runtime/services/restart
Content-Type: application/json

{"scope":"module_stack"}
```

The Backend persists an Audit Log entry before returning `202`. A successful response means the restart was scheduled, not that the services have already restarted:

```json
{
  "ok": true,
  "reason": "MODULE_SERVICE_RESTART_SCHEDULED",
  "detail": "The requested module service restart has been scheduled.",
  "retryable": false,
  "request_id": "...",
  "scope": "module_stack",
  "services": ["vss-snapshot.service", "vss-admin-web.service"],
  "already_scheduled": false,
  "reconnect_expected": true
}
```

The request is idempotent for an already-existing marker of the same scope. Overlapping restart scopes are rejected so two independent service bounces cannot be queued accidentally.

## Admin Web behavior

Admin users see three fixed controls in the top bar:

- `Backend ↻`
- `Admin ↻`
- `Module ↻`

Every action uses the existing in-page confirmation dialog. After receiving `202`, the UI expects the connection to drop and polls the Admin runtime endpoint until the Module services are reachable again. Non-admin roles cannot see or proxy the restart routes.

## Root-owned controller

Assets live under:

```text
module/ops/service_restart/
  restart_module_services.sh
  vss-module-ops.path
  vss-module-ops.service
  vss-module-ops.tmpfiles.conf
  vss-snapshot-ops-trigger.conf
```

The controller uses `/run/vss-ops` as a volatile request channel. The directory is `root:<snapshot-service-group>` with mode `0730`; the Backend can create fixed marker files but cannot enumerate arbitrary host state. The oneshot controller is root-owned, serializes execution with `flock`, consumes only fixed marker names, and restarts only the two fixed Module services.

A four-second grace period separates marker consumption from the first restart. This allows the Backend to commit the audit entry and return HTTP `202` before it can be terminated by its own requested restart.

## Installation

Install the controller on a systemd host from the checked-out Module tree:

```bash
sudo bash module/scripts/install_admin_service_restart.sh
```

The installer:

1. detects the configured `vss-snapshot.service` group (or accepts `SNAPSHOT_OPS_GROUP`),
2. installs the root-owned helper and systemd path/oneshot units,
3. creates `/run/vss-ops` through tmpfiles with the detected service group,
4. installs a `vss-snapshot.service` drop-in granting write access only to `/run/vss-ops`,
5. enables `vss-module-ops.path`.

The installer deliberately does **not** restart `vss-snapshot.service` or `vss-admin-web.service`. After first installation, restart both services once through the host operator. The Backend restart activates its new `ReadWritePaths=/run/vss-ops` sandbox permission, and the Admin Web restart loads the new BFF allowlist and restart-control UI:

```bash
sudo systemctl restart vss-snapshot.service
sudo systemctl restart vss-admin-web.service
```

After that one-time bootstrap step, future Module service restarts can be requested from Admin Web.

## Audit

Successful scheduling writes:

```text
action      = restart_module_services
target_type = module_services
target_id   = snapshot_backend | admin_web | module_stack
```

If the audit record cannot be persisted, the Backend attempts to cancel the marker before returning an error. No unaudited restart is intentionally issued by the API.

## Failure semantics

- `MODULE_SERVICE_RESTART_NOT_CONFIGURED`: trigger channel is missing or not writable.
- `MODULE_SERVICE_RESTART_IN_PROGRESS`: the root controller is already processing a restart.
- `MODULE_SERVICE_RESTART_ALREADY_SCHEDULED`: another scope is already queued.
- `MODULE_SERVICE_RESTART_SCHEDULE_FAILED`: marker creation/persistence failed.
- `MODULE_SERVICE_RESTART_AUDIT_FAILED`: audit persistence failed; the marker is cancelled when still possible.

The Admin UI never treats the `202` scheduling response as proof of restart completion. Runtime recovery is established by reconnecting after the expected service interruption.
