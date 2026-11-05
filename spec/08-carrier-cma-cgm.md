# 08 — Carrier: CMA CGM (official DCSA Track & Trace API)

Implements `src/eta_automations/carriers/cma.py`, conforming to `03-foundations.md` §9
(`CarrierClient`). Shared DCSA parsing lives in `carriers/dcsa.py`, owned by
`09-dcsa-aggregators-grimaldi.md`. This file only **uses** the names defined there and states the
behaviour CMA relies on (§7.4).

---

## 1. Summary

**What it replaces.** `CMA/checkETA.py` did this for every CMA booking:

1. Launch a headful Chrome. DataDome blocks headless browsers.
2. Wait up to 30 s for DataDome's device check, and up to 120 s for a person to solve a CAPTCHA.
3. Dismiss the OneTrust banner, then sleep 2 s.
4. Type the booking into the Kendo search box at 50 ms per character.
5. Submit a full-page POST (45 s timeout, 2 retries) and read the page globals `options.responseData` and `model`.
6. Save DataDome cookies to `auth_for_CMA.json`.

That is roughly 10–60 s per booking, it needs a desktop session, and it sometimes needs a human.

**What replaces it.** One authenticated HTTPS GET to CMA CGM's official **"Logistic Tracking service API — DCSA
OpenAPI specification for Track & Trace v2.2.0"**. In the portal catalogue it is listed as
`operation.trackandtrace.v1`:

```
GET https://apis.cma-cgm.net/operation/trackandtrace/v1/events/{bookingReference}?limit=500
KeyId: <your API key>
```

- **No browser, CAPTCHA or cookies.** The call is fully unattended.
- **Speed:** about one second per booking, two in flight at a time (UNVERIFIED latency).
- **Parsing:** the JSON array of DCSA events goes through `carriers/dcsa.py`.
- **Port of discharge (POD):** found exactly via CMA's `carrierSpecificData.shipmentLocationType == "POD"` extension, with no route guessing.

**Behaviour kept from the original:**

- An ETA per container at the POD.
- An actual discharge beats any estimate.
- The booking ETA is the **latest** across containers.
- The same "not found / no ETA / wrong booking" outcomes, with equivalent messages.
- The same `[CMA] …` progress lines.

## 2. Access & onboarding

CMA CGM API Portal: <https://api-portal.cma-cgm.com>. Registration is self-service.

| Step | What the user does | Notes |
|---|---|---|
| 1 | **Create account** on the API Portal: company details, then accept the API usage Terms & Conditions. | The first user of a client account gets the **Main User** role, which can view credentials (2FA may be required). |
| 2 | **Free trial** starts: *"free trial access to public APIs in production … available for 30 days and has a limited API call quota as determined by CMA CGM."* It includes the **Logistics Tracking service API**. | The quota is not published (UNVERIFIED). |
| 3 | Copy the **API key** from *API Applications*, at the bottom of your application card. | Put it in `.env` as `CMA_API_KEY`. It is sent as request header `KeyId`. |
| 4 | Run `eta probe cma --booking <a real NAM… booking>` (§10). | Confirms the key works and the payload matches this spec. |
| 5 | **Before the 30 days end**: *Commercial offers → Subscription request*. *"Upon submission, you will be contacted within 48 working hours for a contract proposal."* Sign (DocuSign). | After signature *"you can use same API key generated for your free trial application"*, with a contract rate limit. |
| 6 (optional) | **Private access (OAuth2)**: *"You will receive your client ID by mail after your setup is done"*. The secret is under *My Organization → Client Settings → API Credentials* (Main User, 2FA). | Needed only for private events. Private events are given *"only if you (or your end customer) are identified on the booking as: Booking Party, Deciding Party, Consignee, Notifier or Shipper"*. Third parties also need an LOA/client mandate and `behalfOf`. **Not required** for the POD ETA. |

**Access tiers, as documented in the API description:**

| Tier | Auth | Returns | Enough for this program? |
|---|---|---|---|
| Public | API key (`KeyId` header) | *"standard equipment moves (e.g. ready to be loaded, discharged), Transhipment moves … and planned vessel departure and arrival dates"*. The event catalogue lists, as **Public**: TRANSPORT ARRI ACT/EST/PLN "Vessel Arrival" and EQUIPMENT DISC ACT LADEN "Discharged". | **Yes** |
| Private | OAuth2 client credentials | Public events plus private ones: actual rail/ramp moves and inland planned dates. Also returns the **real** booking reference in `documentReferences` (public access gets a 64-hex hash instead). | Optional; enables the wrong-booking check (§7.2 R3) |

**Billing** (FAQ, "How am I billed for API usage?"):

- **Chargeable:** `200, 201, 204, 206, 404`.
- **Not chargeable:** `400, 401, 403, 429, 500, 502, 503, 504`.
- Prices are on the portal's *Commercial offers* page (UNVERIFIED amounts).

Volume is tiny: the sample input had 8 CMA rows out of 494, so expect a few dozen calls a month. The client still avoids
waste: it never retries a 404, and it shares one call between duplicate bookings in a run (§6.6).

**Lead time.** The trial starts right after registration (UNVERIFIED: the account may need approval first). A
contract proposal comes within 48 working hours of a subscription request; signature time depends on the
user.

## 3. Configuration

```python
@dataclass(frozen=True)
class CmaSettings:
    auth_mode: Literal["apikey", "oauth"]          # resolved (see rules below)
    api_key: str | None                            # CMA_API_KEY
    client_id: str | None                          # CMA_CLIENT_ID
    client_secret: str | None                      # CMA_CLIENT_SECRET
    oauth_scope: str                               # CMA_OAUTH_SCOPE
    token_url: str                                 # CMA_TOKEN_URL
    base_url: str                                  # CMA_BASE_URL (no trailing slash)
    lookup_mode: Literal["reference", "dcsa_query"]  # CMA_LOOKUP_MODE
    page_limit: int                                # CMA_PAGE_LIMIT
    max_pages: int                                 # CMA_MAX_PAGES
    behalf_of: str | None                          # CMA_BEHALF_OF
    max_concurrency: int                           # CMA_MAX_CONCURRENCY
    max_calls_per_minute: int | None               # CMA_MAX_CALLS_PER_MINUTE
    max_retry_after_s: int                         # CMA_MAX_RETRY_AFTER_S
    timeout_s: float                               # CMA_TIMEOUT_S
```

| Env var | Required? | Default | Meaning |
|---|---|---|---|
| `CMA_API_KEY` | one of key **or** id+secret | — | Public-tier API key ("keyId") from *API Applications*. Sent as header `KeyId`. |
| `CMA_CLIENT_ID` | with secret, for OAuth | — | Private-tier OAuth2 client id (mailed by CMA after contract setup). |
| `CMA_CLIENT_SECRET` | with id, for OAuth | — | OAuth2 client secret (*Client Settings → API Credentials*). |
| `CMA_AUTH_MODE` | no | `auto` | `auto` uses `oauth` if id and secret are both set, else `apikey` if the key is set. `apikey` and `oauth` force a mode. |
| `CMA_OAUTH_SCOPE` | no | `tandtcommercial:read:be tandtpublic:read:be` | Space-delimited scopes requested at the token endpoint. Both are listed on the `/events` operations. |
| `CMA_TOKEN_URL` | no | `https://auth.cma-cgm.com/as/token.oauth2` | OAuth2 token endpoint (PingFederate path). |
| `CMA_BASE_URL` | no | `https://apis.cma-cgm.net/operation/trackandtrace/v1` | API base. Override only for a UAT URL given by CMA support. |
| `CMA_LOOKUP_MODE` | no | `reference` | `reference` → `GET /events/{ref}` (CMA's recommended "simplest way", includes split/merged/part-load children). `dcsa_query` → `GET /events?carrierBookingReference={ref}` (DCSA-standard filter). |
| `CMA_PAGE_LIMIT` | no | `500` | `limit` query value, 1–500. The FAQ gives max 500 and default 100. |
| `CMA_MAX_PAGES` | no | `10` | Safety cap on cursor pages per lookup. |
| `CMA_BEHALF_OF` | no | — | CMA partner code of the end customer. *"Mandatory if you are a Third Party"*; leave empty for your own bookings. |
| `CMA_MAX_CONCURRENCY` | no | `2` | Size of the CMA semaphore (03-foundations §10). |
| `CMA_MAX_CALLS_PER_MINUTE` | no | unset | Optional proactive `AsyncLimiter(n, 60)`. Set it if probes or runs hit 429. |
| `CMA_MAX_RETRY_AFTER_S` | no | `120` | Longest `Retry-After` the client will wait out. Longer → lookup fails (§6.5). |
| `CMA_TIMEOUT_S` | no | `30` | Read timeout; connect timeout stays 10 s. |

**Resolution rules** (in `config.py`; see 03-foundations §12).

*Auth mode:*

- `auto` with id + secret → `oauth`.
- Otherwise `auto` with an API key → `apikey`.
- Otherwise `settings.cma = None`, with `missing["cma"] = ("CMA_API_KEY", "CMA_CLIENT_ID", "CMA_CLIENT_SECRET")`. The router's
  not-configured message then reads: `CMA lookups are not configured — set CMA_API_KEY (or CMA_CLIENT_ID + CMA_CLIENT_SECRET) in .env`.

*Explicit modes with missing credentials:*

- `CMA_AUTH_MODE=apikey` without a key → `cma=None`, `missing["cma"]=("CMA_API_KEY",)`.
- `CMA_AUTH_MODE=oauth` with id or secret missing → `cma=None`, and `missing["cma"]` lists the missing one(s).

*`ConfigError` (malformed values only):*

- `CMA_AUTH_MODE` is not one of `auto|apikey|oauth`.
- `CMA_LOOKUP_MODE` is not `reference|dcsa_query`.
- A numeric value is not an integer or float, or is out of range: `page_limit` must be 1–500, every other count ≥ 1,
  `max_retry_after_s` ≥ 0, `timeout_s` > 0.
- `CMA_BASE_URL` or `CMA_TOKEN_URL` does not start with `https://`.

*Normalisation:*

- Strip a trailing `/` from `base_url`.
- Strip whitespace from all values.

**Obsolete.** `auth_for_CMA.json`, the DataDome cookie file, is no longer used. Tell the user to delete it.

## 4. Endpoints

### 4.1 Gateway

- **Production base:** `https://apis.cma-cgm.net`. The portal guide states *"The base URL of production APIs is : https://apis.cma-cgm.net"*.
  This API lives at `/operation/trackandtrace/v1`, per the OpenAPI `servers[0].url`.
- **Gateway behaviour:** a Kong gateway behind Akamai's CDN. This is a normal API endpoint, not a bot-protected website.
- **Observed unauthenticated call:** `HTTP 401` with body `{"message":"Unauthorized","request_id":"<hex>"}`, plus headers
  `x-request-id`, `x-correlationid`, `x-kong-request-id` and `cache-control: max-age=0, no-cache, no-store`.
- **Request ID:** `x-request-id` is already in 03-foundations' request-id header list.
- **CORS:** not supported. That is irrelevant here, since this is server-side code.
- **Headers on every call:**
  - `Accept: application/json`
  - API-key mode: `KeyId: <CMA_API_KEY>`
  - OAuth mode: `Authorization: Bearer <access_token>`

### 4.2 Primary: `GET /events/{trackingReference}` (operationId `getMoveOnCommercialCycle`)

- **Description:** *"Return commercial cycle belonging to provided tracking reference. Query is not DCSA compliant but
  Return data follow DCSA TNT 2.2.0 specifications."*
- **Path parameter:** `trackingReference` is the booking reference (URL-encode with `urllib.parse.quote(ref, safe="")`).
- **Query parameters:**
  - `limit` (int ≥ 1; CMA allows ≤ 500)
  - `cursor` (string, from `Next-Page`)
  - `behalfOf` (third parties only)
- **Security:** either `oAuth2` with scopes `tandtcommercial:read:be` + `tandtpublic:read:be`, or `ApiKeyAuth` (header `keyId`).
  The guide spells the header `KeyId`; header names are case-insensitive.
- **FAQ:**
  - *"The simplest way to retrieve events is to call the endpoint /events/{trackingReference} using the booking
    reference as the trackingReference, for example, /events/RTM1234567."*
  - It also returns the events of split/merged/part-load child bookings.
  - *"The latter is primarily designed for backward compatibility with the former Tracking API and provides more
    detailed information about the commercial cycle."* This is the closest equivalent of the website search the original used.

### 4.3 Alternative: `GET /events` (operationId `searchMoveOnCommercialCycle`, DCSA-compliant)

- **Query parameters used:** `carrierBookingReference={ref}` (max length 35), `limit`, `cursor`, and optionally `behalfOf`.
- **Other filters exist but are not used:** `eventType`, `transportEventTypeCode`, `equipmentEventTypeCode`, `equipmentReference`,
  `transportDocumentReference`, `UNLocationCode`, `eventCreatedDateTime:gte`, …
- **When to use it:** only when `CMA_LOOKUP_MODE=dcsa_query`. The probe always calls it for comparison.
- **Why results can differ:** the FAQ says results *"differ"* between the two endpoints, because `/events` is DCSA-compliant and
  `/events/{trackingReference}` is not.

### 4.4 Pagination (both endpoints)

- **Mechanism:** DCSA cursor pagination. The response is always `200`.
- **Headers:** `Current-Page` (required), plus `Next-Page`, `Prev-Page`, `First-Page`, `Last-Page` (optional).
  Example value: `fE9mZnNldHw9NSZsaW1pdD01`.
- **Next page:** *"specify the optional query parameter `cursor` using a previous returned value from the headers"*. Continue
  *"until the next-page header is no longer returned"*.
- **Cursor format:** treat the header value as either a raw cursor token or a URL/query containing `cursor=` (UNVERIFIED which;
  see `extract_cursor`, §6.3).
- **Follow-up requests:** same path, with params `{"cursor": <token>, "limit": page_limit}` plus `behalfOf` if set. The original
  filter params are **not** repeated, because the cursor encodes the query (DCSA convention; UNVERIFIED for CMA — covered by the probe).

### 4.5 Response body

- **Shape:** a JSON **array** of event objects, each one of `transportEvent | shipmentEvent | equipmentEvent` per the OpenAPI.
- **Shipment events:** returned only to business parties. They are ignored here.
- **Fields CMA logic reads:**

| Field | Where | Use |
|---|---|---|
| `eventType` | all | `TRANSPORT` / `EQUIPMENT` / `SHIPMENT` |
| `eventClassifierCode` | all | `ACT` (actual) / `EST` (estimated) / `PLN` (planned) |
| `eventDateTime` | all | ISO-8601 with offset, *"The local date and time, where the event took place or when the event will take place"*. Example: `2019-11-12T07:41:00+08:30`. |
| `eventCreatedDateTime` | all (required) | Newer estimates supersede older ones (09) |
| `transportEventTypeCode` | TRANSPORT | `ARRI` / `DEPA` |
| `equipmentEventTypeCode` | EQUIPMENT | `LOAD, DISC, GTIN, GTOT, STUF, STRP, PICK, DROP, INSP, RSEA, RMVD` |
| `equipmentReference` | EQUIPMENT | Container number (≤ 15 chars) |
| `emptyIndicatorCode` | EQUIPMENT (required) | `EMPTY` / `LADEN` |
| `transportCall.UNLocationCode`, `.transportCallSequenceNumber`, `.facilityTypeCode` (e.g. `POTE`), `.modeOfTransport` (`VESSEL/RAIL/TRUCK/BARGE`), `.vessel.vesselName` | TRANSPORT (and EQUIPMENT moves on a vessel) | Location, ordering, display |
| `eventLocation.UNLocationCode` | EQUIPMENT | Location |
| `documentReferences[] {documentReferenceType: "BKG"\|"TRD", documentReferenceValue}` | all | Booking identity check (§7.2 R3). With public access the BKG value is a 64-hex hash (FAQ). |
| `carrierSpecificData.shipmentLocationType` | all (CMA extension) | `DEPOT, COL, ABP_EXP, POL, PTS, POD, ABP_IMP, DEL`. **`POD` = "Discharge location - Vessel"**. |
| `carrierSpecificData.transportationPhase` | all (CMA ext.) | `Export` / `Transshipment` / `Import` |
| `carrierSpecificData.internalEventLabel`, `.internalEventCode`, `.internalLocationCode` | CMA ext. | Logged by probe only |
| `carrierSpecificData.originBookingReference`, `.splitToBookingReferences[]`, `.mergeToBookingReference`, `.masterPartLoadBookingReference`, `.partLoadGroupBookingReferences[]` | CMA ext. | Split/merge relations, used in the identity check (§7.2 R3) |

Realistic shapes of the two event kinds this module reads (synthetic values):

```json
{
  "eventID": "0b6f1f7e-0000-4000-8000-000000000005",
  "eventCreatedDateTime": "2026-10-18T19:30:00Z",
  "eventType": "EQUIPMENT",
  "eventClassifierCode": "ACT",
  "eventDateTime": "2026-10-18T03:20:00+08:00",
  "equipmentEventTypeCode": "LOAD",
  "equipmentReference": "CMAU1234567",
  "ISOEquipmentCode": "45G1",
  "emptyIndicatorCode": "LADEN",
  "eventLocation": {
    "locationName": "CNSHA",
    "UNLocationCode": "CNSHA"
  },
  "documentReferences": [
    {
      "documentReferenceType": "BKG",
      "documentReferenceValue": "769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"
    }
  ],
  "carrierSpecificData": {
    "internalEventLabel": "Loaded on board",
    "internalLocationCode": "CNSHA",
    "transportationPhase": "Export",
    "shipmentLocationType": "POL",
    "transportCallSequenceTotal": 3
  },
  "transportCall": {
    "transportCallID": "TC-CNSHA-1",
    "carrierServiceCode": "PEX3",
    "exportVoyageNumber": "0PE3UW1MA",
    "transportCallSequenceNumber": 1,
    "UNLocationCode": "CNSHA",
    "facilityCode": "SGH",
    "facilityCodeListProvider": "SMDG",
    "facilityTypeCode": "POTE",
    "modeOfTransport": "VESSEL",
    "location": {
      "locationName": "CNSHA",
      "UNLocationCode": "CNSHA"
    },
    "vessel": {
      "vesselIMONumber": "9999991",
      "vesselName": "CMA CGM SAMPLE"
    }
  }
}
```

```json
{
  "eventID": "0b6f1f7e-0000-4000-8000-000000000009",
  "eventCreatedDateTime": "2026-10-19T06:00:00Z",
  "eventType": "TRANSPORT",
  "eventClassifierCode": "EST",
  "eventDateTime": "2026-11-12T06:00:00-08:00",
  "transportEventTypeCode": "ARRI",
  "transportCall": {
    "transportCallID": "TC-USLGB-2",
    "carrierServiceCode": "PEX3",
    "exportVoyageNumber": "0PE3UW1MA",
    "transportCallSequenceNumber": 2,
    "UNLocationCode": "USLGB",
    "facilityCode": "LBT",
    "facilityCodeListProvider": "SMDG",
    "facilityTypeCode": "POTE",
    "modeOfTransport": "VESSEL",
    "location": {
      "locationName": "USLGB",
      "UNLocationCode": "USLGB"
    },
    "vessel": {
      "vesselIMONumber": "9999991",
      "vesselName": "CMA CGM SAMPLE"
    }
  },
  "documentReferences": [
    {
      "documentReferenceType": "BKG",
      "documentReferenceValue": "769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"
    }
  ],
  "carrierSpecificData": {
    "internalEventLabel": "Estimated Vessel Arrival",
    "internalLocationCode": "USLGB",
    "transportationPhase": "Import",
    "shipmentLocationType": "POD",
    "transportCallSequenceTotal": 3
  }
}
```

### 4.6 Status codes

| Status | Meaning (CMA docs / observed) | Client behaviour |
|---|---|---|
| 200 | Events (possibly `[]`) | Parse. `[]` on the first page means "shipment not found" (§7.2 R1). |
| 401 | Missing/invalid key or token. Observed body `{"message":"Unauthorized","request_id":…}` | OAuth: refresh the token once (`request_with_auth`). Then `CarrierLookupError` with the 401 hint (§6.7). |
| 403 | *"No sufficient rights to perform the request"* | `CarrierLookupError` with the 403 hint. Not treated as "token expired". |
| 404 | *"The API returns a "404 Not Found" error if the booking has not been confirmed or has already been cancelled."* Chargeable. | `CarrierLookupError` (§7.2 R1). **Never retried.** |
| 429 | *"Rate limit reached"*, with a standard **`retry-after`** header. *"If not respected, the rate limit will not be reinitialised automatically."* | Wait the exact `Retry-After` (§6.5). Never retry earlier. |
| 500/502/503/504 | Server/gateway errors (non-chargeable) | Retry with backoff (§6.5), then `CarrierLookupError`. |
| other 4xx (e.g. 400) | DCSA error body: `{httpMethod, requestUri, statusCode, statusCodeText, errorDateTime, errors:[{reason, message}]}` | `CarrierLookupError` including `errors[*].reason/message` when the body parses. |

### 4.7 Rate limits

- **Per-application quota.** The portal guide says *"We limit the number of requests each application can make"*. Exceeding it
  returns 429 + `retry-after`. The numbers are not published (UNVERIFIED). The free trial has *"a limited API call quota"*.
- **Batch use is discouraged.** CMA *"highly encourages clients to setup transactional interfaces with local cache rather than
  regular bulk data retrieval"*. This program does one call per CMA booking per run, plus pages, which is transactional use.
  Responses carry `cache-control`. The in-run memo (§6.6) is the only cache, and no disk cache is needed at this volume.

### 4.8 OAuth2 token endpoint (private tier only)

```
POST https://auth.cma-cgm.com/as/token.oauth2
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials&client_id=<id>&client_secret=<secret>&scope=tandtcommercial:read:be tandtpublic:read:be
```

- **Response** (portal guide): `{"access_token": "<token_value>", "token_type": "Bearer", "expires_in": 299}`.
- **Lifetime:** *"The access token lasts for 5 minutes."*
- **Errors:** RFC 6749 bodies, e.g. `{"error":"invalid_client","error_description":"…"}` (UNVERIFIED wording). Map them to `AuthError`.

## 5. Auth / token lifecycle

Two `TokenProvider` implementations (03-foundations §8), both in `cma.py`.

**`CmaApiKeyAuth(api_key)`**

- `get_token()` returns the key.
- `invalidate()` is a no-op.
- Headers: `build_headers=lambda k: {"KeyId": k}`.
- A 401 is retried once by `request_with_auth()`. That is harmless; the second 401 raises `AuthError`.

**`CmaOAuth(settings, token_store, http)`**

- **Token cache:** TokenStore key `"cma"`, value `{"access_token", "expires_at", "token_type", "scope"}`.
- **`get_token()`:**
  1. Return the cached token if `is_fresh(entry, skew_seconds=60)`.
  2. Otherwise, under an `asyncio.Lock`, re-check the cache.
  3. Then call `request(http, "POST", token_url, service="cma", data=<form>, expected=(200,), idempotent=True)`. The default
     03 retries apply to this call.
  4. Store `expires_at = time.time() + float(body.get("expires_in", 299))`.
- **Skew:** 60 s rather than the default 120 s, because the token only lives 299 s.
- **`invalidate()`:** deletes the `"cma"` entry from the store and from memory.
- **Token-endpoint errors:**
  - `ServiceError` with status 400/401 → `AuthError(f"cma: OAuth token request rejected: {error}: {error_description}")`,
    with both values read from the JSON body when present.
  - Other failures → `AuthError(f"cma: OAuth token request failed: {exc}")`.
  - In both cases the boundary rule converts the `AuthError` to `CarrierLookupError` (§6.7).
- **Headers:** default `build_headers` (`Authorization: Bearer …`).

Never log the key, client secret or token (03-foundations §11 already masks `keyid`, `client_secret`, `token`).

## 6. Module design (`src/eta_automations/carriers/cma.py`)

Logger: `log = logging.getLogger("eta.carrier.cma")`.

```python
TNT_PATH_REFERENCE = "/events/{ref}"
TNT_PATH_QUERY = "/events"
RETRY_STATUSES = frozenset({500, 502, 503, 504})
MAX_TRANSIENT_ATTEMPTS = 3          # total attempts for 5xx / network errors
MAX_RATE_LIMIT_WAITS = 3            # 429 waits per logical call
DEFAULT_RETRY_AFTER_S = 60.0        # when a 429 has no usable Retry-After
PLAUSIBLE_YEARS = range(2000, 2101) # original: accept only years 2000–2100
BOOKING_REF_RE = re.compile(r"^[A-Z0-9]{1,35}$")
HASHED_REF_RE = re.compile(r"^[0-9A-F]{32,}$")                        # FAQ: public access gets a 64-hex hash
UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")

class CmaApiKeyAuth: ...            # §5
class CmaOAuth: ...                 # §5

@dataclass(frozen=True, slots=True)
class FetchResult:
    events: list[Any]               # concatenated raw items from all pages
    pages: int
    requests: list[dict[str, Any]]  # per-request summaries for probe/logs (no secrets)
    endpoint: Literal["reference", "dcsa_query"]

class CmaClient:
    carrier = Carrier.CMA
    def __init__(self, settings: CmaSettings, token_store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None: ...
    async def get_eta(self, booking: str) -> EtaResult: ...
    async def probe(self, booking: str) -> dict[str, Any]: ...
    async def aclose(self) -> None: ...
    # internals
    async def _lookup(self, ref: str) -> EtaResult: ...
    async def _fetch_events(self, ref: str, mode: Literal["reference", "dcsa_query"]) -> FetchResult: ...
    async def _send(self, url: str, params: dict[str, str], summaries: list[dict[str, Any]]) -> httpx.Response: ...

def build_client(settings: Settings, token_store: TokenStore) -> CmaClient | None:
    return None if settings.cma is None else CmaClient(settings.cma, token_store)

# ── pure helpers (unit-tested directly) ──
def extract_cursor(header_value: str) -> str: ...
def prefilter_raw_events(raw: list[Any]) -> list[dict[str, Any]]: ...
def check_booking_identity(ref: str, raw: list[dict[str, Any]]) -> None: ...
def resolve_pod_unlocode(events: Sequence[DcsaEvent]) -> str | None: ...
def container_references(events: Sequence[DcsaEvent]) -> list[str]: ...
def is_empty_equipment(event: DcsaEvent) -> bool: ...
def describe_arrival(arrival: DcsaArrival) -> str: ...
def evaluate_events(ref: str, raw_events: list[Any], *, endpoint: str = "reference", pages: int = 1) -> EtaResult: ...
```

### 6.1 Constructor

- **HTTP client:** if `http` is None, create `new_async_client(base_url="", headers={"Accept": "application/json"},
  timeout=httpx.Timeout(settings.timeout_s, connect=10.0))` and remember to close it in `aclose()`. Never close an injected client.
  Build absolute URLs (`f"{settings.base_url}{path}"`), because the token URL is on another host.
- **Auth:** `CmaOAuth(...)` if `settings.auth_mode == "oauth"`, else `CmaApiKeyAuth(settings.api_key)`.
- **Concurrency:**
  - `self._sem = asyncio.Semaphore(settings.max_concurrency)`
  - `self._limiter = AsyncLimiter(settings.max_calls_per_minute, 60.0)` if set, else None
  - `self._blocked_until = 0.0` (a `time.monotonic()` value shared by all CMA calls, §6.5)
  - `self._memo: dict[str, asyncio.Task[EtaResult]] = {}`

### 6.2 `get_eta(booking)`, step by step

1. **Normalise:** `ref = booking.strip().upper()`.
   - Empty → `CarrierLookupError("No CMA booking number given.")`.
   - Not matching `BOOKING_REF_RE` → `CarrierLookupError(f"'{ref}' is not a valid CMA booking reference.")`.
   - The router already guarantees a bare `NAM\d{7}` token (01-business-rules §4, D6), so these are defensive checks.
2. **Single-flight memo (§6.6):** `task = self._memo.get(ref)`. If there is none, create it with
   `asyncio.create_task(self._lookup(ref))` and store it. Then `return await asyncio.shield(task)`.
3. **`_lookup(ref)`:**
   1. Log `log.info("  [CMA] Looking up ETA for booking %s...", ref)`.
   2. `fr = await self._fetch_events(ref, settings.lookup_mode)`.
   3. Log `log.info("  [CMA] responded OK — parsing for ETA...")`.
   4. `result = evaluate_events(ref, fr.events, endpoint=fr.endpoint, pages=fr.pages)`.
   5. Log `log.info("  [CMA] Returning ETA: %s", result.eta.strftime("%m/%d/%Y"))`, then return.
   6. **Boundary rule (03-foundations §5):**
      - Let `CarrierLookupError` and `NoEtaError` propagate unchanged.
      - Wrap `ServiceError`, `AuthError`, `httpx.HTTPError`, `KeyError`, `TypeError` and `ValueError` as
        `CarrierLookupError(f"Failed to get CMA tracking info for {ref}: {exc}") from exc`, unless §6.7 gives a more specific message.

### 6.3 `_fetch_events(ref, mode)`

1. **URL and params:**
   - `reference` → `url = base_url + "/events/" + quote(ref, safe="")`, `params = {"limit": str(page_limit)}`.
   - `dcsa_query` → `url = base_url + "/events"`, `params = {"carrierBookingReference": ref, "limit": str(page_limit)}`.
   - If `behalf_of` is set, add `params["behalfOf"] = behalf_of` in both modes.
2. **Page loop.** Run `for page in range(1, max_pages + 1)`:
   1. `resp = await self._send(url, params, summaries)`.
   2. **Status 404:**
      - On page 1 → raise `CarrierLookupError` with the 404 message (§7.2 R1).
      - On a later page → stop paging.
   3. **Status 200:** `body = json_body(resp, service="cma")`.
      - A list → those are the items.
      - A dict holding a list under `"events"`, `"data"` or `"items"` (defensive) → use that list.
      - Anything else → `CarrierLookupError(f"Could not read CMA tracking data for {ref}: expected a JSON array of events, got {type(body).__name__} with keys {sorted(body)[:10] if isinstance(body, dict) else '-'}")`.
   4. Append the items to `events`.
   5. `nxt = resp.headers.get("Next-Page")`. If `nxt` is empty or the page had no items, stop paging.
   6. Otherwise set `params = {"cursor": extract_cursor(nxt), "limit": str(page_limit)}`, plus `behalfOf` if set, and continue.
3. **Page cap:** if the loop ends because `max_pages` was reached while `Next-Page` was still present, log a WARNING
   (`"[CMA] booking %s has more than %d pages of events — using the first %d"`) and keep what was collected.
4. **Return** `FetchResult(events, pages, summaries, mode)`.

`extract_cursor(v)`:

- `v.strip()`.
- If it contains `cursor=`, parse it with `urllib.parse.urlsplit` (or `parse_qs` on the part after `?`, or on the whole string when there
  is no `?`) and return the first `cursor` value.
- Otherwise return the stripped value unchanged.
- Examples:
  - `"fE9mZnNldHw9NSZsaW1pdD01"` → itself.
  - `"https://apis.cma-cgm.net/operation/trackandtrace/v1/events?cursor=abc&limit=5"` → `"abc"`.
  - `"cursor=abc"` → `"abc"`.

### 6.4 `_send(url, params, summaries)` — one logical call

The CMA call policy below **overrides 03-foundations §6 retry policy for CMA API calls**. CMA requires `Retry-After` to be
honoured in full, which may exceed the 30 s cap in 03. Each physical attempt therefore uses
`max_attempts=1`, so 03's loop never retries on its own.

```python
transient_attempt = 0
rate_waits = 0
while True:
    await self._wait_if_blocked()                     # sleep until self._blocked_until (monotonic), if in future
    started = time.monotonic()
    try:
        async with self._sem:
            resp = await request_with_auth(
                self._http, "GET", url, service="cma", auth=self._auth,
                build_headers=self._build_headers,     # KeyId or Bearer
                params=params, expected=(200, 404, 429), idempotent=True,
                max_attempts=1, limiter=self._limiter)
    except ServiceError as exc:                       # 5xx, network (status None), other 4xx
        summaries.append(_summary(url, params, exc.status, started, exc.request_id))
        if (exc.status is None or exc.status in RETRY_STATUSES) and transient_attempt < MAX_TRANSIENT_ATTEMPTS - 1:
            transient_attempt += 1
            await self._sleep(min(30.0, 1.0 * 2 ** (transient_attempt - 1)) + random.uniform(0, 0.25))
            continue
        raise _map_service_error(exc)                 # §6.7
    except AuthError as exc:
        raise CarrierLookupError(HINT_401) from exc
    summaries.append(_summary(url, params, resp.status_code, started, _request_id(resp), resp.headers))
    if resp.status_code != 429:
        return resp                                   # 200 or 404
    ra = _parse_retry_after(resp.headers.get("Retry-After"), default=DEFAULT_RETRY_AFTER_S)
    if ra > settings.max_retry_after_s or rate_waits >= MAX_RATE_LIMIT_WAITS:
        raise CarrierLookupError(f"CMA rate limit reached (HTTP 429, retry after {ra:.0f}s; "
                                 f"CMA_MAX_RETRY_AFTER_S={settings.max_retry_after_s}). Try again later or ask "
                                 f"CMA to raise your application's quota.")
    rate_waits += 1
    self._blocked_until = max(self._blocked_until, time.monotonic() + ra + 0.5)
    log.warning("[CMA] rate limited (HTTP 429) — waiting %.0fs as instructed by Retry-After", ra)
```

How the pieces fit:

- **403 is not "token expired".** `request_with_auth` is used with its default 401-only refresh behaviour.
- **401 recovery.** `request_with_auth` refreshes the token once in OAuth mode. A second 401 raises `AuthError`, which becomes `HINT_401`.
- **403 mapping.** A 403 arrives as `ServiceError(status=403)` and `_map_service_error` turns it into `HINT_403`.
- **The semaphore is held only around the HTTP attempt.** It is never held during the sleeps.
- **`_parse_retry_after(value, default)`:**
  - An integer or float → that many seconds.
  - An HTTP-date (`email.utils.parsedate_to_datetime`) → seconds until that time, floored at 0.
  - Missing or unparseable → `default`.
- **`_request_id(resp)`** returns the first present of `x-request-id`, `x-correlationid`, `x-kong-request-id`.
- **`_summary(...)`** returns `{"method":"GET","url": redact(url_with_params),"status":…, "elapsed_ms":…, "request_id":…,
  "api_version": headers.get("API-Version"), "next_page": bool(headers.get("Next-Page")),
  "cache_control": headers.get("cache-control"), "retry_after": headers.get("Retry-After")}`.

### 6.5 Shared rate-limit gate

`self._blocked_until` makes every concurrent CMA call wait once any call receives a 429, because CMA's quota is per
application. `_wait_if_blocked()` computes `delay = self._blocked_until - time.monotonic()` and, if `delay > 0`, calls
`await self._sleep(delay)`. The `sleep` callable is injectable so tests run instantly and can assert the waits.

### 6.6 Single-flight memo

- **Scope:** the memo lives as long as the client, i.e. one run. Both successes and failures are memoised.
- **Why failures too:** the same booking can appear on two input rows, e.g. `NAM1234567` and `NAM1234567 A`. Re-asking costs a
  billable call and would give the same answer within minutes.
- **Cancellation:** `asyncio.shield` keeps one caller's cancellation from cancelling the shared lookup.
- **Probe:** `probe()` bypasses the memo.

### 6.7 Error mapping

| Situation | Exception | Message (exact text; `{ref}` = booking) |
|---|---|---|
| HTTP 404 on page 1 | `CarrierLookupError` | `CMA tracking API has no record of booking {ref} (HTTP 404). CMA returns 404 when a booking is not confirmed yet or was cancelled; CMA also drops shipments whose containers were returned to the depot more than 15 days ago.` |
| 200 with `[]` on page 1 | `CarrierLookupError` | `CMA public tracking shows 'shipment not found' for booking {ref}. Check the booking number — CMA also drops shipments whose containers were returned to the depot more than 15 days ago.` (original text) |
| 401 (after the OAuth refresh) | `CarrierLookupError` (`HINT_401`) | `CMA rejected the API credentials (HTTP 401). Check CMA_API_KEY (API Portal → API Applications → your application card) or CMA_CLIENT_ID/CMA_CLIENT_SECRET; the 30-day free trial may have ended.` |
| 403 | `CarrierLookupError` (`HINT_403`) | `CMA refused the request (HTTP 403: no sufficient rights). Check that your API application is subscribed to the 'Logistic Tracking service API' (operation.trackandtrace.v1) and that your trial or contract is active.` |
| 429 beyond cap | `CarrierLookupError` | see §6.4 |
| 5xx / network after retries | `CarrierLookupError` | `Failed to get CMA tracking info for {ref}: {ServiceError text}` |
| other 4xx with DCSA error body | `CarrierLookupError` | `CMA tracking API rejected the request for {ref} (HTTP {status}): {reason}: {message}[; …]` (fallback: the `ServiceError` text) |
| Body not JSON / not an array | `CarrierLookupError` | `Could not read CMA tracking data for {ref}: …` (original prefix) |
| `dcsa.parse_events` raises `KeyError`/`TypeError`/`ValueError` | `CarrierLookupError` | `Could not read CMA tracking data for {ref}: {exc!r}` |
| Wrong booking returned | `CarrierLookupError` | `CMA returned tracking for {others} instead of the requested booking {ref}.` (original text; `{others}` = sorted, comma-joined) |
| Events but none with a plausible date | `NoEtaError` | `CMA returned events for booking {ref} but none carried a usable date. Can't determine ETA.` |
| No containers | `NoEtaError` | `CMA found booking {ref} but listed no containers to read an ETA from. Can't determine ETA.` (original text) |
| Some container lacks an ETA | `NoEtaError` | `No ETA published by CMA for booking {ref} (container{'s' if len(missing) > 1 else ''} {', '.join(missing)}). Can't determine ETA.` (original text) |

The pipeline writes these as `[ERROR] #{doc} - Carrier Lookup Failed.\n{err}\n` or `[ERROR] #{doc} -  Can't determine ETA.\n{err}\n`
(01-business-rules §6.1, §14). CMA never raises `CustomsHoldError`; that heuristic is MSC-only.

### 6.8 `probe(booking)`

1. `ref` is normalised as in §6.2 step 1. The probe **bypasses the memo**.
2. `info = {"auth_mode", "base_url", "lookup_mode", "page_limit", "behalf_of_set": bool}`. No secrets.
3. **OAuth mode:** call `self._auth.invalidate()`, then `get_token()`, and record `{"token_ok": True, "expires_in": …, "scope": …}`
   or the error.
4. **Fetch with both endpoints:** first the configured mode, then the other one.
   - Each is wrapped in try/except, and errors are recorded as strings.
   - **This makes 2 or more billable calls**; the CLI prints this before running.
5. For each successful fetch, compute `analysis`:
   - `event_count`, `pages`
   - `counts` keyed `"TRANSPORT ARRI EST"`, `"EQUIPMENT DISC ACT LADEN"`, …
   - `shipment_location_types`: set of `carrierSpecificData.shipmentLocationType` values
   - `carrier_specific_data_ratio`
   - `pod` (from `resolve_pod_unlocode`)
   - `containers`
   - `per_container`: container → `{eta, classifier, basis, label}`
   - `booking_refs_seen`, and whether the identity check ran or was skipped (hashed)
   - `eta`: the `evaluate_events` result as a dict, or `"<ExceptionName>: <message>"`
6. **Return:**

   ```python
   {"info": info,
    "requests": summaries,
    "raw": {"reference": redact(events_ref), "dcsa_query": redact(events_q)},
    "eta": <configured-mode result>,
    "analysis": {"reference": …, "dcsa_query": …, "endpoints_agree": <same eta & containers>}}
   ```

### 6.9 Logging (parity with the original prints)

- **INFO lines:**
  - `  [CMA] Looking up ETA for booking NAM1234567...`
  - `  [CMA] responded OK — parsing for ETA...`
  - one per container: `  [CMA] Container CMAU1234567: found 'ETA Berth at POD (estimated vessel arrival)': 11/10/2026 14:00`
  - for multi-container bookings: `  [CMA] Booking has 2 containers — using the latest ETA.`
  - `  [CMA] Returning ETA: 11/10/2026`
- **WARNING:** 429 waits and the page cap.
- **DEBUG:** dropped sentinel/implausible events (count), resolved POD and identity-check outcome.

## 7. Business logic

### 7.1 Concept mapping: original website globals → DCSA events

| Original (website) | Meaning | API equivalent used now |
|---|---|---|
| `responseData.ContextInfo.TitleLeft` = **"Arrived at POD"**, `ValueLeft` = discharge time (beats per-container fields) | Container discharged at the port of discharge | **EQUIPMENT `DISC` `ACT`** with `emptyIndicatorCode` ≠ `EMPTY`, at the POD UN/LOCODE, for that container. The `eventDateTime` is the discharge time, and `is_actual=True`. |
| — (vessel arrived, container not yet discharged) | Vessel at POD | **TRANSPORT `ARRI` `ACT`** at the POD → `is_actual=True` ("Vessel Arrival", Public) |
| `TitleLeft` = **"ETA Berth at POD"** / `EstimatedTimeOfArrival` / `ContainerMoveDetails.estimatedTimeOfArrival` | Current estimate of vessel arrival at POD | **TRANSPORT `ARRI` `EST`** at the POD ("Estimated Vessel Arrival", EVA). The newest `eventCreatedDateTime` wins. |
| `routingInformation.portOfDischarge.date` | Planned arrival from the routing | **TRANSPORT `ARRI` `PLN`** at the POD ("Planned Vessel Arrival", PVA) |
| `ActualTimeOfArrival`, `PODDate` (detail fallbacks) | Actual/POD date | Covered by `ARRI ACT` / `DISC ACT` above |
| `TitleLeft` = **"ETA at FPD"** (final place of delivery, inland) | Inland ETA | **Not used — DEVIATION CMA-D1** |
| `model[]` (one entry per container) | Containers on the booking | Distinct `equipmentReference` values of EQUIPMENT events (any empty indicator) |
| `SearchByReference` ≠ requested | Wrong booking returned | `documentReferences` BKG values plus split/merge relations (§7.2 R3) |
| Sentinel dates `0001-01-01…`, `9999-12-31…`; only years 2000–2100 accepted | Unknown dates | Events whose `eventDateTime` year is outside 2000–2100 are dropped before parsing (§7.2 R2) |
| `options.responseData` empty and `model` empty → "shipment not found" | Unknown booking | HTTP 404 (unconfirmed/cancelled) or `[]` (§7.2 R1) |
| Latest ETA across containers (*"A booking is only fully in once its last container arrives."*) | Booking-level ETA | Same: **policy "latest"** (§7.3) |

**Precedence for one container at the POD** (highest first):

1. `DISC ACT` for that container.
2. Vessel-level `ARRI ACT`.
3. `ARRI EST`.
4. `ARRI PLN`.

Within one classifier, the newest `eventCreatedDateTime` wins. This mirrors the original:

- The headline ("Arrived at POD" = discharge time, else "ETA Berth at POD") beat the per-container fields.
- The per-container estimate came before the planned routing date.

### 7.2 Rules (implemented in `evaluate_events`)

- **R1 Not found.**
  - A 404 on page 1, or a `200 []` on page 1, raises `CarrierLookupError` with the texts in §6.7.
  - **R1b:** if items were returned but every one is dropped by R2 → `NoEtaError` ("none carried a usable date").
- **R2 Prefilter** (`prefilter_raw_events`):
  - Keep only `dict` items.
  - Drop items whose `eventDateTime` is missing or whose year is outside `PLAUSIBLE_YEARS`. The check is
    `re.match(r"^\s*(\d{4})-", value)` and must not parse the full datetime, so year 0001/9999 can never crash a parser.
  - Log a DEBUG count of dropped items.
- **R3 Booking identity** (`check_booking_identity(ref, raw)`):
  1. `seen` = every `documentReferences[*].documentReferenceValue` whose `documentReferenceType` upper-cased starts with
     `"BKG"`, stripped and upper-cased, excluding hashed values (`HASHED_REF_RE`) and UUIDs (`UUID_RE`).
  2. `related` = upper-cased values of `carrierSpecificData.originBookingReference`, `.mergeToBookingReference`,
     `.masterPartLoadBookingReference`, plus every element of `.splitToBookingReferences` and `.partLoadGroupBookingReferences`.
  3. If `seen` is non-empty, `ref not in seen` and `ref not in related` → `CarrierLookupError` ("CMA returned tracking for …").
  4. If `seen` is empty, which is normal with API-key access, skip the check (DEBUG log). This is **DEVIATION CMA-D3**.
- **R4 Parse:** `events = dcsa.parse_events(raw)` (09).
- **R5 POD** (`resolve_pod_unlocode(events)`):
  1. Collect `(unlocode, transport_call_sequence)` from events whose `raw["carrierSpecificData"]["shipmentLocationType"]`
     upper-cased equals `"POD"` and whose `unlocode` is set.
  2. Choose the UN/LOCODE with the highest sequence number, with `None` treated as −1. On a tie, take the one with the most events.
  3. If there is no such event, return `None`. `dcsa.py` then applies its own POD heuristic (E6).
  4. Transshipment discharges carry `shipmentLocationType == "PTS"`, so they are never mistaken for the POD.
- **R6 Containers** (`container_references(events)`): `equipment_reference` of EQUIPMENT events, stripped, upper-cased and
  de-duplicated in **first-seen order**.
- **R7 Arrival candidates:** `arrival_events = [e for e in events if not is_empty_equipment(e)]`, where `is_empty_equipment(e)`
  is `e.event_type == "EQUIPMENT" and str(e.raw.get("emptyIndicatorCode", "")).upper() == "EMPTY"`. An empty container being
  discharged is not cargo arriving.
- **R8 Per-equipment arrivals:**
  1. `per_eq = dcsa.select_pod_arrival_per_equipment(arrival_events, pod_unlocode=pod)`.
  2. Normalise the keys: `str` keys are stripped and upper-cased; the `None` key is kept.
  3. Drop any arrival whose `eta.year not in PLAUSIBLE_YEARS`. This is defence in depth.

### 7.3 Aggregation, policy "latest" (CMA-authoritative)

```python
if not containers:
    raise NoEtaError(f"CMA found booking {ref} but listed no containers to read an ETA from. Can't determine ETA.")
vessel_level = per_eq.get(None)
container_arrivals = {c: (per_eq.get(c) or vessel_level) for c in containers}
missing = [c for c, a in container_arrivals.items() if a is None]
if missing:
    raise NoEtaError(f"No ETA published by CMA for booking {ref} "
                     f"(container{'s' if len(missing) > 1 else ''} {', '.join(missing)}). Can't determine ETA.")
for c, a in container_arrivals.items():
    log.info("  [CMA] Container %s: found '%s': %s", c, describe_arrival(a), a.eta.strftime("%m/%d/%Y %H:%M"))
latest_container, latest = max(container_arrivals.items(), key=lambda kv: kv[1].eta)   # first max wins on ties
if len(containers) > 1:
    log.info("  [CMA] Booking has %d containers — using the latest ETA.", len(containers))
return EtaResult(
    carrier=Carrier.CMA, booking=ref, eta=latest.eta,
    source=f"CMA {describe_arrival(latest)} at {latest.unlocode or pod or 'POD'}",
    is_actual=(latest.classifier == "ACT"),
    containers=tuple(containers),
    details={"pod": pod, "endpoint": endpoint, "pages": pages, "event_count": len(raw_events),
             "latest_container": latest_container, "basis": latest.basis,
             "per_container": {c: {"eta": a.eta.isoformat(), "classifier": a.classifier,
                                   "basis": a.basis, "unlocode": a.unlocode}
                               for c, a in container_arrivals.items()}})
```

`describe_arrival(a)` gives the label staff will see in logs and the run report. `b = (a.basis or "").upper()`:

| Condition | Label |
|---|---|
| `a.classifier == "ACT"` and `"DISC" in b` | `Arrived at POD (container discharged)` |
| `a.classifier == "ACT"` otherwise | `Arrived at POD (vessel arrival)` |
| `a.classifier == "EST"` | `ETA Berth at POD (estimated vessel arrival)` |
| `a.classifier == "PLN"` | `ETA Berth at POD (planned vessel arrival)` |
| anything else | `{a.classifier} arrival at POD` |

The aggregation needs only `basis`, `classifier`, `eta` and `unlocode` from `DcsaArrival`, so it does not depend on 09's exact
`basis` wording.

### 7.4 What CMA relies on from `carriers/dcsa.py` (owned by 09)

The CMA fixtures in §9 are acceptance tests for these behaviours. If 09's design differs, adjust `dcsa.py` or this module until
`tests/test_carrier_cma.py` passes. Do not change the expected values.

- **E1.** `parse_events(list_of_dicts)` accepts CMA's array as-is.
  - Unknown keys are ignored.
  - It returns one `DcsaEvent` per TRANSPORT/EQUIPMENT item; SHIPMENT items may be dropped.
  - `raw` is the original dict. CMA reads `raw["carrierSpecificData"]`, `raw["emptyIndicatorCode"]` and `raw["documentReferences"]`.
- **E2.** `event_datetime` is `eventDateTime` parsed with its offset, then made naive **keeping the local wall time**.
  Example: `"2026-11-10T14:00:00-08:00"` → `datetime(2026, 11, 10, 14, 0)`.
- **E3.** Field mapping:
  - `unlocode` = `transportCall.UNLocationCode`, else `eventLocation.UNLocationCode`, upper-cased.
  - `equipment_reference` = `equipmentReference` (or None).
  - `transport_call_sequence` = `transportCall.transportCallSequenceNumber`.
  - `classifier` = `eventClassifierCode`.
  - `type_code` = `transportEventTypeCode` / `equipmentEventTypeCode`.
- **E4.** `select_pod_arrival_per_equipment(events, pod_unlocode=P)`:
  - With `P` given, it only considers events at `P`.
  - For each container with an EQUIPMENT `DISC` `ACT` at `P`, it returns an arrival with `classifier == "ACT"` and a
    `basis` containing `"DISC"`.
  - Under key `None`, it returns the vessel-level arrival from TRANSPORT `ARRI` events at `P`, with precedence ACT > EST > PLN.
  - If it also puts vessel-level arrivals under container keys, the §7.3 aggregation gives the same results.
- **E5.** Among competing candidates of the same kind, the latest `eventCreatedDateTime` wins.
  - "Same kind" means same type, code, classifier, location and equipment.
  - `events_in_transit`: an EST created 2026-11-02 says 11-10 14:00 and an older EST created 2026-10-19 said 11-12. Expected: 11-10 14:00.
- **E6.** With `pod_unlocode=None`, 09's own POD heuristic applies. CMA passes None only when no event is tagged `shipmentLocationType == "POD"`.
- **E7.** `booking_arrival(events, policy="latest", pod_unlocode=P).eta` equals the §7.3 result for every fixture without a
  missing container. This is a cross-check test only; §7.3 is what CMA uses.

### 7.5 Deviations from the original

- **CMA-D1 — POD only, never FPD.**
  - **Original:** it accepted any headline matching `\beta\b|arriv`, including "ETA at FPD" (an inland final place of delivery),
    whenever CMA's website showed it.
  - **New:** always uses the POD vessel arrival or discharge, which is consistent with the MSC and Maersk specs.
  - **Why:** inland planned dates are *private* events (OAuth plus booking party), so public access cannot reproduce FPD anyway.
- **CMA-D2 — No CAPTCHA, no cookie file, no interactive waits.** DataDome was the only reason for them.
- **CMA-D3 — Identity check needs real references.**
  - **Original:** the "returned tracking for another booking" check used `SearchByReference`.
  - **New:** it can only run when CMA returns real BKG references (OAuth/business party). With an API key the references are
    hashed and the check is skipped.
  - The query itself is by booking reference, so a mismatch is very unlikely.
- **CMA-D4 — Duplicate bookings in one run share one API call** (§6.6), because calls are billable.
- **CMA-D5 — Empty-container discharges are ignored as arrival evidence** (R7). The website headline never showed them.
- **CMA-D6 — New not-found and no-dated-events messages.**
  - A booking that is unconfirmed or cancelled now gets its own "HTTP 404" message (§6.7).
  - Events whose dates are all sentinels give a `NoEtaError` (R1b).
  - The original treated both as "shipment not found".
- **Parity kept deliberately:**
  - A booking with transport events but **no containers** still raises `NoEtaError`, even though CMA may publish a planned
    vessel arrival for it.
  - Any container without a POD arrival makes the whole booking `NoEtaError`.

## 8. Fallbacks

Options, in order of preference, when the official API is unavailable:

1. **Aggregator provider.** Set `ETA_PROVIDER_CMA=aggregator` (09-dcsa-aggregators-grimaldi.md; resolved by the router, 01 §4).
   Use this if the user does not want a CMA contract after the 30-day trial, or until access is set up.
2. **Private OAuth tier** (§2 step 6), if public events turn out to lack ARRI EST/PLN for some bookings. Switch with
   `CMA_CLIENT_ID`/`CMA_CLIENT_SECRET`; no code change is needed. It also enables the identity check (CMA-D3).
3. **Shipment API v3** (`shipping.shipment.v3`, private OAuth scope `shipment:read:be`, contract):
   - Call: `GET https://apis.cma-cgm.net/shipping/shipment/v3/shipments/{bookingReference}`.
   - It exposes `portOfDischarge.arrivalDate` ("Expected Arrival Date").
   - It gives planned data only and is not implemented. List it as a possible future second source.
4. **Event push** (webhooks via `eventhub.subscription.v1`). Not suitable: it needs a public HTTPS endpoint, and this is a
   desktop batch tool.
5. **Manual.** With CMA set to `off` or unconfigured, CMA rows land in `Failed_To_Process.txt` with the carrier-lookup error,
   and staff check them by hand. This is the same outcome as when today's tool fails.

**Explicitly not offered: automating or scraping `www.cma-cgm.com`.**

- The site sits behind DataDome bot protection. It returned 403 to a plain request from this environment.
- The original needed a visible Chrome and occasionally a human solving a CAPTCHA.
- Defeating that protection (TLS/fingerprint impersonation, harvested `datadome` cookies, CAPTCHA solvers, stealth browsers)
  is brittle, against the site's terms, and out of scope.

## 9. Fixtures & tests

All fixtures are synthetic, valid DCSA 2.2 shapes as defined by CMA's OpenAPI.

- **Time zones:** every `eventDateTime` carries the port's offset. Expected values are the **local wall times** (E2).
- **Shared hash:** `769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df` is the hashed BKG reference that public
  access returns (FAQ example value).
- **Expected results** were computed with a reference implementation of §7 and must not be changed.

### 9.1 Event fixtures

#### `tests/fixtures/cma/events_in_transit.json`

```json
[
  {"eventCreatedDateTime":"2026-10-10T01:05:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-10T09:00:00+08:00","equipmentEventTypeCode":"GTOT","equipmentReference":"CMAU1234567","emptyIndicatorCode":"EMPTY","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Empty to shipper","transportationPhase":"Export","shipmentLocationType":"DEPOT"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}]},
  {"eventCreatedDateTime":"2026-10-10T02:40:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-10T10:30:00+08:00","equipmentEventTypeCode":"GTOT","equipmentReference":"TGHU7654321","emptyIndicatorCode":"EMPTY","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Empty to shipper","transportationPhase":"Export","shipmentLocationType":"DEPOT"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}]},
  {"eventCreatedDateTime":"2026-10-14T08:10:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-14T16:00:00+08:00","equipmentEventTypeCode":"GTIN","equipmentReference":"CMAU1234567","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Ready to be loaded","transportationPhase":"Export","shipmentLocationType":"POL"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}],"transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-14T09:20:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-14T17:10:00+08:00","equipmentEventTypeCode":"GTIN","equipmentReference":"TGHU7654321","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Ready to be loaded","transportationPhase":"Export","shipmentLocationType":"POL"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}],"transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-18T19:30:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-18T03:20:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU1234567","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}],"transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-18T20:10:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-18T04:05:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"TGHU7654321","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}],"transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-18T14:30:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-18T22:00:00+08:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Vessel Departure","transportationPhase":"Export","shipmentLocationType":"POL"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}]},
  {"eventCreatedDateTime":"2026-10-05T08:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"PLN","eventDateTime":"2026-11-08T06:00:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLGB-2","transportCallSequenceNumber":2,"UNLocationCode":"USLGB","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Planned Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}]},
  {"eventCreatedDateTime":"2026-10-19T06:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-12T06:00:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLGB-2","transportCallSequenceNumber":2,"UNLocationCode":"USLGB","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Estimated Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}]},
  {"eventCreatedDateTime":"2026-11-02T07:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-10T14:00:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLGB-2","transportCallSequenceNumber":2,"UNLocationCode":"USLGB","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Estimated Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"769c846f64ba4e49de9caa9f72fb8c0e701bd6f0edd32dbe7916e67808f978df"}]}
]
```

#### `tests/fixtures/cma/events_arrived.json`

```json
[
  {"eventCreatedDateTime":"2026-10-20T08:20:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T10:00:00+02:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU2000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"FRLEH"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"transportCall":{"transportCallID":"TC-FRLEH-1","transportCallSequenceNumber":1,"UNLocationCode":"FRLEH","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999992","vesselName":"CMA CGM EXAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-20T08:55:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T10:40:00+02:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU2000002","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"FRLEH"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"transportCall":{"transportCallID":"TC-FRLEH-1","transportCallSequenceNumber":1,"UNLocationCode":"FRLEH","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999992","vesselName":"CMA CGM EXAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-20T23:10:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-21T01:00:00+02:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-FRLEH-1","transportCallSequenceNumber":1,"UNLocationCode":"FRLEH","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999992","vesselName":"CMA CGM EXAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Vessel Departure","transportationPhase":"Export","shipmentLocationType":"POL"}},
  {"eventCreatedDateTime":"2026-10-28T09:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-02T20:00:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USNYC-2","transportCallSequenceNumber":2,"UNLocationCode":"USNYC","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999992","vesselName":"CMA CGM EXAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Estimated Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"}},
  {"eventCreatedDateTime":"2026-11-03T13:05:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-11-03T08:00:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USNYC-2","transportCallSequenceNumber":2,"UNLocationCode":"USNYC","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999992","vesselName":"CMA CGM EXAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"}},
  {"eventCreatedDateTime":"2026-11-03T19:25:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-11-03T14:20:00-05:00","equipmentEventTypeCode":"DISC","equipmentReference":"CMAU2000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"USNYC"},"carrierSpecificData":{"internalEventLabel":"Discharged","transportationPhase":"Import","shipmentLocationType":"POD"},"transportCall":{"transportCallID":"TC-USNYC-2","transportCallSequenceNumber":2,"UNLocationCode":"USNYC","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999992","vesselName":"CMA CGM EXAMPLE"}}},
  {"eventCreatedDateTime":"2026-11-04T14:50:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-11-04T09:45:00-05:00","equipmentEventTypeCode":"DISC","equipmentReference":"CMAU2000002","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"USNYC"},"carrierSpecificData":{"internalEventLabel":"Discharged","transportationPhase":"Import","shipmentLocationType":"POD"},"transportCall":{"transportCallID":"TC-USNYC-2","transportCallSequenceNumber":2,"UNLocationCode":"USNYC","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999992","vesselName":"CMA CGM EXAMPLE"}}},
  {"eventCreatedDateTime":"2026-11-05T16:02:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-11-05T11:00:00-05:00","equipmentEventTypeCode":"GTOT","equipmentReference":"CMAU2000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"USNYC"},"carrierSpecificData":{"internalEventLabel":"Container to consignee","transportationPhase":"Import","shipmentLocationType":"POD"},"transportCall":{"transportCallID":"TC-USNYC-2","transportCallSequenceNumber":2,"UNLocationCode":"USNYC","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999992","vesselName":"CMA CGM EXAMPLE"}}}
]
```

#### `tests/fixtures/cma/events_partial_discharge.json`

```json
[
  {"eventCreatedDateTime":"2026-10-22T07:10:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-22T09:00:00+02:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU3000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"BEANR"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"transportCall":{"transportCallID":"TC-BEANR-1","transportCallSequenceNumber":1,"UNLocationCode":"BEANR","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999993","vesselName":"CMA CGM DEMO"}}},
  {"eventCreatedDateTime":"2026-10-22T07:40:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-22T09:30:00+02:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU3000002","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"BEANR"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"transportCall":{"transportCallID":"TC-BEANR-1","transportCallSequenceNumber":1,"UNLocationCode":"BEANR","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999993","vesselName":"CMA CGM DEMO"}}},
  {"eventCreatedDateTime":"2026-11-04T12:35:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-11-04T07:30:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USSAV-2","transportCallSequenceNumber":2,"UNLocationCode":"USSAV","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999993","vesselName":"CMA CGM DEMO"}},"carrierSpecificData":{"internalEventLabel":"Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"}},
  {"eventCreatedDateTime":"2026-11-04T18:15:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-11-04T13:10:00-05:00","equipmentEventTypeCode":"DISC","equipmentReference":"CMAU3000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"USSAV"},"carrierSpecificData":{"internalEventLabel":"Discharged","transportationPhase":"Import","shipmentLocationType":"POD"},"transportCall":{"transportCallID":"TC-USSAV-2","transportCallSequenceNumber":2,"UNLocationCode":"USSAV","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999993","vesselName":"CMA CGM DEMO"}}}
]
```

#### `tests/fixtures/cma/events_transshipment.json`

```json
[
  {"eventCreatedDateTime":"2026-10-11T21:10:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-12T05:00:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU4000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999994","vesselName":"CMA CGM FEEDER"}}},
  {"eventCreatedDateTime":"2026-10-12T10:05:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-12T18:00:00+08:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999994","vesselName":"CMA CGM FEEDER"}},"carrierSpecificData":{"internalEventLabel":"Vessel Departure","transportationPhase":"Export","shipmentLocationType":"POL"}},
  {"eventCreatedDateTime":"2026-10-19T21:05:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T05:00:00+08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-SGSIN-2","transportCallSequenceNumber":2,"UNLocationCode":"SGSIN","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999994","vesselName":"CMA CGM FEEDER"}},"carrierSpecificData":{"internalEventLabel":"Vessel Arrival","transportationPhase":"Transshipment","shipmentLocationType":"PTS"}},
  {"eventCreatedDateTime":"2026-10-20T03:10:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-20T11:00:00+08:00","equipmentEventTypeCode":"DISC","equipmentReference":"CMAU4000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"SGSIN"},"carrierSpecificData":{"internalEventLabel":"Discharged in transhipment","transportationPhase":"Transshipment","shipmentLocationType":"PTS"},"transportCall":{"transportCallID":"TC-SGSIN-2","transportCallSequenceNumber":2,"UNLocationCode":"SGSIN","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999994","vesselName":"CMA CGM FEEDER"}}},
  {"eventCreatedDateTime":"2026-10-22T18:10:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-23T02:00:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU4000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"SGSIN"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Transshipment","shipmentLocationType":"PTS"},"transportCall":{"transportCallID":"TC-SGSIN-3","transportCallSequenceNumber":3,"UNLocationCode":"SGSIN","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999995","vesselName":"CMA CGM MAINLINER"}}},
  {"eventCreatedDateTime":"2026-10-23T01:05:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-23T09:00:00+08:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-SGSIN-3","transportCallSequenceNumber":3,"UNLocationCode":"SGSIN","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999995","vesselName":"CMA CGM MAINLINER"}},"carrierSpecificData":{"internalEventLabel":"Vessel Departure","transportationPhase":"Transshipment","shipmentLocationType":"PTS"}},
  {"eventCreatedDateTime":"2026-10-02T08:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"PLN","eventDateTime":"2026-11-14T07:00:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USNYC-4","transportCallSequenceNumber":4,"UNLocationCode":"USNYC","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999995","vesselName":"CMA CGM MAINLINER"}},"carrierSpecificData":{"internalEventLabel":"Planned Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"}},
  {"eventCreatedDateTime":"2026-10-23T08:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-16T07:00:00-05:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USNYC-4","transportCallSequenceNumber":4,"UNLocationCode":"USNYC","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999995","vesselName":"CMA CGM MAINLINER"}},"carrierSpecificData":{"internalEventLabel":"Estimated Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"}}
]
```

#### `tests/fixtures/cma/events_missing_container_eta.json`

```json
[
  {"eventCreatedDateTime":"2026-10-15T06:10:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-15T08:00:00+02:00","equipmentEventTypeCode":"LOAD","equipmentReference":"TGHU5000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"DEHAM"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"transportCall":{"transportCallID":"TC-DEHAM-1","transportCallSequenceNumber":1,"UNLocationCode":"DEHAM","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999996","vesselName":"CMA CGM TEST"}}},
  {"eventCreatedDateTime":"2026-10-15T06:40:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-15T08:30:00+02:00","equipmentEventTypeCode":"LOAD","equipmentReference":"TGHU5000002","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"DEHAM"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"transportCall":{"transportCallID":"TC-DEHAM-1","transportCallSequenceNumber":1,"UNLocationCode":"DEHAM","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999996","vesselName":"CMA CGM TEST"}}},
  {"eventCreatedDateTime":"2026-10-15T18:05:00Z","eventType":"TRANSPORT","eventClassifierCode":"ACT","eventDateTime":"2026-10-15T20:00:00+02:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-DEHAM-1","transportCallSequenceNumber":1,"UNLocationCode":"DEHAM","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999996","vesselName":"CMA CGM TEST"}},"carrierSpecificData":{"internalEventLabel":"Vessel Departure","transportationPhase":"Export","shipmentLocationType":"POL"}},
  {"eventCreatedDateTime":"2026-11-02T16:05:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-11-02T10:00:00-06:00","equipmentEventTypeCode":"DISC","equipmentReference":"TGHU5000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"USHOU"},"carrierSpecificData":{"internalEventLabel":"Discharged","transportationPhase":"Import","shipmentLocationType":"POD"},"transportCall":{"transportCallID":"TC-USHOU-2","transportCallSequenceNumber":2,"UNLocationCode":"USHOU","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999996","vesselName":"CMA CGM TEST"}}}
]
```

#### `tests/fixtures/cma/events_no_containers.json`

```json
[
  {"eventCreatedDateTime":"2026-11-01T08:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"PLN","eventDateTime":"2026-11-20T18:00:00+08:00","transportEventTypeCode":"DEPA","transportCall":{"transportCallID":"TC-CNYTN-1","transportCallSequenceNumber":1,"UNLocationCode":"CNYTN","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Planned Vessel Departure","transportationPhase":"Export","shipmentLocationType":"POL"}},
  {"eventCreatedDateTime":"2026-11-01T08:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"PLN","eventDateTime":"2026-12-04T06:00:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLAX-2","transportCallSequenceNumber":2,"UNLocationCode":"USLAX","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Planned Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"}}
]
```

#### `tests/fixtures/cma/events_empty.json`

```json
[]
```

#### `tests/fixtures/cma/events_sentinel_and_empty_disc.json`

```json
[
  {"eventCreatedDateTime":"2026-10-25T08:05:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-25T09:00:00+01:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU7000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"ITGOA"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"transportCall":{"transportCallID":"TC-ITGOA-1","transportCallSequenceNumber":1,"UNLocationCode":"ITGOA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-26T08:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"0001-01-01T00:00:00Z","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USHOU-2","transportCallSequenceNumber":2,"UNLocationCode":"USHOU","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Estimated Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"}},
  {"eventCreatedDateTime":"2026-10-02T08:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"PLN","eventDateTime":"2026-11-20T08:00:00-06:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USHOU-2","transportCallSequenceNumber":2,"UNLocationCode":"USHOU","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Planned Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"}},
  {"eventCreatedDateTime":"2026-11-01T18:05:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-11-01T12:00:00-06:00","equipmentEventTypeCode":"DISC","equipmentReference":"CMAU7000001","emptyIndicatorCode":"EMPTY","eventLocation":{"UNLocationCode":"USHOU"},"carrierSpecificData":{"internalEventLabel":"Discharged Empty","transportationPhase":"Import","shipmentLocationType":"POD"},"transportCall":{"transportCallID":"TC-USHOU-2","transportCallSequenceNumber":2,"UNLocationCode":"USHOU","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}}}
]
```

#### `tests/fixtures/cma/events_other_booking.json`

```json
[
  {"eventCreatedDateTime":"2026-10-18T19:30:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-18T03:20:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU9000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM2222222"}],"transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-19T06:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-12T06:00:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLGB-2","transportCallSequenceNumber":2,"UNLocationCode":"USLGB","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Estimated Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM2222222"}]}
]
```

#### `tests/fixtures/cma/events_split_child.json`

```json
[
  {"eventCreatedDateTime":"2026-10-18T19:30:00Z","eventType":"EQUIPMENT","eventClassifierCode":"ACT","eventDateTime":"2026-10-18T03:20:00+08:00","equipmentEventTypeCode":"LOAD","equipmentReference":"CMAU9000001","emptyIndicatorCode":"LADEN","eventLocation":{"UNLocationCode":"CNSHA"},"carrierSpecificData":{"internalEventLabel":"Loaded on board","transportationPhase":"Export","shipmentLocationType":"POL","originBookingReference":"NAM3333333"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM3333334"}],"transportCall":{"transportCallID":"TC-CNSHA-1","transportCallSequenceNumber":1,"UNLocationCode":"CNSHA","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}}},
  {"eventCreatedDateTime":"2026-10-19T06:00:00Z","eventType":"TRANSPORT","eventClassifierCode":"EST","eventDateTime":"2026-11-12T06:00:00-08:00","transportEventTypeCode":"ARRI","transportCall":{"transportCallID":"TC-USLGB-2","transportCallSequenceNumber":2,"UNLocationCode":"USLGB","facilityTypeCode":"POTE","modeOfTransport":"VESSEL","vessel":{"vesselIMONumber":"9999991","vesselName":"CMA CGM SAMPLE"}},"carrierSpecificData":{"internalEventLabel":"Estimated Vessel Arrival","transportationPhase":"Import","shipmentLocationType":"POD","originBookingReference":"NAM3333333"},"documentReferences":[{"documentReferenceType":"BKG","documentReferenceValue":"NAM3333334"}]}
]
```

#### `tests/fixtures/cma/events_page1.json` and `events_page2.json`

- `events_page1.json` = the **first 6** items of `events_in_transit.json`.
- `events_page2.json` = the **remaining 4** items.
- Tests may build them with `json.load` plus slicing, or the implementer can write them out as files.

### 9.2 Other fixtures

#### `tests/fixtures/cma/oauth_token.json`

```json
{"access_token": "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.c2lnbmF0dXJl", "token_type": "Bearer", "expires_in": 299}
```

#### `tests/fixtures/cma/error_401.json` (observed shape)

```json
{"message": "Unauthorized", "request_id": "30e5da4bfc32f503d25b175d57848b45"}
```

#### `tests/fixtures/cma/error_404.json` (DCSA error shape, synthetic)

```json
{"httpMethod": "GET", "requestUri": "https://apis.cma-cgm.net/operation/trackandtrace/v1/events/NAM0000000",
 "statusCode": 404, "statusCodeText": "Not Found", "errorDateTime": "2026-11-05T09:00:00+01:00",
 "errors": [{"reason": "notFound", "message": "No commercial cycle found for the provided reference"}]}
```

#### `tests/fixtures/cma/error_400.json` (DCSA error shape, synthetic)

```json
{"httpMethod": "GET", "requestUri": "https://apis.cma-cgm.net/operation/trackandtrace/v1/events",
 "statusCode": 400, "statusCodeText": "Bad Request", "errorDateTime": "2026-11-05T09:00:00+01:00",
 "errors": [{"reason": "invalidQuery", "message": "The request did not contain one of the three required query parameters."}]}
```

#### `tests/fixtures/cma/error_429.json` (UNVERIFIED body; served with header `Retry-After: 2`)

```json
{"message": "API rate limit exceeded", "request_id": "6f1c0d2e9b8a7c6d5e4f3a2b1c0d9e8f"}
```

### 9.3 Expected results of `evaluate_events`

These are pure, with no HTTP involved.

| Fixture | `ref` | Expected |
|---|---|---|
| `events_in_transit` | `NAM1234567` | `eta == datetime(2026,11,10,14,0)`; `is_actual False`; `containers == ("CMAU1234567","TGHU7654321")`; `details["pod"] == "USLGB"`; `source == "CMA ETA Berth at POD (estimated vessel arrival) at USLGB"` (newest-created EST supersedes the older 11-12 EST; PLN 11-08 ignored; identity check skipped — hashed refs) |
| `events_arrived` | `NAM2345678` | `eta == datetime(2026,11,4,9,45)` (CMAU2000002's discharge, latest of 11-03 14:20 / 11-04 09:45); `is_actual True`; `details["latest_container"] == "CMAU2000002"`; `source` starts with `"CMA Arrived at POD (container discharged)"`; `details["pod"] == "USNYC"` |
| `events_partial_discharge` | `NAM3456789` | `eta == datetime(2026,11,4,13,10)`; `is_actual True`; `details["per_container"]["CMAU3000002"]["eta"] == "2026-11-04T07:30:00"` (falls back to vessel `ARRI ACT`); `details["latest_container"] == "CMAU3000001"` |
| `events_transshipment` | `NAM4567890` | `eta == datetime(2026,11,16,7,0)` (`ARRI EST` at USNYC beats `PLN` 11-14; the `DISC ACT` at SGSIN, tagged `PTS`, is ignored); `is_actual False`; `details["pod"] == "USNYC"` |
| `events_missing_container_eta` | `NAM5678901` | `NoEtaError`, message exactly `No ETA published by CMA for booking NAM5678901 (container TGHU5000002). Can't determine ETA.` |
| `events_no_containers` | `NAM6789012` | `NoEtaError`, message exactly `CMA found booking NAM6789012 but listed no containers to read an ETA from. Can't determine ETA.` |
| `events_empty` | `NAM0000001` | `CarrierLookupError`; message contains `'shipment not found'` and `15 days` |
| `events_sentinel_and_empty_disc` | `NAM7890123` | `eta == datetime(2026,11,20,8,0)` (`PLN`; the `0001-01-01` EST is dropped by R2; the `EMPTY` `DISC` is ignored by R7); `is_actual False`; `source == "CMA ETA Berth at POD (planned vessel arrival) at USHOU"` |
| `events_other_booking` | `NAM1111111` | `CarrierLookupError`, message exactly `CMA returned tracking for NAM2222222 instead of the requested booking NAM1111111.` |
| `events_split_child` | `NAM3333333` | Accepted (the child `NAM3333334` has `originBookingReference == "NAM3333333"`); `eta == datetime(2026,11,12,6,0)`; `is_actual False` |
| `events_page1 + events_page2` concatenated | `NAM1234567` | Same as `events_in_transit` |

### 9.4 `tests/test_carrier_cma.py`

**Pure helpers:**

1. `test_evaluate_*`: one test per row of §9.3, with exact messages where given.
2. `test_resolve_pod_unlocode_prefers_carrier_specific_pod`:
   - Returns `"USNYC"` for `events_transshipment`.
   - Returns `None` when `carrierSpecificData` is stripped from every event.
3. `test_container_references_first_seen_order_and_dedupe`.
4. `test_prefilter_drops_sentinels_without_parsing`: items with `eventDateTime` `"0001-01-01T00:00:00Z"`,
   `"9999-12-31T23:59:59.9999999"`, `""`, missing, a non-dict, and `"2026-11-20T08:00:00-06:00"` → only the last survives.
5. `test_check_booking_identity`:
   - Hashed only → no error.
   - Real ref equal → ok.
   - Real ref different → error.
   - Different but related via `splitToBookingReferences` → ok.
6. `test_describe_arrival_labels`: all 5 rows of the §7.3 table.
7. `test_extract_cursor`: the 3 examples in §6.3.
8. `test_equivalence_with_dcsa_booking_arrival` (E7): for in_transit, arrived, partial_discharge, transshipment, sentinel and
   split_child.

**HTTP (respx), with `sleep` injected as an async recorder:**

9. `test_reference_mode_request_shape`:
   - `GET https://apis.cma-cgm.net/operation/trackandtrace/v1/events/NAM1234567?limit=500`.
   - Header `KeyId` equals the configured key; no `Authorization` header.
10. `test_dcsa_query_mode_request_shape`: `GET …/events?carrierBookingReference=NAM1234567&limit=500`.
11. `test_behalf_of_param_added_when_configured`.
12. `test_pagination_follows_next_page`:
    - Page 1 answers with header `Next-Page: fE9mZnNldHw9NiZsaW1pdD02`.
    - The second call's params are exactly `{"cursor": "fE9mZnNldHw9NiZsaW1pdD02", "limit": "500"}`.
    - Two calls total; the result equals in_transit.
13. `test_page_cap_warns_and_uses_collected`: every page returns `Next-Page`, `CMA_MAX_PAGES=2` → 2 calls and a WARNING.
14. `test_404_maps_to_lookup_error_and_is_not_retried`: exactly 1 call.
15. `test_empty_array_maps_to_shipment_not_found`.
16. `test_401_apikey_hint` and `test_403_hint`: exact texts from §6.7.
17. `test_400_includes_dcsa_reason_and_message`.
18. `test_429_honours_retry_after`:
    - The first response is 429 with `Retry-After: 2`, then 200.
    - The recorded sleeps include ≥ 2.5 (2 plus the 0.5 margin).
    - Two calls.
19. `test_429_over_cap_fails_fast`: `Retry-After: 999` with the cap at 120 → `CarrierLookupError`, 1 call, no sleep ≥ 999.
20. `test_429_blocks_concurrent_calls`:
    - Two concurrent different bookings; the first gets a 429 with `Retry-After: 2`.
    - The second's next attempt happens only after the gate. Assert via the recorded sleeps or call timestamps.
21. `test_5xx_retried_then_success`: 503, 502, then 200 → 3 calls. Also 500×3 → `CarrierLookupError`.
22. `test_network_error_retried`: `httpx.ConnectError` once, then 200.
23. `test_non_json_body_maps_to_could_not_read`.

**Auth:**

24. `test_oauth_token_fetched_once_for_concurrent_lookups`:
    - Three concurrent lookups → 1 token POST, with form fields `grant_type=client_credentials`, `client_id`, `client_secret`
      and `scope="tandtcommercial:read:be tandtpublic:read:be"`.
    - `Authorization: Bearer …` is sent.
    - The TokenStore `"cma"` entry has `expires_at ≈ now + 299`.
25. `test_oauth_401_refreshes_once`: a 401 then a 200 → 2 token POSTs and 2 GETs. A 401 twice → `HINT_401`.
26. `test_cached_fresh_token_reused_and_skew_60s`: `expires_at = now + 61` is reused; `now + 59` triggers a refresh.

**Client lifecycle:**

27. `test_single_flight_memo`: two concurrent `get_eta("NAM1234567")` and one later call → exactly 1 GET. A failure is memoised too.
28. `test_boundary_rule`: `parse_events` monkeypatched to raise `TypeError` → `CarrierLookupError` (never `TypeError`).
29. `test_build_client_none_when_unconfigured` and `test_settings_resolution`:
    - auto→oauth when id and secret are set.
    - auto→apikey with only the key.
    - Forced apikey without a key → `None` plus `missing["cma"] == ("CMA_API_KEY",)`.
    - Invalid `CMA_LOOKUP_MODE` → `ConfigError`.
    - `CMA_PAGE_LIMIT=501` → `ConfigError`.
30. `test_probe_calls_both_endpoints_and_redacts`:
    - 2 GETs.
    - The output contains no key or token value.
    - `analysis["reference"]["pod"] == "USLGB"`.
    - `endpoints_agree` is True when both are mocked with the same fixture.

## 10. Probe / verification procedure

Command: `eta probe cma --booking NAM1234567`.

- It makes **2 or more billable calls**: the configured endpoint plus the other one, and pages if any.
- It saves the redacted output to `<ETA_STATE_DIR>/probe/cma_<booking>_<timestamp>.json` (11-cli.md).

Steps for the user:

1. Register and copy the API key (§2). Add `CMA_API_KEY=…` to `.env`.
2. Pick **three known CMA bookings**: one in transit, one already discharged at the POD, and one with a transshipment if available.
   Run the probe for each.
3. For each, check in the probe output:
   - **`eta`** matches what CMA shows for the booking in a normal browser session, opened by a person:
     - the "ETA Berth at POD" date and time while in transit, or
     - the "Arrived at POD" discharge time once arrived.
     For multi-container bookings it should match the **latest** container.
   - **`analysis.shipment_location_types`** contains `"POD"`, and `analysis.pod` is the correct port. If `"POD"` is missing, POD
     detection relies on 09's heuristic (E6). Confirm the port, and report it so the spec can be updated.
   - **`analysis.counts`** includes `TRANSPORT ARRI EST` and/or `PLN`. If not, public events may be insufficient → consider the
     OAuth tier (§8 option 2).
   - **`endpoints_agree`** is True. If not, keep `CMA_LOOKUP_MODE=reference` (the default) and note the difference.
   - **`requests[*].next_page`.** If any page had `Next-Page`, confirm the cursor handling produced page 2 (UNVERIFIED cursor format).
   - **`requests[*].request_id`** is present. Quote it when contacting CMA support
     (*Contact Us → Report Issue*, or b2becustomersupport@cma-cgm.com).
4. Probe one **unknown or cancelled** booking and confirm you get the 404 or `[]` message (§6.7). Record which of the two CMA
   returns (UNVERIFIED).
5. Run `eta eta <8–10 recent NAM bookings>` and compare against the old tool or the website for the same day.
6. If a 429 ever appears, set `CMA_MAX_CALLS_PER_MINUTE` (e.g. 20) and/or ask CMA to raise the quota.

**UNVERIFIED items the probe settles on first run:**

- (a) Public access returns `ARRI EST`/`PLN` and `DISC ACT` through `/events/{ref}`.
- (b) `carrierSpecificData` is present with an API key.
- (c) The `Next-Page` value is a raw cursor rather than a URL.
- (d) Whether an unknown booking returns a 404 or `[]`.
- (e) `eventDateTime` offsets are port-local.
- (f) The 429 headers and body.
- (g) Latency.

## 11. Sources & verification status

| Claim | Source | Status |
|---|---|---|
| T&T API = "Logistic Tracking service API - DCSA OpenAPI specification for Track & Trace v2.2.0", id `operation.trackandtrace.v1`, family VISIBILITY, security "mixed", public/private tier description, polling via `GET /events` | <https://api-portal.cma-cgm.com/api/v1/apisummaries> (portal catalogue JSON behind <https://api-portal.cma-cgm.com/>) | VERIFIED-DOCS |
| Server `https://apis.cma-cgm.net/operation/trackandtrace/v1`; paths `/events`, `/events/{trackingReference}`; params (`carrierBookingReference`, `limit`, `cursor`, `behalfOf`, …); security schemes `ApiKeyAuth` (header `keyId`) and `oAuth2` clientCredentials at `https://auth.cma-cgm.com/as/token.oauth2` with scopes `tandtcommercial:read:be`, `tandtpublic:read:be`; pagination headers; event/transportCall/`carrierSpecificData` schemas incl. `shipmentLocationType` POD/PTS/…; DCSA error schema; `x-cmaVersion` 1.2.9 | <https://api-portal.cma-cgm.com/api/v1/apis/operation.trackandtrace.v1/swagger> | VERIFIED-DOCS |
| Two operations `getMoveOnCommercialCycle` / `searchMoveOnCommercialCycle`, both accept oauth2 or apikey | <https://api-portal.cma-cgm.com/api/v1/apis/operation.trackandtrace.v1/endpoints> | VERIFIED-DOCS |
| Base URL statement; API key header `KeyId`; OAuth curl example; token response with `expires_in: 299`; *"The access token lasts for 5 minutes"*; rate limiting with 429 + `retry-after` (*"If not respected, the rate limit will not be reinitialised automatically"*); caching advice; DCSA cursor pagination; no CORS; 5-step starting guide (30-day free trial, subscription request answered within 48 working hours, same API key after contract, private client ID by mail) | Portal Guides page <https://api-portal.cma-cgm.com/guides> (content served in `/assets/GuidesPage-*.js`) | VERIFIED-DOCS |
| FAQ: limit default 100 / max 500 with cursor paging; `/events/{trackingReference}` is the simplest way and covers split/merged/part-load children; endpoint results can differ; 404 for unconfirmed/cancelled bookings; hashed booking reference for non-parties; shipment events only for parties; free-trial API list incl. Logistics Tracking; billing per status code; PVA/PVD and EVA/EVD triggers; Main User / client secret retrieval; LOA/client mandate | <https://api-portal.cma-cgm.com/api/v1/cms/getAllCms> (FAQ content of the portal) | VERIFIED-DOCS |
| Public event catalogue: TRANSPORT ARRI ACT/EST/PLN "Vessel Arrival" and EQUIPMENT DISC ACT LADEN "Discharged" are **Public** | <https://api-portal.cma-cgm.com/api/v1/events/available> | VERIFIED-DOCS |
| Shipment API v3 (`/shipping/shipment/v3/shipments/{bookingReference}`, scope `shipment:read:be`, `portOfDischarge.arrivalDate`) | <https://api-portal.cma-cgm.com/api/v1/apis/shipping.shipment.v3/swagger> | VERIFIED-DOCS |
| Gateway returns `401 {"message":"Unauthorized","request_id":…}` with `x-request-id`, `x-correlationid`, `x-kong-request-id` when no key is sent | unauthenticated `GET https://apis.cma-cgm.net/operation/trackandtrace/v1/events/NAM0000000` (2026-11-05) | OBSERVED |
| `www.cma-cgm.com` refuses plain HTTP clients (403, DataDome) | plain GET from this environment (2026-11-05); original `CMA/checkETA.py` comments | OBSERVED |
| Same backend serves ANL, APL, CNC, Containerships | <https://providers.apievangelist.com/providers/cma-cgm/>, <https://blogs.tradlinx.com/how-cma-cgm-container-tracking-actually-works-what-the-portal-shows-what-it-doesnt-and-what-the-events-mean/> (third-party) | INFERRED |
| Tracking data typically appears ~8 working hours after sailing; backend syncs 2–4×/day | <https://blogs.tradlinx.com/how-cma-cgm-container-tracking-actually-works-what-the-portal-shows-what-it-doesnt-and-what-the-events-mean/> (third-party) | INFERRED |
| DCSA information model reference used by CMA | <https://dcsa.org/wp-content/uploads/2021/10/202108_DCSA_P1_Information-Model-v3.3_TNT22_Final.pdf> (linked from CMA's API description) | VERIFIED-DOCS (link) |
| Free-trial quota numbers, contract rate limits, prices | not published | UNVERIFIED |
| `Next-Page` value format (raw cursor vs URL); whether cursor-only follow-up requests are accepted | DCSA convention; CMA guide example cursor only | UNVERIFIED |
| Unknown booking → 404 vs `200 []` | FAQ covers unconfirmed/cancelled → 404 only | UNVERIFIED |
| `carrierSpecificData` populated for API-key (public) callers | schema only | UNVERIFIED |
| 429 response body / exact header casing; OAuth error body wording | not documented | UNVERIFIED |
| Per-call latency (~1 s) | not measured with credentials | UNVERIFIED |
