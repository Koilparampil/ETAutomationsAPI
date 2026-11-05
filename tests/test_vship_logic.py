"""vship/logic.py: best match (05 §6.1, D17), notif status (01 §11, D15, O7), TR/SWB notes (01 §12)
— 05-vship-crm §9 tests 1–3."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any

import pytest

from eta_automations.vship.logic import (
    EXCLUDE,
    KEYWORDS,
    NotifStatus,
    best_match,
    has_tr_swb_note,
    is_tr_swb_comment,
    match_rank,
    notif_status,
    parse_vship_datetime,
)

RUN_NOW = datetime(2026, 11, 5, 9, 30)
WANTED = "EBKG17208602"


def records(*booking_nos: str) -> list[dict[str, Any]]:
    return [{"bookingId": 1000 + i, "bookingNo": no} for i, no in enumerate(booking_nos)]


# ── test 1: match_rank / best_match, every row of the 05 §6.1 table ─────────────────────────────────
@pytest.mark.parametrize(
    ("candidates", "chosen"),
    [
        (("EBKG17208602_Copy_76099", "EBKG17208602"), "EBKG17208602"),            # tier 0 beats tier 1
        (("EBKG17208602_Copy_76099", "EBKG17208602 HOLD"), "EBKG17208602 HOLD"),  # both tier 1; 5 < 11
        (("XEBKG17208602",), "XEBKG17208602"),                                    # tier 2
        (("EBKG17208999",), None),                                                # tier 3 → none (D17)
        ((), None),                                                               # empty → none
    ],
)
def test_best_match_table(candidates: tuple[str, ...], chosen: str | None) -> None:
    best = best_match(records(*candidates), WANTED)
    assert (best["bookingNo"] if best else None) == chosen


@pytest.mark.parametrize(
    ("candidate", "tier", "length_diff"),
    [
        ("EBKG17208602", 0, 0),
        (" ebkg17208602 ", 0, 0),          # stripped + upper-cased
        ("EBKG17208602_Copy_76099", 1, 11),
        ("EBKG17208602 HOLD", 1, 5),
        ("XEBKG17208602", 2, 1),
        ("EBKG17208999", 3, 0),
        ("", 3, 12),
    ],
)
def test_match_rank_tiers(candidate: str, tier: int, length_diff: int) -> None:
    rank = match_rank(candidate, WANTED)
    assert rank[:2] == (tier, length_diff)
    assert -1.0 <= rank[2] <= 0.0


def test_match_rank_ratio_breaks_remaining_ties() -> None:
    assert match_rank("EBKG17208602", WANTED)[2] == -1.0
    assert match_rank("EBKG17208603", WANTED)[2] > -1.0


def test_best_match_first_wins_on_ties() -> None:
    recs = [{"bookingId": 1, "bookingNo": "EBKG17208602"}, {"bookingId": 2, "bookingNo": "EBKG17208602"}]
    assert best_match(recs, WANTED) == {"bookingId": 1, "bookingNo": "EBKG17208602"}


def test_best_match_on_the_search_fixture(load_fixture: Callable[[str], Any]) -> None:
    data = load_fixture("vship/search_multi.json")["value"]["data"]
    assert best_match(data, WANTED)["bookingId"] == 72892  # type: ignore[index]


def test_best_match_is_tolerant_of_odd_records() -> None:
    recs: list[Any] = ["junk", None, {"bookingId": 5}, {"bookingId": 6, "bookingNo": 267546627}]
    assert best_match(recs, "267546627") == {"bookingId": 6, "bookingNo": 267546627}
    assert best_match(None, WANTED) is None


@pytest.mark.parametrize("wanted", ["", "   "])
def test_best_match_never_matches_an_empty_booking(wanted: str) -> None:
    assert best_match(records("EBKG17208602", "X"), wanted) is None


# ── test 2: notif_status (01 §11) ────────────────────────────────────────────────────────────────────
def c(text: str, created: Any) -> dict[str, Any]:
    return {"comment": text, "createdDate": created}


def ago(days: float) -> str:
    return (RUN_NOW - timedelta(days=days)).isoformat()


@pytest.mark.parametrize(
    ("comments", "expected"),
    [
        ([], NotifStatus(0, True)),                                                       # no comments
        ([c("ARRIVED ON 11/03. NOTIF #1", ago(2))], NotifStatus(1, False)),              # 2 days old
        ([c("ARRIVED ON 10/28. NOTIF #1", ago(8))], NotifStatus(1, True)),               # 8 days old
        ([c("ARRIVED ON 10/16. NOTIF #1", ago(20)), c("ARRIVED ON 11/03. NOTIF #1", ago(2))],
         NotifStatus(1, False)),                                                          # D15 (newest decides)
        ([c("ARRIVED ON 11/03. NOTIF #1", ago(2)), c("ARRIVED ON 10/16. NOTIF #1", ago(20))],
         NotifStatus(1, False)),                                                          # D15, other order
        ([c("ARRIVED ON 10/28. NOTIF #1", ago(9)), c("ARRIVED ON 11/03. NOTIF #2", ago(2))],
         NotifStatus(2, False)),                                                          # #1 + #2
        ([c("ARRIVED ON 11/03. NOTIF #2", ago(2))], NotifStatus(0, True)),               # #2 only (O7)
        ([c("arrived on 11/03. notif #1", ago(1))], NotifStatus(1, False)),              # case-insensitive
        ([c("ARRIVING ON 11/18.", ago(1)), c("SWB RCVD", ago(1))], NotifStatus(0, True)),
        ([c("NOTIF #1", ago(7))], NotifStatus(1, True)),                                 # exactly 7 days: not newer
        ([c("NOTIF #1", (RUN_NOW - timedelta(days=7) + timedelta(seconds=1)).isoformat())], NotifStatus(1, False)),
    ],
)
def test_notif_status(comments: list[dict[str, Any]], expected: NotifStatus) -> None:
    assert notif_status(comments, RUN_NOW) == expected


def test_notif_status_null_internal_comments() -> None:
    assert notif_status(None, RUN_NOW) == NotifStatus(0, True)


def test_notif_status_unparseable_date_is_old_and_warns(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    status = notif_status([c("ARRIVED ON 11/03. NOTIF #1", "not a date")], RUN_NOW)
    assert status == NotifStatus(1, True)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and "createdDate" in warnings[0].getMessage()


def test_notif_status_unparseable_date_does_not_hide_a_recent_one(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    comments = [c("NOTIF #1", None), c("NOTIF #1", ago(1))]
    assert notif_status(comments, RUN_NOW) == NotifStatus(1, False)


def test_notif_status_on_the_comments_fixture(load_fixture: Callable[[str], Any]) -> None:
    comments = load_fixture("vship/comments_72892.json")["value"]["internalComments"]
    assert notif_status(comments, RUN_NOW) == NotifStatus(1, False)  # NOTIF #1 on 2026-11-03 16:45
    assert notif_status(comments, datetime(2026, 11, 11)) == NotifStatus(1, True)


def test_notif_status_tolerates_odd_entries() -> None:
    comments: list[Any] = ["junk", None, {"comment": None}, {"comment": 42}, c("NOTIF #1", ago(1))]
    assert notif_status(comments, RUN_NOW) == NotifStatus(1, False)


def test_notif_status_is_frozen() -> None:
    status = NotifStatus(1, True)
    with pytest.raises(AttributeError):
        status.count = 2  # type: ignore[misc]


# ── parse_vship_datetime ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-11-03T16:45:00", datetime(2026, 11, 3, 16, 45)),
        (" 2026-11-03T16:45:00 ", datetime(2026, 11, 3, 16, 45)),
        ("2026-11-03T16:45:00.1234567", datetime(2026, 11, 3, 16, 45, 0, 123456)),   # .NET 7 digits
        ("2026-11-03 16:45", datetime(2026, 11, 3, 16, 45)),
        ("2026-11-03", datetime(2026, 11, 3)),
        (datetime(2026, 11, 3, 1, 2), datetime(2026, 11, 3, 1, 2)),
        (date(2026, 11, 3), datetime(2026, 11, 3)),
    ],
)
def test_parse_vship_datetime_naive(value: Any, expected: datetime) -> None:
    assert parse_vship_datetime(value) == expected


@pytest.mark.parametrize(
    "value",
    ["2026-11-03T16:45:00Z", "2026-11-03T16:45:00.1234567Z", "2026-11-03T18:45:00+02:00"],
)
def test_parse_vship_datetime_aware_is_converted_to_local_naive(value: str) -> None:
    instant = datetime(2026, 11, 3, 16, 45, tzinfo=timezone.utc)
    expected = instant.astimezone().replace(tzinfo=None)
    parsed = parse_vship_datetime(value)
    assert parsed is not None and parsed.tzinfo is None
    assert parsed.replace(microsecond=0) == expected


@pytest.mark.parametrize("value", [None, "", "not a date", "11/03/2026", 1793000000, True, [], {}])
def test_parse_vship_datetime_unparseable(value: Any) -> None:
    assert parse_vship_datetime(value) is None


# ── test 3: TR/SWB golden cases (01 §12) ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SWB RCVD 10/12", True),
        ("swb rcvd", True),
        ("TR RCVD", True),
        ("TR RECVD BY JOHN", True),
        ("RTR", True),
        ("RTR SENT", True),
        ("PR FOR TR", True),
        ("PR/ PS FOR TR", True),
        ("PS FOR RTR", True),
        ("PAID IN FULL, TR FEE SENT TO MSC FOR TR", True),
        ("NO TR", False),
        ("TR PENDING", False),
        ("NO TR - SWB RCVD", True),
        ("TR PENDING, RTR", True),
        ("PS - LESS TR FEE, PAID", False),
        ("PS - LESS TR FEE, RTR", True),
        ("STRTR", False),
        ("TR RCVDX", False),
        ("SWBRCVD", False),
        ("PR FOR TRUCKING", False),
        ("ARRIVED ON 11/02. NOTIF #1", False),
    ],
)
def test_tr_swb_golden_cases(text: str, expected: bool) -> None:
    assert is_tr_swb_comment(text) is expected
    assert has_tr_swb_note([{"comment": text}]) is expected


def test_keyword_lists_are_verbatim() -> None:
    assert KEYWORDS == ["PAID IN FULL, TR FEE SENT TO MSC FOR TR", "PR FOR TR", "PS FOR TR", "PR FOR TR",
                        "PS FOR RTR", "PR/ PS FOR TR", "RTR", "SWB RCVD", "TR RECVD", "TR RCVD"]
    assert EXCLUDE == ["PS - LESS TR FEE, ", "NO TR", "TR PENDING"]


def test_has_tr_swb_note_over_lists(load_fixture: Callable[[str], Any]) -> None:
    comments = load_fixture("vship/comments_72892.json")["value"]["internalComments"]
    assert has_tr_swb_note(comments) is True             # "SWB RCVD 10/12"
    assert has_tr_swb_note(comments[:2]) is False
    assert has_tr_swb_note([]) is False
    assert has_tr_swb_note(None) is False
    assert has_tr_swb_note([{"comment": None}, "junk", {"other": "RTR"}]) is False
