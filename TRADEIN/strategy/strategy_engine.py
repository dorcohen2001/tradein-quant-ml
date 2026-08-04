"""StrategyEngine: ties MTFA, the liquidity filter, the correlation gate, and
RiskManager together into a single go/no-go trade decision.

`evaluate_setup()` is the per-ticker replacement for the old single-timeframe
"has a setup" check -- it runs the expensive MTFA pass first, then gates on
the macro-context correlation rule for whatever direction MTFA actually
found (cheaper than gating up front, since direction isn't known until MTFA
resolves it), then hands off to RiskManager for structural SL/TP and the
minimum-R:R rule.
"""
from __future__ import annotations

from dataclasses import dataclass

from config import MAX_SLIPPAGE_PCT, TP1_CLOSE_PCT
from liquidity.orderbook import LiquidityContext, get_liquidity_context
from macro.macro_analyzer import MacroContext
from risk.risk_manager import PositionSizeEstimate, compute_stop, compute_targets, position_size_estimate, validate_min_rr
from ta.mtfa import MTFASetup, analyze_mtfa
from utils.data_fetch import get_stock_history


@dataclass
class StrategyDecision:
    ticker: str
    asset_class: str
    direction: str
    entry_low: float
    entry_high: float
    entry_price: float
    stop: float
    target1: float
    target2: float
    rr1: float
    rr2: float
    tp1_close_pct: float
    breakeven_price: float
    setup: MTFASetup
    liquidity: LiquidityContext
    position_size: PositionSizeEstimate


def entry_zone(direction: str, price: float) -> tuple[float, float]:
    if direction == "LONG":
        return price * 0.995, price * 1.002
    return price * 0.998, price * 1.005


def evaluate_setup(ticker: str, asset_class: str, macro: MacroContext) -> tuple[StrategyDecision | None, str]:
    setup = analyze_mtfa(ticker, asset_class)
    if setup is None:
        return None, f"{ticker}: no qualifying HTF zone + ITF trigger confluence"

    direction = setup.direction_bias
    if direction not in macro.allowed_directions():
        return None, (
            f"{ticker}: {direction} setup blocked by macro regime "
            f"'{macro.regime}' (allowed: {sorted(macro.allowed_directions())})"
        )

    entry_low, entry_high = entry_zone(direction, setup.price)
    entry_price = (entry_low + entry_high) / 2

    stop = compute_stop(direction, setup, entry_price)
    if stop is None:
        return None, f"{ticker}: no valid structural stop available for {direction}"

    targets = compute_targets(direction, setup, entry_price, stop)
    target1, target2 = targets["target1"], targets["target2"]
    if target1 is None or target2 is None:
        return None, f"{ticker}: could not derive targets (zero-risk stop)"

    ok, rr1, rr2 = validate_min_rr(entry_price, stop, target1, target2)
    if not ok:
        return None, f"{ticker}: structural R:R below minimum (TP1 {rr1}, TP2 {rr2})"

    volume_df = None
    if asset_class == "stocks":
        volume_df = get_stock_history(ticker, period="1y", interval="1d")
    liquidity = get_liquidity_context(ticker, asset_class, entry_price, df=volume_df)

    breakeven_price = entry_price * (1 + MAX_SLIPPAGE_PCT) if direction == "LONG" else entry_price * (1 - MAX_SLIPPAGE_PCT)
    sizing = position_size_estimate(entry_price, stop)

    decision = StrategyDecision(
        ticker=ticker, asset_class=asset_class, direction=direction,
        entry_low=entry_low, entry_high=entry_high, entry_price=entry_price,
        stop=stop, target1=target1, target2=target2, rr1=rr1, rr2=rr2,
        tp1_close_pct=TP1_CLOSE_PCT, breakeven_price=breakeven_price,
        setup=setup, liquidity=liquidity, position_size=sizing,
    )
    return decision, "ok"
