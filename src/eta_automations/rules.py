"""Pure decision functions: input rows, suffix classification, thresholds (01-business-rules §2–§3, §6.1,
§7, §9). No I/O (the D2 helper only logs at INFO).

Pipeline usage (10-pipeline Phase 1/2a):
    rows   = parse_rows(csv.reader(f))                    # 01 §2 (blank rows skipped, line_no from 1)
    parsed = [classify(r) for r in rows]                  # 01 §3
    parsed, header = drop_header_row(parsed)              # D2
    dups   = find_duplicates(parsed)                      # D3: {line_no: first occurrence}
    route  = initial_route(p, settings.run.min_invoice_amount)   # None → carrier lookup (STANDARD/NOTIF1_SENT)

The carrier pattern table of 01 §4 lives in carriers/router.py, not here.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from . import texts
from .business_days import in_window, window_cutoff
from .errors import CarrierLookupError, CustomsHoldError, NoEtaError
from .models import InputRow, OutputFile, ParsedRow, Route, RowKind

__all__ = [
    "DEFAULT_MIN_INVOICE_AMOUNT",
    "EMPTY_DOC_NUMBER_ERROR",
    "THRESHOLD_KINDS",
    "arrival_doc_number",
    "carrier_failure",
    "classify",
    "clean_amount",
    "drop_header_row",
    "duplicate_entry",
    "find_duplicates",
    "in_window",
    "initial_route",
    "is_below_threshold",
    "is_header_row",
    "malformed_entry",
    "parse_amount",
    "parse_row",
    "parse_rows",
    "with_arrival_suffix",
    "window_cutoff",
]

log = logging.getLogger("eta.pipeline")

# ETA_MIN_INVOICE_AMOUNT default (01 §3, 02 §1)
DEFAULT_MIN_INVOICE_AMOUNT = Decimal("1000")

# D1: <error text> line of the malformed entry when only the DocNumber cell is empty.
EMPTY_DOC_NUMBER_ERROR = "missing invoice number (first column is empty)"

# Kinds the amount threshold applies to, before anything else (01 §3).
THRESHOLD_KINDS = frozenset({RowKind.STANDARD, RowKind.NOTIF1_SENT, RowKind.BOTH_SENT})

_ASCII_DIGITS = frozenset("0123456789")
_NOTIF1_RE = re.compile(r"(?i)[^a]a$")  # 01 §3 rule 3, verbatim


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §2 — input rows
# ════════════════════════════════════════════════════════════════════════════════════════════════
def clean_amount(amount_raw: str) -> str:
    """01 §2: drop thousands separators and '$', then strip."""
    return amount_raw.replace(",", "").replace("$", "").strip()


def _float_style_error(cleaned: str) -> str:
    # Identical to CPython's float() message ("... float: %R"), for parity with the original (01 §7.1).
    return f"could not convert string to float: {cleaned!r}"


def parse_amount(amount_raw: str) -> tuple[Decimal | None, str | None]:
    """Parse an amount cell: `(Decimal, None)` or `(None, <error text>)` (01 §2).

    - Every string the original's `float()` rejected stays MALFORMED with the float-style message, e.g.
      `could not convert string to float: 'abc'` (Decimal alone would accept `_1`, `1_`, `1__0`, `sNaN`).
    - `Decimal` is used for the value (same comparisons for money values, no binary rounding).
    - NaN / Infinity are MALFORMED too (safer: a NaN Decimal cannot be compared with the threshold, and
      neither is an invoice amount); their text is `amount is not a finite number: '<cleaned>'`.
    """
    cleaned = clean_amount(amount_raw)
    try:
        float(cleaned)  # parity gate with the original's float() parsing
        value = Decimal(cleaned)
    except (ValueError, InvalidOperation):
        return None, _float_style_error(cleaned)
    if not value.is_finite():
        return None, f"amount is not a finite number: {cleaned!r}"
    return value, None


def parse_row(row: Sequence[str], line_no: int) -> InputRow:
    """One csv.reader row → InputRow (01 §2). The row is MALFORMED iff `parse_error` is not None; then
    `amount` is None as well.

    - `doc_number = row[0].strip()`, `amount_raw = row[1].strip() if len(row) > 1 else ""`.
    - Non-numeric / missing amount → the float-style error text (checked first, as the original did).
    - D1: an empty DocNumber is MALFORMED too (the original crashed with IndexError).
    """
    doc_number = row[0].strip() if len(row) > 0 else ""
    amount_raw = row[1].strip() if len(row) > 1 else ""
    amount, error = parse_amount(amount_raw)
    if error is None and doc_number == "":
        amount, error = None, EMPTY_DOC_NUMBER_ERROR  # D1
    return InputRow(line_no=line_no, doc_number=doc_number, amount_raw=amount_raw, amount=amount,
                    parse_error=error)


def parse_rows(rows: Iterable[Sequence[str]]) -> list[InputRow]:
    """csv.reader rows → InputRows: empty lists (blank lines) are skipped; `line_no` counts the remaining
    rows from 1 (01 §2). The caller opens the file with `open(path, "r", newline="", encoding="utf-8-sig")`."""
    parsed: list[InputRow] = []
    for row in rows:
        if not row:
            continue
        parsed.append(parse_row(row, len(parsed) + 1))
    return parsed


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §3 — suffix classification
# ════════════════════════════════════════════════════════════════════════════════════════════════
def classify(row: InputRow) -> ParsedRow:
    """01 §3 suffix table, evaluated in order (`last = doc_number[-1].lower()`).

    | 0 | MALFORMED                          | base None                      |
    | 1 | last is 0-9                        | STANDARD    base doc_number    |
    | 2 | last == "a" and [-2:] == "aa"      | BOTH_SENT   base doc[:-2]      |
    | 3 | last == "a" and (?i)[^a]a$         | NOTIF1_SENT base doc[:-1]      |
    | 4 | last == "a" (the 1-char "a")       | MANUAL (D4)                    |
    | 5 | last == "r"                        | SKIP_R                         |
    | 6 | anything else                      | MANUAL                         |
    The base booking is then `.strip().upper()`-ed (D5). The QBO lookup always uses `row.doc_number`.
    """
    if row.parse_error is not None or not row.doc_number:
        return ParsedRow(row=row, kind=RowKind.MALFORMED, base_booking=None)
    doc = row.doc_number
    last = doc[-1].lower()
    if last in _ASCII_DIGITS:
        kind, base = RowKind.STANDARD, doc
    elif last == "a" and doc[-2:].lower() == "aa":
        kind, base = RowKind.BOTH_SENT, doc[:-2].strip()
    elif last == "a" and _NOTIF1_RE.search(doc):
        kind, base = RowKind.NOTIF1_SENT, doc[:-1].strip()
    elif last == "a":
        return ParsedRow(row=row, kind=RowKind.MANUAL, base_booking=None)  # D4: reported, not ignored
    elif last == "r":
        return ParsedRow(row=row, kind=RowKind.SKIP_R, base_booking=None)
    else:
        return ParsedRow(row=row, kind=RowKind.MANUAL, base_booking=None)
    return ParsedRow(row=row, kind=kind, base_booking=base.strip().upper())  # D5


def is_below_threshold(parsed: ParsedRow, min_amount: Decimal = DEFAULT_MIN_INVOICE_AMOUNT) -> bool:
    """01 §3: `amount < ETA_MIN_INVOICE_AMOUNT` for STANDARD / NOTIF1_SENT / BOTH_SENT only."""
    amount = parsed.row.amount
    return parsed.kind in THRESHOLD_KINDS and amount is not None and amount < min_amount


def initial_route(parsed: ParsedRow, min_amount: Decimal = DEFAULT_MIN_INVOICE_AMOUNT) -> Route | None:
    """Route of a row that needs no carrier lookup (01 §6 top / §7), or None for "carrier lookup".

    MALFORMED → FAILED (malformed entry) · SKIP_R → SKIPPED · MANUAL → FAILED (raw DocNumber) ·
    STANDARD/NOTIF1_SENT/BOTH_SENT below the threshold → BINDU (checked before the BOTH_SENT → BEN routing) ·
    BOTH_SENT → BEN · STANDARD/NOTIF1_SENT → None.
    """
    kind = parsed.kind
    if kind is RowKind.MALFORMED or kind is RowKind.MANUAL:
        return Route.FAILED
    if kind is RowKind.SKIP_R:
        return Route.SKIPPED
    if is_below_threshold(parsed, min_amount):
        return Route.BINDU
    if kind is RowKind.BOTH_SENT:
        return Route.BEN
    return None


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §2 D2 / D3 — header row and duplicates
# ════════════════════════════════════════════════════════════════════════════════════════════════
def is_header_row(row: InputRow) -> bool:
    """D2: the FIRST row (line 1) is MALFORMED, its amount cell is non-numeric, and its first cell
    contains no digit (e.g. `Num,Amount`)."""
    if row.line_no != 1 or row.parse_error is None:
        return False
    amount_is_numeric = parse_amount(row.amount_raw)[1] is None
    return not amount_is_numeric and not any(ch.isdigit() for ch in row.doc_number)


def drop_header_row(parsed: Sequence[ParsedRow]) -> tuple[list[ParsedRow], ParsedRow | None]:
    """D2: remove the header row (if any) and log it at INFO; returns (remaining rows, skipped header)."""
    remaining: list[ParsedRow] = []
    header: ParsedRow | None = None
    for p in parsed:
        if header is None and is_header_row(p.row):
            header = p
            log.info("Line 1 looks like a header row (%r, %r) - skipped", p.row.doc_number, p.row.amount_raw)
            continue
        remaining.append(p)
    return remaining, header


def find_duplicates(parsed: Iterable[ParsedRow]) -> dict[int, ParsedRow]:
    """D3: `{line_no of every later occurrence: ParsedRow of the first occurrence}`.

    Only the first occurrence of a DocNumber is processed; the later ones are reported with
    `duplicate_entry()`. Keys compare the DocNumber case-insensitively (`casefold`) because the QBO
    lookup falls back to a case-insensitive match (04 §6.3), so `EBKG1` and `ebkg1` can resolve to the
    same invoice and must not be e-mailed twice. MALFORMED rows never take part: each is reported with
    its own malformed entry, and a malformed row does not hide a later valid row.
    """
    first_by_key: dict[str, ParsedRow] = {}
    duplicates: dict[int, ParsedRow] = {}
    for p in parsed:
        if p.kind is RowKind.MALFORMED:
            continue
        key = p.row.doc_number.casefold()
        first = first_by_key.get(key)
        if first is None:
            first_by_key[key] = p
        else:
            duplicates[p.row.line_no] = first
    return duplicates


def duplicate_entry(duplicate: ParsedRow) -> str:
    """Failed_To_Process.txt entry for a later occurrence (D3): its DocNumber and its own line number."""
    return texts.duplicate_row_entry(duplicate.row.doc_number, duplicate.row.line_no)


def malformed_entry(row: InputRow | ParsedRow) -> str:
    """Failed_To_Process.txt entry of a MALFORMED row, exactly as 01 §7.1."""
    input_row = row.row if isinstance(row, ParsedRow) else row
    error = input_row.parse_error
    if error is None:  # only for hand-built rows: parse_row() always sets parse_error on MALFORMED rows
        error = EMPTY_DOC_NUMBER_ERROR if not input_row.doc_number else ""
    return texts.malformed_row_entry(input_row.doc_number, input_row.amount_raw, error)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §6.1 — carrier lookup errors
# ════════════════════════════════════════════════════════════════════════════════════════════════
def carrier_failure(doc_number: str, exc: Exception) -> tuple[Route, OutputFile, str]:
    """Map an exception from `get_eta()` / `router.route()` to (route, file, entry) (01 §6.1).

    CustomsHoldError → CUSTOMS_HOLD / PotentialCustomsHold.txt; CarrierLookupError (incl.
    UnsupportedCarrierError) → "Carrier Lookup Failed."; NoEtaError → "Can't determine ETA."; anything
    else → "Unexpected error during carrier lookup.". All of them count as failed (01 §13).
    """
    if isinstance(exc, CustomsHoldError):
        return Route.CUSTOMS_HOLD, OutputFile.CUSTOMS, texts.customs_hold_entry(doc_number)
    if isinstance(exc, CarrierLookupError):
        return Route.FAILED, OutputFile.FAILED, texts.carrier_lookup_failed_entry(doc_number, exc)
    if isinstance(exc, NoEtaError):
        return Route.FAILED, OutputFile.FAILED, texts.cant_determine_eta_entry(doc_number, exc)
    return Route.FAILED, OutputFile.FAILED, texts.unexpected_carrier_error_entry(doc_number, exc)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §9 — DocNumber arrival suffix
# ════════════════════════════════════════════════════════════════════════════════════════════════
def with_arrival_suffix(doc_number: str) -> str:
    """01 §9 verbatim: `EBKG12345678` → `EBKG12345678 A`; `EBKG12345678 A` → `EBKG12345678 AA`."""
    if not doc_number:
        raise ValueError("cannot add the arrival suffix to an empty DocNumber")
    return doc_number + (" A" if doc_number[-1].isdigit() else "A")


def arrival_doc_number(doc_number: str, eta: datetime, today: date) -> str | None:
    """01 §6.4 step 2: the new DocNumber when `eta.date() <= today` (NOTIFY only), else None.

    Note O6: an ETA later today gets the suffix although the subject still says "ESTIMATED ARRIVING".
    The QBO length check (04 §7 `doc_number_problem`) is applied by the pipeline, never truncation.
    """
    return with_arrival_suffix(doc_number) if eta.date() <= today else None
