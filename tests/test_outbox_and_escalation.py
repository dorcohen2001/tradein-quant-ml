"""Alerts that survive a failed send, and tenants that announce their own death.

Both close the same class of hole: something went wrong and NOBODY WAS TOLD.
The outbox covers a notification the transport dropped; escalation covers a
tenant that stopped running at all. In each case the pre-existing behaviour was
not an error message -- it was silence, which is indistinguishable from a quiet
market.
"""
from __future__ import annotations

import datetime as dt
import importlib
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.observability import read_heartbeat  # noqa: E402
from core.paper_trading import outbox  # noqa: E402
from core.registry import ModelConfig  # noqa: E402

NOW = dt.datetime(2026, 8, 15, 12, 0, 0)


def _trade(tid="t1", status="LOSS_SL", **kw) -> dict:
    base = {"id": tid, "ticker": "BTCUSDT", "asset_class": "crypto",
            "direction": "LONG", "current_status": status}
    base.update(kw)
    return base


# ==========================================================================
# Outbox
# ==========================================================================
def test_queue_records_the_status_that_must_be_announced():
    trade = _trade()
    entry = outbox.queue(trade, now=NOW)

    assert entry["status"] == "LOSS_SL"
    assert entry["queued_at"] == NOW.isoformat(timespec="seconds")
    assert outbox.is_pending(trade)


def test_a_successful_send_clears_the_entry():
    trade = _trade()
    outbox.queue(trade, now=NOW)

    result = outbox.flush([trade], send=lambda t: True)

    assert result == {"attempted": 1, "sent": 1, "failed": 0, "abandoned": 0}
    assert outbox.FIELD not in trade


def test_a_failed_send_keeps_the_alert_owed():
    """THE regression this exists for. The transition is already persisted, so
    without the entry the alert would simply never be mentioned again."""
    trade = _trade()
    outbox.queue(trade, now=NOW)

    result = outbox.flush([trade], send=lambda t: False)

    assert result["failed"] == 1
    assert outbox.is_pending(trade)
    assert trade[outbox.FIELD]["attempts"] == 1


def test_a_failed_alert_is_retried_on_the_next_pass():
    trade = _trade()
    outbox.queue(trade, now=NOW)
    outbox.flush([trade], send=lambda t: False)

    delivered = []
    outbox.flush([trade], send=lambda t: delivered.append(t["id"]) or True)

    assert delivered == ["t1"]
    assert outbox.FIELD not in trade


def test_a_raising_transport_is_a_failure_not_a_crash():
    """A bug in message formatting must not abort position management."""
    trade = _trade()
    outbox.queue(trade, now=NOW)

    def explode(_t):
        raise ValueError("bad format string")

    result = outbox.flush([trade], send=explode)

    assert result["failed"] == 1
    assert "ValueError" in trade[outbox.FIELD]["last_error"]


def test_an_undeliverable_alert_is_abandoned_rather_than_retried_forever():
    """A 400 "can't parse entities" fails identically forever. Retrying it every
    two minutes turns one lost alert into permanent log noise."""
    trade = _trade()
    outbox.queue(trade, now=NOW)

    for _ in range(outbox.MAX_ATTEMPTS):
        outbox.flush([trade], send=lambda t: False)

    assert trade[outbox.FIELD]["abandoned"] is True
    assert not outbox.is_pending(trade)
    assert outbox.flush([trade], send=lambda t: True)["attempted"] == 0


def test_an_abandoned_entry_is_kept_as_evidence():
    """"This alert was never delivered" is exactly what an operator needs to
    know; deleting the entry would erase it."""
    trade = _trade()
    outbox.queue(trade, now=NOW)
    for _ in range(outbox.MAX_ATTEMPTS):
        outbox.flush([trade], send=lambda t: False)

    assert trade[outbox.FIELD]["last_error"]
    assert trade[outbox.FIELD]["abandoned_at"]


def test_requeueing_announces_where_the_position_ENDED_UP():
    """A position that moved ACTIVE -> LOSS_SL during an outage should announce
    the close, not narrate every intermediate step late. The original queue time
    is kept, so the backlog still delivers in the order things happened."""
    trade = _trade(status="ACTIVE")
    outbox.queue(trade, now=NOW)
    trade["current_status"] = "LOSS_SL"
    outbox.queue(trade, now=NOW + dt.timedelta(minutes=10))

    assert trade[outbox.FIELD]["status"] == "LOSS_SL"
    assert trade[outbox.FIELD]["queued_at"] == NOW.isoformat(timespec="seconds")


def test_backlog_is_delivered_oldest_first():
    old = _trade("old"); outbox.queue(old, now=NOW)
    new = _trade("new"); outbox.queue(new, now=NOW + dt.timedelta(minutes=5))

    order = []
    outbox.flush([new, old], send=lambda t: order.append(t["id"]) or True)

    assert order == ["old", "new"]


def test_trades_with_nothing_owed_are_left_alone():
    quiet = _trade("quiet")
    result = outbox.flush([quiet], send=lambda t: pytest.fail("must not send"))

    assert result["attempted"] == 0
    assert quiet == _trade("quiet")


# ==========================================================================
# Dead-man escalation
# ==========================================================================
def _model(key="client_test") -> ModelConfig:
    return ModelConfig(
        key=key, label="Test tenant", cycle_interval_minutes=5,
        closed_statuses=("LOSS_SL",), update_statuses=(), closed_with_pnl=("LOSS_SL",),
    )


@pytest.fixture
def sup(tmp_path, monkeypatch):
    """Supervisor with sandboxed state and a captured ops channel."""
    module = importlib.import_module("run_supervisor")
    monkeypatch.setattr(module, "STATE_DIR", str(tmp_path / "state"))
    sent: list[str] = []
    monkeypatch.setattr(module, "_notify_ops", lambda message: sent.append(message))
    module._sent = sent
    return module


def test_a_single_failure_does_not_alert(sup):
    """One timeout is a slow provider. Alerting on it trains the reader to
    ignore the alert."""
    sup._record_outcome(_model(), "timeout", "slow")
    assert sup._sent == []


def test_alert_fires_once_the_streak_crosses_the_threshold(sup, monkeypatch):
    monkeypatch.setattr(sup, "HEARTBEAT_ALERT_AFTER", 3)
    model = _model()
    for _ in range(3):
        sup._record_outcome(model, "error", "exit code 1")

    assert len(sup._sent) == 1
    assert "client_test" in sup._sent[0]
    assert "3 consecutive" in sup._sent[0]


def test_the_alert_does_not_repeat_every_cycle(sup, monkeypatch):
    """The failure mode of a noisy alert is a muted alert."""
    monkeypatch.setattr(sup, "HEARTBEAT_ALERT_AFTER", 3)
    model = _model()
    for _ in range(10):
        sup._record_outcome(model, "error", "exit code 1")

    assert len(sup._sent) == 1


def test_recovery_is_announced_and_re_arms_the_alert(sup, monkeypatch):
    monkeypatch.setattr(sup, "HEARTBEAT_ALERT_AFTER", 2)
    model = _model()
    sup._record_outcome(model, "error", "boom")
    sup._record_outcome(model, "error", "boom")
    sup._record_outcome(model, "ok", duration_seconds=1.0)

    assert len(sup._sent) == 2
    assert "recovered" in sup._sent[1]

    # ...and a NEW outage alerts again rather than staying latched.
    sup._record_outcome(model, "error", "boom")
    sup._record_outcome(model, "error", "boom")
    assert len(sup._sent) == 3


def test_a_success_resets_the_streak(sup, monkeypatch):
    monkeypatch.setattr(sup, "HEARTBEAT_ALERT_AFTER", 3)
    model = _model()
    sup._record_outcome(model, "error", "boom")
    sup._record_outcome(model, "error", "boom")
    sup._record_outcome(model, "ok")
    sup._record_outcome(model, "error", "boom")

    assert sup._sent == []
    assert read_heartbeat(sup.STATE_DIR, model.key)["failure_streak"] == 1


def test_the_streak_survives_a_supervisor_restart(sup, monkeypatch):
    """It lives in the heartbeat, not in memory: restarting during an outage
    must not re-arm an alert somebody already received."""
    monkeypatch.setattr(sup, "HEARTBEAT_ALERT_AFTER", 2)
    model = _model()
    sup._record_outcome(model, "error", "boom")
    sup._record_outcome(model, "error", "boom")
    assert len(sup._sent) == 1

    # A restart re-reads state from disk; `alerted` is already latched there.
    sup._record_outcome(model, "error", "boom")
    assert len(sup._sent) == 1
    assert read_heartbeat(sup.STATE_DIR, model.key)["failure_streak"] == 3


def test_escalation_can_be_disabled(sup, monkeypatch):
    monkeypatch.setattr(sup, "HEARTBEAT_ALERT_AFTER", 0)
    model = _model()
    for _ in range(20):
        sup._record_outcome(model, "error", "boom")
    assert sup._sent == []


def test_a_missing_tenant_directory_counts_as_a_failure(sup, monkeypatch):
    """`skipped` means the code is not on disk. That is not a healthy cycle and
    must not read as one on the dashboard."""
    monkeypatch.setattr(sup, "HEARTBEAT_ALERT_AFTER", 2)
    model = _model()
    sup._record_outcome(model, "skipped", "no live_runner on disk")
    sup._record_outcome(model, "skipped", "no live_runner on disk")
    assert len(sup._sent) == 1


def test_a_failing_ops_send_never_breaks_the_cycle(tmp_path, monkeypatch):
    """The escalation path reports that something is already broken. It must
    not become the thing that breaks the supervisor."""
    module = importlib.import_module("run_supervisor")
    monkeypatch.setattr(module, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(module, "HEARTBEAT_ALERT_AFTER", 1)

    import core.utils.telegram_notifier as notifier
    monkeypatch.setattr(notifier, "send_telegram_message",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("network down")))

    module._record_outcome(_model(), "error", "boom")  # must not raise
    assert read_heartbeat(module.STATE_DIR, "client_test")["failure_streak"] == 1
