"""Maersk ETA client: the official "Track & Trace Events" API, DCSA T&T 2.2 (07-carrier-maersk.md).

One authenticated JSON call per booking replaces the original headful-Chrome scrape (07 §1):

    GET {MAERSK_BASE_URL}/track-and-trace-private/events?carrierBookingReference=<booking>
    Consumer-Key: <MAERSK_CLIENT_ID>        (always sent: without it Akamai's edge answers an HTML 403)
    Authorization: Bearer <OAuth2 client-credentials token>

The maersk.com website backend (`synergy/tracking`) is deliberately NOT used: it sits behind bot protection
and calling it without a browser would mean defeating that protection (07 §8). There is no fallback that
works around an edge block — the error tells the user to contact Maersk support (07 §6.4).

Shared DCSA parsing and port-of-discharge selection live in carriers/dcsa.py (09 Part A). This module adds
what is Maersk-specific: the token provider (07 §5, §6.1), the request shape and pagination (07 §6.3), the
error mapping (07 §6.4), the final-destination ETA target and date-only option (07 §7) and probe() (07 §10).

DEVIATION (data source, 07 §7.6): structured carrier events replace scraped page text; ETA revisions are
handled explicitly (classifier precedence, newest eventCreatedDateTime). Parity kept: first-container policy,
final-destination target and midnight ETA (all configurable), and the error categories ("Carrier Lookup
Failed." / "Can't determine ETA.").
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import math
import re
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from datetime import time as dtime
from types import TracebackType
from typing import Any, Literal

import httpx

from ..auth import request_with_auth
from ..clock import Clock, SystemClock
from ..config import MaerskSettings, Settings
from ..errors import AuthError, CarrierLookupError, NoEtaError, ServiceError
from ..http import AsyncLimiter, json_body, new_async_client, request
from ..logging_setup import MASK, redact
from ..models import Carrier, EtaResult
from ..token_store import TokenStore, is_fresh, jwt_exp
from . import dcsa
from .base import TokenCache, error_text
from .dcsa import DcsaArrival, DcsaEvent

__all__ = [
    "BOOKING_RE",
    "CLASSIFIER_RANK",
    "DEFAULT_TOKEN_LIFETIME_S",
    "EDGE_403_MSG",
    "EMPTY_EVENTS_MSG",
    "EVENTS_PATH",
    "EVENT_TYPES",
    "MaerskAuth",
    "MaerskClient",
    "ONBOARDING_HINT",
    "RATE_LIMIT_MSG",
    "TOKEN_400_MSG",
    "TOKEN_401_GATEWAY_MSG",
    "TOKEN_403_MSG",
    "TOKEN_STORE_KEY",
    "build_client",
    "choose_arrival",
    "key_fingerprint",
    "select_final_arrival",
]

log = logging.getLogger("eta.carrier.maersk")

# ── 07 §6 constants ──
EVENTS_PATH = "/track-and-trace-private/events"
TOKEN_STORE_KEY = "maersk"
DEFAULT_TOKEN_LIFETIME_S = 7000
BOOKING_RE = re.compile(r"^[A-Za-z0-9]{9}$")
CLASSIFIER_RANK: Mapping[str, int] = dcsa.CLASSIFIER_RANK  # {"ACT": 3, "EST": 2, "PLN": 1}, same as 09 §A.3.1
EVENT_TYPES = "TRANSPORT,EQUIPMENT"  # 07 §4.2: shipment (document) events are not needed; probe() omits it
ONBOARDING_HINT = (
    "Check MAERSK_CLIENT_ID / MAERSK_CLIENT_SECRET and that your developer.maersk.com app is "
    "approved for the 'Track & Trace Events' API product (spec 07 §2)."
)
EMPTY_EVENTS_MSG = (
    "Maersk returned no tracking events for booking {ref}. It may not exist, may be too new, "
    "or may not be linked to the Maersk customer account that owns MAERSK_CLIENT_ID."
)

# ── 07 §6.1 step 4: token-call failures (AuthError texts) ──
TOKEN_400_MSG = (
    "Maersk token request rejected (HTTP 400). Maersk documents this as 'client ID not provisioned' — "
    "usually API access was requested but not yet approved, or the secret is wrong. " + ONBOARDING_HINT
)
TOKEN_401_GATEWAY_MSG = (
    "Maersk gateway rejected the Consumer-Key (ERR_GW_001 'Invalid or expired Key'). "
    "MAERSK_CLIENT_ID must be the app's Consumer Key."
)
TOKEN_403_MSG = "Maersk refused the token request (HTTP 403) — the key may be revoked. " + ONBOARDING_HINT

# ── 07 §6.4: events-call failures (CarrierLookupError texts) ──
EDGE_403_MSG = (
    "Maersk's edge network refused the request (HTTP 403 'Access Denied' HTML page). This normally means "
    "the Consumer-Key header was missing or the request was blocked upstream. Re-run `eta probe maersk`; "
    "if it persists, contact Maersk developer support with the Reference # from the log."
)
RATE_LIMIT_MSG = (
    "Maersk API rate limit hit (HTTP 429) after retries — lower MAERSK_MAX_CONCURRENCY or set "
    "MAERSK_RATE_PER_MINUTE."
)

_TARGETS = ("final", "pod")
_MIN_SCRUB_LEN = 6  # value-based scrubbing ignores implausibly short "secrets" (they would mangle text)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Small helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
def key_fingerprint(client_id: str) -> str:
    """sha256(client_id)[:16]: ties a cached token to the app (Consumer Key) that obtained it (07 §5)."""
    return hashlib.sha256(client_id.encode()).hexdigest()[:16]


def _u(value: str | None) -> str:
    """Stripped upper-case code ("" for None) — hand-built events may be less tidy than parsed ones."""
    return value.strip().upper() if isinstance(value, str) else ""


def _naive(dt: datetime | None) -> datetime | None:
    """Strip tzinfo if any, keeping the wall-clock time (07 §7.2; defensive: keeps comparisons legal)."""
    if dt is None:
        return None
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _rank_key(e: DcsaEvent) -> tuple[int, datetime, datetime]:
    """ACT > EST > PLN, then the newest record (revision), then the later time (07 §7.2)."""
    return (
        CLASSIFIER_RANK.get(_u(e.classifier), 0),
        _naive(e.created_datetime) or datetime.min,
        _naive(e.event_datetime) or datetime.min,
    )


def _is(e: DcsaEvent, event_type: str, type_code: str) -> bool:
    return _u(e.event_type) == event_type and _u(e.type_code) == type_code


def _event_counts(events: Sequence[DcsaEvent]) -> dict[str, int]:
    counts = Counter(f"{e.event_type}/{e.type_code}/{e.classifier}" for e in events)
    return dict(sorted(counts.items()))


def _positive_seconds(value: Any) -> float | None:
    """`expires_in` as a positive, finite number of seconds; None when missing or invalid (07 §5)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _scrub(value: Any, secrets: Sequence[str]) -> Any:
    """Replace every occurrence of a known secret VALUE (Consumer-Key, client secret, access token) with
    "***", recursively. redact() works on key names; this also catches a value echoed in free text."""
    if isinstance(value, str):
        for secret in secrets:
            if secret and len(secret) >= _MIN_SCRUB_LEN and secret in value:
                value = value.replace(secret, MASK)
        return value
    if isinstance(value, Mapping):
        return {k: _scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, secrets) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v, secrets) for v in value)
    return value


def _invalid_booking_message(booking: object) -> str:
    return f"'{booking}' is not a valid Maersk booking reference (expected 9 letters/digits)."


def _page_events(body: Any) -> list[Any]:
    """Maersk wraps the DCSA array as {"events": [...]}; a bare array (the DCSA standard) is accepted too."""
    if isinstance(body, dict) and isinstance(body.get("events"), list):
        return body["events"]
    if isinstance(body, list):
        return body
    keys: Any = sorted(str(k) for k in body)[:10] if isinstance(body, dict) else type(body).__name__
    raise CarrierLookupError(f"Maersk returned an unexpected payload shape (top-level keys: {keys})")


def _same_origin(a: httpx.URL, b: httpx.URL) -> bool:
    return (a.scheme, a.host, a.port) == (b.scheme, b.host, b.port)


def _request_url(resp: httpx.Response, fallback: str) -> str:
    try:
        return str(redact(str(resp.request.url)))
    except RuntimeError:  # response built without a request (only in unusual mocks)
        return str(redact(fallback))


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 07 §7.2 — pure selection functions
# ════════════════════════════════════════════════════════════════════════════════════════════════
def select_final_arrival(events: Sequence[DcsaEvent]) -> tuple[DcsaArrival, str | None] | None:
    """Arrival at the last location of the route (any mode). Returns (arrival, mode_of_transport).

    07 §7.2: TRANSPORT ARRI events grouped by city-level UN/LOCODE; one representative per location
    (ACT > EST > PLN, newest record, later time); locations the cargo DEPArts from are not final (immune to
    stale PLN dates); the latest remaining location wins; an EQUIPMENT DISC ACT there proves arrival.
    """
    # Safer reading of 07 §7.2 step 1: unknown classifiers (REQ, …) rank 0 = "unusable" in the shared DCSA
    # rules (09 §A.3.1), so they never become an ETA.
    arrivals = [e for e in events
                if _is(e, "TRANSPORT", "ARRI") and _u(e.unlocode) and CLASSIFIER_RANK.get(_u(e.classifier), 0) > 0]
    if not arrivals:
        return None
    groups: dict[str, list[DcsaEvent]] = {}
    for e in arrivals:
        groups.setdefault(_u(e.unlocode), []).append(e)
    reps = {loc: max(group, key=_rank_key) for loc, group in groups.items()}
    departure_locs = {_u(e.unlocode) for e in events if _is(e, "TRANSPORT", "DEPA") and _u(e.unlocode)}
    pool = [loc for loc in reps if loc not in departure_locs] or list(reps)
    final_loc = max(pool, key=lambda loc: (_naive(reps[loc].event_datetime) or datetime.min,
                                           CLASSIFIER_RANK.get(_u(reps[loc].classifier), 0)))
    rep = reps[final_loc]
    rep_cls = _u(rep.classifier)
    mode = _u(rep.mode_of_transport) or None

    # Proof of arrival (07 §7.2 step 7). Safer reading: laden boxes only, as in the shared DCSA rules
    # (09 §A.6 test 15) — an EMPTY box movement must not turn an estimate into "ARRIVED".
    if rep_cls != "ACT":
        discs = [e for e in events
                 if _is(e, "EQUIPMENT", "DISC") and _u(e.classifier) == "ACT" and _u(e.unlocode) == final_loc
                 and dcsa.is_laden(e)]
        if discs:
            first = min(discs, key=lambda e: _naive(e.event_datetime) or datetime.min)
            return (
                DcsaArrival(
                    eta=_naive(first.event_datetime) or datetime.min,
                    classifier="ACT",
                    unlocode=final_loc,
                    facility_code=first.facility_code or rep.facility_code,
                    equipment_reference=first.equipment_reference,
                    basis=f"EQUIPMENT DISC/ACT at {final_loc} (final destination)",
                ),
                mode,
            )
    facility = f"/{rep.facility_code}" if rep.facility_code else ""
    return (
        DcsaArrival(
            eta=_naive(rep.event_datetime) or datetime.min,
            classifier=rep_cls,
            unlocode=final_loc,
            facility_code=rep.facility_code,
            equipment_reference=None,
            basis=f"TRANSPORT ARRI/{rep_cls} at {final_loc}{facility} by {mode or 'unknown mode'} (final destination)",
        ),
        mode,
    )


def choose_arrival(events: Sequence[DcsaEvent], *, target: Literal["final", "pod"],
                   policy: Literal["first", "latest"]) -> tuple[DcsaArrival, str | None] | None:
    """Applies MAERSK_ETA_TARGET / MAERSK_MULTI_CONTAINER_POLICY. Returns (arrival, mode) (07 §7.2).

    `pod` → the shared DCSA port-of-discharge rules. `final` → select_final_arrival() finds WHICH location
    is final; when that leg is by vessel the shared rules answer for it (classifier precedence, revisions,
    per-container policy, DISC handling); an inland (RAIL/TRUCK/BARGE) final leg is answered per booking.
    """
    if target not in _TARGETS:
        raise ValueError(f"unknown Maersk ETA target {target!r} (expected 'final' or 'pod')")
    if target == "pod":
        a = dcsa.booking_arrival(events, policy=policy)
        return (a, "VESSEL") if a else None
    final = select_final_arrival(events)
    if final is None:  # no TRANSPORT ARRI at all → let dcsa use DISC evidence
        a = dcsa.booking_arrival(events, policy=policy)
        return (a, "VESSEL") if a else None
    arrival, mode = final
    if mode in (None, "VESSEL"):  # final destination is a seaport → reuse the shared DCSA rules
        final_loc = _u(arrival.unlocode)
        a = dcsa.booking_arrival(events, policy=policy, pod_unlocode=final_loc)
        if a is not None and _u(a.unlocode) in ("", final_loc):
            return (a, "VESSEL")
    return final  # inland final leg (or no DCSA answer there) → booking-level answer


def _source_for(arrival: DcsaArrival) -> str:
    """'Maersk <basis>' (07 §7.3).

    Spec reconciliation (07 §7.3 vs the §9.2 acceptance table): the final-destination bases built above
    already name the location, but the shared DCSA bases ('TRANSPORT ARRI EST', 'EQUIPMENT DISC ACT', …) do
    not, while §9.2 requires the location in `source` ("USNYC" for events_in_transit). For those,
    ' at <UNLOCODE>[/<facility>]' is appended — the same format as dcsa.to_eta_result()."""
    if " at " in arrival.basis:
        return f"Maersk {arrival.basis}"
    where = arrival.unlocode or "?"
    if arrival.facility_code:
        where += f"/{arrival.facility_code}"
    return f"Maersk {arrival.basis} at {where}"


def _alternative(picked: tuple[DcsaArrival, str | None] | None) -> dict[str, Any] | None:
    """probe() 'alternatives' entry (07 §10.1): the exact local arrival time and how it was chosen."""
    if picked is None:
        return None
    arrival, mode = picked
    return {
        "eta": (_naive(arrival.eta) or datetime.min).isoformat(),
        "basis": arrival.basis,
        "classifier": arrival.classifier,
        "unlocode": arrival.unlocode,
        "mode": mode,
    }


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 07 §5, §6.1 — token provider
# ════════════════════════════════════════════════════════════════════════════════════════════════
class MaerskAuth:
    """OAuth2 client-credentials TokenProvider (03 §8, 07 §5).

    Cache entry under TokenStore key "maersk": {access_token, expires_at, token_type, scope, obtained_at,
    key_fingerprint}. A cached token is used only while fresh (120 s skew) AND its fingerprint matches the
    current MAERSK_CLIENT_ID, so switching apps never reuses another app's token.
    **Never acquires the Maersk semaphore**: callers already hold a slot while awaiting the token, so
    acquiring it again could deadlock (03 §8, 07 §5).
    """

    def __init__(self, settings: MaerskSettings, token_store: TokenStore, http: httpx.AsyncClient,
                 *, wall_time: Callable[[], float] = time.time) -> None:
        self._settings = settings
        self._store = token_store
        self._cache = TokenCache(token_store, TOKEN_STORE_KEY, label="Maersk")   # best-effort writes (MSK-05)
        self._http = http
        self.wall_time = wall_time  # epoch seconds (token expiry), injectable for tests
        self._lock = asyncio.Lock()
        self._fingerprint = key_fingerprint(settings.client_id)

    # ── TokenProvider ──
    async def get_token(self) -> str:
        """Cached-if-valid, else one client-credentials request; the cache is re-checked inside the lock so
        concurrent callers trigger a single token call (03 §8)."""
        entry = self._valid_entry()
        if entry is not None:
            return str(entry["access_token"])
        async with self._lock:
            entry = self._valid_entry()
            if entry is not None:
                return str(entry["access_token"])
            return await self._fetch_token()

    async def invalidate(self) -> None:
        """Drop the cached token (request_with_auth() calls this on a 401, then retries once)."""
        self._cache.delete()

    def cached_entry(self) -> dict[str, Any] | None:
        """For probe(): a copy of the cached entry WITHOUT access_token when it is valid (fresh, same
        Consumer Key); None otherwise."""
        entry = self._valid_entry()
        if entry is None:
            return None
        entry.pop("access_token", None)
        return entry

    # ── internals ──
    def _valid_entry(self) -> dict[str, Any] | None:
        entry = self._cache.get()  # a copy
        if entry is None or entry.get("key_fingerprint") != self._fingerprint:
            return None
        return entry if is_fresh(entry, now=self.wall_time()) else None

    def _known_secrets(self) -> tuple[str, ...]:
        """Values that must never appear in messages or probe output (Consumer-Key, secret, token)."""
        entry = self._cache.get() or {}
        token = entry.get("access_token")
        values = [self._settings.client_secret, self._settings.client_id]
        if isinstance(token, str):
            values.append(token)
        return tuple(sorted({v for v in values if v}, key=len, reverse=True))  # longest first

    async def _fetch_token(self) -> str:
        """07 §4.1 / §6.1 steps 3–6."""
        s = self._settings
        headers = {
            "Consumer-Key": s.client_id,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        form = {"grant_type": "client_credentials", "client_id": s.client_id, "client_secret": s.client_secret}
        try:
            resp = await request(self._http, "POST", s.token_url, service="maersk", expected=(200,),
                                 idempotent=True, headers=headers, data=form)
            body = json_body(resp, service="maersk")
        except ServiceError as exc:
            raise AuthError(self._explain(exc)) from exc

        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            keys: Any = sorted(str(k) for k in body) if isinstance(body, dict) else type(body).__name__
            raise AuthError(f"Maersk token response had no access_token (keys: {keys})")
        token = token.strip()
        now = self.wall_time()
        seconds = _positive_seconds(body.get("expires_in"))
        if seconds is not None:
            expires_at = now + seconds
        else:  # 07 §5: the JWT `exp` claim, failing that the documented ~2 h lifetime
            exp = jwt_exp(token)
            expires_at = exp if exp is not None else now + DEFAULT_TOKEN_LIFETIME_S
        token_type = body.get("token_type")
        scope = body.get("scope")
        self._cache.put({
            "access_token": token,
            "expires_at": expires_at,
            "token_type": token_type if isinstance(token_type, str) else None,
            "scope": scope if isinstance(scope, str) else None,
            "obtained_at": now,
            "key_fingerprint": self._fingerprint,
        })
        log.info("[Maersk] OAuth token obtained (valid ~%d min)", max(0, int((expires_at - now) // 60)))
        return token

    def _explain(self, exc: ServiceError) -> str:
        """AuthError text for a failed token call (07 §6.1 step 4)."""
        if exc.status == 400:
            return TOKEN_400_MSG
        if exc.status == 401 and "ERR_GW_001" in (exc.body_excerpt or ""):
            return TOKEN_401_GATEWAY_MSG
        if exc.status == 403:
            return TOKEN_403_MSG
        return _scrub(f"Maersk token request failed: {exc}", self._known_secrets())


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 07 §6.2 — carrier client
# ════════════════════════════════════════════════════════════════════════════════════════════════
class MaerskClient:
    """CarrierClient for Maersk (03 §9). get_eta() raises only CarrierLookupError / NoEtaError (03 §5);
    Maersk never raises CustomsHoldError — that heuristic is MSC-only (07 §7.3)."""

    carrier = Carrier.MAERSK

    def __init__(self, settings: MaerskSettings, token_store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None) -> None:
        self._settings = settings
        self._owns_http = http is None
        self._http = http or new_async_client(
            base_url=settings.base_url, timeout=httpx.Timeout(settings.timeout_s, connect=10.0))
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrency))  # held around each HTTP call (03 §10)
        # MAERSK_RATE_PER_MINUTE = 0 means "off"; the quota is UNVERIFIED (07 §3)
        self._limiter = AsyncLimiter(settings.rate_per_minute, 60.0) if settings.rate_per_minute > 0 else None
        self._auth = MaerskAuth(settings, token_store, self._http)
        # 03 §9 constructor contract; no Maersk rule depends on the run's clock (the API returns dates).
        self._clock = clock or SystemClock()

    @property
    def auth(self) -> MaerskAuth:
        """The token provider (probe/CLI token checks)."""
        return self._auth

    # ── CarrierClient ──
    async def get_eta(self, booking: str) -> EtaResult:
        """07 §6.3. `booking` is the base booking (the router already stripped suffixes)."""
        ref = booking.strip().upper() if isinstance(booking, str) else ""
        if not BOOKING_RE.fullmatch(ref):  # no HTTP call for an impossible reference
            raise CarrierLookupError(_invalid_booking_message(booking))
        log.info("[Maersk] Looking up ETA for booking %s...", ref)
        raw_events, _ = await self._fetch_mapped(ref, event_types=EVENT_TYPES)
        if not raw_events:
            # DEVIATION (no retries on "no results", 07 §7.6): the original reloaded up to 3x because the bot
            # check produced false "No results found" pages; an empty API answer is real. Same outcome as the
            # original's RuntimeError: "Carrier Lookup Failed." with this reason.
            raise CarrierLookupError(EMPTY_EVENTS_MSG.format(ref=ref))
        events = self._parse(raw_events)
        try:
            result = self._select(ref, events)  # NoEtaError propagates
        except (KeyError, TypeError, ValueError) as exc:  # 03 §5 boundary rule
            raise CarrierLookupError(f"Maersk returned events in an unexpected shape: {exc}") from exc
        log.info("[Maersk] ETA=%s (%s)", result.eta.strftime("%m/%d/%Y"), result.source)
        return result

    async def probe(self, booking: str) -> dict[str, Any]:
        """Diagnostics for `eta probe maersk` (07 §10.1): JSON-serializable, never contains the token, the
        secret or the Consumer-Key value. `error` is set when the integration did not deliver events
        (auth, HTTP, shape, empty result); a booking that simply has no ETA yet only fills `eta`."""
        s = self._settings
        ref = booking.strip().upper() if isinstance(booking, str) else ""
        out: dict[str, Any] = {
            "carrier": str(Carrier.MAERSK),
            "booking": ref,
            "config": {
                "base_url": s.base_url,
                "token_url": s.token_url,
                "api_version": s.api_version,
                "eta_target": s.eta_target,
                "policy": s.multi_container_policy,
                "date_only": s.eta_date_only,
                "page_limit": s.page_limit,
            },
            "token": {"ok": False, "cached": False, "expires_in_s": None, "scope": None, "error": None},
            "requests": [],
            "counts": {},
            "raw": [],
            "eta": None,
            "alternatives": {"final": None, "pod": None},
            "explain": [],
            "error": None,
        }

        def fail(exc: Exception) -> dict[str, Any]:
            out["eta"] = error_text(exc)
            out["error"] = str(exc)
            return self._scrubbed(out)

        if not BOOKING_RE.fullmatch(ref):
            return fail(CarrierLookupError(_invalid_booking_message(booking)))

        # 1. token first; on failure report it and stop (07 §10.1)
        token_info = out["token"]
        token_info["cached"] = self._auth.cached_entry() is not None
        try:
            await self._auth.get_token()
        except AuthError as exc:
            token_info["error"] = str(exc)
            return fail(CarrierLookupError(f"Maersk API authentication failed: {exc}"))
        token_info["ok"] = True
        entry = self._auth.cached_entry()
        if entry is not None:
            token_info["expires_in_s"] = int(float(entry["expires_at"]) - self._auth.wall_time())
            token_info["scope"] = entry.get("scope")

        # 2. every event type (event_types=None), with the page summaries kept even on failure
        try:
            raw_events, _ = await self._fetch_mapped(ref, event_types=None, summaries=out["requests"])
        except CarrierLookupError as exc:
            return fail(exc)
        out["raw"] = redact(raw_events)
        if not raw_events:
            return fail(CarrierLookupError(EMPTY_EVENTS_MSG.format(ref=ref)))
        try:
            events = self._parse(raw_events)
        except CarrierLookupError as exc:
            return fail(exc)
        out["counts"] = _event_counts(events)

        # 3. the ETA under the configured target, both alternatives, and the shared DCSA audit (09 §A.7)
        policy = s.multi_container_policy
        try:
            final = select_final_arrival(events)
            out["alternatives"] = {
                target: _alternative(choose_arrival(events, target=target, policy=policy)) for target in _TARGETS
            }
            hint = final[0].unlocode if (s.eta_target == "final" and final and final[1] in (None, "VESSEL")) else None
            head = (f"Maersk target={s.eta_target}, policy={policy}: "
                    + (f"final destination {final[0].unlocode} by {final[1] or 'unknown mode'} via {final[0].basis}"
                       if final else "no TRANSPORT ARRI event (final destination unknown)"))
            out["explain"] = [head, *dcsa.explain_selection(events, pod_unlocode=hint, policy=policy)]
            out["eta"] = self._select(ref, events).to_dict()
        except NoEtaError as exc:
            out["eta"] = error_text(exc)
        except (KeyError, TypeError, ValueError) as exc:
            return fail(CarrierLookupError(f"Maersk returned events in an unexpected shape: {exc}"))
        return self._scrubbed(out)

    async def aclose(self) -> None:
        """Close the HTTP client only if this instance created it."""
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> MaerskClient:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.aclose()

    # ── internals ──
    async def _fetch_mapped(self, ref: str, *, event_types: str | None,
                            summaries: list[dict[str, Any]] | None = None
                            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """_fetch_events() with the 07 §6.3 step-3 exception mapping (each chained `from exc`)."""
        try:
            return await self._fetch_events(ref, event_types=event_types, summaries=summaries)
        except CarrierLookupError:
            raise
        except AuthError as exc:
            raise CarrierLookupError(self._scrub_text(f"Maersk API authentication failed: {exc}")) from exc
        except ServiceError as exc:
            raise self._lookup_error(ref, exc) from exc
        except httpx.HTTPError as exc:
            raise CarrierLookupError(self._scrub_text(f"Maersk API network error: {exc!r}")) from exc
        except OSError as exc:  # 03 §5 safety net (MSK-05), e.g. a local file error
            raise CarrierLookupError(self._scrub_text(f"Maersk lookup failed for {ref}: {exc!r}")) from exc

    async def _fetch_events(self, ref: str, *, event_types: str | None,
                            summaries: list[dict[str, Any]] | None = None
                            ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """GET every page of events for one booking (07 §6.3). Returns (raw event dicts, page summaries);
        pass `summaries` to collect them into a caller-owned list (kept by probe() even on failure).

        `Next-Page` (format UNVERIFIED, 07 §4.2) may be an absolute URL, a path ("/…"), or a bare token sent
        as `cursor`. Safer reading of 07 §6.3 (same rule as dcsa.fetch_event_pages, 09 §A.3.12): an absolute
        link is followed only on the API's own scheme, host AND port, so the bearer token never goes to
        another host or over plain http; a query-only link ("?…") is resolved against the current URL
        instead of being sent as a cursor. Paging stops when Next-Page is missing, equals Current-Page, or
        the page is empty; after MAERSK_MAX_PAGES a WARNING is logged.
        """
        s = self._settings
        base = s.base_url.rstrip("/")
        url = base + EVENTS_PATH  # always absolute: works with or without the client's base_url (07 §6.2)
        origin = httpx.URL(url)
        params: dict[str, str] | None = {"carrierBookingReference": ref, "limit": str(s.page_limit)}
        if event_types:
            params["eventType"] = event_types
        headers = {"Consumer-Key": s.client_id, "Accept": "application/json"}
        if s.api_version:
            headers["API-Version"] = s.api_version
        events: list[dict[str, Any]] = []
        pages = summaries if summaries is not None else []
        max_pages = max(1, int(s.max_pages))

        for page_no in range(1, max_pages + 1):
            t0 = time.monotonic()
            async with self._sem:  # only around the HTTP call; the token provider never takes it (07 §5)
                resp = await request_with_auth(self._http, "GET", url, service="maersk", auth=self._auth,
                                               params=params, headers=headers, expected=(200,),
                                               limiter=self._limiter)
            body = json_body(resp, service="maersk")
            page = _page_events(body)
            events.extend(e for e in page if isinstance(e, dict))
            nxt = (resp.headers.get("Next-Page") or "").strip()
            cur = (resp.headers.get("Current-Page") or "").strip()
            pages.append({
                "page": page_no,
                "status": resp.status_code,
                "events": len(page),
                "elapsed_ms": int((time.monotonic() - t0) * 1000),
                "current_page": cur or None,
                "next_page": redact(nxt) if nxt else None,
                "url": _request_url(resp, url),  # the query holds no secrets (Consumer-Key is a header)
            })
            if not nxt or nxt == cur or not page:
                break
            try:  # httpx.InvalidURL is not an httpx.HTTPError: keep it inside the 03 §5 boundary
                if nxt.lower().startswith(("http://", "https://")):
                    target = httpx.URL(nxt)
                    if not _same_origin(target, origin):  # never follow a link to another host (07 §6.3)
                        log.warning("[Maersk] ignoring Next-Page link to foreign host (%s://%s) for %s",
                                    target.scheme, target.host, ref)
                        break
                    url, params = str(target), None  # the link already carries the query
                elif nxt.startswith("/"):
                    url, params = str(httpx.URL(base + nxt)), None
                elif nxt.startswith("?"):
                    url, params = str(httpx.URL(url).join(nxt)), None
                else:
                    params = {**(params or {}), "cursor": nxt}
            except (httpx.InvalidURL, ValueError):
                log.warning("[Maersk] ignoring malformed Next-Page link for %s", ref)
                break
        else:
            log.warning("[Maersk] stopped after %d pages for %s (MAERSK_MAX_PAGES)", max_pages, ref)
        return events, pages

    def _parse(self, raw_events: list[dict[str, Any]]) -> list[DcsaEvent]:
        """07 §6.3 step 5."""
        try:
            return dcsa.parse_events(raw_events)
        except (KeyError, TypeError, ValueError) as exc:
            raise CarrierLookupError(f"Maersk returned events in an unexpected shape: {exc}") from exc

    def _select(self, ref: str, events: Sequence[DcsaEvent]) -> EtaResult:
        """07 §7.3: choose the arrival, apply the date-only option, build the EtaResult."""
        s = self._settings
        picked = choose_arrival(events, target=s.eta_target, policy=s.multi_container_policy)
        if picked is None:
            raise NoEtaError(f"No ETA published by Maersk for booking {ref} "
                             f"(events seen: {_event_counts(events)}). Can't determine ETA.")
        arrival, mode = picked
        exact = _naive(arrival.eta) or datetime.min
        # 07 §7.5: maersk.com shows dates only, so the original's ETA was midnight local (parity)
        eta = datetime.combine(exact.date(), dtime.min) if s.eta_date_only else exact
        containers = tuple(sorted({_u(e.equipment_reference) for e in events if _u(e.equipment_reference)}))
        base = dcsa.to_eta_result(arrival, carrier=Carrier.MAERSK, booking=ref, containers=containers)
        return dataclasses.replace(
            base,
            eta=eta,
            source=_source_for(arrival),
            details={
                **dict(base.details),
                "target": s.eta_target,
                "policy": s.multi_container_policy,
                "mode": mode,
                "unlocode": arrival.unlocode,
                "facility": arrival.facility_code,
                "classifier": arrival.classifier,
                "eta_exact": exact.isoformat(),
                "event_count": len(events),
            },
        )

    def _lookup_error(self, ref: str, exc: ServiceError) -> CarrierLookupError:
        """Map an events-call ServiceError to CarrierLookupError (07 §6.4)."""
        status = exc.status
        excerpt = exc.body_excerpt or ""
        if status is None:
            # Spec reconciliation (07 §6.3 vs 03 §6 step 5): http.request() re-raises exhausted transport
            # failures as ServiceError(status=None), so 07 §6.3's httpx.HTTPError branch never sees them. Use
            # its network-error text here: no booking number in it, so an outage trips the 10 §6 breaker.
            message = f"Maersk API network error: {exc}"
        elif status == 404:
            message = (f"Maersk has no shipment for booking {ref} (HTTP 404). Check the number; bookings not "
                       "linked to your Maersk customer account are not visible to the API.")
        elif status == 403 and ("access denied" in excerpt.lower() or "edgesuite" in excerpt.lower()):
            # Akamai's page carries a Reference # for Maersk support, no secrets (07 §6.4). Do not work around it.
            log.warning("[Maersk] edge network refused the request for %s (HTTP 403 HTML): %s",
                        ref, self._scrub_text(" ".join(excerpt.split())))
            message = EDGE_403_MSG
        elif status == 403:
            message = (f"Maersk API refused access (HTTP 403): the app is not approved for Track & Trace Events, "
                       f"or booking {ref} is not visible to your account. {ONBOARDING_HINT}")
        elif status == 401:
            message = f"Maersk rejected the credentials (HTTP 401). {ONBOARDING_HINT}"
        elif status == 400:
            message = f"Maersk rejected the request for {ref} (HTTP 400): {exc}"
        elif status == 429:
            message = RATE_LIMIT_MSG
        else:
            message = f"Maersk API error for {ref}: {exc}"
        return CarrierLookupError(self._scrub_text(message))

    def _scrub_text(self, text: str) -> str:
        return str(_scrub(text, self._auth._known_secrets()))

    def _scrubbed(self, out: dict[str, Any]) -> dict[str, Any]:
        return dict(_scrub(out, self._auth._known_secrets()))


def build_client(settings: Settings, token_store: TokenStore, *, clock: Clock | None = None,
                 http: httpx.AsyncClient | None = None) -> MaerskClient | None:
    """None when MAERSK_CLIENT_ID / MAERSK_CLIENT_SECRET are missing (07 §2, 03 §9): the router then reports
    "Maersk lookups are not configured" or uses the aggregator (09 Part D)."""
    if settings.maersk is None:
        return None
    return MaerskClient(settings.maersk, token_store, http=http, clock=clock)
