/* ==========================================================================
   TEAM 84FF - Frontend Application Controller (app.js)
   Handles Auth, Live WebSocket + Polling Updates, Charts, CRUD, Bulk Actions
   ========================================================================== */

const STATE = {
  token: localStorage.getItem('ff_admin_token') || null,
  user: null,
  currentView: 'overview',
  accountsLayout: 'table', // 'table' | 'cards'
  sortDir: 'desc',
  statsChartMode: 'levels', // 'levels' | 'regions'
  overview: {
    summary: {
      total_accounts: 0,
      active_accounts: 0,
      inactive_accounts: 0,
      error_accounts: 0,
      total_matches_completed: 0,
      total_matches_searched: 0,
      total_exp: 0,
      average_level: 0,
      total_errors: 0,
    },
    status_breakdown: {
      in_match: 0,
      searching_match: 0,
      online: 0,
      authenticating: 0,
      inactive: 0,
      error: 0,
    },
    region_breakdown: {},
    system: {},
    recent_activity: [],
    accounts: [],
  },
  selectedIds: new Set(),
  openDetailsId: null,
  ws: null,
  pollTimer: null,
};

const STATUS_META = {
  in_match: { label: 'In Match (UDP)', color: '#10b981', cls: 'status-in_match' },
  searching_match: { label: 'Searching Queue', color: '#06b6d4', cls: 'status-searching_match' },
  online: { label: 'Online (Gateway)', color: '#34d399', cls: 'status-online' },
  authenticating: { label: 'Authenticating', color: '#f59e0b', cls: 'status-authenticating' },
  reconnecting: { label: 'Reconnecting', color: '#fbbf24', cls: 'status-reconnecting' },
  inactive: { label: 'Inactive / Idle', color: '#64748b', cls: 'status-inactive' },
  error: { label: 'Error / Alert', color: '#f43f5e', cls: 'status-error' },
};

/* --------------------------------------------------------------------------
   Utility Helpers
   -------------------------------------------------------------------------- */
function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatNumber(n) {
  return Number(n || 0).toLocaleString();
}

function formatRelativeTime(isoStr) {
  if (!isoStr) return 'Never';
  const dt = new Date(isoStr);
  if (isNaN(dt.getTime())) return isoStr;
  const diffSec = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000));
  if (diffSec < 10) return 'Just now';
  if (diffSec < 60) return `${diffSec}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return dt.toISOString().slice(0, 16).replace('T', ' ');
}

function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(Number(seconds || 0)));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <span>${escapeHtml(message)}</span>
    <button class="btn btn-icon" style="background:transparent;color:var(--text-secondary);padding:2px;" onclick="this.parentElement.remove()">✕</button>
  `;
  container.appendChild(toast);
  setTimeout(() => {
    if (toast.parentElement) toast.remove();
  }, 4200);
}

async function apiRequest(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (STATE.token) {
    headers['Authorization'] = `Bearer ${STATE.token}`;
  }
  const resp = await fetch(path, Object.assign({}, options, { headers, credentials: 'same-origin' }));
  if (resp.status === 401 && !path.includes('/api/auth/login')) {
    handleUnauthorized();
    throw new Error('Session expired. Please sign in again.');
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) {
    throw new Error(data.detail || data.message || `Request failed (${resp.status})`);
  }
  return data;
}

/* --------------------------------------------------------------------------
   Authentication Lifecycle
   -------------------------------------------------------------------------- */
async function checkInitialAuth() {
  try {
    const res = await apiRequest('/api/auth/me');
    STATE.user = res.user;
    enterDashboard();
  } catch (_) {
    showLoginScreen();
  }
}

function showLoginScreen() {
  document.getElementById('auth-screen').classList.remove('hidden');
  document.getElementById('app-shell').classList.add('hidden');
  stopLiveSync();
}

function handleUnauthorized() {
  STATE.token = null;
  localStorage.removeItem('ff_admin_token');
  showLoginScreen();
}

function enterDashboard() {
  document.getElementById('auth-screen').classList.add('hidden');
  document.getElementById('app-shell').classList.remove('hidden');
  if (STATE.user && STATE.user.username) {
    document.getElementById('admin-username-label').textContent = STATE.user.username;
  }
  fetchOverviewData();
  loadSupportedRegionsMatrix();
  startLiveSync();
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const btn = document.getElementById('login-submit-btn');
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Authenticating...';

  try {
    const res = await apiRequest('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    STATE.token = res.token;
    STATE.user = res.user;
    localStorage.setItem('ff_admin_token', res.token);
    showToast(`Welcome back, ${res.user.username}!`, 'success');
    enterDashboard();
  } catch (err) {
    showToast(err.message || 'Authentication failed', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Sign In to Dashboard';
  }
});

async function handleLogout() {
  try {
    await apiRequest('/api/auth/logout', { method: 'POST' });
  } catch (_) {}
  handleUnauthorized();
  showToast('Signed out of administrator session.', 'info');
}

async function handleChangeAdminPassword(e) {
  e.preventDefault();
  const cur = document.getElementById('cp-current').value;
  const nw = document.getElementById('cp-new').value;
  try {
    const res = await apiRequest('/api/auth/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: cur, new_password: nw }),
    });
    document.getElementById('change-pw-form').reset();
    showToast(res.message || 'Password updated.', 'success');
  } catch (err) {
    showToast(err.message, 'error');
  }
}

/* --------------------------------------------------------------------------
   Live Updates (WebSocket + Automatic Polling Fallback)
   -------------------------------------------------------------------------- */
function startLiveSync() {
  stopLiveSync();
  connectWebSocket();
  STATE.pollTimer = setInterval(() => {
    if (!STATE.ws || STATE.ws.readyState !== WebSocket.OPEN) {
      fetchOverviewData(true);
    }
  }, 6000);
}

function stopLiveSync() {
  if (STATE.ws) {
    try { STATE.ws.close(); } catch (_) {}
    STATE.ws = null;
  }
  if (STATE.pollTimer) {
    clearInterval(STATE.pollTimer);
    STATE.pollTimer = null;
  }
}

function connectWebSocket() {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const tokenParam = STATE.token ? `?token=${encodeURIComponent(STATE.token)}` : '';
  const wsUrl = `${proto}//${window.location.host}/ws/live${tokenParam}`;

  try {
    const ws = new WebSocket(wsUrl);
    STATE.ws = ws;

    ws.onopen = () => {
      setLiveIndicator('emerald', 'Live WebSocket Active');
    };

    ws.onmessage = (evt) => {
      try {
        const msg = JSON.parse(evt.data);
        if (msg.type === 'dashboard_sync' && msg.data) {
          applyOverviewPayload(msg.data);
        }
      } catch (_) {}
    };

    ws.onclose = () => {
      setLiveIndicator('amber', 'Polling Sync Active');
    };

    ws.onerror = () => {
      setLiveIndicator('amber', 'Polling Sync Active');
    };
  } catch (_) {
    setLiveIndicator('amber', 'Polling Sync Active');
  }
}

function setLiveIndicator(color, text) {
  const dot = document.getElementById('ws-status-dot');
  const lbl = document.getElementById('ws-status-text');
  if (dot) {
    dot.className = `pulse-dot ${color === 'emerald' ? '' : color}`;
  }
  if (lbl) {
    lbl.textContent = text;
  }
}

async function fetchOverviewData(silent = false) {
  try {
    const data = await apiRequest('/api/dashboard/overview');
    applyOverviewPayload(data);
  } catch (err) {
    if (!silent) showToast(err.message, 'error');
  }
}

function applyOverviewPayload(data) {
  if (!data) return;
  STATE.overview = data;

  const badge = document.getElementById('nav-accounts-count');
  if (badge) badge.textContent = (data.accounts || []).length;

  renderOverviewDashboard();
  renderAccountsView();
  renderFullActivityLog();

  if (STATE.openDetailsId) {
    const now = Date.now();
    if (!STATE._lastDetailFetchTs || now - STATE._lastDetailFetchTs > 4500) {
      STATE._lastDetailFetchTs = now;
      refreshOpenAccountDetails(STATE.openDetailsId);
    }
  }
}

/* --------------------------------------------------------------------------
   Navigation & View Switching
   -------------------------------------------------------------------------- */
function switchView(viewName) {
  STATE.currentView = viewName;
  document.querySelectorAll('.view-section').forEach((el) => el.classList.add('hidden'));
  const target = document.getElementById(`view-${viewName}`);
  if (target) target.classList.remove('hidden');

  document.querySelectorAll('.sidebar-nav .nav-item[data-view]').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-view') === viewName);
  });

  const titles = {
    overview: ['Dashboard Overview', 'Real-time multi-account status, telemetry, and BR engine health'],
    accounts: ['Multiple Account Management', 'Add, configure, filter, import/export, and orchestrate accounts'],
    activity: ['Live Activity & Audit Stream', 'Chronological system events, worker transitions, and security logs'],
    system: ['System & Security Configuration', 'Administrator credentials, Fernet encryption vault, and region matrix'],
  };
  const [t, s] = titles[viewName] || titles.overview;
  document.getElementById('topbar-title').textContent = t;
  document.getElementById('topbar-subtitle').textContent = s;

  document.getElementById('sidebar').classList.remove('open');
}

function toggleMobileSidebar() {
  document.getElementById('sidebar').classList.toggle('open');
}

/* --------------------------------------------------------------------------
   Overview Dashboard Rendering (KPIs, Doughnut Chart, Bar Chart, Diagnostics)
   -------------------------------------------------------------------------- */
function renderOverviewDashboard() {
  const sum = STATE.overview.summary || {};
  const sb = STATE.overview.status_breakdown || {};
  const sys = STATE.overview.system || {};
  const diag = sys.diagnostics || {};
  const regMap = STATE.overview.region_breakdown || {};

  document.getElementById('kpi-total-accounts').textContent = formatNumber(sum.total_accounts);
  document.getElementById('kpi-regions-count').textContent = `${Object.keys(regMap).length} Region(s)`;

  document.getElementById('kpi-active-accounts').textContent = formatNumber(sum.active_accounts);
  document.getElementById('kpi-in-match-label').textContent = `${formatNumber(sb.in_match || 0)} In Match`;
  document.getElementById('kpi-searching-label').textContent = `${formatNumber(sb.searching_match || 0)} Queue`;

  document.getElementById('kpi-inactive-accounts').textContent = formatNumber(sum.inactive_accounts);
  document.getElementById('kpi-error-pill').textContent = `${formatNumber(sum.error_accounts)} In Error`;

  const totalAcc = sum.total_accounts || 0;
  const healthyRatio = totalAcc > 0 ? Math.round(((totalAcc - (sum.error_accounts || 0)) / totalAcc) * 100) : 100;
  document.getElementById('kpi-health-ratio').textContent = `${healthyRatio}%`;
  document.getElementById('kpi-total-errors-logged').textContent = `${formatNumber(sum.total_errors || 0)} Alerts`;

  document.getElementById('kpi-matches-completed').textContent = formatNumber(sum.total_matches_completed);
  document.getElementById('kpi-avg-level').textContent = `Avg Lvl ${sum.average_level || 1.0}`;
  document.getElementById('kpi-total-exp').textContent = `${formatNumber(sum.total_exp)} EXP`;

  document.getElementById('kpi-sys-cpu').textContent = `${sys.cpu_percent || 0}%`;
  document.getElementById('kpi-sys-mem').textContent = `RAM ${sys.process_memory_mb || 0} MB`;
  document.getElementById('kpi-sys-uptime').textContent = `${formatDuration(sys.uptime_seconds)} up`;

  // System Diagnostics Panel
  document.getElementById('diag-play-ver').textContent = diag.playstore_version || '1.132.8';
  document.getElementById('diag-rel-ver').textContent = `${diag.release_version || 'OB55'} (${diag.remote_version || 'live'})`;
  document.getElementById('diag-dns-ip').textContent = diag.dns_resolved_ip || 'Pending check';
  document.getElementById('diag-dns-ms').textContent = diag.dns_latency_ms !== null && diag.dns_latency_ms !== undefined ? `${diag.dns_latency_ms} ms` : '—';
  document.getElementById('diag-workers').textContent = `${sys.active_workers || 0} active / ${sys.websocket_clients || 0} WS`;
  document.getElementById('diag-match-cfg').textContent = `${sys.start_match_interval_sec || 1}s interval / ${sys.max_match_duration_sec || 1500}s max`;

  renderStatusDoughnut(sb, totalAcc);
  renderStatisticsBarChart();
  renderActivityFeed(STATE.overview.recent_activity || [], 'overview-activity-feed', 10);
}

function renderStatusDoughnut(sb, totalAcc) {
  const svg = document.getElementById('status-doughnut-svg');
  const legend = document.getElementById('status-legend-list');
  document.getElementById('doughnut-total-num').textContent = formatNumber(totalAcc);

  const segments = [
    { key: 'in_match', label: 'In Match (UDP)', count: sb.in_match || 0, color: '#10b981' },
    { key: 'searching_match', label: 'Searching Queue', count: sb.searching_match || 0, color: '#06b6d4' },
    { key: 'online', label: 'Online (Gateway)', count: sb.online || 0, color: '#34d399' },
    { key: 'authenticating', label: 'Authenticating', count: sb.authenticating || 0, color: '#f59e0b' },
    { key: 'inactive', label: 'Inactive / Stopped', count: sb.inactive || 0, color: '#64748b' },
    { key: 'error', label: 'Error State', count: sb.error || 0, color: '#f43f5e' },
  ];

  const cx = 92.5, cy = 92.5, r = 70;
  const circumference = 2 * Math.PI * r;
  let htmlCircles = `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="rgba(255,255,255,0.06)" stroke-width="18"></circle>`;

  if (totalAcc > 0) {
    let offset = 0;
    for (const seg of segments) {
      if (seg.count <= 0) continue;
      const frac = seg.count / totalAcc;
      const dash = frac * circumference;
      htmlCircles += `
        <circle
          cx="${cx}" cy="${cy}" r="${r}"
          fill="none"
          stroke="${seg.color}"
          stroke-width="18"
          stroke-dasharray="${dash} ${circumference - dash}"
          stroke-dashoffset="${-offset}"
          transform="rotate(-90 ${cx} ${cy})"
        />
      `;
      offset += dash;
    }
  }
  svg.innerHTML = htmlCircles;

  legend.innerHTML = segments.map((seg) => {
    const pct = totalAcc > 0 ? Math.round((seg.count / totalAcc) * 100) : 0;
    return `
      <div class="legend-row">
        <div class="legend-left">
          <span class="legend-swatch" style="background:${seg.color};"></span>
          <span>${escapeHtml(seg.label)}</span>
        </div>
        <div class="mono" style="font-weight:600;">
          ${seg.count} <span style="color:var(--text-muted);font-size:11px;">(${pct}%)</span>
        </div>
      </div>
    `;
  }).join('');
}

function setStatsChartMode(mode) {
  STATE.statsChartMode = mode;
  renderStatisticsBarChart();
}

function renderStatisticsBarChart() {
  const container = document.getElementById('stats-bar-chart-area');
  const tabLevels = document.getElementById('chart-tab-levels');
  const tabRegions = document.getElementById('chart-tab-regions');

  if (tabLevels && tabRegions) {
    tabLevels.className = `btn btn-sm ${STATE.statsChartMode === 'levels' ? 'btn-primary' : 'btn-secondary'}`;
    tabRegions.className = `btn btn-sm ${STATE.statsChartMode === 'regions' ? 'btn-primary' : 'btn-secondary'}`;
  }

  const accounts = STATE.overview.accounts || [];
  if (accounts.length === 0) {
    container.innerHTML = `
      <div class="empty-state" style="padding:24px;">
        <h4>No Accounts Registered Yet</h4>
        <p style="font-size:12.5px;margin-bottom:12px;">Add single or multiple accounts to visualize levels, EXP, and regional telemetry.</p>
        <button class="btn btn-primary btn-sm" onclick="openAddAccountModal()">+ Add First Account</button>
      </div>
    `;
    return;
  }

  if (STATE.statsChartMode === 'regions') {
    const regMap = STATE.overview.region_breakdown || {};
    const entries = Object.entries(regMap).sort((a, b) => b[1] - a[1]);
    const maxCount = Math.max(1, ...entries.map((e) => e[1]));

    container.innerHTML = `
      <div style="display:flex; flex-direction:column; gap:10px;">
        ${entries.map(([reg, cnt]) => {
          const widthPct = Math.max(6, Math.round((cnt / maxCount) * 100));
          return `
            <div>
              <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
                <span class="mono" style="font-weight:700; color:#a5b4fc;">Region ${escapeHtml(reg)}</span>
                <span class="mono">${cnt} account(s)</span>
              </div>
              <div style="height:10px; background:rgba(255,255,255,0.05); border-radius:999px; overflow:hidden;">
                <div style="width:${widthPct}%; height:100%; background:linear-gradient(90deg, #6366f1, #06b6d4); border-radius:999px;"></div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;
    return;
  }

  // Levels & EXP top 6 accounts
  const topAccounts = [...accounts]
    .sort((a, b) => (b.level - a.level) || (b.exp - a.exp))
    .slice(0, 6);
  const maxLvl = Math.max(10, ...topAccounts.map((a) => a.level || 1));

  container.innerHTML = `
    <div style="display:flex; flex-direction:column; gap:11px;">
      ${topAccounts.map((a) => {
        const pct = Math.max(8, Math.min(100, Math.round(((a.level || 1) / maxLvl) * 100)));
        return `
          <div style="cursor:pointer;" onclick="openAccountDetails('${escapeHtml(a.id)}')">
            <div style="display:flex; justify-content:space-between; font-size:12px; margin-bottom:4px;">
              <span>
                <strong>${escapeHtml(a.display_name)}</strong>
                <span class="mono" style="color:var(--text-muted); font-size:11px;">(${escapeHtml(a.uid)} • ${escapeHtml(a.region)})</span>
              </span>
              <span class="mono">
                Lvl <strong>${a.level}</strong> • ${formatNumber(a.exp)} EXP • ${a.matches_completed} Matches
              </span>
            </div>
            <div style="height:9px; background:rgba(255,255,255,0.05); border-radius:999px; overflow:hidden;">
              <div style="width:${pct}%; height:100%; background:linear-gradient(90deg, #10b981, #06b6d4); border-radius:999px;"></div>
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderActivityFeed(items, containerId, limit = 30) {
  const el = document.getElementById(containerId);
  if (!el) return;
  const list = items.slice(0, limit);
  if (list.length === 0) {
    el.innerHTML = `<div class="empty-state" style="padding:20px;">No recent activity events yet.</div>`;
    return;
  }
  el.innerHTML = list.map((item) => `
    <div class="activity-item">
      <span class="activity-dot ${escapeHtml(item.level || 'info')}"></span>
      <div class="activity-body">
        <div class="activity-meta">
          <span class="activity-label">${escapeHtml(item.account_label || 'System')}</span>
          <span class="activity-time">${formatRelativeTime(item.created_at)}</span>
        </div>
        <div class="activity-msg">${escapeHtml(item.message)}</div>
      </div>
    </div>
  `).join('');
}

function renderFullActivityLog() {
  const levelFilter = (document.getElementById('activity-level-filter')?.value || 'all').toLowerCase();
  let items = STATE.overview.recent_activity || [];
  if (levelFilter !== 'all') {
    items = items.filter((i) => (i.level || '').toLowerCase() === levelFilter);
  }
  renderActivityFeed(items, 'full-activity-feed', 100);
}

async function triggerSystemDiagnostics() {
  const btn = document.getElementById('btn-run-diag');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span> Checking DNS & Version...';
  }
  try {
    const res = await apiRequest('/api/system/diagnostics?region=ME', { method: 'POST' });
    const d = res.diagnostics || {};
    showToast(`DNS: ${d.dns_resolved_ip} (${d.dns_latency_ms}ms) | PlayStore: v${d.playstore_version}`, 'success');
    await fetchOverviewData(true);
  } catch (err) {
    showToast(`Diagnostics error: ${err.message}`, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polyline points="23 4 23 10 17 10"></polyline>
          <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
        </svg>
        Verify DNS &amp; Version
      `;
    }
  }
}

/* --------------------------------------------------------------------------
   Multiple Account Management View (Filter, Sort, Table/Card Views, Bulk)
   -------------------------------------------------------------------------- */
function getFilteredSortedAccounts() {
  const search = (document.getElementById('acc-search-input')?.value || '').trim().toLowerCase();
  const regionFilter = (document.getElementById('acc-region-filter')?.value || 'ALL').toUpperCase();
  const statusFilter = (document.getElementById('acc-status-filter')?.value || 'all').toLowerCase();
  const sortBy = document.getElementById('acc-sort-by')?.value || 'created_at';

  let list = [...(STATE.overview.accounts || [])];

  if (search) {
    list = list.filter((a) =>
      (a.id || '').toLowerCase().includes(search) ||
      (a.uid || '').toLowerCase().includes(search) ||
      (a.game_account_id || '').toLowerCase().includes(search) ||
      (a.display_name || '').toLowerCase().includes(search) ||
      (a.nickname || '').toLowerCase().includes(search) ||
      (a.status_message || '').toLowerCase().includes(search)
    );
  }

  if (regionFilter !== 'ALL') {
    list = list.filter((a) => (a.region || '').toUpperCase() === regionFilter);
  }

  if (statusFilter !== 'all') {
    if (statusFilter === 'active') {
      const act = new Set(['in_match', 'searching_match', 'online', 'authenticating', 'reconnecting']);
      list = list.filter((a) => act.has(a.status));
    } else {
      list = list.filter((a) => a.status === statusFilter);
    }
  }

  const dir = STATE.sortDir === 'asc' ? 1 : -1;
  list.sort((a, b) => {
    const va = a[sortBy] ?? '';
    const vb = b[sortBy] ?? '';
    if (typeof va === 'number' && typeof vb === 'number') {
      return (va - vb) * dir;
    }
    return String(va).localeCompare(String(vb), undefined, { numeric: true }) * dir;
  });

  return list;
}

function toggleSortDir() {
  STATE.sortDir = STATE.sortDir === 'desc' ? 'asc' : 'desc';
  const btn = document.getElementById('acc-sort-dir-btn');
  if (btn) btn.textContent = STATE.sortDir === 'desc' ? 'DESC ↓' : 'ASC ↑';
  renderAccountsView();
}

function setAccountsLayout(layout) {
  STATE.accountsLayout = layout;
  const tblBtn = document.getElementById('layout-btn-table');
  const crdBtn = document.getElementById('layout-btn-cards');
  const tblView = document.getElementById('accounts-table-view');
  const crdView = document.getElementById('accounts-cards-view');

  if (layout === 'table') {
    tblBtn.className = 'btn btn-sm btn-primary';
    tblBtn.style = '';
    crdBtn.className = 'btn btn-sm';
    crdBtn.style = 'background:transparent;color:var(--text-secondary);';
    tblView.classList.remove('hidden');
    crdView.classList.add('hidden');
  } else {
    crdBtn.className = 'btn btn-sm btn-primary';
    crdBtn.style = '';
    tblBtn.className = 'btn btn-sm';
    tblBtn.style = 'background:transparent;color:var(--text-secondary);';
    crdView.classList.remove('hidden');
    tblView.classList.add('hidden');
  }
  renderAccountsView();
}

function renderStatusBadgeHtml(statusKey) {
  const meta = STATUS_META[statusKey] || STATUS_META.inactive;
  return `<span class="status-badge ${meta.cls}"><span class="dot"></span>${escapeHtml(meta.label)}</span>`;
}

function renderAccountsView() {
  const filtered = getFilteredSortedAccounts();
  const tbody = document.getElementById('accounts-table-body');
  const cardsContainer = document.getElementById('accounts-cards-view');

  // Prune any deleted IDs from selection
  const existingIds = new Set((STATE.overview.accounts || []).map((a) => a.id));
  for (const id of [...STATE.selectedIds]) {
    if (!existingIds.has(id)) STATE.selectedIds.delete(id);
  }
  updateBulkActionBar();

  if (filtered.length === 0) {
    const emptyHtml = `
      <div class="empty-state">
        <h4>No Matching Accounts Found</h4>
        <p style="margin-bottom:14px;">Add new accounts or adjust your search/region/status filters.</p>
        <div style="display:flex; justify-content:center; gap:10px;">
          <button class="btn btn-primary btn-sm" onclick="openAddAccountModal()">+ Add Account</button>
          <button class="btn btn-secondary btn-sm" onclick="openBulkAddModal()">+ Bulk Add (UID:PASS)</button>
        </div>
      </div>
    `;
    tbody.innerHTML = `<tr><td colspan="9">${emptyHtml}</td></tr>`;
    cardsContainer.innerHTML = `<div style="grid-column: 1 / -1;">${emptyHtml}</div>`;
    return;
  }

  const activeSet = new Set(['in_match', 'searching_match', 'online', 'authenticating', 'reconnecting']);

  // Render Table Rows
  tbody.innerHTML = filtered.map((acc) => {
    const isSel = STATE.selectedIds.has(acc.id);
    const isRunning = activeSet.has(acc.status);
    const errBadge = acc.error_count > 0
      ? `<span class="mono" style="font-size:11px; color:#fb7185; background:rgba(244,63,94,0.14); padding:1px 6px; border-radius:4px; margin-left:6px;">${acc.error_count} err</span>`
      : '';

    return `
      <tr class="${isSel ? 'selected-row' : ''}">
        <td>
          <input type="checkbox" ${isSel ? 'checked' : ''} onchange="toggleSelectAccount('${escapeHtml(acc.id)}', this.checked)" />
        </td>
        <td>
          <div class="mono" style="font-weight:700; color:var(--text-primary); cursor:pointer;" onclick="openAccountDetails('${escapeHtml(acc.id)}')">
            ${escapeHtml(acc.id)}
          </div>
          <div class="mono" style="font-size:11.5px; color:var(--text-secondary);">
            UID: ${escapeHtml(acc.uid)} ${acc.game_account_id !== 'Pending' ? `• GameID: ${escapeHtml(acc.game_account_id)}` : ''}
          </div>
        </td>
        <td>
          <div style="font-weight:600; color:var(--text-primary); cursor:pointer;" onclick="openAccountDetails('${escapeHtml(acc.id)}')">
            ${escapeHtml(acc.display_name)} ${errBadge}
          </div>
          <div style="font-size:11.5px; color:var(--text-secondary);">
            Nickname: <span class="mono">${escapeHtml(acc.nickname || '—')}</span>
          </div>
        </td>
        <td>
          <span class="region-tag">${escapeHtml(acc.region)} <span style="opacity:0.65;">${escapeHtml(acc.region_prefix)}</span></span>
        </td>
        <td>
          <div>${renderStatusBadgeHtml(acc.status)}</div>
          <div style="font-size:11.5px; color:var(--text-secondary); margin-top:4px; max-width:250px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(acc.status_message)}">
            ${escapeHtml(acc.status_message || '—')}
          </div>
        </td>
        <td class="mono">
          <div style="font-weight:700;">Lvl ${acc.level}</div>
          <div style="font-size:11.5px; color:var(--text-secondary);">
            ${formatNumber(acc.exp)} EXP ${acc.exp_gained > 0 ? `<span style="color:var(--status-emerald);">(+${formatNumber(acc.exp_gained)})</span>` : ''}
          </div>
        </td>
        <td class="mono">
          <div style="font-weight:700;">${formatNumber(acc.matches_completed)} done</div>
          <div style="font-size:11.5px; color:var(--text-secondary);">${formatNumber(acc.matches_searched)} queued</div>
        </td>
        <td class="mono" style="font-size:12px; color:var(--text-secondary);">
          ${formatRelativeTime(acc.last_activity)}
        </td>
        <td style="text-align:right; white-space:nowrap;">
          <div style="display:inline-flex; gap:6px;">
            ${isRunning
              ? `<button class="btn btn-amber btn-sm" onclick="stopAccountWorker('${escapeHtml(acc.id)}')">Stop</button>`
              : `<button class="btn btn-emerald btn-sm" onclick="startAccountWorker('${escapeHtml(acc.id)}')">Start</button>`
            }
            <button class="btn btn-secondary btn-sm" onclick="openAccountDetails('${escapeHtml(acc.id)}')">Details</button>
            <button class="btn btn-secondary btn-sm" onclick="openEditAccountModal('${escapeHtml(acc.id)}')">Edit</button>
            <button class="btn btn-rose btn-sm" onclick="deleteSingleAccount('${escapeHtml(acc.id)}')">✕</button>
          </div>
        </td>
      </tr>
    `;
  }).join('');

  // Render Cards Grid
  cardsContainer.innerHTML = filtered.map((acc) => {
    const isSel = STATE.selectedIds.has(acc.id);
    const isRunning = activeSet.has(acc.status);
    return `
      <div class="account-card">
        <div class="account-card-top">
          <div style="display:flex; align-items:flex-start; gap:10px;">
            <input type="checkbox" style="margin-top:4px;" ${isSel ? 'checked' : ''} onchange="toggleSelectAccount('${escapeHtml(acc.id)}', this.checked)" />
            <div>
              <div style="font-weight:700; font-size:15px; cursor:pointer;" onclick="openAccountDetails('${escapeHtml(acc.id)}')">
                ${escapeHtml(acc.display_name)}
              </div>
              <div class="mono" style="font-size:11.5px; color:var(--text-secondary);">
                ${escapeHtml(acc.id)} • UID ${escapeHtml(acc.uid)}
              </div>
            </div>
          </div>
          <span class="region-tag">${escapeHtml(acc.region)}</span>
        </div>

        <div style="display:flex; align-items:center; justify-content:space-between; gap:8px;">
          ${renderStatusBadgeHtml(acc.status)}
          <span class="mono" style="font-size:11.5px; color:var(--text-muted);">${formatRelativeTime(acc.last_activity)}</span>
        </div>

        <div style="font-size:12px; color:var(--text-secondary); background:rgba(255,255,255,0.02); padding:7px 10px; border-radius:6px; border:1px solid var(--border-subtle);">
          ${escapeHtml(acc.status_message || 'Ready')}
        </div>

        <div class="account-card-metrics">
          <div class="acc-metric-box">
            <span>Level</span>
            <strong>Lvl ${acc.level}</strong>
          </div>
          <div class="acc-metric-box">
            <span>Total EXP</span>
            <strong>${formatNumber(acc.exp)}</strong>
          </div>
          <div class="acc-metric-box">
            <span>Matches</span>
            <strong>${acc.matches_completed} / ${acc.matches_searched}</strong>
          </div>
        </div>

        <div style="display:flex; justify-content:space-between; align-items:center; gap:8px; margin-top:auto;">
          <div style="display:flex; gap:6px;">
            ${isRunning
              ? `<button class="btn btn-amber btn-sm" onclick="stopAccountWorker('${escapeHtml(acc.id)}')">Stop</button>`
              : `<button class="btn btn-emerald btn-sm" onclick="startAccountWorker('${escapeHtml(acc.id)}')">Start</button>`
            }
            <button class="btn btn-secondary btn-sm" onclick="restartAccountWorker('${escapeHtml(acc.id)}')">Restart</button>
          </div>
          <div style="display:flex; gap:6px;">
            <button class="btn btn-secondary btn-sm" onclick="openAccountDetails('${escapeHtml(acc.id)}')">Details</button>
            <button class="btn btn-secondary btn-sm" onclick="openEditAccountModal('${escapeHtml(acc.id)}')">Edit</button>
            <button class="btn btn-rose btn-sm" onclick="deleteSingleAccount('${escapeHtml(acc.id)}')">✕</button>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

/* --------------------------------------------------------------------------
   Selection & Bulk Operations
   -------------------------------------------------------------------------- */
function toggleSelectAccount(accountId, checked) {
  if (checked) {
    STATE.selectedIds.add(accountId);
  } else {
    STATE.selectedIds.delete(accountId);
  }
  renderAccountsView();
}

function toggleSelectAllFiltered(checked) {
  const filtered = getFilteredSortedAccounts();
  for (const acc of filtered) {
    if (checked) STATE.selectedIds.add(acc.id);
    else STATE.selectedIds.delete(acc.id);
  }
  renderAccountsView();
}

function clearAllSelections() {
  STATE.selectedIds.clear();
  const selAll = document.getElementById('select-all-checkbox');
  if (selAll) selAll.checked = false;
  renderAccountsView();
}

function updateBulkActionBar() {
  const bar = document.getElementById('bulk-action-bar');
  const cnt = document.getElementById('bulk-selected-count');
  if (!bar || !cnt) return;
  const n = STATE.selectedIds.size;
  if (n > 0) {
    bar.classList.remove('hidden');
    cnt.textContent = `${n} account(s) selected`;
  } else {
    bar.classList.add('hidden');
  }
}

async function executeBulkAction(action, region = null) {
  const ids = Array.from(STATE.selectedIds);
  if (ids.length === 0) return;

  if (action === 'delete') {
    if (!confirm(`Are you sure you want to permanently remove ${ids.length} selected account(s)?`)) {
      return;
    }
  }

  try {
    const res = await apiRequest('/api/accounts/bulk-action', {
      method: 'POST',
      body: JSON.stringify({ account_ids: ids, action, region }),
    });
    showToast(`Bulk '${action}' completed: ${res.processed_count} processed.`, res.error_count ? 'warning' : 'success');
    if (action === 'delete') clearAllSelections();
    await fetchOverviewData(true);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function handleBulkRegionChange(selectEl) {
  const reg = selectEl.value;
  if (!reg) return;
  executeBulkAction('change_region', reg);
  selectEl.value = '';
}

/* --------------------------------------------------------------------------
   Single Account Worker Controls & CRUD Modals
   -------------------------------------------------------------------------- */
async function startAccountWorker(accountId) {
  try {
    const res = await apiRequest(`/api/accounts/${encodeURIComponent(accountId)}/start`, { method: 'POST' });
    showToast(res.message || 'Worker started', 'success');
    await fetchOverviewData(true);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function stopAccountWorker(accountId) {
  try {
    const res = await apiRequest(`/api/accounts/${encodeURIComponent(accountId)}/stop`, { method: 'POST' });
    showToast(res.message || 'Worker stopped', 'info');
    await fetchOverviewData(true);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function restartAccountWorker(accountId) {
  try {
    const res = await apiRequest(`/api/accounts/${encodeURIComponent(accountId)}/restart`, { method: 'POST' });
    showToast(res.message || 'Worker restarted', 'success');
    await fetchOverviewData(true);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

async function deleteSingleAccount(accountId) {
  const acc = (STATE.overview.accounts || []).find((a) => a.id === accountId);
  const label = acc ? `${acc.display_name} (${acc.uid})` : accountId;
  if (!confirm(`Remove account ${label}? Active worker will be stopped.`)) return;

  try {
    await apiRequest(`/api/accounts/${encodeURIComponent(accountId)}`, { method: 'DELETE' });
    STATE.selectedIds.delete(accountId);
    if (STATE.openDetailsId === accountId) closeAccountDetails();
    showToast(`Removed account ${label}.`, 'info');
    await fetchOverviewData(true);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function openAddAccountModal() {
  document.getElementById('modal-account-title').textContent = 'Add New Account';
  document.getElementById('form-account-id').value = '';
  document.getElementById('form-uid').value = '';
  document.getElementById('form-uid').disabled = false;
  document.getElementById('form-display-name').value = '';
  document.getElementById('form-password').value = '';
  document.getElementById('form-password').required = true;
  document.getElementById('form-password-label').textContent = 'Account Password / Secret * (Encrypted via Fernet)';
  document.getElementById('form-region').value = 'ME';
  document.getElementById('form-mode-id').value = '1';
  document.getElementById('form-map-id').value = '1';
  document.getElementById('form-notes').value = '';
  document.getElementById('modal-account').classList.remove('hidden');
}

function openEditAccountModal(accountId) {
  const acc = (STATE.overview.accounts || []).find((a) => a.id === accountId);
  if (!acc) return;
  document.getElementById('modal-account-title').textContent = `Edit Account — ${acc.display_name}`;
  document.getElementById('form-account-id').value = acc.id;
  document.getElementById('form-uid').value = acc.uid;
  document.getElementById('form-uid').disabled = true;
  document.getElementById('form-display-name').value = acc.display_name;
  document.getElementById('form-password').value = '';
  document.getElementById('form-password').required = false;
  document.getElementById('form-password-label').textContent = 'Rotate Password (leave blank to keep current encrypted password)';
  document.getElementById('form-region').value = acc.region || 'ME';
  document.getElementById('form-mode-id').value = acc.mode_id || 1;
  document.getElementById('form-map-id').value = acc.map_id || 1;
  document.getElementById('form-notes').value = acc.notes || '';
  document.getElementById('modal-account').classList.remove('hidden');
}

function closeAccountModal() {
  document.getElementById('modal-account').classList.add('hidden');
}

async function submitAccountForm(e) {
  e.preventDefault();
  const editId = document.getElementById('form-account-id').value.trim();
  const uid = document.getElementById('form-uid').value.trim();
  const display_name = document.getElementById('form-display-name').value.trim();
  const password = document.getElementById('form-password').value;
  const region = document.getElementById('form-region').value;
  const mode_id = parseInt(document.getElementById('form-mode-id').value || '1', 10);
  const map_id = parseInt(document.getElementById('form-map-id').value || '1', 10);
  const notes = document.getElementById('form-notes').value.trim();

  try {
    if (editId) {
      const payload = { display_name, region, mode_id, map_id, notes };
      if (password && password.trim()) payload.password = password.trim();
      await apiRequest(`/api/accounts/${encodeURIComponent(editId)}`, {
        method: 'PUT',
        body: JSON.stringify(payload),
      });
      showToast('Account updated successfully.', 'success');
    } else {
      await apiRequest('/api/accounts', {
        method: 'POST',
        body: JSON.stringify({ uid, password, display_name: display_name || null, region, mode_id, map_id, notes }),
      });
      showToast('Account added & credentials encrypted in Fernet vault.', 'success');
    }
    closeAccountModal();
    await fetchOverviewData(true);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

/* --------------------------------------------------------------------------
   Bulk Add & Metadata Import / Export
   -------------------------------------------------------------------------- */
function openBulkAddModal() {
  document.getElementById('bulk-raw-lines').value = '';
  document.getElementById('modal-bulk-add').classList.remove('hidden');
}

function closeBulkAddModal() {
  document.getElementById('modal-bulk-add').classList.add('hidden');
}

async function submitBulkAddForm(e) {
  e.preventDefault();
  const raw_lines = document.getElementById('bulk-raw-lines').value;
  const default_region = document.getElementById('bulk-default-region').value;
  try {
    const res = await apiRequest('/api/accounts/bulk-add', {
      method: 'POST',
      body: JSON.stringify({ raw_lines, default_region }),
    });
    showToast(`Added ${res.created_count} account(s). ${res.error_count ? `(${res.error_count} skipped)` : ''}`, res.error_count ? 'warning' : 'success');
    closeBulkAddModal();
    await fetchOverviewData(true);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

function exportAccountsMetadata(format = 'json') {
  const ids = STATE.selectedIds.size > 0 ? `&ids=${encodeURIComponent(Array.from(STATE.selectedIds).join(','))}` : '';
  const url = `/api/accounts/export/metadata?format=${encodeURIComponent(format)}${ids}`;
  window.open(url, '_blank');
  showToast(`Exporting sanitized account metadata (${format.toUpperCase()})...`, 'info');
}

function openImportMetadataModal() {
  document.getElementById('import-meta-json').value = '';
  document.getElementById('modal-import-meta').classList.remove('hidden');
}

function closeImportMetadataModal() {
  document.getElementById('modal-import-meta').classList.add('hidden');
}

function loadMetadataFileIntoTextarea(fileInput) {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = (e) => {
    document.getElementById('import-meta-json').value = e.target.result || '';
  };
  reader.readAsText(file);
}

async function submitImportMetadata(e) {
  e.preventDefault();
  const raw = document.getElementById('import-meta-json').value.trim();
  try {
    const parsed = JSON.parse(raw);
    const accountsList = Array.isArray(parsed) ? parsed : (parsed.accounts || []);
    if (!Array.isArray(accountsList) || accountsList.length === 0) {
      throw new Error('JSON must contain an "accounts" array or be a non-empty array of account objects.');
    }
    const res = await apiRequest('/api/accounts/import/metadata', {
      method: 'POST',
      body: JSON.stringify({ accounts: accountsList, default_region: 'ME' }),
    });
    showToast(`Imported ${res.imported_count} new, updated ${res.updated_count} existing account(s).`, 'success');
    closeImportMetadataModal();
    await fetchOverviewData(true);
  } catch (err) {
    showToast(`Import error: ${err.message}`, 'error');
  }
}

/* --------------------------------------------------------------------------
   Dedicated Account Details Slide-Over Drawer
   -------------------------------------------------------------------------- */
async function openAccountDetails(accountId) {
  STATE.openDetailsId = accountId;
  document.getElementById('details-drawer-overlay').classList.remove('hidden');
  await refreshOpenAccountDetails(accountId);
}

function closeAccountDetails() {
  STATE.openDetailsId = null;
  document.getElementById('details-drawer-overlay').classList.add('hidden');
}

function handleDrawerBackdropClick(e) {
  if (e.target.id === 'details-drawer-overlay') {
    closeAccountDetails();
  }
}

async function refreshOpenAccountDetails(accountId) {
  try {
    const res = await apiRequest(`/api/accounts/${encodeURIComponent(accountId)}`);
    const acc = res.account;
    if (!acc) return;

    document.getElementById('det-display-name').textContent = acc.display_name;
    const badgeEl = document.getElementById('det-status-badge');
    const meta = STATUS_META[acc.status] || STATUS_META.inactive;
    badgeEl.className = `status-badge ${meta.cls}`;
    badgeEl.innerHTML = `<span class="dot"></span>${escapeHtml(meta.label)}`;

    document.getElementById('det-id-subtitle').textContent = `System ID: ${acc.id} • Guest UID: ${acc.uid} • Game ID: ${acc.game_account_id}`;
    document.getElementById('det-status-msg').textContent = acc.status_message || '—';

    document.getElementById('det-sys-id').textContent = acc.id;
    document.getElementById('det-uid').textContent = acc.uid;
    document.getElementById('det-game-id').textContent = acc.game_account_id || 'Pending';
    document.getElementById('det-nickname').textContent = acc.nickname || '—';
    document.getElementById('det-region').textContent = `${acc.region} (${acc.region_prefix} / ${acc.region_lang})`;
    document.getElementById('det-level-exp').textContent = `Lvl ${acc.level} • ${formatNumber(acc.exp)} EXP`;
    document.getElementById('det-exp-gained').textContent = `+${formatNumber(acc.exp_gained || 0)} EXP`;
    document.getElementById('det-matches').textContent = `${acc.matches_completed} / ${acc.matches_searched}`;
    document.getElementById('det-last-activity').textContent = formatRelativeTime(acc.last_activity);

    // Device profile
    const dev = acc.device_profile || {};
    document.getElementById('det-device-box').innerHTML = `
      <div class="detail-stat-card">
        <div class="lbl">Brand &amp; Model</div>
        <div class="val" style="font-size:13px;">${escapeHtml(dev.brand || '—')} ${escapeHtml(dev.model || '')}</div>
      </div>
      <div class="detail-stat-card">
        <div class="lbl">GPU &amp; OS</div>
        <div class="val" style="font-size:12px;">${escapeHtml(dev.gpu_renderer || '—')}</div>
      </div>
      <div class="detail-stat-card">
        <div class="lbl">Device Fingerprint</div>
        <div class="val" style="font-size:12px;">${escapeHtml(dev.device_fingerprint_masked || 'Google|****')}</div>
      </div>
    `;

    // Error History
    const errs = acc.error_history || [];
    document.getElementById('det-error-count').textContent = errs.length;
    const errList = document.getElementById('det-error-list');
    if (errs.length === 0) {
      errList.innerHTML = `<div style="padding:12px; color:var(--status-emerald); background:rgba(16,185,129,0.08); border:1px solid rgba(16,185,129,0.2); border-radius:8px; font-size:12.5px;">Zero errors recorded for this account.</div>`;
    } else {
      errList.innerHTML = errs.map((e) => `
        <div class="error-entry">
          <div class="error-entry-top">
            <span class="error-stage-badge">${escapeHtml(e.stage)}</span>
            <span class="mono" style="font-size:11px; color:var(--text-muted);">${formatRelativeTime(e.created_at)}</span>
          </div>
          <div style="color:var(--text-primary);">${escapeHtml(e.error_message)}</div>
        </div>
      `).join('');
    }

    // Account Logs
    renderActivityFeed(acc.recent_logs || [], 'det-logs-list', 30);
  } catch (_) {}
}

async function drawerAction(act) {
  const id = STATE.openDetailsId;
  if (!id) return;
  if (act === 'start') await startAccountWorker(id);
  else if (act === 'stop') await stopAccountWorker(id);
  else if (act === 'restart') await restartAccountWorker(id);
  else if (act === 'edit') openEditAccountModal(id);
}

async function clearDrawerAccountErrors() {
  const id = STATE.openDetailsId;
  if (!id) return;
  try {
    await apiRequest(`/api/accounts/${encodeURIComponent(id)}/errors`, { method: 'DELETE' });
    showToast('Cleared error history for account.', 'info');
    await refreshOpenAccountDetails(id);
    await fetchOverviewData(true);
  } catch (err) {
    showToast(err.message, 'error');
  }
}

/* --------------------------------------------------------------------------
   System Region Matrix Loader
   -------------------------------------------------------------------------- */
async function loadSupportedRegionsMatrix() {
  try {
    const res = await apiRequest('/api/regions');
    const tbody = document.getElementById('regions-matrix-body');
    if (!tbody) return;
    tbody.innerHTML = (res.regions || []).map((r) => {
      const ka = r.code === 'ME' ? '0219' : (r.code === 'IND' ? '0214' : '0215');
      return `
        <tr>
          <td><span class="region-tag">${escapeHtml(r.code)}</span></td>
          <td class="mono">${escapeHtml(r.prefix)}</td>
          <td class="mono">${escapeHtml(r.lang)}</td>
          <td class="mono">0x${ka}</td>
        </tr>
      `;
    }).join('');
  } catch (_) {}
}

/* Initialize on DOM Load */
window.addEventListener('DOMContentLoaded', () => {
  checkInitialAuth();
});
