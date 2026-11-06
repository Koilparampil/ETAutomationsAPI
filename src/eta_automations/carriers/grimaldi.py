"""Grimaldi stub (09 Part C) — keeps the original "not yet implemented" behaviour (parity).

Grimaldi (booking pattern `S3` + 8 digits, 01 §4) has no public tracking API and is not a DCSA member;
its public tracking is the GNET eService web portal, and automating that would mean browser automation,
which this program rules out (03 §1, 09 §C.1). Rows therefore fail with the exact original text unless
`ETA_PROVIDER_GRIMALDI=aggregator` routes them to an aggregator that covers Grimaldi (09 Part B/D).

This module performs no network I/O at all.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import UnsupportedCarrierError
from ..models import Carrier, EtaResult

if TYPE_CHECKING:
    import httpx

    from ..clock import Clock
    from ..config import Settings
    from ..token_store import TokenStore

__all__ = ["GRIMALDI_NOT_IMPLEMENTED", "GRIMALDI_PROBE_NOTE", "GrimaldiClient", "build_client"]

# 01 §4 / 09 §C.2 — exact original text.
GRIMALDI_NOT_IMPLEMENTED = "Grimaldi ETA lookup not yet implemented for booking {booking}"
GRIMALDI_PROBE_NOTE = (
    "No Grimaldi API; see 09 Part C. Use ETA_PROVIDER_GRIMALDI=aggregator if your aggregator covers it."
)


class GrimaldiClient:
    """CarrierClient (03 §9) for Grimaldi: every lookup fails with the original message (09 §C.2)."""

    carrier = Carrier.GRIMALDI

    def __init__(self, *, clock: Clock | None = None, http: httpx.AsyncClient | None = None) -> None:
        # 03 §9 constructor contract (optional clock/http); the stub needs neither and never uses `http`,
        # so an injected client is never touched or closed.
        self._clock = clock
        self._http = http

    async def get_eta(self, booking: str) -> EtaResult:
        """Always raises UnsupportedCarrierError with the exact original text (01 §4, 01 §6.1 → Carrier
        Lookup Failed.)."""
        raise UnsupportedCarrierError(GRIMALDI_NOT_IMPLEMENTED.format(booking=booking))

    async def probe(self, booking: str) -> dict[str, Any]:
        """`eta probe` output (09 §C.2): no request is made."""
        return {
            "requests": [],
            "raw": None,
            "eta": GRIMALDI_NOT_IMPLEMENTED.format(booking=booking),
            "note": GRIMALDI_PROBE_NOTE,
        }

    async def aclose(self) -> None:
        """Nothing to close."""
        return None


def build_client(settings: Settings, token_store: TokenStore, *, clock: Clock | None = None,
                 http: httpx.AsyncClient | None = None) -> GrimaldiClient:
    """Always "configured" — the stub needs no settings (09 §C.2). The 03 §9 keyword arguments are accepted
    for factory-signature parity; `settings` and `token_store` are unused."""
    return GrimaldiClient(clock=clock, http=http)
