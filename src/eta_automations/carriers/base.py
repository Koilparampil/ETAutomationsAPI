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

import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from ..models import Carrier, EtaResult

if TYPE_CHECKING:
    import httpx

    from ..clock import Clock
    from ..config import Settings
    from ..token_store import TokenStore

__all__ = ["CarrierClient", "CarrierClientFactory", "TokenCache", "error_text"]

log = logging.getLogger("eta.carrier")


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


class TokenCache:
    """One carrier token provider's entry in tokens.json, written best effort (review fixes MSK-05 / CRA-05).

    tokens.json is only a cache for carrier tokens, so a failed write (full disk, permissions, a Windows sharing
    violation while another program has the file open) must neither fail a lookup (03 §5 boundary rule) nor throw
    away a token just obtained. The file stays the source of truth while it can be written. After a failed put()
    the entry is kept in memory for this process; after a failed delete() the entry left in the file (a token the
    carrier may have rejected) is ignored for the rest of the process. One WARNING per cache, never a token."""

    def __init__(self, store: TokenStore, key: str, *, label: str) -> None:
        self.store = store
        self.key = key
        self._label = label
        self._unsaved: dict[str, Any] | None = None   # an entry the file could not take
        self._file_stale = False                        # the file still holds an entry that was deleted
        self._warned = False

    def get(self) -> dict[str, Any] | None:
        """A copy of the current entry, or None."""
        if self._unsaved is not None:
            return dict(self._unsaved)
        return None if self._file_stale else self.store.get(self.key)

    def put(self, entry: Mapping[str, Any], *, strict: bool = False) -> None:
        """Save the entry; a failed write keeps it in memory (WARNING), or is raised when `strict` (an explicit save
        the user asked for, e.g. `eta msc-token`, must say that nothing was stored)."""
        try:
            self.store.put(self.key, dict(entry))
        except OSError as exc:
            if strict:
                raise
            self._unsaved = dict(entry)
            self._warn("save", exc)
            return
        self._unsaved = None
        self._file_stale = False

    def delete(self) -> None:
        """Drop the entry; when the file cannot be rewritten its stale entry is ignored from now on (WARNING)."""
        self._unsaved = None
        try:
            self.store.delete(self.key)
        except OSError as exc:
            self._file_stale = True
            self._warn("remove", exc)

    def _warn(self, action: str, exc: OSError) -> None:
        if self._warned:
            return
        self._warned = True
        log.warning("[%s] Could not %s the cached access token in %s (%s); this run keeps it in memory only. Check "
                    "that the file can be written.", self._label, action, self.store.path, type(exc).__name__)
