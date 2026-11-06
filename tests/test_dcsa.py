"""Tests for the shared DCSA Track & Trace parser (09 §A.6), plus the fixture table of 09 §A.5.

Pure tests, no HTTP — except the pagination helper (respx, 09 §A.6 test 21).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eta_automations.carriers import dcsa
from eta_automations.carriers.dcsa import (
    CLASSIFIER_RANK,
    PLAUSIBLE_YEARS,
    POST_ARRIVAL_CODES,
    WRAPPER_KEYS,
    DcsaArrival,
    DcsaEvent,
    booking_arrival,
    eta_result_from_payload,
    explain_selection,
    fetch_event_pages,
    naive_local,
    naive_utc,
    parse_dcsa_datetime,
    parse_events,
    rank_key,
    resolve_pod,
    select_pod_arrival,
    select_pod_arrival_per_equipment,
    to_eta_result,
    to_local_naive,
)
from eta_automations.errors import AuthError, CarrierLookupError, NoEtaError, ServiceError
from eta_automations.models import Carrier, EtaResult

FIXTURES = Path(__file__).parent / "fixtures" / "dcsa"
DT = datetime


def _load(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


# ── raw event builders (DCSA 2.2 shapes) ──
def _call(loc: str | None, *, mode: str | None = "VESSEL", ftype: str | None = "POTE", fac: str | None = None,
          vessel: dict[str, Any] | None = None, tcid: str | None = None) -> dict[str, Any]:
    call: dict[str, Any] = {"transportCallID": tcid or f"TC-{loc}"}
    if loc:
        call["UNLocationCode"] = loc
    if mode:
        call["modeOfTransport"] = mode
    if ftype:
        call["facilityTypeCode"] = ftype
    if fac:
        call["facilityCode"] = fac
    if vessel:
        call["vessel"] = vessel
    return call


def _tev(eid: str, cls: str, code: str, when: str, loc: str, *, created: str | None = None,
         mode: str | None = "VESSEL", ftype: str | None = "POTE", fac: str | None = None,
         vessel: dict[str, Any] | None = None, refs: tuple[str, ...] = ()) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "eventID": eid, "eventType": "TRANSPORT", "eventClassifierCode": cls, "transportEventTypeCode": code,
        "eventDateTime": when, "eventCreatedDateTime": created or when,
        "transportCall": _call(loc, mode=mode, ftype=ftype, fac=fac, vessel=vessel),
    }
    if refs:
        ev["references"] = [{"referenceType": "EQ", "referenceValue": r} for r in refs]
    return ev


def _eev(eid: str, cls: str, code: str, when: str, loc: str, box: str, *, created: str | None = None,
         empty: str = "LADEN", mode: str | None = "VESSEL", ftype: str | None = "POTE", fac: str | None = None,
         vessel: dict[str, Any] | None = None, with_call: bool = True) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "eventID": eid, "eventType": "EQUIPMENT", "eventClassifierCode": cls, "equipmentEventTypeCode": code,
        "eventDateTime": when, "eventCreatedDateTime": created or when, "equipmentReference": box,
        "emptyIndicatorCode": empty,
    }
    if with_call:
        ev["transportCall"] = _call(loc, mode=mode, ftype=ftype, fac=fac, vessel=vessel)
    else:
        ev["eventLocation"] = {"UNLocationCode": loc}
    return ev


def _event(**overrides: Any) -> DcsaEvent:
    """Hand-built DcsaEvent (aggregator/tests style)."""
    base: dict[str, Any] = dict(
        event_type="TRANSPORT", classifier="EST", type_code="ARRI", event_datetime=DT(2026, 11, 10, 8, 0),
        created_datetime=None, unlocode="USLAX", facility_code=None, facility_type="POTE",
        mode_of_transport="VESSEL", equipment_reference=None, vessel_name=None, transport_call_sequence=None,
        raw={},
    )
    base.update(overrides)
    return DcsaEvent(**base)


VESSEL_A = {"vesselIMONumber": "9000001", "vesselName": "EXAMPLE ONE"}
VESSEL_B = {"vesselIMONumber": "9000002", "vesselName": "EXAMPLE TWO"}


# ───────────────────────── 1. expected results (09 §A.5 table) ─────────────────────────
# per: key → (eta, classifier, basis, facility_code)
EXPECTED: dict[str, dict[str, Any]] = {
    "v22_single_revised_est": {
        "count": 8,
        "pod": "USLAX",
        "per": {
            "MSKU1234565": (DT(2026, 11, 13, 9, 30), "EST", "TRANSPORT ARRI EST", "APMT"),
            None: (DT(2026, 11, 13, 9, 30), "EST", "TRANSPORT ARRI EST", "APMT"),
        },
        "vessel": (DT(2026, 11, 13, 9, 30), "EST"),
        "first": (DT(2026, 11, 13, 9, 30), "EST", "MSKU1234565"),
        "latest": (DT(2026, 11, 13, 9, 30), "EST", "MSKU1234565"),
    },
    "v22_transshipment_discharged": {
        "count": 9,
        "pod": "USNYC",
        "per": {
            "MEDU1234567": (DT(2026, 11, 2, 22, 40), "ACT", "EQUIPMENT DISC ACT", "PNCT"),
            None: (DT(2026, 11, 2, 7, 15), "ACT", "TRANSPORT ARRI ACT", "PNCT"),
        },
        "vessel": (DT(2026, 11, 2, 7, 15), "ACT"),
        "first": (DT(2026, 11, 2, 22, 40), "ACT", "MEDU1234567"),
        "latest": (DT(2026, 11, 2, 22, 40), "ACT", "MEDU1234567"),
    },
    "v22_multi_container_two_vessels": {
        "count": 7,
        "pod": "USSAV",
        "per": {
            "CMAU1111111": (DT(2026, 11, 9, 10, 0), "EST", "TRANSPORT ARRI EST", "GCT"),
            "CMAU2222222": (DT(2026, 11, 16, 8, 0), "EST", "TRANSPORT ARRI EST", "GCT"),
            None: (DT(2026, 11, 16, 8, 0), "EST", "TRANSPORT ARRI EST", "GCT"),
        },
        "vessel": (DT(2026, 11, 16, 8, 0), "EST"),
        "first": (DT(2026, 11, 9, 10, 0), "EST", "CMAU1111111"),
        "latest": (DT(2026, 11, 16, 8, 0), "EST", "CMAU2222222"),
    },
    "v3_envelopes_with_retraction": {
        "count": 4,
        "pod": "USHOU",
        "per": {
            "HLXU1234567": (DT(2026, 11, 20, 6, 0), "EST", "TRANSPORT ARRI EST", None),
            None: (DT(2026, 11, 20, 6, 0), "EST", "TRANSPORT ARRI EST", None),
        },
        "vessel": (DT(2026, 11, 20, 6, 0), "EST"),
        "first": (DT(2026, 11, 20, 6, 0), "EST", "HLXU1234567"),
        "latest": (DT(2026, 11, 20, 6, 0), "EST", "HLXU1234567"),
    },
    "v22_no_pod_yet": {
        "count": 3,
        "pod": None,
        "per": {},
        "vessel": None,
        "first": None,
        "latest": None,
    },
}


@pytest.mark.parametrize("fixture", list(EXPECTED))
def test_expected_results(fixture: str) -> None:
    exp = EXPECTED[fixture]
    events = parse_events(_load(fixture))
    assert len(events) == exp["count"]
    assert resolve_pod(events) == exp["pod"]

    per = select_pod_arrival_per_equipment(events)
    assert list(per) == list(exp["per"])  # container keys in equipment order, then None (last)
    for key, (eta, cls, basis, facility) in exp["per"].items():
        a = per[key]
        assert (a.eta, a.classifier, a.basis, a.facility_code) == (eta, cls, basis, facility)
        assert a.unlocode == exp["pod"]
        assert a.equipment_reference == key
        assert a.eta.tzinfo is None

    vessel = select_pod_arrival(events)
    if exp["vessel"] is None:
        assert vessel is None
    else:
        assert (vessel.eta, vessel.classifier) == exp["vessel"]
        assert vessel.unlocode == exp["pod"]
        assert vessel.equipment_reference is None
        assert vessel.basis.startswith("TRANSPORT ARRI ")

    for policy in ("first", "latest"):
        chosen = booking_arrival(events, policy=policy)
        if exp[policy] is None:
            assert chosen is None
        else:
            assert (chosen.eta, chosen.classifier, chosen.equipment_reference) == exp[policy]


def test_expected_to_eta_result_first_fixture() -> None:
    events = parse_events(_load("v22_single_revised_est"))
    first = booking_arrival(events, policy="first")
    result = to_eta_result(first, carrier=Carrier.MAERSK, booking="267546627", containers=("MSKU1234565",))
    assert isinstance(result, EtaResult)
    assert result.eta == datetime(2026, 11, 13, 9, 30)
    assert result.source == "DCSA TRANSPORT ARRI EST at USLAX/APMT"
    assert result.is_actual is False
    assert result.carrier is Carrier.MAERSK
    assert result.booking == "267546627"
    assert result.containers == ("MSKU1234565",)
    assert result.details == {"basis": "TRANSPORT ARRI EST", "classifier": "EST", "unlocode": "USLAX",
                              "facility_code": "APMT", "equipment_reference": "MSKU1234565"}


def test_to_eta_result_variants() -> None:
    actual = DcsaArrival(DT(2026, 11, 2, 22, 40), "ACT", "USNYC", None, "MEDU1234567", "EQUIPMENT DISC ACT")
    res = to_eta_result(actual, carrier=Carrier.MSC, booking="EBKG12345678", source_prefix="ShipsGo")
    assert res.source == "ShipsGo EQUIPMENT DISC ACT at USNYC"
    assert res.is_actual is True
    assert res.containers == ()
    unknown = DcsaArrival(DT(2026, 11, 2), "EST", None, None, None, "ROUTE POD date_of_discharge")
    assert to_eta_result(unknown, carrier=Carrier.GRIMALDI, booking="X").source == (
        "DCSA ROUTE POD date_of_discharge at ?")
    aware = DcsaArrival(DT(2026, 11, 2, 7, 0, tzinfo=timezone(timedelta(hours=-5))), "EST", "USNYC", None, None,
                        "TRANSPORT ARRI EST")
    assert to_eta_result(aware, carrier=Carrier.CMA, booking="NAM1").eta == DT(2026, 11, 2, 7, 0)  # naive, wall time


# ───────────────────────── 2. wrappers ─────────────────────────
def test_wrappers() -> None:
    items = _load("v22_single_revised_est")
    base = parse_events(items)
    for key in ("events", "data", "items", "content", "results"):
        assert parse_events({key: items}) == base
    assert WRAPPER_KEYS == ("events", "data", "items", "content", "results")
    assert parse_events({"Events": items}) == base  # wrapper keys are read case-insensitively too
    assert parse_events(tuple(items)) == base

    single = parse_events(items[2])
    assert len(single) == 1
    assert single[0].event_id == "t-arr-pol"
    assert single[0].raw is items[2]

    envelope = _load("v3_envelopes_with_retraction")[1]
    single_v3 = parse_events(envelope)
    assert len(single_v3) == 1 and single_v3[0].event_id == "v3-load-1"

    with pytest.raises(ValueError, match="foo") as excinfo:
        parse_events({"foo": 1})
    assert "unrecognised DCSA payload shape" in str(excinfo.value)
    with pytest.raises(ValueError, match="unrecognised DCSA payload shape: str"):
        parse_events("not json")
    with pytest.raises(ValueError):
        parse_events(None)
    with pytest.raises(ValueError):
        parse_events({"events": {"not": "a list"}})
    assert parse_events([]) == []
    assert parse_events({"events": []}) == []


# ───────────────────────── 3. location fallbacks / field mapping ─────────────────────────
def test_location_fallbacks() -> None:
    only_location = {"eventType": "TRANSPORT", "eventClassifierCode": "PLN", "transportEventTypeCode": "ARRI",
                     "eventDateTime": "2026-11-14T06:00:00-05:00",
                     "transportCall": {"modeOfTransport": "VESSEL",
                                       "location": {"UNLocationCode": "usnyc", "facilityCode": "apmt"}}}
    only_event_location = {"eventType": "EQUIPMENT", "eventClassifierCode": "ACT", "equipmentEventTypeCode": "GTOT",
                           "eventDateTime": "2026-11-15T10:00:00-05:00", "equipmentReference": "msku1111111",
                           "eventLocation": {"UNLocationCode": "USNYC", "facilityCode": "PNCT"}}
    only_call = {"eventType": "TRANSPORT", "eventClassifierCode": "EST", "transportEventTypeCode": "ARRI",
                 "eventDateTime": "2026-11-15T22:00:00-05:00",
                 "transportCall": {"UNLocationCode": "USNYC", "facilityCode": "APMT", "modeOfTransport": "VESSEL",
                                   "transportCallSequenceNumber": "3"},
                 "eventId": "evt-42"}
    camel_call = {"eventType": "TRANSPORT", "eventClassifierCode": "EST", "transportEventTypeCode": "ARRI",
                  "eventDateTime": "2026-11-12T06:00:00Z", "transportCall": {"unLocationCode": "GHTEM"}}
    camel_location = {"eventType": "EQUIPMENT", "eventClassifierCode": "ACT", "equipmentEventTypeCode": "LOAD",
                      "eventDateTime": "2026-10-20T09:15:00Z", "equipmentReference": "MSCU1111111",
                      "eventLocation": {"unLocationCode": "USNYC", "locationName": "NEW YORK"}}
    lower = {"eventtype": "transport", "eventclassifiercode": "est", "transporteventtypecode": "arri",
             "eventdatetime": "2026-11-12T08:00:00-08:00", "transportcall": {"unlocationcode": "uslax"}}
    call_beats_location = {"eventType": "TRANSPORT", "eventClassifierCode": "EST", "transportEventTypeCode": "ARRI",
                           "eventDateTime": "2026-11-12T08:00:00-08:00",
                           "transportCall": {"UNLocationCode": "USLAX", "location": {"UNLocationCode": "USLGB"}},
                           "eventLocation": {"UNLocationCode": "USOAK"}}
    events = parse_events([only_location, only_event_location, only_call, camel_call, camel_location, lower,
                           call_beats_location])
    assert [e.unlocode for e in events] == ["USNYC", "USNYC", "USNYC", "GHTEM", "USNYC", "USLAX", "USLAX"]
    assert [e.facility_code for e in events][:3] == ["APMT", "PNCT", "APMT"]
    assert events[1].equipment_reference == "MSKU1111111"  # upper-cased
    assert events[2].transport_call_sequence == 3
    assert events[2].event_id == "evt-42"
    assert events[5].event_type == "TRANSPORT" and events[5].classifier == "EST" and events[5].type_code == "ARRI"
    assert [e.index for e in events] == list(range(7))


def test_field_mapping_v22_and_v3() -> None:
    v22 = parse_events(_load("v22_transshipment_discharged"))
    load = v22[0]
    assert (load.event_type, load.classifier, load.type_code) == ("EQUIPMENT", "ACT", "LOAD")
    assert load.event_datetime == DT(2026, 10, 10, 18, 0)
    assert load.created_datetime == DT(2026, 10, 10, 16, 5)
    assert (load.unlocode, load.facility_code, load.facility_type, load.mode_of_transport) == (
        "ITGOA", "PSAGP", "POTE", "VESSEL")
    assert (load.vessel_name, load.vessel_imo, load.voyage) == ("MSC FEEDER", "9000011", "FA642W")
    assert (load.transport_call_key, load.transport_call_sequence) == ("TC-ITGOA-F", 1)
    assert load.empty_indicator == "LADEN"
    assert load.is_transshipment_move is None
    assert load.eq_refs == ()

    v3 = parse_events(_load("v3_envelopes_with_retraction"))
    assert [e.event_id for e in v3] == ["v3-shp-1", "v3-load-1", "v3-arr-pln", "v3-arr-1"]
    shipment, v3_load, pln, est = v3
    assert (shipment.event_type, shipment.type_code) == ("SHIPMENT", "CONF")
    assert (v3_load.unlocode, v3_load.facility_code) == ("BEANR", "PSADGD")  # from transportCall.location
    assert v3_load.vessel_name == "EXAMPLE EXPRESS"  # v3 vessel.name
    assert v3_load.vessel_imo == "9500001"
    assert v3_load.voyage == "612W"  # carrierExportVoyageNumber
    assert v3_load.transport_call_key == "TCR-ANR-1"  # transportCallReference
    assert v3_load.is_transshipment_move is False
    assert v3_load.created_datetime == DT(2026, 10, 24, 20, 0)
    assert v3_load.event_datetime == DT(2026, 10, 24, 21, 15)
    assert pln.voyage == "612W"  # carrierImportVoyageNumber
    assert est.eq_refs == ("HLXU1234567",)
    assert est.index == 3
    assert est.raw["metadata"]["eventID"] == "v3-arr-1"  # raw is the original v3 envelope

    voyages = parse_events([{
        "eventType": "TRANSPORT", "eventClassifierCode": "EST", "transportEventTypeCode": "ARRI",
        "eventDateTime": "2026-11-12T08:00:00", "transportCall": {
            "UNLocationCode": "USLAX", "exportVoyageNumber": "E1", "carrierVoyageNumber": "C1",
            "importVoyageNumber": " ", "transportCallID": "", "transportCallReference": "REF-1",
            "facilityTypeCode": "pote", "vessel": {"vesselName": "  Lower Case  ", "vesselIMONumber": 9811000}},
        "references": [{"type": "EQ", "value": "abcu1234567"}, {"referenceType": "EQ", "referenceValue": "ABCU1234567"},
                       {"referenceType": "FF", "referenceValue": "X"}, "junk", {"type": "eq", "value": "XYZU7654321"}],
    }])[0]
    assert voyages.voyage == "C1"  # importVoyageNumber is blank → carrierVoyageNumber
    assert voyages.transport_call_key == "REF-1"
    assert voyages.facility_type == "POTE"
    assert voyages.vessel_name == "Lower Case"
    assert voyages.vessel_imo == "9811000"
    assert voyages.eq_refs == ("ABCU1234567", "XYZU7654321")  # de-duplicated, in order, upper-cased
    assert voyages.created_datetime is None  # no eventCreatedDateTime
    assert dcsa.vessel_identity(voyages) == "9811000"


def test_case_insensitive_keys_first_occurrence_wins() -> None:
    item = {"eventType": "TRANSPORT", "EVENTTYPE": "EQUIPMENT", "eventClassifierCode": "EST",
            "transportEventTypeCode": "ARRI", "eventDateTime": "2026-11-12T08:00:00",
            "transportCall": {"UNLocationCode": "USLAX", "unlocationcode": "USLGB"}}
    (event,) = parse_events([item])
    assert event.event_type == "TRANSPORT"
    assert event.unlocode == "USLAX"


# ───────────────────────── 3b. MSC official-API shape (06 §9.2 api_in_transit.json) ─────────────────────────
MSC_API_IN_TRANSIT = json.loads("""[
 {"eventType": "TRANSPORT", "transportEventTypeCode": "ARRI", "eventClassifierCode": "EST", "description": "Estimated Time of Arrival",
  "eventId": "EST2026-11-12T06:00:00:00+00:00", "eventDateTime": "2026-11-12T06:00:00Z", "eventCreatedDateTime": "2026-11-01T10:00:00Z",
  "transportCall": {"transportCallID": "GHTEM0001", "unLocationCode": "GHTEM", "facilityTypeCode": "POTE", "modeOfTransport": "VESSEL",
                    "vessel": {"vesselIMONumber": "9000001", "vesselName": "MSC EXAMPLE"}},
  "documentReferences": [{"documentReferenceType": "BKG", "documentReferenceValue": "EBKG12345678"}], "references": []},
 {"eventType": "TRANSPORT", "transportEventTypeCode": "DEPA", "eventClassifierCode": "ACT", "description": "Vessel Departure",
  "eventId": "ACT2026-10-20T18:00:00:00-04:00", "eventDateTime": "2026-10-20T18:00:00.0000000+00:00", "eventCreatedDateTime": "2026-10-20T19:00:00.0000000+00:00",
  "transportCall": {"transportCallID": "USNYC0001", "unLocationCode": "USNYC", "facilityTypeCode": "POTE", "modeOfTransport": "VESSEL"}, "references": []},
 {"eventType": "EQUIPMENT", "equipmentEventTypeCode": "LOAD", "eventClassifierCode": "ACT", "description": "Export Loaded on Vessel",
  "eventId": "1000000003", "eventDateTime": "2026-10-20T09:15:00Z", "eventCreatedDateTime": "2026-10-20T09:20:00Z",
  "equipmentReference": "MSCU1111111", "emptyIndicatorCode": "LADEN",
  "transportCall": {"transportCallID": "USNYC0001", "unLocationCode": "USNYC", "facilityTypeCode": "POTE", "modeOfTransport": "VESSEL"},
  "eventLocation": {"locationName": "NEW YORK", "unLocationCode": "USNYC"}, "seals": [], "references": []}
]""")


def test_msc_api_shape() -> None:
    bare = parse_events(MSC_API_IN_TRANSIT)
    wrapped = parse_events({"events": MSC_API_IN_TRANSIT})
    assert bare == wrapped
    assert len(bare) == 3
    assert bare[0].raw["description"] == "Estimated Time of Arrival"
    assert bare[0].event_id == "EST2026-11-12T06:00:00:00+00:00"
    assert bare[0].unlocode == "GHTEM"
    assert bare[1].event_datetime == datetime(2026, 10, 20, 18, 0)
    assert bare[1].created_datetime == datetime(2026, 10, 20, 19, 0)
    assert bare[2].unlocode == "USNYC"

    arrival = booking_arrival(bare, policy="first")
    assert arrival is not None
    assert arrival.eta == datetime(2026, 11, 12, 6, 0)
    assert arrival.classifier == "EST"
    assert arrival.unlocode == "GHTEM"
    assert arrival.equipment_reference == "MSCU1111111"
    # the order of MSC's list (newest first) does not matter
    assert booking_arrival(parse_events(list(reversed(MSC_API_IN_TRANSIT))), policy="first") == arrival
    result = to_eta_result(arrival, carrier=Carrier.MSC, booking="EBKG12345678", containers=("MSCU1111111",))
    assert result.eta.date() == date(2026, 11, 12)
    assert result.is_actual is False


# ───────────────────────── 4. raw is the original dict ─────────────────────────
def test_raw_is_original_dict() -> None:
    items = _load("v22_transshipment_discharged")
    items[0]["carrierSpecificData"] = {"shipmentLocationType": "POL", "internalEventLabel": "Loaded on board"}
    snapshot = copy.deepcopy(items)
    events = parse_events(items)
    for e in events:
        assert e.raw is items[e.index]
    assert events[0].raw["carrierSpecificData"]["shipmentLocationType"] == "POL"
    assert items == snapshot  # parsing never mutates the payload

    envelopes = _load("v3_envelopes_with_retraction")
    for e in parse_events(envelopes):
        assert e.raw is envelopes[e.index]  # v3: the original envelope, not the flattened payload


# ───────────────────────── 5. datetime parsing ─────────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-12T08:00:00Z", DT(2026, 11, 12, 8, 0, tzinfo=timezone.utc)),
        ("2026-11-12T08:00:00+0000", DT(2026, 11, 12, 8, 0, tzinfo=timezone.utc)),
        ("2026-11-12T08:00:00+05:30", DT(2026, 11, 12, 8, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))),
        ("2026-11-12T08:00:00.1234567+00:00", DT(2026, 11, 12, 8, 0, 0, 123456, tzinfo=timezone.utc)),
        ("2026-12-31T23:59:59.9999999", DT(2026, 12, 31, 23, 59, 59, 999999)),
        ("2026-11-12 08:00:00", DT(2026, 11, 12, 8, 0)),
        ("2026-11-12", DT(2026, 11, 12, 0, 0)),
        ("20261112T080000Z", DT(2026, 11, 12, 8, 0, tzinfo=timezone.utc)),
        ("  2026-11-12T08:00:00-08:00  ", DT(2026, 11, 12, 8, 0, tzinfo=timezone(timedelta(hours=-8)))),
    ],
)
def test_datetime_parsing(text: str, expected: datetime) -> None:
    parsed = parse_dcsa_datetime(text)
    assert parsed == expected
    assert parsed.utcoffset() == expected.utcoffset()


@pytest.mark.parametrize("bad", [None, "", "   ", "garbage", "2026-13-45", "12 Nov 2026", 20261112, ["2026-11-12"]])
def test_datetime_parsing_never_raises(bad: Any) -> None:
    assert parse_dcsa_datetime(bad) is None


def test_event_and_created_datetime_conversion() -> None:
    item = {"eventType": "TRANSPORT", "eventClassifierCode": "EST", "transportEventTypeCode": "ARRI",
            "eventDateTime": "2026-11-10T14:00:00-08:00", "eventCreatedDateTime": "2026-11-04T09:15:00+01:00",
            "transportCall": {"UNLocationCode": "USLGB"}}
    (event,) = parse_events([item])
    assert event.event_datetime == datetime(2026, 11, 10, 14, 0)  # 08 E2: local wall time kept
    assert event.event_datetime.tzinfo is None
    assert event.created_datetime == datetime(2026, 11, 4, 8, 15)  # naive UTC
    assert event.created_datetime.tzinfo is None


def test_naive_helpers() -> None:
    aware = DT(2026, 11, 10, 14, 0, tzinfo=timezone(timedelta(hours=-8)))
    assert naive_local(aware) == DT(2026, 11, 10, 14, 0)
    assert naive_utc(aware) == DT(2026, 11, 10, 22, 0)
    assert naive_local(None) is None and naive_utc(None) is None
    plain = DT(2026, 11, 10, 14, 0)
    assert naive_local(plain) == plain and naive_utc(plain) == plain
    # year-1 times with a positive offset overflow → None (never raises)
    assert naive_utc(DT(1, 1, 1, 0, 0, tzinfo=timezone(timedelta(hours=1)))) is None
    assert naive_utc(DT(9999, 12, 31, 23, 59, tzinfo=timezone(timedelta(hours=-12)))) is None


# ───────────────────────── 6. sentinel years ─────────────────────────
def test_sentinel_years_never_crash() -> None:
    good = _tev("ok", "EST", "ARRI", "2026-11-20T08:00:00-06:00", "USHOU")
    year_one = _tev("y1", "EST", "ARRI", "0001-01-01T00:00:00+01:00", "USHOU")
    year_9999 = _tev("y9", "EST", "ARRI", "9999-12-31T23:59:59.9999999-12:00", "USHOU")
    created_9999 = _tev("c9", "PLN", "ARRI", "2026-11-19T08:00:00-06:00", "USHOU",
                        created="9999-12-31T23:59:59.9999999-12:00")
    created_year_one = _tev("c1", "PLN", "ARRI", "2026-11-18T08:00:00-06:00", "USHOU",
                            created="0001-01-01T00:00:00+01:00")
    created_far = _tev("cf", "PLN", "ARRI", "2026-11-17T08:00:00-06:00", "USHOU", created="2150-01-01T00:00:00Z")
    events = parse_events([year_one, good, year_9999, created_9999, created_year_one, created_far])
    assert [e.event_id for e in events] == ["ok", "c9", "c1", "cf"]
    assert all(e.created_datetime is None for e in events[1:])
    assert events[0].created_datetime == DT(2026, 11, 20, 14, 0)
    assert all(e.event_datetime.year in PLAUSIBLE_YEARS for e in events)
    assert PLAUSIBLE_YEARS == range(2000, 2101)
    # ranking and selection still work with created_datetime None
    assert booking_arrival(events, policy="first").eta == DT(2026, 11, 20, 8, 0)


# ───────────────────────── 7. malformed events ─────────────────────────
def test_malformed_events_skipped(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="eta.carrier.dcsa")
    good = _tev("good", "EST", "ARRI", "2026-11-20T08:00:00-06:00", "USHOU")
    unknown_type = {**good, "eventID": "u", "eventType": "IOT"}
    no_type = {k: v for k, v in good.items() if k != "eventType"}
    no_classifier = {k: v for k, v in good.items() if k != "eventClassifierCode"}
    blank_classifier = {**good, "eventID": "b", "eventClassifierCode": "  "}
    no_code = {k: v for k, v in good.items() if k != "transportEventTypeCode"}
    bad_date = {**good, "eventID": "d", "eventDateTime": "next Tuesday"}
    no_date = {k: v for k, v in good.items() if k != "eventDateTime"}
    deprecated_alias = {k: v for k, v in good.items() if k != "transportEventTypeCode"}
    deprecated_alias.update({"eventID": "alias", "eventTypeCode": "arri"})
    weird_nested = {**good, "eventID": "weird", "transportCall": ["not", "an", "object"], "references": "nope"}
    items: list[Any] = [unknown_type, no_type, no_classifier, blank_classifier, no_code, bad_date, no_date,
                        "a string", 42, None, ["list"], deprecated_alias, good, weird_nested]
    events = parse_events(items)
    assert [e.event_id for e in events] == ["alias", "good", "weird"]
    assert events[0].type_code == "ARRI"  # deprecated eventTypeCode alias accepted
    assert events[0].index == 11
    assert events[2].unlocode is None and events[2].eq_refs == ()
    assert "[DCSA] skipped event #0" in caplog.text
    assert "[DCSA] skipped event #7: not an object" in caplog.text


# ───────────────────────── 8. v3 withdrawals ─────────────────────────
def test_retraction_v3() -> None:
    envelopes = _load("v3_envelopes_with_retraction")
    ids = [e.event_id for e in parse_events(envelopes)]
    assert "v3-arr-2" not in ids
    assert ids == ["v3-shp-1", "v3-load-1", "v3-arr-pln", "v3-arr-1"]

    # the withdrawal may come before the withdrawn event
    reordered = [envelopes[5]] + envelopes[:5]
    assert [e.event_id for e in parse_events(reordered)] == ids

    harmless = envelopes[:5] + [{"metadata": {"eventID": "v3-ret-x", "eventType": "TRANSPORT",
                                              "eventCreatedDateTime": "2026-11-04T08:00:00Z",
                                              "retractedEventID": "does-not-exist"}}]
    assert [e.event_id for e in parse_events(harmless)] == ["v3-shp-1", "v3-load-1", "v3-arr-pln", "v3-arr-1",
                                                             "v3-arr-2"]
    # an envelope with an empty payload produces no event
    assert parse_events([{"metadata": {"eventID": "x", "eventType": "TRANSPORT"}, "payload": {}}]) == []


def test_v3_metadata_fills_missing_fields() -> None:
    envelope = {"metadata": {"eventID": "m-1", "eventType": "TRANSPORT", "eventCreatedDateTime": "2026-11-01T00:00:00Z"},
                "payload": {"eventType": None, "eventClassifierCode": "EST", "transportEventTypeCode": "ARRI",
                            "eventDateTime": "2026-11-20T06:00:00-06:00",
                            "transportCall": {"location": {"UNLocationCode": "USHOU"}}}}
    (event,) = parse_events([envelope])
    assert (event.event_type, event.event_id, event.created_datetime) == ("TRANSPORT", "m-1", DT(2026, 11, 1))


# ───────────────────────── 9. ranking ─────────────────────────
def test_rank_key() -> None:
    assert dict(CLASSIFIER_RANK) == {"ACT": 3, "EST": 2, "PLN": 1}
    act_old = _event(classifier="ACT", created_datetime=DT(2026, 10, 1), event_datetime=DT(2026, 11, 1))
    est_new = _event(classifier="EST", created_datetime=DT(2026, 11, 4), event_datetime=DT(2026, 11, 20))
    assert max([est_new, act_old], key=rank_key) is act_old  # ACT beats a newer EST

    # 08 E5: EST created 11-02 saying 11-10 14:00 beats EST created 10-19 saying 11-12
    est_1019 = _event(created_datetime=DT(2026, 10, 19, 6, 0), event_datetime=DT(2026, 11, 12, 6, 0), index=8)
    est_1102 = _event(created_datetime=DT(2026, 11, 2, 7, 0), event_datetime=DT(2026, 11, 10, 14, 0), index=9)
    assert max([est_1019, est_1102], key=rank_key) is est_1102
    assert max([est_1102, est_1019], key=rank_key) is est_1102

    # created ties → the later eventDateTime; then the later index
    tie_early = _event(created_datetime=DT(2026, 11, 1), event_datetime=DT(2026, 11, 10), index=5)
    tie_late = _event(created_datetime=DT(2026, 11, 1), event_datetime=DT(2026, 11, 11), index=1)
    assert max([tie_late, tie_early], key=rank_key) is tie_late
    same_a = _event(created_datetime=DT(2026, 11, 1), event_datetime=DT(2026, 11, 10), index=1)
    same_b = _event(created_datetime=DT(2026, 11, 1), event_datetime=DT(2026, 11, 10), index=2)
    assert max([same_b, same_a], key=rank_key) is same_b

    # 07 req. 4: mixed naive and aware created_datetime (and event_datetime) values never raise
    aware = _event(created_datetime=DT(2026, 11, 3, 9, 0, tzinfo=timezone(timedelta(hours=1))),
                   event_datetime=DT(2026, 11, 16, 7, 0, tzinfo=timezone(timedelta(hours=-5))))
    naive = _event(created_datetime=DT(2026, 11, 3, 8, 30), event_datetime=DT(2026, 11, 15, 22, 0))
    missing = _event(created_datetime=None, event_datetime=DT(2026, 11, 14, 6, 0))
    assert max([aware, naive, missing], key=rank_key) is naive  # 08:30Z is newer than 09:00+01:00 (= 08:00Z)
    assert sorted([aware, naive, missing], key=rank_key)[0] is missing

    # unknown classifiers rank 0
    assert rank_key(_event(classifier="REQ"))[0] == 0
    assert rank_key(_event(classifier="act"))[0] == 3


# ───────────────────────── predicates ─────────────────────────
def test_predicates() -> None:
    assert dcsa.is_usable(_event(classifier="PLN"))
    assert not dcsa.is_usable(_event(classifier="REQ"))
    assert not dcsa.is_usable(_event(event_type="SHIPMENT", classifier="ACT", type_code="CONF"))
    assert dcsa.is_laden(_event(empty_indicator=None)) and dcsa.is_laden(_event(empty_indicator="LADEN"))
    assert not dcsa.is_laden(_event(empty_indicator="EMPTY"))
    assert dcsa.is_vessel_ish(_event(mode_of_transport="VESSEL", facility_type="RAMP"))
    assert dcsa.is_vessel_ish(_event(mode_of_transport=None, facility_type=None))
    assert dcsa.is_vessel_ish(_event(mode_of_transport=None, facility_type="POTE"))
    assert not dcsa.is_vessel_ish(_event(mode_of_transport=None, facility_type="RAMP"))
    assert not dcsa.is_vessel_ish(_event(mode_of_transport="RAIL"))
    assert dcsa.is_vessel_arrival(_event())
    assert not dcsa.is_vessel_arrival(_event(unlocode=None))
    assert not dcsa.is_vessel_arrival(_event(type_code="DEPA"))
    assert not dcsa.is_vessel_arrival(_event(mode_of_transport="TRUCK"))
    assert dcsa.vessel_identity(_event(vessel_imo="9000001", vessel_name="X")) == "9000001"
    assert dcsa.vessel_identity(_event(vessel_name="Msc Example")) == "MSC EXAMPLE"
    assert dcsa.vessel_identity(_event()) is None

    disc = _event(event_type="EQUIPMENT", classifier="ACT", type_code="DISC", equipment_reference="ABCU1",
                  event_datetime=DT(2026, 10, 14))
    later_load = _event(event_type="EQUIPMENT", classifier="ACT", type_code="LOAD", equipment_reference="ABCU1",
                        unlocode="MAPTM", event_datetime=DT(2026, 10, 19))
    other_box_load = _event(event_type="EQUIPMENT", classifier="ACT", type_code="LOAD", equipment_reference="ABCU2",
                            event_datetime=DT(2026, 10, 19))
    rail_load = _event(event_type="EQUIPMENT", classifier="ACT", type_code="LOAD", equipment_reference="ABCU1",
                       mode_of_transport="RAIL", event_datetime=DT(2026, 10, 19))
    assert dcsa.is_final_disc(disc, [disc, other_box_load, rail_load])
    assert not dcsa.is_final_disc(disc, [disc, later_load])
    assert not dcsa.is_final_disc(dataclass_replace(disc, is_transshipment_move=True), [disc])
    assert not dcsa.is_final_disc(dataclass_replace(disc, empty_indicator="EMPTY"), [disc])
    assert not dcsa.is_final_disc(dataclass_replace(disc, unlocode=None), [disc])
    assert POST_ARRIVAL_CODES == ("GTOT", "AVPU", "PICK")


def dataclass_replace(event: DcsaEvent, **changes: Any) -> DcsaEvent:
    import dataclasses

    return dataclasses.replace(event, **changes)


def test_dataclasses_positional_construction() -> None:
    raw = {"x": 1}
    event = DcsaEvent("TRANSPORT", "EST", "ARRI", DT(2026, 11, 10), None, "USLAX", "APMT", "POTE", "VESSEL",
                      None, "EXAMPLE", 2, raw)
    assert event.raw is raw
    assert (event.event_id, event.transport_call_key, event.vessel_imo, event.voyage, event.empty_indicator,
            event.is_transshipment_move, event.eq_refs, event.index) == (None, None, None, None, None, None, (), 0)
    arrival = DcsaArrival(DT(2026, 11, 10), "EST", "USLAX", None, None, "TRANSPORT ARRI EST")
    assert arrival.basis == "TRANSPORT ARRI EST"
    with pytest.raises(AttributeError):
        arrival.eta = DT(2026, 1, 1)  # type: ignore[misc]  # frozen


# ───────────────────────── 10–12. resolve_pod ─────────────────────────
def test_resolve_pod_ipi_rail() -> None:
    items = [
        _eev("E1", "ACT", "LOAD", "2026-10-25T09:00:00+08:00", "CNSHK", "MSKU5555555", vessel=VESSEL_A),
        _tev("T1", "ACT", "DEPA", "2026-10-25T21:00:00+08:00", "CNSHK", vessel=VESSEL_A),
        _tev("T2", "EST", "ARRI", "2026-11-10T08:00:00-08:00", "USLAX", created="2026-10-26T10:00:00Z", vessel=VESSEL_A),
        _tev("T3", "EST", "ARRI", "2026-11-12T08:00:00-08:00", "USLAX", created="2026-11-04T10:00:00Z", vessel=VESSEL_A),
        _tev("T4", "PLN", "DEPA", "2026-11-14T20:00:00-08:00", "USLAX", mode="RAIL", ftype="RAMP"),
        _tev("T5", "PLN", "ARRI", "2026-11-18T14:00:00-06:00", "USCHI", mode="RAIL", ftype="RAMP", fac="LPC"),
    ]
    events = parse_events(items)
    assert resolve_pod(events) == "USLAX"
    arrival = booking_arrival(events, policy="first")
    assert (arrival.eta, arrival.classifier, arrival.unlocode) == (DT(2026, 11, 12, 8, 0), "EST", "USLAX")
    # without a transport call, a rail arrival at a RAMP is still not vessel-ish
    no_mode = [_tev("R", "PLN", "ARRI", "2026-11-18T14:00:00-06:00", "USCHI", mode=None, ftype="RAMP")]
    assert resolve_pod(parse_events(items[:4] + no_mode)) == "USLAX"


def test_resolve_pod_loading_guard() -> None:
    events = parse_events(_load("v22_no_pod_yet"))
    assert resolve_pod(events) is None  # never guess the port of loading
    assert select_pod_arrival(events) is None
    assert select_pod_arrival_per_equipment(events) == {}


def test_resolve_pod_last_vessel_arrival_and_transshipment_exclusion() -> None:
    events = parse_events(_load("v22_transshipment_discharged"))
    assert resolve_pod(events) == "USNYC"  # MAPTM excluded: the box was loaded there
    # a stale PLN at a later location does not beat the actual arrival... it is the LAST arrival that counts
    later = parse_events(_load("v22_transshipment_discharged") + [
        _tev("t9", "PLN", "ARRI", "2026-11-25T08:00:00-05:00", "USBAL", created="2026-10-01T00:00:00Z")])
    assert resolve_pod(later) == "USBAL"  # 09 §A.4 limitation 3: a hint or a final DISC is needed then
    assert resolve_pod(later, "USNYC") == "USNYC"


def test_resolve_pod_disc_fallback() -> None:
    items = [
        _eev("L", "ACT", "LOAD", "2026-10-10T18:00:00+02:00", "ITGOA", "MEDU1234567"),
        _eev("D", "ACT", "DISC", "2026-11-02T22:40:00-05:00", "USNYC", "MEDU1234567"),
    ]
    assert resolve_pod(parse_events(items)) == "USNYC"
    # a DISC followed by a laden vessel LOAD of the same box is a transshipment, not the POD
    tranship = items + [_eev("L2", "ACT", "LOAD", "2026-11-05T10:00:00-05:00", "USNYC", "MEDU1234567")]
    assert resolve_pod(parse_events(tranship)) is None
    # the last final discharge wins
    two = [
        _eev("L1", "ACT", "LOAD", "2026-10-10T18:00:00+02:00", "ITGOA", "AAAU1111111"),
        _eev("D1", "ACT", "DISC", "2026-10-20T10:00:00+01:00", "MAPTM", "AAAU1111111"),
        _eev("L2", "ACT", "LOAD", "2026-10-10T18:00:00+02:00", "ITGOA", "BBBU2222222"),
        _eev("D2", "EST", "DISC", "2026-11-04T10:00:00-05:00", "USNYC", "BBBU2222222"),
    ]
    assert resolve_pod(parse_events(two)) == "USNYC"
    marked = dict(items[1], isTransshipmentMove=True)  # a transshipment move is never the final discharge
    assert resolve_pod(parse_events([items[0], marked])) is None
    not_bool = dict(items[1], isTransshipmentMove="true")  # only a real bool is read (09 §A.3.4)
    assert parse_events([not_bool])[0].is_transshipment_move is None
    assert resolve_pod(parse_events([items[0], not_bool])) == "USNYC"
    empty = [items[0], dict(items[1], emptyIndicatorCode="EMPTY")]
    assert resolve_pod(parse_events(empty)) is None


# ───────────────────────── 13. hints ─────────────────────────
def test_hint_restricts_location() -> None:
    events = parse_events(_load("v22_single_revised_est"))
    pol = booking_arrival(events, policy="first", pod_unlocode="CNSHA")
    assert pol is not None
    assert (pol.eta, pol.classifier, pol.unlocode, pol.basis) == (DT(2026, 10, 20, 5, 0), "ACT", "CNSHA",
                                                                  "TRANSPORT ARRI ACT")
    assert resolve_pod(events, " cnsha ") == "CNSHA"
    assert booking_arrival(events, policy="first", pod_unlocode="USHOU") is None
    assert select_pod_arrival(events, pod_unlocode="USHOU") is None
    assert select_pod_arrival_per_equipment(events, pod_unlocode="USHOU") == {}
    assert resolve_pod(events, "   ") == "USLAX"  # a blank hint is no hint


# ───────────────────────── 14–16. per-equipment rules ─────────────────────────
def test_post_arrival_proof() -> None:
    items = [
        _eev("L", "ACT", "LOAD", "2026-10-20T10:00:00+08:00", "CNSHA", "ABCU1234567", vessel=VESSEL_A),
        _tev("A", "EST", "ARRI", "2026-11-20T08:00:00-08:00", "USLAX", created="2026-11-04T00:00:00Z", vessel=VESSEL_A),
        _eev("G", "ACT", "GTOT", "2026-11-10T09:00:00-08:00", "USLAX", "ABCU1234567", mode="TRUCK", ftype=None,
             fac="APMT"),
    ]
    events = parse_events(items)
    per = select_pod_arrival_per_equipment(events)
    assert per["ABCU1234567"] == DcsaArrival(DT(2026, 11, 10, 9, 0), "ACT", "USLAX", "APMT", "ABCU1234567",
                                             "EQUIPMENT GTOT ACT")
    first = booking_arrival(events, policy="first")
    assert first.basis == "EQUIPMENT GTOT ACT" and first.classifier == "ACT"  # the later EST does not override it
    assert to_eta_result(first, carrier=Carrier.MAERSK, booking="267546627").is_actual is True
    assert per[None].basis == "TRANSPORT ARRI EST"

    # the earliest proof wins; EMPTY moves and other locations do not count
    more = items + [
        _eev("P", "ACT", "PICK", "2026-11-11T09:00:00-08:00", "USLAX", "ABCU1234567", mode="TRUCK", ftype=None),
        _eev("V", "ACT", "AVPU", "2026-11-09T09:00:00-08:00", "USLAX", "ABCU1234567", mode=None, ftype=None),
    ]
    assert select_pod_arrival_per_equipment(parse_events(more))["ABCU1234567"].basis == "EQUIPMENT AVPU ACT"
    empty_only = items[:2] + [dict(items[2], emptyIndicatorCode="EMPTY")]
    assert select_pod_arrival_per_equipment(parse_events(empty_only))["ABCU1234567"].basis == "TRANSPORT ARRI EST"
    est_proof = items[:2] + [dict(items[2], eventClassifierCode="EST")]
    assert select_pod_arrival_per_equipment(parse_events(est_proof))["ABCU1234567"].basis == "TRANSPORT ARRI EST"


def test_empty_box_ignored() -> None:
    items = [
        _eev("L", "ACT", "LOAD", "2026-10-20T10:00:00+08:00", "CNSHA", "ABCU1234567"),
        _tev("A", "EST", "ARRI", "2026-11-20T08:00:00-08:00", "USLAX"),
        _eev("D", "ACT", "DISC", "2026-11-12T08:00:00-08:00", "USLAX", "ABCU1234567", empty="EMPTY"),
    ]
    events = parse_events(items)
    arrival = select_pod_arrival_per_equipment(events)["ABCU1234567"]
    assert (arrival.classifier, arrival.basis, arrival.eta) == ("EST", "TRANSPORT ARRI EST", DT(2026, 11, 20, 8, 0))
    # without the vessel arrival, an empty discharge is no evidence at all
    no_arri = parse_events([items[0], items[2]])
    assert resolve_pod(no_arri) is None
    assert booking_arrival(no_arri, policy="first", pod_unlocode="USLAX") is None
    assert select_pod_arrival(no_arri, pod_unlocode="USLAX") is None


def test_none_key_and_fallback() -> None:
    loads = [
        _eev("LA", "ACT", "LOAD", "2026-10-20T10:00:00+08:00", "CNSHA", "AAAU1111111", vessel=VESSEL_A),
        _eev("LB", "ACT", "LOAD", "2026-10-20T11:00:00+08:00", "CNSHA", "BBBU2222222", vessel=VESSEL_A),
    ]
    arrival_items = [
        _tev("P", "PLN", "ARRI", "2026-11-08T06:00:00-08:00", "USLGB", created="2026-10-05T08:00:00Z", vessel=VESSEL_A),
        _tev("E", "EST", "ARRI", "2026-11-10T14:00:00-08:00", "USLGB", created="2026-11-02T07:00:00Z", vessel=VESSEL_A),
    ]
    per = select_pod_arrival_per_equipment(parse_events(loads + arrival_items), pod_unlocode="USLGB")
    assert list(per) == ["AAAU1111111", "BBBU2222222", None]  # None inserted last (08 E4)
    for key in ("AAAU1111111", "BBBU2222222", None):
        assert (per[key].eta, per[key].classifier, per[key].basis) == (DT(2026, 11, 10, 14, 0), "EST",
                                                                       "TRANSPORT ARRI EST")
        assert per[key].equipment_reference == key

    disc_b = _eev("DB", "ACT", "DISC", "2026-11-10T20:00:00-08:00", "USLGB", "BBBU2222222", fac="LBCT")
    per = select_pod_arrival_per_equipment(parse_events(loads + arrival_items + [disc_b]), pod_unlocode="USLGB")
    assert per["AAAU1111111"].basis == "TRANSPORT ARRI EST"
    assert per["BBBU2222222"] == DcsaArrival(DT(2026, 11, 10, 20, 0), "ACT", "USLGB", "LBCT", "BBBU2222222",
                                             "EQUIPMENT DISC ACT")
    assert per[None].basis == "TRANSPORT ARRI EST"
    assert list(per)[-1] is None


def test_per_equipment_eq_refs_and_vessel_fallback() -> None:
    loads = [
        _eev("LA", "ACT", "LOAD", "2026-10-24T21:15:00+02:00", "BEANR", "AAAU1111111"),
        _eev("LC", "ACT", "LOAD", "2026-10-24T22:15:00+02:00", "BEANR", "CCCU3333333"),
    ]
    arrivals = [
        _tev("RA", "EST", "ARRI", "2026-11-20T06:00:00-06:00", "USHOU", created="2026-11-01T00:00:00Z",
             refs=("AAAU1111111",)),
        _tev("RX", "EST", "ARRI", "2026-11-22T06:00:00-06:00", "USHOU", created="2026-11-02T00:00:00Z",
             refs=("XXXU9999999",)),
    ]
    events = parse_events(loads + arrivals)
    per = select_pod_arrival_per_equipment(events)
    # equipment order: EQUIPMENT events first, then TRANSPORT eq_refs
    assert list(per) == ["AAAU1111111", "CCCU3333333", "XXXU9999999", None]
    assert per["AAAU1111111"].eta == DT(2026, 11, 20, 6, 0)  # its own referenced arrival
    assert per["XXXU9999999"].eta == DT(2026, 11, 22, 6, 0)
    # CCCU3333333 is referenced by no arrival and there is no general one → (e) the vessel-level arrival
    assert per["CCCU3333333"].eta == per[None].eta == DT(2026, 11, 22, 6, 0)
    assert per["CCCU3333333"].equipment_reference == "CCCU3333333"


def test_estimated_discharge_and_disc_based_vessel_level() -> None:
    items = [
        _eev("L", "ACT", "LOAD", "2026-10-15T08:00:00+02:00", "DEHAM", "TGHU5000001"),
        _eev("DP", "PLN", "DISC", "2026-11-01T10:00:00-06:00", "USHOU", "TGHU5000001", created="2026-10-01T00:00:00Z"),
        _eev("DE", "EST", "DISC", "2026-11-03T10:00:00-06:00", "USHOU", "TGHU5000001", created="2026-10-20T00:00:00Z"),
    ]
    events = parse_events(items)
    per = select_pod_arrival_per_equipment(events, pod_unlocode="USHOU")
    assert per == {"TGHU5000001": DcsaArrival(DT(2026, 11, 3, 10, 0), "EST", "USHOU", None, "TGHU5000001",
                                              "EQUIPMENT DISC EST")}
    vessel = select_pod_arrival(events, pod_unlocode="USHOU")
    assert vessel == DcsaArrival(DT(2026, 11, 3, 10, 0), "EST", "USHOU", None, "TGHU5000001", "EQUIPMENT DISC EST")
    # no vessel arrival → no None key
    assert booking_arrival(events, policy="latest", pod_unlocode="USHOU").basis == "EQUIPMENT DISC EST"


def test_vessel_groups_drop_unidentified_arrivals_when_several_vessels_call() -> None:
    items = [
        _eev("LA", "ACT", "LOAD", "2026-10-12T11:00:00+08:00", "CNNGB", "AAAU1111111", vessel=VESSEL_A),
        _tev("A", "EST", "ARRI", "2026-11-09T10:00:00-05:00", "USSAV", vessel=VESSEL_A),
        _tev("B", "EST", "ARRI", "2026-11-16T08:00:00-05:00", "USSAV", vessel=VESSEL_B),
        _tev("N", "EST", "ARRI", "2026-11-30T08:00:00-05:00", "USSAV"),  # no vessel identity → dropped
    ]
    events = parse_events(items)
    assert select_pod_arrival(events).eta == DT(2026, 11, 16, 8, 0)
    assert select_pod_arrival_per_equipment(events)["AAAU1111111"].eta == DT(2026, 11, 9, 10, 0)
    # with a single identity, the unidentified arrival stays in the one group
    single = parse_events([items[0], items[1], items[3]])
    assert select_pod_arrival(single).eta == DT(2026, 11, 30, 8, 0)


# ───────────────────────── 17. booking policy ─────────────────────────
def test_booking_policy() -> None:
    events = parse_events(_load("v22_multi_container_two_vessels"))
    first = booking_arrival(events, policy="first")
    latest = booking_arrival(events, policy="latest")
    assert (first.equipment_reference, first.eta) == ("CMAU1111111", DT(2026, 11, 9, 10, 0))
    assert (latest.equipment_reference, latest.eta) == ("CMAU2222222", DT(2026, 11, 16, 8, 0))
    with pytest.raises(ValueError, match="policy"):
        booking_arrival(events, policy="median")  # type: ignore[arg-type]

    transport_only = parse_events([
        _tev("D", "ACT", "DEPA", "2026-10-01T16:00:00+08:00", "CNSHA"),
        _tev("A", "EST", "ARRI", "2026-11-09T10:00:00-05:00", "USNYC"),
    ])
    per = select_pod_arrival_per_equipment(transport_only)
    assert list(per) == [None]
    for policy in ("first", "latest"):
        arrival = booking_arrival(transport_only, policy=policy)
        assert (arrival.eta, arrival.equipment_reference) == (DT(2026, 11, 9, 10, 0), None)

    # "latest" breaks an eta tie by classifier rank
    tie = parse_events([
        _eev("LA", "ACT", "LOAD", "2026-10-20T10:00:00+08:00", "CNSHA", "AAAU1111111"),
        _eev("LB", "ACT", "LOAD", "2026-10-20T10:00:00+08:00", "CNSHA", "BBBU2222222"),
        _tev("A", "EST", "ARRI", "2026-11-12T08:00:00-08:00", "USLAX"),
        _eev("DB", "ACT", "DISC", "2026-11-12T08:00:00-08:00", "USLAX", "BBBU2222222"),
    ])
    assert booking_arrival(tie, policy="latest").equipment_reference == "BBBU2222222"
    assert booking_arrival(tie, policy="first").equipment_reference == "AAAU1111111"


# ───────────────────────── 18. eta_result_from_payload ─────────────────────────
def test_eta_result_from_payload(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.dcsa")
    with pytest.raises(CarrierLookupError, match="no tracking events") as excinfo:
        eta_result_from_payload([], carrier=Carrier.CMA, booking="NAM1234567", policy="latest")
    assert str(excinfo.value) == "CMA returned no tracking events for booking NAM1234567."
    shipment_only = [_load("v22_single_revised_est")[0]]
    with pytest.raises(CarrierLookupError, match="no tracking events"):
        eta_result_from_payload(shipment_only, carrier=Carrier.MAERSK, booking="267546627", policy="first")
    with pytest.raises(CarrierLookupError, match="unexpected shape") as excinfo:
        eta_result_from_payload({"foo": 1}, carrier=Carrier.MAERSK, booking="267546627", policy="first",
                                label="Maersk API")
    assert str(excinfo.value).startswith("Maersk API returned events in an unexpected shape: ")
    assert isinstance(excinfo.value.__cause__, ValueError)

    with pytest.raises(NoEtaError, match="Can't determine ETA") as no_eta:
        eta_result_from_payload(_load("v22_no_pod_yet"), carrier=Carrier.MAERSK, booking="267546627", policy="first")
    assert str(no_eta.value) == (
        "No arrival at the port of discharge published by Maersk for booking 267546627 (events seen: "
        "{'EQUIPMENT/GTIN/ACT': 1, 'EQUIPMENT/LOAD/ACT': 1, 'TRANSPORT/ARRI/ACT': 1}). Can't determine ETA.")

    assert not caplog.records
    result = eta_result_from_payload(_load("v22_multi_container_two_vessels"), carrier=Carrier.CMA,
                                     booking="NAM1234567", policy="latest")
    assert result.eta == DT(2026, 11, 16, 8, 0)
    assert result.source == "DCSA TRANSPORT ARRI EST at USSAV/GCT"
    assert result.containers == ("CMAU1111111", "CMAU2222222")
    assert result.is_actual is False
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "[DCSA] containers on booking NAM1234567 disagree on arrival" in warnings[0].getMessage()
    assert warnings[0].name == "eta.carrier.dcsa"
    assert result.details["policy"] == "latest"
    assert result.details["pod"] == "USSAV"
    assert result.details["event_count"] == 7
    assert result.details["basis"] == "TRANSPORT ARRI EST"
    assert result.details["per_container"] == {
        "CMAU1111111": {"eta": "2026-11-09T10:00:00", "classifier": "EST", "basis": "TRANSPORT ARRI EST"},
        "CMAU2222222": {"eta": "2026-11-16T08:00:00", "classifier": "EST", "basis": "TRANSPORT ARRI EST"},
    }
    json.dumps(result.to_dict())  # JSON-serializable details

    caplog.clear()
    shipsgo = eta_result_from_payload(_load("v22_transshipment_discharged"), carrier=Carrier.MSC,
                                      booking="EBKG12345678", policy="first", source_prefix="ShipsGo")
    assert shipsgo.source == "ShipsGo EQUIPMENT DISC ACT at USNYC/PNCT"
    assert shipsgo.is_actual is True
    assert not caplog.records  # one container: nothing to disagree about
    with pytest.raises(ValueError):
        eta_result_from_payload(_load("v22_single_revised_est"), carrier=Carrier.MAERSK, booking="267546627",
                                policy="sometimes")  # type: ignore[arg-type]


# ───────────────────────── 19. to_local_naive ─────────────────────────
@pytest.mark.parametrize(
    ("value", "hint", "expected"),
    [
        ("2026-10-20T08:00:00Z", "Europe/Rome", DT(2026, 10, 20, 10, 0)),
        ("2026-10-20T08:00:00Z", "+02:00", DT(2026, 10, 20, 10, 0)),
        ("2026-10-20T08:00:00Z", "UTC-5", DT(2026, 10, 20, 3, 0)),
        ("2026-10-20T08:00:00Z", "GMT+0530", DT(2026, 10, 20, 13, 30)),
        ("2026-10-20T08:00:00Z", "Mars/Olympus", DT(2026, 10, 20, 8, 0)),
        ("2026-10-20T08:00:00", "America/New_York", DT(2026, 10, 20, 8, 0)),
        ("2026-11-03T02:40:00Z", "America/New_York", DT(2026, 11, 2, 21, 40)),
    ],
)
def test_to_local_naive_table(value: str, hint: str, expected: datetime) -> None:
    result = to_local_naive(value, hint)
    assert result == expected
    assert result.tzinfo is None


def test_to_local_naive_edge_cases() -> None:
    assert to_local_naive(None, "Europe/Rome") is None
    assert to_local_naive("not a date", "Europe/Rome") is None
    assert to_local_naive("2026-10-20T08:00:00+02:00") == DT(2026, 10, 20, 8, 0)  # no hint: given offset's wall time
    assert to_local_naive("2026-10-20T08:00:00+02:00", "Europe/Rome") == DT(2026, 10, 20, 8, 0)  # idempotent
    assert to_local_naive("2026-10-20T08:00:00Z", "   ") == DT(2026, 10, 20, 8, 0)
    assert to_local_naive("2026-10-20T08:00:00Z", "Z") == DT(2026, 10, 20, 8, 0)
    assert to_local_naive("2026-10-20T08:00:00Z", "gmt") == DT(2026, 10, 20, 8, 0)
    assert to_local_naive("2026-10-20T08:00:00Z", "UTC+5:30") == DT(2026, 10, 20, 13, 30)
    assert to_local_naive("2026-10-20T08:00:00Z", "+99:00") == DT(2026, 10, 20, 8, 0)  # invalid offset → no tz
    assert to_local_naive("2026-10-20T08:00:00Z", "../etc/passwd") == DT(2026, 10, 20, 8, 0)
    aware = DT(2026, 10, 20, 8, 0, tzinfo=timezone.utc)
    assert to_local_naive(aware, "Asia/Shanghai") == DT(2026, 10, 20, 16, 0)
    assert to_local_naive(date(2026, 10, 20), "Asia/Shanghai") == DT(2026, 10, 20, 0, 0)
    assert to_local_naive("0001-01-01T00:00:00Z", "-05:00") is None  # overflow never raises


# ───────────────────────── 20. explain_selection ─────────────────────────
def test_explain_selection() -> None:
    events = parse_events(_load("v22_single_revised_est"))
    lines = explain_selection(events)
    assert all(isinstance(line, str) for line in lines)
    text = "\n".join(lines)
    assert "USLAX" in text
    assert "2026-11-13T09:30" in text
    assert "loading ports excluded: CNSHA" in text
    assert "MSKU1234565" in text
    assert "9778791" in text  # vessel identity of the arrivals at the POD

    hinted = "\n".join(explain_selection(events, pod_unlocode="CNSHA", policy="latest"))
    assert "from hint" in hinted and "2026-10-20T05:00" in hinted
    missing = "\n".join(explain_selection(parse_events(_load("v22_no_pod_yet"))))
    assert "not found" in missing and "no arrival" in missing
    finals = parse_events([
        _eev("L", "ACT", "LOAD", "2026-10-10T18:00:00+02:00", "ITGOA", "MEDU1234567"),
        _eev("D", "ACT", "DISC", "2026-11-02T22:40:00-05:00", "USNYC", "MEDU1234567"),
    ])
    assert "final discharge" in "\n".join(explain_selection(finals))
    assert "invalid policy" in "\n".join(explain_selection(events, policy="median"))
    utc_note = "\n".join(explain_selection(parse_events(MSC_API_IN_TRANSIT)))
    assert "GHTEM" in utc_note and "2026-11-12T06:00" in utc_note and "UTC" in utc_note
    json.dumps(lines)


# ───────────────────────── 21. fetch_event_pages (respx) ─────────────────────────
API = "https://api.dcsa.test/v2/events"
PAGE_1 = [_tev("T1", "ACT", "DEPA", "2026-10-21T18:00:00+08:00", "CNNGB"),
          _eev("E1", "ACT", "LOAD", "2026-10-21T08:40:00+08:00", "CNNGB", "MSKU7777777")]
PAGE_2 = [_tev("T2", "EST", "ARRI", "2026-11-20T09:00:00-05:00", "USNYC")]


class _FakeAuth:
    """Minimal TokenProvider (03 §8) for the pagination helper."""

    def __init__(self) -> None:
        self.tokens = ["tok-1", "tok-2"]
        self.invalidations = 0

    async def get_token(self) -> str:
        return self.tokens[min(self.invalidations, len(self.tokens) - 1)]

    async def invalidate(self) -> None:
        self.invalidations += 1


async def test_fetch_event_pages_cursor_form() -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(API)
        route.side_effect = [
            httpx.Response(200, json={"events": PAGE_1}, headers={"Current-Page": "1", "Next-Page": "2"}),
            httpx.Response(200, json=PAGE_2, headers={"Current-Page": "2"}),
        ]
        async with httpx.AsyncClient() as client:
            events, summaries = await fetch_event_pages(
                client, API, service="dcsa-test", label="Test",
                params={"carrierBookingReference": "210000007", "limit": "100"},
                headers={"Consumer-Key": "abc"})
    assert route.call_count == 2
    first, second = (call.request for call in route.calls)
    assert dict(first.url.params) == {"carrierBookingReference": "210000007", "limit": "100"}
    assert dict(second.url.params) == {"carrierBookingReference": "210000007", "limit": "100", "cursor": "2"}
    assert second.headers["Consumer-Key"] == "abc"
    assert [e["eventID"] for e in events] == ["T1", "E1", "T2"]
    assert [s["page"] for s in summaries] == [1, 2]
    assert summaries[0]["events"] == 2 and summaries[0]["next_page"] == "2" and summaries[0]["current_page"] == "1"
    assert summaries[1]["next_page"] is None and summaries[1]["status"] == 200
    assert set(summaries[0]) == {"page", "status", "events", "elapsed_ms", "current_page", "next_page", "url"}
    assert summaries[0]["url"].startswith(API)
    # the collected raw events feed the parser directly
    assert booking_arrival(parse_events(events), policy="first").eta == DT(2026, 11, 20, 9, 0)


async def test_fetch_event_pages_same_host_absolute_url_and_path() -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(API)
        route.side_effect = [
            httpx.Response(200, json=PAGE_1, headers={"Next-Page": f"{API}?cursor=abc&limit=100"}),
            httpx.Response(200, json=PAGE_2, headers={"Next-Page": "/v2/events?cursor=def"}),
            httpx.Response(200, json=[], headers={"Next-Page": "ghi"}),  # an empty page stops paging
        ]
        async with httpx.AsyncClient() as client:
            events, summaries = await fetch_event_pages(
                client, API, service="dcsa-test", label="Test", params={"carrierBookingReference": "X"})
    assert route.call_count == 3
    urls = [call.request.url for call in route.calls]
    assert dict(urls[1].params) == {"cursor": "abc", "limit": "100"}  # followed with its own query
    assert dict(urls[2].params) == {"cursor": "def"}
    assert len(events) == 3 and len(summaries) == 3


async def test_fetch_event_pages_foreign_host_not_followed(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.dcsa")
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(API)
        route.side_effect = [httpx.Response(200, json=PAGE_1,
                                            headers={"Next-Page": "https://evil.example.test/v2/events?cursor=2"})]
        async with httpx.AsyncClient() as client:
            events, _ = await fetch_event_pages(client, API, service="dcsa-test", label="Test", params={})
    assert route.call_count == 1
    assert len(events) == 2
    assert "not followed" in caplog.text and "evil.example.test" in caplog.text


async def test_fetch_event_pages_never_follows_a_protocol_relative_link_to_another_host(
        caplog: pytest.LogCaptureFixture) -> None:
    """MSK-03 (09 §A.3.12): '//other-host/…' is a network-path reference — resolved, it points to another host, so it
    is never followed and the bearer token never leaves the origin."""
    caplog.set_level(logging.WARNING, logger="eta.carrier.dcsa")
    async with respx.mock(assert_all_called=False) as router:
        route = router.get(API)
        route.side_effect = [httpx.Response(200, json=PAGE_1, headers={"Next-Page": "//evil.example.test/x?cursor=2"})]
        evil = router.route(host="evil.example.test").mock(return_value=httpx.Response(200, json=PAGE_2))
        async with httpx.AsyncClient() as client:
            events, _ = await fetch_event_pages(client, API, service="dcsa-test", label="Test", params={},
                                                auth=_FakeAuth())
    assert route.call_count == 1 and evil.call_count == 0
    assert len(events) == 2
    assert "not followed" in caplog.text and "evil.example.test" in caplog.text


@pytest.mark.parametrize("link", ["/v2/events?cursor=\x01", "https://api.dcsa.test/v2/events?cursor=\x01",
                                  "https://api.dcsa.test:abc/v2/events", "//[::1/v2/events"])
async def test_fetch_event_pages_stops_at_a_malformed_link(caplog: pytest.LogCaptureFixture, link: str) -> None:
    """MSK-03: a Next-Page link that is not a valid URL stops paging with a WARNING (the events read so far are kept)
    instead of raising httpx.InvalidURL, which is no httpx.HTTPError and would escape a carrier's get_eta()."""
    caplog.set_level(logging.WARNING, logger="eta.carrier.dcsa")
    async with respx.mock(assert_all_called=False) as router:
        route = router.get(API)
        route.side_effect = [httpx.Response(200, json=PAGE_1, headers={"Next-Page": link})]
        async with httpx.AsyncClient() as client:
            events, summaries = await fetch_event_pages(client, API, service="dcsa-test", label="Test", params={})
    assert route.call_count == 1 and len(events) == 2 and len(summaries) == 1
    assert any("Next-Page" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_fetch_event_pages_never_downgrades_to_http(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.dcsa")
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(API)
        route.side_effect = [httpx.Response(200, json=PAGE_1,
                                            headers={"Next-Page": "http://api.dcsa.test/v2/events?cursor=2"})]
        async with httpx.AsyncClient() as client:
            await fetch_event_pages(client, API, service="dcsa-test", label="Test", params={},
                                    auth=_FakeAuth())
    assert route.call_count == 1
    assert "not followed" in caplog.text


async def test_fetch_event_pages_max_pages_cap(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.dcsa")
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(API).mock(
            return_value=httpx.Response(200, json=PAGE_2, headers={"Current-Page": "n", "Next-Page": "more"}))
        async with httpx.AsyncClient() as client:
            events, summaries = await fetch_event_pages(client, API, service="dcsa-test", label="Test",
                                                        params={"limit": "1"}, max_pages=3)
    assert route.call_count == 3
    assert len(events) == 3 and len(summaries) == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "stopped after 3 pages" in warnings[0].getMessage()


async def test_fetch_event_pages_stops_when_next_equals_current() -> None:
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(API).mock(
            return_value=httpx.Response(200, json=PAGE_2, headers={"Current-Page": "7", "Next-Page": "7"}))
        async with httpx.AsyncClient() as client:
            events, _ = await fetch_event_pages(client, API, service="dcsa-test", label="Test", params={})
    assert route.call_count == 1 and len(events) == 1


async def test_fetch_event_pages_auth_semaphore_and_reauth() -> None:
    auth = _FakeAuth()
    sem = asyncio.Semaphore(1)
    async with respx.mock(assert_all_called=True) as router:
        route = router.get(API)
        route.side_effect = [
            httpx.Response(401, json={"message": "expired"}),
            httpx.Response(200, json={"data": PAGE_2}),
        ]
        async with httpx.AsyncClient() as client:
            events, summaries = await fetch_event_pages(client, API, service="dcsa-test", label="Test",
                                                        params={"x": "1"}, auth=auth, sem=sem)
    assert [call.request.headers["Authorization"] for call in route.calls] == ["Bearer tok-1", "Bearer tok-2"]
    assert auth.invalidations == 1
    assert len(events) == 1 and len(summaries) == 1
    assert not sem.locked()


async def test_fetch_event_pages_errors() -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.get(API).mock(return_value=httpx.Response(200, json={"foo": 1, "bar": [1]}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(CarrierLookupError) as excinfo:
                await fetch_event_pages(client, API, service="dcsa-test", label="Maersk", params={})
    assert str(excinfo.value) == "Maersk returned an unexpected payload shape (top-level keys: ['bar', 'foo'])"

    async with respx.mock(assert_all_called=True) as router:
        router.get(API).mock(return_value=httpx.Response(404, json={"message": "Not Found"}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as service_error:
                await fetch_event_pages(client, API, service="dcsa-test", label="Test", params={})
    assert service_error.value.status == 404  # carriers map ServiceError to their own messages

    auth = _FakeAuth()
    async with respx.mock(assert_all_called=True) as router:
        router.get(API).mock(return_value=httpx.Response(401, json={"message": "nope"}))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AuthError):
                await fetch_event_pages(client, API, service="dcsa-test", label="Test", params={}, auth=auth)
