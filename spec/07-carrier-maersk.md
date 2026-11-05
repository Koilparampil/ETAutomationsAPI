# 07 — Carrier: Maersk (official Track & Trace Events API, DCSA T&T 2.2)

Implements `src/eta_automations/carriers/maersk.py` against the `CarrierClient`
contract in `03-foundations.md` §9. Shared DCSA parsing/selection lives in
`carriers/dcsa.py` (owned by `09-dcsa-aggregators-grimaldi.md`); this file only
adds what is Maersk-specific.

---

## 1. Summary

**Replaces:** `Maersk/checkETA.py` (original repo). For **every** booking it launched a
*visible* Google Chrome (`headless=False`, `channel="chrome"`), loaded
`https://www.maersk.com/tracking/`, waited 3 s for Akamai's sensor script, dismissed the
cookie banner, deep-linked to `/tracking/<booking>`, retried up to 3× on "No results found",
clicked away up to 5 feature-tour pop-ups (each attempt waits up to 5 s), then scraped the
first container's "Arrives …" date (new UI) or the `container-eta` card (old UI).

**New approach:** one authenticated JSON call per booking to Maersk's official,
developer-portal API **"Track & Trace Events"**:

```
GET https://api.maersk.com/track-and-trace-private/events?carrierBookingReference=<booking>
Consumer-Key: <MAERSK_CLIENT_ID>
Authorization: Bearer <OAuth2 client-credentials token>
```

The response is a list of DCSA Track & Trace v2.2 events (transport, equipment and
shipment events). The ETA is selected from the TRANSPORT `ARRI` events (and EQUIPMENT
`DISC` events as proof of arrival), following the rules in §7.

**Why it is faster:** ~0.3–1.5 s per booking (one HTTPS round trip; a token is fetched
once per ~2 h and cached) versus ~15–40 s per booking for a cold Chrome launch, sensor
warm-up, page loads and pop-up timeouts. Lookups run concurrently (default 3 in flight),
and no window ever opens, so runs can be unattended.

**Why the June 2026 API attempt failed** (commit `7d73c1e`, "Cannot use api as of now"):
1. It called `https://api.maersk.com/synergy/tracking/{booking}?operator=MAEU`. That is the
   **maersk.com website's private backend**, not a developer-portal product. It sits behind
   Akamai Bot Manager, which expects browser-generated sensor telemetry
   (`akamai-bm-telemetry` header, `_abck` cookies). A copied telemetry blob is bound to one
   browser session and expires, so replaying it fails. OBSERVED today: a plain request gets
   Akamai's HTML `403 Access Denied`.
2. It sent only the app's `Consumer-Key` to that endpoint, with no `Authorization: Bearer`,
   and the app is not subscribed to that internal endpoint anyway.
3. The OAuth code (`Maersk/oAuth.py`) was correct in shape. The documented endpoint is
   `/track-and-trace-private/events`, and the app must be **approved for the Track & Trace
   Events product**. Until then Maersk returns HTTP 400 on the token call ("client ID not
   provisioned") or 401/403 on the API call.

---

## 2. Access & onboarding (user actions — do these first)

1. Sign in at **https://developer.maersk.com** with the company's Maersk account. That is the
   same login as maersk.com / `accounts.maersk.com`, and it must be the account (customer
   code) that **books** these shipments. The "private" events API only returns shipments
   your company is a party to (INFERRED: Maersk describes the API as giving "shippers and
   consignees visibility … for shipments booked with A.P. Moller-Maersk"; CMA CGM documents
   the same booking-party rule for its private events).
2. Go to **My Apps**. Open the app whose client ID the old code used (`MAERSK_CLIENT_ID`), or
   create a new app.
3. Add the API product **"Track & Trace Events"**. This is the DCSA T&T 2.2 API whose path is
   `/track-and-trace-private/events`. The exact catalogue label may differ slightly; pick
   the product whose reference shows that path. The portal requires an "intended API usage"
   description, so write something like: *"Daily lookup of ETAs for our own Maersk bookings
   (approx. 150 bookings/day) to notify our consignees."*
4. Wait for approval. Lead time is UNVERIFIED; budget a few days to 2 weeks. Cost is
   UNVERIFIED: the portal supports monetized products, but Track & Trace for a carrier's own
   customers has historically been free. Confirm the plan shown on the product page.
5. Copy the app's **Consumer Key** (a 32-character string that also serves as the OAuth
   `client_id`) and **Secret** into `.env` as `MAERSK_CLIENT_ID` / `MAERSK_CLIENT_SECRET`.
6. Run the probe procedure in §10 before the first real run.

If approval is pending, keep `MAERSK_CLIENT_ID`/`MAERSK_CLIENT_SECRET` empty. `build_client()`
then returns `None`, and Maersk bookings either use the aggregator fallback (spec 09, if
configured) or fail with "Maersk lookups are not configured" (03 §9).

Support: https://developer.maersk.com/support (contact form). When asking about a block or
error, quote the HTTP status, Maersk's `id`/`message` from the error body, and the time of
the request.

---

## 3. Configuration

```python
@dataclass(frozen=True)
class MaerskSettings:
    client_id: str                     # MAERSK_CLIENT_ID  (also sent as the Consumer-Key header)
    client_secret: str                 # MAERSK_CLIENT_SECRET
    base_url: str = "https://api.maersk.com"
    token_url: str = "https://api.maersk.com/customer-identity/oauth/v2/access_token"
    api_version: str | None = None     # sent as API-Version header only when set
    eta_target: Literal["final", "pod"] = "final"
    multi_container_policy: Literal["first", "latest"] = "first"
    eta_date_only: bool = True
    page_limit: int = 100
    max_pages: int = 10
    max_concurrency: int = 3
    rate_per_minute: int = 0           # 0 = no client-side rate limiting
    timeout_s: float = 30.0
```

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `MAERSK_CLIENT_ID` | yes* | — | Consumer Key of your developer.maersk.com app. Sent as `Consumer-Key` on **every** call and as `client_id` in the token request. |
| `MAERSK_CLIENT_SECRET` | yes* | — | App secret (token request only). |
| `MAERSK_BASE_URL` | no | `https://api.maersk.com` | `https://api-stage.maersk.com` is Maersk's pre-production gateway. Use it only if Maersk provisions your app there (UNVERIFIED). |
| `MAERSK_TOKEN_URL` | no | `https://api.maersk.com/customer-identity/oauth/v2/access_token` | OAuth2 token endpoint. |
| `MAERSK_API_VERSION` | no | *(unset)* | If set (e.g. `1`), sent as the `API-Version` request header. The API spec says it must hold only the MAJOR version and must match the URI version. Leave unset unless Maersk support asks for it. |
| `MAERSK_ETA_TARGET` | no | `final` | `final` = arrival at the container's last location on Maersk's route, matching the maersk.com "Arrives …" date the original scraped. `pod` = vessel arrival at the port of discharge. See §7. |
| `MAERSK_MULTI_CONTAINER_POLICY` | no | `first` | Passed to `dcsa.booking_arrival(policy=...)`. `first` = parity with the original, which read the first container on the page. `latest` = the booking is "in" when its last container arrives. |
| `MAERSK_ETA_DATE_ONLY` | no | `true` | Truncate the ETA to 00:00 local. Parity: the current maersk.com page shows dates without times, and MSC also returns dates only. See §7.5. |
| `MAERSK_PAGE_LIMIT` | no | `100` | `limit` query parameter, 1–100. The documented default is 100; larger values are UNVERIFIED, so do not exceed 100. |
| `MAERSK_MAX_PAGES` | no | `10` | Safety cap on pagination per booking. |
| `MAERSK_MAX_CONCURRENCY` | no | `3` | Semaphore size for Maersk HTTP calls (03 §10). |
| `MAERSK_RATE_PER_MINUTE` | no | `0` | If > 0, wraps calls in `http.AsyncLimiter(rate, 60)`. Maersk's quota is UNVERIFIED; use this only if 429s appear. |
| `MAERSK_TIMEOUT_SECONDS` | no | `30` | Read timeout for Maersk calls (connect stays 10 s). |

\* "Required" means required for Maersk to be *configured*. When either value is empty,
`settings.maersk` is `None` (03 §12).

Validation in `config.py` raises `ConfigError` with a message naming the variable when:
- `MAERSK_ETA_TARGET` is not in `{final, pod}`;
- `MAERSK_MULTI_CONTAINER_POLICY` is not in `{first, latest}`;
- `MAERSK_PAGE_LIMIT` is not in 1–100;
- `MAERSK_MAX_PAGES`, `MAERSK_MAX_CONCURRENCY` or `MAERSK_TIMEOUT_SECONDS` is < 1;
- `MAERSK_RATE_PER_MINUTE` is < 0.

Strip whitespace from both credentials. Never log them.

Token cache: `TokenStore` key **`"maersk"`** (03 §7).

---

## 4. Endpoints

### 4.1 Token — `POST {MAERSK_TOKEN_URL}`

| | |
|---|---|
| Headers | `Consumer-Key: <client_id>`, `Content-Type: application/x-www-form-urlencoded`, `Accept: application/json` |
| Body (form) | `grant_type=client_credentials&client_id=<client_id>&client_secret=<client_secret>` |
| 200 | `{"access_token": "<JWT>", "token_type": "Bearer", "expires_in": <seconds>, "scope": "...", "id_token": "..."}`. Maersk documents a token lifetime of 2 hours. Use `expires_in` when present. |
| 400 | `{"error": "...", "error_description": "..."}`. Maersk documents this as "client ID not provisioned on the authorisation server", which typically means API access was requested but **not yet approved** (or the secret is wrong). |
| 401 | Gateway JSON `{"error":[{"code":"ERR_GW_001","message":"API Key Validation Failed, please check the API key","reason":"Invalid or expired Key","status":"401"}]}`. OBSERVED with an invalid key: the gateway checks `Consumer-Key` before anything else. |

### 4.2 Events — `GET {MAERSK_BASE_URL}/track-and-trace-private/events`

Headers (all required except `API-Version`):

| Header | Value |
|---|---|
| `Consumer-Key` | `<client_id>`. **Always send it.** OBSERVED: requests without it are rejected at the Akamai edge with an HTML 403 "Access Denied" before reaching the API gateway. |
| `Authorization` | `Bearer <access_token>` |
| `Accept` | `application/json`. The API can also stream `application/stream+json` / `text/event-stream`, so be explicit. |
| `API-Version` | only when `MAERSK_API_VERSION` is set |

Query parameters used:

| Param | Value | Notes |
|---|---|---|
| `carrierBookingReference` | the 9-digit booking, e.g. `267546627` | API pattern `[a-zA-Z0-9]{9}`. At least one of `carrierBookingReference` / `transportDocumentReference` / `equipmentReference` is required. |
| `eventType` | `TRANSPORT,EQUIPMENT` | Comma-separated, OR'ed. Shipment (document) events are not needed. `probe()` omits this parameter to fetch everything. |
| `limit` | `MAERSK_PAGE_LIMIT` (default `100`) | |
| `cursor` | only on follow-up pages | "For the initial request … null or 1." |

Not used: `transportEventTypeCode` / `equipmentEventTypeCode` filters. The spec warns that
contradictory filter combinations silently return an empty list, so filter client-side.

Responses:

| Status | Body | Meaning / mapping |
|---|---|---|
| 200 | `{"events": [ <event>, ... ]}` | Maersk **wraps** the DCSA array in an object. Accept a bare JSON array too, as the DCSA standard returns one. An empty list means not found or not visible to your account (§6.4). |
| 400 | `ApiError` | Bad parameter, e.g. a malformed booking reference. |
| 401 | `ApiError` or gateway `ERR_GW_001` JSON | Token expired or invalid, or `Consumer-Key` invalid. `request_with_auth()` refreshes once. |
| 403 | `ApiError` JSON | App not permitted to use this endpoint (product not approved). |
| 403 | **text/html** "Access Denied … errors.edgesuite.net" | Akamai edge rejection. Usually a missing `Consumer-Key` header; otherwise contact Maersk support with the Reference #. **Do not try to work around it.** |
| 404 | `ApiError` | Booking not found. |
| 429 | — | Rate limited. `http.request()` retries with `Retry-After` (03 §6). |
| 500 | `ApiError` | Retried by `http.request()` (GET is idempotent). |

`ApiError` shape: `{"id": str, "method": str, "requestUri": str, "status": int, "timestamp":
"dd-MM-yyyy hh:mm:ss", "message": str, "debugMessage": str, "subErrors": [{"field",
"rejectedValue", "message"}]}`.

Pagination: response headers `Current-Page`, `Next-Page`, `Prev-Page`, `First-Page`,
`Last-Page`. The spec calls them "a link to the next page", with example value `"11"`, so the
format is UNVERIFIED and both forms must be supported (§6.3). `Next-Page` is omitted on the
last page.

### 4.3 Event fields used (Maersk Track & Trace Events v1.1.1 = DCSA T&T 2.2)

Common to all events: `eventID`, `eventType` (`TRANSPORT` | `EQUIPMENT` | `SHIPMENT`),
`eventClassifierCode` (`PLN` | `EST` | `ACT`), `eventDateTime`, `eventCreatedDateTime`, and
optionally `references`.

- **`eventDateTime`**: "the **local** date and time where the event took place or will take
  place, in ISO 8601", e.g. `2019-11-12T07:41:00+08:00`. Shipment events use UTC instead.
  Future (`EST`/`PLN`) events are included, which is how ETAs are published.
- **`eventCreatedDateTime`**: UTC timestamp of when this event *record* was created. A newer
  record supersedes an older one, so this is how ETA revisions appear.

TRANSPORT event fields:
- `transportEventTypeCode` (`ARRI` | `DEPA`), `delayReasonCode`, `changeRemark`, `documentReferences[]`.
- `transportCall`:
  - `transportCallID`, `carrierServiceCode`, `exportVoyageNumber`, `importVoyageNumber`, `transportCallSequenceNumber`;
  - `UNLocationCode`, `facilityCode`, `facilityCodeListProvider`;
  - `facilityTypeCode` (`POTE` port terminal, `RAMP`, `INTE`, `DEPO`, …);
  - `modeOfTransport` (`VESSEL` | `RAIL` | `TRUCK` | `BARGE`);
  - `location{locationName, UNLocationCode, facilityCode, …}`;
  - `vessel{vesselIMONumber, vesselName, …}`.
- Transport events carry **no** `equipmentReference`. They are shared by every container on
  that transport call.

EQUIPMENT event fields:
- `equipmentEventTypeCode` (`LOAD`, `DISC`, `GTIN`, `GTOT`, `STUF`, `STRP`, `PICK`, `DROP`, `RSEA`, `RMVD`, `INSP`);
- `equipmentReference` (container number), `ISOEquipmentCode`, `emptyIndicatorCode`;
- `eventLocation{UNLocationCode, facilityCode, …}`, `transportCall{…}`, `documentReferences[]`.

**For the 09 owner:** `dcsa.parse_events()` must read the UN/LOCODE from
`transportCall.UNLocationCode`, then `transportCall.location.UNLocationCode`, then (for
equipment events) `eventLocation.UNLocationCode`. Read the facility code from the same
places, and the mode from `transportCall.modeOfTransport`. The fixtures in §9 use all of
these shapes, and `test_dcsa_parses_maersk_shapes` (§9.3) checks them.

---

## 5. Auth / token lifecycle

- **Grant:** OAuth2 client credentials (§4.1). The token is a JWT, valid about 2 h.
- **Cache entry**, `TokenStore` key `"maersk"`:
  `{"access_token", "expires_at", "token_type", "scope", "obtained_at", "key_fingerprint"}`.
  - `expires_at = now + expires_in`. If `expires_in` is missing or invalid, use
    `token_store.jwt_exp(access_token)` (the JWT `exp` claim, 03 §7). Failing that, use
    `now + 7000`.
  - `key_fingerprint = sha256(client_id.encode()).hexdigest()[:16]`. A cached token whose
    fingerprint differs from the current `MAERSK_CLIENT_ID` is ignored, so changing apps
    never reuses another app's token.
- **Freshness:** `token_store.is_fresh(entry)` (120 s skew) **and** a matching fingerprint.
- **Concurrency:** `get_token()` re-checks the cache inside an `asyncio.Lock`. Only one
  coroutine requests a token (03 §8). **`MaerskAuth` never acquires the Maersk semaphore**:
  callers already hold a semaphore slot while awaiting the token, so acquiring it again
  could deadlock.
- **401 on the events call:** `request_with_auth()` calls `invalidate()`, which deletes the
  `"maersk"` entry, fetches a new token, and retries once. A second 401 raises `AuthError`.
  Do not pass `403` as a "token expired" status: Maersk's 403 means "not permitted".

---

## 6. Module design — `src/eta_automations/carriers/maersk.py`

```python
from __future__ import annotations

import asyncio, dataclasses, hashlib, logging, re, time
from collections import Counter
from datetime import datetime, time as dtime
from typing import Any, Callable, Literal, Sequence

import httpx

from ..auth import request_with_auth
from ..clock import Clock, SystemClock
from ..config import MaerskSettings, Settings
from ..errors import AuthError, CarrierLookupError, NoEtaError, ServiceError
from ..http import AsyncLimiter, json_body, new_async_client, request
from ..logging_setup import redact
from ..models import Carrier, EtaResult
from ..token_store import TokenStore, is_fresh, jwt_exp
from . import dcsa
from .dcsa import DcsaArrival, DcsaEvent

log = logging.getLogger("eta.carrier.maersk")

EVENTS_PATH = "/track-and-trace-private/events"
TOKEN_STORE_KEY = "maersk"
DEFAULT_TOKEN_LIFETIME_S = 7000
BOOKING_RE = re.compile(r"^[A-Za-z0-9]{9}$")
CLASSIFIER_RANK = {"ACT": 3, "EST": 2, "PLN": 1}
ONBOARDING_HINT = (
    "Check MAERSK_CLIENT_ID / MAERSK_CLIENT_SECRET and that your developer.maersk.com app is "
    "approved for the 'Track & Trace Events' API product (spec 07 §2)."
)
EMPTY_EVENTS_MSG = (
    "Maersk returned no tracking events for booking {ref}. It may not exist, may be too new, "
    "or may not be linked to the Maersk customer account that owns MAERSK_CLIENT_ID."
)
```

> `token_store.jwt_exp(token: str) -> float | None` is the helper described in 03 §7: it
> base64url-decodes the JWT payload without verifying it, and returns `exp` as epoch seconds
> or `None`. If `token_store.py` does not yet export it, add it there; it is shared.

### 6.1 `MaerskAuth` (implements `TokenProvider`, 03 §8)

```python
class MaerskAuth:
    def __init__(self, settings: MaerskSettings, token_store: TokenStore, http: httpx.AsyncClient,
                 *, wall_time: Callable[[], float] = time.time) -> None: ...
    async def get_token(self) -> str: ...
    async def invalidate(self) -> None: ...
    def cached_entry(self) -> dict[str, Any] | None: ...  # for probe(); returns a copy WITHOUT access_token
```

`get_token()`:
1. If the cached entry is valid (§5), return `entry["access_token"]`.
2. Otherwise, inside `self._lock` (`asyncio.Lock`), re-check the cache and return it if it is
   now valid.
3. Otherwise call `request(self._http, "POST", settings.token_url, service="maersk",
   expected=(200,), idempotent=True, headers=…, data=…)` as in §4.1.
4. On `ServiceError`, raise `AuthError(self._explain(exc))` chained from the original error.
   `_explain` builds the message from the status:
   - **400:** "Maersk token request rejected (HTTP 400). Maersk documents this as 'client ID
     not provisioned' — usually API access was requested but not yet approved, or the secret
     is wrong." + `ONBOARDING_HINT`
   - **401** (with `ERR_GW_001` in the body): "Maersk gateway rejected the Consumer-Key
     (ERR_GW_001 'Invalid or expired Key'). MAERSK_CLIENT_ID must be the app's Consumer Key."
   - **403:** "Maersk refused the token request (HTTP 403) — the key may be revoked."
     + `ONBOARDING_HINT`
   - **Anything else:** `f"Maersk token request failed: {exc}"`.
5. Parse the body with `json_body()`. `access_token` must be a non-empty `str`; otherwise
   raise `AuthError(f"Maersk token response had no access_token (keys: {sorted(body)})")`.
6. Compute `expires_at` (§5), `put()` the entry, and return the token. Log
   `"[Maersk] OAuth token obtained (valid ~%d min)"` at INFO. **Never log the token.**

`invalidate()` deletes the `"maersk"` key.

### 6.2 `MaerskClient` (implements `CarrierClient`, 03 §9)

```python
class MaerskClient:
    carrier = Carrier.MAERSK

    def __init__(self, settings: MaerskSettings, token_store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None) -> None:
        # self._http = http or new_async_client(base_url=settings.base_url,
        #                 timeout=httpx.Timeout(settings.timeout_s, connect=10.0)); remember ownership
        # self._sem = asyncio.Semaphore(settings.max_concurrency)
        # self._limiter = AsyncLimiter(settings.rate_per_minute, 60.0) if settings.rate_per_minute > 0 else None
        # self._auth = MaerskAuth(settings, token_store, self._http)
        # self._clock = clock or SystemClock()
        ...

    async def get_eta(self, booking: str) -> EtaResult: ...
    async def probe(self, booking: str) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...          # close self._http only if this instance created it

    # internals
    async def _fetch_events(self, ref: str, *, event_types: str | None) -> tuple[list[dict], list[dict]]: ...
    def _select(self, ref: str, events: Sequence[DcsaEvent]) -> EtaResult: ...
    def _lookup_error(self, ref: str, exc: ServiceError) -> CarrierLookupError: ...


def build_client(settings: Settings, token_store: TokenStore) -> MaerskClient | None:
    return MaerskClient(settings.maersk, token_store) if settings.maersk else None
```

When `http` is injected (tests), it must already have `base_url` set, or the client must
build absolute URLs. **Always build absolute URLs** with `settings.base_url.rstrip("/") +
EVENTS_PATH`, so both cases work.

### 6.3 `get_eta(booking)` — step by step

1. `ref = booking.strip().upper()`. If not `BOOKING_RE.fullmatch(ref)`, raise
   `CarrierLookupError(f"'{booking}' is not a valid Maersk booking reference (expected 9 letters/digits).")`
   **without any HTTP call**.
2. Log at INFO: `"[Maersk] Looking up ETA for booking %s..."`.
3. Call `raw_events, _ = await self._fetch_events(ref, event_types="TRANSPORT,EQUIPMENT")`.
   Map exceptions as follows (chain each with `from exc`):
   - `CarrierLookupError` → re-raise unchanged;
   - `AuthError` → `CarrierLookupError(f"Maersk API authentication failed: {exc}")`;
   - `ServiceError` → `self._lookup_error(ref, exc)` (§6.4);
   - `httpx.HTTPError` → `CarrierLookupError(f"Maersk API network error: {exc!r}")`.
4. If `raw_events` is empty, raise `CarrierLookupError(EMPTY_EVENTS_MSG.format(ref=ref))`.
   This matches the original's "No results found" → RuntimeError → "Carrier Lookup Failed."
5. `events = dcsa.parse_events(raw_events)`. On `KeyError`, `TypeError` or `ValueError`,
   raise `CarrierLookupError(f"Maersk returned events in an unexpected shape: {exc}")`.
6. `result = self._select(ref, events)` (§7). `_select` may raise `NoEtaError`; let it
   propagate.
7. Log at INFO: `"[Maersk] ETA=%s (%s)"`, formatted `%m/%d/%Y`, plus `result.source`.
   Return `result`.

`_fetch_events(ref, *, event_types)`:

```
url     = base_url.rstrip("/") + EVENTS_PATH
params  = {"carrierBookingReference": ref, "limit": str(page_limit)}
if event_types: params["eventType"] = event_types
headers = {"Consumer-Key": client_id, "Accept": "application/json"}
if api_version: headers["API-Version"] = api_version
events, summaries = [], []
for page_no in 1..max_pages:
    t0 = monotonic()
    async with self._sem:
        resp = await request_with_auth(self._http, "GET", url, service="maersk", auth=self._auth,
                                       params=params, headers=headers, expected=(200,),
                                       limiter=self._limiter)
    body = json_body(resp, service="maersk")
    page = body["events"] if isinstance(body, dict) and isinstance(body.get("events"), list) \
           else body if isinstance(body, list) \
           else raise CarrierLookupError(f"Maersk returned an unexpected payload shape "
                                         f"(top-level keys: {sorted(body)[:10] if isinstance(body, dict) else type(body).__name__})")
    events.extend(e for e in page if isinstance(e, dict))
    nxt = resp.headers.get("Next-Page", "").strip(); cur = resp.headers.get("Current-Page", "").strip()
    summaries.append({"page": page_no, "status": resp.status_code, "events": len(page),
                      "elapsed_ms": int((monotonic()-t0)*1000), "current_page": cur or None,
                      "next_page": nxt or None, "url": str(resp.request.url)})   # URL has no secrets
    if not nxt or nxt == cur or not page: break
    if nxt.lower().startswith(("http://", "https://")):
        if httpx.URL(nxt).host != httpx.URL(url).host:   # never follow a link to another host
            log.warning("[Maersk] ignoring Next-Page link to foreign host"); break
        url, params = nxt, None                          # the link already carries the query
    elif nxt.startswith("/"):
        url, params = base_url.rstrip("/") + nxt, None
    else:
        params = {**(params or {}), "cursor": nxt}
else:
    log.warning("[Maersk] stopped after %d pages for %s (MAERSK_MAX_PAGES)", max_pages, ref)
return events, summaries
```

Hold the semaphore only around the HTTP call (03 §10). Token acquisition happens inside
`request_with_auth()` and does not take the semaphore (§5).

### 6.4 `_lookup_error(ref, exc)` — mapping `ServiceError` to `CarrierLookupError`

| Condition | Message |
|---|---|
| `status == 404` | `f"Maersk has no shipment for booking {ref} (HTTP 404). Check the number; bookings not linked to your Maersk customer account are not visible to the API."` |
| `status == 403` and body excerpt contains `Access Denied` or `edgesuite` | `"Maersk's edge network refused the request (HTTP 403 'Access Denied' HTML page). This normally means the Consumer-Key header was missing or the request was blocked upstream. Re-run `eta probe maersk`; if it persists, contact Maersk developer support with the Reference # from the log."` (log the excerpt at WARNING; it contains Akamai's reference number, not secrets) |
| `status == 403` (JSON) | `f"Maersk API refused access (HTTP 403): the app is not approved for Track & Trace Events, or booking {ref} is not visible to your account. {ONBOARDING_HINT}"` |
| `status == 401` | `f"Maersk rejected the credentials (HTTP 401). {ONBOARDING_HINT}"` |
| `status == 400` | `f"Maersk rejected the request for {ref} (HTTP 400): {exc}"` |
| `status == 429` | `"Maersk API rate limit hit (HTTP 429) after retries — lower MAERSK_MAX_CONCURRENCY or set MAERSK_RATE_PER_MINUTE."` |
| otherwise | `f"Maersk API error for {ref}: {exc}"` |

---

## 7. Business logic — choosing the ETA

### 7.1 What the original returned (parity target)

The original scraped the first container accordion on maersk.com and read its
**"Arrives <terminal>"** date. If Maersk had revised the ETA, it read the warning heading
instead (e.g. `"22 Oct 2026 (ETA)"`). On the old layout it read the `container-eta` card,
which shows the container's estimated arrival at its **final destination** on Maersk's
transport plan. That card is backed by `eta_final_delivery` in the website's synergy JSON
(INFERRED from the page structure; the website is not reachable from here). For port-to-port
(CY/CY) bookings into US ports, the final destination **is** the port of discharge. For
inland/IPI bookings (vessel to a US port, then rail to an inland ramp) it is the **inland
ramp**.

So the default target is the **final arrival** (`MAERSK_ETA_TARGET=final`). The `pod` option
remains available, and the probe (§10) prints both so the user can confirm which matches
the website.

### 7.2 Module-level selection functions (pure, unit-tested)

```python
def select_final_arrival(events: Sequence[DcsaEvent]) -> tuple[DcsaArrival, str | None] | None:
    """Arrival at the last location of the route (any mode). Returns (arrival, mode_of_transport)."""

def choose_arrival(events: Sequence[DcsaEvent], *, target: Literal["final", "pod"],
                   policy: Literal["first", "latest"]) -> tuple[DcsaArrival, str | None] | None:
    """Applies MAERSK_ETA_TARGET / MAERSK_MULTI_CONTAINER_POLICY. Returns (arrival, mode)."""
```

Helpers:

```python
def _naive(dt: datetime | None) -> datetime | None:   # strip tzinfo if any (defensive: keeps comparisons legal)
def _rank_key(e: DcsaEvent) -> tuple:                  # (CLASSIFIER_RANK.get((e.classifier or "").upper(), 0),
                                                       #  _naive(e.created_datetime) or datetime.min,
                                                       #  _naive(e.event_datetime))
```

**`select_final_arrival(events)`**:

1. Collect `arrivals`: `e.event_type == "TRANSPORT"`, `e.type_code == "ARRI"` and `e.unlocode`
   is set. If there are none, return `None`.
2. Group `arrivals` by `e.unlocode.upper()` (city-level UN/LOCODE). Facility codes are often
   missing on PLN/EST records, so do not group by facility.
3. For each group, the **representative** is `max(group, key=_rank_key)`. This prefers ACT
   over EST over PLN, then the newest record (revision), then the later time.
4. Build `departure_locs`: the set of `e.unlocode.upper()` for every TRANSPORT `DEPA` event.
   A location the cargo departs from (origin, transshipment port, or a port with onward rail)
   is not the final destination. This test does not depend on dates, so it is immune to stale
   PLN dates.
5. Set `pool = [loc for loc in reps if loc not in departure_locs]`. If that is empty, use
   `list(reps)`.
6. `final_loc = max(pool, key=lambda loc: (_naive(reps[loc].event_datetime),
   CLASSIFIER_RANK.get(reps[loc].classifier, 0)))`, and let `rep = reps[final_loc]`.
7. **Proof of arrival.** If `rep.classifier != "ACT"`, collect `discs`: EQUIPMENT events
   with `type_code == "DISC"`, `classifier == "ACT"`, and `unlocode.upper() == final_loc`.
   If there are any, take the earliest by `event_datetime` and return:
   `(DcsaArrival(eta=first.event_datetime, classifier="ACT", unlocode=final_loc,
   facility_code=first.facility_code or rep.facility_code,
   equipment_reference=first.equipment_reference,
   basis=f"EQUIPMENT DISC/ACT at {final_loc} (final destination)"), rep.mode_of_transport)`.
8. Otherwise return:
   `(DcsaArrival(eta=rep.event_datetime, classifier=rep.classifier, unlocode=final_loc,
   facility_code=rep.facility_code, equipment_reference=None,
   basis=f"TRANSPORT ARRI/{rep.classifier} at {final_loc}{'/' + rep.facility_code if rep.facility_code else ''} by {rep.mode_of_transport or 'unknown mode'} (final destination)"),
   rep.mode_of_transport)`.

**`choose_arrival(events, *, target, policy)`**:

```
if target == "pod":
    a = dcsa.booking_arrival(events, policy=policy)
    return (a, "VESSEL") if a else None
final = select_final_arrival(events)
if final is None:                                   # no TRANSPORT ARRI at all → let dcsa use DISC evidence
    a = dcsa.booking_arrival(events, policy=policy)
    return (a, "VESSEL") if a else None
arrival, mode = final
if mode in (None, "VESSEL"):                         # final destination is a seaport → reuse shared DCSA rules
    a = dcsa.booking_arrival(events, policy=policy, pod_unlocode=arrival.unlocode)
    if a is not None and (a.unlocode or "").upper() in ("", arrival.unlocode.upper()):
        return (a, "VESSEL")                         # gets per-container policy + DISC handling from 09
return final                                         # inland (RAIL/TRUCK/BARGE) final leg → booking-level answer
```

Why delegate when the final leg is by vessel: the shared `dcsa.booking_arrival()` owns
classifier precedence, revision handling and the per-container `first`/`latest` policy that
every DCSA carrier uses. `select_final_arrival()` is only needed to find *which* location is
final, and to answer directly for inland legs. Transport events carry no
`equipmentReference`, so an inland leg's ETA is per booking anyway.

### 7.3 `_select(ref, events)` → `EtaResult`

```
picked = choose_arrival(events, target=s.eta_target, policy=s.multi_container_policy)
if picked is None:
    counts = Counter(f"{e.event_type}/{e.type_code}/{e.classifier}" for e in events)
    raise NoEtaError(f"No ETA published by Maersk for booking {ref} "
                     f"(events seen: {dict(sorted(counts.items()))}). Can't determine ETA.")
arrival, mode = picked
eta = datetime.combine(arrival.eta.date(), dtime.min) if s.eta_date_only else arrival.eta
containers = tuple(sorted({e.equipment_reference.strip().upper()
                           for e in events if e.equipment_reference}))
base = dcsa.to_eta_result(arrival, carrier=Carrier.MAERSK, booking=ref, containers=containers)
return dataclasses.replace(
    base,
    eta=eta,
    source=f"Maersk {arrival.basis}",
    details={**dict(base.details), "target": s.eta_target, "policy": s.multi_container_policy,
             "mode": mode, "unlocode": arrival.unlocode, "facility": arrival.facility_code,
             "classifier": arrival.classifier, "eta_exact": arrival.eta.isoformat(),
             "event_count": len(events)},
)
```

`is_actual` comes from `to_eta_result` (classifier `ACT`). Maersk **never** raises
`CustomsHoldError`; that heuristic is MSC-only, as in the original.

### 7.4 Time zones

`eventDateTime` is local time at the event location with a UTC offset. `dcsa.parse_events()`
parses it and **drops the offset, keeping the local wall-clock time** (03 §4). For example,
`2026-11-16T07:00:00-05:00` becomes `2026-11-16 07:00`, which is what maersk.com displays.
`eventCreatedDateTime` is UTC and is only compared with other created timestamps.

### 7.5 Date-only ETA (parity)

The current maersk.com layout, which the original read first, shows **dates without times**
(`"21 May 2026"`), so the original's ETA was midnight local. With `MAERSK_ETA_DATE_ONLY=true`
(the default) the new client truncates to 00:00. That keeps the ETA-day behaviour identical
to today's: the subject says "ARRIVED ON …", consistent with the invoice-number "A" suffix
that 01-business-rules applies when `eta.date() <= today`. The exact time stays in
`details["eta_exact"]` for the run report.

### 7.6 Deviations from the original

- **DEVIATION (data source):** structured carrier events replace scraped page text. ETA
  revisions are handled explicitly, through classifier precedence and the newest
  `eventCreatedDateTime`, instead of relying on whichever widget rendered.
- **DEVIATION (no retries on "no results"):** the original reloaded up to 3× because the
  *bot check* produced false "No results found" pages. The API has no such flakiness. An empty
  list is a real answer and fails immediately with a clear message. Transport-level failures
  are still retried by `http.request()`.
- **Parity kept:** first-container policy (configurable), final-destination target
  (configurable), midnight ETA (configurable), and error categories: no booking or empty
  → "Carrier Lookup Failed."; no date → "Can't determine ETA."

---

## 8. Fallbacks (honest assessment)

| Option | Verdict |
|---|---|
| **Maersk Track & Trace Events API** (this spec) | **Recommended.** Official, licensed, fast, stable DCSA contract. Requires product approval (§2). |
| **Third-party aggregator** (spec 09, `carriers/aggregator.py`) | **Use while approval is pending**, or if Maersk refuses access. Licensed data, one integration for all carriers, but paid, and some providers are asynchronous (create a tracking request, then poll or receive a webhook). Router wiring is owned by spec 09. |
| **Website backend `api.maersk.com/synergy/tracking/{ref}`** | **Rejected.** Not a published product. It is protected by Akamai Bot Manager, which requires browser sensor telemetry. Calling it without a browser would mean defeating bot protection (TLS fingerprint impersonation, telemetry replay or cookie harvesting), which is against Maersk's terms and brittle. This spec does not do it. OBSERVED: a plain request returns HTML 403 "Access Denied". |
| **Other Maersk tracking products** ("Track and Trace Plus", "MEC Tracking", or a future DCSA 3.0 `/v3/events` version) | UNVERIFIED. They may offer richer per-container ETAs. If Maersk support recommends one, ask for its OpenAPI document; only `_fetch_events` and the payload parsing change behind the same `CarrierClient` boundary. |
| **Manual** | Unconfigured or failed Maersk bookings land in `Failed_To_Process.txt` with "Carrier Lookup Failed." and the reason, for staff to check by hand. |
| **Old Playwright tool** | Can still be run from the old repo as a stop-gap for Maersk-only batches. It is deliberately **not** part of this program. |

---

## 9. Fixtures & tests

All fixtures are synthetic. Booking numbers follow the router pattern `2\d{8}` but are fake.

### 9.1 Fixture files (`tests/fixtures/maersk/`)

**`token_ok.json`**
```json
{"access_token": "eyJhbGciOiJub25lIiwidHlwIjoiSldUIn0.eyJzdWIiOiJzeW50aGV0aWMtbWFlcnNrLWFwcCIsImV4cCI6MTc5Mzg2MDAwMH0.c3ludGhldGlj",
 "token_type": "Bearer", "expires_in": 7199, "scope": "openid", "id_token": "synthetic-id-token"}
```

**`token_no_expires_in.json`**: the same `access_token` with no `expires_in` key. The JWT
`exp` is `1793860000`, i.e. 2026-11-05T06:26:40Z.

**`token_error_400.json`**
```json
{"error": "invalid_client", "error_description": "Client not provisioned"}
```

**`error_gateway_401.json`** (OBSERVED shape)
```json
{"error":[{"code":"ERR_GW_001","message":"API Key Validation Failed, please check the API key","reason":"Invalid or expired Key","status":"401"}]}
```

**`error_404.json`**
```json
{"id": "a1b2c3", "method": "GET", "requestUri": "/track-and-trace-private/events", "status": 404,
 "timestamp": "05-11-2026 09:00:00", "message": "Not Found", "debugMessage": "No shipment found"}
```

**`edge_403.html`** (OBSERVED shape; serve it with `content-type: text/html`)
```html
<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD><BODY><H1>Access Denied</H1>
You don't have permission to access "http&#58;&#47;&#47;api&#46;maersk&#46;com&#47;track&#45;and&#45;trace&#45;private&#47;events&#63;" on this server.<P>
Reference&#32;&#35;18&#46;0000000&#46;1793852633&#46;00000000
<P>https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;18&#46;0000000&#46;1793852633&#46;00000000</P></BODY></HTML>
```

**`events_in_transit.json`**: booking `210000001`, Ningbo to New York/Newark, two containers.
```json
{"events": [
  {"eventID": "E1", "eventType": "EQUIPMENT", "eventClassifierCode": "ACT",
   "eventDateTime": "2026-10-18T10:15:00+08:00", "eventCreatedDateTime": "2026-10-18T02:20:00Z",
   "equipmentEventTypeCode": "GTIN", "equipmentReference": "MSKU1111111", "ISOEquipmentCode": "45G1",
   "emptyIndicatorCode": "LADEN",
   "eventLocation": {"locationName": "Ningbo Beilun", "UNLocationCode": "CNNGB", "facilityCode": "NBCT", "facilityCodeListProvider": "SMDG"},
   "transportCall": {"modeOfTransport": "TRUCK", "UNLocationCode": "CNNGB"},
   "documentReferences": [{"documentReferenceType": "BKG", "documentReferenceValue": "210000001"}]},
  {"eventID": "E2", "eventType": "EQUIPMENT", "eventClassifierCode": "ACT",
   "eventDateTime": "2026-10-20T08:40:00+08:00", "eventCreatedDateTime": "2026-10-20T00:45:00Z",
   "equipmentEventTypeCode": "LOAD", "equipmentReference": "MSKU1111111", "emptyIndicatorCode": "LADEN",
   "eventLocation": {"UNLocationCode": "CNNGB", "facilityCode": "NBCT"},
   "transportCall": {"transportCallID": "TC-NGB-01", "modeOfTransport": "VESSEL", "UNLocationCode": "CNNGB",
                     "facilityCode": "NBCT", "facilityTypeCode": "POTE", "exportVoyageNumber": "442E",
                     "transportCallSequenceNumber": 1, "vessel": {"vesselIMONumber": "9778791", "vesselName": "SYNTHETIC MAERSK"}}},
  {"eventID": "E3", "eventType": "EQUIPMENT", "eventClassifierCode": "ACT",
   "eventDateTime": "2026-10-20T09:05:00+08:00", "eventCreatedDateTime": "2026-10-20T01:10:00Z",
   "equipmentEventTypeCode": "LOAD", "equipmentReference": "MSKU2222222", "emptyIndicatorCode": "LADEN",
   "eventLocation": {"UNLocationCode": "CNNGB", "facilityCode": "NBCT"},
   "transportCall": {"transportCallID": "TC-NGB-01", "modeOfTransport": "VESSEL", "UNLocationCode": "CNNGB",
                     "facilityCode": "NBCT", "facilityTypeCode": "POTE", "transportCallSequenceNumber": 1}},
  {"eventID": "T1", "eventType": "TRANSPORT", "eventClassifierCode": "ACT",
   "eventDateTime": "2026-10-20T18:00:00+08:00", "eventCreatedDateTime": "2026-10-20T10:30:00Z",
   "transportEventTypeCode": "DEPA",
   "transportCall": {"transportCallID": "TC-NGB-01", "modeOfTransport": "VESSEL", "UNLocationCode": "CNNGB",
                     "facilityCode": "NBCT", "facilityTypeCode": "POTE", "exportVoyageNumber": "442E",
                     "transportCallSequenceNumber": 1, "location": {"locationName": "Ningbo", "UNLocationCode": "CNNGB"},
                     "vessel": {"vesselIMONumber": "9778791", "vesselName": "SYNTHETIC MAERSK"}},
   "documentReferences": [{"documentReferenceType": "BKG", "documentReferenceValue": "210000001"}]},
  {"eventID": "T2", "eventType": "TRANSPORT", "eventClassifierCode": "PLN",
   "eventDateTime": "2026-11-14T06:00:00-05:00", "eventCreatedDateTime": "2026-10-01T12:00:00Z",
   "transportEventTypeCode": "ARRI",
   "transportCall": {"transportCallID": "TC-NYC-01", "modeOfTransport": "VESSEL",
                     "location": {"locationName": "Port Elizabeth", "UNLocationCode": "USNYC", "facilityCode": "APMT"},
                     "facilityTypeCode": "POTE", "importVoyageNumber": "442W", "transportCallSequenceNumber": 2}},
  {"eventID": "T3", "eventType": "TRANSPORT", "eventClassifierCode": "EST",
   "eventDateTime": "2026-11-15T22:00:00-05:00", "eventCreatedDateTime": "2026-10-25T09:00:00Z",
   "transportEventTypeCode": "ARRI",
   "transportCall": {"transportCallID": "TC-NYC-01", "modeOfTransport": "VESSEL", "UNLocationCode": "USNYC",
                     "facilityCode": "APMT", "facilityTypeCode": "POTE", "transportCallSequenceNumber": 2}},
  {"eventID": "T4", "eventType": "TRANSPORT", "eventClassifierCode": "EST",
   "eventDateTime": "2026-11-16T07:00:00-05:00", "eventCreatedDateTime": "2026-11-03T09:00:00Z",
   "transportEventTypeCode": "ARRI", "delayReasonCode": "WEA", "changeRemark": "Bad weather",
   "transportCall": {"transportCallID": "TC-NYC-01", "modeOfTransport": "VESSEL", "UNLocationCode": "USNYC",
                     "facilityCode": "APMT", "facilityTypeCode": "POTE", "transportCallSequenceNumber": 2}},
  {"eventID": "S1", "eventType": "SHIPMENT", "eventClassifierCode": "ACT",
   "eventDateTime": "2026-10-10T08:00:00Z", "eventCreatedDateTime": "2026-10-10T08:00:00Z",
   "shipmentEventTypeCode": "CONF", "documentTypeCode": "BKG", "documentID": "210000001"}
]}
```
Notes: T2 has its UN/LOCODE **only** under `transportCall.location`. SHIPMENT event S1 is
included to prove it is ignored. T4 is both the newest record and the latest time, so any
sane revision rule picks it.

**`events_arrived.json`**: booking `210000002`, Yantian to Savannah. Arrived and discharged.
- `T1` TRANSPORT DEPA ACT at `CNYTN`: `2026-10-05T20:00:00+08:00` (created `2026-10-05T12:30:00Z`)
- `T2` TRANSPORT ARRI EST at `USSAV`: `2026-11-01T06:00:00-04:00` (created `2026-10-20T08:00:00Z`)
- `T3` TRANSPORT ARRI ACT at `USSAV`, facility `GCT`: `2026-11-02T05:30:00-05:00` (created `2026-11-02T11:00:00Z`)
- `E1` EQUIPMENT DISC ACT, `MSKU3333333`, at `USSAV` (both `eventLocation` and `transportCall.UNLocationCode`): `2026-11-02T09:00:00-05:00`
- `E2` EQUIPMENT DISC ACT, `MSKU4444444`, at `USSAV`: `2026-11-02T15:20:00-05:00`

Write these in full JSON using the same shapes as `events_in_transit.json`. All TRANSPORT
events have `modeOfTransport: "VESSEL"` and `facilityTypeCode: "POTE"`.

**`events_ipi_rail.json`**: booking `210000003`, Shekou to Los Angeles by vessel, then Chicago
by rail.
- `T1` TRANSPORT DEPA ACT VESSEL at `CNSHK`: `2026-10-25T21:00:00+08:00` (created `2026-10-25T14:00:00Z`)
- `T2` TRANSPORT ARRI EST VESSEL at `USLAX`: `2026-11-10T08:00:00-08:00` (created `2026-10-26T10:00:00Z`)
- `T3` TRANSPORT ARRI EST VESSEL at `USLAX`: `2026-11-12T08:00:00-08:00` (created `2026-11-04T10:00:00Z`, the revision)
- `T4` TRANSPORT DEPA PLN **RAIL** at `USLAX`, `facilityTypeCode: "RAMP"`: `2026-11-14T20:00:00-08:00` (created `2026-10-26T10:00:00Z`)
- `T5` TRANSPORT ARRI PLN **RAIL** at `USCHI`, `facilityCode: "LPC"`, `facilityTypeCode: "RAMP"`: `2026-11-18T14:00:00-06:00` (created `2026-10-26T10:00:00Z`)
- `E1` EQUIPMENT LOAD ACT, `MSKU5555555`, at `CNSHK`

**`events_transshipment.json`**: booking `210000004`, Shanghai to Singapore to New York.
- `T1` DEPA ACT at `CNSHA`: `2026-10-01T16:00:00+08:00`
- `T2` ARRI ACT at `SGSIN`: `2026-10-08T06:00:00+08:00`
- `T3` DEPA EST at `SGSIN`: `2026-10-15T23:00:00+08:00`
- `T4` ARRI EST at `USNYC`: `2026-11-09T10:00:00-05:00`

All are TRANSPORT / VESSEL, each `eventCreatedDateTime` = its local date at 00:00Z.

**`events_empty.json`**: `{"events": []}`.

**`events_no_arrival.json`**: booking `210000006`.
- SHIPMENT CONF;
- EQUIPMENT GTIN ACT and LOAD ACT at `CNNGB`;
- TRANSPORT DEPA PLN at `CNNGB`.

There are no ARRI and no DISC events.

**`events_page1.json`** + **`events_page2.json`**: booking `210000007`.
- Page 1 holds `T1` DEPA ACT at `CNNGB` and one EQUIPMENT LOAD. Its response headers are
  `Current-Page: 1`, `Next-Page: 2`.
- Page 2 holds `T2` ARRI EST at `USNYC` (`2026-11-20T09:00:00-05:00`). Its response headers
  are `Current-Page: 2`, with no `Next-Page`.

### 9.2 Expected results (with defaults: `final`, `first`, date-only)

| Fixture | `eta` | `is_actual` | Other assertions |
|---|---|---|---|
| `events_in_transit` | `2026-11-16 00:00` | False | `details["unlocode"]=="USNYC"`; `details["eta_exact"]=="2026-11-16T07:00:00"`; `containers==("MSKU1111111","MSKU2222222")`; `"USNYC" in source` |
| `events_in_transit`, `MAERSK_ETA_DATE_ONLY=false` | `2026-11-16 07:00` | False | offset dropped, local wall time kept |
| `events_arrived` | `2026-11-02 00:00` | True | `details["unlocode"]=="USSAV"` |
| `events_ipi_rail` (`final`) | `2026-11-18 00:00` | False | `details["unlocode"]=="USCHI"`, `details["mode"]=="RAIL"`, `details["classifier"]=="PLN"` |
| `events_ipi_rail` (`MAERSK_ETA_TARGET=pod`) | `2026-11-12 00:00` | False | `details["unlocode"]=="USLAX"`. This expects `dcsa.booking_arrival()` to treat the last **vessel** arrival as the POD (DCSA meaning of port of discharge); if spec 09 differs, reconcile the two specs. |
| `events_transshipment` | `2026-11-09 00:00` | False | `details["unlocode"]=="USNYC"` (SGSIN excluded because it has a DEPA) |
| `events_empty` | raises `CarrierLookupError` | | message contains "no tracking events" |
| `events_no_arrival` | raises `NoEtaError` | | message contains "Can't determine ETA" |
| `events_page1` + `events_page2` | `2026-11-20 00:00` | False | exactly two GET calls; the second has `cursor=2` |

### 9.3 Tests — `tests/test_maersk.py` (respx + pytest-asyncio)

Build the client with `MaerskClient(settings, TokenStore(tmp_path / "tokens.json"),
http=httpx.AsyncClient())` under a `respx.mock` router. Use `base_url="https://api.maersk.test"`
in test settings so nothing can reach the real host. Use `assert_all_called` where it
applies.

1. `test_build_client_none_when_unconfigured`: `settings.maersk is None` → `build_client()` returns `None`.
2. `test_invalid_booking_rejected_without_http`: `"26754662"` (8 chars) and `"EBKG1234567"` → `CarrierLookupError`; respx records zero calls.
3. `test_token_request_shape`: the token POST has form body `grant_type=client_credentials`, the correct `client_id` and `client_secret`, a `Consumer-Key` header, and form content-type. The stored entry has `expires_at ≈ now + 7199` and a `key_fingerprint`, and contains no secret.
4. `test_token_cached_across_bookings`: two `get_eta()` calls make one token POST and two GETs.
5. `test_token_reused_from_store`: with a pre-seeded fresh entry and matching fingerprint, there is no token POST.
6. `test_token_fingerprint_mismatch_refetches`: a pre-seeded entry with a different fingerprint triggers a token POST.
7. `test_token_expiry_from_jwt_when_no_expires_in`: `token_no_expires_in.json` gives `expires_at == 1793860000`.
8. `test_token_400_is_lookup_error_with_onboarding_hint`: `CarrierLookupError` whose message contains "not yet approved" and "developer.maersk.com".
9. `test_token_401_gateway_key_error`: `error_gateway_401.json` → `CarrierLookupError` mentioning "Consumer-Key".
10. `test_events_request_shape`: GET to `/track-and-trace-private/events` with `carrierBookingReference=210000001`, `eventType=TRANSPORT,EQUIPMENT`, `limit=100`, plus `Consumer-Key`, `Authorization: Bearer …` and `Accept: application/json`. `API-Version` is absent by default and present when configured.
11. `test_401_refreshes_token_once_then_succeeds`: the first GET returns 401 and the second 200. That makes two token POSTs and the correct ETA.
12. `test_401_twice_is_lookup_error`.
13. `test_404_is_lookup_error`, with "HTTP 404" in the message.
14. `test_edge_html_403_is_lookup_error`: `edge_403.html` → the message mentions "edge" and "Access Denied" and contains no Consumer-Key value.
15. `test_json_403_is_lookup_error_mentions_approval`.
16. `test_429_retried_then_success`: 429 with `Retry-After: 0`, then 200 (exercises `http.request()`).
17. `test_empty_events_is_lookup_error`.
18. `test_no_arrival_is_no_eta_error`.
19. Parametrized over §9.2: `test_expected_eta[...]`.
20. `test_pagination_cursor_and_url_forms`: once with `Next-Page: 2` (query `cursor=2`) and once with an absolute same-host URL. A foreign-host link is not followed.
21. `test_max_pages_cap`: every page returns `Next-Page`, so with `MAERSK_MAX_PAGES=3` exactly 3 GETs happen, followed by a warning log.
22. `test_bare_array_payload_accepted`: a 200 with a JSON array instead of `{"events": [...]}` gives the same result.
23. `test_unexpected_payload_shape`: `{"foo": 1}` → `CarrierLookupError` listing the top-level keys.
24. `test_dcsa_parses_maersk_shapes`: `dcsa.parse_events()` on `events_in_transit.json` gives unlocode `USNYC` for T2 (only under `transportCall.location`), unlocode `CNNGB` for E2 (`eventLocation`), mode `VESSEL` for T1, and `equipment_reference` `MSKU1111111` for E1.
25. `test_select_final_arrival_unit`: pure-function tests on hand-built `DcsaEvent` lists:
    - stale PLN at the final ramp still wins via the DEPA rule;
    - with no DEPA events at all, the latest location by time wins;
    - DISC ACT at the final location upgrades EST to ACT;
    - naive and aware `created_datetime` values do not raise.
26. `test_probe_redacts_and_reports_alternatives`: `probe()` output has `token.ok == True`, `counts`, `raw` (list), and both `alternatives.final` and `alternatives.pod`. `json.dumps(output)` contains neither the access token nor the client secret.
27. `test_concurrency_single_token_fetch`: 10 concurrent `get_eta()` calls on a cold cache make exactly one token POST.

---

## 10. Probe / verification procedure

### 10.1 `probe(booking)` output

`probe()` returns a JSON-serializable dict:

```python
{
  "carrier": "Maersk", "booking": ref,
  "config": {"base_url", "token_url", "api_version", "eta_target", "policy", "date_only", "page_limit"},
  "token": {"ok": bool, "cached": bool, "expires_in_s": int | None, "scope": str | None, "error": str | None},
  "requests": [ ...summaries from _fetch_events... ],
  "counts": {"TRANSPORT/ARRI/EST": 3, ...},
  "raw": redact(raw_events),                     # ALL event types (event_types=None)
  "eta": {...EtaResult as dict, datetimes isoformat...} | "NoEtaError: ...",
  "alternatives": {"final": {"eta": iso, "basis": str} | None, "pod": {...} | None},
  "error": str | None
}
```

Behaviour:
- Get a token first. On `AuthError`, fill `token.error` and return early.
- Then fetch with `event_types=None`.
- Never include the token, the secret or the Consumer-Key value.
- The CLI (11-cli.md) writes the dict to `<ETA_STATE_DIR>/probe/maersk_<booking>_<ts>.json`.

### 10.2 Steps for the user, with real credentials (read-only; this API only reads)

1. Set `MAERSK_CLIENT_ID` and `MAERSK_CLIENT_SECRET` in `.env`.
2. Run `eta probe maersk --booking <a live Maersk booking>`. Expect `token.ok: true` and
   `counts` > 0. If it fails, use this key:

   | Error | Meaning |
   |---|---|
   | Token HTTP 400 | Product not approved yet (§2) |
   | `ERR_GW_001` | Wrong `MAERSK_CLIENT_ID` (use the Consumer Key) |
   | JSON 403 | Product not subscribed, or the booking is not visible to your customer account |
   | HTML 403 | Edge block; contact Maersk support with the Reference # |
   | 404 or empty `counts` | Booking unknown, or not linked to your account |

3. For **3–5 bookings of different kinds** (in transit, recently arrived, and any
   inland/IPI move), open maersk.com/tracking in a normal browser by hand. Compare its
   "Arrives …" date with `eta` and with `alternatives.final` / `alternatives.pod`. If `pod`
   matches better, set `MAERSK_ETA_TARGET=pod`. If a multi-container booking disagrees,
   consider `MAERSK_MULTI_CONTAINER_POLICY`.
4. Run `eta eta <b1> <b2> …` (ETA-only lookup, no side effects; 11-cli.md) for about 20
   bookings, and compare against the old tool's printed ETAs.
5. Only then include Maersk in a `--dry-run` batch, then a live run.

---

## 11. Sources & verification status

| Claim | Source | Status |
|---|---|---|
| Endpoint `GET /track-and-trace-private/events` on `https://api.maersk.com`; pre-prod `https://api-stage.maersk.com`; API title "Track & Trace Events", version 1.1.1, "based on DCSA Interface standard v.2.2" | OpenAPI document embedded in buyco/go-maersk-sdk, generated from Maersk's spec: https://raw.githubusercontent.com/buyco/go-maersk-sdk/maersk/v1.0.2/maersk/api/openapi.yaml (also https://github.com/buyco/go-maersk-sdk/blob/maersk/v1.0.2/maersk/docs/EventsApi.md) | VERIFIED-DOCS (third-party copy of Maersk's spec, 2023) |
| Query params (`carrierBookingReference` pattern `[a-zA-Z0-9]{9}`, `eventType` comma list, `limit` default 100, `cursor`), required `Consumer-Key` + `Authorization: Bearer`, optional `API-Version` (major only), `{"events": [...]}` wrapper, page headers, ApiError schema, 400/401/403/404/500 | same openapi.yaml | VERIFIED-DOCS |
| `eventDateTime` is local time with offset; `eventCreatedDateTime` is UTC; future events are included ("when the event will take place") | same openapi.yaml | VERIFIED-DOCS |
| TransportCall fields (`UNLocationCode`, `location`, `facilityTypeCode`, `modeOfTransport`, `vessel`); equipment `eventLocation`; transport events have no `equipmentReference` | same openapi.yaml | VERIFIED-DOCS |
| Token URL `https://api.maersk.com/customer-identity/oauth/v2/access_token`, client credentials, form body, `Consumer-Key` header | apis.io scopes page https://apis.io/scopes/maersk-line/maersk-line-scopes/; user's own `Maersk/oAuth.py` (commit 815551a); the 2023 SDK spec lists an older `/oauth2/access_token` | VERIFIED (corroborated) |
| Consumer-Key equals the client ID; token lifetime 2 h; HTTP 400 can mean "client ID not provisioned" (access requested, not approved) | https://developer.maersk.com/support/authorisation (JS-rendered page; content seen via search-engine excerpt) | VERIFIED-DOCS (excerpt) |
| Token endpoint and events endpoint return gateway JSON 401 `ERR_GW_001` for an invalid key; events without `Consumer-Key` get an Akamai HTML 403; `synergy/tracking` without a browser gets an Akamai HTML 403 | Unauthenticated requests from this environment, 2026-11-05 | OBSERVED |
| Production/stage API domains; Consumer Key length 32; apps can hold many API products; some products are monetized | developer.maersk.com public JS bundle (`assets/constants-*.js`) | OBSERVED |
| Maersk tracking follows DCSA T&T v2.2 in production; self-service registration; event meanings (Arrival at POD, Discharged at POD, Gate out) | https://blogs.tradlinx.com/how-maersk-container-tracking-actually-works-what-the-portal-shows-what-it-doesnt-and-what-the-events-mean/ | INFERRED (secondary) |
| Private events limited to parties on the booking | CMA CGM's equivalent rule (https://api-portal.cma-cgm.com/products/visibility) plus Maersk's API description ("shippers and consignees … shipments booked with" Maersk) | INFERRED |
| Approval lead time (days to ~2 weeks); cost | https://blog.shipmnts.com/ocean-carrier-track-and-trace-apis-integration-guide (generic "1–2 weeks on some lines") | UNVERIFIED |
| Rate limits / quotas; `limit` > 100; exact `Next-Page` value format | not published in accessible sources | UNVERIFIED (handled defensively: 429 retry, cap 100, both header forms) |
| Exact product label in today's catalogue; existence of newer "Track and Trace Plus" / DCSA 3.0 variants | apis.io listing (https://apis.io/apis/maersk-line/maersk-line-tracking-api/; its OpenAPI mirror looks machine-generated, so it was **not** used for field names) | UNVERIFIED |
| maersk.com "Arrives …" / `container-eta` equals the final-destination ETA (`eta_final_delivery`) | original code comments, plus knowledge of the site's synergy JSON; the site cannot be reached from here | INFERRED (hence `MAERSK_ETA_TARGET` and the probe comparison) |
| DCSA semantics (classifier PLN/EST/ACT; ARRI/DEPA; DISC) | https://dcsa.org/standards/track-and-trace/standard-documentation-track-and-trace ; https://developer.dcsa.org/explore-apis/track-and-trace | VERIFIED-DOCS |
