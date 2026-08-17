"""The execution loop every tenant runs, and the surface a tenant implements.

WHY THIS EXISTS
---------------
A model used to BE a copy of the loop. `models/livne/live_runner.py` and
`models/biton/live_runner.py` shared 390 of 432 lines; their `main.py` files
were byte-identical; `scripts/create_client_model.py` emitted a third copy as a
template string. So every fix to catch-up, deduplication, alerting, locking or
reporting had to be applied N times -- and where it was not, the copies drifted:
the generated client runner had no journal, no scheduled summaries, no funnel
diagnostics, and deduplicated on ticker alone while both first-party models
also compared asset class.

That is the wrong axis of variation. Tenants differ in their STRATEGY. They do
not differ in "load positions, mark them to market, alert on transitions, scan,
dispatch" -- and pretending they might is what produced three subtly different
answers to the same question.

THE SPLIT
---------
    TenantRunner     ORDER and SAFETY. Owns the one-instance lock, the cycle
                     sequence, error containment, the loop/--once distinction
                     and the CLI. Contains no trading threshold and decides
                     nothing about a market.

    TenantStrategy   MEANING. Supplies the model's lifecycle transition, its
                     candidates, its setups, its message text. Every method
                     that a model does not need has a working default, so a
                     generated tenant implements four of them.

The seam is deliberately drawn where the two live models genuinely disagree.
Livne banks a partial at TP1 and moves its stop to breakeven; Biton closes
outright. A shared state machine would have to know about both, which is the
exact conflation `core.registry` records as having corrupted every downstream
metric once already. So `apply_bar` stays with the model, and the runner never
looks inside a trade to decide anything.

CYCLE ORDER IS LOAD-BEARING
---------------------------
    1. catch-up      resolve what was missed while stopped, at the level and
                     minute it actually happened, BEFORE anything looks at the
                     current price.
    2. refresh       mark open positions to market.
    3. flush         deliver everything owed, including alerts an earlier cycle
                     failed to send.
    4. summaries     after position management, so a close that happened this
                     cycle is already in the numbers.
    5. scan          last: opening new exposure matters less than managing what
                     is already open, and a scan is the slow half.

Each stage is wrapped independently. A failing scan must not cost a position
update, and a failing summary must not cost either.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
import time
import traceback
from dataclasses import dataclass, field

from core.locking import TenantBusy, TenantLock
from core.paper_trading import outbox
from core.paper_trading.catch_up import (
    CatchUpConfig,
    CatchUpReport,
    OPEN_STATUSES,
    format_catch_up_summary,
    run_catch_up,
)
from core.paper_trading.refresh import RefreshPolicy, refresh_positions
from core.registry import ModelConfig
from core.reporting.summary import (
    build_daily_summary,
    build_weekly_summary,
    daily_report_due,
    load_report_state,
    save_report_state,
    weekly_report_due,
)
from core.utils.market_hours import is_market_open
from core.utils.telegram_notifier import (
    escape_markdown,
    send_telegram_message,
    send_telegram_photo,
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


@dataclass(frozen=True)
class TenantContext:
    """Everything the loop needs to know that is not strategy.

    Built from the model's `ModelConfig` plus its own `config.py`, so a tenant
    declares these once instead of the runner reaching into a module it must
    not import.
    """

    model: ModelConfig
    state_dir: str
    catch_up: CatchUpConfig = field(default_factory=CatchUpConfig)

    # --- scan defaults (overridable per invocation) -----------------------
    asset_class: str = "both"
    top_n: int = 2
    candidate_pool: int = 6

    # --- scheduled summaries ---------------------------------------------
    # None disables them entirely, which is what a tenant with no reporting
    # story (a fresh scaffold) wants.
    report_state_store: str | None = None
    daily_report_hour: int = 21
    weekly_report_weekday: int = 4
    send_empty_daily_report: bool = True

    # --- alerting ---------------------------------------------------------
    telegram_strict: bool = False

    @property
    def key(self) -> str:
        return self.model.key

    @property
    def name(self) -> str:
        return self.model.label

    @property
    def route(self) -> str:
        return self.model.telegram_route

    @property
    def cycle_interval_minutes(self) -> float:
        return self.model.cycle_interval_minutes


class TenantStrategy:
    """What a model must supply. Subclass and override.

    Only four methods have no useful default:

        load_trades / save_trades   this model's store
        apply_bar                   this model's lifecycle
        find_candidates             this model's screener
        build_setup                 this model's entry rules

    Everything else -- journalling, charts, funnel diagnostics, the emergency
    pre-bar hook, message text -- has a default that either does the obvious
    thing or does nothing, so a tenant pays only for what it uses.
    """

    # --- state ------------------------------------------------------------
    def load_trades(self) -> list[dict]:
        raise NotImplementedError

    def save_trades(self, trades: list[dict]) -> None:
        raise NotImplementedError

    def journal(self, trade: dict) -> None:
        """Mirror a trade into a spreadsheet/ledger. Optional by design: it is
        a convenience view, and it must never be able to cost an alert."""

    # --- lifecycle --------------------------------------------------------
    def apply_bar(self, trade: dict, high: float, low: float, ts: dt.datetime) -> bool:
        raise NotImplementedError

    @property
    def open_statuses(self) -> frozenset[str]:
        """This model's non-terminal statuses. The cross-model superset by
        default; narrow it if the model's vocabulary is smaller."""
        return OPEN_STATUSES

    def pre_bar(self, trade: dict, price: float, now: dt.datetime) -> bool:
        """A live-only close that runs BEFORE the range test (Livne's volume
        reversal). Return True if it closed the position. Default: no such
        signal, which is why the default is False rather than a hook a tenant
        must remember to stub."""
        return False

    # --- market data ------------------------------------------------------
    def current_price(self, ticker: str, asset_class: str) -> float | None:
        raise NotImplementedError

    def price_range(self, ticker: str, asset_class: str, lookback_minutes: int) -> dict | None:
        """High/low traded over the window. None degrades to the last price."""
        return None

    def fetch_crypto(self, ticker: str, interval: str = "5m", limit: int = 300):
        raise NotImplementedError

    def fetch_stock(self, ticker: str, period: str = "5d", interval: str = "5m"):
        raise NotImplementedError

    # --- scanning ---------------------------------------------------------
    def find_candidates(self, asset_class: str) -> list:
        """Ranked candidates for this asset class. The runner does not know or
        care what a candidate is; it only passes them to `build_setup`."""
        raise NotImplementedError

    def build_setup(self, candidate, asset_class: str):
        """`(setup, reason)`. `setup` is None when nothing qualifies, and
        `reason` is logged -- which is what makes a quiet strategy debuggable
        rather than mysterious."""
        raise NotImplementedError

    def candidate_ticker(self, candidate) -> str:
        """How to read a ticker off whatever `find_candidates` returned."""
        return getattr(candidate, "ticker", None) or candidate["ticker"]

    def has_open_trade(self, ticker: str, asset_class: str, trades: list[dict]) -> bool:
        """Deduplication. Compares asset class as well as ticker by default --
        the generated client runner used to compare only the ticker, so one
        symbol traded on two venues deduplicated against itself."""
        return any(
            t.get("ticker") == ticker
            and t.get("asset_class") == asset_class
            and t.get("current_status") in self.open_statuses
            for t in trades
        )

    def log_trade(self, setup, telegram_message_id: int | None) -> dict:
        raise NotImplementedError

    def chart_for(self, setup) -> str | None:
        """Rendered chart path, or None for a text-only alert."""
        return None

    def export_diagnostics(self, asset_class: str, records) -> None:
        """Optional per-cycle funnel export. Read-only and independent of the
        scan by contract."""

    def trace_diagnostics(self, asset_class: str):
        """Optional per-ticker gate trace, returned to `export_diagnostics`."""
        return None

    def diagnostics_due(self) -> bool:
        """Whether to run the tracer this cycle. Gated because it re-runs the
        whole gate chain for every watchlist ticker."""
        return False

    # --- presentation -----------------------------------------------------
    def format_status_update(self, trade: dict) -> str:
        """The message for one status transition. Overridden by every real
        model; the default is deliberately plain but complete."""
        lines = [
            f"[{escape_markdown(self.name)}] {escape_markdown(trade['ticker'])} "
            f"-> {escape_markdown(trade['current_status'])}",
        ]
        if trade.get("exit_price") is not None:
            lines.append(f"Exit price: `{trade['exit_price']:.4g}`")
        if trade.get("pnl_pct") is not None:
            lines.append(f"P&L: *{trade['pnl_pct']:+.2f}%*")
        return "\n".join(lines)

    def format_setup_alert(self, setup) -> str:
        raise NotImplementedError

    # Set by the runner so the defaults above can name the model.
    name: str = "tenant"


class TenantRunner:
    """Runs one tenant: lock, cycle, loop, CLI."""

    def __init__(self, context: TenantContext, strategy: TenantStrategy) -> None:
        self.ctx = context
        self.strategy = strategy
        strategy.name = context.name

    # ======================================================================
    # Stage 1 -- catch-up
    # ======================================================================
    def process_catch_up(self, now: dt.datetime | None = None) -> CatchUpReport:
        """Replay what was missed while stopped, reported as ONE message.

        Deliberately not per-trade alerts: a runner that was down overnight can
        come back to a dozen resolutions, and a dozen delayed "EXIT ALERT"
        messages would read as if they were all happening now -- actively
        misleading in a channel people trade from.

        Costs nothing at steady state: every open trade was checked one cycle
        ago, so the gap check short-circuits before any historical fetch.
        """
        trades = self.strategy.load_trades()
        stocks_open = is_market_open()

        def tradeable(trade: dict) -> bool:
            # Mirrors the live loop: stocks are not evaluated outside RTH, so
            # the replay must not resolve them on bars the live system would
            # never have acted on either.
            return not (trade.get("asset_class") == "stocks" and not stocks_open)

        try:
            report = run_catch_up(
                trades, self.strategy.apply_bar,
                fetch_crypto=self.strategy.fetch_crypto,
                fetch_stock=self.strategy.fetch_stock,
                config=self.ctx.catch_up, now=now, is_tradeable=tradeable,
            )
        except Exception as exc:  # noqa: BLE001 -- catch-up must never break a cycle
            _log(f"[catch-up] pass failed (live positions unaffected): {exc}")
            return CatchUpReport()

        if not report.events:
            return report

        self.strategy.save_trades(trades)
        by_id = {t.get("id"): t for t in trades}
        for event in report.events:
            record = by_id.get(event.trade_id)
            if record is not None:
                self._journal_safe(record)

        _log(f"[catch-up] {len(report.events)} transition(s) across "
             f"{report.trades_replayed} position(s), {report.bars_replayed} bars replayed")
        message = format_catch_up_summary(self.ctx.name, report)
        if message:
            result = self._send(message)
            _log(f"[catch-up] consolidated summary "
                 f"(telegram {'sent' if result['ok'] else 'FAILED'})")
        return report

    # ======================================================================
    # Stage 2/3 -- refresh, then deliver what is owed
    # ======================================================================
    def process_open_positions(self, now: dt.datetime | None = None) -> list[dict]:
        trades = self.strategy.load_trades()
        stocks_open = is_market_open()
        if not stocks_open:
            _log("[market] US equities market is CLOSED — skipping stock position "
                 "refresh this cycle (crypto still evaluated).")

        policy = RefreshPolicy(
            cycle_interval_minutes=self.ctx.cycle_interval_minutes,
            max_lookback_minutes=self.ctx.catch_up.min_gap_minutes,
            open_statuses=self.strategy.open_statuses,
        )
        result = refresh_positions(
            trades, policy=policy,
            apply_bar=self.strategy.apply_bar,
            current_price=self.strategy.current_price,
            price_range=self.strategy.price_range,
            stocks_market_open=stocks_open,
            pre_bar=self.strategy.pre_bar,
            now=now,
        )

        if result.touched:
            self.strategy.save_trades(trades)
        for trade in result.changed:
            self._journal_safe(trade)
        return result.changed

    def flush_alerts(self) -> dict:
        """Deliver everything this tenant owes, oldest first.

        Covers BOTH the transitions just queued and anything left over from an
        earlier cycle whose send failed -- one code path, so a retried alert
        cannot behave differently from a fresh one.
        """
        trades = self.strategy.load_trades()
        outcome = outbox.flush(trades, self._send_status_update)
        if outcome["attempted"]:
            self.strategy.save_trades(trades)
            _log(f"[outbox] {outcome['sent']}/{outcome['attempted']} alert(s) delivered"
                 + (f", {outcome['failed']} still queued" if outcome["failed"] else ""))
        if outcome["abandoned"]:
            _log(f"[outbox] WARNING: {outcome['abandoned']} alert(s) abandoned after repeated "
                 f"failures -- kept on their trade records as `pending_alert.abandoned`.")
        return outcome

    def _send_status_update(self, trade: dict) -> bool:
        result = self._send(
            self.strategy.format_status_update(trade),
            reply_to_message_id=trade.get("telegram_message_id"),
        )
        _log(f"[paper] {trade['ticker']} -> {trade['current_status']} "
             f"(telegram {'sent' if result['ok'] else 'FAILED -- stays queued'})")
        return bool(result["ok"])

    # ======================================================================
    # Stage 4 -- scheduled summaries
    # ======================================================================
    def send_scheduled_summaries(self, now: dt.datetime | None = None) -> list[str]:
        """Daily (and, on the configured weekday, weekly) summary if due.

        Guarded by a per-model date marker, because the supervisor launches a
        fresh `--once` process per tick: an in-memory flag would reset every
        cycle and re-send for the rest of the evening. Wrapped so a locked
        state file or a Telegram outage can never interrupt position
        management.
        """
        if not self.ctx.report_state_store:
            return []

        now = now or dt.datetime.now()
        sent: list[str] = []
        try:
            state = load_report_state(self.ctx.report_state_store)
            trades = self.strategy.load_trades()
            model = self.ctx.model

            if daily_report_due(state, now, self.ctx.daily_report_hour):
                message = build_daily_summary(
                    self.ctx.name, trades, now,
                    closed_statuses=model.closed_statuses,
                    update_statuses=model.update_statuses,
                )
                has_activity = "_none_" not in message or message.count("_none_") < 2
                if has_activity or self.ctx.send_empty_daily_report:
                    self._send(message)
                    sent.append("daily")
                # Marked done either way, so a quiet day is not retried every
                # cycle for the rest of the evening.
                state["last_daily_report_date"] = now.date().isoformat()

            if weekly_report_due(state, now, self.ctx.daily_report_hour,
                                 self.ctx.weekly_report_weekday):
                self._send(build_weekly_summary(
                    self.ctx.name, trades, now, closed_statuses=model.closed_statuses,
                ))
                state["last_weekly_report_date"] = now.date().isoformat()
                sent.append("weekly")

            if sent:
                save_report_state(self.ctx.report_state_store, state)
                _log(f"[report] Sent {'/'.join(sent)} summary for {self.ctx.name}.")
            elif state.get("last_daily_report_date") == now.date().isoformat():
                save_report_state(self.ctx.report_state_store, state)
        except Exception as exc:  # noqa: BLE001
            _log(f"[report] Summary dispatch failed (scan/positions unaffected): {exc}")
        return sent

    # ======================================================================
    # Stage 5 -- scan
    # ======================================================================
    def scan(self, asset_class: str, top_n: int, candidate_pool: int,
             dry_run: bool = False) -> list[str]:
        """Find and dispatch new setups. Returns the alert texts produced.

        With `dry_run=True` nothing is sent, logged or rendered -- no
        deduplication either, so a dry run reports what the strategy sees
        rather than what is left after existing positions are excluded.
        """
        classes = ["stocks", "crypto"] if asset_class == "both" else [asset_class]
        reports: list[str] = []
        rejections: list[str] = []
        traces: dict[str, object] = {}
        diagnostics_due = self.strategy.diagnostics_due() if not dry_run else False

        for cls in classes:
            if cls == "stocks" and not is_market_open():
                _log("[market] US equities market is CLOSED — skipping stocks scan "
                     "(crypto continues 24/7).")
                continue

            try:
                candidates = self.strategy.find_candidates(cls)
            except Exception as exc:  # noqa: BLE001 -- one class must not kill the other
                _log(f"[screener] {cls} scan failed ({type(exc).__name__}: {exc}); "
                     f"other asset classes unaffected.")
                continue
            _log(f"[screener] {len(candidates)} {cls} candidate(s) passed the screener")

            if diagnostics_due:
                try:
                    trace = self.strategy.trace_diagnostics(cls)
                    if trace is not None:
                        traces[cls] = trace
                except Exception as exc:  # noqa: BLE001
                    _log(f"[funnel] {cls} diagnostic trace failed "
                         f"(scan/alerts unaffected): {exc}")

            # One snapshot per asset class rather than a store read per
            # candidate: a watchlist has no duplicate tickers, so the answer
            # cannot change mid-pass.
            open_trades = [] if dry_run else self.strategy.load_trades()

            accepted = 0
            for candidate in candidates[:candidate_pool]:
                if accepted >= top_n:
                    break
                ticker = self.strategy.candidate_ticker(candidate)

                if not dry_run and self.strategy.has_open_trade(ticker, cls, open_trades):
                    _log(f"[dedup] {ticker} already has an open paper trade — "
                         f"skipping duplicate alert")
                    continue

                try:
                    setup, reason = self.strategy.build_setup(candidate, cls)
                except Exception as exc:  # noqa: BLE001 -- one bad symbol is not a failed scan
                    _log(f"[setup] {ticker} failed ({type(exc).__name__}: {exc}); skipped.")
                    continue
                if setup is None:
                    rejections.append(reason)
                    continue

                message = self.strategy.format_setup_alert(setup)
                if not dry_run:
                    self._dispatch_setup(setup, message)
                reports.append(message)
                accepted += 1

            if accepted == 0:
                _log(f"[result] No qualifying {cls} setups this cycle.")

        if rejections:
            _log("\n[filtered out]")
            for reason in rejections:
                _log(f"  - {reason}")

        if traces:
            try:
                self.strategy.export_diagnostics(asset_class, traces)
            except Exception as exc:  # noqa: BLE001
                _log(f"[funnel] diagnostic export failed (scan/alerts unaffected): {exc}")

        return reports

    def _dispatch_setup(self, setup, message: str) -> None:
        """Chart + alert + log, in the order that keeps the message id."""
        chart_path = None
        try:
            chart_path = self.strategy.chart_for(setup)
        except Exception as exc:  # noqa: BLE001 -- a chart must never cost an alert
            _log(f"[chart] render failed ({type(exc).__name__}: {exc}); text-only alert.")

        if chart_path:
            _log(f"[chart] Rendered {chart_path}")
            result = send_telegram_photo(chart_path, caption=message, route=self.ctx.route,
                                         strict_route=self.ctx.telegram_strict)
            if not result["ok"]:
                _log("[chart] Photo alert failed — falling back to text-only.")
                result = self._send(message)
        else:
            result = self._send(message)

        record = self.strategy.log_trade(setup, result.get("message_id"))
        _log(f"[paper] Logged trade {record.get('id')} for "
             f"{record.get('ticker')} (telegram {'sent' if result['ok'] else 'FAILED'})")

    # ======================================================================
    # Cycle
    # ======================================================================
    def run_cycle(self, args) -> None:
        """One full cycle. Stage order and containment are the whole point --
        see the module docstring."""
        if not getattr(args, "skip_positions", False):
            self.process_catch_up()
            _log(f"[{self.ctx.name}] Refreshing open paper trade positions...")
            self.process_open_positions()
            self.flush_alerts()
            self.send_scheduled_summaries()

        if getattr(args, "skip_scan", False):
            return

        reports = self.scan(args.asset_class, args.top_n, args.candidate_pool,
                            dry_run=getattr(args, "dry_run", False))
        if not reports:
            print("No high-conviction setups found across the scanned watchlist.")
            return
        for report in reports:
            print(report)
            print("\n" + "=" * 60 + "\n")

    # ======================================================================
    # Process entry point
    # ======================================================================
    def main(self, argv: list[str] | None = None) -> int:
        args = self.parse_args(argv)

        # A deactivated model must not trade or alert even when invoked
        # directly: a stale Task Scheduler entry must not quietly keep running
        # something someone deliberately paused.
        if not self.ctx.model.enabled and not args.force:
            _log(f"[runner] {self.ctx.name} is DEACTIVATED in core/registry.py -- refusing to run.")
            _log("[runner] Stored state is untouched. Set enabled=True to resume, "
                 "or pass --force for a one-off run.")
            return 0

        # ONE INSTANCE PER MODEL. Everything below read-modify-writes this
        # model's store, and an atomic save prevents a torn file, not a lost
        # update. `--force` overrides the pause flag, never this.
        lock = TenantLock(self.ctx.state_dir, self.ctx.key)
        try:
            lock.acquire()
        except TenantBusy as exc:
            _log(f"[runner] {exc}")
            _log("[runner] Nothing was read or written. If that PID is gone, the OS has "
                 "already released the lock and the next start will succeed.")
            return 1

        _log(f"[runner] {self.ctx.name} [{self.ctx.model.stage}] route={self.ctx.route}"
             f"{' (dedicated)' if self.ctx.telegram_strict else ''}")
        try:
            return self._run_until_stopped(args)
        finally:
            lock.release()

    def _run_until_stopped(self, args) -> int:
        if args.once:
            try:
                self.run_cycle(args)
            except Exception as exc:  # noqa: BLE001
                _log(f"[runner] Cycle failed at {_timestamp()}: {exc}")
                traceback.print_exc(file=sys.stderr)
                return 1
            return 0

        interval = self.ctx.cycle_interval_minutes
        _log(f"[runner] Starting continuous execution loop (every {interval} min). "
             f"Press Ctrl+C to stop.")
        cycle = 0
        while True:
            cycle += 1
            _log(f"\n{'=' * 60}")
            _log(f"[cycle #{cycle}] START — {_timestamp()}")
            _log("=" * 60)
            try:
                self.run_cycle(args)
            except Exception as exc:  # noqa: BLE001
                _log(f"[cycle #{cycle}] ERROR at {_timestamp()}: {exc}")
                traceback.print_exc(file=sys.stderr)
                _log(f"[cycle #{cycle}] Continuing to next cycle despite error.")

            finished = dt.datetime.now()
            _log(f"[cycle #{cycle}] DONE — {finished:%Y-%m-%d %H:%M:%S}")
            _log(f"[runner] Sleeping {interval} min. Next cycle at "
                 f"{finished + dt.timedelta(minutes=interval):%Y-%m-%d %H:%M:%S}.")
            try:
                time.sleep(interval * 60)
            except KeyboardInterrupt:
                _log(f"\n[runner] Stopped by user at {_timestamp()}.")
                break
        return 0

    def parse_args(self, argv: list[str] | None = None):
        """The CLI every tenant exposes.

        The flag set is part of the platform contract, not a per-model choice:
        the supervisor builds a command line from `run_supervisor._child_args`
        and any tenant that did not accept one of these would fail to launch.
        """
        ap = argparse.ArgumentParser(description=f"{self.ctx.name} live runner")
        ap.add_argument("--asset-class", choices=["stocks", "crypto", "both"],
                        default=self.ctx.asset_class,
                        help="which market(s) to scan (default: %(default)s)")
        ap.add_argument("--top-n", type=int, default=self.ctx.top_n,
                        help="max setups per asset class (default: %(default)s)")
        ap.add_argument("--candidate-pool", type=int, default=self.ctx.candidate_pool,
                        help="how many top-scored candidates to evaluate (default: %(default)s)")
        ap.add_argument("--skip-scan", action="store_true",
                        help="refresh open positions only, no new-signal scan")
        ap.add_argument("--skip-positions", action="store_true",
                        help="run the scan only, no position refresh")
        ap.add_argument("--dry-run", action="store_true",
                        help="scan and print, but send nothing and log nothing")
        ap.add_argument("--once", action="store_true",
                        help="run a single cycle and exit instead of looping")
        ap.add_argument("--force", action="store_true",
                        help="run even if this model is marked enabled=False in core/registry.py")
        return ap.parse_args(argv)

    # ======================================================================
    # helpers
    # ======================================================================
    def _send(self, message: str, reply_to_message_id: int | None = None) -> dict:
        return send_telegram_message(
            message, route=self.ctx.route, strict_route=self.ctx.telegram_strict,
            reply_to_message_id=reply_to_message_id,
        )

    def _journal_safe(self, trade: dict) -> bool:
        """A journal write must never break a cycle -- the store and the alert
        are what matter, and neither may be lost because a spreadsheet was open
        in Excel."""
        try:
            self.strategy.journal(trade)
            return True
        except Exception as exc:  # noqa: BLE001
            _log(f"[journal] could not journal {trade.get('ticker')} ({trade.get('id')}): "
                 f"{type(exc).__name__}: {exc}. Trade store and alerts unaffected.")
            return False


def _timestamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
