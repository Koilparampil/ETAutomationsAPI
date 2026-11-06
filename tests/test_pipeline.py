"""Pipeline end to end with in-memory fakes and FixedClock(2026-11-05 09:30) — cutoff Mon 2026-11-16 09:30.

12-testing §4 matrix P1–P38 (one test each, P31 with its control case), then the 10 §4 phase rules the matrix
does not name: Phase 0 failures, read/selection, the MSC token pre-flight, 2b/2c corner cases, the report and
console summary, D11 revert failure, a TemplateRestoreError after a successful send, and Ctrl+C during apply.
Each case asserts the route, the writes executed against the fakes (in order), the exact output-file
contents and the counters (01 §13).
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import signal
import threading
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakes import (
    DEFAULT_EMAIL,
    WRITE_METHODS,
    Booking,
    FakeMscCarrier,
    FakeWorld,
    fake_build_services,
)

from eta_automations import pipeline
from eta_automations.carriers.msc import NO_TOKEN_MESSAGE
from eta_automations.carriers.router import CarrierRouter
from eta_automations.clock import FixedClock
from eta_automations.config import Settings
from eta_automations.errors import (
    AuthError,
    BookingChangeError,
    CarrierLookupError,
    ConfigError,
    CustomsHoldError,
    EmailPathUnavailableError,
    InvoiceSendError,
    InvoiceUpdateError,
    NoEtaError,
    NoteError,
    ServiceError,
    TemplateRestoreError,
)
from eta_automations.ledger import LEDGER_FILE_NAME
from eta_automations.models import Carrier, OutputFile, Plan, Route
from eta_automations.outputs import REPORT_COLUMNS, UTF8_BOM
from eta_automations.pipeline import RunSummary, read_input, run
from eta_automations.qbo.client import QboClient
from eta_automations.qbo.email import (
    RECOVERY_FILE_NAME,
    REDIRECT_NOT_ALLOWED,
    SendReceipt,
    manual_restore_instructions,
)
from eta_automations.run_lock import run_lock
from eta_automations.vship.client import VShipClient

RUN_ID = "20261105_093000"
ARRIVED = datetime(2026, 11, 3)                 # past, in window
CMA_ETA = datetime(2026, 11, 12, 14, 0)         # future, in window, with a time of day (CMA style)
OUT_OF_WINDOW = datetime(2026, 12, 1)
BOM = UTF8_BOM.encode("utf-8")

NOTIF1_OLD = [{"comment": "ARRIVED ON 10/20. NOTIF #1", "createdDate": "2026-10-20T10:00:00"}]       # (1, True)
NOTIF1_RECENT = [{"comment": "ARRIVED ON 11/02. NOTIF #1", "createdDate": "2026-11-02T10:00:00"}]    # (1, False)
NOTIF2 = [*NOTIF1_OLD, {"comment": "ARRIVED ON 10/27. NOTIF #2", "createdDate": "2026-10-27T10:00:00"}]  # (2, False)

SUBJECT_P1 = "Invoice for Booking no. EBKG10000001. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE CARGO****"
RESTORE_MSG = manual_restore_instructions("Invoice [Invoice No.] from ACME Logistics",
                                          Path("state") / "qbo_email_template_recovery.json")


def comment(text: str, created: str = "2026-10-01T10:00:00") -> dict[str, Any]:
    return {"comment": text, "createdDate": created}


def subject(doc: str, tail: str) -> str:
    return f"Invoice for Booking no. {doc}. {tail}"


ARRIVED_FIRST = "****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE CARGO****"
ARRIVED_SECOND = "****ARRIVED ON 11/03. PLEASE PAY AND CLEAR THIS PAST DUE INVOICE AND CLEAR THE CARGO****"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Harness
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class Result:
    summary: RunSummary
    out: str
    settings: Settings
    prompts: list[str]
    secret_prompts: list[str]

    def path(self, file: OutputFile) -> Path:
        return self.settings.run.output_dir / file.value

    def text(self, file: OutputFile) -> str | None:
        path = self.path(file)
        return path.read_text(encoding="utf-8") if path.exists() else None

    @property
    def failed_text(self) -> str | None:
        return self.text(OutputFile.FAILED)

    def plan(self, doc: str, nth: int = 0) -> Plan:
        return [p for p in self.summary.plans if p.doc_number == doc][nth]

    def ledger(self) -> list[dict[str, Any]]:
        path = self.settings.run.state_dir / LEDGER_FILE_NAME
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

    def report(self) -> dict[str, Any]:
        assert self.summary.report_json is not None
        return json.loads(self.summary.report_json.read_text(encoding="utf-8"))

    def no_output_files(self) -> bool:
        out = self.settings.run.output_dir
        return not any(self.path(f).exists() for f in OutputFile) and not list(out.glob("ToEmail_*.csv"))


Runner = Callable[..., Awaitable[Result]]


@pytest.fixture
def world(fixed_clock: FixedClock) -> FakeWorld:
    return FakeWorld(fixed_clock)


def write_input(tmp_path: Path, rows: Iterable[Booking | str], name: str = "input.csv") -> Path:
    path = tmp_path / name
    lines = [r.line if isinstance(r, Booking) else r for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def run_pipeline(world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path,
                 fixed_clock: FixedClock) -> Runner:
    async def _run(rows: Iterable[Booking | str], *, env: Mapping[str, str] | None = None, dry_run: bool = False,
                   assume_yes: bool = True, force: bool = False, no_email: bool = False,
                   only: set[str] | None = None, limit: int | None = None, answer: str = "y", tty: bool = True,
                   token: str = "eyJhbGciOiJIUzI1NiJ9.e30.c2ln", input_path: Path | None = None) -> Result:
        settings = make_settings(dict(env or {}))
        path = input_path or write_input(tmp_path, rows)
        prompts: list[str] = []
        secret_prompts: list[str] = []

        def input_fn(prompt: str) -> str:
            prompts.append(prompt)
            return answer

        def getpass_fn(prompt: str) -> str:
            secret_prompts.append(prompt)
            return token

        out = io.StringIO()
        summary = await run(settings, path, dry_run=dry_run, assume_yes=assume_yes, force=force, no_email=no_email,
                            only=only, limit=limit, clock=fixed_clock,
                            services=world.services(settings, read_only=dry_run), input_fn=input_fn,
                            is_tty=lambda: tty, getpass_fn=getpass_fn, out=out)
        return Result(summary, out.getvalue(), settings, prompts, secret_prompts)

    return _run


def upd(doc: str, eta: date | None, new_doc: str | None) -> tuple[Any, ...]:
    return ("qbo.update_invoice", doc, eta, new_doc)


def send(doc: str, subj: str) -> tuple[Any, ...]:
    return ("qbo.send_invoice", doc, subj)


def note(booking_id: str | None, text: str) -> tuple[Any, ...]:
    return ("vship.post_comment", booking_id, text)


def patch(booking_id: str | None, eta: str) -> tuple[Any, ...]:
    return ("vship.patch_expected_arrival", booking_id, eta)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Scenarios of P1–P29 (each builds its fakes and returns its input rows; P30 runs them all at once)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def p25_setup(w: FakeWorld) -> list[Booking | str]:
    b = w.add("EBKG10000025", eta=ARRIVED)
    assert b.invoice_id is not None
    w.qbo.send_errors[b.invoice_id] = InvoiceSendError("qbo", "send rejected: Business Validation Error")
    return [b]


def p26_setup(w: FakeWorld) -> list[Booking | str]:
    b = w.add("EBKG10000026", eta=ARRIVED)
    assert b.vship_id is not None
    w.vship.patch_errors[b.vship_id] = BookingChangeError(
        "vship", f"unexpected HTTP 500 for PATCH /Bookings/{b.vship_id}", status=500)
    return [b]


def p28_setup(w: FakeWorld) -> list[Booking | str]:
    b = w.add("EBKG10000028", eta=ARRIVED)
    w.emailed_today(b, datetime(2026, 11, 5, 8, 15))
    return [b]


def p29_setup(w: FakeWorld) -> list[Booking | str]:
    b = w.add("EBKG10000029", eta=ARRIVED)
    return [b, b]


SCENARIOS: dict[str, Callable[[FakeWorld], list[Booking | str]]] = {
    "P1": lambda w: [w.add("EBKG10000001", "2450.00", eta=ARRIVED)],
    "P2": lambda w: [w.add("EBKG10000002 A", "2450.00", eta=ARRIVED, comments=NOTIF1_OLD)],
    "P3": lambda w: [w.add("NAM1000003", "1800", eta=CMA_ETA)],
    "P4": lambda w: [w.add("EBKG10000004", eta=ARRIVED, invoice="paid")],
    "P5": lambda w: [w.add("EBKG10000005", eta=ARRIVED, invoice="partial", comments=[comment("SWB RCVD 10/12")])],
    "P6": lambda w: [w.add("EBKG10000006", eta=ARRIVED, invoice="partial", comments=[comment("TR PENDING")])],
    "P7": lambda w: [w.add("EBKG10000007", eta=OUT_OF_WINDOW)],
    "P8": lambda w: [w.add("EBKG10000008", eta=ARRIVED, comments=NOTIF2)],
    "P9": lambda w: [w.add("EBKG10000009", eta=ARRIVED, comments=NOTIF1_RECENT)],
    "P10": lambda w: [w.add("EBKG10000010 A", eta=ARRIVED, comments=NOTIF1_RECENT)],
    "P11": lambda w: [w.add("EBKG10000011", eta=ARRIVED, invoice=None)],
    "P12": lambda w: [w.add("EBKG10000012", eta=OUT_OF_WINDOW, invoice=None)],
    "P13": lambda w: [w.add("EBKG10000013", "850", customer="ACME", invoice=None)],
    "P14": lambda w: [w.add("EBKG10000014 AA", "850", customer="ACME", invoice=None)],
    "P15": lambda w: ["EBKG10000015 AA,5000"],
    "P16": lambda w: ["EBKG10000016 R,5000"],
    "P17": lambda w: ["NAM1000017 C,5000"],
    "P18": lambda w: ["EBKG10000018,abc"],
    "P19": lambda w: [w.add("EBKG10000019", eta=CarrierLookupError("boom"))],
    "P20": lambda w: [w.add("EBKG10000020", eta=NoEtaError("none"))],
    "P21": lambda w: [w.add("EBKG10000021", eta=CustomsHoldError("held at USSAV"))],
    "P22": lambda w: [w.add("EBKG10000022", eta=RuntimeError("x"))],
    "P23": lambda w: [w.add("EBKG10000023", eta=ARRIVED, vship=False)],
    "P24": lambda w: [w.add("EBKG10000024", eta=ARRIVED, vship=False)],
    "P25": p25_setup,
    "P26": p26_setup,
    "P27": lambda w: [w.add("EBKG10000027", eta=ARRIVED, bill_email=None)],
    "P28": p28_setup,
    "P29": p29_setup,
}


# ════════════════════════════════════════════════════════════════════════════════════════════════
# P1–P38
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_p1_notify_first_notice_after_arrival(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P1"](world)
    b = rows[0]
    assert isinstance(b, Booking)
    res = await run_pipeline(rows)
    plan = res.plan("EBKG10000001")
    assert plan.route is Route.NOTIFY and plan.status == "OK"
    assert world.writes() == [
        upd("EBKG10000001", date(2026, 11, 3), "EBKG10000001 A"),
        send("EBKG10000001 A", SUBJECT_P1),               # the subject keeps the DocNumber read before the change
        note(b.vship_id, "ARRIVED ON 11/03. NOTIF #1"),
        patch(b.vship_id, "2026-11-03T00:00:00"),
    ]
    assert (res.summary.ok, res.summary.failed, res.summary.exit_code) == (1, 0, 0)
    assert res.no_output_files()
    assert world.calls.named("qbo.prepare_for_sending") == [("qbo.prepare_for_sending", False)]
    records = res.ledger()
    assert [r["action"] for r in records] == ["qbo_update", "emailed", "vship_comment", "vship_patch_eta"]
    assert all(r["ok"] and r["invoice_id"] == b.invoice_id and r["run_id"] == RUN_ID for r in records)
    assert records[1]["detail"]["to"] == [DEFAULT_EMAIL] and "body" not in records[1]["detail"]
    assert "[OK]    #EBKG10000001" not in res.out   # logged, not printed
    assert "Done: 1 succeeded, 0 failed/skipped." in res.out


async def test_p2_notify_second_notice(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P2"](world)
    b = rows[0]
    assert isinstance(b, Booking)
    res = await run_pipeline(rows)
    assert res.plan("EBKG10000002 A").route is Route.NOTIFY
    assert world.writes() == [
        upd("EBKG10000002 A", date(2026, 11, 3), "EBKG10000002 AA"),
        send("EBKG10000002 AA", subject("EBKG10000002 A", ARRIVED_SECOND)),
        note(b.vship_id, "ARRIVED ON 11/03. NOTIF #2"),
        patch(b.vship_id, "2026-11-03T00:00:00"),
    ]
    # D16: VShip is searched with the base booking, never with the suffix attached
    assert ("vship.find_booking_id", "EBKG10000002") in world.calls
    assert res.plan("EBKG10000002 A").notif is not None and res.plan("EBKG10000002 A").notif.count == 1
    assert res.summary.ok == 1


async def test_p3_notify_future_eta_with_time_no_doc_change(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P3"](world)
    b = rows[0]
    assert isinstance(b, Booking)
    res = await run_pipeline(rows)
    assert res.plan("NAM1000003").route is Route.NOTIFY
    assert world.writes() == [
        upd("NAM1000003", date(2026, 11, 12), None),
        send("NAM1000003",
             subject("NAM1000003", "***ESTIMATED ARRIVING ON 11/12. PLEASE MAKE PAYMENT ARRANGEMENT****")),
        note(b.vship_id, "ARRIVING ON 11/12."),
        patch(b.vship_id, "2026-11-12T14:00:00"),
    ]
    assert world.carriers[Carrier.CMA].get_eta_calls == ["NAM1000003"]
    assert res.summary.ok == 1


async def test_p4_paid_update(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P4"](world)
    b = rows[0]
    assert isinstance(b, Booking)
    res = await run_pipeline(rows)
    plan = res.plan("EBKG10000004")
    assert plan.route is Route.PAID_UPDATE and plan.status == "OK"
    assert world.writes() == [upd("EBKG10000004", date(2026, 11, 3), None), patch(b.vship_id, "2026-11-03T00:00:00")]
    assert world.calls.named("qbo.prepare_for_sending") == []     # nothing to e-mail → no e-mail preflight
    assert (res.summary.ok, res.summary.failed) == (1, 0)


async def test_p5_partially_paid_with_tr_swb_note_goes_to_accounting(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P5"](world))
    assert res.plan("EBKG10000005").route is Route.ACCOUNTING
    assert res.text(OutputFile.ACCOUNTING) == "EBKG10000005 - is partially paid and has a TR/SWB note\n"
    assert world.writes() == []
    assert (res.summary.routed, res.summary.ok, res.summary.failed) == (1, 0, 0)


async def test_p6_partially_paid_without_tr_swb_note_is_notified(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P6"](world)
    res = await run_pipeline(rows)
    assert res.plan("EBKG10000006").route is Route.NOTIFY
    assert [c[0] for c in world.writes()] == ["qbo.update_invoice", "qbo.send_invoice", "vship.post_comment",
                                              "vship.patch_expected_arrival"]
    assert res.summary.ok == 1


async def test_p7_future_update(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P7"](world)
    b = rows[0]
    assert isinstance(b, Booking)
    res = await run_pipeline(rows)
    assert res.plan("EBKG10000007").route is Route.FUTURE_UPDATE
    assert world.writes() == [
        upd("EBKG10000007", date(2026, 12, 1), None),
        note(b.vship_id, "ARRIVING ON 12/01."),
        patch(b.vship_id, "2026-12-01T00:00:00"),
    ]
    assert world.calls.named("vship.get_internal_comments") == []   # out of window: no notif lookup
    assert res.summary.ok == 1


async def test_p8_both_notices_sent_goes_to_ben(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P8"](world))
    assert res.plan("EBKG10000008").route is Route.BEN
    assert res.text(OutputFile.BEN) == "EBKG10000008\n"
    assert world.calls.named("qbo.find_invoices_by_doc_numbers") == []
    assert world.writes() == [] and res.summary.routed == 1


async def test_p9_done_in_the_last_week(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P9"](world))
    assert res.failed_text == "[SKIP] #EBKG10000009 - Done in the last week\n"
    assert (res.summary.failed, res.summary.ok) == (1, 0)
    assert world.writes() == []


async def test_p10_notif1_sent_row_done_in_the_last_week_d8(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P10"](world))
    assert res.failed_text == "[SKIP] #EBKG10000010 A - Done in the last week\n"
    assert res.summary.failed == 1


async def test_p11_invoice_missing_in_window(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P11"](world))
    assert res.failed_text == "[SKIP] #EBKG10000011 - not found in QuickBooks\n"
    assert world.calls.named("qbo.find_invoices_by_doc_numbers") == [
        ("qbo.find_invoices_by_doc_numbers", ("EBKG10000011",))]
    assert res.summary.failed == 1


async def test_p12_invoice_missing_out_of_window_d9(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P12"](world))
    assert res.failed_text == "[SKIP] #EBKG10000012 - not found in QuickBooks\n"
    assert res.summary.failed == 1


async def test_p13_below_threshold_goes_to_bindu(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P13"](world))
    assert res.plan("EBKG10000013").route is Route.BINDU
    assert res.path(OutputFile.BINDU).read_bytes() == BOM + b"EBKG10000013,850,ACME\r\n"
    assert world.carriers[Carrier.MSC].get_eta_calls == []
    assert (res.summary.routed, res.summary.failed, res.summary.ok) == (1, 0, 0)


async def test_p14_amount_checked_before_ben(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P14"](world))
    assert res.plan("EBKG10000014 AA").route is Route.BINDU
    assert res.path(OutputFile.BINDU).read_bytes() == BOM + b"EBKG10000014 AA,850,ACME\r\n"
    assert ("vship.get_customer_name", "EBKG10000014") in world.calls
    assert res.text(OutputFile.BEN) is None


async def test_p15_both_sent_goes_to_ben(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P15"](world))
    assert res.text(OutputFile.BEN) == "EBKG10000015 AA\n"
    assert all(not c.get_eta_calls for c in world.carriers.values())


async def test_p16_skip_r(world: FakeWorld, run_pipeline: Runner, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="eta.pipeline"):
        res = await run_pipeline(SCENARIOS["P16"](world))
    plan = res.plan("EBKG10000016 R")
    assert plan.route is Route.SKIPPED and plan.status == "SKIPPED"
    assert res.no_output_files()
    assert (res.summary.skipped, res.summary.failed) == (1, 0)
    assert "Don't need to do this one, ends in R" in caplog.text


async def test_p17_manual_row_is_written_raw(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P17"](world))
    assert res.failed_text == "NAM1000017 C\n"
    assert res.summary.failed == 1


async def test_p18_malformed_row(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P18"](world))
    assert res.failed_text == (
        "[ERROR] Row {'inv_number': 'EBKG10000018', 'amount_due': 'abc'} - Malformed row (missing amount column or "
        "non-numeric amount).\ncould not convert string to float: 'abc'\n\n")
    assert res.summary.failed == 1


async def test_p19_carrier_lookup_failed(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P19"](world))
    assert res.failed_text == "[ERROR] #EBKG10000019 - Carrier Lookup Failed.\nboom\n\n"
    assert res.summary.failed == 1 and world.calls.named("vship.find_booking_id") == []


async def test_p20_cant_determine_eta(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P20"](world))
    assert res.failed_text == "[ERROR] #EBKG10000020 -  Can't determine ETA.\nnone\n\n"
    assert res.summary.failed == 1


async def test_p21_customs_hold(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P21"](world))
    assert res.plan("EBKG10000021").route is Route.CUSTOMS_HOLD
    assert res.text(OutputFile.CUSTOMS) == "[ERROR] #EBKG10000021 -  Possible Customs Hold.\n"
    assert res.failed_text is None
    assert res.summary.failed == 1


async def test_p22_unexpected_carrier_error(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P22"](world))
    assert res.failed_text == "[ERROR] #EBKG10000022 - Unexpected error during carrier lookup.\nx\n\n"
    assert res.summary.failed == 1


async def test_p23_notif_lookup_error_defaults_to_send(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P23"](world))
    plan = res.plan("EBKG10000023")
    error = "vship: No VShip booking matches 'EBKG10000023' (candidates: none)"
    assert plan.route is Route.NOTIFY and plan.notif_defaulted
    assert plan.warnings == [f"Notif lookup failed: {error} — defaulting to False"]
    assert f"#EBKG10000023: Notif lookup failed: {error} — defaulting to False" in res.out
    assert world.writes() == [
        upd("EBKG10000023", date(2026, 11, 3), "EBKG10000023 A"),
        send("EBKG10000023 A", subject("EBKG10000023", ARRIVED_FIRST)),
    ]
    assert res.failed_text == (f"[ERROR] #EBKG10000023 - Note Write Error, QB Invoice Processing Successful\n"
                               f"{error}\n\n")
    assert (res.summary.failed, res.summary.ok) == (1, 0)


async def test_p24_notif_lookup_error_with_fail_setting(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P24"](world), env={"ETA_ON_NOTIF_LOOKUP_ERROR": "fail"})
    assert res.failed_text == (
        "[ERROR] #EBKG10000024 - VShip notification lookup failed, nothing sent\n"
        "vship: No VShip booking matches 'EBKG10000024' (candidates: none)\n\n")
    assert world.writes() == [] and res.summary.failed == 1


async def test_p25_send_failure_reverts_the_doc_number_d11(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P25"](world))
    assert world.writes() == [
        upd("EBKG10000025", date(2026, 11, 3), "EBKG10000025 A"),
        send("EBKG10000025 A", subject("EBKG10000025", ARRIVED_FIRST)),
        upd("EBKG10000025 A", None, "EBKG10000025"),            # D11 revert; no VShip writes
    ]
    assert res.failed_text == (
        "[ERROR] #EBKG10000025 - QB Invoice Processing Error, No Email Sent\n"
        "qbo: send rejected: Business Validation Error (invoice number reverted to EBKG10000025)\n\n")
    assert (res.summary.failed, res.summary.exit_code, res.summary.email_blocked) == (1, 0, False)
    assert [(r["action"], r["ok"]) for r in res.ledger()] == [
        ("qbo_update", True), ("emailed", False), ("qbo_revert_doc_number", True)]


async def test_p26_vship_patch_failure_counts_as_failed_d12(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P26"](world)
    b = rows[0]
    assert isinstance(b, Booking)
    res = await run_pipeline(rows)
    assert [c[0] for c in world.writes()] == ["qbo.update_invoice", "qbo.send_invoice", "vship.post_comment",
                                              "vship.patch_expected_arrival"]
    assert res.failed_text == (
        "[ERROR] #EBKG10000026 - Booking ETA Change Failed, QB Invoice Processing Successful\n"
        f"vship: unexpected HTTP 500 for PATCH /Bookings/{b.vship_id} (HTTP 500)\n\n")
    assert (res.summary.ok, res.summary.failed) == (0, 1)


async def test_p27_invoice_without_bill_email(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P27"](world))
    assert res.failed_text == ("[ERROR] #EBKG10000027 - QB Invoice Processing Error, No Email Sent\n"
                               "Invoice has no customer e-mail address (BillEmail)\n\n")
    assert world.writes() == [] and res.summary.failed == 1


async def test_p28_ledger_same_day_guard_and_force(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P28"](world)
    res = await run_pipeline(rows)
    assert res.failed_text == ("[SKIP] #EBKG10000028 - already e-mailed today at 08:15 (ledger); "
                               "use --force to resend\n")
    assert world.writes() == [] and res.summary.failed == 1
    world.calls.clear()
    forced = await run_pipeline(rows, force=True)
    assert forced.plan("EBKG10000028").route is Route.NOTIFY and forced.plan("EBKG10000028").status == "OK"
    assert [c[0] for c in world.writes()] == ["qbo.update_invoice", "qbo.send_invoice", "vship.post_comment",
                                              "vship.patch_expected_arrival"]


async def test_p29_duplicate_rows_d3(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P29"](world))
    assert res.failed_text == "[SKIP] #EBKG10000029 - duplicate row in input file (line 2)\n"
    assert res.plan("EBKG10000029", 0).status == "OK" and res.plan("EBKG10000029", 1).status == "FAILED"
    assert world.carriers[Carrier.MSC].get_eta_calls == ["EBKG10000029"]
    assert len(world.calls.named("qbo.send_invoice")) == 1
    assert (res.summary.ok, res.summary.failed) == (1, 1)


async def test_p30_dry_run_over_p1_to_p29(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = [row for key in SCENARIOS for row in SCENARIOS[key](world)]
    res = await run_pipeline(rows, dry_run=True)
    assert res.summary.exit_code == 0
    assert world.qbo.write_attempts == [] and world.vship.write_attempts == []
    assert world.writes() == []
    assert res.no_output_files()
    ledger = res.ledger()
    assert len(ledger) == 1 and ledger[0]["run_id"] == "20261105_081500"   # only P28's seed: untouched
    assert world.calls.named("qbo.prepare_for_sending") == [("qbo.prepare_for_sending", True)]
    assert f"Dry run: nothing was changed. Plan saved to {res.summary.report_csv}" in res.out
    report = res.report()
    assert len(report["rows"]) == len(rows) == 30
    assert {r["status"] for r in report["rows"]} == {"PENDING"}
    routes = {(r["doc_number"], r["line_no"]): r["route"] for r in report["rows"]}
    assert routes[("EBKG10000001", 1)] == "NOTIFY" and routes[("EBKG10000004", 4)] == "PAID_UPDATE"
    assert routes[("EBKG10000007", 7)] == "FUTURE_UPDATE" and routes[("EBKG10000005", 5)] == "ACCOUNTING"
    assert routes[("EBKG10000021", 21)] == "CUSTOMS_HOLD" and routes[("EBKG10000029", 30)] == "FAILED"
    assert res.summary.report_csv is not None and res.summary.report_csv.exists()


async def test_p31_circuit_breaker_trips_after_identical_failures(world: FakeWorld, run_pipeline: Runner) -> None:
    msc = world.carriers[Carrier.MSC]
    msc.behaviour = lambda booking: CarrierLookupError("MSC login rejected")
    rows = [world.add(f"EBKG1000003{i}", eta=None) for i in range(1, 7)]
    res = await run_pipeline(rows, env={"ETA_MAX_PARALLEL_BOOKINGS": "1"})
    assert len(msc.get_eta_calls) == 5
    blocks = (res.failed_text or "").split("\n\n")
    assert blocks[:5] == [f"[ERROR] #EBKG1000003{i} - Carrier Lookup Failed.\nMSC login rejected" for i in range(1, 6)]
    assert blocks[5] == ("[ERROR] #EBKG10000036 - Carrier Lookup Failed.\nMSC lookups aborted after 5 identical "
                         "consecutive failures; last error: MSC login rejected")
    assert res.summary.failed == 6
    assert "MSC: 5 booking(s) failed — MSC login rejected" in res.out


async def test_p31_control_distinct_failures_never_trip(world: FakeWorld, run_pipeline: Runner) -> None:
    msc = world.carriers[Carrier.MSC]
    msc.behaviour = lambda booking: CarrierLookupError(f"no record of booking {booking}")
    rows = [world.add(f"EBKG1000003{i}", eta=None) for i in range(1, 7)]
    res = await run_pipeline(rows, env={"ETA_MAX_PARALLEL_BOOKINGS": "1"})
    assert len(msc.get_eta_calls) == 6
    assert "aborted after" not in (res.failed_text or "")
    assert res.summary.failed == 6


async def test_p32_confirmation_declined(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = [*SCENARIOS["P1"](world), *SCENARIOS["P15"](world)]
    res = await run_pipeline(rows, assume_yes=False, answer="n")
    assert res.summary.exit_code == 3
    assert res.prompts == ["Apply 4 change(s) to 1 invoice(s) and send 1 e-mail(s)? [y/N] "]
    assert world.writes() == [] and world.qbo.write_attempts == []
    assert res.no_output_files() and res.ledger() == []
    assert "Aborted: nothing was changed." in res.out
    assert res.report()["report_stage"] == "aborted" and res.report()["exit_code"] == 3


async def test_p33_email_preflight_blocked(world: FakeWorld, run_pipeline: Runner) -> None:
    p1, p4, p7 = SCENARIOS["P1"](world)[0], SCENARIOS["P4"](world)[0], SCENARIOS["P7"](world)[0]
    assert isinstance(p1, Booking) and isinstance(p4, Booking) and isinstance(p7, Booking)
    world.qbo.prepare_error = EmailPathUnavailableError("not verified")
    res = await run_pipeline([p1, p4, p7])
    assert res.failed_text == "[ERROR] #EBKG10000001 - QB Invoice Processing Error, No Email Sent\nnot verified\n\n"
    assert world.row_writes(p1) == []                                  # no QBO update for the NOTIFY row
    assert world.row_writes(p4) == [upd("EBKG10000004", date(2026, 11, 3), None),
                                    patch(p4.vship_id, "2026-11-03T00:00:00")]
    assert world.row_writes(p7) == [upd("EBKG10000007", date(2026, 12, 1), None),
                                    note(p7.vship_id, "ARRIVING ON 12/01."),
                                    patch(p7.vship_id, "2026-12-01T00:00:00")]
    assert (res.summary.exit_code, res.summary.email_blocked) == (4, True)
    assert (res.summary.ok, res.summary.failed) == (2, 1)
    assert "INVOICE E-MAILS CANNOT BE SENT IN THIS RUN:\nnot verified" in res.out


async def test_p34_template_restore_error_mid_run(world: FakeWorld, run_pipeline: Runner) -> None:
    n1 = world.add("EBKG10000041", eta=ARRIVED)
    n2 = world.add("EBKG10000042", eta=ARRIVED)
    paid = world.add("EBKG10000043", eta=ARRIVED, invoice="paid")
    n3 = world.add("EBKG10000044", eta=ARRIVED)
    future = world.add("EBKG10000045", eta=OUT_OF_WINDOW)
    assert n1.invoice_id is not None
    world.qbo.send_errors[n1.invoice_id] = TemplateRestoreError(RESTORE_MSG)
    res = await run_pipeline([n1, n2, paid, n3, future], env={"ETA_MAX_PARALLEL_BOOKINGS": "1"})
    assert world.row_writes(n1) == [
        upd("EBKG10000041", date(2026, 11, 3), "EBKG10000041 A"),
        send("EBKG10000041 A", subject("EBKG10000041", ARRIVED_FIRST)),
        upd("EBKG10000041 A", None, "EBKG10000041"),                # D11 revert attempted
    ]
    assert world.row_writes(n2) == [] and world.row_writes(n3) == []   # no QBO call at all
    assert [c[0] for c in world.row_writes(paid)] == ["qbo.update_invoice", "vship.patch_expected_arrival"]
    assert [c[0] for c in world.row_writes(future)] == ["qbo.update_invoice", "vship.post_comment",
                                                        "vship.patch_expected_arrival"]
    assert res.failed_text == (
        f"[ERROR] #EBKG10000041 - QB Invoice Processing Error, No Email Sent\n{RESTORE_MSG} "
        "(invoice number reverted to EBKG10000041)\n\n"
        f"[ERROR] #EBKG10000042 - QB Invoice Processing Error, No Email Sent\n{RESTORE_MSG}\n\n"
        f"[ERROR] #EBKG10000044 - QB Invoice Processing Error, No Email Sent\n{RESTORE_MSG}\n\n")
    assert (res.summary.exit_code, res.summary.email_blocked) == (4, True)
    assert (res.summary.ok, res.summary.failed) == (2, 3)
    assert res.out.count(RESTORE_MSG) >= 2     # printed prominently, and again with the final summary


async def test_p35_no_email(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = SCENARIOS["P1"](world)
    b = rows[0]
    assert isinstance(b, Booking)
    res = await run_pipeline(rows, no_email=True)
    plan = res.plan("EBKG10000001")
    assert plan.route is Route.NOTIFY and plan.status == "OK"
    assert plan.message == "e-mail pending (manual send, --no-email)"
    assert world.writes() == [upd("EBKG10000001", date(2026, 11, 3), None), patch(b.vship_id, "2026-11-03T00:00:00")]
    assert world.calls.named("qbo.prepare_for_sending") == []
    path = res.settings.run.output_dir / f"ToEmail_{RUN_ID}.csv"
    assert res.summary.to_email_csv == path
    data = path.read_bytes()
    assert data.startswith(BOM)
    assert data[len(BOM):].decode("utf-8").split("\r\n") == [
        "doc_number,invoice_id,customer,to,cc,subject,doc_number_after_sending,vship_note_after_sending",
        f"EBKG10000001,{b.invoice_id},ACME Imports,{DEFAULT_EMAIL},,{SUBJECT_P1},EBKG10000001 A,"
        "ARRIVED ON 11/03. NOTIF #1",
        "",
    ]
    assert res.summary.ok == 1 and res.summary.exit_code == 0


GRAPH_ENV = {"QB_EMAIL_STRATEGY": "graph", "EMAIL_FROM": "accounting@example.com", "EMAIL_GRAPH_TENANT_ID": "t",
             "EMAIL_GRAPH_CLIENT_ID": "c", "EMAIL_GRAPH_CLIENT_SECRET": "s"}


@pytest.mark.parametrize(("case", "env", "no_email"), [
    ("--no-email", {}, True),
    ("no e-mail rows", {}, False),
    ("graph sends", GRAPH_ENV, False),
])
async def test_a_template_left_swapped_by_a_crashed_run_is_repaired_by_every_real_run(
        world: FakeWorld, run_pipeline: Runner, state_dir: Path, case: str, env: dict[str, str],
        no_email: bool) -> None:
    """XCT-03 (10 Phase 3 step 2, 04b §6.2 step 1): the repair happens before the prompt also when this run sends
    nothing through template_swap, whose preflight would otherwise be the only one to look at the recovery file."""
    (state_dir / RECOVERY_FILE_NAME).write_text("{}", encoding="utf-8")
    rows = SCENARIOS["P4"](world) if case == "no e-mail rows" else SCENARIOS["P1"](world)
    res = await run_pipeline(rows, env=env, no_email=no_email)
    assert world.calls.named("qbo.recover_email_template") == [("qbo.recover_email_template",)]
    first_write = next(i for i, c in enumerate(world.calls) if c[0] in WRITE_METHODS)
    assert world.calls.index(("qbo.recover_email_template",)) < first_write       # before the prompt and any write
    assert res.summary.exit_code == 0


async def test_template_swap_sends_leave_the_repair_to_their_preflight(world: FakeWorld, run_pipeline: Runner,
                                                                       state_dir: Path) -> None:
    (state_dir / RECOVERY_FILE_NAME).write_text("{}", encoding="utf-8")
    await run_pipeline(SCENARIOS["P1"](world))
    assert world.calls.named("qbo.recover_email_template") == []
    assert world.calls.named("qbo.prepare_for_sending") == [("qbo.prepare_for_sending", False)]


async def test_no_recovery_file_means_no_repair_and_a_dry_run_only_warns(world: FakeWorld, run_pipeline: Runner,
                                                                         state_dir: Path) -> None:
    await run_pipeline(SCENARIOS["P4"](world))
    assert world.calls.named("qbo.recover_email_template") == []
    (state_dir / RECOVERY_FILE_NAME).write_text("{}", encoding="utf-8")
    world.calls.clear()
    world.qbo.write_attempts.clear()
    dry = await run_pipeline(SCENARIOS["P4"](world), dry_run=True)
    assert world.calls.named("qbo.recover_email_template") == [] and world.qbo.write_attempts == []
    assert "may still be modified by a crashed run" in dry.out


async def test_a_failed_template_repair_is_reported_prominently(world: FakeWorld, run_pipeline: Runner,
                                                                state_dir: Path) -> None:
    (state_dir / RECOVERY_FILE_NAME).write_text("{}", encoding="utf-8")
    world.qbo.recover_error = TemplateRestoreError(RESTORE_MSG)
    res = await run_pipeline(SCENARIOS["P4"](world))
    assert res.out.count(RESTORE_MSG) >= 2                 # before the prompt, and again with the final summary
    assert res.summary.exit_code == 4
    assert res.plan("EBKG10000004").status == "OK"         # the ETA updates are not affected


def unusable_recovery_file(path: Path, problem: str) -> EmailPathUnavailableError:
    """What QboClient.recover_email_template() raises for a recovery file this company cannot use (NEW-03)."""
    error: EmailPathUnavailableError
    if problem == "another company":
        error = EmailPathUnavailableError(f"recovery file belongs to another QuickBooks company: {path}")
    else:
        error = TemplateRestoreError(f"The e-mail template recovery file {path} is not valid JSON. Check the invoice "
                                     "e-mail subject in QuickBooks (Settings → Account and settings → Sales → "
                                     "Messages), then delete the file.")
    error.recovery_file_unusable = True
    return error


@pytest.mark.parametrize("problem", ["another company", "unreadable"])
@pytest.mark.parametrize(("case", "env", "no_email"), [
    ("graph sends", GRAPH_ENV, False),
    ("--no-email", {}, True),
    ("no e-mail rows", {}, False),
])
async def test_a_recovery_file_this_company_cannot_use_only_warns_a_run_without_template_swap_e_mails(
        world: FakeWorld, run_pipeline: Runner, state_dir: Path, caplog: pytest.LogCaptureFixture, case: str,
        env: dict[str, str], no_email: bool, problem: str) -> None:
    """NEW-03: a recovery file of another QuickBooks company (e.g. a sandbox crash before switching to production), or
    one that cannot be read, says nothing about this company's template and does not touch a run that e-mails nothing
    through template_swap: a WARNING names the file and the reason, the run goes on and exits 0 (a template_swap run
    that e-mails still stops its e-mails, see the next test)."""
    path = state_dir / RECOVERY_FILE_NAME
    path.write_text("{}", encoding="utf-8")
    error = unusable_recovery_file(path, problem)
    world.qbo.recover_error = error
    rows = SCENARIOS["P4"](world) if case == "no e-mail rows" else SCENARIOS["P1"](world)
    with caplog.at_level(logging.WARNING, logger="eta.pipeline"):
        res = await run_pipeline(rows, env=env, no_email=no_email)
    assert world.calls.named("qbo.recover_email_template") == [("qbo.recover_email_template",)]
    assert (res.summary.exit_code, res.summary.email_blocked) == (0, False)
    assert "COULD NOT BE RESTORED" not in res.out and "restored by hand" not in res.out
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and r.name == "eta.pipeline"]
    assert any(str(path) in m and str(error) in m for m in warnings), warnings
    assert [p.status for p in res.summary.plans] == ["OK"]


async def test_a_recovery_file_of_another_company_with_the_real_qbo_client_only_warns(
        world: FakeWorld, run_pipeline: Runner, make_settings: Callable[..., Settings], state_dir: Path,
        caplog: pytest.LogCaptureFixture) -> None:
    """NEW-03 end to end: the real QboClient.recover_email_template() marks another company's file (e.g. left by a
    sandbox template_swap run before QB_SANDBOX was switched off), so a graph run with only an ETA update exits 0 and
    the file is left alone."""
    import respx

    path = state_dir / RECOVERY_FILE_NAME
    path.write_text(json.dumps({"realm_id": "999", "original_subject": "Invoice [Invoice No.]",
                                "swapped_subject": SUBJECT_P1}), encoding="utf-8")
    qbo_settings = make_settings(GRAPH_ENV).qbo
    assert qbo_settings is not None
    real = QboClient(qbo_settings, world.qbo.auth, state_dir=state_dir)  # type: ignore[arg-type]
    world.qbo.recover_email_template = real.recover_email_template  # type: ignore[method-assign]
    with respx.mock(assert_all_mocked=True), caplog.at_level(logging.WARNING, logger="eta"):
        res = await run_pipeline(SCENARIOS["P4"](world), env=GRAPH_ENV)
        await real.aclose()
    assert (res.summary.exit_code, res.plan("EBKG10000004").status) == (0, "OK")
    assert path.exists() and "restored by hand" not in res.out
    assert [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert any(f"recovery file belongs to another QuickBooks company: {path}" in r.getMessage()
               for r in caplog.records if r.name == "eta.pipeline")


async def test_a_recovery_file_of_another_company_still_stops_template_swap_e_mails(world: FakeWorld,
                                                                                    run_pipeline: Runner,
                                                                                    state_dir: Path) -> None:
    """NEW-03: a run that e-mails through template_swap keeps the hard failure of its preflight (04b §6.2 step 1)."""
    path = state_dir / RECOVERY_FILE_NAME
    path.write_text("{}", encoding="utf-8")
    world.qbo.prepare_error = unusable_recovery_file(path, "another company")
    res = await run_pipeline(SCENARIOS["P1"](world))
    assert (res.summary.exit_code, res.summary.email_blocked) == (4, True)
    assert world.calls.named("qbo.send_invoice") == [] and world.calls.named("qbo.recover_email_template") == []
    assert f"recovery file belongs to another QuickBooks company: {path}" in (res.failed_text or "")


async def test_p36_ambiguous_doc_number_d21(world: FakeWorld, run_pipeline: Runner) -> None:
    b = world.add("EBKG10000036", eta=ARRIVED, invoice=None)
    world.qbo.ambiguous["EBKG10000036"] = ("101", "102")
    res = await run_pipeline([b])
    assert res.failed_text == (
        "[ERROR] #EBKG10000036 - QB Invoice Processing Error, No Email Sent\n"
        "2 QuickBooks invoices share DocNumber 'EBKG10000036' (Ids 101, 102) — fix in QBO\n\n")
    assert world.writes() == [] and res.summary.failed == 1


async def test_p37_router_memo_one_carrier_call_per_booking(world: FakeWorld, run_pipeline: Runner) -> None:
    a = world.add("NAM1000037", eta=CMA_ETA)
    b = world.add("NAM1000037 A", eta=CMA_ETA, comments=NOTIF1_OLD)
    res = await run_pipeline([a, b])
    assert world.carriers[Carrier.CMA].get_eta_calls == ["NAM1000037"]
    assert res.plan("NAM1000037").eta is not None and res.plan("NAM1000037 A").eta is not None


async def test_two_rows_of_one_booking_after_a_first_notice_send_one_second_notice(world: FakeWorld,
                                                                                    run_pipeline: Runner) -> None:
    """XCT-01: 'X' and 'X A' (two open invoices of one booking, a normal input pair) are both decided from one read of
    the VShip notes. The original handled rows one at a time and re-read the notes, so its second row saw the first
    row's new note: here NOTIF #2 → Ben, not a second e-mail and a second note."""
    a = world.add("EBKG10000050", eta=ARRIVED, comments=NOTIF1_OLD)
    b = world.add("EBKG10000050 A", eta=ARRIVED, comments=NOTIF1_OLD)       # the same VShip booking
    res = await run_pipeline([a, b])
    assert world.calls.named("qbo.send_invoice") == [send("EBKG10000050 A", subject("EBKG10000050", ARRIVED_SECOND))]
    assert world.calls.named("vship.post_comment") == [note(b.vship_id, "ARRIVED ON 11/03. NOTIF #2")]
    assert res.plan("EBKG10000050").route is Route.NOTIFY and res.plan("EBKG10000050").status == "OK"
    assert res.plan("EBKG10000050 A").route is Route.BEN and res.text(OutputFile.BEN) == "EBKG10000050 A\n"
    assert world.row_writes(Booking("", "", None, None, None, b.invoice_id, None)) == []   # 'X A' untouched


async def test_two_rows_of_one_booking_without_notes_send_one_first_notice(world: FakeWorld,
                                                                           run_pipeline: Runner) -> None:
    a = world.add("EBKG10000050", eta=ARRIVED)
    b = world.add("EBKG10000050 A", eta=ARRIVED)
    res = await run_pipeline([a, b])
    assert world.calls.named("qbo.send_invoice") == [send("EBKG10000050 A", subject("EBKG10000050", ARRIVED_FIRST))]
    assert world.calls.named("vship.post_comment") == [note(b.vship_id, "ARRIVED ON 11/03. NOTIF #1")]
    assert res.plan("EBKG10000050 A").status == "FAILED"
    assert res.failed_text == "[SKIP] #EBKG10000050 A - Done in the last week\n"
    assert all(c[1] != "EBKG10000050 A" for c in world.calls.named("qbo.update_invoice"))   # never renamed 'AA'


async def test_two_rows_of_one_booking_with_future_eta_are_both_notified(world: FakeWorld,
                                                                         run_pipeline: Runner) -> None:
    """'ARRIVING ON mm/dd.' notes are not notices (01 §11), so the original e-mailed both rows (O4): unchanged."""
    a = world.add("NAM1000052", eta=CMA_ETA)
    b = world.add("NAM1000052 A", eta=CMA_ETA)
    res = await run_pipeline([a, b])
    assert len(world.calls.named("qbo.send_invoice")) == 2
    assert res.plan("NAM1000052").status == "OK" and res.plan("NAM1000052 A").status == "OK"


def untouched_invoice(world: FakeWorld, booking: Booking) -> bool:
    """No QuickBooks write for this row's own invoice (the rows of one booking share their VShip booking)."""
    return world.calls.writes(booking.invoice_id) == []


NOT_SENT_50 = ("not processed: #EBKG10000050 (same booking) was to give this booking's notice in this run, but its "
               "e-mail was not confirmed as sent - run the file again")


async def test_two_rows_of_one_booking_with_the_e_mail_path_blocked_are_both_reported(world: FakeWorld,
                                                                                     run_pipeline: Runner) -> None:
    """NEW-02: the e-mail preflight fails, so 'X' sends no notice in this run. 'X A' must not be sent to Ben as if 'X'
    had; both rows fail with the e-mail path error."""
    a = world.add("EBKG10000050", eta=ARRIVED, comments=NOTIF1_OLD)
    b = world.add("EBKG10000050 A", eta=ARRIVED, comments=NOTIF1_OLD)
    world.qbo.prepare_error = EmailPathUnavailableError("not verified")
    res = await run_pipeline([a, b])
    assert res.text(OutputFile.BEN) is None
    assert res.failed_text == ("[ERROR] #EBKG10000050 - QB Invoice Processing Error, No Email Sent\nnot verified\n\n"
                               "[ERROR] #EBKG10000050 A - QB Invoice Processing Error, No Email Sent\nnot verified\n\n")
    assert world.writes() == []
    assert (res.summary.exit_code, res.summary.failed, res.summary.routed) == (4, 2, 0)


@pytest.mark.parametrize("comments", [NOTIF1_OLD, []], ids=["second notice", "first notice"])
async def test_two_rows_of_one_booking_when_the_first_e_mail_fails_the_second_is_not_processed(
        world: FakeWorld, run_pipeline: Runner, comments: list[dict[str, Any]]) -> None:
    """NEW-02: 'X' fails its send at apply (D11 revert, no note), so the booking got no notice in this run. 'X A' is
    neither sent to Ben nor skipped as 'Done in the last week'; it is reported as not processed, and it is not
    e-mailed either (never two notices for one booking in one run, and no e-mail that was not in the plan)."""
    a = world.add("EBKG10000050", eta=ARRIVED, comments=comments)
    b = world.add("EBKG10000050 A", eta=ARRIVED, comments=comments)
    assert a.invoice_id is not None
    world.qbo.send_errors[a.invoice_id] = InvoiceSendError("qbo", "send rejected: Business Validation Error")
    res = await run_pipeline([a, b])
    assert len(world.calls.named("qbo.send_invoice")) == 1 and untouched_invoice(world, b)
    assert world.calls.named("vship.post_comment") == []
    assert res.text(OutputFile.BEN) is None
    assert res.failed_text == (
        "[ERROR] #EBKG10000050 - QB Invoice Processing Error, No Email Sent\n"
        "qbo: send rejected: Business Validation Error (invoice number reverted to EBKG10000050)\n\n"
        f"[ERROR] #EBKG10000050 A - QB Invoice Processing Error, No Email Sent\n{NOT_SENT_50}\n\n")
    later = res.plan("EBKG10000050 A")
    assert (later.route, later.status) == (Route.FAILED, "FAILED")
    assert (res.summary.ok, res.summary.failed, res.summary.routed) == (0, 2, 0)
    assert "EBKG10000050 A" in res.out.split("Failed while applying:")[1]


async def test_two_rows_of_one_booking_hold_the_second_row_until_the_first_e_mail_went_out(
        world: FakeWorld, run_pipeline: Runner) -> None:
    """NEW-02: the plan says why 'X A' goes to Ben, and its Send2Ben.txt line is written only after the e-mail of 'X'
    went out."""
    a = world.add("EBKG10000050", eta=ARRIVED, comments=NOTIF1_OLD)
    b = world.add("EBKG10000050 A", eta=ARRIVED, comments=NOTIF1_OLD)
    ben_before_send: list[bool] = []

    async def hold(name: str, target: Any) -> None:
        if name == "qbo.send_invoice":
            ben_before_send.append((output_dir / OutputFile.BEN.value).exists())

    world.qbo.before_write = hold
    dry = await run_pipeline([a, b], dry_run=True)
    output_dir = dry.settings.run.output_dir
    warning = ("same booking as #EBKG10000050, which gives this booking's notice in this run: goes to Ben once that "
               "e-mail is sent, else it is not processed")
    assert dry.plan("EBKG10000050 A").route is Route.BEN and warning in dry.plan("EBKG10000050 A").warnings
    assert f"#EBKG10000050 A: {warning}" in dry.out
    res = await run_pipeline([a, b])
    assert ben_before_send == [False]
    assert res.text(OutputFile.BEN) == "EBKG10000050 A\n" and res.plan("EBKG10000050 A").status == "ROUTED"


async def test_two_rows_of_one_booking_after_an_e_mail_with_an_unknown_outcome(world: FakeWorld,
                                                                               run_pipeline: Runner) -> None:
    """NEW-02: the e-mail of 'X' may or may not have gone out (04b §7). 'X A' is not e-mailed (a second notice is
    possible) and not sent to Ben (the first may be missing): it is reported as not processed."""
    a = world.add("EBKG10000050", eta=ARRIVED, comments=NOTIF1_OLD)
    b = world.add("EBKG10000050 A", eta=ARRIVED, comments=NOTIF1_OLD)
    assert a.invoice_id is not None
    error = InvoiceSendError("qbo", "invoice e-mail outcome unknown (timeout); QuickBooks shows no delivery yet")
    error.outcome_unknown = True
    world.qbo.send_errors[a.invoice_id] = error
    res = await run_pipeline([a, b])
    assert len(world.calls.named("qbo.send_invoice")) == 1 and untouched_invoice(world, b)
    assert res.text(OutputFile.BEN) is None
    assert res.plan("EBKG10000050 A").message == (
        f"[ERROR] #EBKG10000050 A - QB Invoice Processing Error, No Email Sent\n{NOT_SENT_50}\n")


@pytest.mark.parametrize("method", ["qbo.update_invoice", "qbo.send_invoice"])
async def test_two_rows_of_one_booking_interrupted_while_the_first_one_gives_the_notice(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock,
        method: str) -> None:
    """NEW-02: Ctrl+C while 'X' makes its QBO update: its notice does not go out (the suffix is reverted), so 'X A' is
    INTERRUPTED like the rest of the run instead of being sent to Ben. Ctrl+C while the e-mail of 'X' is being sent:
    the e-mail goes out and gets its note (RUL-01), so 'X A' goes to Ben."""
    a = world.add("EBKG10000050", eta=ARRIVED, comments=NOTIF1_OLD)
    b = world.add("EBKG10000050 A", eta=ARRIVED, comments=NOTIF1_OLD)
    settings = make_settings()
    report = await run_and_interrupt(world, settings, tmp_path, fixed_clock, [a, b], method)
    assert untouched_invoice(world, b) and not (settings.run.output_dir / OutputFile.FAILED.value).exists()
    by_doc = {r["doc_number"]: r for r in report["rows"]}
    ben = settings.run.output_dir / OutputFile.BEN.value
    if method == "qbo.update_invoice":
        assert world.calls.named("qbo.send_invoice") == [] and not ben.exists()
        assert by_doc["EBKG10000050 A"]["status"] == "INTERRUPTED"
        assert by_doc["EBKG10000050 A"]["message"] == "interrupted before #EBKG10000050 (same booking) gave the notice"
    else:
        assert world.calls.named("vship.post_comment") == [note(b.vship_id, "ARRIVED ON 11/03. NOTIF #2")]
        assert ben.read_text(encoding="utf-8") == "EBKG10000050 A\n"
        assert by_doc["EBKG10000050 A"]["status"] == "ROUTED"


@pytest.mark.parametrize("first_update_fails", [False, True], ids=["listed", "not listed"])
async def test_two_rows_of_one_booking_with_no_email(world: FakeWorld, run_pipeline: Runner,
                                                     first_update_fails: bool) -> None:
    """NEW-02 with --no-email: the notice of 'X' is the e-mail a person sends from ToEmail_<run>.csv, listed when its
    QBO update succeeds. Then 'X A' goes to Ben (one notice per booking); when 'X' is not listed, 'X A' is not
    processed."""
    a = world.add("EBKG10000050", eta=ARRIVED, comments=NOTIF1_OLD)
    b = world.add("EBKG10000050 A", eta=ARRIVED, comments=NOTIF1_OLD)
    assert a.invoice_id is not None
    if first_update_fails:
        world.qbo.update_errors[a.invoice_id] = InvoiceUpdateError("qbo", "stale object")
    res = await run_pipeline([a, b], no_email=True)
    assert untouched_invoice(world, b)
    to_email = res.settings.run.output_dir / f"ToEmail_{RUN_ID}.csv"
    if first_update_fails:
        assert not to_email.exists() and res.text(OutputFile.BEN) is None
        assert res.plan("EBKG10000050 A").message.endswith(f"\n{NOT_SENT_50}\n")
    else:
        listed = list(csv.reader(io.StringIO(to_email.read_text(encoding="utf-8-sig"))))
        assert [cells[0] for cells in listed[1:]] == ["EBKG10000050"]                  # one notice for the booking
        assert res.text(OutputFile.BEN) == "EBKG10000050 A\n" and res.plan("EBKG10000050 A").status == "ROUTED"


async def test_p38_run_lock(world: FakeWorld, run_pipeline: Runner, state_dir: Path) -> None:
    rows = SCENARIOS["P1"](world)
    with run_lock(state_dir):
        res = await run_pipeline(rows)
        assert res.summary.exit_code == 2
        assert "Another ETAutomationsAPI run is in progress" in res.summary.message
        assert world.qbo.auth.calls == 0 and world.calls == []
        dry = await run_pipeline(rows, dry_run=True)      # a dry run takes no lock
        assert dry.summary.exit_code == 0 and dry.plan("EBKG10000001").route is Route.NOTIFY


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Phase 0 / Phase 1
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_qbo_auth_failure_exits_2(world: FakeWorld, run_pipeline: Runner,
                                       caplog: pytest.LogCaptureFixture) -> None:
    world.qbo.auth.error = AuthError("QuickBooks rejected the refresh token (invalid_grant).")
    res = await run_pipeline(SCENARIOS["P1"](world))
    assert res.summary.exit_code == 2
    assert "QuickBooks authentication failed" in res.summary.message and "eta qbo-auth" in res.summary.message
    assert world.vship.auth.calls == 0 and world.carriers[Carrier.MSC].get_eta_calls == []
    assert "eta qbo-auth" in caplog.text


async def test_vship_auth_failure_exits_2(world: FakeWorld, run_pipeline: Runner) -> None:
    world.vship.auth.error = AuthError("VShip rejected the login (HTTP 401)")
    res = await run_pipeline(SCENARIOS["P1"](world))
    assert res.summary.exit_code == 2 and "VShip authentication failed" in res.summary.message
    assert "VSHIP_USER_NAME" in res.summary.message


async def test_unreachable_service_at_login_exits_2_with_a_network_hint(world: FakeWorld,
                                                                         run_pipeline: Runner) -> None:
    """Phase 0 step 6: a network error is not a credential problem, so the hint names the connection."""
    world.vship.auth.error = ServiceError("vship", "network error: ConnectError()")
    res = await run_pipeline(SCENARIOS["P1"](world))
    assert res.summary.exit_code == 2
    assert res.summary.message == (
        "VShip authentication failed: vship: network error: ConnectError()\n"
        "VShip could not be reached: check the network connection and the proxy settings (HTTPS_PROXY), then "
        "retry (`eta probe vship` shows details).")
    assert world.writes() == [] and world.carriers[Carrier.MSC].get_eta_calls == []

    world.vship.auth.error = None
    world.qbo.auth.error = ServiceError("qbo-oauth", "unexpected HTTP 503 for POST /oauth2/v1/tokens/bearer",
                                        status=503)
    res = await run_pipeline(SCENARIOS["P1"](world))
    assert res.summary.exit_code == 2
    assert res.summary.message.endswith(
        "QuickBooks answered HTTP 503 to the login: retry later (`eta probe qbo` shows details).")
    assert "eta qbo-auth" not in res.summary.message

    world.qbo.auth.error = ServiceError("qbo-oauth", "unexpected HTTP 401 for POST /oauth2/v1/tokens/bearer",
                                        status=401)
    res = await run_pipeline(SCENARIOS["P1"](world))
    assert res.summary.exit_code == 2 and "Run `eta qbo-auth`" in res.summary.message   # a credential verdict


async def test_missing_required_settings_exit_2(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P1"](world), env={"QB_CLIENT_ID": None, "VSHIP_PASSWORD": None})  # type: ignore[dict-item]
    assert res.summary.exit_code == 2
    assert "QB_CLIENT_ID" in res.summary.message and "VSHIP_PASSWORD" in res.summary.message
    assert world.qbo.auth.calls == 0


async def test_invalid_input_path_exit_2(world: FakeWorld, run_pipeline: Runner, tmp_path: Path) -> None:
    txt = tmp_path / "input.txt"
    txt.write_text("EBKG1,5000\n", encoding="utf-8")
    res = await run_pipeline([], input_path=txt)
    assert res.summary.exit_code == 2 and "must be a .csv file" in res.summary.message
    missing = await run_pipeline([], input_path=tmp_path / "nope.CSV")
    assert missing.summary.exit_code == 2 and "not found" in missing.summary.message


async def test_header_only_file_is_empty(world: FakeWorld, run_pipeline: Runner,
                                         caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="eta.pipeline"):
        res = await run_pipeline(["Num,Amount", ""])
    assert res.summary.exit_code == 0
    assert "Invoice list is empty - nothing to do." in res.out
    assert res.no_output_files()                               # D2: the header is not reported as malformed
    assert "Loaded 0 booking(s) from file." in caplog.text


async def test_header_row_skipped_and_rows_loaded(world: FakeWorld, run_pipeline: Runner,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level("INFO", logger="eta.pipeline"):
        res = await run_pipeline(["Num,Amount", *SCENARIOS["P15"](world)])
    assert res.text(OutputFile.BEN) == "EBKG10000015 AA\n" and res.failed_text is None
    assert "Loaded 1 booking(s) from file." in caplog.text


async def test_only_and_limit(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = [*SCENARIOS["P15"](world), *SCENARIOS["P17"](world), *SCENARIOS["P2"](world), *SCENARIOS["P16"](world)]
    res = await run_pipeline(rows, only={"nam1000017 c", "EBKG10000002", "EBKG10000016 R"}, limit=2)
    # --only matches the DocNumber or the base booking, case-insensitively; --limit keeps the first 2 of those
    assert [p.doc_number for p in res.summary.plans] == ["NAM1000017 C", "EBKG10000002 A"]
    assert res.failed_text == "NAM1000017 C\n" and res.text(OutputFile.BEN) is None
    assert res.summary.ok == 1


async def test_read_input_bom_blank_lines_and_encoding(tmp_path: Path) -> None:
    path = tmp_path / "in.CSV"
    path.write_bytes(BOM + b"EBKG1,\"2,450.00\"\r\n\r\nNAM1000017 C,5000\r\n")
    rows = read_input(path)
    assert [(r.line_no, r.doc_number, r.amount_raw) for r in rows] == [(1, "EBKG1", "2,450.00"),
                                                                       (2, "NAM1000017 C", "5000")]
    bad = tmp_path / "latin.csv"
    bad.write_bytes("Soci\xe9t\xe9,5000\n".encode("latin-1"))
    with pytest.raises(ConfigError, match="not UTF-8"):
        read_input(bad)


# ── MSC token pre-flight (06 §5.1, 10 Phase 1) ──
@pytest.fixture
def msc_world(fixed_clock: FixedClock) -> FakeWorld:
    return FakeWorld(fixed_clock, msc=FakeMscCarrier(seconds_left=None))


async def test_msc_preflight_without_tty_fails_fast(msc_world: FakeWorld, make_settings: Callable[..., Settings],
                                                    tmp_path: Path, fixed_clock: FixedClock) -> None:
    b = msc_world.add("EBKG10000001", eta=ARRIVED)
    settings = make_settings()
    summary = await run(settings, write_input(tmp_path, [b]), assume_yes=True, clock=fixed_clock,
                        services=msc_world.services(settings), is_tty=lambda: False, out=io.StringIO())
    assert summary.exit_code == 2 and summary.message == NO_TOKEN_MESSAGE
    assert msc_world.carriers[Carrier.MSC].get_eta_calls == [] and msc_world.writes() == []


async def test_msc_preflight_prompts_on_a_tty(msc_world: FakeWorld, make_settings: Callable[..., Settings],
                                              tmp_path: Path, fixed_clock: FixedClock) -> None:
    b = msc_world.add("EBKG10000001", eta=ARRIVED)
    settings = make_settings()
    prompts: list[str] = []
    out = io.StringIO()

    def getpass_fn(prompt: str) -> str:
        prompts.append(prompt)
        return "  eyJhbGciOiJIUzI1NiJ9.e30.c2ln  "

    summary = await run(settings, write_input(tmp_path, [b]), assume_yes=True, clock=fixed_clock,
                        services=msc_world.services(settings), is_tty=lambda: True, getpass_fn=getpass_fn, out=out)
    msc = msc_world.carriers[Carrier.MSC]
    assert isinstance(msc, FakeMscCarrier)
    assert prompts == ["Paste a fresh MSC access token (input hidden): "]
    assert msc.stored_tokens == ["eyJhbGciOiJIUzI1NiJ9.e30.c2ln"]
    assert "no MSC access token is stored" in out.getvalue()
    assert summary.exit_code == 0 and summary.ok == 1


async def test_msc_preflight_skipped_when_token_fresh_or_api_mode_or_no_msc_rows(
        fixed_clock: FixedClock, make_settings: Callable[..., Settings], tmp_path: Path) -> None:
    for msc, rows_for in (
        (FakeMscCarrier(seconds_left=3600), lambda w: [w.add("EBKG10000001", eta=ARRIVED)]),
        (FakeMscCarrier(mode="api"), lambda w: [w.add("EBKG10000001", eta=ARRIVED)]),
        (FakeMscCarrier(seconds_left=None), lambda w: [w.add("NAM1000003", eta=CMA_ETA)]),
        (FakeMscCarrier(seconds_left=None), lambda w: [w.add("EBKG10000013", "850", invoice=None)]),
    ):
        w = FakeWorld(fixed_clock, msc=msc)
        rows = rows_for(w)
        settings = make_settings()
        summary = await run(settings, write_input(tmp_path, rows), assume_yes=True, clock=fixed_clock,
                            services=w.services(settings), is_tty=lambda: False, out=io.StringIO())
        assert summary.exit_code == 0, (msc.mode, msc.seconds_left)
        assert msc.stored_tokens == []


async def test_msc_preflight_token_still_too_short(msc_world: FakeWorld, make_settings: Callable[..., Settings],
                                                   tmp_path: Path, fixed_clock: FixedClock) -> None:
    msc = msc_world.carriers[Carrier.MSC]
    assert isinstance(msc, FakeMscCarrier)
    msc.seconds_left = 120
    msc.set_manual_token = lambda token: {"mode": "mymsc", "has_token": True, "seconds_left": 30}  # type: ignore[method-assign]
    b = msc_world.add("EBKG10000001", eta=ARRIVED)
    settings = make_settings()
    out = io.StringIO()
    summary = await run(settings, write_input(tmp_path, [b]), assume_yes=True, clock=fixed_clock,
                        services=msc_world.services(settings), is_tty=lambda: True,
                        getpass_fn=lambda prompt: "eyJ.x.y", out=out)
    assert summary.exit_code == 2 and NO_TOKEN_MESSAGE in summary.message
    assert "120 s left" in out.getvalue()


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Phase 2 / Phase 3 corner cases
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_router_prepare_gets_the_bases_needing_an_eta(world: FakeWorld, make_settings: Callable[..., Settings],
                                                            tmp_path: Path, fixed_clock: FixedClock) -> None:
    rows = [*SCENARIOS["P1"](world), *SCENARIOS["P2"](world), *SCENARIOS["P13"](world), *SCENARIOS["P15"](world),
            *SCENARIOS["P29"](world)]
    settings = make_settings()
    services = world.services(settings)
    prepared: list[list[str]] = []

    class SpyRouter(CarrierRouter):
        async def prepare(self, bases: Iterable[str]) -> None:
            prepared.append(list(bases))

    services.router = SpyRouter(providers=dict(world.make_router().providers), native=dict(world.carriers))
    await run(settings, write_input(tmp_path, rows), assume_yes=True, clock=fixed_clock, services=services,
              out=io.StringIO())
    assert prepared == [["EBKG10000001", "EBKG10000002", "EBKG10000029"]]


async def test_bindu_customer_name_failure_writes_empty_name_d14(world: FakeWorld, run_pipeline: Runner,
                                                                 caplog: pytest.LogCaptureFixture) -> None:
    rows = SCENARIOS["P13"](world)
    world.vship.customer_errors["EBKG10000013"] = ServiceError("vship", "unexpected HTTP 500", status=500)
    with caplog.at_level("WARNING", logger="eta.pipeline"):
        res = await run_pipeline(rows)
    assert res.path(OutputFile.BINDU).read_bytes() == BOM + b"EBKG10000013,850,\r\n"
    assert "customer name lookup failed" in caplog.text
    assert res.summary.routed == 1 and res.summary.exit_code == 0


async def test_qbo_lookup_failure_fails_every_pending_row(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = [*SCENARIOS["P1"](world), *SCENARIOS["P7"](world), *SCENARIOS["P15"](world)]
    world.qbo.lookup_error = ServiceError("qbo", "network error: ReadTimeout()")
    res = await run_pipeline(rows)
    assert res.failed_text == (
        "[ERROR] #EBKG10000001 - QB Invoice Processing Error, No Email Sent\nqbo: network error: ReadTimeout()\n\n"
        "[ERROR] #EBKG10000007 - QB Invoice Processing Error, No Email Sent\nqbo: network error: ReadTimeout()\n\n")
    assert res.text(OutputFile.BEN) == "EBKG10000015 AA\n"
    assert world.writes() == [] and res.summary.failed == 2


async def test_new_doc_number_too_long_guard(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P1"](world), env={"QB_DOCNUMBER_MAX_LEN": "13"})
    assert res.failed_text == (
        "[ERROR] #EBKG10000001 - QB Invoice Processing Error, No Email Sent\n"
        "New invoice number 'EBKG10000001 A' is 14 characters; QuickBooks allows at most 13 (QB_DOCNUMBER_MAX_LEN)\n\n")
    assert world.writes() == []


async def test_partially_paid_without_vship_comments_fails_the_tr_swb_lookup(world: FakeWorld,
                                                                             run_pipeline: Runner) -> None:
    b = world.add("EBKG10000061", eta=ARRIVED, invoice="partial", vship=False)
    res = await run_pipeline([b])
    assert res.failed_text == (
        "[ERROR] #EBKG10000061 - QB Invoice Processing Error, No Email Sent\n"
        "vship: No VShip booking matches 'EBKG10000061' (candidates: none)\n\n")
    assert world.writes() == []


async def test_comments_read_error_with_send_default(world: FakeWorld, run_pipeline: Runner) -> None:
    b = world.add("EBKG10000062", eta=ARRIVED)
    assert b.vship_id is not None
    world.vship.comment_errors[b.vship_id] = ServiceError("vship", "unexpected HTTP 502", status=502)
    res = await run_pipeline([b])
    plan = res.plan("EBKG10000062")
    assert plan.notif_defaulted and plan.route is Route.NOTIFY and plan.status == "OK"
    assert [c[0] for c in world.writes()] == ["qbo.update_invoice", "qbo.send_invoice", "vship.post_comment",
                                              "vship.patch_expected_arrival"]


async def test_paid_update_without_vship_booking_is_a_note_write_error(world: FakeWorld, run_pipeline: Runner) -> None:
    b = world.add("EBKG10000063", eta=ARRIVED, invoice="paid", vship=False)
    res = await run_pipeline([b])
    assert world.writes() == [upd("EBKG10000063", date(2026, 11, 3), None)]
    assert res.failed_text == (
        "[ERROR] #EBKG10000063 - Note Write Error, QB Invoice Processing Successful\n"
        "vship: No VShip booking matches 'EBKG10000063' (candidates: none)\n\n")


async def test_qbo_update_failure_stops_the_row(world: FakeWorld, run_pipeline: Runner) -> None:
    b = world.add("EBKG10000064", eta=OUT_OF_WINDOW)
    assert b.invoice_id is not None
    world.qbo.update_errors[b.invoice_id] = InvoiceUpdateError("qbo", "update rejected: 6000 Business Validation Error")
    res = await run_pipeline([b])
    assert world.writes() == [upd("EBKG10000064", date(2026, 12, 1), None)]
    assert res.failed_text == ("[ERROR] #EBKG10000064 - QB Invoice Processing Error, No Email Sent\n"
                               "qbo: update rejected: 6000 Business Validation Error\n\n")


async def test_send_with_unknown_outcome_blocks_a_same_day_rerun(world: FakeWorld, run_pipeline: Runner) -> None:
    """RUL-02 (04b §7): a send that may have reached the provider (timeout, 5xx, lost answer) is reverted (D11) and
    reported like any failed send, but the ledger counts it as possibly e-mailed: a re-run the same day does not
    e-mail the customer again without --force."""
    b = world.add("EBKG10000001", eta=ARRIVED)
    assert b.invoice_id is not None
    unknown = InvoiceSendError("qbo", "invoice e-mail outcome unknown (ReadTimeout); QuickBooks shows no delivery "
                                      "yet — check the invoice before re-sending.")
    unknown.outcome_unknown = True
    world.qbo.send_errors[b.invoice_id] = unknown
    first = await run_pipeline([b])
    assert world.writes() == [upd("EBKG10000001", date(2026, 11, 3), "EBKG10000001 A"),
                              send("EBKG10000001 A", SUBJECT_P1),
                              upd("EBKG10000001 A", None, "EBKG10000001")]            # D11 revert kept
    assert first.failed_text == (
        "[ERROR] #EBKG10000001 - QB Invoice Processing Error, No Email Sent\n"
        f"{unknown} (invoice number reverted to EBKG10000001)\n\n")
    emailed = [r for r in first.ledger() if r["action"] == "emailed"]
    assert len(emailed) == 1 and emailed[0]["ok"] is False and emailed[0]["detail"]["outcome_unknown"] is True
    world.qbo.send_errors.clear()
    world.calls.clear()
    (first.settings.run.output_dir / OutputFile.FAILED.value).unlink()
    second = await run_pipeline([b])
    assert world.calls.named("qbo.send_invoice") == [] and world.writes() == []
    assert second.plan("EBKG10000001").status == "FAILED"
    assert second.failed_text == ("[SKIP] #EBKG10000001 - already e-mailed today at 09:30 (ledger); "
                                  "use --force to resend\n")
    forced = await run_pipeline([b], force=True)                         # --force overrides, as for any e-mail
    assert forced.plan("EBKG10000001").status == "OK" and len(world.calls.named("qbo.send_invoice")) == 1


async def test_failed_revert_is_reported_d11(world: FakeWorld, run_pipeline: Runner) -> None:
    b = world.add("EBKG10000065", eta=ARRIVED)
    assert b.invoice_id is not None
    world.qbo.send_errors[b.invoice_id] = InvoiceSendError("qbo", "invoice e-mail not sent")
    world.qbo.revert_errors[b.invoice_id] = InvoiceUpdateError("qbo", "stale object twice")
    res = await run_pipeline([b])
    assert res.failed_text == (
        "[ERROR] #EBKG10000065 - QB Invoice Processing Error, No Email Sent\n"
        "qbo: invoice e-mail not sent (could not revert invoice number: qbo: stale object twice)\n\n")
    assert [(r["action"], r["ok"]) for r in res.ledger()][-1] == ("qbo_revert_doc_number", False)


async def next_day_run(settings: Settings, tmp_path: Path, doc: str,
                       comments: Iterable[Mapping[str, Any]]) -> tuple[FakeWorld, RunSummary]:
    """The next morning's run (2026-11-06 09:30) on what QuickBooks and VShip show after a run on 2026-11-05."""
    day2 = FixedClock(datetime(2026, 11, 6, 9, 30))
    world2 = FakeWorld(day2)
    row = world2.add(doc, eta=ARRIVED, comments=comments)
    summary = await run(settings, write_input(tmp_path, [row], "day2.csv"), assume_yes=True, clock=day2,
                        services=world2.services(settings), out=io.StringIO())
    return world2, summary


async def test_restore_error_after_a_successful_send_keeps_the_suffix(world: FakeWorld, run_pipeline: Runner,
                                                                      tmp_path: Path) -> None:
    """RUL-03: the e-mail went out and only the template restore failed (04b §6.2 step 3.5). The send counts as done:
    the suffix stays, the ledger records the e-mail, and the row still writes its VShip note and ETA (01 §6.4 steps
    5-6), so the 7-day rule (01 §11) holds the next day. The other e-mails of the run are blocked (04b §6.1)."""
    b = world.add("EBKG10000066", eta=ARRIVED)
    later = world.add("EBKG10000067", eta=ARRIVED)
    assert b.invoice_id is not None
    error = TemplateRestoreError(f"{RESTORE_MSG} (The invoice e-mail for #EBKG10000066 A WAS sent before the restore "
                                 "failed.)")
    error.receipt = SendReceipt(strategy="template_swap", invoice_id=b.invoice_id,  # type: ignore[attr-defined]
                                doc_number="EBKG10000066 A", subject="s", to=(DEFAULT_EMAIL,),
                                sent_at=datetime(2026, 11, 5, 9, 31))
    world.qbo.send_errors[b.invoice_id] = error
    res = await run_pipeline([b, later], env={"ETA_MAX_PARALLEL_BOOKINGS": "1"})
    assert world.row_writes(b) == [upd("EBKG10000066", date(2026, 11, 3), "EBKG10000066 A"),
                                   send("EBKG10000066 A", subject("EBKG10000066", ARRIVED_FIRST)),   # no revert
                                   note(b.vship_id, "ARRIVED ON 11/03. NOTIF #1"),
                                   patch(b.vship_id, "2026-11-03T00:00:00")]
    assert world.row_writes(later) == []
    emailed = [r for r in res.ledger() if r["action"] == "emailed"]
    assert emailed and emailed[0]["ok"] is True and emailed[0]["detail"]["sent_at"] == "2026-11-05T09:31:00"
    plan = res.plan("EBKG10000066")
    assert plan.status == "OK" and any("could not be restored" in w for w in plan.warnings)
    assert "EBKG10000066 - QB Invoice Processing Error" not in (res.failed_text or "")
    assert (res.failed_text or "").startswith("[ERROR] #EBKG10000067 - QB Invoice Processing Error, No Email Sent\n")
    assert (res.summary.exit_code, res.summary.ok, res.summary.failed) == (4, 1, 1)
    assert res.out.count(RESTORE_MSG) >= 2     # the banner, and again with the final summary
    assert b.vship_id is not None
    world2, day2 = await next_day_run(res.settings, tmp_path, "EBKG10000066 A", world.vship.comments[b.vship_id])
    assert day2.plans[0].message == "[SKIP] #EBKG10000066 A - Done in the last week"
    assert world2.calls.named("qbo.send_invoice") == []


async def test_send_while_blocked_reverts_rows_already_running(world: FakeWorld, run_pipeline: Runner) -> None:
    """With rows in parallel, a row that already passed its QBO update when the e-mail path broke does not send:
    it gets the stored error and its DocNumber is reverted (D11)."""
    a = world.add("EBKG10000071", eta=ARRIVED)
    b = world.add("EBKG10000072", eta=ARRIVED)
    assert a.invoice_id is not None
    world.qbo.send_errors[a.invoice_id] = EmailPathUnavailableError("mailbox rejected the credentials")
    entered = asyncio.Event()

    async def hold(method: str, target: Any) -> None:
        if method == "qbo.send_invoice" and getattr(target, "id", None) == a.invoice_id:
            await entered.wait()            # a's send fails only after b has done its QBO update
        if method == "qbo.update_invoice" and getattr(target, "id", None) == b.invoice_id:
            entered.set()

    world.qbo.before_write = hold
    res = await run_pipeline([a, b])
    assert world.row_writes(b) == [upd("EBKG10000072", date(2026, 11, 3), "EBKG10000072 A"),
                                   upd("EBKG10000072 A", None, "EBKG10000072")]
    assert "mailbox rejected the credentials (invoice number reverted to EBKG10000072)" in (res.failed_text or "")
    assert res.summary.exit_code == 4


async def test_non_tty_without_yes_aborts(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P1"](world), assume_yes=False, tty=False)
    assert res.summary.exit_code == 3 and res.prompts == []
    assert world.writes() == [] and res.no_output_files()
    assert "--yes" in res.out


async def test_confirmation_yes_applies(world: FakeWorld, run_pipeline: Runner) -> None:
    res = await run_pipeline(SCENARIOS["P1"](world), assume_yes=False, answer=" YES ")
    assert res.summary.exit_code == 0 and len(world.writes()) == 4


async def test_no_actions_needs_no_confirmation_and_writes_the_entries(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = [*SCENARIOS["P15"](world), *SCENARIOS["P17"](world)]
    res = await run_pipeline(rows, assume_yes=False, tty=False)
    assert res.summary.exit_code == 0 and res.prompts == []
    assert res.text(OutputFile.BEN) == "EBKG10000015 AA\n" and res.failed_text == "NAM1000017 C\n"
    assert "Done: 0 succeeded, 1 failed/skipped." in res.out


async def test_routed_and_failed_entries_are_written_in_input_order(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = [*SCENARIOS["P22"](world), *SCENARIOS["P17"](world), *SCENARIOS["P19"](world), *SCENARIOS["P9"](world),
            *SCENARIOS["P18"](world)]
    res = await run_pipeline(rows)
    text = res.failed_text or ""
    order = [text.index(doc) for doc in ("EBKG10000022", "NAM1000017 C", "EBKG10000019", "EBKG10000009",
                                         "EBKG10000018")]
    assert order == sorted(order)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Report and console summary (10 §7, §8, Phase 5)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_report_rows_and_metadata(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = [*SCENARIOS["P1"](world), *SCENARIOS["P15"](world)]
    b = rows[0]
    assert isinstance(b, Booking)
    res = await run_pipeline(rows)
    csv_path = res.summary.report_csv
    assert csv_path == res.settings.run.output_dir / "reports" / f"run_{RUN_ID}.csv"
    import csv
    parsed = list(csv.reader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
    assert parsed[0] == list(REPORT_COLUMNS)
    row = dict(zip(REPORT_COLUMNS, parsed[1], strict=True))
    assert row == {
        "line_no": "1", "doc_number": "EBKG10000001", "amount": "2450.00", "kind": "STANDARD",
        "base_booking": "EBKG10000001", "carrier": "MSC", "eta": "2026-11-03T00:00:00", "eta_source": "fake MSC event",
        "eta_is_actual": "False", "eta_rescue": "False", "cutoff": "2026-11-16T09:30:00", "in_window": "True",
        "notif_count": "0", "notif_defaulted": "False", "invoice_id": b.invoice_id, "balance": "2450.00",
        "total": "2450.00", "bill_email": DEFAULT_EMAIL, "route": "NOTIFY", "new_doc_number": "EBKG10000001 A",
        "subject": SUBJECT_P1, "vship_booking_id": b.vship_id,
        "actions_planned": ("QBO update ETA=2026-11-03, DocNumber -> EBKG10000001 A; QBO e-mail; "
                            "VShip note 'ARRIVED ON 11/03. NOTIF #1'; VShip ETA 2026-11-03T00:00:00"),
        "actions_done": ("QBO update ETA=2026-11-03, DocNumber -> EBKG10000001 A; QBO e-mail; "
                         "VShip note 'ARRIVED ON 11/03. NOTIF #1'; VShip ETA 2026-11-03T00:00:00"),
        "status": "OK", "message": "", "warnings": "",
    }
    ben = dict(zip(REPORT_COLUMNS, parsed[2], strict=True))
    assert ben["route"] == "BEN" and ben["status"] == "ROUTED" and ben["message"] == "EBKG10000015 AA"
    report = res.report()
    assert report["run_id"] == RUN_ID and report["report_stage"] == "final" and report["exit_code"] == 0
    assert report["counts"]["by_route"]["NOTIFY"] == 1 and report["counts"]["ok"] == 1
    assert set(report["phase_timings"]) >= {"preflight", "read", "plan", "review", "apply"}
    assert report["versions"]["eta_automations"] == "1.0.0"
    assert report["settings"]["qbo"]["configured"] is True
    text = res.summary.report_json.read_text(encoding="utf-8")
    for secret in ("test-qb-client-secret", "test-qb-refresh-token", "test-vship-password"):
        assert secret not in text


async def test_console_plan_summary_format(world: FakeWorld, run_pipeline: Runner) -> None:
    rows = [*SCENARIOS["P1"](world), *SCENARIOS["P4"](world), *SCENARIOS["P15"](world), *SCENARIOS["P19"](world),
            *SCENARIOS["P23"](world)]
    res = await run_pipeline(rows)
    lines = res.out.splitlines()
    start = lines.index(f"Plan (run {RUN_ID}, 5 rows, cutoff Mon 2026-11-16 09:30)")
    assert lines[start + 1:start + 11] == [
        "  NOTIFY ......... 2   (e-mails to send)",
        "  PAID_UPDATE .... 1",
        "  FUTURE_UPDATE .. 0",
        "  BEN ............ 1",
        "  BINDU .......... 0",
        "  ACCOUNTING ..... 0",
        "  SKIPPED ........ 0",
        "  CUSTOMS_HOLD ... 0",
        "  FAILED ......... 1",
        "Warnings: 1 (see report)",
    ]
    assert f"  EBKG10000001 → EBKG10000001 A | {SUBJECT_P1} | to: {DEFAULT_EMAIL}" in lines
    assert "  EBKG10000004 → 2026-11-03 (PAID_UPDATE)" in lines
    assert "  1 booking(s) failed — CarrierLookupError: boom [EBKG10000019]" not in lines
    assert "  MSC: 1 booking(s) failed — boom [EBKG10000019]" in lines
    assert "Done: 2 succeeded, 2 failed/skipped." in lines


async def test_plan_summary_warns_that_a_shadow_run_changes_quickbooks_and_vship(world: FakeWorld,
                                                                                 run_pipeline: Runner) -> None:
    """CLI-01 (04b §10 step 4): QB_EMAIL_REDIRECT_TO only changes the e-mail recipients; the DocNumber suffix, the
    VShip notes and the ledger are still written, and they record the notice as sent."""
    rows = SCENARIOS["P1"](world)
    res = await run_pipeline(rows, env={"QB_EMAIL_STRATEGY": "graph", "QB_EMAIL_REDIRECT_TO": "shadow@internal.example",
                                        "EMAIL_FROM": "accounting@example.com", "EMAIL_GRAPH_TENANT_ID": "t",
                                        "EMAIL_GRAPH_CLIENT_ID": "c", "EMAIL_GRAPH_CLIENT_SECRET": "s"},
                             dry_run=True)
    line = next(x for x in res.out.splitlines() if "QB_EMAIL_REDIRECT_TO is set" in x)
    assert "shadow@internal.example" in line and "REAL" in line
    quiet = await run_pipeline(rows, dry_run=True)                       # no redirect → no such line
    assert "QB_EMAIL_REDIRECT_TO" not in quiet.out
    manual = await run_pipeline(rows, env={"QB_EMAIL_REDIRECT_TO": "shadow@internal.example"}, dry_run=True,
                                no_email=True)
    assert "QB_EMAIL_REDIRECT_TO" not in manual.out                      # --no-email: this run sends no e-mail


async def test_redirect_refused_by_template_swap_fails_the_row_before_any_change(world: FakeWorld,
                                                                                 run_pipeline: Runner) -> None:
    """QBO-03 (04 §6.6, 10 Phase 4, 04b §3): with template_swap on the production company, QB_EMAIL_REDIRECT_TO is
    allowed only for an invoice whose BillEmail already is the redirect address. Any other NOTIFY row fails while
    planning, before its DocNumber is suffixed — not at the send, after the QuickBooks update."""
    customer = world.add("EBKG10000001", eta=ARRIVED)
    internal = world.add("EBKG10000002", eta=ARRIVED, bill_email="Shadow@Example.com")
    res = await run_pipeline([customer, internal], env={"QB_EMAIL_REDIRECT_TO": "shadow@example.com"})
    assert res.plan("EBKG10000001").status == "FAILED"
    assert res.failed_text == ("[ERROR] #EBKG10000001 - QB Invoice Processing Error, No Email Sent\n"
                               f"{REDIRECT_NOT_ALLOWED}\n\n")
    assert not any(w[1].startswith("EBKG10000001") for w in world.writes())     # no update, no send, no revert
    assert res.plan("EBKG10000002").status == "OK"                             # the spec-allowed case still sends
    assert send("EBKG10000002 A", subject("EBKG10000002", ARRIVED_FIRST)) in world.writes()


async def test_redirect_refusal_does_not_apply_without_e_mails(world: FakeWorld, run_pipeline: Runner) -> None:
    """QBO-03: --no-email sends nothing, so the redirect rule cannot fail its rows (they go to ToEmail_<run>.csv)."""
    b = world.add("EBKG10000001", eta=ARRIVED)
    res = await run_pipeline([b], env={"QB_EMAIL_REDIRECT_TO": "shadow@example.com"}, no_email=True)
    plan = res.plan("EBKG10000001")
    assert plan.status == "OK" and plan.message == "e-mail pending (manual send, --no-email)"
    assert res.failed_text is None


@pytest.fixture
def report_csv_locked(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Windows/Excel: an open run_<id>.csv cannot be replaced (PermissionError, WinError 32). Returns the refused
    destinations."""
    from eta_automations import outputs
    real_replace = outputs.os.replace
    refused: list[Path] = []

    def replace(src: Any, dst: Any) -> None:
        target = Path(dst)
        if target.name.startswith("run_") and target.suffix == ".csv" and target.stem.count("_") == 2 \
                and target.exists():
            refused.append(target)
            raise PermissionError(32, "The process cannot access the file because it is being used by another "
                                      "process", str(target))
        real_replace(src, dst)

    monkeypatch.setattr(outputs.os, "replace", replace)
    return refused


async def test_declined_run_with_the_plan_report_open_in_excel_still_aborts(
        world: FakeWorld, run_pipeline: Runner, report_csv_locked: list[Path]) -> None:
    """XCT-04: the user opened the plan CSV while the [y/N] prompt waited, then answered N: exit 3, nothing changed,
    no crash; the JSON report still records the abort."""
    res = await run_pipeline(SCENARIOS["P1"](world), assume_yes=False, answer="n")
    assert res.summary.exit_code == 3 and report_csv_locked
    assert world.writes() == [] and res.report()["report_stage"] == "aborted"


async def test_confirmed_run_with_the_plan_report_open_in_excel_writes_the_final_report(
        world: FakeWorld, run_pipeline: Runner, report_csv_locked: list[Path]) -> None:
    res = await run_pipeline(SCENARIOS["P1"](world), assume_yes=False, answer="y")
    assert res.summary.exit_code == 0 and report_csv_locked
    report = res.report()
    assert report["report_stage"] == "final" and [r["status"] for r in report["rows"]] == ["OK"]
    line = next(x for x in res.out.splitlines() if x.startswith("Report: "))
    csv_path = Path(line.removeprefix("Report: ").split(" | ")[0])
    assert csv_path == res.settings.run.output_dir / "reports" / f"run_{RUN_ID}_final.csv"
    import csv
    rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
    assert [r["status"] for r in rows] == ["OK"]


async def test_rescue_and_container_warnings_are_listed(world: FakeWorld, run_pipeline: Runner) -> None:
    b = world.add("EBKG10000068", eta=OUT_OF_WINDOW,
                  details={"rescue": True, "other_container_etas": {"MSCU2": "2026-12-03"}})
    res = await run_pipeline([b], dry_run=True)
    warnings = res.plan("EBKG10000068").warnings
    assert any("rescue-only fallback" in w for w in warnings)
    assert any("MSC other containers disagree" in w for w in warnings)
    assert "Warnings: 2 (see report)" in res.out
    assert res.report()["rows"][0]["eta_rescue"] is True


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Ctrl+C during apply (10 Phase 4 step 3)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_interruption_finishes_the_current_action_and_marks_the_rest(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock,
        state_dir: Path) -> None:
    rows = [world.add(f"EBKG1000005{i}", eta=OUT_OF_WINDOW) for i in range(1, 4)]
    settings = make_settings({"ETA_MAX_PARALLEL_BOOKINGS": "1"})
    entered, gate = asyncio.Event(), asyncio.Event()

    async def hold(method: str, target: Any) -> None:
        if method == "qbo.update_invoice" and not gate.is_set():
            entered.set()
            await gate.wait()

    world.qbo.before_write = hold
    out = io.StringIO()
    task = asyncio.create_task(run(settings, write_input(tmp_path, rows), assume_yes=True, clock=fixed_clock,
                                   services=world.services(settings), out=out))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()                     # the running QBO update is allowed to finish
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert world.writes() == [upd("EBKG10000051", date(2026, 12, 1), None)]   # nothing after the current action
    report = json.loads((settings.run.output_dir / "reports" / f"run_{RUN_ID}.json").read_text(encoding="utf-8"))
    assert report["report_stage"] == "interrupted"
    assert [r["status"] for r in report["rows"]] == ["INTERRUPTED"] * 3
    assert report["rows"][0]["actions_done"] == "QBO update ETA=2026-12-01"
    assert report["rows"][1]["message"] == "interrupted (not started)"
    ledger = [json.loads(x) for x in (state_dir / LEDGER_FILE_NAME).read_text(encoding="utf-8").splitlines()]
    assert [(r["action"], r["ok"]) for r in ledger] == [("qbo_update", True)]
    assert "Interrupted" in out.getvalue()
    with run_lock(state_dir):                   # the lock was released
        pass


async def run_and_interrupt(world: FakeWorld, settings: Settings, tmp_path: Path, fixed_clock: FixedClock,
                            rows: list[Booking], method: str) -> dict[str, Any]:
    """Start a confirmed run, hold the first `method` write, press Ctrl+C (cancel the run task) while it is held,
    then let the write finish. Returns the rewritten (interrupted) run report."""
    entered, gate = asyncio.Event(), asyncio.Event()

    async def hold(name: str, target: Any) -> None:
        if name == method and not gate.is_set():
            entered.set()
            await gate.wait()

    world.qbo.before_write = hold
    world.vship.before_write = hold
    task = asyncio.create_task(run(settings, write_input(tmp_path, rows), assume_yes=True, clock=fixed_clock,
                                   services=world.services(settings), out=io.StringIO()))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done()                     # the running write is allowed to finish
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    report: dict[str, Any] = json.loads(
        (settings.run.output_dir / "reports" / f"run_{RUN_ID}.json").read_text(encoding="utf-8"))
    assert report["report_stage"] == "interrupted"
    return report


async def test_interruption_after_the_suffix_reverts_the_doc_number(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock,
        state_dir: Path) -> None:
    """RUL-01: Ctrl+C while a NOTIFY row's suffixing QBO update runs. A notice is recorded twice — the `A` suffix and
    the VShip NOTIF note (01 §1) — so a suffix without an e-mail would make the next run send the first notice again
    under 'X AA' and lose the second one. The update finishes, then the D11 revert puts the number back."""
    b = world.add("EBKG10000001", "2450.00", eta=ARRIVED)
    settings = make_settings()
    report = await run_and_interrupt(world, settings, tmp_path, fixed_clock, [b], "qbo.update_invoice")
    assert world.writes() == [upd("EBKG10000001", date(2026, 11, 3), "EBKG10000001 A"),
                              upd("EBKG10000001 A", None, "EBKG10000001")]
    row = report["rows"][0]
    assert row["status"] == "INTERRUPTED"
    assert row["message"] == ("interrupted after: QBO update ETA=2026-11-03, DocNumber -> EBKG10000001 A; "
                              "DocNumber reverted to EBKG10000001")
    ledger = [json.loads(x) for x in (state_dir / LEDGER_FILE_NAME).read_text(encoding="utf-8").splitlines()]
    assert [(r["action"], r["ok"]) for r in ledger] == [("qbo_update", True), ("qbo_revert_doc_number", True)]
    assert not (settings.run.output_dir / OutputFile.FAILED.value).exists()     # nothing left to repair


async def test_interruption_after_the_suffix_with_a_failed_revert_is_reported(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock) -> None:
    b = world.add("EBKG10000001", "2450.00", eta=ARRIVED)
    assert b.invoice_id is not None
    world.qbo.revert_errors[b.invoice_id] = InvoiceUpdateError("qbo", "stale object twice")
    settings = make_settings()
    report = await run_and_interrupt(world, settings, tmp_path, fixed_clock, [b], "qbo.update_invoice")
    assert report["rows"][0]["status"] == "FAILED"
    assert (settings.run.output_dir / OutputFile.FAILED.value).read_text(encoding="utf-8") == (
        "[ERROR] #EBKG10000001 - QB Invoice Processing Error, No Email Sent\n"
        "interrupted (Ctrl+C) before the e-mail was sent (could not revert invoice number: qbo: stale object "
        "twice)\n\n")


async def test_interruption_during_the_send_still_writes_the_vship_note(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock) -> None:
    """RUL-01: Ctrl+C while a NOTIFY row's e-mail is being sent. The e-mail goes out, so the VShip NOTIF note — the
    record the 7-day rule reads (01 §11) — is written before the plan stops; the next day the row is
    'Done in the last week' instead of a second first notice."""
    b = world.add("EBKG10000001", "2450.00", eta=ARRIVED)
    assert b.vship_id is not None
    settings = make_settings()
    report = await run_and_interrupt(world, settings, tmp_path, fixed_clock, [b], "qbo.send_invoice")
    assert world.writes() == [upd("EBKG10000001", date(2026, 11, 3), "EBKG10000001 A"),
                              send("EBKG10000001 A", SUBJECT_P1),
                              note(b.vship_id, "ARRIVED ON 11/03. NOTIF #1")]       # no ETA patch after Ctrl+C
    row = report["rows"][0]
    assert row["status"] == "INTERRUPTED"
    assert row["message"].endswith("QBO e-mail; VShip note 'ARRIVED ON 11/03. NOTIF #1'")
    assert not (settings.run.output_dir / OutputFile.FAILED.value).exists()
    world2, day2 = await next_day_run(settings, tmp_path, "EBKG10000001 A", world.vship.comments[b.vship_id])
    assert day2.plans[0].message == "[SKIP] #EBKG10000001 A - Done in the last week"
    assert world2.calls.named("qbo.send_invoice") == []


async def test_interruption_with_parallel_graph_sends_writes_the_note_of_every_e_mailed_row(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock) -> None:
    """Four NOTIFY rows in parallel with graph/smtp, whose sends all enter the e-mail layer at once and queue inside it
    (QB_EMAIL_MAX_CONCURRENCY) when Ctrl+C comes. Every started send finishes (Phase 4 step 3), and every row that
    e-mailed also gets its VShip note."""
    rows = [world.add(f"EBKG1000008{i}", eta=ARRIVED) for i in range(1, 5)]
    settings = make_settings({**GRAPH_ENV, "ETA_MAX_PARALLEL_BOOKINGS": "4"})
    entered, gate, lock = asyncio.Event(), asyncio.Event(), asyncio.Lock()

    async def hold(name: str, target: Any) -> None:
        if name == "qbo.send_invoice":
            async with lock:
                if not gate.is_set():
                    entered.set()
                    await gate.wait()

    world.qbo.before_write = hold
    task = asyncio.create_task(run(settings, write_input(tmp_path, rows), assume_yes=True, clock=fixed_clock,
                                   services=world.services(settings), out=io.StringIO()))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    for b in rows:
        assert [c[0] for c in world.row_writes(b)] == ["qbo.update_invoice", "qbo.send_invoice", "vship.post_comment"]
    assert world.calls.named("vship.patch_expected_arrival") == []


async def test_interruption_during_the_send_with_a_failed_note_is_reported(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock) -> None:
    b = world.add("EBKG10000001", "2450.00", eta=ARRIVED)
    assert b.vship_id is not None
    world.vship.post_errors[b.vship_id] = NoteError("vship", "comment rejected (HTTP 500)")
    settings = make_settings()
    report = await run_and_interrupt(world, settings, tmp_path, fixed_clock, [b], "qbo.send_invoice")
    assert report["rows"][0]["status"] == "FAILED"
    assert (settings.run.output_dir / OutputFile.FAILED.value).read_text(encoding="utf-8") == (
        "[ERROR] #EBKG10000001 - Note Write Error, QB Invoice Processing Successful\n"
        "vship: comment rejected (HTTP 500)\n\n")


STOP_HINT = "do not press Ctrl+C again"


@dataclass
class FakeAsyncioRunSigint:
    handler: Callable[[int, Any], None]
    calls: list[str]


@pytest.fixture
def asyncio_run_sigint() -> Iterator[FakeAsyncioRunSigint]:
    """asyncio.run()'s SIGINT handler, emulated for one test (no real signal is sent). After the first Ctrl+C, which
    cancels the main task, it turns the next one into KeyboardInterrupt, and asyncio.run() then cancels every task —
    the shielded writes included. Here a call records itself and cancels every task but the caller's. The process's
    own handler is put back afterwards."""
    calls: list[str] = []

    def handler(signum: int, frame: Any) -> None:
        calls.append("KeyboardInterrupt")
        current = asyncio.current_task()
        for task in asyncio.all_tasks():
            if task is not current:
                task.cancel()

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handler)
    try:
        yield FakeAsyncioRunSigint(handler, calls)
    finally:
        signal.signal(signal.SIGINT, previous if previous is not None else signal.default_int_handler)


async def spin(predicate: Callable[[], bool] | None = None, *, turns: int = 50) -> None:
    """Let the other tasks run (deterministic: no sleeping) until `predicate()` holds, or for `turns` loop turns."""
    for _ in range(turns):
        if predicate is not None and predicate():
            return
        await asyncio.sleep(0)
    assert predicate is None or predicate(), "condition not reached"


def second_ctrl_c() -> None:
    """Call the SIGINT handler that is installed now, as a second Ctrl+C would."""
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler), handler
    handler(signal.SIGINT, None)


@pytest.mark.parametrize("method", ["qbo.update_invoice", "qbo.send_invoice"])
async def test_a_second_ctrl_c_while_stopping_only_repeats_the_message(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock,
        asyncio_run_sigint: FakeAsyncioRunSigint, method: str) -> None:
    """NEW-01: the first Ctrl+C makes the run say at once that it finishes the changes it started and that Ctrl+C must
    not be pressed again; a second Ctrl+C only repeats that. Left to asyncio.run(), it would raise KeyboardInterrupt and
    cancel every task, so the D11 revert (QBO update in flight) or the VShip NOTIF note (e-mail in flight) that keeps
    the notice whole (RUL-01) would never be written."""
    b = world.add("EBKG10000001", "2450.00", eta=ARRIVED)
    settings = make_settings()
    entered, gate = asyncio.Event(), asyncio.Event()

    async def hold(name: str, target: Any) -> None:
        if name == method and not gate.is_set():
            entered.set()
            await gate.wait()

    world.qbo.before_write = hold
    out = io.StringIO()
    task = asyncio.create_task(run(settings, write_input(tmp_path, [b]), assume_yes=True, clock=fixed_clock,
                                   services=world.services(settings), out=out))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()                                     # the first Ctrl+C: asyncio.run() cancels the main task
    await spin()
    assert out.getvalue().count(STOP_HINT) == 1       # said at once, while the write is still running
    assert "Stopping (Ctrl+C): finishing the changes already started for 1 invoice(s)." in out.getvalue()
    second_ctrl_c()
    await spin()
    assert asyncio_run_sigint.calls == []             # asyncio.run()'s handler was not reached ...
    assert out.getvalue().count(STOP_HINT) == 2       # ... the message is only repeated
    assert not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    suffix = upd("EBKG10000001", date(2026, 11, 3), "EBKG10000001 A")
    if method == "qbo.update_invoice":
        assert world.writes() == [suffix, upd("EBKG10000001 A", None, "EBKG10000001")]
    else:
        assert world.writes() == [suffix, send("EBKG10000001 A", SUBJECT_P1),
                                  note(b.vship_id, "ARRIVED ON 11/03. NOTIF #1")]
    assert signal.getsignal(signal.SIGINT) is asyncio_run_sigint.handler     # put back once the run has ended


async def test_ctrl_c_withdraws_the_e_mails_still_waiting_for_their_template_swap_turn(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock) -> None:
    """NEW-01: template_swap sends one e-mail at a time (04b §6.2 step 3). At Ctrl+C the e-mail being sent finishes
    and gets its VShip note. The rows still waiting for their turn have written nothing but their suffixing QBO update,
    so they are withdrawn at once and their DocNumber is put back (D11), instead of being e-mailed one after the other
    while the user waits."""
    rows = [world.add(f"EBKG1000008{i}", eta=ARRIVED) for i in range(1, 4)]
    settings = make_settings({"ETA_MAX_PARALLEL_BOOKINGS": "3"})
    entered, gate = asyncio.Event(), asyncio.Event()

    async def hold(name: str, target: Any) -> None:
        if name == "qbo.send_invoice" and not gate.is_set():
            entered.set()
            await gate.wait()

    world.qbo.before_write = hold
    task = asyncio.create_task(run(settings, write_input(tmp_path, rows), assume_yes=True, clock=fixed_clock,
                                   services=world.services(settings), out=io.StringIO()))
    await asyncio.wait_for(entered.wait(), 5)
    await spin(lambda: len(world.calls.named("qbo.update_invoice")) == 3)   # the others wait for their turn
    await spin()
    task.cancel()
    senders = [b for b in rows if send(f"{b.doc} A", subject(b.doc, ARRIVED_FIRST)) in world.calls]
    assert len(senders) == 1
    waiting = [b for b in rows if b not in senders]
    await spin(lambda: all(len(world.row_writes(b)) == 2 for b in waiting))
    for b in waiting:                 # reverted at once, while the e-mail in progress is still being sent
        assert world.row_writes(b) == [upd(b.doc, date(2026, 11, 3), f"{b.doc} A"), upd(f"{b.doc} A", None, b.doc)]
    assert not task.done()
    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    sender = senders[0]
    assert world.row_writes(sender) == [upd(sender.doc, date(2026, 11, 3), f"{sender.doc} A"),
                                        send(f"{sender.doc} A", subject(sender.doc, ARRIVED_FIRST)),
                                        note(sender.vship_id, "ARRIVED ON 11/03. NOTIF #1")]
    assert len(world.calls.named("qbo.send_invoice")) == 1
    report = json.loads((settings.run.output_dir / "reports" / f"run_{RUN_ID}.json").read_text(encoding="utf-8"))
    by_doc = {r["doc_number"]: r for r in report["rows"]}
    for b in waiting:
        assert by_doc[b.doc]["status"] == "INTERRUPTED"
        assert by_doc[b.doc]["message"] == (f"interrupted after: QBO update ETA=2026-11-03, DocNumber -> {b.doc} A; "
                                            f"DocNumber reverted to {b.doc}")
    assert not (settings.run.output_dir / OutputFile.FAILED.value).exists()


def test_ctrl_c_outside_the_main_thread_still_finishes_the_started_changes(
        world: FakeWorld, make_settings: Callable[..., Settings], tmp_path: Path, fixed_clock: FixedClock) -> None:
    """NEW-01: a run on another thread cannot change the SIGINT handler (signal.signal only works on the main thread);
    the cancellation is still handled the same way."""
    b = world.add("EBKG10000001", "2450.00", eta=ARRIVED)
    settings = make_settings()
    before = signal.getsignal(signal.SIGINT)
    outcome: dict[str, Any] = {}

    def worker() -> None:
        try:
            outcome["report"] = asyncio.run(run_and_interrupt(world, settings, tmp_path, fixed_clock, [b],
                                                              "qbo.send_invoice"))
        except BaseException as exc:  # noqa: BLE001 - reported by the assertion below
            outcome["error"] = exc

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert not thread.is_alive() and "error" not in outcome, outcome
    assert outcome["report"]["rows"][0]["status"] == "INTERRUPTED"
    assert world.writes()[-1] == note(b.vship_id, "ARRIVED ON 11/03. NOTIF #1")
    assert signal.getsignal(signal.SIGINT) is before


# ════════════════════════════════════════════════════════════════════════════════════════════════
# build_services (10 §3)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_build_services_creates_real_clients_without_network(make_settings: Callable[..., Settings],
                                                                   state_dir: Path) -> None:
    settings = make_settings()
    async with pipeline.build_services(settings, datetime(2026, 11, 5, 9, 30), read_only=True) as services:
        assert isinstance(services.qbo, QboClient) and services.qbo.read_only
        assert isinstance(services.vship, VShipClient) and services.vship.read_only
        assert isinstance(services.router, CarrierRouter)
        assert services.clock.now() == datetime(2026, 11, 5, 9, 30)
        assert not services.outputs.enabled and not services.ledger.enabled
        assert services.token_store.path == state_dir / "tokens.json"
    async with pipeline.build_services(settings, datetime(2026, 11, 5, 9, 30), read_only=False) as services:
        assert not services.qbo.read_only and services.outputs.enabled and services.ledger.enabled


async def test_dry_run_never_registers_aggregator_bookings(world: FakeWorld, make_settings: Callable[..., Settings],
                                                         tmp_path: Path, fixed_clock: FixedClock,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """CRA-01 (11 §2: a dry run writes nothing anywhere): the real build_services() builds the router read-only, so
    the aggregator never registers a booking at ShipsGo (1 credit each) nor writes aggregator_tracking.json."""
    import respx

    def fake_qbo(settings: Settings, token_store: Any, *, read_only: bool) -> Any:
        world.qbo.read_only = read_only
        return world.qbo

    def fake_vship(settings: Settings, token_store: Any, *, read_only: bool) -> Any:
        world.vship.read_only = read_only
        return world.vship

    monkeypatch.setattr(pipeline, "build_qbo", fake_qbo)
    monkeypatch.setattr(pipeline, "build_vship", fake_vship)
    settings = make_settings({"AGG_API_TOKEN": "agg-token-123456", "ETA_PROVIDER_CMA": "aggregator"})
    assert settings.aggregator is not None
    base = settings.aggregator.base_url
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as api:
        api.get(f"{base}/ocean/shipments").mock(return_value=httpx.Response(
            200, json={"message": "SUCCESS", "shipments": [], "meta": {"more": False, "total": 0}}))
        create = api.post(f"{base}/ocean/shipments").mock(return_value=httpx.Response(
            200, json={"message": "SUCCESS", "shipment": {"id": 501}}))
        summary = await run(settings, write_input(tmp_path, ["NAM1234567,5000"]), dry_run=True, clock=fixed_clock,
                            out=io.StringIO())
    assert summary.exit_code == 0 and create.call_count == 0
    assert not settings.aggregator.state_file.exists()
    assert "a real run registers it (1 credit)" in summary.plans[0].message
    assert world.qbo.closed == 1


async def test_run_through_monkeypatched_build_services(world: FakeWorld, make_settings: Callable[..., Settings],
                                                        tmp_path: Path, fixed_clock: FixedClock,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline, "build_services", fake_build_services(world))
    rows = SCENARIOS["P1"](world)
    settings = make_settings()
    summary = await run(settings, write_input(tmp_path, rows), dry_run=True, clock=fixed_clock, out=io.StringIO())
    assert summary.exit_code == 0 and summary.plans[0].route is Route.NOTIFY
    assert world.qbo.closed == 1 and world.vship.closed == 1 and world.writes() == []
    real = await run(settings, write_input(tmp_path, rows), assume_yes=True, clock=fixed_clock, out=io.StringIO())
    assert real.exit_code == 0 and real.ok == 1 and len(world.writes()) == 4
