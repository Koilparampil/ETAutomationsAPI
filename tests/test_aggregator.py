"""ShipsGo aggregator (09 Part B) — 09 §B.10 tests 1–20, the expected-results table, plus edge cases.

respx mocks every HTTP call on https://api.shipsgo.test/v2 (no network, 12-testing §1); the poll `sleep` is
injected as a recording async no-op and the clock is FixedClock(2026-11-05 09:30). Fixtures:
tests/fixtures/aggregator/ (verbatim from 09 §B.10).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eta_automations.carriers import aggregator, dcsa
from eta_automations.carriers.aggregator import (
    AGG_POLICY,
    NOT_TRACKED_NOTE,
    ROUTE_POD_BASIS,
    AggregatorBackend,
    AggregatorCarrierView,
    AggregatorClient,
    AggregatorSnapshot,
    ShipsGoBackend,
    TrackingState,
    build_client,
    snapshot_from_shipsgo,
)
from eta_automations.carriers.base import CarrierClient
from eta_automations.clock import FixedClock
from eta_automations.config import AggregatorSettings
from eta_automations.errors import (
    AggregatorBookingError,
    CarrierLookupError,
    ConfigError,
    NoEtaError,
)
from eta_automations.models import Carrier, EtaResult

FIX = Path(__file__).parent / "fixtures" / "aggregator"
BASE = "https://api.shipsgo.test/v2"
TOKEN = "sg-user-token-TEST-0123456789abcdef"
RUN_NOW = datetime(2026, 11, 5, 9, 30)
NOW_ISO = "2026-11-05T09:30:00"
TWO_DAYS_AGO = "2026-11-03T09:30:00"
LIST = "/ocean/shipments"
CARRIERS = "/ocean/carriers"

MSC_BOOKING = "EBKG12345678"      # tracked as shipment 501234 (sailing / discharged fixtures)
NEW_BOOKING = "EBKG87654321"      # created as shipment 501300 (create_200 / in-progress fixtures)


def fixture_json(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def respond(name_or_body: str | Mapping[str, Any], status: int = 200) -> httpx.Response:
    body = fixture_json(name_or_body) if isinstance(name_or_body, str) else dict(name_or_body)
    return httpx.Response(status, json=body)


def detail_path(tid: str | int) -> str:
    return f"/ocean/shipments/{tid}"


def sailing_without_containers() -> dict[str, Any]:
    body = fixture_json("shipsgo_detail_sailing.json")
    body["shipment"]["containers"] = []
    return body


class Sleeps:
    """Injected poll sleep: records the requested delays, returns at once."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# fixtures
# ════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def agg_settings(make_settings: Callable[..., Any]) -> Callable[..., AggregatorSettings]:
    """agg_settings(**env) -> AggregatorSettings for https://api.shipsgo.test/v2 (state file in tmp)."""

    def _make(**env: Any) -> AggregatorSettings:
        settings = make_settings({"AGG_API_TOKEN": TOKEN, "AGG_BASE_URL": BASE,
                                  **{name: str(value) for name, value in env.items()}})
        assert settings.aggregator is not None
        return settings.aggregator

    return _make


@pytest.fixture
def api() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=BASE, assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
def sleeps() -> Sleeps:
    return Sleeps()


@pytest.fixture
async def make_client(agg_settings: Callable[..., AggregatorSettings], fixed_clock: FixedClock,
                      sleeps: Sleeps) -> AsyncIterator[Callable[..., AggregatorClient]]:
    """make_client(settings=None, *, backend=None, state=None, **env) — closed at teardown."""
    made: list[AggregatorClient] = []

    def _make(settings: AggregatorSettings | None = None, *, backend: Any = None,
              state: TrackingState | None = None, **env: Any) -> AggregatorClient:
        client = AggregatorClient(settings or agg_settings(**env), backend=backend, state=state,
                                  clock=fixed_clock, sleep=sleeps)
        made.append(client)
        return client

    yield _make
    for client in made:
        await client.aclose()


def seed(client: AggregatorClient, scac: str, booking: str, tid: str, *, first_seen_at: str = TWO_DAYS_AGO,
         created_by_us: bool = True) -> None:
    client.state.put("shipsgo", scac, booking, {"tracking_id": tid, "created_by_us": created_by_us,
                                                 "first_seen_at": first_seen_at})


def state_entry(client: AggregatorClient, scac: str, booking: str) -> dict[str, Any] | None:
    """The entry as persisted on disk (a fresh TrackingState reads the file)."""
    return TrackingState(client.settings.state_file).get("shipsgo", scac, booking)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 1. build_client
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_build_client_none_when_unconfigured(make_settings: Callable[..., Any], token_store: Any) -> None:
    assert build_client(make_settings(), token_store) is None

    settings = make_settings({"AGG_API_TOKEN": TOKEN, "AGG_BASE_URL": BASE})
    client = build_client(settings, token_store)
    try:
        assert isinstance(client, AggregatorClient)
        assert isinstance(client.backend, ShipsGoBackend)
        assert isinstance(client.backend, AggregatorBackend)
        assert settings.aggregator is not None
        assert client.state.path == settings.aggregator.state_file
        view = client.view(Carrier.MSC)
        assert isinstance(view, CarrierClient) and view.carrier is Carrier.MSC
        assert TOKEN not in repr(client) and TOKEN not in repr(client.backend)
    finally:
        assert client is not None
        await client.aclose()


def test_build_client_rejects_unimplemented_provider(make_settings: Callable[..., Any], token_store: Any) -> None:
    import dataclasses

    settings = make_settings({"AGG_API_TOKEN": TOKEN})
    assert settings.aggregator is not None
    odd = dataclasses.replace(settings, aggregator=dataclasses.replace(settings.aggregator, provider="terminal49"))
    with pytest.raises(ConfigError, match=r"^AGG_PROVIDER=terminal49 is described in 09 §B\.9 but not implemented$"):
        build_client(odd, token_store)


def test_agg_policy_parity() -> None:
    assert dict(AGG_POLICY) == {Carrier.MSC: "first", Carrier.MAERSK: "first", Carrier.CMA: "latest",
                                Carrier.GRIMALDI: "latest"}


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 2–4. resolving the tracking id
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_known_state_entry_skips_lookup(api: respx.MockRouter,
                                              make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    seed(client, "MSCU", MSC_BOOKING, "501234")
    listing = api.get(LIST).mock(return_value=respond("shipsgo_list_found.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))
    details = api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    result = await client.get_eta(Carrier.MSC, MSC_BOOKING)

    assert result.eta == datetime(2026, 11, 12, 7, 30)
    assert details.call_count == 1
    assert listing.call_count == 0 and create.call_count == 0
    request = details.calls.last.request
    assert request.headers["X-Shipsgo-User-Token"] == TOKEN
    assert request.headers["Accept"] == "application/json"
    entry = state_entry(client, "MSCU", MSC_BOOKING)
    assert entry is not None
    assert entry["last_status"] == "SAILING" and entry["last_checked_at"] == NOW_ISO
    assert entry["first_seen_at"] == TWO_DAYS_AGO and entry["tracking_id"] == "501234"


async def test_existing_found_by_list_no_create(api: respx.MockRouter,
                                                make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    listing = api.get(LIST).mock(return_value=respond("shipsgo_list_found.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))
    details = api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    result = await client.get_eta(Carrier.MSC, MSC_BOOKING)

    assert result.eta == datetime(2026, 11, 12, 7, 30)
    assert create.call_count == 0
    assert listing.call_count == 1 and details.call_count == 1
    params = listing.calls.last.request.url.params
    assert params["filters[booking_number]"] == "eq:EBKG12345678"
    assert params["filters[carrier]"] == "eq:MSCU"
    assert params["take"] == "25"
    assert listing.calls.last.request.headers["X-Shipsgo-User-Token"] == TOKEN
    assert state_entry(client, "MSCU", MSC_BOOKING) == {
        "tracking_id": "501234", "created_by_us": False, "first_seen_at": NOW_ISO,
        "last_status": "SAILING", "last_checked_at": NOW_ISO,
    }
    assert client.creates_this_run == 0


async def test_create_when_absent(api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_FIRST_LOOKUP_WAIT_S=0)
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))
    details = api.get(detail_path(501300)).mock(return_value=respond("shipsgo_detail_inprogress.json"))

    with pytest.raises(AggregatorBookingError, match="re-run later"):
        await client.get_eta(Carrier.MSC, NEW_BOOKING)

    assert create.call_count == 1
    request = create.calls.last.request
    assert json.loads(request.content) == {"booking_number": "EBKG87654321", "carrier": "MSCU"}
    assert request.headers["X-Shipsgo-User-Token"] == TOKEN
    assert request.headers["Content-Type"] == "application/json"
    assert details.call_count == 1  # the detail was fetched right after the create
    assert state_entry(client, "MSCU", NEW_BOOKING) == {
        "tracking_id": "501300", "created_by_us": True, "first_seen_at": NOW_ISO,
        "last_status": "INPROGRESS", "last_checked_at": NOW_ISO,
    }
    assert client.creates_this_run == 1


async def test_create_409_is_success_and_not_counted(api: respx.MockRouter,
                                                     make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_MAX_CREATES_PER_RUN=1)
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(side_effect=[respond("shipsgo_create_409.json", 409),
                                              respond("shipsgo_create_200.json")])
    api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))
    api.get(detail_path(501300)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    result = await client.get_eta(Carrier.MSC, MSC_BOOKING)
    assert result.eta == datetime(2026, 11, 12, 7, 30)
    assert result.details["tracking_id"] == "501234"
    assert client.creates_this_run == 0          # 409 ALREADY_EXISTS: no charge, not counted
    assert state_entry(client, "MSCU", MSC_BOOKING)["tracking_id"] == "501234"  # type: ignore[index]

    # the cap of 1 is still available for a really new booking
    await client.get_eta(Carrier.MSC, NEW_BOOKING)
    assert create.call_count == 2
    assert client.creates_this_run == 1


async def test_create_402_plain_lookup_error(api: respx.MockRouter,
                                             make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_402.json", 402))

    with pytest.raises(CarrierLookupError) as info:
        await client.get_eta(Carrier.MSC, NEW_BOOKING)

    assert not isinstance(info.value, AggregatorBookingError)  # systemic: the circuit breaker counts it
    assert "credits" in str(info.value)
    assert str(info.value) == ("ShipsGo: not enough credits to start tracking a new booking (HTTP 402) — buy "
                               "credits or set AGG_ALLOW_CREATE=false")
    assert create.call_count == 1  # never retried
    assert state_entry(client, "MSCU", NEW_BOOKING) is None
    assert client.creates_this_run == 0


async def test_allow_create_false(api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_ALLOW_CREATE="false")
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))

    with pytest.raises(AggregatorBookingError) as info:
        await client.get_eta(Carrier.MSC, NEW_BOOKING)

    assert str(info.value) == "MSC booking EBKG87654321 is not tracked in ShipsGo and AGG_ALLOW_CREATE=false"
    assert create.call_count == 0


async def test_create_cap(api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_MAX_CREATES_PER_RUN=1)
    seed(client, "MSCU", MSC_BOOKING, "501234")
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))
    api.get(detail_path(501300)).mock(return_value=respond("shipsgo_detail_sailing.json"))
    api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    first = await client.get_eta(Carrier.MSC, NEW_BOOKING)
    assert first.details["tracking_id"] == "501300"

    with pytest.raises(AggregatorBookingError) as info:
        await client.get_eta(Carrier.MSC, "EBKG11112222")
    assert str(info.value) == ("AGG_MAX_CREATES_PER_RUN (1) reached — booking EBKG11112222 not registered with "
                               "ShipsGo this run")
    assert create.call_count == 1

    # an already-tracked booking still succeeds
    known = await client.get_eta(Carrier.MSC, MSC_BOOKING)
    assert known.eta == datetime(2026, 11, 12, 7, 30)


async def test_create_cap_zero_never_creates(api: respx.MockRouter,
                                             make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_MAX_CREATES_PER_RUN=0)
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))
    with pytest.raises(AggregatorBookingError, match=r"^AGG_MAX_CREATES_PER_RUN \(0\) reached"):
        await client.get_eta(Carrier.MSC, NEW_BOOKING)
    assert create.call_count == 0


async def test_creates_are_serialised(api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_MAX_CONCURRENCY=3)  # the HTTP semaphore alone would let the POSTs overlap
    bookings = {"EBKG10000001": 601, "EBKG10000002": 602, "EBKG10000003": 603}
    in_flight = 0
    max_in_flight = 0

    async def create_side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            in_flight -= 1
        booking = json.loads(request.content)["booking_number"]
        return httpx.Response(200, json={"message": "SUCCESS", "shipment": {"id": bookings[booking]}})

    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(side_effect=create_side_effect)
    for tid in bookings.values():
        api.get(detail_path(tid)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    results = await asyncio.gather(*(client.get_eta(Carrier.MSC, b) for b in bookings))

    assert [r.details["tracking_id"] for r in results] == ["601", "602", "603"]
    assert create.call_count == 3
    assert max_in_flight == 1
    assert client.creates_this_run == 3


async def test_concurrent_create_cap_is_rechecked_under_the_lock(
        api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_MAX_CREATES_PER_RUN=1)
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))
    api.get(detail_path(501300)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    results = await asyncio.gather(*(client.get_eta(Carrier.MSC, b) for b in ("EBKG20000001", "EBKG20000002")),
                                   return_exceptions=True)

    assert create.call_count == 1
    assert sum(isinstance(r, EtaResult) for r in results) == 1
    assert sum(isinstance(r, AggregatorBookingError) and "AGG_MAX_CREATES_PER_RUN (1) reached" in str(r)
               for r in results) == 1


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 10–14. readiness, polling and lost shipments
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_inprogress_then_ready(api: respx.MockRouter, make_client: Callable[..., AggregatorClient],
                                     sleeps: Sleeps) -> None:
    client = make_client(AGG_FIRST_LOOKUP_WAIT_S=30, AGG_POLL_INTERVAL_S=10)
    seed(client, "MSCU", MSC_BOOKING, "501234", first_seen_at=NOW_ISO)  # fresh
    details = api.get(detail_path(501234)).mock(side_effect=[respond("shipsgo_detail_inprogress.json"),
                                                             respond("shipsgo_detail_sailing.json")])

    result = await client.get_eta(Carrier.MSC, MSC_BOOKING)

    assert result.eta == datetime(2026, 11, 12, 7, 30)
    assert details.call_count == 2
    assert sleeps.calls == [10.0]


async def test_inprogress_no_wait(api: respx.MockRouter, make_client: Callable[..., AggregatorClient],
                                  sleeps: Sleeps) -> None:
    client = make_client(AGG_FIRST_LOOKUP_WAIT_S=0)
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))
    details = api.get(detail_path(501300)).mock(return_value=respond("shipsgo_detail_inprogress.json"))

    with pytest.raises(AggregatorBookingError) as info:
        await client.get_eta(Carrier.MSC, NEW_BOOKING)

    assert str(info.value) == ("ShipsGo has not fetched MSC data for booking EBKG87654321 yet (status INPROGRESS, "
                               "tracking since 2026-11-05T09:30:00) — re-run later")
    assert details.call_count == 1
    assert sleeps.calls == []


async def test_stale_entry_not_waited(api: respx.MockRouter, make_client: Callable[..., AggregatorClient],
                                      sleeps: Sleeps) -> None:
    client = make_client(AGG_FIRST_LOOKUP_WAIT_S=45)
    seed(client, "MSCU", NEW_BOOKING, "501300", first_seen_at=TWO_DAYS_AGO)
    details = api.get(detail_path(501300)).mock(return_value=respond("shipsgo_detail_inprogress.json"))

    with pytest.raises(AggregatorBookingError, match="re-run later") as info:
        await client.get_eta(Carrier.MSC, NEW_BOOKING)

    assert "tracking since 2026-11-03T09:30:00" in str(info.value)
    assert details.call_count == 1
    assert sleeps.calls == []


async def test_fresh_shipment_polls_at_most_until_the_wait_ends(
        api: respx.MockRouter, make_client: Callable[..., AggregatorClient], sleeps: Sleeps) -> None:
    client = make_client(AGG_FIRST_LOOKUP_WAIT_S=45, AGG_POLL_INTERVAL_S=15)
    seed(client, "MSCU", NEW_BOOKING, "501300", first_seen_at="2026-11-05T09:10:00")  # 20 min ago: fresh
    details = api.get(detail_path(501300)).mock(return_value=respond("shipsgo_detail_inprogress.json"))

    with pytest.raises(AggregatorBookingError, match="re-run later"):
        await client.get_eta(Carrier.MSC, NEW_BOOKING)

    assert sleeps.calls == [15.0, 15.0, 15.0]
    assert details.call_count == 4


async def test_untracked(api: respx.MockRouter, make_client: Callable[..., AggregatorClient], sleeps: Sleeps) -> None:
    client = make_client()
    seed(client, "CMDU", "NAM7654321", "501301", first_seen_at=NOW_ISO)  # fresh, but a failure is final
    details = api.get(detail_path(501301)).mock(return_value=respond("shipsgo_detail_untracked.json"))

    with pytest.raises(AggregatorBookingError) as info:
        await client.view(Carrier.CMA).get_eta("NAM7654321")

    assert "could not track" in str(info.value)
    assert str(info.value) == ("ShipsGo could not track CMA booking NAM7654321 (status UNTRACKED) — the number may "
                               "be wrong or CMA publishes no data for it")
    assert details.call_count == 1 and sleeps.calls == []
    assert state_entry(client, "CMDU", "NAM7654321")["last_status"] == "UNTRACKED"  # type: ignore[index]


async def test_detail_404_forgets_and_retries_once(api: respx.MockRouter,
                                                   make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    seed(client, "MSCU", MSC_BOOKING, "999")
    gone = api.get(detail_path(999)).mock(return_value=httpx.Response(404, json={"message": "NOT_FOUND"}))
    listing = api.get(LIST).mock(return_value=respond("shipsgo_list_found.json"))
    details = api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    result = await client.get_eta(Carrier.MSC, MSC_BOOKING)

    assert result.details["tracking_id"] == "501234"
    assert gone.call_count == 1 and listing.call_count == 1 and details.call_count == 1
    entry = state_entry(client, "MSCU", MSC_BOOKING)
    assert entry is not None and entry["tracking_id"] == "501234" and entry["created_by_us"] is False


async def test_detail_404_twice_is_a_plain_lookup_error(api: respx.MockRouter,
                                                        make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    seed(client, "MSCU", MSC_BOOKING, "999")
    api.get(detail_path(999)).mock(return_value=httpx.Response(404))
    listing = api.get(LIST).mock(return_value=respond("shipsgo_list_found.json"))
    second = api.get(detail_path(501234)).mock(return_value=httpx.Response(404))

    with pytest.raises(CarrierLookupError) as info:
        await client.get_eta(Carrier.MSC, MSC_BOOKING)

    assert not isinstance(info.value, AggregatorBookingError)
    assert str(info.value) == "ShipsGo lost shipment 501234 for booking EBKG12345678"
    assert listing.call_count == 1 and second.call_count == 1  # re-resolved exactly once


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 15. expected results (09 §B.10 table)
# ════════════════════════════════════════════════════════════════════════════════════════════════
EXPECTED_CASES = [
    pytest.param(Carrier.MSC, MSC_BOOKING, "MSCU", "shipsgo_detail_sailing.json",
                 {"eta": datetime(2026, 11, 12, 7, 30), "is_actual": False,
                  "source": "ShipsGo TRANSPORT ARRI EST at USNYC",
                  "containers": ("MEDU1234567", "MEDU7654321"), "policy": "first"},
                 id="sailing-msc-first"),
    pytest.param(Carrier.CMA, "NAM1234567", "CMDU", "shipsgo_detail_sailing.json",
                 {"eta": datetime(2026, 11, 14, 6, 0), "is_actual": False,
                  "source": "ShipsGo TRANSPORT ARRI EST at USNYC", "policy": "latest"},
                 id="sailing-cma-latest"),
    pytest.param(Carrier.MSC, MSC_BOOKING, "MSCU", "shipsgo_detail_discharged.json",
                 {"eta": datetime(2026, 11, 2, 21, 40), "is_actual": True,
                  "source": "ShipsGo EQUIPMENT DISC ACT at USNYC", "containers": ("MEDU1234567",),
                  "status": "DISCHARGED"},
                 id="discharged"),
    pytest.param(Carrier.MSC, MSC_BOOKING, "MSCU", "SAILING_WITHOUT_CONTAINERS",
                 {"eta": datetime(2026, 11, 12, 13, 0), "is_actual": False, "classifier": "EST",
                  "basis": ROUTE_POD_BASIS, "source": "ShipsGo ROUTE POD date_of_discharge at USNYC",
                  "containers": ()},
                 id="route-fallback"),
    pytest.param(Carrier.MSC, NEW_BOOKING, "MSCU", "shipsgo_detail_inprogress.json",
                 {"error": (AggregatorBookingError, "re-run later")}, id="inprogress-no-wait"),
    pytest.param(Carrier.CMA, "NAM7654321", "CMDU", "shipsgo_detail_untracked.json",
                 {"error": (AggregatorBookingError, "could not track")}, id="untracked"),
]


@pytest.mark.parametrize(("carrier", "booking", "scac", "payload", "expected"), EXPECTED_CASES)
async def test_expected_results(api: respx.MockRouter, make_client: Callable[..., AggregatorClient],
                                carrier: Carrier, booking: str, scac: str, payload: str,
                                expected: dict[str, Any]) -> None:
    client = make_client(AGG_FIRST_LOOKUP_WAIT_S=0)
    seed(client, scac, booking, "501234", first_seen_at=NOW_ISO)
    body = sailing_without_containers() if payload == "SAILING_WITHOUT_CONTAINERS" else fixture_json(payload)
    api.get(detail_path(501234)).mock(return_value=respond(body))
    view = client.view(carrier)

    if "error" in expected:
        error_type, fragment = expected["error"]
        with pytest.raises(error_type, match=fragment):
            await view.get_eta(booking)
        return

    result = await view.get_eta(booking)
    assert result.carrier is carrier and result.booking == booking
    assert result.eta == expected["eta"]
    assert result.is_actual is expected["is_actual"]
    assert result.source == expected["source"]
    if "containers" in expected:
        assert result.containers == expected["containers"]
    for key in ("classifier", "basis"):
        if key in expected:
            assert result.details[key] == expected[key]
    assert result.details["tracking_id"] == "501234"
    assert result.details["provider"] == "shipsgo"
    assert result.details["policy"] == expected.get("policy", AGG_POLICY[carrier])
    assert result.details["status"] == expected.get("status", "SAILING")
    assert result.details["pod"] == "USNYC"
    json.dumps(result.to_dict())  # JSON-serializable details


async def test_no_pod_date_raises_no_eta(api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    seed(client, "MSCU", MSC_BOOKING, "501234")
    body = sailing_without_containers()
    body["shipment"]["route"]["port_of_discharge"]["date_of_discharge"] = None
    api.get(detail_path(501234)).mock(return_value=respond(body))

    with pytest.raises(NoEtaError) as info:
        await client.get_eta(Carrier.MSC, MSC_BOOKING)
    assert str(info.value) == ("ShipsGo found MSC booking EBKG12345678 but has no arrival date for the port of "
                               "discharge. Can't determine ETA.")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 16. HTTP errors
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_401_message_mentions_token_var(api: respx.MockRouter,
                                              make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    seed(client, "MSCU", MSC_BOOKING, "501234")
    details = api.get(detail_path(501234)).mock(return_value=httpx.Response(401, json={"message": "UNAUTHORIZED"}))

    with pytest.raises(CarrierLookupError) as info:
        await client.get_eta(Carrier.MSC, MSC_BOOKING)

    assert not isinstance(info.value, AggregatorBookingError)
    assert "AGG_API_TOKEN" in str(info.value)
    assert str(info.value) == "ShipsGo rejected the API token (HTTP 401) — check AGG_API_TOKEN"
    assert TOKEN not in str(info.value)
    assert details.call_count == 1  # 401 is never retried


async def test_403_on_list_message(api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    api.get(LIST).mock(return_value=httpx.Response(403, json={"message": "FORBIDDEN"}))
    with pytest.raises(CarrierLookupError) as info:
        await client.get_eta(Carrier.MSC, NEW_BOOKING)
    assert str(info.value) == ("ShipsGo refused the operation (HTTP 403) — the token's user role may not create or "
                               "read shipments")


async def test_422_on_create_names_booking_and_carrier(api: respx.MockRouter,
                                                       make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    api.post(LIST).mock(return_value=httpx.Response(422, json={"message": "INVALID_CARRIER"}))
    with pytest.raises(CarrierLookupError) as info:
        await client.get_eta(Carrier.MSC, NEW_BOOKING)
    assert str(info.value) == "ShipsGo rejected booking EBKG87654321 for carrier MSCU (HTTP 422): INVALID_CARRIER"
    assert client.creates_this_run == 0


async def test_server_error_and_network_error_become_lookup_errors(
        api: respx.MockRouter, make_client: Callable[..., AggregatorClient], http_sleeps: list[float]) -> None:
    client = make_client()
    seed(client, "MSCU", MSC_BOOKING, "501234")
    details = api.get(detail_path(501234)).mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(CarrierLookupError) as info:
        await client.get_eta(Carrier.MSC, MSC_BOOKING)
    assert str(info.value).startswith("ShipsGo API error for booking EBKG12345678: aggregator: unexpected HTTP 500")
    assert details.call_count == 4  # idempotent GET: retried by http.request (03 §6)

    details.mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(CarrierLookupError, match=r"^ShipsGo API error for booking EBKG12345678: aggregator: network "
                                                 r"error: ConnectError"):
        await client.get_eta(Carrier.MSC, MSC_BOOKING)


async def test_create_read_timeout_not_retried_and_counted(
        api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_MAX_CREATES_PER_RUN=1)
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(CarrierLookupError, match="network error"):
        await client.get_eta(Carrier.MSC, NEW_BOOKING)
    assert create.call_count == 1  # no retry: no double charge (09 §B.7.2)
    # the provider may have registered (and charged) it, so it counts against the cap
    assert client.creates_this_run == 1
    with pytest.raises(AggregatorBookingError, match="AGG_MAX_CREATES_PER_RUN"):
        await client.get_eta(Carrier.MSC, "EBKG11112222")


async def test_unexpected_payload_is_a_lookup_error(api: respx.MockRouter,
                                                    make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    seed(client, "MSCU", MSC_BOOKING, "501234")
    api.get(detail_path(501234)).mock(return_value=httpx.Response(200, json={"message": "SUCCESS", "items": []}))
    with pytest.raises(CarrierLookupError, match=r"^ShipsGo returned an unexpected payload: no 'shipment' object"):
        await client.get_eta(Carrier.MSC, MSC_BOOKING)

    api.get(detail_path(501234)).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(CarrierLookupError, match="response was not valid JSON"):
        await client.get_eta(Carrier.MSC, MSC_BOOKING)


async def test_invalid_booking_number_makes_no_request(api: respx.MockRouter,
                                                       make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    with pytest.raises(AggregatorBookingError, match="only letters, digits"):
        await client.get_eta(Carrier.MSC, "EBKG 1234 C")
    assert api.calls.call_count == 0


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 17. probe
# ════════════════════════════════════════════════════════════════════════════════════════════════
CARRIER_LIST = {
    "message": "SUCCESS",
    "carriers": [
        {"scac": "MSCU", "name": "MSC", "status": "ACTIVE"},
        {"scac": "MAEU", "name": "Maersk", "status": "ACTIVE"},
        {"scac": "CMDU", "name": "CMA CGM", "status": "ACTIVE"},
        {"scac": "GRIU", "name": "Grimaldi Lines", "status": "ACTIVE"},
        {"scac": "HLCU", "name": "Hapag-Lloyd", "status": "ACTIVE"},
    ],
    "meta": {"more": False, "total": 5},
}


async def test_probe_never_creates_and_redacts(api: respx.MockRouter,
                                               make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    api.get(CARRIERS).mock(return_value=respond(CARRIER_LIST))
    api.get(LIST).mock(return_value=respond("shipsgo_list_found.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))
    api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    out = await client.probe(Carrier.MSC, MSC_BOOKING)

    assert create.call_count == 0
    text = json.dumps(out)
    assert TOKEN not in text
    assert out["provider"] == "shipsgo" and out["account_ok"] is True and out["error"] is None
    assert {c["scac"] for c in out["carriers"]} == {"MSCU", "MAEU", "CMDU", "GRIU"}
    assert out["scac_check"]["Grimaldi"] == {"scac": "GRIU", "active": True}
    assert out["state_entry"] is None and out["find_existing"] == "501234"
    assert out["raw"]["shipment"]["tokens"] == "***"  # redacted copy of the body
    assert out["snapshot"]["status"] == "SAILING" and out["snapshot"]["ready"] is True
    assert out["snapshot"]["pod"] == "USNYC"
    assert out["snapshot"]["containers"] == ["MEDU1234567", "MEDU7654321"]
    explain = "\n".join(out["explain"])
    assert "USNYC" in explain and "2026-11-12T07:30:00" in explain
    assert out["eta"]["eta"] == "2026-11-12T07:30:00"
    assert out["eta"]["source"] == "ShipsGo TRANSPORT ARRI EST at USNYC"
    assert [r["path"] for r in out["requests"]] == [CARRIERS, LIST, detail_path(501234)]
    assert not client.settings.state_file.exists()  # read-only: nothing written


async def test_probe_untracked_booking_notes_the_credit(api: respx.MockRouter,
                                                        make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    api.get(CARRIERS).mock(return_value=respond(CARRIER_LIST))
    api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    create = api.post(LIST).mock(return_value=respond("shipsgo_create_200.json"))

    out = await client.view(Carrier.MSC).probe(NEW_BOOKING)

    assert create.call_count == 0
    assert out["find_existing"] is None
    assert out["note"] == NOT_TRACKED_NOTE == "not tracked yet — eta run / eta eta would register it (1 credit)"
    assert out["error"] is None and out["eta"] is None


async def test_probe_derives_carrier_and_reports_state_entry(api: respx.MockRouter,
                                                             make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    seed(client, "CMDU", "NAM1234567", "501234")
    api.get(CARRIERS).mock(return_value=respond(CARRIER_LIST))
    listing = api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
    api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    out = await client.probe(None, "nam1234567")

    assert out["carrier"] == "CMA" and out["booking"] == "NAM1234567" and out["policy"] == "latest"
    assert listing.calls.last.request.url.params["filters[carrier]"] == "eq:CMDU"
    assert out["state_entry"]["tracking_id"] == "501234"
    assert out["eta"]["eta"] == "2026-11-14T06:00:00"


async def test_probe_account_only_and_carrier_paging(api: respx.MockRouter,
                                                     make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    page1 = {"carriers": CARRIER_LIST["carriers"][:3], "meta": {"more": True}}
    page2 = {"carriers": CARRIER_LIST["carriers"][3:], "meta": {"more": False}}
    carriers = api.get(CARRIERS).mock(side_effect=[respond(page1), respond(page2)])

    out = await client.probe(None, None)

    assert out["account_ok"] is True and out["error"] is None
    assert [c["scac"] for c in out["carriers"]] == ["MSCU", "MAEU", "CMDU", "GRIU"]
    first, second = (call.request.url.params for call in carriers.calls)
    assert first["filters[status]"] == "eq:ACTIVE" and first["take"] == "100" and first["skip"] == "0"
    assert second["skip"] == "3"
    assert "booking" not in out


async def test_probe_bad_token_stops_after_account_check(api: respx.MockRouter,
                                                         make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    api.get(CARRIERS).mock(return_value=httpx.Response(401, json={"message": "UNAUTHORIZED"}))
    listing = api.get(LIST).mock(return_value=respond("shipsgo_list_found.json"))

    out = await client.probe(Carrier.MSC, MSC_BOOKING)

    assert out["account_ok"] is False
    assert "AGG_API_TOKEN" in out["error"]
    assert listing.call_count == 0
    assert TOKEN not in json.dumps(out)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 18. tracking state file
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_state_file_corrupt_is_tolerated(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "aggregator_tracking.json"
    path.write_text("{not json", encoding="utf-8")
    state = TrackingState(path)

    with caplog.at_level(logging.WARNING, logger="eta.carrier.aggregator"):
        assert state.get("shipsgo", "MSCU", MSC_BOOKING) is None
    assert any(r.levelno == logging.WARNING and "not valid JSON" in r.getMessage() for r in caplog.records)

    state.put("shipsgo", "MSCU", MSC_BOOKING, {"tracking_id": "501234", "created_by_us": False,
                                                "first_seen_at": NOW_ISO})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "entries": {"shipsgo:MSCU:EBKG12345678": {
        "tracking_id": "501234", "created_by_us": False, "first_seen_at": NOW_ISO}}}


async def test_corrupt_state_file_does_not_break_lookups(api: respx.MockRouter,
                                                         make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client()
    client.settings.state_file.write_text("[1, 2", encoding="utf-8")
    api.get(LIST).mock(return_value=respond("shipsgo_list_found.json"))
    api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    result = await client.get_eta(Carrier.MSC, MSC_BOOKING)

    assert result.eta == datetime(2026, 11, 12, 7, 30)
    assert state_entry(client, "MSCU", MSC_BOOKING)["tracking_id"] == "501234"  # type: ignore[index]


def test_state_file_atomic_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state" / "aggregator_tracking.json"
    state = TrackingState(path)
    state.put("shipsgo", "MSCU", MSC_BOOKING, {"tracking_id": "501234", "created_by_us": True,
                                                "first_seen_at": NOW_ISO})
    before = path.read_bytes()

    def failing_replace(src: Any, dst: Any) -> None:
        raise OSError("simulated crash before the rename")

    monkeypatch.setattr(aggregator.os, "replace", failing_replace)
    with pytest.raises(OSError, match="simulated crash"):
        state.put("shipsgo", "MSCU", NEW_BOOKING, {"tracking_id": "501300", "created_by_us": True,
                                                    "first_seen_at": NOW_ISO})
    monkeypatch.undo()

    assert path.read_bytes() == before                       # the old file is intact …
    assert [p.name for p in path.parent.iterdir()] == [path.name]   # … and no partial temp file is left
    assert json.loads(before)["entries"].keys() == {"shipsgo:MSCU:EBKG12345678"}

    def failing_fsync(fd: int) -> None:
        raise OSError("simulated disk error while writing")

    monkeypatch.setattr(aggregator.os, "fsync", failing_fsync)
    with pytest.raises(OSError, match="simulated disk error"):
        state.forget("shipsgo", "MSCU", MSC_BOOKING)
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_state_put_get_forget_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "aggregator_tracking.json"
    state = TrackingState(path)
    assert state.get("shipsgo", "MSCU", MSC_BOOKING) is None  # missing file = empty, no error
    assert not path.exists()
    entry = {"tracking_id": "501234", "created_by_us": True, "first_seen_at": NOW_ISO}
    state.put("shipsgo", "MSCU", MSC_BOOKING, entry)

    other = TrackingState(path)  # a new instance reads the file
    loaded = other.get("shipsgo", "MSCU", MSC_BOOKING)
    assert loaded == entry
    loaded["tracking_id"] = "changed"  # get() returns a copy
    assert other.get("shipsgo", "MSCU", MSC_BOOKING) == entry
    assert other.get("shipsgo", "CMDU", MSC_BOOKING) is None  # SCAC is part of the key

    # an entry written meanwhile by another process survives this instance's next write
    other.put("shipsgo", "CMDU", "NAM1234567", {"tracking_id": "7", "created_by_us": False, "first_seen_at": NOW_ISO})
    state.forget("shipsgo", "MSCU", MSC_BOOKING)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert list(data["entries"]) == ["shipsgo:CMDU:NAM1234567"]
    assert TrackingState.make_key("shipsgo", "MSCU", "X") == "shipsgo:MSCU:X"


async def test_state_write_failure_does_not_fail_the_lookup(
        api: respx.MockRouter, make_client: Callable[..., AggregatorClient], monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    client = make_client()
    api.get(LIST).mock(return_value=respond("shipsgo_list_found.json"))
    details = api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))

    def disk_full(self: TrackingState, payload: str) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(TrackingState, "_write_atomic", disk_full)
    with caplog.at_level(logging.WARNING, logger="eta.carrier.aggregator"):
        result = await client.get_eta(Carrier.MSC, MSC_BOOKING)
        again = await client.get_eta(Carrier.MSC, MSC_BOOKING)  # the id is kept in memory for this run

    assert result.eta == again.eta == datetime(2026, 11, 12, 7, 30)
    assert details.call_count == 2
    assert any("No space left on device" in r.getMessage() for r in caplog.records)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 19. prepare()
# ════════════════════════════════════════════════════════════════════════════════════════════════
class FakeBackend:
    """In-memory AggregatorBackend that records calls and detects overlapping ones."""

    name = "ShipsGo"
    key = "shipsgo"

    def __init__(self, *, existing: Mapping[tuple[str, str], str] | None = None,
                 failures: Mapping[str, Exception] | None = None) -> None:
        self.existing = dict(existing or {})
        self.failures = dict(failures or {})
        self.calls: list[tuple[str, ...]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.next_id = 700000
        self.last_create_was_new: bool | None = None
        self.closed = False

    async def _enter(self, *call: str) -> None:
        self.calls.append(call)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        await asyncio.sleep(0)
        self.in_flight -= 1

    async def find_existing(self, scac: str, booking: str) -> str | None:
        await self._enter("find", scac, booking)
        if booking in self.failures:
            raise self.failures[booking]
        return self.existing.get((scac, booking))

    async def create(self, scac: str, booking: str) -> str:
        await self._enter("create", scac, booking)
        self.next_id += 1
        self.last_create_was_new = True
        return str(self.next_id)

    async def fetch(self, tracking_id: str) -> AggregatorSnapshot | None:
        await self._enter("fetch", tracking_id)
        return snapshot_from_shipsgo(fixture_json("shipsgo_detail_sailing.json"), tracking_id=tracking_id)

    async def list_carriers(self) -> list[dict[str, Any]]:
        return []

    async def aclose(self) -> None:
        self.closed = True


async def test_prepare_counts_and_sequential(make_client: Callable[..., AggregatorClient],
                                             caplog: pytest.LogCaptureFixture) -> None:
    backend = FakeBackend(existing={("MAEU", "212345678"): "800001"},
                          failures={"EBKG99999999": CarrierLookupError("ShipsGo is down")})
    client = make_client(backend=backend)
    seed(client, "MSCU", MSC_BOOKING, "501234")
    items = [
        (Carrier.MSC, MSC_BOOKING),         # known (state file): no call
        (Carrier.MSC, MSC_BOOKING),         # duplicate: ignored
        (Carrier.MAERSK, "212345678"),      # found by the provider list
        (Carrier.CMA, "NAM1234567"),        # created
        (Carrier.MSC, "EBKG99999999"),      # provider error: swallowed
    ]

    with caplog.at_level(logging.INFO, logger="eta.carrier.aggregator"):
        counts = await client.prepare(items)

    assert counts == {"known": 1, "found": 1, "created": 1, "failed": 1}
    assert backend.calls == [("find", "MAEU", "212345678"), ("find", "CMDU", "NAM1234567"),
                             ("create", "CMDU", "NAM1234567"), ("find", "MSCU", "EBKG99999999")]
    assert backend.max_in_flight == 1  # strictly sequential
    assert client.state.get("shipsgo", "CMDU", "NAM1234567")["created_by_us"] is True  # type: ignore[index]
    assert client.state.get("shipsgo", "MAEU", "212345678")["created_by_us"] is False  # type: ignore[index]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("EBKG99999999" in w and "ShipsGo is down" in w for w in warnings)
    assert any("1 known, 1 found, 1 created, 1 failed" in r.getMessage() for r in caplog.records
               if r.levelno == logging.INFO)

    # a later get_eta of a pre-registered booking needs no further list/create call
    backend.calls.clear()
    result = await client.view(Carrier.CMA).get_eta("NAM1234567")
    assert result.eta == datetime(2026, 11, 14, 6, 0)
    assert backend.calls == [("fetch", "700001")]


async def test_prepare_booking_errors_do_not_stop_the_warm_up(make_client: Callable[..., AggregatorClient]) -> None:
    backend = FakeBackend()
    client = make_client(backend=backend, AGG_ALLOW_CREATE="false")
    bookings = [f"EBKG3000000{i}" for i in range(5)]

    counts = await client.prepare([(Carrier.MSC, b) for b in bookings])

    assert counts == {"known": 0, "found": 0, "created": 0, "failed": 5}
    assert [c[0] for c in backend.calls] == ["find"] * 5  # every booking was tried, none created


async def test_prepare_stops_after_consecutive_provider_failures(
        make_client: Callable[..., AggregatorClient]) -> None:
    bookings = [f"EBKG4000000{i}" for i in range(6)]
    backend = FakeBackend(failures={b: CarrierLookupError("ShipsGo rejected the API token (HTTP 401)")
                                    for b in bookings})
    client = make_client(backend=backend)

    counts = await client.prepare([(Carrier.MSC, b) for b in bookings])

    assert counts["failed"] == 6
    assert len(backend.calls) == aggregator.PREPARE_SYSTEMIC_LIMIT


async def test_prepare_never_raises_on_bad_items(make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(backend=FakeBackend())
    counts = await client.prepare([("not-a-carrier", "X"), (Carrier.MSC, "EBKG 1")])  # type: ignore[list-item]
    assert counts == {"known": 0, "found": 0, "created": 0, "failed": 2}


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 20. views
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_view_aclose_noop(agg_settings: Callable[..., AggregatorSettings], fixed_clock: FixedClock) -> None:
    client = AggregatorClient(agg_settings(), clock=fixed_clock)
    backend = client.backend
    assert isinstance(backend, ShipsGoBackend)
    view = client.view(Carrier.MAERSK)
    assert client.view(Carrier.MAERSK) is view  # cached per carrier
    assert isinstance(view, AggregatorCarrierView) and view.client is client

    assert await view.aclose() is None
    assert not backend.http.is_closed  # closing a view does not close the shared client

    await client.aclose()
    assert backend.http.is_closed
    await client.aclose()  # idempotent


async def test_view_forwards_its_carrier(make_client: Callable[..., AggregatorClient]) -> None:
    backend = FakeBackend(existing={("MAEU", "212345678"): "800001"})
    client = make_client(backend=backend)
    result = await client.view(Carrier.MAERSK).get_eta("212345678")
    assert result.carrier is Carrier.MAERSK
    assert result.details["policy"] == "first"
    assert backend.calls[0] == ("find", "MAEU", "212345678")


async def test_injected_http_client_is_not_closed(agg_settings: Callable[..., AggregatorSettings],
                                                  fixed_clock: FixedClock, api: respx.MockRouter) -> None:
    api.get(detail_path(501234)).mock(return_value=respond("shipsgo_detail_sailing.json"))
    async with httpx.AsyncClient() as http:  # no base_url, no token header: the backend adds both
        backend = ShipsGoBackend(agg_settings(), http=http)
        client = AggregatorClient(agg_settings(), backend=backend, clock=fixed_clock)
        seed(client, "MSCU", MSC_BOOKING, "501234")
        result = await client.get_eta(Carrier.MSC, MSC_BOOKING)
        assert result.eta == datetime(2026, 11, 12, 7, 30)
        assert api.calls.last.request.headers["X-Shipsgo-User-Token"] == TOKEN
        await client.aclose()
        assert not http.is_closed


# ════════════════════════════════════════════════════════════════════════════════════════════════
# payload mapping (09 §B.7.4) and provider list matching
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_snapshot_mapping_of_the_sailing_fixture() -> None:
    snap = snapshot_from_shipsgo(fixture_json("shipsgo_detail_sailing.json"))
    assert (snap.tracking_id, snap.status, snap.ready, snap.failure) == ("501234", "SAILING", True, None)
    assert snap.pod_unlocode == "USNYC"
    assert snap.containers == ("MEDU1234567", "MEDU7654321")
    assert snap.route_pod_arrival == dcsa.DcsaArrival(datetime(2026, 11, 12, 13, 0), "EST", "USNYC", None, None,
                                                      ROUTE_POD_BASIS)  # predicted date ignored
    assert len(snap.events) == 12

    emsh, gtin, load = snap.events[0], snap.events[1], snap.events[2]
    assert (emsh.event_type, emsh.type_code, emsh.empty_indicator) == ("EQUIPMENT", "GTOT", "EMPTY")
    assert emsh.event_datetime == datetime(2026, 10, 15, 9, 0)  # 07:00Z → Rome (CEST)
    assert emsh.mode_of_transport is None and emsh.vessel_name is None
    assert (gtin.type_code, gtin.empty_indicator, gtin.equipment_reference) == ("GTIN", "LADEN", "MEDU1234567")
    assert (load.mode_of_transport, load.vessel_imo, load.vessel_name, load.voyage) == (
        "VESSEL", "9000011", "MSC FEEDER", "FA642W")
    arrival = snap.events[8]
    assert (arrival.event_type, arrival.type_code, arrival.classifier) == ("TRANSPORT", "ARRI", "EST")
    assert arrival.equipment_reference is None and arrival.eq_refs == ("MEDU1234567",)
    assert arrival.event_datetime == datetime(2026, 11, 12, 7, 30)  # 12:30Z → New York (EST)
    assert arrival.created_datetime is None and arrival.raw["event"] == "ARRV"
    assert [e.index for e in snap.events] == list(range(12))


def test_snapshot_skips_bad_movements_and_unassigned_containers() -> None:
    body = {"shipment": {"id": 5, "status": "loaded", "route": None, "containers": [
        {"number": "NOT_ASSIGNED", "movements": [
            {"event": "ARRV", "status": "EST", "location": {"code": "usnyc", "timezone": "America/New_York"},
             "timestamp": "2026-11-12T12:30:00Z", "vessel": None, "voyage": None}]},
        {"number": "medu1111111", "movements": [
            {"event": "EMRT", "status": "ACT", "location": {"code": "USNYC"}, "timestamp": "2026-11-20T10:00:00Z"},
            {"event": "XXXX", "status": "ACT", "location": {"code": "USNYC"}, "timestamp": "2026-11-20T10:00:00Z"},
            {"event": "DISC", "status": "PLN", "location": {"code": "USNYC"}, "timestamp": "2026-11-20T10:00:00Z"},
            {"event": "DISC", "status": "EST", "location": {"code": "USNYC"}, "timestamp": "not a date"},
            {"event": "DISC", "status": "EST", "location": {"code": "USNYC"}, "timestamp": "0001-01-01T00:00:00Z"},
            "not a movement",
        ]},
    ]}}
    snap = snapshot_from_shipsgo(body)
    assert snap.status == "LOADED" and snap.ready is True
    assert snap.containers == ("MEDU1111111",)  # NOT_ASSIGNED skipped, numbers upper-cased
    assert snap.pod_unlocode is None and snap.route_pod_arrival is None
    assert [(e.type_code, e.empty_indicator) for e in snap.events] == [("ARRI", None), ("GTIN", "EMPTY")]
    unassigned = snap.events[0]
    assert unassigned.unlocode == "USNYC" and unassigned.eq_refs == ()  # a general (vessel-level) arrival
    assert snap.events[1].event_datetime == datetime(2026, 11, 20, 10, 0)  # no tz hint: the given wall time


@pytest.mark.parametrize(("status", "containers", "route", "ready", "failure"), [
    ("NEW", [], None, False, None),
    ("inprogress", [], None, False, None),
    ("UNTRACKED", [], None, False, "UNTRACKED"),
    ("BOOKED", [], None, True, None),
    ("ARRIVED", [], None, True, None),
    ("SOMETHING_NEW", [], None, False, None),
    ("SOMETHING_NEW", [{"number": "X"}], None, True, None),
    (None, [], {"port_of_discharge": None}, True, None),
])
def test_snapshot_readiness_rules(status: str | None, containers: list[Any], route: Any, ready: bool,
                                  failure: str | None) -> None:
    snap = snapshot_from_shipsgo({"shipment": {"id": 1, "status": status, "containers": containers, "route": route}})
    assert (snap.ready, snap.failure) == (ready, failure)


def test_discharged_route_date_is_actual_and_converted_to_port_time() -> None:
    snap = snapshot_from_shipsgo(fixture_json("shipsgo_detail_discharged.json"))
    assert snap.route_pod_arrival is not None
    assert snap.route_pod_arrival.eta == datetime(2026, 11, 2, 21, 40)  # 02:40Z → previous day in New York
    assert snap.route_pod_arrival.classifier == "ACT"


async def test_find_existing_exact_match_highest_id(api: respx.MockRouter,
                                                    agg_settings: Callable[..., AggregatorSettings]) -> None:
    listing = {"message": "SUCCESS", "meta": {"more": False, "total": 4}, "shipments": [
        {"id": 501234, "booking_number": "EBKG12345678", "carrier": {"scac": "MSCU", "name": "MSC"}},
        {"id": 501999, "booking_number": "ebkg12345678", "carrier": {"scac": "mscu", "name": "MSC"}},
        {"id": 600000, "booking_number": "EBKG12345678X", "carrier": {"scac": "MSCU", "name": "MSC"}},
        {"id": 700000, "booking_number": "EBKG12345678", "carrier": {"scac": "MAEU", "name": "Maersk"}},
    ]}
    api.get(LIST).mock(return_value=respond(listing))
    backend = ShipsGoBackend(agg_settings())
    try:
        assert await backend.find_existing("MSCU", MSC_BOOKING) == "501999"
        api.get(LIST).mock(return_value=respond("shipsgo_list_empty.json"))
        assert await backend.find_existing("MSCU", MSC_BOOKING) is None
    finally:
        await backend.aclose()


async def test_backend_create_reports_new_or_existing(api: respx.MockRouter,
                                                      agg_settings: Callable[..., AggregatorSettings]) -> None:
    api.post(LIST).mock(side_effect=[respond("shipsgo_create_200.json"), respond("shipsgo_create_409.json", 409)])
    backend = ShipsGoBackend(agg_settings())
    try:
        assert await backend.create("MSCU", NEW_BOOKING) == "501300"
        assert backend.last_create_was_new is True
        assert await backend.create("MSCU", MSC_BOOKING) == "501234"
        assert backend.last_create_was_new is False
    finally:
        await backend.aclose()


async def test_http_concurrency_is_bounded(api: respx.MockRouter, make_client: Callable[..., AggregatorClient]) -> None:
    client = make_client(AGG_MAX_CONCURRENCY=2)
    in_flight = 0
    max_in_flight = 0

    async def slow_detail(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.01)
        finally:
            in_flight -= 1
        return respond("shipsgo_detail_sailing.json")

    bookings = [f"EBKG5000000{i}" for i in range(5)]
    for i, booking in enumerate(bookings):
        seed(client, "MSCU", booking, str(900 + i))
        api.get(detail_path(900 + i)).mock(side_effect=slow_detail)

    results = await asyncio.gather(*(client.get_eta(Carrier.MSC, b) for b in bookings))

    assert len(results) == 5
    assert max_in_flight == 2


def test_snapshot_dataclass_shape() -> None:
    snap = snapshot_from_shipsgo(fixture_json("shipsgo_detail_inprogress.json"))
    assert snap == AggregatorSnapshot(tracking_id="501300", status="INPROGRESS", ready=False, failure=None,
                                      pod_unlocode=None, events=(), route_pod_arrival=None, containers=(),
                                      raw=snap.raw)
    assert snap.raw == fixture_json("shipsgo_detail_inprogress.json")  # the body as received
    with pytest.raises(ValueError, match="no 'shipment' object"):
        snapshot_from_shipsgo([])
