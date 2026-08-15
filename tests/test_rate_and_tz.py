"""Timezone preservation in the stock adapter, and cross-process rate limiting.

The timezone tests guard a silent failure mode: a naive-UTC index shifts every
resample bucket boundary, moving the swing extremes Livne's HTF zone map is
built from. Nothing raises; the signals just quietly change.
"""
from __future__ import annotations

import multiprocessing
import os

import pandas as pd
import pytest

from core.data.market import EXCHANGE_TZ, get_stock_candles, period_to_bars, to_exchange_tz


def _naive_utc_frame(periods=8, freq="1D"):
    # 04:00 UTC == 00:00 EDT, which is where yfinance puts a daily bar.
    idx = pd.date_range("2026-08-10 04:00", periods=periods, freq=freq)
    return pd.DataFrame(
        {"Open": 1.0, "High": 2.0, "Low": 0.5, "Close": 1.5, "Volume": 10}, index=idx)


class _StubFeed:
    download_count = 0

    def __init__(self, frame=None):
        self._frame = frame if frame is not None else _naive_utc_frame()

    def get_candles(self, symbol, asset_class, interval, start, end):
        return self._frame


# ==========================================================================
# Timezone adapter
# ==========================================================================
def test_naive_utc_is_relabelled_to_exchange_local_without_moving_bars():
    """Localise+convert relabels the same instants. A daily bar stored as
    04:00 UTC is 00:00-04:00 in New York -- exactly what yfinance returned."""
    frame = _naive_utc_frame()
    out = to_exchange_tz(frame)
    assert str(out.index.tz) == EXCHANGE_TZ
    assert out.index[0].hour == 0
    assert list(out.index.tz_convert("UTC").tz_localize(None)) == list(frame.index)


def test_conversion_is_idempotent():
    """An already-aware frame is converted, not double-localised (which raises)."""
    once = to_exchange_tz(_naive_utc_frame())
    twice = to_exchange_tz(once)
    assert str(twice.index.tz) == EXCHANGE_TZ
    assert list(once.index) == list(twice.index)


def test_empty_frame_survives_conversion():
    assert len(to_exchange_tz(pd.DataFrame())) == 0


def test_stock_candles_are_tz_aware_by_default():
    frame = get_stock_candles("AAPL", interval="1d", bars=5, feed=_StubFeed())
    assert str(frame.index.tz) == EXCHANGE_TZ


def test_tz_none_returns_the_raw_storage_form():
    frame = get_stock_candles("AAPL", interval="1d", bars=5, tz=None, feed=_StubFeed())
    assert frame.index.tz is None


def test_resample_buckets_shift_when_the_timezone_is_dropped():
    """THE regression this adapter exists to prevent. Same bars, two index
    labellings, different 4h boundaries -- and therefore different swing
    structure downstream."""
    hourly = _naive_utc_frame(periods=48, freq="1h")
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    naive = hourly.resample("4h").agg(agg).dropna()
    aware = to_exchange_tz(hourly).resample("4h").agg(agg).dropna()
    naive_edges = [t.strftime("%H:%M") for t in naive.index]
    aware_edges = [t.strftime("%H:%M") for t in aware.index]
    assert naive_edges != aware_edges, "if these ever match, this test proves nothing"


def test_period_translates_to_a_sane_bar_count():
    assert period_to_bars("1y", "1d") == 260
    assert period_to_bars("6mo", "1d") == 130
    assert period_to_bars("1y", "1h") > period_to_bars("1y", "1d")
    assert period_to_bars("nonsense", "1d") == 260   # falls back, never zero


def test_ohlcv_columns_only():
    frame = get_stock_candles("AAPL", interval="1d", bars=5, feed=_StubFeed())
    assert list(frame.columns) == ["Open", "High", "Low", "Close", "Volume"]


# ==========================================================================
# Rate governor
# ==========================================================================
@pytest.fixture
def governor(monkeypatch, tmp_path):
    """Private bucket directory so tests never contend with a live supervisor."""
    monkeypatch.setenv("TRADEIN_RATELIMIT_DIR", str(tmp_path))
    from core.data import rate_governor as rg
    rg.reset("binance")
    rg.reset("yahoo")
    return rg


def test_burst_within_capacity_is_not_throttled(governor):
    waits = [governor.acquire("binance", weight=2.0, sleep=False) for _ in range(10)]
    assert all(w == 0.0 for w in waits)


def test_exceeding_capacity_forces_a_wait(governor):
    governor.acquire("binance", weight=governor.BINANCE.capacity, sleep=False)
    wait = governor.acquire("binance", weight=governor.BINANCE.rate, sleep=False)
    assert wait > 0.5, f"expected roughly 1s of debt, got {wait}"


def test_min_interval_spaces_calls_even_with_tokens_available(governor):
    """Yahoo publishes no quota but throttles aggressive callers, so spacing is
    enforced on top of the bucket."""
    first = governor.acquire("yahoo", sleep=False)
    second = governor.acquire("yahoo", sleep=False)
    assert first == 0.0
    assert second >= governor.YAHOO.min_interval * 0.9


def test_unknown_provider_is_ungoverned(governor):
    assert governor.acquire("not_a_provider") == 0.0


def test_wait_is_capped_so_a_cycle_is_never_wedged(governor):
    """A tenant's cycle is bounded by the supervisor timeout; an unbounded wait
    would convert a delayed request into a killed cycle."""
    governor.acquire("binance", weight=100_000, sleep=False)
    assert governor.acquire("binance", weight=1, sleep=False, max_wait=2.0) <= 2.0


def test_corrupt_bucket_state_allows_the_call(governor, tmp_path):
    """A governor that blocks market data because it cannot parse JSON has
    caused a worse outage than the one it was preventing."""
    (tmp_path / "ratelimit_binance.json").write_text("{ truncated", encoding="utf-8")
    assert governor.acquire("binance", weight=1, sleep=False) == 0.0


def test_snapshot_reports_live_bucket_state(governor):
    snap = governor.snapshot("binance")
    assert snap["provider"] == "binance"
    assert snap["tokens"] <= snap["capacity"]


def _drain(state_dir, count, results):
    """Child process: consume tokens from the SHARED on-disk bucket."""
    os.environ["TRADEIN_RATELIMIT_DIR"] = state_dir
    import importlib
    rg = importlib.import_module("core.data.rate_governor")
    total = 0.0
    for _ in range(count):
        total += rg.acquire("binance", weight=4.0, sleep=False)
    results.put(total)


@pytest.mark.skipif(os.name != "nt", reason="file-lock behaviour is verified on Windows")
def test_two_processes_share_one_budget(tmp_path, monkeypatch):
    """THE cross-process property. Two processes draining the same bucket must
    between them exhaust it -- a per-process limiter would let each burst to the
    full capacity independently."""
    monkeypatch.setenv("TRADEIN_RATELIMIT_DIR", str(tmp_path))
    from core.data import rate_governor as rg
    rg.reset("binance")

    ctx = multiprocessing.get_context("spawn")
    results = ctx.Queue()
    procs = [ctx.Process(target=_drain, args=(str(tmp_path), 20, results)) for _ in range(2)]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=180)

    waits = [results.get(timeout=30) for _ in procs]
    # 2 processes x 20 calls x weight 4 = 160 against a capacity of 120, so the
    # excess must have produced measurable waiting somewhere.
    assert sum(waits) > 0, "160 weight drained from a 120 bucket with zero throttling"
    assert rg.snapshot("binance")["tokens"] < rg.BINANCE.capacity
