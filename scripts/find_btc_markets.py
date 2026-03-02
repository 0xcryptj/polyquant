"""
Discover active Polymarket BTC/crypto markets.

Searches Polymarket Gamma and CLOB APIs for currently open, liquid
markets related to Bitcoin price movements.  Saves results to
config/btc_markets.json so the trading engine can load them.

Market priority (highest first):
  1. BTC 5-minute tick markets (resolution < 1 h)
  2. BTC daily / weekly price range markets (resolution < 7 days)
  3. BTC monthly price markets (resolution < 60 days)
  4. General BTC/crypto high-liquidity events

Run before starting the bot:
    python scripts/find_btc_markets.py
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
GAMMA_API   = "https://gamma-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
OUTPUT_PATH = PROJECT_ROOT / "config" / "btc_markets.json"

# Keywords for matching Bitcoin / crypto price markets
BTC_KEYWORDS = ["btc", "bitcoin"]
TIME_KEYWORDS_5M = ["5-minute", "5 minute", "5min", "next 5"]
PRICE_KEYWORDS = [
    "above", "below", "higher", "lower", "reach", "hit",
    "price", "end", "close", "over", "under",
]
MIN_LIQUIDITY = 100.0   # USDC — ignore tiny markets

NOW = datetime.now(timezone.utc)


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _parse_date(s: str) -> datetime | None:
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


def _days_until(end_str: str) -> float | None:
    dt = _parse_date(end_str)
    if dt is None:
        return None
    delta = (dt - NOW).total_seconds() / 86400
    return delta


def _is_btc(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in BTC_KEYWORDS)


def _is_price_market(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in PRICE_KEYWORDS)


def _is_5min(question: str) -> bool:
    q = question.lower()
    return any(kw in q for kw in TIME_KEYWORDS_5M)


def _market_priority(market: dict) -> int:
    """Lower number = higher priority."""
    q = market.get("question", "").lower()
    days = market.get("days_until_end")

    if _is_5min(q):
        return 0
    if days is not None and days < 1:
        return 1
    if days is not None and days < 7:
        return 2
    if days is not None and days < 60:
        return 3
    return 4


def _event_slug(title: str) -> str:
    """Derive Polymarket event slug from title (generic fallback)."""
    import re
    s = (title or "").lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"[\s_]+", "-", s)
    return s[:80] if s else ""


def _slug_for_5m_market(market: dict) -> str | None:
    """
    Build Polymarket 5m event slug: btc-updown-5m-{window_start_unix}.
    Window start = end_date - 5 minutes. Returns None if end_date missing/invalid.
    """
    end_str = market.get("end_date") or ""
    dt = _parse_date(end_str)
    if dt is None:
        return None
    from datetime import timedelta
    window_start = dt - timedelta(minutes=5)
    return f"btc-updown-5m-{int(window_start.timestamp())}"


# ─── Discovery ────────────────────────────────────────────────────────────────

def _gamma_search(client: httpx.Client, limit: int = 200) -> list[dict]:
    """Search Gamma API for active BTC events and extract market metadata."""
    found: list[dict] = []
    seen_conditions: set[str] = set()

    search_terms = [
        "Bitcoin", "BTC price", "BTC above", "bitcoin hit",
        "BTC 5", "bitcoin 5-minute", "crypto price",
    ]

    for term in search_terms:
        try:
            resp = client.get(
                f"{GAMMA_API}/markets",
                params={"active": "true", "closed": "false", "limit": 200, "keyword": term},
            )
            resp.raise_for_status()
            markets = resp.json()
            if not isinstance(markets, list):
                continue

            for m in markets:
                cid = m.get("conditionId", "")
                if not cid or cid in seen_conditions:
                    continue

                q = m.get("question", "")
                if not _is_btc(q):
                    continue

                liquidity = float(m.get("liquidity", 0) or 0)
                if liquidity < MIN_LIQUIDITY and not _is_5min(q):
                    continue

                end_str = m.get("endDate", "")
                days = _days_until(end_str)

                # Skip markets that have already resolved
                if days is not None and days < 0:
                    continue

                # Get YES token ID from outcomeTokens or clobTokenIds
                yes_token = _extract_yes_token_gamma(m)
                if not yes_token:
                    continue

                seen_conditions.add(cid)
                found.append({
                    "token_id": yes_token,
                    "question": q,
                    "end_date": end_str,
                    "condition_id": cid,
                    "liquidity": liquidity,
                    "days_until_end": days,
                    "source": "gamma",
                    "market_type": "5min" if _is_5min(q) else "price" if _is_price_market(q) else "event",
                })
                logger.info("[Gamma] %s | %.0f USDC liq | %.1f days",
                            q[:70], liquidity, days or 0)

        except Exception as exc:
            logger.warning("Gamma search '%s' failed: %s", term, exc)

    return found


def _extract_yes_token_gamma(m: dict) -> str:
    """
    Extract YES token ID from a Gamma API market object.

    The field layout varies by market version — try several paths.
    """
    # v2 events: clobTokenIds is a JSON-encoded string or list
    clob_ids = m.get("clobTokenIds")
    if clob_ids:
        if isinstance(clob_ids, str):
            try:
                clob_ids = json.loads(clob_ids)
            except Exception:
                pass
        if isinstance(clob_ids, list) and clob_ids:
            return str(clob_ids[0])

    # v1: tokens list with outcome labels
    tokens = m.get("tokens", [])
    if isinstance(tokens, list):
        for tok in tokens:
            if isinstance(tok, dict):
                outcome = tok.get("outcome", "").upper()
                if outcome == "YES":
                    return tok.get("token_id", "")
        # fallback: first token
        if tokens:
            tok = tokens[0]
            return tok.get("token_id", "") if isinstance(tok, dict) else str(tok)

    return ""


def _clob_search(client: httpx.Client) -> list[dict]:
    """
    Search CLOB API for active BTC/crypto markets with real order books.
    Iterates through pages until BTC markets are found.
    """
    found: list[dict] = []
    seen: set[str] = set()
    cursor = ""
    pages_checked = 0

    while pages_checked < 20:
        try:
            resp = client.get(
                f"{CLOB_API}/markets",
                params={"next_cursor": cursor, "limit": 500},
            )
            resp.raise_for_status()
            data = resp.json()
            markets = data.get("data", []) if isinstance(data, dict) else []
            cursor = data.get("next_cursor", "") if isinstance(data, dict) else ""
            pages_checked += 1

            for m in markets:
                q = m.get("question", "")
                if not _is_btc(q):
                    continue

                condition_id = m.get("condition_id", "")
                if condition_id in seen:
                    continue

                end_str = m.get("end_date_iso", "")
                days = _days_until(end_str)
                if days is not None and days < 0:
                    continue

                tokens = m.get("tokens", [])
                yes_tok = next(
                    (t for t in tokens if isinstance(t, dict) and t.get("outcome", "").upper() == "YES"),
                    tokens[0] if tokens else None
                )
                if not yes_tok:
                    continue
                token_id = yes_tok.get("token_id", "") if isinstance(yes_tok, dict) else ""
                if not token_id:
                    continue

                seen.add(condition_id)
                found.append({
                    "token_id": token_id,
                    "question": q,
                    "end_date": end_str,
                    "condition_id": condition_id,
                    "liquidity": 0,
                    "days_until_end": days,
                    "source": "clob",
                    "market_type": "5min" if _is_5min(q) else "price" if _is_price_market(q) else "event",
                })
                logger.info("[CLOB] %s | %.1f days", q[:70], days or 0)

            if not cursor:
                break

        except Exception as exc:
            logger.warning("CLOB page %d failed: %s", pages_checked, exc)
            break

    return found


def _gamma_events_search(client: httpx.Client) -> list[dict]:
    """Search Gamma events endpoint for BTC/crypto events with embedded markets."""
    found: list[dict] = []
    seen: set[str] = set()

    try:
        resp = client.get(
            f"{GAMMA_API}/events",
            params={"active": "true", "closed": "false", "limit": 200},
        )
        resp.raise_for_status()
        events = resp.json()
        if not isinstance(events, list):
            return found

        for e in events:
            title = e.get("title", "")
            if not any(kw in title.lower() for kw in BTC_KEYWORDS + ["crypto"]):
                continue

            # Polymarket event slug (e.g. btc-updown-5m-1772436600) when present
            event_slug = e.get("slug") or e.get("slugId") or ""

            for m in e.get("markets", []):
                q = m.get("question", "")
                condition_id = m.get("conditionId", "")
                if condition_id in seen:
                    continue

                end_str = m.get("endDate", "")
                days = _days_until(end_str)
                if days is not None and days < 0:
                    continue

                # clobTokenIds is a JSON-encoded list
                clob_ids = m.get("clobTokenIds", [])
                if isinstance(clob_ids, str):
                    try:
                        clob_ids = json.loads(clob_ids)
                    except Exception:
                        clob_ids = []

                if not clob_ids or not isinstance(clob_ids, list):
                    continue

                yes_token = str(clob_ids[0])
                liquidity = float(m.get("liquidity", 0) or 0)
                seen.add(condition_id)

                row = {
                    "token_id": yes_token,
                    "question": q,
                    "end_date": end_str,
                    "condition_id": condition_id,
                    "liquidity": liquidity,
                    "days_until_end": days,
                    "source": "gamma_events",
                    "market_type": "5min" if _is_5min(q) else "price" if _is_price_market(q) else "event",
                    "event_title": title,
                }
                if event_slug:
                    row["event_slug"] = event_slug
                found.append(row)
                logger.info("[Events] %s | %s | %.0f USDC liq",
                            title[:40], q[:50], liquidity)

    except Exception as exc:
        logger.warning("Gamma events search failed: %s", exc)

    return found


def find_all_btc_markets(limit: int = 200) -> list[dict]:
    """
    Search all Polymarket sources for active BTC/crypto markets.

    Returns markets sorted by priority (5-min first, then by liquidity).
    """
    found: list[dict] = []
    seen_tokens: set[str] = set()

    with httpx.Client(timeout=30.0) as client:
        logger.info("Searching Gamma API...")
        for m in _gamma_search(client):
            if m["token_id"] not in seen_tokens:
                seen_tokens.add(m["token_id"])
                found.append(m)

        logger.info("Searching Gamma Events...")
        for m in _gamma_events_search(client):
            if m["token_id"] not in seen_tokens:
                seen_tokens.add(m["token_id"])
                found.append(m)

        if len(found) < 5:
            logger.info("Searching CLOB API (fallback)...")
            for m in _clob_search(client):
                if m["token_id"] not in seen_tokens:
                    seen_tokens.add(m["token_id"])
                    found.append(m)

    # Sort: 5-min first, then by liquidity descending
    found.sort(key=lambda m: (_market_priority(m), -(m.get("liquidity") or 0)))
    return found


def save_markets(markets: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out = []
    for m in markets:
        row = dict(m)
        # Prefer real Polymarket slug; for 5m use btc-updown-5m-{window_start_unix} when possible
        if not row.get("event_slug") or (row.get("market_type") == "5min" and not row.get("event_slug", "").startswith("btc-updown-5m-")):
            if row.get("market_type") == "5min":
                slug_5m = _slug_for_5m_market(row)
                if slug_5m:
                    row["event_slug"] = slug_5m
        if not row.get("event_slug") and (row.get("event_title") or row.get("question")):
            row["event_slug"] = _event_slug(row.get("event_title") or row.get("question", ""))
        out.append(row)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(out, f, indent=2)
    logger.info("Saved %d markets to %s", len(out), OUTPUT_PATH)


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Discover Polymarket BTC/crypto markets")
    ap.add_argument("--5min-only", dest="five_min_only", action="store_true",
                    help="Only save 5-minute markets (for 5-min trading; skip date/event markets)")
    args = ap.parse_args()

    logger.info("="*60)
    logger.info("PolyQuant — Polymarket BTC/Crypto Market Discovery")
    if args.five_min_only:
        logger.info("Mode: 5-minute markets ONLY")
    logger.info("="*60)

    markets = find_all_btc_markets()
    if args.five_min_only:
        markets = [m for m in markets if _is_5min(m.get("question", ""))]
        if not markets:
            logger.warning("No 5-minute markets found. Run without --5min-only to use date/event markets.")

    if not markets:
        logger.warning(
            "\nNo active BTC/crypto markets found.\n"
            "Possible reasons:\n"
            "  1. No BTC 5-min markets running right now (they're episodic)\n"
            "  2. API rate limiting — try again in a few minutes\n\n"
            "The bot will use SIMULATION MODE until markets are found."
        )
        save_markets([])
        return

    logger.info("\n%d BTC/crypto markets found:", len(markets))
    logger.info("%-6s %-10s %-12s %s", "Type", "Liquidity", "Days Left", "Question")
    logger.info("-"*80)
    for m in markets:
        days = m.get("days_until_end")
        days_str = f"{days:.1f}d" if days is not None else "?"
        liq_str = f"${m.get('liquidity', 0):.0f}"
        logger.info("%-6s %-10s %-12s %s",
                    m.get("market_type","?")[:6], liq_str, days_str,
                    m.get("question","")[:60])

    save_markets(markets)
    logger.info("\nConfig saved to %s", OUTPUT_PATH)
    logger.info("Start the bot with: python bot/main.py")


if __name__ == "__main__":
    main()
