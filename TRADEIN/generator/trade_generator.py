"""Actionable Output Generator.

Assembles the already-fully-decided `strategy.strategy_engine.StrategyDecision`
(MTFA + liquidity + correlation gate + RiskManager already ran in the
screener) plus macro context and news sentiment into a display `TradeSetup`,
then renders a Telegram-ready Markdown report.

The correlation gate and the 1:3/1:1.5 R:R minimums are already enforced
upstream in `strategy.strategy_engine.evaluate_setup` -- this module never
rejects a setup on those grounds, it only formats. News sentiment is
informational only (not a hard reject), per the spec's module list not
including it as a strategy gate.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

from macro.macro_analyzer import MacroContext
from news.news_analyzer import NewsResult, confluence_check
from risk.risk_manager import PositionSizeEstimate
from screener.market_screener import Candidate
from ta.order_blocks import OrderBlock, nearest_unmitigated
from utils.telegram_notifier import escape_markdown, escape_markdown_code


@dataclass
class TradeSetup:
    ticker: str
    asset_class: str
    direction: str
    entry_low: float
    entry_high: float
    stop: float
    target1: float
    target2: float
    rr1: float
    rr2: float
    tp1_close_pct: float
    breakeven_price: float
    macro_summary: str
    thesis: str
    divergence_note: str
    liquidity_note: str
    correlation_note: str
    news_label: str
    news_reason: str
    top_headlines: list[str]
    score: float
    position_size: PositionSizeEstimate
    # Structural levels for chart rendering — passed to the graphs renderer
    # directly so the alert text never has to be re-parsed.
    fib_zone: tuple[float, float] | None = None
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)
    ob_zone: tuple[float, float] | None = None


def _divergence_note(decision) -> str:
    setup = decision.setup
    div = setup.divergence
    if decision.direction == "LONG" and div.bullish:
        d = div.detail.get("bullish", {})
        return (
            f"Accumulation (bullish) RSI divergence confirmed on weekly: price low "
            f"{d.get('price_low_1')} -> {d.get('price_low_2')}, RSI low {d.get('rsi_low_1')} -> {d.get('rsi_low_2')}"
        )
    if decision.direction == "SHORT" and div.bearish:
        d = div.detail.get("bearish", {})
        return (
            f"Distribution (bearish) RSI divergence confirmed on weekly: price high "
            f"{d.get('price_high_1')} -> {d.get('price_high_2')}, RSI high {d.get('rsi_high_1')} -> {d.get('rsi_high_2')}"
        )
    return f"No RSI divergence at entry -- HTF trend ({setup.htf_trend}) confirmation used instead"


def _liquidity_note(decision) -> str:
    liq = decision.liquidity
    if liq.favored_direction is None:
        return f"No clear liquidity skew from {liq.source} data"
    agree = "agrees with" if liq.favored_direction == decision.direction else "leans against (informational only)"
    return f"{liq.source.replace('_', ' ').title()} liquidity concentration {agree} the {decision.direction} thesis"


def _ob_zone(decision) -> tuple[float, float] | None:
    ob: OrderBlock | None = nearest_unmitigated(decision.setup.order_blocks, decision.entry_price, decision.direction)
    return (ob.bottom, ob.top) if ob is not None else None


def build_trade_setup(candidate: Candidate, macro: MacroContext, news: NewsResult) -> tuple[TradeSetup, str]:
    decision = candidate.decision
    direction = decision.direction

    _, news_reason = confluence_check(direction, news)
    top_headlines = [h.title for h in news.headlines[:3]]

    setup = decision.setup
    fib_zone = (setup.itf_fib_levels.get("0.786"), setup.itf_fib_levels.get("0.5"))
    if None in fib_zone:
        fib_zone = None
    else:
        fib_zone = tuple(sorted(fib_zone))

    supports = [setup.nearest_support] if setup.nearest_support else []
    resistances = [setup.nearest_resistance] if setup.nearest_resistance else []

    trade_setup = TradeSetup(
        ticker=candidate.ticker,
        asset_class=candidate.asset_class,
        direction=direction,
        entry_low=decision.entry_low, entry_high=decision.entry_high,
        stop=decision.stop, target1=decision.target1, target2=decision.target2,
        rr1=decision.rr1, rr2=decision.rr2,
        tp1_close_pct=decision.tp1_close_pct, breakeven_price=decision.breakeven_price,
        macro_summary=macro.summary,
        thesis=setup.thesis,
        divergence_note=_divergence_note(decision),
        liquidity_note=_liquidity_note(decision),
        correlation_note=f"Regime: {macro.regime} (direction confirmed within {sorted(macro.allowed_directions())})",
        news_label=news.label,
        news_reason=news_reason,
        top_headlines=top_headlines,
        score=candidate.score,
        position_size=decision.position_size,
        fib_zone=fib_zone,
        supports=supports,
        resistances=resistances,
        ob_zone=_ob_zone(decision),
    )
    return trade_setup, "ok"


def _fmt(v: float) -> str:
    return f"{v:,.4f}" if v < 10 else f"{v:,.2f}"


def format_telegram_message(setup: TradeSetup) -> str:
    """Every dynamic (not hand-written-template) value below is escaped for
    Telegram's legacy-Markdown reserved characters before interpolation --
    `setup.thesis`/pattern names in particular come from `ta.mtfa` as
    snake_case identifiers (`pin_bar_rejection`, `trendline_support`, ...)
    whose underscores Telegram's parser reads as italic delimiters. An
    unescaped odd/mismatched count of them gets the WHOLE message rejected
    with a 400 "can't parse entities" error -- exactly what happened to a
    prior AVGO alert. See utils.telegram_notifier.escape_markdown."""
    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    direction_emoji = "🟢 LONG" if setup.direction == "LONG" else "🔴 SHORT"
    asset_label = "Crypto" if setup.asset_class == "crypto" else "Stock"

    headline_block = "\n".join(f"  - {escape_markdown(h)}" for h in setup.top_headlines) or "  - No major headlines in window"
    ob_line = f"Order Block: `{_fmt(setup.ob_zone[0])} - {_fmt(setup.ob_zone[1])}`\n" if setup.ob_zone else ""
    tp1_pct = int(setup.tp1_close_pct * 100)

    ticker = escape_markdown_code(setup.ticker)
    macro_summary = escape_markdown(setup.macro_summary)
    thesis = escape_markdown(setup.thesis)
    divergence_note = escape_markdown(setup.divergence_note)
    liquidity_note = escape_markdown(setup.liquidity_note)
    correlation_note = escape_markdown(setup.correlation_note)
    news_label = escape_markdown(setup.news_label)
    news_reason = escape_markdown(setup.news_reason)

    return f"""📊 *DAILY TRADE SETUP* — {date_str}

*Asset:* `{ticker}` ({asset_label})
*Confluence Score:* {setup.score}/100

*🌍 Macro / Correlation*
{macro_summary}
{correlation_note}

*📈 Technical Thesis (MTFA)*
{thesis}
{divergence_note}
{ob_line}
*💧 Liquidity*
{liquidity_note}

*📰 Fundamental Catalyst (informational)*
Sentiment: *{news_label}* — {news_reason}
{headline_block}

*⚙️ Trade Parameters*
Direction: *{direction_emoji}*
Entry Zone: `{_fmt(setup.entry_low)} - {_fmt(setup.entry_high)}`
Stop Loss: `{_fmt(setup.stop)}`
Target 1 ({tp1_pct}% close, R:R {setup.rr1:.2f}): `{_fmt(setup.target1)}` -> move SL to breakeven (`{_fmt(setup.breakeven_price)}`)
Target 2 (remaining {100 - tp1_pct}%, R:R {setup.rr2:.2f}): `{_fmt(setup.target2)}`
Illustrative size @ {setup.position_size.risk_pct:.0%} risk of ${setup.position_size.account_size:,.0f}: `{setup.position_size.units:,.4g}` units

⚠️ *Risk Disclaimer:* This is not financial advice. Markets are volatile and all trading involves risk of loss. Position size responsibly and do your own research (DYOR) before entering any trade.
"""
