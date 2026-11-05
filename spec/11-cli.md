# 11 — Command-line interface, packaging and UX (`cli.py`, `ui.py`, `pyproject.toml`)

## 1. Entry points

- Console script `eta` (pyproject `[project.scripts] eta = "eta_automations.cli:main"`).
- `python -m eta_automations …` (same `main`).
- `scripts/run_eta.bat` (Windows double-click helper, §7).

`main(argv: list[str] | None = None) -> int` parses arguments with `argparse` (no extra CLI library),
reconfigures stdout/stderr to UTF-8 (`errors="replace"`), loads settings (`--env-file` or `./.env`),
configures logging, then dispatches to `asyncio.run(<command coroutine>)`. It returns the exit code;
`if __name__ == "__main__": raise SystemExit(main())`.

## 2. Commands

### `eta run [INPUT_CSV] [options]`
Runs the full pipeline (10-pipeline.md).

| Option | Meaning |
|---|---|
| `INPUT_CSV` | Path to the input `.csv`. If omitted, open the Tk file picker (§6). |
| `--dry-run` | Plan only: all reads happen, nothing is written anywhere (no QBO/VShip writes, no e-mails, no output files, no ledger). Plan report is still saved. |
| `--yes` / `-y` | Skip the confirmation prompt. |
| `--force` | Ignore the same-day e-mail ledger guard. |
| `--no-email` | Never send e-mail: NOTIFY rows only get their QBO ETA update and VShip ETA patch; the e-mails to send by hand are listed in `ToEmail_<run_id>.csv` (10-pipeline.md Phase 3 step 1). |
| `--only DOC [DOC ...]` | Process only rows whose doc_number or base booking matches (exact, case-insensitive). |
| `--limit N` | Process only the first N rows (after `--only`). |
| `--env-file PATH` | Use this `.env` instead of `./.env`. |
| `--log-level LEVEL` | Console log level (default `INFO`; file log is always DEBUG). |
| `--max-parallel N` | Override `ETA_MAX_PARALLEL_BOOKINGS` for this run. |

### `eta eta BOOKING [BOOKING ...] [--json]`
ETA lookup only (no VShip/QBO). For each booking: carrier, ETA, source, is_actual, in-window flag
(computed with the current cutoff), or the error. Exit 0 even if some lookups fail. Useful for parity checks
against the old tool and carrier websites.

### `eta probe {qbo,vship,msc,maersk,cma,aggregator,all} [--booking B] [--invoice DOC] [qbo flags] [--yes]`
Diagnostics per integration as defined in each integration spec's "Probe" section
(04 §10, 04b §10, 05 §10, 06–09 §10). Prints a human summary, saves redacted raw payloads to
`<ETA_STATE_DIR>/probe/<service>_<id>_<timestamp>.json`, and exits non-zero if the integration is not working.
`all` runs every configured **read-only** probe that has the arguments it needs.

Without flags every probe is read-only. QuickBooks write probes (each asks for confirmation; on production
they also require `--yes`; they take the run lock, 03 §14):

| Flag (with `probe qbo`) | What it does | Spec |
|---|---|---|
| `--write-test --invoice DOC` | Sets the ETA field to original+1 day, verifies, restores, verifies again | 04 §10 step 4 |
| `--prefs-roundtrip` | Proves Preferences can be written back unchanged and with only the subject changed | 04b §10 step 2 |
| `--send-test --invoice DOC` | Sends one probe e-mail to the internal test customer and records the human-confirmed verification | 04b §10 step 3 |
| `--restore-template` | Runs only the e-mail template crash recovery | 04b §10 step 5 |

Probes that call billable carrier APIs (CMA) print the number of calls they will make before running.

### `eta qbo-auth [--listen] [--port N] [--no-browser]`
Interactive OAuth helper (04 §5.4): paste-back flow by default (works with production keys; the
redirect URI is `QB_REDIRECT_URI`); `--listen` uses a one-shot `http://localhost:<port>/callback` listener
(development/sandbox keys only). Stores the refresh token in the TokenStore and `.env`.

MSC options (06 §10): `eta probe msc --sandbox [--save-raw]` (public mock sandbox, no credentials),
`--token-info` (no network), `--booking EBKG… [--booking …]` (repeatable), `--compare` (bridge vs. official
API side by side; exit 1 on any difference).

### `eta msc-token`
Prompts (hidden input, `getpass`) for an MSC access token copied from the user's own signed-in myMSC browser
session (06 §2.2), stores it in the TokenStore and prints its status (seconds left, `iss`/`aud`/`exp`).
Needed only while MSC runs in `mymsc` bridge mode.

### `eta qbo-fields --invoice DOC`
Lists the custom fields on the given invoice (name, DefinitionId, type, value) so the user can confirm
`QB_ETA_DEFINITION_ID` / `QB_ETA_FIELD_NAME`.

### `eta version`
Prints the program version, Python version and the installed versions of httpx/pandas.

## 3. Exit codes

| Code | Meaning |
|---|---|
| 0 | Completed (row-level failures are normal and go to the output files) |
| 1 | Unexpected crash (traceback printed and written to the log) |
| 2 | Configuration or authentication preflight error |
| 3 | Aborted at the confirmation prompt (or non-interactive without `--yes`) |
| 4 | Run completed, but the invoice e-mail path was unavailable or failed (NOTIFY rows reported as failed; see 04b) |

## 4. Console output

- INFO lines mirror the original's progress prints so staff recognise them, e.g.
  `Processing: EBKG17208602`, `  [Carrier] Identified as MSC — querying tracking API...`,
  `  ETA: 2026-11-16 | In 6-day window: True`, `  [OK]    #EBKG17208602`.
  In the concurrent plan phase, prefix each row's lines with `[line/total]` and emit them as a single block
  when the row's plan is complete (so lines from different rows do not interleave).
- Never print secrets (03-foundations §11).

## 5. Double-click mode (parity with the original's Tk + "Press ENTER" behaviour)

If `main()` receives **no arguments at all** (`argv == []`):
1. Behave like `eta run` with the Tk file picker.
2. At the very end (success, failure or crash) print `\nPress ENTER to close this window...` and wait for
   input (`EOFError` ignored) — parity with `pause_before_exit()`.
3. The confirmation prompt (10-pipeline.md Phase 3) still applies.

## 6. `ui.py` — Tk file picker (port of `tinkyWinky.get_user_inputs_authed`)

```python
def pick_input_file() -> Path | None:
    """Small Tk window titled 'ETA Tool' with: label 'FileName', entry (width 42), 'Browse...' button
    (filedialog.askopenfilename(title='Select .csv file', filetypes=[('CSV Files', '*.csv')])),
    'Cancel' and 'Submit' buttons; Enter submits, Esc cancels. Submit validates with
    is_valid_csv_file(path) -> (ok, reason): exists, is a file, suffix .csv (case-insensitive); on failure
    messagebox.showerror('Invalid file', reason) and stay open. Returns the Path, or None if cancelled."""
```
`tkinter` is imported lazily inside the function. If it is unavailable (e.g. headless Linux), raise
`ConfigError("No input file given and no GUI available — pass the CSV path: eta run <file.csv>")`.
Credentials are **never** collected through the GUI any more — they come from `.env`.

## 7. `scripts/run_eta.bat`

```bat
@echo off
cd /d "%~dp0\.."
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m eta_automations %*
) else (
  python -m eta_automations %*
)
```
(With no arguments this triggers double-click mode, §5.)

## 8. Program README (`README.md` at repo root, written by the implementer)

Must contain, in this order: what it does (one paragraph); requirements (Python 3.12+, no browser);
install (`python -m venv .venv`, activate, `pip install -e .[dev]` or `pip install .`); configuration
(copy `.env.example` → `.env`; the variable table from 02-configuration.md); first-time setup checklist
(13-rollout-checklist.md, condensed); daily usage (`eta run file.csv --dry-run`, then `eta run file.csv`);
output files table (01 §14); troubleshooting (auth errors → which probe/command; carrier not configured;
"already e-mailed today"); safety notes (sends real customer e-mails; use QBO sandbox / `--dry-run` first).

## 9. `pyproject.toml` essentials

```toml
[build-system]
requires = ["setuptools>=69"]
build-backend = "setuptools.build_meta"

[project]
name = "eta-automations"
version = "1.0.0"
requires-python = ">=3.12"
dependencies = ["httpx>=0.28,<1", "python-dotenv>=1.0", "pandas>=2.2"]

[project.optional-dependencies]
dev = ["pytest>=8", "pytest-asyncio>=0.24", "respx>=0.22"]
# plus any extra an integration spec defines (e.g. an e-mail backend in 04)

[project.scripts]
eta = "eta_automations.cli:main"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```
