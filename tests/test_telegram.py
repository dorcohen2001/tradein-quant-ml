"""Telegram route resolution, payload shape, and retry behaviour.

No test here touches the network: `requests.post` is replaced with a scripted
stub. The single most important property under test is the FALLBACK -- with no
per-route configuration, every route must resolve to the same destination the
system used before routing existed. If that breaks, alerts silently stop
arriving in the chat people are actually watching.
"""
from __future__ import annotations

import importlib
import os
import sys

import pytest
import requests


DEFAULTS = {"TELEGRAM_BOT_TOKEN": "TOKDEFAULT", "TELEGRAM_CHAT_ID": "-100DEFAULT",
            "TELEGRAM_BACKOFF_BASE_SECONDS": "0"}


def _reload(env: dict):
    """Reimport settings + notifier under a controlled environment.

    Every TELEGRAM_* var is cleared first, then set to exactly `env`. Empty
    strings rather than deletion for the defaults, because `load_dotenv` runs
    with override=False -- a deleted key would simply be repopulated from the
    real repo-root .env and the test would silently exercise live credentials.
    """
    for key in [k for k in os.environ if k.startswith("TELEGRAM")]:
        del os.environ[key]
    os.environ.update(env)
    for name in ("core.settings", "core.utils.telegram_notifier"):
        sys.modules.pop(name, None)
    settings = importlib.import_module("core.settings")
    notifier = importlib.import_module("core.utils.telegram_notifier")
    return settings, notifier


@pytest.fixture
def notifier():
    _settings, mod = _reload(dict(DEFAULTS))
    yield mod
    for name in ("core.settings", "core.utils.telegram_notifier"):
        sys.modules.pop(name, None)


class Response:
    def __init__(self, status_code, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": {"message_id": 42}}
        self.headers = headers or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            err = requests.exceptions.HTTPError(str(self.status_code))
            err.response = self
            raise err


def script(*responses):
    """A requests.post stub returning `responses` in order, recording calls."""
    calls = []

    def post(url, **kwargs):
        item = responses[min(len(calls), len(responses) - 1)]
        calls.append({"url": url, **kwargs})
        if isinstance(item, Exception):
            raise item
        return item

    post.calls = calls
    return post


# --------------------------------------------------------------------------
# Route resolution
# --------------------------------------------------------------------------
def test_unconfigured_routes_inherit_the_shared_default():
    """THE backward-compatibility guarantee: nothing configured means every
    model still posts exactly where it always did."""
    settings, _ = _reload(dict(DEFAULTS))
    for name in (None, "livne", "biton", "anything"):
        route = settings.get_telegram_route(name)
        assert route.chat_id == "-100DEFAULT"
        assert route.token == "TOKDEFAULT"
        assert route.is_configured is True


def test_per_route_env_var_overrides_chat_but_inherits_token():
    """One bot posting into several groups is the common case."""
    settings, _ = _reload({**DEFAULTS, "TELEGRAM_CHAT_ID_LIVNE": "-100LIVNE"})
    livne = settings.get_telegram_route("livne")
    assert livne.chat_id == "-100LIVNE"
    assert livne.token == "TOKDEFAULT"
    assert settings.get_telegram_route("biton").chat_id == "-100DEFAULT"


def test_routes_json_can_override_both_fields():
    settings, _ = _reload({
        **DEFAULTS,
        "TELEGRAM_ROUTES": '{"biton": {"chat_id": "-100B", "token": "TOKB"}}',
    })
    biton = settings.get_telegram_route("biton")
    assert (biton.chat_id, biton.token) == ("-100B", "TOKB")
    assert settings.get_telegram_route("livne").chat_id == "-100DEFAULT"


def test_json_takes_precedence_over_per_route_env():
    settings, _ = _reload({
        **DEFAULTS,
        "TELEGRAM_CHAT_ID_BITON": "-100FROM_ENV",
        "TELEGRAM_ROUTES": '{"biton": {"chat_id": "-100FROM_JSON"}}',
    })
    assert settings.get_telegram_route("biton").chat_id == "-100FROM_JSON"


def test_malformed_routes_json_degrades_instead_of_crashing():
    """A typo in one env var must not take the whole alerting path down."""
    settings, _ = _reload({**DEFAULTS, "TELEGRAM_ROUTES": "{not valid json"})
    assert settings.get_telegram_route("livne").chat_id == "-100DEFAULT"


def test_route_names_are_normalised_to_env_suffixes():
    settings, _ = _reload({**DEFAULTS, "TELEGRAM_CHAT_ID_MY_MODEL": "-100X"})
    assert settings.get_telegram_route("my-model").chat_id == "-100X"


# --------------------------------------------------------------------------
# Payload
# --------------------------------------------------------------------------
def test_message_payload_targets_the_resolved_chat(notifier, monkeypatch):
    os.environ["TELEGRAM_CHAT_ID_LIVNE"] = "-100LIVNE"
    _settings, mod = _reload({**DEFAULTS, "TELEGRAM_CHAT_ID_LIVNE": "-100LIVNE"})
    post = script(Response(200))
    monkeypatch.setattr(mod.requests, "post", post)

    result = mod.send_telegram_message("hello", route="livne")
    assert result == {"ok": True, "message_id": 42}
    assert post.calls[0]["json"]["chat_id"] == "-100LIVNE"
    assert "TOKDEFAULT" in post.calls[0]["url"]


def test_explicit_chat_id_overrides_the_route(notifier, monkeypatch):
    post = script(Response(200))
    monkeypatch.setattr(notifier.requests, "post", post)
    notifier.send_telegram_message("hi", route="livne", chat_id="-100EXPLICIT")
    assert post.calls[0]["json"]["chat_id"] == "-100EXPLICIT"


def test_reply_threading_is_passed_through(notifier, monkeypatch):
    post = script(Response(200))
    monkeypatch.setattr(notifier.requests, "post", post)
    notifier.send_telegram_message("update", reply_to_message_id=7)
    assert post.calls[0]["json"]["reply_to_message_id"] == 7


def test_unconfigured_destination_never_touches_the_network():
    _settings, mod = _reload({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""})
    def explode(*a, **k):
        raise AssertionError("network must not be touched")
    mod.requests.post = explode
    assert mod.send_telegram_message("hi") == {"ok": False, "message_id": None}
    assert mod.is_configured() is False


# --------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------
def test_success_on_first_attempt_does_not_retry(notifier, monkeypatch):
    post = script(Response(200))
    monkeypatch.setattr(notifier.requests, "post", post)
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    assert notifier.send_telegram_message("hi")["ok"] is True
    assert len(post.calls) == 1


def test_rate_limit_is_retried_and_honours_retry_after(notifier, monkeypatch):
    slept = []
    post = script(Response(429, {"ok": False}, {"Retry-After": "3"}), Response(200))
    monkeypatch.setattr(notifier.requests, "post", post)
    monkeypatch.setattr(notifier.time, "sleep", slept.append)
    assert notifier.send_telegram_message("hi")["ok"] is True
    assert len(post.calls) == 2
    assert slept == [3.0]


def test_retry_after_is_capped_so_a_live_loop_cannot_wedge(notifier, monkeypatch):
    slept = []
    post = script(Response(429, {"ok": False}, {"Retry-After": "99999"}), Response(200))
    monkeypatch.setattr(notifier.requests, "post", post)
    monkeypatch.setattr(notifier.time, "sleep", slept.append)
    notifier.send_telegram_message("hi")
    assert slept == [30.0]


def test_transient_failures_eventually_give_up(notifier, monkeypatch):
    post = script(Response(500, {"ok": False}))
    monkeypatch.setattr(notifier.requests, "post", post)
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    assert notifier.send_telegram_message("hi") == {"ok": False, "message_id": None}
    assert len(post.calls) == notifier.TELEGRAM_MAX_ATTEMPTS


def test_permanent_400_is_not_retried(notifier, monkeypatch):
    """A 'can't parse entities' 400 is a bug in the message text -- it will
    fail identically forever, so burning three attempts only delays the report."""
    post = script(Response(400, {"ok": False}))
    monkeypatch.setattr(notifier.requests, "post", post)
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    assert notifier.send_telegram_message("hi")["ok"] is False
    assert len(post.calls) == 1


def test_timeout_is_retried(notifier, monkeypatch):
    post = script(requests.exceptions.Timeout("timed out"), Response(200))
    monkeypatch.setattr(notifier.requests, "post", post)
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)
    assert notifier.send_telegram_message("hi")["ok"] is True
    assert len(post.calls) == 2


def test_missing_photo_fails_fast_without_retrying(notifier, monkeypatch):
    def explode(*a, **k):
        raise AssertionError("must not attempt an upload for an unreadable file")
    monkeypatch.setattr(notifier.requests, "post", explode)
    assert notifier.send_telegram_photo("does_not_exist.png", caption="x") == {
        "ok": False, "message_id": None,
    }


def test_oversized_caption_follow_up_goes_to_the_same_chat(notifier, monkeypatch, tmp_path):
    """The follow-up must not fall back to the default chat -- that would split
    an alert across two groups."""
    photo = tmp_path / "chart.png"
    photo.write_bytes(b"\x89PNG\r\n\x1a\n")
    post = script(Response(200), Response(200))
    monkeypatch.setattr(notifier.requests, "post", post)
    monkeypatch.setattr(notifier.time, "sleep", lambda s: None)

    notifier.send_telegram_photo(str(photo), caption="x" * 2000, chat_id="-100SPECIFIC")
    assert len(post.calls) == 2
    assert "sendPhoto" in post.calls[0]["url"]
    assert post.calls[1]["json"]["chat_id"] == "-100SPECIFIC"
