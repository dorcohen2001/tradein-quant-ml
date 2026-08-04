"""Live funnel diagnostics: traces every watchlist ticker through the same
gates `strategy.strategy_engine.evaluate_setup` / `ta.mtfa.analyze_mtfa` use,
recording exactly which stage passed or failed and why, then exports a
two-sheet Excel report each scan cycle.

This is a separate, read-only tracer -- it re-runs the same shared, already-
tested primitives (`htf_pass`, `detect_braking_event`, `volume_spike`,
`fib_reaction`, `compute_stop`/`compute_targets`/`validate_min_rr`) in its
own short-circuiting orchestration, exactly the same pattern
`backtest_suite.run_backtest.ta_mtfa_backtest` already uses for the backtest's
daily-bar proxy. It never calls or modifies `evaluate_setup`/`analyze_mtfa`
themselves, so it carries zero risk to the real trading-decision path -- at
worst it duplicates a network fetch (mitigated by `utils.data_fetch`'s
30-minute cache, already warmed by the real scan moments earlier) or its own
export step fails, which is caught and logged, never allowed to interrupt
the live scan/alert loop.

Gate order below matches the real code's actual short-circuit sequence
(`ta.mtfa.analyze_mtfa` then `strategy.strategy_engine.evaluate_setup`), so
`rejection_reason` reflects the literal first gate that would reject a
ticker today -- not a re-ordered narrative.
"""
from __future__ import annotations

import datetime as dt
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from config import (
    CRYPTO_BENCHMARK,
    EARNINGS_BLACKOUT_HOURS,
    FUNNEL_LOG_DIR,
    FUNNEL_LOG_RETENTION_PER_MODEL,
    LTF_ALIGNMENT_TFS,
    LTF_MA_MIN_ALIGNED,
    LTF_TRIGGER_TF,
    LTF_VOLUME_SURGE_MULT,
    MIN_CRYPTO_DOLLAR_VOLUME_SMA20,
    MIN_DAILY_HISTORY_BARS,
    MIN_STOCK_DOLLAR_VOLUME_SMA20,
    MIN_STOCK_MARKET_CAP,
    PRIMARY_MA_PERIOD,
    WATCHLIST_CRYPTO,
    WATCHLIST_STOCKS,
)
from macro.macro_analyzer import MacroContext
from risk.risk_manager import compute_stop, compute_targets, validate_min_rr
from screener.market_screener import _dollar_volume_sma
from strategy.strategy_engine import entry_zone
from ta.candles import detect_reversal_formation, directional_volume_confirmation
from ta.mtfa import (
    MTFASetup,
    _fetch_frames,
    _ma_alignment_vote,
    _within,
    _zone_frame_ok,
    fib_reaction,
    map_zones_top_down,
    swing_excluding_recent,
)
from ta.divergence import DivergenceResult
from ta.ta_engine import add_moving_averages, atr, determine_trend, fibonacci_levels
from utils.data_fetch import get_binance_klines, get_next_earnings_date, get_stock_history, get_stock_market_cap

SCAN_WORKERS = 8  # matches screener.market_screener's concurrency

STAGE_COLUMNS = [
    ("liquidity", "Liquidity / Volume Filter (7d)"),
    ("htf_zone_volume_confluence", "Zone Map: Top-Down Discovery + Refine"),
    ("braking_event", "Trigger: Reversal Formation (15m)"),
    ("itf_volume_spike", "Trigger: Directional Volume Confirmation"),
    ("ma_alignment", "Trigger: 200MA Alignment (1h/15m/5m)"),
    ("fib_confluence", "Trigger: Fib Containment / Reaction"),
    ("macro_regime", "Macro Correlation Regime"),
    ("risk_validation", "Risk Validation (SL/TP1/TP2 + Min R:R)"),
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


def _trace_ticker(ticker: str, asset_class: str, macro: MacroContext) -> TickerFunnelRecord:
    rec = TickerFunnelRecord(ticker=ticker, asset_class=asset_class)
    try:
        # --- Cheap filters (mirrors screener._scan_stock_ticker / _scan_crypto_symbol) ---
        if asset_class == "crypto":
            df = get_binance_klines(ticker, interval="1d", limit=300)
        else:
            df = get_stock_history(ticker, period="1y", interval="1d")

        if df.empty or len(df) < MIN_DAILY_HISTORY_BARS:
            rec.mark("liquidity", False, f"insufficient history (<{MIN_DAILY_HISTORY_BARS} daily bars)")
            return rec
        rec.price = float(df["Close"].iloc[-1])

        dollar_volume = _dollar_volume_sma(df)
        rec.dollar_volume = dollar_volume
        floor = MIN_CRYPTO_DOLLAR_VOLUME_SMA20 if asset_class == "crypto" else MIN_STOCK_DOLLAR_VOLUME_SMA20
        if dollar_volume < floor:
            rec.mark("liquidity", False, f"7d dollar volume ${dollar_volume:,.0f} < ${floor:,.0f}")
            return rec

        if asset_class == "stocks":
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

        # --- Zone map: top-down discovery (weekly/daily/12h) + refinement
        # (4h/3h/1h). Zone-timeframe volume is NOT a gate -- it's a confluence
        # score bonus; a zone touch on any volume passes.
        frames = _fetch_frames(ticker, asset_class)
        trigger_df = frames.get(LTF_TRIGGER_TF)
        if trigger_df is None or trigger_df.empty or len(trigger_df) < 30:
            rec.mark("htf_zone_volume_confluence", False, f"insufficient {LTF_TRIGGER_TF} history")
            return rec
        itf_price = float(trigger_df["Close"].iloc[-1])
        rec.price = itf_price

        daily = frames.get("daily")
        htf_trend = determine_trend(add_moving_averages(daily)) if _zone_frame_ok(daily) else "UNKNOWN"

        long_map = map_zones_top_down("LONG", itf_price, frames)
        short_map = map_zones_top_down("SHORT", itf_price, frames)
        if long_map and short_map:
            if htf_trend == "UPTREND":
                direction, zone_map = "LONG", long_map
            elif htf_trend == "DOWNTREND":
                direction, zone_map = "SHORT", short_map
            else:
                rec.mark("htf_zone_volume_confluence", False,
                         "price at both support and resistance, daily context neutral -- ambiguous")
                return rec
        elif long_map:
            direction, zone_map = "LONG", long_map
        elif short_map:
            direction, zone_map = "SHORT", short_map
        else:
            rec.mark("htf_zone_volume_confluence", False,
                     "no zone match on weekly/daily/12h within proximity")
            return rec

        refined = "/".join(sorted({r["tf"] for r in zone_map["refined"]})) or "none"
        rec.mark("htf_zone_volume_confluence", True,
                 f"{zone_map['zone_type']} on {zone_map['zone_tf']}, refined: {refined}")
        rec.direction = direction

        # --- Trigger: explicit reversal FORMATION on the 15m frame ---
        found, pattern, conviction, bar_offset = detect_reversal_formation(trigger_df, direction)
        if not found:
            rec.mark("braking_event", False,
                     f"no engulfing/hammer/shooting-star/pin-bar formation on {LTF_TRIGGER_TF}")
            return rec
        rec.mark("braking_event", True, f"{pattern} (conviction {conviction:.2f}, {bar_offset} bars back)")

        # --- Trigger: MANDATORY directional volume confirmation ---
        formation_df = trigger_df.iloc[: len(trigger_df) - bar_offset]
        volume_ok, vdetail = directional_volume_confirmation(formation_df, direction)
        if not volume_ok:
            reason = (f"{vdetail['ratio']}x < {LTF_VOLUME_SURGE_MULT}x required"
                      if vdetail["direction_ok"] else
                      f"candle leans against {direction} ({vdetail['ratio']}x volume)")
            rec.mark("itf_volume_spike", False, reason)
            return rec
        vol_ratio = vdetail["ratio"]
        rec.mark("itf_volume_spike", True,
                 f"{vol_ratio}x" + (" + exhaustion" if vdetail.get("exhaustion") else ""))

        # --- Trigger: 200MA alignment across 1h/15m/5m ---
        ma_aligned_count = _ma_alignment_vote(direction, frames)
        n_align = len(LTF_ALIGNMENT_TFS)
        if ma_aligned_count < LTF_MA_MIN_ALIGNED:
            rec.mark("ma_alignment", False,
                     f"{ma_aligned_count}/{n_align} aligned, need {LTF_MA_MIN_ALIGNED}")
            return rec
        rec.mark("ma_alignment", True, f"{ma_aligned_count}/{n_align}")

        itf_hi, itf_lo = swing_excluding_recent(trigger_df)
        itf_fib = fibonacci_levels(itf_hi, itf_lo)
        contained = _within(itf_price, itf_fib["0.5"], itf_fib["0.786"])
        reacted = fib_reaction(trigger_df, itf_fib, direction)
        if not (contained or reacted):
            rec.mark("fib_confluence", False, "not contained in golden zone, no 0.5/0.618 reaction")
            return rec
        rec.mark("fib_confluence", True, "contained" if contained else "reacted")

        # --- Correlation gate (strategy_engine.evaluate_setup) ---
        if direction not in macro.allowed_directions():
            rec.mark("macro_regime", False, f"{direction} blocked, regime '{macro.regime}' allows {sorted(macro.allowed_directions())}")
            return rec
        rec.mark("macro_regime", True)

        # --- Risk validation (risk.risk_manager, via the real MTFASetup so
        # this reuses the exact same structural levels evaluate_setup would) ---
        atr_val = atr(trigger_df).iloc[-1]
        trigger_ma = add_moving_averages(trigger_df, periods=(PRIMARY_MA_PERIOD,))
        ema_val = trigger_ma[f"EMA{PRIMARY_MA_PERIOD}"].iloc[-1]

        setup = MTFASetup(
            ticker=ticker, asset_class=asset_class, price=itf_price, direction_bias=direction,
            htf_trend=htf_trend, htf_zone_type=zone_map["zone_type"], htf_zone=zone_map["zone"],
            divergence=zone_map.get("divergence") or DivergenceResult(),
            order_blocks=zone_map["order_blocks"],
            nearest_support=zone_map["nearest_support"], nearest_resistance=zone_map["nearest_resistance"],
            swing_high=zone_map["swing_high"], swing_low=zone_map["swing_low"],
            fib_levels=zone_map["fib"], monthly_trend=htf_trend,
            itf_trigger_tf=LTF_TRIGGER_TF, itf_pattern=pattern, itf_volume_ratio=vol_ratio,
            itf_ma200_aligned_count=ma_aligned_count, itf_fib_levels=itf_fib,
            itf_ema200=(None if pd.isna(ema_val) else float(ema_val)),
            itf_atr=(None if pd.isna(atr_val) else float(atr_val)), thesis="",
            htf_volume_ratio=0.0,
            zone_tf=zone_map["zone_tf"], reversal_conviction=conviction,
            ema200_anchor=zone_map["ema200_anchor"], volume_detail=vdetail,
        )

        entry_low, entry_high = entry_zone(direction, itf_price)
        entry_price = (entry_low + entry_high) / 2
        stop = compute_stop(direction, setup, entry_price)
        if stop is None:
            rec.mark("risk_validation", False, "no valid structural stop")
            return rec

        targets = compute_targets(direction, setup, entry_price, stop)
        target1, target2 = targets["target1"], targets["target2"]
        if target1 is None or target2 is None:
            rec.mark("risk_validation", False, "targets invalid (non-positive)")
            return rec

        ok, rr1, rr2 = validate_min_rr(entry_price, stop, target1, target2)
        rec.rr1, rec.rr2 = rr1, rr2
        if not ok:
            rec.mark("risk_validation", False, f"R:R {rr1}/{rr2} below minimum")
            return rec
        rec.mark("risk_validation", True, f"R:R {rr1}/{rr2}")

        rec.final_decision = "QUALIFIED"
        return rec
    except Exception as exc:
        rec.rejection_reason = f"diagnostic error: {exc}"
        print(f"[funnel_diagnostics] {ticker} trace failed: {exc}", file=sys.stderr)
        return rec


def run_diagnostics(asset_class: str, macro: MacroContext, watchlist: list[str] | None = None) -> list[TickerFunnelRecord]:
    """Traces every ticker in `watchlist` (default: the full configured
    watchlist for `asset_class`) through the funnel, in parallel."""
    if watchlist is None:
        watchlist = WATCHLIST_CRYPTO if asset_class == "crypto" else WATCHLIST_STOCKS

    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
        records = list(pool.map(lambda t: _trace_ticker(t, asset_class, macro), watchlist))
    return records


# ---------------------------------------------------------------------------
# Excel export
# ---------------------------------------------------------------------------
_HEADER_FONT = Font(color="FFFFFF", bold=True)
_HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
_QUALIFIED_FILL = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")


def _stage_funnel_counts(records: list[TickerFunnelRecord]) -> list[tuple[str, int, int]]:
    """[(stage_label, entered_count, passed_count), ...] in STAGE_COLUMNS
    order. "Entered" = the stage was actually reached (PASS or FAIL, not
    NOT_REACHED because an earlier gate already rejected the ticker)."""
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
    ws.append(["TRADEIN Live Funnel Diagnostic", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
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

    for col, width in zip("ABCDE", (48, 12, 12, 10, 10)):
        ws.column_dimensions[col].width = width


def _write_ticker_log_sheet(ws, records_by_class: dict[str, list[TickerFunnelRecord]]) -> None:
    header = ["Ticker", "Asset Class", "Price", "Direction", "Dollar Volume (SMA20)", "RR1", "RR2"]
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

    widths = [12, 10, 12, 10, 20, 8, 8] + [36] * len(STAGE_COLUMNS) + [16, 64]
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
    """Writes the two-sheet Excel report to FUNNEL_LOG_DIR, timestamped as
    `live_funnel_livne_YYYYMMDD_HHMMSS.xlsx` -- the `livne` infix keeps this
    model's reports distinct from the Biton (legacy) model's own
    `live_funnel_biton_*.xlsx` reports in the same shared directory (see
    models/biton/screener/funnel_diagnostics.py). Returns the written path,
    or None (after logging why) if the write failed -- e.g. a previous report
    still open/locked in Excel. Never raises, so a bad export can't interrupt
    the live scan/alert loop."""
    try:
        os.makedirs(FUNNEL_LOG_DIR, exist_ok=True)
        timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(FUNNEL_LOG_DIR, f"live_funnel_livne_{timestamp}.xlsx")

        wb = Workbook()
        summary_ws = wb.active
        summary_ws.title = "Funnel Summary"
        _write_summary_sheet(summary_ws, records_by_class)

        log_ws = wb.create_sheet("Ticker Diagnostic Log")
        _write_ticker_log_sheet(log_ws, records_by_class)

        wb.save(path)

        pruned = _prune_old_reports("live_funnel_livne_", FUNNEL_LOG_RETENTION_PER_MODEL)
        if pruned:
            print(f"[funnel_diagnostics] Pruned {pruned} old Livne report(s), "
                  f"keeping the newest {FUNNEL_LOG_RETENTION_PER_MODEL}.", file=sys.stderr)
        return path
    except PermissionError as exc:
        print(f"[funnel_diagnostics] Could not write report (file open/locked elsewhere?): {exc}", file=sys.stderr)
        return None
    except Exception as exc:
        print(f"[funnel_diagnostics] Report export failed: {exc}", file=sys.stderr)
        return None
