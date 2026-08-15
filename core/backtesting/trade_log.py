"""Trade-by-trade log: one row per simulated trade, as a pandas DataFrame.

`TradeRecord` is the single shape both models' simulators produce, so the
printer, the CSV exporter and the metrics all work unchanged across them.

The CSV column names `ticker`, `asset_class`, `direction`, `outcome` and
`pnl_pct` are load-bearing beyond this package: `agents/steve/
daily_tradein_analyzer.py` reads the exported file and keys on exactly those
five. They stay put; every intraday addition is a NEW column alongside them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from core.backtesting.engine import duration_breakdown, pnl_points

TRADE_LOG_COLUMNS = [
    # --- identity ---
    "ticker", "asset_class", "direction", "interval",
    # --- entry ---
    "entry_time", "entry_price",
    # --- exit ---
    "exit_time", "exit_price", "exit_reason", "outcome", "resolved",
    # --- result ---
    "pnl_points", "pnl_pct", "pnl_usd", "notional",
    # --- holding period ---
    "duration_minutes", "duration_hours", "duration_days",
    # --- levels ---
    "initial_stop", "stop_at_exit", "target1", "target2", "tp1_hit",
    # --- intrabar excursions ---
    "mae_pct", "mae_time", "mfe_pct",
    # --- provenance ---
    "score",
]

# Human-readable exit reasons. Simulators may emit their own model-specific
# tags too; these are the ones the summary groups on.
EXIT_REASONS = {
    "SL": "Stop Loss Hit",
    "TP1": "Take Profit 1 Hit",
    "TP2": "Take Profit 2 Hit",
    "TP1_TP2": "Take Profit 1 + 2 Hit",
    "TP1_SL_BE": "Breakeven Stop After TP1",
    "TREND_REVERSAL": "Strategy Signal (Trend Reversal)",
    "MAX_HOLD": "Max Holding Period Reached",
    "END_OF_DATA": "Still Open (End Of Data)",
}


def describe_exit(tag: str) -> str:
    return EXIT_REASONS.get(tag, tag.replace("_", " ").title())


@dataclass
class TradeRecord:
    """One simulated trade, from entry fill to exit."""
    ticker: str
    asset_class: str
    direction: str
    interval: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: pd.Timestamp | None
    exit_price: float
    exit_tag: str                    # machine tag: SL / TP1_TP2 / TREND_REVERSAL / ...
    outcome: str                     # WIN / LOSS / OPEN
    resolved: bool                   # False for a position still open at end of data
    pnl_pct: float
    notional: float = 0.0
    initial_stop: float = 0.0
    stop_at_exit: float = 0.0
    target1: float = 0.0
    target2: float = 0.0
    tp1_hit: bool = False
    mae_pct: float = 0.0
    mfe_pct: float = 0.0
    mae_time: pd.Timestamp | None = None
    score: float = 0.0
    extra: dict = field(default_factory=dict)

    @property
    def exit_reason(self) -> str:
        return describe_exit(self.exit_tag)

    @property
    def combined_outcome(self) -> str:
        """Single token for logs/CSV, e.g. `WIN_TP1_TP2` or `LOSS_SL` -- the
        format `agents/steve` and the existing journals already expect."""
        return f"{self.outcome}_{self.exit_tag}"

    @property
    def pnl_points(self) -> float:
        return pnl_points(self.direction, self.entry_price, self.exit_price)

    @property
    def pnl_usd(self) -> float:
        """P&L in currency for a position of `notional` at entry. With a fixed
        notional per trade this is just the percentage return applied to the
        deployed capital -- no leverage or compounding is modelled."""
        return self.notional * self.pnl_pct / 100.0

    def to_row(self) -> dict:
        dur = duration_breakdown(self.entry_time, self.exit_time)
        return {
            "ticker": self.ticker,
            "asset_class": self.asset_class,
            "direction": self.direction,
            "interval": self.interval,
            "entry_time": self.entry_time,
            "entry_price": round(float(self.entry_price), 8),
            "exit_time": self.exit_time,
            "exit_price": round(float(self.exit_price), 8),
            "exit_reason": self.exit_reason,
            "outcome": self.combined_outcome,
            "resolved": self.resolved,
            "pnl_points": round(self.pnl_points, 8),
            "pnl_pct": round(float(self.pnl_pct), 3),
            "pnl_usd": round(self.pnl_usd, 2),
            "notional": round(float(self.notional), 2),
            "duration_minutes": dur["minutes"],
            "duration_hours": dur["hours"],
            "duration_days": dur["days"],
            "initial_stop": round(float(self.initial_stop), 8),
            "stop_at_exit": round(float(self.stop_at_exit), 8),
            "target1": round(float(self.target1), 8),
            "target2": round(float(self.target2), 8),
            "tp1_hit": self.tp1_hit,
            "mae_pct": round(float(self.mae_pct), 3),
            "mae_time": self.mae_time,
            "mfe_pct": round(float(self.mfe_pct), 3),
            "score": self.score,
        }


def trades_to_dataframe(records: list[TradeRecord]) -> pd.DataFrame:
    """Chronological (by entry time) trade log."""
    if not records:
        return pd.DataFrame(columns=TRADE_LOG_COLUMNS)
    df = pd.DataFrame([r.to_row() for r in records], columns=TRADE_LOG_COLUMNS)
    for col in ("entry_time", "exit_time", "mae_time"):
        df[col] = pd.to_datetime(df[col])
    return df.sort_values("entry_time", kind="stable").reset_index(drop=True)


_TS = "%Y-%m-%d %H:%M"


def format_trade_log(df: pd.DataFrame, max_rows: int | None = None) -> str:
    """Fixed-width rendering of the trade log for the terminal.

    pandas' own `to_string` is avoided here because it right-truncates wide
    frames by default and the price columns are the first thing it drops.
    """
    if df.empty:
        return "No trades were simulated in this window.\n"

    view = df if max_rows is None else df.tail(max_rows)
    header = "  ".join([
        f"{'Ticker':<10}", f"{'Dir':<5}", f"{'Entry (UTC)':<16}", f"{'Entry':>12}",
        f"{'Exit (UTC)':<16}", f"{'Exit':>12}", f"{'Exit Reason':<32}",
        f"{'PnL $':>10}", f"{'Return %':>9}", f"{'Held':>8}",
    ])
    lines = [header, "-" * len(header)]
    for _i, r in view.iterrows():
        entry_ts = pd.Timestamp(r["entry_time"]).strftime(_TS)
        exit_ts = pd.Timestamp(r["exit_time"]).strftime(_TS) if pd.notna(r["exit_time"]) else "-"
        held = _format_held(r["duration_minutes"])
        lines.append("  ".join([
            f"{str(r['ticker'])[:10]:<10}", f"{str(r['direction']):<5}",
            f"{entry_ts:<16}", f"{r['entry_price']:>12,.4f}",
            f"{exit_ts:<16}", f"{r['exit_price']:>12,.4f}",
            f"{str(r['exit_reason'])[:32]:<32}",
            f"{r['pnl_usd']:>+10,.2f}", f"{r['pnl_pct']:>+9.2f}", f"{held:>8}",
        ]))
    if max_rows is not None and len(df) > max_rows:
        lines.append(f"... {len(df) - max_rows} earlier trades omitted (full log in the CSV export)")
    return "\n".join(lines) + "\n"


def _format_held(minutes: float) -> str:
    minutes = float(minutes or 0)
    if minutes < 60:
        return f"{minutes:.0f}m"
    if minutes < 1440:
        return f"{minutes / 60:.1f}h"
    return f"{minutes / 1440:.1f}d"


def export_trades_csv(df: pd.DataFrame, path: str | Path) -> Path:
    """Write the trade log to `path`, creating parent directories as needed.

    Timestamps are written in ISO form with minute resolution so the file
    stays readable in a spreadsheet and diffable between runs.
    """
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    frame = df.copy()
    for col in ("entry_time", "exit_time", "mae_time"):
        if col in frame.columns:
            frame[col] = pd.to_datetime(frame[col]).dt.strftime("%Y-%m-%d %H:%M:%S")
    frame.to_csv(out, index=False, encoding="utf-8")
    return out
