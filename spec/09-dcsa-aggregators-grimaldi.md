# 09 — DCSA Track & Trace parser, third-party aggregator, Grimaldi, provider selection

This file specifies four pieces:

| Part | Module | What it is |
|---|---|---|
| **A** | `src/eta_automations/carriers/dcsa.py` | Pure, version-tolerant parser for DCSA Track & Trace events (v2.2, plus the v3.0 envelope), and the rules that turn events into **one arrival at the port of discharge (POD)**, per container and per booking. Used by Maersk (07 §7), CMA CGM (08 §7) and the aggregator (Part B). |
| **B** | `src/eta_automations/carriers/aggregator.py` | **Optional** third-party tracking provider (default: **ShipsGo API v2**). It can serve any carrier while official API access is pending, and is the only realistic route for Grimaldi. |
| **C** | `src/eta_automations/carriers/grimaldi.py` | Stub that keeps the original "not yet implemented" behaviour (parity). |
| **D** | provider selection in `src/eta_automations/carriers/router.py` | `ETA_PROVIDER_<CARRIER>` = `native` \| `aggregator` \| `off`, the `prepare()` warm-up hook, and how this interacts with the circuit breaker (10 §6). |

All names follow 03-foundations.md. **Cross-spec acceptance:** the Maersk tests (07 §9), the CMA tests
(08 §9) and MSC's API "rescue" fixture (06 §9.2 `api_in_transit.json`) exercise `dcsa.py` too. Every
expected value in 07 §9.2, 08 §9.3 and that MSC fixture was re-computed against the algorithm in Part A
while this spec was written, and they all hold. If an implementation detail here ever seems to conflict with
those tables, the tables win: fix `dcsa.py`.

New runtime dependency (add to 03 §2): **`tzdata>=2024.1`**. `zoneinfo` needs it on Windows. It is used
by `dcsa.to_local_naive()`, which the aggregator uses to turn provider UTC timestamps into port-local time.

---

# Part A — `carriers/dcsa.py`

## A.1 Summary

DCSA (Digital Container Shipping Association) Track & Trace is the event standard behind the official
tracking APIs of Maersk and CMA CGM, and of the other DCSA member carriers. The original program scraped
pages: Maersk's "Arrives …" accordion and CMA's page globals. Those numbers come from the same carrier
systems that publish DCSA events. This module turns a list of events into an ETA in microseconds, with no
browser and no network. The only exception is the optional pagination helper in §A.3.12.

It is **pure**: no I/O, no clock, no settings. Logger: `eta.carrier.dcsa` (DEBUG only, except one WARNING
in §A.3.10).

## A.2 Facts about the payloads (what the parser must accept)

### A.2.1 DCSA Track & Trace 2.2 — what carriers serve today (VERIFIED-DOCS)

Source: the official OpenAPI on SwaggerHub, `dcsaorg/DCSA_TNT/2.2.0` (published 2021-10-28). It
references the domains `EVENT_DOMAIN/1.0.4`, `DCSA_DOMAIN/1.0.3`, `LOCATION_DOMAIN/1.0.3` and
`DOCUMENTATION_DOMAIN/1.0.4`.

- **Endpoint:** `GET /v2/events`. Carriers mount it under their own paths:
  - Maersk: `/track-and-trace-private/events` (07 §4.2);
  - CMA: `/events` and `/events/{ref}` (08 §4).
- **Response:** a **bare JSON array** of `transportEvent` | `shipmentEvent` | `equipmentEvent`.
  Maersk wraps it as `{"events": [...]}`.
- **Query parameters:** `eventType` (array: SHIPMENT, TRANSPORT, EQUIPMENT), `carrierBookingReference`,
  `transportDocumentReference`, `equipmentReference`, `transportEventTypeCode`, `equipmentEventTypeCode`,
  `shipmentEventTypeCode`, `UNLocationCode`, `vesselIMONumber`, `carrierVoyageNumber`, `eventCreatedDateTime`, …
  Also `limit` (int, default 100), `cursor` and `sort`, plus an optional `API-Version` request header (major
  version only). Combining contradictory filters returns `[]` (spec text), so carriers' clients filter
  client-side.
- **Pagination:** response headers `Current-Page`, `Next-Page`, `Prev-Page`, `First-Page`, `Last-Page`,
  each "a link to the … page". `Next-Page` MAY be omitted on the last page.
- **Common event fields:**
  - `eventID`;
  - `eventCreatedDateTime` (required; metadata timestamp of the record);
  - `eventType` (discriminator);
  - `eventClassifierCode`;
  - `eventDateTime`: "the **local** date and time, where the event took place or when the event will take
    place, in ISO 8601 format", e.g. `2019-11-12T07:41:00+08:30`.
- **TRANSPORT event fields:**
  - `eventClassifierCode` ∈ {ACT, PLN, EST};
  - `transportEventTypeCode` ∈ {ARRI, DEPA} (deprecated alias `eventTypeCode`);
  - `delayReasonCode`, `changeRemark`, `vesselScheduleChangeRemark`, `documentReferences[]`;
  - `transportCall` (required).
- **EQUIPMENT event fields:**
  - `eventClassifierCode` ∈ {PLN, ACT, EST};
  - `equipmentEventTypeCode` ∈ {LOAD, DISC, GTIN, GTOT, STUF, STRP, PICK, DROP, INSP, RSEA, RMVD};
  - `equipmentReference` (≤15 chars), `ISOEquipmentCode`;
  - `emptyIndicatorCode` ∈ {EMPTY, LADEN} (required);
  - `eventLocation{…}`, `transportCall{…}`, `documentReferences[]`, `references[]`, `seals[]`.
- **SHIPMENT event fields:**
  - `shipmentEventTypeCode` ∈ {RECE, DRFT, PENA, PENU, REJE, APPR, ISSU, SURR, SUBM, VOID, CONF, REQS, CMPL, HOLD, RELS};
  - `documentID`, `documentTypeCode`;
  - `eventDateTime` equals `eventCreatedDateTime`.
  - These are document events and are never used for ETAs.
- **`transportCall` fields:**
  - required: `transportCallID`, `modeOfTransport` ∈ {VESSEL, RAIL, TRUCK, BARGE};
  - `carrierServiceCode`, `carrierVoyageNumber`, `exportVoyageNumber`, `importVoyageNumber`;
  - `transportCallSequenceNumber` (integer);
  - `UNLocationCode` (≤5), `facilityCode` (≤6), `facilityCodeListProvider` ∈ {BIC, SMDG};
  - `facilityTypeCode` ∈ {BOCR, CLOC, COFS, COYA, OFFD, DEPO, INTE, POTE, RAMP} (POTE = port terminal);
  - `otherFacility`;
  - `location{locationName, latitude, longitude, UNLocationCode, facilityCode, facilityCodeListProvider, address}`;
  - `vessel{vesselIMONumber, vesselName, vesselFlag, vesselCallSignNumber, vesselOperatorCarrierCode, …}`.
- **`documentReferences[]`:** `{documentReferenceType: "BKG" | "TRD", documentReferenceValue}`.
- **`references[]`:** `{referenceType, referenceValue}`.

**Carrier dialects of 2.2 that the parser must accept** (from 06/07/08):
- **Maersk** wraps the array as `{"events": [...]}`. It also puts the UN/LOCODE only under
  `transportCall.location` on some PLN/EST records.
- **CMA** returns a bare array, adds `carrierSpecificData{…}`, and can carry sentinel dates
  (`0001-01-01…`, `9999-12-31T23:59:59.9999999`).
- **MSC** (official API) uses camelCase keys:
  - `transportCall.unLocationCode`, `eventLocation.unLocationCode`, `eventId`;
  - classifiers ACT/EST only;
  - `Z` or `+00:00` timestamps with 7 fractional digits;
  - a `description` field holding myMSC-style event names. It stays reachable through `DcsaEvent.raw`.

  Hence **every key lookup is case-insensitive** (§A.3.4).

### A.2.2 DCSA Track & Trace 3.0 — tolerate it, do not depend on it (INFERRED)

- DCSA published Track & Trace **3.0.0 on 2026-09-30**. It adds IoT and reefer events and improves how
  corrections and withdrawals are sent. It is available **first to DCSA members and DCSA+ partners**,
  with a public release planned for H2 2027 (trade press, VERIFIED).
- The field-level 3.0 schema is not public. The shape below is **INFERRED** from DCSA's published
  `EVENT_DOMAIN` 3.x components on SwaggerHub (3.0.0 Dec 2022 → 3.2.0 Jun 2025, whose `iotEvent` /
  `reeferEvent` match the 3.0 scope).
- No carrier used by this program is known to serve 3.0 yet. The parser accepts it anyway, so a future
  switch only changes a URL.

| Concept | v2.2 | v3 (inferred) |
|---|---|---|
| Envelope | flat event object | `{"metadata": {...}, "payload": {...}}` |
| Event type / id / created | `eventType`, `eventID`, `eventCreatedDateTime` | `metadata.eventType`, `metadata.eventID`, `metadata.eventCreatedDateTime` |
| Withdrawal | — | `metadata.retractedEventID` = id of an earlier event to discard; such an envelope has **no** `payload` |
| Transport call id | `transportCall.transportCallID` | `transportCall.transportCallReference` |
| Location | `transportCall.UNLocationCode` (+ `transportCall.location{...}`) | `transportCall.location{locationType: UNLO\|FACI\|ADDR\|GEOL, UNLocationCode, facilityCode, facilityCodeListProvider, locationName}` |
| Voyage | `carrierVoyageNumber`, `importVoyageNumber`, `exportVoyageNumber` | `carrierImportVoyageNumber`, `carrierExportVoyageNumber` |
| Vessel name | `vessel.vesselName` | `vessel.name` |
| Document refs | `documentReferences[{documentReferenceType, documentReferenceValue}]` | `relatedDocumentReferences[{type, value}]` |
| Equipment refs on non-equipment events | rare | `references[{type: "EQ", value: "<container>"}]` |
| Transshipment flag | — | `payload.isTransshipmentMove` (LOAD/DISC/GTIN/GTOT/PICK/DROP) |
| Extra equipment codes | — | `AVPU`, `AVDO`, `CUSS`, `CUSI`, `CUSR`, `CROS` |
| Extra facility types | — | `WAYP`, `BORD` |

## A.3 Module design

### A.3.1 Constants and types

```python
CLASSIFIER_RANK: Mapping[str, int] = {"ACT": 3, "EST": 2, "PLN": 1}   # anything else (REQ, unknown) ranks 0 = unusable
PLAUSIBLE_YEARS = range(2000, 2101)                                   # event dates outside this are skipped (sentinels 0001/9999)
WRAPPER_KEYS = ("events", "data", "items", "content", "results")      # accepted list keys of wrapped payloads
POST_ARRIVAL_CODES = ("GTOT", "AVPU", "PICK")                         # laden ACT events at the POD that prove arrival
Policy = Literal["first", "latest"]
```

### A.3.2 Dataclasses (required interface; the added fields are defaulted, so positional construction still works)

```python
@dataclass(frozen=True, slots=True)
class DcsaEvent:
    event_type: str                     # "TRANSPORT" | "EQUIPMENT" | "SHIPMENT"
    classifier: str                     # "ACT" | "EST" | "PLN" | "REQ"
    type_code: str                      # "ARRI","DEPA","DISC","LOAD","GTOT", ...
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
```

The `basis` vocabulary produced by this module is exactly:
- `"TRANSPORT ARRI {ACT|EST|PLN}"`
- `"EQUIPMENT DISC {ACT|EST|PLN}"`
- `"EQUIPMENT {GTOT|AVPU|PICK} ACT"`

The aggregator adds `"ROUTE POD date_of_discharge"` (§B.7.4). 08 §7.3 tests `"DISC" in basis`; 07 builds
its own basis strings for the final-destination variant.

### A.3.3 Datetime helpers

```python
def parse_dcsa_datetime(value: Any) -> datetime | None:
    """ISO-8601 → datetime (aware if an offset/Z is present, else naive). Never raises.
    1. Non-str or blank → None.
    2. s = value.strip(); if len(s) > 10 and s[10] == " ": s = s[:10] + "T" + s[11:]   # "2026-11-12 08:00:00"
    3. Truncate fractional seconds to 6 digits: re.sub(r"(\\.\\d{6})\\d+", r"\\1", s)        # .NET "…59.9999999"
    4. try datetime.fromisoformat(s) (Python ≥ 3.11 accepts 'Z', '+0000', '+05:30', basic format, date-only)
       except (ValueError, OverflowError): return None"""

def naive_local(dt: datetime | None) -> datetime | None:
    """Drop tzinfo WITHOUT converting: keeps the local wall-clock time (03 §4)."""

def naive_utc(dt: datetime | None) -> datetime | None:
    """Aware → convert to UTC, then drop tzinfo; naive → unchanged.
    (OverflowError, ValueError) → None. Year-1 times with a positive offset overflow."""

def to_local_naive(value: str | datetime | None, tz_hint: str | None = None) -> datetime | None:
    """Port-local naive wall time. Used by the aggregator, whose providers send UTC timestamps plus the port's tz.
    dt = parse_dcsa_datetime(value) if str else value; None → None.
    tz = _tz_from_hint(tz_hint):
       blank → None; 'Z'/'UTC'/'GMT' → timezone.utc;
       '^(?:UTC|GMT)?\\s*([+-])(\\d{1,2}):?(\\d{2})?$' → fixed offset ('+02:00', 'UTC-5', 'GMT+0530');
       otherwise ZoneInfo(hint) (IANA name), invalid → None.
    if dt is aware and tz is not None: dt = dt.astimezone(tz)    # idempotent when dt already carries the local offset
    return dt.replace(tzinfo=None)"""
```

Examples (all asserted in tests):

| Input | `tz_hint` | Result |
|---|---|---|
| `2026-10-20T08:00:00Z` | `Europe/Rome` | `2026-10-20 10:00` |
| `2026-10-20T08:00:00Z` | `+02:00` | `2026-10-20 10:00` |
| `2026-10-20T08:00:00Z` | `UTC-5` | `2026-10-20 03:00` |
| `2026-10-20T08:00:00Z` | `GMT+0530` | `2026-10-20 13:30` |
| `2026-10-20T08:00:00Z` | `Mars/Olympus` (invalid) | `2026-10-20 08:00` (given offset's wall time) |
| `2026-10-20T08:00:00` (naive) | `America/New_York` | `2026-10-20 08:00` (unchanged) |
| `2026-11-03T02:40:00Z` | `America/New_York` | `2026-11-02 21:40` (**date changes**: that is why conversion matters) |

### A.3.4 `parse_events(payload: Any) -> list[DcsaEvent]`

1. **Find the list.**
   - A `list` is used as-is.
   - For a `Mapping`, use the first value under `WRAPPER_KEYS` that is a list. Failing that, a Mapping
     with `eventType` or `metadata` is treated as a single event (`[payload]`).
   - Anything else raises `ValueError(f"unrecognised DCSA payload shape: {type} keys={sorted(keys)[:10]}")`.
   - Carrier clients map that `ValueError` to `CarrierLookupError`.
2. **For each item** (keep its list position as `index`):
   - Skip non-Mapping items.
   - **Case-insensitive keys.** Read every Mapping through `_ci(m) = {str(k).casefold(): v}`, keeping the
     first occurrence if two keys differ only in case. Look names up case-folded. So `UNLocationCode`,
     `unLocationCode` and `unlocationcode` are the same key, and so are `eventID` and `eventId`. This
     applies to nested objects too (`transportCall`, `location`, `eventLocation`, `vessel`, `references[]`,
     the v3 `metadata`/`payload`). "Non-empty" below means not `None` and not a blank string.
   - **Flatten v3 envelopes:**
     - If `item["metadata"]` is a Mapping, `flat = dict(item.get("payload") or {})`.
     - Then `flat.setdefault` `eventType`, `eventID` and `eventCreatedDateTime` from metadata.
     - If `metadata.retractedEventID` is set, add it to `retracted`.
     - An envelope without a payload produces no event.
   - Otherwise `flat = item`.
   - `raw` is always the **original item**.
3. **Required fields.** Skip the item (DEBUG log `"[DCSA] skipped event #%d: %s"`) when any of these holds:
   - `event_type = upper(flat.eventType)` is not in {TRANSPORT, EQUIPMENT, SHIPMENT};
   - `type_code` is empty. It is the first non-empty of `transportEventTypeCode`, `equipmentEventTypeCode`,
     `shipmentEventTypeCode`, `eventTypeCode`, upper-cased;
   - `classifier = upper(eventClassifierCode)` is empty;
   - `eventDateTime` does not parse (`parse_dcsa_datetime` → None);
   - **its year is not in `PLAUSIBLE_YEARS`.** This protects against 0001/9999 sentinels, and parsing must
     never crash on them.
4. **Field mapping.** The first non-empty value wins. Strings are stripped. The codes `unlocode`,
   `facility_code`, `facility_type`, `mode_of_transport`, `equipment_reference`, `empty_indicator` and
   `eq_refs` are upper-cased.

   | `DcsaEvent` field | Source, in order (`tc` = `transportCall`, `tcl` = `tc.location`, `el` = `eventLocation`) |
   |---|---|
   | `event_datetime` | `naive_local(parse_dcsa_datetime(eventDateTime))`. Example: `"2026-11-10T14:00:00-08:00"` → `datetime(2026,11,10,14,0)`. |
   | `created_datetime` | `naive_utc(parse_dcsa_datetime(eventCreatedDateTime))`. If the year is outside `PLAUSIBLE_YEARS`, use `None` (the event is kept). |
   | `unlocode` | `tc.UNLocationCode` → `tcl.UNLocationCode` → `el.UNLocationCode`, for any event type. Case-insensitive, so MSC's `unLocationCode` works. |
   | `facility_code` | `tc.facilityCode` → `tcl.facilityCode` → `el.facilityCode` |
   | `facility_type` | `tc.facilityTypeCode` → `flat.facilityTypeCode` (v3 equipment) |
   | `mode_of_transport` | `tc.modeOfTransport` |
   | `equipment_reference` | `equipmentReference` |
   | `vessel_name` / `vessel_imo` | `tc.vessel.vesselName` → `tc.vessel.name` / `tc.vessel.vesselIMONumber` |
   | `voyage` | `tc.importVoyageNumber` → `tc.carrierImportVoyageNumber` → `tc.carrierVoyageNumber` → `tc.exportVoyageNumber` → `tc.carrierExportVoyageNumber` |
   | `transport_call_key` | `tc.transportCallID` → `tc.transportCallReference` |
   | `transport_call_sequence` | `tc.transportCallSequenceNumber`. Accept int, or a digit string converted with `int()`; else None. |
   | `empty_indicator` | `emptyIndicatorCode` |
   | `is_transshipment_move` | `isTransshipmentMove`, only if it is a bool |
   | `eq_refs` | Values of `references[]` whose `type` / `referenceType` is `"EQ"`, using `value` / `referenceValue`. De-duplicated, in order. |
   | `event_id` | `eventID` |

   Unknown keys are ignored.
5. **Withdrawals.** After the loop, drop every event whose `event_id` is in `retracted`.
6. **Return** the events in source order. SHIPMENT events are returned; the selection functions ignore
   them. Never raise for a single bad item.

### A.3.5 Ranking

```python
def rank_key(e: DcsaEvent) -> tuple:
    return (CLASSIFIER_RANK.get((e.classifier or "").upper(), 0),
            naive_utc(e.created_datetime) or datetime.min,      # newest record wins (revisions)
            naive_local(e.event_datetime),                       # then the later time
            e.index)                                             # then the later position in the payload
```

`max(candidates, key=rank_key)` gives ACT > EST > PLN, then the newest `eventCreatedDateTime`, then the later
`eventDateTime`. It never raises on a mix of naive and aware datetimes, because the helpers normalise both.
(08 E5: an EST created 2026-11-02 saying 11-10 14:00 beats an EST created 2026-10-19 saying 11-12.)

### A.3.6 Predicates

```python
def is_usable(e):           e.event_type in ("TRANSPORT","EQUIPMENT") and CLASSIFIER_RANK.get(e.classifier, 0) > 0
def is_laden(e):            e.empty_indicator != "EMPTY"            # None counts as laden
def is_vessel_ish(e):       e.mode_of_transport == "VESSEL" or (e.mode_of_transport is None and e.facility_type in (None, "POTE"))
def is_vessel_arrival(e):   e.event_type == "TRANSPORT" and e.type_code == "ARRI" and bool(e.unlocode) and is_vessel_ish(e)
def vessel_identity(e):     e.vessel_imo or (e.vessel_name.upper() if e.vessel_name else None)
def is_final_disc(e, evs):  # laden vessel discharge, not a transshipment move, not followed by a laden vessel LOAD of the same box
    (e.event_type == "EQUIPMENT" and e.type_code == "DISC" and e.unlocode and is_laden(e) and is_vessel_ish(e)
     and e.is_transshipment_move is not True
     and not any(l.event_type == "EQUIPMENT" and l.type_code == "LOAD" and is_laden(l) and is_vessel_ish(l)
                 and l.equipment_reference == e.equipment_reference and l.event_datetime > e.event_datetime for l in evs))
```

### A.3.7 `resolve_pod(events, pod_unlocode: str | None = None) -> str | None`

1. If `pod_unlocode` is non-blank, return it stripped and upper-cased. **A hint always wins**, even if it is
   a loading port. All later selection is restricted to events whose `unlocode` equals it (07 req. 2, 08 E4).
2. `evs = [e for e in events if is_usable(e)]`.
3. `loading_locs` = {`unlocode` of every laden, vessel-ish EQUIPMENT `LOAD`}. Cargo is *loaded* at the port
   of loading and at transshipment ports, so those places cannot be the POD.
4. `arrivals = [e for e in evs if is_vessel_arrival(e) and e.unlocode not in loading_locs]`. If there are any:
   - pick the representative per location with `max(rank_key)`;
   - the POD is the location whose representative has the greatest
     `(event_datetime, CLASSIFIER_RANK, index)`, i.e. the **last VESSEL arrival** in the DCSA sense.
   - Because rail/truck arrivals are not vessel-ish, a vessel-then-rail (IPI) booking resolves to the
     seaport (e.g. `USLAX`), not the inland ramp (`USCHI`) (07 §9.2).
5. Otherwise, if any event satisfies `is_final_disc`, the POD is the `unlocode` of the one with the
   greatest `(event_datetime, index)`.
6. Otherwise return `None`. Example: a booking with only port-of-loading events has no arrival yet, so
   **never** guess the POL.

### A.3.8 `select_pod_arrival(events, *, pod_unlocode=None) -> DcsaArrival | None` (vessel level)

1. `pod = resolve_pod(...)`. If it is `None`, return `None`.
2. `arrs` = the vessel arrivals at `pod`. If there are any:
   - Group them by `vessel_identity`. If at most one distinct non-null identity exists, use **one** group
     with all of `arrs`. Otherwise use one group per identity, and drop arrivals without an identity
     (DEBUG log).
   - Each group's best is `max(group, key=rank_key)`.
   - Return the best whose `(event_datetime, CLASSIFIER_RANK, index)` is greatest (the latest vessel):
     `DcsaArrival(eta=b.event_datetime, classifier=b.classifier, unlocode=pod, facility_code=b.facility_code,
     equipment_reference=None, basis=f"TRANSPORT ARRI {b.classifier}")`.
3. Otherwise, take the laden, non-transshipment `DISC` events at `pod`. If there are any, return the one with
   `max(rank_key)` as `basis=f"EQUIPMENT DISC {cls}"`, carrying its `equipment_reference`.
4. Otherwise return `None`.

### A.3.9 `select_pod_arrival_per_equipment(events, *, pod_unlocode=None) -> dict[str | None, DcsaArrival]`

1. Set up:
   - `evs = [e for e in events if is_usable(e)]`;
   - `pod = resolve_pod(evs, pod_unlocode)`; if it is None, return `{}`.
2. `equipment`: the `equipment_reference` values of EQUIPMENT events, then the `eq_refs` of TRANSPORT
   events, de-duplicated in **first-seen order**.
3. Candidates and the vessel-level arrival:
   - `arrs` = the vessel arrivals at `pod`;
   - `general = [a for a in arrs if not a.eq_refs]`;
   - `vessel` = the step-2 result of §A.3.8 computed on `arrs` only. It is `None` when `arrs` is empty, and
     never DISC-based.
4. For each container `E`, with `ee` = its events. **Order matters:**
   - **a. Actual discharge.**
     - Look for laden, non-transshipment `DISC` `ACT` at `pod`.
     - If found, take `d = max(rank_key)` and return
       `DcsaArrival(d.event_datetime, "ACT", pod, d.facility_code or (vessel.facility_code if vessel else None), E, "EQUIPMENT DISC ACT")`.
   - **b. Post-arrival proof.**
     - Look for laden `ACT` events at `pod` with `type_code` in `POST_ARRIVAL_CODES`.
     - If found, take the **earliest** by `(event_datetime, index)` and return `basis f"EQUIPMENT {code} ACT"`.
     - Use: the box is out of the terminal, so it has arrived, even if the carrier never published DISC.
   - **c. Vessel arrival for this box.**
     - `cands = [a for a in arrs if E in a.eq_refs] or general`.
     - `vid_E` = the `vessel_identity` of `E`'s latest laden, vessel-ish `LOAD` (by `(event_datetime, index)`)
       that has an identity. This is the vessel that carries the box into the POD.
     - Pick:
       - one vessel group: its `max(rank_key)`;
       - several groups: the group whose identity equals `vid_E`, else the group with the latest best.
     - Return `basis f"TRANSPORT ARRI {cls}"`.
     - Note: transport events carry no `equipmentReference`; they are shared by every box on the call (07
       req. 3).
   - **d. Estimated discharge.** Laden, non-transshipment `DISC` with `EST`/`PLN` at `pod` → `max(rank_key)`.
   - **e. Fallback.** If `vessel` is not None, give `E` the vessel-level arrival
     (`equipment_reference=E`, same basis). Otherwise `E` gets **no entry**.
5. If `vessel` is not None, add it under the key **`None`**, inserted **last** (08 E4: CMA reads
   `per_eq.get(None)`).
6. Return the dict. Container keys come in equipment order, then `None`.

### A.3.10 Booking level and result helpers

```python
def booking_arrival(events, *, policy: Policy, pod_unlocode: str | None = None) -> DcsaArrival | None:
    """per = select_pod_arrival_per_equipment(...); policy not in ("first","latest") → ValueError.
    containers = [(k, v) for k, v in per.items() if k is not None]
    no containers → per.get(None)
    "first"  → the first container's arrival (first-seen order: Maersk/MSC parity, 07 §7.1)
    "latest" → max by (eta, CLASSIFIER_RANK) (CMA parity: 'a booking is only fully in once its last container arrives').
    08 E7: with policy="latest" this equals CMA's own aggregation."""

def to_eta_result(arrival: DcsaArrival, *, carrier: Carrier, booking: str,
                  containers: tuple[str, ...] = (), source_prefix: str = "DCSA") -> EtaResult:
    """EtaResult(carrier=carrier, booking=booking, eta=arrival.eta,
                 source=f"{source_prefix} {arrival.basis} at {arrival.unlocode or '?'}" + (f"/{arrival.facility_code}" if arrival.facility_code else ""),
                 is_actual=(arrival.classifier == "ACT"), containers=containers,
                 details={"basis": ..., "classifier": ..., "unlocode": ..., "facility_code": ..., "equipment_reference": ...})"""

def eta_result_from_payload(payload: Any, *, carrier: Carrier, booking: str, policy: Policy,
                            pod_unlocode: str | None = None, label: str | None = None,
                            source_prefix: str = "DCSA") -> EtaResult:
    """Convenience for simple DCSA carriers (07/08 have their own richer versions).
    label = label or str(carrier)
    1. events = parse_events(payload); ValueError → CarrierLookupError(f"{label} returned events in an unexpected shape: {exc}")
    2. no TRANSPORT/EQUIPMENT event → CarrierLookupError(f"{label} returned no tracking events for booking {booking}.")
    3. per = select_pod_arrival_per_equipment(events, pod_unlocode=pod_unlocode); arrival = booking_arrival(...)
       None → NoEtaError(f"No arrival at the port of discharge published by {label} for booking {booking} "
                         f"(events seen: {dict(sorted(Counter(f'{e.event_type}/{e.type_code}/{e.classifier}' for e in events).items()))}). Can't determine ETA.")
    4. containers = tuple(k for k in per if k is not None)
    5. if len(containers) > 1 and (max - min of their etas) > 1 day: log WARNING
       "[DCSA] containers on booking %s disagree on arrival: %s" (per-container summary)
    6. result = to_eta_result(arrival, ...); details += {"policy": policy, "pod": arrival.unlocode, "event_count": len(events),
       "per_container": {c: {"eta": a.eta.isoformat(), "classifier": a.classifier, "basis": a.basis} for c, a in per.items() if c}}"""
```

### A.3.11 `explain_selection(events, *, pod_unlocode=None, policy="first") -> list[str]`

Used by every carrier's `probe()` (03 §9) under the key `"explain"`, so that a person can audit the choice.
The lines must contain:
1. Counts by `event_type/type_code/classifier`.
2. The POD and how it was found: `"from hint"`, `"last vessel arrival (loading ports excluded: CNSHA)"`,
   `"final discharge"`, or `"not found"`.
3. Every vessel arrival at the POD: classifier, local time, created time, vessel identity.
4. One line per container: chosen time, classifier, basis.
5. The booking result under `policy`, or `"no arrival"`.

The exact wording is free. Tests assert only that the POD code and the chosen ISO time appear.

### A.3.12 Optional shared pagination helper

```python
async def fetch_event_pages(client: httpx.AsyncClient, url: str, *, service: str, label: str,
                            params: Mapping[str, str], headers: Mapping[str, str] | None = None,
                            auth: TokenProvider | None = None, sem: asyncio.Semaphore | None = None,
                            limiter: AsyncLimiter | None = None, max_pages: int = 10
                            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
```

The algorithm is the same as Maersk's `_fetch_events` (07 §6.3), generalised:
- Use `request_with_auth` if `auth` is given, else `request`. Hold `sem` only around each HTTP call.
- The page is a list, or the list under `WRAPPER_KEYS`. Anything else raises
  `CarrierLookupError(f"{label} returned an unexpected payload shape (top-level keys: …)")`.
- `Next-Page` may be one of:
  - an absolute URL on the same host — follow it with its own query;
  - an absolute URL on a foreign host — **never followed**: WARNING, then stop;
  - a path starting with `/` — follow it;
  - a bare token — sent as `cursor`.
- Stop when `Next-Page` is missing, equals `Current-Page`, or the page is empty. After `max_pages`, log a
  WARNING.
- Returns `(events, page_summaries)`, where each summary has
  `{page, status, events, elapsed_ms, current_page, next_page, url}`.

07 and 08 may call it instead of duplicating the loop, keeping their own error messages. This is a
recommendation, not a requirement.

## A.4 Business logic notes, parity and limitations

- **Why the DCSA "port of discharge" is the target:** the original's ETA was the arrival at the port. For
  inland moves, Maersk's spec decides separately (07 §7.1, `MAERSK_ETA_TARGET`); this module always answers
  for the POD.
- **Policy per carrier:**
  - Maersk `first`, configurable (07);
  - CMA `latest` (08 §7.3);
  - aggregator: per carrier, §B.7.5;
  - MSC's primary myMSC GraphQL path (06) does not use this module. Its official-API "rescue" path calls
    `booking_arrival(parse_events(payload), policy="first")` with no hint (06 §7.5).
- **Revisions:** a newer `eventCreatedDateTime` wins inside the same classifier. ACT always beats an
  estimate, however recent. In v3, withdrawals remove the target event entirely.
- **Limitations** (flagged in `explain_selection` and covered by the probe procedure):
  1. A carrier that sends `eventDateTime` in UTC instead of local time (non-compliant) shifts times. There
     is no tz information in DCSA to fix it.
  2. If a rail `LOAD` at the seaport lacks a `transportCall` (no mode), the seaport looks like a loading
     port. Then `resolve_pod` may find no POD and the booking raises `NoEtaError`, which is safe. Carriers
     that know the POD should pass `pod_unlocode`.
  3. If a carrier also publishes the vessel's calls *after* the POD, "last vessel arrival" would be wrong.
     A final `DISC` or a `pod_unlocode` hint prevents this.

## A.5 Fixtures and expected results (`tests/fixtures/dcsa/`)

All of these are synthetic. The JSON below is exactly what the reference implementation was run on.

**`v22_single_revised_est.json`** (v2.2 bare list):
- one container;
- a SHIPMENT event;
- GTIN/LOAD and vessel ARRI ACT at the POL;
- PLN, EST and a **revised** EST at the POD.

`tests/fixtures/dcsa/v22_single_revised_est.json`

```json
[
 {"eventID":"s-conf","eventCreatedDateTime":"2026-10-15T08:00:00Z","eventType":"SHIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-15T08:00:00Z","shipmentEventTypeCode":"CONF","documentID":"267546627","documentTypeCode":"BKG"},
 {"eventID":"e-gtin","eventCreatedDateTime":"2026-10-18T03:00:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-18T10:12:00+08:00","equipmentEventTypeCode":"GTIN","equipmentReference":"MSKU1234565","ISOEquipmentCode":"45G1","emptyIndicatorCode":"LADEN","eventLocation":{"locationName":"Yangshan Terminal","UNLocationCode":"CNSHA","facilityCode":"YSD","facilityCodeListProvider":"SMDG"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]},
 {"eventID":"t-arr-pol","eventCreatedDateTime":"2026-10-20T00:30:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T05:00:00+08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-CNSHA-1","carrierVoyageNumber":"443E","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityCode":"YSD","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9778791","vesselName":"MAERSK EXAMPLE"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]},
 {"eventID":"e-load","eventCreatedDateTime":"2026-10-20T14:00:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T21:40:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"MSKU1234565","ISOEquipmentCode":"45G1","emptyIndicatorCode":"LADEN","transportCall":{"transportCallID":"TC-CNSHA-1","carrierVoyageNumber":"443E","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityCode":"YSD","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9778791","vesselName":"MAERSK EXAMPLE"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]},
 {"eventID":"t-dep-pol","eventCreatedDateTime":"2026-10-21T02:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-21T09:30:00+08:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-CNSHA-1","carrierVoyageNumber":"443E","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityCode":"YSD","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9778791","vesselName":"MAERSK EXAMPLE"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]},
 {"eventID":"t-arr-pod-pln","eventCreatedDateTime":"2026-10-01T00:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"PLN","eventDateTime":"2026-11-10T06:00:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLAX-1","carrierVoyageNumber":"443E","transportCallSequenceNumber":2,"UNLocationCode":"USLAX","facilityCode":"APMT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9778791","vesselName":"MAERSK EXAMPLE"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]},
 {"eventID":"t-arr-pod-est1","eventCreatedDateTime":"2026-11-01T12:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-12T14:00:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLAX-1","carrierVoyageNumber":"443E","transportCallSequenceNumber":2,"UNLocationCode":"USLAX","facilityCode":"APMT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9778791","vesselName":"MAERSK EXAMPLE"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]},
 {"eventID":"t-arr-pod-est2","eventCreatedDateTime":"2026-11-04T09:15:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-13T09:30:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLAX-1","carrierVoyageNumber":"443E","transportCallSequenceNumber":2,"UNLocationCode":"USLAX","facilityCode":"APMT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9778791","vesselName":"MAERSK EXAMPLE"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]}
]
```

**`v22_transshipment_discharged.json`**: Genoa → Tanger Med (transshipment) → New York, then discharged and gated out.

`tests/fixtures/dcsa/v22_transshipment_discharged.json`

```json
[
 {"eventID":"e1","eventCreatedDateTime":"2026-10-10T16:05:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-10T18:00:00+02:00","equipmentEventTypeCode":"LOAD","equipmentReference":"MEDU1234567","emptyIndicatorCode":"LADEN","documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}],"transportCall":{"transportCallID":"TC-ITGOA-F","carrierVoyageNumber":"FA642W","transportCallSequenceNumber":1,"UNLocationCode":"ITGOA","facilityCode":"PSAGP","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9000011","vesselName":"MSC FEEDER"}}},
 {"eventID":"t1","eventCreatedDateTime":"2026-10-11T00:10:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-11T02:00:00+02:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-ITGOA-F","carrierVoyageNumber":"FA642W","transportCallSequenceNumber":1,"UNLocationCode":"ITGOA","facilityCode":"PSAGP","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9000011","vesselName":"MSC FEEDER"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}]},
 {"eventID":"t2","eventCreatedDateTime":"2026-10-14T06:10:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-14T07:00:00+01:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-MAPTM-F","carrierVoyageNumber":"FA642W","transportCallSequenceNumber":2,"UNLocationCode":"MAPTM","facilityCode":"TC3","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9000011","vesselName":"MSC FEEDER"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}]},
 {"eventID":"e2","eventCreatedDateTime":"2026-10-14T14:30:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-14T15:20:00+01:00","equipmentEventTypeCode":"DISC","equipmentReference":"MEDU1234567","emptyIndicatorCode":"LADEN","documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}],"transportCall":{"transportCallID":"TC-MAPTM-F","carrierVoyageNumber":"FA642W","transportCallSequenceNumber":2,"UNLocationCode":"MAPTM","facilityCode":"TC3","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9000011","vesselName":"MSC FEEDER"}}},
 {"eventID":"e3","eventCreatedDateTime":"2026-10-19T03:20:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-19T04:10:00+01:00","equipmentEventTypeCode":"LOAD","equipmentReference":"MEDU1234567","emptyIndicatorCode":"LADEN","documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}],"transportCall":{"transportCallID":"TC-MAPTM-M","carrierVoyageNumber":"UX643A","transportCallSequenceNumber":5,"UNLocationCode":"MAPTM","facilityCode":"TC3","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9000022","vesselName":"MSC MOTHER"}}},
 {"eventID":"t3","eventCreatedDateTime":"2026-10-19T11:10:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-19T12:00:00+01:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-MAPTM-M","carrierVoyageNumber":"UX643A","transportCallSequenceNumber":5,"UNLocationCode":"MAPTM","facilityCode":"TC3","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9000022","vesselName":"MSC MOTHER"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}]},
 {"eventID":"t4","eventCreatedDateTime":"2026-11-02T12:20:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-11-02T07:15:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USNYC-M","carrierVoyageNumber":"UX643A","transportCallSequenceNumber":6,"UNLocationCode":"USNYC","facilityCode":"PNCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9000022","vesselName":"MSC MOTHER"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}]},
 {"eventID":"e4","eventCreatedDateTime":"2026-11-03T03:45:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-11-02T22:40:00-05:00","equipmentEventTypeCode":"DISC","equipmentReference":"MEDU1234567","emptyIndicatorCode":"LADEN","documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}],"transportCall":{"transportCallID":"TC-USNYC-M","carrierVoyageNumber":"UX643A","transportCallSequenceNumber":6,"UNLocationCode":"USNYC","facilityCode":"PNCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9000022","vesselName":"MSC MOTHER"}}},
 {"eventID":"e5","eventCreatedDateTime":"2026-11-04T14:10:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-11-04T09:05:00-05:00","equipmentEventTypeCode":"GTOT","equipmentReference":"MEDU1234567","emptyIndicatorCode":"LADEN","documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"EBKG12345678"}],"eventLocation":{"UNLocationCode":"USNYC","facilityCode":"PNCT","facilityCodeListProvider":"SMDG"}}
]
```

**`v22_multi_container_two_vessels.json`**: one booking, two boxes on two vessels. The second box was rolled to a later sailing.

`tests/fixtures/dcsa/v22_multi_container_two_vessels.json`

```json
[
 {"eventID":"e-a-load","eventCreatedDateTime":"2026-10-12T03:30:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-12T11:00:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU1111111","emptyIndicatorCode":"LADEN","transportCall":{"transportCallID":"TC-A-NGB","carrierVoyageNumber":"0AB1CW","transportCallSequenceNumber":1,"UNLocationCode":"CNNGB","facilityCode":"NBCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9300001","vesselName":"CMA CGM ALPHA"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM1234567"}]},
 {"eventID":"e-b-load","eventCreatedDateTime":"2026-10-19T08:45:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-19T16:30:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU2222222","emptyIndicatorCode":"LADEN","transportCall":{"transportCallID":"TC-B-NGB","carrierVoyageNumber":"0AC3EW","transportCallSequenceNumber":1,"UNLocationCode":"CNNGB","facilityCode":"NBCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9300002","vesselName":"CMA CGM BETA"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM1234567"}]},
 {"eventID":"t-a-dep","eventCreatedDateTime":"2026-10-12T14:10:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-12T22:00:00+08:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-A-NGB","carrierVoyageNumber":"0AB1CW","transportCallSequenceNumber":1,"UNLocationCode":"CNNGB","facilityCode":"NBCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9300001","vesselName":"CMA CGM ALPHA"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM1234567"}]},
 {"eventID":"t-b-dep","eventCreatedDateTime":"2026-10-19T19:10:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T03:00:00+08:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-B-NGB","carrierVoyageNumber":"0AC3EW","transportCallSequenceNumber":1,"UNLocationCode":"CNNGB","facilityCode":"NBCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9300002","vesselName":"CMA CGM BETA"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM1234567"}]},
 {"eventID":"t-a-arr-est","eventCreatedDateTime":"2026-11-02T10:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-09T10:00:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-A-SAV","carrierVoyageNumber":"0AB1CW","transportCallSequenceNumber":4,"UNLocationCode":"USSAV","facilityCode":"GCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9300001","vesselName":"CMA CGM ALPHA"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM1234567"}]},
 {"eventID":"t-a-arr-pln","eventCreatedDateTime":"2026-10-01T00:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"PLN","eventDateTime":"2026-11-07T10:00:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-A-SAV","carrierVoyageNumber":"0AB1CW","transportCallSequenceNumber":4,"UNLocationCode":"USSAV","facilityCode":"GCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9300001","vesselName":"CMA CGM ALPHA"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM1234567"}]},
 {"eventID":"t-b-arr-est","eventCreatedDateTime":"2026-11-03T10:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-16T08:00:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-B-SAV","carrierVoyageNumber":"0AC3EW","transportCallSequenceNumber":4,"UNLocationCode":"USSAV","facilityCode":"GCT","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9300002","vesselName":"CMA CGM BETA"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM1234567"}]}
]
```

**`v3_envelopes_with_retraction.json`**: v3 envelopes, `references[type=EQ]`, a later EST that is then withdrawn, and a SHIPMENT event.

`tests/fixtures/dcsa/v3_envelopes_with_retraction.json`

```json
[
 {"metadata":{"eventID":"v3-shp-1","eventCreatedDateTime":"2026-10-20T10:00:00Z","publisher":{"partyName":"Example Carrier"},"publisherRole":"CA","eventType":"SHIPMENT"},"payload":{"eventClassifierCode":"ACT","eventDateTime":"2026-10-20T10:00:00Z","shipmentEventTypeCode":"CONF","documentTypeCode":"BKG","documentReference":"BKG000111"}},
 {"metadata":{"eventID":"v3-load-1","eventCreatedDateTime":"2026-10-24T20:00:00Z","publisher":{"partyName":"Example Carrier"},"publisherRole":"CA","eventType":"EQUIPMENT"},"payload":{"eventClassifierCode":"ACT","eventDateTime":"2026-10-24T21:15:00+02:00","equipmentEventTypeCode":"LOAD","equipmentReference":"HLXU1234567","emptyIndicatorCode":"LADEN","isTransshipmentMove":false,"transportCall":{"transportCallReference":"TCR-ANR-1","transportCallSequenceNumber":1,"location":{"locationType":"FACI","UNLocationCode":"BEANR","facilityCode":"PSADGD","facilityCodeListProvider":"SMDG"},"facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9500001","name":"EXAMPLE EXPRESS"},"carrierExportVoyageNumber":"612W"},"relatedDocumentReferences":[{"type":"BKG","value":"BKG000111"}]}},
 {"metadata":{"eventID":"v3-arr-pln","eventCreatedDateTime":"2026-10-01T00:00:00Z","publisher":{"partyName":"Example Carrier"},"publisherRole":"CA","eventType":"TRANSPORT"},"payload":{"eventClassifierCode":"PLN","eventDateTime":"2026-11-19T06:00:00-06:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallReference":"TCR-HOU-1","transportCallSequenceNumber":2,"location":{"locationType":"UNLO","UNLocationCode":"USHOU"},"facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9500001","name":"EXAMPLE EXPRESS"},"carrierImportVoyageNumber":"612W"},"relatedDocumentReferences":[{"type":"BKG","value":"BKG000111"}]}},
 {"metadata":{"eventID":"v3-arr-1","eventCreatedDateTime":"2026-10-28T12:00:00Z","publisher":{"partyName":"Example Carrier"},"publisherRole":"CA","eventType":"TRANSPORT"},"payload":{"eventClassifierCode":"EST","eventDateTime":"2026-11-20T06:00:00-06:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallReference":"TCR-HOU-1","transportCallSequenceNumber":2,"location":{"locationType":"UNLO","UNLocationCode":"USHOU"},"facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9500001","name":"EXAMPLE EXPRESS"},"carrierImportVoyageNumber":"612W"},"relatedDocumentReferences":[{"type":"BKG","value":"BKG000111"}],"references":[{"type":"EQ","value":"HLXU1234567"}]}},
 {"metadata":{"eventID":"v3-arr-2","eventCreatedDateTime":"2026-11-03T12:00:00Z","publisher":{"partyName":"Example Carrier"},"publisherRole":"CA","eventType":"TRANSPORT"},"payload":{"eventClassifierCode":"EST","eventDateTime":"2026-11-22T06:00:00-06:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallReference":"TCR-HOU-1","transportCallSequenceNumber":2,"location":{"locationType":"UNLO","UNLocationCode":"USHOU"},"facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9500001","name":"EXAMPLE EXPRESS"},"carrierImportVoyageNumber":"612W"},"relatedDocumentReferences":[{"type":"BKG","value":"BKG000111"}],"references":[{"type":"EQ","value":"HLXU1234567"}]}},
 {"metadata":{"eventID":"v3-ret-1","eventCreatedDateTime":"2026-11-04T08:00:00Z","publisher":{"partyName":"Example Carrier"},"publisherRole":"CA","eventType":"TRANSPORT","retractedEventID":"v3-arr-2"}}
]
```

**`v22_no_pod_yet.json`**: only port-of-loading events, including the vessel's ACT arrival at the POL.

`tests/fixtures/dcsa/v22_no_pod_yet.json`

```json
[
 {"eventID":"e-gtin","eventCreatedDateTime":"2026-10-18T03:00:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-18T10:12:00+08:00","equipmentEventTypeCode":"GTIN","equipmentReference":"MSKU1234565","ISOEquipmentCode":"45G1","emptyIndicatorCode":"LADEN","eventLocation":{"locationName":"Yangshan Terminal","UNLocationCode":"CNSHA","facilityCode":"YSD","facilityCodeListProvider":"SMDG"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]},
 {"eventID":"t-arr-pol","eventCreatedDateTime":"2026-10-20T00:30:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T05:00:00+08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-CNSHA-1","carrierVoyageNumber":"443E","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityCode":"YSD","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9778791","vesselName":"MAERSK EXAMPLE"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]},
 {"eventID":"e-load","eventCreatedDateTime":"2026-10-20T14:00:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T21:40:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"MSKU1234565","ISOEquipmentCode":"45G1","emptyIndicatorCode":"LADEN","transportCall":{"transportCallID":"TC-CNSHA-1","carrierVoyageNumber":"443E","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityCode":"YSD","facilityCodeListProvider":"SMDG","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9778791","vesselName":"MAERSK EXAMPLE"}},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"267546627"}]}
]
```

Expected results (`resolve_pod` without a hint; times are naive local):

| Fixture | `len(parse_events)` | POD | Per container | `select_pod_arrival` (vessel) | `first` | `latest` |
|---|---|---|---|---|---|---|
| `v22_single_revised_est` | 8 | USLAX | `MSKU1234565` → 2026-11-13 09:30 EST `TRANSPORT ARRI EST`, facility APMT; key `None` → same time | 2026-11-13 09:30 EST | 2026-11-13 09:30 EST | same |
| `v22_transshipment_discharged` | 9 | USNYC (MAPTM excluded: LOAD there) | `MEDU1234567` → 2026-11-02 22:40 ACT `EQUIPMENT DISC ACT`, facility PNCT; `None` → 2026-11-02 07:15 ACT `TRANSPORT ARRI ACT` | 2026-11-02 07:15 ACT | 2026-11-02 22:40 ACT | same |
| `v22_multi_container_two_vessels` | 7 | USSAV | `CMAU1111111` → 2026-11-09 10:00 EST (vessel 9300001 matched via its LOAD); `CMAU2222222` → 2026-11-16 08:00 EST (vessel 9300002); `None` → 2026-11-16 08:00 EST | 2026-11-16 08:00 EST | `CMAU1111111` 2026-11-09 10:00 | `CMAU2222222` 2026-11-16 08:00 |
| `v3_envelopes_with_retraction` | 4 (SHIPMENT, LOAD, ARRI PLN, ARRI EST `v3-arr-1`; `v3-arr-2` withdrawn; withdrawal envelope emits nothing) | USHOU | `HLXU1234567` → 2026-11-20 06:00 EST | 2026-11-20 06:00 EST | 2026-11-20 06:00 EST | same |
| `v22_no_pod_yet` | 3 | `None` | `{}` | `None` | `None` | `None` |

`to_eta_result(first, carrier=Carrier.MAERSK, booking="267546627", containers=("MSKU1234565",))` for the
first fixture gives:
- `eta == datetime(2026,11,13,9,30)`;
- `source == "DCSA TRANSPORT ARRI EST at USLAX/APMT"`;
- `is_actual is False`.

## A.6 Tests — `tests/test_dcsa.py` (pure, no HTTP except the pagination helper)

1. `test_expected_results[fixture]`: parametrised over the table in §A.5. Check every column.
2. `test_wrappers`: these all give the same events as the bare list:
   - fixture 1 wrapped as `{"events": …}`, `{"data": …}` and `{"items": …}`;
   - a single event object gives one event.
   - `{"foo": 1}` → `ValueError` whose message lists `foo`.
3. `test_location_fallbacks` (07 req. 1, 08 E3, 06 quirks): the UN/LOCODE is read
   - only under `transportCall.location`;
   - only under `eventLocation`;
   - only under `transportCall.UNLocationCode`;
   - in camelCase `transportCall.unLocationCode` / `eventLocation.unLocationCode`;
   - all lower-case `unlocationcode: "uslax"` → `USLAX`.
   `transportCallSequenceNumber` `"3"` → `3`, and `eventId` maps to `event_id`.
3b. `test_msc_api_shape` (06 §9.2 `api_in_transit.json`): parse as a bare list and as `{"events": …}`;
    `raw["description"] == "Estimated Time of Arrival"`; `"2026-10-20T18:00:00.0000000+00:00"` →
    `datetime(2026,10,20,18,0)`. `booking_arrival(events, policy="first")` with no hint →
    `2026-11-12 06:00` EST at `GHTEM`, container `MSCU1111111`.
4. `test_raw_is_original_dict` (08 E1): `e.raw is item` for v2.2. Unknown keys such as
   `carrierSpecificData` stay reachable via `raw`.
5. `test_datetime_parsing`: each of these parses:
   - `Z`, `+0000`, `+05:30`;
   - 7 fractional digits;
   - a space separator;
   - date-only (→ 00:00);
   - basic format `20261112T080000Z`.
   Also: `"2026-11-10T14:00:00-08:00"` → `datetime(2026,11,10,14,0)` (08 E2), and created
   `"2026-11-04T09:15:00+01:00"` → `datetime(2026,11,4,8,15)` (naive UTC).
6. `test_sentinel_years_never_crash`:
   - events with `eventDateTime` `0001-01-01T00:00:00+01:00` or `9999-12-31T23:59:59.9999999-12:00` are
     skipped;
   - a valid event whose `eventCreatedDateTime` is `9999-…` is kept with `created_datetime is None`;
   - no exception.
7. `test_malformed_events_skipped`: these are skipped without raising:
   - unknown `eventType`;
   - missing classifier;
   - missing type code;
   - unparsable date;
   - non-dict list items.
   The deprecated `eventTypeCode` alias is accepted.
8. `test_retraction_v3`: the withdrawn event is gone, and withdrawing an id that does not exist is harmless.
9. `test_rank_key`:
   - ACT beats a newer EST;
   - the newest created wins among ESTs (08 E5 example);
   - created ties are broken by the later `eventDateTime`;
   - mixed naive and aware `created_datetime` values do not raise (07 req. 4).
10. `test_resolve_pod_ipi_rail` (07 req. 2): vessel ARRI at USLAX, then RAIL DEPA at USLAX and RAIL ARRI at
    USCHI → `USLAX`.
11. `test_resolve_pod_loading_guard`: the `v22_no_pod_yet` fixture → `None`.
12. `test_resolve_pod_disc_fallback`: equipment-only events (LOAD at A, then DISC at B), with no transport
    events → `B`.
13. `test_hint_restricts_location`:
    - `booking_arrival(fixture1, policy="first", pod_unlocode="CNSHA")` → the POL vessel ARRI ACT
      2026-10-20 05:00 (the hint wins);
    - `pod_unlocode="USHOU"` → `None`.
14. `test_post_arrival_proof`: GTOT ACT laden at the POD and no DISC → ACT with basis `"EQUIPMENT GTOT ACT"`.
    A **later** transport EST at the POD does not override it.
15. `test_empty_box_ignored`: an EMPTY DISC ACT at the POD does not count as an arrival.
16. `test_none_key_and_fallback` (08 E4): with only transport arrivals at the POD, every container *and*
    the key `None` get the vessel-level arrival, and `None` is the last key. A box with its own DISC ACT
    keeps it.
17. `test_booking_policy`:
    - `first` / `latest` on the multi-container fixture;
    - an invalid policy → `ValueError`;
    - no containers at all → the `None` entry is used.
18. `test_eta_result_from_payload`:
    - `[]` → `CarrierLookupError` containing "no tracking events";
    - no-POD fixture → `NoEtaError` containing "Can't determine ETA";
    - containers more than a day apart → WARNING logged (caplog);
    - `details["per_container"]` is filled.
19. `test_to_local_naive_table`: every row of §A.3.3.
20. `test_explain_selection`: the lines mention `USLAX` and `2026-11-13T09:30`.
21. `test_fetch_event_pages` (respx, only if §A.3.12 is implemented):
    - cursor form and same-host absolute URL;
    - a foreign host is not followed;
    - the `max_pages` cap logs a WARNING.

Cross-spec acceptance: these must pass unchanged against this module:
- `tests/test_maersk.py` (07 §9.3, tests 19 and 24–25);
- `tests/test_carrier_cma.py` (08 §9.4);
- the MSC rescue-path tests in `tests/test_msc.py` (06 §9.3).

## A.7 Probe / verification

`dcsa.py` has no probe command of its own. Each DCSA carrier's `probe()` includes
`"explain": explain_selection(...)` next to the redacted raw events. To verify:
1. Run `eta probe maersk --booking <real>`, and compare the explained POD and ETA with the carrier's website
   once.
2. Run `eta probe cma --booking <real>` the same way.
3. If the times are off by whole hours, the carrier is sending UTC (limitation 1 in §A.4). Report it; do not
   "fix" times globally.

---

# Part B — `carriers/aggregator.py` (optional third-party provider)

## B.1 Summary

A licensed tracking aggregator gives one API key and one integration for every carrier. Use it:
- while official API approval from Maersk, CMA or MSC is pending;
- if a carrier refuses API access;
- for Grimaldi, which has no public API (Part C).

It replaces the browser lookups with plain HTTPS calls.

**Trade-off:** aggregators are mostly **asynchronous**. You register a booking once, and the provider polls
the carrier for you. The first lookup of a *new* booking may therefore not have data yet. Bookings already
registered (in earlier runs) answer immediately. They are paid services.

## B.2 Provider comparison (researched 2026-11-05)

| Provider | Booking-number tracking | Model | Pricing | API | Verdict |
|---|---|---|---|---|---|
| **ShipsGo (API v2)** | Yes: `booking_number` (or container) + carrier SCAC. MSC `MSCU`, Maersk `MAEU`, CMA `CMDU` listed as examples. Grimaldi: marketing pages exist; **confirm via `GET /ocean/carriers`**. | Async. `POST /ocean/shipments` once, then `GET` details (status `NEW` → `INPROGRESS` → data). **Duplicates return 409 with the existing shipment and no charge.** Server-side filter by `booking_number`. | Prepaid credits, 1 credit per new shipment; GETs are free. API-usage fee per the help center: none under 100 shipments/yr, custom between 100 and 2,000, one-time USD 500 at 2,000+. | Public OpenAPI 3.1 (`api.shipsgo.com/docs/v2/specs/openapi.json`). Header `X-Shipsgo-User-Token`. 100 req/min per company. Creates are processed serially (429 if concurrent). | **Recommended default:** cheapest for a small forwarder, simplest dedupe, fully documented. |
| Terminal49 | Yes for MSC/Maersk/CMA (`request_type: booking_number`). **No Grimaldi integration.** | Async: tracking request `pending` → `created` (can be `awaiting_manifest` up to 7 days), then a shipment. Webhooks. | Free plan: 10 active containers, and reading data via the API needs a trial or paid plan. Paid plans are per container (amounts not published). | JSON:API, `Authorization: Token …`, 100 req/min. Excellent docs (`terminal49.com/docs/llms.txt`). | Good alternative backend (§B.9). |
| Vizion | Booking numbers in **beta** (enable via support) for CMA, Evergreen, Maersk, MSC, ONE. No Grimaldi. | Async, webhook-centric, container-level child references. | Enterprise, via sales. | `X-API-Key`. Uses DCSA event codes. | Heavier than needed. |
| SeaRates (DP World) | Was synchronous `GET tracking.searates.com/tracking?api_key&number&sealine&type=BK`. | Sync. | Per container. | searates.com now redirects to dpworld.com, and the API host did not respond from our environment (2026-11-05). | **Avoid** until its status is clear. |
| Portcast, project44 | Yes (enterprise). | Async platform. | Enterprise contracts. | Large platforms. | Overkill for this tool. |

Data licensing: these providers obtain the data from carriers under their own agreements. Use the API
only as their terms allow (internal operational use).

## B.3 Access & onboarding (ShipsGo)

1. Create a company account at shipsgo.com. Sign-up includes a few free credits (help center: 3).
2. Dashboard → **Integrations → API Tokens** → create a token. The user must have the **Full Access** or
   **Co-Worker** role (Co-Worker Plus cannot manage other users' shipments).
3. Buy credits. For the API-usage fee, contact sales@shipsgo.com (see the tiers in §B.2). The expected cost
   is about one credit per booking over its whole life, because shipments are reused across runs (§B.7.3).
4. Put the token in `.env` as `AGG_API_TOKEN`. Then set `ETA_PROVIDER_<CARRIER>=aggregator` for the
   carriers it should serve (Part D).
5. Run the probe (§B.11) before the first real run.

## B.4 Configuration

```python
@dataclass(frozen=True)
class AggregatorSettings:
    api_token: str                                 # AGG_API_TOKEN — required; empty ⇒ settings.aggregator is None
    state_file: Path                               # derived: <ETA_STATE_DIR>/aggregator_tracking.json (not an env var)
    provider: str = "shipsgo"                      # AGG_PROVIDER — only "shipsgo" is implemented (others → ConfigError, §B.9)
    base_url: str = "https://api.shipsgo.com/v2"   # AGG_BASE_URL
    max_concurrency: int = 2                       # AGG_MAX_CONCURRENCY (03 §10)
    rate_per_minute: int = 90                      # AGG_RATE_PER_MINUTE (ShipsGo allows 100/min per company; keep headroom)
    timeout_s: float = 30.0                        # AGG_TIMEOUT_SECONDS
    allow_create: bool = True                      # AGG_ALLOW_CREATE — false = never spend credits; only read already-tracked bookings
    max_creates_per_run: int = 100                 # AGG_MAX_CREATES_PER_RUN — credit safety valve
    first_lookup_wait_s: int = 45                  # AGG_FIRST_LOOKUP_WAIT_S — how long get_eta polls a FRESH shipment (0 = no wait)
    poll_interval_s: int = 15                      # AGG_POLL_INTERVAL_S
    fresh_minutes: int = 30                        # AGG_FRESH_MINUTES — shipments first seen less than this ago are "fresh"
    scac: Mapping[Carrier, str] = field(default_factory=lambda: {
        Carrier.MSC: "MSCU", Carrier.MAERSK: "MAEU", Carrier.CMA: "CMDU", Carrier.GRIMALDI: "GRIU"})
                                                   # AGG_SCAC_MSC / AGG_SCAC_MAERSK / AGG_SCAC_CMA / AGG_SCAC_GRIMALDI
```

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `AGG_API_TOKEN` | yes, to enable | — | ShipsGo API token (secret: redacted everywhere) |
| `AGG_PROVIDER` | no | `shipsgo` | Backend (§B.9) |
| `AGG_BASE_URL` | no | `https://api.shipsgo.com/v2` | |
| `AGG_MAX_CONCURRENCY` | no | `2` | Parallel HTTP calls |
| `AGG_RATE_PER_MINUTE` | no | `90` | `AsyncLimiter(n, 60)` |
| `AGG_TIMEOUT_SECONDS` | no | `30` | |
| `AGG_ALLOW_CREATE` | no | `true` | |
| `AGG_MAX_CREATES_PER_RUN` | no | `100` | |
| `AGG_FIRST_LOOKUP_WAIT_S` | no | `45` | |
| `AGG_POLL_INTERVAL_S` | no | `15` | |
| `AGG_FRESH_MINUTES` | no | `30` | |
| `AGG_SCAC_MSC` / `_MAERSK` / `_CMA` / `_GRIMALDI` | no | `MSCU` / `MAEU` / `CMDU` / `GRIU` | Carrier codes sent to the provider (Grimaldi's code is UNVERIFIED for ShipsGo; check the probe) |

Validation:
- Integers must be ≥ 0, except `rate_per_minute` and `max_concurrency`, which must be ≥ 1.
- `AGG_PROVIDER` other than `shipsgo` → `ConfigError("AGG_PROVIDER=<x> is described in 09 §B.9 but not implemented")`.

## B.5 Endpoints (ShipsGo API v2) — VERIFIED from the published OpenAPI (`ShipsGo API 2.0`)

Base `https://api.shipsgo.com/v2`. Every request sends:
- `X-Shipsgo-User-Token: <AGG_API_TOKEN>`
- `Accept: application/json`

POSTs send JSON (`Content-Type: application/json`).

| Purpose | Request | Success | Notes |
|---|---|---|---|
| Find an existing shipment | `GET /ocean/shipments?filters[booking_number]=eq:{booking}&filters[carrier]=eq:{scac}&take=25` | 200 `{"message":"SUCCESS","shipments":[OceanShipmentsListData…],"meta":{"more":bool,"total":int\|null}}` | Filter syntax `filters[field]=operator:value`. `booking_number` supports `eq`. Free. |
| Create (start tracking) | `POST /ocean/shipments` body `{"booking_number": "<booking>", "carrier": "<SCAC>"}` | 200 `{"message":"SUCCESS","shipment":{"id":…}}`, or **409** `{"message":"ALREADY_EXISTS","shipment":{"id":…}}` (existing; **no charge**) | 402 `NOT_ENOUGH_CREDITS`; 422 bad payload. Creates are processed one at a time per company: concurrent creates can get 429 `TOO_MANY_CONCURRENT_REQUESTS`, so **serialise them**. Pattern: `booking_number` `^[a-zA-Z0-9/-]+$`, `carrier` `^(SG_)?[A-Z0-9]{4}$`. Do **not** send `reference`: it participates in duplicate detection. |
| Details | `GET /ocean/shipments/{id}` | 200 `{"message":"SUCCESS","shipment":OceanShipment}` | 404 means the shipment was deleted (e.g. in the dashboard). Free. |
| Carriers (probe only) | `GET /ocean/carriers?filters[status]=eq:ACTIVE&take=100&skip={n}` | 200 `{"carriers":[{"scac","name","status"}],"meta":{"more":…}}` | Page until `meta.more` is false. `filters[name]=contains:GRIMALDI` finds Grimaldi. |

Status codes:
- `401`: invalid token;
- `403`: role not allowed;
- `404`, `409`, `402`, `422` as above;
- `429`: rate limit (100 req/min per company). Headers `X-RateLimit-Limit/Remaining/Reset`;
- `5xx`.

`OceanShipment` fields used:
- `id`, `booking_number`, `carrier{scac,name}`, `status`, `discarded_at`;
- `route.port_of_discharge{location{code,name,timezone,country}, date_of_discharge,
  date_of_discharge_initial, date_of_discharge_predicted}`;
- `containers[]{number, status, movements[]{event, status, location{code,name,timezone}, vessel{imo,name}|null,
  voyage, timestamp}}`.

Field values:
- `status` ∈ {NEW, INPROGRESS, BOOKED, LOADED, SAILING, ARRIVED, DISCHARGED, UNTRACKED}.
- Movement `event` ∈ {EMSH, GTIN, LOAD, DEPA, ARRV, DISC, GTOT, EMRT}, `status` ∈ {EST, ACT}.
- Movement `timestamp` is "ISO 8601 with timezone". **Which offset (UTC or local) is UNVERIFIED**, so always
  convert to `location.timezone` (§A.3.3, idempotent).
- `date_of_discharge` is "the date when the shipment is expected to be discharged".
- `date_of_discharge_predicted` is ShipsGo's own ML estimate. It is **not used**: the program reports
  carrier data only.
- `container.number` can be `NOT_ASSIGNED`.

## B.6 Auth

A static API token in a header. There is **no** `TokenProvider` and no `tokens.json` entry, and
`jwt_exp`/the token store are not used. Token providers must never acquire their semaphore (03 §8); that
does not apply here, because there is none. A 401/403 raises `CarrierLookupError` immediately (§B.7.6).

## B.7 Module design

### B.7.1 Public surface

```python
class AggregatorBookingError(CarrierLookupError):
    """A per-booking aggregator condition (not tracked yet / still fetching / UNTRACKED / creation cap reached).
    The circuit breaker (10 §6) must NOT count it: it says nothing about the provider being down."""

@dataclass(frozen=True, slots=True)
class AggregatorSnapshot:
    tracking_id: str
    status: str                       # provider status, upper-case
    ready: bool                       # provider has carrier data
    failure: str | None               # permanent per-booking failure (e.g. "UNTRACKED"), else None
    pod_unlocode: str | None
    events: tuple[DcsaEvent, ...]     # provider movements adapted to DCSA (§B.7.4)
    route_pod_arrival: DcsaArrival | None
    containers: tuple[str, ...]
    raw: Mapping[str, Any]            # body as received (redact before logging/probe output)

class AggregatorBackend(Protocol):
    name: str                         # "ShipsGo" (used in messages and EtaResult.source)
    key: str                          # "shipsgo" (state-file namespace)
    async def find_existing(self, scac: str, booking: str) -> str | None: ...
    async def create(self, scac: str, booking: str) -> str: ...          # returns tracking id; 409 counts as success
    async def fetch(self, tracking_id: str) -> AggregatorSnapshot | None: ...   # None ⇔ HTTP 404
    async def list_carriers(self) -> list[dict[str, Any]]: ...
    async def aclose(self) -> None: ...

class ShipsGoBackend:  # implements AggregatorBackend (§B.7.4)
    def __init__(self, settings: AggregatorSettings, *, http: httpx.AsyncClient | None = None): ...

class TrackingState:   # <ETA_STATE_DIR>/aggregator_tracking.json (§B.7.3)
    def __init__(self, path: Path): ...
    def get(self, provider_key: str, scac: str, booking: str) -> dict[str, Any] | None: ...
    def put(self, provider_key: str, scac: str, booking: str, entry: dict[str, Any]) -> None: ...
    def forget(self, provider_key: str, scac: str, booking: str) -> None: ...

class AggregatorClient:
    def __init__(self, settings: AggregatorSettings, *, backend: AggregatorBackend | None = None,
                 state: TrackingState | None = None, clock: Clock | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep): ...   # sleep injectable for tests
    def view(self, carrier: Carrier) -> CarrierClient: ...                         # cached per carrier
    async def prepare(self, items: Sequence[tuple[Carrier, str]]) -> dict[str, int]: ...
    async def get_eta(self, carrier: Carrier, booking: str) -> EtaResult: ...
    async def probe(self, carrier: Carrier | None, booking: str | None) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...

class AggregatorCarrierView:   # implements CarrierClient (03 §9) for one carrier
    carrier: Carrier
    async def get_eta(self, booking: str) -> EtaResult        # → client.get_eta(self.carrier, booking)
    async def probe(self, booking: str) -> dict[str, Any]     # → client.probe(self.carrier, booking)
    async def aclose(self) -> None                            # no-op: the router closes the AggregatorClient once

def build_client(settings: Settings, token_store: TokenStore) -> AggregatorClient | None:
    """None when settings.aggregator is None. token_store is unused (static token).
    Unlike the carrier modules this returns the multi-carrier client; the router asks it for views."""

AGG_POLICY: Mapping[Carrier, Policy] = {Carrier.MSC: "first", Carrier.MAERSK: "first",
                                        Carrier.CMA: "latest", Carrier.GRIMALDI: "latest"}   # parity with 06/07/08
```

### B.7.2 HTTP rules (ShipsGoBackend)

- Construct an own `httpx.AsyncClient` via `http.new_async_client(base_url=settings.base_url,
  timeout=settings.timeout_s, headers={"X-Shipsgo-User-Token": token})`, unless one is injected.
  `aclose()` closes only an owned client.
- Every call goes through `http.request(..., service="aggregator", limiter=AsyncLimiter(rate_per_minute, 60))`.
  It holds the client's `asyncio.Semaphore(max_concurrency)` **only around the HTTP call**, never across
  sleeps.
- The list and detail GETs and the carriers GET use `expected=(200,)`, with 404 allowed on details.
- Create uses `expected=(200, 409)` and the default `idempotent=False`. A read timeout on create is **not**
  retried, which avoids a double charge. The next run's `find_existing` picks the shipment up.
- Error mapping inside the backend (`ServiceError.status`):

  | Status | Error |
  |---|---|
  | 401 | `CarrierLookupError("ShipsGo rejected the API token (HTTP 401) — check AGG_API_TOKEN")` |
  | 403 | `CarrierLookupError("ShipsGo refused the operation (HTTP 403) — the token's user role may not create or read shipments")` |
  | 402 | `CarrierLookupError("ShipsGo: not enough credits to start tracking a new booking (HTTP 402) — buy credits or set AGG_ALLOW_CREATE=false")` |
  | 422 | `CarrierLookupError(f"ShipsGo rejected booking {booking} for carrier {scac} (HTTP 422): {message}")` |

  The 402 is systemic, so it stays a plain `CarrierLookupError` that the breaker counts.

### B.7.3 Tracking state (`aggregator_tracking.json`)

```json
{"version": 1,
 "entries": {"shipsgo:MSCU:EBKG12345678": {"tracking_id": "501234", "created_by_us": true,
             "first_seen_at": "2026-11-05T09:00:00", "last_status": "SAILING", "last_checked_at": "2026-11-05T09:01:10"}}}
```

- Key format: `f"{provider_key}:{scac}:{booking}"`. Times are naive local ISO from the injected clock.
- File handling:
  - load lazily;
  - a missing file is empty;
  - corrupt JSON → WARNING, treat as empty, and overwrite on the next `put`;
  - writes are atomic (temp file in the same directory + `os.replace`) under a `threading.Lock`.
- No secrets live in this file.
- Entries are never deleted automatically, except by `forget()` after a 404. A few hundred bytes per booking
  is fine.

### B.7.4 ShipsGo payload → snapshot

`fetch(id)`: GET details; 404 → `None`. Otherwise `sh = body["shipment"]`, and:
- `status = upper(sh["status"])`
- `ready`:
  - `status in {"BOOKED","LOADED","SAILING","ARRIVED","DISCHARGED"}` → True;
  - `{"NEW","INPROGRESS"}` → False;
  - `"UNTRACKED"` → False, with `failure="UNTRACKED"`;
  - any other value → `bool(sh.get("containers") or sh.get("route"))` (forward compatibility).
- `pod_unlocode = upper(route.port_of_discharge.location.code)`, if present.
- `route_pod_arrival`: if `route.port_of_discharge.date_of_discharge` is set →
  `DcsaArrival(eta=to_local_naive(date, pod_location.timezone), classifier="ACT" if status == "DISCHARGED" else "EST",
  unlocode=pod, facility_code=None, equipment_reference=None, basis="ROUTE POD date_of_discharge")`.
- `containers = tuple(upper(c.number) for c in containers if number and number != "NOT_ASSIGNED")`
- `events` = every movement of every container adapted to a `DcsaEvent`, as follows. Movements with an
  unknown `event`, a `status` other than EST/ACT, or an unparsable timestamp are skipped (DEBUG).

| ShipsGo `event` | `event_type` | `type_code` | `empty_indicator` |
|---|---|---|---|
| `EMSH` (empty to shipper) | EQUIPMENT | `GTOT` | `EMPTY` |
| `GTIN` | EQUIPMENT | `GTIN` | `LADEN` |
| `LOAD` | EQUIPMENT | `LOAD` | `LADEN` |
| `DEPA` | TRANSPORT | `DEPA` | — |
| `ARRV` | TRANSPORT | `ARRI` | — |
| `DISC` | EQUIPMENT | `DISC` | `LADEN` |
| `GTOT` | EQUIPMENT | `GTOT` | `LADEN` |
| `EMRT` (empty return) | EQUIPMENT | `GTIN` | `EMPTY` |

The other `DcsaEvent` fields:
- `classifier` = movement `status`;
- `event_datetime = to_local_naive(timestamp, location.timezone)`;
- `created_datetime = None`;
- `unlocode = upper(location.code)`;
- `mode_of_transport = "VESSEL"` if `vessel` is set, else `None`;
- `vessel_name = vessel.name`, `vessel_imo = str(vessel.imo)` (when not null);
- `voyage = movement.voyage`;
- `equipment_reference` = the container number (EQUIPMENT events only);
- `eq_refs = (number,)` for TRANSPORT events (ShipsGo movements are per container);
- `index` = running position;
- `raw` = the movement dict.

### B.7.5 `get_eta(carrier, booking)` — step by step

```
scac, policy = settings.scac[carrier], AGG_POLICY[carrier]
try:
    1. entry, tid, created_now = await self._ensure_tracking(carrier, scac, booking, allow_create=settings.allow_create)
    2. snap = await backend.fetch(tid)
       if snap is None:                                         # deleted provider-side → forget and re-resolve ONCE
           state.forget(...); entry, tid, created_now = await self._ensure_tracking(...); snap = await backend.fetch(tid)
           if snap is None: raise CarrierLookupError(f"ShipsGo lost shipment {tid} for booking {booking}")
    3. fresh = created_now or (clock.now() - parse(entry["first_seen_at"])) < timedelta(minutes=settings.fresh_minutes)
       deadline = monotonic() + settings.first_lookup_wait_s
       while fresh and not snap.ready and snap.failure is None and monotonic() < deadline:
           await self._sleep(settings.poll_interval_s)          # NOT holding the semaphore
           snap = await backend.fetch(tid) or snap
    4. state.put(..., {**entry, "last_status": snap.status, "last_checked_at": clock.now().isoformat(timespec="seconds")})
    5. if snap.failure: raise AggregatorBookingError(
           f"ShipsGo could not track {carrier} booking {booking} (status {snap.failure}) — the number may be wrong "
           f"or {carrier} publishes no data for it")
       if not snap.ready: raise AggregatorBookingError(
           f"ShipsGo has not fetched {carrier} data for booking {booking} yet (status {snap.status}, "
           f"tracking since {entry['first_seen_at']}) — re-run later")
    6. arrival = dcsa.booking_arrival(snap.events, policy=policy, pod_unlocode=snap.pod_unlocode) or snap.route_pod_arrival
       if arrival is None: raise NoEtaError(
           f"ShipsGo found {carrier} booking {booking} but has no arrival date for the port of discharge. Can't determine ETA.")
    7. result = dcsa.to_eta_result(arrival, carrier=carrier, booking=booking, containers=snap.containers, source_prefix="ShipsGo")
       return replace(result, details={**result.details, "provider": "shipsgo", "tracking_id": tid,
                                        "status": snap.status, "policy": policy, "pod": snap.pod_unlocode})
except (CarrierLookupError, NoEtaError): raise
except AuthError as e:                    raise CarrierLookupError(f"ShipsGo authentication failed: {e}") from e
except ServiceError as e:                 raise CarrierLookupError(f"ShipsGo API error for booking {booking}: {e}") from e
except httpx.HTTPError as e:              raise CarrierLookupError(f"ShipsGo network error: {e!r}") from e
except (KeyError, TypeError, ValueError) as e: raise CarrierLookupError(f"ShipsGo returned an unexpected payload: {e}") from e
```

`_ensure_tracking(carrier, scac, booking, *, allow_create) -> (entry, tracking_id, created_now)`:
1. `entry = state.get(...)`. If it is present, return it with `created_now=False`.
2. `tid = await backend.find_existing(scac, booking)`.
   - Pick the exact match: upper-case `booking_number` and `carrier.scac`.
   - If there are several, take the highest `id`.
   - If found, `put` it with `created_by_us=False` and `first_seen_at=now`, and return it.
3. If `not allow_create`, raise
   `AggregatorBookingError(f"{carrier} booking {booking} is not tracked in ShipsGo and AGG_ALLOW_CREATE=false")`.
4. If `self._creates >= settings.max_creates_per_run`, raise
   `AggregatorBookingError(f"AGG_MAX_CREATES_PER_RUN ({n}) reached — booking {booking} not registered with ShipsGo this run")`.
5. Under the client's **create lock** (an `asyncio.Lock`, so creates are serial):
   - re-check the state, because another coroutine may have just created it;
   - `tid = await backend.create(scac, booking)`;
   - `self._creates += 1`, but only when the response was 200, not 409. The backend reports this through a
     `last_create_was_new` flag, or by returning `(tid, is_new)`; the implementer chooses.
   - `put(created_by_us=True, first_seen_at=now)`;
   - return with `created_now=True`.

`prepare(items)`:
- De-duplicate the items.
- For each `(carrier, booking)`, **sequentially**: call `_ensure_tracking(..., allow_create=settings.allow_create)`.
  Catch every exception and log it as a WARNING; `get_eta` re-raises properly later.
- Return counts `{"known","found","created","failed"}`, and log them at INFO.
- Called once per run by the router (Part D) **before** the per-row tasks start. New bookings then have the
  whole planning phase to become ready.

`probe(carrier, booking)` **never creates** (it costs a credit). It returns:
- `{"provider": "shipsgo", "account_ok": bool, "carriers": [ACTIVE carriers whose scac is in settings.scac values or whose name contains GRIMALDI]}`;
- with a booking:
  - `"state_entry"`, `"find_existing"` (id or None);
  - if tracked: `"raw"` (redacted body), `"snapshot": {status, ready, failure, pod, containers}`,
    `"explain": dcsa.explain_selection(...)`, and `"eta"` (`EtaResult` as a dict, or an error string);
  - otherwise `"note": "not tracked yet — eta run / eta eta would register it (1 credit)"`.

The token must never appear in the output.

### B.7.6 Deviations and notes

- **DEVIATION (MSC via aggregator):** MSC's customs-hold heuristic and its "Full Transshipment Loaded + 7
  days" fallback (06) are MSC-GraphQL rules. They are **not** applied when MSC is routed to the aggregator.
  A booking without a POD date raises `NoEtaError`.
- **Freshness:** ShipsGo refreshes on its own schedule (UNVERIFIED frequency). Dates can lag the carrier's
  site by hours, which is acceptable for a 6-business-day window.
- **First sight of a booking:**
  - it may fail with "re-run later" (`AggregatorBookingError` → `Failed_To_Process.txt`, Carrier Lookup
    Failed);
  - the next run, usually the next day, has data;
  - `AGG_FIRST_LOOKUP_WAIT_S` trades run time for fewer such rows.

## B.8 Fallbacks

- If the aggregator is not configured, rows routed to it fail with the configuration message (Part D).
- If ShipsGo is down, the circuit breaker (10 §6) stops hammering it after N consecutive non-booking
  failures.
- Staff can switch a carrier back with `ETA_PROVIDER_<X>=native` once official access is approved.

## B.9 Swapping providers (Terminal49 outline)

Implement another `AggregatorBackend`; nothing else changes. For Terminal49:
- **Requests:**
  - base `https://api.terminal49.com/v2`;
  - headers `Authorization: Token <key>` and `Content-Type: application/vnd.api+json`;
  - limits: 100 req/min; 100 creates/min.
- **`find_existing`:** `GET /tracking_requests?filter[request_number]={booking}&filter[scac]={scac}&include=tracked_object`
  → the newest `data[]` item with `request_type == "booking_number"`.
- **`create`:** `POST /tracking_requests` with
  `{"data":{"type":"tracking_request","attributes":{"request_type":"booking_number","request_number":booking,"scac":scac}}}`
  → 201 `data.id` (status `pending`). A 422 means a bad or unsupported SCAC.
- **`fetch`:** `GET /tracking_requests/{id}`. By `status`:

  | `status` | Snapshot |
  |---|---|
  | `pending` / `awaiting_manifest` | not ready |
  | `failed` / `cancelled` | `failure = failed_reason` (e.g. `not_found`, `duplicate`, `data_unavailable`, `booking_cancelled`) |
  | `created` / `tracking_stopped` | ready; continue below |

  When ready, read `relationships.tracked_object.data.id`, then
  `GET /shipments/{sid}?include=containers`. Events:
  - each container's `attributes.pod_discharged_at` → EQUIPMENT DISC ACT;
  - `attributes.pod_arrived_at` → TRANSPORT ARRI ACT with `eq_refs=(number,)`;
  - shipment `pod_ata_at` → TRANSPORT ARRI ACT;
  - else `pod_eta_at` → TRANSPORT ARRI EST.

  Time zone: `pod_timezone`. `pod_unlocode`: `port_of_discharge_locode`.
- **Cost and coverage:** the free plan covers 10 active containers, and reading data needs a trial or paid
  plan. There is no Grimaldi coverage.

## B.10 Fixtures and tests (`tests/fixtures/aggregator/`, `tests/test_aggregator.py`)

Booking numbers and ids are synthetic.

**`shipsgo_detail_sailing.json`**: MSC booking, two boxes. Box 1 goes Genoa → Valencia (transshipment) → New
York. Box 2 sails on a later vessel. Timestamps are UTC; locations carry IANA zones.

`tests/fixtures/aggregator/shipsgo_detail_sailing.json`

```json
{
 "message":"SUCCESS",
 "shipment":{
  "id":501234,
  "reference":null,
  "booking_number":"EBKG12345678",
  "container_number":null,
  "container_count":2,
  "carrier":{"scac":"MSCU","name":"MSC"},
  "status":"SAILING",
  "route":{
   "port_of_loading":{
    "location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},
    "date_of_loading":"2026-10-20T08:00:00Z",
    "date_of_loading_initial":"2026-10-19T08:00:00Z"
   },
   "ts_count":1,
   "port_of_discharge":{
    "location":{"code":"USNYC","name":"New York","timezone":"America/New_York","country":{"code":"US","name":"United States"}},
    "date_of_discharge":"2026-11-12T18:00:00Z",
    "date_of_discharge_initial":"2026-11-10T18:00:00Z",
    "date_of_discharge_predicted":"2026-11-13T15:00:00Z"
   },
   "transit_time":24,
   "transit_percentage":60,
   "co2_emission":1.9
  },
  "containers":[
   {
    "number":"MEDU1234567",
    "status":"SAILING",
    "size":40,
    "type":"HC",
    "movements":[
     {"event":"EMSH","status":"ACT","location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},"timestamp":"2026-10-15T07:00:00Z","vessel":null,"voyage":null},
     {"event":"GTIN","status":"ACT","location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},"timestamp":"2026-10-17T12:30:00Z","vessel":null,"voyage":null},
     {"event":"LOAD","status":"ACT","location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},"timestamp":"2026-10-20T08:00:00Z","vessel":{"imo":9000011,"name":"MSC FEEDER"},"voyage":"FA642W"},
     {"event":"DEPA","status":"ACT","location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},"timestamp":"2026-10-20T20:00:00Z","vessel":{"imo":9000011,"name":"MSC FEEDER"},"voyage":"FA642W"},
     {"event":"ARRV","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-22T05:00:00Z","vessel":{"imo":9000011,"name":"MSC FEEDER"},"voyage":"FA642W"},
     {"event":"DISC","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-22T11:00:00Z","vessel":{"imo":9000011,"name":"MSC FEEDER"},"voyage":"FA642W"},
     {"event":"LOAD","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-26T09:00:00Z","vessel":{"imo":9000022,"name":"MSC MOTHER"},"voyage":"UX643A"},
     {"event":"DEPA","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-26T22:00:00Z","vessel":{"imo":9000022,"name":"MSC MOTHER"},"voyage":"UX643A"},
     {"event":"ARRV","status":"EST","location":{"code":"USNYC","name":"New York","timezone":"America/New_York","country":{"code":"US","name":"United States"}},"timestamp":"2026-11-12T12:30:00Z","vessel":{"imo":9000022,"name":"MSC MOTHER"},"voyage":"UX643A"},
     {"event":"DISC","status":"EST","location":{"code":"USNYC","name":"New York","timezone":"America/New_York","country":{"code":"US","name":"United States"}},"timestamp":"2026-11-12T18:00:00Z","vessel":{"imo":9000022,"name":"MSC MOTHER"},"voyage":"UX643A"}
    ]
   },
   {
    "number":"MEDU7654321",
    "status":"SAILING",
    "size":40,
    "type":"HC",
    "movements":[
     {"event":"LOAD","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-30T09:00:00Z","vessel":{"imo":9000033,"name":"MSC SECOND"},"voyage":"UX645A"},
     {"event":"ARRV","status":"EST","location":{"code":"USNYC","name":"New York","timezone":"America/New_York","country":{"code":"US","name":"United States"}},"timestamp":"2026-11-14T11:00:00Z","vessel":{"imo":9000033,"name":"MSC SECOND"},"voyage":"UX645A"}
    ]
   }
  ],
  "tokens":{"map":"00000000-0000-0000-0000-000000000000"},
  "followers":[],
  "tags":[],
  "creator":{"name":"API User","email":"ops@example.com"},
  "created_at":"2026-10-16 09:00:00",
  "updated_at":"2026-11-04 06:10:00",
  "checked_at":"2026-11-05 03:00:00",
  "discarded_at":null
 }
}
```

**`shipsgo_detail_discharged.json`**: box 1 arrived and was discharged. The discharge at `02:40Z` is 21:40 the previous day in New York.

`tests/fixtures/aggregator/shipsgo_detail_discharged.json`

```json
{
 "message":"SUCCESS",
 "shipment":{
  "id":501234,
  "reference":null,
  "booking_number":"EBKG12345678",
  "container_number":null,
  "container_count":1,
  "carrier":{"scac":"MSCU","name":"MSC"},
  "status":"DISCHARGED",
  "route":{
   "port_of_loading":{
    "location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},
    "date_of_loading":"2026-10-20T08:00:00Z",
    "date_of_loading_initial":"2026-10-19T08:00:00Z"
   },
   "ts_count":1,
   "port_of_discharge":{
    "location":{"code":"USNYC","name":"New York","timezone":"America/New_York","country":{"code":"US","name":"United States"}},
    "date_of_discharge":"2026-11-03T02:40:00Z",
    "date_of_discharge_initial":"2026-11-10T18:00:00Z",
    "date_of_discharge_predicted":null
   },
   "transit_time":24,
   "transit_percentage":60,
   "co2_emission":1.9
  },
  "containers":[
   {
    "number":"MEDU1234567",
    "status":"DISCHARGED",
    "size":40,
    "type":"HC",
    "movements":[
     {"event":"EMSH","status":"ACT","location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},"timestamp":"2026-10-15T07:00:00Z","vessel":null,"voyage":null},
     {"event":"GTIN","status":"ACT","location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},"timestamp":"2026-10-17T12:30:00Z","vessel":null,"voyage":null},
     {"event":"LOAD","status":"ACT","location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},"timestamp":"2026-10-20T08:00:00Z","vessel":{"imo":9000011,"name":"MSC FEEDER"},"voyage":"FA642W"},
     {"event":"DEPA","status":"ACT","location":{"code":"ITGOA","name":"Genoa","timezone":"Europe/Rome","country":{"code":"IT","name":"Italy"}},"timestamp":"2026-10-20T20:00:00Z","vessel":{"imo":9000011,"name":"MSC FEEDER"},"voyage":"FA642W"},
     {"event":"ARRV","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-22T05:00:00Z","vessel":{"imo":9000011,"name":"MSC FEEDER"},"voyage":"FA642W"},
     {"event":"DISC","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-22T11:00:00Z","vessel":{"imo":9000011,"name":"MSC FEEDER"},"voyage":"FA642W"},
     {"event":"LOAD","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-26T09:00:00Z","vessel":{"imo":9000022,"name":"MSC MOTHER"},"voyage":"UX643A"},
     {"event":"DEPA","status":"ACT","location":{"code":"ESVLC","name":"Valencia","timezone":"Europe/Madrid","country":{"code":"ES","name":"Spain"}},"timestamp":"2026-10-26T22:00:00Z","vessel":{"imo":9000022,"name":"MSC MOTHER"},"voyage":"UX643A"},
     {"event":"ARRV","status":"ACT","location":{"code":"USNYC","name":"New York","timezone":"America/New_York","country":{"code":"US","name":"United States"}},"timestamp":"2026-11-02T12:15:00Z","vessel":{"imo":9000022,"name":"MSC MOTHER"},"voyage":"UX643A"},
     {"event":"DISC","status":"ACT","location":{"code":"USNYC","name":"New York","timezone":"America/New_York","country":{"code":"US","name":"United States"}},"timestamp":"2026-11-03T02:40:00Z","vessel":{"imo":9000022,"name":"MSC MOTHER"},"voyage":"UX643A"}
    ]
   }
  ],
  "tokens":{"map":"00000000-0000-0000-0000-000000000000"},
  "followers":[],
  "tags":[],
  "creator":{"name":"API User","email":"ops@example.com"},
  "created_at":"2026-10-16 09:00:00",
  "updated_at":"2026-11-04 06:10:00",
  "checked_at":"2026-11-05 03:00:00",
  "discarded_at":null
 }
}
```

**`shipsgo_detail_inprogress.json`**, **`shipsgo_detail_untracked.json`**:

`tests/fixtures/aggregator/shipsgo_detail_inprogress.json`

```json
{
 "message":"SUCCESS",
 "shipment":{
  "id":501300,
  "reference":null,
  "booking_number":"EBKG87654321",
  "container_number":null,
  "container_count":0,
  "carrier":{"scac":"MSCU","name":"MSC"},
  "status":"INPROGRESS",
  "route":null,
  "containers":[],
  "tokens":{"map":"00000000-0000-0000-0000-000000000000"},
  "followers":[],
  "tags":[],
  "creator":{"name":"API User","email":"ops@example.com"},
  "created_at":"2026-11-05 09:00:00",
  "updated_at":"2026-11-05 09:00:00",
  "checked_at":null,
  "discarded_at":null
 }
}
```

`tests/fixtures/aggregator/shipsgo_detail_untracked.json`

```json
{
 "message":"SUCCESS",
 "shipment":{
  "id":501301,
  "reference":null,
  "booking_number":"NAM7654321",
  "container_number":null,
  "container_count":0,
  "carrier":{"scac":"CMDU","name":"CMA CGM"},
  "status":"UNTRACKED",
  "route":null,
  "containers":[],
  "tokens":{"map":"00000000-0000-0000-0000-000000000000"},
  "followers":[],
  "tags":[],
  "creator":{"name":"API User","email":"ops@example.com"},
  "created_at":"2026-11-05 09:00:00",
  "updated_at":"2026-11-05 09:00:00",
  "checked_at":"2026-11-05 09:05:00",
  "discarded_at":null
 }
}
```

**`shipsgo_list_found.json`**, **`shipsgo_list_empty.json`**, **`shipsgo_create_200.json`**, **`shipsgo_create_409.json`**, **`shipsgo_create_402.json`**:

`tests/fixtures/aggregator/shipsgo_list_found.json`

```json
{
 "message":"SUCCESS",
 "shipments":[
  {
   "id":501234,
   "reference":null,
   "booking_number":"EBKG12345678",
   "container_number":null,
   "container_count":2,
   "carrier":{"scac":"MSCU","name":"MSC"},
   "status":"SAILING",
   "created_at":"2026-10-16 09:00:00",
   "updated_at":"2026-11-04 06:10:00"
  }
 ],
 "meta":{"more":false,"total":1}
}
```

`tests/fixtures/aggregator/shipsgo_list_empty.json`

```json
{"message":"SUCCESS","shipments":[],"meta":{"more":false,"total":0}}
```

`tests/fixtures/aggregator/shipsgo_create_200.json`

```json
{
 "message":"SUCCESS",
 "shipment":{"id":501300,"reference":null,"booking_number":"EBKG87654321","container_number":null}
}
```

`tests/fixtures/aggregator/shipsgo_create_409.json`

```json
{
 "message":"ALREADY_EXISTS",
 "shipment":{"id":501234,"reference":null,"booking_number":"EBKG12345678","container_number":null}
}
```

`tests/fixtures/aggregator/shipsgo_create_402.json`

```json
{"message":"NOT_ENOUGH_CREDITS"}
```

Expected results (computed with the reference implementation):

| Case | Expected |
|---|---|
| Sailing, `view(MSC)` (policy `first`) | `eta == datetime(2026,11,12,7,30)` (12:30Z → New York, EST); `is_actual False`; `source == "ShipsGo TRANSPORT ARRI EST at USNYC"`; `containers == ("MEDU1234567","MEDU7654321")`; `details["tracking_id"] == "501234"` |
| Sailing payload via `view(CMA)` (policy `latest`) | `eta == datetime(2026,11,14,6,0)` (box 2) |
| Discharged | `eta == datetime(2026,11,2,21,40)`; `is_actual True`; `source == "ShipsGo EQUIPMENT DISC ACT at USNYC"` |
| Sailing with `containers` emptied | Route fallback: `eta == datetime(2026,11,12,13,0)` (18:00Z → local), EST, basis `ROUTE POD date_of_discharge` |
| In progress, `AGG_FIRST_LOOKUP_WAIT_S=0` | `AggregatorBookingError`, message contains "re-run later" |
| Untracked | `AggregatorBookingError`, message contains "could not track" |

Tests (respx router on `https://api.shipsgo.test/v2`; `sleep` injected as an async no-op; `FixedClock`):
1. `test_build_client_none_when_unconfigured`.
2. `test_known_state_entry_skips_lookup`: one detail GET, no list, no create.
3. `test_existing_found_by_list_no_create`: the list returns the shipment → no POST; the state is written
   with `created_by_us False`.
4. `test_create_when_absent`: empty list → POST body exactly `{"booking_number": "EBKG87654321", "carrier": "MSCU"}`
   with the token header → state `created_by_us True` → detail fetched.
5. `test_create_409_is_success_and_not_counted`.
6. `test_create_402_plain_lookup_error`: a `CarrierLookupError` that is **not** an `AggregatorBookingError`;
   the message mentions credits.
7. `test_allow_create_false`: not found → `AggregatorBookingError`, zero POSTs.
8. `test_create_cap`: with `AGG_MAX_CREATES_PER_RUN=1`, the second new booking → `AggregatorBookingError`;
   an already-tracked booking still succeeds.
9. `test_creates_are_serialised`: 3 concurrent new bookings. A respx side effect asserts that no two POSTs
   overlap (it counts in-flight requests).
10. `test_inprogress_then_ready`: details return in-progress, then sailing, with wait 30 and interval 10 →
    success after 2 GETs.
11. `test_inprogress_no_wait`: wait 0 → "re-run later".
12. `test_stale_entry_not_waited`: `first_seen_at` 2 days ago and status `INPROGRESS` → immediate error, one
    GET.
13. `test_untracked`.
14. `test_detail_404_forgets_and_retries_once`.
15. `test_expected_results[...]`: the table above, including the CMA policy and the route fallback.
16. `test_401_message_mentions_token_var`.
17. `test_probe_never_creates_and_redacts`: zero POSTs; `json.dumps(output)` does not contain the token;
    the explain lines are present.
18. `test_state_file_corrupt_is_tolerated` and `test_state_file_atomic_write` (no partial file after a
    simulated exception).
19. `test_prepare_counts_and_sequential`: mixed known, found and created items give the right counts, and
    exceptions are swallowed.
20. `test_view_aclose_noop`: closing a view does not close the shared client.

## B.11 Probe / verification (safe, real account)

1. `eta probe aggregator`: shows `account_ok` and the ACTIVE carriers. Confirm `MSCU`, `MAEU` and `CMDU`,
   and find Grimaldi's code. If it is not `GRIU`, set `AGG_SCAC_GRIMALDI`.
2. `eta probe aggregator --booking <a booking you already track in the ShipsGo dashboard>`. The carrier is
   derived from the booking pattern. Check the explained POD/ETA against the ShipsGo dashboard and the
   carrier's site. This verifies the time zone conversion.
3. `eta eta <one new booking>`. This registers it (1 credit) and may say "re-run later". Run it again after a
   few minutes; it should now return an ETA.
4. Only then set `ETA_PROVIDER_<X>=aggregator` for real runs.

---

# Part C — Grimaldi (`carriers/grimaldi.py`)

## C.1 Verdict

- **No public tracking API was found.**
  - Grimaldi (booking pattern `S3` + 8 digits) is **not** a DCSA member. The members are Maersk, MSC,
    CMA CGM, Hapag-Lloyd, ONE, Evergreen, HMM, Yang Ming and ZIM (plus later joiners such as PIL).
  - No DCSA or proprietary API documentation exists publicly.
  - Public tracking is the GNET eService web portal (`gnet.grimaldi-eservice.com`). Automating it would mean
    browser automation or scraping, which this program rules out.
- **Aggregators:**
  - Terminal49 states it "does not yet integrate Grimaldi Lines's feed".
  - Vizion's booking beta does not list it.
  - ShipsGo, GoComet, project44 and Visiwise advertise Grimaldi container tracking, but **booking-number**
    support via API is UNVERIFIED.
- **SCAC:** `GRIU` according to SeaRates, Beacon and Safecube. It is UNVERIFIED for ShipsGo.

So the default stays **parity**: Grimaldi rows fail with the original message. If the ShipsGo probe shows
Grimaldi as ACTIVE, set `ETA_PROVIDER_GRIMALDI=aggregator`, and confirm one booking with §B.11 steps 2–3
before relying on it. You can also ask Grimaldi's eService team whether they offer EDI (IFTSTA) or an API to
customers.

## C.2 Module design

```python
GRIMALDI_NOT_IMPLEMENTED = "Grimaldi ETA lookup not yet implemented for booking {booking}"   # exact original text

class GrimaldiClient:                    # implements CarrierClient
    carrier = Carrier.GRIMALDI
    async def get_eta(self, booking: str) -> EtaResult:
        raise UnsupportedCarrierError(GRIMALDI_NOT_IMPLEMENTED.format(booking=booking))
    async def probe(self, booking: str) -> dict[str, Any]:
        return {"requests": [], "raw": None, "eta": GRIMALDI_NOT_IMPLEMENTED.format(booking=booking),
                "note": "No Grimaldi API; see 09 Part C. Use ETA_PROVIDER_GRIMALDI=aggregator if your aggregator covers it."}
    async def aclose(self) -> None: return None

def build_client(settings: Settings, token_store: TokenStore) -> GrimaldiClient:
    return GrimaldiClient()              # always "configured" — it needs nothing
```

## C.3 Tests (`tests/test_grimaldi.py`)

1. `get_eta("S312345678")` raises `UnsupportedCarrierError` whose `str` is exactly
   `Grimaldi ETA lookup not yet implemented for booking S312345678`.
2. `probe()` returns the same text and makes no HTTP call.

---

# Part D — Provider selection (`carriers/router.py`)

01 §4 owns the booking-number patterns and their messages. This part owns what happens **after** a carrier
is identified.

## D.1 Configuration

`RunSettings.providers: Mapping[Carrier, Literal["native", "aggregator", "off"]]` (02 §1), read from:

| Env var | Default | Values |
|---|---|---|
| `ETA_PROVIDER_MSC` | `native` | `native` \| `aggregator` \| `off` (case-insensitive, stripped) |
| `ETA_PROVIDER_MAERSK` | `native` | same |
| `ETA_PROVIDER_CMA` | `native` | same |
| `ETA_PROVIDER_GRIMALDI` | `off` | same (`native` and `off` both mean the parity stub) |

Any other value raises `ConfigError(f"ETA_PROVIDER_{X} must be one of native, aggregator, off (got {value!r})")`
in `load_settings()`.

## D.2 `CarrierRouter`

```python
class CarrierRouter:
    def __init__(self, *, providers: Mapping[Carrier, str],
                 native: Mapping[Carrier, CarrierClient | None],      # from each carrier module's build_client()
                 native_missing: Mapping[Carrier, tuple[str, ...]],   # env var names for the "not configured" message (Settings.missing)
                 aggregator: AggregatorClient | None,
                 failure_limit: int): ...
    def route(self, base: str) -> tuple[Carrier, str, CarrierClient]: ...
    async def prepare(self, bases: Sequence[str]) -> None: ...
    async def aclose(self) -> None: ...
```

`route(base)`:
1. Apply the 01 §4 pattern table to get `(carrier, token)`. This includes its `UnsupportedCarrierError`s
   (reference number, no pattern) and the D6 extra-text `CarrierLookupError`.
2. `provider = providers[carrier]`; the message suffix `X = carrier.name` (`MSC`, `MAERSK`, `CMA`, `GRIMALDI`).
3. `carrier is Carrier.GRIMALDI and provider in ("native", "off")` →
   `raise UnsupportedCarrierError(GRIMALDI_NOT_IMPLEMENTED.format(booking=token))`. This is the exact original
   text, and parity is why Grimaldi's `off` does not use the generic message.
4. `provider == "off"` → `raise CarrierLookupError(f"{carrier} lookups are disabled (ETA_PROVIDER_{X}=off)")`.
5. `provider == "aggregator"`:
   - if `aggregator is None`, raise
     `CarrierLookupError(f"{carrier} is set to use the aggregator (ETA_PROVIDER_{X}=aggregator) but the aggregator is not configured — set AGG_API_TOKEN in .env")`;
   - otherwise return `(carrier, token, self._breaker(aggregator.view(carrier)))`.
6. `native` → `client = native[carrier]`:
   - if it is `None`, raise
     `CarrierLookupError(f"{carrier} lookups are not configured — set {', '.join(native_missing[carrier])} in .env")`
     (03 §9);
   - otherwise return `(carrier, token, self._breaker(client))`.

Route-time errors are raised **before** any client is called, so the circuit breaker never sees them.

Circuit breaker (10 §6) additions:
- Keep one breaker per `(provider, carrier)`.
- **Do not count** `UnsupportedCarrierError`, `AggregatorBookingError`, `NoEtaError` or `CustomsHoldError`.
- Count only other `CarrierLookupError`s.

`prepare(bases)`:
- For each base booking, try `route()`. Ignore errors; the per-row task reports them later.
- Collect `(carrier, token)` pairs that resolved to the aggregator.
- If there are any, `await aggregator.prepare(pairs)`.
- No-op without an aggregator.
- **Pipeline hook (10 Phase 2):** call it once, before the per-row tasks start, with the base booking of
  every row that will need an ETA. Those are the STANDARD and NOTIF1_SENT rows at or above the amount
  threshold.

`aclose()`: close every native client once, and the aggregator once. Views are no-ops.

## D.3 Tests (`tests/test_router.py`, in addition to 12-testing's pattern tests)

1. `off` → the exact disabled message; no client is called.
2. `aggregator` with no aggregator → the message names `AGG_API_TOKEN`.
3. `aggregator` → the returned client is a view with the right `carrier`. `get_eta` forwards the carrier.
4. Grimaldi:
   - default (`off`) → `UnsupportedCarrierError` with the exact original text;
   - `native` → same;
   - `aggregator` → a view.
5. Native not configured → the message lists the missing env vars.
6. An invalid `ETA_PROVIDER_CMA=shipsgo` → `ConfigError` at load.
7. `prepare()` forwards only aggregator-routed pairs, de-duplicated, and survives route errors.
8. Breaker: 6 consecutive `AggregatorBookingError`s do not trip it; 5 consecutive plain
   `CarrierLookupError`s from the aggregator view do.

---

# Sources & verification status

| Claim | Source | Status |
|---|---|---|
| DCSA T&T 2.2: `/v2/events` returns a bare array; params; pagination headers; contradictory filters → `[]` | https://api.swaggerhub.com/apis/dcsaorg/DCSA_TNT/2.2.0 (published 2021-10-28) | VERIFIED-DOCS |
| 2.2 event schemas, enums (classifier, ARRI/DEPA, equipment and shipment codes, modeOfTransport, facilityTypeCode, emptyIndicator, documentReferences) | https://api.swaggerhub.com/domains/dcsaorg/EVENT_DOMAIN/1.0.4, …/DCSA_DOMAIN/1.0.3, …/LOCATION_DOMAIN/1.0.3, …/DOCUMENTATION_DOMAIN/1.0.4 | VERIFIED-DOCS |
| `eventDateTime` is local time in ISO 8601 with offset | EVENT_DOMAIN 1.0.4 / 3.2.0 descriptions | VERIFIED-DOCS |
| T&T 3.0.0 published 2026-09-30; members and DCSA+ first, public H2 2027; IoT and reefer; better corrections | https://cyprusshippingnews.com/2026/10/06/dcsa-publishes-track-trace-3-0-0-expanding-standard-to-cover-iot-and-reefer-visibility/ ; https://www.ajot.com/news/dcsa-publishes-track-trace-3.0.0-expanding-standard-to-cover-iot-and-reefer-visibility ; https://blogs.tradlinx.com/when-container-timestamps-conflict-what-dcsa-track-trace-3-0-changes/ | VERIFIED (trade press) |
| v3 envelope, `retractedEventID`, `transportCallReference`, `location.locationType`, `isTransshipmentMove`, `references[type=EQ]`, new codes | https://api.swaggerhub.com/domains/dcsaorg/EVENT_DOMAIN/3.2.0 (and 3.0.0); https://api.swaggerhub.com/domains/dcsaorg/LOCATION_DOMAIN/3.0.0 | INFERRED for T&T 3.0 (domain components) |
| Maersk wraps events in `{"events": [...]}` | 07 §4.2; Maersk SDK models `Events`/`EventsEventsInner` (https://github.com/buyco/go-maersk-sdk) | OBSERVED / INFERRED |
| CMA bare array, `carrierSpecificData`, sentinel dates | 08 §4.5, §7.2 | VERIFIED-DOCS (per 08) |
| MSC dialect: camelCase `unLocationCode`, `eventId`, ACT/EST only, 7-digit fractions, `description` | 06 §4 and §9 (MSC's OpenAPI export from its developer portal) | VERIFIED-DOCS (per 06) |
| Python `datetime.fromisoformat` accepts Z, `+0000`, 7-digit fractions (3.13), basic format | Tested locally (Python 3.13) | OBSERVED |
| ShipsGo v2: base URL, `X-Shipsgo-User-Token`, endpoints, filters, 409 ALREADY_EXISTS with no cost, 402, serial creates (429), 100 req/min, statuses, movement codes, `date_of_discharge*` | https://api.shipsgo.com/docs/v2/specs/openapi.json (ShipsGo API 2.0) | VERIFIED-DOCS |
| ShipsGo movement `timestamp` offset is local vs UTC | — | UNVERIFIED (handled by conversion) |
| ShipsGo pricing: 1 credit per new shipment; GETs free; API usage fee tiers; 3 free sign-up credits | https://help.shipsgo.com/article/100-api-usage-pricing ; https://help.shipsgo.com/article/28-pricing-credit-usage ; https://shipsgo.com/pricing | VERIFIED-DOCS (fee page) / UNVERIFIED (credit details from search summaries) |
| Terminal49: tracking requests, statuses, failed reasons, `Token` auth, 100 req/min, free plan 10 containers (reads need trial/paid), POD fields, no Grimaldi | https://terminal49.com/docs/llms.txt and the linked .md pages (create/list/get tracking request, get shipment, lifecycle, rate limiting, ocean carriers, SeaRates migration); https://terminal49.com/shipping-lines/grimaldi-lines-container-tracking | VERIFIED-DOCS |
| Vizion booking tracking is beta (CMA, Evergreen, Maersk, MSC, ONE); `X-API-Key` | https://docs.vizionapi.com/docs/track-by-booking ; https://support.vizionapi.com/track-by-booking-number-beta | VERIFIED-DOCS |
| SeaRates API shape (`tracking.searates.com/tracking`, `type=BK`) | Terminal49's SeaRates migration page | VERIFIED-DOCS (third party); current availability UNVERIFIED (searates.com → dpworld.com redirect observed 2026-11-05) |
| Grimaldi not a DCSA member; no public API; GNET portal; SCAC GRIU | https://dcsa.org/about-us/faq ; Beacon / SeaRates / Safecube Grimaldi pages; Terminal49 Grimaldi page | INFERRED (no official Grimaldi statement found) |
| `tzdata` needed for `zoneinfo` on Windows | Python docs (`zoneinfo`) | VERIFIED-DOCS |
