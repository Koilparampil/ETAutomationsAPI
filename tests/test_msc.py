"""carriers/msc.py — 06-carrier-msc §9.3 tests 1–29, plus the rescue paths (06 §7.5, 09 §A.6), the edge-block and
fatal latches, probe() and the helpers the CLI wires (06 §10, 11).

Every test uses FixedClock(datetime(2026, 11, 5, 9, 30)) unless it says otherwise (06 §9). HTTP goes through respx
mocks only (12-testing §1); fixtures are tests/fixtures/msc/* (06 §9.2).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import locale
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eta_automations.carriers import msc
from eta_automations.carriers.base import CarrierClient
from eta_automations.carriers.msc import (
    DISCHARGE_EVENT,
    EDGE_BLOCK_MESSAGE,
    ETA_EVENT,
    GRAPHQL_QUERY,
    GRAPHQL_QUERY_SHA256,
    NO_TOKEN_MESSAGE,
    SANDBOX_BASE_URL,
    SANDBOX_REFUSAL,
    TOKEN_REJECTED_MESSAGE,
    TRANSSHIPMENT_EVENT,
    MscBridgeAuth,
    MscClient,
    eta_from_api,
    eta_from_graphql,
    graphql_body,
    parse_msc_date,
    parse_wall_datetime,
    token_needs_refresh,
    transshipment_eta,
)
from eta_automations.clock import FixedClock
from eta_automations.config import MscSettings
from eta_automations.errors import CarrierLookupError, ConfigError, CustomsHoldError, NoEtaError
from eta_automations.models import Carrier
from eta_automations.token_store import TokenStore
from fakes import UnwritableTokenStore

NOW = datetime(2026, 11, 5, 9, 30)
BOOKING = "EBKG12345678"
GQL_URL = "https://services.mymsc.com/tracking/graphql"
API_BASE = "https://api.msc.example.test/dpo/trackandtrace/v2.2"
API_EVENTS = f"{API_BASE}/events"
SANDBOX_EVENTS = f"{SANDBOX_BASE_URL}/events"
API_KEY = "test-msc-subscription-key-7f3a9c"
TOKEN = "test-msc-bridge-token-5d1e8b"
EPOCH = 1_793_000_000.0      # injected epoch "now" for token expiry checks

BRIDGE = MscSettings(mode="mymsc", user_email="ops@example.com")
API = MscSettings(mode="api", api_base_url=API_BASE, api_key=API_KEY)

# Akamai's standard denial page, entity-encoded the way the real page is served (06 §6.5).
AKAMAI_DENIED = (
    "<HTML><HEAD>\n<TITLE>Access Denied</TITLE>\n</HEAD><BODY>\n<H1>Access Denied</H1>\n \n"
    "You don't have permission to access \"http&#58;&#47;&#47;services&#46;mymsc&#46;com&#47;tracking&#47;graphql\""
    " on this server.<P>\nReference&#32;&#35;18&#46;5b7c1002&#46;1793870000&#46;1a2b3c4d\n"
    "<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;5b7c1002&#46;1793870000&#46;1a2b3c4d</P>\n"
    "</BODY>\n</HTML>\n"
)


# ── 06 §9.1 test helpers (verbatim) ──────────────────────────────────────────────────────────────────
def gql_event(name: str, date: str, country: str = "US", location: str = "NEW YORK") -> dict:
    return {"eventName": name, "eventDate": date, "eventDateDateTime": None, "eventTime": "",
            "locationCountry": country, "locationName": location, "locationUnCode": None,
            "vesselName": "MSC EXAMPLE", "voyageDesc": "AB642A", "isEventPassed": False, "portCalls": []}


def gql_payload(*containers: dict, errors: list | None = None) -> dict:
    body = {"data": {"trackingByBookingNumber": [{"allBillNumber": "MEDUAB123456", "containers": list(containers)}]}}
    if errors is not None:
        body["errors"] = errors
    return body


def gql_container(number: str, events: list[dict], reached: bool | None = False, final_eta: str | None = None) -> dict:
    return {"containerNumber": number, "isDestinationReached": reached, "finalEta": final_eta, "events": events}


def api_event(etype: str, code: str, classifier: str, description: str, when: str, unloc: str,
              equipment: str | None = None) -> dict:
    key = {"TRANSPORT": "transportEventTypeCode", "EQUIPMENT": "equipmentEventTypeCode"}[etype]
    ev = {"eventType": etype, key: code, "eventClassifierCode": classifier, "description": description,
          "eventDateTime": when, "eventCreatedDateTime": when, "eventId": "1",
          "transportCall": {"transportCallID": "X", "unLocationCode": unloc, "facilityTypeCode": "POTE",
                            "modeOfTransport": "VESSEL"},
          "documentReferences": [{"documentReferenceType": "BKG", "documentReferenceValue": "EBKG12345678"}],
          "references": []}
    if equipment:
        ev["equipmentReference"] = equipment
        ev["eventLocation"] = {"unLocationCode": unloc, "locationName": unloc}
    return ev


# ── stage helpers ────────────────────────────────────────────────────────────────────────────────────
def make_jwt(claims: dict[str, Any]) -> str:
    """Unsigned test JWT (`alg: none`, empty signature segment)."""
    def b64(obj: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()
    return f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64(claims)}."


def seed_token(store: TokenStore, token: str = TOKEN, *, expires_at: float = EPOCH + 3600) -> None:
    store.put("msc", {"access_token": token, "expires_at": expires_at, "obtained_at": EPOCH,
                      "source": "manual", "mode": "mymsc"})


def warnings_of(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


@pytest.fixture
def fx(load_fixture: Callable[[str], Any]) -> Callable[[str], Any]:
    """fx("graphql_in_transit") → tests/fixtures/msc/graphql_in_transit.json (06 §9.2)."""
    return lambda name: load_fixture(f"msc/{name}.json")


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as router:
        yield router


@pytest.fixture
async def make_client(token_store: TokenStore) -> AsyncIterator[Callable[..., MscClient]]:
    created: list[MscClient] = []

    def _make(settings: MscSettings = BRIDGE, *, now: datetime = NOW, allow_sandbox: bool | None = None,
              epoch: Callable[[], float] = lambda: EPOCH) -> MscClient:
        client = MscClient(settings, token_store, clock=FixedClock(now), allow_sandbox=allow_sandbox, epoch=epoch)
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


# ═════════════════════════════ Pure logic — mymsc (eta_from_graphql) ═════════════════════════════
def test_01_graphql_in_transit_fixture(fx: Callable[[str], Any]) -> None:
    result = eta_from_graphql(fx("graphql_in_transit"), booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 12)
    assert result.source == "MSC event 'Estimated Time of Arrival'"
    assert result.is_actual is False
    assert result.containers == ("MSCU1111111",)
    assert (result.carrier, result.booking, result.from_transshipment_rule) == (Carrier.MSC, BOOKING, False)
    assert result.details == {"mode": "mymsc", "event_name": ETA_EVENT, "raw_date": "12 Nov 2026",
                              "container": "MSCU1111111"}


def test_02_discharge_listed_first_wins() -> None:
    body = gql_payload(gql_container("MSCU1111111", [gql_event(DISCHARGE_EVENT, "03 Nov 2026", "GH"),
                                                     gql_event(ETA_EVENT, "02 Nov 2026", "GH")]))
    result = eta_from_graphql(body, booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 3)
    assert result.is_actual is True
    assert result.source == "MSC event 'Import Discharged from Vessel'"


def test_03_eta_listed_first_wins_over_later_discharge() -> None:
    body = gql_payload(gql_container("MSCU1111111", [gql_event(ETA_EVENT, "02 Nov 2026", "GH"),
                                                     gql_event(DISCHARGE_EVENT, "03 Nov 2026", "GH")]))
    result = eta_from_graphql(body, booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 2)
    assert result.is_actual is False


@pytest.mark.parametrize(("loaded", "expected"), [
    ("20 Oct 2026", datetime(2026, 11, 12, 9, 30)),   # branch 1: keeps now's time of day
    ("02 Nov 2026", datetime(2026, 11, 9, 0, 0)),     # branch 2
    ("07 Nov 2026", datetime(2026, 11, 14, 0, 0)),    # branch 3
])
def test_04_transshipment_rule(loaded: str, expected: datetime) -> None:
    body = gql_payload(gql_container("MSCU1111111", [gql_event(TRANSSHIPMENT_EVENT, loaded, "MA", "TANGER MED"),
                                                     gql_event("Export Loaded on Vessel", "18 Oct 2026")]))
    result = eta_from_graphql(body, booking=BOOKING, now=NOW)
    assert result.eta == expected
    assert result.from_transshipment_rule is True
    assert result.is_actual is False
    assert result.source == f"MSC 'Full Transshipment Loaded' on {loaded} → ETA estimate (+7 days)"
    assert result.details["raw_date"] == loaded


@pytest.mark.parametrize(("loaded", "expected"), [
    ("30 Oct 2026", datetime(2026, 11, 12)),   # == now − 6 d → branch 1
    ("31 Oct 2026", datetime(2026, 11, 7)),    # branch 2
    ("05 Nov 2026", datetime(2026, 11, 12)),   # == now → branch 3
])
def test_05_transshipment_boundaries_at_midnight(loaded: str, expected: datetime) -> None:
    now = FixedClock(datetime(2026, 11, 5)).now()
    body = gql_payload(gql_container("MSCU1111111", [gql_event(TRANSSHIPMENT_EVENT, loaded, "MA")]))
    assert eta_from_graphql(body, booking=BOOKING, now=now).eta == expected
    assert transshipment_eta(parse_msc_date(loaded), now) == expected


def test_06_customs_hold_rule() -> None:
    us_latest = [gql_event("Export received at CY", "14 Oct 2026", "US"), gql_event(ETA_EVENT, "12 Nov 2026", "GH")]
    with pytest.raises(CustomsHoldError) as exc:
        eta_from_graphql(gql_payload(gql_container("MSCU1111111", us_latest, reached=True)), booking=BOOKING, now=NOW)
    assert str(exc.value) == ("Possible customs hold for MSC booking EBKG12345678: destination reached but latest "
                              "event is in the US ('Export received at CY' at 'NEW YORK')")
    # country compared after strip/upper
    lower_us = [gql_event("Export received at CY", "14 Oct 2026", " us "), gql_event(ETA_EVENT, "12 Nov 2026", "GH")]
    with pytest.raises(CustomsHoldError):
        eta_from_graphql(gql_payload(gql_container("MSCU1111111", lower_us, reached=True)), booking=BOOKING, now=NOW)
    # reached, latest event in Ghana → normal ETA
    gh_latest = [gql_event(DISCHARGE_EVENT, "03 Nov 2026", "GH", "TEMA")]
    result = eta_from_graphql(gql_payload(gql_container("MSCU1111111", gh_latest, reached=True)),
                              booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 3)
    # MSC-D5: isDestinationReached None counts as False
    result = eta_from_graphql(gql_payload(gql_container("MSCU1111111", us_latest, reached=None)),
                              booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 12)
    # MSC-D5: reached with no events → no crash, NoEtaError
    with pytest.raises(NoEtaError):
        eta_from_graphql(gql_payload(gql_container("MSCU1111111", [], reached=True)), booking=BOOKING, now=NOW)


def test_07_missing_data_and_graphql_errors(fx: Callable[[str], Any], caplog: pytest.LogCaptureFixture) -> None:
    no_match = gql_payload(gql_container("MSCU1111111", [gql_event("Export received at CY", "14 Oct 2026")]))
    with pytest.raises(NoEtaError) as exc:
        eta_from_graphql(no_match, booking=BOOKING, now=NOW)
    assert str(exc.value) == ("No 'Estimated Time of Arrival', 'Import Discharged from Vessel' or 'Full Transshipment "
                              "Loaded' event for MSC booking EBKG12345678. Can't determine ETA.")
    with pytest.raises(NoEtaError, match=r"^MSC lists no containers for booking EBKG12345678 yet\. Can't determine"):
        eta_from_graphql(gql_payload(), booking=BOOKING, now=NOW)
    null_events = gql_payload({"containerNumber": "MSCU1111111", "isDestinationReached": False, "finalEta": None,
                               "events": None})
    with pytest.raises(NoEtaError, match=r"^MSC returned no events for container MSCU1111111 of booking EBKG12345678"):
        eta_from_graphql(null_events, booking=BOOKING, now=NOW)
    with pytest.raises(CarrierLookupError, match=r"^MSC returned no tracking result for booking EBKG12345678"):
        eta_from_graphql({"data": {"trackingByBookingNumber": []}}, booking=BOOKING, now=NOW)
    with pytest.raises(CarrierLookupError, match=r"^MSC returned no tracking result"):
        eta_from_graphql({"data": None}, booking=BOOKING, now=NOW)
    with pytest.raises(CarrierLookupError) as gql_err:
        eta_from_graphql(fx("graphql_errors"), booking=BOOKING, now=NOW)
    assert str(gql_err.value) == "MSC GraphQL error for EBKG12345678: Booking number not found"
    with pytest.raises(CarrierLookupError, match="non-object response"):
        eta_from_graphql(["not", "an", "object"], booking=BOOKING, now=NOW)
    # errors alongside results → result returned plus a WARNING
    caplog.set_level(logging.WARNING, logger="eta.carrier.msc")
    body = gql_payload(gql_container("MSCU1111111", [gql_event(ETA_EVENT, "12 Nov 2026", "GH")]),
                       errors=[{"message": "customsRelease: upstream timeout"}])
    assert eta_from_graphql(body, booking=BOOKING, now=NOW).eta == datetime(2026, 11, 12)
    assert any("customsRelease: upstream timeout" in m for m in warnings_of(caplog))


def test_08_unreadable_event_date_is_no_eta() -> None:
    body = gql_payload(gql_container("MSCU1111111", [gql_event(ETA_EVENT, "2026-11-12", "GH")]))
    with pytest.raises(NoEtaError) as exc:
        eta_from_graphql(body, booking=BOOKING, now=NOW)
    assert "2026-11-12" in str(exc.value)
    assert str(exc.value).startswith("MSC event 'Estimated Time of Arrival' has an unreadable date")
    bad_transshipment = gql_payload(gql_container("MSCU1111111", [gql_event(TRANSSHIPMENT_EVENT, "soon", "MA")]))
    with pytest.raises(NoEtaError, match="'Full Transshipment Loaded' has an unreadable date"):
        eta_from_graphql(bad_transshipment, booking=BOOKING, now=NOW)


def test_09_multi_container_warning(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.msc")
    c0 = gql_container("MSCU1111111", [gql_event(ETA_EVENT, "12 Nov 2026", "GH")])
    c1 = gql_container("MSCU2222222", [gql_event(ETA_EVENT, "19 Nov 2026", "GH")])
    result = eta_from_graphql(gql_payload(c0, c1), booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 12)
    assert result.containers == ("MSCU1111111",)
    assert result.details["other_container_etas"] == {"MSCU2222222": "2026-11-19"}
    assert any("containers have different ETAs" in m and "using first container MSCU1111111 (parity)" in m
               for m in warnings_of(caplog))
    # a container without a usable event shows "n/a" when another one differs
    c2 = gql_container("MSCU3333333", [])
    mixed = eta_from_graphql(gql_payload(c0, c1, c2), booking=BOOKING, now=NOW)
    assert mixed.details["other_container_etas"] == {"MSCU2222222": "2026-11-19", "MSCU3333333": "n/a"}
    # same date everywhere (or no date) → no warning, no extra details; a broken container never fails the lookup
    caplog.clear()
    same = gql_container("MSCU4444444", [gql_event(ETA_EVENT, "12 Nov 2026", "GH")])
    broken = gql_container("MSCU5555555", [gql_event(ETA_EVENT, "next week", "GH")])
    quiet = eta_from_graphql(gql_payload(c0, same, c2, broken, None), booking=BOOKING, now=NOW)
    assert "other_container_etas" not in quiet.details
    assert warnings_of(caplog) == []


@pytest.mark.parametrize(("text", "expected"), [
    ("5 Nov 2026", datetime(2026, 11, 5)),
    (" 12 nov 2026 ", datetime(2026, 11, 12)),
    ("12 NOV 2026", datetime(2026, 11, 12)),
])
def test_10_parse_msc_date_accepts(text: str, expected: datetime) -> None:
    assert parse_msc_date(text) == expected


@pytest.mark.parametrize("text", ["12 Nov 26", "2026-11-12", "", "12 Foo 2026", "31 Feb 2026", "12 November 2026"])
def test_10_parse_msc_date_rejects(text: str) -> None:
    with pytest.raises(ValueError, match="unrecognised MSC date"):
        parse_msc_date(text)


def test_10_parse_msc_date_is_locale_independent() -> None:
    saved = locale.setlocale(locale.LC_TIME)
    try:
        try:
            locale.setlocale(locale.LC_TIME, "de_DE.UTF-8")
        except locale.Error:
            pytest.skip("no German locale installed")
        assert parse_msc_date("12 Oct 2026") == datetime(2026, 10, 12)   # German %b would be "Okt"
        assert parse_msc_date("05 Dec 2026") == datetime(2026, 12, 5)    # German %b would be "Dez"
        body = gql_payload(gql_container("MSCU1111111", [gql_event(TRANSSHIPMENT_EVENT, "20 Oct 2026", "MA")]))
        assert "on 20 Oct 2026 →" in eta_from_graphql(body, booking=BOOKING, now=NOW).source
    finally:
        locale.setlocale(locale.LC_TIME, saved)


def test_11_graphql_query_verbatim_and_body() -> None:
    assert hashlib.sha256(GRAPHQL_QUERY.encode()).hexdigest() == GRAPHQL_QUERY_SHA256
    assert GRAPHQL_QUERY.startswith(
        "\n    query trackingByBookingNumber($input: TrackingByBookingNumberGraphQlRequestInput!) {\n")
    body = graphql_body("EBKG12345678")
    assert list(body) == ["query", "variables", "operationName"]
    assert body["query"] == GRAPHQL_QUERY
    assert body["operationName"] == "trackingByBookingNumber"
    assert list(body["variables"]) == ["input"]
    data = body["variables"]["input"]
    assert list(data) == ["retrieveFreightRelease", "retrieveTracking", "agency", "retrieveCustomsRelease",
                          "retrieveLastFreeDate", "bookingNumber"]
    assert data == {
        "retrieveFreightRelease": False,
        "retrieveTracking": True,
        "agency": {"id": -1, "isCustomsReleaseActive": False, "isFreightReleaseActive": False,
                   "isLastFreeDateActive": False, "isTrackingActive": False, "name": ""},
        "retrieveCustomsRelease": False,
        "retrieveLastFreeDate": False,
        "bookingNumber": "EBKG12345678",
    }


# ═════════════════════════════ Pure logic — api (eta_from_api) ═════════════════════════════
def test_12_api_in_transit_bare_wrapped_and_reversed(fx: Callable[[str], Any]) -> None:
    payload = fx("api_in_transit")
    result = eta_from_api(payload, booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 12)
    assert result.details["event_datetime"] == "2026-11-12T06:00:00"
    assert result.containers == ("MSCU1111111",)
    assert result.is_actual is False
    assert result.source == "MSC API event 'Estimated Time of Arrival'"
    assert result.details["mode"] == "api" and result.details["container"] == "MSCU1111111"
    assert eta_from_api({"events": payload}, booking=BOOKING, now=NOW) == result
    assert eta_from_api(list(reversed(payload)), booking=BOOKING, now=NOW) == result


def test_13_api_discharged() -> None:
    payload = [
        api_event("EQUIPMENT", "DISC", "ACT", DISCHARGE_EVENT, "2026-11-03T14:20:00.0000000+00:00", "GHTEM",
                  "MSCU1111111"),
        api_event("TRANSPORT", "ARRI", "ACT", "Vessel Arrival", "2026-11-02T22:00:00Z", "GHTEM"),
    ]
    result = eta_from_api(payload, booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 3)
    assert result.is_actual is True
    assert result.details["event_datetime"] == "2026-11-03T14:20:00"


def _customs_payload() -> list[dict]:
    return [
        api_event("TRANSPORT", "ARRI", "ACT", "Vessel Arrival", "2026-11-02T22:00:00Z", "GHTEM"),
        api_event("TRANSPORT", "DEPA", "ACT", "Vessel Departure", "2026-10-15T10:00:00Z", "USSAV"),
        api_event("EQUIPMENT", "GTIN", "ACT", "Export received at CY", "2026-10-10T08:00:00Z", "USSAV", "MSCU2222222"),
    ]


def test_14_api_customs_hold() -> None:
    with pytest.raises(CustomsHoldError) as exc:
        eta_from_api(_customs_payload(), booking=BOOKING, now=NOW)
    assert str(exc.value) == ("Possible customs hold for MSC booking EBKG12345678: vessel arrived at GHTEM but "
                              "container MSCU2222222's latest event is in the US ('Export received at CY')")
    with pytest.raises(NoEtaError, match="Can't determine ETA"):
        eta_from_api(_customs_payload(), booking=BOOKING, now=NOW, customs_rule=False)
    # the newest ARRI is only an estimate → the destination is not reached → no hold
    estimated = _customs_payload() + [
        api_event("TRANSPORT", "ARRI", "EST", ETA_EVENT, "2026-11-20T06:00:00Z", "GHTEM")]
    assert eta_from_api(estimated, booking=BOOKING, now=NOW).eta == datetime(2026, 11, 20)


def test_15_api_transshipment() -> None:
    payload = [api_event("EQUIPMENT", "LOAD", "ACT", TRANSSHIPMENT_EVENT, "2026-11-02T08:00:00Z", "MAPTM",
                         "MSCU1111111")]
    result = eta_from_api(payload, booking=BOOKING, now=NOW)
    assert result.eta == datetime(2026, 11, 9, 0, 0)
    assert result.from_transshipment_rule is True
    assert result.is_actual is False   # a +7-day estimate, even though the LOAD itself is ACT
    assert result.source == "MSC 'Full Transshipment Loaded' on 02 Nov 2026 → ETA estimate (+7 days)"
    assert result.details["event_datetime"] == "2026-11-02T08:00:00"


@pytest.mark.parametrize("payload", [None, "events", 42, {"events": "nope"}, {"data": []}])
def test_16_api_unexpected_shapes(payload: Any) -> None:
    with pytest.raises(CarrierLookupError, match="^Unexpected MSC API response shape for EBKG12345678$"):
        eta_from_api(payload, booking=BOOKING, now=NOW)


@pytest.mark.parametrize("payload", [[], {"events": []}])
def test_16_api_no_events(payload: Any) -> None:
    with pytest.raises(CarrierLookupError, match="^MSC API returned no events for booking EBKG12345678 — booking "):
        eta_from_api(payload, booking=BOOKING, now=NOW)


def test_api_events_without_readable_datetime() -> None:
    unreadable = [api_event("TRANSPORT", "ARRI", "EST", ETA_EVENT, "soon", "GHTEM"), "not-an-event"]
    with pytest.raises(NoEtaError, match="^MSC API returned 2 event\\(s\\) for booking EBKG12345678 but none has a "
                                         "readable eventDateTime"):
        eta_from_api(unreadable, booking=BOOKING, now=NOW, rescue=True)
    # unreadable events are skipped, the readable ones still decide
    mixed = unreadable + [api_event("TRANSPORT", "ARRI", "EST", ETA_EVENT, "2026-11-12T06:00:00Z", "GHTEM")]
    assert eta_from_api(mixed, booking=BOOKING, now=NOW).eta == datetime(2026, 11, 12)


def test_api_event_keys_are_case_tolerant() -> None:
    """eventType inferred from the type-code key; UNLocationCode / unLocationCode / location fallbacks (06 §7.2)."""
    payload = [
        {"transportEventTypeCode": "ARRI", "eventClassifierCode": "act", "description": " Vessel Arrival ",
         "eventDateTime": "2026-11-02T22:00:00Z", "transportCall": {"UNLocationCode": "ghtem"}},
        {"eventType": "equipment", "equipmentEventTypeCode": "gtin", "eventClassifierCode": "ACT",
         "description": "Export received at CY", "eventDateTime": "2026-10-10T08:00:00Z",
         "equipmentReference": " mscu2222222 ", "transportCall": {"location": {"UNLocationCode": "USSAV"}}},
    ]
    with pytest.raises(CustomsHoldError, match="vessel arrived at GHTEM but container MSCU2222222's latest event"):
        eta_from_api(payload, booking=BOOKING, now=NOW)


def test_17_api_sandbox_trimmed(fx: Callable[[str], Any], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.msc")
    result = eta_from_api(fx("api_sandbox_trimmed"), booking=BOOKING, now=NOW)
    assert result.eta == datetime(2022, 12, 26)
    assert result.containers == ("MSCU1234567",)    # c0 is the alphabetical first (MSC-D9)
    assert result.details["container"] == "MSCU1234567"
    assert "other_container_etas" not in result.details
    assert warnings_of(caplog) == []                # MSCU1234568's timeline shares the TRANSPORT ETA event


@pytest.mark.parametrize(("text", "expected"), [
    ("2022-12-26T04:00:00Z", datetime(2022, 12, 26, 4, 0)),
    ("2022-12-26T04:00:00.0000000+00:00", datetime(2022, 12, 26, 4, 0)),
    ("2026-11-12T06:00:00+02:00", datetime(2026, 11, 12, 6, 0)),          # the offset is dropped, not converted
    ("2026-11-12", datetime(2026, 11, 12)),
    (" 2026-11-12T06:00:00.1234567-05:00 ", datetime(2026, 11, 12, 6, 0, 0, 123456)),
])
def test_18_parse_wall_datetime(text: str, expected: datetime) -> None:
    parsed = parse_wall_datetime(text)
    assert parsed == expected
    assert parsed.tzinfo is None


@pytest.mark.parametrize("text", ["garbage", "", "12 Nov 2026", None])
def test_18_parse_wall_datetime_rejects(text: Any) -> None:
    with pytest.raises(ValueError):
        parse_wall_datetime(text)


# ── review fix MSK-02 / MSK-04: implausible (sentinel) dates, as 09 §A.3.4 step 3 ──
def test_api_event_with_a_sentinel_date_never_becomes_the_eta(fx: Callable[[str], Any]) -> None:
    """MSK-02: an event dated with a .NET sentinel (9999-12-31T23:59:59.9999999, 0001-01-01) is skipped, as in
    dcsa.parse_events: it can neither sort ahead of the real ETA nor become the ETA itself."""
    payload = fx("api_in_transit")
    sentinel = json.loads(json.dumps(payload[0]))                       # a copy of its 'Estimated Time of Arrival'
    sentinel["eventDateTime"] = "9999-12-31T23:59:59.9999999"
    assert eta_from_api([*payload, sentinel], booking=BOOKING, now=NOW).eta == datetime(2026, 11, 12)
    loaded = api_event("EQUIPMENT", "LOAD", "ACT", TRANSSHIPMENT_EVENT, "9999-12-31T23:59:59.9999999", "ESVLC",
                       "MSCU1111111")
    assert eta_from_api([*payload, loaded], booking=BOOKING, now=NOW).eta == datetime(2026, 11, 12)
    payload[0]["eventDateTime"] = "0001-01-01T00:00:00Z"                 # the only ETA event is a sentinel
    with pytest.raises(NoEtaError):
        eta_from_api(payload, booking=BOOKING, now=NOW)


@pytest.mark.parametrize("text", ["31 Dec 9999", "01 Jan 0001", "31 Dec 1999", "01 Jan 2101"])
def test_parse_msc_date_rejects_years_outside_2000_2100(text: str) -> None:
    """MSK-02: a matched event with such a date is the parity NoEtaError ('unreadable date'), never a year-1 or
    year-9999 ETA (which would e-mail 'ARRIVED ON 01/01' or overflow the transshipment rule)."""
    with pytest.raises(ValueError):
        parse_msc_date(text)
    body = gql_payload(gql_container("MSCU1111111", [gql_event(ETA_EVENT, text, "GH")]))
    with pytest.raises(NoEtaError, match="unreadable date"):
        eta_from_graphql(body, booking=BOOKING, now=NOW)


def test_parse_msc_date_accepts_the_plausible_bounds() -> None:
    assert parse_msc_date("01 Jan 2000") == datetime(2000, 1, 1)
    assert parse_msc_date("31 Dec 2100") == datetime(2100, 12, 31)


@pytest.mark.parametrize("final_eta", ["9999-12-31T23:59:59.9999999", "0001-01-01T00:00:00Z", "31 Dec 9999"])
def test_rescue_never_uses_a_sentinel_final_eta(final_eta: str) -> None:
    """MSK-02: the opt-in finalEta rescue (06 §7.5) skips a sentinel like every other MSC date."""
    body = gql_payload(gql_container("MSCU1111111", [gql_event("Export Loaded on Vessel", "20 Oct 2026", "ES")],
                                      final_eta=final_eta))
    with pytest.raises(NoEtaError):
        eta_from_graphql(body, booking=BOOKING, now=NOW, rescue=True)
    usable = gql_payload(gql_container("MSCU1111111", [gql_event("Export Loaded on Vessel", "20 Oct 2026", "ES")],
                                       final_eta="2026-11-12T06:00:00Z"))
    assert eta_from_graphql(usable, booking=BOOKING, now=NOW, rescue=True).eta == datetime(2026, 11, 12)


def test_a_sentinel_transshipment_on_another_container_never_fails_the_lookup() -> None:
    """MSK-04: the multi-container check is log only (06 §7.1 step 9). A 'Full Transshipment Loaded' on 31 Dec 9999
    on a second container (loaded + 7 days overflows) used to raise OverflowError and fail the whole booking."""
    c0 = gql_container("MSCU1111111", [gql_event(ETA_EVENT, "12 Nov 2026", "GH")])
    c1 = gql_container("MSCU2222222", [gql_event(TRANSSHIPMENT_EVENT, "31 Dec 9999", "ES")])
    assert eta_from_graphql(gql_payload(c0, c1), booking=BOOKING, now=NOW).eta == datetime(2026, 11, 12)


async def test_a_sentinel_transshipment_date_is_a_lookup_outcome_not_a_crash(
        token_store: TokenStore, mock: respx.MockRouter, make_client: Callable[..., MscClient]) -> None:
    """MSK-04 (03 §5 boundary rule): get_eta() raises NoEtaError or CarrierLookupError, never OverflowError."""
    seed_token(token_store)
    body = gql_payload(gql_container("MSCU1111111", [gql_event(TRANSSHIPMENT_EVENT, "31 Dec 9999", "ES")]))
    mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=body))
    with pytest.raises((NoEtaError, CarrierLookupError)):
        await make_client().get_eta(BOOKING)


def test_wrap_parse_maps_date_overflow_to_a_lookup_error() -> None:
    """MSK-04, defence in depth: date arithmetic that overflows is a payload problem, not a crash (03 §5)."""
    def overflow() -> Any:
        return datetime(9999, 12, 31) + (datetime(2026, 1, 8) - datetime(2026, 1, 1))

    with pytest.raises(CarrierLookupError, match="Unexpected MSC payload shape"):
        msc._wrap_parse(overflow, BOOKING)


# ═════════════════════════════ Rescue-only fallbacks (06 §7.5) ═════════════════════════════
def test_rescue_mymsc_final_eta(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.msc")
    events = [gql_event("Export received at CY", "14 Oct 2026")]
    body = gql_payload(gql_container("MSCU1111111", events, final_eta="12 Nov 2026"))
    with pytest.raises(NoEtaError):                                   # off by default (MSC-D10)
        eta_from_graphql(body, booking=BOOKING, now=NOW)
    result = eta_from_graphql(body, booking=BOOKING, now=NOW, rescue=True)
    assert result.eta == datetime(2026, 11, 12)
    assert result.source == "MSC containers[0].finalEta (rescue)"
    assert result.details["rescue"] is True and result.is_actual is False
    assert any("rescue fallback" in m for m in warnings_of(caplog))
    iso = gql_payload(gql_container("MSCU1111111", events, final_eta="2026-11-12T06:00:00Z"))
    assert eta_from_graphql(iso, booking=BOOKING, now=NOW, rescue=True).eta == datetime(2026, 11, 12)
    for unusable in ("soon", None):
        with pytest.raises(NoEtaError, match="Can't determine ETA"):
            eta_from_graphql(gql_payload(gql_container("MSCU1111111", events, final_eta=unusable)),
                             booking=BOOKING, now=NOW, rescue=True)
    # never changes a result the original produces
    normal = gql_payload(gql_container("MSCU1111111", [gql_event(ETA_EVENT, "13 Nov 2026", "GH")],
                                       final_eta="20 Nov 2026"))
    kept = eta_from_graphql(normal, booking=BOOKING, now=NOW, rescue=True)
    assert kept.eta == datetime(2026, 11, 13) and "rescue" not in kept.details


def test_rescue_api_dcsa_arrival() -> None:
    payload = [
        api_event("TRANSPORT", "ARRI", "EST", "Vessel Arrival", "2026-11-12T06:00:00Z", "GHTEM"),
        api_event("EQUIPMENT", "LOAD", "ACT", "Export Loaded on Vessel", "2026-10-20T09:15:00Z", "USNYC",
                  "MSCU1111111"),
    ]
    with pytest.raises(NoEtaError):                                   # off by default (MSC-D10)
        eta_from_api(payload, booking=BOOKING, now=NOW)
    result = eta_from_api(payload, booking=BOOKING, now=NOW, rescue=True)
    assert result.eta == datetime(2026, 11, 12)                       # truncated to midnight
    assert result.source == "MSC API DCSA arrival (rescue)"
    assert result.details["rescue"] is True and result.details["mode"] == "api"
    assert result.details["event_datetime"] == "2026-11-12T06:00:00"
    assert result.containers == ("MSCU1111111",) and result.is_actual is False
    nothing = [api_event("EQUIPMENT", "LOAD", "ACT", "Export Loaded on Vessel", "2026-10-20T09:15:00Z", "USNYC",
                         "MSCU1111111")]
    with pytest.raises(NoEtaError, match="Can't determine ETA"):      # rescue fails too → the normal NoEtaError
        eta_from_api(nothing, booking=BOOKING, now=NOW, rescue=True)


# ═════════════════════════════ Client behaviour (respx) ═════════════════════════════
async def test_19_bridge_request_shape(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                       token_store: TokenStore, fx: Callable[[str], Any]) -> None:
    seed_token(token_store)
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=fx("graphql_in_transit")))
    result = await make_client().get_eta(BOOKING)
    assert result.eta == datetime(2026, 11, 12)
    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {TOKEN}"
    assert sent.headers["mymsc-user-email"] == "ops@example.com"
    assert sent.headers["mymsc-user-roles"] == "Customer"
    assert sent.headers["Referer"] == "https://www.mymsc.com/"
    assert sent.headers["Content-Type"] == "application/json"
    assert not any(name.lower().startswith("sec-ch-ua") for name in sent.headers)   # MSC-D2
    assert sent.headers["User-Agent"].startswith("ETAutomationsAPI/")
    assert json.loads(sent.content) == graphql_body(BOOKING)


async def test_20_no_token_latches_without_http(mock: respx.MockRouter,
                                                make_client: Callable[..., MscClient]) -> None:
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json={}))
    client = make_client()
    with pytest.raises(CarrierLookupError) as first:
        await client.get_eta(BOOKING)
    assert str(first.value) == NO_TOKEN_MESSAGE
    assert str(first.value).startswith("No usable MSC access token")
    with pytest.raises(CarrierLookupError) as second:
        await client.get_eta("EBKG00000002")
    assert str(second.value) == str(first.value)
    assert route.call_count == 0
    assert client.latched == NO_TOKEN_MESSAGE


async def test_21_rejected_token_latches(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                         token_store: TokenStore) -> None:
    seed_token(token_store)
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(
        401, text="Something went wrong!!! Authentication failed.", headers={"Content-Type": "text/plain"}))
    client = make_client()
    with pytest.raises(CarrierLookupError, match="expired or was rejected") as first:
        await client.get_eta(BOOKING)
    assert str(first.value) == TOKEN_REJECTED_MESSAGE
    assert route.call_count == 1          # request_with_auth's retry hits the latched error, not the network
    assert token_store.get("msc") is None
    for later in ("EBKG00000002", "EBKG00000003"):
        with pytest.raises(CarrierLookupError) as again:
            await client.get_eta(later)
        assert str(again.value) == TOKEN_REJECTED_MESSAGE
    assert route.call_count == 1


@pytest.mark.parametrize("page", [
    AKAMAI_DENIED,
    "<html><body><h1>Access Denied</h1>Reference #18.5b7c1002.1793870000.1a2b3c4d</body></html>",
])
async def test_22_edge_block_latches_without_retry(page: str, mock: respx.MockRouter,
                                                   make_client: Callable[..., MscClient],
                                                   token_store: TokenStore) -> None:
    seed_token(token_store)
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(403, text=page,
                                                                headers={"Content-Type": "text/html"}))
    client = make_client()
    with pytest.raises(CarrierLookupError) as first:
        await client.get_eta(BOOKING)
    assert str(first.value) == EDGE_BLOCK_MESSAGE
    with pytest.raises(CarrierLookupError) as again:
        await client.get_eta("EBKG00000002")
    assert str(again.value) == EDGE_BLOCK_MESSAGE
    assert route.call_count == 1                       # no retry, let alone one with altered headers
    assert token_store.get("msc") is not None          # a 403 edge block is not a token problem


async def test_22_api_mode_edge_block(mock: respx.MockRouter, make_client: Callable[..., MscClient]) -> None:
    route = mock.get(API_EVENTS).mock(return_value=httpx.Response(403, text=AKAMAI_DENIED))
    client = make_client(API)
    with pytest.raises(CarrierLookupError, match="^MSC's edge network refused"):
        await client.get_eta(BOOKING)
    with pytest.raises(CarrierLookupError, match="^MSC's edge network refused"):
        await client.get_eta(BOOKING)
    assert route.call_count == 1


def test_edge_block_detection() -> None:
    assert msc._is_edge_block_text(AKAMAI_DENIED)
    assert msc._is_edge_block_text("Access Denied … https://errors.edgesuite.net/18.1")
    assert not msc._is_edge_block_text("Access denied due to invalid subscription key. Make sure to provide a "
                                       "valid key for an active subscription.")
    assert not msc._is_edge_block_text("Reference #12 without the denial text")
    assert not msc._is_edge_block_text("")


async def test_23_env_token_prefix_and_expiry(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                              make_settings: Callable[..., Any], token_store: TokenStore,
                                              fx: Callable[[str], Any]) -> None:
    settings = make_settings(MSC_ACCESS_TOKEN="Bearer abc.def.ghi")
    assert settings.msc.mode == "mymsc"
    assert settings.msc.manual_token == "abc.def.ghi"
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=fx("graphql_in_transit")))
    await make_client(settings.msc).get_eta(BOOKING)
    assert route.calls.last.request.headers["Authorization"] == "Bearer abc.def.ghi"
    # no readable JWT exp → obtained_at + MSC_TOKEN_TTL_SECONDS (1800)
    assert token_store.get("msc") == {"access_token": "abc.def.ghi", "expires_at": EPOCH + 1800,
                                      "obtained_at": EPOCH, "source": "manual", "mode": "mymsc"}
    # a JWT: expiry from its exp claim
    token_store.delete("msc")
    exp = EPOCH + 2 * 3600
    jwt = make_jwt({"exp": exp})
    auth = MscBridgeAuth(make_settings(MSC_ACCESS_TOKEN=f"Bearer {jwt}").msc, token_store, epoch=lambda: EPOCH)
    assert await auth.get_token() == jwt
    assert token_store.get("msc")["expires_at"] == exp
    status = auth.token_status()
    assert (status["has_token"], status["seconds_left"], status["jwt_claims"]) == (True, 7200, {"exp": exp})
    # set_manual_token strips the prefix too
    other = make_jwt({"exp": exp + 60})
    assert auth.set_manual_token(f"Bearer {other}")["expires_at"] == exp + 60
    assert token_store.get("msc")["access_token"] == other


def test_24_manual_token_validation_and_claims(token_store: TokenStore) -> None:
    auth = MscBridgeAuth(BRIDGE, token_store, epoch=lambda: EPOCH)
    for bad in ("   ", "", "Bearer", "Bearer   ", "abc def", "Bearer abc\ndef"):
        with pytest.raises(ConfigError) as exc:
            auth.set_manual_token(bad)
        assert "abc" not in str(exc.value)          # the error never echoes the value
    assert token_store.get("msc") is None
    claims = {"iss": "https://login.mymsc.example/", "aud": ["tracking-api"], "exp": EPOCH + 3600,
              "iat": EPOCH - 60, "sub": "0f9c-user-id", "email": "clerk@example.com", "name": "Jane Clerk",
              "preferred_username": "jclerk"}
    jwt = make_jwt(claims)
    status = auth.set_manual_token(f"  bearer {jwt}\n")
    assert status == {"mode": "mymsc", "has_token": True, "source": "manual", "expires_at": EPOCH + 3600,
                      "seconds_left": 3600, "expired": False,
                      "jwt_claims": {"iss": "https://login.mymsc.example/", "aud": ["tracking-api"],
                                     "exp": EPOCH + 3600, "iat": EPOCH - 60}}
    dumped = json.dumps(status)
    for leaked in ("sub", "0f9c-user-id", "email", "clerk@example.com", "Jane Clerk", "jclerk", jwt):
        assert leaked not in dumped
    assert token_store.get("msc")["access_token"] == jwt


# ── review fix MSK-01: invisible / non-ASCII characters in a header value ──
@pytest.mark.parametrize("bad", ["Bearer abc\u200bdef.ghi.jkl", "\ufeffeyJhbGciOi.abc.def", "eyJhbGciOi\u2026",
                                 "abc\x01def.ghi.jkl"])
def test_token_with_invisible_or_non_ascii_characters_is_refused(token_store: TokenStore, bad: str) -> None:
    """MSK-01: a zero-width space, a BOM, a '…' from a shortened copy or a control character is refused when the
    token is pasted (an HTTP header can carry only ASCII), and the error never echoes the value."""
    with pytest.raises(ConfigError) as exc:
        msc.normalize_token(bad)
    assert "abc" not in str(exc.value) and "eyJ" not in str(exc.value)
    auth = MscBridgeAuth(BRIDGE, token_store, epoch=lambda: EPOCH)
    with pytest.raises(ConfigError):
        auth.set_manual_token(bad)
    assert token_store.get("msc") is None


async def test_non_ascii_msc_user_email_fails_every_lookup_fast(token_store: TokenStore, mock: respx.MockRouter,
                                                                make_client: Callable[..., MscClient]) -> None:
    """MSK-01 (03 §5 boundary rule): a non-ASCII MSC_USER_EMAIL cannot be sent as a header. Every MSC lookup fails
    with one latched CarrierLookupError naming the setting — never a UnicodeEncodeError — and nothing is sent."""
    seed_token(token_store)
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json={}))
    client = make_client(MscSettings(mode="mymsc", user_email="m\u00fcller@example.com"))
    with pytest.raises(CarrierLookupError) as first:
        await client.get_eta(BOOKING)
    assert "MSC_USER_EMAIL" in str(first.value) and "m\u00fcller" not in str(first.value)
    with pytest.raises(CarrierLookupError) as second:
        await client.get_eta("EBKG00000002")
    assert str(second.value) == str(first.value) and route.call_count == 0
    assert client.latched == str(first.value)


async def test_stored_token_with_a_non_ascii_character_fails_fast_and_a_new_token_clears_it(
        token_store: TokenStore, mock: respx.MockRouter, make_client: Callable[..., MscClient]) -> None:
    """MSK-01: a token stored before this check (or edited by hand) gives a latched 'paste it again' error; pasting a
    clean token clears that latch."""
    seed_token(token_store, "abc\u200bdef.ghi.jkl")
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json={}))
    client = make_client()
    with pytest.raises(CarrierLookupError, match="eta msc-token") as first:
        await client.get_eta(BOOKING)
    assert "abc" not in str(first.value) and route.call_count == 0
    client.set_manual_token(TOKEN)
    assert client.latched is None


# ── review fix MSK-05: tokens.json that cannot be written ──
async def test_rejected_token_is_latched_although_tokens_json_cannot_be_written(
        state_dir: Path, mock: respx.MockRouter) -> None:
    """MSK-05 (03 §5, MSC-D8): a 401 latches 'expired or was rejected' even when the rejected token cannot be removed
    from tokens.json; later bookings fail fast with the same text and send nothing."""
    path = state_dir / "tokens.json"
    seed_token(TokenStore(path))
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(
        401, text="Something went wrong!!! Authentication failed.", headers={"Content-Type": "text/plain"}))
    client = MscClient(BRIDGE, UnwritableTokenStore(path), clock=FixedClock(NOW), epoch=lambda: EPOCH)
    try:
        for booking in (BOOKING, "EBKG00000002", "EBKG00000003"):
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta(booking)
            assert str(info.value) == TOKEN_REJECTED_MESSAGE
    finally:
        await client.aclose()
    assert route.call_count == 1


async def test_env_token_is_used_although_tokens_json_cannot_be_written(
        state_dir: Path, mock: respx.MockRouter, fx: Callable[[str], Any], caplog: pytest.LogCaptureFixture) -> None:
    """MSK-05: MSC_ACCESS_TOKEN still works when it cannot be cached in tokens.json (one WARNING, never the token)."""
    caplog.set_level(logging.WARNING, logger="eta")
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=fx("graphql_in_transit")))
    settings = MscSettings(mode="mymsc", user_email="ops@example.com", manual_token=TOKEN)
    client = MscClient(settings, UnwritableTokenStore(state_dir / "tokens.json"), clock=FixedClock(NOW),
                       epoch=lambda: EPOCH)
    try:
        for booking in (BOOKING, "EBKG00000002"):
            assert (await client.get_eta(booking)).eta == datetime(2026, 11, 12)
    finally:
        await client.aclose()
    assert route.call_count == 2
    assert all(call.request.headers["Authorization"] == f"Bearer {TOKEN}" for call in route.calls)
    warned = [m for m in warnings_of(caplog) if "tokens.json" in m]
    assert len(warned) == 1 and TOKEN not in warned[0]


async def test_token_pasted_at_the_run_prompt_is_used_although_tokens_json_cannot_be_written(
        state_dir: Path, mock: respx.MockRouter, fx: Callable[[str], Any]) -> None:
    """MSK-05: the run's pre-flight paste (MscClient.set_manual_token) keeps the token in memory when it cannot be
    saved; `eta msc-token` (store_manual_token) still fails loudly, so it never claims a token was stored."""
    store = UnwritableTokenStore(state_dir / "tokens.json")
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=fx("graphql_in_transit")))
    client = MscClient(BRIDGE, store, clock=FixedClock(NOW), epoch=lambda: EPOCH)
    try:
        status = client.set_manual_token(f"Bearer {TOKEN}")
        assert status["has_token"] is True
        assert (await client.get_eta(BOOKING)).eta == datetime(2026, 11, 12)
    finally:
        await client.aclose()
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    with pytest.raises(OSError):
        msc.store_manual_token(BRIDGE, store, TOKEN, epoch=lambda: EPOCH)


async def test_non_ascii_subscription_key_fails_fast(token_store: TokenStore, mock: respx.MockRouter,
                                                     make_client: Callable[..., MscClient]) -> None:
    route = mock.get(API_EVENTS).mock(return_value=httpx.Response(200, json=[]))
    client = make_client(MscSettings(mode="api", api_base_url=API_BASE, api_key="key\u2026"))
    with pytest.raises(CarrierLookupError, match="MSC_API_SUBSCRIPTION_KEY"):
        await client.get_eta(BOOKING)
    assert route.call_count == 0


async def test_25_concurrency_is_bounded(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                         token_store: TokenStore, fx: Callable[[str], Any]) -> None:
    seed_token(token_store)
    payload = fx("graphql_in_transit")
    in_flight = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json=payload)

    route = mock.post(GQL_URL).mock(side_effect=handler)
    client = make_client(dataclasses.replace(BRIDGE, max_concurrency=4))
    results = await asyncio.gather(*(client.get_eta(f"EBKG{n:08d}") for n in range(20)))
    assert route.call_count == 20
    assert all(r.eta == datetime(2026, 11, 12) for r in results)
    assert 2 <= peak <= 4


async def test_26_api_success(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                              fx: Callable[[str], Any]) -> None:
    route = mock.get(API_EVENTS).mock(return_value=httpx.Response(200, json=fx("api_in_transit")))
    result = await make_client(API).get_eta(BOOKING)
    assert result.eta == datetime(2026, 11, 12) and result.details["mode"] == "api"
    sent = route.calls.last.request
    assert sent.url.params["carrierBookingReference"] == BOOKING
    assert sent.headers["Ocp-Apim-Subscription-Key"] == API_KEY
    assert "subscription-key" not in sent.url.params      # the header, never the query form (06 §4.2)
    assert "Authorization" not in sent.headers


async def test_26_api_404_is_not_latched(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                         fx: Callable[[str], Any]) -> None:
    route = mock.get(API_EVENTS).mock(return_value=httpx.Response(404, json=fx("api_error_404")))
    client = make_client(API)
    for booking in ("EBKG00000001", "EBKG00000002"):
        with pytest.raises(CarrierLookupError) as exc:
            await client.get_eta(booking)
        assert str(exc.value) == (f"MSC API has no events for booking {booking} (HTTP 404): No events found for the "
                                  "given reference — the booking may be unknown to MSC or not linked to your account")
    assert route.call_count == 2
    assert client.latched is None


async def test_26_api_quota_403_latches(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                        fx: Callable[[str], Any]) -> None:
    route = mock.get(API_EVENTS).mock(return_value=httpx.Response(403, json=fx("apim_quota_403")))
    client = make_client(API)
    expected = "MSC API call quota exhausted: Out of call volume quota. Quota will be replenished in 2.11:42:00."
    with pytest.raises(CarrierLookupError) as first:
        await client.get_eta(BOOKING)
    assert str(first.value) == expected
    with pytest.raises(CarrierLookupError) as again:
        await client.get_eta("EBKG00000002")
    assert str(again.value) == expected
    assert route.call_count == 1


async def test_26_api_401_latches(mock: respx.MockRouter, make_client: Callable[..., MscClient]) -> None:
    route = mock.get(API_EVENTS).mock(return_value=httpx.Response(401, json={
        "statusCode": 401, "message": "Access denied due to invalid subscription key. Make sure to provide a valid "
                                      "key for an active subscription."}))
    client = make_client(API)
    with pytest.raises(CarrierLookupError) as first:
        await client.get_eta(BOOKING)
    assert str(first.value) == (
        "MSC API rejected the subscription key (HTTP 401): Access denied due to invalid subscription key. Make sure "
        "to provide a valid key for an active subscription. Check MSC_API_SUBSCRIPTION_KEY and that the "
        "subscription is approved/active.")
    with pytest.raises(CarrierLookupError, match="rejected the subscription key"):
        await client.get_eta("EBKG00000002")
    assert route.call_count == 1


async def test_26_api_429_then_200(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                   fx: Callable[[str], Any], http_sleeps: list[float]) -> None:
    route = mock.get(API_EVENTS).mock(side_effect=[
        httpx.Response(429, headers={"Retry-After": "0"},
                       json={"statusCode": 429, "message": "Rate limit is exceeded. Try again in 0 seconds."}),
        httpx.Response(200, json=fx("api_in_transit")),
    ])
    result = await make_client(API).get_eta(BOOKING)
    assert result.eta == datetime(2026, 11, 12)
    assert route.call_count == 2
    assert http_sleeps == [0.0]


async def test_api_400_hint_and_exhausted_retries(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                                  http_sleeps: list[float]) -> None:
    busy = httpx.Response(503, text="Service Unavailable", headers={"x-azure-ref": "20261105T093000Z-ref42"})
    route = mock.get(API_EVENTS).mock(side_effect=[
        httpx.Response(400, json={
            "httpMethod": "GET",
            "errors": [{"reason": "Bad Request", "message": "Invalid carrierBookingReference format"}],
            "statusCode": 400, "statusCodeText": "BadRequest"}),
        busy, busy, busy, busy,
    ])
    client = make_client(API)
    with pytest.raises(CarrierLookupError) as bad:
        await client.get_eta(BOOKING)
    assert str(bad.value).startswith("MSC API rejected the request for EBKG12345678 (HTTP 400): Invalid "
                                     "carrierBookingReference format — MSC may not accept EBKG")
    with pytest.raises(CarrierLookupError) as unavailable:
        await client.get_eta(BOOKING)
    assert str(unavailable.value).startswith(
        "MSC API unavailable/rate-limited for EBKG12345678: msc: unexpected HTTP 503")
    assert "request_id=20261105T093000Z-ref42" in str(unavailable.value)   # x-azure-ref (06 §12)
    assert route.call_count == 5            # 400 once, then 503 retried up to 4 attempts (03 §6)
    assert client.latched is None


async def test_27_sandbox_refused_unless_allowed(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                                 make_settings: Callable[..., Any], fx: Callable[[str], Any]) -> None:
    settings = make_settings(MSC_MODE="api", MSC_API_BASE_URL=SANDBOX_BASE_URL).msc
    assert settings.mode == "api" and settings.api_is_sandbox and not settings.api_key
    route = mock.get(SANDBOX_EVENTS).mock(return_value=httpx.Response(200, json=fx("api_sandbox_trimmed")))
    with pytest.raises(CarrierLookupError) as refused:
        await make_client(settings, allow_sandbox=False).get_eta(BOOKING)
    assert str(refused.value) == SANDBOX_REFUSAL
    with pytest.raises(CarrierLookupError, match="SANDBOX"):
        await make_client(settings).get_eta(BOOKING)          # MSC_ALLOW_SANDBOX defaults to false
    assert route.call_count == 0
    result = await make_client(settings, allow_sandbox=True).get_eta(BOOKING)
    assert result.eta == datetime(2022, 12, 26)
    assert route.call_count == 1
    assert "Ocp-Apim-Subscription-Key" not in route.calls.last.request.headers   # no key for the sandbox


def test_28_msc_mode_resolution(make_settings: Callable[..., Any]) -> None:
    both = make_settings(MSC_API_BASE_URL=API_BASE, MSC_API_SUBSCRIPTION_KEY=API_KEY)
    assert both.msc is not None and both.msc.mode == "api"
    assert make_settings(MSC_API_BASE_URL=API_BASE).msc.mode == "mymsc"
    assert make_settings(MSC_API_SUBSCRIPTION_KEY=API_KEY).msc.mode == "mymsc"
    assert make_settings().msc.mode == "mymsc"
    explicit = make_settings(MSC_MODE="api")
    assert explicit.msc is None
    assert explicit.missing["msc"] == ("MSC_API_BASE_URL", "MSC_API_SUBSCRIPTION_KEY")
    assert make_settings(MSC_MODE="api", MSC_API_BASE_URL=API_BASE).missing["msc"] == ("MSC_API_SUBSCRIPTION_KEY",)
    sandbox = make_settings(MSC_MODE="api", MSC_API_BASE_URL=SANDBOX_BASE_URL)
    assert sandbox.msc is not None and "msc" not in sandbox.missing      # the key is exempt for the sandbox
    forced = make_settings(MSC_MODE="mymsc", MSC_API_BASE_URL=API_BASE, MSC_API_SUBSCRIPTION_KEY=API_KEY)
    assert forced.msc.mode == "mymsc"
    with pytest.raises(ConfigError):
        make_settings(MSC_MODE="browser")


async def test_29_secrets_never_logged(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                       make_settings: Callable[..., Any], token_store: TokenStore,
                                       fx: Callable[[str], Any], caplog: pytest.LogCaptureFixture,
                                       http_sleeps: list[float]) -> None:
    caplog.set_level(logging.DEBUG, logger="eta")
    env_token = make_jwt({"exp": EPOCH + 3600, "sub": "user-1"})
    settings = make_settings(MSC_ACCESS_TOKEN=f"Bearer {env_token}")
    mock.post(GQL_URL).mock(side_effect=[
        httpx.Response(200, json=fx("graphql_in_transit")),
        httpx.Response(401, text="Something went wrong!!! Authentication failed."),
    ])
    bridge = make_client(settings.msc)
    await bridge.get_eta(BOOKING)
    with pytest.raises(CarrierLookupError) as rejected:
        await bridge.get_eta("EBKG00000002")
    mock.get(API_EVENTS).mock(side_effect=[
        httpx.Response(200, json=fx("api_in_transit")),
        httpx.Response(401, json={"statusCode": 401, "message": "Access denied due to invalid subscription key."}),
    ])
    api = make_client(API)
    await api.get_eta(BOOKING)
    with pytest.raises(CarrierLookupError) as key_rejected:
        await api.get_eta("EBKG00000002")
    seed_token(token_store)
    mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=fx("graphql_in_transit")))
    probe = await make_client().probe(BOOKING)
    assert any(r.name == "eta.http" for r in caplog.records)     # the DEBUG request lines were captured
    texts = [caplog.text, str(rejected.value), str(key_rejected.value), json.dumps(probe)]
    for secret in (env_token, API_KEY, TOKEN):
        assert all(secret not in text for text in texts), secret


# ═════════════════════════════ Latch recovery, api-mode token surface ═════════════════════════════
async def test_pasted_token_clears_the_token_latch(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                                   fx: Callable[[str], Any]) -> None:
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=fx("graphql_in_transit")))
    client = make_client()
    with pytest.raises(CarrierLookupError, match="No usable MSC access token"):
        await client.get_eta(BOOKING)
    status = client.set_manual_token(f"Bearer {TOKEN}")      # the CLI pre-flight path (06 §5.1)
    assert status["has_token"] is True and status["seconds_left"] == 1800
    assert (await client.get_eta(BOOKING)).eta == datetime(2026, 11, 12)
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"


async def test_api_mode_token_surface(make_client: Callable[..., MscClient], token_store: TokenStore) -> None:
    client = make_client(API)
    with pytest.raises(ConfigError):
        client.set_manual_token(TOKEN)
    assert client.token_status() == {"mode": "api", "host": "api.msc.example.test", "sandbox": False, "has_key": True}
    assert client.auth is None
    # `eta msc-token` still stores a bridge token in api mode (`--compare` needs one)
    status = msc.store_manual_token(API, token_store, TOKEN, epoch=lambda: EPOCH)
    assert status["has_token"] is True and token_store.get("msc")["access_token"] == TOKEN
    assert msc.bridge_token_status(API, token_store, epoch=lambda: EPOCH)["seconds_left"] == 1800


def test_token_status_flags_expired_and_stale_tokens(token_store: TokenStore) -> None:
    auth = MscBridgeAuth(BRIDGE, token_store, epoch=lambda: EPOCH)
    assert auth.token_status() == {"mode": "mymsc", "has_token": False, "source": None, "expires_at": None,
                                   "seconds_left": None, "expired": False, "jwt_claims": {}}
    expired = make_jwt({"exp": EPOCH - 60})
    status = auth.set_manual_token(expired)                 # expired on arrival: still stored, but flagged
    assert (status["has_token"], status["expired"], status["seconds_left"]) == (True, True, -60)
    assert token_store.get("msc")["expires_at"] == EPOCH - 60
    assert token_needs_refresh(status) is True
    # MSC_ACCESS_TOKEN beats a stale stored token, exactly as get_token() would
    with_env = MscBridgeAuth(dataclasses.replace(BRIDGE, manual_token=TOKEN), token_store, epoch=lambda: EPOCH)
    assert with_env.token_status()["seconds_left"] == 1800


def test_token_needs_refresh() -> None:
    assert token_needs_refresh({"mode": "api", "host": "x"}) is False
    assert token_needs_refresh({"mode": "mymsc", "has_token": False, "seconds_left": None}) is True
    assert token_needs_refresh({"mode": "mymsc", "has_token": True, "seconds_left": None}) is True
    assert token_needs_refresh({"mode": "mymsc", "has_token": True, "seconds_left": 599}) is True
    assert token_needs_refresh({"mode": "mymsc", "has_token": True, "seconds_left": 600}) is False


async def test_empty_user_email_omits_header_and_warns_once(mock: respx.MockRouter,
                                                            make_client: Callable[..., MscClient],
                                                            token_store: TokenStore, fx: Callable[[str], Any],
                                                            caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.msc")
    seed_token(token_store)
    route = mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=fx("graphql_in_transit")))
    client = make_client(dataclasses.replace(BRIDGE, user_email=""))
    await client.get_eta(BOOKING)
    await client.get_eta("EBKG00000002")
    assert "mymsc-user-email" not in route.calls.last.request.headers
    assert route.calls.last.request.headers["mymsc-user-roles"] == "Customer"
    assert len([m for m in warnings_of(caplog) if "MSC_USER_EMAIL is empty" in m]) == 1


async def test_bridge_server_errors_are_not_latched(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                                    token_store: TokenStore, http_sleeps: list[float]) -> None:
    seed_token(token_store)
    route = mock.post(GQL_URL).mock(side_effect=[httpx.Response(500, text="oops")] * 4
                                    + [httpx.Response(200, text="<html>maintenance</html>")])
    client = make_client()
    with pytest.raises(CarrierLookupError,
                       match="^MSC tracking request failed for EBKG12345678: msc: unexpected HTTP 500"):
        await client.get_eta(BOOKING)
    assert route.call_count == 4          # the GraphQL POST is a pure read: idempotent, retried (06 §4.1)
    with pytest.raises(CarrierLookupError, match="response was not valid JSON"):
        await client.get_eta(BOOKING)
    assert client.latched is None


async def test_unexpected_payload_shape_is_carrier_lookup_error(mock: respx.MockRouter,
                                                                make_client: Callable[..., MscClient],
                                                                token_store: TokenStore) -> None:
    seed_token(token_store)
    mock.post(GQL_URL).mock(side_effect=[
        httpx.Response(200, json={"data": {"trackingByBookingNumber": [{"containers": [None]}]}}),
        httpx.Response(200, json={"data": ["unexpected"]}),
    ])
    client = make_client()
    for _ in range(2):
        with pytest.raises(CarrierLookupError, match="^Unexpected MSC payload shape for EBKG12345678: "):
            await client.get_eta(BOOKING)


# ═════════════════════════════ Factory, probe and CLI helpers ═════════════════════════════
async def test_build_client(make_settings: Callable[..., Any], token_store: TokenStore,
                            caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="eta.carrier.msc")
    assert msc.build_client(make_settings(MSC_MODE="api"), token_store) is None
    bridge = msc.build_client(make_settings(), token_store, clock=FixedClock(NOW))
    api = msc.build_client(make_settings(MSC_API_BASE_URL=API_BASE, MSC_API_SUBSCRIPTION_KEY=API_KEY), token_store)
    try:
        assert isinstance(bridge, MscClient) and bridge.mode == "mymsc" and isinstance(bridge, CarrierClient)
        assert isinstance(api, MscClient) and api.mode == "api" and api.carrier is Carrier.MSC
        assert "MSC mode: mymsc (token bridge)" in caplog.text
        assert "MSC mode: api (api.msc.example.test)" in caplog.text
    finally:
        await bridge.aclose()
        await api.aclose()


async def test_probe_reports_requests_raw_and_eta(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                                  token_store: TokenStore, fx: Callable[[str], Any]) -> None:
    seed_token(token_store)
    mock.post(GQL_URL).mock(return_value=httpx.Response(200, json=fx("graphql_in_transit")))
    client = make_client()
    out = await client.probe(BOOKING)
    json.dumps(out)                                           # JSON-safe
    assert out["mode"] == "mymsc" and out["error"] is None
    assert out["eta"]["eta"] == "2026-11-12T00:00:00" and out["eta"]["carrier"] == "MSC"
    assert [(r["method"], r["url"], r["status"]) for r in out["requests"]] == [("POST", GQL_URL, 200)]
    assert isinstance(out["requests"][0]["elapsed_ms"], int)
    assert out["raw"]["data"]["trackingByBookingNumber"][0]["containers"][0]["containerNumber"] == "MSCU1111111"
    assert out["bridge"]["has_token"] is True and out["bridge"]["seconds_left"] == 3600
    assert client._http.event_hooks == {"request": [], "response": []}     # probe hooks removed again
    # the saved (redacted) file keeps the token status readable — and never the token
    saved = json.loads(msc.save_probe_output(token_store.path.parent, BOOKING, out).read_text(encoding="utf-8"))
    assert saved["bridge"]["seconds_left"] == 3600 and saved["eta"]["eta"] == "2026-11-12T00:00:00"
    assert TOKEN not in json.dumps(saved)


async def test_probe_never_raises(mock: respx.MockRouter, make_client: Callable[..., MscClient],
                                  fx: Callable[[str], Any]) -> None:
    out = await make_client().probe(BOOKING)
    assert out["eta"] == out["error"] == f"CarrierLookupError: {NO_TOKEN_MESSAGE}"
    assert out["requests"] == [] and out["raw"] is None
    mock.get(API_EVENTS).mock(return_value=httpx.Response(404, json=fx("api_error_404")))
    api_out = await make_client(API).probe(BOOKING)
    assert api_out["error"].startswith("CarrierLookupError: MSC API has no events for booking EBKG12345678")
    assert api_out["raw"]["errors"][0]["message"] == "No events found for the given reference"
    assert [r["status"] for r in api_out["requests"]] == [404]
    assert api_out["api"] == {"mode": "api", "host": "api.msc.example.test", "sandbox": False, "has_key": True}


async def test_probe_sandbox(mock: respx.MockRouter, token_store: TokenStore, fx: Callable[[str], Any]) -> None:
    route = mock.get(SANDBOX_EVENTS).mock(return_value=httpx.Response(200, json=fx("api_sandbox_trimmed")))
    out = await msc.probe_sandbox(token_store, settings=API, clock=FixedClock(NOW))
    assert out["sandbox"] is True and out["matches_expected"] is True and out["expected_eta"] == "2022-12-26"
    assert out["eta"]["source"] == "MSC API event 'Estimated Time of Arrival'"
    assert out["event_count"] == 3
    assert any("CXXXX" in line for line in out["explain"])
    sent = route.calls.last.request
    assert "Ocp-Apim-Subscription-Key" not in sent.headers       # the user's key is never sent to the sandbox
    assert sent.url.params["carrierBookingReference"] == msc.SANDBOX_PROBE_BOOKING
    json.dumps(out)


async def test_compare_modes(mock: respx.MockRouter, token_store: TokenStore, fx: Callable[[str], Any]) -> None:
    def graphql(request: httpx.Request) -> httpx.Response:
        booking = json.loads(request.content)["variables"]["input"]["bookingNumber"]
        if booking == BOOKING:
            return httpx.Response(200, json=fx("graphql_in_transit"))
        return httpx.Response(200, json=gql_payload(gql_container("MSCU9999999", [gql_event(ETA_EVENT, "20 Nov 2026",
                                                                                              "GH")])))

    mock.post(GQL_URL).mock(side_effect=graphql)
    mock.get(API_EVENTS).mock(return_value=httpx.Response(200, json=fx("api_in_transit")))
    settings = dataclasses.replace(API, user_email="ops@example.com")
    with pytest.raises(ConfigError, match="No usable MSC access token"):
        await msc.compare_modes(settings, token_store, [BOOKING], clock=FixedClock(NOW), epoch=lambda: EPOCH)
    with pytest.raises(ConfigError, match="MSC_API_BASE_URL"):
        await msc.compare_modes(BRIDGE, token_store, [BOOKING], clock=FixedClock(NOW), epoch=lambda: EPOCH)
    seed_token(token_store)
    report = await msc.compare_modes(settings, token_store, [BOOKING, "EBKG99999999"], clock=FixedClock(NOW),
                                     epoch=lambda: EPOCH)
    rows = {r["booking"]: r for r in report["rows"]}
    assert rows[BOOKING]["match"] == "MATCH"
    assert rows[BOOKING]["mymsc"]["outcome"] == rows[BOOKING]["api"]["outcome"] == "2026-11-12"
    assert rows["EBKG99999999"]["match"] == "DIFF"
    assert (report["matches"], report["diffs"], report["any_diff"]) == (1, 1, True)
    table = msc.format_compare_table(report)
    assert table.splitlines()[0].startswith("BOOKING")
    assert "MATCH" in table and "DIFF" in table and table.endswith("1 MATCH, 1 DIFF")
    json.dumps(report)


def test_save_probe_output(state_dir: Path) -> None:
    data = {"raw": {"Authorization": "Bearer secret-xyz"}, "note": "Bearer other-secret", "eta": "x"}
    path = msc.save_probe_output(state_dir, "EBKG1234/5678", data, when=datetime(2026, 11, 5, 9, 30, 1))
    assert path == state_dir / "probe" / "msc_EBKG1234_5678_20261105_093001.json"
    text = path.read_text(encoding="utf-8")
    assert "secret-xyz" not in text and "other-secret" not in text
    assert json.loads(text)["eta"] == "x"
