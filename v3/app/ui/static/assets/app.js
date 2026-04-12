/* ── IABridge Dashboard — app.js ─────────────────────────────────── */

const POLL_MS = 2000;
const PAGE_SIZE = 50;
let histPage = 0;
let histTotal = 0;

// ── Navigation ───────────────────────────────────────────────────

document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.add('hidden'));
    const el = document.getElementById(`tab-${tab}`);
    el.classList.remove('hidden');
    // Trigger refresh pour l'onglet
    if (tab === 'history') loadHistory();
    if (tab === 'permissions') loadPermissions();
    if (tab === 'monitoring') loadMonitoring();
    if (tab === 'settings') loadSettings();
  });
});

// ── Killswitch + Panic ───────────────────────────────────────────

const killBtn = document.getElementById('killswitch-btn');
let lastKillClick = 0;

killBtn.addEventListener('click', async () => {
  const now = Date.now();
  const active = killBtn.classList.contains('active');

  if (active) {
    // Désactiver killswitch + panic
    await post('/api/killswitch', { enable: false });
    await post('/api/panic', { enable: false });
    refresh();
    return;
  }

  // Premier clic = killswitch. Double-clic rapide (< 800ms) = panic
  if (now - lastKillClick < 800) {
    // Double-clic → PANIC MODE
    if (confirm('⚠️ PANIC MODE ⚠️\n\nCeci va :\n• Bloquer toutes les commandes\n• Fermer le navigateur piloté\n• Tuer les processus enfants\n• Verrouiller le PC\n\nConfirmer ?')) {
      await post('/api/panic', { enable: true });
      refresh();
    }
    lastKillClick = 0;
    return;
  }

  // Premier clic → killswitch simple
  lastKillClick = now;
  if (!confirm('Activer le KILLSWITCH ?\nToutes les commandes seront bloquées.\n\n(Double-clic rapide = PANIC MODE)')) return;
  await post('/api/killswitch', { enable: true });
  refresh();
});

// ── Helpers ──────────────────────────────────────────────────────

const $ = id => document.getElementById(id);
const fmtDur = s => {
  if (s == null) return '—';
  s = Math.floor(s);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (h) return `${h}h${String(m).padStart(2,'0')}m`;
  if (m) return `${m}m${String(sec).padStart(2,'0')}s`;
  return `${sec}s`;
};
const fmtTs = ts => ts ? new Date(ts * 1000).toLocaleString('fr-FR') : '—';
const fmtMs = ms => ms < 1000 ? `${ms}ms` : `${(ms/1000).toFixed(1)}s`;

async function post(url, data) {
  return fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(data) }).then(r => r.json());
}

function closeModal() { $('modal-overlay').classList.add('hidden'); }

// ── Overview polling ─────────────────────────────────────────────

async function refresh() {
  try {
    const s = await fetch('/api/status').then(r => r.json());
    // Topbar
    $('conn-dot').className = `dot ${s.connected ? 'online' : 'offline'}`;
    $('conn-label').textContent = s.connected ? 'Connecté' : 'Déconnecté';
    $('conn-uptime').textContent = s.connected ? `Uptime : ${fmtDur(s.uptime)}` : (s.last_disconnect_reason || '—');
    $('kill-badge').classList.toggle('hidden', !s.killswitch);
    $('panic-badge').classList.toggle('hidden', !s.panic_mode);
    killBtn.classList.toggle('active', !!s.killswitch);
    // Overview cards
    const cv = $('stat-connected');
    cv.textContent = s.connected ? 'ONLINE' : 'OFFLINE';
    cv.style.color = s.connected ? 'var(--ok)' : 'var(--err)';
    $('stat-gateway').textContent = s.gateway_url || '—';
    $('stat-uptime').textContent = fmtDur(s.session?.uptime);
    $('stat-actions').textContent = s.session?.actions_count ?? 0;
    $('stat-ok').textContent = `${s.session?.ok ?? 0} ok`;
    $('stat-err').textContent = `${s.session?.error ?? 0} err`;
    $('stat-deny').textContent = `${s.session?.denied ?? 0} deny`;
    $('stat-reconnects').textContent = s.reconnect_attempts ?? 0;
    $('stat-last-dc').textContent = s.last_disconnect
      ? `dernière : ${fmtTs(s.last_disconnect)}`
      : 'aucune coupure';
    // Dernière action
    const lb = $('last-action-box');
    if (s.last_action) {
      const a = s.last_action;
      const cls = a.status === 'ok' ? 'ok' : a.status === 'denied' ? 'deny' : 'err';
      lb.innerHTML = `
        <div style="font-size:16px;font-weight:700">${a.action}</div>
        <div class="muted" style="margin-top:4px">
          <span class="pill ${cls}">${a.status}</span> · ${fmtMs(a.duration_ms)} · ${fmtTs(a.ts)}
        </div>`;
    } else {
      lb.innerHTML = '<span class="muted">Aucune</span>';
    }
    // Monitoring quick update
    $('mon-ws-reconnects').textContent = s.reconnect_attempts ?? 0;
  } catch (e) { console.warn('refresh err', e); }

  // Stats top actions
  try {
    const st = await fetch('/api/stats?days=7').then(r => r.json());
    const box = $('top-actions-box');
    if (st.top_actions?.length) {
      box.innerHTML = st.top_actions.map(a => `
        <div style="display:flex;justify-content:space-between;padding:5px 0;border-bottom:1px solid var(--border)">
          <span style="font-weight:600">${a.action}</span>
          <span class="muted" style="font-family:'JetBrains Mono',monospace;font-size:12px">${a.count}</span>
        </div>`).join('');
    } else {
      box.innerHTML = '<span class="muted">Aucune (7j)</span>';
    }
  } catch (e) {}
}

setInterval(refresh, POLL_MS);
refresh();

// ── History ──────────────────────────────────────────────────────

async function loadHistory() {
  const search = $('hist-search').value;
  const action = $('hist-action-filter').value;
  const status = $('hist-status-filter').value;
  const params = new URLSearchParams({
    limit: PAGE_SIZE, offset: histPage * PAGE_SIZE,
  });
  if (search) params.set('search', search);
  if (action) params.set('action', action);
  if (status) params.set('status', status);

  const d = await fetch(`/api/actions?${params}`).then(r => r.json());
  histTotal = d.total;
  const body = $('hist-body');
  if (!d.actions.length) {
    body.innerHTML = '<tr><td colspan="4" class="muted" style="text-align:center;padding:32px">Aucune action</td></tr>';
  } else {
    body.innerHTML = d.actions.map(a => {
      const cls = a.status === 'ok' ? 'ok' : a.status === 'denied' ? 'deny' : 'err';
      return `<tr data-id="${a.id}" onclick='showAction(${JSON.stringify(a).replace(/'/g,"&#39;")})'>
        <td>${a.action}</td>
        <td><span class="pill ${cls}">${a.status}</span></td>
        <td style="font-family:'JetBrains Mono',monospace">${fmtMs(a.duration_ms)}</td>
        <td>${fmtTs(a.ts)}</td>
      </tr>`;
    }).join('');
  }
  // Pagination
  const pages = Math.ceil(histTotal / PAGE_SIZE);
  const pag = $('hist-pagination');
  if (pages <= 1) { pag.innerHTML = ''; return; }
  let html = `<button class="btn" onclick="histPage=Math.max(0,histPage-1);loadHistory()" ${histPage===0?'disabled':''}>←</button>`;
  html += `<span class="current">${histPage+1} / ${pages}</span>`;
  html += `<button class="btn" onclick="histPage=Math.min(${pages-1},histPage+1);loadHistory()" ${histPage>=pages-1?'disabled':''}>→</button>`;
  html += `<span class="muted" style="margin-left:8px">${histTotal} total</span>`;
  pag.innerHTML = html;

  // Remplir le filtre actions (unique list)
  const sel = $('hist-action-filter');
  if (sel.options.length <= 1) {
    try {
      const cat = await fetch('/api/actions-catalog').then(r => r.json());
      cat.modules.forEach(m => {
        m.actions.forEach(a => {
          const o = document.createElement('option');
          o.value = a.name;
          o.textContent = a.name;
          sel.appendChild(o);
        });
      });
    } catch (e) {}
  }
}

window.showAction = function(a) {
  $('modal-title').textContent = `${a.action} #${a.id}`;
  let html = `<div style="margin-bottom:12px">
    <span class="pill ${a.status==='ok'?'ok':a.status==='denied'?'deny':'err'}">${a.status}</span>
    · ${fmtMs(a.duration_ms)} · ${fmtTs(a.ts)}
  </div>`;
  html += `<div class="card-title" style="margin-top:16px">Paramètres</div>`;
  html += `<pre>${JSON.stringify(a.params, null, 2)}</pre>`;
  if (a.result) {
    html += `<div class="card-title" style="margin-top:16px">Résultat</div>`;
    const r = typeof a.result === 'string' ? a.result : JSON.stringify(a.result, null, 2);
    html += `<pre>${r.length > 5000 ? r.slice(0,5000) + '\n…tronqué' : r}</pre>`;
  }
  if (a.error) {
    html += `<div class="card-title" style="margin-top:16px;color:var(--err)">Erreur</div>`;
    html += `<pre style="color:var(--err)">${a.error}</pre>`;
  }
  $('modal-body').innerHTML = html;
  $('modal-overlay').classList.remove('hidden');
};

// Debounce search
let histTimer;
$('hist-search').addEventListener('input', () => {
  clearTimeout(histTimer);
  histTimer = setTimeout(() => { histPage = 0; loadHistory(); }, 300);
});
$('hist-action-filter').addEventListener('change', () => { histPage = 0; loadHistory(); });
$('hist-status-filter').addEventListener('change', () => { histPage = 0; loadHistory(); });

// Export
$('hist-export-csv').addEventListener('click', () => {
  window.open('/api/export-actions?fmt=csv', '_blank');
});
$('hist-export-json').addEventListener('click', () => {
  window.open('/api/export-actions?fmt=json', '_blank');
});

// Purge
$('hist-clear').addEventListener('click', async () => {
  if (!confirm('Supprimer tout l\'historique ?')) return;
  await post('/api/clear-history', {});
  histPage = 0;
  loadHistory();
});

// ── Permissions ──────────────────────────────────────────────────

async function loadPermissions() {
  const [catalog, trust] = await Promise.all([
    fetch('/api/actions-catalog').then(r => r.json()),
    fetch('/api/trust').then(r => r.json()),
  ]);

  const container = $('perm-container');
  container.innerHTML = '';

  catalog.modules.forEach(mod => {
    const div = document.createElement('div');
    div.className = 'card perm-module';
    let html = `<div class="perm-module-title">${mod.module}</div>`;
    mod.actions.forEach(a => {
      const cur = trust.current[a.name] || 'allow';
      html += `<div class="perm-row">
        <span class="perm-name">${a.name}</span>
        <span class="perm-label">${a.label}</span>
        <span class="perm-risk ${a.risk}">${a.risk.toUpperCase()}</span>
        <div class="perm-toggle" data-action="${a.name}">
          <button class="${cur==='allow'?'sel-allow':''}" data-mode="allow" onclick="setTrust('${a.name}','allow',this)">ALLOW</button>
          <button class="${cur==='ask'?'sel-ask':''}" data-mode="ask" onclick="setTrust('${a.name}','ask',this)">ASK</button>
          <button class="${cur==='deny'?'sel-deny':''}" data-mode="deny" onclick="setTrust('${a.name}','deny',this)">DENY</button>
        </div>
      </div>`;
    });
    div.innerHTML = html;
    container.appendChild(div);
  });
}

window.setTrust = async function(action, mode, el) {
  await post('/api/trust', { action, mode });
  // Update visuel
  const toggle = el.parentElement;
  toggle.querySelectorAll('button').forEach(b => {
    b.className = '';
  });
  el.className = `sel-${mode}`;
};

// ── Monitoring ───────────────────────────────────────────────────

async function loadMonitoring() {
  try {
    const d = await fetch('/api/monitoring').then(r => r.json());
    if (!d.available) {
      $('mon-unavailable').classList.remove('hidden');
      $('mon-grid').classList.add('hidden');
      return;
    }
    $('mon-unavailable').classList.add('hidden');
    $('mon-grid').classList.remove('hidden');
    // CPU
    const cpu = Math.round(d.cpu.avg);
    $('mon-cpu').textContent = `${cpu}%`;
    $('mon-cpu-bar').style.width = `${cpu}%`;
    $('mon-cpu-bar').className = `meter-fill ${cpu > 80 ? 'red' : 'cyan'}`;
    // Memory
    $('mon-mem').textContent = `${d.memory.percent}%`;
    $('mon-mem-detail').textContent = `${d.memory.used_gb} / ${d.memory.total_gb} GB`;
    $('mon-mem-bar').style.width = `${d.memory.percent}%`;
    $('mon-mem-bar').className = `meter-fill ${d.memory.percent > 85 ? 'red' : 'cyan'}`;
    // Disk
    $('mon-disk').textContent = `${d.disk.percent}%`;
    $('mon-disk-detail').textContent = `${d.disk.used_gb} / ${d.disk.total_gb} GB`;
    $('mon-disk-bar').style.width = `${d.disk.percent}%`;
    $('mon-disk-bar').className = `meter-fill ${d.disk.percent > 90 ? 'red' : 'cyan'}`;
    // Network
    $('mon-net-sent').textContent = `${d.network.sent_mb} MB`;
    $('mon-net-recv').textContent = `${d.network.recv_mb} MB`;
  } catch (e) {
    $('mon-unavailable').classList.remove('hidden');
    $('mon-grid').classList.add('hidden');
  }
}

// Auto-refresh monitoring si l'onglet est actif
setInterval(() => {
  if (!$('tab-monitoring').classList.contains('hidden')) loadMonitoring();
}, 3000);

// ── Settings ─────────────────────────────────────────────────────

async function loadSettings() {
  try {
    const s = await fetch('/api/status').then(r => r.json());
    $('set-gateway').value = s.gateway_url || '';
    $('set-agent-name').value = '';
    $('set-token').value = '••••••••';
  } catch (e) {}
}
