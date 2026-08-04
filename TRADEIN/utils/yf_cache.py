"""Shared, on-disk TTL cache + retry/backoff wrapper for yfinance calls.

File-based (not requests-level, e.g. `requests_cache`) on purpose: yfinance's
HTTP session uses curl_cffi browser impersonation to avoid being blocked
(see the installed `yfinance._http` module's own docstring warning about
this). Wrapping that session in a requests_cache adapter risks breaking the
impersonation and defeating the whole point of caching at all. Caching at
the function-*result* level -- after yfinance has already parsed a response
into a DataFrame/dict -- sidesteps that entirely and is transport-agnostic.

Process-safe and shared: both the Livne (TRADEIN) and Biton (backuptrade1)
pipelines point their own YF_CACHE_DIR at the same physical directory (see
each codebase's config.py), so whichever model fetches a ticker first, the
other reads it straight off disk instead of hitting Yahoo again. Writes are
atomic (temp file + os.replace) so concurrent access from both OS processes
can't corrupt an entry.
"""
from __future__ import annotations

import functools
import hashlib
import os
import pickle
import sys
import time

_MISS = object()


def _cache_path(cache_dir: str, key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, f"{digest}.pkl")


def _read_entry(cache_dir: str, key: str):
    try:
        with open(_cache_path(cache_dir, key), "rb") as f:
            return pickle.load(f)
    except (FileNotFoundError, EOFError, pickle.PickleError, OSError):
        return _MISS


def cache_get(cache_dir: str, key: str):
    """A still-fresh cached value for `key`, or the `_MISS` sentinel."""
    entry = _read_entry(cache_dir, key)
    if entry is _MISS or entry["expires_at"] < time.time():
        return _MISS
    return entry["value"]


def cache_get_stale(cache_dir: str, key: str, max_age_seconds: float):
    """An expired-but-still-on-disk value for `key`, as long as it was
    written within `max_age_seconds` -- an emergency fallback for when a
    fresh fetch fails repeatedly (e.g. a sustained YFRateLimitError), so a
    Yahoo outage degrades to slightly-stale data instead of crashing the
    caller's scan/position-refresh cycle. Returns `_MISS` if nothing usable
    is on disk."""
    entry = _read_entry(cache_dir, key)
    if entry is _MISS or (time.time() - entry["cached_at"]) > max_age_seconds:
        return _MISS
    return entry["value"]


def cache_set(cache_dir: str, key: str, value, ttl_seconds: float) -> None:
    os.makedirs(cache_dir, exist_ok=True)
    path = _cache_path(cache_dir, key)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    now = time.time()
    entry = {"cached_at": now, "expires_at": now + ttl_seconds, "value": value}
    try:
        with open(tmp_path, "wb") as f:
            pickle.dump(entry, f)
        os.replace(tmp_path, path)  # atomic on POSIX and Windows -- safe if Biton/Livne write it at once
    except OSError as exc:
        print(f"[yf_cache] failed to write cache entry (non-fatal): {exc}", file=sys.stderr)
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def cached_call(cache_dir: str, ttl_seconds: float, stale_max_age_seconds: float,
                 retry_delays: tuple[float, ...] = (2, 5, 10)):
    """Decorator: disk-cache a fetch function's return value for
    `ttl_seconds`, keyed by its call arguments (shared across the Livne/Biton
    processes via `cache_dir`). On a cache miss, retries the underlying call
    with the given backoff delays. If every attempt still fails, falls back
    to a stale (expired) cached value within `stale_max_age_seconds` instead
    of raising. Only propagates the exception if there's truly nothing usable
    cached -- so callers that already have their own try/except keep working
    exactly as before, they just see far fewer failures in practice.

    `ttl_seconds=0` disables serving a fresh cache hit (every call re-fetches)
    while still getting retry/backoff and the stale-fallback safety net --
    use this for freshness-critical calls (e.g. live position pricing) that
    must not silently serve old data under normal conditions.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            key = f"{func.__module__}.{func.__qualname__}:{args!r}:{sorted(kwargs.items())!r}"
            if ttl_seconds > 0:
                cached = cache_get(cache_dir, key)
                if cached is not _MISS:
                    return cached

            last_exc: Exception | None = None
            for attempt, delay in enumerate((0.0,) + retry_delays):
                if delay:
                    time.sleep(delay)
                try:
                    value = func(*args, **kwargs)
                    cache_set(cache_dir, key, value, ttl_seconds)
                    return value
                except Exception as exc:
                    last_exc = exc
                    rate_limited = "RateLimit" in type(exc).__name__ or "Too Many Requests" in str(exc)
                    print(
                        f"[yf_cache] {func.__name__}{args} attempt {attempt + 1}/{len(retry_delays) + 1} failed"
                        f"{' [rate limited]' if rate_limited else ''}: {exc}",
                        file=sys.stderr,
                    )

            stale = cache_get_stale(cache_dir, key, stale_max_age_seconds)
            if stale is not _MISS:
                print(
                    f"[yf_cache] {func.__name__}{args} -- serving stale cached value "
                    f"after repeated failures ({last_exc})",
                    file=sys.stderr,
                )
                return stale
            raise last_exc
        return wrapper
    return decorator
