"""Clock abstraction (03-foundations §3).

The pipeline captures `run_now = clock.now()` once per run and passes `FixedClock(run_now)` to every
client, so all time comparisons in a run use one instant. Nothing else calls `datetime.now()` except
SystemClock and log/report timestamps.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

__all__ = ["Clock", "FixedClock", "SystemClock"]


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Naive local wall-clock time."""
        ...


class SystemClock:
    """now() → datetime.now() (naive local time)."""

    def now(self) -> datetime:
        return datetime.now()

    def __repr__(self) -> str:
        return "SystemClock()"


class FixedClock:
    """FixedClock(datetime(2026, 11, 5, 9, 30)).now() → that value (tests, and the pipeline's run_now)."""

    def __init__(self, fixed: datetime) -> None:
        self._now = fixed

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        """Move the fixed instant forward (tests only; the pipeline never advances its clock)."""
        self._now = self._now + delta

    def __repr__(self) -> str:
        return f"FixedClock({self._now!r})"
