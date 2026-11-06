"""logging_setup.py: redact() and configure_logging() (03-foundations §11; 12-testing §3)."""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest

from eta_automations.logging_setup import (
    DATE_FORMAT,
    LOG_FORMAT,
    LOGGER_NAMES,
    MASK,
    configure_logging,
    is_secret_key,
    log_file_path,
    redact,
    reset_logging,
)


@pytest.fixture(autouse=True)
def _clean_logging() -> Iterator[None]:
    reset_logging()
    yield
    reset_logging()


# ── redact(): mappings ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", [
    "Authorization", "authorization", "password", "PASSWORD", "passwd", "refresh_token", "access_token",
    "Consumer-Key", "KeyId", "keyid", "api_key", "api-key", "apikey", "ApiKey", "x-api-key", "client_secret",
    "client-secret", "Cookie", "Set-Cookie", "X-Shipsgo-User-Token", "Ocp-Apim-Subscription-Key", "token",
    "refresh_token_seed", "secret", "proxy-authorization",
])
def test_secret_keys_are_masked(key: str) -> None:
    assert is_secret_key(key)
    assert redact({key: "s3cr3t-value"}) == {key: MASK}


@pytest.mark.parametrize("key", ["booking", "expires_at", "status", "Accept", "mymsc-user-email", "limit",
                                 "key_fingerprint", "DocNumber", "cursor"])
def test_non_secret_keys_are_kept(key: str) -> None:
    assert not is_secret_key(key)
    assert redact({key: "visible"}) == {key: "visible"}


def test_redact_mapping_recursively_returns_copy() -> None:
    original = {
        "headers": {"Authorization": "Bearer abc.def.ghi", "Accept": "application/json"},
        "items": [{"password": "p1", "name": "n1"}, ("refresh_token", {"KeyId": "k"})],
        "count": 3,
        "has_token": True,
        "api_key": None,
        "token": {"nested": "dict"},
        "note": "Authorization: Bearer xyz123456",
    }
    result = redact(original)
    assert result == {
        "headers": {"Authorization": MASK, "Accept": "application/json"},
        "items": [{"password": MASK, "name": "n1"}, ("refresh_token", {"KeyId": MASK})],
        "count": 3,
        "has_token": True,     # booleans carry no secret
        "api_key": None,       # an absent secret is shown as absent
        "token": MASK,         # whole value replaced, even a container
        "note": "Authorization: ***",   # header-style line
    }
    assert original["headers"]["Authorization"] == "Bearer abc.def.ghi"  # input untouched
    assert isinstance(result["items"][1], tuple)


def test_redact_numbers_under_secret_keys_and_non_string_keys() -> None:
    from eta_automations.models import Carrier

    assert redact({"x_refresh_token_expires_in": 8726400}) == {"x_refresh_token_expires_in": MASK}
    assert redact({Carrier.MSC: "MSCU", 5: "five"}) == {Carrier.MSC: "MSCU", 5: "five"}


@pytest.mark.parametrize("value", [None, 3, 4.5, True, b"bytes"])
def test_redact_scalars_unchanged(value: object) -> None:
    assert redact(value) == value


# ── redact(): strings ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,expected", [
    ("Bearer eyJhbGciOiJIUzI1NiJ9.eyJleHAiOjF9.c2ln", "Bearer ***"),
    ("Authorization: Bearer abc123", "Authorization: ***"),
    ("got bearer ABCdef-_.~+/= from header", "got bearer *** from header"),
    ("https://h.example/x?access_token=abc&limit=5", "https://h.example/x?access_token=***&limit=5"),
    ("/v1/x?api_key=K1&apikey=K2&password=P&client_secret=S&KeyId=Z&cursor=c",
     "/v1/x?api_key=***&apikey=***&password=***&client_secret=***&KeyId=***&cursor=c"),
    ("grant_type=refresh_token&refresh_token=RT-1", "grant_type=refresh_token&refresh_token=***"),
    ("refresh_token=RT-1&grant_type=refresh_token", "refresh_token=***&grant_type=refresh_token"),
    ('{"access_token": "AT", "expires_in": 3600, "token_type": "bearer"}',
     '{"access_token": "***", "expires_in": 3600, "token_type": "***"}'),
    ("{'Authorization': 'Basic dXNlcjpwYXNz', 'Accept': '*/*'}", "{'Authorization': '***', 'Accept': '*/*'}"),
    ("Authorization: Basic dXNlcjpwYXNz", "Authorization: ***"),
    ("headers authorization=Basic dXNlcjpwYXNz end", "headers authorization=*** *** end"),
    ("Cookie: datadome=abc; session=def", "Cookie: ***"),
    ("KeyId: K-123\nAccept: application/json", "KeyId: ***\nAccept: application/json"),
    ("https://user:pa55@proxy.example:8080/path", "https://user:***@proxy.example:8080/path"),
    ("<html>password=hunter2</html>", "<html>password=***</html>"),
    ('{"refresh_token": "RT-truncat', '{"refresh_token": "***'),
    ("nothing secret here: ARRIVED ON 11/03. NOTIF #1", "nothing secret here: ARRIVED ON 11/03. NOTIF #1"),
    ("Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08.****",
     "Invoice for Booking no. EBKG12345678. ***ESTIMATED ARRIVING ON 11/08.****"),
    ("filters[booking_number]=eq:EBKG1&take=25", "filters[booking_number]=eq:EBKG1&take=25"),
    # the QuickBooks token endpoint path ends in /bearer: the next word of an eta.http log line is no credential
    ("POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer -> ConnectError in 41 ms (attempt 1/4)",
     "POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer -> ConnectError in 41 ms (attempt 1/4)"),
    ("retrying POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer in 0.64 s",
     "retrying POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer in 0.64 s"),
    ("POST /oauth2/v1/tokens/bearer with Bearer abc.def", "POST /oauth2/v1/tokens/bearer with Bearer ***"),
])
def test_redact_strings(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize("text", [
    "Authorization: Bearer abc.def", "?access_token=x&y=1", '{"password": "p"}', "Cookie: a=b",
    "https://u:p@h/", "authorization=Basic Zm9vOmJhcg==", "plain text",
])
def test_redact_is_idempotent(text: str) -> None:
    once = redact(text)
    assert redact(once) == once


def test_redact_escaped_quotes_inside_json_value() -> None:
    text = r'{"client_secret": "a\"b\\c", "next": "kept"}'
    assert redact(text) == '{"client_secret": "***", "next": "kept"}'


# ── configure_logging() ────────────────────────────────────────────────────────────────────────────
def test_logger_names_match_spec() -> None:
    assert LOGGER_NAMES == ("eta", "eta.http", "eta.pipeline", "eta.qbo", "eta.vship", "eta.carrier.msc",
                            "eta.carrier.maersk", "eta.carrier.cma", "eta.carrier.dcsa", "eta.carrier.aggregator")
    assert LOG_FORMAT == "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
    assert DATE_FORMAT == "%H:%M:%S"


def test_console_and_file_handlers(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_file = tmp_path / "logs" / "eta_20261105_093000.log"
    configure_logging("INFO", log_file)
    logger = logging.getLogger("eta.carrier.msc")
    logger.debug("debug detail")
    logger.info("MSC %s: ETA %s", "EBKG17208602", "2026-11-16")
    logger.warning("token Bearer abc.def.ghi rejected")
    err = capsys.readouterr().err
    assert "debug detail" not in err
    assert re.search(r"^\d\d:\d\d:\d\d INFO    eta\.carrier\.msc: MSC EBKG17208602: ETA 2026-11-16$", err, re.M)
    assert re.search(r"^\d\d:\d\d:\d\d WARNING eta\.carrier\.msc: token Bearer \*\*\* rejected$", err, re.M)
    assert "abc.def.ghi" not in err
    for handler in logging.getLogger("eta").handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8")
    assert "DEBUG   eta.carrier.msc: debug detail" in text
    assert "abc.def.ghi" not in text


def test_console_level_respected_without_file(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("warning")
    logging.getLogger("eta.qbo").info("hidden")
    logging.getLogger("eta.qbo").error("shown")
    err = capsys.readouterr().err
    assert "hidden" not in err and "ERROR   eta.qbo: shown" in err


def test_tracebacks_are_redacted(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("INFO")
    try:
        raise RuntimeError("failed with password=hunter2")
    except RuntimeError:
        logging.getLogger("eta").exception("boom")
    err = capsys.readouterr().err
    assert "hunter2" not in err and "password=***" in err


def test_reconfigure_replaces_handlers(tmp_path: Path) -> None:
    configure_logging("INFO")
    configure_logging("DEBUG", tmp_path / "a.log")
    configure_logging("INFO")
    owned = [h for h in logging.getLogger("eta").handlers if getattr(h, "_eta_owned_handler", False)]
    assert len(owned) == 1
    assert isinstance(owned[0], logging.StreamHandler) and not isinstance(owned[0], logging.FileHandler)
    reset_logging()
    assert not [h for h in logging.getLogger("eta").handlers if getattr(h, "_eta_owned_handler", False)]
    assert logging.getLogger("eta").level == logging.NOTSET


def test_unknown_level_rejected() -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("LOUD")


def test_log_file_path_format(tmp_path: Path) -> None:
    assert log_file_path(tmp_path, datetime(2026, 11, 5, 9, 30, 7)) == tmp_path / "logs" / "eta_20261105_093007.log"
