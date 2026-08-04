"""Main CLI launcher for the backtesting suite.

Walks day-by-day through a historical window and, on each date T, feeds only
data up to and including T into the *same* production structural building
blocks used by the live pipeline: `ta.mtfa.htf_pass` (structure/OB/RSI
divergence/zone-match), `ta.candles`/`ta.ta_engine.volume_spike` for the
trigger check, and `risk.risk_manager` for stop/target construction and the
R:R gate. The decision thresholds themselves still come from config.py, so
there is a single source of truth for the rules.

Two scope boundaries versus the live/paper-trading engine, both because this
dataset only has historical *daily* OHLCV (see `ta_mtfa_backtest` docstring
below for why):

  1. HTF structure uses weekly/monthly bars resampled from the daily history
     (same as live), but the ITF 12H/8H/4H/1H trigger cascade is approximated
     by a single "trigger" pass directly on the daily bar itself -- Stage 7's
     `detect_braking_event` still scans a multi-bar window for the same four
     braking/reversal signatures the live path checks on 4H/1H, just applied
     to daily bars, plus daily volume spike + daily 200MA alignment + daily
     Fib golden zone -- instead of true intraday timeframes.
  2. The liquidity-heatmap filter and the intraday emergency-reversal exit
     are live/paper-trading-only features and are not simulated here --
     historical order-book snapshots and intraday volume-anomaly candles
     aren't available. `evaluate_setup`-equivalent scoring uses a neutral
     placeholder `LiquidityContext` so `screener._score_candidate` (the same
     function the live pipeline uses) still runs unmodified.

Point-in-time macro regime (`_stock_regime` / `_crypto_regime`) mirrors
macro_analyzer.py's SPY/QQQ and BTC/ETH trend rules, because macro_analyzer's
public functions always fetch "now" data and aren't parameterized by a
historical date. USDT.D/TOTAL-market-cap trend gating is also live-only (no
free historical dominance/mcap time series exists to backtest against).

Trades have no fixed holding-period cutoff -- `backtest_suite.simulator`
walks each signal forward until it hits its stop, a target, or a dynamic
trend-reversal exit, however long that takes. A trade still unresolved when
the historical data runs out is reported OPEN (unrealized), not force-closed;
`print_report` below excludes OPEN trades from win-rate/profit-factor math
and reports them separately. See `simulator.py`'s module docstring for why a
finite backtest can't mean "wait forever."

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
    BACKTEST_TRADES_CSV,
    CRYPTO_BENCHMARK,
    ITF_VOLUME_LOOKBACK,
    ITF_VOLUME_SPIKE_MULT,
    MIN_CRYPTO_DOLLAR_VOLUME_SMA20,
    MIN_STOCK_DOLLAR_VOLUME_SMA20,
    PRIMARY_MA_PERIOD,
    RS_LOOKBACK_DAYS,
    STOCK_BENCHMARKS,
    USDT_DOMINANCE_RISING_PCT,
    VIX_ELEVATED,
    VIX_HIGH_VOL,
    WATCHLIST_CRYPTO,
    WATCHLIST_STOCKS,
)
from liquidity.orderbook import LiquidityContext
from risk.risk_manager import compute_stop, compute_targets, position_size_estimate, validate_min_rr
from screener.market_screener import Candidate, _dollar_volume_sma, _n_day_return, _score_candidate
from strategy.strategy_engine import StrategyDecision, entry_zone
from ta.mtfa import MTFASetup, detect_braking_event, fib_reaction, htf_pass, swing_excluding_recent
from ta.ta_engine import add_moving_averages, atr, determine_trend, fibonacci_levels, volume_spike


def _within(price: float, a: float, b: float) -> bool:
    lo, hi = sorted((a, b))
    return lo <= price <= hi


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


def _allowed_from_regime(regime: str) -> set[str]:
    """Mirrors `macro.macro_analyzer`'s Bullish->LONG / Bearish->SHORT /
    else-both mapping (the USDT.D/SPY-QQQ correlation rule), without the
    live-only USDT.D/TOTAL trend refinement."""
    if regime == "Bullish":
        return {"LONG"}
    if regime == "Bearish":
        return {"SHORT"}
    return {"LONG", "SHORT"}


# ---------------------------------------------------------------------------
# Point-in-time MTFA-equivalent signal: HTF pass (weekly/monthly resampled
# from daily) + a daily-bar trigger proxy standing in for the live ITF cascade.
# ---------------------------------------------------------------------------
def ta_mtfa_backtest(ticker: str, asset_class: str, sliced_daily: pd.DataFrame, allowed: set[str]) -> MTFASetup | None:
    if len(sliced_daily) < 60:
        return None

    weekly = sliced_daily.resample("W").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open", "High", "Low", "Close"])
    monthly = sliced_daily.resample("ME").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open", "High", "Low", "Close"])

    htf = htf_pass(weekly, monthly)
    if htf is None or htf["direction"] not in allowed:
        return None

    direction = htf["direction"]

    daily_ma = add_moving_averages(sliced_daily, periods=(PRIMARY_MA_PERIOD,))
    ema200_val = daily_ma[f"EMA{PRIMARY_MA_PERIOD}"].iloc[-1]
    if pd.isna(ema200_val):
        return None
    ema200 = float(ema200_val)
    price = float(sliced_daily["Close"].iloc[-1])
    ma_aligned = 1 if (
        (direction == "LONG" and price >= ema200) or (direction == "SHORT" and price <= ema200)
    ) else 0
    if not ma_aligned:
        return None

    # Stage 7 zoom-in (daily-bar proxy -- no historical 4H/1H data exists to
    # backtest against, see module scope-boundary note): scan the last few
    # daily bars for a braking/reversal event via the same shared detector
    # Stage 7 uses live on 4H/1H.
    braking_found, braking_pattern = detect_braking_event(sliced_daily, direction)
    if not braking_found:
        return None

    vol_ok, vol_ratio = volume_spike(sliced_daily, mult=ITF_VOLUME_SPIKE_MULT, lookback=ITF_VOLUME_LOOKBACK)
    if not vol_ok:
        return None

    hi2, lo2 = swing_excluding_recent(sliced_daily)
    daily_fib = fibonacci_levels(hi2, lo2)
    contained = _within(price, daily_fib["0.5"], daily_fib["0.786"])
    reacted = fib_reaction(sliced_daily, daily_fib, direction)
    if not (contained or reacted):
        return None

    atr_val = atr(sliced_daily).iloc[-1]
    daily_atr = float(atr_val) if not pd.isna(atr_val) else None

    fib_note = "inside daily Fib golden zone" if contained else "reacted off daily 0.5/0.618 Fib level"
    thesis = (
        f"[BACKTEST] HTF ({htf['htf_trend']}, monthly {htf['monthly_trend']}) {htf['zone_type']} zone "
        f"{htf['zone'][0]:.4g}-{htf['zone'][1]:.4g}; daily-bar trigger proxy: {braking_pattern} braking/reversal "
        f"event on {vol_ratio}x volume, {fib_note}"
    )

    return MTFASetup(
        ticker=ticker, asset_class=asset_class, price=price, direction_bias=direction,
        htf_trend=htf["htf_trend"], htf_zone_type=htf["zone_type"], htf_zone=htf["zone"],
        divergence=htf["divergence"], order_blocks=htf["obs"],
        nearest_support=htf["near"].get("nearest_support"), nearest_resistance=htf["near"].get("nearest_resistance"),
        swing_high=htf["hi"], swing_low=htf["lo"], fib_levels=htf["fib"], monthly_trend=htf["monthly_trend"],
        itf_trigger_tf="1d_proxy", itf_pattern=braking_pattern, itf_volume_ratio=vol_ratio,
        itf_ma200_aligned_count=ma_aligned, itf_fib_levels=daily_fib, itf_ema200=ema200,
        itf_atr=daily_atr, thesis=thesis, htf_volume_ratio=htf["htf_volume_ratio"],
    )


_NEUTRAL_LIQUIDITY = LiquidityContext(source="unavailable_backtest", above_notional=0.0, below_notional=0.0,
                                       favored_direction=None, top_clusters=[])


def build_backtest_decision(setup: MTFASetup) -> StrategyDecision | None:
    direction = setup.direction_bias
    entry_low, entry_high = entry_zone(direction, setup.price)
    entry_price = (entry_low + entry_high) / 2

    stop = compute_stop(direction, setup, entry_price)
    if stop is None:
        return None

    targets = compute_targets(direction, setup, entry_price, stop)
    target1, target2 = targets["target1"], targets["target2"]
    if target1 is None or target2 is None:
        return None

    ok, rr1, rr2 = validate_min_rr(entry_price, stop, target1, target2)
    if not ok:
        return None

    return StrategyDecision(
        ticker=setup.ticker, asset_class=setup.asset_class, direction=direction,
        entry_low=entry_low, entry_high=entry_high, entry_price=entry_price,
        stop=stop, target1=target1, target2=target2, rr1=rr1, rr2=rr2,
        tp1_close_pct=0.5, breakeven_price=entry_price,
        setup=setup, liquidity=_NEUTRAL_LIQUIDITY, position_size=position_size_estimate(entry_price, stop),
    )


# ---------------------------------------------------------------------------
# Backtest core loop
# ---------------------------------------------------------------------------
def run_backtest(
    start: dt.date,
    end: dt.date,
    asset_class: str = "both",
    max_tickers: int | None = None,
    enable_dynamic_exit: bool = True,
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
            allowed = _allowed_from_regime(regime)

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

                dollar_volume = _dollar_volume_sma(sliced)
                liquidity_floor = MIN_CRYPTO_DOLLAR_VOLUME_SMA20 if cls == "crypto" else MIN_STOCK_DOLLAR_VOLUME_SMA20
                if dollar_volume < liquidity_floor:
                    continue

                setup = ta_mtfa_backtest(ticker, cls, sliced, allowed)
                if setup is None:
                    continue

                decision = build_backtest_decision(setup)
                if decision is None:
                    continue

                asset_ret = _n_day_return(sliced, RS_LOOKBACK_DAYS)
                rs_pct = (asset_ret - bench_ret) * 100

                # earnings_hours_away is intentionally None: a real point-in-time
                # earnings calendar isn't available for free, and using "today's"
                # next-earnings date here would leak future information.
                score, breakdown = _score_candidate(decision, dollar_volume, rs_pct, None)
                candidate = Candidate(
                    ticker=ticker, asset_class=cls, score=score, decision=decision,
                    dollar_volume=dollar_volume, rs_pct=rs_pct,
                    earnings_hours_away=None, breakdown=breakdown,
                )

                trade = simulate_trade(
                    full_df, ticker, cls, decision.direction, T,
                    decision.entry_low, decision.entry_high, decision.stop,
                    decision.target1, decision.target2, candidate.score,
                    enable_dynamic_exit=enable_dynamic_exit,
                )
                outcomes.append(trade)
                # exit_date is always set now (OPEN trades use the last available
                # bar), so the ticker stays blocked from a new signal for as long
                # as its position is still open -- including indefinitely, if it
                # never resolved before the backtest's data ran out.
                open_until[ticker] = trade.exit_date

        print(f"[backtest] {cls}: {sum(1 for o in outcomes if o.asset_class == cls)} signals generated", file=sys.stderr)

    return outcomes


# ---------------------------------------------------------------------------
# Metrics & report
# ---------------------------------------------------------------------------
def print_report(outcomes: list[TradeOutcome], start: dt.date, end: dt.date) -> None:
    print("\n" + "=" * 64)
    print(f"BACKTEST REPORT  |  {start} -> {end}  |  no fixed holding-period cutoff")
    print("=" * 64)

    total = len(outcomes)
    if total == 0:
        print("No signals were generated in this window.")
        return

    # Only RESOLVED trades (WIN/LOSS -- an actual stop/target/reversal exit)
    # feed win-rate/profit-factor. OPEN trades are unrealized marks against
    # the last available close, not a completed result -- mixing them into
    # "win rate" would be misleading in either direction.
    resolved = [o for o in outcomes if o.is_resolved]
    open_trades = [o for o in outcomes if not o.is_resolved]

    if not resolved:
        print(f"Total Signals Generated : {total}  (0 resolved -- all still OPEN, see below)")
    else:
        # Win/loss is based on realized P&L (`is_profitable`), not the exit-
        # mechanism label -- a trade that exits on a trend-reversal signal (or
        # hits breakeven after TP1) with positive P&L is still a win.
        # `tag`/`combined_outcome()` remain available for *how* trades exited,
        # kept separate from *whether* they were profitable.
        wins = [o for o in resolved if o.is_profitable]
        losses = [o for o in resolved if not o.is_profitable]
        win_rate = len(wins) / len(resolved) * 100

        gross_profit = sum(o.pnl_pct for o in resolved if o.pnl_pct > 0)
        gross_loss = abs(sum(o.pnl_pct for o in resolved if o.pnl_pct < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
        strategy_return = sum(o.pnl_pct for o in resolved)

        print(f"Total Signals Generated : {total}  ({len(resolved)} resolved, {len(open_trades)} still OPEN)")
        print(f"Win Rate (resolved)     : {win_rate:.1f}%  ({len(wins)} profitable / {len(losses)} unprofitable)")
        pf_str = "inf (no losses)" if profit_factor == float("inf") else f"{profit_factor:.2f}"
        print(f"Profit Factor           : {pf_str}")
        print(f"Strategy Return (sum)   : {strategy_return:+.2f}%  (resolved trades only, equal-weighted, non-compounded)")

        print("\n--- Exit Reason Breakdown (resolved trades) ---")
        by_tag: dict[str, list[TradeOutcome]] = defaultdict(list)
        for o in resolved:
            by_tag[o.tag].append(o)
        for tag, trades in sorted(by_tag.items(), key=lambda kv: -len(kv[1])):
            n = len(trades)
            profitable = sum(1 for t in trades if t.is_profitable)
            avg_pnl = sum(t.pnl_pct for t in trades) / n
            avg_days = sum(t.days_held for t in trades) / n
            print(f"  {tag:<16}{n:>4} trades   {profitable}/{n} profitable   avg PnL {avg_pnl:>+6.2f}%   avg hold {avg_days:.0f}d")

        print("\n--- Per-Asset Performance (resolved trades) ---")
        by_ticker: dict[str, list[TradeOutcome]] = defaultdict(list)
        for o in resolved:
            by_ticker[o.ticker].append(o)

        rows = []
        for ticker, trades in by_ticker.items():
            n = len(trades)
            w = sum(1 for t in trades if t.is_profitable)
            wr = w / n * 100
            avg_pnl = sum(t.pnl_pct for t in trades) / n
            rows.append((ticker, trades[0].asset_class, n, w, wr, avg_pnl))

        rows.sort(key=lambda r: (-r[4], -r[2]))
        print(f"{'Ticker':<10}{'Class':<8}{'Trades':<8}{'Wins':<6}{'WinRate':<10}{'AvgPnL%':<10}")
        for ticker, cls, n, w, wr, avg_pnl in rows:
            print(f"{ticker:<10}{cls:<8}{n:<8}{w:<6}{wr:>6.1f}%   {avg_pnl:>+7.2f}%")

    if open_trades:
        avg_open_pnl = sum(o.pnl_pct for o in open_trades) / len(open_trades)
        print(f"\n--- Still OPEN at end of available data ({len(open_trades)} positions, unrealized avg PnL {avg_open_pnl:+.2f}%) ---")
        for o in sorted(open_trades, key=lambda o: o.signal_date):
            print(
                f"  {o.ticker:<10}({o.asset_class:<6}) {o.direction:<5} signaled {o.signal_date.strftime('%Y-%m-%d')}, "
                f"still open as of {o.exit_date.strftime('%Y-%m-%d')}  unrealized PnL {o.pnl_pct:+.2f}%"
            )

    print("=" * 64 + "\n")


# ---------------------------------------------------------------------------
# Itemized trade log: terminal (--verbose) + CSV export (always)
# ---------------------------------------------------------------------------
_CSV_COLUMNS = [
    "signal_date", "ticker", "asset_class", "direction",
    "entry_price", "entry_low", "entry_high",
    "initial_stop_loss", "stop_loss_at_exit", "target1", "target2",
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
        # `initial_stop_loss` is the original structural stop at signal time;
        # `stop_loss_at_exit` is whatever was active when the trade closed
        # (the breakeven price, once TP1 has triggered) -- these differ for
        # any trade that reached PARTIAL_TP1, by design (Breakeven Adjustment).
        "initial_stop_loss": round(o.initial_stop, 6),
        "stop_loss_at_exit": round(o.stop, 6),
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
            f"{o.direction:<5} | Entry {o.entry_price:,.4f}  Stop {o.initial_stop:,.4f}  "
            f"TP1 {o.target1:,.4f}  TP2 {o.target2:,.4f}"
        )
        print(
            f"    Exit {exit_date_str} @ {o.exit_price:,.4f}  ->  {o.combined_outcome():<9}  "
            f"PnL {o.pnl_pct:+.2f}%  ({o.days_held}d held, score {o.score})"
        )
    print("-" * 64 + "\n")


def export_trades_csv(outcomes: list[TradeOutcome], path: Path | None = None) -> Path:
    """Write every simulated trade to runtime/backtest/trades_log_livne.csv
    (sorted chronologically) so results can be cross-referenced against
    historical charts (e.g. TradingView) or analyzed in a spreadsheet. The
    path comes from config so this output lands in the shared runtime/ tree
    rather than inside the source package, and so Steve reads exactly the
    file this writes."""
    if path is None:
        path = Path(BACKTEST_TRADES_CSV)
    path.parent.mkdir(parents=True, exist_ok=True)

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
    parser.add_argument("--max-tickers", type=int, default=None, help="Limit watchlist size per class (for faster test runs)")
    parser.add_argument("--verbose", action="store_true", help="Print an itemized trade-by-trade log to the terminal")
    parser.add_argument("--disable-dynamic-exit", action="store_true",
                         help="Disable the trend-reversal+volume early exit, using only the stop/target levels "
                              "(useful for A/B-comparing the two exit policies). Trades still have no fixed "
                              "holding-period cutoff either way.")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date)
    if start >= end:
        parser.error("--start-date must be before --end-date")

    outcomes = run_backtest(
        start, end, args.asset_class, args.max_tickers,
        enable_dynamic_exit=not args.disable_dynamic_exit,
    )

    if args.verbose:
        print_trade_log(outcomes)

    print_report(outcomes, start, end)

    csv_path = export_trades_csv(outcomes)
    print(f"[backtest] Trade log exported to {csv_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
