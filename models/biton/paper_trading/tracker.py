"""Paper Trading Tracker.

Persistent JSON-backed store of simulated ("paper") trade positions produced
by the live runner. Tracks each signal from PENDING (entry zone not yet
touched) through ACTIVE -> WIN_TP1 / WIN_TP2 / LOSS_SL / EXPIRED, marking it
against live market prices via `utils.data_fetch`.
"""
from __future__ import annotations

import datetime as dt
import sys
import uuid
from dataclasses import asdict, dataclass

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    CATCH_UP_CONFIG,
    CYCLE_INTERVAL_MINUTES,
    MAX_SLIPPAGE_PCT,
    PAPER_TRADING_STORE,
)
from core.paper_trading.catch_up import CatchUpConfig, CatchUpReport, run_catch_up
from core.paper_trading.store import load_trades, pnl_pct as _pnl_pct, save_trades
from core.utils.market_hours import is_market_open
from paper_trading import journal
from utils.data_fetch import (
    get_binance_24hr_ticker,
    get_recent_intraday_range,
    get_stock_fast_info,
)

PENDING_EXPIRY_DAYS = 5  # a signal not triggered within this many days is stale


@dataclass
class PaperTrade:
    id: str
    signal_date: str
    ticker: str
    asset_class: str
    direction: str
    entry_zone: list
    stop_loss: float
    target1: float
    target2: float
    current_status: str = "PENDING"
    entry_date: str | None = None
    exit_date: str | None = None
    exit_price: float | None = None
    pnl_pct: float | None = None
    telegram_message_id: int | None = None


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _load() -> list[dict]:
    """Shared atomic-store read (core.paper_trading.store). No migration hook:
    Biton's schema has not changed, so records are used exactly as stored."""
    return load_trades(PAPER_TRADING_STORE)


def _save(trades: list[dict]) -> None:
    save_trades(PAPER_TRADING_STORE, trades)


def _journal_safe(trade: dict) -> bool:
    """Journal a trade without ever letting it break the cycle.

    `core.paper_trading.journal.upsert_row` is already non-raising, but the
    per-model `upsert_trade_row` above it also builds the row values from the
    trade dict -- so a schema surprise there could still raise. This is the
    outer guard: the trade store and the Telegram alert are what matter, and
    neither may be lost because a spreadsheet could not be written.
    """
    try:
        journal.upsert_trade_row(trade)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[journal] could not journal {trade.get('ticker')} "
              f"({trade.get('id')}): {type(exc).__name__}: {exc}. "
              f"Trade store and alerts unaffected.", file=sys.stderr)
        return False


def log_trade(setup, telegram_message_id: int | None = None) -> dict:
    """Persist a new PENDING paper trade from a `TradeSetup` and return its record."""
    trades = _load()
    record = PaperTrade(
        id=str(uuid.uuid4())[:8],
        signal_date=_now(),
        ticker=setup.ticker,
        asset_class=setup.asset_class,
        direction=setup.direction,
        entry_zone=[setup.entry_low, setup.entry_high],
        stop_loss=setup.stop,
        target1=setup.target1,
        target2=setup.target2,
        telegram_message_id=telegram_message_id,
    )
    trade_dict = asdict(record)
    trades.append(trade_dict)
    _save(trades)
    _journal_safe(trade_dict)
    return trade_dict


def has_open_trade(ticker: str, asset_class: str) -> bool:
    """True if `ticker` already has a PENDING/ACTIVE paper trade, so callers
    can skip generating a duplicate alert for the same setup."""
    return any(
        t["ticker"] == ticker and t["asset_class"] == asset_class
        and t["current_status"] in ("PENDING", "ACTIVE")
        for t in _load()
    )


def get_current_price(ticker: str, asset_class: str) -> float | None:
    try:
        if asset_class == "crypto":
            return float(get_binance_24hr_ticker(ticker)["lastPrice"])
        info = get_stock_fast_info(ticker)
        return info.get("last_price")
    except Exception as exc:
        print(f"[tracker] price fetch failed for {ticker}: {exc}", file=sys.stderr)
        return None


def apply_bar(trade: dict, high: float, low: float, ts: dt.datetime) -> bool:
    """Apply ONE bar's traded range to ONE open trade. Returns True if the
    status changed. Mutates `trade` in place.

    The single implementation of Biton's lifecycle transitions, used by both
    the live loop (one call per cycle, ts=now -- byte-for-byte the behaviour it
    always had) and the historical catch-up (one call per missed bar, with that
    bar's timestamp). One implementation so a replay can never drift from the
    live path.

    Biton is single-shot: TP1 CLOSES the position rather than banking a partial
    like Livne's PARTIAL_TP1. That difference is exactly why this lives here
    and not in `core`.
    """
    status = trade["current_status"]
    if status not in ("PENDING", "ACTIVE"):
        return False

    direction = trade["direction"]
    entry_low, entry_high = trade["entry_zone"]
    stamp = ts.isoformat(timespec="seconds")

    if status == "PENDING":
        signal_dt = dt.datetime.fromisoformat(trade["signal_date"])
        # Entry-zone touch is an OVERLAP test between the zone and the bar's
        # range, not "is the last price inside the zone right now" -- price
        # routinely traded through a ~0.7%-wide zone and back out between
        # polls, and a point-in-time check silently under-reported fills.
        if low <= entry_high and high >= entry_low:
            trade["current_status"] = "ACTIVE"
            trade["entry_date"] = stamp
            trade["last_status_change"] = stamp
            return True
        if (ts - signal_dt).days >= PENDING_EXPIRY_DAYS:
            trade["current_status"] = "EXPIRED"
            trade["exit_date"] = stamp
            trade["last_status_change"] = stamp
            return True
        return False

    # ACTIVE. Check the more extreme target first so a gap move clearing both
    # target1 and target2 in one bar is credited as a TP2 win. The stop is
    # checked before either: when one bar's range covers both, OHLC cannot say
    # which came first, and only the adverse assumption cannot flatter results.
    entry_price = (entry_low + entry_high) / 2
    stop = trade["stop_loss"]
    hit_stop = low <= stop if direction == "LONG" else high >= stop
    hit_tp2 = high >= trade["target2"] if direction == "LONG" else low <= trade["target2"]
    hit_tp1 = high >= trade["target1"] if direction == "LONG" else low <= trade["target1"]

    new_status = exit_price = None
    if hit_stop:
        new_status = "LOSS_SL"
        # A stop-loss becomes a market order once triggered - model a small
        # amount of adverse slippage instead of a perfect fill.
        exit_price = stop * (1 - MAX_SLIPPAGE_PCT) if direction == "LONG" else stop * (1 + MAX_SLIPPAGE_PCT)
    elif hit_tp2:
        new_status, exit_price = "WIN_TP2", trade["target2"]
    elif hit_tp1:
        new_status, exit_price = "WIN_TP1", trade["target1"]

    if not new_status:
        return False

    trade["current_status"] = new_status
    trade["exit_date"] = stamp
    trade["exit_price"] = exit_price
    trade["pnl_pct"] = _pnl_pct(direction, entry_price, exit_price)
    trade["last_status_change"] = stamp
    return True


def catch_up_open_positions(now: dt.datetime | None = None) -> CatchUpReport:
    """Replay price action missed while this runner was stopped.

    Call BEFORE `update_open_positions`. On a healthy runner every open trade
    was checked one cycle ago, so `gap_window` returns None for all of them and
    this costs one timestamp comparison each -- no fetch, no bars.

    Never raises: a failed catch-up leaves every position exactly as it was for
    the normal live pass.
    """
    trades = _load()
    stocks_market_open = is_market_open()

    def tradeable(trade: dict) -> bool:
        # Mirrors the live loop: stocks are not evaluated outside RTH, so the
        # replay must not resolve them on bars the live system would not have
        # acted on either.
        return not (trade["asset_class"] == "stocks" and not stocks_market_open)

    try:
        report = run_catch_up(
            trades, apply_bar,
            fetch_crypto=get_binance_klines, fetch_stock=get_stock_history,
            config=CATCH_UP_CONFIG, now=now, is_tradeable=tradeable,
        )
    except Exception as exc:  # noqa: BLE001 -- catch-up must never break the cycle
        print(f"[catch-up] pass failed (live positions unaffected): {exc}", file=sys.stderr)
        return CatchUpReport()

    if report.events:
        _save(trades)
        by_id = {t["id"]: t for t in trades}
        for event in report.events:
            record = by_id.get(event.trade_id)
            if record is not None:
                _journal_safe(record)
    return report


def update_open_positions() -> list[dict]:
    """Refresh every PENDING/ACTIVE trade against the current market price.
    Returns the list of trades whose status changed in this pass, so callers
    can push Telegram notifications for newly triggered/closed positions."""
    trades = _load()
    changed: list[dict] = []
    stocks_market_open = is_market_open()
    if not stocks_market_open:
        print(
            "[market] US equities market is CLOSED — skipping stock position "
            "refresh this cycle (crypto still evaluated).",
            file=sys.stderr,
        )

    for trade in trades:
        status = trade["current_status"]
        if status not in ("PENDING", "ACTIVE"):
            continue
        if trade["asset_class"] == "stocks" and not stocks_market_open:
            continue

        price = get_current_price(trade["ticker"], trade["asset_class"])
        if price is None:
            continue

        # The price RANGE since the last check -- not just the instantaneous
        # last-trade price -- is what every level test uses, so a level touched
        # between two cycles is still caught.
        rng = get_recent_intraday_range(trade["ticker"], trade["asset_class"], CYCLE_INTERVAL_MINUTES)
        hi, lo = (rng["high"], rng["low"]) if rng is not None else (price, price)

        # One bar -- the range since the last poll -- through the SAME
        # transition function the historical catch-up replays bar by bar.
        if apply_bar(trade, hi, lo, dt.datetime.now()):
            changed.append(trade)

        # Written for every trade actually evaluated (changed or not), so the
        # next run can tell how much price action it missed. Not written for
        # trades skipped above (closed market, no price): those genuinely were
        # not checked, and pretending otherwise would hide a real gap.
        trade["last_checked_at"] = _now()

    _save(trades)
    for trade in changed:
        _journal_safe(trade)
    return changed


def get_all_trades() -> list[dict]:
    return _load()
