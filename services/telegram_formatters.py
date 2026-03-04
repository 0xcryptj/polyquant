"""
Shared Telegram message formatters.

Used by both services.telegram (orchestrator bot) and bot.telegram_bot (legacy).
No runtime or engine imports — pure formatting only.
"""


def bar(ratio: float, width: int = 10) -> str:
    """Build a filled/empty Unicode block bar. ratio in [0, 1]."""
    filled = round(max(0.0, min(1.0, ratio)) * width)
    return "█" * filled + "░" * (width - filled)


def format_pnl_card(result: dict, status: dict) -> str:
    """
    Build a rich Telegram PnL card.

    Args:
        result: performance metrics (closed, brier, sharpe, rwr, avg_pnl, best, worst, max_dd)
        status: engine status (balance, return_pct, total_pnl, n_wins, n_losses, win_rate, …)
    """
    total_pnl = status.get("total_pnl", 0.0)
    balance = status.get("balance", 0.0)
    return_pct = status.get("return_pct", 0.0)
    n_wins = status.get("n_wins", 0)
    n_losses = status.get("n_losses", 0)
    n_total = result.get("closed", 0)
    win_rate = status.get("win_rate", 0.0)

    pnl_arrow = "📈" if total_pnl >= 0 else "📉"
    ret_icon = "🟢" if return_pct >= 0 else "🔴"
    pnl_sign = "+" if total_pnl >= 0 else ""

    wr_bar = bar(win_rate)
    rwr = result.get("rwr", 0.0)
    rwr_bar = bar(rwr)

    sharpe = result.get("sharpe", 0.0)
    brier = result.get("brier", 0.25)
    max_dd = result.get("max_dd", 0.0)
    avg_pnl = result.get("avg_pnl", 0.0)
    best = result.get("best", 0.0)
    worst = result.get("worst", 0.0)

    sharpe_icon = "🟢" if sharpe > 0.5 else "🟡" if sharpe > 0 else "🔴"
    brier_icon = "🟢" if brier < 0.20 else "🟡" if brier < 0.25 else "🔴"
    dd_icon = "🟢" if max_dd < 0.05 else "🟡" if max_dd < 0.15 else "🔴"

    return (
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"  {pnl_arrow}  *P & L  REPORT*\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{ret_icon} *${balance:,.2f}*   `{pnl_sign}${total_pnl:.2f}`  `({return_pct:+.1f}%)`\n\n"
        f"*WIN RATE*\n"
        f"`{wr_bar}` {win_rate:.0%}\n"
        f"  {n_wins}W / {n_losses}L  ·  {n_total} trades total\n\n"
        f"*ROLLING* _(last 20)_\n"
        f"`{rwr_bar}` {rwr:.0%}\n\n"
        f"*PER TRADE*\n"
        f"  Avg:    `{avg_pnl:+.2f}`\n"
        f"  Best:   `+${best:.2f}`\n"
        f"  Worst:  `${worst:.2f}`\n\n"
        f"*RISK*\n"
        f"  Sharpe:  `{sharpe:+.2f}` {sharpe_icon}\n"
        f"  Max DD:  `{max_dd:.1%}` {dd_icon}\n"
        f"  Brier:   `{brier:.3f}` {brier_icon}\n"
        f"━━━━━━━━━━━━━━━━━━━━━"
    )
