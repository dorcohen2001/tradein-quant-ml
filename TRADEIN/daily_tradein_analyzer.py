"""Daily Paper-Trading Analyzer, Report Generator & Email Dispatcher ("Steve").

Pipeline (each stage isolated and logged to analyzer.log next to this script):
  1. ANALYZE  — compute aggregate metrics from paper_trading/trades.json (live)
                and backtest_suite/trades_log.csv (simulated), in Python.
  2. REPORT   — if at least MIN_CLOSED_TRADES_FOR_LLM live trades are closed,
                send ONLY the compact metrics JSON (~1-2 KB) to the Anthropic
                API for a strategic narrative. Below the gate — the normal
                case early on — skip the LLM entirely and emit a short
                deterministic status report. Never attaches raw trade files,
                so token cost does not grow with history size.
  3. ARCHIVE  — save as report_YYYY-MM-DD.md in MailToMail/Scripts (overwrite;
                the job is idempotent).
  4. EMAIL    — send via MailToMail/send_email.py (subject
                "TRADEIN Daily Report — YYYY-MM-DD", .md attached,
                executive summary in the body).

Usage:
    python daily_tradein_analyzer.py                 # full run
    python daily_tradein_analyzer.py --no-email      # skip the email stage
    python daily_tradein_analyzer.py --no-llm        # force the deterministic report
    python daily_tradein_analyzer.py --min-closed 5  # override the sample gate

Exit codes: 0 = success, 1 = a stage failed (Task Scheduler surfaces this).
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import logging
import os
import statistics
import sys
from logging.handlers import RotatingFileHandler

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from config import (
    ANTHROPIC_API_KEY,
    ANTHROPIC_MODEL,
    BACKTEST_TRADES_CSV,
    MAILTOMAIL_DIR,
    MIN_CLOSED_TRADES_FOR_LLM,
    REPORT_ARCHIVE_DIR,
)
from paper_trading.tracker import get_all_trades

LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "analyzer.log")

logger = logging.getLogger("daily_tradein_analyzer")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_PATH, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
logger.addHandler(logging.StreamHandler(sys.stderr))

CLOSED_WITH_PNL = ("WIN_TP1", "WIN_TP2", "LOSS_SL")
WIN_STATUSES = ("WIN_TP1", "WIN_TP2")

LLM_SYSTEM_PROMPT = """\
You are a senior trading-strategy analyst reviewing a systematic paper-trading
program. You receive a JSON payload of pre-computed performance aggregates for
two samples: "live" (real-time paper trades) and "backtest" (historical
simulation of the exact same signal logic; note its entry-fill assumption is
slightly optimistic).

Write a daily strategy report in Markdown with exactly this structure:
# TRADEIN Daily Report — {date}
## Executive Summary  (3-5 sentences, blunt, decision-oriented)
## Performance Snapshot  (key numbers from BOTH samples, clearly labeled live vs. simulated)
## Weaknesses & Loss Patterns  (grounded ONLY in the supplied numbers)
## Strategic Proposals  (ranked by expected impact; bold high-level ideas —
   new indicator families, risk-management frameworks, macro filters, revised
   entry/exit logic, position sizing — each tied to a specific weakness above)
## Concrete Next Actions  (numbered, testable steps)

Hard rules:
- STRICTLY NO CODE: no code blocks, no snippets, no syntax. Plain prose only.
- Use ONLY numbers present in the payload. If a metric is null or absent, say
  it could not be computed — never invent a value.
- If the live sample is small, say so and weight the backtest sample instead.
- Keep it under ~900 words."""


# ---------------------------------------------------------------------------
# Stage 1 — metrics
# ---------------------------------------------------------------------------
def _pnl_stats(pnls: list[float]) -> dict:
    if not pnls:
        return {}
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    stats = {
        "trades_with_pnl": len(pnls),
        "win_rate_pct": round(len(wins) / len(pnls) * 100, 1),
        "avg_win_pct": round(statistics.mean(wins), 2) if wins else None,
        "avg_loss_pct": round(statistics.mean(losses), 2) if losses else None,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss > 0 else None,
        "expectancy_pct_per_trade": round(statistics.mean(pnls), 2),
        "total_return_pct_sum": round(sum(pnls), 2),
        "best_trade_pct": round(max(pnls), 2),
        "worst_trade_pct": round(min(pnls), 2),
    }
    # Per-trade risk-adjusted ratio and drawdown only when the sample supports it
    if len(pnls) >= 10 and statistics.pstdev(pnls) > 0:
        stats["per_trade_sharpe"] = round(statistics.mean(pnls) / statistics.pstdev(pnls), 2)
        downside = [p for p in pnls if p < 0]
        if downside and statistics.pstdev(downside) > 0:
            stats["per_trade_sortino"] = round(
                statistics.mean(pnls) / statistics.pstdev(downside), 2)
    equity = peak = max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    stats["max_drawdown_pct_points"] = round(max_dd, 2)
    streak = worst_streak = 0
    for p in pnls:
        streak = streak + 1 if p <= 0 else 0
        worst_streak = max(worst_streak, streak)
    stats["max_consecutive_losses"] = worst_streak
    return stats


def _group_stats(rows: list[dict], key: str) -> dict:
    groups: dict[str, list[float]] = {}
    for r in rows:
        groups.setdefault(r[key], []).append(r["pnl"])
    return {
        k: {"trades": len(v),
            "win_rate_pct": round(sum(1 for p in v if p > 0) / len(v) * 100, 1),
            "avg_pnl_pct": round(statistics.mean(v), 2)}
        for k, v in sorted(groups.items())
    }


def compute_live_metrics() -> dict:
    trades = get_all_trades()
    by_status: dict[str, int] = {}
    for t in trades:
        by_status[t["current_status"]] = by_status.get(t["current_status"], 0) + 1

    closed = [t for t in trades
              if t["current_status"] in CLOSED_WITH_PNL and t.get("pnl_pct") is not None]
    closed.sort(key=lambda t: t.get("exit_date") or t["signal_date"])
    rows = [{"pnl": float(t["pnl_pct"]), "ticker": t["ticker"],
             "asset_class": t["asset_class"], "direction": t["direction"]} for t in closed]
    pnls = [r["pnl"] for r in rows]

    metrics = {
        "sample": "live paper trades (trades.json)",
        "total_signals": len(trades),
        "status_counts": by_status,
        "closed_trades": len(closed),
        **_pnl_stats(pnls),
    }
    if rows:
        metrics["by_asset_class"] = _group_stats(rows, "asset_class")
        metrics["by_direction"] = _group_stats(rows, "direction")
        metrics["recent_closed_trades"] = [
            {"ticker": t["ticker"], "direction": t["direction"],
             "status": t["current_status"], "pnl_pct": t["pnl_pct"],
             "signal_date": t["signal_date"][:10],
             "exit_date": (t.get("exit_date") or "")[:10]}
            for t in closed[-10:]
        ]
    return metrics


def compute_backtest_metrics() -> dict | None:
    if not os.path.exists(BACKTEST_TRADES_CSV):
        logger.info("No backtest CSV at %s — skipping backtest sample.", BACKTEST_TRADES_CSV)
        return None
    rows = []
    with open(BACKTEST_TRADES_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                rows.append({"pnl": float(row["pnl_pct"]), "ticker": row["ticker"],
                             "asset_class": row["asset_class"],
                             "direction": row["direction"], "outcome": row["outcome"]})
            except (KeyError, ValueError):
                continue
    if not rows:
        return None
    pnls = [r["pnl"] for r in rows]
    outcomes: dict[str, int] = {}
    for r in rows:
        outcomes[r["outcome"]] = outcomes.get(r["outcome"], 0) + 1
    return {
        "sample": "simulated backtest signals (trades_log.csv; entry fill assumed at "
                  "signal-date mid-entry — slightly optimistic vs. live)",
        "total_signals": len(rows),
        "outcome_counts": outcomes,
        **_pnl_stats(pnls),
        "by_asset_class": _group_stats(rows, "asset_class"),
        "by_direction": _group_stats(rows, "direction"),
    }


# ---------------------------------------------------------------------------
# Stage 2 — report generation
# ---------------------------------------------------------------------------
def _metrics_markdown(title: str, m: dict | None) -> str:
    if not m:
        return f"### {title}\n\nNo data available.\n"
    lines = [f"### {title}", ""]
    for k, v in m.items():
        if k in ("recent_closed_trades",):
            continue
        if isinstance(v, dict):
            lines.append(f"- **{k}**:")
            lines.extend(f"  - {gk}: {gv}" for gk, gv in v.items())
        else:
            lines.append(f"- **{k}**: {v}")
    lines.append("")
    return "\n".join(lines)


def build_deterministic_report(live: dict, backtest: dict | None, gate_note: str) -> str:
    today = dt.date.today().isoformat()
    return (
        f"# TRADEIN Daily Report — {today}\n\n"
        f"## Executive Summary\n\n{gate_note}\n\n"
        f"## Performance Snapshot\n\n"
        + _metrics_markdown("Live paper trades", live)
        + "\n" + _metrics_markdown("Backtest (simulated)", backtest)
    )


def generate_llm_report(live: dict, backtest: dict | None) -> str | None:
    """Compact-payload Anthropic call. Returns the narrative Markdown, or None
    on any failure (missing key/SDK, API error, safety refusal) so the caller
    can fall back to the deterministic report."""
    if not ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — falling back to deterministic report.")
        return None
    try:
        import anthropic
    except ImportError:
        logger.warning("anthropic SDK not installed (pip install anthropic) — "
                       "falling back to deterministic report.")
        return None

    payload = {
        "report_date": dt.date.today().isoformat(),
        "live": live,
        "backtest": backtest,
    }
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        # Server-side refusal fallback enabled: if the model's safety
        # classifiers decline (unlikely for trading metrics, but possible),
        # the API retries on Anthropic's recommended fallback model.
        response = client.beta.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=LLM_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": json.dumps(payload, indent=1)}],
        )
        if response.stop_reason == "refusal":
            logger.warning("LLM declined the request (stop_reason=refusal).")
            return None
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        if not text:
            logger.warning("LLM returned no text content.")
            return None
        usage = response.usage
        logger.info("LLM report generated (%s): in=%s out=%s tokens.",
                    response.model, usage.input_tokens, usage.output_tokens)
        return text
    except Exception:
        logger.exception("LLM call failed — falling back to deterministic report.")
        return None


def build_report(live: dict, backtest: dict | None, min_closed: int, no_llm: bool) -> str:
    closed = live.get("closed_trades", 0)

    if closed < min_closed:
        gate_note = (
            f"Only **{closed}** live paper trades are closed — below the "
            f"{min_closed}-trade minimum for meaningful strategy analysis, so the "
            f"LLM strategy review was skipped (this is the expected path early on; "
            f"it saves the daily API cost until the sample is worth analyzing). "
            f"The computed aggregates for both samples are below."
        )
        logger.info("Sample gate: %d closed < %d — deterministic report.", closed, min_closed)
        return build_deterministic_report(live, backtest, gate_note)

    if no_llm:
        return build_deterministic_report(
            live, backtest, "LLM narrative disabled via --no-llm; metrics only.")

    narrative = generate_llm_report(live, backtest)
    if narrative is None:
        return build_deterministic_report(
            live, backtest,
            "The LLM strategy review could not be generated today (see analyzer.log); "
            "the computed metrics are below.")

    # Append a deterministic data appendix so the archived report always
    # carries the exact computed numbers, independent of the narrative.
    appendix = (
        "\n\n---\n\n## Data Appendix (computed, not LLM-generated)\n\n"
        + _metrics_markdown("Live paper trades", live)
        + "\n" + _metrics_markdown("Backtest (simulated)", backtest)
    )
    return narrative + appendix


# ---------------------------------------------------------------------------
# Stage 3 & 4 — archive + email
# ---------------------------------------------------------------------------
def archive_report(report_md: str) -> str:
    os.makedirs(REPORT_ARCHIVE_DIR, exist_ok=True)
    path = os.path.join(REPORT_ARCHIVE_DIR, f"report_{dt.date.today().isoformat()}.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(report_md)
    logger.info("Report archived to %s", path)
    return path


def _executive_summary(report_md: str) -> str:
    """First ~15 lines after the Executive Summary heading, for the email body."""
    lines = report_md.splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.lstrip("# ").startswith("Executive Summary")) + 1
    except StopIteration:
        return "\n".join(lines[:15])
    body = []
    for l in lines[start:]:
        if l.startswith("#"):
            break
        body.append(l)
        if len(body) >= 15:
            break
    return "\n".join(body).strip() or "See attached report."


def email_report(report_path: str, report_md: str) -> bool:
    if MAILTOMAIL_DIR not in sys.path:
        sys.path.append(MAILTOMAIL_DIR)
    from send_email import send_report_email

    subject = f"TRADEIN Daily Report — {dt.date.today().isoformat()}"
    body = (_executive_summary(report_md)
            + "\n\n(Full report attached; archived copy in MailToMail/Scripts.)")
    return send_report_email(subject, body, attachments=[report_path])


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="TRADEIN daily paper-trading analyzer")
    parser.add_argument("--no-email", action="store_true", help="skip the email stage")
    parser.add_argument("--no-llm", action="store_true", help="skip the LLM narrative")
    parser.add_argument("--min-closed", type=int, default=MIN_CLOSED_TRADES_FOR_LLM,
                        help="minimum closed live trades before calling the LLM "
                             "(default: %(default)s)")
    args = parser.parse_args()

    logger.info("=== daily_tradein_analyzer start ===")
    failed = False

    # Stage 1 — analyze
    try:
        live = compute_live_metrics()
        logger.info("Live metrics: %d signals, %d closed.",
                    live["total_signals"], live["closed_trades"])
    except Exception:
        logger.exception("Live metrics stage failed.")
        live = {"error": "live metrics could not be computed (see analyzer.log)"}
        failed = True
    try:
        backtest = compute_backtest_metrics()
    except Exception:
        logger.exception("Backtest metrics stage failed.")
        backtest = None
        failed = True

    # Stage 2 — report (build_report never raises; it degrades internally)
    report_md = build_report(live, backtest, args.min_closed, args.no_llm)

    # Stage 3 — archive whatever was produced
    try:
        report_path = archive_report(report_md)
    except Exception:
        logger.exception("Archive stage failed.")
        return 1

    # Stage 4 — email
    if args.no_email:
        logger.info("--no-email: skipping email stage.")
    else:
        try:
            if email_report(report_path, report_md):
                logger.info("Email sent.")
            else:
                logger.error("Email stage reported failure.")
                failed = True
        except Exception:
            logger.exception("Email stage failed.")
            failed = True

    logger.info("=== daily_tradein_analyzer done (failed=%s) ===", failed)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
