"""Telegram Bot API notifier.

Thin wrapper around the official `sendMessage`/`sendPhoto` endpoints. Uses
legacy Telegram "Markdown" parse mode (not MarkdownV2) since that's what
`generator.trade_generator.format_telegram_message` emits (single `*bold*`,
backtick code spans). Deliberately NOT switched to MarkdownV2: that mode
reserves a much longer character set (`_*[]()~`>#+-=|{}.!`) and would
require re-escaping every literal `*bold*`/`` `code` `` marker already
hand-written across this module's templates, for no correctness benefit --
legacy Markdown only treats `_ * `` [` specially, which `escape_markdown`/
`escape_markdown_code` below handle for any dynamic value interpolated into
a message.

ROUTING
-------
Every send used to go to one hard-coded destination: the token and chat id
were bound into this module at import and neither send function took a
parameter for them. Both now accept `route` (a name resolved through
`core.settings.get_telegram_route`) or an explicit `chat_id`/`token` override.
Credentials are resolved at CALL time rather than import time.

With no per-route environment configured, every route resolves to the same
default token/chat that was previously hard-coded -- so an unconfigured
deployment behaves exactly as before.

RETRY
-----
A failed send used to be dropped after a single attempt, which for a trading
alert means the notification simply never arrives. Sends are now retried with
exponential backoff, and a 429 is honoured via its `Retry-After` header rather
than being hammered (which is what escalates a rate-limit into a ban).

Only transport-level and rate-limit failures are retried. A 400 "can't parse
entities" is a bug in the message text and will fail identically forever, so
it is reported immediately instead of burning three attempts on it.
"""
from __future__ import annotations

import time

import requests

from core.settings import (
    REQUEST_TIMEOUT,
    TELEGRAM_BACKOFF_BASE_SECONDS,
    TELEGRAM_MAX_ATTEMPTS,
    get_telegram_route,
)

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_CAPTION_LIMIT = 1024  # hard Bot API limit for sendPhoto captions

# Status codes worth another attempt. 429 is rate limiting; 5xx is Telegram
# having a bad moment. Everything else (notably 400 and 403) is a permanent
# property of this request and retrying only delays the failure report.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def is_configured(route: str | None = None) -> bool:
    """Whether `route` has both a token and a chat id. Kept parameterless-safe:
    existing callers pass nothing and get the default route, as before."""
    return get_telegram_route(route).is_configured


def escape_markdown(text) -> str:
    """Escape this bot's legacy-Markdown reserved characters (`_ * `` [`) in
    a dynamic value before interpolating it into PLAIN or `*bold*` message
    text (NOT inside a `` `code` `` span -- see `escape_markdown_code` for
    that). Without this, a value containing an odd/unmatched count of any of
    these -- e.g. the snake_case pattern/zone names `ta.mtfa` emits
    (`pin_bar_rejection`, `trendline_support`), a status like `PARTIAL_TP1`,
    or a real news headline with arbitrary punctuation -- leaves an
    unterminated formatting entity and Telegram rejects the ENTIRE message
    with a 400 "can't parse entities" error, exactly what happened to the
    AVGO alert's follow-up text (a lone `_` inside `pin_bar_rejection` had no
    matching partner once combined with `trendline_support`'s single `_`)."""
    s = str(text).replace("\\", "\\\\")
    for ch in ("_", "*", "`", "["):
        s = s.replace(ch, f"\\{ch}")
    return s


def escape_markdown_code(text) -> str:
    """Escape a dynamic value that will be placed INSIDE a `` `...` `` code
    span. Telegram's legacy Markdown does not parse nested entities inside
    code/pre, so only a literal backtick or backslash needs escaping there --
    running the plain-text `escape_markdown` on it instead would incorrectly
    render a visible backslash in front of any underscore/asterisk (e.g. a
    ticker like `BRK_B` would show as `BRK\\_B`)."""
    s = str(text).replace("\\", "\\\\")
    return s.replace("`", "\\`")


def _backoff_seconds(attempt: int, response: requests.Response | None) -> float:
    """How long to wait before the next attempt.

    Telegram tells you exactly how long to wait on a 429; obeying that header
    is the difference between recovering and getting the bot IP-banned. Absent
    a header, back off exponentially from the configured base.
    """
    if response is not None and response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                # Cap it: a pathological header should not wedge a live trading
                # loop for minutes while positions go unmanaged.
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        payload = {}
        try:
            payload = response.json().get("parameters") or {}
        except ValueError:
            pass
        if payload.get("retry_after"):
            return min(float(payload["retry_after"]), 30.0)
    return TELEGRAM_BACKOFF_BASE_SECONDS * (2 ** attempt)


def _post(url: str, *, data: dict, files: dict | None = None, timeout: int) -> dict:
    """POST with retry/backoff. Returns `{"ok": bool, "message_id": int|None}`
    and never raises -- callers are alerting paths that must degrade, not
    crash, and several of them fall back to a plain-text send on failure.

    `files` is re-opened by the caller for each attempt via a factory, because
    a file object consumed by a failed upload cannot be replayed.
    """
    last_detail = "no attempt made"

    for attempt in range(TELEGRAM_MAX_ATTEMPTS):
        response = None
        try:
            response = requests.post(
                url, data=data, files=files() if callable(files) else files, timeout=timeout,
            )
            if response.status_code not in _RETRYABLE_STATUS:
                response.raise_for_status()
                body = response.json()
                result = body.get("result") or {}
                return {"ok": bool(body.get("ok", False)), "message_id": result.get("message_id")}
            last_detail = f"HTTP {response.status_code}"
        except requests.exceptions.RequestException as exc:
            last_detail = str(exc)
            # A 4xx that is not rate limiting will never succeed on retry --
            # report it now rather than sleeping through two more attempts.
            status = getattr(exc.response, "status_code", None)
            if status is not None and status not in _RETRYABLE_STATUS:
                print(f"[telegram] permanent failure ({status}): {exc}")
                return {"ok": False, "message_id": None}
        except (ValueError, OSError) as exc:  # malformed JSON body, unreadable photo
            last_detail = str(exc)

        if attempt < TELEGRAM_MAX_ATTEMPTS - 1:
            delay = _backoff_seconds(attempt, response)
            print(f"[telegram] attempt {attempt + 1}/{TELEGRAM_MAX_ATTEMPTS} failed "
                  f"({last_detail}); retrying in {delay:.1f}s")
            time.sleep(delay)

    print(f"[telegram] giving up after {TELEGRAM_MAX_ATTEMPTS} attempts: {last_detail}")
    return {"ok": False, "message_id": None}


def send_telegram_message(
    text: str,
    parse_mode: str = "Markdown",
    disable_preview: bool = True,
    reply_to_message_id: int | None = None,
    route: str | None = None,
    chat_id: str | None = None,
    token: str | None = None,
) -> dict:
    """Send `text` to a Telegram chat.

    `route` names a destination from `core.settings` (each model declares its
    own in `core.registry`); `chat_id`/`token` override it outright. All three
    default to the historical single-destination behaviour.

    Pass `reply_to_message_id` to thread the message as a reply to an earlier
    alert (e.g. status updates replying to the original trade alert).

    Returns `{"ok": bool, "message_id": int | None}`. Never raises — network
    errors and an unconfigured bot both come back as `ok: False`.
    """
    resolved = get_telegram_route(route)
    token = token or resolved.token
    chat_id = chat_id or resolved.chat_id
    if not (token and chat_id):
        print(f"[telegram] route {resolved.name!r} has no token/chat id set, skipping send.")
        return {"ok": False, "message_id": None}

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    return _post(f"{TELEGRAM_API_BASE}/bot{token}/sendMessage", data=payload, timeout=REQUEST_TIMEOUT)


def send_telegram_photo(
    photo_path: str,
    caption: str | None = None,
    parse_mode: str = "Markdown",
    route: str | None = None,
    chat_id: str | None = None,
    token: str | None = None,
) -> dict:
    """Send a photo to the chat, with `caption` as its text.

    If the caption exceeds Telegram's 1024-char sendPhoto limit, the photo is
    sent first (uncaptioned) and the full text follows immediately as a reply
    to it, so the pair stays visually threaded -- and the follow-up goes to the
    SAME resolved destination, not back to the default.

    Returns `{"ok": bool, "message_id": int | None}` where `message_id` is the
    PHOTO message's id (the right anchor for later status-update replies).
    Never raises — failures come back as `ok: False` so callers can fall back
    to a plain text alert.
    """
    resolved = get_telegram_route(route)
    token = token or resolved.token
    chat_id = chat_id or resolved.chat_id
    if not (token and chat_id):
        print(f"[telegram] route {resolved.name!r} has no token/chat id set, skipping send.")
        return {"ok": False, "message_id": None}

    caption_fits = caption is not None and len(caption) <= TELEGRAM_CAPTION_LIMIT
    payload = {"chat_id": chat_id, "parse_mode": parse_mode}
    if caption_fits:
        payload["caption"] = caption

    # Fail fast on an unreadable file rather than letting the retry loop burn
    # three attempts (with backoff) on a condition that cannot improve -- the
    # caller is a live alert path that falls back to a text-only send, and it
    # should get that answer immediately.
    try:
        with open(photo_path, "rb"):
            pass
    except OSError as exc:
        print(f"[telegram] could not open photo {photo_path}: {exc}")
        return {"ok": False, "message_id": None}

    # A factory, not an open handle: an upload that failed mid-stream has
    # already consumed the file object, so each retry needs a fresh one.
    def _photo_file():
        return {"photo": open(photo_path, "rb")}

    result = _post(
        f"{TELEGRAM_API_BASE}/bot{token}/sendPhoto",
        data=payload, files=_photo_file, timeout=REQUEST_TIMEOUT * 3,
    )

    if result["ok"] and caption is not None and not caption_fits:
        follow_up = send_telegram_message(
            caption, parse_mode=parse_mode, reply_to_message_id=result["message_id"],
            chat_id=chat_id, token=token,
        )
        if not follow_up["ok"]:
            print("[telegram] Photo sent but full-text follow-up failed.")

    return result


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Telegram notifier connectivity test")
    ap.add_argument("--route", default=None, help="route name to test (default: the shared default route)")
    args = ap.parse_args()

    resolved = get_telegram_route(args.route)
    print(f"Route {resolved.name!r}: configured={resolved.is_configured} "
          f"chat_id={'set' if resolved.chat_id else 'MISSING'} "
          f"token={'set' if resolved.token else 'MISSING'}")
    outcome = send_telegram_message(
        f"✅ *TRADEIN* Telegram notifier connectivity test (route: {escape_markdown(resolved.name)}).",
        route=args.route,
    )
    print("Telegram test send:", "OK" if outcome["ok"] else "FAILED")
