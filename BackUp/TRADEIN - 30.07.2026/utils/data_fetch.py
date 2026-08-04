"""Shared data-fetching helpers for stocks (yfinance) and crypto (Binance / CoinGecko)."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import pandas as pd
import requests
import yfinance as yf

from config import BINANCE_BASE_URL, COINGECKO_BASE_URL, REQUEST_TIMEOUT


# ---------------------------------------------------------------------------
# Stocks (yfinance)
# ---------------------------------------------------------------------------
def get_stock_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index)
    return df


def get_stock_fast_info(ticker: str) -> dict:
    t = yf.Ticker(ticker)
    try:
        fi = t.fast_info
        return {
            "last_price": fi.get("lastPrice"),
            "market_cap": fi.get("marketCap"),
            "avg_10d_volume": fi.get("tenDayAverageVolume"),
            "avg_3mo_volume": fi.get("threeMonthAverageVolume"),
        }
    except Exception:
        return {}


def get_next_earnings_date(ticker: str) -> dt.datetime | None:
    """Return the next upcoming earnings datetime for `ticker`, or None if unknown."""
    t = yf.Ticker(ticker)
    try:
        edf = t.get_earnings_dates(limit=8)
        if edf is None or edf.empty:
            return None
        now = pd.Timestamp.now(tz=edf.index.tz)
        future = edf[edf.index >= now]
        if future.empty:
            return None
        return future.index.min().to_pydatetime()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Crypto (Binance public REST API - no auth required)
# ---------------------------------------------------------------------------
def get_binance_klines(symbol: str, interval: str = "1d", limit: int = 300) -> pd.DataFrame:
    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    params = {"symbol": symbol, "interval": interval, "limit": limit}
    resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    raw = resp.json()
    cols = [
        "open_time", "Open", "High", "Low", "Close", "Volume", "close_time",
        "quote_asset_volume", "num_trades", "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(raw, columns=cols)
    for c in ["Open", "High", "Low", "Close", "Volume", "quote_asset_volume"]:
        df[c] = df[c].astype(float)
    df.index = pd.to_datetime(df["open_time"], unit="ms")
    return df[["Open", "High", "Low", "Close", "Volume", "quote_asset_volume"]]


def get_binance_24hr_ticker(symbol: str) -> dict:
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr"
    resp = requests.get(url, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# CoinGecko (USDT dominance / global market data)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _cached_global(_bucket: int) -> dict:
    url = f"{COINGECKO_BASE_URL}/global"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data", {})


def get_coingecko_global() -> dict:
    """Cached per 10-minute bucket to avoid hammering the free CoinGecko API."""
    bucket = int(dt.datetime.utcnow().timestamp() // 600)
    return _cached_global(bucket)


def get_usdt_dominance_pct() -> float | None:
    data = get_coingecko_global()
    pct = data.get("market_cap_percentage", {})
    return pct.get("usdt")
