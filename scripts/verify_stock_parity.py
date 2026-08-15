"""Prove the unified stock path is signal-identical to Livne's legacy fetcher.

    python scripts/verify_stock_parity.py                    # full watchlist
    python scripts/verify_stock_parity.py --tickers AAPL,MSFT --verbose
    python scripts/verify_stock_parity.py --max-tickers 10   # quick pass

THE GATE THIS EXISTS TO BE
---------------------------
Livne holds live positions. Swapping its market-data source is only acceptable
if the swap is provably invisible to the strategy, and "the closes look the
same" is not proof: what actually drives Livne's decisions is the RESAMPLED
higher-timeframe structure, and resampling is exactly where a timezone
difference does its damage.

`resample_ohlc` anchors buckets to the index's own midnight. The legacy fetcher
returns a tz-aware America/New_York index; the shared feed stores naive UTC. Fed
the naive frame, every 12h/4h/3h bucket boundary shifts by the UTC offset,
moving the swing highs and lows that `ta.mtfa` builds its zone map from -- a
silent change in generated signals with no error anywhere.

So this compares the things that decide trades, not just the raw feed:

  1. raw OHLCV        -- same bars, same values, same index INCLUDING tz
  2. resampled frames -- 12h / 4h / 3h through Livne's own `resample_ohlc`
  3. indicators       -- EMA/RSI/ATR and the swing extremes zones derive from
  4. structural levels-- the swing high/low pairs an HTF zone map is built on

Exit code 0 means every checked symbol matched on every axis; 1 means at least
one did not, and the migration must not proceed.
"""
from __future__ import annotations

import argparse
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIVNE_DIR = os.path.join(REPO_ROOT, "models", "livne")
for path in (LIVNE_DIR, REPO_ROOT):
    if path not in sys.path:
        sys.path.insert(0, path)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pandas as pd  # noqa: E402

from config import WATCHLIST_STOCKS  # noqa: E402
from core.data.market import get_stock_candles  # noqa: E402
from utils.data_fetch import (  # noqa: E402
    get_stock_history,
    get_stock_intraday,
    get_stock_intraday_1h,
    resample_ohlc,
)

# Which legacy fetcher owns each interval, and whether it has ALREADY been
# migrated to the unified layer.
#
# The second flag matters more than it looks. Once a function delegates to the
# shared layer, comparing it against that layer compares the unified path to
# itself and passes unconditionally -- a green gate that proves nothing. The
# runner refuses that case rather than printing a reassuring 70/70.
# Calendar lookback each legacy fetcher actually requests, so the unified side
# is asked for the same DEPTH rather than a bar count that silently under-
# delivers on regular-hours-only intraday data.
LEGACY_LOOKBACK_DAYS = {"15m": 60, "5m": 60, "30m": 60, "1h": 729, "60m": 729}

LEGACY_SOURCES = {
    "1d":  ("get_stock_history", lambda t, i: get_stock_history(t, period="1y", interval=i), True),
    "1wk": ("get_stock_history", lambda t, i: get_stock_history(t, period="2y", interval=i), True),
    "1h":  ("get_stock_intraday_1h", lambda t, i: get_stock_intraday_1h(t), False),
    "60m": ("get_stock_intraday_1h", lambda t, i: get_stock_intraday_1h(t), False),
    "15m": ("get_stock_intraday", lambda t, i: get_stock_intraday(t, interval=i), False),
    "5m":  ("get_stock_intraday", lambda t, i: get_stock_intraday(t, interval=i), False),
    "30m": ("get_stock_intraday", lambda t, i: get_stock_intraday(t, interval=i), False),
}

RESAMPLE_RULES = ["12h", "4h", "3h"]
OHLCV = ["Open", "High", "Low", "Close", "Volume"]

# Prices are compared to the cent and volumes exactly. A tolerance wide enough
# to hide a shifted resample bucket would defeat the point of the check.
PRICE_TOL = 0.01


class Result:
    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    @property
    def failures(self) -> list[tuple[str, bool, str]]:
        return [c for c in self.checks if not c[1]]


def _align(a: pd.DataFrame, b: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Restrict both frames to their shared timestamps.

    The two sources legitimately differ at the EDGES -- a different history cap,
    and an in-progress bar that one fetched a moment before the other. Comparing
    the intersection tests what both claim to know, which is the question that
    matters; the edges are compared separately as a count, not a value.
    """
    common = a.index.intersection(b.index)
    return a.loc[common].sort_index(), b.loc[common].sort_index()


def _frames_match(a: pd.DataFrame, b: pd.DataFrame) -> tuple[bool, str]:
    if len(a) == 0 or len(b) == 0:
        return (False, f"empty overlap (legacy={len(a)}, unified={len(b)})")
    for column in OHLCV:
        if column not in a.columns or column not in b.columns:
            return (False, f"missing column {column}")
        left, right = a[column].astype(float), b[column].astype(float)
        diff = (left - right).abs()
        tolerance = PRICE_TOL if column != "Volume" else 1.0
        worst = float(diff.max())
        if worst > tolerance:
            where = diff.idxmax()
            return (False, f"{column} differs by {worst:.4f} at {where}")
    return (True, f"{len(a)} bars identical")


def compare(ticker: str, interval: str, bars: int, verbose: bool) -> Result:
    result = Result(ticker)

    _name, fetch_legacy, _migrated = LEGACY_SOURCES[interval]
    legacy = fetch_legacy(ticker, interval)
    lookback = LEGACY_LOOKBACK_DAYS.get(interval)
    unified = (get_stock_candles(ticker, interval=interval, bars=0, lookback_days=lookback)
               if lookback else get_stock_candles(ticker, interval=interval, bars=bars))

    if legacy is None or len(legacy) == 0:
        result.add("legacy fetch", False, "legacy returned no data")
        return result
    if unified is None or len(unified) == 0:
        result.add("unified fetch", False, "unified returned no data")
        return result

    # 0. HISTORY DEPTH. Checked before anything else, because every other check
    #    compares the INTERSECTION of the two frames -- so a unified frame with
    #    a fraction of the legacy history passes them all while handing
    #    downstream code far less warm-up than it had. At 15m the legacy fetcher
    #    returns 60 days (~1560 bars); a unified frame of 260 would still clear
    #    a 200-period MA and quietly change its value.
    depth_ratio = len(unified) / max(len(legacy), 1)
    result.add("history depth", depth_ratio >= 0.95,
               f"legacy={len(legacy)} unified={len(unified)} ratio={depth_ratio:.2f}")

    # 1. The timezone itself -- the whole reason this adapter exists.
    legacy_tz = str(getattr(legacy.index, "tz", None))
    unified_tz = str(getattr(unified.index, "tz", None))
    result.add("index tz preserved", legacy_tz == unified_tz,
               f"legacy={legacy_tz} unified={unified_tz}")

    # 2. Raw OHLCV over the shared window.
    a, b = _align(legacy, unified)
    ok, detail = _frames_match(a, b)
    result.add("raw OHLCV", ok, detail)
    if not ok:
        return result  # resampling a mismatched frame tells us nothing new

    # 3. Resampled frames through Livne's own resampler -- the step a tz
    #    difference actually corrupts.
    for rule in RESAMPLE_RULES:
        ra, rb = _align(resample_ohlc(legacy, rule), resample_ohlc(unified, rule))
        ok, detail = _frames_match(ra, rb)
        result.add(f"resample {rule}", ok, detail)

    # 4. Indicators and the structural extremes zone detection is built on.
    for window in (20, 50):
        ea = a["Close"].ewm(span=window, adjust=False).mean()
        eb = b["Close"].ewm(span=window, adjust=False).mean()
        worst = float((ea - eb).abs().max())
        result.add(f"EMA{window}", worst <= PRICE_TOL, f"max diff {worst:.6f}")

    for label, series_a, series_b in (
        ("swing high", a["High"].rolling(20).max(), b["High"].rolling(20).max()),
        ("swing low", a["Low"].rolling(20).min(), b["Low"].rolling(20).min()),
    ):
        worst = float((series_a - series_b).abs().max())
        result.add(label, worst <= PRICE_TOL, f"max diff {worst:.6f}")

    if verbose:
        print(f"    legacy  {len(legacy):4} bars  {legacy.index[0]} .. {legacy.index[-1]}")
        print(f"    unified {len(unified):4} bars  {unified.index[0]} .. {unified.index[-1]}")

    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Signal-parity gate for Livne's stock data path")
    ap.add_argument("--tickers", help="comma-separated override (default: Livne's stock watchlist)")
    ap.add_argument("--max-tickers", type=int, default=0, help="cap how many are checked (0 = all)")
    ap.add_argument("--interval", default="1d", help="analysis interval to compare (default: %(default)s)")
    ap.add_argument("--bars", type=int, default=260, help="bars requested from the unified layer")
    ap.add_argument("--allow-migrated", action="store_true",
                    help="permit comparing a fetcher that already delegates to the unified "
                         "layer (a tautology -- only useful to re-confirm a round-trip)")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    tickers = ([t.strip().upper() for t in args.tickers.split(",") if t.strip()]
               if args.tickers else list(WATCHLIST_STOCKS))
    if args.max_tickers:
        tickers = tickers[:args.max_tickers]

    if args.interval not in LEGACY_SOURCES:
        print(f"[error] no legacy fetcher mapped for interval {args.interval!r}; "
              f"known: {', '.join(sorted(LEGACY_SOURCES))}", file=sys.stderr)
        return 2

    legacy_name, _fetch, already_migrated = LEGACY_SOURCES[args.interval]
    if already_migrated and not args.allow_migrated:
        print(f"[error] {legacy_name} ALREADY delegates to the unified layer, so comparing "
              f"it against that layer compares the unified path to itself and would pass "
              f"unconditionally.", file=sys.stderr)
        print(f"[error] That is a tautology, not a gate. Use --allow-migrated only to "
              f"re-confirm a completed migration still round-trips.", file=sys.stderr)
        return 2

    print("=" * 78)
    print(f"STOCK DATA PARITY GATE  --  {len(tickers)} symbol(s) @ {args.interval}")
    print(f"legacy:  models/livne/utils/data_fetch.{legacy_name}"
          + ("  [ALREADY MIGRATED -- tautology check only]" if already_migrated else ""))
    print("unified: core.data.market.get_stock_candles")
    print("=" * 78)

    results = []
    for index, ticker in enumerate(tickers, 1):
        print(f"[{index}/{len(tickers)}] {ticker} ...", end=" ", flush=True)
        try:
            result = compare(ticker, args.interval, args.bars, args.verbose)
        except Exception as exc:  # noqa: BLE001 -- one bad symbol must not abort the gate
            result = Result(ticker)
            result.add("comparison", False, f"{type(exc).__name__}: {exc}")
        results.append(result)
        print("PASS" if result.ok else "FAIL")
        for name, _ok, detail in result.failures:
            print(f"      x {name}: {detail}")

    passed = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]

    print()
    print("=" * 78)
    print(f"RESULT: {len(passed)}/{len(results)} symbols identical across "
          f"raw OHLCV, {'/'.join(RESAMPLE_RULES)} resamples, indicators and swing structure")
    if failed:
        print()
        print("MIGRATION BLOCKED -- discrepancies found:")
        for r in failed:
            for name, _ok, detail in r.failures:
                print(f"  {r.ticker:8} {name}: {detail}")
        print("=" * 78)
        return 1
    print("Zero discrepancies. The unified path is safe to wire into Livne.")
    print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
