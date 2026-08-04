"""Shared data-fetching helpers for stocks (yfinance) and crypto (Binance / CoinGecko).

Daily-candle/intraday-1H history and market-cap/earnings-date lookups go
through the shared on-disk cache in `utils.yf_cache` (see that module and
config.py's YF_* constants) -- process-safe and shared between the Livne and
Biton pipelines, with retry/backoff and a stale-data fallback if Yahoo
rate-limits us. Live-price lookups (`get_stock_fast_info` and the intraday
range/candle helpers below) intentionally use a near-zero TTL on that same
cache -- they always re-fetch fresh, only falling back to a short-lived
stale value if Yahoo is actively failing, since paper-trading position
management depends on these staying current.
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
    BYBIT_BASE_URL,
    COINGECKO_BASE_URL,
    ORDERBOOK_DEPTH_LIMIT,
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
# high/low range used for stop/target detection (`get_recent_intraday_range`)
# and the candles used for emergency-reversal detection
# (`get_recent_intraday_candles`). They must NOT share the 30-minute history
# bucket: cycles run every CYCLE_INTERVAL_MINUTES (15), so a 30-minute bucket
# made every second cycle re-use the previous cycle's intraday data verbatim,
# and a stop breached in between was evaluated against stale bars. The
# equivalent stock paths already avoid this via the zero-TTL _live_price_cache;
# this is the crypto counterpart.
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


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate a finer-grained OHLCV frame up to a coarser bar size (e.g. 1H
    bars -> "4h"/"8h"/"12h"), used for stock ITF timeframes that yfinance
    doesn't expose natively. Trailing partial bar (still forming) is dropped
    so callers never analyze an incomplete candle."""
    if df.empty:
        return df
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if "Volume" in df.columns:
        agg["Volume"] = "sum"
    out = df.resample(rule).agg(agg).dropna(subset=["Open", "High", "Low", "Close"])
    if len(out) and df.index[-1] < out.index[-1] + pd.tseries.frequencies.to_offset(rule):
        out = out.iloc[:-1]
    return out


@_ohlcv_cache
def _fetch_stock_intraday_1h(ticker: str) -> pd.DataFrame:
    # 60m interval is capped by yfinance to roughly the last 730 days.
    df = yf.Ticker(ticker).history(period="729d", interval="60m", auto_adjust=True)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def get_stock_intraday_1h(ticker: str) -> pd.DataFrame:
    """1H OHLCV history for `ticker`, the base resolution stock ITF (4H/8H/12H)
    bars are resampled from."""
    return _fetch_stock_intraday_1h(ticker).copy()


@_live_price_cache
def _fetch_stock_5m_history(ticker: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period="1d", interval="5m", auto_adjust=True)
    if df is not None and not df.empty:
        df.index = pd.to_datetime(df.index)
        df = df[["Open", "High", "Low", "Close", "Volume"]]
    return df


def get_recent_intraday_candles(ticker: str, asset_class: str, limit: int = 30) -> pd.DataFrame | None:
    """Recent 5-minute OHLCV bars (not just the aggregated hi/lo/close range
    from `get_recent_intraday_range`) so callers can run per-candle checks
    like emergency-reversal pattern + volume-spike detection."""
    try:
        if asset_class == "crypto":
            df = get_binance_klines(ticker, interval="5m", limit=limit)
        else:
            df = _fetch_stock_5m_history(ticker)
            if df is None or df.empty:
                return None
            df = df.tail(limit)
        return df if df is not None and not df.empty else None
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


def get_binance_orderbook_depth(symbol: str, limit: int = ORDERBOOK_DEPTH_LIMIT) -> dict | None:
    """Raw {"bids": [[price, qty], ...], "asks": [...]} from Binance's public
    depth endpoint (no auth required). Intentionally NOT cached — an order
    book is only meaningful as a live snapshot."""
    try:
        url = f"{BINANCE_BASE_URL}/api/v3/depth"
        resp = requests.get(url, params={"symbol": symbol, "limit": limit}, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return {
            "bids": [(float(p), float(q)) for p, q in data.get("bids", [])],
            "asks": [(float(p), float(q)) for p, q in data.get("asks", [])],
        }
    except Exception:
        return None


def get_bybit_orderbook_depth(symbol: str, limit: int = 200) -> dict | None:
    """Same shape as `get_binance_orderbook_depth`, from Bybit's public v5
    market orderbook endpoint (spot category, no auth required). Bybit caps
    depth at 200 levels for spot."""
    try:
        url = f"{BYBIT_BASE_URL}/v5/market/orderbook"
        resp = requests.get(
            url, params={"category": "spot", "symbol": symbol, "limit": min(limit, 200)},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {})
        return {
            "bids": [(float(p), float(q)) for p, q in result.get("b", [])],
            "asks": [(float(p), float(q)) for p, q in result.get("a", [])],
        }
    except Exception:
        return None


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


def get_total_crypto_market_cap() -> float | None:
    """Total crypto market cap in USD (the "TOTAL" chart), from the same
    CoinGecko /global payload used for USDT dominance."""
    data = get_coingecko_global()
    return data.get("total_market_cap", {}).get("usd")
