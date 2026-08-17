"""Live runner for the Livne model.

THIS FILE IS AN ADAPTER, NOT A LOOP. The loop -- the one-instance lock, cycle
order, historical catch-up, the alert outbox, scheduled summaries,
deduplication, error containment and the CLI the supervisor drives -- lives in
`core.runtime.TenantRunner` and is shared with every other tenant. It used to
live here, and 390 of its 432 lines were identical to Biton's copy.

What remains is what is genuinely Livne's:

  * a multi-timeframe (MTFA) intraday pipeline on a 15m trigger;
  * a multi-tier lifecycle -- PENDING -> ACTIVE -> PARTIAL_TP1 (half closed,
    stop to breakeven) -> WIN_TP2 / LOSS_SL_BE -- which is why `apply_bar`
    stays in this model's tracker rather than in `core`;
  * the EMERGENCY REVERSAL exit, a live-only close driven by a volume anomaly
    rather than a price level. It runs through the runner's `pre_bar` hook,
    which exists precisely because this model has such a signal and the others
    do not. It is deliberately not replayed by the historical catch-up: from
    coarse historical bars it would invent exits the live system would never
    have taken.

Usage:
    python live_runner.py --asset-class both --top-n 2
    python live_runner.py --skip-scan   # only refresh open paper trades
    python live_runner.py --once        # a single cycle, then exit
"""
from __future__ import annotations

import datetime as dt
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
    EMERGENCY_CANDLE_LOOKBACK,
    FUNNEL_EXPORT_INTERVAL_MINUTES,
    GATE_STATE_STORE,
    MODEL_KEY,
    REPORT_STATE_STORE,
    SEND_EMPTY_DAILY_REPORT,
    STATE_DIR,
    TOP_N_ASSETS,
    WEEKLY_REPORT_WEEKDAY,
)
from core.news.news_analyzer import analyze_sentiment
from core.registry import get_model
from core.runtime import TenantContext, TenantRunner, TenantStrategy
from core.scheduling import PersistentIntervalGate
from core.utils.chart_attachment import generate_chart_for_setup
from core.utils.telegram_notifier import escape_markdown, escape_markdown_code
from generator.trade_generator import build_trade_setup, format_telegram_message
from macro.macro_analyzer import get_macro_context
from paper_trading import journal as journal_module
from paper_trading.tracker import (
    _close_trade,
    apply_bar,
    get_all_trades,
    get_current_price,
    log_trade,
)
from risk.risk_manager import detect_emergency_reversal
from screener.funnel_diagnostics import export_funnel_report, run_diagnostics
from screener.market_screener import scan_crypto, scan_stocks
from utils.data_fetch import (
    get_binance_klines,
    get_recent_intraday_candles,
    get_recent_intraday_range,
    get_stock_history,
)

_STATUS_EMOJI = {
    "ACTIVE": "🔵",
    "PARTIAL_TP1": "✅",
    "WIN_TP2": "🏆",
    "LOSS_SL": "🛑",
    "LOSS_SL_BE": "⚪",
    "EMERGENCY_EXIT": "🚨",
    "EXPIRED": "⌛",
}

# ACTIVE is the only status here that OPENS exposure: price just reached the
# planned entry zone and the paper position went live. Every other status
# closes some or all of it. The header used to be hardcoded to "EXIT ALERT", so
# an entry fill was announced as an exit -- exactly backwards, and easy to
# misread at a glance in a busy chat.
_ENTRY_STATUSES = frozenset({"ACTIVE"})

# The funnel export must NOT inherit Livne's 2-minute cadence: it re-runs the
# whole gate chain for every watchlist ticker, so per-cycle it would emit ~30
# reports/hour, churn through the 50-file retention in under two hours, and
# roughly double scan cost for something no trade depends on.
#
# FILE-PERSISTED rather than in-memory: the supervisor launches this runner as
# a fresh `--once` process per tick, so an in-memory marker would reset every
# invocation and the gate would degrade to "fire every cycle".
_funnel_gate = PersistentIntervalGate(
    FUNNEL_EXPORT_INTERVAL_MINUTES, state_path=GATE_STATE_STORE, name="funnel_export",
)


class LivneStrategy(TenantStrategy):
    """Livne's rules, data and message text."""

    # Multi-tier lifecycle: PARTIAL_TP1 is non-terminal -- half the position is
    # still open with its stop at breakeven -- so it MUST be treated as open.
    open_statuses = frozenset({"PENDING", "ACTIVE", "PARTIAL_TP1"})

    def __init__(self) -> None:
        # The macro regime is a market-wide reading, computed once per asset
        # class per cycle and reused by every candidate in it.
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

    def pre_bar(self, trade, price, now):
        """The emergency reversal exit -- live-only, and checked BEFORE the
        range test.

        It keys off a volume/candle anomaly rather than a price level, so it
        has no meaningful historical replay: `core.paper_trading.catch_up`
        deliberately does not simulate it, and neither does the backtest. A
        position that would have been emergency-exited during a gap is instead
        resolved by its stop or target, or left open -- the conservative
        direction.

        Only ACTIVE and PARTIAL_TP1 carry exposure to close.
        """
        if trade["current_status"] not in ("ACTIVE", "PARTIAL_TP1"):
            return False

        candles = get_recent_intraday_candles(
            trade["ticker"], trade["asset_class"], EMERGENCY_CANDLE_LOOKBACK + 5)
        if candles is None:
            return False

        is_emergency, reason = detect_emergency_reversal(trade["direction"], candles)
        if not is_emergency:
            return False

        entry_low, entry_high = trade["entry_zone"]
        stamp = now.isoformat(timespec="seconds")
        _close_trade(trade, "EMERGENCY_EXIT", price, (entry_low + entry_high) / 2,
                     emergency_reason=reason, at=stamp)
        trade["last_status_change"] = stamp
        return True

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
        return (scan_stocks(macro) if asset_class == "stocks" else scan_crypto(macro))

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
    def diagnostics_due(self):
        return _funnel_gate.due()

    def trace_diagnostics(self, asset_class):
        print(f"[funnel] Tracing {asset_class} watchlist for the diagnostic report...",
              file=sys.stderr)
        return run_diagnostics(asset_class, self._macro.get(asset_class))

    def export_diagnostics(self, asset_class, records):
        path = export_funnel_report(records)
        if path:
            print(f"[funnel] Diagnostic report written to {path}", file=sys.stderr)
        else:
            print("[funnel] Diagnostic report export failed (see above) -- "
                  "scan/alerts unaffected.", file=sys.stderr)

    # --- presentation -----------------------------------------------------
    def format_status_update(self, trade):
        """Status values like `PARTIAL_TP1`/`LOSS_SL_BE` contain underscores and
        get bold-wrapped below -- unescaped, that is the same class of
        "unmatched Markdown entity" 400 error a snake_case pattern name once
        caused in a setup alert. `status` itself stays raw for the control-flow
        comparisons; only the DISPLAYED copies are escaped."""
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
        elif status == "PARTIAL_TP1":
            lines.append(
                f"TP1 hit at `{trade['tp1_exit_price']:.4g}` — "
                f"{int(trade['tp1_close_pct'] * 100)}% closed, "
                f"stop moved to breakeven (`{trade['breakeven_price']:.4g}`).")
        else:
            if status == "EMERGENCY_EXIT" and trade.get("emergency_exit_reason"):
                lines.append(f"Reason: {escape_markdown(trade['emergency_exit_reason'])}")
            if trade.get("exit_price") is not None:
                lines.append(f"Exit price: `{trade['exit_price']:.4g}`")
            if trade.get("pnl_pct") is not None:
                lines.append(f"P&L: *{trade['pnl_pct']:+.2f}%*")
        return "\n".join(lines)

    def format_setup_alert(self, setup):
        return f"[{escape_markdown(self.name)}] 🟢 SETUP ALERT\n" + format_telegram_message(setup)


def build_runner() -> TenantRunner:
    """Wire Livne's identity, paths and cadence into the shared runner."""
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
    return TenantRunner(context, LivneStrategy())


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
        print(f"\n[runner] Stopped by user at {dt.datetime.now():%Y-%m-%d %H:%M:%S}.",
              file=sys.stderr)
