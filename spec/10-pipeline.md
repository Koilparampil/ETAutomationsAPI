# 10 — Pipeline: plan → confirm → apply (`pipeline.py`, `outputs.py`, `ledger.py`)

## 1. Why the new pipeline is fast

The original `mainIG.main()` processed one row at a time, and for each row it might: launch a browser for
the carrier (MSC headless; Maersk and CMA headful Chrome with multi-second warm-ups), drive the QuickBooks
SPA with `slow_mo=200` ms per action plus fixed sleeps, and search VShip several times. That is roughly
30–90 s per row, i.e. hours for a ~500-row file.

The new pipeline:
1. does **all reads first, concurrently** (carrier ETAs, VShip lookups, one batched QBO query),
2. shows a **plan** and asks for confirmation (or `--dry-run` stops here),
3. performs the **writes concurrently across rows** (sequentially within a row), each a single API call.

Expected wall time for ~400 actionable rows: a few minutes end-to-end, dominated by carrier API latency and
by e-mail sending (see 04-quickbooks-online.md for whether sends must be serialized). These are estimates —
the run report records real per-phase timings.

## 2. Models (`models.py`, in addition to 03-foundations §4)

```python
class RowKind(StrEnum):
    MALFORMED = "MALFORMED"; STANDARD = "STANDARD"; NOTIF1_SENT = "NOTIF1_SENT"
    BOTH_SENT = "BOTH_SENT"; SKIP_R = "SKIP_R"; MANUAL = "MANUAL"

@dataclass(frozen=True, slots=True)
class InputRow:
    line_no: int
    doc_number: str
    amount_raw: str
    amount: Decimal | None
    parse_error: str | None           # e.g. "could not convert string to float: 'abc'"

@dataclass(frozen=True, slots=True)
class ParsedRow:
    row: InputRow
    kind: RowKind
    base_booking: str | None          # stripped + upper-cased (01 §3)

class Route(StrEnum):
    FAILED = "FAILED"; CUSTOMS_HOLD = "CUSTOMS_HOLD"; SKIPPED = "SKIPPED"
    BINDU = "BINDU"; BEN = "BEN"; ACCOUNTING = "ACCOUNTING"
    PAID_UPDATE = "PAID_UPDATE"; NOTIFY = "NOTIFY"; FUTURE_UPDATE = "FUTURE_UPDATE"

class OutputFile(StrEnum):
    BINDU = "Send2Bindu.csv"; BEN = "Send2Ben.txt"; ACCOUNTING = "Send2Accounting.txt"
    FAILED = "Failed_To_Process.txt"; CUSTOMS = "PotentialCustomsHold.txt"

# Side-effect actions, executed in list order during apply
@dataclass(frozen=True, slots=True)
class QboUpdate:      eta: date; new_doc_number: str | None = None
@dataclass(frozen=True, slots=True)
class QboSend:        subject: str
@dataclass(frozen=True, slots=True)
class VShipComment:   text: str
@dataclass(frozen=True, slots=True)
class VShipPatchEta:  eta: datetime
Action = QboUpdate | QboSend | VShipComment | VShipPatchEta

@dataclass
class Plan:
    parsed: ParsedRow
    route: Route
    message: str = ""                     # Failed/routed entry text (01 §14) or human reason
    output: tuple[OutputFile, str | list[str]] | None = None   # entry to write for routed/failed rows
    carrier: Carrier | None = None
    eta: EtaResult | None = None
    cutoff: datetime | None = None
    in_window: bool | None = None
    notif: NotifStatus | None = None
    notif_defaulted: bool = False         # 01 §6.2 'send' default applied
    invoice: InvoiceSnapshot | None = None
    vship_booking_id: str | None = None
    vship_error: str | None = None        # VShip id/comments could not be read in the plan phase
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # filled during apply
    status: Literal["PENDING", "OK", "FAILED", "ROUTED", "SKIPPED", "INTERRUPTED"] = "PENDING"
    done: list[str] = field(default_factory=list)
    error: str | None = None
```

## 3. Services container

```python
@dataclass
class Services:
    settings: Settings
    clock: Clock                          # FixedClock(run_now) — every client gets the same instant
    token_store: TokenStore
    qbo: QboClient
    vship: VShipClient
    router: CarrierRouter                 # carriers/router.py: route(base) -> (Carrier, token, CarrierClient)
    outputs: OutputWriter
    ledger: Ledger

@asynccontextmanager
async def build_services(settings: Settings, run_now: datetime, *, read_only: bool) -> AsyncIterator[Services]:
    """Creates TokenStore(<ETA_STATE_DIR>/tokens.json), the QBO client via build_qbo(settings, token_store,
    read_only=read_only) (04 §6.8), VShipAuth/VShipClient(read_only=read_only), every carrier client via its
    build_client(), the router, OutputWriter(enabled=not read_only), Ledger.
    On exit: aclose() every client (in a finally) — QboClient.aclose() also closes the e-mail strategy, which
    restores the QBO template if a recovery record exists (04b §6.2)."""
```

## 4. Phases

```python
async def run(settings: Settings, input_csv: Path, *, dry_run: bool, assume_yes: bool, force: bool,
              no_email: bool, only: set[str] | None, limit: int | None,
              clock: Clock = SystemClock()) -> RunSummary:
    # RunSummary: counts per Route and status, phase timings, report/log paths, email_blocked, exit_code
```

### Phase 0 — preflight (sequential, fail fast)
1. `run_now = clock.now()`; `run_id = run_now.strftime("%Y%m%d_%H%M%S")`.
2. Create `ETA_OUTPUT_DIR`, `ETA_OUTPUT_DIR/reports`, `ETA_OUTPUT_DIR/logs`, `ETA_STATE_DIR` if missing.
3. Unless `dry_run`: enter `run_lock(state_dir)` (03 §14) for the rest of the run; `RunLockedError` → exit code 2.
4. `require(settings, "qbo", "vship")` (03 §12) → `ConfigError` lists every missing variable → exit code 2.
5. `async with build_services(settings, run_now, read_only=dry_run)`: the QBO client is built with
   `read_only=dry_run` (04 §6.1) and the VShip client likewise (05 §6.2), so a dry run cannot write even by mistake.
6. Authenticate QBO (`await qbo_auth.get_token()`) and VShip (`await vship_auth.get_token()`). Failure →
   print a clear message (which variable / which command to run, e.g. `eta qbo-auth`) and exit code 2.
   Carrier auth is lazy (first lookup), guarded by the circuit breaker (§6).
   (The e-mail strategy's crash recovery and verification gate run in Phase 3 — see step 2 there.)

### Phase 1 — read & classify (pure)
`rows = read_input(input_csv)` → `parsed = [classify(r) for r in rows]` (01 §2–3), then apply D2 (header),
D3 (duplicates), `--only` (keep rows whose `doc_number` or `base_booking` is in the set) and `--limit N`
(first N remaining rows). Log `Loaded {n} booking(s) from file.`; if zero → print
`Invoice list is empty - nothing to do.` and exit 0.

**MSC token pre-flight** (06 §5.1, only when MSC runs in `mymsc` bridge mode and at least one row routes to
MSC): read `token_status()`; if there is no token or `seconds_left < 600`, prompt for one on a TTY
(`getpass`, same as `eta msc-token`); without a TTY, fail fast with the 06 §5.1 message (exit code 2) — before
any lookup or write. In `api` mode nothing is needed.

### Phase 2 — plan (read-only, concurrent)
First, once: `await router.prepare(bases)` with the base booking of every STANDARD / NOTIF1_SENT row at or
above the amount threshold (09 §D.2 — lets the optional aggregator register new bookings in one serialized
batch; a no-op without an aggregator).

Then all rows run as tasks in an `asyncio.TaskGroup`, bounded by `asyncio.Semaphore(ETA_MAX_PARALLEL_BOOKINGS)`.
A task never raises: every exception is converted into a `Plan` with route FAILED and the right 01 §14 entry.

**2a — per-row lookups**
```
MALFORMED  → Plan(FAILED, output=(FAILED, malformed entry))
SKIP_R     → Plan(SKIPPED)
MANUAL     → Plan(FAILED, output=(FAILED, doc_number))
amount < threshold (STANDARD/NOTIF1_SENT/BOTH_SENT)
           → name = await vship.get_customer_name(base) (errors → "" + warning, D14)
             Plan(BINDU, output=(BINDU, [doc, amount_raw, name]))
BOTH_SENT  → Plan(BEN, output=(BEN, doc))
STANDARD / NOTIF1_SENT:
   carrier, token, client = router.route(base)            # UnsupportedCarrierError → 01 §6.1
   eta = await client.get_eta(token)                      # errors → 01 §6.1 (CUSTOMS_HOLD route for CustomsHoldError)
   cutoff = window_cutoff(run_now, ETA_WINDOW_BUSINESS_DAYS); in_window = eta.eta <= cutoff
   try:  booking_id = await vship.find_booking_id(base)
   except Exception as e: vship_error = str(e)
   if in_window:
       try:  comments = await vship.get_internal_comments(booking_id)   (skipped if vship_error)
             notif = notif_status(comments, run_now)
       except/if vship_error: apply ETA_ON_NOTIF_LOOKUP_ERROR (01 §6.2) → notif = NotifStatus(0, True), notif_defaulted=True
                              or Plan(FAILED, "VShip notification lookup failed, nothing sent")
       notif.count == 2   → Plan(BEN, output=(BEN, doc))
       not have_to_do     → Plan(FAILED, output=(FAILED, "[SKIP] #{doc} - Done in the last week"))
       else               → mark "needs QBO" (in-window)
   else                   → mark "needs QBO" (out-of-window)
```

**2b — one batched QBO lookup**
`invoices = await qbo.find_invoices_by_doc_numbers(sorted({p.parsed.row.doc_number for p in needs_qbo}))`.
A QBO lookup failure here (network/auth) fails every pending row with
`[ERROR] #{doc} - QB Invoice Processing Error, No Email Sent\n{err}\n`.
A DocNumber listed in `qbo.ambiguous` (several invoices share it — 04 §6.3) fails with the same template and
the `AmbiguousInvoiceError` text (**D21**: the original silently used the first match).

**2c — finalize decisions (pure except the TR/SWB comments fetch, which is already cached)**
```
inv missing                     → FAILED "[SKIP] #{doc} - not found in QuickBooks"
in-window and inv.is_paid       → PAID_UPDATE  actions=[QboUpdate(eta.date()), VShipPatchEta(eta)]
in-window, partially paid:
    vship_error                 → FAILED "QB Invoice Processing Error, No Email Sent" + vship_error   (01 §6.7)
    has_tr_swb_note(comments)   → ACCOUNTING output=(ACCOUNTING, f"{doc} - is partially paid and has a TR/SWB note")
    else                        → NOTIFY (below)
in-window otherwise             → NOTIFY
out-of-window                   → FUTURE_UPDATE actions=[QboUpdate(eta.date()),
                                                       VShipComment(vship_comment_for(eta, notif_index=0, now=run_now)),
                                                       VShipPatchEta(eta)]
NOTIFY:
    subject = subject_for(inv.doc_number, eta, notif_sent=(notif.count == 1), paid=False, now=run_now)
    new_doc = with_arrival_suffix(inv.doc_number) if eta.date() <= today else None
    actions = [QboUpdate(eta.date(), new_doc), QboSend(subject),
               VShipComment(vship_comment_for(eta, notif_index=(1 if notif.count == 1 else 0), now=run_now)),
               VShipPatchEta(eta)]
    guards (each → FAILED "QB Invoice Processing Error, No Email Sent\n<reason>"):
        - inv.recipients is empty  → reason "Invoice has no customer e-mail address (BillEmail)"
        - new_doc and (reason := doc_number_problem(new_doc, settings.qbo)) → that reason (04 §7)
    ledger guard (unless --force): ledger.emailed_on(inv.id, today) → FAILED
        "[SKIP] #{doc} - already e-mailed today at {HH:MM} (ledger); use --force to resend"
```
Where `eta` above means `plan.eta.eta` (the datetime) and `doc` the input doc_number.

### Phase 3 — review & confirm
1. **`--no-email` transformation** (11-cli): every NOTIFY plan keeps only
   `[QboUpdate(eta.date(), new_doc_number=None), VShipPatchEta(eta)]` — no e-mail, no DocNumber suffix and no
   VShip note (the suffix and the `NOTIF #n`/`ARRIVING` note are the records that a notice was sent, so they are
   left for whoever sends the e-mail by hand). Its would-be subject, recipients, suffixed DocNumber and note
   text are written to `<ETA_OUTPUT_DIR>/ToEmail_<run_id>.csv`
   (`doc_number, invoice_id, customer, to, cc, subject, doc_number_after_sending, vship_note_after_sending`)
   during Phase 4. Such rows end as OK with message `e-mail pending (manual send, --no-email)`.
2. **E-mail path preflight** — only if at least one plan still contains a `QboSend`:
   `await qbo.prepare_for_sending(dry_run=dry_run)` (04 §6.6, 04b §6.2/§6.3). In a real run this also
   restores a QBO e-mail template left modified by a crashed run (04b crash recovery) — a repair, so it is
   done before the prompt. If it raises `EmailPathUnavailableError` (including `TemplateRestoreError`):
   print the message prominently, convert every NOTIFY plan to FAILED with
   `[ERROR] #{doc} - QB Invoice Processing Error, No Email Sent\n{err}\n`, set `email_blocked = True`
   (exit code 4 at the end), and continue — the other routes can still be applied.
3. Write the plan report (§7) — always, also in dry-run.
4. Print the summary (§8): counts per route, then every NOTIFY row
   (`doc → new doc | subject | to: recipients`), every PAID_UPDATE/FUTURE_UPDATE row (`doc → ETA`), and all
   warnings (e.g. `notif lookup failed — defaulting to send`, `MSC other containers disagree`).
5. `--dry-run` → stop here; print `Dry run: nothing was changed. Plan saved to <path>`; exit 0.
6. If there are no actions → skip to Phase 5.
7. Unless `--yes`: prompt `Apply {n_actions} change(s) to {n_rows} invoice(s) and send {n_emails} e-mail(s)? [y/N] `
   (read it with `await asyncio.to_thread(input, prompt)` so the event loop is not blocked).
   If stdin is not a TTY and `--yes` was not given → abort with a message (exit code 3). Anything but `y`/`yes` → abort.

### Phase 4 — apply (writes)
1. Write every routed/failed entry produced in Phase 2 to its output file **in input order** (§5).
   Mark those plans ROUTED / FAILED / SKIPPED.
2. Run every plan with actions as a task (TaskGroup + `ETA_MAX_PARALLEL_BOOKINGS` semaphore). Within a plan,
   execute actions in order:
   - `QboUpdate` → `inv = await qbo.update_invoice(inv, eta=a.eta, doc_number=a.new_doc_number)`; ledger record.
   - `QboSend` → `receipt = await qbo.send_invoice(inv, subject=a.subject)`; ledger record (`emailed`, with the
     receipt's strategy, recipients and `sent_at`/`qbo_delivery_time`; never the body).
     On failure, if a previous `QboUpdate` changed the DocNumber, attempt the D11 revert
     (`update_invoice(updated, doc_number=original)`) and append
     `"(invoice number reverted to <old>)"` or `"(could not revert invoice number: <err>)"` to the error.
     If the failure is `EmailPathUnavailableError`/`TemplateRestoreError`: set `email_blocked = True`, and every
     NOTIFY plan that has **not started yet** is failed (without running any of its actions) with
     `[ERROR] #{doc} - QB Invoice Processing Error, No Email Sent\n{err}\n`; non-NOTIFY plans keep running.
     Exception order matters: check `TemplateRestoreError`/`EmailPathUnavailableError` before plain `InvoiceSendError`.
   - NOTIFY plans start their `QboUpdate` only after Phase 3's e-mail preflight succeeded, so a blocked e-mail
     path can never leave a suffixed DocNumber without an e-mail.
   - `VShipComment` → `await vship.post_comment(booking_id, a.text)` (if `vship_booking_id` is None, raise the
     stored `vship_error` as `NoteError`); ledger record.
   - `VShipPatchEta` → `await vship.patch_expected_arrival(booking_id, a.eta)`; ledger record.
   - Map the first failure to 01 §6.6, append it to Failed_To_Process.txt immediately, mark FAILED, stop the plan.
   - All done → log `[OK]    #{doc}`, mark OK.
3. Ctrl+C / cancellation: stop starting new plans; plans already running finish their *current* action, then
   stop; remaining plans are INTERRUPTED (listed in the report). Cleanup (e-mail strategy restore, client close)
   always runs.

### Phase 5 — report
Print `Done: {ok} succeeded, {failed} failed/skipped.` (01 §13), the per-route table, phase timings, and paths
of the report/log files. Rewrite the run report with final statuses. Exit code 0, or **4** if `email_blocked`
(the e-mail path failed; print the 04b manual-restore instructions again if a `TemplateRestoreError` occurred).

## 5. `outputs.py`

```python
class OutputWriter:
    def __init__(self, output_dir: Path, *, enabled: bool): ...   # enabled=False in dry-run
    def write(self, file: OutputFile, entry: str | list[str]) -> None:
        """Append one entry. Text files: f.write(entry + "\\n") (entry may contain newlines).
        Send2Bindu.csv: csv.writer(f).writerow(entry) with newline=""; if the file is new, write a UTF-8 BOM first.
        Opens/closes the file per call (crash-safe), encoding utf-8, guarded by a threading.Lock."""
```

## 6. Carrier lookup memo and circuit breaker

**Memo (single-flight).** The router memoizes `get_eta` per `(carrier, query token)` for the whole run:
the first caller creates an `asyncio.Task`, later callers await the same task (`asyncio.shield`), and both
results and exceptions are memoized. Two input rows for the same booking (e.g. `NAM1234567` and
`NAM1234567 A`) therefore cost one carrier call — important because some carrier APIs bill per call
(08-carrier-cma-cgm.md §2). Carrier clients may also memoize internally (CMA does); the two layers are
compatible. `probe` bypasses the memo.

The router itself (provider selection `ETA_PROVIDER_<CARRIER>`, `route()`, `prepare()`) is specified in
09 Part D; the memo above and the breaker below are its additional responsibilities.

**Circuit breaker.** The router also tracks, per `(provider, carrier)`, the run of consecutive
`CarrierLookupError`s whose **message text is identical**. It never counts `UnsupportedCarrierError`,
`AggregatorBookingError`, `NoEtaError` or `CustomsHoldError` (09 §D.2), and route-time errors are raised before
any client is called, so they never reach it. Per-booking failures such as
"no record of booking NAM1234567" contain the booking number, so their texts differ and never trip the
breaker; service-level failures (rejected credentials, product not approved, network down) produce the
same text for every booking. After `ETA_CARRIER_FAILURE_LIMIT` (default 5) identical consecutive failures,
further rows for that carrier fail fast, without a network call, with
`CarrierLookupError(f"{carrier} lookups aborted after {n} identical consecutive failures; last error: {last}")`.
Any success or differently-worded failure resets the count. Memoized duplicates (above) are not counted twice.

## 7. Run report (`<ETA_OUTPUT_DIR>/reports/run_<run_id>.csv` and `.json`)

One row per input row, columns:
`line_no, doc_number, amount, kind, base_booking, carrier, eta, eta_source, eta_is_actual, eta_rescue, cutoff,
in_window, notif_count, notif_defaulted, invoice_id, balance, total, bill_email, route, new_doc_number, subject,
vship_booking_id, actions_planned, actions_done, status, message, warnings`
(`eta_rescue` = `EtaResult.details.get("rescue", False)` — rows whose ETA came from a carrier's rescue-only
fallback, 06 §7.5, are also listed as warnings in the summary).
The console summary groups identical failure messages per carrier, e.g. `MSC: 287 bookings failed — The MSC
access token expired or was rejected (HTTP 401)…`, instead of printing the same line hundreds of times.
The JSON file contains the same plus `run_id`, settings summary (no secrets), phase timings, and versions.

## 8. Console summary format (example)

```
Plan (run 20261105_093000, 494 rows, cutoff Mon 2026-11-16 09:30)
  NOTIFY ......... 212   (e-mails to send)
  PAID_UPDATE .... 31
  FUTURE_UPDATE .. 74
  BEN ............ 15
  BINDU .......... 28
  ACCOUNTING ..... 3
  SKIPPED ........ 30
  CUSTOMS_HOLD ... 2
  FAILED ......... 99
Warnings: 4 (see report)
```

## 9. `ledger.py`

```python
class Ledger:
    def __init__(self, path: Path): ...      # <ETA_STATE_DIR>/ledger.jsonl ; loads an index of e-mail events at init
    def record(self, *, run_id: str, doc_number: str, invoice_id: str | None, base_booking: str | None,
               action: str, detail: Mapping[str, Any], ok: bool) -> None   # append one JSON line, flush+fsync
    def emailed_on(self, invoice_id: str, day: date) -> datetime | None    # time of a successful QboSend that day
```
Never written in dry-run. Corrupt lines are skipped with a WARNING.

## 10. Tests (see 12-testing.md for the full matrix)

The pipeline is tested end-to-end with fake clients (in-memory implementations of QboClient, VShipClient and
CarrierClient behind the same method names) and a `FixedClock`: one test per route and per failure template
in 01 §14, plus dry-run (no writes, no output files, report written), confirmation abort, `--only`, duplicate
rows, ledger same-day guard, D11 revert, circuit breaker, and interruption.
