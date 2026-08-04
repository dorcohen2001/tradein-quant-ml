"""Macro Market Context / Regime Filter.

Determines the overall market regime (Bullish / Bearish / Neutral / High Volatility)
for stocks and crypto, and enforces directional constraints on trade setups.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import USDT_DOMINANCE_RISING_PCT, VIX_ELEVATED, VIX_HIGH_VOL
from ta.ta_engine import add_moving_averages, determine_trend
from utils.data_fetch import get_binance_klines, get_stock_history, get_usdt_dominance_pct


@dataclass
class MacroContext:
    asset_class: str            # "stocks" or "crypto"
    regime: str                 # Bullish / Bearish / Neutral / High Volatility
    details: dict
    summary: str

    def allowed_directions(self) -> set[str]:
        if self.regime == "Bearish":
            return {"SHORT"}
        if self.regime == "Bullish":
            return {"LONG"}
        # Neutral and High Volatility permit both, but callers should size down
        return {"LONG", "SHORT"}


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

    if vix_level is not None and vix_level >= VIX_HIGH_VOL:
        regime = "High Volatility"
    elif spy_trend == "UPTREND" and qqq_trend == "UPTREND" and (vix_level or 0) < VIX_ELEVATED:
        regime = "Bullish"
    elif spy_trend == "DOWNTREND" and qqq_trend == "DOWNTREND":
        regime = "Bearish"
    else:
        regime = "Neutral"

    details = {
        "spy_trend": spy_trend, "spy_price": round(spy_price, 2),
        "qqq_trend": qqq_trend, "qqq_price": round(qqq_price, 2),
        "vix": round(vix_level, 2) if vix_level is not None else None,
    }
    summary = (
        f"SPY {spy_trend.title()} (${spy_price:.2f}), QQQ {qqq_trend.title()} (${qqq_price:.2f}), "
        f"VIX {details['vix']} -> Regime: {regime}"
    )
    return MacroContext("stocks", regime, details, summary)


def analyze_crypto_macro() -> MacroContext:
    btc_df = get_binance_klines("BTCUSDT", interval="1d", limit=300)
    eth_df = get_binance_klines("ETHUSDT", interval="1d", limit=300)

    btc_trend, btc_price, _ = _trend_and_price(btc_df)
    eth_trend, eth_price, _ = _trend_and_price(eth_df)

    usdt_d_now = get_usdt_dominance_pct()
    # Risk sentiment proxy: BTC's 5-day return direction. This is NOT a real
    # USDT-dominance trend (a free point-in-time dominance history isn't
    # available), so it's labeled as the BTC-momentum proxy it actually is.
    risk_sentiment = "unknown"
    btc_5d_ret = (btc_df["Close"].iloc[-1] / btc_df["Close"].iloc[-6] - 1)
    risk_sentiment = "Risk-off" if btc_5d_ret < -USDT_DOMINANCE_RISING_PCT else "Risk-on"

    if btc_trend == "UPTREND" and eth_trend == "UPTREND" and risk_sentiment == "Risk-on":
        regime = "Bullish"
    elif btc_trend == "DOWNTREND" and eth_trend == "DOWNTREND":
        regime = "Bearish"
    elif risk_sentiment == "Risk-off" and btc_trend != "UPTREND":
        regime = "High Volatility"
    else:
        regime = "Neutral"

    details = {
        "btc_trend": btc_trend, "btc_price": round(btc_price, 2),
        "eth_trend": eth_trend, "eth_price": round(eth_price, 2),
        "usdt_dominance_pct": usdt_d_now,
        "risk_sentiment_btc_5d_proxy": risk_sentiment,
        "btc_5d_return_pct": round(btc_5d_ret * 100, 2),
    }
    usdt_d_str = f"{usdt_d_now:.2f}%" if usdt_d_now is not None else "n/a"
    summary = (
        f"BTC {btc_trend.title()} (${btc_price:,.2f}), ETH {eth_trend.title()} (${eth_price:,.2f}), "
        f"USDT.D {usdt_d_str}, BTC 5d momentum: {risk_sentiment} -> Regime: {regime}"
    )
    return MacroContext("crypto", regime, details, summary)


def get_macro_context(asset_class: str) -> MacroContext:
    if asset_class == "crypto":
        return analyze_crypto_macro()
    return analyze_stock_macro()
