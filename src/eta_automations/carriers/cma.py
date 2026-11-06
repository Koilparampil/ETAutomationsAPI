"""CMA CGM carrier client — official DCSA Track & Trace API v2.2 (08-carrier-cma-cgm.md).

Replaces CMA/checkETA.py (headful Chrome, DataDome device check / CAPTCHA, cookie file `auth_for_CMA.json`,
CMA-D2) with one authenticated HTTPS GET per booking (08 §4):

    GET {CMA_BASE_URL}/events/{bookingReference}?limit=500             CMA_LOOKUP_MODE=reference (default)
    GET {CMA_BASE_URL}/events?carrierBookingReference={ref}&limit=500  CMA_LOOKUP_MODE=dcsa_query

Auth (08 §5): public tier `KeyId: <CMA_API_KEY>` (CmaApiKeyAuth), or private-tier OAuth2 client credentials
(CmaOAuth: Bearer token cached under TokenStore key "cma", 60 s skew because it lives 299 s).

CMA call policy (08 §6.4 — it **overrides 03-foundations §6 retry policy for CMA API calls**): every physical
attempt runs with max_attempts=1 so http.request() never retries on its own; 5xx / network errors get
MAX_TRANSIENT_ATTEMPTS attempts in total; a 429 waits the exact Retry-After (+0.5 s) through a gate shared by
every concurrent CMA call (the quota is per application, 08 §6.5), at most MAX_RATE_LIMIT_WAITS times and never
longer than CMA_MAX_RETRY_AFTER_S; a 404 is final (billable, never retried). Duplicate bookings in one run share
one call (single-flight memo, 08 §6.6, CMA-D4); probe() bypasses the memo and calls both endpoints.

Business logic (08 §7): events are prefiltered for sentinel dates (R2), checked for the requested booking when
CMA returns real references (R3, CMA-D3), parsed by the shared DCSA module (09 Part A), the port of discharge is
CMA's own `carrierSpecificData.shipmentLocationType == "POD"` tag (R5), empty-container moves are ignored (R7,
CMA-D5), and the booking ETA is the LATEST container arrival at the POD (§7.3, original parity). Never the inland
final place of delivery (CMA-D1). CMA never raises CustomsHoldError (MSC-only heuristic).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal
from urllib.parse import quote, unquote, urlencode, urlsplit

import httpx

from ..auth import bearer_headers, request_with_auth
from ..clock import Clock, SystemClock
from ..config import CmaSettings, Settings
from ..errors import AuthError, CarrierLookupError, ConfigError, NoEtaError, ServiceError
from ..http import AsyncLimiter, json_body, new_async_client, parse_retry_after, request
from ..logging_setup import MASK, redact
from ..models import Carrier, EtaResult
from ..token_store import TokenStore, is_fresh, jwt_exp
from . import dcsa
from .base import error_text
from .dcsa import DcsaArrival, DcsaEvent

__all__ = [
    "BOOKING_REF_RE",
    "DEFAULT_RETRY_AFTER_S",
    "HASHED_REF_RE",
    "HINT_401",
    "HINT_403",
    "MAX_RATE_LIMIT_WAITS",
    "MAX_TRANSIENT_ATTEMPTS",
    "PLAUSIBLE_YEARS",
    "PROBE_MIN_BILLABLE_CALLS",
    "RETRY_STATUSES",
    "TNT_PATH_QUERY",
    "TNT_PATH_REFERENCE",
    "UUID_RE",
    "CmaApiKeyAuth",
    "CmaClient",
    "CmaOAuth",
    "CmaTokenError",
    "FetchResult",
    "build_client",
    "check_booking_identity",
    "container_references",
    "describe_arrival",
    "evaluate_events",
    "extract_cursor",
    "is_empty_equipment",
    "prefilter_raw_events",
    "resolve_pod_unlocode",
]

log = logging.getLogger("eta.carrier.cma")

# ── 08 §6 constants ──
SERVICE = "cma"
TOKEN_STORE_KEY = "cma"
TNT_PATH_REFERENCE = "/events/{ref}"
TNT_PATH_QUERY = "/events"
RETRY_STATUSES = frozenset({500, 502, 503, 504})
MAX_TRANSIENT_ATTEMPTS = 3          # total attempts for 5xx / network errors
MAX_RATE_LIMIT_WAITS = 3            # 429 waits per logical call
DEFAULT_RETRY_AFTER_S = 60.0        # when a 429 has no usable Retry-After
RATE_LIMIT_MARGIN_S = 0.5           # added to every Retry-After wait (08 §6.4)
PLAUSIBLE_YEARS = range(2000, 2101) # original: accept only years 2000–2100
BOOKING_REF_RE = re.compile(r"^[A-Z0-9]{1,35}$")
HASHED_REF_RE = re.compile(r"^[0-9A-F]{32,}$")                        # FAQ: public access gets a 64-hex hash
UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
OAUTH_SKEW_S = 60                   # 08 §5: 60 s instead of 120 s — the token only lives 299 s
DEFAULT_TOKEN_LIFETIME_S = 299.0    # 08 §4.8: "expires_in": 299 / "The access token lasts for 5 minutes."
PROBE_MIN_BILLABLE_CALLS = 2        # 08 §6.8 / §10: the probe calls both endpoints (plus any pages)
REQUEST_ID_HEADERS = ("x-request-id", "x-correlationid", "x-kong-request-id")   # 08 §6.4 _request_id()
_PAGE_LIST_KEYS = ("events", "data", "items")                       # 08 §6.3 defensive wrapped shapes
_YEAR_RE = re.compile(r"^\s*(\d{4})-")                               # 08 §7.2 R2: year only, never a full parse
_BRACED_RE = re.compile(r"\{[^{}]*\}")                              # salvage DCSA error objects from a cut excerpt
_MIN_SECRET_LEN = 6                 # value-based scrubbing ignores implausibly short "secrets"

# ── 08 §6.2 / §6.7 exact texts ──
NO_BOOKING_MSG = "No CMA booking number given."
INVALID_BOOKING_MSG = "'{ref}' is not a valid CMA booking reference."
NOT_FOUND_404_MSG = (
    "CMA tracking API has no record of booking {ref} (HTTP 404). CMA returns 404 when a booking is not confirmed "
    "yet or was cancelled; CMA also drops shipments whose containers were returned to the depot more than 15 days ago."
)
SHIPMENT_NOT_FOUND_MSG = (
    "CMA public tracking shows 'shipment not found' for booking {ref}. Check the booking number — CMA also drops "
    "shipments whose containers were returned to the depot more than 15 days ago."
)
HINT_401 = (
    "CMA rejected the API credentials (HTTP 401). Check CMA_API_KEY (API Portal → API Applications → your "
    "application card) or CMA_CLIENT_ID/CMA_CLIENT_SECRET; the 30-day free trial may have ended."
)
HINT_403 = (
    "CMA refused the request (HTTP 403: no sufficient rights). Check that your API application is subscribed to "
    "the 'Logistic Tracking service API' (operation.trackandtrace.v1) and that your trial or contract is active."
)
RATE_LIMIT_MSG = (
    "CMA rate limit reached (HTTP 429, retry after {ra:.0f}s; CMA_MAX_RETRY_AFTER_S={cap}). Try again later or ask "
    "CMA to raise your application's quota."
)
LOOKUP_FAILED_MSG = "Failed to get CMA tracking info for {ref}: {exc}"
REJECTED_MSG = "CMA tracking API rejected the request for {ref} (HTTP {status}): {detail}"
COULD_NOT_READ_MSG = "Could not read CMA tracking data for {ref}: {detail}"
WRONG_BOOKING_MSG = "CMA returned tracking for {others} instead of the requested booking {ref}."
NO_USABLE_DATE_MSG = "CMA returned events for booking {ref} but none carried a usable date. Can't determine ETA."
NO_CONTAINERS_MSG = "CMA found booking {ref} but listed no containers to read an ETA from. Can't determine ETA."


class CmaTokenError(AuthError):
    """The OAuth access token could not be obtained (08 §5 token-endpoint errors).

    Kept apart from the AuthError that request_with_auth() raises after a second API 401 (→ HINT_401): the
    boundary rule (08 §6.2 step 6) wraps this one with the booking, keeping the token endpoint's own reason.
    """


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 08 §5 — token providers (03 §8 TokenProvider)
# ════════════════════════════════════════════════════════════════════════════════════════════════
class CmaApiKeyAuth:
    """Public tier: the API key is the "token", sent as header `KeyId` (08 §4.1, §5).

    invalidate() is a no-op, so a 401 is retried once by request_with_auth(); that is harmless and the second
    401 raises AuthError, which the client maps to HINT_401.
    """

    def __init__(self, api_key: str | None) -> None:
        key = (api_key or "").strip()
        if not key:
            raise ConfigError("CMA API-key mode needs CMA_API_KEY")
        self._key = key

    async def get_token(self) -> str:
        return self._key

    async def invalidate(self) -> None:
        return None

    @staticmethod
    def build_headers(key: str) -> dict[str, str]:
        """08 §5: build_headers=lambda k: {"KeyId": k}."""
        return {"KeyId": key}

    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in messages or probe output."""
        return (self._key,)

    def __repr__(self) -> str:
        return f"CmaApiKeyAuth(api_key={MASK})"


class CmaOAuth:
    """Private tier: OAuth2 client credentials at CMA_TOKEN_URL (08 §4.8, §5).

    Cache: TokenStore key "cma" = {access_token, expires_at, token_type, scope}, mirrored in memory. A cached
    token is reused while is_fresh(skew_seconds=60). get_token() re-checks the cache under an asyncio.Lock, so
    concurrent lookups trigger one token POST (03 §8). It never acquires the CMA semaphore (callers may hold a
    slot while awaiting the token). Token-endpoint failures raise CmaTokenError (an AuthError):
    400/401 → "cma: OAuth token request rejected: {error}: {error_description}", else "… failed: {exc}".
    """

    build_headers = staticmethod(bearer_headers)   # 08 §5: default `Authorization: Bearer …`

    def __init__(self, settings: CmaSettings, token_store: TokenStore, http: httpx.AsyncClient, *,
                 wall_time: Callable[[], float] = time.time) -> None:
        if not settings.client_id or not settings.client_secret:
            raise ConfigError("CMA OAuth mode needs CMA_CLIENT_ID and CMA_CLIENT_SECRET")
        self._settings = settings
        self._store = token_store
        self._http = http
        self.wall_time = wall_time   # epoch seconds for expires_at; injectable for tests
        self._lock = asyncio.Lock()
        self._entry: dict[str, Any] | None = None

    # ── TokenProvider ──
    async def get_token(self) -> str:
        """Cached-if-fresh (60 s skew), else one client-credentials POST (08 §5 steps 1–4)."""
        entry = self._fresh_entry()
        if entry is not None:
            return str(entry["access_token"])
        async with self._lock:
            entry = self._fresh_entry()
            if entry is not None:
                return str(entry["access_token"])
            return await self._fetch_token()

    async def invalidate(self) -> None:
        """Drop the token from the store and from memory (request_with_auth calls this on a 401)."""
        self._entry = None
        self._store.delete(TOKEN_STORE_KEY)

    def cached_entry(self) -> dict[str, Any] | None:
        """A copy of the fresh cached entry WITHOUT access_token (probe/CLI), or None."""
        entry = self._fresh_entry()
        if entry is None:
            return None
        info = dict(entry)
        info.pop("access_token", None)
        return info

    def secrets(self) -> tuple[str, ...]:
        """Values that must never appear in messages or probe output (client secret, access tokens)."""
        values: list[str] = [self._settings.client_secret or ""]
        for entry in (self._entry, self._store.get(TOKEN_STORE_KEY)):
            token = entry.get("access_token") if entry else None
            if isinstance(token, str):
                values.append(token)
        return tuple(v for v in values if v)

    def __repr__(self) -> str:
        return f"CmaOAuth(client_id={self._settings.client_id!r}, token_url={self._settings.token_url!r})"

    # ── internals ──
    def _fresh_entry(self) -> dict[str, Any] | None:
        now = self.wall_time()
        for entry in (self._entry, self._store.get(TOKEN_STORE_KEY)):
            if entry is not None and is_fresh(entry, skew_seconds=OAUTH_SKEW_S, now=now):
                self._entry = dict(entry)
                return self._entry
        return None

    async def _fetch_token(self) -> str:
        """08 §5 step 3: request(POST token_url, form, expected=(200,), idempotent=True) — the default 03 §6
        retries apply to this call (it is not a billable Track & Trace call)."""
        s = self._settings
        form = {
            "grant_type": "client_credentials",
            "client_id": s.client_id or "",
            "client_secret": s.client_secret or "",
            "scope": s.oauth_scope,
        }
        try:
            resp = await request(self._http, "POST", s.token_url, service=SERVICE, data=form,
                                 headers={"Accept": "application/json"}, expected=(200,), idempotent=True)
            body = json_body(resp, service=SERVICE)
        except ServiceError as exc:
            if exc.status in (400, 401):
                raise CmaTokenError(f"cma: OAuth token request rejected: {_oauth_error_detail(exc)}") from exc
            raise CmaTokenError(f"cma: OAuth token request failed: {exc}") from exc

        token = body.get("access_token") if isinstance(body, Mapping) else None
        if not isinstance(token, str) or not token.strip():
            shape = sorted(str(k) for k in body)[:10] if isinstance(body, Mapping) else type(body).__name__
            raise CmaTokenError(f"cma: OAuth token request failed: the response had no access_token (keys: {shape})")
        token = token.strip()
        now = self.wall_time()
        lifetime = _positive_seconds(body.get("expires_in"))
        if lifetime is not None:
            expires_at = now + lifetime
        else:
            # 03 §7: without an explicit lifetime use the JWT `exp` claim, failing that CMA's documented 299 s.
            exp = jwt_exp(token)
            expires_at = exp if exp is not None else now + DEFAULT_TOKEN_LIFETIME_S
        token_type = body.get("token_type")
        scope = body.get("scope")
        entry = {
            "access_token": token,
            "expires_at": expires_at,
            "token_type": token_type if isinstance(token_type, str) and token_type.strip() else "Bearer",
            "scope": scope if isinstance(scope, str) and scope.strip() else s.oauth_scope,
        }
        self._store.put(TOKEN_STORE_KEY, entry)
        self._entry = dict(entry)
        log.debug("[CMA] OAuth token obtained (expires in %d s)", int(expires_at - now))
        return token


def _oauth_error_detail(exc: ServiceError) -> str:
    """"{error}: {error_description}" read from an RFC 6749 error body when present (08 §5)."""
    body: Any = None
    if exc.body_excerpt:
        try:
            body = json.loads(exc.body_excerpt)
        except ValueError:
            body = None
    error = _clean_text(_get_ci(body, "error"))
    description = _clean_text(_get_ci(body, "error_description"))
    if error is None and description is None:
        return f"HTTP {exc.status} (no OAuth error details in the response)"
    return ": ".join(part for part in (error, description) if part)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 08 §6 — client
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True, slots=True)
class FetchResult:
    events: list[Any]               # concatenated raw items from all pages
    pages: int
    requests: list[dict[str, Any]]  # per-request summaries for probe/logs (no secrets)
    endpoint: Literal["reference", "dcsa_query"]


class CmaClient:
    """CarrierClient for CMA CGM (03 §9). get_eta() raises only CarrierLookupError / NoEtaError (03 §5)."""

    carrier = Carrier.CMA

    def __init__(self, settings: CmaSettings, token_store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        """08 §6.1. `sleep` and `monotonic` (the clock of the shared 429 gate) are injectable so tests run
        instantly and can assert the waits; an injected sleep should advance the injected monotonic clock."""
        _check_credentials(settings)
        self._settings = settings
        self._owns_http = http is None
        # Absolute URLs everywhere (the token URL is on another host), hence base_url="".
        self._http = http if http is not None else new_async_client(
            base_url="", headers={"Accept": "application/json"},
            timeout=httpx.Timeout(settings.timeout_s, connect=10.0))
        self._auth: CmaApiKeyAuth | CmaOAuth = (
            CmaOAuth(settings, token_store, self._http) if settings.auth_mode == "oauth"
            else CmaApiKeyAuth(settings.api_key)
        )
        self._build_headers: Callable[[str], Mapping[str, str]] = self._auth.build_headers
        # 03 §9 constructor contract; no CMA rule depends on the run's clock (the API returns the dates).
        self.clock: Clock = clock or SystemClock()
        self._sleep = sleep
        self._monotonic = monotonic
        self._sem = asyncio.Semaphore(max(1, settings.max_concurrency))   # held around each HTTP attempt only
        self._limiter = (
            AsyncLimiter(settings.max_calls_per_minute, 60.0, clock=monotonic, sleep=sleep)
            if settings.max_calls_per_minute else None
        )
        self._blocked_until = 0.0   # monotonic instant shared by every CMA call (08 §6.5)
        self._memo: dict[str, asyncio.Task[EtaResult]] = {}

    @property
    def auth(self) -> CmaApiKeyAuth | CmaOAuth:
        """The token provider (CLI/probe token checks)."""
        return self._auth

    # ── CarrierClient ──
    async def get_eta(self, booking: str) -> EtaResult:
        """08 §6.2: normalise, then one shared lookup per booking for the client's lifetime (successes and
        failures memoised — CMA-D4); asyncio.shield keeps one caller's cancellation from cancelling it."""
        ref = _normalise_ref(booking)
        # DEVIATION CMA-D4: duplicate bookings in one run share one (billable) API call, failures included.
        task = self._memo.get(ref)
        if task is None:
            task = asyncio.create_task(self._lookup(ref), name=f"cma-lookup-{ref}")
            self._memo[ref] = task
        return await asyncio.shield(task)

    async def probe(self, booking: str) -> dict[str, Any]:
        """Diagnostics for `eta probe cma` (08 §6.8, §10). Bypasses the memo and calls BOTH endpoints, so it
        makes PROBE_MIN_BILLABLE_CALLS or more billable calls. JSON-serializable; never contains the API key,
        the client secret or a token. `error` is set when the configured endpoint did not deliver a lookup
        result (credentials, HTTP, unreadable payload, booking not found / wrong booking); a booking that simply
        has no ETA yet only fills `eta`."""
        s = self._settings
        other: Literal["reference", "dcsa_query"] = "dcsa_query" if s.lookup_mode == "reference" else "reference"
        info: dict[str, Any] = {
            "auth_mode": s.auth_mode,
            "base_url": s.base_url,
            "lookup_mode": s.lookup_mode,
            "page_limit": s.page_limit,
            "behalf_of_set": bool(s.behalf_of),
        }
        out: dict[str, Any] = {
            "carrier": str(Carrier.CMA),
            "booking": str(booking or "").strip().upper() if isinstance(booking, str) else "",
            "info": info,
            "requests": [],
            "raw": {"reference": None, "dcsa_query": None},
            "eta": None,
            "analysis": {"reference": None, "dcsa_query": None, "endpoints_agree": False},
            "error": None,
        }
        try:
            ref = _normalise_ref(booking)
        except CarrierLookupError as exc:
            out["eta"] = error_text(exc)
            out["error"] = str(exc)
            return self._scrubbed(out)

        # 3. OAuth: prove the credentials with a fresh token (step 3); stop early when it cannot be obtained.
        if isinstance(self._auth, CmaOAuth):
            await self._auth.invalidate()
            try:
                await self._auth.get_token()
            except AuthError as exc:
                info["token"] = {"token_ok": False, "error": self._scrub_text(str(exc))}
                failure = CarrierLookupError(LOOKUP_FAILED_MSG.format(ref=ref, exc=exc))
                out["eta"] = error_text(failure)
                out["error"] = str(failure)
                return self._scrubbed(out)
            entry = self._auth.cached_entry() or {}
            expires_at = _positive_seconds(entry.get("expires_at"))
            info["token"] = {
                "token_ok": True,
                "expires_in": round(expires_at - self._auth.wall_time()) if expires_at is not None else None,
                "scope": entry.get("scope"),
            }

        # 4–5. both endpoints (configured first), each failure recorded as a string
        outcomes: dict[str, EtaResult | Exception] = {}
        for mode in (s.lookup_mode, other):
            first = len(out["requests"])
            try:
                fetched = await self._fetch_events(ref, mode, summaries=out["requests"])
            except (CarrierLookupError, ServiceError, AuthError, httpx.HTTPError, KeyError, TypeError,
                    ValueError) as exc:
                failure = exc if isinstance(exc, CarrierLookupError) else CarrierLookupError(
                    LOOKUP_FAILED_MSG.format(ref=ref, exc=exc))
                outcomes[mode] = failure
                out["analysis"][mode] = {"error": error_text(failure)}
            else:
                out["raw"][mode] = redact(fetched.events)
                analysis, outcome = _analyse(ref, fetched)
                outcomes[mode] = outcome
                out["analysis"][mode] = analysis
            for summary in out["requests"][first:]:
                summary["endpoint"] = mode

        configured = outcomes[s.lookup_mode]
        if isinstance(configured, EtaResult):
            out["eta"] = configured.to_dict()
        else:
            out["eta"] = error_text(configured)
            if isinstance(configured, CarrierLookupError):
                out["error"] = str(configured)
        out["analysis"]["endpoints_agree"] = _endpoints_agree(outcomes["reference"], outcomes["dcsa_query"])
        return self._scrubbed(out)

    async def aclose(self) -> None:
        """Cancel lookups still pending and close the HTTP client only if this instance created it."""
        pending = [task for task in self._memo.values() if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> CmaClient:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        tb: TracebackType | None) -> None:
        await self.aclose()

    # ── internals ──
    async def _lookup(self, ref: str) -> EtaResult:
        """08 §6.2 step 3, including the boundary rule (03 §5, 08 §6.2 step 6)."""
        log.info("  [CMA] Looking up ETA for booking %s...", ref)
        try:
            fetched = await self._fetch_events(ref, self._settings.lookup_mode)
            log.info("  [CMA] responded OK — parsing for ETA...")
            result = evaluate_events(ref, fetched.events, endpoint=fetched.endpoint, pages=fetched.pages)
        except (CarrierLookupError, NoEtaError):
            raise
        except (ServiceError, AuthError, httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
            raise CarrierLookupError(self._scrub_text(LOOKUP_FAILED_MSG.format(ref=ref, exc=exc))) from exc
        log.info("  [CMA] Returning ETA: %s", result.eta.strftime("%m/%d/%Y"))
        return result

    async def _fetch_events(self, ref: str, mode: Literal["reference", "dcsa_query"], *,
                            summaries: list[dict[str, Any]] | None = None) -> FetchResult:
        """08 §6.3: first page by booking, then DCSA cursor pages while `Next-Page` is returned (≤ max_pages).
        `summaries` (probe) collects the per-request summaries even when the fetch fails."""
        s = self._settings
        summaries = [] if summaries is None else summaries
        behalf = {"behalfOf": s.behalf_of} if s.behalf_of else {}
        if mode == "reference":
            url = s.base_url + TNT_PATH_REFERENCE.format(ref=quote(ref, safe=""))
            params = {"limit": str(s.page_limit), **behalf}
        elif mode == "dcsa_query":
            url = s.base_url + TNT_PATH_QUERY
            params = {"carrierBookingReference": ref, "limit": str(s.page_limit), **behalf}
        else:
            raise ValueError(f"unknown CMA lookup mode {mode!r} (expected 'reference' or 'dcsa_query')")

        events: list[Any] = []
        pages = 0
        for page in range(1, s.max_pages + 1):
            resp = await self._send(url, params, summaries, ref=ref)
            if resp.status_code == 404:
                if page == 1:
                    # R1, billable: never retried. DEVIATION CMA-D6: unconfirmed/cancelled bookings get their own
                    # "HTTP 404" text (the original reported every unknown booking as "shipment not found").
                    raise CarrierLookupError(NOT_FOUND_404_MSG.format(ref=ref))
                log.debug("[CMA] page %d for booking %s answered HTTP 404 — keeping the %d page(s) read",
                          page, ref, pages)
                break
            items = _page_items(ref, resp)
            pages += 1
            events.extend(items)
            nxt = (resp.headers.get("Next-Page") or "").strip()
            if not nxt or not items:
                break
            if page == s.max_pages:
                log.warning("[CMA] booking %s has more than %d pages of events — using the first %d",
                            ref, s.max_pages, s.max_pages)
                break
            # DCSA convention: the cursor encodes the query, so the original filters are not repeated (§4.4).
            params = {"cursor": extract_cursor(nxt), "limit": str(s.page_limit), **behalf}
        return FetchResult(events=events, pages=pages, requests=summaries, endpoint=mode)

    async def _send(self, url: str, params: dict[str, str], summaries: list[dict[str, Any]], *,
                    ref: str = "") -> httpx.Response:
        """One logical call under the CMA call policy (08 §6.4, overriding 03 §6). Returns a 200 or 404
        response; everything else ends as CarrierLookupError (or a CmaTokenError for the boundary rule)."""
        s = self._settings
        transient_attempt = 0
        rate_waits = 0
        while True:
            await self._wait_if_blocked()
            started = time.perf_counter()
            try:
                async with self._sem:
                    resp = await request_with_auth(
                        self._http, "GET", url, service=SERVICE, auth=self._auth,
                        build_headers=self._build_headers,           # KeyId or Bearer
                        headers={"Accept": "application/json"},     # 08 §4.1: on every call
                        params=params, expected=(200, 404, 429), idempotent=True,
                        max_attempts=1, limiter=self._limiter)
            except ServiceError as exc:   # 5xx, network (status None), 403, other 4xx
                summaries.append(self._summary(url, params, exc.status, started, exc.request_id, error=exc))
                transient = exc.status is None or exc.status in RETRY_STATUSES
                if transient and transient_attempt < MAX_TRANSIENT_ATTEMPTS - 1:
                    transient_attempt += 1
                    delay = min(30.0, 1.0 * 2 ** (transient_attempt - 1)) + random.uniform(0, 0.25)
                    log.debug("[CMA] %s — retrying in %.1fs (attempt %d/%d)", error_text(exc), delay,
                              transient_attempt + 1, MAX_TRANSIENT_ATTEMPTS)
                    await self._sleep(delay)
                    continue
                raise CarrierLookupError(self._scrub_text(_service_error_message(exc, ref))) from exc
            except CmaTokenError:
                raise   # 08 §5: the boundary rule wraps it with the booking ("Failed to get CMA tracking info …")
            except AuthError as exc:   # second 401 after the token refresh (request_with_auth)
                cause = exc.__cause__
                status = cause.status if isinstance(cause, ServiceError) else 401
                request_id = cause.request_id if isinstance(cause, ServiceError) else None
                summaries.append(self._summary(url, params, status, started, request_id, error=exc))
                raise CarrierLookupError(HINT_401) from exc
            summaries.append(self._summary(url, params, resp.status_code, started, _request_id(resp), resp.headers))
            if resp.status_code != 429:
                return resp   # 200 or 404
            ra = _parse_retry_after(resp.headers.get("Retry-After"), default=DEFAULT_RETRY_AFTER_S)
            if ra > s.max_retry_after_s or rate_waits >= MAX_RATE_LIMIT_WAITS:
                raise CarrierLookupError(RATE_LIMIT_MSG.format(ra=ra, cap=s.max_retry_after_s))
            rate_waits += 1
            # 08 §6.5: CMA's quota is per application — every concurrent CMA call waits at the shared gate.
            self._blocked_until = max(self._blocked_until, self._monotonic() + ra + RATE_LIMIT_MARGIN_S)
            log.warning("[CMA] rate limited (HTTP 429) — waiting %.0fs as instructed by Retry-After", ra)

    async def _wait_if_blocked(self) -> None:
        """08 §6.5: sleep until the shared gate opens, if it is closed."""
        delay = self._blocked_until - self._monotonic()
        if delay > 0:
            log.debug("[CMA] waiting %.1fs at the shared rate-limit gate", delay)
            await self._sleep(delay)

    def _summary(self, url: str, params: Mapping[str, str], status: int | None, started: float,
                 request_id: str | None, headers: httpx.Headers | None = None, *,
                 error: BaseException | None = None) -> dict[str, Any]:
        """08 §6.4 per-request summary (no secrets); `error` and `next_page_value` are added for the probe."""
        query = urlencode(params)
        hdrs = headers if headers is not None else httpx.Headers()
        summary: dict[str, Any] = {
            "method": "GET",
            "url": redact(f"{url}?{query}" if query else url),
            "status": status,
            "elapsed_ms": round((time.perf_counter() - started) * 1000),
            "request_id": request_id,
            "api_version": hdrs.get("API-Version"),
            "next_page": bool(hdrs.get("Next-Page")),
            "next_page_value": redact(hdrs.get("Next-Page")),   # settles UNVERIFIED (c): raw cursor or URL
            "cache_control": hdrs.get("cache-control"),
            "retry_after": hdrs.get("Retry-After"),
        }
        if error is not None:
            summary["error"] = self._scrub_text(error_text(error))
        return summary

    def _secrets(self) -> tuple[str, ...]:
        values = {v for v in self._auth.secrets() if isinstance(v, str) and len(v) >= _MIN_SECRET_LEN}
        return tuple(sorted(values, key=len, reverse=True))   # longest first

    def _scrub_text(self, text: str) -> str:
        """Value-based scrubbing on top of redact(): an echoed key/secret/token never reaches a message."""
        for secret in self._secrets():
            text = text.replace(secret, MASK)
        return text

    def _scrubbed(self, out: dict[str, Any]) -> dict[str, Any]:
        """Probe output with every known secret value masked (on top of redact())."""
        return {key: self._scrub_value(value) for key, value in out.items()}

    def _scrub_value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._scrub_text(value)
        if isinstance(value, Mapping):
            return {k: self._scrub_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._scrub_value(v) for v in value]
        return value


def build_client(settings: Settings, token_store: TokenStore, *, clock: Clock | None = None,
                 http: httpx.AsyncClient | None = None) -> CmaClient | None:
    """None when CMA is not configured (08 §3: no CMA_API_KEY and no CMA_CLIENT_ID + CMA_CLIENT_SECRET); the
    router then reports "CMA lookups are not configured — set …" or uses the aggregator (09 Part D)."""
    if settings.cma is None:
        return None
    return CmaClient(settings.cma, token_store, http=http, clock=clock)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# client helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _check_credentials(settings: CmaSettings) -> None:
    if settings.auth_mode == "oauth":
        if not settings.client_id or not settings.client_secret:
            raise ConfigError("CMA OAuth mode needs CMA_CLIENT_ID and CMA_CLIENT_SECRET")
    elif not (settings.api_key or "").strip():
        raise ConfigError("CMA API-key mode needs CMA_API_KEY")


def _normalise_ref(booking: Any) -> str:
    """08 §6.2 step 1 (defensive: the router already guarantees a bare NAM\\d{7} token, 01 §4 / D6)."""
    ref = booking.strip().upper() if isinstance(booking, str) else ""
    if not ref:
        raise CarrierLookupError(NO_BOOKING_MSG)
    if not BOOKING_REF_RE.fullmatch(ref):
        raise CarrierLookupError(INVALID_BOOKING_MSG.format(ref=ref))
    return ref


def _request_id(resp: httpx.Response) -> str | None:
    """First present of x-request-id, x-correlationid, x-kong-request-id (08 §6.4)."""
    for name in REQUEST_ID_HEADERS:
        value = str(resp.headers.get(name) or "").strip()
        if value:
            return value
    return None


def _parse_retry_after(value: str | None, *, default: float) -> float:
    """Seconds (integer/float) or an HTTP-date (seconds until then, floored at 0); missing/unparseable →
    `default` (08 §6.4)."""
    parsed = parse_retry_after(value)
    return default if parsed is None else parsed


def _page_items(ref: str, resp: httpx.Response) -> list[Any]:
    """08 §6.3 step 2.3: a JSON array, or (defensive) a list under "events"/"data"/"items"."""
    try:
        body = json_body(resp, service=SERVICE)
    except ServiceError as exc:
        raise CarrierLookupError(COULD_NOT_READ_MSG.format(ref=ref, detail=exc)) from exc
    if isinstance(body, list):
        return body
    if isinstance(body, Mapping):
        for key in _PAGE_LIST_KEYS:
            value = body.get(key)
            if isinstance(value, list):
                return value
    keys = sorted(str(k) for k in body)[:10] if isinstance(body, Mapping) else "-"
    raise CarrierLookupError(COULD_NOT_READ_MSG.format(
        ref=ref, detail=f"expected a JSON array of events, got {type(body).__name__} with keys {keys}"))


def _service_error_message(exc: ServiceError, ref: str) -> str:
    """08 §6.7 mapping of a ServiceError that ends a call (after any transient retries)."""
    status = exc.status
    if status == 401:
        return HINT_401
    if status == 403:   # "no sufficient rights" — not "token expired" (08 §6.4)
        return HINT_403
    if status is not None and 400 <= status < 500:
        reasons = _dcsa_error_reasons(exc.body_excerpt)
        # Fallback when the body is not a DCSA error body: the ServiceError text.
        return REJECTED_MSG.format(ref=ref, status=status, detail="; ".join(reasons) if reasons else exc)
    return LOOKUP_FAILED_MSG.format(ref=ref, exc=exc)   # 5xx / network after the retries


def _dcsa_error_reasons(text: str) -> list[str]:
    """"{reason}: {message}" for each errors[*] of a DCSA error body (08 §4.6). The body is a ≤ 500-char
    excerpt; when it was cut, complete `{...}` error objects are still salvaged."""
    errors: Any = None
    try:
        body = json.loads(text) if text else None
    except ValueError:
        errors = []
        for chunk in _BRACED_RE.findall(text):
            try:
                obj = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(obj, Mapping) and ("reason" in obj or "message" in obj):
                errors.append(obj)
    else:
        errors = _get_ci(body, "errors")
    out: list[str] = []
    for err in errors if isinstance(errors, list) else ():
        reason = _clean_text(_get_ci(err, "reason"))
        message = _clean_text(_get_ci(err, "message"))
        part = ": ".join(p for p in (reason, message) if p)
        if part:
            out.append(part)
    return out


def _positive_seconds(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _get_ci(mapping: Any, name: str) -> Any:
    """mapping[name], else a case-insensitive key match (DCSA dialects differ in key case, 09 §A.3.4)."""
    if not isinstance(mapping, Mapping):
        return None
    if name in mapping:
        return mapping[name]
    folded = name.casefold()
    for key, value in mapping.items():
        if isinstance(key, str) and key.casefold() == folded:
            return value
    return None


def _event_body(item: Any) -> Mapping[str, Any]:
    """The event object: the item itself (DCSA 2.2, what CMA serves) or the payload of an inferred v3
    `{"metadata", "payload"}` envelope (tolerated only, 09 §A.2.2)."""
    if not isinstance(item, Mapping):
        return {}
    if isinstance(_get_ci(item, "metadata"), Mapping):
        payload = _get_ci(item, "payload")
        return payload if isinstance(payload, Mapping) else {}
    return item


def _is_withdrawal(item: Mapping[str, Any]) -> bool:
    """A v3 withdrawal envelope (metadata.retractedEventID, no payload): it carries no date but must reach
    dcsa.parse_events() so the withdrawn event is discarded (09 §A.3.4 step 5)."""
    metadata = _get_ci(item, "metadata")
    return (isinstance(metadata, Mapping) and bool(_clean_text(_get_ci(metadata, "retractedEventID")))
            and not isinstance(_get_ci(item, "payload"), Mapping))


def _carrier_specific(item: Any) -> Mapping[str, Any]:
    value = _get_ci(_event_body(item), "carrierSpecificData")
    return value if isinstance(value, Mapping) else {}


def _add_ref(target: set[str], value: Any) -> None:
    text = _clean_text(value)
    if text:
        target.add(text.upper())


def _booking_references(raw: Iterable[Any]) -> tuple[set[str], set[str], set[str]]:
    """(real BKG references, split/merge/part-load related references, every BKG value as sent, incl. hashed)
    — 08 §7.2 R3 steps 1–2. The first two are upper-cased for comparison."""
    real: set[str] = set()
    related: set[str] = set()
    every: set[str] = set()
    for item in raw:
        body = _event_body(item)
        refs = _get_ci(body, "documentReferences")
        for doc in refs if isinstance(refs, list) else ():
            kind = _clean_text(_get_ci(doc, "documentReferenceType"))
            value = _clean_text(_get_ci(doc, "documentReferenceValue"))
            if not kind or not kind.upper().startswith("BKG") or not value:
                continue
            every.add(value)
            value = value.upper()
            if HASHED_REF_RE.match(value) or UUID_RE.match(value):
                continue
            real.add(value)
        csd = _carrier_specific(item)
        for key in ("originBookingReference", "mergeToBookingReference", "masterPartLoadBookingReference"):
            _add_ref(related, _get_ci(csd, key))
        for key in ("splitToBookingReferences", "partLoadGroupBookingReferences"):
            values = _get_ci(csd, key)
            for value in values if isinstance(values, list) else ():
                _add_ref(related, value)
    return real, related, every


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 08 §6 / §7 — pure helpers (unit-tested directly)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def extract_cursor(header_value: str) -> str:
    """08 §6.3 (UNVERIFIED cursor format): a raw cursor token is returned unchanged; a URL / query containing
    `cursor=` gives its first `cursor` value. The value is percent-decoded but a literal '+' is kept (a base64
    cursor may contain one; httpx re-encodes it as %2B)."""
    value = str(header_value or "").strip()
    if "cursor=" not in value:
        return value
    query = urlsplit(value).query if "?" in value else value
    for part in re.split(r"[&;]", query):
        name, sep, cursor = part.partition("=")
        if sep and unquote(name).strip() == "cursor":
            return unquote(cursor)
    return value


def prefilter_raw_events(raw: list[Any]) -> list[dict[str, Any]]:
    """08 §7.2 R2: keep only objects whose eventDateTime year is in PLAUSIBLE_YEARS. Only `^\\s*(\\d{4})-` is
    matched — the datetime is never parsed here, so sentinels (0001-01-01…, 9999-12-31T23:59:59.9999999) can
    never crash a parser. Missing/blank dates and non-objects are dropped too (DEBUG count)."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for item in raw or ():
        if not isinstance(item, Mapping):
            dropped += 1
            continue
        if _is_withdrawal(item):
            kept.append(item if isinstance(item, dict) else dict(item))
            continue
        value = _get_ci(_event_body(item), "eventDateTime")
        match = _YEAR_RE.match(value) if isinstance(value, str) else None
        if match is None or int(match.group(1)) not in PLAUSIBLE_YEARS:
            dropped += 1
            continue
        kept.append(item if isinstance(item, dict) else dict(item))
    if dropped:
        log.debug("[CMA] dropped %d event(s) without a plausible eventDateTime (sentinel/missing dates, "
                  "non-objects)", dropped)
    return kept


def check_booking_identity(ref: str, raw: list[dict[str, Any]]) -> None:
    """08 §7.2 R3. Real BKG references (not hashed, not UUIDs) must include the requested booking, or the
    booking must be related through split/merge/part-load references; else CarrierLookupError. Without real
    references — normal with API-key access, which gets a 64-hex hash — the check is skipped (CMA-D3)."""
    want = str(ref).strip().upper()
    seen, related, _ = _booking_references(raw)
    if not seen:
        # DEVIATION CMA-D3: the original compared SearchByReference; public access only returns hashed
        # references, so the check can run only with real references (OAuth / business party).
        log.debug("[CMA] booking identity check skipped for %s: no real BKG reference in the events "
                  "(hashed with API-key access, CMA-D3)", want)
        return
    if want in seen or want in related:
        log.debug("[CMA] booking identity check passed for %s (references: %s)", want, ", ".join(sorted(seen)))
        return
    raise CarrierLookupError(WRONG_BOOKING_MSG.format(others=", ".join(sorted(seen)), ref=want))


def resolve_pod_unlocode(events: Sequence[DcsaEvent]) -> str | None:
    """08 §7.2 R5: the UN/LOCODE of events CMA tags `carrierSpecificData.shipmentLocationType == "POD"`
    ("Discharge location - Vessel"); the highest transportCallSequenceNumber wins (None = -1), then the most
    events, then the first seen. Transshipment discharges are tagged "PTS", never "POD". None when nothing is
    tagged: dcsa.py then applies its own POD heuristic (E6)."""
    best_seq: dict[str, int] = {}
    counts: Counter[str] = Counter()
    for e in events:
        kind = _clean_text(_get_ci(_carrier_specific(e.raw), "shipmentLocationType"))
        if not kind or kind.upper() != "POD":
            continue
        loc = (e.unlocode or "").strip().upper()
        if not loc:
            continue
        seq = e.transport_call_sequence
        seq_value = seq if isinstance(seq, int) and not isinstance(seq, bool) else -1
        counts[loc] += 1
        best_seq[loc] = max(best_seq.get(loc, -1), seq_value)
    if not counts:
        return None
    return max(counts, key=lambda loc: (best_seq[loc], counts[loc]))


def container_references(events: Sequence[DcsaEvent]) -> list[str]:
    """08 §7.2 R6: equipmentReference of EQUIPMENT events (any empty indicator), stripped, upper-cased and
    de-duplicated in first-seen order — the original's one `model[]` entry per container."""
    seen: dict[str, None] = {}
    for e in events:
        if (e.event_type or "").strip().upper() != "EQUIPMENT":
            continue
        ref = (e.equipment_reference or "").strip().upper()
        if ref:
            seen.setdefault(ref, None)
    return list(seen)


def is_empty_equipment(event: DcsaEvent) -> bool:
    """08 §7.2 R7 (DEVIATION CMA-D5): an EQUIPMENT move of an EMPTY container is not cargo arriving."""
    if (event.event_type or "").strip().upper() != "EQUIPMENT":
        return False
    indicator = _get_ci(_event_body(event.raw), "emptyIndicatorCode")
    if indicator is None:
        indicator = event.empty_indicator
    return str(indicator or "").strip().upper() == "EMPTY"


def describe_arrival(arrival: DcsaArrival) -> str:
    """08 §7.3: the label staff see in logs and the run report (website wording of the original)."""
    basis = (arrival.basis or "").upper()
    classifier = (arrival.classifier or "").strip().upper()
    if classifier == "ACT":
        return "Arrived at POD (container discharged)" if "DISC" in basis else "Arrived at POD (vessel arrival)"
    if classifier == "EST":
        return "ETA Berth at POD (estimated vessel arrival)"
    if classifier == "PLN":
        return "ETA Berth at POD (planned vessel arrival)"
    return f"{arrival.classifier} arrival at POD"


def _select_per_equipment(arrival_events: Sequence[DcsaEvent], pod: str | None) -> dict[str | None, DcsaArrival]:
    """08 §7.2 R8: dcsa per-equipment arrivals at the POD; str keys normalised, None (vessel level) kept,
    implausible years dropped (defence in depth)."""
    per_eq: dict[str | None, DcsaArrival] = {}
    for key, arrival in dcsa.select_pod_arrival_per_equipment(arrival_events, pod_unlocode=pod).items():
        norm = key.strip().upper() if isinstance(key, str) else None
        if arrival.eta.year not in PLAUSIBLE_YEARS:
            log.debug("[CMA] ignored an arrival with an implausible date for %s: %s", norm or "the vessel",
                      arrival.eta.isoformat())
            continue
        per_eq.setdefault(norm, arrival)
    return per_eq


def evaluate_events(ref: str, raw_events: list[Any], *, endpoint: str = "reference", pages: int = 1) -> EtaResult:
    """08 §7.2–§7.3 (pure): raw CMA items → the booking ETA at the port of discharge.

    R1 `[]` → CarrierLookupError ('shipment not found'); R2 sentinel prefilter (all dropped → NoEtaError, R1b);
    R3 identity check; R4 dcsa.parse_events; R5 POD from CMA's tag; R6 containers; R7 empty moves ignored;
    R8 per-container arrivals; §7.3 policy "latest": the booking is only fully in once its last container
    arrives. Raises only CarrierLookupError / NoEtaError.
    """
    ref = str(ref).strip().upper()
    if not raw_events:
        raise CarrierLookupError(SHIPMENT_NOT_FOUND_MSG.format(ref=ref))                    # R1
    items = prefilter_raw_events(raw_events)                                                # R2
    if not items:
        # DEVIATION CMA-D6: the original reported every unusable answer as "shipment not found".
        raise NoEtaError(NO_USABLE_DATE_MSG.format(ref=ref))                                # R1b
    check_booking_identity(ref, items)                                                      # R3
    try:
        events = dcsa.parse_events(items)                                                   # R4
        # DEVIATION CMA-D1: always the port of discharge (vessel arrival / discharge), never the inland
        # "ETA at FPD" headline the original accepted — inland dates are private events anyway.
        pod = resolve_pod_unlocode(events)                                                  # R5
        containers = container_references(events)                                           # R6
        arrival_events = [e for e in events if not is_empty_equipment(e)]                   # R7
        per_eq = _select_per_equipment(arrival_events, pod)                                 # R8
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CarrierLookupError(COULD_NOT_READ_MSG.format(ref=ref, detail=repr(exc))) from exc
    log.debug("[CMA] booking %s: POD %s", ref,
              pod or "not tagged by CMA (shipmentLocationType) — DCSA heuristic applies (09 §A.3.7)")

    # §7.3 aggregation, policy "latest" (CMA-authoritative). Parity: no containers / any container without a
    # POD arrival → NoEtaError, even when CMA publishes a vessel arrival.
    if not containers:
        raise NoEtaError(NO_CONTAINERS_MSG.format(ref=ref))
    vessel_level = per_eq.get(None)
    container_arrivals = {c: (per_eq.get(c) or vessel_level) for c in containers}
    missing = [c for c, a in container_arrivals.items() if a is None]
    if missing:
        raise NoEtaError(f"No ETA published by CMA for booking {ref} "
                         f"(container{'s' if len(missing) > 1 else ''} {', '.join(missing)}). Can't determine ETA.")
    found: dict[str, DcsaArrival] = {c: a for c, a in container_arrivals.items() if a is not None}
    for c, a in found.items():
        log.info("  [CMA] Container %s: found '%s': %s", c, describe_arrival(a), a.eta.strftime("%m/%d/%Y %H:%M"))
    latest_container, latest = max(found.items(), key=lambda kv: kv[1].eta)   # first max wins on ties
    if len(containers) > 1:
        log.info("  [CMA] Booking has %d containers — using the latest ETA.", len(containers))
    return EtaResult(
        carrier=Carrier.CMA,
        booking=ref,
        eta=latest.eta,
        source=f"CMA {describe_arrival(latest)} at {latest.unlocode or pod or 'POD'}",
        is_actual=(latest.classifier == "ACT"),
        containers=tuple(containers),
        details={
            "pod": pod,
            "endpoint": endpoint,
            "pages": pages,
            "event_count": len(raw_events),
            "latest_container": latest_container,
            "basis": latest.basis,
            "per_container": {
                c: {"eta": a.eta.isoformat(), "classifier": a.classifier, "basis": a.basis, "unlocode": a.unlocode}
                for c, a in found.items()
            },
        },
    )


# ── probe analysis (08 §6.8 step 5) ──
def _count_key(e: DcsaEvent) -> str:
    """"TRANSPORT ARRI EST", "EQUIPMENT DISC ACT LADEN", …"""
    parts = [e.event_type, e.type_code, e.classifier]
    if (e.event_type or "").upper() == "EQUIPMENT" and e.empty_indicator:
        parts.append(e.empty_indicator)
    return " ".join(p for p in parts if p)


def _arrival_dict(arrival: DcsaArrival | None) -> dict[str, Any] | None:
    if arrival is None:
        return None
    return {"eta": arrival.eta.isoformat(), "classifier": arrival.classifier, "basis": arrival.basis,
            "label": describe_arrival(arrival), "unlocode": arrival.unlocode}


def _endpoints_agree(a: EtaResult | Exception, b: EtaResult | Exception) -> bool:
    """08 §6.8: both endpoints give the same ETA and containers (or the same "no ETA" verdict). A lookup
    error on either side (HTTP failure, not found, wrong booking, unreadable payload) is never agreement."""
    if isinstance(a, EtaResult) and isinstance(b, EtaResult):
        return a.eta == b.eta and tuple(a.containers) == tuple(b.containers)
    if isinstance(a, NoEtaError) and isinstance(b, NoEtaError):
        return str(a) == str(b)
    return False


def _analyse(ref: str, fetched: FetchResult) -> tuple[dict[str, Any], EtaResult | Exception]:
    """Per-endpoint analysis for probe(): counts, POD, containers, per-container arrivals, identity check,
    the shared DCSA audit (09 §A.3.11 "explain") and the evaluate_events() outcome."""
    raw = fetched.events
    objects = [item for item in raw if isinstance(item, Mapping)]
    items = prefilter_raw_events(raw)
    tagged = sum(1 for item in objects if _carrier_specific(item))
    analysis: dict[str, Any] = {
        "event_count": len(raw),
        "pages": fetched.pages,
        "plausible_items": len(items),
        "shipment_location_types": sorted({
            text.upper() for item in objects
            if (text := _clean_text(_get_ci(_carrier_specific(item), "shipmentLocationType")))
        }),
        "carrier_specific_data_ratio": round(tagged / len(objects), 3) if objects else 0.0,
    }
    real, _, every = _booking_references(items)
    analysis["booking_refs_seen"] = sorted(every)
    try:
        check_booking_identity(ref, items)
        analysis["identity_check"] = "ran: ok" if real else "skipped (no real BKG reference — hashed, CMA-D3)"
    except CarrierLookupError as exc:
        analysis["identity_check"] = f"ran: {exc}"
    try:
        events = dcsa.parse_events(items)
        pod = resolve_pod_unlocode(events)
        containers = container_references(events)
        arrival_events = [e for e in events if not is_empty_equipment(e)]
        per_eq = _select_per_equipment(arrival_events, pod)
        analysis.update({
            "counts": dict(sorted(Counter(_count_key(e) for e in events).items())),
            "pod": pod,
            "containers": containers,
            "per_container": {c: _arrival_dict(per_eq.get(c) or per_eq.get(None)) for c in containers},
            "vessel_level": _arrival_dict(per_eq.get(None)),
            "explain": dcsa.explain_selection(arrival_events, pod_unlocode=pod, policy="latest"),
        })
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        failure = CarrierLookupError(COULD_NOT_READ_MSG.format(ref=ref, detail=repr(exc)))
        analysis["eta"] = error_text(failure)
        return analysis, failure
    try:
        result = evaluate_events(ref, raw, endpoint=fetched.endpoint, pages=fetched.pages)
    except (CarrierLookupError, NoEtaError) as exc:
        analysis["eta"] = error_text(exc)
        return analysis, exc
    analysis["eta"] = result.to_dict()
    return analysis, result
