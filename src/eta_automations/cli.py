"""Command-line interface `eta` (11-cli §1–§5): run, eta, probe, qbo-auth, msc-token, qbo-fields, version.

`main(argv)` parses the arguments with argparse, reconfigures stdout/stderr to UTF-8 (errors="replace"),
configures console logging at INFO *before* load_settings() (so its obsolete-variable notice is not lost),
loads the settings (--env-file or ./.env), reconfigures logging from the settings with the DEBUG log file
(03 §11), then runs the command coroutine with asyncio.run(). It returns the 11 §3 exit code:

    0  completed (row-level failures go to the output files)
    1  unexpected crash (traceback printed and logged) — probes also use 1 for "integration not working"
       (06 §10: `--compare` exits 1 on any DIFF)
    2  configuration / authentication / run-lock error
    3  aborted: confirmation declined, non-interactive without --yes, a write probe refused, Ctrl+C
    4  the run completed, but the invoice e-mail path was unavailable or failed (04b)

With no arguments at all (double-click, 11 §5) it behaves like `eta run` with the Tk file picker and waits for
ENTER at the very end. Probes print a human summary and save their payloads REDACTED under
<ETA_STATE_DIR>/probe/. Secrets are never printed (03 §11): tokens are read with getpass, never echoed, and every
console line of a probe passes through redact().
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import dataclasses
import getpass
import importlib.metadata
import json
import logging
import os
import platform
import re
import signal
import sys
import tempfile
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from . import __version__, pipeline, ui
from .business_days import window_cutoff
from .carriers import aggregator as aggregator_carrier
from .carriers import cma, maersk, msc
from .carriers import router as carrier_router
from .carriers.base import error_text
from .clock import FixedClock, SystemClock
from .config import MscSettings, QboSettings, Settings, load_settings, require
from .errors import AuthError, ConfigError, EtaAutomationError, RunLockedError, TemplateRestoreError
from .http import new_async_client
from .logging_setup import configure_logging, log_file_path, redact
from .models import Carrier
from .prompt import pending_reader
from .qbo import email as qbo_email
from .qbo.auth import QboAuth, run_authorization_code_flow
from .qbo.client import VALUE_KEYS, EtaFieldSpec, InvoiceSnapshot, QboClient, split_addresses
from .run_lock import run_lock
from .token_store import TokenStore
from .vship import client as vship_client

__all__ = [
    "EXIT_ABORTED",
    "EXIT_CONFIG",
    "EXIT_CRASH",
    "EXIT_EMAIL_BLOCKED",
    "EXIT_OK",
    "EXIT_PROBE_FAILED",
    "PAUSE_PROMPT",
    "PROBE_SERVICES",
    "build_parser",
    "main",
]

log = logging.getLogger("eta.cli")

# ── 11 §3 exit codes ──
EXIT_OK = pipeline.EXIT_OK                        # 0
EXIT_CRASH = 1                                    # unexpected crash
EXIT_PROBE_FAILED = 1                             # a probe found the integration not working (06 §10 uses 1)
EXIT_CONFIG = pipeline.EXIT_CONFIG                # 2
EXIT_ABORTED = pipeline.EXIT_ABORTED              # 3
EXIT_EMAIL_BLOCKED = pipeline.EXIT_EMAIL_BLOCKED  # 4

PAUSE_PROMPT = "\nPress ENTER to close this window..."   # 11 §5 step 2 (parity with pause_before_exit())
PROBE_SERVICES = ("qbo", "vship", "msc", "maersk", "cma", "aggregator", "all")
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
QBO_WRITE_FLAGS = ("write_test", "prefs_roundtrip", "send_test", "restore_template")
MSC_FLAGS = ("sandbox", "save_raw", "token_info", "compare")
DEFAULT_AUTH_PORT = 8765                          # 04 §5.4 run_authorization_code_flow(port=8765)
DISTRIBUTIONS = ("httpx", "pandas", "python-dotenv")
_YES = frozenset({"y", "yes"})
_SUBJECT_PATH = "EmailMessagesPrefs.InvoiceMessage.Subject"
_PREFS_IGNORED = frozenset({"SyncToken", "MetaData"})   # 04b §10 step 2: the only keys a write may change
_BODY_TEXT_MAX = 4000
_MSC_BOOKING_HINT = "EBKG…"

_EXIT_HELP = """exit codes (11 §3):
  0  completed (row-level failures are normal and go to the output files)
  1  unexpected crash (traceback printed and logged); for probes: the integration is not working
  2  configuration, authentication or run-lock error
  3  aborted at the confirmation prompt (or non-interactive without --yes, or Ctrl+C)
  4  run completed, but the invoice e-mail path was unavailable or failed

Running `eta` with no arguments at all (double-click) is `eta run` with a file picker."""

Handler = Callable[[argparse.Namespace, Settings, Path | None], int]


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Argument parsing (11 §2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid number: {value!r}") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1 (got {number})")
    return number


def _port(value: str) -> int:
    number = _positive_int(value)
    if number > 65535:
        raise argparse.ArgumentTypeError(f"must be a TCP port between 1 and 65535 (got {number})")
    return number


def _log_level(value: str) -> str:
    name = value.strip().upper()
    if name not in LOG_LEVELS:
        raise argparse.ArgumentTypeError(f"invalid level {value!r} (choose from {', '.join(LOG_LEVELS)})")
    return name


def build_parser() -> argparse.ArgumentParser:
    """The `eta` argument parser: every command and flag of 11 §2 and of the probe sections it wires up."""
    parser = argparse.ArgumentParser(
        prog="eta",
        description="ETAutomationsAPI: carrier ETAs via official APIs, QuickBooks Online invoice updates and "
                    "e-mails, VShipCRM notes. No browser automation.",
        epilog=_EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", type=Path, metavar="PATH",
                        help="use this .env file instead of ./.env")
    common.add_argument("--log-level", type=_log_level, metavar="LEVEL",
                        help="console log level: DEBUG, INFO, WARNING, ERROR or CRITICAL (default: ETA_LOG_LEVEL, "
                             "else INFO); the log file is always DEBUG")

    # ── eta run ──
    run = commands.add_parser(
        "run", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="run the full pipeline on an input CSV",
        description="Run the full pipeline (10-pipeline): read the CSV, look up every ETA, plan, show the plan, "
                    "ask for confirmation, then update QuickBooks and VShip and send the invoice e-mails.",
        epilog="Put INPUT_CSV before --only (--only takes several values).\n\n" + _EXIT_HELP)
    run.add_argument("input_csv", nargs="?", type=Path, metavar="INPUT_CSV",
                     help="path to the input .csv (doc_number,amount); omitted: a file picker opens")
    run.add_argument("--dry-run", action="store_true",
                     help="plan only: every read happens, nothing is written anywhere (no QuickBooks/VShip "
                          "writes, no e-mails, no output files, no ledger); the plan report is still saved")
    run.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    run.add_argument("--force", action="store_true", help="ignore the same-day e-mail ledger guard")
    run.add_argument("--no-email", action="store_true",
                     help="never send e-mail: NOTIFY rows only get their QuickBooks ETA update and VShip ETA patch; "
                          "the e-mails to send by hand are listed in ToEmail_<run_id>.csv")
    run.add_argument("--only", nargs="+", action="extend", metavar="DOC",
                     help="process only rows whose doc_number or base booking matches (exact, case-insensitive)")
    run.add_argument("--limit", type=_positive_int, metavar="N",
                     help="process only the first N rows (after --only)")
    run.add_argument("--max-parallel", type=_positive_int, metavar="N",
                     help="override ETA_MAX_PARALLEL_BOOKINGS for this run")
    run.set_defaults(handler=_cmd_run, subparser=run)

    # ── eta eta ──
    eta = commands.add_parser(
        "eta", parents=[common], help="ETA lookup only (nothing is written)",
        description="ETA lookup only (no VShip/QuickBooks; nothing is written): for each booking the carrier, ETA, "
                    "source, whether it is an actual arrival and whether it is inside the notification window "
                    "(computed with the current cutoff), or the error. Exits 0 even if some lookups fail.")
    eta.add_argument("bookings", nargs="+", metavar="BOOKING", help="base booking number(s), e.g. EBKG17208602")
    eta.add_argument("--json", action="store_true", help="print the results as JSON")
    eta.set_defaults(handler=_cmd_eta, subparser=eta)

    # ── eta probe ──
    probe = commands.add_parser(
        "probe", parents=[common], formatter_class=argparse.RawDescriptionHelpFormatter,
        help="diagnostics per integration (read-only unless a QuickBooks write flag is given)",
        description="Diagnostics per integration (04 §10, 04b §10, 05 §10, 06–09 §10). Prints a human summary, "
                    "saves the redacted payloads under <ETA_STATE_DIR>/probe/ and exits non-zero if the "
                    "integration is not working. `all` runs every configured read-only probe that has the "
                    "arguments it needs. Without flags every probe is read-only.",
        epilog="QuickBooks write probes ask for confirmation, need --yes on a production company and take the run "
               "lock. CMA probes make billable API calls (the count is printed first).\n\n"
               "exit codes: 0 working, 1 not working, 2 configuration/authentication/run lock, "
               "3 refused or aborted")
    probe.add_argument("service", choices=PROBE_SERVICES, metavar="{" + ",".join(PROBE_SERVICES) + "}",
                       help="integration to check")
    probe.add_argument("--booking", action="append", default=[], metavar="B",
                       help="booking number to look up (repeatable)")
    probe.add_argument("--invoice", metavar="DOC", help="QuickBooks invoice DocNumber (qbo probes)")
    qbo_group = probe.add_argument_group("QuickBooks write probes (with `probe qbo`; one at a time)")
    qbo_writes = qbo_group.add_mutually_exclusive_group()
    qbo_writes.add_argument("--write-test", action="store_true",
                            help="with --invoice: set the ETA field to original+1 day, verify, restore, verify "
                                 "again (04 §10 step 4)")
    qbo_writes.add_argument("--prefs-roundtrip", action="store_true",
                            help="prove Preferences can be written back unchanged and with only the subject "
                                 "changed (04b §10 step 2)")
    qbo_writes.add_argument("--send-test", action="store_true",
                            help="with --invoice: send one probe e-mail to the internal test customer and record "
                                 "the human-confirmed verification (04b §10 step 3)")
    qbo_writes.add_argument("--restore-template", action="store_true",
                            help="run only the e-mail template crash recovery (04b §10 step 5)")
    qbo_group.add_argument("--yes", "-y", action="store_true",
                           help="required for the write probes on a production QuickBooks company")
    msc_group = probe.add_argument_group("MSC options (with `probe msc`, 06 §10)")
    msc_modes = msc_group.add_mutually_exclusive_group()
    msc_modes.add_argument("--sandbox", action="store_true",
                           help="call MSC's public mock sandbox (no credentials; expects ETA 2022-12-26)")
    msc_modes.add_argument("--token-info", action="store_true",
                           help="show the stored MSC token status (no network)")
    msc_modes.add_argument("--compare", action="store_true",
                           help="with --booking: myMSC bridge vs official API side by side (exit 1 on any DIFF)")
    msc_group.add_argument("--save-raw", action="store_true",
                           help="also save the redacted raw payload of a --sandbox probe")
    probe.set_defaults(handler=_cmd_probe, subparser=probe)

    # ── eta qbo-auth ──
    qbo_auth = commands.add_parser(
        "qbo-auth", parents=[common], help="connect QuickBooks (OAuth) and store the refresh token",
        description="Interactive QuickBooks OAuth helper (04 §5.4). Default: paste-back flow (works with "
                    "production keys; redirect URI QB_REDIRECT_URI). You sign in and approve in your own browser; "
                    "the refresh token is stored in tokens.json and .env.")
    qbo_auth.add_argument("--listen", action="store_true",
                          help="one-shot http://localhost:<port>/callback listener (development/sandbox keys only)")
    qbo_auth.add_argument("--port", type=_port, default=DEFAULT_AUTH_PORT, metavar="N",
                          help=f"listener port for --listen (default {DEFAULT_AUTH_PORT})")
    qbo_auth.add_argument("--no-browser", action="store_true", help="do not open the browser, only print the address")
    qbo_auth.set_defaults(handler=_cmd_qbo_auth, subparser=qbo_auth)

    # ── eta msc-token ──
    msc_token = commands.add_parser(
        "msc-token", parents=[common], help="store an MSC access token from your myMSC browser session",
        description="Paste (hidden input) an MSC access token copied from your own signed-in myMSC browser session "
                    "(06 §2.2). It is stored in tokens.json and its status is printed (seconds left, iss/aud/exp). "
                    "Needed only while MSC runs in mymsc bridge mode.")
    msc_token.set_defaults(handler=_cmd_msc_token, subparser=msc_token)

    # ── eta qbo-fields ──
    qbo_fields = commands.add_parser(
        "qbo-fields", parents=[common], help="list the custom fields of one QuickBooks invoice",
        description="List the custom fields of the given invoice (name, DefinitionId, type, value) so you can "
                    "confirm QB_ETA_DEFINITION_ID / QB_ETA_FIELD_NAME (04 §6.7, §10 step 3). Read-only.")
    qbo_fields.add_argument("--invoice", required=True, metavar="DOC", help="invoice DocNumber")
    qbo_fields.set_defaults(handler=_cmd_qbo_fields, subparser=qbo_fields)

    # ── eta version ──
    version = commands.add_parser("version", help="print the program, Python, httpx and pandas versions",
                                  description="Print the program version, the Python version and the installed "
                                              "versions of httpx, pandas and python-dotenv.")
    version.set_defaults(handler=None, subparser=version)
    return parser


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _check_arguments(args: argparse.Namespace) -> None:
    """Cross-option rules argparse cannot express; a violation is a usage error (exit 2)."""
    error = args.subparser.error
    if args.command == "eta":
        args.bookings = [b.strip() for b in args.bookings if b.strip()]
        if not args.bookings:
            error("give at least one booking number")
    if args.command == "qbo-fields":
        args.invoice = args.invoice.strip()
        if not args.invoice:
            error("--invoice needs a DocNumber")
    if args.command != "probe":
        return
    service = args.service
    args.booking = [b.strip() for b in args.booking if b and b.strip()]
    if args.invoice is not None:
        args.invoice = args.invoice.strip() or None
    writes = [_flag(n) for n in QBO_WRITE_FLAGS if getattr(args, n)]
    msc_flags = [_flag(n) for n in MSC_FLAGS if getattr(args, n)]
    if writes and service != "qbo":
        error(f"{', '.join(writes)} only work with `eta probe qbo` (`eta probe all` runs read-only probes only)")
    if msc_flags and service != "msc":
        error(f"{', '.join(msc_flags)} only work with `eta probe msc`")
    if args.invoice is not None and service not in ("qbo", "all"):
        error("--invoice is only used by `eta probe qbo`")
    if (args.write_test or args.send_test) and not args.invoice:
        error(f"{', '.join(writes)} needs --invoice DOC")
    if service in ("vship", "maersk", "cma") and not args.booking:
        error(f"`eta probe {service}` needs --booking B")
    if service == "msc":
        if (args.sandbox or args.token_info) and args.booking:
            error("--sandbox and --token-info take no --booking")
        if not (args.sandbox or args.token_info) and not args.booking:
            error(f"`eta probe msc` needs --booking {_MSC_BOOKING_HINT} (or --sandbox / --token-info)")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Entry point (11 §1, §3, §5)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def main(argv: Sequence[str] | None = None) -> int:
    """`eta` console script and `python -m eta_automations`: returns the 11 §3 exit code.

    No arguments at all (double-click, 11 §5): behave like `eta run` with the Tk file picker, then print
    "Press ENTER to close this window..." and wait — after success, failure or crash alike."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    _configure_console()
    if not arguments:
        try:
            return _main(["run"])
        finally:
            _pause_before_exit()
    return _main(arguments)


def _main(arguments: list[str]) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(arguments)
        _check_arguments(args)
    except SystemExit as exc:            # --help / --version (0) and usage errors (2)
        return _system_exit_code(exc)
    try:
        if args.handler is None:
            return _cmd_version()
        return _with_settings(args)
    except KeyboardInterrupt:
        print("\nInterrupted (Ctrl+C).", file=sys.stderr, flush=True)
        return EXIT_ABORTED
    except Exception:  # noqa: BLE001 - 11 §3 exit 1: the traceback is printed and written to the log
        log.exception("Unexpected error — please keep the log file for the bug report")
        return EXIT_CRASH


def _system_exit_code(exc: SystemExit) -> int:
    code = exc.code
    if code is None:
        return EXIT_OK
    if isinstance(code, int):
        return code
    print(code, file=sys.stderr)
    return EXIT_CONFIG


def _configure_console() -> None:
    """11 §1 / 03 §11: stdout and stderr as UTF-8 with errors="replace" (Windows consoles and pipes)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(AttributeError, OSError, TypeError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def _pause_before_exit() -> None:
    """11 §5 step 2: wait for ENTER so a double-clicked console window stays open (EOF ignored). After a Ctrl+C at
    the [y/N] prompt its reader thread still waits for a line (prompt.read_line): that ENTER closes the window, so
    the user is not asked twice."""
    with contextlib.suppress(EOFError, KeyboardInterrupt, OSError, RuntimeError):
        reader = pending_reader()
        if reader is None:
            input(PAUSE_PROMPT)
        else:
            print(PAUSE_PROMPT, end="", flush=True)
            reader.join()


def _with_settings(args: argparse.Namespace) -> int:
    """Logging at INFO first (02 §3: the obsolete-variable notice of load_settings is not lost), then the
    settings, then logging from the settings with the DEBUG log file, then the command."""
    configure_logging(args.log_level or "INFO")
    try:
        settings = load_settings(args.env_file)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    log_file = _configure_logging(args, settings)
    log.debug("eta %s: command %s (Python %s)", __version__, args.command, platform.python_version())
    handler: Handler = args.handler
    return handler(args, settings, log_file)


def _configure_logging(args: argparse.Namespace, settings: Settings) -> Path | None:
    """Console at --log-level / ETA_LOG_LEVEL, file at DEBUG: <ETA_OUTPUT_DIR>/logs/eta_<ts>.log (03 §11)."""
    level = args.log_level or settings.run.log_level
    path = log_file_path(settings.run.output_dir)
    try:
        configure_logging(level, log_file=path)
    except OSError as exc:
        configure_logging(level)
        log.warning("Cannot write the log file %s (%s) — logging to the console only", path, exc)
        return None
    return path


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Console helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _out(text: str = "") -> None:
    """One console line of a probe/command result — passed through redact() as a safety net (03 §11)."""
    print(redact(str(text)), flush=True)


def _stdin_is_tty() -> bool:
    stream = sys.stdin
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


@contextlib.contextmanager
def _default_sigint() -> Iterator[None]:
    """While blocked in input()/getpass(), let Ctrl+C raise KeyboardInterrupt at once. Inside asyncio.run() the
    loop's own SIGINT handler would only cancel the task after the read returns."""
    try:
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except (OSError, TypeError, ValueError):   # not the main thread, or no signal support
        yield
        return
    try:
        yield
    finally:
        with contextlib.suppress(OSError, TypeError, ValueError):
            signal.signal(signal.SIGINT, previous if previous is not None else signal.default_int_handler)


def _prompt(text: str) -> str:
    """input() for confirmations and answers; end of input counts as an empty answer."""
    with _default_sigint():
        try:
            return input(text)
        except EOFError:
            return ""


def _read_secret(text: str) -> str:
    """Hidden input (getpass) for pasted tokens — the value is never echoed, printed or logged."""
    with _default_sigint():
        try:
            return getpass.getpass(text)
        except EOFError:
            return ""


def _ask_yes(text: str) -> bool:
    return _prompt(text).strip().lower() in _YES


def _one_line(value: Any, limit: int = 160) -> str:
    text = " | ".join(part.strip() for part in str(value if value is not None else "").splitlines() if part.strip())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def _fmt_eta(value: datetime) -> str:
    return value.strftime("%Y-%m-%d") if value.time() == datetime.min.time() else value.strftime("%Y-%m-%d %H:%M")


def _epoch_text(value: Any) -> str:
    try:
        return datetime.fromtimestamp(float(value)).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return "unknown"


def _token_store(settings: Settings) -> TokenStore:
    state = settings.run.state_dir
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    return TokenStore(state / "tokens.json")


def _safe_name(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", text.strip()) or "probe"


def _write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return path


def _atomic_write_json(path: Path, data: Any) -> None:
    """Atomic JSON write (temp file + os.replace), 0600 on POSIX — the 04b §6.2 recovery-file convention."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


async def _guard(name: str, factory: Callable[[], Awaitable[int]]) -> int:
    """Map the errors of a command coroutine to 11 §3 exit codes (unexpected ones reach main(): exit 1)."""
    try:
        return await factory()
    except (ConfigError, AuthError, RunLockedError) as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    except EtaAutomationError as exc:   # ServiceError, CarrierLookupError, InvoiceNotFoundError, …
        log.error("%s failed: %s", name, exc)
        return EXIT_PROBE_FAILED


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta run (11 §2, 10-pipeline)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _cmd_run(args: argparse.Namespace, settings: Settings, log_file: Path | None) -> int:
    """`eta run [INPUT_CSV]`: pipeline.run() and its exit code (0/2/3/4); the file picker when no CSV is given.
    The 10 Phase 1 MSC token pre-flight is done by the pipeline with this CLI's TTY check and getpass."""
    input_csv: Path | None = args.input_csv
    if input_csv is None:
        try:
            input_csv = ui.pick_input_file()
        except ConfigError as exc:
            log.error("%s", exc)
            return EXIT_CONFIG
        if input_csv is None:
            print("No input file selected - nothing was changed.", flush=True)
            return EXIT_ABORTED
    if args.max_parallel is not None:
        settings = dataclasses.replace(
            settings, run=dataclasses.replace(settings.run, max_parallel_bookings=args.max_parallel))
    summary = asyncio.run(pipeline.run(
        settings, Path(input_csv), dry_run=args.dry_run, assume_yes=args.yes, force=args.force,
        no_email=args.no_email, only=args.only, limit=args.limit, is_tty=_stdin_is_tty,
        getpass_fn=_read_secret, log_file=log_file,
    ))
    return int(summary.exit_code)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta eta (11 §2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _cmd_eta(args: argparse.Namespace, settings: Settings, log_file: Path | None) -> int:
    return asyncio.run(_guard("eta lookup", lambda: _eta_lookup(settings, args.bookings, as_json=args.json)))


async def _eta_lookup(settings: Settings, bookings: Sequence[str], *, as_json: bool) -> int:
    """ETA lookups through the same CarrierRouter as `eta run` (memo, breaker, providers); nothing is written
    except what a provider itself keeps (the aggregator's tracking state). Exit 0 even if lookups fail."""
    run_now = SystemClock().now()
    cutoff = window_cutoff(run_now, settings.run.window_business_days)
    router = carrier_router.build_router(settings, _token_store(settings), clock=FixedClock(run_now))
    try:
        await router.prepare(list(dict.fromkeys(bookings)))
        sem = asyncio.Semaphore(max(1, settings.run.max_parallel_bookings))
        rows = await asyncio.gather(*(_eta_one(router, booking, sem, cutoff) for booking in bookings))
    finally:
        await router.aclose()
    if as_json:
        payload = {"run_now": run_now.isoformat(timespec="seconds"), "cutoff": cutoff.isoformat(timespec="seconds"),
                   "window_business_days": settings.run.window_business_days, "results": rows}
        print(redact(json.dumps(payload, indent=2, ensure_ascii=False, default=str)), flush=True)
    else:
        _print_eta_table(rows, cutoff, settings.run.window_business_days)
    return EXIT_OK


async def _eta_one(router: carrier_router.CarrierRouter, booking: str, sem: asyncio.Semaphore,
                   cutoff: datetime) -> dict[str, Any]:
    row: dict[str, Any] = {"booking": booking, "carrier": None, "provider": None, "eta": None, "source": None,
                           "is_actual": None, "in_window": None, "error": None}
    try:
        carrier, token, client = router.route(booking)
        row["carrier"] = str(carrier)
        row["provider"] = getattr(client, "provider", None)
        async with sem:
            result = await client.get_eta(token)
    except Exception as exc:  # noqa: BLE001 - every failure is reported per booking (exit stays 0, 11 §2)
        row["error"] = error_text(exc)
        return row
    row.update(eta=result.eta.isoformat(), source=result.source, is_actual=result.is_actual,
               in_window=result.eta <= cutoff)
    if result.containers:
        row["containers"] = list(result.containers)
    if result.details:
        row["details"] = dict(result.details)
    return row


def _print_eta_table(rows: Sequence[Mapping[str, Any]], cutoff: datetime, business_days: int) -> None:
    _out(f"Window cutoff: {cutoff:%Y-%m-%d %H:%M} ({business_days} US business days from now)")
    header = ("BOOKING", "CARRIER", "ETA", "IN WINDOW", "ACTUAL", "SOURCE / ERROR")
    table: list[tuple[str, ...]] = [header]
    for row in rows:
        if row["error"] is not None:
            table.append((row["booking"], row["carrier"] or "-", "-", "-", "-", _one_line(row["error"], 200)))
            continue
        eta = _fmt_eta(datetime.fromisoformat(row["eta"]))
        table.append((row["booking"], row["carrier"] or "-", eta, _yes_no(row["in_window"]),
                      _yes_no(row["is_actual"]), _one_line(row["source"], 200)))
    widths = [max(len(r[i]) for r in table) for i in range(len(header) - 1)]
    for r in table:
        _out("  ".join([*(cell.ljust(width) for cell, width in zip(r, widths, strict=False)), r[-1]]).rstrip())
    failed = sum(1 for row in rows if row["error"] is not None)
    _out(f"{len(rows) - failed} found, {failed} failed.")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta probe (11 §2) — shared context
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class _Probe:
    """What every probe needs: settings, parsed flags, the TokenStore and one timestamp for file names."""

    settings: Settings
    args: argparse.Namespace
    token_store: TokenStore
    now: datetime

    @property
    def state_dir(self) -> Path:
        return self.settings.run.state_dir

    @property
    def stamp(self) -> str:
        return self.now.strftime("%Y%m%d_%H%M%S")

    def save(self, service: str, ident: str | None, data: Any, *, stamped: bool = True) -> Path | None:
        """Write `redact(data)` to <ETA_STATE_DIR>/probe/<service>_<id>_<timestamp>.json (11 §2)."""
        name = _safe_name(ident or service)
        filename = f"{service}_{name}_{self.stamp}.json" if stamped else f"{service}_{name}.json"
        path = self.state_dir / "probe" / filename
        try:
            return _write_json(path, redact(data))
        except OSError as exc:
            log.warning("Could not save the probe output to %s: %s", path, exc)
            return None


def _probe_context(args: argparse.Namespace, settings: Settings) -> _Probe:
    return _Probe(settings=settings, args=args, token_store=_token_store(settings), now=SystemClock().now())


def _saved(path: Path | None) -> None:
    if path is not None:
        _out(f"Saved (redacted): {path}")


def _cmd_probe(args: argparse.Namespace, settings: Settings, log_file: Path | None) -> int:
    ctx = _probe_context(args, settings)
    handlers: dict[str, Callable[[_Probe], Awaitable[int]]] = {
        "qbo": _probe_qbo,
        "vship": lambda c: _probe_vship(c, c.args.booking),
        "msc": _probe_msc,
        "maersk": lambda c: _probe_maersk(c, c.args.booking),
        "cma": lambda c: _probe_cma(c, c.args.booking),
        "aggregator": lambda c: _probe_aggregator(c, c.args.booking),
        "all": _probe_all,
    }
    handler = handlers[args.service]
    return asyncio.run(_guard(f"probe {args.service}", lambda: handler(ctx)))


def _print_requests(requests: Any) -> None:
    if not isinstance(requests, list) or not requests:
        return
    _out("  Requests:")
    for r in requests:
        if not isinstance(r, Mapping):
            continue
        target = r.get("url") or r.get("path") or ""
        parts = [str(r.get("method") or "GET"), str(target)]
        if r.get("endpoint"):
            parts.insert(0, f"[{r['endpoint']}]")
        if r.get("page") is not None:
            parts.append(f"page {r['page']}")
        status = r.get("status")
        parts.append(f"-> {status if status is not None else 'no response'}")
        if r.get("elapsed_ms") is not None:
            parts.append(f"({r['elapsed_ms']} ms)")
        if r.get("events") is not None:
            parts.append(f"{r['events']} event(s)")
        if r.get("request_id"):
            parts.append(f"request id {r['request_id']}")
        if r.get("next_page"):
            parts.append("next page: yes")
        if r.get("error"):
            parts.append(f"error: {_one_line(r['error'], 200)}")
        _out("    " + " ".join(parts))


def _eta_text(eta: Any) -> str:
    if isinstance(eta, Mapping):
        parts = [f"ETA {eta.get('eta')}", f"source: {eta.get('source')}"]
        if eta.get("is_actual"):
            parts.append("actual (already arrived)")
        if eta.get("from_transshipment_rule"):
            parts.append("transshipment rule")
        containers = eta.get("containers")
        if containers:
            parts.append(f"containers: {', '.join(map(str, containers))}")
        return " | ".join(parts)
    return f"ETA: {eta}" if eta else "ETA: -"


def _print_explain(lines: Any, title: str = "Explain") -> None:
    if isinstance(lines, list) and lines:
        _out(f"  {title}:")
        for line in lines:
            _out(f"    {line}")


def _print_error(out: Mapping[str, Any]) -> None:
    if out.get("error"):
        _out(f"  ERROR: {_one_line(out['error'], 500)}")


def _result_line(ok: bool, *, text_ok: str = "OK", text_failed: str = "NOT WORKING") -> None:
    _out(f"Result: {text_ok if ok else text_failed}")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# probe vship (05 §10)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def _probe_vship(ctx: _Probe, bookings: Sequence[str]) -> int:
    """`eta probe vship --booking B` (read-only): VShipClient.probe(); saved as vship_<booking>.json (05 §10)."""
    require(ctx.settings, "vship")
    client = vship_client.build_vship(ctx.settings, ctx.token_store, read_only=True)
    code = EXIT_OK
    try:
        for booking in bookings:
            out = await client.probe(booking, run_now=ctx.now)
            path = ctx.save("vship", str(out.get("booking") or booking), out, stamped=False)
            _print_vship(out)
            _saved(path)
            if out.get("error"):
                code = EXIT_PROBE_FAILED
            _result_line(not out.get("error"))
    finally:
        await client.aclose()
    return code


def _print_vship(out: Mapping[str, Any]) -> None:
    _out(f"VShip — booking {out.get('booking')}")
    info = out.get("token")
    if isinstance(info, Mapping):
        if info.get("cached"):
            _out(f"  Login: cached session, expires {info.get('expires_at')} ({info.get('seconds_left')} s left)")
        else:
            _out("  Login: no cached session")
    candidates = out.get("candidates")
    if isinstance(candidates, list):
        _out(f"  Search candidates: {len(candidates)}")
        for c in candidates:
            if isinstance(c, Mapping):
                _out(f"    bookingId {c.get('bookingId')}  {c.get('bookingNo')}  match rank {c.get('match_rank')}")
    if out.get("booking_id") is not None:
        _out(f"  Chosen bookingId: {out.get('booking_id')} — customer: {out.get('customer_name') or '-'}")
    comments = out.get("comments")
    if isinstance(comments, list):
        _out(f"  Internal comments: {len(comments)}")
        for c in comments:
            if isinstance(c, Mapping):
                _out(f"    {c.get('createdDate')}  {_one_line(c.get('comment'), 140)}")
    notif = out.get("notif_status")
    if isinstance(notif, Mapping):
        _out(f"  Notification status: count {notif.get('count')}, have to do: {notif.get('have_to_do')}")
    if "tr_swb" in out:
        _out(f"  TR/SWB note: {_yes_no(out.get('tr_swb'))}")
    _print_error(out)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# probe msc (06 §10)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def _probe_msc(ctx: _Probe) -> int:
    args = ctx.args
    if args.token_info:
        return await _msc_token_info(ctx)
    if args.sandbox:
        return await _msc_sandbox(ctx)
    if args.compare:
        return await _msc_compare(ctx, args.booking)
    return await _msc_bookings(ctx, args.booking)


def _print_bridge_status(status: Mapping[str, Any], title: str = "Token bridge") -> None:
    if not status.get("has_token"):
        _out(f"  {title}: no MSC access token stored — paste one with `eta msc-token`")
        return
    left = status.get("seconds_left")
    state = "EXPIRED" if status.get("expired") or (isinstance(left, (int, float)) and left <= 0) else "valid"
    _out(f"  {title}: {state}, {left} s left (expires {_epoch_text(status.get('expires_at'))}, source "
         f"{status.get('source')})")
    claims = status.get("jwt_claims")
    if isinstance(claims, Mapping) and claims:
        rendered = ", ".join(f"{k} {claims[k]}" for k in ("iss", "aud", "exp", "iat") if k in claims)
        _out(f"  JWT claims: {rendered}")


def _print_api_status(status: Mapping[str, Any]) -> None:
    _out(f"  Official API: host {status.get('host')}, sandbox {_yes_no(status.get('sandbox'))}, "
         f"subscription key set {_yes_no(status.get('has_key'))}")


def _bridge_usable(status: Mapping[str, Any]) -> bool:
    left = status.get("seconds_left")
    return bool(status.get("has_token")) and isinstance(left, (int, float)) and not isinstance(left, bool) \
        and left > 0 and not status.get("expired")


async def _msc_token_info(ctx: _Probe) -> int:
    """`--token-info`: token_status() without any network call."""
    s = ctx.settings.msc
    _out(f"MSC — mode {s.mode if s is not None else 'not configured'}")
    bridge = msc.bridge_token_status(s if s is not None else MscSettings(mode="mymsc"), ctx.token_store)
    _print_bridge_status(bridge)
    if s is None:
        missing = ", ".join(ctx.settings.missing.get("msc", ())) or "its settings"
        _out(f"  MSC_MODE=api needs {missing}")
        _result_line(False)
        return EXIT_PROBE_FAILED
    if s.mode == "api":
        client = msc.build_client(ctx.settings, ctx.token_store)
        if client is None:
            raise ConfigError("MSC is not configured")
        try:
            status = client.token_status()
        finally:
            await client.aclose()
        _print_api_status(status)
        ok = bool(status.get("has_key") or status.get("sandbox"))
    else:
        ok = _bridge_usable(bridge)
        if ok and msc.token_needs_refresh(bridge):
            _out("  Note: less than 10 minutes left — `eta run` will ask for a fresh token.")
    _result_line(ok)
    return EXIT_OK if ok else EXIT_PROBE_FAILED


async def _msc_sandbox(ctx: _Probe) -> int:
    """`--sandbox [--save-raw]`: MSC's public mock sandbox, no credentials; expects 2022-12-26 (06 §10)."""
    out = await msc.probe_sandbox(ctx.token_store, settings=ctx.settings.msc, clock=FixedClock(ctx.now))
    _print_msc(out)
    _out(f"  Expected ETA {out.get('expected_eta')}: {'matches' if out.get('matches_expected') else 'DOES NOT MATCH'}")
    if ctx.args.save_raw:
        booking = str(out.get("booking") or msc.SANDBOX_PROBE_BOOKING)
        try:
            _saved(msc.save_probe_output(ctx.state_dir, booking, out, when=ctx.now))
        except OSError as exc:
            log.warning("Could not save the probe output: %s", exc)
    ok = bool(out.get("matches_expected")) and not out.get("error")
    if not ok:
        _out("  If this fails, the problem is local: proxy, TLS or installation (06 §10 step 1).")
    _result_line(ok)
    return EXIT_OK if ok else EXIT_PROBE_FAILED


async def _msc_compare(ctx: _Probe, bookings: Sequence[str]) -> int:
    """`--compare --booking …`: bridge vs official API; exit 1 on any DIFF (06 §10)."""
    require(ctx.settings, "msc")
    assert ctx.settings.msc is not None
    refs = [b.upper() for b in bookings]
    report = await msc.compare_modes(ctx.settings.msc, ctx.token_store, refs, clock=FixedClock(ctx.now))
    _out("MSC — myMSC bridge vs official API")
    for line in msc.format_compare_table(report).splitlines():
        _out(line)
    _saved(ctx.save("msc", "compare", report))
    _result_line(not report.get("any_diff"), text_ok="all MATCH", text_failed="DIFF found")
    return EXIT_PROBE_FAILED if report.get("any_diff") else EXIT_OK


async def _msc_bookings(ctx: _Probe, bookings: Sequence[str]) -> int:
    """`--booking EBKG… [--booking …]`: real lookups in the configured mode, each saved redacted (06 §10)."""
    require(ctx.settings, "msc")
    client = msc.build_client(ctx.settings, ctx.token_store, clock=FixedClock(ctx.now))
    if client is None:
        raise ConfigError("MSC is not configured")
    code = EXIT_OK
    try:
        for booking in bookings:
            ref = booking.upper()
            out = await client.probe(ref)
            _print_msc(out)
            try:
                _saved(msc.save_probe_output(ctx.state_dir, ref, out, when=ctx.now))
            except OSError as exc:
                log.warning("Could not save the probe output: %s", exc)
            if out.get("error"):
                code = EXIT_PROBE_FAILED
            _result_line(not out.get("error"))
    finally:
        await client.aclose()
    return code


def _print_msc(out: Mapping[str, Any]) -> None:
    sandbox = " — PUBLIC SANDBOX (mock data)" if out.get("sandbox") else ""
    _out(f"MSC — booking {out.get('booking')} (mode {out.get('mode')}){sandbox}")
    bridge = out.get("bridge")
    if isinstance(bridge, Mapping):
        _print_bridge_status(bridge)
    api = out.get("api")
    if isinstance(api, Mapping):
        _print_api_status(api)
    _print_requests(out.get("requests"))
    if out.get("event_count") is not None:
        _out(f"  Events: {out.get('event_count')}")
    _out(f"  {_eta_text(out.get('eta'))}")
    _print_explain(out.get("explain"))
    _print_error(out)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# probe maersk (07 §10) / cma (08 §10) / aggregator (09 §B.11)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def _probe_maersk(ctx: _Probe, bookings: Sequence[str]) -> int:
    require(ctx.settings, "maersk")
    client = maersk.build_client(ctx.settings, ctx.token_store, clock=FixedClock(ctx.now))
    if client is None:
        raise ConfigError("Maersk is not configured")
    code = EXIT_OK
    try:
        for booking in bookings:
            out = await client.probe(booking)
            _print_maersk(out)
            _saved(ctx.save("maersk", str(out.get("booking") or booking), out))
            if out.get("error"):
                code = EXIT_PROBE_FAILED
            _result_line(not out.get("error"))
    finally:
        await client.aclose()
    return code


def _print_maersk(out: Mapping[str, Any]) -> None:
    _out(f"Maersk — booking {out.get('booking')}")
    config = out.get("config")
    if isinstance(config, Mapping):
        _out(f"  Config: {config.get('base_url')}, ETA target {config.get('eta_target')}, multi-container policy "
             f"{config.get('policy')}, date only {config.get('date_only')}, page limit {config.get('page_limit')}")
    auth = out.get("token")
    if isinstance(auth, Mapping):
        if auth.get("ok"):
            _out(f"  Access token: OK (cached {_yes_no(auth.get('cached'))}, expires in {auth.get('expires_in_s')} s, "
                 f"scope {auth.get('scope')})")
        else:
            _out(f"  Access token: FAILED — {_one_line(auth.get('error'), 300)}")
    _print_requests(out.get("requests"))
    counts = out.get("counts")
    if isinstance(counts, Mapping) and counts:
        _out("  Event counts: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    _out(f"  {_eta_text(out.get('eta'))}")
    alternatives = out.get("alternatives")
    if isinstance(alternatives, Mapping):
        for target in ("final", "pod"):
            alt = alternatives.get(target)
            if isinstance(alt, Mapping):
                _out(f"  Alternative ({target}): {alt.get('eta')} — {alt.get('basis')}")
            else:
                _out(f"  Alternative ({target}): -")
    _print_explain(out.get("explain"))
    _print_error(out)


async def _probe_cma(ctx: _Probe, bookings: Sequence[str]) -> int:
    """`eta probe cma --booking B`: prints the billable-call count first (11 §2, 08 §10)."""
    require(ctx.settings, "cma")
    per = cma.PROBE_MIN_BILLABLE_CALLS
    _out(f"Note: each CMA probe calls both CMA endpoints — at least {per} billable API calls per booking, more if "
         f"CMA returns several pages: {len(bookings)} booking(s), at least {per * len(bookings)} billable call(s).")
    client = cma.build_client(ctx.settings, ctx.token_store, clock=FixedClock(ctx.now))
    if client is None:
        raise ConfigError("CMA is not configured")
    code = EXIT_OK
    try:
        for booking in bookings:
            out = await client.probe(booking)
            _print_cma(out)
            _saved(ctx.save("cma", str(out.get("booking") or booking), out))
            if out.get("error"):
                code = EXIT_PROBE_FAILED
            _result_line(not out.get("error"))
    finally:
        await client.aclose()
    return code


def _print_cma(out: Mapping[str, Any]) -> None:
    _out(f"CMA — booking {out.get('booking')}")
    info = out.get("info")
    if isinstance(info, Mapping):
        _out(f"  Auth mode {info.get('auth_mode')}, lookup mode {info.get('lookup_mode')}, {info.get('base_url')}, "
             f"page limit {info.get('page_limit')}, behalfOf set {_yes_no(info.get('behalf_of_set'))}")
        oauth = info.get("token")
        if isinstance(oauth, Mapping):
            if oauth.get("token_ok"):
                _out(f"  OAuth access token: OK (expires in {oauth.get('expires_in')} s, scope {oauth.get('scope')})")
            else:
                _out(f"  OAuth access token: FAILED — {_one_line(oauth.get('error'), 300)}")
    _print_requests(out.get("requests"))
    _out(f"  {_eta_text(out.get('eta'))}")
    analysis = out.get("analysis")
    if isinstance(analysis, Mapping):
        for mode in ("reference", "dcsa_query"):
            part = analysis.get(mode)
            if not isinstance(part, Mapping):
                continue
            if part.get("error"):
                _out(f"  Endpoint {mode}: {_one_line(part.get('error'), 300)}")
                continue
            counts = part.get("counts")
            rendered = ", ".join(f"{k} {v}" for k, v in counts.items()) if isinstance(counts, Mapping) else "-"
            _out(f"  Endpoint {mode}: {part.get('event_count')} event(s), POD {part.get('pod')}, location types "
                 f"{part.get('shipment_location_types')}, counts: {rendered}")
            _print_explain(part.get("explain"), title=f"Explain ({mode})")
        _out(f"  Endpoints agree: {_yes_no(analysis.get('endpoints_agree'))}")
    _print_error(out)


async def _probe_aggregator(ctx: _Probe, bookings: Sequence[str]) -> int:
    """`eta probe aggregator [--booking B]` (read-only; never creates a tracking, 09 §B.11)."""
    require(ctx.settings, "aggregator")
    client = aggregator_carrier.build_client(ctx.settings, ctx.token_store, clock=FixedClock(ctx.now))
    if client is None:
        raise ConfigError("The aggregator is not configured")
    targets: list[str | None] = list(bookings) or [None]
    code = EXIT_OK
    try:
        for booking in targets:
            out = await client.probe(None, booking)
            _print_aggregator(out)
            _saved(ctx.save("aggregator", booking or "account", out))
            ok = bool(out.get("account_ok")) and not out.get("error")
            if not ok:
                code = EXIT_PROBE_FAILED
            _result_line(ok)
    finally:
        await client.aclose()
    return code


def _print_aggregator(out: Mapping[str, Any]) -> None:
    _out(f"Aggregator {out.get('service')} ({out.get('provider')}) — {out.get('base_url')}")
    _out(f"  Account: {'OK' if out.get('account_ok') else 'NOT OK'}")
    carriers = out.get("carriers")
    if isinstance(carriers, list) and carriers:
        _out("  Active carriers: " + ", ".join(
            f"{c.get('name')} ({c.get('scac')})" if isinstance(c, Mapping) else str(c) for c in carriers))
    scac = out.get("scac_check")
    if isinstance(scac, Mapping) and scac:
        _out("  Carrier codes: " + ", ".join(
            f"{k} {v.get('scac')} ({'active' if v.get('active') else 'NOT active'})" if isinstance(v, Mapping)
            else f"{k} {v}" for k, v in scac.items()))
    _print_requests(out.get("requests"))
    if out.get("booking"):
        _out(f"  Booking {out.get('booking')} ({out.get('carrier')}, code {out.get('scac')}), tracking id "
             f"{out.get('tracking_id') or '-'}")
        if out.get("note"):
            _out(f"  Note: {_one_line(out.get('note'), 300)}")
        _out(f"  {_eta_text(out.get('eta'))}")
        _print_explain(out.get("explain"))
    _print_error(out)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# probe all (11 §2: every configured read-only probe that has the arguments it needs)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def _probe_all(ctx: _Probe) -> int:
    s = ctx.settings
    by_carrier: dict[Carrier, list[str]] = {}
    unroutable: list[str] = []
    for booking in ctx.args.booking:
        try:
            carrier, token = carrier_router.identify_carrier(booking)
        except EtaAutomationError:
            unroutable.append(booking)
            continue
        by_carrier.setdefault(carrier, []).append(token)
    if unroutable:
        _out(f"Not a recognised carrier booking (used for VShip only): {', '.join(unroutable)}")

    def missing(service: str) -> str:
        return ", ".join(s.missing.get(service, ())) or "its settings"

    plan: list[tuple[str, bool, str, str, Callable[[], Awaitable[int]]]] = [
        ("qbo", s.qbo is not None, missing("qbo"), "", lambda: _qbo_read_probe(ctx)),
        ("vship", s.vship is not None, missing("vship"), "" if ctx.args.booking else "--booking B",
         lambda: _probe_vship(ctx, ctx.args.booking)),
        ("msc", s.msc is not None, missing("msc"), "" if by_carrier.get(Carrier.MSC) else f"--booking {_MSC_BOOKING_HINT}",
         lambda: _msc_bookings(ctx, by_carrier[Carrier.MSC])),
        ("maersk", s.maersk is not None, missing("maersk"),
         "" if by_carrier.get(Carrier.MAERSK) else "--booking <9-digit Maersk booking>",
         lambda: _probe_maersk(ctx, by_carrier[Carrier.MAERSK])),
        ("cma", s.cma is not None, missing("cma"), "" if by_carrier.get(Carrier.CMA) else "--booking NAM…",
         lambda: _probe_cma(ctx, by_carrier[Carrier.CMA])),
        ("aggregator", s.aggregator is not None, missing("aggregator"), "", lambda: _probe_aggregator(ctx, [])),
    ]
    results: list[tuple[str, str]] = []
    worst = EXIT_OK
    for name, configured, missing_vars, needs, run in plan:
        if not configured:
            results.append((name, f"skipped — not configured (set {missing_vars})"))
            continue
        if needs:
            results.append((name, f"skipped — needs {needs}"))
            continue
        _out("")
        _out(f"=== {name} ===")
        code = await _guard(f"probe {name}", run)
        worst = max(worst, code)
        results.append((name, "OK" if code == EXIT_OK else f"NOT WORKING (exit {code})"))
    _out("")
    _out("Probe summary:")
    for name, status in results:
        _out(f"  {name.ljust(10)} {status}")
    return worst


# ════════════════════════════════════════════════════════════════════════════════════════════════
# probe qbo (04 §10, 04b §10)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def _probe_qbo(ctx: _Probe) -> int:
    args = ctx.args
    if args.write_test:
        return await _qbo_write_test(ctx, args.invoice)
    if args.prefs_roundtrip:
        return await _qbo_prefs_roundtrip(ctx)
    if args.send_test:
        return await _qbo_send_test(ctx, args.invoice)
    if args.restore_template:
        return await _qbo_restore_template(ctx)
    return await _qbo_read_probe(ctx)


def _qbo_settings(settings: Settings) -> QboSettings:
    require(settings, "qbo")
    assert settings.qbo is not None
    return settings.qbo


def _company_label(s: QboSettings) -> str:
    return f"{'SANDBOX' if s.sandbox else 'PRODUCTION'} company, realm {s.realm_id}"


def _open_qbo(settings: Settings, token_store: TokenStore, *, read_only: bool, qbo_settings: QboSettings | None = None,
              http: httpx.AsyncClient | None = None) -> QboClient:
    """build_qbo() (04 §6.8) with two probe additions: other QboSettings (the --send-test verification-gate
    bypass) and an injected HTTP client (write probes record the exact request/response bodies)."""
    s = qbo_settings or _qbo_settings(settings)
    return QboClient(
        s, QboAuth(s, token_store),
        http=http,
        token_store=token_store,
        state_dir=settings.run.state_dir,
        email_settings=settings.email,
        read_only=read_only,
        email_missing=tuple(settings.missing.get("email", ())),
    )


def _body(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        if raw.startswith(b"%PDF"):
            return f"<PDF, {len(raw)} bytes>"
        text = raw.decode("utf-8", "replace")
        return text if len(text) <= _BODY_TEXT_MAX else text[:_BODY_TEXT_MAX] + "…"


def _recorder(exchanges: list[dict[str, Any]]) -> Callable[[httpx.Response], Awaitable[None]]:
    """httpx response hook: keep method, URL, status and both bodies of every QuickBooks API call (the token
    endpoint is not recorded — QboAuth has its own client)."""

    async def on_response(response: httpx.Response) -> None:
        await response.aread()
        request = response.request
        exchanges.append({"method": request.method, "url": str(request.url), "status": response.status_code,
                          "request_body": _body(request.content), "response_body": _body(response.content)})

    return on_response


@contextlib.asynccontextmanager
async def _qbo_session(ctx: _Probe, *, read_only: bool, qbo_settings: QboSettings | None = None,
                       record: bool = False) -> AsyncIterator[tuple[QboClient, list[dict[str, Any]]]]:
    """A QboClient for one probe (closed afterwards) and the list of recorded exchanges (record=True)."""
    s = qbo_settings or _qbo_settings(ctx.settings)
    exchanges: list[dict[str, Any]] = []
    http: httpx.AsyncClient | None = None
    if record:
        http = new_async_client(base_url=s.api_base)
        http.event_hooks["response"].append(_recorder(exchanges))
    qbo = _open_qbo(ctx.settings, ctx.token_store, read_only=read_only, qbo_settings=s, http=http)
    try:
        yield qbo, exchanges
    finally:
        try:
            await qbo.aclose()
        finally:
            if http is not None:
                await http.aclose()


def _write_refusal(ctx: _Probe, flag: str) -> int | None:
    """11 §2: QuickBooks write probes need --yes on production and an interactive confirmation."""
    s = _qbo_settings(ctx.settings)
    if not s.sandbox and not ctx.args.yes:
        _out(f"Refusing: `eta probe qbo {flag}` writes to your PRODUCTION QuickBooks company (realm {s.realm_id}). "
             "Add --yes if that is intended (run it with QB_SANDBOX=true and sandbox keys first).")
        return EXIT_ABORTED
    if not _stdin_is_tty():
        _out(f"Refusing: `eta probe qbo {flag}` writes to QuickBooks and must be confirmed interactively, but "
             "stdin is not a terminal.")
        return EXIT_ABORTED
    return None


def _aborted() -> int:
    _out("Aborted: nothing was changed.")
    return EXIT_ABORTED


# ── read-only probe (04 §10 step 2) ──
def _invoice_message(prefs: Any) -> Mapping[str, Any] | None:
    messages = prefs.get("EmailMessagesPrefs") if isinstance(prefs, Mapping) else None
    invoice = messages.get("InvoiceMessage") if isinstance(messages, Mapping) else None
    return invoice if isinstance(invoice, Mapping) else None


def _template_subject(prefs: Any) -> str | None:
    value = (_invoice_message(prefs) or {}).get("Subject")
    return value if isinstance(value, str) else None


def _template_message(prefs: Any) -> str | None:
    value = (_invoice_message(prefs) or {}).get("Message")
    return value if isinstance(value, str) else None


def _connection_info(expires_at: float | None, now: datetime) -> dict[str, Any]:
    if expires_at is None:
        return {"refreshed": True, "connection_expires_at": None, "days_left": None}
    when = datetime.fromtimestamp(expires_at)
    return {"refreshed": True, "connection_expires_at": when.isoformat(timespec="seconds"),
            "days_left": (when - now).days}


def _invoice_summary(inv: InvoiceSnapshot, spec: EtaFieldSpec | None) -> dict[str, Any]:
    return {"id": inv.id, "doc_number": inv.doc_number, "customer": inv.customer_name,
            "total": str(inv.total_amount), "balance": str(inv.balance), "recipients": list(inv.recipients),
            "email_status": inv.email_status, "eta_value": inv.eta_value.isoformat() if inv.eta_value else None,
            "eta_field": dataclasses.asdict(spec) if spec is not None else None}


async def _qbo_read_probe(ctx: _Probe) -> int:
    """`eta probe qbo` (read-only, production OK): token refresh (connection expiry, never the token), company name,
    invoice e-mail template, SalesFormsPrefs.ETransactionAttachPDF / CustomTxnNumbers (04 §10 step 2)."""
    s = _qbo_settings(ctx.settings)
    recovery = ctx.state_dir / qbo_email.RECOVERY_FILE_NAME
    out: dict[str, Any] = {
        "service": "qbo", "realm_id": s.realm_id, "environment": "sandbox" if s.sandbox else "production",
        "api_base": s.api_base, "minor_version": s.minor_version, "email_strategy": s.email_strategy,
        "email_verified": qbo_email.is_verified(ctx.state_dir, s.realm_id, s.email_strategy),
        "template_recovery_file": str(recovery) if recovery.exists() else None,
        "connection": None, "company": None, "email_template": None, "sales_forms_prefs": None, "invoice": None,
        "raw": {}, "error": None,
    }
    code = EXIT_OK
    async with _qbo_session(ctx, read_only=True) as (qbo, _):
        try:
            await qbo.auth.force_refresh()
        except (AuthError, ConfigError) as exc:
            out["error"] = f"QuickBooks authentication failed: {exc}"
            code = EXIT_CONFIG
        except EtaAutomationError as exc:
            out["error"] = f"QuickBooks token refresh failed: {exc}"
            code = EXIT_PROBE_FAILED
        else:
            out["connection"] = _connection_info(qbo.auth.refresh_token_expires_at, ctx.now)
            try:
                info = await qbo.get_company_info()
                out["raw"]["companyinfo"] = info
                out["company"] = {"name": info.get("CompanyName")}
                prefs = await qbo.get_preferences()
                out["raw"]["preferences"] = prefs
                out["email_template"] = {"subject": _template_subject(prefs), "message": _template_message(prefs)}
                sales = prefs.get("SalesFormsPrefs")
                sales = sales if isinstance(sales, Mapping) else {}
                out["sales_forms_prefs"] = {"ETransactionAttachPDF": sales.get("ETransactionAttachPDF"),
                                            "CustomTxnNumbers": sales.get("CustomTxnNumbers")}
                if ctx.args.invoice:
                    inv = await qbo.get_invoice_by_doc_number(ctx.args.invoice)
                    out["invoice"] = _invoice_summary(inv, qbo.eta_field)
            except AuthError as exc:
                out["error"] = str(exc)
                code = EXIT_CONFIG
            except EtaAutomationError as exc:
                out["error"] = str(exc)
                code = EXIT_PROBE_FAILED
    _print_qbo_read(out)
    _saved(ctx.save("qbo", s.realm_id, out))
    _result_line(code == EXIT_OK)
    return code


def _print_qbo_read(out: Mapping[str, Any]) -> None:
    _out(f"QuickBooks Online — {out['environment']}, realm {out['realm_id']} ({out['api_base']}, minor version "
         f"{out['minor_version']})")
    connection = out.get("connection")
    if isinstance(connection, Mapping):
        if connection.get("connection_expires_at"):
            _out(f"  Access refresh: OK — connection valid until {connection['connection_expires_at'][:10]} "
                 f"({connection.get('days_left')} days left)")
        else:
            _out("  Access refresh: OK — connection expiry not reported")
    company = out.get("company")
    if isinstance(company, Mapping):
        _out(f"  Company: {company.get('name')}")
    template = out.get("email_template")
    if isinstance(template, Mapping):
        _out(f"  Invoice e-mail subject: {template.get('subject')!r}")
        _out(f"  Invoice e-mail message: {_one_line(template.get('message'), 200)!r}")
    sales = out.get("sales_forms_prefs")
    if isinstance(sales, Mapping):
        _out(f"  SalesFormsPrefs.ETransactionAttachPDF: {sales.get('ETransactionAttachPDF')}")
        _out(f"  SalesFormsPrefs.CustomTxnNumbers: {sales.get('CustomTxnNumbers')}")
    verified = out.get("email_verified")
    hint = "" if verified else " (run `eta probe qbo --send-test --invoice <test DocNumber>`, 04b §10)"
    _out(f"  E-mail strategy: {out.get('email_strategy')} — verified for this company: {_yes_no(verified)}{hint}")
    if out.get("template_recovery_file"):
        _out(f"  WARNING: a template recovery file exists ({out['template_recovery_file']}) — run "
             "`eta probe qbo --restore-template`")
    invoice = out.get("invoice")
    if isinstance(invoice, Mapping):
        _out(f"  Invoice #{invoice.get('doc_number')} (Id {invoice.get('id')}): customer {invoice.get('customer')}, "
             f"balance {invoice.get('balance')} of {invoice.get('total')}, ETA {invoice.get('eta_value') or '-'}, "
             f"e-mail to {', '.join(invoice.get('recipients') or []) or '-'}")
    _print_error(out)


# ── --write-test (04 §10 step 4) ──
def _other_custom_fields(inv: InvoiceSnapshot, spec: EtaFieldSpec) -> dict[str, Any]:
    """DefinitionId → value of every custom field with a value, except the ETA field (must not change)."""
    values: dict[str, Any] = {}
    for entry in inv.custom_fields:
        definition = str(entry.get("DefinitionId"))
        if definition == spec.definition_id:
            continue
        value = next((entry.get(k) for k in VALUE_KEYS if entry.get(k) not in (None, "")), None)
        if value is not None:
            values[definition] = value
    return values


def _verify_eta(step: str, check: InvoiceSnapshot, *, expected: date, doc_number: str, others: Mapping[str, Any],
                spec: EtaFieldSpec) -> dict[str, Any]:
    problems: list[str] = []
    if check.eta_value != expected:
        problems.append(f"ETA read back as {check.eta_value} (expected {expected})")
    if check.doc_number != doc_number:
        problems.append(f"DocNumber changed to {check.doc_number!r}")
    after = _other_custom_fields(check, spec)
    if after != dict(others):
        problems.append(f"other custom fields changed: {dict(others)} -> {after}")
    return {"step": step, "ok": not problems, "expected": expected.isoformat(),
            "read_back": check.eta_value.isoformat() if check.eta_value else None, "problems": problems}


async def _restore_eta(qbo: QboClient, inv: InvoiceSnapshot, original: date, spec: EtaFieldSpec,
                       others: Mapping[str, Any]) -> dict[str, Any]:
    """Put the original ETA back (always attempted after the test write) and verify it with a fresh read."""
    try:
        current = await qbo.get_invoice(inv.id)
        if current.eta_value != original:
            await qbo.update_invoice(current, eta=original)
        check = await qbo.get_invoice(inv.id)
        return _verify_eta("restore", check, expected=original, doc_number=inv.doc_number, others=others, spec=spec)
    except Exception as exc:  # noqa: BLE001 - reported loudly with manual instructions, never swallowed silently
        return {"step": "restore", "ok": False, "expected": original.isoformat(), "read_back": None,
                "problems": [f"{type(exc).__name__}: {exc}"]}


async def _qbo_write_test(ctx: _Probe, doc: str) -> int:
    """`eta probe qbo --write-test --invoice DOC` (04 §10 step 4): ETA → original+1 day, verify with a fresh GET,
    restore the original value, verify again. Never touches DocNumber or e-mail. Exact bodies are saved."""
    s = _qbo_settings(ctx.settings)
    refusal = _write_refusal(ctx, "--write-test")
    if refusal is not None:
        return refusal
    out: dict[str, Any] = {"service": "qbo", "probe": "write-test", "realm_id": s.realm_id,
                           "environment": "sandbox" if s.sandbox else "production", "invoice": doc, "steps": [],
                           "result": "FAIL", "error": None}
    with run_lock(ctx.state_dir):
        async with _qbo_session(ctx, read_only=False, record=True) as (qbo, exchanges):
            out["exchanges"] = exchanges
            inv = await qbo.get_invoice_by_doc_number(doc)
            spec = qbo.eta_field
            original = inv.eta_value
            out["invoice_id"] = inv.id
            out["eta_field"] = dataclasses.asdict(spec) if spec is not None else None
            _out(f"QuickBooks write test — {_company_label(s)}")
            _out(f"  Invoice #{inv.doc_number} (Id {inv.id}), customer {inv.customer_name or '-'}")
            if spec is None or original is None:
                out["error"] = (f"The ETA field of invoice #{inv.doc_number} is empty or was not found. The write test "
                                "restores the exact original value, so it needs an invoice whose ETA is filled in "
                                "(clearing a custom field is not part of the documented API). Set an ETA on the test "
                                "invoice in QuickBooks first, or check the field with `eta qbo-fields --invoice "
                                f"{inv.doc_number}`.")
                _print_error(out)
                _saved(ctx.save("qbo", f"write-test_{doc}", out))
                _result_line(False, text_failed="NOT RUN (nothing was changed)")
                return EXIT_PROBE_FAILED
            test_value = original + timedelta(days=1)
            others = _other_custom_fields(inv, spec)
            _out(f"  ETA field '{spec.name}' (DefinitionId {spec.definition_id}, {spec.type}, {spec.value_key}), "
                 f"from {spec.source}")
            _out(f"  Plan: ETA {original} -> {test_value}, verify with a fresh read, then back to {original} and "
                 "verify again. DocNumber and e-mail are never touched.")
            if not _ask_yes(f"Change invoice #{inv.doc_number} in QuickBooks now? [y/N] "):
                return _aborted()
            try:
                try:
                    await qbo.update_invoice(inv, eta=test_value)
                    check = await qbo.get_invoice(inv.id)
                    step = _verify_eta("set", check, expected=test_value, doc_number=inv.doc_number, others=others,
                                       spec=spec)
                except EtaAutomationError as exc:
                    step = {"step": "set", "ok": False, "expected": test_value.isoformat(), "read_back": None,
                            "problems": [f"{type(exc).__name__}: {exc}"]}
                out["steps"].append(step)
            finally:
                out["steps"].append(await _restore_eta(qbo, inv, original, spec, others))
    passed = all(step["ok"] for step in out["steps"])
    out["result"] = "PASS" if passed else "FAIL"
    for step in out["steps"]:
        label = f"set ETA {step['expected']}" if step["step"] == "set" else f"restore ETA {step['expected']}"
        detail = "; ".join(step["problems"]) if step["problems"] else f"read back {step['read_back']}"
        _out(f"  {label}: {'PASS' if step['ok'] else 'FAIL'} — {detail}")
    for exchange in out["exchanges"]:
        if exchange["method"] == "POST":
            _out(f"  POST {exchange['url']} -> {exchange['status']}")
            _out(f"    request body: {json.dumps(exchange['request_body'], ensure_ascii=False, default=str)}")
            _out(f"    response body: {json.dumps(exchange['response_body'], ensure_ascii=False, default=str)}")
    restore = out["steps"][-1]
    if not restore["ok"]:
        _out(f"  RESTORE FAILED: set the ETA of invoice #{doc} back to {restore['expected']} in QuickBooks by hand.")
    elif not passed:
        _out("  The ETA field could not be written as expected — see 04 §8.1 (fallbacks).")
    _saved(ctx.save("qbo", f"write-test_{doc}", out))
    _result_line(passed, text_ok="PASS", text_failed="FAIL")
    return EXIT_OK if passed else EXIT_PROBE_FAILED


# ── --prefs-roundtrip (04b §10 step 2) ──
def _prefs_body(prefs: Mapping[str, Any], subject: str, message: str | None, mode: str) -> dict[str, Any]:
    """The Preferences write body as TemplateSwapEmailer sends it (04b §6.2 _write_subject)."""
    if mode == "sparse":
        invoice_message: dict[str, Any] = {"Subject": subject}
        if message is not None:
            invoice_message["Message"] = message
        return {"Id": prefs.get("Id"), "SyncToken": prefs.get("SyncToken"), "sparse": True,
                "EmailMessagesPrefs": {"InvoiceMessage": invoice_message}}
    body = copy.deepcopy(dict(prefs))
    body["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] = subject
    return body


def _json_diff(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Differences between two JSON values as [{"path", "before", "after"}]; top-level SyncToken/MetaData are
    ignored (04b §10 step 2: the only keys a Preferences write may change)."""
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        changes: list[dict[str, Any]] = []
        for key in sorted(set(before) | set(after), key=str):
            if not path and key in _PREFS_IGNORED:
                continue
            sub = f"{path}.{key}" if path else str(key)
            if key not in before:
                changes.append({"path": sub, "before": "<absent>", "after": after[key]})
            elif key not in after:
                changes.append({"path": sub, "before": before[key], "after": "<absent>"})
            else:
                changes.extend(_json_diff(before[key], after[key], sub))
        return changes
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        changes = []
        for index, (left, right) in enumerate(zip(before, after, strict=True)):
            changes.extend(_json_diff(left, right, f"{path}[{index}]"))
        return changes
    return [] if before == after else [{"path": path or "(root)", "before": before, "after": after}]


async def _qbo_prefs_roundtrip(ctx: _Probe) -> int:
    """`eta probe qbo --prefs-roundtrip`: GET → POST unchanged → GET → diff (only SyncToken/MetaData may differ) →
    POST with a probe subject → GET → diff (only the subject) → restore → GET → diff vs. the start (identical).
    A 04b §6.2 recovery file covers a crash while the probe subject is in place (04b §10 step 2)."""
    s = _qbo_settings(ctx.settings)
    refusal = _write_refusal(ctx, "--prefs-roundtrip")
    if refusal is not None:
        return refusal
    mode = s.prefs_update_mode
    recovery = ctx.state_dir / qbo_email.RECOVERY_FILE_NAME
    probe_subject = (f"ETA probe {ctx.now:%Y%m%d-%H%M%S} — Invoice for Booking no. PREFS-TEST. "
                     "***ROUND TRIP TEST, NOT SENT****")
    out: dict[str, Any] = {"service": "qbo", "probe": "prefs-roundtrip", "realm_id": s.realm_id,
                           "environment": "sandbox" if s.sandbox else "production", "prefs_update_mode": mode,
                           "probe_subject": probe_subject, "snapshots": {}, "steps": [], "result": "FAIL",
                           "error": None}
    with run_lock(ctx.state_dir):
        _out(f"QuickBooks Preferences round trip — {_company_label(s)}, QB_PREFS_UPDATE_MODE={mode}")
        if recovery.exists():
            out["error"] = (f"An e-mail template recovery file exists ({recovery}): a previous run may have left the "
                            "template modified. Run `eta probe qbo --restore-template` first.")
            _print_error(out)
            _result_line(False, text_failed="NOT RUN (nothing was changed)")
            return EXIT_PROBE_FAILED
        async with _qbo_session(ctx, read_only=False, record=True) as (qbo, exchanges):
            out["exchanges"] = exchanges
            start = await qbo.get_preferences()
            out["snapshots"]["start"] = start
            subject, message = _template_subject(start), _template_message(start)
            problem = None
            if _invoice_message(start) is None or not subject:
                problem = ("QuickBooks Preferences has no EmailMessagesPrefs.InvoiceMessage subject — template_swap "
                           "cannot work; use QB_EMAIL_STRATEGY=graph or smtp (04b §8).")
            elif qbo_email.ETA_SUBJECT_RE.match(subject):
                problem = (f"The invoice e-mail subject currently looks like an ETA subject ({subject!r}) — restore the "
                           "normal subject in QuickBooks first.")
            if problem is not None or subject is None:
                out["error"] = problem
                _print_error(out)
                _saved(ctx.save("qbo", f"prefs-roundtrip_{s.realm_id}", out))
                _result_line(False, text_failed="NOT RUN (nothing was changed)")
                return EXIT_PROBE_FAILED
            _out(f"  Current invoice e-mail subject: {subject!r}")
            _out("  Plan: write the Preferences 3 times — unchanged, with the probe subject, then the original subject "
                 "again — and compare a fresh read after each write.")
            if not _ask_yes("Write the QuickBooks company Preferences now? [y/N] "):
                return _aborted()
            await _roundtrip_steps(ctx, qbo, out, start, subject, message, probe_subject, recovery)
    passed = len(out["steps"]) == 3 and all(step["ok"] for step in out["steps"])
    out["result"] = "PASS" if passed else "FAIL"
    for step in out["steps"]:
        _out(f"  {step['label']}: {'PASS' if step['ok'] else 'FAIL'}")
        for change in step.get("differences", []):
            _out(f"    {change['path']}: {_one_line(change['before'], 100)!r} -> {_one_line(change['after'], 100)!r}")
        if step.get("error"):
            _out(f"    {_one_line(step['error'], 300)}")
    restored = next((step for step in out["steps"] if step["name"] == "restore"), None)
    if restored is not None and not restored.get("subject_restored"):
        _out(f"  The template may still be modified: {qbo_email.manual_restore_instructions(str(subject), recovery)}")
    elif restored is not None and not restored["ok"]:
        _out("  The subject is back, but the Preferences differ from the start as listed above — check them in "
             "QuickBooks.")
    first = out["steps"][0] if out["steps"] else None
    if first is not None and first["name"] == "unchanged" and not first["ok"]:
        _out("  Step 1 failed, so no probe subject was written.")
        if first.get("differences"):
            # The unchanged full write-back itself changed company settings; this probe cannot undo that.
            _out("  The probe cannot undo the write-back: restore them by hand in QuickBooks (Settings → Account and "
                 "settings):")
            for change in first["differences"]:
                before = change["before"]
                fix = "remove it" if before == "<absent>" else f"set it back to {_one_line(before, 100)}"
                _out(f"    {change['path']}: {fix}")
    if not passed:
        _out("  template_swap is not safe for this company — use QB_EMAIL_STRATEGY=graph (or smtp), 04b §8.")
    _saved(ctx.save("qbo", f"prefs-roundtrip_{s.realm_id}", out))
    _result_line(passed, text_ok="PASS", text_failed="FAIL")
    return EXIT_OK if passed else EXIT_PROBE_FAILED


async def _roundtrip_steps(ctx: _Probe, qbo: QboClient, out: dict[str, Any], start: Mapping[str, Any], subject: str,
                           message: str | None, probe_subject: str, recovery: Path) -> None:
    mode = qbo.settings.prefs_update_mode
    steps: list[dict[str, Any]] = out["steps"]
    snapshots: dict[str, Any] = out["snapshots"]
    # 1. unchanged — a write-back that is not lossless already fails the probe: no further writes then
    try:
        await qbo.update_preferences(_prefs_body(start, subject, message, mode))
        after = await qbo.get_preferences()
    except EtaAutomationError as exc:
        steps.append({"name": "unchanged", "label": "1. write back unchanged", "ok": False, "error": str(exc)})
        return
    snapshots["after_unchanged_write"] = after
    changes = _json_diff(start, after)
    steps.append({"name": "unchanged", "label": "1. write back unchanged", "ok": not changes, "differences": changes})
    if changes:
        return
    # 2. probe subject (recovery file first, so a crash is repaired by the next run or --restore-template)
    try:
        _atomic_write_json(recovery, {
            "realm_id": qbo.settings.realm_id, "original_subject": subject, "original_message": message,
            "swapped_subject": probe_subject, "invoice_id": None, "written_at": ctx.now.isoformat(timespec="seconds"),
            "pid": os.getpid(), "written_by": "eta probe qbo --prefs-roundtrip",
        })
    except OSError as exc:
        steps.append({"name": "subject", "label": "2. write only a probe subject", "ok": False,
                      "error": f"cannot write the recovery file {recovery} ({exc}) — no probe subject was written"})
        return
    try:
        try:
            await qbo.update_preferences(_prefs_body(after, probe_subject, message, mode))
            changed = await qbo.get_preferences()
            snapshots["after_probe_subject"] = changed
            changes = _json_diff(start, changed)
            only_subject = (len(changes) == 1 and changes[0]["path"] == _SUBJECT_PATH
                            and changes[0]["after"] == probe_subject)
            steps.append({"name": "subject", "label": "2. write only a probe subject", "ok": only_subject,
                          "differences": changes})
        except EtaAutomationError as exc:
            steps.append({"name": "subject", "label": "2. write only a probe subject", "ok": False, "error": str(exc)})
    finally:
        steps.append(await _restore_subject(qbo, start, subject, message, recovery, snapshots))


async def _restore_subject(qbo: QboClient, start: Mapping[str, Any], subject: str, message: str | None,
                           recovery: Path, snapshots: dict[str, Any]) -> dict[str, Any]:
    """Step 3 — always attempted: put the original subject back, verify the Preferences equal the start."""
    label = "3. restore the original subject"
    try:
        current = await qbo.get_preferences()
        if _template_subject(current) != subject:
            await qbo.update_preferences(_prefs_body(current, subject, message, qbo.settings.prefs_update_mode))
        final = await qbo.get_preferences()
        snapshots["after_restore"] = final
    except Exception as exc:  # noqa: BLE001 - reported with the manual restore instructions
        return {"name": "restore", "label": label, "ok": False, "subject_restored": False,
                "error": f"{type(exc).__name__}: {exc}"}
    restored = _template_subject(final) == subject
    if restored:
        with contextlib.suppress(OSError):
            recovery.unlink(missing_ok=True)
    changes = _json_diff(start, final)
    return {"name": "restore", "label": label, "ok": restored and not changes, "subject_restored": restored,
            "differences": changes}


# ── --send-test (04b §10 step 3) ──
def _send_test_refusal(s: QboSettings, inv: InvoiceSnapshot) -> str | None:
    """04b §10 step 3: refuse unless QB_SANDBOX=true or the invoice e-mails exactly QB_TEST_SEND_TO (Cc/Bcc too)."""
    if s.sandbox:
        return None
    test_to = (s.test_send_to or "").strip()
    if not test_to:
        return ("Refusing: QB_TEST_SEND_TO is not set. The send test only e-mails the internal test customer: set "
                "QB_TEST_SEND_TO=<internal inbox> and use a test invoice whose customer e-mail is exactly that address "
                "(04b §10 step 1).")
    if {r.lower() for r in inv.recipients} != {test_to.lower()}:
        return (f"Refusing: invoice #{inv.doc_number} is addressed to {', '.join(inv.recipients) or 'nobody'}, not "
                f"exactly to QB_TEST_SEND_TO ({test_to}). Use the test invoice of the internal test customer "
                "(04b §10 step 1).")
    copies = [a for a in (*split_addresses(inv.bill_email_cc), *split_addresses(inv.bill_email_bcc))
              if a.lower() != test_to.lower()]
    if copies:
        return (f"Refusing: invoice #{inv.doc_number} also sends copies to {', '.join(copies)}; the test invoice must "
                f"e-mail only {test_to}.")
    return None


def _receipt_dict(receipt: qbo_email.SendReceipt) -> dict[str, Any]:
    data = dataclasses.asdict(receipt)
    if receipt.sent_at is not None:
        data["sent_at"] = receipt.sent_at.isoformat(timespec="seconds")
    return data


async def _qbo_send_test(ctx: _Probe, doc: str) -> int:
    """`eta probe qbo --send-test --invoice DOC`: one probe e-mail through the configured strategy (verification
    gate bypassed), then the human answer is recorded in qbo_email_probe.json (04b §10 step 3)."""
    s = _qbo_settings(ctx.settings)
    refusal = _write_refusal(ctx, "--send-test")
    if refusal is not None:
        return refusal
    probe_settings = dataclasses.replace(s, email_require_verified=False)   # this probe establishes the gate
    out: dict[str, Any] = {"service": "qbo", "probe": "send-test", "realm_id": s.realm_id,
                           "environment": "sandbox" if s.sandbox else "production", "strategy": s.email_strategy,
                           "invoice": doc, "subject": None, "receipt": None, "verified": None, "error": None}
    with run_lock(ctx.state_dir):
        async with _qbo_session(ctx, read_only=False, qbo_settings=probe_settings) as (qbo, _):
            inv = await qbo.get_invoice_by_doc_number(doc)
            out["invoice_id"] = inv.id
            _out(f"QuickBooks send test — {_company_label(s)}, strategy {s.email_strategy}")
            _out(f"  Invoice #{inv.doc_number} (Id {inv.id}), customer {inv.customer_name or '-'}, e-mail to "
                 f"{', '.join(inv.recipients) or '-'}")
            refusal_text = _send_test_refusal(s, inv)
            if refusal_text is not None:
                out["error"] = refusal_text
                _out(refusal_text)
                _saved(ctx.save("qbo", f"send-test_{doc}", out))
                return EXIT_ABORTED
            eta_day = inv.eta_value or ctx.now.date()
            subject = (f"ETA probe {ctx.now:%Y%m%d-%H%M%S} — Invoice for Booking no. {inv.doc_number}. "
                       f"***ESTIMATED ARRIVING ON {eta_day:%m/%d}.****")
            out["subject"] = subject
            _out(f"  Subject: {subject}")
            if s.email_redirect_to:
                _out(f"  QB_EMAIL_REDIRECT_TO is set: the e-mail goes to {s.email_redirect_to}")
            if not _ask_yes("Send this one probe e-mail now? [y/N] "):
                return _aborted()
            try:
                receipt = await qbo.send_invoice(inv, subject=subject)
            except (ConfigError, AuthError):
                raise                   # configuration / credentials: exit 2 (11 §3) via _guard
            except TemplateRestoreError as exc:
                out["error"] = str(exc)
                _out(f"  SEND FAILED and the template must be restored by hand: {exc}")
                _saved(ctx.save("qbo", f"send-test_{doc}", out))
                _result_line(False, text_failed="FAIL")
                return EXIT_PROBE_FAILED
            except EtaAutomationError as exc:
                out["error"] = str(exc)
                _out(f"  SEND FAILED: {exc}")
                _out("  -> 04b §8: if template_swap cannot work here, use QB_EMAIL_STRATEGY=graph (or smtp).")
                _saved(ctx.save("qbo", f"send-test_{doc}", out))
                _result_line(False, text_failed="FAIL")
                return EXIT_PROBE_FAILED
        out["receipt"] = _receipt_dict(receipt)
        _out(f"  Sent with {receipt.strategy}: to {', '.join(receipt.to) or '-'}, cc {', '.join(receipt.cc) or '-'}, "
             f"bcc {', '.join(receipt.bcc) or '-'}, sent at {out['receipt']['sent_at']}, QuickBooks delivery time "
             f"{receipt.qbo_delivery_time or '-'}, marked sent in QuickBooks {_yes_no(receipt.qbo_marked_sent)}")
        target = s.test_send_to or receipt.redirected_to or ", ".join(receipt.to)
        verified = _ask_yes(f"Check {target}. Did an e-mail arrive with EXACTLY this subject, the invoice PDF, and "
                            "(if online payments are on) the pay button/link? [y/N] ")
        out["verified"] = verified
        record = qbo_email.record_verification(ctx.state_dir, realm_id=s.realm_id, strategy=s.email_strategy,
                                               verified=verified, subject_sent=subject, invoice_id=inv.id)
    _out(f"  Verification recorded ({'verified' if verified else 'NOT verified'}) in {record}")
    if verified:
        _out(f"  Real runs may now send invoice e-mails with {s.email_strategy} for this company.")
    else:
        _out("  -> 04b §8: if the subject was not applied, use QB_EMAIL_STRATEGY=graph (or smtp); without a mailbox "
             "option, send these e-mails by hand and run `eta run --no-email`.")
    _saved(ctx.save("qbo", f"send-test_{doc}", out))
    _result_line(verified, text_ok="PASS", text_failed="FAIL")
    return EXIT_OK if verified else EXIT_PROBE_FAILED


# ── --restore-template (04b §10 step 5) ──
async def _qbo_restore_template(ctx: _Probe) -> int:
    """Run only the 04b §6.2 crash recovery: restore the subject a crashed run left in the recovery file."""
    s = _qbo_settings(ctx.settings)
    refusal = _write_refusal(ctx, "--restore-template")
    if refusal is not None:
        return refusal
    recovery = ctx.state_dir / qbo_email.RECOVERY_FILE_NAME
    with run_lock(ctx.state_dir):
        _out(f"QuickBooks e-mail template recovery — {_company_label(s)}")
        if not recovery.exists():
            _out(f"  Nothing to restore: there is no recovery file ({recovery}).")
            _result_line(True)
            return EXIT_OK
        try:
            record = json.loads(recovery.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            record = None
        if isinstance(record, Mapping):
            _out(f"  A previous run left the subject {record.get('swapped_subject')!r} (written "
                 f"{record.get('written_at')}); it will be set back to {record.get('original_subject')!r}.")
        else:
            _out(f"  The recovery file {recovery} cannot be read; the recovery will report what to do.")
        if not _ask_yes("Restore the QuickBooks invoice e-mail subject now? [y/N] "):
            return _aborted()
        async with _qbo_session(ctx, read_only=False) as (qbo, _):
            emailer = qbo_email.TemplateSwapEmailer(qbo, state_dir=ctx.state_dir, clock=SystemClock())
            try:
                restored = await emailer.recover()
            except EtaAutomationError as exc:
                _out(f"  RESTORE FAILED: {exc}")
                _result_line(False, text_failed="FAIL")
                return EXIT_PROBE_FAILED
    _out("  The invoice e-mail subject was restored and the recovery file deleted." if restored
         else "  Nothing to restore.")
    _result_line(True)
    return EXIT_OK


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta qbo-fields (04 §6.7, §10 step 3)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _cmd_qbo_fields(args: argparse.Namespace, settings: Settings, log_file: Path | None) -> int:
    ctx = _probe_context(args, settings)
    return asyncio.run(_guard("qbo-fields", lambda: _qbo_fields(ctx, args.invoice)))


async def _qbo_fields(ctx: _Probe, doc: str) -> int:
    """Table of the invoice's custom fields and the .env lines for the resolved ETA field (read-only)."""
    s = _qbo_settings(ctx.settings)
    async with _qbo_session(ctx, read_only=True) as (qbo, _):
        rows = await qbo.list_custom_fields(doc)
    resolved: Mapping[str, Any] | None = None
    fields = list(rows)
    if fields and "resolved_eta_field" in fields[-1]:
        last = fields.pop()
        value = last.get("resolved_eta_field")
        resolved = value if isinstance(value, Mapping) else None
    _out(f"Custom fields of invoice #{doc} ({_company_label(s)}):")
    header = ("DefinitionId", "Name", "Type", "Value key", "Value", "ETA field")
    table: list[tuple[str, ...]] = [header]
    for row in fields:
        match = f"yes ({row.get('match_rule')})" if row.get("is_eta_match") else ""
        table.append(tuple(str(x if x is not None else "") for x in (
            row.get("DefinitionId"), row.get("Name"), row.get("Type"), row.get("value_key"), row.get("value"), match)))
    if len(table) == 1:
        _out("  (this invoice has no custom fields)")
    else:
        widths = [max(len(r[i]) for r in table) for i in range(len(header))]
        for r in table:
            _out("  " + "  ".join(cell.ljust(width) for cell, width in zip(r, widths, strict=True)).rstrip())
    _saved(ctx.save("qbo", f"fields_{doc}", {"invoice": doc, "fields": fields, "resolved_eta_field": resolved}))
    if resolved is None:
        _out(f"The ETA field ('{s.eta_field_name}') was not found on this invoice. Enable the ETA custom field for "
             "invoices in QuickBooks (and fill it in on this invoice), then retry; if it still does not appear, see "
             "04 §8.1.")
        _result_line(False, text_failed="ETA field not found")
        return EXIT_PROBE_FAILED
    _out(f"ETA field: '{resolved.get('name')}' (DefinitionId {resolved.get('definition_id')}, Type "
         f"{resolved.get('type')}, value key {resolved.get('value_key')}, from {resolved.get('source')})")
    if resolved.get("source") == "config":
        _out("This comes only from your .env (QB_ETA_DEFINITION_ID / QB_ETA_FIELD_TYPE): the field was not seen on "
             "this invoice, so its shape is not confirmed. Retry with an invoice whose ETA is filled in (04 §10 step 3).")
        _result_line(False, text_failed="ETA field not confirmed")
        return EXIT_PROBE_FAILED
    _out("Suggested .env lines:")
    _out(f"  QB_ETA_DEFINITION_ID={resolved.get('definition_id')}")
    _out(f"  QB_ETA_FIELD_NAME={resolved.get('name')}")
    _out(f"  QB_ETA_FIELD_TYPE={resolved.get('type')}")
    _result_line(True)
    return EXIT_OK


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta qbo-auth (04 §5.4)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _qbo_auth_settings(settings: Settings, env_file: Path) -> QboSettings:
    """QboSettings for `eta qbo-auth`. Settings.qbo is None while QB_REALM_ID is unknown, so the same environment
    is read again with a placeholder realm id, which is then cleared: the flow prints and stores the connected
    company's id (04 §5.4 step 7)."""
    if settings.qbo is not None:
        return settings.qbo
    lacking = [name for name in settings.missing.get("qbo", ()) if name != "QB_REALM_ID"]
    if lacking:
        raise ConfigError(f"`eta qbo-auth` needs {', '.join(lacking)} in .env (Keys & credentials of your app on "
                          "developer.intuit.com, 04 §2)")
    source = dict(os.environ)
    source["QB_REALM_ID"] = "0"
    config_log = logging.getLogger("eta.config")
    disabled = config_log.disabled
    config_log.disabled = True       # the obsolete-variable notice was already logged once
    try:
        filled = load_settings(env_file, env=source).qbo
    finally:
        config_log.disabled = disabled
    if filled is None:
        raise ConfigError("QuickBooks app keys are missing: set QB_CLIENT_ID and QB_CLIENT_SECRET in .env")
    return dataclasses.replace(filled, realm_id="")


def _cmd_qbo_auth(args: argparse.Namespace, settings: Settings, log_file: Path | None) -> int:
    env_file = Path(args.env_file) if args.env_file is not None else Path.cwd() / ".env"
    try:
        qbo_settings = _qbo_auth_settings(settings, env_file)
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    if args.listen and not qbo_settings.sandbox:
        log.warning("--listen redirects to http://localhost:%d/callback, which Intuit accepts only for development "
                    "(sandbox) keys; production keys need the default paste-back flow.", args.port)
    token_store = _token_store(settings)

    async def flow() -> int:
        # 03 §14: qbo-auth writes tokens.json and .env, so it holds the run lock (RunLockedError → exit 2 in
        # _guard). A run started earlier would otherwise write its older refresh token over the new one.
        with run_lock(settings.run.state_dir):
            await run_authorization_code_flow(qbo_settings, token_store, listen=args.listen, port=args.port,
                                              input_fn=input, open_browser=not args.no_browser)
        return EXIT_OK

    return asyncio.run(_guard("qbo-auth", flow))


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta msc-token (06 §5.1, §10)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _cmd_msc_token(args: argparse.Namespace, settings: Settings, log_file: Path | None) -> int:
    """Hidden input of a token copied from the user's own signed-in myMSC session; stored, never echoed."""
    msc_settings = settings.msc if settings.msc is not None else MscSettings(mode="mymsc")
    token_store = _token_store(settings)
    if settings.msc is not None and settings.msc.mode == "api":
        _out("MSC lookups use the official API (MSC_MODE resolves to api); the bridge token is only used by "
             "`eta probe msc --compare`.")
    _out("Copy the access token of your signed-in myMSC browser session (06-carrier-msc §2.2) and paste it below. "
         "The input is hidden.")
    try:
        status = msc.store_manual_token(msc_settings, token_store, _read_secret("MSC access token: "))
    except ConfigError as exc:
        log.error("%s", exc)
        return EXIT_CONFIG
    _out(f"MSC access token stored in {token_store.path}.")
    _print_bridge_status(status, title="Status")
    if not _bridge_usable(status):
        _out("The pasted token has already expired — copy a fresh one from your signed-in myMSC session.")
        return EXIT_CONFIG
    if msc.token_needs_refresh(status):
        _out("Less than 10 minutes left: `eta run` will ask for a fresh token.")
    return EXIT_OK


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta version (11 §2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _cmd_version() -> int:
    print(f"eta-automations {__version__}")
    print(f"Python {platform.python_version()}")
    for dist in DISTRIBUTIONS:
        try:
            version = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            version = "not installed"
        print(f"{dist} {version}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
