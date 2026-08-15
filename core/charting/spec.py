"""Per-model chart configuration.

The alert chart used to be hard-wired to daily candles: `interval="1d"`,
`period="2y"`, EMA 50/200, a 180-bar display window. That was right when both
models were swing pipelines. It stopped being right when Livne moved to a 15m
trigger with a 2-minute cycle and a target holding period of minutes to hours
-- every Livne alert shipped a DAILY chart on which the entry zone, stop and
TP1 routinely sit inside a single candle's range, i.e. a picture that cannot
show the setup it is illustrating.

A `ChartSpec` makes that a per-model decision. `ChartSpec.daily()` is the
previous hard-coded behaviour exactly, so a model that says nothing keeps the
chart it always had.

WHY THE HISTORY WINDOW IS PER-ASSET-CLASS
-----------------------------------------
`stock_period` and `crypto_limit` are separate fields because the two data
sources have genuinely different limits, and getting this wrong fails
silently:

  * yfinance caps intraday history hard -- roughly 60 days at 15m/5m. Asking
    for `period="2y", interval="15m"` does not return two years of 15m bars;
    it returns an EMPTY frame, and the alert would fall back to text-only
    with no obvious cause.
  * Binance has no such cap and is paged by bar count, so crypto is expressed
    as a number of candles instead.

Both must leave enough warm-up for the longest moving average in `emas`: an
EMA 200 drawn on 210 bars is mostly meaningless, so keep the window comfortably
above `max(emas)` plus the display window.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Roughly what yfinance will serve per intraday interval. Used only to warn --
# never to silently rewrite a caller's request, since the fetchers and the
# provider layer already clamp, and a surprising clamp is how the 15m/2y bug
# would hide.
YAHOO_INTRADAY_MAX_DAYS = {
    "1m": 7, "2m": 60, "5m": 60, "15m": 60, "30m": 60, "60m": 730, "1h": 730,
}


@dataclass(frozen=True)
class ChartSpec:
    """How to fetch and draw one model's alert chart."""

    interval: str = "1d"          # fetch interval, passed to both fetchers
    tf_label: str = "1D"          # shown in the title and used in the filename
    stock_period: str = "2y"      # yfinance `period` for the stock path
    crypto_limit: int = 600       # Binance candle count for the crypto path
    emas: tuple[int, ...] = (50, 200)
    show_rsi: bool = True
    show_macd: bool = True

    @classmethod
    def daily(cls) -> "ChartSpec":
        """The pre-refactor behaviour, unchanged. Kept explicit so a model can
        opt INTO it by name rather than by omission."""
        return cls()

    @classmethod
    def intraday_15m(cls) -> "ChartSpec":
        """For a model whose trigger timeframe is 15m.

        `stock_period="45d"` sits inside yfinance's ~60-day 15m cap with margin
        (the cap is approximate and requesting exactly at the boundary
        intermittently returns nothing). 45 days of 15m bars is ~1,300 -- ample
        warm-up for the EMA 200 plus the 180-bar display window.
        """
        return cls(interval="15m", tf_label="15M", stock_period="45d", crypto_limit=1000)

    def warnings(self) -> list[str]:
        """Configuration that will probably produce an empty or degenerate
        chart. Reported by the caller rather than raised: a bad chart spec must
        degrade to a text-only alert, never break the alert."""
        issues: list[str] = []
        cap_days = YAHOO_INTRADAY_MAX_DAYS.get(self.interval)
        if cap_days is not None:
            period = self.stock_period.strip().lower()
            requested_days = None
            if period.endswith("d") and period[:-1].isdigit():
                requested_days = int(period[:-1])
            elif period.endswith("y") and period[:-1].isdigit():
                requested_days = int(period[:-1]) * 365
            elif period.endswith("mo") and period[:-2].isdigit():
                requested_days = int(period[:-2]) * 30
            if requested_days is not None and requested_days > cap_days:
                issues.append(
                    f"stock_period={self.stock_period!r} exceeds yfinance's ~{cap_days}-day "
                    f"cap for interval {self.interval!r}; the stock chart will come back empty"
                )
        if self.emas and self.crypto_limit < max(self.emas) * 2:
            issues.append(
                f"crypto_limit={self.crypto_limit} leaves little warm-up for EMA {max(self.emas)}"
            )
        return issues
