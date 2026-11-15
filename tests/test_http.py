import time

import httpx
import pytest
from better_weather.core import cache, http


def _transport(responses):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        item = responses.pop(0) if responses else httpx.Response(500)
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.MockTransport(handler), calls


def test_retries_then_succeeds():
    transport, calls = _transport([httpx.Response(503), httpx.ConnectTimeout("slow"),
                                   httpx.Response(200, json={"ok": 1})])
    http.set_transport_for_tests(transport)
    got = http.get_json("https://example.test/a", ttl=60)
    assert got.data == {"ok": 1} and not got.stale and len(calls) == 3


def test_fresh_cache_skips_network():
    transport, calls = _transport([httpx.Response(200, json={"v": 1})])
    http.set_transport_for_tests(transport)
    http.get_json("https://example.test/b", ttl=60)
    again = http.get_json("https://example.test/b", ttl=60)
    assert again.data == {"v": 1} and len(calls) == 1


def test_stale_copy_served_when_live_fetch_fails():
    c = cache.get_cache()
    c.put(http.cache_key("https://example.test/c", None), 200, '{"v": 2}', time.time() - 600)
    transport, _ = _transport([httpx.Response(500)] * 3)
    http.set_transport_for_tests(transport)
    got = http.get_json("https://example.test/c", ttl=60, max_stale=3600)
    assert got.data == {"v": 2} and got.stale and got.age_seconds >= 590


def test_too_old_copy_is_not_served():
    c = cache.get_cache()
    c.put(http.cache_key("https://example.test/d", None), 200, '{"v": 3}', time.time() - 7200)
    transport, _ = _transport([httpx.Response(500)] * 3)
    http.set_transport_for_tests(transport)
    with pytest.raises(http.FetchError):
        http.get_json("https://example.test/d", ttl=60, max_stale=3600)


def test_404_is_cached_when_allowed():
    transport, calls = _transport([httpx.Response(404, json={"detail": "outside"})])
    http.set_transport_for_tests(transport)
    first = http.get_json("https://example.test/e", ttl=60, allow_404=True)
    second = http.get_json("https://example.test/e", ttl=60, allow_404=True)
    assert first.status == second.status == 404 and not first.found and len(calls) == 1


def test_error_message_includes_problem_detail():
    transport, _ = _transport([httpx.Response(400, json={"detail": "Bad point"})])
    http.set_transport_for_tests(transport)
    with pytest.raises(http.FetchError, match="Bad point"):
        http.get_json("https://example.test/f", ttl=60)
