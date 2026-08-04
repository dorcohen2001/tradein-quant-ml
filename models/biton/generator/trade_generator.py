"""Actionable Output Generator.

Combines macro context, technical thesis, and news sentiment into a trade
setup with enforced minimum 1:2 risk/reward, then renders a Telegram-ready
Markdown report.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

import _bootstrap  # noqa: F401  -- puts repo root on sys.path for core.*

from config import MIN_RISK_REWARD
from core.news.news_analyzer import NewsResult, confluence_check
from core.utils.telegram_notifier import escape_markdown, escape_markdown_code
from macro.macro_analyzer import MacroContext
from screener.market_screener import Candidate


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
    macro_summary: str
    thesis: str
    news_label: str
    news_reason: str
    top_headlines: list[str]
    score: float
    # Structural levels for chart rendering — passed to the graphs renderer
    # directly so the alert text never has to be re-parsed.
    fib_zone: tuple[float, float] | None = None
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)


def _build_trade_params(candidate: Candidate, direction: str, min_rr: float = MIN_RISK_REWARD) -> dict | None:
    """Build entry/stop/target levels from real market structure. Returns None
    (rather than fabricating an unsafe stop) if the structural setup cannot
    clear the minimum risk/reward, or if the stop would fall inside the entry
    zone."""
    ta = candidate.ta
    price = ta.price
    levels = ta.levels
    fib = ta.fib_levels

    if direction == "LONG":
        entry_low, entry_high = price * 0.995, price * 1.002
        structural_stop = levels.get("nearest_support") or ta.swing_low
        stop = min(structural_stop, price * 0.999) * 0.995
        target1 = levels.get("nearest_resistance") or fib["0.0"]
        target2 = max(target1, ta.swing_high)
        if target2 <= target1:
            target2 = target1 * 1.03
        if stop >= entry_low:
            return None  # structure too tight below price to place a valid stop
    else:
        entry_low, entry_high = price * 0.998, price * 1.005
        structural_stop = levels.get("nearest_resistance") or ta.swing_high
        stop = max(structural_stop, price * 1.001) * 1.005
        target1 = levels.get("nearest_support") or fib["1.0"]
        target2 = min(target1, ta.swing_low)
        if target2 >= target1:
            target2 = target1 * 0.97
        if stop <= entry_high:
            return None  # structure too tight above price to place a valid stop

    risk = abs(price - stop)
    reward1 = abs(target1 - price)
    rr1 = reward1 / risk if risk > 0 else 0.0
    if rr1 < min_rr:
        return None  # structural target1 can't clear min R:R against structural stop

    reward2 = abs(target2 - price)
    rr2 = reward2 / risk if risk > 0 else 0.0

    return {
        "entry_low": entry_low, "entry_high": entry_high, "stop": stop,
        "target1": target1, "target2": target2, "rr1": rr1, "rr2": rr2,
    }


def build_trade_setup(candidate: Candidate, macro: MacroContext, news: NewsResult) -> tuple[TradeSetup | None, str]:
    direction = candidate.ta.direction_bias
    if direction == "NEUTRAL":
        return None, f"{candidate.ticker}: no clear directional bias from TA, skipped"

    if direction not in macro.allowed_directions():
        return None, (
            f"{candidate.ticker}: {direction} setup blocked by macro regime "
            f"'{macro.regime}' (allowed: {sorted(macro.allowed_directions())})"
        )

    ok, reason = confluence_check(direction, news)
    if not ok:
        return None, f"{candidate.ticker}: {reason}"

    params = _build_trade_params(candidate, direction)
    if params is None:
        return None, (
            f"{candidate.ticker}: structural stop/target could not satisfy minimum "
            f"1:{MIN_RISK_REWARD:.0f} risk/reward"
        )

    top_headlines = [h.title for h in news.headlines[:3]]

    ta = candidate.ta
    fib_zone = None
    if ta.in_golden_zone and ta.fib_levels:
        fib_zone = tuple(sorted((ta.fib_levels["0.618"], ta.fib_levels["0.5"])))
    supports = [ta.levels["nearest_support"]] if ta.levels.get("nearest_support") else []
    resistances = [ta.levels["nearest_resistance"]] if ta.levels.get("nearest_resistance") else []

    setup = TradeSetup(
        ticker=candidate.ticker,
        asset_class=candidate.asset_class,
        direction=direction,
        entry_low=params["entry_low"], entry_high=params["entry_high"],
        stop=params["stop"], target1=params["target1"], target2=params["target2"],
        rr1=params["rr1"], rr2=params["rr2"],
        macro_summary=macro.summary,
        thesis=candidate.ta.thesis,
        news_label=news.label,
        news_reason=reason,
        top_headlines=top_headlines,
        score=candidate.score,
        fib_zone=fib_zone,
        supports=supports,
        resistances=resistances,
    )
    return setup, "ok"


def _fmt(v: float) -> str:
    return f"{v:,.4f}" if v < 10 else f"{v:,.2f}"


def format_telegram_message(setup: TradeSetup) -> str:
    """Every dynamic value below is escaped for Telegram's legacy-Markdown
    reserved characters before interpolation -- an unescaped odd/mismatched
    count of `_ * `` [` (from a thesis string, a real news headline's
    arbitrary punctuation, etc.) leaves an unterminated formatting entity and
    gets the WHOLE message rejected with a 400 "can't parse entities" error.
    See utils.telegram_notifier.escape_markdown."""
    date_str = dt.datetime.now().strftime("%Y-%m-%d")
    direction_emoji = "🟢 LONG" if setup.direction == "LONG" else "🔴 SHORT"
    asset_label = "Crypto" if setup.asset_class == "crypto" else "Stock"

    headline_block = "\n".join(f"  - {escape_markdown(h)}" for h in setup.top_headlines) or "  - No major headlines in window"
    ticker = escape_markdown_code(setup.ticker)
    macro_summary = escape_markdown(setup.macro_summary)
    thesis = escape_markdown(setup.thesis)
    news_label = escape_markdown(setup.news_label)

    return f"""📊 *DAILY TRADE SETUP* — {date_str}

*Asset:* `{ticker}` ({asset_label})
*Confluence Score:* {setup.score}/100

*🌍 Macro Context*
{macro_summary}

*📈 Technical Thesis*
{thesis}

*📰 Fundamental Catalyst*
Sentiment: *{news_label}*
{headline_block}

*⚙️ Trade Parameters*
Direction: *{direction_emoji}*
Entry Zone: `{_fmt(setup.entry_low)} - {_fmt(setup.entry_high)}`
Target 1: `{_fmt(setup.target1)}` (R:R {setup.rr1:.2f})
Target 2: `{_fmt(setup.target2)}` (R:R {setup.rr2:.2f})
Stop Loss: `{_fmt(setup.stop)}`

⚠️ *Risk Disclaimer:* This is not financial advice. Markets are volatile and all trading involves risk of loss. Position size responsibly and do your own research (DYOR) before entering any trade.
"""
