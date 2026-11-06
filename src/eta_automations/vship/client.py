"""VShipCRM HTTP client: VShipAuth (token lifecycle) and VShipClient (05-vship-crm §4–§6, §10).

Endpoints (05 §4, paths relative to VSHIP_API_BASE):
  V1 POST  /Auth/login                  {"username", "password"}            200 → value.token
  V2 GET   /Bookings/searchBooking      searchText, page=1, pageSize=50     200 → value.data[].bookingId/bookingNo
  V3 GET   /Bookings/{bookingId}                                            200 → value.customerName
  V4 GET   /Bookings/{bookingId}/comments                                   200 → value.internalComments[]
  V5 POST  /Bookings/{bookingId}/comments  {"comment", "commentType"}       201
  V6 PATCH /Bookings/{bookingId}        {"booking_Id", "expected_Arrival_Date"}  204

- Every HTTP call of VShipClient runs under its semaphore and through `request_with_auth` with
  reauth on 401 **and 403** (05 §5 step 5). VShipAuth never takes that semaphore (03 §8).
- Each base booking is resolved to its VShip id once per run, and each booking's comments are read once
  per run (single-flight caches; D18). Posting a note invalidates that booking's cached comments.
- V5/V6 are not idempotent: `http.request()` retries them only on connect-phase errors and 429/503; a
  read timeout is reported, never retried (a duplicate note is worse than a missing one).
- `read_only=True` (dry run / probes): V5/V6 raise RuntimeError without any HTTP call.
- Never logs or raises with the password or a token (05 §9 test 9).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, TypeVar
from urllib.parse import quote

import httpx

from ..auth import request_with_auth
from ..config import Settings, VShipSettings
from ..errors import (
    AuthError,
    BookingChangeError,
    ConfigError,
    NoteError,
    ServiceError,
    VShipBookingNotFoundError,
)
from ..http import json_body, new_async_client, request
from ..logging_setup import MASK, redact, redact_excerpt
from ..token_store import TokenStore, is_fresh, jwt_exp
from .logic import best_match, comment_text, has_tr_swb_note, match_rank, notif_status

__all__ = [
    "READ_ONLY_MESSAGE",
    "REAUTH_STATUSES",
    "VSHIP_TIMEOUT",
    "VShipAuth",
    "VShipClient",
    "build_vship",
]

log = logging.getLogger("eta.vship")

T = TypeVar("T")
E = TypeVar("E", bound=ServiceError)

SERVICE = "vship"
TOKEN_KEY = "vship"                       # TokenStore entry (03 §7)
VSHIP_TIMEOUT = httpx.Timeout(20.0)       # 05 §4: 20 s per call
REAUTH_STATUSES = (401, 403)              # 05 §5 step 5: VShip may answer an expired token with 403
WRITE_HEADERS = {"Content-Type": "application/json", "Referer": "https://vshipcrm.com/"}  # 05 §4 (parity)
SEARCH_PAGE_SIZE = 50
LOGIN_EXCERPT_CHARS = 200
CANDIDATES_SHOWN = 5
READ_ONLY_MESSAGE = "VShipClient is read-only (dry run / probe)"

_SEARCH = "GET /Bookings/searchBooking"
_BOOKING = "GET /Bookings/{bookingId}"
_COMMENTS = "GET /Bookings/{bookingId}/comments"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# helpers
# ════════════════════════════════════════════════════════════════════════════════════════════════
def _api_base(settings: VShipSettings) -> str:
    return settings.api_base.rstrip("/")


def _normalize_booking(base_booking: str) -> str:
    """Cache key and search text: stripped + upper-cased base booking (01 §3 D5)."""
    return str(base_booking).strip().upper()


def _path_id(booking_id: str) -> str:
    """Booking id as one URL path segment (ids are numeric in practice; never let one add path segments)."""
    return quote(str(booking_id).strip(), safe="")


def _response_text(resp: httpx.Response) -> str:
    try:
        return resp.text
    except Exception:  # noqa: BLE001 - undecodable body: excerpt is best-effort
        return ""


def _structure_error(payload: Any, what: str) -> ServiceError:
    """05 §6.2: ServiceError naming only the key names of the payload (never values)."""
    if isinstance(payload, Mapping):
        detail = f"top-level keys={sorted(str(k) for k in payload)}"
        value = payload.get("value")
        if isinstance(value, Mapping):
            detail += f", value keys={sorted(str(k) for k in value)}"
    else:
        detail = f"top-level keys=[] (JSON {type(payload).__name__})"
    return ServiceError(SERVICE, f"unexpected JSON structure: {detail} ({what})")


def _value(payload: Any, what: str) -> dict[str, Any]:
    """`payload["value"]` as a dict; `value` missing/null/not an object → ServiceError (05 §4 notes)."""
    value = payload.get("value") if isinstance(payload, Mapping) else None
    if not isinstance(value, Mapping):
        raise _structure_error(payload, what)
    return dict(value)


def _search_records(payload: Any) -> list[dict[str, Any]]:
    """V2: `value.data[]` (non-object entries ignored); `data` not a list → ServiceError."""
    data = _value(payload, _SEARCH).get("data")
    if not isinstance(data, list):
        raise _structure_error(payload, _SEARCH)
    return [dict(r) for r in data if isinstance(r, Mapping)]


def _internal_comments(payload: Any) -> list[dict[str, Any]]:
    """V4: `value.internalComments` or [] (01 §11: missing/null → []); not a list → ServiceError."""
    raw = _value(payload, _COMMENTS).get("internalComments")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _structure_error(payload, _COMMENTS)
    return [dict(c) for c in raw if isinstance(c, Mapping)]


def _customer_name(booking: Mapping[str, Any]) -> str:
    """V3: `value.customerName or ""` (05 §6.2)."""
    name = booking.get("customerName")
    if not name:
        return ""
    return name if isinstance(name, str) else str(name)


def _booking_no(record: Mapping[str, Any]) -> str:
    value = record.get("bookingNo")
    return "" if value is None else str(value)


def _booking_id_of(record: Mapping[str, Any]) -> str:
    """`bookingId` as a string (it may arrive as an int — 05 §4 notes)."""
    raw = record.get("bookingId")
    text = "" if raw is None or isinstance(raw, bool) else str(raw).strip()
    if not text:
        raise ServiceError(
            SERVICE,
            f"unexpected JSON structure: matched booking has no bookingId (keys={sorted(str(k) for k in record)})",
        )
    return text


def _wrap(cls: type[E], exc: ServiceError | AuthError) -> E:
    """Re-type a ServiceError/AuthError as NoteError/BookingChangeError (05 §6.2), keeping status/body."""
    if isinstance(exc, ServiceError):
        return cls.wrap(exc)
    text = str(exc)
    prefix = f"{SERVICE}: "
    return cls(SERVICE, text[len(prefix):] if text.startswith(prefix) else text)


def _epoch_to_iso(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(float(value), UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 05 §5 — VShipAuth
# ════════════════════════════════════════════════════════════════════════════════════════════════
class VShipAuth:
    """TokenProvider for VShipCRM (05 §5): cached in the TokenStore under "vship".

    `expires_at` is the JWT `exp` when the token is a JWT (03 §7), else now + VSHIP_TOKEN_TTL_MINUTES.
    Never prompts: bad credentials raise AuthError (the pipeline's preflight reports it).
    `time_source` (epoch seconds) is injectable for tests.
    """

    def __init__(
        self,
        settings: VShipSettings,
        token_store: TokenStore,
        *,
        http: httpx.AsyncClient | None = None,
        time_source: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings
        self._store = token_store
        self._http = http
        self._owns_http = False
        self._time = time_source
        self._lock = asyncio.Lock()
        self.logins = 0  # V1 calls made by this provider (diagnostics)

    async def get_token(self) -> str:
        """Cached token if fresh (120 s skew), else log in — under a lock, so concurrent callers share one login."""
        async with self._lock:
            entry = self._store.get(TOKEN_KEY)
            if entry is not None and is_fresh(entry, now=self._time()):
                return str(entry["access_token"])
            return await self._login()

    async def invalidate(self) -> None:
        """Drop the cached token (request_with_auth calls this after a 401/403)."""
        self._store.delete(TOKEN_KEY)

    def token_info(self) -> dict[str, Any]:
        """Expiry of the cached token for the probe (05 §10 step 1) — never the token itself."""
        entry = self._store.get(TOKEN_KEY)
        if not entry:
            return {"cached": False}
        now = self._time()
        expires_at = entry.get("expires_at")
        info: dict[str, Any] = {
            "cached": True,
            "fresh": is_fresh(entry, now=now),
            "expires_at": _epoch_to_iso(expires_at),
            "obtained_at": _epoch_to_iso(entry.get("obtained_at")),
        }
        try:
            info["seconds_left"] = int(float(expires_at) - now)
        except (TypeError, ValueError, OverflowError):
            info["seconds_left"] = None
        return info

    async def aclose(self) -> None:
        """Close the HTTP client this provider created itself (an injected one is left to its owner)."""
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None
            self._owns_http = False

    # ── internals ──
    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = new_async_client(timeout=VSHIP_TIMEOUT)
            self._owns_http = True
        return self._http

    def _scrub(self, text: str, *, truncated: bool = False) -> str:
        """Mask the password even where it is not under a secret-looking key (e.g. echoed in free text).

        `truncated=True` (body excerpts cut at a fixed length) also masks a trailing start of the password
        that the cut left behind (3+ characters).
        """
        password = self._settings.password
        if not password:
            return text
        forms = sorted({password, json.dumps(password)[1:-1]}, key=len, reverse=True)
        for form in forms:
            if form:
                text = text.replace(form, MASK)
        if truncated:
            for form in forms:
                for n in range(min(len(form), len(text)), 2, -1):
                    if text.endswith(form[:n]):
                        return text[:-n] + MASK
        return text

    def _sanitized(self, exc: ServiceError) -> ServiceError:
        """Copy of a V1 ServiceError with the password masked everywhere (it may become a __cause__)."""
        return ServiceError(
            SERVICE,
            self._scrub(exc.message),
            status=exc.status,
            body_excerpt=self._scrub(exc.body_excerpt, truncated=True),
            request_id=exc.request_id,
        )

    async def _login(self) -> str:
        """V1 → token; store {"access_token", "expires_at", "obtained_at"} (05 §5 steps 2–4)."""
        url = f"{_api_base(self._settings)}/Auth/login"
        body = {"username": self._settings.username, "password": self._settings.password}
        self.logins += 1
        failure: ServiceError | None = None
        try:
            resp = await request(
                self._client(), "POST", url, service=SERVICE, expected=(200,), idempotent=True,
                json=body, headers={"Accept": "application/json"}, timeout=VSHIP_TIMEOUT,
            )
        except ServiceError as exc:
            failure = self._sanitized(exc)
        if failure is not None:
            # Raised outside the except block so the unsanitized error is not attached as __context__.
            if failure.status is not None:
                message = f"VShip login failed (HTTP {failure.status})"
                if failure.request_id:
                    message += f" [request_id={failure.request_id}]"
                if failure.body_excerpt:
                    message += f" body: {failure.body_excerpt}"
            else:
                message = f"VShip login failed: {failure.message}"
            raise AuthError(message) from failure

        token = self._extract_token(resp)
        now = self._time()
        exp = jwt_exp(token)
        expires_at = exp if exp is not None else now + self._settings.token_ttl_minutes * 60
        self._store.put(TOKEN_KEY, {"access_token": token, "expires_at": expires_at, "obtained_at": now})
        if expires_at <= now:
            log.warning("VShip login returned a token whose expiry %s is not in the future (clock skew?)",
                        _epoch_to_iso(expires_at))
        log.debug("VShip login OK; token valid until %s (%s)", _epoch_to_iso(expires_at),
                  "JWT exp" if exp is not None else f"assumed {self._settings.token_ttl_minutes} min")
        return token

    def _extract_token(self, resp: httpx.Response) -> str:
        """`value.token`, or AuthError with the first 200 redacted characters of the response (05 §5 step 2)."""
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        value = payload.get("value") if isinstance(payload, Mapping) else None
        token = value.get("token") if isinstance(value, Mapping) else None
        if isinstance(token, str) and token.strip():
            return token.strip()
        excerpt = self._scrub(redact_excerpt(_response_text(resp), LOGIN_EXCERPT_CHARS), truncated=True)
        raise AuthError(f"VShip login succeeded but no token in response: {excerpt}")


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 05 §6.2 — VShipClient
# ════════════════════════════════════════════════════════════════════════════════════════════════
class VShipClient:
    """Async VShipCRM client for one run (05 §6.2). See the module docstring for the endpoints."""

    def __init__(
        self,
        settings: VShipSettings,
        auth: VShipAuth,
        *,
        http: httpx.AsyncClient | None = None,
        semaphore: asyncio.Semaphore | None = None,
        read_only: bool = False,
    ) -> None:
        self.settings = settings
        self.auth = auth
        self.read_only = read_only
        self._owns_http = http is None
        self._http = http if http is not None else new_async_client(timeout=VSHIP_TIMEOUT)
        self._sem = semaphore if semaphore is not None else asyncio.Semaphore(settings.max_concurrency)
        self._base = _api_base(settings)
        # per-run single-flight caches (D18): results AND exceptions are shared by every caller
        self._booking_ids: dict[str, asyncio.Future[str]] = {}
        self._comments: dict[str, asyncio.Future[list[dict[str, Any]]]] = {}
        self._base_by_id: dict[str, str] = {}  # booking id → base booking, for log lines

    async def __aenter__(self) -> VShipClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    # ── reads (V2–V4; allowed in dry run) ──
    async def search_bookings(self, search_text: str) -> list[dict[str, Any]]:
        """V2 candidate records for `search_text` (not cached, not ranked)."""
        return _search_records(await self._get_json("/Bookings/searchBooking", params=self._search_params(search_text)))

    async def find_booking_id(self, base_booking: str) -> str:
        """V2 + best_match → bookingId as a string; cached per run, concurrent callers share one request.

        Not found (no candidates, or only unrelated ones — D17) → VShipBookingNotFoundError(
        'vship', "No VShip booking matches 'X' (candidates: <up to 5 bookingNo values>)").
        """
        key = _normalize_booking(base_booking)
        return await self._single_flight(self._booking_ids, key, lambda: self._resolve_booking_id(key))

    async def get_booking(self, booking_id: str) -> dict[str, Any]:
        """V3 → payload["value"]."""
        return _value(await self._get_json(f"/Bookings/{_path_id(booking_id)}"), _BOOKING)

    async def get_customer_name(self, base_booking: str) -> str:
        """find_booking_id → V3 → `value.customerName` or "" (01 §7.2; the pipeline applies D14 on errors)."""
        booking_id = await self.find_booking_id(base_booking)
        return _customer_name(await self.get_booking(booking_id))

    async def get_internal_comments(self, booking_id: str) -> list[dict[str, Any]]:
        """V4 → `value.internalComments` or []; cached per run (shared by the notif and TR/SWB checks).

        Returns fresh copies, so callers may modify them without affecting the cache.
        """
        key = str(booking_id).strip()
        comments = await self._single_flight(self._comments, key, lambda: self._fetch_comments(key))
        return [dict(c) for c in comments]

    # ── writes (V5/V6; never in dry run) ──
    async def post_comment(self, booking_id: str, text: str) -> None:
        """V5 with `commentType` VSHIP_COMMENT_TYPE; anything but 201 → NoteError (chained).

        Not retried after a read timeout (03 §6: POST is not idempotent). Invalidates the comments cache.
        """
        if self.read_only:
            raise RuntimeError(READ_ONLY_MESSAGE)
        key = str(booking_id).strip()
        log.info("[VShip] Posting note for booking %s: %r", self._label(key), text)
        body = {"comment": text, "commentType": self.settings.comment_type}
        try:
            await self._call("POST", f"/Bookings/{_path_id(key)}/comments", expected=(201,), write=True, json=body)
        except (ServiceError, AuthError) as exc:
            raise _wrap(NoteError, exc) from exc
        finally:
            self._comments.pop(key, None)  # the comments changed (or may have, e.g. after a timeout)

    async def patch_expected_arrival(self, booking_id: str, eta: datetime) -> None:
        """V6 `{"booking_Id": "<id>", "expected_Arrival_Date": "YYYY-mm-ddTHH:MM:SS"}`; anything but 204 →
        BookingChangeError (chained). Not retried on ambiguous failures (PATCH is not idempotent)."""
        if self.read_only:
            raise RuntimeError(READ_ONLY_MESSAGE)
        key = str(booking_id).strip()
        body = {"booking_Id": key, "expected_Arrival_Date": eta.strftime("%Y-%m-%dT%H:%M:%S")}
        try:
            await self._call("PATCH", f"/Bookings/{_path_id(key)}", expected=(204,), write=True, json=body)
        except (ServiceError, AuthError) as exc:
            raise _wrap(BookingChangeError, exc) from exc
        log.info("[VShip] VShip ETA %s for %s done.", eta.strftime("%Y-%m-%d"), self._label(key))

    # ── diagnostics ──
    async def probe(self, base_booking: str, *, run_now: datetime) -> dict[str, Any]:
        """05 §10 steps 1–4 (read-only: V1–V4 only, bypasses the caches). JSON-serializable result:
        token expiry (never the token), candidates with match_rank, chosen bookingId, customerName, internal
        comments, NotifStatus, TR/SWB verdict, redacted raw payloads under "raw", and "error" (or None)."""
        key = _normalize_booking(base_booking)
        result: dict[str, Any] = {"service": SERVICE, "booking": key, "raw": {}, "error": None}
        try:
            await self.auth.get_token()
            result["token"] = self.auth.token_info()
            search_payload = await self._get_json("/Bookings/searchBooking", params=self._search_params(key))
            result["raw"]["search"] = redact(search_payload)
            records = _search_records(search_payload)
            result["candidates"] = [
                {"bookingId": r.get("bookingId"), "bookingNo": _booking_no(r),
                 "match_rank": list(match_rank(_booking_no(r), key))}
                for r in records
            ]
            best = best_match(records, key)
            if best is None:
                result["error"] = self._not_found_message(key, records)
                return result
            booking_id = _booking_id_of(best)
            result["booking_id"] = booking_id
            booking_payload = await self._get_json(f"/Bookings/{_path_id(booking_id)}")
            result["raw"]["booking"] = redact(booking_payload)
            result["customer_name"] = _customer_name(_value(booking_payload, _BOOKING))
            comments_payload = await self._get_json(f"/Bookings/{_path_id(booking_id)}/comments")
            result["raw"]["comments"] = redact(comments_payload)
            comments = _internal_comments(comments_payload)
            status = notif_status(comments, run_now)
            result["comments"] = [{"comment": comment_text(c), "createdDate": c.get("createdDate")}
                                  for c in comments]
            result["notif_status"] = {"count": status.count, "have_to_do": status.have_to_do}
            result["tr_swb"] = has_tr_swb_note(comments)
        except (ServiceError, AuthError) as exc:
            result["error"] = str(exc)
        return result

    async def aclose(self) -> None:
        """Cancel lookups still in flight, close the HTTP client this instance owns, and the auth's own client."""
        pending: list[asyncio.Future[Any]] = []
        for cache in (self._booking_ids, self._comments):
            pending.extend(f for f in cache.values() if not f.done())
            cache.clear()
        for fut in pending:
            fut.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._owns_http:
            await self._http.aclose()
        await self.auth.aclose()

    # ── internals ──
    @staticmethod
    def _search_params(search_text: str) -> dict[str, str | int]:
        return {"searchText": search_text, "page": 1, "pageSize": SEARCH_PAGE_SIZE}

    def _label(self, booking_id: str) -> str:
        return self._base_by_id.get(booking_id, booking_id)

    async def _call(
        self,
        method: str,
        path: str,
        *,
        expected: tuple[int, ...] = (200,),
        write: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        """One authenticated call: semaphore, Bearer token, reauth once on 401/403, 20 s timeout (05 §4–§6)."""
        headers = {"Accept": "application/json", **(WRITE_HEADERS if write else {})}
        async with self._sem:
            return await request_with_auth(
                self._http, method, f"{self._base}{path}", service=SERVICE, auth=self.auth,
                reauth_statuses=REAUTH_STATUSES, expected=expected, headers=headers, timeout=VSHIP_TIMEOUT,
                **kwargs,
            )

    async def _get_json(self, path: str, **kwargs: Any) -> Any:
        return json_body(await self._call("GET", path, **kwargs), service=SERVICE)

    @staticmethod
    def _single_flight(
        cache: dict[str, asyncio.Future[T]],
        key: str,
        factory: Callable[[], Coroutine[Any, Any, T]],
    ) -> asyncio.Future[T]:
        """Return a shielded view of the one task computing `key` (created on first use)."""
        fut = cache.get(key)
        if fut is None or fut.cancelled():
            fut = asyncio.get_running_loop().create_task(factory())
            cache[key] = fut
        return asyncio.shield(fut)

    def _not_found_message(self, key: str, records: list[dict[str, Any]]) -> str:
        if not key:
            return "No VShip booking matches '' (empty booking number)"
        ranked = sorted(records, key=lambda r: match_rank(_booking_no(r), key))
        shown = ", ".join(repr(_booking_no(r)) for r in ranked[:CANDIDATES_SHOWN]) or "none"
        return f"No VShip booking matches {key!r} (candidates: {shown})"

    async def _resolve_booking_id(self, key: str) -> str:
        if not key:  # an empty search would list unrelated bookings; never pick one
            raise VShipBookingNotFoundError(SERVICE, self._not_found_message(key, []))
        records = await self.search_bookings(key)
        best = best_match(records, key)
        if best is None:  # D17
            raise VShipBookingNotFoundError(SERVICE, self._not_found_message(key, records))
        booking_id = _booking_id_of(best)
        self._base_by_id.setdefault(booking_id, key)
        log.debug("[VShip] %s -> bookingId %s (bookingNo %r, rank %s)", key, booking_id, _booking_no(best),
                  match_rank(_booking_no(best), key)[:2])
        return booking_id

    async def _fetch_comments(self, booking_id: str) -> list[dict[str, Any]]:
        return _internal_comments(await self._get_json(f"/Bookings/{_path_id(booking_id)}/comments"))


# ════════════════════════════════════════════════════════════════════════════════════════════════
# factory
# ════════════════════════════════════════════════════════════════════════════════════════════════
def build_vship(
    settings: Settings,
    token_store: TokenStore,
    *,
    read_only: bool = False,
    http: httpx.AsyncClient | None = None,
) -> VShipClient:
    """VShipAuth + VShipClient sharing one HTTP client (pipeline `build_services`, CLI probe).

    The returned client owns the HTTP client it created (closed by `aclose()`); an injected `http` is left
    to its owner. `client.auth` is the VShipAuth (Phase 0 preflight: `await client.auth.get_token()`).
    Raises ConfigError when VShip is not configured (VSHIP_USER_NAME / VSHIP_PASSWORD missing).
    """
    vship_settings = settings.vship
    if vship_settings is None:
        names = ", ".join(settings.missing.get("vship", ())) or "VSHIP_USER_NAME, VSHIP_PASSWORD"
        raise ConfigError(f"VShip is not configured: set {names} in .env (see .env.example).")
    shared = http if http is not None else new_async_client(timeout=VSHIP_TIMEOUT)
    auth = VShipAuth(vship_settings, token_store, http=shared)
    client = VShipClient(vship_settings, auth, http=shared, read_only=read_only)
    client._owns_http = http is None  # the client closes the shared client it was handed by this factory
    return client
