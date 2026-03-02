"""
Multi-source Sentiment Collector.

Aggregates real-time signals from:
  1. Crypto Fear & Greed Index (alternative.me) — macro sentiment
  2. Binance perpetual futures funding rate — short-term directional pressure
  3. Binance open interest — momentum proxy
  4. RSS news feeds (CoinDesk, CoinTelegraph) — event-driven sentiment
  5. Binance BTC/USDT liquidation proxy (from ticker data)

All sources return a SentimentSnapshot that is consumed by the feature
builder to add market-context features to the model.
"""

from __future__ import annotations

import logging
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ── Data Classes ──────────────────────────────────────────────────────────────

@dataclass
class FearGreedSnapshot:
    value: int          # 0 (extreme fear) – 100 (extreme greed)
    label: str          # e.g. "Extreme Fear", "Neutral", "Greed"
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def normalized(self) -> float:
        """Return 0..1 (0=max fear, 1=max greed)."""
        return self.value / 100.0


@dataclass
class FundingRateSnapshot:
    symbol: str         # e.g. "BTCUSDT"
    funding_rate: float # positive = longs pay shorts (bearish), negative = shorts pay longs (bullish)
    next_funding_ms: int
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def sentiment_score(self) -> float:
        """Funding rate mapped to [-1, 1] — negative is bullish."""
        return -float(self.funding_rate) / 0.001  # normalize by typical max


@dataclass
class NewsHeadline:
    title: str
    source: str
    published: datetime
    url: str = ""

    def is_btc_relevant(self) -> bool:
        keywords = {"bitcoin", "btc", "crypto", "blockchain", "halving",
                    "etf", "sec", "fed", "fomc", "rate", "inflation"}
        return any(kw in self.title.lower() for kw in keywords)


@dataclass
class SentimentSnapshot:
    """Aggregated sentiment from all sources for one point in time."""
    timestamp: datetime
    fear_greed: FearGreedSnapshot | None
    funding_rate: FundingRateSnapshot | None
    btc_headlines: list[NewsHeadline]
    open_interest_change_pct: float   # OI change in past hour (positive = building longs)
    btc_dominance: float | None       # BTC dominance % (proxy for risk-on/off)

    @property
    def composite_score(self) -> float:
        """
        Composite sentiment score: 0 = very bearish, 0.5 = neutral, 1 = very bullish.
        Combines Fear & Greed + funding rate + news headline polarity.
        """
        scores: list[float] = []

        if self.fear_greed:
            scores.append(self.fear_greed.normalized)

        if self.funding_rate:
            # Positive funding = bullish pressure (longs paying)
            fr_score = 0.5 + self.funding_rate.sentiment_score * 0.1
            scores.append(max(0.0, min(1.0, fr_score)))

        if self.btc_headlines:
            # Simple keyword scoring
            hl_score = _score_headlines(self.btc_headlines)
            scores.append(hl_score)

        if self.open_interest_change_pct > 1.0:
            scores.append(0.6)   # OI rising = more longs entering
        elif self.open_interest_change_pct < -1.0:
            scores.append(0.4)   # OI falling = deleveraging

        return float(sum(scores) / len(scores)) if scores else 0.5

    def as_feature_dict(self) -> dict[str, float]:
        """Return a flat dict of sentiment features for the model."""
        return {
            "fear_greed": float(self.fear_greed.normalized) if self.fear_greed else 0.5,
            "funding_rate": float(self.funding_rate.funding_rate) if self.funding_rate else 0.0,
            "funding_sentiment": float(self.funding_rate.sentiment_score) if self.funding_rate else 0.0,
            "oi_change_pct": float(self.open_interest_change_pct),
            "composite_sentiment": float(self.composite_score),
            "n_btc_headlines": float(len(self.btc_headlines)),
            "headline_sentiment": _score_headlines(self.btc_headlines) if self.btc_headlines else 0.5,
        }


# ── Fetchers ──────────────────────────────────────────────────────────────────

def fetch_fear_greed(client: httpx.Client | None = None) -> FearGreedSnapshot | None:
    """Fetch the Crypto Fear & Greed Index from alternative.me."""
    close = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.get("https://api.alternative.me/fng/", params={"limit": 1})
        resp.raise_for_status()
        data = resp.json()["data"][0]
        return FearGreedSnapshot(
            value=int(data["value"]),
            label=data["value_classification"],
        )
    except Exception as exc:
        logger.warning("Fear & Greed fetch failed: %s", exc)
        return None
    finally:
        if close:
            client.close()


def fetch_funding_rate(
    symbol: str = "BTCUSDT",
    client: httpx.Client | None = None,
) -> FundingRateSnapshot | None:
    """Fetch BTC perpetual futures funding rate from Binance."""
    close = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.get(
            "https://fapi.binance.com/fapi/v1/fundingRate",
            params={"symbol": symbol, "limit": 1},
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        latest = data[0]
        return FundingRateSnapshot(
            symbol=symbol,
            funding_rate=float(latest["fundingRate"]),
            next_funding_ms=int(latest.get("fundingTime", 0)),
        )
    except Exception as exc:
        logger.warning("Funding rate fetch failed: %s", exc)
        return None
    finally:
        if close:
            client.close()


def fetch_open_interest_change(
    symbol: str = "BTCUSDT",
    client: httpx.Client | None = None,
) -> float:
    """
    Fetch recent open interest change (%) as a momentum signal.
    Compares current OI vs 1h ago using Binance futures OI history.
    """
    close = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.get(
            "https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": symbol, "period": "1h", "limit": 2},
        )
        resp.raise_for_status()
        data = resp.json()
        if len(data) >= 2:
            current = float(data[-1]["sumOpenInterest"])
            previous = float(data[-2]["sumOpenInterest"])
            if previous > 0:
                return 100.0 * (current - previous) / previous
    except Exception as exc:
        logger.debug("OI change fetch failed: %s", exc)
    finally:
        if close:
            client.close()
    return 0.0


def fetch_rss_headlines(
    max_per_feed: int = 5,
    client: httpx.Client | None = None,
) -> list[NewsHeadline]:
    """
    Fetch recent crypto news headlines from RSS feeds.

    Sources:
      - CoinDesk
      - CoinTelegraph
      - Decrypt
    """
    feeds = [
        ("CoinDesk",      "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("CoinTelegraph",  "https://cointelegraph.com/rss"),
        ("Decrypt",        "https://decrypt.co/feed"),
    ]

    close = client is None
    client = client or httpx.Client(timeout=15.0, follow_redirects=True)
    headlines: list[NewsHeadline] = []

    for source, url in feeds:
        try:
            resp = client.get(url)
            resp.raise_for_status()
            root = ET.fromstring(resp.text)
            ns = {}

            items = root.findall(".//item")[:max_per_feed]
            for item in items:
                title = _xml_text(item, "title")
                link  = _xml_text(item, "link")
                pub   = _xml_text(item, "pubDate")

                try:
                    from email.utils import parsedate_to_datetime
                    pub_dt = parsedate_to_datetime(pub)
                except Exception:
                    pub_dt = datetime.now(timezone.utc)

                if title:
                    headlines.append(NewsHeadline(
                        title=title,
                        source=source,
                        published=pub_dt,
                        url=link or "",
                    ))
        except Exception as exc:
            logger.debug("RSS fetch failed for %s: %s", source, exc)

    if close:
        client.close()

    logger.debug("Fetched %d headlines from RSS feeds", len(headlines))
    return headlines


# ── Composite Fetcher ─────────────────────────────────────────────────────────

def fetch_all_sentiment(cache_ttl: int = 300) -> SentimentSnapshot:
    """
    Fetch all sentiment sources and return a composite snapshot.
    Designed to be called once per cycle (results cached externally if needed).
    """
    with httpx.Client(timeout=15.0, follow_redirects=True) as client:
        fear_greed  = fetch_fear_greed(client)
        funding     = fetch_funding_rate(client=client)
        oi_change   = fetch_open_interest_change(client=client)
        headlines   = fetch_rss_headlines(client=client)

    btc_headlines = [h for h in headlines if h.is_btc_relevant()]

    snap = SentimentSnapshot(
        timestamp=datetime.now(timezone.utc),
        fear_greed=fear_greed,
        funding_rate=funding,
        btc_headlines=btc_headlines,
        open_interest_change_pct=oi_change,
        btc_dominance=None,   # TODO: add dominance from CoinGecko
    )

    logger.info(
        "Sentiment: F&G=%s | funding=%.6f | OI_chg=%.2f%% | headlines=%d | composite=%.3f",
        fear_greed.value if fear_greed else "N/A",
        funding.funding_rate if funding else 0.0,
        oi_change,
        len(btc_headlines),
        snap.composite_score,
    )

    return snap


# ── Helpers ───────────────────────────────────────────────────────────────────

def _xml_text(elem: ET.Element, tag: str) -> str:
    child = elem.find(tag)
    return child.text.strip() if child is not None and child.text else ""


def _score_headlines(headlines: list[NewsHeadline]) -> float:
    """
    Very lightweight headline sentiment: keyword counting.
    Returns 0..1 (0=bearish, 0.5=neutral, 1=bullish).
    """
    bullish = {"surge", "rally", "soar", "pump", "ath", "breakout",
               "bull", "adoption", "approval", "etf approved", "buy"}
    bearish = {"crash", "drop", "dump", "ban", "hack", "sec", "lawsuit",
               "bear", "sell", "fall", "plunge", "risk", "fear"}

    bull_count = 0
    bear_count = 0
    for h in headlines:
        text = h.title.lower()
        bull_count += sum(1 for kw in bullish if kw in text)
        bear_count += sum(1 for kw in bearish if kw in text)

    total = bull_count + bear_count
    if total == 0:
        return 0.5
    return float(bull_count / total)
