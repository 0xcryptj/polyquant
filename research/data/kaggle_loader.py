"""
Bootstrap historical Polymarket data from Kaggle datasets.

This provides a large corpus of historical prediction market data to:
1. Train the calibration model before any live data is available
2. Backtest the full pipeline on months of historical data

Expected Kaggle dataset: polymarket historical trades / prices
(Update the dataset slug to match the current best dataset on Kaggle)
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

RAW_DIR = Path("data/raw/kaggle")
RAW_DIR.mkdir(parents=True, exist_ok=True)

KAGGLE_DATASET_SLUG = "polymarket/polymarket-historical-data"  # update as needed


def download_kaggle_dataset(dataset_slug: str = KAGGLE_DATASET_SLUG, output_dir: Path = RAW_DIR) -> Path:
    """
    Download a Kaggle dataset using the kaggle CLI.

    Prerequisites:
        pip install kaggle
        ~/.kaggle/kaggle.json with API credentials

    Args:
        dataset_slug: e.g. "username/dataset-name"
        output_dir:   Directory to unzip into.

    Returns:
        Path to the downloaded directory.
    """
    try:
        import kaggle  # noqa: F401 — just checking it's installed
    except ImportError:
        raise ImportError(
            "kaggle package not installed. Run: pip install kaggle\n"
            "Also place your Kaggle API key at ~/.kaggle/kaggle.json"
        )

    import subprocess

    cmd = [
        "kaggle", "datasets", "download",
        "-d", dataset_slug,
        "-p", str(output_dir),
        "--unzip",
    ]
    logger.info("Downloading Kaggle dataset: %s", dataset_slug)
    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise RuntimeError(f"Kaggle download failed:\n{result.stderr}")

    logger.info("Download complete: %s", output_dir)
    return output_dir


def load_kaggle_btc_markets(data_dir: Path = RAW_DIR) -> pd.DataFrame:
    """
    Load and normalize BTC 5-min market data from Kaggle dump.

    Searches for CSV/Parquet files in data_dir and returns a cleaned DataFrame
    suitable for use in the backtest pipeline.

    Expected columns after normalization:
        timestamp, token_id, price, volume, outcome (YES=1, NO=0)

    Returns:
        DataFrame sorted by timestamp.
    """
    # Try Parquet first (faster), fall back to CSV
    parquet_files = list(data_dir.glob("**/*.parquet"))
    csv_files = list(data_dir.glob("**/*.csv"))

    if parquet_files:
        logger.info("Loading %d Parquet files from %s", len(parquet_files), data_dir)
        dfs = [pd.read_parquet(f) for f in parquet_files]
    elif csv_files:
        logger.info("Loading %d CSV files from %s", len(csv_files), data_dir)
        dfs = [pd.read_csv(f) for f in csv_files]
    else:
        raise FileNotFoundError(
            f"No Parquet or CSV files found in {data_dir}.\n"
            "Run: python scripts/run_backtest.py --download-data  or\n"
            "     python -c 'from data.kaggle_loader import download_kaggle_dataset; download_kaggle_dataset()'"
        )

    df = pd.concat(dfs, ignore_index=True)
    df = _normalize_columns(df)
    df = _filter_btc_5min(df)
    df = df.sort_values("timestamp").reset_index(drop=True)
    logger.info("Loaded %d rows of BTC 5-min Kaggle data", len(df))
    return df


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column names to our internal schema."""
    col_map = {
        # common Kaggle Polymarket column names → internal names
        "market_slug": "market_slug",
        "token_id": "token_id",
        "outcome": "outcome",
        "price": "price",
        "timestamp": "timestamp",
        "created_at": "timestamp",
        "updated_at": "timestamp",
        "volume": "volume",
        "liquidity": "liquidity",
        "end_date_iso": "end_date",
        "question": "question",
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        df = df.dropna(subset=["timestamp"])

    return df


def _filter_btc_5min(df: pd.DataFrame) -> pd.DataFrame:
    """Filter to rows matching BTC 5-minute up/down markets."""
    if "question" not in df.columns and "market_slug" not in df.columns:
        logger.warning("No 'question' or 'market_slug' column — returning all data without BTC filter")
        return df

    search_col = "question" if "question" in df.columns else "market_slug"
    mask = df[search_col].str.contains(
        r"bitcoin|btc",
        case=False,
        na=False,
        regex=True,
    ) & df[search_col].str.contains(
        r"5.?min|5-min|5_min|five.?minute",
        case=False,
        na=False,
        regex=True,
    )
    filtered = df[mask].copy()
    logger.info("BTC 5-min filter: %d / %d rows", len(filtered), len(df))
    return filtered


def prepare_training_data(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series]:
    """
    Prepare feature matrix X and outcome label y for model training.

    Returns:
        X: DataFrame with feature columns
        y: Series with binary outcome (1=YES resolved, 0=NO)
    """
    required = {"price", "timestamp"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"DataFrame missing required columns: {missing}")

    # Outcome: market resolved YES = 1, NO = 0
    if "outcome" in df.columns:
        y = df["outcome"].map({"YES": 1, "NO": 0, 1: 1, 0: 0}).astype(int)
    else:
        raise ValueError("No 'outcome' column found. Cannot create labels without ground truth.")

    X = df[["price"]].copy()  # minimal features; feature_builder.py adds more
    return X, y
