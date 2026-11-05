"""Shared pytest fixtures for every stage (12-testing §1–§2).

- Autouse network guard: any socket connection to a non-loopback address, and any DNS lookup of a
  non-local host name, raises NetworkAccessBlocked. respx mocks httpx above the socket layer, so mocked
  requests are unaffected; loopback stays allowed. `*_proxy` variables are removed for each test so an
  un-mocked request cannot escape through a proxy listening on loopback.
- Autouse environment isolation: os.environ is restored after every test (load_dotenv() mutates it), and
  the default ETA_STATE_DIR location (XDG_STATE_HOME / LOCALAPPDATA) points into a per-test temp dir.
- fixed_clock, state_dir, output_dir, token_store, make_settings, fixtures_dir, load_fixture, http_sleeps.
- Tests marked @pytest.mark.live are skipped unless ETA_LIVE_TESTS=1 (12-testing §5); only those may
  use the network.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
from collections.abc import Callable, Iterator, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from eta_automations.clock import FixedClock
from eta_automations.config import Settings, load_settings
from eta_automations.token_store import TokenStore

FIXTURES_DIR = Path(__file__).parent / "fixtures"
RUN_NOW = datetime(2026, 11, 5, 9, 30)  # Thu; cutoff Mon 2026-11-16 09:30 (01 §5)

# Fake credentials so Settings.qbo and Settings.vship are configured by default (overridable per test).
DEFAULT_TEST_ENV: dict[str, str] = {
    "QB_CLIENT_ID": "test-qb-client-id",
    "QB_CLIENT_SECRET": "test-qb-client-secret",
    "QB_REALM_ID": "1234567890",
    "QB_REFRESH_TOKEN": "test-qb-refresh-token",
    "VSHIP_USER_NAME": "ops@example.com",
    "VSHIP_PASSWORD": "test-vship-password",
}

_LOCAL_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


class NetworkAccessBlocked(RuntimeError):
    """A test tried to reach the network (12-testing §1: tests never touch the network)."""


def _host_text(host: object) -> str:
    if isinstance(host, (bytes, bytearray)):
        return bytes(host).decode("ascii", "replace")
    return "" if host is None else str(host)


def _is_loopback(host: object) -> bool:
    text = _host_text(host).strip().strip("[]").lower()
    if text in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(text.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def _is_ip_literal(host: object) -> bool:
    try:
        ipaddress.ip_address(_host_text(host).strip().strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _check_address(address: object) -> None:
    if isinstance(address, (str, bytes, bytearray)):  # AF_UNIX path: not network
        return
    if isinstance(address, tuple) and address and _is_loopback(address[0]):
        return
    raise NetworkAccessBlocked(f"network access blocked in tests: connect to {address!r}")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "live: optional live smoke test (skipped unless ETA_LIVE_TESTS=1)")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("ETA_LIVE_TESTS") == "1":
        return
    skip_live = pytest.mark.skip(reason="live test: set ETA_LIVE_TESTS=1 to run (12-testing §5)")
    for item in items:
        if item.get_closest_marker("live") is not None:
            item.add_marker(skip_live)


@pytest.fixture(autouse=True)
def _isolate_environment_and_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch,
                                     tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    saved_environ = os.environ.copy()
    live = request.node.get_closest_marker("live") is not None and os.environ.get("ETA_LIVE_TESTS") == "1"
    if not live:
        for name in list(os.environ):
            if name.lower().endswith("_proxy"):
                monkeypatch.delenv(name, raising=False)
        # A test that forgets ETA_STATE_DIR must never touch the real state dir (tokens.json, ledger, run.lock):
        # point the 02 §1 default location (XDG_STATE_HOME / LOCALAPPDATA) at a per-test temp dir.
        default_state_base = tmp_path_factory.mktemp("default-state-home")
        monkeypatch.setenv("XDG_STATE_HOME", str(default_state_base))
        monkeypatch.setenv("LOCALAPPDATA", str(default_state_base))

        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        real_create_connection = socket.create_connection
        real_getaddrinfo = socket.getaddrinfo

        def guarded_connect(self: socket.socket, address: Any) -> None:
            _check_address(address)
            return real_connect(self, address)

        def guarded_connect_ex(self: socket.socket, address: Any) -> int:
            _check_address(address)
            return real_connect_ex(self, address)

        def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> socket.socket:
            _check_address(address)
            return real_create_connection(address, *args, **kwargs)

        def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
            if host is not None and _host_text(host) and not (_is_loopback(host) or _is_ip_literal(host)):
                raise NetworkAccessBlocked(f"network access blocked in tests: DNS lookup of {_host_text(host)!r}")
            return real_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket.socket, "connect", guarded_connect)
        monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
        monkeypatch.setattr(socket, "create_connection", guarded_create_connection)
        monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    try:
        yield
    finally:
        if os.environ != saved_environ:
            os.environ.clear()
            os.environ.update(saved_environ)


@pytest.fixture
def fixed_clock() -> FixedClock:
    """FixedClock(datetime(2026, 11, 5, 9, 30)) — the run_now of every golden vector (12-testing §4)."""
    return FixedClock(RUN_NOW)


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    """Temporary ETA_STATE_DIR."""
    path = tmp_path / "state"
    path.mkdir(mode=0o700)
    return path


@pytest.fixture
def output_dir(tmp_path: Path) -> Path:
    """Temporary ETA_OUTPUT_DIR."""
    path = tmp_path / "output"
    path.mkdir()
    return path


@pytest.fixture
def token_store(state_dir: Path) -> TokenStore:
    """TokenStore at <state_dir>/tokens.json."""
    return TokenStore(state_dir / "tokens.json")


@pytest.fixture
def make_settings(tmp_path: Path, state_dir: Path, output_dir: Path) -> Callable[..., Settings]:
    """Factory: make_settings(env=None, *, env_file=<tmp>/.env, **overrides) -> Settings
    (`make_settings({"MSC_MODE": "api"})`, `make_settings(env={...})` and `make_settings(MSC_MODE="api")`
    are equivalent).

    Builds Settings via load_settings(env=...) (no .env file is read) from DEFAULT_TEST_ENV plus
    ETA_STATE_DIR/ETA_OUTPUT_DIR pointing into tmp_path, then `env`, then keyword overrides. A value of
    None removes a variable, e.g. make_settings({"QB_CLIENT_ID": None}) or make_settings(VSHIP_PASSWORD=None).
    `env_file` only sets QboSettings.env_file (refresh-token write-back target), defaulting into tmp_path.
    """

    def _make(env: Mapping[str, Any] | None = None, *, env_file: Path | None = None,
              **overrides: Any) -> Settings:
        values: dict[str, Any] = {
            "ETA_STATE_DIR": str(state_dir),
            "ETA_OUTPUT_DIR": str(output_dir),
            **DEFAULT_TEST_ENV,
        }
        values.update(env or {})
        values.update(overrides)
        clean = {name: str(value) for name, value in values.items() if value is not None}
        return load_settings(env_file if env_file is not None else tmp_path / ".env", env=clean)

    return _make


@pytest.fixture
def fixtures_dir() -> Path:
    """tests/fixtures (files created verbatim from the spec code blocks)."""
    return FIXTURES_DIR


@pytest.fixture
def load_fixture() -> Callable[[str], Any]:
    """load_fixture("vship/login_ok.json") → parsed JSON (UTF-8)."""

    def _load(relative: str) -> Any:
        return json.loads((FIXTURES_DIR / relative).read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def http_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Make eta_automations.http.request() back-off instant; returns the list of requested delays."""
    delays: list[float] = []

    async def _no_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr("eta_automations.http._sleep", _no_sleep)
    return delays
