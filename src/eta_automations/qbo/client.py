"""QuickBooks Online Accounting API client (04-quickbooks-online §6).

Replaces the QuickBooks web-UI automation of the original: batched invoice lookup by DocNumber, sparse
invoice updates of the ETA custom field and the DocNumber (with requestid idempotency, stale-object retry and
post-write verification), the invoice e-mail via the configured `qbo/email.py` strategy (04b), and the
low-level helpers 04b uses. Every request goes through `_call()`: QBO semaphore + per-second/per-minute
limiter + `auth.request_with_auth()` + `minorversion`.

No browser, no private web-UI endpoints (04 §8.4). Business decisions live in the pipeline (10); this client
only makes each step reproducible through the API (04 §1).
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Collection, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

import httpx

from ..auth import request_with_auth
from ..clock import Clock, SystemClock
from ..config import QBO_REQUIRED, EmailSettings, QboSettings, Settings
from ..errors import (
    AmbiguousInvoiceError,
    AuthError,
    ConfigError,
    InvoiceNotFoundError,
    InvoiceSendError,
    InvoiceUpdateError,
    ServiceError,
)
from ..http import AsyncLimiter, json_body, new_async_client, request_id_from
from ..token_store import TokenStore
from .auth import QboAuth

if TYPE_CHECKING:
    from .email import InvoiceEmailer, SendReceipt, TemplateSwapEmailer

__all__ = [
    "EtaFieldSpec",
    "InvoiceSnapshot",
    "MultiLimiter",
    "QboClient",
    "QboFault",
    "QboFaultError",
    "READ_ONLY_MESSAGE",
    "ambiguous_invoice_error",
    "build_qbo",
    "doc_number_problem",
    "parse_faults",
    "parse_qbo_date",
    "split_addresses",
]

log = logging.getLogger("eta.qbo")

QBO_SERVICE = "qbo"
READ_ONLY_MESSAGE = "QboClient is read-only (dry run / probe)"       # 04 §6.1
INCLUDE_CUSTOM_FIELDS = "enhancedAllCustomFields"                    # 04 §4 E2/E3/E4
INCLUDE_INVOICE_LINK = "invoiceLink"                                 # 04 §4 E7
MAX_RESULTS = 1000                                                   # 04 §4.1 MAXRESULTS maximum
MAX_QUERY_PAGES = 50                                                 # safety stop for STARTPOSITION paging
MAX_ATTEMPTS = 5                                                     # 04 §6.1 _call(max_attempts=5)

FAULT_STALE_OBJECT = "5010"
FAULT_DUPLICATE_DOCNUMBER = "6140"
FAULT_OBJECT_NOT_FOUND = "610"

VALUE_KEYS = ("DateValue", "StringValue", "NumberValue", "BooleanValue")
TYPE_VALUE_KEYS: Mapping[str, str] = {
    "DateType": "DateValue",
    "StringType": "StringValue",
    "NumberType": "NumberValue",
    "BooleanType": "BooleanValue",
}
INACTIVE_SUFFIX = "(inactive)"          # QBO appends this to deactivated custom fields (04 §6.4)

CANNOT_DETERMINE_ETA_FIELD = (
    "Cannot determine the QuickBooks ETA custom field. Run `eta qbo-fields --invoice <DocNumber>` on an invoice "
    "whose ETA is filled in, then set QB_ETA_DEFINITION_ID / QB_ETA_FIELD_TYPE."
)

_T = TypeVar("_T")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Small pure helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DATETIME_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T\d{2}:\d{2}:\d{2}(?:[+-]\d{2}:\d{2})?$")
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")


def parse_qbo_date(value: Any) -> date | None:
    """04 §6.2: `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SS[±hh:mm]` (date part as written), `MM/DD/YYYY`,
    `M/D/YYYY`; anything else → None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    try:
        if m := _ISO_DATE_RE.match(text) or _ISO_DATETIME_RE.match(text):
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if m := _US_DATE_RE.match(text):
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:  # e.g. 2026-02-30
        return None
    return None


def split_addresses(value: str | None) -> tuple[str, ...]:
    """Split an address list on `,`/`;`, strip, drop empties, de-duplicate case-insensitively (keep order)."""
    if not value:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[,;]", value):
        address = part.strip()
        if address and address.casefold() not in seen:
            seen.add(address.casefold())
            out.append(address)
    return tuple(out)


def _money(value: Any) -> Decimal | None:
    """Decimal(str(value)) for QBO amounts; None when missing/unparseable/non-finite (04 §6.2)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _address(raw: Mapping[str, Any], key: str) -> str | None:
    """BillEmail/BillEmailCc/BillEmailBcc .Address; empty → None (04 §6.2)."""
    obj = raw.get(key)
    if not isinstance(obj, Mapping):
        return None
    return _text_or_none(obj.get("Address"))


def _entries(raw: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The invoice's CustomField entries (only mappings)."""
    value = raw.get("CustomField")
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, Mapping)]


def _has_value(entry: Mapping[str, Any]) -> bool:
    """04 §6.5: any value key present and not None/""."""
    return any(key in entry and entry[key] is not None and entry[key] != "" for key in VALUE_KEYS)


def _value_key_for(entry: Mapping[str, Any], fallback_type: str | None = None) -> str:
    """04 §6.4: first value key present in the entry, else derived from Type (unknown → StringValue)."""
    for key in VALUE_KEYS:
        if key in entry:
            return key
    type_name = entry.get("Type") or fallback_type or ""
    return TYPE_VALUE_KEYS.get(str(type_name), "StringValue")


def _normalize(name: Any) -> str:
    """04 §6.4: normalize(n) = n.strip().casefold()."""
    return str(name or "").strip().casefold()


def _format_money_for_log(value: Decimal) -> str:
    return f"{value:,.2f}"


async def _gather_all(aws: Sequence[Awaitable[_T]]) -> list[_T]:
    """asyncio.gather that cancels the remaining awaitables when one fails."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Faults (04 §4.3)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class QboFault:
    code: str
    message: str
    detail: str

    def __str__(self) -> str:
        text = f"{self.code} {self.message}".strip()
        return f"{text} — {self.detail}" if self.detail else text


class QboFaultError(ServiceError):
    """A QuickBooks answer carried a Fault (HTTP 400, or HTTP 200 with a Fault key — 04 §4.3).

    Raised by the read helpers and the Preferences helpers; `.faults` / `.codes` let callers react to a
    specific code (e.g. 04b's 5010 retry). Invoice updates raise InvoiceUpdateError and sends
    InvoiceSendError instead (04 §4.3 table)."""

    def __init__(self, service: str = "", message: str | None = None, *, status: int | None = None,
                 body_excerpt: str = "", request_id: str | None = None,
                 faults: Sequence[QboFault] = ()) -> None:
        super().__init__(service, message, status=status, body_excerpt=body_excerpt, request_id=request_id)
        self.faults: tuple[QboFault, ...] = tuple(faults)

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(f.code for f in self.faults)


def _ci_get(mapping: Mapping[str, Any], name: str) -> Any:
    """Case-insensitive key lookup (auth/throttle faults use lower-case keys, 04 §4.3)."""
    if name in mapping:
        return mapping[name]
    lowered = name.lower()
    for key, value in mapping.items():
        if isinstance(key, str) and key.lower() == lowered:
            return value
    return None


def parse_faults(payload: Any) -> list[QboFault]:
    """Faults of a QBO JSON payload; [] when there is no Fault/fault key. Tolerant of key case."""
    if not isinstance(payload, Mapping):
        return []
    fault = _ci_get(payload, "fault")
    if fault is None:
        return []
    errors = _ci_get(fault, "error") if isinstance(fault, Mapping) else None
    if isinstance(errors, Mapping):
        errors = [errors]
    out: list[QboFault] = []
    if isinstance(errors, list):
        for err in errors:
            if not isinstance(err, Mapping):
                continue
            out.append(QboFault(
                code=str(_ci_get(err, "code") or "").strip(),
                message=str(_ci_get(err, "message") or "").strip(),
                detail=str(_ci_get(err, "detail") or "").strip(),
            ))
    if not out:
        kind = _ci_get(fault, "type") if isinstance(fault, Mapping) else None
        out.append(QboFault("", f"QuickBooks fault{f' ({kind})' if kind else ''}", ""))
    return out


def _fault_summary(faults: Sequence[QboFault]) -> str:
    return "; ".join(str(f) for f in faults)


def _has_code(faults: Sequence[QboFault], code: str) -> bool:
    return any(f.code == code for f in faults)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Rate limiting (04 §6.1)
# ════════════════════════════════════════════════════════════════════════════════════════════════
class MultiLimiter:
    """async context manager entering several http.AsyncLimiter instances in order."""

    def __init__(self, *limiters: AsyncLimiter) -> None:
        self.limiters: tuple[AsyncLimiter, ...] = tuple(limiters)

    async def __aenter__(self) -> MultiLimiter:
        entered: list[AsyncLimiter] = []
        try:
            for limiter in self.limiters:
                await limiter.__aenter__()
                entered.append(limiter)
        except BaseException as exc:
            for limiter in reversed(entered):
                await limiter.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        for limiter in reversed(self.limiters):
            await limiter.__aexit__(exc_type, exc, tb)

    def __repr__(self) -> str:
        return f"MultiLimiter({', '.join(map(repr, self.limiters))})"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# ETA custom field (04 §6.4)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class EtaFieldSpec:
    definition_id: str
    name: str
    type: str          # as reported by QBO: "DateType", "StringType", "NumberType", …
    value_key: str     # "DateValue" | "StringValue" | "NumberValue" | "BooleanValue"
    source: str        # "invoice <Id>" | "config"


def _entry_date(entry: Mapping[str, Any], spec: EtaFieldSpec, string_format: str) -> date | None:
    value = entry.get(spec.value_key)
    if value is None or value == "":
        value = next((entry[k] for k in VALUE_KEYS if entry.get(k) not in (None, "")), None)
    if value is None:
        return None
    if isinstance(value, str) and (spec.value_key == "StringValue" or "StringValue" in entry):
        try:  # 04 §3 QB_ETA_STRING_FORMAT: the format the UI flow typed
            return datetime.strptime(value.strip(), string_format).date()
        except ValueError:
            pass
    return parse_qbo_date(value)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# InvoiceSnapshot (04 §6.2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class InvoiceSnapshot:
    id: str
    sync_token: str
    doc_number: str
    total_amount: Decimal
    balance: Decimal
    customer_name: str | None
    bill_email: str | None            # BillEmail.Address (may hold several addresses, comma/semicolon separated)
    email_status: str | None          # "NotSet" | "NeedToSend" | "EmailSent"
    eta_value: date | None            # current ETA custom-field value, if present and parseable
    raw: Mapping[str, Any]            # full Invoice JSON as returned
    # ── additions ──
    bill_email_cc: str | None = None  # BillEmailCc.Address
    bill_email_bcc: str | None = None  # BillEmailBcc.Address
    due_date: date | None = None
    delivery_time: str | None = None  # DeliveryInfo.DeliveryTime (ISO string) if ever sent by QBO
    custom_fields: tuple[Mapping[str, Any], ...] = ()

    @property
    def is_paid(self) -> bool:            # UI "#balanceAmount" == "$0.00"
        return self.balance == 0

    @property
    def is_partially_paid(self) -> bool:  # UI "Partially paid" banner
        return Decimal(0) < self.balance < self.total_amount

    @property
    def recipients(self) -> tuple[str, ...]:
        """BillEmail split on [,;], stripped, empties dropped (case-insensitive duplicates removed)."""
        return split_addresses(self.bill_email)

    @classmethod
    def from_api(cls, raw: Mapping[str, Any], eta_field: EtaFieldSpec | None, *,
                 string_format: str = "%m/%d/%Y") -> InvoiceSnapshot:
        """Build a snapshot from an Invoice JSON object (04 §6.2 parsing rules)."""
        if not isinstance(raw, Mapping):
            raise ServiceError(QBO_SERVICE, "invoice payload is not a JSON object")
        invoice_id = str(raw.get("Id") or "")
        doc = str(raw.get("DocNumber") or "")
        total = _money(raw.get("TotalAmt", 0))
        if total is None:
            log.warning("QuickBooks invoice %s (#%s) has an unreadable TotalAmt %r — using 0",
                        invoice_id, doc, raw.get("TotalAmt"))
            total = Decimal(0)
        balance = _money(raw.get("Balance"))
        if balance is None:
            log.warning("QuickBooks invoice %s (#%s) has no usable Balance — treating it as unpaid (Balance = "
                        "TotalAmt %s)", invoice_id, doc, _format_money_for_log(total))
            balance = total
        customer = raw.get("CustomerRef")
        customer_name = _text_or_none(customer.get("name")) if isinstance(customer, Mapping) else None
        delivery = raw.get("DeliveryInfo")
        delivery_time = _text_or_none(delivery.get("DeliveryTime")) if isinstance(delivery, Mapping) else None
        entries = _entries(raw)
        eta_value: date | None = None
        if eta_field is not None:
            for entry in entries:
                if str(entry.get("DefinitionId")) == eta_field.definition_id:
                    eta_value = _entry_date(entry, eta_field, string_format)
                    break
        return cls(
            id=invoice_id,
            sync_token=str(raw.get("SyncToken") if raw.get("SyncToken") is not None else ""),
            doc_number=doc,
            total_amount=total,
            balance=balance,
            customer_name=customer_name,
            bill_email=_address(raw, "BillEmail"),
            email_status=_text_or_none(raw.get("EmailStatus")),
            eta_value=eta_value,
            raw=raw,
            bill_email_cc=_address(raw, "BillEmailCc"),
            bill_email_bcc=_address(raw, "BillEmailBcc"),
            due_date=parse_qbo_date(raw.get("DueDate")),
            delivery_time=delivery_time,
            custom_fields=tuple(dict(e) for e in entries),
        )


def ambiguous_invoice_error(doc_number: str, ids: Sequence[str]) -> AmbiguousInvoiceError:
    """The 04 §6.3 error for a DocNumber shared by several invoices; the pipeline uses its text for DocNumbers
    listed in `QboClient.ambiguous` (D21)."""
    return AmbiguousInvoiceError(
        f"{len(ids)} QuickBooks invoices share DocNumber {doc_number!r} (Ids {', '.join(ids)}) — fix in QBO")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# DocNumber length pre-check (04 §7)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def doc_number_problem(new_doc: str, settings: QboSettings) -> str | None:
    """None if OK, else a human reason, e.g.
    "New invoice number 'X' is 23 characters; QuickBooks allows at most 21 (QB_DOCNUMBER_MAX_LEN)"."""
    limit = settings.docnumber_max_len
    if len(new_doc) > limit:
        return (f"New invoice number '{new_doc}' is {len(new_doc)} characters; QuickBooks allows at most "
                f"{limit} (QB_DOCNUMBER_MAX_LEN)")
    return None


# ════════════════════════════════════════════════════════════════════════════════════════════════
# QboClient (04 §6)
# ════════════════════════════════════════════════════════════════════════════════════════════════
class QboClient:
    """QuickBooks Online invoice client (04 §6). Create it with build_qbo() in production code."""

    def __init__(self, settings: QboSettings, auth: QboAuth, *,
                 http: httpx.AsyncClient | None = None,
                 semaphore: asyncio.Semaphore | None = None,
                 clock: Clock | None = None,
                 token_store: TokenStore | None = None,          # ADDITION: needed by 04b emailers
                 state_dir: Path | None = None,                  # ADDITION: for 04b recovery files
                 email_settings: EmailSettings | None = None,    # ADDITION: see 04b
                 read_only: bool = False,                        # ADDITION: dry-run / probes
                 email_missing: Sequence[str] = ()) -> None:     # ADDITION: Settings.missing["email"] (04b §6.1)
        self._s = settings
        self._auth = auth
        self._http = http if http is not None else new_async_client(base_url=settings.api_base)
        self._owns_http = http is None
        self._sem = semaphore or asyncio.Semaphore(settings.max_concurrency)
        self._limiter = MultiLimiter(AsyncLimiter(settings.max_calls_per_second, 1.0),
                                     AsyncLimiter(settings.max_calls_per_minute, 60.0))
        self._clock: Clock = clock or SystemClock()
        self._token_store = token_store
        self._state_dir = Path(state_dir) if state_dir is not None else None
        self._email_settings = email_settings
        self._email_missing = tuple(email_missing)
        self._read_only = bool(read_only)
        self._company = f"/v3/company/{quote(str(settings.realm_id), safe='')}"
        self.ambiguous: dict[str, tuple[str, ...]] = {}        # ADDITION: DocNumber -> invoice Ids, cumulative
        self._eta_field: EtaFieldSpec | None = None
        self._warned: set[str] = set()
        self._emailer: InvoiceEmailer | None = None
        self._recovery_emailer: TemplateSwapEmailer | None = None   # XCT-03: recovery without a template_swap emailer
        self._send_ready = False
        self._closed = False

    # ── accessors used by the pipeline, the CLI and qbo/email.py ──
    @property
    def settings(self) -> QboSettings:
        return self._s

    @property
    def auth(self) -> QboAuth:
        return self._auth

    @property
    def read_only(self) -> bool:
        return self._read_only

    @property
    def clock(self) -> Clock:
        return self._clock

    @property
    def token_store(self) -> TokenStore | None:
        return self._token_store

    @property
    def state_dir(self) -> Path | None:
        return self._state_dir

    @property
    def email_settings(self) -> EmailSettings | None:
        return self._email_settings

    @property
    def email_missing(self) -> tuple[str, ...]:
        return self._email_missing

    @property
    def eta_field(self) -> EtaFieldSpec | None:
        """The ETA custom-field shape learned from the invoices read so far (04 §6.4)."""
        return self._eta_field

    @property
    def emailer(self) -> InvoiceEmailer | None:
        """The e-mail strategy, once created (04 §6.6)."""
        return self._emailer

    # ── plumbing (04 §6.1) ──
    def _ensure_writable(self) -> None:
        if self._read_only:
            raise RuntimeError(READ_ONLY_MESSAGE)

    def _warn_once(self, key: str, message: str, *args: Any) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    def _invoice_path(self, invoice_id: str) -> str:
        return f"{self._company}/invoice/{quote(str(invoice_id), safe='')}"

    async def _call(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                    json: Any = None, content: bytes | None = None,
                    headers: Mapping[str, str] | None = None,
                    expected: Collection[int] = (200, 400),
                    idempotent: bool | None = None,
                    max_attempts: int = MAX_ATTEMPTS) -> httpx.Response:
        """One QBO API call: minorversion, QBO semaphore, limiter, auth with one 401 re-auth (04 §6.1).

        HTTP 403 (app not authorized for the realm / missing scope, codes 3100/3003) → AuthError, no retry
        (04 §4.3)."""
        query = {"minorversion": self._s.minor_version, **(params or {})}
        accepted = tuple(sorted(set(expected) | {403}))
        extra: dict[str, Any] = {}
        if json is not None:
            extra["json"] = json
        if content is not None:
            extra["content"] = content
        async with self._sem:
            resp = await request_with_auth(
                self._http, method, path, service=QBO_SERVICE, auth=self._auth,
                params=query, headers=headers, expected=accepted, idempotent=idempotent,
                limiter=self._limiter, max_attempts=max_attempts, **extra,
            )
        if resp.status_code == 403 and 403 not in expected:
            faults = parse_faults(self._try_json(resp))
            codes = ", ".join(f.code for f in faults if f.code) or "?"
            rid = request_id_from(resp.headers)
            raise AuthError(f"QuickBooks denied access to realm {self._s.realm_id} (code {codes})"
                            + (f" [request_id={rid}]" if rid else ""))
        return resp

    @staticmethod
    def _try_json(resp: httpx.Response) -> Any:
        try:
            return resp.json()
        except ValueError:
            return None

    @staticmethod
    def _faults(resp: httpx.Response) -> list[QboFault]:
        """04 §6.1: faults of a response; [] when there is no Fault/fault key or no JSON body."""
        try:
            payload = resp.json()
        except ValueError:
            return []
        return parse_faults(payload)

    def _payload(self, resp: httpx.Response) -> Any:
        """JSON body: strict for 200 (ServiceError if malformed), tolerant for error statuses."""
        if resp.status_code == 200:
            return json_body(resp, service=QBO_SERVICE)
        return self._try_json(resp)

    @staticmethod
    def _problems(resp: httpx.Response, payload: Any) -> list[QboFault]:
        """[] for a clean 200; otherwise the faults (at least one). A 200 with a Fault is an error (04 §4.3)."""
        faults = parse_faults(payload)
        if resp.status_code == 200 and not faults:
            return []
        return faults or [QboFault("", f"HTTP {resp.status_code} without a Fault body", "")]

    def _fault_error(self, resp: httpx.Response, faults: Sequence[QboFault], what: str) -> QboFaultError:
        return QboFaultError(QBO_SERVICE, f"{what}: {_fault_summary(faults)}", status=resp.status_code,
                             request_id=request_id_from(resp.headers), faults=faults)

    # ── ETA custom field (04 §6.4) ──
    def _match_eta_entry_rule(self, entries: Sequence[Mapping[str, Any]]
                              ) -> tuple[Mapping[str, Any] | None, str | None]:
        configured = (self._s.eta_definition_id or "").strip()
        if configured:                                                              # rule 1
            for entry in entries:
                if str(entry.get("DefinitionId")) == configured:
                    return entry, "definition_id"
        target = _normalize(self._s.eta_field_name)                                 # rule 2
        candidates = []
        for entry in entries:
            name = _normalize(entry.get("Name"))
            if not name or name.endswith(INACTIVE_SUFFIX):
                continue
            if name == target:
                candidates.append(entry)
        if not candidates:                                                          # rule 3
            return None, None
        if len(candidates) == 1:
            return candidates[0], "name"
        with_value = [e for e in candidates if _has_value(e)]
        if len(with_value) == 1:
            return with_value[0], "name"
        pool = with_value or candidates
        self._warn_once(
            "several-eta-fields",
            "Several custom fields are named %r on one invoice (DefinitionIds %s) — using %s; set "
            "QB_ETA_DEFINITION_ID to choose", self._s.eta_field_name,
            ", ".join(str(e.get("DefinitionId")) for e in pool), pool[0].get("DefinitionId"),
        )
        return pool[0], "name"

    def _match_eta_entry(self, entries: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        """04 §6.4: configured DefinitionId, else the (active) entry named QB_ETA_FIELD_NAME, else None."""
        return self._match_eta_entry_rule(entries)[0]

    def _spec_from_entry(self, entry: Mapping[str, Any], *, source: str) -> EtaFieldSpec:
        type_name = str(entry.get("Type") or "").strip() or self._s.eta_field_type
        return EtaFieldSpec(
            definition_id=str(entry.get("DefinitionId")),
            name=str(entry.get("Name") or self._s.eta_field_name),
            type=type_name,
            value_key=_value_key_for(entry, type_name),
            source=source,
        )

    def _learn_eta_field(self, raw: Mapping[str, Any]) -> None:
        entry, rule = self._match_eta_entry_rule(_entries(raw))
        if entry is None:
            return
        spec = self._spec_from_entry(entry, source=f"invoice {raw.get('Id')}")
        if self._eta_field is None:
            self._eta_field = spec
            configured = (self._s.eta_definition_id or "").strip()
            if rule == "name" and configured:
                self._warn_once(
                    "eta-definition-id",
                    "QB_ETA_DEFINITION_ID=%s was not found; QuickBooks reports field '%s' as DefinitionId %s "
                    "(Type %s). Set QB_ETA_DEFINITION_ID=%s in .env.",
                    configured, spec.name, spec.definition_id, spec.type, spec.definition_id,
                )
        elif (spec.definition_id != self._eta_field.definition_id
              and _normalize(spec.name) == _normalize(self._eta_field.name)):
            self._warn_once(
                "eta-definition-conflict",
                "Invoice %s reports the custom field '%s' as DefinitionId %s, but %s reported DefinitionId %s "
                "— keeping %s", raw.get("Id"), spec.name, spec.definition_id, self._eta_field.source,
                self._eta_field.definition_id, self._eta_field.definition_id,
            )

    def _resolve_eta_field(self, raw: Mapping[str, Any], *, warn: bool) -> EtaFieldSpec | None:
        entry = self._match_eta_entry(_entries(raw))
        if entry is not None:                                    # entry on this invoice
            return self._spec_from_entry(entry, source=f"invoice {raw.get('Id')}")
        if self._eta_field is not None:                          # the learned spec
            return self._eta_field
        configured = (self._s.eta_definition_id or "").strip()
        if configured:                                           # configuration (UNVERIFIED shape)
            type_name = self._s.eta_field_type
            if warn:
                self._warn_once("eta-config-fallback",
                                "ETA field shape not observed on any invoice; using QB_ETA_FIELD_TYPE=%s (UNVERIFIED)",
                                type_name)
            return EtaFieldSpec(configured, self._s.eta_field_name, type_name,
                                TYPE_VALUE_KEYS.get(type_name, "StringValue"), "config")
        return None

    def _eta_field_for_write(self, raw: Mapping[str, Any]) -> EtaFieldSpec:
        """04 §6.4: entry on this invoice → learned spec → configuration → InvoiceUpdateError."""
        spec = self._resolve_eta_field(raw, warn=True)
        if spec is None:
            raise InvoiceUpdateError(CANNOT_DETERMINE_ETA_FIELD)
        return spec

    def _format_eta(self, spec: EtaFieldSpec, d: date) -> str:
        """04 §6.4 value formatting."""
        if spec.value_key == "DateValue":
            return d.isoformat()                                 # UNVERIFIED shape — verified on write (§6.5)
        if spec.value_key == "StringValue":
            return d.strftime(self._s.eta_string_format)
        raise InvoiceUpdateError(
            f"ETA custom field has unsupported type {spec.type} ({spec.value_key}); it must be a date or text "
            "field (see 04 §8)")

    def _snapshot(self, raw: Mapping[str, Any], *, spec: EtaFieldSpec | None = None) -> InvoiceSnapshot:
        self._learn_eta_field(raw)
        if spec is None:
            spec = self._resolve_eta_field(raw, warn=False)
        return InvoiceSnapshot.from_api(raw, spec, string_format=self._s.eta_string_format)

    # ── lookups (04 §6.3) ──
    @staticmethod
    def _quote(value: str) -> str:
        """04 §4.1: single-quoted literal, a quote escaped as \\'."""
        return "'" + value.replace("'", "\\'") + "'"

    async def _query_invoices(self, where: str) -> list[Mapping[str, Any]]:
        """E2 with STARTPOSITION paging when a page is full (04 §4.1). Faults → QboFaultError."""
        out: list[Mapping[str, Any]] = []
        start = 1
        for _page in range(MAX_QUERY_PAGES):
            position = f"STARTPOSITION {start} " if start > 1 else ""
            query = f"SELECT * FROM Invoice WHERE {where} {position}MAXRESULTS {MAX_RESULTS}"
            resp = await self._call("GET", f"{self._company}/query",
                                    params={"query": query, "include": INCLUDE_CUSTOM_FIELDS})
            payload = self._payload(resp)
            problems = self._problems(resp, payload)
            if problems:
                raise self._fault_error(resp, problems, "invoice query rejected")
            response = payload.get("QueryResponse") if isinstance(payload, Mapping) else None
            invoices = response.get("Invoice") if isinstance(response, Mapping) else None
            page = [i for i in invoices if isinstance(i, Mapping)] if isinstance(invoices, list) else []
            out.extend(page)
            if len(page) < MAX_RESULTS:
                return out
            start += MAX_RESULTS
        log.warning("QuickBooks query returned more than %d invoices for %s — stopped paging",
                    MAX_QUERY_PAGES * MAX_RESULTS, where[:120])
        return out

    async def _query_single(self, doc_number: str) -> list[Mapping[str, Any]]:
        try:
            return await self._query_invoices(f"DocNumber = {self._quote(doc_number)}")
        except QboFaultError as exc:
            log.warning("QuickBooks rejected the query for DocNumber %r (%s) — treating it as not found",
                        doc_number, exc)
            return []

    async def _query_chunk(self, chunk: Sequence[str]) -> list[Mapping[str, Any]]:
        where = "DocNumber IN (" + ", ".join(self._quote(d) for d in chunk) + ")"
        try:
            return await self._query_invoices(where)
        except QboFaultError as exc:
            if len(chunk) > 1:  # 04 §6.3 step 3: isolate a problematic value
                log.warning("QuickBooks rejected a DocNumber IN (...) query for %d DocNumbers (%s) — querying them "
                            "one at a time", len(chunk), exc)
                parts = await _gather_all([self._query_single(d) for d in chunk])
                return [raw for part in parts for raw in part]
            log.warning("QuickBooks rejected the query for DocNumber %r (%s) — treating it as not found",
                        chunk[0], exc)
            return []

    async def find_invoices_by_doc_numbers(self, doc_numbers: Sequence[str]) -> dict[str, InvoiceSnapshot]:
        """04 §6.3: one `IN (…)` query per chunk; exact match, then a single case-insensitive match;
        DocNumbers shared by several invoices go to `self.ambiguous` and are excluded (D21)."""
        wanted = list(dict.fromkeys(d.strip() for d in doc_numbers if d and d.strip()))       # step 1
        if not wanted:
            return {}
        size = max(1, int(self._s.query_chunk_size))
        chunks = [wanted[i:i + size] for i in range(0, len(wanted), size)]                  # step 2
        results = await _gather_all([self._query_chunk(c) for c in chunks])                 # step 3
        by_id: dict[str, Mapping[str, Any]] = {}
        for invoices in results:
            for raw in invoices:
                key = str(raw.get("Id") or f"<no Id {id(raw)}>")
                by_id.setdefault(key, raw)
        everything = list(by_id.values())
        for raw in everything:                                                              # step 4
            self._learn_eta_field(raw)
        found: dict[str, InvoiceSnapshot] = {}
        for d in wanted:                                                                    # step 5
            matches = [i for i in everything if i.get("DocNumber") == d]
            if not matches:
                folded = d.casefold()
                ci = [i for i in everything if str(i.get("DocNumber") or "").casefold() == folded]
                if len(ci) == 1:
                    log.info("case-insensitive DocNumber match: %r is %r in QuickBooks (invoice %s)",
                             d, ci[0].get("DocNumber"), ci[0].get("Id"))
                    matches = ci
                elif len(ci) > 1:
                    log.warning("DocNumber %r matches %d QuickBooks invoices only case-insensitively (Ids %s) — "
                                "not processed", d, len(ci), ", ".join(str(i.get("Id")) for i in ci))
            if len(matches) == 1:
                found[d] = self._snapshot(matches[0])
                self.ambiguous.pop(d, None)
            elif len(matches) > 1:
                # DEVIATION (D21): the original silently took invoices[0]; e-mailing a customer from an
                # arbitrary duplicate is unsafe, so ambiguous DocNumbers are not processed (04 §6.3).
                ids = tuple(str(i.get("Id")) for i in matches)
                self.ambiguous[d] = ids
                log.warning("%d QuickBooks invoices share DocNumber %r (Ids %s) — not processed",
                            len(ids), d, ", ".join(ids))
            else:
                self.ambiguous.pop(d, None)
        return found                                                                        # step 6

    async def get_invoice_by_doc_number(self, doc_number: str) -> InvoiceSnapshot:
        """ADDITION (04 §6.3): one invoice by DocNumber, or AmbiguousInvoiceError / InvoiceNotFoundError."""
        d = (doc_number or "").strip()
        found = await self.find_invoices_by_doc_numbers([d])
        if d in found:
            return found[d]
        ids = self.ambiguous.get(d)
        if ids:
            raise ambiguous_invoice_error(d, ids)
        raise InvoiceNotFoundError(f"#{d} not found in QuickBooks")

    async def _fetch_invoice(self, invoice_id: str, *, spec: EtaFieldSpec | None = None) -> InvoiceSnapshot:
        resp = await self._call("GET", self._invoice_path(invoice_id), params={"include": INCLUDE_CUSTOM_FIELDS})
        payload = self._payload(resp)
        problems = self._problems(resp, payload)
        if problems:
            if _has_code(problems, FAULT_OBJECT_NOT_FOUND):
                raise InvoiceNotFoundError(f"Invoice {invoice_id} not found in QuickBooks (deleted?)")
            raise self._fault_error(resp, problems, f"reading invoice {invoice_id} was rejected")
        raw = payload.get("Invoice") if isinstance(payload, Mapping) else None
        if not isinstance(raw, Mapping):
            raise ServiceError(QBO_SERVICE, f"the answer for invoice {invoice_id} has no Invoice object",
                               status=resp.status_code, request_id=request_id_from(resp.headers))
        return self._snapshot(raw, spec=spec)

    async def get_invoice(self, invoice_id: str) -> InvoiceSnapshot:
        """E3 with include=enhancedAllCustomFields; 400 code 610 → InvoiceNotFoundError (04 §6.5)."""
        return await self._fetch_invoice(invoice_id)

    # ── updates (04 §6.5) ──
    def _update_body(self, inv: InvoiceSnapshot, want_doc: str | None, want_eta: date | None
                     ) -> tuple[dict[str, Any], EtaFieldSpec | None]:
        body: dict[str, Any] = {"Id": inv.id, "SyncToken": inv.sync_token, "sparse": True}
        spec: EtaFieldSpec | None = None
        if want_doc is not None:
            body["DocNumber"] = want_doc
        if want_eta is not None:
            spec = self._eta_field_for_write(inv.raw)
            value = self._format_eta(spec, want_eta)
            # Sparse-update array semantics are UNVERIFIED (04 §4.2): send the complete CustomField array as
            # last read, with only the ETA entry replaced/appended.
            entries = [dict(e) for e in _entries(inv.raw)
                       if _has_value(e) and str(e.get("DefinitionId")) != spec.definition_id]
            entries.append({"DefinitionId": spec.definition_id, "Name": spec.name, "Type": spec.type,
                            spec.value_key: value})
            body["CustomField"] = entries
        return body, spec

    async def _post_update(self, body: Mapping[str, Any]) -> httpx.Response:
        """E4: a fresh requestid per logical update, reused by every transport retry of this body (04 §4.2)."""
        params: dict[str, Any] = {"requestid": uuid.uuid4().hex}
        if self._s.include_on_writes:
            params["include"] = INCLUDE_CUSTOM_FIELDS
        return await self._call("POST", f"{self._company}/invoice", params=params, json=dict(body),
                                idempotent=True)

    def _update_fault(self, inv: InvoiceSnapshot, want_doc: str | None, faults: Sequence[QboFault],
                      resp: httpx.Response) -> Exception:
        rid = request_id_from(resp.headers)
        if _has_code(faults, FAULT_DUPLICATE_DOCNUMBER):
            doc = want_doc if want_doc is not None else inv.doc_number
            return InvoiceUpdateError(f"DocNumber {doc!r} is already used in QuickBooks (duplicate document number)",
                                      request_id=rid)
        if _has_code(faults, FAULT_OBJECT_NOT_FOUND):
            return InvoiceNotFoundError(f"Invoice {inv.id} no longer exists in QuickBooks")
        return InvoiceUpdateError(QBO_SERVICE, f"update rejected: {_fault_summary(faults)}",
                                  status=resp.status_code, request_id=rid)

    async def update_invoice(self, inv: InvoiceSnapshot, *, eta: date | None = None,
                             doc_number: str | None = None) -> InvoiceSnapshot:
        """Sparse update of the ETA custom field and/or the DocNumber, verified (04 §6.5)."""
        self._ensure_writable()                                                             # step 1
        if eta is None and doc_number is None:
            return inv
        want_doc = doc_number if doc_number is not None and doc_number != inv.doc_number else None   # step 2
        want_eta = eta if eta is not None and eta != inv.eta_value else None
        if want_doc is None and want_eta is None:
            # DEVIATION (D22): the UI always clicked Save; an update whose values already match is skipped.
            log.debug("[QBO] Invoice #%s: no change (ETA=%s, DocNumber=%s) — no API call",
                      inv.doc_number, inv.eta_value, inv.doc_number)
            return inv
        try:
            return await self._apply_update(inv, want_doc, want_eta)
        except InvoiceUpdateError:
            raise
        except ServiceError as exc:  # network / 5xx / unexpected payload while updating
            raise InvoiceUpdateError.wrap(exc) from exc

    async def _apply_update(self, inv: InvoiceSnapshot, want_doc: str | None,
                            want_eta: date | None) -> InvoiceSnapshot:
        body, spec = self._update_body(inv, want_doc, want_eta)                             # step 3
        resp = await self._post_update(body)                                                # step 4
        payload = self._payload(resp)
        problems = self._problems(resp, payload)
        if problems and _has_code(problems, FAULT_STALE_OBJECT):                            # step 6: 5010
            log.info("[QBO] Invoice #%s changed in QuickBooks meanwhile (stale SyncToken) — re-reading and "
                     "retrying once", inv.doc_number)
            fresh = await self.get_invoice(inv.id)
            if want_doc is not None and fresh.doc_number != inv.doc_number:
                raise InvoiceUpdateError(
                    f"Invoice {inv.id} DocNumber changed in QuickBooks while processing "
                    f"({inv.doc_number!r} → {fresh.doc_number!r}); not overwriting")
            body, spec = self._update_body(fresh, want_doc, want_eta)
            resp = await self._post_update(body)                                            # new requestid
            payload = self._payload(resp)
            problems = self._problems(resp, payload)
            if problems and _has_code(problems, FAULT_STALE_OBJECT):
                raise InvoiceUpdateError(
                    f"stale object twice — someone is editing invoice {inv.id} (#{inv.doc_number}) in QuickBooks; "
                    "try again later", request_id=request_id_from(resp.headers))
        if problems:
            raise self._update_fault(inv, want_doc, problems, resp)
        raw = payload.get("Invoice") if isinstance(payload, Mapping) else None              # step 5
        if not isinstance(raw, Mapping):
            raise InvoiceUpdateError(QBO_SERVICE, f"the update answer for invoice {inv.id} has no Invoice object",
                                     status=resp.status_code, request_id=request_id_from(resp.headers))
        updated = self._snapshot(raw, spec=spec)
        if self._s.verify_updates:                                                          # step 7
            if want_doc is not None and updated.doc_number != want_doc:
                raise InvoiceUpdateError(
                    f"QuickBooks stored DocNumber {updated.doc_number!r} instead of {want_doc!r} (length limit?)")
            if want_eta is not None and updated.eta_value != want_eta:
                check = await self._fetch_invoice(inv.id, spec=spec)
                if check.eta_value != want_eta:
                    raise InvoiceUpdateError(
                        f"QuickBooks accepted the update but the ETA custom field did not change on invoice "
                        f"{inv.id} (still {check.eta_value}). The API may be unable to write this field — see 04 §8.")
                updated = check
        log.info("[QBO] Invoice #%s saved — ETA=%s DocNumber=%s", inv.doc_number,
                 updated.eta_value if updated.eta_value is not None else want_eta, updated.doc_number)   # step 8
        return updated

    async def mark_email_sent(self, inv: InvoiceSnapshot) -> InvoiceSnapshot:
        """Sparse update {"EmailStatus": "EmailSent"} for the mailbox strategies (04 §6.6, 04b §6.3 step 6).

        UNVERIFIED that QuickBooks accepts it — callers treat a failure as a warning."""
        self._ensure_writable()
        body = {"Id": inv.id, "SyncToken": inv.sync_token, "sparse": True, "EmailStatus": "EmailSent"}
        resp = await self._post_update(body)
        payload = self._payload(resp)
        problems = self._problems(resp, payload)
        if problems:
            raise InvoiceUpdateError(QBO_SERVICE, f"marking invoice {inv.id} as e-mailed was rejected: "
                                     f"{_fault_summary(problems)}", status=resp.status_code,
                                     request_id=request_id_from(resp.headers))
        raw = payload.get("Invoice") if isinstance(payload, Mapping) else None
        if not isinstance(raw, Mapping):
            raise InvoiceUpdateError(QBO_SERVICE, f"the answer for invoice {inv.id} has no Invoice object",
                                     status=resp.status_code, request_id=request_id_from(resp.headers))
        return self._snapshot(raw)

    # ── e-mail (04 §6.6, 04b) ──
    def _get_emailer(self) -> InvoiceEmailer:
        if self._emailer is None:
            from .email import build_emailer  # local import: email.py imports this module

            self._emailer = build_emailer(self)
        return self._emailer

    async def prepare_for_sending(self, *, dry_run: bool = False) -> None:
        """ADDITION — 04b §6.2/§6.3 preflight (crash recovery, verification gate, template checks).

        The pipeline calls it once before applying any plan that sends, so a blocked e-mail path is
        detected before any DocNumber is suffixed. A read-only client always runs the dry-run variant
        (it cannot repair anything)."""
        effective_dry_run = dry_run or self._read_only
        await self._get_emailer().preflight(dry_run=effective_dry_run)
        if not effective_dry_run:
            self._send_ready = True

    async def recover_email_template(self) -> bool:
        """ADDITION (review fix XCT-03) — the 04b §6.2 step 1 crash recovery on its own, whatever QB_EMAIL_STRATEGY is
        (as `eta probe qbo --restore-template`): restore the invoice e-mail subject a crashed template_swap run left in
        `<state_dir>/qbo_email_template_recovery.json` and delete the file. True when a recovery file was handled,
        False when there was nothing to do. Raises TemplateRestoreError (with the manual instructions) or
        EmailPathUnavailableError (another company's file); RuntimeError on a read-only client."""
        self._ensure_writable()
        emailer = self._template_recovery()
        return False if emailer is None else await emailer.recover()

    def _template_recovery(self) -> TemplateSwapEmailer | None:
        """The template_swap emailer that owns the recovery file: the strategy itself, else one built for it."""
        # local import: email.py imports this module
        from .email import TemplateSwapEmailer

        if isinstance(self._emailer, TemplateSwapEmailer):
            return self._emailer
        if self._state_dir is None:
            return None
        if self._recovery_emailer is None:
            self._recovery_emailer = TemplateSwapEmailer(self, state_dir=self._state_dir, clock=self._clock)
        return self._recovery_emailer

    async def _recover_template_at_close(self) -> None:
        """aclose() fallback (XCT-03): a client whose own strategy is not template_swap, or that never sent, still
        repairs a template left swapped by a crashed template_swap run. Never raises; a read-only client warns."""
        # local import: email.py imports this module
        from .email import RECOVERY_FILE_NAME, TemplateSwapEmailer

        if self._state_dir is None or isinstance(self._emailer, TemplateSwapEmailer):
            return  # a template_swap strategy handles its recovery file in its own aclose()
        path = self._state_dir / RECOVERY_FILE_NAME
        if not path.exists():
            return
        if self._read_only:
            log.warning("The QuickBooks invoice e-mail subject may still be modified by a crashed run (%s); the next "
                        "real run restores it (or `eta probe qbo --restore-template`).", path)
            return
        try:
            await self.recover_email_template()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise; the instructions are logged
            log.error("The QuickBooks invoice e-mail subject could not be restored at shutdown: %s", exc)

    async def send_invoice(self, inv: InvoiceSnapshot, *, subject: str) -> SendReceipt:
        """E-mail the invoice with this exact subject through the configured strategy (04 §6.6, 04b)."""
        self._ensure_writable()
        from .email import validate_send  # local import: email.py imports this module

        validate_send(self, inv, subject)  # 04b §6.1: common validation before any network call
        if not self._send_ready:
            await self.prepare_for_sending()
        return await self._get_emailer().send(inv, subject=subject)

    # ── low-level helpers used by 04b (all through _call) ──
    async def _post_send(self, invoice_id: str, send_to: str | None) -> dict[str, Any]:
        """E5 POST …/invoice/{id}/send[?sendTo=] → the full JSON answer ({"Invoice": {…}, "time": …}).

        idempotent=False: http.request() retries only what never reached Intuit (connect errors) and
        429/503 refusals; a timeout or 5xx is never re-POSTed (Intuit ignores requestid here — a repeat
        e-mails the customer again)."""
        self._ensure_writable()
        params = {"sendTo": send_to} if send_to else None
        resp = await self._call("POST", f"{self._invoice_path(invoice_id)}/send", params=params, content=b"",
                                headers={"Content-Type": "application/octet-stream"}, idempotent=False)
        payload = self._payload(resp)
        problems = self._problems(resp, payload)
        if problems:
            raise InvoiceSendError(QBO_SERVICE, f"send rejected: {_fault_summary(problems)}",
                                   status=resp.status_code, request_id=request_id_from(resp.headers))
        return dict(payload) if isinstance(payload, Mapping) else {}

    async def get_invoice_pdf(self, invoice_id: str) -> bytes:
        """E6: the invoice PDF; must start with b"%PDF" else ServiceError."""
        resp = await self._call("GET", f"{self._invoice_path(invoice_id)}/pdf", headers={"Accept": "application/pdf"})
        data = resp.content
        if resp.status_code != 200 or not data.startswith(b"%PDF"):
            faults = parse_faults(self._try_json(resp))
            what = f"invoice {invoice_id} PDF download did not return a PDF"
            if faults:
                raise self._fault_error(resp, faults, what)
            raise ServiceError(QBO_SERVICE, what, status=resp.status_code,
                               request_id=request_id_from(resp.headers))
        return data

    async def get_invoice_link(self, invoice_id: str) -> str | None:
        """E7: Invoice.InvoiceLink (customer pay/view URL) or None (online payments off / no BillEmail)."""
        resp = await self._call("GET", self._invoice_path(invoice_id), params={"include": INCLUDE_INVOICE_LINK})
        payload = self._payload(resp)
        problems = self._problems(resp, payload)
        if problems:
            if _has_code(problems, FAULT_OBJECT_NOT_FOUND):
                raise InvoiceNotFoundError(f"Invoice {invoice_id} not found in QuickBooks (deleted?)")
            raise self._fault_error(resp, problems, f"reading the pay link of invoice {invoice_id} was rejected")
        invoice = payload.get("Invoice") if isinstance(payload, Mapping) else None
        link = invoice.get("InvoiceLink") if isinstance(invoice, Mapping) else None
        return link.strip() if isinstance(link, str) and link.strip() else None

    async def get_preferences(self) -> dict[str, Any]:
        """E8: the company's Preferences object."""
        resp = await self._call("GET", f"{self._company}/preferences")
        payload = self._payload(resp)
        problems = self._problems(resp, payload)
        if problems:
            raise self._fault_error(resp, problems, "reading Preferences was rejected")
        prefs = payload.get("Preferences") if isinstance(payload, Mapping) else None
        if not isinstance(prefs, Mapping):
            raise ServiceError(QBO_SERVICE, "the Preferences answer has no Preferences object",
                               status=resp.status_code, request_id=request_id_from(resp.headers))
        return dict(prefs)

    async def update_preferences(self, prefs: Mapping[str, Any]) -> dict[str, Any]:
        """E9 (requestid): write Preferences; returns the new Preferences object. Faults → QboFaultError."""
        self._ensure_writable()
        resp = await self._call("POST", f"{self._company}/preferences", params={"requestid": uuid.uuid4().hex},
                                json=dict(prefs), idempotent=True)
        payload = self._payload(resp)
        problems = self._problems(resp, payload)
        if problems:
            raise self._fault_error(resp, problems, "Preferences update rejected")
        result = payload.get("Preferences") if isinstance(payload, Mapping) else None
        if not isinstance(result, Mapping):
            raise ServiceError(QBO_SERVICE, "the Preferences update answer has no Preferences object",
                               status=resp.status_code, request_id=request_id_from(resp.headers))
        return dict(result)

    async def get_company_info(self) -> dict[str, Any]:
        """E10: the CompanyInfo object (CompanyName, Email, …)."""
        realm = quote(str(self._s.realm_id), safe="")
        resp = await self._call("GET", f"{self._company}/companyinfo/{realm}")
        payload = self._payload(resp)
        problems = self._problems(resp, payload)
        if problems:
            raise self._fault_error(resp, problems, "reading CompanyInfo was rejected")
        info = payload.get("CompanyInfo") if isinstance(payload, Mapping) else None
        if not isinstance(info, Mapping):
            raise ServiceError(QBO_SERVICE, "the CompanyInfo answer has no CompanyInfo object",
                               status=resp.status_code, request_id=request_id_from(resp.headers))
        return dict(info)

    # ── diagnostics (04 §6.7) ──
    async def list_custom_fields(self, doc_number: str) -> list[dict[str, Any]]:
        """One dict per CustomField entry of that invoice, plus a final {"resolved_eta_field": …} row."""
        inv = await self.get_invoice_by_doc_number(doc_number)
        entries = _entries(inv.raw)
        matched, rule = self._match_eta_entry_rule(entries)
        rows: list[dict[str, Any]] = []
        for entry in entries:
            key = _value_key_for(entry, self._s.eta_field_type)
            is_match = entry is matched
            rows.append({
                "DefinitionId": entry.get("DefinitionId"),
                "Name": entry.get("Name"),
                "Type": entry.get("Type"),
                "value_key": key,
                "value": entry.get(key),
                "is_eta_match": is_match,
                "match_rule": rule if is_match else None,
            })
        spec = self._resolve_eta_field(inv.raw, warn=False)
        rows.append({"resolved_eta_field": asdict(spec) if spec is not None else None})
        return rows

    # ── lifecycle ──
    async def aclose(self) -> None:
        """Close the e-mail strategy (it restores a swapped template, 04b §6.2), the auth client and the
        HTTP client (04 §6.8). A template left swapped by a crashed run is restored even when this client built no
        template_swap strategy (XCT-03)."""
        if self._closed:
            return
        self._closed = True
        try:
            try:
                if self._emailer is not None:
                    await self._emailer.aclose()
            finally:
                await self._recover_template_at_close()
        finally:
            try:
                closer = getattr(self._auth, "aclose", None)
                if closer is not None:
                    await closer()
            finally:
                if self._owns_http:
                    await self._http.aclose()

    async def __aenter__(self) -> QboClient:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.aclose()


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Factory (04 §6.8)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def build_qbo(settings: Settings, token_store: TokenStore, *, read_only: bool = False) -> QboClient:
    """QboAuth + QboClient (token_store, settings.run.state_dir, settings.email). ConfigError when QuickBooks
    is not configured."""
    if settings.qbo is None:
        missing = settings.missing.get("qbo") or QBO_REQUIRED
        raise ConfigError(f"QuickBooks is not configured — set {', '.join(missing)} in .env")
    auth = QboAuth(settings.qbo, token_store)
    return QboClient(
        settings.qbo, auth,
        token_store=token_store,
        state_dir=settings.run.state_dir,
        email_settings=settings.email,
        read_only=read_only,
        email_missing=tuple(settings.missing.get("email", ())),
    )
