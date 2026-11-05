# 13 — Rollout checklist (from API access to the first live run)

This is the **user's** checklist. Nothing here is code; the implementer copies a condensed version into the
program README (11-cli.md §8). Keep the old Playwright tool installed until step 10 is done.

## 0. Lead times — start these first

| Item | Who | Lead time | Needed for |
|---|---|---|---|
| Maersk: add the **"Track & Trace Events"** product to the existing developer.maersk.com app | you | days – ~2 weeks (approval; UNVERIFIED) | Maersk ETAs (07 §2) |
| CMA CGM: register on **api-portal.cma-cgm.com**, copy the API key | you | immediate (30-day free trial) | CMA ETAs (08 §2) |
| CMA CGM: send the *subscription request* before the trial ends | you | contract proposal within 48 working hours, then DocuSign | CMA after day 30 |
| MSC: submit the **Integration Form** on developerportal.msc.com for production access to *DCSA Track and Trace v2.2* (ask for the Unlimited product, the production base URL, and confirmation that `EBKG…` references work) | you | 1–4 weeks (UNVERIFIED) | MSC `api` mode (06 §2.1) |
| MSC interim: nothing to request — paste a token from your own signed-in myMSC browser per run (`eta msc-token`) | you | ~30 s per run | MSC `mymsc` bridge (06 §2.2) |
| Intuit app: add the Playground redirect URI and a Reconnect URL | you | minutes | `eta qbo-auth` (04 §2) |
| QuickBooks: make sure the **ETA** custom field is *active* and *enabled for invoices*; create the test customer "ETA Automation Test" with e-mail = `QB_TEST_SEND_TO` and one $1 test invoice | you | minutes | probes (04 §10, 04b §10) |
| Microsoft 365 app registration with `Mail.Send` (only if the QBO template-swap test fails) | IT admin | hours – days | `QB_EMAIL_STRATEGY=graph` (04b §2) |
| Optional aggregator (ShipsGo) while carrier approvals are pending: company account, API token, credits (1 per new booking; API-usage fee tiers per 09 §B.2) | you | same day | `ETA_PROVIDER_<CARRIER>=aggregator` (09 Part B) — note the first lookup of a newly registered booking may have no data yet |

While an approval is pending, set `ETA_PROVIDER_<CARRIER>=aggregator` (if configured, 09) or leave the
carrier unconfigured: its rows then land in `Failed_To_Process.txt` as "Carrier Lookup Failed." for manual
handling — the same outcome as a failed lookup today.

## 1. Build and test the program

1. Follow `BUILD_PROMPT.md`.
2. `pip install -e .[dev]` and `pytest -q` → all green.
3. `eta --help` works.

## 2. Configure

1. Copy `.env.example` to `.env` (same folder you will run from).
2. Carry over from the old `.env`: `QB_CLIENT_ID`, `QB_CLIENT_SECRET`, `QB_REFRESH_TOKEN`, `QB_REALM_ID`,
   `QB_SANDBOX`, `VSHIP_USER_NAME`, `VSHIP_PASSWORD` (it also feeds MSC's `mymsc-user-email` header, as before).
3. Add the new ones: `MAERSK_CLIENT_ID`/`MAERSK_CLIENT_SECRET`, `CMA_API_KEY`, `QB_TEST_SEND_TO`, …
4. Delete obsolete values: `QB`, `QB_HEADLESS`, `HOME_PAGE`, `LOGIN_URL`, `MSC_PASSWORD` (02 §3). Delete the old session files
   (`auth_for_MSC.json`, `MSCauthToken.json`, `auth_for_VshipCRM.txt`, `auth_for_CMA.json`, the QBO browser profile).
5. If `QB_REFRESH_TOKEN` is stale (the old tool has not run for > 100 days), run `eta qbo-auth`.

## 3. Read-only probes (safe; nothing is written)

Run each and fix problems before moving on — every probe prints what is wrong and where to fix it.

| # | Command | Expect |
|---|---|---|
| 3.1 | `eta probe qbo` | Company name, invoice e-mail template subject, refresh-token expiry date |
| 3.2 | `eta qbo-fields --invoice <a DocNumber whose ETA is set in QBO>` | The ETA field with its `DefinitionId` and `Type` → put the suggested `QB_ETA_DEFINITION_ID` / `QB_ETA_FIELD_TYPE` in `.env` |
| 3.3 | `eta probe vship --booking <a base booking>` | Booking id, customer name, comments, notif status |
| 3.4 | `eta probe maersk --booking <a live 9-digit booking>` | `token.ok: true`, events, ETA, and both `final`/`pod` alternatives |
| 3.5 | `eta probe cma --booking <a live NAM booking>` (**makes 2+ billable calls**) | Events from both endpoints, POD, per-container ETAs |
| 3.6 | `eta probe msc --sandbox` (no credentials) | Expected ETA `2022-12-26` from MSC's public mock sandbox — proves the parser and your network |
| 3.7 | `eta msc-token`, then `eta probe msc --booking <EBKG…>` for 3 known bookings | Same ETAs as myMSC / the old tool; note the token lifetime printed |
| 3.8 | (after MSC API approval) `eta probe msc --compare --booking …` on 20+ bookings | All MATCH → `MSC_MODE=auto` switches to the official API |

## 4. ETA parity check (read-only)

Pick ~20 recent bookings across carriers (include arrived, in-transit, transshipped, multi-container and
inland/IPI moves). Run `eta eta <b1> <b2> …` and compare with the old tool's printed ETA and the carrier
website (opened by hand in a normal browser):

| Booking | Carrier | Old tool ETA | Website ETA | New ETA | Match? | Note |
|---|---|---|---|---|---|---|
| | | | | | | |

Acceptance: every date matches, or the difference is one of the documented deviations
(e.g. Maersk `MAERSK_ETA_TARGET`, CMA-D1 "POD only, never FPD"). Adjust the carrier settings if a
different target matches better (07 §10.2).

## 5. QuickBooks write test (writes to one test invoice only)

1. Sandbox first if available (`QB_SANDBOX=true` with sandbox keys/realm).
2. `eta probe qbo --write-test --invoice <test DocNumber>` (production additionally needs `--yes`) → PASS.
   FAIL means the API cannot write this ETA field: follow 04 §8.1 (string custom field or Ship date) and
   decide with the business before continuing.

## 6. E-mail verification (sends one e-mail to your own test inbox)

1. `eta probe qbo --prefs-roundtrip` → PASS (Preferences can be written back unchanged).
2. `eta probe qbo --send-test --invoice <test DocNumber>` → check the `QB_TEST_SEND_TO` inbox: the subject
   must be **exactly** the probe subject, with the invoice PDF and (if online payments are on) the pay link.
   Answer `y` only if all of that is true — this records the verification the program requires before it
   will send to customers (`QB_EMAIL_REQUIRE_VERIFIED=true`).
3. If it fails: set `QB_EMAIL_STRATEGY=graph` (04b §2 onboarding), repeat 6.2.
4. Tell staff not to e-mail invoices from QuickBooks while a run is in progress (template-swap modifies the
   company e-mail subject for ~1–2 s per send).

## 7. Dry run on a real input file

`eta run <real file>.csv --dry-run` → open `reports/run_<id>.csv`. Spot-check ~20 rows against what the
old tool would have done: route, ETA, in-window flag, subject text, new DocNumber, VShip note text.
Review every warning (e.g. "notif lookup failed — defaulting to send", ambiguous DocNumbers).

## 8. Small live run

`eta run <real file>.csv --limit 5` (or `--only <doc> <doc> …`), answer `y` at the prompt. Then verify:
- QBO: ETA field, DocNumber suffix where expected, invoice shows as sent (or the mailbox Sent Items for graph/smtp);
- the customer e-mail (ask for a BCC copy, e.g. `EMAIL_BCC` or the company's sales-form BCC setting);
- VShip: the new note and the expected-arrival date;
- output files and `Failed_To_Process.txt` entries.

## 9. Full runs

`eta run <file>.csv` (dry-run first whenever the input looks unusual). Compare the run time and results
with the old tool for the first week.

## 10. Ongoing maintenance

- QuickBooks: the program warns 30 days before the refresh token expires → `eta qbo-auth`.
- CMA: keep the subscription active; watch for 429s (then set `CMA_MAX_CALLS_PER_MINUTE`).
- Maersk: rotate keys in the developer portal if needed; update `.env`.
- MSC: until the official API is approved, paste a fresh token before each run (the run prompts for one if needed).
- If a carrier changes its API, run its probe and compare the raw payload with the spec's fixtures.
