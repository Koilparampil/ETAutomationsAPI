"""MSC carrier client (06-carrier-msc): the myMSC token bridge and the official DCSA Track & Trace v2.2 API.

Modes (06 §1; MSC_MODE is resolved by config.py, so "auto" never reaches this module):
- ``mymsc`` (interim bridge): the original GraphQL query, verbatim (06 §4.1), sent over plain HTTP with an access
  token the user pastes from their OWN signed-in myMSC browser session (``eta msc-token``, 06 §2.2).
  MSC-D1: no browser at all. Automating the myMSC sign-in is deliberately not specified (06 §1).
- ``api`` (recommended): ``GET {MSC_API_BASE_URL}/events?carrierBookingReference=…`` with the Azure API Management
  subscription key (06 §4.2). The public mock sandbox is refused for real lookups (06 §6.4).

The ETA rules are pure functions (``select_eta``, ``eta_from_graphql``, ``eta_from_api`` and the parsers): no I/O,
and all business logic is tested through them (06 §6.1). ``MscClient.get_eta`` raises only CarrierLookupError,
NoEtaError or CustomsHoldError (03 §5 boundary rule).

No bot-protection workaround of any kind (06 §6.5, §8): an Akamai "Access Denied" page stops MSC lookups for the
run with one clear message, and a request is never retried with different headers, user agents or TLS settings.
Secrets (bridge token, subscription key) are never logged; probe payloads are redacted (06 §6.6).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import html
import json
import logging
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from types import MappingProxyType, TracebackType
from typing import Any, Final, Literal, NoReturn
from urllib.parse import urlsplit

import httpx

from ..auth import bearer_headers, request_with_auth
from ..clock import Clock, SystemClock
from ..config import MscSettings, Settings
from ..errors import AuthError, CarrierLookupError, ConfigError, CustomsHoldError, NoEtaError, ServiceError
from ..http import AsyncLimiter, json_body, new_async_client, request
from ..logging_setup import redact
from ..models import Carrier, EtaResult
from ..token_store import TokenStore, is_fresh, jwt_exp
from . import dcsa
from .base import TokenCache, error_text

__all__ = [
    "DISCHARGE_EVENT",
    "EDGE_BLOCK_MESSAGE",
    "ETA_EVENT",
    "GRAPHQL_QUERY",
    "GRAPHQL_QUERY_SHA256",
    "NO_TOKEN_MESSAGE",
    "PREFLIGHT_MIN_SECONDS",
    "RESCUE_SOURCE_API",
    "RESCUE_SOURCE_MYMSC",
    "SANDBOX_BASE_URL",
    "SANDBOX_EXPECTED_ETA",
    "SANDBOX_HOST",
    "SANDBOX_PROBE_BOOKING",
    "SANDBOX_REFUSAL",
    "TOKEN_REJECTED_MESSAGE",
    "TRANSSHIPMENT_EVENT",
    "MscBridgeAuth",
    "MscClient",
    "MscSelection",
    "bridge_settings",
    "bridge_token_status",
    "build_client",
    "compare_modes",
    "eta_from_api",
    "eta_from_graphql",
    "format_compare_table",
    "graphql_body",
    "graphql_variables",
    "no_match_message",
    "normalize_token",
    "parse_msc_date",
    "parse_wall_datetime",
    "probe_output_path",
    "probe_sandbox",
    "sandbox_settings",
    "save_probe_output",
    "select_eta",
    "store_manual_token",
    "token_needs_refresh",
    "transshipment_eta",
]

log = logging.getLogger("eta.carrier.msc")

# ════════════════════════════════════════════════════════════════════════════════════════════════
# Constants (06 §6.1)
# ════════════════════════════════════════════════════════════════════════════════════════════════
SERVICE: Final = "msc"            # service label of http/auth errors and of the "eta.http" DEBUG lines
TOKEN_KEY: Final = "msc"          # TokenStore entry (03 §7, 06 §5.1)

ETA_EVENT: Final = "Estimated Time of Arrival"
DISCHARGE_EVENT: Final = "Import Discharged from Vessel"
TRANSSHIPMENT_EVENT: Final = "Full Transshipment Loaded"
SANDBOX_HOST: Final = "portal.api.msc.com"
SANDBOX_REFUSAL: Final = ("MSC_API_BASE_URL points at MSC's public SANDBOX (mock data). Refusing to use mock ETAs "
                          "for real bookings — use `eta probe msc --sandbox` for testing only.")
SANDBOX_BASE_URL: Final = "https://portal.api.msc.com/dpo/trackandtrace/v2.2"   # 06 §2.1 public sandbox (no key)
SANDBOX_EXPECTED_ETA: Final = date(2022, 12, 26)  # 06 §10: the mock data's 'Estimated Time of Arrival'
SANDBOX_PROBE_BOOKING: Final = "EBKG00000000"     # the sandbox returns the same mock events for any reference

EDGE_BLOCK_MESSAGE: Final = (
    "MSC's edge network refused the request from this network (Akamai 'Access Denied'). This tool does not try "
    "to get around bot protection. Run it from the network where myMSC works in your browser, or switch to the "
    "official MSC API (MSC_MODE=api)."
)
NO_TOKEN_MESSAGE: Final = (
    "No usable MSC access token. Paste a fresh one from your signed-in myMSC browser session with `eta msc-token` "
    "(see 06-carrier-msc §2.2), or configure the official MSC API (MSC_API_BASE_URL + MSC_API_SUBSCRIPTION_KEY)."
)
TOKEN_REJECTED_MESSAGE: Final = (
    "The MSC access token expired or was rejected (HTTP 401). Paste a fresh one with `eta msc-token`."
)
EMPTY_TOKEN_MESSAGE: Final = (
    "The MSC access token is empty. Copy the value of the Authorization request header (Bearer eyJ…) of the "
    "myMSC 'graphql' request from your signed-in browser (06-carrier-msc §2.2)."
)
SPACED_TOKEN_MESSAGE: Final = (
    "The MSC access token contains spaces or line breaks. Paste only the token itself "
    "(with or without the leading 'Bearer ')."
)
UNPRINTABLE_TOKEN_MESSAGE: Final = (   # review fix MSK-01: an HTTP header can carry only printable ASCII
    "The MSC access token contains invisible or non-ASCII characters (for example a zero-width space, or '…' from "
    "a shortened copy). Copy the whole token again from your signed-in myMSC session (06-carrier-msc §2.2)."
)
STORED_TOKEN_UNSENDABLE_MESSAGE: Final = (
    "The stored MSC access token contains invisible or non-ASCII characters and cannot be sent. Paste it again "
    "with `eta msc-token`."
)
RESCUE_SOURCE_MYMSC: Final = "MSC containers[0].finalEta (rescue)"   # 06 §7.5
RESCUE_SOURCE_API: Final = "MSC API DCSA arrival (rescue)"           # 06 §7.5

API_KEY_HEADER: Final = "Ocp-Apim-Subscription-Key"                  # 06 §4.2 (header, never the query form)
API_EXPECTED_STATUSES: Final = (200, 400, 401, 403, 404)               # 06 §6.4 api step 2
MYMSC_REFERER: Final = "https://www.mymsc.com/"                        # 06 §4.1 (parity; harmless)
GRAPHQL_OPERATION: Final = "trackingByBookingNumber"
CONNECT_TIMEOUT_S: Final = 10.0                                        # 06 §3.2: connect stays 10 s
PREFLIGHT_MIN_SECONDS: Final = 600   # 06 §5.1 / 10 Phase 1: a bridge token needs >= 10 minutes left before a run

# ── GraphQL tracking query, VERBATIM from the original (06 §4.1): do not change the text ──
GRAPHQL_QUERY: Final[str] = "\n    query trackingByBookingNumber($input: TrackingByBookingNumberGraphQlRequestInput!) {\n  trackingByBookingNumber(request: $input) {\n    allBillNumber\n    finalDischargePortCountry\n    finalDischargePortName\n    finalDischargePortUnCode\n    originName\n    originCode\n    destinationName\n    destinationCode\n    portLoadName\n    portLoadUnCode\n    priceCalculationDate\n    priceCalculationDateAsString\n    shippedToCountry\n    shippedToName\n    transshipmentName\n    transshipmentCode\n    resultsDateTimeInformations {\n      date\n      time\n    }\n    containers {\n      containerIsoDesc\n      containerIsOffService\n      containerNumber\n      finalEta\n      isDestinationReached\n      events {\n        emptyIndicatorCode\n        eventDate\n        eventDateDateTime\n        eventName\n        eventTime\n        lloydsNumber\n        locationCountry\n        locationName\n        locationUnCode\n        vesselName\n        voyageDesc\n        smdgTerminalCode\n        bicFacilityCode\n        activityEquipmentHandlingFacilityName\n        activityEquipmentHandlingFacilityCode\n        isEventPassed\n        portCalls {\n          portCallDate\n          isEventPassed\n          locationName\n          locationUnCode\n          locationCountry\n          etd\n          eta\n          eventDateTime\n        }\n      }\n    }\n    customsRelease {\n      error\n      response\n      details {\n        container\n        customsReference\n        customsStatus\n      }\n    }\n    lastFreeDates {\n      retrievalLevel\n      lastFreeDates {\n        containerNumber\n        lastFreeDate\n        errorMessage\n      }\n    }\n    freightRelease {\n      error\n      response\n    }\n  }\n}\n    "
GRAPHQL_QUERY_SHA256: Final[str] = "d5230f52190941f6f8ce1af4e5619ebe05b795b54d2eec0bc1ae95b9f67d5512"  # sha256(GRAPHQL_QUERY.encode())

_REFERENCE_HINT: Final = (" — MSC may not accept EBKG… references as carrierBookingReference on this subscription; "
                          "ask MSC to confirm (06-carrier-msc §2.1, question 3)")
_REFERENCE_HINT_RE = re.compile(r"(?i)reference|format|pattern|invalid|length")
_MSC_DATE_RE = re.compile(r"^([0-9]{1,2})\s+([A-Za-z]{3})\s+([0-9]{4})$")
_MONTHS: Final[Mapping[str, int]] = MappingProxyType({
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
})
_MONTH_ABBR: Final = ("", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_FRACTION_RE = re.compile(r"([.,]\d{6})\d+")
_AUTH_PREFIX_RE = re.compile(r"^(?:authorization\s*:\s*)?(?:bearer(?:\s+|$))?", re.IGNORECASE)
_ALLOWED_JWT_CLAIMS: Final = ("iss", "aud", "exp", "iat")   # never sub / e-mail / name claims (06 §5.1)
_TYPE_CODE_KEYS: Final[Mapping[str, str]] = MappingProxyType({
    "TRANSPORT": "transporteventtypecode",
    "EQUIPMENT": "equipmenteventtypecode",
    "SHIPMENT": "shipmenteventtypecode",
})
_SOURCE_COLUMN_MAX = 70

LatchKind = Literal["auth", "edge", "api", "config"]   # "config": a setting that cannot be sent (MSK-01)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# GraphQL body (06 §4.1)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def graphql_variables(booking: str) -> dict[str, Any]:
    """The original's variables, unchanged (06 §4.1)."""
    return {"input": {
        "retrieveFreightRelease": False,
        "retrieveTracking": True,
        "agency": {"id": -1, "isCustomsReleaseActive": False, "isFreightReleaseActive": False,
                   "isLastFreeDateActive": False, "isTrackingActive": False, "name": ""},
        "retrieveCustomsRelease": False,
        "retrieveLastFreeDate": False,
        "bookingNumber": booking,
    }}


def graphql_body(booking: str) -> dict[str, Any]:
    """POST body of the tracking query (06 §4.1)."""
    return {"query": GRAPHQL_QUERY, "variables": graphql_variables(booking),
            "operationName": GRAPHQL_OPERATION}


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Parsers (06 §6.2) — pure
# ════════════════════════════════════════════════════════════════════════════════════════════════
def parse_msc_date(value: str) -> datetime:
    """Locale-independent "%d %b %Y" (MSC-D6): "12 Nov 2026" → datetime(2026, 11, 12).

    The month comes from a fixed English map because strptime("%b") depends on the locale (Windows).
    Anything else → ValueError(f"unrecognised MSC date {value!r}"). A year outside dcsa.PLAUSIBLE_YEARS (2000-2100,
    e.g. the .NET sentinels 01 Jan 0001 / 31 Dec 9999) → ValueError too (review fix MSK-02, as 09 §A.3.4 step 3):
    a matched event with such a date is then the parity NoEtaError, never a year-1 or year-9999 ETA.
    """
    text = value.strip() if isinstance(value, str) else ""
    match = _MSC_DATE_RE.match(text)
    month = _MONTHS.get(match.group(2).upper()) if match else None
    if match is None or month is None:
        raise ValueError(f"unrecognised MSC date {value!r}")
    try:
        parsed = datetime(int(match.group(3)), month, int(match.group(1)))
    except ValueError as exc:  # e.g. "31 Feb 2026"
        raise ValueError(f"unrecognised MSC date {value!r}") from exc
    if not _plausible(parsed):
        raise ValueError(f"implausible MSC date {value!r} (year outside {_YEARS_TEXT})")
    return parsed


def _plausible(value: datetime) -> bool:
    """09 §A.3.4 step 3: event dates outside dcsa.PLAUSIBLE_YEARS are sentinels, not dates (review fix MSK-02)."""
    return value.year in dcsa.PLAUSIBLE_YEARS


_YEARS_TEXT = f"{dcsa.PLAUSIBLE_YEARS.start}-{dcsa.PLAUSIBLE_YEARS.stop - 1}"


def parse_wall_datetime(value: str) -> datetime:
    """ISO-8601 → naive wall-clock time: the offset is DROPPED, never converted (03 §4, 06 §4.2).

    A trailing Z means +00:00; fractional seconds are truncated to 6 digits (.NET's 7-digit fractions);
    a date-only "YYYY-MM-DD" gives midnight. Anything else → ValueError.
    """
    if not isinstance(value, str):
        raise ValueError(f"unrecognised MSC date-time {value!r}")
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    text = _FRACTION_RE.sub(r"\1", text)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"unrecognised MSC date-time {value!r}") from exc
    return parsed.replace(tzinfo=None)


def transshipment_eta(loaded: datetime, now: datetime) -> datetime:
    """Exact port of the original's three branches (06 §6.2); the last two are equivalent, kept for visible parity."""
    if loaded <= now - timedelta(days=6):
        return now + timedelta(days=7)            # keeps now's time-of-day (parity: pd.Timestamp.now() + 7 days)
    elif now - timedelta(days=6) < loaded < now:
        return loaded + timedelta(days=7)
    else:                                         # loaded >= now
        return loaded + timedelta(days=7)


def _format_msc_date(value: datetime) -> str:
    """"%d %b %Y" with English month names whatever the locale (the source text of the transshipment rule)."""
    return f"{value.day:02d} {_MONTH_ABBR[value.month]} {value.year:04d}"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Event selection (06 §6.3) — pure
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class MscSelection:
    eta: datetime
    event_name: str
    is_actual: bool
    from_transshipment_rule: bool
    raw_date: str
    # ── additions (defaulted, so the 06 §6.1 positional form still works) ──
    event_date: datetime | None = None    # date_of(matched event): e.g. the loaded date of the transshipment rule
    event: Mapping[str, Any] | None = field(default=None, compare=False, repr=False)   # the matched event itself


def _event_name(ev: Any, name_key: str) -> str:
    """Event name compared after .strip() (MSC-D6); a non-text name or a non-object event never matches."""
    if not isinstance(ev, Mapping):
        return ""
    value = ev.get(name_key)
    return value.strip() if isinstance(value, str) else ""


def _raw_date_of(ev: Any) -> str:
    """_raw_date_of(ev) = ev.get("eventDate") or ev.get("eventDateTime") or "" (06 §6.3)."""
    if not isinstance(ev, Mapping):
        return ""
    value = ev.get("eventDate") or ev.get("eventDateTime") or ""
    return value if isinstance(value, str) else str(value)


def select_eta(events: Sequence[Mapping[str, Any]], *, now: datetime, name_key: str,
               date_of: Callable[[Mapping[str, Any]], datetime]) -> MscSelection | None:
    """The original algorithm (06 §6.3), in payload order (newest first): the first 'Estimated Time of Arrival' or
    'Import Discharged from Vessel' wins; otherwise the first 'Full Transshipment Loaded' + the transshipment rule.
    An unreadable date on the matched event → NoEtaError (parity: the original's ValueError →
    "Can't determine ETA.")."""
    for ev in events:
        name = _event_name(ev, name_key)
        if name == ETA_EVENT or name == DISCHARGE_EVENT:
            try:
                eta = date_of(ev)
            except ValueError as exc:  # parity: original ValueError → "Can't determine ETA."
                raise NoEtaError(f"MSC event {name!r} has an unreadable date ({exc})") from exc
            return MscSelection(eta, name, name == DISCHARGE_EVENT, False, _raw_date_of(ev), eta, ev)
    for ev in events:
        if _event_name(ev, name_key) == TRANSSHIPMENT_EVENT:
            try:
                loaded = date_of(ev)
            except ValueError as exc:
                raise NoEtaError(f"MSC event {TRANSSHIPMENT_EVENT!r} has an unreadable date ({exc})") from exc
            return MscSelection(transshipment_eta(loaded, now), TRANSSHIPMENT_EVENT, False, True,
                                _raw_date_of(ev), loaded, ev)
    return None


def no_match_message(booking: str) -> str:
    """NoEtaError text when no ETA / discharge / transshipment event exists (06 §7.1 step 8, §7.2 step 6)."""
    return (f"No '{ETA_EVENT}', '{DISCHARGE_EVENT}' or '{TRANSSHIPMENT_EVENT}' event for MSC booking {booking}. "
            "Can't determine ETA.")


def _selection_source(sel: MscSelection, *, api: bool) -> str:
    """EtaResult.source (06 §7.4)."""
    if sel.from_transshipment_rule:
        loaded = sel.event_date or sel.eta
        return f"MSC '{TRANSSHIPMENT_EVENT}' on {_format_msc_date(loaded)} → ETA estimate (+7 days)"
    return f"MSC API event '{sel.event_name}'" if api else f"MSC event '{sel.event_name}'"


def _other_container_etas(booking: str, chosen_label: str, chosen_eta: datetime,
                          others: Sequence[tuple[str, datetime | None]]) -> dict[str, str] | None:
    """Multi-container check, log only (MSC-D7, 06 §7.1 step 9): the decision still uses the first container."""
    if not any(eta is not None and eta.date() != chosen_eta.date() for _, eta in others):
        return None
    etas = {label: (eta.date().isoformat() if eta is not None else "n/a") for label, eta in others}
    summary = {chosen_label: chosen_eta.date().isoformat(), **etas}
    log.warning("MSC %s: containers have different ETAs %s; using first container %s (parity)",
                booking, summary, chosen_label)
    return etas


def _wrap_parse(fn: Callable[[], EtaResult], booking: str) -> EtaResult:
    """03 §5 boundary rule: lookup outcomes pass through; parsing crashes become CarrierLookupError (06 §6.4)."""
    try:
        return fn()
    except (CarrierLookupError, NoEtaError, CustomsHoldError):
        raise
    # LookupError = KeyError + IndexError; OverflowError = date arithmetic on an absurd date (MSK-04)
    except (LookupError, TypeError, AttributeError, ValueError, OverflowError) as exc:
        raise CarrierLookupError(f"Unexpected MSC payload shape for {booking}: {exc!r}") from exc


# ════════════════════════════════════════════════════════════════════════════════════════════════
# mymsc mode: exact port of checkingMSC (06 §7.1) — pure
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _error_list(value: Any) -> list[Any]:
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _error_messages(errors: Sequence[Any]) -> list[str]:
    """Readable, redacted, one-line texts of GraphQL errors."""
    texts: list[str] = []
    for err in errors:
        if isinstance(err, Mapping) and isinstance(err.get("message"), str) and err["message"].strip():
            text = err["message"]
        elif isinstance(err, str):
            text = err
        else:
            text = json.dumps(err, default=str)[:200]
        texts.append(" ".join(str(redact(text)).split()))
    return texts


def _graphql_date_of(ev: Mapping[str, Any]) -> datetime:
    return parse_msc_date(ev.get("eventDate") or "")


def _container_label(container: Any, index: int) -> str:
    number = _text(container.get("containerNumber")) if isinstance(container, Mapping) else ""
    return number or f"containers[{index}]"


def _graphql_container_eta(container: Any, now: datetime) -> datetime | None:
    """06 §7.1 step 9: steps 4 and 7 for another container. Log-only, so it never fails the lookup."""
    if not isinstance(container, Mapping):
        return None
    try:
        sel = select_eta(container.get("events") or [], now=now, name_key="eventName", date_of=_graphql_date_of)
    except (NoEtaError, ValueError, LookupError, TypeError, AttributeError, OverflowError):
        return None
    return sel.eta if sel is not None else None


def _graphql_rescue(c0: Mapping[str, Any]) -> MscSelection | None:
    """06 §7.5 (mymsc): containers[0].finalEta via parse_msc_date, else parse_wall_datetime → midnight.
    The field's format is UNVERIFIED; None when it is missing or unreadable."""
    raw = c0.get("finalEta")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        eta = parse_msc_date(raw)
    except ValueError:
        try:
            eta = datetime.combine(parse_wall_datetime(raw).date(), dtime())
        except ValueError:
            return None
        if not _plausible(eta):                    # a sentinel finalEta is no ETA (MSK-02)
            return None
    return MscSelection(eta, "finalEta", False, False, raw.strip(), eta, None)


def eta_from_graphql(body: Any, *, booking: str, now: datetime, rescue: bool = False) -> EtaResult:
    """myMSC GraphQL body → EtaResult: exact port of the original's checkingMSC (06 §7.1, steps 1–10).

    Raises CarrierLookupError (no/erroneous result), NoEtaError (no containers/events/usable event) or
    CustomsHoldError (destination reached but the latest event is in the US). Other shape errors raise
    KeyError/TypeError/AttributeError/ValueError, which MscClient maps to CarrierLookupError (_wrap_parse).
    """
    # 1. object, GraphQL errors (MSC-D4)
    if not isinstance(body, Mapping):
        raise CarrierLookupError(f"MSC returned a non-object response for {booking}")
    errors = _error_list(body.get("errors"))
    data = body.get("data") or {}
    results = data.get("trackingByBookingNumber")
    if errors and not results:
        raise CarrierLookupError(f"MSC GraphQL error for {booking}: " + "; ".join(_error_messages(errors[:3])))
    if errors:
        log.warning("MSC %s: GraphQL returned errors alongside the tracking result: %s",
                    booking, "; ".join(_error_messages(errors[:3])))
    # 2. tracking result (MSC-D4)
    if not results:
        raise CarrierLookupError(f"MSC returned no tracking result for booking {booking} — check the booking on myMSC")
    if len(results) > 1:
        log.info("MSC %s: %d tracking results returned; using the first (parity)", booking, len(results))
    first = results[0]
    # 3. first container only (parity, O8)
    containers = first.get("containers") or []
    if not containers:
        raise NoEtaError(f"MSC lists no containers for booking {booking} yet. Can't determine ETA.")
    c0 = containers[0]
    c0_number = _text(c0.get("containerNumber"))
    # 4. events, newest first (payload order)
    events = c0.get("events") or []
    # 5. customs-hold rule (parity; MSC-D5: isDestinationReached None counts as False, events[0] only if present)
    if (c0.get("isDestinationReached") is True and events
            and _text(events[0].get("locationCountry")).upper() == "US"):
        raise CustomsHoldError(
            f"Possible customs hold for MSC booking {booking}: destination reached but latest event is in the US "
            f"({events[0].get('eventName')!r} at {events[0].get('locationName')!r})")
    # 6. no events (MSC-D4)
    if not events:
        raise NoEtaError(f"MSC returned no events for container {c0.get('containerNumber')} of booking {booking}. "
                         "Can't determine ETA.")
    # 7. the original selection
    sel = select_eta(events, now=now, name_key="eventName", date_of=_graphql_date_of)
    # 8. nothing usable → rescue (MSC-D10, off by default) or NoEtaError
    rescued = False
    if sel is None:
        sel = _graphql_rescue(c0) if rescue else None
        if sel is None:
            raise NoEtaError(no_match_message(booking))
        rescued = True
        log.warning("MSC %s: no ETA event; using the rescue fallback containers[0].finalEta = %s "
                    "(MSC_RESCUE_FALLBACK=true)", booking, sel.eta.date().isoformat())
    # 9. multi-container check, log only (MSC-D7)
    others = [(_container_label(cn, i), _graphql_container_eta(cn, now))
              for i, cn in enumerate(containers[1:], start=1)]
    other_etas = _other_container_etas(booking, c0_number or "containers[0]", sel.eta, others)
    # 10. result (06 §7.4)
    details: dict[str, Any] = {"mode": "mymsc", "event_name": sel.event_name, "raw_date": sel.raw_date,
                               "container": c0_number}
    if other_etas:
        details["other_container_etas"] = other_etas
    if rescued:
        details["rescue"] = True
    return EtaResult(
        carrier=Carrier.MSC,
        booking=booking,
        eta=sel.eta,
        source=RESCUE_SOURCE_MYMSC if rescued else _selection_source(sel, api=False),
        is_actual=sel.is_actual,
        from_transshipment_rule=sel.from_transshipment_rule,
        containers=(c0_number,) if c0_number else (),
        details=details,
    )


# ════════════════════════════════════════════════════════════════════════════════════════════════
# api mode: the same rules mapped onto DCSA events (06 §7.2–7.3, MSC-D9) — pure
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _ci(mapping: Mapping[Any, Any]) -> dict[str, Any]:
    """Case-insensitive view (unLocationCode == UNLocationCode); the first non-blank value of a key wins."""
    out: dict[str, Any] = {}
    for key, value in mapping.items():
        name = str(key).casefold()
        if name not in out or (_is_blank(out[name]) and not _is_blank(value)):
            out[name] = value
    return out


def _ci_child(mapping: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = mapping.get(key)
    return _ci(value) if isinstance(value, Mapping) else {}


def _api_event_list(payload: Any, booking: str) -> list[Any]:
    """06 §7.2 step 1: a bare array (sandbox, OpenAPI example) or {"events": [...]} (OpenAPI schema)."""
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, Mapping) and isinstance(payload.get("events"), list):
        raw = payload["events"]
    else:
        raise CarrierLookupError(f"Unexpected MSC API response shape for {booking}")
    if not raw:
        raise CarrierLookupError(f"MSC API returned no events for booking {booking} — booking unknown to MSC "
                                 "or not linked to your account")
    return raw


def _api_record(item: Any, index: int) -> dict[str, Any] | None:
    """06 §7.2 step 2: one DCSA event → a small MSC-specific record (no dependency on dcsa.py)."""
    if not isinstance(item, Mapping):
        log.debug("[MSC API] skipped event #%d: not an object", index)
        return None
    m = _ci(item)
    etype = _text(m.get("eventtype")).upper()
    code = _text(m.get(_TYPE_CODE_KEYS[etype])).upper() if etype in _TYPE_CODE_KEYS else ""
    if not code:
        for inferred, key in _TYPE_CODE_KEYS.items():  # missing eventType: infer it from the type-code key present
            code = _text(m.get(key)).upper()
            if code:
                etype = etype or inferred
                break
    if not code:
        code = _text(m.get("eventtypecode")).upper()   # deprecated DCSA alias
    raw_when = m.get("eventdatetime")
    try:
        when = parse_wall_datetime(raw_when)
    except ValueError:
        log.debug("[MSC API] skipped event #%d: unreadable eventDateTime %r", index, raw_when)
        return None
    if not _plausible(when):   # .NET sentinels 0001-01-01 / 9999-12-31 (review fix MSK-02, as 09 §A.3.4 step 3)
        log.debug("[MSC API] skipped event #%d: eventDateTime year %d outside %s", index, when.year, _YEARS_TEXT)
        return None
    tc = _ci_child(m, "transportcall")
    unloc = ""
    for candidate in (tc.get("unlocationcode"), _ci_child(m, "eventlocation").get("unlocationcode"),
                      _ci_child(tc, "location").get("unlocationcode")):
        unloc = _text(candidate)
        if unloc:
            break
    return {
        "etype": etype,
        "code": code or None,
        "classifier": _text(m.get("eventclassifiercode")).upper() or None,
        "name": _text(m.get("description")),
        "when": when,
        "unloc": unloc.upper() or None,
        "equipment": _text(m.get("equipmentreference")).upper() or None,
        "eventDateTime": raw_when if isinstance(raw_when, str) else "",
        "index": index,
    }


def _api_records(raw: Iterable[Any]) -> list[dict[str, Any]]:
    return [rec for index, item in enumerate(raw) if (rec := _api_record(item, index)) is not None]


def _timeline(records: Sequence[Mapping[str, Any]], container: str | None) -> list[Mapping[str, Any]]:
    """TRANSPORT events + this container's EQUIPMENT events, newest first (stable). No container → all events."""
    if container is None:
        chosen = list(records)
    else:
        chosen = [r for r in records
                  if r["etype"] == "TRANSPORT" or (r["etype"] == "EQUIPMENT" and r["equipment"] == container)]
    return sorted(chosen, key=lambda r: r["when"], reverse=True)


def _api_date_of(ev: Mapping[str, Any]) -> datetime:
    """Truncated to midnight: parity with mymsc, whose MSC dates are date-only (06 §7.2 step 5)."""
    return datetime.combine(ev["when"].date(), dtime())


def _api_container_eta(records: Sequence[Mapping[str, Any]], container: str, now: datetime) -> datetime | None:
    try:
        sel = select_eta(_timeline(records, container), now=now, name_key="name", date_of=_api_date_of)
    except (NoEtaError, ValueError, OverflowError):   # log only: never fails the lookup (MSK-04)
        return None
    return sel.eta if sel is not None else None


def _check_api_customs_hold(records: Sequence[Mapping[str, Any]], timeline0: Sequence[Mapping[str, Any]],
                            c0: str | None, booking: str) -> None:
    """06 §7.3 (INFERRED approximation, verify with `--compare`): the vessel's newest ARRI is ACT, but the first
    container's newest EQUIPMENT event is in the US → the box probably never sailed (export/customs hold)."""
    arri = [r for r in records if r["etype"] == "TRANSPORT" and r["code"] == "ARRI"]
    if not arri:
        return
    pod_event = max(arri, key=lambda r: r["when"])
    if pod_event["classifier"] != "ACT":
        return
    latest_equipment = next((r for r in timeline0 if r["etype"] == "EQUIPMENT"), None)
    if latest_equipment is not None and (latest_equipment["unloc"] or "")[:2] == "US":
        container = c0 or latest_equipment["equipment"] or "?"
        raise CustomsHoldError(
            f"Possible customs hold for MSC booking {booking}: vessel arrived at {pod_event['unloc']} but container "
            f"{container}'s latest event is in the US ({latest_equipment['name']!r})")


def _api_rescue(payload: Any, *, booking: str, c0: str | None) -> EtaResult | None:
    """06 §7.5 (api): dcsa.booking_arrival(dcsa.parse_events(payload), policy="first") → midnight. None if it fails."""
    try:
        arrival = dcsa.booking_arrival(dcsa.parse_events(payload), policy="first")
    except (ValueError, LookupError, TypeError, AttributeError) as exc:
        log.debug("MSC %s: DCSA rescue failed: %r", booking, exc)
        return None
    if arrival is None:
        return None
    container = arrival.equipment_reference or c0
    base = dcsa.to_eta_result(arrival, carrier=Carrier.MSC, booking=booking,
                              containers=(container,) if container else ())
    details = {**dict(base.details), "mode": "api", "event_name": arrival.basis,
               "raw_date": arrival.eta.isoformat(), "container": container,
               "event_datetime": arrival.eta.isoformat(), "rescue": True}
    return dataclasses.replace(base, eta=datetime.combine(arrival.eta.date(), dtime()), source=RESCUE_SOURCE_API,
                               details=details)


def eta_from_api(payload: Any, *, booking: str, now: datetime, customs_rule: bool = True,
                 rescue: bool = False) -> EtaResult:
    """Official DCSA T&T v2.2 events → EtaResult with the mymsc rules mapped onto DCSA events (06 §7.2, MSC-D9).

    Events are matched by MSC's `description` (myMSC vocabulary), re-sorted newest first per container; the first
    container is the alphabetical first (MSC-D9); the ETA is truncated to midnight (event_datetime keeps the time).
    """
    # 1–2. normalise the payload and the events
    raw = _api_event_list(payload, booking)
    records = _api_records(raw)
    if not records:   # every event was skipped (no readable eventDateTime): say so instead of "no event"
        raise NoEtaError(f"MSC API returned {len(raw)} event(s) for booking {booking} but none has a readable "
                         "eventDateTime. Can't determine ETA.")
    # 3. containers (MSC-D9: alphabetical, deterministic) and the first container's timeline
    containers = sorted({r["equipment"] for r in records if r["etype"] == "EQUIPMENT" and r["equipment"]})
    c0 = containers[0] if containers else None
    timeline0 = _timeline(records, c0)
    # 4. customs-hold approximation (06 §7.3)
    if customs_rule:
        _check_api_customs_hold(records, timeline0, c0, booking)
    # 5. the original selection on the first container's timeline
    sel = select_eta(timeline0, now=now, name_key="name", date_of=_api_date_of)
    # 6. nothing usable → rescue (MSC-D10, off by default) or NoEtaError
    if sel is None:
        rescued = _api_rescue(payload, booking=booking, c0=c0) if rescue else None
        if rescued is None:
            raise NoEtaError(no_match_message(booking))
        log.warning("MSC %s: no ETA event; using the rescue fallback %s = %s (MSC_RESCUE_FALLBACK=true)",
                    booking, RESCUE_SOURCE_API, rescued.eta.date().isoformat())
        chosen = rescued.details.get("container")
        others = [(c, _api_container_eta(records, c, now)) for c in containers if c != chosen]
        other_etas = _other_container_etas(booking, chosen or "?", rescued.eta, others)
        if other_etas:
            rescued = dataclasses.replace(rescued,
                                          details={**dict(rescued.details), "other_container_etas": other_etas})
        return rescued
    matched = sel.event or {}
    # 06 §7.2 step 5: an ACT match counts as actual — but not for the transshipment rule, whose +7-day ETA is an
    # estimate even though the LOAD it is based on is ACT (assumption, see the stage report).
    is_actual = sel.is_actual or (not sel.from_transshipment_rule and matched.get("classifier") == "ACT")
    # 7. multi-container check, log only (MSC-D7)
    others = [(c, _api_container_eta(records, c, now)) for c in containers[1:]]
    other_etas = _other_container_etas(booking, c0 or "?", sel.eta, others)
    # 8. result (06 §7.4)
    when = matched.get("when")
    details: dict[str, Any] = {"mode": "api", "event_name": sel.event_name, "raw_date": sel.raw_date,
                               "container": c0,
                               "event_datetime": when.isoformat() if isinstance(when, datetime) else None}
    if other_etas:
        details["other_container_etas"] = other_etas
    return EtaResult(
        carrier=Carrier.MSC,
        booking=booking,
        eta=sel.eta,
        source=_selection_source(sel, api=True),
        is_actual=is_actual,
        from_transshipment_rule=sel.from_transshipment_rule,
        containers=(c0,) if c0 else (),
        details=details,
    )


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Edge blocks (06 §6.5) — detection only, never circumvention
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _is_edge_block_text(text: str | None) -> bool:
    """Akamai's standard denial page: "Access Denied" and ("edgesuite.net" or "Reference #").
    HTML entities are decoded first: the real page writes "Reference&#32;&#35;18&#46;…"."""
    if not text:
        return False
    plain = html.unescape(text)
    return "Access Denied" in plain and ("edgesuite.net" in plain or "Reference #" in plain)


def _response_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 - undecodable body: detection/excerpts are best-effort
        return ""


def _is_edge_block_response(resp: httpx.Response) -> bool:
    """_raise_if_edge_block's test (06 §6.5): status 403 and the Akamai text."""
    return resp.status_code == 403 and _is_edge_block_text(_response_text(resp))


def _api_error_message(resp: httpx.Response) -> str:
    """06 §6.4: the first errors[].message, else message, else the first 200 chars of the text (redacted, 1 line)."""
    text = _response_text(resp)
    message: str | None = None
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, Mapping):
        errors = body.get("errors")
        if isinstance(errors, list):
            for err in errors:
                if isinstance(err, Mapping) and isinstance(err.get("message"), str) and err["message"].strip():
                    message = err["message"]
                    break
        if message is None and isinstance(body.get("message"), str) and body["message"].strip():
            message = body["message"]
    if message is None:
        message = text[:200]
    cleaned = " ".join(str(redact(message)).split())
    return cleaned or "(empty response body)"


def _report_body(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return _response_text(resp)[:2000]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Bridge token (06 §5.1)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def normalize_token(token: str) -> str:
    """06 §5.1 set_manual_token steps 1–2: strip whitespace and a leading "Bearer " (case-insensitive; a pasted
    "Authorization:" header name is tolerated too). Empty or whitespace-containing → ConfigError (never echoes it).
    Invisible or non-ASCII characters (U+200B, a BOM, '…', control characters) → ConfigError too (review fix MSK-01):
    httpx can send only ASCII header values, so such a token would fail every lookup."""
    if not isinstance(token, str):
        raise ConfigError(EMPTY_TOKEN_MESSAGE)
    value = _AUTH_PREFIX_RE.sub("", token.strip(), count=1).strip()
    if not value:
        raise ConfigError(EMPTY_TOKEN_MESSAGE)
    if any(ch.isspace() for ch in value):
        raise ConfigError(SPACED_TOKEN_MESSAGE)
    if not value.isascii() or not value.isprintable():
        raise ConfigError(UNPRINTABLE_TOKEN_MESSAGE)
    return value


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _jwt_claims(token: str) -> dict[str, Any]:
    """Only iss/aud/exp/iat of a JWT payload (no signature verification) — never sub, e-mail or name (06 §5.1)."""
    parts = token.strip().split(".") if isinstance(token, str) else []
    if len(parts) != 3 or not parts[0] or not parts[1]:
        return {}
    segment = parts[1]
    try:
        claims = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
    except (ValueError, binascii.Error):  # includes JSONDecodeError and UnicodeDecodeError
        return {}
    if not isinstance(claims, dict):
        return {}
    out: dict[str, Any] = {}
    for name in _ALLOWED_JWT_CLAIMS:
        value = claims.get(name)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, str):
            out[name] = value
        elif isinstance(value, (int, float)) and math.isfinite(value):
            out[name] = value
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            out[name] = list(value)
    return out


class MscBridgeAuth:
    """TokenProvider for the myMSC bridge (06 §5.1, 03 §8): the token is pasted by the user — never obtained by an
    automated sign-in (06 §1). TokenStore entry "msc":
    {"access_token", "expires_at", "obtained_at", "source": "manual", "mode": "mymsc"} (MSC-D3).

    There is no automatic re-acquisition: a missing token, or one rejected with HTTP 401, sets a fatal AuthError
    that every later get_token() raises (MSC-D8). `epoch` (Unix seconds) is injectable for tests.
    """

    def __init__(self, settings: MscSettings, token_store: TokenStore, *,
                 epoch: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._store = token_store
        self._cache = TokenCache(token_store, TOKEN_KEY, label="MSC")   # best-effort writes (MSK-05)
        self._epoch = epoch
        self._lock = asyncio.Lock()
        self._rejected = False                 # MSC_ACCESS_TOKEN was rejected (HTTP 401) in this process
        self._fatal: AuthError | None = None
        self._env_warned = False

    async def get_token(self) -> str:
        """06 §5.1 steps 1–4, under an asyncio.Lock."""
        async with self._lock:
            if self._fatal is not None:  # 1. latched: same message every time
                raise AuthError(str(self._fatal)) from self._fatal
            now = self._epoch()
            entry = self._cache.get()
            if entry is not None and entry.get("mode") == "mymsc" and is_fresh(entry, now=now):   # 2.
                return str(entry["access_token"])
            env_token = self._env_token()
            if env_token is not None:                                                            # 3.
                return str(self._store_token(env_token, now=now)["access_token"])
            self._fatal = AuthError(NO_TOKEN_MESSAGE)                                            # 4.
            raise self._fatal

    async def invalidate(self) -> None:
        """Called by request_with_auth after a 401: latch the fatal error (no re-acquisition), then drop the token.
        The latch comes first (review fix MSK-05): a tokens.json that cannot be rewritten must not keep the rejected
        token in use."""
        async with self._lock:
            self._rejected = True
            self._fatal = AuthError(TOKEN_REJECTED_MESSAGE)
            self._cache.delete()

    def set_manual_token(self, token: str, *, strict: bool = True) -> dict[str, Any]:
        """`eta msc-token` (06 §5.1): normalise, store with its expiry, clear the rejection; returns token_status().

        Also clears a fatal error latched earlier in this process: a freshly pasted token is a new state. `strict`
        (the default, `eta msc-token`) raises OSError when tokens.json cannot be written, so the command never says
        "stored" falsely; a run's pre-flight passes strict=False and keeps the token in memory (MSK-05).
        """
        value = normalize_token(token)
        now = self._epoch()
        entry = self._store_token(value, now=now, strict=strict)
        self._rejected = False
        self._fatal = None
        if entry["expires_at"] <= now:
            log.warning("The MSC access token you pasted has already expired (its JWT exp is in the past) — "
                        "copy a fresh one from your signed-in myMSC session")
        return self.token_status()

    def token_status(self) -> dict[str, Any]:
        """No network. {"mode": "mymsc", "has_token", "source", "expires_at", "seconds_left", "expired", "jwt_claims"}.

        Mirrors get_token(): a fresh stored token, else MSC_ACCESS_TOKEN (as it would be stored now), else a stale
        stored token (flagged by a negative seconds_left and expired=True). jwt_claims holds only iss/aud/exp/iat.
        """
        now = self._epoch()
        token, entry = self._current(now)
        if token is None:
            return {"mode": "mymsc", "has_token": False, "source": None, "expires_at": None, "seconds_left": None,
                    "expired": False, "jwt_claims": {}}
        expires_at = _finite_float(entry.get("expires_at"))
        seconds_left = math.floor(expires_at - now) if expires_at is not None else None
        return {
            "mode": "mymsc",
            "has_token": True,
            "source": entry.get("source") or "manual",
            "expires_at": expires_at,
            "seconds_left": seconds_left,
            "expired": seconds_left is not None and seconds_left <= 0,
            "jwt_claims": _jwt_claims(token),
        }

    # ── internals ──
    def _env_token(self) -> str | None:
        """MSC_ACCESS_TOKEN, unless it was rejected in this process (06 §5.1 step 3)."""
        raw = self._settings.manual_token
        if not raw or self._rejected:
            return None
        try:
            return normalize_token(raw)
        except ConfigError as exc:
            if not self._env_warned:
                self._env_warned = True
                log.warning("MSC_ACCESS_TOKEN is set but is not a usable token (%s) — ignored", exc)
            return None

    def _expires_at(self, token: str, now: float) -> float:
        """JWT exp when readable (03 §7), else obtained_at + MSC_TOKEN_TTL_SECONDS."""
        exp = jwt_exp(token)
        return float(exp) if exp is not None else float(now) + float(self._settings.token_ttl_seconds)

    def _store_token(self, token: str, *, now: float, strict: bool = False) -> dict[str, Any]:
        entry = {"access_token": token, "expires_at": self._expires_at(token, now), "obtained_at": float(now),
                 "source": "manual", "mode": "mymsc"}
        self._cache.put(entry, strict=strict)
        return entry

    def _current(self, now: float) -> tuple[str | None, Mapping[str, Any]]:
        entry = self._cache.get()
        stored = (entry if entry is not None and entry.get("mode") == "mymsc"
                  and isinstance(entry.get("access_token"), str) and entry["access_token"] else None)
        if stored is not None and is_fresh(stored, now=now):
            return str(stored["access_token"]), stored
        env_token = self._env_token()
        if env_token is not None:
            return env_token, {"expires_at": self._expires_at(env_token, now), "source": "manual"}
        if stored is not None:
            return str(stored["access_token"]), stored
        return None, {}


# ════════════════════════════════════════════════════════════════════════════════════════════════
# The client (06 §6)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _host_of(url: str | None) -> str:
    return (urlsplit(url or "").hostname or "?") if url else "?"


class MscClient:
    """CarrierClient for MSC (03 §9, 06 §6). One instance per run; mode from settings.mode ("mymsc" | "api").

    `clock` should be FixedClock(run_now) so the transshipment rule uses the run's single `now` (03 §3).
    `allow_sandbox` overrides MSC_ALLOW_SANDBOX (`eta probe msc --sandbox` passes True). `epoch` feeds the bridge
    token's expiry checks (tests).
    """

    carrier = Carrier.MSC

    def __init__(self, settings: MscSettings, token_store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None,
                 allow_sandbox: bool | None = None, epoch: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._http = http if http is not None else new_async_client(
            timeout=httpx.Timeout(settings.timeout_seconds, connect=CONNECT_TIMEOUT_S))
        self._owns_http = http is None
        self._clock: Clock = clock or SystemClock()
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrency))   # 03 §10 (config enforces >= 1)
        rate = settings.rate_per_minute if settings.mode == "mymsc" else settings.api_rate_per_minute
        self._limiter = AsyncLimiter(rate, 60) if rate > 0 else None   # 0 = off (AsyncLimiter needs >= 1)
        self._auth = MscBridgeAuth(settings, token_store, epoch=epoch) if settings.mode == "mymsc" else None
        self._fatal: CarrierLookupError | None = None
        self._fatal_kind: LatchKind | None = None
        self._allow_sandbox = settings.allow_sandbox if allow_sandbox is None else allow_sandbox
        self._email_warned = False

    # ── public surface ──
    @property
    def settings(self) -> MscSettings:
        return self._settings

    @property
    def mode(self) -> str:
        return self._settings.mode

    @property
    def auth(self) -> MscBridgeAuth | None:
        """The bridge TokenProvider (mymsc mode) or None (api mode)."""
        return self._auth

    @property
    def latched(self) -> str | None:
        """The fatal message that stops every MSC lookup for this run, or None."""
        return str(self._fatal) if self._fatal is not None else None

    async def get_eta(self, booking: str) -> EtaResult:
        """ETA for a base booking (06 §6.4). Raises only CarrierLookupError / NoEtaError / CustomsHoldError."""
        result = await self._lookup(booking)
        log.info("MSC %s: ETA %s via %s", booking, f"{result.eta:%Y-%m-%d}", result.source)
        return result

    async def probe(self, booking: str) -> dict[str, Any]:
        """Diagnostics (03 §9, 06 §6.4): the same request(s) as get_eta, never raising for lookup errors.

        JSON-safe: {"service", "mode", "booking", "requests": [{"method", "url" (redacted), "status",
        "elapsed_ms"}], "raw": redacted payload, "eta": EtaResult.to_dict() or "<ErrorClass>: <message>",
        "error": None or that text, "bridge" (mymsc: token_status()) or "api" (api: host info)}; api mode adds
        "event_count" and "explain" (09 §A.3.11) when the payload is an event list. (The status is not stored
        under a key named "token": redact() would mask the whole entry when the output is saved.)
        """
        out: dict[str, Any] = {"service": SERVICE, "mode": self._settings.mode, "booking": booking,
                               "requests": [], "raw": None, "eta": None, "error": None}
        out["bridge" if self._auth is not None else "api"] = self.token_status()
        requests: list[dict[str, Any]] = out["requests"]
        pending: dict[int, tuple[float, httpx.Request]] = {}

        async def on_request(req: httpx.Request) -> None:
            pending[id(req)] = (time.perf_counter(), req)

        async def on_response(resp: httpx.Response) -> None:
            started, _ = pending.pop(id(resp.request), (None, None))
            requests.append(_request_summary(resp.request, resp.status_code, started))

        hooks = self._http.event_hooks
        hooks["request"].append(on_request)
        hooks["response"].append(on_response)
        try:
            result = await self._lookup(booking, capture=out)
            out["eta"] = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - a probe reports every failure instead of raising (06 §6.4)
            out["eta"] = out["error"] = error_text(exc)
        finally:
            _remove_hook(hooks["request"], on_request)
            _remove_hook(hooks["response"], on_response)
            for started, req in pending.values():   # transport failures: no response
                requests.append(_request_summary(req, None, started))
        return out

    def token_status(self) -> dict[str, Any]:
        """mymsc: MscBridgeAuth.token_status(); api: {"mode": "api", "host", "sandbox", "has_key"}. No network."""
        if self._auth is not None:
            return self._auth.token_status()
        s = self._settings
        return {"mode": "api", "host": urlsplit(s.api_base_url).hostname if s.api_base_url else None,
                "sandbox": s.api_is_sandbox, "has_key": bool(s.api_key)}

    def set_manual_token(self, token: str) -> dict[str, Any]:
        """Store a pasted bridge token (mymsc only; ConfigError in api mode). Clears a token-related latch."""
        if self._auth is None:
            raise ConfigError("MSC runs in api mode (official MSC API): no bridge token is used. Use "
                              "store_manual_token() / `eta msc-token` only for the mymsc bridge or `--compare`.")
        status = self._auth.set_manual_token(token, strict=False)   # the run uses it even if it cannot be saved
        if self._fatal_kind == "auth":
            self._fatal = None
            self._fatal_kind = None
        return status

    async def aclose(self) -> None:
        """Close the httpx client if this instance created it (an injected one is left to its owner)."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> MscClient:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.aclose()

    # ── internals ──
    def _raise_if_latched(self) -> None:
        """06 §6.4 step 1: the same message every time, so the run summary can group the failures."""
        if self._fatal is not None:
            raise CarrierLookupError(str(self._fatal)) from self._fatal

    def _latch(self, message: str, kind: LatchKind, cause: BaseException | None = None) -> NoReturn:
        """Set the fatal error that stops MSC lookups for the run (06 §5.2, §6.4, §6.5, MSC-D8) and raise it."""
        first = self._fatal is None
        self._fatal = CarrierLookupError(message)
        self._fatal_kind = kind
        if first:
            log.warning("MSC lookups stopped for this run: %s", message)
        raise self._fatal from cause

    async def _lookup(self, booking: str, capture: dict[str, Any] | None = None) -> EtaResult:
        self._raise_if_latched()
        now = self._clock.now()
        try:
            if self._settings.mode == "api":
                return await self._lookup_api(booking, now, capture)
            return await self._lookup_mymsc(booking, now, capture)
        except httpx.HTTPError as exc:  # 03 §5 safety net: request() already maps transport errors
            raise CarrierLookupError(f"MSC request failed for {booking}: {exc!r}") from exc
        except OSError as exc:          # 03 §5 safety net (MSK-05), e.g. a local file error
            raise CarrierLookupError(f"MSC lookup failed for {booking}: {exc!r}") from exc
        except UnicodeError as exc:
            # Review fix MSK-01 (03 §5): httpx encodes header values as ASCII before sending anything, so a
            # non-ASCII setting or token fails every lookup the same way — latch it, naming the culprit.
            culprit = self._unsendable_header()
            if culprit is None:
                raise CarrierLookupError(f"MSC request failed for {booking}: {exc!r}") from exc
            self._latch(*culprit, exc)

    def _unsendable_header(self) -> tuple[str, LatchKind] | None:
        """MSK-01: the value that cannot be sent in an HTTP header (ASCII only), as (message, latch kind). The other
        headers are constants, so in mymsc mode it is MSC_USER_EMAIL / MSC_USER_ROLES, else the bridge token."""
        s = self._settings
        settings: tuple[tuple[str, str | None, str], ...] = (
            (("MSC_API_SUBSCRIPTION_KEY", s.api_key, "copy the key again into .env"),) if s.mode == "api" else (
                ("MSC_USER_EMAIL", s.user_email,
                 "set it in .env to your myMSC sign-in e-mail (it defaults to VSHIP_USER_NAME)"),
                ("MSC_USER_ROLES", s.user_roles, "correct it in .env")))
        for name, value, hint in settings:
            if value and not value.isascii():
                message = (f"{name} contains non-ASCII characters, which cannot be sent in an HTTP header: {hint} "
                           "(06-carrier-msc §3.2).")
                return message, "config"
        if s.mode == "api":
            return None
        return STORED_TOKEN_UNSENDABLE_MESSAGE, "auth"

    def _warn_if_no_user_email(self) -> None:
        """06 §3.2: an empty MSC_USER_EMAIL omits the header; WARNING logged once."""
        if not self._settings.user_email and not self._email_warned:
            self._email_warned = True
            log.warning("MSC_USER_EMAIL is empty (and VSHIP_USER_NAME / MSC_USER_NAME are not set): the "
                        "'mymsc-user-email' header is omitted. If myMSC returns no result for valid bookings, set "
                        "MSC_USER_EMAIL to your myMSC sign-in e-mail (06-carrier-msc §3.2).")

    def _graphql_headers(self) -> dict[str, str]:
        """06 §4.1 headers (Authorization is added by request_with_auth). MSC-D2: no browser-imitation headers."""
        s = self._settings
        headers = {"Content-Type": "application/json", "Accept": "application/json", "Referer": MYMSC_REFERER}
        if s.user_email:
            headers["mymsc-user-email"] = s.user_email
        if s.user_roles:
            headers["mymsc-user-roles"] = s.user_roles
        return headers

    def _api_headers(self) -> dict[str, str]:
        """06 §4.2: the subscription key header, omitted only when there is no key (public sandbox)."""
        headers = {"Accept": "application/json"}
        if self._settings.api_key:
            headers[API_KEY_HEADER] = self._settings.api_key
        return headers

    async def _lookup_mymsc(self, booking: str, now: datetime, capture: dict[str, Any] | None) -> EtaResult:
        """06 §6.4, mymsc mode."""
        s = self._settings
        auth = self._auth
        if auth is None:  # unreachable: the bridge auth exists whenever mode == "mymsc"
            raise CarrierLookupError("MSC bridge mode has no token provider")
        self._warn_if_no_user_email()
        try:
            async with self._sem:
                resp = await request_with_auth(
                    self._http, "POST", s.graphql_url, service=SERVICE, auth=auth, build_headers=bearer_headers,
                    headers=self._graphql_headers(), json=graphql_body(booking), expected=(200,),
                    idempotent=True, limiter=self._limiter)
            if _is_edge_block_response(resp):   # 06 §6.5 (also applied to ServiceError bodies below)
                self._latch(EDGE_BLOCK_MESSAGE, "edge")
            body = json_body(resp, service=SERVICE)
        except AuthError as exc:   # missing / expired / rejected token (MSC-D8)
            self._latch(str(exc), "auth", exc)
        except ServiceError as exc:
            if _is_edge_block_text(exc.body_excerpt):
                self._latch(EDGE_BLOCK_MESSAGE, "edge", exc)
            raise CarrierLookupError(f"MSC tracking request failed for {booking}: {exc}") from exc
        if capture is not None:
            capture["raw"] = redact(body)
        return _wrap_parse(lambda: eta_from_graphql(body, booking=booking, now=now, rescue=s.rescue_fallback),
                           booking)

    async def _lookup_api(self, booking: str, now: datetime, capture: dict[str, Any] | None) -> EtaResult:
        """06 §6.4, api mode (status handling per §5.2 and §6.4 step 3)."""
        s = self._settings
        if s.api_is_sandbox and not self._allow_sandbox:   # 1. never use mock ETAs for real bookings
            raise CarrierLookupError(SANDBOX_REFUSAL)
        if not s.api_base_url:   # config prevents this (Settings.msc is None); kept for hand-built settings
            raise CarrierLookupError("MSC api mode needs MSC_API_BASE_URL (06-carrier-msc §3.2)")
        try:                                               # 2. 429/5xx are retried by request() (03 §6)
            async with self._sem:
                resp = await request(
                    self._http, "GET", f"{s.api_base_url}/events", service=SERVICE,
                    params={"carrierBookingReference": booking}, headers=self._api_headers(),
                    expected=API_EXPECTED_STATUSES, limiter=self._limiter)
        except ServiceError as exc:
            if _is_edge_block_text(exc.body_excerpt):
                self._latch(EDGE_BLOCK_MESSAGE, "edge", exc)
            raise CarrierLookupError(f"MSC API unavailable/rate-limited for {booking}: {exc}") from exc
        status = resp.status_code                          # 3.
        if status == 200:
            try:
                payload = json_body(resp, service=SERVICE)
            except ServiceError as exc:
                raise CarrierLookupError(f"MSC API returned an unreadable response for {booking}: {exc}") from exc
            if capture is not None:
                _capture_api_payload(capture, payload)
            return _wrap_parse(lambda: eta_from_api(payload, booking=booking, now=now,
                                                    customs_rule=s.api_customs_hold_rule,
                                                    rescue=s.rescue_fallback), booking)
        if capture is not None:
            capture["raw"] = redact(_report_body(resp))
        if _is_edge_block_response(resp):   # 06 §6.5 applies to both modes
            self._latch(EDGE_BLOCK_MESSAGE, "edge")
        message = _api_error_message(resp)
        if status in (401, 403):            # 06 §5.2 fatal latches
            if "quota" in message.lower():
                self._latch(f"MSC API call quota exhausted: {message}", "api")
            self._latch(f"MSC API rejected the subscription key (HTTP {status}): {message.rstrip('. ')}. "
                        "Check MSC_API_SUBSCRIPTION_KEY and that the subscription is approved/active.", "api")
        if status == 404:                   # not latched
            raise CarrierLookupError(f"MSC API has no events for booking {booking} (HTTP 404): {message} — the "
                                     "booking may be unknown to MSC or not linked to your account")
        text = f"MSC API rejected the request for {booking} (HTTP {status}): {message}"   # 400, not latched
        if _REFERENCE_HINT_RE.search(message):
            text += _REFERENCE_HINT
        raise CarrierLookupError(text)


def _request_summary(req: httpx.Request, status: int | None, started: float | None) -> dict[str, Any]:
    return {"method": req.method, "url": redact(str(req.url)), "status": status,
            "elapsed_ms": round((time.perf_counter() - started) * 1000) if started is not None else None}


def _remove_hook(hooks: list[Any], hook: Any) -> None:
    try:
        hooks.remove(hook)
    except ValueError:
        pass


def _capture_api_payload(capture: dict[str, Any], payload: Any) -> None:
    """Probe extras for api mode: redacted payload, event count and the DCSA selection explanation (09 §A.3.11)."""
    capture["raw"] = redact(payload)
    events = payload if isinstance(payload, list) else (
        payload.get("events") if isinstance(payload, Mapping) else None)
    if isinstance(events, list):
        capture["event_count"] = len(events)
        try:
            capture["explain"] = dcsa.explain_selection(dcsa.parse_events(events), policy="first")
        except (ValueError, LookupError, TypeError, AttributeError) as exc:
            capture["explain"] = [f"explain_selection failed: {exc!r}"]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Factory (03 §9)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def build_client(settings: Settings, token_store: TokenStore, *, clock: Clock | None = None,
                 http: httpx.AsyncClient | None = None) -> MscClient | None:
    """None if settings.msc is None. Logs once at INFO: 'MSC mode: mymsc (token bridge)' or 'MSC mode: api (<host>)'."""
    s = settings.msc
    if s is None:
        return None
    if s.mode == "api":
        log.info("MSC mode: api (%s)", _host_of(s.api_base_url))
    else:
        log.info("MSC mode: mymsc (token bridge)")
    return MscClient(s, token_store, http=http, clock=clock)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Helpers for the CLI: `eta msc-token`, `eta probe msc --token-info/--sandbox/--booking/--compare/--save-raw`
# and the `eta run` token pre-flight (06 §5.1, §10, §12; 10 Phase 1; 11)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def bridge_settings(settings: MscSettings) -> MscSettings:
    """The same settings in mymsc mode (the bridge side of `--compare`, `eta msc-token` in any mode)."""
    return settings if settings.mode == "mymsc" else dataclasses.replace(settings, mode="mymsc")


def store_manual_token(settings: MscSettings, token_store: TokenStore, token: str, *,
                       epoch: Callable[[], float] = time.time) -> dict[str, Any]:
    """`eta msc-token`: validate and store a pasted bridge token whatever MSC_MODE is (`--compare` needs one in
    api mode too); returns the bridge token_status(). ConfigError for an empty or whitespace-containing value."""
    return MscBridgeAuth(bridge_settings(settings), token_store, epoch=epoch).set_manual_token(token)


def bridge_token_status(settings: MscSettings, token_store: TokenStore, *,
                        epoch: Callable[[], float] = time.time) -> dict[str, Any]:
    """The bridge token's status whatever MSC_MODE is (no network)."""
    return MscBridgeAuth(bridge_settings(settings), token_store, epoch=epoch).token_status()


def token_needs_refresh(status: Mapping[str, Any], *, min_seconds: int = PREFLIGHT_MIN_SECONDS) -> bool:
    """Pre-flight test (06 §5.1, 10 Phase 1): True in mymsc mode when there is no token, its expiry is unknown, or
    it has fewer than `min_seconds` left. The caller prompts on a TTY (getpass) or fails with NO_TOKEN_MESSAGE."""
    if status.get("mode") != "mymsc":
        return False
    left = status.get("seconds_left")
    if not status.get("has_token") or isinstance(left, bool) or not isinstance(left, (int, float)):
        return True
    return left < min_seconds


def sandbox_settings(base: MscSettings | None = None) -> MscSettings:
    """api-mode settings aimed at the public mock sandbox with no key (`eta probe msc --sandbox`, 06 §10)."""
    template = base if base is not None else MscSettings(mode="api")
    return dataclasses.replace(template, mode="api", api_base_url=SANDBOX_BASE_URL, api_key=None,
                               allow_sandbox=True, api_rate_per_minute=0)


async def probe_sandbox(token_store: TokenStore, *, settings: MscSettings | None = None,
                        booking: str = SANDBOX_PROBE_BOOKING, http: httpx.AsyncClient | None = None,
                        clock: Clock | None = None) -> dict[str, Any]:
    """`eta probe msc --sandbox`: needs no credentials (06 §10). The probe() dict plus "sandbox": True,
    "expected_eta": "2022-12-26" and "matches_expected" (the api-mode parser end to end)."""
    client = MscClient(sandbox_settings(settings), token_store, http=http, clock=clock, allow_sandbox=True)
    try:
        out = await client.probe(booking)
    finally:
        await client.aclose()
    eta = out.get("eta")
    out["sandbox"] = True
    out["expected_eta"] = SANDBOX_EXPECTED_ETA.isoformat()
    out["matches_expected"] = isinstance(eta, Mapping) and str(eta.get("eta", ""))[:10] == out["expected_eta"]
    return out


async def _compare_side(client: MscClient, booking: str) -> dict[str, Any]:
    try:
        result = await client.get_eta(booking)
    except CustomsHoldError as exc:
        return {"outcome": "CUSTOMS HOLD", "eta": None, "source": None, "customs_hold": True,
                "rescue": False, "error": str(exc)}
    except NoEtaError as exc:
        return {"outcome": "NO ETA", "eta": None, "source": None, "customs_hold": False,
                "rescue": False, "error": error_text(exc)}
    except CarrierLookupError as exc:
        return {"outcome": "ERROR", "eta": None, "source": None, "customs_hold": False,
                "rescue": False, "error": error_text(exc)}
    day = result.eta.date().isoformat()
    return {"outcome": day, "eta": result.eta.isoformat(), "source": result.source, "customs_hold": False,
            "rescue": bool(result.details.get("rescue", False)), "error": None}


async def compare_modes(settings: MscSettings, token_store: TokenStore, bookings: Sequence[str], *,
                        http: httpx.AsyncClient | None = None, clock: Clock | None = None,
                        epoch: Callable[[], float] = time.time) -> dict[str, Any]:
    """`eta probe msc --compare` (06 §10): mymsc and api side by side for each booking.

    Needs both a bridge token and the API settings (ConfigError otherwise). Returns {"rows": [{"booking",
    "mymsc": side, "api": side, "match": "MATCH" | "DIFF"}], "matches", "diffs", "any_diff"}, where a side is
    {"outcome" (ETA date, "CUSTOMS HOLD", "NO ETA" or "ERROR"), "eta", "source", "customs_hold", "rescue",
    "error"}. MATCH = same outcome and not an error; the CLI exits 1 when any_diff.
    """
    if not settings.api_base_url or not (settings.api_key or settings.api_is_sandbox):
        raise ConfigError("`eta probe msc --compare` needs the official MSC API settings: set MSC_API_BASE_URL and "
                          "MSC_API_SUBSCRIPTION_KEY (06-carrier-msc §2.1).")
    if not bridge_token_status(settings, token_store, epoch=epoch)["has_token"]:
        raise ConfigError(NO_TOKEN_MESSAGE)
    bridge = MscClient(bridge_settings(settings), token_store, http=http, clock=clock, epoch=epoch)
    api = MscClient(dataclasses.replace(settings, mode="api"), token_store, http=http, clock=clock)
    try:
        async def row(booking: str) -> dict[str, Any]:
            mymsc_side, api_side = await asyncio.gather(_compare_side(bridge, booking), _compare_side(api, booking))
            same = mymsc_side["outcome"] == api_side["outcome"] and mymsc_side["outcome"] != "ERROR"
            return {"booking": booking, "mymsc": mymsc_side, "api": api_side, "match": "MATCH" if same else "DIFF"}

        rows = list(await asyncio.gather(*(row(b) for b in bookings)))
    finally:
        await bridge.aclose()
        await api.aclose()
    diffs = sum(1 for r in rows if r["match"] == "DIFF")
    return {"rows": rows, "matches": len(rows) - diffs, "diffs": diffs, "any_diff": diffs > 0}


def _cell(text: Any, limit: int = _SOURCE_COLUMN_MAX) -> str:
    value = " ".join(str(text if text is not None else "").split())
    return value if len(value) <= limit else value[: limit - 1] + "…"


def format_compare_table(report: Mapping[str, Any]) -> str:
    """Plain-text table of a compare_modes() report (06 §10): ETA, source and customs flag of each mode + MATCH/DIFF."""
    header = ("BOOKING", "ETA (mymsc)", "ETA (api)", "SOURCE (mymsc)", "SOURCE (api)", "CUSTOMS (mymsc)",
              "CUSTOMS (api)", "RESULT")
    table: list[tuple[str, ...]] = [header]
    for row in report.get("rows", []):
        m, a = row["mymsc"], row["api"]
        table.append((
            _cell(row["booking"]), _cell(m["outcome"]), _cell(a["outcome"]),
            _cell(m["source"] or m["error"]), _cell(a["source"] or a["error"]),
            "yes" if m["customs_hold"] else "no", "yes" if a["customs_hold"] else "no", row["match"],
        ))
    widths = [max(len(r[i]) for r in table) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(width) for cell, width in zip(r, widths)).rstrip() for r in table]
    lines.append(f"{report.get('matches', 0)} MATCH, {report.get('diffs', 0)} DIFF")
    return "\n".join(lines)


def probe_output_path(state_dir: Path, booking: str, when: datetime | None = None) -> Path:
    """<ETA_STATE_DIR>/probe/msc_<booking>_<YYYYmmdd_HHMMSS>.json (06 §10)."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")   # file timestamp (allowed by 03 §3)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", booking.strip()) or "booking"
    return Path(state_dir) / "probe" / f"msc_{safe}_{stamp}.json"


def save_probe_output(state_dir: Path, booking: str, data: Any, *, when: datetime | None = None) -> Path:
    """Write probe output (`--save-raw`) REDACTED as UTF-8 JSON; returns the path."""
    path = probe_output_path(state_dir, booking, when)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(redact(data), indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return path
