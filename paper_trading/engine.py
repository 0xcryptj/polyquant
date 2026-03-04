"""
Paper Trading Engine.

Orchestrates the full signal-to-execution loop in paper mode:
  1. Fetch latest BTC OHLCV from Binance
  2. Build feature vector
  3. Get model probability (or momentum heuristic if no model)
  4. Fetch Polymarket order book for active BTC 5-min market
  5. Evaluate EV signal
  6. Execute paper trade if signal fires
  7. Resolve open trades older than 5 minutes based on BTC price movement

State is persisted to SQLite so the bot survives restarts.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from config.settings import settings
from paper_trading import persistence as db
from paper_trading.utils import age_seconds
from control.kill_switch import KillSwitch
from models.ev_filter import evaluate_trade, TradeSignal

logger = logging.getLogger(__name__)

# How long to hold a 5-min position before resolving (BTC price movement)
TRADE_HOLD_SECONDS_5MIN = 5 * 60   # 5 minutes for 5-min markets
RESOLUTION_BUFFER_5MIN  = 30

# For real Polymarket markets: resolve mark-to-market after this hold time
TRADE_HOLD_SECONDS_REAL = 4 * 60 * 60  # 4 hours
# OR close early if price moves this much from entry
MARK_TO_MARKET_EXIT_THRESHOLD = 0.20   # 20% price move = early exit

STARTING_PAPER_BALANCE = 1_000.0   # USDC


class PaperEngine:
    """
    Full paper trading engine with persistent state.

    Usage:
        engine = PaperEngine()
        result = await engine.run_cycle()   # returns event dict or None
    """

    def __init__(self, starting_balance: float = STARTING_PAPER_BALANCE) -> None:
        db.init_db()

        # Restore or initialise balance — never overwrite with 1000 on restart
        saved = db.get_balance_full()
        if saved is None:
            db.set_balance(starting_balance, starting_balance)
            self.balance = starting_balance
            self.starting_balance = starting_balance
            logger.info("Initialized new session: balance=%.2f (bankroll baseline=%.2f)", self.balance, self.starting_balance)
        else:
            self.balance = saved["usdc"]
            self.starting_balance = saved["starting_usdc"]
            logger.info("Restored from DB: balance=%.2f, bankroll baseline=%.2f", self.balance, self.starting_balance)

        self.kill_switch = KillSwitch(
            starting_balance=self.balance,  # conservative: drawdown=0 on restart
            max_drawdown_pct=settings.max_daily_drawdown_pct,
        )

        self._pipeline: Any = None          # sklearn pipeline (loaded lazily)
        self._last_ohlcv: Any = None        # cached DataFrame
        self._market_config: list[dict]  = self._load_market_config_full()
        # Prefer 5-min markets only — fall back to SIM when none (ensures 5m UP/DOWN trading)
        self._active_token_ids: list[str] = [
            m["token_id"] for m in self._market_config
            if (m.get("market_type") or "").lower() == "5min"
        ]
        if self._market_config and not self._active_token_ids:
            logger.info(
                "Markets config has %d entries but no 5-min — using SIMULATION MODE for 5m UP/DOWN",
                len(self._market_config),
            )
        self._market_meta: dict[str, dict] = {m["token_id"]: m for m in self._market_config}

        # LLM reasoning engine (optional — requires ANTHROPIC_API_KEY)
        try:
            from models.llm_reasoner import LLMReasoner
            self._llm = LLMReasoner()
        except Exception as exc:
            logger.debug("LLM reasoner unavailable: %s", exc)
            self._llm = None

        logger.info(
            "PaperEngine ready | balance=%.2f | bankroll baseline=%.2f | markets=%d",
            self.balance, self.starting_balance, len(self._active_token_ids),
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def run_cycle(self) -> list[dict[str, Any]]:
        """
        Execute one full trading cycle (call this every ~45 s from the bot).

        Returns:
            List of event dicts describing what happened:
              {'type': 'trade_opened', ...}
              {'type': 'trade_resolved', ...}
              {'type': 'kill_switch', ...}
        """
        _cycle_start = time.monotonic()
        events: list[dict[str, Any]] = []

        if self.kill_switch.is_triggered():
            events.append({
                "type": "kill_switch",
                "reason": self.kill_switch.reason,
                "balance": self.balance,
            })
            return events

        # 1. Resolve old open trades first (frees up capital)
        events.extend(self._resolve_expired_trades())

        # 2. Fetch BTC data + build features
        ohlcv = self._fetch_ohlcv()
        if ohlcv is None or len(ohlcv) < 60:
            logger.warning("Insufficient OHLCV data — skipping cycle")
            return events

        features = self._build_features(ohlcv)
        if features is None:
            return events

        btc_price = float(ohlcv["close"].iloc[-1])

        # 3. Fetch sentiment + wallet signals (best-effort, non-blocking)
        sentiment = self._fetch_sentiment()
        sentiment_feats = self._build_sentiment_features(sentiment)
        if sentiment_feats:
            features.update(sentiment_feats)

        # 4. Get model probability (uses all features including sentiment)
        model_prob = self._get_model_prob(features)

        # 5. Evaluate signals for each active market (or simulate if none configured)
        import httpx
        open_count = len(db.get_open_trades())
        # More positions in SIM mode to accumulate data for learning
        max_open = 8 if not self._active_token_ids else 5

        if open_count >= max_open:
            logger.debug("Max open positions (%d) reached — skipping signal eval", max_open)
            return events

        if self._active_token_ids:
            # Real Polymarket markets available — use live order books
            with httpx.Client(timeout=6.0) as client:
                for token_id in self._active_token_ids:
                    if open_count >= max_open:
                        break
                    try:
                        whale_signal = self._get_whale_signal(token_id)
                        event = self._evaluate_and_trade(
                            token_id, model_prob, btc_price, features, client,
                            sentiment=sentiment, whale_signal=whale_signal,
                        )
                        if event:
                            events.append(event)
                            open_count += 1
                    except Exception as exc:
                        logger.error("Signal eval failed for %s: %s", token_id[:12], exc)
        else:
            # SIMULATION MODE — no real markets, synthesize a Polymarket-style bet
            # based on model probability vs a simulated market price
            sim_event = self._simulate_trade(model_prob, btc_price, features, sentiment)
            if sim_event:
                events.append(sim_event)

        elapsed = time.monotonic() - _cycle_start
        opened  = sum(1 for e in events if e.get("type") == "trade_opened")
        resolved = sum(1 for e in events if e.get("type") == "trade_resolved")
        logger.info(
            "Cycle done in %.1fs | opened=%d resolved=%d balance=%.2f open_pos=%d BTC=$%.0f",
            elapsed, opened, resolved, self.balance, open_count, btc_price,
        )
        return events

    def get_status(self) -> dict[str, Any]:
        """Return a status snapshot for Telegram /status command."""
        counts = db.get_trade_count()
        daily = db.get_daily_trade_stats()
        closed = db.get_all_closed_trades()
        all_time_pnl = sum(t["pnl"] for t in closed if t["pnl"] is not None)
        open_trades = db.get_open_trades()
        locked = sum(float(t.get("size_usdc") or 0) for t in open_trades)
        total_equity = self.balance + locked
        total_pnl = total_equity - self.starting_balance

        garch_vol: float | None = None
        garch_regime: str = "—"
        if self._last_ohlcv is not None and len(self._last_ohlcv) >= 60:
            try:
                import numpy as np
                close = self._last_ohlcv["close"].astype(float)
                log_ret = np.log(close / close.shift(1)).dropna()
                vol_1h = float(log_ret.tail(60).std() * (525_600 ** 0.5))  # annualized
                garch_vol = round(vol_1h, 3)
                garch_regime = "high" if vol_1h > 0.8 else "normal"
            except Exception:
                pass

        return {
            "balance": self.balance,
            "starting_balance": self.starting_balance,
            "locked_in_positions": locked,
            "total_equity": total_equity,
            "return_pct": 100 * (total_equity / self.starting_balance - 1) if self.starting_balance else 0,
            "total_pnl": total_pnl,
            "all_time_pnl": all_time_pnl,
            "daily_pnl": daily["daily_pnl"],
            "trades_today": daily["trades_today"],
            "garch_vol": garch_vol,
            "garch_regime": garch_regime,
            "n_open": counts["open"],
            "n_wins": counts["wins"],
            "n_losses": counts["losses"],
            "n_total": counts["total"],
            "win_rate": (counts["wins"] or 0) / max((counts["wins"] or 0) + (counts["losses"] or 0), 1),
            "kill_switch": self.kill_switch.is_triggered(),
            "kill_switch_reason": self.kill_switch.reason,
        }

    def get_open_positions(self) -> list[dict[str, Any]]:
        return db.get_open_trades()

    def get_open_positions_for_display(self) -> list[dict[str, Any]]:
        """Open positions still within their hold window (excludes expired-but-not-yet-resolved)."""
        now = datetime.now(timezone.utc)
        open_trades = db.get_open_trades()
        out = []
        for t in open_trades:
            age = age_seconds(t["opened_at"], now)
            is_sim = (t.get("token_id") or "").startswith("SIM-")
            hold = (TRADE_HOLD_SECONDS_5MIN + RESOLUTION_BUFFER_5MIN) if is_sim else TRADE_HOLD_SECONDS_REAL
            if age <= hold:
                out.append(t)
        return out

    def get_recent_trades(self, n: int = 10) -> list[dict[str, Any]]:
        return db.get_recent_trades(n)

    def reset_balance(self, new_balance: float = STARTING_PAPER_BALANCE) -> None:
        """Reset paper balance (used by /reset command). Persists both balance and bankroll baseline."""
        self.balance = new_balance
        self.starting_balance = new_balance
        db.set_balance(new_balance, new_balance)  # persist both so restart restores correctly
        self.kill_switch.reset(new_balance)
        logger.info("Paper balance reset to %.2f USDC (bankroll baseline updated)", new_balance)

    def get_adaptive_params(self) -> dict[str, float]:
        """Return the current adaptive trading parameters."""
        return {
            "min_edge": db.get_param("min_edge", settings.min_edge_threshold),
            "kelly_fraction": db.get_param("kelly_fraction", settings.kelly_fraction),
            "max_spread": db.get_param("max_spread", settings.max_spread),
        }

    # ── Private: Signal Evaluation ────────────────────────────────────────────

    def _get_effective_snap(self, token_id: str, client: Any) -> Any | None:
        """
        Get the best available price snapshot for a market.

        Tries the CLOB order book first. If the CLOB is illiquid (spread > 0.50),
        falls back to:
          1. Gamma AMM price stored in market metadata (yes_price field)
          2. CLOB last-trade price
        Returns None if no usable price can be determined.
        """
        from data.collector_polymarket import (
            get_order_book,
            get_last_trade_price,
            OrderBookSnapshot,
        )
        from datetime import datetime, timezone

        try:
            snap = get_order_book(token_id, client)
        except Exception as exc:
            logger.debug("Order book fetch failed for %s: %s", token_id[:12], exc)
            return None

        if snap.spread <= 0.50:
            return snap  # CLOB is liquid — use directly

        # CLOB is illiquid; look for a fallback price
        fallback_price: float | None = None

        # 1. Gamma AMM price stored in market config
        gamma_price = self._market_meta.get(token_id, {}).get("yes_price")
        if gamma_price is not None:
            fallback_price = float(gamma_price)

        # 2. CLOB last-trade price
        if fallback_price is None:
            try:
                fallback_price = get_last_trade_price(token_id, client)
            except Exception:
                pass

        if fallback_price is None:
            logger.debug(
                "Illiquid CLOB for %s (spread=%.3f) and no fallback price — skipping",
                token_id[:12], snap.spread,
            )
            return None

        fallback_price = max(0.02, min(0.98, fallback_price))
        synthetic_bid = max(0.01, fallback_price - 0.01)
        synthetic_ask = min(0.99, fallback_price + 0.01)

        logger.debug(
            "Illiquid CLOB for %s — using fallback price %.4f (synthetic bid=%.4f ask=%.4f)",
            token_id[:12], fallback_price, synthetic_bid, synthetic_ask,
        )

        return OrderBookSnapshot(
            token_id=token_id,
            timestamp=datetime.now(timezone.utc),
            best_bid=synthetic_bid,
            best_ask=synthetic_ask,
            mid_price=fallback_price,
            spread=synthetic_ask - synthetic_bid,
            bid_size=0.0,
            ask_size=0.0,
            raw={"synthetic": True, "gamma_price": fallback_price},
        )

    def _evaluate_and_trade(
        self,
        token_id: str,
        model_prob: float,
        btc_price: float,
        features: dict[str, float],
        client: Any,
        sentiment: Any = None,
        whale_signal: Any = None,
    ) -> dict[str, Any] | None:
        """
        Evaluate a single market and place a paper trade if edge is sufficient.
        Returns event dict or None.
        """
        snap = self._get_effective_snap(token_id, client)
        if snap is None:
            return None

        # LLM reasoning: blend stat model + Claude for the final probability
        llm_reasoning = ""
        final_prob, llm_reasoning = self._get_llm_prob(
            token_id, snap, btc_price, features, sentiment, whale_signal, model_prob
        )

        min_edge = db.get_param("min_edge", settings.min_edge_threshold)
        max_spread = db.get_param("max_spread", settings.max_spread)

        signal: TradeSignal = evaluate_trade(
            token_id=token_id,
            model_prob=final_prob,
            best_ask=snap.best_ask,
            best_bid=snap.best_bid,
            spread=snap.spread,
            min_edge=min_edge,
            max_spread=max_spread,
        )

        if not signal.should_trade:
            logger.debug("No signal for %s: %s", token_id[:12], signal.reason)
            return None

        # Fixed 5% of total equity per order
        total_equity = self.balance + sum(float(t.get("size_usdc") or 0) for t in db.get_open_trades())
        size_usdc = max(1.0, min(self.balance * 0.99, total_equity * 0.05))

        if size_usdc < 1.0:
            logger.debug("Position too small (%.2f USDC) — skipping", size_usdc)
            return None

        # Whale alignment veto — if whales strongly oppose, skip before any DB changes
        whale_alignment = "NEUTRAL"
        if whale_signal and whale_signal.consensus_strength > 0.7:
            if whale_signal.consensus_direction != signal.direction and whale_signal.consensus_direction != "NEUTRAL":
                logger.info(
                    "Skipping trade — whales disagree: whales=%s, signal=%s (strength=%.2f)",
                    whale_signal.consensus_direction, signal.direction, whale_signal.consensus_strength,
                )
                return None
            whale_alignment = f"{whale_signal.consensus_direction} (strength={whale_signal.consensus_strength:.2f})"

        # Execute paper trade (only after all veto checks pass)
        shares = size_usdc / signal.market_price
        order_id = f"PAPER-{int(time.time())}-{uuid.uuid4().hex[:6]}"

        new_balance = self.balance - size_usdc
        trade_id = db.insert_trade_and_set_balance(
            new_balance=new_balance,
            order_id=order_id,
            token_id=token_id,
            direction=signal.direction,
            entry_price=signal.market_price,
            size_usdc=size_usdc,
            shares=shares,
            model_prob=final_prob,  # decision prob for Brier/calibration metrics
            edge=signal.edge,
            btc_price_entry=btc_price,
            features=features,
        )
        self.balance = new_balance
        db.log_trade_open(
            trade_id=trade_id, order_id=order_id, token_id=token_id,
            direction=signal.direction, entry_price=signal.market_price,
            size_usdc=size_usdc, shares=shares, btc_price_entry=btc_price,
            model_prob=final_prob, edge=signal.edge, simulated=False,
        )

        # Sentiment context
        sentiment_score = None
        fg_value = None
        funding = None
        if sentiment:
            try:
                sentiment_score = sentiment.composite_score
                fg_value = sentiment.fear_greed.value if sentiment.fear_greed else None
                funding = sentiment.funding_rate.funding_rate if sentiment.funding_rate else None
            except Exception:
                pass

        logger.info(
            "PAPER TRADE OPENED #%d | %s %s | prob=%.3f | edge=%.4f | "
            "size=%.2f USDC | price=%.4f | BTC=$%.2f",
            trade_id, signal.direction, token_id[:12],
            final_prob, signal.edge, size_usdc, signal.market_price, btc_price,
        )

        return {
            "type": "trade_opened",
            "trade_id": trade_id,
            "order_id": order_id,
            "token_id": token_id,
            "direction": signal.direction,
            "size_usdc": size_usdc,
            "entry_price": signal.market_price,
            "model_prob": final_prob,
            "edge": signal.edge,
            "btc_price": btc_price,
            "balance": self.balance,
            "sentiment_score": sentiment_score,
            "fear_greed": fg_value,
            "funding_rate": funding,
            "whale_alignment": whale_alignment,
        }

    # ── Private: Trade Resolution ─────────────────────────────────────────────

    def _resolve_expired_trades(self) -> list[dict[str, Any]]:
        """
        Check all open trades and resolve them when their hold time expires.

        Two resolution modes:
          - SIM trades: resolve after 5 min using BTC price movement
          - Real trades: mark-to-market after 4h OR early exit if price moved ±20%
        """
        open_trades = db.get_open_trades()
        if not open_trades:
            return []

        now = datetime.now(timezone.utc)
        btc_now = self._get_current_btc_price()

        events = []
        for trade in open_trades:
            age = age_seconds(trade["opened_at"], now)
            token_id = trade["token_id"]
            is_sim = token_id.startswith("SIM-")

            if is_sim:
                # SIM: resolve after 5-min hold using BTC direction
                hold = TRADE_HOLD_SECONDS_5MIN + RESOLUTION_BUFFER_5MIN
                if age < hold:
                    continue
                if btc_now is not None:
                    event = self._resolve_btc_direction(trade, btc_now)
                else:
                    # No current BTC price — resolve at entry (PnL ≈ 0) so position doesn't stick forever
                    event = self._resolve_at_price(
                        trade,
                        exit_price=trade["entry_price"],
                        btc_exit=trade["btc_price_entry"],
                    )
            else:
                # Real: try mark-to-market first, fall back to BTC direction
                hold = TRADE_HOLD_SECONDS_REAL
                if age < hold:
                    # Check early exit via token price movement
                    event = self._check_early_exit(trade)
                else:
                    event = self._resolve_mark_to_market(trade, btc_now)

            if event:
                events.append(event)

        return events

    def _check_early_exit(self, trade: dict) -> dict[str, Any] | None:
        """
        Early exit based on intra-period price movement.

        Disabled for now: our current markets have illiquid CLOBs where order book
        mid-price (often a market maker sitting at 0.50) differs completely from the
        actual last-trade price used for entry. This causes spurious 1500% 'gains'
        seconds after entering. Resolution is handled cleanly by mark-to-market at 4h.
        """
        return None

    def _resolve_mark_to_market(self, trade: dict, btc_now: float | None) -> dict[str, Any] | None:
        """Resolve a real market position at current token price (mark-to-market)."""
        try:
            import httpx
            with httpx.Client(timeout=5.0) as client:
                snap = self._get_effective_snap(trade["token_id"], client)
            if snap is None:
                raise ValueError("No usable price snapshot")
            direction = trade["direction"]
            # Use bid (what we'd sell at) for YES, ask (what NO is worth) for NO
            exit_price = snap.best_bid if direction == "YES" else (1.0 - snap.best_ask)
            exit_price = max(0.01, min(0.99, exit_price))
            return self._resolve_at_price(trade, exit_price, btc_exit=btc_now)
        except Exception as exc:
            logger.warning("Mark-to-market failed for %s: %s — falling back to BTC direction",
                           trade["token_id"][:12], exc)
            if btc_now is not None:
                return self._resolve_btc_direction(trade, btc_now)
        return None

    def _resolve_btc_direction(self, trade: dict, btc_now: float) -> dict[str, Any] | None:
        """
        Resolve based on BTC price direction (5-min / SIM logic).
        Uses Polymarket-style payouts: $1 per share on win, $0 on loss.
        Fee (2%) is applied in _resolve_at_price on positive gains.
        """
        btc_entry = trade["btc_price_entry"]
        direction = trade["direction"]
        btc_moved_up = btc_now > btc_entry
        won = (direction == "YES" and btc_moved_up) or (direction == "NO" and not btc_moved_up)
        # Polymarket resolution: win = $1/share, lose = $0/share
        exit_price = 1.0 if won else 0.0
        return self._resolve_at_price(trade, exit_price, btc_exit=btc_now)

    def _resolve_at_price(
        self,
        trade: dict,
        exit_price: float,
        btc_exit: float | None,
    ) -> dict[str, Any] | None:
        """Core resolution: compute P&L and update DB."""
        direction = trade["direction"]
        shares = trade["shares"]
        size_usdc = trade["size_usdc"]
        entry_price = trade["entry_price"]
        fee = settings.POLYMARKET_FEE

        # P&L = (exit_price - entry_price) * shares - fee on wins
        price_delta = exit_price - entry_price
        raw_pnl = price_delta * shares
        won = exit_price > entry_price  # price went in our favour

        # Fee only on positive gains
        pnl = raw_pnl - (max(raw_pnl, 0) * fee)
        status = "won" if pnl > 0 else "lost"

        returned = size_usdc + pnl
        new_balance = self.balance + returned
        now_resolved = datetime.now(timezone.utc).isoformat()
        db.resolve_trade_and_set_balance(
            trade_id=trade["id"],
            btc_price_exit=btc_exit or trade["btc_price_entry"],
            exit_price=exit_price,
            pnl=pnl,
            status=status,
            new_balance=new_balance,
        )
        self.balance = new_balance
        self.kill_switch.update(self.balance)
        db.log_trade_resolve(
            trade_id=trade["id"], token_id=trade["token_id"],
            direction=direction, status=status,
            entry_price=entry_price, exit_price=exit_price, pnl=pnl,
            btc_price_entry=trade["btc_price_entry"],
            btc_price_exit=btc_exit or trade["btc_price_entry"],
            size_usdc=size_usdc, resolved_at=now_resolved,
        )

        logger.info(
            "TRADE RESOLVED #%d | %s | %s | entry=%.4f exit=%.4f | "
            "pnl=%+.2f | balance=%.2f",
            trade["id"], status.upper(), direction,
            entry_price, exit_price, pnl, self.balance,
        )

        return {
            "type": "trade_resolved",
            "trade_id": trade["id"],
            "token_id": trade["token_id"],
            "direction": direction,
            "won": won,
            "pnl": pnl,
            "btc_entry": trade["btc_price_entry"],
            "btc_exit": btc_exit,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "size_usdc": size_usdc,
            "balance": self.balance,
            "total_pnl": self.balance - self.starting_balance,
            "model_prob": trade["model_prob"],
            "edge": trade["edge"],
        }

    # ── Private: Data Fetching ────────────────────────────────────────────────

    def _fetch_ohlcv(self) -> Any | None:
        """Fetch recent BTC 1m candles from Binance."""
        try:
            from data.collector_binance import fetch_ohlcv
            df = fetch_ohlcv(limit=300)
            self._last_ohlcv = df
            return df
        except Exception as exc:
            logger.error("OHLCV fetch failed: %s", exc)
            return self._last_ohlcv  # return cached data if available

    def _get_current_btc_price(self) -> float | None:
        """Get latest BTC close price."""
        try:
            from data.collector_binance import fetch_ohlcv
            df = fetch_ohlcv(limit=2)
            return float(df["close"].iloc[-1])
        except Exception as exc:
            logger.warning("BTC price fetch failed: %s", exc)
            if self._last_ohlcv is not None:
                return float(self._last_ohlcv["close"].iloc[-1])
            return None

    def _build_features(self, ohlcv: Any) -> dict[str, float] | None:
        """Build feature dict from OHLCV DataFrame."""
        try:
            from features.feature_builder import build_features, FEATURE_COLUMNS
            feat_df = build_features(ohlcv)
            last = feat_df.dropna().iloc[-1]
            return {col: float(last[col]) for col in FEATURE_COLUMNS if col in last.index}
        except Exception as exc:
            logger.error("Feature building failed: %s", exc)
            return None

    def _get_model_prob(self, features: dict[str, float]) -> float:
        """
        Get P(YES) — probability BTC resolves in YES direction.

        Priority:
          1. Trained sklearn calibration model
          2. Momentum heuristic (fallback)
        Note: LLM reasoning is done per-market in _evaluate_and_trade
        """
        if self._pipeline is None:
            try:
                from models.calibration_model import load_model
                self._pipeline = load_model()
                logger.info("Calibration model loaded")
            except FileNotFoundError:
                pass

        if self._pipeline is not None:
            try:
                import pandas as pd
                from features.feature_builder import FEATURE_COLUMNS
                row = {col: features.get(col, 0.0) for col in FEATURE_COLUMNS}
                X = pd.DataFrame([row])[FEATURE_COLUMNS]
                prob = float(self._pipeline.predict_proba(X)[0, 1])
                return np.clip(prob, 0.05, 0.95)
            except Exception as exc:
                logger.warning("Model inference failed: %s — using heuristic", exc)

        # Heuristic: sigmoid of 5-min momentum, clamped to [0.35, 0.65]
        mom = features.get("mom_5m", 0.0)
        prob = 1.0 / (1.0 + np.exp(-50.0 * mom))
        return float(np.clip(prob, 0.35, 0.65))

    def invalidate_model_cache(self) -> None:
        """Clear cached calibration model so next inference loads the latest from disk."""
        self._pipeline = None

    def _get_llm_prob(
        self,
        token_id: str,
        snap: Any,
        btc_price: float,
        features: dict[str, float],
        sentiment: Any,
        whale_signal: Any,
        stat_prob: float,
    ) -> tuple[float, str]:
        """
        Call Claude LLM to analyze the market and return (probability, reasoning).
        Falls back to stat_prob if LLM unavailable.
        """
        if not self._llm or not self._llm.is_available:
            return stat_prob, ""

        market_meta = self._market_meta.get(token_id, {})

        context: dict[str, Any] = {
            "btc_price":       btc_price,
            "btc_change_5m":   features.get("mom_5m"),
            "btc_change_1h":   features.get("mom_1h"),      # feature key is mom_1h
            "rsi_14":          features.get("rsi_14"),
            "macd_hist":       features.get("macd_hist"),
            "volume_ratio":    features.get("vol_ratio"),    # feature key is vol_ratio
            "model_prob":      stat_prob,
            "market_question": market_meta.get("question", "Will BTC move in the predicted direction?"),
            "market_yes_price": snap.best_ask,
            "market_no_price": 1.0 - snap.best_bid,
            "market_spread":   snap.spread,
            "market_end_date": market_meta.get("end_date", ""),
        }

        if sentiment:
            try:
                context["fear_greed"] = sentiment.fear_greed.value if sentiment.fear_greed else None
                context["fear_greed_label"] = sentiment.fear_greed.label if sentiment.fear_greed else ""
                context["funding_rate"] = sentiment.funding_rate.funding_rate if sentiment.funding_rate else None
                context["oi_change_pct"] = sentiment.open_interest_change_pct
                context["headlines"] = [h.title for h in (sentiment.btc_headlines or [])[:5]]
            except Exception:
                pass

        if whale_signal:
            context["whale_direction"] = whale_signal.consensus_direction
            context["whale_strength"]  = whale_signal.consensus_strength

        reasoning_obj = self._llm.analyze(context)
        if reasoning_obj is None:
            return stat_prob, ""

        # Blend LLM and statistical prob (weighted average).
        # For macro/event markets, BTC momentum is irrelevant — weight LLM almost exclusively.
        # For BTC price markets, momentum adds some signal — use 60/40 blend.
        market_type = market_meta.get("market_type", "price")
        if market_type in ("macro", "event"):
            blended = 0.9 * reasoning_obj.prob_yes + 0.1 * stat_prob
        else:
            blended = 0.6 * reasoning_obj.prob_yes + 0.4 * stat_prob
        return float(np.clip(blended, 0.05, 0.95)), reasoning_obj.reasoning

    def _simulate_trade(
        self,
        model_prob: float,
        btc_price: float,
        features: dict[str, float],
        sentiment: Any,
    ) -> dict[str, Any] | None:
        """
        SIMULATION MODE: synthesise a virtual Polymarket-style trade when no
        real markets are configured.

        Uses a simulated market price derived from recent Polymarket-like pricing
        (approximately efficient, slight momentum premium for the model to exploit).
        The simulated "token" ID is a deterministic hash of the current 5-min window
        so each window gets at most one simulated trade.
        """
        import hashlib
        from datetime import datetime, timezone

        # Up to 2 simulated trades per 5-min window for more order flow
        now = datetime.now(timezone.utc)
        window = int(now.timestamp()) // 300   # 5-min epoch
        window_prefix = f"SIM-{window:x}"
        open_trades = db.get_open_trades()
        window_count = sum(1 for t in open_trades if (t.get("token_id") or "").startswith(window_prefix + "-") or t.get("token_id") == window_prefix)
        if window_count >= 2:
            return None
        sim_token = f"{window_prefix}-{window_count}"

        # Simulated market price: use BTC 5m momentum (mom_5m) for realistic tilt
        # Positive momentum -> Yes more expensive; negative -> No more expensive
        mom_5m = float(features.get("mom_5m", 0.0) or 0.0)
        momentum_tilt = float(np.clip(mom_5m * 3.0, -0.12, 0.12))
        import random
        rng = random.Random(window)
        market_noise = rng.uniform(-0.04, 0.04)
        base = 0.50 + momentum_tilt + market_noise
        sim_ask = max(0.35, min(0.65, base))
        sim_bid = sim_ask - 0.02   # 2-cent spread

        min_edge = db.get_param("min_edge", settings.min_edge_threshold)
        # In SIM, optionally accept slightly negative EV to ensure 5m order flow for learning
        min_edge_sim = min(min_edge, -0.02) if settings.sim_allow_negative_edge else min_edge
        max_spread = db.get_param("max_spread", settings.max_spread)

        from models.ev_filter import evaluate_trade
        signal = evaluate_trade(
            token_id=sim_token,
            model_prob=model_prob,
            best_ask=sim_ask,
            best_bid=sim_bid,
            min_edge=min_edge_sim,
            max_spread=max_spread,
        )

        if not signal.should_trade:
            logger.debug("SIM: no signal (prob=%.3f ask=%.3f edge=%.4f)",
                         model_prob, sim_ask, signal.edge)
            return None

        # Fixed 5% of total equity per order
        total_equity = self.balance + sum(float(t.get("size_usdc") or 0) for t in open_trades)
        size_usdc = max(1.0, min(self.balance * 0.99, total_equity * 0.05))

        if size_usdc < 1.0 or size_usdc > self.balance:
            return None

        shares = size_usdc / signal.market_price
        order_id = f"SIM-{sim_token}-{int(time.time())}"

        trade_id = db.insert_trade(
            order_id=order_id,
            token_id=sim_token,
            direction=signal.direction,
            entry_price=signal.market_price,
            size_usdc=size_usdc,
            shares=shares,
            model_prob=model_prob,
            edge=signal.edge,
            btc_price_entry=btc_price,
            features=features,
        )

        self.balance -= size_usdc
        db.set_balance(self.balance)
        db.log_trade_open(
            trade_id=trade_id, order_id=order_id, token_id=sim_token,
            direction=signal.direction, entry_price=signal.market_price,
            size_usdc=size_usdc, shares=shares, btc_price_entry=btc_price,
            model_prob=model_prob, edge=signal.edge, simulated=True,
        )

        # Sentiment context
        sentiment_score = None
        fg_value = None
        funding = None
        if sentiment:
            try:
                sentiment_score = sentiment.composite_score
                fg_value = sentiment.fear_greed.value if sentiment.fear_greed else None
                funding = sentiment.funding_rate.funding_rate if sentiment.funding_rate else None
            except Exception:
                pass

        logger.info(
            "SIM TRADE OPENED #%d | %s | prob=%.3f | edge=%.4f | "
            "size=%.2f | BTC=$%.2f [SIMULATION MODE]",
            trade_id, signal.direction, model_prob, signal.edge, size_usdc, btc_price,
        )

        return {
            "type": "trade_opened",
            "trade_id": trade_id,
            "order_id": order_id,
            "token_id": sim_token,
            "direction": signal.direction,
            "size_usdc": size_usdc,
            "entry_price": signal.market_price,
            "model_prob": model_prob,
            "edge": signal.edge,
            "btc_price": btc_price,
            "balance": self.balance,
            "sentiment_score": sentiment_score,
            "fear_greed": fg_value,
            "funding_rate": funding,
            "whale_alignment": "NEUTRAL",
            "simulated": True,
        }

    def _fetch_sentiment(self) -> Any | None:
        """Fetch all sentiment sources (best-effort, non-blocking)."""
        try:
            from data.sentiment_collector import fetch_all_sentiment
            return fetch_all_sentiment()
        except Exception as exc:
            logger.debug("Sentiment fetch failed (non-critical): %s", exc)
            return None

    def _build_sentiment_features(self, sentiment: Any | None) -> dict[str, float]:
        """Convert sentiment snapshot to features dict."""
        try:
            from features.sentiment_features import build_sentiment_features
            return build_sentiment_features(sentiment)
        except Exception as exc:
            logger.debug("Sentiment feature build failed: %s", exc)
            return {}

    def _get_whale_signal(self, token_id: str) -> Any | None:
        """Get whale consensus signal for a token (best-effort)."""
        try:
            from data.wallet_tracker import WalletTracker
            if not hasattr(self, "_wallet_tracker"):
                self._wallet_tracker = WalletTracker()
            if self._wallet_tracker._wallets:
                self._wallet_tracker.analyse_all()
                return self._wallet_tracker.get_consensus_signal(token_id)
        except Exception as exc:
            logger.debug("Wallet signal fetch failed (non-critical): %s", exc)
        return None

    def get_sentiment_snapshot(self) -> dict[str, Any]:
        """Return latest sentiment data for Telegram /sentiment command."""
        snap = self._fetch_sentiment()
        if snap is None:
            return {"available": False}
        result = {"available": True}
        if snap.fear_greed:
            result["fear_greed_value"] = snap.fear_greed.value
            result["fear_greed_label"] = snap.fear_greed.label
        if snap.funding_rate:
            result["funding_rate"] = snap.funding_rate.funding_rate
        result["oi_change_pct"] = snap.open_interest_change_pct
        result["composite_score"] = snap.composite_score
        result["n_headlines"] = len(snap.btc_headlines)
        result["headlines"] = [h.title for h in snap.btc_headlines[:3]]
        return result

    # ── Private: Config ───────────────────────────────────────────────────────

    def _load_market_config_full(self) -> list[dict]:
        """Load full BTC market metadata from config JSON."""
        config_path = settings.btc_markets_config_path
        if not config_path.exists():
            logger.warning(
                "No BTC markets config at %s — running in SIMULATION MODE. "
                "Run scripts/find_btc_markets.py to load real Polymarket data.",
                config_path,
            )
            return []
        try:
            with open(config_path) as f:
                data = json.load(f)
            if not isinstance(data, list) or not data:
                logger.info("Markets config is empty — SIMULATION MODE")
                return []

            markets = []
            for item in data:
                if isinstance(item, str):
                    markets.append({"token_id": item, "question": "", "market_type": "unknown"})
                elif isinstance(item, dict) and item.get("token_id"):
                    markets.append(item)

            logger.info("Loaded %d real Polymarket markets from config", len(markets))
            for m in markets[:5]:
                logger.info("  %s | %s", m.get("market_type","?"), m.get("question","")[:60])
            return markets
        except Exception as exc:
            logger.error("Failed to load markets config: %s", exc)
            return []

