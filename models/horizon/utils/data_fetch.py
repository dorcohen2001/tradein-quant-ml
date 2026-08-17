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
    YF_INTRADAY_CACHE_SECONDS,
    YF_INTRADAY_STALE_FALLBACK_SECONDS,
    YF_METADATA_CACHE_SECONDS,
    YF_OHLCV_CACHE_SECONDS,
    YF_RETRY_DELAYS,
    YF_STALE_FALLBACK_SECONDS,
)
from core.data.cache_policy import HISTORY_CACHE_SECONDS, bucket, bucket_seconds_for
from core.data.health import note_failure, note_success
from core.data.market import get_stock_candles
from core.utils.yf_cache import cached_call

# Cache-bucket policy is shared (core.data.cache_policy): a cache entry must
# never outlive the bar it describes. The two models had drifted -- Biton
# lacked the 1h-4h middle tier, so a 1h frame was cached for a full 30 minutes
# and every second cycle re-used the previous cycle's bars verbatim.
_HISTORY_CACHE_SECONDS = HISTORY_CACHE_SECONDS


def _bucket(seconds: int = _HISTORY_CACHE_SECONDS) -> int:
    return bucket(seconds)


def _bucket_seconds_for(interval: str) -> int:
    return bucket_seconds_for(interval)


# ---------------------------------------------------------------------------
# Stocks (yfinance) -- see module docstring for the caching strategy
# ---------------------------------------------------------------------------
_ohlcv_cache = cached_call(YF_CACHE_DIR, YF_OHLCV_CACHE_SECONDS, YF_STALE_FALLBACK_SECONDS, YF_RETRY_DELAYS)
_metadata_cache = cached_call(YF_CACHE_DIR, YF_METADATA_CACHE_SECONDS, YF_STALE_FALLBACK_SECONDS, YF_RETRY_DELAYS)
# ttl_seconds=0: always re-fetch: live price/position-tracking data must stay fresh, never routinely served
# stale. Still gets retry/backoff, and a short (3-min) stale fallback purely as an outage safety net.
_live_price_cache = cached_call(YF_CACHE_DIR, 0, 180, YF_RETRY_DELAYS)
# Execution-timeframe bars (15m/5m): short TTL so the trigger candle is never
# evaluated from a bar that has already closed. See config's YF_INTRADAY_*.
_intraday_cache = cached_call(
    YF_CACHE_DIR, YF_INTRADAY_CACHE_SECONDS, YF_INTRADAY_STALE_FALLBACK_SECONDS, YF_RETRY_DELAYS,
)


@_ohlcv_cache
def _fetch_stock_history(ticker: str, period: str, interval: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if not df.empty:
        df.index = pd.to_datetime(df.index)
    return df


def get_stock_history(ticker: str, period: str = "1y", interval: str = "1d") -> pd.DataFrame:
    """Daily/weekly stock context bars, now served by the SHARED data layer.

    Delegates to `core.data.market.get_stock_candles`, so a frame fetched by one
    tenant is served from the shared cache to every other tenant instead of
    being downloaded again per model.

    The adapter re-attaches the America/New_York timezone before returning. That
    is load-bearing rather than cosmetic: `resample_ohlc` below anchors its
    buckets to the index's own midnight, so a naive-UTC index would shift every
    12h/4h/3h boundary and move the swing extremes `ta.mtfa` builds its zone map
    from -- a silent change in generated signals.

    Verified identical to the previous implementation across all 70 watchlist
    symbols on raw OHLCV, the 12h/4h/3h resamples, EMA20/50 and 20-bar swing
    high/low, with the index timezone preserved:
    `python scripts/verify_stock_parity.py`.

    NOT migrated: `get_stock_intraday` / `get_stock_intraday_1h` below still use
    the legacy path. They feed the 15m/5m/1h execution ladder and the parity gate
    has only been run at 1d, so they need their own run at those intervals first.

    copy() so callers that add indicator columns never mutate the cached frame.
    """
    frame = get_stock_candles(ticker, interval=interval, period=period)
    if frame is None or len(frame) == 0:
        # Preserve the old contract: an empty frame with the expected columns,
        # so every downstream `len(df) < N` guard behaves exactly as before.
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
    return frame.copy()


@_metadata_cache
def _fetch_stock_market_cap(ticker: str) -> float | None:
    return yf.Ticker(ticker).fast_info.get("marketCap")


def get_stock_market_cap(ticker: str) -> float | None:
    """Market cap only, cached for YF_METADATA_CACHE_SECONDS (see config.py) --
    it barely moves intraday, unlike price. Kept separate from
    `get_stock_fast_info` below, which backs LIVE position pricing and must
    never serve a many-hours-stale value."""
    try:
        value = _fetch_stock_market_cap(ticker)
        note_success("yahoo")
        return value
    except Exception as exc:
        note_failure("yahoo", "market_cap", exc, ticker)
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
        info = _fetch_stock_fast_info(ticker)
        note_success("yahoo")
        return info
    except Exception as exc:
        # Live position pricing. A silent {} here reads downstream as "no price
        # available", which is exactly what a delisted ticker looks like too.
        note_failure("yahoo", "fast_info", exc, ticker)
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
        result = {
            "high": float(window["High"].max()),
            "low": float(window["Low"].min()),
            "last_close": float(window["Close"].iloc[-1]),
        }
        note_success("binance" if asset_class == "crypto" else "yahoo")
        return result
    except Exception as exc:
        # This one feeds stop/target checks. Returning None silently means the
        # level test falls back to the last price alone -- a real degradation
        # that used to leave no trace at all.
        note_failure("binance" if asset_class == "crypto" else "yahoo",
                     "intraday_range", exc, ticker)
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
    """1H OHLCV history for `ticker` -- the base resolution the coarser stock
    zone frames (12H/4H/3H) are resampled from, since yfinance offers none of
    them natively."""
    return _fetch_stock_intraday_1h(ticker).copy()


@_intraday_cache
def _fetch_stock_intraday(ticker: str, interval: str, period: str) -> pd.DataFrame:
    df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def get_stock_intraday(ticker: str, interval: str = "15m", period: str | None = None) -> pd.DataFrame:
    """Sub-hourly stock OHLCV for the execution timeframes (15m trigger, 5m
    confirmation).

    yfinance caps sub-hourly history: 1m to ~7 days, everything else from 2m
    to 90m to ~60 days. `period` defaults to the largest window the requested
    interval actually allows, which still leaves ample bars for a 200-period
    MA (200 x 15m is under 8 trading days; 200 x 5m is under 3)."""
    if period is None:
        period = "7d" if interval == "1m" else "60d"
    return _fetch_stock_intraday(ticker, interval, period).copy()


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
        if df is None or df.empty:
            return None
        note_success("binance" if asset_class == "crypto" else "yahoo")
        return df
    except Exception as exc:
        note_failure("binance" if asset_class == "crypto" else "yahoo",
                     "intraday_candles", exc, ticker)
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
        value = _fetch_next_earnings_date(ticker)
        note_success("yahoo")
        return value
    except Exception as exc:
        note_failure("yahoo", "earnings_date", exc, ticker)
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
        book = {
            "bids": [(float(p), float(q)) for p, q in data.get("bids", [])],
            "asks": [(float(p), float(q)) for p, q in data.get("asks", [])],
        }
        note_success("binance")
        return book
    except Exception as exc:
        note_failure("binance", "orderbook_depth", exc, symbol)
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
        book = {
            "bids": [(float(p), float(q)) for p, q in result.get("b", [])],
            "asks": [(float(p), float(q)) for p, q in result.get("a", [])],
        }
        note_success("bybit")
        return book
    except Exception as exc:
        note_failure("bybit", "orderbook_depth", exc, symbol)
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
