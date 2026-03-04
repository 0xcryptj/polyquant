"""
Adaptive Learning System.

Analyses closed trade history to:
  1. Measure real performance (win rate, Brier score, Sharpe)
  2. Identify which market conditions yield positive EV
  3. Auto-tune min_edge and kelly_fraction to improve future results
  4. Generate human-readable insight reports for Telegram /learn command

Learning triggers after every LEARN_EVERY new closed trades.
All parameter changes are persisted in the SQLite adaptive_params table.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timezone
from typing import Any

import numpy as np

from config.settings import settings
from paper_trading import persistence as db

logger = logging.getLogger(__name__)

# Trigger a learning pass after this many new closed trades (adapt more frequently)
LEARN_EVERY = 4

# Rolling window for win-rate computation
ROLLING_WINDOW = 20

# Parameter adaptation bounds
MIN_EDGE_MIN = 0.01
MIN_EDGE_MAX = 0.15
KELLY_MIN    = 0.05
KELLY_MAX    = 0.50


class Learner:
    """
    Stateful learner that adapts trading parameters based on outcome history.

    Attach to PaperEngine and call `maybe_learn()` after each cycle.
    """

    def __init__(self) -> None:
        self._last_learned_at_count = self._closed_count()

    # ── Public ────────────────────────────────────────────────────────────────

    def maybe_learn(self) -> dict[str, Any] | None:
        """
        Run a learning pass if enough new trades have closed since last pass.

        Returns:
            Insight dict if learning ran, None otherwise.
        """
        current = self._closed_count()
        if current - self._last_learned_at_count < LEARN_EVERY:
            return None
        if current < 5:
            return None   # need at least 5 trades for meaningful stats

        insights = self._learn(current)
        self._last_learned_at_count = current
        return insights

    def force_learn(self) -> dict[str, Any]:
        """Force a learning pass regardless of trade count."""
        return self._learn(self._closed_count())

    def build_report(self) -> str:
        """
        Build the full text report for the Telegram /learn command.
        """
        trades = db.get_all_closed_trades()
        if not trades:
            return "No closed trades yet. The system will learn once trades resolve."

        n = len(trades)
        wins = [t for t in trades if t["status"] == "won"]
        losses = [t for t in trades if t["status"] == "lost"]
        win_rate = len(wins) / n if n else 0.0

        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
        avg_pnl = np.mean(pnls) if pnls else 0.0
        total_pnl = sum(pnls)

        brier = self._brier_score(trades)
        sharpe = self._sharpe(pnls)

        params = db.get_all_params()
        cur_edge = params.get("min_edge", settings.min_edge_threshold)
        cur_kelly = params.get("kelly_fraction", settings.kelly_fraction)

        hour_stats = self._hourly_stats(trades)
        regime_stats = self._regime_stats(trades)

        recent_wr = self._rolling_win_rate(trades, ROLLING_WINDOW)

        snapshots = db.get_recent_snapshots(5)

        lines = [
            "🧠 *Learning Report*",
            "",
            f"*Trades analysed:* {n} ({len(wins)}W / {len(losses)}L)",
            f"*Overall win rate:* {win_rate:.1%}",
            f"*Rolling {ROLLING_WINDOW}-trade WR:* {recent_wr:.1%}",
            f"*Total PnL:* {total_pnl:+.2f} USDC",
            f"*Avg PnL per trade:* {avg_pnl:+.2f} USDC",
            f"*Brier score:* {brier:.4f}  (0=perfect, 0.25=random)",
            f"*Sharpe (trade returns):* {sharpe:.2f}",
            "",
            "📐 *Current Adaptive Params*",
            f"  min\\_edge: {cur_edge:.4f}  (base: {settings.min_edge_threshold:.4f})",
            f"  kelly\\_fraction: {cur_kelly:.3f}  (base: {settings.kelly_fraction:.3f})",
            "",
        ]

        # Best hours
        if hour_stats:
            best_hour, best_wr = max(hour_stats.items(), key=lambda x: x[1]["win_rate"])
            worst_hour, worst_wr = min(hour_stats.items(), key=lambda x: x[1]["win_rate"])
            if best_wr["n"] >= 3:
                lines.append(
                    f"⏰ *Best hour:* {best_hour:02d}:00 UTC — "
                    f"{best_wr['win_rate']:.0%} WR ({best_wr['n']} trades)"
                )
            if worst_wr["n"] >= 3:
                lines.append(
                    f"⚠️ *Worst hour:* {worst_hour:02d}:00 UTC — "
                    f"{worst_wr['win_rate']:.0%} WR ({worst_wr['n']} trades)"
                )
            lines.append("")

        # Regime stats
        if regime_stats:
            lines.append("📊 *Performance by volatility regime*")
            for label, stat in regime_stats.items():
                if stat["n"] >= 3:
                    lines.append(
                        f"  {label}: {stat['win_rate']:.0%} WR, "
                        f"avg {stat['avg_pnl']:+.2f} ({stat['n']} trades)"
                    )
            lines.append("")

        # Trend
        if len(snapshots) >= 2:
            prev_wr = snapshots[-1].get("win_rate", win_rate)
            trend = "📈" if recent_wr > prev_wr else "📉"
            lines.append(f"Trend: {trend} win rate {prev_wr:.1%} → {recent_wr:.1%}")

        # Adaptation advice
        lines.append("")
        lines.append("🔧 *Auto-adaptations applied*")
        lines.extend(self._adaptation_notes(win_rate, recent_wr, n, brier))

        return "\n".join(lines)

    # ── Private: Core Learning ────────────────────────────────────────────────

    def _learn(self, n_closed: int) -> dict[str, Any]:
        trades = db.get_all_closed_trades()
        if not trades:
            return {"n": 0}

        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
        win_rate = len([t for t in trades if t["status"] == "won"]) / len(trades)
        avg_pnl = float(np.mean(pnls)) if pnls else 0.0
        edges = [t["edge"] for t in trades if t.get("edge") is not None]
        avg_edge = float(np.mean(edges)) if edges else 0.0
        brier = self._brier_score(trades)
        recent_wr = self._rolling_win_rate(trades, ROLLING_WINDOW)

        # Fetch current adaptive params
        cur_edge = db.get_param("min_edge", settings.min_edge_threshold)
        cur_kelly = db.get_param("kelly_fraction", settings.kelly_fraction)

        new_edge, new_kelly, notes = self._adapt_params(
            cur_edge, cur_kelly, win_rate, recent_wr, brier, len(trades)
        )

        # Only update if changed meaningfully
        if abs(new_edge - cur_edge) > 1e-6:
            db.set_param("min_edge", new_edge)
            logger.info("Learner: min_edge adjusted %.4f → %.4f", cur_edge, new_edge)

        if abs(new_kelly - cur_kelly) > 1e-6:
            db.set_param("kelly_fraction", new_kelly)
            logger.info("Learner: kelly_fraction adjusted %.3f → %.3f", cur_kelly, new_kelly)

        db.save_snapshot(
            n_trades=n_closed,
            win_rate=win_rate,
            avg_edge=avg_edge,
            avg_pnl=avg_pnl,
            brier_score=brier,
            min_edge_used=new_edge,
            kelly_used=new_kelly,
            notes="; ".join(notes),
        )

        # Retrain calibration model when poorly calibrated and enough data
        retrained = False
        if brier > 0.28 and n_closed >= 50:
            try:
                from learning.retrain import maybe_retrain
                metrics = maybe_retrain(
                    min_trades=50,
                    min_brier_to_trigger=0.28,
                    current_brier=brier,
                    n_closed=n_closed,
                )
                retrained = metrics is not None
            except Exception as exc:
                logger.warning("Retrain skipped: %s", exc)

        return {
            "n": n_closed,
            "win_rate": win_rate,
            "recent_win_rate": recent_wr,
            "avg_pnl": avg_pnl,
            "brier": brier,
            "new_edge": new_edge,
            "new_kelly": new_kelly,
            "notes": notes,
            "retrained": retrained,
        }

    def _adapt_params(
        self,
        cur_edge: float,
        cur_kelly: float,
        win_rate: float,
        recent_wr: float,
        brier: float,
        n_trades: int,
    ) -> tuple[float, float, list[str]]:
        """
        Compute adjusted min_edge and kelly_fraction based on performance.

        Rules (applied sequentially, effects compound):
          • recent_wr < 0.35 → tighten edge by 15%, reduce kelly by 10%
          • recent_wr < 0.45 → tighten edge by 7%
          • recent_wr > 0.65 → loosen edge by 3% (more opportunities)
          • brier > 0.28 → model is uncalibrated, tighten edge by 10%
          • n_trades < 10 → conservative, no loosening allowed
        """
        notes: list[str] = []
        edge = cur_edge
        kelly = cur_kelly

        if n_trades < 10:
            # Not enough data — only allow tightening
            if recent_wr < 0.35:
                edge = min(edge * 1.12, MIN_EDGE_MAX)
                kelly = max(kelly * 0.90, KELLY_MIN)
                notes.append(f"Early sample, low WR ({recent_wr:.0%}): edge↑ kelly↓")
            return edge, kelly, notes

        # Sustained poor performance
        if recent_wr < 0.35:
            edge = min(edge * 1.15, MIN_EDGE_MAX)
            kelly = max(kelly * 0.90, KELLY_MIN)
            notes.append(f"Rolling WR={recent_wr:.0%} < 35%: edge tightened, kelly reduced")

        elif recent_wr < 0.45:
            edge = min(edge * 1.07, MIN_EDGE_MAX)
            notes.append(f"Rolling WR={recent_wr:.0%} < 45%: edge tightened slightly")

        elif recent_wr > 0.65 and n_trades >= 20:
            # Only loosen if we have a solid sample
            edge = max(edge * 0.97, MIN_EDGE_MIN)
            notes.append(f"Rolling WR={recent_wr:.0%} > 65%: edge relaxed slightly")

        # Model calibration check
        if brier > 0.28:
            edge = min(edge * 1.10, MIN_EDGE_MAX)
            notes.append(f"Brier={brier:.4f} > 0.28 (poor calibration): edge tightened")

        if not notes:
            notes.append("Performance stable — params unchanged")

        edge = float(np.clip(edge, MIN_EDGE_MIN, MIN_EDGE_MAX))
        kelly = float(np.clip(kelly, KELLY_MIN, KELLY_MAX))
        return edge, kelly, notes

    # ── Private: Analytics ────────────────────────────────────────────────────

    def _brier_score(self, trades: list[dict]) -> float:
        """Brier score: mean squared error between model_prob and actual outcome."""
        pairs = [
            (t["model_prob"], 1.0 if t["status"] == "won" else 0.0)
            for t in trades
            if t["model_prob"] is not None
        ]
        if not pairs:
            return 0.25
        probs, actuals = zip(*pairs)
        return float(np.mean([(p - a) ** 2 for p, a in zip(probs, actuals)]))

    def _sharpe(self, pnls: list[float], risk_free: float = 0.0) -> float:
        """Trade-level Sharpe ratio (not annualized)."""
        if len(pnls) < 2:
            return 0.0
        arr = np.array(pnls)
        std = np.std(arr)
        if std == 0:
            return 0.0
        return float((np.mean(arr) - risk_free) / std)

    def _rolling_win_rate(self, trades: list[dict], window: int) -> float:
        recent = trades[-window:]
        if not recent:
            return 0.0
        wins = sum(1 for t in recent if t["status"] == "won")
        return wins / len(recent)

    def _hourly_stats(self, trades: list[dict]) -> dict[int, dict]:
        stats: dict[int, dict] = {}
        for t in trades:
            try:
                ts = datetime.fromisoformat(t["opened_at"])
                hour = ts.hour
            except Exception:
                continue
            if hour not in stats:
                stats[hour] = {"n": 0, "wins": 0}
            stats[hour]["n"] += 1
            if t["status"] == "won":
                stats[hour]["wins"] += 1
        for h in stats:
            n = stats[h]["n"]
            stats[h]["win_rate"] = stats[h]["wins"] / n if n else 0.0
        return stats

    def _regime_stats(self, trades: list[dict]) -> dict[str, dict]:
        """Break down performance by volatility regime (high/low rv_5m)."""
        high_vol = []
        low_vol = []
        for t in trades:
            feats = {}
            if t.get("features"):
                try:
                    feats = json.loads(t["features"])
                except Exception:
                    pass
            rv = feats.get("rv_5m")
            if rv is None:
                continue
            (high_vol if rv > 0.5 else low_vol).append(t)

        result = {}
        for label, group in [("High vol (rv>0.5)", high_vol), ("Low vol (rv≤0.5)", low_vol)]:
            if not group:
                continue
            wins = sum(1 for t in group if t["status"] == "won")
            pnls = [t["pnl"] for t in group if t["pnl"] is not None]
            result[label] = {
                "n": len(group),
                "win_rate": wins / len(group),
                "avg_pnl": float(np.mean(pnls)) if pnls else 0.0,
            }
        return result

    def _adaptation_notes(
        self, win_rate: float, recent_wr: float, n: int, brier: float
    ) -> list[str]:
        notes = []
        if n < 5:
            notes.append("  Collecting data — no adaptations yet (need 5+ trades)")
            return notes
        if recent_wr < 0.35:
            notes.append(f"  ↑ min\\_edge tightened (rolling WR={recent_wr:.0%}, too many losses)")
            notes.append(f"  ↓ kelly reduced (preserve capital during losing streak)")
        elif recent_wr < 0.45:
            notes.append(f"  ↑ min\\_edge tightened slightly (rolling WR={recent_wr:.0%})")
        elif recent_wr > 0.65 and n >= 20:
            notes.append(f"  ↓ min\\_edge relaxed slightly (rolling WR={recent_wr:.0%}, performing well)")
        else:
            notes.append(f"  No parameter changes (WR={recent_wr:.0%}, within target range)")
        if brier > 0.28:
            notes.append(f"  ↑ min\\_edge tightened (Brier={brier:.4f}, model needs retraining)")
        return notes

    def _closed_count(self) -> int:
        counts = db.get_trade_count()
        total = counts.get("total") or 0
        open_ = counts.get("open") or 0
        return total - open_
