"""Plan → confirm → apply orchestration of `eta run` (10-pipeline §3–§8, 01-business-rules §6, §13–§14).

    Phase 0  preflight   run_now, directories, run lock (03 §14), require(qbo, vship), services, auth checks
    Phase 1  read        read_input → classify → D2 header → D3 duplicates → --only → --limit; MSC token pre-flight
    Phase 2  plan        router.prepare(); one task per row (lookups only); one batched QBO query; finalize
    Phase 3  review      --no-email transformation, e-mail path preflight, one notice per booking (XCT-01), plan
                         report, summary, confirmation
    Phase 4  apply       routed/failed entries in input order, then every plan's actions (concurrent rows,
                         sequential actions, one template_swap e-mail at a time), D11 revert, mid-run e-mail-path
                         failure, Ctrl+C handling, then the entries held on another row's notice (NEW-02)
    Phase 5  report      Done line, per-route table, timings, final report; exit code 0 or 4

Every text that reaches an output file comes from texts.py / rules.py (01 §14, byte for byte). Deliberate
deviations implemented here, each marked where it happens: D2, D3, D8, D9, D11, D12, D14, D20 (dry run,
confirmation, ledger), D21. Everything time-related uses the single `run_now` (03 §3, D19).
"""

from __future__ import annotations

import asyncio
import contextlib
import csv
import getpass
import importlib.metadata
import logging
import platform
import signal
import sys
import time
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Iterable, Mapping
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, TextIO

from . import __version__, texts
from .business_days import window_cutoff
from .carriers.msc import NO_TOKEN_MESSAGE, token_needs_refresh
from .carriers.router import build_router, identify_carrier
from .clock import Clock, FixedClock, SystemClock
from .config import Settings, require, settings_summary
from .errors import (
    AuthError,
    ConfigError,
    EmailPathUnavailableError,
    NoteError,
    RunLockedError,
    ServiceError,
    TemplateRestoreError,
)
from .ledger import EMAILED, LEDGER_FILE_NAME, Ledger
from .models import (
    Action,
    Carrier,
    InputRow,
    OutputFile,
    ParsedRow,
    Plan,
    PlanStatus,
    QboSend,
    QboUpdate,
    Route,
    RowKind,
    VShipComment,
    VShipPatchEta,
)
from .outputs import OutputWriter, write_report
from .prompt import read_line
from .qbo.client import ambiguous_invoice_error, build_qbo, doc_number_problem, split_addresses
from .qbo.email import RECOVERY_FILE_NAME, redirect_refusal
from .rules import (
    arrival_doc_number,
    carrier_failure,
    classify,
    drop_header_row,
    duplicate_entry,
    find_duplicates,
    initial_route,
    is_below_threshold,
    malformed_entry,
    parse_rows,
)
from .run_lock import run_lock
from .token_store import TokenStore
from .vship.client import build_vship
from .vship.logic import NotifStatus, has_tr_swb_note, notif_status

if TYPE_CHECKING:
    from .carriers.router import CarrierRouter
    from .qbo.client import InvoiceSnapshot, QboClient
    from .vship.client import VShipClient

__all__ = [
    "EXIT_ABORTED",
    "EXIT_CONFIG",
    "EXIT_EMAIL_BLOCKED",
    "EXIT_OK",
    "ROUTE_ORDER",
    "RunSummary",
    "Services",
    "build_services",
    "read_input",
    "run",
    "validate_input_path",
]

log = logging.getLogger("eta.pipeline")

# 11 §3 exit codes produced by run() (1 = unexpected crash is the CLI's).
EXIT_OK = 0
EXIT_CONFIG = 2            # configuration / authentication / run-lock / MSC-token preflight
EXIT_ABORTED = 3           # confirmation declined, or no TTY without --yes
EXIT_EMAIL_BLOCKED = 4     # the invoice e-mail path was unavailable or failed (04b)

# Order of the console tables (10 §8).
ROUTE_ORDER: tuple[Route, ...] = (
    Route.NOTIFY, Route.PAID_UPDATE, Route.FUTURE_UPDATE, Route.BEN, Route.BINDU, Route.ACCOUNTING,
    Route.SKIPPED, Route.CUSTOMS_HOLD, Route.FAILED,
)
_STATUS_ORDER: tuple[str, ...] = ("OK", "FAILED", "ROUTED", "SKIPPED", "INTERRUPTED", "PENDING")
_ROUTED = frozenset({Route.BINDU, Route.BEN, Route.ACCOUNTING})
_CARRIER_KINDS = frozenset({RowKind.STANDARD, RowKind.NOTIF1_SENT})
_YES = frozenset({"y", "yes"})
_BANNER = "!" * 78
_MAX_DOCS_SHOWN = 5
_CREDENTIAL_STATUSES = frozenset({400, 401, 403})   # a login answered like this points at the credentials


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Services (10 §3)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class Services:
    settings: Settings
    clock: Clock                  # FixedClock(run_now) — every client gets the same instant
    token_store: TokenStore
    qbo: QboClient
    vship: VShipClient
    router: CarrierRouter         # carriers/router.py: route(base) -> (Carrier, token, CarrierClient)
    outputs: OutputWriter
    ledger: Ledger


async def _close_quietly(name: str, closer: Callable[[], Awaitable[None]]) -> None:
    """Cleanup never raises: a failing close is logged and the other clients are still closed."""
    try:
        await closer()
    except Exception as exc:  # noqa: BLE001 - cleanup must go on
        log.warning("Closing the %s failed: %s", name, exc)


@asynccontextmanager
async def build_services(settings: Settings, run_now: datetime, *, read_only: bool) -> AsyncIterator[Services]:
    """TokenStore(<ETA_STATE_DIR>/tokens.json), build_qbo(read_only) (04 §6.8), build_vship(read_only) (05 §6.2),
    build_router(read_only) with every carrier client (09 §D.2, clock=FixedClock(run_now); a read-only aggregator
    never registers bookings nor writes its state file), OutputWriter(enabled=not
    read_only) and Ledger (never written when read_only). On exit every client is closed (in a finally) —
    QboClient.aclose() also closes the e-mail strategy, which restores the QBO template if a recovery record
    exists (04b §6.2)."""
    clock = FixedClock(run_now)
    state_dir = settings.run.state_dir
    token_store = TokenStore(state_dir / "tokens.json")
    async with AsyncExitStack() as stack:
        qbo = build_qbo(settings, token_store, read_only=read_only)
        stack.push_async_callback(_close_quietly, "QuickBooks client", qbo.aclose)
        vship = build_vship(settings, token_store, read_only=read_only)
        stack.push_async_callback(_close_quietly, "VShip client", vship.aclose)
        router = build_router(settings, token_store, clock=clock, read_only=read_only)   # CRA-01: no credits
        stack.push_async_callback(_close_quietly, "carrier clients", router.aclose)
        yield Services(
            settings=settings,
            clock=clock,
            token_store=token_store,
            qbo=qbo,
            vship=vship,
            router=router,
            outputs=OutputWriter(settings.run.output_dir, enabled=not read_only),
            ledger=Ledger(state_dir / LEDGER_FILE_NAME, enabled=not read_only),
        )


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Input (01 §2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def validate_input_path(path: Path) -> None:
    """01 §2 (`tinkyWinky.is_valid_txt_file`): the input must exist, be a file and end in .csv (any case).
    Raises ConfigError with the reason."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Input file not found: {p}")
    if not p.is_file():
        raise ConfigError(f"Input path is not a file: {p}")
    if p.suffix.lower() != ".csv":
        raise ConfigError(f"Input file must be a .csv file: {p}")


def read_input(path: Path) -> list[InputRow]:
    """01 §2: open(path, "r", newline="", encoding="utf-8-sig") + csv.reader; blank rows skipped; line_no
    counts the remaining rows from 1 (rules.parse_rows). ConfigError for an invalid path or unreadable text."""
    validate_input_path(path)
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            return parse_rows(csv.reader(fh))
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Input file {path} is not UTF-8 text ({exc.reason} at byte {exc.start}); "
                          "save it as 'CSV UTF-8'") from exc
    except csv.Error as exc:
        raise ConfigError(f"Input file {path} could not be read as CSV: {exc}") from exc


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Run summary (10 §4)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class RunSummary:
    """What run() returns: counts per route and status, phase timings, report/log paths, email_blocked and
    the exit code (11 §3)."""

    run_id: str
    exit_code: int = EXIT_OK
    dry_run: bool = False
    message: str = ""                       # why the run stopped early (exit 2/3), else ""
    counts_by_route: dict[str, int] = field(default_factory=dict)
    counts_by_status: dict[str, int] = field(default_factory=dict)
    ok: int = 0                             # 01 §13
    failed: int = 0                         # rows written to Failed_To_Process.txt / PotentialCustomsHold.txt
    routed: int = 0
    skipped: int = 0
    interrupted: int = 0
    phase_timings: dict[str, float] = field(default_factory=dict)
    report_csv: Path | None = None
    report_json: Path | None = None
    log_file: Path | None = None
    to_email_csv: Path | None = None
    email_blocked: bool = False
    plans: list[Plan] = field(default_factory=list)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Internal per-row state
# ════════════════════════════════════════════════════════════════════════════════════════════════
@dataclass
class _ManualEmail:
    """--no-email: the e-mail a person sends by hand instead (10 Phase 3 step 1)."""

    subject: str
    doc_number_after: str
    note: str


@dataclass
class _Row:
    plan: Plan
    index: int                                   # 1-based position among the rows of this run
    duplicate_of: ParsedRow | None = None        # D3: first occurrence of this DocNumber
    comments: list[dict[str, Any]] | None = None  # VShip internal comments (notif + TR/SWB, 01 §11–§12)
    comments_error: str | None = None
    needs_qbo: bool = False                      # 2a → 2b/2c
    manual: _ManualEmail | None = None
    reason: str = ""                             # short failure reason for the grouped console lines
    started: bool = False                        # apply began ("not started yet", Phase 4)
    apply_failed: bool = False
    lines: list[str] = field(default_factory=list)
    after: _Row | None = None                    # NEW-02: entry held until this row (same booking) gave the notice
    notice_out: bool = False                     # NEW-02: this row's notice went out (e-mail sent / listed by hand)


@dataclass
class _ApplyState:
    current: InvoiceSnapshot | None
    original_doc: str
    doc_changed: bool = False


class _VShipIdUnavailable(NoteError):
    """The VShip booking id could not be resolved in the plan phase (01 §6.6: a Note Write Error)."""


def _status_for(route: Route) -> PlanStatus:
    if route in _ROUTED:
        return "ROUTED"
    if route is Route.SKIPPED:
        return "SKIPPED"
    return "FAILED"


def _first_line(text: str) -> str:
    return text.splitlines()[0] if text else ""


def _dots(name: str) -> str:
    """10 §8 table label: the route name, a space and dots up to 16 characters."""
    return f"{name} {'.' * max(2, 15 - len(name))}"


def _fmt_eta(value: datetime) -> str:
    return value.strftime("%Y-%m-%d") if value.time() == datetime.min.time() else value.strftime("%Y-%m-%d %H:%M")


def _describe(action: Action) -> str:
    if isinstance(action, QboUpdate):
        doc = f", DocNumber -> {action.new_doc_number}" if action.new_doc_number else ""
        return f"QBO update ETA={action.eta.isoformat()}{doc}"
    if isinstance(action, QboSend):
        return "QBO e-mail"
    if isinstance(action, VShipComment):
        return f"VShip note {action.text!r}"
    return f"VShip ETA {action.eta:%Y-%m-%dT%H:%M:%S}"


def _receipt_detail(receipt: Any) -> dict[str, Any]:
    """Ledger detail of a QboSend: strategy, recipients, times — never the body (10 Phase 4)."""
    sent_at = getattr(receipt, "sent_at", None)
    return {
        "strategy": getattr(receipt, "strategy", None),
        "subject": getattr(receipt, "subject", None),
        "to": list(getattr(receipt, "to", ()) or ()),
        "cc": list(getattr(receipt, "cc", ()) or ()),
        "bcc": list(getattr(receipt, "bcc", ()) or ()),
        "sent_at": sent_at.isoformat(timespec="seconds") if isinstance(sent_at, datetime) else None,
        "qbo_delivery_time": getattr(receipt, "qbo_delivery_time", None),
        "qbo_marked_sent": getattr(receipt, "qbo_marked_sent", None),
        "redirected_to": getattr(receipt, "redirected_to", None),
        "outcome_inferred": getattr(receipt, "outcome_inferred", None),
    }


def _service_hint(name: str, exc: ServiceError, probe: str) -> str:
    """Phase 0 step 6 hint for a login that failed without a credential verdict (03 §6: network error or an
    unexpected HTTP status): re-authorising would not help, so point at the connection instead."""
    if exc.status is None:
        return (f"{name} could not be reached: check the network connection and the proxy settings "
                f"(HTTPS_PROXY), then retry (`{probe}` shows details).")
    return f"{name} answered HTTP {exc.status} to the login: retry later (`{probe}` shows details)."


def _versions() -> dict[str, str]:
    out = {"eta_automations": __version__, "python": platform.python_version()}
    for dist in ("httpx", "pandas", "python-dotenv"):
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = "not installed"
    return out


def _default_is_tty() -> bool:
    stream = sys.stdin
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


# ════════════════════════════════════════════════════════════════════════════════════════════════
# One run
# ════════════════════════════════════════════════════════════════════════════════════════════════
class _Run:
    def __init__(self, settings: Settings, input_csv: Path, *, dry_run: bool, assume_yes: bool, force: bool,
                 no_email: bool, only: Collection[str] | None, limit: int | None, clock: Clock,
                 input_fn: Callable[[str], str], is_tty: Callable[[], bool], getpass_fn: Callable[[str], str],
                 out: TextIO | None, log_file: Path | None) -> None:
        self.settings = settings
        self.input_csv = Path(input_csv)
        self.dry_run = dry_run
        self.assume_yes = assume_yes
        self.force = force
        self.no_email = no_email
        self.only = only
        self.limit = limit
        self.input_fn = input_fn
        self.is_tty = is_tty
        self.getpass_fn = getpass_fn
        self.out = out
        self.run_now = clock.now()                                   # Phase 0 step 1 (03 §3: once per run)
        self.run_id = self.run_now.strftime("%Y%m%d_%H%M%S")
        self.today = self.run_now.date()
        self.cutoff: datetime | None = None
        self.rows: list[_Row] = []
        self.timings: dict[str, float] = {}
        self.email_blocked = False
        self.email_error: BaseException | None = None
        self.restore_error: BaseException | None = None   # the QBO e-mail template must be restored by hand
        self.output_errors = 0
        self.report_stage = "plan"
        self.summary = RunSummary(run_id=self.run_id, dry_run=dry_run, log_file=log_file)
        self._phase_started = time.perf_counter()
        self._services: Services | None = None
        self._send_turn: asyncio.Lock | None = None        # NEW-01: one template_swap e-mail at a time (Phase 4)
        self._sigint_guard: _SigintGuard | None = None     # NEW-01: Ctrl+C only repeats the stop message
        self._console_best_effort = False                  # R2-01: set once Phase 4 begins, and on Ctrl+C (_print)
        self._console_failed = False

    # ── services shortcuts ──
    @property
    def services(self) -> Services:
        assert self._services is not None
        return self._services

    @property
    def qbo(self) -> Any:
        return self.services.qbo

    @property
    def vship(self) -> Any:
        return self.services.vship

    @property
    def router(self) -> Any:
        return self.services.router

    # ── console ──
    def _print(self, text: str = "") -> None:
        """One console line, also logged at DEBUG (so the log file has it). Review fix R2-01: once Phase 4 has begun
        changing things, and on every Ctrl+C path, the console is best effort. A stream that fails — stdout of
        `eta run … | tee run.log` whose reader ended with the same Ctrl+C, a redirect to a full disk, a closed stream —
        never stops the apply, the Ctrl+C drain or the final report; its first failure is logged as a WARNING. Before
        Phase 4 a failing console still stops the run, while nothing has been changed."""
        log.debug("%s", text)
        try:
            print(text, file=self.out if self.out is not None else sys.stdout, flush=True)
        except (OSError, ValueError) as exc:
            if not self._console_best_effort:
                raise
            if not self._console_failed:
                self._console_failed = True
                log.warning("Console output failed (%s: %s); the run goes on without it - see the run report and the "
                            "log file.", type(exc).__name__, exc)

    def _phase_done(self, name: str) -> None:
        now = time.perf_counter()
        self.timings[name] = round(now - self._phase_started, 3)
        self._phase_started = now

    def _stop(self, code: int, message: str) -> RunSummary:
        """Early end of the run (exit 2 / 3)."""
        if code == EXIT_CONFIG:
            log.error("%s", message)
        else:
            self._print(message)
        self.summary.exit_code = code
        self.summary.message = message
        self.summary.phase_timings = dict(self.timings)
        self.summary.plans = [r.plan for r in self.rows]
        return self.summary

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # Phase 0 — preflight
    # ════════════════════════════════════════════════════════════════════════════════════════════
    async def execute(self, injected: Services | None) -> RunSummary:
        try:
            return await self._run_phases(injected)
        finally:
            if self._sigint_guard is not None:
                # NEW-01 / R2-02: from the start of Phase 4 until the report was rewritten, the clients closed and the
                # lock released, only the first Ctrl+C stopped the run; now Ctrl+C is back to normal.
                self._sigint_guard.restore()

    async def _run_phases(self, injected: Services | None) -> RunSummary:
        run = self.settings.run
        for directory in (run.output_dir, run.output_dir / "reports", run.output_dir / "logs"):  # step 2
            directory.mkdir(parents=True, exist_ok=True)
        run.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            validate_input_path(self.input_csv)   # fail fast, before any lock or network call
        except ConfigError as exc:
            return self._stop(EXIT_CONFIG, str(exc))
        with ExitStack() as locks:
            if not self.dry_run:                                                            # step 3
                try:
                    locks.enter_context(run_lock(run.state_dir))
                except RunLockedError as exc:
                    return self._stop(EXIT_CONFIG, str(exc))
            try:                                                                            # step 4
                require(self.settings, "qbo", "vship")
            except ConfigError as exc:
                return self._stop(EXIT_CONFIG, str(exc))
            if injected is not None:
                return await self._with_services(injected)
            async with AsyncExitStack() as stack:                                           # step 5
                try:
                    services = await stack.enter_async_context(
                        build_services(self.settings, self.run_now, read_only=self.dry_run))
                except ConfigError as exc:
                    return self._stop(EXIT_CONFIG, str(exc))
                return await self._with_services(services)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _authenticate(self) -> str | None:
        """Step 6: QBO and VShip tokens now (carriers authenticate lazily); a failure names the fix — the
        credentials/command for a rejected login, the network for a service that could not be reached."""
        checks: tuple[tuple[str, Any, str, str], ...] = (
            ("QuickBooks", self.qbo.auth,
             "Run `eta qbo-auth` to connect QuickBooks again, or check QB_CLIENT_ID / QB_CLIENT_SECRET / "
             "QB_REALM_ID / QB_REFRESH_TOKEN in .env (`eta probe qbo` shows details).", "eta probe qbo"),
            ("VShip", self.vship.auth,
             "Check VSHIP_USER_NAME / VSHIP_PASSWORD in .env (`eta probe vship` shows details).", "eta probe vship"),
        )
        for name, auth, hint, probe in checks:
            try:
                await auth.get_token()
            except (AuthError, ConfigError) as exc:
                return f"{name} authentication failed: {exc}\n{hint}"
            except ServiceError as exc:
                advice = hint if exc.status in _CREDENTIAL_STATUSES else _service_hint(name, exc, probe)
                return f"{name} authentication failed: {exc}\n{advice}"
        return None

    async def _with_services(self, services: Services) -> RunSummary:
        self._services = services
        problem = await self._authenticate()
        if problem is not None:
            return self._stop(EXIT_CONFIG, problem)
        self._phase_done("preflight")

        # ── Phase 1 — read & classify ──
        try:
            input_rows = read_input(self.input_csv)
        except ConfigError as exc:
            return self._stop(EXIT_CONFIG, str(exc))
        self._select([classify(r) for r in input_rows])
        if not self.rows:
            self._phase_done("read")
            self._print("Invoice list is empty - nothing to do.")
            self.summary.phase_timings = dict(self.timings)
            return self.summary
        problem = self._msc_token_preflight()
        if problem is not None:
            return self._stop(EXIT_CONFIG, problem)
        self._phase_done("read")

        # ── Phase 2 — plan / Phase 3 — review & confirm (nothing is written before Phase 4) ──
        try:
            await self._plan_all()
            self._phase_done("plan")
            code = await self._review()
        except asyncio.CancelledError:
            self._console_best_effort = True        # R2-01: a Ctrl+C ends as one (exit code 3) without a console too
            self._print("\nInterrupted before anything was changed.")
            raise
        self._phase_done("review")
        if code is not None:
            if code == EXIT_ABORTED:
                self.report_stage = "aborted"
                try:
                    self._write_report(final_code=code)
                except OSError as exc:   # e.g. the report is open in Excel: nothing was changed, still exit 3
                    log.error("Could not rewrite the run report: %s", exc)
            self.summary.exit_code = code
            return self._summarize(code)

        # ── Phase 4 — apply ──
        try:
            await self._apply()
        except asyncio.CancelledError:
            self._phase_done("apply")
            self.report_stage = "interrupted"
            self._print("\nInterrupted: plans that had not finished are marked INTERRUPTED in the report.")
            self._finish()
            raise
        self._phase_done("apply")

        # ── Phase 5 — report ──
        return self._finish()

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # Phase 1 — rows of this run
    # ════════════════════════════════════════════════════════════════════════════════════════════
    def _select(self, parsed: list[ParsedRow]) -> None:
        parsed, _header = drop_header_row(parsed)      # D2: a header-looking first row is skipped (logged INFO)
        duplicates = find_duplicates(parsed)           # D3: only the first occurrence of a DocNumber is processed
        log.info("Loaded %d booking(s) from file.", len(parsed))
        selected = parsed
        if self.only is not None:                      # 11 §2: exact, case-insensitive
            wanted = {s.strip().casefold() for s in self.only if s and s.strip()}
            selected = [p for p in selected
                        if p.row.doc_number.casefold() in wanted or (p.base_booking or "").casefold() in wanted]
        if self.limit is not None:
            selected = selected[: max(0, self.limit)]
        if len(selected) != len(parsed):
            log.info("Processing %d of %d row(s) (--only / --limit).", len(selected), len(parsed))
        self.rows = [
            _Row(plan=Plan(parsed=p, route=Route.FAILED), index=i, duplicate_of=duplicates.get(p.row.line_no))
            for i, p in enumerate(selected, start=1)
        ]

    def _needs_carrier(self, row: _Row) -> bool:
        """STANDARD / NOTIF1_SENT rows at or above the threshold (the rows that get a carrier lookup)."""
        p = row.plan.parsed
        return (row.duplicate_of is None and p.kind in _CARRIER_KINDS and p.base_booking is not None
                and not is_below_threshold(p, self.settings.run.min_invoice_amount))

    def _msc_token_preflight(self) -> str | None:
        """06 §5.1 / 10 Phase 1: with MSC in mymsc bridge mode and at least one MSC row, a stored token needs
        at least 10 minutes left; prompt for one on a TTY (getpass), else fail fast (exit 2)."""
        router = self.router
        try:
            if router.provider_for(Carrier.MSC) != "native":
                return None
            client = router.native_client(Carrier.MSC)
        except (AttributeError, KeyError, ValueError):
            return None
        token_status = getattr(client, "token_status", None)
        if client is None or not callable(token_status):
            return None
        if not any(self._routes_to(Carrier.MSC, row) for row in self.rows):
            return None
        status = token_status()
        if not token_needs_refresh(status):
            return None
        if not self.is_tty():
            return NO_TOKEN_MESSAGE
        left = status.get("seconds_left")
        state = ("no MSC access token is stored" if not status.get("has_token")
                 else f"the stored MSC access token has {left} s left (less than 10 minutes)")
        self._print(f"MSC lookups use the myMSC token bridge (06-carrier-msc §2.2) and {state}.")
        token = self.getpass_fn("Paste a fresh MSC access token (input hidden): ")
        try:
            status = client.set_manual_token(token)
        except ConfigError as exc:
            return str(exc)
        if token_needs_refresh(status):
            return f"The MSC access token is still not usable for this run. {NO_TOKEN_MESSAGE}"
        return None

    def _routes_to(self, carrier: Carrier, row: _Row) -> bool:
        if not self._needs_carrier(row):
            return False
        try:
            return identify_carrier(row.plan.base_booking or "")[0] is carrier
        except Exception:  # noqa: BLE001 - unroutable rows are reported by their own task
            return False

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # Phase 2 — plan (read-only, concurrent)
    # ════════════════════════════════════════════════════════════════════════════════════════════
    async def _plan_all(self) -> None:
        run = self.settings.run
        self.cutoff = window_cutoff(self.run_now, run.window_business_days)
        bases = list(dict.fromkeys(r.plan.base_booking for r in self.rows
                                   if self._needs_carrier(r) and r.plan.base_booking))
        if bases:
            try:
                await self.router.prepare(bases)      # 09 §D.2: once, before the per-row tasks
            except Exception as exc:  # noqa: BLE001 - a failed warm-up must not stop the run
                log.warning("Carrier warm-up failed: %s", exc)
        sem = asyncio.Semaphore(max(1, run.max_parallel_bookings))
        async with asyncio.TaskGroup() as tg:
            for row in self.rows:
                tg.create_task(self._plan_row_task(row, sem))
        await self._lookup_invoices()                                                       # 2b
        for row in self.rows:                                                               # 2c
            if row.needs_qbo:
                row.needs_qbo = False
                try:
                    self._finalize(row)
                except Exception as exc:  # never e-mail from a half-made decision
                    log.exception("Unexpected error while finalizing #%s", row.plan.doc_number)
                    self._fail(row, texts.qb_processing_error_entry(row.plan.doc_number, exc),
                               reason="QB Invoice Processing Error", error=str(exc))

    def _one_notice_per_booking(self) -> None:
        """Review fixes XCT-01 / NEW-02 (Phase 3, after the e-mail preflight). Rows of one booking ('X' and 'X A', a
        normal input pair, 10 §6) are decided from one read of its VShip notes (D18), but the original handled rows
        one at a time and read the notes again for each, so a later row saw the note an earlier row had just posted.
        Replay that in input order: each NOTIFY plan still giving a notice (e-mail, or --no-email manual e-mail) adds
        its planned note to the booking's notes, and a later NOTIFY plan of the same booking is decided again with
        them (01 §6 step 3, §11): NOTIF #2 → Ben, a NOTIF #1 of today → 'Done in the last week'. Such a row is held
        on the row giving the notice (`after`): its entry is written only once that notice went out (_settle_held).
        Plans the preflight failed give no notice and are skipped. 'ARRIVING ON' notes are no notices, so those rows
        are unchanged (O4)."""
        givers: dict[str, tuple[_Row, list[dict[str, Any]]]] = {}
        for row in self.rows:
            plan = row.plan
            if plan.route is not Route.NOTIFY or not plan.actions:
                continue
            key = plan.vship_booking_id or plan.base_booking or plan.doc_number
            known = givers.get(key)
            if known is None:
                giver, notes = givers[key] = (row, [])
            else:
                giver, notes = known
                notif = notif_status([*(row.comments or []), *notes], self.run_now)
                if notif.count == 2 or not notif.have_to_do:
                    log.info("#%s: same booking as #%s, which gives the notice in this run", plan.doc_number,
                             giver.plan.doc_number)
                    plan.notif = notif
                    row.manual = None
                    if notif.count == 2:
                        self._route(row, Route.BEN, OutputFile.BEN, texts.ben_entry(plan.doc_number))
                        outcome = "goes to Ben"
                    else:
                        self._fail(row, texts.done_in_last_week_entry(plan.doc_number), reason="Done in the last week")
                        outcome = "is skipped as done in the last week"
                    row.after = giver
                    plan.warnings.append(f"same booking as #{giver.plan.doc_number}, which gives this booking's notice "
                                         f"in this run: {outcome} once that e-mail is sent, else it is not processed")
                    continue
            note = row.manual.note if row.manual is not None else next(
                (a.text for a in plan.actions if isinstance(a, VShipComment)), None)
            if note is not None:
                notes.append({"comment": note, "createdDate": self.run_now.isoformat()})

    async def _plan_row_task(self, row: _Row, sem: asyncio.Semaphore) -> None:
        """A plan task never raises (10 Phase 2): every exception becomes a FAILED plan with a 01 §14 entry."""
        async with sem:
            try:
                await self._plan_row(row)
            except Exception as exc:
                log.exception("Unexpected error while planning #%s", row.plan.doc_number)
                doc = row.plan.doc_number
                if row.plan.eta is None:
                    _route, file, entry = carrier_failure(doc, exc)
                    self._fail(row, entry, reason="Unexpected error during carrier lookup", error=str(exc),
                               file=file)
                else:
                    self._fail(row, texts.qb_processing_error_entry(doc, exc),
                               reason="QB Invoice Processing Error", error=str(exc))
            finally:
                if row.lines:
                    log.info("%s", "\n".join(row.lines))

    async def _plan_row(self, row: _Row) -> None:
        """2a — per-row lookups (01 §6)."""
        plan = row.plan
        p = plan.parsed
        doc = p.row.doc_number
        row.lines.append(f"[{row.index}/{len(self.rows)}] Processing: {doc}")
        if row.duplicate_of is not None:
            # D3: the original processed (and possibly e-mailed) the same invoice twice in one run.
            self._fail(row, duplicate_entry(p), reason="duplicate row in input file")
            return
        first = initial_route(p, self.settings.run.min_invoice_amount)
        if first is Route.FAILED:
            if p.kind is RowKind.MALFORMED:
                self._fail(row, malformed_entry(p), reason="malformed row", error=p.row.parse_error)
            else:
                self._fail(row, texts.manual_entry(doc), reason="no suffix rule applies - process manually")
            return
        if first is Route.SKIPPED:
            plan.route = Route.SKIPPED
            plan.message = texts.SKIP_R_LOG
            row.lines.append(f"  {texts.SKIP_R_LOG}")
            return
        if first is Route.BINDU:
            name = await self._customer_name(row)
            self._route(row, Route.BINDU, OutputFile.BINDU, texts.bindu_row(doc, p.row.amount_raw, name))
            return
        if first is Route.BEN:
            self._route(row, Route.BEN, OutputFile.BEN, texts.ben_entry(doc))
            return

        # STANDARD / NOTIF1_SENT at or above the threshold: carrier lookup (01 §6 steps 1–2, §6.1)
        base = p.base_booking or ""
        try:
            carrier, token, client = self.router.route(base)
            plan.carrier = carrier
            row.lines.append(f"  [Carrier] Identified as {carrier} — querying tracking API...")
            eta = await client.get_eta(token)
            in_window = eta.eta <= self.cutoff
        except Exception as exc:  # noqa: BLE001 - 01 §6.1 maps every exception
            route, file, entry = carrier_failure(doc, exc)
            self._fail(row, entry, reason=type(exc).__name__, error=str(exc), file=file, route=route)
            return
        plan.eta = eta
        plan.cutoff = self.cutoff
        plan.in_window = in_window
        row.lines.append(f"  ETA: {eta.eta:%Y-%m-%d} | In {self.settings.run.window_business_days}-day window: "
                         f"{in_window}")
        self._eta_warnings(row)

        # VShip booking id (cached per run, D18); a failure is kept for the apply phase (01 §6.6)
        try:
            plan.vship_booking_id = str(await self.vship.find_booking_id(base))
        except Exception as exc:  # noqa: BLE001
            plan.vship_error = str(exc)

        if in_window:                                                                       # 01 §6 step 3
            notif, error = await self._notif(row)
            if notif is None:
                if self.settings.run.on_notif_lookup_error == "fail":
                    self._fail(row, texts.notif_lookup_failed_entry(doc, error),
                               reason="VShip notification lookup failed", error=error)
                    return
                warning = texts.notif_lookup_defaulted_warning(error)              # 01 §6.2 'send' (parity)
                log.warning("#%s: %s", doc, warning)
                plan.warnings.append(warning)
                notif = NotifStatus(0, True)
                plan.notif_defaulted = True
            plan.notif = notif
            if notif.count == 2:
                self._route(row, Route.BEN, OutputFile.BEN, texts.ben_entry(doc))
                return
            if not notif.have_to_do:
                # D8: the original logged these NOTIF1_SENT rows as "not found in QuickBooks".
                self._fail(row, texts.done_in_last_week_entry(doc), reason="Done in the last week")
                return
        row.needs_qbo = True
        row.lines.append("  -> QuickBooks lookup")

    def _eta_warnings(self, row: _Row) -> None:
        plan = row.plan
        eta = plan.eta
        if eta is None:
            return
        details = eta.details or {}
        if details.get("rescue"):
            plan.warnings.append(f"ETA from the {eta.carrier} rescue-only fallback ({eta.source}) - verify it")
        others = details.get("other_container_etas")
        if others:
            plan.warnings.append(f"{eta.carrier} other containers disagree: {others}")

    async def _customer_name(self, row: _Row) -> str:
        """01 §7.2 customer name for Send2Bindu.csv; D14: a failed lookup gives "" and a WARNING."""
        plan = row.plan
        try:
            name = await self.vship.get_customer_name(plan.parsed.base_booking or "")
        except Exception as exc:  # noqa: BLE001
            # D14: the original crashed the whole run when the customer name could not be read.
            warning = f"customer name lookup failed ({exc}) - Send2Bindu.csv gets an empty name"
            log.warning("#%s: %s", plan.doc_number, warning)
            plan.warnings.append(warning)
            return ""
        return name or ""

    async def _notif(self, row: _Row) -> tuple[NotifStatus | None, str]:
        """01 §11 notif status from the VShip internal comments (also kept for the TR/SWB check)."""
        plan = row.plan
        if plan.vship_booking_id is None:
            return None, plan.vship_error or "VShip booking id unknown"
        try:
            comments = await self.vship.get_internal_comments(plan.vship_booking_id)
        except Exception as exc:  # noqa: BLE001
            row.comments_error = str(exc)
            return None, str(exc)
        row.comments = list(comments or [])
        try:
            return notif_status(row.comments, self.run_now), ""
        except Exception as exc:  # noqa: BLE001
            return None, str(exc)

    async def _lookup_invoices(self) -> None:
        """2b — one batched QBO lookup; a failure fails every pending row; D21 ambiguous DocNumbers."""
        pending = [r for r in self.rows if r.needs_qbo]
        if not pending:
            return
        docs = sorted({r.plan.doc_number for r in pending})
        try:
            invoices = await self.qbo.find_invoices_by_doc_numbers(docs)
        except Exception as exc:  # noqa: BLE001 - network/auth: every pending row fails
            log.error("QuickBooks invoice lookup failed: %s", exc)
            for r in pending:
                r.needs_qbo = False
                self._fail(r, texts.qb_processing_error_entry(r.plan.doc_number, exc),
                           reason="QuickBooks invoice lookup failed", error=str(exc))
            return
        ambiguous: Mapping[str, Any] = getattr(self.qbo, "ambiguous", None) or {}
        for r in pending:
            doc = r.plan.doc_number
            ids = ambiguous.get(doc)
            if ids:
                # D21: the original silently used the first of several invoices sharing a DocNumber.
                r.needs_qbo = False
                error = str(ambiguous_invoice_error(doc, tuple(str(i) for i in ids)))
                self._fail(r, texts.qb_processing_error_entry(doc, error),
                           reason="several QuickBooks invoices share the DocNumber", error=error)
                continue
            r.plan.invoice = invoices.get(doc)

    def _finalize(self, row: _Row) -> None:
        """2c — decisions (01 §6 steps 3d–e / d'–e', §6.3–§6.5, 10 §4 guards)."""
        plan = row.plan
        doc = plan.doc_number
        inv = plan.invoice
        assert plan.eta is not None
        eta = plan.eta.eta
        if inv is None:
            # D9 (out of window): the original silently skipped these rows.
            self._fail(row, texts.not_found_in_quickbooks_entry(doc), reason="not found in QuickBooks")
            return
        if not plan.in_window:                                                              # 01 §6.5
            plan.route = Route.FUTURE_UPDATE
            plan.actions = [QboUpdate(eta.date()),
                            VShipComment(texts.vship_comment_for(eta, notif_index=0, now=self.run_now)),
                            VShipPatchEta(eta)]
            return
        if inv.is_paid:                                                                     # 01 §6.3
            plan.route = Route.PAID_UPDATE
            plan.actions = [QboUpdate(eta.date()), VShipPatchEta(eta)]
            return
        if inv.is_partially_paid:                                                           # 01 §6.7
            if row.comments is None:
                error = plan.vship_error or row.comments_error or "VShip internal comments unavailable"
                self._fail(row, texts.qb_processing_error_entry(doc, error),
                           reason="TR/SWB lookup failed", error=error)
                return
            if has_tr_swb_note(row.comments):
                self._route(row, Route.ACCOUNTING, OutputFile.ACCOUNTING, texts.accounting_entry(doc))
                return
        self._plan_notify(row)

    def _plan_notify(self, row: _Row) -> None:
        """01 §6.4 actions and the 10 §4 guards."""
        plan = row.plan
        doc = plan.doc_number
        inv = plan.invoice
        assert inv is not None and plan.eta is not None
        eta = plan.eta.eta
        notif = plan.notif or NotifStatus(0, True)
        notif_sent = notif.count == 1
        subject = texts.subject_for(inv.doc_number, eta, notif_sent=notif_sent, paid=False, now=self.run_now)
        new_doc = arrival_doc_number(inv.doc_number, eta, self.today)
        note = texts.vship_comment_for(eta, notif_index=1 if notif_sent else 0, now=self.run_now)
        plan.route = Route.NOTIFY
        plan.actions = [QboUpdate(eta.date(), new_doc), QboSend(subject), VShipComment(note), VShipPatchEta(eta)]
        row.lines.append(f"  -> NOTIFY ({subject})")
        if not inv.recipients:
            self._fail(row, texts.qb_processing_error_entry(doc, texts.BILL_EMAIL_MISSING_REASON),
                       reason="invoice has no BillEmail", error=texts.BILL_EMAIL_MISSING_REASON)
            return
        refusal = redirect_refusal(self.settings.qbo, inv) if self.settings.qbo is not None else None
        if refusal and not self.no_email:
            # 04b §3 (review fix QBO-03): template_swap would refuse this send. Fail the row now, before its DocNumber
            # is suffixed (04 §6.6) — not at the send, after the QuickBooks update. --no-email sends nothing.
            self._fail(row, texts.qb_processing_error_entry(doc, refusal),
                       reason="QB_EMAIL_REDIRECT_TO not allowed with template_swap", error=refusal)
            return
        if new_doc is not None and self.settings.qbo is not None:
            problem = doc_number_problem(new_doc, self.settings.qbo)                        # 04 §7
            if problem:
                self._fail(row, texts.qb_processing_error_entry(doc, problem),
                           reason="new DocNumber too long", error=problem)
                return
        if not self.force:
            # D20: same-day e-mail guard — never e-mail one invoice twice on one day (--force overrides).
            sent_at = self.services.ledger.emailed_on(inv.id, self.today)
            if sent_at is not None:
                self._fail(row, texts.already_emailed_entry(doc, sent_at), reason="already e-mailed today")

    # ── plan helpers ──
    def _fail(self, row: _Row, entry: str, *, reason: str, error: str | None = None,
              file: OutputFile = OutputFile.FAILED, route: Route = Route.FAILED) -> None:
        plan = row.plan
        plan.route = route
        plan.output = (file, entry)
        plan.message = entry
        plan.error = error
        plan.actions = []
        row.reason = reason
        row.lines.append(f"  -> {route}: {_first_line(entry)}")

    def _route(self, row: _Row, route: Route, file: OutputFile, entry: str | list[str]) -> None:
        plan = row.plan
        plan.route = route
        plan.output = (file, entry)
        plan.message = entry if isinstance(entry, str) else ",".join(entry)
        plan.actions = []
        row.lines.append(f"  -> {route} ({file})")

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # Phase 3 — review & confirm
    # ════════════════════════════════════════════════════════════════════════════════════════════
    async def _review(self) -> int | None:
        """Returns an exit code to stop with (dry run / abort), or None to apply."""
        if self.no_email:                                                                   # step 1
            for row in self.rows:
                if row.plan.route is Route.NOTIFY and row.plan.actions:
                    self._to_manual(row)
        sends = any(isinstance(a, QboSend) for r in self.rows for a in r.plan.actions)
        if sends:                                                                           # step 2
            try:
                await self.qbo.prepare_for_sending(dry_run=self.dry_run)
            except Exception as exc:  # noqa: BLE001 - any problem with the e-mail path blocks every e-mail
                self._email_path_failed(exc, before_apply=True)
        self._one_notice_per_booking()          # XCT-01 after step 2 (NEW-02): a row it failed gives no notice
        await self._repair_email_template(template_swap_preflight_ran=sends)
        self._write_report()                                                                # step 3
        self._print_plan_summary()                                                          # step 4
        if self.dry_run:                                                                    # step 5
            self._print(f"Dry run: nothing was changed. Plan saved to {self.summary.report_csv}")
            return EXIT_OK
        n_actions = sum(len(r.plan.actions) for r in self.rows)
        if n_actions == 0:                                                                  # step 6
            return None
        if self.assume_yes:
            return None
        n_rows = sum(1 for r in self.rows if r.plan.actions)                               # step 7
        n_emails = sum(1 for r in self.rows for a in r.plan.actions if isinstance(a, QboSend))
        if not self.is_tty():
            self.summary.message = ("Not running interactively and --yes was not given: nothing was changed. "
                                    "Re-run with --yes to apply the plan.")
            self._print(self.summary.message)
            return EXIT_ABORTED
        prompt = (f"Apply {n_actions} change(s) to {n_rows} invoice(s) and send {n_emails} e-mail(s)? [y/N] ")
        try:
            answer = await read_line(self.input_fn, prompt)   # interruptible: one Ctrl+C ends the process
        except EOFError:
            answer = ""
        if str(answer).strip().lower() not in _YES:
            self.summary.message = "Aborted: nothing was changed."
            self._print(self.summary.message)
            return EXIT_ABORTED
        return None

    async def _repair_email_template(self, *, template_swap_preflight_ran: bool) -> None:
        """Step 2's repair for every real run (04b §6.2 step 1; review fix XCT-03): a QuickBooks invoice e-mail subject
        left swapped by a crashed template_swap run is restored before the prompt. The template_swap preflight does it
        for runs that send through template_swap; this covers the others — nothing to e-mail, --no-email,
        QB_EMAIL_STRATEGY=graph/smtp — so the company-wide subject never keeps naming another customer's booking.
        A dry run only warns; a failed restore is printed with the manual instructions and makes the run exit 4. So
        does a recovery file that cannot be read or is damaged, or this company's file without an original subject
        (review fix R2-03): this company's subject may still name another customer's booking, and a person must check
        it in QuickBooks and delete the file. Only a readable file of another QuickBooks company (review fix NEW-03)
        says nothing about this company's template and does not touch these runs: a WARNING names it and the run goes
        on (a run that e-mails through template_swap is stopped by its preflight instead, 04b §6.2 step 1)."""
        qbo_settings = self.settings.qbo
        recovery = self.settings.run.state_dir / RECOVERY_FILE_NAME
        if qbo_settings is None or not recovery.exists():
            return
        if self.dry_run:
            self._print(f"Warning: the QuickBooks invoice e-mail subject may still be modified by a crashed run "
                        f"({recovery}); a real run restores it before its prompt, or says why it cannot.")
            return
        if template_swap_preflight_ran and qbo_settings.email_strategy == "template_swap":
            return
        try:
            await self.qbo.recover_email_template()
        except Exception as exc:  # noqa: BLE001 - reported with the manual restore instructions
            if getattr(exc, "recovery_file_unusable", False):
                log.warning("The QuickBooks e-mail template recovery file %s was left as it is: %s This run e-mails "
                            "nothing through template_swap, so it goes on; a template_swap run sends no e-mail while "
                            "the file is there.", recovery, _sentence(str(exc)))
                return
            self.restore_error = exc
            log.error("The QuickBooks invoice e-mail template could not be restored: %s", exc)
            self._print(_BANNER)
            self._print("THE QUICKBOOKS INVOICE E-MAIL TEMPLATE COULD NOT BE RESTORED:")
            self._print(str(exc))
            self._print(_BANNER)

    def _to_manual(self, row: _Row) -> None:
        """--no-email (10 Phase 3 step 1): keep only the ETA update (no DocNumber suffix) and the VShip ETA patch;
        the e-mail, the suffix and the note are left for whoever sends the e-mail by hand."""
        plan = row.plan
        inv = plan.invoice
        assert inv is not None and plan.eta is not None
        update = next(a for a in plan.actions if isinstance(a, QboUpdate))
        send = next(a for a in plan.actions if isinstance(a, QboSend))
        note = next(a for a in plan.actions if isinstance(a, VShipComment))
        row.manual = _ManualEmail(subject=send.subject, doc_number_after=update.new_doc_number or inv.doc_number,
                                  note=note.text)
        plan.actions = [QboUpdate(plan.eta.eta.date(), new_doc_number=None), VShipPatchEta(plan.eta.eta)]

    def _email_path_failed(self, exc: BaseException, *, before_apply: bool) -> None:
        """04b §6.1: no invoice e-mail can be sent in this run. Before apply every NOTIFY plan fails (step 2);
        mid-run every NOTIFY plan that has not started yet fails without running any action (Phase 4)."""
        first = not self.email_blocked
        self.email_blocked = True
        if self.email_error is None:
            self.email_error = exc
        if isinstance(exc, TemplateRestoreError) and self.restore_error is None:
            self.restore_error = exc
        if first:
            self._print(_BANNER)
            self._print("INVOICE E-MAILS CANNOT BE SENT IN THIS RUN:")
            self._print(str(exc))
            self._print("NOTIFY rows are reported as failed (no e-mail); the other rows are still processed.")
            self._print(_BANNER)
            log.error("Invoice e-mail path unavailable: %s", exc)
        for row in self.rows:
            plan = row.plan
            if plan.route is not Route.NOTIFY or row.started or plan.status != "PENDING":
                continue
            if not any(isinstance(a, QboSend) for a in plan.actions):
                continue
            entry = texts.qb_processing_error_entry(plan.doc_number, exc)
            if before_apply:
                self._fail(row, entry, reason="e-mail path unavailable", error=str(exc))
            else:
                plan.route = Route.FAILED
                plan.actions = []
                plan.status = "FAILED"
                plan.error = str(exc)
                plan.message = entry
                row.reason = "e-mail path unavailable"
                row.apply_failed = True
                self._write_output(OutputFile.FAILED, entry)

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # Phase 4 — apply
    # ════════════════════════════════════════════════════════════════════════════════════════════
    async def _apply(self) -> None:
        self._console_best_effort = True    # R2-01: from here on a failing console never stops the run (_print)
        task = asyncio.current_task()
        assert task is not None
        # R2-02: from now until the run has ended, the first Ctrl+C still stops the run (step 3) and any other one only
        # repeats the stop message; in place before any row task exists, put back by execute() on every exit path.
        self._sigint_guard = _SigintGuard(asyncio.get_running_loop(), task, self._say_stopping)
        self._sigint_guard.install()
        for row in self.rows:                                                               # step 1
            plan = row.plan
            if plan.actions or row.after is not None:      # NEW-02: a held entry waits for its booking's notice
                continue
            if plan.output is not None:
                self._write_output(*plan.output)
            plan.status = _status_for(plan.route)
        todo = [r for r in self.rows if r.plan.actions]                                     # step 2
        if todo:
            qbo_settings = self.settings.qbo
            if qbo_settings is not None and qbo_settings.email_strategy == "template_swap":
                # NEW-01: template_swap sends one e-mail at a time (04b §6.2 step 3). The queue is kept here, where an
                # e-mail still waiting for its turn has written nothing and can be withdrawn by Ctrl+C (step 3).
                self._send_turn = asyncio.Lock()
            sem = asyncio.Semaphore(max(1, self.settings.run.max_parallel_bookings))
            tasks = [asyncio.create_task(self._apply_row(row, sem)) for row in todo]
            try:
                await asyncio.wait(tasks)
            except asyncio.CancelledError:                                                  # step 3
                await self._drain(tasks)
                for row in todo:
                    self._mark_interrupted(row)
                self._settle_held(interrupted=True)
                raise
            for task in tasks:                  # a row task reports its own failures; anything else is a bug
                if not task.cancelled() and (error := task.exception()) is not None:
                    raise error
        self._settle_held(interrupted=False)

    async def _drain(self, tasks: list[asyncio.Task[None]]) -> None:
        """Phase 4 step 3 after Ctrl+C (review fixes RUL-01, NEW-01, R2-01). Says at once that the started changes are
        being finished and that Ctrl+C must not be pressed again; until the run has ended a further Ctrl+C only repeats
        that (_SigintGuard, in place since Phase 4 began: asyncio.run() would turn it into KeyboardInterrupt and cancel
        every task, the shielded writes included). Plans not started yet stop, e-mails still waiting for their
        template_swap turn are withdrawn (nothing has been written for them), and every started action finishes with
        the revert or note that keeps its notice whole (_settle_interrupted). The tasks are cancelled before anything is
        printed, the message is best effort (_print) and the wait comes in any case, so a console that fails cannot
        skip it. A further cancellation of this task changes nothing."""
        for task in tasks:
            task.cancel()
        try:
            self._say_stopping()
        finally:
            while not all(task.done() for task in tasks):
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait(tasks)

    def _say_stopping(self) -> None:
        """NEW-01: printed at the first Ctrl+C during apply, and again at every further Ctrl+C until the run ends."""
        busy = sum(1 for row in self.rows if row.started and row.plan.status == "PENDING")
        doing = f"finishing the changes already started for {busy} invoice(s)" if busy else "finishing up"
        self._print(f"\nStopping (Ctrl+C): {doing}. Please wait - do not press Ctrl+C again.")

    def _settle_held(self, *, interrupted: bool) -> None:
        """End of Phase 4 (review fix NEW-02). A row that the one-notice-per-booking replay sent to Ben or skipped,
        because an earlier row of its booking gives the notice in this run, gets its entry only once that notice went
        out: the e-mail was sent, or with --no-email listed for sending by hand. Otherwise it is not processed — not
        e-mailed either: that e-mail was not in the confirmed plan and could be a second notice (outcome unknown) — and
        is reported with the reason (QB Invoice Processing Error, No Email Sent), or INTERRUPTED after Ctrl+C."""
        for row in self.rows:
            giver = row.after
            plan = row.plan
            if giver is None or plan.status != "PENDING":
                continue
            if giver.notice_out:
                if plan.output is not None:
                    self._write_output(*plan.output)
                plan.status = _status_for(plan.route)
            elif interrupted:
                plan.status = "INTERRUPTED"
                plan.message = f"interrupted before #{giver.plan.doc_number} (same booking) gave the notice"
            else:
                error = texts.same_booking_notice_not_sent_reason(giver.plan.doc_number)
                entry = texts.qb_processing_error_entry(plan.doc_number, error)
                plan.route = Route.FAILED
                plan.output = (OutputFile.FAILED, entry)
                self._apply_failure(row, entry, error=error, reason=error)

    def _mark_interrupted(self, row: _Row) -> None:
        plan = row.plan
        if plan.status == "PENDING":
            plan.status = "INTERRUPTED"
            plan.message = ("interrupted after: " + "; ".join(plan.done)) if plan.done else "interrupted (not started)"

    async def _apply_row(self, row: _Row, sem: asyncio.Semaphore) -> None:
        try:
            async with sem:
                if row.plan.status != "PENDING":   # failed meanwhile (blocked e-mail path)
                    return
                row.started = True
                await self._run_actions(row)
        except asyncio.CancelledError:
            self._mark_interrupted(row)
            raise
        except Exception as exc:  # a bug in one row must not stop the others
            log.exception("Unexpected error while applying #%s", row.plan.doc_number)
            if row.plan.status == "PENDING":
                entry = texts.qb_processing_error_entry(row.plan.doc_number, exc)
                self._apply_failure(row, entry, error=str(exc), reason="unexpected error")

    async def _run_actions(self, row: _Row) -> None:
        plan = row.plan
        inv = plan.invoice
        state = _ApplyState(current=inv, original_doc=inv.doc_number if inv is not None else plan.doc_number)
        for index, action in enumerate(plan.actions):
            turn = self._send_turn if isinstance(action, QboSend) else None
            if turn is not None:
                try:
                    await turn.acquire()
                except asyncio.CancelledError:
                    # NEW-01: Ctrl+C while this e-mail waited for its template_swap turn. Nothing has been written for
                    # it, so it is withdrawn; the suffix of the QBO update before it is put back (RUL-01).
                    if index:
                        await self._settle_interrupted(row, state, plan.actions[index - 1], plan.actions[index:])
                    raise
            step = asyncio.ensure_future(self._execute(row, state, action))
            try:
                ok = await asyncio.shield(step)
            except asyncio.CancelledError:
                # Phase 4 step 3: the current action finishes, then this plan stops — never between the two records
                # of a notice (_settle_interrupted).
                await _finish_shielded(step)
                if not step.cancelled() and step.exception() is None and step.result():
                    rest = plan.actions[index + 1:]
                    if rest:
                        await self._settle_interrupted(row, state, action, rest)
                    else:
                        self._finish_row(row)
                raise
            finally:
                if turn is not None:
                    turn.release()
            if not ok:
                return
        self._finish_row(row)

    async def _settle_interrupted(self, row: _Row, state: _ApplyState, done: Action, rest: list[Action]) -> None:
        """Ctrl+C stopped a NOTIFY plan after `done`. A notice is recorded twice — the `A` suffix and the VShip NOTIF
        note (01 §1) — and the next run must not see one without the other:
        - after the suffixing QBO update, before the e-mail: the D11 revert puts the DocNumber back, so the next run
          sees the invoice under its old number (a lone suffix would make it send the first notice again as 'X AA');
        - after the e-mail: the VShip note is still written, so the 7-day rule (01 §11) holds the next day.
        A failing revert or note writes its 01 §14 entry (FAILED); otherwise the caller marks the plan INTERRUPTED."""
        plan = row.plan
        if isinstance(done, QboUpdate) and state.doc_changed and any(isinstance(a, QboSend) for a in rest):
            revert = asyncio.ensure_future(self._revert(row, state))
            await _finish_shielded(revert)
            note = revert.result()
            if state.doc_changed:                                           # the revert failed too
                error = f"interrupted (Ctrl+C) before the e-mail was sent {note}"
                self._apply_failure(row, texts.qb_processing_error_entry(plan.doc_number, error), error=error,
                                    reason="interrupted before the e-mail was sent")
            else:
                plan.done.append(f"DocNumber reverted to {state.original_doc}")
        elif isinstance(done, QboSend) and isinstance(rest[0], VShipComment):
            await _finish_shielded(asyncio.ensure_future(self._execute(row, state, rest[0])))

    def _finish_row(self, row: _Row) -> None:
        plan = row.plan
        plan.status = "OK"
        plan.message = texts.NO_EMAIL_PENDING_MESSAGE if row.manual is not None else ""
        log.info("%s", texts.ok_log_line(plan.doc_number))

    async def _execute(self, row: _Row, state: _ApplyState, action: Action) -> bool:
        """One action; on failure the 01 §6.6 entry is written at once and the plan is FAILED. Returns success."""
        try:
            await self._perform(row, state, action)
        except Exception as exc:  # noqa: BLE001 - mapped to 01 §6.6
            if isinstance(action, QboSend) and getattr(exc, "receipt", None) is not None:
                self._sent_but_template_not_restored(row, action, exc)
                return True
            await self._action_failed(row, state, action, exc)
            return False
        row.plan.done.append(_describe(action))
        return True

    def _sent_but_template_not_restored(self, row: _Row, action: QboSend, exc: Exception) -> None:
        """04b §6.2 step 3.5: the customer WAS e-mailed and only the template restore failed afterwards (the error
        carries `.receipt`). The send counts as done: the ledger records the e-mail, the `A` suffix stays, and the
        row goes on with its VShip note and ETA (01 §6.4 steps 5–6) — the note is what the 7-day rule reads the next
        day (01 §11). Every other e-mail of this run is blocked and the restore instructions are printed (04b §6.1)."""
        plan = row.plan
        self._ledger(row, EMAILED, _receipt_detail(getattr(exc, "receipt", None)))
        row.notice_out = True
        plan.done.append(_describe(action))
        warning = f"e-mail sent, but the QuickBooks e-mail template could not be restored afterwards: {exc}"
        plan.warnings.append(warning)
        log.error("#%s: %s", plan.doc_number, warning)
        if isinstance(exc, EmailPathUnavailableError):
            self._email_path_failed(exc, before_apply=False)

    async def _perform(self, row: _Row, state: _ApplyState, action: Action) -> None:
        plan = row.plan
        if isinstance(action, QboUpdate):
            assert state.current is not None
            before = state.current.doc_number
            updated = await self.qbo.update_invoice(state.current, eta=action.eta, doc_number=action.new_doc_number)
            state.current = updated
            state.doc_changed = updated.doc_number != state.original_doc
            self._ledger(row, "qbo_update", {"eta": action.eta, "doc_number_before": before,
                                             "doc_number_after": updated.doc_number})
            if row.manual is not None:
                self._write_to_email(row)
        elif isinstance(action, QboSend):
            assert state.current is not None
            if self.email_blocked and self.email_error is not None:
                raise _blocked_copy(self.email_error)
            receipt = await self.qbo.send_invoice(state.current, subject=action.subject)
            row.notice_out = True
            self._ledger(row, EMAILED, _receipt_detail(receipt))
        elif isinstance(action, VShipComment):
            booking_id = self._booking_id(plan)
            await self.vship.post_comment(booking_id, action.text)
            self._ledger(row, "vship_comment", {"booking_id": booking_id, "text": action.text})
        elif isinstance(action, VShipPatchEta):
            booking_id = self._booking_id(plan)
            await self.vship.patch_expected_arrival(booking_id, action.eta)
            self._ledger(row, "vship_patch_eta", {"booking_id": booking_id,
                                                  "eta": action.eta.strftime("%Y-%m-%dT%H:%M:%S")})
        else:  # pragma: no cover - Action is a closed union
            raise TypeError(f"unknown action {action!r}")

    @staticmethod
    def _booking_id(plan: Plan) -> str:
        if plan.vship_booking_id is None:
            # 01 §6.6: "resolving the VShip booking id" failed in the plan phase → Note Write Error.
            raise _VShipIdUnavailable(plan.vship_error or "VShip booking id unknown")
        return plan.vship_booking_id

    async def _action_failed(self, row: _Row, state: _ApplyState, action: Action, exc: Exception) -> None:
        """01 §6.6 mapping of the first failing step (D12, D13), with the D11 revert for a failed send."""
        plan = row.plan
        doc = plan.doc_number
        error = str(exc)
        if isinstance(action, QboUpdate):
            self._ledger(row, "qbo_update", {"eta": action.eta, "error": error}, ok=False)
            entry = texts.qb_processing_error_entry(doc, error)
        elif isinstance(action, QboSend):
            # (A send error carrying `.receipt` — the e-mail went out — never gets here: _execute counts it as sent.)
            detail: dict[str, Any] = {"subject": action.subject, "error": error}
            if getattr(exc, "outcome_unknown", False):
                # 04b §7: the request may have reached the provider, so the e-mail may have gone out. The ledger's
                # same-day guard counts this record, so a re-run today needs --force (the D11 revert still runs).
                detail["outcome_unknown"] = True
            self._ledger(row, EMAILED, detail, ok=False)
            if state.doc_changed:
                # D11: the original left the DocNumber changed when the send failed; put it back.
                error = f"{error} {await self._revert(row, state)}"
            entry = texts.qb_processing_error_entry(doc, error)
        elif isinstance(action, VShipComment) or isinstance(exc, _VShipIdUnavailable):
            name = "vship_comment" if isinstance(action, VShipComment) else "vship_patch_eta"
            self._ledger(row, name, {"error": error}, ok=False)
            entry = texts.note_write_error_entry(doc, error)
        else:
            # D12: the original counted a failed VShip ETA PATCH as neither ok nor failed.
            self._ledger(row, "vship_patch_eta", {"error": error}, ok=False)
            entry = texts.booking_eta_change_failed_entry(doc, error)
        self._apply_failure(row, entry, error=error, reason=_first_line(entry).split(" - ", 1)[-1])
        if isinstance(action, QboSend) and isinstance(exc, EmailPathUnavailableError):
            # 04b §6.1: TemplateRestoreError / EmailPathUnavailableError (checked before plain InvoiceSendError)
            # block every e-mail of this run; this row's own entry is written first.
            self._email_path_failed(exc, before_apply=False)

    async def _revert(self, row: _Row, state: _ApplyState) -> str:
        """D11: update_invoice(updated, doc_number=original) — best effort; returns the note for the entry."""
        assert state.current is not None
        changed_to = state.current.doc_number
        try:
            reverted = await self.qbo.update_invoice(state.current, doc_number=state.original_doc)
        except Exception as exc:  # noqa: BLE001
            self._ledger(row, "qbo_revert_doc_number", {"from": changed_to, "to": state.original_doc,
                                                        "error": str(exc)}, ok=False)
            return texts.revert_failed_note(exc)
        state.current = reverted
        state.doc_changed = reverted.doc_number != state.original_doc
        self._ledger(row, "qbo_revert_doc_number", {"from": changed_to, "to": reverted.doc_number})
        return texts.reverted_note(state.original_doc)

    def _apply_failure(self, row: _Row, entry: str, *, error: str, reason: str) -> None:
        plan = row.plan
        plan.status = "FAILED"
        plan.error = error
        plan.message = entry
        row.reason = reason
        row.apply_failed = True
        self._write_output(OutputFile.FAILED, entry)       # 01 §6.6: appended immediately
        log.warning("%s", entry.rstrip("\n"))

    # ── writers ──
    def _write_output(self, file: OutputFile, entry: str | list[str]) -> None:
        try:
            self.services.outputs.write(file, entry)
        except OSError as exc:
            self.output_errors += 1
            log.error("Could not write to %s (%s). The entry was:\n%s", file, exc,
                      entry if isinstance(entry, str) else ",".join(entry))

    def _write_to_email(self, row: _Row) -> None:
        plan = row.plan
        inv = plan.invoice
        manual = row.manual
        assert inv is not None and manual is not None
        cells = [inv.doc_number, inv.id, inv.customer_name or "", ", ".join(inv.recipients),
                 ", ".join(split_addresses(getattr(inv, "bill_email_cc", None))), manual.subject,
                 manual.doc_number_after, manual.note]
        try:
            path = self.services.outputs.write_to_email(self.run_id, cells)
        except OSError as exc:
            self.output_errors += 1
            log.error("Could not write the manual e-mail list (%s): %s", exc, cells)
            return
        row.notice_out = True              # NEW-02: the notice is now a person's to send
        if path is not None:
            self.summary.to_email_csv = path

    def _ledger(self, row: _Row, action: str, detail: Mapping[str, Any], *, ok: bool = True) -> None:
        plan = row.plan
        inv = plan.invoice
        try:
            self.services.ledger.record(run_id=self.run_id, doc_number=plan.doc_number,
                                        invoice_id=inv.id if inv is not None else None,
                                        base_booking=plan.base_booking, action=action, detail=detail, ok=ok)
        except Exception as exc:  # noqa: BLE001 - the action itself is done; say loudly that the record is missing
            log.error("Could not write the ledger entry %s for #%s: %s", action, plan.doc_number, exc)

    # ════════════════════════════════════════════════════════════════════════════════════════════
    # Reports and console summaries (10 §7, §8, Phase 5)
    # ════════════════════════════════════════════════════════════════════════════════════════════
    def _report_row(self, row: _Row) -> dict[str, Any]:
        plan = row.plan
        eta = plan.eta
        inv = plan.invoice
        update = next((a for a in plan.actions if isinstance(a, QboUpdate)), None)
        send = next((a for a in plan.actions if isinstance(a, QboSend)), None)
        subject = send.subject if send is not None else (row.manual.subject if row.manual is not None else None)
        return {
            "line_no": plan.line_no,
            "doc_number": plan.doc_number,
            "amount": plan.parsed.row.amount_raw,
            "kind": str(plan.parsed.kind),
            "base_booking": plan.base_booking,
            "carrier": str(plan.carrier) if plan.carrier is not None else None,
            "eta": eta.eta.isoformat() if eta is not None else None,
            "eta_source": eta.source if eta is not None else None,
            "eta_is_actual": eta.is_actual if eta is not None else None,
            "eta_rescue": bool((eta.details or {}).get("rescue", False)) if eta is not None else None,
            "cutoff": plan.cutoff.isoformat() if plan.cutoff is not None else None,
            "in_window": plan.in_window,
            "notif_count": plan.notif.count if plan.notif is not None else None,
            "notif_defaulted": plan.notif_defaulted,
            "invoice_id": inv.id if inv is not None else None,
            "balance": str(inv.balance) if inv is not None else None,
            "total": str(inv.total_amount) if inv is not None else None,
            "bill_email": inv.bill_email if inv is not None else None,
            "route": str(plan.route),
            "new_doc_number": update.new_doc_number if update is not None else None,
            "subject": subject,
            "vship_booking_id": plan.vship_booking_id,
            "actions_planned": "; ".join(_describe(a) for a in plan.actions),
            "actions_done": "; ".join(plan.done),
            "status": plan.status,
            "message": plan.message,
            "warnings": " | ".join(plan.warnings),
        }

    def _counts(self) -> dict[str, Any]:
        by_route = Counter(str(r.plan.route) for r in self.rows)
        by_status: Counter[str] = Counter(str(r.plan.status) for r in self.rows)
        return {
            "by_route": {str(route): by_route.get(str(route), 0) for route in ROUTE_ORDER},
            "by_status": {status: by_status[status] for status in _STATUS_ORDER if by_status.get(status)},
            "ok": by_status.get("OK", 0),
            "failed": by_status.get("FAILED", 0),
            "routed": by_status.get("ROUTED", 0),
            "skipped": by_status.get("SKIPPED", 0),
            "interrupted": by_status.get("INTERRUPTED", 0),
        }

    def _write_report(self, *, final_code: int | None = None) -> None:
        meta = {
            "run_id": self.run_id,
            "report_stage": self.report_stage,
            "input_file": str(self.input_csv),
            "run_now": self.run_now.isoformat(),
            "cutoff": self.cutoff.isoformat() if self.cutoff is not None else None,
            "options": {"dry_run": self.dry_run, "assume_yes": self.assume_yes, "force": self.force,
                        "no_email": self.no_email, "only": sorted(self.only) if self.only is not None else None,
                        "limit": self.limit},
            "exit_code": final_code,
            "email_blocked": self.email_blocked,
            "email_error": str(self.email_error) if self.email_error is not None else None,
            "counts": self._counts(),
            "phase_timings": dict(self.timings),
            "paths": {"log_file": self.summary.log_file, "to_email_csv": self.summary.to_email_csv},
            "settings": settings_summary(self.settings),
            "versions": _versions(),
        }
        rows = [self._report_row(r) for r in self.rows]
        self.summary.report_csv, self.summary.report_json = write_report(
            self.settings.run.output_dir, self.run_id, rows, meta)

    def _print_plan_summary(self) -> None:
        cutoff = self.cutoff.strftime("%a %Y-%m-%d %H:%M") if self.cutoff is not None else "-"
        self._print(f"Plan (run {self.run_id}, {len(self.rows)} rows, cutoff {cutoff})")
        counts = Counter(r.plan.route for r in self.rows)
        for route in ROUTE_ORDER:
            line = f"  {_dots(route)} {counts.get(route, 0)}"
            if route is Route.NOTIFY:
                line += "   (e-mails to send by hand, --no-email)" if self.no_email else "   (e-mails to send)"
            self._print(line)
        warnings = [(r.plan.doc_number, w) for r in self.rows for w in r.plan.warnings]
        self._print(f"Warnings: {len(warnings)} (see report)")
        notify = [r for r in self.rows if r.plan.route is Route.NOTIFY]
        if notify:
            self._print("E-mails to send by hand (--no-email):" if self.no_email else "E-mails:")
            for row in notify:
                self._print("  " + self._notify_line(row))
        redirect = (self.settings.qbo.email_redirect_to or "").strip() if self.settings.qbo is not None else ""
        if redirect and any(isinstance(a, QboSend) for r in self.rows for a in r.plan.actions):
            # 04b §10 step 4: a shadow run changes only the recipients; the notice is still recorded as sent.
            self._print(f"QB_EMAIL_REDIRECT_TO is set: e-mails go to {redirect} instead of the customers, but the "
                        "QuickBooks and VShip changes are REAL (ETA, DocNumber suffix, NOTIF notes, ledger).")
        updates = [r for r in self.rows if r.plan.route in (Route.PAID_UPDATE, Route.FUTURE_UPDATE)]
        if updates:
            self._print("ETA updates:")
            for row in updates:
                assert row.plan.eta is not None
                self._print(f"  {row.plan.doc_number} → {_fmt_eta(row.plan.eta.eta)} ({row.plan.route})")
        self._print_failure_groups([r for r in self.rows if r.plan.route in (Route.FAILED, Route.CUSTOMS_HOLD)],
                                   "Failed / skipped rows:")
        if warnings:
            self._print("Warnings:")
            for doc, warning in warnings:
                self._print(f"  #{doc}: {warning}")

    def _notify_line(self, row: _Row) -> str:
        plan = row.plan
        inv = plan.invoice
        to = ", ".join(inv.recipients) if inv is not None else ""
        if row.manual is not None:
            return f"{plan.doc_number} → {row.manual.doc_number_after} | {row.manual.subject} | to: {to}"
        update = next((a for a in plan.actions if isinstance(a, QboUpdate)), None)
        send = next((a for a in plan.actions if isinstance(a, QboSend)), None)
        new_doc = update.new_doc_number if update is not None and update.new_doc_number else "(no change)"
        subject = send.subject if send is not None else ""
        return f"{plan.doc_number} → {new_doc} | {subject} | to: {to}"

    @staticmethod
    def _failure_key(row: _Row) -> tuple[str, str]:
        """(carrier prefix, reason) of a failed row: carrier lookup failures are grouped per carrier by their
        error text; other failures by their short reason plus the error text, if any."""
        plan = row.plan
        if plan.carrier is not None and plan.eta is None and plan.error:
            return f"{plan.carrier}: ", _first_line(plan.error)
        reason = row.reason or _first_line(plan.message)
        if plan.error and plan.error != reason:
            reason = f"{reason}: {_first_line(plan.error)}"
        return "", reason

    def _print_failure_groups(self, rows: Iterable[_Row], title: str) -> None:
        """Identical failures are grouped (per carrier for lookups, 10 §7) instead of printing the same line
        hundreds of times, e.g. `MSC: 287 booking(s) failed — <error> [EBKG…, …]`."""
        groups: dict[tuple[str, str], list[str]] = {}
        for row in rows:
            groups.setdefault(self._failure_key(row), []).append(row.plan.doc_number)
        if not groups:
            return
        self._print(title)
        for (carrier, reason), docs in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
            shown = ", ".join(docs[:_MAX_DOCS_SHOWN]) + (", …" if len(docs) > _MAX_DOCS_SHOWN else "")
            self._print(f"  {carrier}{len(docs)} booking(s) failed — {reason} [{shown}]")

    def _summarize(self, code: int) -> RunSummary:
        counts = self._counts()
        s = self.summary
        s.exit_code = code
        s.counts_by_route = dict(counts["by_route"])
        s.counts_by_status = dict(counts["by_status"])
        s.ok, s.failed, s.routed = counts["ok"], counts["failed"], counts["routed"]
        s.skipped, s.interrupted = counts["skipped"], counts["interrupted"]
        s.phase_timings = dict(self.timings)
        s.email_blocked = self.email_blocked
        s.plans = [r.plan for r in self.rows]
        return s

    def _finish(self) -> RunSummary:
        """Phase 5: Done line (01 §13), per-route table, timings, paths; the report is rewritten."""
        code = EXIT_EMAIL_BLOCKED if self.email_blocked or self.restore_error is not None else EXIT_OK
        if self.report_stage == "plan":
            self.report_stage = "final"
        counts = self._counts()
        self._print("")
        self._print(texts.done_line(counts["ok"], counts["failed"]))
        self._print(f"Result (run {self.run_id})")
        for route in ROUTE_ORDER:
            plans = [r.plan for r in self.rows if r.plan.route is route]
            statuses: Counter[str] = Counter(str(p.status) for p in plans)
            detail = ", ".join(f"{status} {statuses[status]}" for status in _STATUS_ORDER if statuses.get(status))
            self._print(f"  {_dots(route)} {len(plans)}" + (f"   {detail}" if detail else ""))
        self._print_failure_groups([r for r in self.rows if r.apply_failed], "Failed while applying:")
        if self.output_errors:
            noun = "entry" if self.output_errors == 1 else "entries"
            self._print(f"{self.output_errors} output {noun} could not be written - see the log.")
        if self.email_blocked or self.restore_error is not None:
            self._print(_BANNER)
            if self.email_blocked:
                self._print(f"The invoice e-mail path failed in this run: {self.email_error}")
            if self.restore_error is not None:
                self._print("The QuickBooks invoice e-mail template must be restored by hand:")
                self._print(str(self.restore_error))
            self._print(_BANNER)
        self._phase_done("report")
        self._print("Phase timings: " + ", ".join(f"{name} {seconds:.2f} s" for name, seconds in self.timings.items()))
        try:
            self._write_report(final_code=code)
        except OSError as exc:
            log.error("Could not rewrite the run report: %s", exc)
            self._print(f"The run report could not be rewritten ({exc}); it may still show the plan.")
        self._print(f"Report: {self.summary.report_csv} | {self.summary.report_json}")
        if self.summary.log_file is not None:
            self._print(f"Log: {self.summary.log_file}")
        if self.summary.to_email_csv is not None:
            self._print(f"E-mails to send by hand: {self.summary.to_email_csv}")
        return self._summarize(code)


async def _finish_shielded(fut: asyncio.Future[Any]) -> None:
    """Wait for `fut` although this task is being cancelled (Phase 4 step 3: a started write always finishes)."""
    while not fut.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.shield(fut)


class _SigintGuard:
    """Ctrl+C while Phase 4 changes things (review fixes NEW-01, R2-02; Windows and POSIX).

    asyncio.run() answers the first Ctrl+C by cancelling the main task, and turns any later one into KeyboardInterrupt,
    after which it cancels every task — the shielded writes of Phase 4 step 3 included, which could leave a notice
    half-recorded (RUL-01). This handler replaces asyncio.run()'s from the start of Phase 4, before any row task
    exists, until the run has ended (restore()), so no Ctrl+C can reach asyncio.run() in between, however busy the
    event loop is. The first Ctrl+C is passed on to the handler it replaced (asyncio.run()'s: the run is cancelled,
    stops as Phase 4 step 3 says and ends with exit code 3) — unless the run is being cancelled already (a Ctrl+C that
    reached asyncio.run() just before Phase 4, or any other cancellation). Every other one only schedules `warn` on the
    event loop (printing inside a signal handler could re-enter a console write). Only signal.signal(), the previous
    handler and call_soon_threadsafe() are used, which work the same on Windows and POSIX; outside the main thread
    signal.signal() raises and nothing is installed (Ctrl+C does not reach such a run), nor when SIGINT is not handled
    in Python (ignored, or the default action). restore() puts the previous handler back."""

    def __init__(self, loop: asyncio.AbstractEventLoop, task: asyncio.Task[Any], warn: Callable[[], None]) -> None:
        self._loop = loop
        self._task = task                               # the run's task: the one asyncio.run() cancels
        self._warn = warn
        self._previous: Callable[[int, FrameType | None], Any] | None = None
        self._passed_on = False

    def install(self) -> None:
        try:
            previous = signal.getsignal(signal.SIGINT)
            if not callable(previous):                  # SIG_IGN, SIG_DFL or not set from Python: leave it alone
                return
            # R3-02: known before the handler can run — a Ctrl+C that arrives while signal.signal() returns is then
            # passed on like any first one, instead of only repeating the stop message of a run that goes on.
            self._previous = previous
            signal.signal(signal.SIGINT, self._handle)
        except (ValueError, OSError, RuntimeError):     # not the main thread, or no signal support
            self._previous = None

    def _handle(self, signum: int, frame: FrameType | None) -> None:
        previous = self._previous
        if previous is not None and not self._passed_on and not self._task.cancelling():
            self._passed_on = True
            previous(signum, frame)     # asyncio.run(): cancel the run (another handler: whatever it does, as before)
            return
        with contextlib.suppress(RuntimeError):         # the event loop is already closed
            self._loop.call_soon_threadsafe(self._warn)

    def restore(self) -> None:
        previous = self._previous
        if previous is None:
            return
        with contextlib.suppress(ValueError, OSError, RuntimeError, TypeError):
            if signal.getsignal(signal.SIGINT) == self._handle:     # nobody replaced it meanwhile
                signal.signal(signal.SIGINT, previous)
        self._previous = None


def _sentence(text: str) -> str:
    """`text` ending with a full stop (log messages that go on after it)."""
    text = text.rstrip()
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _blocked_copy(exc: BaseException) -> EmailPathUnavailableError:
    """A fresh exception of the same kind for a later send while the e-mail path is blocked."""
    if isinstance(exc, TemplateRestoreError):
        return TemplateRestoreError(str(exc))
    return EmailPathUnavailableError(str(exc))


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Entry point (10 §4)
# ════════════════════════════════════════════════════════════════════════════════════════════════
async def run(settings: Settings, input_csv: Path, *, dry_run: bool = False, assume_yes: bool = False,
              force: bool = False, no_email: bool = False, only: Collection[str] | None = None,
              limit: int | None = None, clock: Clock | None = None, services: Services | None = None,
              input_fn: Callable[[str], str] | None = None, is_tty: Callable[[], bool] | None = None,
              getpass_fn: Callable[[str], str] | None = None, out: TextIO | None = None,
              log_file: Path | None = None) -> RunSummary:
    """Run the whole pipeline: Phase 0–5 of 10 §4. Returns a RunSummary whose exit_code follows 11 §3
    (0 done, 2 configuration/authentication/lock, 3 aborted, 4 e-mail path failed).

    Test/CLI hooks (additions to the 10 §4 signature): `services` replaces build_services() (the caller then
    owns and closes them); `input_fn` answers the confirmation prompt (default `input`, read through
    prompt.read_line, so one Ctrl+C ends the process); `is_tty` says whether stdin is interactive; `getpass_fn`
    reads a pasted MSC token; `out` receives the console summary (default sys.stdout); `log_file` is only reported.
    A Ctrl+C (task cancellation) during apply marks unfinished plans INTERRUPTED, rewrites the report and
    re-raises the cancellation after the clients are closed.
    """
    runner = _Run(
        settings, Path(input_csv), dry_run=dry_run, assume_yes=assume_yes, force=force, no_email=no_email,
        only=only, limit=limit, clock=clock or SystemClock(), input_fn=input_fn or input,
        is_tty=is_tty or _default_is_tty, getpass_fn=getpass_fn or getpass.getpass, out=out, log_file=log_file,
    )
    return await runner.execute(services)
