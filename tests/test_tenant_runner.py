"""The shared tenant loop: order, containment, and the seam to strategy.

These drive a FAKE strategy end to end. That is the point of the refactor being
testable at all: the loop no longer lives inside a model package that pytest
cannot import (the two models define clashing top-level `config`/`utils`
modules), so for the first time the cycle order and its failure containment can
be asserted directly instead of inferred.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.paper_trading import outbox  # noqa: E402
from core.paper_trading.catch_up import CatchUpConfig  # noqa: E402
from core.registry import ModelConfig  # noqa: E402
from core.runtime import TenantContext, TenantRunner, TenantStrategy  # noqa: E402

NOW = dt.datetime(2026, 8, 17, 12, 0, 0)


def _model(**kw) -> ModelConfig:
    base = dict(
        key="client_fake", label="Fake tenant", cycle_interval_minutes=5,
        closed_statuses=("WIN_TP1", "LOSS_SL", "EXPIRED"),
        update_statuses=(), closed_with_pnl=("WIN_TP1", "LOSS_SL"),
        telegram_route="client_fake",
    )
    base.update(kw)
    return ModelConfig(**base)


def _trade(tid="t1", status="ACTIVE", **kw) -> dict:
    base = {"id": tid, "ticker": "BTCUSDT", "asset_class": "crypto",
            "direction": "LONG", "current_status": status,
            "entry_zone": [99.0, 101.0], "stop_loss": 95.0,
            "target1": 110.0, "target2": 120.0,
            "signal_date": (NOW - dt.timedelta(hours=2)).isoformat(),
            "last_checked_at": (NOW - dt.timedelta(minutes=5)).isoformat()}
    base.update(kw)
    return base


class FakeStrategy(TenantStrategy):
    """A complete tenant in ~30 lines -- which is the claim being tested."""

    def __init__(self, trades=None, price=100.0):
        self.trades = trades if trades is not None else []
        self.price = price
        self.saves = 0
        self.sent: list[dict] = []
        self.journalled: list[str] = []
        self.candidates: list = []
        self.calls: list[str] = []
        self.send_ok = True

    # state
    def load_trades(self):
        # Return the same list object so mutations are visible, mirroring a
        # store that round-trips through JSON.
        return self.trades

    def save_trades(self, trades):
        self.saves += 1
        self.trades = trades

    def journal(self, trade):
        self.journalled.append(trade["id"])

    # lifecycle
    def apply_bar(self, trade, high, low, ts):
        self.calls.append("apply_bar")
        if trade["current_status"] == "ACTIVE" and low <= trade["stop_loss"]:
            trade["current_status"] = "LOSS_SL"
            trade["exit_price"] = trade["stop_loss"]
            trade["pnl_pct"] = -5.0
            return True
        return False

    # data
    def current_price(self, ticker, asset_class):
        return self.price

    def price_range(self, ticker, asset_class, lookback_minutes):
        self.calls.append(f"range:{lookback_minutes}")
        return {"high": self.price, "low": self.price}

    def fetch_crypto(self, ticker, interval="5m", limit=300):
        return None

    def fetch_stock(self, ticker, period="5d", interval="5m"):
        return None

    # scanning
    def find_candidates(self, asset_class):
        self.calls.append(f"find:{asset_class}")
        return list(self.candidates)

    def build_setup(self, candidate, asset_class):
        if candidate.get("reject"):
            return None, f"{candidate['ticker']}: rejected"
        return candidate, "ok"

    def candidate_ticker(self, candidate):
        return candidate["ticker"]

    def log_trade(self, setup, telegram_message_id):
        record = _trade(tid=f"new-{setup['ticker']}", status="PENDING",
                        ticker=setup["ticker"], telegram_message_id=telegram_message_id)
        self.trades.append(record)
        return record

    def format_setup_alert(self, setup):
        return f"SETUP {setup['ticker']}"


@pytest.fixture
def runner(monkeypatch, tmp_path):
    strategy = FakeStrategy()
    ctx = TenantContext(model=_model(), state_dir=str(tmp_path / "state"),
                        catch_up=CatchUpConfig(min_gap_minutes=30.0))
    r = TenantRunner(ctx, strategy)

    import core.runtime.tenant as mod
    monkeypatch.setattr(mod, "is_market_open", lambda *a, **k: True)

    def fake_send(message, route=None, strict_route=False, reply_to_message_id=None, **kw):
        strategy.sent.append({"message": message, "route": route})
        return {"ok": strategy.send_ok, "message_id": 42}

    monkeypatch.setattr(mod, "send_telegram_message", fake_send)
    monkeypatch.setattr(mod, "send_telegram_photo",
                        lambda *a, **k: pytest.fail("no chart configured; must not be called"))
    return r


# ==========================================================================
# Position refresh
# ==========================================================================
def test_a_stop_breach_transitions_and_queues_one_alert(runner):
    runner.strategy.trades = [_trade()]
    runner.strategy.price = 90.0          # through the 95 stop

    changed = runner.process_open_positions(now=NOW)

    assert [t["current_status"] for t in changed] == ["LOSS_SL"]
    assert outbox.is_pending(runner.strategy.trades[0])
    assert runner.strategy.journalled == ["t1"]


def test_alerts_are_only_sent_by_the_flush_stage(runner):
    """The refresh must never send: the transition has to be PERSISTED with its
    outbox entry first, or a failed send loses the alert forever."""
    runner.strategy.trades = [_trade()]
    runner.strategy.price = 90.0

    runner.process_open_positions(now=NOW)
    assert runner.strategy.sent == []

    runner.flush_alerts()
    assert len(runner.strategy.sent) == 1
    assert not outbox.is_pending(runner.strategy.trades[0])


def test_a_failed_send_leaves_the_alert_owed_for_the_next_cycle(runner):
    runner.strategy.trades = [_trade()]
    runner.strategy.price = 90.0
    runner.strategy.send_ok = False

    runner.process_open_positions(now=NOW)
    runner.flush_alerts()
    assert outbox.is_pending(runner.strategy.trades[0])

    runner.strategy.send_ok = True
    outcome = runner.flush_alerts()
    assert outcome["sent"] == 1
    assert not outbox.is_pending(runner.strategy.trades[0])


def test_the_store_is_not_rewritten_when_nothing_moved(runner):
    """A cycle with no open positions must not rewrite a 110 KB store."""
    runner.strategy.trades = [_trade(status="LOSS_SL")]
    runner.process_open_positions(now=NOW)
    assert runner.strategy.saves == 0


def test_an_evaluated_trade_is_stamped_even_when_it_did_not_move(runner):
    runner.strategy.trades = [_trade()]
    runner.process_open_positions(now=NOW)
    assert runner.strategy.trades[0]["last_checked_at"] == NOW.isoformat(timespec="seconds")
    assert runner.strategy.saves == 1


def test_stocks_are_not_stamped_while_the_exchange_is_shut(runner, monkeypatch):
    """Stamping a trade nobody checked would hide the gap from catch-up."""
    import core.runtime.tenant as mod
    monkeypatch.setattr(mod, "is_market_open", lambda *a, **k: False)
    stamp = (NOW - dt.timedelta(days=2)).isoformat()
    runner.strategy.trades = [_trade(asset_class="stocks", ticker="AAPL", last_checked_at=stamp)]

    runner.process_open_positions(now=NOW)

    assert runner.strategy.trades[0]["last_checked_at"] == stamp


def test_the_lookback_window_follows_elapsed_time(runner):
    runner.strategy.trades = [_trade(last_checked_at=(NOW - dt.timedelta(minutes=9)).isoformat())]
    runner.process_open_positions(now=NOW)
    assert "range:9" in runner.strategy.calls


def test_the_pre_bar_hook_can_close_before_the_range_test(runner):
    """Livne's emergency exit. When it fires, the bar test must be skipped --
    otherwise a position could be closed twice in one pass."""
    class Emergency(FakeStrategy):
        def pre_bar(self, trade, price, now):
            trade["current_status"] = "EMERGENCY_EXIT"
            trade["pnl_pct"] = -1.0
            return True

    runner.strategy = Emergency(trades=[_trade()])
    runner.strategy.name = "Fake tenant"

    changed = runner.process_open_positions(now=NOW)

    assert [t["current_status"] for t in changed] == ["EMERGENCY_EXIT"]
    assert "apply_bar" not in runner.strategy.calls


# ==========================================================================
# Cycle order and containment
# ==========================================================================
def test_positions_are_managed_before_the_scan(runner, monkeypatch):
    """Opening new exposure matters less than managing what is already open,
    and the scan is the slow half."""
    order = []
    monkeypatch.setattr(runner, "process_catch_up", lambda *a, **k: order.append("catch_up"))
    monkeypatch.setattr(runner, "process_open_positions", lambda *a, **k: order.append("refresh"))
    monkeypatch.setattr(runner, "flush_alerts", lambda *a, **k: order.append("flush"))
    monkeypatch.setattr(runner, "send_scheduled_summaries", lambda *a, **k: order.append("summary"))
    monkeypatch.setattr(runner, "scan", lambda *a, **k: order.append("scan") or [])

    runner.run_cycle(runner.parse_args(["--once"]))

    assert order == ["catch_up", "refresh", "flush", "summary", "scan"]


def test_skip_scan_still_manages_positions(runner, monkeypatch):
    order = []
    monkeypatch.setattr(runner, "process_catch_up", lambda *a, **k: order.append("catch_up"))
    monkeypatch.setattr(runner, "process_open_positions", lambda *a, **k: order.append("refresh"))
    monkeypatch.setattr(runner, "flush_alerts", lambda *a, **k: order.append("flush"))
    monkeypatch.setattr(runner, "send_scheduled_summaries", lambda *a, **k: order.append("summary"))
    monkeypatch.setattr(runner, "scan", lambda *a, **k: pytest.fail("must not scan"))

    runner.run_cycle(runner.parse_args(["--once", "--skip-scan"]))

    assert order == ["catch_up", "refresh", "flush", "summary"]


def test_skip_positions_runs_only_the_scan(runner, monkeypatch):
    monkeypatch.setattr(runner, "process_open_positions",
                        lambda *a, **k: pytest.fail("must not refresh"))
    monkeypatch.setattr(runner, "scan", lambda *a, **k: [])
    runner.run_cycle(runner.parse_args(["--once", "--skip-positions"]))


def test_a_failing_screener_does_not_stop_the_other_asset_class(runner):
    class Broken(FakeStrategy):
        def find_candidates(self, asset_class):
            if asset_class == "stocks":
                raise RuntimeError("screener exploded")
            return [{"ticker": "BTCUSDT"}]

    runner.strategy = Broken()
    runner.strategy.name = "Fake tenant"

    reports = runner.scan("both", top_n=2, candidate_pool=6)

    assert reports == ["SETUP BTCUSDT"]


def test_one_bad_symbol_does_not_abort_the_scan(runner):
    class Flaky(FakeStrategy):
        def build_setup(self, candidate, asset_class):
            if candidate["ticker"] == "BAD":
                raise ValueError("indicator blew up")
            return candidate, "ok"

    runner.strategy = Flaky()
    runner.strategy.name = "Fake tenant"
    runner.strategy.candidates = [{"ticker": "BAD"}, {"ticker": "GOOD"}]

    assert runner.scan("crypto", top_n=2, candidate_pool=6) == ["SETUP GOOD"]


def test_dedup_compares_asset_class_not_just_ticker(runner):
    """The generated client runner used to key on ticker alone, so one symbol
    traded on two venues deduplicated against itself."""
    runner.strategy.trades = [_trade(ticker="BTCUSDT", asset_class="stocks")]
    runner.strategy.candidates = [{"ticker": "BTCUSDT"}]

    assert runner.scan("crypto", top_n=1, candidate_pool=6) == ["SETUP BTCUSDT"]


def test_an_open_position_in_the_same_class_is_deduplicated(runner):
    runner.strategy.trades = [_trade(ticker="BTCUSDT", asset_class="crypto")]
    runner.strategy.candidates = [{"ticker": "BTCUSDT"}]

    assert runner.scan("crypto", top_n=1, candidate_pool=6) == []


def test_top_n_caps_dispatch_but_rejections_do_not_count(runner):
    runner.strategy.candidates = [
        {"ticker": "A", "reject": True}, {"ticker": "B"}, {"ticker": "C"}, {"ticker": "D"}]

    assert runner.scan("crypto", top_n=2, candidate_pool=6) == ["SETUP B", "SETUP C"]


def test_dry_run_sends_nothing_and_logs_nothing(runner):
    runner.strategy.candidates = [{"ticker": "BTCUSDT"}]

    reports = runner.scan("crypto", top_n=1, candidate_pool=6, dry_run=True)

    assert reports == ["SETUP BTCUSDT"]
    assert runner.strategy.sent == []
    assert runner.strategy.trades == []


# ==========================================================================
# Locking and the process contract
# ==========================================================================
def test_a_second_instance_refuses_to_start(runner, monkeypatch):
    from core.locking import TenantLock

    held = TenantLock(runner.ctx.state_dir, runner.ctx.key)
    held.acquire()
    try:
        monkeypatch.setattr(runner, "run_cycle", lambda args: pytest.fail("must not run"))
        assert runner.main(["--once"]) == 1
    finally:
        held.release()


def test_a_disabled_model_refuses_to_run(tmp_path, monkeypatch):
    strategy = FakeStrategy()
    ctx = TenantContext(model=_model(enabled=False), state_dir=str(tmp_path))
    r = TenantRunner(ctx, strategy)
    monkeypatch.setattr(r, "run_cycle", lambda args: pytest.fail("a paused model must not run"))

    assert r.main(["--once"]) == 0


def test_force_overrides_the_pause_but_not_the_lock(tmp_path, monkeypatch):
    from core.locking import TenantLock

    strategy = FakeStrategy()
    ctx = TenantContext(model=_model(enabled=False), state_dir=str(tmp_path))
    r = TenantRunner(ctx, strategy)
    ran = []
    monkeypatch.setattr(r, "run_cycle", lambda args: ran.append(True))

    assert r.main(["--once", "--force"]) == 0
    assert ran == [True]

    held = TenantLock(ctx.state_dir, ctx.key)
    held.acquire()
    try:
        assert r.main(["--once", "--force"]) == 1
    finally:
        held.release()


def test_a_crashing_cycle_exits_non_zero_for_the_supervisor(tmp_path, monkeypatch):
    """The supervisor records a non-zero child as an `error` heartbeat, which
    is what feeds the dead-man escalation."""
    strategy = FakeStrategy()
    ctx = TenantContext(model=_model(), state_dir=str(tmp_path))
    r = TenantRunner(ctx, strategy)

    def explode(args):
        raise RuntimeError("cycle exploded")

    monkeypatch.setattr(r, "run_cycle", explode)
    assert r.main(["--once"]) == 1


def test_the_cli_accepts_every_flag_the_supervisor_sends(runner):
    """`run_supervisor._child_args` builds these; a tenant that rejected one
    would fail to launch with a usage error and look like a crash."""
    args = runner.parse_args(
        ["--once", "--asset-class", "crypto", "--top-n", "3",
         "--candidate-pool", "9", "--skip-scan"])
    assert (args.once, args.asset_class, args.top_n, args.candidate_pool, args.skip_scan) \
        == (True, "crypto", 3, 9, True)


def test_defaults_come_from_the_tenant_context(tmp_path):
    ctx = TenantContext(model=_model(), state_dir=str(tmp_path),
                        asset_class="crypto", top_n=7, candidate_pool=11)
    args = TenantRunner(ctx, FakeStrategy()).parse_args([])
    assert (args.asset_class, args.top_n, args.candidate_pool) == ("crypto", 7, 11)


# ==========================================================================
# Summaries
# ==========================================================================
def test_summaries_are_skipped_when_no_state_store_is_configured(runner):
    assert runner.send_scheduled_summaries(now=NOW) == []


def test_the_daily_summary_fires_once_per_day(tmp_path, monkeypatch):
    strategy = FakeStrategy(trades=[_trade(status="LOSS_SL", exit_date=NOW.isoformat())])
    ctx = TenantContext(model=_model(), state_dir=str(tmp_path),
                        report_state_store=str(tmp_path / "report_state.json"),
                        daily_report_hour=21)
    r = TenantRunner(ctx, strategy)

    import core.runtime.tenant as mod
    monkeypatch.setattr(mod, "send_telegram_message",
                        lambda message, **kw: strategy.sent.append(message) or {"ok": True, "message_id": 1})

    evening = NOW.replace(hour=21, minute=30)
    assert r.send_scheduled_summaries(now=evening) == ["daily"]
    assert r.send_scheduled_summaries(now=evening.replace(minute=45)) == []

    state = json.load(open(ctx.report_state_store, encoding="utf-8"))
    assert state["last_daily_report_date"] == evening.date().isoformat()


def test_a_summary_failure_never_breaks_the_cycle(tmp_path, monkeypatch):
    strategy = FakeStrategy()
    ctx = TenantContext(model=_model(), state_dir=str(tmp_path),
                        report_state_store=str(tmp_path / "report_state.json"))
    r = TenantRunner(ctx, strategy)

    import core.runtime.tenant as mod
    monkeypatch.setattr(mod, "load_report_state",
                        lambda path: (_ for _ in ()).throw(OSError("locked")))

    assert r.send_scheduled_summaries(now=NOW.replace(hour=21, minute=30)) == []
