# 06 — Carrier: MSC (Mediterranean Shipping Company)

Module: `src/eta_automations/carriers/msc.py` · Logger: `eta.carrier.msc` · TokenStore key: `"msc"`
Implements the `CarrierClient` contract (03-foundations §9). Booking pattern: `EBKG\d{8}` (01-business-rules §3).

---

## 1. Summary

**Replaces:** `MSC/signInMSC.py` and `MSC/checkETA.py`.

The original does two things with a browser:
- Every ~30 min, a headless Chromium logs in to myMSC and fetches an API access token (`MSCauthToken.json`).
- For **every booking**, it launches a **new** headless Chromium, loads the heavy myMSC tracking page, and then POSTs one GraphQL query to `https://services.mymsc.com/tracking/graphql` with `Authorization: Bearer <AccessToken>`.

**Key findings:**
1. **The browser was never needed for the data call.** The GraphQL POST was already sent through Playwright's request context (a plain HTTP client, not the page). The gateway answers plain, cookie-less, non-browser HTTP: without a token it returns `401` with a plain-text body (OBSERVED, §11). Only the bearer token gates it.
2. **MSC publishes an official DCSA Track & Trace v2.2 API** on its developer portal, with a public sandbox. Its events carry the **same human event names** as the myMSC GraphQL data. So the original ETA rules can be ported almost exactly (§7).

**Modes** (one `MscClient`, chosen by `MSC_MODE`):

| Mode | What it calls | Status |
|---|---|---|
| `api` (**recommended**) | Official DCSA Track & Trace v2.2: `GET /events?carrierBookingReference=…`, authenticated with an Azure API Management subscription key. | Official and stable. Needs onboarding with MSC (production URL + key, §2.1). |
| `mymsc` (interim bridge) | The **same GraphQL query as today (verbatim)**, using an access token the **user supplies from their own logged-in myMSC browser session** (§5.1). | Works immediately. Exact parity with today's data and logic. Needs a fresh token per run (lifetime ≈ 30–60 min, UNVERIFIED). |

`MSC_MODE=auto` (default) means `api` when `MSC_API_BASE_URL` and `MSC_API_SUBSCRIPTION_KEY` are set, otherwise `mymsc`.

**Not specified on purpose:**
- **Automating the myMSC sign-in itself.** It is an undocumented, federated web sign-in behind MSC's bot-protection infrastructure. Scripting it would be brittle and would work against the site's protections. The bridge keeps the human in the loop for sign-in (once per run, ~30 s). The official API removes the need entirely.
- **MSC's public msc.com tracking endpoint.** It sits behind Akamai and returned "Access Denied" to plain HTTP (OBSERVED), so it is rejected (§8).

**Why it is faster:**
- Old: per booking ≈ 4–8 s (browser launch + SPA page load + API call + teardown), plus a browser login every 30 min.
- New: one HTTP request per booking (≈ 0.3–1.0 s), 4 in parallel. About 300 MSC bookings go from ~30 min to ~1–2 min.

---

## 2. Access & onboarding

### 2.1 `api` mode (official — apply now)
- **Developer portal:** <https://developerportal.msc.com>, an Azure API Management developer portal.
  - Its public catalogue lists **"DCSA Track and Trace"**: API id `DPO-DCSATrackAndTrace-API-V2`, path `dpo/trackandtrace/v2.2`.
  - Description: "Provides equipment actual milestones along with Vessel Events and Estimated Time of Arrival, following DCSA v2.2 standard".
  - Also listed: DCSA Booking, DCSA Commercial Schedules, DCSA Operational Vessel Schedule, DCSA JIT, MSC Referential, MSC Reporting, MSC Routing Sailings, Rate Integration.
- **Public sandbox** (mock data, no key — OBSERVED): `https://portal.api.msc.com/dpo/trackandtrace/v2.2/events`.
  - It returns the same 8 mock events for any reference.
  - Use it for contract tests and `eta probe msc --sandbox`. **The code refuses it for real runs** (§6.4).
- **Production access** is not self-service. Portal FAQ: "To get connected to MSC APIs, please submit your request via our Integration Form and our team of specialists will contact you."
  - Form (the portal's "Submit your request here" button): <https://forms.office.com/pages/responsepage.aspx?id=AJuOCND_jkW_oaz0xZbTy0aW8B-5TOxClNxXlpbxHp1URDMzTDkzQVZCUFhHVkZVTUs4UEU1OUY2WCQlQCN0PWcu&route=shorturl>
  - Background: <https://www.msc.com/solutions/digital-solutions/direct-integrations>
  - Lead time and cost: UNVERIFIED. Plan for 1–4 weeks; a third-party directory says "fees may apply".
- **Portal API products** (OBSERVED from product descriptions):
  - **Starter**: "5 calls/minute up to a maximum of 100 calls/week", no approval. **Too small**: one run needs ~300 MSC lookups.
  - **Unlimited**: "completely unlimited access … Administrator approval is required."
- **What to ask MSC for** (paste into the form):
  1. Production access to **DCSA Track and Trace v2.2** (`GET /events`), on the Unlimited product or a plan sized for ~500 lookups per run, several runs per week.
  2. The **production base URL** and the subscription-key header. The published spec uses `Ocp-Apim-Subscription-Key`.
  3. Confirmation that **`carrierBookingReference` accepts the `EBKG########` references** shown on myMSC. The sandbox's `BKG` references are 15-digit numerics (`177…`). This is UNVERIFIED and the #1 risk for `api` mode.
  4. Confirmation that every booking under the company's MSC customer account(s) is visible to the subscription.
  5. Optional, later: the "DCSA Track and Trace" **Azure Service Bus subscription** (push events) from the portal's *Subscription* page. Out of scope here.

### 2.2 `mymsc` bridge (usable immediately)
The user supplies their **own** access token from their **own** signed-in browser session, once per run.
1. Sign in to <https://www.mymsc.com> in your normal browser and open the tracking page.
2. Open DevTools (F12) → **Network**, search any booking, click the `graphql` request to `services.mymsc.com`, and copy the value of its **`Authorization`** request header (`Bearer eyJ…`).
3. Run `eta msc-token` and paste it (hidden input, 11-cli.md), or put it in `.env` as `MSC_ACCESS_TOKEN=...`.
   The tool accepts it with or without the `Bearer ` prefix.

Notes:
- Lifetime is UNVERIFIED (the original refreshed every 30 min). The tool reads the JWT `exp` claim when present and warns before a run if the token will expire within 10 minutes (§5.1).
- Run from the usual office network. If MSC's edge network answers with an "Access Denied" page, the tool stops MSC lookups and says so (§6.5). It never tries to get around it.
- This is the user's own token for their own account, used for the same request the current tool makes. It is an interim bridge until `api` mode is approved.

---

## 3. Configuration

### 3.1 Settings dataclass (built by `config.py`)

```python
@dataclass(frozen=True)
class MscSettings:
    mode: Literal["mymsc", "api"]             # resolved from MSC_MODE ("auto" resolved at load time)
    # ── mymsc bridge ──
    user_email: str                           # value of the `mymsc-user-email` header ("" → header omitted)
    user_roles: str                           # value of the `mymsc-user-roles` header
    graphql_url: str
    manual_token: str | None = field(repr=False)   # MSC_ACCESS_TOKEN (optional; tokens can also come from `eta msc-token`)
    token_ttl_seconds: int                    # assumed lifetime when the token has no readable `exp`
    rate_per_minute: int                      # politeness limiter for GraphQL calls (0 = off)
    # ── api mode ──
    api_base_url: str | None                  # no trailing slash
    api_key: str | None = field(repr=False)
    api_rate_per_minute: int                  # 0 = off (set 5 on the Starter product)
    api_customs_hold_rule: bool
    allow_sandbox: bool
    # ── common ──
    rescue_fallback: bool
    max_concurrency: int
    timeout_seconds: float

    @property
    def api_is_sandbox(self) -> bool:         # True when urlsplit(api_base_url).hostname == "portal.api.msc.com"
        ...
```

### 3.2 Environment variables

| Env var | Required? | Default | Meaning |
|---|---|---|---|
| `MSC_MODE` | no | `auto` | `auto` \| `mymsc` \| `api`. `auto` → `api` if both `MSC_API_BASE_URL` and `MSC_API_SUBSCRIPTION_KEY` are set, else `mymsc`. Any other value → `ConfigError`. |
| `MSC_ACCESS_TOKEN` | no | — | Bridge token (§2.2). Optional, because `eta msc-token` can store one instead. A leading `Bearer ` is stripped. Never logged. |
| `MSC_USER_EMAIL` | no | `VSHIP_USER_NAME`, then `MSC_USER_NAME` | Value of the `mymsc-user-email` header. The default keeps parity: the original sent `os.getenv('VSHIP_USER_NAME')` (earlier a hard-coded address). It should equal the myMSC sign-in email. Empty → header omitted, WARNING logged once. |
| `MSC_USER_ROLES` | no | `Customer` | Value of the `mymsc-user-roles` header (parity). |
| `MSC_GRAPHQL_URL` | no | `https://services.mymsc.com/tracking/graphql` | |
| `MSC_TOKEN_TTL_SECONDS` | no | `1800` | Assumed bridge-token lifetime when no JWT `exp` is readable. |
| `MSC_RATE_PER_MINUTE` | no | `120` | Limiter for GraphQL calls in `mymsc` mode. |
| `MSC_API_BASE_URL` | api | — | Production base URL from onboarding, e.g. `https://<host>/dpo/trackandtrace/v2.2`. The sandbox is `https://portal.api.msc.com/dpo/trackandtrace/v2.2`. |
| `MSC_API_SUBSCRIPTION_KEY` | api | — | Sent as `Ocp-Apim-Subscription-Key`. May be empty only for the sandbox. Never logged. |
| `MSC_API_RATE_PER_MINUTE` | no | `0` | `5` if stuck on the Starter product. |
| `MSC_API_CUSTOMS_HOLD_RULE` | no | `true` | Apply the approximated customs-hold rule in `api` mode (§7.3). |
| `MSC_ALLOW_SANDBOX` | no | `false` | Allow a sandbox base URL outside `probe --sandbox`. For tests only. |
| `MSC_RESCUE_FALLBACK` | no | `false` | Enable the rescue-only fallbacks (§7.5). Off by default for safety. |
| `MSC_MAX_CONCURRENCY` | no | `4` | Semaphore size (03 §10). |
| `MSC_TIMEOUT_SECONDS` | no | `30` | httpx read timeout (connect stays 10 s). |
| `MSC_USER_NAME`, `MSC_PASSWORD`, `LOGIN_URL`, `HOME_PAGE` | no | — | **Legacy.** Accepted and ignored by MSC lookups: this program does not automate the MSC sign-in. `MSC_USER_NAME` only serves as the last fallback for `MSC_USER_EMAIL`. Log once at DEBUG that the others are unused. |

**Presence rule:**
- `Settings.msc` is present in both modes.
- The single exception: `MSC_MODE=api` set explicitly while `MSC_API_BASE_URL` or `MSC_API_SUBSCRIPTION_KEY` is missing. Then `Settings.msc = None` and `Settings.missing["msc"]` lists the missing names. The key is exempt when the base URL is the sandbox.
- In `mymsc` mode a missing token is a **runtime** condition, reported by `get_eta()` and by the CLI pre-flight (§5.1), not a config error.

The old files `MSCauthToken.json` and `auth_for_MSC.json` are not read; the user can delete them.

---

## 4. Endpoints

All calls go through `http.request(...)` / `auth.request_with_auth(...)` (03 §6, §8), on one `httpx.AsyncClient` from `new_async_client(timeout=httpx.Timeout(settings.timeout_seconds, connect=10.0))`.

Use the default honest `User-Agent: ETAutomationsAPI/<version>` from 03 §6. **Do not** send the original's browser-imitation headers (`sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`, the Chrome `user-agent` string). See DEVIATION MSC-D2.

### 4.1 `mymsc` bridge — GraphQL tracking query

```
POST {graphql_url}                        (default https://services.mymsc.com/tracking/graphql)
Authorization: Bearer <token>
mymsc-user-email: <user_email>            (omitted if empty)
mymsc-user-roles: <user_roles>            ("Customer")
Content-Type: application/json
Accept: application/json
Referer: https://www.mymsc.com/           (parity with the original; harmless)
```

Body — **verbatim** from the original (do not change the text, the variable shape or `operationName`):

```python
GRAPHQL_QUERY: Final[str] = "\n    query trackingByBookingNumber($input: TrackingByBookingNumberGraphQlRequestInput!) {\n  trackingByBookingNumber(request: $input) {\n    allBillNumber\n    finalDischargePortCountry\n    finalDischargePortName\n    finalDischargePortUnCode\n    originName\n    originCode\n    destinationName\n    destinationCode\n    portLoadName\n    portLoadUnCode\n    priceCalculationDate\n    priceCalculationDateAsString\n    shippedToCountry\n    shippedToName\n    transshipmentName\n    transshipmentCode\n    resultsDateTimeInformations {\n      date\n      time\n    }\n    containers {\n      containerIsoDesc\n      containerIsOffService\n      containerNumber\n      finalEta\n      isDestinationReached\n      events {\n        emptyIndicatorCode\n        eventDate\n        eventDateDateTime\n        eventName\n        eventTime\n        lloydsNumber\n        locationCountry\n        locationName\n        locationUnCode\n        vesselName\n        voyageDesc\n        smdgTerminalCode\n        bicFacilityCode\n        activityEquipmentHandlingFacilityName\n        activityEquipmentHandlingFacilityCode\n        isEventPassed\n        portCalls {\n          portCallDate\n          isEventPassed\n          locationName\n          locationUnCode\n          locationCountry\n          etd\n          eta\n          eventDateTime\n        }\n      }\n    }\n    customsRelease {\n      error\n      response\n      details {\n        container\n        customsReference\n        customsStatus\n      }\n    }\n    lastFreeDates {\n      retrievalLevel\n      lastFreeDates {\n        containerNumber\n        lastFreeDate\n        errorMessage\n      }\n    }\n    freightRelease {\n      error\n      response\n    }\n  }\n}\n    "
GRAPHQL_QUERY_SHA256: Final[str] = "d5230f52190941f6f8ce1af4e5619ebe05b795b54d2eec0bc1ae95b9f67d5512"  # sha256(GRAPHQL_QUERY.encode())

def graphql_variables(booking: str) -> dict[str, Any]:
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
    return {"query": GRAPHQL_QUERY, "variables": graphql_variables(booking),
            "operationName": "trackingByBookingNumber"}
```

Send with `idempotent=True` (a pure read).

Relevant response fields (shape from the query; values UNVERIFIED beyond what the original parses):

```
data.trackingByBookingNumber: list[TrackingResult] | null
  TrackingResult.containers: list[Container] | null
    Container.containerNumber: str
    Container.isDestinationReached: bool | null
    Container.finalEta: str | null            (format UNVERIFIED — only used by the rescue path, §7.5)
    Container.events: list[Event] | null      (order as returned: newest first, §7.1)
      Event.eventName: str                    e.g. "Estimated Time of Arrival"
      Event.eventDate: str                    "%d %b %Y", e.g. "12 Nov 2026" (VERIFIED by the original parser)
      Event.locationCountry: str              ISO-3166 alpha-2, e.g. "US"
      Event.locationName / locationUnCode / vesselName / voyageDesc / isEventPassed / ...
errors: list[{ "message": str, ... }]        (standard GraphQL; may accompany HTTP 200)
```

Status codes:
- `200` — success. It may still carry `errors` (§7.1).
- `401` — token missing, expired or invalid. The body is plain text, e.g. `Something went wrong!!! Authentication failed.` (OBSERVED for a missing token), not JSON.
- `403` with an HTML "Access Denied" page — edge block (§6.5).
- Anything else → `ServiceError` → `CarrierLookupError`.

### 4.2 `api` mode — DCSA Track & Trace v2.2 (from MSC's published OpenAPI)

```
GET {api_base_url}/events?carrierBookingReference={booking}
Ocp-Apim-Subscription-Key: {api_key}         (omitted only for the sandbox with an empty key)
Accept: application/json
```

From MSC's published spec (exported from the portal) and the live sandbox:
- `servers`: `https://portal.api.msc.com/dpo/trackandtrace/v2.2`. That is the **sandbox**: the FAQ says the sandbox serves "a predefined set of production-like mock data".
- Security: `Ocp-Apim-Subscription-Key` header, or the `subscription-key` query parameter. **Use the header.**
- Query: exactly one of `carrierBookingReference`, `transportDocumentReference` (B/L), `equipmentReference` ("REQUIRES ONLY 1 of the 3 query parameters"). This module sends `carrierBookingReference` only.
- Responses: `200`, `400`, `403`, `404`, `500`.
  - MSC error body: `{"httpMethod","requestUri","errors":[{"reason","message"}],"statusCode","statusCodeText","errorDateTime"}`.
  - APIM gateway errors look like `{"statusCode": <int>, "message": "<text>"}`, e.g. `429 "Rate limit is exceeded. Try again in N seconds."` with `Retry-After`, or `403 "Out of call volume quota. Quota will be replenished in d.hh:mm:ss."`. These are standard APIM texts, UNVERIFIED for MSC.
- **Body shape:** the OpenAPI `events` schema says `{"events": [...]}`, but both the OpenAPI example and the live sandbox return a **bare JSON array**. Accept both.
- **Event fields used:**
  - `eventType` — `TRANSPORT` / `EQUIPMENT` / `SHIPMENT`.
  - Type code — `transportEventTypeCode` (`ARRI`/`DEPA`), `equipmentEventTypeCode` (`LOAD`/`DISC`/`GTIN`/`GTOT`/…) or `shipmentEventTypeCode`.
  - `eventClassifierCode` — **MSC's enum is only `ACT`/`EST`**.
  - `eventDateTime` — described as "the local date and time, where the event took place". `eventCreatedDateTime` is also present.
  - **`description`** — MSC's human event name, the **same vocabulary as myMSC's `eventName`**. Seen: "Estimated Time of Arrival", "Vessel Arrival", "Vessel Departure", "Export Loaded on Vessel", "Full Intended Transshipment", "Full Transshipment Discharged", "Export received at CY", "Empty to Shipper".
  - `equipmentReference`.
  - `transportCall.unLocationCode` — camelCase `un…`, not DCSA's `UNLocationCode`. Also `transportCall.facilityTypeCode` and `transportCall.modeOfTransport`.
  - `eventLocation.unLocationCode` and `eventLocation.locationName`.
- **Timestamp formats** seen: `2022-12-26T04:00:00Z` (sandbox) and `2022-12-26T04:00:00.0000000+00:00` (OpenAPI example, 7 fractional digits).
  The example's `eventId` `EST2022-12-26T04:00:00:00-03:00` shows the same wall time with a different offset. The offset is not trustworthy: **keep the wall-clock time, drop the offset** (03 §4).
- Response headers carry `x-azure-ref` and `request-context`. Use `x-azure-ref` as the request id (§12).

---

## 5. Auth / token lifecycle

### 5.1 `mymsc` bridge — `MscBridgeAuth` (implements `TokenProvider`, 03 §8)

TokenStore entry `"msc"`:

```json
{"access_token": "<token>", "expires_at": 1793853000.0, "obtained_at": 1793851200.0,
 "source": "manual", "mode": "mymsc"}
```

**`expires_at`:**
- From the JWT `exp` claim when readable (03 §7). Expired-on-arrival `exp` values are still stored, but flagged by `token_status()`.
- Otherwise `obtained_at + token_ttl_seconds`.

**`set_manual_token(token: str) -> dict[str, Any]`** (used by `eta msc-token`):
1. Strip whitespace and a leading `Bearer ` (case-insensitive).
2. Reject empty or whitespace-containing values with `ConfigError`.
3. Compute `expires_at`, `put("msc", entry)`, clear `self._rejected`.
4. Return `token_status()`.

**`token_status() -> dict[str, Any]`** returns `{"mode": "mymsc", "has_token": bool, "source": "manual"|None, "expires_at": float|None, "seconds_left": int|None, "jwt_claims": {"iss","aud","exp","iat"} or {}}`.
- Never return `sub`, email or name claims.
- The CLI pre-flight (11-cli.md) calls it before `eta run`. If the input contains MSC bookings and `seconds_left` is missing or below 600:
  - on an interactive TTY, prompt to paste a token now (`getpass`);
  - otherwise fail fast with the message from `get_token()` below.

**`get_token()`**, under an `asyncio.Lock`:
1. If `self._fatal` is set → raise it.
2. A TokenStore entry with `mode == "mymsc"` that `is_fresh()` → return it.
3. Else, if `settings.manual_token` is set and was not rejected in this process → store it as in `set_manual_token` and return it.
4. Else → set and raise the fatal `AuthError("No usable MSC access token. Paste a fresh one from your signed-in myMSC browser session with `eta msc-token` (see 06-carrier-msc §2.2), or configure the official MSC API (MSC_API_BASE_URL + MSC_API_SUBSCRIPTION_KEY).")`.

**`invalidate()`** — called by `request_with_auth` after a `401`:
- Delete the `"msc"` entry and set `self._rejected = True`.
- Set the fatal `AuthError("The MSC access token expired or was rejected (HTTP 401). Paste a fresh one with `eta msc-token`.")`.
- There is **no automatic re-acquisition**: the retry inside `request_with_auth` hits the fatal error and stops, and every later MSC booking fails fast with the same message.

### 5.2 `api` mode
A static subscription key; no `TokenProvider`. Fatal-latch conditions (§6.4):
- `401`/`403` whose message does not mention a quota → `CarrierLookupError("MSC API rejected the subscription key (HTTP {status}): {message}. Check MSC_API_SUBSCRIPTION_KEY and that the subscription is approved/active.")`.
- A quota message (`"quota"` in the message, case-insensitive) → `CarrierLookupError("MSC API call quota exhausted: {message}")`.

---

## 6. Module design (`src/eta_automations/carriers/msc.py`)

### 6.1 Public surface

```python
log = logging.getLogger("eta.carrier.msc")

ETA_EVENT: Final = "Estimated Time of Arrival"
DISCHARGE_EVENT: Final = "Import Discharged from Vessel"
TRANSSHIPMENT_EVENT: Final = "Full Transshipment Loaded"
SANDBOX_HOST: Final = "portal.api.msc.com"
SANDBOX_REFUSAL: Final = ("MSC_API_BASE_URL points at MSC's public SANDBOX (mock data). Refusing to use mock ETAs "
                          "for real bookings — use `eta probe msc --sandbox` for testing only.")

# GRAPHQL_QUERY, GRAPHQL_QUERY_SHA256, graphql_variables(), graphql_body() — §4.1

def parse_msc_date(value: str) -> datetime: ...
def parse_wall_datetime(value: str) -> datetime: ...
def transshipment_eta(loaded: datetime, now: datetime) -> datetime: ...

@dataclass(frozen=True, slots=True)
class MscSelection:
    eta: datetime
    event_name: str
    is_actual: bool
    from_transshipment_rule: bool
    raw_date: str

def select_eta(events: Sequence[Mapping[str, Any]], *, now: datetime, name_key: str,
               date_of: Callable[[Mapping[str, Any]], datetime]) -> MscSelection | None: ...

def eta_from_graphql(body: Any, *, booking: str, now: datetime, rescue: bool = False) -> EtaResult: ...
def eta_from_api(payload: Any, *, booking: str, now: datetime, customs_rule: bool = True,
                 rescue: bool = False) -> EtaResult: ...

class MscBridgeAuth:                        # TokenProvider (§5.1)
    def __init__(self, settings: MscSettings, token_store: TokenStore, *,
                 epoch: Callable[[], float] = time.time) -> None: ...
    async def get_token(self) -> str: ...
    async def invalidate(self) -> None: ...
    def set_manual_token(self, token: str) -> dict[str, Any]: ...
    def token_status(self) -> dict[str, Any]: ...

class MscClient:                            # CarrierClient
    carrier = Carrier.MSC
    def __init__(self, settings: MscSettings, token_store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None,
                 allow_sandbox: bool | None = None) -> None: ...
    async def get_eta(self, booking: str) -> EtaResult: ...
    async def probe(self, booking: str) -> dict[str, Any]: ...
    def token_status(self) -> dict[str, Any]: ...            # mymsc: delegates; api: {"mode": "api", "host": ...}
    def set_manual_token(self, token: str) -> dict[str, Any]: ...  # mymsc only; ConfigError in api mode
    async def aclose(self) -> None: ...                       # closes the httpx client if it owns it

def build_client(settings: Settings, token_store: TokenStore, *, clock: Clock | None = None,
                 http: httpx.AsyncClient | None = None) -> MscClient | None:
    """None if settings.msc is None. Logs once at INFO: 'MSC mode: mymsc (token bridge)' or 'MSC mode: api (<host>)'."""
```

`eta_from_graphql`, `eta_from_api`, `select_eta` and the parsers are **pure**: no I/O. All business logic is tested through them.

`MscClient.__init__`:
- `self._http = http or new_async_client(...)`, `self._owns_http = http is None`.
- `self._clock = clock or SystemClock()`.
- `self._sem = asyncio.Semaphore(settings.max_concurrency)`.
- `self._limiter = AsyncLimiter(n, 60)` where `n` is `rate_per_minute` (mymsc) or `api_rate_per_minute` (api); `None` when `n == 0`.
- `self._auth = MscBridgeAuth(...)` in mymsc mode.
- `self._fatal: CarrierLookupError | None = None`.
- `self._allow_sandbox = settings.allow_sandbox if allow_sandbox is None else allow_sandbox`.

The pipeline must build carrier clients with `clock=FixedClock(run_now)`, so the transshipment rule uses the run's single `now` (03 §3). See "Interface additions", §12.

### 6.2 Parsers

- **`parse_msc_date(value)`** — locale-independent `"%d %b %Y"`.
  `value.strip()` must match `^(\d{1,2})\s+([A-Za-z]{3})\s+(\d{4})$`. The month comes from a fixed English map `{"JAN": 1, …, "DEC": 12}` on the upper-cased text, because `strptime("%b")` is locale-dependent on Windows. Returns `datetime(y, m, d)`. Anything else → `ValueError(f"unrecognised MSC date {value!r}")`.
- **`parse_wall_datetime(value)`**:
  1. Strip.
  2. A trailing `Z` → `+00:00`.
  3. Truncate fractional seconds to 6 digits (regex `(\.\d{6})\d+` → `\1`).
  4. `datetime.fromisoformat(...)`, then `.replace(tzinfo=None)`. **Drop the offset, never convert.**
  5. A date-only `YYYY-MM-DD` → midnight. Anything else → `ValueError`.
- **`transshipment_eta(loaded, now)`** — exact port of the original's three branches. Keep them literally, although the last two are equivalent, so parity is visible:

```python
if loaded <= now - timedelta(days=6):
    return now + timedelta(days=7)            # keeps now's time-of-day (parity: pd.Timestamp.now() + 7 days)
elif now - timedelta(days=6) < loaded < now:
    return loaded + timedelta(days=7)
else:                                         # loaded >= now
    return loaded + timedelta(days=7)
```

### 6.3 `select_eta` — the original algorithm, generalized over field names

```python
def select_eta(events, *, now, name_key, date_of):
    for ev in events:                                        # payload order (newest first)
        name = (ev.get(name_key) or "").strip()
        if name == ETA_EVENT or name == DISCHARGE_EVENT:
            try:
                eta = date_of(ev)
            except ValueError as exc:                        # parity: original ValueError → "Can't determine ETA."
                raise NoEtaError(f"MSC event {name!r} has an unreadable date ({exc})") from exc
            return MscSelection(eta, name, name == DISCHARGE_EVENT, False, _raw_date_of(ev))
    for ev in events:
        if (ev.get(name_key) or "").strip() == TRANSSHIPMENT_EVENT:
            try:
                loaded = date_of(ev)
            except ValueError as exc:
                raise NoEtaError(f"MSC event {TRANSSHIPMENT_EVENT!r} has an unreadable date ({exc})") from exc
            return MscSelection(transshipment_eta(loaded, now), TRANSSHIPMENT_EVENT, False, True, _raw_date_of(ev))
    return None
# _raw_date_of(ev) = ev.get("eventDate") or ev.get("eventDateTime") or ""
```

### 6.4 `MscClient.get_eta(booking)`

Common steps:
1. If `self._fatal` is set → raise it (same message every time, so failure lines group together).
2. `now = self._clock.now()`.

**mymsc mode:**

```python
try:
    async with self._sem:
        resp = await request_with_auth(
            self._http, "POST", s.graphql_url, service="msc", auth=self._auth,
            build_headers=lambda t: {"Authorization": f"Bearer {t}", **self._gql_headers},
            json=graphql_body(booking), expected=(200,), idempotent=True, limiter=self._limiter)
    _raise_if_edge_block(resp)              # §6.5 (also applied to ServiceError bodies below)
    body = json_body(resp, service="msc")
except AuthError as exc:                   # missing/expired/rejected token
    self._fatal = CarrierLookupError(str(exc)); raise self._fatal from exc
except ServiceError as exc:
    if _is_edge_block_text(exc.body_excerpt):
        self._fatal = CarrierLookupError(EDGE_BLOCK_MESSAGE); raise self._fatal from exc
    raise CarrierLookupError(f"MSC tracking request failed for {booking}: {exc}") from exc
return _wrap_parse(lambda: eta_from_graphql(body, booking=booking, now=now, rescue=s.rescue_fallback), booking)
```

**api mode:**
1. If `s.api_is_sandbox and not self._allow_sandbox` → raise `CarrierLookupError(SANDBOX_REFUSAL)`.
2. `resp = await request(self._http, "GET", f"{s.api_base_url}/events", service="msc", params={"carrierBookingReference": booking}, headers=key_header, expected=(200, 400, 401, 403, 404), limiter=self._limiter)` inside `self._sem`.
   `429` and `5xx` are retried by `request()` (03 §6); on exhaustion → `CarrierLookupError(f"MSC API unavailable/rate-limited for {booking}: {exc}")`.
3. Handle the status:
   - `200` → `payload = json_body(...)`; return `_wrap_parse(lambda: eta_from_api(payload, booking=booking, now=now, customs_rule=s.api_customs_hold_rule, rescue=s.rescue_fallback), booking)`.
   - `401`/`403` → latch per §5.2. Get the message via `_api_error_message(resp)`: the first `errors[].message`, else `message`, else the first 200 chars of the text.
   - `404` → `CarrierLookupError(f"MSC API has no events for booking {booking} (HTTP 404): {msg} — the booking may be unknown to MSC or not linked to your account")`. Not latched.
   - `400` → `CarrierLookupError(f"MSC API rejected the request for {booking} (HTTP 400): {msg}")`. Not latched. If the message suggests an invalid reference format, add a hint about the EBKG question in §2.1.

`_wrap_parse(fn, booking)` runs `fn()`:
- It re-raises `CarrierLookupError`, `NoEtaError` and `CustomsHoldError` unchanged.
- It wraps `KeyError`/`TypeError`/`AttributeError`/`ValueError` into `CarrierLookupError(f"Unexpected MSC payload shape for {booking}: {exc!r}")`, chained.
- This enforces the boundary rule in 03 §5.

`probe(booking)` performs the same request(s) and returns `{"requests": [{"method", "url" (redacted), "status", "elapsed_ms"}], "raw": redact(payload), "eta": asdict(result) | "<ErrorClass>: <message>", "mode": ...}`.
- It never raises for lookup errors; it reports them.
- With `allow_sandbox=True` it may target the sandbox.

### 6.5 Edge blocks (no circumvention)
- `_is_edge_block_text(text)` is true when the text contains `Access Denied` **and** (`edgesuite.net` or `Reference #`). This is Akamai's standard denial page.
- `_raise_if_edge_block(resp)` checks status `403` and that text.
- On detection, set the latch: `EDGE_BLOCK_MESSAGE = "MSC's edge network refused the request from this network (Akamai 'Access Denied'). This tool does not try to get around bot protection. Run it from the network where myMSC works in your browser, or switch to the official MSC API (MSC_MODE=api)."`
- Never retry with different headers, user agents or TLS settings.

### 6.6 Logging
- INFO, one line per booking: `MSC {booking}: ETA {eta:%Y-%m-%d} via {source}`. WARNING lines for the multi-container differences described in §7.1.
- DEBUG: request summaries from `eta.http`.
- Never log the token, the subscription key, or full payloads (the payload is only in `probe` output, redacted).

---

## 7. Business logic

### 7.1 `mymsc` mode — exact port of `checkingMSC`

`eta_from_graphql(body, *, booking, now, rescue=False)` — steps, in this order:

1. `body` must be a dict, else `CarrierLookupError(f"MSC returned a non-object response for {booking}")`.
   `errors = body.get("errors") or []`, `data = body.get("data") or {}`,
   `results = data.get("trackingByBookingNumber")`.
   - `errors` and no `results` → `CarrierLookupError(f"MSC GraphQL error for {booking}: " + "; ".join(first 3 error messages))`.
   - `errors` with `results` → log WARNING with the messages and continue.
2. `results` is `None` or `[]` → `CarrierLookupError(f"MSC returned no tracking result for booking {booking} — check the booking on myMSC")`.
   If `len(results) > 1`, log INFO and use `results[0]` (parity).
3. `containers = results[0].get("containers") or []`.
   Empty → `NoEtaError(f"MSC lists no containers for booking {booking} yet. Can't determine ETA.")`.
   `c0 = containers[0]` (**parity: first container only**).
4. `events = c0.get("events") or []`.
5. **Customs-hold rule (parity):**
   `if c0.get("isDestinationReached") is True and events and (events[0].get("locationCountry") or "").strip().upper() == "US":`
   raise `CustomsHoldError(f"Possible customs hold for MSC booking {booking}: destination reached but latest event is in the US ({events[0].get('eventName')!r} at {events[0].get('locationName')!r})")`.
6. `events` empty → `NoEtaError(f"MSC returned no events for container {c0.get('containerNumber')} of booking {booking}. Can't determine ETA.")`.
7. `sel = select_eta(events, now=now, name_key="eventName", date_of=lambda ev: parse_msc_date(ev.get("eventDate") or ""))`.
8. `sel is None`:
   - if `rescue` → try the rescue in §7.5;
   - otherwise → `NoEtaError(f"No 'Estimated Time of Arrival', 'Import Discharged from Vessel' or 'Full Transshipment Loaded' event for MSC booking {booking}. Can't determine ETA.")`.
9. **Multi-container check (log only):** for each `cN` in `containers[1:]`, run steps 4 and 7 inside `try/except (NoEtaError, ValueError)`. If any resulting ETA date differs from `sel.eta.date()`:
   - log a WARNING: `MSC {booking}: containers have different ETAs {…}; using first container {c0} (parity)`;
   - put `{"other_container_etas": {container: "YYYY-MM-DD" | "n/a"}}` into `details`.
10. Return `EtaResult` per §7.4, with `containers=(c0.get("containerNumber") or "",)` (drop empty values) and `details["mode"] = "mymsc"`.

**Ordering note:** events are used **in payload order**, which is newest first. MSC's UI and API both list the latest event first. The original relies on this in two places: `events[0]` is "the latest event" for the customs rule, and "first ETA/discharge match wins".

### 7.2 `api` mode — the same rules mapped onto DCSA events

`eta_from_api(payload, *, booking, now, customs_rule=True, rescue=False)`:

1. **Normalize the payload:**
   - list → `raw = payload`;
   - dict with a list under `"events"` → that list;
   - anything else → `CarrierLookupError(f"Unexpected MSC API response shape for {booking}")`.
   - `raw == []` → `CarrierLookupError(f"MSC API returned no events for booking {booking} — booking unknown to MSC or not linked to your account")`.
2. **Normalize each event** into a small internal record (MSC-specific, no dependency on `dcsa.py`):
   - `etype` = `eventType` upper-cased; if missing, infer it from whichever of `transportEventTypeCode` / `equipmentEventTypeCode` / `shipmentEventTypeCode` is present.
   - `code` = that type code; `classifier` = `eventClassifierCode`; `name` = `(description or "").strip()`.
   - `when` = `parse_wall_datetime(eventDateTime)`. Skip the event with a DEBUG log if it is unparseable.
   - `unloc` = first non-empty of `transportCall.unLocationCode`, `eventLocation.unLocationCode`, `transportCall.UNLocationCode`, `transportCall.location.UNLocationCode`, upper-cased.
   - `equipment` = `equipmentReference`.
3. **Containers:** `sorted({equipment for EQUIPMENT events if equipment})`. The first is `c0`.
   - **DEVIATION MSC-D9:** DCSA has no "containers[0]"; alphabetical order is deterministic.
   - `timeline(c)` = all TRANSPORT events + EQUIPMENT events whose `equipment == c`, **sorted by `when` descending** (stable). This reproduces "newest first" regardless of API order.
   - With no containers, the timeline is all events sorted the same way.
4. **Customs-hold rule** (if `customs_rule`; INFERRED approximation, §7.3) → may raise `CustomsHoldError`.
5. `sel = select_eta(timeline(c0), now=now, name_key="name", date_of=lambda ev: datetime.combine(ev["when"].date(), time()))`.
   - **Truncation to midnight** keeps parity with `mymsc` mode, where MSC dates are date-only. The full timestamp goes into `details["event_datetime"]`.
   - `is_actual = sel.is_actual or (matched event's classifier == "ACT")`.
6. `sel is None` → rescue (§7.5) if enabled, else `NoEtaError` (same message as §7.1 step 8).
7. Multi-container check exactly as §7.1 step 9, using `timeline(cN)`.
8. Return `EtaResult` (§7.4) with `details["mode"] = "api"`.

### 7.3 Customs-hold approximation for `api` mode (INFERRED — verify with `--compare`, §10)

The original rule needs `isDestinationReached`, which the DCSA payload does not have.

**Business meaning** (from context): these are US export shipments. If MSC says the destination was reached but the container's latest event is still in the US, the container probably never sailed — for example an export or customs hold.

**DCSA approximation:**
- `arri` = TRANSPORT events with `code == "ARRI"`. With none, the rule does not apply.
- `pod_event = max(arri, key=when)`; `destination_reached = pod_event.classifier == "ACT"`.
- `latest_equipment` = the first EQUIPMENT event in `timeline(c0)`, i.e. the newest one for that container.
- If `destination_reached and latest_equipment and (latest_equipment.unloc or "")[:2] == "US"`:
  raise `CustomsHoldError(f"Possible customs hold for MSC booking {booking}: vessel arrived at {pod_event.unloc} but container {c0}'s latest event is in the US ({latest_equipment.name!r})")`.

Disable it with `MSC_API_CUSTOMS_HOLD_RULE=false` if `--compare` shows false positives.

### 7.4 `EtaResult` fields

| Field | Value |
|---|---|
| `carrier` | `Carrier.MSC` |
| `booking` | `booking` |
| `eta` | `sel.eta` (naive; midnight for event dates; the transshipment rule's first branch keeps `now`'s time of day) |
| `source` | `f"MSC event '{sel.event_name}'"` (mymsc) / `f"MSC API event '{sel.event_name}'"` (api). Transshipment: `f"MSC '{TRANSSHIPMENT_EVENT}' on {loaded:%d %b %Y} → ETA estimate (+7 days)"` |
| `is_actual` | `True` for `Import Discharged from Vessel` (and for an `ACT` match in api mode) |
| `from_transshipment_rule` | `sel.from_transshipment_rule` |
| `containers` | `(c0,)` |
| `details` | `{"mode", "event_name", "raw_date", "container"}`, plus `"event_datetime"` (api), `"other_container_etas"`, `"rescue": True` when §7.5 was used |

### 7.5 Rescue-only fallbacks (`MSC_RESCUE_FALLBACK`, default **false**)

These run only where the original would have failed with "Can't determine ETA". They never change a result the original produces. Off by default because a wrong rescued ETA could e-mail a customer.
- **mymsc:** parse `c0["finalEta"]` with `parse_msc_date`, then `parse_wall_datetime` → midnight.
  Source: `"MSC containers[0].finalEta (rescue)"`; set `details["rescue"] = True`. The field's format is UNVERIFIED.
- **api:** `dcsa.booking_arrival(dcsa.parse_events(payload), policy="first")` (09 spec) → convert with `dcsa.to_eta_result(...)`, then truncate to midnight. Source: `"MSC API DCSA arrival (rescue)"`.
- If the rescue also fails → the normal `NoEtaError`.

### 7.6 Deviations from the original (all intentional)

| ID | Deviation | Why |
|---|---|---|
| MSC-D1 | No browser at all. GraphQL is sent over plain HTTP. No tracking-page visit. The **token is supplied by the user** (bridge) or replaced by the official API key. | Speed; no Playwright. Automating the sign-in is deliberately not specified (§1). |
| MSC-D2 | Browser-imitation headers (`sec-ch-ua*`, Chrome UA) dropped. Honest UA. | No impersonation. The gateway answers plain HTTP (OBSERVED). |
| MSC-D3 | Token cached in TokenStore with expiry from JWT `exp`, else a TTL. The original used `MSCauthToken.json` with an mtime < 30 min check. | Robust and explicit. |
| MSC-D4 | Empty `trackingByBookingNumber` and GraphQL `errors` → `CarrierLookupError`. No containers / no events → `NoEtaError`. The original crashed with IndexError/TypeError, reported as "Unexpected error during carrier lookup." | Clear failure reasons. Same outcome (failed list). |
| MSC-D5 | `isDestinationReached` of `None` is treated as False. `events[0]` is read only when events exist. The original used `&` and crashed on empty events. | Bug fix. Same decision whenever the original didn't crash. |
| MSC-D6 | Event names compared after `.strip()`. Month names parsed locale-independently. | Robustness. Identical results on well-formed data. |
| MSC-D7 | Multi-container WARNING plus `details`. The decision still uses container 0. | Visibility without behaviour change. |
| MSC-D8 | On 401 or missing token, MSC lookups stop for the run with one clear message. The original re-ran the browser login, or showed a Tk prompt when `MSC_PASSWORD` was unset. | No automated sign-in. The carrier layer never prompts; the CLI pre-flight does (§5.1). |
| MSC-D9 | `api` mode is new: mapping via `description`, sorted newest-first, alphabetical first container, ETA truncated to the date, customs rule approximated (§7.2–7.3). | Official API. Verify with `--compare` before relying on it. |
| MSC-D10 | Rescue fallbacks exist but are **off** by default. | Safety. |

---

## 8. Fallbacks (honest assessment)

| Option | Verdict |
|---|---|
| **Official DCSA Track & Trace API** (`api` mode) | **Primary target.** Official, documented, key-based, no web sign-in. Blockers: onboarding time, and whether `EBKG…` references are accepted (§2.1). |
| **Token bridge** (`mymsc` mode) | Works today with no new accounts. Costs ~30 s of manual work per run (paste a token). Uses an internal, undocumented myMSC API that can change without notice. Interim only. |
| **Licensed tracking aggregator** (09 spec, `aggregator.py`) | Viable if MSC onboarding stalls. Paid, but licensed data. Same `CarrierClient` contract. Ports the "ETA at POD" semantics, not MSC's exact event names, so expect small differences. |
| **Automating the myMSC sign-in over HTTP** | **Not specified.** It is an undocumented federated web login behind bot-protection infrastructure. Brittle, and it works against the site's protections. |
| **msc.com public tracking endpoint** | **Rejected.** Akamai returns "Access Denied" to plain HTTP (OBSERVED). Using it would require defeating bot protection. |
| **Any bot-protection workaround** (TLS/JA3 impersonation, replaying sensor data, harvesting browser cookies to pass bot checks, CAPTCHA solving, stealth browsers) | **Never.** If the official API is unavailable and the bridge is not acceptable, the remaining fallback is manual: failed bookings land in `Failed_To_Process.txt`, as today. |

---

## 9. Fixtures & tests

All tests use `FixedClock(datetime(2026, 11, 5, 9, 30))` → `now = 2026-11-05 09:30`. Fixture JSON is synthetic, except `api_sandbox_trimmed.json`, which is a trimmed copy of MSC's **public** sandbox mock.

### 9.1 Test helpers (`tests/test_msc.py`)

```python
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
```

### 9.2 Fixture files

`tests/fixtures/msc/graphql_in_transit.json`

```json
{"data": {"trackingByBookingNumber": [{"allBillNumber": "MEDUAB123456", "finalDischargePortCountry": "GH",
  "finalDischargePortName": "TEMA", "finalDischargePortUnCode": "GHTEM", "originName": "NEW YORK", "originCode": "USNYC",
  "destinationName": "TEMA", "destinationCode": "GHTEM", "portLoadName": "NEW YORK", "portLoadUnCode": "USNYC",
  "priceCalculationDate": null, "priceCalculationDateAsString": null, "shippedToCountry": "GH", "shippedToName": "TEMA",
  "transshipmentName": null, "transshipmentCode": null, "resultsDateTimeInformations": {"date": "05 Nov 2026", "time": "09:29"},
  "containers": [{"containerIsoDesc": "40' HIGH CUBE", "containerIsOffService": false, "containerNumber": "MSCU1111111",
    "finalEta": "12 Nov 2026", "isDestinationReached": false, "events": [
      {"emptyIndicatorCode": "Laden", "eventDate": "12 Nov 2026", "eventDateDateTime": null, "eventName": "Estimated Time of Arrival",
       "eventTime": "", "lloydsNumber": "9000001", "locationCountry": "GH", "locationName": "TEMA", "locationUnCode": "GHTEM",
       "vesselName": "MSC EXAMPLE", "voyageDesc": "AB642A", "smdgTerminalCode": null, "bicFacilityCode": null,
       "activityEquipmentHandlingFacilityName": null, "activityEquipmentHandlingFacilityCode": null, "isEventPassed": false, "portCalls": []},
      {"emptyIndicatorCode": "Laden", "eventDate": "20 Oct 2026", "eventDateDateTime": null, "eventName": "Export Loaded on Vessel",
       "eventTime": "", "lloydsNumber": "9000001", "locationCountry": "US", "locationName": "NEW YORK", "locationUnCode": "USNYC",
       "vesselName": "MSC EXAMPLE", "voyageDesc": "AB642A", "smdgTerminalCode": null, "bicFacilityCode": null,
       "activityEquipmentHandlingFacilityName": null, "activityEquipmentHandlingFacilityCode": null, "isEventPassed": true, "portCalls": []},
      {"emptyIndicatorCode": "Laden", "eventDate": "14 Oct 2026", "eventDateDateTime": null, "eventName": "Export received at CY",
       "eventTime": "", "lloydsNumber": null, "locationCountry": "US", "locationName": "NEW YORK", "locationUnCode": "USNYC",
       "vesselName": null, "voyageDesc": null, "smdgTerminalCode": null, "bicFacilityCode": null,
       "activityEquipmentHandlingFacilityName": null, "activityEquipmentHandlingFacilityCode": null, "isEventPassed": true, "portCalls": []}]}],
  "customsRelease": null, "lastFreeDates": null, "freightRelease": null}]}}
```

Expected: `eta == datetime(2026, 11, 12)`, `source == "MSC event 'Estimated Time of Arrival'"`, `is_actual is False`, `containers == ("MSCU1111111",)`.

`tests/fixtures/msc/graphql_errors.json` → `CarrierLookupError` (message contains "Booking number not found"):

```json
{"errors": [{"message": "Booking number not found", "extensions": {"code": "NOT_FOUND"}}], "data": {"trackingByBookingNumber": null}}
```

`tests/fixtures/msc/api_in_transit.json` → bare array. Expected: `eta == datetime(2026, 11, 12)`, `details["event_datetime"] == "2026-11-12T06:00:00"`, `containers == ("MSCU1111111",)`, `is_actual is False`.

```json
[
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
]
```

`tests/fixtures/msc/api_sandbox_trimmed.json` — the first, third and sixth events of the public sandbox response, unchanged:
- "Estimated Time of Arrival" `2022-12-26T04:00:00Z` at `CXXXX`;
- "Full Transshipment Discharged" `DISC ACT` at `KRPUS`, container `MSCU1234567`;
- "Export Loaded on Vessel" `LOAD ACT` at `CNSHA`, container `MSCU1234568`.

Capture it once with `eta probe msc --sandbox --save-raw` and commit only these three events. Expected:
- `eta == datetime(2022, 12, 26)`;
- `c0 == "MSCU1234567"` (alphabetical);
- no WARNING: container `MSCU1234568`'s timeline also matches the shared TRANSPORT ETA event.

`tests/fixtures/msc/api_error_404.json`:

```json
{"httpMethod": "GET", "requestUri": "https://example.invalid/dpo/trackandtrace/v2.2/events?carrierBookingReference=EBKG00000001",
 "errors": [{"reason": "Not Found", "message": "No events found for the given reference"}], "statusCode": 404,
 "statusCodeText": "NotFound", "errorDateTime": "2026-11-05T09:30:00Z"}
```

`tests/fixtures/msc/apim_quota_403.json`:

```json
{"statusCode": 403, "message": "Out of call volume quota. Quota will be replenished in 2.11:42:00."}
```

### 9.3 Unit tests (`tests/test_msc.py`)

**Pure logic — mymsc (`eta_from_graphql`):**
1. `graphql_in_transit.json` → expected result above.
2. Discharged first: `[gql_event("Import Discharged from Vessel", "03 Nov 2026", "GH"), gql_event("Estimated Time of Arrival", "02 Nov 2026", "GH")]` → `eta 2026-11-03`, `is_actual True`. **First match wins.**
3. ETA first, discharge later in the list → ETA wins (order test).
4. Transshipment only, parametrized against `now = 2026-11-05 09:30`:
   - `"20 Oct 2026"` → `2026-11-12 09:30` (branch 1, keeps the time of day);
   - `"02 Nov 2026"` → `2026-11-09 00:00` (branch 2);
   - `"07 Nov 2026"` → `2026-11-14 00:00` (branch 3).
   All have `from_transshipment_rule True`.
5. Boundaries with `FixedClock(datetime(2026, 11, 5))` (midnight):
   - `"30 Oct 2026"` (== now − 6 d) → `2026-11-12 00:00` (branch 1);
   - `"31 Oct 2026"` → `2026-11-07`;
   - `"05 Nov 2026"` (== now) → `2026-11-12` (branch 3).
6. Customs hold: `reached=True` with `events[0]` country `"US"` → `CustomsHoldError`. With `reached=True` and country `"GH"` → normal ETA. With `reached=None` and country `"US"` → normal ETA (MSC-D5).
7. No matching events → `NoEtaError`. Empty containers → `NoEtaError`. `events: null` → `NoEtaError`. `trackingByBookingNumber: []` → `CarrierLookupError`. `graphql_errors.json` → `CarrierLookupError`. `errors` alongside results → result returned plus a WARNING logged (`caplog`).
8. Bad date `"2026-11-12"` on an ETA event → `NoEtaError` whose message contains the raw value.
9. Multi-container: c0 ETA `"12 Nov 2026"`, c1 ETA `"19 Nov 2026"` → `eta 2026-11-12`, a WARNING logged, `details["other_container_etas"] == {"MSCU2222222": "2026-11-19"}`.
10. `parse_msc_date` accepts `"5 Nov 2026"`, `" 12 nov 2026 "`, `"12 NOV 2026"`; rejects `"12 Nov 26"` and `"2026-11-12"`. Run once under a non-English locale if available, e.g. `locale.setlocale(LC_TIME, "de_DE.UTF-8")` wrapped in `try/skip`.
11. `hashlib.sha256(GRAPHQL_QUERY.encode()).hexdigest() == GRAPHQL_QUERY_SHA256`, and `graphql_body("EBKG12345678")` equals the original dict structure (assert the keys and the nested `agency` dict).

**Pure logic — api (`eta_from_api`):**

12. `api_in_transit.json` → expected result. The same payload wrapped as `{"events": [...]}` gives the same result. The same events in reversed order give the same result (sorting).
13. Discharged: `DISC ACT "Import Discharged from Vessel" at GHTEM 2026-11-03T14:20:00.0000000+00:00` plus `TRANSPORT ARRI ACT "Vessel Arrival" at GHTEM 2026-11-02T22:00:00Z` → `eta 2026-11-03`, `is_actual True`, no customs hold.
14. Customs hold:
    - `TRANSPORT ARRI ACT "Vessel Arrival" GHTEM 2026-11-02T22:00:00Z` (newest ARRI, `ACT`), `TRANSPORT DEPA ACT USSAV 2026-10-15T10:00:00Z`, and `EQUIPMENT GTIN ACT "Export received at CY" USSAV 2026-10-10T08:00:00Z` for `MSCU2222222` → `CustomsHoldError`.
    - With `customs_rule=False` → `NoEtaError` (no ETA/discharge/transshipment event, rescue off).
15. Transshipment: `EQUIPMENT LOAD ACT "Full Transshipment Loaded" MAPTM 2026-11-02T08:00:00Z` → `eta 2026-11-09 00:00`, `from_transshipment_rule True`.
16. `[]` → `CarrierLookupError`. A non-list/non-dict payload → `CarrierLookupError`.
17. `api_sandbox_trimmed.json` → `eta 2022-12-26`, `c0 == "MSCU1234567"`.
18. `parse_wall_datetime`:
    - `"2022-12-26T04:00:00Z"` → `2022-12-26 04:00`;
    - `"2022-12-26T04:00:00.0000000+00:00"` → same;
    - `"2026-11-12T06:00:00+02:00"` → `06:00` (not converted);
    - `"2026-11-12"` → midnight;
    - `"garbage"` → `ValueError`.

**Client behaviour (respx):**

19. mymsc, token in store (fresh) → one POST with headers `Authorization: Bearer <tok>`, `mymsc-user-email`, `mymsc-user-roles: Customer`. **No** `sec-ch-ua` header. The body matches `graphql_body`.
20. mymsc, no token anywhere → `CarrierLookupError` with the "No usable MSC access token" text. A second call raises the same error **without any HTTP request** (latch).
21. mymsc, `401` text/plain → exactly 2 POST attempts at most: the first, plus `request_with_auth`'s retry hitting the latched error, so in practice 1 POST. Then `CarrierLookupError` "expired or was rejected". Later bookings make no requests.
22. mymsc, `403` Akamai "Access Denied" HTML → `CarrierLookupError(EDGE_BLOCK_MESSAGE)`, latched. **Assert no retry with altered headers.**
23. `MSC_ACCESS_TOKEN="Bearer abc.def.ghi"` → stored without the prefix. Expiry from a test JWT `exp`, built in the test as `alg: none` with a `{"exp": ...}` payload.
24. `set_manual_token("   ")` → `ConfigError`. `token_status()` never contains `sub`/`email` claims.
25. 20 concurrent `get_eta()` with a fresh token → 20 POSTs and at most `MSC_MAX_CONCURRENCY` in flight. Assert with a respx side effect that tracks concurrency.
26. api: `200` → parsed. `404` (`api_error_404.json`) → `CarrierLookupError` containing "No events found", not latched. `403` quota (`apim_quota_403.json`) → latched "quota exhausted". `401` → latched "rejected the subscription key". `429` then `200` → success (test passes `Retry-After: 0`).
27. api against a sandbox base URL with `allow_sandbox=False` → `CarrierLookupError(SANDBOX_REFUSAL)` and **no HTTP request**. With `allow_sandbox=True` → allowed.
28. `MSC_MODE=auto` resolution: both API vars → `api`; one missing → `mymsc`; explicit `api` with missing vars → `Settings.msc is None` and `missing["msc"]` lists them.
29. Secrets never appear in `caplog.text`: token, subscription key, `MSC_ACCESS_TOKEN` value.

---

## 10. Probe / verification procedure

These are MSC options of `eta probe msc` (11-cli.md wires them). Raw payloads are written **redacted** to `<ETA_STATE_DIR>/probe/msc_<booking>_<YYYYmmdd_HHMMSS>.json`.

| Command | What it does |
|---|---|
| `eta probe msc --sandbox [--save-raw]` | Calls the **public sandbox** with `allow_sandbox=True` and no key. Prints the event count and the parsed `EtaResult`: expected ETA **2022-12-26** from 'Estimated Time of Arrival'. Needs no credentials. Proves the `api`-mode parser end to end. |
| `eta msc-token` | Prompts (hidden) for a token copied per §2.2, stores it, and prints `token_status()`: seconds left, `iss`/`aud`/`exp`. |
| `eta probe msc --token-info` | Prints `token_status()` without any network call. |
| `eta probe msc --booking EBKG… [--booking …]` | Real lookups in the configured mode. Prints mode, HTTP status, `EtaResult`, and any error class with its message. |
| `eta probe msc --compare --booking …` | Needs **both** a bridge token and API settings. Runs `mymsc` and `api` for each booking and prints a table: ETA (mymsc), ETA (api), source of each, customs flag of each, and a MATCH/DIFF column. Exit code 1 if any DIFF. |

**First-run checklist (user):**
1. `eta probe msc --sandbox` → expect `2022-12-26`. If this fails, the problem is local: proxy, TLS or install.
2. Paste a token (`eta msc-token`). Then run `eta probe msc --booking` for three bookings you know: in transit, already discharged at destination, and transshipment-only or not yet loaded. Compare with the myMSC website and with the old tool's result.
   - Empty results for valid bookings → check that `MSC_USER_EMAIL` equals your myMSC sign-in email.
   - "Access Denied" → run from the office network.
3. Note the token's lifetime as printed (the JWT `exp`) and tell the team how long a pasted token lasts.
4. Submit the MSC Integration Form (§2.1). When approved, set `MSC_API_BASE_URL` and `MSC_API_SUBSCRIPTION_KEY`.
   Paste a fresh token and run `eta probe msc --compare` on **20+ bookings**, including one known customs-hold case if possible. Check that:
   - (a) `EBKG` references return events in `api` mode;
   - (b) ETA dates match (offset handling, §4.2);
   - (c) the customs-hold approximation agrees (§7.3; set `MSC_API_CUSTOMS_HOLD_RULE=false` if not).
5. All MATCH → leave `MSC_MODE=auto`; `api` is picked automatically. The bridge token is then no longer needed.

---

## 11. Sources & verification status

| Claim | Source | Status |
|---|---|---|
| MSC developer portal exists, is Azure APIM, and publishes "DCSA Track and Trace" (`DPO-DCSATrackAndTrace-API-V2`, path `dpo/trackandtrace/v2.2`) plus 8 other APIs | <https://developerportal.msc.com> (public catalogue data behind the portal) | OBSERVED |
| Spec: server `https://portal.api.msc.com/dpo/trackandtrace/v2.2`, security `Ocp-Apim-Subscription-Key` header or `subscription-key` query, `GET /events` with one of `carrierBookingReference`, `transportDocumentReference`, `equipmentReference`; error schema; classifier enum ACT/EST; `description` field; `transportCall.unLocationCode`; `eventDateTime` = local time | MSC's OpenAPI export from the portal | VERIFIED-DOCS |
| Spec example and live sandbox return a **bare array**; sandbox uses `Z` timestamps, the example 7-digit fractions; `eventId` offset differs from the `eventDateTime` offset | Portal OpenAPI example; `GET https://portal.api.msc.com/dpo/trackandtrace/v2.2/events` | OBSERVED |
| Sandbox = mock data; production access via the Integration Form | <https://developerportal.msc.com/faq> | VERIFIED-DOCS |
| Integration Form link; Direct Integrations page | Portal home page links; <https://www.msc.com/solutions/digital-solutions/direct-integrations> | OBSERVED |
| Products: Starter = 5 calls/min, 100 calls/week, no approval; Unlimited = approval required | Portal product descriptions | OBSERVED |
| "API and EDI access is not self-service … fees may apply" | <https://apis.io/providers/msc-cargo/> (third-party directory) | UNVERIFIED |
| Production base URL; lead time; cost; `EBKG` references accepted as `carrierBookingReference`; which bookings are visible | — | UNVERIFIED (ask MSC, §2.1) |
| `services.mymsc.com/tracking/graphql`: GET → APIM-style JSON 404; POST without token → `401` plain text "Something went wrong!!! Authentication failed." (reaches origin over plain HTTP, no cookies) | Direct observation 2026-11-05 | OBSERVED |
| GraphQL works with the bearer token plus `mymsc-user-*` headers alone, without browser cookies | Inferred from the observation above and the original's use of Playwright's request context | INFERRED (verify with probe step 2) |
| Token JSON key `AccessToken`; GraphQL response paths and the `eventDate` format `%d %b %Y` | Original `MSC/signInMSC.py` and `MSC/checkETA.py` (working code) | VERIFIED (by original code) |
| Bridge-token lifetime (≈ 30–60 min) | Original refreshed after 30 min | UNVERIFIED |
| `www.msc.com` (incl. `/api/feature/tools/TrackingInfo`) answers Akamai "Access Denied" to plain HTTP | Direct observation 2026-11-05 | OBSERVED → rejected |
| No public documentation of the myMSC GraphQL or token endpoints | Web search 2026-11-05 | OBSERVED |
| DCSA Track & Trace standard (event model background) | <https://dcsa.org/standards/track-and-trace/standard-documentation-track-and-trace> | VERIFIED-DOCS |

---

## 12. Interface additions other sections must know

1. **`build_client(settings, token_store, *, clock=None, http=None)`.** The carrier factories need `clock` (and test `http`) so the pipeline can pass `FixedClock(run_now)` (03 §3). Suggest adopting this signature for all carriers in 03 §9.
2. **Request-id header list** (03 §6 step 4): add `x-azure-ref`. MSC (Azure Front Door / APIM) returns it.
3. **CLI (11-cli.md):**
   - new command `eta msc-token`, which calls `MscClient.set_manual_token()` and prints `token_status()`;
   - `eta probe msc` options `--sandbox`, `--token-info`, `--booking` (repeatable), `--compare`, `--save-raw`;
   - **pre-flight in `eta run`**: if the input has MSC bookings and the client is in `mymsc` mode with `seconds_left < 600` (or no token) → prompt for a token on a TTY, else fail fast before any side effects.
4. **02-configuration.md:**
   - MSC env vars per §3.2;
   - `MSC_USER_NAME`, `MSC_PASSWORD`, `LOGIN_URL`, `HOME_PAGE` are legacy and unused by lookups;
   - `Settings.msc` is `None` only for an explicit `MSC_MODE=api` with missing API vars.
5. **09 (dcsa.py) — MSC payload quirks the shared parser should tolerate,** used by the MSC rescue path:
   - body may be a bare array **or** `{"events": [...]}`;
   - location is `transportCall.unLocationCode` / `eventLocation.unLocationCode` (camelCase), not `UNLocationCode`;
   - classifier is only `ACT`/`EST`;
   - timestamps use `Z` or 7 fractional digits;
   - `description` holds MSC's event names.
6. **10-pipeline.md:**
   - `CustomsHoldError` from MSC → `PotentialCustomsHold.txt`.
   - All MSC failures after a latch carry identical messages, so the run summary can group them: "MSC: 287 bookings failed — token expired".
   - `EtaResult.details["rescue"]` is `True` when §7.5 was used; the report should flag those rows.
