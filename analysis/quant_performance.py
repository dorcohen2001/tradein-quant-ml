"""Quantitative Performance & Risk Analysis Engine.

Calculates institutional quantitative metrics, equity curves, drawdown statistics,
and Monte Carlo risk profiles from paper-trading records or backtest logs.

Usage:
    python analysis/quant_performance.py --model livne
    python analysis/quant_performance.py --model biton --export-charts
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.registry import get_model


@dataclass
class QuantMetrics:
    total_trades: int
    closed_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    profit_factor: float
    payoff_ratio: float
    expectancy: float
    cumulative_return: float
    annualized_return: float
    annualized_volatility: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    calmar_ratio: float
    avg_trade_pnl: float
    avg_win: float
    avg_loss: float
    max_win: float
    max_loss: float
    monte_carlo_var_95: float
    monte_carlo_max_dd_95: float


def load_trades_df(model_key: str) -> pd.DataFrame:
    """Load model trade store into a pandas DataFrame."""
    cfg = get_model(model_key)
    store_path = cfg.trade_store
    if not os.path.isfile(store_path):
        raise FileNotFoundError(f"Trade store not found for model '{model_key}': {store_path}")

    with open(store_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if not data:
        return pd.DataFrame()

    df = pd.DataFrame(data)
    if "pnl_pct" in df.columns:
        df["pnl_pct"] = pd.to_numeric(df["pnl_pct"], errors="coerce")
    if "entry_date" in df.columns:
        df["entry_date"] = pd.to_datetime(df["entry_date"], errors="coerce")
    if "exit_date" in df.columns:
        df["exit_date"] = pd.to_datetime(df["exit_date"], errors="coerce")
    return df


def calculate_quant_metrics(
    df: pd.DataFrame,
    closed_statuses: tuple[str, ...],
    risk_free_rate: float = 0.04,
    mc_simulations: int = 1000,
) -> QuantMetrics:
    """Calculate institutional risk and performance metrics."""
    if df.empty:
        raise ValueError("Cannot calculate metrics on empty DataFrame")

    total_trades = len(df)
    closed = df[df["current_status"].isin(closed_statuses) & df["pnl_pct"].notna()].copy()
    closed_count = len(closed)

    if closed_count == 0:
        raise ValueError("No closed trades with valid PnL found")

    pnl = closed["pnl_pct"].values / 100.0  # Decimal form

    wins = pnl[pnl > 0]
    losses = pnl[pnl < 0]
    win_count = len(wins)
    loss_count = len(losses)

    win_rate = win_count / closed_count if closed_count > 0 else 0.0
    sum_wins = float(np.sum(wins)) if win_count > 0 else 0.0
    sum_losses = float(np.abs(np.sum(losses))) if loss_count > 0 else 0.0

    profit_factor = (sum_wins / sum_losses) if sum_losses > 0 else (float("inf") if sum_wins > 0 else 0.0)
    avg_win = float(np.mean(wins)) if win_count > 0 else 0.0
    avg_loss = float(np.mean(losses)) if loss_count > 0 else 0.0
    payoff_ratio = (avg_win / abs(avg_loss)) if avg_loss != 0 else 0.0
    expectancy = (win_rate * avg_win) + ((1.0 - win_rate) * avg_loss)

    # Compounded Equity Curve
    equity = np.cumprod(1.0 + pnl)
    cumulative_return = float(equity[-1] - 1.0) if len(equity) > 0 else 0.0

    # Drawdown series
    running_max = np.maximum.accumulate(equity)
    drawdowns = (equity - running_max) / running_max
    max_drawdown = float(abs(np.min(drawdowns))) if len(drawdowns) > 0 else 0.0

    # Annualization assuming typical trading frequency (~252 trading periods/year)
    # Estimate trades per year from date range if available
    periods_per_year = 252.0
    if "exit_date" in closed.columns and closed["exit_date"].notna().sum() > 2:
        valid_dates = pd.to_datetime(closed["exit_date"].dropna(), errors="coerce").dropna()
        if len(valid_dates) > 2:
            date_span_days = max(1, (valid_dates.max() - valid_dates.min()).days)
            trades_per_year = (closed_count / date_span_days) * 365.25
            periods_per_year = max(12.0, trades_per_year)

    mean_ret = float(np.mean(pnl))
    std_ret = float(np.std(pnl, ddof=1)) if len(pnl) > 1 else 1e-6
    downside_std = float(np.std(pnl[pnl < 0], ddof=1)) if len(losses) > 1 else 1e-6

    # Annualized Return & Volatility
    annualized_return = float((1.0 + mean_ret) ** periods_per_year - 1.0)
    annualized_volatility = float(std_ret * np.sqrt(periods_per_year))

    rf_per_period = (1.0 + risk_free_rate) ** (1.0 / periods_per_year) - 1.0
    excess_mean = mean_ret - rf_per_period

    sharpe_ratio = float((excess_mean / std_ret) * np.sqrt(periods_per_year)) if std_ret > 0 else 0.0
    sortino_ratio = float((excess_mean / downside_std) * np.sqrt(periods_per_year)) if downside_std > 0 else 0.0
    calmar_ratio = (annualized_return / max_drawdown) if max_drawdown > 0 else 0.0

    # Monte Carlo Bootstrapping for Risk Profile (95% VaR and 95% Max DD)
    rng = np.random.default_rng(seed=42)
    mc_terminal_returns = []
    mc_max_dds = []

    for _ in range(mc_simulations):
        sampled_pnl = rng.choice(pnl, size=closed_count, replace=True)
        sim_eq = np.cumprod(1.0 + sampled_pnl)
        mc_terminal_returns.append(sim_eq[-1] - 1.0)
        sim_rm = np.maximum.accumulate(sim_eq)
        sim_dd = (sim_eq - sim_rm) / sim_rm
        mc_max_dds.append(abs(np.min(sim_dd)))

    var_95 = float(np.percentile(mc_terminal_returns, 5))
    max_dd_95 = float(np.percentile(mc_max_dds, 95))

    return QuantMetrics(
        total_trades=total_trades,
        closed_trades=closed_count,
        winning_trades=win_count,
        losing_trades=loss_count,
        win_rate=win_rate,
        profit_factor=profit_factor,
        payoff_ratio=payoff_ratio,
        expectancy=expectancy,
        cumulative_return=cumulative_return,
        annualized_return=annualized_return,
        annualized_volatility=annualized_volatility,
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        max_drawdown=max_drawdown,
        calmar_ratio=calmar_ratio,
        avg_trade_pnl=mean_ret,
        avg_win=avg_win,
        avg_loss=avg_loss,
        max_win=float(np.max(wins)) if win_count > 0 else 0.0,
        max_loss=float(np.min(losses)) if loss_count > 0 else 0.0,
        monte_carlo_var_95=var_95,
        monte_carlo_max_dd_95=max_dd_95,
    )


def format_metrics_report(model_key: str, m: QuantMetrics) -> str:
    """Format metrics into an institutional report."""
    return f"""================================================================================
INSTITUTIONAL QUANTITATIVE PERFORMANCE REPORT: [{model_key.upper()}]
================================================================================
TRADE SAMPLE & WIN METRICS:
  Total Logged Trades        : {m.total_trades}
  Closed Resolved Trades     : {m.closed_trades}
  Win Rate                   : {m.win_rate:.2%} ({m.winning_trades} wins / {m.losing_trades} losses)
  Profit Factor              : {m.profit_factor:.2f}
  Payoff Ratio (Avg W / Avg L): {m.payoff_ratio:.2f}
  Mathematical Expectancy    : {m.expectancy:+.2%} per trade

RETURN & VOLATILITY PROFILE:
  Cumulative Strategy Return : {m.cumulative_return:+.2%}
  Annualized Return (CAGR)   : {m.annualized_return:+.2%}
  Annualized Volatility      : {m.annualized_volatility:.2%}
  Average Trade PnL          : {m.avg_trade_pnl:+.2%}
  Average Winning Trade      : {m.avg_win:+.2%}
  Average Losing Trade       : {m.avg_loss:+.2%}
  Max Single Win / Loss      : {m.max_win:+.2%} / {m.max_loss:+.2%}

RISK-ADJUSTED METRICS:
  Sharpe Ratio (Annualized)  : {m.sharpe_ratio:.2f}
  Sortino Ratio (Downside)   : {m.sortino_ratio:.2f}
  Maximum Drawdown (MDD)     : {m.max_drawdown:.2%}
  Calmar Ratio (CAGR / MDD)  : {m.calmar_ratio:.2f}

MONTE CARLO RISK ANALYSIS (1,000 BOOTSTRAP RUNS):
  95% Confidence VaR         : {m.monte_carlo_var_95:+.2%}
  95% Worst-Case Drawdown    : {m.monte_carlo_max_dd_95:.2%}
================================================================================
"""


def main():
    parser = argparse.ArgumentParser(description="TRADEIN Quantitative Risk & Performance Analytics")
    parser.add_argument("--model", default="livne", help="Model key (default: livne)")
    parser.add_argument("--export-charts", action="store_true", help="Generate performance charts")
    args = parser.parse_args()

    cfg = get_model(args.model)
    df = load_trades_df(args.model)
    metrics = calculate_quant_metrics(df, cfg.closed_with_pnl)
    print(format_metrics_report(args.model, metrics))


if __name__ == "__main__":
    main()
