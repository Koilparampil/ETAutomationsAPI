"""CMA CGM Track & Trace client — 08-carrier-cma-cgm.md §9.4 tests 1–30, plus edge cases.

Pure helpers run on the fixtures of 08 §9.1–§9.2 (tests/fixtures/cma/, written verbatim from the spec) with
the expected values of 08 §9.3. HTTP tests use respx (no network, 12-testing §1) and inject `sleep` /
`monotonic` into CmaClient: FakeTime makes every wait instant and records it; ScaledTime keeps real asyncio
ordering at 1/100 speed for the shared 429 gate test.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from eta_automations.carriers import cma, dcsa
from eta_automations.carriers.base import CarrierClient
from eta_automations.carriers.router import CarrierRouter
from eta_automations.carriers.cma import (
    HINT_401,
    HINT_403,
    CmaApiKeyAuth,
    CmaClient,
    CmaOAuth,
    build_client,
    check_booking_identity,
    container_references,
    describe_arrival,
    evaluate_events,
    extract_cursor,
    is_empty_equipment,
    prefilter_raw_events,
    resolve_pod_unlocode,
)
from eta_automations.carriers.dcsa import DcsaArrival
from eta_automations.config import CmaSettings
from eta_automations.errors import CarrierLookupError, ConfigError, NoEtaError
from eta_automations.models import Carrier, EtaResult
from eta_automations.token_store import TokenStore
from fakes import UnwritableTokenStore

FIX = Path(__file__).parent / "fixtures" / "cma"
BASE = "https://apis.cma-cgm.net/operation/trackandtrace/v1"
TOKEN_URL = "https://auth.cma-cgm.com/as/token.oauth2"
QUERY_URL = f"{BASE}/events"
API_KEY = "test-cma-api-key-0123456789abcdef"
CLIENT_ID = "test-cma-client-id"
CLIENT_SECRET = "test-cma-client-secret-value"
SCOPE = "tandtcommercial:read:be tandtpublic:read:be"
HASH = "769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"
NEXT_CURSOR = "fE9mZnNldHw9NiZsaW1pdD02"

MSG_404 = ("CMA tracking API has no record of booking {ref} (HTTP 404). CMA returns 404 when a booking is not "
           "confirmed yet or was cancelled; CMA also drops shipments whose containers were returned to the depot "
           "more than 15 days ago.")
MSG_EMPTY = ("CMA public tracking shows 'shipment not found' for booking {ref}. Check the booking number — CMA also "
             "drops shipments whose containers were returned to the depot more than 15 days ago.")
EXACT_HINT_401 = ("CMA rejected the API credentials (HTTP 401). Check CMA_API_KEY (API Portal → API Applications → "
                  "your application card) or CMA_CLIENT_ID/CMA_CLIENT_SECRET; the 30-day free trial may have ended.")
EXACT_HINT_403 = ("CMA refused the request (HTTP 403: no sufficient rights). Check that your API application is "
                  "subscribed to the 'Logistic Tracking service API' (operation.trackandtrace.v1) and that your "
                  "trial or contract is active.")


def fixture_json(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


TOKEN_OK = fixture_json("oauth_token.json")
ACCESS_TOKEN = TOKEN_OK["access_token"]


def ref_url(ref: str) -> str:
    return f"{BASE}/events/{ref}"


def events_response(name: str = "events_in_transit.json", *, headers: Mapping[str, str] | None = None,
                    status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=fixture_json(name), headers=dict(headers or {}))


def json_response(status: int, name: str, headers: Mapping[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=fixture_json(name), headers=dict(headers or {}))


def token_response(token: str = ACCESS_TOKEN, **extra: Any) -> httpx.Response:
    return httpx.Response(200, json={**TOKEN_OK, "access_token": token, **extra})


class FakeTime:
    """Virtual monotonic clock + instant sleep recorder; a sleep advances the virtual clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


class ScaledTime:
    """Real asyncio scheduling at 1/scale speed: virtual seconds = real seconds × scale."""

    def __init__(self, scale: float = 100.0) -> None:
        self.scale = scale
        self._t0 = time.monotonic()
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return (time.monotonic() - self._t0) * self.scale

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(max(0.0, seconds) / self.scale)


@pytest.fixture
def cma_settings(make_settings: Callable[..., Any]) -> Callable[..., CmaSettings]:
    """cma_settings(**env) -> CmaSettings (API-key mode by default; pass CMA_API_KEY=None for OAuth)."""

    def _make(**env: Any) -> CmaSettings:
        settings = make_settings({"CMA_API_KEY": API_KEY, **env})
        assert settings.cma is not None
        return settings.cma

    return _make


@pytest.fixture
def oauth_settings(cma_settings: Callable[..., CmaSettings]) -> CmaSettings:
    settings = cma_settings(CMA_API_KEY=None, CMA_CLIENT_ID=CLIENT_ID, CMA_CLIENT_SECRET=CLIENT_SECRET)
    assert settings.auth_mode == "oauth"
    return settings


@contextlib.asynccontextmanager
async def open_client(settings: CmaSettings, store: TokenStore,
                      clock: FakeTime | ScaledTime | None = None) -> AsyncIterator[CmaClient]:
    """CmaClient on an injected (respx-mocked) httpx.AsyncClient with injected sleep/monotonic."""
    clock = clock or FakeTime()
    http = httpx.AsyncClient()
    client = CmaClient(settings, store, http=http, sleep=clock.sleep, monotonic=clock.monotonic)
    try:
        yield client
    finally:
        await client.aclose()
        await http.aclose()


def stamped(responses: Iterable[httpx.Response | Exception], stamps: list[float],
            clock: FakeTime | ScaledTime) -> Callable[[httpx.Request], httpx.Response]:
    """respx side effect returning `responses` in order and recording the virtual time of each call."""
    queue = list(responses)

    def side_effect(request: httpx.Request) -> httpx.Response:
        stamps.append(clock.monotonic())
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return side_effect


def cma_messages(caplog: pytest.LogCaptureFixture, level: int | None = None) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "eta.carrier.cma" and (level is None or r.levelno == level)]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 1. evaluate_events — one test per row of 08 §9.3
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_evaluate_in_transit() -> None:
    result = evaluate_events("NAM1234567", fixture_json("events_in_transit.json"))
    assert isinstance(result, EtaResult)
    assert result.carrier is Carrier.CMA and result.booking == "NAM1234567"
    assert result.eta == datetime(2026, 11, 10, 14, 0)   # newest-created EST supersedes the older 11-12 EST
    assert result.is_actual is False
    assert result.containers == ("CMAU1234567", "TGHU7654321")
    assert result.details["pod"] == "USLGB"
    assert result.source == "CMA ETA Berth at POD (estimated vessel arrival) at USLGB"
    assert result.details["endpoint"] == "reference" and result.details["pages"] == 1
    assert result.details["event_count"] == 10
    assert result.details["latest_container"] == "CMAU1234567"   # tie → the first max wins
    assert result.details["basis"] == "TRANSPORT ARRI EST"
    assert result.details["per_container"]["TGHU7654321"] == {
        "eta": "2026-11-10T14:00:00", "classifier": "EST", "basis": "TRANSPORT ARRI EST", "unlocode": "USLGB"}
    json.dumps(result.to_dict())   # details are JSON-serializable (report, probe)


def test_evaluate_arrived() -> None:
    result = evaluate_events("NAM2345678", fixture_json("events_arrived.json"))
    assert result.eta == datetime(2026, 11, 4, 9, 45)   # CMAU2000002's discharge, latest of the two
    assert result.is_actual is True
    assert result.details["latest_container"] == "CMAU2000002"
    assert result.source.startswith("CMA Arrived at POD (container discharged)")
    assert result.details["pod"] == "USNYC"
    assert result.details["per_container"]["CMAU2000001"]["eta"] == "2026-11-03T14:20:00"


def test_evaluate_partial_discharge() -> None:
    result = evaluate_events("NAM3456789", fixture_json("events_partial_discharge.json"))
    assert result.eta == datetime(2026, 11, 4, 13, 10)
    assert result.is_actual is True
    assert result.details["per_container"]["CMAU3000002"]["eta"] == "2026-11-04T07:30:00"   # vessel ARRI ACT
    assert result.details["latest_container"] == "CMAU3000001"


def test_evaluate_transshipment() -> None:
    result = evaluate_events("NAM4567890", fixture_json("events_transshipment.json"))
    assert result.eta == datetime(2026, 11, 16, 7, 0)   # ARRI EST at USNYC beats PLN 11-14; PTS DISC ignored
    assert result.is_actual is False
    assert result.details["pod"] == "USNYC"


def test_evaluate_missing_container_eta() -> None:
    with pytest.raises(NoEtaError) as info:
        evaluate_events("NAM5678901", fixture_json("events_missing_container_eta.json"))
    assert str(info.value) == (
        "No ETA published by CMA for booking NAM5678901 (container TGHU5000002). Can't determine ETA.")


def test_evaluate_missing_containers_plural() -> None:
    raw = [item for item in fixture_json("events_missing_container_eta.json")
           if item.get("equipmentEventTypeCode") != "DISC"]
    raw.append({**raw[0], "eventDateTime": "2026-11-02T10:00:00-06:00", "equipmentEventTypeCode": "GTIN",
                "eventLocation": {"UNLocationCode": "USHOU"},
                "carrierSpecificData": {"shipmentLocationType": "POD"},
                "transportCall": {"transportCallSequenceNumber": 2, "UNLocationCode": "USHOU",
                                  "modeOfTransport": "TRUCK"}})
    with pytest.raises(NoEtaError) as info:
        evaluate_events("NAM5678901", raw)
    assert str(info.value) == ("No ETA published by CMA for booking NAM5678901 "
                               "(containers TGHU5000001, TGHU5000002). Can't determine ETA.")


def test_evaluate_no_containers() -> None:
    with pytest.raises(NoEtaError) as info:
        evaluate_events("NAM6789012", fixture_json("events_no_containers.json"))
    assert str(info.value) == (
        "CMA found booking NAM6789012 but listed no containers to read an ETA from. Can't determine ETA.")


def test_evaluate_empty() -> None:
    with pytest.raises(CarrierLookupError) as info:
        evaluate_events("NAM0000001", fixture_json("events_empty.json"))
    assert "'shipment not found'" in str(info.value) and "15 days" in str(info.value)
    assert str(info.value) == MSG_EMPTY.format(ref="NAM0000001")


def test_evaluate_sentinel_and_empty_disc() -> None:
    result = evaluate_events("NAM7890123", fixture_json("events_sentinel_and_empty_disc.json"))
    assert result.eta == datetime(2026, 11, 20, 8, 0)   # PLN; 0001-01-01 EST dropped (R2); EMPTY DISC ignored (R7)
    assert result.is_actual is False
    assert result.source == "CMA ETA Berth at POD (planned vessel arrival) at USHOU"


def test_evaluate_other_booking() -> None:
    with pytest.raises(CarrierLookupError) as info:
        evaluate_events("NAM1111111", fixture_json("events_other_booking.json"))
    assert str(info.value) == "CMA returned tracking for NAM2222222 instead of the requested booking NAM1111111."


def test_evaluate_split_child() -> None:
    result = evaluate_events("NAM3333333", fixture_json("events_split_child.json"))
    assert result.eta == datetime(2026, 11, 12, 6, 0)   # child NAM3333334 has originBookingReference NAM3333333
    assert result.is_actual is False


def test_evaluate_page1_plus_page2_equals_in_transit() -> None:
    pages = fixture_json("events_page1.json") + fixture_json("events_page2.json")
    assert len(fixture_json("events_page1.json")) == 6 and len(fixture_json("events_page2.json")) == 4
    assert pages == fixture_json("events_in_transit.json")
    combined = evaluate_events("NAM1234567", pages, pages=2)
    single = evaluate_events("NAM1234567", fixture_json("events_in_transit.json"))
    assert (combined.eta, combined.source, combined.containers, combined.is_actual) == (
        single.eta, single.source, single.containers, single.is_actual)
    assert combined.details["pages"] == 2


def test_evaluate_all_sentinel_dates_is_no_eta() -> None:
    """R1b (CMA-D6): items returned but every one dropped by R2 → NoEtaError, not 'shipment not found'."""
    raw = [{"eventType": "TRANSPORT", "eventDateTime": "0001-01-01T00:00:00Z"},
           {"eventType": "EQUIPMENT", "eventDateTime": "9999-12-31T23:59:59.9999999"}]
    with pytest.raises(NoEtaError) as info:
        evaluate_events("NAM7777777", raw)
    assert str(info.value) == (
        "CMA returned events for booking NAM7777777 but none carried a usable date. Can't determine ETA.")


def test_evaluate_logs_container_lines(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="eta.carrier.cma")
    evaluate_events("NAM1234567", fixture_json("events_in_transit.json"))
    assert cma_messages(caplog, logging.INFO) == [
        "  [CMA] Container CMAU1234567: found 'ETA Berth at POD (estimated vessel arrival)': 11/10/2026 14:00",
        "  [CMA] Container TGHU7654321: found 'ETA Berth at POD (estimated vessel arrival)': 11/10/2026 14:00",
        "  [CMA] Booking has 2 containers — using the latest ETA.",
    ]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 2–8. pure helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _strip_csd(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{k: v for k, v in item.items() if k != "carrierSpecificData"} for item in raw]


def _pod_event(loc: str, seq: int | None, tag: str = "POD", code: str = "ARRI") -> dict[str, Any]:
    tc: dict[str, Any] = {"UNLocationCode": loc, "modeOfTransport": "VESSEL", "facilityTypeCode": "POTE"}
    if seq is not None:
        tc["transportCallSequenceNumber"] = seq
    return {"eventCreatedDateTime": "2026-10-01T00:00:00Z", "eventType": "TRANSPORT", "eventClassifierCode": "EST",
            "eventDateTime": "2026-11-10T08:00:00-08:00", "transportEventTypeCode": code, "transportCall": tc,
            "carrierSpecificData": {"shipmentLocationType": tag}}


def test_resolve_pod_unlocode_prefers_carrier_specific_pod() -> None:
    raw = fixture_json("events_transshipment.json")
    assert resolve_pod_unlocode(dcsa.parse_events(raw)) == "USNYC"
    stripped = _strip_csd(raw)
    assert resolve_pod_unlocode(dcsa.parse_events(stripped)) is None
    # E6: without CMA's tag the shared DCSA heuristic still finds USNYC (loading ports CNSHA/SGSIN excluded).
    result = evaluate_events("NAM4567890", stripped)
    assert result.eta == datetime(2026, 11, 16, 7, 0) and result.details["pod"] is None
    assert result.source == "CMA ETA Berth at POD (estimated vessel arrival) at USNYC"


def test_resolve_pod_unlocode_tie_breaks() -> None:
    def pod(events: list[dict[str, Any]]) -> str | None:
        return resolve_pod_unlocode(dcsa.parse_events(events))

    # highest transport-call sequence wins, even with fewer events
    assert pod([_pod_event("USLGB", 2), _pod_event("USLGB", 2), _pod_event("USOAK", 3)]) == "USOAK"
    # same sequence → the location with the most events
    assert pod([_pod_event("USOAK", 2), _pod_event("USLGB", 2), _pod_event("USLGB", 2)]) == "USLGB"
    # None sequence counts as -1
    assert pod([_pod_event("USOAK", None), _pod_event("USOAK", None), _pod_event("USLGB", 0)]) == "USLGB"
    # only "POD" counts (PTS = transshipment discharge, POL = loading), tag compared case-insensitively
    assert pod([_pod_event("SGSIN", 9, tag="PTS"), _pod_event("USNYC", 1, tag="pod")]) == "USNYC"
    assert pod([_pod_event("SGSIN", 9, tag="PTS"), _pod_event("CNSHA", 1, tag="POL")]) is None


def _equipment_item(ref: str, code: str = "LOAD", empty: str = "LADEN") -> dict[str, Any]:
    return {"eventCreatedDateTime": "2026-10-01T08:00:00Z", "eventType": "EQUIPMENT", "eventClassifierCode": "ACT",
            "eventDateTime": "2026-10-01T10:00:00+02:00", "equipmentEventTypeCode": code,
            "equipmentReference": ref, "emptyIndicatorCode": empty, "eventLocation": {"UNLocationCode": "DEHAM"}}


def test_container_references_first_seen_order_and_dedupe() -> None:
    assert container_references(dcsa.parse_events(fixture_json("events_in_transit.json"))) == [
        "CMAU1234567", "TGHU7654321"]
    raw = [
        _equipment_item(" tghu1111111 "),
        _pod_event("USLGB", 2),
        _equipment_item("CMAU2222222", code="GTOT", empty="EMPTY"),   # any empty indicator counts (R6)
        _equipment_item("TGHU1111111", code="DISC"),
        _equipment_item("cmau2222222"),
        _equipment_item("TGHU3333333"),
    ]
    assert container_references(dcsa.parse_events(raw)) == ["TGHU1111111", "CMAU2222222", "TGHU3333333"]
    assert container_references(dcsa.parse_events(fixture_json("events_no_containers.json"))) == []


def test_is_empty_equipment() -> None:
    events = dcsa.parse_events([_equipment_item("CMAU1", empty="EMPTY"), _equipment_item("CMAU2", empty="laden"),
                                _equipment_item("CMAU3", empty="empty"), _pod_event("USLGB", 2)])
    assert [is_empty_equipment(e) for e in events] == [True, False, True, False]


def test_prefilter_drops_sentinels_without_parsing(monkeypatch: pytest.MonkeyPatch,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    def no_parse(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("the prefilter must not parse datetimes")

    monkeypatch.setattr(dcsa, "parse_dcsa_datetime", no_parse)
    caplog.set_level(logging.DEBUG, logger="eta.carrier.cma")
    keep = {"eventType": "TRANSPORT", "eventDateTime": "2026-11-20T08:00:00-06:00"}
    raw = [
        {"eventType": "TRANSPORT", "eventDateTime": "0001-01-01T00:00:00Z"},
        {"eventType": "TRANSPORT", "eventDateTime": "9999-12-31T23:59:59.9999999"},
        {"eventType": "TRANSPORT", "eventDateTime": ""},
        {"eventType": "TRANSPORT"},
        "not a dict",
        keep,
    ]
    assert prefilter_raw_events(raw) == [keep]
    assert any("dropped 5 event(s)" in m for m in cma_messages(caplog, logging.DEBUG))
    # boundaries of PLAUSIBLE_YEARS (2000–2100) and leading whitespace
    edge = [{"eventDateTime": "1999-12-31T23:00:00Z"}, {"eventDateTime": "2000-01-01T00:00:00Z"},
            {"eventDateTime": " 2100-12-31T00:00:00Z"}, {"eventDateTime": "2101-01-01T00:00:00Z"},
            {"eventDateTime": None}, {"eventDateTime": 20261120}]
    assert [e["eventDateTime"] for e in prefilter_raw_events(edge)] == [
        "2000-01-01T00:00:00Z", " 2100-12-31T00:00:00Z"]
    assert prefilter_raw_events([]) == []


def _bkg(*values: str, kind: str = "BKG", **csd: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"documentReferences": [
        {"documentReferenceType": kind, "documentReferenceValue": value} for value in values]}
    if csd:
        item["carrierSpecificData"] = csd
    return item


def test_check_booking_identity() -> None:
    # hashed only (API-key access) → skipped, no error
    check_booking_identity("NAM1111111", [_bkg(HASH)])
    check_booking_identity("NAM1111111", fixture_json("events_in_transit.json"))
    # real reference equal → ok (values are compared stripped and upper-cased)
    check_booking_identity("NAM1111111", [_bkg("NAM1111111")])
    check_booking_identity("nam1111111", [_bkg(" nam1111111 ")])
    # real reference different → error
    with pytest.raises(CarrierLookupError) as info:
        check_booking_identity("NAM1111111", [_bkg("NAM2222222")])
    assert str(info.value) == "CMA returned tracking for NAM2222222 instead of the requested booking NAM1111111."
    # different but related via splitToBookingReferences → ok
    check_booking_identity("NAM1111111", [_bkg("NAM2222222", splitToBookingReferences=["nam1111111"])])
    # … or via the other split/merge/part-load relations
    for key in ("originBookingReference", "mergeToBookingReference", "masterPartLoadBookingReference"):
        check_booking_identity("NAM1111111", [_bkg("NAM2222222", **{key: "NAM1111111"})])
    check_booking_identity("NAM1111111", [_bkg("NAM2222222", partLoadGroupBookingReferences=["NAM1111111"])])
    # UUIDs and TRD references never count; several others are listed sorted and comma-joined
    check_booking_identity("NAM1111111", [_bkg("0b6f1f7e-0000-4000-8000-000000000005"), _bkg("X1", kind="TRD")])
    with pytest.raises(CarrierLookupError) as info:
        check_booking_identity("NAM1111111", [_bkg("NAM3333333"), _bkg("NAM2222222", HASH)])
    assert str(info.value) == ("CMA returned tracking for NAM2222222, NAM3333333 instead of the requested "
                               "booking NAM1111111.")


def test_describe_arrival_labels() -> None:
    eta = datetime(2026, 11, 10, 14, 0)

    def arrival(classifier: str, basis: str) -> DcsaArrival:
        return DcsaArrival(eta, classifier, "USLGB", None, None, basis)

    assert describe_arrival(arrival("ACT", "EQUIPMENT DISC ACT")) == "Arrived at POD (container discharged)"
    assert describe_arrival(arrival("ACT", "TRANSPORT ARRI ACT")) == "Arrived at POD (vessel arrival)"
    assert describe_arrival(arrival("EST", "TRANSPORT ARRI EST")) == "ETA Berth at POD (estimated vessel arrival)"
    assert describe_arrival(arrival("PLN", "TRANSPORT ARRI PLN")) == "ETA Berth at POD (planned vessel arrival)"
    assert describe_arrival(arrival("REQ", "TRANSPORT ARRI REQ")) == "REQ arrival at POD"


def test_extract_cursor() -> None:
    assert extract_cursor("fE9mZnNldHw9NSZsaW1pdD01") == "fE9mZnNldHw9NSZsaW1pdD01"
    assert extract_cursor("https://apis.cma-cgm.net/operation/trackandtrace/v1/events?cursor=abc&limit=5") == "abc"
    assert extract_cursor("cursor=abc") == "abc"
    # stripped; relative URLs; percent-decoded; a literal '+' (base64) is kept
    assert extract_cursor("  fE9mZnNldHw9NSZsaW1pdD01 \r\n") == "fE9mZnNldHw9NSZsaW1pdD01"
    assert extract_cursor("/operation/trackandtrace/v1/events?limit=5&cursor=a%2Fb%3D") == "a/b="
    assert extract_cursor("limit=5&cursor=ab+c") == "ab+c"


EQUIVALENCE_CASES = [
    ("events_in_transit.json", "NAM1234567"),
    ("events_arrived.json", "NAM2345678"),
    ("events_partial_discharge.json", "NAM3456789"),
    ("events_transshipment.json", "NAM4567890"),
    ("events_sentinel_and_empty_disc.json", "NAM7890123"),
    ("events_split_child.json", "NAM3333333"),
]


@pytest.mark.parametrize(("name", "ref"), EQUIVALENCE_CASES)
def test_equivalence_with_dcsa_booking_arrival(name: str, ref: str) -> None:
    """E7 cross-check: dcsa.booking_arrival(policy="latest", pod_unlocode=P) equals CMA's §7.3 aggregation."""
    raw = fixture_json(name)
    result = evaluate_events(ref, raw)
    events = dcsa.parse_events(prefilter_raw_events(raw))
    pod = resolve_pod_unlocode(events)
    arrival = dcsa.booking_arrival(events, policy="latest", pod_unlocode=pod)
    assert arrival is not None
    assert arrival.eta == result.eta


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 9–23. HTTP (respx), sleep injected as an async recorder
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_reference_mode_request_shape(cma_settings: Callable[..., CmaSettings], token_store: TokenStore,
                                            caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="eta")
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        async with open_client(cma_settings(), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 1
    sent = route.calls.last.request
    assert sent.method == "GET"
    assert str(sent.url) == "https://apis.cma-cgm.net/operation/trackandtrace/v1/events/NAM1234567?limit=500"
    assert sent.headers.get_list("KeyId") == [API_KEY]
    assert "Authorization" not in sent.headers
    assert sent.headers["Accept"] == "application/json"
    assert result.eta == datetime(2026, 11, 10, 14, 0) and result.details["endpoint"] == "reference"
    assert cma_messages(caplog, logging.INFO) == [
        "  [CMA] Looking up ETA for booking NAM1234567...",
        "  [CMA] responded OK — parsing for ETA...",
        "  [CMA] Container CMAU1234567: found 'ETA Berth at POD (estimated vessel arrival)': 11/10/2026 14:00",
        "  [CMA] Container TGHU7654321: found 'ETA Berth at POD (estimated vessel arrival)': 11/10/2026 14:00",
        "  [CMA] Booking has 2 containers — using the latest ETA.",
        "  [CMA] Returning ETA: 11/10/2026",
    ]
    assert API_KEY not in caplog.text


async def test_dcsa_query_mode_request_shape(cma_settings: Callable[..., CmaSettings],
                                             token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(QUERY_URL).mock(return_value=events_response())
        async with open_client(cma_settings(CMA_LOOKUP_MODE="dcsa_query"), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 1
    sent = route.calls.last.request
    assert str(sent.url) == (
        "https://apis.cma-cgm.net/operation/trackandtrace/v1/events?carrierBookingReference=NAM1234567&limit=500")
    assert sent.headers["KeyId"] == API_KEY
    assert result.details["endpoint"] == "dcsa_query" and result.eta == datetime(2026, 11, 10, 14, 0)


async def test_behalf_of_param_added_when_configured(cma_settings: Callable[..., CmaSettings],
                                                     token_store: TokenStore) -> None:
    settings = cma_settings(CMA_BEHALF_OF="P123456")
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            events_response("events_page1.json", headers={"Next-Page": NEXT_CURSOR}),
            events_response("events_page2.json"),
        ])
        async with open_client(settings, token_store) as client:
            await client.get_eta("NAM1234567")
    assert dict(route.calls[0].request.url.params) == {"limit": "500", "behalfOf": "P123456"}
    assert dict(route.calls[1].request.url.params) == {"cursor": NEXT_CURSOR, "limit": "500", "behalfOf": "P123456"}

    async with respx.mock(assert_all_called=True) as router:
        route = router.get(QUERY_URL).mock(return_value=events_response())
        async with open_client(cma_settings(CMA_BEHALF_OF="P123456", CMA_LOOKUP_MODE="dcsa_query"),
                               token_store) as client:
            await client.get_eta("NAM1234567")
    assert dict(route.calls[0].request.url.params) == {
        "carrierBookingReference": "NAM1234567", "limit": "500", "behalfOf": "P123456"}

    async with respx.mock(assert_all_called=True) as router:   # not configured → never sent
        route = router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        async with open_client(cma_settings(), token_store) as client:
            await client.get_eta("NAM1234567")
    assert "behalfOf" not in route.calls[0].request.url.params


async def test_pagination_follows_next_page(cma_settings: Callable[..., CmaSettings],
                                            token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            events_response("events_page1.json", headers={"Next-Page": NEXT_CURSOR, "Current-Page": "c1"}),
            events_response("events_page2.json", headers={"Current-Page": NEXT_CURSOR}),
        ])
        async with open_client(cma_settings(), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 2
    assert dict(route.calls[0].request.url.params) == {"limit": "500"}
    assert dict(route.calls[1].request.url.params) == {"cursor": NEXT_CURSOR, "limit": "500"}
    expected = evaluate_events("NAM1234567", fixture_json("events_in_transit.json"))
    assert (result.eta, result.source, result.containers, result.is_actual) == (
        expected.eta, expected.source, expected.containers, expected.is_actual)
    assert result.details["pages"] == 2 and result.details["event_count"] == 10


async def test_pagination_next_page_as_url(cma_settings: Callable[..., CmaSettings],
                                           token_store: TokenStore) -> None:
    """UNVERIFIED (c): a Next-Page URL is reduced to its cursor; the request still goes to the booking path."""
    next_url = f"{BASE}/events/NAM1234567?cursor={NEXT_CURSOR}&limit=500"
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            events_response("events_page1.json", headers={"Next-Page": next_url}),
            events_response("events_page2.json"),
        ])
        async with open_client(cma_settings(), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert dict(route.calls[1].request.url.params) == {"cursor": NEXT_CURSOR, "limit": "500"}
    assert result.eta == datetime(2026, 11, 10, 14, 0)


async def test_page_cap_warns_and_uses_collected(cma_settings: Callable[..., CmaSettings], token_store: TokenStore,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.cma")
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(
            side_effect=lambda request: events_response(headers={"Next-Page": NEXT_CURSOR}))
        async with open_client(cma_settings(CMA_MAX_PAGES="2"), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 2
    assert cma_messages(caplog, logging.WARNING) == [
        "[CMA] booking NAM1234567 has more than 2 pages of events — using the first 2"]
    assert result.eta == datetime(2026, 11, 10, 14, 0)
    assert result.details["pages"] == 2 and result.details["event_count"] == 20


async def test_404_on_a_later_page_stops_paging(cma_settings: Callable[..., CmaSettings],
                                                token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            events_response(headers={"Next-Page": NEXT_CURSOR}),
            json_response(404, "error_404.json"),
        ])
        async with open_client(cma_settings(), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 2
    assert result.eta == datetime(2026, 11, 10, 14, 0) and result.details["pages"] == 1


async def test_empty_page_stops_paging(cma_settings: Callable[..., CmaSettings], token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            events_response(headers={"Next-Page": NEXT_CURSOR}),
            httpx.Response(200, json=[], headers={"Next-Page": "another"}),
        ])
        async with open_client(cma_settings(), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 2 and result.eta == datetime(2026, 11, 10, 14, 0)


async def test_404_maps_to_lookup_error_and_is_not_retried(cma_settings: Callable[..., CmaSettings],
                                                           token_store: TokenStore) -> None:
    clock = FakeTime()
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM0000000")).mock(return_value=json_response(404, "error_404.json"))
        async with open_client(cma_settings(), token_store, clock) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM0000000")
    assert route.call_count == 1
    assert str(info.value) == MSG_404.format(ref="NAM0000000")
    assert clock.sleeps == []


async def test_empty_array_maps_to_shipment_not_found(cma_settings: Callable[..., CmaSettings],
                                                      token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM0000001")).mock(return_value=events_response("events_empty.json"))
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM0000001")
    assert route.call_count == 1
    assert str(info.value) == MSG_EMPTY.format(ref="NAM0000001")


async def test_401_apikey_hint(cma_settings: Callable[..., CmaSettings], token_store: TokenStore,
                               caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="eta")
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(return_value=json_response(
            401, "error_401.json", headers={"x-request-id": "30e5da4bfc32f503d25b175d57848b45"}))
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert str(info.value) == EXACT_HINT_401 == HINT_401
    assert route.call_count == 2   # request_with_auth retries a 401 once (harmless, 08 §5)
    assert API_KEY not in caplog.text


async def test_403_hint(cma_settings: Callable[..., CmaSettings], token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(return_value=httpx.Response(
            403, json={"message": "You cannot consume this service"}))
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert str(info.value) == EXACT_HINT_403 == HINT_403
    assert route.call_count == 1   # 403 is not "token expired": no refresh, no retry


async def test_400_includes_dcsa_reason_and_message(cma_settings: Callable[..., CmaSettings],
                                                    token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(QUERY_URL).mock(return_value=json_response(400, "error_400.json"))
        async with open_client(cma_settings(CMA_LOOKUP_MODE="dcsa_query"), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert route.call_count == 1
    assert str(info.value) == (
        "CMA tracking API rejected the request for NAM1234567 (HTTP 400): invalidQuery: The request did not "
        "contain one of the three required query parameters.")


async def test_4xx_without_dcsa_body_falls_back_to_service_error_text(cma_settings: Callable[..., CmaSettings],
                                                                      token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM1234567")).mock(return_value=httpx.Response(
            422, text="unprocessable", headers={"x-kong-request-id": "kong-1"}))
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    text = str(info.value)
    assert text.startswith("CMA tracking API rejected the request for NAM1234567 (HTTP 422): cma: unexpected HTTP 422")
    assert "unprocessable" in text and "kong-1" in text


async def test_429_honours_retry_after(cma_settings: Callable[..., CmaSettings], token_store: TokenStore,
                                       caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.cma")
    clock = FakeTime()
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            json_response(429, "error_429.json", headers={"Retry-After": "2"}),
            events_response(),
        ])
        async with open_client(cma_settings(), token_store, clock) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 2
    assert any(s >= 2.5 for s in clock.sleeps)   # 2 s plus the 0.5 s margin
    assert result.eta == datetime(2026, 11, 10, 14, 0)
    assert cma_messages(caplog, logging.WARNING) == [
        "[CMA] rate limited (HTTP 429) — waiting 2s as instructed by Retry-After"]


async def test_429_over_cap_fails_fast(cma_settings: Callable[..., CmaSettings], token_store: TokenStore) -> None:
    clock = FakeTime()
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(
            return_value=json_response(429, "error_429.json", headers={"Retry-After": "999"}))
        async with open_client(cma_settings(), token_store, clock) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert route.call_count == 1
    assert not any(s >= 999 for s in clock.sleeps)
    assert str(info.value) == (
        "CMA rate limit reached (HTTP 429, retry after 999s; CMA_MAX_RETRY_AFTER_S=120). Try again later or ask "
        "CMA to raise your application's quota.")


async def test_429_waits_at_most_three_times(cma_settings: Callable[..., CmaSettings],
                                             token_store: TokenStore) -> None:
    clock = FakeTime()
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(
            return_value=json_response(429, "error_429.json", headers={"Retry-After": "1"}))
        async with open_client(cma_settings(), token_store, clock) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert route.call_count == 4   # MAX_RATE_LIMIT_WAITS = 3 waits, then the 4th 429 fails
    assert clock.sleeps == [1.5, 1.5, 1.5]
    assert str(info.value).startswith("CMA rate limit reached (HTTP 429, retry after 1s; CMA_MAX_RETRY_AFTER_S=120)")


async def test_429_without_retry_after_waits_the_default(cma_settings: Callable[..., CmaSettings],
                                                         token_store: TokenStore) -> None:
    clock = FakeTime()
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            json_response(429, "error_429.json"), events_response()])
        async with open_client(cma_settings(), token_store, clock) as client:
            await client.get_eta("NAM1234567")
    assert route.call_count == 2 and clock.sleeps == [60.5]   # DEFAULT_RETRY_AFTER_S + 0.5


def test_parse_retry_after_forms() -> None:
    parse = cma._parse_retry_after
    assert parse("2", default=60.0) == 2.0
    assert parse("1.5", default=60.0) == 1.5
    assert parse("-3", default=60.0) == 0.0
    assert parse(None, default=60.0) == 60.0
    assert parse("soon", default=60.0) == 60.0
    http_date = email.utils.formatdate(time.time() + 100, usegmt=True)
    assert 97.0 <= parse(http_date, default=60.0) <= 100.0
    past = email.utils.formatdate(time.time() - 100, usegmt=True)
    assert parse(past, default=60.0) == 0.0


async def test_429_blocks_concurrent_calls(cma_settings: Callable[..., CmaSettings],
                                           token_store: TokenStore) -> None:
    """Two concurrent bookings: A gets a 429 (Retry-After 2). While A waits, B starts; B's attempt must wait at
    the shared gate until A's 429 + 2.5 s (08 §6.5: CMA's quota is per application). Real asyncio ordering at
    1/50 speed (ScaledTime); B is started once A is waiting, so the interleaving is deterministic."""
    clock = ScaledTime(scale=50.0)
    a_calls: list[float] = []
    b_calls: list[float] = []
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM1111111")).mock(side_effect=stamped([
            json_response(429, "error_429.json", headers={"Retry-After": "2"}), events_response()], a_calls, clock))
        router.get(ref_url("NAM2222222")).mock(side_effect=stamped([events_response()], b_calls, clock))
        async with open_client(cma_settings(), token_store, clock) as client:
            task_a = asyncio.create_task(client.get_eta("NAM1111111"))
            while not clock.sleeps:              # A got its 429 and is now waiting at the gate
                await asyncio.sleep(0.001)
            result_b = await client.get_eta("NAM2222222")
            result_a = await task_a
    assert result_a.eta == result_b.eta == datetime(2026, 11, 10, 14, 0)
    assert len(a_calls) == 2 and len(b_calls) == 1
    gate_opens = a_calls[0] + 2.5
    assert b_calls[0] >= gate_opens - 1e-3       # B's attempt happened only after the gate opened
    assert a_calls[1] >= gate_opens - 1e-3
    assert len(clock.sleeps) == 2                # A's wait, then B's wait for the rest of the same gate
    assert 2.0 < clock.sleeps[0] <= 2.5 and 0.0 < clock.sleeps[1] <= clock.sleeps[0]


def with_latency(responses: Iterable[httpx.Response], stamps: list[float], clock: ScaledTime,
                 latency_s: float = 0.2) -> Callable[[httpx.Request], Any]:
    """respx side effect with a network round trip of `latency_s` virtual seconds (a real HTTP call yields to the
    event loop; a plain respx answer does not). Records the virtual time each request leaves the client."""
    queue = list(responses)

    async def side_effect(request: httpx.Request) -> httpx.Response:
        stamps.append(clock.monotonic())
        await asyncio.sleep(latency_s / clock.scale)
        return queue.pop(0)

    return side_effect


# ── review fix CRA-02: calls queued at the semaphore or in the limiter re-check the gate ──
async def test_429_holds_back_a_call_queued_at_the_semaphore(cma_settings: Callable[..., CmaSettings],
                                                             token_store: TokenStore) -> None:
    """CRA-02 (08 §4.6 'Never retry earlier', §6.5): with one CMA slot, B waits at the semaphore while A is in
    flight. A's 429 closes the gate, so B, once it has the slot, waits for the gate instead of sending at once."""
    clock = ScaledTime(scale=50.0)
    a_calls: list[float] = []
    b_calls: list[float] = []
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM1111111")).mock(side_effect=with_latency([
            json_response(429, "error_429.json", headers={"Retry-After": "2"}), events_response()], a_calls, clock))
        router.get(ref_url("NAM2222222")).mock(side_effect=with_latency([events_response()], b_calls, clock))
        async with open_client(cma_settings(CMA_MAX_CONCURRENCY="1"), token_store, clock) as client:
            await asyncio.gather(client.get_eta("NAM1111111"), client.get_eta("NAM2222222"))
    gate_opens = a_calls[0] + 0.2 + 2.5          # A's 429 arrives after the round trip; 2 s + 0.5 s margin
    assert b_calls[0] >= gate_opens - 1e-3
    assert a_calls[1] >= gate_opens - 1e-3


async def test_429_holds_back_every_queued_call_with_the_default_slots(cma_settings: Callable[..., CmaSettings],
                                                                       token_store: TokenStore) -> None:
    """CRA-02 with CMA_MAX_CONCURRENCY=2 and three bookings: C is queued behind A and B and must not be sent inside
    the window A's 429 announced."""
    clock = ScaledTime(scale=50.0)
    calls: dict[str, list[float]] = {"A": [], "B": [], "C": []}
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM1111111")).mock(side_effect=with_latency([
            json_response(429, "error_429.json", headers={"Retry-After": "2"}), events_response()], calls["A"], clock))
        router.get(ref_url("NAM2222222")).mock(side_effect=with_latency([events_response()], calls["B"], clock, 1.0))
        router.get(ref_url("NAM3333333")).mock(side_effect=with_latency([events_response()], calls["C"], clock))
        async with open_client(cma_settings(), token_store, clock) as client:
            await asyncio.gather(*(client.get_eta(b) for b in ("NAM1111111", "NAM2222222", "NAM3333333")))
    assert calls["C"][0] >= calls["A"][0] + 0.2 + 2.5 - 1e-3


async def test_429_holds_back_a_call_waiting_in_the_proactive_limiter(cma_settings: Callable[..., CmaSettings],
                                                                      token_store: TokenStore) -> None:
    """CRA-02 with CMA_MAX_CALLS_PER_MINUTE=1: B waits in the limiter until t≈60 while A's 429 (Retry-After 100)
    closes the gate until t≈100.7; B must not be sent before the gate opens."""
    clock = ScaledTime(scale=400.0)
    calls: dict[str, list[float]] = {"A": [], "B": []}
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM1111111")).mock(side_effect=with_latency([
            json_response(429, "error_429.json", headers={"Retry-After": "100"}), events_response()], calls["A"],
            clock))
        router.get(ref_url("NAM2222222")).mock(side_effect=with_latency([events_response()], calls["B"], clock))
        async with open_client(cma_settings(CMA_MAX_CALLS_PER_MINUTE="1"), token_store, clock) as client:
            await asyncio.gather(client.get_eta("NAM1111111"), client.get_eta("NAM2222222"))
    gate_opens = calls["A"][0] + 0.2 + 100.5
    assert calls["B"][0] >= gate_opens - 0.5 and calls["A"][1] >= gate_opens - 0.5


async def test_every_request_still_takes_a_limiter_slot(cma_settings: Callable[..., CmaSettings],
                                                        token_store: TokenStore) -> None:
    """CRA-02: _send takes the first limiter slot itself (to re-check the gate after the wait); the retry after a 401
    still takes its own, so CMA_MAX_CALLS_PER_MINUTE counts every request."""
    settings = cma_settings(CMA_API_KEY=None, CMA_CLIENT_ID=CLIENT_ID, CMA_CLIENT_SECRET=CLIENT_SECRET,
                            CMA_MAX_CALLS_PER_MINUTE="10")
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(side_effect=[token_response("tok-1"), token_response("tok-2")])
        get_route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            json_response(401, "error_401.json"), events_response()])
        async with open_client(settings, token_store) as client:
            await client.get_eta("NAM1234567")
            limiter = client._limiter
            assert limiter is not None and limiter.acquired == get_route.call_count == 2


# ── review fix CRA-03: a 429 over the cap closes the gate too ──
async def test_429_over_cap_closes_the_gate_for_later_lookups(cma_settings: Callable[..., CmaSettings],
                                                              token_store: TokenStore) -> None:
    """CRA-03 (08 §4.6, §6.5; 10 §6): a 429 announcing more than CMA_MAX_RETRY_AFTER_S fails its lookup AND closes
    the shared gate. Later lookups fail at once with the very same text (so the circuit breaker can trip) and send
    nothing; once the rest of the window fits the cap they wait it out, and after it they call CMA again."""
    clock = FakeTime()
    async with respx.mock(assert_all_called=False) as router:
        a = router.get(ref_url("NAM1111111")).mock(
            return_value=json_response(429, "error_429.json", headers={"Retry-After": "300"}))
        later = {ref: router.get(ref_url(ref)).mock(return_value=events_response())
                 for ref in ("NAM2222222", "NAM3333333", "NAM4444444", "NAM5555555")}
        async with open_client(cma_settings(), token_store, clock) as client:
            texts = []
            for ref in ("NAM1111111", "NAM2222222", "NAM3333333"):
                with pytest.raises(CarrierLookupError) as info:
                    await client.get_eta(ref)
                texts.append(str(info.value))
            assert later["NAM2222222"].call_count == 0 and later["NAM3333333"].call_count == 0
            assert clock.sleeps == []
            clock.now += 200.0                        # 100.5 s of the window left: within the 120 s cap → wait
            await client.get_eta("NAM4444444")
            assert later["NAM4444444"].call_count == 1 and clock.sleeps == [pytest.approx(100.5)]
            await client.get_eta("NAM5555555")         # after the window: sent at once
    assert a.call_count == 1 and later["NAM5555555"].call_count == 1 and len(clock.sleeps) == 1
    assert texts == [("CMA rate limit reached (HTTP 429, retry after 300s; CMA_MAX_RETRY_AFTER_S=120). Try again "
                      "later or ask CMA to raise your application's quota.")] * 3


async def test_5xx_retried_then_success(cma_settings: Callable[..., CmaSettings], token_store: TokenStore) -> None:
    clock = FakeTime()
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            httpx.Response(503, text="unavailable"), httpx.Response(502, text="bad gateway"), events_response()])
        async with open_client(cma_settings(), token_store, clock) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 3
    assert result.eta == datetime(2026, 11, 10, 14, 0)
    assert len(clock.sleeps) == 2
    assert 1.0 <= clock.sleeps[0] <= 1.25 and 2.0 <= clock.sleeps[1] <= 2.25

    clock = FakeTime()
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(return_value=httpx.Response(
            500, text="boom", headers={"x-request-id": "rid-500"}))
        async with open_client(cma_settings(), token_store, clock) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert route.call_count == 3   # MAX_TRANSIENT_ATTEMPTS
    text = str(info.value)
    assert text.startswith("Failed to get CMA tracking info for NAM1234567: cma: unexpected HTTP 500 for GET ")
    assert "rid-500" in text


async def test_network_error_retried(cma_settings: Callable[..., CmaSettings], token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            httpx.ConnectError("connection refused"), events_response()])
        async with open_client(cma_settings(), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert route.call_count == 2
    assert result.eta == datetime(2026, 11, 10, 14, 0)

    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(side_effect=httpx.ConnectError("connection refused"))
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert route.call_count == 3
    assert str(info.value).startswith(
        "Failed to get CMA tracking info for NAM1234567: cma: network error: ConnectError")


async def test_non_json_body_maps_to_could_not_read(cma_settings: Callable[..., CmaSettings],
                                                    token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(return_value=httpx.Response(
            200, text="<html>maintenance</html>", headers={"content-type": "text/html"}))
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert route.call_count == 1
    assert str(info.value).startswith(
        "Could not read CMA tracking data for NAM1234567: cma: response was not valid JSON")

    async with respx.mock(assert_all_called=True) as router:   # JSON but not an array of events
        router.get(ref_url("NAM1234567")).mock(return_value=httpx.Response(200, json={"message": "x", "total": 0}))
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert str(info.value) == ("Could not read CMA tracking data for NAM1234567: expected a JSON array of events, "
                               "got dict with keys ['message', 'total']")


async def test_wrapped_event_list_is_accepted(cma_settings: Callable[..., CmaSettings],
                                              token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM1234567")).mock(return_value=httpx.Response(
            200, json={"events": fixture_json("events_in_transit.json")}))
        async with open_client(cma_settings(), token_store) as client:
            result = await client.get_eta("NAM1234567")
    assert result.eta == datetime(2026, 11, 10, 14, 0)


async def test_booking_normalisation(cma_settings: Callable[..., CmaSettings], token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        async with open_client(cma_settings(), token_store) as client:
            result = await client.get_eta("  nam1234567 ")
            assert result.booking == "NAM1234567"
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("   ")
            assert str(info.value) == "No CMA booking number given."
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM 1234567")
            assert str(info.value) == "'NAM 1234567' is not a valid CMA booking reference."
    assert route.call_count == 1


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 24–26. auth
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_oauth_token_fetched_once_for_concurrent_lookups(oauth_settings: CmaSettings,
                                                               token_store: TokenStore) -> None:
    bookings = ["NAM1234567", "NAM1234568", "NAM1234569"]
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        routes = [router.get(ref_url(b)).mock(return_value=events_response()) for b in bookings]
        async with open_client(oauth_settings, token_store) as client:
            results = await asyncio.gather(*(client.get_eta(b) for b in bookings))
    assert [r.booking for r in results] == bookings
    assert token_route.call_count == 1
    form = parse_qs(token_route.calls.last.request.content.decode())
    assert form == {"grant_type": ["client_credentials"], "client_id": [CLIENT_ID],
                    "client_secret": [CLIENT_SECRET], "scope": [SCOPE]}
    assert token_route.calls.last.request.headers["content-type"] == "application/x-www-form-urlencoded"
    for route in routes:
        sent = route.calls.last.request
        assert sent.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
        assert "KeyId" not in sent.headers
    entry = token_store.get("cma")
    assert entry is not None and set(entry) == {"access_token", "expires_at", "token_type", "scope"}
    assert entry["access_token"] == ACCESS_TOKEN and entry["token_type"] == "Bearer" and entry["scope"] == SCOPE
    assert abs(entry["expires_at"] - (time.time() + 299)) < 5


async def test_oauth_401_refreshes_once(oauth_settings: CmaSettings, tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "a" / "tokens.json")
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(side_effect=[token_response("tok-1"), token_response("tok-2")])
        get_route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            json_response(401, "error_401.json"), events_response()])
        async with open_client(oauth_settings, store) as client:
            result = await client.get_eta("NAM1234567")
    assert result.eta == datetime(2026, 11, 10, 14, 0)
    assert token_route.call_count == 2 and get_route.call_count == 2
    assert [c.request.headers["Authorization"] for c in get_route.calls] == ["Bearer tok-1", "Bearer tok-2"]

    store = TokenStore(tmp_path / "b" / "tokens.json")
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(side_effect=[token_response("tok-1"), token_response("tok-2")])
        get_route = router.get(ref_url("NAM1234567")).mock(return_value=json_response(401, "error_401.json"))
        async with open_client(oauth_settings, store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert str(info.value) == HINT_401
    assert token_route.call_count == 2 and get_route.call_count == 2


# ── review fix CRA-05: tokens.json that cannot be written ──
async def test_oauth_token_kept_in_memory_when_tokens_json_cannot_be_written(
        oauth_settings: CmaSettings, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """CRA-05 (03 §5 boundary rule): a token that cannot be saved is kept for this process — two bookings, one token
    POST, one WARNING (never the token) — instead of an OSError escaping get_eta()."""
    caplog.set_level(logging.WARNING, logger="eta")
    store = UnwritableTokenStore(tmp_path / "tokens.json")
    bookings = ["NAM1234567", "NAM1234568"]
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        for b in bookings:
            router.get(ref_url(b)).mock(return_value=events_response())
        async with open_client(oauth_settings, store) as client:
            results = [await client.get_eta(b) for b in bookings]
    assert [r.booking for r in results] == bookings and token_route.call_count == 1
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "tokens.json" in r.getMessage()]
    assert len(warned) == 1 and ACCESS_TOKEN not in warned[0]


async def test_oauth_rejected_token_that_cannot_be_removed_is_not_reused(oauth_settings: CmaSettings,
                                                                         tmp_path: Path) -> None:
    """CRA-05: after a 401 the rejected token cannot be deleted from tokens.json; it is not read back, so the retry
    uses a new token."""
    path = tmp_path / "tokens.json"
    TokenStore(path).put("cma", {"access_token": "rejected-cma-token", "expires_at": time.time() + 3000,
                                 "token_type": "Bearer", "scope": SCOPE})
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response("tok-new"))
        get_route = router.get(ref_url("NAM1234567")).mock(side_effect=[
            json_response(401, "error_401.json"), events_response()])
        async with open_client(oauth_settings, UnwritableTokenStore(path)) as client:
            result = await client.get_eta("NAM1234567")
    assert result.eta == datetime(2026, 11, 10, 14, 0) and token_route.call_count == 1
    assert [c.request.headers["Authorization"] for c in get_route.calls] == ["Bearer rejected-cma-token",
                                                                             "Bearer tok-new"]


# ── review fix CRA-04: OAuth token failures name no booking, so the circuit breaker can trip ──
async def test_rejected_oauth_credentials_trip_the_circuit_breaker(oauth_settings: CmaSettings,
                                                                   token_store: TokenStore) -> None:
    """CRA-04 (10 §6: rejected credentials give the same text for every booking): a token endpoint answering 401
    invalid_client fails every CMA lookup with one text, so after ETA_CARRIER_FAILURE_LIMIT rows the breaker stops
    the rest without calling the token endpoint again."""
    clock = FakeTime()
    http = httpx.AsyncClient()
    client = CmaClient(oauth_settings, token_store, http=http, sleep=clock.sleep, monotonic=clock.monotonic)
    router = CarrierRouter(providers={Carrier.CMA: "native"}, native={Carrier.CMA: client}, failure_limit=5)
    errors: list[str] = []
    try:
        async with respx.mock(assert_all_called=False) as mock:
            token = mock.post(TOKEN_URL).mock(return_value=httpx.Response(
                401, json={"error": "invalid_client", "error_description": "Bad client credentials"}))
            for i in range(8):
                _carrier, ref, lookup = router.route(f"NAM100000{i}")
                with pytest.raises(CarrierLookupError) as info:
                    await lookup.get_eta(ref)
                errors.append(str(info.value))
    finally:
        await router.aclose()
        await http.aclose()
    assert token.call_count == 5
    assert errors[:5] == ["cma: OAuth token request rejected: invalid_client: Bad client credentials"] * 5
    assert all("lookups aborted after 5 identical consecutive failures" in e for e in errors[5:])
    assert CLIENT_SECRET not in " ".join(errors)


async def test_cached_fresh_token_reused_and_skew_60s(oauth_settings: CmaSettings, token_store: TokenStore) -> None:
    now = 1_800_000_000.0
    async with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response("new-token-value"))
        async with httpx.AsyncClient() as http:
            token_store.put("cma", {"access_token": "cached-token-61", "expires_at": now + 61,
                                    "token_type": "Bearer", "scope": SCOPE})
            auth = CmaOAuth(oauth_settings, token_store, http, wall_time=lambda: now)
            assert await auth.get_token() == "cached-token-61"
            assert token_route.call_count == 0

            token_store.put("cma", {"access_token": "cached-token-59", "expires_at": now + 59,
                                    "token_type": "Bearer", "scope": SCOPE})
            auth = CmaOAuth(oauth_settings, token_store, http, wall_time=lambda: now)
            assert await auth.get_token() == "new-token-value"
            assert token_route.call_count == 1
            assert token_store.get("cma")["expires_at"] == now + 299
            assert auth.cached_entry() == {"expires_at": now + 299, "token_type": "Bearer", "scope": SCOPE}

            await auth.invalidate()   # 08 §5: from the store and from memory
            assert token_store.get("cma") is None and auth.cached_entry() is None


async def test_oauth_token_lifetime_fallbacks(oauth_settings: CmaSettings, token_store: TokenStore) -> None:
    now = 1_800_000_000.0
    async with respx.mock(assert_all_called=True) as router:
        route = router.post(TOKEN_URL).mock(side_effect=[
            httpx.Response(200, json={"access_token": "opaque-token-1", "token_type": "Bearer"}),
            httpx.Response(200, json={"access_token": "opaque-token-2", "expires_in": "soon"}),
        ])
        async with httpx.AsyncClient() as http:
            auth = CmaOAuth(oauth_settings, token_store, http, wall_time=lambda: now)
            await auth.get_token()
            assert token_store.get("cma")["expires_at"] == now + 299   # no expires_in, no JWT exp → 299 s
            await auth.invalidate()
            await auth.get_token()
            assert token_store.get("cma")["expires_at"] == now + 299
    assert route.call_count == 2


async def test_oauth_token_errors_map_through_the_boundary_rule(oauth_settings: CmaSettings,
                                                                token_store: TokenStore,
                                                                http_sleeps: list[float]) -> None:
    async with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=httpx.Response(
            401, json={"error": "invalid_client", "error_description": "Client authentication failed"}))
        get_route = router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        async with open_client(oauth_settings, token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    # CRA-04: the token endpoint's reason, without the booking (one text for every booking, 10 §6)
    assert str(info.value) == "cma: OAuth token request rejected: invalid_client: Client authentication failed"
    assert token_route.call_count == 1 and get_route.call_count == 0
    assert CLIENT_SECRET not in str(info.value)

    async with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=httpx.Response(503, text="down"))
        router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        async with open_client(oauth_settings, token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert str(info.value).startswith("cma: OAuth token request failed: cma: unexpected HTTP 503")
    assert "NAM1234567" not in str(info.value)
    assert token_route.call_count == 4   # the default 03 §6 retries apply to the token call (08 §5)

    async with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(400, text="not json"))
        async with open_client(oauth_settings, token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert str(info.value).endswith(
        "cma: OAuth token request rejected: HTTP 400 (no OAuth error details in the response)")


async def test_api_key_auth() -> None:
    auth = CmaApiKeyAuth(f"  {API_KEY} ")
    assert await auth.get_token() == API_KEY
    await auth.invalidate()   # no-op
    assert await auth.get_token() == API_KEY
    assert auth.build_headers(API_KEY) == {"KeyId": API_KEY}
    assert API_KEY not in repr(auth)
    with pytest.raises(ConfigError):
        CmaApiKeyAuth("  ")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 27–30. client lifecycle
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def test_single_flight_memo(cma_settings: Callable[..., CmaSettings], token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        ok_route = router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        missing_route = router.get(ref_url("NAM7654321")).mock(return_value=json_response(404, "error_404.json"))
        async with open_client(cma_settings(), token_store) as client:
            first, second = await asyncio.gather(client.get_eta("NAM1234567"), client.get_eta("nam1234567 "))
            later = await client.get_eta("NAM1234567")
            assert first is second is later
            errors = []
            for _ in range(2):   # a failure is memoised too (CMA-D4: calls are billable)
                with pytest.raises(CarrierLookupError) as info:
                    await client.get_eta("NAM7654321")
                errors.append(str(info.value))
    assert ok_route.call_count == 1
    assert missing_route.call_count == 1
    assert errors == [MSG_404.format(ref="NAM7654321")] * 2


async def test_memo_survives_caller_cancellation(cma_settings: Callable[..., CmaSettings],
                                                 token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        async with open_client(cma_settings(), token_store) as client:
            cancelled = asyncio.create_task(client.get_eta("NAM1234567"))
            survivor = asyncio.create_task(client.get_eta("NAM1234567"))
            await asyncio.sleep(0)
            cancelled.cancel()
            result = await survivor
            with pytest.raises(asyncio.CancelledError):
                await cancelled
    assert route.call_count == 1 and result.eta == datetime(2026, 11, 10, 14, 0)


async def test_boundary_rule(cma_settings: Callable[..., CmaSettings], token_store: TokenStore,
                             monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_parser(payload: Any) -> list[Any]:
        raise TypeError("unexpected event layout")

    monkeypatch.setattr(dcsa, "parse_events", broken_parser)
    with pytest.raises(CarrierLookupError) as info:
        evaluate_events("NAM1234567", fixture_json("events_in_transit.json"))
    assert str(info.value) == ("Could not read CMA tracking data for NAM1234567: "
                               "TypeError('unexpected event layout')")
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert not isinstance(info.value, (TypeError, NoEtaError))
    assert isinstance(info.value.__cause__, TypeError)
    assert str(info.value) == ("Could not read CMA tracking data for NAM1234567: "
                               "TypeError('unexpected event layout')")


async def test_boundary_rule_wraps_unexpected_parsing_errors(cma_settings: Callable[..., CmaSettings],
                                                             token_store: TokenStore,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_evaluation(*args: Any, **kwargs: Any) -> EtaResult:
        raise KeyError("eventDateTime")

    monkeypatch.setattr(cma, "evaluate_events", broken_evaluation)
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        async with open_client(cma_settings(), token_store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("NAM1234567")
    assert str(info.value) == "Failed to get CMA tracking info for NAM1234567: 'eventDateTime'"
    assert isinstance(info.value.__cause__, KeyError)


async def test_build_client_none_when_unconfigured(make_settings: Callable[..., Any], token_store: TokenStore) -> None:
    settings = make_settings()
    assert settings.cma is None
    assert build_client(settings, token_store) is None
    assert settings.missing["cma"] == ("CMA_API_KEY", "CMA_CLIENT_ID", "CMA_CLIENT_SECRET")

    configured = make_settings({"CMA_API_KEY": API_KEY})
    async with httpx.AsyncClient() as http:
        client = build_client(configured, token_store, http=http)
        assert isinstance(client, CmaClient) and isinstance(client, CarrierClient)
        assert client.carrier is Carrier.CMA and isinstance(client.auth, CmaApiKeyAuth)
        await client.aclose()
        assert not http.is_closed   # never closes an injected client
    own = build_client(configured, token_store)
    assert own is not None
    await own.aclose()
    assert own._http.is_closed   # closes the client it created


def test_settings_resolution(make_settings: Callable[..., Any], token_store: TokenStore) -> None:
    oauth = make_settings({"CMA_CLIENT_ID": CLIENT_ID, "CMA_CLIENT_SECRET": CLIENT_SECRET, "CMA_API_KEY": API_KEY})
    assert oauth.cma is not None and oauth.cma.auth_mode == "oauth"
    client = CmaClient(oauth.cma, token_store, http=httpx.AsyncClient())
    assert isinstance(client.auth, CmaOAuth)
    apikey = make_settings({"CMA_API_KEY": API_KEY})
    assert apikey.cma is not None and apikey.cma.auth_mode == "apikey"
    forced = make_settings({"CMA_AUTH_MODE": "apikey", "CMA_CLIENT_ID": CLIENT_ID,
                            "CMA_CLIENT_SECRET": CLIENT_SECRET})
    assert forced.cma is None and forced.missing["cma"] == ("CMA_API_KEY",)
    assert build_client(forced, token_store) is None
    with pytest.raises(ConfigError):
        make_settings({"CMA_API_KEY": API_KEY, "CMA_LOOKUP_MODE": "search"})
    with pytest.raises(ConfigError):
        make_settings({"CMA_API_KEY": API_KEY, "CMA_PAGE_LIMIT": "501"})


async def test_probe_calls_both_endpoints_and_redacts(cma_settings: Callable[..., CmaSettings],
                                                      token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        ref_route = router.get(ref_url("NAM1234567")).mock(return_value=events_response(
            headers={"x-request-id": "req-ref", "cache-control": "max-age=0, no-cache, no-store"}))
        query_route = router.get(QUERY_URL).mock(return_value=events_response(headers={"x-request-id": "req-q"}))
        async with open_client(cma_settings(), token_store) as client:
            await client.get_eta("NAM1234567")
            out = await client.probe("NAM1234567")   # bypasses the memo
    assert ref_route.call_count == 2 and query_route.call_count == 1   # the probe made 2 GETs
    dumped = json.dumps(out)   # JSON-serializable
    assert API_KEY not in dumped
    assert out["info"] == {"auth_mode": "apikey", "base_url": BASE, "lookup_mode": "reference",
                           "page_limit": 500, "behalf_of_set": False}
    assert out["error"] is None
    assert out["eta"]["eta"] == "2026-11-10T14:00:00" and out["eta"]["details"]["endpoint"] == "reference"
    analysis = out["analysis"]
    assert analysis["reference"]["pod"] == "USLGB"
    assert analysis["endpoints_agree"] is True
    ref_analysis = analysis["reference"]
    assert ref_analysis["containers"] == ["CMAU1234567", "TGHU7654321"]
    assert ref_analysis["counts"]["TRANSPORT ARRI EST"] == 2
    assert ref_analysis["counts"]["EQUIPMENT GTOT ACT EMPTY"] == 2
    assert "POD" in ref_analysis["shipment_location_types"]
    assert ref_analysis["carrier_specific_data_ratio"] == 1.0
    assert ref_analysis["booking_refs_seen"] == [HASH]
    assert ref_analysis["identity_check"].startswith("skipped")
    assert ref_analysis["per_container"]["TGHU7654321"]["label"] == "ETA Berth at POD (estimated vessel arrival)"
    assert any("USLGB" in line for line in ref_analysis["explain"])
    assert any("2026-11-10T14:00:00" in line for line in ref_analysis["explain"])
    assert analysis["dcsa_query"]["eta"]["details"]["endpoint"] == "dcsa_query"
    assert len(out["raw"]["reference"]) == 10 and len(out["raw"]["dcsa_query"]) == 10
    assert [r["endpoint"] for r in out["requests"]] == ["reference", "dcsa_query"]
    assert [r["request_id"] for r in out["requests"]] == ["req-ref", "req-q"]
    assert out["requests"][0]["url"] == f"{BASE}/events/NAM1234567?limit=500"
    assert out["requests"][0]["cache_control"] == "max-age=0, no-cache, no-store"
    assert out["requests"][0]["status"] == 200 and out["requests"][0]["next_page"] is False


async def test_probe_oauth_reports_token_and_never_the_secret(oauth_settings: CmaSettings,
                                                              token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(ref_url("NAM1234567")).mock(return_value=events_response())
        router.get(QUERY_URL).mock(return_value=events_response())
        async with open_client(oauth_settings, token_store) as client:
            out = await client.probe("NAM1234567")
    assert token_route.call_count == 1
    token = out["info"]["token"]
    assert token["token_ok"] is True and token["scope"] == SCOPE and 290 <= token["expires_in"] <= 299
    dumped = json.dumps(out)
    assert ACCESS_TOKEN not in dumped and CLIENT_SECRET not in dumped
    assert out["analysis"]["endpoints_agree"] is True


async def test_probe_records_endpoint_errors_as_strings(cma_settings: Callable[..., CmaSettings],
                                                        token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.get(QUERY_URL).mock(return_value=events_response())
        router.get(ref_url("NAM1234567")).mock(return_value=json_response(400, "error_400.json"))
        async with open_client(cma_settings(CMA_LOOKUP_MODE="dcsa_query"), token_store) as client:
            out = await client.probe("NAM1234567")
    assert [r["endpoint"] for r in out["requests"]] == ["dcsa_query", "reference"]
    assert out["error"] is None and out["eta"]["details"]["endpoint"] == "dcsa_query"
    assert out["analysis"]["reference"] == {"error": (
        "CarrierLookupError: CMA tracking API rejected the request for NAM1234567 (HTTP 400): invalidQuery: The "
        "request did not contain one of the three required query parameters.")}
    assert out["analysis"]["endpoints_agree"] is False
    assert out["raw"]["reference"] is None


async def test_probe_configured_endpoint_failure_sets_error(cma_settings: Callable[..., CmaSettings],
                                                            token_store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.get(ref_url("NAM0000000")).mock(return_value=json_response(404, "error_404.json"))
        router.get(QUERY_URL).mock(return_value=httpx.Response(200, json=[]))
        async with open_client(cma_settings(), token_store) as client:
            out = await client.probe("NAM0000000")
            invalid = await client.probe("NAM-1")
    assert out["error"] == MSG_404.format(ref="NAM0000000")
    assert out["eta"] == "CarrierLookupError: " + MSG_404.format(ref="NAM0000000")
    assert out["analysis"]["dcsa_query"]["eta"] == "CarrierLookupError: " + MSG_EMPTY.format(ref="NAM0000000")
    assert out["analysis"]["endpoints_agree"] is False
    assert invalid["error"] == "'NAM-1' is not a valid CMA booking reference." and invalid["requests"] == []
