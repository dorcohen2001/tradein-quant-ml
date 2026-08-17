"""On-disk candle cache.

One file per (venue, symbol, interval), holding every candle ever downloaded
for that series. Re-running a backtest -- or running the other model's
backtest over an overlapping window -- reads from disk instead of re-hitting
Binance/Yahoo, which is what keeps long intraday windows practical at all: a
year of 5m BTCUSDT candles is ~105k rows and 105 paginated REST calls, and
nobody wants to pay that twice.

Format is Parquet when a Parquet engine is installed (much smaller and far
faster to load for 100k+ row frames) and CSV otherwise, decided once at
construction. Both round-trip a tz-naive UTC DatetimeIndex plus the five
OHLCV columns and nothing else.

The cache is deliberately append-only-ish: `merge` unions new candles with
whatever is already stored, keeping the NEWEST copy of any duplicated
timestamp (a bar that was still forming when it was first cached gets
corrected on the next run) and re-sorting. Nothing is ever deleted, so a
cached series only grows and gaps only ever get filled in.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

OHLCV_COLUMNS = ["Open", "High", "Low", "Close", "Volume"]

# Symbols reach us as tickers, and tickers contain characters Windows will not
# put in a filename ("^VIX", "BRK.B", "BTC/USDT").
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _parquet_available() -> bool:
    try:
        import pyarrow  # noqa: F401
        return True
    except Exception:
        pass
    try:
        import fastparquet  # noqa: F401
        return True
    except Exception:
        return False


def empty_frame() -> pd.DataFrame:
    """A correctly-typed empty OHLCV frame, so callers never have to special-case
    "no data" differently from "some data" when concatenating or slicing."""
    df = pd.DataFrame(columns=OHLCV_COLUMNS, dtype="float64")
    df.index = pd.DatetimeIndex([], name="timestamp")
    return df


def normalize_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce any provider's output into the canonical shape used everywhere
    downstream: a sorted, duplicate-free, tz-naive **UTC** DatetimeIndex named
    `timestamp` and exactly the five float OHLCV columns.

    The tz handling is the part that matters. Binance hands back epoch
    milliseconds (already UTC); yfinance hands back candles stamped in the
    exchange's local timezone. Left alone, a 15m AAPL bar and a 15m BTCUSDT
    bar would sit on two different clocks and any cross-asset ordering (the
    replay loop's timestamp sequence, the equity curve, "which trade closed
    first") would be quietly wrong. Everything is converted to UTC and then
    made naive, so one comparable clock is used end to end.
    """
    if df is None or len(df) == 0:
        return empty_frame()

    out = df.copy()
    idx = pd.DatetimeIndex(pd.to_datetime(out.index))
    if idx.tz is not None:
        idx = idx.tz_convert("UTC").tz_localize(None)
    out.index = idx

    for col in OHLCV_COLUMNS:
        if col not in out.columns:
            out[col] = float("nan")
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out = out[OHLCV_COLUMNS]
    out = out[~out.index.isna()]
    # keep="last": a re-fetched bar supersedes a previously cached partial one
    out = out[~out.index.duplicated(keep="last")]
    out = out.sort_index()
    out = out.dropna(subset=["Open", "High", "Low", "Close"])
    out.index.name = "timestamp"
    return out


class CandleCache:
    """File-backed store of OHLCV series, rooted at `root` (default `data/`)."""

    def __init__(self, root: str | Path, fmt: str = "auto") -> None:
        self.root = Path(root)
        if fmt == "auto":
            fmt = "parquet" if _parquet_available() else "csv"
        if fmt not in ("parquet", "csv"):
            raise ValueError(f"cache format must be 'parquet', 'csv' or 'auto', got {fmt!r}")
        self.fmt = fmt

    # -- paths --------------------------------------------------------------
    def path(self, venue: str, symbol: str, interval: str) -> Path:
        safe = _UNSAFE.sub("_", symbol)
        suffix = "parquet" if self.fmt == "parquet" else "csv"
        return self.root / venue / f"{safe}_{interval}.{suffix}"

    def meta_path(self, venue: str, symbol: str, interval: str) -> Path:
        safe = _UNSAFE.sub("_", symbol)
        return self.root / venue / f"{safe}_{interval}.meta.json"

    # -- series metadata ----------------------------------------------------
    def load_meta(self, venue: str, symbol: str, interval: str) -> dict:
        """What we have learned about this series beyond its candles.

        Currently one fact, and it exists because of a specific waste: a stock
        that IPO'd in 2023 has no history before its listing, yet a caller
        asking for five years of context requests one anyway. The request comes
        back empty, nothing is cached for that span, so the gap is STILL
        missing on the next cycle -- and is requested again, forever, at one
        wasted round trip per ticker per cycle. yfinance answers each one with
        "possibly delisted; no price data found", which is both alarming and
        wrong: the ticker is listed and trading, it simply did not exist yet.

        A corrupt or absent metadata file is an empty dict, never an error: it
        is an optimisation hint, and losing it costs one redundant request.
        """
        try:
            with open(self.meta_path(venue, symbol, interval), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

    def save_meta(self, venue: str, symbol: str, interval: str, meta: dict) -> None:
        """Persist series metadata. Best-effort and never fatal."""
        path = self.meta_path(venue, symbol, interval)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(f".{os.getpid()}.tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass

    def earliest_available(self, venue: str, symbol: str, interval: str) -> pd.Timestamp | None:
        """The oldest timestamp this venue will serve for this series, once
        learned. None means "not known yet, so ask"."""
        raw = self.load_meta(venue, symbol, interval).get("earliest_available")
        if not raw:
            return None
        try:
            return pd.Timestamp(raw)
        except (TypeError, ValueError):
            return None

    def record_earliest(self, venue: str, symbol: str, interval: str,
                        earliest: pd.Timestamp) -> None:
        """Remember that nothing exists before `earliest` for this series.

        Only ever moves the floor EARLIER-to-later on first discovery and then
        keeps the oldest known value, so a transient empty response cannot
        permanently shrink a series' known history.
        """
        meta = self.load_meta(venue, symbol, interval)
        known = self.earliest_available(venue, symbol, interval)
        if known is not None and known <= earliest:
            return
        meta["earliest_available"] = pd.Timestamp(earliest).isoformat()
        meta["earliest_recorded_at"] = pd.Timestamp.now("UTC").isoformat()
        self.save_meta(venue, symbol, interval, meta)

    # -- io -----------------------------------------------------------------
    def load(self, venue: str, symbol: str, interval: str) -> pd.DataFrame:
        """Whatever is cached for this series, or an empty frame. A corrupt or
        half-written cache file is treated as a miss rather than an error --
        the data is always re-downloadable, and a crashed run must not
        permanently break every later one."""
        path = self.path(venue, symbol, interval)
        if not path.exists():
            return empty_frame()
        try:
            if self.fmt == "parquet":
                df = pd.read_parquet(path)
            else:
                df = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception:
            return empty_frame()
        return normalize_frame(df)

    def save(self, venue: str, symbol: str, interval: str, df: pd.DataFrame) -> Path:
        path = self.path(venue, symbol, interval)
        path.parent.mkdir(parents=True, exist_ok=True)
        frame = normalize_frame(df)

        # Write to a temp sibling then replace, so an interrupted run can never
        # leave a truncated file where a valid cache used to be.
        #
        # The temp name is PID-scoped because this cache is now written from
        # SEVERAL PROCESSES AT ONCE. Every tenant runner is its own process and
        # they share this directory, so two tenants scanning the same symbol on
        # the same interval used to race on one fixed `<file>.tmp`: on Windows
        # that surfaces as a PermissionError on `replace`, or as one process
        # truncating the partially-written file of the other. `os.replace` onto
        # the final path stays atomic, so the loser of a race simply overwrites
        # with equivalent data instead of corrupting it.
        #
        # This matches what `core.utils.yf_cache` already does for the same
        # reason -- see its `cache_set`.
        tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        try:
            if self.fmt == "parquet":
                frame.to_parquet(tmp)
            else:
                frame.to_csv(tmp)
            tmp.replace(path)
        except BaseException:
            # Never leave the scratch file behind: it is PID-scoped, so nothing
            # else will ever reclaim it and it would accumulate silently.
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return path

    def merge(self, venue: str, symbol: str, interval: str, fresh: pd.DataFrame) -> pd.DataFrame:
        """Union `fresh` into the cached series, persist, and return the result."""
        if fresh is None or len(fresh) == 0:
            return self.load(venue, symbol, interval)
        cached = self.load(venue, symbol, interval)
        if len(cached) == 0:
            merged = normalize_frame(fresh)
        else:
            merged = normalize_frame(pd.concat([cached, normalize_frame(fresh)]))
        self.save(venue, symbol, interval, merged)
        return merged

    def coverage(self, venue: str, symbol: str, interval: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
        """(first, last) cached timestamp, or None when nothing is cached."""
        df = self.load(venue, symbol, interval)
        if len(df) == 0:
            return None
        return df.index[0], df.index[-1]
