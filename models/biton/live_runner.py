"""Live Paper Trading Runner with Telegram Alerts.

Ties together the screener, TA/macro/news engines, and trade generator to:
  1. Scan crypto/stocks for qualifying setups.
  2. Log each accepted setup as a new PENDING paper trade.
  3. Send the formatted trade alert to the configured Telegram chat.
  4. Refresh existing open paper trades against live prices and push
     Telegram status updates when a position triggers or closes.

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
    MODEL_ENABLED,
    MODEL_KEY,
    MODEL_NAME,
    REPORT_CLOSED_STATUSES,
    REPORT_STATE_STORE,
    REPORT_UPDATE_STATUSES,
    SEND_EMPTY_DAILY_REPORT,
    STATE_DIR,
    TELEGRAM_ROUTE,
    TOP_N_ASSETS,
    WEEKLY_REPORT_WEEKDAY,
)
from core.locking import TenantBusy, TenantLock
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
from core.utils.chart_attachment import generate_chart_for_setup
from core.utils.market_hours import is_market_open
from core.utils.telegram_notifier import escape_markdown, escape_markdown_code, send_telegram_message, send_telegram_photo
from generator.trade_generator import build_trade_setup, format_telegram_message
from macro.macro_analyzer import get_macro_context
from paper_trading.tracker import (
    catch_up_open_positions,
    flush_alerts,
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
    "WIN_TP1": "✅",
    "WIN_TP2": "🏆",
    "LOSS_SL": "🛑",
    "EXPIRED": "⌛",
}

# ACTIVE is the only status in this notifier that OPENS exposure: price just
# reached the planned entry zone and the paper position went live. Every other
# status closes it. The header used to be hardcoded to "EXIT ALERT", so an
# entry fill was announced as an exit -- exactly backwards, and easy to misread
# at a glance in a busy chat.
_ENTRY_STATUSES = frozenset({"ACTIVE"})


def _alert_header(status: str) -> str:
    return "🔵 ENTRY ALERT" if status in _ENTRY_STATUSES else "🔴 EXIT ALERT"


def _format_status_update(trade: dict) -> str:
    """Status values like `WIN_TP1`/`LOSS_SL` contain underscores and get
    bold-wrapped below -- unescaped, that leaves an unmatched Markdown
    entity and Telegram rejects the whole message with a 400. `status`
    itself stays raw for the control-flow check below; only the *displayed*
    copy is escaped."""
    status = trade["current_status"]
    emoji = _STATUS_EMOJI.get(status, "ℹ️")

    lines = [
        f"[{escape_markdown(MODEL_NAME)}] {_alert_header(status)}",
        f"{emoji} *PAPER TRADE UPDATE* — `{escape_markdown_code(trade['ticker'])}` ({escape_markdown(trade['direction'])})",
        f"Status: *{escape_markdown(status)}*",
    ]
    if status == "ACTIVE":
        lines.append("Price entered the planned entry zone.")
    else:
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


def _send_status_update(trade: dict) -> bool:
    """Transport for one queued status alert. True only on confirmed delivery --
    the outbox clears an entry on True and keeps owing it on False."""
    result = send_telegram_message(
        _format_status_update(trade),
        reply_to_message_id=trade.get("telegram_message_id"),
        route=TELEGRAM_ROUTE,
    )
    print(
        f"[paper] {trade['ticker']} -> {trade['current_status']} "
        f"(telegram {'sent' if result['ok'] else 'FAILED -- stays queued'})",
        file=sys.stderr,
    )
    return bool(result["ok"])


def process_open_positions() -> None:
    """Refresh positions, then deliver everything this model owes.

    Transitions are persisted with an outbox entry and sent afterwards, so a
    failed send leaves the alert owed rather than losing it permanently once
    the store already says "changed last cycle". See core.paper_trading.outbox.
    """
    update_open_positions()

    outcome = flush_alerts(_send_status_update)
    if outcome["attempted"]:
        print(f"[outbox] {outcome['sent']}/{outcome['attempted']} alert(s) delivered"
              + (f", {outcome['failed']} still queued" if outcome["failed"] else ""),
              file=sys.stderr)
    if outcome["abandoned"]:
        print(f"[outbox] WARNING: {outcome['abandoned']} alert(s) abandoned after "
              f"repeated failures -- kept on their trade records as "
              f"`pending_alert.abandoned` and will NOT be retried.", file=sys.stderr)


def run(asset_class: str, top_n: int, candidate_pool: int, dry_run: bool = False,
        export_funnel: bool = True) -> list[str]:
    """Scan for setups. With dry_run=True, only report (no dedup against open
    trades, no Telegram alerts, no paper-trade logging, no chart rendering).

    `export_funnel=True` (default) traces every scanned ticker through the
    same gates a second time via `screener.funnel_diagnostics` and writes a
    two-sheet Excel report to `FUNNEL_LOG_DIR` after each asset class's scan.
    That tracer is fully independent, read-only, and wrapped so any failure
    can never interrupt the real scan/alert/paper-trading flow above."""
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
        candidates = scan_stocks() if cls == "stocks" else scan_crypto()
        print(f"[screener] {len(candidates)} candidates passed liquidity/TA filters", file=sys.stderr)

        # One snapshot per asset class instead of a full store read per
        # candidate -- see the same comment in models/livne/live_runner.py.
        open_trades = [] if dry_run else get_all_trades()

        if export_funnel:
            try:
                print(f"[funnel] Tracing {cls} watchlist for the Biton diagnostic report...", file=sys.stderr)
                funnel_records[cls] = run_diagnostics(cls, macro)
            except Exception as exc:
                print(f"[funnel] {cls} diagnostic trace failed (scan/alerts unaffected): {exc}", file=sys.stderr)

        accepted = 0
        for candidate in candidates[:candidate_pool]:
            if accepted >= top_n:
                break

            if not dry_run and has_open_trade(candidate.ticker, cls, trades=open_trades):
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
                print(f"[funnel] Biton diagnostic report written to {path}", file=sys.stderr)
            else:
                print("[funnel] Biton diagnostic report export failed (see above) -- scan/alerts unaffected.", file=sys.stderr)
        except Exception as exc:
            print(f"[funnel] Unexpected error exporting Biton diagnostic report (scan/alerts unaffected): {exc}", file=sys.stderr)

    return reports


def send_scheduled_summaries(now: dt.datetime | None = None) -> list[str]:
    """Emit the daily (and, on Friday, weekly) summary if they are due.

    Deliberately called from `run_cycle`, not from the main loop: this runner
    is normally launched by the supervisor with `--once`, so the loop never executes and
    a loop-level check would mean Biton never reported at all. For the same
    reason the send-once marker is file-persisted -- the process exits every
    cycle, so an in-memory variable would reset and re-send each time.

    Biton keeps its OWN state file, so its reports are fully independent of
    every other model's; no model can suppress another's summary.

    Wrapped so a reporting failure can never interrupt scanning or position
    management."""
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
    """Execute a single scan + position-refresh cycle. `--skip-scan` runs
    positions only; `--skip-positions` runs the scan only -- used by the
    supervisor (or an operator) to run just one half of a cycle. Livne's
    runner used to drive these two phases itself; scheduling now lives in
    run_supervisor.py and no model invokes another."""
    if not args.skip_positions:
        # Before the live refresh: resolve anything missed while stopped.
        process_catch_up()
        print("[paper] Refreshing open paper trade positions...", file=sys.stderr)
        process_open_positions()

    # After position management, so a close that just happened this cycle is
    # already reflected in the summary. Outside the skip_positions guard: the
    # summary must still go out on a scan-only invocation, since a caller may
    # split the cycle into a positions phase and a scan phase.
    send_scheduled_summaries()

    if args.skip_scan:
        return

    reports = run(args.asset_class, args.top_n, args.candidate_pool)

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
    parser.add_argument("--skip-positions", action="store_true",
                         help="Skip the open-position refresh and only run the new-signal scan "
                              "(run only the scan half of a cycle)")
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
        return 0

    # ONE INSTANCE PER MODEL -- see the same guard in models/livne/live_runner.py.
    # Two processes on one trade store means the second save erases the first's
    # transitions while both alert. `--force` overrides the deactivation flag,
    # never this.
    lock = TenantLock(STATE_DIR, MODEL_KEY)
    try:
        lock.acquire()
    except TenantBusy as exc:
        print(f"[runner] {exc}", file=sys.stderr)
        print("[runner] Nothing was read or written. If that PID is gone, the OS has "
              "already released the lock and the next start will succeed.", file=sys.stderr)
        return 1

    try:
        return _run_until_stopped(args)
    finally:
        lock.release()


def _run_until_stopped(args) -> int:
    """The cycle loop itself, run with this model's lock already held."""
    if args.once:
        try:
            run_cycle(args)
        except Exception as exc:
            print(f"[runner] Cycle failed at {_timestamp()}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            return 1
        return 0

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
    return 0


if __name__ == "__main__":
    try:
        # Non-zero propagates to the supervisor, which records it as an `error`
        # heartbeat -- how a refused duplicate start reaches the fleet view.
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print(f"\n[runner] Stopped by user at {_timestamp()}.", file=sys.stderr)
