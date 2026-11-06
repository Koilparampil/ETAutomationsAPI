"""Optional live smoke tests (12-testing §5): the read-only probes against the real services configured in .env.

Every test here is marked `live`, so tests/conftest.py skips it unless ETA_LIVE_TESTS=1; they are never part of
the default run and only they may use the network. They run `eta probe <service>` exactly as a person would,
WITHOUT any QuickBooks write flag: nothing is written to QuickBooks or VShip and no e-mail is ever sent.

Inputs (environment variables):
- ETA_LIVE_ENV_FILE — the .env to use (default ./.env); without it every test is skipped.
- ETA_LIVE_VSHIP_BOOKING, ETA_LIVE_MSC_BOOKING, ETA_LIVE_MAERSK_BOOKING — a real booking for that probe;
  the probe is skipped when its variable is unset.
- ETA_LIVE_CMA_BOOKING — CMA bills per API call (08 §2) and its probe makes at least two billable calls, so the
  CMA probe runs only when this is set explicitly.
A carrier or service that is not configured in the .env is skipped, not failed.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from eta_automations import cli
from eta_automations.config import Settings, load_settings
from eta_automations.logging_setup import reset_logging

pytestmark = pytest.mark.live


@pytest.fixture(autouse=True)
def _drop_cli_logging() -> Iterator[None]:
    """main() installs console/file handlers on the 'eta' logger; remove them after each probe."""
    yield
    reset_logging()


def _env_file() -> Path:
    path = Path(os.environ.get("ETA_LIVE_ENV_FILE") or ".env").expanduser()
    if not path.is_file():
        pytest.skip(f"no .env for the live smoke tests at {path} (set ETA_LIVE_ENV_FILE)")
    return path


def _settings() -> Settings:
    # load_settings() puts the .env values into os.environ; conftest restores os.environ after every test.
    return load_settings(_env_file())


def _configured(service: str) -> None:
    settings = _settings()
    if getattr(settings, service) is None:
        missing = ", ".join(settings.missing.get(service, ())) or "its settings"
        pytest.skip(f"{service} is not configured in {_env_file()} (set {missing})")


def _booking(service: str) -> str:
    name = f"ETA_LIVE_{service.upper()}_BOOKING"
    value = (os.environ.get(name) or "").strip()
    if not value:
        pytest.skip(f"set {name} to a real booking number to run the live {service} probe")
    return value


def _probe(*args: str) -> int:
    return cli.main(["probe", *args, "--env-file", str(_env_file())])


def test_live_probe_qbo_read_only() -> None:
    """04 §10 step 2: token refresh, company, invoice e-mail template — no write flag, nothing changes."""
    _configured("qbo")
    assert _probe("qbo") == cli.EXIT_OK


def test_live_probe_vship() -> None:
    """05 §10: login, booking search, internal comments (read-only)."""
    _configured("vship")
    assert _probe("vship", "--booking", _booking("vship")) == cli.EXIT_OK


def test_live_probe_msc_public_sandbox() -> None:
    """06 §10 step 1: MSC's public mock sandbox needs no credentials (proves proxy/TLS/installation)."""
    _env_file()
    assert _probe("msc", "--sandbox") == cli.EXIT_OK


def test_live_probe_msc_booking() -> None:
    _configured("msc")
    assert _probe("msc", "--booking", _booking("msc")) == cli.EXIT_OK


def test_live_probe_maersk_booking() -> None:
    _configured("maersk")
    assert _probe("maersk", "--booking", _booking("maersk")) == cli.EXIT_OK


def test_live_probe_cma_booking_is_opt_in() -> None:
    """08 §10: billable — runs only with ETA_LIVE_CMA_BOOKING."""
    booking = _booking("cma")
    _configured("cma")
    assert _probe("cma", "--booking", booking) == cli.EXIT_OK


def test_live_probe_aggregator_account() -> None:
    """09 §B.11: account check only; the probe never creates a tracking."""
    _configured("aggregator")
    assert _probe("aggregator") == cli.EXIT_OK
