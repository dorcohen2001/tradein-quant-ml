"""Atomic JSON persistence for a paper-trading store.

Lifted verbatim from the two models' trackers, where it existed as a
byte-identical pair differing only in which module-level constant held the
path. Both behaviours below are load-bearing and were preserved exactly:

CORRUPT STORE IS A HARD ERROR, NOT AN EMPTY LIST
------------------------------------------------
Returning `[]` for an unparseable store would let the very next save overwrite
the file and silently erase the entire trade history. Failing loudly leaves the
bad file on disk to be fixed or restored. This is the single most important
line in the module.

WRITES ARE ATOMIC
-----------------
Temp file in the same directory, then `os.replace` -- atomic on both POSIX and
Windows. A crash mid-write can therefore never leave a truncated store, which
(see above) would then be a hard error on the next read.
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable


def load_trades(path: str, migrate: Callable[[dict], dict] | None = None) -> list[dict]:
    """Read a trade store. A missing store is an empty list (the model may
    simply not have traded yet); a corrupt one raises RuntimeError.

    `migrate` is applied to each record on load -- a per-model hook, because
    only the owning model knows how to bring its own older records onto its
    current schema. Livne backfills its multi-tier TP fields here; Biton passes
    nothing.
    """
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        try:
            trades = json.load(f)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"{path} is corrupt ({exc}). Fix or restore it "
                f"manually before running again."
            ) from exc
    return [migrate(t) for t in trades] if migrate else trades


def save_trades(path: str, trades: list[dict]) -> None:
    """Atomically replace the store (temp file + os.replace)."""
    store_dir = os.path.dirname(path)
    os.makedirs(store_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=store_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(trades, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def pnl_pct(direction: str, entry_price: float, exit_price: float) -> float:
    """Percentage P&L of one closed leg, quoted so a SHORT gains when price
    falls -- matching `core.backtesting.engine.pnl_pct`, so a trade scores the
    same live as it does in a backtest.

    The zero guards come from the backtest copy: both models' live versions
    divided unguarded, so a bad price feed returning 0.0 raised
    ZeroDivisionError inside position management rather than returning a
    harmless 0.0.
    """
    if entry_price <= 0:
        return 0.0
    if direction == "LONG":
        return (exit_price / entry_price - 1) * 100
    return (entry_price / exit_price - 1) * 100 if exit_price > 0 else 0.0
