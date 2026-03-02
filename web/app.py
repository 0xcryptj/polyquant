"""
PolyQuant Web GUI — Trading terminal dashboard.

Run: uvicorn web.app:app --reload --host 0.0.0.0 --port 8080

Features:
  - Dashboard with bankroll, PnL, positions, trades
  - Charts (PnL over time, win rate)
  - Controls (paper/live mode, wallet, reset)
  - Polymarket market links
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent

app = FastAPI(title="PolyQuant Terminal", version="1.0.0")

# Mount static files
static_dir = PROJECT_ROOT / "web" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


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


def _get_market_meta(token_id: str) -> dict:
    """Get market metadata (question, condition_id, event_slug, etc.) from config."""
    try:
        cfg_path = PROJECT_ROOT / "config" / "btc_markets.json"
        with open(cfg_path) as f:
            markets = json.load(f)
        for m in markets:
            if m.get("token_id") == token_id:
                out = dict(m)
                # For 5m markets, prefer btc-updown-5m-{window_start_unix} from config or end_date
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
    """Get current status (balance, PnL, counts)."""
    s = _load_engine_status()
    if s is None:
        s = _load_status_from_db()
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
        pos = {
            **dict(r),
            "question": meta.get("question") or f"Unknown market ({token_id[:12]}…)",
            "market_type": meta.get("market_type", ""),
            "market_label": _market_label(meta.get("market_type", "")),
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
        shares = _shares_from_row(r)
        entry = float(r.get("entry_price") or 0)
        direction = r.get("direction", "")
        trades.append({
            **dict(r),
            "question": meta.get("question") or f"Unknown market ({r.get('token_id', '')[:12]}…)",
            "market_type": meta.get("market_type", ""),
            "market_label": _market_label(meta.get("market_type", "")),
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
        "paper_trading": settings.paper_trading,
    }


class ModeUpdate(BaseModel):
    paper_trading: bool


@app.post("/api/mode")
async def api_set_mode(body: ModeUpdate):
    """Request mode change (paper/live). Requires restart to apply."""
    # Write to override file - bot would need to check this
    override_path = PROJECT_ROOT / "config" / "mode_override.json"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    with open(override_path, "w") as f:
        json.dump({"paper_trading": body.paper_trading}, f)
    return {"ok": True, "message": "Mode update saved. Restart the bot to apply."}


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
        if not has_5min and by_type:
            notice = "Configured markets are not 5-min. Run: python scripts/find_btc_markets.py"
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
        {"id": r["id"], "pnl": r.get("pnl") or 0, "status": r.get("status", "won")}
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
        if getattr(settings, "coingecko_api_key", None) and settings.coingecko_api_key:
            headers["x-cg-demo-api-key"] = settings.coingecko_api_key
        async with httpx.AsyncClient(timeout=15.0) as c:
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
