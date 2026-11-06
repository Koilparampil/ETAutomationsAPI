# ETAutomationsAPI — `eta`

`eta` is a browser-free rebuild of the [ETAutomations](https://github.com/Koilparampil/ETAutomations) tool. It
reads a CSV of QuickBooks invoice numbers and amounts and looks up each booking's ETA through the carriers'
official tracking APIs (MSC, Maersk and CMA CGM; Grimaldi only through an optional tracking aggregator). It
checks the booking's notification history in VShipCRM and plans what to do with each row. Then it shows you that
plan and asks for confirmation. Only after you answer `y` does it update the invoice's ETA custom field in
QuickBooks Online (adding the `A` arrival suffix to the invoice number once a shipment has arrived), e-mail the
invoice to the customer with the ETA subject line, add the matching note and expected-arrival date in VShipCRM,
and write the rows that need a person to the familiar `Send2*.txt` / `Failed_To_Process.txt` files. Every step
is a direct HTTPS call instead of a browser session, so a file of about 500 rows should take minutes instead of
hours (an estimate; the run report records the real timings).

Contents: [Requirements](#requirements) · [Install](#install) · [Configuration](#configuration) ·
[First-time setup checklist](#first-time-setup-checklist) · [Daily usage](#daily-usage) ·
[Output files](#output-files) · [Troubleshooting](#troubleshooting) · [Safety notes](#safety-notes) ·
[Running the tests](#running-the-tests) · [About the specification](#about-the-specification)

## Requirements

- **Python 3.12 or newer** (developed and fully tested on Python 3.13; see `IMPLEMENTATION_NOTES.md` for the
  3.12 status). Windows, macOS or Linux.
- **No browser.** No Playwright, Chromium or Chrome is installed or driven; nothing tries to get around bot
  protection. Dependencies: `httpx`, `python-dotenv`, `pandas`, `tzdata` (installed by `pip`).
- HTTPS access to QuickBooks Online, VShipCRM and the carrier APIs. A proxy is used when the standard
  `HTTPS_PROXY` variable is set.
- Tkinter only for the file picker of double-click mode (it comes with the python.org Windows installer).
  Without it, pass the CSV path on the command line.
- The accounts and API access listed in the [first-time setup checklist](#first-time-setup-checklist).

## Install

```sh
git clone <this repository> etautomationsapi
cd etautomationsapi
python -m venv .venv               # Windows: py -m venv .venv
source .venv/bin/activate          # Windows cmd: .venv\Scripts\activate   PowerShell: .venv\Scripts\Activate.ps1
pip install -e ".[dev]"            # program plus test tools; or `pip install .` for the program only
eta --help
```

The `eta` command lives in the virtual environment (`.venv/bin/eta`, or `.venv\Scripts\eta.exe` on Windows).
`python -m eta_automations …` is the same program. On Windows, `scripts\run_eta.bat` starts it from the
repository folder with the `.venv` Python; double-clicking it opens a file picker
([double-click mode](#double-click-mode-windows)).

## Configuration

1. Copy the example file and fill it in (it lists every setting with its default and a one-line comment):

   ```sh
   cp .env.example .env               # Windows: copy .env.example .env
   ```

2. Fill in at least the **required** values below. `.env` is read from the folder you run `eta` from; every
   command except `version` also accepts `--env-file PATH`.
3. Rules worth knowing:
   - Save `.env` as UTF-8 text. A file saved as "ANSI" with an accented character, or as UTF-16 (Notepad's
     "Unicode", or Windows PowerShell's `>` / `Out-File`), stops every command with exit code 2.
   - Values in `.env` **override** variables set in the operating system, as in the original tool.
   - An empty value (`NAME=`) counts as unset. Because `.env` wins, delete a `NAME=` line if you set that
     variable in the operating system instead.
   - Yes/no values accept `1/true/yes/on` or `0/false/no/off`.
   - A malformed value stops every command with exit code 2 and names the variable, even for a service you do
     not use. A missing credential only disables its service; `eta run` names every missing variable of
     QuickBooks and VShip (`Missing required settings: … Set them in .env (see .env.example).`).
   - Secrets live only in `.env` and in `tokens.json` under `ETA_STATE_DIR`. Never commit `.env` (it is in
     `.gitignore`). The program writes to `.env` only `QB_REFRESH_TOKEN` (every rotated token) and, during
     `eta qbo-auth`, `QB_REALM_ID` when it is empty.
   - Delete the obsolete variables of the old tool: `QB`, `QB_HEADLESS`, `HOME_PAGE`, `LOGIN_URL`,
     `MSC_PASSWORD`. They are ignored.

**What you must set**

| Variable(s) | Needed for |
|---|---|
| `QB_CLIENT_ID`, `QB_CLIENT_SECRET`, `QB_REALM_ID`, `QB_REFRESH_TOKEN` | QuickBooks Online (required by `eta run`); carry them over from the old `.env`, or run `eta qbo-auth` |
| `VSHIP_USER_NAME`, `VSHIP_PASSWORD` | VShipCRM (required by `eta run`) |
| `MAERSK_CLIENT_ID`, `MAERSK_CLIENT_SECRET` | Maersk ETAs |
| `CMA_API_KEY` (or `CMA_CLIENT_ID` + `CMA_CLIENT_SECRET`) | CMA CGM ETAs |
| nothing (token pasted per run with `eta msc-token`), or `MSC_API_BASE_URL` + `MSC_API_SUBSCRIPTION_KEY` | MSC ETAs: myMSC token bridge until MSC approves official API access, then the official API |
| `QB_TEST_SEND_TO` | The one-time e-mail verification (`eta probe qbo --send-test --invoice <test DocNumber>`) |
| `QB_ETA_DEFINITION_ID` (recommended) | Identifies the ETA custom field (a hint; the field name is the fallback); `eta qbo-fields --invoice <DocNumber>` prints the line to paste |

A carrier without credentials is simply "not configured": its rows go to `Failed_To_Process.txt` as
`Carrier Lookup Failed.`, the same outcome as a failed lookup today.

### All settings

The tables below mirror `spec/02-configuration.md` and the defaults in `src/eta_automations/config.py`;
`.env.example` has the same list. "—" means no default (unset).

<details>
<summary><b>Run settings</b> (spec 02 §1)</summary>

| Variable | Default | Meaning |
|---|---|---|
| `ETA_OUTPUT_DIR` | the folder you run `eta` from | Where `Send2Bindu.csv`, `Send2Ben.txt`, `Send2Accounting.txt`, `Failed_To_Process.txt`, `PotentialCustomsHold.txt`, `ToEmail_<run_id>.csv`, `reports/` and `logs/` are written |
| `ETA_STATE_DIR` | Windows `%LOCALAPPDATA%\ETAutomationsAPI`; else `$XDG_STATE_HOME/eta_automations` or `~/.local/state/eta_automations` | `tokens.json`, `ledger.jsonl`, `run.lock`, `probe/` and the e-mail state files; keep it outside any git repository |
| `ETA_MIN_INVOICE_AMOUNT` | `1000` | Rows with a lower amount go to `Send2Bindu.csv` (`$` and `,` allowed) |
| `ETA_WINDOW_BUSINESS_DAYS` | `6` | Notification window in US business days |
| `ETA_ON_NOTIF_LOOKUP_ERROR` | `send` | VShip notification lookup failed: `send` (e-mail anyway, as the original did) or `fail` (row to `Failed_To_Process.txt`) |
| `ETA_MAX_PARALLEL_BOOKINGS` | `8` | Rows processed concurrently (`eta run --max-parallel` overrides it) |
| `ETA_CARRIER_FAILURE_LIMIT` | `5` | Identical consecutive failures of one carrier before its remaining lookups are skipped (circuit breaker) |
| `ETA_LOG_LEVEL` | `INFO` | Console log level (`--log-level` overrides it; the log file is always DEBUG) |
| `ETA_PROVIDER_MSC` | `native` | Which client serves MSC rows: `native`, `aggregator` or `off` |
| `ETA_PROVIDER_MAERSK` | `native` | Same for Maersk |
| `ETA_PROVIDER_CMA` | `native` | Same for CMA CGM |
| `ETA_PROVIDER_GRIMALDI` | `off` | Grimaldi has no public API: `off`/`native` keep the original "not yet implemented" message; `aggregator` tries ShipsGo |

</details>

<details>
<summary><b>QuickBooks Online</b> (spec 04 §3, 04b §3)</summary>

| Variable | Default | Meaning |
|---|---|---|
| `QB_CLIENT_ID` | — (required) | Intuit app Client ID |
| `QB_CLIENT_SECRET` | — (required) | Intuit app Client Secret |
| `QB_REALM_ID` | — (required) | Company (realm) id; `eta qbo-auth` fills it in when it is empty |
| `QB_REFRESH_TOKEN` | — (first run) | OAuth refresh token; each rotated value is written back here and to `tokens.json` |
| `QB_SANDBOX` | `false` | Use the QuickBooks sandbox API host (with sandbox keys and realm) |
| `QB_MINOR_VERSION` | `75` | API minor version sent on every call |
| `QB_ETA_FIELD_NAME` | `ETA` | Name of the invoice custom field that holds the ETA (case-insensitive) |
| `QB_ETA_DEFINITION_ID` | — | Hint: the field's `DefinitionId`; confirm it with `eta qbo-fields` (the old tool's `20000000000000150677` is probably not the REST id) |
| `QB_ETA_FIELD_TYPE` | `DateType` | Field type used only when no invoice in the run shows it |
| `QB_ETA_STRING_FORMAT` | `%m/%d/%Y` | ETA format if the field is a `StringType` |
| `QB_VERIFY_UPDATES` | `true` | Re-read each invoice after writing to confirm the ETA and DocNumber. Keep it on |
| `QB_INCLUDE_ON_WRITES` | `true` | Request the enhanced custom fields on update calls too |
| `QB_QUERY_CHUNK_SIZE` | `25` | DocNumbers per `IN (…)` query |
| `QB_MAX_CONCURRENCY` | `4` | Concurrent QuickBooks calls |
| `QB_MAX_CALLS_PER_MINUTE` | `450` | Below Intuit's 500 requests/minute per company |
| `QB_MAX_CALLS_PER_SECOND` | `8` | Below Intuit's 10 requests/second per company and app |
| `QB_DOCNUMBER_MAX_LEN` | `21` | Longest DocNumber accepted; a longer suffixed number is refused before any write |
| `QB_REDIRECT_URI` | `https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl` | Redirect URI for `eta qbo-auth` (must be registered on the app) |
| `QB_OAUTH_SCOPES` | `com.intuit.quickbooks.accounting` | Scopes requested by `eta qbo-auth` |
| `QB_EMAIL_STRATEGY` | `template_swap` | How invoice e-mails are sent: `template_swap` (QuickBooks sends, with a temporarily swapped subject), `graph` or `smtp` (company mailbox) |
| `QB_EMAIL_REQUIRE_VERIFIED` | `true` | No customer e-mail until the `--send-test` probe (`eta probe qbo`) recorded a verified test send. Keep it on |
| `QB_EMAIL_REDIRECT_TO` | — | Shadow mode: every invoice e-mail goes to this address instead of the customer (`graph`/`smtp`; see 04b §3). With `template_swap` it works only with `QB_SANDBOX=true` or for invoices whose BillEmail already is this address; any other row to e-mail fails in the plan (`QB_EMAIL_REDIRECT_TO cannot be used with template_swap on real invoices …`) before its invoice is changed. Only the recipients change: the QuickBooks ETA and DocNumber suffix, the VShip notes and ETA and the ledger are still written for real and record the notice as sent, so use it with `QB_SANDBOX=true` or on test bookings only |
| `QB_TEST_SEND_TO` | — | Inbox of the internal test customer, used by the `--send-test` probe of `eta probe qbo` |
| `QB_PREFS_UPDATE_MODE` | `full` | How Preferences are written: `full`, or `sparse` only after `--prefs-roundtrip` passes with it |
| `QB_MARK_SENT` | `true` | `graph`/`smtp`: mark the invoice as sent in QuickBooks (best effort) |
| `QB_EMAIL_MAX_PER_MINUTE` | `25` | `graph`/`smtp`: e-mails per minute |
| `QB_EMAIL_MAX_CONCURRENCY` | `2` | `graph`/`smtp`: parallel sends (`template_swap` always sends one at a time) |

</details>

<details>
<summary><b>Company-mailbox e-mail</b>, only for <code>QB_EMAIL_STRATEGY=graph</code> or <code>smtp</code> (spec 04b §3)</summary>

| Variable | Default | Meaning |
|---|---|---|
| `EMAIL_FROM` | — (required for graph/smtp) | Sending mailbox |
| `EMAIL_REPLY_TO` | — | Reply-To address |
| `EMAIL_BCC` | — | Extra BCC recipients, comma-separated (e.g. an archive mailbox) |
| `EMAIL_BODY_TEMPLATE` | — | Text file with the body; default: the QuickBooks invoice e-mail template message |
| `EMAIL_INCLUDE_PAY_LINK` | `true` | Add the QuickBooks online-payment link when there is one |
| `EMAIL_ATTACH_PDF` | `true` | Attach the invoice PDF from QuickBooks |
| `EMAIL_GRAPH_TENANT_ID` / `EMAIL_GRAPH_CLIENT_ID` / `EMAIL_GRAPH_CLIENT_SECRET` | — / — / — (required for graph) | Microsoft 365 app registration with the `Mail.Send` application permission |
| `EMAIL_SMTP_HOST` / `EMAIL_SMTP_USERNAME` / `EMAIL_SMTP_PASSWORD` | — / — / — (required for smtp) | SMTP server and login (the password is used exactly as typed) |
| `EMAIL_SMTP_PORT` | `587` | SMTP port |
| `EMAIL_SMTP_SECURITY` | `starttls` | `starttls`, `ssl` or `none` |

</details>

<details>
<summary><b>VShipCRM</b> (spec 05 §3)</summary>

| Variable | Default | Meaning |
|---|---|---|
| `VSHIP_USER_NAME` | — (required) | VShipCRM user name (e-mail); also the default `MSC_USER_EMAIL` |
| `VSHIP_PASSWORD` | — (required) | VShipCRM password (used exactly as typed) |
| `VSHIP_API_BASE` | `https://vship2000-prod-api.azurewebsites.net/api` | API root |
| `VSHIP_MAX_CONCURRENCY` | `4` | Concurrent VShip calls |
| `VSHIP_TOKEN_TTL_MINUTES` | `55` | Token lifetime to assume when the token carries no expiry |
| `VSHIP_COMMENT_TYPE` | `1` | `commentType` of the notes the program posts (1 = internal comment) |

</details>

<details>
<summary><b>MSC</b> (spec 06 §3)</summary>

`MSC_MODE=auto` uses the official API when both `MSC_API_BASE_URL` and `MSC_API_SUBSCRIPTION_KEY` are set, and
otherwise the myMSC token bridge (a token you paste from your own signed-in browser with `eta msc-token`).

| Variable | Default | Meaning |
|---|---|---|
| `MSC_MODE` | `auto` | `auto`, `mymsc` (token bridge) or `api` (official DCSA Track & Trace API) |
| `MSC_ACCESS_TOKEN` | — | Bridge token; optional, because `eta msc-token` stores one in `tokens.json` |
| `MSC_USER_EMAIL` | `VSHIP_USER_NAME`, then `MSC_USER_NAME` | `mymsc-user-email` header; must equal your myMSC sign-in e-mail |
| `MSC_USER_ROLES` | `Customer` | `mymsc-user-roles` header |
| `MSC_GRAPHQL_URL` | `https://services.mymsc.com/tracking/graphql` | myMSC GraphQL endpoint (token bridge) |
| `MSC_TOKEN_TTL_SECONDS` | `1800` | Bridge-token lifetime to assume when the token shows no expiry |
| `MSC_RATE_PER_MINUTE` | `120` | Token bridge: GraphQL calls per minute (0 = no limit) |
| `MSC_API_BASE_URL` | — (api mode) | Production base URL from MSC onboarding, e.g. `https://<host>/dpo/trackandtrace/v2.2` |
| `MSC_API_SUBSCRIPTION_KEY` | — (api mode) | Sent as `Ocp-Apim-Subscription-Key` |
| `MSC_API_RATE_PER_MINUTE` | `0` | Official API calls per minute (0 = no limit; 5 on the Starter product) |
| `MSC_API_CUSTOMS_HOLD_RULE` | `true` | api mode: apply the approximated customs-hold rule (06 §7.3) |
| `MSC_ALLOW_SANDBOX` | `false` | Allow the public mock sandbox as base URL outside `eta probe msc --sandbox` (tests only) |
| `MSC_RESCUE_FALLBACK` | `false` | Rescue-only fallbacks of 06 §7.5 (off for safety; flagged in the report when used) |
| `MSC_MAX_CONCURRENCY` | `4` | Concurrent MSC calls |
| `MSC_TIMEOUT_SECONDS` | `30` | Read timeout (the connect timeout stays 10 s) |
| `MSC_USER_NAME` | — | Legacy: only the last fallback for `MSC_USER_EMAIL` |

</details>

<details>
<summary><b>Maersk</b> (spec 07 §3)</summary>

Configured only when both credentials are set.

| Variable | Default | Meaning |
|---|---|---|
| `MAERSK_CLIENT_ID` | — (required for Maersk) | Consumer Key of your developer.maersk.com app |
| `MAERSK_CLIENT_SECRET` | — (required for Maersk) | App secret |
| `MAERSK_BASE_URL` | `https://api.maersk.com` | API gateway (`https://api-stage.maersk.com` only if Maersk provisions your app there) |
| `MAERSK_TOKEN_URL` | `https://api.maersk.com/customer-identity/oauth/v2/access_token` | OAuth2 token endpoint |
| `MAERSK_API_VERSION` | — | Sent as the `API-Version` header only when set; leave unset unless Maersk support asks |
| `MAERSK_ETA_TARGET` | `final` | `final` = arrival at the last location (the maersk.com "Arrives" date); `pod` = vessel arrival at the port of discharge |
| `MAERSK_MULTI_CONTAINER_POLICY` | `first` | Which container decides: `first` (original behaviour) or `latest` |
| `MAERSK_ETA_DATE_ONLY` | `true` | Truncate the ETA to its date (00:00 local time) |
| `MAERSK_PAGE_LIMIT` | `100` | Events per page (1–100) |
| `MAERSK_MAX_PAGES` | `10` | Safety cap on pages per booking |
| `MAERSK_MAX_CONCURRENCY` | `3` | Concurrent Maersk calls |
| `MAERSK_RATE_PER_MINUTE` | `0` | Calls per minute (0 = no limit); set it only if HTTP 429 appears |
| `MAERSK_TIMEOUT_SECONDS` | `30` | Read timeout (the connect timeout stays 10 s) |

</details>

<details>
<summary><b>CMA CGM</b> (spec 08 §3)</summary>

Configured with an API key (public tier, enough for the ETA) or an OAuth client id + secret. **CMA bills every
API call**, including 200 and 404 answers.

| Variable | Default | Meaning |
|---|---|---|
| `CMA_API_KEY` | — (key, or id + secret) | API key from api-portal.cma-cgm.com, sent as the `KeyId` header |
| `CMA_CLIENT_ID` | — | Private-tier OAuth2 client id (mailed by CMA after the contract) |
| `CMA_CLIENT_SECRET` | — | Private-tier OAuth2 client secret |
| `CMA_AUTH_MODE` | `auto` | `auto` = `oauth` when id and secret are set, else `apikey`; or force `apikey` / `oauth` |
| `CMA_OAUTH_SCOPE` | `tandtcommercial:read:be tandtpublic:read:be` | Space-separated OAuth scopes |
| `CMA_TOKEN_URL` | `https://auth.cma-cgm.com/as/token.oauth2` | OAuth2 token endpoint (https only) |
| `CMA_BASE_URL` | `https://apis.cma-cgm.net/operation/trackandtrace/v1` | API base (https only) |
| `CMA_LOOKUP_MODE` | `reference` | `reference` = `GET /events/{booking}` (CMA's recommendation); `dcsa_query` = `GET /events?carrierBookingReference=…` |
| `CMA_PAGE_LIMIT` | `500` | Events per page (1–500) |
| `CMA_MAX_PAGES` | `10` | Safety cap on pages per lookup |
| `CMA_BEHALF_OF` | — | Partner code of the end customer; third parties only |
| `CMA_MAX_CONCURRENCY` | `2` | Concurrent CMA calls |
| `CMA_MAX_CALLS_PER_MINUTE` | — | Proactive calls-per-minute limit; set it (e.g. 20) if HTTP 429 appears |
| `CMA_MAX_RETRY_AFTER_S` | `120` | Longest `Retry-After` (seconds) waited out; longer fails the lookup, and every other CMA lookup of the run fails at once with the same message until that window has passed (no CMA call is made inside it) |
| `CMA_TIMEOUT_S` | `30` | Read timeout (the connect timeout stays 10 s) |

</details>

<details>
<summary><b>Optional aggregator (ShipsGo API v2)</b> (spec 09 Part B)</summary>

Enabled only when `AGG_API_TOKEN` is set, and used only for carriers set to `ETA_PROVIDER_<CARRIER>=aggregator`.
Paid service: 1 credit per newly registered booking. The first lookup of a newly registered booking may have no
data yet ("re-run later").

| Variable | Default | Meaning |
|---|---|---|
| `AGG_API_TOKEN` | — (to enable) | ShipsGo API token |
| `AGG_PROVIDER` | `shipsgo` | Backend; only `shipsgo` is implemented |
| `AGG_BASE_URL` | `https://api.shipsgo.com/v2` | API base |
| `AGG_MAX_CONCURRENCY` | `2` | Concurrent calls |
| `AGG_RATE_PER_MINUTE` | `90` | Calls per minute (ShipsGo allows 100 per company) |
| `AGG_TIMEOUT_SECONDS` | `30` | Read timeout |
| `AGG_ALLOW_CREATE` | `true` | Register untracked bookings (1 credit each); `false` = only read bookings already tracked |
| `AGG_MAX_CREATES_PER_RUN` | `100` | Credit safety valve per run |
| `AGG_FIRST_LOOKUP_WAIT_S` | `45` | How long a lookup waits for data on a freshly registered booking (0 = no wait) |
| `AGG_POLL_INTERVAL_S` | `15` | Seconds between polls while waiting |
| `AGG_FRESH_MINUTES` | `30` | Bookings first seen less than this many minutes ago count as fresh |
| `AGG_SCAC_MSC` / `AGG_SCAC_MAERSK` / `AGG_SCAC_CMA` / `AGG_SCAC_GRIMALDI` | `MSCU` / `MAEU` / `CMDU` / `GRIU` | Carrier codes sent to ShipsGo (Grimaldi's is unverified: check `eta probe aggregator`) |

</details>

## First-time setup checklist

Condensed from `spec/13-rollout-checklist.md`, which has the details. Keep the old Playwright tool installed
until step 9 runs well.

**0. Start the access requests first (they have lead times).**

| Item | Lead time | Needed for |
|---|---|---|
| Maersk: add the **"Track & Trace Events"** product to your developer.maersk.com app | days to ~2 weeks (unverified) | Maersk ETAs |
| CMA CGM: register on **api-portal.cma-cgm.com** and copy the API key; send the subscription request before the 30-day free trial ends | key at once; contract proposal within 48 working hours | CMA ETAs |
| MSC: submit the **Integration Form** on developerportal.msc.com for *DCSA Track and Trace v2.2* (ask for the Unlimited product, the production base URL, and confirmation that `EBKG…` references work) | 1–4 weeks (unverified) | MSC `api` mode. Until then, paste a token from your own signed-in myMSC browser each run (`eta msc-token`, ~30 s) |
| Intuit app: add the Playground redirect URI (`QB_REDIRECT_URI`) and a Reconnect URL | minutes | `eta qbo-auth` |
| QuickBooks: make sure the **ETA** custom field is active and enabled for invoices; create the test customer "ETA Automation Test" with e-mail = `QB_TEST_SEND_TO` and one $1 test invoice | minutes | the probes |
| Microsoft 365 app registration with `Mail.Send` (only if the template-swap e-mail test fails) | hours to days (IT admin) | `QB_EMAIL_STRATEGY=graph` |
| Optional ShipsGo account, API token and credits, while carrier approvals are pending | same day | `ETA_PROVIDER_<CARRIER>=aggregator` |

While an approval is pending, leave that carrier unconfigured (its rows go to `Failed_To_Process.txt` as
`Carrier Lookup Failed.` for manual handling) or route it through the aggregator.

**1. Install and test.** `pip install -e ".[dev]"`, `pytest -q` (all green), `eta --help`.

**2. Configure.** Copy `.env.example` to `.env`. Carry over from the old `.env`: `QB_CLIENT_ID`,
`QB_CLIENT_SECRET`, `QB_REFRESH_TOKEN`, `QB_REALM_ID`, `QB_SANDBOX`, `VSHIP_USER_NAME`, `VSHIP_PASSWORD`. Add
the new ones (`MAERSK_CLIENT_ID`/`MAERSK_CLIENT_SECRET`, `CMA_API_KEY`, `QB_TEST_SEND_TO`, …). Delete `QB`,
`QB_HEADLESS`, `HOME_PAGE`, `LOGIN_URL` and `MSC_PASSWORD`, and the old session files (`auth_for_MSC.json`,
`MSCauthToken.json`, `auth_for_VshipCRM.txt`, `auth_for_CMA.json`, the QuickBooks browser profile). If the old
tool has not run for more than 100 days, the refresh token is stale: run `eta qbo-auth`.

**3. Read-only probes** (nothing is written; each prints what is wrong and where to fix it):

| # | Command | Expect |
|---|---|---|
| 3.1 | `eta probe qbo` | Company name, invoice e-mail template subject, refresh-token expiry date |
| 3.2 | `eta qbo-fields --invoice <a DocNumber whose ETA is set in QuickBooks>` | The ETA field with its `DefinitionId` and type; paste the printed `QB_ETA_DEFINITION_ID` / `QB_ETA_FIELD_TYPE` lines into `.env` |
| 3.3 | `eta probe vship --booking <a base booking>` | Booking id, customer name, comments, notification status |
| 3.4 | `eta probe maersk --booking <a live 9-digit booking>` | Token OK, events, ETA, and both the `final` and `pod` alternatives |
| 3.5 | `eta probe cma --booking <a live NAM booking>` (**2+ billable calls**) | Events from both endpoints, the POD, per-container ETAs |
| 3.6 | `eta probe msc --sandbox` (no credentials) | ETA `2022-12-26` from MSC's public mock sandbox: proves the parser and your network |
| 3.7 | `eta msc-token`, then `eta probe msc --booking <EBKG…>` for 3 known bookings | The same ETAs as myMSC and the old tool; note the token lifetime printed |
| 3.8 | after MSC API approval: `eta probe msc --compare --booking <EBKG…> --booking …` on 20+ bookings | All MATCH, then `MSC_MODE=auto` uses the official API |

**4. ETA parity check.** Run `eta eta <b1> <b2> …` for ~20 recent bookings across carriers (arrived, in transit,
transshipped, multi-container, inland moves). Compare with the old tool and with the carrier website opened by
hand. Every date should match or differ only by a documented deviation (e.g. `MAERSK_ETA_TARGET`, CMA "POD
only").

**5. QuickBooks write test** (one test invoice only; sandbox first with `QB_SANDBOX=true`):
`eta probe qbo --write-test --invoice <test DocNumber>`, adding `--yes` on production. It must print PASS. FAIL
means the API cannot write this ETA field: see spec 04 §8.1 and decide with the business before going on.

**6. E-mail verification** (one e-mail to your own test inbox):
1. `eta probe qbo --prefs-roundtrip` → PASS: on the sandbox first (`QB_SANDBOX=true`), then on production with
   `--yes`. A FAIL at its step 1 lists the company settings QuickBooks changed; restore them by hand.
2. `eta probe qbo --send-test --invoice <test DocNumber>` → check the `QB_TEST_SEND_TO` inbox. The subject must be
   **exactly** the probe subject, with the invoice PDF and (if online payments are on) the pay link. Answer `y`
   only if all of that is true. This records the verification that real runs require before any customer
   e-mail.
3. If it fails, set `QB_EMAIL_STRATEGY=graph` (spec 04b §2) and repeat step 2.
4. Tell staff not to e-mail invoices from QuickBooks while a run is in progress: `template_swap` changes the
   company's invoice e-mail subject for 1–2 seconds per send.

**7. Dry run on a real file.** `eta run <file>.csv --dry-run`, then open `reports/run_<run_id>.csv` and
spot-check ~20 rows against what the old tool would have done (route, ETA, in-window flag, subject, new
DocNumber, VShip note). Review every warning.

**8. Small live run.** `eta run <file>.csv --limit 5` (or `--only <doc> <doc> …`) and answer `y`. Check the
QuickBooks ETA field, the DocNumber suffix, the sent status, the customer e-mail (ask for a BCC copy), the VShip
note and expected-arrival date, and the output files.

**9. Full runs.** `eta run <file>.csv` (dry run first whenever the input looks unusual). Compare run time and
results with the old tool for the first week.

**10. Ongoing.** The program warns 30 days before the QuickBooks connection expires (run `eta qbo-auth`). Keep
the CMA subscription active. Rotate Maersk keys in the developer portal when needed. Until MSC's API is approved,
paste a fresh MSC token before each run (the run asks for one when needed). If a carrier changes its API, run its
probe and compare the saved payload with the spec's fixtures.

Every item the specification could not confirm without your credentials, with the exact probe command, is in
`IMPLEMENTATION_NOTES.md` (section D). Work through it before the first live run.

## Daily usage

```sh
eta msc-token                       # only while MSC uses the token bridge (the run also asks when needed)
eta run invoices.csv --dry-run      # plan only: nothing is written anywhere; the plan report is saved
eta run invoices.csv                # plans again, shows the plan, asks before changing anything
```

What a run does:

1. **Preflight.** Takes the run lock (not in a dry run), checks the settings, and signs in to QuickBooks and
   VShip. If MSC rows are present in token-bridge mode and the stored token has less than 10 minutes left, it
   asks for a fresh token (hidden input) on a terminal, and stops with exit code 2 otherwise.
2. **Plan** (reads only, many rows in parallel): reads the CSV, looks up every ETA, the VShip notification
   status and the QuickBooks invoice, and decides each row's route.
3. **Review.** Checks the e-mail path (and repairs a QuickBooks e-mail template left modified by a crashed run),
   writes `reports/run_<run_id>.csv` and `.json`, prints the plan summary (counts per route, every
   e-mail with its new DocNumber, subject and recipients, every warning), then asks
   `Apply <n> change(s) to <n> invoice(s) and send <n> e-mail(s)? [y/N]`. Anything but `y`/`yes` aborts with
   nothing changed. Without a terminal, `--yes` is required.
4. **Apply.** QuickBooks updates and e-mails, VShip notes and ETAs, output files, ledger.
5. **Report.** Final summary, with identical failures grouped (e.g. "MSC: 287 bookings failed — …"), and the
   paths of the report and the log file.

Useful `eta run` options:

| Option | Meaning |
|---|---|
| `INPUT_CSV` | The input file (`doc_number,amount`, no header needed; a `Num,Amount` style header row is skipped). Omitted: a file picker opens |
| `--dry-run` | Plan only: every read happens, nothing is written anywhere (no QuickBooks/VShip writes, no e-mails, no output files, no ledger, no aggregator registration or credit); the plan report is still saved. A booking routed to the aggregator that is not tracked there yet shows `… is not registered with ShipsGo yet — a real run registers it (1 credit)` |
| `--yes`, `-y` | Skip the confirmation prompt |
| `--force` | Ignore the same-day e-mail ledger guard |
| `--no-email` | Never send e-mail: e-mail rows only get their QuickBooks ETA update and VShip ETA; the e-mails to send by hand are listed in `ToEmail_<run_id>.csv` |
| `--only DOC [DOC ...]` | Process only rows whose DocNumber or base booking matches (exact, case-insensitive). Put `INPUT_CSV` before `--only` |
| `--limit N` | Process only the first N rows (after `--only`) |
| `--max-parallel N` | Override `ETA_MAX_PARALLEL_BOOKINGS` for this run |
| `--env-file PATH` | Use this `.env` instead of `./.env` |
| `--log-level LEVEL` | Console log level (the log file is always DEBUG) |

### Double-click mode (Windows)

Double-click `scripts\run_eta.bat`, or run `eta` with no arguments. A small "ETA Tool" window asks for the CSV
file, the normal run follows (including the confirmation prompt), and at the end the window shows
`Press ENTER to close this window...`. The batch file starts in the repository folder, so `.env` must be there,
and the output files go there unless `ETA_OUTPUT_DIR` is set. Credentials are never asked for in the window;
they come from `.env`.

### Commands

| Command | What it does |
|---|---|
| `eta run [INPUT_CSV] [options]` | The full pipeline (above) |
| `eta eta BOOKING [BOOKING ...] [--json]` | ETA lookup only, nothing written: carrier, ETA, source, actual/estimated, in-window flag (with the current cutoff), or the error. Exits 0 even if lookups fail. A booking routed to the aggregator may be registered there (1 credit) |
| `eta probe SERVICE [--booking B ...] [--invoice DOC] [flags]` | Diagnostics for `qbo`, `vship`, `msc`, `maersk`, `cma`, `aggregator` or `all` (below) |
| `eta qbo-auth [--listen] [--port N] [--no-browser]` | Connect QuickBooks (OAuth) in your own browser and store the refresh token in `tokens.json` and `.env`. Default: paste-back flow (works with production keys); `--listen` uses a one-shot `http://localhost:<port>/callback` listener (development/sandbox keys only; default port 8765) |
| `eta msc-token` | Paste (hidden input) an MSC token copied from your own signed-in myMSC browser session; stores it and prints its status (time left, `iss`/`aud`/`exp`). An empty or already-expired token exits 2 |
| `eta qbo-fields --invoice DOC` | Lists the invoice's custom fields (name, `DefinitionId`, type, value) and the `QB_ETA_*` lines to paste into `.env`. Exits 1 if the ETA field is not on the invoice |
| `eta version` | Program, Python, httpx, pandas and python-dotenv versions |

`eta --help` and `eta <command> --help` show every option. Every command except `version` writes a DEBUG log
file to `<ETA_OUTPUT_DIR>/logs/eta_<YYYYmmdd_HHMMSS>.log`.

### Probes

Probes print a human summary and save the redacted payloads under `<ETA_STATE_DIR>/probe/` as
`<service>_<id>_<YYYYmmdd_HHMMSS>.json` (VShip: `vship_<booking>.json`). Without a QuickBooks write flag every
probe is read-only and takes no lock.

| Probe | Checks |
|---|---|
| `eta probe qbo [--invoice DOC]` | Token refresh (prints the refresh-token expiry, never the token), company name, invoice e-mail template subject and message, PDF attachment setting, custom transaction numbers; with `--invoice`, a read-only summary of that invoice |
| `eta probe vship --booking B` | Login, search candidates, chosen booking id, customer, internal comments, notification status, TR/SWB verdict |
| `eta probe msc --sandbox [--save-raw]` | MSC's public mock sandbox, no credentials (expects `2022-12-26`); saved only with `--save-raw` |
| `eta probe msc --token-info` | The stored bridge token's status, no network call |
| `eta probe msc --booking EBKG… [--booking …]` | Real lookups in the configured MSC mode |
| `eta probe msc --compare --booking …` | Token bridge and official API side by side (needs both); exit 1 on any DIFF |
| `eta probe maersk --booking B` | Token, events, ETA, and the `final`/`pod` alternatives |
| `eta probe cma --booking B` | Both CMA endpoints: events, POD, per-container ETAs, endpoint agreement. **Billable**: prints the number of calls first (at least 2 per booking) |
| `eta probe aggregator [--booking B]` | Account and active carrier codes; with a booking, its tracked data. Never registers anything |
| `eta probe all [--booking B ...] [--invoice DOC]` | Every configured read-only probe that has its arguments: qbo, vship for each booking, msc/maersk/cma for the bookings matching their pattern (CMA is billable), and the aggregator account. Exit code = the worst result |

QuickBooks **write probes** (`eta probe qbo` only, one at a time) write only to the test invoice or the company
e-mail template and restore what they change — except `--prefs-roundtrip`, whose first step writes the whole
company Preferences object back unchanged (with `QB_PREFS_UPDATE_MODE=full`): if QuickBooks drops or changes a
setting on that write, the probe reports FAIL and lists the settings, but it cannot undo that write. Run it with
`QB_SANDBOX=true` first:

| Flag | What it does |
|---|---|
| `--write-test --invoice DOC` | Sets the invoice's ETA to original + 1 day, verifies, restores and verifies again; also checks that the DocNumber and the other custom fields did not change. The invoice must already have an ETA (otherwise NOT RUN, exit 1) |
| `--prefs-roundtrip` | Writes Preferences back unchanged, then with only a probe subject, then restores; PASS only if nothing else changed. Refuses if a template recovery file exists |
| `--send-test --invoice DOC` | Sends one probe e-mail through the configured strategy, then asks whether it arrived with exactly that subject, the PDF and the pay link; the answer is recorded in `qbo_email_probe.json`. On production the invoice's recipients must be exactly `QB_TEST_SEND_TO` |
| `--restore-template` | Runs only the e-mail template crash recovery ("Nothing to restore" when there is nothing to do) |

Each write probe asks for confirmation (always, even with `--yes`), needs `--yes` as well on a production
company, refuses when there is no terminal to ask on, and takes the run lock.

### Exit codes

| Code | `eta run` and the other commands | `eta probe` |
|---|---|---|
| 0 | Completed (row-level failures are normal and go to the output files) | The integration works |
| 1 | Unexpected crash (traceback printed and written to the log) | The integration is not working (including a `--compare` DIFF or a write-test FAIL) |
| 2 | Configuration, authentication or run-lock error; invalid command-line arguments | Same, including "service not configured" |
| 3 | Aborted: answered no at the prompt, no terminal and no `--yes`, Ctrl+C, or no file picked | A write probe was refused or not confirmed |
| 4 | Run completed, but the invoice e-mail path was unavailable or failed (e-mail rows reported as failed) | — |

## Output files

Written to `ETA_OUTPUT_DIR` (default: the folder you run `eta` from), appended to, UTF-8, with the same names
as the original tool. A dry run writes none of them; the plan report shows what would be written.

| File | Who/what it is for | Entry |
|---|---|---|
| `Send2Bindu.csv` | Invoices below `ETA_MIN_INVOICE_AMOUNT` | CSV row `doc_number, amount, customer_name` |
| `Send2Ben.txt` | Both notices already sent (DocNumber ends in `AA`, or VShip shows `NOTIF #2`, or an earlier row of the same booking in this run sent the second notice; such a line is written only once that e-mail went out) | `{doc_number}` |
| `Send2Accounting.txt` | Partially paid invoice with a TR/SWB note | `{doc_number} - is partially paid and has a TR/SWB note` |
| `PotentialCustomsHold.txt` | MSC shows a possible customs hold (no e-mail sent) | `[ERROR] #{doc_number} -  Possible Customs Hold.` |
| `Failed_To_Process.txt` | Every row that needs a person (below) | First line, then the error text and a blank line where shown |
| `ToEmail_<run_id>.csv` | `--no-email` runs: the e-mails to send by hand | `doc_number, invoice_id, customer, to, cc, subject, doc_number_after_sending, vship_note_after_sending` |
| `reports/run_<run_id>.csv` and `.json` | Every run, dry runs included (rewritten when the run ends; if the CSV is open in Excel then, the final one is written as `run_<run_id>_final.csv` and printed) | One row per input row: route, ETA, source, in-window, subject, actions, status, message, warnings |
| `logs/eta_<YYYYmmdd_HHMMSS>.log` | Every command except `version` | DEBUG log; secrets are redacted |

`Failed_To_Process.txt` entries (`{err}` is the error text, written on the next line):

| Entry | Meaning |
|---|---|
| `[ERROR] Row {'inv_number': '...', 'amount_due': '...'} - Malformed row (missing amount column or non-numeric amount).` | Fix the CSV row (missing invoice number or amount) |
| `{doc_number}` on its own | The DocNumber has a suffix the rules do not handle (e.g. `NAM1234567 C`): handle by hand |
| `[ERROR] #{doc} - Carrier Lookup Failed.` | The carrier lookup failed, the carrier is not configured or is off, or the number is a reference number / matches no carrier |
| `[ERROR] #{doc} -  Can't determine ETA.` | The carrier has no usable ETA yet (two spaces after the dash, as in the original) |
| `[ERROR] #{doc} - Unexpected error during carrier lookup.` | Unexpected failure; the log has the details |
| `[ERROR] #{doc} - VShip notification lookup failed, nothing sent` | Only with `ETA_ON_NOTIF_LOOKUP_ERROR=fail` |
| `[SKIP] #{doc} - Done in the last week` | A notice was sent within the last 7 days, or an earlier row of the same booking (e.g. `X` before `X A`) sent one in this run (written only once that e-mail went out) |
| `[SKIP] #{doc} - not found in QuickBooks` | No invoice with exactly this DocNumber |
| `[SKIP] #{doc} - duplicate row in input file (line {n})` | The same DocNumber appeared earlier in the file; only the first row was processed |
| `[SKIP] #{doc} - already e-mailed today at {HH:MM} (ledger); use --force to resend` | This invoice was already e-mailed today, or an e-mail to it today may have gone out (outcome unknown) (see [Troubleshooting](#troubleshooting)) |
| `[ERROR] #{doc} - QB Invoice Processing Error, No Email Sent` | A QuickBooks step (or the TR/SWB check) failed; no e-mail went out — unless the error text says the e-mail **outcome is unknown** (a timeout or server error after the request went out): then the e-mail may have arrived, so check the invoice in QuickBooks (or the mailbox's Sent Items for `graph`/`smtp`) before the next run. A re-run the same day skips such an invoice until you pass `--force`. With the text `not processed: #X (same booking) was to give this booking's notice in this run, but its e-mail was not confirmed as sent`, nothing was changed for this row: the booking gets one notice per run, the earlier row `X` was to send it and failed (its own entry says why). Run the file again once `X` is fixed |
| `[ERROR] #{doc} - Note Write Error, QB Invoice Processing Successful` | QuickBooks (and the e-mail, if one was due) succeeded; the VShip note was not written |
| `[ERROR] #{doc} - Booking ETA Change Failed, QB Invoice Processing Successful` | Everything succeeded except the VShip expected-arrival date |

State files in `ETA_STATE_DIR` (keep this folder outside any git repository): `tokens.json` (access tokens and
the QuickBooks refresh token, user-only permissions), `ledger.jsonl` (every completed write, used by the
same-day e-mail guard), `run.lock`, `probe/*.json` (redacted), `qbo_email_template_recovery.json` (only while
a template swap is in flight), `qbo_email_probe.json` (the recorded e-mail verifications) and
`aggregator_tracking.json` (if the aggregator is used).

## Troubleshooting

Details for every failure are in the log file (`logs/eta_<timestamp>.log`, DEBUG level, secrets redacted) and in
the run report.

| You see | What to do |
|---|---|
| `QuickBooks authentication failed: …` / `QuickBooks rejected the refresh token (invalid_grant)` | Run `eta qbo-auth` to reconnect (do not delete `tokens.json`), or check `QB_CLIENT_ID` / `QB_CLIENT_SECRET` / `QB_REALM_ID` / `QB_REFRESH_TOKEN`. `eta probe qbo` shows details |
| `QB_CLIENT_ID / QB_CLIENT_SECRET were rejected (invalid_client)` | Wrong keys, or production keys with `QB_SANDBOX=true` (or the reverse) |
| ``QuickBooks connection expires on <date>; reconnect with `eta qbo-auth` before then.`` | Warning 30 days ahead: run `eta qbo-auth` |
| `… could not be reached: check the network connection and the proxy settings (HTTPS_PROXY) …` | Network problem, not a credential problem: check the connection or proxy and retry |
| `VShip authentication failed: …` | Check `VSHIP_USER_NAME` / `VSHIP_PASSWORD`; `eta probe vship --booking <b>` shows details |
| `No usable MSC access token…` / `The MSC access token expired or was rejected (HTTP 401)…` | Run `eta msc-token` and paste a fresh token from your signed-in myMSC browser session (a run started in a terminal asks for one by itself) |
| MSC returns nothing for valid bookings | `MSC_USER_EMAIL` (default: `VSHIP_USER_NAME`) must equal your myMSC sign-in e-mail |
| `The MSC access token contains invisible or non-ASCII characters…` / `MSC_USER_EMAIL contains non-ASCII characters…` | A token can only hold plain ASCII: copy it again in full (a shortened copy ends in `…`) and paste it with `eta msc-token`. An HTTP header cannot carry accented letters either: set `MSC_USER_EMAIL` (default: `VSHIP_USER_NAME`) to your plain myMSC sign-in e-mail |
| `MSC's edge network refused the request from this network (Akamai 'Access Denied')…` | Run from the network where myMSC works in your browser, or switch to the official MSC API. The program does not try to get around bot protection |
| Maersk token HTTP 400 | The "Track & Trace Events" product is not approved yet |
| Maersk `ERR_GW_001` | Wrong `MAERSK_CLIENT_ID`: use the app's Consumer Key |
| Maersk JSON 403 / HTML 403 | Product not subscribed, or the booking is not visible to your account / edge block: contact Maersk support with the Reference # |
| CMA 401 / 429 | Check `CMA_API_KEY` / set `CMA_MAX_CALLS_PER_MINUTE` (e.g. 20) or ask CMA to raise the quota |
| `cma: OAuth token request rejected: …` / `cma: OAuth token request failed: …` | The private-tier OAuth credentials (`CMA_CLIENT_ID` / `CMA_CLIENT_SECRET`) were refused, or CMA's token server could not be reached. The same message for every CMA row stops the remaining CMA lookups after `ETA_CARRIER_FAILURE_LIMIT` rows |
| `<Carrier> lookups are not configured — set <VARIABLES> in .env` | Add the carrier's credentials, or set `ETA_PROVIDER_<CARRIER>=aggregator`. Until then its rows go to `Failed_To_Process.txt` |
| `<Carrier> lookups are disabled (ETA_PROVIDER_<CARRIER>=off)` | Intentional; change `ETA_PROVIDER_<CARRIER>` to use the carrier |
| `<Carrier> lookups aborted after <n> identical consecutive failures; last error: …` | The circuit breaker stopped a carrier that keeps failing the same way (e.g. an expired token). Fix the cause with its probe and run the file again |
| `Grimaldi ETA lookup not yet implemented for booking …`, `This is a Ref Number: …`, `No carrier pattern matched booking … — process manually` | Expected: handle these rows by hand (Grimaldi only works through the aggregator) |
| `[SKIP] #<doc> - already e-mailed today at <HH:MM> (ledger); use --force to resend` | The ledger in `ETA_STATE_DIR` shows that this invoice's e-mail already went out today (for example in an earlier run that was interrupted), or that a send today ended with "outcome unknown" and may have gone out. Check the customer's e-mail or QuickBooks before you consider `--force`; it would send the e-mail again |
| `The template_swap e-mail method has not been verified for this company…` | Run the e-mail verification (`eta probe qbo --send-test --invoice <test invoice>`). Until then e-mail rows fail with `QB Invoice Processing Error, No Email Sent` and the run exits with code 4 |
| A run was killed while sending | The next real run (not a dry run) restores the QuickBooks e-mail template subject before its prompt, whatever `QB_EMAIL_STRATEGY` is and also with `--no-email` or nothing to e-mail; `eta probe qbo --restore-template` does only that. A dry run only warns |
| `recovery file belongs to another QuickBooks company: …` / `The e-mail template recovery file … cannot be read` (or `is not valid JSON`, `has an unexpected format`, `has no original subject`) | `qbo_email_template_recovery.json` in `ETA_STATE_DIR` was left by a run on another company (e.g. the sandbox before switching `QB_SANDBOX` off), or is damaged. A run that e-mails nothing through `template_swap` (`graph`/`smtp`, `--no-email`, nothing to e-mail) only logs this as a WARNING and ends normally (exit code 0); a `template_swap` run with e-mails sends none of them (exit code 4). Restore the other company's subject with `eta probe qbo --restore-template` and that company's settings, or check the invoice e-mail subject in QuickBooks (Settings → Account and settings → Sales → Messages); then delete the file |
| `The QuickBooks invoice e-mail template must be restored by hand: …` (exit code 4) | Restoring the company-wide invoice e-mail subject failed after a send. Set the subject back as the message says (or run `eta probe qbo --restore-template` once QuickBooks answers again). The invoice being e-mailed at that moment did get its e-mail and its VShip note; the run's other e-mail rows failed with `QB Invoice Processing Error, No Email Sent` and are e-mailed by the next run |
| `Another ETAutomationsAPI run is in progress (lock: …)` | Wait for the other run. The lock is released by the operating system when that process ends, so a leftover `run.lock` file is harmless |
| `Missing required settings: …` / `<NAME> must be …` / `Settings file not found: …` / `Cannot read the settings file …: save it as UTF-8 text` | Fix `.env` (or the `--env-file` path) as the message says; save it as UTF-8 |
| `No input file given and no GUI available — pass the CSV path: eta run <file.csv>` | Tkinter is not available: pass the file on the command line |
| Ctrl+C at the `[y/N]` prompt or at the `eta qbo-auth` paste prompt | One Ctrl+C ends the program (exit code 3); nothing has been changed at that point. On a Windows console this is not verified yet: if the window does not close, press Enter |
| `Stopping (Ctrl+C): finishing the changes already started for <n> invoice(s). Please wait - do not press Ctrl+C again.` | Ctrl+C while changes were being applied. Wait: the program finishes what it started (see [Safety notes](#safety-notes)), prints the summary and ends with exit code 3. Further Ctrl+C presses only repeat this line; run the file again afterwards |

## Safety notes

- **`eta run` changes real QuickBooks invoices and VShip bookings and sends real e-mails to real customers**
  once you answer `y` (or pass `--yes`). Try new settings against the QuickBooks sandbox (`QB_SANDBOX=true`
  with sandbox keys) and with `--dry-run` first, and start live use with `--limit` or `--only`.
- Nothing is written before the confirmation, except the repair of a QuickBooks e-mail template subject left
  modified by a crashed run. Without a terminal and without `--yes` the run aborts.
- Ctrl+C while the changes are applied stops the run: it prints `Stopping (Ctrl+C): … do not press Ctrl+C again`
  at once, starts nothing new and lets every write that has started finish. An e-mail still waiting for its turn
  (`template_swap` sends one at a time) is not sent. A notice is recorded twice, as the `A` suffix and the VShip
  `NOTIF` note, and the run does not stop between the two: an invoice whose suffix is set but whose e-mail was not
  sent gets its old number back, and an e-mailed invoice still gets its note (a failure there is written to
  `Failed_To_Process.txt`). Pressing Ctrl+C again only repeats the message, so wait for the program to end.
  Closing the window or killing the process (Task Manager, `kill`) while it finishes can still leave a notice
  half-recorded: then check the invoices the report shows as unfinished in QuickBooks and VShip. The report
  marks the unfinished rows `INTERRUPTED`; run the file again to finish them.
- A send that may have reached QuickBooks or the mail server is never retried. The ledger blocks a second e-mail
  for the same invoice on the same day, also after a send whose outcome is unknown (the error says
  `outcome unknown`); use `--force` only when you are sure the first one did not arrive. The ledger guards only
  the same day: check an "outcome unknown" invoice before the next day's run.
- `QB_EMAIL_REDIRECT_TO` (shadow mode) changes only who receives the e-mails. QuickBooks (ETA, DocNumber suffix)
  and VShip (notes, ETA) are still changed and the ledger still records the e-mails, so those invoices count as
  notified afterwards: use it with `QB_SANDBOX=true` or on test bookings only. The plan summary says so when it is
  set.
- No customer e-mail goes out until the one-time `--send-test` verification has been recorded
  (`QB_EMAIL_REQUIRE_VERIFIED=true`; keep it on).
- `template_swap` briefly changes the company-wide QuickBooks invoice e-mail subject for each send; staff
  should not e-mail invoices from QuickBooks during a run.
- Only one run at a time (run lock). Dry runs and read-only probes take no lock.
- Secrets stay in `.env` and `tokens.json`; logs, reports, probe files and the ledger are redacted. Never commit
  `.env`, and keep `ETA_STATE_DIR` outside the repository.
- The MSC bridge token comes from your own signed-in browser session: treat it like a password.
- CMA bills every API call (probes make at least 2 per booking); aggregator registrations cost credits
  (`AGG_ALLOW_CREATE`, `AGG_MAX_CREATES_PER_RUN`).

## Running the tests

```sh
pip install -e ".[dev]"
pytest -q
```

The tests never use the network (`tests/conftest.py` blocks it). Optional live smoke tests run the read-only
probes against the real services in a `.env`; they are skipped unless `ETA_LIVE_TESTS=1`. They read
`ETA_LIVE_ENV_FILE` (default `./.env`) and `ETA_LIVE_VSHIP_BOOKING`, `ETA_LIVE_MSC_BOOKING`,
`ETA_LIVE_MAERSK_BOOKING`; the billable CMA probe runs only when `ETA_LIVE_CMA_BOOKING` is set. They never
write anything or send e-mail.

`IMPLEMENTATION_NOTES.md` lists every deliberate deviation from the specification, the open issues, and the
checklist of unverified items with their probe commands.

## About the specification

This program was built from the specification in this repository: **implementation notes** for rebuilding the
ETAutomations ETA-tracking and invoice pipeline without Playwright or any browser automation, using direct API
calls instead, written so that Claude could turn them into a complete, tested Python program.

| Part of the pipeline | Original tool | This program |
|---|---|---|
| QuickBooks: ETA field, invoice number, e-mail | Drives the QuickBooks web app in Chromium | QuickBooks Online Accounting API (the OAuth app you already have) |
| MSC ETA | Headless browser login + a new browser per booking | MSC's official DCSA Track & Trace API (after onboarding); meanwhile the same GraphQL query over plain HTTP with a token you paste from your own browser |
| Maersk ETA | Visible Chrome per booking (Akamai) | Maersk's official "Track & Trace Events" API on your existing developer app |
| CMA CGM ETA | Visible Chrome per booking (DataDome, sometimes a CAPTCHA) | CMA CGM's official tracking API (API key) |
| VShipCRM | Already HTTP | Same API, async and cached |

| Path | Content |
|---|---|
| `BUILD_PROMPT.md` | The exact prompt given to Claude (Claude Code or claude.ai) to build the program, stage by stage |
| `spec/00-overview.md` | The answer in detail: what changes, why it is faster, architecture, risks |
| `spec/01-business-rules.md` | Exact port of every decision, message and output file of the original, with golden values captured from the original code, plus the list of deliberate deviations |
| `spec/02-configuration.md` | Every environment variable, local state files, secrets handling |
| `spec/03-foundations.md` | Shared contracts: layout, types, errors, HTTP/retry, token store, auth, logging, run lock |
| `spec/04-quickbooks-online.md` | QuickBooks OAuth, invoice lookup, ETA custom field, invoice-number updates |
| `spec/04b-invoice-email-delivery.md` | Sending the invoice e-mail with the ETA subject (template swap, Graph, SMTP) |
| `spec/05-vship-crm.md` | VShipCRM API client |
| `spec/06-carrier-msc.md` | MSC: official DCSA API + interim token bridge |
| `spec/07-carrier-maersk.md` | Maersk Track & Trace Events API |
| `spec/08-carrier-cma-cgm.md` | CMA CGM tracking API |
| `spec/09-dcsa-aggregators-grimaldi.md` | Shared DCSA event parser, optional third-party aggregator, Grimaldi |
| `spec/10-pipeline.md` | Plan → confirm → apply orchestration, outputs, ledger, reports |
| `spec/11-cli.md` | Commands, options, exit codes, packaging |
| `spec/12-testing.md` | Test strategy and the end-to-end test matrix |
| `spec/13-rollout-checklist.md` | Your step-by-step path from API access to the first live run |
| `spec/14-original-code-map.md` | Where every piece of the original code went |
| `SPEC_BUNDLE.md` | All spec files in one file (generated by `tools/bundle_spec.py`) for uploading to claude.ai |
| `IMPLEMENTATION_NOTES.md` | How the built program deviates from or interprets the spec, open issues, and the probe checklist |

After editing anything in `spec/`, regenerate the single-file bundle with `python tools/bundle_spec.py`.

### Verification status

Facts were taken from official documentation where available and checked with unauthenticated requests on
2026-11-05. Every integration spec ends with a **Sources & verification status** table. The items that cannot be
confirmed without your credentials have a probe command that checks them on first run (the full list is in
`IMPLEMENTATION_NOTES.md`, section D). The most important are:

1. whether QuickBooks' API writes the ETA **date** custom field
   (`eta probe qbo --write-test --invoice <test DocNumber>`);
2. whether the template-swap e-mail arrives with the exact subject
   (`eta probe qbo --send-test --invoice <test DocNumber>`);
3. whether MSC's official API accepts `EBKG…` booking numbers (`eta probe msc --compare --booking <EBKG…>`);
4. whether the Maersk "Arrives" date matches the API's final-destination arrival or the port-of-discharge
   arrival (`eta probe maersk --booking <9-digit booking>`).
