"""Horizon's baseline report: both watchlists, both directions, one table.

    python -m backtest_suite.baseline --start-date 2026-06-01 --end-date 2026-08-01

WHY A DRIVER AND NOT JUST `run_backtest`
----------------------------------------
`run_backtest` already scans both watchlists and already produces LONG and
SHORT setups -- direction is derived from the zone map, not chosen by a flag.
What it does not do is BREAK THE RESULT DOWN along the two axes that decide
whether this recalibration worked.

Horizon exists to address two specific complaints about Livne: the LOSS_SL
drag, and crypto/stock divergence. Judging it on one pooled win rate would
average away exactly the signal being looked for -- a calibration that helps
crypto and hurts equities would show up as "no change", and the per-asset-class
risk profiles that are the whole point of this model would be unmeasurable.

So the report is always cut by asset class x direction, and the exit-tag
distribution is always shown: "fewer losses" is only meaningful if it is not
merely "fewer trades".

APPLES TO APPLES
----------------
`--compare-with livne` runs the SAME window, the SAME watchlists and the same
execution assumptions through the sibling model's parameters, so the two
columns differ only by calibration. It runs Livne's numbers WITHOUT importing
or touching Livne: a second process, in Livne's own directory, writing to its
own CSV -- see `--help` for the exact command it prints.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import statistics
import sys

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from backtest_suite.run_backtest import run_backtest
from config import LTF_TRIGGER_TF, MODEL_NAME, WATCHLIST_CRYPTO, WATCHLIST_STOCKS


def _stats(trades: list) -> dict:
    """Aggregates for one bucket. Deliberately the same set the daily analyzer
    computes, so a backtest cell and a live cell can be read side by side."""
    resolved = [t for t in trades if t.resolved]
    if not resolved:
        return {"trades": len(trades), "resolved": 0}

    pnls = [t.pnl_pct for t in resolved]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "trades": len(trades),
        "resolved": len(resolved),
        "win_rate": len(wins) / len(resolved) * 100,
        "avg_pnl": statistics.mean(pnls),
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "total_pnl": sum(pnls),
        "expectancy": statistics.mean(pnls),
    }


def _fmt(cell: dict) -> str:
    if not cell.get("resolved"):
        return f"{cell.get('trades', 0):>6}  {'--':>7}  {'--':>8}  {'--':>8}  {'--':>8}"
    pf = cell["profit_factor"]
    return (f"{cell['trades']:>6}  {cell['win_rate']:>6.1f}%  {cell['avg_pnl']:>+7.2f}%  "
            f"{cell['total_pnl']:>+7.2f}%  {(f'{pf:.2f}' if pf else 'inf'):>8}")


def report(trades: list, label: str) -> None:
    print()
    print("=" * 78)
    print(f"{label}")
    print("=" * 78)

    if not trades:
        print("  no setups were produced in this window.")
        return

    print(f"{'bucket':<26}{'trades':>6}  {'win%':>7}  {'avg':>8}  {'total':>8}  {'PF':>8}")
    print("-" * 78)
    for asset_class in ("stocks", "crypto"):
        for direction in ("LONG", "SHORT"):
            bucket = [t for t in trades
                      if t.asset_class == asset_class and t.direction == direction]
            if bucket:
                print(f"{asset_class + ' / ' + direction:<26}{_fmt(_stats(bucket))}")
        rows = [t for t in trades if t.asset_class == asset_class]
        if rows:
            print(f"{asset_class + ' / ALL':<26}{_fmt(_stats(rows))}")
            print("-" * 78)
    print(f"{'FLEET / ALL':<26}{_fmt(_stats(trades))}")

    # The exit-tag distribution is the point of this model. "Fewer losses" is
    # only meaningful next to "how many trades" -- a calibration that simply
    # stops trading also reports fewer losses.
    print()
    print("exit-tag distribution (this is what the recalibration targets):")
    tags = collections.Counter(t.exit_tag for t in trades if t.resolved)
    total = sum(tags.values()) or 1
    for tag, n in tags.most_common():
        print(f"  {tag:<22}{n:>5}  {n / total * 100:>5.1f}%")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=f"{MODEL_NAME} baseline: both watchlists, both directions",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--interval", default=LTF_TRIGGER_TF,
                    help=f"trigger timeframe (default {LTF_TRIGGER_TF}, matching the live model)")
    ap.add_argument("--exec-interval", default="5m",
                    help="timeframe open positions are managed on (default 5m: finer bars localise "
                         "exits and reduce ambiguous same-bar stop/target collisions)")
    ap.add_argument("--max-tickers", type=int, default=None,
                    help="cap watchlist size per class, for a fast smoke run")
    ap.add_argument("--offline", action="store_true",
                    help="replay purely from cached candles; never hit the network")
    ap.add_argument("--compare-with", metavar="MODEL",
                    help="print the command that produces the comparable run for a sibling "
                         "model (it is NOT executed here: a sibling runs in its own process, "
                         "in its own directory, writing its own CSV)")
    args = ap.parse_args()

    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date)
    if start >= end:
        ap.error("--start-date must be before --end-date")

    print(f"{MODEL_NAME} baseline  {start} -> {end}")
    print(f"  trigger {args.interval}, execution {args.exec_interval}")
    print(f"  stocks watchlist: {len(WATCHLIST_STOCKS)} symbols")
    print(f"  crypto watchlist: {len(WATCHLIST_CRYPTO)} symbols")
    if args.max_tickers:
        print(f"  (capped to {args.max_tickers} per class -- SMOKE RUN, not a baseline)")

    trades = run_backtest(
        start, end,
        asset_class="both",          # both watchlists, always
        interval=args.interval,
        exec_interval=args.exec_interval,
        max_tickers=args.max_tickers,
        offline=args.offline,
    )
    report(trades, f"{MODEL_NAME}  {start} -> {end}")

    if args.compare_with:
        print()
        print(f"comparable run for {args.compare_with} (run it yourself; this process never "
              f"touches a sibling model):")
        print(f"    cd models/{args.compare_with} && python -m backtest_suite.run_backtest \\")
        print(f"        --start-date {start} --end-date {end} \\")
        print(f"        --interval {args.interval} --exec-interval {args.exec_interval} "
              f"--asset-class both")
    return 0


if __name__ == "__main__":
    sys.exit(main())
