/**
 * PolyQuant Terminal — Bloomberg-style live dashboard
 *
 * Modules:
 *   FeedManager/ChartState — symbol + timeframe state; TradingView widget only
 *   FeedManager    — Binance WebSocket with auto-reconnect + status badge
 *   CommandPalette — overlay palette (/ to open, ↑↓ navigate, ↵ run)
 *   KeyboardShortcuts — global bindings (1/2/3 symbols, D/P/T/F/I/C pages)
 */

// ── XSS helpers ──────────────────────────────────────────────────────────────

function esc(v) {
  if (v == null) return '';
  return String(v)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function safeUrl(url) {
  if (!url || typeof url !== 'string') return '';
  const t = url.trim();
  return t.startsWith('https://') ? t : '';
}

// ── Formatters ────────────────────────────────────────────────────────────────

function fmtDollar(v, { signed = true, decimals = 2 } = {}) {
  if (v == null || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  const prefix = signed && n >= 0 ? '+$' : n < 0 ? '-$' : '$';
  return `${prefix}${Math.abs(n).toFixed(decimals)}`;
}

function fmtPct(v, decimals = 1) {
  if (v == null || Number.isNaN(Number(v))) return '—';
  const n = Number(v);
  return `${n >= 0 ? '+' : ''}${n.toFixed(decimals)}%`;
}

function fmtDir(d) {
  return (d || '').toUpperCase() === 'YES' ? 'UP' : 'DOWN';
}

function fmtTypeLabel(t, isSim, question) {
  if (isSim) return '5M';
  const v = (t || '').toLowerCase();
  if (v === '5min') return '5M';
  if (v === 'event' || v === 'macro') return 'EVT';
  if (v === 'price') return 'PRC';
  if (question && /5m|5-min|btc.*up.*down/i.test(String(question))) return '5M';
  return '5M';
}

function fmtTypeKey(t, isSim, question) {
  if (isSim) return 'sim';
  const v = (t || '').toLowerCase();
  if (v === '5min') return 'sim';
  if (question && /5m|5-min|btc.*up.*down/i.test(String(question))) return 'sim';
  return v || 'sim';
}

function truncate(str, max) {
  const s = (str || '').trim();
  return s.length <= max ? s : s.slice(0, max) + '…';
}

function timeAgo(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const s = Math.floor((Date.now() - d) / 1000);
  if (s < 60) return 'now';
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

function fmtTs(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  const mo = String(d.getMonth() + 1).padStart(2, '0');
  const dy = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${mo}/${dy} ${hh}:${mm}:${ss}`;
}

// ── API fetch ─────────────────────────────────────────────────────────────────
// Use same origin when served over http(s); fallback for file:// so data can load
const _apiBase = (typeof window !== 'undefined' && /^https?:/.test(window.location?.protocol || ''))
  ? '' : (window.__POLYQUANT_API__ || 'http://127.0.0.1:8080');

async function apiFetch(path, { silent = false } = {}) {
  const url = _apiBase + '/api' + path;
  const r = await fetch(url);
  if (!r.ok) {
    if (!silent) console.warn(`API ${path} failed: ${r.status}`);
    throw new Error(`${r.status} ${path}`);
  }
  return r.json();
}

// ── Chart state (symbol + timeframe for TradingView only) ────────────────────
let _pnlChart = null;

const ChartState = (() => {
  let _sym = 'btc';
  let _tf = '1m';

  function _updateTitle() {
    const titleEl = document.getElementById('chart-sym-title');
    if (titleEl) titleEl.innerHTML = `${_sym.toUpperCase()} / USD  <span class="ph-sub">${_tf}</span>`;
    document.querySelectorAll('.sym-btn').forEach(b => b.classList.toggle('active', (b.dataset.sym || 'btc') === _sym));
    document.querySelectorAll('.tf-btn').forEach(b => b.classList.toggle('active', (b.dataset.tf || '1m') === _tf));
  }

  function switchSymbol(sym) {
    if (!['btc', 'eth', 'sol'].includes(sym)) return;
    if (sym === _sym) return;
    _sym = sym;
    _updateTitle();
    if (typeof TradingViewWidget !== 'undefined') TradingViewWidget.load(_sym, _tf);
    if (typeof updateChartPriceBadge === 'function') updateChartPriceBadge();
  }

  function switchTimeframe(tf) {
    if (!['1m', '5m', '15m', '1h'].includes(tf)) return;
    if (tf === _tf) return;
    _tf = tf;
    _updateTitle();
    if (typeof TradingViewWidget !== 'undefined') TradingViewWidget.load(_sym, _tf);
  }

  return { getSymbol: () => _sym, getTimeframe: () => _tf, switchSymbol, switchTimeframe };
})();

// Alias for code that still references FeedManager
const FeedManager = ChartState;

// ── Clock & Countdown ─────────────────────────────────────────────────────────

function updateClock() {
  const now = new Date();
  const pad = n => n.toString().padStart(2, '0');
  const s = `${pad(now.getUTCHours())}:${pad(now.getUTCMinutes())}:${pad(now.getUTCSeconds())} UTC`;
  const el = document.getElementById('tb-clock');
  if (el) el.textContent = s;
}
setInterval(updateClock, 1000);
updateClock();

let _cd = 5;
function tickCd() {
  _cd--;
  if (_cd <= 0) _cd = 5;
  const ctrl = document.getElementById('ctrl-refresh');
  if (ctrl) ctrl.textContent = `${_cd}s`;
}
setInterval(tickCd, 1000);

// ── CommandPalette ─────────────────────────────────────────────────────────────
const CommandPalette = (() => {
  /** @type {{ id: string, label: string, keys: string[], action: () => void }[]} */
  const _cmds = [
    { id: 'btc',       label: 'Switch chart → BTC',  keys: ['btc','1'],       action: () => FeedManager.switchSymbol('btc') },
    { id: 'eth',       label: 'Switch chart → ETH',  keys: ['eth','2'],       action: () => FeedManager.switchSymbol('eth') },
    { id: 'sol',       label: 'Switch chart → SOL',  keys: ['sol','3'],       action: () => FeedManager.switchSymbol('sol') },
    { id: 'reconnect', label: 'Reconnect live feed', keys: ['reconnect','r'], action: () => FeedManager.reconnect() },
    { id: 'export',    label: 'Export trades CSV',   keys: ['export','csv'],  action: _exportCsv },
    { id: 'dash',      label: 'Dashboard',           keys: ['dash','d'],      action: () => showPage('dashboard') },
    { id: 'pos',       label: 'Positions',           keys: ['pos','p'],       action: () => showPage('positions') },
    { id: 'trades',    label: 'Trade history',       keys: ['trades','t'],    action: () => showPage('trades') },
    { id: 'perf',      label: 'Performance',         keys: ['perf','f'],      action: () => showPage('performance') },
    { id: 'intel',     label: 'Market intelligence', keys: ['intel','i'],     action: () => showPage('sentiment') },
    { id: 'trader',    label: 'Tracked trader',       keys: ['trader','o'],    action: () => showPage('trader') },
    { id: 'strategies', label: 'Strategy observer',   keys: ['strategies','s'], action: () => showPage('strategies') },
    { id: 'ctrl',      label: 'System controls',     keys: ['ctrl','c'],      action: () => showPage('controls') },
  ];

  let _open        = false;
  let _inputCb     = null;
  let _keyCb       = null;

  async function _exportCsv() {
    try {
      const rows = await apiFetch('/trades?limit=100');
      if (!rows.length) { alert('No trades to export.'); return; }
      const cols = ['id','direction','status','market','size_usdc','pnl','entry_price','exit_price','opened_at','resolved_at'];
      const csv  = [
        cols.join(','),
        ...rows.map(t => cols.map(k => {
          const v = String(t[k] ?? '').replace(/"/g, '""');
          return v.includes(',') ? `"${v}"` : v;
        }).join(','))
      ].join('\n');
      const a = Object.assign(document.createElement('a'), {
        href:     URL.createObjectURL(new Blob([csv], { type: 'text/csv' })),
        download: `polyquant_trades_${new Date().toISOString().slice(0,10)}.csv`,
      });
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) { alert('Export failed: ' + e.message); }
  }

  function _render(q) {
    const list = document.getElementById('cmd-list');
    if (!list) return;
    const lq = (q || '').toLowerCase().trim();
    const filtered = lq
      ? _cmds.filter(c => c.label.toLowerCase().includes(lq) || c.keys.some(k => k.startsWith(lq)))
      : _cmds;
    list.innerHTML = filtered.length
      ? filtered.map((c, i) =>
          `<li class="cmd-item${i === 0 ? ' cmd-active' : ''}" data-id="${esc(c.id)}" role="option" aria-selected="${i === 0}">
            <span class="cmd-item-label">${esc(c.label)}</span>
            <span class="cmd-item-keys">${c.keys.slice(0,2).map(k => `<kbd>${esc(k)}</kbd>`).join(' ')}</span>
          </li>`
        ).join('')
      : `<li class="cmd-item-empty">No matches for "${esc(q)}"</li>`;
    list.querySelectorAll('.cmd-item').forEach(li =>
      li.addEventListener('click', () => _exec(li.dataset.id))
    );
  }

  function _exec(id) {
    const cmd = _cmds.find(c => c.id === id);
    if (cmd) { close(); setTimeout(cmd.action, 40); }
  }

  function _moveActive(dir) {
    const items = [...document.querySelectorAll('.cmd-item')];
    if (!items.length) return;
    const cur  = items.findIndex(i => i.classList.contains('cmd-active'));
    const next = Math.max(0, Math.min(items.length - 1, cur + dir));
    items.forEach(i => { i.classList.remove('cmd-active'); i.setAttribute('aria-selected','false'); });
    items[next].classList.add('cmd-active');
    items[next].setAttribute('aria-selected','true');
    items[next].scrollIntoView({ block: 'nearest' });
  }

  function open() {
    const overlay = document.getElementById('cmd-palette');
    const input   = document.getElementById('cmd-input');
    if (!overlay || !input || _open) return;
    _open = true;
    overlay.classList.add('visible');
    overlay.setAttribute('aria-hidden','false');
    input.value = '';
    _render('');
    input.focus();
    _inputCb = e => _render(e.target.value);
    _keyCb   = e => {
      if (e.key === 'Escape')    { close(); return; }
      if (e.key === 'Enter')     { const a = document.querySelector('.cmd-active'); if (a) _exec(a.dataset.id); return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); _moveActive(1);  return; }
      if (e.key === 'ArrowUp')   { e.preventDefault(); _moveActive(-1); }
    };
    input.addEventListener('input',   _inputCb);
    input.addEventListener('keydown', _keyCb);
  }

  function close() {
    const overlay = document.getElementById('cmd-palette');
    const input   = document.getElementById('cmd-input');
    if (!overlay || !_open) return;
    _open = false;
    overlay.classList.remove('visible');
    overlay.setAttribute('aria-hidden','true');
    if (input) {
      if (_inputCb) input.removeEventListener('input',   _inputCb);
      if (_keyCb)   input.removeEventListener('keydown', _keyCb);
      _inputCb = _keyCb = null;
    }
  }

  function isOpen() { return _open; }

  document.addEventListener('click', e => {
    if (e.target.id === 'cmd-palette') close();
  });

  return { open, close, isOpen };
})();

// ── Keyboard Shortcuts ─────────────────────────────────────────────────────────
document.addEventListener('keydown', e => {
  const tag    = (e.target.tagName || '').toLowerCase();
  const typing = tag === 'input' || tag === 'textarea' || e.target.isContentEditable;

  if (e.key === 'Escape') { CommandPalette.close(); return; }

  if (e.key === '/' && !typing && !CommandPalette.isOpen()) {
    e.preventDefault();
    CommandPalette.open();
    return;
  }

  if (typing || CommandPalette.isOpen()) return;

  switch (e.key) {
    case '1': FeedManager.switchSymbol('btc'); break;
    case '2': FeedManager.switchSymbol('eth'); break;
    case '3': FeedManager.switchSymbol('sol'); break;
    case 'd': case 'D': showPage('dashboard');   break;
    case 'p': case 'P': showPage('positions');   break;
    case 't': case 'T': showPage('trades');      break;
    case 'f': case 'F': showPage('performance'); break;
    case 'i': case 'I': showPage('sentiment');   break;
    case 'o': case 'O': showPage('trader');      break;
    case 's': case 'S': showPage('strategies');  break;
    case 'c': case 'C': showPage('controls');    break;
  }
});

// ── setCard helper ────────────────────────────────────────────────────────────

function setCard(id, val, sub, cls) {
  const v = document.getElementById(`mv-${id}`);
  const s = document.getElementById(`ms-${id}`);
  if (v) { v.textContent = val; v.className = `mc-val${cls ? ' ' + cls : ''}`; }
  if (s) { s.textContent = sub; s.className = `mc-sub${cls ? ' ' + cls : ''}`; }
}

/** Render one activity item (open or closed) as a card: market title, timestamp, direction + cents, shares, price bar, value row. */
function renderActivityCard(it) {
  const d_ = fmtDir(it.direction);
  const dirCls = d_.toLowerCase() === 'down' ? 'down' : 'up';
  const title = esc(it.market_title || it.market || 'Polymarket BTC 5m');
  const ts = it.type === 'open'
    ? (it.opened_at ? 'Opened ' + fmtTs(it.opened_at) : '')
    : (it.resolved_at ? 'Resolved ' + fmtTs(it.resolved_at) : '');
  const outcomeLabel = it.type === 'open' ? 'OPEN' : ((it.status || '').toLowerCase() === 'won' ? 'WON' : 'LOST');
  const outcomeCls = it.type === 'open' ? 'open' : (it.status || '').toLowerCase() === 'won' ? 'won' : 'lost';

  if (it.type === 'open') {
    const cents = it.entry_price_cents != null ? it.entry_price_cents : (it.entry_price != null ? Math.round(it.entry_price * 100 * 10) / 10 : '—');
    const shares = it.shares != null ? it.shares.toLocaleString(undefined, { maximumFractionDigits: 1 }) : '—';
    const toWin = it.to_win != null ? `to win $${Number(it.to_win).toFixed(2)}` : '';
    return (
      '<div class="act-card act-open">' +
      '<div class="act-card-head">' +
      '<span class="act-card-badge ' + outcomeCls + '">' + outcomeLabel + '</span>' +
      (ts ? '<span class="act-card-ts">' + esc(ts) + '</span>' : '') +
      '</div>' +
      '<div class="act-card-title">' + title + '</div>' +
      '<div class="act-card-direction ' + dirCls + '">' + d_ + ' ' + cents + (typeof cents === 'number' ? '¢' : '') + '</div>' +
      '<div class="act-card-shares">' + shares + ' shares</div>' +
      '<div class="act-card-bar"><span class="current">' + (typeof cents === 'number' ? cents + '¢' : '—') + '</span><span class="max">100¢</span></div>' +
      '<div class="act-card-value">' +
      '<span class="position">$' + (it.size_usdc != null ? Number(it.size_usdc).toFixed(2) : '—') + '</span>' +
      (toWin ? '<span class="pnl pos">' + toWin + '</span>' : '') +
      '</div></div>'
    );
  }

  const pnlCls = (it.pnl || 0) >= 0 ? 'positive' : 'negative';
  const pnlStr = it.pnl != null ? fmtDollar(it.pnl) : '—';
  const pctStr = it.pnl_pct != null ? (it.pnl_pct >= 0 ? '+' : '') + it.pnl_pct.toFixed(2) + '%' : '';
  const exitCents = it.exit_price_cents != null ? it.exit_price_cents : (it.status === 'won' ? 100 : 0);
  const shares = it.shares != null ? it.shares.toLocaleString(undefined, { maximumFractionDigits: 1 }) : '—';
  const positionUsd = it.position_usd != null ? Number(it.position_usd).toFixed(2) : '—';

  return (
    '<div class="act-card act-closed">' +
    '<div class="act-card-head">' +
    '<span class="act-card-badge ' + outcomeCls + '">' + outcomeLabel + '</span>' +
    (ts ? '<span class="act-card-ts">' + esc(ts) + '</span>' : '') +
    '</div>' +
    '<div class="act-card-title">' + title + '</div>' +
    '<div class="act-card-direction ' + dirCls + '">' + d_ + ' ' + (exitCents !== null && exitCents !== undefined ? exitCents + '¢' : '—') + '</div>' +
    '<div class="act-card-shares">' + shares + ' shares</div>' +
    '<div class="act-card-bar"><span class="current">' + (exitCents != null ? exitCents + '¢' : '—') + '</span><span class="max">100¢</span></div>' +
    '<div class="act-card-value">' +
    '<span class="position">$' + positionUsd + '</span>' +
    '<span class="pnl ' + pnlCls + '">' + pnlStr + (pctStr ? ' (' + pctStr + ')' : '') + '</span>' +
    '</div></div>'
  );
}

// ── Ticker (BTC price + sentiment) ───────────────────────────────────────────

/** Set the chart header price badge to the current chart symbol's price (from ticker). */
function updateChartPriceBadge() {
  const badge = document.getElementById('chart-price-badge');
  if (!badge) return;
  const sym = ChartState.getSymbol();
  const el = document.getElementById(sym === 'btc' ? 't-btc-price' : sym === 'eth' ? 't-eth-price' : 't-sol-price');
  if (el && el.textContent && el.textContent !== '—') {
    badge.textContent = el.textContent;
    badge.style.color = el.className.includes('up') ? '#22c55e' : el.className.includes('down') ? '#ef4444' : '';
  } else {
    badge.textContent = '—';
    badge.style.color = '';
  }
}

async function refreshTicker() {
  try {
    const t = await apiFetch('/ticker');

    if (t.btc_price != null) {
      const price = Number(t.btc_price);
      const priceStr = price.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
      const chPct = t.btc_change_1h_pct;
      const up = chPct != null && chPct >= 0;
      const pEl = document.getElementById('t-btc-price');
      if (pEl) {
        pEl.textContent = `$${priceStr}`;
        pEl.className = `tb-val accent tb-btc-big ${chPct != null ? (up ? 'up' : 'down') : ''}`;
      }
      const cEl = document.getElementById('t-btc-change');
      if (cEl && chPct != null) {
        cEl.textContent = `${up ? '+' : ''}${chPct.toFixed(2)}%`;
        cEl.className = `tb-val ${up ? 'up' : 'down'}`;
      }
      updateChartPriceBadge();
    }

    if (t.fear_greed != null) {
      const fg = Number(t.fear_greed);
      const lbl = t.fear_greed_label || '';
      const fgEl = document.getElementById('t-fg');
      if (fgEl) {
        fgEl.textContent = `${fg} ${lbl}`;
        fgEl.className = `tb-val ${fg >= 55 ? 'up' : fg <= 45 ? 'down' : ''}`;
      }
    }

    if (t.funding_rate != null) {
      const fr = Number(t.funding_rate) * 100;
      const frEl = document.getElementById('t-fund');
      if (frEl) {
        frEl.textContent = `${fr >= 0 ? '+' : ''}${fr.toFixed(4)}%`;
        frEl.className = `tb-val ${fr > 0.01 ? 'down' : fr < -0.01 ? 'up' : ''}`;
      }
    }
  } catch (_e) { /* ticker fails silently */ }
}

// ── Crypto prices (ETH, SOL) ───────────────────────────────────────────────────

async function refreshCryptoPrices() {
  try {
    const p = await apiFetch('/crypto-prices');
    const set = (id, data) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (!data || data.price == null) { el.textContent = '—'; return; }
      const ch = data.change_24h != null ? data.change_24h : null;
      const chStr = ch != null ? ` ${ch >= 0 ? '+' : ''}${ch.toFixed(2)}%` : '';
      el.textContent = `$${Number(data.price).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}${chStr}`;
      el.className = `tb-val ${ch != null && ch >= 0 ? 'up' : ch != null ? 'down' : ''}`;
    };
    set('t-eth-price', p.eth);
    set('t-sol-price', p.sol);
    updateChartPriceBadge();
  } catch (_e) {}
}

// ── Dashboard (single bulk fetch) ──────────────────────────────────────────────

function applyDashboardData(d) {
  if (!d) return;
  const s = d.status || {};
  const total = s.total_equity ?? (Number(s.balance || 0) + Number(s.locked_in_positions || 0));
  const pnl = s.total_pnl ?? (total - Number(s.starting_balance || 1000));
  const wr = Number(s.win_rate || 0) * 100;
  setCard('equity', total != null ? `$${Number(total).toFixed(2)}` : '—', s.return_pct != null ? `${Number(s.return_pct).toFixed(1)}%` : '—', pnl >= 0 ? 'pos' : 'neg');
  setCard('avail', s.balance != null ? `$${Number(s.balance).toFixed(2)}` : '—', '', '');
  setCard('locked', s.locked_in_positions != null ? `$${Number(s.locked_in_positions).toFixed(2)}` : '—', '', '');
  setCard('pnl', pnl != null ? fmtDollar(pnl) : '—', s.daily_pnl != null ? `today ${fmtDollar(s.daily_pnl)}` : '—', pnl >= 0 ? 'pos' : 'neg');
  setCard('daily', s.daily_pnl != null ? fmtDollar(s.daily_pnl) : '—', `(${s.trades_today ?? 0} today)`, Number(s.daily_pnl) >= 0 ? 'pos' : 'neg');
  setCard('trades', s.n_total ?? 0, `${s.n_wins ?? 0}W / ${s.n_losses ?? 0}L`, '');
  setCard('wr', wr != null ? `${wr.toFixed(0)}%` : '—', '', wr >= 50 ? 'pos' : 'neg');
  const params = s.params || {};
  const edge = Number(params.min_edge ?? 0);
  const kelly = Number(params.kelly_fraction ?? 0);
  setCard('edge', edge ? edge.toFixed(3) : '—', kelly ? `kelly ${kelly.toFixed(2)}` : '—', 'accent');
  const modeEl = document.getElementById('t-mode');
  if (modeEl) { modeEl.textContent = s.paper_trading !== false ? 'PAPER' : 'LIVE'; modeEl.className = `tb-val ${s.paper_trading !== false ? '' : 'down'}`; }
  const openEl = document.getElementById('t-open');
  if (openEl) openEl.textContent = s.n_open ?? 0;

  // PnL badge: always use actual total_pnl (not raw cumulative)
  const pnlBadge = document.getElementById('pnl-badge');
  if (pnlBadge && pnl != null) {
    pnlBadge.textContent = pnl >= 0 ? `+$${pnl.toFixed(2)}` : `-$${Math.abs(pnl).toFixed(2)}`;
    pnlBadge.style.color = pnl >= 0 ? '#22c55e' : '#ef4444';
  }

  const items = d.activity || [];
  const el = document.getElementById('activity-feed');
  const cnt = document.getElementById('activity-count');
  if (cnt) cnt.textContent = items.length;
  if (!el) return;
  if (!items.length) {
    el.innerHTML = '<div class="empty" role="status">No orders yet. Start the bot to begin trading.</div>';
  } else {
    el.innerHTML = items.map(it => renderActivityCard(it)).join('');
  }

  // Dashboard mini positions table — show: # | DIR | PRICE | TO WIN
  const pos = d.positions || [];
  const posBody = document.getElementById('positions-body');
  ['pos-count', 'pos-count-full'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.textContent = pos.length;
  });

  const dashPosBody = document.getElementById('dash-pos-body');
  if (dashPosBody) {
    if (!pos.length) {
      dashPosBody.innerHTML = '<tr><td colspan="4" class="empty">No open positions</td></tr>';
    } else {
      dashPosBody.innerHTML = pos.map(p => {
        const d_ = fmtDir(p.direction);
        return `<tr>
          <td>#${p.id}</td>
          <td class="dir-${d_.toLowerCase()}">${d_}</td>
          <td>${Number(p.entry_price ?? 0).toFixed(3)}</td>
          <td class="pnl-pos">$${Number(p.to_win ?? 0).toFixed(2)}</td>
        </tr>`;
      }).join('');
    }
  }

  // Full positions page — ID | DIR | TYPE | MARKET | ENTRY | TO WIN | LINK
  if (posBody) {
    if (!pos.length) {
      posBody.innerHTML = '<tr><td colspan="7" class="empty" role="status">No open positions</td></tr>';
    } else {
      posBody.innerHTML = pos.map(p => {
        const d_ = fmtDir(p.direction);
        const tok    = String(p.token_id || '');
        const isSim  = tok.startsWith('SIM-');
        const typeK  = fmtTypeKey(p.market_type, isSim, p.question);
        const typeL  = fmtTypeLabel(p.market_type, isSim, p.question);
        const q      = esc(truncate(p.question || `Polymarket BTC 5m ${d_}`, 48));
        const link   = safeUrl(p.polymarket_url);
        return `<tr>
          <td>#${p.id}</td>
          <td class="dir-${d_.toLowerCase()}">${d_}</td>
          <td><span class="tb-badge tb-${typeK}">${esc(typeL)}</span></td>
          <td title="${esc(p.question || '')}">${q}</td>
          <td>${Number(p.entry_price ?? 0).toFixed(3)}</td>
          <td class="pnl-pos">$${Number(p.to_win ?? 0).toFixed(2)}</td>
          <td>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener" class="pm-link">↗</a>` : ''}</td>
        </tr>`;
      }).join('');
    }
  }

  // Trades
  const tr = d.trades || [];
  const trBody = document.getElementById('trades-body');
  const trCnt  = document.getElementById('trade-count');
  if (trCnt) trCnt.textContent = tr.length;
  if (trBody) {
    if (!tr.length) {
      trBody.innerHTML = '<tr><td colspan="10" class="empty" role="status">No trades yet.</td></tr>';
    } else {
      trBody.innerHTML = tr.map(t => {
        const d_     = fmtDir(t.direction);
        const isLost = (t.status || '').toLowerCase() === 'lost';
        const rawPnl = t.pnl ?? 0;
        const pnl    = isLost && rawPnl >= 0 ? -Math.abs(t.size_usdc ?? 0) : rawPnl;
        const pnlCls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        const tok    = String(t.token_id || '');
        const isSim  = tok.startsWith('SIM-');
        const typeK  = fmtTypeKey(t.market_type, isSim, t.question);
        const typeL  = fmtTypeLabel(t.market_type, isSim, t.question);
        const q      = esc(truncate(t.question || `Polymarket BTC 5m ${d_}`, 45));
        const link   = safeUrl(t.polymarket_url);
        const btcE   = t.btc_price_entry ? `$${Number(t.btc_price_entry).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : '—';
        const btcX   = t.btc_price_exit  ? `$${Number(t.btc_price_exit).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : '—';
        const resultCls = isLost ? 'neg' : 'pos';
        const resultTxt = isLost ? 'LOST' : 'WON';
        return `<tr>
          <td>#${t.id}</td>
          <td class="dir-${d_.toLowerCase()}">${d_}</td>
          <td><span class="tb-badge tb-${typeK}">${esc(typeL)}</span></td>
          <td><span class="tb-badge ${resultCls}">${resultTxt}</span></td>
          <td title="${esc(t.question || '')}">${q}</td>
          <td>$${Number(t.size_usdc ?? 0).toFixed(2)}</td>
          <td class="${pnlCls}">${fmtDollar(pnl)}</td>
          <td>${btcE}</td>
          <td>${btcX}</td>
          <td>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener" class="pm-link">↗</a>` : ''}</td>
        </tr>`;
      }).join('');
    }
  }
}

// ── Live Activity feed ─────────────────────────────────────────────────────────

async function refreshActivity() {
  try {
    const items = await apiFetch('/activity?limit=25');
    const el = document.getElementById('activity-feed');
    const cnt = document.getElementById('activity-count');
    if (!el) return;
    if (cnt) cnt.textContent = items.length;
    if (!items.length) {
      el.innerHTML = '<div class="empty">No activity yet</div>';
      return;
    }
    el.innerHTML = items.map(it => renderActivityCard(it)).join('');
  } catch (_e) {}
}

// ── Footer sentiment ticker ────────────────────────────────────────────────────

async function refreshFooterSentiment() {
  try {
    const s = await apiFetch('/sentiment');
    const el = document.getElementById('footer-sentiment');
    if (!el) return;
    if (!s.available || s.fear_greed_value == null) {
      el.textContent = 'Market sentiment loading…';
      return;
    }
    const fg = s.fear_greed_value ?? 50;
    const lbl = s.fear_greed_label || '';
    const fr = (s.funding_rate ?? 0) * 100;
    const oi = s.oi_change_pct ?? 0;
    const headlines = (s.headlines || []).slice(0, 3).join(' · ');
    const parts = [
      `Fear & Greed: ${fg}/100 (${lbl})`,
      `Funding: ${fr >= 0 ? '+' : ''}${fr.toFixed(4)}%`,
      `OI 1h: ${oi >= 0 ? '+' : ''}${oi.toFixed(1)}%`,
    ];
    if (headlines) parts.push(`News: ${headlines}`);
    el.textContent = parts.join('  │  ') + '  │  ' + parts.join('  │  ');
  } catch (_e) {
    const el = document.getElementById('footer-sentiment');
    if (el) el.textContent = 'Sentiment unavailable';
  }
}

// ── Insights (Sentiment / Learn / Wallets) ─────────────────────────────────────

async function refreshInsights() {
  const activePane = document.querySelector('.insight-pane.active');
  const tab = activePane ? activePane.id.replace('pane-', '') : 'sentiment';
  try {
    if (tab === 'sentiment') {
      const s = await apiFetch('/sentiment');
      const el = document.getElementById('insight-sentiment');
      if (el) {
        if (!s.available) { el.textContent = 'Sentiment unavailable'; return; }
        let txt = `Fear & Greed: ${s.fear_greed_value ?? '—'}/100 — ${s.fear_greed_label || '—'}\n`;
        txt += `Funding rate: ${(s.funding_rate ?? 0) * 100 >= 0 ? '+' : ''}${((s.funding_rate ?? 0) * 100).toFixed(4)}%\n`;
        txt += `OI change 1h: ${(s.oi_change_pct ?? 0) >= 0 ? '+' : ''}${(s.oi_change_pct ?? 0).toFixed(2)}%\n`;
        txt += `Composite: ${((s.composite_score ?? 0.5) * 100).toFixed(0)}%\n\n`;
        txt += 'Headlines:\n' + (s.headlines || []).map(h => '• ' + h).join('\n') || '  None';
        el.textContent = txt;
      }
    } else if (tab === 'learn') {
      const l = await apiFetch('/learn');
      const el = document.getElementById('insight-learn');
      if (el) el.textContent = (l.report || l.error || '—').slice(0, 2000);
    } else if (tab === 'wallets') {
      const w = await apiFetch('/wallets');
      const el = document.getElementById('insight-wallets');
      if (el) el.textContent = (w.report || w.error || '—').slice(0, 2000);
    }
  } catch (_e) {}
}

async function refreshTraderPage() {
  const container = document.getElementById('trader-cards');
  const emptyEl = document.getElementById('trader-cards-empty');
  const profileEl = document.getElementById('trader-profile');
  const labelEl = document.getElementById('trader-label');
  const strategyEl = document.getElementById('trader-strategy');
  const wrEl = document.getElementById('trader-wr');
  const pnlEl = document.getElementById('trader-total-pnl');
  const nEl = document.getElementById('trader-n-trades');
  const countEl = document.getElementById('trader-pos-count');
  if (!container) return;
  try {
    const r = await apiFetch('/tracked-traders', { silent: true });
    const traders = r.traders || [];
    const first = traders[0];
    if (!first || !first.positions || !first.positions.length) {
      if (emptyEl) { emptyEl.style.display = 'block'; emptyEl.textContent = r.error || 'No tracked traders or no open positions. Add wallets in wallets_to_watch.txt'; }
      container.innerHTML = '';
      if (profileEl) profileEl.style.display = 'none';
      if (countEl) countEl.textContent = '0';
      return;
    }
    if (profileEl) profileEl.style.display = 'block';
    const prof = first.profile || {};
    if (labelEl) labelEl.textContent = prof.label || prof.address?.slice(0, 10) || '—';
    if (strategyEl) strategyEl.textContent = prof.strategy_type || '—';
    if (wrEl) wrEl.textContent = (prof.win_rate != null ? (prof.win_rate * 100).toFixed(1) + '%' : '—');
    if (pnlEl) {
      const pnl = prof.total_pnl;
      pnlEl.textContent = (pnl != null ? (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2) : '—');
      pnlEl.className = pnl != null && pnl >= 0 ? 'pos' : pnl != null ? 'neg' : '';
    }
    if (nEl) nEl.textContent = String(prof.n_trades ?? 0);
    if (countEl) countEl.textContent = String(first.positions.length);

    if (emptyEl) emptyEl.style.display = 'none';
    container.innerHTML = first.positions.map(card => {
      const dirCls = (card.direction_short || '').toLowerCase() === 'down' ? 'down' : 'up';
      const pnlCls = card.pnl_usd > 0 ? 'positive' : card.pnl_usd < 0 ? 'negative' : 'neutral';
      const pnlStr = card.pnl_usd >= 0 ? '+' + card.pnl_usd.toFixed(2) : card.pnl_usd.toFixed(2);
      const pctStr = card.pnl_pct != null ? (card.pnl_pct >= 0 ? '+' : '') + card.pnl_pct.toFixed(2) + '%' : '';
      return (
        '<div class="trader-card">' +
        '<span class="tc-icon" aria-hidden="true">' + (card.market_icon === 'BTC' ? '₿' : '◆') + '</span>' +
        '<div class="tc-title">' + esc(card.market_title || 'Unknown market') + '</div>' +
        '<div class="tc-direction ' + dirCls + '">' + esc(card.direction_short || '') + ' ' + (card.price_cents != null ? card.price_cents + '¢' : '—') + '</div>' +
        '<div class="tc-shares">' + (card.shares != null ? card.shares.toLocaleString(undefined, { maximumFractionDigits: 1 }) + ' shares' : '—') + '</div>' +
        '<div class="tc-price-bar">' +
        '<span class="current">' + (card.current_cents != null ? card.current_cents + '¢' : '—') + '</span>' +
        '<span class="max">' + (card.max_cents != null ? card.max_cents + '¢' : '100¢') + '</span>' +
        '</div>' +
        '<div class="tc-value-row">' +
        '<span class="tc-position-usd">$' + (card.position_usd != null ? card.position_usd.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '—') + '</span>' +
        '<span class="tc-pnl ' + pnlCls + '">$' + pnlStr + (pctStr ? ' (' + pctStr + ')' : '') + '</span>' +
        '</div></div>'
      );
    }).join('');
  } catch (e) {
    if (emptyEl) { emptyEl.style.display = 'block'; emptyEl.textContent = 'Failed to load: ' + (e.message || 'Unknown error'); }
    container.innerHTML = '';
  }
}

async function refreshStrategiesPage() {
  const tbody = document.getElementById('strategies-body');
  if (!tbody) return;
  try {
    const r = await apiFetch('/strategy-observer', { silent: true });
    const modes = r.modes || [];
    if (!modes.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="empty">No strategy modes (run with orchestrator for live data)</td></tr>';
      return;
    }
    tbody.innerHTML = modes.map(m => {
      const pnl = m.total_pnl != null ? m.total_pnl : 0;
      const pnlCls = pnl > 0 ? 'pos' : pnl < 0 ? 'neg' : '';
      const wr = (m.win_rate != null ? m.win_rate * 100 : 0).toFixed(1);
      return (
        '<tr>' +
        '<td><strong>' + esc(m.label || m.id) + '</strong></td>' +
        '<td>' + (m.min_edge != null ? m.min_edge.toFixed(3) : '—') + '</td>' +
        '<td>' + (m.kelly != null ? m.kelly.toFixed(2) : '—') + '</td>' +
        '<td>$' + (m.balance != null ? m.balance.toFixed(2) : '—') + '</td>' +
        '<td class="' + pnlCls + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '</td>' +
        '<td>' + (m.n_trades ?? 0) + '</td>' +
        '<td>' + wr + '%</td></tr>'
      );
    }).join('');
  } catch (e) {
    tbody.innerHTML = '<tr><td colspan="7" class="empty">Error: ' + esc(e.message || 'Unknown') + '</td></tr>';
  }
}

// ── Status ────────────────────────────────────────────────────────────────────

async function refreshStatus() {
  try {
    const s = await apiFetch('/status');
    const total = s.total_equity ?? (Number(s.balance || 0) + Number(s.locked_in_positions || 0));
    const pnl = s.total_pnl ?? (total - Number(s.starting_balance || 1000));
    const wr = Number(s.win_rate || 0) * 100;

    const modeEl = document.getElementById('t-mode');
    if (modeEl) {
      modeEl.textContent = s.paper_trading !== false ? 'PAPER' : 'LIVE';
      modeEl.className = `tb-val ${s.paper_trading !== false ? '' : 'down'}`;
    }
    const openEl = document.getElementById('t-open');
    if (openEl) openEl.textContent = s.n_open ?? 0;

    const ksEl = document.getElementById('kill-sw');
    if (ksEl) {
      ksEl.textContent = s.kill_switch ? 'TRIGGERED' : 'ARMED';
      ksEl.className = `cv ${s.kill_switch ? 'killed' : 'ok'}`;
    }

    const paused = s.trading_paused === true;
    const hintEl = document.getElementById('trading-state-hint');
    if (hintEl) hintEl.textContent = paused ? 'Paused' : 'Running';
    const resumeBtn = document.getElementById('btn-resume');
    const pauseBtn  = document.getElementById('btn-pause');
    if (resumeBtn) { resumeBtn.style.display = paused ? 'block' : 'none'; resumeBtn.disabled = !paused; }
    if (pauseBtn)  { pauseBtn.style.display  = paused ? 'none' : 'block'; pauseBtn.disabled  = paused; }

    const pnlCls = pnl >= 0 ? 'pos' : 'neg';
    const retCls = (s.return_pct || 0) >= 0 ? 'pos' : 'neg';
    setCard('equity', `$${Number(total).toFixed(2)}`, fmtPct(s.return_pct || 0) + ' return', retCls);
    setCard('avail',  `$${Number(s.balance || 0).toFixed(2)}`, 'free capital', '');
    setCard('locked', `$${Number(s.locked_in_positions || 0).toFixed(2)}`,
      `${s.n_open ?? 0} active position${(s.n_open ?? 0) !== 1 ? 's' : ''}`, '');
    setCard('pnl', fmtDollar(pnl), `vs $${Number(s.starting_balance || 1000).toFixed(0)} start`, pnlCls);
    const dailyCls = (s.daily_pnl || 0) >= 0 ? 'pos' : 'neg';
    setCard('daily', fmtDollar(s.daily_pnl || 0),
      `${s.trades_today ?? 0} trade${(s.trades_today ?? 0) !== 1 ? 's' : ''} today`, dailyCls);
    setCard('trades', String(s.n_total ?? 0), `${s.n_wins ?? 0}W / ${s.n_losses ?? 0}L`, '');
    const wrCls = wr >= 55 ? 'pos' : wr < 40 ? 'neg' : '';
    setCard('wr', `${wr.toFixed(1)}%`, `${s.n_wins ?? 0} wins`, wrCls);

    return s;
  } catch (e) {
    console.error('status:', e);
    return null;
  }
}

// ── Params ────────────────────────────────────────────────────────────────────

async function refreshParams() {
  try {
    const p = await apiFetch('/params');
    const edge  = Number(p.min_edge || 0);
    const kelly = Number(p.kelly_fraction || 0);
    setCard('edge', edge.toFixed(3), `5% per trade | kelly ${kelly.toFixed(2)}`, 'accent');
    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set('ctrl-min-edge',  edge.toFixed(4));
    set('ctrl-kelly',     kelly.toFixed(2));
    set('ctrl-max-pos',   '$' + (p.max_position_usdc ?? 50));
    set('ctrl-max-spread', (Number(p.max_spread || 0.1) * 100).toFixed(0) + '%');
  } catch (_e) {}
}

// ── Controls page status ──────────────────────────────────────────────────────

function refreshControls(s, ms) {
  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = val;
    if (cls) el.className = 'csv ' + cls;
  };
  if (s) {
    set('ctrl-market-mode', ms?.notice || (ms?.has_5min ? 'Polymarket 5m' : 'SIM (BTC 5m)'));
    set('ctrl-trading', s.trading_paused ? 'Paused' : 'Running', s.trading_paused ? 'neg' : 'pos');
    set('ctrl-open',    String(s.n_open ?? 0));
    set('ctrl-kill',    s.kill_switch ? 'TRIGGERED' : 'ARMED', s.kill_switch ? 'killed' : 'ok');
  }
  const cdEl = document.getElementById('ctrl-refresh');
  if (cdEl) cdEl.textContent = `${_cd}s`;
}

// ── Performance ───────────────────────────────────────────────────────────────

async function refreshPerformance() {
  const setAll = (base, val, cls) => {
    [base, base + '2'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.textContent = val; if (cls) el.className = `pv ${cls}`; }
    });
  };
  try {
    const p = await apiFetch('/performance');
    const sharpeCls = Number(p.sharpe) > 0.5 ? 'pos' : Number(p.sharpe) < 0 ? 'neg' : '';
    setAll('pv-sharpe', p.sharpe != null ? Number(p.sharpe).toFixed(3) : '—', sharpeCls);
    setAll('pv-brier',  p.brier  != null ? Number(p.brier).toFixed(4)  : '—', Number(p.brier) < 0.25 ? 'pos' : 'neg');
    setAll('pv-maxdd',  p.max_dd != null ? (Number(p.max_dd) * 100).toFixed(1) + '%' : '—', Number(p.max_dd) < 0.05 ? 'pos' : 'neg');
    setAll('pv-edge',   p.avg_edge != null ? Number(p.avg_edge).toFixed(4) : '—', Number(p.avg_edge) > 0.03 ? 'pos' : '');
    setAll('pv-apnl',   p.avg_pnl  != null ? fmtDollar(p.avg_pnl) : '—', Number(p.avg_pnl) >= 0 ? 'pos' : 'neg');
  } catch (_e) {}

  try {
    const ms = await apiFetch('/markets-summary');
    const mkts = ms.notice
      ? { text: ms.notice, cls: ms.has_5min ? '' : 'neg' }
      : { text: `${ms.total} market${ms.total !== 1 ? 's' : ''} loaded`, cls: 'pos' };
    ['pv-markets', 'pv-markets2'].forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.textContent = mkts.text; el.className = `pv ${mkts.cls}`; }
    });
  } catch (_e) {}
}

// ── Positions ─────────────────────────────────────────────────────────────────

async function refreshPositions() {
  try {
    const rows = await apiFetch('/positions');
    const tb   = document.getElementById('positions-body');
    const cnt  = document.getElementById('pos-count');
    if (cnt) cnt.textContent = rows.length;
    if (!rows.length) {
      if (tb) tb.innerHTML = '<tr><td colspan="7" class="empty">No open positions</td></tr>';
      return;
    }
    if (tb) {
      tb.innerHTML = rows.map(p => {
        const d_    = fmtDir(p.direction);
        const tok   = String(p.token_id || '');
        const isSim = tok.startsWith('SIM-');
        const typeK = fmtTypeKey(p.market_type, isSim, p.question);
        const typeL = fmtTypeLabel(p.market_type, isSim, p.question);
        const q     = esc(truncate(p.question || `Polymarket BTC 5m ${d_}`, 48));
        const link  = safeUrl(p.polymarket_url);
        return `<tr>
          <td>#${p.id}</td>
          <td class="dir-${d_.toLowerCase()}">${d_}</td>
          <td><span class="tb-badge tb-${typeK}">${esc(typeL)}</span></td>
          <td title="${esc(p.question || '')}">${q}</td>
          <td>${Number(p.entry_price ?? 0).toFixed(3)}</td>
          <td class="pnl-pos">$${Number(p.to_win ?? 0).toFixed(2)}</td>
          <td>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener" class="pm-link">↗</a>` : ''}</td>
        </tr>`;
      }).join('');
    }
  } catch (e) { console.error('positions:', e); }
}

// ── Trades ────────────────────────────────────────────────────────────────────

async function refreshTrades() {
  try {
    const rows = await apiFetch('/trades?limit=40');
    const tb   = document.getElementById('trades-body');
    const cnt  = document.getElementById('trade-count');
    if (cnt) cnt.textContent = rows.length;
    if (!rows.length) {
      if (tb) tb.innerHTML = '<tr><td colspan="11" class="empty">No closed trades yet</td></tr>';
      return;
    }
    if (tb) {
      tb.innerHTML = rows.map(t => {
        const d_        = fmtDir(t.direction);
        const isLost    = (t.status || '').toLowerCase() === 'lost';
        const rawPnl    = t.pnl ?? 0;
        const pnl       = isLost && rawPnl >= 0 ? -Math.abs(t.size_usdc ?? 0) : rawPnl;
        const pnlCls    = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        const tok       = String(t.token_id || '');
        const isSim     = tok.startsWith('SIM-');
        const typeK     = fmtTypeKey(t.market_type, isSim, t.question);
        const typeL     = fmtTypeLabel(t.market_type, isSim, t.question);
        const q         = esc(truncate(t.question || `Polymarket BTC 5m ${d_}`, 42));
        const link      = safeUrl(t.polymarket_url);
        const btcE      = t.btc_price_entry ? `$${Number(t.btc_price_entry).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : '—';
        const btcX      = t.btc_price_exit  ? `$${Number(t.btc_price_exit).toLocaleString(undefined, { maximumFractionDigits: 0 })}` : '—';
        const resultCls = isLost ? 'neg' : 'pos';
        const resultTxt = isLost ? 'LOST' : 'WON';
        const opened    = fmtTs(t.opened_at);
        const closed    = fmtTs(t.resolved_at);
        return `<tr>
          <td class="ts-cell" title="${esc(t.opened_at || '')}">${esc(opened)}</td>
          <td class="ts-cell" title="${esc(t.resolved_at || '')}">${esc(closed)}</td>
          <td>#${t.id}</td>
          <td class="dir-${d_.toLowerCase()}">${d_}</td>
          <td><span class="tb-badge ${resultCls}">${resultTxt}</span></td>
          <td title="${esc(t.question || '')}">${q}</td>
          <td>$${Number(t.size_usdc ?? 0).toFixed(2)}</td>
          <td class="${pnlCls}">${fmtDollar(pnl)}</td>
          <td>${btcE}</td>
          <td>${btcX}</td>
          <td>${link ? `<a href="${esc(link)}" target="_blank" rel="noopener" class="pm-link">↗</a>` : ''}</td>
        </tr>`;
      }).join('');
    }
  } catch (e) { console.error('trades:', e); }
}

// ── Trade Grid ────────────────────────────────────────────────────────────────

async function refreshGrid() {
  try {
    const rows = await apiFetch('/trade-grid');
    const html = !rows.length
      ? '<span style="font-size:9px;color:var(--dim)">No trades yet</span>'
      : rows.map(t => {
          const cls = (t.status || '').toLowerCase() === 'won' ? 'win' : 'loss';
          const raw = t.pnl ?? 0;
          const pnl = cls === 'loss' && raw >= 0 ? -Math.abs(t.size_usdc ?? 0) : raw;
          const tip = `#${t.id} ${pnl >= 0 ? '+' : '-'}$${Math.abs(pnl).toFixed(2)}`;
          return `<span class="tg-cell ${cls}" title="${esc(tip)}"></span>`;
        }).join('');
    ['trade-grid', 'trade-grid-perf'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.innerHTML = html;
    });
  } catch (_e) {}
}

// ── Charts ────────────────────────────────────────────────────────────────────

Chart.defaults.color = '#566070';
Chart.defaults.font.family = "'JetBrains Mono', monospace";
Chart.defaults.font.size = 9;
Chart.defaults.animation = { duration: 300 };
Chart.defaults.transitions = { active: { animation: { duration: 200 } } };

const CHART_GREEN = '#22c55e';
const CHART_RED   = '#ef4444';

function _chartOpts(yFmt) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 300 },
    transitions: { active: { animation: { duration: 200 } } },
    interaction: { intersect: false, mode: 'index' },
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: 'rgba(7,11,17,.97)',
        borderColor: '#1a2433',
        borderWidth: 1,
        titleFont: { size: 9 },
        bodyFont: { size: 9 },
        padding: 6,
      },
    },
    scales: {
      x: {
        grid: { color: 'rgba(21,30,44,.6)', drawBorder: false },
        ticks: { maxTicksLimit: 8, font: { size: 8 }, color: '#36404e' },
        border: { display: false },
      },
      y: {
        grid: { color: 'rgba(21,30,44,.6)', drawBorder: false },
        ticks: { font: { size: 8 }, color: '#36404e', callback: yFmt },
        border: { display: false },
        beginAtZero: false,
      },
    },
  };
}

async function refreshPnlChart() {
  try {
    const series = await apiFetch('/pnl-history');
    if (!series?.length) return;

    const starting = series[0]?.cumulative ?? 1000;
    const labels   = series.map((_, i) => i === 0 ? 'Start' : `#${i}`);
    const data     = series.map(s => s.cumulative);
    const last     = data[data.length - 1];
    const diff     = last - starting;
    const up       = diff >= 0;

    const ctx = document.getElementById('pnl-chart');
    if (!ctx) return;
    const ctx2      = ctx.getContext('2d');
    const lineColor = up ? CHART_GREEN : CHART_RED;
    const grad      = ctx2.createLinearGradient(0, 0, 0, ctx.offsetHeight || 160);
    grad.addColorStop(0, up ? 'rgba(34,197,94,0.22)' : 'rgba(239,68,68,0.18)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');

    if (_pnlChart) {
      _pnlChart.data.labels = labels;
      _pnlChart.data.datasets[0].data = data;
      _pnlChart.data.datasets[0].borderColor = lineColor;
      _pnlChart.data.datasets[0].backgroundColor = grad;
      _pnlChart.data.datasets[0].pointHoverBackgroundColor = lineColor;
      _pnlChart.data.datasets[1].data = data.map(() => starting);
      _pnlChart.update('active');
      return;
    }
    _pnlChart = new Chart(ctx2, {
      type: 'line',
      data: {
        labels,
        datasets: [
          { label: 'Equity', data, borderColor: lineColor, backgroundColor: grad,
            fill: true, tension: 0.3, borderWidth: 2, pointRadius: 0, pointHoverRadius: 3,
            pointHoverBackgroundColor: lineColor },
          { label: 'Baseline', data: data.map(() => starting),
            borderColor: 'rgba(86,96,112,.45)', borderDash: [3,3],
            borderWidth: 1, pointRadius: 0, fill: false },
        ],
      },
      options: {
        ..._chartOpts(v => `$${Number(v).toFixed(0)}`),
        plugins: {
          ..._chartOpts().plugins,
          tooltip: {
            ..._chartOpts().plugins.tooltip,
            callbacks: { label: c => `${c.dataset.label}: $${Number(c.raw).toFixed(2)}` },
          },
        },
      },
    });
  } catch (e) { console.error('pnl-chart:', e); }
}

// refreshBtcChart: no-op (chart is TradingView only)
async function refreshBtcChart() {}

// ── Controls ──────────────────────────────────────────────────────────────────

document.querySelectorAll('.mbtn').forEach(btn => {
  btn.addEventListener('click', async () => {
    const mode = btn.dataset.mode;
    document.querySelectorAll('.mbtn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    try {
      await fetch('/api/mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ paper_trading: mode === 'paper' }),
      });
    } catch (_e) {}
  });
});

document.getElementById('btn-reset').addEventListener('click', async () => {
  const amt = parseFloat(document.getElementById('reset-amount').value) || 1000;
  if (!confirm(`Reset bankroll to $${amt}?\n\nTrade history is kept. Balance resets to baseline.`)) return;
  try {
    const r = await fetch(`/api/reset?amount=${amt}`, { method: 'POST' });
    if (!r.ok) { alert('Reset failed: ' + (await r.text())); return; }
    _cd = 1;
    await refreshAll();
  } catch (e) { alert('Reset failed: ' + e.message); }
});

document.getElementById('btn-pause').addEventListener('click', async () => {
  try { await fetch('/api/pause', { method: 'POST' }); _cd = 1; await refreshStatus(); }
  catch (e) { console.error(e); }
});

document.getElementById('btn-resume').addEventListener('click', async () => {
  try { await fetch('/api/resume', { method: 'POST' }); _cd = 1; await refreshStatus(); }
  catch (e) { console.error(e); }
});

document.getElementById('btn-find-markets').addEventListener('click', async () => {
  const btn = document.getElementById('btn-find-markets');
  btn.disabled = true; btn.textContent = '…';
  try {
    const r = await fetch('/api/find-markets', { method: 'POST' });
    const data = await r.json();
    alert(data.ok ? `Found ${data.n_markets} markets. Restart bot to use them.` : `Error: ${data.error || data.stderr}`);
  } catch (e) { alert('Find markets failed: ' + e.message); }
  finally { btn.disabled = false; btn.textContent = 'FIND MARKETS'; }
});

const btnRetrain = document.getElementById('btn-retrain');
if (btnRetrain) {
  btnRetrain.addEventListener('click', async () => {
    btnRetrain.disabled = true;
    btnRetrain.textContent = 'Retraining…';
    try {
      const r = await fetch(_apiBase + '/api/retrain', { method: 'POST' });
      const j = await r.json();
      if (j.ok) {
        btnRetrain.textContent = '✓ Retrained';
        const el = document.getElementById('insight-learn');
        if (el) el.textContent = (el.textContent || '') + '\n\n[Model retrained. Brier: ' + (j.train_brier?.toFixed(4) ?? '—') + ']';
        refreshInsights();
      } else {
        btnRetrain.textContent = 'Retrain failed';
        alert(j.error || 'Retrain failed');
      }
    } catch (e) {
      btnRetrain.textContent = 'Retrain failed';
      alert(e.message || 'Request failed');
    }
    setTimeout(() => { btnRetrain.disabled = false; btnRetrain.textContent = '🔄 Retrain Model'; }, 3000);
  });
}

document.getElementById('btn-live-check').addEventListener('click', async () => {
  try {
    const d = await apiFetch('/live-check');
    const c = d.checks || {};
    const b = d.balances || {};
    let msg = (c.ready_for_live ? 'Ready for live' : 'Not ready') + '\n\n';
    msg += 'Checks: ' + JSON.stringify(c, null, 2) + '\n\nBalances: ' + JSON.stringify(b, null, 2);
    alert(msg.slice(0, 800));
  } catch (e) { alert('Live check failed: ' + e.message); }
});

// ── Page routing ──────────────────────────────────────────────────────────────

let _currentView = 'dashboard';

function showPage(view) {
  _currentView = view;
  document.querySelectorAll('.nav-btn').forEach(b => b.classList.toggle('active', b.dataset.view === view));
  document.querySelectorAll('.page').forEach(p => {
    p.classList.toggle('active', p.id === 'page-' + view);
  });
  if (view === 'trader') refreshTraderPage();
  if (view === 'strategies') refreshStrategiesPage();
}

document.querySelectorAll('.nav-btn').forEach(btn => {
  btn.addEventListener('click', () => showPage(btn.dataset.view));
});

document.querySelectorAll('.itab').forEach(btn => {
  btn.addEventListener('click', () => {
    const tab = btn.dataset.tab;
    document.querySelectorAll('.itab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.insight-pane').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    const pane = document.getElementById('pane-' + tab);
    if (pane) pane.classList.add('active');
    refreshInsights();
  });
});

// ── Master refresh ────────────────────────────────────────────────────────────

async function refreshDashboard() {
  try {
    const d = await apiFetch('/dashboard');
    applyDashboardData(d);
    refreshControls(d?.status, null);
  } catch (e) {
    console.warn('Dashboard fetch failed:', e.message);
    _showDataLoadError(e.message);
  }
}

function _showDataLoadError(msg) {
  const el = document.getElementById('activity-feed');
  if (el) {
    el.innerHTML = '<div class="empty" role="alert">Unable to load data. Ensure the server is running: <code>python web/run.py</code></div>';
  }
}

async function refreshAll() {
  _cd = 5;
  let d = null;
  try {
    d = await apiFetch('/dashboard');
    applyDashboardData(d);
  } catch (e) { console.warn('Dashboard fetch failed:', e.message); }
  await Promise.all([
    refreshTicker(),
    refreshCryptoPrices(),
    refreshParams(),
    refreshPnlChart(),
    // BTC chart: FeedManager handles live updates via WebSocket
    // Chart is TradingView only; refreshBtcChart is a no-op
  ]);
  refreshGrid();
  refreshPerformance();
  refreshFooterSentiment();
  refreshInsights();
  if (_currentView === 'trader') refreshTraderPage();
  if (_currentView === 'strategies') refreshStrategiesPage();
  try {
    const ms = await apiFetch('/markets-summary', { silent: true });
    refreshControls(d?.status, ms);
  } catch (_e) { refreshControls(d?.status, null); }
}

// ── Symbol & Timeframe (TradingView only) ──────────────────────────────────────

const TradingViewWidget = (() => {
  let _container = null;
  const SYMBOL_MAP = { btc: 'BINANCE:BTCUSDT', eth: 'BINANCE:ETHUSDT', sol: 'BINANCE:SOLUSDT' };
  const INTERVAL_MAP = { '1m': '1', '5m': '5', '15m': '15', '1h': '60' };

  function load(symbolKey, timeframeKey) {
    const symbol = SYMBOL_MAP[symbolKey] || SYMBOL_MAP.btc;
    const interval = INTERVAL_MAP[timeframeKey] || '1';
    _container = document.getElementById('tradingview-chart-wrap');
    if (!_container) return;
    _container.innerHTML = '';
    const div = document.createElement('div');
    div.className = 'tradingview-widget-container';
    div.style.cssText = 'height:100%;width:100%;min-height:200px;';
    div.innerHTML = '<div class="tradingview-widget-container__widget" style="height:calc(100% - 32px);width:100%"></div>';
    const script = document.createElement('script');
    script.src = 'https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js';
    script.type = 'text/javascript';
    script.async = true;
    script.textContent = JSON.stringify({
      autosize: true,
      symbol: symbol,
      interval: interval,
      timezone: 'Etc/UTC',
      theme: 'dark',
      style: '1',
      locale: 'en',
      backgroundColor: 'rgba(7, 11, 17, 1)',
      hide_top_toolbar: false,
      save_image: true,
      calendar: false,
      support_host: 'https://www.tradingview.com',
    });
    div.appendChild(script);
    _container.appendChild(div);
  }

  return { load };
})();

document.querySelectorAll('.sym-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    ChartState.switchSymbol(btn.dataset.sym);
    showPage('dashboard');
  });
});

document.querySelectorAll('.tf-btn').forEach(btn => {
  btn.addEventListener('click', () => ChartState.switchTimeframe(btn.dataset.tf));
});

// ── Init ──────────────────────────────────────────────────────────────────────

// ── Init ──────────────────────────────────────────────────────────────────────

TradingViewWidget.load('btc', '1m');
updateChartPriceBadge();

refreshAll();
setInterval(refreshAll,          8000);
setInterval(refreshDashboard,    2000);
setInterval(refreshTicker,       5000);
setInterval(refreshCryptoPrices, 15000);

let _rTimer;
window.addEventListener('resize', () => {
  clearTimeout(_rTimer);
  _rTimer = setTimeout(() => { if (_pnlChart) refreshPnlChart(); }, 200);
});

// ── Column resize splitter ────────────────────────────────────────────────────
(function initDashSplitter() {
  const splitter = document.getElementById('dash-splitter');
  const colLeft = document.getElementById('dash-charts-col');
  const colRight = document.getElementById('dash-orders-col');
  const grid = document.querySelector('.dash-grid');
  if (!splitter || !colLeft || !colRight || !grid) return;

  let startX = 0, startLeft = 0, startRight = 0;

  splitter.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    splitter.classList.add('dragging');
    document.body.classList.add('panel-resize-horizontal');
    startX = e.clientX;
    startLeft = colLeft.offsetWidth;
    startRight = colRight.offsetWidth;
    const onMove = (move) => {
      move.preventDefault();
      const dx = move.clientX - startX;
      const w = grid.offsetWidth;
      const left = Math.max(200, Math.min(w - 220, startLeft + dx));
      const right = w - left - (splitter.offsetWidth || 6);
      colLeft.style.flex = `1 1 ${left}px`;
      colLeft.style.maxWidth = 'none';
      colRight.style.flex = `1 1 ${right}px`;
      colRight.style.maxWidth = 'none';
    };
    const onUp = () => {
      splitter.classList.remove('dragging');
      document.body.classList.remove('panel-resize-horizontal');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      try {
        const leftBasis = parseInt(colLeft.style.flex?.replace(/.*\s(\d+)px/, '$1') || '0', 10);
        const rightBasis = colRight.offsetWidth;
        if (leftBasis >= 200 && rightBasis >= 220)
          localStorage.setItem('polyquant_dash_columns', JSON.stringify({ left: leftBasis, right: rightBasis }));
      } catch (_) {}
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  // Restore column widths
  try {
    const saved = JSON.parse(localStorage.getItem('polyquant_dash_columns') || 'null');
    if (saved && typeof saved.left === 'number' && saved.left >= 200 && saved.right >= 220) {
      colLeft.style.flex = `1 1 ${saved.left}px`;
      colLeft.style.maxWidth = 'none';
      colRight.style.flex = `1 1 ${saved.right}px`;
      colRight.style.maxWidth = 'none';
    }
  } catch (_) {}
})();

// ── Panel height resize (per-panel drag handle) ─────────────────────────────────
(function initPanelResize() {
  const STORAGE_KEY = 'polyquant_panel_';

  function applyPanelHeight(panel, heightPx) {
    if (!panel || heightPx == null) return;
    const min = parseInt(panel.getAttribute('data-resize-min'), 10) || 100;
    const max = parseInt(panel.getAttribute('data-resize-max'), 10) || 800;
    const px = Math.max(min, Math.min(max, heightPx));
    panel.style.height = px + 'px';
    panel.style.flex = '0 0 auto';
    panel.style.minHeight = px + 'px';
  }

  function persistPanelHeight(id, heightPx) {
    try { localStorage.setItem(STORAGE_KEY + id, String(heightPx)); } catch (_) {}
  }

  function loadPanelHeights() {
    document.querySelectorAll('.panel-resize-handle[data-resize]').forEach(handle => {
      const id = handle.getAttribute('data-resize');
      const panel = document.getElementById(id);
      if (!panel) return;
      try {
        const saved = localStorage.getItem(STORAGE_KEY + id);
        if (saved != null) applyPanelHeight(panel, parseInt(saved, 10));
      } catch (_) {}
    });
  }

  document.querySelectorAll('.panel-resize-handle[data-resize]').forEach(handle => {
    const id = handle.getAttribute('data-resize');
    const panel = document.getElementById(id);
    if (!panel) return;

      handle.addEventListener('mousedown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      handle.classList.add('dragging');
      document.body.classList.add('panel-resize-vertical');
      const startY = e.clientY;
      const startHeight = panel.offsetHeight;
      const min = parseInt(panel.getAttribute('data-resize-min'), 10) || 100;
      const max = parseInt(panel.getAttribute('data-resize-max'), 10) || 800;

      const onMove = (move) => {
        move.preventDefault();
        const dy = move.clientY - startY;
        const h = Math.max(min, Math.min(max, startHeight + dy));
        panel.style.height = h + 'px';
        panel.style.flex = '0 0 auto';
        panel.style.minHeight = h + 'px';
      };
      const onUp = () => {
        handle.classList.remove('dragging');
        document.body.classList.remove('panel-resize-vertical');
        document.removeEventListener('mousemove', onMove);
        document.removeEventListener('mouseup', onUp);
        persistPanelHeight(id, panel.offsetHeight);
      };
      document.addEventListener('mousemove', onMove);
      document.addEventListener('mouseup', onUp);
    });

    // Double-click: reset to default (clear saved height)
    handle.addEventListener('dblclick', (e) => {
      e.preventDefault();
      try { localStorage.removeItem(STORAGE_KEY + id); } catch (_) {}
      panel.style.height = '';
      panel.style.flex = '';
      panel.style.minHeight = '';
    });
  });

  loadPanelHeights();
})();

// ── Row splitter (equity | perf) ──────────────────────────────────────────────
(function initRowSplitter() {
  const splitter = document.getElementById('splitter-equity-perf');
  const row = document.getElementById('row-equity-perf');
  const panelEquity = document.getElementById('panel-equity');
  const panelPerf = document.getElementById('panel-perf');
  const STORAGE_KEY = 'polyquant_row_equity_perf';
  const MIN_LEFT = 140, MIN_RIGHT = 120;

  if (!splitter || !row || !panelEquity || !panelPerf) return;

  splitter.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    splitter.classList.add('dragging');
    document.body.classList.add('panel-resize-horizontal');
    const startX = e.clientX;
    const startLeft = panelEquity.offsetWidth;

    const onMove = (move) => {
      move.preventDefault();
      const dx = move.clientX - startX;
      const total = row.offsetWidth - splitter.offsetWidth;
      let left = Math.max(MIN_LEFT, Math.min(total - MIN_RIGHT, startLeft + dx));
      panelEquity.style.flex = `0 0 ${left}px`;
      panelEquity.style.minWidth = left + 'px';
      panelPerf.style.flex = '1 1 0';
      panelPerf.style.minWidth = MIN_RIGHT + 'px';
    };
    const onUp = () => {
      splitter.classList.remove('dragging');
      document.body.classList.remove('panel-resize-horizontal');
      document.removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseup', onUp);
      try { localStorage.setItem(STORAGE_KEY, String(panelEquity.offsetWidth)); } catch (_) {}
    };
    document.addEventListener('mousemove', onMove);
    document.addEventListener('mouseup', onUp);
  });

  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved != null) {
      const left = Math.max(MIN_LEFT, parseInt(saved, 10));
      panelEquity.style.flex = `0 0 ${left}px`;
      panelEquity.style.minWidth = left + 'px';
      panelPerf.style.flex = '1 1 0';
      panelPerf.style.minWidth = MIN_RIGHT + 'px';
    }
  } catch (_) {}
})();
