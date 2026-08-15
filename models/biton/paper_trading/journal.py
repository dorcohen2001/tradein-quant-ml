"""Excel trade journal (`paper_trades_journal.xlsx`).

Mirrors the JSON paper-trading store (`paper_trading/tracker.py`) into a
human-readable spreadsheet so the trade history can be reviewed offline.
Rows are matched and updated by trade ID every time a trade is logged or its
status changes, so the sheet stays in sync with `trades.json` rather than
being appended to indefinitely.
"""
from __future__ import annotations

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import PAPER_TRADING_JOURNAL_XLSX
from core.paper_trading.journal import upsert_row

COLUMNS = [
    "ID", "Signal Date", "Ticker", "Asset Class", "Direction",
    "Entry Zone", "Stop Loss", "Target 1", "Target 2", "Status",
    "Telegram Message ID", "Exit Date", "Exit Price", "PnL %",
]


def _entry_zone_str(trade: dict) -> str:
    low, high = trade["entry_zone"]
    return f"{low:g} - {high:g}"


def _row_values(trade: dict) -> list:
    return [
        trade["id"],
        trade["signal_date"],
        trade["ticker"],
        trade["asset_class"],
        trade["direction"],
        _entry_zone_str(trade),
        trade["stop_loss"],
        trade["target1"],
        trade["target2"],
        trade["current_status"],
        trade.get("telegram_message_id"),
        trade.get("exit_date"),
        trade.get("exit_price"),
        trade.get("pnl_pct"),
    ]


def upsert_trade_row(trade: dict) -> None:
    """Insert or update the spreadsheet row for `trade`, matched by ID.

    The COLUMNS/`_row_values` pair above is this model's own schema; the
    open-or-create + match-by-ID + save mechanism is shared (it was duplicated
    verbatim in both models). See core/paper_trading/journal.py.
    """
    upsert_row(PAPER_TRADING_JOURNAL_XLSX, COLUMNS, _row_values(trade), trade["id"])
