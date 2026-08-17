"""A market that does exactly what a test tells it to.

WHY NOT RECORDED FIXTURES
-------------------------
Cassettes of real responses go stale, need refreshing per symbol, and encode
whatever the market happened to do that day -- which is never the case you
actually want to test. "Price gapped through both the stop and the target in
one bar" is a five-line script here and an act of archaeology in a cassette.

WHY NOT LIVE DATA
-----------------
A test that reaches Yahoo is a test that fails on a plane, fails in CI at 3am
when Yahoo rate-limits, and passes or fails depending on what a stock did this
morning. It also cannot produce the interesting shapes on demand: an IPO with
four weeks of history, a halted symbol, a delisting.

So: scripted series, deterministic, offline, instant.
"""
from __future__ import annotations

import datetime as dt

import pandas as pd


def scripted_series(bars: list[tuple[float, float]] | list[float],
                    start: dt.datetime | None = None,
                    interval_minutes: int = 5) -> pd.DataFrame:
    """An OHLCV frame from a list of `(high, low)` pairs, or of closes.

    Open and Close are placed inside the range so the frame is self-consistent
    for anything that reads them; only High/Low drive level tests, which is
    what the lifecycle actually keys on.
    """
    start = start or dt.datetime(2026, 8, 17, 9, 0, 0)
    rows = []
    index = []
    for i, bar in enumerate(bars):
        high, low = bar if isinstance(bar, (tuple, list)) else (bar, bar)
        mid = (high + low) / 2
        rows.append({"Open": mid, "High": high, "Low": low, "Close": mid, "Volume": 1_000.0})
        index.append(start + dt.timedelta(minutes=interval_minutes * i))
    frame = pd.DataFrame(rows, index=pd.DatetimeIndex(index, name="timestamp"))
    return frame


class FakeMarket:
    """A deterministic stand-in for every market-data method a tenant uses.

    One object supplies the whole data surface -- last price, traded range,
    candle history for both venues -- so a conformance run can hold price
    completely still, walk it through a level, or hand back nothing at all,
    without any tenant knowing it is not talking to a venue.
    """

    def __init__(self, price: float = 100.0) -> None:
        self.price = price
        self.range: tuple[float, float] | None = None
        self.history: pd.DataFrame | None = None
        self.calls: list[tuple[str, str]] = []
        # When set, every accessor returns "nothing available", which is how a
        # provider outage looks to a tenant.
        self.blackout = False

    # -- controls ----------------------------------------------------------
    def hold(self, price: float) -> "FakeMarket":
        """Price flat at `price`: nothing should transition."""
        self.price = price
        self.range = (price, price)
        return self

    def sweep(self, low: float, high: float) -> "FakeMarket":
        """A bar whose traded range spans [low, high] -- the shape that
        triggers stops, fills and targets."""
        self.range = (high, low)
        return self

    # -- the tenant-facing surface ----------------------------------------
    def current_price(self, ticker: str, asset_class: str) -> float | None:
        self.calls.append(("current_price", ticker))
        return None if self.blackout else self.price

    def price_range(self, ticker: str, asset_class: str, lookback_minutes: int) -> dict | None:
        self.calls.append(("price_range", ticker))
        if self.blackout or self.range is None:
            return None
        high, low = self.range
        return {"high": high, "low": low, "last_close": self.price}

    def candles(self, ticker: str, *args, **kwargs) -> pd.DataFrame:
        self.calls.append(("candles", ticker))
        if self.blackout:
            return scripted_series([])
        if self.history is not None:
            return self.history
        return scripted_series([(self.price, self.price)] * 3)

    def install(self, strategy) -> "FakeMarket":
        """Point a `TenantStrategy` at this market instead of the network.

        Replaces only the four data accessors the platform defines. Everything
        else -- the lifecycle, the screener, the message text -- runs exactly as
        it does in production, which is the whole point: the tenant is not
        modified for the test, only unplugged from the venue.
        """
        strategy.current_price = self.current_price
        strategy.price_range = self.price_range
        strategy.fetch_crypto = lambda ticker, interval="5m", limit=300: self.candles(ticker)
        strategy.fetch_stock = lambda ticker, period="5d", interval="5m": self.candles(ticker)
        return self
