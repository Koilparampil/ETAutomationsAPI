"""VShip business logic, pure (05-vship-crm §6.1; 01-business-rules §11–§12).

- `match_rank` / `best_match`: choose the VShip booking for a base booking (D17: no unrelated bookings).
- `NotifStatus` / `notif_status`: Notif #1 / #2 bookkeeping from the internal comments (D15, O7).
- `parse_vship_datetime`: tolerant ISO-8601 parsing of `createdDate`.
- `is_tr_swb_comment` / `has_tr_swb_note`: TR/SWB keyword match, regexes built verbatim from the original
  (`VShip/noteSearch.py`), including its duplicate "PR FOR TR" keyword.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

__all__ = [
    "EXCLUDE",
    "KEYWORDS",
    "NotifStatus",
    "best_match",
    "comment_text",
    "has_tr_swb_note",
    "is_tr_swb_comment",
    "match_rank",
    "notif_status",
    "parse_vship_datetime",
]

log = logging.getLogger("eta.vship")

NOTIF_WINDOW = timedelta(days=7)  # 01 §11: "done in the last week"
_FRACTION_RE = re.compile(r"(\.\d{6})\d+")  # .NET emits 7 fractional digits; keep microseconds


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 05 §6.1 — booking best match
# ════════════════════════════════════════════════════════════════════════════════════════════════
def match_rank(candidate: str, booking_no: str) -> tuple[int, int, float]:
    """Sort key (lower is better): tier 0 exact, 1 prefix, 2 contains, 3 unrelated; then the length
    difference; then the negated SequenceMatcher ratio (05 §6.1, verbatim)."""
    cand = str(candidate or "").strip().upper()
    wanted = booking_no.strip().upper()
    if cand == wanted:
        tier = 0
    elif cand.startswith(wanted):
        tier = 1
    elif wanted in cand:
        tier = 2
    else:
        tier = 3
    return tier, abs(len(cand) - len(wanted)), -SequenceMatcher(None, cand, wanted).ratio()


def _booking_no(record: Mapping[str, Any]) -> str:
    value = record.get("bookingNo")
    return "" if value is None else str(value)


def best_match(records: Sequence[Mapping[str, Any]] | None, booking_no: str) -> dict[str, Any] | None:
    """`min()` by match_rank over the records' `bookingNo` (first wins on ties — parity) (05 §6.1).

    D17: None when the best record's tier is 3 (no textual relation) or there are no records (the original
    accepted unrelated bookings, or crashed with ValueError on an empty list). Also None for an empty
    `booking_no`: every candidate would trivially "contain" it, so any match would be arbitrary.
    Non-dict entries are ignored.
    """
    if not booking_no or not booking_no.strip():
        return None
    candidates = [r for r in (records or []) if isinstance(r, Mapping)]
    if not candidates:
        return None
    best = min(candidates, key=lambda r: match_rank(_booking_no(r), booking_no))
    if match_rank(_booking_no(best), booking_no)[0] == 3:
        return None  # D17
    return dict(best)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §11 — notification status
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class NotifStatus:
    """01 §11: `count` 0, 1 or 2 notices already sent; `have_to_do` False means "skip this row"."""

    count: int
    have_to_do: bool


def comment_text(comment: Any) -> str:
    """`comment.get("comment") or ""` (01 §11), tolerant of non-dict entries and non-string values."""
    if not isinstance(comment, Mapping):
        return ""
    value = comment.get("comment")
    if not value:
        return ""
    return value if isinstance(value, str) else str(value)


def parse_vship_datetime(value: Any) -> datetime | None:
    """ISO-8601 → naive datetime (01 §11): aware values are converted to machine-local time and made
    naive; naive values are kept as-is; anything unparseable (or not a string/date) → None.

    Tolerates .NET's 7-digit fractions, a trailing "Z", a space separator and date-only values (00:00).
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            normalized = _FRACTION_RE.sub(r"\1", text)
            if normalized.endswith(("Z", "z")):
                normalized = normalized[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                return None
    else:
        return None
    if parsed.tzinfo is not None and parsed.utcoffset() is not None:
        try:
            return parsed.astimezone().replace(tzinfo=None)
        except (OverflowError, ValueError, OSError):  # e.g. 0001-01-01T00:00:00Z near datetime.min
            return None
    return parsed.replace(tzinfo=None)


def notif_status(comments: Iterable[Any] | None, run_now: datetime) -> NotifStatus:
    """01 §11 port of `lookup_customer_notif`.

    - no comment containing "notif #1" → (0, True)          (O7: "notif #2" alone also counts as 0)
    - any comment containing "notif #2" (with a #1 present) → (2, False)
    - else (1, have_to_do) where have_to_do = not (newest #1 createdDate > run_now - 7 days)
      D15: the MOST RECENT "notif #1" comment decides (the original used the first one in API order).
      An unparseable createdDate counts as "old" and is logged as a WARNING.
    A missing/None comment list is treated as [] (the original raised and fell into 01 §6.2).
    """
    items = list(comments or [])
    texts = [comment_text(c) for c in items]
    n1 = [c for c, t in zip(items, texts, strict=True) if "notif #1" in t.lower()]
    if not n1:
        return NotifStatus(0, True)
    if any("notif #2" in t.lower() for t in texts):
        return NotifStatus(2, False)
    dates: list[datetime] = []
    for c in n1:
        raw = c.get("createdDate") if isinstance(c, Mapping) else None
        parsed = parse_vship_datetime(raw)
        if parsed is None:
            log.warning("VShip NOTIF #1 comment has an unreadable createdDate %s - treating it as older than "
                        "a week", repr(raw)[:80])
            continue
        dates.append(parsed)
    if not dates:
        return NotifStatus(1, True)
    newest = max(dates)  # D15
    return NotifStatus(1, not (newest > run_now - NOTIF_WINDOW))


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §12 — TR/SWB note detection (verbatim)
# ════════════════════════════════════════════════════════════════════════════════════════════════
KEYWORDS = ["PAID IN FULL, TR FEE SENT TO MSC FOR TR", "PR FOR TR", "PS FOR TR", "PR FOR TR", "PS FOR RTR",
            "PR/ PS FOR TR", "RTR", "SWB RCVD", "TR RECVD", "TR RCVD"]
EXCLUDE = ["PS - LESS TR FEE, ", "NO TR", "TR PENDING"]


def _build_pattern(words: Iterable[str]) -> re.Pattern[str]:
    return re.compile(r"\b(?:" + "|".join(re.escape(kw) for kw in words) + r")\b", re.IGNORECASE)


_PATTERN = _build_pattern(KEYWORDS)
_EXCLUDE = _build_pattern(EXCLUDE)


def is_tr_swb_comment(text: str) -> bool:
    """True if the comment (after removing the EXCLUDE phrases) contains a TR/SWB keyword (01 §12)."""
    return bool(_PATTERN.search(_EXCLUDE.sub("", text)))


def has_tr_swb_note(comments: Iterable[Any] | None) -> bool:
    """True if any internal comment is a TR/SWB note (01 §12); None is treated as []."""
    return any(is_tr_swb_comment(comment_text(c)) for c in (comments or []))
