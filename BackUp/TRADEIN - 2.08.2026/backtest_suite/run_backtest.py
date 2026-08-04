"""Main CLI launcher for the backtesting suite.

Walks day-by-day through a historical window and, on each date T, feeds only
data up to and including T into the *exact same* production TA/scoring/trade
building blocks used by the live pipeline (ta_engine.analyze, market_screener
scoring, macro_analyzer's direction gate, generator.trade_generator's
entry/stop/target construction). Nothing in this file re-implements that
math -- it only replicates the macro *regime-computation* orchestration
(see `_stock_regime` / `_crypto_regime` below), because macro_analyzer's
public functions always fetch "now" data and aren't parameterized by a
historical date. The decision thresholds themselves still come from
config.py, so there is a single source of truth for the rules.

Usage:
    python -m backtest_suite.run_backtest --start-date 2026-05-01 --end-date 2026-07-01
    python -m backtest_suite.run_backtest --start-date 2026-05-01 --end-date 2026-07-01 --asset-class crypto
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd

from backtest_suite.historical_data import (
    fetch_crypto_klines,
    fetch_stock_history,
    slice_upto,
    trading_dates,
)
from backtest_suite.simulator import TradeOutcome, simulate_trade
from config import (
    CRYPTO_BENCHMARK,
    MIN_DOLLAR_VOLUME,
    RS_LOOKBACK_DAYS,
    STOCK_BENCHMARKS,
    USDT_DOMINANCE_RISING_PCT,
    VIX_ELEVATED,
    VIX_HIGH_VOL,
    WATCHLIST_CRYPTO,
    WATCHLIST_STOCKS,
)
from generator.trade_generator import _build_trade_params
from macro.macro_analyzer import MacroContext
from screener.market_screener import Candidate, _n_day_return, _score_candidate
from ta.ta_engine import add_moving_averages, analyze, determine_trend, volume_spike


# ---------------------------------------------------------------------------
# Point-in-time macro regime (mirrors macro_analyzer.py's decision rules)
# ---------------------------------------------------------------------------
def _stock_regime(spy_slice: pd.DataFrame, qqq_slice: pd.DataFrame, vix_slice: pd.DataFrame) -> str:
    spy_trend = determine_trend(add_moving_averages(spy_slice))
    qqq_trend = determine_trend(add_moving_averages(qqq_slice))
    vix_level = float(vix_slice["Close"].iloc[-1]) if not vix_slice.empty else 0.0

    if vix_level >= VIX_HIGH_VOL:
        return "High Volatility"
    if spy_trend == "UPTREND" and qqq_trend == "UPTREND" and vix_level < VIX_ELEVATED:
        return "Bullish"
    if spy_trend == "DOWNTREND" and qqq_trend == "DOWNTREND":
        return "Bearish"
    return "Neutral"


def _crypto_regime(btc_slice: pd.DataFrame, eth_slice: pd.DataFrame) -> str:
    btc_trend = determine_trend(add_moving_averages(btc_slice))
    eth_trend = determine_trend(add_moving_averages(eth_slice))

    risk_on = True
    if len(btc_slice) > RS_LOOKBACK_DAYS:
        btc_5d_ret = btc_slice["Close"].iloc[-1] / btc_slice["Close"].iloc[-1 - RS_LOOKBACK_DAYS] - 1
        risk_on = btc_5d_ret >= -USDT_DOMINANCE_RISING_PCT

    if btc_trend == "UPTREND" and eth_trend == "UPTREND" and risk_on:
        return "Bullish"
    if btc_trend == "DOWNTREND" and eth_trend == "DOWNTREND":
        return "Bearish"
    if not risk_on and btc_trend != "UPTREND":
        return "High Volatility"
    return "Neutral"


# ---------------------------------------------------------------------------
# Backtest core loop
# ---------------------------------------------------------------------------
def run_backtest(
    start: dt.date,
    end: dt.date,
    asset_class: str = "both",
    forward_days: int = 10,
    max_tickers: int | None = None,
) -> list[TradeOutcome]:
    outcomes: list[TradeOutcome] = []
    classes = ["stocks", "crypto"] if asset_class == "both" else [asset_class]

    for cls in classes:
        if cls == "stocks":
            watchlist = WATCHLIST_STOCKS[:max_tickers] if max_tickers else WATCHLIST_STOCKS
            print(f"[backtest] Fetching {len(watchlist)} stock histories + SPY/QQQ/VIX benchmarks...", file=sys.stderr)
            spy_df = fetch_stock_history(STOCK_BENCHMARKS["default"], start, end)
            qqq_df = fetch_stock_history("QQQ", start, end)
            vix_df = fetch_stock_history("^VIX", start, end)
            asset_dfs = {t: fetch_stock_history(t, start, end) for t in watchlist}
            calendar_df = spy_df
        else:
            watchlist = WATCHLIST_CRYPTO[:max_tickers] if max_tickers else WATCHLIST_CRYPTO
            print(f"[backtest] Fetching {len(watchlist)} crypto histories + BTC/ETH benchmarks...", file=sys.stderr)
            btc_df = fetch_crypto_klines(CRYPTO_BENCHMARK, start, end)
            eth_df = fetch_crypto_klines("ETHUSDT", start, end)
            asset_dfs = {s: fetch_crypto_klines(s, start, end) for s in watchlist}
            calendar_df = btc_df

        dates = trading_dates(calendar_df, start, end)
        print(f"[backtest] Replaying {len(dates)} trading days for {cls}...", file=sys.stderr)

        open_until: dict[str, pd.Timestamp] = {}  # ticker -> last date its open trade still runs

        for T in dates:
            if cls == "stocks":
                regime = _stock_regime(slice_upto(spy_df, T), slice_upto(qqq_df, T), slice_upto(vix_df, T))
            else:
                regime = _crypto_regime(slice_upto(btc_df, T), slice_upto(eth_df, T))
            allowed = MacroContext(asset_class=cls, regime=regime, details={}, summary="").allowed_directions()

            bench_slice = slice_upto(spy_df if cls == "stocks" else btc_df, T)
            bench_ret = _n_day_return(bench_slice, RS_LOOKBACK_DAYS)

            for ticker, full_df in asset_dfs.items():
                if full_df.empty:
                    continue
                if ticker in open_until and T <= open_until[ticker]:
                    continue  # a trade on this ticker is still live; don't stack signals

                sliced = slice_upto(full_df, T)
                if len(sliced) < 60:
                    continue  # not enough warmup history yet for this historical date

                price = float(sliced["Close"].iloc[-1])
                volume = float(sliced["Volume"].iloc[-1])
                dollar_volume = price * volume
                if dollar_volume < MIN_DOLLAR_VOLUME:
                    continue

                ta_result = analyze(ticker, sliced)
                if not (ta_result.ma_hits or ta_result.in_golden_zone):
                    continue

                direction = ta_result.direction_bias
                if direction == "NEUTRAL" or direction not in allowed:
                    continue

                _, ratio = volume_spike(sliced)
                asset_ret = _n_day_return(sliced, RS_LOOKBACK_DAYS)
                rs_pct = (asset_ret - bench_ret) * 100

                # earnings_hours_away is intentionally None: a real point-in-time
                # earnings calendar isn't available for free, and using "today's"
                # next-earnings date here would leak future information.
                score, breakdown = _score_candidate(ta_result, ratio, dollar_volume, rs_pct, None)
                candidate = Candidate(
                    ticker=ticker, asset_class=cls, score=score, ta=ta_result,
                    dollar_volume=dollar_volume, vol_ratio=ratio, rs_pct=rs_pct,
                    earnings_hours_away=None, breakdown=breakdown,
                )

                params = _build_trade_params(candidate, direction)
                if params is None:
                    continue

                trade = simulate_trade(
                    full_df, ticker, cls, direction, T,
                    params["entry_low"], params["entry_high"], params["stop"],
                    params["target1"], params["target2"], score, max_days=forward_days,
                )
                outcomes.append(trade)
                open_until[ticker] = trade.exit_date if trade.exit_date is not None else T

        print(f"[backtest] {cls}: {sum(1 for o in outcomes if o.asset_class == cls)} signals generated", file=sys.stderr)

    return outcomes


# ---------------------------------------------------------------------------
# Metrics & report
# ---------------------------------------------------------------------------
def print_report(outcomes: list[TradeOutcome], start: dt.date, end: dt.date, forward_days: int) -> None:
    print("\n" + "=" * 64)
    print(f"BACKTEST REPORT  |  {start} -> {end}  |  max hold {forward_days}d")
    print("=" * 64)

    total = len(outcomes)
    if total == 0:
        print("No signals were generated in this window.")
        return

    wins = [o for o in outcomes if o.outcome == "WIN"]
    losses = [o for o in outcomes if o.outcome == "LOSS"]
    expired = [o for o in outcomes if o.outcome == "EXPIRED"]

    win_rate = len(wins) / total * 100
    decided = len(wins) + len(losses)
    decided_win_rate = (len(wins) / decided * 100) if decided else 0.0

    gross_profit = sum(o.pnl_pct for o in outcomes if o.pnl_pct > 0)
    gross_loss = abs(sum(o.pnl_pct for o in outcomes if o.pnl_pct < 0))
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    strategy_return = sum(o.pnl_pct for o in outcomes)

    print(f"Total Signals Generated : {total}")
    print(f"Win Rate (of all signals): {win_rate:.1f}%   |  Win Rate (decided only): {decided_win_rate:.1f}%")
    print(f"Wins / Losses / Expired : {len(wins)} / {len(losses)} / {len(expired)}")
    pf_str = "inf (no losses)" if profit_factor == float("inf") else f"{profit_factor:.2f}"
    print(f"Profit Factor           : {pf_str}")
    print(f"Strategy Return (sum)   : {strategy_return:+.2f}%  (equal-weighted, non-compounded across all signals)")

    print("\n--- Per-Asset Performance ---")
    by_ticker: dict[str, list[TradeOutcome]] = defaultdict(list)
    for o in outcomes:
        by_ticker[o.ticker].append(o)

    rows = []
    for ticker, trades in by_ticker.items():
        n = len(trades)
        w = sum(1 for t in trades if t.outcome == "WIN")
        wr = w / n * 100
        avg_pnl = sum(t.pnl_pct for t in trades) / n
        rows.append((ticker, trades[0].asset_class, n, w, wr, avg_pnl))

    rows.sort(key=lambda r: (-r[4], -r[2]))
    print(f"{'Ticker':<10}{'Class':<8}{'Trades':<8}{'Wins':<6}{'WinRate':<10}{'AvgPnL%':<10}")
    for ticker, cls, n, w, wr, avg_pnl in rows:
        print(f"{ticker:<10}{cls:<8}{n:<8}{w:<6}{wr:>6.1f}%   {avg_pnl:>+7.2f}%")

    print("=" * 64 + "\n")


# ---------------------------------------------------------------------------
# Itemized trade log: terminal (--verbose) + CSV export (always)
# ---------------------------------------------------------------------------
_CSV_COLUMNS = [
    "signal_date", "ticker", "asset_class", "direction",
    "entry_price", "entry_low", "entry_high",
    "stop_loss", "target1", "target2",
    "exit_date", "exit_price", "outcome", "pnl_pct", "days_held", "score",
]


def _log_row(o: TradeOutcome) -> dict:
    return {
        "signal_date": o.signal_date.strftime("%Y-%m-%d"),
        "ticker": o.ticker,
        "asset_class": o.asset_class,
        "direction": o.direction,
        "entry_price": round(o.entry_price, 6),
        "entry_low": round(o.entry_low, 6),
        "entry_high": round(o.entry_high, 6),
        "stop_loss": round(o.stop, 6),
        "target1": round(o.target1, 6),
        "target2": round(o.target2, 6),
        "exit_date": o.exit_date.strftime("%Y-%m-%d") if o.exit_date is not None else "",
        "exit_price": round(o.exit_price, 6),
        "outcome": o.combined_outcome(),
        "pnl_pct": o.pnl_pct,
        "days_held": o.days_held,
        "score": o.score,
    }


def print_trade_log(outcomes: list[TradeOutcome]) -> None:
    """Itemized per-trade log for --verbose runs: one entry per signal with
    trigger date, direction, entry/stop/targets, exit, and outcome/PnL."""
    if not outcomes:
        return

    ordered = sorted(outcomes, key=lambda o: o.signal_date)

    print("--- Trade Log " + "-" * 49)
    for o in ordered:
        exit_date_str = o.exit_date.strftime("%Y-%m-%d") if o.exit_date is not None else "n/a"
        print(
            f"[{o.signal_date.strftime('%Y-%m-%d')}] {o.ticker:<10}({o.asset_class:<6}) "
            f"{o.direction:<5} | Entry {o.entry_price:,.4f}  Stop {o.stop:,.4f}  "
            f"TP1 {o.target1:,.4f}  TP2 {o.target2:,.4f}"
        )
        print(
            f"    Exit {exit_date_str} @ {o.exit_price:,.4f}  ->  {o.combined_outcome():<9}  "
            f"PnL {o.pnl_pct:+.2f}%  ({o.days_held}d held, score {o.score})"
        )
    print("-" * 64 + "\n")


def export_trades_csv(outcomes: list[TradeOutcome], path: Path | None = None) -> Path:
    """Write every simulated trade to backtest_suite/trades_log.csv (sorted
    chronologically) so results can be cross-referenced against historical
    charts (e.g. TradingView) or analyzed in a spreadsheet."""
    if path is None:
        path = Path(__file__).resolve().parent / "trades_log.csv"

    ordered = sorted(outcomes, key=lambda o: o.signal_date)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for o in ordered:
            writer.writerow(_log_row(o))

    return path


def main():
    parser = argparse.ArgumentParser(description="Historical backtest of the market analysis pipeline's signal logic")
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--asset-class", choices=["stocks", "crypto", "both"], default="both")
    parser.add_argument("--forward-days", type=int, default=10, help="Max trading days to hold a simulated trade")
    parser.add_argument("--max-tickers", type=int, default=None, help="Limit watchlist size per class (for faster test runs)")
    parser.add_argument("--verbose", action="store_true", help="Print an itemized trade-by-trade log to the terminal")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date)
    if start >= end:
        parser.error("--start-date must be before --end-date")

    outcomes = run_backtest(start, end, args.asset_class, args.forward_days, args.max_tickers)

    if args.verbose:
        print_trade_log(outcomes)

    print_report(outcomes, start, end, args.forward_days)

    csv_path = export_trades_csv(outcomes)
    print(f"[backtest] Trade log exported to {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
