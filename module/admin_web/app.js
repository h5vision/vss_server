"use strict";

const roleLevel = { viewer: 0, operator: 1, admin: 2 };
const views = {
  repositories: {
    title: "Repositories",
    subtitle: "등록된 Git 저장소와 수집 상태",
    endpoint: "/v1/admin/repositories",
    columns: ["canonical_name", "display_name", "provider", "default_branch_ref", "active", "updated_at"],
  },
  "tracked-branches": {
    title: "Tracked branches",
    subtitle: "수집 대상으로 선택된 branch",
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
    columns: ["repository_id", "trigger", "state", "reason", "started_at", "finished_at"],
  },
  snapshots: {
    title: "Snapshots",
    subtitle: "Revision materialization과 VSS 처리 이력",
    endpoint: "/v1/admin/snapshots",
    columns: ["snapshot_id", "vss_project_id", "target_revision", "state", "attempt_count", "updated_at"],
  },
  vss: {
    title: "VSS projects",
    subtitle: "VSS exact project catalog",
    endpoint: "/v1/admin/vss/projects",
    columns: ["project_id", "active", "state", "commit", "chunks", "indexed_at"],
  },
  audit: {
    title: "Audit log",
    subtitle: "관리자 mutation 감사 기록",
    endpoint: "/v1/admin/audit-logs",
    columns: ["created_at", "actor", "action", "target_type", "target_id", "outcome", "request_id"],
  },
};

const state = { session: null, view: "repositories", rows: [], loading: false, empty: false, error: null };
const byId = (id) => document.getElementById(id);

async function apiRequest(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") {
    headers["X-CSRF-Token"] = state.session.csrf_token;
  }
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  if (response.status === 401) {
    showLogin();
    throw new Error("세션이 만료되었습니다.");
  }
  if (response.status === 204) return null;
  let payload;
  try { payload = await response.json(); } catch { payload = null; }
  if (!response.ok) throw new Error(payload?.detail || "요청을 처리하지 못했습니다.");
  return payload;
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
  selectView("repositories");
}

function setTableState(kind, detail = "") {
  state.loading = kind === "loading";
  state.empty = kind === "empty";
  state.error = kind === "error" ? detail : null;
  byId("loading-state").hidden = kind !== "loading";
  byId("empty-state").hidden = kind !== "empty";
  byId("error-state").hidden = kind !== "error";
  byId("error-detail").textContent = detail;
}

function valueText(value) {
  if (value === null || value === undefined || value === "") return "-";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function rowId(row) {
  return row.binding_id || row.tracked_branch_id || row.snapshot_id || row.repository_id || row.project_id || row.audit_id;
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
    if (can("operator")) cell.append(actionButton("Sync", "sync-repository", row));
    if (can("admin")) cell.append(actionButton("Deactivate", "deactivate-repository", row, true));
  }
  if (state.view === "tracked-branches") {
    cell.append(actionButton("History", "branch-history", row));
    if (can("admin")) cell.append(actionButton("Untrack", "untrack-branch", row, true));
  }
  if (state.view === "branch-bindings" && can("admin")) {
    cell.append(actionButton("Deactivate", "deactivate-binding", row, true));
  }
  if (state.view === "snapshots" && can("operator") && row.state === "failed") {
    cell.append(actionButton("Retry", "retry-snapshot", row));
  }
  return cell;
}

function renderTable() {
  const config = views[state.view];
  const headRow = document.createElement("tr");
  config.columns.forEach((column) => {
    const th = document.createElement("th");
    th.textContent = column.replaceAll("_", " ");
    headRow.append(th);
  });
  const hasActions = ["repositories", "tracked-branches", "branch-bindings", "snapshots"].includes(state.view);
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
      if (["state", "outcome"].includes(column) && value !== "-") {
        const pill = document.createElement("span");
        pill.className = "state-pill";
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

async function loadView() {
  setTableState("loading");
  byId("status-band").textContent = "";
  try {
    const payload = await apiRequest(views[state.view].endpoint);
    state.rows = Array.isArray(payload) ? payload : (payload?.items || payload?.branches || []);
    renderTable();
    setTableState(state.rows.length ? "ready" : "empty");
    byId("status-band").textContent = `${state.rows.length} items`;
  } catch (error) {
    state.rows = [];
    renderTable();
    setTableState("error", error.message);
  }
}

function selectView(name) {
  if (!views[name] || (name === "audit" && !can("admin"))) return;
  state.view = name;
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  byId("view-title").textContent = views[name].title;
  byId("view-subtitle").textContent = views[name].subtitle;
  byId("create-repository").hidden = name !== "repositories" || !can("admin");
  byId("create-tracked-branch").hidden = name !== "tracked-branches" || !can("admin");
  byId("create-branch-binding").hidden = name !== "branch-bindings" || !can("admin");
  loadView();
}

function field(name, label, type = "text") {
  const wrapper = document.createElement("label");
  wrapper.textContent = label;
  const input = document.createElement("input");
  input.name = name;
  input.type = type;
  input.required = true;
  wrapper.append(input);
  return wrapper;
}

function openModal(kind) {
  const fields = byId("modal-fields");
  fields.replaceChildren();
  byId("modal-error").hidden = true;
  byId("modal-form").dataset.kind = kind;
  if (kind === "repository") {
    byId("modal-title").textContent = "Repository 등록";
    fields.append(field("canonical_name", "Canonical name"), field("display_name", "Display name"), field("provider", "Provider"), field("remote_url", "Remote URL", "url"), field("default_branch_ref", "Default branch ref"));
  } else if (kind === "tracked-branch") {
    byId("modal-title").textContent = "Branch 추적";
    fields.append(field("repository_id", "Repository ID"), field("branch_ref", "Branch ref"), field("vss_project_id", "VSS project ID"));
  } else {
    byId("modal-title").textContent = "Frontend binding 등록";
    fields.append(field("frontend_project_id", "Frontend project ID"), field("frontend_workspace_name", "Workspace name"), field("repository_id", "Repository ID"), field("branch_ref", "Branch ref"), field("vss_project_id", "VSS project ID"));
  }
  byId("action-modal").showModal();
}

async function submitModal(event) {
  event.preventDefault();
  const kind = event.currentTarget.dataset.kind;
  const payload = Object.fromEntries(new FormData(event.currentTarget).entries());
  const endpoint = kind === "repository"
    ? "/v1/admin/repositories"
    : kind === "tracked-branch"
      ? "/v1/admin/tracked-branches"
      : "/v1/admin/branch-bindings";
  try {
    await apiRequest(endpoint, { method: "POST", body: JSON.stringify(payload) });
    byId("action-modal").close();
    await loadView();
  } catch (error) {
    byId("modal-error").textContent = error.message;
    byId("modal-error").hidden = false;
  }
}

async function handleRowAction(event) {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const id = encodeURIComponent(button.dataset.itemId);
  const actions = {
    "sync-repository": ["POST", `/v1/admin/repositories/${id}/sync`],
    "deactivate-repository": ["DELETE", `/v1/admin/repositories/${id}`],
    "branch-history": ["GET", `/v1/admin/tracked-branches/${id}/head-history`],
    "untrack-branch": ["DELETE", `/v1/admin/tracked-branches/${id}`],
    "deactivate-binding": ["DELETE", `/v1/admin/branch-bindings/${id}`],
    "retry-snapshot": ["POST", `/v1/admin/snapshots/${id}/retry`],
  };
  const [method, endpoint] = actions[button.dataset.action];
  button.disabled = true;
  try {
    const result = await apiRequest(endpoint, { method });
    if (button.dataset.action === "branch-history") {
      byId("modal-title").textContent = "Branch history";
      byId("modal-fields").textContent = JSON.stringify(result?.items || [], null, 2);
      byId("action-modal").showModal();
    } else {
      await loadView();
    }
  } catch (error) {
    byId("status-band").textContent = error.message;
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
document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => selectView(button.dataset.view)));
byId("refresh-button").addEventListener("click", loadView);
byId("retry-button").addEventListener("click", loadView);
byId("create-repository").addEventListener("click", () => openModal("repository"));
byId("create-tracked-branch").addEventListener("click", () => openModal("tracked-branch"));
byId("create-branch-binding").addEventListener("click", () => openModal("branch-binding"));
byId("data-body").addEventListener("click", handleRowAction);
byId("modal-form").addEventListener("submit", submitModal);
byId("modal-close").addEventListener("click", () => byId("action-modal").close());
byId("modal-cancel").addEventListener("click", () => byId("action-modal").close());

apiRequest("/api/auth/session")
  .then((session) => session.authenticated ? showApp(session) : showLogin())
  .catch(showLogin);
