"""Live funnel diagnostics for the Biton (legacy) pipeline: traces every
watchlist ticker through the same gates `screener.market_screener.scan_stocks`
/ `scan_crypto` + `generator.trade_generator.build_trade_setup` use, recording
exactly which stage passed or failed and why, then exports a two-sheet Excel
report each scan cycle -- the Biton-side counterpart to
`TRADEIN/screener/funnel_diagnostics.py`'s Livne report.

This is a separate, read-only tracer -- it re-runs the same already-tested
primitives (`ta.ta_engine.analyze`, `macro.macro_analyzer.get_macro_context`,
`news.news_analyzer.confluence_check`, `generator.trade_generator.
_build_trade_params`) in its own short-circuiting orchestration. It never
calls or modifies `scan_stocks`/`scan_crypto`/`build_trade_setup` themselves,
so it carries zero risk to the real trading-decision path -- at worst it
duplicates a network fetch or its own export step fails, which is caught and
logged, never allowed to interrupt the live scan/alert loop.

Gate order below matches this codebase's actual short-circuit sequence
(`screener.market_screener._scan_stock_ticker`/`_scan_crypto_symbol` then
`generator.trade_generator.build_trade_setup`), so `rejection_reason`
reflects the literal first gate that would reject a ticker today.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    CRYPTO_BENCHMARK,
    EARNINGS_BLACKOUT_HOURS,
    FUNNEL_LOG_DIR,
    FUNNEL_LOG_RETENTION_PER_MODEL,
    MIN_DOLLAR_VOLUME,
    MIN_RISK_REWARD,
    MIN_STOCK_MARKET_CAP,
    MIN_STOCK_PRICE,
    RS_LOOKBACK_DAYS,
    STOCK_BENCHMARKS,
    WATCHLIST_CRYPTO,
    WATCHLIST_STOCKS,
)
from core.news.news_analyzer import analyze_sentiment, confluence_check
from generator.trade_generator import _build_trade_params
from macro.macro_analyzer import MacroContext
from screener.market_screener import Candidate, _n_day_return
from ta.ta_engine import analyze
from utils.data_fetch import get_binance_klines, get_next_earnings_date, get_stock_history, get_stock_market_cap

SCAN_WORKERS = 8  # matches screener.market_screener's concurrency

STAGE_COLUMNS = [
    ("liquidity", "Liquidity / Price / Market Cap / Earnings Filter"),
    ("ta_setup", "TA Setup (MA Confluence or Fib Golden Zone)"),
    ("direction_bias", "Direction Bias (not NEUTRAL)"),
    ("macro_regime", "Macro Correlation Regime"),
    ("news_confluence", "News Confluence"),
    ("risk_validation", "Risk Validation (Stop/Targets + Min R:R)"),
]
_PASS, _FAIL, _NOT_REACHED = "PASS", "FAIL", "NOT_REACHED"


@dataclass
class TickerFunnelRecord:
    ticker: str
    asset_class: str
    price: float | None = None
    direction: str | None = None
    dollar_volume: float | None = None
    rr1: float | None = None
    rr2: float | None = None
    stages: dict = field(default_factory=lambda: {k: _NOT_REACHED for k, _ in STAGE_COLUMNS})
    final_decision: str = "REJECTED"
    rejection_reason: str = ""

    def mark(self, stage: str, passed: bool, reason: str = "") -> None:
        self.stages[stage] = _PASS if passed else _FAIL
        if not passed and not self.rejection_reason:
            label = dict(STAGE_COLUMNS)[stage]
            self.rejection_reason = f"{label}: {reason}" if reason else label


def _trace_stock(ticker: str, bench_ret: float, macro: MacroContext) -> TickerFunnelRecord:
    rec = TickerFunnelRecord(ticker=ticker, asset_class="stocks")
    try:
        df = get_stock_history(ticker, period="1y", interval="1d")
        if df.empty or len(df) < 60:
            rec.mark("liquidity", False, "insufficient history (<60 bars)")
            return rec

        price = float(df["Close"].iloc[-1])
        volume = float(df["Volume"].iloc[-1])
        dollar_volume = price * volume
        rec.price, rec.dollar_volume = price, dollar_volume

        if dollar_volume < MIN_DOLLAR_VOLUME:
            rec.mark("liquidity", False, f"dollar volume ${dollar_volume:,.0f} < ${MIN_DOLLAR_VOLUME:,.0f}")
            return rec
        if price < MIN_STOCK_PRICE:
            rec.mark("liquidity", False, f"price ${price:.2f} < ${MIN_STOCK_PRICE:.2f} floor")
            return rec
        market_cap = get_stock_market_cap(ticker)
        if market_cap is not None and market_cap < MIN_STOCK_MARKET_CAP:
            rec.mark("liquidity", False, f"market cap ${market_cap:,.0f} < ${MIN_STOCK_MARKET_CAP:,.0f}")
            return rec
        next_earnings = get_next_earnings_date(ticker)
        if next_earnings is not None:
            now = dt.datetime.now(next_earnings.tzinfo) if next_earnings.tzinfo else dt.datetime.now()
            hours_away = (next_earnings - now).total_seconds() / 3600
            if 0 <= hours_away <= EARNINGS_BLACKOUT_HOURS:
                rec.mark("liquidity", False, f"earnings in {hours_away:.0f}h (blackout {EARNINGS_BLACKOUT_HOURS}h)")
                return rec
        rec.mark("liquidity", True)

        ta_result = analyze(ticker, df)
        asset_ret = _n_day_return(df, RS_LOOKBACK_DAYS)
        rs_pct = (asset_ret - bench_ret) * 100

        if not (ta_result.ma_hits or ta_result.in_golden_zone):
            rec.mark("ta_setup", False, "no 50/200 EMA confluence, not in Fib golden zone")
            return rec
        rec.mark("ta_setup", True)

        direction = ta_result.direction_bias
        rec.direction = direction
        if direction == "NEUTRAL":
            rec.mark("direction_bias", False, f"trend {ta_result.trend_1d} + TA setup did not resolve to LONG/SHORT")
            return rec
        rec.mark("direction_bias", True)

        if direction not in macro.allowed_directions():
            rec.mark("macro_regime", False, f"{direction} blocked, regime '{macro.regime}' allows {sorted(macro.allowed_directions())}")
            return rec
        rec.mark("macro_regime", True)

        news = analyze_sentiment(ticker, "stocks")
        ok, reason = confluence_check(direction, news)
        if not ok:
            rec.mark("news_confluence", False, reason)
            return rec
        rec.mark("news_confluence", True)

        candidate = Candidate(
            ticker=ticker, asset_class="stocks", score=0.0, ta=ta_result,
            dollar_volume=dollar_volume, vol_ratio=ta_result.vol_ratio, rs_pct=rs_pct,
            earnings_hours_away=None,
        )
        params = _build_trade_params(candidate, direction)
        if params is None:
            rec.mark("risk_validation", False, f"structural stop/target could not clear min 1:{MIN_RISK_REWARD:.0f} R:R")
            return rec
        rec.rr1, rec.rr2 = round(params["rr1"], 2), round(params["rr2"], 2)
        rec.mark("risk_validation", True, f"R:R {rec.rr1}/{rec.rr2}")

        rec.final_decision = "QUALIFIED"
        return rec
    except Exception as exc:
        rec.rejection_reason = f"diagnostic error: {exc}"
        print(f"[funnel_diagnostics] {ticker} trace failed: {exc}", file=sys.stderr)
        return rec


def _trace_crypto(symbol: str, bench_ret: float, macro: MacroContext) -> TickerFunnelRecord:
    rec = TickerFunnelRecord(ticker=symbol, asset_class="crypto")
    try:
        df = get_binance_klines(symbol, interval="1d", limit=300)
        if df.empty or len(df) < 60:
            rec.mark("liquidity", False, "insufficient history (<60 bars)")
            return rec

        price = float(df["Close"].iloc[-1])
        volume = float(df["Volume"].iloc[-1])
        dollar_volume = price * volume
        rec.price, rec.dollar_volume = price, dollar_volume

        if dollar_volume < MIN_DOLLAR_VOLUME:
            rec.mark("liquidity", False, f"dollar volume ${dollar_volume:,.0f} < ${MIN_DOLLAR_VOLUME:,.0f}")
            return rec
        rec.mark("liquidity", True)

        ta_result = analyze(symbol, df)
        asset_ret = _n_day_return(df, RS_LOOKBACK_DAYS)
        rs_pct = (asset_ret - bench_ret) * 100 if symbol != CRYPTO_BENCHMARK else 0.0

        if not (ta_result.ma_hits or ta_result.in_golden_zone):
            rec.mark("ta_setup", False, "no 50/200 EMA confluence, not in Fib golden zone")
            return rec
        rec.mark("ta_setup", True)

        direction = ta_result.direction_bias
        rec.direction = direction
        if direction == "NEUTRAL":
            rec.mark("direction_bias", False, f"trend {ta_result.trend_1d} + TA setup did not resolve to LONG/SHORT")
            return rec
        rec.mark("direction_bias", True)

        if direction not in macro.allowed_directions():
            rec.mark("macro_regime", False, f"{direction} blocked, regime '{macro.regime}' allows {sorted(macro.allowed_directions())}")
            return rec
        rec.mark("macro_regime", True)

        news = analyze_sentiment(symbol, "crypto")
        ok, reason = confluence_check(direction, news)
        if not ok:
            rec.mark("news_confluence", False, reason)
            return rec
        rec.mark("news_confluence", True)

        candidate = Candidate(
            ticker=symbol, asset_class="crypto", score=0.0, ta=ta_result,
            dollar_volume=dollar_volume, vol_ratio=ta_result.vol_ratio, rs_pct=rs_pct,
            earnings_hours_away=None,
        )
        params = _build_trade_params(candidate, direction)
        if params is None:
            rec.mark("risk_validation", False, f"structural stop/target could not clear min 1:{MIN_RISK_REWARD:.0f} R:R")
            return rec
        rec.rr1, rec.rr2 = round(params["rr1"], 2), round(params["rr2"], 2)
        rec.mark("risk_validation", True, f"R:R {rec.rr1}/{rec.rr2}")

        rec.final_decision = "QUALIFIED"
        return rec
    except Exception as exc:
        rec.rejection_reason = f"diagnostic error: {exc}"
        print(f"[funnel_diagnostics] {symbol} trace failed: {exc}", file=sys.stderr)
        return rec


def run_diagnostics(asset_class: str, macro: MacroContext, watchlist: list[str] | None = None) -> list[TickerFunnelRecord]:
    """Traces every ticker in `watchlist` (default: the full configured
    watchlist for `asset_class`) through the Biton funnel, in parallel."""
    if asset_class == "crypto":
        watchlist = watchlist or WATCHLIST_CRYPTO
        bench_df = get_binance_klines(CRYPTO_BENCHMARK, interval="1d", limit=300)
        bench_ret = _n_day_return(bench_df, RS_LOOKBACK_DAYS)
        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
            return list(pool.map(lambda t: _trace_crypto(t, bench_ret, macro), watchlist))

    watchlist = watchlist or WATCHLIST_STOCKS
    bench_df = get_stock_history(STOCK_BENCHMARKS["default"], period="6mo", interval="1d")
    bench_ret = _n_day_return(bench_df, RS_LOOKBACK_DAYS)
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        return list(pool.map(lambda t: _trace_stock(t, bench_ret, macro), watchlist))


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
_QUALIFIED_FILL = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")


def _stage_funnel_counts(records: list[TickerFunnelRecord]) -> list[tuple[str, int, int]]:
    out = []
    for key, label in STAGE_COLUMNS:
        entered = sum(1 for r in records if r.stages[key] != _NOT_REACHED)
        passed = sum(1 for r in records if r.stages[key] == _PASS)
        out.append((label, entered, passed))
    return out


def _style_header_row(ws, row: int) -> None:
    for cell in ws[row]:
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL


def _write_summary_sheet(ws, records_by_class: dict[str, list[TickerFunnelRecord]]) -> None:
    ws.append(["TRADEIN Live Funnel Diagnostic — Biton (legacy) model", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    ws.append([])

    for asset_class, records in records_by_class.items():
        ws.append([f"{asset_class.upper()} — {len(records)} tickers scanned"])
        _style_header_row(ws, ws.max_row)
        header_row = ws.max_row + 1
        ws.append(["Stage", "Entered", "Passed", "Pass %", "Dropped"])
        _style_header_row(ws, header_row)

        for label, entered, passed in _stage_funnel_counts(records):
            pct = round(passed / entered * 100, 1) if entered else 0.0
            ws.append([label, entered, passed, pct, entered - passed])

        qualified = sum(1 for r in records if r.final_decision == "QUALIFIED")
        total = len(records)
        ws.append(["FINAL: QUALIFIED", total, qualified, round(qualified / total * 100, 2) if total else 0.0, total - qualified])
        ws.append([])

    for col, width in zip("ABCDE", (52, 12, 12, 10, 10)):
        ws.column_dimensions[col].width = width


def _write_ticker_log_sheet(ws, records_by_class: dict[str, list[TickerFunnelRecord]]) -> None:
    header = ["Ticker", "Asset Class", "Price", "Direction", "Dollar Volume", "RR1", "RR2"]
    header += [label for _, label in STAGE_COLUMNS]
    header += ["Final Decision", "Rejection Reason"]
    ws.append(header)
    _style_header_row(ws, 1)

    all_records = [r for records in records_by_class.values() for r in records]
    for r in sorted(all_records, key=lambda r: (r.final_decision != "QUALIFIED", r.ticker)):
        row = [r.ticker, r.asset_class, r.price, r.direction, r.dollar_volume, r.rr1, r.rr2]
        row += [r.stages[key] for key, _ in STAGE_COLUMNS]
        row += [r.final_decision, r.rejection_reason]
        ws.append(row)
        if r.final_decision == "QUALIFIED":
            for cell in ws[ws.max_row]:
                cell.fill = _QUALIFIED_FILL

    widths = [12, 10, 12, 10, 18, 8, 8] + [36] * len(STAGE_COLUMNS) + [16, 64]
    for i, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = width
    ws.freeze_panes = "A2"


def _prune_old_reports(prefix: str, keep: int) -> int:
    """Delete all but the newest `keep` reports whose filename starts with
    `prefix`, and return how many were removed.

    Scoped to one model's prefix on purpose: both models export into the same
    FUNNEL_LOG_DIR, and a naive "keep newest N files in this directory" would
    let the more frequent writer evict the other model's reports. Sorted by
    filename rather than mtime because the timestamp is embedded in the name
    (`..._YYYYMMDD_HHMMSS.xlsx`), which is stable even if files get copied.
    Never raises -- a locked/undeletable file just stays."""
    try:
        matching = sorted(f for f in os.listdir(FUNNEL_LOG_DIR)
                          if f.startswith(prefix) and f.endswith(".xlsx"))
    except OSError:
        return 0

    removed = 0
    for name in matching[:-keep] if keep > 0 else matching:
        try:
            os.remove(os.path.join(FUNNEL_LOG_DIR, name))
            removed += 1
        except OSError:
            pass  # open in Excel, permissions, ... -- pruning is best-effort
    return removed


def export_funnel_report(records_by_class: dict[str, list[TickerFunnelRecord]]) -> str | None:
    """Writes the two-sheet Excel report to FUNNEL_LOG_DIR (shared with
    Livne, see config.py), timestamped as
    `live_funnel_biton_YYYYMMDD_HHMMSS.xlsx`. Returns the written path, or
    None (after logging why) if the write failed. Never raises, so a bad
    export can't interrupt the live scan/alert loop."""
    try:
        os.makedirs(FUNNEL_LOG_DIR, exist_ok=True)
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(FUNNEL_LOG_DIR, f"live_funnel_biton_{timestamp}.xlsx")

        wb = Workbook()
        summary_ws = wb.active
        summary_ws.title = "Funnel Summary"
        _write_summary_sheet(summary_ws, records_by_class)

        log_ws = wb.create_sheet("Ticker Diagnostic Log")
        _write_ticker_log_sheet(log_ws, records_by_class)

        wb.save(path)

        pruned = _prune_old_reports("live_funnel_biton_", FUNNEL_LOG_RETENTION_PER_MODEL)
        if pruned:
            print(f"[funnel_diagnostics] Pruned {pruned} old Biton report(s), "
                  f"keeping the newest {FUNNEL_LOG_RETENTION_PER_MODEL}.", file=sys.stderr)
        return path
    except PermissionError as exc:
        print(f"[funnel_diagnostics] Could not write report (file open/locked elsewhere?): {exc}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[funnel_diagnostics] Report export failed: {exc}", file=sys.stderr)
        return None
