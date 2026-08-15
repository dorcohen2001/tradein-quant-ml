"""One table showing every tenant's health.

    python scripts/fleet_status.py
    python scripts/fleet_status.py --watch 30     # refresh every 30s
    python scripts/fleet_status.py --json         # machine-readable

Answers the question the logs could not: with N tenants running, which of them
actually ran recently, which are behind schedule, how many positions each is
carrying, and where each one is sending alerts.

READ-ONLY. It opens trade stores and heartbeats for reading and writes nothing,
so it is safe to run against a live supervisor at any time.

WINDOWS CONSOLE. Output is plain ASCII by default because `cmd.exe` on a
non-UTF-8 codepage renders box-drawing characters and coloured glyphs as
mojibake. `--unicode` opts into nicer symbols once you have run `chcp 65001`.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.observability import format_age, heartbeat_age, is_stale, read_heartbeat  # noqa: E402
from core.registry import all_models  # noqa: E402
from core.settings import get_telegram_route  # noqa: E402

RUNTIME_DIR = os.path.join(REPO_ROOT, "runtime")
STATE_DIR = os.path.join(RUNTIME_DIR, "state")

# Non-terminal across every tenant's vocabulary. A superset on purpose: this is
# a dashboard, not an accounting boundary, and a status one model uses but
# another does not must still be counted as open rather than silently dropped.
OPEN_STATUSES = frozenset({"PENDING", "ACTIVE", "PARTIAL_TP1"})

HEALTH = {
    "ascii": {"ok": "OK", "stale": "STALE", "never": "-", "off": "OFF", "missing": "NO CODE"},
    "unicode": {"ok": "● ok", "stale": "▲ stale", "never": "· never", "off": "○ off", "missing": "✗ no code"},
}


def _open_trade_count(store_path: str) -> tuple[int, int, str | None]:
    """(open, total, error). Never raises -- a dashboard must render even when
    one tenant's store is unreadable, and say which one."""
    if not os.path.exists(store_path):
        return (0, 0, None)  # registered but has not traded yet
    try:
        with open(store_path, "r", encoding="utf-8") as handle:
            trades = json.load(handle)
        if not isinstance(trades, list):
            return (0, 0, "malformed")
        return (sum(1 for t in trades if t.get("current_status") in OPEN_STATUSES),
                len(trades), None)
    except (json.JSONDecodeError, OSError) as exc:
        return (0, 0, type(exc).__name__)


def collect(now: dt.datetime | None = None) -> list[dict]:
    """One row per registered tenant."""
    now = now or dt.datetime.now()
    rows = []
    for model in all_models():
        open_count, total, store_error = _open_trade_count(model.trade_store)
        beat = read_heartbeat(STATE_DIR, model.key)
        age = heartbeat_age(STATE_DIR, model.key, now)

        if not model.enabled:
            health = "off"
        elif not model.exists:
            health = "missing"
        elif age is None:
            health = "never"
        elif is_stale(STATE_DIR, model.key, model.cycle_interval_minutes, now=now):
            health = "stale"
        else:
            health = "ok"

        route = get_telegram_route(model.telegram_route, strict=model.dedicated_route)
        rows.append({
            "key": model.key,
            "label": model.label,
            "stage": model.stage,
            "enabled": model.enabled,
            "interval_minutes": model.cycle_interval_minutes,
            "last_run": (beat or {}).get("last_run"),
            "age": format_age(age),
            "last_status": (beat or {}).get("status", "-"),
            "health": health,
            "open_trades": open_count,
            "total_trades": total,
            "store_error": store_error,
            "route": model.telegram_route,
            "route_configured": route.is_configured,
            "dedicated": model.dedicated_route,
        })
    return rows


def render(rows: list[dict], symbols: dict) -> str:
    headers = ["MODEL", "NAME", "STAGE", "ON", "EVERY", "LAST RUN", "HEALTH",
               "OPEN", "TRADES", "ROUTE"]
    table = [headers]
    for r in rows:
        route = r["route"]
        if r["dedicated"]:
            route += "*" if r["route_configured"] else "* UNSET"
        elif not r["route_configured"]:
            route += " UNSET"
        table.append([
            r["key"],
            r["label"][:22],
            r["stage"],
            "yes" if r["enabled"] else "no",
            f"{r['interval_minutes']:g}m",
            r["age"],
            symbols[r["health"]],
            str(r["open_trades"]),
            str(r["total_trades"]) if not r["store_error"] else f"ERR:{r['store_error']}",
            route,
        ])

    widths = [max(len(row[i]) for row in table) for i in range(len(headers))]
    lines = []
    for index, row in enumerate(table):
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
        if index == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def summarise(rows: list[dict]) -> str:
    enabled = [r for r in rows if r["enabled"]]
    stale = [r for r in enabled if r["health"] == "stale"]
    never = [r for r in enabled if r["health"] == "never"]
    unset = [r for r in enabled if not r["route_configured"]]
    errors = [r for r in rows if r["store_error"]]

    parts = [f"{len(enabled)}/{len(rows)} enabled",
             f"{sum(r['open_trades'] for r in rows)} open positions"]
    if stale:
        parts.append(f"STALE: {', '.join(r['key'] for r in stale)}")
    if never:
        parts.append(f"never ran: {', '.join(r['key'] for r in never)}")
    if unset:
        parts.append(f"no telegram route: {', '.join(r['key'] for r in unset)}")
    if errors:
        parts.append(f"UNREADABLE STORE: {', '.join(r['key'] for r in errors)}")
    return "  |  ".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fleet status for every registered TRADEIN model")
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON and exit")
    ap.add_argument("--unicode", action="store_true",
                    help="use symbols instead of plain ASCII (run `chcp 65001` first on cmd.exe)")
    ap.add_argument("--watch", type=float, metavar="SECONDS",
                    help="refresh continuously every SECONDS until Ctrl+C")
    args = ap.parse_args()

    symbols = HEALTH["unicode" if args.unicode else "ascii"]

    if args.json:
        print(json.dumps(collect(), indent=2))
        return 0

    def draw() -> None:
        rows = collect()
        print(f"TRADEIN fleet  --  {dt.datetime.now():%Y-%m-%d %H:%M:%S}")
        print()
        print(render(rows, symbols))
        print()
        print(summarise(rows))
        print()
        print("* = dedicated Telegram route (never falls back to the shared hub)")

    if not args.watch:
        draw()
        return 0

    try:
        while True:
            # Scroll rather than clear: a cleared screen loses the previous
            # reading, and comparing two consecutive frames is how you spot a
            # tenant that has stopped advancing.
            draw()
            print("\n" + "=" * 70 + "\n")
            time.sleep(max(args.watch, 1.0))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
