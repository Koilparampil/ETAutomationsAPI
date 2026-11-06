"""cli.py — the `eta` command line (11-cli, 12-testing §2/§6).

Argument parsing for every command and flag, the 11 §3 exit codes, double-click mode (monkeypatched input and
ui.pick_input_file), the end-to-end `eta run <csv> --dry-run` with pipeline.build_services monkeypatched to the
fakes of tests/fakes.py, `eta eta` with a fake router, every probe with its client mocked (output saved redacted,
exit codes), the QuickBooks write probes against an in-memory respx QuickBooks, msc-token (stored, never echoed),
qbo-auth, qbo-fields and version. No network. Every test runs in tmp_path (a real ./.env in the repository root is
never loaded) and passes --env-file wherever it needs settings.
"""

from __future__ import annotations

import asyncio
import base64
import builtins
import copy
import getpass
import importlib.metadata
import io
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from fakes import FakeCarrier, FakeWorld, fake_build_services

from eta_automations import __version__, cli, pipeline, ui
from eta_automations.carriers import aggregator as aggregator_carrier
from eta_automations.carriers import cma, maersk, msc
from eta_automations.carriers import router as carrier_router
from eta_automations.carriers.router import CarrierRouter
from eta_automations.clock import FixedClock
from eta_automations.errors import AuthError, ConfigError, NoEtaError
from eta_automations.ledger import LEDGER_FILE_NAME
from eta_automations.logging_setup import reset_logging
from eta_automations.models import Carrier, EtaResult, OutputFile
from eta_automations.pipeline import RunSummary
from eta_automations.qbo import auth as qbo_auth
from eta_automations.qbo.client import QboClient
from eta_automations.qbo.email import RECOVERY_FILE_NAME, SendReceipt, load_verifications
from eta_automations.run_lock import run_lock
from eta_automations.token_store import TokenStore
from eta_automations.ui import NO_GUI_MESSAGE
from eta_automations.vship import client as vship_client

REPO_ROOT = Path(__file__).resolve().parents[1]
RUN_NOW = datetime(2026, 11, 5, 9, 30)          # Thu; cutoff Mon 2026-11-16 09:30 (01 §5)
REALM = "1234567890"
SANDBOX_API = "https://sandbox-quickbooks.api.intuit.com"
PRODUCTION_API = "https://quickbooks.api.intuit.com"
ORIGINAL_SUBJECT = "Invoice [Invoice No.] from [Company Name]"
TEST_ENV = {
    "QB_CLIENT_ID": "test-qb-client-id",
    "QB_CLIENT_SECRET": "test-qb-client-secret",
    "QB_REALM_ID": REALM,
    "QB_REFRESH_TOKEN": "test-qb-refresh-token",
    "VSHIP_USER_NAME": "ops@example.com",
    "VSHIP_PASSWORD": "test-vship-password",
}
SECRETS = ("test-qb-client-secret", "test-qb-refresh-token", "test-vship-password", "AT-NEW-123", "RT-NEW-456")
_ENV_PREFIXES = ("QB", "VSHIP_", "MSC_", "MAERSK_", "CMA_", "AGG_", "ETA_", "EMAIL_", "LOGIN_URL", "HOME_PAGE")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Fixtures and helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture(autouse=True)
def _isolated_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every CLI test in tmp_path (./.env is never the repository's), without inherited program variables,
    non-interactive by default; afterwards drop the logging handlers main() installed."""
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith(_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: False)
    yield
    reset_logging()


def run_cli(argv: list[str] | None = None) -> int:
    """run_cli(argv) as one process would see it: load_dotenv() puts the .env values into os.environ and never
    removes them, so os.environ is restored after each call (several calls in one test must not leak settings)."""
    saved = os.environ.copy()
    try:
        return cli.main(argv)
    finally:
        os.environ.clear()
        os.environ.update(saved)


@pytest.fixture
def write_env(tmp_path: Path) -> Callable[..., Path]:
    """write_env(name="test.env", **overrides) -> path of a .env with tmp ETA_OUTPUT_DIR/ETA_STATE_DIR, the test
    QuickBooks/VShip credentials and the overrides (None removes a variable)."""

    def _write(name: str = "test.env", **overrides: str | None) -> Path:
        values: dict[str, str | None] = {"ETA_OUTPUT_DIR": str(tmp_path / "output"),
                                         "ETA_STATE_DIR": str(tmp_path / "state"), **TEST_ENV}
        values.update(overrides)
        path = tmp_path / name
        path.write_text("".join(f"{k}={v}\n" for k, v in values.items() if v is not None), encoding="utf-8")
        return path

    return _write


class Console:
    """Stand-in for builtins.input: answers in order (EOFError when none is left) and records every prompt."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.prompts: list[str] = []

    def __call__(self, prompt: str = "") -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)


@pytest.fixture
def console(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Console]:
    def _make(*answers: str, tty: bool = True) -> Console:
        fake = Console(list(answers))
        monkeypatch.setattr(builtins, "input", fake)
        monkeypatch.setattr(cli, "_stdin_is_tty", lambda: tty)
        return fake

    return _make


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    """The CLI's own SystemClock (probe timestamps, `eta eta` cutoff) frozen at RUN_NOW."""
    monkeypatch.setattr(cli, "SystemClock", lambda: FixedClock(RUN_NOW))
    return RUN_NOW


def probe_files(tmp_path: Path, pattern: str = "*.json") -> list[Path]:
    return sorted((tmp_path / "state" / "probe").glob(pattern))


def log_text(tmp_path: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in sorted((tmp_path / "output" / "logs").glob("*.log")))


def assert_no_secrets(*texts: str) -> None:
    for text in texts:
        for secret in SECRETS:
            assert secret not in text


class FakeProbeClient:
    """A client whose probe() returns a copy of `result` (or the result of `result(*args)`), recording calls."""

    def __init__(self, result: Mapping[str, Any] | Callable[..., Mapping[str, Any]], *,
                 on_probe: Callable[[], None] | None = None) -> None:
        self.result = result
        self.on_probe = on_probe
        self.calls: list[tuple[Any, ...]] = []
        self.kwargs: list[dict[str, Any]] = []
        self.closed = 0

    async def probe(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if self.on_probe is not None:
            self.on_probe()
        self.calls.append(args)
        self.kwargs.append(kwargs)
        value = self.result(*args) if callable(self.result) else self.result
        return copy.deepcopy(dict(value))

    def token_status(self) -> dict[str, Any]:
        return {"mode": "api", "host": "api.msc.example", "sandbox": False, "has_key": True}

    async def aclose(self) -> None:
        self.closed += 1


def _jwt(exp_offset: float, **claims: Any) -> str:
    def part(data: Mapping[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    now = int(time.time())
    payload = {"iss": "https://mymsc.example", "aud": "tracking", "exp": now + int(exp_offset), "iat": now,
               "sub": "user-secret-subject", **claims}
    return f"{part({'alg': 'none', 'typ': 'JWT'})}.{part(payload)}.c2lnbmF0dXJl"


# ── in-memory QuickBooks (respx) ──
def _fault(code: str, message: str) -> dict[str, Any]:
    return {"Fault": {"Error": [{"Message": message, "Detail": message, "code": code}], "type": "ValidationFault"},
            "time": "t"}


class FakeQuickBooks:
    """Token refresh, invoice query / read / sparse update, Preferences read / write and CompanyInfo, in memory."""

    def __init__(self, load_fixture: Callable[[str], Any], router: respx.MockRouter, *, sandbox: bool) -> None:
        self.router = router
        self.company = f"{SANDBOX_API if sandbox else PRODUCTION_API}/v3/company/{REALM}"
        self.token_status = 200
        self.token_body: dict[str, Any] = load_fixture("qbo/token_refresh_ok.json")
        raws = copy.deepcopy(load_fixture("qbo/query_invoices.json")["QueryResponse"]["Invoice"])
        self.invoices: dict[str, dict[str, Any]] = {str(r["Id"]): r for r in raws if r["Id"] != "190901"}
        self.prefs: dict[str, Any] = copy.deepcopy(load_fixture("qbo/preferences.json")["Preferences"])
        self.company_info = load_fixture("qbo/companyinfo.json")
        self.invoice_posts: list[dict[str, Any]] = []
        self.prefs_posts: list[dict[str, Any]] = []
        self.ignore_eta_writes = False
        self.lossy_prefs_key: str | None = None
        self.reject_invoice_post: int | None = None     # 1-based number of the invoice POST answered with a fault
        self.reject_prefs_posts_from: int | None = None  # every Preferences POST from this number on gets a fault
        router.post(qbo_auth.TOKEN_URL).mock(
            side_effect=lambda request: httpx.Response(self.token_status, json=self.token_body))
        router.get(f"{self.company}/query").mock(side_effect=self._query)
        for invoice_id in self.invoices:
            router.get(f"{self.company}/invoice/{invoice_id}").mock(side_effect=self._read)
        router.post(f"{self.company}/invoice").mock(side_effect=self._update)
        router.get(f"{self.company}/preferences").mock(
            side_effect=lambda request: httpx.Response(200, json={"Preferences": copy.deepcopy(self.prefs)}))
        router.post(f"{self.company}/preferences").mock(side_effect=self._write_prefs)
        router.get(f"{self.company}/companyinfo/{REALM}").mock(return_value=httpx.Response(200, json=self.company_info))

    @property
    def subject(self) -> str:
        return str(self.prefs["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"])

    def eta_of(self, invoice_id: str = "190613") -> str | None:
        return next((e.get("DateValue") for e in self.invoices[invoice_id]["CustomField"] if e.get("Name") == "ETA"),
                    None)

    def _query(self, request: httpx.Request) -> httpx.Response:
        wanted = set(re.findall(r"'((?:[^'\\]|\\.)*)'", request.url.params["query"]))
        found = [copy.deepcopy(r) for r in self.invoices.values() if r.get("DocNumber") in wanted]
        return httpx.Response(200, json={"QueryResponse": {"Invoice": found} if found else {}, "time": "t"})

    def _read(self, request: httpx.Request) -> httpx.Response:
        invoice_id = request.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json={"Invoice": copy.deepcopy(self.invoices[invoice_id]), "time": "t"})

    def _update(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.invoice_posts.append(body)
        if self.reject_invoice_post == len(self.invoice_posts):
            return httpx.Response(400, json=_fault("6000", "A business validation error has occurred"))
        invoice = self.invoices[str(body["Id"])]
        if str(body["SyncToken"]) != str(invoice["SyncToken"]):
            return httpx.Response(400, json=_fault("5010", "Stale Object Error"))
        if "CustomField" in body and not self.ignore_eta_writes:
            invoice["CustomField"] = copy.deepcopy(body["CustomField"])
        if "DocNumber" in body:
            invoice["DocNumber"] = body["DocNumber"]
        invoice["SyncToken"] = str(int(invoice["SyncToken"]) + 1)
        return httpx.Response(200, json={"Invoice": copy.deepcopy(invoice), "time": "t"})

    def _write_prefs(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.prefs_posts.append(body)
        if self.reject_prefs_posts_from is not None and len(self.prefs_posts) >= self.reject_prefs_posts_from:
            return httpx.Response(400, json=_fault("2010", "Request has invalid or unsupported property"))
        if str(body.get("SyncToken")) != str(self.prefs["SyncToken"]):
            return httpx.Response(400, json=_fault("5010", "Stale Object Error"))
        if body.get("sparse") is True:
            new = copy.deepcopy(self.prefs)
            new["EmailMessagesPrefs"]["InvoiceMessage"].update(body["EmailMessagesPrefs"]["InvoiceMessage"])
        else:
            new = copy.deepcopy(body)
        if self.lossy_prefs_key:
            new["SalesFormsPrefs"].pop(self.lossy_prefs_key, None)
        new["SyncToken"] = str(int(self.prefs["SyncToken"]) + 1)
        new["MetaData"] = {**self.prefs.get("MetaData", {}),
                           "LastUpdatedTime": f"2026-11-06T10:00:{len(self.prefs_posts):02d}-08:00"}
        self.prefs = new
        return httpx.Response(200, json={"Preferences": copy.deepcopy(new), "time": "t"})


@pytest.fixture
def quickbooks(load_fixture: Callable[[str], Any]) -> Iterator[Callable[..., FakeQuickBooks]]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:

        def _make(*, sandbox: bool = True) -> FakeQuickBooks:
            return FakeQuickBooks(load_fixture, router, sandbox=sandbox)

        yield _make


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Parsing (11 §2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_parser_run_every_flag() -> None:
    args = cli.build_parser().parse_args([
        "run", "in.csv", "--dry-run", "-y", "--force", "--no-email", "--only", "EBKG1", "ebkg2", "--only", "NAM3",
        "--limit", "5", "--max-parallel", "2", "--env-file", "x.env", "--log-level", "debug"])
    assert args.command == "run" and args.input_csv == Path("in.csv")
    assert args.dry_run and args.yes and args.force and args.no_email
    assert args.only == ["EBKG1", "ebkg2", "NAM3"]
    assert (args.limit, args.max_parallel) == (5, 2)
    assert args.env_file == Path("x.env") and args.log_level == "DEBUG"
    assert args.handler is cli._cmd_run


def test_parser_run_defaults() -> None:
    args = cli.build_parser().parse_args(["run", "--yes"])
    assert args.input_csv is None and args.yes
    assert not (args.dry_run or args.force or args.no_email)
    assert args.only is None and args.limit is None and args.max_parallel is None
    assert args.env_file is None and args.log_level is None


def test_parser_other_commands() -> None:
    parser = cli.build_parser()
    eta = parser.parse_args(["eta", "EBKG17208602", "267546627", "--json"])
    assert eta.bookings == ["EBKG17208602", "267546627"] and eta.json and eta.handler is cli._cmd_eta
    probe = parser.parse_args(["probe", "msc", "--booking", "EBKG1", "--booking", "EBKG2", "--compare"])
    assert probe.service == "msc" and probe.booking == ["EBKG1", "EBKG2"] and probe.compare
    assert not (probe.sandbox or probe.token_info or probe.save_raw or probe.yes)
    write = parser.parse_args(["probe", "qbo", "--write-test", "--invoice", "D1", "--yes"])
    assert write.write_test and write.invoice == "D1" and write.yes
    for flag in ("--prefs-roundtrip", "--restore-template"):
        assert getattr(parser.parse_args(["probe", "qbo", flag]), flag[2:].replace("-", "_"))
    sandbox = parser.parse_args(["probe", "msc", "--sandbox", "--save-raw"])
    assert sandbox.sandbox and sandbox.save_raw
    assert parser.parse_args(["probe", "msc", "--token-info"]).token_info
    assert parser.parse_args(["probe", "all", "-y"]).yes
    auth = parser.parse_args(["qbo-auth"])
    assert (auth.listen, auth.port, auth.no_browser) == (False, 8765, False)
    auth = parser.parse_args(["qbo-auth", "--listen", "--port", "9000", "--no-browser"])
    assert (auth.listen, auth.port, auth.no_browser) == (True, 9000, True)
    assert parser.parse_args(["qbo-fields", "--invoice", "EBKG1"]).invoice == "EBKG1"
    assert parser.parse_args(["msc-token"]).handler is cli._cmd_msc_token
    assert parser.parse_args(["version"]).handler is None


@pytest.mark.parametrize(("argv", "message"), [
    (["run", "x.csv", "--limit", "0"], "must be at least 1"),
    (["run", "x.csv", "--max-parallel", "abc"], "invalid number"),
    (["run", "--log-level", "verbose"], "invalid level"),
    (["frobnicate"], "invalid choice"),
    (["eta"], "required"),
    (["eta", "  "], "at least one booking"),
    (["probe", "fedex"], "invalid choice"),
    (["probe", "maersk", "--booking", "267546627", "--write-test"], "only work with `eta probe qbo`"),
    (["probe", "all", "--write-test", "--invoice", "X"], "only work with `eta probe qbo`"),
    (["probe", "all", "--sandbox"], "only work with `eta probe msc`"),
    (["probe", "qbo", "--write-test"], "needs --invoice"),
    (["probe", "qbo", "--send-test"], "needs --invoice"),
    (["probe", "qbo", "--write-test", "--send-test", "--invoice", "X"], "not allowed with"),
    (["probe", "vship"], "needs --booking"),
    (["probe", "maersk"], "needs --booking"),
    (["probe", "cma"], "needs --booking"),
    (["probe", "msc"], "needs --booking"),
    (["probe", "msc", "--compare"], "needs --booking"),
    (["probe", "msc", "--sandbox", "--booking", "EBKG1"], "take no --booking"),
    (["probe", "msc", "--sandbox", "--compare"], "not allowed with"),
    (["probe", "cma", "--booking", "NAM1234567", "--invoice", "X"], "--invoice is only used"),
    (["qbo-auth", "--port", "70000"], "between 1 and 65535"),
    (["qbo-fields"], "required"),
])
def test_usage_errors_exit_2(argv: list[str], message: str, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(argv) == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--help"], ["run", "--help"], ["probe", "--help"], ["eta", "--help"],
                                  ["qbo-auth", "--help"], ["msc-token", "--help"], ["qbo-fields", "--help"],
                                  ["version", "--help"], ["--version"]])
def test_help_exits_0(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(argv) == 0
    assert "eta" in capsys.readouterr().out


def test_help_texts_name_every_command_and_exit_code(capsys: pytest.CaptureFixture[str]) -> None:
    run_cli(["--help"])
    out = capsys.readouterr().out
    for command in ("run", "eta", "probe", "qbo-auth", "msc-token", "qbo-fields", "version"):
        assert command in out
    for code in ("0  completed", "2  configuration", "3  aborted", "4  run completed"):
        assert code in out
    run_cli(["probe", "--help"])
    out = capsys.readouterr().out
    for flag in ("--write-test", "--prefs-roundtrip", "--send-test", "--restore-template", "--sandbox",
                 "--token-info", "--compare", "--save-raw", "--booking", "--invoice", "--yes"):
        assert flag in out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["version"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"eta-automations {__version__}"
    assert lines[1].startswith("Python ")
    assert any(line.startswith("httpx ") for line in lines)
    assert any(line.startswith("pandas ") for line in lines)


def test_console_script_module_entry_and_no_browser_libraries(tmp_path: Path) -> None:
    """11 §1 entry points; 12-testing §6: importing the package imports no browser-automation library (and no
    tkinter: the file picker imports it lazily)."""
    scripts = importlib.metadata.entry_points(group="console_scripts", name="eta")
    assert [ep.value for ep in scripts] == ["eta_automations.cli:main"]
    code = ("import sys, eta_automations, eta_automations.cli, eta_automations.__main__; "
            "bad = ('playwright', 'selenium', 'pyppeteer', 'undetected_chromedriver', 'splinter', 'tkinter'); "
            "print(','.join(m for m in bad if m in sys.modules))")
    checked = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=tmp_path, timeout=120,
                             check=False)
    assert checked.returncode == 0, checked.stderr
    assert checked.stdout.strip() == ""
    helped = subprocess.run([sys.executable, "-m", "eta_automations", "--help"], capture_output=True, text=True,
                            cwd=tmp_path, timeout=120, check=False)
    assert helped.returncode == 0, helped.stderr
    assert "usage: eta" in helped.stdout


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Console, logging and settings (11 §1)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_stdout_and_stderr_are_reconfigured_to_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    out = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    err = io.TextIOWrapper(io.BytesIO(), encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert run_cli(["--help"]) == 0          # the help text contains "§" and "—"
    assert (out.encoding, out.errors, err.encoding, err.errors) == ("utf-8", "replace", "utf-8", "replace")
    out.flush()
    assert "§" in out.buffer.getvalue().decode("utf-8")  # type: ignore[attr-defined]


def test_logging_is_configured_before_settings_and_writes_a_debug_log_file(
        write_env: Callable[..., Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    env = write_env(QB="old-web-password", QB_HEADLESS="false")
    assert run_cli(["eta", "XYZ", "--env-file", str(env)]) == 0
    err = capsys.readouterr().err
    assert "QB and QB_HEADLESS are no longer used" in err      # logged by load_settings() itself
    assert "old-web-password" not in err
    assert "command eta" not in err                            # DEBUG: not on the INFO console …
    assert "command eta" in log_text(tmp_path)                 # … but in the DEBUG log file


def test_log_level_option_overrides_the_console_level(write_env: Callable[..., Path],
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["eta", "XYZ", "--log-level", "DEBUG", "--env-file", str(write_env(QB="x"))]) == 0
    assert "command eta" in capsys.readouterr().err
    assert run_cli(["eta", "XYZ", "--log-level", "WARNING", "--env-file", str(write_env(QB="x"))]) == 0
    assert "no longer used" not in capsys.readouterr().err


def test_missing_env_file_and_malformed_settings_exit_2(write_env: Callable[..., Path], tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["eta", "XYZ", "--env-file", str(tmp_path / "nope.env")]) == 2
    assert "Settings file not found" in capsys.readouterr().err
    env = write_env(ETA_MAX_PARALLEL_BOOKINGS="lots")
    assert run_cli(["run", "x.csv", "--env-file", str(env)]) == 2
    assert "ETA_MAX_PARALLEL_BOOKINGS" in capsys.readouterr().err


@pytest.mark.parametrize(("name", "data"), [
    ("cp1252.env", b"VSHIP_USER_NAME=ops@example.com\nVSHIP_PASSWORD=Pass\xe9word\n"),          # Windows "ANSI"
    ("utf16.env", "VSHIP_PASSWORD=Passéword\n".encode("utf-16")),                               # BOM ff fe
])
def test_env_file_that_is_not_utf8_is_a_configuration_error(name: str, data: bytes, tmp_path: Path,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    """CLI-02: a .env saved as cp1252 or UTF-16 (PowerShell `>`, Notepad 'Unicode') is a configuration error (11 §3
    exit 2) that names the file and says to save it as UTF-8 — not an 'Unexpected error' traceback (exit 1)."""
    path = tmp_path / name
    path.write_bytes(data)
    assert run_cli(["msc-token", "--env-file", str(path)]) == 2
    err = capsys.readouterr().err
    assert str(path) in err and "UTF-8" in err
    assert "Traceback" not in err and "Unexpected error" not in err


def test_repository_dot_env_is_never_loaded(write_env: Callable[..., Path], tmp_path: Path) -> None:
    assert Path.cwd() == tmp_path and Path.cwd() != REPO_ROOT
    (tmp_path / ".env").write_text("ETA_MAX_PARALLEL_BOOKINGS=not-a-number\n", encoding="utf-8")
    assert run_cli(["eta", "XYZ", "--env-file", str(write_env())]) == 0   # --env-file: ./.env is not read
    assert run_cli(["eta", "XYZ"]) == 2                                    # ./.env is the cwd's (tmp) file


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta run (11 §2, 12-testing §6)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def world_csv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[FakeWorld, Path]:
    """pipeline.build_services → the fakes (12-testing §6) and the pipeline clock frozen at RUN_NOW."""
    world = FakeWorld(FixedClock(RUN_NOW))
    rows = [
        world.add("EBKG10000001", "2450.00", eta=datetime(2026, 11, 3)),   # NOTIFY (arrived, unpaid)
        world.add("EBKG10000007", "5000", eta=datetime(2026, 12, 1)),      # FUTURE_UPDATE
        world.add("EBKG10000013", "850", customer="ACME"),                 # BINDU
    ]
    csv_path = tmp_path / "sample_input.csv"
    csv_path.write_text("".join(f"{row.line}\n" for row in rows), encoding="utf-8")
    monkeypatch.setattr(pipeline, "build_services", fake_build_services(world))
    monkeypatch.setattr(pipeline, "SystemClock", lambda: FixedClock(RUN_NOW))
    return world, csv_path


def test_run_dry_run_end_to_end(world_csv: tuple[FakeWorld, Path], write_env: Callable[..., Path], tmp_path: Path,
                                capsys: pytest.CaptureFixture[str]) -> None:
    world, csv_path = world_csv
    write_env(name=".env")                       # ./.env of the (temporary) working directory
    assert run_cli(["run", str(csv_path), "--dry-run"]) == 0       # exactly the 12-testing §6 call
    out = capsys.readouterr().out
    assert "Dry run: nothing was changed. Plan saved to" in out
    assert world.writes() == []
    output = tmp_path / "output"
    assert list((output / "reports").glob("run_20261105_093000.json"))
    assert not any((output / f.value).exists() for f in OutputFile)
    assert not (tmp_path / "state" / LEDGER_FILE_NAME).exists()
    assert list((output / "logs").glob("eta_*.log"))


def test_run_applies_with_yes(world_csv: tuple[FakeWorld, Path], write_env: Callable[..., Path], tmp_path: Path,
                              capsys: pytest.CaptureFixture[str]) -> None:
    world, csv_path = world_csv
    assert run_cli(["run", str(csv_path), "--yes", "--env-file", str(write_env())]) == 0
    names = [call[0] for call in world.writes()]
    assert "qbo.send_invoice" in names and "vship.patch_expected_arrival" in names
    assert "EBKG10000013,850,ACME" in (tmp_path / "output" / OutputFile.BINDU.value).read_text(encoding="utf-8-sig")
    out = capsys.readouterr().out
    assert "Done: 2 succeeded" in out and "Log: " in out


def test_run_non_interactive_without_yes_aborts_with_3(world_csv: tuple[FakeWorld, Path],
                                                        write_env: Callable[..., Path]) -> None:
    world, csv_path = world_csv
    assert run_cli(["run", str(csv_path), "--env-file", str(write_env())]) == 3
    assert world.writes() == []


def test_run_confirmation_prompt_on_a_terminal(world_csv: tuple[FakeWorld, Path], write_env: Callable[..., Path],
                                               console: Callable[..., Console]) -> None:
    world, csv_path = world_csv
    answers = console("n")
    assert run_cli(["run", str(csv_path), "--env-file", str(write_env())]) == 3
    assert answers.prompts == ["Apply 7 change(s) to 2 invoice(s) and send 1 e-mail(s)? [y/N] "]
    assert world.writes() == []
    console("yes")
    assert run_cli(["run", str(csv_path), "--env-file", str(write_env())]) == 0
    assert world.writes()


def test_run_passes_every_flag_to_the_pipeline(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path],
                                               tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    async def fake_run(settings: Any, input_csv: Path, **kwargs: Any) -> RunSummary:
        seen.update(settings=settings, input_csv=input_csv, **kwargs)
        return RunSummary(run_id="r", exit_code=4)

    monkeypatch.setattr(pipeline, "run", fake_run)
    code = run_cli(["run", "in.csv", "--dry-run", "--yes", "--force", "--no-email", "--only", "A", "b", "--limit",
                     "7", "--max-parallel", "3", "--env-file", str(write_env())])
    assert code == 4                                                   # pipeline exit codes pass through
    assert seen["input_csv"] == Path("in.csv")
    assert seen["dry_run"] and seen["assume_yes"] and seen["force"] and seen["no_email"]
    assert seen["only"] == ["A", "b"] and seen["limit"] == 7
    assert seen["settings"].run.max_parallel_bookings == 3
    assert seen["log_file"].parent == tmp_path / "output" / "logs"
    assert seen["is_tty"]() is False and seen["getpass_fn"] is cli._read_secret
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": f"typed after {prompt}")
    assert seen["getpass_fn"]("Token: ") == "typed after Token: "


def test_run_dry_run_with_real_clients_stops_cleanly_at_the_auth_preflight(
        write_env: Callable[..., Path], tmp_path: Path, http_sleeps: list[float],
        capsys: pytest.CaptureFixture[str]) -> None:
    """The real build_services (no fakes) with fake credentials and no network: the QuickBooks token refresh fails,
    so `eta run --dry-run` exits 2 (10 Phase 0 step 6) with a clear message, no traceback, no secret and nothing
    written to the output files."""
    csv_path = tmp_path / "tiny.csv"
    csv_path.write_text("EBKG17208602,2450.00\nNAM1234567,1800\n267546627,5000\n", encoding="utf-8")
    env = write_env()
    with respx.mock(assert_all_called=True, assert_all_mocked=True) as router:
        token = router.post("https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer").mock(
            side_effect=httpx.ConnectError("All connection attempts failed"))
        assert run_cli(["run", str(csv_path), "--dry-run", "--env-file", str(env)]) == 2
    assert token.call_count == 4 and len(http_sleeps) == 3          # 03 §6: a ConnectError is retried, then given up
    err = capsys.readouterr().err
    assert "QuickBooks authentication failed: qbo-oauth: network error" in err
    assert "QuickBooks could not be reached: check the network connection" in err
    assert "Traceback" not in err and "Traceback" not in log_text(tmp_path)
    assert not any(secret in err or secret in log_text(tmp_path) for secret in SECRETS)
    output = tmp_path / "output"
    assert not any((output / f.value).exists() for f in OutputFile)
    assert list((output / "reports").iterdir()) == []
    assert not (tmp_path / "state" / LEDGER_FILE_NAME).exists()


def test_run_missing_input_file_exits_2(write_env: Callable[..., Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["run", "missing.csv", "--dry-run", "--env-file", str(write_env())]) == 2
    assert "Input file not found" in capsys.readouterr().err


def test_unexpected_crash_exits_1_with_the_traceback_printed_and_logged(
        monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path], tmp_path: Path,
        capsys: pytest.CaptureFixture[str]) -> None:
    async def boom(*args: Any, **kwargs: Any) -> RunSummary:
        raise RuntimeError("boom-crash")

    monkeypatch.setattr(pipeline, "run", boom)
    assert run_cli(["run", "x.csv", "--env-file", str(write_env())]) == 1
    err = capsys.readouterr().err
    assert "Traceback" in err and "RuntimeError: boom-crash" in err
    assert "boom-crash" in log_text(tmp_path) and "Traceback" in log_text(tmp_path)


def test_ctrl_c_exits_3(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path],
                        capsys: pytest.CaptureFixture[str]) -> None:
    async def interrupted(*args: Any, **kwargs: Any) -> RunSummary:
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "run", interrupted)
    assert run_cli(["run", "x.csv", "--env-file", str(write_env())]) == 3
    assert "Interrupted (Ctrl+C)." in capsys.readouterr().err


def test_two_quick_ctrl_c_presses_during_eta_run_finish_the_notice_and_exit_3(
        world_csv: tuple[FakeWorld, Path], write_env: Callable[..., Path], capsys: pytest.CaptureFixture[str]) -> None:
    """R2-02 through `eta run --yes` (asyncio.run()): two Ctrl+C presses while an e-mail is in flight, before the run
    could begin to stop (no real signal is sent: the installed handler is called). The first stops the run, the second
    only repeats the stop message; the e-mail still gets its VShip note and the program ends with exit code 3."""
    world, csv_path = world_csv
    pressed: list[bool] = []

    async def hold(name: str, target: Any) -> None:
        if name == "qbo.send_invoice" and not pressed:
            pressed.append(True)
            for _ in range(2):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)   # type: ignore[operator]
            for _ in range(10):
                await asyncio.sleep(0)            # the e-mail is still in flight

    world.qbo.before_write = hold
    assert run_cli(["run", str(csv_path), "--yes", "--env-file", str(write_env())]) == 3
    subject = "Invoice for Booking no. EBKG10000001. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE CARGO****"
    assert world.calls.writes("1001", "70001") == [
        ("qbo.update_invoice", "EBKG10000001", date(2026, 11, 3), "EBKG10000001 A"),
        ("qbo.send_invoice", "EBKG10000001 A", subject),
        ("vship.post_comment", "70001", "ARRIVED ON 11/03. NOTIF #1")]
    captured = capsys.readouterr()
    assert captured.out.count("Please wait - do not press Ctrl+C again.") == 2
    assert "Interrupted (Ctrl+C)." in captured.err
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler


class GoneConsole(io.StringIO):
    """A console stream whose reader has gone (`eta run … 2>&1 | tee run.log` after Ctrl+C): every write raises."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_ctrl_c_exits_3_when_the_console_is_gone(monkeypatch: pytest.MonkeyPatch,
                                                write_env: Callable[..., Path]) -> None:
    """R2-01: the `Interrupted (Ctrl+C).` line is best effort, so a console that is gone does not turn the end of a
    stopped run into a traceback (exit code 1)."""
    async def interrupted(*args: Any, **kwargs: Any) -> RunSummary:
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "run", interrupted)
    monkeypatch.setattr(sys, "stderr", GoneConsole())
    assert run_cli(["run", "x.csv", "--env-file", str(write_env())]) == 3


@pytest.mark.skipif(sys.platform == "win32", reason="replaces the process's stdout with a POSIX pipe")
@pytest.mark.parametrize("unbuffered", [False, True], ids=["buffered", "PYTHONUNBUFFERED"])
@pytest.mark.parametrize("method", ["qbo.update_invoice", "qbo.send_invoice"])
def test_ctrl_c_with_eta_run_piped_into_tee_still_finishes_the_notice_and_exits_3(
        write_env: Callable[..., Path], tmp_path: Path, method: str, unbuffered: bool) -> None:
    """R2-01 end to end: `eta run x.csv --yes | tee run.log` and one Ctrl+C. The terminal sends it to tee as well, so
    from then on stdout is a pipe without a reader. A real process runs `eta run` through cli.main() (asyncio.run(), the
    real sys.stdout) on the fakes; while the QBO update or the e-mail is in flight, fd 1 becomes such a pipe and the
    installed SIGINT handler is called (no real signal is sent). The write in flight still gets its revert or VShip
    note, the report is rewritten and the process ends with exit code 3. Before the fix the stop message raised
    BrokenPipeError out of the drain: the in-flight write was cancelled, the report kept its plan and the exit code was
    1. R3-01: with buffered output (no PYTHONUNBUFFERED, the usual console) the stop message that the pipe did not take
    stays buffered; the interpreter's last flush at exit used to fail on it and turn exit code 3 into 120."""
    code = textwrap.dedent(f"""
        import asyncio, json, os, signal, sys
        from datetime import datetime
        sys.path.insert(0, {str(Path(__file__).parent)!r})
        from fakes import FakeWorld, fake_build_services
        from eta_automations import cli, pipeline
        from eta_automations.clock import FixedClock

        clock = FixedClock(datetime(2026, 11, 5, 9, 30))
        world = FakeWorld(clock)
        booking = world.add("EBKG10000001", "2450.00", eta=datetime(2026, 11, 3))
        with open("input.csv", "w", encoding="utf-8") as fh:
            fh.write(booking.line + "\\n")

        async def ctrl_c_with_tee(name, target):
            if name == {method!r}:
                read_end, write_end = os.pipe()
                os.close(read_end)                         # tee ends with the Ctrl+C ...
                os.dup2(write_end, sys.stdout.fileno())
                os.close(write_end)
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)     # ... that asyncio.run() gets
                for _ in range(50):
                    await asyncio.sleep(0)                 # the write is still in flight while the run stops

        world.qbo.before_write = ctrl_c_with_tee
        pipeline.build_services = fake_build_services(world)
        pipeline.SystemClock = lambda: clock
        code = cli.main(["run", "input.csv", "--yes", "--env-file", {str(write_env())!r}])
        with open("writes.json", "w", encoding="utf-8") as fh:
            json.dump([[str(part) for part in call] for call in world.writes()], fh)
        sys.exit(code)
    """)
    env = {name: value for name, value in os.environ.items() if name != "PYTHONUNBUFFERED"}
    if unbuffered:
        env["PYTHONUNBUFFERED"] = "1"
    done = subprocess.run([sys.executable, "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                          cwd=tmp_path, env=env, timeout=120, check=False)
    assert done.returncode == 3, done.stderr
    assert "Exception ignored" not in done.stderr, done.stderr
    writes = json.loads((tmp_path / "writes.json").read_text(encoding="utf-8"))
    suffix = ["qbo.update_invoice", "EBKG10000001", "2026-11-03", "EBKG10000001 A"]
    if method == "qbo.update_invoice":
        assert writes == [suffix, ["qbo.update_invoice", "EBKG10000001 A", "None", "EBKG10000001"]]
    else:
        subject = ("Invoice for Booking no. EBKG10000001. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE "
                   "CARGO****")
        assert writes == [suffix, ["qbo.send_invoice", "EBKG10000001 A", subject],
                          ["vship.post_comment", "70001", "ARRIVED ON 11/03. NOTIF #1"]]   # the note, no ETA patch
    report = json.loads((tmp_path / "output" / "reports" / "run_20261105_093000.json").read_text(encoding="utf-8"))
    assert (report["report_stage"], [r["status"] for r in report["rows"]]) == ("interrupted", ["INTERRUPTED"])
    assert "Console output failed (BrokenPipeError" in done.stderr and "Interrupted (Ctrl+C)." in done.stderr


def test_prompt_restores_the_sigint_handler(console: Callable[..., Console]) -> None:
    before = signal.getsignal(signal.SIGINT)
    answers = console("y")
    assert cli._ask_yes("Go? [y/N] ")
    assert not cli._ask_yes("Again? [y/N] ")          # EOF counts as "no"
    assert answers.prompts == ["Go? [y/N] ", "Again? [y/N] "]
    assert signal.getsignal(signal.SIGINT) is before


# ── double-click mode (11 §5) ──
@pytest.fixture
def fake_pipeline_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {"exit_code": 0}

    async def fake_run(settings: Any, input_csv: Path, **kwargs: Any) -> RunSummary:
        seen.update(input_csv=input_csv, **kwargs)
        if isinstance(seen.get("raise"), BaseException):
            raise seen["raise"]
        return RunSummary(run_id="r", exit_code=seen["exit_code"])

    monkeypatch.setattr(pipeline, "run", fake_run)
    return seen


def test_double_click_uses_the_file_picker_and_pauses(write_env: Callable[..., Path], tmp_path: Path,
                                                      monkeypatch: pytest.MonkeyPatch, console: Callable[..., Console],
                                                      fake_pipeline_run: dict[str, Any]) -> None:
    write_env(name=".env")                      # no arguments → ./.env of the working directory
    picked = tmp_path / "picked.csv"
    picked.write_text("EBKG17208602,2450\n", encoding="utf-8")
    monkeypatch.setattr(ui, "pick_input_file", lambda: picked)
    answers = console()                         # nothing to answer: EOF at the pause is ignored
    assert run_cli([]) == 0
    assert fake_pipeline_run["input_csv"] == picked
    assert fake_pipeline_run["assume_yes"] is False and fake_pipeline_run["dry_run"] is False   # prompt still applies
    assert answers.prompts == [cli.PAUSE_PROMPT] == ["\nPress ENTER to close this window..."]
    monkeypatch.setattr(sys, "argv", ["eta"])
    fake_pipeline_run["exit_code"] = 4
    assert run_cli() == 4                      # argv None → sys.argv[1:] == [] → double-click mode too
    assert answers.prompts[-1] == cli.PAUSE_PROMPT


@pytest.mark.parametrize(("picker", "expected", "message"), [
    (lambda: None, 3, "No input file selected"),
    ("no-gui", 2, NO_GUI_MESSAGE),
    ("ctrl-c", 3, "Interrupted"),
])
def test_double_click_pauses_after_cancel_errors_and_ctrl_c(
        picker: Any, expected: int, message: str, write_env: Callable[..., Path], monkeypatch: pytest.MonkeyPatch,
        console: Callable[..., Console], capsys: pytest.CaptureFixture[str]) -> None:
    write_env(name=".env")

    def raising() -> None:
        if picker == "no-gui":
            raise ConfigError(NO_GUI_MESSAGE)
        raise KeyboardInterrupt

    monkeypatch.setattr(ui, "pick_input_file", picker if callable(picker) else raising)
    answers = console("")
    assert run_cli([]) == expected
    captured = capsys.readouterr()
    assert message in captured.out + captured.err
    assert answers.prompts == [cli.PAUSE_PROMPT]


def test_double_click_pauses_after_a_crash(write_env: Callable[..., Path], tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch, console: Callable[..., Console],
                                           fake_pipeline_run: dict[str, Any]) -> None:
    write_env(name=".env")
    monkeypatch.setattr(ui, "pick_input_file", lambda: tmp_path / "picked.csv")
    fake_pipeline_run["raise"] = RuntimeError("crash in double-click mode")
    answers = console()
    assert run_cli([]) == 1
    assert answers.prompts == [cli.PAUSE_PROMPT]


def test_command_line_mode_never_pauses(write_env: Callable[..., Path], console: Callable[..., Console],
                                        fake_pipeline_run: dict[str, Any]) -> None:
    answers = console()
    assert run_cli(["run", "x.csv", "--env-file", str(write_env())]) == 0
    assert answers.prompts == []


# ════════════════════════════════════════════════════════════════════════════════════════════════
# eta eta (11 §2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def fake_router(monkeypatch: pytest.MonkeyPatch) -> dict[Carrier, FakeCarrier]:
    carriers = {c: FakeCarrier(c) for c in (Carrier.MSC, Carrier.MAERSK, Carrier.CMA)}
    carriers[Carrier.MSC].results["EBKG17208602"] = EtaResult(
        Carrier.MSC, "EBKG17208602", datetime(2026, 11, 12, 14, 0), "MSC event 'Estimated Time of Arrival'",
        containers=("MSCU1234567",))
    carriers[Carrier.MSC].results["EBKG10000003"] = NoEtaError("no ETA event yet")
    carriers[Carrier.MAERSK].results["267546627"] = EtaResult(
        Carrier.MAERSK, "267546627", datetime(2026, 12, 1), "DCSA TRANSPORT ARRI/EST at USSAV")
    carriers[Carrier.CMA].results["NAM1000017"] = EtaResult(
        Carrier.CMA, "NAM1000017", datetime(2026, 11, 3, 8, 15), "DCSA EQUIPMENT DISC/ACT at USSAV", is_actual=True)

    def build(settings: Any, token_store: TokenStore, *, clock: Any = None, http: Any = None) -> CarrierRouter:
        assert clock is not None and clock.now() == RUN_NOW
        return CarrierRouter(
            providers={Carrier.MSC: "native", Carrier.MAERSK: "native", Carrier.CMA: "native", Carrier.GRIMALDI: "off"},
            native={**carriers, Carrier.GRIMALDI: None})

    monkeypatch.setattr(carrier_router, "build_router", build)
    return carriers


def test_eta_lookup_prints_a_table_and_exits_0_with_failures(
        fake_router: dict[Carrier, FakeCarrier], fixed_now: datetime, write_env: Callable[..., Path],
        capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["eta", "EBKG17208602", "267546627", "NAM1000017", "NAM1234567", "EBKG10000003", "XYZ", "ebkg17208602",
            "--env-file", str(write_env())]
    assert run_cli(argv) == 0
    out = capsys.readouterr().out
    assert "Window cutoff: 2026-11-16 09:30 (6 US business days from now)" in out
    rows = {line.split()[0]: line for line in out.splitlines() if line and not line.startswith(("Window", "BOOKING"))}
    assert re.search(r"^EBKG17208602\s+MSC\s+2026-11-12 14:00\s+yes\s+no\s+MSC event", rows["EBKG17208602"])
    assert re.search(r"^267546627\s+Maersk\s+2026-12-01\s+no\s+no\s+DCSA TRANSPORT", rows["267546627"])
    assert re.search(r"^NAM1000017\s+CMA\s+2026-11-03 08:15\s+yes\s+yes\s+DCSA EQUIPMENT", rows["NAM1000017"])
    assert "CarrierLookupError: no record of booking NAM1234567" in rows["NAM1234567"]
    assert "NoEtaError: no ETA event yet" in rows["EBKG10000003"]
    assert "UnsupportedCarrierError: No carrier pattern matched booking XYZ" in rows["XYZ"]
    assert "4 found, 3 failed." in out
    assert fake_router[Carrier.MSC].get_eta_calls.count("EBKG17208602") == 1    # router memo (10 §6)


def test_eta_lookup_json(fake_router: dict[Carrier, FakeCarrier], fixed_now: datetime,
                         write_env: Callable[..., Path], capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["eta", "EBKG17208602", "XYZ", "--json", "--env-file", str(write_env())]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cutoff"] == "2026-11-16T09:30:00" and payload["window_business_days"] == 6
    found, failed = payload["results"]
    assert found == {"booking": "EBKG17208602", "carrier": "MSC", "provider": "native", "eta": "2026-11-12T14:00:00",
                     "source": "MSC event 'Estimated Time of Arrival'", "is_actual": False, "in_window": True,
                     "error": None, "containers": ["MSCU1234567"]}
    assert failed["carrier"] is None and failed["error"].startswith("UnsupportedCarrierError: ")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Probes with mocked clients (11 §2: output saved redacted, exit codes)
# ════════════════════════════════════════════════════════════════════════════════════════════════
VSHIP_PROBE = {
    "service": "vship", "booking": "EBKG17208602", "raw": {"login": {"password": "hunter2", "userName": "ops"}},
    "token": {"cached": True, "fresh": True, "expires_at": "2026-11-06T11:00:00", "seconds_left": 3000},
    "candidates": [{"bookingId": "72892", "bookingNo": "EBKG17208602", "match_rank": [0, 0]}],
    "booking_id": "72892", "customer_name": "ACME Imports",
    "comments": [{"comment": "ARRIVED ON 10/20. NOTIF #1", "createdDate": "2026-10-20T10:00:00"}],
    "notif_status": {"count": 1, "have_to_do": True}, "tr_swb": False, "error": None,
}


def test_probe_vship_saves_redacted_output(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path],
                                           tmp_path: Path, fixed_now: datetime,
                                           capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeProbeClient(VSHIP_PROBE)
    built: list[bool] = []

    def build(settings: Any, token_store: TokenStore, *, read_only: bool = False, http: Any = None) -> FakeProbeClient:
        built.append(read_only)
        return client

    monkeypatch.setattr(vship_client, "build_vship", build)
    assert run_cli(["probe", "vship", "--booking", "EBKG17208602", "--env-file", str(write_env())]) == 0
    assert built == [True] and client.closed == 1
    assert client.calls == [("EBKG17208602",)] and client.kwargs == [{"run_now": RUN_NOW}]
    saved = tmp_path / "state" / "probe" / "vship_EBKG17208602.json"      # 05 §10 step 4 file name
    text = saved.read_text(encoding="utf-8")
    assert "hunter2" not in text and json.loads(text)["raw"]["login"]["password"] == "***"
    out = capsys.readouterr().out
    assert "hunter2" not in out
    assert "Chosen bookingId: 72892 — customer: ACME Imports" in out
    assert "Notification status: count 1, have to do: True" in out and "Result: OK" in out


def test_probe_vship_error_exits_1_and_not_configured_exits_2(monkeypatch: pytest.MonkeyPatch,
                                                                write_env: Callable[..., Path]) -> None:
    client = FakeProbeClient({**VSHIP_PROBE, "error": "vship: credentials/token rejected (HTTP 403)"})
    monkeypatch.setattr(vship_client, "build_vship", lambda *a, **k: client)
    assert run_cli(["probe", "vship", "--booking", "EBKG17208602", "--env-file", str(write_env())]) == 1
    env = write_env(VSHIP_PASSWORD=None)
    assert run_cli(["probe", "vship", "--booking", "EBKG17208602", "--env-file", str(env)]) == 2
    assert len(client.calls) == 1


MSC_PROBE = {"service": "msc", "mode": "mymsc", "booking": "EBKG17208602",
             "requests": [{"method": "POST", "url": "https://services.mymsc.com/tracking/graphql", "status": 200,
                           "elapsed_ms": 120}],
             "raw": {"data": {"authorization": "Bearer abc.def.ghi"}},
             "eta": {"carrier": "MSC", "booking": "EBKG17208602", "eta": "2026-11-16T00:00:00",
                     "source": "MSC event 'Estimated Time of Arrival'", "is_actual": False,
                     "from_transshipment_rule": False, "containers": [], "details": {}},
             "error": None,
             "bridge": {"mode": "mymsc", "has_token": True, "source": "manual", "expires_at": 1893456000.0,
                        "seconds_left": 1500, "expired": False, "jwt_claims": {"iss": "https://mymsc.example"}}}


def test_probe_msc_bookings(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path], tmp_path: Path,
                            fixed_now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeProbeClient(lambda booking: {**MSC_PROBE, "booking": booking,
                                              "error": None if booking == "EBKG17208602" else "token rejected"})
    monkeypatch.setattr(msc, "build_client", lambda settings, store, *, clock=None, http=None: client)
    env = str(write_env())
    assert run_cli(["probe", "msc", "--booking", "ebkg17208602", "--env-file", env]) == 0
    assert client.calls == [("EBKG17208602",)] and client.closed == 1
    saved = tmp_path / "state" / "probe" / "msc_EBKG17208602_20261105_093000.json"
    assert "abc.def.ghi" not in saved.read_text(encoding="utf-8")
    out = capsys.readouterr().out
    assert "ETA 2026-11-16T00:00:00 | source: MSC event 'Estimated Time of Arrival'" in out
    assert "POST https://services.mymsc.com/tracking/graphql -> 200 (120 ms)" in out
    assert run_cli(["probe", "msc", "--booking", "EBKG17208602", "--booking", "EBKG99999999", "--env-file", env]) == 1


def test_probe_msc_sandbox_saves_only_with_save_raw(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path],
                                                    tmp_path: Path, fixed_now: datetime) -> None:
    result = {**MSC_PROBE, "mode": "api", "booking": msc.SANDBOX_PROBE_BOOKING, "bridge": None,
              "api": {"mode": "api", "host": "portal.api.msc.com", "sandbox": True, "has_key": False},
              "event_count": 12, "sandbox": True, "expected_eta": "2022-12-26", "matches_expected": True}
    calls: list[dict[str, Any]] = []

    async def fake_sandbox(token_store: TokenStore, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return copy.deepcopy(result)

    monkeypatch.setattr(msc, "probe_sandbox", fake_sandbox)
    env = str(write_env())
    assert run_cli(["probe", "msc", "--sandbox", "--env-file", env]) == 0
    assert probe_files(tmp_path) == []
    assert run_cli(["probe", "msc", "--sandbox", "--save-raw", "--env-file", env]) == 0
    assert [p.name for p in probe_files(tmp_path)] == ["msc_EBKG00000000_20261105_093000.json"]
    result["matches_expected"] = False
    assert run_cli(["probe", "msc", "--sandbox", "--env-file", env]) == 1
    assert len(calls) == 3


def test_probe_msc_compare_exit_1_on_diff(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path],
                                          tmp_path: Path, fixed_now: datetime,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    side = {"outcome": "2026-11-16", "eta": "2026-11-16T00:00:00", "source": "MSC event", "customs_hold": False,
            "rescue": False, "error": None}
    report: dict[str, Any] = {"rows": [{"booking": "EBKG17208602", "mymsc": side, "api": {**side, "outcome": "2026-11-17"},
                                        "match": "DIFF"}], "matches": 0, "diffs": 1, "any_diff": True}
    seen: list[list[str]] = []

    async def fake_compare(settings: Any, token_store: TokenStore, bookings: list[str], **kwargs: Any) -> dict[str, Any]:
        seen.append(list(bookings))
        if bookings == ["EBKG00000001"]:
            raise ConfigError(msc.NO_TOKEN_MESSAGE)
        return copy.deepcopy(report)

    monkeypatch.setattr(msc, "compare_modes", fake_compare)
    env = str(write_env())
    assert run_cli(["probe", "msc", "--compare", "--booking", "ebkg17208602", "--env-file", env]) == 1
    out = capsys.readouterr().out
    assert "DIFF" in out and "0 MATCH, 1 DIFF" in out
    assert [p.name for p in probe_files(tmp_path)] == ["msc_compare_20261105_093000.json"]
    report.update(rows=[{**report["rows"][0], "api": side, "match": "MATCH"}], matches=1, diffs=0, any_diff=False)
    assert run_cli(["probe", "msc", "--compare", "--booking", "EBKG17208602", "--env-file", env]) == 0
    assert run_cli(["probe", "msc", "--compare", "--booking", "EBKG00000001", "--env-file", env]) == 2
    assert seen[0] == ["EBKG17208602"]


def test_probe_msc_token_info_needs_no_network(write_env: Callable[..., Path], tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    env = write_env()
    assert run_cli(["probe", "msc", "--token-info", "--env-file", str(env)]) == 1
    assert "no MSC access token stored" in capsys.readouterr().out
    token = _jwt(1800)
    msc.store_manual_token(msc.MscSettings(mode="mymsc"), TokenStore(tmp_path / "state" / "tokens.json"), token)
    assert run_cli(["probe", "msc", "--token-info", "--env-file", str(env)]) == 0
    out = capsys.readouterr().out
    assert "Token bridge: valid" in out and "iss https://mymsc.example" in out
    assert token not in out and "user-secret-subject" not in out
    assert probe_files(tmp_path) == []


MAERSK_PROBE = {"carrier": "Maersk", "booking": "267546627",
                "config": {"base_url": "https://api.maersk.com", "token_url": "https://api.maersk.com/token",
                           "api_version": None, "eta_target": "final", "policy": "first", "date_only": True,
                           "page_limit": 100},
                "token": {"ok": True, "cached": False, "expires_in_s": 7199, "scope": "tnt", "error": None},
                "requests": [{"page": 1, "status": 200, "events": 3, "elapsed_ms": 80, "current_page": None,
                              "next_page": None, "url": "https://api.maersk.com/events"}],
                "counts": {"TRANSPORT/ARRI/EST": 2}, "raw": [{"eventID": "1"}],
                "eta": {"carrier": "Maersk", "booking": "267546627", "eta": "2026-11-20T00:00:00",
                        "source": "DCSA TRANSPORT ARRI/EST at USSAV", "is_actual": False,
                        "from_transshipment_rule": False, "containers": [], "details": {}},
                "alternatives": {"final": {"eta": "2026-11-20T08:00:00", "basis": "final destination"}, "pod": None},
                "explain": ["POD USSAV"], "error": None}


def test_probe_maersk(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path], tmp_path: Path,
                      fixed_now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    client = FakeProbeClient(MAERSK_PROBE)
    built: list[Any] = []

    def build(settings: Any, store: TokenStore, *, clock: Any = None, http: Any = None) -> FakeProbeClient:
        built.append(clock.now())
        return client

    monkeypatch.setattr(maersk, "build_client", build)
    assert run_cli(["probe", "maersk", "--booking", "267546627", "--env-file", str(write_env())]) == 2   # no keys
    assert built == []
    env = str(write_env(MAERSK_CLIENT_ID="consumer-key", MAERSK_CLIENT_SECRET="maersk-secret"))
    assert run_cli(["probe", "maersk", "--booking", "267546627", "--env-file", env]) == 0
    assert built == [RUN_NOW] and client.closed == 1
    out = capsys.readouterr().out
    assert "Access token: OK (cached no, expires in 7199 s, scope tnt)" in out
    assert "Event counts: TRANSPORT/ARRI/EST 2" in out and "Alternative (final): 2026-11-20T08:00:00" in out
    saved = json.loads((tmp_path / "state" / "probe" / "maersk_267546627_20261105_093000.json").read_text("utf-8"))
    assert saved["token"] == "***" and saved["counts"] == {"TRANSPORT/ARRI/EST": 2}
    client.result = {**MAERSK_PROBE, "error": "Maersk API authentication failed: HTTP 400"}
    assert run_cli(["probe", "maersk", "--booking", "267546627", "--env-file", env]) == 1


def test_probe_cma_prints_the_billable_call_count_first(monkeypatch: pytest.MonkeyPatch,
                                                        write_env: Callable[..., Path], tmp_path: Path,
                                                        fixed_now: datetime,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    printed_before: list[str] = []
    client = FakeProbeClient(
        lambda booking: {"carrier": "CMA", "booking": booking,
                         "info": {"auth_mode": "apikey", "base_url": "https://apis.cma-cgm.net", "lookup_mode": "reference",
                                  "page_limit": 500, "behalf_of_set": False},
                         "requests": [{"method": "GET", "url": "https://apis.cma-cgm.net/events/" + booking,
                                       "status": 200, "elapsed_ms": 50, "request_id": "rid-1", "endpoint": "reference"}],
                         "raw": {"reference": [], "dcsa_query": []}, "eta": "NoEtaError: no ETA",
                         "analysis": {"reference": {"event_count": 0, "counts": {}, "pod": None,
                                                    "shipment_location_types": []},
                                      "dcsa_query": {"error": "CarrierLookupError: HTTP 404"},
                                      "endpoints_agree": False},
                         "error": None},
        on_probe=lambda: printed_before.append(capsys.readouterr().out))
    monkeypatch.setattr(cma, "build_client", lambda settings, store, *, clock=None, http=None: client)
    env = str(write_env(CMA_API_KEY="cma-key"))
    assert run_cli(["probe", "cma", "--booking", "NAM1234567", "--booking", "NAM7654321", "--env-file", env]) == 0
    expected = (f"at least {cma.PROBE_MIN_BILLABLE_CALLS} billable API calls per booking, more if CMA returns "
                f"several pages: 2 booking(s), at least {2 * cma.PROBE_MIN_BILLABLE_CALLS} billable call(s)")
    assert expected in printed_before[0]
    out = capsys.readouterr().out
    assert "Endpoints agree: no" in out and "request id rid-1" in out
    assert len(probe_files(tmp_path, "cma_*.json")) == 2
    assert run_cli(["probe", "cma", "--booking", "NAM1234567", "--env-file", str(write_env())]) == 2   # no key


def test_probe_aggregator_account_and_booking(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path],
                                              tmp_path: Path, fixed_now: datetime,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    def result(carrier: Any, booking: str | None) -> dict[str, Any]:
        out: dict[str, Any] = {"provider": "shipsgo", "service": "ShipsGo", "base_url": "https://api.shipsgo.com/v2",
                               "account_ok": True, "carriers": [{"scac": "MSCU", "name": "MSC", "status": "ACTIVE"}],
                               "scac_check": {"MSC": {"scac": "MSCU", "active": True}}, "requests": [], "raw": None,
                               "eta": None, "error": None}
        if booking:
            out.update(booking=booking, carrier="MSC", scac="MSCU", tracking_id=None,
                       note="not tracked yet", account_ok=booking != "EBKG00000000")
        return out

    client = FakeProbeClient(result)
    monkeypatch.setattr(aggregator_carrier, "build_client", lambda settings, store, *, clock=None, http=None: client)
    env = str(write_env(AGG_API_TOKEN="shipsgo-token"))
    assert run_cli(["probe", "aggregator", "--env-file", env]) == 0
    assert client.calls == [(None, None)]
    assert [p.name for p in probe_files(tmp_path)] == ["aggregator_account_20261105_093000.json"]
    out = capsys.readouterr().out
    assert "Account: OK" in out and "Active carriers: MSC (MSCU)" in out and "MSC MSCU (active)" in out
    assert run_cli(["probe", "aggregator", "--booking", "EBKG17208602", "--env-file", env]) == 0
    assert client.calls[-1] == (None, "EBKG17208602")
    assert run_cli(["probe", "aggregator", "--booking", "EBKG00000000", "--env-file", env]) == 1
    assert run_cli(["probe", "aggregator", "--env-file", str(write_env())]) == 2     # AGG_API_TOKEN missing


def test_probe_all_runs_every_configured_read_only_probe(monkeypatch: pytest.MonkeyPatch,
                                                         write_env: Callable[..., Path], fixed_now: datetime,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    ran: list[str] = []

    async def fake_qbo(ctx: Any) -> int:
        ran.append("qbo")
        return 0

    monkeypatch.setattr(cli, "_qbo_read_probe", fake_qbo)
    vship = FakeProbeClient(VSHIP_PROBE)
    monkeypatch.setattr(vship_client, "build_vship", lambda *a, **k: vship)
    msc_client = FakeProbeClient({**MSC_PROBE, "error": "MSC lookups stopped: no token"})
    monkeypatch.setattr(msc, "build_client", lambda *a, **k: msc_client)
    argv = ["probe", "all", "--booking", "EBKG17208602", "--booking", "267546627", "--env-file", str(write_env())]
    assert run_cli(argv) == 1
    out = capsys.readouterr().out
    assert ran == ["qbo"]
    assert vship.calls == [("EBKG17208602",), ("267546627",)]
    assert msc_client.calls == [("EBKG17208602",)]
    assert "qbo        OK" in out and "vship      OK" in out and "msc        NOT WORKING (exit 1)" in out
    assert "maersk     skipped — not configured (set MAERSK_CLIENT_ID, MAERSK_CLIENT_SECRET)" in out
    assert "cma        skipped — not configured" in out
    assert "aggregator skipped — not configured (set AGG_API_TOKEN)" in out
    assert run_cli(["probe", "all", "--env-file", str(write_env())]) == 0
    out = capsys.readouterr().out
    assert "vship      skipped — needs --booking B" in out and "msc        skipped — needs --booking EBKG…" in out


# ════════════════════════════════════════════════════════════════════════════════════════════════
# probe qbo (04 §10, 04b §10) against the in-memory QuickBooks
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_probe_qbo_read_only(quickbooks: Callable[..., FakeQuickBooks], write_env: Callable[..., Path],
                             tmp_path: Path, fixed_now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=False)
    env = write_env()
    assert run_cli(["probe", "qbo", "--invoice", "EBKG12345678", "--env-file", str(env)]) == 0
    captured = capsys.readouterr()
    out = captured.out
    assert "QuickBooks Online — production, realm 1234567890" in out
    assert "Access refresh: OK — connection valid until" in out
    assert "Company: Example Forwarding LLC" in out
    assert f"Invoice e-mail subject: {ORIGINAL_SUBJECT!r}" in out
    assert "SalesFormsPrefs.ETransactionAttachPDF: True" in out and "SalesFormsPrefs.CustomTxnNumbers: True" in out
    assert "verified for this company: no" in out
    assert "Invoice #EBKG12345678 (Id 190613): customer Acme Imports LLC, balance 2450.0 of 2450.0, ETA 2026-11-02" in out
    assert qb.invoice_posts == [] and qb.prefs_posts == []                       # read-only
    saved = probe_files(tmp_path)
    assert [p.name for p in saved] == ["qbo_1234567890_20261105_093000.json"]
    data = json.loads(saved[0].read_text(encoding="utf-8"))
    assert data["company"] == {"name": "Example Forwarding LLC"} and data["raw"]["preferences"]["Id"] == "1"
    assert_no_secrets(out, captured.err, saved[0].read_text(encoding="utf-8"), log_text(tmp_path))


def test_probe_qbo_read_only_auth_failure_exits_2(quickbooks: Callable[..., FakeQuickBooks],
                                                  write_env: Callable[..., Path],
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=False)
    qb.token_status, qb.token_body = 400, {"error": "invalid_grant"}
    assert run_cli(["probe", "qbo", "--env-file", str(write_env())]) == 2
    assert "invalid_grant" in capsys.readouterr().out
    assert run_cli(["probe", "qbo", "--env-file", str(write_env(QB_CLIENT_ID=None))]) == 2   # not configured


def _eta_posts(qb: FakeQuickBooks) -> list[str]:
    return [next(e["DateValue"] for e in body["CustomField"] if e["Name"] == "ETA") for body in qb.invoice_posts]


def test_probe_qbo_write_test_sets_verifies_and_restores(quickbooks: Callable[..., FakeQuickBooks],
                                                         write_env: Callable[..., Path], tmp_path: Path,
                                                         console: Callable[..., Console], fixed_now: datetime,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=True)
    answers = console("y")
    env = write_env(QB_SANDBOX="true")
    assert run_cli(["probe", "qbo", "--write-test", "--invoice", "EBKG12345678", "--env-file", str(env)]) == 0
    captured = capsys.readouterr()
    assert answers.prompts == ["Change invoice #EBKG12345678 in QuickBooks now? [y/N] "]
    assert _eta_posts(qb) == ["2026-11-03", "2026-11-02"]
    assert all("DocNumber" not in body for body in qb.invoice_posts)          # never touches DocNumber
    assert qb.eta_of() == "2026-11-02"
    assert {"DefinitionId": "1", "Name": "Container No.", "Type": "StringType", "StringValue": "MSCU1234567"} in \
        qb.invoices["190613"]["CustomField"]
    assert "set ETA 2026-11-03: PASS" in captured.out and "restore ETA 2026-11-02: PASS" in captured.out
    assert "Result: PASS" in captured.out and '"CustomField"' in captured.out    # request bodies reported
    saved = json.loads(probe_files(tmp_path)[0].read_text(encoding="utf-8"))
    assert saved["result"] == "PASS" and [s["ok"] for s in saved["steps"]] == [True, True]
    assert [e["method"] for e in saved["exchanges"]].count("POST") == 2
    assert saved["exchanges"][-1]["response_body"]["Invoice"]["Id"] == "190613"
    assert_no_secrets(captured.out, captured.err, json.dumps(saved))
    with run_lock(tmp_path / "state"):      # the run lock was released
        pass


def test_probe_qbo_write_test_reports_an_ignored_write(quickbooks: Callable[..., FakeQuickBooks],
                                                       write_env: Callable[..., Path], console: Callable[..., Console],
                                                       fixed_now: datetime,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=True)
    qb.ignore_eta_writes = True                 # 04 §8: QuickBooks accepts the update but keeps the old value
    console("y")
    argv = ["probe", "qbo", "--write-test", "--invoice", "EBKG12345678", "--env-file", str(write_env(QB_SANDBOX="1"))]
    assert run_cli(argv) == 1
    out = capsys.readouterr().out
    assert "set ETA 2026-11-03: FAIL" in out and "did not change" in out
    assert "restore ETA 2026-11-02: PASS" in out and "04 §8.1" in out and "Result: FAIL" in out
    assert _eta_posts(qb) == ["2026-11-03"]     # nothing to restore: the value never changed


def test_probe_qbo_write_test_restore_failure_is_reported_loudly(
        quickbooks: Callable[..., FakeQuickBooks], write_env: Callable[..., Path], console: Callable[..., Console],
        fixed_now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=True)
    qb.reject_invoice_post = 2                  # the set works, the restore write is rejected
    console("y")
    argv = ["probe", "qbo", "--write-test", "--invoice", "EBKG12345678", "--env-file", str(write_env(QB_SANDBOX="1"))]
    assert run_cli(argv) == 1
    out = capsys.readouterr().out
    assert "set ETA 2026-11-03: PASS" in out and "restore ETA 2026-11-02: FAIL" in out
    assert "RESTORE FAILED: set the ETA of invoice #EBKG12345678 back to 2026-11-02 in QuickBooks by hand." in out
    assert qb.eta_of() == "2026-11-03"


def test_probe_qbo_write_probes_are_guarded(quickbooks: Callable[..., FakeQuickBooks],
                                            write_env: Callable[..., Path], tmp_path: Path,
                                            console: Callable[..., Console],
                                            capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=False)
    production = str(write_env())
    write_test = ["probe", "qbo", "--write-test", "--invoice", "EBKG12345678"]
    console("y")
    assert run_cli([*write_test, "--env-file", production]) == 3               # production needs --yes
    assert "PRODUCTION QuickBooks company" in capsys.readouterr().out
    assert len(qb.router.calls) == 0
    console("y", tty=False)
    assert run_cli([*write_test, "--yes", "--env-file", production]) == 3      # needs a terminal
    assert "not a terminal" in capsys.readouterr().out
    assert len(qb.router.calls) == 0
    answers = console("n")
    assert run_cli([*write_test, "--yes", "--env-file", production]) == 3      # declined
    assert "Aborted: nothing was changed." in capsys.readouterr().out
    assert len(answers.prompts) == 1 and qb.invoice_posts == []
    console("y")
    with run_lock(tmp_path / "state"):                                          # another run holds the lock
        assert run_cli([*write_test, "--yes", "--env-file", production]) == 2
    assert "Another ETAutomationsAPI run is in progress" in capsys.readouterr().err
    answers = console("y")
    assert run_cli(["probe", "qbo", "--write-test", "--invoice", "EBKG23456789 A", "--yes", "--env-file",
                     production]) == 1                                          # empty ETA: refused, no prompt
    assert "needs an invoice whose ETA is filled in" in capsys.readouterr().out
    assert answers.prompts == [] and qb.invoice_posts == []


def test_probe_qbo_prefs_roundtrip_passes(quickbooks: Callable[..., FakeQuickBooks], write_env: Callable[..., Path],
                                          tmp_path: Path, console: Callable[..., Console], fixed_now: datetime,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=True)
    answers = console("y")
    assert run_cli(["probe", "qbo", "--prefs-roundtrip", "--env-file", str(write_env(QB_SANDBOX="true"))]) == 0
    out = capsys.readouterr().out
    assert answers.prompts == ["Write the QuickBooks company Preferences now? [y/N] "]
    subjects = [body["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] for body in qb.prefs_posts]
    assert subjects[0] == ORIGINAL_SUBJECT and subjects[2] == ORIGINAL_SUBJECT
    assert subjects[1].startswith("ETA probe 20261105-093000 — Invoice for Booking no. ")
    assert all(body.get("sparse") is not True for body in qb.prefs_posts)     # full mode (default)
    assert qb.subject == ORIGINAL_SUBJECT
    assert not (tmp_path / "state" / RECOVERY_FILE_NAME).exists()
    assert "1. write back unchanged: PASS" in out and "2. write only a probe subject: PASS" in out
    assert "3. restore the original subject: PASS" in out and "Result: PASS" in out
    saved = json.loads(probe_files(tmp_path)[0].read_text(encoding="utf-8"))
    assert set(saved["snapshots"]) == {"start", "after_unchanged_write", "after_probe_subject", "after_restore"}
    assert saved["result"] == "PASS"


def test_probe_qbo_prefs_roundtrip_sparse_mode(quickbooks: Callable[..., FakeQuickBooks],
                                               write_env: Callable[..., Path], console: Callable[..., Console],
                                               fixed_now: datetime) -> None:
    qb = quickbooks(sandbox=True)
    console("y")
    env = write_env(QB_SANDBOX="true", QB_PREFS_UPDATE_MODE="sparse")
    assert run_cli(["probe", "qbo", "--prefs-roundtrip", "--env-file", str(env)]) == 0
    assert len(qb.prefs_posts) == 3
    assert all(body["sparse"] is True and set(body) == {"Id", "SyncToken", "sparse", "EmailMessagesPrefs"}
               for body in qb.prefs_posts)
    assert qb.subject == ORIGINAL_SUBJECT


def test_probe_qbo_prefs_roundtrip_stops_when_the_write_back_is_lossy(
        quickbooks: Callable[..., FakeQuickBooks], write_env: Callable[..., Path], tmp_path: Path,
        console: Callable[..., Console], fixed_now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=True)
    qb.lossy_prefs_key = "SalesEmailBcc"
    console("y")
    assert run_cli(["probe", "qbo", "--prefs-roundtrip", "--env-file", str(write_env(QB_SANDBOX="true"))]) == 1
    out = capsys.readouterr().out
    assert len(qb.prefs_posts) == 1                            # no probe subject after a lossy write-back
    assert "1. write back unchanged: FAIL" in out and "SalesFormsPrefs.SalesEmailBcc" in out
    assert "use QB_EMAIL_STRATEGY=graph" in out
    assert qb.subject == ORIGINAL_SUBJECT and not (tmp_path / "state" / RECOVERY_FILE_NAME).exists()
    # CLI-03: the lossy write-back cannot be undone by the probe — it says which settings to restore by hand
    assert "cannot undo" in out and "restore them by hand" in out
    assert "    SalesFormsPrefs.SalesEmailBcc: set it back to {'Address': 'accounting@example.com'}" in out


def test_probe_qbo_prefs_roundtrip_keeps_the_recovery_file_when_the_restore_fails(
        quickbooks: Callable[..., FakeQuickBooks], write_env: Callable[..., Path], tmp_path: Path,
        console: Callable[..., Console], fixed_now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=True)
    qb.reject_prefs_posts_from = 3              # unchanged + probe subject work; the restore is rejected
    env = str(write_env(QB_SANDBOX="true"))
    console("y")
    assert run_cli(["probe", "qbo", "--prefs-roundtrip", "--env-file", env]) == 1
    out = capsys.readouterr().out
    assert "3. restore the original subject: FAIL" in out
    assert f"set the invoice e-mail subject back to: '{ORIGINAL_SUBJECT}'" in out
    recovery = tmp_path / "state" / RECOVERY_FILE_NAME
    record = json.loads(recovery.read_text(encoding="utf-8"))
    assert record["realm_id"] == REALM and record["original_subject"] == ORIGINAL_SUBJECT
    assert qb.subject == record["swapped_subject"] != ORIGINAL_SUBJECT
    qb.reject_prefs_posts_from = None           # --restore-template repairs it from the recovery file
    console("y")
    assert run_cli(["probe", "qbo", "--restore-template", "--env-file", env]) == 0
    assert qb.subject == ORIGINAL_SUBJECT and not recovery.exists()


def test_probe_qbo_prefs_roundtrip_refuses_with_a_pending_recovery_file(
        quickbooks: Callable[..., FakeQuickBooks], write_env: Callable[..., Path], tmp_path: Path,
        console: Callable[..., Console], capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / RECOVERY_FILE_NAME).write_text("{}", encoding="utf-8")
    answers = console("y")
    assert run_cli(["probe", "qbo", "--prefs-roundtrip", "--env-file", str(write_env(QB_SANDBOX="true"))]) == 1
    assert "--restore-template" in capsys.readouterr().out
    assert qb.prefs_posts == [] and answers.prompts == []


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """QboClient.send_invoice replaced: records the subject and the client settings the send test used."""
    records: list[dict[str, Any]] = []

    async def send_invoice(self: QboClient, inv: Any, *, subject: str) -> SendReceipt:
        records.append({"doc": inv.doc_number, "subject": subject, "read_only": self.read_only,
                        "require_verified": self.settings.email_require_verified})
        return SendReceipt(strategy=self.settings.email_strategy, invoice_id=inv.id, doc_number=inv.doc_number,
                           subject=subject, to=inv.recipients, sent_at=datetime(2026, 11, 5, 9, 31),
                           qbo_delivery_time="2026-11-05T06:31:00-08:00")

    monkeypatch.setattr(QboClient, "send_invoice", send_invoice)
    return records


def test_probe_qbo_send_test_records_the_human_verification(
        quickbooks: Callable[..., FakeQuickBooks], sent: list[dict[str, Any]], write_env: Callable[..., Path],
        tmp_path: Path, console: Callable[..., Console], fixed_now: datetime,
        capsys: pytest.CaptureFixture[str]) -> None:
    quickbooks(sandbox=True)
    answers = console("y", "y")
    env = str(write_env(QB_SANDBOX="true"))
    assert run_cli(["probe", "qbo", "--send-test", "--invoice", "EBKG12345678", "--env-file", env]) == 0
    subject = "ETA probe 20261105-093000 — Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/02.****"
    assert sent == [{"doc": "EBKG12345678", "subject": subject, "read_only": False, "require_verified": False}]
    assert answers.prompts[0] == "Send this one probe e-mail now? [y/N] "
    assert answers.prompts[1].startswith("Check ap@acme.example, ops@acme.example. Did an e-mail arrive with EXACTLY")
    records = load_verifications(tmp_path / "state")
    assert records[-1] == {**records[-1], "realm_id": REALM, "strategy": "template_swap", "verified": True,
                           "subject_sent": subject, "invoice_id": "190613"}
    assert "Result: PASS" in capsys.readouterr().out
    console("y", "n")
    assert run_cli(["probe", "qbo", "--send-test", "--invoice", "EBKG12345678", "--env-file", env]) == 1
    assert load_verifications(tmp_path / "state")[-1]["verified"] is False
    assert "--no-email" in capsys.readouterr().out


def test_probe_qbo_send_test_never_emails_a_real_customer_on_production(
        quickbooks: Callable[..., FakeQuickBooks], sent: list[dict[str, Any]], write_env: Callable[..., Path],
        tmp_path: Path, console: Callable[..., Console], fixed_now: datetime,
        capsys: pytest.CaptureFixture[str]) -> None:
    quickbooks(sandbox=False)
    answers = console("y", "y")
    test_inbox = "ap@gamma.example"
    env = str(write_env(QB_TEST_SEND_TO="eta-test@internal.example"))
    assert run_cli(["probe", "qbo", "--send-test", "--invoice", "EBKG12345678", "--yes", "--env-file", env]) == 3
    assert "not exactly to QB_TEST_SEND_TO" in capsys.readouterr().out
    assert sent == [] and answers.prompts == [] and load_verifications(tmp_path / "state") == []
    env = str(write_env(QB_TEST_SEND_TO=test_inbox))
    assert run_cli(["probe", "qbo", "--send-test", "--invoice", "267546627", "--yes", "--env-file", env]) == 0
    assert sent[0]["subject"].endswith("— Invoice for Booking no. 267546627. ***ESTIMATED ARRIVING ON 11/05.****")
    assert answers.prompts[1].startswith(f"Check {test_inbox}.")


@pytest.mark.parametrize(("field", "test_send_to", "refusal"), [
    ("BillEmailCc", "ap@gamma.example", "also sends copies to real.customer@gamma.example"),
    ("BillEmailBcc", "ap@gamma.example", "also sends copies to real.customer@gamma.example"),
    (None, None, "QB_TEST_SEND_TO is not set"),
])
def test_probe_qbo_send_test_refuses_copies_and_a_missing_test_inbox_on_production(
        field: str | None, test_send_to: str | None, refusal: str, quickbooks: Callable[..., FakeQuickBooks],
        sent: list[dict[str, Any]], write_env: Callable[..., Path], tmp_path: Path, console: Callable[..., Console],
        fixed_now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    """CLI-05: on production the send test e-mails only QB_TEST_SEND_TO — a Cc/Bcc to anyone else on the test invoice,
    or no QB_TEST_SEND_TO at all, is refused before any prompt or send (stricter than 04b §10)."""
    qb = quickbooks(sandbox=False)
    if field is not None:
        qb.invoices["190801"][field] = {"Address": "real.customer@gamma.example"}   # invoice 267546627
    answers = console("y", "y")
    env = str(write_env(QB_TEST_SEND_TO=test_send_to))
    assert run_cli(["probe", "qbo", "--send-test", "--invoice", "267546627", "--yes", "--env-file", env]) == 3
    assert refusal in capsys.readouterr().out
    assert sent == [] and answers.prompts == [] and load_verifications(tmp_path / "state") == []


def test_probe_qbo_send_test_configuration_error_exits_2(quickbooks: Callable[..., FakeQuickBooks],
                                                        write_env: Callable[..., Path], tmp_path: Path,
                                                        console: Callable[..., Console], fixed_now: datetime,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    quickbooks(sandbox=True)
    console("y")
    env = str(write_env(QB_SANDBOX="true", QB_EMAIL_STRATEGY="graph"))     # graph without its EMAIL_* settings
    assert run_cli(["probe", "qbo", "--send-test", "--invoice", "EBKG12345678", "--env-file", env]) == 2
    assert "EMAIL_FROM" in capsys.readouterr().err
    assert load_verifications(tmp_path / "state") == []

def test_probe_qbo_restore_template(quickbooks: Callable[..., FakeQuickBooks], write_env: Callable[..., Path],
                                    tmp_path: Path, console: Callable[..., Console],
                                    capsys: pytest.CaptureFixture[str]) -> None:
    qb = quickbooks(sandbox=True)
    env = str(write_env(QB_SANDBOX="true"))
    answers = console("y")
    assert run_cli(["probe", "qbo", "--restore-template", "--env-file", env]) == 0
    assert "Nothing to restore" in capsys.readouterr().out and answers.prompts == []
    swapped = "Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/12. PLEASE MAKE PAYMENT ARRANGEMENT****"
    qb.prefs["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] = swapped
    recovery = tmp_path / "state" / RECOVERY_FILE_NAME
    recovery.write_text(json.dumps({"realm_id": REALM, "original_subject": ORIGINAL_SUBJECT, "original_message": None,
                                    "swapped_subject": swapped, "invoice_id": "190613",
                                    "written_at": "2026-11-05T09:00:00", "pid": 1}), encoding="utf-8")
    assert run_cli(["probe", "qbo", "--restore-template", "--env-file", env]) == 0
    assert answers.prompts == ["Restore the QuickBooks invoice e-mail subject now? [y/N] "]
    assert qb.subject == ORIGINAL_SUBJECT and not recovery.exists()
    assert "restored and the recovery file deleted" in capsys.readouterr().out


def test_qbo_fields(quickbooks: Callable[..., FakeQuickBooks], write_env: Callable[..., Path], tmp_path: Path,
                    fixed_now: datetime, capsys: pytest.CaptureFixture[str]) -> None:
    quickbooks(sandbox=False)
    env = str(write_env())
    assert run_cli(["qbo-fields", "--invoice", "EBKG12345678", "--env-file", env]) == 0
    out = capsys.readouterr().out
    assert re.search(r"1000000003\s+ETA\s+DateType\s+DateValue\s+2026-11-02\s+yes \(name\)", out)
    assert re.search(r"1\s+Container No\.\s+StringType\s+StringValue\s+MSCU1234567", out)
    for line in ("QB_ETA_DEFINITION_ID=1000000003", "QB_ETA_FIELD_NAME=ETA", "QB_ETA_FIELD_TYPE=DateType"):
        assert f"  {line}" in out
    assert [p.name for p in probe_files(tmp_path)] == ["qbo_fields_EBKG12345678_20261105_093000.json"]
    assert run_cli(["qbo-fields", "--invoice", "EBKG23456789 A", "--env-file", env]) == 1
    assert "was not found on this invoice" in capsys.readouterr().out
    assert run_cli(["qbo-fields", "--invoice", "NOPE-1", "--env-file", env]) == 1
    assert "#NOPE-1 not found in QuickBooks" in capsys.readouterr().err


# ════════════════════════════════════════════════════════════════════════════════════════════════
# qbo-auth (04 §5.4) and msc-token (06 §5.1)
# ════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def auth_flow(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def fake_flow(settings: Any, token_store: TokenStore, **kwargs: Any) -> None:
        seen.update(settings=settings, token_store=token_store, **kwargs)
        if "raise" in seen:
            raise seen["raise"]

    monkeypatch.setattr(cli, "run_authorization_code_flow", fake_flow)
    return seen


def test_qbo_auth_works_before_the_realm_id_is_known(auth_flow: dict[str, Any], write_env: Callable[..., Path],
                                                     tmp_path: Path) -> None:
    env = write_env(QB_REALM_ID=None, QB_SANDBOX="true", QB_REDIRECT_URI="https://example.com/callback")
    assert run_cli(["qbo-auth", "--listen", "--port", "9001", "--no-browser", "--env-file", str(env)]) == 0
    settings = auth_flow["settings"]
    assert settings.realm_id == "" and settings.client_id == "test-qb-client-id" and settings.sandbox is True
    assert settings.redirect_uri == "https://example.com/callback" and settings.env_file == env.absolute()
    assert (auth_flow["listen"], auth_flow["port"], auth_flow["open_browser"]) == (True, 9001, False)
    assert auth_flow["token_store"].path == tmp_path / "state" / "tokens.json"


def test_qbo_auth_with_a_configured_realm_and_errors(auth_flow: dict[str, Any], write_env: Callable[..., Path],
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    assert run_cli(["qbo-auth", "--env-file", str(write_env())]) == 0
    assert auth_flow["settings"].realm_id == REALM
    assert (auth_flow["listen"], auth_flow["port"], auth_flow["open_browser"]) == (False, 8765, True)
    auth_flow["raise"] = AuthError("state mismatch — start again")
    assert run_cli(["qbo-auth", "--env-file", str(write_env())]) == 2
    assert "state mismatch" in capsys.readouterr().err
    auth_flow.clear()
    assert run_cli(["qbo-auth", "--env-file", str(write_env(QB_REALM_ID=None, QB_CLIENT_SECRET=None))]) == 2
    assert "QB_CLIENT_SECRET" in capsys.readouterr().err and auth_flow == {}


def test_qbo_auth_holds_the_run_lock(auth_flow: dict[str, Any], write_env: Callable[..., Path], tmp_path: Path,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    """03 §14 (FND-01): `qbo-auth` writes tokens.json and .env, so it holds the run lock; a running `eta run` would
    otherwise write its older refresh token over the new authorization (04 §5.3)."""
    state = tmp_path / "state"
    state.mkdir()
    env = write_env()
    with run_lock(state):
        assert run_cli(["qbo-auth", "--env-file", str(env)]) == 2
    assert "Another ETAutomationsAPI run is in progress" in capsys.readouterr().err
    assert auth_flow == {}                                                     # the OAuth flow never started
    assert run_cli(["qbo-auth", "--env-file", str(env)]) == 0                  # control: lock free → the flow runs
    assert auth_flow["token_store"].path == state / "tokens.json"
    with run_lock(state):                                                      # and the lock was released again
        pass


def test_msc_token_is_stored_and_never_echoed(monkeypatch: pytest.MonkeyPatch, write_env: Callable[..., Path],
                                              tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    token = _jwt(1800)
    prompts: list[str] = []

    def fake_getpass(prompt: str = "Password: ") -> str:
        prompts.append(prompt)
        return f"Bearer {token}"

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    assert run_cli(["msc-token", "--env-file", str(write_env())]) == 0
    captured = capsys.readouterr()
    assert prompts == ["MSC access token: "]
    entry = TokenStore(tmp_path / "state" / "tokens.json").get("msc")
    assert entry is not None and entry["access_token"] == token and entry["source"] == "manual"
    assert token not in captured.out + captured.err and token not in log_text(tmp_path)
    assert "user-secret-subject" not in captured.out
    left = int(re.search(r"Status: valid, (\d+) s left", captured.out).group(1))  # type: ignore[union-attr]
    assert 1700 < left <= 1800
    assert "JWT claims: iss https://mymsc.example, aud tracking" in captured.out


@pytest.mark.parametrize(("pasted", "message"), [
    ("", "The MSC access token is empty"),
    ("two words", "contains spaces"),
    (_jwt(-60), "already expired"),
])
def test_msc_token_rejects_empty_spaced_and_expired_tokens(pasted: str, message: str, monkeypatch: pytest.MonkeyPatch,
                                                           write_env: Callable[..., Path],
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": pasted)
    assert run_cli(["msc-token", "--env-file", str(write_env())]) == 2
    captured = capsys.readouterr()
    assert message in captured.out + captured.err
    if pasted:
        assert pasted not in captured.out + captured.err


# ════════════════════════════════════════════════════════════════════════════════════════════════
# ui.py — the Tk file picker of `eta run` without a CSV (11 §6); tkinter itself is faked
# ════════════════════════════════════════════════════════════════════════════════════════════════
def test_is_valid_csv_file(tmp_path: Path) -> None:
    good = tmp_path / "invoices.CSV"
    good.write_text("EBKG17208602,2450\n", encoding="utf-8")
    other = tmp_path / "invoices.txt"
    other.write_text("x", encoding="utf-8")
    assert ui.is_valid_csv_file(good) == (True, "")
    assert ui.is_valid_csv_file(str(good)) == (True, "")
    assert ui.is_valid_csv_file(tmp_path / "missing.csv") == (False, f"File not found: {tmp_path / 'missing.csv'}")
    assert ui.is_valid_csv_file(tmp_path) == (False, f"Not a file: {tmp_path}")
    assert ui.is_valid_csv_file(other) == (False, f"Not a .csv file: {other}")
    assert ui.is_valid_csv_file("  ") == (False, "Please choose a .csv file.")
    assert ui.is_valid_csv_file(None) == (False, "Please choose a .csv file.")


def test_pick_input_file_without_tkinter_raises_the_no_gui_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "tkinter", None)          # `import tkinter` → ImportError
    with pytest.raises(ConfigError) as caught:
        ui.pick_input_file()
    assert str(caught.value) == NO_GUI_MESSAGE == (
        "No input file given and no GUI available — pass the CSV path: eta run <file.csv>")


class _FakeTkModule:
    """Just enough of tkinter for ui.pick_input_file(); `script(root, widgets)` plays the user inside mainloop()."""

    class TclError(Exception):
        pass

    def __init__(self, script: Callable[[Any, list[Any]], None], *, dialog_paths: list[str],
                 display: bool = True) -> None:
        self.script = script
        self.display = display
        self.widgets: list[Any] = []
        self.errors: list[tuple[str, str]] = []
        self.dialog_calls: list[dict[str, Any]] = []
        self.roots: list[Any] = []
        fake = self

        class Tk:
            def __init__(self) -> None:
                if not fake.display:
                    raise fake.TclError("no display name and no $DISPLAY environment variable")
                self.bindings: dict[str, Callable[..., None]] = {}
                self.protocols: dict[str, Callable[..., None]] = {}
                self.titles: list[str] = []
                self.destroyed = False
                fake.roots.append(self)

            def title(self, text: str) -> None:
                self.titles.append(text)

            def bind(self, sequence: str, handler: Callable[..., None]) -> None:
                self.bindings[sequence] = handler

            def protocol(self, name: str, handler: Callable[..., None]) -> None:
                self.protocols[name] = handler

            def mainloop(self) -> None:
                fake.script(self, fake.widgets)

            def destroy(self) -> None:
                self.destroyed = True

        class StringVar:
            def __init__(self, master: Any = None) -> None:
                self.value = ""

            def get(self) -> str:
                return self.value

            def set(self, value: str) -> None:
                self.value = value

        class Widget:
            def __init__(self, master: Any = None, **options: Any) -> None:
                self.options = options
                fake.widgets.append(self)

            def grid(self, **options: Any) -> None:
                return None

            def focus_set(self) -> None:
                return None

        def askopenfilename(**options: Any) -> str:
            fake.dialog_calls.append(options)
            return dialog_paths.pop(0) if dialog_paths else ""

        def showerror(title: str, message: str, **options: Any) -> None:
            fake.errors.append((title, message))

        self.Tk, self.StringVar = Tk, StringVar
        self.Label = self.Entry = self.Button = Widget
        self.filedialog = type(sys)("tkinter.filedialog")
        self.filedialog.askopenfilename = askopenfilename  # type: ignore[attr-defined]
        self.messagebox = type(sys)("tkinter.messagebox")
        self.messagebox.showerror = showerror  # type: ignore[attr-defined]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setitem(sys.modules, "tkinter", self)
        monkeypatch.setitem(sys.modules, "tkinter.filedialog", self.filedialog)
        monkeypatch.setitem(sys.modules, "tkinter.messagebox", self.messagebox)


def _button(widgets: list[Any], text: str) -> Any:
    return next(w for w in widgets if w.options.get("text") == text)


def test_pick_input_file_browse_validate_submit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    good = tmp_path / "invoices.csv"
    good.write_text("EBKG17208602,2450\n", encoding="utf-8")

    def user(root: Any, widgets: list[Any]) -> None:
        _button(widgets, "Browse...").options["command"]()      # picks a .txt file first
        root.bindings["<Return>"]()                              # Enter submits → invalid, stays open
        assert not root.destroyed
        _button(widgets, "Browse...").options["command"]()      # then the .csv
        _button(widgets, "Submit").options["command"]()

    fake = _FakeTkModule(user, dialog_paths=[str(tmp_path / "notes.txt"), str(good)])
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    fake.install(monkeypatch)
    assert ui.pick_input_file() == good
    root = fake.roots[0]
    assert root.titles == ["ETA Tool"] and root.destroyed
    assert any(w.options.get("text") == "FileName" for w in fake.widgets)
    assert any(w.options.get("width") == 42 for w in fake.widgets)
    assert {"Browse...", "Cancel", "Submit"} <= {w.options.get("text") for w in fake.widgets}
    assert fake.errors == [("Invalid file", f"Not a .csv file: {tmp_path / 'notes.txt'}")]
    assert fake.dialog_calls[0]["title"] == "Select .csv file"
    assert fake.dialog_calls[0]["filetypes"] == [("CSV Files", "*.csv")]


@pytest.mark.parametrize("how", ["escape", "cancel-button", "window-close"])
def test_pick_input_file_cancel_returns_none(how: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def user(root: Any, widgets: list[Any]) -> None:
        if how == "escape":
            root.bindings["<Escape>"]()
        elif how == "cancel-button":
            _button(widgets, "Cancel").options["command"]()
        else:
            root.protocols["WM_DELETE_WINDOW"]()

    fake = _FakeTkModule(user, dialog_paths=[])
    fake.install(monkeypatch)
    assert ui.pick_input_file() is None
    assert fake.roots[0].destroyed


def test_pick_input_file_without_a_display_raises_the_no_gui_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeTkModule(lambda root, widgets: None, dialog_paths=[], display=False)
    fake.install(monkeypatch)
    with pytest.raises(ConfigError, match="no GUI available"):
        ui.pick_input_file()
