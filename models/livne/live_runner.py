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

This runner drives THIS MODEL ONLY. It used to also launch the Biton model as
two subprocesses every cycle, which made Livne Biton's de-facto scheduler:
Livne dying stopped Biton entirely, Livne's `--asset-class`/`--top-n` silently
governed Biton's scan scope, and Biton's cadence was a gate inside Livne's
loop. That orchestration now lives in `run_supervisor.py` at the repository
root, which runs every registered model independently, in parallel, each on
its own interval and with its own timeout.

Models remain fully isolated: separate OS processes, separate import
namespaces, and per-model runtime artifacts that never collide:

    runtime/stores/trades_livne.json      runtime/stores/trades_biton.json
    runtime/journals/trade_journal_livne.xlsx   ..._biton.xlsx
    runtime/funnel_logs/live_funnel_livne_*.xlsx   ..._biton_*.xlsx

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
import sys
import time
import traceback

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    CHART_OUTPUT_DIR,
    CHART_SPEC,
    CYCLE_INTERVAL_MINUTES,
    DAILY_REPORT_HOUR,
    FUNNEL_EXPORT_INTERVAL_MINUTES,
    GATE_STATE_STORE,
    MODEL_ENABLED,
    MODEL_KEY,
    MODEL_NAME,
    REPORT_CLOSED_STATUSES,
    REPORT_STATE_STORE,
    REPORT_UPDATE_STATUSES,
    SEND_EMPTY_DAILY_REPORT,
    TELEGRAM_ROUTE,
    TOP_N_ASSETS,
    WEEKLY_REPORT_WEEKDAY,
)
from core.reporting.summary import (
    build_daily_summary,
    build_weekly_summary,
    daily_report_due,
    load_report_state,
    save_report_state,
    weekly_report_due,
)
from core.news.news_analyzer import analyze_sentiment
from core.paper_trading.catch_up import format_catch_up_summary
from core.scheduling import PersistentIntervalGate
from core.utils.chart_attachment import generate_chart_for_setup
from core.utils.market_hours import is_market_open
from core.utils.telegram_notifier import escape_markdown, escape_markdown_code, send_telegram_message, send_telegram_photo
from generator.trade_generator import build_trade_setup, format_telegram_message
from macro.macro_analyzer import get_macro_context
from paper_trading.tracker import (
    catch_up_open_positions,
    get_all_trades,
    has_open_trade,
    log_trade,
    update_open_positions,
)
from screener.funnel_diagnostics import export_funnel_report, run_diagnostics
from screener.market_screener import scan_crypto, scan_stocks
from utils.data_fetch import get_binance_klines, get_stock_history


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

# ACTIVE is the only status in this notifier that OPENS exposure: price just
# reached the planned entry zone and the paper position went live. Every other
# status closes some or all of it. The header used to be hardcoded to
# "EXIT ALERT", so an entry fill was announced as an exit -- exactly backwards,
# and easy to misread at a glance in a busy chat.
_ENTRY_STATUSES = frozenset({"ACTIVE"})


def _alert_header(status: str) -> str:
    return "🔵 ENTRY ALERT" if status in _ENTRY_STATUSES else "🔴 EXIT ALERT"


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
        f"[{escape_markdown(MODEL_NAME)}] {_alert_header(status)}",
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


def process_catch_up() -> None:
    """Replay price action missed while this runner was stopped, then report it
    as ONE consolidated message.

    Deliberately not per-trade alerts. A runner that was down overnight can come
    back to several resolutions at once, and firing that many delayed
    "EXIT ALERT" messages would read as if they were all happening right now --
    actively misleading in a channel people trade from. One clearly-labelled
    summary carrying the real crossing timestamps says what actually happened.

    Costs nothing at steady state: with a healthy cadence every open trade was
    checked one cycle ago, so the gap check short-circuits before any historical
    fetch (see core.paper_trading.catch_up.gap_window).
    """
    report = catch_up_open_positions()
    if not report.events:
        return

    print(f"[catch-up] {len(report.events)} transition(s) across "
          f"{report.trades_replayed} position(s), {report.bars_replayed} bars replayed",
          file=sys.stderr)

    message = format_catch_up_summary(MODEL_NAME, report)
    if message:
        result = send_telegram_message(message, route=TELEGRAM_ROUTE)
        print(f"[catch-up] consolidated summary "
              f"(telegram {'sent' if result['ok'] else 'FAILED'})", file=sys.stderr)


def process_open_positions() -> None:
    changed = update_open_positions()
    for trade in changed:
        result = send_telegram_message(
            _format_status_update(trade),
            reply_to_message_id=trade.get("telegram_message_id"),
            route=TELEGRAM_ROUTE,
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
                    spec=CHART_SPEC,
                    model_key=MODEL_KEY,
                )
                if chart_path:
                    print(f"[chart] Rendered {chart_path}", file=sys.stderr)
                    tg_result = send_telegram_photo(chart_path, caption=message, route=TELEGRAM_ROUTE)
                    if not tg_result["ok"]:
                        print("[chart] Photo alert failed — falling back to text-only.",
                              file=sys.stderr)
                        tg_result = send_telegram_message(message, route=TELEGRAM_ROUTE)
                else:
                    tg_result = send_telegram_message(message, route=TELEGRAM_ROUTE)
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


# The funnel export must NOT inherit Livne's 2-minute cadence: it re-runs the
# whole gate chain for every watchlist ticker, so per-cycle it would emit ~30
# reports/hour, churn through the 50-file retention in under two hours, and
# roughly double scan cost for something no trade depends on.
#
# The gate is FILE-PERSISTED rather than in-memory. Under the supervisor this
# runner is launched as a fresh `--once` process per tick, so an in-memory
# marker would reset every invocation and the gate would degrade to "fire every
# cycle" -- reintroducing exactly the cost it exists to prevent. The old
# in-memory `_IntervalGate` (and the Biton scheduling gate beside it) moved to
# `core.scheduling`; Biton is now scheduled by run_supervisor.py, not from here.
_funnel_gate = PersistentIntervalGate(
    FUNNEL_EXPORT_INTERVAL_MINUTES, state_path=GATE_STATE_STORE, name="funnel_export",
)


def send_scheduled_summaries(now: dt.datetime | None = None) -> list[str]:
    """Emit the daily (and, on Friday, weekly) summary if they are due.

    Called once per cycle from `run_cycle`. Returns the labels actually sent,
    for logging and tests.

    Placed in `run_cycle` rather than the main loop on purpose: it is the one
    code path that executes under BOTH `--once` and the continuous loop, which
    keeps this identical in shape to Biton's copy (whose loop never runs at
    all, since Livne always invokes it with `--once`).

    Wrapped so a reporting failure -- a locked state file, a Telegram outage --
    can never interrupt scanning or position management. A missed summary is an
    inconvenience; a crashed cycle with open positions is not."""
    now = now or dt.datetime.now()
    sent: list[str] = []
    try:
        state = load_report_state(REPORT_STATE_STORE)
        trades = get_all_trades()

        if daily_report_due(state, now, DAILY_REPORT_HOUR):
            message = build_daily_summary(
                MODEL_NAME, trades, now,
                closed_statuses=REPORT_CLOSED_STATUSES,
                update_statuses=REPORT_UPDATE_STATUSES,
            )
            has_activity = "_none_" not in message or message.count("_none_") < 2
            if has_activity or SEND_EMPTY_DAILY_REPORT:
                send_telegram_message(message, route=TELEGRAM_ROUTE)
                sent.append("daily")
            # Mark the day done either way, so a quiet day is not retried every
            # cycle for the rest of the evening.
            state["last_daily_report_date"] = now.date().isoformat()

        if weekly_report_due(state, now, DAILY_REPORT_HOUR, WEEKLY_REPORT_WEEKDAY):
            send_telegram_message(build_weekly_summary(
                MODEL_NAME, trades, now, closed_statuses=REPORT_CLOSED_STATUSES,
            ), route=TELEGRAM_ROUTE)
            state["last_weekly_report_date"] = now.date().isoformat()
            sent.append("weekly")

        if sent:
            save_report_state(REPORT_STATE_STORE, state)
            print(f"[report] Sent {'/'.join(sent)} summary for {MODEL_NAME}.", file=sys.stderr)
        elif state.get("last_daily_report_date") == now.date().isoformat():
            save_report_state(REPORT_STATE_STORE, state)
    except Exception as exc:
        print(f"[report] Summary dispatch failed (scan/positions unaffected): {exc}", file=sys.stderr)
    return sent


def run_cycle(args) -> None:
    """Execute a single scan + position-refresh cycle for THIS model.

    Position management first, then the scan -- unchanged. What is gone is the
    interleaved Biton invocation that used to bracket both halves; other models
    are scheduled independently by run_supervisor.py."""
    # Before the live refresh: resolve anything missed while stopped, so a
    # position is graded at the level and minute it was actually crossed
    # rather than wherever price happens to be right now.
    process_catch_up()
    print(f"[{MODEL_NAME}] Refreshing open paper trade positions...", file=sys.stderr)
    process_open_positions()

    # After position management, so a close that just happened this cycle is
    # already reflected in the summary.
    send_scheduled_summaries()

    if args.skip_scan:
        return

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
    parser.add_argument("--force", action="store_true",
                         help="Run even if this model is marked enabled=False in core/registry.py")
    args = parser.parse_args()

    # A deactivated model must not trade or alert even when invoked directly.
    # Without this, a Task Scheduler entry or a stale shell command pointing at
    # this file would keep running a model someone deliberately paused -- while
    # the registry, the supervisor and the daily report all say it is off.
    if not MODEL_ENABLED and not args.force:
        print(f"[runner] {MODEL_NAME} is DEACTIVATED in core/registry.py -- refusing to run.",
              file=sys.stderr)
        print("[runner] Stored state is untouched. Set enabled=True to resume, "
              "or pass --force for a one-off run.", file=sys.stderr)
        return

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
