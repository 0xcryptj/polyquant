"""
PolyQuant Web GUI — Trading terminal dashboard.

Standalone:  uvicorn web.app:app --reload --host 0.0.0.0 --port 8080
In-process:  from web.app import create_app; app = create_app(ctx)

Features:
  - Dashboard with bankroll, PnL, positions, trades
  - Charts (PnL over time, win rate)
  - Controls (paper/live mode, wallet, reset)
  - Polymarket market links
"""

from __future__ import annotations

import json
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if TYPE_CHECKING:
    from runtime.context import RuntimeContext

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── Runtime context (injected by create_app; None in standalone mode) ─────────
_ctx: "RuntimeContext | None" = None


def create_app(ctx: "RuntimeContext") -> FastAPI:
    """
    Factory for in-process use: wires the shared RuntimeContext into the
    FastAPI app so pause/resume endpoints use ctx.trading_active directly.
    Call this from WebService instead of importing the module-level `app`.
    """
    global _ctx
    _ctx = ctx
    return app


def _make_app() -> FastAPI:
    a = FastAPI(title="PolyQuant Terminal", version="2.0.0")
    a.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    static_dir = PROJECT_ROOT / "web" / "static"
    static_dir.mkdir(parents=True, exist_ok=True)
    a.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    return a


app = _make_app()


def _load_engine_status() -> dict | None:
    """Load status from engine if available (bot running)."""
    try:
        from paper_trading.engine import PaperEngine
        engine = PaperEngine()
        return engine.get_status()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).debug("Engine not available: %s", exc)
        return None


def _load_status_from_db() -> dict:
    """Load status from DB when engine not available."""
    from paper_trading import persistence as db
    db.init_db()
    bal = db.get_balance_full()
    if not bal:
        return {
            "balance": 1000.0,
            "starting_balance": 1000.0,
            "locked_in_positions": 0.0,
            "total_equity": 1000.0,
            "return_pct": 0.0,
            "total_pnl": 0.0,
            "all_time_pnl": 0.0,
            "n_open": 0,
            "n_wins": 0,
            "n_losses": 0,
            "n_total": 0,
            "win_rate": 0.0,
            "kill_switch": False,
            "kill_switch_reason": "",
        }
    counts = db.get_trade_count()
    closed = db.get_all_closed_trades()
    all_time_pnl = sum(t["pnl"] for t in closed if t.get("pnl") is not None)
    balance = bal["usdc"]
    starting = bal["starting_usdc"]
    open_trades = db.get_open_trades()
    locked = sum(float(t.get("size_usdc") or 0) for t in open_trades)
    total_equity = balance + locked
    total_pnl = total_equity - starting
    return_pct = 100 * (total_equity / starting - 1) if starting else 0
    win_rate = (counts["wins"] or 0) / max((counts["wins"] or 0) + (counts["losses"] or 0), 1)
    daily = db.get_daily_trade_stats()
    return {
        "balance": balance,
        "starting_balance": starting,
        "locked_in_positions": locked,
        "total_equity": total_equity,
        "return_pct": return_pct,
        "total_pnl": total_pnl,
        "all_time_pnl": all_time_pnl,
        "n_open": counts["open"],
        "n_wins": counts["wins"],
        "n_losses": counts["losses"],
        "n_total": counts["total"],
        "win_rate": win_rate,
        "kill_switch": False,
        "kill_switch_reason": "",
        "daily_pnl": daily.get("daily_pnl", 0.0),
        "trades_today": daily.get("trades_today", 0),
    }


def _slug_from_title(title: str) -> str:
    """Derive Polymarket event slug from event title."""
    import re
    s = (title or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s[:80] if s else ""


def _slug_for_5m_from_end_date(end_date_str: str) -> str | None:
    """Build Polymarket 5m slug btc-updown-5m-{window_start_unix} from end_date (resolution time)."""
    from datetime import datetime, timezone, timedelta
    end_date_str = (end_date_str or "").strip()
    if not end_date_str:
        return None
    try:
        # Accept ISO-style 2026-03-02T07:35:00Z or 2026-03-02T07:35:00.000Z
        s = end_date_str.replace("Z", "+00:00")[:25]
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        window_start = dt - timedelta(minutes=5)
        return f"btc-updown-5m-{int(window_start.timestamp())}"
    except (ValueError, TypeError):
        return None


def _market_label(market_type: str) -> str:
    """Display type: U/D (BTC up/down), Y/N (Yes/No), Price, SIM, ?."""
    t = (market_type or "").strip().lower()
    if t == "5min":
        return "U/D"
    if t in ("event", "macro"):
        return "Y/N"
    if t == "price":
        return "Price"
    return "?"


def _polymarket_url(token_id: str, meta: dict | None = None) -> str:
    """Build Polymarket market URL. Prefer event slug (e.g. btc-updown-5m-1772436600 for 5m)."""
    if token_id and not token_id.startswith("SIM-"):
        meta = meta or {}
        slug = meta.get("event_slug")
        if not slug and (meta.get("market_type") or "").lower() == "5min" and meta.get("end_date"):
            slug = _slug_for_5m_from_end_date(meta["end_date"])
        if not slug:
            slug = _slug_from_title(meta.get("event_title") or meta.get("question", ""))
        if slug:
            return f"https://polymarket.com/event/{slug}"
        return f"https://polymarket.com/?tid={token_id}"
    return f"https://polymarket.com/?tid={token_id}" if token_id else "https://polymarket.com"


def _display_label_for_trade(r: dict, meta: dict) -> tuple[str, str]:
    """
    Return (question_display, market_type) for positions/trades.
    All 5-min and SIM trades show as 'Polymarket BTC 5m UP' / 'Polymarket BTC 5m DOWN'.
    """
    token_id = (r.get("token_id") or "").strip()
    direction = (r.get("direction") or "YES").upper()
    dir_short = "UP" if direction == "YES" else "DOWN"
    btc_5m_label = f"Polymarket BTC 5m {dir_short}"

    if token_id.startswith("SIM-"):
        return btc_5m_label, "5min"
    if (meta.get("market_type") or "").lower() == "5min":
        return btc_5m_label, "5min"
    # When meta is empty (legacy trades), treat as 5m directional
    q = meta.get("question") or meta.get("event_title")
    if not q:
        return btc_5m_label, "5min"
    return q[:80] + ("…" if len(q) > 80 else ""), meta.get("market_type", "price")


def _load_markets_index() -> dict[str, dict]:
    """Load btc_markets.json once and index by token_id. Use per-request for dashboard."""
    try:
        cfg_path = PROJECT_ROOT / "config" / "btc_markets.json"
        with open(cfg_path) as f:
            markets = json.load(f)
        idx = {}
        for m in markets:
            tid = m.get("token_id")
            if not tid:
                continue
            out = dict(m)
            if (out.get("market_type") or "").lower() == "5min":
                if not (out.get("event_slug") or "").startswith("btc-updown-5m-") and out.get("end_date"):
                    slug_5m = _slug_for_5m_from_end_date(out["end_date"])
                    if slug_5m:
                        out["event_slug"] = slug_5m
            if not out.get("event_slug") and (out.get("event_title") or out.get("question")):
                out["event_slug"] = _slug_from_title(out.get("event_title") or out.get("question", ""))
            idx[tid] = out
        return idx
    except Exception:
        return {}


def _get_market_meta(token_id: str, markets_index: dict[str, dict] | None = None) -> dict:
    """Get market metadata. Pass markets_index to avoid repeated file reads."""
    if markets_index is not None:
        return markets_index.get(token_id or "", {})
    try:
        cfg_path = PROJECT_ROOT / "config" / "btc_markets.json"
        with open(cfg_path) as f:
            markets = json.load(f)
        for m in markets:
            if m.get("token_id") == token_id:
                out = dict(m)
                if (out.get("market_type") or "").lower() == "5min":
                    if not (out.get("event_slug") or "").startswith("btc-updown-5m-") and out.get("end_date"):
                        slug_5m = _slug_for_5m_from_end_date(out["end_date"])
                        if slug_5m:
                            out["event_slug"] = slug_5m
                if "event_slug" not in out or not out.get("event_slug"):
                    if out.get("event_title") or out.get("question"):
                        out["event_slug"] = _slug_from_title(
                            out.get("event_title") or out.get("question", "")
                        )
                return out
        return {}
    except Exception:
        return {}


def _to_win(shares: float, entry_price: float, direction: str) -> float:
    """Potential profit if position wins. YES: shares * (1 - price), NO: shares * price."""
    if shares <= 0 or entry_price <= 0:
        return 0.0
    direction = (direction or "").upper()
    if direction == "YES":
        return shares * (1.0 - entry_price) if entry_price < 1 else 0.0
    if direction == "NO":
        return shares * entry_price if entry_price <= 1 else 0.0
    return 0.0


def _shares_from_row(r: dict) -> float:
    """Get shares; compute from size_usdc/entry_price if shares is 0."""
    shares = float(r.get("shares") or 0)
    if shares > 0:
        return shares
    size = float(r.get("size_usdc") or 0)
    entry = float(r.get("entry_price") or 0)
    if size > 0 and entry > 0:
        return size / entry
    return 0.0


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the dashboard."""
    html_path = static_dir / "index.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(html_path)


@app.get("/api/status")
async def api_status():
    """Get current status (balance, PnL, counts). Always uses DB for fast response."""
    s = _load_status_from_db()
    from config.settings import settings
    s.setdefault("paper_trading", settings.paper_trading)
    if _ctx is not None:
        s["trading_paused"] = not _ctx.trading_active.is_set()
    else:
        ov = _read_mode_override()
        s["trading_paused"] = ov.get("trading_paused", False)
    return s


@app.get("/api/positions")
async def api_positions():
    """Get open positions with Polymarket-style fields + current value (mark-to-market)."""
    from paper_trading import persistence as db
    db.init_db()
    rows = db.get_open_trades()
    positions = []
    for r in rows:
        meta = _get_market_meta(r["token_id"])
        shares = _shares_from_row(r)
        entry = float(r.get("entry_price") or 0)
        direction = (r.get("direction") or "").upper()
        to_win = round(_to_win(shares, entry, direction), 2)
        # Mark-to-market: fetch current Polymarket price for value
        current_price = None
        token_id = r.get("token_id") or ""
        if token_id and not token_id.startswith("SIM-"):
            try:
                from data.collector_polymarket import get_order_book
                snap = get_order_book(token_id)
                current_price = snap.mid_price
            except Exception:
                try:
                    from data.collector_polymarket import get_last_trade_price
                    current_price = get_last_trade_price(token_id)
                except Exception:
                    pass
        current_value = None
        if current_price is not None and shares > 0:
            if direction == "YES":
                current_value = round(shares * current_price, 2)
            else:
                current_value = round(shares * (1.0 - current_price), 2)
        q_display, mtype = _display_label_for_trade(r, meta)
        pos = {
            **dict(r),
            "question": q_display,
            "market_type": mtype,
            "market_label": _market_label(mtype),
            "event_title": meta.get("event_title", ""),
            "event_slug": meta.get("event_slug", ""),
            "condition_id": meta.get("condition_id"),
            "polymarket_url": _polymarket_url(token_id, meta),
            "shares": round(shares, 4),
            "to_win": to_win,
            "current_price": round(current_price, 4) if current_price is not None else None,
            "current_value": current_value,
        }
        positions.append(pos)
    return positions


@app.get("/api/activity")
async def api_activity(limit: int = 30):
    """Recent activity: open positions + recent closed trades. Fast, DB-only (no Polymarket fetches)."""
    from paper_trading import persistence as db
    db.init_db()
    opens = db.get_open_trades()
    closed = db.get_recent_trades(max(limit, 15))
    items = []
    for r in opens:
        meta = _get_market_meta(r["token_id"])
        q_display, _ = _display_label_for_trade(r, meta)
        shares = _shares_from_row(r)
        entry = float(r.get("entry_price") or 0)
        direction = (r.get("direction") or "YES").upper()
        to_win = round(_to_win(shares, entry, direction), 2)
        items.append({
            "type": "open",
            "id": r.get("id"),
            "direction": r.get("direction", "YES"),
            "market": q_display,
            "size_usdc": float(r.get("size_usdc") or 0),
            "to_win": to_win,
            "opened_at": r.get("opened_at"),
            "btc_entry": r.get("btc_price_entry"),
        })
    for r in closed:
        meta = _get_market_meta(r["token_id"])
        q_display, _ = _display_label_for_trade(r, meta)
        pnl = r.get("pnl") or 0
        status = (r.get("status") or "lost").lower()
        if status == "lost" and pnl >= 0:
            pnl = -abs(float(r.get("size_usdc") or 0))
        items.append({
            "type": "closed",
            "id": r.get("id"),
            "direction": r.get("direction", "YES"),
            "market": q_display,
            "size_usdc": float(r.get("size_usdc") or 0),
            "pnl": pnl,
            "status": status,
            "resolved_at": r.get("resolved_at"),
        })
    # Sort: opens first (by opened_at desc), then closed (by resolved_at desc)
    def _ts(it):
        s = it.get("opened_at") or it.get("resolved_at") or ""
        return s
    items.sort(key=_ts, reverse=True)
    return items[:limit]


@app.get("/api/dashboard")
async def api_dashboard():
    """Single fast endpoint: status + activity + positions + trades. Batched DB + cached markets."""
    from paper_trading import persistence as db
    db.init_db()
    data = db.get_dashboard_data(n_closed=25)
    markets = _load_markets_index()

    status = {
        "balance": data["balance"],
        "starting_balance": data["starting_balance"],
        "locked_in_positions": data["locked_in_positions"],
        "total_equity": data["total_equity"],
        "return_pct": data["return_pct"],
        "total_pnl": data["total_pnl"],
        "all_time_pnl": data["all_time_pnl"],
        "n_open": data["n_open"],
        "n_wins": data["n_wins"],
        "n_losses": data["n_losses"],
        "n_total": data["n_total"],
        "win_rate": data["win_rate"],
        "daily_pnl": data["daily_pnl"],
        "trades_today": data["trades_today"],
        "kill_switch": False,
        "kill_switch_reason": "",
    }
    from config.settings import settings
    status.setdefault("paper_trading", settings.paper_trading)
    if _ctx is not None:
        status["trading_paused"] = not _ctx.trading_active.is_set()
    else:
        ov = _read_mode_override()
        status["trading_paused"] = ov.get("trading_paused", False)

    p = db.get_all_params()
    status["params"] = {
        "min_edge": p.get("min_edge", settings.min_edge_threshold),
        "kelly_fraction": p.get("kelly_fraction", settings.kelly_fraction),
    }

    opens, closed = data["opens"], data["closed"]
    activity = []
    for r in opens:
        meta = _get_market_meta(r.get("token_id") or "", markets)
        q_display, _ = _display_label_for_trade(r, meta)
        shares = _shares_from_row(r)
        entry = float(r.get("entry_price") or 0)
        direction = (r.get("direction") or "YES").upper()
        to_win = round(_to_win(shares, entry, direction), 2)
        activity.append({
            "type": "open", "id": r.get("id"), "direction": r.get("direction", "YES"),
            "market": q_display, "size_usdc": float(r.get("size_usdc") or 0),
            "to_win": to_win, "opened_at": r.get("opened_at"), "btc_entry": r.get("btc_price_entry"),
        })
    for r in closed:
        meta = _get_market_meta(r.get("token_id") or "", markets)
        q_display, _ = _display_label_for_trade(r, meta)
        pnl = r.get("pnl") or 0
        status_val = (r.get("status") or "lost").lower()
        if status_val == "lost" and pnl >= 0:
            pnl = -abs(float(r.get("size_usdc") or 0))
        activity.append({
            "type": "closed", "id": r.get("id"), "direction": r.get("direction", "YES"),
            "market": q_display, "size_usdc": float(r.get("size_usdc") or 0),
            "pnl": pnl, "status": status_val, "resolved_at": r.get("resolved_at"),
        })
    activity.sort(key=lambda x: x.get("opened_at") or x.get("resolved_at") or "", reverse=True)

    positions = []
    for r in opens:
        meta = _get_market_meta(r.get("token_id") or "", markets)
        shares = _shares_from_row(r)
        entry = float(r.get("entry_price") or 0)
        direction = (r.get("direction") or "").upper()
        to_win = round(_to_win(shares, entry, direction), 2)
        q_display, mtype = _display_label_for_trade(r, meta)
        token_id = r.get("token_id") or ""
        positions.append({
            **dict(r), "question": q_display, "market_type": mtype,
            "shares": round(shares, 4), "to_win": to_win,
            "current_value": None, "polymarket_url": _polymarket_url(token_id, meta),
        })

    trades = []
    for r in closed:
        meta = _get_market_meta(r.get("token_id") or "", markets)
        q_display, mtype = _display_label_for_trade(r, meta)
        shares = _shares_from_row(r)
        entry = float(r.get("entry_price") or 0)
        direction = r.get("direction", "")
        trades.append({
            **dict(r), "question": q_display, "market_type": mtype,
            "shares": round(shares, 4), "to_win": round(_to_win(shares, entry, direction), 2),
            "polymarket_url": _polymarket_url(r.get("token_id") or "", meta),
        })

    return {"status": status, "activity": activity, "positions": positions, "trades": trades}


@app.get("/api/trades")
async def api_trades(limit: int = 50):
    """Get recent closed trades with Polymarket-style fields."""
    try:
        n = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        n = 50
    from paper_trading import persistence as db
    db.init_db()
    rows = db.get_recent_trades(n)
    trades = []
    for r in rows:
        meta = _get_market_meta(r["token_id"])
        q_display, mtype = _display_label_for_trade(r, meta)
        shares = _shares_from_row(r)
        entry = float(r.get("entry_price") or 0)
        direction = r.get("direction", "")
        trades.append({
            **dict(r),
            "question": q_display,
            "market_type": mtype,
            "market_label": _market_label(mtype),
            "event_title": meta.get("event_title", ""),
            "event_slug": meta.get("event_slug", ""),
            "condition_id": meta.get("condition_id"),
            "polymarket_url": _polymarket_url(r["token_id"], meta),
            "shares": round(shares, 4),
            "to_win": round(_to_win(shares, entry, direction), 2),
        })
    return trades


@app.get("/api/pnl-history")
async def api_pnl_history():
    """Get PnL history for charts (cumulative PnL per trade)."""
    from paper_trading import persistence as db
    db.init_db()
    closed = db.get_all_closed_trades()
    bal = db.get_balance_full()
    starting = bal["starting_usdc"] if bal else 1000.0
    cum = starting
    series = [{"trade": 0, "timestamp": None, "cumulative": starting, "pnl": 0}]
    for i, t in enumerate(closed):
        pnl = t.get("pnl") or 0
        cum += pnl
        ts = t.get("resolved_at")
        series.append({
            "trade": i + 1,
            "timestamp": ts,
            "cumulative": cum,
            "pnl": pnl,
        })
    return series


@app.get("/api/params")
async def api_params():
    """Get trading parameters."""
    from config.settings import settings
    from paper_trading import persistence as db
    db.init_db()
    p = db.get_all_params()
    return {
        "min_edge": p.get("min_edge", settings.min_edge_threshold),
        "kelly_fraction": p.get("kelly_fraction", settings.kelly_fraction),
        "max_spread": p.get("max_spread", settings.max_spread),
        "max_position_usdc": settings.max_position_usdc,
        "position_pct": 5,
        "paper_trading": settings.paper_trading,
    }


class ModeUpdate(BaseModel):
    paper_trading: bool


def _read_mode_override() -> dict:
    """Read mode override file (paper_trading, trading_paused)."""
    override_path = PROJECT_ROOT / "config" / "mode_override.json"
    if not override_path.exists():
        return {"paper_trading": True, "trading_paused": False}
    try:
        with open(override_path) as f:
            data = json.load(f)
        return {
            "paper_trading": data.get("paper_trading", True),
            "trading_paused": data.get("trading_paused", False),
        }
    except Exception:
        return {"paper_trading": True, "trading_paused": False}


def _write_mode_override(updates: dict) -> None:
    """Merge updates into mode_override.json."""
    override_path = PROJECT_ROOT / "config" / "mode_override.json"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_mode_override()
    current.update(updates)
    with open(override_path, "w") as f:
        json.dump(current, f, indent=2)


@app.post("/api/mode")
async def api_set_mode(body: ModeUpdate):
    """Request mode change (paper/live). Requires restart to apply."""
    _write_mode_override({"paper_trading": body.paper_trading})
    return {"ok": True, "message": "Mode update saved. Restart the bot to apply."}


@app.post("/api/pause")
async def api_pause():
    """Pause trading loop."""
    if _ctx is not None:
        _ctx.trading_active.clear()
    else:
        _write_mode_override({"trading_paused": True})
    return {"ok": True, "trading_paused": True}


@app.post("/api/resume")
async def api_resume():
    """Resume trading loop."""
    if _ctx is not None:
        _ctx.trading_active.set()
    else:
        _write_mode_override({"trading_paused": False})
    return {"ok": True, "trading_paused": False}


@app.get("/api/trading-state")
async def api_trading_state():
    """Get current trading state (paused, paper_trading)."""
    if _ctx is not None:
        from config.settings import settings
        return {
            "paper_trading": settings.paper_trading,
            "trading_paused": not _ctx.trading_active.is_set(),
        }
    return _read_mode_override()


@app.post("/api/reset")
async def api_reset_balance(amount: float = 1000.0):
    """Reset paper balance (requires engine). Amount must be between 100 and 1,000,000."""
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid amount")
    if amt != amt:  # NaN
        raise HTTPException(status_code=400, detail="Invalid amount")
    if amt < 100 or amt > 1_000_000:
        raise HTTPException(status_code=400, detail="Amount must be between 100 and 1,000,000")
    try:
        from paper_trading.engine import PaperEngine
        engine = PaperEngine()
        engine.reset_balance(amt)
        return {"ok": True, "balance": amt}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/markets")
async def api_markets():
    """Get configured markets with Polymarket links."""
    try:
        cfg_path = PROJECT_ROOT / "config" / "btc_markets.json"
        with open(cfg_path) as f:
            markets = json.load(f)
        for m in markets:
            tid = m.get("token_id", "")
            m["polymarket_url"] = _polymarket_url(tid, m) if tid else ""
        return markets
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/markets-summary")
async def api_markets_summary():
    """Summary of configured markets (5min vs price vs event) for data verification."""
    try:
        cfg_path = PROJECT_ROOT / "config" / "btc_markets.json"
        if not cfg_path.exists():
            return {"has_5min": False, "total": 0, "by_type": {}, "notice": "No config. Run scripts/find_btc_markets.py"}
        with open(cfg_path) as f:
            markets = json.load(f)
        by_type: dict[str, int] = {}
        has_5min = False
        for m in markets:
            if not isinstance(m, dict) or not m.get("token_id"):
                continue
            t = m.get("market_type", "unknown") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
            if t == "5min":
                has_5min = True
        notice = None
        if not has_5min:
            if not by_type:
                notice = "SIM mode — no 5-min Polymarket markets. Use FIND MARKETS to discover."
            else:
                notice = "No 5-min markets. Use FIND MARKETS (5min-only)."
        return {"has_5min": has_5min, "total": len(markets), "by_type": by_type, "notice": notice}
    except Exception as e:
        return {"has_5min": False, "total": 0, "by_type": {}, "notice": str(e)}


@app.get("/api/trade-grid")
async def api_trade_grid():
    """Closed trades for grid visualization: [{id, pnl, status}, ...] oldest first."""
    from paper_trading import persistence as db
    db.init_db()
    rows = db.get_all_closed_trades()
    return [
        {"id": r["id"], "pnl": r.get("pnl") or 0, "status": r.get("status", "won"), "size_usdc": r.get("size_usdc")}
        for r in rows
    ]


@app.get("/api/btc-chart")
async def api_btc_chart(limit: int = 120):
    """Live BTC chart: Coinbase Exchange → ccxt (Binance/Kraken) → CoinGecko (optional key)."""
    try:
        n = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        n = 120

    def _to_candles(df):
        return [
            {"t": int(ts.timestamp() * 1000), "o": float(row["open"]),
             "h": float(row["high"]), "l": float(row["low"]), "c": float(row["close"])}
            for ts, row in df.tail(n).iterrows()
        ]

    # 1) Coinbase Exchange (public, no geo-block)
    try:
        from data.collector_coinbase import fetch_ohlcv as coinbase_fetch
        df = coinbase_fetch(product_id="BTC-USD", granularity=60, limit=n)
        if df is not None and not df.empty:
            return _to_candles(df)
    except Exception:
        pass

    # 2) ccxt: Binance or Kraken
    try:
        from data.collector_binance import fetch_ohlcv
        df = fetch_ohlcv(symbol="BTC/USDT", timeframe="1m", limit=n)
        if df is not None and not df.empty:
            return _to_candles(df)
    except Exception:
        pass

    # 3) CoinGecko (optional API key from settings)
    try:
        from config.settings import settings
        import httpx
        base = (settings.coingecko_base_url or "https://api.coingecko.com").rstrip("/")
        url = f"{base}/api/v3/coins/bitcoin/market_chart"
        params = {"vs_currency": "usd", "days": "1"}
        headers = {}
        if settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = settings.coingecko_api_key
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(url, params=params, headers=headers or None)
            r.raise_for_status()
            data = r.json()
        prices = data.get("prices", [])
        if not prices:
            return []
        out = []
        for p in prices[-n:]:
            t, v = p[0], float(p[1])
            out.append({"t": t, "o": v, "h": v, "l": v, "c": v})
        return out
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Ticker & Performance endpoints ────────────────────────────────────────────

_ticker_cache: dict = {}
_ticker_cache_ts: float = 0.0
_TICKER_TTL = 12.0   # seconds


@app.get("/api/ticker")
async def api_ticker():
    """
    Live BTC price, 1-hour change, Fear & Greed, and funding rate for the ticker bar.
    Cached for 30 s so rapid dashboard refreshes don't hammer external APIs.
    """
    global _ticker_cache, _ticker_cache_ts
    now = _time.monotonic()
    if _ticker_cache and now - _ticker_cache_ts < _TICKER_TTL:
        return _ticker_cache

    result: dict = {
        "btc_price": None,
        "btc_change_1h_pct": None,
        "fear_greed": None,
        "fear_greed_label": None,
        "funding_rate": None,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    import asyncio

    async def _get_btc() -> None:
        # Try Coinbase first (public, no geo-block)
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(
                    "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                    params={"granularity": 60, "limit": 62},
                )
                r.raise_for_status()
                rows = r.json()  # [[time, low, high, open, close, volume], ...]
                if rows and len(rows) >= 2:
                    rows_sorted = sorted(rows, key=lambda x: x[0])
                    result["btc_price"] = float(rows_sorted[-1][4])
                    if len(rows_sorted) >= 62:
                        result["btc_change_1h_pct"] = round(
                            (rows_sorted[-1][4] / rows_sorted[-62][4] - 1) * 100, 2
                        )
                    return
        except Exception:
            pass
        # Fallback: ccxt via thread
        try:
            def _ccxt_fetch():
                from data.collector_binance import fetch_ohlcv
                df = fetch_ohlcv(limit=62)
                return df
            df = await asyncio.to_thread(_ccxt_fetch)
            if df is not None and not df.empty:
                result["btc_price"] = float(df["close"].iloc[-1])
                if len(df) >= 62:
                    result["btc_change_1h_pct"] = round(
                        (float(df["close"].iloc[-1]) / float(df["close"].iloc[-62]) - 1) * 100, 2
                    )
        except Exception:
            pass

    async def _get_sentiment() -> None:
        try:
            def _fetch():
                from data.sentiment_collector import fetch_all_sentiment
                return fetch_all_sentiment()
            snap = await asyncio.to_thread(_fetch)
            if snap and snap.fear_greed:
                result["fear_greed"] = snap.fear_greed.value
                result["fear_greed_label"] = snap.fear_greed.label
            if snap and snap.funding_rate:
                result["funding_rate"] = snap.funding_rate.funding_rate
        except Exception:
            pass

    await asyncio.gather(_get_btc(), _get_sentiment())

    _ticker_cache = result
    _ticker_cache_ts = _time.monotonic()
    return result


@app.get("/api/sentiment")
async def api_sentiment():
    """Full sentiment snapshot: Fear & Greed, funding, OI change, headlines."""
    try:
        from paper_trading.engine import PaperEngine
        engine = PaperEngine()
        snap = engine.get_sentiment_snapshot()
        return snap
    except Exception as e:
        return {"available": False, "error": str(e)}


@app.get("/api/learn")
async def api_learn():
    """Adaptive learning report (same as Telegram /learn)."""
    try:
        from paper_trading.learner import Learner
        learner = Learner()
        return {"report": learner.build_report()}
    except Exception as e:
        return {"report": "", "error": str(e)}


@app.get("/api/wallets")
async def api_wallets():
    """Whale wallet analysis report."""
    try:
        from data.wallet_tracker import WalletTracker
        tracker = WalletTracker()
        return {"report": tracker.build_report()}
    except Exception as e:
        return {"report": f"Wallet report unavailable: {e}", "error": str(e)}


@app.get("/api/live-check")
async def api_live_check():
    """Live trading readiness check (same as Telegram /live_check)."""
    try:
        from wallets.agentkit_base import live_trading_readiness_check, get_all_balances
        checks = live_trading_readiness_check()
        balances = get_all_balances()
        return {"checks": checks, "balances": balances}
    except Exception as e:
        return {"checks": None, "balances": {"error": str(e)}, "error": str(e)}


_crypto_prices_cache: dict = {}
_crypto_prices_ts: float = 0.0
_CRYPTO_TTL = 8.0


@app.get("/api/crypto-prices")
async def api_crypto_prices():
    """Live prices for BTC, ETH, SOL. CoinGecko first, Binance fallback."""
    global _crypto_prices_cache, _crypto_prices_ts
    now = _time.monotonic()
    if _crypto_prices_cache and now - _crypto_prices_ts < _CRYPTO_TTL:
        return _crypto_prices_cache
    result = {"btc": None, "eth": None, "sol": None}
    import httpx
    # 1) CoinGecko (free, no key for basic)
    try:
        async with httpx.AsyncClient(timeout=6.0) as c:
            r = await c.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "bitcoin,ethereum,solana", "vs_currencies": "usd", "include_24hr_change": "true"},
            )
            r.raise_for_status()
            data = r.json()
            for key, coin_id in [("btc", "bitcoin"), ("eth", "ethereum"), ("sol", "solana")]:
                if coin_id in data:
                    result[key] = {
                        "price": data[coin_id].get("usd"),
                        "change_24h": data[coin_id].get("usd_24h_change"),
                    }
            if any(result[k] and result[k].get("price") for k in result):
                _crypto_prices_cache = result
                _crypto_prices_ts = now
                return result
    except Exception:
        pass
    # 2) Binance fallback
    try:
        async with httpx.AsyncClient(timeout=5.0) as c:
            for sym, key in [("BTCUSDT", "btc"), ("ETHUSDT", "eth"), ("SOLUSDT", "sol")]:
                r = await c.get(f"https://api.binance.com/api/v3/ticker/24hr", params={"symbol": sym})
                if r.status_code == 200:
                    d = r.json()
                    result[key] = {
                        "price": float(d.get("lastPrice", 0)),
                        "change_24h": float(d.get("priceChangePercent", 0)),
                    }
    except Exception:
        pass
    _crypto_prices_cache = result
    _crypto_prices_ts = now
    return result


@app.post("/api/find-markets")
async def api_find_markets():
    """Run find_btc_markets script (same as Telegram /find_markets)."""
    import subprocess
    import sys
    try:
        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "find_btc_markets.py"), "--5min-only"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=90,
        )
        cfg_path = PROJECT_ROOT / "config" / "btc_markets.json"
        n = 0
        if cfg_path.exists():
            with open(cfg_path) as f:
                data = json.load(f)
            n = len(data) if isinstance(data, list) else 0
        return {
            "ok": result.returncode == 0,
            "n_markets": n,
            "stdout": (result.stdout or "")[-500:],
            "stderr": (result.stderr or "")[-300:],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/performance")
async def api_performance():
    """Key performance metrics: Sharpe, Brier score, max drawdown, avg edge."""
    from paper_trading import persistence as db
    import numpy as np
    db.init_db()
    closed = db.get_all_closed_trades()
    if not closed:
        return {"n": 0, "sharpe": 0, "brier": 0.25, "max_dd": 0, "avg_edge": 0, "avg_pnl": 0}

    pnls = [t["pnl"] for t in closed if t.get("pnl") is not None]
    probs = [(t["model_prob"], 1.0 if t["status"] == "won" else 0.0)
             for t in closed if t.get("model_prob") is not None]

    brier = float(np.mean([(p - a) ** 2 for p, a in probs])) if probs else 0.25
    avg_pnl = float(np.mean(pnls)) if pnls else 0.0
    avg_edge = float(np.mean([t["edge"] for t in closed if t.get("edge") is not None])) if closed else 0.0

    sharpe = 0.0
    if len(pnls) >= 2:
        arr = np.array(pnls)
        std = float(np.std(arr))
        if std > 0:
            sharpe = round(float(np.mean(arr)) / std, 3)

    # Max drawdown
    max_dd = 0.0
    running_pnl, peak = 0.0, 0.0
    for pnl in pnls:
        running_pnl += pnl
        if running_pnl > peak:
            peak = running_pnl
        if peak > 0:
            dd = (peak - running_pnl) / peak
            max_dd = max(max_dd, dd)

    return {
        "n": len(closed),
        "sharpe": round(sharpe, 3),
        "brier": round(brier, 4),
        "max_dd": round(max_dd, 4),
        "avg_edge": round(avg_edge, 4),
        "avg_pnl": round(avg_pnl, 4),
    }


@app.get("/api/health")
async def api_health():
    """Runtime health: service states, uptime, last error. Requires in-process mode."""
    if _ctx is None:
        return {"mode": "standalone", "healthy": True}
    from runtime.context import BOT_VERSION
    return {
        "mode": "orchestrated",
        "version": BOT_VERSION,
        "healthy": True,
        "uptime": _ctx.uptime_str,
        "uptime_seconds": round(_ctx.uptime_seconds),
        "trading_active": _ctx.trading_active.is_set(),
        "last_error": _ctx.last_error or None,
    }
