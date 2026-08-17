"""Hot-path caching, the two unswept artifacts, and provider failure accounting.

None of these change what the system decides -- they change what it costs and
what it says while deciding it. Each is tested for the property that made it
worth doing, not merely for "it runs".
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

from core.data import health  # noqa: E402
from core.maintenance import prune_timestamped_records, sweep_runtime  # noqa: E402
from core.utils import market_hours  # noqa: E402


# ==========================================================================
# Market session caching
# ==========================================================================
@pytest.fixture(autouse=True)
def _clean_session_cache():
    market_hours.clear_session_cache()
    yield
    market_hours.clear_session_cache()


def test_the_calendar_is_consulted_once_per_date(monkeypatch):
    """`mcal.schedule()` rebuilds a DataFrame from the exchange's holiday rules
    on every call, and this is asked thousands of times an hour across tenants
    for an answer that changes once a day."""
    calls = []
    real = market_hours._NYSE.schedule

    def counted(start_date, end_date):
        calls.append(start_date)
        return real(start_date=start_date, end_date=end_date)

    monkeypatch.setattr(market_hours._NYSE, "schedule", counted)

    noon = dt.datetime(2026, 8, 12, 16, 0, tzinfo=dt.timezone.utc)  # a Wednesday
    for minute in range(20):
        market_hours.is_market_open(noon + dt.timedelta(minutes=minute))

    assert len(calls) == 1


def test_each_new_date_is_looked_up_once(monkeypatch):
    calls = []
    real = market_hours._NYSE.schedule
    monkeypatch.setattr(market_hours._NYSE, "schedule",
                        lambda start_date, end_date: calls.append(start_date)
                        or real(start_date=start_date, end_date=end_date))

    base = dt.datetime(2026, 8, 12, 16, 0, tzinfo=dt.timezone.utc)
    for day in range(3):
        for _ in range(4):
            market_hours.is_market_open(base + dt.timedelta(days=day))

    assert len(calls) == 3


def test_caching_the_session_does_not_freeze_the_answer():
    """Only the day's (open, close) is memoised -- "is now inside it" must still
    flip at 09:30 and 16:00 ET, or a cached tenant would trade after the bell."""
    before = dt.datetime(2026, 8, 12, 12, 0, tzinfo=dt.timezone.utc)   # 08:00 ET
    during = dt.datetime(2026, 8, 12, 16, 0, tzinfo=dt.timezone.utc)   # 12:00 ET
    after = dt.datetime(2026, 8, 12, 21, 30, tzinfo=dt.timezone.utc)   # 17:30 ET

    assert market_hours.is_market_open(before) is False
    assert market_hours.is_market_open(during) is True
    assert market_hours.is_market_open(after) is False


def test_a_weekend_is_still_closed():
    saturday = dt.datetime(2026, 8, 15, 16, 0, tzinfo=dt.timezone.utc)
    assert market_hours.is_market_open(saturday) is False
    assert market_hours.session_bounds(saturday.date()) is None


# ==========================================================================
# Retention: snapshots compacted in place
# ==========================================================================
def _snapshot_file(tmp_path, ages_days: list[float]) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    records = [{"ts": (now - dt.timedelta(days=age)).isoformat(),
                "usdt_dominance_pct": 8.1, "total_mcap_usd": 2.2e12}
               for age in ages_days]
    path = str(tmp_path / "market_snapshots.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(records, fh)
    return path


def test_stale_snapshot_records_are_dropped(tmp_path):
    path = _snapshot_file(tmp_path, [1, 5, 40, 100])
    removed, _bytes = prune_timestamped_records(path, 30)

    assert removed == 2
    kept = json.load(open(path, encoding="utf-8"))
    assert len(kept) == 2


def test_snapshot_dry_run_counts_without_writing(tmp_path):
    path = _snapshot_file(tmp_path, [1, 40, 100])
    before = open(path, encoding="utf-8").read()

    removed, _ = prune_timestamped_records(path, 30, dry_run=True)

    assert removed == 2
    assert open(path, encoding="utf-8").read() == before


def test_unparseable_records_are_kept_never_guessed(tmp_path):
    """Housekeeping must not be able to damage a store it does not understand."""
    path = str(tmp_path / "market_snapshots.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([{"ts": "not-a-date"}, {"no_ts_field": 1}, "a bare string"], fh)

    removed, _ = prune_timestamped_records(path, 30)

    assert removed == 0
    assert len(json.load(open(path, encoding="utf-8"))) == 3


def test_a_corrupt_snapshot_file_is_left_alone(tmp_path):
    path = str(tmp_path / "market_snapshots.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ not json")

    assert prune_timestamped_records(path, 30) == (0, 0)
    assert open(path, encoding="utf-8").read() == "{ not json"


def test_zero_retention_disables_snapshot_compaction(tmp_path):
    path = _snapshot_file(tmp_path, [500])
    assert prune_timestamped_records(path, 0) == (0, 0)
    assert len(json.load(open(path, encoding="utf-8"))) == 1


# ==========================================================================
# Retention: backtest logs, and what must NEVER be swept
# ==========================================================================
def test_sweep_ages_out_backtest_logs(tmp_path):
    backtest = tmp_path / "backtest"
    backtest.mkdir()
    old = backtest / "trades_log_retired.csv"
    fresh = backtest / "trades_log_livne.csv"
    old.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")
    ancient = dt.datetime.now().timestamp() - 200 * 86400
    os.utime(old, (ancient, ancient))

    results = sweep_runtime(str(tmp_path), 0, 0, backtest_days=90)

    assert results["backtest_logs"][0] == 1
    assert fresh.exists() and not old.exists()


def test_sweep_still_never_touches_stores_or_journals(tmp_path):
    """The boundary that matters most: retention may delete derived artifacts
    and NOTHING else. Re-asserted here because this change added two new
    sweeps, one of which writes inside runtime/stores."""
    stores = tmp_path / "stores"
    journals = tmp_path / "journals"
    stores.mkdir(); journals.mkdir()
    trades = stores / "trades_livne.json"
    journal = journals / "trade_journal_livne.xlsx"
    trades.write_text('[{"id": "keep me"}]', encoding="utf-8")
    journal.write_text("binary-ish", encoding="utf-8")
    ancient = dt.datetime.now().timestamp() - 900 * 86400
    for path in (trades, journal):
        os.utime(path, (ancient, ancient))

    sweep_runtime(str(tmp_path), 1, 1, backtest_days=1, snapshot_days=1,
                  log_days=1, funnel_days=1)

    assert trades.exists() and json.loads(trades.read_text(encoding="utf-8"))
    assert journal.exists()


# ==========================================================================
# Provider failure accounting
# ==========================================================================
@pytest.fixture(autouse=True)
def _clean_health():
    health.reset()
    yield
    health.reset()


def test_the_first_failure_names_the_exception_type(capsys):
    """A YFRateLimitError and a KeyError mean completely different things, and
    the old handler showed neither."""
    health.note_failure("yahoo", "fast_info", KeyError("marketCap"), "AAPL")
    err = capsys.readouterr().err

    assert "yahoo" in err and "fast_info(AAPL)" in err and "KeyError" in err


def test_repeat_failures_are_counted_not_printed(capsys):
    for _ in range(9):
        health.note_failure("yahoo", "fast_info", ValueError("boom"), "AAPL")
    err = capsys.readouterr().err

    assert health.failure_streak("yahoo") == 9
    assert err.count("fast_info") == 1        # only the first line


def test_a_sustained_outage_gets_one_loud_summary(capsys):
    for _ in range(10):
        health.note_failure("yahoo", "fast_info", ValueError("boom"), "AAPL")
    err = capsys.readouterr().err

    assert "10 consecutive" in err


def test_recovery_after_an_outage_is_reported(capsys):
    for _ in range(10):
        health.note_failure("binance", "klines", OSError("timeout"), "BTCUSDT")
    capsys.readouterr()

    health.note_success("binance")

    assert "recovered after 10" in capsys.readouterr().err
    assert health.failure_streak("binance") == 0


def test_success_resets_the_streak_quietly(capsys):
    health.note_failure("yahoo", "fast_info", ValueError("x"), "AAPL")
    capsys.readouterr()

    health.note_success("yahoo")

    assert health.failure_streak("yahoo") == 0
    assert capsys.readouterr().err == ""      # one blip is not news


def test_providers_are_tracked_independently():
    """One venue being down must not mask or inflate the other's health."""
    health.note_failure("yahoo", "fast_info", ValueError("x"), "AAPL")
    health.note_failure("yahoo", "fast_info", ValueError("x"), "MSFT")
    health.note_failure("binance", "klines", ValueError("x"), "BTCUSDT")

    assert health.snapshot() == {"yahoo": 2, "binance": 1}
