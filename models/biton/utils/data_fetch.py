"""Shared data-fetching helpers for stocks (yfinance) and crypto (Binance / CoinGecko).

Daily-candle history and market-cap/earnings-date lookups go through the
shared on-disk cache in `utils.yf_cache` (see that module and config.py's
YF_* constants) -- process-safe and shared with the Livne (../TRADEIN)
pipeline, with retry/backoff and a stale-data fallback if Yahoo rate-limits
us. Live-price lookups (`get_stock_fast_info` and `get_recent_intraday_range`)
intentionally use a near-zero TTL on that same cache -- they always re-fetch
fresh, only falling back to a short-lived stale value if Yahoo is actively
failing, since paper-trading position management depends on these staying
current.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import pandas as pd
import requests
import yfinance as yf

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    BINANCE_BASE_URL,
    COINGECKO_BASE_URL,
    REQUEST_TIMEOUT,
    YF_CACHE_DIR,
    YF_METADATA_CACHE_SECONDS,
    YF_OHLCV_CACHE_SECONDS,
    YF_RETRY_DELAYS,
    YF_STALE_FALLBACK_SECONDS,
)
from core.utils.yf_cache import cached_call

_HISTORY_CACHE_SECONDS = 1800  # 30-minute buckets for crypto/CoinGecko daily-candle history (unaffected by yf_cache)

# Sub-hourly Binance intervals back LIVE position management -- the intraday
# high/low range used for stop/target detection (`get_recent_intraday_range`).
# They must NOT share the 30-minute history bucket: cycles run every
# CYCLE_INTERVAL_MINUTES (15), so a 30-minute bucket made every second cycle
# re-use the previous cycle's intraday data verbatim, and a stop breached in
# between was evaluated against stale bars. The equivalent stock paths already
# avoid this via the zero-TTL _live_price_cache; this is the crypto counterpart.
_INTRADAY_CACHE_SECONDS = 60
_LIVE_INTRADAY_INTERVALS = frozenset({"1m", "3m", "5m", "15m"})


def _bucket(seconds: int = _HISTORY_CACHE_SECONDS) -> int:
    return int(dt.datetime.now(dt.timezone.utc).timestamp() // seconds)


def _bucket_seconds_for(interval: str) -> int:
    """Cache-bucket width for a Binance interval: short for the live intraday
    intervals above, the normal history bucket for everything else."""
    return _INTRADAY_CACHE_SECONDS if interval in _LIVE_INTRADAY_INTERVALS else _HISTORY_CACHE_SECONDS


# ---------------------------------------------------------------------------
# Stocks (yfinance) -- see module docstring for the caching strategy
# ---------------------------------------------------------------------------
_ohlcv_cache = cached_call(YF_CACHE_DIR, YF_OHLCV_CACHE_SECONDS, YF_STALE_FALLBACK_SECONDS, YF_RETRY_DELAYS)
_metadata_cache = cached_call(YF_CACHE_DIR, YF_METADATA_CACHE_SECONDS, YF_STALE_FALLBACK_SECONDS, YF_RETRY_DELAYS)
# ttl_seconds=0: always re-fetch: live price/position-tracking data must stay fresh, never routinely served
# stale. Still gets retry/backoff, and a short (3-min) stale fallback purely as an outage safety net.
_live_price_cache = cached_call(YF_CACHE_DIR, 0, 180, YF_RETRY_DELAYS)


@_ohlcv_cache
def _fetch_stock_history(ticker: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if not df.empty:
        df.index = pd.to_datetime(df.index)
    return df


def get_stock_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    # copy() so callers that add indicator columns never mutate the cached frame
    return _fetch_stock_history(ticker, period, interval).copy()


@_metadata_cache
def _fetch_stock_market_cap(ticker: str) -> float | None:
    return yf.Ticker(ticker).fast_info.get("marketCap")


def get_stock_market_cap(ticker: str) -> float | None:
    """Market cap only, cached for YF_METADATA_CACHE_SECONDS (see config.py) --
    it barely moves intraday, unlike price. Kept separate from
    `get_stock_fast_info` below, which backs LIVE position pricing and must
    never serve a many-hours-stale value."""
    try:
        return _fetch_stock_market_cap(ticker)
    except Exception:
        return None


@_live_price_cache
def _fetch_stock_fast_info(ticker: str) -> dict:
    fi = yf.Ticker(ticker).fast_info
    return {
        "last_price": fi.get("lastPrice"),
        "market_cap": fi.get("marketCap"),
        "avg_10d_volume": fi.get("tenDayAverageVolume"),
        "avg_3mo_volume": fi.get("threeMonthAverageVolume"),
    }


def get_stock_fast_info(ticker: str) -> dict:
    """Live price + volume snapshot, used by paper_trading.tracker for
    stop/target checks -- see module docstring for why this stays on the
    near-zero-TTL live-price cache rather than the long-lived metadata one."""
    try:
        return _fetch_stock_fast_info(ticker)
    except Exception:
        return {}


@_live_price_cache
def _fetch_stock_1m_history(ticker: str) -> pd.DataFrame:
    return yf.Ticker(ticker).history(period="1d", interval="1m", auto_adjust=True)


def get_recent_intraday_range(ticker: str, asset_class: str, lookback_minutes: int) -> dict | None:
    """1-minute-candle High/Low/last-close over the last `lookback_minutes`, so
    callers can detect a stop/target touch that happened between polling
    cycles instead of only the instantaneous last-trade price."""
    try:
        if asset_class == "crypto":
            df = get_binance_klines(ticker, interval="1m", limit=lookback_minutes + 5)
        else:
            df = _fetch_stock_1m_history(ticker)
        if df is None or df.empty:
            return None
        window = df.tail(lookback_minutes + 1)
        return {
            "high": float(window["High"].max()),
            "low": float(window["Low"].min()),
            "last_close": float(window["Close"].iloc[-1]),
        }
    except Exception:
        return None


@_metadata_cache
def _fetch_next_earnings_date(ticker: str) -> dt.datetime | None:
    edf = yf.Ticker(ticker).get_earnings_dates(limit=8)
    if edf is None or edf.empty:
        return None
    now = pd.Timestamp.now(tz=edf.index.tz)
    future = edf[edf.index >= now]
    if future.empty:
        return None
    return future.index.min().to_pydatetime()


def get_next_earnings_date(ticker: str) -> dt.datetime | None:
    """Return the next upcoming earnings datetime for `ticker`, or None if unknown."""
    try:
        return _fetch_next_earnings_date(ticker)
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
    return _cached_binance_klines(symbol, interval, limit, _bucket(_bucket_seconds_for(interval))).copy()


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
