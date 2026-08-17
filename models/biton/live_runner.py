"""Live runner for the Biton model.

THIS FILE IS AN ADAPTER, NOT A LOOP. The loop -- the one-instance lock, cycle
order, historical catch-up, the alert outbox, scheduled summaries,
deduplication, error containment and the CLI the supervisor drives -- lives in
`core.runtime.TenantRunner` and is shared with every other tenant. It used to
live here, in 432 lines that were 390 lines identical to Livne's copy, so every
fix had to be made twice and the copies drifted anyway.

What remains is what is genuinely Biton's: a single-timeframe daily pipeline
(MA confluence + Fib golden zone) with a SINGLE-SHOT exit -- TP1 closes the
position outright rather than banking a partial like Livne's PARTIAL_TP1. That
difference is exactly why `apply_bar` stays in this model's tracker and not in
`core`.

Usage:
    python live_runner.py --asset-class both --top-n 2
    python live_runner.py --skip-scan       # only refresh open paper trades
    python live_runner.py --skip-positions  # only run the scan
    python live_runner.py --once            # a single cycle, then exit
"""
from __future__ import annotations

import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import (
    CATCH_UP_CONFIG,
    CHART_OUTPUT_DIR,
    CHART_SPEC,
    DAILY_REPORT_HOUR,
    MODEL_KEY,
    REPORT_STATE_STORE,
    SEND_EMPTY_DAILY_REPORT,
    STATE_DIR,
    TOP_N_ASSETS,
    WEEKLY_REPORT_WEEKDAY,
)
from core.registry import get_model
from core.runtime import TenantContext, TenantRunner, TenantStrategy
from core.utils.chart_attachment import generate_chart_for_setup
from core.utils.telegram_notifier import escape_markdown, escape_markdown_code
from core.news.news_analyzer import analyze_sentiment
from generator.trade_generator import build_trade_setup, format_telegram_message
from macro.macro_analyzer import get_macro_context
from paper_trading import journal as journal_module
from paper_trading.tracker import apply_bar, get_all_trades, get_current_price, log_trade
from screener.funnel_diagnostics import export_funnel_report, run_diagnostics
from screener.market_screener import scan_crypto, scan_stocks
from utils.data_fetch import get_binance_klines, get_recent_intraday_range, get_stock_history

_STATUS_EMOJI = {
    "ACTIVE": "🔵",
    "WIN_TP1": "✅",
    "WIN_TP2": "🏆",
    "LOSS_SL": "🛑",
    "EXPIRED": "⌛",
}

# ACTIVE is the only status here that OPENS exposure: price just reached the
# planned entry zone and the paper position went live. Every other status
# closes it. The header used to be hardcoded to "EXIT ALERT", so an entry fill
# was announced as an exit -- backwards, and easy to misread in a busy chat.
_ENTRY_STATUSES = frozenset({"ACTIVE"})


class BitonStrategy(TenantStrategy):
    """Biton's rules, data and message text."""

    # Single-shot lifecycle: no partial-close state exists in this model, so
    # PARTIAL_TP1 must never be treated as open here even though the platform
    # superset includes it.
    open_statuses = frozenset({"PENDING", "ACTIVE"})

    def __init__(self) -> None:
        # The macro regime is computed once per asset class per cycle and
        # reused by every candidate in it -- it is a market-wide reading, and
        # recomputing it per ticker was pure duplicated network cost.
        self._macro: dict[str, object] = {}

    # --- state ------------------------------------------------------------
    def load_trades(self):
        return get_all_trades()

    def save_trades(self, trades):
        from paper_trading.tracker import _save
        _save(trades)

    def journal(self, trade):
        journal_module.upsert_trade_row(trade)

    # --- lifecycle --------------------------------------------------------
    def apply_bar(self, trade, high, low, ts):
        return apply_bar(trade, high, low, ts)

    # --- market data ------------------------------------------------------
    def current_price(self, ticker, asset_class):
        return get_current_price(ticker, asset_class)

    def price_range(self, ticker, asset_class, lookback_minutes):
        return get_recent_intraday_range(ticker, asset_class, lookback_minutes)

    def fetch_crypto(self, ticker, interval="5m", limit=300):
        return get_binance_klines(ticker, interval=interval, limit=limit)

    def fetch_stock(self, ticker, period="5d", interval="5m"):
        return get_stock_history(ticker, period=period, interval=interval)

    # --- scanning ---------------------------------------------------------
    def find_candidates(self, asset_class):
        print(f"[macro] Analyzing {asset_class} market regime...", file=sys.stderr)
        macro = get_macro_context(asset_class)
        self._macro[asset_class] = macro
        print(f"[macro] {macro.summary}", file=sys.stderr)

        print(f"[screener] Scanning {asset_class} watchlist...", file=sys.stderr)
        # Biton's screener does NOT take the macro context -- its gates are
        # self-contained, and the regime is applied later in build_trade_setup.
        return scan_stocks() if asset_class == "stocks" else scan_crypto()

    def build_setup(self, candidate, asset_class):
        print(f"[news] Fetching sentiment for {candidate.ticker}...", file=sys.stderr)
        news = analyze_sentiment(candidate.ticker, asset_class)
        return build_trade_setup(candidate, self._macro[asset_class], news)

    def log_trade(self, setup, telegram_message_id):
        return log_trade(setup, telegram_message_id=telegram_message_id)

    def chart_for(self, setup):
        return generate_chart_for_setup(
            setup, fetch_crypto=get_binance_klines, fetch_stock=get_stock_history,
            output_dir=CHART_OUTPUT_DIR, spec=CHART_SPEC, model_key=MODEL_KEY,
        )

    # --- funnel diagnostics ----------------------------------------------
    # Read-only, independent of the scan, and wrapped by the runner so any
    # failure here can never interrupt scanning, alerting or paper trading.
    # Biton runs on a 15-minute cadence, so unlike Livne it needs no interval
    # gate: one export per cycle is already modest.
    def diagnostics_due(self):
        return True

    def trace_diagnostics(self, asset_class):
        print(f"[funnel] Tracing {asset_class} watchlist for the Biton diagnostic report...",
              file=sys.stderr)
        return run_diagnostics(asset_class, self._macro.get(asset_class))

    def export_diagnostics(self, asset_class, records):
        path = export_funnel_report(records)
        if path:
            print(f"[funnel] Biton diagnostic report written to {path}", file=sys.stderr)
        else:
            print("[funnel] Biton diagnostic report export failed (see above) -- "
                  "scan/alerts unaffected.", file=sys.stderr)

    # --- presentation -----------------------------------------------------
    def format_status_update(self, trade):
        """Status values like `WIN_TP1`/`LOSS_SL` contain underscores and get
        bold-wrapped below -- unescaped, that leaves an unmatched Markdown
        entity and Telegram rejects the whole message with a 400. `status`
        itself stays raw for the control-flow check; only the displayed copy is
        escaped."""
        status = trade["current_status"]
        emoji = _STATUS_EMOJI.get(status, "ℹ️")
        header = "🔵 ENTRY ALERT" if status in _ENTRY_STATUSES else "🔴 EXIT ALERT"

        lines = [
            f"[{escape_markdown(self.name)}] {header}",
            f"{emoji} *PAPER TRADE UPDATE* — `{escape_markdown_code(trade['ticker'])}` "
            f"({escape_markdown(trade['direction'])})",
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

    def format_setup_alert(self, setup):
        return f"[{escape_markdown(self.name)}] 🟢 SETUP ALERT\n" + format_telegram_message(setup)


def build_runner() -> TenantRunner:
    """Wire Biton's identity, paths and cadence into the shared runner."""
    context = TenantContext(
        model=get_model(MODEL_KEY),
        state_dir=STATE_DIR,
        catch_up=CATCH_UP_CONFIG,
        top_n=TOP_N_ASSETS,
        report_state_store=REPORT_STATE_STORE,
        daily_report_hour=DAILY_REPORT_HOUR,
        weekly_report_weekday=WEEKLY_REPORT_WEEKDAY,
        send_empty_daily_report=SEND_EMPTY_DAILY_REPORT,
    )
    return TenantRunner(context, BitonStrategy())


def run(asset_class: str, top_n: int, candidate_pool: int, dry_run: bool = False,
        export_funnel: bool = True) -> list[str]:
    """Scan only, returning the alert texts. Kept because `main.py` is the
    dry-run CLI and calls exactly this, so the scan pipeline still exists in
    one place rather than being re-implemented for the terminal."""
    runner = build_runner()
    if not export_funnel:
        runner.strategy.diagnostics_due = lambda: False
    return runner.scan(asset_class, top_n, candidate_pool, dry_run=dry_run)


if __name__ == "__main__":
    try:
        sys.exit(build_runner().main())
    except KeyboardInterrupt:
        print("\n[runner] Stopped by user.", file=sys.stderr)
