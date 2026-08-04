"""Live Paper Trading Runner with Telegram Alerts.

Ties together the screener, TA/macro/news engines, and trade generator to:
  1. Scan crypto/stocks for qualifying setups.
  2. Log each accepted setup as a new PENDING paper trade.
  3. Send the formatted trade alert to the configured Telegram chat.
  4. Refresh existing open paper trades against live prices and push
     Telegram status updates when a position triggers or closes.
  5. Trace every scanned ticker through the same gates a second time
     (`screener.funnel_diagnostics`) and export a two-sheet Excel report to
     `funnel_logs/` -- a read-only diagnostic, independent of steps 1-4, that
     can never affect scanning/alerting/paper-trading even if it fails.

Dual-model execution: this codebase is the "Livne" model (current/refactored
pipeline, MODEL_NAME in config.py). Each cycle also drives the "Biton" model
-- the frozen legacy pipeline in the sibling ../biton codebase -- as two
fully isolated `python models/biton/live_runner.py` subprocesses (one for
position management, one for the market scan), so the two pipelines never
share a Python process, import namespace, or global state. Both models write
their runtime artifacts into the shared ../../runtime/ tree, namespaced per
model and never colliding:

    runtime/stores/trades_livne.json      runtime/stores/trades_biton.json
    runtime/journals/trade_journal_livne.xlsx   ..._biton.xlsx
    runtime/funnel_logs/live_funnel_livne_*.xlsx   ..._biton_*.xlsx

The only coupling between the two models is this subprocess call plus those
sibling output paths -- neither imports the other. A failure in the Biton
subprocess (missing folder, crashed cycle, a hang) is caught, bounded by
BITON_PHASE_TIMEOUT_SECONDS, and logged; it can never block or affect
Livne's own cycle.

Usage:
    python live_runner.py --asset-class both --top-n 2
    python live_runner.py --skip-scan   # only refresh open paper trades
    python live_runner.py --once        # run a single cycle and exit (no loop)

By default this script runs forever, executing one full scan/refresh cycle
every CYCLE_INTERVAL_MINUTES (see config.py), sleeping in between. Stop it
with Ctrl+C.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import time
import traceback

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    BITON_CYCLE_INTERVAL_MINUTES,
    BITON_LIVE_RUNNER,
    BITON_MODEL_DIR,
    BITON_PHASE_TIMEOUT_SECONDS,
    CHART_OUTPUT_DIR,
    CYCLE_INTERVAL_MINUTES,
    FUNNEL_EXPORT_INTERVAL_MINUTES,
    MODEL_NAME,
    TOP_N_ASSETS,
)
from core.news.news_analyzer import analyze_sentiment
from core.utils.chart_attachment import generate_chart_for_setup
from core.utils.market_hours import is_market_open
from core.utils.telegram_notifier import escape_markdown, escape_markdown_code, send_telegram_message, send_telegram_photo
from generator.trade_generator import build_trade_setup, format_telegram_message
from macro.macro_analyzer import get_macro_context
from paper_trading.tracker import has_open_trade, log_trade, update_open_positions
from screener.funnel_diagnostics import export_funnel_report, run_diagnostics
from screener.market_screener import scan_crypto, scan_stocks
from utils.data_fetch import get_binance_klines, get_stock_history


def _run_biton_phase(phase: str, asset_class: str, top_n: int, candidate_pool: int) -> None:
    """Invoke the Biton (legacy) model's own live_runner.py as a completely
    separate OS process -- `phase` is "positions" or "scan", matching that
    script's own `--skip-scan`/`--skip-positions` flags, so the two models'
    position-management and market-screen phases interleave in the same
    order every cycle. Never raises: a Biton failure is logged and skipped,
    it can never block or affect Livne's own cycle below.

    The `timeout` is not optional defensive padding -- it is the only thing
    that bounds this call. `subprocess.run` without one blocks forever, and a
    hung child (a Binance/Yahoo socket that never returns, most likely) is
    NOT an exception, so the surrounding `except` cannot rescue it. Livne
    would stop managing its own open positions and stop emitting exit alerts
    for as long as the child stayed stuck -- with real positions open. On a
    timeout the child is killed and the cycle continues; Biton simply misses
    that phase and picks it up next cycle."""
    if not os.path.isfile(BITON_LIVE_RUNNER):
        print(f"[biton] {BITON_LIVE_RUNNER} not found -- skipping Biton {phase} phase.", file=sys.stderr)
        return

    cmd = [sys.executable, BITON_LIVE_RUNNER, "--asset-class", asset_class, "--once",
           "--top-n", str(top_n), "--candidate-pool", str(candidate_pool)]
    cmd.append("--skip-scan" if phase == "positions" else "--skip-positions")

    print(f"[biton] Running Biton (legacy) {phase} phase...", file=sys.stderr)
    try:
        result = subprocess.run(cmd, cwd=BITON_MODEL_DIR, timeout=BITON_PHASE_TIMEOUT_SECONDS)
        if result.returncode != 0:
            print(f"[biton] {phase} phase exited with code {result.returncode} (Livne unaffected).", file=sys.stderr)
    except subprocess.TimeoutExpired:
        # The child is already killed by subprocess.run before this is raised.
        print(f"[biton] {phase} phase exceeded {BITON_PHASE_TIMEOUT_SECONDS}s and was terminated "
              f"(Livne unaffected; Biton retries next cycle).", file=sys.stderr)
    except Exception as exc:
        print(f"[biton] {phase} phase failed to launch (Livne unaffected): {exc}", file=sys.stderr)


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

_STATUS_EMOJI = {
    "ACTIVE": "🔵",
    "PARTIAL_TP1": "✅",
    "WIN_TP2": "🏆",
    "LOSS_SL": "🛑",
    "LOSS_SL_BE": "⚪",
    "EMERGENCY_EXIT": "🚨",
    "EXPIRED": "⌛",
}


def _format_status_update(trade: dict) -> str:
    """Status values like `PARTIAL_TP1`/`LOSS_SL_BE` contain underscores and
    get bold-wrapped (`*{status}*`) below -- unescaped, that's the same class
    of "unmatched Markdown entity" 400 error a snake_case pattern name caused
    in a setup alert (see generator.trade_generator.format_telegram_message
    and utils.telegram_notifier.escape_markdown). `status` itself stays raw
    for the control-flow comparisons below; only the *displayed* copies are
    escaped."""
    status = trade["current_status"]
    emoji = _STATUS_EMOJI.get(status, "ℹ️")

    lines = [
        f"[{escape_markdown(MODEL_NAME)}] 🔴 EXIT ALERT",
        f"{emoji} *PAPER TRADE UPDATE* — `{escape_markdown_code(trade['ticker'])}` ({escape_markdown(trade['direction'])})",
        f"Status: *{escape_markdown(status)}*",
    ]
    if status == "ACTIVE":
        lines.append("Price entered the planned entry zone.")
    elif status == "PARTIAL_TP1":
        lines.append(f"TP1 hit at `{trade['tp1_exit_price']:.4g}` — {int(trade['tp1_close_pct'] * 100)}% closed, "
                      f"stop moved to breakeven (`{trade['breakeven_price']:.4g}`).")
    else:
        if status == "EMERGENCY_EXIT" and trade.get("emergency_exit_reason"):
            lines.append(f"Reason: {escape_markdown(trade['emergency_exit_reason'])}")
        if trade.get("exit_price") is not None:
            lines.append(f"Exit price: `{trade['exit_price']:.4g}`")
        if trade.get("pnl_pct") is not None:
            lines.append(f"P&L: *{trade['pnl_pct']:+.2f}%*")
    return "\n".join(lines)


def process_open_positions() -> None:
    changed = update_open_positions()
    for trade in changed:
        result = send_telegram_message(
            _format_status_update(trade),
            reply_to_message_id=trade.get("telegram_message_id"),
        )
        print(
            f"[paper] {trade['ticker']} -> {trade['current_status']} "
            f"(telegram {'sent' if result['ok'] else 'FAILED'})",
            file=sys.stderr,
        )


def run(asset_class: str, top_n: int, candidate_pool: int, dry_run: bool = False,
        export_funnel: bool = True) -> list[str]:
    """Scan for setups. With dry_run=True, only report (no dedup against open
    trades, no Telegram alerts, no paper-trade logging, no chart rendering).

    `export_funnel=True` (default) traces every scanned ticker through the
    same gates a second time via `screener.funnel_diagnostics` and writes a
    two-sheet Excel report to `FUNNEL_LOG_DIR` after each asset class's scan.
    That tracer is fully independent, read-only, and wrapped so any failure
    (network hiccup, a locked previous report) is caught and logged -- it can
    never interrupt or affect the real scan/alert/paper-trading flow above."""
    reports: list[str] = []
    rejections: list[str] = []
    funnel_records: dict[str, list] = {}

    classes = ["stocks", "crypto"] if asset_class == "both" else [asset_class]

    for cls in classes:
        if cls == "stocks" and not is_market_open():
            print(
                "[market] US equities market is CLOSED — skipping stocks scan "
                "(crypto continues 24/7).",
                file=sys.stderr,
            )
            continue

        print(f"[macro] Analyzing {cls} market regime...", file=sys.stderr)
        macro = get_macro_context(cls)
        print(f"[macro] {macro.summary}", file=sys.stderr)

        print(f"[screener] Scanning {cls} watchlist...", file=sys.stderr)
        candidates = scan_stocks(macro) if cls == "stocks" else scan_crypto(macro)
        print(f"[screener] {len(candidates)} candidates passed liquidity/TA filters", file=sys.stderr)

        if export_funnel:
            try:
                print(f"[funnel] Tracing {cls} watchlist for the diagnostic report...", file=sys.stderr)
                funnel_records[cls] = run_diagnostics(cls, macro)
            except Exception as exc:
                print(f"[funnel] {cls} diagnostic trace failed (scan/alerts unaffected): {exc}", file=sys.stderr)

        accepted = 0
        for candidate in candidates[:candidate_pool]:
            if accepted >= top_n:
                break

            if not dry_run and has_open_trade(candidate.ticker, cls):
                print(
                    f"[dedup] {candidate.ticker} already has an open/active paper "
                    f"trade — skipping duplicate alert",
                    file=sys.stderr,
                )
                continue

            print(f"[news] Fetching sentiment for {candidate.ticker}...", file=sys.stderr)
            news = analyze_sentiment(candidate.ticker, cls)

            setup, reason = build_trade_setup(candidate, macro, news)
            if setup is None:
                rejections.append(reason)
                continue

            message = f"[{escape_markdown(MODEL_NAME)}] 🟢 SETUP ALERT\n" + format_telegram_message(setup)

            if not dry_run:
                chart_path = generate_chart_for_setup(
                    setup,
                    fetch_crypto=get_binance_klines,
                    fetch_stock=get_stock_history,
                    output_dir=CHART_OUTPUT_DIR,
                )
                if chart_path:
                    print(f"[chart] Rendered {chart_path}", file=sys.stderr)
                    tg_result = send_telegram_photo(chart_path, caption=message)
                    if not tg_result["ok"]:
                        print("[chart] Photo alert failed — falling back to text-only.",
                              file=sys.stderr)
                        tg_result = send_telegram_message(message)
                else:
                    tg_result = send_telegram_message(message)
                trade_record = log_trade(setup, telegram_message_id=tg_result["message_id"])
                print(
                    f"[paper] Logged trade {trade_record['id']} for {setup.ticker} "
                    f"(telegram {'sent' if tg_result['ok'] else 'FAILED'})",
                    file=sys.stderr,
                )

            reports.append(message)
            accepted += 1

        if accepted == 0:
            print(f"[result] No qualifying {cls} setups today.", file=sys.stderr)

    if rejections:
        print("\n[filtered out]", file=sys.stderr)
        for r in rejections:
            print(f"  - {r}", file=sys.stderr)

    if export_funnel and funnel_records:
        try:
            path = export_funnel_report(funnel_records)
            if path:
                print(f"[funnel] Diagnostic report written to {path}", file=sys.stderr)
            else:
                print("[funnel] Diagnostic report export failed (see above) -- scan/alerts unaffected.", file=sys.stderr)
        except Exception as exc:
            print(f"[funnel] Unexpected error exporting diagnostic report (scan/alerts unaffected): {exc}", file=sys.stderr)

    return reports


class _IntervalGate:
    """Fires at most once per `minutes`, independent of the caller's cadence.

    Livne now cycles every 2 minutes (intraday), but two things must NOT
    inherit that rate:

      Biton -- a separate, frozen production model. It is invoked on its own
      15-minute schedule so a Livne-only timeframe change does not silently
      multiply Biton's run rate and API load by ~7x. Its behaviour is
      unchanged from before the intraday switch.

      The funnel export -- a diagnostic that re-runs the whole gate chain for
      every watchlist ticker. Per-cycle at 2 minutes it would emit ~30
      reports/hour, churn through the 50-file retention in under two hours,
      and roughly double scan cost for something no trade depends on.

    First call always fires, so a freshly started runner does both immediately
    rather than waiting out a full interval.
    """

    def __init__(self, minutes: float) -> None:
        self.interval = dt.timedelta(minutes=minutes)
        self.last: dt.datetime | None = None

    def due(self) -> bool:
        now = dt.datetime.now()
        if self.last is None or (now - self.last) >= self.interval:
            self.last = now
            return True
        return False


_biton_gate = _IntervalGate(BITON_CYCLE_INTERVAL_MINUTES)
_funnel_gate = _IntervalGate(FUNNEL_EXPORT_INTERVAL_MINUTES)


def run_cycle(args) -> None:
    """Execute a single scan + position-refresh cycle.

    Livne's own position management and scan run every cycle (every
    CYCLE_INTERVAL_MINUTES, now 2 min). Biton is interleaved in the same
    order as before -- positions then scan, Biton before Livne -- but only on
    the cycles where its own 15-minute gate is due."""
    biton_due = _biton_gate.due()

    if biton_due:
        _run_biton_phase("positions", args.asset_class, args.top_n, args.candidate_pool)
    print(f"[{MODEL_NAME}] Refreshing open paper trade positions...", file=sys.stderr)
    process_open_positions()

    if args.skip_scan:
        return

    if biton_due:
        _run_biton_phase("scan", args.asset_class, args.top_n, args.candidate_pool)
    reports = run(args.asset_class, args.top_n, args.candidate_pool,
                  export_funnel=_funnel_gate.due())

    if not reports:
        print("No high-conviction setups found today across the scanned watchlist.")
        return

    for report in reports:
        print(report)
        print("\n" + "=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Live Paper Trading Runner with Telegram Alerts")
    parser.add_argument("--asset-class", choices=["stocks", "crypto", "both"], default="both",
                         help="Which market(s) to scan (default: both)")
    parser.add_argument("--top-n", type=int, default=TOP_N_ASSETS,
                         help="Max number of setups to output per asset class (default: %(default)s)")
    parser.add_argument("--candidate-pool", type=int, default=6,
                         help="How many top-scored candidates to evaluate before giving up (default: %(default)s)")
    parser.add_argument("--skip-scan", action="store_true",
                         help="Skip the new-signal scan and only refresh existing open paper trades")
    parser.add_argument("--once", action="store_true",
                         help="Run a single cycle and exit instead of looping forever")
    args = parser.parse_args()

    if args.once:
        try:
            run_cycle(args)
        except Exception as exc:
            print(f"[runner] Cycle failed at {_timestamp()}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
        return

    print(f"[runner] Starting continuous execution loop "
          f"(every {CYCLE_INTERVAL_MINUTES} min). Press Ctrl+C to stop.", file=sys.stderr)

    cycle = 0
    while True:
        cycle += 1
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(f"[cycle #{cycle}] START — {_timestamp()}", file=sys.stderr)
        print("=" * 60, file=sys.stderr)

        try:
            run_cycle(args)
        except Exception as exc:
            print(f"[cycle #{cycle}] ERROR at {_timestamp()}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            print(f"[cycle #{cycle}] Continuing to next cycle despite error.", file=sys.stderr)

        finished_at = dt.datetime.now()
        next_run = finished_at + dt.timedelta(minutes=CYCLE_INTERVAL_MINUTES)
        print(f"[cycle #{cycle}] DONE — {finished_at.strftime('%Y-%m-%d %H:%M:%S')}", file=sys.stderr)
        print(f"[runner] Sleeping {CYCLE_INTERVAL_MINUTES} min. "
              f"Next cycle at {next_run.strftime('%Y-%m-%d %H:%M:%S')}.", file=sys.stderr)

        try:
            time.sleep(CYCLE_INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            print(f"\n[runner] Stopped by user at {_timestamp()}.", file=sys.stderr)
            break


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[runner] Stopped by user at {_timestamp()}.", file=sys.stderr)
