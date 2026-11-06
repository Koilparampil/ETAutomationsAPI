# Implementation notes

How the program in `src/` interprets or departs from the specification in `spec/`, which files were changed
outside their own build stage, what is still open, and the checklist of unverified items to confirm with
`eta probe …` before the first live run.

The program was built in stages, following `BUILD_PROMPT.md`:

| Stage | Scope |
|---|---|
| A | Foundations: config, errors, clock, models, logging, http, token store, auth, run lock, test harness |
| B | Pure rules (`business_days`, `rules`, `texts`, `vship/logic`) and the VShipCRM client |
| D | QuickBooks Online: `qbo/auth.py`, `qbo/client.py`, `qbo/email.py` |
| E1–E6 | Carriers: DCSA parser, MSC, Maersk, CMA CGM, aggregator + Grimaldi, router |
| F | `outputs.py`, `ledger.py`, `pipeline.py` |
| G | `cli.py` (`ui.py` and `scripts/run_eta.bat` were reviewed) |
| INT | Integration: contract checks, cross-stage fixes, `test_contracts.py`, `test_no_browser.py`, `test_live_smoke.py` |
| H | `README.md`, `.env.example`, this file |

Test status at the end of the build (Python 3.13.18):
`python -m pytest -q` → **1553 passed, 8 skipped**. The skips are the 7 opt-in live smoke tests (12 §5,
`ETA_LIVE_TESTS=1`) and one MSC test that needs a German locale. The tests never touch the network.

Precedence used for conflicts (from `BUILD_PROMPT.md`): 03-foundations > 01-business-rules > 04–09 (their test
tables are acceptance tests) > 10-pipeline > the rest. Where a genuine contradiction remained, the safer behaviour
was chosen (never send an e-mail, never write to QuickBooks or VShip when unsure). Those choices are marked
**safer** below.

---

## A. Deviations and assumptions, by module

### Test harness and test layout (`tests/`)

- **Network guard.** 12 §1 suggests patching `httpx.AsyncClient.send`. `tests/conftest.py` guards at socket
  level instead (`connect`, `connect_ex`, `create_connection` and `getaddrinfo`), so DNS lookups are blocked
  too. An un-mocked request raises `NetworkAccessBlocked` and fails loudly; it cannot turn into a retried
  `ServiceError` that a test might accept by mistake. Every `*_proxy` variable is removed per test, because a
  loopback proxy would otherwise reach the internet. Loopback and AF_UNIX stay allowed.
- `conftest.py` restores `os.environ` after every test (`load_dotenv(override=True)` mutates it) and points
  `XDG_STATE_HOME` / `LOCALAPPDATA` at a per-test temporary folder, so no test can touch the real state folder.
  Tests marked `live` are skipped unless `ETA_LIVE_TESTS=1`, and the guard is off for them (12 §5).
- **Test file names** follow the integration specs where 12 §2 differs: `tests/test_msc.py` and
  `tests/test_maersk.py` (06 §9, 07 §9.3) instead of `test_carrier_msc.py` / `test_carrier_maersk.py`; CMA is
  `tests/test_carrier_cma.py`. The 01 §4 router-table cases are in `tests/test_router.py`, not `test_rules.py`.
- **Router case `2675466270`.** 12 §3 calls it a reference number, but it does not match `20\d{8}`. 01 §4 takes
  precedence, so the expected result is the no-pattern text
  (`No carrier pattern matched booking 2675466270 — process manually`); the row outcome is the same.
  `2026546627` covers the reference-number text.
- **Fixtures.** MSC and CMA fixtures were extracted byte-for-byte from the spec code blocks by script.
  `tests/fixtures/msc/api_sandbox_trimmed.json` (described only in prose) holds events 1, 3 and 6 of MSC's public
  sandbox, fetched once outside the tests. Maersk fixtures that 07 §9.1 describes only in prose were written as
  synthetic JSON that satisfies every 07 §9.2 value. CMA `events_page1/2.json` are split from
  `events_in_transit.json`, and a test asserts that they add up to it.
- CMA test 20 (shared 429 gate) runs in real asyncio time slowed down 50 times, so the ordering is deterministic.
- The live smoke tests read test-only variables, not program settings: `ETA_LIVE_ENV_FILE` and
  `ETA_LIVE_<VSHIP|MSC|MAERSK|CMA>_BOOKING`. They run only read-only probes; the billable CMA probe runs only when
  `ETA_LIVE_CMA_BOOKING` is set.

### `config.py` (02, 03 §12)

- Secret fields are `repr=False`: QBO client secret and refresh token, VShip password, MSC manual token and API
  key, Maersk client id (it is the Consumer-Key) and secret, CMA API key and client secret, aggregator token,
  Graph client secret, SMTP password. `MscSettings` / `CmaSettings` fields have defaults equal to the env-var
  defaults; every `Settings` field except `run` defaults to `None`.
- Every value is validated even when its service is not configured: a malformed value is always a `ConfigError`
  (exit 2). URLs must be http(s) (https for CMA). Trailing `/` is removed from the VShip, MSC API, Maersk, CMA
  and aggregator base URLs. Empty or whitespace-only values count as unset. Values the spec does not bound get
  conservative limits, for example: concurrency and page sizes ≥ 1; waits and caps ≥ 0; timeouts > 0 (Maersk
  ≥ 1 s); rate limits ≥ 0 where 0 means "no limit" (MSC, Maersk), otherwise ≥ 1; `ETA_WINDOW_BUSINESS_DAYS` ≥ 0;
  `ETA_MIN_INVOICE_AMOUNT` finite and ≥ 0 (`$` and `,` allowed); `ETA_LOG_LEVEL` a known level;
  `EMAIL_SMTP_PORT` 1–65535.
- `VSHIP_PASSWORD` and `EMAIL_SMTP_PASSWORD` are used exactly as typed (parity with the original's `os.getenv`);
  every other value is stripped. A leading `Bearer ` is removed from `MSC_ACCESS_TOKEN`.
- `load_settings(env_file=None, *, env=None)`: with `env`, only that mapping is read. Otherwise
  `load_dotenv(path, override=True, encoding="utf-8-sig")` (tolerates a BOM written by Windows editors). An
  explicit `--env-file` that does not exist raises `Settings file not found: …`; a missing `./.env` is fine.
- `load_settings` logs the obsolete-variable notice itself: the exact 04 §3 INFO line for `QB` / `QB_HEADLESS`,
  one DEBUG line for `MSC_PASSWORD`, `LOGIN_URL`, `HOME_PAGE`.
- `EmailSettings` is built only for `graph` and `smtp`; with `template_swap`, `Settings.email` is `None` and
  nothing is reported missing. `EMAIL_BCC` is split on `,` and `;` and de-duplicated case-insensitively.

### Foundations: `errors.py`, `http.py`, `logging_setup.py`, `run_lock.py`, `__main__.py` (03)

- `http.request()`: a status listed in `expected` is final and never retried (03 §6 is silent; CMA needs it for
  `expected=(200, 404, 429)`). Other statuses follow the 03 §6 retry table.
- `ServiceError` also accepts a single argument, which becomes the whole message (04/04b raise e.g.
  `EmailPathUnavailableError("not verified")`, and P33 expects exactly that text). Absent parts (status,
  request id, body) are left out of `str()`. `body_excerpt` is redacted again and capped at 500 characters.
- `redact()` masks more than the 03 §11 key pattern: `subscription[-_]?key` (MSC's `Ocp-Apim-Subscription-Key`),
  quoted JSON/repr pairs (also when cut off by truncation), `key=value` pairs, header-style lines,
  Authorization scheme credentials and URL `user:password`. Under a secret key, `None` and booleans are kept
  (e.g. `has_token: True`). Masking a few non-secrets (e.g. `token_type`) is accepted. Integration change:
  `bearer` directly after `/` (QuickBooks' `/oauth2/v1/tokens/bearer` URL) is no longer treated as an auth
  scheme; real credentials are still masked.
- `new_async_client`: a float timeout means read/write/pool with connect `min(10, t)`; default headers are
  merged case-insensitively.
- `run_lock` creates the state folder (mode 0700). Only lock contention becomes `RunLockedError`; other
  `OSError`s propagate. On Windows the `pid=… started=…` line is written after the locked placeholder byte.
- `__main__.py` wraps `raise SystemExit(main())` in `if __name__ == "__main__":`, so importing it never runs the
  CLI.

### Business rules: `rules.py`, `texts.py`, `business_days.py` (01)

- **D1 text** (not specified): an empty DocNumber with a valid amount gives
  `missing invoice number (first column is empty)`. When the amount is also bad, the amount error wins (checked
  first, as in the original).
- Amount parsing runs `float(cleaned)` as a parity check before `Decimal(cleaned)`, because `Decimal` accepts
  `_1`, `1_`, `1__0` and `sNaN`, which the original rejected. Rejected rows get exactly
  `could not convert string to float: <repr(cleaned)>`.
- **Safer:** NaN/Infinity amounts are MALFORMED (`amount is not a finite number: '<cleaned>'`); the original's
  `float()` accepted them.
- **D3 duplicates** are compared case-insensitively (the QBO lookup has a case-insensitive fallback, so `EBKG1`
  and `ebkg1` could be the same invoice and be e-mailed twice). MALFORMED rows are left out of duplicate
  detection. `(line N)` is the duplicate row's own line.
- 01 §3 rule 1 ("last is a digit") uses ASCII 0-9 as the table says; `with_arrival_suffix` keeps the verbatim
  `str.isdigit()` of 01 §9 (identical for ASCII). `with_arrival_suffix('')` raises `ValueError`.
- **D2 header:** only the row with line 1 can be a header; a missing or empty amount counts as non-numeric.
  `drop_header_row()` logs the INFO line itself (the pipeline does not repeat it).
- `texts.py` also holds the fixed log/console strings of 01 §6, §6.2, §6.6 and §13, the 10 §2c BillEmail reason,
  the D11 revert notes and the `--no-email` message.

### VShipCRM: `vship/client.py`, `vship/logic.py` (05, 01 §11–§12)

- **Safer:** `best_match()` returns `None` for an empty booking, and `find_booking_id()` never searches for one
  (otherwise every candidate "contains" the empty string and an arbitrary booking could be edited, e.g. for a
  DocNumber like `AA`).
- Login network failures raise `AuthError` (`VShip login failed: network error: …`) so the preflight sees one
  error type. The password is masked in every error text, even when echoed, JSON-escaped or cut off; the masked
  error is re-created outside the `except` block so the unmasked original is never attached as `__context__`.
- Structure errors name key names only, never values. `VShipBookingNotFoundError` lists up to 5 `bookingNo`
  values. The booking-id cache keeps failures too (one lookup per base booking per run, D18).
- `post_comment` clears the cached comments even when the POST fails (a timed-out POST may still have created
  the note). The read-only client raises before any logging or HTTP call.
- `notif_status`: an unreadable `createdDate` logs a WARNING and counts as old, but does not hide a newer
  readable note. The 7-day check is strictly `newest > run_now - 7 days`.
- Additions: `build_vship()`, `VShipClient.search_bookings()`, `VShipClient.probe()` (05 §10),
  `VShipAuth.token_info()`.

### QuickBooks: `qbo/auth.py` (04 §5)

- `refresh_token_seed` holds the `.env` value at the time the entry was written, so a failed `.env` write-back
  does not make the old value look like a newly pasted token. In `eta qbo-auth` the seed is the `.env` value
  from before the flow.
- `eta qbo-auth`: the realmId-vs-`QB_REALM_ID` check runs **before** the code exchange (the spec puts it after),
  so no code is exchanged for the wrong company. The single-use code exchange is not retried. A failing
  CompanyInfo read after connecting is only a warning. With `QB_REALM_ID` empty, the realm id is printed and
  written to `.env`.
- `invalidate()` writes `expires_at=0` only when there is an access token. A `.env` write-back failure is a
  WARNING (`os.environ` is updated anyway); an existing UTF-8 BOM in `.env` is kept.
- Any token-response field whose name contains `expir` (besides `expires_in` and `x_refresh_token_expires_in`)
  is stored under `response_expiry_fields` in the `qbo` token entry; only key names are logged.

### QuickBooks: `qbo/client.py` (04 §6–§8)

- HTTP 403 becomes `AuthError("QuickBooks denied access to realm … (code …) [request_id=…]")` without a retry.
- `update_invoice` wraps other `ServiceError`s (network, 5xx after retries, malformed answers) as
  `InvoiceUpdateError`. A 6140 error names the DocNumber. Verification re-reads with the field definition used
  for the write.
- `_post_send` returns the full JSON answer; `get_preferences`, `update_preferences` and `get_company_info`
  return the unwrapped object. Reads and Preferences calls raise `QboFaultError(ServiceError)` with `.faults`
  and `.codes` (defined in `qbo/client.py` because `errors.py` belongs to stage A).
- `InvoiceSnapshot.recipients` removes case-insensitive duplicates. If a case-insensitive lookup finds more than
  one match, the DocNumber is treated as absent (WARNING), not ambiguous. A 200 answer containing a Fault
  triggers the same one-by-one fallback as a 400.
- `parse_qbo_date` accepts exactly the spec's formats; `doc_number_problem` checks length only.
- `send_invoice` validates subject and BillEmail (04b §6.1) before the preflight, so an invalid row never causes
  an HTTP call. `prepare_for_sending` on a read-only client always runs the dry-run variant.
- **Clock:** `build_qbo` keeps the spec signature without a `clock`, so QBO auth, e-mail strategies, send
  receipts and outcome inference use the real wall clock (token expiry must use real time; a frozen `run_now`
  would never expire a token in a long run). The ledger's timestamps do the same. Every business-rule time
  comparison still uses the run's single `run_now`.

### QuickBooks e-mail: `qbo/email.py` (04b)

- **Safer verification gate:** the **newest** record in `qbo_email_probe.json` for this realm and strategy must
  have `"verified": true`, so a later failed `--send-test` revokes an earlier success. The file is read as a JSON
  array, a single object or JSON lines.
- A dry-run preflight with a recovery file present judges the template by the original subject stored in the
  recovery file; otherwise every dry run after a crash would be blocked although the real run repairs the
  template. A dry run does not keep the Preferences it read.
- Template restore makes 3 attempts with 1 s and 2 s pauses (the spec's 4 s pause of "1, 2, 4" cannot be reached
  with 3 attempts).
- If the restore fails **after a successful send**, `TemplateRestoreError` is raised as the spec requires, but
  its message says the e-mail WAS sent and it carries `.receipt` (see `pipeline.py`).
- If a stale-object (5010) re-read during the swap shows that a person changed the template subject, that subject
  is kept, restored and written to the recovery file (step 3.1 rule) instead of being overwritten.
- If the Preferences POST answer lacks `InvoiceMessage`, the stored subject is checked with a GET. A missing or
  empty template subject at preflight raises `EmailPathUnavailableError`; an unreadable recovery file raises
  `TemplateRestoreError`; failing to write the recovery file raises `EmailPathUnavailableError`. A cancellation
  during the swap write or the send still triggers the restore.
- Send failures are classified: a failure at the token endpoint, a 400/401/403/404/429/503 or a connect error
  means "not sent, safe to retry"; timeouts, 500/502/504 and unreadable answers mean "outcome unknown". `/send`
  is never POSTed twice.
- `graph`/`smtp`: **safer:** in redirect (shadow) mode `mark_sent` is skipped, since the customer was not
  e-mailed. A Graph 403/404 on `sendMail` and rejected Graph or SMTP credentials raise
  `EmailPathUnavailableError` (every send would fail the same way). If the pay link cannot be read, the e-mail
  is sent without it with a WARNING. Preflight failures raise `EmailPathUnavailableError`; a missing
  `EMAIL_BODY_TEMPLATE` file raises `ConfigError`. The Graph URL uses `quote(sender, safe='@')`; empty
  cc/bcc/replyTo/attachment/header lists are left out; SMTP passes an explicit Message-ID domain (no DNS lookup)
  and de-duplicates recipients.

### Carriers, common to all

- Every carrier factory is `build_client(settings, token_store, *, clock=None, http=None)` (03 §9 / 06 §12),
  where 07 and 09 show a two-argument form. The pipeline passes `FixedClock(run_now)`. An injected HTTP client
  is never closed by the carrier.
- **Safer paging:** an absolute `Next-Page` link is followed only when scheme, host **and port** match the API
  base (the spec checks the host only, which would let plain http or another port receive the token). A
  `?query` link is resolved against the current URL rather than sent as a cursor. A malformed link stops paging
  with a WARNING. This holds for `dcsa.fetch_event_pages` and the Maersk client.
- Probe output: `error` is set only when the integration itself failed (auth, HTTP error, unusable shape, no
  events); "no ETA" or an untracked aggregator booking leaves it empty. Probes add diagnostic keys beyond the
  spec (request summaries with `next_page_value`, `elapsed_ms`, `explain` lines, etc.). Maersk and aggregator
  outputs are additionally scrubbed of the credential values themselves.

### `carriers/dcsa.py` (09 Part A)

- "Laden, non-transshipment DISC at POD" (§A.3.8 step 3, §A.3.9 a/d) is implemented literally; the stricter
  `is_final_disc` is used only where the spec names it (`resolve_pod` step 5). All acceptance tables hold
  either way.
- §A.3.9 (b), the post-arrival proof, has no facility rule: it uses the proof event's facility, falling back to
  the vessel-level arrival's.
- Comparisons of event type, classifier, codes, UN/LOCODE and container numbers are done after strip/upper;
  wrapper keys are read case-insensitively; a tuple payload is accepted like a list.
- v3 envelopes: metadata also replaces blank (not only missing) payload values; an envelope without payload
  produces no event.
- `to_local_naive` returns `None` on overflow (sentinel years), takes a `date` as midnight, reads offset hints
  case-insensitively and treats invalid hours/minutes as an invalid hint. A missing `event_datetime` ranks as
  `datetime.min`.
- `eta_result_from_payload` checks the payload before the policy (spec step order). `explain_selection` reports
  an invalid policy as a text line and adds notes for the §A.4 limitations 1–3.
- `parse_events` skips a malformed item with a DEBUG line instead of raising. `CLASSIFIER_RANK` is a read-only
  mapping.

### `carriers/msc.py` (06)

- api mode `is_actual`: the "matched classifier is ACT" part is not applied when the transshipment rule was used
  (the +7-day ETA is an estimate even though the LOAD it comes from is ACT).
- Edge-block detection decodes HTML entities first (Akamai writes `Reference&#32;&#35;…`). The edge latch also
  applies in api mode (403 + Akamai text gives the edge-block message rather than "rejected the subscription
  key").
- Latches raise a fresh error with the same message, chained to the latched one (re-raising one instance hundreds
  of times would grow its traceback); failure grouping still works.
- `MscBridgeAuth.set_manual_token` also clears a fatal latch from earlier in the same process, so a token pasted
  at the pre-flight prompt is used. `MscClient.set_manual_token` clears only a token latch.
- `normalize_token` also accepts a pasted `Authorization:` prefix; a bare `Bearer` counts as empty
  (`ConfigError`). Messages never echo the value. An `MSC_ACCESS_TOKEN` containing whitespace is ignored with one
  WARNING, then the no-token error (a `ConfigError` there would break the 03 §5 boundary rule).
- `token_status()` adds `expired` (`seconds_left` can be negative); in api mode also `sandbox` and `has_key`.
- `MscSelection` has two extra defaulted fields (`event_date`, `event`); positional construction still works.
  The transshipment source date uses a fixed English month map (locale-independent).
- Multi-container check (log only): a container without a usable date shows `n/a` and never triggers the
  WARNING by itself.
- No readable `eventDateTime` gives a specific `NoEtaError`. 401/403 key messages strip trailing periods before
  the hint; a 400 whose message mentions reference/format/pattern/invalid/length gets the EBKG hint (06 §6.4).
- The empty-`MSC_USER_EMAIL` WARNING is logged once per client at its first GraphQL request, so runs without MSC
  rows do not warn.
- `probe()` returns `EtaResult.to_dict()`; the bridge status sits under `bridge`, not `token` (which `redact()`
  would mask whole).
- **Safer:** when the API timeline has no containers, the customs rule still runs on the all-events timeline (a
  hold means no e-mail).

### `carriers/maersk.py` (07)

- Source text: 07 §7.3 says `source=f"Maersk {arrival.basis}"`, but §9.2 requires the UN/LOCODE in the source.
  `" at <UNLOCODE>[/<facility>]"` is appended to the shared DCSA bases, e.g.
  `Maersk TRANSPORT ARRI EST at USNYC/APMT`.
- Exhausted transport errors (`ServiceError` with no status) map to `Maersk API network error: …`, without the
  booking number, so a network outage can trip the circuit breaker (the 07 §6.4 catch-all text contains the
  booking and never would).
- **Safer:** `select_final_arrival` ignores ARRI events with an unknown classifier, and its DISC-ACT proof counts
  only laden boxes (an EMPTY box move cannot turn an estimate into an arrival).
- Token response: a non-JSON body is `AuthError("Maersk token request failed: …")`; an invalid `expires_in`
  falls back to the JWT `exp`, then now + 7000 s; `id_token` is not stored.
- Shape errors in selection become `CarrierLookupError("Maersk returned events in an unexpected shape: …")`;
  `NoEtaError` propagates unchanged.
- Additive API: `MaerskClient.auth`, async context-manager support, `MaerskAuth.wall_time`.

### `carriers/cma.py` (08)

- Token-endpoint failures raise `CmaTokenError` (an `AuthError`), re-raised unchanged so the message keeps the
  token endpoint's reason (e.g. a network error) instead of the misleading "CMA rejected the API credentials";
  `HINT_401` is used only after a second API 401.
- Other 4xx without DCSA `errors[]`:
  `CMA tracking API rejected the request for {ref} (HTTP {status}): {ServiceError text}`. DCSA errors are read
  from the redacted, ≤ 500-character body excerpt (complete objects are recovered from a cut-off excerpt).
- Token lifetime: `expires_in`, else the JWT `exp`, else CMA's documented 299 s.
- Extra injectable clocks (`monotonic=` for the shared 429 gate, `wall_time=` on `CmaOAuth`) and extra keyword
  arguments `_send(..., ref='')`, `_fetch_events(..., summaries=)`.
- `prefilter_raw_events` accepts any mapping, reads keys case-insensitively and keeps v3 withdrawal envelopes.
- `endpoints_agree` is true only when both endpoints give the same ETA and containers, or the same `NoEtaError`
  text. Unexpected `AttributeError`s in selection become `Could not read CMA tracking data for {ref}: …`.
  `details["pod"]` stays `None` when CMA tagged no POD (the source text still shows the heuristic's choice).
- The wrong-booking check compares the references stripped and upper-cased; the probe shows them as CMA sent
  them. `probe()` returns early, with `error` set, for an invalid booking or when the OAuth token cannot be
  obtained, so it makes no pointless billable calls.

### `carriers/aggregator.py` and `carriers/grimaldi.py` (09 Parts B, C)

- `ShipsGoBackend` sends `X-Shipsgo-User-Token` and `Accept` on every request and builds absolute URLs, so an
  injected client works. `GrimaldiClient` accepts `clock`/`http` and ignores them.
- HTTP 401/402/403/422 are never retried and map to the exact 09 §B.7.2 messages. A 422 on a request without a
  booking becomes a `ServiceError`; the 422 text is the body's `message`, else a redacted excerpt (≤ 200 chars).
- **Safer credit handling:** the per-run create cap is re-checked inside the create lock; a create that ends
  with no response or 500/502/504 also counts against the cap (it may have been charged); a 409 never counts.
  A 409 is stored with `created_by_us=True` (literal 09 §B.7.5 step 5) and counted as "found".
- Polling uses the larger of real elapsed time and the sum of poll intervals; the interval has a 1 s floor.
- `prepare()` stops after 3 consecutive provider failures and counts the rest as failed (avoids minutes of
  retries with a bad token); per-booking errors never stop it.
- Bookings must match ShipsGo's pattern `^[A-Za-z0-9/-]+$` and are normalised with `strip().upper()`.
- The tracking-state file is re-read before every write (concurrent processes keep their entries); a write
  failure is a WARNING. Entries without a tracking id are re-resolved; an unreadable `first_seen_at` counts as
  not fresh.
- Movements outside the plausible years are skipped; a `NOT_ASSIGNED` container makes its events vessel-level;
  container numbers are de-duplicated; a missing status uses the forward-compatible readiness rule.
- `probe()` never registers a booking or writes the state file; without a carrier it derives one from the 01 §4
  patterns (the router stays the authority).
- MSC via the aggregator (09 §B.7.6, a documented deviation) applies no MSC customs-hold or transshipment rule.

### `carriers/router.py` (09 Part D, 10 §6)

- CMA not-configured text follows 08 §3 when all three CMA names are missing:
  `CMA lookups are not configured — set CMA_API_KEY (or CMA_CLIENT_ID + CMA_CLIENT_SECRET) in .env`. Every other
  case uses the 09 §D.2 join.
- **Circuit breaker:** `UnsupportedCarrierError`, `AggregatorBookingError`, `NoEtaError`, `CustomsHoldError` and
  unexpected exceptions never count, and like a success they end the current run of identical failures. A counted
  failure with a new text starts a new run at 1. A cancelled lookup changes nothing. A tripped breaker resets when
  a lookup already in flight then succeeds or fails differently. The fail-fast message uses the current count.
- `build_router` builds only clients that `route()` can reach: a native client per carrier set to `native`, the
  aggregator only if some carrier uses it, the Grimaldi stub only for `native`.
- Additive: `CarrierRouter` defaults and validates `failure_limit >= 1`, normalises providers, swallows a failed
  aggregator warm-up with a WARNING, has an idempotent `aclose()` and async context-manager support.
- Memo key `(carrier, booking.strip().upper())`; `probe()` bypasses memo and breaker.

### `outputs.py` and `ledger.py` (01 §14, 10 §5, §9)

- Text output files use the platform newline (like the original's `open(..., "a")`). `Send2Bindu.csv` uses
  `csv.writer`'s `\r\n` and gets a BOM only when new or empty. Report and `ToEmail` CSVs are UTF-8 with a BOM;
  the report is rewritten atomically.
- An `OSError` while appending an entry (e.g. `Send2Bindu.csv` open in Excel) does not stop the run: the entry
  text is logged at ERROR and counted in the final summary.
- `Ledger(path, *, clock=None, enabled=True)`. `emailed_on()` returns the latest successful `emailed` event of
  that day (its `detail.sent_at`, else the record time). Records are redacted, written as one line with flush
  and fsync; ledger write failures are logged, never fatal. Actions: `qbo_update`, `emailed`,
  `qbo_revert_doc_number`, `vship_comment`, `vship_patch_eta`; failures are recorded with `ok=false`.

### `pipeline.py` (10)

- "No actions → skip to Phase 5" skips the prompt but still writes the routed/failed entries, so BEN, BINDU and
  Failed entries are never lost.
- Declined, non-interactive without `--yes` (both exit 3), and dry runs write no output files, `ToEmail` CSV or
  ledger records. The plan report is still written; on abort it is rewritten with stage `aborted`.
- **Safer:** any exception from the e-mail preflight (`prepare_for_sending`) blocks e-mail for the whole run, not
  only `EmailPathUnavailableError`. NOTIFY rows then fail with `QB Invoice Processing Error, No Email Sent` and the
  run exits 4 (a dry run stays 0).
- **Restore failure after a sent e-mail** (spec tension between 04b and P34): a `TemplateRestoreError` carrying
  `.receipt` means the e-mail went out. The row is still FAILED with the QB entry (P34), but there is no D11
  revert (the `A` suffix records the notice that was sent), and the e-mail is recorded in the ledger as
  `emailed` so the same-day guard blocks a resend. Without a receipt, the D11 revert runs.
- A NOTIFY row already running when the e-mail path breaks does not send; it gets the stored error and its
  DocNumber is reverted (D11). NOTIFY rows not yet started fail without any action.
- An unresolved VShip booking id fails the VShip steps with `Note Write Error, QB Invoice Processing Successful`
  (01 §6.6), including PAID_UPDATE rows that have no comment step.
- A partially paid invoice whose internal comments are unavailable fails the TR/SWB check with
  `QB Invoice Processing Error, No Email Sent` (01 §6.7).
- **Ctrl+C during apply:** each running plan finishes its current (shielded) action, then stops; unfinished
  plans become INTERRUPTED, the summary is printed, the report is rewritten with stage `interrupted`, the clients
  are closed and the lock released, then the cancellation propagates (the CLI exits 3). During planning or review
  it prints `Interrupted before anything was changed.`.
- The input path check (exists, file, `.csv`) runs first, before the lock and any network call (exit 2); a
  non-UTF-8 input or a CSV error is exit 2 as well.
- `Loaded {n} booking(s) from file.` counts rows after the D2 header skip and before `--only`/`--limit`; an extra
  INFO line reports the selection. The report lists the selected rows. D3 duplicates are found before
  `--only`/`--limit`.
- The NOTIFY guards (no BillEmail, DocNumber too long, ledger same-day guard) run in Phase 2c, before the
  `--no-email` transformation, so they also apply to `--no-email` runs.
- `--no-email`: a `ToEmail_<run_id>.csv` row is written when the row's QuickBooks update succeeds (where the
  e-mail would have been sent); `to`/`cc` joined with `, `; header and BOM on creation; never in a dry run.
- Phase 0 step 6 (integration change): a login that fails with a network error, or with an HTTP status other
  than 400/401/403, gets a network/retry hint instead of "Run `eta qbo-auth`"; credential failures keep the
  credential hint. Exit code 2 either way.
- `run()` takes extra keyword hooks (`clock`, `services`, `input_fn`, `is_tty`, `getpass_fn`, `out`,
  `log_file`), all defaulted.

### `cli.py` (11)

- **Exit codes:** Ctrl+C exits 3 (`Interrupted (Ctrl+C).`), since 11 §3 defines no code for it. A cancelled
  file picker prints `No input file selected - nothing was changed.` and exits 3; no Tk exits 2.
- **Probe exit codes:** 0 working; 1 not working (probe `error` set, service or lookup errors; a `--compare`
  DIFF, as in 06 §10); 2 configuration, authentication or run-lock error, including "service not configured";
  3 when a write probe is refused or not confirmed. Usage errors exit 2 (argparse).
- **Write probes (safer reading** of "each asks for confirmation; on production they also require `--yes`"):
  they always ask y/N, even with `--yes`; on production they need `--yes` too; without a terminal they refuse
  (exit 3). They take the run lock; read-only probes take none.
- `--write-test` refuses before any write (`NOT RUN`, exit 1) when the invoice's ETA is empty: restoring "the
  previous state" would mean clearing a custom field, and the spec gives no request body for that. It also
  checks that DocNumber and the other custom fields are unchanged after each step, and saves the full
  request/response exchanges (redacted) through a recording HTTP client injected into `QboClient`.
- `--prefs-roundtrip` writes the bodies the e-mailer would write (full or sparse per `QB_PREFS_UPDATE_MODE`),
  stops after step 1 if the unchanged write-back is not lossless, writes the 04b §6.2 recovery file before
  changing the subject (so a crash is repaired by `--restore-template` or the next run), always attempts the
  restore, and refuses when a recovery file already exists. Diffs ignore only top-level `SyncToken` and
  `MetaData`.
- **Safer:** `--send-test` bypasses the verification gate for the probe only. On production it refuses unless
  `BillEmail` is exactly `QB_TEST_SEND_TO` and `BillEmailCc`/`Bcc` hold no other address (stricter than 04b §10).
  The subject's mm/dd is the invoice's ETA, or today. A failed send records nothing.
- `--restore-template` uses the template-swap recovery whatever `QB_EMAIL_STRATEGY` is (only template swap writes
  recovery files); with nothing to restore it prints `Nothing to restore` and exits 0.
- Console output of probes is a redacted human summary (11 §2); the full redacted JSON is saved under
  `<ETA_STATE_DIR>/probe/` as `<service>_<id>_<YYYYmmdd_HHMMSS>.json`, except VShip (`vship_<booking>.json`,
  05 §10). QuickBooks files: `qbo_<realm>_…`, `qbo_write-test_<doc>_…`, `qbo_prefs-roundtrip_<realm>_…`,
  `qbo_send-test_<doc>_…`, `qbo_fields_<doc>_…`; others `aggregator_account_…`, `msc_compare_…`,
  `msc_<booking>_…`. MSC booking probes are always saved; `--sandbox` only with `--save-raw`.
- `eta probe all` runs: the QuickBooks read probe (plus the `--invoice` lookup), VShip for every `--booking`,
  MSC/Maersk/CMA for the bookings matching their 01 §4 pattern (CMA prints the billable notice first), and the
  aggregator account check. Unconfigured or booking-less services are listed as skipped; `--token-info` is not
  part of it. The exit code is the worst result.
- `--env-file` and `--log-level` work on every command except `version`. Every command except `version` and
  `--help` writes a DEBUG log file `<ETA_OUTPUT_DIR>/logs/eta_<ts>.log`; if it cannot be created, logging falls
  back to the console with a warning. Logging is configured before settings are loaded, so the obsolete-variable
  notice is not lost.
- Additions: `eta probe qbo --invoice DOC` without a write flag reads that invoice; `eta --version`.
- `eta msc-token` works in any MSC mode. An empty or already-expired token exits 2 (an expired one is stored
  first); less than 10 minutes left only prints a note. The token is never printed.
- `eta qbo-fields` exits 1 when the ETA field is not on the invoice, or when its shape comes only from `.env`.
- `eta qbo-auth` without `QB_REALM_ID` builds the QuickBooks settings itself (`realm_id=""`). `--listen` with
  production keys only logs a warning.
- `eta eta` uses the real router (memo, breaker, providers) with a fixed clock; a configured aggregator may
  register new bookings (09 §B.11 step 3). Bookings are routed as typed (stripped, no suffix removal).
  `--json` prints `{run_now, cutoff, window_business_days, results: [...]}`. Exit 0 even when lookups fail.
- The MSC token pre-flight (10 Phase 1) stays in `pipeline.run`; the CLI only passes the terminal check, the
  hidden-input function and the log file. `--max-parallel` is applied with `dataclasses.replace`.

---

## B. Changes to files owned by another stage

Stages A–F and G changed only their own files. The integration stage made these cross-stage fixes:

| File (owning stage) | Change | Reason |
|---|---|---|
| `src/eta_automations/logging_setup.py` (A) | `_BEARER_RE` skips a `/bearer` URL path segment | The real dry run showed garbled DEBUG lines for the QuickBooks token endpoint (`tokens/bearer ***`); only that over-masking changed, no secret is unmasked |
| `src/eta_automations/pipeline.py` (F) | `_authenticate` gives a network/retry hint for a login `ServiceError` without a credential verdict (no status, or not 400/401/403) | A plain network error had told the user to run `eta qbo-auth` |
| `tests/test_logging.py` (A) | 3 regression cases for the Bearer URL-path redaction | Covers the change above |
| `tests/test_pipeline.py` (F) | `test_vship_auth_failure_exits_2` uses an `AuthError`; new `test_unreachable_service_at_login_exits_2_with_a_network_hint` (network error → connection hint; HTTP 503 → retry hint; HTTP 401 → `eta qbo-auth` hint) | Covers the change above |
| `tests/test_cli.py` (G) | New `test_run_dry_run_with_real_clients_stops_cleanly_at_the_auth_preflight` | End-to-end dry run with the real clients: exit 2, clear message, no traceback, no secrets, no output files |

Stage H (documentation) changed no code: it wrote only `README.md`, `.env.example` and this file.

---

## C. Open issues

1. **Ctrl+C at two prompts needs Enter as well.** The `[y/N]` confirmation (`pipeline.py`, Phase 3) and the
   paste-back prompt of `eta qbo-auth` (`qbo/auth.py`) read input with `await asyncio.to_thread(input, …)`, as
   10 §4 specifies. After Ctrl+C the worker thread stays blocked in `input()`, so the process exits only after
   Enter or a second Ctrl+C. Nothing has been written at that point. A fix would read the line directly (nothing
   else runs concurrently there) but departs from the spec, so it was left. The CLI's own prompts (write-probe
   confirmations, the send-test question, `eta msc-token`) are interruptible at once.
2. **Python 3.12 is only partly verified.** All source and test files parse under Python 3.12.3, but no 3.12
   virtual environment could be created in the build container. Running the suite with 3.12 against the 3.13
   environment's packages gave 1444 passed and 109 failed; every failure inspected was numpy's 3.13 binary
   extension failing to load (pandas paths), and without the pandas-dependent test modules 1155 passed and 0
   failed. 12 §6 asks for a full pass on 3.12 and 3.13: run the suite once in a real 3.12 environment
   (section D).
3. `pip install -e ".[dev]"` was not re-run at the end of the build (no package installs were allowed); the
   existing editable install, including the `eta` command, works.
4. **Nothing has been run against a real service yet.** Every probe and the opt-in live smoke tests
   (`tests/test_live_smoke.py`) are still to be run with real credentials (section D).
5. **Windows-only paths are tested with fakes only:** the `msvcrt` branch of the run lock, the default
   `ETA_STATE_DIR`, the Tk file picker of double-click mode, and Ctrl+C at the CLI's own prompts on a Windows
   console (section D lists a check for each).
6. **Circuit breaker cannot trip on per-booking error texts** (spec tension left as written). The 07 §6.4 Maersk
   JSON-403 text ("the app is not approved … or booking {ref} is not visible …") and catch-all 5xx texts contain
   the booking number, so repeated identical causes do not trip the breaker; each booking fails on its own. The
   usual "not approved" case fails earlier at the token call (HTTP 400) with an identical text, which does trip
   it. CMA's per-booking texts (404, `[]`, wrong booking, no ETA) never trip it, which is intended; its 401/403
   hints and the 429-over-cap text do.
7. **Notes for maintainers:** `DcsaEvent` is a frozen dataclass holding a dict, so it is not hashable (do not
   put events in sets or use them as dict keys). `dcsa.fetch_event_pages` sends only bearer tokens (the spec
   signature has no header hook), so CMA keeps its own `_send` with the `KeyId` header and its 429 gate.

All other open issues listed in the stage reports were resolved during integration or by later stages.

---

## D. Checklist: unverified items to confirm before the first live run

Run these after configuring `.env` (README, "First-time setup checklist"). Without a write flag every probe is
read-only. Write probes ask for confirmation and need `--yes` on a production company; run them on the
QuickBooks sandbox first (`QB_SANDBOX=true` with sandbox keys and realm). Each probe saves its redacted output
under `<ETA_STATE_DIR>/probe/`; the item says what to look for in it.

### QuickBooks Online (04 §11, 04b §11)

- [ ] **Connection and refresh-token expiry field.** `eta probe qbo --log-level DEBUG` → company name, invoice
  e-mail template subject, refresh-token expiry. The DEBUG lines (also in the log file) list the token
  response's key names; any new absolute-expiry field is stored in `tokens.json` under
  `response_expiry_fields`.
- [ ] **ETA custom-field shape** (`"Type": "DateType"`, `"DateValue": "YYYY-MM-DD"` is UNVERIFIED).
  `eta qbo-fields --invoice <DocNumber whose ETA is set in QuickBooks>` → exit 0, the ETA field with its
  `DefinitionId`, type and value. Paste the printed `QB_ETA_DEFINITION_ID` / `QB_ETA_FIELD_TYPE` lines into
  `.env`. If the field does not appear, enable it for invoices in QuickBooks; if it still does not, see 04 §8.1.
- [ ] **DocNumber case handling.** `eta qbo-fields --invoice <the same DocNumber in lower case>` → the invoice is
  found (an INFO line reports the case-insensitive match).
- [ ] **Writing the ETA date field, sandbox vs production, and the `CustomField` array semantics.**
  `eta probe qbo --write-test --invoice <test DocNumber whose ETA is set>` on the sandbox, then
  `eta probe qbo --write-test --invoice <test DocNumber whose ETA is set> --yes` on production → PASS after the
  set and after the restore, with DocNumber and the other custom fields unchanged. FAIL → 04 §8.1, decide with
  the business before any live run.
- [ ] **Preferences write-back** (full update vs the conflicting read-only annotation).
  `eta probe qbo --prefs-roundtrip` (add `--yes` on production) → PASS: the unchanged write changes only
  `SyncToken`/`MetaData`, the probe write only `InvoiceMessage.Subject`, and the restore matches the start.
  Before using `QB_PREFS_UPDATE_MODE=sparse`, set it and run the probe again.
- [ ] **`/send` uses the Preferences subject** (mandatory before any customer e-mail).
  `eta probe qbo --send-test --invoice <test DocNumber>` (add `--yes` on production) → in the `QB_TEST_SEND_TO`
  inbox: exactly the probe subject, the invoice PDF and, with online payments on, the pay link. Answer `y` only
  if all are true; the answer is recorded in `qbo_email_probe.json`.
- [ ] **graph/smtp only — marking the invoice as sent and the pay link** (`EmailStatus=EmailSent` and
  `InvoiceLink` are UNVERIFIED). With `QB_EMAIL_STRATEGY=graph` (or `smtp`):
  `eta probe qbo --send-test --invoice <test DocNumber>` → the e-mail carries the pay link (if online payments
  are on), and QuickBooks shows the invoice as sent. A WARNING `could not mark invoice … as sent in QuickBooks`
  means that feature is not available; e-mails still go out.
- [ ] **DocNumber maximum length** (21 is documented, but 22-character DocNumbers exist in the company's data).
  No probe: the write test never changes a DocNumber. Rename a test invoice by hand in QuickBooks to 22
  characters; if QuickBooks accepts it, raise `QB_DOCNUMBER_MAX_LEN`. The first live run
  (`eta run <file>.csv --limit 5`) also reports `stored DocNumber … (length limit?)` if a write is truncated.

### VShipCRM (05 §11)

- [ ] **Payload fields, 403-means-expired, token lifetime.** `eta probe vship --booking <a base booking>` → no
  `error`; the token expiry and seconds left, the search candidates with the chosen booking id, the customer
  name, the internal comments and the notification status match VShipCRM.

### MSC (06 §11)

- [ ] **Parser, body shape, timestamps, network.** `eta probe msc --sandbox` → ETA `2022-12-26`. A failure here
  is local (proxy, TLS or installation).
- [ ] **Bridge-token lifetime.** `eta msc-token`, then `eta probe msc --token-info` → note `seconds_left` and
  `exp`, and tell the team how long a pasted token lasts.
- [ ] **GraphQL with token + headers only, no cookies; response values.** `eta probe msc --booking <EBKG…>` for
  three known bookings (in transit, discharged at destination, transshipment or not yet loaded) → the same ETAs
  as myMSC and the old tool. Empty results → `MSC_USER_EMAIL` must equal your myMSC sign-in e-mail. The saved
  payload shows the raw values (including `containers[0].finalEta`, used only with `MSC_RESCUE_FALLBACK=true`).
- [ ] **Official API (after MSC approval): base URL, key header, error texts.** With `MSC_API_BASE_URL` and
  `MSC_API_SUBSCRIPTION_KEY` set: `eta probe msc --booking <EBKG…>` → mode `api`, HTTP 200, an ETA.
- [ ] **`EBKG…` accepted as `carrierBookingReference`; ETA offsets; customs-hold approximation.** With a fresh
  bridge token as well: `eta probe msc --compare --booking <EBKG…> --booking <EBKG…> …` on 20+ bookings,
  including a known customs-hold case → every row MATCH (exit 0). If only the customs columns differ, set
  `MSC_API_CUSTOMS_HOLD_RULE=false`.

### Maersk (07 §11)

- [ ] **Token, quota, paging.** `eta probe maersk --booking <a live 9-digit booking>` → `token.ok: true`,
  `token.expires_in_s` about 7199, `counts` > 0, no 429. In `requests[]`, note `next_page` (format) and the events
  per page (`limit` 100). Leave `MAERSK_API_VERSION` unset unless Maersk support asks. If the token call answers
  HTTP 400, the product is not approved yet; `ERR_GW_001` means the wrong client id (use the Consumer Key). If
  429s appear in runs, set `MAERSK_RATE_PER_MINUTE`. Use `MAERSK_BASE_URL=https://api-stage.maersk.com` only if
  Maersk provisions your app there, and run this probe against it first.
- [ ] **Final destination vs port of discharge; port-local times.** Run the same probe for 3–5 bookings (in
  transit, recently arrived, inland/IPI) and compare `eta`, `alternatives.final` and `alternatives.pod` with the
  maersk.com "Arrives …" date opened by hand in a browser. If `pod` matches better, set `MAERSK_ETA_TARGET=pod`;
  if a multi-container booking disagrees, consider `MAERSK_MULTI_CONTAINER_POLICY=latest`. Times off by whole
  hours mean the carrier sends UTC: report it, do not "fix" times globally.

### CMA CGM (08 §10–§11) — billable: at least 2 calls per probe

- [ ] **(a) public event types, (b) `carrierSpecificData`, (c) `Next-Page` format, (e) port-local offsets,
  (g) latency.** `eta probe cma --booking <NAM…>` for three bookings (in transit, discharged at the POD, with a
  transshipment) → `eta` matches CMA's website ("ETA Berth at POD", or "Arrived at POD" once discharged; the
  latest container for multi-container bookings); `analysis.<mode>.counts` includes `TRANSPORT ARRI EST` and/or
  `PLN`; `shipment_location_types` contains `POD` and `carrier_specific_data_ratio` is about 1.0;
  `endpoints_agree` is true; if a page had `Next-Page`, `requests[*].next_page_value` shows its format and page 2
  answered 200; `requests[*].elapsed_ms` gives the latency. Quote `requests[*].request_id` to CMA support.
- [ ] **(d) unknown booking: 404 or `[]`.** `eta probe cma --booking NAM0000000` → record which answer CMA gives
  (`requests[0].status`); both are handled.
- [ ] **OAuth tier only — token error wording.** With `CMA_CLIENT_ID` + `CMA_CLIENT_SECRET`:
  `eta probe cma --booking <NAM…>` → `info.token` shows a token; on failure, `info.token.error` shows CMA's text.
- [ ] **(f) 429 headers and body.** Nothing to run: when CMA rate-limits, the WARNING
  `[CMA] rate limited (HTTP 429) — waiting Ns …` and the request summaries' `retry_after` show it. Then set
  `CMA_MAX_CALLS_PER_MINUTE` (e.g. 20).

### Aggregator and Grimaldi (09 Parts B, C) — only if you use ShipsGo

- [ ] **Account and carrier codes (Grimaldi's `GRIU` is UNVERIFIED).** `eta probe aggregator` →
  `Account: OK`; `Carrier codes:` shows `MSCU`, `MAEU` and `CMDU` as active; `Active carriers:` also lists any
  carrier whose name contains GRIMALDI, with its code. If that code is not `GRIU`, set `AGG_SCAC_GRIMALDI`.
- [ ] **ShipsGo timestamp offsets (UTC vs local).**
  `eta probe aggregator --booking <a booking already tracked in the ShipsGo dashboard>` → the `explain` lines and
  `eta` match the ShipsGo dashboard and the carrier's site.
- [ ] **Grimaldi tracking by booking number.** With `ETA_PROVIDER_GRIMALDI=aggregator`:
  `eta eta <S3… booking>` → registers it (1 credit) and may answer "re-run later"; run it again after a few
  minutes → an ETA. A ShipsGo rejection (HTTP 422) shows its message here.

### Commercial items (no probe: ask the provider)

- MSC: production base URL, lead time, cost, whether API access is self-service, and which bookings are visible
  (06 §2.1, §11). Ask in the Integration Form.
- Maersk: approval lead time, cost, quotas, and the current product label (07 §11).
- CMA CGM: free-trial quota, contract rate limits and prices (08 §11).
- ShipsGo: credit details beyond the published fee page (09 §B.2).

### Covered by unit tests only (nothing to probe live)

- `requestid` reuse on QuickBooks writes, and `/send` never being re-POSTed (Intuit ignores `requestid` there);
  lower-case `fault`/`error` keys on 401/429; the Graph inline-attachment limit (PDFs over 3 MiB are refused).
- DCSA Track & Trace 3.0 envelopes (09 §A.2.2): parsed tolerantly from a synthetic fixture; no carrier serves
  v3 yet.

### Platform checks (not `eta probe`)

- [ ] **Windows state folder.**
  `.venv\Scripts\python -c "from eta_automations.config import default_state_dir; print(default_state_dir())"`
  → the `ETAutomationsAPI` folder under `%LOCALAPPDATA%` (used whenever `ETA_STATE_DIR` is not set).
- [ ] **Windows run lock.** In one console run `eta run <file>.csv` and leave it at the `[y/N]` prompt; in a
  second console run `eta run <file>.csv` → `Another ETAutomationsAPI run is in progress (lock: …)` and exit
  code 2. Then answer `N` in the first console (nothing is changed).
- [ ] **Double-click mode.** Double-click `scripts\run_eta.bat`, pick a CSV in the "ETA Tool" window, answer `N`
  at the prompt, then press ENTER at `Press ENTER to close this window...`.
- [ ] **Ctrl+C on a Windows console.** `eta probe qbo --write-test --invoice <test DocNumber whose ETA is set>`,
  press Ctrl+C at the confirmation question → exit code 3, nothing written.
- [ ] **Python 3.12.** `py -3.12 -m venv .venv312`, `.venv312\Scripts\pip install -e ".[dev]"`,
  `.venv312\Scripts\python -m pytest -q` → all tests pass (only `live` tests and the German-locale test skipped).
- [ ] **Optional live smoke tests** (read-only probes; no writes, no e-mail). On Windows, set each variable
  with `set NAME=value` first. Add `ETA_LIVE_CMA_BOOKING=<NAM…>` only if the billable CMA probe should run.

  ```sh
  ETA_LIVE_TESTS=1 ETA_LIVE_ENV_FILE=.env ETA_LIVE_VSHIP_BOOKING=<booking> ETA_LIVE_MSC_BOOKING=<EBKG…> \
    ETA_LIVE_MAERSK_BOOKING=<9-digit booking> python -m pytest -q tests/test_live_smoke.py
  ```
