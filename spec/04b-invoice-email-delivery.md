# 04b — Invoice e-mail delivery with a per-invoice subject (`qbo/email.py`)

Companion to `04-quickbooks-online.md`. Replaces the UI steps
"Review and send → type Subject → Send invoice" in `QBTings/invoiceProcessing.py`.

---

## 1. Summary — the problem and the decision

The original sends the **QuickBooks-generated invoice e-mail** (QBO template body, PDF, pay
button, QBO records it as sent) but overwrites the subject with an ETA-aware string from
`subjectDecision()` (now `texts.subject_for()`, 01).

The Accounting API's send operation — `POST /v3/company/{realm}/invoice/{id}/send[?sendTo=addr]` —
takes **no subject or body parameter**. It uses the company's invoice e-mail template. There is no
documented per-send subject. Three API-only ways remain:

| Strategy (`QB_EMAIL_STRATEGY`) | How | Customer receives | QBO records "sent" | Risks |
|---|---|---|---|---|
| **`template_swap`** (PRIMARY, default) | Per invoice, under a global lock: set the company's invoice e-mail subject (`Preferences.EmailMessagesPrefs.InvoiceMessage.Subject`) to the ETA subject → call `/send` → restore the original subject immediately | The genuine QBO e-mail, exactly like today | yes (`EmailStatus=EmailSent`, `DeliveryInfo`) | (a) UNVERIFIED that `/send` uses this Preferences subject when custom form styles exist → **mandatory one-time verification** (§10) before real sends; (b) the company template is modified for ~1–2 s per send — a staff member sending from QBO at that moment would see an ETA subject; (c) Preferences is a *full-update* entity — must round-trip the whole object |
| **`graph`** (FALLBACK) | Send from the company's own Microsoft 365 mailbox via Microsoft Graph `sendMail`, attaching the QBO invoice PDF and the QBO pay link (`InvoiceLink`) | An e-mail from the company mailbox with the exact subject, the QBO PDF, the pay link | best-effort: `EmailStatus=EmailSent` via sparse update (UNVERIFIED that QBO accepts it); QBO's own `DeliveryInfo` is not set | needs an Entra app registration with `Mail.Send` |
| **`smtp`** (FALLBACK for non-Microsoft mail) | Same as `graph` but via SMTP (`smtplib`) | same as `graph` | same as `graph` | Microsoft 365 disables Basic SMTP AUTH by default from Dec 2026 → use `graph` for M365 |

**Recommendation.** Keep the QBO-native e-mail (`template_swap`) if — and only if — the §10
send-test proves that the received subject equals the requested one in *this* company. The tool
refuses to send with `template_swap` until that verification is recorded
(`QB_EMAIL_REQUIRE_VERIFIED=true`). If the test fails, or if staff routinely e-mail invoices from
QBO while the tool runs, use `graph` (the company already had `o365`/`msal` in its requirements,
so a Microsoft 365 tenant is likely; an existing app registration with `Mail.Send` can be reused).

Speed: either way ≈ 1–2 s per e-mail versus ~10–20 s for the UI send dialog. `template_swap`
sends are strictly sequential (global template); `graph`/`smtp` sends may run 2-wide, limited to
25/min (Exchange Online allows 30 messages/min per mailbox).

## 2. Access & onboarding

- **template_swap**: nothing new (uses the existing QBO app and scope). Must pass §10.
- **graph**:
  1. Entra admin center → *App registrations* → New (single tenant). Note *Tenant ID*, *Client ID*;
     create a *client secret*.
  2. *API permissions* → Microsoft Graph → **Application** permission `Mail.Send` → *Grant admin consent*.
  3. Restrict the app to the one sending mailbox (strongly recommended): Exchange Online
     *RBAC for Applications* (or the older `New-ApplicationAccessPolicy`) scoped to e.g.
     `accounting@company.com`.
  4. The sender mailbox must be licensed (shared mailboxes work with application permissions).
- **smtp**: host/port/credentials from the mail provider (e.g. Google Workspace SMTP with an app
  password or SMTP relay). Not recommended for Microsoft 365 after Dec 2026.

## 3. Configuration

E-mail options that belong to `QboSettings` (04 §3):

```python
    email_strategy: Literal["template_swap", "graph", "smtp"] = "template_swap"
    email_require_verified: bool = True
    email_redirect_to: str | None = None
    test_send_to: str | None = None
    prefs_update_mode: Literal["full", "sparse"] = "full"
    mark_sent: bool = True
    email_max_per_minute: int = 25
    email_max_concurrency: int = 2
```

| Env var | Default | Meaning |
|---|---|---|
| `QB_EMAIL_STRATEGY` | `template_swap` | `template_swap` \| `graph` \| `smtp`. |
| `QB_EMAIL_REQUIRE_VERIFIED` | `true` | Refuse real sends until §10 recorded a successful verification for this realm + strategy. |
| `QB_EMAIL_REDIRECT_TO` | — | "Shadow mode": deliver every invoice e-mail to this address instead of the customer. `graph`/`smtp` only; with `template_swap` it is allowed only on the sandbox or when the invoice's BillEmail already equals it (QBO's `sendTo` overwrites the invoice's customer e-mail). |
| `QB_TEST_SEND_TO` | — | Address of the internal test customer used by `eta probe qbo --send-test`. |
| `QB_PREFS_UPDATE_MODE` | `full` | How Preferences are written: `full` = read-modify-write of the whole object (documented); `sparse` = minimal body (only after `--prefs-roundtrip` proves it safe). |
| `QB_MARK_SENT` | `true` | `graph`/`smtp`: after sending, set the invoice's `EmailStatus` to `EmailSent` (best-effort). |
| `QB_EMAIL_MAX_PER_MINUTE` | `25` | `graph`/`smtp` rate limit. |
| `QB_EMAIL_MAX_CONCURRENCY` | `2` | `graph`/`smtp` parallel sends (`template_swap` is always 1). |

`EmailSettings` (`Settings.email`; non-None only when `QB_EMAIL_STRATEGY` is `graph`/`smtp` and its
required variables are present; otherwise `Settings.missing["email"]` lists them):

```python
@dataclass(frozen=True)
class EmailSettings:
    transport: Literal["graph", "smtp"]
    sender: str                                   # EMAIL_FROM
    reply_to: str | None = None                   # EMAIL_REPLY_TO
    bcc: tuple[str, ...] = ()                     # EMAIL_BCC (comma-separated), e.g. an archive address
    body_template: Path | None = None             # EMAIL_BODY_TEMPLATE
    include_pay_link: bool = True                 # EMAIL_INCLUDE_PAY_LINK
    attach_pdf: bool = True                       # EMAIL_ATTACH_PDF
    graph_tenant_id: str | None = None            # EMAIL_GRAPH_TENANT_ID      (graph: required)
    graph_client_id: str | None = None            # EMAIL_GRAPH_CLIENT_ID      (graph: required)
    graph_client_secret: str | None = None        # EMAIL_GRAPH_CLIENT_SECRET  (graph: required)
    smtp_host: str | None = None                  # EMAIL_SMTP_HOST            (smtp: required)
    smtp_port: int = 587                          # EMAIL_SMTP_PORT
    smtp_username: str | None = None              # EMAIL_SMTP_USERNAME        (smtp: required)
    smtp_password: str | None = None              # EMAIL_SMTP_PASSWORD        (smtp: required)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"   # EMAIL_SMTP_SECURITY
```

## 4. Endpoints

QuickBooks (paths relative to the QBO base; `minorversion` on all; see 04 §4):

| # | Call | Notes |
|---|---|---|
| E5 | `POST /v3/company/{realm}/invoice/{id}/send` (+ `sendTo=` only in redirect/test mode) | `Content-Type: application/octet-stream`, empty body. 200 → `{"Invoice": {…, "EmailStatus": "EmailSent", "DeliveryInfo": {"DeliveryType": "Email", "DeliveryTime": "2026-11-05T06:32:10-08:00"}}, "time": …}`. **Never re-POST** after the request may have reached Intuit (it ignores `requestid`; a repeat e-mails the customer twice). `sendTo` overwrites `BillEmail.Address` on the invoice. |
| E6 | `GET /v3/company/{realm}/invoice/{id}/pdf` (`Accept: application/pdf`) | bytes starting `%PDF`. |
| E7 | `GET /v3/company/{realm}/invoice/{id}?include=invoiceLink` | `Invoice.InvoiceLink` (customer pay/view URL) — present only when online payments are enabled and the invoice has a BillEmail. |
| E8 | `GET /v3/company/{realm}/preferences` | `{"Preferences": {"Id": "1", "SyncToken": "12", "EmailMessagesPrefs": {"InvoiceMessage": {"Subject": "…", "Message": "…"}, …}, "SalesFormsPrefs": {…}, …}}` |
| E9 | `POST /v3/company/{realm}/preferences?requestid=<uuid>` | Full update: **writable fields omitted from the body are reset**, so send the complete object as read, with only `EmailMessagesPrefs.InvoiceMessage.Subject` changed. Needs the latest `SyncToken` (5010 otherwise). Returns the new `Preferences`. |

Microsoft Graph:

| Call | Details |
|---|---|
| Token | `POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, form `client_id`, `client_secret`, `scope=https://graph.microsoft.com/.default`, `grant_type=client_credentials` → `{"token_type": "Bearer", "expires_in": 3599, "access_token": "…"}` |
| Send | `POST https://graph.microsoft.com/v1.0/users/{sender UPN, URL-quoted}/sendMail`, JSON below → **202 Accepted**, empty body. Request size ≤ 4 MB (attachments ≲ 3 MB inline). Throttling → 429 with `Retry-After`. |

```json
{"message": {
   "subject": "Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/12. PLEASE MAKE PAYMENT ARRANGEMENT****",
   "body": {"contentType": "Text", "content": "Dear Acme Imports LLC, ..."},
   "toRecipients": [{"emailAddress": {"address": "ap@acme.example"}}, {"emailAddress": {"address": "ops@acme.example"}}],
   "ccRecipients": [{"emailAddress": {"address": "cc@acme.example"}}],
   "bccRecipients": [{"emailAddress": {"address": "archive@example.com"}}],
   "replyTo": [{"emailAddress": {"address": "accounting@example.com"}}],
   "attachments": [{"@odata.type": "#microsoft.graph.fileAttachment", "name": "Invoice_EBKG12345678.pdf",
                    "contentType": "application/pdf", "contentBytes": "<base64>"}],
   "internetMessageHeaders": [{"name": "x-eta-invoice-id", "value": "190613"}]},
 "saveToSentItems": true}
```

SMTP: `smtplib.SMTP(host, port, timeout=30)` → `starttls(ssl.create_default_context())` →
`login()` → `send_message(msg, to_addrs=to+cc+bcc)`; `smtp_security="ssl"` uses `SMTP_SSL`.

## 5. Auth

- QBO: `QboAuth` (04 §5) via the QboClient helpers.
- Graph: `GraphAuth(TokenProvider)` in `qbo/email.py`; TokenStore key `"graph"`,
  `expires_at = now + expires_in`; 400/401 from the token endpoint → `AuthError("Microsoft Graph rejected EMAIL_GRAPH_* credentials: <error_description>")`.
  Like every token provider it never takes a service semaphore (03 §8).
- SMTP: credentials per connection; `SMTPAuthenticationError` → `AuthError`.

## 6. Module design (`qbo/email.py`)

### 6.1 Types

```python
@dataclass(frozen=True, slots=True)
class SendReceipt:
    strategy: str                       # "template_swap" | "graph" | "smtp"
    invoice_id: str
    doc_number: str
    subject: str
    to: tuple[str, ...]
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    sent_at: datetime | None = None     # local time the send call returned
    qbo_delivery_time: str | None = None   # Invoice.DeliveryInfo.DeliveryTime (template_swap)
    qbo_marked_sent: bool = False       # graph/smtp: EmailStatus update succeeded
    redirected_to: str | None = None
    outcome_inferred: bool = False      # response lost, but QBO shows the delivery

class InvoiceEmailer(Protocol):
    name: str
    async def preflight(self, *, dry_run: bool = False) -> None: ...
    async def send(self, inv: InvoiceSnapshot, *, subject: str) -> SendReceipt: ...
    async def aclose(self) -> None: ...

def build_emailer(qbo: "QboClient") -> InvoiceEmailer:
    """TemplateSwapEmailer for template_swap; MailboxEmailer(GraphTransport|SmtpTransport) for graph/smtp.
    ConfigError if the strategy needs Settings.email and it is None (message lists Settings.missing['email'])."""
```
Additions to `errors.py` (extend 03 §5):
```python
class EmailPathUnavailableError(InvoiceSendError): ...   # no invoice e-mail can be sent in this run
class TemplateRestoreError(EmailPathUnavailableError): ...  # QBO template left modified — manual restore needed
```
Pipeline contract (10): `await qbo.prepare_for_sending(dry_run=…)` before applying any NOTIFY plan;
if it raises `EmailPathUnavailableError`, do **not** apply NOTIFY plans (fail them with
"QB Invoice Processing Error, No Email Sent\n<message>") but continue other routes. If
`send_invoice()` raises `EmailPathUnavailableError`/`TemplateRestoreError` mid-run, stop applying
remaining NOTIFY plans the same way, print the message prominently, and exit non-zero.
Any other `InvoiceSendError` affects only that invoice.

Common send validation (all strategies, before any network call):
- `subject = subject.strip()`; empty or containing `\r`/`\n` → `InvoiceSendError("invalid subject")`.
- `inv.recipients` empty → `InvoiceSendError("Invoice has no customer e-mail address (BillEmail)")`.
- `qbo.read_only` → `RuntimeError` (04 §6.1).

### 6.2 `TemplateSwapEmailer`

```python
class TemplateSwapEmailer:
    name = "template_swap"
    def __init__(self, qbo: "QboClient", *, state_dir: Path, clock: Clock): ...
```
State: `self._lock = asyncio.Lock()` (exactly one emailer per process — `QboClient` creates it once);
`self._prefs: dict | None` (latest full Preferences); `self._original: TemplateText | None`
(`@dataclass TemplateText(subject: str, message: str | None)`); `self._broken: str | None`.
Files: `recovery = state_dir / "qbo_email_template_recovery.json"`,
`verified = state_dir / "qbo_email_probe.json"` (both written atomically, 0600).

`_get_subject(prefs)` / `_get_message(prefs)` read `prefs["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"|"Message"]`
(missing → None). `_with_subject(prefs, s)` returns a deep copy with only that value replaced.
`ETA_SUBJECT_RE = re.compile(r"^(ETA probe .* — )?Invoice for Booking no\. .*\*{3}")` recognises our own subjects.

`_write_subject(subject) -> dict`:
- `full` mode: `body = _with_subject(self._prefs, subject)`; POST E9 (new requestid).
- `sparse` mode: `{"Id", "SyncToken", "sparse": True, "EmailMessagesPrefs": {"InvoiceMessage": {"Subject": subject, "Message": self._original.message}}}`.
- Fault 5010 → `self._prefs = await qbo.get_preferences()`; rebuild; retry once.
- Verify `_get_subject(result) == subject`, else raise `InvoiceSendError("qbo", f"QuickBooks stored the template subject as {got!r} instead of {subject!r} (length limit?)")`.
- `self._prefs = result`; return it.

**`preflight(dry_run=False)`** (idempotent; runs once per process unless it failed):
1. **Recovery** — if `recovery` exists: load `{realm_id, original_subject, original_message, swapped_subject, invoice_id, written_at, pid}`.
   - Different `realm_id` → `EmailPathUnavailableError("recovery file belongs to another QuickBooks company: <path>")`.
   - `dry_run` → WARNING "A previous run left the QuickBooks invoice e-mail subject modified ('<swapped>'); it will be restored automatically on the next real run." and continue to step 2 without writing.
   - else: `self._prefs = await qbo.get_preferences()`; if current subject ≠ `original_subject`: restore via `_write_subject(original_subject)`; then delete the file. Failure → `TemplateRestoreError(<manual instructions>)` where the instructions read: *"In QuickBooks open Settings → Account and settings → Sales → Messages (or Custom form styles → Emails) and set the invoice e-mail subject back to: '<original_subject>'. Then delete <path>."*
2. **Verification gate** — unless `dry_run` or `email_require_verified` is false: `verified` must
   contain an entry `{"realm_id": <realm>, "strategy": "template_swap", "verified": true}`, else
   `EmailPathUnavailableError("The template_swap e-mail method has not been verified for this company. Run `eta probe qbo --send-test --invoice <test invoice>` (04b §10) or set QB_EMAIL_STRATEGY=graph.")`.
3. **Read the template** — `self._prefs = self._prefs or await qbo.get_preferences()`; if
   `EmailMessagesPrefs.InvoiceMessage` is missing → `EmailPathUnavailableError("QuickBooks Preferences has no EmailMessagesPrefs.InvoiceMessage — template_swap cannot work; use graph/smtp")`.
   `self._original = TemplateText(subject, message)`.
4. **Sanity** — if `ETA_SUBJECT_RE.match(self._original.subject)` → `EmailPathUnavailableError("The company's invoice e-mail subject currently looks like an ETA subject (<value>) — a previous run may have crashed without a recovery file. Restore the normal subject in QuickBooks first.")`.

**`send(inv, subject)`**:
1. `if self._broken: raise TemplateRestoreError(self._broken)`; common validation (§6.1);
   redirect rule (§3): `send_to = settings.email_redirect_to` only if `settings.sandbox` or
   `{r.lower() for r in inv.recipients} == {redirect.lower()}`; otherwise, if a redirect is configured →
   `EmailPathUnavailableError("QB_EMAIL_REDIRECT_TO cannot be used with template_swap on real invoices (QuickBooks would overwrite the customer's e-mail). Use graph/smtp for shadow runs.")`.
2. `await self.preflight()` if not done.
3. `async with self._lock:`
   1. Re-read only if needed: `current = _get_subject(self._prefs)`. If `current != self._original.subject`
      and not `ETA_SUBJECT_RE.match(current)` → the template was edited by a person during the run:
      WARNING, adopt it as the new original. If it matches `ETA_SUBJECT_RE` → `TemplateRestoreError`.
   2. Write `recovery` with `{realm_id, original_subject, original_message, swapped_subject: subject, invoice_id: inv.id, written_at, pid}`.
   3. `await self._write_subject(subject)` (on failure: delete recovery if the template is unchanged, raise `InvoiceSendError`).
   4. `started = clock.now()`; `try: resp = await qbo._post_send(inv.id, send_to)` / `except Exception as e: send_exc = e`.
   5. `finally:` restore — `await asyncio.shield(self._restore())` where `_restore()` tries
      `_write_subject(self._original.subject)` up to 3 times (re-reading Preferences before each
      retry; sleeps 1, 2, 4 s); success → delete `recovery`; final failure → `self._broken = <manual
      instructions>` and raise `TemplateRestoreError(...)` (chained from `send_exc` if any).
      If the coroutine is cancelled (Ctrl+C) the shielded restore still completes; `aclose()` also
      runs `_restore()` if `recovery` exists.
4. If `send_exc`: infer the outcome — `check = await qbo.get_invoice(inv.id)`; if
   `check.delivery_time` parses (ISO with offset → `astimezone().replace(tzinfo=None)`) to ≥ `started - 5 s`
   → WARNING "send response lost but QuickBooks shows delivery at …" and return a receipt with
   `outcome_inferred=True`. Otherwise raise `InvoiceSendError("qbo", f"invoice e-mail not sent: {send_exc}. QuickBooks shows no delivery — safe to retry later.")`
   (for timeouts/5xx say "outcome unknown; QuickBooks shows no delivery yet — check the invoice before re-sending").
5. Success: response `Invoice.EmailStatus != "EmailSent"` → WARNING. Return
   `SendReceipt(strategy="template_swap", to=(send_to,) if send_to else inv.recipients, cc=<BillEmailCc split>, qbo_delivery_time=…, redirected_to=send_to, sent_at=clock.now(), …)`.

Per send: 2 Preferences POSTs + 1 send POST (+1 Preferences GET on the first send or after 5010).

### 6.3 `MailboxEmailer` (`graph` / `smtp`)

```python
@dataclass(frozen=True, slots=True)
class OutgoingMail:
    sender: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    bcc: tuple[str, ...]
    reply_to: str | None
    subject: str
    text_body: str
    attachments: tuple[tuple[str, str, bytes], ...]   # (filename, content_type, data)
    headers: Mapping[str, str]                          # "x-eta-invoice-id", "x-eta-doc-number"

class MailTransport(Protocol):
    name: str
    async def send(self, mail: OutgoingMail) -> str | None: ...   # provider message id if known
    async def aclose(self) -> None: ...

class GraphTransport:   # httpx + GraphAuth; POST sendMail; expected=(202,), idempotent=False
class SmtpTransport:    # smtplib inside asyncio.to_thread; one connection per message

class MailboxEmailer:
    def __init__(self, qbo: "QboClient", email: EmailSettings, transport: MailTransport, *, clock: Clock): ...
```

`preflight(dry_run)`: obtain a Graph token (graph) or open+close an SMTP login (smtp) — skipped in
dry-run; fetch `CompanyInfo.CompanyName` and the template body
(`Preferences.EmailMessagesPrefs.InvoiceMessage.Message`) once; load `EMAIL_BODY_TEMPLATE` if set
(file must exist → else `ConfigError`); verification gate as in §6.2 step 2 for the chosen strategy.

`send(inv, subject)`:
1. Common validation. Recipients: `to = inv.recipients`; `cc = split(inv.bill_email_cc)`;
   `bcc = split(inv.bill_email_bcc) + email.bcc` (split on `[,;]`, strip, dedupe case-insensitively, keep order).
   Redirect mode: `to = (redirect,)`, `cc = bcc = ()`, and the body starts with
   `"[TEST REDIRECT] This e-mail would have been sent to: <to>; cc: <cc>\n\n"`.
2. PDF (`attach_pdf`): `await qbo.get_invoice_pdf(inv.id)`; failure → `InvoiceSendError` (never send without the invoice). File name `f"Invoice_{re.sub(r'[^A-Za-z0-9._-]+', '_', inv.doc_number)}.pdf"`. > 3 MB with `graph` → `InvoiceSendError("PDF too large for a Graph inline attachment")`.
3. Pay link (`include_pay_link`): `await qbo.get_invoice_link(inv.id)` → may be None (INFO once per run: "QuickBooks returned no InvoiceLink (online payments off?) — e-mails will not contain a pay link").
4. Body (`render_body`): template = `EMAIL_BODY_TEMPLATE` file → else the QBO template message → else the
   built-in default `"Dear [Customer Name],\n\nHere's your invoice! We appreciate your prompt payment.\n\nThanks for your business!\n[Company Name]"`.
   Replace placeholders case-insensitively: `[Customer Name]`/`[Customer]` → `inv.customer_name or ""`,
   `[Company Name]`, `[Invoice No.]`/`[Invoice Number]` → `inv.doc_number`, `[Invoice Amount]` → `f"${inv.total_amount:,.2f}"`,
   `[Balance Due]` → `f"${inv.balance:,.2f}"`, `[Due Date]` → `inv.due_date.strftime("%m/%d/%Y")` or `""`,
   `[Invoice Link]` → link or `""`. Unknown `[...]` tokens are left and logged once (WARNING).
   If a link exists and the template had no `[Invoice Link]`, append `"\n\nView and pay your invoice online:\n<link>\n"`.
5. `async with self._sem (email_max_concurrency), self._limiter (email_max_per_minute/60 s):` `await transport.send(mail)`.
   One attempt for anything ambiguous (read timeout, 5xx) → `InvoiceSendError("e-mail outcome unknown — check the Sent Items of <sender> before re-sending")`.
   `http.request()` may still retry connect errors and 429/503 (nothing was accepted).
6. `mark_sent` and `inv.email_status != "EmailSent"` → `await qbo.mark_email_sent(inv)` (04 §6.6 helper:
   sparse update `{"Id", "SyncToken", "sparse": true, "EmailStatus": "EmailSent"}`); any failure → WARNING
   "could not mark invoice … as sent in QuickBooks (UNVERIFIED feature)". Never fails the send.
7. Return `SendReceipt(strategy=transport.name, …, qbo_marked_sent=…, redirected_to=…)`.

## 7. Business logic

- The subject is sent **verbatim** (no prefix/suffix) — it is the string from `texts.subject_for()`
  built from the DocNumber *before* the suffix change (04 §7).
- Recipients follow QBO: To = BillEmail (may contain several addresses), Cc = BillEmailCc,
  Bcc = BillEmailBcc (`template_swap` lets QBO decide; INFERRED that `/send` honours Cc/Bcc fields).
- **Exactly-once**: no strategy retries a send after the request may have been accepted. The pipeline's
  ledger (10) additionally prevents a second e-mail for the same invoice on the same day.
- **DEVIATION (graph/smtp only):** the e-mail comes from the company mailbox instead of QuickBooks'
  mail service, and QBO's own delivery tracking (`DeliveryInfo`, "Sent" activity) is not recorded.
  Customers who click the pay link still open QBO's hosted invoice page.

## 8. Fallbacks

1. §10 send-test fails for `template_swap` (subject not applied, Preferences round-trip not clean, or
   Preferences not writable) → `QB_EMAIL_STRATEGY=graph` (or `smtp`).
2. Neither mailbox option is available → the user must keep sending these e-mails manually from QBO;
   run the tool with `--no-email` (11-cli) so it still updates ETA fields/VShip and lists the invoices
   to e-mail. There is no other API-only way to set a per-send subject.
3. Do **not** automate QBO's web UI or its private endpoints.

## 9. Fixtures & tests

`tests/fixtures/qbo/preferences.json`
```json
{"Preferences": {"Id": "1", "SyncToken": "12", "sparse": false,
  "EmailMessagesPrefs": {
    "InvoiceMessage": {"Subject": "Invoice [Invoice No.] from [Company Name]",
                       "Message": "Dear [Customer Name],\n\nHere's your invoice! We appreciate your prompt payment.\n\nThanks for your business!\n[Company Name]"},
    "EstimateMessage": {"Subject": "Estimate [Estimate No.] from [Company Name]", "Message": "…"}},
  "SalesFormsPrefs": {"ETransactionAttachPDF": true, "CustomTxnNumbers": true,
                      "SalesEmailBcc": {"Address": "accounting@example.com"}},
  "OtherPrefs": {"NameValue": [{"Name": "SalesFormsPrefs.DefaultCustomerMessage", "Value": "Thank you"}]},
  "MetaData": {"CreateTime": "2020-01-01T00:00:00-08:00", "LastUpdatedTime": "2026-11-01T00:00:00-08:00"}},
 "time": "2026-11-05T06:30:00.000-08:00"}
```
`tests/fixtures/qbo/send_ok.json` — `{"Invoice": {"Id": "190613", "SyncToken": "9", "DocNumber": "EBKG12345678 A", "EmailStatus": "EmailSent", "BillEmail": {"Address": "ap@acme.example, ops@acme.example"}, "DeliveryInfo": {"DeliveryType": "Email", "DeliveryTime": "2026-11-05T06:32:10-08:00"}}, "time": "…"}`.
`tests/fixtures/graph/token.json` — `{"token_type": "Bearer", "expires_in": 3599, "ext_expires_in": 3599, "access_token": "GRAPH-AT"}`.

Unit tests (`tests/test_qbo_email.py`, respx; SMTP via a fake `smtplib.SMTP` class monkeypatched):
1. template_swap call order: GET prefs (first send only) → POST prefs (subject = requested) → POST send → POST prefs (original); the full-mode body equals the fixture except the subject; recovery file exists during the send and is gone after.
2. Send raises `ReadTimeout` → restore still happens; verification GET shows a fresh `DeliveryTime` → receipt `outcome_inferred=True`; no `DeliveryTime` → `InvoiceSendError` with "outcome unknown".
3. Restore keeps failing (3×) → `TemplateRestoreError`; emailer `_broken`; the next `send()` raises immediately without HTTP calls.
4. Startup with a recovery file → preflight restores and deletes it; dry-run preflight only warns (no POST).
5. Stored subject differs from requested (simulated truncation) → restore + `InvoiceSendError`, no send POST.
6. Two concurrent `send()` calls never interleave (assert the request sequence prefs/send/prefs/prefs/send/prefs).
7. Verification gate: no `qbo_email_probe.json` → `EmailPathUnavailableError`; with a verified entry → proceeds; dry-run skips the gate.
8. Sanity: template subject matching `ETA_SUBJECT_RE` at preflight → `EmailPathUnavailableError`.
9. Redirect with template_swap on production and a real customer → `EmailPathUnavailableError`; on sandbox → `sendTo` param present.
10. 5010 on the first prefs POST → re-read + retry once.
11. graph: payload (recipients split/deduped, Cc/Bcc, replyTo, base64 PDF, exact subject, `saveToSentItems: true`), token cached across sends, 202 accepted, `ReadTimeout` → `InvoiceSendError` without retry, 429 with `Retry-After` → retried.
12. smtp: `EmailMessage` headers (From/To/Cc/Reply-To/Subject), Bcc only in `to_addrs`, PDF attached, STARTTLS + login called.
13. Body rendering: QBO placeholders replaced, unknown token kept + warned once, pay link appended once.
14. `mark_sent`: sparse body `{"EmailStatus": "EmailSent"}`; a 400 fault → WARNING only, receipt `qbo_marked_sent=False`.
15. Empty BillEmail / multi-line subject → `InvoiceSendError` before any HTTP call.

## 10. Probe / verification procedure (never e-mails a real customer)

1. **Create a test customer and test invoice** in QBO (sandbox first, then production): customer
   "ETA Automation Test", e-mail = `QB_TEST_SEND_TO` (an internal inbox), one $1 invoice, ETA field
   empty. Note its DocNumber.
2. `eta probe qbo --prefs-roundtrip` (`template_swap` readiness; on production requires `--yes`):
   GET Preferences → POST unchanged → GET → diff (must differ only in `SyncToken`/`MetaData`) →
   POST with a probe subject → GET → diff (only the subject) → restore → GET → diff vs. start
   (identical). Prints PASS/FAIL and saves all three snapshots under `<ETA_STATE_DIR>/probe/`.
   With `QB_PREFS_UPDATE_MODE=sparse` it also verifies that a sparse body leaves other fields intact.
3. `eta probe qbo --send-test --invoice <test DocNumber>`:
   - Refuses unless `QB_SANDBOX=true` or the invoice's recipients are exactly `{QB_TEST_SEND_TO}`.
   - Subject: `f"ETA probe {now:%Y%m%d-%H%M%S} — Invoice for Booking no. {doc}. ***ESTIMATED ARRIVING ON {mm/dd}.****"`.
   - Runs the configured emailer (verification gate bypassed), prints the receipt, then asks:
     "Check <QB_TEST_SEND_TO>. Did an e-mail arrive with EXACTLY this subject, the invoice PDF, and
     (if online payments are on) the pay button/link? [y/N]".
   - `y` → append `{"realm_id", "strategy", "verified": true, "verified_at", "subject_sent", "invoice_id"}`
     to `qbo_email_probe.json`; `n` → append `verified: false` and print §8 advice.
4. Optional shadow run (`graph`/`smtp` only): set `QB_EMAIL_REDIRECT_TO=<internal inbox>` and run the
   real pipeline once; every invoice e-mail goes to the internal inbox (note: QBO ETA/DocNumber
   updates and VShip notes still happen — combine with the QBO sandbox if that is not wanted).
5. Manual template restore helper: `eta probe qbo --restore-template` runs §6.2 preflight step 1
   (useful if a run was killed and you do not want to start a full run).

## 11. Sources & verification status

| Claim | Source | Status |
|---|---|---|
| `POST …/invoice/{id}/send`, optional `sendTo` (overwrites `BillEmail.Address`), sets `EmailStatus=EmailSent`, populates `DeliveryInfo`; `application/octet-stream` | Intuit API reference mirror: https://www.withone.ai/knowledge/quickbooks/conn_mod_def%3A%3AGJ6MhoWyA-k%3A%3A4V1DFuD1TiW_YxiNtZe5Aw/md ; https://www.apideck.com/blog/quickbooks-invoice-api-integration | VERIFIED-DOCS (mirror) |
| No subject/body parameter on `/send`; subject comes from the company's form style / message settings | same + QuickBooks community answers (e.g. https://quickbooks.intuit.com/learn-support/en-us/account-management/invoice-email/00/1330285) | VERIFIED-DOCS (absence) / OBSERVED |
| `/send` ignores `requestid` → repeat = duplicate e-mail | https://github.com/voska/qbo-cli/issues/19 | OBSERVED |
| `Preferences.EmailMessagesPrefs.InvoiceMessage.Subject/Message` writable; Preferences update is a full update (omitted writable fields reset); needs latest SyncToken | https://www.withone.ai/knowledge/quickbooks/conn_mod_def%3A%3AGJ6MkKwf2nA%3A%3AC5kYWJnPQFO6TjrFitIpRA/md ; conflicting SDK annotation (quickbooks-api on unpkg marks it read-only) | VERIFIED-DOCS (mirror), conflicting → probe §10.2 |
| `/send` uses the Preferences invoice subject (incl. with custom form styles) | none | **UNVERIFIED** → mandatory §10.3 |
| Preferences sparse update honoured | none | UNVERIFIED → default `full` |
| `EmailStatus` manually settable to `EmailSent` | Intuit staff reply says status changes only on actual send (https://quickbooks.intuit.com/learn-support/en-uk/other-questions/how-do-i-mark-an-invoice-as-sent-without-emailing-it/00/1585756) | UNVERIFIED (best-effort only) |
| `InvoiceLink` via `include=invoiceLink` (minor version 36/37+), only with online payments + BillEmail | Workato support post, quickbooks-ruby issue #477, QuickBooks community "payments url via api" | OBSERVED |
| Graph `sendMail`: `POST /users/{id\|UPN}/sendMail`, application permission `Mail.Send`, 202 Accepted, `fileAttachment` JSON, `internetMessageHeaders` | https://learn.microsoft.com/en-us/graph/api/user-sendmail?view=graph-rest-1.0 | VERIFIED-DOCS |
| Graph request ≤ 4 MB (inline attachments ≲ 3 MB) | https://learn.microsoft.com/en-us/graph/outlook-large-attachments | INFERRED (well known) |
| Exchange Online: 30 messages/min per mailbox | https://learn.microsoft.com/en-us/office365/servicedescriptions/exchange-online-service-description/exchange-online-limits | INFERRED (well known) |
| Exchange Online disables Basic SMTP AUTH by default from Dec 2026; final removal date announced H2 2027 | Message Center MC786329 (https://mc.merill.net/message/MC786329) | VERIFIED-DOCS (excerpt) |
