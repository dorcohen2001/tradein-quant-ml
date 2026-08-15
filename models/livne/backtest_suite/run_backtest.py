"""Main CLI launcher for the Livne INTRADAY backtesting suite.

    python -m backtest_suite.run_backtest --start-date 2026-06-15 --end-date 2026-07-15
    python -m backtest_suite.run_backtest --start-date 2026-01-01 --end-date 2026-07-01 --asset-class crypto
    python -m backtest_suite.run_backtest --start-date 2026-06-15 --end-date 2026-07-15 \
        --interval 15m --exec-interval 5m --require-entry-touch --verbose

WHAT THIS NOW REPLAYS
---------------------
The previous version walked DAILY bars and approximated the live 15m trigger
with a daily-bar proxy, so its win rate was explicitly not an estimate of live
intraday performance. That gap is closed. This walks the trigger timeframe bar
by bar and drives the REAL production functions -- `ta.mtfa.map_zones_top_down`
for the top-down zone map, `ta.candles.detect_reversal_formation` for the
trigger formation, `ta.candles.directional_volume_confirmation` for the
mandatory volume proof, `ta.mtfa._ma_alignment_vote` for the 200 EMA vote, and
`risk.risk_manager` for stops/targets and the R:R gate -- against point-in-time
intraday frames. Nothing in this file re-implements a trading rule; it only
replicates the macro *regime computation* (`_stock_regime`/`_crypto_regime`),
because `macro.macro_analyzer`'s public functions always fetch "now" and are
not parameterised by a historical instant.

Gate order differs from `analyze_mtfa` and this is deliberate. Live, the zone
map runs first; here the cheap trigger-frame gates (reversal formation,
directional volume, Fib confluence) run first and the expensive zone map only
on bars that already passed them. Every gate is an AND, so the accept/reject
decision is identical -- it just avoids building a full weekly/daily/12h/4h/3h
zone map on the ~99% of bars that could never have produced a trade anyway.

SCOPE BOUNDARIES (unchanged from the daily version, and still real)
-------------------------------------------------------------------
  1. The liquidity-heatmap bias is live-only: historical order-book snapshots
     do not exist to backtest against. `screener._score_candidate` -- the same
     function the live pipeline scores with -- runs unmodified against a
     neutral placeholder `LiquidityContext`.
  2. USDT.D / TOTAL-market-cap trend gating is live-only; no free historical
     dominance or market-cap time series exists.
  3. `earnings_hours_away` is passed as None on purpose. A real point-in-time
     earnings calendar is not available for free, and using "today's" next
     earnings date would leak the future into a historical decision.
  4. Stock intraday history is capped by yfinance (~60 days at 15m/5m). Longer
     stock windows get clamped, with a warning, and coarser bars substituted.
     Crypto has no such limit.

Trades have no fixed holding-period cutoff -- see `simulator.py`. One still
unresolved when the data runs out is reported OPEN and excluded from every
summary figure.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time

import pandas as pd

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from backtest_suite.historical_data import (
    AssetData,
    build_asset_data,
    build_benchmark,
    decision_time,
    enough_history,
    forward_bars,
    signal_bars,
)
from backtest_suite.simulator import simulate_trade
from config import (
    BACKTEST_TRADES_CSV,
    CRYPTO_BENCHMARK,
    DOLLAR_VOLUME_SMA_LOOKBACK,
    LTF_ALIGNMENT_TFS,
    LTF_CONFIRM_TFS,
    LTF_MA_MIN_ALIGNED,
    LTF_TRIGGER_TF,
    MIN_CRYPTO_DOLLAR_VOLUME_SMA20,
    MIN_DAILY_HISTORY_BARS,
    MIN_STOCK_DOLLAR_VOLUME_SMA20,
    PENDING_EXPIRY_HOURS,
    PRIMARY_MA_PERIOD,
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
from liquidity.orderbook import LiquidityContext
from risk.risk_manager import compute_stop, compute_targets, position_size_estimate, validate_min_rr
from screener.market_screener import Candidate, _dollar_volume_sma, _n_day_return, _score_candidate
from strategy.strategy_engine import StrategyDecision, entry_zone
from ta.candles import detect_reversal_formation, directional_volume_confirmation
from ta.divergence import DivergenceResult, rsi_divergence
from ta.mtfa import (
    MTFASetup,
    _confirm_agreement,
    _ma_alignment_vote,
    _zone_frame_ok,
    fib_reaction,
    htf_volume_ratio,
    map_zones_top_down,
    swing_excluding_recent,
)
from ta.ta_engine import add_moving_averages, atr, determine_trend, fibonacci_levels

# Trailing trigger bars handed to the CHEAP gates. Every one of them looks at
# most ~63 bars back (the Fib swing lookback of 60 plus its 3-bar exclusion),
# so 400 is comfortable slack while keeping the per-bar slice small on a
# 100k-row intraday frame. The EXPENSIVE stage always gets the full slice,
# because a 200-period EMA seeded 400 bars ago is measurably off.
CHEAP_GATE_TAIL_BARS = 400

_NEUTRAL_LIQUIDITY = LiquidityContext(
    source="unavailable_backtest", above_notional=0.0, below_notional=0.0,
    favored_direction=None, top_clusters=[],
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


def _within(price: float, a: float, b: float) -> bool:
    lo, hi = sorted((a, b))
    return lo <= price <= hi


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


def _allowed_from_regime(regime: str) -> set[str]:
    """Mirrors `macro.macro_analyzer`'s Bullish->LONG / Bearish->SHORT /
    else-both mapping, without the live-only USDT.D/TOTAL refinement."""
    if regime == "Bullish":
        return {"LONG"}
    if regime == "Bearish":
        return {"SHORT"}
    return {"LONG", "SHORT"}


# ---------------------------------------------------------------------------
# Stage 1 -- cheap, trigger-frame-only gates
# ---------------------------------------------------------------------------
def _trigger_gates(trigger_tail: pd.DataFrame, direction: str) -> dict | None:
    """The three hard gates that only need the trigger timeframe:
    an explicit reversal FORMATION, MANDATORY directional volume confirmation
    on that formation candle, and Fib containment or a 0.5/0.618 reaction.

    Identical checks, and identical thresholds, to `ta.mtfa.analyze_mtfa`
    steps 2/3/5 -- just evaluated before the expensive zone map instead of
    after it. Returns the values the setup needs, or None on any rejection."""
    if len(trigger_tail) < 30:
        return None

    found, pattern, conviction, bar_offset = detect_reversal_formation(trigger_tail, direction)
    if not found:
        return None

    formation_df = trigger_tail.iloc[: len(trigger_tail) - bar_offset]
    volume_ok, volume_detail = directional_volume_confirmation(formation_df, direction)
    if not volume_ok:
        return None

    price = float(trigger_tail["Close"].iloc[-1])
    itf_hi, itf_lo = swing_excluding_recent(trigger_tail)
    itf_fib = fibonacci_levels(itf_hi, itf_lo)
    contained = _within(price, itf_fib["0.5"], itf_fib["0.786"])
    reacted = fib_reaction(trigger_tail, itf_fib, direction)
    if not (contained or reacted):
        return None

    return {
        "pattern": pattern, "conviction": conviction, "volume_detail": volume_detail,
        "fib": itf_fib, "swing_high": itf_hi, "swing_low": itf_lo,
        "contained": contained, "price": price,
    }


# ---------------------------------------------------------------------------
# Stage 2 -- full MTFA against point-in-time frames
# ---------------------------------------------------------------------------
def evaluate_intraday_setup(
    ticker: str, asset_class: str, frames: dict[str, pd.DataFrame],
    trigger_df: pd.DataFrame, trigger_tf: str, candidates: dict[str, dict],
) -> MTFASetup | None:
    """The historical equivalent of `ta.mtfa.analyze_mtfa`, reading pre-sliced
    frames instead of calling the live fetchers.

    `candidates` maps each direction that already cleared `_trigger_gates` to
    that gate's output. Both zone maps are still computed regardless, because
    live REJECTS a bar where price sits at both a support and a resistance
    read with no daily trend to break the tie -- resolving only the
    cheap-passing direction would quietly accept setups live would refuse.
    """
    if not candidates:
        return None

    price = next(iter(candidates.values()))["price"]

    daily = frames.get("daily")
    weekly = frames.get("weekly")
    htf_trend = determine_trend(add_moving_averages(daily)) if _zone_frame_ok(daily) else "UNKNOWN"
    higher_trend = determine_trend(add_moving_averages(weekly)) if _zone_frame_ok(weekly) else "UNKNOWN"
    zone_volume_ratio = htf_volume_ratio(daily) if _zone_frame_ok(daily) else 0.0
    divergence = rsi_divergence(daily) if _zone_frame_ok(daily) else DivergenceResult()

    long_map = map_zones_top_down("LONG", price, frames)
    short_map = map_zones_top_down("SHORT", price, frames)
    if long_map and short_map:
        if htf_trend == "UPTREND":
            direction, zone_map = "LONG", long_map
        elif htf_trend == "DOWNTREND":
            direction, zone_map = "SHORT", short_map
        else:
            return None  # genuinely ambiguous location -- not worth trading intraday
    elif long_map:
        direction, zone_map = "LONG", long_map
    elif short_map:
        direction, zone_map = "SHORT", short_map
    else:
        return None

    gate = candidates.get(direction)
    if gate is None:
        return None  # the zone map resolved to a direction the trigger did not confirm

    ma_aligned_count = _ma_alignment_vote(direction, frames)
    if ma_aligned_count < LTF_MA_MIN_ALIGNED:
        return None

    trigger_ma = add_moving_averages(trigger_df, periods=(PRIMARY_MA_PERIOD,))
    ema200_val = trigger_ma[f"EMA{PRIMARY_MA_PERIOD}"].iloc[-1]
    itf_ema200 = float(ema200_val) if not pd.isna(ema200_val) else None
    atr_val = atr(trigger_df).iloc[-1]
    itf_atr = float(atr_val) if not pd.isna(atr_val) else None

    confirm_agreement = _confirm_agreement(direction, frames)
    discovery_tfs = sorted({d["tf"] for d in zone_map["discovered"]})
    refined_tfs = sorted({r["tf"] for r in zone_map["refined"]})
    zone = zone_map["zone"]
    volume_detail = gate["volume_detail"]

    thesis_bits = [
        f"[BACKTEST {trigger_tf}] Daily context {htf_trend} (weekly {higher_trend}); "
        f"{zone_map['zone_type']} zone {zone[0]:.4g}-{zone[1]:.4g} discovered on "
        f"{'/'.join(discovery_tfs)}" + (f", refined on {'/'.join(refined_tfs)}" if refined_tfs else "")
    ]
    if zone_map["ema200_anchor"] is not None:
        thesis_bits.append(
            f"price reacting at the {zone_map['ema200_anchor_tf']} {PRIMARY_MA_PERIOD} EMA "
            f"({zone_map['ema200_anchor']:.4g})"
        )
    vol_word = "buyer volume surge" if direction == "LONG" else "seller volume expansion"
    thesis_bits.append(f"{vol_word} {volume_detail['ratio']}x avg on the {trigger_tf} {gate['pattern']}")
    fib_note = "inside Fib golden zone" if gate["contained"] else "reacted off 0.5/0.618 Fib level"
    thesis_bits.append(
        f"{ma_aligned_count}/{len(LTF_ALIGNMENT_TFS)} execution timeframes {PRIMARY_MA_PERIOD}MA-aligned, "
        f"{confirm_agreement}/{len(LTF_CONFIRM_TFS)} confirmation frames agree, {fib_note}"
    )

    return MTFASetup(
        ticker=ticker, asset_class=asset_class, price=price, direction_bias=direction,
        htf_trend=htf_trend, htf_zone_type=zone_map["zone_type"], htf_zone=zone, divergence=divergence,
        order_blocks=zone_map["order_blocks"], nearest_support=zone_map["nearest_support"],
        nearest_resistance=zone_map["nearest_resistance"],
        swing_high=zone_map["swing_high"], swing_low=zone_map["swing_low"],
        fib_levels=zone_map["fib"], monthly_trend=higher_trend,
        itf_trigger_tf=trigger_tf, itf_pattern=gate["pattern"],
        itf_volume_ratio=volume_detail["ratio"], itf_ma200_aligned_count=ma_aligned_count,
        itf_fib_levels=gate["fib"], itf_ema200=itf_ema200, itf_atr=itf_atr,
        itf_swing_high=gate["swing_high"], itf_swing_low=gate["swing_low"],
        thesis="; ".join(thesis_bits), htf_volume_ratio=zone_volume_ratio,
        zone_tf=zone_map["zone_tf"], zone_discovery_tfs=discovery_tfs, zone_refined_tfs=refined_tfs,
        reversal_conviction=gate["conviction"], ema200_anchor=zone_map["ema200_anchor"],
        ema200_anchor_tf=zone_map["ema200_anchor_tf"], volume_detail=volume_detail,
        confirm_agreement=confirm_agreement,
    )


def build_backtest_decision(setup: MTFASetup) -> StrategyDecision | None:
    """`strategy_engine.evaluate_setup`'s risk half, with the live-only
    liquidity fetch replaced by a neutral placeholder."""
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
class _Replay:
    """One asset class pass. Holds the per-class benchmarks and the caches that
    keep daily-resolution work (regime, liquidity, relative strength) from
    being recomputed on every intraday bar of every ticker."""

    def __init__(self, feed: IntradayDataFeed, asset_class: str, start: dt.date, end: dt.date) -> None:
        self.feed = feed
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

    def regime_at(self, at: pd.Timestamp) -> str:
        """Macro regime is a DAILY read, so it is computed once per calendar
        day and reused across that day's intraday bars -- recomputing 200-period
        EMAs on three benchmarks for every 15m bar would dominate the runtime
        and produce the same answer."""
        day = at.date()
        cached = self._regime.get(day)
        if cached is not None:
            return cached
        if self.asset_class == "stocks":
            regime = _stock_regime(self.spy.as_of(at), self.qqq.as_of(at), self.vix.as_of(at))
        else:
            regime = _crypto_regime(self.btc.as_of(at), self.eth.as_of(at))
        self._regime[day] = regime
        return regime

    def benchmark_return(self, at: pd.Timestamp) -> float:
        day = at.date()
        cached = self._bench_ret.get(day)
        if cached is not None:
            return cached
        value = _n_day_return(self.bench.as_of(at), RS_LOOKBACK_DAYS)
        self._bench_ret[day] = value
        return value


def _liquidity_floor(asset_class: str) -> float:
    return MIN_CRYPTO_DOLLAR_VOLUME_SMA20 if asset_class == "crypto" else MIN_STOCK_DOLLAR_VOLUME_SMA20


def _replay_asset(
    data: AssetData,
    replay: _Replay,
    start: dt.date,
    end: dt.date,
    notional: float,
    enable_dynamic_exit: bool,
    tie_breaker: FinerBarTieBreaker | None,
    require_entry_touch: bool,
) -> tuple[list[TradeRecord], int]:
    """Walk one asset's trigger bars and simulate every signal it produces.

    Returns (trades, unfilled) -- `unfilled` counts setups that were valid but
    whose entry zone price never returned to, which only happens under
    `--require-entry-touch`.
    """
    trades: list[TradeRecord] = []
    unfilled = 0
    trigger_pit = data.frames.get(data.trigger_interval)
    if trigger_pit is None:
        return trades, unfilled

    floor = _liquidity_floor(data.asset_class)
    entry_expiry = pd.Timedelta(hours=PENDING_EXPIRY_HOURS) if require_entry_touch else None
    open_until: pd.Timestamp | None = None
    daily_cache: dict[dt.date, tuple[float, float] | None] = {}

    for bar_open in signal_bars(data.trigger, start, end):
        at = decision_time(bar_open, data.trigger_interval)

        # A position is still live on this ticker -- don't stack signals.
        if open_until is not None and at <= open_until:
            continue
        if not enough_history(data, at):
            continue

        # --- daily-resolution context (liquidity floor + relative strength) ---
        # Both are daily-bar measures in the live screener, so they are computed
        # once per calendar day and reused across that day's intraday bars. The
        # trade-off is that the current session's still-growing volume is read
        # at the first bar of the day rather than continuously -- a liquidity
        # FLOOR does not need per-bar resolution, and recomputing a 7-day SMA
        # on every 15m bar would dominate the runtime.
        day = at.date()
        if day not in daily_cache:
            daily_slice = data.daily.as_of(at)
            if len(daily_slice) < MIN_DAILY_HISTORY_BARS:
                daily_cache[day] = None
            else:
                daily_cache[day] = (
                    _dollar_volume_sma(daily_slice, lookback=DOLLAR_VOLUME_SMA_LOOKBACK),
                    _n_day_return(daily_slice, RS_LOOKBACK_DAYS),
                )
        context = daily_cache[day]
        if context is None:
            continue
        dollar_volume, asset_ret = context
        if dollar_volume < floor:
            continue

        allowed = _allowed_from_regime(replay.regime_at(at))

        # --- Stage 1: cheap trigger-frame gates ---
        cut = trigger_pit.cutoff_position(at)
        tail = data.trigger.iloc[max(0, cut - CHEAP_GATE_TAIL_BARS):cut]
        candidates = {}
        for direction in ("LONG", "SHORT"):
            if direction not in allowed:
                continue
            gate = _trigger_gates(tail, direction)
            if gate is not None:
                candidates[direction] = gate
        if not candidates:
            continue

        # --- Stage 2: full point-in-time MTFA ---
        frames = data.frames.frames_at(at)
        setup = evaluate_intraday_setup(
            data.ticker, data.asset_class, frames,
            data.trigger.iloc[:cut], data.trigger_interval, candidates,
        )
        if setup is None:
            continue

        decision = build_backtest_decision(setup)
        if decision is None:
            continue

        rs_pct = (asset_ret - replay.benchmark_return(at)) * 100
        score, breakdown = _score_candidate(decision, dollar_volume, rs_pct, None)
        candidate = Candidate(
            ticker=data.ticker, asset_class=data.asset_class, score=score, decision=decision,
            dollar_volume=dollar_volume, rs_pct=rs_pct, earnings_hours_away=None, breakdown=breakdown,
        )

        trade = simulate_trade(
            forward_bars(data.execution, at), data.ticker, data.asset_class,
            decision.direction, at, decision.entry_low, decision.entry_high,
            decision.stop, decision.target1, decision.target2, candidate.score,
            interval=data.exec_interval, notional=notional,
            reversal_df=data.trigger, reversal_interval=data.trigger_interval,
            enable_dynamic_exit=enable_dynamic_exit, tie_breaker=tie_breaker,
            require_entry_touch=require_entry_touch, entry_expiry=entry_expiry,
        )
        if trade is None:
            unfilled += 1
            continue

        trades.append(trade)
        open_until = trade.exit_time

    return trades, unfilled


def run_backtest(
    start: dt.date,
    end: dt.date,
    asset_class: str = "both",
    interval: str = LTF_TRIGGER_TF,
    exec_interval: str | None = None,
    max_tickers: int | None = None,
    enable_dynamic_exit: bool = True,
    require_entry_touch: bool = False,
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

        _log(f"[backtest] === {cls}: {len(watchlist)} tickers, {interval} signals, "
             f"{exec_interval} execution, {start} -> {end} ===")
        replay = _Replay(feed, cls, start, end)

        started = time.time()
        class_trades = 0
        class_unfilled = 0
        for n, ticker in enumerate(watchlist, 1):
            data = build_asset_data(feed, ticker, cls, start, end, interval, exec_interval)
            if data is None or not data.usable:
                _log(f"[backtest] ({n}/{len(watchlist)}) {ticker}: no usable intraday history, skipped")
                continue
            trades, unfilled = _replay_asset(
                data, replay, start, end, notional, enable_dynamic_exit, tie_breaker, require_entry_touch,
            )
            outcomes.extend(trades)
            class_trades += len(trades)
            class_unfilled += unfilled
            _log(f"[backtest] ({n}/{len(watchlist)}) {ticker}: {len(data.trigger)} {interval} bars, "
                 f"{len(trades)} trade(s)" + (f", {unfilled} unfilled" if unfilled else ""))

        elapsed = time.time() - started
        _log(f"[backtest] {cls}: {class_trades} trade(s)"
             + (f", {class_unfilled} setup(s) never filled" if class_unfilled else "")
             + f" in {elapsed:.0f}s")

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
        description="Intraday historical backtest of the Livne pipeline's signal logic",
    )
    parser.add_argument("--start-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end-date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--asset-class", choices=["stocks", "crypto", "both"], default="both")
    parser.add_argument("--interval", default=LTF_TRIGGER_TF,
                        help=f"signal/trigger timeframe (default {LTF_TRIGGER_TF}, matching the live model)")
    parser.add_argument("--exec-interval", default=None,
                        help="timeframe open positions are managed on; finer than --interval gives more "
                             "precise exit timestamps and fewer ambiguous same-bar stop/target collisions "
                             "(default: same as --interval)")
    parser.add_argument("--tickers", default=None,
                        help="comma-separated symbols to test instead of the config watchlist")
    parser.add_argument("--max-tickers", type=int, default=None,
                        help="limit watchlist size per class (for faster test runs)")
    parser.add_argument("--require-entry-touch", action="store_true",
                        help="only fill a trade if price actually trades into the entry zone within "
                             f"{PENDING_EXPIRY_HOURS}h, as the live paper tracker requires (default: assume "
                             "a fill at the entry-zone midpoint on the trigger bar's close)")
    parser.add_argument("--disable-dynamic-exit", action="store_true",
                        help="disable the trend-reversal+volume early exit, using only stop/target levels")
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
        enable_dynamic_exit=not args.disable_dynamic_exit,
        require_entry_touch=args.require_entry_touch,
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
        title=f"LIVNE INTRADAY BACKTEST  |  {start} -> {end}  |  "
              f"{args.interval} signals / {exec_tf} execution",
    ))

    csv_path = export_trades_csv(trades, args.csv_out or BACKTEST_TRADES_CSV)
    _log(f"[backtest] trade log exported to {csv_path}")


if __name__ == "__main__":
    main()
