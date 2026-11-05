"""6-US-business-day window (01-business-rules §5).

Exact parity with the original `carrierTing/carriers.py`: pandas `CustomBusinessDay` with
`USFederalHolidayCalendar` (observed dates included, e.g. 2027-12-31 for New Year 2028). The time of day
is preserved and a weekend/holiday start rolls forward as the first step.

pandas is imported lazily (03-foundations §2): importing this module costs nothing, the first call
builds the offset once (cached for the life of the process).

All datetimes are naive. The ETA is carrier-local wall time and run_now is machine-local; they are
compared without any time-zone conversion, as the original did (01 §16 O1).
"""

from __future__ import annotations

import operator
from datetime import datetime
from functools import lru_cache
from typing import Any

__all__ = ["DEFAULT_WINDOW_BUSINESS_DAYS", "add_business_days", "in_window", "window_cutoff"]

# 01 §5 / ETA_WINDOW_BUSINESS_DAYS default (02 §1)
DEFAULT_WINDOW_BUSINESS_DAYS = 6


@lru_cache(maxsize=1)
def _us_business_day() -> Any:
    """One `CustomBusinessDay(calendar=USFederalHolidayCalendar())`, built on first use."""
    from pandas.tseries.holiday import USFederalHolidayCalendar  # lazy: 03 §2
    from pandas.tseries.offsets import CustomBusinessDay

    return CustomBusinessDay(calendar=USFederalHolidayCalendar())


def add_business_days(start: datetime, n: int) -> datetime:
    """`start + n * CustomBusinessDay(calendar=USFederalHolidayCalendar())` as a plain datetime (01 §5).

    Time of day is preserved; a weekend/holiday `start` rolls forward as the first step (pandas
    semantics, e.g. Sat 2026-11-07 10:00 + 6 → Tue 2026-11-17 10:00).
    """
    if isinstance(n, bool):
        raise TypeError("n must be an int (got bool)")
    steps = operator.index(n)  # int-like only (TypeError for floats/strings)
    result = start + steps * _us_business_day()
    # pandas returns a Timestamp; callers compare it with plain datetimes (EtaResult.eta), so convert.
    to_pydatetime = getattr(result, "to_pydatetime", None)
    return to_pydatetime() if callable(to_pydatetime) else result


def window_cutoff(run_now: datetime, business_days: int = DEFAULT_WINDOW_BUSINESS_DAYS) -> datetime:
    """Latest ETA that is still "in window": run_now + `business_days` US business days (01 §5)."""
    return add_business_days(run_now, business_days)


def in_window(eta: datetime, cutoff: datetime) -> bool:
    """01 §5: `eta <= cutoff`. Arrived shipments (ETA in the past) are always in window."""
    return eta <= cutoff
