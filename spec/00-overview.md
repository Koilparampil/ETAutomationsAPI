# 00 — Overview: ETAutomations without a browser

## 1. The question and the short answer

**Question:** can the ETAutomations pipeline (carrier ETA lookup → QuickBooks invoice update + e-mail →
VShipCRM note + ETA) run without Playwright or any other browser-driving workflow, so that it is faster?

**Answer: yes.** Every browser step in the original has a direct-API replacement. Two of them (QuickBooks
e-mail subject and the QuickBooks ETA date field) have one unverifiable detail each, which the program checks
on first use before it touches a customer. One carrier (MSC) needs a short manual step per run until MSC
approves official API access.

| Step | Original (browser?) | New (no browser) | Status / prerequisite |
|---|---|---|---|
| QuickBooks login | Persistent Chromium profile + password autofill | OAuth refresh token (already used by the original for lookups) | Ready (04 §5) |
| Find invoice, paid / partially paid | One REST query per row + reading the page | One batched query per 25 rows; `Balance`/`TotalAmt` | Ready (04 §6.3) |
| Set ETA custom field, change invoice number | Typing into the QBO web app (`slow_mo=200`, fixed sleeps) | One sparse update per invoice, verified after writing | Date-field write shape **UNVERIFIED** → probe `--write-test`; fallbacks in 04 §8 |
| E-mail the invoice with an ETA subject | "Review and send" dialog, type subject, Send | QBO `/send` with a temporarily swapped template subject (serialized, crash-safe); or company mailbox via Microsoft Graph/SMTP with the QBO PDF + pay link | Must pass the one-time `--send-test` before any customer e-mail (04b §10) |
| MSC ETA | Headless login every 30 min + **new browser per booking** | Official MSC DCSA Track & Trace API (subscription key) — or, until approved, the same GraphQL query over plain HTTP with a token pasted from the user's own signed-in browser | API needs MSC onboarding; `EBKG` support **UNVERIFIED** (06 §2) |
| Maersk ETA | **Headful Chrome per booking** behind Akamai | Official Maersk "Track & Trace Events" API (DCSA 2.2, OAuth client credentials) | Needs product approval on the existing developer app (07 §2) |
| CMA CGM ETA | **Headful Chrome per booking** behind DataDome, sometimes a human CAPTCHA | Official CMA "Logistic Tracking service API" (DCSA 2.2, API key) | Self-service 30-day trial, then a contract; billed per call (08 §2) |
| Grimaldi ETA | Not implemented | Not implemented (parity) — Grimaldi has no public API and is not a DCSA member; possibly via the aggregator | see 09 Part C |
| Any carrier while approval is pending | — | Optional licensed aggregator, **ShipsGo API v2** (1 credit per new booking; asynchronous — a newly registered booking may need a later run before data appears) | see 09 Part B |
| VShipCRM | Already plain HTTP | Same endpoints, async, cached, one lookup per booking | Ready (05) |

Explicitly **not** done (by design): no browser automation of any kind; no workarounds for bot protection
(Akamai, DataDome, CAPTCHAs, TLS-fingerprint impersonation, telemetry replay); no automated myMSC sign-in;
no calls to QuickBooks' private web-app endpoints. Where an official API is not yet available, the program
says so clearly and the row goes to `Failed_To_Process.txt` for manual handling — the same outcome as a
failed lookup today — or to an optional licensed aggregator (09).

## 2. Why it is faster

Per actionable row the original spends roughly:

| Work | Original | New |
|---|---|---|
| Carrier lookup | MSC 4–8 s · Maersk 15–40 s · CMA 10–60 s (browser launch, warm-ups, pop-up timeouts) | ~0.3–1.5 s per HTTP call, 2–4 carrier calls in flight per carrier |
| QuickBooks update + e-mail | ~20–40 s of SPA page loads, `slow_mo=200` ms per action, 400–1 500 ms sleeps | ~1–3 s (1 update + e-mail calls) |
| VShip | 2–4 searches per row | 1 search + 1 comments read per booking, cached |
| Ordering | strictly one row after another | all reads concurrent (plan), writes concurrent across rows (apply) |

For a ~500-row file (the sample in the original's history had 494 rows: ~63% MSC, ~29% Maersk) that is
**several hours today** versus an estimated **10–20 minutes**, most of it the e-mail sends (which are
serialized under the template-swap strategy). These are estimates; the run report records actual timings.

## 3. Architecture

```
            eta run file.csv [--dry-run] [--yes] [--no-email]
                               │
                 ┌─────────────▼──────────────┐
                 │ pipeline.py                │
                 │  Phase 0 preflight (lock,  │
                 │   config, auth)            │
                 │  Phase 1 read + classify   │──── rules.py / texts.py / business_days.py (pure)
                 │  Phase 2 PLAN (reads only, │
                 │   concurrent)              │
                 │  Phase 3 review + confirm  │──── reports/run_<id>.csv|json
                 │  Phase 4 APPLY (writes)    │──── Send2*.txt, Failed_To_Process.txt, ledger.jsonl
                 │  Phase 5 summary           │
                 └──┬──────────┬───────────┬──┘
                    │          │           │
           ┌────────▼───┐ ┌────▼─────┐ ┌───▼───────────────────────────────┐
           │ qbo/       │ │ vship/   │ │ carriers/router.py (memo, breaker)│
           │ auth,      │ │ client,  │ │  msc.py  maersk.py  cma.py        │
           │ client,    │ │ logic    │ │  dcsa.py (shared parser)          │
           │ email      │ │          │ │  grimaldi.py  aggregator.py       │
           └─────┬──────┘ └────┬─────┘ └───┬───────────────────────────────┘
                 │             │           │
          http.py (httpx, retries, rate limits, redaction) · auth.py · token_store.py
```

Key properties:
- **Reads first, writes later.** Nothing changes anywhere until the plan has been shown and confirmed
  (`--dry-run` stops after the plan; `--yes` skips the prompt for scheduled use).
- **Exactly-once e-mail.** No send is ever retried after it may have reached the server; a same-day ledger
  blocks accidental re-sends; the QBO template swap is crash-recoverable.
- **Parity.** Every decision and text string of the original is specified in 01 with golden values taken by
  running the original code; every change is a labelled, reviewable deviation (D1…, CMA-D1…).
- **Configurable per carrier.** `ETA_PROVIDER_<CARRIER>` = native | aggregator | off, so a carrier whose API
  access is pending never blocks the others.

## 4. Biggest risks and how the design handles them

| Risk | Handling |
|---|---|
| QBO API cannot write the ETA **date** custom field in production (reported by others) | Every write is verified by reading back; `eta probe qbo --write-test` before go-live; fallbacks: string custom field or Ship date (04 §8.1) |
| QBO `/send` has no subject parameter | Template-swap with recovery file, lock, restore-in-finally and a mandatory human-confirmed send test; Graph/SMTP mailbox fallback (04b) |
| MSC official API may not accept `EBKG…` references / onboarding takes weeks | Token bridge keeps today's exact GraphQL query working; `eta probe msc --compare` validates the API before switching (06 §10) |
| Maersk product approval pending | Maersk rows fail cleanly ("not configured") or use the aggregator (09) |
| CMA trial ends / per-call billing | One call per booking per run (memo), 404s never retried, probe warns before billable calls (08) |
| Behaviour drift from the original | Golden tests (12), deviations list (01 §15), parity spot-check (13 §4, §7) |
| Concurrent runs | OS-level single-instance lock (03 §14) |

## 5. Reading order and precedence

1. `00-overview.md` (this file)
2. `01-business-rules.md` — what the program decides (authoritative for behaviour and texts)
3. `02-configuration.md` — every environment variable, local state files, secrets rules
4. `03-foundations.md` — shared contracts (authoritative for names, types, errors, HTTP/auth conventions)
5. `04-quickbooks-online.md`, `04b-invoice-email-delivery.md`
6. `05-vship-crm.md`
7. `06-carrier-msc.md`, `07-carrier-maersk.md`, `08-carrier-cma-cgm.md`, `09-dcsa-aggregators-grimaldi.md`
8. `10-pipeline.md`, `11-cli.md`
9. `12-testing.md`, `13-rollout-checklist.md`, `14-original-code-map.md`

Precedence for the implementer is stated in `BUILD_PROMPT.md`: 03 for names/contracts, 01 for behaviour,
04–09 for integration details, 10 for orchestration.

## 6. Provenance

Derived from the `ETAutomations` repository (branch `addingCMA`, commit `1fb01ac`, 2026-10-15) and from
public documentation and unauthenticated observations of the carriers' and Intuit's APIs on 2026-11-05.
Each integration spec ends with a "Sources & verification status" table; anything marked UNVERIFIED is
covered by a probe command that the user runs before the first live run (13).
