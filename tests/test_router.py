"""Carrier router (carriers/router.py): 09 §D.3 tests 1–8, the 12-testing §3 router table (01 §4), and the
10 §6 run memo and circuit breaker. In-memory fakes only (no HTTP); build_router() also runs on real Settings."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

import httpx
import pytest

from eta_automations.carriers import aggregator as aggregator_module
from eta_automations.carriers import cma, grimaldi, maersk, msc
from eta_automations.carriers.aggregator import AggregatorCarrierView, AggregatorClient
from eta_automations.carriers.base import CarrierClient
from eta_automations.carriers.router import (
    CarrierRouter,
    CircuitBreaker,
    GuardedClient,
    build_router,
    counts_toward_breaker,
    identify_carrier,
    normalise_providers,
    not_configured_message,
)
from eta_automations.clock import FixedClock
from eta_automations.config import Settings
from eta_automations.errors import (
    AggregatorBookingError,
    CarrierLookupError,
    ConfigError,
    CustomsHoldError,
    NoEtaError,
    UnsupportedCarrierError,
)
from eta_automations.models import Carrier, EtaResult
from eta_automations.rules import classify, parse_rows
from eta_automations.token_store import TokenStore

ETA = datetime(2026, 11, 12, 14, 0)
NATIVE_CARRIERS = (Carrier.MSC, Carrier.MAERSK, Carrier.CMA)
DEFAULT_PROVIDERS = {Carrier.MSC: "native", Carrier.MAERSK: "native", Carrier.CMA: "native",
                     Carrier.GRIMALDI: "off"}
GRIMALDI_TEXT = "Grimaldi ETA lookup not yet implemented for booking S312345678"
LOGIN_REJECTED = "MSC login rejected"
TOKEN_401 = "ShipsGo rejected the API token (HTTP 401) — check AGG_API_TOKEN"

Outcome = EtaResult | BaseException


def eta_for(carrier: Carrier, booking: str) -> EtaResult:
    return EtaResult(carrier=carrier, booking=booking, eta=ETA, source="fake")


class FakeCarrier:
    """CarrierClient fake (03 §9): records calls; `behaviour(booking)` returns an EtaResult or an exception to
    raise; `gate` (an asyncio.Event) holds get_eta() open until set."""

    def __init__(self, carrier: Carrier, behaviour: Callable[[str], Outcome] | None = None) -> None:
        self.carrier = carrier
        self.behaviour: Callable[[str], Outcome] = behaviour or (lambda booking: eta_for(carrier, booking))
        self.calls: list[str] = []
        self.probes: list[str] = []
        self.closed = 0
        self.close_error: Exception | None = None
        self.gate: asyncio.Event | None = None

    async def get_eta(self, booking: str) -> EtaResult:
        self.calls.append(booking)
        if self.gate is not None:
            await self.gate.wait()
        outcome = self.behaviour(booking)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def probe(self, booking: str) -> dict[str, Any]:
        self.probes.append(booking)
        return {"requests": [], "raw": None, "eta": f"probe {booking}"}

    async def aclose(self) -> None:
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error


class FakeAggregator:
    """AggregatorClient fake (09 §B.7.1) handing out the real AggregatorCarrierView."""

    def __init__(self, behaviour: Callable[[Carrier, str], Outcome] | None = None) -> None:
        self.behaviour: Callable[[Carrier, str], Outcome] = behaviour or eta_for
        self.calls: list[tuple[Carrier, str]] = []
        self.prepared: list[list[tuple[Carrier, str]]] = []
        self.probes: list[tuple[Carrier | None, str | None]] = []
        self.prepare_error: Exception | None = None
        self.closed = 0
        self._views: dict[Carrier, AggregatorCarrierView] = {}

    def view(self, carrier: Carrier) -> AggregatorCarrierView:
        view = self._views.get(carrier)
        if view is None:
            view = self._views[carrier] = AggregatorCarrierView(self, carrier)  # type: ignore[arg-type]
        return view

    async def prepare(self, items: list[tuple[Carrier, str]]) -> dict[str, int]:
        batch = list(items)
        self.prepared.append(batch)
        if self.prepare_error is not None:
            raise self.prepare_error
        return {"known": 0, "found": 0, "created": len(batch), "failed": 0}

    async def get_eta(self, carrier: Carrier, booking: str) -> EtaResult:
        self.calls.append((carrier, booking))
        outcome = self.behaviour(carrier, booking)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def probe(self, carrier: Carrier | None, booking: str | None) -> dict[str, Any]:
        self.probes.append((carrier, booking))
        return {"eta": None, "error": None}

    async def aclose(self) -> None:
        self.closed += 1


def natives() -> dict[Carrier, FakeCarrier]:
    return {carrier: FakeCarrier(carrier) for carrier in NATIVE_CARRIERS}


def make_router(*, providers: Mapping[Carrier, str] | None = None, native: Mapping[Carrier, Any] | None = None,
                missing: Mapping[Carrier, tuple[str, ...]] | None = None, aggregator: Any = None,
                failure_limit: int = 5) -> CarrierRouter:
    return CarrierRouter(providers={**DEFAULT_PROVIDERS, **(providers or {})},
                         native=native if native is not None else natives(),
                         native_missing=missing, aggregator=aggregator, failure_limit=failure_limit)


async def lookup(router: CarrierRouter, base: str) -> EtaResult:
    """What a pipeline row does (10 Phase 2a): route, then get_eta(token)."""
    _carrier, token, client = router.route(base)
    return await client.get_eta(token)


async def lookup_error(router: CarrierRouter, base: str) -> Exception:
    with pytest.raises(Exception) as info:
        await lookup(router, base)
    return info.value


async def settle() -> None:
    """Let started tasks run until they block."""
    for _ in range(5):
        await asyncio.sleep(0)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 01 §4 / 12-testing §3 — the router table
# ════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(("base", "expected"), [
    ("EBKG17208602", (Carrier.MSC, "EBKG17208602")),
    ("267546627", (Carrier.MAERSK, "267546627")),
    ("NAM1234567", (Carrier.CMA, "NAM1234567")),
    ("S312345678", (Carrier.GRIMALDI, "S312345678")),
    ("ebkg17208602", (Carrier.MSC, "EBKG17208602")),        # D5: upper-cased before matching
    ("  nam1234567\t", (Carrier.CMA, "NAM1234567")),
    ("s312345678", (Carrier.GRIMALDI, "S312345678")),
    ("202654662", (Carrier.MAERSK, "202654662")),            # 9 digits: order 2 (Maersk) before order 5
])
def test_identify_carrier_pattern_table(base: str, expected: tuple[Carrier, str]) -> None:
    assert identify_carrier(base) == expected


ROUTER_TABLE_ERRORS: list[tuple[str, type[Exception], str]] = [
    ("XYZ", UnsupportedCarrierError, "No carrier pattern matched booking XYZ — process manually"),
    # 12-testing §3 lists 2675466270 as the reference-number case, but it contains no "20" followed by 8 digits,
    # so the 01 §4 table (which takes precedence over 12) gives the no-pattern text. Either way it is an
    # UnsupportedCarrierError and never Maersk (10 digits, fullmatch). 2026546627 shows the D7 ref text.
    ("2675466270", UnsupportedCarrierError, "No carrier pattern matched booking 2675466270 — process manually"),
    ("2026546627", UnsupportedCarrierError, "This is a Ref Number: 2026546627"),
    ("EBKG17208602 C 2", CarrierLookupError,
     "Booking 'EBKG17208602 C 2' contains text besides the MSC booking number EBKG17208602 — process manually"),
]


@pytest.mark.parametrize(("base", "error", "message"), [
    *ROUTER_TABLE_ERRORS,
    ("2031000001", UnsupportedCarrierError, "This is a Ref Number: 2031000001"),        # D7: year-proof
    ("REF 2026123456", UnsupportedCarrierError, "This is a Ref Number: REF 2026123456"),  # searched, not anchored
    ("267546627 X", UnsupportedCarrierError, "No carrier pattern matched booking 267546627 X — process manually"),
    ("nam1234567x", CarrierLookupError,
     "Booking 'NAM1234567X' contains text besides the CMA booking number NAM1234567 — process manually"),
    ("S312345678 B", CarrierLookupError,
     "Booking 'S312345678 B' contains text besides the Grimaldi booking number S312345678 — process manually"),
    ("NAM1234567 EBKG17208602", CarrierLookupError,   # order 1 (MSC) is tested first
     ("Booking 'NAM1234567 EBKG17208602' contains text besides the MSC booking number EBKG17208602 — "
      "process manually")),
])
def test_identify_carrier_errors(base: str, error: type[Exception], message: str) -> None:
    with pytest.raises(CarrierLookupError) as info:
        identify_carrier(base)
    assert type(info.value) is error
    assert str(info.value) == message


@pytest.mark.parametrize(("base", "carrier", "token"), [
    ("EBKG17208602", Carrier.MSC, "EBKG17208602"),
    ("267546627", Carrier.MAERSK, "267546627"),
    ("NAM1234567", Carrier.CMA, "NAM1234567"),
    ("ebkg17208602", Carrier.MSC, "EBKG17208602"),   # D5
])
async def test_router_table_routes_to_the_carrier_client(base: str, carrier: Carrier, token: str) -> None:
    fakes = natives()
    router = make_router(native=fakes)
    routed, routed_token, client = router.route(base)
    assert (routed, routed_token) == (carrier, token)
    assert isinstance(client, GuardedClient)
    assert client.inner is fakes[carrier] and client.carrier is carrier and client.provider == "native"
    result = await client.get_eta(routed_token)
    assert result == eta_for(carrier, token)
    assert fakes[carrier].calls == [token]
    assert all(not fake.calls for other, fake in fakes.items() if other is not carrier)


@pytest.mark.parametrize(("base", "error", "message"), [
    ("S312345678", UnsupportedCarrierError, GRIMALDI_TEXT),
    *ROUTER_TABLE_ERRORS,
])
def test_router_table_errors_are_raised_at_route_time(base: str, error: type[Exception], message: str) -> None:
    fakes = natives()
    agg = FakeAggregator()
    router = make_router(native=fakes, aggregator=agg)
    with pytest.raises(CarrierLookupError) as info:
        router.route(base)
    assert type(info.value) is error
    assert str(info.value) == message
    assert not any(fake.calls for fake in fakes.values()) and agg.calls == []


def test_route_hands_out_one_guarded_client_per_provider_and_carrier() -> None:
    fakes = natives()
    router = make_router(native=fakes)
    first = router.route("NAM1234567")[2]
    assert router.route("NAM7654321")[2] is first
    assert router.route("EBKG17208602")[2] is not first
    assert isinstance(first, CarrierClient)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 09 §D.3 tests 1–8
# ════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize(("carrier", "base", "message"), [
    (Carrier.MSC, "EBKG17208602", "MSC lookups are disabled (ETA_PROVIDER_MSC=off)"),
    (Carrier.MAERSK, "267546627", "Maersk lookups are disabled (ETA_PROVIDER_MAERSK=off)"),
    (Carrier.CMA, "NAM1234567", "CMA lookups are disabled (ETA_PROVIDER_CMA=off)"),
])
async def test_1_provider_off_gives_the_exact_disabled_message_and_calls_no_client(
        carrier: Carrier, base: str, message: str) -> None:
    fakes = natives()
    agg = FakeAggregator()
    router = make_router(providers={carrier: "off"}, native=fakes, aggregator=agg)
    for _ in range(2):
        error = await lookup_error(router, base)
        assert type(error) is CarrierLookupError
        assert str(error) == message
    assert not any(fake.calls for fake in fakes.values())
    assert agg.calls == [] and agg.prepared == []


@pytest.mark.parametrize(("carrier", "base"), [
    (Carrier.MSC, "EBKG17208602"),
    (Carrier.MAERSK, "267546627"),
    (Carrier.CMA, "NAM1234567"),
    (Carrier.GRIMALDI, "S312345678"),
])
def test_2_aggregator_provider_without_an_aggregator_names_agg_api_token(carrier: Carrier, base: str) -> None:
    fakes = natives()
    router = make_router(providers={carrier: "aggregator"}, native=fakes, aggregator=None)
    with pytest.raises(CarrierLookupError) as info:
        router.route(base)
    assert type(info.value) is CarrierLookupError
    assert str(info.value) == (
        f"{carrier} is set to use the aggregator (ETA_PROVIDER_{carrier.name}=aggregator) but the aggregator is "
        "not configured — set AGG_API_TOKEN in .env")
    assert "AGG_API_TOKEN" in str(info.value)
    assert not any(fake.calls for fake in fakes.values())


async def test_3_aggregator_route_returns_a_view_and_get_eta_forwards_the_carrier() -> None:
    fakes = natives()
    agg = FakeAggregator()
    router = make_router(providers={Carrier.CMA: "aggregator", Carrier.MAERSK: "aggregator"}, native=fakes,
                         aggregator=agg)
    carrier, token, client = router.route("NAM1234567")
    assert (carrier, token) == (Carrier.CMA, "NAM1234567")
    assert isinstance(client, GuardedClient) and isinstance(client, CarrierClient)
    assert client.carrier is Carrier.CMA and client.provider == "aggregator"
    assert isinstance(client.inner, AggregatorCarrierView)
    assert client.inner.carrier is Carrier.CMA
    assert client.inner is agg.view(Carrier.CMA)
    result = await client.get_eta(token)
    assert result == eta_for(Carrier.CMA, "NAM1234567")
    assert agg.calls == [(Carrier.CMA, "NAM1234567")]

    _carrier, maersk_token, maersk_client = router.route("267546627")
    assert isinstance(maersk_client, GuardedClient)
    assert isinstance(maersk_client.inner, AggregatorCarrierView)
    assert maersk_client.inner.carrier is Carrier.MAERSK
    await maersk_client.get_eta(maersk_token)
    assert agg.calls[-1] == (Carrier.MAERSK, "267546627")
    assert fakes[Carrier.CMA].calls == [] and fakes[Carrier.MAERSK].calls == []   # native clients unused
    assert await client.probe(token) == {"eta": None, "error": None}
    assert agg.probes == [(Carrier.CMA, "NAM1234567")]


@pytest.mark.parametrize("provider", [None, "off", "native", " Native "])
async def test_4_grimaldi_native_or_off_keeps_the_original_text(provider: str | None) -> None:
    stub = FakeCarrier(Carrier.GRIMALDI)
    agg = FakeAggregator()
    providers = {carrier: value for carrier, value in DEFAULT_PROVIDERS.items() if carrier is not Carrier.GRIMALDI}
    if provider is not None:                       # None: Grimaldi missing from the mapping → default "off"
        providers[Carrier.GRIMALDI] = provider
    router = CarrierRouter(providers=providers, native={**natives(), Carrier.GRIMALDI: stub}, aggregator=agg)
    error = await lookup_error(router, "S312345678")
    assert type(error) is UnsupportedCarrierError
    assert str(error) == GRIMALDI_TEXT
    assert stub.calls == [] and agg.calls == []


async def test_4_grimaldi_via_the_aggregator_is_a_view() -> None:
    agg = FakeAggregator()
    router = make_router(providers={Carrier.GRIMALDI: "aggregator"}, aggregator=agg)
    carrier, token, client = router.route("s312345678")
    assert (carrier, token) == (Carrier.GRIMALDI, "S312345678")
    assert isinstance(client, GuardedClient)
    assert isinstance(client.inner, AggregatorCarrierView) and client.inner.carrier is Carrier.GRIMALDI
    assert await client.get_eta(token) == eta_for(Carrier.GRIMALDI, "S312345678")
    assert agg.calls == [(Carrier.GRIMALDI, "S312345678")]


@pytest.mark.parametrize(("carrier", "base", "missing", "message"), [
    (Carrier.MAERSK, "267546627", ("MAERSK_CLIENT_ID", "MAERSK_CLIENT_SECRET"),
     "Maersk lookups are not configured — set MAERSK_CLIENT_ID, MAERSK_CLIENT_SECRET in .env"),
    (Carrier.MSC, "EBKG17208602", ("MSC_API_BASE_URL", "MSC_API_SUBSCRIPTION_KEY"),
     "MSC lookups are not configured — set MSC_API_BASE_URL, MSC_API_SUBSCRIPTION_KEY in .env"),
    (Carrier.CMA, "NAM1234567", ("CMA_CLIENT_SECRET",),
     "CMA lookups are not configured — set CMA_CLIENT_SECRET in .env"),
    # 08 §3: with no CMA credential at all, either set configures CMA.
    (Carrier.CMA, "NAM1234567", ("CMA_API_KEY", "CMA_CLIENT_ID", "CMA_CLIENT_SECRET"),
     "CMA lookups are not configured — set CMA_API_KEY (or CMA_CLIENT_ID + CMA_CLIENT_SECRET) in .env"),
])
def test_5_native_not_configured_lists_the_missing_env_vars(carrier: Carrier, base: str, missing: tuple[str, ...],
                                                            message: str) -> None:
    fakes: dict[Carrier, FakeCarrier | None] = {**natives(), carrier: None}
    router = make_router(native=fakes, missing={carrier: missing})
    with pytest.raises(CarrierLookupError) as info:
        router.route(base)
    assert type(info.value) is CarrierLookupError
    assert str(info.value) == message
    assert all(name in str(info.value) for name in missing)


def test_5_not_configured_message_without_known_names_is_still_actionable() -> None:
    assert not_configured_message(Carrier.MSC, ()) == "MSC lookups are not configured — set the MSC_* settings in .env"


def test_6_invalid_provider_value_is_a_config_error_at_load(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings(ETA_PROVIDER_CMA="shipsgo")
    assert str(info.value) == "ETA_PROVIDER_CMA must be one of native, aggregator, off (got 'shipsgo')"
    # case-insensitive and stripped (09 §D.1)
    assert make_settings(ETA_PROVIDER_CMA=" Aggregator ").run.providers[Carrier.CMA] == "aggregator"


def test_6_router_rejects_an_invalid_provider_too() -> None:
    with pytest.raises(ConfigError) as info:
        make_router(providers={Carrier.CMA: "shipsgo"})
    assert str(info.value) == "ETA_PROVIDER_CMA must be one of native, aggregator, off (got 'shipsgo')"


async def test_7_prepare_forwards_only_aggregator_routed_pairs_deduplicated_and_survives_route_errors() -> None:
    fakes = natives()
    agg = FakeAggregator()
    router = make_router(providers={Carrier.MAERSK: "aggregator", Carrier.CMA: "aggregator",
                                    Carrier.GRIMALDI: "aggregator"}, native=fakes, aggregator=agg)
    await router.prepare([
        "EBKG17208602",          # MSC is native → not forwarded
        "267546627",
        "267546627",             # duplicate
        "nam1234567",
        "NAM1234567",            # same pair after D5
        "XYZ",                   # route error: no pattern
        "2026123456",            # route error: reference number
        "NAM1234567 X",          # route error: D6
        "S312345678",
    ])
    assert agg.prepared == [[(Carrier.MAERSK, "267546627"), (Carrier.CMA, "NAM1234567"),
                             (Carrier.GRIMALDI, "S312345678")]]
    assert agg.calls == []
    assert not any(fake.calls for fake in fakes.values())


async def test_7_prepare_is_a_no_op_without_an_aggregator_or_without_aggregator_rows() -> None:
    fakes = natives()
    no_aggregator = make_router(providers={Carrier.CMA: "aggregator"}, native=fakes, aggregator=None)
    await no_aggregator.prepare(["NAM1234567", "EBKG17208602"])
    agg = FakeAggregator()
    all_native = make_router(native=fakes, aggregator=agg)
    await all_native.prepare(["NAM1234567", "EBKG17208602", "267546627"])
    await all_native.prepare([])
    assert agg.prepared == []
    assert not any(fake.calls for fake in fakes.values())


async def test_7_prepare_survives_an_aggregator_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.router")
    agg = FakeAggregator()
    agg.prepare_error = CarrierLookupError("ShipsGo network error: ConnectError('down')")
    router = make_router(providers={Carrier.CMA: "aggregator"}, aggregator=agg)
    await router.prepare(["NAM1234567"])
    assert agg.prepared == [[(Carrier.CMA, "NAM1234567")]]
    assert "Aggregator warm-up failed" in caplog.text and "ConnectError" in caplog.text
    # the rows still get their own lookups afterwards
    assert await lookup(router, "NAM1234567") == eta_for(Carrier.CMA, "NAM1234567")


async def test_8_breaker_ignores_aggregator_booking_errors_and_trips_on_identical_plain_errors() -> None:
    agg = FakeAggregator(lambda carrier, booking: AggregatorBookingError(
        f"ShipsGo is still fetching {carrier} booking {booking} — re-run later"))
    router = make_router(providers={Carrier.CMA: "aggregator"}, aggregator=agg, failure_limit=5)
    for i in range(1, 7):
        error = await lookup_error(router, f"NAM100000{i}")
        assert type(error) is AggregatorBookingError
    # also with identical texts: still never counted
    agg.behaviour = lambda carrier, booking: AggregatorBookingError("not tracked yet — re-run later")
    for i in range(1, 7):
        assert type(await lookup_error(router, f"NAM200000{i}")) is AggregatorBookingError
    breaker = router.breaker_for("aggregator", Carrier.CMA)
    assert breaker.count == 0 and not breaker.tripped
    assert len(agg.calls) == 12

    agg.behaviour = lambda carrier, booking: CarrierLookupError(TOKEN_401)
    for i in range(1, 6):
        error = await lookup_error(router, f"NAM300000{i}")
        assert str(error) == TOKEN_401
    assert breaker.tripped and len(agg.calls) == 17
    error = await lookup_error(router, "NAM3000006")
    assert type(error) is CarrierLookupError
    assert str(error) == f"CMA lookups aborted after 5 identical consecutive failures; last error: {TOKEN_401}"
    assert len(agg.calls) == 17                     # failed fast: no provider call


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 10 §6 — run memo (single-flight, per (carrier, token), results and exceptions)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_memo_two_rows_for_one_booking_cost_one_carrier_call() -> None:
    rows = [classify(row) for row in parse_rows([["NAM1000037", "5000"], ["NAM1000037 A", "5000"]])]
    assert [row.base_booking for row in rows] == ["NAM1000037", "NAM1000037"]
    fakes = natives()
    router = make_router(native=fakes)
    results = [await lookup(router, row.base_booking or "") for row in rows]
    assert fakes[Carrier.CMA].calls == ["NAM1000037"]
    assert results[0] is results[1]


async def test_memo_is_single_flight_for_concurrent_callers() -> None:
    fake = FakeCarrier(Carrier.MSC)
    fake.gate = asyncio.Event()
    router = make_router(native={Carrier.MSC: fake})
    _carrier, token, client = router.route("EBKG17208602")
    first = asyncio.create_task(client.get_eta(token))
    second = asyncio.create_task(client.get_eta(token))
    await settle()
    assert fake.calls == ["EBKG17208602"]
    fake.gate.set()
    one, two = await asyncio.gather(first, second)
    assert one is two
    assert fake.calls == ["EBKG17208602"]


@pytest.mark.parametrize("make_error", [
    lambda booking: NoEtaError(f"No arrival at the port of discharge published for {booking}. Can't determine ETA."),
    lambda booking: CarrierLookupError(f"no record of booking {booking}"),
    lambda booking: CustomsHoldError("Potential customs hold"),
])
async def test_memo_memoizes_exceptions(make_error: Callable[[str], Exception]) -> None:
    fake = FakeCarrier(Carrier.CMA, make_error)
    router = make_router(native={Carrier.CMA: fake})
    first = await lookup_error(router, "NAM1234567")
    second = await lookup_error(router, "NAM1234567")
    assert second is first
    assert fake.calls == ["NAM1234567"]


async def test_memo_is_keyed_by_carrier_and_normalised_token() -> None:
    fakes = natives()
    router = make_router(native=fakes)
    _carrier, token, client = router.route("NAM1234567")
    await client.get_eta(token)
    await client.get_eta("nam1234567 ")              # same token after strip/upper → memo hit
    await client.get_eta("NAM7654321")              # another booking → another call
    await lookup(router, "EBKG17208602")
    assert fakes[Carrier.CMA].calls == ["NAM1234567", "NAM7654321"]
    assert fakes[Carrier.MSC].calls == ["EBKG17208602"]


async def test_cancelling_one_caller_does_not_cancel_the_shared_lookup() -> None:
    fake = FakeCarrier(Carrier.CMA)
    fake.gate = asyncio.Event()
    router = make_router(native={Carrier.CMA: fake})
    _carrier, token, client = router.route("NAM1234567")
    first = asyncio.create_task(client.get_eta(token))
    second = asyncio.create_task(client.get_eta(token))
    await settle()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    fake.gate.set()
    result = await second
    assert await client.get_eta(token) is result
    assert fake.calls == ["NAM1234567"]


async def test_probe_bypasses_the_memo_and_the_breaker() -> None:
    fake = FakeCarrier(Carrier.MSC, lambda booking: CarrierLookupError(LOGIN_REJECTED))
    router = make_router(native={Carrier.MSC: fake}, failure_limit=1)
    _carrier, token, client = router.route("EBKG17208602")
    with pytest.raises(CarrierLookupError):
        await client.get_eta(token)
    assert client.breaker.tripped                    # type: ignore[attr-defined]
    assert await client.probe(token) == {"requests": [], "raw": None, "eta": "probe EBKG17208602"}
    await client.probe(token)
    assert fake.probes == ["EBKG17208602", "EBKG17208602"]
    assert fake.calls == ["EBKG17208602"]


async def test_memoized_duplicates_are_not_counted_twice() -> None:
    fake = FakeCarrier(Carrier.MSC, lambda booking: CarrierLookupError(LOGIN_REJECTED))
    router = make_router(native={Carrier.MSC: fake}, failure_limit=2)
    for _ in range(3):
        assert str(await lookup_error(router, "EBKG10000001")) == LOGIN_REJECTED
    breaker = router.breaker_for("native", Carrier.MSC)
    assert fake.calls == ["EBKG10000001"]
    assert breaker.count == 1 and not breaker.tripped
    assert str(await lookup_error(router, "EBKG10000002")) == LOGIN_REJECTED
    assert breaker.tripped


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 10 §6 — circuit breaker
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_breaker_trips_after_identical_consecutive_failures() -> None:
    """12-testing P31 shape: 6 MSC rows with the identical text → the 6th fails fast, 5 carrier calls."""
    fake = FakeCarrier(Carrier.MSC, lambda booking: CarrierLookupError(LOGIN_REJECTED))
    router = make_router(native={Carrier.MSC: fake}, failure_limit=5)
    errors = [await lookup_error(router, f"EBKG1000000{i}") for i in range(1, 7)]
    assert [str(error) for error in errors[:5]] == [LOGIN_REJECTED] * 5
    assert type(errors[5]) is CarrierLookupError
    assert str(errors[5]) == "MSC lookups aborted after 5 identical consecutive failures; last error: MSC login rejected"
    assert len(fake.calls) == 5


async def test_breaker_never_trips_on_differently_worded_failures() -> None:
    """12-testing P31 control: per-booking texts differ, so every row reaches the carrier."""
    fake = FakeCarrier(Carrier.MSC, lambda booking: CarrierLookupError(f"no record of booking {booking}"))
    router = make_router(native={Carrier.MSC: fake}, failure_limit=5)
    for i in range(1, 8):
        assert str(await lookup_error(router, f"EBKG1000000{i}")) == f"no record of booking EBKG1000000{i}"
    assert len(fake.calls) == 7
    assert not router.breaker_for("native", Carrier.MSC).tripped


@pytest.mark.parametrize("excluded", [
    UnsupportedCarrierError("Grimaldi ETA lookup not yet implemented"),
    AggregatorBookingError("not tracked yet — re-run later"),
    NoEtaError("No arrival at the port of discharge. Can't determine ETA."),
    CustomsHoldError("Potential customs hold"),
])
async def test_breaker_never_counts_the_excluded_error_types(excluded: Exception) -> None:
    fake = FakeCarrier(Carrier.CMA, lambda booking: type(excluded)(str(excluded)))
    router = make_router(native={Carrier.CMA: fake}, failure_limit=2)
    for i in range(1, 7):
        error = await lookup_error(router, f"NAM100000{i}")
        assert type(error) is type(excluded) and str(error) == str(excluded)
    breaker = router.breaker_for("native", Carrier.CMA)
    assert len(fake.calls) == 6
    assert breaker.count == 0 and not breaker.tripped


async def test_breaker_success_or_another_failure_ends_the_run() -> None:
    """10 §6: "Any success or differently-worded failure resets the count"; an uncounted failure (here a
    NoEtaError) never counts, and it ends the current run of identical failures as well."""
    outcomes: dict[str, Outcome] = {}
    fake = FakeCarrier(Carrier.MSC, lambda booking: outcomes.get(booking, CarrierLookupError(LOGIN_REJECTED)))
    router = make_router(native={Carrier.MSC: fake}, failure_limit=5)
    breaker = router.breaker_for("native", Carrier.MSC)
    outcomes["EBKG20000005"] = eta_for(Carrier.MSC, "EBKG20000005")
    outcomes["EBKG20000010"] = NoEtaError("Can't determine ETA.")
    outcomes["EBKG20000015"] = CarrierLookupError("MSC rate limit (HTTP 429)")
    counts = []
    for i in range(1, 21):
        booking = f"EBKG200000{i:02d}"
        try:
            await lookup(router, booking)
        except (CarrierLookupError, NoEtaError):
            pass
        counts.append(breaker.count)
    # 4 identical failures, success → 0, 4 failures, NoEtaError → 0, 4 failures, another text → 1 (new run),
    # then 5 identical failures → 1, 2, 3, 4, 5 → tripped on the last one
    assert counts == [1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 1, 1, 2, 3, 4, 5]
    assert breaker.tripped and breaker.last == LOGIN_REJECTED
    assert len(fake.calls) == 20
    assert "aborted after 5 identical consecutive failures" in str(await lookup_error(router, "EBKG20000021"))
    assert len(fake.calls) == 20


async def test_breaker_is_per_provider_and_carrier() -> None:
    msc_fake = FakeCarrier(Carrier.MSC, lambda booking: CarrierLookupError(LOGIN_REJECTED))
    cma_fake = FakeCarrier(Carrier.CMA)
    agg = FakeAggregator(lambda carrier, booking: CarrierLookupError(TOKEN_401) if carrier is Carrier.MAERSK
                         else eta_for(carrier, booking))
    router = make_router(providers={Carrier.MAERSK: "aggregator", Carrier.GRIMALDI: "aggregator"},
                         native={Carrier.MSC: msc_fake, Carrier.CMA: cma_fake}, aggregator=agg, failure_limit=2)
    for i in range(1, 4):
        await lookup_error(router, f"EBKG1000000{i}")
        await lookup_error(router, f"26754662{i}")
    assert router.breaker_for("native", Carrier.MSC).tripped
    assert router.breaker_for("aggregator", Carrier.MAERSK).tripped
    assert len(msc_fake.calls) == 2
    assert [call for call in agg.calls if call[0] is Carrier.MAERSK] == [
        (Carrier.MAERSK, "267546621"), (Carrier.MAERSK, "267546622")]
    # the other carriers, on the same or another provider, are unaffected
    assert await lookup(router, "NAM1234567") == eta_for(Carrier.CMA, "NAM1234567")
    assert await lookup(router, "S312345678") == eta_for(Carrier.GRIMALDI, "S312345678")
    assert router.breaker_for("aggregator", Carrier.GRIMALDI) is not router.breaker_for("aggregator", Carrier.MAERSK)
    assert router.breaker_for("native", Carrier.CMA) is not router.breaker_for("aggregator", Carrier.CMA)
    assert not router.breaker_for("native", Carrier.CMA).tripped


async def test_route_time_errors_never_reach_the_breaker() -> None:
    fake = FakeCarrier(Carrier.MSC)
    router = make_router(providers={Carrier.CMA: "off"}, native={Carrier.MSC: fake, Carrier.MAERSK: None},
                         missing={Carrier.MAERSK: ("MAERSK_CLIENT_ID", "MAERSK_CLIENT_SECRET")}, failure_limit=1)
    for _ in range(5):
        for base in ("EBKG17208602 C 2", "267546627", "NAM1234567", "S312345678", "XYZ", "2026123456"):
            with pytest.raises(CarrierLookupError):
                router.route(base)
    for carrier in Carrier:
        for provider in ("native", "off", "aggregator"):
            assert router.breaker_for(provider, carrier).count == 0
    assert await lookup(router, "EBKG17208602") == eta_for(Carrier.MSC, "EBKG17208602")


async def test_unexpected_errors_propagate_unchanged_and_are_not_counted() -> None:
    fake = FakeCarrier(Carrier.MSC, lambda booking: RuntimeError("x"))
    router = make_router(native={Carrier.MSC: fake}, failure_limit=1)
    for i in range(1, 4):
        error = await lookup_error(router, f"EBKG1000000{i}")
        assert type(error) is RuntimeError and str(error) == "x"   # → "Unexpected error during carrier lookup."
    assert len(fake.calls) == 3
    assert not router.breaker_for("native", Carrier.MSC).tripped


async def test_breaker_trip_is_logged_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="eta.carrier.router")
    fake = FakeCarrier(Carrier.MSC, lambda booking: CarrierLookupError(LOGIN_REJECTED))
    router = make_router(native={Carrier.MSC: fake}, failure_limit=2)
    for i in range(1, 6):
        await lookup_error(router, f"EBKG1000000{i}")
    warnings = [record for record in caplog.records
                if record.name == "eta.carrier.router" and record.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "circuit breaker tripped" in warnings[0].getMessage()
    assert LOGIN_REJECTED in warnings[0].getMessage()


def test_circuit_breaker_unit() -> None:
    breaker = CircuitBreaker("native", Carrier.MAERSK, 2)
    breaker.check()
    breaker.record_failure(CarrierLookupError("Maersk rejected the credentials (HTTP 401)"))
    assert (breaker.count, breaker.tripped) == (1, False)
    breaker.record_failure(CarrierLookupError("Maersk rejected the credentials (HTTP 401)"))
    assert breaker.tripped
    with pytest.raises(CarrierLookupError) as info:
        breaker.check()
    assert str(info.value) == ("Maersk lookups aborted after 2 identical consecutive failures; last error: "
                               "Maersk rejected the credentials (HTTP 401)")
    breaker.record_success()                          # e.g. a lookup that was already running succeeds
    assert (breaker.count, breaker.last, breaker.tripped) == (0, None, False)
    breaker.check()


def test_failure_limit_must_be_at_least_one() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        make_router(failure_limit=0)
    with pytest.raises(ValueError, match="at least 1"):
        CircuitBreaker("native", Carrier.MSC, 0)


@pytest.mark.parametrize(("error", "counted"), [
    (CarrierLookupError("x"), True),
    (UnsupportedCarrierError("x"), False),
    (AggregatorBookingError("x"), False),
    (NoEtaError("x"), False),
    (CustomsHoldError("x"), False),
    (RuntimeError("x"), False),
])
def test_counts_toward_breaker(error: Exception, counted: bool) -> None:
    assert counts_toward_breaker(error) is counted


# ════════════════════════════════════════════════════════════════════════════════════════════════
# aclose() and accessors
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_aclose_closes_every_client_once_and_views_are_no_ops() -> None:
    shared = FakeCarrier(Carrier.MSC)                # the same object for two carriers is closed once
    cma_fake = FakeCarrier(Carrier.CMA)
    agg = FakeAggregator()
    router = make_router(providers={Carrier.GRIMALDI: "aggregator"},
                         native={Carrier.MSC: shared, Carrier.MAERSK: shared, Carrier.CMA: cma_fake,
                                 Carrier.GRIMALDI: None}, aggregator=agg)
    _carrier, _token, view_client = router.route("S312345678")
    _carrier, _token, native_client = router.route("NAM1234567")
    await view_client.aclose()
    await native_client.aclose()
    assert agg.closed == 0 and cma_fake.closed == 0
    await router.aclose()
    await router.aclose()                            # idempotent
    assert (shared.closed, cma_fake.closed, agg.closed) == (1, 1, 1)


async def test_aclose_survives_a_failing_close(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.router")
    broken = FakeCarrier(Carrier.MSC)
    broken.close_error = RuntimeError("socket already gone")
    others = {Carrier.MAERSK: FakeCarrier(Carrier.MAERSK), Carrier.CMA: FakeCarrier(Carrier.CMA)}
    agg = FakeAggregator()
    router = make_router(native={Carrier.MSC: broken, **others}, aggregator=agg)
    await router.aclose()
    assert broken.closed == 1 and all(fake.closed == 1 for fake in others.values()) and agg.closed == 1
    assert "Could not close FakeCarrier: RuntimeError: socket already gone" in caplog.text


async def test_aclose_cancels_lookups_still_pending() -> None:
    fake = FakeCarrier(Carrier.MAERSK)
    fake.gate = asyncio.Event()                      # never released
    router = make_router(native={Carrier.MAERSK: fake})
    caller = asyncio.create_task(lookup(router, "267546627"))
    await settle()
    assert fake.calls == ["267546627"]
    await router.aclose()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert fake.closed == 1


async def test_router_is_an_async_context_manager() -> None:
    fakes = natives()
    async with make_router(native=fakes) as router:
        assert await lookup(router, "267546627") == eta_for(Carrier.MAERSK, "267546627")
    assert all(fake.closed == 1 for fake in fakes.values())


def test_accessors_and_provider_normalisation() -> None:
    fakes = natives()
    agg = FakeAggregator()
    router = CarrierRouter(providers={Carrier.MSC: " NATIVE ", Carrier.CMA: "Aggregator"}, native=fakes,
                           aggregator=agg, failure_limit=3)
    assert dict(router.providers) == {Carrier.MSC: "native", Carrier.MAERSK: "native", Carrier.CMA: "aggregator",
                                      Carrier.GRIMALDI: "off"}
    assert router.provider_for(Carrier.CMA) == "aggregator"
    assert router.native_client(Carrier.MAERSK) is fakes[Carrier.MAERSK]
    assert router.native_client(Carrier.GRIMALDI) is None
    assert router.aggregator is agg and router.failure_limit == 3
    with pytest.raises(TypeError):
        router.providers[Carrier.MSC] = "off"  # type: ignore[index]
    assert normalise_providers({}) == DEFAULT_PROVIDERS


# ════════════════════════════════════════════════════════════════════════════════════════════════
# build_router()
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_build_router_with_default_settings(make_settings: Callable[..., Settings], token_store: TokenStore,
                                                  fixed_clock: FixedClock) -> None:
    settings = make_settings()
    router = build_router(settings, token_store, clock=fixed_clock)
    try:
        assert isinstance(router.native_client(Carrier.MSC), msc.MscClient)        # auto → mymsc bridge
        assert router.native_client(Carrier.MAERSK) is None
        assert router.native_client(Carrier.CMA) is None
        assert router.native_client(Carrier.GRIMALDI) is None                      # provider "off" by default
        assert router.aggregator is None
        assert router.failure_limit == 5
        assert dict(router.providers) == DEFAULT_PROVIDERS

        carrier, token, client = router.route("EBKG17208602")
        assert (carrier, token) == (Carrier.MSC, "EBKG17208602")
        assert isinstance(client, GuardedClient) and client.inner is router.native_client(Carrier.MSC)
        expected = {
            "267546627": "Maersk lookups are not configured — set MAERSK_CLIENT_ID, MAERSK_CLIENT_SECRET in .env",
            "NAM1234567": "CMA lookups are not configured — set CMA_API_KEY (or CMA_CLIENT_ID + CMA_CLIENT_SECRET) "
                          "in .env",
        }
        for base, message in expected.items():
            with pytest.raises(CarrierLookupError) as info:
                router.route(base)
            assert str(info.value) == message
        with pytest.raises(UnsupportedCarrierError) as grimaldi_info:
            router.route("S312345678")
        assert str(grimaldi_info.value) == GRIMALDI_TEXT
    finally:
        await router.aclose()


async def test_build_router_reports_missing_msc_api_settings(make_settings: Callable[..., Settings],
                                                             token_store: TokenStore) -> None:
    settings = make_settings(MSC_MODE="api", ETA_PROVIDER_GRIMALDI="native")
    router = build_router(settings, token_store)
    try:
        with pytest.raises(CarrierLookupError) as info:
            router.route("EBKG17208602")
        assert str(info.value) == ("MSC lookups are not configured — set MSC_API_BASE_URL, MSC_API_SUBSCRIPTION_KEY "
                                   "in .env")
        assert isinstance(router.native_client(Carrier.GRIMALDI), grimaldi.GrimaldiClient)
        with pytest.raises(UnsupportedCarrierError) as grimaldi_info:   # native Grimaldi = the parity stub text
            router.route("S312345678")
        assert str(grimaldi_info.value) == GRIMALDI_TEXT
    finally:
        await router.aclose()


async def test_build_router_with_configured_carriers_and_a_real_aggregator(
        make_settings: Callable[..., Settings], token_store: TokenStore, fixed_clock: FixedClock) -> None:
    settings = make_settings(MAERSK_CLIENT_ID="maersk-id", MAERSK_CLIENT_SECRET="maersk-secret",
                             CMA_API_KEY="cma-key", AGG_API_TOKEN="agg-token", ETA_PROVIDER_MAERSK="aggregator",
                             ETA_PROVIDER_GRIMALDI="AGGREGATOR", ETA_CARRIER_FAILURE_LIMIT="2")
    router = build_router(settings, token_store, clock=fixed_clock)
    try:
        assert isinstance(router.aggregator, AggregatorClient)
        assert isinstance(router.native_client(Carrier.CMA), cma.CmaClient)
        assert router.native_client(Carrier.MAERSK) is None          # routed to the aggregator: not built
        assert router.failure_limit == 2
        for base, carrier in (("267546627", Carrier.MAERSK), ("S312345678", Carrier.GRIMALDI)):
            routed, token, client = router.route(base)
            assert (routed, token) == (carrier, base)
            assert isinstance(client, GuardedClient) and client.provider == "aggregator"
            assert isinstance(client.inner, AggregatorCarrierView) and client.inner.carrier is carrier
            assert client.inner.client is router.aggregator
            assert client.breaker.limit == 2
        _carrier, _token, cma_client = router.route("NAM1234567")
        assert isinstance(cma_client, GuardedClient)
        assert cma_client.inner is router.native_client(Carrier.CMA) and cma_client.provider == "native"
    finally:
        await router.aclose()


async def test_build_router_passes_settings_clock_and_http_to_each_factory(
        make_settings: Callable[..., Settings], token_store: TokenStore, fixed_clock: FixedClock,
        monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, Settings, TokenStore, Any, Any]] = []
    built: dict[str, Any] = {}

    def recorder(name: str, make: Callable[[], Any]) -> Callable[..., Any]:
        def factory(settings: Settings, store: TokenStore, *, clock: Any = None, http: Any = None) -> Any:
            calls.append((name, settings, store, clock, http))
            built[name] = make()
            return built[name]
        return factory

    monkeypatch.setattr(msc, "build_client", recorder("msc", lambda: FakeCarrier(Carrier.MSC)))
    monkeypatch.setattr(maersk, "build_client", recorder("maersk", lambda: FakeCarrier(Carrier.MAERSK)))
    monkeypatch.setattr(cma, "build_client", recorder("cma", lambda: FakeCarrier(Carrier.CMA)))
    monkeypatch.setattr(grimaldi, "build_client", recorder("grimaldi", lambda: FakeCarrier(Carrier.GRIMALDI)))
    monkeypatch.setattr(aggregator_module, "build_client", recorder("aggregator", FakeAggregator))

    async with httpx.AsyncClient() as http:
        settings = make_settings(ETA_PROVIDER_GRIMALDI="native")
        router = build_router(settings, token_store, clock=fixed_clock, http=http)
        assert [call[0] for call in calls] == ["msc", "maersk", "cma", "grimaldi"]   # no aggregator needed
        assert all(call[1] is settings and call[2] is token_store and call[3] is fixed_clock and call[4] is http
                   for call in calls)
        for carrier, name in ((Carrier.MSC, "msc"), (Carrier.MAERSK, "maersk"), (Carrier.CMA, "cma"),
                              (Carrier.GRIMALDI, "grimaldi")):
            assert router.native_client(carrier) is built[name]
        await router.aclose()
        assert all(built[name].closed == 1 for name in ("msc", "maersk", "cma", "grimaldi"))

        calls.clear()
        built.clear()
        settings = make_settings(ETA_PROVIDER_MSC="aggregator", ETA_PROVIDER_CMA="off")
        router = build_router(settings, token_store, clock=fixed_clock, http=http)
        assert [call[0] for call in calls] == ["maersk", "aggregator"]   # off/aggregator carriers: no native client
        assert all(call[3] is fixed_clock and call[4] is http for call in calls)
        assert router.aggregator is built["aggregator"]
        assert router.native_client(Carrier.MSC) is None and router.native_client(Carrier.CMA) is None
        _carrier, token, client = router.route("EBKG17208602")
        assert isinstance(client, GuardedClient) and isinstance(client.inner, AggregatorCarrierView)
        await client.get_eta(token)
        assert built["aggregator"].calls == [(Carrier.MSC, "EBKG17208602")]
        with pytest.raises(CarrierLookupError) as info:
            router.route("NAM1234567")
        assert str(info.value) == "CMA lookups are disabled (ETA_PROVIDER_CMA=off)"
        await router.aclose()
        assert built["maersk"].closed == 1 and built["aggregator"].closed == 1


async def test_build_router_aggregator_selected_but_not_configured(make_settings: Callable[..., Settings],
                                                                   token_store: TokenStore) -> None:
    router = build_router(make_settings(ETA_PROVIDER_CMA="aggregator"), token_store)
    try:
        assert router.aggregator is None
        with pytest.raises(CarrierLookupError) as info:
            router.route("NAM1234567")
        assert str(info.value) == ("CMA is set to use the aggregator (ETA_PROVIDER_CMA=aggregator) but the "
                                   "aggregator is not configured — set AGG_API_TOKEN in .env")
        await router.prepare(["NAM1234567"])           # no aggregator → no-op
    finally:
        await router.aclose()
