"""CarrierClient contract shared by every carrier module (03-foundations §9).

Boundary rule (03 §5): get_eta() raises ONLY CarrierLookupError (or a subclass), NoEtaError or
CustomsHoldError. Clients catch ServiceError, AuthError, httpx.HTTPError and parsing errors
(KeyError/TypeError/ValueError) and re-raise them as CarrierLookupError (or NoEtaError when the payload
is valid but has no usable date), chaining with `raise ... from exc`.

Each carrier module exposes a factory with the CarrierClientFactory signature:
    build_client(settings, token_store, *, clock=None, http=None) -> CarrierClient | None
returning None when the carrier is not configured (required env vars missing).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..models import Carrier, EtaResult

if TYPE_CHECKING:
    import httpx

    from ..clock import Clock
    from ..config import Settings
    from ..token_store import TokenStore

__all__ = ["CarrierClient", "CarrierClientFactory", "error_text"]


@runtime_checkable
class CarrierClient(Protocol):
    carrier: Carrier

    async def get_eta(self, booking: str) -> EtaResult:
        """`booking` is the BASE booking number (suffixes removed, stripped, upper-cased).
        Never returns None: raises NoEtaError instead. Raises only CarrierLookupError / NoEtaError /
        CustomsHoldError (03 §5 boundary rule)."""
        ...

    async def probe(self, booking: str) -> dict[str, Any]:
        """Diagnostics for `eta probe <carrier>`: performs the same calls as get_eta but returns
        {"requests": [...summaries...], "raw": <payload(s), redacted>, "eta": <EtaResult as dict or error string>}."""
        ...

    async def aclose(self) -> None:
        """Close owned resources (never an injected http client)."""
        ...


class CarrierClientFactory(Protocol):
    """Signature of every carrier module's build_client() (03 §9; specs 07/08 show the shorter
    two-argument form — the keyword arguments are additive)."""

    def __call__(
        self,
        settings: Settings,
        token_store: TokenStore,
        *,
        clock: Clock | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> CarrierClient | None: ...


def error_text(exc: BaseException) -> str:
    """"<ErrorClass>: <message>" — how probes report a lookup error instead of raising (06 §6.4)."""
    return f"{type(exc).__name__}: {exc}"
