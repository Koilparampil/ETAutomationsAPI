"""Carrier routing (01 §4), provider selection (09 Part D), run memo and circuit breaker (10 §6).

Pipeline usage (10 Phase 2):

    router = build_router(settings, token_store, clock=FixedClock(run_now))
    await router.prepare(bases)                  # once, before the per-row tasks (09 §D.2)
    carrier, token, client = router.route(base)  # route-time errors are raised before any client is called
    eta = await client.get_eta(token)            # run memo -> circuit breaker -> the provider's client
    ...
    await router.aclose()                        # closes every client once

route() hands out a `GuardedClient` (a 03 §9 CarrierClient) for the carrier's provider:
- `get_eta()` is memoized per (carrier, query token) for the whole run: single-flight, results AND
  exceptions (10 §6), so two rows for one booking (`NAM1234567` and `NAM1234567 A`) cost one carrier call;
- inside the memo, one `CircuitBreaker` per (provider, carrier) makes further lookups fail fast after
  `ETA_CARRIER_FAILURE_LIMIT` identical consecutive CarrierLookupErrors (10 §6, 09 §D.2);
- `probe()` bypasses both; `aclose()` is a no-op (the router closes the real clients).

Deliberate deviations implemented here (01 §15): D5 (the base booking is upper-cased before matching), D6
(extra text next to an MSC/CMA/Grimaldi booking number is a clear CarrierLookupError) and D7 (reference
numbers are `20\\d{8}` instead of `2026\\d{6}`).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable, Iterable, Mapping
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any, Final, Self, cast

from ..config import (
    CMA_ALL_CREDENTIALS,
    DEFAULT_PROVIDERS,
    PROVIDER_CHOICES,
    ProviderName,
)
from ..errors import (
    AggregatorBookingError,
    CarrierLookupError,
    ConfigError,
    CustomsHoldError,
    NoEtaError,
    UnsupportedCarrierError,
)
from ..models import Carrier, EtaResult
from .base import CarrierClient, error_text
from .grimaldi import GRIMALDI_NOT_IMPLEMENTED

if TYPE_CHECKING:
    import httpx

    from ..clock import Clock
    from ..config import Settings
    from ..token_store import TokenStore
    from .aggregator import AggregatorClient

__all__ = [
    "AGGREGATOR_NOT_CONFIGURED_MESSAGE",
    "BREAKER_MESSAGE",
    "CMA_CREDENTIALS_HINT",
    "CMA_PATTERN",
    "DEFAULT_FAILURE_LIMIT",
    "EXTRA_TEXT_MESSAGE",
    "GRIMALDI_PATTERN",
    "MAERSK_PATTERN",
    "MSC_PATTERN",
    "NATIVE_SERVICES",
    "NOT_CONFIGURED_MESSAGE",
    "NO_PATTERN_MESSAGE",
    "PROVIDER_OFF_MESSAGE",
    "REF_NUMBER_MESSAGE",
    "REF_NUMBER_PATTERN",
    "UNCOUNTED_ERRORS",
    "CarrierRouter",
    "CircuitBreaker",
    "GuardedClient",
    "build_router",
    "counts_toward_breaker",
    "identify_carrier",
    "normalise_providers",
    "not_configured_message",
]

log = logging.getLogger("eta.carrier.router")

# ── 01 §4 pattern table, applied in this order to the stripped, upper-cased base booking ──
MSC_PATTERN: Final = re.compile(r"EBKG\d{8}")        # 1: MSC, query token = the match
MAERSK_PATTERN: Final = re.compile(r"2\d{8}")        # 2: Maersk, fullmatch, query token = the whole base
CMA_PATTERN: Final = re.compile(r"NAM\d{7}")         # 3: CMA, query token = the match
GRIMALDI_PATTERN: Final = re.compile(r"S3\d{8}")     # 4: Grimaldi, query token = the match
REF_NUMBER_PATTERN: Final = re.compile(r"20\d{8}")   # 5: reference number (D7: the original used 2026\d{6})

# ── exact texts (01 §4, 09 §D.2, 08 §3, 10 §6) ──
REF_NUMBER_MESSAGE: Final = "This is a Ref Number: {booking}"
NO_PATTERN_MESSAGE: Final = "No carrier pattern matched booking {booking} — process manually"
EXTRA_TEXT_MESSAGE: Final = (
    "Booking '{booking}' contains text besides the {carrier} booking number {token} — process manually"
)
PROVIDER_OFF_MESSAGE: Final = "{carrier} lookups are disabled (ETA_PROVIDER_{name}=off)"
AGGREGATOR_NOT_CONFIGURED_MESSAGE: Final = (
    "{carrier} is set to use the aggregator (ETA_PROVIDER_{name}=aggregator) but the aggregator is not "
    "configured — set AGG_API_TOKEN in .env"
)
NOT_CONFIGURED_MESSAGE: Final = "{carrier} lookups are not configured — set {names} in .env"
CMA_CREDENTIALS_HINT: Final = "CMA_API_KEY (or CMA_CLIENT_ID + CMA_CLIENT_SECRET)"   # 08 §3
BREAKER_MESSAGE: Final = (
    "{carrier} lookups aborted after {count} identical consecutive failures; last error: {last}"
)

DEFAULT_FAILURE_LIMIT: Final = 5   # ETA_CARRIER_FAILURE_LIMIT default (02 §1)

# Settings.missing key of each carrier with credentials (03 §12); Grimaldi needs none (09 Part C).
NATIVE_SERVICES: Final[Mapping[Carrier, str]] = MappingProxyType({
    Carrier.MSC: "msc",
    Carrier.MAERSK: "maersk",
    Carrier.CMA: "cma",
})

# 09 §D.2 / 10 §6: failures the circuit breaker never counts.
UNCOUNTED_ERRORS: Final = (UnsupportedCarrierError, AggregatorBookingError, NoEtaError, CustomsHoldError)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §4 — booking number → (carrier, query token)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def identify_carrier(base: str) -> tuple[Carrier, str]:
    """Apply the 01 §4 pattern table to a base booking and return (carrier, query token).

    Raises UnsupportedCarrierError for a reference number (order 5) or when no pattern matches (order 6),
    and CarrierLookupError for D6 (text besides an MSC/CMA/Grimaldi booking number). The Grimaldi
    "not yet implemented" decision depends on the provider, so it belongs to CarrierRouter.route().
    """
    b = (base or "").strip().upper()   # D5: patterns are matched on the upper-cased base booking
    carrier: Carrier
    if (m := MSC_PATTERN.search(b)) is not None:
        carrier, token = Carrier.MSC, m.group(0)
    elif MAERSK_PATTERN.fullmatch(b):
        return Carrier.MAERSK, b       # fullmatch: the token is always the whole base, so D6 cannot apply
    elif (m := CMA_PATTERN.search(b)) is not None:
        carrier, token = Carrier.CMA, m.group(0)
    elif (m := GRIMALDI_PATTERN.search(b)) is not None:
        carrier, token = Carrier.GRIMALDI, m.group(0)
    elif REF_NUMBER_PATTERN.search(b):  # D7: 10 digits starting "20" (year-proof; cannot collide with Maersk)
        raise UnsupportedCarrierError(REF_NUMBER_MESSAGE.format(booking=b))
    else:
        raise UnsupportedCarrierError(NO_PATTERN_MESSAGE.format(booking=b))
    if b != token:
        # D6: the original sent the whole string to the carrier and failed later with a less clear error;
        # the outcome (Failed_To_Process.txt, "Carrier Lookup Failed.") is the same.
        raise CarrierLookupError(EXTRA_TEXT_MESSAGE.format(booking=b, carrier=carrier, token=token))
    return carrier, token


def normalise_providers(providers: Mapping[Carrier, str]) -> dict[Carrier, ProviderName]:
    """Every carrier's provider (09 §D.1), stripped and lower-cased; a carrier missing from `providers` gets
    its default (native, or off for Grimaldi). An unknown value raises the 09 §D.1 ConfigError."""
    out: dict[Carrier, ProviderName] = {}
    for carrier in Carrier:
        raw = providers.get(carrier, DEFAULT_PROVIDERS[carrier])
        value = str(raw).strip().lower()
        if value not in PROVIDER_CHOICES:
            raise ConfigError(f"ETA_PROVIDER_{carrier.name} must be one of native, aggregator, off (got {raw!r})")
        out[carrier] = cast(ProviderName, value)
    return out


def not_configured_message(carrier: Carrier, missing: Iterable[str]) -> str:
    """09 §D.2 step 6 / 03 §9: "<carrier> lookups are not configured — set <vars> in .env"."""
    names = tuple(missing)
    if names == CMA_ALL_CREDENTIALS:
        listed = CMA_CREDENTIALS_HINT        # 08 §3: either credential set configures CMA
    elif names:
        listed = ", ".join(names)
    else:
        # Defensive: only a hand-built router without Settings.missing gets here.
        listed = f"the {carrier.name}_* settings"
    return NOT_CONFIGURED_MESSAGE.format(carrier=carrier, names=listed)


def counts_toward_breaker(exc: BaseException) -> bool:
    """10 §6 / 09 §D.2: only a CarrierLookupError that is not an UnsupportedCarrierError or an
    AggregatorBookingError counts (NoEtaError and CustomsHoldError are not CarrierLookupErrors)."""
    return isinstance(exc, CarrierLookupError) and not isinstance(exc, UNCOUNTED_ERRORS)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 10 §6 — circuit breaker (one per (provider, carrier))
# ════════════════════════════════════════════════════════════════════════════════════════════════
class CircuitBreaker:
    """The run of consecutive identical CarrierLookupErrors of one (provider, carrier) (10 §6, 09 §D.2).

    - A counted failure (`counts_toward_breaker`) with the same text as the previous one extends the run;
      one with another text starts a new run of 1.
    - Any other outcome ends the run (count 0): a success, or a failure the breaker never counts
      (UnsupportedCarrierError, AggregatorBookingError, NoEtaError, CustomsHoldError, an unexpected error).
      10 §6: "Any success or differently-worded failure resets the count"; an uncounted failure never
      counts, so it can never trip the breaker.
    - Once the run reaches `limit`, check() fails every further lookup fast, without a network call. A
      lookup that was already running and then succeeds (or fails differently) resets it.
    A cancelled lookup changes nothing. Memoized duplicates are recorded once (the memo wraps the breaker).
    """

    def __init__(self, provider: str, carrier: Carrier, limit: int) -> None:
        if limit < 1:
            raise ValueError(f"failure limit must be at least 1 (got {limit})")
        self.provider = provider
        self.carrier = carrier
        self.limit = limit
        self.count = 0
        self.last: str | None = None

    @property
    def tripped(self) -> bool:
        return self.count >= self.limit

    def check(self) -> None:
        """Raise the 10 §6 fail-fast CarrierLookupError once the breaker has tripped."""
        if self.tripped:
            raise CarrierLookupError(BREAKER_MESSAGE.format(carrier=self.carrier, count=self.count,
                                                            last=self.last))

    def record_success(self) -> None:
        self._reset("a successful lookup")

    def record_failure(self, exc: BaseException) -> None:
        if not counts_toward_breaker(exc):
            self._reset(f"a {type(exc).__name__}")
            return
        text = str(exc)
        if text == self.last:
            self.count += 1
        else:
            self.count, self.last = 1, text
        if self.count == self.limit:
            log.warning("%s lookups (%s): %d identical consecutive failures — circuit breaker tripped, further "
                        "%s lookups fail without a call. Last error: %s", self.carrier, self.provider,
                        self.count, self.carrier, text)

    def _reset(self, reason: str) -> None:
        if self.tripped:
            log.info("%s lookups (%s): circuit breaker reset after %s", self.carrier, self.provider, reason)
        self.count, self.last = 0, None

    def __repr__(self) -> str:
        return (f"CircuitBreaker({self.carrier!s} via {self.provider}: {self.count}/{self.limit}"
                f"{', tripped' if self.tripped else ''})")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# The CarrierClient that route() hands out
# ════════════════════════════════════════════════════════════════════════════════════════════════
class GuardedClient:
    """CarrierClient (03 §9) for one (provider, carrier): run memo → circuit breaker → `inner` (10 §6)."""

    def __init__(self, *, carrier: Carrier, provider: str, inner: CarrierClient, breaker: CircuitBreaker,
                 memo: dict[tuple[Carrier, str], asyncio.Task[EtaResult]]) -> None:
        self.carrier = carrier
        self.provider = provider
        self.inner = inner           # the native client or the aggregator's view for this carrier
        self.breaker = breaker
        self._memo = memo            # shared by every GuardedClient of the router, keyed (carrier, token)

    async def get_eta(self, booking: str) -> EtaResult:
        """Single-flight memo per (carrier, token) for the whole run (10 §6): the first caller creates the
        lookup task, later callers await the same task through asyncio.shield (one caller's cancellation
        never cancels the shared lookup); results and exceptions are both memoized."""
        ref = booking.strip().upper()
        key = (self.carrier, ref)
        task = self._memo.get(key)
        if task is None:
            task = asyncio.create_task(self._lookup(ref), name=f"eta-lookup {self.carrier} {ref}")
            self._memo[key] = task
        return await asyncio.shield(task)

    async def _lookup(self, ref: str) -> EtaResult:
        self.breaker.check()          # tripped → fail fast without calling the provider
        try:
            result = await self.inner.get_eta(ref)
        except Exception as exc:
            self.breaker.record_failure(exc)
            raise
        self.breaker.record_success()
        return result

    async def probe(self, booking: str) -> dict[str, Any]:
        """Diagnostics bypass the memo and the breaker (10 §6)."""
        return await self.inner.probe(booking)

    async def aclose(self) -> None:
        """No-op: CarrierRouter.aclose() closes every real client once (09 §D.2)."""

    def __repr__(self) -> str:
        return f"GuardedClient({self.carrier!s} via {self.provider}: {type(self.inner).__name__})"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 09 Part D — provider selection
# ════════════════════════════════════════════════════════════════════════════════════════════════
class CarrierRouter:
    """Maps a base booking to (carrier, query token, CarrierClient) and owns the run memo and breakers.

    `providers`: ETA_PROVIDER_<CARRIER> per carrier (09 §D.1). `native`: each carrier module's
    build_client() result (None = not configured). `native_missing`: the env var names for the
    "not configured" message (Settings.missing). `aggregator`: the multi-carrier AggregatorClient or None.
    `failure_limit`: ETA_CARRIER_FAILURE_LIMIT.
    """

    def __init__(self, *, providers: Mapping[Carrier, str],
                 native: Mapping[Carrier, CarrierClient | None],
                 native_missing: Mapping[Carrier, tuple[str, ...]] | None = None,
                 aggregator: AggregatorClient | None = None,
                 failure_limit: int = DEFAULT_FAILURE_LIMIT) -> None:
        if failure_limit < 1:
            raise ValueError(f"failure_limit must be at least 1 (got {failure_limit})")
        self._providers: Mapping[Carrier, ProviderName] = MappingProxyType(normalise_providers(providers))
        self._native: dict[Carrier, CarrierClient | None] = dict(native)
        self._native_missing: dict[Carrier, tuple[str, ...]] = {
            carrier: tuple(names) for carrier, names in (native_missing or {}).items()
        }
        self._aggregator = aggregator
        self._failure_limit = failure_limit
        self._memo: dict[tuple[Carrier, str], asyncio.Task[EtaResult]] = {}
        self._breakers: dict[tuple[str, Carrier], CircuitBreaker] = {}
        self._guarded: dict[tuple[str, Carrier], GuardedClient] = {}
        self._closed = False

    # ── accessors ──
    @property
    def providers(self) -> Mapping[Carrier, ProviderName]:
        """The resolved provider of every carrier (read-only)."""
        return self._providers

    @property
    def aggregator(self) -> AggregatorClient | None:
        return self._aggregator

    @property
    def failure_limit(self) -> int:
        return self._failure_limit

    def provider_for(self, carrier: Carrier) -> ProviderName:
        """"native", "aggregator" or "off" (e.g. the 10 Phase 1 MSC token pre-flight only applies to native)."""
        return self._providers[Carrier(carrier)]

    def native_client(self, carrier: Carrier) -> CarrierClient | None:
        """The carrier's own client as built (None when not configured or not used this run)."""
        return self._native.get(Carrier(carrier))

    def breaker_for(self, provider: str, carrier: Carrier) -> CircuitBreaker:
        """The circuit breaker of one (provider, carrier) (09 §D.2), created on first use."""
        key = (provider, Carrier(carrier))
        breaker = self._breakers.get(key)
        if breaker is None:
            breaker = self._breakers[key] = CircuitBreaker(provider, key[1], self._failure_limit)
        return breaker

    # ── 09 §D.2 ──
    def route(self, base: str) -> tuple[Carrier, str, CarrierClient]:
        """(carrier, query token, client) for a base booking. Every routing error is raised here, before any
        client is called, so the circuit breaker never sees it (09 §D.2)."""
        carrier, token, provider, inner = self._resolve(base)
        return carrier, token, self._guard(carrier, provider, inner)

    async def prepare(self, bases: Iterable[str]) -> None:
        """09 §D.2 warm-up hook (10 Phase 2: called once, before the per-row tasks, with the base booking of
        every STANDARD / NOTIF1_SENT row at or above the amount threshold).

        Routes every base, ignoring routing errors (each row's task reports its own), and hands the
        de-duplicated (carrier, token) pairs that resolved to the aggregator to `aggregator.prepare()` in
        one call. No-op without an aggregator or without aggregator-routed bookings.
        """
        aggregator = self._aggregator
        if aggregator is None:
            return
        pairs: dict[tuple[Carrier, str], None] = {}
        for base in bases:
            try:
                carrier, token, provider, _inner = self._resolve(base)
            except Exception as exc:  # noqa: BLE001 - 09 §D.2: ignored here; the row's own task reports it
                log.debug("prepare: %r not routed (%s)", base, error_text(exc))
                continue
            if provider == "aggregator":
                pairs.setdefault((carrier, token), None)
        if not pairs:
            return
        try:
            await aggregator.prepare(list(pairs))
        except Exception as exc:  # noqa: BLE001 - a failed warm-up must not stop the run; get_eta reports per row
            log.warning("Aggregator warm-up failed (%s); bookings are registered when they are looked up",
                        error_text(exc))

    async def aclose(self) -> None:
        """Cancel lookups still pending, then close every native client once and the aggregator once
        (09 §D.2; GuardedClients and aggregator views own nothing). Idempotent; a failing close is logged
        and the others are still closed."""
        if self._closed:
            return
        self._closed = True
        pending = [task for task in self._memo.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        closables: list[Any] = [client for client in self._native.values() if client is not None]
        if self._aggregator is not None:
            closables.append(self._aggregator)
        closed: set[int] = set()
        for client in closables:
            if id(client) in closed:
                continue
            closed.add(id(client))
            try:
                await client.aclose()
            except Exception as exc:  # noqa: BLE001 - keep closing the other clients
                log.warning("Could not close %s: %s", type(client).__name__, error_text(exc))

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.aclose()

    # ── internals ──
    def _resolve(self, base: str) -> tuple[Carrier, str, ProviderName, CarrierClient]:
        """09 §D.2 route() steps 1–6, returning the unguarded client and its provider."""
        carrier, token = identify_carrier(base)                                   # 1. 01 §4 table
        provider = self._providers[carrier]                                       # 2.
        name = carrier.name
        if carrier is Carrier.GRIMALDI and provider in ("native", "off"):        # 3. exact original text
            raise UnsupportedCarrierError(GRIMALDI_NOT_IMPLEMENTED.format(booking=token))
        if provider == "off":                                                     # 4.
            raise CarrierLookupError(PROVIDER_OFF_MESSAGE.format(carrier=carrier, name=name))
        if provider == "aggregator":                                              # 5.
            if self._aggregator is None:
                raise CarrierLookupError(AGGREGATOR_NOT_CONFIGURED_MESSAGE.format(carrier=carrier, name=name))
            return carrier, token, provider, self._aggregator.view(carrier)
        client = self._native.get(carrier)                                        # 6. native
        if client is None:
            raise CarrierLookupError(not_configured_message(carrier, self._native_missing.get(carrier, ())))
        return carrier, token, provider, client

    def _guard(self, carrier: Carrier, provider: str, inner: CarrierClient) -> GuardedClient:
        """One GuardedClient per (provider, carrier), sharing the router's memo."""
        key = (provider, carrier)
        guarded = self._guarded.get(key)
        if guarded is None or guarded.inner is not inner:
            guarded = self._guarded[key] = GuardedClient(
                carrier=carrier, provider=provider, inner=inner, breaker=self.breaker_for(provider, carrier),
                memo=self._memo)
        return guarded

    def __repr__(self) -> str:
        providers = ", ".join(f"{carrier.name}={provider}" for carrier, provider in self._providers.items())
        return f"CarrierRouter({providers}, failure_limit={self._failure_limit})"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Factory (10 §3 build_services)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def build_router(settings: Settings, token_store: TokenStore, *, clock: Clock | None = None,
                 http: httpx.AsyncClient | None = None, read_only: bool = False) -> CarrierRouter:
    """Build every client the configured providers can use and wrap them in a CarrierRouter.

    Each carrier module's build_client(settings, token_store, clock=clock, http=http) (03 §9) is called for
    the carriers whose provider is "native"; aggregator.build_client() is called when at least one carrier
    uses the aggregator. A carrier routed to "aggregator" or "off" never reaches its own client (09 §D.2
    steps 4–5), so none is built for it (no unused HTTP client, no misleading start-up log line).
    `native_missing` comes from Settings.missing for the "not configured" messages; `failure_limit` from
    ETA_CARRIER_FAILURE_LIMIT. `read_only` (dry run) builds the aggregator so that it never registers a booking
    (1 credit each) nor writes its tracking-state file (CRA-01).
    """
    # Imported here so that importing the router (identify_carrier) stays cheap and cycle-free.
    from . import aggregator as aggregator_module
    from . import cma, grimaldi, maersk, msc

    providers = normalise_providers(settings.run.providers)

    def native_client(carrier: Carrier, factory: Callable[..., CarrierClient | None]) -> CarrierClient | None:
        if providers[carrier] != "native":
            return None
        return factory(settings, token_store, clock=clock, http=http)

    native: dict[Carrier, CarrierClient | None] = {
        Carrier.MSC: native_client(Carrier.MSC, msc.build_client),
        Carrier.MAERSK: native_client(Carrier.MAERSK, maersk.build_client),
        Carrier.CMA: native_client(Carrier.CMA, cma.build_client),
        Carrier.GRIMALDI: native_client(Carrier.GRIMALDI, grimaldi.build_client),   # parity stub (09 Part C)
    }
    aggregator = None
    if "aggregator" in providers.values():
        aggregator = aggregator_module.build_client(settings, token_store, clock=clock, http=http)
        if read_only and aggregator is not None:
            aggregator.set_read_only()
    native_missing = {carrier: tuple(settings.missing.get(service, ()))
                      for carrier, service in NATIVE_SERVICES.items()}
    return CarrierRouter(providers=providers, native=native, native_missing=native_missing,
                         aggregator=aggregator, failure_limit=settings.run.carrier_failure_limit)
