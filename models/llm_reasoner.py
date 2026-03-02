"""
Claude LLM Reasoning Engine for PolyQuant.

Uses Claude claude-sonnet-4-6 to analyze BTC market conditions, sentiment, whale activity,
and Polymarket order book data to produce probability estimates and trade rationale.

The LLM replaces (or augments) the statistical model when an Anthropic API key
is available.  It is deliberately prompted to reason like a quantitative analyst:
  - State a probability estimate with confidence
  - Cite the evidence (BTC momentum, F&G, news, whale positioning)
  - Output a structured JSON decision

Usage:
    from models.llm_reasoner import LLMReasoner
    reasoner = LLMReasoner()
    result = reasoner.analyze(context)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class TradeReasoning:
    """Structured output from Claude's market analysis."""

    prob_yes: float                   # P(BTC/market resolves YES), 0-1
    direction: str                    # "YES" | "NO" | "SKIP"
    confidence: float                 # 0-1 (how certain the model is)
    reasoning: str                    # one-paragraph rationale
    key_signals: list[str] = field(default_factory=list)  # bullet list of factors
    raw_response: str = ""            # full Claude response for debugging

    @property
    def should_trade(self) -> bool:
        return self.direction in ("YES", "NO") and self.confidence >= 0.55


def _build_prompt(context: dict[str, Any]) -> str:
    """
    Build the analysis prompt from market context.

    context keys (all optional, provide what you have):
        btc_price      – current BTC/USDT price
        btc_change_5m  – 5-min BTC return (e.g. 0.0012 = +0.12%)
        btc_change_1h  – 1-hour BTC return
        btc_change_24h – 24-hour BTC return
        rsi_14         – 14-period RSI
        macd_hist      – MACD histogram value
        volume_ratio   – current/avg volume ratio (>1 = elevated)
        fear_greed     – Fear & Greed index value (0-100)
        fear_greed_label – label e.g. "Fear"
        funding_rate   – Binance perpetual funding rate
        oi_change_pct  – open interest change %
        headlines      – list of recent news headline strings
        market_question – the Polymarket question text
        market_yes_price  – current YES token price (e.g. 0.42)
        market_no_price   – current NO token price
        market_spread  – bid-ask spread
        market_end_date – market resolution date
        whale_direction – "YES" | "NO" | "NEUTRAL"
        whale_strength  – 0-1
        recent_trades   – list of dicts with won/lost/pnl for recent trades
        model_prob      – statistical model probability (if available)
    """
    mq = context.get("market_question", "Will BTC be higher in 5 minutes?")
    yes_price = context.get("market_yes_price", 0.50)
    no_price = context.get("market_no_price", 1 - yes_price)
    end_date = context.get("market_end_date", "soon")

    btc_price  = context.get("btc_price", "N/A")
    chg_5m     = context.get("btc_change_5m")
    chg_1h     = context.get("btc_change_1h")
    chg_24h    = context.get("btc_change_24h")
    rsi        = context.get("rsi_14")
    macd       = context.get("macd_hist")
    vol_ratio  = context.get("volume_ratio")
    fg         = context.get("fear_greed")
    fg_label   = context.get("fear_greed_label", "")
    funding    = context.get("funding_rate")
    oi_chg     = context.get("oi_change_pct")
    headlines  = context.get("headlines", [])
    whale_dir  = context.get("whale_direction", "NEUTRAL")
    whale_str  = context.get("whale_strength", 0.0)
    stat_prob  = context.get("model_prob")

    def fmt(v, fmt_str=".4f", suffix=""):
        return f"{v:{fmt_str}}{suffix}" if v is not None else "N/A"

    lines = [
        "You are a quantitative analyst and prediction market trader specialising in crypto.",
        "Analyse the following market data and produce a trade decision in JSON.",
        "",
        f"=== MARKET ===",
        f"Question : {mq}",
        f"Resolves : {end_date}",
        f"YES price: {yes_price:.4f}   NO price: {no_price:.4f}",
        f"Spread   : {fmt(context.get('market_spread'), '.4f')}",
        "",
        "=== BTC DATA ===",
        f"Price    : ${btc_price}",
        f"5m chg   : {fmt(chg_5m, '+.4%')}",
        f"1h chg   : {fmt(chg_1h, '+.4%')}",
        f"24h chg  : {fmt(chg_24h, '+.4%')}",
        f"RSI-14   : {fmt(rsi, '.1f')}",
        f"MACD hist: {fmt(macd, '.6f')}",
        f"Vol ratio: {fmt(vol_ratio, '.2f')}x",
        "",
        "=== SENTIMENT ===",
        f"Fear & Greed: {fmt(fg, '.0f')} ({fg_label})",
        f"Funding rate: {fmt(funding, '+.6f')}",
        f"OI change   : {fmt(oi_chg, '+.2f', '%')}",
    ]

    if headlines:
        lines += ["", "=== RECENT HEADLINES ==="]
        for h in headlines[:5]:
            lines.append(f"  • {h}")

    lines += [
        "",
        "=== WHALE ACTIVITY ===",
        f"Consensus: {whale_dir}  (strength={fmt(whale_str, '.2f')})",
    ]

    if stat_prob is not None:
        lines += ["", f"Statistical model P(YES): {stat_prob:.4f}"]

    lines += [
        "",
        "=== INSTRUCTIONS ===",
        "1. Estimate P(YES) — the probability this market resolves YES.",
        "2. Decide direction: YES (buy YES token), NO (buy NO token), or SKIP.",
        "   Only trade if you see a genuine edge vs the market price.",
        "3. State your confidence in the decision (0 = wild guess, 1 = very sure).",
        "4. Give a 2-3 sentence rationale citing the most important signals.",
        "5. List up to 5 key signals as bullet points.",
        "",
        "Respond ONLY with valid JSON in this exact schema:",
        '{',
        '  "prob_yes": <float 0-1>,',
        '  "direction": "YES" | "NO" | "SKIP",',
        '  "confidence": <float 0-1>,',
        '  "reasoning": "<2-3 sentences>",',
        '  "key_signals": ["<signal 1>", "<signal 2>", ...]',
        '}',
    ]

    return "\n".join(lines)


class LLMReasoner:
    """
    Claude-backed trade reasoning engine.

    Falls back gracefully when no API key is configured or when the
    API is unavailable — in that case it returns None and the engine
    uses the statistical model instead.
    """

    def __init__(self) -> None:
        self._client = None
        self._model = "claude-sonnet-4-6"
        self._available = False
        self._call_count = 0
        self._error_count = 0

        try:
            from config.settings import settings
            key = settings.anthropic_api_key
            if key and key.get_secret_value():
                import anthropic
                self._client = anthropic.Anthropic(
                    api_key=key.get_secret_value()
                )
                self._available = True
                logger.info("LLM Reasoner initialised (model=%s)", self._model)
            else:
                logger.info("LLM Reasoner disabled — set ANTHROPIC_API_KEY to enable")
        except ImportError:
            logger.warning("anthropic package not installed — pip install anthropic")
        except Exception as exc:
            logger.warning("LLM Reasoner init failed: %s", exc)

    @property
    def is_available(self) -> bool:
        return self._available and self._client is not None

    def analyze(self, context: dict[str, Any]) -> TradeReasoning | None:
        """
        Analyze market context and return a trade reasoning.

        Returns None if the LLM is unavailable or returns an invalid response.
        """
        if not self.is_available:
            return None

        prompt = _build_prompt(context)

        try:
            self._call_count += 1
            message = self._client.messages.create(
                model=self._model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = message.content[0].text.strip()
            return self._parse_response(raw)

        except Exception as exc:
            self._error_count += 1
            logger.warning("LLM analysis failed (%d/%d): %s",
                           self._error_count, self._call_count, exc)
            return None

    def _parse_response(self, raw: str) -> TradeReasoning | None:
        """Parse Claude's JSON response into a TradeReasoning."""
        try:
            # Extract JSON block (Claude sometimes wraps in ```json ... ```)
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if not match:
                logger.warning("LLM response has no JSON block: %s", raw[:200])
                return None

            data = json.loads(match.group())

            prob_yes   = float(data.get("prob_yes", 0.5))
            direction  = str(data.get("direction", "SKIP")).upper()
            confidence = float(data.get("confidence", 0.5))
            reasoning  = str(data.get("reasoning", ""))
            signals    = list(data.get("key_signals", []))

            # Validate
            prob_yes   = max(0.01, min(0.99, prob_yes))
            confidence = max(0.0,  min(1.0,  confidence))
            if direction not in ("YES", "NO", "SKIP"):
                direction = "SKIP"

            logger.info(
                "LLM decision: direction=%s  P(YES)=%.3f  confidence=%.2f  reason=%s",
                direction, prob_yes, confidence, reasoning[:80],
            )

            return TradeReasoning(
                prob_yes=prob_yes,
                direction=direction,
                confidence=confidence,
                reasoning=reasoning,
                key_signals=signals,
                raw_response=raw,
            )

        except Exception as exc:
            logger.warning("LLM response parse failed: %s | raw=%s", exc, raw[:300])
            return None

    def get_stats(self) -> dict:
        return {
            "available": self.is_available,
            "model": self._model,
            "calls": self._call_count,
            "errors": self._error_count,
        }
