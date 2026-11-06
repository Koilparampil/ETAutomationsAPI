"""Optional third-party tracking provider: ShipsGo API v2 (09 Part B).

One API token serves every carrier while official API access is pending, and it is the only realistic
route for Grimaldi (09 §B.1, Part C). ShipsGo is **asynchronous**: a booking is registered once
(`POST /ocean/shipments`, 1 credit; a duplicate answers 409 at no charge), then the provider polls the
carrier, and later `GET /ocean/shipments/{id}` calls (free) return the data. Registered bookings are
remembered in `<ETA_STATE_DIR>/aggregator_tracking.json` (§B.7.3), so each booking costs about one credit
over its whole life.

Layers:
- `ShipsGoBackend` — HTTP (§B.5, §B.7.2) and the ShipsGo payload → `AggregatorSnapshot` mapping (§B.7.4).
  ShipsGo movements are adapted to `DcsaEvent`s, so the POD/ETA choice is the shared DCSA logic of
  `dcsa.py` (09 Part A); their UTC timestamps are converted to the port's time zone.
- `TrackingState` — the tracking-id file (atomic writes, corrupt file tolerated).
- `AggregatorClient` — `prepare()` (serial registration before the per-row tasks), `get_eta()`
  (§B.7.5), `probe()` (never creates, never writes) and `view(carrier)`.
- `AggregatorCarrierView` — the `CarrierClient` (03 §9) the router hands out for one carrier.

Boundary rule (03 §5): `get_eta()` raises only `CarrierLookupError` (incl. `AggregatorBookingError`, which
the circuit breaker must not count) or `NoEtaError`. The static API token is a header: there is no token
provider and no tokens.json entry (§B.6); it never appears in logs or probe output.

DEVIATION (MSC via aggregator, 09 §B.7.6): MSC's customs-hold heuristic and its "Full Transshipment Loaded
+ 7 days" fallback (06) are MSC-GraphQL rules and are NOT applied when MSC is routed here; a booking without
a POD date raises NoEtaError.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import logging
import os
import re
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import quote

import httpx

from ..clock import Clock, SystemClock
from ..errors import (
    AggregatorBookingError,
    AuthError,
    CarrierLookupError,
    ConfigError,
    NoEtaError,
    ServiceError,
)
from ..http import AsyncLimiter, json_body, new_async_client, request, request_id_from
from ..logging_setup import MASK, redact
from ..models import Carrier, EtaResult
from . import dcsa
from .base import error_text
from .dcsa import DcsaArrival, DcsaEvent, Policy

if TYPE_CHECKING:
    from ..config import AggregatorSettings, Settings
    from ..token_store import TokenStore

__all__ = [
    "AGG_POLICY",
    "NOT_TRACKED_NOTE",
    "ROUTE_POD_BASIS",
    "SHIPSGO_MOVEMENTS",
    "AggregatorBackend",
    "AggregatorBookingError",
    "AggregatorCarrierView",
    "AggregatorClient",
    "AggregatorSnapshot",
    "ShipsGoBackend",
    "TrackingState",
    "build_client",
    "snapshot_from_shipsgo",
]

log = logging.getLogger("eta.carrier.aggregator")

SERVICE = "aggregator"                       # ServiceError.service / http.request(service=...) (09 §B.7.2)
TOKEN_HEADER = "X-Shipsgo-User-Token"        # 09 §B.5
STATE_VERSION = 1                            # aggregator_tracking.json "version" (09 §B.7.3)

# 09 §B.7.1 — multi-container policy per carrier, parity with 06/07/08.
AGG_POLICY: Mapping[Carrier, Policy] = MappingProxyType({
    Carrier.MSC: "first", Carrier.MAERSK: "first", Carrier.CMA: "latest", Carrier.GRIMALDI: "latest",
})

# 09 §B.7.4 — provider status → readiness.
READY_STATUSES = frozenset({"BOOKED", "LOADED", "SAILING", "ARRIVED", "DISCHARGED"})
PENDING_STATUSES = frozenset({"NEW", "INPROGRESS"})
FAILED_STATUSES = frozenset({"UNTRACKED"})
NOT_ASSIGNED = "NOT_ASSIGNED"                # container.number placeholder (09 §B.5)
ROUTE_POD_BASIS = "ROUTE POD date_of_discharge"

# 09 §B.7.4 table: ShipsGo movement event → (DCSA event_type, type_code, empty_indicator).
SHIPSGO_MOVEMENTS: Mapping[str, tuple[str, str, str | None]] = MappingProxyType({
    "EMSH": ("EQUIPMENT", "GTOT", "EMPTY"),   # empty container to shipper
    "GTIN": ("EQUIPMENT", "GTIN", "LADEN"),
    "LOAD": ("EQUIPMENT", "LOAD", "LADEN"),
    "DEPA": ("TRANSPORT", "DEPA", None),
    "ARRV": ("TRANSPORT", "ARRI", None),
    "DISC": ("EQUIPMENT", "DISC", "LADEN"),
    "GTOT": ("EQUIPMENT", "GTOT", "LADEN"),
    "EMRT": ("EQUIPMENT", "GTIN", "EMPTY"),   # empty container returned
})
MOVEMENT_CLASSIFIERS = frozenset({"EST", "ACT"})

# 09 §B.5: ShipsGo's booking_number pattern. Checked before any request (no call, no credit for a number
# the provider would reject with 422 anyway).
BOOKING_NUMBER_RE = re.compile(r"[A-Za-z0-9/-]+")

NOT_TRACKED_NOTE = "not tracked yet — eta run / eta eta would register it (1 credit)"   # 09 §B.7.5 probe

LIST_TAKE = 25                     # GET /ocean/shipments?...&take=25 (09 §B.5)
CARRIERS_TAKE = 100                # GET /ocean/carriers?...&take=100&skip=n (09 §B.5)
MAX_CARRIER_PAGES = 50             # safety stop for the probe-only carriers listing
MIN_POLL_INTERVAL_S = 1.0          # defensive floor: AGG_POLL_INTERVAL_S=0 must not busy-poll the provider
PREPARE_SYSTEMIC_LIMIT = 3         # prepare(): stop the warm-up after this many consecutive provider failures

# Statuses the backend maps to its own messages (09 §B.7.2); never retried by http.request() anyway.
_MAPPED_STATUSES = (401, 402, 403, 422)
# A create that ended like this may still have been processed (and charged) by the provider.
_AMBIGUOUS_CREATE_STATUSES = (None, 500, 502, 504)

# _ensure_tracking() outcomes
_KNOWN, _FOUND, _CREATED, _EXISTING = "known", "found", "created", "existing"
_CREATE_OUTCOMES = (_CREATED, _EXISTING)

# Request summaries for probe() output; set only inside probe(), so concurrent lookups are not traced.
_TRACE: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "eta_aggregator_trace", default=None
)

# 01 §4 booking patterns (orders 1–4), used ONLY when probe() gets a booking without a carrier. The router
# (01 §4) stays the authority for real lookups.
_PROBE_PATTERNS: tuple[tuple[Carrier, re.Pattern[str], bool], ...] = (
    (Carrier.MSC, re.compile(r"EBKG\d{8}"), False),
    (Carrier.MAERSK, re.compile(r"2\d{8}"), True),
    (Carrier.CMA, re.compile(r"NAM\d{7}"), False),
    (Carrier.GRIMALDI, re.compile(r"S3\d{8}"), False),
)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 09 §B.7.1 — snapshot and backend protocol
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class AggregatorSnapshot:
    """What a provider currently knows about one tracked booking (09 §B.7.1)."""

    tracking_id: str
    status: str                       # provider status, upper-case
    ready: bool                       # provider has carrier data
    failure: str | None               # permanent per-booking failure (e.g. "UNTRACKED"), else None
    pod_unlocode: str | None
    events: tuple[DcsaEvent, ...]     # provider movements adapted to DCSA (§B.7.4)
    route_pod_arrival: DcsaArrival | None
    containers: tuple[str, ...]
    raw: Mapping[str, Any]            # body as received (redact before logging/probe output)


@runtime_checkable
class AggregatorBackend(Protocol):
    """One tracking provider (09 §B.7.1). Swapping providers means another backend (§B.9)."""

    name: str                         # "ShipsGo" (messages and EtaResult.source)
    key: str                          # "shipsgo" (state-file namespace)

    async def find_existing(self, scac: str, booking: str) -> str | None: ...

    async def create(self, scac: str, booking: str) -> str:
        """Returns the tracking id; "already exists" counts as success. A backend may expose
        `last_create_was_new: bool` (False for a free duplicate); without it every create counts against
        AGG_MAX_CREATES_PER_RUN."""
        ...

    async def fetch(self, tracking_id: str) -> AggregatorSnapshot | None:
        """None ⇔ the provider no longer knows the id (HTTP 404)."""
        ...

    async def list_carriers(self) -> list[dict[str, Any]]: ...

    async def aclose(self) -> None: ...


# ════════════════════════════════════════════════════════════════════════════════════════════════
# small helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _text(value: Any) -> str | None:
    """Stripped non-empty text of a str/number, else None (booleans and containers are not text)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        text = str(int(value)) if value.is_integer() else str(value)
    elif isinstance(value, (int, str)):
        text = str(value)
    else:
        return None
    text = text.strip()
    return text or None


def _upper(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text else None


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _keys(body: Any) -> str:
    """Top-level keys (or the type) of an unexpected payload, for error messages (03 §13)."""
    if isinstance(body, Mapping):
        return str(sorted(str(k) for k in body)[:10])
    return type(body).__name__


def _id_sort_key(tid: str) -> tuple[int, int, str]:
    """Numeric ids compare numerically ("the highest id", 09 §B.7.5); other ids sort below them."""
    return (1, int(tid), tid) if tid.isdigit() else (0, 0, tid)


def _plausible(dt: datetime | None) -> bool:
    return dt is not None and dt.year in dcsa.PLAUSIBLE_YEARS


def _container_number(value: Any) -> str | None:
    number = _upper(value)
    return None if number == NOT_ASSIGNED else number


def _parse_local_iso(value: Any) -> datetime | None:
    """A naive local ISO time from the state file (offsets, if any, are dropped)."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _scrub(value: Any, secret: str) -> Any:
    """Replace every occurrence of `secret` in strings (recursively) — belt and braces for probe output."""
    if not secret:
        return value
    if isinstance(value, str):
        return value.replace(secret, MASK)
    if isinstance(value, Mapping):
        return {_scrub(k, secret): _scrub(v, secret) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub(v, secret) for v in value]
    return value


def _carrier_from_booking(booking: str) -> tuple[Carrier, str] | None:
    """01 §4 orders 1–4 (probe convenience only): (carrier, query token) or None."""
    for carrier, pattern, full in _PROBE_PATTERNS:
        match = pattern.fullmatch(booking) if full else pattern.search(booking)
        if match:
            return carrier, match.group(0)
    return None


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 09 §B.7.4 — ShipsGo payload → snapshot
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _adapt_movement(movement: Any, number: str | None, index: int) -> DcsaEvent | None:
    """One ShipsGo container movement → DcsaEvent (09 §B.7.4 table), or None (skipped, DEBUG)."""
    if not isinstance(movement, Mapping):
        log.debug("[ShipsGo] skipped movement #%d: not an object", index)
        return None
    code = _upper(movement.get("event"))
    mapped = SHIPSGO_MOVEMENTS.get(code or "")
    if mapped is None:
        log.debug("[ShipsGo] skipped movement #%d: unknown event %r", index, movement.get("event"))
        return None
    classifier = _upper(movement.get("status"))
    if classifier not in MOVEMENT_CLASSIFIERS:
        log.debug("[ShipsGo] skipped movement #%d: status %r is not EST/ACT", index, movement.get("status"))
        return None
    location = _mapping(movement.get("location"))
    # The timestamp's offset (UTC or local) is UNVERIFIED (09 §B.5): always convert to the location's
    # time zone; to_local_naive() is idempotent when the offset already is the local one.
    when = dcsa.to_local_naive(movement.get("timestamp"), _text(location.get("timezone")))
    if when is None or not _plausible(when):
        log.debug("[ShipsGo] skipped movement #%d: unusable timestamp %r", index, movement.get("timestamp"))
        return None
    event_type, type_code, empty_indicator = mapped
    vessel = movement.get("vessel")
    vessel_info = vessel if isinstance(vessel, Mapping) else None
    is_equipment = event_type == "EQUIPMENT"
    return DcsaEvent(
        event_type=event_type,
        classifier=classifier,
        type_code=type_code,
        event_datetime=when,
        created_datetime=None,
        unlocode=_upper(location.get("code")),
        facility_code=None,
        facility_type=None,
        mode_of_transport="VESSEL" if vessel_info is not None else None,
        equipment_reference=number if is_equipment else None,
        vessel_name=_text(vessel_info.get("name")) if vessel_info is not None else None,
        transport_call_sequence=None,
        raw=movement,
        vessel_imo=_text(vessel_info.get("imo")) if vessel_info is not None else None,
        voyage=_text(movement.get("voyage")),
        empty_indicator=empty_indicator,
        # ShipsGo movements are per container, so a TRANSPORT event belongs to this box only.
        eq_refs=(number,) if (not is_equipment and number) else (),
        index=index,
    )


def snapshot_from_shipsgo(body: Any, *, tracking_id: str | None = None) -> AggregatorSnapshot:
    """`GET /ocean/shipments/{id}` body → AggregatorSnapshot (09 §B.7.4).

    Raises ValueError when there is no `shipment` object; everything inside it is read tolerantly
    (missing/null parts count as absent).
    """
    shipment = body.get("shipment") if isinstance(body, Mapping) else None
    if not isinstance(shipment, Mapping):
        raise ValueError(f"no 'shipment' object in the response (top-level keys: {_keys(body)})")
    status = _upper(shipment.get("status")) or ""
    containers_raw = shipment.get("containers")
    route = _mapping(shipment.get("route"))

    failure: str | None = None
    if status in READY_STATUSES:
        ready = True
    elif status in PENDING_STATUSES:
        ready = False
    elif status in FAILED_STATUSES:
        ready, failure = False, status
    else:  # forward compatibility: an unknown status is ready when the provider shows data
        ready = bool(containers_raw or shipment.get("route"))

    pod_info = _mapping(route.get("port_of_discharge"))
    pod_location = _mapping(pod_info.get("location"))
    pod = _upper(pod_location.get("code"))
    pod_tz = _text(pod_location.get("timezone"))

    route_arrival: DcsaArrival | None = None
    discharge = pod_info.get("date_of_discharge")
    if _text(discharge):
        eta = dcsa.to_local_naive(discharge, pod_tz)
        if eta is not None and _plausible(eta):
            route_arrival = DcsaArrival(
                eta=eta,
                classifier="ACT" if status == "DISCHARGED" else "EST",
                unlocode=pod,
                facility_code=None,
                equipment_reference=None,
                basis=ROUTE_POD_BASIS,
            )
        else:
            log.debug("[ShipsGo] ignored unusable route date_of_discharge %r", discharge)
    # date_of_discharge_predicted (ShipsGo's own ML estimate) is deliberately NOT used: carrier data only.

    events: list[DcsaEvent] = []
    numbers: dict[str, None] = {}
    index = 0
    for container in containers_raw if isinstance(containers_raw, list) else ():
        if not isinstance(container, Mapping):
            continue
        number = _container_number(container.get("number"))
        if number:
            numbers.setdefault(number, None)
        movements = container.get("movements")
        for movement in movements if isinstance(movements, list) else ():
            event = _adapt_movement(movement, number, index)
            index += 1
            if event is not None:
                events.append(event)

    return AggregatorSnapshot(
        tracking_id=_text(shipment.get("id")) or (tracking_id or ""),
        status=status,
        ready=ready,
        failure=failure,
        pod_unlocode=pod,
        events=tuple(events),
        route_pod_arrival=route_arrival,
        containers=tuple(numbers),
        raw=body,
    )


def _pick_existing(body: Any, scac: str, booking: str) -> str | None:
    """`GET /ocean/shipments?filters…` body → the exact match's id (highest id if several), or None."""
    shipments = body.get("shipments") if isinstance(body, Mapping) else None
    if not isinstance(shipments, list):
        raise ValueError(f"no 'shipments' list in the response (top-level keys: {_keys(body)})")
    want_booking, want_scac = booking.strip().upper(), scac.strip().upper()
    matches: list[str] = []
    for item in shipments:
        if not isinstance(item, Mapping):
            continue
        if _upper(item.get("booking_number")) != want_booking:
            continue
        if _upper(_mapping(item.get("carrier")).get("scac")) != want_scac:
            continue
        tid = _text(item.get("id"))
        if tid:
            matches.append(tid)
    if not matches:
        return None
    return max(matches, key=_id_sort_key)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 09 §B.7.2 — ShipsGo HTTP backend
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _response_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 - undecodable body: best-effort excerpt only
        return ""


def _error_message(resp: httpx.Response) -> str:
    """The provider's `message` for a rejected request, else a short redacted body excerpt."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, Mapping):
        message = _text(body.get("message"))
        if message:
            return redact(message)
    excerpt = redact(_response_text(resp)).strip()[:200]
    return excerpt or "no details"


class ShipsGoBackend:
    """ShipsGo API v2 (09 §B.5) — implements AggregatorBackend.

    Every request sends `X-Shipsgo-User-Token` and `Accept: application/json` (also with an injected
    client), goes through http.request(service="aggregator") with an AsyncLimiter(rate_per_minute, 60),
    and holds the backend's semaphore only around the HTTP call (03 §10).
    """

    name = "ShipsGo"
    key = "shipsgo"

    def __init__(self, settings: AggregatorSettings, *, http: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._base_url = settings.base_url.rstrip("/")
        self._headers = {TOKEN_HEADER: settings.api_token, "Accept": "application/json"}
        self._owns_http = http is None
        self._http = http if http is not None else new_async_client(
            base_url=self._base_url, timeout=settings.timeout_s, headers={TOKEN_HEADER: settings.api_token})
        self._sem = asyncio.Semaphore(max(1, int(settings.max_concurrency)))
        self._limiter = AsyncLimiter(max(1, int(settings.rate_per_minute)), 60.0)
        # Set by create(): True for 200 (new shipment, 1 credit), False for 409 (existing, no charge).
        # Read by AggregatorClient right after create() while it holds its create lock.
        self.last_create_was_new: bool | None = None

    @property
    def http(self) -> httpx.AsyncClient:
        return self._http

    def __repr__(self) -> str:
        return f"ShipsGoBackend(base_url={self._base_url!r})"

    # ── AggregatorBackend ──
    async def find_existing(self, scac: str, booking: str) -> str | None:
        """GET /ocean/shipments filtered server-side by booking_number and carrier (free)."""
        params = {
            "filters[booking_number]": f"eq:{booking}",
            "filters[carrier]": f"eq:{scac}",
            "take": str(LIST_TAKE),
        }
        resp = await self._call("GET", "/ocean/shipments", params=params, booking=booking, scac=scac)
        return _pick_existing(json_body(resp, service=SERVICE), scac, booking)

    async def create(self, scac: str, booking: str) -> str:
        """POST /ocean/shipments (1 credit; 409 ALREADY_EXISTS = existing shipment, no charge).

        Not idempotent: a read timeout is NOT retried (no double charge); the next run's find_existing()
        picks the shipment up. `reference` is never sent: it takes part in duplicate detection (09 §B.5).
        """
        self.last_create_was_new = None
        resp = await self._call("POST", "/ocean/shipments", expected=(200, 409),
                                payload={"booking_number": booking, "carrier": scac}, booking=booking, scac=scac)
        body = json_body(resp, service=SERVICE)
        tid = _text(_mapping(_mapping(body).get("shipment")).get("id"))
        if not tid:
            raise ValueError(f"the create answer (HTTP {resp.status_code}) has no shipment id "
                             f"(top-level keys: {_keys(body)})")
        self.last_create_was_new = resp.status_code == 200
        return tid

    async def fetch(self, tracking_id: str) -> AggregatorSnapshot | None:
        """GET /ocean/shipments/{id} (free); 404 → None (deleted provider-side)."""
        path = f"/ocean/shipments/{quote(str(tracking_id), safe='')}"
        resp = await self._call("GET", path, expected=(200, 404))
        if resp.status_code == 404:
            return None
        return snapshot_from_shipsgo(json_body(resp, service=SERVICE), tracking_id=str(tracking_id))

    async def list_carriers(self) -> list[dict[str, Any]]:
        """ACTIVE carriers (probe only), paged with take/skip until meta.more is false."""
        carriers: list[dict[str, Any]] = []
        skip = 0
        for _page in range(MAX_CARRIER_PAGES):
            params = {"filters[status]": "eq:ACTIVE", "take": str(CARRIERS_TAKE), "skip": str(skip)}
            body = json_body(await self._call("GET", "/ocean/carriers", params=params), service=SERVICE)
            items = body.get("carriers") if isinstance(body, Mapping) else None
            if not isinstance(items, list):
                raise ValueError(f"no 'carriers' list in the response (top-level keys: {_keys(body)})")
            carriers.extend(dict(item) for item in items if isinstance(item, Mapping))
            if _mapping(_mapping(body).get("meta")).get("more") is not True or not items:
                break
            skip += len(items)
        else:
            log.warning("[ShipsGo] stopped listing carriers after %d pages", MAX_CARRIER_PAGES)
        return carriers

    async def aclose(self) -> None:
        """Close the HTTP client only if this backend created it."""
        if self._owns_http and not self._http.is_closed:
            await self._http.aclose()

    # ── internals ──
    async def _call(self, method: str, path: str, *, expected: tuple[int, ...] = (200,),
                    params: Mapping[str, str] | None = None, payload: Any = None,
                    booking: str | None = None, scac: str | None = None) -> httpx.Response:
        """One request with the 09 §B.7.2 error mapping (401/402/403/422 → CarrierLookupError)."""
        kwargs: dict[str, Any] = {"headers": self._headers}
        if params is not None:
            kwargs["params"] = dict(params)
        if payload is not None:
            kwargs["json"] = payload  # httpx sends Content-Type: application/json
        trace = _TRACE.get()
        started = time.perf_counter()
        try:
            async with self._sem:  # held only around the HTTP call, never across poll sleeps
                resp = await request(self._http, method, f"{self._base_url}{path}", service=SERVICE,
                                     expected=(*expected, *_MAPPED_STATUSES), limiter=self._limiter, **kwargs)
        except ServiceError as exc:
            if trace is not None:
                trace.append(self._summary(method, path, params, exc.status, started, error=str(exc)))
            raise
        if trace is not None:
            trace.append(self._summary(method, path, params, resp.status_code, started))
        if resp.status_code in _MAPPED_STATUSES and resp.status_code not in expected:
            raise self._mapped_error(resp, method=method, path=path, booking=booking, scac=scac)
        return resp

    @staticmethod
    def _summary(method: str, path: str, params: Mapping[str, str] | None, status: int | None,
                 started: float, *, error: str | None = None) -> dict[str, Any]:
        return {
            "method": method,
            "path": path,
            "params": dict(params) if params else None,
            "status": status,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "error": error,
        }

    @staticmethod
    def _mapped_error(resp: httpx.Response, *, method: str, path: str, booking: str | None,
                      scac: str | None) -> Exception:
        """09 §B.7.2 error table. 402 is systemic, so it stays a plain CarrierLookupError (the breaker
        counts it)."""
        status = resp.status_code
        if status == 401:
            return CarrierLookupError("ShipsGo rejected the API token (HTTP 401) — check AGG_API_TOKEN")
        if status == 403:
            return CarrierLookupError(
                "ShipsGo refused the operation (HTTP 403) — the token's user role may not create or read shipments")
        if status == 402:
            return CarrierLookupError(
                "ShipsGo: not enough credits to start tracking a new booking (HTTP 402) — buy credits or set "
                "AGG_ALLOW_CREATE=false")
        if booking is not None and scac is not None:  # 422 on a booking-specific call
            return CarrierLookupError(
                f"ShipsGo rejected booking {booking} for carrier {scac} (HTTP 422): {_error_message(resp)}")
        return ServiceError(SERVICE, f"unexpected HTTP {status} for {method} {path}", status=status,
                            body_excerpt=redact(_response_text(resp))[:500],
                            request_id=request_id_from(resp.headers))


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 09 §B.7.3 — tracking state file
# ════════════════════════════════════════════════════════════════════════════════════════════════
class TrackingState:
    """`<ETA_STATE_DIR>/aggregator_tracking.json` (09 §B.7.3): key "{provider}:{scac}:{booking}" → entry
    {tracking_id, created_by_us, first_seen_at, last_status, last_checked_at} (naive local ISO times).

    Loaded lazily; a missing file is empty; a corrupt file → WARNING, treated as empty and overwritten on
    the next put(). Writes are atomic (temp file in the same directory + os.replace) under a
    threading.Lock, and re-read the file first so that entries written meanwhile by another process (e.g.
    a concurrent `eta eta`) are kept. No secrets live here. Entries are only removed by forget().
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] | None = None

    @staticmethod
    def make_key(provider_key: str, scac: str, booking: str) -> str:
        return f"{provider_key}:{scac}:{booking}"

    def get(self, provider_key: str, scac: str, booking: str) -> dict[str, Any] | None:
        """A copy of the entry, or None."""
        with self._lock:
            entry = self._loaded().get(self.make_key(provider_key, scac, booking))
            return dict(entry) if entry is not None else None

    def put(self, provider_key: str, scac: str, booking: str, entry: Mapping[str, Any]) -> None:
        """Store `entry` (replacing the old one). The in-memory copy is updated before the disk write, so
        this run keeps the tracking id even if the write fails; an OSError from the write is re-raised."""
        self._mutate(self.make_key(provider_key, scac, booking), dict(entry))

    def forget(self, provider_key: str, scac: str, booking: str) -> None:
        """Remove the entry (after the provider answered 404 for its tracking id)."""
        self._mutate(self.make_key(provider_key, scac, booking), None)

    # ── internals ──
    def _loaded(self) -> dict[str, dict[str, Any]]:
        if self._entries is None:
            entries, problem = self._read()
            if problem:
                log.warning("[aggregator] %s — treating the tracking state as empty; it is rewritten on the "
                            "next update", problem)
            self._entries = entries or {}
        return self._entries

    def _read(self) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
        """(entries, None) | (None, None) for a missing file | (None, problem) for an unusable one."""
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None, None
        except (OSError, UnicodeDecodeError) as exc:
            return None, f"could not read {self.path} ({exc})"
        try:
            data = json.loads(text)
        except ValueError as exc:
            return None, f"{self.path} is not valid JSON ({exc})"
        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return None, f"{self.path} has no 'entries' object"
        return {str(k): dict(v) for k, v in entries.items() if isinstance(v, dict)}, None

    def _mutate(self, key: str, entry: dict[str, Any] | None) -> None:
        with self._lock:
            current, _problem = self._read()  # changes another process made since our load
            entries = dict(current) if current is not None else dict(self._loaded())
            if entry is None:
                if key not in entries:
                    self._entries = entries
                    return
                entries.pop(key)
            else:
                entries[key] = entry
            # Serialise first: a non-JSON value fails here, before anything changes.
            payload = json.dumps({"version": STATE_VERSION, "entries": entries}, indent=2, sort_keys=True,
                                 ensure_ascii=False) + "\n"
            self._entries = entries
            self._write_atomic(payload)

    def _write_atomic(self, payload: str) -> None:
        directory = self.path.parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 09 §B.7.5 — the multi-carrier client
# ════════════════════════════════════════════════════════════════════════════════════════════════
class AggregatorClient:
    """Serves every carrier routed to the aggregator (09 Part D asks it for per-carrier views)."""

    def __init__(self, settings: AggregatorSettings, *, backend: AggregatorBackend | None = None,
                 state: TrackingState | None = None, clock: Clock | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        self._settings = settings
        self._backend: AggregatorBackend = backend if backend is not None else ShipsGoBackend(settings)
        self._state = state if state is not None else TrackingState(settings.state_file)
        self._clock: Clock = clock if clock is not None else SystemClock()
        self._sleep = sleep                       # injectable for tests (poll waits)
        self._create_lock = asyncio.Lock()        # creates are processed one at a time per company (§B.5)
        self._creates = 0                         # new (charged) registrations this run
        self._views: dict[Carrier, AggregatorCarrierView] = {}
        self._closed = False

    # ── accessors ──
    @property
    def settings(self) -> AggregatorSettings:
        return self._settings

    @property
    def backend(self) -> AggregatorBackend:
        return self._backend

    @property
    def state(self) -> TrackingState:
        return self._state

    @property
    def creates_this_run(self) -> int:
        """Registrations counted against AGG_MAX_CREATES_PER_RUN so far."""
        return self._creates

    def __repr__(self) -> str:
        return f"AggregatorClient(provider={self._backend.key!r}, state={str(self._state.path)!r})"

    # ── public API ──
    def view(self, carrier: Carrier) -> AggregatorCarrierView:
        """The CarrierClient for one carrier (cached per carrier)."""
        member = Carrier(carrier)
        view = self._views.get(member)
        if view is None:
            view = self._views[member] = AggregatorCarrierView(self, member)
        return view

    async def prepare(self, items: Iterable[tuple[Carrier, str]]) -> dict[str, int]:
        """Register every (carrier, booking) once, sequentially, before the per-row tasks start
        (09 §B.7.5, called by the router): new bookings then have the whole planning phase to become ready.

        Never raises: failures are logged as WARNINGs (get_eta reports them properly later). Returns
        {"known", "found", "created", "failed"} ("found" includes a create answered 409 — no charge).
        """
        name = self._backend.name
        counts = {"known": 0, "found": 0, "created": 0, "failed": 0}
        unique: dict[tuple[Carrier, str], None] = {}
        for item in items:
            try:
                carrier, booking = item
                unique.setdefault((Carrier(carrier), str(booking).strip().upper()), None)
            except (TypeError, ValueError) as exc:
                counts["failed"] += 1
                log.warning("[%s] prepare: ignored an invalid item %r (%s)", name, item, exc)
        systemic = 0
        pending = list(unique)
        for position, (carrier, ref) in enumerate(pending):
            if systemic >= PREPARE_SYSTEMIC_LIMIT:
                # Defensive early stop: the provider itself is failing (token, credits, outage); every further
                # call would fail the same way and slow the run down. get_eta() reports each row later.
                skipped = len(pending) - position
                counts["failed"] += skipped
                log.warning("[%s] prepare: stopped after %d consecutive provider errors; %d booking(s) not "
                            "pre-registered", name, systemic, skipped)
                break
            try:
                member, scac, _policy = self._carrier_config(carrier)
                self._check_booking(ref)
                _entry, _tid, outcome = await self._ensure_tracking(member, scac, ref,
                                                                    allow_create=self._settings.allow_create)
            except Exception as exc:  # noqa: BLE001 - 09 §B.7.5: swallow, get_eta re-raises properly later
                counts["failed"] += 1
                if not isinstance(exc, AggregatorBookingError):
                    systemic += 1
                log.warning("[%s] prepare: could not register %s booking %s: %s", name, carrier, ref, exc)
                continue
            systemic = 0
            if outcome == _CREATED:
                counts["created"] += 1
            elif outcome == _KNOWN:
                counts["known"] += 1
            else:
                counts["found"] += 1
        log.info("[%s] prepare: %d booking(s) — %d known, %d found, %d created, %d failed", name, len(unique),
                 counts["known"], counts["found"], counts["created"], counts["failed"])
        return counts

    async def get_eta(self, carrier: Carrier, booking: str) -> EtaResult:
        """09 §B.7.5. Raises only CarrierLookupError (incl. AggregatorBookingError) or NoEtaError."""
        name = self._backend.name
        member, scac, policy = self._carrier_config(carrier)
        ref = self._check_booking(booking)
        log.info("[%s] Looking up ETA for %s booking %s...", name, member, ref)
        try:
            # 1. resolve the tracking id (state → provider list → create)
            entry, tid, outcome = await self._ensure_tracking(member, scac, ref,
                                                              allow_create=self._settings.allow_create)
            # 2. details; deleted provider-side → forget and re-resolve ONCE
            snap = await self._backend.fetch(tid)
            if snap is None:
                log.warning("[%s] shipment %s of %s booking %s no longer exists (HTTP 404) — resolving the "
                            "booking again", name, tid, member, ref)
                self._forget(scac, ref)
                entry, tid, outcome = await self._ensure_tracking(member, scac, ref,
                                                                  allow_create=self._settings.allow_create)
                snap = await self._backend.fetch(tid)
                if snap is None:
                    raise CarrierLookupError(f"{name} lost shipment {tid} for booking {ref}")
            # 3. a FRESH shipment may still be fetching carrier data: poll a little
            fresh = outcome in _CREATE_OUTCOMES or self._is_fresh(entry)
            snap = await self._wait_until_ready(tid, snap, fresh=fresh, carrier=member, booking=ref)
            # 4. remember what we saw
            self._save(scac, ref, {**entry, "last_status": snap.status, "last_checked_at": self._now_iso()})
            # 5–7. per-booking failures, then the DCSA arrival choice
            result = self._result(member, ref, tid, snap, entry, policy)
        except (CarrierLookupError, NoEtaError):
            raise
        except AuthError as exc:
            raise CarrierLookupError(redact(f"{name} authentication failed: {exc}")) from exc
        except ServiceError as exc:
            raise CarrierLookupError(f"{name} API error for booking {ref}: {exc}") from exc
        except httpx.HTTPError as exc:
            raise CarrierLookupError(redact(f"{name} network error: {exc!r}")) from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise CarrierLookupError(f"{name} returned an unexpected payload: {exc}") from exc
        log.info("[%s] ETA=%s (%s)", name, result.eta.strftime("%m/%d/%Y"), result.source)
        return result

    async def probe(self, carrier: Carrier | None, booking: str | None) -> dict[str, Any]:
        """Diagnostics for `eta probe aggregator` (09 §B.7.5, §B.11). NEVER creates (a create costs a
        credit) and never writes the state file. JSON-serializable; the token never appears.

        Keys: provider, service, base_url, account_ok, carriers (ACTIVE carriers whose SCAC is configured
        or whose name contains GRIMALDI), scac_check, requests, raw, eta, error; with a booking also
        carrier, booking, policy, state_entry, find_existing and — if tracked — tracking_id, snapshot,
        explain; otherwise note. `error` is set only when the integration itself failed.
        """
        s = self._settings
        backend = self._backend
        out: dict[str, Any] = {
            "provider": backend.key,
            "service": backend.name,
            "base_url": s.base_url,
            "account_ok": False,
            "carriers": [],
            "scac_check": {},
            "requests": [],
            "raw": None,
            "eta": None,
            "error": None,
        }
        trace_token = _TRACE.set(out["requests"])
        try:
            await self._probe_account(out)
            if out["account_ok"] and booking is not None and str(booking).strip():
                await self._probe_booking(out, carrier, str(booking))
        finally:
            _TRACE.reset(trace_token)
        return _scrub(out, s.api_token)

    async def aclose(self) -> None:
        """Close the backend once (views never close anything)."""
        if self._closed:
            return
        self._closed = True
        await self._backend.aclose()

    async def __aenter__(self) -> AggregatorClient:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.aclose()

    # ── internals: configuration and state ──
    def _carrier_config(self, carrier: Carrier | str) -> tuple[Carrier, str, Policy]:
        try:
            member = Carrier(carrier)
        except ValueError:
            raise CarrierLookupError(f"Unknown carrier {carrier!r} for the aggregator") from None
        scac = (self._settings.scac.get(member) or "").strip().upper()
        if not scac:
            raise CarrierLookupError(f"No {self._backend.name} carrier code configured for {member} — set "
                                     f"AGG_SCAC_{member.name} in .env")
        return member, scac, AGG_POLICY.get(member, "first")

    def _check_booking(self, booking: Any) -> str:
        ref = booking.strip().upper() if isinstance(booking, str) else ""
        if not BOOKING_NUMBER_RE.fullmatch(ref):
            raise AggregatorBookingError(
                f"{self._backend.name} cannot track booking {booking!r}: only letters, digits, '/' and '-' are "
                f"allowed")
        return ref

    def _now_iso(self) -> str:
        return self._clock.now().isoformat(timespec="seconds")

    def _is_fresh(self, entry: Mapping[str, Any]) -> bool:
        """First seen less than AGG_FRESH_MINUTES ago (an unreadable time counts as not fresh: no waiting)."""
        first_seen = _parse_local_iso(entry.get("first_seen_at"))
        if first_seen is None:
            return False
        return (self._clock.now() - first_seen) < timedelta(minutes=self._settings.fresh_minutes)

    def _known_entry(self, scac: str, ref: str) -> dict[str, Any] | None:
        entry = self._state.get(self._backend.key, scac, ref)
        if entry is None:
            return None
        tid = _text(entry.get("tracking_id"))
        if not tid:
            log.warning("[%s] ignoring the tracking-state entry of %s %s: it has no tracking_id",
                        self._backend.name, scac, ref)
            return None
        entry["tracking_id"] = tid
        return entry

    def _save(self, scac: str, ref: str, entry: Mapping[str, Any]) -> None:
        """put() that never fails the lookup: a lost write only costs a free list call next run."""
        try:
            self._state.put(self._backend.key, scac, ref, entry)
        except OSError as exc:
            log.warning("[%s] could not update %s (%s) — the tracking id is kept for this run; the next run "
                        "finds the booking again (no charge)", self._backend.name, self._state.path, exc)

    def _forget(self, scac: str, ref: str) -> None:
        try:
            self._state.forget(self._backend.key, scac, ref)
        except OSError as exc:
            log.warning("[%s] could not update %s (%s)", self._backend.name, self._state.path, exc)

    def _check_create_cap(self, ref: str) -> None:
        cap = self._settings.max_creates_per_run
        if self._creates >= cap:
            raise AggregatorBookingError(
                f"AGG_MAX_CREATES_PER_RUN ({cap}) reached — booking {ref} not registered with "
                f"{self._backend.name} this run")

    # ── internals: lookup steps ──
    async def _ensure_tracking(self, carrier: Carrier, scac: str, ref: str, *,
                               allow_create: bool) -> tuple[dict[str, Any], str, str]:
        """09 §B.7.5 `_ensure_tracking` → (entry, tracking_id, outcome); outcome is "known" (state file),
        "found" (provider list), "created" (200, 1 credit) or "existing" (409, no charge). The spec's
        `created_now` is `outcome in ("created", "existing")`."""
        name = self._backend.name
        entry = self._known_entry(scac, ref)
        if entry is not None:
            return entry, entry["tracking_id"], _KNOWN

        tid = await self._backend.find_existing(scac, ref)
        if tid is not None:
            entry = {"tracking_id": str(tid), "created_by_us": False, "first_seen_at": self._now_iso()}
            self._save(scac, ref, entry)
            log.info("[%s] %s booking %s is already tracked (shipment %s)", name, carrier, ref, tid)
            return entry, str(tid), _FOUND

        if not allow_create:
            raise AggregatorBookingError(
                f"{carrier} booking {ref} is not tracked in {name} and AGG_ALLOW_CREATE=false")
        self._check_create_cap(ref)
        async with self._create_lock:  # serialised creates (ShipsGo answers 429 to concurrent ones)
            entry = self._known_entry(scac, ref)  # another coroutine may have just created it
            if entry is not None:
                return entry, entry["tracking_id"], _KNOWN
            self._check_create_cap(ref)  # re-check: other creates may have used the cap while we waited
            try:
                tid = await self._backend.create(scac, ref)
            except ServiceError as exc:
                if exc.status in _AMBIGUOUS_CREATE_STATUSES:
                    # Defensive (credit safety valve): no answer / 5xx — the provider may have registered and
                    # charged it, so it counts against AGG_MAX_CREATES_PER_RUN.
                    self._creates += 1
                raise
            is_new = getattr(self._backend, "last_create_was_new", True) is not False
            if is_new:
                self._creates += 1  # 200 only; a 409 duplicate is free and not counted
            entry = {"tracking_id": str(tid), "created_by_us": True, "first_seen_at": self._now_iso()}
            self._save(scac, ref, entry)
            if is_new:
                log.info("[%s] registered %s booking %s (shipment %s, 1 credit; %d this run)", name, carrier,
                         ref, tid, self._creates)
            else:
                log.info("[%s] %s booking %s was already registered (shipment %s, no charge)", name, carrier,
                         ref, tid)
            return entry, str(tid), _CREATED if is_new else _EXISTING

    async def _wait_until_ready(self, tid: str, snap: AggregatorSnapshot, *, fresh: bool, carrier: Carrier,
                                booking: str) -> AggregatorSnapshot:
        """09 §B.7.5 step 3: poll a FRESH shipment for up to AGG_FIRST_LOOKUP_WAIT_S. The time waited is
        the larger of the real elapsed time and the sum of the poll intervals, so an injected no-op sleep
        still ends the loop after ceil(wait / interval) polls."""
        s = self._settings
        if not fresh or snap.ready or snap.failure is not None:
            return snap
        wait = float(max(0, s.first_lookup_wait_s))
        interval = max(MIN_POLL_INTERVAL_S, float(s.poll_interval_s))
        started = time.monotonic()
        waited = 0.0
        while not snap.ready and snap.failure is None:
            if max(time.monotonic() - started, waited) >= wait:
                break
            log.info("[%s] %s booking %s: status %s, no carrier data yet — checking again in %.0f s",
                     self._backend.name, carrier, booking, snap.status or "?", interval)
            await self._sleep(interval)  # NOT holding the semaphore
            waited += interval
            snap = await self._backend.fetch(tid) or snap
        return snap

    def _result(self, carrier: Carrier, booking: str, tid: str, snap: AggregatorSnapshot,
                entry: Mapping[str, Any] | None, policy: Policy) -> EtaResult:
        """09 §B.7.5 steps 5–7 (pure): per-booking failures, then the DCSA POD arrival, else the route's
        date_of_discharge."""
        name = self._backend.name
        if snap.failure:
            raise AggregatorBookingError(
                f"{name} could not track {carrier} booking {booking} (status {snap.failure}) — the number may be "
                f"wrong or {carrier} publishes no data for it")
        if not snap.ready:
            since = (entry or {}).get("first_seen_at") or "unknown"
            raise AggregatorBookingError(
                f"{name} has not fetched {carrier} data for booking {booking} yet (status {snap.status}, "
                f"tracking since {since}) — re-run later")
        arrival = (dcsa.booking_arrival(snap.events, policy=policy, pod_unlocode=snap.pod_unlocode)
                   or snap.route_pod_arrival)
        if arrival is None:
            # DEVIATION (MSC via aggregator, 09 §B.7.6): no customs-hold heuristic and no "Full Transshipment
            # Loaded + 7 days" fallback here (MSC-GraphQL rules, 06); a booking without a POD date is NoEtaError.
            raise NoEtaError(
                f"{name} found {carrier} booking {booking} but has no arrival date for the port of discharge. "
                "Can't determine ETA.")
        result = dcsa.to_eta_result(arrival, carrier=carrier, booking=booking, containers=snap.containers,
                                    source_prefix=name)
        return replace(result, details={**result.details, "provider": self._backend.key, "tracking_id": tid,
                                        "status": snap.status, "policy": policy, "pod": snap.pod_unlocode})

    # ── internals: probe ──
    async def _probe_account(self, out: dict[str, Any]) -> None:
        name = self._backend.name
        try:
            carriers = await self._backend.list_carriers()
        except (CarrierLookupError, ServiceError, AuthError, httpx.HTTPError, KeyError, TypeError,
                ValueError) as exc:
            out["error"] = f"{name} account check failed: {exc}"
            return
        out["account_ok"] = True
        wanted = {code.strip().upper() for code in self._settings.scac.values() if code}
        active: set[str] = set()
        listed: list[dict[str, Any]] = []
        for item in carriers:
            scac = _upper(item.get("scac")) or ""
            status = _upper(item.get("status")) or "ACTIVE"   # the listing is already filtered on ACTIVE
            if status != "ACTIVE":
                continue
            active.add(scac)
            carrier_name = _text(item.get("name")) or ""
            if scac in wanted or "GRIMALDI" in carrier_name.upper():
                listed.append({"scac": scac, "name": carrier_name, "status": status})
        out["carriers"] = listed
        out["scac_check"] = {
            str(member): {"scac": code, "active": code.strip().upper() in active}
            for member, code in self._settings.scac.items()
        }

    async def _probe_booking(self, out: dict[str, Any], carrier: Carrier | None, booking: str) -> None:
        name = self._backend.name
        ref = booking.strip().upper()
        if carrier is None:
            derived = _carrier_from_booking(ref)
            if derived is None:
                out["error"] = (f"cannot tell the carrier of booking {ref} from its pattern (01 §4) — pass the "
                                f"carrier explicitly")
                return
            carrier, ref = derived
        try:
            member, scac, policy = self._carrier_config(carrier)
            ref = self._check_booking(ref)
        except CarrierLookupError as exc:
            out["error"] = str(exc)
            return
        out.update({"carrier": str(member), "booking": ref, "scac": scac, "policy": policy})
        entry = self._known_entry(scac, ref)
        out["state_entry"] = entry
        try:
            found = await self._backend.find_existing(scac, ref)
        except (CarrierLookupError, ServiceError, AuthError, httpx.HTTPError, KeyError, TypeError,
                ValueError) as exc:
            out["find_existing"] = None
            out["error"] = f"{name} shipment search failed: {exc}"
            return
        out["find_existing"] = found
        tid = entry["tracking_id"] if entry is not None else found
        if not tid:
            out["note"] = NOT_TRACKED_NOTE
            return
        out["tracking_id"] = tid
        try:
            snap = await self._backend.fetch(tid)
        except (CarrierLookupError, ServiceError, AuthError, httpx.HTTPError, KeyError, TypeError,
                ValueError) as exc:
            out["error"] = f"{name} shipment details failed: {exc}"
            out["eta"] = error_text(CarrierLookupError(f"{name} shipment details failed: {exc}"))
            return
        if snap is None:
            out["note"] = (f"shipment {tid} no longer exists at {name} (HTTP 404); the next lookup forgets it and "
                           f"resolves the booking again")
            out["eta"] = error_text(CarrierLookupError(f"{name} lost shipment {tid} for booking {ref}"))
            return
        out["raw"] = redact(snap.raw)
        route = snap.route_pod_arrival
        out["snapshot"] = {
            "status": snap.status,
            "ready": snap.ready,
            "failure": snap.failure,
            "pod": snap.pod_unlocode,
            "containers": list(snap.containers),
            "events": len(snap.events),
            "route_pod_arrival": None if route is None else {
                "eta": route.eta.isoformat(), "classifier": route.classifier, "basis": route.basis},
            "discarded_at": _mapping(_mapping(snap.raw).get("shipment")).get("discarded_at"),
        }
        explain = [
            f"{name} shipment {tid}: status {snap.status or '?'} (ready={snap.ready}, failure={snap.failure}), "
            f"POD {snap.pod_unlocode or '-'}, {len(snap.events)} movement(s) adapted to DCSA events",
        ]
        try:
            explain += dcsa.explain_selection(snap.events, pod_unlocode=snap.pod_unlocode, policy=policy)
        except (KeyError, TypeError, ValueError) as exc:
            explain.append(f"explain failed: {exc}")
        if route is not None:
            explain.append(f"route POD date_of_discharge: {route.eta.isoformat()} {route.classifier} at "
                           f"{route.unlocode or '?'} (used only when no container/vessel arrival is found)")
        explain.append("note: movement timestamps are converted to each location's time zone "
                       "(location.timezone); compare with the carrier's site (09 §B.11 step 2)")
        out["explain"] = explain
        try:
            out["eta"] = self._result(member, ref, tid, snap, entry, policy).to_dict()
        except (CarrierLookupError, NoEtaError) as exc:
            out["eta"] = error_text(exc)
        except (KeyError, TypeError, ValueError) as exc:
            out["eta"] = error_text(CarrierLookupError(f"{name} returned an unexpected payload: {exc}"))


class AggregatorCarrierView:
    """CarrierClient (03 §9) for one carrier, served by the shared AggregatorClient (09 §B.7.1)."""

    def __init__(self, client: AggregatorClient, carrier: Carrier) -> None:
        self.carrier = carrier
        self._client = client

    @property
    def client(self) -> AggregatorClient:
        return self._client

    async def get_eta(self, booking: str) -> EtaResult:
        return await self._client.get_eta(self.carrier, booking)

    async def probe(self, booking: str) -> dict[str, Any]:
        return await self._client.probe(self.carrier, booking)

    async def aclose(self) -> None:
        """No-op: the router closes the AggregatorClient once (09 §D.2)."""
        return None

    def __repr__(self) -> str:
        return f"AggregatorCarrierView({self.carrier!s})"


def build_client(settings: Settings, token_store: TokenStore | None, *, clock: Clock | None = None,
                 http: httpx.AsyncClient | None = None) -> AggregatorClient | None:
    """None when settings.aggregator is None (AGG_API_TOKEN missing). `token_store` is unused (static
    token, 09 §B.6). Unlike the carrier modules this returns the multi-carrier client; the router asks it
    for views (09 §B.7.1). `http` is passed to the backend (tests inject a respx-mocked client)."""
    agg = settings.aggregator
    if agg is None:
        return None
    provider = (agg.provider or "shipsgo").strip()
    if provider.lower() != "shipsgo":
        raise ConfigError(f"AGG_PROVIDER={provider} is described in 09 §B.9 but not implemented")
    return AggregatorClient(agg, backend=ShipsGoBackend(agg, http=http), clock=clock)
