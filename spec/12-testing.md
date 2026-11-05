# 12 — Testing strategy and test matrix

## 1. Principles

- `pytest -q` must pass with **no network access**. Add an autouse fixture in `tests/conftest.py` that
  fails any test performing an un-mocked HTTP request (respx with `assert_all_mocked=True`, plus a guard
  that patches `httpx.AsyncClient.send` outside respx contexts to raise).
- Every pure rule in 01-business-rules.md has table-driven tests whose expected values are the golden values
  in that file (they were produced by running the original code).
- Every integration client (04–09) is tested with respx against the fixtures given in its spec file. Fixture
  files are created **verbatim** from the code blocks in the spec files (`tests/fixtures/<service>/<name>.json`).
- The pipeline is tested end-to-end with in-memory fakes (no respx needed) and a `FixedClock`.
- Time is always injected (`FixedClock`); tests never depend on today's date.
- Tests run on Windows and Linux (use `tmp_path`, never hard-coded paths; compare text files with `\n`).

## 2. Layout

```
tests/
  conftest.py                  # network guard, FixedClock fixture, settings factory, tmp dirs, fake services
  fakes.py                     # FakeQbo, FakeVShip, FakeCarrier (same method names as the real clients)
  fixtures/qbo/…  fixtures/vship/…  fixtures/msc/…  fixtures/maersk/…  fixtures/cma/…  fixtures/dcsa/…  fixtures/aggregator/…
  test_rules.py                # classification, routing, suffix, malformed message, duplicates, header skip
  test_business_days.py        # 01 §5 golden vectors (all 22 rows) + in_window boundary cases
  test_texts.py                # 01 §8 all 8 subject goldens + unreachable branch; 01 §10 note texts
  test_vship_logic.py          # 05 §9 tests 1–3
  test_vship_client.py         # 05 §9 tests 4–9
  test_qbo_*.py                # tests listed in 04
  test_carrier_msc.py          # tests listed in 06
  test_carrier_maersk.py       # tests listed in 07
  test_carrier_cma.py          # tests listed in 08
  test_dcsa.py, test_aggregator.py, test_router.py   # tests listed in 09 + router tests below
  test_http.py                 # retry/backoff/idempotency/Retry-After/redaction
  test_token_store.py          # atomic write, corrupt file, is_fresh skew, JWT exp parsing
  test_config.py               # required vars, booleans, legacy names, carriers None when unconfigured
  test_outputs.py              # exact bytes of every output template, BOM only on new CSV, append mode
  test_ledger.py               # record/emailed_on, corrupt line skipped, not written in dry-run
  test_pipeline.py             # end-to-end matrix (§4)
  test_cli.py                  # argument parsing, exit codes, double-click mode (monkeypatched input/Tk)
```

## 3. Unit test highlights (in addition to those listed in each spec)

**Rules (`test_rules.py`)**
- Every example row of 01 §3 (doc_number, amount) → (kind, base, route).
- Router table 01 §4: `EBKG17208602`→MSC; `267546627`→Maersk; `2675466270`→ref-number error;
  `NAM1234567`→CMA; `S312345678`→Grimaldi error text exactly; `XYZ`→no-pattern error text exactly;
  `EBKG17208602 C 2`→D6 error; lower-case `ebkg17208602`→MSC (D5).
- `with_arrival_suffix`: `EBKG12345678`→`EBKG12345678 A`; `EBKG12345678 A`→`EBKG12345678 AA`.
- Malformed message equals the 01 §7.1 template for amount `abc` and for a 1-column row.

**HTTP (`test_http.py`)**
- GET 503 twice then 200 → returns 200, 3 calls; Retry-After `2` honoured (patch `asyncio.sleep` and assert the delay).
- POST 500 → no retry (1 call) → `ServiceError`; POST 503 → retried; POST ReadTimeout → not retried.
- ConnectError on POST → retried.
- `ServiceError` message contains redacted body excerpt ≤ 500 chars and the `intuit_tid` request id when present.
- `redact()` masks `Authorization`, `password`, `refresh_token`, `Consumer-Key`, `KeyId`, `Bearer …` in strings and query params.

**Token store**
- `put` then `get` across instances; file mode 0600 on POSIX; corrupt JSON → warning + empty;
  `is_fresh` false within the 120 s skew; JWT `exp` extraction from the 05 §9 sample token.

## 4. Pipeline matrix (`test_pipeline.py`)

Use `FixedClock(datetime(2026, 11, 5, 9, 30))` (cutoff Mon 2026-11-16 09:30) and fakes. Each case asserts the
route, the actions executed (in order) against the fakes, the exact output-file contents, and the counters.

| # | Input row | Fake setup | Expected |
|---|---|---|---|
| P1 | `EBKG10000001,2450.00` | ETA 2026-11-03 (arrived), notif (0,True), invoice unpaid, BillEmail set | NOTIFY; QBO update(eta=11-03, doc→`EBKG10000001 A`); send subject `…****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE CARGO****` (old DocNumber in subject); note `ARRIVED ON 11/03. NOTIF #1`; patch `2026-11-03T00:00:00`; ok=1 |
| P2 | `EBKG10000002 A,2450.00` | ETA 2026-11-03, notif (1,True) | NOTIFY; doc→`EBKG10000002 AA`; subject `…PLEASE PAY AND CLEAR THIS PAST DUE INVOICE AND CLEAR THE CARGO****`; note `…NOTIF #2` |
| P3 | `NAM1000003,1800` | ETA 2026-11-12 14:00 (CMA-style time; future, in window), notif (0,True), unpaid | NOTIFY; **no** doc change; subject `…***ESTIMATED ARRIVING ON 11/12. PLEASE MAKE PAYMENT ARRANGEMENT****`; note `ARRIVING ON 11/12.`; patch `2026-11-12T14:00:00` |
| P4 | `EBKG10000004,5000` | ETA 2026-11-03, invoice paid (balance 0) | PAID_UPDATE; QBO update(eta) only (no doc change, no send, no note); patch |
| P5 | `EBKG10000005,5000` | ETA 2026-11-03, partially paid, comments contain `SWB RCVD` | ACCOUNTING; Send2Accounting line exact; no writes |
| P6 | `EBKG10000006,5000` | partially paid, no TR/SWB comment | NOTIFY |
| P7 | `EBKG10000007,5000` | ETA 2026-12-01 (out of window) | FUTURE_UPDATE; QBO update(eta); note `ARRIVING ON 12/01.`; patch |
| P8 | `EBKG10000008,5000` | notif (2,False) | BEN line `EBKG10000008` |
| P9 | `EBKG10000009,5000` | notif (1,False) | FAILED `[SKIP] #EBKG10000009 - Done in the last week` |
| P10 | `EBKG10000010 A,5000` | notif (1,False) | FAILED `[SKIP] #EBKG10000010 A - Done in the last week` (D8) |
| P11 | `EBKG10000011,5000` | invoice missing (in window) | FAILED `[SKIP] #EBKG10000011 - not found in QuickBooks` |
| P12 | `EBKG10000012,5000` | invoice missing (out of window) | same entry (D9) |
| P13 | `EBKG10000013,850` | VShip customer `ACME` | BINDU CSV row `EBKG10000013,850,ACME` |
| P14 | `EBKG10000014 AA,850` | — | BINDU (amount before Ben) |
| P15 | `EBKG10000015 AA,5000` | — | BEN |
| P16 | `EBKG10000016 R,5000` | — | SKIPPED; no file entry |
| P17 | `NAM1000017 C,5000` | — | FAILED entry is exactly `NAM1000017 C` |
| P18 | `EBKG10000018,abc` | — | FAILED malformed template |
| P19 | carrier raises `CarrierLookupError("boom")` | — | `[ERROR] #… - Carrier Lookup Failed.\nboom\n` |
| P20 | carrier raises `NoEtaError("none")` | — | `[ERROR] #… -  Can't determine ETA.\nnone\n` |
| P21 | carrier raises `CustomsHoldError` | — | PotentialCustomsHold line exact; failed+1 |
| P22 | carrier raises `RuntimeError("x")` | — | `Unexpected error during carrier lookup.` entry |
| P23 | notif lookup raises; `ETA_ON_NOTIF_LOOKUP_ERROR=send` | VShip id lookup fails | NOTIFY planned with warning; apply: QBO update+send OK, note fails → `Note Write Error, QB Invoice Processing Successful`; failed=1 |
| P24 | same with `=fail` | — | FAILED `VShip notification lookup failed, nothing sent` |
| P25 | QBO send fails after doc change | — | FAILED `QB Invoice Processing Error, No Email Sent` + `(invoice number reverted to …)`; revert call made (D11); no VShip writes |
| P26 | VShip PATCH fails | — | FAILED `Booking ETA Change Failed, QB Invoice Processing Successful`; counted failed (D12) |
| P27 | invoice without BillEmail | — | FAILED `QB Invoice Processing Error, No Email Sent\nInvoice has no customer e-mail address (BillEmail)` |
| P28 | ledger shows e-mail today for that invoice | — | FAILED `[SKIP] … already e-mailed today at HH:MM (ledger); use --force to resend`; with `force=True` → NOTIFY |
| P29 | duplicate doc_number rows | — | second → `[SKIP] #… - duplicate row in input file (line N)` |
| P30 | dry-run over P1–P29 inputs | — | no fake write methods called, no output files, ledger untouched, report written |
| P31 | 6 consecutive MSC rows whose fake raises `CarrierLookupError("MSC login rejected")` (identical text) | — | 6th row fails fast with the circuit-breaker message; the fake carrier was called only 5 times. Control: 6 rows raising `CarrierLookupError(f"no record of booking {b}")` (texts differ) → fake called 6 times, breaker never trips |
| P32 | confirmation declined | — | exit code 3, no writes |
| P33 | `prepare_for_sending` raises `EmailPathUnavailableError("not verified")` | P1 + P4 + P7 inputs | P1 (NOTIFY) FAILED `QB Invoice Processing Error, No Email Sent\nnot verified`, **no** QBO update for it; P4/P7 applied; exit code 4 |
| P34 | `send_invoice` raises `TemplateRestoreError` on the 1st of 3 NOTIFY rows (parallel=1) | — | 1st row FAILED (+ D11 revert attempted); 2nd/3rd FAILED without any QBO call; non-NOTIFY rows still applied; exit code 4 |
| P35 | `--no-email` with P1 input | — | QBO update with `doc_number=None`, no send, no VShip comment, VShip patch done; `ToEmail_<run_id>.csv` row with subject, `EBKG10000001 A`, `ARRIVED ON 11/03. NOTIF #1`; status OK |
| P36 | DocNumber in `qbo.ambiguous` | — | FAILED `QB Invoice Processing Error, No Email Sent` + the AmbiguousInvoiceError text (D21) |
| P37 | two rows `NAM1000037` and `NAM1000037 A` | — | the fake CMA client's `get_eta` called **once** (router memo, 10 §6) |
| P38 | a second `run()` while the first holds `run_lock` | — | `RunLockedError` → exit code 2; dry-run is not blocked |

## 5. Optional live smoke tests

Marked `@pytest.mark.live` and skipped unless `ETA_LIVE_TESTS=1`: run each probe (read-only) against real
services using `.env`. Never part of the default run; never send e-mail.

## 6. Definition of done

- `pip install -e .[dev] && pytest -q` passes on Python 3.12 and 3.13.
- `eta --help`, `eta run --help`, `eta probe --help` work.
- `test_cli.py` runs `main(["run", "<tmp>/sample_input.csv", "--dry-run"])` end-to-end with
  `eta_automations.pipeline.build_services` monkeypatched to yield the fakes from `tests/fakes.py`
  (production code never imports from `tests/`).
- No Playwright/Selenium/browser dependency anywhere (`pip list` check in a test: importing the package must
  not import any of them).
