"""Grimaldi stub — 09 §C.3 tests 1–2, plus the factory / CarrierClient contract (03 §9).

Grimaldi has no public API (09 §C.1), so the client never makes an HTTP request: every httpx send is
patched to fail, which proves it.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from eta_automations.carriers import grimaldi
from eta_automations.carriers.base import CarrierClient
from eta_automations.carriers.grimaldi import GRIMALDI_NOT_IMPLEMENTED, GrimaldiClient, build_client
from eta_automations.errors import CarrierLookupError, UnsupportedCarrierError
from eta_automations.models import Carrier

BOOKING = "S312345678"
EXPECTED_TEXT = "Grimaldi ETA lookup not yet implemented for booking S312345678"


@pytest.fixture
def no_http(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Any httpx request (sync or async) fails the test; returns the list of attempted requests."""
    attempted: list[Any] = []

    async def async_send(self: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> Any:
        attempted.append(request)
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    def sync_send(self: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> Any:
        attempted.append(request)
        raise AssertionError(f"unexpected HTTP request: {request.method} {request.url}")

    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    monkeypatch.setattr(httpx.Client, "send", sync_send)
    return attempted


# ── 09 §C.3 test 1 ──
async def test_get_eta_raises_unsupported_with_exact_original_text(no_http: list[Any]) -> None:
    client = GrimaldiClient()
    with pytest.raises(UnsupportedCarrierError) as info:
        await client.get_eta(BOOKING)
    assert str(info.value) == EXPECTED_TEXT
    assert isinstance(info.value, CarrierLookupError)  # 01 §6.1: "Carrier Lookup Failed."
    assert no_http == []


# ── 09 §C.3 test 2 ──
async def test_probe_returns_same_text_and_makes_no_http_call(no_http: list[Any]) -> None:
    out = await GrimaldiClient().probe(BOOKING)
    assert out == {
        "requests": [],
        "raw": None,
        "eta": EXPECTED_TEXT,
        "note": "No Grimaldi API; see 09 Part C. Use ETA_PROVIDER_GRIMALDI=aggregator if your aggregator covers it.",
    }
    assert no_http == []
    json.dumps(out)  # JSON-serializable probe output (03 §9)


def test_message_template_is_the_original_text() -> None:
    assert GRIMALDI_NOT_IMPLEMENTED == "Grimaldi ETA lookup not yet implemented for booking {booking}"
    assert GRIMALDI_NOT_IMPLEMENTED.format(booking=BOOKING) == EXPECTED_TEXT


async def test_build_client_always_configured(make_settings: Callable[..., Any], token_store: Any,
                                              no_http: list[Any]) -> None:
    # Grimaldi needs no settings at all (02 §2.6): even with nothing configured it returns the stub.
    settings = make_settings({"QB_CLIENT_ID": None, "VSHIP_PASSWORD": None})
    client = build_client(settings, token_store)
    assert isinstance(client, GrimaldiClient)
    assert isinstance(client, CarrierClient)
    assert client.carrier is Carrier.GRIMALDI
    with pytest.raises(UnsupportedCarrierError,
                       match="^Grimaldi ETA lookup not yet implemented for booking S399999999$"):
        await client.get_eta("S399999999")
    assert await client.aclose() is None


async def test_injected_http_client_is_never_used_or_closed(make_settings: Callable[..., Any],
                                                            token_store: Any) -> None:
    async with httpx.AsyncClient() as http:
        client = grimaldi.build_client(make_settings(), token_store, clock=None, http=http)
        await client.aclose()
        assert not http.is_closed
        with pytest.raises(UnsupportedCarrierError):
            await client.get_eta(BOOKING)
