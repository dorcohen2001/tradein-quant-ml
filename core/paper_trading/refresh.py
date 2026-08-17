"""One implementation of "walk the open positions and mark them to market".

WHAT THIS REPLACES
------------------
Every tenant had its own `update_open_positions`, and they were the same
function three times over: load the store, skip closed trades, skip stocks
while the exchange is shut, fetch a price, size a lookback window, fetch the
range traded in it, run the model's `apply_bar`, stamp `last_checked_at`, queue
an alert, save. Livne's copy carried one extra step (an emergency-exit check
before the range test) and Biton's carried one fewer status; everything else
was duplicated verbatim, including the comments.

Duplication in THIS function is not a tidiness problem. It is the function that
decides whether a position closed, and a fix applied to two copies out of three
is a model that silently grades trades differently from its siblings.

WHAT IS AND IS NOT HERE
-----------------------
Here: the ORDER of operations, the market-session gate, the lookback sizing,
the `last_checked_at` bookkeeping and the outbox handshake.

Not here: what a status means, when a stop is hit, how P&L is computed. All of
that stays behind the injected `apply_bar`, because the two live models run
genuinely different lifecycles (Livne banks a partial at TP1 and moves to
breakeven; Biton closes outright) and a shared state machine would have to know
about both -- which is exactly the conflation `core.registry` documents as
having already corrupted metrics once.

Everything this needs is injected, so it is testable without a network, a
store, or a model package -- none of which a unit test can import anyway.
"""
from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field

from core.paper_trading import outbox
from core.paper_trading.catch_up import OPEN_STATUSES, refresh_lookback_minutes


@dataclass(frozen=True)
class RefreshPolicy:
    """Bounds on one refresh pass."""

    # Floor for the lookback window: a healthy cycle asks for exactly this, so
    # steady-state behaviour is unchanged by the elapsed-time sizing.
    cycle_interval_minutes: float
    # Ceiling: past this the historical catch-up owns the gap, because
    # flattening hours into a single high/low resolves at the wrong price and
    # stamps the wrong time.
    max_lookback_minutes: float
    # This model's non-terminal statuses. Defaults to the cross-model superset;
    # a tenant whose vocabulary is narrower (Biton has no PARTIAL_TP1) passes
    # its own so an unknown status can never be treated as open.
    open_statuses: frozenset[str] = OPEN_STATUSES


@dataclass
class RefreshResult:
    """What one pass did."""

    changed: list[dict] = field(default_factory=list)
    evaluated: int = 0
    skipped_market_closed: int = 0
    skipped_no_price: int = 0
    # True when any record was mutated -- including a `last_checked_at` stamp on
    # a trade that did not move. The caller saves only when this is set: a cycle
    # with nothing open must not rewrite a 110 KB store.
    touched: bool = False


def refresh_positions(
    trades: list[dict],
    *,
    policy: RefreshPolicy,
    apply_bar,
    current_price,
    price_range,
    stocks_market_open: bool,
    pre_bar=None,
    now: dt.datetime | None = None,
) -> RefreshResult:
    """Mark every open position in `trades` to market. Mutates them in place.

    Injected collaborators:

      `apply_bar(trade, high, low, ts) -> bool`
          The model's own lifecycle transition -- the SAME function the
          historical catch-up replays bar by bar, so live and replay can never
          drift.
      `current_price(ticker, asset_class) -> float | None`
      `price_range(ticker, asset_class, lookback_minutes) -> dict | None`
          `{"high", "low", ...}` over that window, or None. When it returns
          None the last price is used for both sides, which is the pre-existing
          degradation and is strictly worse at catching a wick -- hence the
          range being preferred.
      `pre_bar(trade, price, now) -> bool`
          Optional. A live-only check that may close the trade BEFORE the range
          test (Livne's volume-anomaly emergency exit). Returns True if it
          closed the position, in which case the bar test is skipped. It must
          do its own `_close_trade`; this module never invents an exit.

    Never raises on a per-trade failure path: a missing price skips that trade,
    exactly as before.
    """
    now = now or dt.datetime.now()
    result = RefreshResult()

    for trade in trades:
        if trade.get("current_status") not in policy.open_statuses:
            continue
        if trade.get("asset_class") == "stocks" and not stocks_market_open:
            # Deliberately does NOT stamp last_checked_at: this trade genuinely
            # was not checked, and pretending otherwise would hide the gap from
            # the catch-up replay on the next session.
            result.skipped_market_closed += 1
            continue

        price = current_price(trade["ticker"], trade["asset_class"])
        if price is None:
            result.skipped_no_price += 1
            continue

        result.evaluated += 1

        if pre_bar is not None and pre_bar(trade, price, now):
            stamp = now.isoformat(timespec="seconds")
            trade["last_checked_at"] = stamp
            outbox.queue(trade, now=now)
            result.changed.append(trade)
            result.touched = True
            continue

        # Sized from THIS trade's last check rather than the nominal cadence:
        # a cycle may run for as long as the supervisor's phase timeout allows,
        # so the real gap is the previous cycle's duration.
        lookback = refresh_lookback_minutes(
            trade, now,
            floor_minutes=policy.cycle_interval_minutes,
            ceiling_minutes=policy.max_lookback_minutes,
        )
        window = price_range(trade["ticker"], trade["asset_class"], int(math.ceil(lookback)))
        high, low = (window["high"], window["low"]) if window else (price, price)

        if apply_bar(trade, high, low, now):
            # Queued in the same pass that mutates the trade, so the intent to
            # notify is persisted by the same save as the transition itself.
            outbox.queue(trade, now=now)
            result.changed.append(trade)

        trade["last_checked_at"] = now.isoformat(timespec="seconds")
        result.touched = True

    return result
