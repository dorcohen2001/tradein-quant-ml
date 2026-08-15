"""Intraday trade execution logic & outcome tracker (Biton).

Biton's lifecycle is the older single-shot one and is preserved exactly:

  - LONG:  WIN once High >= Target1 before Low <= Stop.  LOSS if Stop first.
  - SHORT: WIN once Low  <= Target1 before High >= Stop.  LOSS if Stop first.
  - Once Target1 is hit the trade is already a WIN; scanning continues only to
    see whether Target2 is also reached. The stop is assumed moved to
    breakeven after TP1, so no further loss risk is tracked -- exactly as
    before.
  - Nothing hit within the maximum holding period -> closed at that bar's
    close (tag `MAX_HOLD`).

WHAT THE INTRADAY UPGRADE CHANGES
---------------------------------
Only the resolution, not the rules. Stops and targets are evaluated against
each INTRADAY bar's High and Low rather than daily ones, so an exit is dated
to the bar it actually happened on instead of to a whole session, and a wick
through the stop is no longer invisible. The holding cap is expressed in HOURS
rather than in daily bars, because "10 bars" means ten very different things
at 5m and at 1d; `MAX_HOLD_HOURS` below is the direct equivalent of the old
`max_days=10` default.

Same-bar ambiguity (one bar's range covering both the stop and a target) is
resolved conservatively in favour of the stop, since a single OHLC bar cannot
say which came first. A finer `--exec-interval` makes it rarer and an optional
`core.backtesting.FinerBarTieBreaker` resolves the rest from finer candles.

Known simplification, unchanged: the trade is assumed FILLED at the entry-zone
midpoint at the signal bar's close -- the simulator does not require price to
touch the entry zone first. Entry zones sit within ~0.5% of the signal price
so this is usually harmless, but it slightly flatters results versus the live
paper tracker, which requires a real entry-zone touch before a trade goes
ACTIVE. Keep it in mind when comparing backtest and live win rates.
"""
from __future__ import annotations

import pandas as pd

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import MAX_SLIPPAGE_PCT
from core.backtesting import ExcursionTracker, TradeRecord, apply_slippage, hits, interval_of, pnl_pct
from core.backtesting.engine import resolve_same_bar

# The intraday equivalent of the old `max_days=10` cutoff: ten calendar days.
MAX_HOLD_HOURS = 240


def simulate_trade(
    exec_df: pd.DataFrame,
    ticker: str,
    asset_class: str,
    direction: str,
    signal_time: pd.Timestamp,
    entry_low: float,
    entry_high: float,
    stop: float,
    target1: float,
    target2: float,
    score: float,
    interval: str,
    notional: float = 1000.0,
    max_hold_hours: float = MAX_HOLD_HOURS,
    tie_breaker=None,
) -> TradeRecord:
    entry_price = (entry_low + entry_high) / 2
    entry_time = pd.Timestamp(signal_time)
    exec_delta = interval_of(interval).delta
    deadline = entry_time + pd.Timedelta(hours=max_hold_hours) if max_hold_hours else None

    walk = exec_df.loc[exec_df.index >= entry_time]
    excursion = ExcursionTracker(direction, entry_price)

    outcome = tag = None
    exit_price = exit_time = None
    tp1_locked = False

    for ts, row in walk.iterrows():
        excursion.update(ts, row)
        # Bars are stamped with their OPEN time; an exit is only KNOWN once the
        # bar completes, so exits are timestamped at the bar's close.
        bar_close = ts + exec_delta

        stop_hit = hits(row, direction, stop, "stop") and not tp1_locked
        t1_hit = hits(row, direction, target1, "target")
        t2_hit = hits(row, direction, target2, "target")

        if stop_hit and (t1_hit or t2_hit):
            first = resolve_same_bar(
                tie_breaker, ticker, asset_class, ts, direction,
                stop, target2 if t2_hit else target1,
            )
            if first == "target":
                stop_hit = False

        if stop_hit:
            # A stop-loss becomes a market order once triggered -- model a
            # small amount of adverse slippage instead of a perfect fill,
            # consistent with the live paper tracker.
            fill = apply_slippage(stop, direction, MAX_SLIPPAGE_PCT, adverse=True)
            outcome, tag, exit_price, exit_time = "LOSS", "SL", fill, bar_close
            break

        if t2_hit:
            outcome, tag, exit_price, exit_time = "WIN", "TP2", target2, bar_close
            break

        if t1_hit and not tp1_locked:
            # Already a win. Keep walking only to see whether TP2 also prints;
            # the stop is assumed at breakeven from here, so the loss branch
            # above is disabled for the rest of the walk.
            tp1_locked = True
            outcome, tag, exit_price, exit_time = "WIN", "TP1", target1, bar_close
            continue

        if deadline is not None and bar_close >= deadline and not tp1_locked:
            exit_p = float(row["Close"])
            result = pnl_pct(direction, entry_price, exit_p)
            outcome = "WIN" if result >= 0 else "LOSS"
            tag, exit_price, exit_time = "MAX_HOLD", exit_p, bar_close
            break

    resolved = outcome is not None
    if not resolved:
        # Ran out of historical data before the trade resolved or timed out.
        # Reported OPEN with an unrealised mark, not force-closed -- every
        # summary metric excludes it.
        if len(walk) == 0:
            exit_price, exit_time = entry_price, entry_time
        else:
            exit_price, exit_time = float(walk["Close"].iloc[-1]), walk.index[-1] + exec_delta
        outcome, tag = "OPEN", "END_OF_DATA"

    result_pct = pnl_pct(direction, entry_price, float(exit_price))

    return TradeRecord(
        ticker=ticker, asset_class=asset_class, direction=direction, interval=interval,
        entry_time=entry_time, entry_price=entry_price,
        exit_time=exit_time, exit_price=float(exit_price),
        exit_tag=tag, outcome=outcome, resolved=resolved,
        pnl_pct=round(float(result_pct), 3), notional=notional,
        initial_stop=stop, stop_at_exit=(entry_price if tp1_locked else stop),
        target1=target1, target2=target2, tp1_hit=tp1_locked,
        mae_pct=excursion.mae_pct, mfe_pct=excursion.mfe_pct, mae_time=excursion.mae_time,
        score=score,
    )
