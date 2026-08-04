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

from config import CYCLE_INTERVAL_MINUTES, MODEL_NAME, TOP_N_ASSETS
from generator.trade_generator import build_trade_setup, format_telegram_message
from macro.macro_analyzer import get_macro_context
from news.news_analyzer import analyze_sentiment
from paper_trading.tracker import has_open_trade, log_trade, update_open_positions
from screener.funnel_diagnostics import export_funnel_report, run_diagnostics
from screener.market_screener import scan_crypto, scan_stocks
from utils.chart_attachment import generate_chart_for_setup
from utils.market_hours import is_market_open
from utils.telegram_notifier import escape_markdown, escape_markdown_code, send_telegram_message, send_telegram_photo


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

_STATUS_EMOJI = {
    "ACTIVE": "🔵",
    "WIN_TP1": "✅",
    "WIN_TP2": "🏆",
    "LOSS_SL": "🛑",
    "EXPIRED": "⌛",
}


def _format_status_update(trade: dict) -> str:
    """Status values like `WIN_TP1`/`LOSS_SL` contain underscores and get
    bold-wrapped below -- unescaped, that leaves an unmatched Markdown
    entity and Telegram rejects the whole message with a 400. `status`
    itself stays raw for the control-flow check below; only the *displayed*
    copy is escaped."""
    status = trade["current_status"]
    emoji = _STATUS_EMOJI.get(status, "ℹ️")

    lines = [
        f"[{escape_markdown(MODEL_NAME)}] 🔴 EXIT ALERT",
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
                chart_path = generate_chart_for_setup(setup)
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
                print(f"[funnel] Biton diagnostic report written to {path}", file=sys.stderr)
            else:
                print("[funnel] Biton diagnostic report export failed (see above) -- scan/alerts unaffected.", file=sys.stderr)
        except Exception as exc:
            print(f"[funnel] Unexpected error exporting Biton diagnostic report (scan/alerts unaffected): {exc}", file=sys.stderr)

    return reports


def run_cycle(args) -> None:
    """Execute a single scan + position-refresh cycle. `--skip-scan` runs
    positions only; `--skip-positions` runs the scan only -- used by the
    Livne (current) codebase to interleave Biton's position-management and
    market-screen phases with its own, matching live_runner.py's own cycle
    ordering (see TRADEIN/live_runner.py)."""
    if not args.skip_positions:
        print("[paper] Refreshing open paper trade positions...", file=sys.stderr)
        process_open_positions()

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
                              "(used by the Livne runner to interleave both models' cycle phases)")
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
