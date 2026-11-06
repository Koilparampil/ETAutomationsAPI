"""Integration contracts between the orchestration layer and the modules it calls (03 §9, 03 §12, 10 §3, 12 §2).

- Every carrier module exposes `build_client(settings, token_store, *, clock=None, http=None)` (03 §9).
- `pipeline.build_services()` hands `FixedClock(run_now)` to every carrier factory, and the real clients keep it
  (03 §3: one instant per run).
- The in-memory fakes of tests/fakes.py have the same method names and parameters as the real clients (12 §2), so
  the pipeline matrix (12 §4), which runs on the fakes, exercises the call shapes the real clients accept.
- The call shapes pipeline.py and cli.py use bind to the real signatures.
- Every Settings field that pipeline.py, cli.py and carriers/router.py read exists (03 §12).
"""

from __future__ import annotations

import ast
import dataclasses
import inspect
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fakes import FakeAuth, FakeCarrier, FakeMscCarrier, FakeQbo, FakeVShip

import eta_automations
from eta_automations import pipeline
from eta_automations.carriers import aggregator, cma, grimaldi, maersk, msc
from eta_automations.carriers import router as carrier_router
from eta_automations.carriers.aggregator import AggregatorCarrierView, AggregatorClient
from eta_automations.carriers.cma import CmaClient
from eta_automations.carriers.grimaldi import GrimaldiClient
from eta_automations.carriers.maersk import MaerskClient
from eta_automations.carriers.msc import MscClient
from eta_automations.carriers.router import CarrierRouter
from eta_automations.clock import FixedClock
from eta_automations.config import (
    SERVICE_NAMES,
    AggregatorSettings,
    CmaSettings,
    EmailSettings,
    MaerskSettings,
    MscSettings,
    QboSettings,
    RunSettings,
    Settings,
    VShipSettings,
)
from eta_automations.models import Carrier
from eta_automations.qbo.auth import QboAuth
from eta_automations.qbo.client import InvoiceSnapshot, QboClient, build_qbo
from eta_automations.vship.client import VShipAuth, VShipClient, build_vship

RUN_NOW = datetime(2026, 11, 5, 9, 30)
CARRIER_MODULES: tuple[ModuleType, ...] = (msc, maersk, cma, grimaldi, aggregator)
P = inspect.Parameter


def _clock_of(client: Any) -> Any:
    """The clock a client keeps (the 03 §9 constructor argument; CMA names it `clock`, the others `_clock`)."""
    return getattr(client, "_clock", None) or getattr(client, "clock", None)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# 03 §9 carrier factories and the run's single clock
# ════════════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("module", CARRIER_MODULES, ids=lambda m: m.__name__.rsplit(".", 1)[-1])
def test_every_carrier_module_exposes_the_03_9_factory(module: ModuleType) -> None:
    params = list(inspect.signature(module.build_client).parameters.values())
    assert [(p.name, p.kind) for p in params] == [
        ("settings", P.POSITIONAL_OR_KEYWORD), ("token_store", P.POSITIONAL_OR_KEYWORD),
        ("clock", P.KEYWORD_ONLY), ("http", P.KEYWORD_ONLY),
    ]
    assert params[2].default is None and params[3].default is None


async def test_build_services_hands_the_run_clock_to_every_carrier_factory(
        make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[str, Any, Any]] = []

    def recorder(name: str, make: Callable[[], Any]) -> Callable[..., Any]:
        def factory(settings: Settings, token_store: Any, *, clock: Any = None, http: Any = None) -> Any:
            seen.append((name, clock, http))
            return make()
        return factory

    monkeypatch.setattr(msc, "build_client", recorder("msc", lambda: FakeCarrier(Carrier.MSC)))
    monkeypatch.setattr(maersk, "build_client", recorder("maersk", lambda: FakeCarrier(Carrier.MAERSK)))
    monkeypatch.setattr(cma, "build_client", recorder("cma", lambda: FakeCarrier(Carrier.CMA)))
    monkeypatch.setattr(grimaldi, "build_client", recorder("grimaldi", lambda: FakeCarrier(Carrier.GRIMALDI)))
    monkeypatch.setattr(aggregator, "build_client", recorder("aggregator", lambda: None))

    settings = make_settings(ETA_PROVIDER_GRIMALDI="native", ETA_PROVIDER_CMA="aggregator")
    async with pipeline.build_services(settings, RUN_NOW, read_only=True) as services:
        assert sorted(name for name, _clock, _http in seen) == ["aggregator", "grimaldi", "maersk", "msc"]
        assert isinstance(services.clock, FixedClock) and services.clock.now() == RUN_NOW
        for name, clock, http in seen:
            assert clock is services.clock, name          # FixedClock(run_now), the same object for every client
            assert http is None, name                      # each real client owns (and closes) its HTTP client


async def test_real_carrier_clients_built_by_build_services_keep_the_run_clock(
        make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(MAERSK_CLIENT_ID="test-maersk-id", MAERSK_CLIENT_SECRET="test-maersk-secret",
                             CMA_API_KEY="test-cma-key", ETA_PROVIDER_GRIMALDI="native")
    async with pipeline.build_services(settings, RUN_NOW, read_only=True) as services:
        router = services.router
        expected = {Carrier.MSC: MscClient, Carrier.MAERSK: MaerskClient, Carrier.CMA: CmaClient,
                    Carrier.GRIMALDI: GrimaldiClient}
        for carrier, cls in expected.items():
            client = router.native_client(carrier)
            assert isinstance(client, cls), carrier
            assert _clock_of(client).now() == RUN_NOW, carrier
        assert router.aggregator is None                    # no carrier is routed to the aggregator

    settings = make_settings(AGG_API_TOKEN="test-agg-token", ETA_PROVIDER_MSC="aggregator")
    async with pipeline.build_services(settings, RUN_NOW, read_only=True) as services:
        agg = services.router.aggregator
        assert isinstance(agg, AggregatorClient) and _clock_of(agg).now() == RUN_NOW
        assert services.router.native_client(Carrier.MSC) is None


async def test_real_services_expose_what_the_pipeline_reads(make_settings: Callable[..., Settings]) -> None:
    """10 Phase 0 step 6 authenticates through `qbo.auth` / `vship.auth`; 2b reads `qbo.ambiguous` (D21)."""
    async with pipeline.build_services(make_settings(), RUN_NOW, read_only=True) as services:
        assert isinstance(services.qbo.auth, QboAuth) and isinstance(services.vship.auth, VShipAuth)
        assert services.qbo.ambiguous == {}
        assert services.router.provider_for(Carrier.MSC) == "native"
        assert isinstance(services.router.native_client(Carrier.MSC), MscClient)


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Fakes (tests/fakes.py) mirror the real clients (12 §2)
# ════════════════════════════════════════════════════════════════════════════════════════════════
FAKE_PAIRS: tuple[tuple[type, type, tuple[str, ...]], ...] = (
    (FakeQbo, QboClient,
     ("find_invoices_by_doc_numbers", "update_invoice", "prepare_for_sending", "send_invoice", "aclose")),
    (FakeVShip, VShipClient,
     ("find_booking_id", "get_customer_name", "get_internal_comments", "post_comment", "patch_expected_arrival",
      "aclose")),
    (FakeCarrier, MscClient, ("get_eta", "probe", "aclose")),
    (FakeCarrier, MaerskClient, ("get_eta", "probe", "aclose")),
    (FakeCarrier, CmaClient, ("get_eta", "probe", "aclose")),
    (FakeCarrier, GrimaldiClient, ("get_eta", "probe", "aclose")),
    (FakeCarrier, AggregatorCarrierView, ("get_eta", "probe", "aclose")),
    (FakeMscCarrier, MscClient, ("token_status", "set_manual_token")),
    (FakeAuth, QboAuth, ("get_token", "invalidate")),
    (FakeAuth, VShipAuth, ("get_token", "invalidate")),
)


def _shape(func: Callable[..., Any]) -> tuple[list[tuple[str, Any, bool]], bool]:
    params = list(inspect.signature(func).parameters.values())[1:]           # without self
    return ([(p.name, p.kind, p.default is not P.empty) for p in params], inspect.iscoroutinefunction(func))


@pytest.mark.parametrize(("fake", "real", "methods"), FAKE_PAIRS,
                         ids=[f"{f.__name__}-{r.__name__}" for f, r, _m in FAKE_PAIRS])
def test_fakes_have_the_real_method_names_and_parameters(fake: type, real: type, methods: tuple[str, ...]) -> None:
    for name in methods:
        assert callable(getattr(real, name, None)), f"{real.__name__}.{name} is missing"
        assert _shape(getattr(fake, name)) == _shape(getattr(real, name)), f"{fake.__name__}.{name}"


# ════════════════════════════════════════════════════════════════════════════════════════════════
# The call shapes of pipeline.py and cli.py bind to the real signatures
# ════════════════════════════════════════════════════════════════════════════════════════════════
_INV = InvoiceSnapshot(id="1", sync_token="0", doc_number="EBKG10000001", total_amount=Decimal("2450.00"),
                       balance=Decimal("2450.00"), customer_name="ACME", bill_email="ap@example.com",
                       email_status=None, eta_value=None, raw={})
_ETA = datetime(2026, 11, 3)

CALL_SHAPES: tuple[tuple[str, Callable[..., Any], tuple[Any, ...], dict[str, Any]], ...] = (
    # pipeline.py (10 §3–§4)
    ("build_qbo", build_qbo, (None, None), {"read_only": True}),
    ("build_vship", build_vship, (None, None), {"read_only": True}),
    ("build_router", carrier_router.build_router, (None, None), {"clock": FixedClock(RUN_NOW)}),
    ("QboClient.find_invoices_by_doc_numbers", QboClient.find_invoices_by_doc_numbers, (None, ["EBKG1"]), {}),
    ("QboClient.update_invoice", QboClient.update_invoice, (None, _INV),
     {"eta": date(2026, 11, 3), "doc_number": "EBKG10000001 A"}),
    ("QboClient.update_invoice (D11 revert)", QboClient.update_invoice, (None, _INV), {"doc_number": "EBKG10000001"}),
    ("QboClient.prepare_for_sending", QboClient.prepare_for_sending, (None,), {"dry_run": True}),
    ("QboClient.send_invoice", QboClient.send_invoice, (None, _INV), {"subject": "subject"}),
    ("VShipClient.find_booking_id", VShipClient.find_booking_id, (None, "EBKG10000001"), {}),
    ("VShipClient.get_customer_name", VShipClient.get_customer_name, (None, "EBKG10000001"), {}),
    ("VShipClient.get_internal_comments", VShipClient.get_internal_comments, (None, "72892"), {}),
    ("VShipClient.post_comment", VShipClient.post_comment, (None, "72892", "ARRIVED ON 11/03. NOTIF #1"), {}),
    ("VShipClient.patch_expected_arrival", VShipClient.patch_expected_arrival, (None, "72892", _ETA), {}),
    ("CarrierRouter.prepare", CarrierRouter.prepare, (None, ["EBKG10000001"]), {}),
    ("CarrierRouter.route", CarrierRouter.route, (None, "EBKG10000001"), {}),
    ("CarrierRouter.provider_for", CarrierRouter.provider_for, (None, Carrier.MSC), {}),
    ("CarrierRouter.native_client", CarrierRouter.native_client, (None, Carrier.MSC), {}),
    ("MscClient.token_status", MscClient.token_status, (None,), {}),
    ("MscClient.set_manual_token", MscClient.set_manual_token, (None, "pasted"), {}),
    ("msc.token_needs_refresh", msc.token_needs_refresh, ({"has_token": False},), {}),
    # cli.py probes and helpers (11 §2)
    ("VShipClient.probe", VShipClient.probe, (None, "EBKG17208602"), {"run_now": RUN_NOW}),
    ("MscClient.probe", MscClient.probe, (None, "EBKG17208602"), {}),
    ("MaerskClient.probe", MaerskClient.probe, (None, "267546627"), {}),
    ("CmaClient.probe", CmaClient.probe, (None, "NAM1234567"), {}),
    ("AggregatorClient.probe", AggregatorClient.probe, (None, None, "EBKG17208602"), {}),
    ("msc.bridge_token_status", msc.bridge_token_status, (MscSettings(mode="mymsc"), None), {}),
    ("msc.store_manual_token", msc.store_manual_token, (MscSettings(mode="mymsc"), None, "pasted"), {}),
    ("msc.probe_sandbox", msc.probe_sandbox, (None,), {"settings": None, "clock": FixedClock(RUN_NOW)}),
    ("msc.compare_modes", msc.compare_modes, (MscSettings(mode="api"), None, ["EBKG17208602"]),
     {"clock": FixedClock(RUN_NOW)}),
    ("msc.save_probe_output", msc.save_probe_output, (Path("state"), "EBKG17208602", {}), {"when": RUN_NOW}),
    ("QboClient.get_invoice_by_doc_number", QboClient.get_invoice_by_doc_number, (None, "EBKG10000001"), {}),
    ("QboClient.get_invoice", QboClient.get_invoice, (None, "1"), {}),
    ("QboClient.update_invoice (write test)", QboClient.update_invoice, (None, _INV), {"eta": date(2026, 11, 4)}),
    ("QboClient.get_preferences", QboClient.get_preferences, (None,), {}),
    ("QboClient.update_preferences", QboClient.update_preferences, (None, {}), {}),
    ("QboClient.get_company_info", QboClient.get_company_info, (None,), {}),
    ("QboClient.list_custom_fields", QboClient.list_custom_fields, (None, "EBKG10000001"), {}),
    ("QboAuth.force_refresh", QboAuth.force_refresh, (None,), {}),
)


@pytest.mark.parametrize(("label", "func", "args", "kwargs"), CALL_SHAPES, ids=[c[0] for c in CALL_SHAPES])
def test_orchestration_call_shapes_bind_to_the_real_signatures(label: str, func: Callable[..., Any],
                                                               args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    inspect.signature(func).bind(*args, **kwargs)   # raises TypeError on a mismatch


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Every Settings field the orchestration modules read exists (03 §12)
# ════════════════════════════════════════════════════════════════════════════════════════════════
_SRC = Path(eta_automations.__file__).parent
ORCHESTRATION_MODULES = ("pipeline.py", "cli.py", "carriers/router.py")
SERVICE_SETTINGS: dict[str, type] = {
    "run": RunSettings, "qbo": QboSettings, "vship": VShipSettings, "msc": MscSettings, "maersk": MaerskSettings,
    "cma": CmaSettings, "aggregator": AggregatorSettings, "email": EmailSettings,
}


def _chain(node: ast.Attribute) -> list[str] | None:
    names: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        names.append(current.attr)
        current = current.value
    if not isinstance(current, ast.Name):
        return None
    names.append(current.id)
    return names[::-1]


def settings_reads(source: str) -> set[tuple[str, ...]]:
    """Attribute paths read below a Settings object: `settings.<a>[.<b>]`, `self.settings.…`, `ctx.settings.…`
    (in the orchestration modules these names always hold the top-level Settings)."""
    reads: set[tuple[str, ...]] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Attribute):
            continue
        chain = _chain(node)
        if chain is None:
            continue
        if chain[0] == "settings":
            rest = chain[1:]
        elif len(chain) > 1 and chain[0] in ("self", "ctx") and chain[1] == "settings":
            rest = chain[2:]
        else:
            continue
        if rest:
            reads.add(tuple(rest[:2]))
    return reads


def _members(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)} | {name for name in dir(cls) if not name.startswith("_")}


def test_settings_scan_finds_reads() -> None:
    reads = settings_reads("def f(settings, self, ctx):\n    settings.run.state_dir\n    self.settings.qbo\n"
                           "    ctx.settings.missing.get('qbo')\n    other.settings.ignored\n")
    assert reads == {("run", "state_dir"), ("run",), ("qbo",), ("missing", "get"), ("missing",)}


def required_services(source: str) -> set[str]:
    """String arguments of every `require(settings, "<service>", …)` call (03 §12)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "require":
            names.update(arg.value for arg in node.args[1:]
                         if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
    return names


def test_every_required_service_name_is_known_to_config_require() -> None:
    used = set().union(*(required_services((_SRC / m).read_text(encoding="utf-8")) for m in ("pipeline.py", "cli.py")))
    assert {"qbo", "vship", "msc", "maersk", "cma", "aggregator"} <= used
    assert used <= set(SERVICE_NAMES)        # require() raises ValueError for any other name


@pytest.mark.parametrize("module", ORCHESTRATION_MODULES)
def test_every_settings_field_the_orchestration_reads_exists(module: str) -> None:
    reads = settings_reads((_SRC / module).read_text(encoding="utf-8"))
    assert reads, f"no Settings reads found in {module}"
    top = {f.name for f in dataclasses.fields(Settings)}
    problems = []
    for read in sorted(reads):
        if read[0] not in top:
            problems.append(".".join(read))
        elif len(read) == 2 and read[0] in SERVICE_SETTINGS and read[1] not in _members(SERVICE_SETTINGS[read[0]]):
            problems.append(".".join(read))
    assert problems == [], f"{module} reads Settings fields that do not exist: {problems}"
