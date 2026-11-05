"""qbo/auth.py — QboAuth seeding, refresh, persistence, expiry warning and the `eta qbo-auth` flow
(04-quickbooks-online §9.2 tests 1–9, plus edge cases). respx-mocked; no network."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import logging
import os
import socket
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from eta_automations.clock import FixedClock
from eta_automations.config import QboSettings, Settings
from eta_automations.errors import AuthError, ConfigError
from eta_automations.qbo import auth as qbo_auth
from eta_automations.qbo.auth import (
    INVALID_CLIENT_MESSAGE,
    INVALID_GRANT_MESSAGE,
    NO_REFRESH_TOKEN_MESSAGE,
    TOKEN_URL,
    QboAuth,
    authorization_url,
    parse_callback,
    run_authorization_code_flow,
)
from eta_automations.qbo.client import QboClient
from eta_automations.token_store import TokenStore

RUN_NOW = datetime(2026, 11, 5, 9, 30)
NOW = RUN_NOW.timestamp()
REALM = "1234567890"
API = "https://quickbooks.api.intuit.com"
COMPANY = f"{API}/v3/company/{REALM}"
PLAYGROUND = "https://developer.intuit.com/v2/OAuth2Playground/RedirectUrl"


# ── helpers ──
def qbo_settings(make_settings: Callable[..., Settings], **env: Any) -> QboSettings:
    settings = make_settings(env).qbo
    assert settings is not None
    return settings


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode())


def token_response(load_fixture: Callable[[str], Any], **overrides: Any) -> httpx.Response:
    body = dict(load_fixture("qbo/token_refresh_ok.json"))
    body.update(overrides)
    return httpx.Response(200, json=body)


def stored_entry(**values: Any) -> dict[str, Any]:
    entry = {"access_token": "AT-OLD", "expires_at": NOW - 60, "refresh_token": "RT-STORE",
             "refresh_token_seed": "test-qb-refresh-token", "refresh_token_expires_at": NOW + 50 * 86400,
             "realm_id": REALM, "updated_at": NOW - 4000}
    entry.update(values)
    return entry


def new_auth(settings: QboSettings, token_store: TokenStore) -> QboAuth:
    return QboAuth(settings, token_store, clock=FixedClock(RUN_NOW))


# ── test 1 ──
async def test_refresh_persists_rotated_token_before_returning_and_writes_env(
        make_settings, token_store, tmp_path, load_fixture, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("QB_CLIENT_ID=test-qb-client-id\nQB_REFRESH_TOKEN=test-qb-refresh-token\n"
                        "QB_REALM_ID=1234567890\n", encoding="utf-8")
    settings = make_settings(env_file=env_file).qbo
    events: list[tuple[str, ...]] = []
    real_put = token_store.put
    real_set_key = qbo_auth.set_key

    def recording_put(key: str, value: dict[str, Any]) -> None:
        events.append(("put", key, value["refresh_token"]))
        real_put(key, value)

    def recording_set_key(path: Any, key: str, value: str, **kwargs: Any) -> Any:
        on_disk = TokenStore(token_store.path).get("qbo") or {}
        events.append(("set_key", key, on_disk.get("refresh_token", "")))   # store already updated
        return real_set_key(path, key, value, **kwargs)

    monkeypatch.setattr(token_store, "put", recording_put)
    monkeypatch.setattr(qbo_auth, "set_key", recording_set_key)
    auth = new_auth(settings, token_store)
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        token = await auth.get_token()
        events.append(("returned", token))
    await auth.aclose()

    assert token == "AT-NEW-123"
    assert events == [("put", "qbo", "RT-NEW-456"), ("set_key", "QB_REFRESH_TOKEN", "RT-NEW-456"),
                      ("returned", "AT-NEW-123")]
    request = route.calls.last.request
    basic = base64.b64encode(b"test-qb-client-id:test-qb-client-secret").decode()
    assert request.headers["Authorization"] == f"Basic {basic}"
    assert request.headers["Accept"] == "application/json"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert form(request) == {"grant_type": ["refresh_token"], "refresh_token": ["test-qb-refresh-token"]}

    entry = TokenStore(token_store.path).get("qbo")
    assert entry is not None
    assert entry["access_token"] == "AT-NEW-123"
    assert entry["refresh_token"] == "RT-NEW-456"
    assert entry["realm_id"] == REALM
    assert entry["expires_at"] == pytest.approx(NOW + 3600)
    assert entry["refresh_token_expires_at"] == pytest.approx(NOW + 8726400)
    assert entry["refresh_token_seed"] == "test-qb-refresh-token"
    assert entry["updated_at"] == pytest.approx(NOW)
    text = env_file.read_text(encoding="utf-8")
    assert "QB_REFRESH_TOKEN=RT-NEW-456\n" in text
    assert "test-qb-refresh-token" not in text
    assert "QB_CLIENT_ID=test-qb-client-id\n" in text and "QB_REALM_ID=1234567890\n" in text
    assert os.environ["QB_REFRESH_TOKEN"] == "RT-NEW-456"
    assert auth.refresh_token_expires_at == pytest.approx(NOW + 8726400)


async def test_cached_fresh_token_needs_no_refresh_and_tokens_are_never_logged(
        make_settings, token_store, load_fixture, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    token_store.put("qbo", stored_entry(access_token="AT-CACHED", expires_at=NOW + 3000,
                                        refresh_token="test-qb-refresh-token"))
    auth = new_auth(qbo_settings(make_settings), token_store)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        assert await auth.get_token() == "AT-CACHED"
        assert route.call_count == 0
        await auth.force_refresh()          # forced refresh even though the cached token is fresh
        assert route.call_count == 1
        assert await auth.get_token() == "AT-NEW-123"
    await auth.aclose()
    for secret in ("AT-CACHED", "AT-NEW-123", "RT-NEW-456", "test-qb-refresh-token", "test-qb-client-secret"):
        assert secret not in caplog.text
    assert "access_token" in caplog.text    # 04 §5.1: the raw key NAMES are logged at DEBUG


# ── test 2 ──
async def test_seeding_store_newer_than_env_uses_store_token(make_settings, token_store, load_fixture) -> None:
    token_store.put("qbo", stored_entry(refresh_token="RT-STORE", refresh_token_seed="test-qb-refresh-token"))
    auth = new_auth(qbo_settings(make_settings), token_store)   # .env still holds the seed
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        assert await auth.get_token() == "AT-NEW-123"
    assert form(route.calls.last.request)["refresh_token"] == ["RT-STORE"]


async def test_seeding_env_equal_to_store_token_uses_store(make_settings, token_store, load_fixture) -> None:
    token_store.put("qbo", stored_entry(refresh_token="RT-SAME", refresh_token_seed="RT-OLDER"))
    auth = new_auth(qbo_settings(make_settings, QB_REFRESH_TOKEN="RT-SAME"), token_store)
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        await auth.get_token()
    assert form(route.calls.last.request)["refresh_token"] == ["RT-SAME"]


# ── test 3 ──
async def test_seeding_new_env_token_wins_and_discards_cached_access_token(
        make_settings, token_store, load_fixture, caplog) -> None:
    caplog.set_level(logging.INFO, logger="eta")
    token_store.put("qbo", stored_entry(access_token="AT-CACHED", expires_at=NOW + 3000, refresh_token="RT-STORE",
                                        refresh_token_seed="RT-SEED"))
    auth = new_auth(qbo_settings(make_settings, QB_REFRESH_TOKEN="RT-PASTED"), token_store)
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        token = await auth.get_token()
    assert token == "AT-NEW-123"                    # not the cached AT-CACHED
    assert form(route.calls.last.request)["refresh_token"] == ["RT-PASTED"]
    assert "Using the new QB_REFRESH_TOKEN from .env" in caplog.text
    entry = token_store.get("qbo")
    assert entry is not None
    assert entry["refresh_token"] == "RT-NEW-456" and entry["refresh_token_seed"] == "RT-PASTED"


# ── test 4 ──
async def test_store_entry_of_another_realm_is_ignored(make_settings, token_store, load_fixture) -> None:
    token_store.put("qbo", stored_entry(access_token="AT-OTHER-COMPANY", expires_at=NOW + 3000,
                                        refresh_token="RT-OTHER", refresh_token_seed="RT-OTHER", realm_id="999"))
    auth = new_auth(qbo_settings(make_settings), token_store)
    assert auth.refresh_token_expires_at is None    # the other company's expiry is not ours
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        assert await auth.get_token() == "AT-NEW-123"
    assert form(route.calls.last.request)["refresh_token"] == ["test-qb-refresh-token"]
    entry = token_store.get("qbo")
    assert entry is not None and entry["realm_id"] == REALM


async def test_other_realm_store_and_no_env_token_raises(make_settings, token_store) -> None:
    token_store.put("qbo", stored_entry(realm_id="999"))
    auth = new_auth(qbo_settings(make_settings, QB_REFRESH_TOKEN=None), token_store)
    with respx.mock() as router, pytest.raises(AuthError) as info:
        await auth.get_token()
    assert str(info.value) == NO_REFRESH_TOKEN_MESSAGE
    assert router.calls.call_count == 0


async def test_no_refresh_token_anywhere(make_settings, token_store) -> None:
    auth = new_auth(qbo_settings(make_settings, QB_REFRESH_TOKEN=None), token_store)
    with pytest.raises(AuthError, match="No QuickBooks refresh token. Run `eta qbo-auth`"):
        await auth.get_token()
    await auth.invalidate()   # nothing to invalidate: no error


# ── test 5 ──
async def test_invalid_grant_raises_and_leaves_state_untouched(
        make_settings, token_store, tmp_path, load_fixture) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("QB_REFRESH_TOKEN=test-qb-refresh-token\n", encoding="utf-8")
    token_store.put("qbo", stored_entry())
    store_before = token_store.path.read_bytes()
    auth = QboAuth(make_settings(env_file=env_file).qbo, token_store, clock=FixedClock(RUN_NOW))
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(400, json=load_fixture("qbo/token_invalid_grant.json")))
        with pytest.raises(AuthError) as info:
            await auth.get_token()
    assert str(info.value) == INVALID_GRANT_MESSAGE
    assert "Do not delete tokens.json" in str(info.value)
    assert token_store.path.read_bytes() == store_before
    assert env_file.read_text(encoding="utf-8") == "QB_REFRESH_TOKEN=test-qb-refresh-token\n"


async def test_invalid_client_and_other_token_errors(make_settings, token_store) -> None:
    auth = new_auth(qbo_settings(make_settings), token_store)
    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(return_value=httpx.Response(401, json={"error": "invalid_client"}))
        with pytest.raises(AuthError) as info:
            await auth.get_token()
        assert str(info.value) == INVALID_CLIENT_MESSAGE
        route.mock(return_value=httpx.Response(400, json={"error": "invalid_request",
                                                          "error_description": "grant_type missing"}))
        with pytest.raises(AuthError, match="invalid_request: grant_type missing"):
            await auth.get_token()
    assert token_store.get("qbo") is None


# ── test 6 ──
async def test_twenty_concurrent_get_token_calls_refresh_once(make_settings, token_store, load_fixture) -> None:
    auth = new_auth(qbo_settings(make_settings), token_store)

    async def slow_token(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)   # keep the refresh in flight while the other callers queue up
        return token_response(load_fixture)

    with respx.mock() as router:
        route = router.post(TOKEN_URL).mock(side_effect=slow_token)
        tokens = await asyncio.gather(*(auth.get_token() for _ in range(20)))
    assert tokens == ["AT-NEW-123"] * 20
    assert route.call_count == 1


# ── test 7 ──
async def test_api_401_refreshes_once_and_retries_then_auth_error(make_settings, token_store, load_fixture) -> None:
    token_store.put("qbo", stored_entry(access_token="AT-OLD", expires_at=NOW + 3000,
                                        refresh_token="test-qb-refresh-token"))
    settings = qbo_settings(make_settings)
    invoice = load_fixture("qbo/query_invoices.json")["QueryResponse"]["Invoice"][0]
    fault_401 = load_fixture("qbo/fault_auth_401.json")
    client = QboClient(settings, new_auth(settings, token_store))
    with respx.mock() as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        api = router.get(f"{COMPANY}/invoice/190613").mock(side_effect=[
            httpx.Response(401, json=fault_401),
            httpx.Response(200, json={"Invoice": invoice, "time": "t"}),
        ])
        snapshot = await client.get_invoice("190613")
        assert snapshot.id == "190613"
        assert token_route.call_count == 1
        assert api.call_count == 2
        assert [c.request.headers["Authorization"] for c in api.calls] == ["Bearer AT-OLD", "Bearer AT-NEW-123"]
        entry = token_store.get("qbo")
        assert entry is not None and entry["access_token"] == "AT-NEW-123"

        # second scenario: the API keeps answering 401 → exactly one more refresh, then AuthError
        token_store.put("qbo", stored_entry(access_token="AT-OLD", expires_at=NOW + 3000,
                                            refresh_token="test-qb-refresh-token"))
        client2 = QboClient(settings, new_auth(settings, token_store))
        token_route.reset()
        api.reset()
        api.side_effect = None
        api.mock(return_value=httpx.Response(401, json=fault_401))
        with pytest.raises(AuthError) as info:
            await client2.get_invoice("190613")
        assert str(info.value) == "qbo: credentials/token rejected (HTTP 401)"
        assert token_route.call_count == 1
        assert api.call_count == 2
    await client.aclose()
    await client2.aclose()


async def test_invalidate_drops_access_token_only(make_settings, token_store) -> None:
    token_store.put("qbo", stored_entry(access_token="AT-LIVE", expires_at=NOW + 3000))
    auth = new_auth(qbo_settings(make_settings), token_store)
    assert await auth.get_token() == "AT-LIVE"
    await auth.invalidate()
    entry = token_store.get("qbo")
    assert entry is not None
    assert entry["expires_at"] == 0 and entry["refresh_token"] == "RT-STORE"


# ── test 8 ──
async def test_expiry_warning_when_refresh_token_expires_within_30_days(
        make_settings, token_store, load_fixture, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    auth = new_auth(qbo_settings(make_settings), token_store)
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=token_response(load_fixture, x_refresh_token_expires_in=20 * 86400))
        await auth.get_token()
    expected_day = datetime.fromtimestamp(NOW + 20 * 86400).strftime("%Y-%m-%d")
    assert (f"QuickBooks connection expires on {expected_day}; reconnect with `eta qbo-auth` before then."
            in caplog.messages)


async def test_no_expiry_warning_for_a_long_lived_refresh_token(
        make_settings, token_store, load_fixture, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    auth = new_auth(qbo_settings(make_settings), token_store)
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))   # ≈ 101 days
        await auth.get_token()
    assert not any("connection expires" in m for m in caplog.messages)


async def test_env_write_back_failure_is_only_a_warning(make_settings, token_store, tmp_path, load_fixture,
                                                         caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    unwritable = tmp_path / "missing-dir" / ".env"     # set_key cannot create its temp file here
    auth = QboAuth(make_settings(env_file=unwritable).qbo, token_store, clock=FixedClock(RUN_NOW))
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        assert await auth.get_token() == "AT-NEW-123"
    entry = token_store.get("qbo")
    assert entry is not None and entry["refresh_token"] == "RT-NEW-456"   # the TokenStore is the truth
    assert any("Could not write QB_REFRESH_TOKEN" in m for m in caplog.messages)
    assert "RT-NEW-456" not in caplog.text


async def test_response_without_rotation_keeps_refresh_token_and_extra_expiry_fields(
        make_settings, token_store) -> None:
    auth = new_auth(qbo_settings(make_settings), token_store)
    body = {"token_type": "bearer", "access_token": "AT-2", "expires_in": 3600,
            "x_refresh_token_expires_in": 8726400, "refresh_token_expires_at": "2030-01-01T00:00:00Z"}
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(200, json=body))
        assert await auth.get_token() == "AT-2"
    entry = token_store.get("qbo")
    assert entry is not None
    assert entry["refresh_token"] == "test-qb-refresh-token"
    assert entry["response_expiry_fields"] == {"refresh_token_expires_at": "2030-01-01T00:00:00Z"}


# ── test 9 ──
def _paste(code: str = "AB11-CODE", state: str = "STATE123", realm: str = REALM) -> Callable[[str], str]:
    def fake_input(prompt: str) -> str:
        return f"{PLAYGROUND}?code={code}&state={state}&realmId={realm}"
    return fake_input


async def test_authorization_code_flow_paste_back(make_settings, token_store, tmp_path, load_fixture, monkeypatch,
                                                  capsys) -> None:
    monkeypatch.setattr(qbo_auth.secrets, "token_urlsafe", lambda n: "STATE123")
    env_file = tmp_path / ".env"
    env_file.write_text("QB_REFRESH_TOKEN=old-token\n", encoding="utf-8")
    settings = make_settings(env_file=env_file).qbo
    with respx.mock() as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        info_route = router.get(f"{COMPANY}/companyinfo/{REALM}").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/companyinfo.json")))
        await run_authorization_code_flow(settings, token_store, input_fn=_paste(), open_browser=False)
    assert form(token_route.calls.last.request) == {
        "grant_type": ["authorization_code"], "code": ["AB11-CODE"], "redirect_uri": [PLAYGROUND]}
    assert token_route.call_count == 1                       # E10 used the new access token, no refresh
    assert info_route.calls.last.request.headers["Authorization"] == "Bearer AT-NEW-123"
    out = capsys.readouterr().out
    assert ("https://appcenter.intuit.com/connect/oauth2?client_id=test-qb-client-id&response_type=code"
            "&scope=com.intuit.quickbooks.accounting"
            "&redirect_uri=https%3A%2F%2Fdeveloper.intuit.com%2Fv2%2FOAuth2Playground%2FRedirectUrl"
            "&state=STATE123") in out
    assert "Copy the entire address bar and paste it here:" in out
    valid_until = datetime.fromtimestamp(token_store.get("qbo")["refresh_token_expires_at"]).strftime("%Y-%m-%d")
    assert f"Connected to Example Forwarding LLC (realm {REALM}). Refresh token valid until {valid_until}." in out
    assert "RT-NEW-456" not in out and "AT-NEW-123" not in out
    entry = token_store.get("qbo")
    assert entry is not None
    assert entry["refresh_token"] == "RT-NEW-456" and entry["realm_id"] == REALM
    assert entry["refresh_token_seed"] == settings.refresh_token   # what .env held before the flow
    assert env_file.read_text(encoding="utf-8") == "QB_REFRESH_TOKEN=RT-NEW-456\n"


async def test_authorization_code_flow_state_mismatch(make_settings, token_store, monkeypatch) -> None:
    monkeypatch.setattr(qbo_auth.secrets, "token_urlsafe", lambda n: "STATE123")
    settings = qbo_settings(make_settings)
    with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=httpx.Response(500))
        with pytest.raises(AuthError) as info:
            await run_authorization_code_flow(settings, token_store, input_fn=_paste(state="FORGED"),
                                              open_browser=False)
    assert str(info.value) == "state mismatch — start again"
    assert token_route.call_count == 0
    assert token_store.get("qbo") is None


async def test_authorization_code_flow_error_parameter_and_wrong_company(make_settings, token_store,
                                                                         monkeypatch) -> None:
    monkeypatch.setattr(qbo_auth.secrets, "token_urlsafe", lambda n: "STATE123")
    settings = qbo_settings(make_settings)

    def denied(prompt: str) -> str:
        return f"{PLAYGROUND}?error=access_denied&error_description=User+denied+access&state=STATE123"

    with respx.mock() as router:
        with pytest.raises(AuthError, match="^User denied access$"):
            await run_authorization_code_flow(settings, token_store, input_fn=denied, open_browser=False)
        with pytest.raises(ConfigError) as info:
            await run_authorization_code_flow(settings, token_store, input_fn=_paste(realm="5555"),
                                              open_browser=False)
        assert router.calls.call_count == 0
    assert str(info.value) == f"You connected company 5555 but QB_REALM_ID is {REALM}"


async def test_authorization_code_flow_writes_realm_when_unset(make_settings, token_store, tmp_path, load_fixture,
                                                               monkeypatch, capsys) -> None:
    monkeypatch.setattr(qbo_auth.secrets, "token_urlsafe", lambda n: "STATE123")
    env_file = tmp_path / ".env"
    settings = dataclasses.replace(make_settings(env_file=env_file).qbo, realm_id="", refresh_token="")
    with respx.mock() as router:
        router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        router.get(f"{API}/v3/company/777/companyinfo/777").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/companyinfo.json")))
        await run_authorization_code_flow(settings, token_store, input_fn=_paste(realm="777"), open_browser=False)
    text = env_file.read_text(encoding="utf-8")
    assert "QB_REALM_ID=777\n" in text and "QB_REFRESH_TOKEN=RT-NEW-456\n" in text
    assert "QuickBooks company (realm) id: 777" in capsys.readouterr().out
    assert os.environ["QB_REALM_ID"] == "777"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _http_get(port: int, target: str) -> bytes:
    for _ in range(300):
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            break
        except OSError:
            await asyncio.sleep(0.01)
    else:  # pragma: no cover - the listener never came up
        raise AssertionError("listener did not start")
    writer.write(f"GET {target} HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


async def test_authorization_code_flow_listen_mode(make_settings, token_store, load_fixture, monkeypatch) -> None:
    monkeypatch.setattr(qbo_auth.secrets, "token_urlsafe", lambda n: "STATE123")
    settings = qbo_settings(make_settings)
    port = _free_port()
    with respx.mock() as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response(load_fixture))
        router.get(f"{COMPANY}/companyinfo/{REALM}").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/companyinfo.json")))
        flow = asyncio.create_task(run_authorization_code_flow(settings, token_store, listen=True, port=port,
                                                               open_browser=False))
        favicon = await _http_get(port, "/favicon.ico")
        page = await _http_get(port, f"/callback?code=LISTEN-CODE&state=STATE123&realmId={REALM}")
        await asyncio.wait_for(flow, 10)
    assert favicon.startswith(b"HTTP/1.1 404")
    assert page.startswith(b"HTTP/1.1 200 OK") and b"You can close this tab" in page
    assert form(token_route.calls.last.request) == {
        "grant_type": ["authorization_code"], "code": ["LISTEN-CODE"],
        "redirect_uri": [f"http://localhost:{port}/callback"]}
    entry = token_store.get("qbo")
    assert entry is not None and entry["refresh_token"] == "RT-NEW-456"


async def test_listen_mode_times_out(monkeypatch) -> None:
    with pytest.raises(AuthError, match="No authorization callback arrived within 0 s"):
        await qbo_auth._wait_for_callback(_free_port(), timeout=0.05)


def test_authorization_url_and_callback_parsing(make_settings) -> None:
    settings = qbo_settings(make_settings, QB_OAUTH_SCOPES="com.intuit.quickbooks.accounting openid")
    url = authorization_url(settings, redirect_uri="http://localhost:8765/callback", state="S1")
    assert url == ("https://appcenter.intuit.com/connect/oauth2?client_id=test-qb-client-id&response_type=code"
                   "&scope=com.intuit.quickbooks.accounting%20openid"
                   "&redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback&state=S1")
    assert parse_callback("code=C1&state=S1&realmId=42", expected_state="S1") == ("C1", "42")
    assert parse_callback("code=C1&state=S1", expected_state="S1") == ("C1", None)
    with pytest.raises(AuthError, match="no code="):
        parse_callback("state=S1&realmId=42", expected_state="S1")
    with pytest.raises(AuthError, match="state mismatch"):
        parse_callback("code=C1&realmId=42", expected_state="S1")
    assert qbo_auth._query_part(" ?code=C1&state=S1 ") == "code=C1&state=S1"
    assert qbo_auth._query_part("https://x.test/cb?code=C1") == "code=C1"


def test_build_token_entry_without_expires_in_uses_jwt_exp_or_default() -> None:
    payload = base64.urlsafe_b64encode(b'{"exp": 1800000000}').decode().rstrip("=")
    jwt = f"eyJhbGciOiJub25lIn0.{payload}.sig"
    entry = qbo_auth.build_token_entry({"access_token": jwt, "refresh_token": "R"}, realm_id=REALM, seed="",
                                       previous=None, now=NOW)
    assert entry["expires_at"] == 1800000000.0
    entry = qbo_auth.build_token_entry({"access_token": "opaque", "refresh_token": "R"}, realm_id=REALM, seed="",
                                       previous={"refresh_token_expires_at": NOW + 10}, now=NOW)
    assert entry["expires_at"] == pytest.approx(NOW + 3600)
    assert entry["refresh_token_expires_at"] == pytest.approx(NOW + 10)
    with pytest.raises(AuthError, match="no access_token"):
        qbo_auth.build_token_entry({"refresh_token": "R"}, realm_id=REALM, seed="", previous=None, now=NOW)


def test_write_env_value_keeps_bom_and_other_lines(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_bytes("﻿QB_REFRESH_TOKEN=old\nOTHER=1\n".encode("utf-8"))
    assert qbo_auth.write_env_value(env_file, "QB_REFRESH_TOKEN", "new-value")
    assert env_file.read_bytes().startswith(b"\xef\xbb\xbf")
    assert env_file.read_text(encoding="utf-8-sig") == "QB_REFRESH_TOKEN=new-value\nOTHER=1\n"   # any OS newline
