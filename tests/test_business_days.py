"""business_days.py: the 6-US-business-day window (01-business-rules §5; 12-testing §2)."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timedelta

import pytest

from eta_automations import rules
from eta_automations.business_days import (
    DEFAULT_WINDOW_BUSINESS_DAYS,
    add_business_days,
    in_window,
    window_cutoff,
)

FMT = "%Y-%m-%d %H:%M"

# 01 §5 golden vectors (generated with the original pandas implementation): run_now, weekday, cutoff, weekday
GOLDEN = [
    ("2026-11-05 09:30", "Thu", "2026-11-16 09:30", "Mon"),
    ("2026-11-06 16:00", "Fri", "2026-11-17 16:00", "Tue"),
    ("2026-11-07 10:00", "Sat", "2026-11-17 10:00", "Tue"),
    ("2026-11-08 23:59", "Sun", "2026-11-17 23:59", "Tue"),
    ("2026-11-09 08:00", "Mon", "2026-11-18 08:00", "Wed"),
    ("2026-11-11 12:00", "Wed", "2026-11-19 12:00", "Thu"),
    ("2026-11-23 07:15", "Mon", "2026-12-02 07:15", "Wed"),
    ("2026-11-26 09:00", "Thu", "2026-12-04 09:00", "Fri"),
    ("2026-12-18 17:45", "Fri", "2026-12-29 17:45", "Tue"),
    ("2026-12-24 08:00", "Thu", "2027-01-05 08:00", "Tue"),
    ("2026-12-31 13:00", "Thu", "2027-01-11 13:00", "Mon"),
    ("2027-01-15 10:00", "Fri", "2027-01-26 10:00", "Tue"),
    ("2027-02-12 10:00", "Fri", "2027-02-23 10:00", "Tue"),
    ("2027-05-28 10:00", "Fri", "2027-06-08 10:00", "Tue"),
    ("2027-06-17 10:00", "Thu", "2027-06-28 10:00", "Mon"),
    ("2027-07-02 10:00", "Fri", "2027-07-13 10:00", "Tue"),
    ("2027-08-31 10:00", "Tue", "2027-09-09 10:00", "Thu"),
    ("2027-10-08 10:00", "Fri", "2027-10-19 10:00", "Tue"),
    ("2027-11-10 10:00", "Wed", "2027-11-19 10:00", "Fri"),
    ("2027-12-23 10:00", "Thu", "2028-01-04 10:00", "Tue"),
    ("2027-12-30 10:00", "Thu", "2028-01-10 10:00", "Mon"),
    ("2028-01-14 10:00", "Fri", "2028-01-25 10:00", "Tue"),
]


def test_golden_table_has_all_22_rows() -> None:
    assert len(GOLDEN) == 22


@pytest.mark.parametrize(("run_now", "run_wd", "cutoff", "cutoff_wd"), GOLDEN)
def test_window_cutoff_golden_vectors(run_now: str, run_wd: str, cutoff: str, cutoff_wd: str) -> None:
    start = datetime.strptime(run_now, FMT)
    expected = datetime.strptime(cutoff, FMT)
    assert start.strftime("%a") == run_wd  # the table itself is consistent
    result = window_cutoff(start)
    assert result == expected
    assert result.strftime("%a") == cutoff_wd
    assert add_business_days(start, 6) == expected


def test_result_is_a_plain_datetime_not_a_pandas_timestamp() -> None:
    result = window_cutoff(datetime(2026, 11, 5, 9, 30))
    assert type(result) is datetime
    assert result.tzinfo is None


def test_time_of_day_is_preserved_including_microseconds() -> None:
    start = datetime(2026, 11, 5, 9, 30, 15, 123456)
    assert add_business_days(start, 1) == datetime(2026, 11, 6, 9, 30, 15, 123456)


def test_weekend_start_rolls_forward_as_first_step() -> None:
    # Sat → the first step lands on Monday (pandas semantics, kept for parity)
    assert add_business_days(datetime(2026, 11, 7, 10, 0), 1) == datetime(2026, 11, 9, 10, 0)
    assert add_business_days(datetime(2026, 11, 8, 23, 59), 1) == datetime(2026, 11, 9, 23, 59)


@pytest.mark.parametrize(
    ("start", "expected"),
    [
        # 2026-07-03 (Fri) is the observed Independence Day (July 4th 2026 is a Saturday)
        (datetime(2026, 7, 2, 10, 0), datetime(2026, 7, 6, 10, 0)),
        # 2027-12-31 (Fri) is the observed New Year 2028 (Jan 1st 2028 is a Saturday)
        (datetime(2027, 12, 30, 10, 0), datetime(2028, 1, 3, 10, 0)),
        # Thanksgiving 2026-11-26 (Thu)
        (datetime(2026, 11, 25, 8, 0), datetime(2026, 11, 27, 8, 0)),
    ],
)
def test_observed_federal_holidays_are_skipped(start: datetime, expected: datetime) -> None:
    assert add_business_days(start, 1) == expected


def test_custom_window_length() -> None:
    run_now = datetime(2026, 11, 5, 9, 30)
    assert DEFAULT_WINDOW_BUSINESS_DAYS == 6
    assert window_cutoff(run_now, 1) == datetime(2026, 11, 6, 9, 30)
    assert window_cutoff(run_now, 2) == datetime(2026, 11, 9, 9, 30)  # skips the weekend
    assert window_cutoff(run_now, 0) == run_now


@pytest.mark.parametrize("bad", [True, 1.5, "6"])
def test_add_business_days_rejects_non_integers(bad: object) -> None:
    with pytest.raises(TypeError):
        add_business_days(datetime(2026, 11, 5), bad)  # type: ignore[arg-type]


# ── in_window boundaries (01 §5 worked example: run_now Thu 2026-11-05 09:30 → cutoff Mon 2026-11-16 09:30)
CUTOFF = datetime(2026, 11, 16, 9, 30)


@pytest.mark.parametrize(
    ("eta", "expected"),
    [
        (datetime(2026, 11, 16, 0, 0), True),                    # MSC date-only ETA on the cutoff day
        (datetime(2026, 11, 16, 14, 0), False),                  # CMA-style ETA with a time of day
        (CUTOFF, True),                                          # eta == cutoff is inside (<=)
        (CUTOFF + timedelta(microseconds=1), False),
        (CUTOFF - timedelta(microseconds=1), True),
        (datetime(2026, 11, 3, 0, 0), True),                     # arrived: always in window
        (datetime(2020, 1, 1), True),
        (datetime(2026, 12, 1), False),
    ],
)
def test_in_window_boundaries(eta: datetime, expected: bool) -> None:
    assert window_cutoff(datetime(2026, 11, 5, 9, 30)) == CUTOFF
    assert in_window(eta, CUTOFF) is expected


def test_rules_re_exports_the_window_helpers() -> None:
    assert rules.in_window is in_window
    assert rules.window_cutoff is window_cutoff


def test_pandas_is_imported_lazily() -> None:
    """03 §2: importing business_days (or rules) must not import pandas."""
    code = (
        "import sys; import eta_automations.business_days, eta_automations.rules, eta_automations.texts; "
        "print('pandas' in sys.modules)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=120)
    assert out.stdout.strip() == "False"
