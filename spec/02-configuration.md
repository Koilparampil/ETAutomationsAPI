# 02 — Configuration, secrets and local state

All configuration comes from environment variables, normally via a `.env` file in the working directory
(`load_dotenv(..., override=True)` — `.env` wins over OS variables, as in the original). `.env` must be
git-ignored. The implementer generates `.env.example` from the tables below (every variable, with the
default or a placeholder and a one-line comment).

## 1. Run settings (`RunSettings`)

| Env var | Default | Meaning |
|---|---|---|
| `ETA_OUTPUT_DIR` | current working directory | Where `Send2Bindu.csv`, `Send2Ben.txt`, `Send2Accounting.txt`, `Failed_To_Process.txt`, `PotentialCustomsHold.txt` are appended (same names as the original), plus `reports/` and `logs/` |
| `ETA_STATE_DIR` | Windows: `%LOCALAPPDATA%\ETAutomationsAPI`; else `$XDG_STATE_HOME/eta_automations` or `~/.local/state/eta_automations` | `tokens.json`, `ledger.jsonl`, `probe/`, e-mail recovery file (04). Created with user-only permissions |
| `ETA_MIN_INVOICE_AMOUNT` | `1000` | Rows below this amount go to Send2Bindu.csv (01 §3) |
| `ETA_WINDOW_BUSINESS_DAYS` | `6` | Business-day window (01 §5) |
| `ETA_ON_NOTIF_LOOKUP_ERROR` | `send` | `send` (parity) or `fail` (01 §6.2) |
| `ETA_MAX_PARALLEL_BOOKINGS` | `8` | Rows processed concurrently (10 §4) |
| `ETA_CARRIER_FAILURE_LIMIT` | `5` | Consecutive carrier failures before the circuit breaker trips (10 §6) |
| `ETA_LOG_LEVEL` | `INFO` | Console log level |
| `ETA_PROVIDER_MSC` / `ETA_PROVIDER_MAERSK` / `ETA_PROVIDER_CMA` / `ETA_PROVIDER_GRIMALDI` | `native` / `native` / `native` / `off` | Which client serves each carrier: `native`, `aggregator`, `off` (09) |

```python
@dataclass(frozen=True)
class RunSettings:
    output_dir: Path
    state_dir: Path
    min_invoice_amount: Decimal = Decimal("1000")
    window_business_days: int = 6
    on_notif_lookup_error: Literal["send", "fail"] = "send"
    max_parallel_bookings: int = 8
    carrier_failure_limit: int = 5
    log_level: str = "INFO"
    providers: Mapping[Carrier, Literal["native", "aggregator", "off"]] = ...
```

## 2. Service settings

Each service's settings dataclass, variables and defaults are defined in its own spec; this section is the
consolidated index (the implementer copies every row into `.env.example`).

### 2.1 QuickBooks Online — see 04-quickbooks-online.md §3 and 04b §3

| Env var | Required | Default |
|---|---|---|
| `QB_CLIENT_ID` | yes | — |
| `QB_CLIENT_SECRET` | yes | — |
| `QB_REALM_ID` | yes | — |
| `QB_REFRESH_TOKEN` | first run | — (rotated value is written back here and to `tokens.json`) |
| `QB_SANDBOX` | no | `false` |
| `QB_MINOR_VERSION` | no | `75` |
| `QB_ETA_FIELD_NAME` | no | `ETA` |
| `QB_ETA_DEFINITION_ID` | no | — (hint; confirm with `eta qbo-fields`) |
| `QB_ETA_FIELD_TYPE` | no | `DateType` |
| `QB_ETA_STRING_FORMAT` | no | `%m/%d/%Y` |
| `QB_VERIFY_UPDATES` | no | `true` |
| `QB_INCLUDE_ON_WRITES` | no | `true` |
| `QB_QUERY_CHUNK_SIZE` | no | `25` |
| `QB_MAX_CONCURRENCY` | no | `4` |
| `QB_MAX_CALLS_PER_MINUTE` | no | `450` |
| `QB_MAX_CALLS_PER_SECOND` | no | `8` |
| `QB_DOCNUMBER_MAX_LEN` | no | `21` |
| `QB_REDIRECT_URI` | no | `https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl` |
| `QB_OAUTH_SCOPES` | no | `com.intuit.quickbooks.accounting` |
| `QB_EMAIL_STRATEGY` | no | `template_swap` (`template_swap` \| `graph` \| `smtp`) |
| `QB_EMAIL_REQUIRE_VERIFIED` | no | `true` |
| `QB_EMAIL_REDIRECT_TO` | no | — (shadow mode; `graph`/`smtp` only on real invoices) |
| `QB_TEST_SEND_TO` | no | — (internal inbox of the test customer) |
| `QB_PREFS_UPDATE_MODE` | no | `full` |
| `QB_MARK_SENT` | no | `true` |
| `QB_EMAIL_MAX_PER_MINUTE` | no | `25` |
| `QB_EMAIL_MAX_CONCURRENCY` | no | `2` |

### 2.2 VShipCRM — see 05-vship-crm.md §3

| Env var | Required | Default |
|---|---|---|
| `VSHIP_USER_NAME` | yes | — |
| `VSHIP_PASSWORD` | yes | — |
| `VSHIP_API_BASE` | no | `https://vship2000-prod-api.azurewebsites.net/api` |
| `VSHIP_MAX_CONCURRENCY` | no | `4` |
| `VSHIP_TOKEN_TTL_MINUTES` | no | `55` |
| `VSHIP_COMMENT_TYPE` | no | `1` |

### 2.3 MSC — see 06-carrier-msc.md §3

`MSC_MODE=auto` picks `api` when both API variables are set, otherwise the `mymsc` token bridge.
`Settings.msc` is `None` only for an explicit `MSC_MODE=api` with missing API variables.

| Env var | Required | Default |
|---|---|---|
| `MSC_MODE` | no | `auto` (`auto` \| `mymsc` \| `api`) |
| `MSC_ACCESS_TOKEN` | no | — (bridge token; usually stored via `eta msc-token` instead) |
| `MSC_USER_EMAIL` | no | `VSHIP_USER_NAME`, then `MSC_USER_NAME` (header `mymsc-user-email`, parity) |
| `MSC_USER_ROLES` | no | `Customer` |
| `MSC_GRAPHQL_URL` | no | `https://services.mymsc.com/tracking/graphql` |
| `MSC_TOKEN_TTL_SECONDS` | no | `1800` |
| `MSC_RATE_PER_MINUTE` | no | `120` |
| `MSC_API_BASE_URL` | api | — (from MSC onboarding) |
| `MSC_API_SUBSCRIPTION_KEY` | api | — (header `Ocp-Apim-Subscription-Key`) |
| `MSC_API_RATE_PER_MINUTE` | no | `0` (`5` on the Starter product) |
| `MSC_API_CUSTOMS_HOLD_RULE` | no | `true` |
| `MSC_ALLOW_SANDBOX` | no | `false` |
| `MSC_RESCUE_FALLBACK` | no | `false` |
| `MSC_MAX_CONCURRENCY` | no | `4` |
| `MSC_TIMEOUT_SECONDS` | no | `30` |

Legacy and unused by lookups: `MSC_USER_NAME` (only a fallback for `MSC_USER_EMAIL`), `MSC_PASSWORD`,
`LOGIN_URL`, `HOME_PAGE` — this program does not automate the myMSC sign-in (06 §1).

### 2.4 Maersk — see 07-carrier-maersk.md §3

Maersk is *configured* only when both credentials are non-empty.

| Env var | Required | Default |
|---|---|---|
| `MAERSK_CLIENT_ID` | yes* | — (Consumer Key of the developer.maersk.com app) |
| `MAERSK_CLIENT_SECRET` | yes* | — |
| `MAERSK_BASE_URL` | no | `https://api.maersk.com` |
| `MAERSK_TOKEN_URL` | no | `https://api.maersk.com/customer-identity/oauth/v2/access_token` |
| `MAERSK_API_VERSION` | no | *(unset — header not sent)* |
| `MAERSK_ETA_TARGET` | no | `final` (`final` \| `pod`) |
| `MAERSK_MULTI_CONTAINER_POLICY` | no | `first` (`first` \| `latest`) |
| `MAERSK_ETA_DATE_ONLY` | no | `true` |
| `MAERSK_PAGE_LIMIT` | no | `100` (1–100) |
| `MAERSK_MAX_PAGES` | no | `10` |
| `MAERSK_MAX_CONCURRENCY` | no | `3` |
| `MAERSK_RATE_PER_MINUTE` | no | `0` (off) |
| `MAERSK_TIMEOUT_SECONDS` | no | `30` |

### 2.5 CMA CGM — see 08-carrier-cma-cgm.md §3

CMA is *configured* with either an API key (public tier — enough for the ETA) or an OAuth client id + secret.

| Env var | Required | Default |
|---|---|---|
| `CMA_API_KEY` | key **or** id+secret | — (sent as header `KeyId`) |
| `CMA_CLIENT_ID` | with secret, for OAuth | — |
| `CMA_CLIENT_SECRET` | with id, for OAuth | — |
| `CMA_AUTH_MODE` | no | `auto` (`auto` \| `apikey` \| `oauth`) |
| `CMA_OAUTH_SCOPE` | no | `tandtcommercial:read:be tandtpublic:read:be` |
| `CMA_TOKEN_URL` | no | `https://auth.cma-cgm.com/as/token.oauth2` |
| `CMA_BASE_URL` | no | `https://apis.cma-cgm.net/operation/trackandtrace/v1` |
| `CMA_LOOKUP_MODE` | no | `reference` (`reference` \| `dcsa_query`) |
| `CMA_PAGE_LIMIT` | no | `500` (1–500) |
| `CMA_MAX_PAGES` | no | `10` |
| `CMA_BEHALF_OF` | no | — (third parties only) |
| `CMA_MAX_CONCURRENCY` | no | `2` |
| `CMA_MAX_CALLS_PER_MINUTE` | no | unset |
| `CMA_MAX_RETRY_AFTER_S` | no | `120` |
| `CMA_TIMEOUT_S` | no | `30` |

Note: CMA bills per call (200/404 are chargeable); `eta probe cma` makes 2+ billable calls.

### 2.6 Aggregator (optional, ShipsGo by default) and Grimaldi — see 09 §B.4 and Part C/D

Enabled only when `AGG_API_TOKEN` is set; a carrier uses it only when `ETA_PROVIDER_<CARRIER>=aggregator` (§1).
Grimaldi needs no variables: with `native`/`off` it keeps the original "not yet implemented" message.

| Env var | Required | Default |
|---|---|---|
| `AGG_API_TOKEN` | to enable | — |
| `AGG_PROVIDER` | no | `shipsgo` (only implemented backend) |
| `AGG_BASE_URL` | no | `https://api.shipsgo.com/v2` |
| `AGG_MAX_CONCURRENCY` | no | `2` |
| `AGG_RATE_PER_MINUTE` | no | `90` |
| `AGG_TIMEOUT_SECONDS` | no | `30` |
| `AGG_ALLOW_CREATE` | no | `true` (`false` = never spend credits) |
| `AGG_MAX_CREATES_PER_RUN` | no | `100` |
| `AGG_FIRST_LOOKUP_WAIT_S` | no | `45` |
| `AGG_POLL_INTERVAL_S` | no | `15` |
| `AGG_FRESH_MINUTES` | no | `30` |
| `AGG_SCAC_MSC` / `AGG_SCAC_MAERSK` / `AGG_SCAC_CMA` / `AGG_SCAC_GRIMALDI` | no | `MSCU` / `MAEU` / `CMDU` / `GRIU` |

### 2.7 Company-mailbox e-mail (only for `QB_EMAIL_STRATEGY=graph` or `smtp`) — see 04b §3

| Env var | Required | Default |
|---|---|---|
| `EMAIL_FROM` | yes | — (sending mailbox) |
| `EMAIL_REPLY_TO` | no | — |
| `EMAIL_BCC` | no | — (comma-separated, e.g. an archive address) |
| `EMAIL_BODY_TEMPLATE` | no | — (text file; else the QBO template message) |
| `EMAIL_INCLUDE_PAY_LINK` | no | `true` |
| `EMAIL_ATTACH_PDF` | no | `true` |
| `EMAIL_GRAPH_TENANT_ID` / `EMAIL_GRAPH_CLIENT_ID` / `EMAIL_GRAPH_CLIENT_SECRET` | graph | — |
| `EMAIL_SMTP_HOST` / `EMAIL_SMTP_USERNAME` / `EMAIL_SMTP_PASSWORD` | smtp | — |
| `EMAIL_SMTP_PORT` | no | `587` |
| `EMAIL_SMTP_SECURITY` | no | `starttls` (`starttls` \| `ssl` \| `none`) |

## 3. Obsolete variables from the original

| Variable | Why it is gone |
|---|---|
| `QB` | QBO web-login password — no browser login any more |
| `QB_HEADLESS` | No browser |
| `HOME_PAGE` | MSC browser navigation target |
| `LOGIN_URL`, `MSC_PASSWORD` | MSC browser sign-in — not automated any more (06 §1); the official API key or a pasted token replaces them |

Unknown variables are ignored (log obsolete ones once at INFO/DEBUG as the specs say).

## 4. Local state files (`ETA_STATE_DIR`)

| File | Content | Sensitive |
|---|---|---|
| `tokens.json` | Access tokens + QBO refresh token (03 §7) | **yes** (0600) |
| `ledger.jsonl` | Completed write actions (10 §9) | low |
| `run.lock` | Single-instance OS lock (03 §14) | no |
| `probe/*.json` | Redacted raw payloads from `eta probe` | low (redacted) |
| `qbo_email_template_recovery.json` | Original QBO e-mail subject while a template swap is in flight (04b §6.2) | low |
| `qbo_email_probe.json` | Human-confirmed e-mail verification records (04b §10) | low |
| `aggregator_tracking.json` | Aggregator tracking-request ids (09), if used | low |

None of these may be inside the git repository. On first run, the program migrates nothing from the old
tool (the old token files are browser sessions and are not reusable); the only value to carry over is
`QB_REFRESH_TOKEN` (and the other credentials in the old `.env`).

## 5. Secrets handling rules

- Secrets live only in `.env` and `tokens.json`. Never in logs, reports, exceptions, probe files or the ledger.
- `.env` write-back is limited to `QB_REFRESH_TOKEN` (04) and uses `dotenv.set_key(env_path, "QB_REFRESH_TOKEN", value)`
  — never rewrite the whole file.
- The repository's `.gitignore` already lists `.env`, `*.log`, `reports/`, `logs/`, `Send2*.txt`,
  `Send2Bindu.csv`, `Failed_To_Process.txt`, `PotentialCustomsHold.txt`, `ToEmail_*.csv`; keep it. Unlike the
  original, it must **not** ignore `*.json` — the test fixtures are JSON files.
