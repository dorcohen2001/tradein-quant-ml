"""CorrelationEngine: Macro Market Context / Regime Filter.

Determines the overall market regime (Bullish / Bearish / Neutral / High
Volatility) for stocks and crypto, and enforces directional constraints on
trade setups per the spec's explicit correlation rules:

  Crypto (USDT.D inverse correlation): USDT.D trending up -> SHORT only;
  USDT.D breaking down/trending down -> LONG only. TOTAL market cap trend is
  surfaced as directional *confirmation* in the summary, not an independent
  hard gate.

  Stocks (SPY/QQQ index correlation): both indexes in bullish structure ->
  LONG-only equities; both in bearish structure -> SHORT-only equities.

USDT.D and TOTAL market cap have no free historical-trend API, so their
trend is derived from a small locally-accumulated JSON snapshot history
(same atomic-write pattern as `paper_trading.tracker`) rather than a paid
feed -- until enough history has accumulated this honestly reports "Unknown"
(both directions allowed) instead of guessing, the same way the BTC-momentum
risk-sentiment proxy below is labeled for what it actually is.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass, field

from config import (
    CORRELATION_TREND_MIN_PCT,
    MACRO_SNAPSHOT_MAX_AGE_DAYS,
    MACRO_SNAPSHOT_STORE,
    TOTAL_MCAP_TREND_LOOKBACK_HOURS,
    USDT_D_TREND_LOOKBACK_HOURS,
    USDT_DOMINANCE_RISING_PCT,
    VIX_ELEVATED,
    VIX_HIGH_VOL,
)
from ta.ta_engine import add_moving_averages, determine_trend
from utils.data_fetch import (
    get_binance_klines,
    get_stock_history,
    get_total_crypto_market_cap,
    get_usdt_dominance_pct,
)


@dataclass
class MacroContext:
    asset_class: str            # "stocks" or "crypto"
    regime: str                 # Bullish / Bearish / Neutral / High Volatility
    details: dict
    summary: str
    allowed: frozenset = field(default_factory=lambda: frozenset({"LONG", "SHORT"}))

    def allowed_directions(self) -> set[str]:
        return set(self.allowed)


# ---------------------------------------------------------------------------
# Local snapshot history (USDT.D / TOTAL trend derivation)
# ---------------------------------------------------------------------------
def _load_snapshots() -> list[dict]:
    if not os.path.exists(MACRO_SNAPSHOT_STORE):
        return []
    try:
        with open(MACRO_SNAPSHOT_STORE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return []  # a corrupt/missing snapshot cache only degrades trend precision, never a hard failure


def _save_snapshots(snapshots: list[dict]) -> None:
    store_dir = os.path.dirname(MACRO_SNAPSHOT_STORE)
    os.makedirs(store_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=store_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshots, f, indent=2)
        os.replace(tmp_path, MACRO_SNAPSHOT_STORE)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def record_snapshot(usdt_dominance_pct: float | None, total_mcap_usd: float | None) -> list[dict]:
    if usdt_dominance_pct is None and total_mcap_usd is None:
        return _load_snapshots()

    snapshots = _load_snapshots()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=MACRO_SNAPSHOT_MAX_AGE_DAYS)
    snapshots = [s for s in snapshots if dt.datetime.fromisoformat(s["ts"]) >= cutoff]
    snapshots.append({
        "ts": dt.datetime.now(dt.timezone.utc).isoformat(),
        "usdt_dominance_pct": usdt_dominance_pct,
        "total_mcap_usd": total_mcap_usd,
    })
    _save_snapshots(snapshots)
    return snapshots


def _trend_from_snapshots(snapshots: list[dict], field_name: str, current: float | None, lookback_hours: float) -> str:
    """"Rising" / "Falling" / "Neutral" (small move) / "Unknown" (no snapshot
    old enough yet to compare against)."""
    if current is None:
        return "Unknown"

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=lookback_hours)
    candidates = [
        s for s in snapshots
        if s.get(field_name) is not None and dt.datetime.fromisoformat(s["ts"]) <= cutoff
    ]
    if not candidates:
        return "Unknown"

    reference = max(candidates, key=lambda s: s["ts"])  # closest snapshot at/before the cutoff
    past_val = reference[field_name]
    if not past_val:
        return "Unknown"

    pct_change = (current - past_val) / past_val * 100
    if pct_change >= CORRELATION_TREND_MIN_PCT:
        return "Rising"
    if pct_change <= -CORRELATION_TREND_MIN_PCT:
        return "Falling"
    return "Neutral"


# ---------------------------------------------------------------------------
def _trend_and_price(df):
    df = add_moving_averages(df)
    trend = determine_trend(df)
    price = float(df["Close"].iloc[-1])
    return trend, price, df


def analyze_stock_macro() -> MacroContext:
    spy_df = get_stock_history("SPY", period="1y", interval="1d")
    qqq_df = get_stock_history("QQQ", period="1y", interval="1d")
    vix_df = get_stock_history("^VIX", period="3mo", interval="1d")

    spy_trend, spy_price, _ = _trend_and_price(spy_df)
    qqq_trend, qqq_price, _ = _trend_and_price(qqq_df)
    vix_level = float(vix_df["Close"].iloc[-1]) if not vix_df.empty else None

    high_vol = vix_level is not None and vix_level >= VIX_HIGH_VOL
    if high_vol:
        regime = "High Volatility"
        allowed = frozenset({"LONG", "SHORT"})
    elif spy_trend == "UPTREND" and qqq_trend == "UPTREND" and (vix_level or 0) < VIX_ELEVATED:
        regime = "Bullish"
        allowed = frozenset({"LONG"})
    elif spy_trend == "DOWNTREND" and qqq_trend == "DOWNTREND":
        regime = "Bearish"
        allowed = frozenset({"SHORT"})
    else:
        regime = "Neutral"
        allowed = frozenset({"LONG", "SHORT"})

    details = {
        "spy_trend": spy_trend, "spy_price": round(spy_price, 2),
        "qqq_trend": qqq_trend, "qqq_price": round(qqq_price, 2),
        "vix": round(vix_level, 2) if vix_level is not None else None,
        "reduce_size": high_vol,
    }
    summary = (
        f"SPY {spy_trend.title()} (${spy_price:.2f}), QQQ {qqq_trend.title()} (${qqq_price:.2f}), "
        f"VIX {details['vix']} -> Regime: {regime} (equities restricted to {sorted(allowed)})"
    )
    return MacroContext("stocks", regime, details, summary, allowed)


def analyze_crypto_macro() -> MacroContext:
    btc_df = get_binance_klines("BTCUSDT", interval="1d", limit=300)
    eth_df = get_binance_klines("ETHUSDT", interval="1d", limit=300)

    btc_trend, btc_price, _ = _trend_and_price(btc_df)
    eth_trend, eth_price, _ = _trend_and_price(eth_df)

    usdt_d_now = get_usdt_dominance_pct()
    total_mcap_now = get_total_crypto_market_cap()
    snapshots = record_snapshot(usdt_d_now, total_mcap_now)
    usdt_d_trend = _trend_from_snapshots(snapshots, "usdt_dominance_pct", usdt_d_now, USDT_D_TREND_LOOKBACK_HOURS)
    total_trend = _trend_from_snapshots(snapshots, "total_mcap_usd", total_mcap_now, TOTAL_MCAP_TREND_LOOKBACK_HOURS)

    # Risk sentiment proxy: BTC's 5-day return direction. This is NOT a real
    # USDT-dominance trend read (that comes from usdt_d_trend above, once
    # enough snapshot history exists) -- it only feeds the High Volatility flag.
    btc_5d_ret = (btc_df["Close"].iloc[-1] / btc_df["Close"].iloc[-6] - 1)
    risk_sentiment = "Risk-off" if btc_5d_ret < -USDT_DOMINANCE_RISING_PCT else "Risk-on"

    # USDT.D is the spec's explicit directional rule; TOTAL trend is
    # surfaced as confirmation only (see module docstring).
    if usdt_d_trend == "Rising":
        allowed = frozenset({"SHORT"})
    elif usdt_d_trend == "Falling":
        allowed = frozenset({"LONG"})
    else:
        allowed = frozenset({"LONG", "SHORT"})

    if risk_sentiment == "Risk-off" and btc_trend != "UPTREND":
        regime = "High Volatility"
    elif btc_trend == "UPTREND" and eth_trend == "UPTREND" and usdt_d_trend != "Rising":
        regime = "Bullish"
    elif btc_trend == "DOWNTREND" and eth_trend == "DOWNTREND" and usdt_d_trend != "Falling":
        regime = "Bearish"
    else:
        regime = "Neutral"

    details = {
        "btc_trend": btc_trend, "btc_price": round(btc_price, 2),
        "eth_trend": eth_trend, "eth_price": round(eth_price, 2),
        "usdt_dominance_pct": usdt_d_now, "usdt_dominance_trend": usdt_d_trend,
        "total_mcap_usd": total_mcap_now, "total_mcap_trend": total_trend,
        "risk_sentiment_btc_5d_proxy": risk_sentiment,
        "btc_5d_return_pct": round(btc_5d_ret * 100, 2),
        "reduce_size": regime == "High Volatility",
    }
    usdt_d_str = f"{usdt_d_now:.2f}% ({usdt_d_trend})" if usdt_d_now is not None else "n/a"
    summary = (
        f"BTC {btc_trend.title()} (${btc_price:,.2f}), ETH {eth_trend.title()} (${eth_price:,.2f}), "
        f"USDT.D {usdt_d_str}, TOTAL mcap {total_trend} -> Regime: {regime} "
        f"(crypto restricted to {sorted(allowed)})"
    )
    return MacroContext("crypto", regime, details, summary, allowed)


def get_macro_context(asset_class: str) -> MacroContext:
    if asset_class == "crypto":
        return analyze_crypto_macro()
    return analyze_stock_macro()
