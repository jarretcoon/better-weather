"""Download weather images into the plugin's data folder, reuse fresh copies, prune old ones.

Images are files on purpose: Hermes delivers a ``MEDIA:/path`` in a reply as a native photo or
animation in Telegram (and inline in the Desktop app)."""

from __future__ import annotations

import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path

from . import http, settings

KEEP_SECONDS = 24 * 3600
_MAGIC = (b"GIF87a", b"GIF89a", b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")
_lock = threading.Lock()
_last_prune = 0.0


@dataclass(frozen=True)
class Image:
    path: Path
    url: str
    fetched_at: float
    stale: bool = False
    image_time: str | None = None  # the server's Last-Modified, ISO 8601

    @property
    def media(self) -> str:
        return f"MEDIA:{self.path}"

    @property
    def age_minutes(self) -> int:
        return int(max(0.0, time.time() - self.fetched_at) // 60)


def images_dir() -> Path:
    base = settings.data_dir()
    root = (base / "images") if base is not None else Path(tempfile.gettempdir()) / "better-weather-images"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _prune(root: Path) -> None:
    global _last_prune
    now = time.time()
    if now - _last_prune < 600:
        return
    _last_prune = now
    for item in root.iterdir():
        try:
            if item.is_file() and now - item.stat().st_mtime > KEEP_SECONDS:
                item.unlink()
        except OSError:
            pass


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:120]


def looks_like_image(content: bytes) -> bool:
    return any(content.startswith(m) for m in _MAGIC)


def fetch(url: str, name: str, *, ttl: float, max_stale: float = 3 * 3600) -> Image:
    """Return a local copy of *url* no older than *ttl* seconds, else a flagged stale copy if
    downloading fails, else raise ``http.FetchError``."""
    root = images_dir()
    path = root / _safe_name(name)
    meta = path.with_suffix(path.suffix + ".time")
    with _lock:
        _prune(root)
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        return Image(path, url, path.stat().st_mtime, False, _read(meta))
    try:
        content, headers = http.download(url)
        if not looks_like_image(content):
            raise http.FetchError(url, "the server did not return an image")
        fd, tmp_name = tempfile.mkstemp(dir=root, prefix=path.name + ".", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        image_time = _iso(headers.get("last-modified"))
        if image_time:
            meta.write_text(image_time, encoding="utf-8")
        elif meta.exists():
            meta.unlink()
        return Image(path, url, time.time(), False, image_time)
    except http.FetchError:
        if path.exists() and time.time() - path.stat().st_mtime < max_stale:
            return Image(path, url, path.stat().st_mtime, True, _read(meta))
        raise


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _iso(http_date: str | None) -> str | None:
    if not http_date:
        return None
    try:
        return parsedate_to_datetime(http_date).isoformat()
    except (TypeError, ValueError):
        return None
