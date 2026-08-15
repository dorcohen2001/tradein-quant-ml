"""Replay the price action a stopped runner missed, bar by bar.

THE PROBLEM
-----------
Position management polls. Each cycle it fetches the traded range since the
last poll and tests the stop/targets against it. That is correct while the
runner is up -- but if the process is stopped (CMD closed, machine rebooted,
model paused) and started again hours or days later, the next poll compares a
position against a range covering only the last few minutes. Everything that
happened in between is invisible:

  * a stop breached overnight is never seen, and the trade keeps running as if
    it were still open;
  * a target hit and then retraced is missed entirely;
  * whatever the eventual exit is, it gets stamped with the time the runner
    happened to restart, not when the level was actually crossed.

The recorded P&L is then not what the strategy would have produced. On a store
with 133 trades that is not a rounding error, it is a corrupted sample.

THE APPROACH
------------
Walk the missed window in chronological order and apply the SAME per-bar
transition the live loop applies, once per historical bar, stamping each
transition with that bar's timestamp.

The critical design choice is that this module does NOT contain a state
machine. It walks bars and calls back into the model's own `_apply_bar`, which
is the exact function the live path uses. The two models have genuinely
different lifecycles (Livne: PENDING -> ACTIVE -> PARTIAL_TP1 -> WIN_TP2 /
LOSS_SL_BE, with a breakeven move; Biton: single-shot), and a second copy of
either here would drift from the original the first time one was tuned. One
implementation, two callers: live passes one bar (the current cycle's range),
catch-up passes N.

WHAT IS DELIBERATELY NOT REPLAYED
---------------------------------
Livne's emergency reversal exit. It keys off a volume anomaly across recent
candles and is documented as live-only (it is not simulated in the backtest
either, for the same reason). Replaying it from coarse historical bars would
invent exits that the live system would not have taken. A position that would
have been emergency-exited during the gap is instead resolved by its stop or
target, or left open -- which is the conservative direction.

SAME-BAR AMBIGUITY
------------------
One bar's range can cover both the stop and a target, and OHLC alone cannot say
which came first. Resolved in favour of the STOP, matching both the live loop
and `core.backtesting.engine.resolve_same_bar`. Assuming the adverse outcome is
the only choice that cannot flatter the win rate.
"""
from __future__ import annotations

import datetime as dt
import math
import sys
from dataclasses import dataclass, field

from core.utils.telegram_notifier import escape_markdown, escape_markdown_code

# Terminal statuses across every model. A trade in one of these is finished and
# is never replayed. Kept as a superset rather than per-model because this
# module only needs "is there anything left to do", not the vocabulary itself.
_OPEN_STATUSES = frozenset({"PENDING", "ACTIVE", "PARTIAL_TP1"})


@dataclass(frozen=True)
class CatchUpConfig:
    """Bounds on the replay. Every one of these exists to stop the catch-up
    becoming more expensive or more speculative than the gap justifies."""

    # Gaps smaller than this are left to the normal live path. THIS IS THE
    # ZERO-REGRESSION GUARANTEE: at a 2-minute cycle the gap is ~2 minutes, far
    # below the floor, so a healthy runner never fetches a single historical
    # bar and the catch-up code costs one timestamp comparison per trade.
    min_gap_minutes: float = 30.0

    # Hard ceiling on how far back to replay. Protects against a trade whose
    # last-checked stamp is missing or ancient (every trade written before this
    # feature existed), and matches roughly what the free intraday feeds will
    # actually serve.
    max_lookback_hours: float = 72.0

    # Replay resolution. Finer bars localise the exit more precisely but cost
    # more history; 5m is a good balance and is available for both venues.
    # yfinance caps 5m history at ~60 days, comfortably past max_lookback_hours.
    crypto_interval: str = "5m"
    stock_interval: str = "5m"
    interval_minutes: int = 5

    # Bars per request. Binance hard-caps a klines response at 1000.
    max_bars: int = 1000


@dataclass
class CatchUpEvent:
    """One status transition discovered during replay."""

    trade_id: str
    ticker: str
    from_status: str
    to_status: str
    at: dt.datetime
    exit_price: float | None = None
    pnl_pct: float | None = None


@dataclass
class CatchUpReport:
    """Everything one catch-up pass did, for logging and the summary message."""

    events: list[CatchUpEvent] = field(default_factory=list)
    trades_replayed: int = 0
    bars_replayed: int = 0
    skipped_no_data: list[str] = field(default_factory=list)

    @property
    def resolved(self) -> list[CatchUpEvent]:
        """Transitions into a terminal state (i.e. positions that closed)."""
        return [e for e in self.events if e.to_status not in _OPEN_STATUSES]


# ---------------------------------------------------------------------------
# Gap detection
# ---------------------------------------------------------------------------
def _parse(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def last_checked_at(trade: dict) -> dt.datetime | None:
    """When this trade was last evaluated against the market.

    `last_checked_at` is written on every pass once this feature is live. It is
    absent on every trade written before, so fall back through the timestamps
    that do exist. The fallbacks are ordered newest-meaning-first: a trade that
    entered is known to have been checked at least at entry, and a PENDING
    trade at least at signal time.
    """
    for key in ("last_checked_at", "last_status_change", "entry_date", "signal_date"):
        parsed = _parse(trade.get(key))
        if parsed is not None:
            return parsed
    return None


def gap_window(trade: dict, now: dt.datetime, config: CatchUpConfig) -> tuple[dt.datetime, dt.datetime] | None:
    """The (start, end) span to replay for `trade`, or None if there is nothing
    worth replaying.

    Returns None when the trade is closed, when its last check is unknown, or
    when the gap is below `min_gap_minutes` -- the last of which is the common
    case on a healthy runner and is what keeps this free at steady state.
    """
    if trade.get("current_status") not in _OPEN_STATUSES:
        return None

    last = last_checked_at(trade)
    if last is None or last >= now:
        return None

    gap_minutes = (now - last).total_seconds() / 60.0
    if gap_minutes < config.min_gap_minutes:
        return None

    # Clamp rather than refuse: replaying the most recent 72h of a week-long
    # gap is strictly better than replaying none of it.
    earliest = now - dt.timedelta(hours=config.max_lookback_hours)
    return (max(last, earliest), now)


# ---------------------------------------------------------------------------
# Candle retrieval
# ---------------------------------------------------------------------------
def to_local_naive(index):
    """Normalise a candle index to NAIVE LOCAL time.

    This is the single most bug-prone part of the module. Trade timestamps are
    written with `datetime.now()` -- naive local. Binance returns UTC epochs;
    yfinance returns tz-aware timestamps (usually US/Eastern). Comparing either
    against a naive local stamp without conversion silently shifts the whole
    replay window by the UTC offset, which on a several-hour gap can drop or
    duplicate hours of bars.
    """
    import pandas as pd

    local_tz = dt.datetime.now().astimezone().tzinfo
    if getattr(index, "tz", None) is not None:
        return index.tz_convert(local_tz).tz_localize(None)
    # Naive index from Binance is UTC by construction (see cache.normalize_frame).
    return index.tz_localize("UTC").tz_convert(local_tz).tz_localize(None)


def fetch_gap_candles(trade: dict, start: dt.datetime, end: dt.datetime,
                      config: CatchUpConfig, *, fetch_crypto, fetch_stock):
    """OHLC bars covering (start, end], newest-last, indexed in naive local time.

    The model's own fetchers are injected, exactly as `chart_attachment` does
    it -- `core` must not import a model's data layer, and the two models have
    different ones.

    Returns None (never raises) if anything goes wrong. A failed catch-up must
    degrade to "no replay", leaving the position for the normal live path,
    rather than taking down position management.
    """
    span_minutes = max((end - start).total_seconds() / 60.0, config.interval_minutes)
    try:
        if trade["asset_class"] == "crypto":
            bars = int(min(config.max_bars, math.ceil(span_minutes / config.interval_minutes) + 5))
            df = fetch_crypto(trade["ticker"], interval=config.crypto_interval, limit=bars)
        else:
            # yfinance takes a period, not a bar count. +1 day of slack so a
            # gap that straddles a session boundary is fully covered.
            days = int(math.ceil(span_minutes / (60 * 24))) + 1
            df = fetch_stock(trade["ticker"], period=f"{days}d", interval=config.stock_interval)
    except Exception as exc:  # noqa: BLE001 -- network/parse failures must not propagate
        print(f"[catch-up] {trade['ticker']}: candle fetch failed ({exc}); "
              f"leaving it to the live path.", file=sys.stderr)
        return None

    if df is None or len(df) == 0:
        return None
    if not {"High", "Low"}.issubset(df.columns):
        print(f"[catch-up] {trade['ticker']}: frame lacks High/Low columns; skipping.", file=sys.stderr)
        return None

    df = df.copy()
    try:
        df.index = to_local_naive(df.index)
    except Exception as exc:  # noqa: BLE001 -- an odd index must not be fatal
        print(f"[catch-up] {trade['ticker']}: could not normalise candle index ({exc}); skipping.",
              file=sys.stderr)
        return None

    # Strictly after `start`: the bar containing the last check was already
    # evaluated by the cycle that ran then, and re-applying it could re-trigger
    # a level that was already handled.
    window = df.loc[(df.index > start) & (df.index <= end)]
    return window if len(window) else None


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def replay_trade(trade: dict, candles, apply_bar) -> list[CatchUpEvent]:
    """Walk `candles` oldest-first, applying the model's own per-bar transition.

    `apply_bar(trade, high, low, ts) -> bool` is the model's function -- the
    same one its live loop calls with the current cycle's range. It mutates
    `trade` in place and returns True if the status changed.

    Stops as soon as the trade reaches a terminal status: a closed position must
    not keep reacting to later bars.
    """
    events: list[CatchUpEvent] = []
    for ts, row in candles.iterrows():
        if trade.get("current_status") not in _OPEN_STATUSES:
            break
        before = trade["current_status"]
        try:
            changed = apply_bar(trade, float(row["High"]), float(row["Low"]), ts.to_pydatetime())
        except Exception as exc:  # noqa: BLE001
            print(f"[catch-up] {trade.get('ticker')}: bar at {ts} failed ({exc}); "
                  f"stopping replay for this trade.", file=sys.stderr)
            break
        if changed:
            events.append(CatchUpEvent(
                trade_id=trade.get("id", "?"),
                ticker=trade.get("ticker", "?"),
                from_status=before,
                to_status=trade["current_status"],
                at=ts.to_pydatetime(),
                exit_price=trade.get("exit_price"),
                pnl_pct=trade.get("pnl_pct"),
            ))
    return events


def run_catch_up(trades: list[dict], apply_bar, *, fetch_crypto, fetch_stock,
                 config: CatchUpConfig | None = None, now: dt.datetime | None = None,
                 is_tradeable=None) -> CatchUpReport:
    """Replay every open trade that has a gap. Mutates the trades in place.

    `is_tradeable(trade) -> bool` optionally filters (the callers use it to skip
    stocks while the US market is closed, matching the live loop).

    Never raises. A catch-up that cannot run leaves positions exactly as they
    were, for the live path to handle as before.
    """
    config = config or CatchUpConfig()
    now = now or dt.datetime.now()
    report = CatchUpReport()

    for trade in trades:
        if is_tradeable is not None and not is_tradeable(trade):
            continue
        window = gap_window(trade, now, config)
        if window is None:
            continue  # healthy cadence, or already closed -- the fast path

        start, end = window
        candles = fetch_gap_candles(trade, start, end, config,
                                    fetch_crypto=fetch_crypto, fetch_stock=fetch_stock)
        if candles is None:
            report.skipped_no_data.append(trade.get("ticker", "?"))
            continue

        gap_hours = (end - start).total_seconds() / 3600.0
        print(f"[catch-up] {trade['ticker']} ({trade['current_status']}): replaying "
              f"{len(candles)} bars over {gap_hours:.1f}h gap", file=sys.stderr)

        report.trades_replayed += 1
        report.bars_replayed += len(candles)
        report.events.extend(replay_trade(trade, candles, apply_bar))

    return report


# ---------------------------------------------------------------------------
# Consolidated notification
# ---------------------------------------------------------------------------
def format_catch_up_summary(model_name: str, report: CatchUpReport,
                            gap_description: str | None = None) -> str | None:
    """ONE message covering everything the replay resolved, or None if nothing
    changed.

    Deliberately not per-trade alerts. A runner that was down overnight can come
    back to a dozen resolutions, and firing a dozen delayed "EXIT ALERT"
    messages would read as if they were all happening now -- actively
    misleading in a channel people trade from. One clearly-labelled summary,
    with the real timestamps, says what actually happened.

    Rows go in a fenced block: Telegram renders it monospaced (real column
    alignment) and parses no entities inside, so statuses like `LOSS_SL_BE`
    cannot produce the unmatched-underscore 400 this bot has hit before.
    """
    if not report.events:
        return None

    resolved = report.resolved
    head = f"[{escape_markdown(model_name)}] 🔄 *CATCH-UP SYNC*"
    lines = [head]

    detail = f" ({escape_markdown(gap_description)})" if gap_description else ""
    lines.append(
        f"Replayed {report.bars_replayed} historical bars across "
        f"{report.trades_replayed} open position(s){detail}. "
        f"*{len(resolved)}* resolved while the runner was offline; "
        f"timestamps below are when each level was actually crossed."
    )

    rows = [f"{'TICKER':<9}{'TRANSITION':<26}{'P&L %':>9}  WHEN"]
    for event in report.events:
        transition = f"{event.from_status}->{event.to_status}"
        pnl = f"{event.pnl_pct:+.2f}" if event.pnl_pct is not None else "-"
        rows.append(f"{event.ticker:<9}{transition:<26}{pnl:>9}  {event.at:%Y-%m-%d %H:%M}")
    lines.append("```\n" + "\n".join(escape_markdown_code(r) for r in rows) + "\n```")

    realized = [e.pnl_pct for e in resolved if e.pnl_pct is not None]
    if realized:
        lines.append(f"Net realized during the gap: *{sum(realized):+.2f}%*")
    if report.skipped_no_data:
        lines.append(
            "_No gap history available for: "
            f"{escape_markdown(', '.join(sorted(set(report.skipped_no_data))))} "
            "— these stay open for the live pass._"
        )
    return "\n".join(lines)
