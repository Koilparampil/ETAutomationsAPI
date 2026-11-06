"""Exception hierarchy (03-foundations §5, with the additions from 04 §6.3, 04b §6.1, 09 §B.7.1 and 03 §14)."""

from __future__ import annotations

from typing import Self

from .logging_setup import redact_excerpt

__all__ = [
    "AggregatorBookingError",
    "AmbiguousInvoiceError",
    "AuthError",
    "BookingChangeError",
    "CarrierLookupError",
    "ConfigError",
    "CustomsHoldError",
    "EmailPathUnavailableError",
    "EtaAutomationError",
    "InvoiceNotFoundError",
    "InvoiceSendError",
    "InvoiceUpdateError",
    "NoEtaError",
    "NoteError",
    "RunLockedError",
    "SWBTRError",
    "ServiceError",
    "TemplateRestoreError",
    "UnsupportedCarrierError",
    "VShipBookingNotFoundError",
]

BODY_EXCERPT_MAX = 500


class EtaAutomationError(Exception):
    """Base for every error raised by this program."""


class ConfigError(EtaAutomationError):
    """Missing/invalid settings, or a missing optional package (03 §2, §12)."""


class AuthError(EtaAutomationError):
    """Credentials rejected / token unobtainable."""


class ServiceError(EtaAutomationError):
    """Unexpected HTTP status or malformed payload from an external service (03 §5).

    `ServiceError(service, message, *, status=None, body_excerpt="", request_id=None)`;
    `str(err)` → f"{service}: {message} (HTTP {status}) [request_id=...] body: {excerpt}" where the
    status / request-id / body parts are omitted when absent.

    Subclasses are also raised with a single argument (e.g. `InvoiceSendError("invalid subject")`,
    04b §6.1); that argument is then the message, there is no service prefix and `str(err)` is exactly
    the message. `body_excerpt` is passed through redact() again and capped at 500 characters.
    """

    def __init__(
        self,
        service: str = "",
        message: str | None = None,
        *,
        status: int | None = None,
        body_excerpt: str = "",
        request_id: str | None = None,
    ) -> None:
        if message is None:  # single-argument form: ServiceError("text")
            service, message = "", service
        self.service: str = service
        self.message: str = message
        self.status: int | None = status
        self.body_excerpt: str = redact_excerpt(body_excerpt or "", BODY_EXCERPT_MAX)
        self.request_id: str | None = request_id
        super().__init__(self._format())

    def _format(self) -> str:
        head = f"{self.service}: {self.message}" if self.service and self.message else (self.service or self.message)
        parts = [head] if head else []
        if self.status is not None:
            parts.append(f"(HTTP {self.status})")
        if self.request_id:
            parts.append(f"[request_id={self.request_id}]")
        text = " ".join(parts)
        if self.body_excerpt:
            text = f"{text} body: {self.body_excerpt}" if text else f"body: {self.body_excerpt}"
        return text

    def __str__(self) -> str:
        return self._format()

    @classmethod
    def wrap(cls, exc: BaseException, message: str | None = None, *, service: str | None = None) -> Self:
        """Re-type another error as this class, keeping status/body/request id when it is a ServiceError.

        Typical use: `raise NoteError.wrap(exc) from exc` (05 §6.2 wraps ServiceError/AuthError).
        """
        if isinstance(exc, ServiceError):
            return cls(
                exc.service if service is None else service,
                exc.message if message is None else message,
                status=exc.status,
                body_excerpt=exc.body_excerpt,
                request_id=exc.request_id,
            )
        return cls(service or "", str(exc) if message is None else message)


# ── carriers ──
class CarrierLookupError(EtaAutomationError):
    """Lookup failed: network, blocked, not found at carrier, carrier not configured → "Carrier Lookup Failed."."""


class UnsupportedCarrierError(CarrierLookupError):
    """Grimaldi stub, reference numbers, unknown booking pattern (01 §4)."""


class AggregatorBookingError(CarrierLookupError):
    """Per-booking aggregator state, e.g. "not tracked yet — re-run later" (09 §B.7.1).

    The circuit breaker (10 §6) must not count it: it says nothing about the provider being down.
    """


class NoEtaError(EtaAutomationError):
    """Booking found but no usable ETA → "Can't determine ETA."."""


class CustomsHoldError(EtaAutomationError):
    """MSC heuristic → PotentialCustomsHold.txt (06 §7)."""


# ── VShip ──
class VShipBookingNotFoundError(ServiceError):
    """No VShip booking matches the base booking (05 §6.2, D17)."""


class NoteError(ServiceError):
    """Posting a VShip comment failed."""


class BookingChangeError(ServiceError):
    """PATCHing the VShip booking ETA failed."""


# ── QuickBooks ──
class InvoiceNotFoundError(EtaAutomationError):
    """No QuickBooks invoice with that DocNumber / Id."""


class AmbiguousInvoiceError(InvoiceNotFoundError):
    """Several invoices share one DocNumber (04 §6.3, D21)."""


class InvoiceUpdateError(ServiceError):
    """A QuickBooks invoice update was rejected or did not persist (04 §6.5)."""


class InvoiceSendError(ServiceError):
    """An invoice e-mail could not be sent (04 §6.6, 04b).

    `outcome_unknown` is True when the request may have reached the provider (a timeout, a 5xx or a lost answer
    after the request went out): the e-mail may have been delivered, so the pipeline records it in the ledger as
    possibly e-mailed and the same-day guard blocks a resend until --force (04b §7)."""

    outcome_unknown: bool = False


class EmailPathUnavailableError(InvoiceSendError):
    """No invoice e-mail can be sent this run (04b §6.1)."""


class TemplateRestoreError(EmailPathUnavailableError):
    """The QBO e-mail template was left modified — manual restore needed (04b §6.2)."""


# ── single-instance guard ──
class RunLockedError(EtaAutomationError):
    """Another instance holds the run lock (03 §14)."""


# ── business routing signal (not a failure) ──
class SWBTRError(EtaAutomationError):
    """Partially paid + TR/SWB note → Send2Accounting.txt.

    Kept for parity/readability; the pipeline decides the ACCOUNTING route while planning and need not
    raise it (03 §5).
    """
