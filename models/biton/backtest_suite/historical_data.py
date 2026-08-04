"""Historical OHLCV dynamic slicer.

Fetches each asset's full price history exactly once, then hands out
point-in-time "as of date T" slices for the day-by-day backtest loop. This is
the mechanism that prevents lookahead bias: nothing downstream (ta_engine,
market_screener scoring, macro regime replication) ever sees a row dated
after T, because the DataFrame it receives simply doesn't contain one.

Kept fully separate from utils/data_fetch.py (used by the live pipeline) so
the production code path is never touched or imported for mutation here --
only yfinance / Binance's public REST API are hit directly, mirroring the
same libraries the live modules use.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd
import requests
import yfinance as yf

from config import BINANCE_BASE_URL, REQUEST_TIMEOUT

# Calendar-day buffer fetched *before* the requested start date so that on
# day 1 of the backtest window there is already enough history for a 200-day
# EMA, the swing-high/low lookback (60), and the pivot lookback (120).
WARMUP_CALENDAR_DAYS = 400

# Calendar-day buffer fetched *after* the requested end date so a signal
# generated on the last day of the window still has forward bars available
# to simulate against.
FORWARD_CALENDAR_DAYS = 30


def fetch_stock_history(ticker: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Full daily OHLCV for `ticker` covering [start - warmup, end + forward]."""
    warmup_start = start - dt.timedelta(days=WARMUP_CALENDAR_DAYS)
    fetch_end = end + dt.timedelta(days=FORWARD_CALENDAR_DAYS + 1)
    df = yf.Ticker(ticker).history(
        start=warmup_start.isoformat(), end=fetch_end.isoformat(),
        interval="1d", auto_adjust=True,
    )
    if df.empty:
        return df
    df.index = pd.to_datetime(df.index).tz_localize(None)
    return df[["Open", "High", "Low", "Close", "Volume"]]


def fetch_crypto_klines(symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
    """Full daily OHLCV for `symbol` covering [start - warmup, end + forward].

    Paginates Binance's 1000-candle-per-request cap using startTime/endTime
    so arbitrarily long historical windows can be requested.
    """
    warmup_start = dt.datetime.combine(start - dt.timedelta(days=WARMUP_CALENDAR_DAYS), dt.time.min)
    fetch_end = dt.datetime.combine(end + dt.timedelta(days=FORWARD_CALENDAR_DAYS + 1), dt.time.min)
    start_ms = int(warmup_start.timestamp() * 1000)
    end_ms = int(fetch_end.timestamp() * 1000)

    url = f"{BINANCE_BASE_URL}/api/v3/klines"
    frames: list = []
    cursor = start_ms
    while cursor < end_ms:
        params = {"symbol": symbol, "interval": "1d", "startTime": cursor, "endTime": end_ms, "limit": 1000}
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        batch = resp.json()
        if not batch:
            break
        frames.extend(batch)
        next_cursor = batch[-1][0] + 1
        if next_cursor <= cursor or len(batch) < 1000:
            break
        cursor = next_cursor

    if not frames:
        return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])

    cols = [
        "open_time", "Open", "High", "Low", "Close", "Volume", "close_time",
        "quote_asset_volume", "num_trades", "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(frames, columns=cols)
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df.index = pd.to_datetime(df["open_time"], unit="ms")
    df = df[~df.index.duplicated(keep="first")]
    return df[["Open", "High", "Low", "Close", "Volume"]]


def slice_upto(df: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """The time-machine cut: only rows dated on or before `as_of`."""
    return df.loc[df.index <= as_of]


def trading_dates(df: pd.DataFrame, start: dt.date, end: dt.date) -> list[pd.Timestamp]:
    """Dates within [start, end] present in `df`'s own index (its trading calendar)."""
    mask = (df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))
    return list(df.index[mask])


def forward_window(df: pd.DataFrame, signal_date: pd.Timestamp, n_days: int) -> pd.DataFrame:
    """Rows strictly after `signal_date`, up to `n_days` trading sessions -- the
    only data the simulator is allowed to look at when grading a trade."""
    future = df.loc[df.index > signal_date]
    return future.iloc[:n_days]
