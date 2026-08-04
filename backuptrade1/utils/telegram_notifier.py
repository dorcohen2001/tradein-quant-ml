"""Telegram Bot API notifier.

Thin wrapper around the official `sendMessage` endpoint. Uses legacy
Telegram "Markdown" parse mode (not MarkdownV2) since that's what
`generator.trade_generator.format_telegram_message` emits (single `*bold*`,
backtick code spans). Deliberately NOT switched to MarkdownV2: that mode
reserves a much longer character set (`_*[]()~`>#+-=|{}.!`) and would
require re-escaping every literal `*bold*`/`` `code` `` marker already
hand-written across this module's templates, for no correctness benefit --
legacy Markdown only treats `_ * `` [` specially, which `escape_markdown`/
`escape_markdown_code` below handle for any dynamic value interpolated into
a message.
"""
from __future__ import annotations

import requests

from config import REQUEST_TIMEOUT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_CAPTION_LIMIT = 1024  # hard Bot API limit for sendPhoto captions


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


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


def send_telegram_message(
    text: str,
    parse_mode: str = "Markdown",
    disable_preview: bool = True,
    reply_to_message_id: int | None = None,
) -> dict:
    """Send `text` to the configured Telegram chat.

    Pass `reply_to_message_id` to thread the message as a reply to an earlier
    alert (e.g. status updates replying to the original trade alert).

    Returns `{"ok": bool, "message_id": int | None}`. Never raises — network
    errors and an unconfigured bot both come back as `ok: False`.
    """
    if not is_configured():
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skipping send.")
        return {"ok": False, "message_id": None}

    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_preview,
    }
    if reply_to_message_id is not None:
        payload["reply_to_message_id"] = reply_to_message_id

    try:
        resp = requests.post(url, json=payload, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result") or {}
        return {"ok": data.get("ok", False), "message_id": result.get("message_id")}
    except requests.exceptions.RequestException as exc:
        print(f"[telegram] Failed to send message: {exc}")
        return {"ok": False, "message_id": None}


def send_telegram_photo(
    photo_path: str,
    caption: str | None = None,
    parse_mode: str = "Markdown",
) -> dict:
    """Send a photo to the configured chat, with `caption` as its text.

    If the caption exceeds Telegram's 1024-char sendPhoto limit, the photo is
    sent first (uncaptioned) and the full text follows immediately as a reply
    to it, so the pair stays visually threaded.

    Returns `{"ok": bool, "message_id": int | None}` where `message_id` is the
    PHOTO message's id (the right anchor for later status-update replies).
    Never raises — failures come back as `ok: False` so callers can fall back
    to a plain text alert.
    """
    if not is_configured():
        print("[telegram] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, skipping send.")
        return {"ok": False, "message_id": None}

    caption_fits = caption is not None and len(caption) <= TELEGRAM_CAPTION_LIMIT
    url = f"{TELEGRAM_API_BASE}/bot{TELEGRAM_BOT_TOKEN}/sendPhoto"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "parse_mode": parse_mode}
    if caption_fits:
        payload["caption"] = caption

    try:
        with open(photo_path, "rb") as photo:
            resp = requests.post(
                url, data=payload, files={"photo": photo}, timeout=REQUEST_TIMEOUT * 3
            )
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result") or {}
        photo_ok = data.get("ok", False)
        photo_message_id = result.get("message_id")
    except (requests.exceptions.RequestException, OSError) as exc:
        print(f"[telegram] Failed to send photo: {exc}")
        return {"ok": False, "message_id": None}

    if photo_ok and caption is not None and not caption_fits:
        follow_up = send_telegram_message(
            caption, parse_mode=parse_mode, reply_to_message_id=photo_message_id
        )
        if not follow_up["ok"]:
            print("[telegram] Photo sent but full-text follow-up failed.")

    return {"ok": photo_ok, "message_id": photo_message_id}


if __name__ == "__main__":
    result = send_telegram_message("✅ *TRADEIN* Telegram notifier connectivity test.")
    print("Telegram test send:", "OK" if result["ok"] else "FAILED")
