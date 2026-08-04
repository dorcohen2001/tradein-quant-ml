"""Liquidity / order-flow filter.

No paid liquidity-heatmap feed (Coinglass/Hyblock) is configured, so this
approximates one from free public data:

  - Crypto: combined Binance + Bybit public spot order-book depth (resting
    bid/ask notional near price).
  - Stocks: no free Level-2 book exists, so historical volume-at-price
    (volume profile) over the recent lookback window stands in for resting
    liquidity.

Convention used throughout (documented here since it's the one genuinely
interpretive call in an otherwise data-driven filter): a large concentration
of liquidity/volume ABOVE the current price is treated as a magnet that
favors LONG (price is more likely to be drawn up to sweep it), and a large
concentration BELOW price favors SHORT -- mirroring the standard "liquidity
rests beyond swing highs/lows and price gets drawn to it" read used for real
liquidation-heatmap tools. This is a best-effort proxy, not a precise
liquidation map.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config import (
    LIQUIDITY_PROXIMITY_PCT,
    LIQUIDITY_TOP_CLUSTERS,
    VOLUME_PROFILE_BUCKETS,
    VOLUME_PROFILE_LOOKBACK,
)
from utils.data_fetch import get_binance_orderbook_depth, get_bybit_orderbook_depth


@dataclass
class LiquidityContext:
    source: str            # "orderbook" (crypto) or "volume_profile" (stocks)
    above_notional: float  # liquidity/volume concentrated above current price
    below_notional: float  # liquidity/volume concentrated below current price
    favored_direction: str | None  # "LONG" / "SHORT" / None if no meaningful skew
    top_clusters: list[tuple[float, float]]  # (price, notional) nearest clusters, either side


def _cluster_notional_above_below(
    levels_bid: list[tuple[float, float]], levels_ask: list[tuple[float, float]],
    price: float, proximity_pct: float,
) -> tuple[float, float]:
    above = sum(p * q for p, q in levels_ask if p > price and (p - price) / price <= proximity_pct)
    below = sum(p * q for p, q in levels_bid if p < price and (price - p) / price <= proximity_pct)
    return above, below


def _top_clusters(levels: list[tuple[float, float]], price: float, proximity_pct: float, n: int) -> list[tuple[float, float]]:
    near = [(p, p * q) for p, q in levels if abs(p - price) / price <= proximity_pct]
    near.sort(key=lambda x: x[1], reverse=True)
    return near[:n]


def get_crypto_liquidity(symbol: str, price: float) -> LiquidityContext:
    binance = get_binance_orderbook_depth(symbol)
    bybit = get_bybit_orderbook_depth(symbol)

    bids: list[tuple[float, float]] = []
    asks: list[tuple[float, float]] = []
    for book in (binance, bybit):
        if book is None:
            continue
        bids.extend(book["bids"])
        asks.extend(book["asks"])

    if not bids and not asks:
        return LiquidityContext("orderbook", 0.0, 0.0, None, [])

    above, below = _cluster_notional_above_below(bids, asks, price, LIQUIDITY_PROXIMITY_PCT)
    clusters = _top_clusters(bids + asks, price, LIQUIDITY_PROXIMITY_PCT, LIQUIDITY_TOP_CLUSTERS)

    favored = None
    if above > 0 or below > 0:
        favored = "LONG" if above > below else ("SHORT" if below > above else None)

    return LiquidityContext("orderbook", above, below, favored, clusters)


def get_stock_liquidity(df: pd.DataFrame, price: float) -> LiquidityContext:
    window = df.tail(VOLUME_PROFILE_LOOKBACK)
    if window.empty or "Volume" not in window.columns:
        return LiquidityContext("volume_profile", 0.0, 0.0, None, [])

    lo, hi = float(window["Low"].min()), float(window["High"].max())
    if hi <= lo:
        return LiquidityContext("volume_profile", 0.0, 0.0, None, [])

    edges = np.linspace(lo, hi, VOLUME_PROFILE_BUCKETS + 1)
    mids = (edges[:-1] + edges[1:]) / 2
    typical = (window["High"] + window["Low"] + window["Close"]) / 3
    bucket_idx = np.clip(np.digitize(typical, edges) - 1, 0, VOLUME_PROFILE_BUCKETS - 1)
    vol_by_bucket = pd.Series(window["Volume"].to_numpy(), index=bucket_idx).groupby(level=0).sum()

    clusters = []
    above = below = 0.0
    for idx, vol in vol_by_bucket.items():
        bucket_price = float(mids[idx])
        notional = float(vol) * bucket_price
        if abs(bucket_price - price) / price > LIQUIDITY_PROXIMITY_PCT:
            continue
        clusters.append((bucket_price, notional))
        if bucket_price > price:
            above += notional
        elif bucket_price < price:
            below += notional

    clusters.sort(key=lambda x: x[1], reverse=True)
    favored = None
    if above > 0 or below > 0:
        favored = "LONG" if above > below else ("SHORT" if below > above else None)

    return LiquidityContext("volume_profile", above, below, favored, clusters[:LIQUIDITY_TOP_CLUSTERS])


def get_liquidity_context(ticker: str, asset_class: str, price: float, df: pd.DataFrame | None = None) -> LiquidityContext:
    if asset_class == "crypto":
        return get_crypto_liquidity(ticker, price)
    if df is None:
        return LiquidityContext("volume_profile", 0.0, 0.0, None, [])
    return get_stock_liquidity(df, price)
