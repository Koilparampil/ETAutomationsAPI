"""Every customer-, VShip- and output-file-facing text of the program (01-business-rules §6–§14).

- `subject_for`        — e-mail subject (01 §8), ported verbatim from `stringTing/subjectEdit.py`.
- `vship_comment_for`  — VShip internal note (01 §10), ported verbatim from `VShip/writeNotif.py`.
- one helper per entry template of 01 §14 (Failed_To_Process.txt, Send2Ben.txt, Send2Accounting.txt,
  PotentialCustomsHold.txt, Send2Bindu.csv) so the pipeline never hand-writes these strings.
- the few log/console lines whose wording 01 fixes (§6, §6.2, §6.6, §13) and the D11 revert notes and
  guard reasons of 10-pipeline §2c / Phase 4.

All strings are byte-for-byte; do not "fix" them: `PLEASE PLEASE` (01 §16 O3), the asterisk counts, and
the two spaces in `-  Can't determine ETA.` / `-  Possible Customs Hold.` (O2) are intentional.
`{err}` in every template is `str(exception)` (01 §14). Text-file entries are written by
`OutputWriter.write()` as `entry + "\\n"` (10 §5), so templates that end in "\\n" leave a blank line.
Pure functions only (no I/O).
"""

from __future__ import annotations

from datetime import datetime, time

__all__ = [
    "BILL_EMAIL_MISSING_REASON",
    "NO_EMAIL_PENDING_MESSAGE",
    "SKIP_R_LOG",
    "accounting_entry",
    "already_emailed_entry",
    "ben_entry",
    "bindu_row",
    "booking_eta_change_failed_entry",
    "cant_determine_eta_entry",
    "carrier_lookup_failed_entry",
    "customs_hold_entry",
    "done_in_last_week_entry",
    "done_line",
    "duplicate_row_entry",
    "malformed_row_entry",
    "manual_entry",
    "not_found_in_quickbooks_entry",
    "note_write_error_entry",
    "notif_lookup_defaulted_warning",
    "notif_lookup_failed_entry",
    "ok_log_line",
    "qb_processing_error_entry",
    "revert_failed_note",
    "reverted_note",
    "same_booking_notice_not_sent_reason",
    "subject_for",
    "unexpected_carrier_error_entry",
    "vship_comment_for",
]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §8 — e-mail subject
# ════════════════════════════════════════════════════════════════════════════════════════════════
def subject_for(doc_number: str, eta: datetime, *, notif_sent: bool, paid: bool, now: datetime) -> str:
    """E-mail subject, verbatim port of `subjectDecision` (01 §8).

    `doc_number` is the invoice DocNumber BEFORE any change (01 §6.4 step 1). `notif_sent` is True
    exactly when the VShip notif status count is 1. In practice `paid` is always False (paid invoices
    take PAID_UPDATE). When eta == now both "arrived" and "estimated" hold; "arrived" wins (order kept).
    The final fallback is unreachable with real datetimes (exactly one of <, ==, > holds) — kept for parity.
    """
    d = eta.strftime("%m/%d")
    p = f"Invoice for Booking no. {doc_number}."
    arrived, estimated = eta <= now, eta >= now
    if notif_sent:
        if arrived and paid:
            return f"{p} ****ARRIVED ON {d}. PLEASE CLEAR THE CARGO****"
        if arrived and not paid:
            return f"{p} ****ARRIVED ON {d}. PLEASE PAY AND CLEAR THIS PAST DUE INVOICE AND CLEAR THE CARGO****"
        if estimated and paid:
            return f"{p} ***ESTIMATED ARRIVING ON {d}.****"
        if estimated and not paid:
            return f"{p} ***ESTIMATED ARRIVING ON {d}. PLEASE MAKE PAYMENT ARRANGEMENT****"
        return f"{p} ***Please Check Attached Invoice for ETA***"
    else:
        if arrived and paid:
            return f"{p} ****ARRIVED ON {d}. PLEASE CLEAR THE CARGO****"
        if arrived and not paid:
            # O3: "PLEASE PLEASE" is the original's text — keep it.
            return f"{p} ****ARRIVED ON {d}. PLEASE PLEASE PAY AND CLEAR THE CARGO****"
        if estimated and paid:
            return f"{p} ***ESTIMATED ARRIVING ON {d}.****"
        if estimated and not paid:
            return f"{p} ***ESTIMATED ARRIVING ON {d}. PLEASE MAKE PAYMENT ARRANGEMENT****"
        return f"{p} ***Please Check Attached Invoice for ETA***"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §10 — VShip note
# ════════════════════════════════════════════════════════════════════════════════════════════════
def vship_comment_for(eta: datetime, *, notif_index: int, now: datetime) -> str:
    """VShip internal comment (01 §10): `ARRIVED ON mm/dd. NOTIF #n` when eta < now, else `ARRIVING ON mm/dd.`.

    `notif_index` is 0 for the first notice and 1 for the second (01 §6.4 step 5); FUTURE_UPDATE passes 0.
    Posted with commentType 1 (05 §4 V5).
    """
    if eta < now:
        return f"ARRIVED ON {eta.strftime('%m/%d')}. NOTIF #{notif_index + 1}"
    return f"ARRIVING ON {eta.strftime('%m/%d')}."


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §14 — Failed_To_Process.txt templates (in the order 01 §14 lists them)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def malformed_row_entry(doc_number: str, amount_raw: str, error: object) -> str:
    """01 §7.1: the original's dict repr, the fixed reason, then the error text and a trailing newline.

    `error` is normally `InputRow.parse_error`, e.g. "could not convert string to float: 'abc'".
    """
    row = {"inv_number": doc_number, "amount_due": amount_raw}
    return f"[ERROR] Row {row!r} - Malformed row (missing amount column or non-numeric amount).\n{error}\n"


def manual_entry(doc_number: str) -> str:
    """MANUAL rows (01 §6): just the raw DocNumber."""
    return doc_number


def carrier_lookup_failed_entry(doc_number: str, error: object) -> str:
    """01 §6.1: CarrierLookupError (incl. UnsupportedCarrierError)."""
    return f"[ERROR] #{doc_number} - Carrier Lookup Failed.\n{error}\n"


def cant_determine_eta_entry(doc_number: str, error: object) -> str:
    """01 §6.1: NoEtaError. Two spaces after '-' (O2)."""
    return f"[ERROR] #{doc_number} -  Can't determine ETA.\n{error}\n"


def unexpected_carrier_error_entry(doc_number: str, error: object) -> str:
    """01 §6.1: any other exception from the carrier lookup."""
    return f"[ERROR] #{doc_number} - Unexpected error during carrier lookup.\n{error}\n"


def notif_lookup_failed_entry(doc_number: str, error: object) -> str:
    """01 §6.2: only with ETA_ON_NOTIF_LOOKUP_ERROR=fail."""
    return f"[ERROR] #{doc_number} - VShip notification lookup failed, nothing sent\n{error}\n"


def done_in_last_week_entry(doc_number: str) -> str:
    """01 §6 step 3c (D8): Notif #1 sent less than 7 days ago."""
    return f"[SKIP] #{doc_number} - Done in the last week"


def not_found_in_quickbooks_entry(doc_number: str) -> str:
    """01 §6 steps 3d / d' (D9)."""
    return f"[SKIP] #{doc_number} - not found in QuickBooks"


def duplicate_row_entry(doc_number: str, line_no: int) -> str:
    """01 §2 D3: a later occurrence of a DocNumber; `line_no` is that row's own line number (01 §2)."""
    return f"[SKIP] #{doc_number} - duplicate row in input file (line {line_no})"


def qb_processing_error_entry(doc_number: str, error: object) -> str:
    """01 §6.6 (D13): any QBO step (lookup aside), the TR/SWB lookup, NOTIFY guards (10 §2c), D21."""
    return f"[ERROR] #{doc_number} - QB Invoice Processing Error, No Email Sent\n{error}\n"


def note_write_error_entry(doc_number: str, error: object) -> str:
    """01 §6.6 (D13): resolving the VShip booking id or posting the comment, after QBO succeeded."""
    return f"[ERROR] #{doc_number} - Note Write Error, QB Invoice Processing Successful\n{error}\n"


def booking_eta_change_failed_entry(doc_number: str, error: object) -> str:
    """01 §6.6 (D12/D13): the VShip expected-arrival PATCH, after everything else succeeded."""
    return f"[ERROR] #{doc_number} - Booking ETA Change Failed, QB Invoice Processing Successful\n{error}\n"


def already_emailed_entry(doc_number: str, sent_at: datetime | time) -> str:
    """10 §2c ledger guard: `sent_at` is what `Ledger.emailed_on()` returned; rendered as HH:MM."""
    return f"[SKIP] #{doc_number} - already e-mailed today at {sent_at:%H:%M} (ledger); use --force to resend"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §14 — the other output files
# ════════════════════════════════════════════════════════════════════════════════════════════════
def ben_entry(doc_number: str) -> str:
    """Send2Ben.txt line (01 §7.3)."""
    return doc_number


def accounting_entry(doc_number: str) -> str:
    """Send2Accounting.txt line (01 §7.4)."""
    return f"{doc_number} - is partially paid and has a TR/SWB note"


def customs_hold_entry(doc_number: str) -> str:
    """PotentialCustomsHold.txt line (01 §6.1). Two spaces after '-' (O2)."""
    return f"[ERROR] #{doc_number} -  Possible Customs Hold."


def bindu_row(doc_number: str, amount_raw: str, customer_name: str) -> list[str]:
    """Send2Bindu.csv row `[doc_number, amount_raw, customer_name]` (01 §7.2); "" name on lookup failure (D14)."""
    return [doc_number, amount_raw, customer_name]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Log / console lines fixed by 01, and 10-pipeline notes
# ════════════════════════════════════════════════════════════════════════════════════════════════
SKIP_R_LOG = "Don't need to do this one, ends in R"  # 01 §6: SKIP_R rows (no file entry)

# 10 §2c NOTIFY guard reason (P27); used inside qb_processing_error_entry(doc, reason).
BILL_EMAIL_MISSING_REASON = "Invoice has no customer e-mail address (BillEmail)"

# 10 Phase 3 step 1: final message of NOTIFY rows under --no-email.
NO_EMAIL_PENDING_MESSAGE = "e-mail pending (manual send, --no-email)"


def notif_lookup_defaulted_warning(error: object) -> str:
    """01 §6.2 'send' default: WARNING text (with an em dash, as specified)."""
    return f"Notif lookup failed: {error} — defaulting to False"


def ok_log_line(doc_number: str) -> str:
    """01 §6.6: logged when every action of a row succeeded (four spaces after [OK])."""
    return f"[OK]    #{doc_number}"


def done_line(ok: int, failed: int) -> str:
    """01 §13 final summary line (parity)."""
    return f"Done: {ok} succeeded, {failed} failed/skipped."


def reverted_note(old_doc_number: str) -> str:
    """10 Phase 4 / D11: appended to the send error when the DocNumber was put back."""
    return f"(invoice number reverted to {old_doc_number})"


def revert_failed_note(error: object) -> str:
    """10 Phase 4 / D11: appended to the send error when putting the DocNumber back failed too."""
    return f"(could not revert invoice number: {error})"


def same_booking_notice_not_sent_reason(giver_doc_number: str) -> str:
    """Review fix NEW-02: reason (inside qb_processing_error_entry) for a row of a booking whose notice in this run
    was to come from an earlier row (`giver_doc_number`) whose e-mail was then not sent (or its outcome is unknown)."""
    return (f"not processed: #{giver_doc_number} (same booking) was to give this booking's notice in this run, but its "
            "e-mail was not confirmed as sent - run the file again")
