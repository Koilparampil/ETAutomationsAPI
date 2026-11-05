"""TokenProvider protocol and request_with_auth() (03-foundations §8).

Each service implements its own TokenProvider (QboAuth, VShipAuth, MscBridgeAuth, MaerskAuth, CmaOAuth,
GraphAuth, …). Concurrency rule: get_token() takes an asyncio.Lock, re-checks freshness inside it, and
only then performs the network call. A token provider never acquires its service's concurrency semaphore
(03 §10) — token calls go straight through http.request().
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping
from typing import Any, Protocol, runtime_checkable

import httpx

from .errors import AuthError, ServiceError
from .http import request

__all__ = ["TokenProvider", "bearer_headers", "request_with_auth"]

log = logging.getLogger("eta.http")


@runtime_checkable
class TokenProvider(Protocol):
    async def get_token(self) -> str:
        """Cached-if-fresh, else obtain/refresh (under an asyncio.Lock)."""
        ...

    async def invalidate(self) -> None:
        """Drop the cached access token (keep refresh tokens)."""
        ...


def bearer_headers(token: str) -> dict[str, str]:
    """Default header builder: {"Authorization": f"Bearer {token}"}."""
    return {"Authorization": f"Bearer {token}"}


async def request_with_auth(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL | str,
    *,
    service: str,
    auth: TokenProvider,
    build_headers: Callable[[str], Mapping[str, str]] = bearer_headers,
    reauth_statuses: Collection[int] = (401,),
    **kwargs: Any,
) -> httpx.Response:
    """token = await auth.get_token(); call http.request(...) with the caller's headers plus
    build_headers(token) (auth headers win, case-insensitively).

    If request() raises ServiceError with a status in `reauth_statuses` (default 401 only; pass
    `reauth_statuses=(401, 403)` for services whose 403 means "token expired", e.g. VShip 05 §5):
    await auth.invalidate(), get a fresh token and retry exactly once. A second rejection raises
    AuthError(f"{service}: credentials/token rejected (HTTP {status})") chained from the ServiceError.
    Every other error (including an AuthError raised by the provider itself) propagates unchanged.
    Remaining kwargs (params, json, expected, idempotent, max_attempts, limiter, …) go to request().
    """
    caller_headers = kwargs.pop("headers", None)
    reauth = frozenset(reauth_statuses)
    for attempt in (1, 2):
        token = await auth.get_token()
        headers = httpx.Headers(caller_headers or {})
        headers.update(build_headers(token))
        try:
            return await request(client, method, url, service=service, headers=headers, **kwargs)
        except ServiceError as exc:
            if exc.status is None or exc.status not in reauth:
                raise
            if attempt == 2:
                raise AuthError(f"{service}: credentials/token rejected (HTTP {exc.status})") from exc
            log.debug("%s: HTTP %s — dropping the cached token and retrying once", service, exc.status)
            await auth.invalidate()
    raise AssertionError("unreachable: request_with_auth always returns or raises")  # pragma: no cover
