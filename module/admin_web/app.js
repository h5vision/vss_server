"use strict";

const roleLevel = { viewer: 0, operator: 1, admin: 2 };
const listPageSize = 25;
const runtimeModelRefreshMs = 15_000;
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
};
const byId = (id) => document.getElementById(id);
let runtimeModelTimer = null;

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
  if (response.status === 401) {
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

function renderRuntimeModels(payload) {
  const target = byId("runtime-models");
  if (!target) return;
  const models = Array.isArray(payload?.models)
    ? payload.models.filter((name) => typeof name === "string" && name.trim())
    : [];
  target.textContent = models.length
    ? `Ollama: ${models.join(" · ")}`
    : "Ollama: 활성 모델 없음";
  target.title = payload?.available
    ? target.textContent
    : "Ollama 응답 없음 또는 활성 모델 없음";
}

async function refreshRuntimeModels() {
  if (!state.session) return;
  try {
    renderRuntimeModels(await apiRequest("/v1/admin/runtime/models"));
  } catch {
    if (state.session) renderRuntimeModels({ available: false, models: [] });
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
  if (runtimeModelTimer !== null) {
    clearInterval(runtimeModelTimer);
    runtimeModelTimer = null;
  }
  renderRuntimeModels({ available: false, models: [] });
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
  }
  if (state.view === "commits") {
    cell.append(actionButton("Details", "commit-details", row));
    if (can("operator") && row.status === "git_only") {
      cell.append(actionButton("Materialize", "materialize-commit", row));
    }
  }
  if (state.view === "tracked-branches") {
    cell.append(actionButton("History", "branch-history", row));
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
  const hasActions = ["repositories", "tracked-branches", "branch-bindings", "snapshots", "commits"].includes(state.view);
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
        const code = document.createElement("span");
        code.className = "sha-code";
        code.title = row.commit_sha;
        code.textContent = row.commit_sha ? row.commit_sha.slice(0, 8) : "-";
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

async function loadView() {
  const sequence = ++state.loadSequence;
  const requestedView = state.view;
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

function selectView(name) {
  if (!views[name] || (name === "audit" && !can("admin"))) return;
  state.view = name;
  state.selectedCommitShas = [];
  resetPagination();
  document.querySelectorAll(".nav-item").forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  byId("view-title").textContent = views[name].title;
  byId("view-subtitle").textContent = views[name].subtitle;
  byId("create-repository").hidden = name !== "repositories" || !can("admin");
  byId("create-tracked-branch").hidden = name !== "tracked-branches" || !can("admin");
  byId("create-branch-binding").hidden = name !== "branch-bindings" || !can("admin");

  const repoSelect = byId("repository-filter-select");
  if (repoSelect) repoSelect.hidden = name !== "commits";
  const compareBtn = byId("compare-commits-button");
  if (compareBtn) {
    compareBtn.hidden = name !== "commits";
    updateCompareButton();
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
        fields.append(
          textField("canonical_name", "Canonical name"),
          textField("display_name", "Display name"),
          textField("provider", "Provider"),
          textField("remote_url", "Remote URL", { type: "url" }),
          textField("default_branch_ref", "Default branch ref"),
        );
        setMutationModal("/v1/admin/repositories", "POST");
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
      const ok = window.confirm(`커밋 ${shortSha}을(를) Snapshot으로 승격하시겠습니까?\n\n검증된 소스 트리를 준비합니다. VSS 인덱싱은 Snapshot 화면의 Index 버튼으로 별도 요청합니다.`);
      if (!ok) return;
      const repoId = encodeURIComponent(state.selectedRepositoryId);
      const commitSha = encodeURIComponent(sha);
      const res = await apiRequest(`/v1/admin/repositories/${repoId}/commits/${commitSha}/materialize`, { method: "POST", body: JSON.stringify({}) });
      await loadView();
      showStatusResult({ ok: true, detail: `커밋 ${shortSha}이(가) Snapshot (${res.snapshot_id})으로 승격되었습니다.` });
      return;
    }
    if (action === "index-snapshot") {
      const shortRevision = String(row.target_revision || "").slice(0, 8);
      const ok = window.confirm(`Snapshot ${shortRevision || itemId}을(를) VSS에 인덱싱하시겠습니까?\n\n검증된 immutable Snapshot만 제출하며 force 옵션은 사용하지 않습니다.`);
      if (!ok) return;
    }
    const id = encodeURIComponent(itemId);
    const actions = {
      "sync-repository": ["POST", `/v1/admin/repositories/${id}/sync`],
      "deactivate-repository": ["DELETE", `/v1/admin/repositories/${id}`],
      "untrack-branch": ["DELETE", `/v1/admin/tracked-branches/${id}`],
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
document.querySelectorAll(".nav-item").forEach((button) => button.addEventListener("click", () => selectView(button.dataset.view)));
byId("refresh-button").addEventListener("click", () => {
  void loadView();
  void refreshRuntimeModels();
});
byId("retry-button").addEventListener("click", loadView);
byId("binding-fix-button").addEventListener("click", () => selectView("branch-bindings"));
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
