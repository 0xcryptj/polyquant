"""
Polymarket Whale Wallet Tracker.

Fetches open positions and historical trades for watched wallets
using the Polymarket CLOB and Gamma APIs. Identifies:
  - Which markets whales currently hold
  - Direction (YES/NO) and size
  - Historical win rate and avg edge
  - Detectable strategy patterns (scalper, conviction, news-driven, etc.)

Usage:
    tracker = WalletTracker()
    snapshot = tracker.analyse_all()
    signal = tracker.get_consensus_signal(token_id)

Data is cached in SQLite to avoid hammering the API.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dateutil import parser as dateutil_parser
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import httpx

from config.settings import settings, PROJECT_ROOT

logger = logging.getLogger(__name__)

# Paths relative to project root (robust when cwd differs)
WALLET_FILE = PROJECT_ROOT / "wallets_to_watch.txt"
CACHE_DB    = PROJECT_ROOT / "paper_trading" / "paper_trades.db"
GAMMA_BASE  = "https://gamma-api.polymarket.com"
CLOB_BASE   = settings.polymarket_clob_host

# Cache TTL: don't re-fetch wallet data more than once per 5 minutes
CACHE_TTL_SECONDS = 300


def _format_market_title(question: str, end_date_iso: str) -> str:
    """Build display title like 'Bitcoin Up or Down - March 3, 10:20PM-10:25PM ET'."""
    if not question and not end_date_iso:
        return "Unknown market"
    try:
        if end_date_iso:
            dt = dateutil_parser.parse(end_date_iso)
            if dt.tzinfo:
                try:
                    from zoneinfo import ZoneInfo
                    dt = dt.astimezone(ZoneInfo("America/New_York"))
                except Exception:
                    pass
            date_str = dt.strftime("%b %d, %I:%M%p").lstrip("0").replace("  ", " ")
            if "ET" not in date_str and "UTC" not in date_str:
                date_str += " ET"
            return f"{question.strip()} - {date_str}" if question.strip() else date_str
    except Exception:
        pass
    return question.strip() or "Unknown market"


def position_to_card(p: WalletPosition) -> dict:
    """Convert WalletPosition to web GUI card dict (market icon, title, direction, price, shares, PnL)."""
    direction_short = "Up" if p.direction == "YES" else "Down"
    price_cents = round(p.current_price * 100, 1)
    shares = p.size_usdc / p.entry_price if p.entry_price else 0
    position_usd = p.size_usdc + p.unrealized_pnl  # current value
    pnl_pct = (p.unrealized_pnl / p.size_usdc * 100) if p.size_usdc else 0
    return {
        "market_icon": "BTC",  # could derive from market_question / slug later
        "market_title": _format_market_title(p.market_question, p.end_date_iso),
        "direction": p.direction,
        "direction_short": direction_short,
        "price_cents": price_cents,
        "shares": round(shares, 1),
        "current_cents": price_cents,
        "max_cents": 100,
        "position_usd": round(position_usd, 2),
        "cost_usd": round(p.size_usdc, 2),
        "pnl_usd": round(p.unrealized_pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
    }


# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class WalletPosition:
    wallet: str
    label: str
    token_id: str
    direction: str       # YES or NO
    size_usdc: float
    entry_price: float
    current_price: float
    unrealized_pnl: float
    market_question: str = ""
    end_date_iso: str = ""   # for "March 3, 10:20PM-10:25PM ET" style title


@dataclass
class WalletStats:
    wallet: str
    label: str
    n_trades: int
    win_rate: float
    total_pnl: float
    avg_trade_size: float
    strategy_type: str            # SCALPER | CONVICTION | ARBITRAGE | NEWS_DRIVEN | UNKNOWN
    last_trade_at: datetime | None = None
    is_stale: bool = False        # True if no activity in 7 days
    top_markets: list[str] = field(default_factory=list)
    current_positions: list[WalletPosition] = field(default_factory=list)


@dataclass
class WalletSignal:
    """Aggregated signal from whale wallet activity on a specific market."""
    token_id: str
    n_whales_long: int       # whales holding YES
    n_whales_short: int      # whales holding NO
    total_whale_usdc_yes: float
    total_whale_usdc_no: float
    consensus_direction: str    # YES | NO | NEUTRAL
    consensus_strength: float   # 0..1
    whale_labels: list[str] = field(default_factory=list)


# ── Wallet File Parser ────────────────────────────────────────────────────────

def load_wallets() -> list[tuple[str, str, str]]:
    """
    Load wallets from wallets_to_watch.txt.

    Returns:
        List of (address, label, notes) tuples.
    """
    if not WALLET_FILE.exists():
        logger.warning("No wallet watch file at %s", WALLET_FILE)
        return []

    wallets = []
    with open(WALLET_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 1:
                continue
            address = parts[0].lower()
            label   = parts[1] if len(parts) > 1 else address[:10]
            notes   = parts[2] if len(parts) > 2 else ""
            if address.startswith("0x") and len(address) == 42:
                wallets.append((address, label, notes))
    logger.info("Loaded %d wallets to watch", len(wallets))
    return wallets


# ── API Fetchers ──────────────────────────────────────────────────────────────

def _fetch_wallet_positions(wallet: str, client: httpx.Client) -> list[dict]:
    """Fetch open positions for a wallet from Gamma API."""
    try:
        resp = client.get(
            f"{GAMMA_BASE}/positions",
            params={"user": wallet, "sizeThreshold": "0.01"},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("positions", [])
    except Exception as exc:
        logger.debug("Positions fetch failed for %s: %s", wallet[:10], exc)
        return []


def _fetch_wallet_trades(wallet: str, client: httpx.Client, limit: int = 100) -> list[dict]:
    """Fetch recent trade history for a wallet from CLOB API."""
    try:
        resp = client.get(
            f"{CLOB_BASE}/data/trades",
            params={"maker_address": wallet, "limit": limit},
            timeout=15.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("data", [])
    except Exception as exc:
        logger.debug("Trades fetch failed for %s: %s", wallet[:10], exc)
        return []


def _fetch_market_info(token_id: str, client: httpx.Client) -> dict:
    """Fetch market metadata (question, endDate) from Gamma."""
    try:
        resp = client.get(
            f"{GAMMA_BASE}/markets",
            params={"clob_token_ids": token_id},
            timeout=10.0,
        )
        resp.raise_for_status()
        markets = resp.json()
        if markets:
            return markets[0] if isinstance(markets, list) else markets
    except Exception as exc:
        logger.debug("Market info fetch failed for %s: %s", token_id[:12], exc)
    return {}


# ── Analytics ─────────────────────────────────────────────────────────────────

STALE_DAYS = 7   # label wallet inactive for this many days as STALE


def _compute_wallet_stats(
    wallet: str,
    label: str,
    trades: list[dict],
    positions: list[dict],
) -> WalletStats:
    """Compute performance stats and staleness from raw trade/position data."""
    n_trades = len(trades)
    wins = sum(1 for t in trades if _is_win(t))
    win_rate = wins / n_trades if n_trades else 0.0

    sizes = [float(t.get("size", 0)) for t in trades if t.get("size")]
    avg_size = sum(sizes) / len(sizes) if sizes else 0.0

    pnls = [float(t.get("profit", 0)) for t in trades if t.get("profit") is not None]
    total_pnl = sum(pnls)

    strategy = _detect_strategy(trades, positions)

    # Last trade timestamp
    last_trade_at: datetime | None = None
    for t in trades:
        ts_raw = t.get("created_at") or t.get("timestamp") or t.get("matched_time")
        if ts_raw:
            try:
                ts = dateutil_parser.parse(str(ts_raw))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if last_trade_at is None or ts > last_trade_at:
                    last_trade_at = ts
            except Exception:
                pass

    # Staleness check
    now = datetime.now(timezone.utc)
    is_stale = (
        n_trades == 0
        or (last_trade_at is not None and (now - last_trade_at).days >= STALE_DAYS)
        or (last_trade_at is None and n_trades == 0)
    )

    # Top markets by trade count
    market_counts: dict[str, int] = {}
    for t in trades:
        mid = t.get("market", t.get("condition_id", ""))
        if mid:
            market_counts[mid] = market_counts.get(mid, 0) + 1
    top_markets = sorted(market_counts, key=market_counts.get, reverse=True)[:5]  # type: ignore

    return WalletStats(
        wallet=wallet,
        label=label,
        n_trades=n_trades,
        win_rate=win_rate,
        total_pnl=total_pnl,
        avg_trade_size=avg_size,
        strategy_type=strategy,
        last_trade_at=last_trade_at,
        is_stale=is_stale,
        top_markets=top_markets,
    )


def _detect_strategy(trades: list[dict], positions: list[dict]) -> str:
    """Heuristically classify trading strategy."""
    if not trades:
        return "UNKNOWN"

    sizes = [float(t.get("size", 0)) for t in trades if t.get("size")]
    avg_size = sum(sizes) / len(sizes) if sizes else 0

    # High frequency + small sizes → SCALPER
    if len(trades) > 50 and avg_size < 50:
        return "SCALPER"

    # Few trades + large sizes → CONVICTION
    if len(trades) < 15 and avg_size > 500:
        return "CONVICTION"

    # Many different markets → ARBITRAGE
    markets = {t.get("market", "") for t in trades}
    if len(markets) > len(trades) * 0.7:
        return "ARBITRAGE"

    # Many open positions in same direction → DIRECTIONAL
    if positions:
        directions = [p.get("side", "") for p in positions]
        yes_pct = directions.count("YES") / len(directions)
        if yes_pct > 0.8 or yes_pct < 0.2:
            return "DIRECTIONAL"

    return "MIXED"


def _is_win(trade: dict) -> bool:
    """Determine if a trade was profitable."""
    profit = trade.get("profit")
    if profit is not None:
        return float(profit) > 0
    # Fallback: price-based
    outcome = trade.get("outcome", "").lower()
    return outcome in ("yes", "win", "1")


# ── Main Tracker Class ────────────────────────────────────────────────────────

class WalletTracker:
    """
    Tracks whale wallets on Polymarket, generates signals, and builds reports.
    """

    def __init__(self) -> None:
        self._wallets = load_wallets()
        self._stats_cache: dict[str, WalletStats] = {}
        self._last_refresh: float = 0.0
        self._init_db()

    def analyse_all(self, force: bool = False) -> list[WalletStats]:
        """
        Fetch and analyse all watched wallets.

        Returns cached results if fetched recently (within CACHE_TTL_SECONDS).
        """
        if not force and time.time() - self._last_refresh < CACHE_TTL_SECONDS:
            return list(self._stats_cache.values())

        if not self._wallets:
            logger.info("No wallets configured in wallets_to_watch.txt")
            return []

        results: list[WalletStats] = []
        with httpx.Client(timeout=15.0) as client:
            for address, label, _ in self._wallets:
                try:
                    trades    = _fetch_wallet_trades(address, client)
                    positions = _fetch_wallet_positions(address, client)
                    stats     = _compute_wallet_stats(address, label, trades, positions)

                    # Attach current positions with market context
                    stats.current_positions = self._enrich_positions(
                        address, label, positions, client
                    )

                    self._stats_cache[address] = stats
                    self._save_stats(stats)
                    results.append(stats)

                    # Rate limiting (runs in thread pool, time.sleep is safe here)
                    time.sleep(0.3)

                except Exception as exc:
                    logger.error("Failed to analyse wallet %s: %s", label, exc)

        self._last_refresh = time.time()
        logger.info("Analysed %d wallets", len(results))
        return results

    def get_consensus_signal(self, token_id: str) -> WalletSignal:
        """
        Aggregate whale positions on a specific token into a consensus signal.

        Returns a WalletSignal indicating whether whales are collectively
        bullish (YES) or bearish (NO) on the market.
        """
        yes_wallets: list[str] = []
        no_wallets: list[str] = []
        yes_usdc = 0.0
        no_usdc = 0.0

        for stats in self._stats_cache.values():
            for pos in stats.current_positions:
                if pos.token_id != token_id:
                    continue
                if pos.direction == "YES":
                    yes_wallets.append(stats.label)
                    yes_usdc += pos.size_usdc
                else:
                    no_wallets.append(stats.label)
                    no_usdc += pos.size_usdc

        total = yes_usdc + no_usdc
        if total == 0:
            return WalletSignal(
                token_id=token_id,
                n_whales_long=0, n_whales_short=0,
                total_whale_usdc_yes=0.0, total_whale_usdc_no=0.0,
                consensus_direction="NEUTRAL", consensus_strength=0.0,
            )

        yes_frac = yes_usdc / total
        if yes_frac > 0.6:
            direction = "YES"
            strength = yes_frac
        elif yes_frac < 0.4:
            direction = "NO"
            strength = 1.0 - yes_frac
        else:
            direction = "NEUTRAL"
            strength = abs(yes_frac - 0.5) * 2

        return WalletSignal(
            token_id=token_id,
            n_whales_long=len(yes_wallets),
            n_whales_short=len(no_wallets),
            total_whale_usdc_yes=yes_usdc,
            total_whale_usdc_no=no_usdc,
            consensus_direction=direction,
            consensus_strength=float(strength),
            whale_labels=yes_wallets + no_wallets,
        )

    def build_report(self) -> str:
        """Build a rich text report for the Telegram /wallets command."""
        if not self._wallets:
            return (
                "🐋 *Whale Wallet Tracker*\n\n"
                "No wallets configured yet.\n\n"
                "Add wallet addresses to `wallets_to_watch.txt`:\n"
                "`0xADDRESS | LABEL | notes`\n\n"
                "Find top traders at polymarket.com/leaderboard\n"
                "Sort by All-Time profit → click trader → copy wallet from URL"
            )

        stats_list = self.analyse_all()
        if not stats_list:
            return "No wallet data available yet. Try again in a moment."

        active  = [s for s in stats_list if not s.is_stale]
        stale   = [s for s in stats_list if s.is_stale]

        lines = [f"🐋 *Whale Wallet Intelligence* ({len(stats_list)} wallets)\n"]

        if active:
            lines.append(f"✅ *Active Wallets ({len(active)})*\n")
            for s in sorted(active, key=lambda x: x.total_pnl, reverse=True):
                pnl_str = f"+${s.total_pnl:.0f}" if s.total_pnl >= 0 else f"-${abs(s.total_pnl):.0f}"
                last_str = ""
                if s.last_trade_at:
                    days_ago = (datetime.now(timezone.utc) - s.last_trade_at).days
                    last_str = f" | Last trade: {days_ago}d ago"
                lines.append(
                    f"*{s.label}* — `{s.strategy_type}`\n"
                    f"  WR: `{s.win_rate:.0%}` | PnL: `{pnl_str}` | "
                    f"Trades: {s.n_trades}{last_str}\n"
                    f"  Avg Size: `${s.avg_trade_size:.0f}`"
                )
                if s.current_positions:
                    pos_summary = ", ".join(
                        f"{p.direction} ${p.size_usdc:.0f}" for p in s.current_positions[:3]
                    )
                    lines.append(f"  📋 Open: {pos_summary}")
                lines.append("")

        if stale:
            lines.append(f"\n⚠️ *Stale Wallets (inactive >{STALE_DAYS}d)* ({len(stale)})\n")
            for s in stale:
                last_str = "never seen" if s.last_trade_at is None else \
                    f"{(datetime.now(timezone.utc) - s.last_trade_at).days}d ago"
                lines.append(
                    f"💤 *{s.label}* — {s.n_trades} trades, last: {last_str}\n"
                    f"  WR: `{s.win_rate:.0%}` | PnL: `${s.total_pnl:+.0f}`"
                )

        return "\n".join(lines)

    # ── Private ───────────────────────────────────────────────────────────────

    def _enrich_positions(
        self,
        wallet: str,
        label: str,
        positions: list[dict],
        client: httpx.Client,
    ) -> list[WalletPosition]:
        enriched = []
        for pos in positions[:15]:  # cap to avoid rate limits
            try:
                token_id  = pos.get("asset_id", pos.get("token_id", ""))
                if not token_id:
                    continue
                direction = pos.get("side", pos.get("outcome", "YES")).upper()
                # size can be in size, size_usdc, or cash_balance (value in USDC)
                size      = float(pos.get("size", pos.get("size_usdc", pos.get("cash_balance", 0))))
                avg_price = float(pos.get("avg_price", pos.get("entry_price", pos.get("avgPrice", 0.5))))
                cur_price = float(pos.get("cur_price", pos.get("current_price", pos.get("curPrice", avg_price))))
                unreal    = (cur_price - avg_price) * size if direction == "YES" else (avg_price - cur_price) * size

                market_question = ""
                end_date_iso    = ""
                meta = _fetch_market_info(token_id, client)
                if meta:
                    market_question = meta.get("question", meta.get("title", "")) or ""
                    end_date_iso    = meta.get("end_date_iso", meta.get("endDate", meta.get("end_date", ""))) or ""

                enriched.append(WalletPosition(
                    wallet=wallet,
                    label=label,
                    token_id=token_id,
                    direction=direction,
                    size_usdc=size,
                    entry_price=avg_price,
                    current_price=cur_price,
                    unrealized_pnl=unreal,
                    market_question=market_question,
                    end_date_iso=end_date_iso,
                ))
            except Exception as exc:
                logger.debug("Failed to parse position: %s", exc)
        return enriched

    def _init_db(self) -> None:
        """Add wallet tracking tables to the shared DB."""
        CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(CACHE_DB))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS wallet_snapshots (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_at  TEXT    NOT NULL,
                wallet       TEXT    NOT NULL,
                label        TEXT,
                n_trades     INTEGER,
                win_rate     REAL,
                total_pnl    REAL,
                avg_size     REAL,
                strategy     TEXT
            );
        """)
        conn.commit()
        conn.close()

    def _save_stats(self, stats: WalletStats) -> None:
        conn = sqlite3.connect(str(CACHE_DB))
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO wallet_snapshots
               (snapshot_at, wallet, label, n_trades, win_rate, total_pnl, avg_size, strategy)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (now, stats.wallet, stats.label, stats.n_trades, stats.win_rate,
             stats.total_pnl, stats.avg_trade_size, stats.strategy_type),
        )
        conn.commit()
        conn.close()
