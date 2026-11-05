"""config.py: Settings, load_settings(), require() (03 §12, 02, 04 §3, 04b §3, 05 §3, 06 §3, 07 §3, 08 §3, 09 §B.4/§D.1)."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eta_automations.config import (
    AggregatorSettings,
    CmaSettings,
    EmailSettings,
    MaerskSettings,
    MscSettings,
    QboSettings,
    RunSettings,
    Settings,
    VShipSettings,
    default_state_dir,
    load_settings,
    require,
    settings_summary,
)
from eta_automations.errors import ConfigError
from eta_automations.models import Carrier

MakeSettings = Callable[..., Settings]


# ── run settings (02 §1) ───────────────────────────────────────────────────────────────────────────
def test_run_defaults(make_settings: MakeSettings, state_dir: Path, output_dir: Path) -> None:
    run = make_settings().run
    assert run == RunSettings(output_dir=output_dir, state_dir=state_dir)
    assert run.min_invoice_amount == Decimal("1000")
    assert run.window_business_days == 6
    assert run.on_notif_lookup_error == "send"
    assert run.max_parallel_bookings == 8
    assert run.carrier_failure_limit == 5
    assert run.log_level == "INFO"
    assert dict(run.providers) == {Carrier.MSC: "native", Carrier.MAERSK: "native", Carrier.CMA: "native",
                                   Carrier.GRIMALDI: "off"}


def test_run_values_are_parsed(make_settings: MakeSettings, tmp_path: Path) -> None:
    run = make_settings({
        "ETA_MIN_INVOICE_AMOUNT": "1,500.50", "ETA_WINDOW_BUSINESS_DAYS": "4", "ETA_ON_NOTIF_LOOKUP_ERROR": " FAIL ",
        "ETA_MAX_PARALLEL_BOOKINGS": "3", "ETA_CARRIER_FAILURE_LIMIT": "2", "ETA_LOG_LEVEL": "debug",
        "ETA_OUTPUT_DIR": str(tmp_path / "out"),
    }).run
    assert run.min_invoice_amount == Decimal("1500.50")
    assert run.window_business_days == 4
    assert run.on_notif_lookup_error == "fail"
    assert run.max_parallel_bookings == 3
    assert run.carrier_failure_limit == 2
    assert run.log_level == "DEBUG"
    assert run.output_dir == tmp_path / "out"


def test_output_dir_defaults_to_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_settings(env={"ETA_STATE_DIR": str(tmp_path / "s")}).run.output_dir == tmp_path


@pytest.mark.skipif(os.name == "nt", reason="POSIX default state dir")
def test_state_dir_default_posix(tmp_path: Path) -> None:
    xdg = tmp_path / "xdg"
    assert load_settings(env={"XDG_STATE_HOME": str(xdg)}).run.state_dir == xdg / "eta_automations"
    home = tmp_path / "home"
    assert load_settings(env={"HOME": str(home)}).run.state_dir == home / ".local" / "state" / "eta_automations"
    # XDG requires an absolute path; a relative value is ignored
    assert load_settings(env={"HOME": str(home), "XDG_STATE_HOME": "rel"}).run.state_dir == \
        home / ".local" / "state" / "eta_automations"


@pytest.mark.skipif(os.name == "nt", reason="POSIX default state dir")
def test_tests_never_use_the_real_default_state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest points XDG_STATE_HOME at a temp dir, so a test without ETA_STATE_DIR stays sandboxed."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ETA_STATE_DIR", raising=False)
    state_dir = load_settings().run.state_dir
    assert state_dir == Path(os.environ["XDG_STATE_HOME"]) / "eta_automations"
    assert state_dir != Path.home() / ".local" / "state" / "eta_automations"


def test_state_dir_default_windows_rules() -> None:
    assert default_state_dir({"LOCALAPPDATA": "C:/Users/me/AppData/Local"}, windows=True) == \
        Path("C:/Users/me/AppData/Local") / "ETAutomationsAPI"
    assert default_state_dir({"USERPROFILE": "C:/Users/me"}, windows=True) == \
        Path("C:/Users/me") / "AppData" / "Local" / "ETAutomationsAPI"


@pytest.mark.parametrize("name,value", [
    ("ETA_MAX_PARALLEL_BOOKINGS", "abc"),
    ("ETA_MAX_PARALLEL_BOOKINGS", "0"),
    ("ETA_MIN_INVOICE_AMOUNT", "lots"),
    ("ETA_MIN_INVOICE_AMOUNT", "-5"),
    ("ETA_MIN_INVOICE_AMOUNT", "NaN"),
    ("ETA_WINDOW_BUSINESS_DAYS", "1.5"),
    ("ETA_WINDOW_BUSINESS_DAYS", "-1"),
    ("ETA_ON_NOTIF_LOOKUP_ERROR", "maybe"),
    ("ETA_CARRIER_FAILURE_LIMIT", "0"),
    ("ETA_LOG_LEVEL", "LOUD"),
])
def test_malformed_run_values_raise(make_settings: MakeSettings, name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value})


# ── providers (09 §D.1) ────────────────────────────────────────────────────────────────────────────
def test_providers_case_insensitive_and_stripped(make_settings: MakeSettings) -> None:
    providers = make_settings({"ETA_PROVIDER_MSC": " Aggregator ", "ETA_PROVIDER_MAERSK": "OFF",
                               "ETA_PROVIDER_GRIMALDI": "native"}).run.providers
    assert providers[Carrier.MSC] == "aggregator"
    assert providers[Carrier.MAERSK] == "off"
    assert providers[Carrier.CMA] == "native"
    assert providers[Carrier.GRIMALDI] == "native"


def test_invalid_provider_raises_exact_message(make_settings: MakeSettings) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings({"ETA_PROVIDER_CMA": "shipsgo"})
    assert str(info.value) == "ETA_PROVIDER_CMA must be one of native, aggregator, off (got 'shipsgo')"


# ── booleans (03 §12) ──────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("On", True),
    ("0", False), ("false", False), ("No", False), ("OFF", False), (" off ", False), ("", False),
])
def test_boolean_forms(make_settings: MakeSettings, value: str, expected: bool) -> None:
    qbo = make_settings({"QB_SANDBOX": value}).qbo
    assert qbo is not None and qbo.sandbox is expected


def test_boolean_default_true_and_invalid(make_settings: MakeSettings) -> None:
    qbo = make_settings().qbo
    assert qbo is not None and qbo.verify_updates is True
    with pytest.raises(ConfigError, match="QB_VERIFY_UPDATES"):
        make_settings({"QB_VERIFY_UPDATES": "maybe"})


# ── QuickBooks (04 §3, 04b §3) ─────────────────────────────────────────────────────────────────────
def test_qbo_defaults(make_settings: MakeSettings, tmp_path: Path) -> None:
    qbo = make_settings().qbo
    assert isinstance(qbo, QboSettings)
    assert (qbo.client_id, qbo.client_secret, qbo.realm_id) == ("test-qb-client-id", "test-qb-client-secret",
                                                                 "1234567890")
    assert qbo.refresh_token == "test-qb-refresh-token"
    assert qbo.sandbox is False and qbo.minor_version == "75"
    assert (qbo.eta_field_name, qbo.eta_definition_id, qbo.eta_field_type) == ("ETA", None, "DateType")
    assert qbo.eta_string_format == "%m/%d/%Y"
    assert qbo.verify_updates and qbo.include_on_writes
    assert (qbo.query_chunk_size, qbo.max_concurrency, qbo.max_calls_per_minute, qbo.max_calls_per_second,
            qbo.docnumber_max_len) == (25, 4, 450, 8, 21)
    assert qbo.redirect_uri == "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"
    assert qbo.oauth_scopes == "com.intuit.quickbooks.accounting"
    assert qbo.env_file == tmp_path / ".env"
    assert (qbo.email_strategy, qbo.email_require_verified, qbo.email_redirect_to, qbo.test_send_to) == \
        ("template_swap", True, None, None)
    assert (qbo.prefs_update_mode, qbo.mark_sent, qbo.email_max_per_minute, qbo.email_max_concurrency) == \
        ("full", True, 25, 2)
    assert qbo.api_base == "https://quickbooks.api.intuit.com"


def test_qbo_values(make_settings: MakeSettings) -> None:
    qbo = make_settings({
        "QB_SANDBOX": "true", "QB_MINOR_VERSION": "76", "QB_ETA_DEFINITION_ID": "1000000003",
        "QB_ETA_FIELD_TYPE": "StringType", "QB_QUERY_CHUNK_SIZE": "2", "QB_EMAIL_STRATEGY": "GRAPH",
        "QB_PREFS_UPDATE_MODE": "sparse", "QB_EMAIL_REDIRECT_TO": "shadow@example.com",
        "QB_TEST_SEND_TO": "test@example.com", "QB_MARK_SENT": "no", "QB_INCLUDE_ON_WRITES": "0",
    }).qbo
    assert qbo is not None
    assert qbo.api_base == "https://sandbox-quickbooks.api.intuit.com"
    assert qbo.minor_version == "76" and qbo.eta_definition_id == "1000000003"
    assert qbo.eta_field_type == "StringType" and qbo.query_chunk_size == 2
    assert qbo.email_strategy == "graph" and qbo.prefs_update_mode == "sparse"
    assert qbo.email_redirect_to == "shadow@example.com" and qbo.test_send_to == "test@example.com"
    assert qbo.mark_sent is False and qbo.include_on_writes is False


def test_qbo_missing_required_vars(make_settings: MakeSettings) -> None:
    settings = make_settings({"QB_CLIENT_ID": None, "QB_REALM_ID": "   "})
    assert settings.qbo is None
    assert settings.missing["qbo"] == ("QB_CLIENT_ID", "QB_REALM_ID")


def test_qbo_refresh_token_not_required_at_load(make_settings: MakeSettings) -> None:
    qbo = make_settings({"QB_REFRESH_TOKEN": None}).qbo
    assert qbo is not None and qbo.refresh_token == ""


@pytest.mark.parametrize("name,value", [
    ("QB_MAX_CONCURRENCY", "abc"), ("QB_EMAIL_STRATEGY", "outlook"), ("QB_PREFS_UPDATE_MODE", "partial"),
    ("QB_MINOR_VERSION", "v75"), ("QB_QUERY_CHUNK_SIZE", "0"), ("QB_REDIRECT_URI", "ftp://example.com/cb"),
    ("QB_MAX_CALLS_PER_SECOND", "0"), ("QB_EMAIL_MAX_PER_MINUTE", "-1"), ("QB_DOCNUMBER_MAX_LEN", "x"),
])
def test_malformed_qbo_values_raise_even_when_qbo_unconfigured(make_settings: MakeSettings, name: str,
                                                                value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value})
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value, "QB_CLIENT_ID": None})


def test_obsolete_qb_variables_logged_once_at_info(make_settings: MakeSettings,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="eta")
    make_settings({"QB": "old-password", "QB_HEADLESS": "true"})
    infos = [r for r in caplog.records if r.levelno == logging.INFO]
    assert [r.getMessage() for r in infos] == [
        "QB and QB_HEADLESS are no longer used (no browser) — you can delete them from .env"]
    assert "old-password" not in caplog.text


def test_legacy_msc_variables_logged_at_debug(make_settings: MakeSettings, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="eta")
    settings = make_settings({"MSC_PASSWORD": "pw-legacy", "LOGIN_URL": "https://x", "HOME_PAGE": "https://y"})
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug) == 1
    assert "MSC_PASSWORD, LOGIN_URL, HOME_PAGE" in debug[0] and "no longer used" in debug[0]
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]
    assert "pw-legacy" not in caplog.text
    assert settings.msc is not None  # legacy variables are ignored by lookups


def test_no_obsolete_log_without_obsolete_variables(make_settings: MakeSettings,
                                                    caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="eta")
    make_settings()
    assert caplog.records == []


# ── e-mail (04b §3) ────────────────────────────────────────────────────────────────────────────────
def test_email_none_and_not_missing_for_template_swap(make_settings: MakeSettings) -> None:
    settings = make_settings({"EMAIL_FROM": "accounting@example.com"})
    assert settings.email is None
    assert "email" not in settings.missing


def test_email_graph_configured(make_settings: MakeSettings, tmp_path: Path) -> None:
    email = make_settings({
        "QB_EMAIL_STRATEGY": "graph", "EMAIL_FROM": "accounting@example.com", "EMAIL_REPLY_TO": "ar@example.com",
        "EMAIL_BCC": "archive@example.com; audit@example.com, ARCHIVE@example.com,,",
        "EMAIL_BODY_TEMPLATE": str(tmp_path / "body.txt"), "EMAIL_INCLUDE_PAY_LINK": "false",
        "EMAIL_GRAPH_TENANT_ID": "tenant", "EMAIL_GRAPH_CLIENT_ID": "client", "EMAIL_GRAPH_CLIENT_SECRET": "g-secret",
    }).email
    assert isinstance(email, EmailSettings)
    assert email.transport == "graph" and email.sender == "accounting@example.com"
    assert email.reply_to == "ar@example.com"
    assert email.bcc == ("archive@example.com", "audit@example.com")
    assert email.body_template == tmp_path / "body.txt"
    assert email.include_pay_link is False and email.attach_pdf is True
    assert (email.graph_tenant_id, email.graph_client_id, email.graph_client_secret) == ("tenant", "client", "g-secret")
    assert (email.smtp_port, email.smtp_security) == (587, "starttls")
    assert "g-secret" not in repr(email)


def test_email_graph_missing_vars(make_settings: MakeSettings) -> None:
    settings = make_settings({"QB_EMAIL_STRATEGY": "graph", "EMAIL_FROM": "a@example.com",
                              "EMAIL_GRAPH_CLIENT_ID": "client"})
    assert settings.email is None
    assert settings.missing["email"] == ("EMAIL_GRAPH_TENANT_ID", "EMAIL_GRAPH_CLIENT_SECRET")


def test_email_smtp_missing_then_configured(make_settings: MakeSettings) -> None:
    settings = make_settings({"QB_EMAIL_STRATEGY": "smtp"})
    assert settings.email is None
    assert settings.missing["email"] == ("EMAIL_FROM", "EMAIL_SMTP_HOST", "EMAIL_SMTP_USERNAME", "EMAIL_SMTP_PASSWORD")
    email = make_settings({"QB_EMAIL_STRATEGY": "smtp", "EMAIL_FROM": "a@example.com", "EMAIL_SMTP_HOST": "smtp.example.com",
                           "EMAIL_SMTP_USERNAME": "user", "EMAIL_SMTP_PASSWORD": " app pass ",
                           "EMAIL_SMTP_PORT": "465", "EMAIL_SMTP_SECURITY": "SSL", "EMAIL_ATTACH_PDF": "off"}).email
    assert email is not None
    assert (email.transport, email.smtp_host, email.smtp_port, email.smtp_security) == ("smtp", "smtp.example.com", 465, "ssl")
    assert email.smtp_password == " app pass "  # user-chosen password kept as typed
    assert email.attach_pdf is False


@pytest.mark.parametrize("name,value", [("EMAIL_SMTP_PORT", "70000"), ("EMAIL_SMTP_PORT", "smtp"),
                                        ("EMAIL_SMTP_SECURITY", "tls"), ("EMAIL_INCLUDE_PAY_LINK", "sometimes")])
def test_malformed_email_values_raise(make_settings: MakeSettings, name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value})


# ── VShip (05 §3) ──────────────────────────────────────────────────────────────────────────────────
def test_vship_defaults_and_values(make_settings: MakeSettings) -> None:
    vship = make_settings().vship
    assert vship == VShipSettings(username="ops@example.com", password="test-vship-password")
    assert vship.api_base == "https://vship2000-prod-api.azurewebsites.net/api"
    assert (vship.max_concurrency, vship.token_ttl_minutes, vship.comment_type) == (4, 55, 1)
    custom = make_settings({"VSHIP_API_BASE": "https://vship.example.test/api/", "VSHIP_MAX_CONCURRENCY": "2",
                            "VSHIP_TOKEN_TTL_MINUTES": "30", "VSHIP_COMMENT_TYPE": "2",
                            "VSHIP_PASSWORD": " pass word "}).vship
    assert custom is not None
    assert custom.api_base == "https://vship.example.test/api"
    assert (custom.max_concurrency, custom.token_ttl_minutes, custom.comment_type) == (2, 30, 2)
    assert custom.password == " pass word "  # sent as typed (parity)


def test_vship_missing(make_settings: MakeSettings) -> None:
    settings = make_settings({"VSHIP_USER_NAME": None, "VSHIP_PASSWORD": ""})
    assert settings.vship is None
    assert settings.missing["vship"] == ("VSHIP_USER_NAME", "VSHIP_PASSWORD")


@pytest.mark.parametrize("name,value", [("VSHIP_MAX_CONCURRENCY", "0"), ("VSHIP_TOKEN_TTL_MINUTES", "1h"),
                                        ("VSHIP_API_BASE", "vship2000-prod-api.azurewebsites.net/api"),
                                        ("VSHIP_API_BASE", "https://[::1/api"), ("VSHIP_API_BASE", "https://")])
def test_malformed_vship_values_raise(make_settings: MakeSettings, name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value})


# ── MSC (06 §3) ────────────────────────────────────────────────────────────────────────────────────
API_URL = "https://msc-api.example.test/dpo/trackandtrace/v2.2"
SANDBOX_URL = "https://portal.api.msc.com/dpo/trackandtrace/v2.2"


def test_msc_auto_resolution(make_settings: MakeSettings) -> None:
    both = make_settings({"MSC_API_BASE_URL": API_URL + "/", "MSC_API_SUBSCRIPTION_KEY": "sub-key"}).msc
    assert both is not None and both.mode == "api" and both.api_base_url == API_URL and both.api_key == "sub-key"
    only_url = make_settings({"MSC_API_BASE_URL": API_URL}).msc
    assert only_url is not None and only_url.mode == "mymsc" and only_url.api_base_url == API_URL
    only_key = make_settings({"MSC_API_SUBSCRIPTION_KEY": "sub-key"}).msc
    assert only_key is not None and only_key.mode == "mymsc"
    neither = make_settings().msc
    assert neither is not None and neither.mode == "mymsc"
    explicit_bridge = make_settings({"MSC_MODE": "MyMSC", "MSC_API_BASE_URL": API_URL,
                                     "MSC_API_SUBSCRIPTION_KEY": "k"}).msc
    assert explicit_bridge is not None and explicit_bridge.mode == "mymsc"


def test_msc_explicit_api_with_missing_vars_is_none(make_settings: MakeSettings) -> None:
    settings = make_settings({"MSC_MODE": "api", "MSC_API_BASE_URL": API_URL})
    assert settings.msc is None
    assert settings.missing["msc"] == ("MSC_API_SUBSCRIPTION_KEY",)
    settings = make_settings({"MSC_MODE": "api"})
    assert settings.msc is None
    assert settings.missing["msc"] == ("MSC_API_BASE_URL", "MSC_API_SUBSCRIPTION_KEY")


def test_msc_explicit_api_sandbox_needs_no_key(make_settings: MakeSettings) -> None:
    msc = make_settings({"MSC_MODE": "api", "MSC_API_BASE_URL": SANDBOX_URL}).msc
    assert msc is not None and msc.mode == "api" and msc.api_key is None
    assert msc.api_is_sandbox is True
    assert make_settings({"MSC_API_BASE_URL": API_URL, "MSC_API_SUBSCRIPTION_KEY": "k"}).msc.api_is_sandbox is False  # type: ignore[union-attr]
    assert MscSettings(mode="mymsc").api_is_sandbox is False


def test_msc_mode_invalid(make_settings: MakeSettings) -> None:
    with pytest.raises(ConfigError, match="MSC_MODE"):
        make_settings({"MSC_MODE": "browser"})


def test_msc_user_email_fallback_chain(make_settings: MakeSettings) -> None:
    assert make_settings({"MSC_USER_EMAIL": "me@example.com", "MSC_USER_NAME": "old@example.com"}).msc.user_email == "me@example.com"  # type: ignore[union-attr]
    assert make_settings({"MSC_USER_NAME": "old@example.com"}).msc.user_email == "ops@example.com"  # type: ignore[union-attr]
    assert make_settings({"VSHIP_USER_NAME": None, "MSC_USER_NAME": "old@example.com"}).msc.user_email == "old@example.com"  # type: ignore[union-attr]
    assert make_settings({"VSHIP_USER_NAME": None}).msc.user_email == ""  # type: ignore[union-attr]


@pytest.mark.parametrize("raw,expected", [
    ("Bearer abc.def.ghi", "abc.def.ghi"), ("bearer   xyz ", "xyz"), ("  plain-token  ", "plain-token"),
    ("Bearer", None), ("", None),
])
def test_msc_access_token_prefix_stripped(make_settings: MakeSettings, raw: str, expected: str | None) -> None:
    msc = make_settings({"MSC_ACCESS_TOKEN": raw}).msc
    assert msc is not None and msc.manual_token == expected


def test_msc_defaults_and_secret_repr(make_settings: MakeSettings) -> None:
    msc = make_settings({"MSC_ACCESS_TOKEN": "tok-secret-1", "MSC_API_SUBSCRIPTION_KEY": "sub-secret-2"}).msc
    assert msc is not None
    assert msc.user_roles == "Customer"
    assert msc.graphql_url == "https://services.mymsc.com/tracking/graphql"
    assert (msc.token_ttl_seconds, msc.rate_per_minute, msc.api_rate_per_minute) == (1800, 120, 0)
    assert (msc.api_customs_hold_rule, msc.allow_sandbox, msc.rescue_fallback) == (True, False, False)
    assert (msc.max_concurrency, msc.timeout_seconds) == (4, 30.0)
    assert "tok-secret-1" not in repr(msc) and "sub-secret-2" not in repr(msc)


@pytest.mark.parametrize("name,value", [
    ("MSC_TIMEOUT_SECONDS", "0"), ("MSC_RATE_PER_MINUTE", "-1"), ("MSC_ALLOW_SANDBOX", "perhaps"),
    ("MSC_TOKEN_TTL_SECONDS", "0"), ("MSC_MAX_CONCURRENCY", "four"), ("MSC_API_BASE_URL", "not a url"),
])
def test_malformed_msc_values_raise(make_settings: MakeSettings, name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value})


# ── Maersk (07 §3) ─────────────────────────────────────────────────────────────────────────────────
def test_maersk_unconfigured_when_a_credential_is_empty(make_settings: MakeSettings) -> None:
    settings = make_settings({"MAERSK_CLIENT_ID": "id"})
    assert settings.maersk is None and settings.missing["maersk"] == ("MAERSK_CLIENT_SECRET",)
    settings = make_settings()
    assert settings.maersk is None and settings.missing["maersk"] == ("MAERSK_CLIENT_ID", "MAERSK_CLIENT_SECRET")


def test_maersk_defaults_and_stripping(make_settings: MakeSettings) -> None:
    maersk = make_settings({"MAERSK_CLIENT_ID": "  consumer-key ", "MAERSK_CLIENT_SECRET": " secret-x "}).maersk
    assert maersk == MaerskSettings(client_id="consumer-key", client_secret="secret-x")
    assert maersk.base_url == "https://api.maersk.com"
    assert maersk.token_url == "https://api.maersk.com/customer-identity/oauth/v2/access_token"
    assert maersk.api_version is None
    assert (maersk.eta_target, maersk.multi_container_policy, maersk.eta_date_only) == ("final", "first", True)
    assert (maersk.page_limit, maersk.max_pages, maersk.max_concurrency, maersk.rate_per_minute,
            maersk.timeout_s) == (100, 10, 3, 0, 30.0)
    assert "consumer-key" not in repr(maersk) and "secret-x" not in repr(maersk)


def test_maersk_values(make_settings: MakeSettings) -> None:
    maersk = make_settings({"MAERSK_CLIENT_ID": "id", "MAERSK_CLIENT_SECRET": "s",
                            "MAERSK_BASE_URL": "https://api.maersk.test/", "MAERSK_API_VERSION": "1",
                            "MAERSK_ETA_TARGET": "POD", "MAERSK_MULTI_CONTAINER_POLICY": "latest",
                            "MAERSK_ETA_DATE_ONLY": "false", "MAERSK_PAGE_LIMIT": "50", "MAERSK_MAX_PAGES": "3",
                            "MAERSK_RATE_PER_MINUTE": "60", "MAERSK_TIMEOUT_SECONDS": "12.5"}).maersk
    assert maersk is not None
    assert maersk.base_url == "https://api.maersk.test"
    assert maersk.api_version == "1"
    assert (maersk.eta_target, maersk.multi_container_policy, maersk.eta_date_only) == ("pod", "latest", False)
    assert (maersk.page_limit, maersk.max_pages, maersk.rate_per_minute, maersk.timeout_s) == (50, 3, 60, 12.5)


@pytest.mark.parametrize("name,value", [
    ("MAERSK_ETA_TARGET", "port"), ("MAERSK_MULTI_CONTAINER_POLICY", "last"), ("MAERSK_PAGE_LIMIT", "0"),
    ("MAERSK_PAGE_LIMIT", "101"), ("MAERSK_MAX_PAGES", "0"), ("MAERSK_MAX_CONCURRENCY", "0"),
    ("MAERSK_TIMEOUT_SECONDS", "0.5"), ("MAERSK_RATE_PER_MINUTE", "-1"),
])
def test_malformed_maersk_values_raise_naming_the_variable(make_settings: MakeSettings, name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value, "MAERSK_CLIENT_ID": "id", "MAERSK_CLIENT_SECRET": "s"})


# ── CMA (08 §3) ────────────────────────────────────────────────────────────────────────────────────
def test_cma_auto_resolution(make_settings: MakeSettings) -> None:
    oauth = make_settings({"CMA_CLIENT_ID": "cid", "CMA_CLIENT_SECRET": "csec", "CMA_API_KEY": "key"}).cma
    assert oauth is not None and oauth.auth_mode == "oauth"
    apikey = make_settings({"CMA_API_KEY": "key"}).cma
    assert apikey is not None and apikey.auth_mode == "apikey" and apikey.api_key == "key"
    for env in ({}, {"CMA_CLIENT_ID": "cid"}, {"CMA_CLIENT_SECRET": "csec"}):
        settings = make_settings(env)
        assert settings.cma is None
        assert settings.missing["cma"] == ("CMA_API_KEY", "CMA_CLIENT_ID", "CMA_CLIENT_SECRET")


def test_cma_forced_modes_with_missing_credentials(make_settings: MakeSettings) -> None:
    settings = make_settings({"CMA_AUTH_MODE": "apikey", "CMA_CLIENT_ID": "cid", "CMA_CLIENT_SECRET": "csec"})
    assert settings.cma is None and settings.missing["cma"] == ("CMA_API_KEY",)
    settings = make_settings({"CMA_AUTH_MODE": "oauth", "CMA_CLIENT_ID": "cid", "CMA_API_KEY": "key"})
    assert settings.cma is None and settings.missing["cma"] == ("CMA_CLIENT_SECRET",)
    settings = make_settings({"CMA_AUTH_MODE": "OAuth"})
    assert settings.cma is None and settings.missing["cma"] == ("CMA_CLIENT_ID", "CMA_CLIENT_SECRET")
    forced = make_settings({"CMA_AUTH_MODE": "apikey", "CMA_API_KEY": "key", "CMA_CLIENT_ID": "cid",
                            "CMA_CLIENT_SECRET": "csec"}).cma
    assert forced is not None and forced.auth_mode == "apikey"


def test_cma_defaults_and_normalisation(make_settings: MakeSettings) -> None:
    cma = make_settings({"CMA_API_KEY": "  key-1  ", "CMA_BASE_URL": "https://uat.cma.test/trackandtrace/v1/",
                         "CMA_LOOKUP_MODE": "DCSA_QUERY", "CMA_BEHALF_OF": " P123 "}).cma
    assert cma is not None
    assert cma.api_key == "key-1" and cma.behalf_of == "P123"
    assert cma.base_url == "https://uat.cma.test/trackandtrace/v1"
    assert cma.lookup_mode == "dcsa_query"
    assert cma.oauth_scope == "tandtcommercial:read:be tandtpublic:read:be"
    assert cma.token_url == "https://auth.cma-cgm.com/as/token.oauth2"
    assert (cma.page_limit, cma.max_pages, cma.max_concurrency, cma.max_calls_per_minute, cma.max_retry_after_s,
            cma.timeout_s) == (500, 10, 2, None, 120, 30.0)
    default = make_settings({"CMA_API_KEY": "k"}).cma
    assert default == CmaSettings(auth_mode="apikey", api_key="k")
    assert default.base_url == "https://apis.cma-cgm.net/operation/trackandtrace/v1"
    assert make_settings({"CMA_API_KEY": "k", "CMA_MAX_CALLS_PER_MINUTE": "30",
                          "CMA_MAX_RETRY_AFTER_S": "0"}).cma == CmaSettings(auth_mode="apikey", api_key="k",
                                                                          max_calls_per_minute=30, max_retry_after_s=0)
    assert "key-1" not in repr(cma)


@pytest.mark.parametrize("name,value", [
    ("CMA_AUTH_MODE", "basic"), ("CMA_LOOKUP_MODE", "booking"), ("CMA_PAGE_LIMIT", "501"), ("CMA_PAGE_LIMIT", "0"),
    ("CMA_PAGE_LIMIT", "ten"), ("CMA_MAX_PAGES", "0"), ("CMA_MAX_CONCURRENCY", "0"), ("CMA_MAX_CALLS_PER_MINUTE", "0"),
    ("CMA_MAX_CALLS_PER_MINUTE", "x"), ("CMA_MAX_RETRY_AFTER_S", "-1"), ("CMA_MAX_RETRY_AFTER_S", "1.5"),
    ("CMA_TIMEOUT_S", "0"), ("CMA_TIMEOUT_S", "abc"), ("CMA_BASE_URL", "http://apis.cma-cgm.net/x"),
    ("CMA_TOKEN_URL", "http://auth.cma-cgm.com/as/token.oauth2"),
])
def test_malformed_cma_values_raise(make_settings: MakeSettings, name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value, "CMA_API_KEY": "k"})
    with pytest.raises(ConfigError, match=name):  # also when CMA itself is not configured
        make_settings({name: value})


# ── aggregator (09 §B.4) ───────────────────────────────────────────────────────────────────────────
def test_aggregator_disabled_without_token(make_settings: MakeSettings) -> None:
    settings = make_settings()
    assert settings.aggregator is None and settings.missing["aggregator"] == ("AGG_API_TOKEN",)


def test_aggregator_defaults_and_values(make_settings: MakeSettings, state_dir: Path) -> None:
    agg = make_settings({"AGG_API_TOKEN": "agg-secret"}).aggregator
    assert isinstance(agg, AggregatorSettings)
    assert agg.state_file == state_dir / "aggregator_tracking.json"
    assert (agg.provider, agg.base_url) == ("shipsgo", "https://api.shipsgo.com/v2")
    assert (agg.max_concurrency, agg.rate_per_minute, agg.timeout_s, agg.allow_create) == (2, 90, 30.0, True)
    assert (agg.max_creates_per_run, agg.first_lookup_wait_s, agg.poll_interval_s, agg.fresh_minutes) == (100, 45, 15, 30)
    assert dict(agg.scac) == {Carrier.MSC: "MSCU", Carrier.MAERSK: "MAEU", Carrier.CMA: "CMDU", Carrier.GRIMALDI: "GRIU"}
    assert "agg-secret" not in repr(agg)
    custom = make_settings({"AGG_API_TOKEN": "t", "AGG_PROVIDER": "ShipsGo", "AGG_ALLOW_CREATE": "false",
                            "AGG_MAX_CREATES_PER_RUN": "0", "AGG_FIRST_LOOKUP_WAIT_S": "0", "AGG_SCAC_GRIMALDI": " sg_griu ",
                            "AGG_BASE_URL": "https://shipsgo.example.test/v2/"}).aggregator
    assert custom is not None
    assert custom.allow_create is False and custom.max_creates_per_run == 0 and custom.first_lookup_wait_s == 0
    assert custom.scac[Carrier.GRIMALDI] == "SG_GRIU"
    assert custom.base_url == "https://shipsgo.example.test/v2"


def test_aggregator_unknown_provider_exact_message(make_settings: MakeSettings) -> None:
    with pytest.raises(ConfigError) as info:
        make_settings({"AGG_API_TOKEN": "t", "AGG_PROVIDER": "terminal49"})
    assert str(info.value) == "AGG_PROVIDER=terminal49 is described in 09 §B.9 but not implemented"


@pytest.mark.parametrize("name,value", [
    ("AGG_RATE_PER_MINUTE", "0"), ("AGG_MAX_CONCURRENCY", "0"), ("AGG_POLL_INTERVAL_S", "-1"),
    ("AGG_FRESH_MINUTES", "-5"), ("AGG_MAX_CREATES_PER_RUN", "many"), ("AGG_TIMEOUT_SECONDS", "0"),
    ("AGG_ALLOW_CREATE", "sure"),
])
def test_malformed_aggregator_values_raise(make_settings: MakeSettings, name: str, value: str) -> None:
    with pytest.raises(ConfigError, match=name):
        make_settings({name: value, "AGG_API_TOKEN": "t"})


# ── require() ──────────────────────────────────────────────────────────────────────────────────────
def test_require_lists_every_missing_variable(make_settings: MakeSettings) -> None:
    settings = make_settings({"QB_CLIENT_ID": None, "QB_REALM_ID": None, "VSHIP_PASSWORD": None})
    with pytest.raises(ConfigError) as info:
        require(settings, "qbo", "vship")
    message = str(info.value)
    assert "qbo needs QB_CLIENT_ID, QB_REALM_ID" in message
    assert "vship needs VSHIP_PASSWORD" in message
    assert ".env" in message


def test_require_passes_when_configured(make_settings: MakeSettings) -> None:
    settings = make_settings()
    require(settings, "qbo", "vship", "msc")
    require(settings)


def test_require_cma_and_email_messages(make_settings: MakeSettings) -> None:
    settings = make_settings()
    with pytest.raises(ConfigError, match=r"cma needs CMA_API_KEY \(or CMA_CLIENT_ID \+ CMA_CLIENT_SECRET\)"):
        require(settings, "cma")
    with pytest.raises(ConfigError, match="QB_EMAIL_STRATEGY"):
        require(settings, "email")
    with pytest.raises(ConfigError, match="maersk needs MAERSK_CLIENT_ID, MAERSK_CLIENT_SECRET"):
        require(settings, "qbo", "maersk")


def test_require_unknown_service_is_a_programming_error(make_settings: MakeSettings) -> None:
    with pytest.raises(ValueError, match="unknown service"):
        require(make_settings(), "quickbooks")


# ── load_settings(): .env handling ─────────────────────────────────────────────────────────────────
def _write_env(path: Path, values: dict[str, str], *, bom: bool = False) -> Path:
    text = "".join(f"{k}={v}\n" for k, v in values.items())
    path.write_text(("\ufeff" if bom else "") + text, encoding="utf-8")
    return path


BASE_FILE_ENV = {"QB_CLIENT_SECRET": "file-secret", "QB_REALM_ID": "999", "VSHIP_USER_NAME": "u",
                 "VSHIP_PASSWORD": "p"}


def test_env_file_wins_over_os_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = _write_env(tmp_path / "custom.env", {"QB_CLIENT_ID": "from-file", **BASE_FILE_ENV,
                                                    "ETA_STATE_DIR": str(tmp_path / "s")})
    monkeypatch.setenv("QB_CLIENT_ID", "from-os")
    monkeypatch.setenv("ETA_MAX_PARALLEL_BOOKINGS", "5")  # OS-only variables are still read
    settings = load_settings(env_file)
    assert settings.qbo is not None and settings.qbo.client_id == "from-file"
    assert settings.qbo.env_file == env_file
    assert settings.run.max_parallel_bookings == 5
    assert os.environ["QB_CLIENT_ID"] == "from-file"  # load_dotenv(override=True) semantics


def test_default_env_file_is_cwd_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path / ".env", {"QB_CLIENT_ID": "cwd-id", **BASE_FILE_ENV, "ETA_STATE_DIR": str(tmp_path / "s")})
    monkeypatch.chdir(tmp_path)
    settings = load_settings()
    assert settings.qbo is not None and settings.qbo.client_id == "cwd-id"
    assert settings.qbo.env_file == tmp_path / ".env"


def test_env_file_with_utf8_bom(tmp_path: Path) -> None:
    env_file = _write_env(tmp_path / "bom.env", {"QB_CLIENT_ID": "bom-id", **BASE_FILE_ENV,
                                                 "ETA_STATE_DIR": str(tmp_path / "s")}, bom=True)
    settings = load_settings(env_file)
    assert settings.qbo is not None and settings.qbo.client_id == "bom-id"


def test_explicit_missing_env_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Settings file not found"):
        load_settings(tmp_path / "nope.env")


def test_env_mapping_mode_reads_only_the_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_env(tmp_path / ".env", {"QB_CLIENT_ID": "file-id", **BASE_FILE_ENV})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VSHIP_USER_NAME", "os-user")
    before = dict(os.environ)
    settings = load_settings(env={"ETA_STATE_DIR": str(tmp_path / "s"), "VSHIP_PASSWORD": "p", "QB_CLIENT_ID": None})
    assert settings.qbo is None and settings.vship is None
    assert settings.missing["vship"] == ("VSHIP_USER_NAME",)
    assert dict(os.environ) == before  # nothing loaded into the process environment
    assert load_settings(tmp_path / "x.env", env={}).run.state_dir  # env_file need not exist in mapping mode


# ── secrets never shown ────────────────────────────────────────────────────────────────────────────
ALL_SECRETS = {
    "QB_CLIENT_SECRET": "S-qb-client-secret", "QB_REFRESH_TOKEN": "S-qb-refresh", "VSHIP_PASSWORD": "S-vship-pw",
    "MSC_ACCESS_TOKEN": "S-msc-token", "MSC_API_SUBSCRIPTION_KEY": "S-msc-key", "MAERSK_CLIENT_ID": "S-maersk-id",
    "MAERSK_CLIENT_SECRET": "S-maersk-secret", "CMA_API_KEY": "S-cma-key", "CMA_CLIENT_ID": "cma-id",
    "CMA_CLIENT_SECRET": "S-cma-secret", "AGG_API_TOKEN": "S-agg-token", "QB_EMAIL_STRATEGY": "smtp",
    "EMAIL_FROM": "a@example.com", "EMAIL_SMTP_HOST": "smtp.example.com", "EMAIL_SMTP_USERNAME": "user",
    "EMAIL_SMTP_PASSWORD": "S-smtp-pw", "EMAIL_GRAPH_CLIENT_SECRET": "S-graph-secret",
}


def test_repr_and_summary_contain_no_secrets(make_settings: MakeSettings) -> None:
    settings = make_settings(ALL_SECRETS)
    assert all(getattr(settings, name) is not None for name in ("qbo", "vship", "msc", "maersk", "cma",
                                                                 "aggregator", "email"))
    secrets = [v for v in ALL_SECRETS.values() if v.startswith("S-")]
    text = repr(settings)
    summary = settings_summary(settings)
    dumped = json.dumps(summary)
    for secret in secrets:
        assert secret not in text, secret
        assert secret not in dumped, secret
    assert summary["qbo"]["configured"] is True and summary["qbo"]["realm_id"] == "1234567890"
    assert summary["run"]["providers"] == {"MSC": "native", "Maersk": "native", "CMA": "native", "Grimaldi": "off"}


def test_summary_lists_missing_services(make_settings: MakeSettings) -> None:
    summary = settings_summary(make_settings())
    assert summary["cma"] == {"configured": False, "missing": ["CMA_API_KEY", "CMA_CLIENT_ID", "CMA_CLIENT_SECRET"]}
    assert summary["email"] == {"configured": False, "missing": []}
    json.dumps(summary)


def test_settings_defaults_allow_direct_construction(tmp_path: Path) -> None:
    settings = Settings(run=RunSettings(output_dir=tmp_path, state_dir=tmp_path))
    assert settings.qbo is None and settings.missing == {}
    kwargs: dict[str, Any] = {"api_token": "t", "state_file": tmp_path / "agg.json"}
    assert AggregatorSettings(**kwargs).scac[Carrier.CMA] == "CMDU"
