"""The current time, in one place so tests can replay recorded data at a fixed moment."""

from __future__ import annotations

from datetime import UTC, datetime

_frozen: datetime | None = None


def now_utc() -> datetime:
    return _frozen if _frozen is not None else datetime.now(UTC)


def freeze_for_tests(moment: datetime | None) -> None:
    global _frozen
    _frozen = moment
