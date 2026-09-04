// evaTorrent Frontend Client Application

let torrents = [];
let selectedTorrentHash = null;
let currentFilter = 'all';
let searchQuery = '';
let activeInspectorTab = 'overview';
let ws = null;
let reconnectTimer = null;

let authState = {
  setup_required: false,
  is_authenticated: false,
  user_email: null,
  google_enabled: false,
  google_client_id: null,
};

// --- Authentication & Setup ---

async function checkAuth() {
  try {
    const res = await fetch('/api/auth/status');
    if (!res.ok) return false;
    const data = await res.json();
    authState = data;

    const modal = document.getElementById('modal-auth');
    const viewSetup = document.getElementById('view-setup');
    const viewLogin = document.getElementById('view-login');
    const userGroup = document.getElementById('nav-user-group');
    const userDisplay = document.getElementById('user-display');

    if (data.setup_required) {
      modal.classList.remove('hidden');
      viewSetup.classList.remove('hidden');
      viewLogin.classList.add('hidden');
      userGroup.classList.add('hidden');
      document.getElementById('auth-title').textContent = 'Setup Administrator';
      document.getElementById('auth-subtitle').textContent = 'Register your primary email to secure evaTorrent';
      return false;
    } else if (!data.is_authenticated) {
      modal.classList.remove('hidden');
      viewSetup.classList.add('hidden');
      viewLogin.classList.remove('hidden');
      userGroup.classList.add('hidden');
      document.getElementById('auth-title').textContent = 'Sign In to evaTorrent';
      document.getElementById('auth-subtitle').textContent = 'Authorized Administrator Access';

      if (data.admin_email_masked) {
        document.getElementById('login-email').placeholder = `Authorized: ${data.admin_email_masked}`;
      }

      setupGoogleAuth(data.google_client_id);
      return false;
    } else {
      modal.classList.add('hidden');
      viewSetup.classList.add('hidden');
      viewLogin.classList.add('hidden');
      userGroup.classList.remove('hidden');
      userDisplay.textContent = data.user_email;
      connectWebSocket();
      return true;
    }
  } catch (err) {
    console.error('Error checking auth:', err);
    return false;
  }
}

function switchAuthTab(tab) {
  document.querySelectorAll('.auth-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.auth-tab-content').forEach(c => c.classList.remove('active'));

  if (tab === 'otp') {
    document.getElementById('tab-btn-otp').classList.add('active');
    document.getElementById('auth-tab-otp').classList.add('active');
  } else if (tab === 'google') {
    document.getElementById('tab-btn-google').classList.add('active');
    document.getElementById('auth-tab-google').classList.add('active');
  }
}

async function submitSetup() {
  const email = document.getElementById('setup-email').value.trim();
  const googleId = document.getElementById('setup-google-id').value.trim();

  if (!email) {
    showToast('Please provide an administrator email.', 'error');
    return;
  }

  const btn = document.getElementById('btn-setup-submit');
  btn.disabled = true;
  btn.textContent = 'Saving setup...';

  try {
    const res = await fetch('/api/auth/setup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ admin_email: email, google_client_id: googleId || null }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast('Setup complete! Welcome to evaTorrent.', 'success');
      await checkAuth();
    } else {
      showToast(data.detail || 'Setup failed.', 'error');
    }
  } catch (err) {
    showToast('Failed to complete setup.', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Complete Setup & Sign In';
  }
}

async function requestLoginOTP() {
  const email = document.getElementById('login-email').value.trim();
  if (!email) {
    showToast('Please enter your authorized email.', 'error');
    return;
  }

  const btn = document.getElementById('btn-request-otp');
  btn.disabled = true;
  btn.textContent = 'Sending code...';

  try {
    const res = await fetch('/api/auth/otp/request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast(data.message || 'OTP sent! Check your inbox.', 'success');
      document.getElementById('otp-code-group').classList.remove('hidden');
      document.getElementById('btn-request-otp').classList.add('hidden');
      document.getElementById('btn-verify-otp').classList.remove('hidden');
      document.getElementById('login-otp').focus();
    } else {
      showToast(data.detail || 'Failed to send OTP code.', 'error');
    }
  } catch (err) {
    showToast('Failed to request OTP code.', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Send Login Code';
  }
}

async function handleOTPSubmit() {
  const email = document.getElementById('login-email').value.trim();
  const otp = document.getElementById('login-otp').value.trim();

  if (!email || !otp) {
    showToast('Please enter both email and 6-digit code.', 'error');
    return;
  }

  const btn = document.getElementById('btn-verify-otp');
  btn.disabled = true;
  btn.textContent = 'Verifying...';

  try {
    const res = await fetch('/api/auth/otp/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, otp }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast('Logged in successfully!', 'success');
      await checkAuth();
    } else {
      showToast(data.detail || 'Invalid verification code.', 'error');
    }
  } catch (err) {
    showToast('Verification failed.', 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = 'Verify & Sign In';
  }
}

function setupGoogleAuth(clientId) {
  const container = document.getElementById('google-btn-container');
  const disabledMsg = document.getElementById('google-disabled-msg');

  if (!clientId) {
    container.style.display = 'none';
    disabledMsg.classList.remove('hidden');
    return;
  }

  container.style.display = 'flex';
  disabledMsg.classList.add('hidden');

  if (window.google && window.google.accounts && window.google.accounts.id) {
    try {
      google.accounts.id.initialize({
        client_id: clientId,
        callback: handleGoogleCredentialResponse,
      });
      google.accounts.id.renderButton(container, {
        theme: 'filled_black',
        size: 'large',
        text: 'signin_with',
        shape: 'rectangular',
        width: 280,
      });
    } catch (e) {
      console.warn('Google Identity initialization error:', e);
    }
  }
}

async function handleGoogleCredentialResponse(response) {
  if (!response || !response.credential) return;

  try {
    const res = await fetch('/api/auth/google', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ credential: response.credential }),
    });
    const data = await res.json();
    if (res.ok) {
      showToast('Google Sign-In successful!', 'success');
      await checkAuth();
    } else {
      showToast(data.detail || 'Google authentication failed.', 'error');
    }
  } catch (err) {
    showToast('Failed to authenticate with Google.', 'error');
  }
}

async function logout() {
  try {
    await fetch('/api/auth/logout', { method: 'POST' });
    if (ws) ws.close();
    showToast('Signed out.', 'info');
    await checkAuth();
  } catch (err) {
    console.error('Logout error:', err);
  }
}

// Format utilities
function formatBytes(bytes, decimals = 2) {
  if (!bytes || bytes === 0) return '0 B';
  const k = 1024;
  const dm = decimals < 0 ? 0 : decimals;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

function formatSpeed(bytesPerSec) {
  return formatBytes(bytesPerSec) + '/s';
}

function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return '∞';
  if (seconds <= 0) return '0s';
  const hrs = Math.floor(seconds / 3600);
  const mins = Math.floor((seconds % 3600) / 60);
  const secs = seconds % 60;
  if (hrs > 0) return `${hrs}h ${mins}m`;
  if (mins > 0) return `${mins}m ${secs}s`;
  return `${secs}s`;
}

// Toast Notifications
function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  const toast = document.createElement('div');
  toast.className = `toast ${type}`;
  toast.textContent = message;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    toast.style.transform = 'translateX(100%)';
    toast.style.transition = 'all 0.3s ease';
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// WebSocket Connection
function connectWebSocket() {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const wsUrl = `${protocol}//${window.location.host}/ws`;

  ws = new WebSocket(wsUrl);

  ws.onopen = () => {
    console.log('[evaTorrent] WebSocket connected');
    if (reconnectTimer) clearTimeout(reconnectTimer);
  };

  ws.onmessage = (event) => {
    try {
      const msg = JSON.parse(event.data);
      if (msg.type === 'telemetry') {
        updateGlobalStats(msg.stats);
        updateTorrents(msg.torrents);
      }
    } catch (e) {
      console.error('WebSocket parse error:', e);
    }
  };

  ws.onclose = () => {
    console.log('[evaTorrent] WebSocket disconnected, reconnecting...');
    reconnectTimer = setTimeout(connectWebSocket, 2000);
  };

  ws.onerror = (err) => {
    console.error('WebSocket error:', err);
    ws.close();
  };
}

// Global Stats
function updateGlobalStats(stats) {
  if (!stats) return;
  document.getElementById('global-dl-speed').textContent = formatSpeed(stats.total_download_speed);
  document.getElementById('global-ul-speed').textContent = formatSpeed(stats.total_upload_speed);
  document.getElementById('global-active-torrents').textContent = stats.active_torrents;
}

// Update Torrents & Render List
function updateTorrents(newTorrents) {
  torrents = newTorrents;
  updateCounts();
  renderTorrentList();

  // If inspector is open on a torrent, refresh its values
  if (selectedTorrentHash) {
    const selected = torrents.find(t => t.info_hash === selectedTorrentHash);
    if (selected) {
      refreshInspectorData(selected);
    }
  }
}

function updateCounts() {
  document.getElementById('count-all').textContent = torrents.length;
  document.getElementById('count-dl').textContent = torrents.filter(t => t.status === 'downloading').length;
  document.getElementById('count-done').textContent = torrents.filter(t => t.status === 'completed').length;
  document.getElementById('count-paused').textContent = torrents.filter(t => t.status === 'paused').length;
  const errEl = document.getElementById('count-err');
  if (errEl) errEl.textContent = torrents.filter(t => t.status === 'error').length;
}

function renderTorrentList() {
  const listEl = document.getElementById('torrent-list');
  const emptyEl = document.getElementById('empty-state');

  const filtered = torrents.filter(t => {
    const matchesFilter = (currentFilter === 'all') || (t.status === currentFilter);
    const matchesSearch = !searchQuery || t.name.toLowerCase().includes(searchQuery.toLowerCase()) || t.info_hash.toLowerCase().includes(searchQuery.toLowerCase());
    return matchesFilter && matchesSearch;
  });

  if (filtered.length === 0) {
    listEl.innerHTML = '';
    emptyEl.style.display = 'flex';
    return;
  }

  emptyEl.style.display = 'none';

  // Build card elements efficiently
  listEl.innerHTML = filtered.map(t => `
    <div class="torrent-card" onclick="openInspector('${t.info_hash}')">
      <div class="card-header">
        <div class="card-title-group">
          <span class="status-badge ${t.status}">${t.status}</span>
          <div class="card-title" title="${t.name}">${t.name}</div>
        </div>
        <div class="card-actions" onclick="event.stopPropagation()">
          <button class="card-btn" title="Set Speed Limit" onclick="promptSpeedLimit('${t.info_hash}')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg>
          </button>
          ${t.status === 'downloading'
            ? `<button class="card-btn" title="Pause" onclick="pauseTorrent('${t.info_hash}')">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg>
               </button>`
            : `<button class="card-btn" title="Resume / Retry" onclick="resumeTorrent('${t.info_hash}')">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>
               </button>`
          }
          <button class="card-btn delete" title="Delete Torrent" onclick="deleteTorrent('${t.info_hash}')">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
          </button>
        </div>
      </div>

      <div class="progress-bar-container">
        <div class="progress-fill ${t.status === 'error' ? 'error' : ''}" style="width: ${t.progress}%"></div>
      </div>

      <div class="card-meta">
        <div class="meta-group">
          <div class="meta-item">Progress: <strong>${t.progress}%</strong></div>
          <div class="meta-item">Size: <strong>${formatBytes(t.downloaded)} / ${formatBytes(t.total_size)}</strong></div>
          <div class="meta-item">Peers: <strong>${t.peers_connected} / ${t.peers_total}</strong></div>
          ${t.download_limit ? `<div class="meta-item purple-text">Limit: <strong>${formatSpeed(t.download_limit)}</strong></div>` : ''}
        </div>
        <div class="meta-group">
          ${t.status === 'error' && t.error_message
            ? `<div class="meta-item" style="color: var(--accent-danger); font-weight: 600;">⚠️ ${t.error_message}</div>`
            : `
              <div class="meta-item">DL: <strong class="cyan-text">${formatSpeed(t.download_speed)}</strong></div>
              <div class="meta-item">UL: <strong class="purple-text">${formatSpeed(t.upload_speed)}</strong></div>
              <div class="meta-item">ETA: <strong>${formatDuration(t.eta)}</strong></div>
            `
          }
        </div>
      </div>
    </div>
  `).join('');
}

// Torrent Actions
async function pauseTorrent(hash) {
  try {
    const res = await fetch(`/api/torrents/${hash}/pause`, { method: 'POST' });
    if (res.ok) {
      showToast('Torrent paused', 'info');
    }
  } catch (e) {
    showToast('Failed to pause torrent', 'error');
  }
}

async function resumeTorrent(hash) {
  try {
    const res = await fetch(`/api/torrents/${hash}/resume`, { method: 'POST' });
    if (res.ok) {
      showToast('Torrent resumed', 'success');
    }
  } catch (e) {
    showToast('Failed to resume torrent', 'error');
  }
}

async function promptSpeedLimit(hash) {
  if (!hash) return;
  const current = torrents.find(t => t.info_hash === hash);
  const currentKb = current && current.download_limit ? Math.round(current.download_limit / 1024) : 0;
  const input = prompt(`Enter max download speed limit in KB/s (0 for unlimited):`, currentKb > 0 ? currentKb : '');
  if (input === null) return;

  const kb = parseInt(input.trim(), 10);
  const bytesLimit = (isNaN(kb) || kb <= 0) ? null : kb * 1024;

  try {
    const res = await fetch(`/api/torrents/${hash}/speed_limit`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ download_limit: bytesLimit }),
    });
    if (res.ok) {
      showToast(bytesLimit ? `Speed limit set to ${kb} KB/s` : 'Speed limit removed (Unlimited)', 'success');
    }
  } catch (e) {
    showToast('Failed to update speed limit', 'error');
  }
}

async function deleteTorrent(hash) {
  if (!confirm('Are you sure you want to remove this torrent?')) return;
  try {
    const res = await fetch(`/api/torrents/${hash}`, { method: 'DELETE' });
    if (res.ok) {
      showToast('Torrent removed', 'info');
      if (selectedTorrentHash === hash) {
        closeInspector();
      }
    }
  } catch (e) {
    showToast('Failed to delete torrent', 'error');
  }
}

// Inspector Drawer
function openInspector(hash) {
  selectedTorrentHash = hash;
  const t = torrents.find(item => item.info_hash === hash);
  if (!t) return;

  document.getElementById('inspector-overlay').classList.remove('hidden');
  document.getElementById('inspector-drawer').classList.remove('hidden');
  refreshInspectorData(t);
  switchInspectorTab(activeInspectorTab);
}

function closeInspector() {
  selectedTorrentHash = null;
  document.getElementById('inspector-overlay').classList.add('hidden');
  document.getElementById('inspector-drawer').classList.add('hidden');
}

function refreshInspectorData(t) {
  document.getElementById('insp-name').textContent = t.name;
  document.getElementById('insp-hash').textContent = t.info_hash;
  const badge = document.getElementById('insp-badge');
  badge.textContent = t.status.toUpperCase();
  badge.className = `inspector-badge status-badge ${t.status}`;

  document.getElementById('insp-size').textContent = formatBytes(t.total_size);
  document.getElementById('insp-downloaded').textContent = `${formatBytes(t.downloaded)} (${t.progress}%)`;
  document.getElementById('insp-dl-speed').textContent = formatSpeed(t.download_speed);
  document.getElementById('insp-ul-speed').textContent = formatSpeed(t.upload_speed);
  document.getElementById('insp-eta').textContent = formatDuration(t.eta);
  document.getElementById('insp-peers').textContent = `${t.peers_connected} connected (${t.peers_total} discovered in swarm)`;
  document.getElementById('insp-pieces').textContent = `${t.pieces_completed} / ${t.piece_count}`;
  document.getElementById('insp-piece-len').textContent = formatBytes(t.piece_length);

  const limitEl = document.getElementById('insp-speed-limit');
  if (limitEl) {
    limitEl.textContent = t.download_limit ? formatSpeed(t.download_limit) : 'Unlimited';
  }

  const errBox = document.getElementById('insp-error-box');
  const errMsg = document.getElementById('insp-error-msg');
  if (errBox && errMsg) {
    if (t.status === 'error' && t.error_message) {
      errMsg.textContent = t.error_message;
      errBox.classList.remove('hidden');
    } else {
      errBox.classList.add('hidden');
    }
  }

  // Trackers list
  const trackersEl = document.getElementById('insp-trackers');
  if (t.trackers && t.trackers.length > 0) {
    trackersEl.innerHTML = t.trackers.map(tr => `<li>${tr}</li>`).join('');
  } else {
    trackersEl.innerHTML = `<li>No trackers announced</li>`;
  }

  // Files list
  const filesEl = document.getElementById('files-list');
  if (t.files && t.files.length > 0) {
    filesEl.innerHTML = t.files.map(f => `
      <li>
        <span>${f.path}</span>
        <strong style="float: right;">${formatBytes(f.length)}</strong>
      </li>
    `).join('');
  } else {
    filesEl.innerHTML = `<li>${t.name} (${formatBytes(t.total_size)})</li>`;
  }

  if (activeInspectorTab === 'pieces') {
    fetchPiecesMap(t.info_hash);
  } else if (activeInspectorTab === 'peers') {
    fetchPeersList(t.info_hash);
  }
}

function switchInspectorTab(tabName) {
  activeInspectorTab = tabName;
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.textContent.toLowerCase().includes(tabName.slice(0, 4)));
  });
  document.querySelectorAll('.tab-content').forEach(content => {
    content.classList.toggle('active', content.id === `tab-${tabName}`);
  });

  if (selectedTorrentHash) {
    if (tabName === 'pieces') fetchPiecesMap(selectedTorrentHash);
    if (tabName === 'peers') fetchPeersList(selectedTorrentHash);
  }
}

// Fetch Piece Map Grid
async function fetchPiecesMap(hash) {
  try {
    const res = await fetch(`/api/torrents/${hash}/pieces`);
    if (!res.ok) return;
    const data = await res.json();

    document.getElementById('piece-stat-count').textContent = `${data.completed_indices.length} / ${data.total_pieces}`;
    const gridEl = document.getElementById('piece-grid');

    const completedSet = new Set(data.completed_indices);
    const ongoingSet = new Set(data.ongoing_indices);

    let html = '';
    for (let i = 0; i < data.total_pieces; i++) {
      let state = 'missing';
      if (completedSet.has(i)) state = 'completed';
      else if (ongoingSet.has(i)) state = 'ongoing';
      html += `<div class="piece-cell ${state}" title="Piece #${i}: ${state}"></div>`;
    }
    gridEl.innerHTML = html;
  } catch (e) {
    console.error('Failed to load pieces map:', e);
  }
}

// Fetch Peers List
async function fetchPeersList(hash) {
  try {
    const res = await fetch(`/api/torrents/${hash}/peers`);
    if (!res.ok) return;
    const data = await res.json();
    const tbody = document.getElementById('peers-table-body');

    if (!data.peers || data.peers.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">No peers connected</td></tr>`;
      return;
    }

    tbody.innerHTML = data.peers.map(p => `
      <tr>
        <td><strong>${p.ip}</strong>:${p.port}</td>
        <td>${p.connected ? '<span class="mint-text">Connected</span>' : 'Connecting'}</td>
        <td>${p.choked ? 'Choked' : '<span class="cyan-text">Unchoked</span>'}</td>
        <td>${formatSpeed(p.download_speed)}</td>
        <td>${formatBytes(p.bytes_downloaded)}</td>
      </tr>
    `).join('');
  } catch (e) {
    console.error('Failed to load peers:', e);
  }
}

// Add Modal Handling
function openAddModal() {
  document.getElementById('modal-add').classList.remove('hidden');
}

function closeAddModal() {
  document.getElementById('modal-add').classList.add('hidden');
  document.getElementById('magnet-input').value = '';
}

async function uploadFile(file) {
  const formData = new FormData();
  formData.append('file', file);

  try {
    const res = await fetch('/api/torrents/upload', {
      method: 'POST',
      body: formData,
    });
    const result = await res.json();
    if (res.ok) {
      showToast(`Added: ${result.name}`, 'success');
      closeAddModal();
    } else {
      showToast(result.detail || 'Upload failed', 'error');
    }
  } catch (e) {
    showToast('Failed to upload torrent', 'error');
  }
}

async function submitMagnet() {
  const input = document.getElementById('magnet-input');
  const magnetUri = input.value.trim();
  if (!magnetUri) return;

  try {
    const res = await fetch('/api/torrents/magnet', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ magnet: magnetUri }),
    });
    const result = await res.json();
    if (res.ok) {
      showToast(`Added: ${result.name}`, 'success');
      closeAddModal();
    } else {
      showToast(result.detail || 'Invalid magnet link', 'error');
    }
  } catch (e) {
    showToast('Failed to add magnet link', 'error');
  }
}

// Event Listeners
document.addEventListener('DOMContentLoaded', () => {
  checkAuth();

  // Add Torrent Button
  document.getElementById('btn-add-torrent').addEventListener('click', openAddModal);

  // Filter Tabs
  document.querySelectorAll('.filter-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.filter-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      currentFilter = tab.dataset.filter;
      renderTorrentList();
    });
  });

  // Search input
  document.getElementById('search-input').addEventListener('input', (e) => {
    searchQuery = e.target.value;
    renderTorrentList();
  });

  // Dropzone setup
  const dropZone = document.getElementById('drop-zone');
  const fileInput = document.getElementById('file-input');

  dropZone.addEventListener('click', () => fileInput.click());
  fileInput.addEventListener('change', (e) => {
    if (e.target.files.length > 0) uploadFile(e.target.files[0]);
  });

  dropZone.addEventListener('dragover', (e) => {
    e.preventDefault();
    dropZone.classList.add('dragover');
  });

  dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));

  dropZone.addEventListener('drop', (e) => {
    e.preventDefault();
    dropZone.classList.remove('dragover');
    if (e.dataTransfer.files.length > 0) {
      uploadFile(e.dataTransfer.files[0]);
    }
  });

  // Global window drag & drop for convenience
  window.addEventListener('dragover', (e) => e.preventDefault());
  window.addEventListener('drop', (e) => {
    e.preventDefault();
    if (e.dataTransfer.files.length > 0 && e.dataTransfer.files[0].name.endsWith('.torrent')) {
      uploadFile(e.dataTransfer.files[0]);
    }
  });
});
