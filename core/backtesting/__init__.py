"""Shared intraday backtesting infrastructure.

Everything in this package is STRATEGY-AGNOSTIC: candle retrieval, on-disk
caching, point-in-time resampling, intrabar stop/target evaluation, the trade
log and the summary metrics. No trading rule lives here -- each model wires
its own (unchanged) strategy modules into these primitives from its own
`backtest_suite/` package.

It lives under `core/` for the same reason `core/utils/yf_cache.py` does: the
Livne and Biton backtests are two separate OS processes with two separate
top-level import namespaces, and a shared on-disk candle cache means a symbol
downloaded by one model's backtest is served from disk to the other instead
of being pulled from Binance/Yahoo twice.

    import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*
    from core.backtesting import IntradayDataFeed, INTERVALS
"""
from __future__ import annotations

from core.backtesting.cache import CandleCache
from core.backtesting.engine import (
    FinerBarTieBreaker,
    ExcursionTracker,
    apply_slippage,
    duration_breakdown,
    hits,
    pnl_pct,
)
from core.backtesting.feed import IntradayDataFeed
from core.backtesting.intervals import INTERVALS, Interval, interval_of
from core.backtesting.metrics import BacktestMetrics, compute_metrics, format_summary
from core.backtesting.resampling import PointInTimeResampler, resample_ohlcv
from core.backtesting.trade_log import (
    TRADE_LOG_COLUMNS,
    TradeRecord,
    export_trades_csv,
    format_trade_log,
    trades_to_dataframe,
)

__all__ = [
    "BacktestMetrics",
    "CandleCache",
    "ExcursionTracker",
    "FinerBarTieBreaker",
    "INTERVALS",
    "IntradayDataFeed",
    "Interval",
    "PointInTimeResampler",
    "TRADE_LOG_COLUMNS",
    "TradeRecord",
    "apply_slippage",
    "compute_metrics",
    "duration_breakdown",
    "export_trades_csv",
    "format_summary",
    "format_trade_log",
    "hits",
    "interval_of",
    "pnl_pct",
    "resample_ohlcv",
    "trades_to_dataframe",
]
