"""Fetch OHLCV market data for a parsed TradeSetup via yfinance."""
from __future__ import annotations

import pandas as pd
import yfinance as yf

from setup_parser import TradeSetup

# How much history to request per interval — enough warm-up for an EMA 200
# plus a comfortable display window.
_PERIODS = {
    "15m": "60d",
    "30m": "60d",
    "1h": "720d",
    "1d": "600d",
    "1wk": "10y",
}


def fetch_ohlcv(setup: TradeSetup) -> pd.DataFrame:
    period = _PERIODS.get(setup.interval, "600d")
    df = yf.download(
        setup.yf_symbol,
        period=period,
        interval=setup.interval,
        auto_adjust=True,
        progress=False,
    )
    if df is None or df.empty:
        raise RuntimeError(
            f"No market data returned for {setup.yf_symbol} "
            f"(interval={setup.interval}, period={period})."
        )

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)[["Open", "High", "Low", "Close", "Volume"]]

    if setup.resample:
        df = (
            df.resample(setup.resample)
            .agg({"Open": "first", "High": "max", "Low": "min",
                  "Close": "last", "Volume": "sum"})
            .dropna()
        )

    df = df.dropna(subset=["Open", "High", "Low", "Close"])
    return df
