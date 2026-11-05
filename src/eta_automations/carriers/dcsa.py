"""Shared DCSA Track & Trace parser and port-of-discharge (POD) arrival rules (09 Part A).

Used by Maersk (07 §7), CMA CGM (08 §7), MSC's API rescue path (06 §7.5) and the aggregator (09 Part B).

Everything here is **pure** (no I/O, no clock, no settings) except the optional pagination helper
`fetch_event_pages()` (09 §A.3.12). Logger `eta.carrier.dcsa`: DEBUG only, except the multi-container
WARNING of `eta_result_from_payload()` (09 §A.3.10) and the paging WARNINGs of `fetch_event_pages()`.

Accepted payloads (09 §A.2): DCSA T&T 2.2 bare arrays, wrapped arrays (`{"events": [...]}` …), single
events, the inferred v3 `{"metadata", "payload"}` envelopes with withdrawals, and the carrier dialects of
Maersk, CMA and MSC (camelCase keys — every key lookup is case-insensitive, 09 §A.3.4).

Times: `eventDateTime` is the carrier's LOCAL wall time; the offset is parsed and dropped, never
converted (03 §4). `eventCreatedDateTime` is converted to naive UTC and only compared with other created
timestamps (revision tie-breaks, 09 §A.3.5).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from types import MappingProxyType
from typing import Any, Literal, TypeAlias
from zoneinfo import ZoneInfo

import httpx

from ..auth import TokenProvider, request_with_auth
from ..errors import CarrierLookupError, NoEtaError
from ..http import AsyncLimiter, json_body, request
from ..logging_setup import redact
from ..models import Carrier, EtaResult

__all__ = [
    "CLASSIFIER_RANK",
    "PLAUSIBLE_YEARS",
    "POST_ARRIVAL_CODES",
    "WRAPPER_KEYS",
    "DcsaArrival",
    "DcsaEvent",
    "Policy",
    "booking_arrival",
    "eta_result_from_payload",
    "explain_selection",
    "fetch_event_pages",
    "is_final_disc",
    "is_laden",
    "is_usable",
    "is_vessel_arrival",
    "is_vessel_ish",
    "naive_local",
    "naive_utc",
    "parse_dcsa_datetime",
    "parse_events",
    "rank_key",
    "resolve_pod",
    "select_pod_arrival",
    "select_pod_arrival_per_equipment",
    "to_eta_result",
    "to_local_naive",
    "vessel_identity",
]

log = logging.getLogger("eta.carrier.dcsa")

# ── 09 §A.3.1 constants and types ──
CLASSIFIER_RANK: Mapping[str, int] = MappingProxyType({"ACT": 3, "EST": 2, "PLN": 1})
"""ACT > EST > PLN; anything else (REQ, unknown) ranks 0 = unusable."""
PLAUSIBLE_YEARS = range(2000, 2101)
"""Event dates outside this range are skipped (CMA sentinels 0001-01-01 / 9999-12-31)."""
WRAPPER_KEYS = ("events", "data", "items", "content", "results")
"""Accepted list keys of wrapped payloads (Maersk wraps the DCSA array as {"events": [...]})."""
POST_ARRIVAL_CODES = ("GTOT", "AVPU", "PICK")
"""Laden ACT events at the POD that prove arrival (the box is out of the terminal)."""
Policy: TypeAlias = Literal["first", "latest"]

_POLICIES: tuple[str, ...] = ("first", "latest")
_EVENT_TYPES = frozenset({"TRANSPORT", "EQUIPMENT", "SHIPMENT"})
_SELECTABLE_TYPES = frozenset({"TRANSPORT", "EQUIPMENT"})
_FRACTION_RE = re.compile(r"([.,]\d{6})\d+")
_OFFSET_HINT_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2}):?(\d{2})?$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"[0-9]+")
_UTC_SUFFIX_RE = re.compile(r"(?:Z|[+-]00:?00)\s*$", re.IGNORECASE)
_TYPE_CODE_KEYS = ("transporteventtypecode", "equipmenteventtypecode", "shipmenteventtypecode", "eventtypecode")
_ENVELOPE_KEYS = ("eventtype", "eventid", "eventcreateddatetime")


# ── 09 §A.3.2 dataclasses (the added fields are defaulted, so positional construction still works) ──
@dataclass(frozen=True, slots=True)
class DcsaEvent:
    event_type: str                     # "TRANSPORT" | "EQUIPMENT" | "SHIPMENT"
    classifier: str                     # "ACT" | "EST" | "PLN" | "REQ"
    type_code: str                      # "ARRI", "DEPA", "DISC", "LOAD", "GTOT", ...
    event_datetime: datetime            # naive local wall time (offset dropped)
    created_datetime: datetime | None   # eventCreatedDateTime (aware → naive UTC), for tie-breaks
    unlocode: str | None
    facility_code: str | None
    facility_type: str | None           # e.g. "POTE"
    mode_of_transport: str | None       # "VESSEL" | "RAIL" | "TRUCK" | "BARGE"
    equipment_reference: str | None
    vessel_name: str | None
    transport_call_sequence: int | None
    raw: Mapping[str, Any]              # the ORIGINAL item dict, unmodified (08 reads raw["carrierSpecificData"], ...)
    # ── additions (09) ──
    event_id: str | None = None
    transport_call_key: str | None = None      # transportCallID (v2.2) / transportCallReference (v3)
    vessel_imo: str | None = None
    voyage: str | None = None
    empty_indicator: str | None = None         # "EMPTY" | "LADEN" | None
    is_transshipment_move: bool | None = None  # v3 only
    eq_refs: tuple[str, ...] = ()              # container numbers from references[type == "EQ"]
    index: int = 0                             # position in the source list: the last, stable tie-break


@dataclass(frozen=True, slots=True)
class DcsaArrival:
    eta: datetime                       # naive local wall time
    classifier: str                     # "ACT" | "EST" | "PLN"
    unlocode: str | None
    facility_code: str | None
    equipment_reference: str | None
    basis: str                          # "TRANSPORT ARRI EST", "EQUIPMENT DISC ACT", "EQUIPMENT GTOT ACT", ...


# ── 09 §A.3.3 datetime helpers ──
def parse_dcsa_datetime(value: Any) -> datetime | None:
    """ISO-8601 → datetime (aware if an offset/Z is present, else naive). Never raises.

    Accepts 'Z', '+0000', '+05:30', 7 fractional digits (.NET), a space separator, date-only (→ 00:00)
    and the basic format (`20261112T080000Z`).
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if not s:
        return None
    if len(s) > 10 and s[10] == " ":  # "2026-11-12 08:00:00"
        s = s[:10] + "T" + s[11:]
    s = _FRACTION_RE.sub(r"\1", s)  # .NET "…59.9999999" → 6 digits
    try:
        return datetime.fromisoformat(s)
    except (ValueError, OverflowError, TypeError):
        return None


def naive_local(dt: datetime | None) -> datetime | None:
    """Drop tzinfo WITHOUT converting: keeps the local wall-clock time (03 §4)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None)


def naive_utc(dt: datetime | None) -> datetime | None:
    """Aware → convert to UTC, then drop tzinfo; naive → unchanged.

    (OverflowError, ValueError) → None: year-1 times with a positive offset overflow.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt
    if dt.utcoffset() is None:  # tzinfo without an offset counts as naive in Python
        return dt.replace(tzinfo=None)
    try:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    except (OverflowError, ValueError):
        return None


def _tz_from_hint(tz_hint: str | None) -> tzinfo | None:
    """blank → None; 'Z'/'UTC'/'GMT' → UTC; '+02:00' / 'UTC-5' / 'GMT+0530' → fixed offset;
    otherwise an IANA zone name via zoneinfo (tzdata on Windows); invalid → None (09 §A.3.3)."""
    if tz_hint is None or not isinstance(tz_hint, str):
        return None
    hint = tz_hint.strip()
    if not hint:
        return None
    if hint.upper() in ("Z", "UTC", "GMT"):
        return timezone.utc
    match = _OFFSET_HINT_RE.match(hint)
    if match:
        sign, hours, minutes = match.groups()
        h, m = int(hours), int(minutes or 0)
        if h > 23 or m > 59:
            return None
        delta = timedelta(hours=h, minutes=m)
        return timezone(-delta if sign == "-" else delta)
    try:
        return ZoneInfo(hint)
    except (KeyError, ValueError, OSError, TypeError):  # ZoneInfoNotFoundError is a KeyError
        return None


def to_local_naive(value: str | datetime | None, tz_hint: str | None = None) -> datetime | None:
    """Port-local naive wall time (used by the aggregator, whose providers send UTC plus the port's tz).

    An aware value is converted to `tz_hint` (idempotent when it already carries the local offset), then
    made naive. A naive value is returned unchanged. With a blank/invalid hint the given offset's wall
    time is kept. Never raises: unparsable input, or a conversion that overflows (sentinel years) → None.
    """
    if isinstance(value, datetime):
        dt: datetime | None = value
    elif isinstance(value, date):
        dt = datetime.combine(value, dtime.min)
    elif isinstance(value, str):
        dt = parse_dcsa_datetime(value)
    else:
        return None
    if dt is None:
        return None
    tz = _tz_from_hint(tz_hint)
    if tz is not None and dt.tzinfo is not None and dt.utcoffset() is not None:
        try:
            dt = dt.astimezone(tz)
        except (OverflowError, ValueError):
            return None
    return dt.replace(tzinfo=None)


# ── 09 §A.3.4 parse_events ──
def _ci(m: Mapping[Any, Any]) -> dict[str, Any]:
    """Case-insensitive view: keys case-folded, the FIRST occurrence wins when two differ only in case."""
    out: dict[str, Any] = {}
    for key, value in m.items():
        out.setdefault(str(key).casefold(), value)
    return out


def _ci_child(m: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Case-insensitive view of a nested object (`{}` when missing or not an object)."""
    value = m.get(key)
    return _ci(value) if isinstance(value, Mapping) else {}


def _is_nonempty(value: Any) -> bool:
    """09 §A.3.4: "non-empty" means not None and not a blank string."""
    return value is not None and not (isinstance(value, str) and not value.strip())


def _text(value: Any) -> str | None:
    """A scalar as a stripped string; None for blank strings, None, booleans and containers."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else str(value)
    return None


def _first_text(*values: Any) -> str | None:
    """The first non-empty value, as a stripped string (09 §A.3.4 step 4)."""
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def _code(*values: Any) -> str | None:
    """Like _first_text, upper-cased (UN/LOCODEs, facility/mode codes, container numbers)."""
    text = _first_text(*values)
    return text.upper() if text is not None else None


def _sequence_number(value: Any) -> int | None:
    """transportCallSequenceNumber: an int, or a digit string converted with int(); else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _DIGITS_RE.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _eq_refs(references: Any) -> tuple[str, ...]:
    """Container numbers from references[] whose type/referenceType is "EQ" (value/referenceValue)."""
    if not isinstance(references, (list, tuple)):
        return ()
    seen: dict[str, None] = {}
    for ref in references:
        if not isinstance(ref, Mapping):
            continue
        r = _ci(ref)
        ref_type = _code(r.get("type"), r.get("referencetype"))
        if ref_type != "EQ":
            continue
        number = _code(r.get("value"), r.get("referencevalue"))
        if number:
            seen.setdefault(number, None)
    return tuple(seen)


def _short(value: Any, limit: int = 60) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _find_list(payload: Any) -> list[Any] | tuple[Any, ...]:
    """09 §A.3.4 step 1: a list as-is; a Mapping's first list under WRAPPER_KEYS; a single event."""
    if isinstance(payload, (list, tuple)):
        return payload
    if isinstance(payload, Mapping):
        top = _ci(payload)
        for key in WRAPPER_KEYS:
            value = top.get(key)
            if isinstance(value, list):
                return value
        if "eventtype" in top or "metadata" in top:
            return [payload]
        keys = sorted(str(k) for k in payload.keys())
        raise ValueError(f"unrecognised DCSA payload shape: {type(payload).__name__} keys={keys[:10]}")
    raise ValueError(f"unrecognised DCSA payload shape: {type(payload).__name__} keys=[]")


def _flatten(item: Mapping[str, Any], retracted: set[str]) -> tuple[dict[str, Any] | None, str]:
    """v3 envelope → flat case-folded dict (+ withdrawals); v2.2 item → its case-folded view.

    Returns (flat, reason); flat is None when the item produces no event.
    """
    top = _ci(item)
    metadata = top.get("metadata")
    if not isinstance(metadata, Mapping):
        return top, ""
    meta = _ci(metadata)
    retracted_id = _text(meta.get("retractedeventid"))
    if retracted_id:
        retracted.add(retracted_id)
    payload = top.get("payload")
    if not isinstance(payload, Mapping) or not payload:
        reason = f"v3 withdrawal of event {retracted_id!r}" if retracted_id else "v3 envelope without payload"
        return None, reason
    flat = _ci(payload)
    for key in _ENVELOPE_KEYS:  # flat.setdefault(...) from metadata (also fills blank values)
        if not _is_nonempty(flat.get(key)) and key in meta:
            flat[key] = meta[key]
    return flat, ""


def _parse_item(item: Mapping[str, Any], index: int, retracted: set[str]) -> DcsaEvent | None:
    """One list item → DcsaEvent, or None (DEBUG-logged) when it is not a usable event (09 §A.3.4)."""
    flat, reason = _flatten(item, retracted)
    if flat is None:
        log.debug("[DCSA] skipped event #%d: %s", index, reason)
        return None

    event_type = (_text(flat.get("eventtype")) or "").upper()
    if event_type not in _EVENT_TYPES:
        log.debug("[DCSA] skipped event #%d: unknown eventType %s", index, _short(flat.get("eventtype")))
        return None
    type_code = _code(*(flat.get(k) for k in _TYPE_CODE_KEYS))
    if not type_code:
        log.debug("[DCSA] skipped event #%d: no event type code", index)
        return None
    classifier = _code(flat.get("eventclassifiercode"))
    if not classifier:
        log.debug("[DCSA] skipped event #%d: no eventClassifierCode", index)
        return None
    when = parse_dcsa_datetime(flat.get("eventdatetime"))
    if when is None:
        log.debug("[DCSA] skipped event #%d: unparsable eventDateTime %s", index, _short(flat.get("eventdatetime")))
        return None
    if when.year not in PLAUSIBLE_YEARS:  # sentinels 0001/9999 (never converted, so never overflow)
        log.debug("[DCSA] skipped event #%d: eventDateTime year %d outside %d-%d",
                  index, when.year, PLAUSIBLE_YEARS.start, PLAUSIBLE_YEARS.stop - 1)
        return None

    created = naive_utc(parse_dcsa_datetime(flat.get("eventcreateddatetime")))
    if created is not None and created.year not in PLAUSIBLE_YEARS:
        created = None  # the event is kept

    tc = _ci_child(flat, "transportcall")
    tcl = _ci_child(tc, "location")
    el = _ci_child(flat, "eventlocation")
    vessel = _ci_child(tc, "vessel")
    transshipment = flat.get("istransshipmentmove")

    return DcsaEvent(
        event_type=event_type,
        classifier=classifier,
        type_code=type_code,
        event_datetime=when.replace(tzinfo=None),  # naive_local(): keep the local wall time (03 §4)
        created_datetime=created,
        unlocode=_code(tc.get("unlocationcode"), tcl.get("unlocationcode"), el.get("unlocationcode")),
        facility_code=_code(tc.get("facilitycode"), tcl.get("facilitycode"), el.get("facilitycode")),
        facility_type=_code(tc.get("facilitytypecode"), flat.get("facilitytypecode")),
        mode_of_transport=_code(tc.get("modeoftransport")),
        equipment_reference=_code(flat.get("equipmentreference")),
        vessel_name=_first_text(vessel.get("vesselname"), vessel.get("name")),
        transport_call_sequence=_sequence_number(tc.get("transportcallsequencenumber")),
        raw=item,
        event_id=_first_text(flat.get("eventid")),
        transport_call_key=_first_text(tc.get("transportcallid"), tc.get("transportcallreference")),
        vessel_imo=_first_text(vessel.get("vesselimonumber")),
        voyage=_first_text(
            tc.get("importvoyagenumber"),
            tc.get("carrierimportvoyagenumber"),
            tc.get("carriervoyagenumber"),
            tc.get("exportvoyagenumber"),
            tc.get("carrierexportvoyagenumber"),
        ),
        empty_indicator=_code(flat.get("emptyindicatorcode")),
        is_transshipment_move=transshipment if isinstance(transshipment, bool) else None,
        eq_refs=_eq_refs(flat.get("references")),
        index=index,
    )


def parse_events(payload: Any) -> list[DcsaEvent]:
    """DCSA T&T payload → events in source order (09 §A.3.4).

    Accepts a bare list, a Mapping wrapping the list under WRAPPER_KEYS, or a single event object
    (v2.2 or v3 envelope). Any other shape raises ValueError("unrecognised DCSA payload shape: …");
    carrier clients map it to CarrierLookupError. Never raises for a single bad item: non-objects,
    unknown event types, missing type code/classifier, unparsable or implausible dates are skipped
    (DEBUG). v3 withdrawals (metadata.retractedEventID) remove their target event. SHIPMENT events are
    returned; the selection functions ignore them.
    """
    items = _find_list(payload)
    retracted: set[str] = set()
    events: list[DcsaEvent] = []
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            log.debug("[DCSA] skipped event #%d: not an object (%s)", index, type(item).__name__)
            continue
        try:
            event = _parse_item(item, index, retracted)
        except (TypeError, ValueError, AttributeError, KeyError, IndexError, OverflowError) as exc:
            log.debug("[DCSA] skipped event #%d: %s", index, _short(exc, 120))
            continue
        if event is not None:
            events.append(event)
    if retracted:
        withdrawn = [e for e in events if e.event_id is not None and e.event_id in retracted]
        if withdrawn:
            log.debug("[DCSA] withdrew %d event(s): %s", len(withdrawn), [e.event_id for e in withdrawn])
            events = [e for e in events if e.event_id is None or e.event_id not in retracted]
    return events


# ── small normalisers (hand-built events from the aggregator/tests may be less tidy than parsed ones) ──
def _up(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text or None


def _rank(e: DcsaEvent) -> int:
    return CLASSIFIER_RANK.get((e.classifier or "").strip().upper(), 0)


def _local(dt: datetime | None) -> datetime:
    return naive_local(dt) or datetime.min


def _is(e: DcsaEvent, event_type: str, type_code: str | None = None) -> bool:
    if _up(e.event_type) != event_type:
        return False
    return type_code is None or _up(e.type_code) == type_code


# ── 09 §A.3.5 ranking ──
def rank_key(e: DcsaEvent) -> tuple[int, datetime, datetime, int]:
    """ACT > EST > PLN, then the newest eventCreatedDateTime (revisions), then the later eventDateTime,
    then the later position in the payload. Never raises on a mix of naive and aware datetimes."""
    return (
        _rank(e),
        naive_utc(e.created_datetime) or datetime.min,
        _local(e.event_datetime),
        e.index,
    )


def _latest_key(e: DcsaEvent) -> tuple[datetime, int, int]:
    """(event_datetime, CLASSIFIER_RANK, index): "the latest" among representatives (09 §A.3.7–A.3.8)."""
    return (_local(e.event_datetime), _rank(e), e.index)


# ── 09 §A.3.6 predicates ──
def is_usable(e: DcsaEvent) -> bool:
    """TRANSPORT/EQUIPMENT event with a known classifier (ACT/EST/PLN)."""
    return _up(e.event_type) in _SELECTABLE_TYPES and _rank(e) > 0


def is_laden(e: DcsaEvent) -> bool:
    """Not marked EMPTY (None counts as laden)."""
    return _up(e.empty_indicator) != "EMPTY"


def is_vessel_ish(e: DcsaEvent) -> bool:
    """VESSEL mode, or no mode at a port terminal / unknown facility type."""
    mode = _up(e.mode_of_transport)
    if mode == "VESSEL":
        return True
    return mode is None and _up(e.facility_type) in (None, "POTE")


def is_vessel_arrival(e: DcsaEvent) -> bool:
    """TRANSPORT ARRI with a UN/LOCODE, by vessel (rail/truck arrivals are not vessel arrivals)."""
    return _is(e, "TRANSPORT", "ARRI") and bool(_up(e.unlocode)) and is_vessel_ish(e)


def vessel_identity(e: DcsaEvent) -> str | None:
    """IMO number, else the upper-cased vessel name, else None."""
    imo = (e.vessel_imo or "").strip()
    if imo:
        return imo
    name = (e.vessel_name or "").strip().upper()
    return name or None


def _is_laden_vessel_load(e: DcsaEvent) -> bool:
    return _is(e, "EQUIPMENT", "LOAD") and is_laden(e) and is_vessel_ish(e)


def is_final_disc(e: DcsaEvent, evs: Iterable[DcsaEvent]) -> bool:
    """Laden vessel discharge, not a transshipment move, not followed by a laden vessel LOAD of the same box."""
    if not (_is(e, "EQUIPMENT", "DISC") and _up(e.unlocode) and is_laden(e) and is_vessel_ish(e)):
        return False
    if e.is_transshipment_move is True:
        return False
    when = _local(e.event_datetime)
    box = _up(e.equipment_reference)
    return not any(
        _is_laden_vessel_load(l) and _up(l.equipment_reference) == box and _local(l.event_datetime) > when
        for l in evs
    )


def _is_pod_disc(e: DcsaEvent, pod: str) -> bool:
    """Laden, non-transshipment DISC at the POD (09 §A.3.8 step 3, §A.3.9 a and d)."""
    return (
        _is(e, "EQUIPMENT", "DISC")
        and _up(e.unlocode) == pod
        and is_laden(e)
        and e.is_transshipment_move is not True
    )


# ── 09 §A.3.7 resolve_pod ──
def _usable(events: Iterable[DcsaEvent]) -> list[DcsaEvent]:
    return [e for e in events if is_usable(e)]


def _hint(pod_unlocode: str | None) -> str | None:
    return _up(pod_unlocode) if isinstance(pod_unlocode, str) else None


def _loading_locations(evs: Sequence[DcsaEvent]) -> set[str]:
    """UN/LOCODEs of laden vessel LOADs: port of loading and transshipment ports, never the POD."""
    return {loc for e in evs if _is_laden_vessel_load(e) and (loc := _up(e.unlocode))}


def _resolve_pod_detail(events: Iterable[DcsaEvent], pod_unlocode: str | None) -> tuple[str | None, str]:
    """(pod, how it was found) — shared by resolve_pod() and explain_selection()."""
    hint = _hint(pod_unlocode)
    if hint:
        return hint, "from hint"
    evs = _usable(events)
    loading = _loading_locations(evs)
    arrivals = [e for e in evs if is_vessel_arrival(e) and _up(e.unlocode) not in loading]
    if arrivals:
        reps: dict[str, DcsaEvent] = {}
        for e in arrivals:
            loc = _up(e.unlocode) or ""
            if loc not in reps or rank_key(e) > rank_key(reps[loc]):
                reps[loc] = e
        pod = max(reps, key=lambda loc: _latest_key(reps[loc]))
        excluded = ", ".join(sorted(loading)) or "none"
        return pod, f"last vessel arrival (loading ports excluded: {excluded})"
    finals = [e for e in evs if is_final_disc(e, evs)]
    if finals:
        last = max(finals, key=lambda e: (_local(e.event_datetime), e.index))
        return _up(last.unlocode), "final discharge"
    return None, "not found"


def resolve_pod(events: Iterable[DcsaEvent], pod_unlocode: str | None = None) -> str | None:
    """The port of discharge (09 §A.3.7).

    1. A non-blank hint always wins (stripped, upper-cased), even if it is a loading port.
    2. Else the LAST vessel arrival, excluding locations where the box was loaded onto a vessel
       (port of loading, transshipment ports); rail/truck arrivals are not vessel arrivals, so an IPI
       booking resolves to the seaport.
    3. Else the location of the last final discharge (`is_final_disc`).
    4. Else None: never guess the port of loading.
    """
    return _resolve_pod_detail(events, pod_unlocode)[0]


# ── 09 §A.3.8 select_pod_arrival (vessel level) ──
def _arrivals_at(evs: Iterable[DcsaEvent], pod: str) -> list[DcsaEvent]:
    return [e for e in evs if is_vessel_arrival(e) and _up(e.unlocode) == pod]


def _vessel_groups(arrs: Sequence[DcsaEvent]) -> list[list[DcsaEvent]]:
    """One group when at most one distinct vessel identity exists; else one group per identity, and
    arrivals without an identity are dropped (DEBUG)."""
    identities = {vid for a in arrs if (vid := vessel_identity(a)) is not None}
    if len(identities) <= 1:
        return [list(arrs)] if arrs else []
    groups: dict[str, list[DcsaEvent]] = {}
    dropped = 0
    for a in arrs:
        vid = vessel_identity(a)
        if vid is None:
            dropped += 1
            continue
        groups.setdefault(vid, []).append(a)
    if dropped:
        log.debug("[DCSA] %d vessel arrival(s) without a vessel identity ignored (vessels: %s)",
                  dropped, sorted(identities))
    return list(groups.values())


def _latest_vessel_best(arrs: Sequence[DcsaEvent]) -> DcsaEvent | None:
    """Best record per vessel (max rank_key), then the latest vessel (09 §A.3.8 step 2)."""
    bests = [max(group, key=rank_key) for group in _vessel_groups(arrs) if group]
    return max(bests, key=_latest_key) if bests else None


def _transport_arrival(b: DcsaEvent, pod: str, equipment: str | None) -> DcsaArrival:
    cls = _up(b.classifier) or ""
    return DcsaArrival(
        eta=_local(b.event_datetime),
        classifier=cls,
        unlocode=pod,
        facility_code=b.facility_code,
        equipment_reference=equipment,
        basis=f"TRANSPORT ARRI {cls}",
    )


def select_pod_arrival(events: Iterable[DcsaEvent], *, pod_unlocode: str | None = None) -> DcsaArrival | None:
    """Vessel-level arrival at the POD (09 §A.3.8): the latest vessel's best ARRI record, else the best
    laden, non-transshipment DISC at the POD, else None."""
    evs = _usable(events)
    pod = resolve_pod(evs, pod_unlocode)
    if pod is None:
        return None
    best = _latest_vessel_best(_arrivals_at(evs, pod))
    if best is not None:
        return _transport_arrival(best, pod, None)
    discs = [e for e in evs if _is_pod_disc(e, pod)]
    if discs:
        d = max(discs, key=rank_key)
        cls = _up(d.classifier) or ""
        return DcsaArrival(_local(d.event_datetime), cls, pod, d.facility_code, _up(d.equipment_reference),
                           f"EQUIPMENT DISC {cls}")
    return None


# ── 09 §A.3.9 select_pod_arrival_per_equipment ──
def _equipment_order(evs: Iterable[DcsaEvent]) -> list[str]:
    """Container numbers: EQUIPMENT events' equipmentReference, then TRANSPORT events' eq_refs,
    de-duplicated in first-seen order."""
    evs = list(evs)
    seen: dict[str, None] = {}
    for e in evs:
        if _is(e, "EQUIPMENT") and (ref := _up(e.equipment_reference)):
            seen.setdefault(ref, None)
    for e in evs:
        if _is(e, "TRANSPORT"):
            for raw_ref in e.eq_refs:
                if ref := _up(raw_ref):
                    seen.setdefault(ref, None)
    return list(seen)


def _carries(a: DcsaEvent, box: str) -> bool:
    return any(_up(r) == box for r in a.eq_refs)


def _box_vessel(ee: Sequence[DcsaEvent]) -> str | None:
    """Identity of the box's latest laden, vessel-ish LOAD that has one: the vessel into the POD."""
    loads = [e for e in ee if _is_laden_vessel_load(e) and vessel_identity(e) is not None]
    if not loads:
        return None
    return vessel_identity(max(loads, key=lambda e: (_local(e.event_datetime), e.index)))


def _box_vessel_arrival(cands: Sequence[DcsaEvent], vid: str | None) -> DcsaEvent | None:
    """09 §A.3.9 c: one vessel group → its best; several → the box's vessel, else the latest best."""
    groups = _vessel_groups(cands)
    if not groups:
        return None
    if len(groups) == 1:
        return max(groups[0], key=rank_key)
    for group in groups:
        if vid is not None and vessel_identity(group[0]) == vid:
            return max(group, key=rank_key)
    return max((max(group, key=rank_key) for group in groups), key=_latest_key)


def select_pod_arrival_per_equipment(
    events: Iterable[DcsaEvent], *, pod_unlocode: str | None = None
) -> dict[str | None, DcsaArrival]:
    """One arrival at the POD per container, plus the vessel-level arrival under the key None, inserted
    LAST (08 E4: CMA reads per_eq.get(None)). Containers come in first-seen order (09 §A.3.9).

    Per container, in this order: (a) actual laden DISC at the POD; (b) post-arrival proof (laden ACT
    GTOT/AVPU/PICK at the POD, earliest); (c) the vessel arrival for this box (references[EQ] first, else
    the general arrivals; the box's own vessel when several vessels call); (d) estimated DISC;
    (e) the vessel-level arrival. A container with none of these gets no entry.
    """
    evs = _usable(events)
    pod = resolve_pod(evs, pod_unlocode)
    if pod is None:
        return {}
    equipment = _equipment_order(evs)
    arrs = _arrivals_at(evs, pod)
    general = [a for a in arrs if not a.eq_refs]
    vessel_best = _latest_vessel_best(arrs)
    vessel = _transport_arrival(vessel_best, pod, None) if vessel_best is not None else None
    vessel_facility = vessel.facility_code if vessel is not None else None

    out: dict[str | None, DcsaArrival] = {}
    for box in equipment:
        ee = [e for e in evs if _is(e, "EQUIPMENT") and _up(e.equipment_reference) == box]
        pod_discs = [e for e in ee if _is_pod_disc(e, pod)]

        # a. actual discharge
        actual = [e for e in pod_discs if _up(e.classifier) == "ACT"]
        if actual:
            d = max(actual, key=rank_key)
            out[box] = DcsaArrival(_local(d.event_datetime), "ACT", pod, d.facility_code or vessel_facility,
                                   box, "EQUIPMENT DISC ACT")
            continue

        # b. post-arrival proof: the box is out of the terminal, so it has arrived
        proofs = [
            e for e in ee
            if _up(e.classifier) == "ACT" and _up(e.unlocode) == pod and is_laden(e)
            and _up(e.type_code) in POST_ARRIVAL_CODES
        ]
        if proofs:
            p = min(proofs, key=lambda e: (_local(e.event_datetime), e.index))
            out[box] = DcsaArrival(_local(p.event_datetime), "ACT", pod, p.facility_code or vessel_facility,
                                   box, f"EQUIPMENT {_up(p.type_code)} ACT")
            continue

        # c. vessel arrival for this box (transport events are shared by every box on the call)
        cands = [a for a in arrs if _carries(a, box)] or general
        if cands:
            best = _box_vessel_arrival(cands, _box_vessel(ee))
            if best is not None:
                out[box] = _transport_arrival(best, pod, box)
                continue

        # d. estimated discharge
        estimated = [e for e in pod_discs if _up(e.classifier) in ("EST", "PLN")]
        if estimated:
            d = max(estimated, key=rank_key)
            cls = _up(d.classifier) or ""
            out[box] = DcsaArrival(_local(d.event_datetime), cls, pod, d.facility_code or vessel_facility,
                                   box, f"EQUIPMENT DISC {cls}")
            continue

        # e. fallback: the vessel-level arrival
        if vessel is not None:
            out[box] = dataclasses.replace(vessel, equipment_reference=box)
        else:
            log.debug("[DCSA] container %s: no arrival at %s", box, pod)

    if vessel is not None:
        out[None] = vessel
    return out


# ── 09 §A.3.10 booking level and result helpers ──
def _check_policy(policy: str) -> None:
    if policy not in _POLICIES:
        raise ValueError(f"unknown multi-container policy {policy!r} (expected 'first' or 'latest')")


def _pick_booking(per: Mapping[str | None, DcsaArrival], policy: str) -> DcsaArrival | None:
    containers = [(k, v) for k, v in per.items() if k is not None]
    if not containers:
        return per.get(None)
    if policy == "first":
        return containers[0][1]  # first-seen order: Maersk/MSC parity (07 §7.1)
    # "latest": a booking is only fully in once its last container arrives (CMA parity, 08 §7.3)
    return max((a for _, a in containers), key=lambda a: (_local(a.eta), CLASSIFIER_RANK.get(a.classifier, 0)))


def booking_arrival(
    events: Iterable[DcsaEvent], *, policy: Policy, pod_unlocode: str | None = None
) -> DcsaArrival | None:
    """Booking-level arrival (09 §A.3.10). "first" → the first container's arrival; "latest" → the latest
    by (eta, classifier rank); no containers → the vessel-level entry (key None). Invalid policy →
    ValueError. 08 E7: with policy="latest" this equals CMA's own aggregation."""
    _check_policy(policy)
    return _pick_booking(select_pod_arrival_per_equipment(events, pod_unlocode=pod_unlocode), policy)


def to_eta_result(
    arrival: DcsaArrival,
    *,
    carrier: Carrier,
    booking: str,
    containers: tuple[str, ...] = (),
    source_prefix: str = "DCSA",
) -> EtaResult:
    """EtaResult for an arrival: source "{prefix} {basis} at {unlocode}[/{facility}]", is_actual for ACT."""
    source = f"{source_prefix} {arrival.basis} at {arrival.unlocode or '?'}"
    if arrival.facility_code:
        source += f"/{arrival.facility_code}"
    return EtaResult(
        carrier=carrier,
        booking=booking,
        eta=_local(arrival.eta),
        source=source,
        is_actual=(arrival.classifier == "ACT"),
        containers=tuple(containers),
        details={
            "basis": arrival.basis,
            "classifier": arrival.classifier,
            "unlocode": arrival.unlocode,
            "facility_code": arrival.facility_code,
            "equipment_reference": arrival.equipment_reference,
        },
    )


def _event_counts(events: Iterable[DcsaEvent]) -> dict[str, int]:
    counts = Counter(f"{e.event_type}/{e.type_code}/{e.classifier}" for e in events)
    return dict(sorted(counts.items()))


def eta_result_from_payload(
    payload: Any,
    *,
    carrier: Carrier,
    booking: str,
    policy: Policy,
    pod_unlocode: str | None = None,
    label: str | None = None,
    source_prefix: str = "DCSA",
) -> EtaResult:
    """Convenience for simple DCSA carriers (07/08 have their own richer versions), 09 §A.3.10.

    Unexpected shape / no TRANSPORT-EQUIPMENT events → CarrierLookupError; no POD arrival → NoEtaError.
    Logs a WARNING when containers disagree on the arrival by more than one day.
    """
    label = label or str(carrier)
    try:
        events = parse_events(payload)
    except ValueError as exc:
        raise CarrierLookupError(f"{label} returned events in an unexpected shape: {exc}") from exc
    if not any(_up(e.event_type) in _SELECTABLE_TYPES for e in events):
        raise CarrierLookupError(f"{label} returned no tracking events for booking {booking}.")
    _check_policy(policy)  # booking_arrival()'s ValueError for an invalid policy (step 3)
    per = select_pod_arrival_per_equipment(events, pod_unlocode=pod_unlocode)
    arrival = _pick_booking(per, policy)
    if arrival is None:
        raise NoEtaError(
            f"No arrival at the port of discharge published by {label} for booking {booking} "
            f"(events seen: {_event_counts(events)}). Can't determine ETA."
        )
    containers = tuple(k for k in per if k is not None)
    if len(containers) > 1:
        etas = [_local(per[c].eta) for c in containers]
        if max(etas) - min(etas) > timedelta(days=1):
            summary = ", ".join(f"{c}={per[c].eta.isoformat()} {per[c].classifier}" for c in containers)
            log.warning("[DCSA] containers on booking %s disagree on arrival: %s", booking, summary)
    result = to_eta_result(arrival, carrier=carrier, booking=booking, containers=containers,
                           source_prefix=source_prefix)
    details = {
        **dict(result.details),
        "policy": policy,
        "pod": arrival.unlocode,
        "event_count": len(events),
        "per_container": {
            c: {"eta": a.eta.isoformat(), "classifier": a.classifier, "basis": a.basis}
            for c, a in per.items() if c
        },
    }
    return dataclasses.replace(result, details=details)


# ── 09 §A.3.11 explain_selection ──
def _raw_event_datetime(e: DcsaEvent) -> str:
    """The event's raw eventDateTime string (v2.2 item or v3 payload), for the limitation notes."""
    if not isinstance(e.raw, Mapping):
        return ""
    top = _ci(e.raw)
    payload = top.get("payload")
    source = _ci(payload) if isinstance(payload, Mapping) else top
    value = source.get("eventdatetime")
    return value.strip() if isinstance(value, str) else ""


def explain_selection(
    events: Sequence[DcsaEvent], *, pod_unlocode: str | None = None, policy: str = "first"
) -> list[str]:
    """Human-readable audit of the POD/ETA choice for probe() output under "explain" (09 §A.3.11):
    event counts, the POD and how it was found, every vessel arrival at the POD, one line per
    container, the booking result under `policy`, and notes on the known limitations (09 §A.4)."""
    events = list(events)
    counts = _event_counts(events)
    lines = [
        f"events: {len(events)} parsed"
        + (": " + ", ".join(f"{k}={v}" for k, v in counts.items()) if counts else "")
    ]
    evs = _usable(events)
    pod, how = _resolve_pod_detail(evs, pod_unlocode)
    lines.append(f"POD: {pod or '-'} ({how})")
    notes: list[str] = []

    if pod is not None:
        arrs = _arrivals_at(evs, pod)
        if not arrs:
            lines.append(f"vessel arrivals at {pod}: none")
        for a in sorted(arrs, key=lambda a: (_local(a.event_datetime), a.index)):
            created = a.created_datetime.isoformat() if a.created_datetime else "-"
            lines.append(
                f"vessel arrival at {pod}: {a.classifier} {_local(a.event_datetime).isoformat()} "
                f"(created {created} UTC, vessel {vessel_identity(a) or '-'}, event #{a.index})"
            )
        if how.startswith("last vessel arrival"):
            others = sorted({_up(e.unlocode) or "" for e in evs if is_vessel_arrival(e)} - _loading_locations(evs) - {pod})
            if others:
                notes.append(
                    f"note: vessel arrivals also at {', '.join(others)}; the last one was taken as the POD. "
                    "If the carrier publishes calls after the POD, pass pod_unlocode (09 §A.4 limitation 3)."
                )
    else:
        notes.append(
            "note: no POD found (no vessel arrival outside loading ports, no final discharge). A carrier "
            "that knows the POD should pass pod_unlocode (09 §A.4 limitation 2)."
        )

    per = select_pod_arrival_per_equipment(evs, pod_unlocode=pod_unlocode)
    for box in _equipment_order(evs):
        a = per.get(box)
        if a is None:
            lines.append(f"container {box}: no arrival at {pod or 'the POD'}")
        else:
            lines.append(f"container {box}: {a.eta.isoformat()} {a.classifier} via {a.basis}"
                         + (f" at {a.unlocode}/{a.facility_code}" if a.facility_code else f" at {a.unlocode}"))
    if None in per:
        v = per[None]
        lines.append(f"vessel level: {v.eta.isoformat()} {v.classifier} via {v.basis}")

    if policy in _POLICIES:
        chosen = _pick_booking(per, policy)
        if chosen is None:
            lines.append(f"booking ({policy}): no arrival")
        else:
            lines.append(
                f"booking ({policy}): {chosen.eta.isoformat()} {chosen.classifier} via {chosen.basis} "
                f"at {chosen.unlocode or '?'}" + (f" (container {chosen.equipment_reference})"
                                                   if chosen.equipment_reference else "")
            )
    else:
        lines.append(f"booking ({policy}): invalid policy (expected 'first' or 'latest')")

    if any(_UTC_SUFFIX_RE.search(_raw_event_datetime(e)) for e in evs):
        notes.append(
            "note: some eventDateTime values carry a UTC offset (Z/+00:00). DCSA eventDateTime is port-local "
            "time; if this carrier sends UTC instead, times are off by whole hours (09 §A.4 limitation 1)."
        )
    return lines + notes


# ── 09 §A.3.12 optional shared pagination helper ──
def _page_items(body: Any, label: str) -> list[Any]:
    if isinstance(body, list):
        return body
    if isinstance(body, Mapping):
        top = _ci(body)
        for key in WRAPPER_KEYS:
            value = top.get(key)
            if isinstance(value, list):
                return value
        keys: Any = sorted(str(k) for k in body)[:10]
    else:
        keys = type(body).__name__
    raise CarrierLookupError(f"{label} returned an unexpected payload shape (top-level keys: {keys})")


def _same_origin(a: httpx.URL, b: httpx.URL) -> bool:
    return (a.scheme, a.host, a.port) == (b.scheme, b.host, b.port)


async def fetch_event_pages(
    client: httpx.AsyncClient,
    url: str,
    *,
    service: str,
    label: str,
    params: Mapping[str, str],
    headers: Mapping[str, str] | None = None,
    auth: TokenProvider | None = None,
    sem: asyncio.Semaphore | None = None,
    limiter: AsyncLimiter | None = None,
    max_pages: int = 10,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """GET every page of a DCSA events endpoint (09 §A.3.12; same loop as Maersk's _fetch_events, 07 §6.3).

    - `request_with_auth` when `auth` is given, else `request`; `sem` is held only around each HTTP call.
    - A page is a list, or the list under WRAPPER_KEYS; anything else → CarrierLookupError.
    - `Next-Page`: an absolute URL on the same origin is followed with its own query; one on another host
      is NEVER followed (WARNING, stop); a path ("/…", or "?…") is resolved against the current URL; a bare
      token is sent as `cursor`. Same origin = same scheme, host and port, so credentials are never sent
      to another host nor downgraded to plain http.
    - Stops when Next-Page is missing, equals Current-Page, or the page is empty; WARNING after max_pages.
    ServiceError / AuthError from the HTTP layer propagate (carriers map them to their own messages).
    Returns (events, page_summaries); a summary is {page, status, events, elapsed_ms, current_page,
    next_page, url}.
    """
    origin = httpx.URL(url)
    current_url: httpx.URL | str = url
    current_params: dict[str, str] | None = dict(params)
    events: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []
    pages = max(1, int(max_pages))

    async def _call() -> httpx.Response:
        common: dict[str, Any] = {"params": current_params, "headers": dict(headers or {}),
                                  "expected": (200,), "limiter": limiter}
        if auth is not None:
            return await request_with_auth(client, "GET", current_url, service=service, auth=auth, **common)
        return await request(client, "GET", current_url, service=service, **common)

    for page_no in range(1, pages + 1):
        started = time.monotonic()
        if sem is not None:
            async with sem:
                resp = await _call()
        else:
            resp = await _call()
        body = json_body(resp, service=service)
        page = _page_items(body, label)
        events.extend(item for item in page if isinstance(item, dict))
        nxt = (resp.headers.get("Next-Page") or "").strip()
        cur = (resp.headers.get("Current-Page") or "").strip()
        try:
            requested = str(resp.request.url)
        except RuntimeError:
            requested = str(current_url)
        summaries.append({
            "page": page_no,
            "status": resp.status_code,
            "events": len(page),
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "current_page": cur or None,
            "next_page": redact(nxt) if nxt else None,
            "url": redact(requested),
        })
        if not nxt or nxt == cur or not page:
            break
        if nxt.lower().startswith(("http://", "https://")):
            target = httpx.URL(nxt)
            if not _same_origin(target, origin):
                log.warning("[%s] Next-Page link points to another host or scheme (%s://%s) — not followed",
                            label, target.scheme, target.host)
                break
            current_url, current_params = target, None  # the link already carries its query
        elif nxt.startswith(("/", "?")):
            current_url, current_params = httpx.URL(str(current_url)).join(nxt), None
        else:
            current_params = {**(current_params or {}), "cursor": nxt}
    else:
        log.warning("[%s] stopped after %d pages of events (max_pages) — using the events collected so far",
                    label, pages)
    return events, summaries
