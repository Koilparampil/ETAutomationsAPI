# 03 — Foundations: shared contracts every module uses

This file is the **single source of truth** for package layout, shared types,
error classes, the HTTP layer, token caching, the carrier interface, logging and
settings conventions. Every other spec file uses the names defined here. If
another file appears to contradict this one, **this file wins** unless the
other file explicitly says "overrides 03-foundations §X".

---

## 1. Project layout (src layout, Python ≥ 3.12)

```
pyproject.toml
README.md                      # end-user README for the program (see 11-cli.md §8)
.env.example                   # generated from 02-configuration.md
src/eta_automations/
  __init__.py                  # __version__ = "1.0.0"
  __main__.py                  # `python -m eta_automations` → cli.main()
  cli.py                       # argparse CLI (11-cli.md)
  config.py                    # Settings + load_settings() (02-configuration.md)
  clock.py                     # Clock / SystemClock / FixedClock (§3)
  models.py                    # Carrier, EtaResult (§4) + pipeline models (10-pipeline.md)
  errors.py                    # exception hierarchy (§5)
  http.py                      # httpx client factory, request(), retries, rate limiter (§6)
  token_store.py               # TokenStore (§7)
  auth.py                      # TokenProvider protocol + request_with_auth() (§8)
  logging_setup.py             # logging config + redact() (§11)
  business_days.py             # 6-business-day window (01-business-rules.md §4)
  rules.py                     # pure decision functions (01-business-rules.md)
  texts.py                     # email subject + VShip note text (01-business-rules.md §7–8)
  outputs.py                   # Send2*.txt / Failed_To_Process.txt writers (10-pipeline.md)
  ledger.py                    # idempotency ledger (10-pipeline.md)
  run_lock.py                  # single-instance OS file lock (§14)
  pipeline.py                  # plan → confirm → apply orchestration (10-pipeline.md)
  qbo/
    __init__.py
    auth.py                    # OAuth2 refresh-token flow (04-quickbooks-online.md)
    client.py                  # QboClient (04-quickbooks-online.md)
    email.py                   # invoice e-mail delivery strategy (04-quickbooks-online.md)
  vship/
    __init__.py
    client.py                  # VShipClient (05-vship-crm.md)
    logic.py                   # notif status, TR/SWB matcher, booking best-match (05-vship-crm.md)
  carriers/
    __init__.py
    base.py                    # CarrierClient protocol (§9)
    router.py                  # booking number → Carrier (01-business-rules.md §3)
    dcsa.py                    # shared DCSA Track & Trace parser (09-dcsa-aggregators-grimaldi.md)
    msc.py                     # (06-carrier-msc.md)
    maersk.py                  # (07-carrier-maersk.md)
    cma.py                     # (08-carrier-cma-cgm.md)
    grimaldi.py                # (09-dcsa-aggregators-grimaldi.md)
    aggregator.py              # optional third-party provider (09-dcsa-aggregators-grimaldi.md)
tests/
  conftest.py
  fixtures/<service>/*.json    # sample payloads given in the spec files
  test_*.py                    # see 12-testing.md
```

Rules:
- No browser automation of any kind (no Playwright, Selenium, Puppeteer,
  pyppeteer, undetected-chromedriver, requests-html JS rendering, etc.).
- No module performs network I/O at import time. No module reads `.env` at
  import time — only `config.load_settings()` does, called once by the CLI.
- Everything that talks to the network is `async` and uses `httpx.AsyncClient`.

## 2. Dependencies (`pyproject.toml`)

Runtime (minimum versions; do not pin exact patch versions):

| Package | Why |
|---|---|
| `httpx>=0.28,<1` | All HTTP (async). |
| `python-dotenv>=1.0` | `.env` loading and writing back rotated values. |
| `pandas>=2.2` | Only for `CustomBusinessDay` + `USFederalHolidayCalendar` in `business_days.py` (exact parity with the original). Import it lazily inside that module. |
| `tzdata>=2024.1` | IANA time-zone data for `zoneinfo` (Windows has none built in); used by `dcsa.to_local_naive()` (09 §A.3.3). |

Optional extras (declared as `[project.optional-dependencies]`): any extra an
integration spec asks for (e.g. an e-mail backend in 04). Code must import
optional packages lazily and raise `ConfigError` with an install hint when the
feature is enabled but the package is missing.

Dev extras (`dev`): `pytest>=8`, `pytest-asyncio>=0.24`, `respx>=0.22`.
`pytest-asyncio` runs in `asyncio_mode = "auto"` (set in `pyproject.toml`).

Entry point: `[project.scripts] eta = "eta_automations.cli:main"`.

## 3. Clock (`clock.py`)

```python
class Clock(Protocol):
    def now(self) -> datetime: ...          # naive local wall-clock time

class SystemClock:                           # now() -> datetime.now()
class FixedClock:                            # FixedClock(datetime(2026, 11, 5, 9, 30)).now() -> that value
```

The pipeline captures `run_now = clock.now()` **once** at the start of a run
and uses that single value for every time comparison in the run (window
cutoff, "has it arrived", "Notif #1 within 7 days", MSC transshipment rule).
`today = run_now.date()`. Nothing else in the code calls `datetime.now()`
except `SystemClock` and log/report timestamps.

## 4. Shared models (`models.py`)

```python
class Carrier(StrEnum):
    MSC = "MSC"
    MAERSK = "Maersk"
    CMA = "CMA"
    GRIMALDI = "Grimaldi"

@dataclass(frozen=True, slots=True)
class EtaResult:
    carrier: Carrier
    booking: str                 # the base booking number that was queried (no suffixes)
    eta: datetime                # NAIVE datetime: carrier-reported local wall-clock time at the
                                 # port/terminal (any tz offset is parsed, then DROPPED, keeping the
                                 # local wall time — this is what the carrier website displays and
                                 # what the original code compared against). Date-only sources use 00:00.
    source: str                  # short human description, e.g. "MSC event 'Estimated Time of Arrival'",
                                 # "DCSA TRANSPORT ARRI/EST at USSAV"
    is_actual: bool = False      # True when the source says the arrival/discharge already happened
    from_transshipment_rule: bool = False   # MSC fallback rule used (06-carrier-msc.md)
    containers: tuple[str, ...] = ()        # container numbers considered, if known
    details: Mapping[str, Any] = field(default_factory=dict)  # small JSON-serializable extras for the report
```

Pipeline-specific models (`InputRow`, `ParsedBooking`, `Route`, `Plan`,
`Action`, …) are defined in `10-pipeline.md`.

## 5. Errors (`errors.py`)

```python
class EtaAutomationError(Exception): ...            # base for everything below

class ConfigError(EtaAutomationError): ...          # missing/invalid settings, missing optional package
class AuthError(EtaAutomationError): ...            # credentials rejected / token unobtainable

class ServiceError(EtaAutomationError):
    """Unexpected HTTP status or malformed payload from an external service."""
    def __init__(self, service: str, message: str, *, status: int | None = None,
                 body_excerpt: str = "", request_id: str | None = None): ...
    # str(err) -> f"{service}: {message} (HTTP {status}) [request_id=...] body: {excerpt}"
    # body_excerpt is <= 500 chars and already passed through redact()

# ── carriers ──
class CarrierLookupError(EtaAutomationError): ...   # lookup failed: network, blocked, not found at carrier,
                                                    # carrier not configured → "Carrier Lookup Failed."
class UnsupportedCarrierError(CarrierLookupError): ...  # Grimaldi stub, reference numbers, unknown pattern
class AggregatorBookingError(CarrierLookupError): ...   # per-booking aggregator state, e.g. "not tracked yet — re-run later"
                                                        # (09 §B.7.1 describes it; define it HERE and import it in aggregator.py)
class NoEtaError(EtaAutomationError): ...           # booking found but no usable ETA → "Can't determine ETA."
class CustomsHoldError(EtaAutomationError): ...     # MSC heuristic → PotentialCustomsHold.txt

# ── VShip ──
class VShipBookingNotFoundError(ServiceError): ...
class NoteError(ServiceError): ...                  # posting a VShip comment failed
class BookingChangeError(ServiceError): ...         # PATCHing the VShip booking ETA failed

# ── QuickBooks ──
class InvoiceNotFoundError(EtaAutomationError): ...
class AmbiguousInvoiceError(InvoiceNotFoundError): ...     # several invoices share one DocNumber (04 §6.3)
class InvoiceUpdateError(ServiceError): ...
class InvoiceSendError(ServiceError): ...
class EmailPathUnavailableError(InvoiceSendError): ...     # no invoice e-mail can be sent this run (04b §6.1)
class TemplateRestoreError(EmailPathUnavailableError): ... # QBO e-mail template left modified (04b §6.2)

# ── single-instance guard ──
class RunLockedError(EtaAutomationError): ...              # another instance holds the run lock (§14)

# ── business routing signal (not a failure) ──
class SWBTRError(EtaAutomationError): ...           # partially paid + TR/SWB note → Send2Accounting.txt
                                                    # (kept for parity/readability; the new pipeline decides the
                                                    # ACCOUNTING route while planning and need not raise it)
```

**Carrier boundary rule:** `CarrierClient.get_eta()` may raise **only**
`CarrierLookupError` (or a subclass), `NoEtaError`, or `CustomsHoldError`.
Carrier clients must catch `ServiceError`, `AuthError`, `httpx.HTTPError`,
`KeyError`/`TypeError`/`ValueError` from parsing, etc. and re-raise them as
`CarrierLookupError(...)` (or `NoEtaError` when the payload is valid but has no
usable date), chaining with `raise ... from exc`. Anything else escaping
`get_eta()` is a bug; the pipeline still catches it and records
"Unexpected error during carrier lookup." (parity with the original).

## 6. HTTP layer (`http.py`)

```python
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)

def new_async_client(*, base_url: str = "", headers: Mapping[str, str] | None = None,
                     timeout: httpx.Timeout | float = DEFAULT_TIMEOUT) -> httpx.AsyncClient:
    """follow_redirects=False, trust_env=True (honours HTTPS_PROXY / SSL_CERT_FILE),
    default headers: {"User-Agent": f"ETAutomationsAPI/{__version__}", "Accept": "application/json"}
    merged with `headers`."""

async def request(client: httpx.AsyncClient, method: str, url: str, *, service: str,
                  expected: Collection[int] = (200,), idempotent: bool | None = None,
                  max_attempts: int = 4, limiter: "AsyncLimiter | None" = None,
                  **kwargs) -> httpx.Response:
    ...

def json_body(resp: httpx.Response, *, service: str) -> Any:
    """resp.json() or raise ServiceError(service, 'response was not valid JSON', status=..., body_excerpt=...)."""
```

`request()` behaviour:
1. `idempotent` defaults to `True` for GET/HEAD/OPTIONS/PUT/DELETE and `False` for POST/PATCH.
   Callers may pass `idempotent=True` for POSTs that are pure reads (e.g. GraphQL queries, QBO query endpoint, OAuth token calls).
2. Attempt loop (`max_attempts` total). Retry when:
   - `httpx.ConnectError`, `httpx.ConnectTimeout`, `httpx.PoolTimeout` — always (request never reached the server);
   - `httpx.ReadTimeout`, `httpx.WriteTimeout`, `httpx.RemoteProtocolError` — only if idempotent;
   - status `429` or `503` — always (server explicitly refused, nothing was processed);
   - status `500`, `502`, `504` — only if idempotent.
   Backoff before attempt *n* (n ≥ 2): `min(30, 0.5 * 2**(n-2)) + random.uniform(0, 0.25)` seconds,
   or the `Retry-After` header value (seconds or HTTP-date) if present, capped at 30 s.
3. If the final status is in `expected` → return the response.
4. Otherwise raise `ServiceError(service, f"unexpected HTTP {status} for {method} {redacted_path}", status=..., body_excerpt=redact(resp.text)[:500], request_id=<first present of headers 'intuit_tid', 'x-request-id', 'x-correlation-id', 'x-correlationid', 'x-kong-request-id', 'x-azure-ref', 'request-id'>)`.
   **401 and 403 are never retried here** — see `request_with_auth()` in §8.
5. Transport errors that exhaust the attempts are re-raised as
   `ServiceError(service, f"network error: {exc!r}")` chained from the original.
6. Every attempt logs at DEBUG on logger `eta.http`: method, redacted URL, status (or exception), elapsed ms, attempt number.
7. If `limiter` is given, `async with limiter:` wraps each attempt (see §10).

## 7. Token store (`token_store.py`)

```python
class TokenStore:
    def __init__(self, path: Path): ...                  # e.g. <ETA_STATE_DIR>/tokens.json
    def get(self, key: str) -> dict[str, Any] | None: ...
    def put(self, key: str, value: dict[str, Any]) -> None: ...   # atomic: write tmp file in same dir,
                                                                  # flush+fsync, os.replace; chmod 0o600 on POSIX
    def delete(self, key: str) -> None: ...

def is_fresh(entry: Mapping[str, Any] | None, *, skew_seconds: int = 120, now: float | None = None) -> bool:
    """True if entry has 'access_token' and 'expires_at' (epoch seconds) > now + skew."""

def jwt_exp(token: str) -> float | None:
    """If `token` looks like a JWT (three dot-separated segments), base64url-decode the payload segment
    (padding added as needed, NO signature verification) and return its numeric `exp` claim as epoch
    seconds; otherwise, or on any decoding error, return None. Shared by every auth provider."""
```

- One JSON object, top-level keys = service names: `"qbo"`, `"vship"`, `"msc"`, `"maersk"`, `"cma"`, `"aggregator"`, …
- Each value has at least `access_token: str` and `expires_at: float` (Unix epoch seconds). Services may add
  keys (e.g. QBO stores `refresh_token`, `refresh_token_expires_at`, `refresh_token_seed`).
- Corrupt/unreadable file → log a WARNING, treat as empty, and overwrite on next `put()`.
- Guard reads/writes with a `threading.Lock` (writes are synchronous and tiny).
- When a JWT is received and no explicit lifetime is given, derive `expires_at` from the JWT `exp` claim
  (base64url-decode the payload segment; **no signature verification**); if that fails, use the
  service-specific default lifetime documented in that service's spec.

## 8. Auth pattern (`auth.py`)

```python
class TokenProvider(Protocol):
    async def get_token(self) -> str: ...      # cached-if-fresh, else obtain/refresh (under an asyncio.Lock)
    async def invalidate(self) -> None: ...    # drop the cached access token (keep refresh tokens)

async def request_with_auth(client: httpx.AsyncClient, method: str, url: str, *, service: str,
                            auth: TokenProvider,
                            build_headers: Callable[[str], Mapping[str, str]] = lambda t: {"Authorization": f"Bearer {t}"},
                            **kwargs) -> httpx.Response:
    """token = await auth.get_token(); call request(...) with merged headers.
    If it raises ServiceError with status 401 (or 403 when the service spec says 403 means 'token expired'):
    await auth.invalidate(); get a fresh token; retry exactly once. A second 401/403 raises
    AuthError(f"{service}: credentials/token rejected (HTTP {status})")."""
```

Each service implements its own `TokenProvider` (`QboAuth`, `VShipAuth`, `MscAuth`, `MaerskAuth`, …).
Concurrency rule: `get_token()` takes an `asyncio.Lock`, re-checks freshness inside the lock, and only
then performs the network call — so 20 concurrent coroutines trigger one login, not 20.
**A token provider never acquires its service's concurrency semaphore** (§10): callers may already hold a
slot while awaiting the token, so acquiring it again could deadlock. Token calls go straight through
`http.request()`.

## 9. Carrier client contract (`carriers/base.py`)

```python
class CarrierClient(Protocol):
    carrier: Carrier
    async def get_eta(self, booking: str) -> EtaResult: ...
        # `booking` is the BASE booking number (suffixes already removed, stripped, upper-cased).
        # Never returns None: raise NoEtaError instead.
        # Raises only CarrierLookupError / NoEtaError / CustomsHoldError (see §5 boundary rule).
    async def probe(self, booking: str) -> dict[str, Any]: ...
        # Diagnostics for the `eta probe <carrier>` command: performs the same calls as get_eta but
        # returns {"requests": [...summaries...], "raw": <payload(s), redacted>, "eta": <EtaResult as dict or error string>}.
    async def aclose(self) -> None: ...
```

Each carrier module exposes a factory:

```python
def build_client(settings: Settings, token_store: TokenStore, *,
                 clock: Clock | None = None, http: httpx.AsyncClient | None = None) -> CarrierClient | None:
    """Return None when the carrier is not configured (required env vars missing).
    `clock` and `http` are passed through to the client constructor: the pipeline passes
    FixedClock(run_now) so every carrier rule uses the run's single timestamp; tests pass a respx-mocked
    client. (Specs 07/08 show the shorter two-argument form — the keyword arguments here are additive.)"""
```

Constructors accept an optional `http: httpx.AsyncClient | None = None` (tests inject a respx-mocked
client) and an optional `clock: Clock`. `carriers/router.py` maps booking numbers to `Carrier`
(01-business-rules.md §3) and holds `dict[Carrier, CarrierClient | None]`; a booking whose carrier
client is `None` fails with `CarrierLookupError(f"{carrier} lookups are not configured — set {vars} in .env")`.

## 10. Concurrency & rate limiting

- One `asyncio.Semaphore` per service, sized from settings (defaults in 02-configuration.md):
  QBO 4, VShip 4, MSC 4, Maersk 3, CMA 2, aggregator 2. A client acquires its semaphore around **each
  HTTP call** (not around a whole booking), so one slow booking never blocks others.
- `http.AsyncLimiter(max_calls: int, period_s: float)` — a small sliding-window limiter usable as
  `async with limiter:`. Use it only where a spec documents a hard per-minute/per-second quota.
- The pipeline's overall fan-out is bounded by `ETA_MAX_PARALLEL_BOOKINGS` (default 8).

## 11. Logging & redaction (`logging_setup.py`)

- `configure_logging(level: str = "INFO", log_file: Path | None = None)`: console handler +
  optional file handler (`<ETA_OUTPUT_DIR>/logs/eta_<YYYYmmdd_HHMMSS>.log`, DEBUG level).
  Format: `"%(asctime)s %(levelname)-7s %(name)s: %(message)s"`, `datefmt="%H:%M:%S"`.
- Logger names: `eta`, `eta.http`, `eta.pipeline`, `eta.qbo`, `eta.vship`, `eta.carrier.msc`,
  `eta.carrier.maersk`, `eta.carrier.cma`, `eta.carrier.dcsa`, `eta.carrier.aggregator`.
- `redact(value)` works on str / Mapping / list (recursively) and returns a redacted copy:
  any key matching `(?i)(authorization|token|secret|password|passwd|cookie|consumer-key|keyid|api[-_]?key|client[-_]?secret|refresh)`
  has its value replaced by `"***"`; in strings, `Bearer <x>` → `Bearer ***`, and URL query parameters
  with those names → `***`. **Never log a secret, token, password or full cookie.**
- Console output must be readable on Windows: the CLI reconfigures `sys.stdout`/`sys.stderr` to
  UTF-8 with `errors="replace"`.

## 12. Settings access pattern (`config.py`)

`Settings` is a frozen dataclass composed of per-service frozen dataclasses:

```python
@dataclass(frozen=True)
class Settings:
    run: RunSettings                     # thresholds, dirs, concurrency, providers (02)
    qbo: QboSettings | None              # (04) None when its required vars are missing
    vship: VShipSettings | None          # (05)
    msc: MscSettings | None              # (06)
    maersk: MaerskSettings | None        # (07)
    cma: CmaSettings | None              # (08)
    aggregator: AggregatorSettings | None  # (09)
    email: EmailSettings | None          # (04) only if 04 defines an e-mail backend that needs it
    missing: Mapping[str, tuple[str, ...]]  # service name -> names of its missing required env vars

def require(settings: Settings, *services: str) -> None:
    """Raise ConfigError listing, for every requested service that is None, all of its missing env vars,
    e.g. require(s, "qbo", "vship") at the start of `eta run`."""
```

- `load_settings(env_file: Path | None = None) -> Settings` calls
  `dotenv.load_dotenv(env_file or Path.cwd() / ".env", override=True)` (the original deliberately lets
  `.env` win over OS variables), then reads `os.environ`. It never raises for a missing service; it
  raises `ConfigError` only for **malformed** values (e.g. a non-integer concurrency).
- Each integration spec defines its settings dataclass fields **and** the exact env var names in a
  table (name · required? · default · meaning). 02-configuration.md consolidates them.
- Commands call `require(...)` for what they need: `run` → qbo + vship (+ at least the carriers present
  in the file, checked lazily by the router); `probe qbo` → qbo; `eta eta` → only the carriers involved.
- Booleans accept `1/true/yes/on` and `0/false/no/off` (case-insensitive).

## 13. Conventions for integration spec files (04–09)

Every integration spec uses this outline, in this order:

1. **Summary** — what it replaces in the original code and why it is faster.
2. **Access & onboarding** — accounts/products/keys the user must obtain, who to ask, expected lead time.
3. **Configuration** — settings dataclass + env var table.
4. **Endpoints** — method, URL, headers, params/body, relevant response fields, status codes, errors, rate limits.
5. **Auth / token lifecycle.**
6. **Module design** — file path, classes, exact function signatures with type hints, step-by-step behaviour, error mapping to §5.
7. **Business logic** — ported precisely from the original, with any deviation flagged as **DEVIATION** and justified.
8. **Fallbacks** — honest assessment of alternatives if the primary path is unavailable.
9. **Fixtures & tests** — synthetic sample payloads (in code blocks, each with its `tests/fixtures/...` path) and the list of unit tests.
10. **Probe / verification procedure** — how the user safely confirms it works with real credentials.
11. **Sources & verification status** — table: claim · source URL · status (`VERIFIED-DOCS`, `OBSERVED`, `INFERRED`, `UNVERIFIED`).

Anything not verifiable from public documentation must be marked `UNVERIFIED`, and the code must then be
written defensively (tolerant parsing, clear error messages that include the redacted raw payload keys)
and covered by the probe procedure so the user can confirm it on first run.

## 14. Single-instance run lock (`run_lock.py`)

Two concurrent runs could double-send e-mails and race QuickBooks refresh-token rotation (04 §5.3), so
every command that writes (`run` without `--dry-run`, `qbo-auth`, write probes) holds an exclusive lock:

```python
@contextmanager
def run_lock(state_dir: Path) -> Iterator[None]:
    """Open/create <state_dir>/run.lock and take an exclusive, NON-blocking OS lock on it for the
    lifetime of the process: fcntl.flock(fd, LOCK_EX | LOCK_NB) on POSIX,
    msvcrt.locking(fd, LK_NBLCK, 1) on Windows (lock byte 0; write a placeholder byte first if the file is empty).
    On contention raise RunLockedError("Another ETAutomationsAPI run is in progress (lock: <path>).").
    Write "pid=<pid> started=<iso time>" into the file for humans; release and close on exit.
    The OS drops the lock automatically if the process dies, so there is no stale-lock handling and
    NO PID probing (on Windows os.kill(pid, 0) would terminate the process)."""
```

Read-only commands (`--dry-run`, `eta eta`, read-only probes) do not take the lock.
