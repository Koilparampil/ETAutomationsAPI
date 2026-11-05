"""rules.py: input rows, suffix classification, thresholds, D1–D5, malformed entry, duplicates, header skip,
arrival suffix, carrier-error mapping (01-business-rules §2–§3, §6.1, §7, §9; 12-testing §3).

The 01 §4 router table is tested with carriers/router.py (test_router.py), not here.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime
from decimal import Decimal

import pytest

from eta_automations import rules
from eta_automations.errors import (
    AggregatorBookingError,
    CarrierLookupError,
    CustomsHoldError,
    NoEtaError,
    UnsupportedCarrierError,
)
from eta_automations.models import InputRow, OutputFile, ParsedRow, Route, RowKind


def parse(doc: str, amount: str | None = "2,450.00", line_no: int = 1) -> ParsedRow:
    cells = [doc] if amount is None else [doc, amount]
    return rules.classify(rules.parse_row(cells, line_no))


# ── 01 §3 examples: (doc_number, amount) → (kind, base, route); route None = "carrier lookup" ──────────
EXAMPLES = [
    ("EBKG17208602", "2,450.00", RowKind.STANDARD, "EBKG17208602", None),
    ("267546627", "2,450.00", RowKind.STANDARD, "267546627", None),
    ("EBKG17208602 A", "2,450.00", RowKind.NOTIF1_SENT, "EBKG17208602", None),
    ("EBKG17208602a", "2,450.00", RowKind.NOTIF1_SENT, "EBKG17208602", None),
    ("EBKG17208602 AA", "2,450.00", RowKind.BOTH_SENT, "EBKG17208602", Route.BEN),
    ("EBKG17208602AA", "2,450.00", RowKind.BOTH_SENT, "EBKG17208602", Route.BEN),
    ("EBKG17208602 AA", "850.00", RowKind.BOTH_SENT, "EBKG17208602", Route.BINDU),  # amount check first
    ("EBKG17208602 A R", "2,450.00", RowKind.SKIP_R, None, Route.SKIPPED),
    ("NAM1234567 C", "2,450.00", RowKind.MANUAL, None, Route.FAILED),
    ("267546627/$150/RlsR", "2,450.00", RowKind.SKIP_R, None, Route.SKIPPED),
    ("EBKG17208602", "abc", RowKind.MALFORMED, None, Route.FAILED),
]


@pytest.mark.parametrize(("doc", "amount", "kind", "base", "route"), EXAMPLES)
def test_01_section3_examples(doc: str, amount: str, kind: RowKind, base: str | None, route: Route | None) -> None:
    p = parse(doc, amount)
    assert p.kind is kind
    assert p.base_booking == base
    assert rules.initial_route(p) == route
    assert p.row.doc_number == doc  # the QBO lookup always uses doc_number exactly as in the file


def test_examples_cover_every_row_of_the_table() -> None:
    assert len(EXAMPLES) == 11


# ── suffix rules one by one ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("doc", "kind", "base"),
    [
        ("EBKG12345670", RowKind.STANDARD, "EBKG12345670"),         # rule 1: any last digit 0-9
        ("ebkg17208602", RowKind.STANDARD, "EBKG17208602"),         # D5: base upper-cased
        ("EBKG17208602 aa", RowKind.BOTH_SENT, "EBKG17208602"),     # rule 2 is case-insensitive
        ("EBKG17208602 aA", RowKind.BOTH_SENT, "EBKG17208602"),
        ("ebkg17208602 a", RowKind.NOTIF1_SENT, "EBKG17208602"),    # rule 3 + D5
        ("NAM1234567A", RowKind.NOTIF1_SENT, "NAM1234567"),
        ("a", RowKind.MANUAL, None),                                # rule 4 (D4): reported, not ignored
        ("A", RowKind.MANUAL, None),
        ("EBKG17208602 r", RowKind.SKIP_R, None),                   # rule 5 is case-insensitive
        ("EBKG17208602R", RowKind.SKIP_R, None),
        ("NAM1234567 C", RowKind.MANUAL, None),                     # rule 6
        ("EBKG17208602 X", RowKind.MANUAL, None),
        ("EBKG1720860²", RowKind.MANUAL, None),                     # "a digit 0-9" only
    ],
)
def test_suffix_rules(doc: str, kind: RowKind, base: str | None) -> None:
    p = parse(doc)
    assert p.kind is kind
    assert p.base_booking == base


def test_doc_number_case_is_kept_for_qbo_while_base_is_upper_cased() -> None:
    p = parse("ebkg17208602 a")
    assert p.row.doc_number == "ebkg17208602 a"
    assert p.base_booking == "EBKG17208602"


def test_cells_are_stripped() -> None:
    p = parse("  EBKG17208602 A  ", "  $2,450.00 ")
    assert p.row.doc_number == "EBKG17208602 A"
    assert p.row.amount_raw == "$2,450.00"
    assert p.row.amount == Decimal("2450.00")
    assert p.base_booking == "EBKG17208602"


# ── 01 §2 amounts and MALFORMED rows ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2,450.00", Decimal("2450.00")),
        ("$1,000", Decimal("1000")),
        ("850", Decimal("850")),
        ("0", Decimal("0")),
        ("-12.5", Decimal("-12.5")),
        ("1e3", Decimal("1E+3")),
        ("1_000", Decimal("1000")),  # float() accepts it too
    ],
)
def test_amount_parsing_uses_decimal(raw: str, expected: Decimal) -> None:
    row = rules.parse_row(["EBKG17208602", raw], 1)
    assert row.parse_error is None
    assert row.amount == expected
    assert isinstance(row.amount, Decimal)


MALFORMED_ABC = (
    "[ERROR] Row {'inv_number': 'EBKG17208602', 'amount_due': 'abc'} - Malformed row "
    "(missing amount column or non-numeric amount).\ncould not convert string to float: 'abc'\n"
)
MALFORMED_ONE_COLUMN = (
    "[ERROR] Row {'inv_number': 'EBKG17208602', 'amount_due': ''} - Malformed row "
    "(missing amount column or non-numeric amount).\ncould not convert string to float: ''\n"
)


def test_malformed_message_for_abc_amount() -> None:
    row = rules.parse_row(["EBKG17208602", "abc"], 4)
    assert row.parse_error == "could not convert string to float: 'abc'"
    assert row.amount is None
    p = rules.classify(row)
    assert p.kind is RowKind.MALFORMED and p.base_booking is None
    assert rules.malformed_entry(row) == MALFORMED_ABC
    assert rules.malformed_entry(p) == MALFORMED_ABC


def test_malformed_message_for_a_one_column_row() -> None:
    row = rules.parse_row(["EBKG17208602"], 2)
    assert row.amount_raw == ""
    assert row.parse_error == "could not convert string to float: ''"
    assert rules.classify(row).kind is RowKind.MALFORMED
    assert rules.malformed_entry(row) == MALFORMED_ONE_COLUMN


def test_malformed_error_text_uses_the_cleaned_amount_and_python_repr() -> None:
    row = rules.parse_row(["EBKG1", "$1,2x"], 1)
    assert row.parse_error == "could not convert string to float: '12x'"
    assert row.amount_raw == "$1,2x"  # the dict repr shows the raw cell
    tricky = rules.parse_row(["O'BRIEN 1", "o'clock"], 1)
    # repr() switches to double quotes, exactly like the original's dict repr and float() message
    assert tricky.parse_error == "could not convert string to float: \"o'clock\""
    assert rules.malformed_entry(tricky).startswith(
        "[ERROR] Row {'inv_number': \"O'BRIEN 1\", 'amount_due': \"o'clock\"} - Malformed row"
    )


@pytest.mark.parametrize("raw", ["_1", "1_", "1__0", "sNaN"])
def test_strings_rejected_by_float_stay_malformed(raw: str) -> None:
    """Decimal() alone would accept these; the original's float() rejected them (parity)."""
    row = rules.parse_row(["EBKG17208602", raw], 1)
    assert row.parse_error == f"could not convert string to float: {raw!r}"
    assert rules.classify(row).kind is RowKind.MALFORMED


@pytest.mark.parametrize("raw", ["nan", "NaN", "inf", "-Infinity"])
def test_non_finite_amounts_are_malformed(raw: str) -> None:
    row = rules.parse_row(["EBKG17208602", raw], 1)
    assert row.parse_error == f"amount is not a finite number: {raw!r}"
    assert row.amount is None
    assert rules.classify(row).kind is RowKind.MALFORMED


def test_d1_empty_doc_number_is_malformed() -> None:
    row = rules.parse_row(["", "2,450.00"], 5)
    assert row.parse_error == rules.EMPTY_DOC_NUMBER_ERROR
    assert row.amount is None
    p = rules.classify(row)
    assert p.kind is RowKind.MALFORMED
    assert rules.initial_route(p) is Route.FAILED
    assert rules.malformed_entry(row) == (
        "[ERROR] Row {'inv_number': '', 'amount_due': '2,450.00'} - Malformed row "
        "(missing amount column or non-numeric amount).\nmissing invoice number (first column is empty)\n"
    )


def test_d1_with_bad_amount_reports_the_amount_error_first() -> None:
    row = rules.parse_row(["   ", "abc"], 1)
    assert row.parse_error == "could not convert string to float: 'abc'"


def test_malformed_is_checked_before_suffix_rules() -> None:
    """MALFORMED wins even for SKIP_R / MANUAL rows (the amount must still parse)."""
    assert parse("EBKG17208602 R", "abc").kind is RowKind.MALFORMED
    assert parse("NAM1234567 C", "").kind is RowKind.MALFORMED


def test_hand_built_malformed_row_without_parse_error() -> None:
    row = InputRow(line_no=1, doc_number="", amount_raw="10", amount=Decimal(10), parse_error=None)
    assert rules.classify(row).kind is RowKind.MALFORMED
    assert rules.malformed_entry(row).endswith("\n" + rules.EMPTY_DOC_NUMBER_ERROR + "\n")


def test_parse_rows_skips_blank_lines_and_numbers_the_rest() -> None:
    text = "﻿EBKG10000001,\"2,450.00\"\r\n\r\nNAM1000003,1800\n\n267546627 A,900\n"
    rows = rules.parse_rows(csv.reader(io.StringIO(text.removeprefix("﻿"), newline="")))
    assert [(r.line_no, r.doc_number, r.amount_raw) for r in rows] == [
        (1, "EBKG10000001", "2,450.00"),
        (2, "NAM1000003", "1800"),
        (3, "267546627 A", "900"),
    ]


def test_parse_row_with_extra_columns_uses_the_first_two() -> None:
    row = rules.parse_row(["EBKG17208602", "2", "450.00"], 1)  # unquoted thousands separator (parity)
    assert row.amount == Decimal("2")


# ── amount threshold (01 §3) ─────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("doc", "amount", "expected"),
    [
        ("EBKG17208602", "999.99", Route.BINDU),
        ("EBKG17208602", "1000", None),            # not below: carrier lookup
        ("EBKG17208602", "1,000.00", None),
        ("EBKG17208602 A", "850", Route.BINDU),
        ("EBKG17208602 AA", "850", Route.BINDU),   # before the BOTH_SENT → BEN routing
        ("EBKG17208602 AA", "5000", Route.BEN),
        ("EBKG17208602 R", "10", Route.SKIPPED),   # threshold does not apply to SKIP_R
        ("NAM1000017 C", "10", Route.FAILED),      # nor to MANUAL
    ],
)
def test_threshold(doc: str, amount: str, expected: Route | None) -> None:
    assert rules.initial_route(parse(doc, amount)) == expected


def test_threshold_is_configurable() -> None:
    p = parse("EBKG17208602", "1500")
    assert rules.initial_route(p, Decimal("2000")) is Route.BINDU
    assert rules.is_below_threshold(p, Decimal("2000")) is True
    assert rules.is_below_threshold(p) is False
    assert rules.is_below_threshold(parse("EBKG17208602 R", "10")) is False


# ── D2: header row ───────────────────────────────────────────────────────────────────────────────────
def test_header_row_is_skipped_with_an_info_log(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="eta")
    parsed = [parse("Num", "Amount", 1), parse("EBKG17208602", "2,450.00", 2)]
    remaining, header = rules.drop_header_row(parsed)
    assert header is parsed[0]
    assert remaining == [parsed[1]]
    assert any(r.levelno == logging.INFO and "header" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    ("cells", "line_no", "is_header"),
    [
        (["Num", "Amount"], 1, True),
        (["Invoice No.", "Amount Due"], 1, True),
        (["", "Amount"], 1, True),            # empty first cell has no digit either
        (["Num"], 1, True),                   # missing amount column is non-numeric
        (["Num", "Amount"], 2, False),        # only the first row can be a header
        (["Num1", "Amount"], 1, False),       # first cell contains a digit → reported as MALFORMED
        (["EBKG17208602", "abc"], 1, False),
        (["Num", "2450"], 1, False),          # amount is numeric (row is MANUAL, not MALFORMED)
        (["", "2450"], 1, False),             # D1 row: amount numeric → reported
    ],
)
def test_is_header_row(cells: list[str], line_no: int, is_header: bool) -> None:
    assert rules.is_header_row(rules.parse_row(cells, line_no)) is is_header


def test_non_header_malformed_first_row_is_kept() -> None:
    parsed = [parse("EBKG1", "abc", 1), parse("EBKG2", "2000", 2)]
    remaining, header = rules.drop_header_row(parsed)
    assert header is None
    assert remaining == parsed


# ── D3: duplicates ───────────────────────────────────────────────────────────────────────────────────
def test_duplicates_keep_only_the_first_occurrence() -> None:
    parsed = [
        parse("EBKG10000029", "5000", 1),
        parse("EBKG10000030", "5000", 2),
        parse("EBKG10000029", "5000", 3),
        parse("ebkg10000029", "5000", 4),   # same DocNumber for QBO's case-insensitive fallback (04 §6.3)
        parse("EBKG10000030 A", "5000", 5),  # a different DocNumber
    ]
    dups = rules.find_duplicates(parsed)
    assert dups == {3: parsed[0], 4: parsed[0]}
    assert rules.duplicate_entry(parsed[2]) == "[SKIP] #EBKG10000029 - duplicate row in input file (line 3)"
    assert rules.duplicate_entry(parsed[3]) == "[SKIP] #ebkg10000029 - duplicate row in input file (line 4)"


def test_duplicates_apply_to_every_non_malformed_kind() -> None:
    parsed = [parse("NAM1000017 C", "5000", 1), parse("NAM1000017 C", "5000", 2),
              parse("EBKG1 R", "5000", 3), parse("EBKG1 R", "5000", 4)]
    assert set(rules.find_duplicates(parsed)) == {2, 4}


def test_malformed_rows_never_count_as_duplicates() -> None:
    parsed = [parse("EBKG10000029", "abc", 1), parse("EBKG10000029", "5000", 2),
              parse("", "1", 3), parse("", "2", 4), parse("EBKG10000029", "", 5)]
    assert rules.find_duplicates(parsed) == {}


# ── 01 §9: arrival suffix ────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        ("EBKG12345678", "EBKG12345678 A"),
        ("EBKG12345678 A", "EBKG12345678 AA"),
        ("267546627", "267546627 A"),
        ("EBKG12345678a", "EBKG12345678aA"),
    ],
)
def test_with_arrival_suffix(doc: str, expected: str) -> None:
    assert rules.with_arrival_suffix(doc) == expected


def test_with_arrival_suffix_rejects_an_empty_doc_number() -> None:
    with pytest.raises(ValueError):
        rules.with_arrival_suffix("")


def test_arrival_doc_number_uses_the_date_rule() -> None:
    today = date(2026, 11, 5)
    assert rules.arrival_doc_number("EBKG1", datetime(2026, 11, 3), today) == "EBKG1 A"
    assert rules.arrival_doc_number("EBKG1 A", datetime(2026, 11, 5, 0, 0), today) == "EBKG1 AA"
    # O6: an ETA later today already gets the suffix
    assert rules.arrival_doc_number("EBKG1", datetime(2026, 11, 5, 23, 0), today) == "EBKG1 A"
    assert rules.arrival_doc_number("EBKG1", datetime(2026, 11, 6, 0, 0), today) is None


# ── 01 §6.1: carrier lookup errors ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("exc", "route", "file", "entry"),
    [
        (CarrierLookupError("boom"), Route.FAILED, OutputFile.FAILED,
         "[ERROR] #EBKG10000019 - Carrier Lookup Failed.\nboom\n"),
        (UnsupportedCarrierError("This is a Ref Number: 2026123456"), Route.FAILED, OutputFile.FAILED,
         "[ERROR] #EBKG10000019 - Carrier Lookup Failed.\nThis is a Ref Number: 2026123456\n"),
        (AggregatorBookingError("not tracked yet"), Route.FAILED, OutputFile.FAILED,
         "[ERROR] #EBKG10000019 - Carrier Lookup Failed.\nnot tracked yet\n"),
        (NoEtaError("none"), Route.FAILED, OutputFile.FAILED,
         "[ERROR] #EBKG10000019 -  Can't determine ETA.\nnone\n"),
        (CustomsHoldError("hold"), Route.CUSTOMS_HOLD, OutputFile.CUSTOMS,
         "[ERROR] #EBKG10000019 -  Possible Customs Hold."),
        (RuntimeError("x"), Route.FAILED, OutputFile.FAILED,
         "[ERROR] #EBKG10000019 - Unexpected error during carrier lookup.\nx\n"),
    ],
)
def test_carrier_failure_mapping(exc: Exception, route: Route, file: OutputFile, entry: str) -> None:
    assert rules.carrier_failure("EBKG10000019", exc) == (route, file, entry)
