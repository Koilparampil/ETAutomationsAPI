"""qbo/email.py — invoice e-mail delivery strategies (04b-invoice-email-delivery §9 tests 1–15, plus edge cases).

respx-mocked QuickBooks/Graph (an in-memory fake keeps the Preferences state so the call order and the subject
in effect at send time can be asserted); SMTP via a fake smtplib.SMTP class. No network."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
import logging
import os
import smtplib
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eta_automations.clock import FixedClock
from eta_automations.config import Settings
from eta_automations.errors import (
    ConfigError,
    EmailPathUnavailableError,
    InvoiceSendError,
    TemplateRestoreError,
)
from eta_automations.qbo import email as qbo_email
from eta_automations.qbo.client import EtaFieldSpec, InvoiceSnapshot, QboClient
from eta_automations.qbo.email import (
    DEFAULT_BODY_TEMPLATE,
    ETA_SUBJECT_RE,
    RECOVERY_FILE_NAME,
    VERIFIED_FILE_NAME,
    GraphTransport,
    MailboxEmailer,
    OutgoingMail,
    SendReceipt,
    SmtpTransport,
    TemplateSwapEmailer,
    build_emailer,
    build_mime_message,
    graph_payload,
    is_verified,
    load_verifications,
    pdf_file_name,
    record_verification,
    render_body,
    validate_send,
)
from eta_automations.token_store import TokenStore

RUN_NOW = datetime(2026, 11, 5, 9, 30)
REALM = "1234567890"
API = "https://quickbooks.api.intuit.com"
SANDBOX_API = "https://sandbox-quickbooks.api.intuit.com"
COMPANY = f"{API}/v3/company/{REALM}"
INVOICE_URL = f"{COMPANY}/invoice"
ORIGINAL = "Invoice [Invoice No.] from [Company Name]"
SUBJECT = ("Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE "
           "CARGO****")
SUBJECT_B = "Invoice for Booking no. EBKG23456789 A. ***ESTIMATED ARRIVING ON 11/12. PLEASE MAKE PAYMENT ARRANGEMENT****"
ETA_SPEC = EtaFieldSpec("1000000003", "ETA", "DateType", "DateValue", "invoice 190613")
PDF = b"%PDF-1.7\n% test invoice\n"
LINK = "https://connect.intuit.com/portal/app/CommerceNetwork/view/scs-v1-abc"
GRAPH_TOKEN_URL = "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/users/accounting@example.com/sendMail"
GRAPH_ENV = {"QB_EMAIL_STRATEGY": "graph", "EMAIL_FROM": "accounting@example.com",
             "EMAIL_REPLY_TO": "accounting@example.com", "EMAIL_BCC": "archive@example.com",
             "EMAIL_GRAPH_TENANT_ID": "tenant-1", "EMAIL_GRAPH_CLIENT_ID": "graph-client",
             "EMAIL_GRAPH_CLIENT_SECRET": "graph-secret"}
SMTP_ENV = {"QB_EMAIL_STRATEGY": "smtp", "EMAIL_FROM": "accounting@example.com", "EMAIL_REPLY_TO": "billing@example.com",
            "EMAIL_BCC": "archive@example.com", "EMAIL_SMTP_HOST": "smtp.example.com",
            "EMAIL_SMTP_USERNAME": "mailer", "EMAIL_SMTP_PASSWORD": "smtp-pass"}


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
class StaticAuth:
    async def get_token(self) -> str:
        return "AT-TEST"

    async def invalidate(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def fault(code: str, message: str = "Validation error") -> dict[str, Any]:
    return {"Fault": {"Error": [{"Message": message, "Detail": message, "code": code}], "type": "ValidationFault"},
            "time": "t"}


class FakeQuickBooks:
    """Preferences (full update with SyncToken checks), invoice /send and invoice reads, in memory."""

    def __init__(self, load_fixture: Callable[[str], Any]) -> None:
        self.prefs: dict[str, Any] = copy.deepcopy(load_fixture("qbo/preferences.json")["Preferences"])
        self.send_ok: dict[str, Any] = load_fixture("qbo/send_ok.json")
        self.events: list[tuple[Any, ...]] = []
        self.prefs_bodies: list[dict[str, Any]] = []
        self.subject_limit: int | None = None
        self.fail_subjects: set[str] = set()
        self.send_effect: str | None = None          # None | "timeout" | "fault"
        self.send_delay = 0.0
        self.prefs_delay = 0.0                       # answer delay of a successful Preferences POST
        self.on_send: Callable[[], None] | None = None
        self.invoice_raw: dict[str, Any] = {"Id": "190613", "SyncToken": "9", "DocNumber": "EBKG12345678",
                                            "TotalAmt": 2450.0, "Balance": 2450.0}

    @property
    def subject(self) -> str:
        return str(self.prefs["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"])

    def set_subject(self, subject: str) -> None:
        self.prefs["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] = subject

    def kinds(self) -> list[str]:
        return [e[0] for e in self.events]

    def mount(self, router: respx.MockRouter, *, base: str = COMPANY) -> None:
        router.get(f"{base}/preferences").mock(side_effect=self.get_prefs)
        router.post(f"{base}/preferences").mock(side_effect=self.post_prefs)
        router.route(method="POST", path__regex=r"^/v3/company/\d+/invoice/\d+/send$").mock(side_effect=self.post_send)
        router.route(method="GET", path__regex=r"^/v3/company/\d+/invoice/\d+$").mock(side_effect=self.get_invoice)

    def get_prefs(self, request: httpx.Request) -> httpx.Response:
        self.events.append(("GET prefs",))
        return httpx.Response(200, json={"Preferences": copy.deepcopy(self.prefs), "time": "t"})

    async def post_prefs(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        subject = body["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"]
        self.events.append(("POST prefs", subject))
        self.prefs_bodies.append(body)
        if subject in self.fail_subjects:
            return httpx.Response(400, json=fault("2020", "Required param missing"))
        if str(body.get("SyncToken")) != str(self.prefs["SyncToken"]):
            return httpx.Response(400, json=fault("5010", "Stale Object Error"))
        self.set_subject(subject if self.subject_limit is None else subject[: self.subject_limit])
        self.prefs["SyncToken"] = str(int(self.prefs["SyncToken"]) + 1)
        answer = copy.deepcopy(self.prefs)
        if self.prefs_delay:
            await asyncio.sleep(self.prefs_delay)    # applied, but the answer is slow
        return httpx.Response(200, json={"Preferences": answer, "time": "t"})

    async def post_send(self, request: httpx.Request) -> httpx.Response:
        self.events.append(("POST send", self.subject, request.url.params.get("sendTo")))
        if self.on_send is not None:
            self.on_send()
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.send_effect == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if self.send_effect == "fault":
            return httpx.Response(400, json=fault("6000", "Email address is invalid"))
        body = copy.deepcopy(self.send_ok)
        body["Invoice"]["Id"] = request.url.path.split("/")[-2]
        return httpx.Response(200, json=body)

    def get_invoice(self, request: httpx.Request) -> httpx.Response:
        self.events.append(("GET invoice",))
        return httpx.Response(200, json={"Invoice": self.invoice_raw, "time": "t"})


def invoices(load_fixture: Callable[[str], Any]) -> list[dict[str, Any]]:
    return copy.deepcopy(load_fixture("qbo/query_invoices.json")["QueryResponse"]["Invoice"])


def make_inv(raw: dict[str, Any]) -> InvoiceSnapshot:
    return InvoiceSnapshot.from_api(raw, ETA_SPEC, string_format="%m/%d/%Y")


def write_verified(state_dir: Path, strategy: str = "template_swap", verified: bool = True) -> None:
    record_verification(state_dir, realm_id=REALM, strategy=strategy, verified=verified,
                        subject_sent=f"ETA probe 20261101-120000 — {SUBJECT}", invoice_id="999")


def make_client(settings: Settings, state_dir: Path, token_store: TokenStore | None = None, *,
                read_only: bool = False) -> QboClient:
    assert settings.qbo is not None
    return QboClient(settings.qbo, StaticAuth(), state_dir=state_dir, token_store=token_store,
                     email_settings=settings.email, clock=FixedClock(RUN_NOW), read_only=read_only,
                     email_missing=tuple(settings.missing.get("email", ())))


@pytest.fixture
def no_restore_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(qbo_email, "_sleep", fake_sleep)
    return delays


@pytest.fixture
def swap_client(make_settings, state_dir) -> QboClient:
    write_verified(state_dir)
    return make_client(make_settings(), state_dir)


def recovery_file(state_dir: Path) -> Path:
    return state_dir / RECOVERY_FILE_NAME


# ════════════════════════════════════════════════════════════════════════════════════════════════
# template_swap
# ════════════════════════════════════════════════════════════════════════════════════════════════
# ── test 1 ──
async def test_template_swap_call_order_full_body_and_recovery_file(swap_client, state_dir, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    seen_recovery: list[dict[str, Any]] = []
    fake.on_send = lambda: seen_recovery.append(json.loads(recovery_file(state_dir).read_text(encoding="utf-8")))
    inv = make_inv(invoices(load_fixture)[0])
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        receipt = await swap_client.send_invoice(inv, subject=SUBJECT)
        assert fake.events == [("GET prefs",), ("POST prefs", SUBJECT), ("POST send", SUBJECT, None),
                               ("POST prefs", ORIGINAL)]
        expected = copy.deepcopy(load_fixture("qbo/preferences.json")["Preferences"])
        expected["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] = SUBJECT
        assert fake.prefs_bodies[0] == expected                        # the whole object, only the subject changed
        restore_expected = copy.deepcopy(load_fixture("qbo/preferences.json")["Preferences"])
        restore_expected["SyncToken"] = "13"
        assert fake.prefs_bodies[1] == restore_expected
        assert fake.subject == ORIGINAL
        # second send: no Preferences GET any more
        await swap_client.send_invoice(inv, subject=SUBJECT_B)
        assert fake.kinds()[4:] == ["POST prefs", "POST send", "POST prefs"]
    assert len(seen_recovery) == 2
    assert seen_recovery[0] == {**seen_recovery[0], "realm_id": REALM, "original_subject": ORIGINAL,
                                "swapped_subject": SUBJECT, "invoice_id": "190613",
                                "original_message": fake.prefs["EmailMessagesPrefs"]["InvoiceMessage"]["Message"],
                                "written_at": "2026-11-05T09:30:00"}
    assert isinstance(seen_recovery[0]["pid"], int)
    assert not recovery_file(state_dir).exists()
    assert receipt == SendReceipt(strategy="template_swap", invoice_id="190613", doc_number="EBKG12345678",
                                  subject=SUBJECT, to=("ap@acme.example", "ops@acme.example"),
                                  cc=("cc@acme.example",), bcc=(), sent_at=RUN_NOW,
                                  qbo_delivery_time="2026-11-05T06:32:10-08:00")
    await swap_client.aclose()


async def test_recovery_and_probe_files_are_private(swap_client, state_dir, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    modes: list[int] = []
    fake.on_send = lambda: modes.append(recovery_file(state_dir).stat().st_mode & 0o777)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        await swap_client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT)
    if os.name == "posix":
        assert (state_dir / VERIFIED_FILE_NAME).stat().st_mode & 0o777 == 0o600
        assert modes == [0o600]


# ── test 2 ──
async def test_lost_send_response_is_inferred_from_a_fresh_delivery_time(swap_client, state_dir, load_fixture,
                                                                         caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    fake = FakeQuickBooks(load_fixture)
    fake.send_effect = "timeout"
    delivered = (RUN_NOW + timedelta(seconds=3)).astimezone().isoformat()
    fake.invoice_raw = {**fake.invoice_raw, "DeliveryInfo": {"DeliveryType": "Email", "DeliveryTime": delivered}}
    inv = make_inv(invoices(load_fixture)[0])
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        receipt = await swap_client.send_invoice(inv, subject=SUBJECT)
    assert fake.kinds() == ["GET prefs", "POST prefs", "POST send", "POST prefs", "GET invoice"]
    assert fake.subject == ORIGINAL and not recovery_file(state_dir).exists()
    assert receipt.outcome_inferred is True and receipt.qbo_delivery_time == delivered
    assert any("send response lost but QuickBooks shows delivery at" in m for m in caplog.messages)


async def test_lost_send_response_without_delivery_is_outcome_unknown(swap_client, state_dir, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.send_effect = "timeout"
    fake.invoice_raw = {**fake.invoice_raw,
                        "DeliveryInfo": {"DeliveryType": "Email", "DeliveryTime": "2026-10-30T08:00:00-07:00"}}
    inv = make_inv(invoices(load_fixture)[0])
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(InvoiceSendError) as info:
            await swap_client.send_invoice(inv, subject=SUBJECT)
    assert "outcome unknown" in str(info.value)
    assert "check the invoice before re-sending" in str(info.value)
    assert fake.kinds().count("POST send") == 1                     # never re-POSTed
    assert fake.subject == ORIGINAL and not recovery_file(state_dir).exists()


async def test_rejected_send_is_safe_to_retry(swap_client, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.send_effect = "fault"
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(InvoiceSendError) as info:
            await swap_client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT)
    assert "invoice e-mail not sent" in str(info.value) and "safe to retry later" in str(info.value)
    assert fake.subject == ORIGINAL


# ── test 3 ──
async def test_restore_failing_three_times_breaks_the_emailer(swap_client, state_dir, load_fixture,
                                                             no_restore_sleep) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.fail_subjects = {ORIGINAL}
    raws = invoices(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(TemplateRestoreError) as info:
            await swap_client.send_invoice(make_inv(raws[0]), subject=SUBJECT)
        assert fake.kinds() == ["GET prefs", "POST prefs", "POST send",
                                "POST prefs", "GET prefs", "POST prefs", "GET prefs", "POST prefs"]
        assert no_restore_sleep == [1.0, 2.0]
        message = str(info.value)
        assert f"set the invoice e-mail subject back to: '{ORIGINAL}'" in message
        assert str(recovery_file(state_dir)) in message
        assert "WAS sent" in message                                   # the customer did get the e-mail
        assert isinstance(info.value.receipt, SendReceipt)              # type: ignore[attr-defined]
        emailer = swap_client.emailer
        assert isinstance(emailer, TemplateSwapEmailer) and emailer.broken
        assert recovery_file(state_dir).exists()
        before = len(fake.events)
        with pytest.raises(TemplateRestoreError):
            await swap_client.send_invoice(make_inv(raws[1]), subject=SUBJECT_B)
        assert len(fake.events) == before                              # refused without any HTTP call
        # shutdown retries the restore from the recovery file (QuickBooks works again by then)
        fake.fail_subjects = set()
        await swap_client.aclose()
    assert fake.subject == ORIGINAL and not recovery_file(state_dir).exists()


# ── test 4 ──
async def test_startup_recovery_restores_and_dry_run_only_warns(swap_client, state_dir, load_fixture,
                                                                caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    fake = FakeQuickBooks(load_fixture)
    fake.set_subject(SUBJECT)                                          # left modified by a crashed run
    recovery_file(state_dir).write_text(json.dumps({
        "realm_id": REALM, "original_subject": ORIGINAL, "original_message": "Dear [Customer Name]",
        "swapped_subject": SUBJECT, "invoice_id": "190613", "written_at": "2026-11-04T10:00:00", "pid": 4242}),
        encoding="utf-8")
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        await swap_client.prepare_for_sending(dry_run=True)
        assert "POST prefs" not in fake.kinds()
        assert recovery_file(state_dir).exists()
        assert (f"A previous run left the QuickBooks invoice e-mail subject modified ('{SUBJECT}'); it will be "
                "restored automatically on the next real run.") in caplog.messages
        await swap_client.prepare_for_sending()
        assert ("POST prefs", ORIGINAL) in fake.events
    assert fake.subject == ORIGINAL
    assert not recovery_file(state_dir).exists()


async def test_restore_template_helper_runs_only_the_recovery_step(make_settings, state_dir, load_fixture) -> None:
    client = make_client(make_settings(), state_dir)                    # no verification record: gate not involved
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        emailer = build_emailer(client)
        assert isinstance(emailer, TemplateSwapEmailer)
        assert await emailer.recover() is False
        assert fake.events == []
        fake.set_subject(SUBJECT)
        recovery_file(state_dir).write_text(json.dumps({"realm_id": REALM, "original_subject": ORIGINAL,
                                                        "swapped_subject": SUBJECT}), encoding="utf-8")
        assert await emailer.recover() is True
    assert fake.kinds() == ["GET prefs", "POST prefs"]
    assert fake.subject == ORIGINAL and not recovery_file(state_dir).exists()


async def test_real_preflight_after_a_dry_run_reads_the_template_again(swap_client, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        await swap_client.prepare_for_sending(dry_run=True)
        fake.set_subject("Edited after the dry run")
        await swap_client.prepare_for_sending()
        await swap_client.prepare_for_sending()                          # idempotent once it succeeded
    assert fake.kinds() == ["GET prefs", "GET prefs"]
    emailer = swap_client.emailer
    assert isinstance(emailer, TemplateSwapEmailer)
    assert emailer.original is not None and emailer.original.subject == "Edited after the dry run"


async def test_recovery_file_of_another_company_blocks_email(swap_client, state_dir, load_fixture) -> None:
    recovery_file(state_dir).write_text(json.dumps({"realm_id": "999", "original_subject": ORIGINAL}),
                                        encoding="utf-8")
    with respx.mock(assert_all_called=False) as router:
        FakeQuickBooks(load_fixture).mount(router)
        with pytest.raises(EmailPathUnavailableError) as info:
            await swap_client.prepare_for_sending()
    assert str(info.value) == f"recovery file belongs to another QuickBooks company: {recovery_file(state_dir)}"
    assert recovery_file(state_dir).exists()


async def test_failed_startup_recovery_is_a_template_restore_error(swap_client, state_dir, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.set_subject(SUBJECT)
    fake.fail_subjects = {ORIGINAL}
    recovery_file(state_dir).write_text(json.dumps({"realm_id": REALM, "original_subject": ORIGINAL,
                                                    "swapped_subject": SUBJECT}), encoding="utf-8")
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(TemplateRestoreError) as info:
            await swap_client.prepare_for_sending()
    assert str(info.value).startswith("In QuickBooks open Settings → Account and settings → Sales → Messages")
    assert recovery_file(state_dir).exists()


async def test_unreadable_recovery_file_blocks_email(swap_client, state_dir) -> None:
    recovery_file(state_dir).write_text("{not json", encoding="utf-8")
    with respx.mock(assert_all_called=False), pytest.raises(TemplateRestoreError, match="is not valid JSON"):
        await swap_client.prepare_for_sending()


# ── test 5 ──
async def test_truncated_subject_restores_and_never_sends(swap_client, state_dir, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.subject_limit = 60
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(InvoiceSendError) as info:
            await swap_client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT)
    assert str(info.value) == (f"qbo: QuickBooks stored the template subject as {SUBJECT[:60]!r} instead of "
                               f"{SUBJECT!r} (length limit?)")
    assert "POST send" not in fake.kinds()
    assert fake.events[-1] == ("POST prefs", ORIGINAL)
    assert fake.subject == ORIGINAL and not recovery_file(state_dir).exists()


async def test_rejected_subject_write_deletes_recovery_without_restore(swap_client, state_dir, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.fail_subjects = {SUBJECT}
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(InvoiceSendError, match="could not set the invoice e-mail subject in QuickBooks"):
            await swap_client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT)
    assert fake.kinds() == ["GET prefs", "POST prefs", "GET prefs"]      # template unchanged → no restore
    assert not recovery_file(state_dir).exists()


# ── test 6 ──
async def test_two_concurrent_sends_never_interleave(swap_client, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.send_delay = 0.02
    raws = invoices(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        r1, r2 = await asyncio.gather(swap_client.send_invoice(make_inv(raws[0]), subject=SUBJECT),
                                      swap_client.send_invoice(make_inv(raws[1]), subject=SUBJECT_B))
    assert fake.kinds() == ["GET prefs", "POST prefs", "POST send", "POST prefs",
                            "POST prefs", "POST send", "POST prefs"]
    swaps = [e for e in fake.events if e[0] == "POST prefs"]
    assert [s[1] for s in swaps[1::2]] == [ORIGINAL, ORIGINAL]
    sends = [e for e in fake.events if e[0] == "POST send"]
    assert {s[1] for s in sends} == {SUBJECT, SUBJECT_B}               # each e-mail went out with its own subject
    assert (r1.subject, r2.subject) == (SUBJECT, SUBJECT_B)
    assert fake.subject == ORIGINAL


async def test_cancellation_during_the_subject_write_restores_the_template(swap_client, state_dir,
                                                                           load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        await swap_client.prepare_for_sending()
        fake.prefs_delay = 5
        task = asyncio.create_task(swap_client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT))
        for _ in range(500):
            if fake.subject == SUBJECT:
                break
            await asyncio.sleep(0.005)
        fake.prefs_delay = 0
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert fake.kinds() == ["GET prefs", "POST prefs", "GET prefs", "POST prefs"]
    assert "POST send" not in fake.kinds()
    assert fake.subject == ORIGINAL and not recovery_file(state_dir).exists()


def test_send_failure_classification() -> None:
    from eta_automations.errors import AuthError, ServiceError
    never = qbo_email._send_never_reached
    assert never(ServiceError("qbo-oauth", "network error: ReadTimeout('x')"))
    assert never(AuthError("qbo: credentials/token rejected (HTTP 401)"))
    assert never(ServiceError("qbo", "send rejected", status=400))
    assert never(ServiceError("qbo", "unexpected HTTP 503", status=503))
    assert not never(ServiceError("qbo", "unexpected HTTP 502", status=502))
    timeout = ServiceError("qbo", "network error: ReadTimeout('x')")
    timeout.__cause__ = httpx.ReadTimeout("x")
    assert not never(timeout)
    refused = ServiceError("qbo", "network error: ConnectError('x')")
    refused.__cause__ = httpx.ConnectError("x")
    assert never(refused)
    assert not never(ValueError("unexpected"))


async def test_cancelled_send_still_restores_the_template(swap_client, state_dir, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.send_delay = 5
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        task = asyncio.create_task(swap_client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT))
        for _ in range(500):
            if "POST send" in fake.kinds():
                break
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert fake.kinds() == ["GET prefs", "POST prefs", "POST send", "POST prefs"]
    assert fake.subject == ORIGINAL and not recovery_file(state_dir).exists()


# ── test 7 ──
async def test_verification_gate(make_settings, state_dir, load_fixture) -> None:
    client = make_client(make_settings(), state_dir)
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(EmailPathUnavailableError) as info:
            await client.prepare_for_sending()
        assert str(info.value) == ("The template_swap e-mail method has not been verified for this company. Run "
                                   "`eta probe qbo --send-test --invoice <test invoice>` (04b §10) or set "
                                   "QB_EMAIL_STRATEGY=graph.")
        assert fake.events == []
        await client.prepare_for_sending(dry_run=True)                  # dry run skips the gate
        write_verified(state_dir, verified=True)
        await client.prepare_for_sending()                              # proceeds now
        write_verified(state_dir, verified=False)                       # a later failed send-test revokes it
        client2 = make_client(make_settings(), state_dir)
        with pytest.raises(EmailPathUnavailableError, match="has not been verified"):
            await client2.prepare_for_sending()
        client3 = make_client(make_settings(QB_EMAIL_REQUIRE_VERIFIED="false"), state_dir)
        await client3.prepare_for_sending()


def test_verification_records(state_dir) -> None:
    assert load_verifications(state_dir) == [] and not is_verified(state_dir, REALM, "template_swap")
    path = record_verification(state_dir, realm_id=REALM, strategy="graph", verified=True, subject_sent="S",
                               invoice_id="7", when=RUN_NOW)
    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"realm_id": REALM, "strategy": "graph", "verified": True, "verified_at": "2026-11-05T09:30:00",
         "subject_sent": "S", "invoice_id": "7"}]
    assert is_verified(state_dir, REALM, "graph") and not is_verified(state_dir, REALM, "template_swap")
    assert not is_verified(state_dir, "other", "graph") and not is_verified(None, REALM, "graph")
    path.write_text('{"realm_id": "1234567890", "strategy": "smtp", "verified": true}\n'
                    '{"realm_id": "1234567890", "strategy": "smtp", "verified": "yes"}\n', encoding="utf-8")
    assert len(load_verifications(state_dir)) == 2
    assert not is_verified(state_dir, REALM, "smtp")                     # latest record is not a JSON true


# ── test 8 ──
async def test_sanity_check_refuses_an_eta_looking_template_subject(swap_client, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    fake.set_subject(SUBJECT_B)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(EmailPathUnavailableError) as info:
            await swap_client.prepare_for_sending()
    assert str(info.value) == (f"The company's invoice e-mail subject currently looks like an ETA subject ({SUBJECT_B}) "
                               "— a previous run may have crashed without a recovery file. Restore the normal subject "
                               "in QuickBooks first.")
    assert "POST prefs" not in fake.kinds()


def test_eta_subject_regex() -> None:
    assert ETA_SUBJECT_RE.match(SUBJECT) and ETA_SUBJECT_RE.match(SUBJECT_B)
    assert ETA_SUBJECT_RE.match(f"ETA probe 20261105-093000 — {SUBJECT}")
    assert not ETA_SUBJECT_RE.match(ORIGINAL)
    assert not ETA_SUBJECT_RE.match("Invoice for Booking no. X without stars")


async def test_missing_invoice_message_blocks_template_swap(swap_client, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    del fake.prefs["EmailMessagesPrefs"]["InvoiceMessage"]
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(EmailPathUnavailableError, match="has no EmailMessagesPrefs.InvoiceMessage"):
            await swap_client.prepare_for_sending()


# ── test 9 ──
async def test_redirect_on_production_real_customer_is_refused(make_settings, state_dir, load_fixture) -> None:
    write_verified(state_dir)
    client = make_client(make_settings(QB_EMAIL_REDIRECT_TO="shadow@example.com"), state_dir)
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        with pytest.raises(EmailPathUnavailableError) as info:
            await client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT)
    assert str(info.value) == ("QB_EMAIL_REDIRECT_TO cannot be used with template_swap on real invoices (QuickBooks "
                               "would overwrite the customer's e-mail). Use graph/smtp for shadow runs.")
    assert "POST prefs" not in fake.kinds() and "POST send" not in fake.kinds()


async def test_redirect_on_sandbox_sends_with_send_to(make_settings, state_dir, load_fixture) -> None:
    write_verified(state_dir)
    client = make_client(make_settings(QB_EMAIL_REDIRECT_TO="shadow@example.com", QB_SANDBOX="true"), state_dir)
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router, base=f"{SANDBOX_API}/v3/company/{REALM}")
        receipt = await client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT)
    assert ("POST send", SUBJECT, "shadow@example.com") in fake.events
    assert receipt.redirected_to == "shadow@example.com" and receipt.to == ("shadow@example.com",)


async def test_redirect_allowed_when_recipient_already_is_the_redirect(make_settings, state_dir, load_fixture) -> None:
    write_verified(state_dir)
    client = make_client(make_settings(QB_EMAIL_REDIRECT_TO="Test@Example.com"), state_dir)
    raw = dict(invoices(load_fixture)[0], BillEmail={"Address": "test@example.com"})
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        await client.send_invoice(make_inv(raw), subject=SUBJECT)
    assert ("POST send", SUBJECT, "Test@Example.com") in fake.events


# ── test 10 ──
async def test_stale_preferences_on_first_write_rereads_and_retries_once(swap_client, load_fixture) -> None:
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        await swap_client.prepare_for_sending()
        fake.prefs["SyncToken"] = "40"                                 # someone saved Preferences meanwhile
        receipt = await swap_client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT)
    assert fake.kinds() == ["GET prefs", "POST prefs", "GET prefs", "POST prefs", "POST send", "POST prefs"]
    assert [b["SyncToken"] for b in fake.prefs_bodies] == ["12", "40", "41"]
    assert receipt.subject == SUBJECT and fake.subject == ORIGINAL


async def test_sparse_preferences_mode(make_settings, state_dir, load_fixture) -> None:
    write_verified(state_dir)
    client = make_client(make_settings(QB_PREFS_UPDATE_MODE="sparse"), state_dir)
    fake = FakeQuickBooks(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        await client.send_invoice(make_inv(invoices(load_fixture)[0]), subject=SUBJECT)
    message = load_fixture("qbo/preferences.json")["Preferences"]["EmailMessagesPrefs"]["InvoiceMessage"]["Message"]
    assert fake.prefs_bodies[0] == {"Id": "1", "SyncToken": "12", "sparse": True,
                                    "EmailMessagesPrefs": {"InvoiceMessage": {"Subject": SUBJECT, "Message": message}}}
    assert fake.prefs_bodies[1]["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] == ORIGINAL


async def test_template_edited_by_a_person_during_the_run_is_adopted(swap_client, load_fixture, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    fake = FakeQuickBooks(load_fixture)
    raws = invoices(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        fake.mount(router)
        await swap_client.send_invoice(make_inv(raws[0]), subject=SUBJECT)
        fake.set_subject("Your invoice from Example Forwarding")         # a person edits the template
        fake.prefs["SyncToken"] = str(int(fake.prefs["SyncToken"]) + 1)
        await swap_client.send_invoice(make_inv(raws[1]), subject=SUBJECT_B)
    # the 5010 re-read revealed the person's subject: it is adopted and restored after the send (04b §6.2 3.1)
    assert fake.kinds()[4:] == ["POST prefs", "GET prefs", "POST prefs", "POST send", "POST prefs"]
    assert fake.events[-1] == ("POST prefs", "Your invoice from Example Forwarding")
    assert fake.subject == "Your invoice from Example Forwarding"
    assert any("was changed during this run" in m for m in caplog.messages)
    emailer = swap_client.emailer
    assert isinstance(emailer, TemplateSwapEmailer)
    assert emailer.original is not None and emailer.original.subject == "Your invoice from Example Forwarding"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# graph / smtp
# ════════════════════════════════════════════════════════════════════════════════════════════════
def mailbox_invoice(load_fixture: Callable[[str], Any], **changes: Any) -> InvoiceSnapshot:
    raw = invoices(load_fixture)[0]
    raw.update({"BillEmail": {"Address": "ap@acme.example, ops@acme.example; AP@acme.example"},
                "BillEmailCc": {"Address": "cc@acme.example"},
                "BillEmailBcc": {"Address": "bcc@acme.example; archive@example.com"}})
    raw.update(changes)
    return make_inv(raw)


def mount_mailbox_qbo(router: respx.MockRouter, load_fixture: Callable[[str], Any], *,
                      mark_response: httpx.Response | None = None) -> dict[str, respx.Route]:
    marked = copy.deepcopy(invoices(load_fixture)[0])
    marked.update({"SyncToken": "8", "EmailStatus": "EmailSent"})
    return {
        "info": router.get(f"{COMPANY}/companyinfo/{REALM}").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/companyinfo.json"))),
        "prefs": router.get(f"{COMPANY}/preferences").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/preferences.json"))),
        "pdf": router.route(method="GET", path__regex=r"^/v3/company/\d+/invoice/\d+/pdf$").mock(
            return_value=httpx.Response(200, content=PDF, headers={"Content-Type": "application/pdf"})),
        "link": router.route(method="GET", path__regex=r"^/v3/company/\d+/invoice/\d+$").mock(
            return_value=httpx.Response(200, json={"Invoice": {"Id": "190613", "InvoiceLink": LINK}})),
        "mark": router.post(INVOICE_URL).mock(
            return_value=mark_response or httpx.Response(200, json={"Invoice": marked, "time": "t"})),
    }


def graph_client(make_settings, state_dir, token_store, **env: Any) -> QboClient:
    write_verified(state_dir, "graph")
    return make_client(make_settings({**GRAPH_ENV, **env}), state_dir, token_store)


# ── test 11 ──
async def test_graph_payload_token_cache_and_mark_sent(make_settings, state_dir, token_store, load_fixture) -> None:
    client = graph_client(make_settings, state_dir, token_store)
    inv = mailbox_invoice(load_fixture)
    with respx.mock(assert_all_called=False) as router:
        token = router.post(GRAPH_TOKEN_URL).mock(return_value=httpx.Response(200, json=load_fixture("graph/token.json")))
        send = router.post(GRAPH_SEND_URL).mock(return_value=httpx.Response(202))
        qbo = mount_mailbox_qbo(router, load_fixture)
        receipt = await client.send_invoice(inv, subject=SUBJECT)
        await client.send_invoice(inv, subject=SUBJECT_B)
        assert token.call_count == 1                                    # cached across sends
        assert parse_form(token.calls.last.request) == {
            "client_id": ["graph-client"], "client_secret": ["graph-secret"],
            "scope": ["https://graph.microsoft.com/.default"], "grant_type": ["client_credentials"]}
        assert send.call_count == 2
        request = send.calls[0].request
        assert request.headers["Authorization"] == "Bearer GRAPH-AT"
        payload = json.loads(request.content)
        assert qbo["mark"].call_count == 2
    message = payload["message"]
    assert payload["saveToSentItems"] is True
    assert message["subject"] == SUBJECT
    assert message["toRecipients"] == [{"emailAddress": {"address": "ap@acme.example"}},
                                       {"emailAddress": {"address": "ops@acme.example"}}]
    assert message["ccRecipients"] == [{"emailAddress": {"address": "cc@acme.example"}}]
    assert message["bccRecipients"] == [{"emailAddress": {"address": "bcc@acme.example"}},
                                        {"emailAddress": {"address": "archive@example.com"}}]
    assert message["replyTo"] == [{"emailAddress": {"address": "accounting@example.com"}}]
    attachment = message["attachments"][0]
    assert attachment == {"@odata.type": "#microsoft.graph.fileAttachment", "name": "Invoice_EBKG12345678.pdf",
                          "contentType": "application/pdf", "contentBytes": attachment["contentBytes"]}
    assert base64.b64decode(attachment["contentBytes"]) == PDF
    assert message["internetMessageHeaders"] == [{"name": "x-eta-invoice-id", "value": "190613"},
                                                 {"name": "x-eta-doc-number", "value": "EBKG12345678"}]
    assert message["body"]["contentType"] == "Text"
    assert message["body"]["content"] == ("Dear Acme Imports LLC,\n\nHere's your invoice! We appreciate your prompt "
                                          "payment.\n\nThanks for your business!\nExample Forwarding LLC"
                                          f"\n\nView and pay your invoice online:\n{LINK}\n")
    assert receipt.strategy == "graph" and receipt.qbo_marked_sent is True and receipt.sent_at == RUN_NOW
    assert receipt.to == ("ap@acme.example", "ops@acme.example") and receipt.cc == ("cc@acme.example",)
    assert receipt.bcc == ("bcc@acme.example", "archive@example.com")
    stored = token_store.get("graph")
    assert stored is not None and stored["access_token"] == "GRAPH-AT"
    await client.aclose()


def parse_form(request: httpx.Request) -> dict[str, list[str]]:
    from urllib.parse import parse_qs
    return parse_qs(request.content.decode())


async def test_graph_read_timeout_is_never_retried(make_settings, state_dir, token_store, load_fixture) -> None:
    client = graph_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        router.post(GRAPH_TOKEN_URL).mock(return_value=httpx.Response(200, json=load_fixture("graph/token.json")))
        send = router.post(GRAPH_SEND_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
        qbo = mount_mailbox_qbo(router, load_fixture)
        with pytest.raises(InvoiceSendError) as info:
            await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        assert send.call_count == 1
        assert qbo["mark"].call_count == 0
    assert str(info.value) == ("e-mail outcome unknown — check the Sent Items of accounting@example.com before "
                               "re-sending")


async def test_graph_429_with_retry_after_is_retried(make_settings, state_dir, token_store, load_fixture,
                                                     http_sleeps) -> None:
    client = graph_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        router.post(GRAPH_TOKEN_URL).mock(return_value=httpx.Response(200, json=load_fixture("graph/token.json")))
        send = router.post(GRAPH_SEND_URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": "2"}),
                                                             httpx.Response(202)])
        mount_mailbox_qbo(router, load_fixture)
        receipt = await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        assert send.call_count == 2
    assert http_sleeps == [2.0]
    assert receipt.strategy == "graph"


async def test_graph_credentials_rejected_blocks_the_email_path(make_settings, state_dir, token_store,
                                                                load_fixture) -> None:
    client = graph_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        router.post(GRAPH_TOKEN_URL).mock(return_value=httpx.Response(
            401, json={"error": "invalid_client",
                       "error_description": "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: x"}))
        mount_mailbox_qbo(router, load_fixture)
        with pytest.raises(EmailPathUnavailableError) as info:
            await client.prepare_for_sending()
    assert str(info.value) == ("Microsoft Graph rejected EMAIL_GRAPH_* credentials: AADSTS7000215: Invalid client "
                               "secret provided.")
    assert "graph-secret" not in str(info.value)


async def test_graph_token_failure_at_send_time_is_a_clear_not_sent(make_settings, state_dir, token_store,
                                                                     load_fixture, http_sleeps) -> None:
    client = graph_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        token = router.post(GRAPH_TOKEN_URL).mock(return_value=httpx.Response(200, json=load_fixture("graph/token.json")))
        send = router.post(GRAPH_SEND_URL).mock(return_value=httpx.Response(202))
        mount_mailbox_qbo(router, load_fixture)
        await client.prepare_for_sending()
        emailer = client.emailer
        assert isinstance(emailer, MailboxEmailer) and isinstance(emailer.transport, GraphTransport)
        await emailer.transport._auth.invalidate()                     # the cached token has expired
        token.mock(return_value=httpx.Response(500, text="AAD down"))
        with pytest.raises(InvoiceSendError) as info:
            await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        assert send.call_count == 0
    assert str(info.value).startswith("graph: e-mail not sent (no Microsoft Graph token)")


async def test_graph_403_blocks_the_email_path(make_settings, state_dir, token_store, load_fixture) -> None:
    client = graph_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        router.post(GRAPH_TOKEN_URL).mock(return_value=httpx.Response(200, json=load_fixture("graph/token.json")))
        router.post(GRAPH_SEND_URL).mock(return_value=httpx.Response(403, json={"error": {"code": "ErrorAccessDenied"}}))
        mount_mailbox_qbo(router, load_fixture)
        with pytest.raises(EmailPathUnavailableError, match="Microsoft Graph refused to send as accounting@example.com"):
            await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)


async def test_graph_pdf_problems_never_send(make_settings, state_dir, token_store, load_fixture) -> None:
    client = graph_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        router.post(GRAPH_TOKEN_URL).mock(return_value=httpx.Response(200, json=load_fixture("graph/token.json")))
        send = router.post(GRAPH_SEND_URL).mock(return_value=httpx.Response(202))
        qbo = mount_mailbox_qbo(router, load_fixture)
        qbo["pdf"].mock(return_value=httpx.Response(200, content=b"%PDF" + b"0" * (3 * 1024 * 1024)))
        with pytest.raises(InvoiceSendError, match="^PDF too large for a Graph inline attachment$"):
            await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        qbo["pdf"].mock(return_value=httpx.Response(200, content=b"<html>error</html>"))
        with pytest.raises(InvoiceSendError, match="could not download the invoice PDF from QuickBooks"):
            await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        assert send.call_count == 0


async def test_mailbox_verification_gate(make_settings, state_dir, token_store, load_fixture) -> None:
    client = make_client(make_settings(GRAPH_ENV), state_dir, token_store)
    with respx.mock(assert_all_called=False) as router, pytest.raises(EmailPathUnavailableError) as info:
        await client.prepare_for_sending()
    assert str(info.value) == ("The graph e-mail method has not been verified for this company. Run `eta probe qbo "
                               "--send-test --invoice <test invoice>` (04b §10).")
    with respx.mock(assert_all_called=False) as router:
        mount_mailbox_qbo(router, load_fixture)
        await client.prepare_for_sending(dry_run=True)                  # no token call in a dry run
        assert router.calls.call_count == 2                              # CompanyInfo + Preferences only


# ── test 12 ──
def fake_smtp_class(*, fail_login: bool = False, send_error: Exception | None = None) -> type:
    class FakeSMTP:
        instances: list[FakeSMTP] = []

        def __init__(self, host: str, port: int, timeout: float | None = None) -> None:
            self.calls: list[tuple[Any, ...]] = [("connect", host, port, timeout)]
            self.sent: list[tuple[Any, list[str]]] = []
            FakeSMTP.instances.append(self)

        def starttls(self, *, context: Any = None) -> None:
            self.calls.append(("starttls", type(context).__name__))

        def login(self, user: str, password: str) -> None:
            self.calls.append(("login", user, password))
            if fail_login:
                raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")

        def send_message(self, msg: Any, from_addr: str | None = None, to_addrs: Any = None) -> dict[str, Any]:
            self.calls.append(("send_message",))
            if send_error is not None:
                raise send_error
            self.sent.append((msg, list(to_addrs)))
            return {}

        def quit(self) -> None:
            self.calls.append(("quit",))

        def close(self) -> None:
            self.calls.append(("close",))

    return FakeSMTP


def smtp_client(make_settings, state_dir, token_store, **env: Any) -> QboClient:
    write_verified(state_dir, "smtp")
    return make_client(make_settings({**SMTP_ENV, **env}), state_dir, token_store)


async def test_smtp_message_headers_bcc_attachment_starttls_and_login(make_settings, state_dir, token_store,
                                                                      load_fixture, monkeypatch) -> None:
    fake_smtp = fake_smtp_class()
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)
    client = smtp_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        mount_mailbox_qbo(router, load_fixture)
        receipt = await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
    verify_conn, send_conn = fake_smtp.instances
    assert verify_conn.calls == [("connect", "smtp.example.com", 587, 30), ("starttls", "SSLContext"),
                                 ("login", "mailer", "smtp-pass"), ("quit",)]
    assert send_conn.calls == [("connect", "smtp.example.com", 587, 30), ("starttls", "SSLContext"),
                               ("login", "mailer", "smtp-pass"), ("send_message",), ("quit",)]
    msg, to_addrs = send_conn.sent[0]
    assert msg["From"] == "accounting@example.com"
    assert msg["To"] == "ap@acme.example, ops@acme.example"
    assert msg["Cc"] == "cc@acme.example"
    assert msg["Reply-To"] == "billing@example.com"
    assert msg["Subject"] == SUBJECT
    assert msg["Bcc"] is None
    assert msg["x-eta-invoice-id"] == "190613" and msg["x-eta-doc-number"] == "EBKG12345678"
    assert msg["Message-ID"].endswith("@example.com>")
    assert to_addrs == ["ap@acme.example", "ops@acme.example", "cc@acme.example", "bcc@acme.example",
                        "archive@example.com"]
    (attachment,) = list(msg.iter_attachments())
    assert attachment.get_filename() == "Invoice_EBKG12345678.pdf"
    assert attachment.get_content_type() == "application/pdf"
    assert attachment.get_content() == PDF
    assert msg.get_body(preferencelist=("plain",)).get_content().startswith("Dear Acme Imports LLC,\n")
    assert receipt.strategy == "smtp" and receipt.bcc == ("bcc@acme.example", "archive@example.com")


async def test_smtp_ssl_mode_and_rejected_login(make_settings, state_dir, token_store, load_fixture,
                                                monkeypatch) -> None:
    calls: list[tuple[Any, ...]] = []

    class FakeSSL:
        def __init__(self, host: str, port: int, timeout: float | None = None, context: Any = None) -> None:
            calls.append(("SMTP_SSL", host, port, timeout, type(context).__name__))

        def login(self, user: str, password: str) -> None:
            raise smtplib.SMTPAuthenticationError(535, b"5.7.8 Authentication failed")

        def quit(self) -> None:
            calls.append(("quit",))

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSSL)
    client = smtp_client(make_settings, state_dir, token_store, EMAIL_SMTP_SECURITY="ssl", EMAIL_SMTP_PORT="465")
    with respx.mock(assert_all_called=False) as router:
        mount_mailbox_qbo(router, load_fixture)
        with pytest.raises(EmailPathUnavailableError) as info:
            await client.prepare_for_sending()
    assert calls == [("SMTP_SSL", "smtp.example.com", 465, 30, "SSLContext"), ("quit",)]
    assert str(info.value) == "The SMTP server rejected EMAIL_SMTP_USERNAME / EMAIL_SMTP_PASSWORD (SMTP 535)"
    assert "smtp-pass" not in str(info.value)


@pytest.mark.parametrize(("error", "expected"), [
    (smtplib.SMTPServerDisconnected("lost"),
     "e-mail outcome unknown — check the Sent Items of accounting@example.com before re-sending"),
    (smtplib.SMTPRecipientsRefused({"ap@acme.example": (550, b"no such user")}),
     "smtp: e-mail not sent (rejected by the server)"),
])
async def test_smtp_send_errors(make_settings, state_dir, token_store, load_fixture, monkeypatch, error: Exception,
                                expected: str) -> None:
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp_class(send_error=error))
    client = smtp_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        qbo = mount_mailbox_qbo(router, load_fixture)
        with pytest.raises(InvoiceSendError) as info:
            await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        assert qbo["mark"].call_count == 0
    assert str(info.value).startswith(expected)


# ── test 13 ──
def test_render_body_placeholders_unknown_tokens_and_pay_link(load_fixture) -> None:
    inv = make_inv(invoices(load_fixture)[0])
    unknown: list[str] = []
    template = ("Dear [Customer Name], [customer]\n[Invoice No.] / [INVOICE NUMBER] amount [Invoice Amount] balance "
                "[Balance Due] due [Due Date] [Foo] [Company  Name]")
    body = render_body(template, inv, company_name="Example Forwarding LLC", link=LINK, on_unknown=unknown.append)
    assert body == ("Dear Acme Imports LLC, Acme Imports LLC\nEBKG12345678 / EBKG12345678 amount $2,450.00 balance "
                    "$2,450.00 due 11/19/2026 [Foo] Example Forwarding LLC"
                    f"\n\nView and pay your invoice online:\n{LINK}\n")
    assert unknown == ["[Foo]"]
    with_link = render_body("Pay here: [Invoice Link]", inv, company_name=None, link=LINK)
    assert with_link == f"Pay here: {LINK}" and with_link.count(LINK) == 1
    assert render_body("Pay here: [Invoice Link]", inv, company_name=None, link=None) == "Pay here: "
    assert render_body(DEFAULT_BODY_TEMPLATE, inv, company_name=None, link=None).endswith("business!\n")


async def test_unknown_placeholder_is_warned_once_per_run(make_settings, state_dir, token_store, tmp_path,
                                                          load_fixture, monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    fake_smtp = fake_smtp_class()
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)
    template = tmp_path / "body.txt"
    template.write_text("Hello [Customer], see [Mystery Token].\n[Invoice Link]", encoding="utf-8")
    client = smtp_client(make_settings, state_dir, token_store, EMAIL_BODY_TEMPLATE=str(template))
    with respx.mock(assert_all_called=False) as router:
        mount_mailbox_qbo(router, load_fixture)
        await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT_B)
    assert [m for m in caplog.messages if "[Mystery Token]" in m] == [
        "Unknown placeholder [Mystery Token] in the invoice e-mail body — left as is"]
    msg, _ = fake_smtp.instances[-1].sent[0]
    assert msg.get_body(preferencelist=("plain",)).get_content() == (
        f"Hello Acme Imports LLC, see [Mystery Token].\n{LINK}\n")


async def test_missing_body_template_file_is_a_config_error(make_settings, state_dir, token_store, tmp_path,
                                                             monkeypatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp_class())
    client = smtp_client(make_settings, state_dir, token_store, EMAIL_BODY_TEMPLATE=str(tmp_path / "missing.txt"))
    with respx.mock(assert_all_called=False), pytest.raises(ConfigError, match="EMAIL_BODY_TEMPLATE file not found"):
        await client.prepare_for_sending()


# ── test 14 ──
async def test_mark_sent_sparse_body_and_failure_is_only_a_warning(make_settings, state_dir, token_store,
                                                                   load_fixture, monkeypatch, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp_class())
    client = smtp_client(make_settings, state_dir, token_store)
    with respx.mock(assert_all_called=False) as router:
        qbo = mount_mailbox_qbo(router, load_fixture)
        ok = await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        request = qbo["mark"].calls.last.request
        assert json.loads(request.content) == {"Id": "190613", "SyncToken": "7", "sparse": True,
                                               "EmailStatus": "EmailSent"}
        assert len(request.url.params["requestid"]) == 32
        assert ok.qbo_marked_sent is True
        qbo["mark"].mock(return_value=httpx.Response(400, json=fault("2010", "Unsupported property")))
        failed = await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        assert failed.qbo_marked_sent is False
        calls = qbo["mark"].call_count
        already = await client.send_invoice(mailbox_invoice(load_fixture, EmailStatus="EmailSent"), subject=SUBJECT)
        assert qbo["mark"].call_count == calls and already.qbo_marked_sent is False
    assert any(m.startswith("could not mark invoice 190613 (#EBKG12345678) as sent in QuickBooks (UNVERIFIED "
                            "feature)") for m in caplog.messages)


async def test_mailbox_redirect_mode(make_settings, state_dir, token_store, load_fixture, monkeypatch) -> None:
    fake_smtp = fake_smtp_class()
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)
    client = smtp_client(make_settings, state_dir, token_store, QB_EMAIL_REDIRECT_TO="shadow@example.com")
    with respx.mock(assert_all_called=False) as router:
        qbo = mount_mailbox_qbo(router, load_fixture)
        receipt = await client.send_invoice(mailbox_invoice(load_fixture), subject=SUBJECT)
        assert qbo["mark"].call_count == 0                              # the customer was not e-mailed
    msg, to_addrs = fake_smtp.instances[-1].sent[0]
    assert to_addrs == ["shadow@example.com"] and msg["Cc"] is None
    assert msg.get_body(preferencelist=("plain",)).get_content().startswith(
        "[TEST REDIRECT] This e-mail would have been sent to: ap@acme.example, ops@acme.example; cc: cc@acme.example"
        "\n\nDear Acme Imports LLC,")
    assert receipt.to == ("shadow@example.com",) and receipt.cc == () and receipt.bcc == ()
    assert receipt.redirected_to == "shadow@example.com"


# ── test 15 ──
async def test_empty_bill_email_or_multiline_subject_fail_before_any_http(swap_client, load_fixture) -> None:
    no_email = make_inv(dict(invoices(load_fixture)[0], BillEmail={"Address": ""}))
    inv = make_inv(invoices(load_fixture)[0])
    with respx.mock(assert_all_called=False) as router:
        with pytest.raises(InvoiceSendError) as missing:
            await swap_client.send_invoice(no_email, subject=SUBJECT)
        assert str(missing.value) == "Invoice has no customer e-mail address (BillEmail)"
        for bad in (f"{SUBJECT}\nBcc: evil@example.com", "line one\rline two", "   ", ""):
            with pytest.raises(InvoiceSendError) as invalid:
                await swap_client.send_invoice(inv, subject=bad)
            assert str(invalid.value) == "invalid subject"
        assert router.calls.call_count == 0


def test_validate_send_strips_and_refuses_read_only(make_settings, state_dir, load_fixture) -> None:
    inv = make_inv(invoices(load_fixture)[0])
    client = make_client(make_settings(), state_dir)
    assert validate_send(client, inv, f"  {SUBJECT}\n") == SUBJECT
    with pytest.raises(RuntimeError, match="read-only"):
        validate_send(make_client(make_settings(), state_dir, read_only=True), inv, SUBJECT)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Factory and small helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_build_emailer_selects_the_strategy(make_settings, state_dir, token_store) -> None:
    swap = build_emailer(make_client(make_settings(), state_dir))
    assert isinstance(swap, TemplateSwapEmailer) and swap.name == "template_swap"
    graph = build_emailer(make_client(make_settings(GRAPH_ENV), state_dir, token_store))
    assert isinstance(graph, MailboxEmailer) and graph.name == "graph"
    assert isinstance(graph.transport, GraphTransport)
    smtp = build_emailer(make_client(make_settings(SMTP_ENV), state_dir, token_store))
    assert isinstance(smtp, MailboxEmailer) and isinstance(smtp.transport, SmtpTransport)
    incomplete = make_settings({"QB_EMAIL_STRATEGY": "graph", "EMAIL_FROM": "a@example.com"})
    with pytest.raises(ConfigError) as info:
        build_emailer(make_client(incomplete, state_dir))
    assert str(info.value) == ("QB_EMAIL_STRATEGY=graph needs these settings in .env: EMAIL_GRAPH_TENANT_ID, "
                               "EMAIL_GRAPH_CLIENT_ID, EMAIL_GRAPH_CLIENT_SECRET (04b §3)")
    no_state = QboClient(make_settings().qbo, StaticAuth())
    with pytest.raises(ConfigError, match="state_dir"):
        build_emailer(no_state)


def test_pdf_file_name_and_mime_message() -> None:
    assert pdf_file_name("EBKG12345678 A") == "Invoice_EBKG12345678_A.pdf"
    assert pdf_file_name("A/B$C  D.e-f") == "Invoice_A_B_C_D.e-f.pdf"
    mail = OutgoingMail(sender="a@example.com", to=("b@example.com",), cc=(), bcc=("c@example.com",),
                        reply_to=None, subject="S", text_body="Body", attachments=(), headers={})
    msg = build_mime_message(mail)
    assert msg["Bcc"] is None and msg["Cc"] is None and msg["Reply-To"] is None
    payload = graph_payload(mail)
    assert "ccRecipients" not in payload["message"] and "attachments" not in payload["message"]
    assert payload["message"]["bccRecipients"] == [{"emailAddress": {"address": "c@example.com"}}]
