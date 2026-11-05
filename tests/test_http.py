"""http.py: retry/backoff/idempotency/Retry-After/redaction/limiter (03-foundations §6, §10; 12-testing §3)."""

from __future__ import annotations

import asyncio
import email.utils
import logging
import time

import httpx
import pytest
import respx

from eta_automations import __version__
from eta_automations.errors import ServiceError
from eta_automations.http import (
    DEFAULT_TIMEOUT,
    REQUEST_ID_HEADERS,
    AsyncLimiter,
    backoff_delay,
    json_body,
    new_async_client,
    parse_retry_after,
    request,
    request_id_from,
)

BASE = "https://api.example.test"
URL = f"{BASE}/v1/items"


class FakeTime:
    """Injectable clock + sleep for AsyncLimiter: sleeping advances the clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# ── retries on status ──────────────────────────────────────────────────────────────────────────────
async def test_get_503_twice_then_200_returns_200_after_3_calls(http_sleeps: list[float]) -> None:
    with respx.mock(assert_all_called=True) as router:
        route = router.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(503),
                                                  httpx.Response(200, json={"ok": True})])
        async with httpx.AsyncClient() as client:
            resp = await request(client, "GET", URL, service="svc")
    assert resp.status_code == 200
    assert route.call_count == 3
    # 03 §6: min(30, 0.5 * 2**(n-2)) + uniform(0, 0.25) before attempts 2 and 3
    assert len(http_sleeps) == 2
    assert 0.5 <= http_sleeps[0] <= 0.75
    assert 1.0 <= http_sleeps[1] <= 1.25


async def test_retry_after_seconds_is_honoured_with_patched_asyncio_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    delays: list[float] = []

    async def fake_sleep(seconds: float, result: object = None) -> object:
        delays.append(seconds)
        return result

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)  # 12-testing §3: "patch asyncio.sleep"
    with respx.mock() as router:
        route = router.get(URL).mock(side_effect=[httpx.Response(503, headers={"Retry-After": "2"}),
                                                  httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            resp = await request(client, "GET", URL, service="svc")
    assert resp.status_code == 200
    assert route.call_count == 2
    assert delays == [2.0]


async def test_retry_after_http_date_is_honoured(http_sleeps: list[float]) -> None:
    when = email.utils.formatdate(time.time() + 10, usegmt=True)
    with respx.mock() as router:
        router.get(URL).mock(side_effect=[httpx.Response(429, headers={"Retry-After": when}), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            await request(client, "GET", URL, service="svc")
    assert len(http_sleeps) == 1
    assert 7.0 <= http_sleeps[0] <= 10.5


async def test_retry_after_is_capped_at_30_seconds(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        router.get(URL).mock(side_effect=[httpx.Response(503, headers={"Retry-After": "120"}), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            await request(client, "GET", URL, service="svc")
    assert http_sleeps == [30.0]


async def test_post_500_is_not_retried(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=httpx.Response(500, text="server error"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request(client, "POST", URL, service="svc", json={"a": 1})
    assert route.call_count == 1
    assert info.value.status == 500
    assert http_sleeps == []


@pytest.mark.parametrize("status", [429, 503])
async def test_post_429_and_503_are_retried(status: int, http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.post(URL).mock(side_effect=[httpx.Response(status), httpx.Response(201)])
        async with httpx.AsyncClient() as client:
            resp = await request(client, "POST", URL, service="svc", expected=(201,))
    assert resp.status_code == 201
    assert route.call_count == 2


@pytest.mark.parametrize("status", [500, 502, 504])
async def test_idempotent_statuses_retried_for_get_only(status: int, http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        get_route = router.get(URL).mock(side_effect=[httpx.Response(status), httpx.Response(200)])
        patch_route = router.patch(URL).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as client:
            assert (await request(client, "GET", URL, service="svc")).status_code == 200
            with pytest.raises(ServiceError):
                await request(client, "PATCH", URL, service="svc", expected=(204,))
    assert get_route.call_count == 2
    assert patch_route.call_count == 1


async def test_post_marked_idempotent_is_retried_on_500(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.post(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            resp = await request(client, "POST", URL, service="svc", idempotent=True)
    assert resp.status_code == 200
    assert route.call_count == 2


async def test_get_marked_not_idempotent_is_not_retried_on_500(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError):
                await request(client, "GET", URL, service="svc", idempotent=False)
    assert route.call_count == 1


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_401_and_403_are_never_retried(method: str, status: int, http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.route(method=method, url=URL).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request(client, method, URL, service="svc", idempotent=True)
    assert route.call_count == 1
    assert info.value.status == status


@pytest.mark.parametrize("status", [404, 429])
async def test_status_listed_in_expected_is_returned_without_retry(status: int, http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(status))
        async with httpx.AsyncClient() as client:
            resp = await request(client, "GET", URL, service="svc", expected=(200, 404, 429))
    assert resp.status_code == status
    assert route.call_count == 1
    assert http_sleeps == []


async def test_max_attempts_one_disables_retries(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(503))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request(client, "GET", URL, service="svc", max_attempts=1)
    assert route.call_count == 1
    assert info.value.status == 503


async def test_retryable_status_exhausting_attempts_raises_last_status(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(503, text="busy"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request(client, "GET", URL, service="svc", max_attempts=3)
    assert route.call_count == 3
    assert info.value.status == 503
    assert len(http_sleeps) == 2


# ── transport errors ───────────────────────────────────────────────────────────────────────────────
async def test_post_read_timeout_is_not_retried(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.post(URL).mock(side_effect=httpx.ReadTimeout("timed out"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request(client, "POST", URL, service="vship", expected=(201,))
    assert route.call_count == 1
    assert info.value.status is None
    assert str(info.value).startswith("vship: network error: ReadTimeout(")
    assert isinstance(info.value.__cause__, httpx.ReadTimeout)


async def test_get_read_timeout_is_retried(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            resp = await request(client, "GET", URL, service="svc")
    assert resp.status_code == 200
    assert route.call_count == 2


@pytest.mark.parametrize("exc_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_connect_phase_errors_are_retried_even_for_post(exc_type: type[Exception],
                                                              http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.post(URL).mock(side_effect=[exc_type("no connection"), httpx.Response(201)])
        async with httpx.AsyncClient() as client:
            resp = await request(client, "POST", URL, service="svc", expected=(201,))
    assert resp.status_code == 201
    assert route.call_count == 2


@pytest.mark.parametrize("exc_type", [httpx.WriteTimeout, httpx.RemoteProtocolError])
async def test_ambiguous_transport_errors_retried_only_when_idempotent(exc_type: type[Exception],
                                                                       http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        post_route = router.post(URL).mock(side_effect=exc_type("boom"))
        put_route = router.put(URL).mock(side_effect=[exc_type("boom"), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError):
                await request(client, "POST", URL, service="svc")
            assert (await request(client, "PUT", URL, service="svc")).status_code == 200
    assert post_route.call_count == 1
    assert put_route.call_count == 2


async def test_connect_error_exhausting_attempts_raises_network_service_error(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(side_effect=httpx.ConnectError("[Errno 111] Connection refused"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request(client, "GET", URL, service="maersk")
    assert route.call_count == 4  # default max_attempts
    assert len(http_sleeps) == 3
    err = info.value
    assert err.status is None
    assert err.service == "maersk"
    assert "network error: ConnectError(" in str(err)
    assert isinstance(err.__cause__, httpx.ConnectError)


async def test_non_retryable_transport_error_is_wrapped(http_sleeps: list[float]) -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(side_effect=httpx.ReadError("reset"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError, match="network error: ReadError"):
                await request(client, "GET", URL, service="svc")
    assert route.call_count == 1


# ── ServiceError contents ──────────────────────────────────────────────────────────────────────────
async def test_service_error_has_redacted_excerpt_request_id_and_redacted_path() -> None:
    body = '{"Fault": {"Error": [{"Message": "bad"}]}, "refresh_token": "RT-SECRET-1", "pad": "' + "x" * 2000 + '"}'
    headers = {"intuit_tid": "tid-123", "x-request-id": "other"}
    with respx.mock() as router:
        router.post(f"{URL}").mock(return_value=httpx.Response(400, text=body, headers=headers))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request(client, "POST", URL, service="qbo",
                              params={"requestid": "r1", "access_token": "AT-SECRET-2"})
    err = info.value
    text = str(err)
    assert err.status == 400
    assert err.request_id == "tid-123"
    assert "[request_id=tid-123]" in text
    assert text.startswith("qbo: unexpected HTTP 400 for POST /v1/items?")
    assert "(HTTP 400)" in text
    assert "access_token=***" in text and "AT-SECRET-2" not in text
    assert "requestid=r1" in text
    assert len(err.body_excerpt) <= 500
    assert '"refresh_token": "***"' in err.body_excerpt
    assert "RT-SECRET-1" not in text


@pytest.mark.parametrize("present,expected", [
    ({"request-id": "a", "x-azure-ref": "b"}, "b"),
    ({"x-kong-request-id": "k", "request-id": "a"}, "k"),
    ({"X-Correlation-Id": "c", "x-correlationid": "d"}, "c"),
    ({"Intuit_Tid": "t", "x-request-id": "r"}, "t"),
    ({"content-type": "application/json"}, None),
])
def test_request_id_header_precedence(present: dict[str, str], expected: str | None) -> None:
    assert request_id_from(present) == expected


def test_request_id_header_list_matches_spec() -> None:
    assert REQUEST_ID_HEADERS == ("intuit_tid", "x-request-id", "x-correlation-id", "x-correlationid",
                                  "x-kong-request-id", "x-azure-ref", "request-id")


async def test_every_attempt_logged_at_debug_with_redacted_url(caplog: pytest.LogCaptureFixture,
                                                               http_sleeps: list[float]) -> None:
    caplog.set_level(logging.DEBUG, logger="eta.http")
    with respx.mock() as router:
        router.get(URL).mock(side_effect=[httpx.ConnectError("down"), httpx.Response(503), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            await request(client, "GET", URL, service="svc", params={"api_key": "K-SECRET", "q": "1"})
    attempt_lines = [r.getMessage() for r in caplog.records if r.name == "eta.http" and "(attempt" in r.getMessage()
                     and "->" in r.getMessage()]
    assert len(attempt_lines) == 3
    assert "ConnectError" in attempt_lines[0] and "attempt 1/4" in attempt_lines[0]
    assert "-> 503" in attempt_lines[1] and "attempt 2/4" in attempt_lines[1]
    assert "-> 200" in attempt_lines[2] and " ms " in attempt_lines[2]
    assert all(line.startswith("GET https://api.example.test/v1/items") for line in attempt_lines)
    assert "K-SECRET" not in caplog.text
    assert "api_key=***" in caplog.text


async def test_limiter_wraps_each_attempt(http_sleeps: list[float]) -> None:
    fake = FakeTime()
    limiter = AsyncLimiter(10, 60.0, clock=fake.clock, sleep=fake.sleep)
    with respx.mock() as router:
        router.get(URL).mock(side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            await request(client, "GET", URL, service="svc", limiter=limiter)
    assert limiter.acquired == 3


async def test_relative_url_uses_client_base_url() -> None:
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(404, text="nope"))
        async with new_async_client(base_url=BASE) as client:
            resp = await request(client, "GET", "/v1/items", service="svc", expected=(200, 404))
            with pytest.raises(ServiceError, match=r"unexpected HTTP 404 for GET /v1/items"):
                await request(client, "GET", "/v1/items", service="svc")
    assert resp.status_code == 404
    assert route.call_count == 2


# ── json_body ──────────────────────────────────────────────────────────────────────────────────────
def test_json_body_returns_parsed_payload() -> None:
    resp = httpx.Response(200, json={"value": {"token": "x"}})
    assert json_body(resp, service="vship") == {"value": {"token": "x"}}


def test_json_body_invalid_raises_service_error() -> None:
    req = httpx.Request("GET", URL)
    resp = httpx.Response(200, text="<html>password=hunter2</html>", headers={"x-request-id": "rid"}, request=req)
    with pytest.raises(ServiceError) as info:
        json_body(resp, service="msc")
    err = info.value
    assert err.message == "response was not valid JSON"
    assert err.status == 200
    assert err.request_id == "rid"
    assert "hunter2" not in str(err)
    assert isinstance(err.__cause__, ValueError)


def test_json_body_empty_body_raises_service_error() -> None:
    with pytest.raises(ServiceError, match="response was not valid JSON"):
        json_body(httpx.Response(204), service="svc")


# ── client factory ─────────────────────────────────────────────────────────────────────────────────
async def test_new_async_client_defaults() -> None:
    async with new_async_client() as client:
        assert client.headers["User-Agent"] == f"ETAutomationsAPI/{__version__}"
        assert client.headers["Accept"] == "application/json"
        assert client.follow_redirects is False
        assert client.trust_env is True
        assert client.timeout == DEFAULT_TIMEOUT
    assert DEFAULT_TIMEOUT == httpx.Timeout(30.0, connect=10.0)


async def test_new_async_client_merges_headers_case_insensitively_and_float_timeout() -> None:
    async with new_async_client(base_url=BASE, headers={"accept": "application/pdf", "X-Extra": "1"},
                                timeout=45.0) as client:
        assert client.headers.get_list("accept") == ["application/pdf"]
        assert client.headers["X-Extra"] == "1"
        assert client.headers["User-Agent"].startswith("ETAutomationsAPI/")
        assert str(client.base_url).rstrip("/") == BASE
        assert client.timeout == httpx.Timeout(45.0, connect=10.0)
    async with new_async_client(timeout=5) as client:
        assert client.timeout == httpx.Timeout(5.0, connect=5.0)


async def test_redirects_are_not_followed() -> None:
    with respx.mock() as router:
        router.get(URL).mock(return_value=httpx.Response(302, headers={"Location": f"{BASE}/elsewhere"}))
        async with new_async_client() as client:
            with pytest.raises(ServiceError) as info:
                await request(client, "GET", URL, service="svc")
    assert info.value.status == 302


# ── Retry-After parsing and backoff ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("value,expected", [
    ("2", 2.0), ("1.5", 1.5), (" 0 ", 0.0), ("-3", 0.0), ("", None), (None, None), ("soon", None),
    ("nan", None), ("inf", None), ("Wed, 21 Oct 2015 07:28:00 GMT", 0.0),
])
def test_parse_retry_after(value: str | None, expected: float | None) -> None:
    assert parse_retry_after(value) == expected


def test_parse_retry_after_future_http_date() -> None:
    value = email.utils.formatdate(time.time() + 20, usegmt=True)
    parsed = parse_retry_after(value)
    assert parsed is not None and 17.0 <= parsed <= 20.5


@pytest.mark.parametrize("attempt,low", [(2, 0.5), (3, 1.0), (4, 2.0), (5, 4.0), (7, 16.0), (8, 30.0), (12, 30.0)])
def test_backoff_delay_formula(attempt: int, low: float) -> None:
    for _ in range(20):
        delay = backoff_delay(attempt)
        assert low <= delay <= low + 0.25


def test_backoff_delay_prefers_retry_after() -> None:
    assert backoff_delay(2, "7") == 7.0
    assert backoff_delay(4, "999") == 30.0
    assert 0.5 <= backoff_delay(2, "not-a-number") <= 0.75


# ── AsyncLimiter ───────────────────────────────────────────────────────────────────────────────────
async def test_async_limiter_sliding_window_on_fake_clock() -> None:
    fake = FakeTime()
    limiter = AsyncLimiter(2, 1.0, clock=fake.clock, sleep=fake.sleep)
    for _ in range(5):
        async with limiter:
            pass
    assert limiter.acquired == 5
    assert fake.now >= 2.0  # 5 calls at 2/s take at least 2 s
    assert fake.sleeps == [1.0, 1.0]


async def test_async_limiter_concurrent_waiters() -> None:
    fake = FakeTime()
    limiter = AsyncLimiter(3, 60.0, clock=fake.clock, sleep=fake.sleep)
    stamps: list[float] = []

    async def one() -> None:
        async with limiter:
            stamps.append(fake.now)

    await asyncio.gather(*(one() for _ in range(7)))
    assert sorted(stamps) == [0.0, 0.0, 0.0, 60.0, 60.0, 60.0, 120.0]


async def test_async_limiter_real_time_is_quick_under_quota() -> None:
    limiter = AsyncLimiter(100, 60.0)
    started = time.monotonic()
    for _ in range(10):
        await limiter.acquire()
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("calls,period", [(0, 1.0), (-1, 1.0), (1, 0.0), (1, -5.0)])
def test_async_limiter_rejects_bad_arguments(calls: int, period: float) -> None:
    with pytest.raises(ValueError):
        AsyncLimiter(calls, period)


# ── the conftest network guard (12-testing §1: tests never touch the network) ──────────────────────
async def test_unmocked_request_is_blocked_not_retried(http_sleeps: list[float]) -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(RuntimeError, match="network access blocked"):
            await request(client, "GET", "https://api.example.test/unmocked", service="svc")
    assert http_sleeps == []  # failed loudly instead of being retried as a ConnectError


def test_raw_sockets_to_public_addresses_are_blocked() -> None:
    import socket

    with pytest.raises(RuntimeError, match="network access blocked"):
        socket.create_connection(("93.184.216.34", 443), timeout=1)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        with pytest.raises(RuntimeError, match="network access blocked"):
            sock.connect(("8.8.8.8", 53))
        with pytest.raises(RuntimeError, match="network access blocked"):
            sock.connect_ex(("1.1.1.1", 443))
    with pytest.raises(RuntimeError, match="network access blocked"):
        socket.getaddrinfo("example.com", 443)


def test_loopback_connections_stay_allowed() -> None:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("localhost", port), timeout=5):
            conn, _ = server.accept()
            conn.close()


def test_proxy_variables_are_removed_during_tests() -> None:
    import os

    assert not [name for name in os.environ if name.lower().endswith("_proxy")]
