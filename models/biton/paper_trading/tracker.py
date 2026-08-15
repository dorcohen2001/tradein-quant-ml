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

from config import CYCLE_INTERVAL_MINUTES, MAX_SLIPPAGE_PCT, PAPER_TRADING_STORE
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
    journal.upsert_trade_row(trade_dict)
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

        direction = trade["direction"]
        entry_low, entry_high = trade["entry_zone"]

        # The price RANGE since the last check -- not just the instantaneous
        # last-trade price -- is what every level test below uses, so a level
        # touched between two cycles is still caught. Fetched once per trade
        # and reused by both the PENDING entry test and the ACTIVE stop/target
        # tests.
        rng = get_recent_intraday_range(trade["ticker"], trade["asset_class"], CYCLE_INTERVAL_MINUTES)
        hi, lo = (rng["high"], rng["low"]) if rng is not None else (price, price)

        if status == "PENDING":
            signal_dt = dt.datetime.fromisoformat(trade["signal_date"])
            # Entry-zone touch is an OVERLAP test between the entry zone and
            # the cycle's traded range, not "is the last price inside the
            # zone right now". Entry zones are ~0.7% wide while a cycle is 15
            # minutes, so price routinely traded through the zone and back out
            # before the poll -- with the old point-in-time check those trades
            # stayed PENDING and eventually EXPIRED, silently under-reporting
            # fills and skewing every downstream win-rate metric.
            if lo <= entry_high and hi >= entry_low:
                trade["current_status"] = "ACTIVE"
                trade["entry_date"] = _now()
                changed.append(trade)
            elif (dt.datetime.now() - signal_dt).days >= PENDING_EXPIRY_DAYS:
                trade["current_status"] = "EXPIRED"
                trade["exit_date"] = _now()
                changed.append(trade)
            continue

        # status == "ACTIVE": check stop/targets against the price range since
        # the last check (not just the instantaneous last-trade price), so a
        # breach that happens between 15-minute cycles is still caught at
        # (near) the actual level instead of wherever price drifted to by the
        # next check. Check the more extreme target first so a gap move that
        # clears both target1 and target2 between updates is credited as a
        # TP2 win.
        entry_price = (entry_low + entry_high) / 2
        stop = trade["stop_loss"]
        hit_stop = lo <= stop if direction == "LONG" else hi >= stop
        hit_tp2 = hi >= trade["target2"] if direction == "LONG" else lo <= trade["target2"]
        hit_tp1 = hi >= trade["target1"] if direction == "LONG" else lo <= trade["target1"]

        new_status = exit_price = None
        if hit_stop:
            new_status = "LOSS_SL"
            # A stop-loss becomes a market order once triggered - model a
            # small amount of adverse slippage instead of a perfect fill.
            exit_price = stop * (1 - MAX_SLIPPAGE_PCT) if direction == "LONG" else stop * (1 + MAX_SLIPPAGE_PCT)
        elif hit_tp2:
            new_status, exit_price = "WIN_TP2", trade["target2"]
        elif hit_tp1:
            new_status, exit_price = "WIN_TP1", trade["target1"]

        if new_status:
            trade["current_status"] = new_status
            trade["exit_date"] = _now()
            trade["exit_price"] = exit_price
            trade["pnl_pct"] = _pnl_pct(direction, entry_price, exit_price)
            changed.append(trade)

    # Stamp every transition in one place, so the daily summary can filter on
    # "changed in the last 24h" rather than on exit_date, which is only written
    # for a full close.
    for trade in changed:
        trade["last_status_change"] = _now()

    _save(trades)
    for trade in changed:
        journal.upsert_trade_row(trade)
    return changed


def get_all_trades() -> list[dict]:
    return _load()
