"""
Centralized configuration using Pydantic BaseSettings.

All environment variables are validated at startup. The module exposes a single
`settings` singleton that every other module imports — never read os.environ
directly outside this file.

Usage:
    from config.settings import settings
    print(settings.polymarket_clob_host)
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import ClassVar

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Project root: config/settings.py -> config/ -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables / .env file.

    Secrets (private keys, API secrets) are typed as `SecretStr` so they
    are never accidentally printed or logged.

    PAPER TRADING MODE:
        Only TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.
        All Polymarket / wallet fields default to empty strings and are
        validated lazily (only checked when actually used for live trading).
    """

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Polymarket CLOB ──────────────────────────────────────────────────────
    polymarket_api_key: str = Field(default="", description="Polymarket CLOB API key")
    polymarket_api_secret: SecretStr = Field(default="", description="Polymarket CLOB API secret")
    polymarket_api_passphrase: SecretStr = Field(default="", description="Polymarket CLOB passphrase")
    polymarket_clob_host: str = Field(
        default="https://clob.polymarket.com",
        description="Polymarket CLOB API base URL",
    )
    polymarket_gamma_host: str = Field(
        default="https://gamma-api.polymarket.com",
        description="Polymarket Gamma API base URL (market discovery)",
    )

    # ── Wallet ───────────────────────────────────────────────────────────────
    wallet_private_key: SecretStr = Field(default="", description="EVM wallet private key (hex)")
    wallet_address: str = Field(default="0x0000000000000000000000000000000000000000", description="EVM wallet address")

    # ── Coinbase CDP / AgentKit ───────────────────────────────────────────────
    cdp_api_key_id: str | None = Field(default=None, description="CDP API key ID")
    cdp_api_key_secret: SecretStr | None = Field(default=None, description="CDP API key secret")
    cdp_wallet_secret: SecretStr | None = Field(default=None, description="CDP wallet secret")

    # ── RPC Endpoints ────────────────────────────────────────────────────────
    polygon_rpc_url: str = Field(
        default="https://polygon-rpc.com",
        description="Polygon mainnet RPC URL (chain_id=137)",
    )
    base_rpc_url: str = Field(
        default="https://mainnet.base.org",
        description="Base mainnet RPC URL",
    )
    eth_rpc_url: str | None = Field(default=None, description="Ethereum mainnet RPC URL")

    # ── Telegram ─────────────────────────────────────────────────────────────
    telegram_bot_token: SecretStr = Field(..., description="Telegram bot token from @BotFather")
    telegram_chat_id: str = Field(..., description="Telegram chat ID for notifications")

    # ── Anthropic / Claude ────────────────────────────────────────────────────
    anthropic_api_key: SecretStr | None = Field(default=None, description="Anthropic API key")

    # ── Data Collection ───────────────────────────────────────────────────────
    binance_base_url: str = Field(
        default="https://api.binance.com",
        description="Binance REST API base URL",
    )
    coinbase_exchange_url: str = Field(
        default="https://api.exchange.coinbase.com",
        description="Coinbase Exchange public REST API (no key required)",
    )
    coingecko_base_url: str = Field(
        default="https://api.coingecko.com",
        description="CoinGecko API base URL",
    )
    coingecko_api_key: str = Field(
        default="",
        description="CoinGecko Demo API key (optional, increases rate limits)",
    )
    btc_symbol: str = Field(default="BTC/USDT", description="ccxt symbol for BTC")
    candle_interval: str = Field(default="1m", description="ccxt OHLCV interval")

    # ── Trading Parameters ────────────────────────────────────────────────────
    max_daily_drawdown_pct: float = Field(default=0.05, ge=0.001, le=0.5)
    kelly_fraction: float = Field(default=0.25, gt=0.0)
    min_edge_threshold: float = Field(default=0.03, ge=0.0, le=0.5)
    max_spread: float = Field(default=0.04, ge=0.0, le=0.5)
    garch_regime_percentile: float = Field(default=0.65, gt=0.0, lt=1.0)
    max_position_usdc: float = Field(default=50.0, gt=0.0)
    paper_trading: bool = Field(default=True)

    # ── Market Config Path ────────────────────────────────────────────────────
    btc_markets_config: str = Field(default="config/btc_markets.json")

    # ── Internal constants ───────────────────────────────────────────────────
    POLYMARKET_FEE: ClassVar[float] = 0.02
    POLYGON_CHAIN_ID: ClassVar[int] = 137

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("kelly_fraction")
    @classmethod
    def kelly_must_be_fractional(cls, v: float) -> float:
        if v > 1.0:
            raise ValueError(f"kelly_fraction={v} > 1.0 — use 0.25 or less.")
        return v

    @field_validator("wallet_private_key")
    @classmethod
    def wallet_private_key_format(cls, v: SecretStr | str) -> SecretStr | str:
        """Validate EVM private key: 64 hex chars (32 bytes), optionally with 0x prefix."""
        raw = v.get_secret_value() if isinstance(v, SecretStr) else (v or "")
        if not raw:
            return v
        s = raw.strip()
        if s.startswith("0x"):
            s = s[2:]
        if len(s) != 64 or not all(c in "0123456789abcdefABCDEF" for c in s):
            raise ValueError(
                "wallet_private_key must be 64 hex characters (32 bytes), optionally with 0x prefix"
            )
        return v

    @field_validator("wallet_address")
    @classmethod
    def wallet_address_format(cls, v: str) -> str:
        # Allow the default placeholder; only validate real addresses
        if v == "0x0000000000000000000000000000000000000000":
            return v
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError(f"wallet_address '{v}' is not a valid EVM address.")
        return v.lower()

    @model_validator(mode="after")
    def warn_if_live_trading(self) -> "Settings":
        if not self.paper_trading:
            logger.warning(
                "⚠️  PAPER_TRADING=false — bot will submit REAL orders. "
                "Review all risk parameters before proceeding."
            )
        return self

    def safe_summary(self) -> dict:
        anthropic_key = None
        if self.anthropic_api_key:
            try:
                anthropic_key = "set" if self.anthropic_api_key.get_secret_value() else "not set"
            except Exception:
                anthropic_key = "not set"
        return {
            "paper_trading":        self.paper_trading,
            "polymarket_clob_host": self.polymarket_clob_host,
            "polymarket_api_key":   (self.polymarket_api_key[:8] + "…") if self.polymarket_api_key else "(not set)",
            "wallet_address":       self.wallet_address,
            "binance_base_url":     self.binance_base_url,
            "kelly_fraction":       self.kelly_fraction,
            "min_edge_threshold":   self.min_edge_threshold,
            "max_spread":           self.max_spread,
            "max_position_usdc":    self.max_position_usdc,
            "max_daily_drawdown":   f"{self.max_daily_drawdown_pct:.0%}",
            "anthropic_key":        anthropic_key or "not set",
        }

    @property
    def btc_markets_config_path(self) -> Path:
        """Resolve btc_markets_config to absolute path (relative to project root)."""
        p = Path(self.btc_markets_config)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        return p

    def is_live_ready(self) -> bool:
        """Return True if all credentials needed for live trading are present."""
        return bool(
            self.polymarket_api_key
            and self.wallet_private_key.get_secret_value()
            and self.wallet_address != "0x0000000000000000000000000000000000000000"
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()


def validate_startup(require_telegram: bool = True) -> None:
    """
    Fail fast before spawning any service.

    Validates required env vars and sane parameter ranges.
    Raises SystemExit with a clear message on failure.
    """
    import sys
    errors: list[str] = []

    if require_telegram:
        try:
            tok = settings.telegram_bot_token.get_secret_value()
        except Exception:
            tok = ""
        if not tok:
            errors.append("TELEGRAM_BOT_TOKEN is not set")
        if not settings.telegram_chat_id:
            errors.append("TELEGRAM_CHAT_ID is not set")

    if not settings.paper_trading:
        # Live trading: validate all required credentials
        if not settings.polymarket_api_key:
            errors.append("POLYMARKET_API_KEY required for live trading (PAPER_TRADING=false)")
        try:
            pk = settings.wallet_private_key.get_secret_value()
        except Exception:
            pk = ""
        if not pk:
            errors.append("WALLET_PRIVATE_KEY required for live trading")
        if settings.wallet_address == "0x0000000000000000000000000000000000000000":
            errors.append("WALLET_ADDRESS required for live trading")

    if settings.kelly_fraction > 0.5:
        errors.append(
            f"KELLY_FRACTION={settings.kelly_fraction} exceeds 0.5 — dangerously high. "
            "Use 0.1–0.25 for safety."
        )

    if settings.max_daily_drawdown_pct > 0.20:
        errors.append(
            f"MAX_DAILY_DRAWDOWN_PCT={settings.max_daily_drawdown_pct:.0%} > 20% — "
            "review risk parameters before proceeding."
        )

    if errors:
        print("\nFATAL: Configuration errors detected:\n", file=sys.stderr)
        for e in errors:
            print(f"  ✗  {e}", file=sys.stderr)
        print(
            "\nFix these in your .env file. "
            "See .env.example for all required variables.\n",
            file=sys.stderr,
        )
        sys.exit(1)

    logger.info("Config validated — all required env vars present.")
