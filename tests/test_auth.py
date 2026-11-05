"""auth.py: TokenProvider protocol and request_with_auth() (03-foundations §8)."""

from __future__ import annotations

import asyncio

import httpx
import pytest
import respx

from eta_automations.auth import TokenProvider, bearer_headers, request_with_auth
from eta_automations.errors import AuthError, ServiceError

URL = "https://api.example.test/v1/resource"


class FakeProvider:
    """Hands out T1, T2, … ; counts invalidations."""

    def __init__(self) -> None:
        self.issued = 0
        self.invalidations = 0

    async def get_token(self) -> str:
        self.issued += 1
        return f"T{self.issued}"

    async def invalidate(self) -> None:
        self.invalidations += 1


class LatchingProvider(FakeProvider):
    """Like MscBridgeAuth (06 §5.1): invalidate() sets a fatal error that get_token() raises afterwards."""

    def __init__(self) -> None:
        super().__init__()
        self.fatal: AuthError | None = None

    async def get_token(self) -> str:
        if self.fatal is not None:
            raise self.fatal
        return await super().get_token()

    async def invalidate(self) -> None:
        await super().invalidate()
        self.fatal = AuthError("The access token expired or was rejected (HTTP 401). Paste a fresh one.")


def test_fake_providers_satisfy_protocol() -> None:
    assert isinstance(FakeProvider(), TokenProvider)
    assert not isinstance(object(), TokenProvider)


def test_bearer_headers() -> None:
    assert bearer_headers("abc") == {"Authorization": "Bearer abc"}


async def test_success_sends_bearer_token_once() -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(200, json={"ok": True}))
        async with httpx.AsyncClient() as client:
            resp = await request_with_auth(client, "GET", URL, service="svc", auth=auth)
    assert resp.status_code == 200
    assert route.call_count == 1
    assert route.calls.last.request.headers["Authorization"] == "Bearer T1"
    assert auth.invalidations == 0


async def test_401_invalidates_and_retries_once_with_fresh_token() -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        route = router.get(URL).mock(side_effect=[httpx.Response(401), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            resp = await request_with_auth(client, "GET", URL, service="svc", auth=auth)
    assert resp.status_code == 200
    assert route.call_count == 2
    assert [c.request.headers["Authorization"] for c in route.calls] == ["Bearer T1", "Bearer T2"]
    assert auth.invalidations == 1


async def test_second_401_raises_auth_error() -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=httpx.Response(401, text="expired"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AuthError) as info:
                await request_with_auth(client, "POST", URL, service="qbo", auth=auth, json={"a": 1})
    assert str(info.value) == "qbo: credentials/token rejected (HTTP 401)"
    assert isinstance(info.value.__cause__, ServiceError)
    assert info.value.__cause__.status == 401
    assert route.call_count == 2
    assert auth.invalidations == 1


async def test_403_is_not_reauthenticated_by_default() -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(403, text="not permitted"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request_with_auth(client, "GET", URL, service="maersk", auth=auth)
    assert info.value.status == 403
    assert route.call_count == 1
    assert auth.invalidations == 0


async def test_403_opt_in_reauth_like_401() -> None:
    """VShip (05 §5): 403 can mean 'token expired' → reauth_statuses=(401, 403)."""
    auth = FakeProvider()
    with respx.mock() as router:
        ok_route = router.get(URL).mock(side_effect=[httpx.Response(403), httpx.Response(200)])
        async with httpx.AsyncClient() as client:
            resp = await request_with_auth(client, "GET", URL, service="vship", auth=auth,
                                           reauth_statuses=(401, 403))
        assert resp.status_code == 200
        assert ok_route.call_count == 2
        assert auth.invalidations == 1

        ok_route.side_effect = [httpx.Response(403), httpx.Response(403)]
        async with httpx.AsyncClient() as client:
            with pytest.raises(AuthError, match=r"^vship: credentials/token rejected \(HTTP 403\)$"):
                await request_with_auth(client, "GET", URL, service="vship", auth=auth,
                                        reauth_statuses=(401, 403))


async def test_custom_build_headers_and_caller_headers_are_merged() -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            await request_with_auth(
                client, "GET", URL, service="cma", auth=auth,
                build_headers=lambda key: {"KeyId": key},
                headers={"X-Extra": "1", "keyid": "caller-value-loses"},
                params={"limit": "500"},
            )
    sent = route.calls.last.request
    assert sent.headers.get_list("KeyId") == ["T1"]
    assert sent.headers["X-Extra"] == "1"
    assert "Authorization" not in sent.headers
    assert sent.url.params["limit"] == "500"


async def test_other_service_errors_propagate_without_reauth(http_sleeps: list[float]) -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=httpx.Response(500))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request_with_auth(client, "POST", URL, service="svc", auth=auth)
    assert info.value.status == 500
    assert route.call_count == 1
    assert auth.invalidations == 0


async def test_network_errors_propagate_without_reauth(http_sleeps: list[float]) -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        router.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
        async with httpx.AsyncClient() as client:
            with pytest.raises(ServiceError) as info:
                await request_with_auth(client, "POST", URL, service="svc", auth=auth)
    assert info.value.status is None
    assert auth.invalidations == 0


async def test_provider_latched_error_stops_the_retry() -> None:
    """06 §5.1: after a 401 the provider's own AuthError surfaces; no second HTTP request is made."""
    auth = LatchingProvider()
    with respx.mock() as router:
        route = router.post(URL).mock(return_value=httpx.Response(401))
        async with httpx.AsyncClient() as client:
            with pytest.raises(AuthError, match="expired or was rejected"):
                await request_with_auth(client, "POST", URL, service="msc", auth=auth, idempotent=True)
    assert route.call_count == 1
    assert auth.invalidations == 1


async def test_expected_401_is_returned_not_reauthenticated() -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        router.get(URL).mock(return_value=httpx.Response(401))
        async with httpx.AsyncClient() as client:
            resp = await request_with_auth(client, "GET", URL, service="svc", auth=auth, expected=(200, 401))
    assert resp.status_code == 401
    assert auth.invalidations == 0


async def test_concurrent_callers_each_get_a_token() -> None:
    auth = FakeProvider()
    with respx.mock() as router:
        route = router.get(URL).mock(return_value=httpx.Response(200))
        async with httpx.AsyncClient() as client:
            results = await asyncio.gather(*(request_with_auth(client, "GET", URL, service="svc", auth=auth)
                                             for _ in range(5)))
    assert [r.status_code for r in results] == [200] * 5
    assert route.call_count == 5
