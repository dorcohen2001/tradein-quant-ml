"""Market Screener & Asset Selection Engine.

Scans stock and crypto watchlists, applies liquidity/technical/relative-strength/
catalyst filters, assigns a weighted confluence score (0-100), and selects the
top-scoring candidates for downstream analysis.

Per-ticker work is I/O-bound (HTTP fetches), so the watchlist is scanned with a
thread pool; results are re-sorted by score at the end so output ordering is
deterministic regardless of completion order.
"""
from __future__ import annotations

import datetime as dt
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd

from config import (
    CRYPTO_BENCHMARK,
    EARNINGS_BLACKOUT_HOURS,
    MIN_DOLLAR_VOLUME,
    MIN_STOCK_MARKET_CAP,
    MIN_STOCK_PRICE,
    RS_LOOKBACK_DAYS,
    SCORE_WEIGHTS,
    STOCK_BENCHMARKS,
    TOP_N_ASSETS,
    VOLUME_SPIKE_MULT,
    WATCHLIST_CRYPTO,
    WATCHLIST_STOCKS,
)
from ta.ta_engine import TAResult, analyze, volume_spike
from utils.data_fetch import (
    get_binance_klines,
    get_next_earnings_date,
    get_stock_history,
    get_stock_fast_info,
)

SCAN_WORKERS = 8  # concurrent fetch/analyze workers per watchlist scan


@dataclass
class Candidate:
    ticker: str
    asset_class: str
    score: float
    ta: TAResult
    dollar_volume: float
    vol_ratio: float
    rs_pct: float
    earnings_hours_away: float | None
    breakdown: dict = field(default_factory=dict)


def _n_day_return(df: pd.DataFrame, n: int) -> float:
    if len(df) <= n:
        return 0.0
    return float(df["Close"].iloc[-1] / df["Close"].iloc[-1 - n] - 1)


def _score_candidate(ta: TAResult, vol_ratio: float, dollar_volume: float, rs_pct: float,
                      earnings_hours_away: float | None) -> tuple[float, dict]:
    w = SCORE_WEIGHTS
    breakdown = {}

    # --- TA confluence (0-1) ---
    ta_score = 0.0
    if ta.ma_hits:
        ta_score += 0.5
    if ta.in_golden_zone:
        ta_score += 0.3
    if ta.trend_1d in ("UPTREND", "DOWNTREND"):
        ta_score += 0.2
    ta_score = min(ta_score, 1.0)
    breakdown["ta_confluence"] = round(ta_score * w["ta_confluence"], 1)

    # --- Volume (0-1): liquidity gate already applied upstream; reward spike magnitude ---
    liquidity_ok = dollar_volume >= MIN_DOLLAR_VOLUME
    vol_score = (0.5 if liquidity_ok else 0.0) + min(vol_ratio / VOLUME_SPIKE_MULT, 1.0) * 0.5
    vol_score = min(vol_score, 1.0)
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


def _scan_stock_ticker(ticker: str, bench_ret: float) -> Candidate | None:
    try:
        df = get_stock_history(ticker, period="1y", interval="1d")
        if df.empty or len(df) < 60:
            return None

        price = float(df["Close"].iloc[-1])
        volume = float(df["Volume"].iloc[-1])
        dollar_volume = price * volume
        if dollar_volume < MIN_DOLLAR_VOLUME:
            return None

        # Safety filter: hard exclude penny stocks / micro-caps regardless
        # of how they ended up on the watchlist.
        if price < MIN_STOCK_PRICE:
            return None
        market_cap = get_stock_fast_info(ticker).get("market_cap")
        if market_cap is not None and market_cap < MIN_STOCK_MARKET_CAP:
            return None

        next_earnings = get_next_earnings_date(ticker)
        hours_away = None
        if next_earnings is not None:
            now = dt.datetime.now(next_earnings.tzinfo) if next_earnings.tzinfo else dt.datetime.now()
            hours_away = (next_earnings - now).total_seconds() / 3600
            if 0 <= hours_away <= EARNINGS_BLACKOUT_HOURS:
                return None  # Catalyst Safety Filter: hard exclude

        ta_result = analyze(ticker, df)
        spike, ratio = volume_spike(df)
        asset_ret = _n_day_return(df, RS_LOOKBACK_DAYS)
        rs_pct = (asset_ret - bench_ret) * 100

        has_setup = bool(ta_result.ma_hits) or ta_result.in_golden_zone
        if not has_setup:
            return None

        score, breakdown = _score_candidate(ta_result, ratio, dollar_volume, rs_pct, hours_away)
        return Candidate(
            ticker=ticker, asset_class="stocks", score=score, ta=ta_result,
            dollar_volume=dollar_volume, vol_ratio=ratio, rs_pct=rs_pct,
            earnings_hours_away=hours_away, breakdown=breakdown,
        )
    except Exception as exc:
        print(f"[screener] {ticker} skipped: {exc}", file=sys.stderr)
        return None


def _scan_crypto_symbol(symbol: str, bench_ret: float) -> Candidate | None:
    try:
        df = get_binance_klines(symbol, interval="1d", limit=300)
        if df.empty or len(df) < 60:
            return None

        price = float(df["Close"].iloc[-1])
        volume = float(df["Volume"].iloc[-1])
        dollar_volume = price * volume
        if dollar_volume < MIN_DOLLAR_VOLUME:
            return None

        ta_result = analyze(symbol, df)
        spike, ratio = volume_spike(df)
        asset_ret = _n_day_return(df, RS_LOOKBACK_DAYS)
        rs_pct = (asset_ret - bench_ret) * 100 if symbol != CRYPTO_BENCHMARK else 0.0

        has_setup = bool(ta_result.ma_hits) or ta_result.in_golden_zone
        if not has_setup:
            return None

        score, breakdown = _score_candidate(ta_result, ratio, dollar_volume, rs_pct, None)
        return Candidate(
            ticker=symbol, asset_class="crypto", score=score, ta=ta_result,
            dollar_volume=dollar_volume, vol_ratio=ratio, rs_pct=rs_pct,
            earnings_hours_away=None, breakdown=breakdown,
        )
    except Exception as exc:
        print(f"[screener] {symbol} skipped: {exc}", file=sys.stderr)
        return None


def scan_stocks(watchlist: list[str] | None = None) -> list[Candidate]:
    watchlist = watchlist or WATCHLIST_STOCKS
    benchmark_df = get_stock_history(STOCK_BENCHMARKS["default"], period="6mo", interval="1d")
    bench_ret = _n_day_return(benchmark_df, RS_LOOKBACK_DAYS)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        results = pool.map(lambda t: _scan_stock_ticker(t, bench_ret), watchlist)
    candidates = [c for c in results if c is not None]
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def scan_crypto(watchlist: list[str] | None = None) -> list[Candidate]:
    watchlist = watchlist or WATCHLIST_CRYPTO
    benchmark_df = get_binance_klines(CRYPTO_BENCHMARK, interval="1d", limit=300)
    bench_ret = _n_day_return(benchmark_df, RS_LOOKBACK_DAYS)

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        results = pool.map(lambda s: _scan_crypto_symbol(s, bench_ret), watchlist)
    candidates = [c for c in results if c is not None]
    return sorted(candidates, key=lambda c: c.score, reverse=True)


def select_top_assets(candidates: list[Candidate], n: int = TOP_N_ASSETS) -> list[Candidate]:
    return candidates[:n]
