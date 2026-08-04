"""Trade execution logic & outcome tracker.

Given a signal (direction, entry zone, stop, target1, target2) generated on a
historical date T, walks forward through the real subsequent daily bars and
grades the trade WIN / LOSS / EXPIRED per the spec:

  - LONG:  WIN if High >= Target1 before Low <= Stop.  LOSS if Stop hit first.
  - SHORT: WIN if Low  <= Target1 before High >= Stop.  LOSS if Stop hit first.
  - Neither hit within `max_days` -> close at the final day's close (EXPIRED).

Same-day ambiguity (a single daily bar's range covers both the stop and a
target) is resolved conservatively in favor of the stop -- daily OHLCV has no
intrabar sequencing, so assuming the adverse outcome avoids overstating the
win rate.

Once Target1 is hit the trade is already a WIN; scanning continues only to
see whether Target2 is also reached within the remaining window (the stop is
assumed moved to breakeven after TP1, so no further loss risk is tracked).
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from backtest_suite.historical_data import forward_window


@dataclass
class TradeOutcome:
    ticker: str
    asset_class: str
    direction: str
    signal_date: pd.Timestamp
    entry_low: float
    entry_high: float
    entry_price: float
    stop: float
    target1: float
    target2: float
    exit_date: pd.Timestamp
    exit_price: float
    outcome: str   # WIN / LOSS / EXPIRED
    tag: str       # TP1 / TP2 / SL / TIMEOUT
    pnl_pct: float
    days_held: int
    score: float

    def combined_outcome(self) -> str:
        """Single-token label for logs/CSV: WIN_TP1, WIN_TP2, LOSS_SL, or EXPIRED."""
        if self.outcome in ("WIN", "LOSS"):
            return f"{self.outcome}_{self.tag}"
        return self.outcome


def simulate_trade(
    full_df: pd.DataFrame,
    ticker: str,
    asset_class: str,
    direction: str,
    signal_date: pd.Timestamp,
    entry_low: float,
    entry_high: float,
    stop: float,
    target1: float,
    target2: float,
    score: float,
    max_days: int = 10,
) -> TradeOutcome:
    entry_price = (entry_low + entry_high) / 2
    future = forward_window(full_df, signal_date, max_days)

    outcome = tag = exit_price = exit_date = None
    tp1_locked = False

    for ts, row in future.iterrows():
        hi, lo = float(row["High"]), float(row["Low"])

        if direction == "LONG":
            hit_stop, hit_t1, hit_t2 = lo <= stop, hi >= target1, hi >= target2
        else:
            hit_stop, hit_t1, hit_t2 = hi >= stop, lo <= target1, lo <= target2

        if not tp1_locked:
            if hit_stop:
                outcome, tag, exit_price, exit_date = "LOSS", "SL", stop, ts
                break
            if hit_t2:
                outcome, tag, exit_price, exit_date = "WIN", "TP2", target2, ts
                break
            if hit_t1:
                tp1_locked = True
                outcome, tag, exit_price, exit_date = "WIN", "TP1", target1, ts
                continue
        elif hit_t2:
            tag, exit_price, exit_date = "TP2", target2, ts
            break

    if outcome is None:
        if len(future) == 0:
            exit_price, exit_date = entry_price, signal_date
        else:
            exit_price, exit_date = float(future["Close"].iloc[-1]), future.index[-1]
        outcome, tag = "EXPIRED", "TIMEOUT"

    if direction == "LONG":
        pnl_pct = (exit_price - entry_price) / entry_price * 100
    else:
        pnl_pct = (entry_price - exit_price) / entry_price * 100

    days_held = (future.index.get_loc(exit_date) + 1) if exit_date in future.index else 0

    return TradeOutcome(
        ticker=ticker, asset_class=asset_class, direction=direction, signal_date=signal_date,
        entry_low=entry_low, entry_high=entry_high, entry_price=entry_price,
        stop=stop, target1=target1, target2=target2,
        exit_date=exit_date, exit_price=exit_price,
        outcome=outcome, tag=tag, pnl_pct=round(pnl_pct, 3), days_held=days_held, score=score,
    )
