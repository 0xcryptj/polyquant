/**
 * PolyQuant Terminal — Bloomberg-style, viewport-fitted
 */

const API_BASE = '';

/** Escape for safe use in HTML text and attributes to prevent XSS. */
function escapeHtml(str) {
  if (str == null) return '';
  const s = String(str);
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Return url if it is a safe https link; otherwise return empty string. */
function safeHref(url) {
  if (url == null || typeof url !== 'string') return '';
  const t = url.trim();
  return t.startsWith('https://') ? t : '';
}

// ─── Screen Size Detection ────────────────────────────────────────────────────

function updateViewportVars() {
  const vh = window.innerHeight;
  const vw = window.innerWidth;
  document.documentElement.style.setProperty('--viewport-height', `${vh}px`);
  document.documentElement.style.setProperty('--viewport-width', `${vw}px`);
  document.body.dataset.viewport = vw >= 1600 ? 'large' : vw >= 1200 ? 'medium' : 'compact';
}

window.addEventListener('resize', updateViewportVars);
window.addEventListener('load', updateViewportVars);
updateViewportVars();

async function fetchApi(path) {
  const r = await fetch(`${API_BASE}/api${path}`);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

function fmtCurrency(v) {
  if (v == null || isNaN(v)) return '—';
  const n = Number(v);
  if (n >= 0) return `+$${n.toFixed(2)}`;
  return `-$${Math.abs(n).toFixed(2)}`;
}

function fmtPct(v) {
  if (v == null || isNaN(v)) return '—';
  const sign = v >= 0 ? '+' : '';
  return `${sign}${Number(v).toFixed(1)}%`;
}

function dirDisplay(d) {
  return (d || '').toUpperCase() === 'YES' ? 'UP' : 'DOWN';
}

function _marketTypeLabel(marketType) {
  const t = (marketType || '').toLowerCase();
  if (t === '5min') return 'U/D';
  if (t === 'event' || t === 'macro') return 'Y/N';
  if (t === 'price') return 'Price';
  return '?';
}

// ─── Status ──────────────────────────────────────────────────────────────────

async function refreshStatus() {
  try {
    const [s, summary] = await Promise.all([
      fetchApi('/status'),
      fetchApi('/markets-summary').catch(() => null),
    ]);
    const total = s.total_equity ?? (s.balance + (s.locked_in_positions ?? 0));
    const pnl = s.total_pnl ?? (total - (s.starting_balance ?? 0));
    document.getElementById('stat-bankroll').textContent = fmtCurrency(total);
    const pnlEl = document.getElementById('stat-profit');
    pnlEl.textContent = fmtCurrency(pnl);
    pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'profit' : 'loss');
    document.getElementById('stat-winrate').textContent = (s.win_rate * 100).toFixed(0) + '%';

    document.getElementById('bankroll-available').textContent = fmtCurrency(s.balance);
    document.getElementById('bankroll-locked').textContent = fmtCurrency(s.locked_in_positions ?? 0);
    document.getElementById('bankroll-total').textContent = fmtCurrency(total);
    document.getElementById('bankroll-baseline').textContent = fmtCurrency(s.starting_balance);
    const profitEl = document.getElementById('bankroll-profit');
    profitEl.textContent = fmtCurrency(pnl);
    profitEl.className = 'card-value ' + (pnl >= 0 ? 'profit' : 'loss');
    document.getElementById('bankroll-return').textContent = fmtPct(s.return_pct ?? 0);

    const noticeEl = document.getElementById('markets-notice');
    if (summary?.notice && noticeEl) {
      noticeEl.textContent = summary.notice;
      noticeEl.className = 'markets-notice ' + (summary.has_5min ? '' : 'warning');
    } else if (noticeEl) noticeEl.textContent = '';

    const badge = document.getElementById('mode-badge');
    badge.textContent = s.paper_trading !== false ? 'Paper' : 'Live';
    badge.className = 'mode-badge' + (s.paper_trading === false ? ' live' : '');

    return s;
  } catch (e) {
    console.error('Status fetch failed:', e);
    return null;
  }
}

// ─── Positions ────────────────────────────────────────────────────────────────

function truncateQuestion(q, maxLen = 65) {
  const s = (q || 'Unknown').trim();
  if (s.length <= maxLen) return s;
  return s.slice(0, maxLen) + '…';
}

async function refreshPositions() {
  try {
    const rows = await fetchApi('/positions');
    const tbody = document.getElementById('positions-body');
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="10" class="empty">No open positions</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(p => {
      const q = escapeHtml(truncateQuestion(p.question, 65));
      const dir = dirDisplay(p.direction);
      const link = safeHref(p.polymarket_url) ? `<a href="${escapeHtml(safeHref(p.polymarket_url))}" target="_blank" rel="noopener" class="pm-link">View ↗</a>` : '';
      const shares = Number(p.shares ?? 0).toFixed(2);
      const size = '$' + Number(p.size_usdc || 0).toFixed(2);
      const avg = Number(p.entry_price || 0).toFixed(3);
      const current = p.current_value != null ? '$' + Number(p.current_value).toFixed(2) : '—';
      const toWin = '$' + Number(p.to_win ?? 0).toFixed(2);
      const tok = (p.token_id || '').toString();
      const rawType = (p.market_type || '').toLowerCase();
      const mtype = tok.startsWith('SIM-') ? 'sim' : (rawType || 'unknown');
      const typeLabel = tok.startsWith('SIM-') ? 'SIM' : _marketTypeLabel(p.market_type);
      const typeBadge = `<span class="type-badge type-${mtype}" title="${escapeHtml(typeLabel)}">${escapeHtml(typeLabel)}</span>`;
      return `
        <tr>
          <td>#${p.id}</td>
          <td class="dir-${dir.toLowerCase()}">${dir}</td>
          <td>${typeBadge}</td>
          <td title="token: ${escapeHtml(tok)} — ${escapeHtml(p.question || '')}">${q}</td>
          <td>${shares}</td>
          <td>${size}</td>
          <td>${avg}</td>
          <td>${current}</td>
          <td>${toWin}</td>
          <td>${link}</td>
        </tr>
      `;
    }).join('');
  } catch (e) {
    console.error('Positions fetch failed:', e);
  }
}

// ─── Trades ───────────────────────────────────────────────────────────────────

async function refreshTrades() {
  try {
    const rows = await fetchApi('/trades?limit=30');
    const tbody = document.getElementById('trades-body');
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="11" class="empty">No trades yet</td></tr>';
      return;
    }
    tbody.innerHTML = rows.map(t => {
      const q = escapeHtml(truncateQuestion(t.question, 65));
      const dir = dirDisplay(t.direction);
      const isLost = (t.status || '').toLowerCase() === 'lost';
      const rawPnl = t.pnl != null ? t.pnl : 0;
      const pnl = isLost && rawPnl >= 0 ? -Math.abs(t.size_usdc || 0) : rawPnl;
      const status = isLost ? 'status-lost' : 'status-won';
      const link = safeHref(t.polymarket_url) ? `<a href="${escapeHtml(safeHref(t.polymarket_url))}" target="_blank" rel="noopener" class="pm-link">View ↗</a>` : '';
      const shares = Number(t.shares ?? 0).toFixed(2);
      const size = '$' + Number(t.size_usdc || 0).toFixed(2);
      const avg = Number(t.entry_price || 0).toFixed(3);
      const exit = t.exit_price != null ? Number(t.exit_price).toFixed(3) : '—';
      const toWin = '$' + Number(t.to_win ?? 0).toFixed(2);
      const tok = (t.token_id || '').toString();
      const rawType = (t.market_type || '').toLowerCase();
      const mtype = tok.startsWith('SIM-') ? 'sim' : (rawType || 'unknown');
      const typeLabel = tok.startsWith('SIM-') ? 'SIM' : _marketTypeLabel(t.market_type);
      const typeBadge = `<span class="type-badge type-${mtype}" title="${escapeHtml(typeLabel)}">${escapeHtml(typeLabel)}</span>`;
      return `
        <tr>
          <td>#${t.id}</td>
          <td class="dir-${dir.toLowerCase()}">${dir}</td>
          <td>${typeBadge}</td>
          <td title="token: ${escapeHtml(tok)} — ${escapeHtml(t.question || '')}">${q}</td>
          <td>${shares}</td>
          <td>${size}</td>
          <td class="${status}">${fmtCurrency(pnl)}</td>
          <td>${avg}</td>
          <td>${exit}</td>
          <td>${toWin}</td>
          <td>${link}</td>
        </tr>
      `;
    }).join('');
  } catch (e) {
    console.error('Trades fetch failed:', e);
  }
}

// ─── PnL Chart ────────────────────────────────────────────────────────────────

let pnlChart = null;

async function refreshChart() {
  try {
    const series = await fetchApi('/pnl-history');
    const labels = series.map((_, i) => i);
    const data = series.map(s => s.cumulative);

    const ctx = document.getElementById('pnl-chart').getContext('2d');
    if (pnlChart) pnlChart.destroy();
    const grad = ctx.createLinearGradient(0, 0, 0, 100);
    grad.addColorStop(0, 'rgba(34, 197, 94, 0.25)');
    grad.addColorStop(0.5, 'rgba(245, 158, 11, 0.08)');
    grad.addColorStop(1, 'rgba(239, 68, 68, 0.05)');

    pnlChart = new Chart(ctx, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'Bankroll',
          data,
          borderColor: '#f59e0b',
          backgroundColor: grad,
          fill: true,
          tension: 0.35,
          borderWidth: 2.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          pointHoverBackgroundColor: '#f59e0b',
          pointHoverBorderColor: '#fff',
          pointHoverBorderWidth: 1,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 600 },
        transitions: { active: { animation: { duration: 300 } } },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: 'rgba(15, 19, 24, 0.95)',
            titleFont: { size: 11 },
            bodyFont: { size: 11 },
            callbacks: {
              label: (c) => `$${Number(c.raw).toFixed(2)}`
            },
          },
        },
        scales: {
          x: {
            display: true,
            ticks: { maxTicksLimit: 8, font: { size: 9 }, color: '#6b7280' },
            grid: { color: 'rgba(30, 37, 45, 0.6)' },
          },
          y: {
            display: true,
            beginAtZero: false,
            ticks: { font: { size: 9 }, color: '#6b7280' },
            grid: { color: 'rgba(30, 37, 45, 0.6)' },
          },
        },
      },
    });
  } catch (e) {
    console.error('Chart fetch failed:', e);
  }
}

// ─── Trade Grid (commit-map style) ────────────────────────────────────────────

async function refreshTradeGrid() {
  try {
    const rows = await fetchApi('/trade-grid');
    const grid = document.getElementById('trade-grid');
    if (!grid) return;
    if (!rows.length) {
      grid.innerHTML = '<span class="grid-empty">No trades yet</span>';
      return;
    }
    grid.innerHTML = rows.map(t => {
      const shares = t.shares ?? (t.size_usdc && t.entry_price ? t.size_usdc / t.entry_price : 0);
      const cls = (t.status || '').toLowerCase() === 'won' ? 'win' : 'loss';
      const rawPnl = t.pnl != null ? t.pnl : 0;
      const pnl = cls === 'loss' && rawPnl >= 0 ? -Math.abs(t.size_usdc || 0) : rawPnl;
      const tip = `#${t.id} ${pnl >= 0 ? '+' : '-'}$${Math.abs(Number(pnl)).toFixed(2)}`;
      return `<span class="trade-grid-cell ${cls}" title="${tip}"></span>`;
    }).join('');
  } catch (e) {
    console.error('Trade grid fetch failed:', e);
  }
}

// ─── Live BTC Chart ──────────────────────────────────────────────────────────

let btcChart = null;

async function refreshBtcChart() {
  try {
    const candles = await fetchApi('/btc-chart?limit=120');
    if (!candles || !candles.length) {
      if (btcChart) { btcChart.destroy(); btcChart = null; }
      return;
    }
    const labels = candles.map(c => new Date(c.t).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
    const data = candles.map(c => c.c);

    const ctx = document.getElementById('btc-chart');
    if (!ctx) return;
    const ctx2d = ctx.getContext('2d');
    if (btcChart) btcChart.destroy();

    const grad = ctx2d.createLinearGradient(0, 0, 0, 100);
    grad.addColorStop(0, 'rgba(34, 197, 94, 0.2)');
    grad.addColorStop(1, 'rgba(34, 197, 94, 0)');

    btcChart = new Chart(ctx2d, {
      type: 'line',
      data: {
        labels,
        datasets: [{
          label: 'BTC/USDT',
          data,
          borderColor: '#22c55e',
          backgroundColor: grad,
          fill: true,
          tension: 0.2,
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 3,
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 500 },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: 'rgba(15, 19, 24, 0.95)',
            callbacks: { label: (c) => `$${Number(c.raw).toLocaleString(undefined, { minimumFractionDigits: 2 })}` },
          },
        },
        scales: {
          x: {
            display: true,
            ticks: { maxTicksLimit: 6, font: { size: 8 }, color: '#6b7280' },
            grid: { color: 'rgba(30, 37, 45, 0.6)' },
          },
          y: {
            display: true,
            ticks: { font: { size: 8 }, color: '#6b7280' },
            grid: { color: 'rgba(30, 37, 45, 0.6)' },
          },
        },
      },
    });
  } catch (e) {
    console.error('BTC chart fetch failed:', e);
  }
  const fallback = document.getElementById('btc-chart-fallback');
  if (fallback) fallback.textContent = btcChart ? '' : 'Live BTC — connect for data';
}

// ─── Params (wallet, etc.) ────────────────────────────────────────────────────

async function refreshParams() {
  try {
    const p = await fetchApi('/params');
    document.getElementById('wallet-info').textContent = p.paper_trading
      ? 'Paper mode — no wallet'
      : 'Live mode — check .env';
  } catch (e) {
    document.getElementById('wallet-info').textContent = '—';
  }
}

// ─── Controls ──────────────────────────────────────────────────────────────────

document.querySelectorAll('.toggle-btn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const mode = btn.dataset.mode;
    document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    try {
      await fetch(`${API_BASE}/api/mode`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paper_trading: mode === 'paper' }),
      });
    } catch (e) {
      console.error('Mode update failed:', e);
    }
  });
});

document.getElementById('btn-reset').addEventListener('click', async () => {
  const amount = parseFloat(document.getElementById('reset-amount').value) || 1000;
  if (!confirm(`Reset everything to $${amount}? Bankroll, charts, and UI will refresh.`)) return;
  try {
    await fetch(`${API_BASE}/api/reset?amount=${amount}`, { method: 'POST' });
    await refreshAll();
    location.reload();
  } catch (e) {
    alert('Reset failed: ' + e.message);
  }
});

// ─── Refresh All ──────────────────────────────────────────────────────────────

async function refreshAll() {
  await Promise.all([
    refreshStatus(),
    refreshPositions(),
    refreshTrades(),
    refreshChart(),
    refreshTradeGrid(),
    refreshBtcChart(),
    refreshParams(),
  ]);
}

// Init & Poll
refreshAll();
setInterval(refreshAll, 10000); // every 10s

// Rebuild charts on resize
let resizeDebounce;
window.addEventListener('resize', () => {
  clearTimeout(resizeDebounce);
  resizeDebounce = setTimeout(() => {
    if (pnlChart) refreshChart();
    if (btcChart) refreshBtcChart();
  }, 150);
});
