# 01 — Business rules (exact port of the original behaviour)

This file specifies **what** the program decides for every input row. It is a
line-by-line port of `mainIG.py`, `carrierTing/carriers.py`,
`stringTing/subjectEdit.py`, `QBTings/invoiceProcessing.py` (the decision parts),
`VShip/customerLookUp.py`, `VShip/noteSearch.py`, `VShip/writeNotif.py` and
`VShip/bookingLookUp.py` from the original `ETAutomations` repository.

**How** each external call is made lives in 04–09; **how** the run is
orchestrated (plan → confirm → apply, concurrency) lives in 10.

Everything here is implemented as **pure functions** in `rules.py`,
`texts.py`, `business_days.py` and `vship/logic.py` (no I/O), so it can be unit
tested exhaustively (see 12-testing.md).

Changes from the original are labelled **D1…Dn** (deliberate deviations, §15).
Quirks kept on purpose are labelled **O1…On** (§16). Nothing else may differ.

---

## 1. Glossary

| Term | Meaning |
|---|---|
| **DocNumber** | The QuickBooks invoice number. In this business it is the carrier booking number, sometimes followed by staff annotations (`EBKG12345678 A`, `NAM1234567 C`, `267546627/$150/RlsR`). The first CSV column holds it **exactly as it appears in QuickBooks**. |
| **Base booking** | The carrier booking number with the notification suffix removed (`EBKG12345678 A` → `EBKG12345678`). |
| **Notif #1 / #2** | First / second post-arrival notice e-mailed to the customer. Recorded twice: (a) a VShip internal comment containing `NOTIF #1`/`NOTIF #2`, (b) an `A` appended to the QBO DocNumber (`X` → `X A` → `X AA`). |
| **Window** | ETA ≤ now + 6 US business days (§5). Arrived shipments (ETA in the past) are always "in window". |
| **run_now** | `clock.now()` captured once at the start of the run (03-foundations §3). `today = run_now.date()`. |

## 2. Input file

- Path chosen by the user (CLI argument or Tk file dialog — 11-cli.md). Must exist, be a file, and have
  a `.csv` extension (case-insensitive) — same validation as `tinkyWinky.is_valid_txt_file`.
- Read with `open(path, "r", newline="", encoding="utf-8-sig")` and `csv.reader` (default dialect).
- Skip rows that are empty lists (blank lines).
- For each remaining row (1-based `line_no` = physical row index among non-blank rows, starting at 1):
  - `doc_number = row[0].strip()`
  - `amount_raw = row[1].strip() if len(row) > 1 else ""`
  - `amount = Decimal(amount_raw.replace(",", "").replace("$", "").strip())` → on `InvalidOperation`
    (or empty string) the row is **MALFORMED** (§7.1). (The original used `float`; `Decimal` gives the
    same comparisons for money values and avoids binary rounding.)
  - `doc_number == ""` → **MALFORMED** too (**D1**: the original crashed with `IndexError`).
- **D2 (header row):** if the **first** row is MALFORMED, its amount cell is non-numeric, and its first
  cell contains no digit (e.g. `Num,Amount`), skip it silently with an INFO log instead of reporting it.
- Duplicate DocNumbers in one file: process only the first occurrence; later ones are reported as
  `[SKIP] #<doc> - duplicate row in input file (line N)` in Failed_To_Process.txt (**D3**: the original
  would process — and possibly e-mail — the same invoice twice in one run).

## 3. Row classification (suffix rules)

Evaluate in this exact order (`last = doc_number[-1].lower()`):

| # | Condition | Kind | Base booking |
|---|---|---|---|
| 0 | row is MALFORMED | `MALFORMED` | — |
| 1 | `last` is a digit `0-9` | `STANDARD` | `doc_number` |
| 2 | `last == "a"` and `doc_number[-2:].lower() == "aa"` | `BOTH_SENT` | `doc_number[:-2].strip()` |
| 3 | `last == "a"` and `re.search(r"(?i)[^a]a$", doc_number)` | `NOTIF1_SENT` | `doc_number[:-1].strip()` |
| 4 | `last == "a"` (only possible for the 1-char DocNumber `"a"`) | `MANUAL` | — (**D4**: original silently did nothing) |
| 5 | `last == "r"` | `SKIP_R` | — |
| 6 | anything else | `MANUAL` | — |

The base booking is then `.strip().upper()`-ed for carrier routing and VShip searches (**D5**, harmless:
the original was case-sensitive). The **QBO lookup always uses `doc_number` exactly as in the file**.

Then the amount threshold (`ETA_MIN_INVOICE_AMOUNT`, default `1000`) applies to kinds
`STANDARD`, `NOTIF1_SENT`, `BOTH_SENT` **before anything else** (including before the BOTH_SENT → Ben
routing): `amount < 1000` → route **BINDU** (§7.2). It does **not** apply to `SKIP_R`/`MANUAL`
(the amount must still parse, though — MALFORMED is checked first for every row, exactly as the original).

Examples (all with amount `2,450.00` unless stated):

| doc_number | Kind | Base booking | Route |
|---|---|---|---|
| `EBKG17208602` | STANDARD | `EBKG17208602` | carrier lookup |
| `267546627` | STANDARD | `267546627` | carrier lookup |
| `EBKG17208602 A` | NOTIF1_SENT | `EBKG17208602` | carrier lookup |
| `EBKG17208602a` | NOTIF1_SENT | `EBKG17208602` | carrier lookup |
| `EBKG17208602 AA` | BOTH_SENT | `EBKG17208602` | BEN |
| `EBKG17208602AA` | BOTH_SENT | `EBKG17208602` | BEN |
| `EBKG17208602 AA`, amount `850.00` | BOTH_SENT | `EBKG17208602` | BINDU (amount check first) |
| `EBKG17208602 A R` | SKIP_R | — | SKIPPED |
| `NAM1234567 C` | MANUAL | — | FAILED (raw doc_number line) |
| `267546627/$150/RlsR` | SKIP_R | — | SKIPPED |
| `EBKG17208602`, amount `abc` | MALFORMED | — | FAILED (malformed message) |

## 4. Carrier routing (`carriers/router.py`)

Applied to the base booking `b` (already stripped + upper-cased), in this order:

| Order | Test | Carrier | Query token |
|---|---|---|---|
| 1 | `m = re.search(r"EBKG\d{8}", b)` | MSC | `m.group(0)` |
| 2 | `re.fullmatch(r"2\d{8}", b)` | Maersk | `b` |
| 3 | `m = re.search(r"NAM\d{7}", b)` | CMA | `m.group(0)` |
| 4 | `m = re.search(r"S3\d{8}", b)` | Grimaldi | `m.group(0)` |
| 5 | `re.search(r"20\d{8}", b)` | — reference number | → `UnsupportedCarrierError(f"This is a Ref Number: {b}")` |
| 6 | otherwise | — | → `UnsupportedCarrierError(f"No carrier pattern matched booking {b} — process manually")` |

- **D6 (extra text):** if `b != query token` for MSC/CMA/Grimaldi (e.g. `EBKG17208602 C 2`), raise
  `CarrierLookupError(f"Booking '{b}' contains text besides the {carrier} booking number {token} — process manually")`.
  The original sent the whole string to the carrier and failed later with a less clear error; the outcome
  (row lands in Failed_To_Process.txt as a carrier-lookup failure) is the same.
- **D7 (reference numbers):** the original matched only `2026\d{6}`; the new pattern `20\d{8}` (10 digits
  starting `20`) keeps working after 2026. It cannot collide with Maersk (exactly 9 digits, `fullmatch`).
- Grimaldi: `UnsupportedCarrierError(f"Grimaldi ETA lookup not yet implemented for booking {b}")` unless an
  aggregator provider is configured for it (09-dcsa-aggregators-grimaldi.md).
- Provider selection per carrier (`ETA_PROVIDER_<CARRIER>` = native | aggregator | off) is resolved by the
  router — see 09.

## 5. Time rules (`business_days.py`, `rules.py`)

```python
def add_business_days(start: datetime, n: int) -> datetime:
    # EXACT parity with the original: pandas CustomBusinessDay(calendar=USFederalHolidayCalendar())
    # start + n * CustomBusinessDay  (time of day is preserved; a weekend/holiday start rolls forward
    # as the first step)
def window_cutoff(run_now: datetime, business_days: int = 6) -> datetime: return add_business_days(run_now, business_days)
def in_window(eta: datetime, cutoff: datetime) -> bool: return eta <= cutoff
```

Comparisons (all naive datetimes; ETA is carrier-local wall time, run_now is machine-local — O1):

| Purpose | Rule | Used in |
|---|---|---|
| In 6-business-day window | `eta <= window_cutoff(run_now)` | §6 |
| Subject "arrived" branch | `eta <= run_now` | §8 |
| Subject "estimated" branch | `eta >= run_now` | §8 |
| Append `A` to DocNumber | `eta.date() <= today` | §9 |
| VShip note "ARRIVED" vs "ARRIVING" | `eta < run_now` | §10 |
| Notif #1 "done in the last week" | `notif1_created > run_now - timedelta(days=7)` | §11 |

Golden vectors for `window_cutoff` (generated with the original pandas implementation; tests must reproduce them):

| run_now | weekday | cutoff | weekday |
|---|---|---|---|
| 2026-11-05 09:30 | Thu | 2026-11-16 09:30 | Mon |
| 2026-11-06 16:00 | Fri | 2026-11-17 16:00 | Tue |
| 2026-11-07 10:00 | Sat | 2026-11-17 10:00 | Tue |
| 2026-11-08 23:59 | Sun | 2026-11-17 23:59 | Tue |
| 2026-11-09 08:00 | Mon | 2026-11-18 08:00 | Wed |
| 2026-11-11 12:00 | Wed | 2026-11-19 12:00 | Thu |
| 2026-11-23 07:15 | Mon | 2026-12-02 07:15 | Wed |
| 2026-11-26 09:00 | Thu | 2026-12-04 09:00 | Fri |
| 2026-12-18 17:45 | Fri | 2026-12-29 17:45 | Tue |
| 2026-12-24 08:00 | Thu | 2027-01-05 08:00 | Tue |
| 2026-12-31 13:00 | Thu | 2027-01-11 13:00 | Mon |
| 2027-01-15 10:00 | Fri | 2027-01-26 10:00 | Tue |
| 2027-02-12 10:00 | Fri | 2027-02-23 10:00 | Tue |
| 2027-05-28 10:00 | Fri | 2027-06-08 10:00 | Tue |
| 2027-06-17 10:00 | Thu | 2027-06-28 10:00 | Mon |
| 2027-07-02 10:00 | Fri | 2027-07-13 10:00 | Tue |
| 2027-08-31 10:00 | Tue | 2027-09-09 10:00 | Thu |
| 2027-10-08 10:00 | Fri | 2027-10-19 10:00 | Tue |
| 2027-11-10 10:00 | Wed | 2027-11-19 10:00 | Fri |
| 2027-12-23 10:00 | Thu | 2028-01-04 10:00 | Tue |
| 2027-12-30 10:00 | Thu | 2028-01-10 10:00 | Mon |
| 2028-01-14 10:00 | Fri | 2028-01-25 10:00 | Tue |

(pandas' calendar includes observed dates, e.g. 2027-12-31 for New Year 2028 and 2026-07-03 for July 4th 2026.)

Worked example: run_now = Thu 2026-11-05 09:30 → cutoff Mon 2026-11-16 09:30. An MSC ETA of
2026-11-16 (00:00) is in window; an ETA that carries a time of day, e.g. 2026-11-16 14:00 (CMA, or Maersk
with `MAERSK_ETA_DATE_ONLY=false` — 07 §7.5), is not. Maersk ETAs default to 00:00 for parity with the
date-only maersk.com page the original scraped.

## 6. Decision flow per row

Notation: `doc` = doc_number, `base` = base booking, `F(...)` = append an entry to
Failed_To_Process.txt (exact templates in §14), counters per §13.

```
MALFORMED            → F(malformed message)                                         [failed]
SKIP_R               → log "Don't need to do this one, ends in R"                   [skipped]
MANUAL               → F(doc)   (just the raw doc_number)                           [failed]
STANDARD / NOTIF1_SENT / BOTH_SENT with amount < threshold
                     → BINDU row [doc, amount_raw, customer_name(base)]            [routed]
BOTH_SENT            → BEN line doc                                                 [routed]

STANDARD / NOTIF1_SENT (amount ≥ threshold):
  1. eta = carrier.get_eta(token)                       (§4 router; errors → §6.1)
  2. cutoff = window_cutoff(run_now); in_window = eta.eta <= cutoff
  3. if in_window:
       a. notif = vship notif status for base           (§11; error → §6.2)
       b. notif.count == 2                → BEN line doc                            [routed]
       c. not notif.have_to_do            → F("[SKIP] #{doc} - Done in the last week")  [failed]  (D8)
       d. inv = QBO invoice with DocNumber == doc (exact) ; missing → F("[SKIP] #{doc} - not found in QuickBooks") [failed]
       e. if inv.is_paid:                 → PAID_UPDATE                             (§6.3)
          elif inv.is_partially_paid and tr_swb_found(base comments):
                                          → ACCOUNTING line                         [routed]
          else:                           → NOTIFY                                  (§6.4)
     else (outside window):
       d'. inv lookup as in d; missing → F("[SKIP] #{doc} - not found in QuickBooks") [failed]  (D9)
       e'.                                → FUTURE_UPDATE                           (§6.5)
```

### 6.1 Carrier lookup errors (both kinds)

| Exception from `get_eta` | Entry | Counter |
|---|---|---|
| `CarrierLookupError` (incl. `UnsupportedCarrierError`) | `[ERROR] #{doc} - Carrier Lookup Failed.\n{err}\n` | failed |
| `NoEtaError` | `[ERROR] #{doc} -  Can't determine ETA.\n{err}\n` (two spaces after `-`, O2) | failed |
| `CustomsHoldError` | PotentialCustomsHold.txt: `[ERROR] #{doc} -  Possible Customs Hold.` | failed |
| any other exception | `[ERROR] #{doc} - Unexpected error during carrier lookup.\n{err}\n` | failed |

The original's separate "ETA not found" path (carrier returned `None`) no longer exists:
`get_eta` raises `NoEtaError` instead (03-foundations §9), so those rows get the "Can't determine ETA." entry (**D10**).

### 6.2 VShip notif-lookup errors

Setting `ETA_ON_NOTIF_LOOKUP_ERROR` = `send` (default, **parity**) | `fail`.
- `send`: log WARNING `Notif lookup failed: {err} — defaulting to False` and continue with
  `NotifStatus(count=0, have_to_do=True)` — exactly what the original did (the QBO e-mail is the priority;
  the VShip note step will then fail visibly and be reported).
- `fail`: `F("[ERROR] #{doc} - VShip notification lookup failed, nothing sent\n{err}\n")` [failed].
The plan summary (10-pipeline.md) lists every row that took the `send` default so a human sees it before confirming.

### 6.3 PAID_UPDATE (in window, invoice fully paid)

Original `process_invoice` paid branch: set the ETA custom field and save — **no e-mail, no DocNumber
change, no VShip note**; then look up the VShip booking id and PATCH its expected-arrival date.
Actions, in order:
1. QBO: `update_invoice(inv, eta=eta.date())`
2. VShip: `patch_expected_arrival(vship_booking_id, eta)`

### 6.4 NOTIFY (in window, not fully paid, not routed to Accounting)

Actions, in order:
1. `subject = subject_for(inv.doc_number, eta, notif_sent=(notif.count == 1), paid=False, now=run_now)` — uses the
   DocNumber **before** any change (§8).
2. `new_doc = with_arrival_suffix(inv.doc_number) if eta.date() <= today else None` (§9).
3. QBO: `inv2 = update_invoice(inv, eta=eta.date(), doc_number=new_doc)`.
4. QBO: `send_invoice(inv2, subject=subject)`.
   **D11:** if step 4 fails and step 3 changed the DocNumber, revert it (`update_invoice(inv2', doc_number=inv.doc_number)`,
   best effort) so the next run sees the invoice under its old number; mention the revert outcome in the failure entry.
5. VShip: post comment `vship_comment_for(eta, notif_index=(1 if notif.count == 1 else 0), now=run_now)` (§10).
6. VShip: `patch_expected_arrival(vship_booking_id, eta)`.

### 6.5 FUTURE_UPDATE (outside window)

1. QBO: `update_invoice(inv, eta=eta.date())`
2. VShip: post comment `vship_comment_for(eta, notif_index=0, now=run_now)` → always `ARRIVING ON mm/dd.` here
3. VShip: `patch_expected_arrival(vship_booking_id, eta)`

### 6.6 Failure handling inside PAID_UPDATE / NOTIFY / FUTURE_UPDATE

Steps run in order; the first failure stops the remaining steps of that row.

| Failing step | Entry | Counter |
|---|---|---|
| any QBO step (lookup aside), or the TR/SWB lookup | `[ERROR] #{doc} - QB Invoice Processing Error, No Email Sent\n{err}\n` | failed |
| resolving the VShip booking id or posting the comment (after QBO succeeded) | `[ERROR] #{doc} - Note Write Error, QB Invoice Processing Successful\n{err}\n` | failed |
| PATCH of the VShip ETA (after everything else succeeded) | `[ERROR] #{doc} - Booking ETA Change Failed, QB Invoice Processing Successful\n{err}\n` | failed (**D12**: original counted it as neither ok nor failed) |
| all steps succeeded | log `[OK]    #{doc}` | ok |

**D13 (message unification):** the original used slightly different texts in the out-of-window branch
("Note Write Error, Email Still Sent" although no e-mail is sent there; "QB Invoice Processing Error, QB NOT
Saved"); the new program uses the three texts above everywhere so they can be grepped reliably.

### 6.7 ACCOUNTING routing detail

Only evaluated when the invoice is not fully paid **and** `is_partially_paid` (04-quickbooks-online.md
defines both properties to match the QBO UI). TR/SWB is checked on the VShip **internal comments** of the
base booking (§12). If the TR/SWB lookup itself fails → §6.6 first row (QB Invoice Processing Error, No Email
Sent) — parity: in the original the exception escaped `process_invoice`.

## 7. Routes without carrier lookup

### 7.1 MALFORMED
Entry (reproduce the original dict repr exactly):
```
[ERROR] Row {'inv_number': '<doc_number>', 'amount_due': '<amount_raw>'} - Malformed row (missing amount column or non-numeric amount).
<error text>
```
followed by a blank line (the template ends with `\n` and the writer adds `\n`). `<error text>` is
`could not convert string to float: '<cleaned amount>'` for parity with the original's `float()` message.

### 7.2 BINDU (amount below threshold)
`Send2Bindu.csv` row `[doc_number, amount_raw, customer_name]` via `csv.writer`.
`customer_name` = VShip booking `customerName` for the base booking. **D14:** if the lookup fails, write
`""` and log a WARNING (the original crashed the whole run). Not counted as ok or failed ("routed").

### 7.3 BEN
`Send2Ben.txt` line `doc_number`. Routed.

### 7.4 ACCOUNTING
`Send2Accounting.txt` line `f"{doc_number} - is partially paid and has a TR/SWB note"`. Routed.

## 8. E-mail subject (`texts.subject_for`)

```python
def subject_for(doc_number: str, eta: datetime, *, notif_sent: bool, paid: bool, now: datetime) -> str:
    d = eta.strftime("%m/%d")
    p = f"Invoice for Booking no. {doc_number}."
    arrived, estimated = eta <= now, eta >= now
    if notif_sent:
        if arrived and paid:       return f"{p} ****ARRIVED ON {d}. PLEASE CLEAR THE CARGO****"
        if arrived and not paid:   return f"{p} ****ARRIVED ON {d}. PLEASE PAY AND CLEAR THIS PAST DUE INVOICE AND CLEAR THE CARGO****"
        if estimated and paid:     return f"{p} ***ESTIMATED ARRIVING ON {d}.****"
        if estimated and not paid: return f"{p} ***ESTIMATED ARRIVING ON {d}. PLEASE MAKE PAYMENT ARRANGEMENT****"
        return f"{p} ***Please Check Attached Invoice for ETA***"
    else:
        if arrived and paid:       return f"{p} ****ARRIVED ON {d}. PLEASE CLEAR THE CARGO****"
        if arrived and not paid:   return f"{p} ****ARRIVED ON {d}. PLEASE PLEASE PAY AND CLEAR THE CARGO****"
        if estimated and paid:     return f"{p} ***ESTIMATED ARRIVING ON {d}.****"
        if estimated and not paid: return f"{p} ***ESTIMATED ARRIVING ON {d}. PLEASE MAKE PAYMENT ARRANGEMENT****"
        return f"{p} ***Please Check Attached Invoice for ETA***"
```

Golden outputs captured from the original function (doc `EBKG12345678`, past ETA 11/03, future ETA 11/08):

| notif_sent | paid | ETA | Subject |
|---|---|---|---|
| False | False | past | `Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE CARGO****` |
| False | False | future | `Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08. PLEASE MAKE PAYMENT ARRANGEMENT****` |
| False | True | past | `Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE CLEAR THE CARGO****` |
| False | True | future | `Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08.****` |
| True | False | past | `Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE PAY AND CLEAR THIS PAST DUE INVOICE AND CLEAR THE CARGO****` |
| True | False | future | `Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08. PLEASE MAKE PAYMENT ARRANGEMENT****` |
| True | True | past | `Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE CLEAR THE CARGO****` |
| True | True | future | `Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08.****` |

In practice `paid` is always `False` when a subject is built (paid invoices take PAID_UPDATE), so only the
four `paid=False` strings are ever sent. `notif_sent` is `True` exactly when the VShip notif status count is 1.
Strings are byte-for-byte; do not "fix" `PLEASE PLEASE` (O3) or the asterisk counts.

## 9. DocNumber arrival suffix (`rules.with_arrival_suffix`)

```python
def with_arrival_suffix(doc_number: str) -> str:
    return doc_number + (" A" if doc_number[-1].isdigit() else "A")
```
Applied only in NOTIFY and only when `eta.date() <= today`.
`EBKG12345678` → `EBKG12345678 A`; `EBKG12345678 A` → `EBKG12345678 AA`; `267546627` → `267546627 A`.
If the result exceeds the QBO DocNumber length limit (see 04), do not truncate — fail the row with
`QB Invoice Processing Error, No Email Sent` and the reason.

## 10. VShip note text (`texts.vship_comment_for`)

```python
def vship_comment_for(eta: datetime, *, notif_index: int, now: datetime) -> str:
    if eta < now:
        return f"ARRIVED ON {eta.strftime('%m/%d')}. NOTIF #{notif_index + 1}"
    return f"ARRIVING ON {eta.strftime('%m/%d')}."
```
Examples: arrived 11/03, first notice → `ARRIVED ON 11/03. NOTIF #1`; second → `ARRIVED ON 11/03. NOTIF #2`;
future 11/18 → `ARRIVING ON 11/18.`. Posted with `commentType: 1` (05-vship-crm.md).

## 11. Notification status (`vship/logic.notif_status`)

Input: the booking's `internalComments` list (05-vship-crm.md) and `run_now`.

```python
@dataclass(frozen=True)
class NotifStatus:
    count: int          # 0, 1 or 2
    have_to_do: bool

def notif_status(comments: list[dict], run_now: datetime) -> NotifStatus:
    texts = [(c.get("comment") or "") for c in comments]
    n1 = [c for c in comments if "notif #1" in (c.get("comment") or "").lower()]
    if not n1:
        return NotifStatus(0, True)
    if any("notif #2" in t.lower() for t in texts):
        return NotifStatus(2, False)
    newest = max(parse_vship_datetime(c.get("createdDate")) for c in n1)      # D15
    return NotifStatus(1, not (newest > run_now - timedelta(days=7)))
```
- `"notif #2"` alone without `"notif #1"` → count 0 (parity: the original only checks #2 after finding #1).
- **D15:** the original used the createdDate of the *first* `notif #1` comment in API order; the new code uses
  the *most recent* one (intent: "done in the last week"). Identical when there is one such comment.
- `parse_vship_datetime`: ISO-8601; aware → converted to local time then made naive; naive → as-is;
  unparseable → treat as "old" (have_to_do=True) and log a WARNING.
- A missing/`null` `internalComments` is treated as `[]` (the original raised and fell into §6.2).

## 12. TR/SWB note detection (`vship/logic.has_tr_swb_note`)

Port verbatim (lists include the original's duplicate `"PR FOR TR"`):

```python
KEYWORDS = ["PAID IN FULL, TR FEE SENT TO MSC FOR TR", "PR FOR TR", "PS FOR TR", "PR FOR TR", "PS FOR RTR",
            "PR/ PS FOR TR", "RTR", "SWB RCVD", "TR RECVD", "TR RCVD"]
EXCLUDE  = ["PS - LESS TR FEE, ", "NO TR", "TR PENDING"]

def _build_pattern(words):
    return re.compile(r"\b(?:" + "|".join(re.escape(kw) for kw in words) + r")\b", re.IGNORECASE)

_PATTERN = _build_pattern(KEYWORDS)
_EXCLUDE = _build_pattern(EXCLUDE)

def is_tr_swb_comment(text: str) -> bool:
    return bool(_PATTERN.search(_EXCLUDE.sub("", text)))

def has_tr_swb_note(comments: list[dict]) -> bool:
    return any(is_tr_swb_comment(c.get("comment") or "") for c in comments)
```

Golden cases (from the original code):

| Comment | Match |
|---|---|
| `SWB RCVD 10/12` | True |
| `swb rcvd` | True |
| `TR RCVD` | True |
| `TR RECVD BY JOHN` | True |
| `RTR` / `RTR SENT` | True |
| `PR FOR TR` / `PR/ PS FOR TR` / `PS FOR RTR` | True |
| `PAID IN FULL, TR FEE SENT TO MSC FOR TR` | True |
| `NO TR` / `TR PENDING` | False |
| `NO TR - SWB RCVD` | True |
| `TR PENDING, RTR` | True |
| `PS - LESS TR FEE, PAID` | False |
| `PS - LESS TR FEE, RTR` | True |
| `STRTR` / `TR RCVDX` / `SWBRCVD` | False |
| `PR FOR TRUCKING` | False |
| `ARRIVED ON 11/02. NOTIF #1` | False |

## 13. Counters and summary

- `ok`: rows whose PAID_UPDATE / NOTIFY / FUTURE_UPDATE completed every step.
- `failed`: every row that wrote to Failed_To_Process.txt or PotentialCustomsHold.txt.
- `routed`: BINDU, BEN, ACCOUNTING. `skipped`: SKIP_R.
- Final line (parity): `Done: {ok} succeeded, {failed} failed/skipped.` followed by a per-route breakdown table
  (10-pipeline.md).

## 14. Output files and exact entry templates

All files live in `ETA_OUTPUT_DIR` (default: the current working directory, like the original), are opened
in **append** mode with UTF-8 encoding, and each `write` appends `entry + "\n"`.

| File | Entry |
|---|---|
| `Send2Bindu.csv` | CSV row `[doc_number, amount_raw, customer_name]` (`newline=""`; write a UTF-8 BOM only when creating the file) |
| `Send2Ben.txt` | `{doc_number}` |
| `Send2Accounting.txt` | `{doc_number} - is partially paid and has a TR/SWB note` |
| `PotentialCustomsHold.txt` | `[ERROR] #{doc_number} -  Possible Customs Hold.` |
| `Failed_To_Process.txt` | one of the templates below |

Failed_To_Process.txt templates (`{err}` = `str(exception)`):

```
[ERROR] Row {'inv_number': '...', 'amount_due': '...'} - Malformed row (missing amount column or non-numeric amount).\n{err}\n
{doc_number}                                                      ← MANUAL rows
[ERROR] #{doc} - Carrier Lookup Failed.\n{err}\n
[ERROR] #{doc} -  Can't determine ETA.\n{err}\n
[ERROR] #{doc} - Unexpected error during carrier lookup.\n{err}\n
[ERROR] #{doc} - VShip notification lookup failed, nothing sent\n{err}\n     ← only with ETA_ON_NOTIF_LOOKUP_ERROR=fail
[SKIP] #{doc} - Done in the last week
[SKIP] #{doc} - not found in QuickBooks
[SKIP] #{doc} - duplicate row in input file (line {n})
[ERROR] #{doc} - QB Invoice Processing Error, No Email Sent\n{err}\n
[ERROR] #{doc} - Note Write Error, QB Invoice Processing Successful\n{err}\n
[ERROR] #{doc} - Booking ETA Change Failed, QB Invoice Processing Successful\n{err}\n
[SKIP] #{doc} - already e-mailed today at {HH:MM} (ledger); use --force to resend      ← 10-pipeline.md
```

In dry-run mode none of these files is written (10-pipeline.md); the plan report shows what would be written.

## 15. Deliberate deviations from the original (review list)

| # | Original behaviour | New behaviour | Why |
|---|---|---|---|
| D1 | Empty DocNumber crashed the run | MALFORMED row | Robustness |
| D2 | Header row reported as malformed | First-row header skipped silently | Noise |
| D3 | Duplicate rows processed twice | First occurrence only | Avoid double e-mails |
| D4 | 1-char `a` row silently ignored | MANUAL (reported) | Visibility |
| D5 | Carrier patterns case-sensitive | Base booking upper-cased first | Harmless |
| D6 | Extra text sent to carrier | Clear CarrierLookupError | Same outcome, clearer |
| D7 | Ref pattern `2026\d{6}` | `20\d{8}` | Year-proof |
| D8 | NOTIF1_SENT rows "not have-to-do" logged as "not found in QuickBooks" | "Done in the last week" | Copy-paste bug |
| D9 | Out-of-window row missing in QBO silently skipped | Reported as not found | Visibility |
| D10 | "ETA not found" path | Merged into "Can't determine ETA." | Single contract |
| D11 | DocNumber stays changed if send fails | Best-effort revert | Keeps state consistent |
| D12 | VShip ETA PATCH failure counted as nothing | Counted as failed | Accurate summary |
| D13 | Inconsistent/misleading failure texts | Three unified texts | Greppable |
| D14 | Customer-name failure crashed the run | Empty name + WARNING | Robustness |
| D15 | First `notif #1` comment's date | Most recent `notif #1` date | Matches intent |
| D16 | TR/SWB + VShip lookups for STANDARD rows used `doc[:-1]` (last digit chopped) and NOTIF1_SENT paid rows searched VShip with the suffix still attached | Base booking everywhere | Bug fix |
| D17 | VShip best-match accepted unrelated bookings | Must be exact/prefix/contains match (05 §6) | Avoid editing the wrong booking |
| D18 | Several VShip searches per row | One cached lookup per base booking per run | Speed |
| D19 | Single per-run timestamp not used | `run_now` captured once | Consistency |
| D20 | No confirmation, no dry-run, no ledger | plan → confirm → apply, `--dry-run`, same-day e-mail ledger | Safety (sends customer e-mail) |
| D21 | Several QBO invoices with the same DocNumber → the first one was used | Not processed; `QB Invoice Processing Error, No Email Sent` + the list of invoice Ids (04 §6.3) | Never e-mail from an arbitrary duplicate |
| D22 | UI always clicked Save | An update whose values already match is skipped (no API call) | Same result, fewer calls |

Integration specs add carrier-specific deviations with their own labels (e.g. `CMA-D1…D6` in 08,
Maersk's in 07 §7.6); they follow the same rule: isolated, commented, listed in the spec.

If the user wants strict parity for any item, it is a one-line change in `rules.py`/`pipeline.py`; keep each
deviation isolated and commented with its D-number.

## 16. Quirks intentionally preserved

| # | Quirk |
|---|---|
| O1 | Carrier-local ETA wall time compared with machine-local `run_now` (no tz conversion), as the original did. |
| O2 | Two spaces in `-  Can't determine ETA.` and `-  Possible Customs Hold.` |
| O3 | Subject text `PLEASE PLEASE PAY AND CLEAR THE CARGO` for the first arrival notice. |
| O4 | Pre-arrival in-window invoices are e-mailed on every run until arrival (no cooldown). |
| O5 | Every out-of-window run posts a fresh `ARRIVING ON mm/dd.` VShip note. |
| O6 | Boundary inconsistency: an ETA later today gets the DocNumber `A` (date rule) while the subject says "ESTIMATED ARRIVING" and the note says "ARRIVING". |
| O7 | `notif #2` without `notif #1` counts as 0. |
| O8 | MSC uses only the first container's events (06-carrier-msc.md). |
