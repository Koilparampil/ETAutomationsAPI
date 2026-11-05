"""texts.py: e-mail subjects (01 §8), VShip notes (01 §10) and every output template (01 §14), byte-for-byte."""

from __future__ import annotations

from datetime import datetime, time

import pytest

from eta_automations import texts
from eta_automations.errors import NoEtaError

NOW = datetime(2026, 11, 5, 9, 30)
PAST = datetime(2026, 11, 3)     # "past ETA 11/03"
FUTURE = datetime(2026, 11, 8)   # "future ETA 11/08"
DOC = "EBKG12345678"

# 01 §8 golden outputs captured from the original function
SUBJECT_GOLDENS = [
    (False, False, PAST,
     "Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE CARGO****"),
    (False, False, FUTURE,
     "Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08. PLEASE MAKE PAYMENT ARRANGEMENT****"),
    (False, True, PAST,
     "Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE CLEAR THE CARGO****"),
    (False, True, FUTURE,
     "Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08.****"),
    (True, False, PAST,
     "Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE PAY AND CLEAR THIS PAST DUE INVOICE "
     "AND CLEAR THE CARGO****"),
    (True, False, FUTURE,
     "Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08. PLEASE MAKE PAYMENT ARRANGEMENT****"),
    (True, True, PAST,
     "Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE CLEAR THE CARGO****"),
    (True, True, FUTURE,
     "Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08.****"),
]


@pytest.mark.parametrize(("notif_sent", "paid", "eta", "expected"), SUBJECT_GOLDENS)
def test_subject_goldens(notif_sent: bool, paid: bool, eta: datetime, expected: str) -> None:
    assert texts.subject_for(DOC, eta, notif_sent=notif_sent, paid=paid, now=NOW) == expected


def test_all_eight_subject_goldens_are_covered() -> None:
    assert len({(n, p, e) for n, p, e, _ in SUBJECT_GOLDENS}) == 8


def test_subject_uses_the_given_doc_number_verbatim() -> None:
    subject = texts.subject_for("EBKG10000002 A", PAST, notif_sent=True, paid=False, now=NOW)
    assert subject.startswith("Invoice for Booking no. EBKG10000002 A. ****ARRIVED ON 11/03.")


def test_subject_eta_equal_to_now_takes_the_arrived_branch() -> None:
    assert texts.subject_for(DOC, NOW, notif_sent=False, paid=False, now=NOW) == (
        "Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/05. PLEASE PLEASE PAY AND CLEAR THE CARGO****"
    )


def test_subject_with_time_of_day_later_today_is_estimated() -> None:
    later_today = datetime(2026, 11, 5, 14, 0)  # O6: subject says ESTIMATED although the DocNumber gets an A
    assert texts.subject_for(DOC, later_today, notif_sent=False, paid=False, now=NOW) == (
        "Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/05. PLEASE MAKE PAYMENT ARRANGEMENT****"
    )


class _Incomparable(datetime):
    """A datetime for which neither `eta <= now` nor `eta >= now` holds — the only way to reach the fallback."""

    def __le__(self, other: object) -> bool:
        return False

    def __ge__(self, other: object) -> bool:
        return False


@pytest.mark.parametrize("notif_sent", [False, True])
@pytest.mark.parametrize("paid", [False, True])
def test_unreachable_fallback_branch(notif_sent: bool, paid: bool) -> None:
    eta = _Incomparable(2026, 11, 3)
    assert texts.subject_for(DOC, eta, notif_sent=notif_sent, paid=paid, now=NOW) == (
        "Invoice for Booking no. EBKG12345678. ***Please Check Attached Invoice for ETA***"
    )


# ── 01 §10 VShip note ────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("eta", "notif_index", "expected"),
    [
        (datetime(2026, 11, 3), 0, "ARRIVED ON 11/03. NOTIF #1"),
        (datetime(2026, 11, 3), 1, "ARRIVED ON 11/03. NOTIF #2"),
        (datetime(2026, 11, 18), 0, "ARRIVING ON 11/18."),
        (datetime(2026, 11, 18), 1, "ARRIVING ON 11/18."),
        (datetime(2026, 12, 1), 0, "ARRIVING ON 12/01."),
        (NOW, 0, "ARRIVING ON 11/05."),                         # eta == now is not "arrived" (eta < now)
        (datetime(2026, 11, 5, 9, 29), 0, "ARRIVED ON 11/05. NOTIF #1"),
    ],
)
def test_vship_comment_for(eta: datetime, notif_index: int, expected: str) -> None:
    assert texts.vship_comment_for(eta, notif_index=notif_index, now=NOW) == expected


# ── 01 §14 Failed_To_Process.txt templates ───────────────────────────────────────────────────────────
def test_malformed_row_entry() -> None:
    assert texts.malformed_row_entry("EBKG10000018", "abc", "could not convert string to float: 'abc'") == (
        "[ERROR] Row {'inv_number': 'EBKG10000018', 'amount_due': 'abc'} - Malformed row (missing amount "
        "column or non-numeric amount).\ncould not convert string to float: 'abc'\n"
    )


def test_manual_entry_is_the_raw_doc_number() -> None:
    assert texts.manual_entry("NAM1000017 C") == "NAM1000017 C"


@pytest.mark.parametrize(
    ("helper", "expected"),
    [
        (texts.carrier_lookup_failed_entry, "[ERROR] #EBKG1 A - Carrier Lookup Failed.\nboom\n"),
        (texts.cant_determine_eta_entry, "[ERROR] #EBKG1 A -  Can't determine ETA.\nboom\n"),
        (texts.unexpected_carrier_error_entry, "[ERROR] #EBKG1 A - Unexpected error during carrier lookup.\nboom\n"),
        (texts.notif_lookup_failed_entry, "[ERROR] #EBKG1 A - VShip notification lookup failed, nothing sent\nboom\n"),
        (texts.qb_processing_error_entry, "[ERROR] #EBKG1 A - QB Invoice Processing Error, No Email Sent\nboom\n"),
        (texts.note_write_error_entry,
         "[ERROR] #EBKG1 A - Note Write Error, QB Invoice Processing Successful\nboom\n"),
        (texts.booking_eta_change_failed_entry,
         "[ERROR] #EBKG1 A - Booking ETA Change Failed, QB Invoice Processing Successful\nboom\n"),
    ],
)
def test_error_templates(helper: object, expected: str) -> None:
    assert helper("EBKG1 A", "boom") == expected  # type: ignore[operator]
    assert helper("EBKG1 A", RuntimeError("boom")) == expected  # type: ignore[operator]  # {err} = str(exc)


def test_error_template_uses_str_of_the_exception() -> None:
    assert texts.cant_determine_eta_entry("NAM1", NoEtaError("no ETA in payload")) == (
        "[ERROR] #NAM1 -  Can't determine ETA.\nno ETA in payload\n"
    )


def test_skip_templates() -> None:
    assert texts.done_in_last_week_entry("EBKG10000010 A") == "[SKIP] #EBKG10000010 A - Done in the last week"
    assert texts.not_found_in_quickbooks_entry("EBKG10000011") == "[SKIP] #EBKG10000011 - not found in QuickBooks"
    assert texts.duplicate_row_entry("EBKG10000029", 7) == (
        "[SKIP] #EBKG10000029 - duplicate row in input file (line 7)"
    )


@pytest.mark.parametrize("sent_at", [datetime(2026, 11, 5, 8, 7, 59), time(8, 7)])
def test_already_emailed_entry(sent_at: datetime | time) -> None:
    assert texts.already_emailed_entry("EBKG10000028", sent_at) == (
        "[SKIP] #EBKG10000028 - already e-mailed today at 08:07 (ledger); use --force to resend"
    )


# ── 01 §14 other files ───────────────────────────────────────────────────────────────────────────────
def test_other_output_entries() -> None:
    assert texts.ben_entry("EBKG10000015 AA") == "EBKG10000015 AA"
    assert texts.accounting_entry("EBKG10000005") == "EBKG10000005 - is partially paid and has a TR/SWB note"
    assert texts.customs_hold_entry("EBKG10000021") == "[ERROR] #EBKG10000021 -  Possible Customs Hold."
    assert texts.bindu_row("EBKG10000013", "850", "ACME") == ["EBKG10000013", "850", "ACME"]
    assert texts.bindu_row("EBKG10000013", "850", "") == ["EBKG10000013", "850", ""]  # D14


# ── fixed log lines and 10-pipeline notes ────────────────────────────────────────────────────────────
def test_fixed_log_lines() -> None:
    assert texts.SKIP_R_LOG == "Don't need to do this one, ends in R"
    assert texts.notif_lookup_defaulted_warning("boom") == "Notif lookup failed: boom — defaulting to False"
    assert texts.ok_log_line("EBKG1") == "[OK]    #EBKG1"
    assert texts.done_line(3, 2) == "Done: 3 succeeded, 2 failed/skipped."
    assert texts.reverted_note("EBKG1") == "(invoice number reverted to EBKG1)"
    assert texts.revert_failed_note(RuntimeError("x")) == "(could not revert invoice number: x)"
    assert texts.BILL_EMAIL_MISSING_REASON == "Invoice has no customer e-mail address (BillEmail)"
    assert texts.qb_processing_error_entry("EBKG10000027", texts.BILL_EMAIL_MISSING_REASON) == (
        "[ERROR] #EBKG10000027 - QB Invoice Processing Error, No Email Sent\n"
        "Invoice has no customer e-mail address (BillEmail)\n"
    )
    assert texts.NO_EMAIL_PENDING_MESSAGE == "e-mail pending (manual send, --no-email)"
