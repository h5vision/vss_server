/**
 * Vision Snapshot Admin Web - Single Page Application Core
 */

class AdminApp {
  constructor() {
    this.apiBase = window.location.origin.includes(':4180') ? '' : 'http://127.0.0.1:8000';
    this.token = localStorage.getItem('vss_admin_token') || '';
    this.actorId = localStorage.getItem('vss_admin_actor') || 'admin';
    this.activeTab = 'overview';
    this.pollingInterval = null;
    this.isPolling = true;

    // Cache
    this.repositories = [];
    this.trackedBranches = [];
    this.snapshots = [];

    this.init();
  }

  init() {
    this.bindEvents();
    this.updateAuthDisplay();
    this.switchTab('overview');
    this.startPolling();
  }

  bindEvents() {
    // Tab Navigation
    document.querySelectorAll('.nav-item').forEach(btn => {
      btn.addEventListener('click', () => {
        const tab = btn.getAttribute('data-tab');
        this.switchTab(tab);
      });
    });

    // Manual & Auto Refresh
    document.getElementById('manualRefreshBtn')?.addEventListener('click', () => this.refreshCurrentTab());
    document.getElementById('autoRefreshToggle')?.addEventListener('change', (e) => {
      this.isPolling = e.target.checked;
      if (this.isPolling) {
        this.startPolling();
      } else {
        this.stopPolling();
      }
    });

    // Auth Modal
    document.getElementById('openAuthModalBtn')?.addEventListener('click', () => {
      document.getElementById('adminTokenInput').value = this.token;
      document.getElementById('actorIdInput').value = this.actorId;
      this.openModal('authModal');
    });
    document.getElementById('saveAuthBtn')?.addEventListener('click', () => this.saveAuth());

    // Repository Form
    document.getElementById('openAddRepoModalBtn')?.addEventListener('click', () => this.openModal('addRepoModal'));
    document.getElementById('submitAddRepoBtn')?.addEventListener('click', () => this.submitAddRepo());

    // Filters & Search
    document.getElementById('repoSearchInput')?.addEventListener('input', () => this.renderRepositoriesTable());
    document.getElementById('branchRepoFilter')?.addEventListener('change', () => this.renderTrackedBranchesTable());
    document.getElementById('snapshotStateFilter')?.addEventListener('change', () => this.renderSnapshotsTable());
    document.getElementById('snapshotProjectFilter')?.addEventListener('input', () => this.renderSnapshotsTable());
  }

  // --- API Client ---
  async apiRequest(endpoint, options = {}) {
    const headers = {
      'Accept': 'application/json',
      ...(options.headers || {})
    };

    if (this.token) {
      headers['X-Admin-Token'] = this.token;
    }

    if (options.body && typeof options.body === 'object' && !(options.body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(options.body);
    }

    const url = `${this.apiBase}/v1/admin${endpoint}`;
    try {
      const response = await fetch(url, { ...options, headers });
      const data = await response.json().catch(() => null);

      if (!response.ok) {
        const errorReason = data?.reason || `HTTP_${response.status}`;
        let errorDetail = data?.detail || response.statusText || '요청 처리에 실패했습니다.';
        if (data?.errors && Array.isArray(data.errors) && data.errors.length > 0) {
          const errorMsgs = data.errors.map(e => `${e.location.filter(p => p !== 'body').join('.')}: ${e.message}`).join('; ');
          errorDetail = `${errorDetail} (${errorMsgs})`;
        }
        const reqId = data?.request_id || response.headers.get('X-Request-ID');

        if (response.status === 401) {
          this.setAuthStatus(false, '인증 필요 (401)');
          this.showToast('인증 오류', 'Admin API 토큰을 설정해주세요.', 'error');
        } else if (response.status === 403) {
          this.showToast('권한 부족', `해당 작업을 수행할 권한이 없습니다. (403 ${errorReason})`, 'error');
        } else {
          this.showToast(`오류: ${errorReason}`, `${errorDetail} (Request ID: ${reqId || 'N/A'})`, 'error');
        }
        throw new Error(errorDetail);
      }

      this.setAuthStatus(true, '인증됨 (Admin/Operator)');
      return data;
    } catch (err) {
      if (err.name === 'TypeError' && err.message.includes('fetch')) {
        this.showToast('네트워크 오류', '백엔드 서버(127.0.0.1:8000)에 연결할 수 없습니다.', 'error');
      }
      throw err;
    }
  }

  // --- Auth Management ---
  saveAuth() {
    this.token = document.getElementById('adminTokenInput').value.trim();
    this.actorId = document.getElementById('actorIdInput').value.trim() || 'admin';
    localStorage.setItem('vss_admin_token', this.token);
    localStorage.setItem('vss_admin_actor', this.actorId);
    this.closeModal('authModal');
    this.showToast('인증 정보 저장', '관리자 토큰이 갱신되었습니다.', 'success');
    this.updateAuthDisplay();
    this.refreshCurrentTab();
  }

  updateAuthDisplay() {
    const roleLabel = document.getElementById('authRoleLabel');
    const actorLabel = document.getElementById('authActorLabel');
    const indicator = document.getElementById('authIndicator');

    actorLabel.textContent = `접속자: ${this.actorId}`;
    if (this.token) {
      roleLabel.textContent = '토큰 설정됨';
      indicator.className = 'auth-indicator authenticated';
    } else {
      roleLabel.textContent = '토큰 미설정 (Viewer)';
      indicator.className = 'auth-indicator';
    }
  }

  setAuthStatus(isValid, label) {
    const roleLabel = document.getElementById('authRoleLabel');
    const indicator = document.getElementById('authIndicator');
    roleLabel.textContent = label;
    indicator.className = `auth-indicator ${isValid ? 'authenticated' : ''}`;
  }

  // --- Polling & Navigation ---
  startPolling() {
    this.stopPolling();
    this.pollingInterval = setInterval(() => {
      if (this.isPolling) {
        this.refreshCurrentTab(true);
      }
    }, 5000);
  }

  stopPolling() {
    if (this.pollingInterval) {
      clearInterval(this.pollingInterval);
      this.pollingInterval = null;
    }
  }

  switchTab(tabId) {
    this.activeTab = tabId;
    document.querySelectorAll('.nav-item').forEach(btn => {
      btn.classList.toggle('active', btn.getAttribute('data-tab') === tabId);
    });
    document.querySelectorAll('.view-pane').forEach(pane => {
      pane.classList.toggle('active', pane.id === `view-${tabId}`);
    });

    const titles = {
      'overview': ['대시보드', 'Vision Snapshot Backend 실시간 모니터링 및 요약'],
      'repositories': ['저장소 관리', '원격 Git 저장소 등록, 브랜치 카탈로그 탐색 및 수동 동기화'],
      'tracked-branches': ['추적 브랜치 관리', '자동 인덱싱 대상 브랜치 및 HEAD SHA 관측 이력'],
      'snapshots': ['스냅샷 & 인덱싱 현황', '불변 승격 스냅샷 상태머신 모니터링 및 멱등 재시도'],
      'vss-projects': ['VSS 프로젝트', '업스트림 VSS 서버(Port 8200) 등록 인덱스 현황'],
      'audit-logs': ['감사 로그', '관리자 CUD 및 동기화/재시도 트랜잭션 감사 이력']
    };

    const [title, subtitle] = titles[tabId] || ['관리자 포털', ''];
    document.getElementById('pageTitle').textContent = title;
    document.getElementById('pageSubtitle').textContent = subtitle;

    this.refreshCurrentTab();
  }

  refreshCurrentTab(isBackground = false) {
    switch (this.activeTab) {
      case 'overview':
        this.loadOverview();
        break;
      case 'repositories':
        this.loadRepositories();
        break;
      case 'tracked-branches':
        this.loadTrackedBranches();
        break;
      case 'snapshots':
        this.loadSnapshots();
        break;
      case 'vss-projects':
        this.loadVssProjects();
        break;
      case 'audit-logs':
        this.loadAuditLogs();
        break;
    }
  }

  // --- View: Overview ---
  async loadOverview() {
    try {
      const [repos, branches, snapshots, vssProjects, syncRuns] = await Promise.allSettled([
        this.apiRequest('/repositories'),
        this.apiRequest('/tracked-branches'),
        this.apiRequest('/snapshots?limit=5'),
        this.apiRequest('/vss/projects'),
        this.apiRequest('/sync-runs?limit=5')
      ]);

      if (repos.status === 'fulfilled') {
        const activeCount = repos.value.items.filter(r => r.active).length;
        document.getElementById('metricActiveRepos').textContent = activeCount;
      }
      if (branches.status === 'fulfilled') {
        const trackedCount = branches.value.items.filter(b => b.tracked).length;
        document.getElementById('metricTrackedBranches').textContent = trackedCount;
      }
      if (snapshots.status === 'fulfilled') {
        document.getElementById('metricTotalSnapshots').textContent = snapshots.value.items.length;
        this.renderOverviewSnapshots(snapshots.value.items);
      }
      if (vssProjects.status === 'fulfilled') {
        document.getElementById('metricVssProjects').textContent = vssProjects.value.items.length;
      }
      if (syncRuns.status === 'fulfilled') {
        this.renderOverviewSyncRuns(syncRuns.value.items);
      }
    } catch (e) {
      console.error('Overview load error', e);
    }
  }

  renderOverviewSyncRuns(items) {
    const tbody = document.getElementById('overviewSyncRunsTbody');
    if (!items || items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">동기화 실행 이력이 없습니다.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(run => `
      <tr>
        <td><span class="badge badge-subtle">${run.trigger}</span></td>
        <td>${this.getStatusBadge(run.state)}</td>
        <td class="text-sm">${run.reason || run.detail || '-'}</td>
        <td class="text-sm font-mono text-muted">${this.formatDate(run.started_at)}</td>
      </tr>
    `).join('');
  }

  renderOverviewSnapshots(items) {
    const tbody = document.getElementById('overviewSnapshotsTbody');
    if (!items || items.length === 0) {
      tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">스냅샷 이력이 없습니다.</td></tr>';
      return;
    }
    tbody.innerHTML = items.map(snap => `
      <tr>
        <td>
          <div class="font-bold text-sm">${snap.vss_project_id}</div>
          <div class="text-muted font-mono text-xs">${snap.branch_ref}</div>
        </td>
        <td>${this.getStatusBadge(snap.state)}</td>
        <td class="font-mono text-xs">${snap.target_revision?.slice(0, 8)}...</td>
        <td class="text-sm font-mono text-muted">${this.formatDate(snap.created_at)}</td>
      </tr>
    `).join('');
  }

  // --- View: Repositories ---
  async loadRepositories() {
    try {
      const data = await this.apiRequest('/repositories');
      this.repositories = data.items || [];
      this.renderRepositoriesTable();
    } catch (e) {
      document.getElementById('repositoriesTbody').innerHTML =
        '<tr><td colspan="6" class="text-center text-danger">저장소 목록을 불러올 수 없습니다.</td></tr>';
    }
  }

  renderRepositoriesTable() {
    const query = document.getElementById('repoSearchInput')?.value.toLowerCase() || '';
    const filtered = this.repositories.filter(r => {
      const name = (r.display_name || r.canonical_name || '').toLowerCase();
      const url = (r.remote_url || '').toLowerCase();
      return name.includes(query) || url.includes(query);
    });

    const tbody = document.getElementById('repositoriesTbody');
    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">등록된 저장소가 없습니다.</td></tr>';
      return;
    }

    tbody.innerHTML = filtered.map(repo => `
      <tr>
        <td><strong class="font-bold">${repo.display_name || repo.canonical_name}</strong></td>
        <td><span class="font-mono text-sm text-secondary">${repo.remote_url}</span></td>
        <td><span class="badge badge-subtle font-mono">${repo.default_branch_ref || 'refs/heads/main'}</span></td>
        <td>${repo.active ? '<span class="badge badge-success">Active</span>' : '<span class="badge badge-subtle">Inactive</span>'}</td>
        <td class="text-sm font-mono text-muted">${this.formatDate(repo.created_at)}</td>
        <td>
          <div class="d-flex gap-2">
            <button class="btn btn-secondary btn-sm" onclick="app.openCatalogModal('${repo.repository_id}', '${repo.display_name || repo.canonical_name}', '${repo.remote_url}')">
              🌿 브랜치 카탈로그
            </button>
            <button class="btn btn-primary btn-sm" onclick="app.triggerSync('${repo.repository_id}')">
              ⚡ 수동 동기화
            </button>
            ${repo.active ? `
              <button class="btn btn-ghost btn-sm text-danger" onclick="app.deactivateRepo('${repo.repository_id}')">
                비활성화
              </button>
            ` : ''}
          </div>
        </td>
      </tr>
    `).join('');
  }

  async submitAddRepo() {
    const name = document.getElementById('newRepoName').value.trim();
    let remoteUrl = document.getElementById('newRepoRemoteUrl').value.trim();
    let defaultBranch = document.getElementById('newRepoDefaultBranch').value.trim() || 'refs/heads/main';
    if (!defaultBranch.startsWith('refs/heads/')) {
      defaultBranch = `refs/heads/${defaultBranch}`;
    }

    if (!name || !remoteUrl) {
      this.showToast('입력 오류', '저장소 이름과 원격 Git URL을 입력해주세요.', 'error');
      return;
    }

    if (!remoteUrl.startsWith('http://') && !remoteUrl.startsWith('https://')) {
      remoteUrl = `https://${remoteUrl}`;
    }

    const canonical = (name.toLowerCase().replace(/[^a-z0-9_-]/g, '-') || 'repo').slice(0, 64);

    try {
      await this.apiRequest('/repositories', {
        method: 'POST',
        body: {
          canonical_name: canonical,
          display_name: name,
          provider: 'github',
          remote_url: remoteUrl,
          default_branch_ref: defaultBranch,
          active: true
        }
      });
      this.showToast('등록 완료', `저장소 [${name}]가 성공적으로 등록되었습니다.`, 'success');
      this.closeModal('addRepoModal');
      document.getElementById('newRepoName').value = '';
      document.getElementById('newRepoRemoteUrl').value = '';
      this.loadRepositories();
    } catch (e) {
      console.error(e);
    }
  }

  async deactivateRepo(repoId) {
    if (!confirm('이 저장소를 비활성화하시겠습니까? (추적 브랜치 수집이 중단됩니다)')) return;
    try {
      await this.apiRequest(`/repositories/${repoId}`, { method: 'DELETE' });
      this.showToast('비활성화 완료', '저장소가 비활성화되었습니다.', 'success');
      this.loadRepositories();
    } catch (e) {
      console.error(e);
    }
  }

  async triggerSync(repoId) {
    try {
      this.showToast('동기화 시작', '원격 저장소 수동 동기화를 시작합니다...', 'info');
      const res = await this.apiRequest(`/repositories/${repoId}/sync`, { method: 'POST' });
      this.showToast('동기화 완료', res.detail || '수동 동기화가 완료되었습니다.', 'success');
      this.loadOverview();
      this.loadRepositories();
    } catch (e) {
      console.error(e);
    }
  }

  // --- View: Remote Branch Catalog ---
  async openCatalogModal(repoId, name, remoteUrl) {
    document.getElementById('catalogRepoName').textContent = name;
    document.getElementById('catalogRepoUrl').textContent = remoteUrl;
    const tbody = document.getElementById('catalogTbody');
    tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">원격 브랜치 탐색 중 (git ls-remote)...</td></tr>';
    this.openModal('catalogModal');

    try {
      const catalog = await this.apiRequest(`/repositories/${repoId}/catalog`);
      if (!catalog.branches || catalog.branches.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">원격 브랜치를 찾을 수 없습니다.</td></tr>';
        return;
      }

      tbody.innerHTML = catalog.branches.map(b => {
        const sha = b.head_sha || b.remote_head_sha || '';
        return `
        <tr>
          <td><span class="font-mono font-bold">${b.branch_ref}</span></td>
          <td><span class="font-mono text-xs">${sha ? sha.slice(0, 10) + '...' : '-'}</span></td>
          <td>${b.tracked ? '<span class="badge badge-success">추적 중</span>' : '<span class="badge badge-subtle">미추적</span>'}</td>
          <td><span class="font-mono text-sm">${b.vss_project_id || '<em class="text-muted">자동 생성</em>'}</span></td>
          <td>
            ${b.tracked ? `
              <button class="btn btn-secondary btn-sm" disabled>추적 중</button>
            ` : `
              <button class="btn btn-primary btn-sm" onclick="app.trackBranch('${repoId}', '${b.branch_ref}')">
                ➕ 추적 등록
              </button>
            `}
          </td>
        </tr>
      `;
      }).join('');
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center text-danger">브랜치 카탈로그를 탐색할 수 없습니다.</td></tr>';
    }
  }

  async trackBranch(repoId, branchRef) {
    const branchShort = branchRef.replace('refs/heads/', '').replace(/\//g, '-');
    const defaultVssId = `${branchShort}-project`;
    let vssProjectId = prompt(`추적할 VSS Project ID를 입력하세요:`, defaultVssId);
    if (vssProjectId === null) return; // 사용자가 취소(Cancel) 누름
    vssProjectId = vssProjectId.trim() || defaultVssId;

    try {
      const payload = {
        repository_id: repoId,
        branch_ref: branchRef,
        vss_project_id: vssProjectId
      };
      await this.apiRequest('/tracked-branches', {
        method: 'POST',
        body: payload
      });
      this.showToast('추적 등록 완료', `[${branchRef}] 브랜치가 VSS ID [${vssProjectId}]로 등록되었습니다.`, 'success');
      this.closeModal('catalogModal');
      this.loadTrackedBranches();
      this.loadRepositories();
    } catch (e) {
      console.error(e);
    }
  }

  // --- View: Tracked Branches ---
  async loadTrackedBranches() {
    try {
      const data = await this.apiRequest('/tracked-branches');
      this.trackedBranches = data.items || [];
      this.renderTrackedBranchesTable();
    } catch (e) {
      document.getElementById('trackedBranchesTbody').innerHTML =
        '<tr><td colspan="7" class="text-center text-danger">추적 브랜치를 불러올 수 없습니다.</td></tr>';
    }
  }

  renderTrackedBranchesTable() {
    const tbody = document.getElementById('trackedBranchesTbody');
    if (this.trackedBranches.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-center text-muted">등록된 추적 브랜치가 없습니다.</td></tr>';
      return;
    }

    tbody.innerHTML = this.trackedBranches.map(tb => `
      <tr>
        <td><span class="font-mono text-xs text-muted">${tb.repository_id.slice(0, 8)}...</span></td>
        <td><strong class="font-mono font-bold">${tb.branch_ref}</strong></td>
        <td><span class="badge badge-info font-mono">${tb.vss_project_id}</span></td>
        <td><span class="font-mono text-xs">${tb.current_head_sha ? tb.current_head_sha.slice(0, 10) + '...' : '<em class="text-muted">미수집</em>'}</span></td>
        <td>${tb.tracked ? '<span class="badge badge-success">Tracking</span>' : '<span class="badge badge-subtle">Paused</span>'}</td>
        <td class="text-sm font-mono text-muted">${this.formatDate(tb.last_checked_at || tb.created_at)}</td>
        <td>
          <div class="d-flex gap-2">
            <button class="btn btn-secondary btn-sm" onclick="app.openBranchHistory('${tb.tracked_branch_id}', '${tb.branch_ref}')">
              📜 HEAD 이력
            </button>
            <button class="btn btn-ghost btn-sm text-danger" onclick="app.untrackBranch('${tb.tracked_branch_id}')">
              해제
            </button>
          </div>
        </td>
      </tr>
    `).join('');
  }

  async untrackBranch(branchId) {
    if (!confirm('이 브랜치의 추적을 해제하시겠습니까?')) return;
    try {
      await this.apiRequest(`/tracked-branches/${branchId}`, { method: 'DELETE' });
      this.showToast('해제 완료', '브랜치 추적이 해제되었습니다.', 'success');
      this.loadTrackedBranches();
    } catch (e) {
      console.error(e);
    }
  }

  async openBranchHistory(branchId, branchRef) {
    document.getElementById('historyBranchRef').textContent = branchRef;
    const timeline = document.getElementById('historyTimeline');
    timeline.innerHTML = '<p class="text-muted text-center">HEAD 변경 이력 로딩 중...</p>';
    this.openModal('historyModal');

    try {
      const data = await this.apiRequest(`/tracked-branches/${branchId}/history`);
      const items = data.items || [];
      if (items.length === 0) {
        timeline.innerHTML = '<p class="text-muted text-center">관측된 변경 이력이 없습니다.</p>';
        return;
      }

      timeline.innerHTML = items.map(item => `
        <div class="timeline-node">
          <div class="timeline-header">
            <span class="badge badge-subtle">${item.change_type}</span>
            <span class="timeline-time">${this.formatDate(item.observed_at)}</span>
          </div>
          <div class="timeline-body">
            HEAD: ${item.observed_head_sha ? `<code>${item.observed_head_sha.slice(0, 10)}</code>` : 'None'}
            ${item.previous_head_sha ? `<span class="text-muted">(이전: ${item.previous_head_sha.slice(0, 10)})</span>` : ''}
          </div>
        </div>
      `).join('');
    } catch (e) {
      timeline.innerHTML = '<p class="text-danger text-center">이력을 불러올 수 없습니다.</p>';
    }
  }

  // --- View: Snapshots & Details & Retry ---
  async loadSnapshots() {
    try {
      const data = await this.apiRequest('/snapshots');
      this.snapshots = data.items || [];
      this.renderSnapshotsTable();
    } catch (e) {
      document.getElementById('snapshotsTbody').innerHTML =
        '<tr><td colspan="8" class="text-center text-danger">스냅샷 목록을 불러올 수 없습니다.</td></tr>';
    }
  }

  renderSnapshotsTable() {
    const stateFilter = document.getElementById('snapshotStateFilter')?.value || '';
    const projQuery = document.getElementById('snapshotProjectFilter')?.value.toLowerCase() || '';

    const filtered = this.snapshots.filter(s => {
      const matchesState = !stateFilter || s.state === stateFilter;
      const matchesProj = !projQuery || s.vss_project_id.toLowerCase().includes(projQuery);
      return matchesState && matchesProj;
    });

    const tbody = document.getElementById('snapshotsTbody');
    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="8" class="text-center text-muted">조건에 일치하는 스냅샷이 없습니다.</td></tr>';
      return;
    }

    tbody.innerHTML = filtered.map(s => `
      <tr>
        <td><strong class="font-mono text-sm">${s.vss_project_id}</strong></td>
        <td><span class="font-mono text-xs text-muted">${s.branch_ref}</span></td>
        <td><span class="font-mono text-xs">${s.target_revision.slice(0, 10)}...</span></td>
        <td>${this.getStatusBadge(s.state)}</td>
        <td><span class="badge badge-subtle">${s.vss_state || 'none'}</span></td>
        <td><span class="badge badge-subtle font-mono">${s.attempt_count}회</span></td>
        <td class="text-sm font-mono text-muted">${this.formatDate(s.created_at)}</td>
        <td>
          <button class="btn btn-secondary btn-sm" onclick="app.openSnapshotDetail('${s.snapshot_id}')">
            🔍 상세 & 재시도
          </button>
        </td>
      </tr>
    `).join('');
  }

  async openSnapshotDetail(snapshotId) {
    const metaContainer = document.getElementById('snapshotDetailMeta');
    const attemptsTbody = document.getElementById('snapshotAttemptsTbody');
    const actionArea = document.getElementById('snapshotRetryActionArea');

    metaContainer.innerHTML = '<p class="text-muted">스냅샷 상세 정보 로딩 중...</p>';
    attemptsTbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">시도 이력 로딩 중...</td></tr>';
    actionArea.innerHTML = '';
    this.openModal('snapshotDetailModal');

    try {
      const snap = await this.apiRequest(`/snapshots/${snapshotId}`);

      metaContainer.innerHTML = `
        <div class="detail-item">
          <span class="detail-item-label">Snapshot ID</span>
          <span class="detail-item-value font-mono text-xs">${snap.snapshot_id}</span>
        </div>
        <div class="detail-item">
          <span class="detail-item-label">VSS Project ID</span>
          <span class="detail-item-value font-mono font-bold">${snap.vss_project_id}</span>
        </div>
        <div class="detail-item">
          <span class="detail-item-label">상태 (State)</span>
          <span class="detail-item-value">${this.getStatusBadge(snap.state)}</span>
        </div>
        <div class="detail-item">
          <span class="detail-item-label">Target Revision</span>
          <span class="detail-item-value font-mono text-xs">${snap.target_revision}</span>
        </div>
        <div class="detail-item">
          <span class="detail-item-label">Materialized Locator</span>
          <span class="detail-item-value font-mono text-xs">${snap.materialized_locator || 'N/A'}</span>
        </div>
        <div class="detail-item">
          <span class="detail-item-label">VSS 사유 / 상세</span>
          <span class="detail-item-value text-xs">${snap.vss_reason || '-'} / ${snap.vss_detail || '-'}</span>
        </div>
      `;

      if (!snap.attempts || snap.attempts.length === 0) {
        attemptsTbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">기록된 인덱싱 시도 이력이 없습니다.</td></tr>';
      } else {
        attemptsTbody.innerHTML = snap.attempts.map(att => `
          <tr>
            <td><strong>#${att.attempt_number}</strong></td>
            <td class="font-mono text-xs">${this.formatDate(att.started_at)}</td>
            <td><span class="badge ${att.upstream_status_code === 202 ? 'badge-success' : 'badge-danger'}">${att.upstream_status_code || '-'}</span></td>
            <td><span class="badge badge-subtle">${att.vss_state || 'none'}</span></td>
            <td class="text-xs">${att.vss_reason || '-'}</td>
            <td class="font-mono text-xs">${att.latency_ms ? att.latency_ms.toFixed(1) + 'ms' : '-'}</td>
          </tr>
        `).join('');
      }

      actionArea.innerHTML = `
        <div class="d-flex align-items-center justify-content-between">
          <div>
            <strong>안전한 멱등 재시도 (Idempotent Retry)</strong>
            <p class="text-muted text-xs">불변 승격된 트리를 재검증하고 VSS 서버에 새 attempt를 발행합니다.</p>
          </div>
          <button class="btn btn-primary" onclick="app.retrySnapshot('${snap.snapshot_id}')">
            ⚡ 스냅샷 재시도
          </button>
        </div>
      `;
    } catch (e) {
      metaContainer.innerHTML = '<p class="text-danger">스냅샷 상세 정보를 불러올 수 없습니다.</p>';
    }
  }

  async retrySnapshot(snapshotId) {
    try {
      this.showToast('재시도 요청', 'VSS 인덱싱 재시도를 요청 중입니다...', 'info');
      const res = await this.apiRequest(`/snapshots/${snapshotId}/retry`, { method: 'POST' });
      this.showToast('재시도 접수', res.detail || '스냅샷 재시도가 안전하게 접수되었습니다.', 'success');
      this.openSnapshotDetail(snapshotId);
      this.loadSnapshots();
    } catch (e) {
      console.error(e);
    }
  }

  // --- View: VSS Projects Proxy ---
  async loadVssProjects() {
    const tbody = document.getElementById('vssProjectsTbody');
    try {
      const data = await this.apiRequest('/vss/projects');
      const items = data.items || [];
      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" class="text-center text-muted">VSS에 등록된 활성 프로젝트가 없습니다.</td></tr>';
        return;
      }

      tbody.innerHTML = items.map(p => `
        <tr>
          <td><strong class="font-mono">${p.project_id}</strong></td>
          <td>${this.getStatusBadge(p.state || 'done')}</td>
          <td><span class="font-mono text-xs">${p.commit ? p.commit.slice(0, 10) + '...' : '-'}</span></td>
          <td><span class="badge badge-info font-mono">${p.chunks || 0} chunks</span></td>
          <td class="text-sm font-mono text-muted">${p.indexed_at ? this.formatDate(p.indexed_at) : '-'}</td>
        </tr>
      `).join('');
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="5" class="text-center text-danger">VSS 프로젝트 목록을 불러올 수 없습니다 (VSS Server 연결 확인 필요).</td></tr>';
    }
  }

  // --- View: Audit Logs ---
  async loadAuditLogs() {
    const tbody = document.getElementById('auditLogsTbody');
    try {
      const data = await this.apiRequest('/audit-logs');
      const items = data.items || [];
      if (items.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" class="text-center text-muted">감사 로그가 없습니다.</td></tr>';
        return;
      }

      tbody.innerHTML = items.map(log => `
        <tr>
          <td class="font-mono text-xs text-muted">${this.formatDate(log.created_at)}</td>
          <td><span class="font-bold text-sm">${log.actor}</span></td>
          <td><span class="badge badge-primary">${log.action}</span></td>
          <td><span class="font-mono text-xs">${log.target_type}: ${log.target_id.slice(0, 12)}...</span></td>
          <td>${log.outcome === 'succeeded' ? '<span class="badge badge-success">Succeeded</span>' : '<span class="badge badge-danger">Failed</span>'}</td>
          <td class="text-xs font-mono text-muted">${log.details ? JSON.stringify(log.details) : '-'}</td>
        </tr>
      `).join('');
    } catch (e) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-center text-danger">감사 로그를 불러올 수 없습니다.</td></tr>';
    }
  }

  // --- UI Helpers ---
  openModal(id) {
    document.getElementById(id)?.classList.add('show');
  }

  closeModal(id) {
    document.getElementById(id)?.classList.remove('show');
  }

  showToast(title, message, type = 'info') {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `
      <div class="toast-content">
        <h4>${title}</h4>
        <p>${message}</p>
      </div>
    `;

    container.appendChild(toast);
    setTimeout(() => {
      toast.style.opacity = '0';
      toast.style.transform = 'translateY(10px)';
      setTimeout(() => toast.remove(), 200);
    }, 4000);
  }

  getStatusBadge(state) {
    const map = {
      'completed': '<span class="badge badge-success">Completed</span>',
      'done': '<span class="badge badge-success">Done</span>',
      'indexing': '<span class="badge badge-info">Indexing</span>',
      'submitting': '<span class="badge badge-warning">Submitting</span>',
      'accepted': '<span class="badge badge-primary">Accepted</span>',
      'materialized': '<span class="badge badge-info">Materialized</span>',
      'failed': '<span class="badge badge-danger">Failed</span>',
      'aborted': '<span class="badge badge-danger">Aborted</span>',
      'running': '<span class="badge badge-warning">Running</span>'
    };
    return map[state] || `<span class="badge badge-subtle">${state || 'unknown'}</span>`;
  }

  formatDate(isoString) {
    if (!isoString) return '-';
    try {
      const d = new Date(isoString);
      return d.toLocaleString('ko-KR', {
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false
      });
    } catch {
      return isoString;
    }
  }

  // --- View: GitHub Webhook Guide ---
  openWebhookModal() {
    const host = window.location.hostname || 'localhost';
    const proto = window.location.protocol;
    const webhookUrl = `${proto}//${host}${window.location.port ? `:${window.location.port}` : ''}/postrecive`;
    const input = document.getElementById('webhookPayloadUrlInput');
    if (input) {
      input.value = webhookUrl;
    }
    this.openModal('webhookModal');
  }

  async copyWebhookUrl() {
    const input = document.getElementById('webhookPayloadUrlInput');
    if (!input || !input.value) return;
    try {
      await navigator.clipboard.writeText(input.value);
      this.showToast('복사 완료', 'Webhook URL이 클립보드에 복사되었습니다.', 'success');
    } catch {
      input.select();
      document.execCommand('copy');
      this.showToast('복사 완료', 'Webhook URL이 클립보드에 복사되었습니다.', 'success');
    }
  }
}

// Global App Instance
const app = new AdminApp();

