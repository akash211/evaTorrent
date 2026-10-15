// evaTorrent v1.0.0 enhancements — keyed DOM, canvas piece map, sparkline,
// native modals, sequential/streaming, bulk controls, batch magnets, shortcuts.
// Loaded after app.js; overrides legacy prompt()/confirm() paths.
// Zero heavy deps: vanilla canvas + fetch only.

(function () {
  'use strict';

  // ---------- state (unique names to avoid colliding with app.js lets) ----------
  var evaSortMode = 'added';
  var evaDlHist = [];
  var evaUlHist = [];
  var evaHistMax = 60;
  var evaPendingDelete = null;
  var evaPendingSpeed = null;
  var evaPieceCache = { hash: null, total: 0, completed: null, ongoing: null };

  // ---------- helpers ----------
  function evaEl(id) { return document.getElementById(id); }
  function evaEsc(s) {
    if (typeof escapeHtml === 'function') return escapeHtml(s);
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[c];
    });
  }
  function evaFmtB(b) { return typeof formatBytes === 'function' ? formatBytes(b) : (b + ' B'); }
  function evaFmtS(s) { return typeof formatSpeed === 'function' ? formatSpeed(s) : (s + ' B/s'); }
  function evaFmtD(s) { return typeof formatDuration === 'function' ? formatDuration(s) : (s + 's'); }

  function evaSorted(list) {
    var arr = list.slice();
    if (evaSortMode === 'speed') arr.sort(function (a, b) { return (b.download_speed || 0) - (a.download_speed || 0); });
    else if (evaSortMode === 'progress') arr.sort(function (a, b) { return (b.progress || 0) - (a.progress || 0); });
    else if (evaSortMode === 'size') arr.sort(function (a, b) { return (b.total_size || 0) - (a.total_size || 0); });
    else if (evaSortMode === 'name') arr.sort(function (a, b) { return String(a.name || '').localeCompare(String(b.name || '')); });
    return arr;
  }

  window.setSortMode = function (mode) {
    evaSortMode = mode || 'added';
    if (typeof renderTorrentList === 'function') renderTorrentList();
  };

  // ---------- 1. Keyed DOM updates (preserve hover/focus/clicks) ----------
  function evaCardHtml(t) {
    var queued = (t.status === 'queued' && t.queue_position)
      ? '<span class="cat-tag" title="Waiting for a download slot">#' + t.queue_position + ' in queue</span>' : '';
    var saved = (t.source === 'db')
      ? '<span class="cat-tag" title="Restored from database after restart — press Resume to reactivate">💾 saved</span>' : '';
    var seq = t.sequential ? '<span class="cat-tag" title="Sequential stream-first order">▶ seq</span>' : '';
    var limit = t.download_limit ? '<div class="meta-item purple-text">Limit: <strong>' + evaFmtS(t.download_limit) + '</strong></div>' : '';
    var errOrSpeed = (t.status === 'error' && t.error_message)
      ? '<div class="meta-item" style="color: var(--accent-danger); font-weight: 600;">⚠️ ' + evaEsc(t.error_message) + '</div>'
      : '<div class="meta-item">DL: <strong class="cyan-text">' + evaFmtS(t.download_speed) + '</strong></div>'
        + '<div class="meta-item">UL: <strong class="purple-text">' + evaFmtS(t.upload_speed) + '</strong></div>'
        + '<div class="meta-item">ETA: <strong>' + evaFmtD(t.eta) + '</strong></div>';
    var actionBtn = t.status === 'downloading'
      ? '<button class="card-btn" title="Pause" onclick="pauseTorrent(\'' + t.info_hash + '\')">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect></svg></button>'
      : '<button class="card-btn" title="Resume / Retry" onclick="resumeTorrent(\'' + t.info_hash + '\')">'
        + '<svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg></button>';
    return ''
      + '<div class="card-header"><div class="card-title-group">'
      + '<span class="status-badge ' + t.status + '">' + t.status + '</span>' + queued + saved + seq
      + '<div class="card-title" title="' + evaEsc(t.name) + '">' + evaEsc(t.name) + '</div>'
      + '</div><div class="card-actions" onclick="event.stopPropagation()">'
      + '<button class="card-btn" title="Sequential stream-first mode" onclick="toggleSequential(\'' + t.info_hash + '\')">▶≡</button>'
      + '<button class="card-btn" title="Set Speed Limit" onclick="promptSpeedLimit(\'' + t.info_hash + '\')">'
      + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><polyline points="12 6 12 12 16 14"></polyline></svg></button>'
      + actionBtn
      + '<button class="card-btn delete" title="Delete Torrent" onclick="deleteTorrent(\'' + t.info_hash + '\')">'
      + '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg></button>'
      + '</div></div>'
      + '<div class="progress-bar-container"><div class="progress-fill ' + (t.status === 'error' ? 'error' : '') + '" style="width: ' + t.progress + '%"></div></div>'
      + '<div class="card-meta"><div class="meta-group">'
      + '<div class="meta-item">Progress: <strong>' + t.progress + '%</strong></div>'
      + '<div class="meta-item">Size: <strong>' + evaFmtB(t.downloaded) + ' / ' + evaFmtB(t.total_size) + '</strong></div>'
      + '<div class="meta-item">Peers: <strong>' + t.peers_connected + ' / ' + t.peers_total + '</strong></div>' + limit
      + '</div><div class="meta-group">' + errOrSpeed + '</div></div>';
  }

  window.renderTorrentList = function () {
    var listEl = evaEl('torrent-list');
    var emptyEl = evaEl('empty-state');
    if (!listEl) return;
    var all = (typeof torrents !== 'undefined' && torrents) ? torrents : [];
    var filtered = all.filter(function (t) {
      var mf = (typeof currentFilter === 'undefined' || currentFilter === 'all') || (t.status === currentFilter);
      var q = (typeof searchQuery !== 'undefined' && searchQuery) ? searchQuery.toLowerCase() : '';
      var ms = !q || String(t.name || '').toLowerCase().indexOf(q) !== -1
        || String(t.info_hash || '').toLowerCase().indexOf(q) !== -1;
      return mf && ms;
    });
    var ordered = evaSorted(filtered);
    if (!ordered.length) {
      listEl.innerHTML = '';
      if (emptyEl) emptyEl.style.display = 'flex';
      return;
    }
    if (emptyEl) emptyEl.style.display = 'none';
    // Keyed diff: reuse nodes by info_hash, update in place.
    var seen = {};
    ordered.forEach(function (t) {
      seen[t.info_hash] = true;
      var node = listEl.querySelector('[data-hash="' + t.info_hash + '"]');
      var html = evaCardHtml(t);
      if (!node) {
        node = document.createElement('div');
        node.className = 'torrent-card';
        node.setAttribute('data-hash', t.info_hash);
        node.addEventListener('click', (function (h) { return function () { openInspector(h); }; })(t.info_hash));
        node.innerHTML = html;
        listEl.appendChild(node);
      } else if (node.__evaHtml !== html) {
        // Preserve scroll/focus: only replace innerHTML when content changed.
        var active = document.activeElement;
        var hadFocus = active && node.contains(active);
        node.innerHTML = html;
        if (hadFocus && active && document.contains(active)) { try { active.focus({ preventScroll: true }); } catch (e) {} }
      }
      node.__evaHtml = html;
      // Reorder without recreation.
      if (listEl.lastElementChild !== node) listEl.appendChild(node);
    });
    // Remove stale cards.
    Array.prototype.slice.call(listEl.children).forEach(function (child) {
      var h = child.getAttribute && child.getAttribute('data-hash');
      if (h && !seen[h]) listEl.removeChild(child);
    });
  };

  // ---------- 2. 60fps canvas piece map ----------
  function evaDrawPieces(total, completedSet, ongoingSet) {
    var cv = evaEl('piece-canvas');
    if (!cv) return;
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    var w = cv.clientWidth || cv.parentElement.clientWidth || 600;
    var h = 120;
    if (cv.width !== Math.floor(w * dpr)) { cv.width = Math.floor(w * dpr); cv.style.width = '100%'; }
    if (cv.height !== Math.floor(h * dpr)) { cv.height = Math.floor(h * dpr); cv.style.height = h + 'px'; }
    var ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    if (!total) return;
    // Grid: square cells, ~4px min.
    var cols = Math.max(1, Math.floor(w / 5));
    var rows = Math.ceil(total / cols);
    var cw = w / cols;
    var ch = h / Math.max(1, rows);
    var s = Math.min(cw, ch, 12);
    for (var i = 0; i < total; i++) {
      var c = i % cols, r = Math.floor(i / cols);
      var x = c * (w / cols), y = r * (h / rows);
      if (completedSet.has(i)) ctx.fillStyle = '#00ff9d';
      else if (ongoingSet.has(i)) ctx.fillStyle = '#00f0ff';
      else ctx.fillStyle = 'rgba(255,255,255,0.12)';
      ctx.fillRect(x + 0.5, y + 0.5, Math.max(1, w / cols - 1), Math.max(1, h / rows - 1));
      void s;
    }
    cv.onmousemove = function (ev) {
      var rect = cv.getBoundingClientRect();
      var px = ev.clientX - rect.left;
      var col = Math.floor(px / (rect.width / cols));
      var rowH = rect.height / Math.max(1, rows);
      var row = Math.floor((ev.clientY - rect.top) / rowH);
      var idx = row * cols + col;
      var tip = evaEl('piece-tooltip');
      if (!tip) return;
      if (idx >= 0 && idx < total) {
        var st = completedSet.has(idx) ? 'completed' : (ongoingSet.has(idx) ? 'in-flight' : 'missing');
        tip.textContent = 'Piece #' + idx + ': ' + st;
        tip.classList.remove('hidden');
        tip.style.left = (ev.clientX - rect.left + 12) + 'px';
        tip.style.top = (ev.clientY - rect.top - 8) + 'px';
      } else tip.classList.add('hidden');
    };
    cv.onmouseleave = function () { var tip = evaEl('piece-tooltip'); if (tip) tip.classList.add('hidden'); };
  }

  window.fetchPiecesMap = async function (hash) {
    try {
      var res = await fetch('/api/torrents/' + hash + '/pieces');
      if (!res.ok) return;
      var data = await res.json();
      var cnt = evaEl('piece-stat-count');
      if (cnt) cnt.textContent = (data.completed_indices.length) + ' / ' + data.total_pieces;
      evaPieceCache = {
        hash: hash, total: data.total_pieces,
        completed: new Set(data.completed_indices), ongoing: new Set(data.ongoing_indices)
      };
      evaDrawPieces(data.total_pieces, evaPieceCache.completed, evaPieceCache.ongoing);
    } catch (e) { /* keep old canvas on error */ }
  };
  window.addEventListener('resize', function () {
    if (evaPieceCache.total) evaDrawPieces(evaPieceCache.total, evaPieceCache.completed, evaPieceCache.ongoing);
  });

  // ---------- 3. Sparkline + system pill ----------
  function evaDrawSpark() {
    var cv = evaEl('sparkline');
    if (!cv) return;
    var ctx = cv.getContext('2d');
    var w = cv.width, h = cv.height;
    ctx.clearRect(0, 0, w, h);
    var peak = Math.max.apply(null, evaDlHist.concat(evaUlHist).concat([0]));
    if (peak <= 0) {
      // Idle: flat line would look broken — draw a dashed baseline + label.
      ctx.strokeStyle = 'rgba(255,255,255,0.25)';
      ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(0, h - 4); ctx.lineTo(w, h - 4); ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = 'rgba(255,255,255,0.45)';
      ctx.font = '9px sans-serif';
      ctx.fillText('idle', 4, h - 8);
      return;
    }
    function line(arr, color) {
      if (arr.length < 2) return;
      var max = Math.max.apply(null, arr.concat([1]));
      ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
      arr.forEach(function (v, i) {
        var x = (i / (evaHistMax - 1)) * w;
        var y = h - 2 - (v / max) * (h - 4);
        if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
      });
      ctx.stroke();
    }
    line(evaDlHist, '#00f0ff');
    line(evaUlHist, '#a855f7');
  }
  var evaOrigStats = window.updateGlobalStats;
  window.updateGlobalStats = function (stats) {
    if (typeof evaOrigStats === 'function') { try { evaOrigStats(stats); } catch (e) {} }
    if (!stats) return;
    evaDlHist.push(stats.total_download_speed || 0);
    evaUlHist.push(stats.total_upload_speed || 0);
    if (evaDlHist.length > evaHistMax) evaDlHist.shift();
    if (evaUlHist.length > evaHistMax) evaUlHist.shift();
    evaDrawSpark();
  };
  async function evaPollHealth() {
    try {
      var res = await fetch('/api/system/health');
      if (!res.ok) return;
      var d = await res.json();
      var el = evaEl('sys-res-text');
      if (!el) return;
      var rss = (d.rss_mb != null) ? d.rss_mb.toFixed(0) + ' MB' : '-- MB';
      var disk = (d.free_disk_mb != null) ? (d.free_disk_mb / 1024).toFixed(1) + ' GB free' : '-- GB free';
      el.textContent = rss + ' • ' + disk;
    } catch (e) {}
  }
  setInterval(evaPollHealth, 10000);
  setTimeout(evaPollHealth, 2000);

  // ---------- 4. Native modals (replace prompt/confirm) ----------
  function evaShow(id) { var m = evaEl(id); if (m) m.classList.remove('hidden'); }
  function evaHide(id) { var m = evaEl(id); if (m) m.classList.add('hidden'); }
  window.closeDeleteModal = function () { evaHide('modal-delete'); evaPendingDelete = null; };
  window.closeSpeedModal = function () { evaHide('modal-speed'); evaPendingSpeed = null; };
  window.closePlayerModal = function () {
    evaHide('modal-player');
    var v = evaEl('stream-player');
    if (v) { try { v.pause(); } catch (e) {} v.removeAttribute('src'); v.load(); }
  };

  window.deleteTorrent = async function (hash) {
    evaPendingDelete = hash;
    var t = (typeof torrents !== 'undefined' ? torrents : []).find(function (x) { return x.info_hash === hash; });
    var nm = evaEl('delete-modal-name');
    if (nm) nm.textContent = t ? t.name : hash;
    var chk = evaEl('delete-files-check');
    if (chk) chk.checked = false;
    evaShow('modal-delete');
  };
  window.confirmDeleteTorrent = async function () {
    var hash = evaPendingDelete;
    if (!hash) return;
    var delFiles = evaEl('delete-files-check') && evaEl('delete-files-check').checked;
    try {
      var url = '/api/torrents/' + hash + (delFiles ? '?delete_files=true' : '');
      var res = await fetch(url, { method: 'DELETE' });
      if (res.ok) {
        if (typeof showToast === 'function') showToast('Torrent removed', 'info');
        if (typeof selectedTorrentHash !== 'undefined' && selectedTorrentHash === hash
          && typeof closeInspector === 'function') closeInspector();
      } else {
        var data = await res.json().catch(function () { return {}; });
        if (typeof showToast === 'function') showToast(data.detail || 'Failed to delete torrent', 'error');
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('Failed to delete torrent', 'error');
    }
    window.closeDeleteModal();
  };

  window.promptSpeedLimit = function (hash) {
    if (!hash) return;
    evaPendingSpeed = hash;
    var t = (typeof torrents !== 'undefined' ? torrents : []).find(function (x) { return x.info_hash === hash; });
    var nm = evaEl('speed-modal-name');
    if (nm) nm.textContent = t ? t.name : hash;
    var inp = evaEl('speed-limit-input');
    if (inp) inp.value = (t && t.download_limit) ? Math.round(t.download_limit / 1024) : '';
    evaShow('modal-speed');
    if (inp) setTimeout(function () { inp.focus(); }, 50);
  };
  window.setSpeedPreset = function (kb) {
    var inp = evaEl('speed-limit-input');
    if (inp) inp.value = kb ? String(kb) : '';
  };
  window.confirmSpeedLimit = async function () {
    var hash = evaPendingSpeed;
    if (!hash) return;
    var inp = evaEl('speed-limit-input');
    var kb = inp ? parseInt((inp.value || '').trim(), 10) : NaN;
    var bytesLimit = (isNaN(kb) || kb <= 0) ? null : kb * 1024;
    try {
      var res = await fetch('/api/torrents/' + hash + '/speed_limit', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ download_limit: bytesLimit })
      });
      if (res.ok && typeof showToast === 'function') {
        showToast(bytesLimit ? ('Speed limit set to ' + kb + ' KB/s') : 'Speed limit removed (Unlimited)', 'success');
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('Failed to update speed limit', 'error');
    }
    window.closeSpeedModal();
  };

  // ---------- 5. Sequential + streaming ----------
  window.toggleSequential = async function (hash) {
    var t = (typeof torrents !== 'undefined' ? torrents : []).find(function (x) { return x.info_hash === hash; });
    var next = !(t && t.sequential);
    try {
      var res = await fetch('/api/torrents/' + hash + '/sequential', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sequential: next })
      });
      if (res.ok && typeof showToast === 'function') showToast(next ? 'Sequential stream-first mode ON' : 'Sequential mode OFF (rarest-first)', 'success');
      if (typeof loadTorrents === 'function') loadTorrents();
    } catch (e) {
      if (typeof showToast === 'function') showToast('Failed to toggle sequential mode', 'error');
    }
  };
  window.openStreamModal = function (hash, fileIdx, fileName) {
    var v = evaEl('stream-player');
    var title = evaEl('player-title');
    if (title) title.textContent = fileName || ('File #' + fileIdx);
    if (v) {
      v.src = '/api/torrents/' + hash + '/stream/' + fileIdx;
      try { v.load(); v.play().catch(function () {}); } catch (e) {}
    }
    evaShow('modal-player');
  };
  window.downloadFile = function (hash, fileIdx) {
    var a = document.createElement('a');
    a.href = '/api/torrents/' + hash + '/stream/' + fileIdx + '?download=1';
    a.download = '';
    document.body.appendChild(a); a.click(); a.remove();
  };

  // Inject Stream/Save buttons into files list after each render.
  function evaAugmentFiles() {
    var list = evaEl('files-list');
    if (!list || typeof selectedTorrentHash === 'undefined' || !selectedTorrentHash) return;
    var hash = selectedTorrentHash;
    Array.prototype.forEach.call(list.querySelectorAll('.file-row'), function (row, i) {
      if (row.querySelector('[data-eva-stream]')) return;
      var btns = document.createElement('span');
      btns.style.cssText = 'display:inline-flex;gap:6px;margin-left:8px';
      var name = '';
      try {
        var label = row.querySelector('.file-path');
        name = label ? label.textContent : ('file ' + i);
      } catch (e) {}
      var isMedia = /\.(mp4|webm|mkv|mp3|m4a|ogg|wav)$/i.test(name);
      if (isMedia) {
        var b1 = document.createElement('button');
        b1.className = 'btn btn-secondary btn-sm'; b1.textContent = '▶ Stream';
        b1.setAttribute('data-eva-stream', '1');
        b1.onclick = (function (h, idx, nm) { return function (ev) { ev.stopPropagation(); openStreamModal(h, idx, nm); }; })(hash, i, name);
        btns.appendChild(b1);
      }
      var b2 = document.createElement('button');
      b2.className = 'btn btn-secondary btn-sm'; b2.textContent = '⬇ Save';
      b2.onclick = (function (h, idx) { return function (ev) { ev.stopPropagation(); downloadFile(h, idx); }; })(hash, i);
      btns.appendChild(b2);
      var sizeEl = row.querySelector('.file-size');
      if (sizeEl) sizeEl.appendChild(btns); else row.appendChild(btns);
    });
  }
  setInterval(function () {
    var tab = evaEl('tab-files');
    if (tab && tab.classList.contains('active')) evaAugmentFiles();
  }, 800);

  // Manual subtitle mode: search by title alone works (backend needs only `q`);
  // saving still needs a downloaded video, so without one we copy the link.
  var evaOrigDlSub = window.downloadSubtitleResult;
  window.downloadSubtitleResult = async function (i) {
    var r = (typeof subtitleResults !== 'undefined' ? subtitleResults : [])[i];
    var sel = evaEl('subtitle-video-select');
    var video = sel ? sel.value : '';
    if (r && !video) {
      var link = r.download_url || r.detail_url || '';
      try {
        if (navigator.clipboard && link) await navigator.clipboard.writeText(link);
        if (typeof showToast === 'function') {
          showToast(link ? 'No video selected — provider link copied. Select a video to save .srt directly.' : 'Select a video to save the .srt', 'info');
        }
      } catch (e) {
        if (typeof showToast === 'function') showToast('Select a video first to save the .srt', 'error');
      }
      return;
    }
    if (typeof evaOrigDlSub === 'function') return evaOrigDlSub(i);
  };

  // ---------- 6. Bulk + batch ----------
  window.bulkAction = async function (action) {
    try {
      var res = await fetch('/api/torrents/bulk', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action })
      });
      if (res.ok) {
        if (typeof showToast === 'function') showToast('Bulk action applied: ' + action, 'success');
        if (typeof loadTorrents === 'function') loadTorrents();
      } else if (typeof showToast === 'function') showToast('Bulk action failed', 'error');
    } catch (e) {
      if (typeof showToast === 'function') showToast('Bulk action failed', 'error');
    }
  };
  // Include sequential flag on single-magnet submit.
  var evaOrigSubmit = window.submitMagnet;
  window.submitMagnet = async function () {
    var input = evaEl('magnet-input');
    var seqBox = evaEl('add-sequential');
    var sequential = !!(seqBox && seqBox.checked);
    var val = input ? input.value.trim() : '';
    if (!val) return;
    // If backend supports sequential on /magnet, pass it; else add then toggle.
    try {
      var res = await fetch('/api/torrents/magnet', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ magnet: val, sequential: sequential })
      });
      var data = await res.json().catch(function () { return {}; });
      if (res.ok) {
        if (typeof showToast === 'function') showToast('Torrent added', 'success');
        if (input) input.value = '';
        if (typeof closeAddModal === 'function') closeAddModal();
        if (typeof loadTorrents === 'function') loadTorrents();
        // If server ignored sequential flag but returned hash, set it explicitly.
        if (sequential && data && data.info_hash) {
          try {
            await fetch('/api/torrents/' + data.info_hash + '/sequential', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ sequential: true })
            });
          } catch (e) {}
        }
        return;
      }
      // Fallback to legacy handler for error toasts/compat.
      if (typeof evaOrigSubmit === 'function' && evaOrigSubmit !== window.submitMagnet) {
        // legacy already tried; show server detail
        if (typeof showToast === 'function') showToast((data && data.detail) || 'Failed to add torrent', 'error');
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('Failed to add torrent', 'error');
    }
  };
  window.submitBatchMagnets = async function () {
    var ta = evaEl('batch-magnet-input');
    if (!ta || !ta.value.trim()) {
      if (typeof showToast === 'function') showToast('Paste at least one magnet link', 'error');
      return;
    }
    var lines = ta.value.split('\n').map(function (s) { return s.trim(); }).filter(Boolean);
    var seqBox = evaEl('add-sequential');
    var sequential = !!(seqBox && seqBox.checked);
    try {
      var res = await fetch('/api/torrents/batch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ items: lines, sequential: sequential })
      });
      if (res.ok) {
        if (typeof showToast === 'function') showToast('Batch queued: ' + lines.length + ' item(s)', 'success');
        ta.value = '';
        if (typeof closeAddModal === 'function') closeAddModal();
        if (typeof loadTorrents === 'function') loadTorrents();
      } else if (typeof showToast === 'function') showToast('Batch add failed', 'error');
    } catch (e) {
      if (typeof showToast === 'function') showToast('Batch add failed', 'error');
    }
  };

  // ---------- 7. Keyboard shortcuts ----------
  document.addEventListener('keydown', function (ev) {
    var tag = (ev.target && ev.target.tagName) ? ev.target.tagName.toLowerCase() : '';
    var typing = tag === 'input' || tag === 'textarea' || tag === 'select' || (ev.target && ev.target.isContentEditable);
    if (ev.key === 'Escape') {
      ['modal-add', 'modal-delete', 'modal-speed', 'modal-player'].forEach(function (id) {
        var m = evaEl(id); if (m) m.classList.add('hidden');
      });
      evaPendingDelete = null; evaPendingSpeed = null;
      return;
    }
    if (typing) return;
    if (ev.key === '/') { ev.preventDefault(); var s = evaEl('search-input'); if (s) s.focus(); }
    else if (ev.key === 'n') { if (typeof openAddModal === 'function') openAddModal(); }
    else if (ev.key === 's') { var sp = evaEl('speed-profile-select'); if (sp) sp.focus(); }
    else if (ev.key >= '1' && ev.key <= '5') {
      var views = ['live', 'search', 'discover', 'subtitles', 'analytics'];
      var v = views[parseInt(ev.key, 10) - 1];
      if (v && typeof switchMainView === 'function') switchMainView(v);
    }
  });

  // Re-draw canvas when pieces tab becomes visible (clientWidth was 0 while hidden).
  var evaOrigTab = window.switchInspectorTab;
  window.switchInspectorTab = function (tabName) {
    if (typeof evaOrigTab === 'function') evaOrigTab(tabName);
    if (tabName === 'pieces' && evaPieceCache.total) {
      setTimeout(function () {
        evaDrawPieces(evaPieceCache.total, evaPieceCache.completed, evaPieceCache.ongoing);
      }, 30);
    }
    if (tabName === 'files') setTimeout(evaAugmentFiles, 100);
  };
})();
