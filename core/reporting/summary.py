"""Daily and weekly Telegram summary builders + send-once state.

Layout choice: the data rows go inside a fenced ``` block. Telegram renders
that monospaced, which is the only way to get real column alignment, and it
parses NO entities inside -- so a ticker like `BRK_B` or a status like
`LOSS_SL_BE` cannot produce the "unmatched entity" 400 that has bitten this
bot before. Only a literal backtick needs neutralising there, which
`escape_markdown_code` handles. Text OUTSIDE the block (model name, dates)
still goes through `escape_markdown`.

State is FILE-persisted, not an in-process variable, because Biton's runner is
launched with `--once` by the supervisor every cycle: the process exits each time, so an
in-memory `last_daily_report_date` would reset constantly and re-send the
report on every invocation.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import tempfile

from core.utils.telegram_notifier import escape_markdown, escape_markdown_code


# ---------------------------------------------------------------------------
# Send-once state
# ---------------------------------------------------------------------------
def load_report_state(path: str) -> dict:
    """Last-sent markers. A missing or corrupt file is treated as "never sent"
    -- at worst that repeats one report, which is strictly better than
    crashing the live loop or silently suppressing every future summary."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_report_state(path: str, state: dict) -> None:
    """Atomic write (temp file + os.replace), matching the paper-trading store,
    so a crash mid-write cannot leave a truncated state file that would make
    the reports fire again."""
    store_dir = os.path.dirname(path)
    os.makedirs(store_dir, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=store_dir, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def daily_report_due(state: dict, now: dt.datetime, hour: int) -> bool:
    """True once per calendar day, at or after `hour`.

    Deliberately `>=` rather than `== hour`: the runner can be restarted, or a
    cycle can overrun the hour boundary, and an equality test would silently
    skip that day's report entirely. `>=` plus the date marker means a late
    start still reports the same day, exactly once."""
    return now.hour >= hour and state.get("last_daily_report_date") != now.date().isoformat()


def weekly_report_due(state: dict, now: dt.datetime, hour: int, weekday: int) -> bool:
    """True once on `weekday` (0=Mon .. 4=Fri), at or after `hour`."""
    return (now.weekday() == weekday and now.hour >= hour
            and state.get("last_weekly_report_date") != now.date().isoformat())


# ---------------------------------------------------------------------------
# Trade helpers
# ---------------------------------------------------------------------------
def _parse(ts: str | None) -> dt.datetime | None:
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def _changed_at(trade: dict) -> dt.datetime | None:
    """When this trade last changed status. Prefers the explicit stamp; falls
    back through exit/entry/signal dates so trades written before that field
    existed still appear in a window."""
    for key in ("last_status_change", "exit_date", "entry_date", "signal_date"):
        parsed = _parse(trade.get(key))
        if parsed is not None:
            return parsed
    return None


def _entry_price(trade: dict) -> float:
    low, high = trade["entry_zone"]
    return (low + high) / 2


def _realized_pct(trade: dict) -> float | None:
    """Realized P&L for the report.

    A fully closed trade carries `pnl_pct`. A PARTIAL_TP1 trade does not -- it
    is still open -- but it HAS banked its first leg, so the locked-in portion
    is reported rather than showing a blank. Weighted by `tp1_close_pct` so the
    number reflects the size actually closed, not the full-position move."""
    if trade.get("pnl_pct") is not None:
        return float(trade["pnl_pct"])
    if trade.get("tp1_hit") and trade.get("tp1_exit_price"):
        entry = _entry_price(trade)
        exit_p = float(trade["tp1_exit_price"])
        leg = (exit_p / entry - 1) * 100 if trade["direction"] == "LONG" else (entry / exit_p - 1) * 100
        return round(leg * float(trade.get("tp1_close_pct", 0.5)), 2)
    return None


def _fmt_price(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.2f}"


def _block(lines: list[str]) -> str:
    """Wrap rows in a fenced block: monospaced (real alignment) and parsed as
    literal text, so tickers/statuses containing `_` are safe unescaped."""
    body = "\n".join(escape_markdown_code(line) for line in lines)
    return f"```\n{body}\n```"


# ---------------------------------------------------------------------------
# Daily
# ---------------------------------------------------------------------------
def build_daily_summary(
    model_name: str, trades: list[dict], now: dt.datetime,
    closed_statuses: tuple[str, ...], update_statuses: tuple[str, ...] = (),
    window_hours: int = 24,
) -> str:
    """Trades that went ACTIVE, and trades that closed or partially closed,
    within the last `window_hours`.

    `closed_statuses` are terminal; `update_statuses` are non-terminal status
    changes still worth reporting (Livne's PARTIAL_TP1). Both are supplied by
    the caller because the two models' vocabularies do not overlap."""
    cutoff = now - dt.timedelta(hours=window_hours)
    reportable = set(closed_statuses) | set(update_statuses)

    entries, closes = [], []
    for t in trades:
        entered = _parse(t.get("entry_date"))
        if entered is not None and entered >= cutoff:
            entries.append(t)
        if t["current_status"] in reportable:
            changed = _changed_at(t)
            if changed is not None and changed >= cutoff:
                closes.append(t)

    entries.sort(key=lambda t: t.get("entry_date") or "")
    closes.sort(key=lambda t: _changed_at(t) or dt.datetime.min)

    head = f"[{escape_markdown(model_name)}] 📊 *DAILY SUMMARY* — {now.date().isoformat()}"
    parts = [head]

    parts.append(f"🟢 *New entries* ({len(entries)})")
    if entries:
        rows = [f"{'TICKER':<9}{'DIR':<6}{'ENTRY':>11}{'TP1':>11}{'TP2':>11}{'SL':>11}"]
        for t in entries:
            rows.append(
                f"{t['ticker']:<9}{t['direction']:<6}"
                f"{_fmt_price(_entry_price(t)):>11}{_fmt_price(t.get('target1')):>11}"
                f"{_fmt_price(t.get('target2')):>11}{_fmt_price(t.get('stop_loss')):>11}"
            )
        parts.append(_block(rows))
    else:
        parts.append("_none_")

    parts.append(f"🔴 *Closed / updated* ({len(closes)})")
    if closes:
        rows = [f"{'TICKER':<9}{'STATUS':<16}{'P&L %':>9}"]
        for t in closes:
            pnl = _realized_pct(t)
            rows.append(f"{t['ticker']:<9}{t['current_status']:<16}"
                        f"{(f'{pnl:+.2f}' if pnl is not None else '-'):>9}")
        parts.append(_block(rows))

        realized = [p for p in (_realized_pct(t) for t in closes) if p is not None]
        if realized:
            parts.append(f"Net realized: *{sum(realized):+.2f}%*")
    else:
        parts.append("_none_")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Weekly
# ---------------------------------------------------------------------------
def build_weekly_summary(
    model_name: str, trades: list[dict], now: dt.datetime,
    closed_statuses: tuple[str, ...], window_days: int = 7,
) -> str:
    """Aggregate performance over the last `window_days`. P&L percentages only
    -- no prices, no levels, per the reporting spec."""
    cutoff = now - dt.timedelta(days=window_days)

    closed = []
    for t in trades:
        if t["current_status"] not in closed_statuses:
            continue
        changed = _changed_at(t)
        if changed is not None and changed >= cutoff and t.get("pnl_pct") is not None:
            closed.append(t)
    closed.sort(key=lambda t: float(t["pnl_pct"]), reverse=True)

    start = (now - dt.timedelta(days=window_days)).date().isoformat()
    head = (f"[{escape_markdown(model_name)}] 📈 *WEEKLY SUMMARY* — "
            f"{start} → {now.date().isoformat()}")
    parts = [head]

    if not closed:
        parts.append("_no closed trades this week_")
        return "\n".join(parts)

    pnls = [float(t["pnl_pct"]) for t in closed]
    wins = [p for p in pnls if p > 0]
    win_rate = len(wins) / len(pnls) * 100

    parts.append(_block([
        f"{'Closed trades':<16}{len(pnls):>9}",
        f"{'Win rate':<16}{win_rate:>8.1f}%",
        f"{'Net P&L':<16}{sum(pnls):>+8.2f}%",
    ]))
    parts.append(_block([f"{t['ticker']:<9}{float(t['pnl_pct']):>+8.2f}%" for t in closed]))
    return "\n".join(parts)
