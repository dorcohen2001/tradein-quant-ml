"""Main CLI launcher for the Biton INTRADAY backtesting suite.

    python -m backtest_suite.run_backtest --start-date 2026-06-01 --end-date 2026-07-01
    python -m backtest_suite.run_backtest --start-date 2026-06-01 --end-date 2026-07-01 \
        --asset-class crypto --interval 15m --exec-interval 5m --verbose
    python -m backtest_suite.run_backtest --start-date 2025-01-01 --end-date 2026-01-01 --interval 1d

Walks the analysis timeframe bar by bar and, at each bar's CLOSE, feeds only
data available by then into the *exact same* production building blocks the
live pipeline uses -- `ta.ta_engine.analyze`, `screener.market_screener`'s
scoring, `macro.macro_analyzer`'s direction gate, and
`generator.trade_generator._build_trade_params` for entry/stop/target
construction. Nothing here re-implements that math. The only thing replicated
is the macro *regime computation* (`_stock_regime`/`_crypto_regime`), because
`macro_analyzer`'s public functions always fetch "now" and are not
parameterised by a historical instant; the thresholds still come from
config.py, so there is a single source of truth for the rules.

WHAT THE INTRADAY UPGRADE CHANGED
---------------------------------
Biton's RULES are untouched -- it remains the frozen legacy model. What
changed is that the analysis bar size is now a parameter rather than being
hard-wired to daily candles, and that stops/targets are graded against
intraday highs and lows with precise exit timestamps.

  --interval 1h   (default) intraday, a reasonable bar size for a swing-style
                  trend/Fib/pivot model
  --interval 1d   reproduces the previous daily behaviour
  --interval 15m  fastest sensible setting; note Biton's rules were designed
                  on daily structure and are not intraday-tuned

The liquidity floor and the relative-strength comparison still run on DAILY
bars regardless of `--interval`, because that is where they run live -- a
single 15m bar's dollar volume is not the thing `MIN_DOLLAR_VOLUME` was ever
calibrated against.

SCOPE BOUNDARIES
----------------
`earnings_hours_away` is passed as None deliberately: a point-in-time earnings
calendar is not available for free, and using "today's" next-earnings date
would leak the future into a historical decision. News/sentiment confluence
(`build_trade_setup`'s `confluence_check`) is likewise not simulated -- there
is no historical headline archive to replay -- so this exercises the TA,
scoring, macro-gate and risk half of the funnel, not the news half.

Stock intraday history is capped by yfinance (~60 days at 15m/5m, ~730 days at
1h). Longer stock windows are clamped, with a warning, and coarser bars
substituted. Crypto has no such limit.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

import pandas as pd

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from backtest_suite.historical_data import (
    MIN_ANALYSIS_BARS,
    AssetData,
    build_asset_data,
    build_benchmark,
    decision_time,
    forward_bars,
    signal_bars,
)
from backtest_suite.simulator import MAX_HOLD_HOURS, simulate_trade
from config import (
    BACKTEST_TRADES_CSV,
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
from core.backtesting import (
    FinerBarTieBreaker,
    IntradayDataFeed,
    TradeRecord,
    compute_metrics,
    export_trades_csv,
    format_summary,
    format_trade_log,
    interval_of,
    trades_to_dataframe,
)
from generator.trade_generator import _build_trade_params
from macro.macro_analyzer import MacroContext
from screener.market_screener import Candidate, _n_day_return, _score_candidate
from ta.ta_engine import add_moving_averages, analyze, determine_trend, volume_spike

# Minimum DAILY bars before the liquidity/relative-strength context is trusted.
MIN_DAILY_CONTEXT_BARS = 60


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


# ---------------------------------------------------------------------------
# Point-in-time macro regime (mirrors macro_analyzer.py's decision rules)
# ---------------------------------------------------------------------------
def _stock_regime(spy_slice: pd.DataFrame, qqq_slice: pd.DataFrame, vix_slice: pd.DataFrame) -> str:
    spy_trend = determine_trend(add_moving_averages(spy_slice)) if len(spy_slice) else "UNKNOWN"
    qqq_trend = determine_trend(add_moving_averages(qqq_slice)) if len(qqq_slice) else "UNKNOWN"
    vix_level = float(vix_slice["Close"].iloc[-1]) if len(vix_slice) else 0.0

    if vix_level >= VIX_HIGH_VOL:
        return "High Volatility"
    if spy_trend == "UPTREND" and qqq_trend == "UPTREND" and vix_level < VIX_ELEVATED:
        return "Bullish"
    if spy_trend == "DOWNTREND" and qqq_trend == "DOWNTREND":
        return "Bearish"
    return "Neutral"


def _crypto_regime(btc_slice: pd.DataFrame, eth_slice: pd.DataFrame) -> str:
    btc_trend = determine_trend(add_moving_averages(btc_slice)) if len(btc_slice) else "UNKNOWN"
    eth_trend = determine_trend(add_moving_averages(eth_slice)) if len(eth_slice) else "UNKNOWN"

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


class _Replay:
    """One asset class pass: benchmarks plus the daily-resolution caches that
    keep regime and relative-strength work from being redone on every
    intraday bar."""

    def __init__(self, feed: IntradayDataFeed, asset_class: str, start: dt.date, end: dt.date) -> None:
        self.asset_class = asset_class
        self._regime: dict[dt.date, str] = {}
        self._bench_ret: dict[dt.date, float] = {}

        if asset_class == "stocks":
            _log("[backtest] loading SPY/QQQ/^VIX benchmark history...")
            self.spy = build_benchmark(feed, STOCK_BENCHMARKS["default"], "stocks", start, end)
            self.qqq = build_benchmark(feed, "QQQ", "stocks", start, end)
            self.vix = build_benchmark(feed, "^VIX", "stocks", start, end)
            self.bench = self.spy
        else:
            _log("[backtest] loading BTC/ETH benchmark history...")
            self.btc = build_benchmark(feed, CRYPTO_BENCHMARK, "crypto", start, end)
            self.eth = build_benchmark(feed, "ETHUSDT", "crypto", start, end)
            self.bench = self.btc

    def allowed_at(self, at: pd.Timestamp) -> set[str]:
        """Directions the macro gate permits. Regime is a DAILY read, so it is
        computed once per calendar day and reused across that day's intraday
        bars -- recomputing 200-period EMAs on three benchmarks per bar would
        dominate the runtime and give the same answer."""
        day = at.date()
        regime = self._regime.get(day)
        if regime is None:
            if self.asset_class == "stocks":
                regime = _stock_regime(self.spy.as_of(at), self.qqq.as_of(at), self.vix.as_of(at))
            else:
                regime = _crypto_regime(self.btc.as_of(at), self.eth.as_of(at))
            self._regime[day] = regime
        return MacroContext(asset_class=self.asset_class, regime=regime, details={}, summary="").allowed_directions()

    def benchmark_return(self, at: pd.Timestamp) -> float:
        day = at.date()
        cached = self._bench_ret.get(day)
        if cached is not None:
            return cached
        value = _n_day_return(self.bench.as_of(at), RS_LOOKBACK_DAYS)
        self._bench_ret[day] = value
        return value


def _replay_asset(
    data: AssetData,
    replay: _Replay,
    start: dt.date,
    end: dt.date,
    notional: float,
    max_hold_hours: float,
    tie_breaker: FinerBarTieBreaker | None,
) -> list[TradeRecord]:
    """Walk one asset's analysis bars and simulate every signal it produces."""
    trades: list[TradeRecord] = []
    open_until: pd.Timestamp | None = None
    daily_cache: dict[dt.date, tuple[float, float] | None] = {}

    for bar_open in signal_bars(data.analysis, start, end):
        at = decision_time(bar_open, data.interval)

        if open_until is not None and at <= open_until:
            continue  # a trade on this ticker is still live; don't stack signals

        cut = data.analysis_pit.cutoff_position(at)
        if cut < MIN_ANALYSIS_BARS:
            continue  # not enough warmup history yet at this point in time

        # --- daily-resolution context: liquidity floor + relative strength ---
        day = at.date()
        if day not in daily_cache:
            daily_slice = data.daily.as_of(at)
            if len(daily_slice) < MIN_DAILY_CONTEXT_BARS:
                daily_cache[day] = None
            else:
                last_price = float(daily_slice["Close"].iloc[-1])
                last_volume = float(daily_slice["Volume"].iloc[-1])
                daily_cache[day] = (
                    last_price * last_volume,
                    _n_day_return(daily_slice, RS_LOOKBACK_DAYS),
                )
        context = daily_cache[day]
        if context is None:
            continue
        dollar_volume, asset_ret = context
        if dollar_volume < MIN_DOLLAR_VOLUME:
            continue

        sliced = data.analysis.iloc[:cut]
        ta_result = analyze(data.ticker, sliced)
        if not (ta_result.ma_hits or ta_result.in_golden_zone):
            continue

        direction = ta_result.direction_bias
        if direction == "NEUTRAL" or direction not in replay.allowed_at(at):
            continue

        _spike, ratio = volume_spike(sliced)
        rs_pct = (asset_ret - replay.benchmark_return(at)) * 100

        # earnings_hours_away is intentionally None: a real point-in-time
        # earnings calendar isn't available for free, and using "today's"
        # next-earnings date here would leak future information.
        score, breakdown = _score_candidate(ta_result, ratio, dollar_volume, rs_pct, None)
        candidate = Candidate(
            ticker=data.ticker, asset_class=data.asset_class, score=score, ta=ta_result,
            dollar_volume=dollar_volume, vol_ratio=ratio, rs_pct=rs_pct,
            earnings_hours_away=None, breakdown=breakdown,
        )

        params = _build_trade_params(candidate, direction)
        if params is None:
            continue

        trade = simulate_trade(
            forward_bars(data.execution, at), data.ticker, data.asset_class,
            direction, at, params["entry_low"], params["entry_high"], params["stop"],
            params["target1"], params["target2"], score,
            interval=data.exec_interval, notional=notional,
            max_hold_hours=max_hold_hours, tie_breaker=tie_breaker,
        )
        trades.append(trade)
        open_until = trade.exit_time

    return trades


def run_backtest(
    start: dt.date,
    end: dt.date,
    asset_class: str = "both",
    interval: str = "1h",
    exec_interval: str | None = None,
    max_tickers: int | None = None,
    max_hold_hours: float = MAX_HOLD_HOURS,
    notional: float = 1000.0,
    cache_dir: str | None = None,
    cache_format: str = "auto",
    offline: bool = False,
    tie_break_interval: str | None = None,
    tickers: list[str] | None = None,
) -> list[TradeRecord]:
    feed = IntradayDataFeed(cache_dir=cache_dir, cache_format=cache_format, offline=offline)
    exec_interval = exec_interval or interval

    tie_breaker = None
    if tie_break_interval:
        tie_breaker = FinerBarTieBreaker(
            loader=lambda sym, cls, s, e: feed.get_candles(sym, cls, tie_break_interval, s, e),
            interval_delta=interval_of(exec_interval).delta,
        )

    outcomes: list[TradeRecord] = []
    classes = ["stocks", "crypto"] if asset_class == "both" else [asset_class]

    for cls in classes:
        if tickers:
            watchlist = list(tickers)
        else:
            watchlist = WATCHLIST_STOCKS if cls == "stocks" else WATCHLIST_CRYPTO
        if max_tickers:
            watchlist = watchlist[:max_tickers]

        _log(f"[backtest] === {cls}: {len(watchlist)} tickers, {interval} analysis, "
             f"{exec_interval} execution, {start} -> {end} ===")
        replay = _Replay(feed, cls, start, end)

        started = time.time()
        class_trades = 0
        for n, ticker in enumerate(watchlist, 1):
            data = build_asset_data(feed, ticker, cls, start, end, interval, exec_interval)
            if data is None or not data.usable:
                _log(f"[backtest] ({n}/{len(watchlist)}) {ticker}: no usable history, skipped")
                continue
            trades = _replay_asset(data, replay, start, end, notional, max_hold_hours, tie_breaker)
            outcomes.extend(trades)
            class_trades += len(trades)
            _log(f"[backtest] ({n}/{len(watchlist)}) {ticker}: {len(data.analysis)} {interval} bars, "
                 f"{len(trades)} trade(s)")

        _log(f"[backtest] {cls}: {class_trades} trade(s) in {time.time() - started:.0f}s")

    _log(f"[backtest] {feed.download_count} provider download(s) this run "
         f"(everything else served from {feed.cache.root})")
    if tie_breaker is not None:
        _log(f"[backtest] intrabar tie-breaker: {tie_breaker.resolved} resolved from {tie_break_interval} "
             f"candles, {tie_breaker.unresolved} fell back to stop-first")
    return outcomes


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Intraday historical backtest of the Biton pipeline's signal logic",
    )
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--asset-class", choices=["stocks", "crypto", "both"], default="both")
    parser.add_argument("--interval", default="1h",
                        help="analysis timeframe the strategy runs on (default 1h; use 1d to reproduce "
                             "the previous daily-bar behaviour)")
    parser.add_argument("--exec-interval", default=None,
                        help="timeframe open positions are managed on; finer than --interval gives more "
                             "precise exit timestamps and fewer ambiguous same-bar stop/target collisions "
                             "(default: same as --interval)")
    parser.add_argument("--tickers", default=None,
                        help="comma-separated symbols to test instead of the config watchlist")
    parser.add_argument("--max-tickers", type=int, default=None,
                        help="limit watchlist size per class (for faster test runs)")
    parser.add_argument("--max-hold-hours", type=float, default=MAX_HOLD_HOURS,
                        help=f"max holding period before a trade is closed at market "
                             f"(default {MAX_HOLD_HOURS}h = the old 10-day cutoff); 0 disables it")
    parser.add_argument("--tie-break-interval", default=None,
                        help="finer interval (e.g. 1m) used to decide which of a stop and a target that "
                             "both sit inside one execution bar triggered first (default: assume the stop)")
    parser.add_argument("--capital", type=float, default=10_000.0,
                        help="starting capital used for ROI and drawdown (default 10000)")
    parser.add_argument("--notional", type=float, default=1_000.0,
                        help="fixed position size per trade in currency; no compounding or leverage "
                             "is modelled (default 1000)")
    parser.add_argument("--data-dir", default=None, help="candle cache directory (default <repo>/data)")
    parser.add_argument("--cache-format", choices=["auto", "parquet", "csv"], default="auto")
    parser.add_argument("--offline", action="store_true",
                        help="never hit the network; run purely from cached candles")
    parser.add_argument("--csv-out", default=None, help=f"trade-log destination (default {BACKTEST_TRADES_CSV})")
    parser.add_argument("--verbose", action="store_true",
                        help="print the full trade-by-trade log to the terminal")
    parser.add_argument("--max-log-rows", type=int, default=60,
                        help="cap terminal trade-log rows without --verbose (default 60)")
    args = parser.parse_args()

    start = dt.date.fromisoformat(args.start_date)
    end = dt.date.fromisoformat(args.end_date)
    if start >= end:
        parser.error("--start-date must be before --end-date")

    outcomes = run_backtest(
        start, end,
        asset_class=args.asset_class,
        interval=args.interval,
        exec_interval=args.exec_interval,
        max_tickers=args.max_tickers,
        max_hold_hours=args.max_hold_hours,
        notional=args.notional,
        cache_dir=args.data_dir,
        cache_format=args.cache_format,
        offline=args.offline,
        tie_break_interval=args.tie_break_interval,
        tickers=[t.strip().upper() for t in args.tickers.split(",")] if args.tickers else None,
    )

    trades = trades_to_dataframe(outcomes)

    print("\n--- Trade Log (all timestamps UTC) " + "-" * 39)
    print(format_trade_log(trades, max_rows=None if args.verbose else args.max_log_rows))

    metrics = compute_metrics(trades, initial_capital=args.capital)
    exec_tf = args.exec_interval or args.interval
    print(format_summary(
        metrics,
        title=f"BITON INTRADAY BACKTEST  |  {start} -> {end}  |  "
              f"{args.interval} analysis / {exec_tf} execution",
    ))

    csv_path = export_trades_csv(trades, args.csv_out or BACKTEST_TRADES_CSV)
    _log(f"[backtest] trade log exported to {csv_path}")


if __name__ == "__main__":
    main()
