"""Parse structured market-analysis text (daily trade setups) into a TradeSetup.

The parser is keyword/regex driven and deliberately lenient: it only extracts
what is actually mentioned in the text, so the renderer draws nothing that the
analysis did not call for.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# plain, comma-grouped, or scientific-notation prices (e.g. 6.409e+04)
_NUM = r"\$?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][+-]?\d+)?"


def _to_float(s: str) -> float:
    return float(s.replace(",", "").replace("$", ""))


@dataclass
class TradeSetup:
    asset: str
    yf_symbol: str
    interval: str            # yfinance interval ("1d", "1wk", "1h", ...)
    tf_label: str            # label used in filename/title ("1D", "1W", "4H", ...)
    resample: str | None     # pandas resample rule when yfinance lacks the interval
    direction: str | None = None            # "LONG" / "SHORT"
    chart_type: str = "candlestick"         # "candlestick" unless text says otherwise
    emas: list[int] = field(default_factory=list)
    smas: list[int] = field(default_factory=list)
    show_rsi: bool = False
    show_macd: bool = False
    fib_zone: tuple[float, float] | None = None
    supports: list[float] = field(default_factory=list)
    resistances: list[float] = field(default_factory=list)
    entry_zone: tuple[float, float] | None = None
    targets: list[tuple[str, float]] = field(default_factory=list)  # (label, price)
    stop_loss: float | None = None
    raw_text: str = ""


_CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "USD", "PERP")


def _detect_asset(text: str) -> tuple[str, str]:
    """Return (display_ticker, yfinance_symbol)."""
    m = re.search(r"\b([A-Z0-9]{2,10}(?:USDT|USDC|BUSD|PERP))\b", text)
    if not m:
        m = re.search(r"\b([A-Z]{2,7}USD)\b", text)
    if m:
        ticker = m.group(1)
        base = ticker
        for q in _CRYPTO_QUOTES:
            if base.endswith(q):
                base = base[: -len(q)]
                break
        return ticker, f"{base}-USD"

    m = re.search(r"\b(?:ASSET|TICKER|SYMBOL)\s*[:\-–]\s*([A-Z.\-]{1,10})", text, re.I)
    if m:
        ticker = m.group(1).upper()
        return ticker, ticker
    raise ValueError("Could not detect an asset ticker in the setup text.")


def _detect_timeframe(text: str) -> tuple[str, str, str | None]:
    """Return (yfinance_interval, label, resample_rule)."""
    t = text.upper()
    m = re.search(r"\b(15M|30M|1H|2H|4H|1D|1W)\b", t)
    token = m.group(1) if m else None
    if token is None:
        if "WEEKLY" in t:
            token = "1W"
        elif re.search(r"\bDAILY\b|\bDAY TRADE\b", t):
            token = "1D"
        elif "HOURLY" in t:
            token = "1H"
        else:
            token = "1D"
    mapping = {
        "15M": ("15m", "15M", None),
        "30M": ("30m", "30M", None),
        "1H": ("1h", "1H", None),
        "2H": ("1h", "2H", "2h"),
        "4H": ("1h", "4H", "4h"),
        "1D": ("1d", "1D", None),
        "1W": ("1wk", "1W", None),
    }
    return mapping[token]


def _find_levels(text: str, keyword: str) -> list[float]:
    """Collect price levels mentioned right after `keyword` (support/resistance)."""
    levels: list[float] = []
    # gap tolerates line wraps but stops at sentence/clause boundaries (. ;)
    pattern = re.compile(
        rf"{keyword}s?[^\d;.]{{0,45}}({_NUM}(?:\s*(?:,|and|&|/)\s*{_NUM})*)", re.I
    )
    for m in pattern.finditer(text):
        for n in re.findall(_NUM, m.group(1)):
            v = _to_float(n)
            if v not in levels:
                levels.append(v)
    return levels


def _find_targets(text: str) -> list[tuple[str, float]]:
    targets: list[tuple[str, float]] = []
    seen: set[float] = set()
    pattern = re.compile(
        rf"\b(?:TP|T/?P|TAKE\s*PROFIT|TARGET)\s*(\d+)?\s*[:\-–]?\s*({_NUM})", re.I
    )
    for m in pattern.finditer(text):
        price = _to_float(m.group(2))
        if price in seen:
            continue
        seen.add(price)
        n = m.group(1) or str(len(targets) + 1)
        targets.append((f"TP{n}", price))
    return targets


def parse_setup(text: str) -> TradeSetup:
    asset, yf_symbol = _detect_asset(text)
    interval, tf_label, resample = _detect_timeframe(text)

    setup = TradeSetup(
        asset=asset,
        yf_symbol=yf_symbol,
        interval=interval,
        tf_label=tf_label,
        resample=resample,
        raw_text=text,
    )

    m = re.search(r"\b(LONG|SHORT)\b", text, re.I)
    if m:
        setup.direction = m.group(1).upper()

    if re.search(r"\bline\s+chart\b", text, re.I):
        setup.chart_type = "line"

    setup.emas = sorted({int(n) for n in re.findall(r"\bEMA[\s\-]*(\d{1,3})\b", text, re.I)})
    setup.smas = sorted({int(n) for n in re.findall(r"\b(?:SMA|MA)[\s\-]*(\d{1,3})\b", text, re.I)})
    setup.show_rsi = bool(re.search(r"\bRSI\b", text, re.I))
    setup.show_macd = bool(re.search(r"\bMACD\b", text, re.I))

    m = re.search(
        rf"(?:GOLDEN\s*ZONE|FIB(?:ONACCI)?[^\n]*?)\D{{0,25}}({_NUM})\s*(?:-|–|—|TO)\s*({_NUM})",
        text, re.I,
    )
    if m:
        lo, hi = sorted((_to_float(m.group(1)), _to_float(m.group(2))))
        setup.fib_zone = (lo, hi)

    setup.supports = _find_levels(text, "support")
    setup.resistances = _find_levels(text, "resistance")

    m = re.search(
        rf"ENTRY(?:\s*(?:ZONE|RANGE|AREA))?\s*[:\-–]?\s*({_NUM})\s*(?:-|–|—|TO)\s*({_NUM})",
        text, re.I,
    )
    if m:
        lo, hi = sorted((_to_float(m.group(1)), _to_float(m.group(2))))
        setup.entry_zone = (lo, hi)

    setup.targets = _find_targets(text)

    m = re.search(rf"\b(?:SL|STOP[\s\-]*LOSS)\b\D{{0,12}}({_NUM})", text, re.I)
    if m:
        setup.stop_loss = _to_float(m.group(1))

    return setup
