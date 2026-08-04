"""Render a parsed TradeSetup + OHLCV data into a dark-mode trading chart PNG."""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from core.charting.setup_parser import TradeSetup

# ----------------------------------------------------------------------------- theme
BG = "#0d1117"
GRID = "rgba(255,255,255,0.06)"
TEXT = "#d1d4dc"
MUTED = "#8b949e"
UP = "#26a69a"
DOWN = "#ef5350"
MA_COLORS = ["#f0b90b", "#4f8cff", "#c678dd", "#56b6c2", "#e5c07b"]
ENTRY_FILL = "rgba(79,140,255,0.16)"
FIB_FILL = "rgba(240,185,11,0.13)"
TP_COLOR = "#2ebd85"
SL_COLOR = "#f6465d"
SR_COLOR = "rgba(209,212,220,0.55)"

_DISPLAY_BARS = 180


def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def _macd(close: pd.Series):
    line = _ema(close, 12) - _ema(close, 26)
    signal = line.ewm(span=9, adjust=False).mean()
    return line, signal, line - signal


def _fmt(v: float) -> str:
    if v >= 1000:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:.2f}"
    return f"{v:.4g}"


def render_chart(setup: TradeSetup, df: pd.DataFrame, out_path: str) -> str:
    # ---- indicators on full history, then slice to the display window
    overlays: list[tuple[str, pd.Series, str]] = []
    for i, n in enumerate(setup.emas):
        overlays.append((f"EMA {n}", _ema(df["Close"], n), MA_COLORS[i % len(MA_COLORS)]))
    for j, n in enumerate(setup.smas):
        color = MA_COLORS[(len(setup.emas) + j) % len(MA_COLORS)]
        overlays.append((f"SMA {n}", df["Close"].rolling(n).mean(), color))

    rsi = _rsi(df["Close"]) if setup.show_rsi else None
    macd_line = macd_sig = macd_hist = None
    if setup.show_macd:
        macd_line, macd_sig, macd_hist = _macd(df["Close"])

    view = df.iloc[-_DISPLAY_BARS:]
    x = view.index

    # ---- subplot grid: price (+volume overlay) / RSI / MACD
    rows = 1 + int(setup.show_rsi) + int(setup.show_macd)
    heights = {1: [1.0], 2: [0.74, 0.26], 3: [0.60, 0.20, 0.20]}[rows]
    specs = [[{"secondary_y": True}]] + [[{"secondary_y": False}]] * (rows - 1)
    fig = make_subplots(
        rows=rows, cols=1, shared_xaxes=True,
        vertical_spacing=0.035, row_heights=heights, specs=specs,
    )

    # ---- price panel
    if setup.chart_type == "line":
        fig.add_trace(
            go.Scatter(x=x, y=view["Close"], name=setup.asset,
                       line=dict(color="#4f8cff", width=2)),
            row=1, col=1,
        )
    else:
        fig.add_trace(
            go.Candlestick(
                x=x, open=view["Open"], high=view["High"],
                low=view["Low"], close=view["Close"], name=setup.asset,
                increasing=dict(line=dict(color=UP, width=1), fillcolor=UP),
                decreasing=dict(line=dict(color=DOWN, width=1), fillcolor=DOWN),
                showlegend=False,
            ),
            row=1, col=1,
        )

    # volume histogram tucked into the bottom of the price panel
    vol_colors = [
        f"rgba(38,166,154,0.35)" if c >= o else f"rgba(239,83,80,0.35)"
        for o, c in zip(view["Open"], view["Close"])
    ]
    fig.add_trace(
        go.Bar(x=x, y=view["Volume"], marker_color=vol_colors,
               marker_line_width=0, name="Volume", showlegend=False),
        row=1, col=1, secondary_y=True,
    )
    fig.update_yaxes(
        range=[0, float(view["Volume"].max()) * 6.0],
        showticklabels=False, showgrid=False, secondary_y=True, row=1, col=1,
    )

    for name, series, color in overlays:
        fig.add_trace(
            go.Scatter(x=x, y=series.iloc[-_DISPLAY_BARS:], name=name,
                       line=dict(color=color, width=1.6)),
            row=1, col=1,
        )

    # ---- trade levels & zones (price panel only)
    ann_font = dict(color=TEXT, size=12)

    if setup.fib_zone:
        lo, hi = setup.fib_zone
        fig.add_hrect(
            y0=lo, y1=hi, fillcolor=FIB_FILL, line_width=0, row=1, col=1,
            annotation_text=f"Golden Zone {_fmt(lo)}–{_fmt(hi)}",
            annotation_position="inside top left",
            annotation_font=dict(color="#f0b90b", size=12),
        )
    if setup.entry_zone:
        lo, hi = setup.entry_zone
        fig.add_hrect(
            y0=lo, y1=hi, fillcolor=ENTRY_FILL, line_width=0, row=1, col=1,
            annotation_text=f"Entry {_fmt(lo)}–{_fmt(hi)}",
            annotation_position="inside bottom left",
            annotation_font=dict(color="#7aa5ff", size=12),
        )

    for level in setup.resistances:
        fig.add_hline(
            y=level, line=dict(color=SR_COLOR, width=1, dash="dot"), row=1, col=1,
            annotation_text=f"R {_fmt(level)}", annotation_position="top left",
            annotation_font=dict(color=MUTED, size=11),
        )
    for level in setup.supports:
        fig.add_hline(
            y=level, line=dict(color=SR_COLOR, width=1, dash="dot"), row=1, col=1,
            annotation_text=f"S {_fmt(level)}", annotation_position="bottom left",
            annotation_font=dict(color=MUTED, size=11),
        )

    for label, price in setup.targets:
        fig.add_hline(
            y=price, line=dict(color=TP_COLOR, width=1.4, dash="dash"), row=1, col=1,
            annotation_text=f"{label}  {_fmt(price)}",
            annotation_position="top right",
            annotation_font=dict(color=TP_COLOR, size=12),
        )
    if setup.stop_loss is not None:
        fig.add_hline(
            y=setup.stop_loss, line=dict(color=SL_COLOR, width=1.4, dash="dash"),
            row=1, col=1,
            annotation_text=f"SL  {_fmt(setup.stop_loss)}",
            annotation_position="top right",
            annotation_font=dict(color=SL_COLOR, size=12),
        )

    # last-price marker
    last_close = float(view["Close"].iloc[-1])
    last_up = last_close >= float(view["Open"].iloc[-1])
    fig.add_hline(
        y=last_close, line=dict(color=UP if last_up else DOWN, width=1, dash="dot"),
        opacity=0.7, row=1, col=1,
        annotation_text=_fmt(last_close), annotation_position="bottom right",
        annotation_font=dict(color=BG, size=11),
        annotation_bgcolor=UP if last_up else DOWN,
    )

    # ---- RSI panel
    row_i = 2
    if setup.show_rsi:
        fig.add_trace(
            go.Scatter(x=x, y=rsi.iloc[-_DISPLAY_BARS:], name="RSI 14",
                       line=dict(color="#c678dd", width=1.6), showlegend=False),
            row=row_i, col=1,
        )
        fig.add_hrect(y0=30, y1=70, fillcolor="rgba(198,120,221,0.05)",
                      line_width=0, row=row_i, col=1)
        for lvl in (30, 70):
            fig.add_hline(y=lvl, line=dict(color=MUTED, width=1, dash="dot"),
                          opacity=0.6, row=row_i, col=1)
        fig.update_yaxes(range=[0, 100], tickvals=[30, 50, 70],
                         title_text="RSI", row=row_i, col=1)
        row_i += 1

    # ---- MACD panel
    if setup.show_macd:
        hist = macd_hist.iloc[-_DISPLAY_BARS:]
        fig.add_trace(
            go.Bar(x=x, y=hist, name="Histogram", showlegend=False,
                   marker_color=[UP if v >= 0 else DOWN for v in hist],
                   marker_line_width=0, opacity=0.55),
            row=row_i, col=1,
        )
        fig.add_trace(
            go.Scatter(x=x, y=macd_line.iloc[-_DISPLAY_BARS:], name="MACD",
                       line=dict(color="#4f8cff", width=1.6), showlegend=False),
            row=row_i, col=1,
        )
        fig.add_trace(
            go.Scatter(x=x, y=macd_sig.iloc[-_DISPLAY_BARS:], name="Signal",
                       line=dict(color="#f0b90b", width=1.4), showlegend=False),
            row=row_i, col=1,
        )
        fig.update_yaxes(title_text="MACD", row=row_i, col=1)

    # ---- layout
    span = x[-1] - x[0]
    x_end = x[-1] + span * 0.10  # room on the right for level labels
    direction_badge = f"  ·  {setup.direction}" if setup.direction else ""
    title = (
        f"<b>{setup.asset}</b>  ·  {setup.tf_label}{direction_badge}"
        f"<br><sup style='color:{MUTED}'>Last close {_fmt(last_close)}"
        f"  ·  generated {pd.Timestamp.now():%Y-%m-%d}</sup>"
    )

    fig.update_layout(
        title=dict(text=title, font=dict(size=22, color=TEXT), x=0.02, y=0.97),
        paper_bgcolor=BG, plot_bgcolor=BG,
        font=dict(family="Segoe UI, Arial, sans-serif", color=TEXT, size=13),
        legend=dict(orientation="h", x=1.0, xanchor="right", y=1.04,
                    yanchor="bottom", bgcolor="rgba(0,0,0,0)",
                    font=dict(color=MUTED, size=12)),
        margin=dict(l=60, r=90, t=95, b=40),
        bargap=0.25,
        showlegend=True,
        xaxis_rangeslider_visible=False,
    )
    fig.update_xaxes(range=[x[0] - span * 0.01, x_end], gridcolor=GRID,
                     zeroline=False, showline=False)
    fig.update_yaxes(gridcolor=GRID, zeroline=False, side="right",
                     tickfont=dict(color=MUTED))

    # focus the price axis on the candles + trade levels; long-history MA tails
    # (e.g. an EMA 200 far above price) may clip off-screen, like on a live chart
    level_vals: list[float] = [p for _, p in setup.targets]
    level_vals += setup.supports + setup.resistances
    if setup.stop_loss is not None:
        level_vals.append(setup.stop_loss)
    for zone in (setup.fib_zone, setup.entry_zone):
        if zone:
            level_vals += list(zone)
    y_lo = min([float(view["Low"].min())] + level_vals)
    y_hi = max([float(view["High"].max())] + level_vals)
    pad = (y_hi - y_lo) * 0.05
    fig.update_yaxes(range=[y_lo - pad * 2.2, y_hi + pad],
                     row=1, col=1, secondary_y=False)
    # stocks: hide weekend gaps (crypto trades 24/7 so no breaks needed)
    if not setup.yf_symbol.endswith("-USD"):
        fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"])])

    height = {1: 860, 2: 980, 3: 1080}[rows]
    fig.write_image(out_path, width=1680, height=height, scale=2)
    return out_path
