/* ── IABridge Dashboard — app.js ─────────────────────────────────── */

const POLL_INTERVAL_MS = 2000;

// Navigation onglets
document.querySelectorAll('.nav-item').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-item').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const tab = btn.dataset.tab;
    document.querySelectorAll('.tab').forEach(t => t.classList.add('hidden'));
    document.getElementById(`tab-${tab}`).classList.remove('hidden');
  });
});

// Killswitch
const killBtn = document.getElementById('killswitch-btn');
killBtn.addEventListener('click', async () => {
  const currentlyActive = killBtn.classList.contains('active');
  const nextState = !currentlyActive;
  if (nextState) {
    if (!confirm('Activer le KILLSWITCH ? Toutes les commandes seront bloquées.')) return;
  }
  try {
    const r = await fetch('/api/killswitch', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enable: nextState })
    });
    await r.json();
    await refresh();
  } catch (e) {
    console.error('killswitch error', e);
  }
});

// Helpers format
const fmtDuration = (sec) => {
  if (sec == null) return '—';
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (h > 0) return `${h}h${String(m).padStart(2, '0')}m`;
  if (m > 0) return `${m}m${String(s).padStart(2, '0')}s`;
  return `${s}s`;
};

const fmtTs = (ts) => {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('fr-FR');
};

// Polling principal
async function refresh() {
  try {
    const r = await fetch('/api/status');
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();

    // Topbar
    const dot = document.getElementById('conn-dot');
    const label = document.getElementById('conn-label');
    const uptime = document.getElementById('conn-uptime');
    if (s.connected) {
      dot.className = 'dot online';
      label.textContent = 'Connecté';
      uptime.textContent = `Uptime : ${fmtDuration(s.uptime)}`;
    } else {
      dot.className = 'dot offline';
      label.textContent = 'Déconnecté';
      uptime.textContent = s.last_disconnect_reason || '—';
    }

    // Badges killswitch/panic
    document.getElementById('kill-badge').classList.toggle('hidden', !s.killswitch);
    document.getElementById('panic-badge').classList.toggle('hidden', !s.panic_mode);
    killBtn.classList.toggle('active', !!s.killswitch);

    // Overview stats cards
    document.getElementById('stat-connected').textContent = s.connected ? 'ONLINE' : 'OFFLINE';
    document.getElementById('stat-connected').style.color = s.connected ? 'var(--ok)' : 'var(--err)';
    document.getElementById('stat-gateway').textContent = s.gateway_url || '—';
    document.getElementById('stat-uptime').textContent = fmtDuration(s.session?.uptime);
    document.getElementById('stat-actions').textContent = s.session?.actions_count ?? 0;
    document.getElementById('stat-ok').textContent = `${s.session?.ok ?? 0} ok`;
    document.getElementById('stat-err').textContent = `${s.session?.error ?? 0} err`;
    document.getElementById('stat-deny').textContent = `${s.session?.denied ?? 0} deny`;
    document.getElementById('stat-reconnects').textContent = s.reconnect_attempts ?? 0;
    document.getElementById('stat-last-dc').textContent = s.last_disconnect
      ? `dernière : ${fmtTs(s.last_disconnect)}`
      : 'aucune coupure';

    // Dernière action
    const lastBox = document.getElementById('last-action-box');
    if (s.last_action) {
      const a = s.last_action;
      lastBox.innerHTML = `
        <div style="font-size:18px;font-weight:600;color:var(--text)">${a.action}</div>
        <div class="muted" style="margin-top:4px">
          <span class="pill ${a.status === 'ok' ? 'ok' : (a.status === 'denied' ? 'deny' : 'err')}">${a.status}</span>
          · ${a.duration_ms} ms · ${fmtTs(a.ts)}
        </div>`;
    } else {
      lastBox.textContent = 'Aucune action encore';
      lastBox.className = 'muted';
    }
  } catch (e) {
    console.warn('refresh status failed', e);
  }

  // Stats 7 jours
  try {
    const r = await fetch('/api/stats?days=7');
    const stats = await r.json();
    const box = document.getElementById('top-actions-box');
    if (stats.top_actions && stats.top_actions.length) {
      box.innerHTML = stats.top_actions.map(a => `
        <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">
          <span style="color:var(--text)">${a.action}</span>
          <span class="muted">${a.count}</span>
        </div>`).join('');
    } else {
      box.textContent = 'Aucune action sur les 7 derniers jours';
    }
  } catch (e) {
    console.warn('stats failed', e);
  }
}

refresh();
setInterval(refresh, POLL_INTERVAL_MS);
