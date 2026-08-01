"""Shared data-fetching helpers for stocks (yfinance) and crypto (Binance / CoinGecko).

Daily-candle history fetches are memoized per 30-minute bucket: within one
scan cycle the same benchmark (SPY/QQQ/BTC/ETH) is requested by the macro
analyzer, the screener, and the watchlist loop — the cache collapses those
into a single HTTP call, while still refreshing between scheduled scans.
Live-price lookups (fast_info / 24hr ticker) are intentionally NOT cached.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import pandas as pd
import requests
import yfinance as yf

from config import BINANCE_BASE_URL, COINGECKO_BASE_URL, REQUEST_TIMEOUT

_HISTORY_CACHE_SECONDS = 1800  # 30-minute buckets for daily-candle history


def _bucket(seconds: int = _HISTORY_CACHE_SECONDS) -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() // seconds)


# ---------------------------------------------------------------------------
# Stocks (yfinance)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=256)
def _cached_stock_history(ticker: str, period: str, interval: str, _bucket_id: int) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index)
    return df


def get_stock_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    # copy() so callers that add indicator columns never mutate the cached frame
    return _cached_stock_history(ticker, period, interval, _bucket()).copy()


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
@lru_cache(maxsize=128)
def _cached_binance_klines(symbol: str, interval: str, limit: int, _bucket_id: int) -> pd.DataFrame:
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


def get_binance_klines(symbol: str, interval: str = "1d", limit: int = 300) -> pd.DataFrame:
    return _cached_binance_klines(symbol, interval, limit, _bucket()).copy()


def get_binance_24hr_ticker(symbol: str) -> dict:
    url = f"{BINANCE_BASE_URL}/api/v3/ticker/24hr"
    resp = requests.get(url, params={"symbol": symbol}, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# CoinGecko (USDT dominance / global market data)
# ---------------------------------------------------------------------------
@lru_cache(maxsize=1)
def _cached_global(_bucket_id: int) -> dict:
    url = f"{COINGECKO_BASE_URL}/global"
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json().get("data", {})


def get_coingecko_global() -> dict:
    """Cached per 10-minute bucket to avoid hammering the free CoinGecko API."""
    return _cached_global(_bucket(600))


def get_usdt_dominance_pct() -> float | None:
    data = get_coingecko_global()
    pct = data.get("market_cap_percentage", {})
    return pct.get("usdt")
