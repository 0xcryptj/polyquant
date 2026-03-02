"""
Sentiment + macro feature engineering.

Converts raw sentiment signals (Fear & Greed, funding rate, news, whale
positions) into model-ready numeric features that complement the
price-based OHLCV features from feature_builder.py.

Features produced:
  fear_greed          — 0..1 (0=extreme fear, 1=extreme greed)
  funding_rate        — raw funding rate (sign carries direction)
  funding_sentiment   — normalised -1..1 (negative = bullish)
  oi_change_pct       — open interest % change in past hour
  composite_sentiment — 0..1 weighted average of all sources
  n_btc_headlines     — count of BTC-relevant news items
  headline_sentiment  — 0..1 (0=bearish, 1=bullish keywords)
  whale_signal_yes    — USDC held by whales in YES direction
  whale_signal_no     — USDC held by whales in NO direction
  whale_consensus     — 0..1 (0=whales short, 1=whales long)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

SENTIMENT_FEATURE_COLUMNS = [
    "fear_greed",
    "funding_rate",
    "funding_sentiment",
    "oi_change_pct",
    "composite_sentiment",
    "n_btc_headlines",
    "headline_sentiment",
    "whale_signal_yes",
    "whale_signal_no",
    "whale_consensus",
]

# Neutral defaults used when data is unavailable
_DEFAULTS: dict[str, float] = {
    "fear_greed": 0.5,
    "funding_rate": 0.0,
    "funding_sentiment": 0.0,
    "oi_change_pct": 0.0,
    "composite_sentiment": 0.5,
    "n_btc_headlines": 0.0,
    "headline_sentiment": 0.5,
    "whale_signal_yes": 0.0,
    "whale_signal_no": 0.0,
    "whale_consensus": 0.5,
}


def build_sentiment_features(
    sentiment: Any | None,           # SentimentSnapshot or None
    wallet_signal: Any | None = None, # WalletSignal or None
) -> dict[str, float]:
    """
    Convert live sentiment and wallet signals into a flat feature dict.

    Args:
        sentiment:     SentimentSnapshot from data.sentiment_collector
        wallet_signal: WalletSignal from data.wallet_tracker

    Returns:
        Dict of feature_name → float, ready to merge with OHLCV features.
        Falls back to neutral defaults on missing/failed data.
    """
    features = dict(_DEFAULTS)

    # ── Sentiment features ────────────────────────────────────────────────
    if sentiment is not None:
        try:
            sent_dict = sentiment.as_feature_dict()
            for key in (
                "fear_greed", "funding_rate", "funding_sentiment",
                "oi_change_pct", "composite_sentiment",
                "n_btc_headlines", "headline_sentiment",
            ):
                if key in sent_dict:
                    features[key] = float(sent_dict[key])
        except Exception as exc:
            logger.warning("Sentiment feature extraction failed: %s", exc)

    # ── Whale signal features ─────────────────────────────────────────────
    if wallet_signal is not None:
        try:
            features["whale_signal_yes"] = float(wallet_signal.total_whale_usdc_yes)
            features["whale_signal_no"]  = float(wallet_signal.total_whale_usdc_no)

            total = features["whale_signal_yes"] + features["whale_signal_no"]
            if total > 0:
                features["whale_consensus"] = features["whale_signal_yes"] / total
            else:
                features["whale_consensus"] = 0.5
        except Exception as exc:
            logger.warning("Wallet signal feature extraction failed: %s", exc)

    return features


def neutral_sentiment_features() -> dict[str, float]:
    """Return neutral defaults (used when sentiment data is unavailable)."""
    return dict(_DEFAULTS)
