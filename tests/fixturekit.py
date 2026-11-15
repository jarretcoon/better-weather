"""Recorded-response fixtures: one JSON file per request, keyed by host + path + sorted query."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INDEX = FIXTURES / "index.json"


def request_key(request: httpx.Request) -> str:
    query = urlencode(sorted(parse_qsl(request.url.query.decode() if isinstance(request.url.query, bytes)
                                       else str(request.url.query))))
    return f"{request.url.host}{request.url.path}" + (f"?{query}" if query else "")


def _file_name(key: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", key)[:80].strip("_")
    return f"{slug}_{hashlib.sha1(key.encode()).hexdigest()[:8]}.json"


def load_index() -> dict[str, dict[str, object]]:
    if not INDEX.exists():
        return {}
    return json.loads(INDEX.read_text(encoding="utf-8"))


def replay_transport(
    overrides: Callable[[httpx.Request], httpx.Response | None] | None = None,
    log: list[str] | None = None,
) -> httpx.MockTransport:
    """Serve recorded responses. *overrides* can answer (or fail) any request first."""
    index = load_index()

    def handler(request: httpx.Request) -> httpx.Response:
        key = request_key(request)
        if log is not None:
            log.append(key)
        if overrides is not None:
            forced = overrides(request)
            if forced is not None:
                return forced
        entry = index.get(key)
        if entry is None:
            return httpx.Response(599, json={"detail": f"no fixture for {key}"})
        body = (FIXTURES / str(entry["file"])).read_bytes()
        return httpx.Response(int(entry["status"]), content=body,  # type: ignore[arg-type]
                              headers={"content-type": "application/json"})

    return httpx.MockTransport(handler)


class RecordingTransport(httpx.BaseTransport):
    """Pass requests to the network and save each response as a fixture."""

    def __init__(self, shrink: Callable[[str, object], object] | None = None) -> None:
        self._inner = httpx.HTTPTransport(retries=1)
        self._shrink = shrink
        self.index = load_index()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        response = self._inner.handle_request(request)
        response.read()
        key = request_key(request)
        try:
            data: object = json.loads(response.content)
        except ValueError:
            return response
        if self._shrink is not None:
            data = self._shrink(key, data)
        name = _file_name(key)
        FIXTURES.mkdir(parents=True, exist_ok=True)
        (FIXTURES / name).write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        self.index[key] = {"file": name, "status": response.status_code}
        INDEX.write_text(json.dumps(self.index, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return httpx.Response(response.status_code, content=json.dumps(data).encode(),
                              headers={"content-type": "application/json"})
