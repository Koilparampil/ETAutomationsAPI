"""qbo/client.py — lookups, snapshots, ETA field discovery, sparse updates, helpers (04-quickbooks-online §9.2
tests 10–25, plus edge cases). respx-mocked; no network."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from eta_automations.config import Settings
from eta_automations.errors import (
    AmbiguousInvoiceError,
    AuthError,
    ConfigError,
    InvoiceNotFoundError,
    InvoiceSendError,
    InvoiceUpdateError,
    ServiceError,
)
from eta_automations.http import AsyncLimiter
from eta_automations.qbo.client import (
    CANNOT_DETERMINE_ETA_FIELD,
    READ_ONLY_MESSAGE,
    EtaFieldSpec,
    InvoiceSnapshot,
    MultiLimiter,
    QboClient,
    QboFault,
    QboFaultError,
    ambiguous_invoice_error,
    build_qbo,
    doc_number_problem,
    parse_faults,
    parse_qbo_date,
    split_addresses,
)
from eta_automations.token_store import TokenStore

REALM = "1234567890"
API = "https://quickbooks.api.intuit.com"
COMPANY = f"{API}/v3/company/{REALM}"
QUERY_URL = f"{COMPANY}/query"
INVOICE_URL = f"{COMPANY}/invoice"
ETA_SPEC = EtaFieldSpec("1000000003", "ETA", "DateType", "DateValue", "invoice 190613")
FIXTURE_DOCS = ["EBKG12345678", "EBKG23456789 A", "267546627", "NAM1234567", "EBKG99999999"]


class StaticAuth:
    """TokenProvider stand-in: a fixed access token (QboAuth itself is covered by test_qbo_auth.py)."""

    def __init__(self, token: str = "AT-TEST") -> None:
        self.token = token
        self.invalidations = 0
        self.closed = False

    async def get_token(self) -> str:
        return self.token

    async def invalidate(self) -> None:
        self.invalidations += 1

    async def aclose(self) -> None:
        self.closed = True


def make_client(settings: Settings, **kwargs: Any) -> QboClient:
    assert settings.qbo is not None
    return QboClient(settings.qbo, StaticAuth(), **kwargs)


def invoices(load_fixture: Callable[[str], Any]) -> list[dict[str, Any]]:
    return copy.deepcopy(load_fixture("qbo/query_invoices.json")["QueryResponse"]["Invoice"])


def invoice_190613(load_fixture: Callable[[str], Any]) -> dict[str, Any]:
    return invoices(load_fixture)[0]


def snapshot(raw: dict[str, Any], spec: EtaFieldSpec | None = ETA_SPEC) -> InvoiceSnapshot:
    return InvoiceSnapshot.from_api(raw, spec, string_format="%m/%d/%Y")


def query_response(*raws: dict[str, Any]) -> httpx.Response:
    body: dict[str, Any] = {"QueryResponse": {"Invoice": list(raws), "startPosition": 1, "maxResults": len(raws)}
                            if raws else {}, "time": "t"}
    return httpx.Response(200, json=body)


def invoice_response(raw: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"Invoice": raw, "time": "t"})


def fault(code: str, message: str = "Validation error", detail: str = "") -> dict[str, Any]:
    return {"Fault": {"Error": [{"Message": message, "Detail": detail or message, "code": code, "element": ""}],
                      "type": "ValidationFault"}, "time": "t"}


def body_of(call: Any) -> dict[str, Any]:
    return json.loads(call.request.content)


# ── pure helpers ──
@pytest.mark.parametrize(("value", "expected"), [
    ("2026-11-04", date(2026, 11, 4)),
    ("2026-11-04T23:30:00-08:00", date(2026, 11, 4)),
    ("2026-11-04T23:30:00", date(2026, 11, 4)),
    ("11/04/2026", date(2026, 11, 4)),
    ("1/4/2026", date(2026, 1, 4)),
    (" 2026-11-04 ", date(2026, 11, 4)),
    ("2026-02-30", None),
    ("2026/11/04", None),
    ("11/4/26", None),
    ("", None),
    (None, None),
    (20261104, None),
])
def test_parse_qbo_date(value: Any, expected: date | None) -> None:
    assert parse_qbo_date(value) == expected


def test_split_addresses_and_doc_number_problem(make_settings) -> None:
    assert split_addresses("ap@acme.example, ops@acme.example; AP@acme.example ;; ") == (
        "ap@acme.example", "ops@acme.example")
    assert split_addresses(None) == () and split_addresses("") == ()
    qbo = make_settings().qbo
    assert doc_number_problem("EBKG12345678 AA", qbo) is None
    assert doc_number_problem("X" * 21, qbo) is None
    assert doc_number_problem("ABCDEFGHIJKLMNOPQRSTU A", qbo) == (
        "New invoice number 'ABCDEFGHIJKLMNOPQRSTU A' is 23 characters; QuickBooks allows at most 21 "
        "(QB_DOCNUMBER_MAX_LEN)")
    assert doc_number_problem("X" * 22, make_settings(QB_DOCNUMBER_MAX_LEN="22").qbo) is None


def test_parse_faults_tolerates_key_case_and_missing_bodies(load_fixture) -> None:
    assert parse_faults(load_fixture("qbo/fault_auth_401.json")) == [
        QboFault("3200", "message=AuthenticationFailed; errorCode=003200; statusCode=401", "Token expired")]
    stale = parse_faults(load_fixture("qbo/fault_stale_5010.json"))
    assert [f.code for f in stale] == ["5010"] and stale[0].message == "Stale Object Error"
    assert parse_faults({"QueryResponse": {}}) == []
    assert parse_faults(None) == [] and parse_faults("text") == []
    assert parse_faults({"Fault": {"type": "SystemFault"}}) == [QboFault("", "QuickBooks fault (SystemFault)", "")]
    assert str(QboFault("6000", "Business validation error", "")) == "6000 Business validation error"


# ── test 13 + snapshot parsing ──
@pytest.mark.parametrize(("total", "balance", "paid", "partial"), [
    (100, 0, True, False),
    (100, 40, False, True),
    (100, 100, False, False),
    (0, 0, True, False),
])
def test_is_paid_and_is_partially_paid_truth_table(total: int, balance: int, paid: bool, partial: bool) -> None:
    inv = snapshot({"Id": "1", "SyncToken": "0", "DocNumber": "D", "TotalAmt": total, "Balance": balance})
    assert inv.is_paid is paid
    assert inv.is_partially_paid is partial


def test_snapshot_from_fixture_fields(load_fixture) -> None:
    raws = invoices(load_fixture)
    a = snapshot(raws[0])
    assert (a.id, a.sync_token, a.doc_number) == ("190613", "7", "EBKG12345678")
    assert a.total_amount == Decimal("2450.0") and a.balance == Decimal("2450.0")
    assert a.customer_name == "Acme Imports LLC"
    assert a.bill_email == "ap@acme.example, ops@acme.example"
    assert a.recipients == ("ap@acme.example", "ops@acme.example")
    assert a.bill_email_cc == "cc@acme.example" and a.bill_email_bcc is None
    assert a.email_status == "NeedToSend"
    assert a.due_date == date(2026, 11, 19)
    assert a.eta_value == date(2026, 11, 2)
    assert a.delivery_time is None
    assert len(a.custom_fields) == 2 and a.raw is raws[0]
    b = snapshot(raws[1])
    assert b.delivery_time == "2026-10-30T08:00:00-07:00" and b.email_status == "EmailSent"
    assert b.eta_value is None and b.due_date is None
    assert snapshot(raws[0], None).eta_value is None     # no known field → no value


def test_missing_balance_is_treated_as_unpaid_with_warning(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    inv = snapshot({"Id": "9", "SyncToken": "0", "DocNumber": "D9", "TotalAmt": "250.00",
                    "BillEmail": {"Address": "  "}})
    assert inv.balance == Decimal("250.00") and not inv.is_paid and not inv.is_partially_paid
    assert inv.bill_email is None and inv.recipients == ()
    assert any("has no usable Balance" in m for m in caplog.messages)


# ── test 10 ──
async def test_query_text_chunking_quoting_and_params(make_settings) -> None:
    client = make_client(make_settings(QB_QUERY_CHUNK_SIZE="2"))
    with respx.mock() as router:
        route = router.get(QUERY_URL).mock(return_value=query_response())
        found = await client.find_invoices_by_doc_numbers(["EBKG1", "O'Neil 1", "EBKG1", " ", "", "C3", "D4", " E5 "])
    assert found == {}
    assert route.call_count == 3
    assert sorted(c.request.url.params["query"] for c in route.calls) == sorted([
        "SELECT * FROM Invoice WHERE DocNumber IN ('EBKG1', 'O\\'Neil 1') MAXRESULTS 1000",
        "SELECT * FROM Invoice WHERE DocNumber IN ('C3', 'D4') MAXRESULTS 1000",
        "SELECT * FROM Invoice WHERE DocNumber IN ('E5') MAXRESULTS 1000",
    ])
    for call in route.calls:
        params = call.request.url.params
        assert params["include"] == "enhancedAllCustomFields"
        assert params["minorversion"] == "75"
        assert call.request.headers["Authorization"] == "Bearer AT-TEST"
        assert call.request.headers["Accept"] == "application/json"
    await client.aclose()


async def test_empty_input_makes_no_call(make_settings) -> None:
    client = make_client(make_settings())
    with respx.mock() as router:
        assert await client.find_invoices_by_doc_numbers(["", "  "]) == {}
        assert router.calls.call_count == 0


# ── test 11 ──
async def test_fixture_exact_matches_and_ambiguous_exclusion(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    with respx.mock() as router:
        route = router.get(QUERY_URL).mock(return_value=httpx.Response(200, json=load_fixture("qbo/query_invoices.json")))
        found = await client.find_invoices_by_doc_numbers(FIXTURE_DOCS)
    assert route.call_count == 1
    assert set(found) == {"EBKG12345678", "EBKG23456789 A", "267546627"}
    a = found["EBKG12345678"]
    assert a.id == "190613" and a.is_paid is False and a.is_partially_paid is False
    assert a.eta_value == date(2026, 11, 2)
    assert a.recipients == ("ap@acme.example", "ops@acme.example")
    b = found["EBKG23456789 A"]
    assert b.is_partially_paid is True and b.eta_value is None
    c = found["267546627"]
    assert c.is_paid is True and c.eta_value is None
    assert client.ambiguous == {"NAM1234567": ("190900", "190901")}
    assert "EBKG99999999" not in client.ambiguous and "EBKG99999999" not in found
    assert client.eta_field == EtaFieldSpec("1000000003", "ETA", "DateType", "DateValue", "invoice 190613")


async def test_case_insensitive_single_fallback(make_settings, load_fixture, caplog) -> None:
    caplog.set_level(logging.INFO, logger="eta")
    client = make_client(make_settings())
    raws = invoices(load_fixture)
    twin = dict(raws[1], Id="190777", DocNumber="ebkg23456789 a")   # differs only by case
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=query_response(raws[0], raws[1], twin))
        found = await client.find_invoices_by_doc_numbers(["ebkg12345678", "Ebkg23456789 A"])
    assert found["ebkg12345678"].id == "190613"
    assert any("case-insensitive DocNumber match" in m for m in caplog.messages)
    assert "Ebkg23456789 A" not in found and "Ebkg23456789 A" not in client.ambiguous   # 2 ci matches: not used


async def test_get_invoice_by_doc_number_errors(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=httpx.Response(200, json=load_fixture("qbo/query_invoices.json")))
        assert (await client.get_invoice_by_doc_number(" EBKG12345678 ")).id == "190613"
        with pytest.raises(AmbiguousInvoiceError) as ambiguous:
            await client.get_invoice_by_doc_number("NAM1234567")
        with pytest.raises(InvoiceNotFoundError) as missing:
            await client.get_invoice_by_doc_number("EBKG99999999")
    assert str(ambiguous.value) == ("2 QuickBooks invoices share DocNumber 'NAM1234567' (Ids 190900, 190901) — "
                                    "fix in QBO")
    assert isinstance(ambiguous.value, InvoiceNotFoundError)
    assert str(ambiguous_invoice_error("NAM1234567", client.ambiguous["NAM1234567"])) == str(ambiguous.value)
    assert str(missing.value) == "#EBKG99999999 not found in QuickBooks"
    assert not isinstance(missing.value, AmbiguousInvoiceError)


# ── test 12 ──
async def test_400_on_in_query_falls_back_to_single_queries(make_settings, load_fixture, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    client = make_client(make_settings())
    raw = invoice_190613(load_fixture)

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params["query"]
        if " IN (" in query:
            return httpx.Response(400, json=fault("4000", "Error parsing query", "QueryParserError"))
        if "'EBKG12345678'" in query:
            return query_response(raw)
        if "BAD" in query:
            return httpx.Response(400, json=fault("4000", "Error parsing query"))
        return query_response()

    with respx.mock() as router:
        route = router.get(QUERY_URL).mock(side_effect=handler)
        found = await client.find_invoices_by_doc_numbers(["EBKG12345678", "BAD\\VALUE", "EBKG00000000"])
    queries = [c.request.url.params["query"] for c in route.calls]
    assert queries[0] == ("SELECT * FROM Invoice WHERE DocNumber IN ('EBKG12345678', 'BAD\\VALUE', "
                          "'EBKG00000000') MAXRESULTS 1000")
    assert sorted(queries[1:]) == sorted([
        "SELECT * FROM Invoice WHERE DocNumber = 'EBKG12345678' MAXRESULTS 1000",
        "SELECT * FROM Invoice WHERE DocNumber = 'BAD\\VALUE' MAXRESULTS 1000",
        "SELECT * FROM Invoice WHERE DocNumber = 'EBKG00000000' MAXRESULTS 1000",
    ])
    assert list(found) == ["EBKG12345678"]
    assert client.ambiguous == {}
    assert any("one at a time" in m for m in caplog.messages)
    assert any("treating it as not found" in m for m in caplog.messages)


async def test_200_with_fault_is_an_error_and_single_chunk_fault_means_absent(make_settings) -> None:
    client = make_client(make_settings())
    with respx.mock() as router:
        route = router.get(QUERY_URL).mock(return_value=httpx.Response(200, json=fault("4001", "Invalid query")))
        assert await client.find_invoices_by_doc_numbers(["ONLY1"]) == {}
    assert route.call_count == 1      # a single-value chunk has nothing to fall back to


async def test_network_failure_of_a_chunk_propagates(make_settings, http_sleeps) -> None:
    client = make_client(make_settings(QB_QUERY_CHUNK_SIZE="1"))
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=httpx.Response(500, text="boom"))
        with pytest.raises(ServiceError) as info:
            await client.find_invoices_by_doc_numbers(["A1", "B2"])
    assert info.value.status == 500


async def test_pagination_with_startposition(make_settings) -> None:
    client = make_client(make_settings())
    page1 = [{"Id": str(i), "SyncToken": "0", "DocNumber": f"BULK{i}", "TotalAmt": 1, "Balance": 1}
             for i in range(1000)]
    page2 = [{"Id": "5000", "SyncToken": "0", "DocNumber": "WANTED", "TotalAmt": 1, "Balance": 1}]
    with respx.mock() as router:
        route = router.get(QUERY_URL).mock(side_effect=[query_response(*page1), query_response(*page2)])
        found = await client.find_invoices_by_doc_numbers(["WANTED"])
    assert [c.request.url.params["query"] for c in route.calls] == [
        "SELECT * FROM Invoice WHERE DocNumber IN ('WANTED') MAXRESULTS 1000",
        "SELECT * FROM Invoice WHERE DocNumber IN ('WANTED') STARTPOSITION 1001 MAXRESULTS 1000",
    ]
    assert found["WANTED"].id == "5000"


# ── test 14 ──
async def test_eta_field_resolution_by_configured_definition_id(make_settings, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    client = make_client(make_settings(QB_ETA_DEFINITION_ID="1000000003"))
    raw = {"Id": "1", "SyncToken": "0", "DocNumber": "D1", "TotalAmt": 1, "Balance": 1, "CustomField": [
        {"DefinitionId": "1000000005", "Name": "ETA", "Type": "DateType", "DateValue": "2026-12-25"},
        {"DefinitionId": "1000000003", "Name": "Arrival (ETA)", "Type": "DateType", "DateValue": "2026-11-02"}]}
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=query_response(raw))
        found = await client.find_invoices_by_doc_numbers(["D1"])
    assert found["D1"].eta_value == date(2026, 11, 2)
    assert client.eta_field == EtaFieldSpec("1000000003", "Arrival (ETA)", "DateType", "DateValue", "invoice 1")
    assert not caplog.messages


async def test_eta_field_resolution_by_name_warns_with_the_correct_id(make_settings, load_fixture, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    client = make_client(make_settings(QB_ETA_DEFINITION_ID="20000000000000150677"))
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=httpx.Response(200, json=load_fixture("qbo/query_invoices.json")))
        found = await client.find_invoices_by_doc_numbers(FIXTURE_DOCS)
    assert found["EBKG12345678"].eta_value == date(2026, 11, 2)
    expected = ("QB_ETA_DEFINITION_ID=20000000000000150677 was not found; QuickBooks reports field 'ETA' as "
                "DefinitionId 1000000003 (Type DateType). Set QB_ETA_DEFINITION_ID=1000000003 in .env.")
    assert caplog.messages.count(expected) == 1


async def test_inactive_eta_field_is_ignored(make_settings) -> None:
    client = make_client(make_settings())
    inactive = {"DefinitionId": "1000000009", "Name": "ETA (inactive)", "Type": "DateType", "DateValue": "2025-01-01"}
    active = {"DefinitionId": "1000000003", "Name": " eta ", "Type": "DateType", "DateValue": "2026-11-02"}
    both = {"Id": "1", "SyncToken": "0", "DocNumber": "D1", "TotalAmt": 1, "Balance": 1,
            "CustomField": [inactive, active]}
    only_inactive = {"Id": "2", "SyncToken": "0", "DocNumber": "D2", "TotalAmt": 1, "Balance": 1,
                     "CustomField": [inactive]}
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=query_response(only_inactive, both))
        found = await client.find_invoices_by_doc_numbers(["D1", "D2"])
    assert found["D1"].eta_value == date(2026, 11, 2)
    assert client.eta_field is not None and client.eta_field.definition_id == "1000000003"
    assert client._match_eta_entry([inactive]) is None


async def test_eta_field_config_fallback_when_nothing_observed(make_settings, load_fixture, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    client = make_client(make_settings(QB_ETA_DEFINITION_ID="1000000003"))
    raw = invoices(load_fixture)[1]          # 190700: no ETA entry; nothing learned yet
    inv = snapshot(raw)
    updated_raw = dict(raw, SyncToken="3", CustomField=raw["CustomField"] + [
        {"DefinitionId": "1000000003", "Name": "ETA", "Type": "DateType", "DateValue": "2026-11-04"}])
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(return_value=invoice_response(updated_raw))
        result = await client.update_invoice(inv, eta=date(2026, 11, 4))
    assert body_of(post.calls.last)["CustomField"] == [
        {"DefinitionId": "1", "Name": "Container No.", "Type": "StringType", "StringValue": "MSCU7654321"},
        {"DefinitionId": "1000000003", "Name": "ETA", "Type": "DateType", "DateValue": "2026-11-04"}]
    assert result.eta_value == date(2026, 11, 4)
    assert "ETA field shape not observed on any invoice; using QB_ETA_FIELD_TYPE=DateType (UNVERIFIED)" in caplog.messages


async def test_eta_field_unknown_raises_before_any_call(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoices(load_fixture)[1])
    with respx.mock() as router:
        with pytest.raises(InvoiceUpdateError) as info:
            await client.update_invoice(inv, eta=date(2026, 11, 4))
        assert router.calls.call_count == 0
    assert str(info.value) == CANNOT_DETERMINE_ETA_FIELD


async def test_number_type_eta_field_is_rejected(make_settings) -> None:
    client = make_client(make_settings())
    raw = {"Id": "1", "SyncToken": "0", "DocNumber": "D1", "TotalAmt": 1, "Balance": 1,
           "CustomField": [{"DefinitionId": "7", "Name": "ETA", "Type": "NumberType", "NumberValue": 20261104}]}
    with respx.mock() as router, pytest.raises(InvoiceUpdateError, match="unsupported type NumberType"):
        await client.update_invoice(snapshot(raw), eta=date(2026, 11, 4))
    assert router.calls.call_count == 0


# ── test 15 ──
async def test_update_body_is_sparse_with_full_custom_field_array(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/update_ok.json")))
        updated = await client.update_invoice(inv, eta=date(2026, 11, 4), doc_number="EBKG12345678 A")
    assert post.call_count == 1
    assert body_of(post.calls.last) == {
        "Id": "190613", "SyncToken": "7", "sparse": True, "DocNumber": "EBKG12345678 A",
        "CustomField": [
            {"DefinitionId": "1", "Name": "Container No.", "Type": "StringType", "StringValue": "MSCU1234567"},
            {"DefinitionId": "1000000003", "Name": "ETA", "Type": "DateType", "DateValue": "2026-11-04"}]}
    params = post.calls.last.request.url.params
    assert re.fullmatch(r"[0-9a-f]{32}", params["requestid"])
    assert params["include"] == "enhancedAllCustomFields" and params["minorversion"] == "75"
    assert post.calls.last.request.headers["Content-Type"] == "application/json"
    assert (updated.sync_token, updated.doc_number, updated.eta_value) == ("8", "EBKG12345678 A", date(2026, 11, 4))


async def test_update_eta_only_or_doc_only(make_settings, load_fixture) -> None:
    client = make_client(make_settings(QB_INCLUDE_ON_WRITES="false"))
    inv = snapshot(invoice_190613(load_fixture))
    eta_only = dict(invoice_190613(load_fixture), SyncToken="8")
    eta_only["CustomField"][1]["DateValue"] = "2026-11-04"
    doc_only = dict(invoice_190613(load_fixture), SyncToken="8", DocNumber="EBKG12345678 A")
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(side_effect=[invoice_response(eta_only), invoice_response(doc_only)])
        await client.update_invoice(inv, eta=date(2026, 11, 4), doc_number="EBKG12345678")   # same doc: no change
        await client.update_invoice(inv, eta=date(2026, 11, 2), doc_number="EBKG12345678 A")  # same ETA
    first, second = (body_of(c) for c in post.calls)
    assert "DocNumber" not in first and first["CustomField"][-1]["DateValue"] == "2026-11-04"
    assert second == {"Id": "190613", "SyncToken": "7", "sparse": True, "DocNumber": "EBKG12345678 A"}
    assert "include" not in post.calls.last.request.url.params
    assert post.calls[0].request.url.params["requestid"] != post.calls[1].request.url.params["requestid"]


# ── test 16 ──
async def test_no_op_update_makes_zero_http_calls(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        assert await client.update_invoice(inv) is inv
        assert await client.update_invoice(inv, eta=date(2026, 11, 2), doc_number="EBKG12345678") is inv
        assert await client.update_invoice(inv, eta=date(2026, 11, 2)) is inv
        assert router.calls.call_count == 0


# ── test 17 ──
async def test_stale_object_rereads_and_retries_once_with_new_requestid(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    fresh = dict(invoice_190613(load_fixture), SyncToken="8")
    done = dict(load_fixture("qbo/update_ok.json")["Invoice"], SyncToken="9")
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(side_effect=[
            httpx.Response(400, json=load_fixture("qbo/fault_stale_5010.json")), invoice_response(done)])
        get = router.get(f"{INVOICE_URL}/190613").mock(return_value=invoice_response(fresh))
        updated = await client.update_invoice(inv, eta=date(2026, 11, 4), doc_number="EBKG12345678 A")
    assert get.call_count == 1 and post.call_count == 2
    first, second = post.calls
    assert first.request.url.params["requestid"] != second.request.url.params["requestid"]
    assert body_of(first)["SyncToken"] == "7" and body_of(second)["SyncToken"] == "8"
    assert body_of(second)["DocNumber"] == "EBKG12345678 A"
    assert get.calls.last.request.url.params["include"] == "enhancedAllCustomFields"
    assert updated.sync_token == "9"


async def test_stale_object_twice_raises(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        router.post(INVOICE_URL).mock(return_value=httpx.Response(400, json=load_fixture("qbo/fault_stale_5010.json"),
                                                                  headers={"intuit_tid": "tid-55"}))
        router.get(f"{INVOICE_URL}/190613").mock(return_value=invoice_response(invoice_190613(load_fixture)))
        with pytest.raises(InvoiceUpdateError) as info:
            await client.update_invoice(inv, eta=date(2026, 11, 4))
    assert "stale object twice — someone is editing invoice 190613" in str(info.value)
    assert info.value.request_id == "tid-55" and "[request_id=tid-55]" in str(info.value)


# ── test 18 ──
async def test_stale_object_with_concurrent_doc_number_change(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    renamed = dict(invoice_190613(load_fixture), SyncToken="8", DocNumber="EBKG12345678 B")
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(return_value=httpx.Response(400, json=load_fixture(
            "qbo/fault_stale_5010.json")))
        router.get(f"{INVOICE_URL}/190613").mock(return_value=invoice_response(renamed))
        with pytest.raises(InvoiceUpdateError) as info:
            await client.update_invoice(inv, eta=date(2026, 11, 4), doc_number="EBKG12345678 A")
    assert post.call_count == 1
    assert str(info.value) == ("Invoice 190613 DocNumber changed in QuickBooks while processing ('EBKG12345678' → "
                               "'EBKG12345678 B'); not overwriting")


# ── test 19 ──
async def test_duplicate_doc_number_6140(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        router.post(INVOICE_URL).mock(return_value=httpx.Response(
            400, json=load_fixture("qbo/fault_duplicate_6140.json"), headers={"intuit_tid": "tid-6140"}))
        with pytest.raises(InvoiceUpdateError) as info:
            await client.update_invoice(inv, eta=date(2026, 11, 4), doc_number="EBKG12345678 A")
    assert "DocNumber 'EBKG12345678 A' is already used in QuickBooks (duplicate document number)" in str(info.value)
    assert info.value.request_id == "tid-6140"


async def test_other_update_faults(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        route = router.post(INVOICE_URL).mock(return_value=httpx.Response(
            400, json=fault("6000", "A business validation error has occurred", "Bad date"),
            headers={"intuit_tid": "tid-6000"}))
        with pytest.raises(InvoiceUpdateError) as info:
            await client.update_invoice(inv, eta=date(2026, 11, 4))
        assert str(info.value) == ("qbo: update rejected: 6000 A business validation error has occurred — Bad date "
                                   "(HTTP 400) [request_id=tid-6000]")
        route.mock(return_value=httpx.Response(400, json=fault("610", "Object Not Found")))
        with pytest.raises(InvoiceNotFoundError, match="Invoice 190613 no longer exists in QuickBooks"):
            await client.update_invoice(inv, eta=date(2026, 11, 4))


# ── test 20 ──
async def test_verification_get_detects_an_eta_that_did_not_persist(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    answer = dict(invoice_190613(load_fixture), SyncToken="8")
    answer["CustomField"] = [answer["CustomField"][0]]          # POST answer lacks the ETA value
    still_old = dict(invoice_190613(load_fixture), SyncToken="8")
    with respx.mock() as router:
        router.post(INVOICE_URL).mock(return_value=invoice_response(answer))
        get = router.get(f"{INVOICE_URL}/190613").mock(return_value=invoice_response(still_old))
        with pytest.raises(InvoiceUpdateError) as info:
            await client.update_invoice(inv, eta=date(2026, 11, 4))
    assert get.call_count == 1
    assert str(info.value) == ("QuickBooks accepted the update but the ETA custom field did not change on invoice "
                               "190613 (still 2026-11-02). The API may be unable to write this field — see 04 §8.")


async def test_verification_get_confirms_the_new_eta(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    answer = dict(invoice_190613(load_fixture), SyncToken="8")
    answer["CustomField"] = [answer["CustomField"][0]]
    stored = load_fixture("qbo/update_ok.json")["Invoice"]
    with respx.mock() as router:
        router.post(INVOICE_URL).mock(return_value=invoice_response(answer))
        router.get(f"{INVOICE_URL}/190613").mock(return_value=invoice_response(stored))
        updated = await client.update_invoice(inv, eta=date(2026, 11, 4))
    assert updated.eta_value == date(2026, 11, 4) and updated.sync_token == "8"


async def test_verification_of_a_truncated_doc_number_and_verify_off(make_settings, load_fixture) -> None:
    inv = snapshot(invoice_190613(load_fixture))
    truncated = dict(load_fixture("qbo/update_ok.json")["Invoice"], DocNumber="EBKG12345678")
    with respx.mock() as router:
        router.post(INVOICE_URL).mock(return_value=invoice_response(truncated))
        with pytest.raises(InvoiceUpdateError, match=r"stored DocNumber 'EBKG12345678' instead of 'EBKG12345678 A' "
                                                     r"\(length limit\?\)"):
            await make_client(make_settings()).update_invoice(inv, doc_number="EBKG12345678 A")
        unverified = await make_client(make_settings(QB_VERIFY_UPDATES="false")).update_invoice(
            inv, doc_number="EBKG12345678 A")
    assert unverified.doc_number == "EBKG12345678"


# ── test 21 ──
async def test_transport_retry_reuses_the_same_requestid(make_settings, load_fixture, http_sleeps) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=load_fixture("qbo/update_ok.json"))])
        updated = await client.update_invoice(inv, eta=date(2026, 11, 4), doc_number="EBKG12345678 A")
    assert post.call_count == 2
    ids = [c.request.url.params["requestid"] for c in post.calls]
    assert ids[0] == ids[1]
    assert body_of(post.calls[0]) == body_of(post.calls[1])
    assert len(http_sleeps) == 1
    assert updated.doc_number == "EBKG12345678 A"


async def test_update_network_failure_is_an_invoice_update_error(make_settings, load_fixture, http_sleeps) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(return_value=httpx.Response(502, text="bad gateway"))
        with pytest.raises(InvoiceUpdateError) as info:
            await client.update_invoice(inv, eta=date(2026, 11, 4))
    assert post.call_count == 5            # idempotent thanks to requestid → retried (max_attempts=5)
    assert info.value.status == 502


# ── test 22 ──
async def test_string_type_field_is_written_as_formatted_string_value(make_settings) -> None:
    client = make_client(make_settings())
    raw = {"Id": "300", "SyncToken": "4", "DocNumber": "S300", "TotalAmt": 10, "Balance": 10, "CustomField": [
        {"DefinitionId": "2", "Name": "ETA", "Type": "StringType", "StringValue": "10/30/2026"}]}
    inv = client._snapshot(raw)
    assert inv.eta_value == date(2026, 10, 30)
    answer = dict(raw, SyncToken="5", CustomField=[
        {"DefinitionId": "2", "Name": "ETA", "Type": "StringType", "StringValue": "11/04/2026"}])
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(return_value=invoice_response(answer))
        updated = await client.update_invoice(inv, eta=date(2026, 11, 4))
    assert body_of(post.calls.last)["CustomField"] == [
        {"DefinitionId": "2", "Name": "ETA", "Type": "StringType", "StringValue": "11/04/2026"}]
    assert updated.eta_value == date(2026, 11, 4)


async def test_string_format_setting_is_used_for_reading_and_writing(make_settings) -> None:
    client = make_client(make_settings(QB_ETA_STRING_FORMAT="%d.%m.%Y"))
    raw = {"Id": "301", "SyncToken": "1", "DocNumber": "S301", "TotalAmt": 10, "Balance": 10, "CustomField": [
        {"DefinitionId": "3", "Name": "ETA", "Type": "StringType", "StringValue": "02.11.2026"}]}
    inv = client._snapshot(raw)
    assert inv.eta_value == date(2026, 11, 2)
    answer = dict(raw, CustomField=[{"DefinitionId": "3", "Name": "ETA", "Type": "StringType",
                                     "StringValue": "04.11.2026"}])
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(return_value=invoice_response(answer))
        await client.update_invoice(inv, eta=date(2026, 11, 4))
    assert body_of(post.calls.last)["CustomField"][0]["StringValue"] == "04.11.2026"


# ── test 23 ──
async def test_read_only_client_refuses_writes_without_http(make_settings, load_fixture) -> None:
    client = make_client(make_settings(), read_only=True)
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        for call in (client.update_invoice(inv, eta=date(2026, 11, 4)),
                     client.update_invoice(inv),
                     client.send_invoice(inv, subject="Invoice for Booking no. X. ***Y***"),
                     client.mark_email_sent(inv),
                     client.update_preferences({"Id": "1"}),
                     client._post_send("190613", None)):
            with pytest.raises(RuntimeError) as info:
                await call
            assert str(info.value) == READ_ONLY_MESSAGE
        assert router.calls.call_count == 0
    assert client.read_only is True


# ── test 24 ──
async def test_list_custom_fields_marks_the_match_and_resolves_the_spec(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=query_response(invoice_190613(load_fixture)))
        rows = await client.list_custom_fields("EBKG12345678")
    assert rows == [
        {"DefinitionId": "1", "Name": "Container No.", "Type": "StringType", "value_key": "StringValue",
         "value": "MSCU1234567", "is_eta_match": False, "match_rule": None},
        {"DefinitionId": "1000000003", "Name": "ETA", "Type": "DateType", "value_key": "DateValue",
         "value": "2026-11-02", "is_eta_match": True, "match_rule": "name"},
        {"resolved_eta_field": {"definition_id": "1000000003", "name": "ETA", "type": "DateType",
                                "value_key": "DateValue", "source": "invoice 190613"}},
    ]


async def test_list_custom_fields_with_configured_id_and_no_field(make_settings, load_fixture) -> None:
    client = make_client(make_settings(QB_ETA_DEFINITION_ID="1000000003"))
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=query_response(invoice_190613(load_fixture)))
        rows = await client.list_custom_fields("EBKG12345678")
    assert rows[1]["match_rule"] == "definition_id"
    client2 = make_client(make_settings())
    with respx.mock() as router:
        router.get(QUERY_URL).mock(return_value=query_response(invoices(load_fixture)[1]))
        rows2 = await client2.list_custom_fields("EBKG23456789 A")
    assert rows2[-1] == {"resolved_eta_field": None}
    assert rows2[0]["is_eta_match"] is False


# ── test 25 ──
class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


async def test_rate_limiting_five_calls_at_two_per_second(make_settings, load_fixture) -> None:
    client = make_client(make_settings(QB_MAX_CALLS_PER_SECOND="2"))
    clock = FakeTime()
    per_second = AsyncLimiter(client.settings.max_calls_per_second, 1.0, clock=clock, sleep=clock.sleep)
    per_minute = AsyncLimiter(client.settings.max_calls_per_minute, 60.0, clock=clock, sleep=clock.sleep)
    client._limiter = MultiLimiter(per_second, per_minute)
    with respx.mock() as router:
        route = router.get(f"{COMPANY}/companyinfo/{REALM}").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/companyinfo.json")))
        infos = await asyncio.gather(*(client.get_company_info() for _ in range(5)))
    assert route.call_count == 5
    assert all(i["CompanyName"] == "Example Forwarding LLC" for i in infos)
    assert clock.now >= 2.0
    assert per_second.acquired == 5 and per_minute.acquired == 5


async def test_multilimiter_enters_every_limiter_in_order() -> None:
    order: list[str] = []

    class Recording(AsyncLimiter):
        def __init__(self, name: str) -> None:
            super().__init__(10, 1.0)
            self.name = name

        async def acquire(self) -> None:
            order.append(self.name)
            await super().acquire()

    multi = MultiLimiter(Recording("second"), Recording("minute"))
    async with multi:
        pass
    assert order == ["second", "minute"]


async def test_semaphore_bounds_concurrent_calls(make_settings, load_fixture) -> None:
    client = make_client(make_settings(), semaphore=asyncio.Semaphore(2))
    active = 0
    peak = 0

    async def slow(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=load_fixture("qbo/companyinfo.json"))

    with respx.mock() as router:
        router.get(f"{COMPANY}/companyinfo/{REALM}").mock(side_effect=slow)
        await asyncio.gather(*(client.get_company_info() for _ in range(6)))
    assert peak == 2


# ── other errors and helpers ──
async def test_403_becomes_auth_error_without_retry(make_settings) -> None:
    client = make_client(make_settings())
    body = {"Fault": {"Error": [{"Message": "message=ApplicationAuthorizationFailed; errorCode=003100",
                                 "Detail": "", "code": "3100"}], "type": "SERVICE"}, "time": "t"}
    with respx.mock() as router:
        route = router.get(f"{INVOICE_URL}/190613").mock(
            return_value=httpx.Response(403, json=body, headers={"intuit_tid": "tid-403"}))
        with pytest.raises(AuthError) as info:
            await client.get_invoice("190613")
    assert route.call_count == 1
    assert str(info.value) == "QuickBooks denied access to realm 1234567890 (code 3100) [request_id=tid-403]"


async def test_get_invoice_610_and_other_faults(make_settings) -> None:
    client = make_client(make_settings())
    with respx.mock() as router:
        route = router.get(f"{INVOICE_URL}/42").mock(return_value=httpx.Response(400, json=fault("610", "Object Not Found")))
        with pytest.raises(InvoiceNotFoundError, match="Invoice 42 not found in QuickBooks"):
            await client.get_invoice("42")
        route.mock(return_value=httpx.Response(400, json=fault("2500", "Invalid Reference Id")))
        with pytest.raises(QboFaultError) as info:
            await client.get_invoice("42")
        assert info.value.codes == ("2500",)
        route.mock(return_value=httpx.Response(200, json={"time": "t"}))
        with pytest.raises(ServiceError, match="has no Invoice object"):
            await client.get_invoice("42")


async def test_e5_to_e10_helpers(make_settings, load_fixture, http_sleeps) -> None:
    client = make_client(make_settings())
    pdf = b"%PDF-1.7 test invoice"
    with respx.mock() as router:
        router.get(f"{INVOICE_URL}/190613/pdf").mock(return_value=httpx.Response(200, content=pdf))
        router.get(f"{INVOICE_URL}/190700/pdf").mock(return_value=httpx.Response(200, content=b"<html>login</html>"))
        link = router.get(f"{INVOICE_URL}/190613").mock(return_value=invoice_response(
            {"Id": "190613", "InvoiceLink": "https://connect.intuit.com/portal/app/CommerceNetwork/view/scs-v1-x"}))
        router.get(f"{INVOICE_URL}/190700").mock(return_value=invoice_response({"Id": "190700"}))
        prefs_get = router.get(f"{COMPANY}/preferences").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/preferences.json")))
        prefs_post = router.post(f"{COMPANY}/preferences").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/preferences.json")))
        send = router.post(f"{INVOICE_URL}/190613/send").mock(side_effect=[
            httpx.Response(503), httpx.Response(200, json=load_fixture("qbo/send_ok.json"))])
        send_500 = router.post(f"{INVOICE_URL}/190700/send").mock(return_value=httpx.Response(500, text="oops"))
        router.get(f"{COMPANY}/companyinfo/{REALM}").mock(
            return_value=httpx.Response(200, json=load_fixture("qbo/companyinfo.json")))

        assert await client.get_invoice_pdf("190613") == pdf
        with pytest.raises(ServiceError, match="did not return a PDF"):
            await client.get_invoice_pdf("190700")
        assert await client.get_invoice_link("190613") == (
            "https://connect.intuit.com/portal/app/CommerceNetwork/view/scs-v1-x")
        assert link.calls.last.request.url.params["include"] == "invoiceLink"
        assert await client.get_invoice_link("190700") is None
        prefs = await client.get_preferences()
        assert prefs["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] == "Invoice [Invoice No.] from [Company Name]"
        assert prefs_get.calls.last.request.url.params["minorversion"] == "75"
        new_prefs = await client.update_preferences(prefs)
        assert new_prefs["Id"] == "1"
        assert re.fullmatch(r"[0-9a-f]{32}", prefs_post.calls.last.request.url.params["requestid"])
        assert body_of(prefs_post.calls.last) == load_fixture("qbo/preferences.json")["Preferences"]
        answer = await client._post_send("190613", "probe@example.com")
        assert answer["Invoice"]["EmailStatus"] == "EmailSent"
        assert send.call_count == 2                      # 503 = refused → retried; never a timeout/5xx
        request = send.calls.last.request
        assert request.url.params["sendTo"] == "probe@example.com"
        assert request.headers["Content-Type"] == "application/octet-stream" and request.content == b""
        with pytest.raises(ServiceError) as info:
            await client._post_send("190700", None)      # 500 on /send: no retry (idempotent=False)
        assert info.value.status == 500
        assert send_500.call_count == 1
        info_obj = await client.get_company_info()
        assert info_obj["CompanyName"] == "Example Forwarding LLC"


async def test_post_send_fault_is_invoice_send_error(make_settings) -> None:
    client = make_client(make_settings())
    with respx.mock() as router:
        router.post(f"{INVOICE_URL}/190613/send").mock(
            return_value=httpx.Response(400, json=fault("6000", "Email address is invalid")))
        with pytest.raises(InvoiceSendError, match="send rejected: 6000 Email address is invalid"):
            await client._post_send("190613", None)


async def test_mark_email_sent_sparse_body(make_settings, load_fixture) -> None:
    client = make_client(make_settings())
    inv = snapshot(invoice_190613(load_fixture))
    with respx.mock() as router:
        post = router.post(INVOICE_URL).mock(return_value=invoice_response(
            dict(invoice_190613(load_fixture), SyncToken="8", EmailStatus="EmailSent")))
        marked = await client.mark_email_sent(inv)
        assert body_of(post.calls.last) == {"Id": "190613", "SyncToken": "7", "sparse": True,
                                            "EmailStatus": "EmailSent"}
        assert marked.email_status == "EmailSent"
        post.mock(return_value=httpx.Response(400, json=fault("2010", "Request has invalid or unsupported property")))
        with pytest.raises(InvoiceUpdateError, match="as e-mailed was rejected"):
            await client.mark_email_sent(inv)


async def test_build_qbo_and_aclose(make_settings, token_store: TokenStore) -> None:
    settings = make_settings()
    client = build_qbo(settings, token_store, read_only=True)
    assert client.read_only is True
    assert client.state_dir == settings.run.state_dir
    assert client.token_store is token_store
    assert client.email_settings is None and client.emailer is None
    assert client.auth.settings is settings.qbo
    await client.aclose()
    await client.aclose()                    # idempotent
    with pytest.raises(ConfigError, match="QB_CLIENT_ID"):
        build_qbo(make_settings({"QB_CLIENT_ID": None}), token_store)
    graph = make_settings({"QB_EMAIL_STRATEGY": "graph", "EMAIL_FROM": "a@example.com"})
    client2 = build_qbo(graph, token_store)
    assert client2.email_missing == ("EMAIL_GRAPH_TENANT_ID", "EMAIL_GRAPH_CLIENT_ID", "EMAIL_GRAPH_CLIENT_SECRET")
    await client2.aclose()


async def test_aclose_closes_auth_and_own_http_client(make_settings) -> None:
    auth = StaticAuth()
    client = QboClient(make_settings().qbo, auth)
    http = client._http
    await client.aclose()
    assert auth.closed is True and http.is_closed
    shared = httpx.AsyncClient(base_url=API)
    client2 = QboClient(make_settings().qbo, StaticAuth(), http=shared)
    await client2.aclose()
    assert not shared.is_closed              # an injected client belongs to the caller
    await shared.aclose()


def test_snapshot_dates_are_not_timezone_shifted() -> None:
    raw = {"Id": "1", "SyncToken": "0", "DocNumber": "D", "TotalAmt": 1, "Balance": 1, "CustomField": [
        {"DefinitionId": "1000000003", "Name": "ETA", "Type": "DateType", "DateValue": "2026-11-04T23:59:59-10:00"}]}
    assert snapshot(raw).eta_value == date(2026, 11, 4)


# ── a template left swapped by a crashed template_swap run (review fix XCT-03) ──
ORIGINAL_SUBJECT = "Invoice [Invoice No.] from [Company Name]"
SWAPPED_SUBJECT = ("Invoice for Booking no. EBKG12345678. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE "
                   "CARGO****")
GRAPH_ENV = {"QB_EMAIL_STRATEGY": "graph", "EMAIL_FROM": "accounting@example.com", "EMAIL_GRAPH_TENANT_ID": "t",
             "EMAIL_GRAPH_CLIENT_ID": "c", "EMAIL_GRAPH_CLIENT_SECRET": "s"}


def write_recovery_file(state_dir: Any) -> Any:
    from eta_automations.qbo.email import RECOVERY_FILE_NAME
    path = state_dir / RECOVERY_FILE_NAME
    path.write_text(json.dumps({"realm_id": REALM, "original_subject": ORIGINAL_SUBJECT, "original_message": None,
                                "swapped_subject": SWAPPED_SUBJECT, "invoice_id": "190613",
                                "written_at": "2026-11-04T16:02:11", "pid": 4242}), encoding="utf-8")
    return path


def mount_swapped_preferences(router: respx.MockRouter, load_fixture: Callable[[str], Any]) -> respx.Route:
    """Preferences that still carry the swapped subject; a POST stores (and echoes) the posted subject."""
    prefs = copy.deepcopy(load_fixture("qbo/preferences.json"))
    prefs["Preferences"]["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] = SWAPPED_SUBJECT
    router.get(f"{COMPANY}/preferences").mock(side_effect=lambda request: httpx.Response(200, json=prefs))

    def post(request: httpx.Request) -> httpx.Response:
        prefs["Preferences"] = json.loads(request.content)
        return httpx.Response(200, json=prefs)

    return router.post(f"{COMPANY}/preferences").mock(side_effect=post)


@pytest.mark.parametrize("env", [{}, GRAPH_ENV], ids=["template_swap", "graph"])
async def test_aclose_restores_a_template_left_swapped_by_a_crashed_run(make_settings, state_dir, load_fixture,
                                                                        env: dict[str, str]) -> None:
    """XCT-03: a run that sent nothing through template_swap (no e-mail, --no-email, graph/smtp) still repairs the
    company-wide subject a crashed template_swap run left behind (04b §6.2; QboClient.aclose, 10 §3)."""
    recovery = write_recovery_file(state_dir)
    client = make_client(make_settings(env), state_dir=state_dir)
    with respx.mock(assert_all_called=True) as router:
        post = mount_swapped_preferences(router, load_fixture)
        await client.aclose()
    assert post.call_count == 1
    assert body_of(post.calls.last)["EmailMessagesPrefs"]["InvoiceMessage"]["Subject"] == ORIGINAL_SUBJECT
    assert not recovery.exists()


async def test_recover_email_template_without_a_recovery_file_does_nothing(make_settings, state_dir) -> None:
    client = make_client(make_settings(GRAPH_ENV), state_dir=state_dir)
    with respx.mock(assert_all_mocked=True):
        assert await client.recover_email_template() is False
        await client.aclose()


async def test_read_only_client_never_restores_the_template_but_warns(make_settings, state_dir, caplog) -> None:
    caplog.set_level(logging.WARNING, logger="eta")
    recovery = write_recovery_file(state_dir)
    client = make_client(make_settings(GRAPH_ENV), state_dir=state_dir, read_only=True)
    with respx.mock(assert_all_mocked=True):
        await client.aclose()
    assert recovery.exists()
    assert any("may still be modified" in m for m in caplog.messages)
