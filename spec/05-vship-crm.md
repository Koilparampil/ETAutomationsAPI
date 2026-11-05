# 05 — VShipCRM API (`vship/client.py`, `vship/logic.py`)

## 1. Summary

The original already talked to VShipCRM over plain HTTPS (`requests`) — no browser — but it:
- re-read a token file and re-checked its mtime on every call,
- searched for the same booking 2–4 times per row (notif lookup, TR/SWB lookup, note writing, booking id),
- popped Tk login dialogs from deep inside lookups when the token expired,
- had no timeouts on two POST/PATCH calls and no retry policy.

The new client is async (`httpx.AsyncClient`), caches the token in `TokenStore` (`"vship"`), resolves each
base booking to its VShip id **once per run**, fetches each booking's comments **once per run** (shared by
the notif-status and TR/SWB checks), and re-authenticates automatically on 401 using the `.env` credentials.
Business logic (notif status, TR/SWB keywords, best-match ranking) is pure code in `vship/logic.py`
(rules in 01-business-rules.md §11–12 and §6 below).

## 2. Access & onboarding

Nothing new: the same VShipCRM user the original used (`VSHIP_USER_NAME` / `VSHIP_PASSWORD`). That user
must be allowed to read bookings/comments, post internal comments and edit a booking's expected arrival date.

## 3. Configuration (`VShipSettings`)

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `VSHIP_USER_NAME` | yes | — | VShipCRM username / e-mail |
| `VSHIP_PASSWORD` | yes | — | VShipCRM password |
| `VSHIP_API_BASE` | no | `https://vship2000-prod-api.azurewebsites.net/api` | API root (no trailing slash) |
| `VSHIP_MAX_CONCURRENCY` | no | `4` | Concurrent HTTP calls to VShip |
| `VSHIP_TOKEN_TTL_MINUTES` | no | `55` | Token lifetime to assume when the token is not a JWT with `exp` (the original assumed 60) |
| `VSHIP_COMMENT_TYPE` | no | `1` | `commentType` sent when posting notes (original: `1` = internal comment) |
| `ETA_ON_NOTIF_LOOKUP_ERROR` | no | `send` | See 01-business-rules.md §6.2 (lives in `RunSettings`) |

```python
@dataclass(frozen=True)
class VShipSettings:
    username: str
    password: str
    api_base: str = "https://vship2000-prod-api.azurewebsites.net/api"
    max_concurrency: int = 4
    token_ttl_minutes: int = 55
    comment_type: int = 1
```

## 4. Endpoints

All paths are relative to `VSHIP_API_BASE`. All authenticated calls send
`Authorization: Bearer <token>` and `Accept: application/json`. Write calls additionally send
`Content-Type: application/json` and `Referer: https://vshipcrm.com/` (parity with the original; harmless).

| # | Purpose | Method & path | Body / params | Success | Response fields used |
|---|---|---|---|---|---|
| V1 | Login | `POST /Auth/login` (no auth header) | JSON `{"username": u, "password": p}` | 200 | `value.token` |
| V2 | Search bookings | `GET /Bookings/searchBooking` | params `searchText=<base booking>`, `page=1`, `pageSize=50` (let httpx URL-encode) | 200 | `value.data[]` → `bookingId`, `bookingNo` |
| V3 | Booking detail | `GET /Bookings/{bookingId}` | — | 200 | `value.customerName` |
| V4 | Booking comments | `GET /Bookings/{bookingId}/comments` | — | 200 | `value.internalComments[]` → `comment`, `createdDate` |
| V5 | Post comment | `POST /Bookings/{bookingId}/comments` | JSON `{"comment": text, "commentType": 1}` | **201** | — |
| V6 | Update ETA | `PATCH /Bookings/{bookingId}` | JSON `{"booking_Id": str(bookingId), "expected_Arrival_Date": eta.strftime("%Y-%m-%dT%H:%M:%S")}` | **204** | — |

Notes:
- Only the fields listed above are relied upon (they are what the original read). Everything else in the
  payloads is unknown — parse tolerantly with `.get()` and treat `value: null` as an error.
- `bookingId` may arrive as an int; always convert with `str()` and send it as a string in V6 (parity).
- Timeouts: 20 s per call (the original used 10 s for GETs and none for writes).
- V5 and V6 are not idempotent: `http.request()` will retry them only on connect-phase errors and 429/503
  (03-foundations §6). A read timeout on V5 must **not** be retried — report it (a duplicate note is worse than
  a missing one, which a human can add).

## 5. Auth / token lifecycle (`VShipAuth(TokenProvider)`)

1. `get_token()`: under an `asyncio.Lock`, return `token_store.get("vship")["access_token"]` if `is_fresh(...)`.
2. Otherwise call V1 with the configured credentials (`http.request(..., idempotent=True, expected=(200,))`).
   - Non-200 → `AuthError(f"VShip login failed (HTTP {status})")` (include redacted body excerpt).
   - 200 but no `value.token` → `AuthError("VShip login succeeded but no token in response: <first 200 chars, redacted>")`.
3. `expires_at` = JWT `exp` if the token is a JWT (03-foundations §7), else `now + token_ttl_minutes*60`.
4. Store `{"access_token": token, "expires_at": expires_at, "obtained_at": now}` under `"vship"`.
5. `invalidate()` deletes the cached entry. `request_with_auth()` (03 §8) handles 401 → re-login → retry once.
   VShip may answer an expired token with 403 — treat **403 like 401** for VShip (`build` the client so
   `request_with_auth` re-authenticates once on either).
6. Never prompt interactively (the original popped Tk dialogs); missing/invalid credentials are a `ConfigError`/`AuthError`
   reported at startup by the pipeline's preflight (10-pipeline.md).

## 6. Module design

### 6.1 `vship/logic.py` (pure)

```python
def match_rank(candidate: str, booking_no: str) -> tuple[int, int, float]:
    cand = (candidate or "").strip().upper()
    wanted = booking_no.strip().upper()
    if cand == wanted: tier = 0
    elif cand.startswith(wanted): tier = 1
    elif wanted in cand: tier = 2
    else: tier = 3
    return tier, abs(len(cand) - len(wanted)), -SequenceMatcher(None, cand, wanted).ratio()

def best_match(records: list[dict], booking_no: str) -> dict | None:
    """min() by match_rank over records' 'bookingNo' (first wins on ties — parity).
    D17: return None when the best record's tier is 3 (no textual relation) or records is empty."""

@dataclass(frozen=True)
class NotifStatus: count: int; have_to_do: bool
def notif_status(comments: list[dict], run_now: datetime) -> NotifStatus      # 01-business-rules.md §11
def parse_vship_datetime(value: Any) -> datetime | None                      # 01 §11
def is_tr_swb_comment(text: str) -> bool                                     # 01 §12 (verbatim regexes)
def has_tr_swb_note(comments: list[dict]) -> bool                            # 01 §12
```

Ranking examples (wanted `EBKG17208602`):

| bookingNo candidates | Chosen | Why |
|---|---|---|
| `EBKG17208602_Copy_76099`, `EBKG17208602` | `EBKG17208602` | tier 0 beats tier 1 |
| `EBKG17208602_Copy_76099`, `EBKG17208602 HOLD` | `EBKG17208602 HOLD` | both tier 1; length diff 5 < 11 |
| `XEBKG17208602` | `XEBKG17208602` | tier 2 |
| `EBKG17208999` | none → `VShipBookingNotFoundError` | tier 3 (D17) |
| *(empty list)* | none → `VShipBookingNotFoundError` | original crashed with `ValueError` |

### 6.2 `vship/client.py`

```python
class VShipClient:
    def __init__(self, settings: VShipSettings, auth: VShipAuth, *,
                 http: httpx.AsyncClient | None = None, semaphore: asyncio.Semaphore | None = None,
                 read_only: bool = False): ...
        # read_only=True (dry run / probes): post_comment and patch_expected_arrival raise
        # RuntimeError("VShipClient is read-only (dry run / probe)") without any HTTP call.

    async def find_booking_id(self, base_booking: str) -> str:
        """V2 + best_match. Cached per instance (one run) in dict[str, asyncio.Future[str]] so concurrent
        callers for the same booking share one request. Not found → VShipBookingNotFoundError(
        'vship', f"No VShip booking matches {base_booking!r} (candidates: <up to 5 bookingNo values>)")."""

    async def get_booking(self, booking_id: str) -> dict[str, Any]:        # V3 → payload["value"]
    async def get_customer_name(self, base_booking: str) -> str:           # find_booking_id → V3 → value.customerName or ""
    async def get_internal_comments(self, booking_id: str) -> list[dict]:  # V4 → value.internalComments or []; cached per run
    async def post_comment(self, booking_id: str, text: str) -> None:      # V5; non-201 → NoteError; invalidates comments cache
    async def patch_expected_arrival(self, booking_id: str, eta: datetime) -> None:  # V6; non-204 → BookingChangeError
    async def aclose(self) -> None
```

Behaviour details:
- Every HTTP call: `async with self._sem:` then `request_with_auth(...)` with `service="vship"`.
- Payload shape errors (`value` missing/null, `data` not a list) → `ServiceError('vship', 'unexpected JSON structure: top-level keys=[...]')`
  (include only key names, never values).
- `post_comment`: wrap any `ServiceError`/`AuthError` into `NoteError` (chained). Log the comment text at INFO
  (`[VShip] Posting note for booking <base>: 'ARRIVED ON 11/03. NOTIF #1'`).
- `patch_expected_arrival`: wrap into `BookingChangeError`. Log `[VShip] VShip ETA 2026-11-16 for <base> done.` on success.
- Dry-run (10-pipeline.md) never calls V5/V6; V1–V4 are allowed in dry-run (read-only).

## 7. Business logic

Lives in 01-business-rules.md §6–§12; this client only fetches data. The pipeline:
1. `booking_id = await vship.find_booking_id(base)` (plan phase, for every row that reaches the notif check,
   the TR/SWB check or any VShip write).
2. `comments = await vship.get_internal_comments(booking_id)` → `notif_status(comments, run_now)` and,
   only when needed, `has_tr_swb_note(comments)` — same comments, one request.
3. Writes (V5/V6) in the apply phase, in the order given by 01 §6.3–6.5.

## 8. Fallbacks

None needed (official/owned API). If VShip is down: notif lookups follow `ETA_ON_NOTIF_LOOKUP_ERROR`
(01 §6.2); writes fail per 01 §6.6 and are reported for manual entry.

## 9. Fixtures & tests

Synthetic payloads (field names other than those in §4 are illustrative only):

`tests/fixtures/vship/login_ok.json`
```json
{"value": {"token": "eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjE3OTQwMDAwMDAsInN1YiI6InRlc3QifQ.c2lnbmF0dXJl", "userName": "ops@example.com"}, "isSuccess": true}
```
(payload segment decodes to `{"exp":1794000000,"sub":"test"}`)

`tests/fixtures/vship/login_no_token.json`
```json
{"value": {}, "isSuccess": false, "message": "Invalid credentials"}
```

`tests/fixtures/vship/search_multi.json`
```json
{"value": {"data": [
  {"bookingId": 90001, "bookingNo": "EBKG17208602_Copy_76099", "customerName": "ACME IMPORTS"},
  {"bookingId": 72892, "bookingNo": "EBKG17208602", "customerName": "ACME IMPORTS"},
  {"bookingId": 90002, "bookingNo": "XEBKG17208602", "customerName": "OTHER CO"}
], "totalCount": 3}}
```

`tests/fixtures/vship/search_empty.json`
```json
{"value": {"data": [], "totalCount": 0}}
```

`tests/fixtures/vship/booking_72892.json`
```json
{"value": {"bookingId": 72892, "bookingNo": "EBKG17208602", "customerName": "ACME IMPORTS", "expected_Arrival_Date": "2026-11-10T00:00:00"}}
```

`tests/fixtures/vship/comments_72892.json`
```json
{"value": {
  "internalComments": [
    {"commentId": 1, "comment": "ARRIVING ON 11/10.", "createdDate": "2026-10-20T15:02:11"},
    {"commentId": 2, "comment": "ARRIVED ON 11/03. NOTIF #1", "createdDate": "2026-11-03T16:45:00"},
    {"commentId": 3, "comment": "SWB RCVD 10/12", "createdDate": "2026-10-12T10:00:00"}
  ],
  "externalComments": []
}}
```

Unit tests (`tests/test_vship_logic.py`, `tests/test_vship_client.py`, respx-mocked):
1. `match_rank`/`best_match`: every row of the §6.1 table.
2. `notif_status`: no comments → (0, True); only `NOTIF #1` 2 days old → (1, False); only `NOTIF #1` 8 days old →
   (1, True); two `NOTIF #1` (20 days and 2 days old) → (1, False) (D15); `NOTIF #1` + `NOTIF #2` → (2, False);
   `NOTIF #2` only → (0, True) (O7); `internalComments: null` → (0, True); unparseable date → (1, True) + WARNING.
3. `has_tr_swb_note`: every golden case in 01 §12.
4. Login: success stores token with `expires_at` from JWT `exp`; non-JWT token uses TTL; missing token → `AuthError`.
5. 401 on V4 → one re-login → retry succeeds; second 401 → `AuthError`. Same for 403.
6. `find_booking_id` caching: two concurrent calls for the same booking → exactly one V2 request (respx call count).
7. `post_comment` 201 → ok; 400 → `NoteError` containing the body excerpt; ReadTimeout → `NoteError`, **no retry** (call count 1).
8. `patch_expected_arrival` sends `{"booking_Id": "72892", "expected_Arrival_Date": "2026-11-16T14:00:00"}` and accepts 204;
   200/500 → `BookingChangeError`.
9. Redaction: the password never appears in logs or exception strings (capture logs at DEBUG).

## 10. Probe / verification procedure

`eta probe vship --booking EBKG17208602` (read-only):
1. Logs in (or reuses the cached token) and prints the token expiry (not the token).
2. Prints the search candidates with their `match_rank`, the chosen `bookingId`, `customerName`.
3. Prints the internal comments (text + createdDate), the computed `NotifStatus` and the TR/SWB verdict.
4. Saves the redacted raw JSON to `<ETA_STATE_DIR>/probe/vship_<booking>.json`.
Writes (V5/V6) are verified with a real run limited to one test booking: `eta run test.csv --only <doc>`
after `--dry-run` shows the expected note text.

## 11. Sources & verification status

| Claim | Source | Status |
|---|---|---|
| Endpoints V1–V6, bodies, success codes 201/204, fields `value.token`, `value.data[].bookingId/bookingNo`, `value.customerName`, `value.internalComments[].comment/createdDate` | Original code: `VShip/syncSignInVShipCRM.py`, `bookingLookUp.py`, `customerLookUp.py`, `noteSearch.py`, `writeNotif.py`, `booking_changes.py` | OBSERVED (in production use) |
| Token lifetime ~1 h | Original cached for 1 h | INFERRED — prefer JWT `exp` |
| 403 means expired token | Original grouped 400/401/403 as "unauth problem" | INFERRED |
| Other payload fields in fixtures | — | UNVERIFIED (illustrative) |
