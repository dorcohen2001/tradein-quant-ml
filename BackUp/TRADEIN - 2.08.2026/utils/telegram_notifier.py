"""Telegram Bot API notifier.

Thin wrapper around the official `sendMessage` endpoint. Uses legacy
Telegram "Markdown" parse mode (not MarkdownV2) since that's what
`generator.trade_generator.format_telegram_message` emits (single `*bold*`,
backtick code spans, no escaping of `.`/`-`/`()`).
"""
from __future__ import annotations

import requests

from config import REQUEST_TIMEOUT, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

TELEGRAM_API_BASE = "https://api.telegram.org"
TELEGRAM_CAPTION_LIMIT = 1024  # hard Bot API limit for sendPhoto captions


def is_configured() -> bool:
    return bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)


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
