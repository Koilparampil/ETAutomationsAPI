"""Invoice e-mail delivery with a per-invoice subject (04b-invoice-email-delivery §6).

QuickBooks' `/send` takes no subject, so the subject from `texts.subject_for()` is delivered by one of:

- `TemplateSwapEmailer` (`template_swap`, default): under a global lock, set the company's invoice e-mail
  subject (Preferences) to the ETA subject → `/send` → restore the original subject immediately (in a
  `finally`, shielded from cancellation, with retries). A recovery file makes a crash repairable; a
  human-confirmed verification record is required before real sends (04b §10).
- `MailboxEmailer` with `GraphTransport` (Microsoft Graph sendMail) or `SmtpTransport` (smtplib): the
  company mailbox sends the QBO invoice PDF and pay link with the exact subject.

Exactly-once rule (04b §7): no strategy ever re-POSTs a send that may have reached the server.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import json
import logging
import math
import os
import re
import smtplib
import ssl
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from ..auth import request_with_auth
from ..clock import Clock, SystemClock
from ..config import EmailSettings, QboSettings
from ..errors import (
    AuthError,
    ConfigError,
    EmailPathUnavailableError,
    InvoiceSendError,
    ServiceError,
    TemplateRestoreError,
)
from ..http import AsyncLimiter, new_async_client, request
from ..logging_setup import redact
from ..token_store import TokenStore, is_fresh, jwt_exp
from .client import READ_ONLY_MESSAGE, InvoiceSnapshot, QboFaultError, split_addresses

if TYPE_CHECKING:
    from .client import QboClient

__all__ = [
    "DEFAULT_BODY_TEMPLATE",
    "ETA_SUBJECT_RE",
    "GraphAuth",
    "GraphTransport",
    "InvoiceEmailer",
    "MailTransport",
    "MailboxEmailer",
    "OutgoingMail",
    "RECOVERY_FILE_NAME",
    "SendReceipt",
    "SmtpTransport",
    "TemplateSwapEmailer",
    "TemplateText",
    "VERIFIED_FILE_NAME",
    "build_emailer",
    "build_mime_message",
    "graph_payload",
    "is_verified",
    "load_verifications",
    "manual_restore_instructions",
    "pdf_file_name",
    "record_verification",
    "redirect_refusal",
    "render_body",
    "validate_send",
]

log = logging.getLogger("eta.qbo")

# ── 04b §6.2 state files (in ETA_STATE_DIR, written atomically, 0600) ──
RECOVERY_FILE_NAME = "qbo_email_template_recovery.json"
VERIFIED_FILE_NAME = "qbo_email_probe.json"

# Recognises our own subjects (texts.subject_for and the 04b §10 probe subject).
ETA_SUBJECT_RE = re.compile(r"^(ETA probe .* — )?Invoice for Booking no\. .*\*{3}")

RESTORE_ATTEMPTS = 3
RESTORE_DELAYS_S = (1.0, 2.0, 4.0)          # 04b §6.2 step 3.5: sleeps 1, 2, 4 s between attempts
INFERENCE_SLACK = timedelta(seconds=5)      # 04b §6.2 step 4: delivery at ≥ started − 5 s
QBO_SERVICE = "qbo"

DEFAULT_BODY_TEMPLATE = ("Dear [Customer Name],\n\nHere's your invoice! We appreciate your prompt payment.\n\n"
                         "Thanks for your business!\n[Company Name]")
PAY_LINK_FOOTER = "\n\nView and pay your invoice online:\n{link}\n"
REDIRECT_PREFIX = "[TEST REDIRECT] This e-mail would have been sent to: {to}; cc: {cc}\n\n"

# ── Microsoft Graph (04b §4) ──
GRAPH_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_SEND_URL = "https://graph.microsoft.com/v1.0/users/{user}/sendMail"
GRAPH_SCOPE = "https://graph.microsoft.com/.default"
GRAPH_STORE_KEY = "graph"
GRAPH_DEFAULT_TTL_S = 3599.0
GRAPH_MAX_PDF_BYTES = 3 * 1024 * 1024      # inline attachments ≲ 3 MB (request ≤ 4 MB)
SMTP_TIMEOUT_S = 30

NOT_VERIFIED_TEMPLATE_SWAP = (
    "The template_swap e-mail method has not been verified for this company. Run `eta probe qbo --send-test "
    "--invoice <test invoice>` (04b §10) or set QB_EMAIL_STRATEGY=graph."
)
NO_INVOICE_MESSAGE = ("QuickBooks Preferences has no EmailMessagesPrefs.InvoiceMessage — template_swap cannot work; "
                      "use graph/smtp")
REDIRECT_NOT_ALLOWED = (
    "QB_EMAIL_REDIRECT_TO cannot be used with template_swap on real invoices (QuickBooks would overwrite the "
    "customer's e-mail). Use graph/smtp for shadow runs."
)


async def _sleep(seconds: float) -> None:
    """Indirection so tests can make the restore back-off instant."""
    await asyncio.sleep(seconds)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Types (04b §6.1)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class SendReceipt:
    strategy: str                       # "template_swap" | "graph" | "smtp"
    invoice_id: str
    doc_number: str
    subject: str
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    sent_at: datetime | None = None     # local time the send call returned
    qbo_delivery_time: str | None = None   # Invoice.DeliveryInfo.DeliveryTime (template_swap)
    qbo_marked_sent: bool = False       # graph/smtp: EmailStatus update succeeded
    redirected_to: str | None = None
    outcome_inferred: bool = False      # response lost, but QBO shows the delivery


@runtime_checkable
class InvoiceEmailer(Protocol):
    name: str

    async def preflight(self, *, dry_run: bool = False) -> None: ...

    async def send(self, inv: InvoiceSnapshot, *, subject: str) -> SendReceipt: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TemplateText:
    subject: str
    message: str | None


@dataclass(frozen=True, slots=True)
class OutgoingMail:
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    reply_to: str | None
    subject: str
    text_body: str
    attachments: tuple[tuple[str, str, bytes], ...]   # (filename, content_type, data)
    headers: Mapping[str, str] = field(default_factory=dict)   # "x-eta-invoice-id", "x-eta-doc-number"


@runtime_checkable
class MailTransport(Protocol):
    name: str

    async def send(self, mail: OutgoingMail) -> str | None: ...   # provider message id if known

    async def aclose(self) -> None: ...


class _SubjectNotStored(InvoiceSendError):
    """QuickBooks answered the Preferences write with a different subject (04b §6.2 _write_subject)."""

    def __init__(self, message: str, *, stored: str | None) -> None:
        super().__init__(QBO_SERVICE, message)
        self.stored = stored


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
def validate_send(qbo: QboClient, inv: InvoiceSnapshot, subject: str) -> str:
    """04b §6.1 common send validation (before any network call); returns the stripped subject."""
    if qbo.read_only:
        raise RuntimeError(READ_ONLY_MESSAGE)
    text = (subject or "").strip()
    if not text or "\r" in text or "\n" in text:
        raise InvoiceSendError("invalid subject")
    if not inv.recipients:
        raise InvoiceSendError("Invoice has no customer e-mail address (BillEmail)")
    return text


def _invoice_message(prefs: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if not isinstance(prefs, Mapping):
        return None
    messages = prefs.get("EmailMessagesPrefs")
    invoice = messages.get("InvoiceMessage") if isinstance(messages, Mapping) else None
    return invoice if isinstance(invoice, Mapping) else None


def _get_subject(prefs: Mapping[str, Any] | None) -> str | None:
    value = (_invoice_message(prefs) or {}).get("Subject")
    return value if isinstance(value, str) else None


def _get_message(prefs: Mapping[str, Any] | None) -> str | None:
    value = (_invoice_message(prefs) or {}).get("Message")
    return value if isinstance(value, str) else None


def _with_subject(prefs: Mapping[str, Any], subject: str) -> dict[str, Any]:
    """Deep copy of the full Preferences object with only InvoiceMessage.Subject replaced."""
    body = copy.deepcopy(dict(prefs))
    body["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] = subject
    return body


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file in the same directory, fsync, os.replace), 0600 on POSIX."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    if os.name == "posix":
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)


def manual_restore_instructions(original_subject: str, path: Path) -> str:
    """04b §6.2 step 1 manual instructions (used whenever the template could not be restored)."""
    return (f"In QuickBooks open Settings → Account and settings → Sales → Messages (or Custom form styles → "
            f"Emails) and set the invoice e-mail subject back to: '{original_subject}'. Then delete {path}.")


def load_verifications(state_dir: Path) -> list[dict[str, Any]]:
    """Records of `qbo_email_probe.json` (a JSON array; a single object or JSON lines are tolerated)."""
    path = Path(state_dir) / VERIFIED_FILE_NAME
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        log.warning("Cannot read %s (%s) — treating the e-mail method as not verified", path, type(exc).__name__)
        return []
    text = text.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except ValueError:
                log.warning("Skipping an unreadable line in %s", path)
                continue
            if isinstance(item, dict):
                records.append(item)
        return records
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    if isinstance(data, dict):
        inner = data.get("records")
        if isinstance(inner, list):
            return [r for r in inner if isinstance(r, dict)]
        return [data]
    return []


def is_verified(state_dir: Path | None, realm_id: str, strategy: str) -> bool:
    """04b §6.2 step 2: the newest record for this realm + strategy says `"verified": true`.

    (A later failed send-test therefore revokes an earlier success — the safer reading.)"""
    if state_dir is None:
        return False
    latest: dict[str, Any] | None = None
    for record in load_verifications(state_dir):
        if str(record.get("realm_id")) == str(realm_id) and record.get("strategy") == strategy:
            latest = record
    return latest is not None and latest.get("verified") is True


def record_verification(state_dir: Path, *, realm_id: str, strategy: str, verified: bool,
                        subject_sent: str, invoice_id: str, when: datetime | None = None) -> Path:
    """Append a 04b §10.3 record {"realm_id", "strategy", "verified", "verified_at", "subject_sent",
    "invoice_id"} to qbo_email_probe.json (atomic, 0600). Used by `eta probe qbo --send-test`."""
    path = Path(state_dir) / VERIFIED_FILE_NAME
    records = load_verifications(state_dir)
    records.append({
        "realm_id": str(realm_id),
        "strategy": strategy,
        "verified": bool(verified),
        "verified_at": (when or SystemClock().now()).isoformat(timespec="seconds"),
        "subject_sent": subject_sent,
        "invoice_id": str(invoice_id),
    })
    _atomic_write_json(path, records)
    return path


def _parse_delivery_time(value: str | None) -> datetime | None:
    """ISO DeliveryTime → naive local time (an offset is converted with astimezone())."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


_NEVER_REACHED_CAUSES: tuple[type[BaseException], ...] = (httpx.ConnectError, httpx.ConnectTimeout,
                                                          httpx.PoolTimeout)


def _send_never_reached(exc: BaseException) -> bool:
    """True when a failed /send certainly did not e-mail anyone (explicit refusal / never connected)."""
    if isinstance(exc, (AuthError, RuntimeError)):
        return True
    if isinstance(exc, ServiceError):
        if exc.service.endswith("-oauth"):   # the token call failed: the send itself was never accepted
            return True
        if exc.status is not None:
            return exc.status in (400, 401, 403, 404, 429, 503)
        return isinstance(exc.__cause__, _NEVER_REACHED_CAUSES)
    return False


def _outcome_unknown(exc: InvoiceSendError) -> InvoiceSendError:
    """Mark a failed send whose request may have reached the provider (04b §7): the e-mail may have gone out, so the
    pipeline records it as possibly e-mailed and the same-day guard blocks a resend until --force."""
    exc.outcome_unknown = True
    return exc


def _unusable_recovery_file[E: EmailPathUnavailableError](exc: E) -> E:
    """Mark an error about a recovery file this company cannot use — another company's, or unreadable (review fix
    NEW-03): only a run that e-mails through template_swap stops for it; any other run warns and goes on."""
    exc.recovery_file_unusable = True
    return exc


def _swap_redirect(settings: QboSettings, inv: InvoiceSnapshot) -> tuple[str | None, str | None]:
    """The 04b §3 redirect rule of a template_swap send: (sendTo, refusal). QBO's `sendTo` overwrites the invoice's
    customer e-mail, so a redirect is allowed only on the sandbox or when BillEmail already is the redirect."""
    redirect = (settings.email_redirect_to or "").strip()
    if not redirect:
        return None, None
    if settings.sandbox or {r.lower() for r in inv.recipients} == {redirect.lower()}:
        return redirect, None
    return None, REDIRECT_NOT_ALLOWED


def redirect_refusal(settings: QboSettings, inv: InvoiceSnapshot) -> str | None:
    """Review fix QBO-03 — the 04b §3 redirect rule as a pure check: why a send of this invoice would be refused
    because of QB_EMAIL_REDIRECT_TO, else None (no redirect, graph/smtp, the sandbox, or a BillEmail that already is
    the redirect address). The pipeline applies it while planning (10 §4 guards), so such a row fails before its
    DocNumber is suffixed (04 §6.6); TemplateSwapEmailer.send() applies the same rule in its step 1."""
    if settings.email_strategy != "template_swap":
        return None
    return _swap_redirect(settings, inv)[1]


def pdf_file_name(doc_number: str) -> str:
    """04b §6.3 step 2: Invoice_<DocNumber with unsafe characters replaced by _>.pdf"""
    return f"Invoice_{re.sub(r'[^A-Za-z0-9._-]+', '_', doc_number)}.pdf"


def _dedupe(addresses: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for address in addresses:
        a = address.strip()
        if a and a.casefold() not in seen:
            seen.add(a.casefold())
            out.append(a)
    return tuple(out)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Body rendering (04b §6.3 step 4)
# ════════════════════════════════════════════════════════════════════════════════════════════════
_PLACEHOLDER_RE = re.compile(r"\[([^\[\]\r\n]{1,80})\]")


def render_body(template: str, inv: InvoiceSnapshot, *, company_name: str | None, link: str | None,
                on_unknown: Callable[[str], None] | None = None) -> str:
    """Replace QBO placeholders case-insensitively; unknown `[...]` tokens are left (and reported through
    `on_unknown`); a pay link is appended when the template has no [Invoice Link]."""
    customer = inv.customer_name or ""
    values = {
        "customer name": customer,
        "customer": customer,
        "company name": company_name or "",
        "invoice no.": inv.doc_number,
        "invoice number": inv.doc_number,
        "invoice amount": f"${inv.total_amount:,.2f}",
        "balance due": f"${inv.balance:,.2f}",
        "due date": inv.due_date.strftime("%m/%d/%Y") if inv.due_date else "",
        "invoice link": link or "",
    }
    had_link = False

    def substitute(match: re.Match[str]) -> str:
        nonlocal had_link
        key = " ".join(match.group(1).split()).casefold()
        if key == "invoice link":
            had_link = True
        if key in values:
            return values[key]
        if on_unknown is not None:
            on_unknown(match.group(0))
        return match.group(0)

    body = _PLACEHOLDER_RE.sub(substitute, template)
    if link and not had_link:
        body += PAY_LINK_FOOTER.format(link=link)
    return body


# ════════════════════════════════════════════════════════════════════════════════════════════════
# TemplateSwapEmailer (04b §6.2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
class TemplateSwapEmailer:
    """Swap the company's invoice e-mail subject around each /send, under a global lock."""

    name = "template_swap"

    def __init__(self, qbo: QboClient, *, state_dir: Path, clock: Clock) -> None:
        self._qbo = qbo
        self._s = qbo.settings
        self._clock = clock
        self._lock = asyncio.Lock()            # exactly one emailer per process (QboClient creates it once)
        self._preflight_lock = asyncio.Lock()
        self._prefs: dict[str, Any] | None = None   # latest full Preferences
        self._original: TemplateText | None = None
        self._broken: str | None = None
        self._ready = False
        self._restore_task: asyncio.Task[None] | None = None
        self.recovery_path = Path(state_dir) / RECOVERY_FILE_NAME
        self.verified_path = Path(state_dir) / VERIFIED_FILE_NAME

    @property
    def broken(self) -> str | None:
        """Manual-restore instructions once a restore failed for good (then every send refuses)."""
        return self._broken

    @property
    def original(self) -> TemplateText | None:
        return self._original

    # ── files ──
    def _load_recovery(self) -> dict[str, Any] | None:
        """The recovery record, or None when there is no file. An unreadable file raises TemplateRestoreError marked
        `recovery_file_unusable` (review fix NEW-03)."""
        try:
            text = self.recovery_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError) as exc:
            raise _unusable_recovery_file(TemplateRestoreError(
                f"The e-mail template recovery file {self.recovery_path} cannot be read ({type(exc).__name__}). "
                "Check the invoice e-mail subject in QuickBooks (Settings → Account and settings → Sales → "
                "Messages), then delete the file.")) from exc
        try:
            data = json.loads(text)
        except ValueError as exc:
            raise _unusable_recovery_file(TemplateRestoreError(
                f"The e-mail template recovery file {self.recovery_path} is not valid JSON. Check the invoice "
                "e-mail subject in QuickBooks (Settings → Account and settings → Sales → Messages), then delete "
                "the file.")) from exc
        if not isinstance(data, dict):
            raise _unusable_recovery_file(TemplateRestoreError(
                f"The e-mail template recovery file {self.recovery_path} has an unexpected format. Check the "
                "invoice e-mail subject in QuickBooks, then delete the file."))
        return data

    def _write_recovery(self, subject: str, invoice_id: str) -> None:
        assert self._original is not None
        data = {
            "realm_id": self._s.realm_id,
            "original_subject": self._original.subject,
            "original_message": self._original.message,
            "swapped_subject": subject,
            "invoice_id": invoice_id,
            "written_at": self._clock.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
        }
        try:
            _atomic_write_json(self.recovery_path, data)
        except OSError as exc:
            raise EmailPathUnavailableError(
                f"Cannot write the e-mail template recovery file {self.recovery_path} ({exc}); without it a crash "
                "could leave the QuickBooks template modified, so no invoice e-mail is sent") from exc

    def _delete_recovery(self) -> None:
        try:
            self.recovery_path.unlink(missing_ok=True)
        except OSError as exc:
            log.warning("Could not delete %s (%s); the next run will check the template and delete it",
                        self.recovery_path, type(exc).__name__)

    # ── Preferences writes ──
    def _subject_body(self, subject: str) -> dict[str, Any]:
        prefs = self._prefs or {}
        if self._s.prefs_update_mode == "sparse":
            message = self._original.message if self._original is not None else _get_message(prefs)
            invoice_message: dict[str, Any] = {"Subject": subject}
            if message is not None:
                invoice_message["Message"] = message
            return {"Id": prefs.get("Id"), "SyncToken": prefs.get("SyncToken"), "sparse": True,
                    "EmailMessagesPrefs": {"InvoiceMessage": invoice_message}}
        if _invoice_message(prefs) is None:
            raise EmailPathUnavailableError(NO_INVOICE_MESSAGE)
        return _with_subject(prefs, subject)     # full mode: the whole object as read (E9 full update)

    async def _write_subject(self, subject: str, *, swap_for: str | None = None) -> dict[str, Any]:
        """Write the template subject (04b §6.2): 5010 → re-read + retry once; verify the stored value.

        `swap_for` (the invoice id) marks the swap write of step 3.3: a subject a person saved meanwhile, revealed
        by the 5010 re-read, is adopted as the subject to restore (the step 3.1 rule)."""
        result: dict[str, Any] | None = None
        for attempt in (1, 2):
            body = self._subject_body(subject)
            try:
                result = await self._qbo.update_preferences(body)
            except QboFaultError as exc:
                if attempt == 1 and "5010" in exc.codes:
                    log.info("QuickBooks Preferences changed meanwhile (stale SyncToken) — re-reading and retrying "
                             "once")
                    self._prefs = await self._qbo.get_preferences()
                    if swap_for is not None:
                        self._adopt_person_edit(subject, swap_for)
                    continue
                raise
            break
        assert result is not None
        self._prefs = result
        if _invoice_message(result) is None:
            # The answer did not echo the e-mail messages: verify with a fresh read instead.
            self._prefs = await self._qbo.get_preferences()
        got = _get_subject(self._prefs)
        if got != subject:
            raise _SubjectNotStored(
                f"QuickBooks stored the template subject as {got!r} instead of {subject!r} (length limit?)",
                stored=got)
        return self._prefs

    # ── preflight (04b §6.2) ──
    async def preflight(self, *, dry_run: bool = False) -> None:
        """Crash recovery, verification gate, template read and sanity check. Idempotent once it succeeded
        for real; a dry run never writes."""
        async with self._preflight_lock:
            if self._broken:
                raise TemplateRestoreError(self._broken)
            if self._ready:
                return
            # 1. Recovery
            recovered: TemplateText | None = None
            prefs: dict[str, Any] | None = None       # a dry run never keeps what it read in self._prefs
            record = self._checked_recovery_record()
            if record is not None:
                original_subject = str(record["original_subject"])
                if dry_run:
                    log.warning("A previous run left the QuickBooks invoice e-mail subject modified ('%s'); it will "
                                "be restored automatically on the next real run.", record.get("swapped_subject"))
                    # Judge the template by the subject the real run would restore (no write in a dry run).
                    message = record.get("original_message")
                    recovered = TemplateText(original_subject, message if isinstance(message, str) else None)
                else:
                    await self._recover(record, original_subject)
                    prefs = self._prefs                 # just read (and restored) by the recovery
            # 2. Verification gate
            if not dry_run and self._s.email_require_verified and not is_verified(
                    self.verified_path.parent, self._s.realm_id, self.name):
                raise EmailPathUnavailableError(NOT_VERIFIED_TEMPLATE_SWAP)
            # 3. Read the template
            if prefs is None:
                try:
                    prefs = await self._qbo.get_preferences()
                except (ServiceError, AuthError) as exc:
                    raise EmailPathUnavailableError(
                        f"Could not read the QuickBooks invoice e-mail template (Preferences): {exc}") from exc
            if _invoice_message(prefs) is None:
                raise EmailPathUnavailableError(NO_INVOICE_MESSAGE)
            current = _get_subject(prefs)
            if recovered is None and not current:
                raise EmailPathUnavailableError("The QuickBooks invoice e-mail template has no subject — "
                                                "template_swap cannot restore it; use graph/smtp")
            original = recovered or TemplateText(current or "", _get_message(prefs))
            # 4. Sanity
            if ETA_SUBJECT_RE.match(original.subject):
                raise EmailPathUnavailableError(
                    f"The company's invoice e-mail subject currently looks like an ETA subject ({original.subject}) "
                    "— a previous run may have crashed without a recovery file. Restore the normal subject in "
                    "QuickBooks first.")
            if not dry_run:
                self._prefs = prefs
                self._original = original
                self._ready = True

    def _checked_recovery_record(self) -> dict[str, Any] | None:
        """The recovery record of this company with a usable original subject, or None when there is none. A file this
        company cannot use (another company's, unreadable, no original subject) raises an error marked
        `recovery_file_unusable` (review fix NEW-03)."""
        record = self._load_recovery()
        if record is None:
            return None
        if str(record.get("realm_id")) != str(self._s.realm_id):
            raise _unusable_recovery_file(EmailPathUnavailableError(
                f"recovery file belongs to another QuickBooks company: {self.recovery_path}"))
        original_subject = record.get("original_subject")
        if not isinstance(original_subject, str) or not original_subject:
            raise _unusable_recovery_file(TemplateRestoreError(
                f"The recovery file {self.recovery_path} has no original subject — check the QuickBooks invoice "
                "e-mail subject manually, then delete the file."))
        return record

    async def recover(self) -> bool:
        """Only preflight step 1 (crash recovery) — `eta probe qbo --restore-template` (04b §10.5).

        Returns True when a recovery file was found (the template is then restored and the file deleted), False
        when there was nothing to do. Raises TemplateRestoreError (with manual instructions) on failure."""
        async with self._preflight_lock:
            record = self._checked_recovery_record()
            if record is None:
                return False
            await self._recover(record, str(record["original_subject"]))
            return True

    async def _recover(self, record: Mapping[str, Any], original_subject: str) -> None:
        """Preflight step 1 (real run): restore the subject a crashed run left behind, delete the file."""
        message = record.get("original_message")
        try:
            self._prefs = await self._qbo.get_preferences()
            if _get_subject(self._prefs) != original_subject:
                log.warning("Restoring the QuickBooks invoice e-mail subject left modified by a previous run "
                            "('%s' → '%s')", _get_subject(self._prefs), original_subject)
                self._original = TemplateText(original_subject,
                                              message if isinstance(message, str) else _get_message(self._prefs))
                await self._write_subject(original_subject)
            self._delete_recovery()
        except Exception as exc:
            self._broken = manual_restore_instructions(original_subject, self.recovery_path)
            raise TemplateRestoreError(self._broken) from exc
        log.info("The QuickBooks invoice e-mail subject is '%s' (recovery file cleared)", original_subject)

    # ── send (04b §6.2) ──
    def _redirect_target(self, inv: InvoiceSnapshot) -> str | None:
        """04b §3 redirect rule for template_swap (QBO's sendTo overwrites the customer's e-mail)."""
        send_to, refusal = _swap_redirect(self._s, inv)
        if refusal:
            raise EmailPathUnavailableError(refusal)
        return send_to

    def _check_template_unchanged(self) -> None:
        """Step 3.1: adopt a subject a person changed during the run; refuse if it looks like ours."""
        assert self._original is not None
        current = _get_subject(self._prefs)
        if current == self._original.subject:
            return
        if not current:
            raise EmailPathUnavailableError(NO_INVOICE_MESSAGE)
        if ETA_SUBJECT_RE.match(current):
            self._broken = manual_restore_instructions(self._original.subject, self.recovery_path)
            raise TemplateRestoreError(f"The QuickBooks invoice e-mail subject is unexpectedly '{current}'. "
                                       + self._broken)
        log.warning("The QuickBooks invoice e-mail subject was changed during this run (now '%s'); it is kept as "
                    "the subject to restore", current)
        self._original = TemplateText(current, _get_message(self._prefs))

    def _adopt_person_edit(self, swapped_subject: str, invoice_id: str) -> None:
        """Step 3.1 applied after a 5010 re-read during the swap: keep a person's new subject (and record it in
        the recovery file) instead of restoring the stale one. An ETA-looking subject is left to the restore."""
        assert self._original is not None
        current = _get_subject(self._prefs)
        if not current or current == self._original.subject or ETA_SUBJECT_RE.match(current):
            return
        log.warning("The QuickBooks invoice e-mail subject was changed during this run (now '%s'); it is kept as "
                    "the subject to restore", current)
        self._original = TemplateText(current, _get_message(self._prefs))
        self._write_recovery(swapped_subject, invoice_id)

    async def _restore(self) -> None:
        """Write the original subject back: up to 3 attempts, re-reading Preferences before each retry."""
        assert self._original is not None
        original = self._original.subject
        last_exc: BaseException | None = None
        for attempt in range(1, RESTORE_ATTEMPTS + 1):
            try:
                if attempt > 1:
                    await _sleep(RESTORE_DELAYS_S[attempt - 2])
                    self._prefs = await self._qbo.get_preferences()
                    if _get_subject(self._prefs) == original:
                        self._delete_recovery()
                        return
                await self._write_subject(original)
                self._delete_recovery()
                return
            except Exception as exc:  # noqa: BLE001 - every failure is retried, then reported
                last_exc = exc
                log.warning("Restoring the QuickBooks invoice e-mail subject failed (attempt %d/%d): %s",
                            attempt, RESTORE_ATTEMPTS, exc)
        self._broken = manual_restore_instructions(original, self.recovery_path)
        log.error("The QuickBooks invoice e-mail subject could NOT be restored. %s", self._broken)
        raise TemplateRestoreError(self._broken) from last_exc

    async def _shielded_restore(self) -> None:
        """Run _restore() so that a cancellation (Ctrl+C) of the caller cannot interrupt it.

        A cancelled caller still waits for the restore to end before the cancellation goes on (review fix QBO-07):
        send() holds the template lock around this call, and a queued send must not take the lock while the swapped
        subject is still in place (04b §6.2 step 3)."""
        task = asyncio.ensure_future(self._restore())
        self._restore_task = task
        task.add_done_callback(_retrieve_exception)
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.shield(task)
            raise

    async def _after_failed_swap(self, exc: BaseException) -> None:
        """Step 3.3 failure: delete the recovery file if the template is unchanged, else restore it."""
        assert self._original is not None
        if isinstance(exc, _SubjectNotStored):
            changed = exc.stored != self._original.subject
        else:
            try:
                self._prefs = await self._qbo.get_preferences()
                changed = _get_subject(self._prefs) != self._original.subject
            except Exception:  # noqa: BLE001 - unknown state: restore (it re-reads before retrying)
                changed = True
        if changed:
            await self._shielded_restore()
        else:
            self._delete_recovery()

    async def send(self, inv: InvoiceSnapshot, *, subject: str) -> SendReceipt:
        if self._broken:                                                                    # step 1
            raise TemplateRestoreError(self._broken)
        subject = validate_send(self._qbo, inv, subject)
        send_to = self._redirect_target(inv)
        if not self._ready:                                                                 # step 2
            await self.preflight()
        async with self._lock:                                                              # step 3
            if self._broken:
                raise TemplateRestoreError(self._broken)
            self._check_template_unchanged()                                                # 3.1
            self._write_recovery(subject, inv.id)                                           # 3.2
            try:
                await self._write_subject(subject, swap_for=inv.id)                         # 3.3
            except BaseException as exc:  # incl. cancellation: never leave the swapped subject behind
                await self._after_failed_swap(exc)
                if not isinstance(exc, Exception) or isinstance(exc, InvoiceSendError):  # incl. EmailPathUnavailable
                    raise
                raise InvoiceSendError(QBO_SERVICE, f"could not set the invoice e-mail subject in QuickBooks: "
                                                    f"{exc} — e-mail not sent") from exc
            started = self._clock.now()                                                     # 3.4
            send_exc: BaseException | None = None
            response: dict[str, Any] | None = None
            restore_failure: TemplateRestoreError | None = None
            try:
                response = await self._qbo._post_send(inv.id, send_to)
            except BaseException as exc:  # incl. cancellation: restore first, re-raise afterwards
                send_exc = exc
            try:                                                                            # 3.5
                await self._shielded_restore()
            except TemplateRestoreError as restore_exc:
                if send_exc is None:
                    # The customer WAS e-mailed; say so, and keep the receipt for the caller's records.
                    error = TemplateRestoreError(
                        f"{restore_exc} (The invoice e-mail for #{inv.doc_number} WAS sent before the restore "
                        "failed.)")
                    error.receipt = self._receipt(inv, subject, send_to, response)  # type: ignore[attr-defined]
                    raise error from restore_exc.__cause__
                if not isinstance(send_exc, Exception):
                    # QBO-07: a cancellation stays a cancellation; the failed restore is already recorded in _broken
                    # (every later send is refused with the instructions) and logged.
                    raise send_exc   # the cancellation itself; the restore failure stays as its __context__
                restore_failure = restore_exc
            if send_exc is not None and not isinstance(send_exc, Exception):
                raise send_exc                                                              # cancelled
        if send_exc is not None:                                                            # step 4
            if restore_failure is not None:
                raise await self._restore_failed_after_failed_send(
                    inv, subject, send_to, started, send_exc, restore_failure) from send_exc
            return await self._infer_outcome(inv, subject, send_to, started, send_exc)
        return self._receipt(inv, subject, send_to, response)                               # step 5

    async def _restore_failed_after_failed_send(self, inv: InvoiceSnapshot, subject: str, send_to: str | None,
                                                started: datetime, send_exc: Exception,
                                                restore_exc: TemplateRestoreError) -> TemplateRestoreError:
        """Review fix QBO-02: the /send failed and the restore failed too. The read-only step-4 inference still runs,
        so a delivery QuickBooks shows is reported as sent — with its receipt, like a restore failure after a
        successful send — instead of 'No Email Sent' (which would revert the suffix and invite a second e-mail).
        Otherwise the error says what the inference found ('not sent' or 'outcome unknown')."""
        try:
            receipt = await self._infer_outcome(inv, subject, send_to, started, send_exc)
        except InvoiceSendError as inferred:
            error = TemplateRestoreError(f"{restore_exc} (Invoice #{inv.doc_number}: {inferred.message})")
            error.outcome_unknown = inferred.outcome_unknown
            return error
        error = TemplateRestoreError(
            f"{restore_exc} (The invoice e-mail for #{inv.doc_number} WAS sent before the restore failed: the send "
            "answer was lost, but QuickBooks shows the delivery.)")
        error.receipt = receipt  # type: ignore[attr-defined]
        return error

    async def _infer_outcome(self, inv: InvoiceSnapshot, subject: str, send_to: str | None,
                             started: datetime, send_exc: BaseException) -> SendReceipt:
        """Step 4: the send failed — does QuickBooks nevertheless show a fresh delivery?"""
        checked = True
        delivery: str | None = None
        try:
            check = await self._qbo.get_invoice(inv.id)
            delivery = check.delivery_time
        except Exception as exc:  # noqa: BLE001 - the original error is what matters
            checked = False
            log.warning("Could not re-read invoice %s to check whether the e-mail went out: %s", inv.id, exc)
        delivered_at = _parse_delivery_time(delivery)
        if delivered_at is not None and delivered_at >= started - INFERENCE_SLACK:
            log.warning("send response lost but QuickBooks shows delivery at %s (invoice #%s)", delivery,
                        inv.doc_number)
            return SendReceipt(
                strategy=self.name, invoice_id=inv.id, doc_number=inv.doc_number, subject=subject,
                to=(send_to,) if send_to else inv.recipients, cc=split_addresses(inv.bill_email_cc),
                bcc=split_addresses(inv.bill_email_bcc), sent_at=self._clock.now(), qbo_delivery_time=delivery,
                redirected_to=send_to, outcome_inferred=True)
        if _send_never_reached(send_exc):
            raise InvoiceSendError(QBO_SERVICE, f"invoice e-mail not sent: {send_exc}. QuickBooks shows no "
                                                "delivery — safe to retry later.") from send_exc
        if not checked:
            raise _outcome_unknown(InvoiceSendError(
                QBO_SERVICE, f"invoice e-mail outcome unknown ({send_exc}); QuickBooks could not be checked — check "
                             "the invoice before re-sending.")) from send_exc
        # timeouts / 5xx / unreadable answers: the request may have reached Intuit (04b §6.2 step 4)
        raise _outcome_unknown(InvoiceSendError(
            QBO_SERVICE, f"invoice e-mail outcome unknown ({send_exc}); QuickBooks shows no delivery yet — check the "
                         "invoice before re-sending.")) from send_exc

    def _receipt(self, inv: InvoiceSnapshot, subject: str, send_to: str | None,
                 response: Mapping[str, Any] | None) -> SendReceipt:
        """Step 5."""
        invoice = response.get("Invoice") if isinstance(response, Mapping) else None
        invoice = invoice if isinstance(invoice, Mapping) else {}
        status = invoice.get("EmailStatus")
        if status != "EmailSent":
            log.warning("QuickBooks answered the send of invoice %s (#%s) with EmailStatus=%r (expected EmailSent)",
                        inv.id, inv.doc_number, status)
        info = invoice.get("DeliveryInfo")
        delivery = info.get("DeliveryTime") if isinstance(info, Mapping) else None
        return SendReceipt(
            strategy=self.name, invoice_id=inv.id, doc_number=inv.doc_number, subject=subject,
            to=(send_to,) if send_to else inv.recipients, cc=split_addresses(inv.bill_email_cc),
            bcc=split_addresses(inv.bill_email_bcc), sent_at=self._clock.now(),
            qbo_delivery_time=delivery if isinstance(delivery, str) else None, redirected_to=send_to)

    async def aclose(self) -> None:
        """Wait for an in-flight restore; restore from the recovery file if one is still present."""
        task = self._restore_task
        if task is not None and not task.done():
            with contextlib.suppress(Exception):
                await task
        if not self.recovery_path.exists():
            return
        if self._qbo.read_only:
            log.warning("The QuickBooks invoice e-mail subject may still be modified (%s); it is restored on the "
                        "next real run.", self.recovery_path)
            return
        try:
            record = self._load_recovery()
            if record is None:
                return
            if str(record.get("realm_id")) != str(self._s.realm_id):
                log.error("%s belongs to another QuickBooks company — not touching it", self.recovery_path)
                return
            original_subject = record.get("original_subject")
            if not isinstance(original_subject, str) or not original_subject:
                log.error("%s has no original subject — check the QuickBooks invoice e-mail subject manually",
                          self.recovery_path)
                return
            message = record.get("original_message")
            self._original = TemplateText(original_subject, message if isinstance(message, str) else None)
            await self._restore()
        except Exception as exc:  # noqa: BLE001 - cleanup must not raise; the instructions are logged
            log.error("The QuickBooks invoice e-mail subject could not be restored at shutdown: %s", exc)


def _retrieve_exception(task: asyncio.Task[None]) -> None:
    """Mark a background restore's exception as retrieved (it is reported by whoever awaits it)."""
    if not task.cancelled():
        task.exception()


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Microsoft Graph (04b §4, §5)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


class GraphAuth:
    """Client-credentials TokenProvider for Microsoft Graph (04b §5); TokenStore key "graph".

    Like every token provider it never takes a service semaphore (03 §8)."""

    def __init__(self, email: EmailSettings, token_store: TokenStore | None = None, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None) -> None:
        if not (email.graph_tenant_id and email.graph_client_id and email.graph_client_secret):
            raise ConfigError("QB_EMAIL_STRATEGY=graph needs EMAIL_GRAPH_TENANT_ID, EMAIL_GRAPH_CLIENT_ID and "
                              "EMAIL_GRAPH_CLIENT_SECRET in .env")
        self._tenant = email.graph_tenant_id
        self._client_id = email.graph_client_id
        self._secret = email.graph_client_secret
        self._store = token_store
        self._http = http if http is not None else new_async_client()
        self._owns_http = http is None
        self._clock: Clock = clock or SystemClock()
        self._lock = asyncio.Lock()
        self._entry: dict[str, Any] | None = None
        self._closed = False

    def _now(self) -> float:
        return self._clock.now().timestamp()

    def _cached(self) -> dict[str, Any] | None:
        if self._entry is None and self._store is not None:
            stored = self._store.get(GRAPH_STORE_KEY)
            if stored and stored.get("tenant_id") == self._tenant and stored.get("client_id") == self._client_id:
                self._entry = stored
        return self._entry

    async def get_token(self) -> str:
        async with self._lock:
            entry = self._cached()
            if is_fresh(entry, skew_seconds=120, now=self._now()):
                assert entry is not None
                return str(entry["access_token"])
            return await self._fetch()

    async def invalidate(self) -> None:
        async with self._lock:
            entry = self._cached()
            if entry:
                self._entry = {**entry, "expires_at": 0.0}
                if self._store is not None:
                    self._store.put(GRAPH_STORE_KEY, self._entry)

    async def _fetch(self) -> str:
        url = GRAPH_TOKEN_URL.format(tenant=quote(str(self._tenant), safe=""))
        resp = await request(
            self._http, "POST", url, service="graph-oauth", expected=(200, 400, 401), idempotent=True,
            data={"client_id": self._client_id, "client_secret": self._secret, "scope": GRAPH_SCOPE,
                  "grant_type": "client_credentials"},
        )
        try:
            body = resp.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        if resp.status_code in (400, 401):
            description = str(body.get("error_description") or body.get("error") or f"HTTP {resp.status_code}")
            first_line = description.strip().splitlines()[0] if description.strip() else description
            raise AuthError(f"Microsoft Graph rejected EMAIL_GRAPH_* credentials: {redact(first_line)}")
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise AuthError("Microsoft Graph token response had no access_token")
        now = self._now()
        expires_in = _number(body.get("expires_in"))
        expires_at = now + expires_in if expires_in is not None else (jwt_exp(token) or now + GRAPH_DEFAULT_TTL_S)
        entry = {"access_token": token, "expires_at": float(expires_at), "tenant_id": self._tenant,
                 "client_id": self._client_id, "updated_at": now}
        if self._store is not None:
            self._store.put(GRAPH_STORE_KEY, entry)
        self._entry = entry
        return token

    async def aclose(self) -> None:
        if self._owns_http and not self._closed:
            self._closed = True
            await self._http.aclose()


def graph_payload(mail: OutgoingMail) -> dict[str, Any]:
    """sendMail JSON body (04b §4)."""
    def recipients(addresses: tuple[str, ...]) -> list[dict[str, Any]]:
        return [{"emailAddress": {"address": a}} for a in addresses]

    message: dict[str, Any] = {
        "subject": mail.subject,
        "body": {"contentType": "Text", "content": mail.text_body},
        "toRecipients": recipients(mail.to),
    }
    if mail.cc:
        message["ccRecipients"] = recipients(mail.cc)
    if mail.bcc:
        message["bccRecipients"] = recipients(mail.bcc)
    if mail.reply_to:
        message["replyTo"] = recipients((mail.reply_to,))
    if mail.attachments:
        message["attachments"] = [
            {"@odata.type": "#microsoft.graph.fileAttachment", "name": name, "contentType": content_type,
             "contentBytes": base64.b64encode(data).decode("ascii")}
            for name, content_type, data in mail.attachments
        ]
    if mail.headers:
        message["internetMessageHeaders"] = [{"name": k, "value": v} for k, v in mail.headers.items()]
    return {"message": message, "saveToSentItems": True}


class GraphTransport:
    """POST /users/{sender}/sendMail → 202 (04b §4); one attempt for anything ambiguous."""

    name = "graph"

    def __init__(self, email: EmailSettings, auth: GraphAuth, *, http: httpx.AsyncClient | None = None) -> None:
        self._email = email
        self._auth = auth
        self._http = http if http is not None else new_async_client()
        self._owns_http = http is None
        self._closed = False

    async def verify(self) -> None:
        """Preflight: obtain a Graph token (04b §6.3)."""
        await self._auth.get_token()

    async def send(self, mail: OutgoingMail) -> str | None:
        url = GRAPH_SEND_URL.format(user=quote(mail.sender, safe="@"))
        try:
            await self._auth.get_token()   # usually cached; a failure here means nothing was sent
            await request_with_auth(self._http, "POST", url, service="graph", auth=self._auth,
                                    json=graph_payload(mail), expected=(202,), idempotent=False)
        except ServiceError as exc:
            if exc.service == "graph-oauth":   # token endpoint failed (also after a 401 from sendMail)
                raise InvoiceSendError("graph", f"e-mail not sent (no Microsoft Graph token): {exc}") from exc
            if exc.status in (403, 404):
                raise EmailPathUnavailableError(
                    f"Microsoft Graph refused to send as {mail.sender} (HTTP {exc.status}) — check EMAIL_FROM and the "
                    f"app's Mail.Send permission / mailbox scope: {exc}") from exc
            if (exc.status is not None and (400 <= exc.status < 500 or exc.status == 503)) or (
                    exc.status is None and isinstance(exc.__cause__, _NEVER_REACHED_CAUSES)):
                raise InvoiceSendError("graph", f"e-mail not sent: {exc}", request_id=exc.request_id) from exc
            raise _outcome_unknown(InvoiceSendError(
                f"e-mail outcome unknown — check the Sent Items of {mail.sender} before re-sending",
                request_id=exc.request_id)) from exc
        return None  # sendMail returns 202 with an empty body: no message id

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._auth.aclose()
        finally:
            if self._owns_http:
                await self._http.aclose()


# ════════════════════════════════════════════════════════════════════════════════════════════════
# SMTP (04b §4)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def build_mime_message(mail: OutgoingMail) -> EmailMessage:
    """RFC 5322 message: From/To/Cc/Reply-To/Subject/Date/Message-ID + headers; Bcc is NOT a header."""
    msg = EmailMessage()
    msg["From"] = mail.sender
    msg["To"] = ", ".join(mail.to)
    if mail.cc:
        msg["Cc"] = ", ".join(mail.cc)
    if mail.reply_to:
        msg["Reply-To"] = mail.reply_to
    msg["Subject"] = mail.subject
    msg["Date"] = formatdate(localtime=True)
    domain = mail.sender.rpartition("@")[2].strip() or "localhost"
    msg["Message-ID"] = make_msgid(domain=domain)   # explicit domain: no DNS lookup
    for name, value in mail.headers.items():
        msg[name] = value
    msg.set_content(mail.text_body)
    for filename, content_type, data in mail.attachments:
        maintype, _, subtype = content_type.partition("/")
        msg.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                           filename=filename)
    return msg


class SmtpTransport:
    """smtplib inside asyncio.to_thread; one connection per message (04b §4)."""

    name = "smtp"

    def __init__(self, email: EmailSettings) -> None:
        if not email.smtp_host:
            raise ConfigError("QB_EMAIL_STRATEGY=smtp needs EMAIL_SMTP_HOST in .env")
        self._email = email

    def _connect(self) -> smtplib.SMTP:
        e = self._email
        context = ssl.create_default_context()
        if e.smtp_security == "ssl":
            return smtplib.SMTP_SSL(e.smtp_host or "", e.smtp_port, timeout=SMTP_TIMEOUT_S, context=context)
        conn = smtplib.SMTP(e.smtp_host or "", e.smtp_port, timeout=SMTP_TIMEOUT_S)
        if e.smtp_security == "starttls":
            try:
                conn.starttls(context=context)
            except BaseException:
                _close_quietly(conn)
                raise
        return conn

    def _login(self, conn: smtplib.SMTP) -> None:
        if self._email.smtp_username and self._email.smtp_password is not None:
            try:
                conn.login(self._email.smtp_username, self._email.smtp_password)
            except smtplib.SMTPAuthenticationError as exc:
                raise AuthError(f"The SMTP server rejected EMAIL_SMTP_USERNAME / EMAIL_SMTP_PASSWORD "
                                f"(SMTP {exc.smtp_code})") from exc

    def _verify_sync(self) -> None:
        conn = self._connect()
        try:
            self._login(conn)
        finally:
            _quit_quietly(conn)

    async def verify(self) -> None:
        """Preflight: open + log in + close (04b §6.3)."""
        await asyncio.to_thread(self._verify_sync)

    def _send_sync(self, mail: OutgoingMail) -> str | None:
        msg = build_mime_message(mail)
        recipients = list(dict.fromkeys(mail.to + mail.cc + mail.bcc))   # to_addrs = to + cc + bcc
        try:
            conn = self._connect()
        except (OSError, smtplib.SMTPException) as exc:
            raise InvoiceSendError("smtp", f"e-mail not sent (cannot connect to {self._email.smtp_host}): "
                                           f"{redact(repr(exc))}") from exc
        try:
            try:
                self._login(conn)
            except AuthError:
                raise
            except (OSError, smtplib.SMTPException) as exc:
                raise InvoiceSendError("smtp", f"e-mail not sent (login failed): {redact(repr(exc))}") from exc
            try:
                refused = conn.send_message(msg, to_addrs=recipients)
            except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
                    smtplib.SMTPNotSupportedError, smtplib.SMTPHeloError) as exc:
                raise InvoiceSendError("smtp", f"e-mail not sent (rejected by the server): "
                                               f"{redact(repr(exc))}") from exc
            except (OSError, smtplib.SMTPException) as exc:   # e.g. timeout / disconnect during DATA
                raise _outcome_unknown(InvoiceSendError(
                    f"e-mail outcome unknown — check the Sent Items of {mail.sender} before re-sending")) from exc
        finally:
            _quit_quietly(conn)
        if refused:
            log.warning("The SMTP server refused some recipients of invoice #%s: %s",
                        mail.headers.get("x-eta-doc-number", "?"), ", ".join(sorted(refused)))
        message_id = msg["Message-ID"]
        return str(message_id) if message_id else None

    async def send(self, mail: OutgoingMail) -> str | None:
        return await asyncio.to_thread(self._send_sync, mail)

    async def aclose(self) -> None:
        return None


def _quit_quietly(conn: smtplib.SMTP) -> None:
    try:
        conn.quit()
    except (OSError, smtplib.SMTPException):
        _close_quietly(conn)


def _close_quietly(conn: smtplib.SMTP) -> None:
    with contextlib.suppress(OSError, smtplib.SMTPException):
        conn.close()


# ════════════════════════════════════════════════════════════════════════════════════════════════
# MailboxEmailer (04b §6.3)
# ════════════════════════════════════════════════════════════════════════════════════════════════
class MailboxEmailer:
    """Send from the company mailbox (graph/smtp) with the QBO PDF and pay link.

    DEVIATION (04b §7, graph/smtp only): the e-mail comes from the company mailbox instead of QuickBooks'
    mail service, and QBO's own delivery tracking (DeliveryInfo, "Sent" activity) is not recorded."""

    def __init__(self, qbo: QboClient, email: EmailSettings, transport: MailTransport, *, clock: Clock) -> None:
        self._qbo = qbo
        self._s = qbo.settings
        self._email = email
        self._transport = transport
        self._clock = clock
        self.name = transport.name
        self._sem = asyncio.Semaphore(max(1, int(self._s.email_max_concurrency)))
        self._limiter = AsyncLimiter(max(1, int(self._s.email_max_per_minute)), 60.0)
        self._preflight_lock = asyncio.Lock()
        self._ready = False
        self._company_name: str | None = None
        self._qbo_template: str | None = None
        self._file_template: str | None = None
        self._warned_tokens: set[str] = set()
        self._warned_no_link = False

    @property
    def transport(self) -> MailTransport:
        return self._transport

    def _warn_unknown(self, token: str) -> None:
        if token not in self._warned_tokens:
            self._warned_tokens.add(token)
            log.warning("Unknown placeholder %s in the invoice e-mail body — left as is", token)

    async def preflight(self, *, dry_run: bool = False) -> None:
        async with self._preflight_lock:
            if self._ready:
                return
            if self._email.body_template is not None and self._file_template is None:
                path = Path(self._email.body_template).expanduser()
                if not path.is_file():
                    raise ConfigError(f"EMAIL_BODY_TEMPLATE file not found: {path}")
                self._file_template = path.read_text(encoding="utf-8-sig")
            if not dry_run and self._s.email_require_verified and not is_verified(
                    self._qbo.state_dir, self._s.realm_id, self.name):
                raise EmailPathUnavailableError(
                    f"The {self.name} e-mail method has not been verified for this company. Run `eta probe qbo "
                    "--send-test --invoice <test invoice>` (04b §10).")
            if not dry_run:
                verify = getattr(self._transport, "verify", None)
                if verify is not None:
                    try:
                        await verify()
                    except EmailPathUnavailableError:
                        raise
                    except AuthError as exc:
                        raise EmailPathUnavailableError(str(exc)) from exc
                    except (ServiceError, OSError, smtplib.SMTPException) as exc:
                        raise EmailPathUnavailableError(f"Cannot reach the {self.name} mail service: {exc}") from exc
            try:
                info = await self._qbo.get_company_info()
                self._company_name = str(info.get("CompanyName") or "").strip() or None
                self._qbo_template = _get_message(await self._qbo.get_preferences())
            except (ServiceError, AuthError) as exc:
                raise EmailPathUnavailableError(
                    f"Could not read the company name / invoice e-mail template from QuickBooks: {exc}") from exc
            if not dry_run:
                self._ready = True

    async def send(self, inv: InvoiceSnapshot, *, subject: str) -> SendReceipt:
        subject = validate_send(self._qbo, inv, subject)                                   # step 1
        if not self._ready:
            await self.preflight()
        redirect = (self._s.email_redirect_to or "").strip() or None
        to = inv.recipients
        cc = split_addresses(inv.bill_email_cc)
        bcc = _dedupe(list(split_addresses(inv.bill_email_bcc)) + list(self._email.bcc))
        prefix = ""
        if redirect:
            prefix = REDIRECT_PREFIX.format(to=", ".join(to), cc=", ".join(cc))
            to, cc, bcc = (redirect,), (), ()
        attachments: tuple[tuple[str, str, bytes], ...] = ()
        if self._email.attach_pdf:                                                         # step 2
            try:
                pdf = await self._qbo.get_invoice_pdf(inv.id)
            except Exception as exc:  # noqa: BLE001 - never send without the invoice
                raise InvoiceSendError(QBO_SERVICE, f"could not download the invoice PDF from QuickBooks: {exc} — "
                                                    "e-mail not sent") from exc
            if self.name == "graph" and len(pdf) > GRAPH_MAX_PDF_BYTES:
                raise InvoiceSendError("PDF too large for a Graph inline attachment")
            attachments = ((pdf_file_name(inv.doc_number), "application/pdf", pdf),)
        link: str | None = None
        if self._email.include_pay_link:                                                   # step 3
            try:
                link = await self._qbo.get_invoice_link(inv.id)
            except Exception as exc:  # noqa: BLE001 - the pay link is optional content
                log.warning("Could not read the pay link of invoice %s: %s — sending without it", inv.id, exc)
            if link is None and not self._warned_no_link:
                self._warned_no_link = True
                log.info("QuickBooks returned no InvoiceLink (online payments off?) — e-mails will not contain a pay "
                         "link")
        template = self._file_template or self._qbo_template or DEFAULT_BODY_TEMPLATE      # step 4
        body = prefix + render_body(template, inv, company_name=self._company_name, link=link,
                                    on_unknown=self._warn_unknown)
        mail = OutgoingMail(sender=self._email.sender, to=to, cc=cc, bcc=bcc, reply_to=self._email.reply_to,
                            subject=subject, text_body=body, attachments=attachments,
                            headers={"x-eta-invoice-id": inv.id, "x-eta-doc-number": inv.doc_number})
        async with self._sem:                                                              # step 5
            async with self._limiter:
                try:
                    await self._transport.send(mail)
                except AuthError as exc:  # credentials rejected: no e-mail can be sent this run
                    raise EmailPathUnavailableError(f"{self.name}: {exc}") from exc
        sent_at = self._clock.now()
        marked = False
        if self._s.mark_sent and not redirect and inv.email_status != "EmailSent":         # step 6
            try:
                await self._qbo.mark_email_sent(inv)
                marked = True
            except Exception as exc:  # noqa: BLE001 - never fails the send
                log.warning("could not mark invoice %s (#%s) as sent in QuickBooks (UNVERIFIED feature): %s",
                            inv.id, inv.doc_number, exc)
        return SendReceipt(strategy=self.name, invoice_id=inv.id, doc_number=inv.doc_number,  # step 7
                           subject=subject, to=to, cc=cc, bcc=bcc, sent_at=sent_at, qbo_marked_sent=marked,
                           redirected_to=redirect)

    async def aclose(self) -> None:
        await self._transport.aclose()


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Factory (04b §6.1)
# ════════════════════════════════════════════════════════════════════════════════════════════════
_REQUIRED_EMAIL_VARS = {
    "graph": ("EMAIL_FROM", "EMAIL_GRAPH_TENANT_ID", "EMAIL_GRAPH_CLIENT_ID", "EMAIL_GRAPH_CLIENT_SECRET"),
    "smtp": ("EMAIL_FROM", "EMAIL_SMTP_HOST", "EMAIL_SMTP_USERNAME", "EMAIL_SMTP_PASSWORD"),
}


def build_emailer(qbo: QboClient) -> InvoiceEmailer:
    """TemplateSwapEmailer for template_swap; MailboxEmailer(GraphTransport|SmtpTransport) for graph/smtp.
    ConfigError if the strategy needs Settings.email and it is None (message lists Settings.missing['email'])."""
    strategy = qbo.settings.email_strategy
    if strategy == "template_swap":
        if qbo.state_dir is None:
            raise ConfigError("template_swap needs ETA_STATE_DIR for its recovery file (QboClient has no state_dir)")
        return TemplateSwapEmailer(qbo, state_dir=qbo.state_dir, clock=qbo.clock)
    if strategy not in _REQUIRED_EMAIL_VARS:
        raise ConfigError(f"Unknown QB_EMAIL_STRATEGY={strategy!r} (template_swap, graph or smtp)")
    email = qbo.email_settings
    if email is None:
        missing = qbo.email_missing or _REQUIRED_EMAIL_VARS[strategy]
        raise ConfigError(f"QB_EMAIL_STRATEGY={strategy} needs these settings in .env: {', '.join(missing)} (04b §3)")
    transport: MailTransport
    if strategy == "graph":
        transport = GraphTransport(email, GraphAuth(email, qbo.token_store, clock=qbo.clock))
    else:
        transport = SmtpTransport(email)
    return MailboxEmailer(qbo, email, transport, clock=qbo.clock)

