"""vship/client.py against respx mocks: VShipAuth, VShipClient (05-vship-crm §9 tests 4–9)."""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import datetime
from typing import Any

import httpx
import pytest
import respx

from eta_automations.config import Settings, VShipSettings
from eta_automations.errors import (
    AuthError,
    BookingChangeError,
    ConfigError,
    NoteError,
    ServiceError,
    VShipBookingNotFoundError,
)
from eta_automations.token_store import TokenStore
from eta_automations.vship.client import READ_ONLY_MESSAGE, VShipAuth, VShipClient, build_vship

API = "https://vship.example.test/api"
PASSWORD = "test-vship-password"
SETTINGS = VShipSettings(username="ops@example.com", password=PASSWORD, api_base=API)
NOW = 1_793_000_000.0   # injected epoch "now": before the fixture JWT's exp
JWT_EXP = 1_794_000_000.0
RUN_NOW = datetime(2026, 11, 5, 9, 30)


# ── fixtures & helpers ───────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def fx(load_fixture: Callable[[str], Any]) -> Callable[[str], Any]:
    """fx("login_ok") → tests/fixtures/vship/login_ok.json (verbatim from 05 §9)."""
    return lambda name: load_fixture(f"vship/{name}.json")


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def make_client(token_store: TokenStore) -> AsyncIterator[Callable[..., VShipClient]]:
    created: list[VShipClient] = []

    def _make(*, read_only: bool = False, settings: VShipSettings = SETTINGS,
              time_source: Callable[[], float] = lambda: NOW) -> VShipClient:
        auth = VShipAuth(settings, token_store, time_source=time_source)
        client = VShipClient(settings, auth, read_only=read_only)
        created.append(client)
        return client

    yield _make
    for client in created:
        await client.aclose()


def seed_token(store: TokenStore, token: str = "cached-token") -> None:
    store.put("vship", {"access_token": token, "expires_at": NOW + 3600, "obtained_at": NOW})


async def until(condition: Callable[[], bool]) -> None:
    for _ in range(200):
        if condition():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition not reached")


def body(call: Any) -> Any:
    return json.loads(call.request.content)


# ── test 4: login ────────────────────────────────────────────────────────────────────────────────────
async def test_login_success_stores_token_with_jwt_exp(mock: respx.MockRouter, make_client: Callable[..., VShipClient],
                                                       token_store: TokenStore, fx: Callable[[str], Any]) -> None:
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    client = make_client()
    token = await client.auth.get_token()
    assert token == fx("login_ok")["value"]["token"]
    assert token_store.get("vship") == {"access_token": token, "expires_at": JWT_EXP, "obtained_at": NOW}
    sent = login.calls.last.request
    assert body(login.calls.last) == {"username": "ops@example.com", "password": PASSWORD}
    assert "Authorization" not in sent.headers  # V1 is sent without an auth header
    assert sent.headers["Accept"] == "application/json"
    assert await client.auth.get_token() == token  # cached and fresh → no second login
    assert login.call_count == 1
    info = client.auth.token_info()
    assert info["cached"] is True and info["fresh"] is True and info["seconds_left"] == int(JWT_EXP - NOW)
    assert token not in json.dumps(info)


@pytest.mark.parametrize(("ttl_minutes", "expected_ttl"), [(55, 3300), (10, 600)])
async def test_login_non_jwt_token_uses_ttl(mock: respx.MockRouter, make_client: Callable[..., VShipClient],
                                            token_store: TokenStore, ttl_minutes: int, expected_ttl: int) -> None:
    mock.post("/Auth/login").mock(return_value=httpx.Response(200, json={"value": {"token": "opaque-token-123"}}))
    settings = VShipSettings(username="ops@example.com", password=PASSWORD, api_base=API,
                             token_ttl_minutes=ttl_minutes)
    client = make_client(settings=settings)
    assert await client.auth.get_token() == "opaque-token-123"
    assert token_store.get("vship") == {"access_token": "opaque-token-123", "expires_at": NOW + expected_ttl,
                                        "obtained_at": NOW}


async def test_login_missing_token_raises_auth_error(mock: respx.MockRouter, make_client: Callable[..., VShipClient],
                                                     token_store: TokenStore, fx: Callable[[str], Any]) -> None:
    mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_no_token")))
    client = make_client()
    with pytest.raises(AuthError) as info:
        await client.auth.get_token()
    assert str(info.value).startswith("VShip login succeeded but no token in response: ")
    assert "Invalid credentials" in str(info.value)
    assert token_store.get("vship") is None


async def test_login_non_json_200_raises_auth_error(mock: respx.MockRouter,
                                                    make_client: Callable[..., VShipClient]) -> None:
    mock.post("/Auth/login").mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(AuthError, match=r"^VShip login succeeded but no token in response: <html>maintenance"):
        await make_client().auth.get_token()


async def test_login_rejected_raises_auth_error_with_status(mock: respx.MockRouter,
                                                            make_client: Callable[..., VShipClient]) -> None:
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(401, json={"message": "Invalid credentials"}))
    with pytest.raises(AuthError) as info:
        await make_client().auth.get_token()
    assert str(info.value).startswith("VShip login failed (HTTP 401)")
    assert "Invalid credentials" in str(info.value)  # redacted body excerpt included
    assert login.call_count == 1  # 401 is never retried by http.request


async def test_login_server_errors_are_retried_then_reported(mock: respx.MockRouter, http_sleeps: list[float],
                                                             make_client: Callable[..., VShipClient]) -> None:
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(500))
    with pytest.raises(AuthError, match=r"^VShip login failed \(HTTP 500\)"):
        await make_client().auth.get_token()
    assert login.call_count == 4  # V1 is a pure read (idempotent=True): 03 §6 retries apply


async def test_login_network_error_raises_auth_error(mock: respx.MockRouter, http_sleeps: list[float],
                                                     make_client: Callable[..., VShipClient]) -> None:
    mock.post("/Auth/login").mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(AuthError, match=r"^VShip login failed: network error"):
        await make_client().auth.get_token()


async def test_concurrent_get_token_triggers_one_login(mock: respx.MockRouter, fx: Callable[[str], Any],
                                                       make_client: Callable[..., VShipClient]) -> None:
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    client = make_client()
    tokens = await asyncio.gather(*(client.auth.get_token() for _ in range(20)))
    assert len(set(tokens)) == 1
    assert login.call_count == 1


async def test_token_within_skew_is_refreshed_and_invalidate_drops_it(
        mock: respx.MockRouter, fx: Callable[[str], Any], token_store: TokenStore,
        make_client: Callable[..., VShipClient]) -> None:
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    token_store.put("vship", {"access_token": "almost-expired", "expires_at": NOW + 60, "obtained_at": NOW - 3000})
    client = make_client()
    assert await client.auth.get_token() == fx("login_ok")["value"]["token"]  # 60 s left < 120 s skew
    assert login.call_count == 1
    await client.auth.invalidate()
    assert token_store.get("vship") is None
    assert client.auth.token_info() == {"cached": False}


# ── test 5: 401/403 → one re-login → retry ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_token_relogs_once_and_retry_succeeds(
        status: int, mock: respx.MockRouter, fx: Callable[[str], Any], token_store: TokenStore,
        make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store, "old-token")
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    comments = mock.get("/Bookings/72892/comments").mock(
        side_effect=[httpx.Response(status), httpx.Response(200, json=fx("comments_72892"))])
    client = make_client()
    result = await client.get_internal_comments("72892")
    assert [c["commentId"] for c in result] == [1, 2, 3]
    assert login.call_count == 1
    assert comments.call_count == 2
    assert [c.request.headers["Authorization"] for c in comments.calls] == [
        "Bearer old-token", f"Bearer {fx('login_ok')['value']['token']}"]


@pytest.mark.parametrize("status", [401, 403])
async def test_second_rejection_raises_auth_error(
        status: int, mock: respx.MockRouter, fx: Callable[[str], Any], token_store: TokenStore,
        make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store, "old-token")
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    comments = mock.get("/Bookings/72892/comments").mock(return_value=httpx.Response(status))
    with pytest.raises(AuthError, match=rf"^vship: credentials/token rejected \(HTTP {status}\)$"):
        await make_client().get_internal_comments("72892")
    assert login.call_count == 1
    assert comments.call_count == 2


# ── test 6: find_booking_id single-flight cache ──────────────────────────────────────────────────────
async def test_concurrent_find_booking_id_makes_one_search(
        mock: respx.MockRouter, fx: Callable[[str], Any], token_store: TokenStore,
        make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    gate = asyncio.Event()
    entered: list[httpx.Request] = []

    async def slow_search(request: httpx.Request) -> httpx.Response:
        entered.append(request)
        await gate.wait()
        return httpx.Response(200, json=fx("search_multi"))

    search = mock.get("/Bookings/searchBooking").mock(side_effect=slow_search)
    client = make_client()
    first = asyncio.create_task(client.find_booking_id("EBKG17208602"))
    second = asyncio.create_task(client.find_booking_id(" ebkg17208602 "))
    await until(lambda: len(entered) == 1)
    for _ in range(10):
        await asyncio.sleep(0)
    assert len(entered) == 1  # still a single request in flight
    assert not first.done() and not second.done()  # both callers wait on the same in-flight request
    gate.set()
    assert await asyncio.gather(first, second) == ["72892", "72892"]
    assert search.call_count == 1
    params = search.calls.last.request.url.params
    assert (params["searchText"], params["page"], params["pageSize"]) == ("EBKG17208602", "1", "50")
    assert await client.find_booking_id("EBKG17208602") == "72892"  # later rows of the same run: cached
    assert search.call_count == 1


async def test_booking_not_found_lists_candidates_and_is_cached(
        mock: respx.MockRouter, token_store: TokenStore, make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    search = mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json={"value": {"data": [
        {"bookingId": 1, "bookingNo": "EBKG17208999"}, {"bookingId": 2, "bookingNo": "NAM1234567"}]}}))
    client = make_client()
    for _ in range(2):
        with pytest.raises(VShipBookingNotFoundError) as info:
            await client.find_booking_id("EBKG17208602")
        assert str(info.value) == (
            "vship: No VShip booking matches 'EBKG17208602' (candidates: 'EBKG17208999', 'NAM1234567')")
    assert search.call_count == 1  # D18: one lookup per base booking per run, failures included


async def test_booking_not_found_on_empty_search(mock: respx.MockRouter, fx: Callable[[str], Any],
                                                 token_store: TokenStore,
                                                 make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json=fx("search_empty")))
    with pytest.raises(VShipBookingNotFoundError,
                       match=r"^vship: No VShip booking matches 'EBKG17208602' \(candidates: none\)$"):
        await make_client().find_booking_id("EBKG17208602")


async def test_empty_booking_is_never_searched(mock: respx.MockRouter, token_store: TokenStore,
                                               make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    search = mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json={"value": {"data": []}}))
    with pytest.raises(VShipBookingNotFoundError):
        await make_client().find_booking_id("  ")
    assert search.call_count == 0


async def test_customer_name_and_booking_detail(mock: respx.MockRouter, fx: Callable[[str], Any],
                                                token_store: TokenStore,
                                                make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json=fx("search_multi")))
    detail = mock.get("/Bookings/72892").mock(return_value=httpx.Response(200, json=fx("booking_72892")))
    client = make_client()
    assert await client.get_customer_name("EBKG17208602") == "ACME IMPORTS"
    assert (await client.get_booking("72892"))["expected_Arrival_Date"] == "2026-11-10T00:00:00"
    assert detail.call_count == 2
    detail.mock(return_value=httpx.Response(200, json={"value": {"bookingId": 72892, "customerName": None}}))
    assert await client.get_customer_name("EBKG17208602") == ""


async def test_comments_are_cached_copied_and_invalidated_by_a_post(
        mock: respx.MockRouter, fx: Callable[[str], Any], token_store: TokenStore,
        make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    comments = mock.get("/Bookings/72892/comments").mock(
        return_value=httpx.Response(200, json=fx("comments_72892")))
    mock.post("/Bookings/72892/comments").mock(return_value=httpx.Response(201))
    client = make_client()
    first = await client.get_internal_comments("72892")
    first[0]["comment"] = "changed by a caller"
    first.clear()
    second = await client.get_internal_comments("72892")
    assert comments.call_count == 1  # notif + TR/SWB checks share one request
    assert second[0]["comment"] == "ARRIVING ON 11/10."
    await client.post_comment("72892", "ARRIVED ON 11/03. NOTIF #2")
    await client.get_internal_comments("72892")
    assert comments.call_count == 2


@pytest.mark.parametrize("value", [{"internalComments": None}, {"externalComments": []}])
async def test_missing_or_null_internal_comments_is_empty(
        value: dict[str, Any], mock: respx.MockRouter, token_store: TokenStore,
        make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    mock.get("/Bookings/1/comments").mock(return_value=httpx.Response(200, json={"value": value}))
    assert await make_client().get_internal_comments("1") == []


@pytest.mark.parametrize(
    ("path", "payload", "call"),
    [
        ("/Bookings/searchBooking", {"value": None, "isSuccess": False, "message": "hidden detail"},
         lambda c: c.find_booking_id("EBKG17208602")),
        ("/Bookings/searchBooking", {"value": {"data": {"hidden": "detail"}}},
         lambda c: c.find_booking_id("EBKG17208602")),
        ("/Bookings/1", {"isSuccess": True}, lambda c: c.get_booking("1")),
        ("/Bookings/1/comments", {"value": {"internalComments": "hidden detail"}},
         lambda c: c.get_internal_comments("1")),
        ("/Bookings/1/comments", ["hidden detail"], lambda c: c.get_internal_comments("1")),
    ],
)
async def test_unexpected_payload_shape_names_keys_only(
        path: str, payload: Any, call: Callable[[VShipClient], Any], mock: respx.MockRouter,
        token_store: TokenStore, make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    mock.get(path).mock(return_value=httpx.Response(200, json=payload))
    with pytest.raises(ServiceError) as info:
        await call(make_client())
    text = str(info.value)
    assert text.startswith("vship: unexpected JSON structure: top-level keys=")
    assert "detail" not in text  # key names only, never values


async def test_matched_booking_without_id_is_a_service_error(mock: respx.MockRouter, token_store: TokenStore,
                                                             make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    mock.get("/Bookings/searchBooking").mock(
        return_value=httpx.Response(200, json={"value": {"data": [{"bookingNo": "EBKG17208602"}]}}))
    with pytest.raises(ServiceError, match="matched booking has no bookingId"):
        await make_client().find_booking_id("EBKG17208602")


async def test_http_calls_respect_the_concurrency_limit_and_share_one_login(
        mock: respx.MockRouter, fx: Callable[[str], Any], make_client: Callable[..., VShipClient]) -> None:
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    in_flight = 0
    peak = 0

    async def slow_comments(request: httpx.Request) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return httpx.Response(200, json={"value": {"internalComments": []}})

    routes = [mock.get(f"/Bookings/{i}/comments").mock(side_effect=slow_comments) for i in range(6)]
    settings = VShipSettings(username="ops@example.com", password=PASSWORD, api_base=API, max_concurrency=2)
    client = make_client(settings=settings)
    await asyncio.gather(*(client.get_internal_comments(str(i)) for i in range(6)))
    assert [r.call_count for r in routes] == [1] * 6
    assert peak == 2
    assert login.call_count == 1  # the token provider never takes the semaphore (no deadlock)


# ── test 7: post_comment ─────────────────────────────────────────────────────────────────────────────
async def test_post_comment_201(mock: respx.MockRouter, fx: Callable[[str], Any], token_store: TokenStore,
                                make_client: Callable[..., VShipClient], caplog: pytest.LogCaptureFixture) -> None:
    seed_token(token_store, "tok")
    mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json=fx("search_multi")))
    post = mock.post("/Bookings/72892/comments").mock(return_value=httpx.Response(201))
    client = make_client()
    booking_id = await client.find_booking_id("EBKG17208602")
    caplog.set_level(logging.INFO, logger="eta")
    await client.post_comment(booking_id, "ARRIVED ON 11/03. NOTIF #1")
    assert post.call_count == 1
    assert body(post.calls.last) == {"comment": "ARRIVED ON 11/03. NOTIF #1", "commentType": 1}
    headers = post.calls.last.request.headers
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Accept"] == "application/json"
    assert headers["Content-Type"] == "application/json"
    assert headers["Referer"] == "https://vshipcrm.com/"
    assert "[VShip] Posting note for booking EBKG17208602: 'ARRIVED ON 11/03. NOTIF #1'" in caplog.messages


async def test_post_comment_uses_the_configured_comment_type(mock: respx.MockRouter, token_store: TokenStore,
                                                             make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    post = mock.post("/Bookings/5/comments").mock(return_value=httpx.Response(201))
    settings = VShipSettings(username="ops@example.com", password=PASSWORD, api_base=API, comment_type=3)
    await make_client(settings=settings).post_comment("5", "ARRIVING ON 11/18.")
    assert body(post.calls.last) == {"comment": "ARRIVING ON 11/18.", "commentType": 3}


async def test_post_comment_400_is_note_error_with_body_excerpt(mock: respx.MockRouter, token_store: TokenStore,
                                                                make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    post = mock.post("/Bookings/72892/comments").mock(
        return_value=httpx.Response(400, json={"errors": {"comment": ["Comment is too long"]}}))
    with pytest.raises(NoteError) as info:
        await make_client().post_comment("72892", "x" * 5000)
    assert "Comment is too long" in str(info.value)
    assert info.value.status == 400
    assert isinstance(info.value.__cause__, ServiceError)
    assert post.call_count == 1


async def test_post_comment_read_timeout_is_note_error_without_retry(
        mock: respx.MockRouter, token_store: TokenStore, http_sleeps: list[float],
        make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    post = mock.post("/Bookings/72892/comments").mock(side_effect=httpx.ReadTimeout("timed out"))
    with pytest.raises(NoteError, match=r"^vship: network error: ReadTimeout"):
        await make_client().post_comment("72892", "ARRIVED ON 11/03. NOTIF #1")
    assert post.call_count == 1  # a duplicate note is worse than a missing one
    assert http_sleeps == []


async def test_post_comment_rejected_twice_is_note_error(mock: respx.MockRouter, fx: Callable[[str], Any],
                                                         token_store: TokenStore,
                                                         make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    post = mock.post("/Bookings/72892/comments").mock(return_value=httpx.Response(401))
    with pytest.raises(NoteError, match=r"^vship: credentials/token rejected \(HTTP 401\)$") as info:
        await make_client().post_comment("72892", "x")
    assert isinstance(info.value.__cause__, AuthError)
    assert post.call_count == 2


# ── test 8: patch_expected_arrival ───────────────────────────────────────────────────────────────────
async def test_patch_expected_arrival_204(mock: respx.MockRouter, fx: Callable[[str], Any], token_store: TokenStore,
                                          make_client: Callable[..., VShipClient],
                                          caplog: pytest.LogCaptureFixture) -> None:
    seed_token(token_store, "tok")
    mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json=fx("search_multi")))
    patch = mock.patch("/Bookings/72892").mock(return_value=httpx.Response(204))
    client = make_client()
    booking_id = await client.find_booking_id("EBKG17208602")
    caplog.set_level(logging.INFO, logger="eta")
    await client.patch_expected_arrival(booking_id, datetime(2026, 11, 16, 14, 0))
    assert body(patch.calls.last) == {"booking_Id": "72892", "expected_Arrival_Date": "2026-11-16T14:00:00"}
    headers = patch.calls.last.request.headers
    assert headers["Authorization"] == "Bearer tok"
    assert headers["Content-Type"] == "application/json"
    assert headers["Referer"] == "https://vshipcrm.com/"
    assert "[VShip] VShip ETA 2026-11-16 for EBKG17208602 done." in caplog.messages


async def test_patch_converts_an_int_booking_id_to_a_string(mock: respx.MockRouter, token_store: TokenStore,
                                                            make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    patch = mock.patch("/Bookings/72892").mock(return_value=httpx.Response(204))
    await make_client().patch_expected_arrival(72892, datetime(2026, 11, 3))  # type: ignore[arg-type]
    assert body(patch.calls.last) == {"booking_Id": "72892", "expected_Arrival_Date": "2026-11-03T00:00:00"}


@pytest.mark.parametrize("status", [200, 500])
async def test_patch_other_status_is_booking_change_error(status: int, mock: respx.MockRouter,
                                                          token_store: TokenStore, http_sleeps: list[float],
                                                          make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    patch = mock.patch("/Bookings/72892").mock(return_value=httpx.Response(status, text="not saved"))
    with pytest.raises(BookingChangeError) as info:
        await make_client().patch_expected_arrival("72892", datetime(2026, 11, 16))
    assert info.value.status == status
    assert "not saved" in str(info.value)
    assert patch.call_count == 1  # PATCH is not idempotent: a 500 is reported, never retried


async def test_read_only_client_never_writes(mock: respx.MockRouter, token_store: TokenStore,
                                             make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    post = mock.post("/Bookings/72892/comments").mock(return_value=httpx.Response(201))
    patch = mock.patch("/Bookings/72892").mock(return_value=httpx.Response(204))
    client = make_client(read_only=True)
    with pytest.raises(RuntimeError, match=r"^VShipClient is read-only \(dry run / probe\)$"):
        await client.post_comment("72892", "x")
    with pytest.raises(RuntimeError) as info:
        await client.patch_expected_arrival("72892", datetime(2026, 11, 16))
    assert str(info.value) == READ_ONLY_MESSAGE
    assert post.call_count == 0 and patch.call_count == 0


# ── test 9: redaction ────────────────────────────────────────────────────────────────────────────────
async def test_password_never_appears_in_logs_or_exceptions(
        mock: respx.MockRouter, make_client: Callable[..., VShipClient], caplog: pytest.LogCaptureFixture,
        http_sleeps: list[float]) -> None:
    caplog.set_level(logging.DEBUG, logger="eta")
    assert PASSWORD not in repr(SETTINGS)
    echo = {"message": f"Login failed for ops@example.com with password {PASSWORD}", "password": PASSWORD}
    login = mock.post("/Auth/login").mock(return_value=httpx.Response(400, json=echo))
    client = make_client()
    with pytest.raises(AuthError) as info:
        await client.find_booking_id("EBKG17208602")
    err = info.value
    rendered = "".join(traceback.format_exception(err))
    assert str(err).startswith("VShip login failed (HTTP 400)")
    for text in (str(err), repr(err), rendered, str(err.__cause__)):
        assert PASSWORD not in text
    assert err.__context__ is None

    # a 200 without a token that echoes the password in free text
    login.mock(return_value=httpx.Response(200, text=f"no token; you sent {PASSWORD}"))
    with pytest.raises(AuthError) as info2:
        await client.auth.get_token()
    assert PASSWORD not in str(info2.value)
    assert PASSWORD not in "".join(traceback.format_exception(info2.value))
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize(("status", "filler"), [(400, 495), (200, 196)])
async def test_password_cut_by_the_excerpt_limit_is_masked(
        status: int, filler: int, mock: respx.MockRouter, make_client: Callable[..., VShipClient]) -> None:
    """Excerpts are cut at 500 (errors) / 200 (no-token) characters; a password straddling the cut leaves
    its first characters at the end of the excerpt — those are masked too."""
    mock.post("/Auth/login").mock(return_value=httpx.Response(status, text="x" * filler + PASSWORD))
    with pytest.raises(AuthError) as info:
        await make_client().auth.get_token()
    text = "".join(traceback.format_exception(info.value))
    assert "x" + PASSWORD[:3] not in text  # no start of the password right after the filler
    assert str(info.value).endswith("x" * 10 + "***")


async def test_successful_flow_logs_neither_password_nor_token(
        mock: respx.MockRouter, fx: Callable[[str], Any], make_client: Callable[..., VShipClient],
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="eta")
    token = fx("login_ok")["value"]["token"]
    mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json=fx("search_multi")))
    mock.get("/Bookings/72892/comments").mock(return_value=httpx.Response(200, json=fx("comments_72892")))
    mock.post("/Bookings/72892/comments").mock(return_value=httpx.Response(201))
    mock.patch("/Bookings/72892").mock(return_value=httpx.Response(204))
    client = make_client()
    booking_id = await client.find_booking_id("EBKG17208602")
    await client.get_internal_comments(booking_id)
    await client.post_comment(booking_id, "ARRIVED ON 11/03. NOTIF #1")
    await client.patch_expected_arrival(booking_id, datetime(2026, 11, 3))
    assert caplog.records  # DEBUG lines were captured
    assert PASSWORD not in caplog.text
    assert token not in caplog.text


# ── factory, probe, close ────────────────────────────────────────────────────────────────────────────
async def test_build_vship_from_settings(make_settings: Callable[..., Settings], token_store: TokenStore) -> None:
    client = build_vship(make_settings(), token_store, read_only=True)
    try:
        assert isinstance(client, VShipClient) and isinstance(client.auth, VShipAuth)
        assert client.read_only is True
        assert client.settings.username == "ops@example.com"
    finally:
        await client.aclose()


def test_build_vship_unconfigured_raises_config_error(make_settings: Callable[..., Settings],
                                                      token_store: TokenStore) -> None:
    with pytest.raises(ConfigError, match="VSHIP_PASSWORD"):
        build_vship(make_settings(VSHIP_PASSWORD=None), token_store)


async def test_probe_is_read_only_and_complete(mock: respx.MockRouter, fx: Callable[[str], Any],
                                               make_client: Callable[..., VShipClient]) -> None:
    token = fx("login_ok")["value"]["token"]
    mock.post("/Auth/login").mock(return_value=httpx.Response(200, json=fx("login_ok")))
    mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json=fx("search_multi")))
    mock.get("/Bookings/72892").mock(return_value=httpx.Response(200, json=fx("booking_72892")))
    mock.get("/Bookings/72892/comments").mock(return_value=httpx.Response(200, json=fx("comments_72892")))
    result = await make_client(read_only=True).probe("ebkg17208602", run_now=RUN_NOW)
    assert result["error"] is None
    assert result["booking"] == "EBKG17208602"
    assert result["booking_id"] == "72892"
    assert result["customer_name"] == "ACME IMPORTS"
    assert [(c["bookingNo"], c["match_rank"][0]) for c in result["candidates"]] == [
        ("EBKG17208602_Copy_76099", 1), ("EBKG17208602", 0), ("XEBKG17208602", 2)]
    assert result["notif_status"] == {"count": 1, "have_to_do": False}
    assert result["tr_swb"] is True
    assert len(result["comments"]) == 3
    assert result["token"]["cached"] is True
    dumped = json.dumps(result)  # JSON-serializable for <ETA_STATE_DIR>/probe/vship_<booking>.json
    assert token not in dumped and PASSWORD not in dumped


async def test_probe_reports_not_found(mock: respx.MockRouter, fx: Callable[[str], Any], token_store: TokenStore,
                                       make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    mock.get("/Bookings/searchBooking").mock(return_value=httpx.Response(200, json=fx("search_empty")))
    result = await make_client(read_only=True).probe("EBKG17208602", run_now=RUN_NOW)
    assert result["error"] == "No VShip booking matches 'EBKG17208602' (candidates: none)"
    assert "booking_id" not in result


async def test_aclose_cancels_lookups_in_flight(mock: respx.MockRouter, token_store: TokenStore,
                                                make_client: Callable[..., VShipClient]) -> None:
    seed_token(token_store)
    never = asyncio.Event()
    entered: list[httpx.Request] = []

    async def hang(request: httpx.Request) -> httpx.Response:
        entered.append(request)
        await never.wait()
        return httpx.Response(200, json={"value": {"data": []}})

    mock.get("/Bookings/searchBooking").mock(side_effect=hang)
    client = make_client()
    pending = asyncio.create_task(client.find_booking_id("EBKG17208602"))
    await until(lambda: len(entered) == 1)
    await client.aclose()
    with pytest.raises(asyncio.CancelledError):
        await pending
