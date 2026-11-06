"""In-memory fakes for the pipeline tests (12-testing §1–§2, 10 §10).

FakeQbo, FakeVShip and FakeCarrier expose the same method names as QboClient, VShipClient and the 03 §9
CarrierClient, and record every call in one shared `CallLog`, so a test can assert the exact order of the
writes a row performed. The real CarrierRouter is used over the fake carriers (route(), run memo and circuit
breaker are therefore the production code). `FakeWorld` bundles everything and builds a `Services` for
`pipeline.run(..., services=...)`; `fake_build_services(world)` has the `pipeline.build_services` signature
for tests that monkeypatch it (12-testing §6). Production code never imports this module.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eta_automations.carriers.router import CarrierRouter, identify_carrier
from eta_automations.clock import Clock, FixedClock
from eta_automations.config import Settings
from eta_automations.errors import CarrierLookupError, ConfigError, VShipBookingNotFoundError
from eta_automations.ledger import EMAILED, LEDGER_FILE_NAME, Ledger
from eta_automations.models import Carrier, EtaResult
from eta_automations.outputs import OutputWriter
from eta_automations.pipeline import Services
from eta_automations.qbo.client import READ_ONLY_MESSAGE as QBO_READ_ONLY
from eta_automations.qbo.client import InvoiceSnapshot, split_addresses
from eta_automations.qbo.email import SendReceipt
from eta_automations.rules import classify, parse_row
from eta_automations.token_store import TokenStore
from eta_automations.vship.client import READ_ONLY_MESSAGE as VSHIP_READ_ONLY

__all__ = [
    "DEFAULT_EMAIL",
    "WRITE_METHODS",
    "Booking",
    "CallLog",
    "FakeAuth",
    "FakeCarrier",
    "FakeMscCarrier",
    "FakeQbo",
    "FakeVShip",
    "FakeWorld",
    "UnwritableTokenStore",
    "eta_result",
    "fake_build_services",
    "make_invoice",
]

DEFAULT_EMAIL = "billing@acme.example"
WRITE_METHODS = frozenset({"qbo.update_invoice", "qbo.send_invoice", "vship.post_comment",
                           "vship.patch_expected_arrival"})

Hook = Callable[..., Awaitable[None]]


class UnwritableTokenStore(TokenStore):
    """tokens.json that can be read but not written: a full disk, missing permissions, or a Windows sharing violation
    while another program has the file open (review fixes MSK-05 / CRA-05). Seed it with a plain TokenStore on the
    same path."""

    def _write(self, data: Mapping[str, Any]) -> None:
        raise PermissionError(13, "Permission denied", str(self.path))


class CallLog(list[tuple[Any, ...]]):
    """Every fake call as a tuple `(service.method, *args)`, in call order. Each entry also has an owner
    (the invoice id of a QBO call, the bookingId of a VShip call) so the writes of one row can be picked out
    while rows run concurrently."""

    def __init__(self) -> None:
        super().__init__()
        self.owners: list[str | None] = []

    def append(self, entry: tuple[Any, ...], owner: str | None = None) -> None:  # type: ignore[override]
        super().append(entry)
        self.owners.append(owner)

    def clear(self) -> None:
        super().clear()
        self.owners.clear()

    def named(self, *names: str) -> list[tuple[Any, ...]]:
        return [c for c in self if c[0] in names]

    def writes(self, *owners: str | None) -> list[tuple[Any, ...]]:
        """Write calls in order; with owners, only those of these invoice ids / bookingIds."""
        return [c for c, owner in zip(self, self.owners, strict=True)
                if c[0] in WRITE_METHODS and (not owners or owner in owners)]


class FakeAuth:
    """TokenProvider stand-in for the Phase 0 authentication check (`await auth.get_token()`)."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.calls = 0

    async def get_token(self) -> str:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "fake-token"

    async def invalidate(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


def make_invoice(doc_number: str, *, invoice_id: str, total: Decimal | str = "5000",
                 balance: Decimal | str | None = None,
                 bill_email: str | None = DEFAULT_EMAIL, customer: str | None = "ACME Imports",
                 bill_email_cc: str | None = None, eta: date | None = None) -> InvoiceSnapshot:
    """A real InvoiceSnapshot parsed from an Invoice JSON object (04 §6.2), so is_paid / is_partially_paid /
    recipients behave exactly as in production."""
    total_d = Decimal(str(total))
    balance_d = total_d if balance is None else Decimal(str(balance))
    raw: dict[str, Any] = {
        "Id": invoice_id,
        "SyncToken": "0",
        "DocNumber": doc_number,
        "TotalAmt": str(total_d),
        "Balance": str(balance_d),
        "CustomerRef": {"value": "58", "name": customer},
        "EmailStatus": "NotSet",
        "CustomField": [],
    }
    if bill_email is not None:
        raw["BillEmail"] = {"Address": bill_email}
    if bill_email_cc is not None:
        raw["BillEmailCc"] = {"Address": bill_email_cc}
    snapshot = InvoiceSnapshot.from_api(raw, None)
    return dataclasses.replace(snapshot, eta_value=eta) if eta is not None else snapshot


def eta_result(carrier: Carrier, booking: str, eta: datetime, **kwargs: Any) -> EtaResult:
    return EtaResult(carrier=carrier, booking=booking, eta=eta, source=f"fake {carrier} event", **kwargs)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# QuickBooks
# ════════════════════════════════════════════════════════════════════════════════════════════════
class FakeQbo:
    """QboClient fake: find_invoices_by_doc_numbers / ambiguous / update_invoice / prepare_for_sending /
    recover_email_template / send_invoice / aclose, plus `auth.get_token()`. Writes raise RuntimeError when
    read_only (dry run)."""

    def __init__(self, calls: CallLog, *, clock: Clock, read_only: bool = False) -> None:
        self.calls = calls
        self.clock = clock
        self.read_only = read_only
        self.auth = FakeAuth()
        self.invoices: dict[str, InvoiceSnapshot] = {}          # DocNumber -> snapshot
        self.ambiguous: dict[str, tuple[str, ...]] = {}
        self.lookup_error: BaseException | None = None
        self.prepare_error: BaseException | None = None
        self.recover_error: BaseException | None = None          # recover_email_template() raises this
        self.update_errors: dict[str, BaseException] = {}       # invoice id -> error on every update
        self.revert_errors: dict[str, BaseException] = {}       # invoice id -> error when the DocNumber goes back
        self.send_errors: dict[str, BaseException] = {}         # invoice id -> error on send
        self.before_write: Hook | None = None                   # awaited at the start of every write
        self.write_attempts: list[str] = []                     # also records refused (read-only) writes
        self.closed = 0
        self._original_docs: dict[str, str] = {}

    def add_invoice(self, invoice: InvoiceSnapshot) -> InvoiceSnapshot:
        self.invoices[invoice.doc_number] = invoice
        self._original_docs[invoice.id] = invoice.doc_number
        return invoice

    def _writable(self, method: str) -> None:
        self.write_attempts.append(method)
        if self.read_only:
            raise RuntimeError(QBO_READ_ONLY)

    async def find_invoices_by_doc_numbers(self, doc_numbers: Sequence[str]) -> dict[str, InvoiceSnapshot]:
        wanted = [d.strip() for d in doc_numbers]
        self.calls.append(("qbo.find_invoices_by_doc_numbers", tuple(wanted)))
        if self.lookup_error is not None:
            raise self.lookup_error
        return {d: self.invoices[d] for d in wanted if d in self.invoices and d not in self.ambiguous}

    async def update_invoice(self, inv: InvoiceSnapshot, *, eta: date | None = None,
                             doc_number: str | None = None) -> InvoiceSnapshot:
        self._writable("qbo.update_invoice")
        self.calls.append(("qbo.update_invoice", inv.doc_number, eta, doc_number), inv.id)
        if self.before_write is not None:
            await self.before_write("qbo.update_invoice", inv)
        reverting = doc_number is not None and doc_number == self._original_docs.get(inv.id) != inv.doc_number
        error = self.revert_errors.get(inv.id) if reverting else self.update_errors.get(inv.id)
        if error is not None:
            raise error
        if eta is None and doc_number is None:
            return inv
        updated = dataclasses.replace(
            inv,
            doc_number=doc_number if doc_number is not None else inv.doc_number,
            eta_value=eta if eta is not None else inv.eta_value,
            sync_token=str(int(inv.sync_token or "0") + 1),
        )
        return updated

    async def prepare_for_sending(self, *, dry_run: bool = False) -> None:
        self.calls.append(("qbo.prepare_for_sending", dry_run))
        if self.prepare_error is not None:
            raise self.prepare_error

    async def recover_email_template(self) -> bool:
        self._writable("qbo.recover_email_template")
        self.calls.append(("qbo.recover_email_template",))
        if self.recover_error is not None:
            raise self.recover_error
        return True

    async def send_invoice(self, inv: InvoiceSnapshot, *, subject: str) -> SendReceipt:
        self._writable("qbo.send_invoice")
        self.calls.append(("qbo.send_invoice", inv.doc_number, subject), inv.id)
        if self.before_write is not None:
            await self.before_write("qbo.send_invoice", inv)
        error = self.send_errors.get(inv.id)
        if error is not None:
            raise error
        return SendReceipt(strategy="template_swap", invoice_id=inv.id, doc_number=inv.doc_number, subject=subject,
                           to=inv.recipients, cc=split_addresses(inv.bill_email_cc), sent_at=self.clock.now(),
                           qbo_delivery_time="2026-11-05T09:31:00-05:00")

    async def aclose(self) -> None:
        self.closed += 1


# ════════════════════════════════════════════════════════════════════════════════════════════════
# VShip
# ════════════════════════════════════════════════════════════════════════════════════════════════
class FakeVShip:
    """VShipClient fake: find_booking_id / get_customer_name / get_internal_comments / post_comment /
    patch_expected_arrival / aclose, plus `auth.get_token()`. Writes raise RuntimeError when read_only."""

    def __init__(self, calls: CallLog, *, read_only: bool = False) -> None:
        self.calls = calls
        self.read_only = read_only
        self.auth = FakeAuth()
        self.booking_ids: dict[str, str] = {}               # base booking -> bookingId
        self.customers: dict[str, str] = {}                 # base booking -> customerName
        self.comments: dict[str, list[dict[str, Any]]] = {}  # bookingId -> internalComments
        self.find_errors: dict[str, BaseException] = {}
        self.customer_errors: dict[str, BaseException] = {}
        self.comment_errors: dict[str, BaseException] = {}  # bookingId -> error reading comments
        self.post_errors: dict[str, BaseException] = {}     # bookingId -> error posting a note
        self.patch_errors: dict[str, BaseException] = {}    # bookingId -> error patching the ETA
        self.before_write: Hook | None = None
        self.write_attempts: list[str] = []
        self.closed = 0

    def _writable(self, method: str) -> None:
        self.write_attempts.append(method)
        if self.read_only:
            raise RuntimeError(VSHIP_READ_ONLY)

    async def find_booking_id(self, base_booking: str) -> str:
        self.calls.append(("vship.find_booking_id", base_booking))
        if base_booking in self.find_errors:
            raise self.find_errors[base_booking]
        if base_booking not in self.booking_ids:
            raise VShipBookingNotFoundError("vship", f"No VShip booking matches {base_booking!r} (candidates: none)")
        return self.booking_ids[base_booking]

    async def get_customer_name(self, base_booking: str) -> str:
        self.calls.append(("vship.get_customer_name", base_booking))
        if base_booking in self.customer_errors:
            raise self.customer_errors[base_booking]
        await self.find_booking_id(base_booking)
        return self.customers.get(base_booking, "")

    async def get_internal_comments(self, booking_id: str) -> list[dict[str, Any]]:
        self.calls.append(("vship.get_internal_comments", booking_id))
        if booking_id in self.comment_errors:
            raise self.comment_errors[booking_id]
        return [dict(c) for c in self.comments.get(booking_id, [])]

    async def post_comment(self, booking_id: str, text: str) -> None:
        self._writable("vship.post_comment")
        self.calls.append(("vship.post_comment", booking_id, text), booking_id)
        if self.before_write is not None:
            await self.before_write("vship.post_comment", booking_id)
        if booking_id in self.post_errors:
            raise self.post_errors[booking_id]
        self.comments.setdefault(booking_id, []).append({"comment": text, "createdDate": "2026-11-05T09:30:00"})

    async def patch_expected_arrival(self, booking_id: str, eta: datetime) -> None:
        self._writable("vship.patch_expected_arrival")
        # recorded as the V6 body value (05 §4): "YYYY-mm-ddTHH:MM:SS"
        self.calls.append(("vship.patch_expected_arrival", booking_id, eta.strftime("%Y-%m-%dT%H:%M:%S")),
                          booking_id)
        if self.before_write is not None:
            await self.before_write("vship.patch_expected_arrival", booking_id)
        if booking_id in self.patch_errors:
            raise self.patch_errors[booking_id]

    async def aclose(self) -> None:
        self.closed += 1


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Carriers
# ════════════════════════════════════════════════════════════════════════════════════════════════
class FakeCarrier:
    """03 §9 CarrierClient fake: `results[booking]` is an EtaResult or an exception to raise; an unknown
    booking raises CarrierLookupError(f"no record of booking {b}")."""

    def __init__(self, carrier: Carrier, calls: CallLog | None = None) -> None:
        self.carrier = carrier
        self.results: dict[str, EtaResult | BaseException] = {}
        self.behaviour: Callable[[str], EtaResult | BaseException] | None = None
        self.get_eta_calls: list[str] = []
        self.calls = calls
        self.closed = 0

    async def get_eta(self, booking: str) -> EtaResult:
        self.get_eta_calls.append(booking)
        if self.calls is not None:
            self.calls.append((f"carrier.{self.carrier.name.lower()}.get_eta", booking))
        if self.behaviour is not None:
            outcome = self.behaviour(booking)
        else:
            outcome = self.results.get(booking, CarrierLookupError(f"no record of booking {booking}"))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def probe(self, booking: str) -> dict[str, Any]:
        return {"requests": [], "raw": None, "eta": f"probe {booking}"}

    async def aclose(self) -> None:
        self.closed += 1


class FakeMscCarrier(FakeCarrier):
    """MSC fake with the bridge-token helpers the 10 Phase 1 pre-flight uses (06 §5.1)."""

    def __init__(self, calls: CallLog | None = None, *, mode: str = "mymsc", seconds_left: int | None = None) -> None:
        super().__init__(Carrier.MSC, calls)
        self.mode = mode
        self.seconds_left = seconds_left
        self.stored_tokens: list[str] = []
        self.status_calls = 0

    def token_status(self) -> dict[str, Any]:
        self.status_calls += 1
        if self.mode != "mymsc":
            return {"mode": "api", "host": "api.msc.example", "sandbox": False, "has_key": True}
        has_token = self.seconds_left is not None
        return {"mode": "mymsc", "has_token": has_token, "source": "manual" if has_token else None,
                "expires_at": None, "seconds_left": self.seconds_left, "jwt_claims": {}}

    def set_manual_token(self, token: str) -> dict[str, Any]:
        text = token.strip()
        if not text:
            raise ConfigError("The MSC access token is empty.")
        self.stored_tokens.append(text)
        self.seconds_left = 1800
        return self.token_status()


# ════════════════════════════════════════════════════════════════════════════════════════════════
# The world of one test
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class Booking:
    """What FakeWorld.add() registered for one input row."""

    line: str
    doc: str
    base: str | None
    carrier: Carrier | None
    token: str | None
    invoice_id: str | None
    vship_id: str | None


class FakeWorld:
    """FakeQbo + FakeVShip + one FakeCarrier per native carrier behind a real CarrierRouter."""

    def __init__(self, clock: Clock, *, failure_limit: int = 5, msc: FakeCarrier | None = None) -> None:
        self.clock = clock
        self.calls = CallLog()
        self.qbo = FakeQbo(self.calls, clock=clock)
        self.vship = FakeVShip(self.calls)
        self.carriers: dict[Carrier, FakeCarrier] = {
            Carrier.MSC: msc if msc is not None else FakeCarrier(Carrier.MSC, self.calls),
            Carrier.MAERSK: FakeCarrier(Carrier.MAERSK, self.calls),
            Carrier.CMA: FakeCarrier(Carrier.CMA, self.calls),
        }
        if self.carriers[Carrier.MSC].calls is None:
            self.carriers[Carrier.MSC].calls = self.calls
        self.failure_limit = failure_limit
        self.router: CarrierRouter | None = None
        self.ledger_seed: list[dict[str, Any]] = []
        self.services_built: list[Services] = []
        self._ids = itertools.count(1)

    # ── scenario building ──
    def add(self, doc: str, amount: str = "5000", *, eta: datetime | BaseException | None = None,
            comments: Iterable[Mapping[str, Any]] = (), invoice: str | None = "unpaid",
            bill_email: str | None = DEFAULT_EMAIL, bill_email_cc: str | None = None, vship: bool = True,
            customer: str = "ACME Imports", invoice_doc: str | None = None,
            details: Mapping[str, Any] | None = None) -> Booking:
        """Register one input row: carrier result for its query token, VShip booking + comments, QBO invoice
        (`invoice` = "unpaid" | "paid" | "partial" | None). Returns the Booking (its CSV line in .line)."""
        n = next(self._ids)
        parsed = classify(parse_row([doc, amount], 1))
        base = parsed.base_booking
        carrier: Carrier | None = None
        token: str | None = None
        vship_id: str | None = None
        if base is not None:
            try:
                carrier, token = identify_carrier(base)
            except CarrierLookupError:
                carrier, token = None, None
            if carrier is not None and token is not None and eta is not None and carrier in self.carriers:
                result: EtaResult | BaseException = (
                    eta if isinstance(eta, BaseException)
                    else eta_result(carrier, token, eta, details=dict(details or {})))
                self.carriers[carrier].results[token] = result
            if vship:
                vship_id = str(70000 + n)
                self.vship.booking_ids[base] = vship_id
                self.vship.customers[base] = customer
                self.vship.comments[vship_id] = [dict(c) for c in comments]
        invoice_id: str | None = None
        if invoice is not None:
            invoice_id = str(1000 + n)
            total = Decimal(amount) if _is_number(amount) else Decimal("5000")
            balance = {"unpaid": total, "paid": Decimal("0"), "partial": (total / 2).quantize(Decimal("0.01"))}[invoice]
            self.qbo.add_invoice(make_invoice(invoice_doc or doc, invoice_id=invoice_id, total=total, balance=balance,
                                              bill_email=bill_email, bill_email_cc=bill_email_cc, customer=customer))
        return Booking(line=f"{doc},{amount}", doc=doc, base=base, carrier=carrier, token=token,
                       invoice_id=invoice_id, vship_id=vship_id)

    def emailed_today(self, booking: Booking, at: datetime) -> None:
        """Seed the ledger with a successful e-mail for this booking's invoice (P28)."""
        self.ledger_seed.append({"booking": booking, "at": at})

    # ── services ──
    def make_router(self) -> CarrierRouter:
        native: dict[Carrier, Any] = dict(self.carriers)
        native[Carrier.GRIMALDI] = None
        return CarrierRouter(
            providers={Carrier.MSC: "native", Carrier.MAERSK: "native", Carrier.CMA: "native",
                       Carrier.GRIMALDI: "off"},
            native=native,
            native_missing={Carrier.MSC: (), Carrier.MAERSK: ("MAERSK_CLIENT_ID", "MAERSK_CLIENT_SECRET"),
                            Carrier.CMA: ("CMA_API_KEY", "CMA_CLIENT_ID", "CMA_CLIENT_SECRET")},
            failure_limit=self.failure_limit,
        )

    def ledger_path(self, settings: Settings) -> Path:
        return settings.run.state_dir / LEDGER_FILE_NAME

    def services(self, settings: Settings, *, read_only: bool = False, run_now: datetime | None = None) -> Services:
        """A Services bundle over the fakes; outputs and ledger are disabled when read_only (dry run)."""
        for seed in self.ledger_seed:
            booking: Booking = seed["booking"]
            at: datetime = seed["at"]
            Ledger(self.ledger_path(settings), clock=FixedClock(at)).record(
                run_id=at.strftime("%Y%m%d_%H%M%S"), doc_number=booking.doc, invoice_id=booking.invoice_id,
                base_booking=booking.base, action=EMAILED,
                detail={"strategy": "template_swap", "to": [DEFAULT_EMAIL], "sent_at": at.isoformat()}, ok=True)
        self.ledger_seed.clear()
        self.qbo.read_only = read_only
        self.vship.read_only = read_only
        self.router = self.make_router()
        services = Services(
            settings=settings,
            clock=FixedClock(run_now or self.clock.now()),
            token_store=TokenStore(settings.run.state_dir / "tokens.json"),
            qbo=self.qbo,  # type: ignore[arg-type]
            vship=self.vship,  # type: ignore[arg-type]
            router=self.router,
            outputs=OutputWriter(settings.run.output_dir, enabled=not read_only),
            ledger=Ledger(self.ledger_path(settings), clock=self.clock, enabled=not read_only),
        )
        self.services_built.append(services)
        return services

    def writes(self) -> list[tuple[Any, ...]]:
        return self.calls.writes()

    def row_writes(self, booking: Booking) -> list[tuple[Any, ...]]:
        """The writes of one row, in order (a row's actions run sequentially, 10 Phase 4)."""
        owners = [o for o in (booking.invoice_id, booking.vship_id) if o is not None]
        return self.calls.writes(*owners) if owners else []


def _is_number(text: str) -> bool:
    try:
        Decimal(text)
    except ArithmeticError:
        return False
    return True


def fake_build_services(world: FakeWorld) -> Callable[..., Any]:
    """A drop-in for `pipeline.build_services(settings, run_now, *, read_only)` yielding the world's fakes
    (for tests that monkeypatch it, e.g. the CLI's end-to-end dry run, 12-testing §6)."""

    @asynccontextmanager
    async def _build(settings: Settings, run_now: datetime, *, read_only: bool) -> AsyncIterator[Services]:
        services = world.services(settings, read_only=read_only, run_now=run_now)
        try:
            yield services
        finally:
            await asyncio.gather(world.qbo.aclose(), world.vship.aclose(), services.router.aclose())

    return _build
