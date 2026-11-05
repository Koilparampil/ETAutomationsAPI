"""Settings + load_settings() + require() (03-foundations §12, 02-configuration).

Every value comes from environment variables, normally via `.env` (`load_dotenv(..., override=True)`:
`.env` wins over OS variables, as in the original). A service whose required variables are missing is
`None` and `Settings.missing[<service>]` names the missing variables; load_settings() never raises for
that. It raises ConfigError only for MALFORMED values (non-numeric counts, unknown modes, bad URLs, …),
and it validates every value even when the service it belongs to is not configured.
Empty values count as unset. Booleans accept 1/true/yes/on and 0/false/no/off (case-insensitive).
Secret fields are declared with repr=False so a printed Settings object never shows them.
"""

from __future__ import annotations

import logging
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .errors import ConfigError
from .models import Carrier

__all__ = [
    "AggregatorSettings",
    "CmaSettings",
    "EmailSettings",
    "MaerskSettings",
    "MscSettings",
    "QboSettings",
    "RunSettings",
    "SERVICE_NAMES",
    "Settings",
    "VShipSettings",
    "default_state_dir",
    "load_settings",
    "require",
    "settings_summary",
]

log = logging.getLogger("eta.config")

ProviderName: TypeAlias = Literal["native", "aggregator", "off"]
EmailStrategy: TypeAlias = Literal["template_swap", "graph", "smtp"]

SERVICE_NAMES = ("qbo", "vship", "msc", "maersk", "cma", "aggregator", "email")

# ── 02 §1 / 09 §D.1 ──
PROVIDER_CHOICES = ("native", "aggregator", "off")
DEFAULT_PROVIDERS: Mapping[Carrier, ProviderName] = {
    Carrier.MSC: "native",
    Carrier.MAERSK: "native",
    Carrier.CMA: "native",
    Carrier.GRIMALDI: "off",
}

# ── defaults copied from the integration specs ──
QBO_REQUIRED = ("QB_CLIENT_ID", "QB_CLIENT_SECRET", "QB_REALM_ID")
QBO_DEFAULT_REDIRECT_URI = "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"
QBO_DEFAULT_SCOPES = "com.intuit.quickbooks.accounting"
VSHIP_DEFAULT_API_BASE = "https://vship2000-prod-api.azurewebsites.net/api"
MSC_DEFAULT_GRAPHQL_URL = "https://services.mymsc.com/tracking/graphql"
MSC_SANDBOX_HOST = "portal.api.msc.com"
MAERSK_DEFAULT_BASE_URL = "https://api.maersk.com"
MAERSK_DEFAULT_TOKEN_URL = "https://api.maersk.com/customer-identity/oauth/v2/access_token"
CMA_DEFAULT_SCOPE = "tandtcommercial:read:be tandtpublic:read:be"
CMA_DEFAULT_TOKEN_URL = "https://auth.cma-cgm.com/as/token.oauth2"
CMA_DEFAULT_BASE_URL = "https://apis.cma-cgm.net/operation/trackandtrace/v1"
CMA_ALL_CREDENTIALS = ("CMA_API_KEY", "CMA_CLIENT_ID", "CMA_CLIENT_SECRET")
AGG_DEFAULT_BASE_URL = "https://api.shipsgo.com/v2"
AGG_DEFAULT_SCAC: Mapping[Carrier, str] = {
    Carrier.MSC: "MSCU",
    Carrier.MAERSK: "MAEU",
    Carrier.CMA: "CMDU",
    Carrier.GRIMALDI: "GRIU",
}
AGG_STATE_FILE_NAME = "aggregator_tracking.json"

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_BEARER_PREFIX = re.compile(r"(?i)^bearer(?:\s+|$)")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Dataclasses
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _default_providers() -> dict[Carrier, ProviderName]:
    return dict(DEFAULT_PROVIDERS)


def _default_scac() -> dict[Carrier, str]:
    return dict(AGG_DEFAULT_SCAC)


@dataclass(frozen=True)
class RunSettings:
    """Thresholds, directories, concurrency and per-carrier providers (02 §1, 09 §D.1)."""

    output_dir: Path
    state_dir: Path
    min_invoice_amount: Decimal = Decimal("1000")
    window_business_days: int = 6
    on_notif_lookup_error: Literal["send", "fail"] = "send"
    max_parallel_bookings: int = 8
    carrier_failure_limit: int = 5
    log_level: str = "INFO"
    providers: Mapping[Carrier, Literal["native", "aggregator", "off"]] = field(default_factory=_default_providers)


@dataclass(frozen=True)
class QboSettings:
    """QuickBooks Online (04 §3) including the e-mail options of 04b §3."""

    client_id: str
    client_secret: str = field(repr=False)
    realm_id: str
    refresh_token: str = field(default="", repr=False)   # seed; "" allowed if the TokenStore has one
    sandbox: bool = False
    minor_version: str = "75"
    eta_field_name: str = "ETA"
    eta_definition_id: str | None = None    # hint only — see 04 §6.4 resolution rules
    eta_field_type: str = "DateType"        # used only if no invoice reveals the field's Type
    eta_string_format: str = "%m/%d/%Y"     # used only if the field is StringType
    verify_updates: bool = True
    include_on_writes: bool = True
    query_chunk_size: int = 25
    max_concurrency: int = 4
    max_calls_per_minute: int = 450
    max_calls_per_second: int = 8
    docnumber_max_len: int = 21
    redirect_uri: str = QBO_DEFAULT_REDIRECT_URI
    oauth_scopes: str = QBO_DEFAULT_SCOPES
    env_file: Path | None = None            # the .env actually loaded (for refresh-token write-back)
    # ── 04b §3 ──
    email_strategy: Literal["template_swap", "graph", "smtp"] = "template_swap"
    email_require_verified: bool = True
    email_redirect_to: str | None = None
    test_send_to: str | None = None
    prefs_update_mode: Literal["full", "sparse"] = "full"
    mark_sent: bool = True
    email_max_per_minute: int = 25
    email_max_concurrency: int = 2

    @property
    def api_base(self) -> str:
        return ("https://sandbox-quickbooks.api.intuit.com" if self.sandbox
                else "https://quickbooks.api.intuit.com")


@dataclass(frozen=True)
class EmailSettings:
    """Company-mailbox e-mail for QB_EMAIL_STRATEGY=graph|smtp (04b §3)."""

    transport: Literal["graph", "smtp"]
    sender: str                                   # EMAIL_FROM
    reply_to: str | None = None                   # EMAIL_REPLY_TO
    bcc: tuple[str, ...] = ()                     # EMAIL_BCC (comma-separated)
    body_template: Path | None = None             # EMAIL_BODY_TEMPLATE
    include_pay_link: bool = True                 # EMAIL_INCLUDE_PAY_LINK
    attach_pdf: bool = True                       # EMAIL_ATTACH_PDF
    graph_tenant_id: str | None = None            # EMAIL_GRAPH_TENANT_ID      (graph: required)
    graph_client_id: str | None = None            # EMAIL_GRAPH_CLIENT_ID      (graph: required)
    graph_client_secret: str | None = field(default=None, repr=False)   # EMAIL_GRAPH_CLIENT_SECRET (graph)
    smtp_host: str | None = None                  # EMAIL_SMTP_HOST            (smtp: required)
    smtp_port: int = 587                          # EMAIL_SMTP_PORT
    smtp_username: str | None = None              # EMAIL_SMTP_USERNAME        (smtp: required)
    smtp_password: str | None = field(default=None, repr=False)         # EMAIL_SMTP_PASSWORD (smtp)
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"   # EMAIL_SMTP_SECURITY


@dataclass(frozen=True)
class VShipSettings:
    """VShipCRM (05 §3)."""

    username: str
    password: str = field(repr=False)
    api_base: str = VSHIP_DEFAULT_API_BASE
    max_concurrency: int = 4
    token_ttl_minutes: int = 55
    comment_type: int = 1


@dataclass(frozen=True)
class MscSettings:
    """MSC (06 §3.1). `mode` is resolved at load time ("auto" never appears here).

    The spec lists no defaults; the defaults below equal the env-var defaults (additive, eases tests).
    """

    mode: Literal["mymsc", "api"]
    # ── mymsc bridge ──
    user_email: str = ""                          # `mymsc-user-email` header ("" → header omitted)
    user_roles: str = "Customer"                  # `mymsc-user-roles` header
    graphql_url: str = MSC_DEFAULT_GRAPHQL_URL
    manual_token: str | None = field(default=None, repr=False)   # MSC_ACCESS_TOKEN ("Bearer " stripped)
    token_ttl_seconds: int = 1800                 # assumed lifetime when the token has no readable `exp`
    rate_per_minute: int = 120                    # GraphQL politeness limiter (0 = off)
    # ── api mode ──
    api_base_url: str | None = None               # no trailing slash
    api_key: str | None = field(default=None, repr=False)        # MSC_API_SUBSCRIPTION_KEY
    api_rate_per_minute: int = 0                  # 0 = off (5 on the Starter product)
    api_customs_hold_rule: bool = True
    allow_sandbox: bool = False
    # ── common ──
    rescue_fallback: bool = False
    max_concurrency: int = 4
    timeout_seconds: float = 30.0

    @property
    def api_is_sandbox(self) -> bool:
        """True when urlsplit(api_base_url).hostname == "portal.api.msc.com"."""
        if not self.api_base_url:
            return False
        return (urlsplit(self.api_base_url).hostname or "").lower() == MSC_SANDBOX_HOST


@dataclass(frozen=True)
class MaerskSettings:
    """Maersk Track & Trace Events API (07 §3). Configured only when both credentials are non-empty."""

    client_id: str = field(repr=False)             # MAERSK_CLIENT_ID (Consumer-Key header — a key)
    client_secret: str = field(repr=False)         # MAERSK_CLIENT_SECRET
    base_url: str = MAERSK_DEFAULT_BASE_URL
    token_url: str = MAERSK_DEFAULT_TOKEN_URL
    api_version: str | None = None                 # sent as API-Version header only when set
    eta_target: Literal["final", "pod"] = "final"
    multi_container_policy: Literal["first", "latest"] = "first"
    eta_date_only: bool = True
    page_limit: int = 100
    max_pages: int = 10
    max_concurrency: int = 3
    rate_per_minute: int = 0                       # 0 = no client-side rate limiting
    timeout_s: float = 30.0


@dataclass(frozen=True)
class CmaSettings:
    """CMA CGM Track & Trace API (08 §3). `auth_mode` is resolved at load time.

    The spec lists no defaults; the defaults below equal the env-var defaults (additive, eases tests).
    """

    auth_mode: Literal["apikey", "oauth"]
    api_key: str | None = field(default=None, repr=False)          # CMA_API_KEY
    client_id: str | None = None                                    # CMA_CLIENT_ID
    client_secret: str | None = field(default=None, repr=False)    # CMA_CLIENT_SECRET
    oauth_scope: str = CMA_DEFAULT_SCOPE                            # CMA_OAUTH_SCOPE
    token_url: str = CMA_DEFAULT_TOKEN_URL                          # CMA_TOKEN_URL
    base_url: str = CMA_DEFAULT_BASE_URL                            # CMA_BASE_URL (no trailing slash)
    lookup_mode: Literal["reference", "dcsa_query"] = "reference"   # CMA_LOOKUP_MODE
    page_limit: int = 500                                           # CMA_PAGE_LIMIT
    max_pages: int = 10                                             # CMA_MAX_PAGES
    behalf_of: str | None = None                                    # CMA_BEHALF_OF
    max_concurrency: int = 2                                        # CMA_MAX_CONCURRENCY
    max_calls_per_minute: int | None = None                         # CMA_MAX_CALLS_PER_MINUTE
    max_retry_after_s: int = 120                                    # CMA_MAX_RETRY_AFTER_S
    timeout_s: float = 30.0                                         # CMA_TIMEOUT_S


@dataclass(frozen=True)
class AggregatorSettings:
    """Optional tracking aggregator, ShipsGo API v2 (09 §B.4). Enabled only when AGG_API_TOKEN is set."""

    api_token: str = field(repr=False)             # AGG_API_TOKEN
    state_file: Path                               # derived: <ETA_STATE_DIR>/aggregator_tracking.json
    provider: str = "shipsgo"                      # AGG_PROVIDER — only "shipsgo" is implemented
    base_url: str = AGG_DEFAULT_BASE_URL           # AGG_BASE_URL
    max_concurrency: int = 2                       # AGG_MAX_CONCURRENCY
    rate_per_minute: int = 90                      # AGG_RATE_PER_MINUTE
    timeout_s: float = 30.0                        # AGG_TIMEOUT_SECONDS
    allow_create: bool = True                      # AGG_ALLOW_CREATE
    max_creates_per_run: int = 100                 # AGG_MAX_CREATES_PER_RUN
    first_lookup_wait_s: int = 45                  # AGG_FIRST_LOOKUP_WAIT_S
    poll_interval_s: int = 15                      # AGG_POLL_INTERVAL_S
    fresh_minutes: int = 30                        # AGG_FRESH_MINUTES
    scac: Mapping[Carrier, str] = field(default_factory=_default_scac)   # AGG_SCAC_<CARRIER>


@dataclass(frozen=True)
class Settings:
    """Everything load_settings() produced (03 §12). A service is None when its required vars are missing."""

    run: RunSettings
    qbo: QboSettings | None = None
    vship: VShipSettings | None = None
    msc: MscSettings | None = None
    maersk: MaerskSettings | None = None
    cma: CmaSettings | None = None
    aggregator: AggregatorSettings | None = None
    email: EmailSettings | None = None
    missing: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Typed access to one environment mapping
# ════════════════════════════════════════════════════════════════════════════════════════════════
class _Env:
    """Reads one mapping of variables; empty / whitespace-only values count as unset."""

    def __init__(self, source: Mapping[str, str]) -> None:
        self._src = source

    def present(self, name: str) -> bool:
        return name in self._src

    def text(self, name: str, default: str | None = None, *, strip: bool = True) -> str | None:
        raw = self._src.get(name)
        if raw is None or not raw.strip():
            return default
        return raw.strip() if strip else raw

    def flag(self, name: str, default: bool) -> bool:
        value = self.text(name)
        if value is None:
            return default
        low = value.lower()
        if low in _TRUE:
            return True
        if low in _FALSE:
            return False
        raise ConfigError(f"{name} must be one of 1/true/yes/on or 0/false/no/off (got {value!r})")

    def integer(self, name: str, default: int | None, *, minimum: int | None = None,
                maximum: int | None = None) -> int | None:
        value = self.text(name)
        if value is None:
            return default
        try:
            number = int(value, 10)
        except ValueError:
            raise ConfigError(f"{name} must be a whole number (got {value!r})") from None
        _check_range(name, number, minimum, maximum)
        return number

    def number(self, name: str, default: float, *, minimum: float | None = None,
               positive: bool = False) -> float:
        value = self.text(name)
        if value is None:
            return default
        try:
            number = float(value)
        except ValueError:
            raise ConfigError(f"{name} must be a number (got {value!r})") from None
        if not math.isfinite(number):
            raise ConfigError(f"{name} must be a finite number (got {value!r})")
        if positive and not number > 0:
            raise ConfigError(f"{name} must be greater than 0 (got {value!r})")
        _check_range(name, number, minimum, None)
        return number

    def choice(self, name: str, default: str, choices: tuple[str, ...]) -> str:
        value = self.text(name)
        if value is None:
            return default
        low = value.lower()
        if low not in choices:
            raise ConfigError(f"{name} must be one of {', '.join(choices)} (got {value!r})")
        return low

    def url(self, name: str, default: str | None, *, https_only: bool = False,
            strip_slash: bool = False) -> str | None:
        value = self.text(name)
        if value is None:
            return default
        allowed = ("https",) if https_only else ("http", "https")
        need = "an https:// URL" if https_only else "an http(s):// URL"
        try:
            parts = urlsplit(value)
            valid = parts.scheme.lower() in allowed and bool(parts.netloc) and parts.hostname is not None
        except ValueError:  # e.g. an unbalanced IPv6 bracket
            valid = False
        if not valid:
            raise ConfigError(f"{name} must be {need} (got {value!r})")
        return value.rstrip("/") if strip_slash else value

    def path(self, name: str, default: Path | None = None) -> Path | None:
        value = self.text(name)
        return Path(value).expanduser() if value is not None else default


def _check_range(name: str, number: float, minimum: float | None, maximum: float | None) -> None:
    if minimum is not None and maximum is not None and not minimum <= number <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum} (got {number})")
    if minimum is not None and number < minimum:
        raise ConfigError(f"{name} must be >= {minimum} (got {number})")
    if maximum is not None and number > maximum:
        raise ConfigError(f"{name} must be <= {maximum} (got {number})")


def _req_int(e: _Env, name: str, default: int, *, minimum: int | None = None,
             maximum: int | None = None) -> int:
    return cast(int, e.integer(name, default, minimum=minimum, maximum=maximum))


def _req_url(e: _Env, name: str, default: str, *, https_only: bool = False, strip_slash: bool = False) -> str:
    return cast(str, e.url(name, default, https_only=https_only, strip_slash=strip_slash))


def _missing_names(pairs: tuple[tuple[str, str | None], ...]) -> tuple[str, ...]:
    return tuple(name for name, value in pairs if not value)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Per-section builders
# ════════════════════════════════════════════════════════════════════════════════════════════════
def default_state_dir(env: Mapping[str, str] | None = None, *, windows: bool | None = None) -> Path:
    """02 §1: Windows %LOCALAPPDATA%\\ETAutomationsAPI; else $XDG_STATE_HOME/eta_automations or
    ~/.local/state/eta_automations."""
    source = os.environ if env is None else env
    is_windows = (os.name == "nt") if windows is None else windows
    home_value = (source.get("USERPROFILE") if is_windows else None) or source.get("HOME")
    home = Path(home_value) if home_value else Path.home()
    if is_windows:
        local = (source.get("LOCALAPPDATA") or "").strip()
        base = Path(local) if local else home / "AppData" / "Local"
        return base / "ETAutomationsAPI"
    xdg = (source.get("XDG_STATE_HOME") or "").strip()
    if xdg and Path(xdg).is_absolute():  # XDG: relative values are invalid and ignored
        return Path(xdg) / "eta_automations"
    return home / ".local" / "state" / "eta_automations"


def _amount(e: _Env, name: str, default: Decimal) -> Decimal:
    value = e.text(name)
    if value is None:
        return default
    try:
        amount = Decimal(value.replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        raise ConfigError(f"{name} must be an amount such as 1000 (got {value!r})") from None
    if not amount.is_finite() or amount < 0:
        raise ConfigError(f"{name} must be a non-negative amount (got {value!r})")
    return amount


def _log_level(e: _Env) -> str:
    value = e.text("ETA_LOG_LEVEL")
    if value is None:
        return "INFO"
    name = value.upper()
    if name not in logging.getLevelNamesMapping():
        raise ConfigError(f"ETA_LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL (got {value!r})")
    return name


def _providers(e: _Env) -> dict[Carrier, ProviderName]:
    providers: dict[Carrier, ProviderName] = {}
    for carrier, default in DEFAULT_PROVIDERS.items():
        name = f"ETA_PROVIDER_{carrier.name}"
        value = e.text(name)
        if value is None:
            providers[carrier] = default
            continue
        low = value.lower()
        if low not in PROVIDER_CHOICES:
            # exact message from 09 §D.1
            raise ConfigError(f"ETA_PROVIDER_{carrier.name} must be one of native, aggregator, off (got {value!r})")
        providers[carrier] = cast(ProviderName, low)
    return providers


def _run_settings(e: _Env, source: Mapping[str, str]) -> RunSettings:
    output_dir = e.path("ETA_OUTPUT_DIR") or Path.cwd()
    state_dir = e.path("ETA_STATE_DIR") or default_state_dir(source)
    return RunSettings(
        output_dir=output_dir,
        state_dir=state_dir,
        min_invoice_amount=_amount(e, "ETA_MIN_INVOICE_AMOUNT", Decimal("1000")),
        window_business_days=_req_int(e, "ETA_WINDOW_BUSINESS_DAYS", 6, minimum=0),
        on_notif_lookup_error=cast(Literal["send", "fail"],
                                   e.choice("ETA_ON_NOTIF_LOOKUP_ERROR", "send", ("send", "fail"))),
        max_parallel_bookings=_req_int(e, "ETA_MAX_PARALLEL_BOOKINGS", 8, minimum=1),
        carrier_failure_limit=_req_int(e, "ETA_CARRIER_FAILURE_LIMIT", 5, minimum=1),
        log_level=_log_level(e),
        providers=_providers(e),
    )


def _qbo_settings(e: _Env, env_file: Path | None, strategy: EmailStrategy,
                  missing: dict[str, tuple[str, ...]]) -> QboSettings | None:
    client_id = e.text("QB_CLIENT_ID")
    client_secret = e.text("QB_CLIENT_SECRET")
    realm_id = e.text("QB_REALM_ID")
    minor_version = _req_int(e, "QB_MINOR_VERSION", 75, minimum=1)
    options: dict[str, Any] = {
        "refresh_token": e.text("QB_REFRESH_TOKEN", ""),
        "sandbox": e.flag("QB_SANDBOX", False),
        "minor_version": str(minor_version),
        "eta_field_name": e.text("QB_ETA_FIELD_NAME", "ETA"),
        "eta_definition_id": e.text("QB_ETA_DEFINITION_ID"),
        "eta_field_type": e.text("QB_ETA_FIELD_TYPE", "DateType"),
        "eta_string_format": e.text("QB_ETA_STRING_FORMAT", "%m/%d/%Y"),
        "verify_updates": e.flag("QB_VERIFY_UPDATES", True),
        "include_on_writes": e.flag("QB_INCLUDE_ON_WRITES", True),
        "query_chunk_size": _req_int(e, "QB_QUERY_CHUNK_SIZE", 25, minimum=1),
        "max_concurrency": _req_int(e, "QB_MAX_CONCURRENCY", 4, minimum=1),
        "max_calls_per_minute": _req_int(e, "QB_MAX_CALLS_PER_MINUTE", 450, minimum=1),
        "max_calls_per_second": _req_int(e, "QB_MAX_CALLS_PER_SECOND", 8, minimum=1),
        "docnumber_max_len": _req_int(e, "QB_DOCNUMBER_MAX_LEN", 21, minimum=1),
        "redirect_uri": _req_url(e, "QB_REDIRECT_URI", QBO_DEFAULT_REDIRECT_URI),
        "oauth_scopes": e.text("QB_OAUTH_SCOPES", QBO_DEFAULT_SCOPES),
        "env_file": env_file,
        # 04b §3
        "email_strategy": strategy,
        "email_require_verified": e.flag("QB_EMAIL_REQUIRE_VERIFIED", True),
        "email_redirect_to": e.text("QB_EMAIL_REDIRECT_TO"),
        "test_send_to": e.text("QB_TEST_SEND_TO"),
        "prefs_update_mode": e.choice("QB_PREFS_UPDATE_MODE", "full", ("full", "sparse")),
        "mark_sent": e.flag("QB_MARK_SENT", True),
        "email_max_per_minute": _req_int(e, "QB_EMAIL_MAX_PER_MINUTE", 25, minimum=1),
        "email_max_concurrency": _req_int(e, "QB_EMAIL_MAX_CONCURRENCY", 2, minimum=1),
    }
    names = _missing_names((("QB_CLIENT_ID", client_id), ("QB_CLIENT_SECRET", client_secret),
                            ("QB_REALM_ID", realm_id)))
    if names:
        missing["qbo"] = names
        return None
    return QboSettings(client_id=cast(str, client_id), client_secret=cast(str, client_secret),
                       realm_id=cast(str, realm_id), **options)


def _split_addresses(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for part in re.split(r"[,;]", value):
        address = part.strip()
        if address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    return tuple(out)


def _email_settings(e: _Env, strategy: EmailStrategy,
                    missing: dict[str, tuple[str, ...]]) -> EmailSettings | None:
    sender = e.text("EMAIL_FROM")
    reply_to = e.text("EMAIL_REPLY_TO")
    bcc = _split_addresses(e.text("EMAIL_BCC"))
    body_template = e.path("EMAIL_BODY_TEMPLATE")
    include_pay_link = e.flag("EMAIL_INCLUDE_PAY_LINK", True)
    attach_pdf = e.flag("EMAIL_ATTACH_PDF", True)
    tenant = e.text("EMAIL_GRAPH_TENANT_ID")
    graph_client_id = e.text("EMAIL_GRAPH_CLIENT_ID")
    graph_secret = e.text("EMAIL_GRAPH_CLIENT_SECRET")
    smtp_host = e.text("EMAIL_SMTP_HOST")
    smtp_port = _req_int(e, "EMAIL_SMTP_PORT", 587, minimum=1, maximum=65535)
    smtp_username = e.text("EMAIL_SMTP_USERNAME")
    smtp_password = e.text("EMAIL_SMTP_PASSWORD", strip=False)  # user-chosen password: keep as typed
    smtp_security = e.choice("EMAIL_SMTP_SECURITY", "starttls", ("starttls", "ssl", "none"))
    if strategy not in ("graph", "smtp"):
        return None  # template_swap sends through QuickBooks; no mailbox settings needed
    if strategy == "graph":
        required = (("EMAIL_FROM", sender), ("EMAIL_GRAPH_TENANT_ID", tenant),
                    ("EMAIL_GRAPH_CLIENT_ID", graph_client_id), ("EMAIL_GRAPH_CLIENT_SECRET", graph_secret))
    else:
        required = (("EMAIL_FROM", sender), ("EMAIL_SMTP_HOST", smtp_host),
                    ("EMAIL_SMTP_USERNAME", smtp_username), ("EMAIL_SMTP_PASSWORD", smtp_password))
    names = _missing_names(required)
    if names:
        missing["email"] = names
        return None
    return EmailSettings(
        transport=strategy,
        sender=cast(str, sender),
        reply_to=reply_to,
        bcc=bcc,
        body_template=body_template,
        include_pay_link=include_pay_link,
        attach_pdf=attach_pdf,
        graph_tenant_id=tenant,
        graph_client_id=graph_client_id,
        graph_client_secret=graph_secret,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username,
        smtp_password=smtp_password,
        smtp_security=cast(Literal["starttls", "ssl", "none"], smtp_security),
    )


def _vship_settings(e: _Env, missing: dict[str, tuple[str, ...]]) -> VShipSettings | None:
    username = e.text("VSHIP_USER_NAME")
    password = e.text("VSHIP_PASSWORD", strip=False)  # sent as typed (parity with the original)
    api_base = _req_url(e, "VSHIP_API_BASE", VSHIP_DEFAULT_API_BASE, strip_slash=True)
    max_concurrency = _req_int(e, "VSHIP_MAX_CONCURRENCY", 4, minimum=1)
    token_ttl_minutes = _req_int(e, "VSHIP_TOKEN_TTL_MINUTES", 55, minimum=1)
    comment_type = _req_int(e, "VSHIP_COMMENT_TYPE", 1, minimum=0)
    names = _missing_names((("VSHIP_USER_NAME", username), ("VSHIP_PASSWORD", password)))
    if names:
        missing["vship"] = names
        return None
    return VShipSettings(username=cast(str, username), password=cast(str, password), api_base=api_base,
                         max_concurrency=max_concurrency, token_ttl_minutes=token_ttl_minutes,
                         comment_type=comment_type)


def _strip_bearer(value: str | None) -> str | None:
    """MSC_ACCESS_TOKEN may be pasted with or without the "Bearer " prefix (06 §2.2)."""
    if value is None:
        return None
    token = _BEARER_PREFIX.sub("", value.strip(), count=1).strip()
    return token or None


def _msc_settings(e: _Env, missing: dict[str, tuple[str, ...]]) -> MscSettings | None:
    requested = e.choice("MSC_MODE", "auto", ("auto", "mymsc", "api"))
    api_base_url = e.url("MSC_API_BASE_URL", None, strip_slash=True)
    api_key = e.text("MSC_API_SUBSCRIPTION_KEY")
    common: dict[str, Any] = {
        # MSC_USER_EMAIL defaults to VSHIP_USER_NAME, then MSC_USER_NAME (parity, 06 §3.2)
        "user_email": e.text("MSC_USER_EMAIL") or e.text("VSHIP_USER_NAME") or e.text("MSC_USER_NAME") or "",
        "user_roles": e.text("MSC_USER_ROLES", "Customer"),
        "graphql_url": _req_url(e, "MSC_GRAPHQL_URL", MSC_DEFAULT_GRAPHQL_URL),
        "manual_token": _strip_bearer(e.text("MSC_ACCESS_TOKEN")),
        "token_ttl_seconds": _req_int(e, "MSC_TOKEN_TTL_SECONDS", 1800, minimum=1),
        "rate_per_minute": _req_int(e, "MSC_RATE_PER_MINUTE", 120, minimum=0),
        "api_base_url": api_base_url,
        "api_key": api_key,
        "api_rate_per_minute": _req_int(e, "MSC_API_RATE_PER_MINUTE", 0, minimum=0),
        "api_customs_hold_rule": e.flag("MSC_API_CUSTOMS_HOLD_RULE", True),
        "allow_sandbox": e.flag("MSC_ALLOW_SANDBOX", False),
        "rescue_fallback": e.flag("MSC_RESCUE_FALLBACK", False),
        "max_concurrency": _req_int(e, "MSC_MAX_CONCURRENCY", 4, minimum=1),
        "timeout_seconds": e.number("MSC_TIMEOUT_SECONDS", 30.0, positive=True),
    }
    if requested == "auto":
        mode: Literal["mymsc", "api"] = "api" if (api_base_url and api_key) else "mymsc"
    elif requested == "api":
        is_sandbox = bool(api_base_url) and (urlsplit(cast(str, api_base_url)).hostname or "").lower() == MSC_SANDBOX_HOST
        names: list[str] = []
        if not api_base_url:
            names.append("MSC_API_BASE_URL")
        if not api_key and not is_sandbox:  # the key is exempt for the public sandbox (06 §3.2)
            names.append("MSC_API_SUBSCRIPTION_KEY")
        if names:
            missing["msc"] = tuple(names)
            return None
        mode = "api"
    else:
        mode = "mymsc"
    return MscSettings(mode=mode, **common)


def _maersk_settings(e: _Env, missing: dict[str, tuple[str, ...]]) -> MaerskSettings | None:
    client_id = e.text("MAERSK_CLIENT_ID")
    client_secret = e.text("MAERSK_CLIENT_SECRET")
    options: dict[str, Any] = {
        "base_url": _req_url(e, "MAERSK_BASE_URL", MAERSK_DEFAULT_BASE_URL, strip_slash=True),
        "token_url": _req_url(e, "MAERSK_TOKEN_URL", MAERSK_DEFAULT_TOKEN_URL),
        "api_version": e.text("MAERSK_API_VERSION"),
        "eta_target": e.choice("MAERSK_ETA_TARGET", "final", ("final", "pod")),
        "multi_container_policy": e.choice("MAERSK_MULTI_CONTAINER_POLICY", "first", ("first", "latest")),
        "eta_date_only": e.flag("MAERSK_ETA_DATE_ONLY", True),
        "page_limit": _req_int(e, "MAERSK_PAGE_LIMIT", 100, minimum=1, maximum=100),
        "max_pages": _req_int(e, "MAERSK_MAX_PAGES", 10, minimum=1),
        "max_concurrency": _req_int(e, "MAERSK_MAX_CONCURRENCY", 3, minimum=1),
        "rate_per_minute": _req_int(e, "MAERSK_RATE_PER_MINUTE", 0, minimum=0),
        "timeout_s": e.number("MAERSK_TIMEOUT_SECONDS", 30.0, minimum=1),
    }
    names = _missing_names((("MAERSK_CLIENT_ID", client_id), ("MAERSK_CLIENT_SECRET", client_secret)))
    if names:
        missing["maersk"] = names
        return None
    return MaerskSettings(client_id=cast(str, client_id), client_secret=cast(str, client_secret), **options)


def _cma_settings(e: _Env, missing: dict[str, tuple[str, ...]]) -> CmaSettings | None:
    requested = e.choice("CMA_AUTH_MODE", "auto", ("auto", "apikey", "oauth"))
    api_key = e.text("CMA_API_KEY")
    client_id = e.text("CMA_CLIENT_ID")
    client_secret = e.text("CMA_CLIENT_SECRET")
    options: dict[str, Any] = {
        "api_key": api_key,
        "client_id": client_id,
        "client_secret": client_secret,
        "oauth_scope": e.text("CMA_OAUTH_SCOPE", CMA_DEFAULT_SCOPE),
        "token_url": _req_url(e, "CMA_TOKEN_URL", CMA_DEFAULT_TOKEN_URL, https_only=True),
        "base_url": _req_url(e, "CMA_BASE_URL", CMA_DEFAULT_BASE_URL, https_only=True, strip_slash=True),
        "lookup_mode": e.choice("CMA_LOOKUP_MODE", "reference", ("reference", "dcsa_query")),
        "page_limit": _req_int(e, "CMA_PAGE_LIMIT", 500, minimum=1, maximum=500),
        "max_pages": _req_int(e, "CMA_MAX_PAGES", 10, minimum=1),
        "behalf_of": e.text("CMA_BEHALF_OF"),
        "max_concurrency": _req_int(e, "CMA_MAX_CONCURRENCY", 2, minimum=1),
        "max_calls_per_minute": e.integer("CMA_MAX_CALLS_PER_MINUTE", None, minimum=1),
        "max_retry_after_s": _req_int(e, "CMA_MAX_RETRY_AFTER_S", 120, minimum=0),
        "timeout_s": e.number("CMA_TIMEOUT_S", 30.0, positive=True),
    }
    # Resolution rules (08 §3)
    if requested == "auto":
        if client_id and client_secret:
            mode: Literal["apikey", "oauth"] = "oauth"
        elif api_key:
            mode = "apikey"
        else:
            missing["cma"] = CMA_ALL_CREDENTIALS
            return None
    elif requested == "apikey":
        if not api_key:
            missing["cma"] = ("CMA_API_KEY",)
            return None
        mode = "apikey"
    else:
        names = _missing_names((("CMA_CLIENT_ID", client_id), ("CMA_CLIENT_SECRET", client_secret)))
        if names:
            missing["cma"] = names
            return None
        mode = "oauth"
    return CmaSettings(auth_mode=mode, **options)


def _aggregator_settings(e: _Env, state_dir: Path,
                         missing: dict[str, tuple[str, ...]]) -> AggregatorSettings | None:
    api_token = e.text("AGG_API_TOKEN")
    provider_raw = cast(str, e.text("AGG_PROVIDER", "shipsgo"))
    if provider_raw.lower() != "shipsgo":
        raise ConfigError(f"AGG_PROVIDER={provider_raw} is described in 09 §B.9 but not implemented")
    options: dict[str, Any] = {
        "provider": "shipsgo",
        "base_url": _req_url(e, "AGG_BASE_URL", AGG_DEFAULT_BASE_URL, strip_slash=True),
        "max_concurrency": _req_int(e, "AGG_MAX_CONCURRENCY", 2, minimum=1),
        "rate_per_minute": _req_int(e, "AGG_RATE_PER_MINUTE", 90, minimum=1),
        "timeout_s": e.number("AGG_TIMEOUT_SECONDS", 30.0, positive=True),
        "allow_create": e.flag("AGG_ALLOW_CREATE", True),
        "max_creates_per_run": _req_int(e, "AGG_MAX_CREATES_PER_RUN", 100, minimum=0),
        "first_lookup_wait_s": _req_int(e, "AGG_FIRST_LOOKUP_WAIT_S", 45, minimum=0),
        "poll_interval_s": _req_int(e, "AGG_POLL_INTERVAL_S", 15, minimum=0),
        "fresh_minutes": _req_int(e, "AGG_FRESH_MINUTES", 30, minimum=0),
        "scac": {carrier: cast(str, e.text(f"AGG_SCAC_{carrier.name}", default)).upper()
                 for carrier, default in AGG_DEFAULT_SCAC.items()},
    }
    if not api_token:
        missing["aggregator"] = ("AGG_API_TOKEN",)
        return None
    return AggregatorSettings(api_token=api_token, state_file=state_dir / AGG_STATE_FILE_NAME, **options)


def _log_obsolete(e: _Env) -> None:
    """02 §3 / 04 §3 / 06 §3.2: mention obsolete variables once per load."""
    if e.present("QB") or e.present("QB_HEADLESS"):
        log.info("QB and QB_HEADLESS are no longer used (no browser) — you can delete them from .env")
    legacy = [name for name in ("MSC_PASSWORD", "LOGIN_URL", "HOME_PAGE") if e.present(name)]
    if legacy:
        log.debug("%s %s no longer used: this program does not automate the myMSC sign-in (06 §1) — "
                  "you can delete %s from .env", ", ".join(legacy), "is" if len(legacy) == 1 else "are",
                  "it" if len(legacy) == 1 else "them")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Public API
# ════════════════════════════════════════════════════════════════════════════════════════════════
def load_settings(env_file: Path | str | None = None, *, env: Mapping[str, Any] | None = None) -> Settings:
    """Build Settings (03 §12).

    - `env` given (tests): read ONLY that mapping; no .env loading, os.environ untouched. `env_file`, if
      given, is only recorded as QboSettings.env_file (refresh-token write-back target).
    - otherwise: `load_dotenv(env_file or ./.env, override=True)` (an explicitly given env_file must
      exist), then read os.environ.
    Never raises for a missing service; raises ConfigError only for malformed values.
    """
    if env is None:
        path = Path(env_file).expanduser() if env_file is not None else Path.cwd() / ".env"
        if env_file is not None and not path.is_file():
            raise ConfigError(f"Settings file not found: {path}")
        # utf-8-sig: tolerate a BOM written by Windows editors
        load_dotenv(path, override=True, encoding="utf-8-sig")
        source: dict[str, str] = dict(os.environ)
        recorded_env_file: Path | None = path.absolute()
    else:
        source = {str(k): str(v) for k, v in env.items() if v is not None}
        recorded_env_file = Path(env_file).expanduser().absolute() if env_file is not None else None

    e = _Env(source)
    missing: dict[str, tuple[str, ...]] = {}
    run = _run_settings(e, source)
    strategy = cast(EmailStrategy, e.choice("QB_EMAIL_STRATEGY", "template_swap", ("template_swap", "graph", "smtp")))
    settings = Settings(
        run=run,
        qbo=_qbo_settings(e, recorded_env_file, strategy, missing),
        vship=_vship_settings(e, missing),
        msc=_msc_settings(e, missing),
        maersk=_maersk_settings(e, missing),
        cma=_cma_settings(e, missing),
        aggregator=_aggregator_settings(e, run.state_dir, missing),
        email=_email_settings(e, strategy, missing),
        missing=missing,
    )
    _log_obsolete(e)
    return settings


def require(settings: Settings, *services: str) -> None:
    """Raise ConfigError listing, for every requested service that is None, all of its missing env
    vars, e.g. require(s, "qbo", "vship") at the start of `eta run` (03 §12)."""
    problems: list[str] = []
    for name in services:
        if name not in SERVICE_NAMES:
            raise ValueError(f"unknown service {name!r}; expected one of {', '.join(SERVICE_NAMES)}")
        if getattr(settings, name) is not None:
            continue
        names = tuple(settings.missing.get(name, ()))
        if names == CMA_ALL_CREDENTIALS:
            needs = "CMA_API_KEY (or CMA_CLIENT_ID + CMA_CLIENT_SECRET)"
        elif names:
            needs = ", ".join(names)
        elif name == "email":
            needs = "QB_EMAIL_STRATEGY=graph or smtp and its EMAIL_* settings"
        else:
            needs = "its settings"
        problems.append(f"{name} needs {needs}")
    if problems:
        raise ConfigError("Missing required settings: " + "; ".join(problems)
                          + ". Set them in .env (see .env.example).")


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in fields(value) if f.repr}
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (Path, Decimal)):
        return str(value)
    if isinstance(value, Mapping):
        return {str(_plain(k)): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def settings_summary(settings: Settings) -> dict[str, Any]:
    """JSON-serializable summary WITHOUT secrets (repr=False fields are left out) for run reports (10 §7)."""
    summary: dict[str, Any] = {"run": _plain(settings.run)}
    for name in SERVICE_NAMES:
        value = getattr(settings, name)
        if value is None:
            summary[name] = {"configured": False, "missing": list(settings.missing.get(name, ()))}
        else:
            summary[name] = {"configured": True, **_plain(value)}
    return summary
