"""End-of-backtest summary metrics.

Every figure here is computed from RESOLVED trades only -- a position still
open when the historical data ran out has an unrealised mark, not a result,
and folding those into a win rate would bias it in whichever direction the
market happened to be drifting. Open positions are counted and reported
separately instead.

Equity model (stated explicitly because a "Total ROI" is meaningless without
one): each trade deploys a FIXED notional; there is no compounding, no
pyramiding and no leverage. The equity curve is

    equity(after trade i) = initial_capital + sum(pnl_usd of trades 1..i)

with trades ordered by EXIT time, since that is when P&L is actually
realised. Total ROI is net P&L over initial capital, and max drawdown is the
deepest peak-to-trough decline of that curve as a percentage of the peak.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd


@dataclass
class BacktestMetrics:
    total_trades: int = 0
    resolved_trades: int = 0
    open_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float = 0.0
    total_pnl_usd: float = 0.0
    total_roi_pct: float = 0.0
    sum_return_pct: float = 0.0        # equal-weighted, non-compounded
    profit_factor: float = 0.0
    max_drawdown_pct: float = 0.0
    max_drawdown_usd: float = 0.0
    max_drawdown_peak_time: pd.Timestamp | None = None
    max_drawdown_trough_time: pd.Timestamp | None = None
    avg_duration_minutes: float = 0.0
    avg_duration_label: str = "0m"
    avg_win_pct: float = 0.0
    avg_loss_pct: float = 0.0
    expectancy_pct: float = 0.0
    best_trade_pct: float = 0.0
    worst_trade_pct: float = 0.0
    avg_mae_pct: float = 0.0
    initial_capital: float = 0.0
    by_exit_reason: dict = field(default_factory=dict)
    by_ticker: dict = field(default_factory=dict)
    unrealised_open_pct: float = 0.0


def _humanize(minutes: float) -> str:
    minutes = float(minutes or 0)
    if minutes < 60:
        return f"{minutes:.0f} min"
    if minutes < 1440:
        return f"{minutes / 60:.1f} hours"
    return f"{minutes / 1440:.1f} days"


def _drawdown(equity: pd.Series) -> tuple[float, float, pd.Timestamp | None, pd.Timestamp | None]:
    """Deepest peak-to-trough decline of an equity curve, as (pct, usd, peak_t,
    trough_t). Percentage is measured against the running peak, which is the
    convention that makes drawdowns comparable across account sizes."""
    if equity.empty:
        return 0.0, 0.0, None, None
    running_peak = equity.cummax()
    drawdown = equity - running_peak
    trough_pos = int(drawdown.values.argmin())
    worst_usd = float(drawdown.iloc[trough_pos])
    if worst_usd >= 0:
        return 0.0, 0.0, None, None
    peak_value = float(running_peak.iloc[trough_pos])
    worst_pct = (worst_usd / peak_value * 100.0) if peak_value else 0.0
    peak_pos = int(equity.iloc[: trough_pos + 1].values.argmax())
    return abs(worst_pct), abs(worst_usd), equity.index[peak_pos], equity.index[trough_pos]


def compute_metrics(df: pd.DataFrame, initial_capital: float = 10_000.0) -> BacktestMetrics:
    m = BacktestMetrics(initial_capital=initial_capital)
    if df is None or df.empty:
        return m

    m.total_trades = len(df)
    resolved = df[df["resolved"].astype(bool)]
    open_df = df[~df["resolved"].astype(bool)]
    m.resolved_trades = len(resolved)
    m.open_trades = len(open_df)
    if m.open_trades:
        m.unrealised_open_pct = round(float(open_df["pnl_pct"].mean()), 2)

    if resolved.empty:
        return m

    # A "win" is decided by realised P&L, not by which exit fired: a trade that
    # closes on a reversal signal, or at a breakeven stop after banking a TP1
    # partial, is a win if it made money.
    wins = resolved[resolved["pnl_pct"] > 0]
    losses = resolved[resolved["pnl_pct"] <= 0]
    m.wins, m.losses = len(wins), len(losses)
    m.win_rate_pct = round(m.wins / len(resolved) * 100, 2)

    gross_profit = float(wins["pnl_usd"].sum())
    gross_loss = abs(float(losses["pnl_usd"].sum()))
    m.profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float("inf")

    m.total_pnl_usd = round(float(resolved["pnl_usd"].sum()), 2)
    m.total_roi_pct = round(m.total_pnl_usd / initial_capital * 100, 2) if initial_capital else 0.0
    m.sum_return_pct = round(float(resolved["pnl_pct"].sum()), 2)

    ordered = resolved.sort_values("exit_time", kind="stable")
    # The curve starts at the untouched starting capital, BEFORE the first
    # trade closes. Without that leading point the first trade's own loss
    # becomes the running peak and a losing opening streak reports a 0%
    # drawdown -- the one case a drawdown figure most needs to catch.
    opened_at = pd.to_datetime(ordered["entry_time"]).min()
    equity = pd.concat([
        pd.Series([float(initial_capital)], index=pd.DatetimeIndex([opened_at])),
        pd.Series(
            initial_capital + ordered["pnl_usd"].cumsum().to_numpy(),
            index=pd.to_datetime(ordered["exit_time"]),
        ),
    ])
    dd_pct, dd_usd, peak_t, trough_t = _drawdown(equity)
    m.max_drawdown_pct = round(dd_pct, 2)
    m.max_drawdown_usd = round(dd_usd, 2)
    m.max_drawdown_peak_time, m.max_drawdown_trough_time = peak_t, trough_t

    m.avg_duration_minutes = round(float(resolved["duration_minutes"].mean()), 1)
    m.avg_duration_label = _humanize(m.avg_duration_minutes)
    m.avg_win_pct = round(float(wins["pnl_pct"].mean()), 2) if len(wins) else 0.0
    m.avg_loss_pct = round(float(losses["pnl_pct"].mean()), 2) if len(losses) else 0.0
    m.expectancy_pct = round(float(resolved["pnl_pct"].mean()), 3)
    m.best_trade_pct = round(float(resolved["pnl_pct"].max()), 2)
    m.worst_trade_pct = round(float(resolved["pnl_pct"].min()), 2)
    m.avg_mae_pct = round(float(resolved["mae_pct"].mean()), 2)

    m.by_exit_reason = {
        reason: {
            "trades": int(len(g)),
            "profitable": int((g["pnl_pct"] > 0).sum()),
            "avg_pnl_pct": round(float(g["pnl_pct"].mean()), 2),
            "avg_hold": _humanize(float(g["duration_minutes"].mean())),
        }
        for reason, g in resolved.groupby("exit_reason", sort=False)
    }
    m.by_ticker = {
        ticker: {
            "asset_class": str(g["asset_class"].iloc[0]),
            "trades": int(len(g)),
            "wins": int((g["pnl_pct"] > 0).sum()),
            "win_rate_pct": round(float((g["pnl_pct"] > 0).mean() * 100), 1),
            "avg_pnl_pct": round(float(g["pnl_pct"].mean()), 2),
            "total_pnl_usd": round(float(g["pnl_usd"].sum()), 2),
        }
        for ticker, g in resolved.groupby("ticker", sort=False)
    }
    return m


def format_summary(m: BacktestMetrics, title: str = "BACKTEST SUMMARY", width: int = 74) -> str:
    """The end-of-run report. Returns a string so callers decide where it goes."""
    bar = "=" * width
    lines = [bar, title, bar]

    if m.total_trades == 0:
        lines += ["No signals were generated in this window.", bar, ""]
        return "\n".join(lines)

    pf = "inf (no losing trades)" if m.profit_factor == float("inf") else f"{m.profit_factor:.2f}"
    lines += [
        f"Total Trades Executed  : {m.total_trades}"
        + (f"   ({m.resolved_trades} closed, {m.open_trades} still open)" if m.open_trades else ""),
        f"Win Rate               : {m.win_rate_pct:.2f}%   ({m.wins} wins / {m.losses} losses)",
        f"Total ROI              : {m.total_roi_pct:+.2f}%   "
        f"(net {m.total_pnl_usd:+,.2f} on {m.initial_capital:,.0f} starting capital)",
        f"Max Drawdown           : {m.max_drawdown_pct:.2f}%   ({m.max_drawdown_usd:,.2f})",
        f"Profit Factor          : {pf}",
        f"Average Trade Duration : {m.avg_duration_label}",
        "",
        f"Sum of Returns         : {m.sum_return_pct:+.2f}%   (equal-weighted, non-compounded)",
        f"Expectancy per Trade   : {m.expectancy_pct:+.3f}%   "
        f"(avg win {m.avg_win_pct:+.2f}% / avg loss {m.avg_loss_pct:+.2f}%)",
        f"Best / Worst Trade     : {m.best_trade_pct:+.2f}% / {m.worst_trade_pct:+.2f}%",
        f"Avg Max Adverse Excur. : {m.avg_mae_pct:.2f}%   (worst intrabar mark before exit)",
    ]
    if m.max_drawdown_peak_time is not None:
        lines.append(
            f"Drawdown Window        : {pd.Timestamp(m.max_drawdown_peak_time):%Y-%m-%d %H:%M} -> "
            f"{pd.Timestamp(m.max_drawdown_trough_time):%Y-%m-%d %H:%M} UTC"
        )
    if m.open_trades:
        lines.append(
            f"Still Open             : {m.open_trades} position(s), "
            f"unrealised avg {m.unrealised_open_pct:+.2f}%  (excluded from every figure above)"
        )

    if m.by_exit_reason:
        lines += ["", "--- Exit Reason Breakdown " + "-" * (width - 26)]
        lines.append(f"{'Reason':<32}{'Trades':>8}{'Profitable':>12}{'AvgPnL%':>10}{'AvgHold':>14}")
        for reason, s in sorted(m.by_exit_reason.items(), key=lambda kv: -kv[1]["trades"]):
            hit_ratio = "{}/{}".format(s["profitable"], s["trades"])
            lines.append(
                f"{reason:<32}{s['trades']:>8}{hit_ratio:>12}"
                f"{s['avg_pnl_pct']:>+10.2f}{s['avg_hold']:>14}"
            )

    if m.by_ticker:
        lines += ["", "--- Per-Asset Performance " + "-" * (width - 26)]
        lines.append(f"{'Ticker':<12}{'Class':<9}{'Trades':>7}{'Wins':>6}{'WinRate':>10}{'AvgPnL%':>10}{'NetPnL$':>12}")
        rows = sorted(m.by_ticker.items(), key=lambda kv: (-kv[1]["win_rate_pct"], -kv[1]["trades"]))
        for ticker, s in rows:
            lines.append(
                f"{ticker:<12}{s['asset_class']:<9}{s['trades']:>7}{s['wins']:>6}"
                f"{s['win_rate_pct']:>9.1f}%{s['avg_pnl_pct']:>+10.2f}{s['total_pnl_usd']:>+12,.2f}"
            )

    lines += [bar, ""]
    return "\n".join(lines)
