# How to turn these notes into the working program

The specification is large (≈ 550 k characters, roughly 150 k tokens) because it pins down every endpoint,
message, fixture and test. The prompt below therefore has Claude read the **core** files first and each
**integration** file right before implementing it, so the details are fresh when they are needed.

## Option A — Claude Code (recommended)

The finished program is roughly 4–6 k lines including tests; Claude Code can create the files, run
`pytest`, and fix failures by itself.

1. Clone this repository and open a terminal in it.
2. Start Claude Code (`claude`) in the repository folder.
3. Paste **the prompt below** as your first message.

The code is generated next to the notes (`src/`, `tests/`, `pyproject.toml`), so spec and program live in
one repository. If the session gets long, it is fine to start a new session at a stage boundary with
"Continue the build prompt in BUILD_PROMPT.md from stage <x>; stages before it are done and tested."

## Option B — claude.ai chat

Use a model with a large context window and attach, per stage, only the files listed here (plus the code
files already produced that the stage imports). Paste the prompt below in the first message and add:

> Because of output limits, work stage by stage. After each stage, output every file of that stage in full,
> then stop and wait for me to say "continue" (I will attach the files for the next stage).

| Stage | Attach |
|---|---|
| a. foundations | `spec/00`, `01`, `02`, `03`, `10`, `14` |
| b. pure rules | `spec/01`, `03` |
| c. VShip | `spec/03`, `05` |
| d. QuickBooks | `spec/03`, `04`, `04b` |
| e. carriers | `spec/03`, `09`, then `06`, `07`, `08` (one carrier per message is fine) |
| f. pipeline | `spec/01`, `03`, `10` |
| g. CLI | `spec/03`, `11`, `12` |
| h. docs | `spec/02`, `11`, `13` |

Save each file Claude outputs at the path it names, run `pip install -e .[dev]` and `pytest -q` locally,
and paste any failures back. (`SPEC_BUNDLE.md` contains every spec file in one document if your chat
can take it all at once.)

---

## The prompt

````text
You are implementing a production-quality Python 3.12+ program from a written specification in the
`spec/` folder of this repository (files 00–14; 04b is the e-mail companion of 04).

GOAL
Build the package `eta_automations` (CLI command `eta`): a browser-free replacement for the
"ETAutomations" tool that looks up ocean-freight ETAs from carrier APIs, updates QuickBooks Online
invoices and VShipCRM bookings, and e-mails invoices with ETA-aware subject lines — exactly as specified.

READING ORDER
1. Before writing any code, read these core files completely: 00-overview, 01-business-rules,
   02-configuration, 03-foundations, 10-pipeline, 14-original-code-map.
2. At each implementation stage below, first read completely the spec file(s) named for that stage.

PRECEDENCE when files seem to conflict
1. 03-foundations — names, types, error classes, HTTP/auth/token conventions (an integration spec may
   override a named item only where it explicitly says "overrides 03-foundations §X").
2. 01-business-rules — every decision, every text string, every output format.
3. 04–09 — HTTP details of each integration; their test tables are acceptance tests.
4. 10-pipeline — orchestration.
5. Everything else.
If a genuine contradiction remains, choose the safer behaviour (never send an e-mail and never write to
QuickBooks/VShip when unsure), implement it, and record it in IMPLEMENTATION_NOTES.md.

HARD CONSTRAINTS
- No browser automation of any kind (no Playwright, Selenium, Puppeteer, headless Chrome, etc.) and no
  attempts to work around bot protection; follow the specs' "not specified on purpose" notes.
- Tests never touch the network.
- Do not invent endpoints, fields, headers or parameters the spec does not give. Where the spec marks
  something UNVERIFIED, implement exactly the defensive design it describes and make sure the
  corresponding `eta probe ...` command exercises it.
- Never log or print secrets (passwords, tokens, API keys, cookies).
- Reproduce every string in 01-business-rules §8, §10 and §14 byte-for-byte.
- Keep each deliberate deviation (D1, D2, … in 01 §15, and carrier-specific ones such as MSC-D1 and
  CMA-D1) isolated and commented with its label.
- Deliver complete files: no placeholders, no "TODO: implement", no elided code.

STAGES (after each stage: write that stage's tests from 12-testing and from the spec files' test lists,
run `pytest -q`, and fix failures before moving on)
a. Skeleton (03 §1, 11 §9) + errors, clock, models, logging_setup, http, token_store, auth, config,
   run_lock.                                                   Read: 03, 02 (already read)
b. business_days, rules, texts, vship/logic.                   Read: 01 (already read)
c. vship/client.                                               Read: 05
d. qbo/auth, qbo/client, qbo/email.                            Read: 04, 04b
e. carriers/dcsa, carriers/msc, carriers/maersk, carriers/cma, carriers/grimaldi,
   carriers/aggregator, carriers/router.                       Read: 09, 06, 07, 08
f. outputs, ledger, pipeline.                                  Read: 10 (already read)
g. cli, ui, scripts/run_eta.bat.                               Read: 11, 12
h. README.md (11 §8), .env.example (every variable in 02), IMPLEMENTATION_NOTES.md.   Read: 13
Create every fixture file verbatim from the code blocks in the spec files.

DONE WHEN
- `pytest -q` passes with zero failures (only `live`-marked tests may be skipped);
- `eta --help`, `eta run --help` and `eta probe --help` work;
- you finish with: the file tree, the test summary, and a checklist of every UNVERIFIED item the user must
  confirm with `eta probe ...` before the first live run (see 13-rollout-checklist).
````

---

## After the code exists

Follow `spec/13-rollout-checklist.md` step by step: get API access, fill `.env`, run the probes, compare
ETAs against the old tool, test e-mail delivery safely, run `--dry-run` on a real file, and only then do a
small live run.
