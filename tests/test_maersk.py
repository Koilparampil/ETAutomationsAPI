"""Maersk Track & Trace Events client — 07-carrier-maersk.md §9.3 tests 1–27, plus edge cases.

respx mocks every HTTP call (no network, 12-testing §1). The client is built as 07 §9.3 says:
`MaerskClient(settings, TokenStore(tmp_path / "tokens.json"), http=httpx.AsyncClient())` with
base URL https://api.maersk.test, so nothing can reach the real host. Fixtures: tests/fixtures/maersk/.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from eta_automations.carriers import dcsa, maersk
from eta_automations.carriers.base import CarrierClient
from eta_automations.carriers.dcsa import DcsaEvent
from eta_automations.carriers.maersk import (
    EMPTY_EVENTS_MSG,
    MaerskAuth,
    MaerskClient,
    choose_arrival,
    key_fingerprint,
    select_final_arrival,
)
from eta_automations.config import MaerskSettings
from eta_automations.errors import AuthError, CarrierLookupError, NoEtaError
from eta_automations.models import Carrier
from eta_automations.token_store import TokenStore
from fakes import UnwritableTokenStore

FIX = Path(__file__).parent / "fixtures" / "maersk"
BASE_URL = "https://api.maersk.test"
TOKEN_URL = f"{BASE_URL}/customer-identity/oauth/v2/access_token"
EVENTS_URL = f"{BASE_URL}/track-and-trace-private/events"
CLIENT_ID = "TestConsumerKey0123456789abcdefX"  # synthetic, 32 characters like a real Consumer Key
CLIENT_SECRET = "test-maersk-client-secret-value"


def fixture_json(name: str) -> Any:
    return json.loads((FIX / name).read_text(encoding="utf-8"))


TOKEN_OK = fixture_json("token_ok.json")
ACCESS_TOKEN = TOKEN_OK["access_token"]

API_ERROR_401 = {"id": "e401", "method": "GET", "requestUri": "/track-and-trace-private/events", "status": 401,
                 "timestamp": "05-11-2026 09:00:00", "message": "Unauthorized", "debugMessage": "Token expired"}
API_ERROR_403 = {"id": "e403", "method": "GET", "requestUri": "/track-and-trace-private/events", "status": 403,
                 "timestamp": "05-11-2026 09:00:00", "message": "Forbidden",
                 "debugMessage": "Application is not subscribed to this API product"}
API_ERROR_400 = {"id": "e400", "method": "GET", "requestUri": "/track-and-trace-private/events", "status": 400,
                 "timestamp": "05-11-2026 09:00:00", "message": "Bad Request",
                 "debugMessage": "Validation failed", "subErrors": [
                     {"field": "carrierBookingReference", "rejectedValue": "ZZZZZZZZZ", "message": "unknown"}]}


def token_response(body: Mapping[str, Any] | None = None, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=dict(TOKEN_OK if body is None else body))


def events_response(name: str, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
    return httpx.Response(200, json=fixture_json(name), headers=dict(headers or {}))


@pytest.fixture
def maersk_settings(make_settings: Callable[..., Any]) -> Callable[..., MaerskSettings]:
    """maersk_settings(**env) -> MaerskSettings pointing at https://api.maersk.test (07 §9.3)."""

    def _make(**env: Any) -> MaerskSettings:
        settings = make_settings({
            "MAERSK_CLIENT_ID": CLIENT_ID,
            "MAERSK_CLIENT_SECRET": CLIENT_SECRET,
            "MAERSK_BASE_URL": BASE_URL,
            "MAERSK_TOKEN_URL": TOKEN_URL,
            **env,
        })
        assert settings.maersk is not None
        return settings.maersk

    return _make


@pytest.fixture
def store(tmp_path: Path) -> TokenStore:
    return TokenStore(tmp_path / "tokens.json")


@contextlib.asynccontextmanager
async def open_client(settings: MaerskSettings, store: TokenStore) -> AsyncIterator[MaerskClient]:
    """MaerskClient on an injected (respx-mocked) httpx.AsyncClient; closes both."""
    http = httpx.AsyncClient()
    client = MaerskClient(settings, store, http=http)
    try:
        yield client
    finally:
        await client.aclose()
        await http.aclose()


async def lookup_error(settings: MaerskSettings, store: TokenStore, *events: httpx.Response,
                       booking: str = "210000001") -> tuple[CarrierLookupError, respx.Route]:
    """Run get_eta() against the given events responses and return the CarrierLookupError raised."""
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(side_effect=list(events))
        async with open_client(settings, store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta(booking)
    return info.value, route


def seed_token(store: TokenStore, *, token: str, fingerprint: str, expires_in: float = 3600) -> None:
    now = time.time()
    store.put("maersk", {"access_token": token, "expires_at": now + expires_in, "token_type": "Bearer",
                         "scope": "openid", "obtained_at": now, "key_fingerprint": fingerprint})


def ev(event_type: str, type_code: str, classifier: str, when: datetime, *, loc: str | None,
       created: datetime | None = None, mode: str | None = "VESSEL", facility: str | None = None,
       facility_type: str | None = None, eq: str | None = None, empty: str | None = None,
       index: int = 0) -> DcsaEvent:
    """Hand-built DcsaEvent (positional fields as in 09 §A.3.2)."""
    return DcsaEvent(event_type, classifier, type_code, when, created, loc, facility, facility_type, mode, eq,
                     None, None, {}, empty_indicator=empty, index=index)


# ───────────────────────── 1. factory ─────────────────────────
async def test_build_client_none_when_unconfigured(make_settings: Callable[..., Any], store: TokenStore) -> None:
    unconfigured = make_settings()
    assert unconfigured.maersk is None
    assert maersk.build_client(unconfigured, store) is None
    only_id = make_settings(MAERSK_CLIENT_ID=CLIENT_ID)  # both credentials are required (07 §3)
    assert only_id.maersk is None and maersk.build_client(only_id, store) is None
    assert only_id.missing["maersk"] == ("MAERSK_CLIENT_SECRET",)

    configured = make_settings(MAERSK_CLIENT_ID=CLIENT_ID, MAERSK_CLIENT_SECRET=CLIENT_SECRET,
                               MAERSK_BASE_URL=BASE_URL, MAERSK_TOKEN_URL=TOKEN_URL)
    async with httpx.AsyncClient() as http:
        client = maersk.build_client(configured, store, http=http)
        assert isinstance(client, MaerskClient)
        assert isinstance(client, CarrierClient)
        assert client.carrier is Carrier.MAERSK
        await client.aclose()
        assert not http.is_closed  # an injected client is never closed by the carrier client


# ───────────────────────── 2. booking validation ─────────────────────────
@pytest.mark.parametrize("booking", ["26754662", "EBKG1234567", "", "2675466 7"])
async def test_invalid_booking_rejected_without_http(booking: str, maersk_settings: Callable[..., MaerskSettings],
                                                     store: TokenStore) -> None:
    async with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        events_route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(), store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta(booking)
        assert router.calls.call_count == 0  # checked inside the block: respx rolls router calls back on exit
    assert str(info.value) == f"'{booking}' is not a valid Maersk booking reference (expected 9 letters/digits)."
    assert token_route.call_count == 0 and events_route.call_count == 0


# ───────────────────────── 3–7. token lifecycle ─────────────────────────
async def test_token_request_shape(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        before = time.time()
        async with open_client(maersk_settings(), store) as client:
            await client.get_eta("210000001")
        after = time.time()

    assert token_route.call_count == 1
    req = token_route.calls[0].request
    assert req.method == "POST"
    assert req.headers["Consumer-Key"] == CLIENT_ID
    assert req.headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert req.headers["Accept"] == "application/json"
    assert "Authorization" not in req.headers
    assert parse_qs(req.content.decode()) == {
        "grant_type": ["client_credentials"], "client_id": [CLIENT_ID], "client_secret": [CLIENT_SECRET]}

    entry = store.get("maersk")
    assert entry is not None
    assert set(entry) == {"access_token", "expires_at", "token_type", "scope", "obtained_at", "key_fingerprint"}
    assert before + 7199 - 1 <= entry["expires_at"] <= after + 7199 + 1
    assert entry["key_fingerprint"] == hashlib.sha256(CLIENT_ID.encode()).hexdigest()[:16]
    assert entry["access_token"] == ACCESS_TOKEN
    assert entry["token_type"] == "Bearer" and entry["scope"] == "openid"
    stored = json.dumps(entry)
    assert CLIENT_SECRET not in stored and CLIENT_ID not in stored and "synthetic-id-token" not in stored


async def test_token_cached_across_bookings(maersk_settings: Callable[..., MaerskSettings],
                                            store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        events_route = router.get(EVENTS_URL).mock(side_effect=[
            events_response("events_in_transit.json"), events_response("events_arrived.json")])
        async with open_client(maersk_settings(), store) as client:
            first = await client.get_eta("210000001")
            second = await client.get_eta("210000002")
    assert token_route.call_count == 1
    assert events_route.call_count == 2
    assert [c.request.headers["Authorization"] for c in events_route.calls] == [f"Bearer {ACCESS_TOKEN}"] * 2
    assert first.eta == datetime(2026, 11, 16) and second.eta == datetime(2026, 11, 2)


async def test_token_reused_from_store(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    seed_token(store, token="seeded-maersk-token-value", fingerprint=key_fingerprint(CLIENT_ID))
    async with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        events_route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(), store) as client:
            result = await client.get_eta("210000001")
    assert token_route.call_count == 0
    assert events_route.calls[0].request.headers["Authorization"] == "Bearer seeded-maersk-token-value"
    assert result.eta == datetime(2026, 11, 16)


async def test_token_fingerprint_mismatch_refetches(maersk_settings: Callable[..., MaerskSettings],
                                                    store: TokenStore) -> None:
    seed_token(store, token="token-of-another-app", fingerprint=key_fingerprint("SomeOtherConsumerKey0000000000000"))
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        events_route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(), store) as client:
            await client.get_eta("210000001")
    assert token_route.call_count == 1
    assert events_route.calls[0].request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    entry = store.get("maersk")
    assert entry is not None and entry["key_fingerprint"] == key_fingerprint(CLIENT_ID)


async def test_stale_cached_token_refetched(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    # within the 120 s skew → not fresh (03 §7)
    seed_token(store, token="almost-expired-token", fingerprint=key_fingerprint(CLIENT_ID), expires_in=60)
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(), store) as client:
            await client.get_eta("210000001")
    assert token_route.call_count == 1


async def test_token_expiry_from_jwt_when_no_expires_in(maersk_settings: Callable[..., MaerskSettings],
                                                        store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response(fixture_json("token_no_expires_in.json")))
        async with httpx.AsyncClient() as http:
            auth = MaerskAuth(maersk_settings(), store, http, wall_time=lambda: 1793850000.0)
            token = await auth.get_token()
            cached = auth.cached_entry()
    assert token == ACCESS_TOKEN
    entry = store.get("maersk")
    assert entry is not None and entry["expires_at"] == 1793860000  # the JWT exp: 2026-11-05T06:26:40Z
    assert cached is not None and "access_token" not in cached and cached["expires_at"] == 1793860000


@pytest.mark.parametrize(
    ("body", "expected_offset"),
    [
        pytest.param({"access_token": "opaque-token-without-exp", "token_type": "Bearer"}, 7000, id="default_lifetime"),
        pytest.param({"access_token": "opaque-token-without-exp", "expires_in": "3600"}, 3600, id="numeric_string"),
        pytest.param({"access_token": "opaque-token-without-exp", "expires_in": "soon"}, 7000, id="invalid_value"),
        pytest.param({"access_token": "opaque-token-without-exp", "expires_in": 0}, 7000, id="zero"),
    ],
)
async def test_token_expiry_fallbacks(body: dict[str, Any], expected_offset: int,
                                      maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response(body))
        async with httpx.AsyncClient() as http:
            auth = MaerskAuth(maersk_settings(), store, http, wall_time=lambda: 1_800_000_000.0)
            assert await auth.get_token() == "opaque-token-without-exp"
    entry = store.get("maersk")
    assert entry is not None and entry["expires_at"] == 1_800_000_000.0 + expected_offset


async def test_token_response_without_access_token(maersk_settings: Callable[..., MaerskSettings],
                                                   store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response({"token_type": "Bearer", "expires_in": 7199}))
        async with httpx.AsyncClient() as http:
            auth = MaerskAuth(maersk_settings(), store, http)
            with pytest.raises(AuthError, match=r"no access_token \(keys: \['expires_in', 'token_type'\]\)"):
                await auth.get_token()
    assert store.get("maersk") is None


async def test_invalidate_deletes_the_entry(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    seed_token(store, token="seeded-maersk-token-value", fingerprint=key_fingerprint(CLIENT_ID))
    async with httpx.AsyncClient() as http:
        auth = MaerskAuth(maersk_settings(), store, http)
        assert auth.cached_entry() is not None
        await auth.invalidate()
        assert auth.cached_entry() is None
    assert store.get("maersk") is None


# ───────────────────────── 8–9. token errors ─────────────────────────
async def test_token_400_is_lookup_error_with_onboarding_hint(maersk_settings: Callable[..., MaerskSettings],
                                                              store: TokenStore) -> None:
    async with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(
            return_value=httpx.Response(400, json=fixture_json("token_error_400.json")))
        events_route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(), store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("210000001")
    message = str(info.value)
    assert "not yet approved" in message and "developer.maersk.com" in message
    assert message.startswith("Maersk API authentication failed: Maersk token request rejected (HTTP 400).")
    assert isinstance(info.value.__cause__, AuthError)
    assert token_route.call_count == 1  # 400 is not retried
    assert events_route.call_count == 0
    assert CLIENT_SECRET not in message and CLIENT_ID not in message


async def test_token_401_gateway_key_error(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    async with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(401, json=fixture_json("error_gateway_401.json")))
        events_route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(), store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("210000001")
    message = str(info.value)
    assert "Consumer-Key" in message and "ERR_GW_001" in message
    assert "MAERSK_CLIENT_ID must be the app's Consumer Key" in message
    assert events_route.call_count == 0
    assert CLIENT_ID not in message


async def test_token_403_and_other_failures(maersk_settings: Callable[..., MaerskSettings], store: TokenStore,
                                            http_sleeps: list[float]) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(side_effect=[httpx.Response(403, json={"error": "forbidden"})]
                                    + [httpx.Response(502, text="bad gateway")] * 4)
        async with httpx.AsyncClient() as http:
            auth = MaerskAuth(maersk_settings(), store, http)
            with pytest.raises(AuthError, match=r"refused the token request \(HTTP 403\) — the key may be revoked"):
                await auth.get_token()
            with pytest.raises(AuthError, match=r"^Maersk token request failed: maersk: unexpected HTTP 502"):
                await auth.get_token()  # 502 on an idempotent token call is retried (03 §6), then reported
    assert len(http_sleeps) == 3


# ───────────────────────── 10. request shape ─────────────────────────
@pytest.mark.parametrize("api_version", [None, "1"])
async def test_events_request_shape(api_version: str | None, maersk_settings: Callable[..., MaerskSettings],
                                    store: TokenStore) -> None:
    env = {} if api_version is None else {"MAERSK_API_VERSION": api_version}
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(**env), store) as client:
            await client.get_eta(" 210000001 ")
    assert route.call_count == 1
    req = route.calls[0].request
    assert req.method == "GET"
    assert (req.url.scheme, req.url.host) == ("https", "api.maersk.test")
    assert req.url.path == "/track-and-trace-private/events"
    assert dict(req.url.params) == {
        "carrierBookingReference": "210000001", "eventType": "TRANSPORT,EQUIPMENT", "limit": "100"}
    assert req.headers["Consumer-Key"] == CLIENT_ID
    assert req.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert req.headers["Accept"] == "application/json"
    if api_version is None:
        assert "API-Version" not in req.headers
    else:
        assert req.headers["API-Version"] == "1"


async def test_page_limit_setting(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(MAERSK_PAGE_LIMIT="25"), store) as client:
            await client.get_eta("210000001")
    assert route.calls[0].request.url.params["limit"] == "25"


# ───────────────────────── 11–16. HTTP error mapping ─────────────────────────
async def test_401_refreshes_token_once_then_succeeds(maersk_settings: Callable[..., MaerskSettings],
                                                      store: TokenStore) -> None:
    second = {**TOKEN_OK, "access_token": "second-token-after-401"}
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(side_effect=[token_response(), token_response(second)])
        events_route = router.get(EVENTS_URL).mock(side_effect=[
            httpx.Response(401, json=API_ERROR_401), events_response("events_in_transit.json")])
        async with open_client(maersk_settings(), store) as client:
            result = await client.get_eta("210000001")
    assert token_route.call_count == 2
    assert events_route.call_count == 2
    assert events_route.calls[0].request.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert events_route.calls[1].request.headers["Authorization"] == "Bearer second-token-after-401"
    assert result.eta == datetime(2026, 11, 16)
    entry = store.get("maersk")
    assert entry is not None and entry["access_token"] == "second-token-after-401"


async def test_401_twice_is_lookup_error(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        events_route = router.get(EVENTS_URL).mock(return_value=httpx.Response(401, json=API_ERROR_401))
        async with open_client(maersk_settings(), store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("210000001")
    assert token_route.call_count == 2 and events_route.call_count == 2
    assert str(info.value) == "Maersk API authentication failed: maersk: credentials/token rejected (HTTP 401)"
    assert isinstance(info.value.__cause__, AuthError)


# ── review fix MSK-05: tokens.json that cannot be written ──
async def test_token_kept_in_memory_when_tokens_json_cannot_be_written(
        maersk_settings: Callable[..., MaerskSettings], tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """MSK-05 (03 §5 boundary rule): the token cache is best effort. A token that cannot be saved is kept for this
    process, so two bookings need one token call; one WARNING, never the token."""
    caplog.set_level(logging.WARNING, logger="eta")
    store = UnwritableTokenStore(tmp_path / "tokens.json")
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        events_route = router.get(EVENTS_URL).mock(side_effect=[
            events_response("events_in_transit.json"), events_response("events_arrived.json")])
        async with open_client(maersk_settings(), store) as client:
            first = await client.get_eta("210000001")
            second = await client.get_eta("210000002")
    assert token_route.call_count == 1 and events_route.call_count == 2
    assert first.eta == datetime(2026, 11, 16) and second.eta == datetime(2026, 11, 2)
    warned = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING and "tokens.json" in r.getMessage()]
    assert len(warned) == 1 and ACCESS_TOKEN not in warned[0]


async def test_rejected_token_that_cannot_be_removed_is_not_reused(maersk_settings: Callable[..., MaerskSettings],
                                                                   tmp_path: Path) -> None:
    """MSK-05: after a 401 the rejected token cannot be deleted from tokens.json; it is not read back, so the retry
    uses a new token."""
    path = tmp_path / "tokens.json"
    seed_token(TokenStore(path), token="rejected-maersk-token", fingerprint=key_fingerprint(CLIENT_ID))
    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        events_route = router.get(EVENTS_URL).mock(side_effect=[
            httpx.Response(401, json=API_ERROR_401), events_response("events_in_transit.json")])
        async with open_client(maersk_settings(), UnwritableTokenStore(path)) as client:
            result = await client.get_eta("210000001")
    assert result.eta == datetime(2026, 11, 16) and token_route.call_count == 1
    assert [c.request.headers["Authorization"] for c in events_route.calls] == [
        "Bearer rejected-maersk-token", f"Bearer {ACCESS_TOKEN}"]


async def test_404_is_lookup_error(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    error, route = await lookup_error(maersk_settings(), store,
                                      httpx.Response(404, json=fixture_json("error_404.json")))
    assert "HTTP 404" in str(error)
    assert str(error) == ("Maersk has no shipment for booking 210000001 (HTTP 404). Check the number; bookings "
                          "not linked to your Maersk customer account are not visible to the API.")
    assert route.call_count == 1


async def test_edge_html_403_is_lookup_error(maersk_settings: Callable[..., MaerskSettings], store: TokenStore,
                                             caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.maersk")
    html = (FIX / "edge_403.html").read_text(encoding="utf-8")
    error, route = await lookup_error(
        maersk_settings(), store, httpx.Response(403, text=html, headers={"Content-Type": "text/html"}))
    message = str(error)
    assert "edge" in message and "Access Denied" in message
    assert message == maersk.EDGE_403_MSG
    assert CLIENT_ID not in message and CLIENT_SECRET not in message and ACCESS_TOKEN not in message
    assert route.call_count == 1  # a 403 is neither retried nor treated as an expired token (07 §5)
    assert route.calls[0].response.headers["Content-Type"].startswith("text/html")
    # the Akamai reference number is logged for Maersk support; no secret is
    assert "Reference" in caplog.text and "edgesuite" in caplog.text
    assert CLIENT_ID not in caplog.text and ACCESS_TOKEN not in caplog.text


async def test_json_403_is_lookup_error_mentions_approval(maersk_settings: Callable[..., MaerskSettings],
                                                          store: TokenStore) -> None:
    error, route = await lookup_error(maersk_settings(), store, httpx.Response(403, json=API_ERROR_403))
    message = str(error)
    assert message.startswith("Maersk API refused access (HTTP 403): the app is not approved for Track & Trace "
                              "Events, or booking 210000001 is not visible to your account.")
    assert "approved" in message and "developer.maersk.com" in message
    assert route.call_count == 1


async def test_429_retried_then_success(maersk_settings: Callable[..., MaerskSettings], store: TokenStore,
                                        http_sleeps: list[float]) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}), events_response("events_in_transit.json")])
        async with open_client(maersk_settings(), store) as client:
            result = await client.get_eta("210000001")
    assert route.call_count == 2
    assert http_sleeps == [0.0]  # Retry-After honoured by http.request()
    assert result.eta == datetime(2026, 11, 16)


async def test_429_after_retries_is_rate_limit_message(maersk_settings: Callable[..., MaerskSettings],
                                                       store: TokenStore, http_sleeps: list[float]) -> None:
    error, route = await lookup_error(maersk_settings(), store,
                                      *[httpx.Response(429, headers={"Retry-After": "1"})] * 4)
    assert route.call_count == 4
    assert str(error) == maersk.RATE_LIMIT_MSG


@pytest.mark.parametrize(
    ("responses", "expected_start"),
    [
        pytest.param([httpx.Response(400, json=API_ERROR_400)],
                     "Maersk rejected the request for 210000001 (HTTP 400): maersk: unexpected HTTP 400", id="400"),
        pytest.param([httpx.Response(500, json={"message": "boom"})] * 4,
                     "Maersk API error for 210000001: maersk: unexpected HTTP 500", id="500_after_retries"),
        pytest.param([httpx.Response(200, text="<html>maintenance</html>")],
                     "Maersk API error for 210000001: maersk: response was not valid JSON", id="not_json"),
        pytest.param([httpx.ConnectError("connection refused")] * 4,
                     "Maersk API network error: maersk: network error: ConnectError", id="network"),
    ],
)
async def test_other_errors_are_lookup_errors(responses: list[Any], expected_start: str,
                                              maersk_settings: Callable[..., MaerskSettings], store: TokenStore,
                                              http_sleeps: list[float]) -> None:
    error, _ = await lookup_error(maersk_settings(), store, *responses)
    assert str(error).startswith(expected_start)


async def test_service_level_failures_have_identical_texts(maersk_settings: Callable[..., MaerskSettings],
                                                           tmp_path: Path, http_sleeps: list[float]) -> None:
    """10 §6: the circuit breaker counts identical consecutive texts, so outage-type failures must not embed
    the booking number (network down, edge block, rate limit, rejected credentials)."""
    settings = maersk_settings()
    cases = {
        "network": [httpx.ConnectError("connection refused")] * 4,
        "edge": [httpx.Response(403, text=(FIX / "edge_403.html").read_text(encoding="utf-8"))],
        "rate_limit": [httpx.Response(429, headers={"Retry-After": "0"})] * 4,
    }
    for name, responses in cases.items():
        texts = set()
        for booking in ("210000001", "210000002"):
            store = TokenStore(tmp_path / f"tokens-{name}-{booking}.json")
            error, _ = await lookup_error(settings, store, *responses, booking=booking)
            texts.add(str(error))
        assert len(texts) == 1, name
        assert "21000000" not in texts.pop(), name


async def test_get_eta_raises_only_boundary_errors(maersk_settings: Callable[..., MaerskSettings],
                                                   store: TokenStore, http_sleeps: list[float]) -> None:
    """03 §5: every failure mode above surfaces as CarrierLookupError (or NoEtaError), never as
    ServiceError/AuthError/httpx errors."""
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(side_effect=[httpx.ReadTimeout("slow")] * 4)
        async with open_client(maersk_settings(), store) as client:
            with pytest.raises(CarrierLookupError) as info:
                await client.get_eta("210000001")
    assert "network error" in str(info.value)


# ───────────────────────── 17–18. empty / no arrival ─────────────────────────
async def test_empty_events_is_lookup_error(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    error, route = await lookup_error(maersk_settings(), store, events_response("events_empty.json"),
                                      booking="210000005")
    assert "no tracking events" in str(error)
    assert str(error) == EMPTY_EVENTS_MSG.format(ref="210000005")
    assert route.call_count == 1  # no retries on an empty answer (07 §7.6)


async def test_no_arrival_is_no_eta_error(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(return_value=events_response("events_no_arrival.json"))
        async with open_client(maersk_settings(), store) as client:
            with pytest.raises(NoEtaError) as info:
                await client.get_eta("210000006")
    assert str(info.value) == (
        "No ETA published by Maersk for booking 210000006 (events seen: {'EQUIPMENT/GTIN/ACT': 1, "
        "'EQUIPMENT/LOAD/ACT': 1, 'SHIPMENT/CONF/ACT': 1, 'TRANSPORT/DEPA/PLN': 1}). Can't determine ETA.")


# ───────────────────────── 19. expected results (07 §9.2) ─────────────────────────
EXPECTED: dict[str, dict[str, Any]] = {
    "in_transit": {
        "booking": "210000001", "pages": ["events_in_transit.json"],
        "eta": datetime(2026, 11, 16), "is_actual": False,
        "details": {"unlocode": "USNYC", "eta_exact": "2026-11-16T07:00:00", "classifier": "EST"},
        "containers": ("MSKU1111111", "MSKU2222222"), "source_has": "USNYC",
    },
    "in_transit_exact_time": {
        "booking": "210000001", "pages": ["events_in_transit.json"], "env": {"MAERSK_ETA_DATE_ONLY": "false"},
        "eta": datetime(2026, 11, 16, 7, 0), "is_actual": False,  # offset dropped, local wall time kept
        "details": {"unlocode": "USNYC", "eta_exact": "2026-11-16T07:00:00"},
    },
    "arrived": {
        "booking": "210000002", "pages": ["events_arrived.json"],
        "eta": datetime(2026, 11, 2), "is_actual": True,
        "details": {"unlocode": "USSAV", "classifier": "ACT"},
    },
    "ipi_rail_final": {
        "booking": "210000003", "pages": ["events_ipi_rail.json"],
        "eta": datetime(2026, 11, 18), "is_actual": False,
        "details": {"unlocode": "USCHI", "mode": "RAIL", "classifier": "PLN", "eta_exact": "2026-11-18T14:00:00"},
    },
    "ipi_rail_pod": {
        "booking": "210000003", "pages": ["events_ipi_rail.json"], "env": {"MAERSK_ETA_TARGET": "pod"},
        "eta": datetime(2026, 11, 12), "is_actual": False,
        "details": {"unlocode": "USLAX", "target": "pod"},
    },
    "transshipment": {
        "booking": "210000004", "pages": ["events_transshipment.json"],
        "eta": datetime(2026, 11, 9), "is_actual": False,
        "details": {"unlocode": "USNYC"},  # SGSIN excluded because it has a DEPA
    },
    "empty": {
        "booking": "210000005", "pages": ["events_empty.json"],
        "raises": (CarrierLookupError, "no tracking events"),
    },
    "no_arrival": {
        "booking": "210000006", "pages": ["events_no_arrival.json"],
        "raises": (NoEtaError, "Can't determine ETA"),
    },
    "paged": {
        "booking": "210000007",
        "pages": [("events_page1.json", {"Current-Page": "1", "Next-Page": "2"}),
                  ("events_page2.json", {"Current-Page": "2"})],
        "eta": datetime(2026, 11, 20), "is_actual": False, "gets": 2, "second_cursor": "2",
    },
}


@pytest.mark.parametrize("case", list(EXPECTED))
async def test_expected_eta(case: str, maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    spec = EXPECTED[case]
    responses = []
    for page in spec["pages"]:
        name, headers = (page, {}) if isinstance(page, str) else page
        responses.append(events_response(name, headers=headers))
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(side_effect=responses)
        async with open_client(maersk_settings(**spec.get("env", {})), store) as client:
            if "raises" in spec:
                exc_type, text = spec["raises"]
                with pytest.raises(exc_type, match=text):
                    await client.get_eta(spec["booking"])
                return
            result = await client.get_eta(spec["booking"])

    assert result.carrier is Carrier.MAERSK
    assert result.booking == spec["booking"]
    assert result.eta == spec["eta"]
    assert result.eta.tzinfo is None
    assert result.is_actual is spec["is_actual"]
    assert result.from_transshipment_rule is False
    assert result.source.startswith("Maersk ")
    for key, value in spec.get("details", {}).items():
        assert result.details[key] == value, key
    if "containers" in spec:
        assert result.containers == spec["containers"]
    if "source_has" in spec:
        assert spec["source_has"] in result.source
    if "gets" in spec:
        assert route.call_count == spec["gets"]
        second = route.calls[1].request
        assert second.url.params["cursor"] == spec["second_cursor"]
        assert second.url.params["carrierBookingReference"] == spec["booking"]
    json.dumps(result.to_dict())  # details stay JSON-serializable for the run report


# ───────────────────────── 20–23. pagination and payload shapes ─────────────────────────
@pytest.mark.parametrize("form", ["cursor", "absolute_url", "path"])
async def test_pagination_cursor_and_url_forms(form: str, maersk_settings: Callable[..., MaerskSettings],
                                               store: TokenStore) -> None:
    next_page = {
        "cursor": "2",
        "absolute_url": f"{EVENTS_URL}?carrierBookingReference=210000007&cursor=abc&limit=100",
        "path": "/track-and-trace-private/events?cursor=def&carrierBookingReference=210000007",
    }[form]
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(side_effect=[
            events_response("events_page1.json", headers={"Current-Page": "1", "Next-Page": next_page}),
            events_response("events_page2.json", headers={"Current-Page": "2"}),
        ])
        async with open_client(maersk_settings(), store) as client:
            result = await client.get_eta("210000007")
    assert route.call_count == 2
    first, second = (call.request for call in route.calls)
    assert dict(first.url.params) == {
        "carrierBookingReference": "210000007", "eventType": "TRANSPORT,EQUIPMENT", "limit": "100"}
    if form == "cursor":
        assert dict(second.url.params) == {
            "carrierBookingReference": "210000007", "eventType": "TRANSPORT,EQUIPMENT", "limit": "100",
            "cursor": "2"}
    elif form == "absolute_url":
        assert str(second.url) == next_page  # followed with its own query
    else:
        assert second.url.host == "api.maersk.test" and second.url.path == "/track-and-trace-private/events"
        assert dict(second.url.params) == {"cursor": "def", "carrierBookingReference": "210000007"}
    assert second.headers["Consumer-Key"] == CLIENT_ID
    assert second.headers["Authorization"] == f"Bearer {ACCESS_TOKEN}"
    assert result.eta == datetime(2026, 11, 20)


@pytest.mark.parametrize("link", [
    pytest.param("https://evil.example.test/track-and-trace-private/events?cursor=2", id="foreign_host"),
    pytest.param("http://api.maersk.test/track-and-trace-private/events?cursor=2", id="plain_http_same_host"),
    pytest.param("https://api.maersk.test:8443/track-and-trace-private/events?cursor=2", id="other_port"),
])
async def test_pagination_foreign_link_not_followed(link: str, maersk_settings: Callable[..., MaerskSettings],
                                                    store: TokenStore, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.maersk")
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(
            return_value=events_response("events_in_transit.json", headers={"Current-Page": "1", "Next-Page": link}))
        async with open_client(maersk_settings(), store) as client:
            result = await client.get_eta("210000001")
        assert router.calls.call_count == 2  # token + one GET: nothing was sent anywhere else
    assert route.call_count == 1
    assert result.eta == datetime(2026, 11, 16)  # the events collected so far are used
    assert "ignoring Next-Page link to foreign host" in caplog.text


@pytest.mark.parametrize("link", ["/track-and-trace-private/events?cursor=\x01", "?cursor=\x7f",
                                  "https://api.maersk.test/track-and-trace-private/events?cursor=\x01"])
async def test_pagination_malformed_link_not_followed(link: str, maersk_settings: Callable[..., MaerskSettings],
                                                      store: TokenStore, caplog: pytest.LogCaptureFixture) -> None:
    """httpx.InvalidURL is not an httpx.HTTPError: it must not escape get_eta() (03 §5)."""
    caplog.set_level(logging.WARNING, logger="eta.carrier.maersk")
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(
            return_value=events_response("events_in_transit.json", headers={"Next-Page": link}))
        async with open_client(maersk_settings(), store) as client:
            result = await client.get_eta("210000001")
    assert route.call_count == 1
    assert result.eta == datetime(2026, 11, 16)
    assert "ignoring malformed Next-Page link for 210000001" in caplog.text


async def test_pagination_stops_when_next_equals_current(maersk_settings: Callable[..., MaerskSettings],
                                                         store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(return_value=events_response(
            "events_in_transit.json", headers={"Current-Page": "7", "Next-Page": "7"}))
        async with open_client(maersk_settings(), store) as client:
            await client.get_eta("210000001")
    assert route.call_count == 1


async def test_max_pages_cap(maersk_settings: Callable[..., MaerskSettings], store: TokenStore,
                             caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="eta.carrier.maersk")
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(return_value=events_response(
            "events_page2.json", headers={"Current-Page": "1", "Next-Page": "next-cursor"}))
        async with open_client(maersk_settings(MAERSK_MAX_PAGES="3"), store) as client:
            result = await client.get_eta("210000007")
    assert route.call_count == 3
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING and r.name == "eta.carrier.maersk"]
    assert len(warnings) == 1
    assert warnings[0].getMessage() == "[Maersk] stopped after 3 pages for 210000007 (MAERSK_MAX_PAGES)"
    assert result.eta == datetime(2026, 11, 20)


async def test_bare_array_payload_accepted(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    wrapped = fixture_json("events_in_transit.json")
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(side_effect=[
            httpx.Response(200, json=wrapped), httpx.Response(200, json=wrapped["events"])])
        async with open_client(maersk_settings(), store) as client:
            from_wrapped = await client.get_eta("210000001")
            from_bare = await client.get_eta("210000001")
    assert from_bare == from_wrapped
    assert from_bare.eta == datetime(2026, 11, 16)


@pytest.mark.parametrize(("body", "listed"), [
    pytest.param({"foo": 1}, "['foo']", id="object"),
    pytest.param({"events": {"not": "a list"}, "meta": 1}, "['events', 'meta']", id="events_not_a_list"),
    pytest.param("just text", "str", id="string"),
])
async def test_unexpected_payload_shape(body: Any, listed: str, maersk_settings: Callable[..., MaerskSettings],
                                        store: TokenStore) -> None:
    error, _ = await lookup_error(maersk_settings(), store, httpx.Response(200, json=body))
    assert str(error) == f"Maersk returned an unexpected payload shape (top-level keys: {listed})"


# ───────────────────────── 24–25. DCSA shapes and pure selection ─────────────────────────
def test_dcsa_parses_maersk_shapes() -> None:
    events = {e.event_id: e for e in dcsa.parse_events(fixture_json("events_in_transit.json"))}
    assert set(events) == {"E1", "E2", "E3", "T1", "T2", "T3", "T4", "S1"}
    assert events["T2"].unlocode == "USNYC"  # only under transportCall.location
    assert events["T2"].facility_code == "APMT"
    assert events["E2"].unlocode == "CNNGB"  # eventLocation (and transportCall)
    assert events["T1"].mode_of_transport == "VESSEL"
    assert events["E1"].equipment_reference == "MSKU1111111"
    assert events["T4"].event_datetime == datetime(2026, 11, 16, 7, 0)  # offset dropped (07 §7.4)
    assert events["T4"].created_datetime == datetime(2026, 11, 3, 9, 0)
    assert events["S1"].event_type == "SHIPMENT"


def _final_case(name: str) -> tuple[list[DcsaEvent], dict[str, Any]]:
    d = datetime
    if name == "stale_pln_at_final_ramp_wins_via_depa":
        # The rail plan to USCHI is stale (dated before the vessel even reaches USLAX), but USLAX has a DEPA,
        # so it is not the final destination: the DEPA rule does not depend on dates (07 §7.2 step 4).
        events = [
            ev("TRANSPORT", "DEPA", "ACT", d(2026, 10, 25, 21, 0), loc="CNSHK", created=d(2026, 10, 25, 14)),
            ev("TRANSPORT", "ARRI", "EST", d(2026, 11, 12, 8, 0), loc="USLAX", created=d(2026, 11, 4, 10),
               facility="APMT", facility_type="POTE"),
            ev("TRANSPORT", "DEPA", "PLN", d(2026, 10, 1, 20, 0), loc="USLAX", created=d(2026, 9, 20),
               mode="RAIL", facility_type="RAMP"),
            ev("TRANSPORT", "ARRI", "PLN", d(2026, 10, 5, 14, 0), loc="USCHI", created=d(2026, 9, 20),
               mode="RAIL", facility="LPC", facility_type="RAMP"),
        ]
        return events, {"eta": d(2026, 10, 5, 14, 0), "classifier": "PLN", "unlocode": "USCHI", "mode": "RAIL",
                        "basis": "TRANSPORT ARRI/PLN at USCHI/LPC by RAIL (final destination)"}
    if name == "no_depa_latest_location_by_time_wins":
        events = [
            ev("TRANSPORT", "ARRI", "EST", d(2026, 11, 9, 10, 0), loc="USNYC", created=d(2026, 11, 1)),
            ev("TRANSPORT", "ARRI", "ACT", d(2026, 10, 8, 6, 0), loc="SGSIN", created=d(2026, 10, 8)),
            ev("TRANSPORT", "ARRI", "PLN", d(2026, 10, 1, 6, 0), loc="CNSHA", created=d(2026, 9, 1)),
        ]
        return events, {"eta": d(2026, 11, 9, 10, 0), "classifier": "EST", "unlocode": "USNYC", "mode": "VESSEL",
                        "basis": "TRANSPORT ARRI/EST at USNYC by VESSEL (final destination)"}
    if name == "disc_act_upgrades_est_to_act":
        events = [
            ev("TRANSPORT", "DEPA", "ACT", d(2026, 10, 5, 20, 0), loc="CNYTN"),
            ev("TRANSPORT", "ARRI", "EST", d(2026, 11, 2, 6, 0), loc="USSAV", created=d(2026, 10, 30),
               facility="GCT"),
            ev("EQUIPMENT", "DISC", "ACT", d(2026, 11, 2, 15, 0), loc="USSAV", eq="MSKU4444444", empty="LADEN"),
            ev("EQUIPMENT", "DISC", "ACT", d(2026, 11, 2, 9, 0), loc="USSAV", eq="MSKU3333333", empty="LADEN"),
        ]
        return events, {"eta": d(2026, 11, 2, 9, 0), "classifier": "ACT", "unlocode": "USSAV", "mode": "VESSEL",
                        "basis": "EQUIPMENT DISC/ACT at USSAV (final destination)",
                        "equipment_reference": "MSKU3333333", "facility_code": "GCT"}
    if name == "naive_and_aware_created_do_not_raise":
        events = [
            ev("TRANSPORT", "ARRI", "EST", d(2026, 11, 15, 22, 0), loc="USNYC", created=d(2026, 10, 25, 9, 0)),
            ev("TRANSPORT", "ARRI", "EST", d(2026, 11, 16, 7, 0), loc="USNYC",
               created=d(2026, 11, 3, 9, 0, tzinfo=timezone.utc)),
            ev("TRANSPORT", "ARRI", "PLN", d(2026, 11, 14, 6, 0, tzinfo=timezone.utc), loc="usnyc", created=None),
        ]
        return events, {"eta": d(2026, 11, 16, 7, 0), "classifier": "EST", "unlocode": "USNYC", "mode": "VESSEL",
                        "basis": "TRANSPORT ARRI/EST at USNYC by VESSEL (final destination)"}
    raise AssertionError(name)


@pytest.mark.parametrize("name", [
    "stale_pln_at_final_ramp_wins_via_depa",
    "no_depa_latest_location_by_time_wins",
    "disc_act_upgrades_est_to_act",
    "naive_and_aware_created_do_not_raise",
])
def test_select_final_arrival_unit(name: str) -> None:
    events, expected = _final_case(name)
    picked = select_final_arrival(events)
    assert picked is not None
    arrival, mode = picked
    assert mode == expected["mode"]
    assert arrival.eta == expected["eta"] and arrival.eta.tzinfo is None
    assert arrival.classifier == expected["classifier"]
    assert arrival.unlocode == expected["unlocode"]
    assert arrival.basis == expected["basis"]
    assert arrival.equipment_reference == expected.get("equipment_reference")
    if "facility_code" in expected:
        assert arrival.facility_code == expected["facility_code"]


def test_select_final_arrival_edge_cases() -> None:
    d = datetime
    assert select_final_arrival([]) is None
    # no TRANSPORT ARRI at all (equipment only, departures only)
    assert select_final_arrival([ev("TRANSPORT", "DEPA", "ACT", d(2026, 10, 1), loc="CNNGB"),
                                 ev("EQUIPMENT", "LOAD", "ACT", d(2026, 10, 1), loc="CNNGB", eq="MSKU1")]) is None
    # an unknown classifier (REQ) is never an ETA (09 §A.3.1 rank 0 = unusable)
    assert select_final_arrival([ev("TRANSPORT", "ARRI", "REQ", d(2026, 11, 1), loc="USNYC")]) is None
    # every arrival location also has a departure → fall back to all representatives
    loop = [ev("TRANSPORT", "ARRI", "EST", d(2026, 11, 1), loc="USNYC"),
            ev("TRANSPORT", "DEPA", "EST", d(2026, 11, 2), loc="USNYC")]
    picked = select_final_arrival(loop)
    assert picked is not None and picked[0].unlocode == "USNYC"
    # an EMPTY box discharge does not prove arrival (shared DCSA rule, 09 §A.6 test 15)
    empty_disc = [ev("TRANSPORT", "ARRI", "EST", d(2026, 11, 2, 6), loc="USSAV"),
                  ev("EQUIPMENT", "DISC", "ACT", d(2026, 11, 2, 9), loc="USSAV", eq="MSKU9", empty="EMPTY")]
    picked = select_final_arrival(empty_disc)
    assert picked is not None and picked[0].classifier == "EST" and picked[0].eta == d(2026, 11, 2, 6)
    # no mode on the representative
    no_mode = select_final_arrival([ev("TRANSPORT", "ARRI", "EST", d(2026, 11, 2), loc="USSAV", mode=None)])
    assert no_mode is not None and no_mode[1] is None
    assert no_mode[0].basis == "TRANSPORT ARRI/EST at USSAV by unknown mode (final destination)"


def test_choose_arrival_unit() -> None:
    rail = dcsa.parse_events(fixture_json("events_ipi_rail.json"))
    final = choose_arrival(rail, target="final", policy="first")
    pod = choose_arrival(rail, target="pod", policy="first")
    assert final is not None and final[1] == "RAIL" and final[0].unlocode == "USCHI"
    assert pod is not None and pod[1] == "VESSEL" and pod[0].unlocode == "USLAX"
    assert pod[0].eta == datetime(2026, 11, 12, 8, 0) and pod[0].basis == "TRANSPORT ARRI EST"
    # a seaport final destination is answered by the shared DCSA rules (per-container policy, DISC handling)
    arrived = choose_arrival(dcsa.parse_events(fixture_json("events_arrived.json")), target="final", policy="latest")
    assert arrived is not None and arrived[0].basis == "EQUIPMENT DISC ACT"
    assert arrived[0].equipment_reference == "MSKU4444444" and arrived[0].eta == datetime(2026, 11, 2, 15, 20)
    assert choose_arrival(dcsa.parse_events(fixture_json("events_no_arrival.json")),
                          target="final", policy="first") is None
    with pytest.raises(ValueError, match="unknown Maersk ETA target"):
        choose_arrival(rail, target="port", policy="first")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        choose_arrival(rail, target="pod", policy="newest")  # type: ignore[arg-type]


async def test_multi_container_policy_latest(maersk_settings: Callable[..., MaerskSettings],
                                            store: TokenStore) -> None:
    settings = maersk_settings(MAERSK_MULTI_CONTAINER_POLICY="latest", MAERSK_ETA_DATE_ONLY="false")
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(return_value=events_response("events_arrived.json"))
        async with open_client(settings, store) as client:
            result = await client.get_eta("210000002")
    assert result.eta == datetime(2026, 11, 2, 15, 20)  # the last container's discharge
    assert result.is_actual is True
    assert result.details["policy"] == "latest" and result.details["equipment_reference"] == "MSKU4444444"
    assert result.source == "Maersk EQUIPMENT DISC ACT at USSAV/GCT"


# ───────────────────────── 26. probe ─────────────────────────
async def test_probe_redacts_and_reports_alternatives(maersk_settings: Callable[..., MaerskSettings],
                                                      store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        route = router.get(EVENTS_URL).mock(return_value=events_response(
            "events_ipi_rail.json", headers={"Current-Page": "1"}))
        async with open_client(maersk_settings(), store) as client:
            out = await client.probe("210000003")

    assert out["carrier"] == "Maersk" and out["booking"] == "210000003"
    assert out["token"]["ok"] is True and out["token"]["cached"] is False and out["token"]["error"] is None
    assert 7000 <= out["token"]["expires_in_s"] <= 7199 and out["token"]["scope"] == "openid"
    assert out["config"] == {"base_url": BASE_URL, "token_url": TOKEN_URL, "api_version": None,
                             "eta_target": "final", "policy": "first", "date_only": True, "page_limit": 100}
    assert out["counts"] == {"EQUIPMENT/LOAD/ACT": 1, "TRANSPORT/ARRI/EST": 2, "TRANSPORT/ARRI/PLN": 1,
                             "TRANSPORT/DEPA/ACT": 1, "TRANSPORT/DEPA/PLN": 1}
    assert isinstance(out["raw"], list) and len(out["raw"]) == 6
    assert out["requests"][0]["status"] == 200 and out["requests"][0]["events"] == 6
    assert out["requests"][0]["current_page"] == "1" and out["requests"][0]["next_page"] is None
    assert "eventType" not in route.calls[0].request.url.params  # the probe fetches every event type
    assert out["alternatives"]["final"]["eta"] == "2026-11-18T14:00:00"
    assert out["alternatives"]["final"]["unlocode"] == "USCHI" and out["alternatives"]["final"]["mode"] == "RAIL"
    assert out["alternatives"]["pod"]["eta"] == "2026-11-12T08:00:00"
    assert out["alternatives"]["pod"]["basis"] == "TRANSPORT ARRI EST"
    assert out["eta"]["eta"] == "2026-11-18T00:00:00" and out["eta"]["details"]["mode"] == "RAIL"
    assert out["error"] is None
    assert any("USLAX" in line for line in out["explain"]) and "final destination USCHI" in out["explain"][0]

    dumped = json.dumps(out)  # JSON-serializable, and no secret inside
    assert ACCESS_TOKEN not in dumped and CLIENT_SECRET not in dumped and CLIENT_ID not in dumped


async def test_probe_reports_cached_token_and_no_eta(maersk_settings: Callable[..., MaerskSettings],
                                                     store: TokenStore) -> None:
    seed_token(store, token="seeded-maersk-token-value", fingerprint=key_fingerprint(CLIENT_ID))
    async with respx.mock(assert_all_called=False) as router:
        token_route = router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(return_value=events_response("events_no_arrival.json"))
        async with open_client(maersk_settings(), store) as client:
            out = await client.probe("210000006")
    assert token_route.call_count == 0
    assert out["token"]["ok"] is True and out["token"]["cached"] is True
    assert out["eta"].startswith("NoEtaError: No ETA published by Maersk for booking 210000006")
    assert out["alternatives"] == {"final": None, "pod": None}
    assert out["error"] is None  # the integration works; this booking simply has no arrival yet
    assert "seeded-maersk-token-value" not in json.dumps(out)


async def test_probe_auth_failure_returns_early(maersk_settings: Callable[..., MaerskSettings],
                                                store: TokenStore) -> None:
    async with respx.mock(assert_all_called=False) as router:
        router.post(TOKEN_URL).mock(return_value=httpx.Response(400, json=fixture_json("token_error_400.json")))
        events_route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(), store) as client:
            out = await client.probe("210000001")
    assert events_route.call_count == 0
    assert out["token"]["ok"] is False and "not yet approved" in out["token"]["error"]
    assert out["error"].startswith("Maersk API authentication failed:")
    assert out["eta"].startswith("CarrierLookupError: Maersk API authentication failed:")
    assert out["requests"] == [] and out["raw"] == []
    json.dumps(out)


async def test_probe_lookup_failures(maersk_settings: Callable[..., MaerskSettings], store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(side_effect=[
            httpx.Response(404, json=fixture_json("error_404.json")), events_response("events_empty.json")])
        async with open_client(maersk_settings(), store) as client:
            not_found = await client.probe("210000001")
            empty = await client.probe("210000005")
            invalid = await client.probe("12345")
    assert "HTTP 404" in not_found["error"] and not_found["eta"].startswith("CarrierLookupError: ")
    assert not_found["requests"] == []  # the failing call produced no page summary
    assert "no tracking events" in empty["error"] and empty["requests"][0]["events"] == 0
    assert "not a valid Maersk booking reference" in invalid["error"]
    assert invalid["token"]["ok"] is False  # rejected before any call


# ───────────────────────── 27. concurrency ─────────────────────────
@pytest.mark.parametrize("concurrency", ["3", "10"])
async def test_concurrency_single_token_fetch(concurrency: str, maersk_settings: Callable[..., MaerskSettings],
                                              store: TokenStore) -> None:
    async def slow_token(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.01)  # keep the token call in flight while the other lookups queue up
        return token_response()

    async with respx.mock(assert_all_called=True) as router:
        token_route = router.post(TOKEN_URL).mock(side_effect=slow_token)
        events_route = router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(MAERSK_MAX_CONCURRENCY=concurrency), store) as client:
            results = await asyncio.gather(*(client.get_eta("210000001") for _ in range(10)))
    assert token_route.call_count == 1
    assert events_route.call_count == 10
    assert {r.eta for r in results} == {datetime(2026, 11, 16)}


async def test_semaphore_bounds_calls_in_flight(maersk_settings: Callable[..., MaerskSettings],
                                                store: TokenStore) -> None:
    in_flight = 0
    peak = 0

    async def slow_events(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return events_response("events_in_transit.json")

    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(side_effect=slow_events)
        async with open_client(maersk_settings(MAERSK_MAX_CONCURRENCY="2"), store) as client:
            await asyncio.gather(*(client.get_eta("210000001") for _ in range(6)))
    assert peak == 2


async def test_rate_limiter_wraps_event_calls(maersk_settings: Callable[..., MaerskSettings],
                                              store: TokenStore) -> None:
    async with respx.mock(assert_all_called=True) as router:
        router.post(TOKEN_URL).mock(return_value=token_response())
        router.get(EVENTS_URL).mock(return_value=events_response("events_in_transit.json"))
        async with open_client(maersk_settings(MAERSK_RATE_PER_MINUTE="100"), store) as client:
            assert client._limiter is not None
            await client.get_eta("210000001")
            await client.get_eta("210000001")
            assert client._limiter.acquired == 2  # event calls only; the token call is not limited
        async with open_client(maersk_settings(), store) as client:
            assert client._limiter is None  # MAERSK_RATE_PER_MINUTE=0 → off


async def test_aclose_closes_only_owned_client(maersk_settings: Callable[..., MaerskSettings],
                                               store: TokenStore) -> None:
    owned = MaerskClient(maersk_settings(), store)
    assert (owned._http.base_url.scheme, owned._http.base_url.host) == ("https", "api.maersk.test")
    assert owned._http.headers["Accept"] == "application/json"  # http.new_async_client defaults
    assert not owned._http.follow_redirects
    assert owned._http.timeout.read == 30.0 and owned._http.timeout.connect == 10.0
    async with owned:
        pass
    assert owned._http.is_closed

    async with httpx.AsyncClient() as http:
        injected = MaerskClient(maersk_settings(), store, http=http)
        await injected.aclose()
        assert not http.is_closed
