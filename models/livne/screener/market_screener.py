"""Market Screener & Asset Selection Engine.

Cheap liquidity/market-cap/earnings-blackout filters run first (a single 1D
fetch each), then `strategy.strategy_engine.evaluate_setup` runs the full
MTFA + liquidity + correlation-gate + RiskManager chain -- that's the
expensive part (HTF weekly/monthly + ITF 12H/8H/4H fetches per ticker), so it
only runs on names that already cleared the cheap gates. Passing setups are
assigned a weighted confluence score (0-100) and the top-scoring candidates
are selected for downstream alerting.

There is no static minimum-price filter -- liquidity (a 20-day SMA of dollar
volume) and market cap are what actually gate out unsuitable names.

Per-ticker work is I/O-bound (HTTP fetches), so the watchlist is scanned with
a thread pool; results are re-sorted by score at the end so output ordering
is deterministic regardless of completion order.
"""
from __future__ import annotations

import datetime as dt
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd

from config import (
    CRYPTO_BENCHMARK,
    DOLLAR_VOLUME_SMA_LOOKBACK,
    EARNINGS_BLACKOUT_HOURS,
    EMA200_ANCHOR_SCORE_BONUS,
    HTF_VOLUME_CONFLUENCE_MULT,
    LTF_ALIGNMENT_TFS,
    LTF_VOLUME_SURGE_MULT,
    MIN_CRYPTO_DOLLAR_VOLUME_SMA20,
    MIN_DAILY_HISTORY_BARS,
    MIN_STOCK_DOLLAR_VOLUME_SMA20,
    MIN_STOCK_MARKET_CAP,
    RS_LOOKBACK_DAYS,
    SCORE_WEIGHTS,
    STOCK_BENCHMARKS,
    TOP_N_ASSETS,
    WATCHLIST_CRYPTO,
    WATCHLIST_STOCKS,
)
from macro.macro_analyzer import MacroContext
from strategy.strategy_engine import StrategyDecision, evaluate_setup
from utils.data_fetch import (
    get_binance_klines,
    get_next_earnings_date,
    get_stock_history,
    get_stock_market_cap,
)

SCAN_WORKERS = 8  # concurrent fetch/analyze workers per watchlist scan


def _dollar_volume_sma(df: pd.DataFrame, lookback: int = DOLLAR_VOLUME_SMA_LOOKBACK) -> float:
    """Rolling SMA of Close * Volume -- a dynamic liquidity metric that a
    single noisy bar can't flip in or out, unlike a last-bar dollar-volume
    check. INTRADAY: the window is now 7 trading days rather than 20, so a
    name that has just become active is recognised as tradeable this week
    instead of being averaged back down by three quiet weeks."""
    dollar_volume = df["Close"] * df["Volume"]
    return float(dollar_volume.tail(lookback).mean())


def _monthly_bias_score(direction: str, monthly_trend: str) -> float:
    """Monthly HTF structure is macro context/bias for scoring, not a hard
    rejection gate (see `ta.mtfa.htf_pass`) -- a monthly uptrend doesn't
    block a legitimate weekly/daily SHORT pullback, it just scores lower on
    macro alignment than a setup the monthly trend actually agrees with."""
    agreeing = "UPTREND" if direction == "LONG" else "DOWNTREND"
    conflicting = "DOWNTREND" if direction == "LONG" else "UPTREND"
    if monthly_trend == agreeing:
        return 1.0
    if monthly_trend == conflicting:
        return 0.0
    return 0.5  # MIXED / UNKNOWN -- neutral, not enough monthly signal either way


@dataclass
class Candidate:
    ticker: str
    asset_class: str
    score: float
    decision: StrategyDecision
    dollar_volume: float
    rs_pct: float
    earnings_hours_away: float | None
    breakdown: dict = field(default_factory=dict)


def _n_day_return(df: pd.DataFrame, n: int) -> float:
    if len(df) <= n:
        return 0.0
    return float(df["Close"].iloc[-1] / df["Close"].iloc[-1 - n] - 1)


def _score_candidate(decision: StrategyDecision, dollar_volume: float, rs_pct: float,
                      earnings_hours_away: float | None) -> tuple[float, dict]:
    w = SCORE_WEIGHTS
    breakdown = {}
    setup = decision.setup

    # --- TA confluence (0-1) ---
    # INTRADAY reweighting. Reversal-formation CERTAINTY is now the single
    # largest component: on a 15m chart the quality of the reversal candle is
    # what separates a real turn from noise, far more than it was on a weekly
    # swing setup. The EMA200 anchor and zone refinement are the other two
    # intraday-specific additions.
    ta_score = 0.10  # a decision only exists if a zone was already matched
    ta_score += setup.reversal_conviction * 0.30          # explicit reversal formation quality
    if setup.ema200_anchor is not None:
        ta_score += EMA200_ANCHOR_SCORE_BONUS             # price pressed against the 200 EMA
    if setup.zone_refined_tfs:
        ta_score += 0.10                                  # zone survived precision refinement on 4h/3h/1h
    if (setup.direction_bias == "LONG" and setup.divergence.bullish) or \
       (setup.direction_bias == "SHORT" and setup.divergence.bearish):
        ta_score += 0.10
    ta_score += (setup.itf_ma200_aligned_count / len(LTF_ALIGNMENT_TFS)) * 0.15
    ta_score += _monthly_bias_score(decision.direction, setup.monthly_trend) * 0.10
    if setup.htf_volume_ratio >= HTF_VOLUME_CONFLUENCE_MULT:
        ta_score += 0.05
    ta_score = min(ta_score, 1.0)
    breakdown["ta_confluence"] = round(ta_score * w["ta_confluence"], 1)

    # --- Volume / liquidity (0-1): SMA20 dollar-volume gate + ITF volume spike + liquidity-cluster confluence ---
    liquidity_floor = MIN_CRYPTO_DOLLAR_VOLUME_SMA20 if decision.asset_class == "crypto" else MIN_STOCK_DOLLAR_VOLUME_SMA20
    liquidity_gate = 1.0 if dollar_volume >= liquidity_floor else 0.0
    itf_vol_component = min(setup.itf_volume_ratio / LTF_VOLUME_SURGE_MULT, 1.0)
    if setup.volume_detail.get("exhaustion"):
        itf_vol_component = min(itf_vol_component + 0.25, 1.0)  # exhaustion into the extreme adds conviction
    if decision.liquidity.favored_direction == decision.direction:
        liquidity_confluence = 1.0
    elif decision.liquidity.favored_direction is None:
        liquidity_confluence = 0.5
    else:
        liquidity_confluence = 0.0
    vol_score = (liquidity_gate + itf_vol_component + liquidity_confluence) / 3
    breakdown["volume"] = round(vol_score * w["volume"], 1)

    # --- Relative strength (0-1): scaled, +/-5% outperformance maps to full score ---
    rs_score = max(0.0, min(abs(rs_pct) / 5.0, 1.0))
    breakdown["relative_strength"] = round(rs_score * w["relative_strength"], 1)

    # --- Catalyst safety (0-1) ---
    if earnings_hours_away is None:
        cat_score = 1.0
    else:
        cat_score = min(earnings_hours_away / (EARNINGS_BLACKOUT_HOURS * 2), 1.0)
    breakdown["catalyst_safety"] = round(cat_score * w["catalyst_safety"], 1)

    total = sum(breakdown.values())
    return round(total, 1), breakdown


def _scan_stock_ticker(ticker: str, bench_ret: float, macro: MacroContext) -> Candidate | None:
    try:
        df = get_stock_history(ticker, period="1y", interval="1d")
        if df.empty or len(df) < MIN_DAILY_HISTORY_BARS:
            return None

        dollar_volume = _dollar_volume_sma(df)
        if dollar_volume < MIN_STOCK_DOLLAR_VOLUME_SMA20:
            return None

        # Safety filter: hard exclude micro-caps regardless of how they
        # ended up on the watchlist. No static minimum price -- liquidity
        # and market cap are what actually gate this.
        market_cap = get_stock_market_cap(ticker)
        if market_cap is not None and market_cap < MIN_STOCK_MARKET_CAP:
            return None

        next_earnings = get_next_earnings_date(ticker)
        hours_away = None
        if next_earnings is not None:
            now = dt.datetime.now(next_earnings.tzinfo) if next_earnings.tzinfo else dt.datetime.now()
            hours_away = (next_earnings - now).total_seconds() / 3600
            if 0 <= hours_away <= EARNINGS_BLACKOUT_HOURS:
                return None  # Catalyst Safety Filter: hard exclude

        decision, reason = evaluate_setup(ticker, "stocks", macro)
        if decision is None:
            return None

        asset_ret = _n_day_return(df, RS_LOOKBACK_DAYS)
        rs_pct = (asset_ret - bench_ret) * 100

        score, breakdown = _score_candidate(decision, dollar_volume, rs_pct, hours_away)
        return Candidate(
            ticker=ticker, asset_class="stocks", score=score, decision=decision,
            dollar_volume=dollar_volume, rs_pct=rs_pct,
            earnings_hours_away=hours_away, breakdown=breakdown,
        )
    except Exception as exc:
        print(f"[screener] {ticker} skipped: {exc}", file=sys.stderr)
        return None


def _scan_crypto_symbol(symbol: str, bench_ret: float, macro: MacroContext) -> Candidate | None:
    try:
        df = get_binance_klines(symbol, interval="1d", limit=300)
        if df.empty or len(df) < MIN_DAILY_HISTORY_BARS:
            return None

        dollar_volume = _dollar_volume_sma(df)
        if dollar_volume < MIN_CRYPTO_DOLLAR_VOLUME_SMA20:
            return None

        decision, reason = evaluate_setup(symbol, "crypto", macro)
        if decision is None:
            return None

        asset_ret = _n_day_return(df, RS_LOOKBACK_DAYS)
        rs_pct = (asset_ret - bench_ret) * 100 if symbol != CRYPTO_BENCHMARK else 0.0

        score, breakdown = _score_candidate(decision, dollar_volume, rs_pct, None)
        return Candidate(
            ticker=symbol, asset_class="crypto", score=score, decision=decision,
            dollar_volume=dollar_volume, rs_pct=rs_pct,
            earnings_hours_away=None, breakdown=breakdown,
        )
    except Exception as exc:
        print(f"[screener] {symbol} skipped: {exc}", file=sys.stderr)
        return None


def scan_stocks(macro: MacroContext, watchlist: list[str] | None = None) -> list[Candidate]:
    watchlist = watchlist or WATCHLIST_STOCKS
    benchmark_df = get_stock_history(STOCK_BENCHMARKS["default"], period="6mo", interval="1d")
    bench_ret = _n_day_return(benchmark_df, RS_LOOKBACK_DAYS)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        results = pool.map(lambda t: _scan_stock_ticker(t, bench_ret, macro), watchlist)
    candidates = [c for c in results if c is not None]
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def scan_crypto(macro: MacroContext, watchlist: list[str] | None = None) -> list[Candidate]:
    watchlist = watchlist or WATCHLIST_CRYPTO
    benchmark_df = get_binance_klines(CRYPTO_BENCHMARK, interval="1d", limit=300)
    bench_ret = _n_day_return(benchmark_df, RS_LOOKBACK_DAYS)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        results = pool.map(lambda s: _scan_crypto_symbol(s, bench_ret, macro), watchlist)
    candidates = [c for c in results if c is not None]
    return sorted(candidates, key=lambda c: c.score, reverse=True)
