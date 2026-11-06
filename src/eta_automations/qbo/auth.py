"""QuickBooks Online OAuth 2.0 (04-quickbooks-online §5).

- `QboAuth` (a `auth.TokenProvider`, 03 §8): chooses the refresh token to use (04 §5.2 seeding rules),
  refreshes the access token, persists a rotated refresh token to the TokenStore FIRST and only then
  writes it back to `.env`, never deletes state on `invalid_grant` (04 §5.3) and warns 30 days before the
  connection expires. It owns its own `httpx.AsyncClient` (no base URL) and calls `http.request()`
  directly: it NEVER acquires the QBO semaphore or rate limiter (03 §8), so a caller that already holds a
  QBO slot can always obtain a token.
- `run_authorization_code_flow()` (`eta qbo-auth`, 04 §5.4): paste-back by default (works with production
  keys); optional one-shot loopback listener for development/sandbox keys. The user signs in and consents
  in their own browser — nothing is automated.

Token values are never logged or printed.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import math
import os
import secrets
import time
import webbrowser
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import httpx
from dotenv import set_key

from ..clock import Clock, SystemClock
from ..config import QboSettings
from ..errors import AuthError, ConfigError
from ..http import new_async_client, request
from ..logging_setup import redact
from ..prompt import read_line
from ..token_store import TokenStore, is_fresh, jwt_exp

__all__ = [
    "AUTHORIZE_URL",
    "QboAuth",
    "REVOKE_URL",
    "STORE_KEY",
    "TOKEN_URL",
    "authorization_url",
    "build_token_entry",
    "parse_callback",
    "run_authorization_code_flow",
    "write_env_value",
]

log = logging.getLogger("eta.qbo")

# ── 04 §4 E1 / §5.4 ──
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
AUTHORIZE_URL = "https://appcenter.intuit.com/connect/oauth2"
# Not used by the tool; documented for manual revocation (04 §4).
REVOKE_URL = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

STORE_KEY = "qbo"                       # TokenStore entry (04 §5.2)
ENV_REFRESH_TOKEN = "QB_REFRESH_TOKEN"
ENV_REALM_ID = "QB_REALM_ID"
OAUTH_SERVICE = "qbo-oauth"
DEFAULT_ACCESS_TTL_S = 3600.0           # 04 §5.1: expires_in is 3600 s
FRESH_SKEW_S = 120                      # 04 §5.2: is_fresh(entry, skew_seconds=120)
EXPIRY_WARNING_S = 30 * 86400           # 04 §5.2 step 5: warn when < 30 days remain
CALLBACK_TIMEOUT_S = 300.0              # 04 §5.4 step 5 (listen mode)
_KNOWN_EXPIRY_KEYS = frozenset({"expires_in", "x_refresh_token_expires_in"})

NO_REFRESH_TOKEN_MESSAGE = "No QuickBooks refresh token. Run `eta qbo-auth` or set QB_REFRESH_TOKEN in .env"
INVALID_GRANT_MESSAGE = (
    "QuickBooks rejected the refresh token (invalid_grant). Reconnect with `eta qbo-auth` (or the OAuth "
    "Playground) and put the new token in QB_REFRESH_TOKEN. Do not delete tokens.json."
)
INVALID_CLIENT_MESSAGE = "QB_CLIENT_ID / QB_CLIENT_SECRET were rejected (invalid_client)"
PASTE_INSTRUCTIONS = (
    "After approving, your browser lands on a page whose address contains `code=` and `realmId=`. "
    "Copy the entire address bar and paste it here:"
)
CALLBACK_PAGE = (
    "<!doctype html><html><head><meta charset=\"utf-8\"><title>ETAutomationsAPI</title></head>"
    "<body><p>QuickBooks authorization received. You can close this tab.</p></body></html>"
)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Shared helpers (refresh + authorization-code exchange)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _basic_auth(settings: QboSettings) -> str:
    """04 §5.1: Authorization: Basic base64("{client_id}:{client_secret}")."""
    raw = f"{settings.client_id}:{settings.client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _token_headers(settings: QboSettings) -> dict[str, str]:
    return {
        "Authorization": _basic_auth(settings),
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _json_or_empty(resp: httpx.Response) -> dict[str, Any]:
    """Tolerant JSON parse for token-endpoint answers (error pages may not be JSON)."""
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _short(text: Any) -> str:
    """First line of an OAuth error description, redacted (never a token)."""
    lines = str(text or "").strip().splitlines()
    return redact(lines[0].strip()) if lines else ""


def build_token_entry(
    body: Mapping[str, Any],
    *,
    realm_id: str,
    seed: str,
    previous: Mapping[str, Any] | None,
    now: float,
) -> dict[str, Any]:
    """Build the TokenStore "qbo" entry (04 §5.2) from a 200 token-endpoint response.

    `previous` is the entry the refresh started from (its refresh token is kept when the response
    carries none, and its refresh-token expiry when the response omits x_refresh_token_expires_in).
    Any other expiry-like field of the response (Intuit's newer absolute-expiry field, 04 §5.1) is
    stored verbatim under "response_expiry_fields".
    """
    previous = previous or {}
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise AuthError("QuickBooks token response had no access_token (fields: "
                        + ", ".join(sorted(str(k) for k in body)) + ")")
    expires_in = _number(body.get("expires_in"))
    if expires_in is not None:
        expires_at = now + expires_in
    else:  # 03 §7: derive from the JWT exp claim, else the documented default lifetime
        expires_at = jwt_exp(access) or now + DEFAULT_ACCESS_TTL_S
    new_rt = body.get("refresh_token")
    if not isinstance(new_rt, str) or not new_rt.strip():
        new_rt = str(previous.get("refresh_token") or "")
    new_rt = new_rt.strip()
    if not new_rt:
        raise AuthError("QuickBooks token response had no refresh_token")
    rt_in = _number(body.get("x_refresh_token_expires_in"))
    if rt_in is not None:
        rt_expires_at: float | None = now + rt_in
    else:
        rt_expires_at = _number(previous.get("refresh_token_expires_at"))
    entry: dict[str, Any] = {
        "access_token": access,
        "expires_at": float(expires_at),
        "refresh_token": new_rt,
        "refresh_token_expires_at": rt_expires_at,
        "refresh_token_seed": seed,
        "realm_id": realm_id,
        "updated_at": now,
    }
    extra = {
        str(k): v for k, v in body.items()
        if "expir" in str(k).lower() and str(k) not in _KNOWN_EXPIRY_KEYS
        and isinstance(v, (str, int, float)) and not isinstance(v, bool)
    }
    if extra:
        entry["response_expiry_fields"] = extra
    return entry


def _env_path(env_file: Path | None) -> Path:
    """04 §5.2 step 4: settings.env_file or Path(".env")."""
    return Path(env_file) if env_file is not None else Path(".env")


def _env_encoding(path: Path) -> str:
    """Keep a UTF-8 BOM if the file has one (load_settings reads .env as utf-8-sig)."""
    try:
        with path.open("rb") as fh:
            return "utf-8-sig" if fh.read(3) == b"\xef\xbb\xbf" else "utf-8"
    except OSError:
        return "utf-8"


def write_env_value(env_file: Path | None, key: str, value: str, *, stored: bool = True) -> bool:
    """dotenv.set_key(<env file>, key, value, quote_mode="never") and os.environ[key] = value.

    Only one key is rewritten, never the whole file (02 §5). A failure is a WARNING, not an error: the
    TokenStore is the source of truth (04 §5.2 step 4). The value itself is never logged. `stored=False` says
    that tokens.json could not be written either, so the WARNING does not claim it holds the value.
    """
    path = _env_path(env_file)
    os.environ[key] = value
    try:
        set_key(path, key, value, quote_mode="never", encoding=_env_encoding(path))
    except (OSError, UnicodeError, ValueError) as exc:
        log.warning("Could not write %s to %s (%s: %s)%s", key, path, type(exc).__name__, redact(str(exc)),
                    "; tokens.json still holds the current value" if stored else "")
        return False
    return True


# ════════════════════════════════════════════════════════════════════════════════════════════════
# QboAuth (04 §5.2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
class QboAuth:
    """TokenProvider for QuickBooks Online (04 §5.2)."""

    def __init__(self, settings: QboSettings, token_store: TokenStore, *,
                 http: httpx.AsyncClient | None = None, clock: Clock | None = None) -> None:
        self._s = settings
        self._store = token_store
        self._http = http if http is not None else new_async_client()   # own client, no base URL
        self._owns_http = http is None
        self._clock: Clock = clock or SystemClock()
        self._lock = asyncio.Lock()
        self._entry: dict[str, Any] | None = None
        # The QB_REFRESH_TOKEN value .env holds as far as this process knows (updated after write-back).
        self._env_rt = (settings.refresh_token or "").strip()
        self._logged_fields = False
        self._closed = False

    @property
    def settings(self) -> QboSettings:
        return self._s

    def _now(self) -> float:
        return self._clock.now().timestamp()

    # ── 04 §5.2 "Choosing the refresh token to use" (done once, lazily, inside the lock) ──
    def _load(self) -> dict[str, Any]:
        if self._entry is not None:
            return self._entry
        env_rt = self._env_rt                                                  # step 1
        entry: dict[str, Any] = self._store.get(STORE_KEY) or {}
        stored_realm = str(entry.get("realm_id") or "")
        if stored_realm and stored_realm != str(self._s.realm_id):            # step 2
            log.warning("The stored QuickBooks token belongs to company %s, not QB_REALM_ID=%s — ignoring it",
                        stored_realm, self._s.realm_id)
            entry = {}
        if not entry.get("refresh_token"):                                     # step 3
            if not env_rt:
                raise AuthError(NO_REFRESH_TOKEN_MESSAGE)
            entry = {"refresh_token": env_rt, "refresh_token_seed": env_rt, "realm_id": self._s.realm_id}
        elif env_rt and env_rt not in (entry.get("refresh_token"), entry.get("refresh_token_seed")):  # step 4
            log.info("Using the new QB_REFRESH_TOKEN from .env")
            entry = {
                **entry,
                "refresh_token": env_rt,
                "refresh_token_seed": env_rt,
                "refresh_token_expires_at": None,   # belonged to the old token
                "access_token": "",                 # discard the cached access token
                "expires_at": 0.0,
                "realm_id": self._s.realm_id,
            }
        # step 5: otherwise the store is newer than or equal to .env → use entry["refresh_token"]
        self._entry = dict(entry)
        return self._entry

    # ── TokenProvider ──
    async def get_token(self) -> str:
        """Cached access token if fresh (120 s skew), else refresh — under the lock (03 §8)."""
        async with self._lock:
            entry = self._load()
            if is_fresh(entry, skew_seconds=FRESH_SKEW_S, now=self._now()):
                return str(entry["access_token"])
            await self._refresh()
            assert self._entry is not None
            return str(self._entry["access_token"])

    async def invalidate(self) -> None:
        """Drop the access token only (expires_at = 0), keep the refresh token, persist (04 §5.2 step 6)."""
        async with self._lock:
            try:
                entry = self._load()
            except AuthError:
                return
            entry["expires_at"] = 0.0
            if entry.get("access_token"):
                self._persist(entry, what="the dropped access token")

    async def force_refresh(self) -> None:
        """Refresh now, regardless of the cached access token (probes, `eta qbo-auth` checks)."""
        async with self._lock:
            self._load()
            await self._refresh()

    @property
    def refresh_token_expires_at(self) -> float | None:
        """Epoch seconds when the refresh token (the QuickBooks connection) expires, if known."""
        entry: Mapping[str, Any] | None = self._entry
        if entry is None:
            stored = self._store.get(STORE_KEY) or {}
            if stored.get("realm_id") and str(stored["realm_id"]) != str(self._s.realm_id):
                return None
            entry = stored
        return _number(entry.get("refresh_token_expires_at"))

    async def aclose(self) -> None:
        if self._owns_http and not self._closed:
            self._closed = True
            await self._http.aclose()

    # ── 04 §5.2 _refresh() (called with the lock held) ──
    async def _refresh(self) -> None:
        entry = self._load()
        rt = str(entry["refresh_token"])
        resp = await request(
            self._http, "POST", TOKEN_URL, service=OAUTH_SERVICE, expected=(200, 400, 401), idempotent=True,
            headers=_token_headers(self._s), data={"grant_type": "refresh_token", "refresh_token": rt},
        )
        body = _json_or_empty(resp)
        if resp.status_code == 400:
            error = str(body.get("error") or "")
            if error == "invalid_grant":
                # 04 §5.3: never delete or overwrite the stored token (Intuit outages produce spurious invalid_grant)
                raise AuthError(INVALID_GRANT_MESSAGE)
            detail = _short(body.get("error_description"))
            raise AuthError(f"QuickBooks token refresh failed ({error or 'HTTP 400'}"
                            + (f": {detail}" if detail else "") + ")")
        if resp.status_code == 401:
            raise AuthError(INVALID_CLIENT_MESSAGE)
        if not self._logged_fields:
            # 04 §5.1: show the raw key names (never values) so the user can see Intuit's expiry fields
            log.debug("QuickBooks token response fields: %s", ", ".join(sorted(str(k) for k in body)))
            self._logged_fields = True
        now = self._now()
        new_entry = build_token_entry(
            body, realm_id=self._s.realm_id,
            seed=self._env_rt or str(entry.get("refresh_token_seed") or ""),
            previous=entry, now=now,
        )
        # 04 §5.2 step 4: persist to the TokenStore FIRST (atomic), then write back to .env. The new entry is kept in
        # memory before that (review fix QBO-06): a failed tokens.json write must never drop a rotated refresh token
        # (04 §5.1: the old one may stop working). This process goes on with it, and .env still receives it, which
        # the next run picks up as a pasted token (step 4 of "Choosing the refresh token").
        self._entry = new_entry
        stored = self._persist(new_entry, what="the new tokens")
        new_rt = str(new_entry["refresh_token"])
        if new_rt != self._env_rt:
            if write_env_value(self._s.env_file, ENV_REFRESH_TOKEN, new_rt, stored=stored):
                self._env_rt = new_rt
            elif not stored:
                log.error("The new QuickBooks refresh token could not be saved: neither %s nor .env could be "
                          "written. If QuickBooks later rejects the old one (invalid_grant), reconnect with "
                          "`eta qbo-auth`.", self._store.path)
        _warn_if_expiring(new_entry, now)

    def _persist(self, entry: Mapping[str, Any], *, what: str) -> bool:
        """token_store.put("qbo", entry); a write failure (full disk, permissions, a Windows sharing violation) is
        logged and this process keeps the entry in memory (QBO-06). Never logs a token. Returns success."""
        try:
            self._store.put(STORE_KEY, dict(entry))
        except OSError as exc:
            log.error("Could not save %s to %s (%s: %s); this run keeps them in memory. Check that the file can be "
                      "written.", what, self._store.path, type(exc).__name__, redact(str(exc)))
            return False
        return True


def _warn_if_expiring(entry: Mapping[str, Any], now: float) -> None:
    """04 §5.2 step 5."""
    expires_at = _number(entry.get("refresh_token_expires_at"))
    if expires_at is not None and expires_at - now < EXPIRY_WARNING_S:
        when = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d")
        log.warning("QuickBooks connection expires on %s; reconnect with `eta qbo-auth` before then.", when)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# `eta qbo-auth` — authorization-code flow (04 §5.4)
# ════════════════════════════════════════════════════════════════════════════════════════════════
def authorization_url(settings: QboSettings, *, redirect_uri: str, state: str) -> str:
    """04 §5.4 step 3: https://appcenter.intuit.com/connect/oauth2?client_id=…&response_type=code&scope=…&
    redirect_uri=<urlenc>&state=…"""
    query = urlencode(
        {
            "client_id": settings.client_id,
            "response_type": "code",
            "scope": settings.oauth_scopes,
            "redirect_uri": redirect_uri,
            "state": state,
        },
        quote_via=quote,
    )
    return f"{AUTHORIZE_URL}?{query}"


def _query_part(text: str) -> str:
    """The query string of a pasted address (or of a pasted bare `code=…&state=…` string)."""
    text = (text or "").strip()
    parts = urlsplit(text)
    if parts.query:
        return parts.query
    if "=" in text and "://" not in text:
        return text.lstrip("?")
    return ""


def parse_callback(query: str, *, expected_state: str) -> tuple[str, str | None]:
    """Parse the redirect's query string → (code, realmId or None) (04 §5.4 step 5).

    An `error=` parameter → AuthError(error_description); a mismatched state →
    AuthError("state mismatch — start again"); a missing code → AuthError.
    """
    params = parse_qs(query or "", keep_blank_values=True)

    def first(name: str) -> str:
        values = params.get(name)
        return values[0].strip() if values else ""

    error = first("error")
    if error:
        raise AuthError(_short(first("error_description")) or error)
    if first("state") != expected_state:
        raise AuthError("state mismatch — start again")
    code = first("code")
    if not code:
        raise AuthError("The address has no code= parameter — copy the whole address bar after approving, "
                        "and start again")
    return code, (first("realmId") or None)


async def _wait_for_callback(port: int, *, timeout: float = CALLBACK_TIMEOUT_S) -> str:
    """Listen mode: one-shot HTTP listener on 127.0.0.1:port; returns the query string of GET /callback?…"""
    loop = asyncio.get_running_loop()
    received: asyncio.Future[str] = loop.create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 30)
            parts = line.decode("latin-1", "replace").split()
            target = parts[1] if len(parts) >= 2 else ""
            if len(parts) >= 2 and parts[0].upper() == "GET" and urlsplit(target).path == "/callback":
                page = CALLBACK_PAGE.encode("utf-8")
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                             + f"Content-Length: {len(page)}\r\n".encode("ascii")
                             + b"Connection: close\r\n\r\n" + page)
                if not received.done():
                    received.set_result(urlsplit(target).query)
            else:  # e.g. /favicon.ico — keep waiting for the real callback
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await writer.drain()
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            log.debug("qbo-auth listener: connection error %s", type(exc).__name__)
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", port)
    try:
        return await asyncio.wait_for(received, timeout)
    except asyncio.TimeoutError:
        raise AuthError(f"No authorization callback arrived within {int(timeout)} s — start again") from None
    finally:
        server.close()
        await server.wait_closed()


async def run_authorization_code_flow(settings: QboSettings, token_store: TokenStore, *,
                                      listen: bool = False, port: int = 8765,
                                      input_fn: Callable[[str], str] = input,
                                      open_browser: bool = True) -> None:
    """`eta qbo-auth` (04 §5.4): connect the app to a QuickBooks company and store the refresh token.

    `settings.realm_id` may be "" (QB_REALM_ID not set yet): the connected company's id is then printed and
    written to .env. Tokens are never printed.
    """
    state = secrets.token_urlsafe(24)                                                      # step 1
    redirect = f"http://localhost:{port}/callback" if listen else settings.redirect_uri    # step 2
    url = authorization_url(settings, redirect_uri=redirect, state=state)                 # step 3
    print("Open this address in your browser, sign in to QuickBooks and approve access:")  # step 4
    print(url)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - opening a browser is a convenience only
            log.debug("could not open a browser: %s", type(exc).__name__)
    if listen:                                                                             # step 5
        print(f"Waiting up to {int(CALLBACK_TIMEOUT_S)} s for QuickBooks to redirect to {redirect} ...")
        query = await _wait_for_callback(port, timeout=CALLBACK_TIMEOUT_S)
    else:
        print(PASTE_INSTRUCTIONS)
        pasted = await read_line(input_fn, "> ")       # interruptible: one Ctrl+C ends the process
        query = _query_part(pasted)
    code, callback_realm = parse_callback(query, expected_state=state)
    configured_realm = (settings.realm_id or "").strip()
    realm = callback_realm or configured_realm
    if not realm:
        raise AuthError("The address has no realmId= parameter — copy the whole address bar after approving, "
                        "and start again")
    if configured_realm and realm != configured_realm:                                     # step 7 (checked
        raise ConfigError(f"You connected company {realm} but QB_REALM_ID is {configured_realm}")  # before step 6)

    async with new_async_client() as http:                                                 # step 6
        resp = await request(
            http, "POST", TOKEN_URL, service=OAUTH_SERVICE, expected=(200, 400, 401),
            # an authorization code is single-use: never resend it after it may have reached Intuit
            idempotent=False,
            headers=_token_headers(settings),
            data={"grant_type": "authorization_code", "code": code, "redirect_uri": redirect},
        )
    body = _json_or_empty(resp)
    if resp.status_code == 400:
        error = str(body.get("error") or "HTTP 400")
        detail = _short(body.get("error_description"))
        raise AuthError(f"QuickBooks rejected the authorization code ({error}"
                        + (f": {detail}" if detail else "") + ") — start again")
    if resp.status_code == 401:
        raise AuthError(INVALID_CLIENT_MESSAGE)

    now = time.time()
    # seed = what .env held before: if the write-back below fails, the next run still prefers the store
    entry = build_token_entry(body, realm_id=realm, seed=(settings.refresh_token or "").strip(),
                              previous=None, now=now)
    token_store.put(STORE_KEY, entry)                                                      # step 8
    new_rt = str(entry["refresh_token"])
    write_env_value(settings.env_file, ENV_REFRESH_TOKEN, new_rt)
    if not configured_realm:
        print(f"QuickBooks company (realm) id: {realm}")
        write_env_value(settings.env_file, ENV_REALM_ID, realm)

    connected = dataclasses.replace(settings, realm_id=realm, refresh_token=new_rt)        # step 9
    from .client import QboClient  # local import: client.py imports this module

    client = QboClient(connected, QboAuth(connected, token_store), read_only=True)
    company: str | None = None
    try:
        info = await client.get_company_info()
        company = str(info.get("CompanyName") or "").strip() or None
    except Exception as exc:  # noqa: BLE001 - the connection itself is stored; the name is informational
        log.warning("Connected, but the company name could not be read: %s", exc)
    finally:
        await client.aclose()
    expires_at = _number(entry.get("refresh_token_expires_at"))
    valid_until = datetime.fromtimestamp(expires_at).strftime("%Y-%m-%d") if expires_at else "unknown"
    print(f"Connected to {company or 'your QuickBooks company'} (realm {realm}). "
          f"Refresh token valid until {valid_until}.")
    _warn_if_expiring(entry, now)
