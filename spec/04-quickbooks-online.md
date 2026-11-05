# 04 — QuickBooks Online: Accounting API, OAuth, invoice client

Replaces **all** QuickBooks web-UI automation in the original
(`QBTings/invoiceProcessing.py`, `QBTings/playWrightQB.py`, the persistent
Chromium profile + password autofill in `mainIG.py`) and the OAuth/lookup code
in `QBTings/apiREST.py`.

Companion file: **`04b-invoice-email-delivery.md`** — how the invoice e-mail is
delivered with a per-invoice subject line (`qbo/email.py`). Read both.

Contracts used from `03-foundations.md`: `http.request()`, `http.json_body()`,
`auth.request_with_auth()`, `TokenProvider`, `TokenStore`/`is_fresh()`,
`Clock`, errors (`InvoiceNotFoundError`, `InvoiceUpdateError`,
`InvoiceSendError`, `AuthError`, `ConfigError`, `ServiceError`), logger `eta.qbo`.

---

## 1. Summary

| Original (browser) | New (API) | Typical time |
|---|---|---|
| Launch persistent Chromium, type password, wait for QBO shell (≤ 3 min first run) | One OAuth refresh call (`QboAuth`) | ~0.3 s once per run |
| `get_invoice_by_number()` one REST query per booking | One `SELECT … WHERE DocNumber IN (…)` per 25 bookings | ~0.5 s per 25 rows |
| Open `/app/invoice?txnId=…`, read `#balanceAmount`, look for "Partially paid" banner | `Balance`/`TotalAmt` fields already in the query result | 0 extra calls |
| Double-click "Date Field", type `MM/DD/YYYY`, Tab, Save, confirm modal, `slow_mo=200` | One sparse update (`POST /invoice`) of the ETA custom field (+ DocNumber) | ~0.4 s |
| Edit "Invoice number" (+" A"/"A") | Same sparse update (`DocNumber`) | (same call) |
| "Review and send" → type Subject → "Send invoice" | `qbo/email.py` strategy (04b) | ~1–2 s |

Per in-window invoice the browser flow costs ~20–40 s (page loads, fixed waits
of 400–1 500 ms, 200 ms slow-motion on every action). The API flow costs ~2–3
calls and finishes in ~1–3 s, and non-e-mail updates run concurrently.

Behaviour that must be preserved exactly (the pipeline decides *what* to do;
this client must make each step reproducible via API):

1. **Paid** (UI balance shows `$0.00`) → set ETA field, save. No e-mail, no
   DocNumber change. → `update_invoice(inv, eta=…)`.
2. **Not paid** → the pipeline checks partial payment + TR/SWB (needs
   `is_partially_paid`). Then: if ETA date ≤ today, DocNumber gets `" A"`
   appended when it ends in a digit, else `"A"`; ETA field set; invoice saved;
   e-mail sent with the subject from `texts.subject_for()`. →
   `update_invoice(inv, eta=…, doc_number=new_or_None)` then
   `send_invoice(updated, subject=…)`.
3. **Future (outside window)** → set ETA field, save. →
   `update_invoice(inv, eta=…)`.

## 2. Access & onboarding

The company already has a production Intuit app (the original used
`QB_CLIENT_ID`/`QB_CLIENT_SECRET`/`QB_REFRESH_TOKEN` against
`quickbooks.api.intuit.com`). Steps for the user:

1. **Developer portal** (developer.intuit.com → *My Hub* → the existing app):
   - Scope: `com.intuit.quickbooks.accounting` (already used).
   - **Redirect URIs**: add `https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl`
     under *Production → Keys & credentials → Redirect URIs* (and Development) — `eta qbo-auth`
     uses it by default (§5.4). Production redirect URIs must be HTTPS and may not be
     `localhost` or an IP address; `http://localhost` is only accepted for development/sandbox keys.
   - **Reconnect URL**: Intuit now requires apps to supply one (it is shown to customers
     when a connection nears expiry). For an in-house app, any HTTPS page you control
     (e.g. the company website) is acceptable; reconnecting is done with `eta qbo-auth`.
   - App Partner Program: the free *Builder* tier is sufficient. Data-out (read) calls are
     metered as "CorePlus" (500 000/month free on Builder, then **blocked**, not billed);
     data-in (create/update) calls are "Core" and unmetered. This tool uses ~1–3 reads per
     booking, far below the cap. The *Custom Fields GraphQL API* is a premium API
     (Silver+); **this design does not use it** — it reads/writes custom-field *values*
     through the ordinary v3 REST API.
2. **QuickBooks company settings** (Settings → Custom fields): the "ETA" date custom field
   must be **active** and **enabled for Invoices** (Intuit support has confirmed that fields not
   enabled for the sales form do not appear in the API). Enhanced (non-string) custom fields
   require QuickBooks Online **Advanced** — the company already has a date-type field, so it has it.
3. **Sandbox company** (developer portal → *Sandboxes*): used by the probes in §10. Note that
   enhanced custom fields may not behave identically in sandbox; production probes in §10 are
   designed to be safe.
4. Run `eta probe qbo` and `eta qbo-fields --invoice <a DocNumber whose ETA is set in QBO>`
   to discover the ETA field's `DefinitionId` and `Type` (§6.4, §10).

## 3. Configuration

```python
@dataclass(frozen=True)
class QboSettings:
    client_id: str
    client_secret: str
    realm_id: str
    refresh_token: str = ""                 # seed; "" allowed if the TokenStore already has one
    sandbox: bool = False
    minor_version: str = "75"
    eta_field_name: str = "ETA"
    eta_definition_id: str | None = None    # hint only — see §6.4 resolution rules
    eta_field_type: str = "DateType"        # used only if no invoice reveals the field's Type
    eta_string_format: str = "%m/%d/%Y"     # used only if the field is StringType
    verify_updates: bool = True
    include_on_writes: bool = True
    query_chunk_size: int = 25
    max_concurrency: int = 4
    max_calls_per_minute: int = 450
    max_calls_per_second: int = 8
    docnumber_max_len: int = 21
    redirect_uri: str = "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"
    oauth_scopes: str = "com.intuit.quickbooks.accounting"
    env_file: Path | None = None            # the .env actually loaded (for refresh-token write-back)
    # e-mail related fields are listed in 04b §3 (email_strategy, test_send_to, …)

    @property
    def api_base(self) -> str:
        return ("https://sandbox-quickbooks.api.intuit.com" if self.sandbox
                else "https://quickbooks.api.intuit.com")
```

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `QB_CLIENT_ID` | yes | — | Intuit app client ID (unchanged from original). |
| `QB_CLIENT_SECRET` | yes | — | Intuit app client secret (unchanged). |
| `QB_REALM_ID` | yes | — | Company (realm) ID (unchanged). |
| `QB_REFRESH_TOKEN` | first run | — | Seed refresh token. After each rotation the new value is stored in the TokenStore **and** written back here (like the original). Paste a new value here after reconnecting; §5.2 detects it. |
| `QB_SANDBOX` | no | `false` | Use the sandbox API host. |
| `QB_MINOR_VERSION` | no | `75` | Sent as `minorversion` on every call. 75 is the minimum Intuit still honours (1–74 deprecated 1 Aug 2025; lower values are silently treated as 75). The original's 73 is obsolete. |
| `QB_ETA_FIELD_NAME` | no | `ETA` | Custom-field name to match (case-insensitive). |
| `QB_ETA_DEFINITION_ID` | no | empty | Hint. The value in the old `.env.example` (`20000000000000150677`, copied from the web app's GraphQL `CustomFieldDefinition:` id) is **probably not** the REST `DefinitionId` (REST uses `legacyIDV2`, e.g. `1000000001`). Name matching wins when this id is not found on an invoice; a WARNING tells the user the correct value. |
| `QB_ETA_FIELD_TYPE` | no | `DateType` | Fallback `Type` when no invoice in the run shows the field. |
| `QB_ETA_STRING_FORMAT` | no | `%m/%d/%Y` | strftime format if the field turns out to be `StringType` (matches what the UI flow typed). |
| `QB_VERIFY_UPDATES` | no | `true` | Re-check that DocNumber/ETA really persisted (§6.6). Keep on. |
| `QB_INCLUDE_ON_WRITES` | no | `true` | Add `include=enhancedAllCustomFields` to update POSTs too. |
| `QB_QUERY_CHUNK_SIZE` | no | `25` | DocNumbers per `IN (…)` query. Long IN lists have been reported to fail. |
| `QB_MAX_CONCURRENCY` | no | `4` | Semaphore size (03 §10). |
| `QB_MAX_CALLS_PER_MINUTE` | no | `450` | Below Intuit's 500/min/realm throttle. |
| `QB_MAX_CALLS_PER_SECOND` | no | `8` | Below Intuit's 10/s per realm+app throttle. |
| `QB_DOCNUMBER_MAX_LEN` | no | `21` | Pre-check for a suffixed DocNumber (§7); QBO's documented limit is 21. |
| `QB_REDIRECT_URI` | no | Playground URL | Redirect URI for `eta qbo-auth` (must be registered on the app). |
| `QB_OAUTH_SCOPES` | no | `com.intuit.quickbooks.accounting` | Space-separated scopes for `eta qbo-auth`. |

`Settings.qbo` is `None` when any of `QB_CLIENT_ID`, `QB_CLIENT_SECRET`, `QB_REALM_ID` is missing
(`Settings.missing["qbo"]` lists them; 03 §12). `QB_REFRESH_TOKEN` is not checked at load time —
`QboAuth` raises `AuthError` with instructions if neither `.env` nor the TokenStore has one.

**Obsolete:** `QB` (web-login password) and `QB_HEADLESS` are no longer used. If present,
`load_settings()` logs one INFO line: "QB and QB_HEADLESS are no longer used (no browser) — you can delete them from .env".
The browser profile under `%LOCALAPPDATA%/QB-APITesting/qbo_browser_session` can be deleted.

## 4. Endpoints

All paths are relative to `settings.api_base`. `{realm}` = `QB_REALM_ID`.
Every request carries `minorversion={QB_MINOR_VERSION}` and
`Authorization: Bearer <access token>`, `Accept: application/json`
(PDF: `Accept: application/pdf`). JSON bodies use `Content-Type: application/json`.

| # | Purpose | Method + path | Extra params | Idempotent for retries |
|---|---|---|---|---|
| E1 | Token refresh / code exchange | `POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer` | form body (§5) | yes (see §5.3 caveat) |
| E2 | Query invoices | `GET /v3/company/{realm}/query` | `query=<QBO SQL>`, `include=enhancedAllCustomFields` | yes |
| E3 | Read invoice | `GET /v3/company/{realm}/invoice/{id}` | `include=enhancedAllCustomFields` | yes |
| E4 | Sparse update invoice | `POST /v3/company/{realm}/invoice` | `requestid=<uuid4>`, `include=enhancedAllCustomFields` (if `QB_INCLUDE_ON_WRITES`) | yes **because** of `requestid` (same id on every attempt) |
| E5 | Send invoice e-mail | `POST /v3/company/{realm}/invoice/{id}/send` | optional `sendTo=<addr>`; `Content-Type: application/octet-stream`, empty body | **NO** — Intuit ignores `requestid` here; a repeat e-mails the customer again |
| E6 | Invoice PDF | `GET /v3/company/{realm}/invoice/{id}/pdf` | `Accept: application/pdf` | yes |
| E7 | Invoice pay link | `GET /v3/company/{realm}/invoice/{id}` | `include=invoiceLink` | yes |
| E8 | Read preferences | `GET /v3/company/{realm}/preferences` | — | yes |
| E9 | Update preferences | `POST /v3/company/{realm}/preferences` | `requestid=<uuid4>` | yes (requestid) |
| E10 | Company info | `GET /v3/company/{realm}/companyinfo/{realm}` | — | yes |

E5–E9 are used by 04b. E1 also: `https://appcenter.intuit.com/connect/oauth2` (authorize
URL, browser only, §5.4) and `https://developer.api.intuit.com/v2/oauth2/tokens/revoke`
(not used by the tool; mention in docs for manual revocation).

### 4.1 Query (E2)

```
SELECT * FROM Invoice WHERE DocNumber IN ('EBKG12345678', 'EBKG23456789 A', '267546627') MAXRESULTS 1000
```
- `IN` is supported for `DocNumber`; `OR` is not supported in the QBO query language.
- String literals are single-quoted; escape a single quote as `\'` (backslash). Other characters
  seen in real DocNumbers (`/`, `$`, spaces, letters) need no escaping inside quotes;
  httpx URL-encodes the whole `query` parameter.
- `MAXRESULTS` max is 1000 (default 100). With ≤ 25 DocNumbers per chunk a single page suffices;
  if `len(results) == 1000`, page with `STARTPOSITION` (1-based) until fewer than 1000 return.
- Case sensitivity of `=`/`IN` on DocNumber is UNVERIFIED → post-filter exactly (§6.3).
- Empty result: `{"QueryResponse": {}, "time": "…"}`.

### 4.2 Sparse update (E4)

```json
{
  "Id": "190613",
  "SyncToken": "7",
  "sparse": true,
  "DocNumber": "EBKG12345678 A",
  "CustomField": [
    {"DefinitionId": "1", "Name": "Container No.", "Type": "StringType", "StringValue": "MSCU1234567"},
    {"DefinitionId": "1000000003", "Name": "ETA", "Type": "DateType", "DateValue": "2026-11-04"}
  ]
}
```
- `DocNumber` present only when changing it; `CustomField` present only when setting the ETA.
- Response: `{"Invoice": {…full invoice…}, "time": "…"}` (HTTP 200).
- Array semantics in a sparse update are UNVERIFIED for `CustomField`; arrays such as `Line`
  are replaced wholesale. **Therefore always send the complete `CustomField` array as last read,
  with only the ETA entry replaced/appended** (§6.5).
- `requestid`: a fresh `uuid4().hex` per *logical* update, reused on every transport retry of that
  same body (Intuit returns the original result for a repeated requestid). A retry after a
  stale-object error (different SyncToken) is a new logical update → new requestid.

### 4.3 Errors

Error bodies (HTTP 400 for validation, 401 auth, 403 authorization, 429 throttle, 5xx):

```json
{"Fault": {"Error": [{"Message": "Stale Object Error", "Detail": "Stale Object Error : You and Jane Doe were working on this at the same time. ...", "code": "5010", "element": ""}], "type": "ValidationFault"}, "time": "2026-11-05T06:31:00.000-08:00"}
```
Auth/throttle faults may use lower-case keys (`"fault"`, `"error"`, `"message"`, `"detail"`,
`"code"`) — parse keys case-insensitively. A 200 response that contains a `Fault` key must also be
treated as an error.

| Code / status | Meaning | Handling |
|---|---|---|
| HTTP 401, code 3200 | access token expired/invalid | `request_with_auth()` invalidates + refreshes once; second 401 → `AuthError` |
| HTTP 403, code 3100/3003 | app not authorized for realm / missing scope | `AuthError("QuickBooks denied access to realm … (code …)")`, no retry |
| HTTP 429, code 3001 | throttled | retried by `http.request()` (honour `Retry-After`; Intuit advises waiting up to 60 s) — the limiter (§6.1) should make this rare |
| 400 code 5010 | stale object (SyncToken outdated) | §6.5 step 6: re-read, re-apply, retry once |
| 400 code 6140 | duplicate DocNumber | `InvoiceUpdateError("DocNumber '…' already used by another transaction")` |
| 400 code 610 | object not found / deleted | `InvoiceNotFoundError` |
| 400 other (2020, 2050, 2500, 6000, …) | validation | `InvoiceUpdateError` (or `InvoiceSendError` for E5) with `Message` + `Detail` |
| 5xx | server error | retried only for idempotent calls (E5 never) |

Always copy the `intuit_tid` response header into the error's `request_id` (03 §6 already does
this for `ServiceError`); include it in user-facing messages — Intuit support asks for it.

### 4.4 Throttling & metering

- 500 requests/min per realm, 10 requests/s per realm+app (official limits page), batch endpoint
  has separate per-minute ceilings. **Do not use `/batch`** — per-invoice error handling is simpler
  and volumes are low.
- Reads are metered (CorePlus). Count per run ≈ `ceil(rows/25)` queries + a few verification
  reads. Writes are unmetered (Core).

## 5. Auth / token lifecycle (`qbo/auth.py`)

### 5.1 Token endpoint (E1)

```
POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer
Authorization: Basic base64("{client_id}:{client_secret}")
Accept: application/json
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&refresh_token=<current refresh token>
```
200 response:
```json
{"token_type": "bearer", "access_token": "<jwt-ish>", "expires_in": 3600,
 "refresh_token": "<maybe-new>", "x_refresh_token_expires_in": 8726400}
```
- Access token lifetime: `expires_in` (3600 s).
- `x_refresh_token_expires_in`: seconds until the refresh token expires (≈ 101 days, rolling).
  Since late 2025 refresh tokens also have a **hard maximum lifetime of 5 years** (first
  accounting-scope expiries Oct 2028); Intuit added a field telling you when the token expires —
  store whatever expiry the response gives (`x_refresh_token_expires_in`, and if present any
  absolute-expiry field — log the raw key names at DEBUG on first refresh so the user can see them).
- Rotation: the response may carry a **different** `refresh_token` (Intuit documents that the value
  can change roughly every 24 h). Always persist the newest one; the old one may stop working.
- Errors: HTTP 400 `{"error": "invalid_grant"}` → refresh token revoked/expired/rotated away;
  HTTP 401 `{"error": "invalid_client"}` → bad client id/secret.

### 5.2 `QboAuth` — seeding, refresh, persistence

```python
class QboAuth:                                    # implements auth.TokenProvider
    def __init__(self, settings: QboSettings, token_store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None) -> None: ...
    async def get_token(self) -> str: ...
    async def invalidate(self) -> None: ...       # drop access token only
    async def force_refresh(self) -> None: ...
    @property
    def refresh_token_expires_at(self) -> float | None: ...
    async def aclose(self) -> None: ...
```

TokenStore entry `"qbo"`:
```json
{"access_token": "…", "expires_at": 1793900000.0,
 "refresh_token": "…", "refresh_token_expires_at": 1801600000.0,
 "refresh_token_seed": "…", "realm_id": "1234567890", "updated_at": 1793896400.0}
```

**Choosing the refresh token to use** (done once, lazily, inside the lock):
1. `env_rt = settings.refresh_token.strip()`; `entry = store.get("qbo") or {}`.
2. If `entry.get("realm_id")` is set and differs from `settings.realm_id` → ignore `entry` (different company).
3. If `entry` has no `refresh_token` → use `env_rt`; if that is empty →
   `AuthError("No QuickBooks refresh token. Run `eta qbo-auth` or set QB_REFRESH_TOKEN in .env")`.
4. Else if `env_rt` is non-empty and `env_rt not in (entry["refresh_token"], entry.get("refresh_token_seed"))`
   → the user pasted a **new** token: use `env_rt`, discard the cached access token, log INFO
   "Using the new QB_REFRESH_TOKEN from .env".
5. Else → use `entry["refresh_token"]` (the store is newer than or equal to `.env`).

**`get_token()`**: `async with self._lock:` if `is_fresh(entry, skew_seconds=120)` return cached
access token; otherwise `_refresh()`. `QboAuth` owns its own `httpx.AsyncClient` (no base URL) and
calls `http.request()` directly — it **never** acquires the QBO semaphore or limiter (03 §8), so a
caller holding a QBO slot can always obtain a token.

**`_refresh()`** (under the lock):
1. POST E1 via `http.request(..., service="qbo-oauth", expected=(200, 400, 401), idempotent=True)`.
2. 400 with `error == "invalid_grant"` → `AuthError("QuickBooks rejected the refresh token (invalid_grant). Reconnect with `eta qbo-auth` (or the OAuth Playground) and put the new token in QB_REFRESH_TOKEN. Do not delete tokens.json.")`. **Do not** delete or overwrite the stored token (Intuit outages have produced spurious invalid_grant).
3. 401 → `AuthError("QB_CLIENT_ID / QB_CLIENT_SECRET were rejected (invalid_client)")`.
4. 200 → build the new entry; **`token_store.put("qbo", entry)` first** (atomic), then if
   `new_rt != env_rt` write it back to `.env`: `dotenv.set_key(settings.env_file or Path(".env"), "QB_REFRESH_TOKEN", new_rt, quote_mode="never")`
   and set `os.environ["QB_REFRESH_TOKEN"] = new_rt`. A write-back failure (`OSError`) is a WARNING, not an error
   (the TokenStore is the source of truth). Never log token values.
5. If `refresh_token_expires_at - now < 30 days` → WARNING "QuickBooks connection expires on {date}; reconnect with `eta qbo-auth` before then."
6. `invalidate()` sets `entry["expires_at"] = 0` (keeps refresh token) and persists.

### 5.3 Caveat: refresh is "mostly" idempotent

If a refresh succeeds server-side but the response is lost, Intuit may already have rotated the
token. Retrying with the old token usually still works within the rotation grace window, but it
can produce `invalid_grant`. This is why the new token is persisted before anything else and why
`invalid_grant` never deletes state. Run only **one** instance of the tool at a time (the
pipeline's run lock, 10-pipeline.md, enforces this).

### 5.4 `eta qbo-auth` — authorization-code flow

```python
async def run_authorization_code_flow(settings: QboSettings, token_store: TokenStore, *,
                                      listen: bool = False, port: int = 8765,
                                      input_fn: Callable[[str], str] = input,
                                      open_browser: bool = True) -> None
```
1. `state = secrets.token_urlsafe(24)`.
2. `redirect = f"http://localhost:{port}/callback" if listen else settings.redirect_uri`.
3. Authorization URL:
   `https://appcenter.intuit.com/connect/oauth2?client_id=…&response_type=code&scope=<QB_OAUTH_SCOPES>&redirect_uri=<urlenc redirect>&state=<state>`.
4. Print the URL; if `open_browser`, call `webbrowser.open(url)` (the user's own browser — the
   user signs in and consents; nothing is automated).
5. **Paste-back mode (default; works with production keys):** print
   "After approving, your browser lands on a page whose address contains `code=` and `realmId=`.
   Copy the entire address bar and paste it here:" → `input_fn()` → parse with
   `urllib.parse.urlsplit/parse_qs` → `code`, `realmId`, `state`. Mismatched `state` →
   `AuthError("state mismatch — start again")`. An `error=` parameter → `AuthError(error_description)`.
   **Listen mode (`--listen`, development/sandbox keys only):** run a one-shot
   `asyncio.start_server` HTTP listener on `127.0.0.1:port`, parse the GET request line of
   `/callback?...`, reply with a small "You can close this tab" HTML page, stop the server
   (timeout 300 s → `AuthError`).
6. Exchange: POST E1 with `grant_type=authorization_code&code=…&redirect_uri=<same redirect>`.
7. If `QB_REALM_ID` is set and differs from `realmId` → `ConfigError("You connected company {realmId} but QB_REALM_ID is {…}")`.
   If unset → print it and write it to `.env` with `set_key`.
8. Persist the `"qbo"` entry exactly as in §5.2 step 4 and write `QB_REFRESH_TOKEN` to `.env`.
9. Print "Connected to <CompanyName> (realm …). Refresh token valid until <date>." (fetch E10).

Documented alternative (no code needed): **OAuth 2.0 Playground** —
developer.intuit.com → *Playground* → select the app → scope *Accounting* → *Get authorization code*
→ *Get tokens* → copy the refresh token into `QB_REFRESH_TOKEN`. §5.2 step 4 picks it up.

## 6. Module design (`qbo/client.py`)

### 6.1 Construction and plumbing

```python
class MultiLimiter:
    """async context manager entering several http.AsyncLimiter instances in order."""
    def __init__(self, *limiters: AsyncLimiter): ...

class QboClient:
    def __init__(self, settings: QboSettings, auth: QboAuth, *,
                 http: httpx.AsyncClient | None = None,
                 semaphore: asyncio.Semaphore | None = None,
                 clock: Clock | None = None,
                 token_store: TokenStore | None = None,     # ADDITION: needed by 04b emailers
                 state_dir: Path | None = None,             # ADDITION: for 04b recovery files
                 email_settings: "EmailSettings | None" = None,  # ADDITION: see 04b
                 read_only: bool = False) -> None: ...      # ADDITION: dry-run / probes
```
- `http` default: `new_async_client(base_url=settings.api_base)`.
- `self._sem = semaphore or asyncio.Semaphore(settings.max_concurrency)`.
- `self._limiter = MultiLimiter(AsyncLimiter(settings.max_calls_per_second, 1.0),
  AsyncLimiter(settings.max_calls_per_minute, 60.0))`.
- Private helper used by every call:

```python
async def _call(self, method: str, path: str, *, params: dict | None = None,
                json: Any = None, content: bytes | None = None,
                headers: Mapping[str, str] | None = None,
                expected: Collection[int] = (200, 400),
                idempotent: bool | None = None) -> httpx.Response:
    params = {"minorversion": self._s.minor_version, **(params or {})}
    async with self._sem:
        return await request_with_auth(self._http, method, path, service="qbo", auth=self._auth,
                                       params=params, json=json, content=content, headers=headers,
                                       expected=expected, idempotent=idempotent,
                                       limiter=self._limiter, max_attempts=5)
```
- `_faults(resp) -> list[QboFault]` where `@dataclass QboFault(code: str, message: str, detail: str)`;
  returns `[]` when the JSON has no `Fault`/`fault` key; tolerant of key case and of a missing body.
- Writes (`update_invoice`, `send_invoice`, preferences writes) raise
  `RuntimeError("QboClient is read-only (dry run / probe)")` when `read_only=True`.

### 6.2 `InvoiceSnapshot`

```python
@dataclass(frozen=True, slots=True)
class InvoiceSnapshot:
    id: str
    sync_token: str
    doc_number: str
    total_amount: Decimal
    balance: Decimal
    customer_name: str | None
    bill_email: str | None            # BillEmail.Address (may hold several addresses, comma/semicolon separated)
    email_status: str | None          # "NotSet" | "NeedToSend" | "EmailSent"
    eta_value: date | None            # current ETA custom-field value, if present and parseable
    raw: Mapping[str, Any]            # full Invoice JSON as returned
    # ── additions ──
    bill_email_cc: str | None = None  # BillEmailCc.Address
    bill_email_bcc: str | None = None # BillEmailBcc.Address
    due_date: date | None = None
    delivery_time: str | None = None  # DeliveryInfo.DeliveryTime (ISO string) if ever sent by QBO
    custom_fields: tuple[Mapping[str, Any], ...] = ()

    @property
    def is_paid(self) -> bool:            # UI "#balanceAmount" == "$0.00"
        return self.balance == 0
    @property
    def is_partially_paid(self) -> bool:  # UI "Partially paid" banner
        return Decimal(0) < self.balance < self.total_amount
    @property
    def recipients(self) -> tuple[str, ...]: ...   # split bill_email on [,;], strip, drop empties

    @classmethod
    def from_api(cls, raw: Mapping[str, Any], eta_field: "EtaFieldSpec | None",
                 *, string_format: str) -> "InvoiceSnapshot": ...
```
Parsing rules: money via `Decimal(str(raw.get("TotalAmt", 0)))` / `Balance`; missing `Balance`
→ treat as `TotalAmt` (unpaid) and log WARNING; `customer_name = raw.get("CustomerRef", {}).get("name")`;
emails from `raw.get("BillEmail") or {}` etc. (empty string → None); `due_date` from
`DueDate` (`YYYY-MM-DD`); `eta_value` from the ETA entry (§6.4) parsed by `parse_qbo_date()`.

`parse_qbo_date(value) -> date | None`: accepts `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SS[±hh:mm]`,
`MM/DD/YYYY`, `M/D/YYYY`; anything else → None.

Paid/partial notes: `Balance` already nets out payments, deposits (`Deposit`) and applied
credits, so `0 < Balance < TotalAmt` matches the UI banner for the normal payment case; an
invoice with `TotalAmt == 0` is "paid" (UI shows `$0.00`) — parity with the original.

### 6.3 `find_invoices_by_doc_numbers()`

```python
async def find_invoices_by_doc_numbers(self, doc_numbers: Sequence[str]) -> dict[str, InvoiceSnapshot]
self.ambiguous: dict[str, tuple[str, ...]]        # ADDITION: DocNumber -> invoice Ids, cumulative
async def get_invoice_by_doc_number(self, doc_number: str) -> InvoiceSnapshot   # ADDITION
```
Algorithm:
1. `wanted = list(dict.fromkeys(d.strip() for d in doc_numbers if d and d.strip()))`.
2. Split into chunks of `query_chunk_size`; run the chunks concurrently (`asyncio.gather`; the
   semaphore/limiter bound them).
3. Per chunk: `q = "SELECT * FROM Invoice WHERE DocNumber IN (" + ", ".join(_quote(d) for d in chunk) + ") MAXRESULTS 1000"`
   where `_quote(d) = "'" + d.replace("'", "\\'") + "'"`. Call E2 with
   `params={"query": q, "include": "enhancedAllCustomFields"}`.
   - If the response is 400 (any fault) **and** `len(chunk) > 1` → log WARNING and fall back to one
     `WHERE DocNumber = '<d>'` query per DocNumber in that chunk (isolates a problematic value).
     A single-value query that still faults → that DocNumber is simply absent (log the fault).
   - Paginate with `STARTPOSITION` if exactly 1000 results.
4. Feed every returned raw invoice to `_learn_eta_field(raw)` (§6.4) **before** building snapshots.
5. For each wanted `d`: `exact = [i for i in all if i.get("DocNumber") == d]`; if none,
   `ci = [i for i in all if (i.get("DocNumber") or "").casefold() == d.casefold()]` and use it
   only if exactly one (INFO "case-insensitive DocNumber match"). One match → snapshot.
   More than one → `self.ambiguous[d] = ids` and **exclude** from the result (WARNING listing ids).
   **DEVIATION:** the original silently took `invoices[0]`; e-mailing a customer from an arbitrary
   duplicate is unsafe, so ambiguous DocNumbers are not processed.
6. Return `{d: snapshot}` for found ones. Missing → absent.

`get_invoice_by_doc_number(d)`: calls the batch method with `[d]`; raises
`AmbiguousInvoiceError(f"{len(ids)} QuickBooks invoices share DocNumber {d!r} (Ids {', '.join(ids)}) — fix in QBO")`
or `InvoiceNotFoundError(f"#{d} not found in QuickBooks")`.
Add to `errors.py`: `class AmbiguousInvoiceError(InvoiceNotFoundError): ...` (addition to 03 §5).

The pipeline treats `d in qbo.ambiguous` as a failure with the AmbiguousInvoiceError message
and absence otherwise as "not found in QuickBooks".

### 6.4 ETA custom field discovery (`EtaFieldSpec`)

```python
@dataclass(frozen=True, slots=True)
class EtaFieldSpec:
    definition_id: str
    name: str
    type: str          # as reported by QBO: "DateType", "StringType", "NumberType", …
    value_key: str     # "DateValue" | "StringValue" | "NumberValue" | "BooleanValue"
    source: str        # "invoice <Id>" | "config"
```
Value key derivation: the first of `DateValue`, `StringValue`, `NumberValue`, `BooleanValue`
present in the entry; otherwise from `Type` (`DateType→DateValue`, `StringType→StringValue`,
`NumberType→NumberValue`, `BooleanType→BooleanValue`; unknown → `StringValue`).

`_match_eta_entry(entries) -> Mapping | None`:
1. If `settings.eta_definition_id` and some entry has `str(entry["DefinitionId"]) == it` → that entry.
2. Else: entries whose normalized name equals `settings.eta_field_name.casefold()` where
   `normalize(n) = n.strip().casefold()`; skip names ending in `"(inactive)"`
   (QBO appends this to deactivated fields). One → that entry. Several → prefer one that has a
   value key; still several → the first, WARNING.
3. Else None.

`_learn_eta_field(raw)`: if `self._eta_field` is None and `_match_eta_entry(raw.get("CustomField") or [])`
returns an entry → store `EtaFieldSpec(source=f"invoice {raw['Id']}")`. If the match came from
the *name* rule while `settings.eta_definition_id` was set → WARNING once:
"QB_ETA_DEFINITION_ID={cfg} was not found; QuickBooks reports field '{name}' as DefinitionId {id} (Type {type}). Set QB_ETA_DEFINITION_ID={id} in .env."
If a later invoice reports a different DefinitionId for the same name → WARNING once, keep the first.

`_eta_field_for_write(raw) -> EtaFieldSpec`: entry on this invoice if present (spec from it), else
the learned spec, else — if `settings.eta_definition_id` — `EtaFieldSpec(definition_id=cfg, name=settings.eta_field_name, type=settings.eta_field_type, value_key=<derived>, source="config")`
with a WARNING "ETA field shape not observed on any invoice; using QB_ETA_FIELD_TYPE={…} (UNVERIFIED)",
else `InvoiceUpdateError("Cannot determine the QuickBooks ETA custom field. Run `eta qbo-fields --invoice <DocNumber>` on an invoice whose ETA is filled in, then set QB_ETA_DEFINITION_ID / QB_ETA_FIELD_TYPE.")`.

Value formatting `_format_eta(spec, d: date)`: `DateValue` → `d.isoformat()` (UNVERIFIED shape);
`StringValue` → `d.strftime(settings.eta_string_format)`; `NumberValue`/`BooleanValue` →
`InvoiceUpdateError("ETA custom field has unsupported type …")`.

### 6.5 `update_invoice()`

```python
async def update_invoice(self, inv: InvoiceSnapshot, *, eta: date | None = None,
                         doc_number: str | None = None) -> InvoiceSnapshot
```
1. Read-only guard. If both args are None → return `inv`.
2. `want_doc = doc_number if doc_number is not None and doc_number != inv.doc_number else None`;
   `want_eta = eta if eta is not None and eta != inv.eta_value else None`.
   If both None → DEBUG "no change" and return `inv` (no API call).
3. Build body: `{"Id": inv.id, "SyncToken": inv.sync_token, "sparse": True}`;
   if `want_doc`: `body["DocNumber"] = want_doc`;
   if `want_eta`: `spec = self._eta_field_for_write(inv.raw)`;
   `entries = [dict(e) for e in (inv.raw.get("CustomField") or []) if _has_value(e) and str(e.get("DefinitionId")) != spec.definition_id]`;
   append `{"DefinitionId": spec.definition_id, "Name": spec.name, "Type": spec.type, spec.value_key: _format_eta(spec, want_eta)}`;
   `body["CustomField"] = entries`. (`_has_value(e)`: any value key present and not None/"".)
4. POST E4 (`idempotent=True`, new `requestid`, include param if configured).
5. 200 without fault → `updated = InvoiceSnapshot.from_api(resp_json["Invoice"], …)`; go to step 7.
6. Fault handling:
   - `5010` → `fresh = await self.get_invoice(inv.id)`; if `want_doc` and `fresh.doc_number != inv.doc_number`
     → `InvoiceUpdateError(f"Invoice {inv.id} DocNumber changed in QuickBooks while processing ({inv.doc_number!r} → {fresh.doc_number!r}); not overwriting")`;
     else rebuild the body from `fresh` (same `want_doc`/`want_eta`) and POST once more with a new
     requestid. A second 5010 → `InvoiceUpdateError("stale object twice — someone is editing invoice …")`.
   - `6140` → `InvoiceUpdateError(f"DocNumber {want_doc!r} is already used in QuickBooks (duplicate document number)")`.
   - `610` → `InvoiceNotFoundError(f"Invoice {inv.id} no longer exists in QuickBooks")`.
   - other → `InvoiceUpdateError("qbo", f"update rejected: {code} {message} — {detail}", status=400, request_id=…)`.
7. Verification (`settings.verify_updates`):
   - `want_doc` and `updated.doc_number != want_doc` → `InvoiceUpdateError("QuickBooks stored DocNumber {updated.doc_number!r} instead of {want_doc!r} (length limit?)")`.
   - `want_eta`: if `updated.eta_value == want_eta` → OK. Else (field absent from the POST response or
     different) → `check = await self.get_invoice(inv.id)`; if `check.eta_value == want_eta` → OK
     (return `check`); else → `InvoiceUpdateError(f"QuickBooks accepted the update but the ETA custom field did not change on invoice {inv.id} (still {check.eta_value}). The API may be unable to write this field — see 04 §8.")`.
8. INFO `"[QBO] Invoice #{doc} saved — ETA={eta} DocNumber={doc2}"`; return the verified snapshot.

`get_invoice(invoice_id)`: E3 with include; 400 code 610 → `InvoiceNotFoundError`; returns snapshot
(and `_learn_eta_field`).

### 6.6 `send_invoice()` and e-mail plumbing

```python
async def prepare_for_sending(self, *, dry_run: bool = False) -> None   # ADDITION — see 04b §6.2
async def send_invoice(self, inv: InvoiceSnapshot, *, subject: str) -> "SendReceipt"
```
- `send_invoice` delegates to the configured `InvoiceEmailer` (04b), created lazily by
  `qbo.email.build_emailer(self, …)`. It returns a `SendReceipt` (04b) — callers that ignore the
  return value still satisfy the required `-> None` contract.
- `send_invoice` calls `prepare_for_sending()` automatically on first use, but **the pipeline must
  call `await qbo.prepare_for_sending(dry_run=…)` once before applying any plan that sends**, so a
  blocked e-mail path is detected *before* any DocNumber has been suffixed (otherwise an invoice
  could get `" A"` without an e-mail).
- Low-level helpers 04b uses (all go through `_call`):
  `_post_send(invoice_id, send_to: str | None) -> dict` (E5, `idempotent=False`, `max_attempts=1`
  for any status other than 429/503 — never re-POST after the request may have reached Intuit),
  `get_invoice_pdf(invoice_id) -> bytes` (E6; must start with `b"%PDF"` else `ServiceError`),
  `get_invoice_link(invoice_id) -> str | None` (E7, returns `Invoice.InvoiceLink` or None),
  `get_preferences() -> dict` (E8, returns the `Preferences` object),
  `update_preferences(prefs: Mapping) -> dict` (E9, returns the new `Preferences` object),
  `get_company_info() -> dict` (E10),
  `mark_email_sent(inv) -> InvoiceSnapshot` (sparse update `{"Id", "SyncToken", "sparse": true, "EmailStatus": "EmailSent"}`,
  used only by the mailbox strategies; UNVERIFIED that QBO accepts it — callers treat failure as a warning).
- E-mail errors (defined in 04b §6.1, added to `errors.py`): `EmailPathUnavailableError(InvoiceSendError)`
  — no invoice e-mail can be sent this run (raised by `prepare_for_sending()` or mid-run);
  `TemplateRestoreError(EmailPathUnavailableError)` — the QBO template could not be restored.
  The pipeline stops applying NOTIFY plans on either; any other `InvoiceSendError` affects one invoice.

### 6.7 `list_custom_fields()` (for `eta qbo-fields`)

```python
async def list_custom_fields(self, doc_number: str) -> list[dict[str, Any]]
```
Returns one dict per `CustomField` entry of that invoice:
`{"DefinitionId", "Name", "Type", "value_key", "value", "is_eta_match": bool, "match_rule": "definition_id"|"name"|None}`
plus a final pseudo-row `{"resolved_eta_field": asdict(spec) or None}`. The CLI prints it as a table
and suggests the `.env` lines.

### 6.8 `build_qbo()` factory

```python
def build_qbo(settings: Settings, token_store: TokenStore, *, read_only: bool = False) -> QboClient
```
Creates `QboAuth` and `QboClient` (passing `token_store`, `settings.run.state_dir`,
`settings.email`). `aclose()` closes the emailer, the auth client and the HTTP client.

## 7. Business logic notes (what QBO must reproduce)

- **DocNumber suffix** (computed by the pipeline/rules, passed as `doc_number`): when the ETA date
  ≤ today and the invoice is unpaid: `doc + " A"` if `doc[-1].isdigit()` else `doc + "A"`.
  `" A"` → `"X A"`; on the next cycle `"X A"` → `"X AA"`. QBO's documented DocNumber limit is
  21 characters (the company's data contains a 22-character value, so the real limit is
  UNVERIFIED). Standard bookings never get close (`EBKG12345678 AA` = 15). Provide

  ```python
  def doc_number_problem(new_doc: str, settings: QboSettings) -> str | None:
      """None if OK, else a human reason, e.g.
      "New invoice number 'X' is 23 characters; QuickBooks allows at most 21 (QB_DOCNUMBER_MAX_LEN)"."""
  ```
  The pipeline (10) calls it while planning and fails the row with that reason (no QBO call).
  QBO's own error (mapped to `InvoiceUpdateError`) and the §6.5 step 7 verification remain the
  final safety net.
- **Reverting a DocNumber** (pipeline D11, after a failed send): `update_invoice(updated, doc_number=original)`
  — the same sparse update path; it is a no-op if QBO already shows the original.
- **Order inside NOTIFY** (parity with "Review and send" which saved before opening the dialog):
  update (ETA + DocNumber) first, then send. If the send then fails, the DocNumber has already
  changed — same as the original; the pipeline records "QB Invoice Processing Error, No Email Sent".
- **Subject**: produced by `texts.subject_for()` (01); it embeds the DocNumber **as read before**
  the suffix change (the UI read `invNum` before editing it). 04b sends it verbatim.
- **Confirmation modals** the UI flow clicked (`confirmation-modal-confirm`) have no API equivalent;
  nothing to do.
- **No-op updates**: skipping an update whose values already match is safe (DEVIATION: the UI
  always clicked Save; the result is identical).

## 8. Fallbacks

1. **ETA field cannot be written through the API** (verification in §6.5 step 7 fails, or the
   probe write-test fails — a reported production issue exists where enhanced custom fields
   could be read but not written):
   a. Re-check the field is active and enabled for Invoices; re-run `eta qbo-fields`.
   b. If it still fails: create a **String**-type custom field named `ETA` among the first three
      string custom fields (these are exposed to the API on every plan via `DefinitionId` 1–3),
      enable it on invoices/the form, set `QB_ETA_DEFINITION_ID` to its id and keep
      `QB_ETA_STRING_FORMAT=%m/%d/%Y`. Customers then see the ETA as text — business decision.
   c. Or use the standard **Ship date** (`ShipDate`) field if shipping fields are enabled (add a
      setting `QB_ETA_TARGET=custom_field|ship_date`; `ship_date` writes `"ShipDate": "YYYY-MM-DD"`).
      Only implement if (a) and (b) are rejected by the user.
2. **Refresh token lost / invalid_grant**: `eta qbo-auth` (or OAuth Playground) → new token.
3. **Throttling**: lower `QB_MAX_CONCURRENCY` / `QB_MAX_CALLS_PER_SECOND`.
4. There is **no** supported non-browser way to drive the QBO web UI's private endpoints; do not
   attempt it.

## 9. Fixtures & tests

### 9.1 Fixtures

`tests/fixtures/qbo/token_refresh_ok.json`
```json
{"token_type": "bearer", "access_token": "AT-NEW-123", "expires_in": 3600,
 "refresh_token": "RT-NEW-456", "x_refresh_token_expires_in": 8726400}
```
`tests/fixtures/qbo/token_invalid_grant.json` (HTTP 400)
```json
{"error": "invalid_grant"}
```
`tests/fixtures/qbo/query_invoices.json`
```json
{"QueryResponse": {"Invoice": [
  {"Id": "190613", "SyncToken": "7", "DocNumber": "EBKG12345678", "TxnDate": "2026-10-20",
   "DueDate": "2026-11-19", "TotalAmt": 2450.0, "Balance": 2450.0, "Deposit": 0,
   "CustomerRef": {"value": "58", "name": "Acme Imports LLC"},
   "BillEmail": {"Address": "ap@acme.example, ops@acme.example"},
   "BillEmailCc": {"Address": "cc@acme.example"}, "EmailStatus": "NeedToSend",
   "CustomField": [
     {"DefinitionId": "1", "Name": "Container No.", "Type": "StringType", "StringValue": "MSCU1234567"},
     {"DefinitionId": "1000000003", "Name": "ETA", "Type": "DateType", "DateValue": "2026-11-02"}],
   "LinkedTxn": [], "MetaData": {"CreateTime": "2026-10-20T09:12:00-07:00", "LastUpdatedTime": "2026-10-28T10:00:00-07:00"}},
  {"Id": "190700", "SyncToken": "2", "DocNumber": "EBKG23456789 A", "TotalAmt": 1875.5, "Balance": 875.5,
   "CustomerRef": {"value": "61", "name": "Beta Freight Co"}, "BillEmail": {"Address": "billing@beta.example"},
   "EmailStatus": "EmailSent", "DeliveryInfo": {"DeliveryType": "Email", "DeliveryTime": "2026-10-30T08:00:00-07:00"},
   "CustomField": [{"DefinitionId": "1", "Name": "Container No.", "Type": "StringType", "StringValue": "MSCU7654321"}],
   "LinkedTxn": [{"TxnId": "8812", "TxnType": "Payment"}]},
  {"Id": "190801", "SyncToken": "0", "DocNumber": "267546627", "TotalAmt": 3200.0, "Balance": 0,
   "CustomerRef": {"value": "70", "name": "Gamma Trading"}, "BillEmail": {"Address": "ap@gamma.example"},
   "CustomField": [{"DefinitionId": "1000000003", "Name": "ETA", "Type": "DateType"}]},
  {"Id": "190900", "SyncToken": "1", "DocNumber": "NAM1234567", "TotalAmt": 5000, "Balance": 5000,
   "CustomerRef": {"value": "71", "name": "Delta"}},
  {"Id": "190901", "SyncToken": "1", "DocNumber": "NAM1234567", "TotalAmt": 5000, "Balance": 5000,
   "CustomerRef": {"value": "71", "name": "Delta"}}
 ], "startPosition": 1, "maxResults": 5}, "time": "2026-11-05T06:30:00.000-08:00"}
```
Expected (request `["EBKG12345678", "EBKG23456789 A", "267546627", "NAM1234567", "EBKG99999999"]`):
- `"EBKG12345678"` → id 190613, unpaid (`is_paid False`, `is_partially_paid False`),
  `eta_value == date(2026,11,2)`, `recipients == ("ap@acme.example", "ops@acme.example")`.
- `"EBKG23456789 A"` → partially paid (`875.5 < 1875.5`), `eta_value None`.
- `"267546627"` → paid (`Balance 0`), `eta_value None` (entry has no value).
- `"NAM1234567"` → absent; `qbo.ambiguous["NAM1234567"] == ("190900", "190901")`.
- `"EBKG99999999"` → absent, not ambiguous.
- Learned ETA spec: `EtaFieldSpec("1000000003", "ETA", "DateType", "DateValue", "invoice 190613")`.

`tests/fixtures/qbo/update_ok.json` — `{"Invoice": {…190613 with "SyncToken": "8", "DocNumber": "EBKG12345678 A", ETA entry "DateValue": "2026-11-04"…}, "time": "…"}`.
`tests/fixtures/qbo/fault_stale_5010.json` — the §4.3 example.
`tests/fixtures/qbo/fault_duplicate_6140.json` —
`{"Fault": {"Error": [{"Message": "Duplicate Document Number Error", "Detail": "Duplicate Document Number Error : You must specify a different number. This number has already been used. DocNumber=EBKG12345678 A is assigned to TxnType=Invoice with TxnId=180001", "code": "6140", "element": ""}], "type": "ValidationFault"}, "time": "…"}`.
`tests/fixtures/qbo/fault_auth_401.json` —
`{"fault": {"error": [{"message": "message=AuthenticationFailed; errorCode=003200; statusCode=401", "detail": "Token expired", "code": "3200"}], "type": "AUTHENTICATION"}, "time": "…"}`.
`tests/fixtures/qbo/companyinfo.json` — `{"CompanyInfo": {"CompanyName": "Example Forwarding LLC", "Email": {"Address": "accounting@example.com"}}, "time": "…"}`.
(04b adds `preferences.json`, `send_ok.json`, Graph fixtures.)

### 9.2 Unit tests (`tests/test_qbo_auth.py`, `tests/test_qbo_client.py`) — respx-mocked, no network

Auth:
1. Refresh persists the rotated token to the TokenStore **before** returning the access token; `.env` (tmp file) gets `QB_REFRESH_TOKEN=RT-NEW-456`.
2. Seeding: store newer than `.env` (env == seed) → store token used.
3. Seeding: user pasted a new env token (≠ store, ≠ seed) → env token used, cached access token discarded.
4. Different `realm_id` in store → store ignored.
5. `invalid_grant` → `AuthError`, store unchanged.
6. 20 concurrent `get_token()` calls → exactly one POST to the token endpoint.
7. API 401 (fixture `fault_auth_401.json`) → one refresh + one retry; second 401 → `AuthError`.
8. Expiry warning logged when `x_refresh_token_expires_in < 30 days`.
9. `run_authorization_code_flow` paste-back mode: fake `input_fn` returns a redirect URL with `code`, `realmId`, `state` → token exchange body contains the same `redirect_uri`; state mismatch → `AuthError`.

Client:
10. Query text: chunking (`QB_QUERY_CHUNK_SIZE=2` → 3 queries for 5 numbers) and quote escaping (`O'Neil 1` → `'O\'Neil 1'`), `include=enhancedAllCustomFields` and `minorversion=75` present.
11. Exact match, case-insensitive single fallback, ambiguous exclusion (fixture expectations above).
12. A 400 on an IN query falls back to single `=` queries for that chunk.
13. `is_paid` / `is_partially_paid` truth table: (total 100, bal 0) paid; (100, 40) partial; (100, 100) neither; (0, 0) paid.
14. ETA field resolution: by configured id; by name when configured id absent (WARNING text includes the correct id); `"ETA (inactive)"` ignored; config fallback when nothing observed.
15. Update body: sparse, full `CustomField` array preserved (Container No. kept), ETA entry replaced, `DocNumber` only when changing, `requestid` present.
16. No-op update makes zero HTTP calls.
17. Stale 5010 → GET + one retry with a **new** requestid and the fresh SyncToken.
18. Stale 5010 with concurrent DocNumber change → `InvoiceUpdateError`, no second POST.
19. 6140 → `InvoiceUpdateError` mentioning the DocNumber.
20. Verification: POST response lacks the ETA value and the verification GET still shows the old one → `InvoiceUpdateError`; GET shows the new one → success.
21. Transport retry of an update reuses the same `requestid` (simulate `httpx.ConnectError` then 200).
22. `StringType` field → `StringValue` formatted `11/04/2026`.
23. `read_only=True` → `update_invoice`/`send_invoice` raise `RuntimeError`, no HTTP calls.
24. `list_custom_fields()` marks the matched entry and returns the resolved spec.
25. Rate limiting: with `max_calls_per_second=2`, five calls take ≥ 2 s on a fake clock (or assert limiter acquisition counts).

## 10. Probe / verification procedure (safe)

All probe commands use `read_only=True` unless a write flag is given, print redacted JSON, and save
raw payloads to `<ETA_STATE_DIR>/probe/qbo_*.json`.

1. **Sandbox first** (`QB_SANDBOX=true` with sandbox keys/realm): run steps 2–4 and 04b's send test.
2. `eta probe qbo` (read-only, production OK): refreshes the token (prints refresh-token expiry,
   never the token), prints CompanyInfo name, the invoice e-mail template subject/message from
   Preferences, `SalesFormsPrefs.ETransactionAttachPDF`, `CustomTxnNumbers`.
3. `eta qbo-fields --invoice <DocNumber whose ETA is visible in the QBO UI>` (read-only):
   confirms the `DefinitionId`, `Type` and value key of the ETA field. Put the suggested
   `QB_ETA_DEFINITION_ID` / `QB_ETA_FIELD_TYPE` into `.env`. **If the ETA field does not appear at
   all**, enable it for invoices in QBO and retry; if it still does not appear, go to §8.1.
4. `eta probe qbo --write-test --invoice <DocNumber>` (writes; asks for confirmation; refuses in
   production unless `--yes` is also given): reads the invoice, sets the ETA field to
   original+1 day, verifies via a fresh GET, then restores the original value (or clears it back
   to its previous state) and verifies again. Never touches DocNumber or e-mail. Reports
   PASS/FAIL with the exact request/response bodies (redacted). FAIL → §8.1.
5. E-mail probes: see 04b §10 (they never e-mail a real customer).

## 11. Sources & verification status

| Claim | Source | Status |
|---|---|---|
| Enhanced custom fields are read via `include=enhancedAllCustomFields` on v3 entity/query endpoints, with `minorversion=75` in current examples | Intuit developer blog (blogs.a.intuit.com/?p=35403, not reachable from the research sandbox — content via search excerpts); Intuit "custom field use cases" page (search excerpt, page now 404); Intuit developer forum thread (DefinitionId `1000000001` returned with this include) | VERIFIED-DOCS (excerpts) / OBSERVED |
| GraphQL `legacyIDV2` == REST `CustomField.DefinitionId`; GraphQL endpoint `https://qb.api.intuit.com/graphql`; scopes `app-foundations.custom-field-definitions[.read]` | https://developer.intuit.com/app/developer/qbo/docs/workflows/create-custom-fields/develop-custom-fields (search excerpt); https://github.com/IntuitDeveloper/Sampleapp-Customfields-Nodejs | VERIFIED-DOCS |
| Custom Fields (GraphQL) is a premium API (Silver/Gold/Platinum), production-only | Intuit docs excerpt; https://www.apideck.com/blog/quickbooks-api-pricing-and-the-intuit-app-partner-program | VERIFIED-DOCS |
| Only the first three String custom fields are exposed through the legacy API path; DATE/NUMBER/DROPDOWN enhanced fields exist | Intuit help article "Enhanced Custom Fields for QuickBooks Online Advanced" (search excerpt); https://developer.intuit.com/app/developer/qbo/docs/workflows/create-custom-fields/create-custom-fields-legacy | VERIFIED-DOCS (excerpt) |
| JSON shape of a DATE custom-field value (`"Type": "DateType"`, `"DateValue": "YYYY-MM-DD"`) | none found | **UNVERIFIED** → discovered at runtime (§6.4), verified on write (§6.5), probe §10.4 |
| Writing an enhanced custom field can fail in production while working in sandbox | https://help.developer.intuit.com/s/question/0D5TR00001LubOT0AZ/ (search excerpt, unresolved) | OBSERVED (risk) |
| Custom fields not enabled for the sales form are invisible to the API | Intuit developer forum (search excerpt, Intuit support reply) | OBSERVED |
| Minor versions 1–74 deprecated 1 Aug 2025; lower values ignored and answered as 75 | https://developer.intuit.com/app/developer/qbo/docs/learn/explore-the-quickbooks-online-api/minor-versions (search excerpt); https://www.apideck.com/blog/ai-built-accounting-integrations-maintenance | VERIFIED-DOCS |
| Refresh tokens: max 5-year lifetime (accounting scope tokens from Oct 2023 expire from Oct 2028); new expiry field; mandatory Reconnect URL | https://blogs.intuit.com/2025/11/12/important-changes-to-refresh-token-policy (redirects to Medium; search excerpt); https://developer.intuit.com/app/developer/qbo/docs/release-notes/general-release-notes (June 2026 entry, excerpt) | VERIFIED-DOCS (excerpt) |
| `x_refresh_token_expires_in` returned (typical 8 726 400 s); tokens rotate | https://github.com/intuit/intuit-cli-for-quickbooks/pull/2; Intuit Authorization FAQ (excerpt: value can change ~every 24 h) | OBSERVED / VERIFIED-DOCS |
| Production redirect URIs must be HTTPS, no localhost/IP | https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/set-redirect-uri (excerpt) | VERIFIED-DOCS |
| OAuth Playground redirect URL `…/v2/OAuth2Playground/RedirectUrl` | developer.intuit.com page bundle (stage variant observed) | OBSERVED |
| Throttles: 500 req/min/realm, 10 req/s per realm+app; 429 → wait ~60 s; batch ceilings | https://developer.intuit.com/app/developer/qbo/docs/learn/limits-and-throttles (search excerpt) | VERIFIED-DOCS |
| App Partner Program: CorePlus reads metered, Builder 500k/month then blocked; Core writes free | apideck + truto blogs (secondary) | OBSERVED |
| Query language: `IN` supported, no `OR`, `MAXRESULTS` ≤ 1000, backslash-escaped quotes; long IN lists may fail | https://developer.intuit.com/hub/blog/2017/02/08/deep-dive-sql-queries/ ; apideck invoice API blog; blog comments | VERIFIED-DOCS / OBSERVED |
| Error codes 5010 stale, 6140 duplicate DocNumber, 610 not found, 6000 validation | https://www.apideck.com/blog/quickbooks-invoice-api-integration | OBSERVED |
| DocNumber max 21 chars | apideck (secondary); contradicted by 22-char DocNumbers in the company's data | UNVERIFIED |
| `requestid` makes writes idempotent; ignored on `/send` | Intuit 2016 throttling blog (retry with same requestId); https://github.com/voska/qbo-cli/issues/19 | INFERRED / OBSERVED |
| Lower-case `fault`/`error` keys on 401/429 | practical experience | INFERRED |
| Sparse-update array semantics for `CustomField` | none found | UNVERIFIED → always send full array |
