"""Alerts that survive a failed send.

THE PROBLEM
-----------
Position management persisted a status change and THEN sent its Telegram
alert:

    update_open_positions()   ->  store written, `changed` returned
    for trade in changed:     ->  send_telegram_message(...)

If that send failed -- three attempts, backoff, then the notifier gives up --
the transition was already on disk. On the next cycle the trade is no longer
in `changed`, because as far as the store is concerned it changed last cycle.
The alert is not delayed; it is gone, and nothing in the system knows.

`core.paper_trading.journal` already identified exactly this shape of bug for
the Excel write ("the status transition was persisted while the alert was
never sent, and the alert was lost permanently") and fixed it there by never
raising. The same reasoning was never applied to the notification itself.

THE APPROACH
------------
The intent to notify is state, so it lives with the state. A transition writes
a `pending_alert` onto the trade record in the same save that persists the
transition, and the field is cleared only after the transport confirms
delivery. Anything still pending at the top of the next cycle is retried.

That makes the store the queue. No extra file to keep in sync, no ordering
problem between two writes, and the retry survives a crash, a reboot, or the
supervisor killing the cycle mid-send -- all of which a purely in-memory queue
would lose.

BOUNDED, NOT INFINITE
---------------------
Some messages can never succeed. A 400 "can't parse entities" is a bug in the
text and will fail identically forever; retrying it every two minutes turns one
lost alert into a permanent stream of noise that trains an operator to ignore
the logs. After `MAX_ATTEMPTS` the entry is marked `abandoned`, kept on the
record for diagnosis, and stops being retried. It is deliberately not deleted:
"this alert was never delivered" is exactly the fact an operator needs.

WHY NOT ALSO QUEUE THE SETUP ALERT
----------------------------------
A new setup sends first and logs second, so a failed send leaves no state
claiming an alert went out -- a different failure (a position with no alert)
with a different fix. This module is shaped to take it later: `kind` is
carried on every entry precisely so a second producer can be added without
changing the consumer.
"""
from __future__ import annotations

import datetime as dt

FIELD = "pending_alert"

# How many cycles an undeliverable alert is retried before it is abandoned.
# Five at Livne's 2-minute cadence is ~10 minutes of transient-outage
# tolerance, which comfortably outlasts a Telegram blip while not turning a
# malformed message into a permanent retry loop.
MAX_ATTEMPTS = 5


def queue(trade: dict, kind: str = "status_update", *, now: dt.datetime | None = None) -> dict:
    """Record that `trade` owes a notification.

    Called in the SAME pass that mutates the trade, so the intent to notify is
    written by the same `save_trades` that writes the transition. Re-queuing an
    already-pending alert overwrites it: the latest status is the one worth
    sending, and a position that moved PENDING -> ACTIVE -> LOSS_SL during an
    outage should announce where it ended up, not narrate every step late.
    """
    moment = now or dt.datetime.now()
    previous = trade.get(FIELD) or {}
    trade[FIELD] = {
        "kind": kind,
        "status": trade.get("current_status"),
        "queued_at": (previous.get("queued_at")
                      or moment.isoformat(timespec="seconds")),
        "attempts": 0,
        "last_error": None,
    }
    return trade[FIELD]


def clear(trade: dict) -> None:
    """Delivery confirmed -- drop the entry."""
    trade.pop(FIELD, None)


def is_pending(trade: dict) -> bool:
    """Whether this trade owes a notification that is still worth attempting.

    An abandoned entry is not pending: it stays on the record as evidence, but
    the sender must not keep trying it.
    """
    entry = trade.get(FIELD)
    return bool(entry) and not entry.get("abandoned")


def pending(trades: list[dict]) -> list[dict]:
    """Every trade still owing a deliverable notification, oldest first.

    Ordered by queue time so a backlog is delivered in the order the
    transitions actually happened, not in whatever order the store lists them.
    """
    return sorted(
        (t for t in trades if is_pending(t)),
        key=lambda t: (t.get(FIELD) or {}).get("queued_at") or "",
    )


def record_failure(trade: dict, error: str = "", *, max_attempts: int = MAX_ATTEMPTS) -> bool:
    """Note a failed attempt. Returns True if the entry was just abandoned.

    The caller logs the abandonment; this module only decides it, so the rule
    lives in one place rather than in each runner.
    """
    entry = trade.get(FIELD)
    if not entry:
        return False
    entry["attempts"] = int(entry.get("attempts", 0)) + 1
    entry["last_error"] = error or None
    if entry["attempts"] >= max_attempts:
        entry["abandoned"] = True
        entry["abandoned_at"] = dt.datetime.now().isoformat(timespec="seconds")
        return True
    return False


def flush(trades: list[dict], send) -> dict:
    """Attempt every pending alert. Mutates the trades; never raises.

    `send(trade) -> bool` is the caller's transport -- the runner owns message
    formatting and routing, and this module deliberately knows nothing about
    either. A `send` that raises is treated as a failed attempt, because a
    transport bug must not be able to abort position management.

    Returns counts for logging: `{sent, failed, abandoned, attempted}`.
    Persisting the result is the CALLER's job: it owns the store path and
    already knows whether anything else in this pass needs saving.
    """
    result = {"attempted": 0, "sent": 0, "failed": 0, "abandoned": 0}

    for trade in pending(trades):
        result["attempted"] += 1
        try:
            ok = bool(send(trade))
            error = "" if ok else "send reported failure"
        except Exception as exc:  # noqa: BLE001 -- a transport bug must not stop the cycle
            ok, error = False, f"{type(exc).__name__}: {exc}"

        if ok:
            clear(trade)
            result["sent"] += 1
        else:
            result["failed"] += 1
            if record_failure(trade, error):
                result["abandoned"] += 1

    return result
