"use strict";

const roleLevel = { viewer: 0, operator: 1, admin: 2 };
const listPageSize = 25;
const runtimeModelRefreshMs = 15_000;
const chatMonitorRefreshMs = 3_000;
const retryableSnapshotStates = new Set(["failed", "rejected", "aborted"]);
const bindingReasons = new Set(["SNAPSHOT_DESTINATION_REQUIRED", "SNAPSHOT_DESTINATION_AMBIGUOUS"]);
const views = {
  repositories: {
    title: "Repositories",
    subtitle: "등록된 Git 저장소와 수집 상태",
    endpoint: "/v1/admin/repositories",
    columns: ["canonical_name", "display_name", "provider", "default_branch_ref", "active", "updated_at"],
  },
  "tracked-branches": {
    title: "Tracked branches",
    subtitle: "수집 대상으로 선택된 exact branch",
    endpoint: "/v1/admin/tracked-branches",
    columns: ["repository_id", "branch_ref", "vss_project_id", "current_head_sha", "tracked", "last_fetched_at"],
  },
  "branch-bindings": {
    title: "Branch bindings",
    subtitle: "Frontend workspace와 exact VSS project 연결",
    endpoint: "/v1/admin/branch-bindings",
    columns: ["frontend_project_id", "frontend_workspace_name", "repository_id", "branch_ref", "vss_project_id", "active"],
  },
  "sync-history": {
    title: "Sync history",
    subtitle: "Repository fetch와 HEAD 관측 실행 기록",
    endpoint: "/v1/admin/repository-sync-runs",
    columns: ["repository_id", "trigger", "state", "reason", "retryable", "started_at", "finished_at"],
  },
  snapshots: {
    title: "Snapshots",
    subtitle: "Revision materialization과 VSS 처리 이력",
    endpoint: "/v1/admin/snapshots",
    columns: ["repository_id", "branch_ref", "base_revision", "target_revision", "state", "vss_state", "vss_reason", "attempt_count", "updated_at"],
  },
  vss: {
    title: "VSS projects",
    subtitle: "VSS exact project catalog",
    endpoint: "/v1/admin/vss/projects",
    columns: ["project_id", "state", "commit", "chunks", "indexed_at"],
  },
  chat: {
    title: "Chat sessions",
    subtitle: "VSS Chat transcript와 embedding / sLLM 실행 trace",
    endpoint: "/v1/admin/chat/conversations",
    columns: [],
  },
  commits: {
    title: "Commits",
    subtitle: "Repository commit graph 및 availability 상태",
    endpoint: "/v1/admin/repositories",
    columns: ["select", "commit_sha", "subject", "author_name", "committed_at", "status", "associated_refs"],
  },
  audit: {
    title: "Audit log",
    subtitle: "관리자 mutation 감사 기록",
    endpoint: "/v1/admin/audit-logs",
    columns: ["created_at", "actor", "action", "target_type", "target_id", "outcome", "reason", "request_id"],
  },
  "vss-requests": {
    title: "VSS request failures",
    subtitle: "VSS → Module 요청 중 200/202가 아닌 응답 기록",
    endpoint: "/v1/admin/vss/request-failures",
    columns: ["created_at", "status_code", "method", "path", "project_id", "reason", "request_id"],
  },
};

const state = {
  session: null,
  view: "repositories",
  rows: [],
  loading: false,
  empty: false,
  error: null,
  cursor: null,
  previousCursors: [],
  nextCursor: null,
  loadSequence: 0,
  selectedRepositoryId: null,
  selectedCommitShas: [],
  repositoriesList: [],
  runtimeModelLoading: false,
  runtimeModelsSignature: null,
  runtimeModelsPayload: null,
  runtimeServiceLoading: false,
  runtimeServiceTriggerReady: false,
  chatConversations: [],
  chatConversation: null,
  chatTrace: null,
  selectedChatConversationId: null,
  selectedChatResponseId: null,
  chatMonitorLoading: false,
  chatMaintenanceLoading: false,
};
const byId = (id) => document.getElementById(id);
let runtimeModelTimer = null;
let chatMonitorTimer = null;

class AdminRequestError extends Error {
  constructor({ status, reason, detail, retryable, requestId }) {
    super(detail || "요청을 처리하지 못했습니다.");
    this.name = "AdminRequestError";
    this.status = status;
    this.reason = reason || "ADMIN_REQUEST_FAILED";
    this.retryable = Boolean(retryable);
    this.requestId = requestId || null;
  }
}

function withQuery(path, values) {
  const url = new URL(path, window.location.origin);
  Object.entries(values).forEach(([key, value]) => {
    if (value !== null && value !== undefined && value !== "") url.searchParams.set(key, value);
  });
  return `${url.pathname}${url.search}`;
}

async function apiRequest(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = state.session.csrf_token;
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  let payload = null;
  if (response.status !== 204) {
    try { payload = await response.json(); } catch { payload = null; }
  }
  if (response.status === 401 && payload?.reason === "AUTHENTICATION_REQUIRED") {
    showLogin();
    throw new AdminRequestError({
      status: 401,
      reason: payload?.reason || "AUTHENTICATION_REQUIRED",
      detail: payload?.detail || "세션이 만료되었습니다.",
      retryable: false,
      requestId: payload?.request_id || response.headers.get("X-Request-ID"),
    });
  }
  if (!response.ok) {
    throw new AdminRequestError({
      status: response.status,
      reason: payload?.reason,
      detail: payload?.detail,
      retryable: payload?.retryable,
      requestId: payload?.request_id || response.headers.get("X-Request-ID"),
    });
  }
  return payload;
}

function runtimeModelNames(value) {
  return Array.isArray(value)
    ? value.filter((name) => typeof name === "string" && name.trim()).map((name) => name.trim())
    : [];
}

function syncRuntimeModelControls() {
  const payload = state.runtimeModelsPayload || {};
  const select = byId("runtime-models");
  const upButton = byId("runtime-model-up");
  const downButton = byId("runtime-model-down");
  const reloadButton = byId("runtime-model-reload");
  const autoUp = byId("runtime-model-auto-up");
  if (!select || !upButton || !downButton || !reloadButton || !autoUp) return;

  const installed = runtimeModelNames(payload.installed_models);
  const running = runtimeModelNames(payload.models);
  const autoUpModels = runtimeModelNames(payload.auto_up_models);
  const selected = select.value;
  const unavailable = !payload.available;
  const selectedRunning = running.includes(selected);
  const operator = can("operator");
  const busy = state.runtimeModelLoading;

  select.disabled = busy || unavailable || installed.length === 0;
  upButton.disabled = busy || unavailable || !operator || !selected || selectedRunning;
  downButton.disabled = busy || unavailable || !operator || !selected || !selectedRunning;
  reloadButton.disabled = busy || unavailable || !operator || !selected;
  autoUp.disabled = busy || unavailable || !operator || !selected;
  autoUp.checked = Boolean(selected) && autoUpModels.includes(selected);
}

function renderRuntimeModels(payload) {
  const select = byId("runtime-models");
  if (!select) return;

  const running = runtimeModelNames(payload?.models);
  const installed = runtimeModelNames(payload?.installed_models);
  const stoppedFromPayload = runtimeModelNames(payload?.stopped_models);
  const stopped = stoppedFromPayload.length || !installed.length
    ? stoppedFromPayload
    : installed.filter((name) => !running.includes(name));
  const autoUpModels = runtimeModelNames(payload?.auto_up_models);
  const normalized = {
    available: Boolean(payload?.available),
    models: running,
    installed_models: installed,
    stopped_models: stopped,
    auto_up_models: autoUpModels,
  };
  const signature = JSON.stringify(normalized);
  const previousSelection = select.value;
  state.runtimeModelsPayload = normalized;

  if (signature !== state.runtimeModelsSignature) {
    select.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";

    if (!normalized.available) {
      placeholder.textContent = "Ollama: 응답 없음";
      select.append(placeholder);
    } else {
      placeholder.textContent = installed.length
        ? `Ollama: Running ${running.length} / Stopped ${stopped.length}`
        : "Ollama: 설치 모델 없음";
      select.append(placeholder);

      if (running.length) {
        const group = document.createElement("optgroup");
        group.label = "Running";
        running.forEach((name) => {
          const option = document.createElement("option");
          option.value = name;
          option.textContent = `● ${name}${autoUpModels.includes(name) ? " · AUTO" : ""}`;
          group.append(option);
        });
        select.append(group);
      }

      if (stopped.length) {
        const group = document.createElement("optgroup");
        group.label = "Stopped";
        stopped.forEach((name) => {
          const option = document.createElement("option");
          option.value = name;
          option.textContent = `○ ${name}${autoUpModels.includes(name) ? " · AUTO" : ""}`;
          group.append(option);
        });
        select.append(group);
      }
    }

    if (installed.includes(previousSelection)) select.value = previousSelection;
    state.runtimeModelsSignature = signature;
  }

  select.title = normalized.available
    ? `Running: ${running.join(", ") || "없음"} / Stopped: ${stopped.join(", ") || "없음"} / Auto Up: ${autoUpModels.join(", ") || "없음"}`
    : "Ollama runtime에 연결할 수 없습니다.";
  syncRuntimeModelControls();
}

async function refreshRuntimeModels() {
  if (!state.session) return;
  try {
    renderRuntimeModels(await apiRequest("/v1/admin/runtime/models"));
  } catch {
    if (state.session) {
      renderRuntimeModels({
        available: false,
        models: [],
        installed_models: [],
        stopped_models: [],
        auto_up_models: [],
      });
    }
  }
}

async function controlRuntimeModel(action) {
  const select = byId("runtime-models");
  const status = byId("runtime-model-status");
  const model = select?.value || "";
  if (!select || !status || !model || !can("operator") || state.runtimeModelLoading) return;

  const labels = { up: "Up", down: "Down", reload: "Reload" };
  state.runtimeModelLoading = true;
  syncRuntimeModelControls();
  status.textContent = `${model} ${labels[action]} 진행 중...`;
  try {
    const result = await apiRequest(`/v1/admin/runtime/models/${action}`, {
      method: "POST",
      body: JSON.stringify({ model }),
    });
    if (action === "up") {
      status.textContent = result.already_running ? `${model} 이미 Up` : `${model} Up 완료`;
    } else if (action === "down") {
      const autoNote = result.auto_up_disabled ? " · Auto Up 해제" : "";
      status.textContent = `${model} ${result.already_stopped ? "이미 Down" : "Down 완료"}${autoNote}`;
    } else {
      status.textContent = `${model} Reload 완료`;
    }
  } catch (error) {
    status.textContent = `${labels[action]} 실패: ${error.reason || error.message}`;
  } finally {
    state.runtimeModelLoading = false;
    await refreshRuntimeModels();
  }
}

async function setRuntimeModelAutoUp() {
  const select = byId("runtime-models");
  const autoUp = byId("runtime-model-auto-up");
  const status = byId("runtime-model-status");
  const model = select?.value || "";
  if (!select || !autoUp || !status || !model || !can("operator") || state.runtimeModelLoading) return;

  const enabled = autoUp.checked;
  state.runtimeModelLoading = true;
  syncRuntimeModelControls();
  status.textContent = `${model} Auto Up ${enabled ? "설정" : "해제"} 중...`;
  try {
    const result = await apiRequest("/v1/admin/runtime/models/auto-up", {
      method: "PUT",
      body: JSON.stringify({ model, enabled }),
    });
    status.textContent = `${model} Auto Up ${result.enabled ? "ON" : "OFF"}${result.loaded_now ? " · Up 완료" : ""}`;
  } catch (error) {
    status.textContent = `Auto Up 실패: ${error.reason || error.message}`;
  } finally {
    state.runtimeModelLoading = false;
    await refreshRuntimeModels();
  }
}

function syncRuntimeServiceControls() {
  const buttons = document.querySelectorAll("button[data-restart-scope]");
  const disabled = state.runtimeServiceLoading || !state.runtimeServiceTriggerReady || !can("admin");
  buttons.forEach((button) => { button.disabled = disabled; });
}

async function refreshRuntimeServices() {
  if (!state.session || !can("admin")) return false;
  const status = byId("runtime-service-status");
  try {
    const result = await apiRequest("/v1/admin/runtime/services");
    state.runtimeServiceTriggerReady = Boolean(result?.trigger_ready);
    if (status && !state.runtimeServiceLoading) {
      status.textContent = state.runtimeServiceTriggerReady ? "Restart channel ready" : "Restart channel not configured";
    }
    syncRuntimeServiceControls();
    return state.runtimeServiceTriggerReady;
  } catch {
    state.runtimeServiceTriggerReady = false;
    syncRuntimeServiceControls();
    return false;
  }
}

function delay(milliseconds) {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

async function waitForModuleServiceRecovery() {
  const status = byId("runtime-service-status");
  await delay(4_000);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const result = await apiRequest("/v1/admin/runtime/services");
      state.runtimeServiceTriggerReady = Boolean(result?.trigger_ready);
      state.runtimeServiceLoading = false;
      syncRuntimeServiceControls();
      if (status) status.textContent = "Module services reconnected";
      void refreshRuntimeModels();
      return true;
    } catch {
      if (status) status.textContent = "Reconnecting module services...";
      await delay(1_000);
    }
  }
  state.runtimeServiceLoading = false;
  state.runtimeServiceTriggerReady = false;
  syncRuntimeServiceControls();
  if (status) status.textContent = "Reconnect timeout · refresh manually";
  return false;
}

async function restartModuleServices(scope) {
  if (!can("admin") || state.runtimeServiceLoading || !state.runtimeServiceTriggerReady) return;
  const labels = {
    snapshot_backend: "Snapshot Backend",
    admin_web: "Admin Web",
    module_stack: "Module Stack",
  };
  const descriptions = {
    snapshot_backend: "vss-snapshot.service를 재시작합니다. Admin Web은 유지되지만 Backend API가 잠시 끊길 수 있습니다.",
    admin_web: "vss-admin-web.service를 재시작합니다. 현재 관리 화면 연결이 잠시 끊길 수 있습니다.",
    module_stack: "vss-snapshot.service와 vss-admin-web.service를 순서대로 재시작합니다. 관리 화면 연결이 잠시 끊길 수 있습니다.",
  };
  const confirmed = await confirmAdminAction(
    `${labels[scope]} 재시작`,
    `${descriptions[scope]}\n\n임의 shell 명령은 실행하지 않으며 systemd의 고정 restart controller만 호출합니다.`,
    { confirmLabel: "Restart" },
  );
  if (!confirmed) return;

  const status = byId("runtime-service-status");
  state.runtimeServiceLoading = true;
  syncRuntimeServiceControls();
  if (status) status.textContent = `${labels[scope]} restart 예약 중...`;
  try {
    const result = await apiRequest("/v1/admin/runtime/services/restart", {
      method: "POST",
      body: JSON.stringify({ scope }),
    });
    if (status) {
      status.textContent = result.already_scheduled
        ? `${labels[scope]} restart 이미 예약됨`
        : `${labels[scope]} restart 예약됨 · reconnect 대기`;
    }
    void waitForModuleServiceRecovery();
  } catch (error) {
    state.runtimeServiceLoading = false;
    syncRuntimeServiceControls();
    if (status) status.textContent = `Restart 실패: ${error.reason || error.message}`;
    showStatusError(error);
  }
}

async function fetchAllItems(path, itemKey = "items") {
  const items = [];
  const seen = new Set();
  let cursor = null;
  for (let page = 0; page < 100; page += 1) {
    const payload = await apiRequest(withQuery(path, { limit: "500", cursor }));
    items.push(...(payload?.[itemKey] || []));
    cursor = payload?.next_cursor || null;
    if (!cursor) return items;
    if (seen.has(cursor)) {
      throw new AdminRequestError({ reason: "ADMIN_CURSOR_LOOP", detail: "목록 cursor가 반복되었습니다.", retryable: false });
    }
    seen.add(cursor);
  }
  throw new AdminRequestError({ reason: "ADMIN_PAGE_LIMIT_EXCEEDED", detail: "목록 페이지가 안전 한도를 초과했습니다.", retryable: false });
}

function can(required) {
  return roleLevel[state.session?.role] >= roleLevel[required];
}

function applyRole() {
  document.querySelectorAll("[data-min-role]").forEach((element) => {
    element.hidden = !can(element.dataset.minRole);
  });
}

function showLogin() {
  state.session = null;
  state.runtimeModelLoading = false;
  state.runtimeModelsSignature = null;
  state.runtimeModelsPayload = null;
  state.runtimeServiceLoading = false;
  state.runtimeServiceTriggerReady = false;
  if (runtimeModelTimer !== null) {
    clearInterval(runtimeModelTimer);
    runtimeModelTimer = null;
  }
  if (chatMonitorTimer !== null) {
    clearInterval(chatMonitorTimer);
    chatMonitorTimer = null;
  }
  renderRuntimeModels({ available: false, models: [], installed_models: [], stopped_models: [] });
  byId("runtime-model-status").textContent = "";
  if (byId("runtime-service-status")) byId("runtime-service-status").textContent = "";
  syncRuntimeServiceControls();
  if (byId("action-modal").open) byId("action-modal").close();
  byId("app-shell").hidden = true;
  byId("login-view").hidden = false;
}

function showApp(session) {
  state.session = session;
  byId("login-view").hidden = true;
  byId("app-shell").hidden = false;
  byId("session-user").textContent = session.username;
  byId("session-role").textContent = session.role;
  applyRole();
  void refreshRuntimeModels();
  if (can("admin")) void refreshRuntimeServices();
  if (runtimeModelTimer !== null) clearInterval(runtimeModelTimer);
  runtimeModelTimer = setInterval(refreshRuntimeModels, runtimeModelRefreshMs);
  selectView("repositories");
}

function setTableState(kind, error = null) {
  state.loading = kind === "loading";
  state.empty = kind === "empty";
  state.error = kind === "error" ? error : null;
  byId("loading-state").hidden = kind !== "loading";
  byId("empty-state").hidden = kind !== "empty";
  byId("error-state").hidden = kind !== "error";
  if (kind === "error") {
    const normalized = error instanceof AdminRequestError
      ? error
      : new AdminRequestError({ detail: error?.message || String(error) });
    byId("error-detail").textContent = normalized.message;
    byId("error-reason").textContent = normalized.reason;
    byId("error-retryable").textContent = normalized.retryable ? "Yes" : "No";
    byId("error-request-id").textContent = normalized.requestId || "-";
    byId("binding-fix-button").hidden = !bindingReasons.has(normalized.reason);
  }
}

function valueText(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function githubRepositoryWebUrl(remoteUrl) {
  if (!remoteUrl) return null;
  try {
    const parsed = new URL(String(remoteUrl));
    if (!["github.com", "www.github.com"].includes(parsed.hostname.toLowerCase())) return null;
    const parts = parsed.pathname.split("/").filter(Boolean);
    if (parts.length !== 2) return null;
    const repository = parts[1].replace(/\.git$/i, "");
    if (!parts[0] || !repository) return null;
    return `https://github.com/${parts[0]}/${repository}`;
  } catch {
    return null;
  }
}

function selectedRepository() {
  return state.repositoriesList.find((repo) => repo.repository_id === state.selectedRepositoryId) || null;
}

function commitWebUrl(commitSha) {
  if (!/^[0-9a-f]{40}$/i.test(String(commitSha || ""))) return null;
  const repositoryUrl = githubRepositoryWebUrl(selectedRepository()?.remote_url);
  return repositoryUrl ? `${repositoryUrl}/commit/${commitSha}` : null;
}

function rowId(row) {
  return row.snapshot_id || row.binding_id || row.tracked_branch_id || row.repository_id || row.project_id || row.audit_id || row.commit_sha;
}

function actionButton(label, action, item, danger = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.dataset.action = action;
  button.dataset.itemId = rowId(item) || "";
  if (danger) button.classList.add("danger");
  return button;
}

function renderActions(row) {
  const cell = document.createElement("td");
  cell.className = "cell-actions";
  if (state.view === "repositories") {
    cell.append(actionButton("Commits", "view-commits", row));
    if (can("admin")) cell.append(actionButton("Edit", "edit-repository", row));
    if (can("operator")) cell.append(actionButton("Sync", "sync-repository", row));
    if (can("admin") && row.active) cell.append(actionButton("Deactivate", "deactivate-repository", row, true));
    if (can("admin")) cell.append(actionButton("Delete", "purge-repository", row, true));
  }
  if (state.view === "commits") {
    cell.append(actionButton("Details", "commit-details", row));
    if (can("operator") && row.status === "git_only") {
      cell.append(actionButton("Materialize", "materialize-commit", row));
    }
  }
  if (state.view === "tracked-branches") {
    cell.append(actionButton("History", "branch-history", row));
    if (can("operator") && row.tracked && row.current_head_sha) {
      cell.append(actionButton("Index", "index-tracked-branch", row));
    }
    if (can("admin")) cell.append(actionButton("Edit", "edit-tracked-branch", row));
    if (can("admin") && row.tracked) cell.append(actionButton("Untrack", "untrack-branch", row, true));
  }
  if (state.view === "branch-bindings" && can("admin")) {
    cell.append(actionButton("Edit", "edit-binding", row));
    if (row.active) cell.append(actionButton("Deactivate", "deactivate-binding", row, true));
  }
  if (state.view === "snapshots") {
    cell.append(actionButton("Details", "snapshot-details", row));
    if (can("operator") && row.state === "materialized") {
      cell.append(actionButton("Index", "index-snapshot", row));
    }
    if (can("operator") && retryableSnapshotStates.has(row.state)) {
      cell.append(actionButton("Retry", "retry-snapshot", row));
    }
  }
  if (state.view === "vss" && can("admin")) {
    cell.append(actionButton("Delete vector", "delete-vss-project", row, true));
  }
  return cell;
}

function renderTable() {
  const config = views[state.view];
  const headRow = document.createElement("tr");
  config.columns.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column === "select" ? "" : column.replaceAll("_", " ");
    headRow.append(th);
  });
  const hasActions = ["repositories", "tracked-branches", "branch-bindings", "snapshots", "commits", "vss"].includes(state.view);
  if (hasActions) {
    const th = document.createElement("th");
    th.textContent = "Actions";
    headRow.append(th);
  }
  byId("data-head").replaceChildren(headRow);

  const body = document.createDocumentFragment();
  state.rows.forEach((row) => {
    const tr = document.createElement("tr");
    config.columns.forEach((column) => {
      const td = document.createElement("td");
      const value = valueText(row[column]);
      if (column === "select") {
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.checked = state.selectedCommitShas.includes(row.commit_sha);
        cb.setAttribute("aria-label", `Select commit ${row.commit_sha}`);
        cb.addEventListener("change", () => {
          if (cb.checked) {
            if (state.selectedCommitShas.length >= 2) {
              cb.checked = false;
              alert("비교는 최대 2개의 커밋만 선택할 수 있습니다.");
              return;
            }
            state.selectedCommitShas.push(row.commit_sha);
          } else {
            state.selectedCommitShas = state.selectedCommitShas.filter((s) => s !== row.commit_sha);
          }
          updateCompareButton();
        });
        td.append(cb);
      } else if (column === "commit_sha") {
        const url = commitWebUrl(row.commit_sha);
        const code = document.createElement(url ? "a" : "span");
        code.className = url ? "sha-code sha-link" : "sha-code";
        code.title = row.commit_sha;
        code.textContent = row.commit_sha ? row.commit_sha.slice(0, 8) : "-";
        if (url) {
          code.href = url;
          code.target = "_blank";
          code.rel = "noopener noreferrer";
          code.setAttribute("aria-label", `Open commit ${row.commit_sha} on GitHub`);
        }
        td.append(code);
      } else if (column === "status") {
        const pill = document.createElement("span");
        pill.className = `state-pill state-${String(row.status || "").replaceAll("_", "-")}`;
        pill.textContent = String(row.status || "-").replaceAll("_", " ");
        td.append(pill);
      } else if (column === "associated_refs" && Array.isArray(row.associated_refs)) {
        if (row.associated_refs.length === 0) {
          td.textContent = "-";
        } else {
          row.associated_refs.forEach((ref) => {
            const badge = document.createElement("span");
            badge.className = `ref-badge ref-${ref.ref_type.replaceAll("_", "-")}`;
            const icon = ref.ref_type === "branch" ? "🌿" : ref.ref_type === "tag" ? "🏷️" : "🔀";
            badge.textContent = `${icon} ${ref.name}`;
            if (ref.detail) badge.title = ref.detail;
            td.append(badge);
          });
        }
      } else if (["state", "outcome", "vss_state"].includes(column) && value !== "-") {
        const pill = document.createElement("span");
        pill.className = `state-pill state-${value.toLowerCase().replaceAll(" ", "-")}`;
        pill.textContent = value;
        td.append(pill);
      } else {
        td.textContent = value;
      }
      tr.append(td);
    });
    if (hasActions) tr.append(renderActions(row));
    body.append(tr);
  });
  byId("data-body").replaceChildren(body);
}

function resetPagination() {
  state.cursor = null;
  state.previousCursors = [];
  state.nextCursor = null;
}

function updatePagination() {
  byId("previous-page").disabled = state.loading || state.previousCursors.length === 0;
  byId("next-page").disabled = state.loading || !state.nextCursor;
  byId("page-number").textContent = `Page ${state.previousCursors.length + 1}`;
}

function updateCompareButton() {
  const btn = byId("compare-commits-button");
  if (!btn) return;
  const count = state.selectedCommitShas.length;
  btn.textContent = `Compare (${count}/2)`;
  btn.disabled = count !== 2 || !can("operator");
}

async function ensureRepositoriesLoaded() {
  if (state.repositoriesList.length === 0) {
    try {
      const payload = await apiRequest("/v1/admin/repositories?limit=100");
      state.repositoriesList = payload?.items || [];
    } catch {
      state.repositoriesList = [];
    }
  }
}

function chatDate(value) {
  if (!value) return "-";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function chatDuration(value) {
  if (value === null || value === undefined) return "-";
  const number = Number(value);
  if (!Number.isFinite(number)) return "-";
  if (number >= 1000) return `${(number / 1000).toFixed(2)} s`;
  return `${number.toFixed(1)} ms`;
}

function chatIdentity(conversation) {
  return conversation?.requester_id
    || conversation?.client_instance_id
    || conversation?.requester_type
    || "unknown";
}

function shortChatId(value) {
  if (!value) return "-";
  const text = String(value);
  return text.length > 12 ? `${text.slice(0, 8)}…` : text;
}

function renderChatSessionList() {
  const list = byId("chat-session-list");
  const filter = (byId("chat-session-filter")?.value || "").trim().toLowerCase();
  const visible = filter
    ? state.chatConversations.filter((conversation) => [
      conversation.title,
      conversation.requester_id,
      conversation.client_instance_id,
      conversation.project_id,
      conversation.last_chat_model,
      conversation.last_embedding_model,
    ].some((value) => String(value || "").toLowerCase().includes(filter)))
    : state.chatConversations;
  list.replaceChildren();
  byId("chat-session-count").textContent = filter
    ? `${visible.length}/${state.chatConversations.length}`
    : `${state.chatConversations.length}`;
  byId("chat-session-empty").hidden = visible.length !== 0;
  visible.forEach((conversation) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chat-session-button";
    if (conversation.conversation_id === state.selectedChatConversationId) button.classList.add("active");
    button.dataset.conversationId = conversation.conversation_id;

    const title = document.createElement("span");
    title.className = "chat-session-title";
    title.textContent = conversation.title || `Chat ${shortChatId(conversation.conversation_id)}`;

    const meta = document.createElement("span");
    meta.className = "chat-session-meta";
    [
      chatIdentity(conversation),
      conversation.project_id || "no project",
      conversation.last_chat_model || "model unknown",
      `${conversation.message_count || 0} messages`,
      chatDate(conversation.last_message_at || conversation.updated_at),
    ].forEach((value) => {
      const span = document.createElement("span");
      span.textContent = value;
      meta.append(span);
    });
    if (conversation.last_response_status) {
      const status = document.createElement("span");
      status.className = "chat-session-status";
      status.textContent = conversation.last_response_status;
      meta.append(status);
    }
    button.append(title, meta);
    button.addEventListener("click", () => void loadChatConversation(conversation.conversation_id));
    list.append(button);
  });
}

function renderChatConversationHeader(detail) {
  const header = byId("chat-conversation-header");
  header.replaceChildren();
  const conversation = detail?.conversation;
  if (!conversation) {
    const title = document.createElement("strong");
    title.textContent = "세션을 선택하세요";
    header.append(title);
    return;
  }
  const titleRow = document.createElement("div");
  titleRow.className = "chat-conversation-title-row";
  const title = document.createElement("strong");
  title.textContent = conversation.title || `Chat ${shortChatId(conversation.conversation_id)}`;
  const actions = document.createElement("div");
  actions.className = "chat-conversation-actions";
  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "quiet chat-compact-action";
  deleteButton.textContent = "Delete";
  const active = (detail.responses || []).some((response) => (
    response.status === "created" || response.status === "running"
  ));
  deleteButton.disabled = active || state.chatMaintenanceLoading;
  deleteButton.title = active
    ? "실행 중인 응답이 있는 Conversation은 삭제할 수 없습니다."
    : "Conversation과 Module trace 기록을 영구 삭제합니다.";
  deleteButton.addEventListener("click", () => void deleteSelectedChatConversation());
  actions.append(deleteButton);
  titleRow.append(title, actions);

  const meta = document.createElement("span");
  meta.className = "chat-muted";
  meta.textContent = [
    `Requester ${chatIdentity(conversation)}`,
    `Origin ${conversation.origin || "unknown"}`,
    `Project ${conversation.project_id || "-"}`,
    `Capture ${conversation.content_capture_mode}`,
  ].join(" · ");
  header.append(titleRow, meta);
}

function responseForAssistantMessage(detail, messageId) {
  return (detail?.responses || []).find((response) => response.assistant_message_id === messageId) || null;
}

function renderChatTranscript(detail) {
  const transcript = byId("chat-transcript");
  transcript.replaceChildren();
  const messages = detail?.messages || [];
  if (!messages.length) {
    const empty = document.createElement("div");
    empty.className = "chat-empty";
    empty.textContent = "이 세션에는 저장된 메시지가 없습니다.";
    transcript.append(empty);
    return;
  }

  messages.forEach((message) => {
    const row = document.createElement("div");
    row.className = `chat-message-row ${message.role}`;
    const bubble = document.createElement("div");
    bubble.className = "chat-bubble";
    const label = document.createElement("span");
    label.className = "chat-message-label";
    label.textContent = message.role;

    const content = document.createElement("div");
    if (message.content === null || message.content === undefined) {
      content.className = "chat-content-omitted";
      content.textContent = `Content not captured · ${message.content_length || 0} bytes · ${shortChatId(message.content_sha256)}`;
    } else {
      content.textContent = message.content;
    }

    const response = message.role === "assistant"
      ? responseForAssistantMessage(detail, message.message_id)
      : null;
    if (response) {
      const select = document.createElement("button");
      select.type = "button";
      select.className = "chat-response-select";
      if (response.response_id === state.selectedChatResponseId) select.classList.add("active");
      select.setAttribute("aria-label", `Trace ${shortChatId(response.trace_id)} 열기`);
      const meta = document.createElement("div");
      meta.className = "chat-message-meta";
      [
        response.chat_model || "model unknown",
        response.status,
        `TTFT ${chatDuration(response.ttft_ms)}`,
        `Total ${chatDuration(response.total_ms)}`,
        `${response.source_count || 0} sources`,
      ].forEach((value) => {
        const span = document.createElement("span");
        span.textContent = value;
        meta.append(span);
      });
      select.append(label, content, meta);
      select.addEventListener("click", () => void loadChatTrace(response.response_id));
      bubble.append(select);
    } else {
      const meta = document.createElement("div");
      meta.className = "chat-message-meta";
      const created = document.createElement("span");
      created.textContent = chatDate(message.created_at);
      meta.append(created);
      bubble.append(label, content, meta);
    }
    row.append(bubble);
    transcript.append(row);
  });
  transcript.scrollTop = transcript.scrollHeight;
}

function traceSection(titleText, rows) {
  const section = document.createElement("section");
  section.className = "chat-trace-section";
  const title = document.createElement("h3");
  title.textContent = titleText;
  const list = document.createElement("dl");
  list.className = "chat-trace-grid";
  rows.forEach(([labelText, value]) => {
    const dt = document.createElement("dt");
    dt.textContent = labelText;
    const dd = document.createElement("dd");
    dd.textContent = value === null || value === undefined || value === "" ? "-" : String(value);
    list.append(dt, dd);
  });
  section.append(title, list);
  return section;
}

function traceEventDetail(event) {
  const payload = event?.payload || {};
  if (payload.label) return payload.label;
  if (payload.code) return `${payload.code}${payload.message ? ` · ${payload.message}` : ""}`;
  if (payload.stage) return payload.stage;
  if (payload.model) return payload.model;
  const text = JSON.stringify(payload);
  return text.length > 260 ? `${text.slice(0, 257)}…` : text;
}

function renderChatTrace(trace) {
  const inspector = byId("chat-trace-inspector");
  inspector.replaceChildren();
  if (!trace?.response) {
    const empty = document.createElement("div");
    empty.className = "chat-empty";
    empty.textContent = "Assistant 응답을 선택하면 실행 trace가 표시됩니다.";
    inspector.append(empty);
    return;
  }
  const response = trace.response;
  inspector.append(
    traceSection("Response", [
      ["Status", response.status],
      ["Outcome", response.outcome],
      ["Trace", response.trace_id],
      ["VSS request", response.vss_request_id],
      ["Project", response.project_id],
      ["Index", response.index_id],
      ["Resolved", response.resolved_by],
    ]),
    traceSection("Models", [
      ["sLLM", response.chat_model],
      ["Embedding", response.embedding_model],
      ["Evidence", response.has_evidence],
      ["Top score", response.top_score],
      ["Threshold", response.threshold],
      ["Sources", response.source_count],
      ["References", response.reference_count],
    ]),
    traceSection("Timing", [
      ["Embedding", chatDuration(response.embed_ms)],
      ["Vector search", chatDuration(response.search_ms)],
      ["BM25", chatDuration(response.bm25_ms)],
      ["Prompt", chatDuration(response.prompt_ms)],
      ["Pre LLM", chatDuration(response.pre_llm_ms)],
      ["TTFT", chatDuration(response.ttft_ms)],
      ["Generation", chatDuration(response.gen_ms)],
      ["Total", chatDuration(response.total_ms)],
      ["Decode", response.decode_tok_s === null ? "-" : `${response.decode_tok_s} tok/s`],
      ["Tokens", response.eval_count],
    ]),
  );

  if ((trace.model_observations || []).length) {
    const section = document.createElement("section");
    section.className = "chat-trace-section";
    const title = document.createElement("h3");
    title.textContent = "Runtime observations";
    section.append(title);
    trace.model_observations.forEach((observation) => {
      const chip = document.createElement("span");
      const residentClass = observation.resident === true ? "resident" : "stopped";
      chip.className = `chat-model-chip ${residentClass}`;
      chip.textContent = `${observation.role} · ${observation.model_name} · ${observation.stage}`;
      section.append(chip);
    });
    inspector.append(section);
  }

  const sourceEvent = (trace.events || []).find((event) => (
    (event.event_type === "meta" || event.event_type === "done")
    && Array.isArray(event.payload?.sources)
    && event.payload.sources.length
  ));
  if (sourceEvent) {
    const section = document.createElement("section");
    section.className = "chat-trace-section";
    const title = document.createElement("h3");
    title.textContent = "Sources";
    section.append(title);
    sourceEvent.payload.sources.forEach((source, index) => {
      const line = document.createElement("div");
      line.className = "chat-event-detail";
      const path = source.path || source.file || source.source || "unknown";
      const start = source.start_line ?? source.line_start;
      const end = source.end_line ?? source.line_end;
      const range = start === undefined ? "" : ` L${start}${end === undefined ? "" : `-${end}`}`;
      const score = source.score === undefined ? "" : ` · score ${source.score}`;
      line.textContent = `#${index + 1} ${path}${range}${score}`;
      section.append(line);
    });
    inspector.append(section);
  }

  const timelineSection = document.createElement("section");
  timelineSection.className = "chat-trace-section";
  const timelineTitle = document.createElement("h3");
  timelineTitle.textContent = "Timeline";
  const timeline = document.createElement("div");
  timeline.className = "chat-timeline";
  (trace.events || []).forEach((event) => {
    const item = document.createElement("div");
    item.className = "chat-event";
    const elapsed = document.createElement("span");
    elapsed.className = "chat-event-time";
    elapsed.textContent = event.elapsed_ms === null || event.elapsed_ms === undefined
      ? "-"
      : `${Number(event.elapsed_ms).toFixed(1)}ms`;
    const type = document.createElement("span");
    type.className = "chat-event-type";
    type.textContent = event.event_type;
    const detail = document.createElement("span");
    detail.className = "chat-event-detail";
    detail.textContent = traceEventDetail(event);
    item.append(elapsed, type, detail);
    timeline.append(item);
  });
  timelineSection.append(timelineTitle, timeline);
  inspector.append(timelineSection);
}

async function loadChatTrace(responseId) {
  if (!responseId) return;
  state.selectedChatResponseId = responseId;
  renderChatTranscript(state.chatConversation);
  byId("chat-trace-inspector").replaceChildren();
  const loading = document.createElement("div");
  loading.className = "chat-empty";
  loading.textContent = "Trace를 불러오는 중...";
  byId("chat-trace-inspector").append(loading);
  try {
    const trace = await apiRequest(`/v1/admin/chat/responses/${encodeURIComponent(responseId)}/trace`);
    if (state.selectedChatResponseId !== responseId || state.view !== "chat") return;
    state.chatTrace = trace;
    renderChatTrace(trace);
  } catch (error) {
    if (state.selectedChatResponseId !== responseId || state.view !== "chat") return;
    const failed = document.createElement("div");
    failed.className = "chat-empty";
    failed.textContent = `Trace 조회 실패: ${error.reason || error.message}`;
    byId("chat-trace-inspector").replaceChildren(failed);
  }
}

async function loadChatConversation(conversationId) {
  state.selectedChatConversationId = conversationId;
  state.selectedChatResponseId = null;
  state.chatTrace = null;
  renderChatSessionList();
  try {
    const detail = await apiRequest(`/v1/admin/chat/conversations/${encodeURIComponent(conversationId)}`);
    if (state.selectedChatConversationId !== conversationId || state.view !== "chat") return;
    state.chatConversation = detail;
    renderChatConversationHeader(detail);
    const responses = detail.responses || [];
    const latest = responses.length ? responses[responses.length - 1] : null;
    state.selectedChatResponseId = latest?.response_id || null;
    renderChatTranscript(detail);
    if (latest) {
      void loadChatTrace(latest.response_id);
    } else {
      renderChatTrace(null);
    }
  } catch (error) {
    if (state.selectedChatConversationId !== conversationId || state.view !== "chat") return;
    state.chatConversation = null;
    const transcript = byId("chat-transcript");
    transcript.replaceChildren();
    const failed = document.createElement("div");
    failed.className = "chat-empty";
    failed.textContent = `Conversation 조회 실패: ${error.reason || error.message}`;
    transcript.append(failed);
  }
}

async function loadChatView(sequence) {
  byId("status-band").classList.remove("error");
  byId("status-band").textContent = "Chat sessions 불러오는 중...";
  try {
    const payload = await apiRequest(withQuery(views.chat.endpoint, { limit: "100" }));
    if (sequence !== state.loadSequence || state.view !== "chat") return;
    state.chatConversations = payload?.items || [];
    byId("status-band").textContent = `${state.chatConversations.length} conversations`;
    const ids = new Set(state.chatConversations.map((item) => item.conversation_id));
    if (!state.selectedChatConversationId || !ids.has(state.selectedChatConversationId)) {
      state.selectedChatConversationId = state.chatConversations[0]?.conversation_id || null;
    }
    renderChatSessionList();
    if (state.selectedChatConversationId) {
      await loadChatConversation(state.selectedChatConversationId);
    } else {
      state.chatConversation = null;
      state.chatTrace = null;
      renderChatConversationHeader(null);
      byId("chat-transcript").replaceChildren();
      renderChatTrace(null);
    }
  } catch (error) {
    if (sequence !== state.loadSequence || state.view !== "chat") return;
    state.chatConversations = [];
    renderChatSessionList();
    byId("status-band").classList.add("error");
    byId("status-band").textContent = `Chat sessions 조회 실패: ${error.reason || error.message}`;
  }
}

function chatRetentionSummary(preview) {
  const policy = preview?.policy || {};
  const counts = preview?.eligible_by_mode || {};
  return [
    `삭제 대상 ${preview?.eligible_total || 0} conversations`,
    `metadata ${counts.metadata || 0} / ${policy.metadata_days || "-"}d`,
    `question_answer ${counts.question_answer || 0} / ${policy.question_answer_days || "-"}d`,
    `full_debug ${counts.full_debug || 0} / ${policy.full_debug_days || "-"}d`,
    `batch ${policy.batch_size || "-"}`,
  ].join(" · ");
}

async function deleteSelectedChatConversation() {
  if (state.chatMaintenanceLoading) return;
  const conversation = state.chatConversation?.conversation;
  if (!conversation) return;
  const active = (state.chatConversation?.responses || []).some((response) => (
    response.status === "created" || response.status === "running"
  ));
  if (active) {
    byId("status-band").classList.add("error");
    byId("status-band").textContent = "실행 중인 응답이 있는 Conversation은 삭제할 수 없습니다.";
    return;
  }
  const confirmed = await confirmAdminAction(
    "Delete Chat conversation",
    `${conversation.title || conversation.conversation_id}\n\n메시지, response trace, model observations를 Module DB에서 영구 삭제합니다. VSS 데이터는 변경하지 않습니다.`,
    { confirmLabel: "Delete conversation" },
  );
  if (!confirmed) return;

  state.chatMaintenanceLoading = true;
  renderChatConversationHeader(state.chatConversation);
  try {
    const id = encodeURIComponent(conversation.conversation_id);
    const confirm = encodeURIComponent(conversation.conversation_id);
    const result = await apiRequest(`/v1/admin/chat/conversations/${id}?confirm=${confirm}`, {
      method: "DELETE",
    });
    state.selectedChatConversationId = null;
    state.selectedChatResponseId = null;
    state.chatConversation = null;
    state.chatTrace = null;
    await loadView();
    showStatusResult(result);
  } catch (error) {
    showStatusError(error);
  } finally {
    state.chatMaintenanceLoading = false;
    if (state.view === "chat") renderChatConversationHeader(state.chatConversation);
  }
}

async function purgeExpiredChatConversations() {
  if (state.chatMaintenanceLoading) return;
  const button = byId("chat-retention-button");
  state.chatMaintenanceLoading = true;
  button.disabled = true;
  try {
    const preview = await apiRequest("/v1/admin/chat/retention");
    if (!preview?.eligible_total) {
      byId("status-band").classList.remove("error");
      byId("status-band").textContent = `Retention: 삭제 대상 없음 · ${chatRetentionSummary(preview)}`;
      return;
    }
    const confirmed = await confirmAdminAction(
      "Purge expired Chat traces",
      `${chatRetentionSummary(preview)}\n\ncreated/running 응답은 제외하며, 이 작업은 Module Chat 기록만 삭제합니다.`,
      { confirmLabel: "Purge expired" },
    );
    if (!confirmed) return;

    const result = await apiRequest(
      "/v1/admin/chat/retention/purge?confirm=purge-expired",
      { method: "POST" },
    );
    const deleted = new Set(result.conversation_ids || []);
    if (state.selectedChatConversationId && deleted.has(state.selectedChatConversationId)) {
      state.selectedChatConversationId = null;
      state.selectedChatResponseId = null;
      state.chatConversation = null;
      state.chatTrace = null;
    }
    await loadView();
    showStatusResult(result);
  } catch (error) {
    showStatusError(error);
  } finally {
    state.chatMaintenanceLoading = false;
    button.disabled = false;
  }
}

async function refreshChatMonitor() {
  if (state.view !== "chat" || !can("admin") || state.chatMonitorLoading) return;
  state.chatMonitorLoading = true;
  try {
    const payload = await apiRequest(withQuery(views.chat.endpoint, { limit: "100" }));
    if (state.view !== "chat") return;
    state.chatConversations = payload?.items || [];
    renderChatSessionList();
    const traceStatus = state.chatTrace?.response?.status;
    if (
      state.selectedChatConversationId
      && (traceStatus === "created" || traceStatus === "running")
    ) {
      await loadChatConversation(state.selectedChatConversationId);
    }
  } catch (error) {
    if (state.view !== "chat") return;
    byId("status-band").classList.add("error");
    byId("status-band").textContent = `Chat monitor 갱신 실패: ${error.reason || error.message}`;
  } finally {
    state.chatMonitorLoading = false;
  }
}

async function loadView() {
  const sequence = ++state.loadSequence;
  const requestedView = state.view;
  if (requestedView === "chat") {
    await loadChatView(sequence);
    return;
  }
  setTableState("loading");
  updatePagination();
  byId("status-band").classList.remove("error");
  byId("status-band").textContent = "";
  try {
    let endpoint = views[requestedView].endpoint;
    if (requestedView === "commits") {
      await ensureRepositoriesLoaded();
      if (!state.selectedRepositoryId && state.repositoriesList.length > 0) {
        state.selectedRepositoryId = state.repositoriesList[0].repository_id;
      }
      const select = byId("repository-filter-select");
      if (select && select.children.length !== state.repositoriesList.length) {
        select.replaceChildren();
        state.repositoriesList.forEach((repo) => {
          const opt = document.createElement("option");
          opt.value = repo.repository_id;
          opt.textContent = `${repo.canonical_name} (${repo.display_name})`;
          if (repo.repository_id === state.selectedRepositoryId) opt.selected = true;
          select.append(opt);
        });
      }
      if (state.selectedRepositoryId) {
        endpoint = `/v1/admin/repositories/${encodeURIComponent(state.selectedRepositoryId)}/commits`;
      } else {
        state.rows = [];
        state.nextCursor = null;
        renderTable();
        setTableState("empty");
        byId("status-band").textContent = "선택 가능한 Repository가 없습니다.";
        return;
      }
    }
    const payload = await apiRequest(withQuery(endpoint, {
      cursor: state.cursor,
      limit: String(listPageSize),
    }));
    if (sequence !== state.loadSequence || requestedView !== state.view) return;
    state.rows = Array.isArray(payload) ? payload : (payload?.items || payload?.branches || []);
    state.nextCursor = payload?.next_cursor || null;
    renderTable();
    setTableState(state.rows.length ? "ready" : "empty");
    byId("status-band").textContent = `${state.rows.length} items`;
  } catch (error) {
    if (sequence !== state.loadSequence || requestedView !== state.view) return;
    state.rows = [];
    state.nextCursor = null;
    renderTable();
    setTableState("error", error);
  } finally {
    if (sequence === state.loadSequence) {
      updatePagination();
      if (requestedView === "commits") updateCompareButton();
    }
  }
}

function applyViewPresentation(name) {
  document.querySelectorAll(".nav-item").forEach((button) => {
    const active = button.dataset.view === name;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  byId("view-title").textContent = views[name].title;
  byId("view-subtitle").textContent = views[name].subtitle;
  byId("create-repository").hidden = name !== "repositories" || !can("admin");
  byId("create-tracked-branch").hidden = name !== "tracked-branches" || !can("admin");
  byId("create-branch-binding").hidden = name !== "branch-bindings" || !can("admin");
  byId("table-view").hidden = name === "chat";
  byId("chat-view").hidden = name !== "chat";

  const repoSelect = byId("repository-filter-select");
  if (repoSelect) repoSelect.hidden = name !== "commits";
  const compareBtn = byId("compare-commits-button");
  if (compareBtn) {
    compareBtn.hidden = name !== "commits";
    updateCompareButton();
  }
}

function selectView(name) {
  if (!views[name] || (["audit", "vss-requests", "chat"].includes(name) && !can("admin"))) return;
  const previousView = state.view;
  const navItems = [...document.querySelectorAll(".nav-item")];
  const previousIndex = navItems.findIndex((button) => button.dataset.view === previousView);
  const nextIndex = navItems.findIndex((button) => button.dataset.view === name);
  const transitionDirection = previousIndex >= 0 && nextIndex >= 0 && nextIndex < previousIndex ? -1 : 1;
  document.documentElement.style.setProperty("--view-direction", String(transitionDirection));

  state.view = name;
  state.selectedCommitShas = [];
  resetPagination();

  const apply = () => applyViewPresentation(name);
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  if (previousView !== name && document.startViewTransition && !reducedMotion) {
    try {
      const transition = document.startViewTransition(apply);
      transition.finished.catch(() => {});
    } catch {
      apply();
    }
  } else {
    apply();
    if (previousView !== name && !reducedMotion) {
      const content = document.querySelector(".content");
      if (content) {
        content.classList.remove("view-arriving");
        void content.offsetWidth;
        content.classList.add("view-arriving");
      }
    }
  }

  if (chatMonitorTimer !== null) {
    clearInterval(chatMonitorTimer);
    chatMonitorTimer = null;
  }
  if (name === "chat") {
    chatMonitorTimer = setInterval(refreshChatMonitor, chatMonitorRefreshMs);
  }

  void loadView();
}

function textField(name, label, { type = "text", value = "", required = true, nullable = false } = {}) {
  const wrapper = document.createElement("label");
  wrapper.textContent = label;
  const input = document.createElement("input");
  input.name = name;
  input.type = type;
  input.value = value ?? "";
  input.required = required;
  if (nullable) input.dataset.nullable = "true";
  wrapper.append(input);
  return wrapper;
}

function checkboxField(name, label, checked) {
  const wrapper = document.createElement("label");
  wrapper.className = "boolean-field";
  wrapper.textContent = label;
  const input = document.createElement("input");
  input.name = name;
  input.type = "checkbox";
  input.checked = checked;
  input.dataset.boolean = "true";
  wrapper.append(input);
  return wrapper;
}

function selectField(name, label, options, selected = "") {
  const wrapper = document.createElement("label");
  wrapper.textContent = label;
  const select = document.createElement("select");
  select.name = name;
  select.required = true;
  options.forEach(({ value, label: optionLabel }) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = optionLabel;
    option.selected = value === selected;
    select.append(option);
  });
  wrapper.append(select);
  return { wrapper, select };
}

function prepareModal(title, { readOnly = false } = {}) {
  byId("modal-title").textContent = title;
  byId("modal-fields").replaceChildren();
  byId("modal-error").hidden = true;
  byId("modal-submit").hidden = readOnly;
  byId("modal-submit").disabled = !readOnly;
  byId("modal-cancel").textContent = readOnly ? "닫기" : "취소";
  byId("modal-form").dataset.endpoint = "";
  byId("modal-form").dataset.method = "";
  if (!byId("action-modal").open) byId("action-modal").showModal();
}

function confirmAdminAction(title, message, { confirmLabel = "확인" } = {}) {
  const modal = byId("confirm-modal");
  const confirmButton = byId("confirm-submit");
  const cancelButton = byId("confirm-cancel");
  const closeButton = byId("confirm-close");
  byId("confirm-title").textContent = title;
  byId("confirm-message").textContent = message;
  confirmButton.textContent = confirmLabel;

  if (modal.open) modal.close();
  return new Promise((resolve) => {
    const cleanup = () => {
      confirmButton.removeEventListener("click", onConfirm);
      cancelButton.removeEventListener("click", onCancel);
      closeButton.removeEventListener("click", onCancel);
      modal.removeEventListener("cancel", onDialogCancel);
      modal.removeEventListener("close", onDialogClose);
    };
    const settle = (confirmed) => {
      cleanup();
      if (modal.open) modal.close();
      resolve(confirmed);
    };
    const onConfirm = () => settle(true);
    const onCancel = () => settle(false);
    const onDialogCancel = (event) => {
      event.preventDefault();
      settle(false);
    };
    const onDialogClose = () => {
      cleanup();
      resolve(false);
    };
    confirmButton.addEventListener("click", onConfirm);
    cancelButton.addEventListener("click", onCancel);
    closeButton.addEventListener("click", onCancel);
    modal.addEventListener("cancel", onDialogCancel);
    modal.addEventListener("close", onDialogClose);
    modal.showModal();
  });
}

function showModalError(error) {
  const normalized = error instanceof AdminRequestError
    ? error
    : new AdminRequestError({ detail: error?.message || String(error) });
  const request = normalized.requestId ? ` · Request ${normalized.requestId}` : "";
  byId("modal-error").textContent = `[${normalized.reason}] ${normalized.message}${request}`;
  byId("modal-error").hidden = false;
}

function setMutationModal(endpoint, method) {
  byId("modal-form").dataset.endpoint = endpoint;
  byId("modal-form").dataset.method = method;
  byId("modal-submit").hidden = false;
  byId("modal-submit").disabled = false;
}

async function repositoryChoices(selected = "", activeOnly = false) {
  const repositories = await fetchAllItems(withQuery("/v1/admin/repositories", { active: activeOnly ? "true" : null }));
  const options = repositories.map((repository) => ({
    value: repository.repository_id,
    label: `${repository.display_name} (${repository.canonical_name})`,
  }));
  if (selected) {
    if (!options.some((option) => option.value === selected)) {
      options.unshift({ value: selected, label: selected });
    }
  } else {
    options.unshift({ value: "", label: options.length ? "Repository 선택" : "Repository 없음" });
  }
  return selectField("repository_id", "Repository", options, selected);
}

async function loadBranchCatalog(repositorySelect, branchSelect, selectedRef = "") {
  const repositoryId = repositorySelect.value;
  byId("modal-error").hidden = true;
  branchSelect.disabled = true;
  branchSelect.replaceChildren();
  const loading = document.createElement("option");
  loading.value = "";
  loading.textContent = repositoryId ? "Branch 불러오는 중" : "Repository를 먼저 선택";
  branchSelect.append(loading);
  if (!repositoryId) return false;
  try {
    const catalog = await apiRequest(`/v1/admin/repositories/${encodeURIComponent(repositoryId)}/branches`);
    const branches = [...(catalog?.branches || [])];
    if (selectedRef && !branches.some((branch) => branch.branch_ref === selectedRef)) {
      branches.unshift({ branch_ref: selectedRef, commit_sha: null });
    }
    branchSelect.replaceChildren();
    branches.forEach((branch) => {
      const option = document.createElement("option");
      option.value = branch.branch_ref;
      option.textContent = branch.commit_sha ? `${branch.branch_ref} · ${branch.commit_sha.slice(0, 12)}` : branch.branch_ref;
      option.selected = branch.branch_ref === (selectedRef || catalog.default_branch_ref);
      branchSelect.append(option);
    });
    branchSelect.disabled = branches.length === 0;
    if (!branches.length) showModalError(new AdminRequestError({ reason: "BRANCH_CATALOG_EMPTY", detail: "선택할 원격 Branch가 없습니다.", retryable: false }));
    return branches.length > 0;
  } catch (error) {
    branchSelect.replaceChildren();
    if (selectedRef) {
      const fallback = document.createElement("option");
      fallback.value = selectedRef;
      fallback.textContent = selectedRef;
      branchSelect.append(fallback);
      branchSelect.disabled = false;
    }
    showModalError(error);
    return Boolean(selectedRef);
  }
}

async function appendRepositoryBranchSelectors(fields, { repositoryId = "", branchRef = "", activeOnly = false } = {}) {
  const repository = await repositoryChoices(repositoryId, activeOnly);
  const branch = selectField("branch_ref", "Remote Branch", [{ value: "", label: "Branch 선택" }], branchRef);
  fields.append(repository.wrapper, branch.wrapper);
  const refresh = async () => {
    const ready = await loadBranchCatalog(repository.select, branch.select, "");
    byId("modal-submit").disabled = !ready || !byId("modal-form").dataset.endpoint;
  };
  repository.select.addEventListener("change", refresh);
  return loadBranchCatalog(repository.select, branch.select, branchRef);
}

function isGithubRepositoryInput(remoteUrl) {
  try {
    return ["github.com", "www.github.com"].includes(new URL(String(remoteUrl)).hostname.toLowerCase());
  } catch {
    return false;
  }
}

function wireRepositoryRegistrationDiscovery(fields) {
  const remoteInput = fields.querySelector('[name="remote_url"]');
  const canonicalInput = fields.querySelector('[name="canonical_name"]');
  const displayInput = fields.querySelector('[name="display_name"]');
  const providerInput = fields.querySelector('[name="provider"]');
  const defaultBranchInput = fields.querySelector('[name="default_branch_ref"]');
  const metadataDetails = fields.querySelector(".repository-metadata-details");
  const status = document.createElement("div");
  status.className = "repository-discovery-status";
  status.setAttribute("role", "status");
  status.setAttribute("aria-live", "polite");
  status.textContent = "GitHub URL을 입력하면 Repository 정보와 default branch를 자동으로 확인합니다.";
  remoteInput.closest("label")?.after(status);

  let timer = null;
  let sequence = 0;
  const setBusy = (busy) => {
    if (busy) remoteInput.setAttribute("aria-busy", "true");
    else remoteInput.removeAttribute("aria-busy");
  };
  const lockDerivedFields = (locked) => {
    [canonicalInput, providerInput, defaultBranchInput].forEach((input) => {
      input.readOnly = locked;
      input.dataset.discovered = locked ? "true" : "false";
    });
  };
  const clearAutoDiscoveredFields = () => {
    [canonicalInput, providerInput, defaultBranchInput].forEach((input) => {
      if (input.dataset.discovered === "true") input.value = "";
    });
    if (displayInput.dataset.autoDiscovered === "true") displayInput.value = "";
  };
  const setStatus = (message, kind = "idle") => {
    status.textContent = message;
    status.dataset.state = kind;
  };
  const discover = async () => {
    const current = ++sequence;
    const remoteUrl = remoteInput.value.trim();
    byId("modal-error").hidden = true;
    if (!remoteUrl) {
      setBusy(false);
      clearAutoDiscoveredFields();
      lockDerivedFields(false);
      if (metadataDetails) metadataDetails.open = false;
      byId("modal-submit").disabled = true;
      setStatus("GitHub URL을 입력하면 Repository 정보와 default branch를 자동으로 확인합니다.");
      return;
    }
    if (!isGithubRepositoryInput(remoteUrl)) {
      setBusy(false);
      clearAutoDiscoveredFields();
      lockDerivedFields(false);
      if (metadataDetails) metadataDetails.open = true;
      byId("modal-submit").disabled = false;
      setStatus("GitHub 외 Repository입니다. 아래 Repository details에서 정보를 직접 입력하세요.", "manual");
      return;
    }

    setBusy(true);
    lockDerivedFields(true);
    byId("modal-submit").disabled = true;
    setStatus("GitHub에서 Repository 정보를 확인하는 중…", "loading");
    try {
      const metadata = await apiRequest(
        `/v1/admin/repositories/discover?remote_url=${encodeURIComponent(remoteUrl)}`,
      );
      if (current !== sequence) return;
      setBusy(false);
      remoteInput.value = metadata.remote_url;
      canonicalInput.value = metadata.canonical_name;
      displayInput.value = metadata.display_name;
      displayInput.dataset.autoDiscovered = "true";
      providerInput.value = metadata.provider;
      defaultBranchInput.value = metadata.default_branch_ref;
      lockDerivedFields(true);
      if (metadataDetails) metadataDetails.open = false;
      byId("modal-submit").disabled = false;
      byId("modal-error").hidden = true;
      const branch = metadata.default_branch_ref.replace(/^refs\/heads\//, "");
      setStatus(
        `✓ ${metadata.canonical_name} · ${branch} · ${metadata.visibility} · GitHub #${metadata.provider_repository_id}`,
        "ready",
      );
    } catch (error) {
      if (current !== sequence) return;
      setBusy(false);
      canonicalInput.value = "";
      providerInput.value = "";
      defaultBranchInput.value = "";
      lockDerivedFields(true);
      if (metadataDetails) metadataDetails.open = true;
      byId("modal-submit").disabled = true;
      setStatus("GitHub Repository 정보를 확인하지 못했습니다.", "error");
      showModalError(error);
    }
  };
  const schedule = () => {
    sequence += 1;
    setBusy(false);
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => void discover(), 180);
  };
  displayInput.addEventListener("input", () => {
    displayInput.dataset.autoDiscovered = "false";
  });
  remoteInput.addEventListener("input", schedule);
  remoteInput.addEventListener("blur", () => {
    if (timer !== null) clearTimeout(timer);
    void discover();
  });
  byId("modal-submit").disabled = true;
}

async function openMutationModal(kind, row = null) {
  const editing = row !== null;
  prepareModal(editing ? "설정 변경" : "등록");
  const fields = byId("modal-fields");
  const loading = document.createElement("p");
  loading.textContent = "선택 항목을 불러오는 중...";
  fields.append(loading);
  try {
    fields.replaceChildren();
    if (kind === "repository") {
      byId("modal-title").textContent = editing ? "Repository 변경" : "Repository 등록";
      if (!editing) {
        const remoteField = textField("remote_url", "Repository URL", { type: "url" });
        const displayField = textField("display_name", "Display name");
        const metadataDetails = document.createElement("details");
        metadataDetails.className = "repository-metadata-details";
        const metadataSummary = document.createElement("summary");
        metadataSummary.textContent = "Repository details";
        const metadataHint = document.createElement("span");
        metadataHint.className = "repository-metadata-hint";
        metadataHint.textContent = "GitHub에서는 자동으로 채워집니다";
        metadataSummary.append(metadataHint);
        metadataDetails.append(
          metadataSummary,
          textField("canonical_name", "Canonical name"),
          textField("provider", "Provider"),
          textField("default_branch_ref", "Default branch ref"),
        );
        fields.append(remoteField, displayField, metadataDetails);
        setMutationModal("/v1/admin/repositories", "POST");
        wireRepositoryRegistrationDiscovery(fields);
      } else {
        fields.append(
          textField("display_name", "Display name", { value: row.display_name }),
          textField("remote_url", "Remote URL", { type: "url", value: row.remote_url }),
          textField("default_branch_ref", "Default branch ref", { value: row.default_branch_ref }),
          checkboxField("active", "Active", row.active),
        );
        setMutationModal(`/v1/admin/repositories/${encodeURIComponent(row.repository_id)}`, "PATCH");
      }
    } else if (kind === "tracked-branch") {
      byId("modal-title").textContent = editing ? "Tracked Branch 변경" : "Branch 추적";
      if (!editing) {
        const branchReady = await appendRepositoryBranchSelectors(fields, { activeOnly: true });
        fields.append(textField("vss_project_id", "VSS project ID"));
        setMutationModal("/v1/admin/tracked-branches", "POST");
        byId("modal-submit").disabled = !branchReady;
      } else {
        fields.append(
          textField("vss_project_id", "VSS project ID", { value: row.vss_project_id }),
          checkboxField("tracked", "Tracked", row.tracked),
        );
        setMutationModal(`/v1/admin/tracked-branches/${encodeURIComponent(row.tracked_branch_id)}`, "PATCH");
      }
    } else {
      byId("modal-title").textContent = editing ? "Frontend Binding 변경" : "Frontend Binding 등록";
      fields.append(
        textField("frontend_project_id", "Frontend project ID", { value: row?.frontend_project_id || "", required: !editing }),
        textField("frontend_workspace_name", "Workspace name", { value: row?.frontend_workspace_name || "", required: false, nullable: true }),
      );
      if (editing) fields.querySelector('[name="frontend_project_id"]').disabled = true;
      const branchReady = await appendRepositoryBranchSelectors(fields, {
        repositoryId: row?.repository_id || "",
        branchRef: row?.branch_ref || "",
        activeOnly: !editing,
      });
      fields.append(
        textField("vss_project_id", "VSS project ID", { value: row?.vss_project_id || "" }),
        ...(editing ? [checkboxField("active", "Active", row.active)] : []),
      );
      setMutationModal(
        editing ? `/v1/admin/branch-bindings/${encodeURIComponent(row.binding_id)}` : "/v1/admin/branch-bindings",
        editing ? "PATCH" : "POST",
      );
      byId("modal-submit").disabled = !branchReady;
    }
  } catch (error) {
    fields.replaceChildren();
    showModalError(error);
    byId("modal-submit").hidden = true;
  }
}

function serializeForm(form) {
  const payload = {};
  form.querySelectorAll("input[name], select[name]").forEach((element) => {
    if (element.disabled) return;
    if (element.dataset.boolean === "true") {
      payload[element.name] = element.checked;
    } else if (element.value !== "") {
      payload[element.name] = element.value;
    } else if (element.dataset.nullable === "true") {
      payload[element.name] = null;
    }
  });
  return payload;
}

function showStatusResult(result) {
  const requestId = result?.request_id ? ` · Request ${result.request_id}` : "";
  byId("status-band").classList.remove("error");
  byId("status-band").textContent = result?.reason ? `[${result.reason}] ${result.detail || ""}${requestId}` : "완료했습니다.";
}

function showStatusError(error) {
  const normalized = error instanceof AdminRequestError ? error : new AdminRequestError({ detail: error?.message || String(error) });
  const requestId = normalized.requestId ? ` · Request ${normalized.requestId}` : "";
  const retryable = normalized.retryable ? " · Retryable" : "";
  byId("status-band").classList.add("error");
  byId("status-band").textContent = `[${normalized.reason}] ${normalized.message}${retryable}${requestId}`;
}

function definitionList(values) {
  const list = document.createElement("dl");
  list.className = "detail-list";
  values.forEach(([label, value]) => {
    const term = document.createElement("dt");
    const detail = document.createElement("dd");
    term.textContent = label;
    detail.textContent = valueText(value);
    list.append(term, detail);
  });
  return list;
}

function readOnlyTable(columns, rows) {
  const wrapper = document.createElement("div");
  wrapper.className = "detail-table-wrap";
  const table = document.createElement("table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  columns.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column.replaceAll("_", " ");
    headRow.append(th);
  });
  head.append(headRow);
  const body = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    columns.forEach((column) => {
      const td = document.createElement("td");
      td.textContent = valueText(row[column]);
      tr.append(td);
    });
    body.append(tr);
  });
  table.append(head, body);
  wrapper.append(table);
  return wrapper;
}

async function showSnapshotDetails(snapshotId) {
  prepareModal("Snapshot 상세", { readOnly: true });
  const fields = byId("modal-fields");
  const detail = await apiRequest(`/v1/admin/snapshots/${encodeURIComponent(snapshotId)}`);
  fields.append(definitionList([
    ["Snapshot ID", detail.snapshot_id],
    ["Repository", detail.repository_id],
    ["Branch", detail.branch_ref],
    ["Base revision", detail.base_revision],
    ["Target revision", detail.target_revision],
    ["Snapshot state", detail.state],
    ["Materialized revision", detail.materialized_locator],
    ["VSS state", detail.vss_state],
    ["VSS reason", detail.vss_reason],
    ["VSS detail", detail.vss_detail],
    ["Attempt count", detail.attempt_count],
    ["Changed files", detail.changed_file_count],
    ["Deleted paths", detail.deleted_path_count],
    ["Renames", detail.rename_count],
    ["Created", detail.created_at],
    ["Updated", detail.updated_at],
  ]));
  const heading = document.createElement("h4");
  heading.className = "detail-section";
  heading.textContent = "Attempts";
  fields.append(heading, readOnlyTable(
    ["attempt_number", "started_at", "finished_at", "upstream_status_code", "vss_state", "vss_reason", "retryable", "latency_ms", "request_id"],
    detail.attempts || [],
  ));
}

async function showBranchHistory(trackedBranchId) {
  prepareModal("Branch history", { readOnly: true });
  const fields = byId("modal-fields");
  const items = await fetchAllItems(`/v1/admin/tracked-branches/${encodeURIComponent(trackedBranchId)}/head-history`);
  fields.append(readOnlyTable(
    ["observed_at", "change_type", "previous_head_sha", "observed_head_sha", "sync_run_id"],
    items,
  ));
}

async function showCommitDetails(commitSha) {
  prepareModal("Commit 상세", { readOnly: true });
  const fields = byId("modal-fields");
  const detail = await apiRequest(`/v1/admin/repositories/${encodeURIComponent(state.selectedRepositoryId)}/commits/${encodeURIComponent(commitSha)}`);
  const c = detail.commit;
  const parents = (c.parents || []).map((p) => p.slice(0, 8)).join(", ") || "-";
  const refs = (c.associated_refs || []).map((r) => `${r.ref_type}:${r.name}`).join(", ") || "-";
  fields.append(definitionList([
    ["Commit SHA", c.commit_sha],
    ["Tree SHA", c.tree_sha],
    ["Author", `${c.author_name} <${c.author_email}>`],
    ["Committed at", c.committed_at],
    ["Subject", c.subject],
    ["Parents", parents],
    ["Status", c.status],
    ["Snapshot ID", c.snapshot_id || "-"],
    ["Eligible for answer", c.eligible_for_answer ? "Yes" : "No"],
    ["Unavailable reason", c.unavailable_reason || "-"],
    ["Associated refs", refs],
  ]));
  if (can("operator") && c.status === "git_only") {
    const matBtn = document.createElement("button");
    matBtn.type = "button";
    matBtn.className = "primary";
    matBtn.textContent = "Materialize Snapshot";
    matBtn.style.marginTop = "1rem";
    matBtn.addEventListener("click", async () => {
      matBtn.disabled = true;
      try {
        const repoId = encodeURIComponent(state.selectedRepositoryId);
        const sha = encodeURIComponent(c.commit_sha);
        const res = await apiRequest(`/v1/admin/repositories/${repoId}/commits/${sha}/materialize`, { method: "POST", body: JSON.stringify({}) });
        byId("action-modal").close();
        await loadView();
        showStatusResult({ ok: true, detail: `커밋 ${c.commit_sha.slice(0, 8)}이(가) Snapshot (${res.snapshot_id})으로 승격되었습니다.` });
      } catch (err) {
        showStatusError(err);
        showModalError(err);
      } finally {
        matBtn.disabled = false;
      }
    });
    fields.append(matBtn);
  }
}

async function showCommitComparison() {
  if (state.selectedCommitShas.length !== 2) return;
  prepareModal("Commit Comparison", { readOnly: true });
  const fields = byId("modal-fields");
  const [baseSha, targetSha] = state.selectedCommitShas;
  const endpoint = `/v1/admin/repositories/${encodeURIComponent(state.selectedRepositoryId)}/compare`;
  const result = await apiRequest(withQuery(endpoint, {
    base_revision: baseSha,
    target_revision: targetSha,
  }));

  const statsGrid = document.createElement("div");
  statsGrid.className = "stat-summary-grid";
  const statBoxes = [
    { label: "Ahead", val: `+${result.ahead_count}`, cls: "" },
    { label: "Behind", val: `-${result.behind_count}`, cls: "" },
    { label: "Files", val: result.files_changed, cls: "" },
    { label: "Additions", val: `+${result.additions}`, cls: "stat-additions" },
    { label: "Deletions", val: `-${result.deletions}`, cls: "stat-deletions" },
  ];
  statBoxes.forEach((s) => {
    const box = document.createElement("div");
    box.className = "stat-box";
    const lbl = document.createElement("div");
    lbl.className = "stat-label";
    lbl.textContent = s.label;
    const v = document.createElement("div");
    v.className = `stat-value ${s.cls}`.trim();
    v.textContent = s.val;
    box.append(lbl, v);
    statsGrid.append(box);
  });
  fields.append(statsGrid);

  fields.append(definitionList([
    ["Base revision", `${result.base_revision} (${result.base_status})`],
    ["Target revision", `${result.target_revision} (${result.target_status})`],
  ]));

  const heading = document.createElement("h4");
  heading.className = "detail-section";
  heading.textContent = `Changed Files (${(result.changes || []).length})`;
  fields.append(heading, readOnlyTable(
    ["change_type", "path", "old_path"],
    result.changes || [],
  ));
}

async function submitModal(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const endpoint = form.dataset.endpoint;
  const method = form.dataset.method;
  if (!endpoint || !method) return;
  byId("modal-submit").disabled = true;
  try {
    const result = await apiRequest(endpoint, { method, body: JSON.stringify(serializeForm(form)) });
    byId("action-modal").close();
    resetPagination();
    await loadView();
    showStatusResult(result);
  } catch (error) {
    showModalError(error);
  } finally {
    byId("modal-submit").disabled = false;
  }
}

async function handleRowAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const itemId = button.dataset.itemId;
  const row = state.rows.find((item) => String(rowId(item)) === itemId);
  if (!row) return;
  const action = button.dataset.action;
  if (action === "view-commits") {
    state.selectedRepositoryId = row.repository_id;
    selectView("commits");
    return;
  }
  if (action === "edit-repository") return void openMutationModal("repository", row);
  if (action === "edit-tracked-branch") return void openMutationModal("tracked-branch", row);
  if (action === "edit-binding") return void openMutationModal("branch-binding", row);
  button.disabled = true;
  try {
    if (action === "branch-history") {
      await showBranchHistory(itemId);
      return;
    }
    if (action === "commit-details") {
      await showCommitDetails(row.commit_sha || itemId);
      return;
    }
    if (action === "snapshot-details") {
      await showSnapshotDetails(itemId);
      return;
    }
    if (action === "materialize-commit") {
      const sha = row.commit_sha || itemId;
      const shortSha = sha.slice(0, 8);
      const ok = await confirmAdminAction(
        "Snapshot materialize 확인",
        `커밋 ${shortSha}을(를) Snapshot으로 승격하시겠습니까?\n\n검증된 소스 트리를 준비합니다. VSS 인덱싱은 Snapshot 화면의 Index 버튼으로 별도 요청합니다.`,
        { confirmLabel: "Materialize" },
      );
      if (!ok) return;
      const repoId = encodeURIComponent(state.selectedRepositoryId);
      const commitSha = encodeURIComponent(sha);
      const res = await apiRequest(`/v1/admin/repositories/${repoId}/commits/${commitSha}/materialize`, { method: "POST", body: JSON.stringify({}) });
      await loadView();
      showStatusResult({ ok: true, detail: `커밋 ${shortSha}이(가) Snapshot (${res.snapshot_id})으로 승격되었습니다.` });
      return;
    }
    if (action === "index-tracked-branch") {
      const shortRevision = String(row.current_head_sha || "").slice(0, 8);
      const branch = String(row.branch_ref || "").replace(/^refs\/heads\//, "");
      const ok = await confirmAdminAction(
        "Tracked Branch Index 확인",
        `${branch || "선택한 Branch"} ${shortRevision}을(를) VSS에 인덱싱하시겠습니까?\n\n/home/ubuntu/repos의 Branch working copy를 exact HEAD로 다시 검증한 뒤 기존 VSS Indexer에 force=false로 요청합니다.`,
        { confirmLabel: "Index" },
      );
      if (!ok) return;
    }
    if (action === "index-snapshot") {
      const shortRevision = String(row.target_revision || "").slice(0, 8);
      const ok = await confirmAdminAction(
        "Snapshot Index 확인",
        `Snapshot ${shortRevision || itemId}을(를) VSS에 인덱싱하시겠습니까?\n\n검증된 immutable Snapshot만 제출하며 force 옵션은 사용하지 않습니다.`,
        { confirmLabel: "Index" },
      );
      if (!ok) return;
    }
    if (action === "purge-repository") {
      const repositoryId = String(row.repository_id || itemId);
      const label = row.display_name || row.canonical_name || repositoryId;
      const ok = await confirmAdminAction(
        "Repository 영구 삭제",
        `${label} 등록 정보와 module이 보관하는 branch/snapshot/commit 이력을 영구 삭제하시겠습니까?\n\nVSS vector는 자동으로 삭제하지 않습니다. Vector 화면에서 필요한 project를 별도로 선택해 삭제해야 합니다.`,
        { confirmLabel: "Delete repository" },
      );
      if (!ok) return;
      const id = encodeURIComponent(repositoryId);
      const result = await apiRequest(`/v1/admin/repositories/${id}/purge?confirm=${encodeURIComponent(repositoryId)}`, { method: "DELETE" });
      state.repositoriesList = [];
      resetPagination();
      await loadView();
      showStatusResult(result);
      return;
    }
    if (action === "delete-vss-project") {
      const projectId = String(row.project_id || itemId);
      const ok = await confirmAdminAction(
        "Vector project 영구 삭제",
        `${projectId}의 VSS vector index를 영구 삭제하시겠습니까?\n\nRepository 등록 정보와 Git history는 유지됩니다.`,
        { confirmLabel: "Delete vector" },
      );
      if (!ok) return;
      const id = encodeURIComponent(projectId);
      const result = await apiRequest(`/v1/admin/vss/projects/${id}?confirm=${encodeURIComponent(projectId)}`, { method: "DELETE" });
      resetPagination();
      await loadView();
      showStatusResult(result);
      return;
    }
    const id = encodeURIComponent(itemId);
    const actions = {
      "sync-repository": ["POST", `/v1/admin/repositories/${id}/sync`],
      "deactivate-repository": ["DELETE", `/v1/admin/repositories/${id}`],
      "untrack-branch": ["DELETE", `/v1/admin/tracked-branches/${id}`],
      "index-tracked-branch": ["POST", `/v1/admin/tracked-branches/${id}/index`],
      "deactivate-binding": ["DELETE", `/v1/admin/branch-bindings/${id}`],
      "index-snapshot": ["POST", `/v1/admin/snapshots/${id}/index`],
      "retry-snapshot": ["POST", `/v1/admin/snapshots/${id}/retry`],
    };
    const [method, endpoint] = actions[action];
    const result = await apiRequest(endpoint, { method });
    resetPagination();
    await loadView();
    showStatusResult(result);
  } catch (error) {
    showStatusError(error);
    if (byId("action-modal").open) showModalError(error);
  } finally {
    button.disabled = false;
  }
}

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form).entries());
  const error = byId("login-error");
  error.hidden = true;
  try {
    const response = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "로그인하지 못했습니다.");
    form.reset();
    showApp(payload);
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
  }
});

byId("logout-button").addEventListener("click", async () => {
  try { await apiRequest("/api/auth/logout", { method: "POST" }); } finally { showLogin(); }
});
function installLiquidGlassPointerEffects() {
  const reducedMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
  const finePointer = window.matchMedia?.("(pointer: fine)")?.matches;
  if (reducedMotion || !finePointer) return;

  let frame = 0;
  let lastEvent = null;
  const interactiveSelector = "button, .nav-item, .cell-actions button, .chat-session-button, .sha-link";

  document.addEventListener("pointermove", (event) => {
    lastEvent = event;
    if (frame) return;
    frame = window.requestAnimationFrame(() => {
      frame = 0;
      if (!lastEvent) return;
      const x = Math.max(0, Math.min(100, (lastEvent.clientX / window.innerWidth) * 100));
      const y = Math.max(0, Math.min(100, (lastEvent.clientY / window.innerHeight) * 100));
      document.documentElement.style.setProperty("--glass-pointer-x", `${x.toFixed(2)}%`);
      document.documentElement.style.setProperty("--glass-pointer-y", `${y.toFixed(2)}%`);

      const target = lastEvent.target instanceof Element ? lastEvent.target : null;
      const interactive = target?.closest(interactiveSelector);
      if (!interactive) return;
      const rect = interactive.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      const localX = Math.max(0, Math.min(100, ((lastEvent.clientX - rect.left) / rect.width) * 100));
      const localY = Math.max(0, Math.min(100, ((lastEvent.clientY - rect.top) / rect.height) * 100));
      interactive.style.setProperty("--hover-x", `${localX.toFixed(1)}%`);
      interactive.style.setProperty("--hover-y", `${localY.toFixed(1)}%`);
    });
  }, { passive: true });
}

installLiquidGlassPointerEffects();

byId("runtime-models").addEventListener("change", () => {
  byId("runtime-model-status").textContent = "";
  syncRuntimeModelControls();
});
byId("runtime-model-up").addEventListener("click", () => void controlRuntimeModel("up"));
byId("runtime-model-down").addEventListener("click", () => void controlRuntimeModel("down"));
byId("runtime-model-reload").addEventListener("click", () => void controlRuntimeModel("reload"));
byId("runtime-model-auto-up").addEventListener("change", () => void setRuntimeModelAutoUp());
document.querySelectorAll("button[data-restart-scope]").forEach((button) => {
  button.addEventListener("click", () => void restartModuleServices(button.dataset.restartScope));
});
document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => selectView(button.dataset.view)));
byId("refresh-button").addEventListener("click", () => {
  void loadView();
  void refreshRuntimeModels();
  if (can("admin")) void refreshRuntimeServices();
});
byId("retry-button").addEventListener("click", loadView);
byId("binding-fix-button").addEventListener("click", () => selectView("branch-bindings"));
byId("chat-session-filter").addEventListener("input", renderChatSessionList);
byId("chat-retention-button").addEventListener("click", () => void purgeExpiredChatConversations());
byId("previous-page").addEventListener("click", () => {
  if (!state.previousCursors.length) return;
  state.cursor = state.previousCursors.pop() || null;
  void loadView();
});
byId("next-page").addEventListener("click", () => {
  if (!state.nextCursor) return;
  state.previousCursors.push(state.cursor);
  state.cursor = state.nextCursor;
  void loadView();
});
byId("create-repository").addEventListener("click", () => void openMutationModal("repository"));
byId("create-tracked-branch").addEventListener("click", () => void openMutationModal("tracked-branch"));
byId("create-branch-binding").addEventListener("click", () => void openMutationModal("branch-binding"));
byId("data-body").addEventListener("click", handleRowAction);
byId("modal-form").addEventListener("submit", submitModal);
byId("modal-close").addEventListener("click", () => byId("action-modal").close());
byId("modal-cancel").addEventListener("click", () => byId("action-modal").close());

const repoSelect = byId("repository-filter-select");
if (repoSelect) {
  repoSelect.addEventListener("change", (e) => {
    state.selectedRepositoryId = e.target.value;
    state.selectedCommitShas = [];
    resetPagination();
    void loadView();
  });
}
const compareBtn = byId("compare-commits-button");
if (compareBtn) {
  compareBtn.addEventListener("click", () => void showCommitComparison());
}

apiRequest("/api/auth/session")
  .then((session) => session.authenticated ? showApp(session) : showLogin())
  .catch(showLogin);
