"""HTTP layer: client factory, request() with retries, json_body(), AsyncLimiter (03-foundations §6, §10).

Retry policy of request() (03 §6):
- idempotent defaults to True for GET/HEAD/OPTIONS/PUT/DELETE, False for POST/PATCH;
- ConnectError / ConnectTimeout / PoolTimeout → always retried (the request never reached the server);
- ReadTimeout / WriteTimeout / RemoteProtocolError → retried only if idempotent;
- HTTP 429 / 503 → always retried (the server refused; nothing was processed);
- HTTP 500 / 502 / 504 → retried only if idempotent;
- 401 / 403 are never retried here (auth.request_with_auth() handles re-authentication).
A status listed in `expected` is final and returned as-is (never retried): CMA passes 429 in `expected`
together with max_attempts=1 to honour Retry-After itself (08 §6.4).
"""

from __future__ import annotations

import asyncio
import collections
import email.utils
import logging
import math
import random
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from contextlib import AbstractAsyncContextManager
from datetime import timezone
from types import TracebackType
from typing import Any

import httpx

from . import __version__
from .errors import ServiceError
from .logging_setup import redact, redact_excerpt

__all__ = [
    "DEFAULT_TIMEOUT",
    "IDEMPOTENT_METHODS",
    "REQUEST_ID_HEADERS",
    "AsyncLimiter",
    "backoff_delay",
    "json_body",
    "new_async_client",
    "parse_retry_after",
    "request",
    "request_id_from",
]

log = logging.getLogger("eta.http")

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
USER_AGENT = f"ETAutomationsAPI/{__version__}"

IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})
ALWAYS_RETRY_STATUSES = frozenset({429, 503})
IDEMPOTENT_RETRY_STATUSES = frozenset({500, 502, 504})
ALWAYS_RETRY_EXCEPTIONS: tuple[type[httpx.TransportError], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
)
IDEMPOTENT_RETRY_EXCEPTIONS: tuple[type[httpx.TransportError], ...] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.RemoteProtocolError,
)
MAX_BACKOFF_S = 30.0
MAX_LOGGED_PATH = 300

# 03 §6 step 4 (incl. x-azure-ref from 06 §12): the first one present becomes ServiceError.request_id.
REQUEST_ID_HEADERS = (
    "intuit_tid",
    "x-request-id",
    "x-correlation-id",
    "x-correlationid",
    "x-kong-request-id",
    "x-azure-ref",
    "request-id",
)


def new_async_client(
    *,
    base_url: str = "",
    headers: Mapping[str, str] | None = None,
    timeout: httpx.Timeout | float = DEFAULT_TIMEOUT,
) -> httpx.AsyncClient:
    """httpx.AsyncClient with follow_redirects=False and trust_env=True (honours HTTPS_PROXY /
    SSL_CERT_FILE); default headers User-Agent/Accept merged (case-insensitively) with `headers`.
    A float timeout is the read/write/pool timeout; the connect timeout stays at most 10 s."""
    merged = httpx.Headers({"User-Agent": USER_AGENT, "Accept": "application/json"})
    if headers:
        merged.update(headers)
    if not isinstance(timeout, httpx.Timeout):
        seconds = float(timeout)
        timeout = httpx.Timeout(seconds, connect=min(10.0, seconds))
    return httpx.AsyncClient(
        base_url=base_url,
        headers=merged,
        timeout=timeout,
        follow_redirects=False,
        trust_env=True,
    )


class AsyncLimiter:
    """Sliding-window rate limiter: at most `max_calls` acquisitions per `period_s` seconds (03 §10).

    Use as `async with limiter:`. Waiters are served in FIFO order. `clock`/`sleep` are injectable for
    tests (an injected sleep must advance the injected clock). `acquired` counts granted slots.
    """

    def __init__(
        self,
        max_calls: int,
        period_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if int(max_calls) < 1:
            raise ValueError(f"max_calls must be >= 1 (got {max_calls!r})")
        if not float(period_s) > 0:
            raise ValueError(f"period_s must be > 0 (got {period_s!r})")
        self.max_calls = int(max_calls)
        self.period_s = float(period_s)
        self.acquired = 0
        self._clock = clock
        self._sleep = sleep
        self._stamps: collections.deque[float] = collections.deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = self._clock()
                cutoff = now - self.period_s
                while self._stamps and self._stamps[0] <= cutoff:
                    self._stamps.popleft()
                if len(self._stamps) < self.max_calls:
                    self._stamps.append(now)
                    self.acquired += 1
                    return
                delay = max(0.0, self._stamps[0] + self.period_s - now)
                await (self._sleep or asyncio.sleep)(delay)

    async def __aenter__(self) -> AsyncLimiter:
        await self.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    def __repr__(self) -> str:
        return f"AsyncLimiter({self.max_calls}, {self.period_s})"


def parse_retry_after(value: str | None) -> float | None:
    """Retry-After as seconds (number or HTTP-date), floored at 0; None when absent/unparseable."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:  # "-0000" zone → naive; RFC 9110 HTTP-dates are GMT
            when = when.replace(tzinfo=timezone.utc)
        seconds = when.timestamp() - time.time()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def backoff_delay(attempt: int, retry_after: str | None = None) -> float:
    """Delay before attempt `attempt` (>= 2): Retry-After capped at 30 s, else
    min(30, 0.5 * 2**(attempt-2)) + uniform(0, 0.25) (03 §6 step 2)."""
    parsed = parse_retry_after(retry_after)
    if parsed is not None:
        return min(MAX_BACKOFF_S, parsed)
    return min(MAX_BACKOFF_S, 0.5 * 2 ** (attempt - 2)) + random.uniform(0, 0.25)


async def _sleep(seconds: float) -> None:
    # Indirection: tests may patch either this function or asyncio.sleep (12-testing §3).
    await asyncio.sleep(seconds)


def request_id_from(headers: Mapping[str, str] | httpx.Headers) -> str | None:
    """First present request-id header (REQUEST_ID_HEADERS order), or None."""
    hdrs = headers if isinstance(headers, httpx.Headers) else httpx.Headers(headers)
    for name in REQUEST_ID_HEADERS:
        value = hdrs.get(name)
        if value and value.strip():
            return value.strip()
    return None


def _shorten(text: str, limit: int = MAX_LOGGED_PATH) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _joined_url(client: httpx.AsyncClient, url: httpx.URL | str) -> str:
    """Best-effort absolute URL for log lines when no request object is available."""
    text = str(url)
    try:
        if httpx.URL(text).is_absolute_url:
            return text
    except Exception:  # noqa: BLE001 - only used for logging
        return text
    base = str(client.base_url)
    return f"{base.rstrip('/')}/{text.lstrip('/')}" if base else text


def _exc_url(exc: httpx.RequestError, fallback: str) -> str:
    try:
        return str(exc.request.url)
    except RuntimeError:  # .request not set
        return fallback


def _path_and_query(resp: httpx.Response, fallback: str) -> str:
    try:
        url = resp.request.url
    except RuntimeError:
        return fallback
    query = url.query.decode("ascii", "replace")
    return url.path + (f"?{query}" if query else "")


def _response_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 - undecodable body: excerpt is best-effort
        return ""


def _status_error(service: str, method: str, resp: httpx.Response, fallback_url: str) -> ServiceError:
    redacted_path = _shorten(redact(_path_and_query(resp, fallback_url)))
    return ServiceError(
        service,
        f"unexpected HTTP {resp.status_code} for {method} {redacted_path}",
        status=resp.status_code,
        body_excerpt=redact_excerpt(_response_text(resp), 500),
        request_id=request_id_from(resp.headers),
    )


async def request(
    client: httpx.AsyncClient,
    method: str,
    url: httpx.URL | str,
    *,
    service: str,
    expected: Collection[int] = (200,),
    idempotent: bool | None = None,
    max_attempts: int = 4,
    limiter: AsyncLimiter | AbstractAsyncContextManager[Any] | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """Send one logical request with the 03 §6 retry policy (see module docstring).

    Returns the response when its status is in `expected`; otherwise raises ServiceError(service,
    "unexpected HTTP {status} for {method} {redacted path}", status, redacted body excerpt <= 500 chars,
    request_id). Transport errors that are not retried / exhaust the attempts raise
    ServiceError(service, f"network error: {exc!r}") chained from the original (status None).
    Every attempt is logged at DEBUG on "eta.http". If `limiter` is given, each attempt runs inside
    `async with limiter:`. kwargs go to httpx.AsyncClient.request (params, json, data, content, headers…).
    """
    method_u = method.upper()
    if idempotent is None:
        idempotent = method_u in IDEMPOTENT_METHODS
    attempts = max(1, int(max_attempts))
    ok_statuses = frozenset(expected)
    shown_url = _joined_url(client, url)
    retry_after: str | None = None

    for attempt in range(1, attempts + 1):
        if attempt > 1:
            delay = backoff_delay(attempt, retry_after)
            log.debug(
                "%s: retrying %s %s in %.2f s (attempt %d/%d)",
                service, method_u, redact(shown_url), delay, attempt, attempts,
            )
            await _sleep(delay)
        retry_after = None
        started = time.perf_counter()
        try:
            if limiter is not None:
                async with limiter:
                    resp = await client.request(method_u, url, **kwargs)
            else:
                resp = await client.request(method_u, url, **kwargs)
        except httpx.RequestError as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            log.debug(
                "%s %s -> %s in %.0f ms (attempt %d/%d)",
                method_u, redact(_exc_url(exc, shown_url)), type(exc).__name__, elapsed_ms, attempt, attempts,
            )
            retryable = isinstance(exc, ALWAYS_RETRY_EXCEPTIONS) or (
                idempotent and isinstance(exc, IDEMPOTENT_RETRY_EXCEPTIONS)
            )
            if retryable and attempt < attempts:
                continue
            raise ServiceError(service, redact(f"network error: {exc!r}")) from exc

        elapsed_ms = (time.perf_counter() - started) * 1000
        status = resp.status_code
        try:
            logged_url = str(resp.request.url)
        except RuntimeError:
            logged_url = shown_url
        log.debug(
            "%s %s -> %d in %.0f ms (attempt %d/%d)",
            method_u, redact(logged_url), status, elapsed_ms, attempt, attempts,
        )
        if status in ok_statuses:
            return resp
        retryable = status in ALWAYS_RETRY_STATUSES or (idempotent and status in IDEMPOTENT_RETRY_STATUSES)
        if retryable and attempt < attempts:
            retry_after = resp.headers.get("Retry-After")
            continue
        raise _status_error(service, method_u, resp, shown_url)

    raise AssertionError("unreachable: the attempt loop always returns or raises")  # pragma: no cover


def json_body(resp: httpx.Response, *, service: str) -> Any:
    """resp.json() or raise ServiceError(service, 'response was not valid JSON', status=..., body_excerpt=...)."""
    try:
        return resp.json()
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise ServiceError(
            service,
            "response was not valid JSON",
            status=resp.status_code,
            body_excerpt=redact_excerpt(_response_text(resp), 500),
            request_id=request_id_from(resp.headers),
        ) from exc
