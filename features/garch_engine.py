"""
GARCH(1,1) with Student-t innovations for BTC volatility regime detection.

Uses the `arch` package (Kevin Sheppard's ARCH library).

Workflow:
1. Fit GARCH(1,1)-t on a rolling window of BTC log-returns
2. Extract conditional volatility (sigma_t)
3. Flag "high vol regime" when current sigma_t > GARCH_REGIME_PERCENTILE of history
4. Bot skips trades when high_vol_regime=True

References:
    - Engle (1982) ARCH
    - Bollerslev (1986) GARCH
    - arch package: https://arch.readthedocs.io/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import pandas as pd
from arch import arch_model
from arch.univariate.base import ARCHModelResult

from config.settings import settings

logger = logging.getLogger(__name__)

# Minimum observations needed to fit GARCH reliably
MIN_OBS = 252  # ~1 day of 1m bars


@dataclass
class GarchResult:
    """Output from a GARCH fit."""

    conditional_vol: pd.Series  # sigma_t (annualized %)
    annualized_vol: float       # most recent annualized vol (%)
    high_vol_regime: bool       # True if current vol > regime_percentile
    percentile_threshold: float # vol threshold used
    params: dict                # fitted model parameters
    model_result: ARCHModelResult | None = None


def fit_garch(
    log_returns: pd.Series,
    regime_percentile: float | None = None,
) -> GarchResult:
    """
    Fit GARCH(1,1) with Student-t innovations on log-returns.

    Args:
        log_returns:       Series of log-returns (not percentage). Index should be DatetimeIndex.
        regime_percentile: Float in (0,1). Current vol > this percentile of history → high vol.
                           Defaults to settings.garch_regime_percentile.

    Returns:
        GarchResult with conditional volatility series and regime flag.

    Raises:
        ValueError: If fewer than MIN_OBS observations are provided.
    """
    if regime_percentile is None:
        regime_percentile = settings.garch_regime_percentile

    clean = log_returns.dropna()
    if len(clean) < MIN_OBS:
        raise ValueError(
            f"GARCH requires at least {MIN_OBS} observations, got {len(clean)}. "
            "Collect more historical data before trading."
        )

    # Scale returns to percentage (arch library works in %)
    returns_pct = clean * 100

    # GARCH(1,1) with Student-t distribution
    model = arch_model(
        returns_pct,
        vol="Garch",
        p=1,
        q=1,
        dist="studentst",
        rescale=False,
    )

    result = model.fit(
        disp="off",      # suppress optimizer output
        show_warning=False,
        options={"maxiter": 500},
    )

    # Conditional standard deviation (in % terms)
    cond_vol = result.conditional_volatility  # pd.Series

    # Annualize: sqrt(525960) for 1-minute bars (minutes per year)
    # Adjust if using different interval
    bars_per_year = _bars_per_year(clean.index)
    ann_factor = np.sqrt(bars_per_year)
    cond_vol_annualized = cond_vol * ann_factor

    current_vol = float(cond_vol_annualized.iloc[-1])
    threshold = float(np.percentile(cond_vol_annualized.dropna(), regime_percentile * 100))
    high_vol = current_vol > threshold

    params = {
        "omega": float(result.params.get("omega", np.nan)),
        "alpha[1]": float(result.params.get("alpha[1]", np.nan)),
        "beta[1]": float(result.params.get("beta[1]", np.nan)),
        "nu": float(result.params.get("nu", np.nan)),  # Student-t dof
        "aic": float(result.aic),
        "bic": float(result.bic),
    }

    logger.info(
        "GARCH fit: omega=%.6f α=%.4f β=%.4f ν=%.2f | "
        "current_vol=%.2f%% threshold=%.2f%% high_vol=%s",
        params["omega"], params["alpha[1]"], params["beta[1]"], params["nu"],
        current_vol, threshold, high_vol,
    )

    return GarchResult(
        conditional_vol=cond_vol_annualized,
        annualized_vol=current_vol,
        high_vol_regime=high_vol,
        percentile_threshold=threshold,
        params=params,
        model_result=result,
    )


def rolling_garch_regimes(
    close_prices: pd.Series,
    window: int = 1440,      # 1 day of 1m bars
    step: int = 60,          # refit every hour
    regime_percentile: float | None = None,
) -> pd.Series:
    """
    Compute rolling GARCH regime flags for backtesting.

    At each step, fit GARCH on the last `window` bars and flag the current
    bar as high_vol or not. Returns a boolean Series aligned with close_prices.

    Args:
        close_prices:   Close price series (DatetimeIndex, 1m bars)
        window:         Lookback window in bars for fitting GARCH
        step:           Refit interval in bars (fitting every bar is slow)
        regime_percentile: Volatility percentile threshold

    Returns:
        Boolean Series: True = high vol regime, False = normal/low vol
    """
    if regime_percentile is None:
        regime_percentile = settings.garch_regime_percentile

    log_returns = np.log(close_prices / close_prices.shift(1)).dropna()
    n = len(log_returns)

    regimes = pd.Series(False, index=log_returns.index, dtype=bool)
    fit_indices = range(window, n, step)

    for i in fit_indices:
        window_returns = log_returns.iloc[i - window : i]
        try:
            result = fit_garch(window_returns, regime_percentile)
            # Apply regime to all bars until next refit
            end = min(i + step, n)
            regimes.iloc[i:end] = result.high_vol_regime
        except Exception as exc:
            logger.warning("GARCH fit failed at bar %d: %s", i, exc)

    logger.info(
        "Rolling GARCH: %d high-vol periods out of %d total (%.1f%%)",
        regimes.sum(), len(regimes), 100 * regimes.mean(),
    )
    return regimes


def _bars_per_year(index: pd.DatetimeIndex) -> float:
    """Estimate bars per year from a DatetimeIndex frequency."""
    if len(index) < 2:
        return 525_600  # assume 1m
    delta = (index[1] - index[0]).total_seconds()
    seconds_per_year = 365.25 * 24 * 3600
    return seconds_per_year / delta
