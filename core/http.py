"""One shared HTTP client: timeouts, retries with backoff, caching, and stale fallback.

Every fetch returns a ``Fetched`` that says how old the data is and whether it is a stale copy
served because the live request failed, so answers can always state their source and age.
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx

from .cache import get_cache

logger = logging.getLogger(__name__)

USER_AGENT = "(better-weather Hermes plugin, github.com/jarretcoon/better-weather)"
TIMEOUT = httpx.Timeout(connect=6.0, read=15.0, write=10.0, pool=10.0)
RETRY_STATUSES = {429, 500, 502, 503, 504}
ATTEMPTS = 3
# While someone waits on a chat answer (report.py's fetches): two shorter tries, so a hanging service gives up in
# ~20 s rather than ~60. Everything else, like the cron warning pushes and background refreshes, keeps the above.
INTERACTIVE_TIMEOUT = httpx.Timeout(connect=4.0, read=10.0, write=10.0, pool=5.0)
INTERACTIVE_ATTEMPTS = 2
_patience: ContextVar[tuple[int, httpx.Timeout]] = ContextVar("better_weather_patience", default=(ATTEMPTS, TIMEOUT))

# TTLs (seconds) by kind of data; callers pick one.
TTL_ALERTS = 60
TTL_OBS = 5 * 60
TTL_FORECAST = 30 * 60
TTL_OUTLOOK = 10 * 60
TTL_MODEL = 15 * 60
TTL_STATIONS = 7 * 24 * 3600
TTL_POINT = 30 * 24 * 3600
TTL_GEOCODE = 30 * 24 * 3600

_DAY = 24 * 3600


class FetchError(Exception):
    def __init__(self, url: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


@dataclass(frozen=True)
class Fetched:
    data: Any
    url: str
    fetched_at: float
    stale: bool = False
    status: int = 200

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at)

    @property
    def found(self) -> bool:
        return self.status == 200 and self.data is not None


_client: httpx.Client | None = None
_client_lock = threading.Lock()
_transport: httpx.BaseTransport | None = None
_sleep = time.sleep


def _get_client() -> httpx.Client:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json, application/json"},
                timeout=TIMEOUT,
                follow_redirects=True,
                transport=_transport,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
            )
    return _client


def set_transport_for_tests(transport: httpx.BaseTransport | None, *, no_sleep: bool = True) -> None:
    """Route every request through *transport* (an ``httpx.MockTransport`` in tests)."""
    global _client, _transport, _sleep
    with _client_lock:
        if _client is not None:
            _client.close()
        _client = None
        _transport = transport
    _sleep = (lambda _s: None) if no_sleep else time.sleep


def cache_key(url: str, params: Mapping[str, Any] | None) -> str:
    if not params:
        return url
    return url + "?" + urlencode(sorted((k, str(v)) for k, v in params.items()))


def saved_status(url: str, params: Mapping[str, Any] | None = None) -> int | None:
    """The status (200, 404) of a saved answer for *url*, however old; None when there is none. No network."""
    saved = get_cache().get(cache_key(url, params))
    return None if saved is None else saved[0]


@contextmanager
def interactive() -> Iterator[None]:
    """Fetches inside this block (in this thread) use the interactive tries and timeouts."""
    token = _patience.set((INTERACTIVE_ATTEMPTS, INTERACTIVE_TIMEOUT))
    try:
        yield
    finally:
        _patience.reset(token)


def _attempt(url: str, params: Mapping[str, Any] | None, headers: Mapping[str, str] | None) -> httpx.Response:
    last_error: Exception | None = None
    attempts, timeout = _patience.get()
    for attempt in range(attempts):
        if attempt:
            _sleep(0.6 * (2 ** (attempt - 1)) + random.uniform(0, 0.3))
        try:
            response = _get_client().get(url, params=dict(params) if params else None, headers=headers,
                                         timeout=timeout)
        except httpx.HTTPError as exc:  # timeouts, DNS, resets
            last_error = exc
            logger.debug("better-weather: %s attempt %d failed: %s", url, attempt + 1, exc)
            continue
        if response.status_code in RETRY_STATUSES and attempt < attempts - 1:
            last_error = FetchError(url, f"HTTP {response.status_code}", response.status_code)
            continue
        return response
    if isinstance(last_error, FetchError):
        raise last_error
    raise FetchError(url, f"network error: {type(last_error).__name__}: {last_error}")


def get_json(
    url: str,
    params: Mapping[str, Any] | None = None,
    *,
    ttl: float,
    max_stale: float = _DAY,
    allow_404: bool = False,
    headers: Mapping[str, str] | None = None,
) -> Fetched:
    """Fetch JSON with caching.

    * Fresh cache (younger than ``ttl``) is returned without a request.
    * A 404 with ``allow_404`` is cached (a day at most) and returned with ``status=404, data=None``.
    * A 204 (no content) is an empty answer: ``data=None``.
    * If the live request fails, a cached copy younger than ``max_stale`` is returned with
      ``stale=True``; otherwise ``FetchError`` is raised.
    """
    key = cache_key(url, params)
    cache = get_cache()
    cached = cache.get(key)
    now = time.time()
    if cached is not None and now - cached[2] < (ttl if cached[0] == 200 else min(ttl, _DAY)):
        status, body, fetched = cached
        return Fetched(json.loads(body) if status == 200 else None, url, fetched, False, status)

    try:
        response = _attempt(url, params, headers)
        if response.status_code == 204:
            cache.put(key, 200, "null")
            return Fetched(None, url, time.time(), False, 200)
        if response.status_code == 404 and allow_404:
            cache.put(key, 404, "null")
            return Fetched(None, url, time.time(), False, 404)
        if response.status_code != 200:
            detail = _problem_detail(response)
            raise FetchError(url, f"HTTP {response.status_code}{': ' + detail if detail else ''}",
                             response.status_code)
        try:
            data = response.json()
        except ValueError as exc:
            raise FetchError(url, f"response was not JSON: {exc}", response.status_code) from exc
        cache.put(key, 200, json.dumps(data, separators=(",", ":")))
        return Fetched(data, url, time.time(), False, 200)
    except FetchError:
        if cached is not None and now - cached[2] < max_stale:
            status, body, fetched = cached
            logger.info("better-weather: serving stale copy of %s (%.0f s old)", url, now - fetched)
            return Fetched(json.loads(body) if status == 200 else None, url, fetched, True, status)
        raise


MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # biggest thing we fetch is a ~9 MB satellite loop


def download(url: str, params: Mapping[str, Any] | None = None) -> tuple[bytes, dict[str, str]]:
    """GET raw bytes (images) with the same retries; no caching here (the caller caches files)."""
    response = _attempt(url, params, {"Accept": "image/*, */*"})
    if response.status_code != 200:
        raise FetchError(url, f"HTTP {response.status_code}", response.status_code)
    content = response.content
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise FetchError(url, f"response too large ({len(content)} bytes)")
    return content, {k.lower(): v for k, v in response.headers.items()}


def _problem_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:160].strip()
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("title") or data.get("reason") or "")[:200]
    return ""
