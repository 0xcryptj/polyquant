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
from typing import ClassVar

from pydantic import Field, HttpUrl, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables / .env file.

    Secrets (private keys, API secrets) are typed as `SecretStr` so they
    are never accidentally printed or logged. Access them with .get_secret_value().
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # silently ignore unknown env vars
    )

    # ── Polymarket CLOB ──────────────────────────────────────────────────────
    polymarket_api_key: str = Field(..., description="Polymarket CLOB API key")
    polymarket_api_secret: SecretStr = Field(..., description="Polymarket CLOB API secret")
    polymarket_api_passphrase: SecretStr = Field(..., description="Polymarket CLOB passphrase")
    polymarket_clob_host: str = Field(
        default="https://clob.polymarket.com",
        description="CLOB API base URL",
    )

    # ── Wallet ───────────────────────────────────────────────────────────────
    wallet_private_key: SecretStr = Field(..., description="EVM wallet private key (hex)")
    wallet_address: str = Field(..., description="EVM wallet address (0x...)")

    # ── Coinbase CDP / AgentKit ───────────────────────────────────────────────
    cdp_api_key_id: str | None = Field(default=None, description="CDP API key ID")
    cdp_api_key_secret: SecretStr | None = Field(default=None, description="CDP API key secret")
    cdp_wallet_secret: SecretStr | None = Field(default=None, description="CDP wallet secret")

    # ── RPC Endpoints ────────────────────────────────────────────────────────
    polygon_rpc_url: str = Field(..., description="Polygon mainnet RPC URL (chain_id=137)")
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
    btc_symbol: str = Field(default="BTC/USDT", description="ccxt symbol for BTC")
    candle_interval: str = Field(default="1m", description="ccxt OHLCV interval")

    # ── Trading Parameters ────────────────────────────────────────────────────
    max_daily_drawdown_pct: float = Field(
        default=0.05,
        ge=0.001,
        le=0.5,
        description="Daily drawdown limit (fraction of starting balance)",
    )
    kelly_fraction: float = Field(
        default=0.25,
        gt=0.0,
        description="Fractional Kelly multiplier",
    )
    min_edge_threshold: float = Field(
        default=0.03,
        ge=0.0,
        le=0.5,
        description="Minimum model_prob - market_price edge to trade",
    )
    max_spread: float = Field(
        default=0.04,
        ge=0.0,
        le=0.5,
        description="Maximum bid-ask spread to tolerate",
    )
    garch_regime_percentile: float = Field(
        default=0.65,
        gt=0.0,
        lt=1.0,
        description="Skip trades when realized vol > this historical percentile",
    )
    max_position_usdc: float = Field(
        default=50.0,
        gt=0.0,
        description="Hard cap: maximum USDC per single trade",
    )
    paper_trading: bool = Field(
        default=True,
        description="If True, log orders but do not submit to exchange",
    )

    # ── Market Config Path ────────────────────────────────────────────────────
    btc_markets_config: str = Field(
        default="config/btc_markets.json",
        description="Path to JSON file with BTC 5-min market token IDs",
    )

    # ── Internal constants (not from env) ───────────────────────────────────
    POLYMARKET_FEE: ClassVar[float] = 0.02  # 2% taker fee
    POLYGON_CHAIN_ID: ClassVar[int] = 137

    # ── Validators ────────────────────────────────────────────────────────────
    @field_validator("kelly_fraction")
    @classmethod
    def kelly_must_be_fractional(cls, v: float) -> float:
        if v > 1.0:
            raise ValueError(
                f"kelly_fraction={v} is dangerously high (>1.0). "
                "Use 0.25 or less for production. Full Kelly risks ruin."
            )
        return v

    @field_validator("wallet_address")
    @classmethod
    def wallet_address_must_be_hex(cls, v: str) -> str:
        if not v.startswith("0x") or len(v) != 42:
            raise ValueError(
                f"wallet_address '{v}' is not a valid EVM address. "
                "Must be 0x followed by 40 hex characters."
            )
        return v.lower()

    @field_validator("wallet_private_key")
    @classmethod
    def private_key_format(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if raw.startswith("0x"):
            raw = raw[2:]
        if len(raw) != 64:
            raise ValueError(
                "wallet_private_key must be a 64-character hex string "
                "(with or without 0x prefix)."
            )
        return v

    @model_validator(mode="after")
    def warn_if_live_trading(self) -> "Settings":
        if not self.paper_trading:
            logger.warning(
                "⚠️  PAPER_TRADING=false — bot will submit REAL orders on Polygon mainnet. "
                "Ensure you have reviewed all risk parameters before proceeding."
            )
        return self

    def safe_summary(self) -> dict:
        """Return a loggable dict with all secrets redacted."""
        return {
            "polymarket_clob_host": self.polymarket_clob_host,
            "polymarket_api_key": self.polymarket_api_key[:8] + "...",
            "wallet_address": self.wallet_address,
            "polygon_rpc_url": self.polygon_rpc_url[:40] + "...",
            "paper_trading": self.paper_trading,
            "kelly_fraction": self.kelly_fraction,
            "max_daily_drawdown_pct": self.max_daily_drawdown_pct,
            "min_edge_threshold": self.min_edge_threshold,
            "max_spread": self.max_spread,
            "max_position_usdc": self.max_position_usdc,
            "garch_regime_percentile": self.garch_regime_percentile,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings singleton. Call this everywhere."""
    return Settings()


# Module-level singleton — import this directly:
#   from config.settings import settings
settings = get_settings()
