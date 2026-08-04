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


if __name__ == "__main__":
    result = send_telegram_message("✅ *TRADEIN* Telegram notifier connectivity test.")
    print("Telegram test send:", "OK" if result["ok"] else "FAILED")
