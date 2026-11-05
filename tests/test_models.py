"""models.py (03 §4, 10 §2) plus the other small foundation modules: clock.py (03 §3) and errors.py (03 §5)."""

from __future__ import annotations

import dataclasses
import json
import os
import pickle
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from eta_automations import errors
from eta_automations.clock import Clock, FixedClock, SystemClock
from eta_automations.models import (
    Action,
    Carrier,
    EtaResult,
    InputRow,
    OutputFile,
    ParsedBooking,
    ParsedRow,
    Plan,
    QboSend,
    QboUpdate,
    Route,
    RowKind,
    VShipComment,
    VShipPatchEta,
)

SRC_DIR = Path(__file__).resolve().parents[1] / "src"


# ── 03 §4 ──────────────────────────────────────────────────────────────────────────────────────────
def test_carrier_values() -> None:
    assert [c.value for c in Carrier] == ["MSC", "Maersk", "CMA", "Grimaldi"]
    assert [c.name for c in Carrier] == ["MSC", "MAERSK", "CMA", "GRIMALDI"]
    assert str(Carrier.MAERSK) == "Maersk" and f"{Carrier.CMA}" == "CMA"
    assert Carrier("Grimaldi") is Carrier.GRIMALDI


def test_eta_result_defaults_frozen_and_slots() -> None:
    result = EtaResult(Carrier.MSC, "EBKG17208602", datetime(2026, 11, 16), "MSC event 'Estimated Time of Arrival'")
    assert result.is_actual is False
    assert result.from_transshipment_rule is False
    assert result.containers == ()
    assert result.details == {}
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.eta = datetime(2026, 11, 17)  # type: ignore[misc]
    assert not hasattr(result, "__dict__")
    other = EtaResult(Carrier.MSC, "X", datetime(2026, 1, 1), "s")
    assert other.details is not result.details  # independent default dicts


def test_eta_result_to_dict_is_json_serializable() -> None:
    result = EtaResult(Carrier.CMA, "NAM1234567", datetime(2026, 11, 12, 14, 0), "DCSA TRANSPORT ARRI/EST at USLGB",
                       is_actual=False, containers=("CMAU1234567",), details={"pod": "USLGB", "rescue": False})
    data = result.to_dict()
    assert data == {
        "carrier": "CMA", "booking": "NAM1234567", "eta": "2026-11-12T14:00:00",
        "source": "DCSA TRANSPORT ARRI/EST at USLGB", "is_actual": False, "from_transshipment_rule": False,
        "containers": ["CMAU1234567"], "details": {"pod": "USLGB", "rescue": False},
    }
    json.dumps(data)


# ── 10 §2 ──────────────────────────────────────────────────────────────────────────────────────────
def test_row_kind_route_and_output_file_values() -> None:
    assert [k.value for k in RowKind] == ["MALFORMED", "STANDARD", "NOTIF1_SENT", "BOTH_SENT", "SKIP_R", "MANUAL"]
    assert [r.value for r in Route] == ["FAILED", "CUSTOMS_HOLD", "SKIPPED", "BINDU", "BEN", "ACCOUNTING",
                                        "PAID_UPDATE", "NOTIFY", "FUTURE_UPDATE"]
    # 01 §14: same file names as the original
    assert {f.name: f.value for f in OutputFile} == {
        "BINDU": "Send2Bindu.csv", "BEN": "Send2Ben.txt", "ACCOUNTING": "Send2Accounting.txt",
        "FAILED": "Failed_To_Process.txt", "CUSTOMS": "PotentialCustomsHold.txt",
    }


def test_input_and_parsed_rows() -> None:
    row = InputRow(line_no=1, doc_number="EBKG17208602 A", amount_raw="2,450.00", amount=Decimal("2450.00"),
                   parse_error=None)
    parsed = ParsedRow(row=row, kind=RowKind.NOTIF1_SENT, base_booking="EBKG17208602")
    assert parsed.row.amount == Decimal("2450.00")
    assert ParsedBooking is ParsedRow
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.kind = RowKind.STANDARD  # type: ignore[misc]
    bad = InputRow(2, "EBKG17208602", "abc", None, "could not convert string to float: 'abc'")
    assert bad.amount is None and bad.parse_error is not None


def test_actions_are_frozen_with_defaults_and_form_the_action_union() -> None:
    update = QboUpdate(date(2026, 11, 3))
    assert update.new_doc_number is None
    actions: list[Action] = [QboUpdate(date(2026, 11, 3), "EBKG10000001 A"),
                             QboSend("Invoice for Booking no. EBKG10000001. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE CARGO****"),
                             VShipComment("ARRIVED ON 11/03. NOTIF #1"),
                             VShipPatchEta(datetime(2026, 11, 3))]
    assert all(isinstance(a, Action) for a in actions)  # type: ignore[arg-type]
    assert not isinstance("text", Action)  # type: ignore[arg-type]
    with pytest.raises(dataclasses.FrozenInstanceError):
        update.eta = date(2026, 11, 4)  # type: ignore[misc]
    assert QboSend("s") == QboSend("s") and hash(VShipComment("x")) == hash(VShipComment("x"))


def test_plan_defaults_and_helpers() -> None:
    row = InputRow(3, "NAM1000017 C", "5000", Decimal("5000"), None)
    plan = Plan(parsed=ParsedRow(row, RowKind.MANUAL, None), route=Route.FAILED)
    assert plan.status == "PENDING"
    assert plan.message == "" and plan.output is None and plan.error is None
    assert plan.actions == [] and plan.warnings == [] and plan.done == []
    assert plan.notif_defaulted is False and plan.in_window is None
    assert plan.doc_number == "NAM1000017 C" and plan.line_no == 3 and plan.base_booking is None
    plan2 = Plan(parsed=ParsedRow(row, RowKind.MANUAL, None), route=Route.FAILED)
    plan.actions.append(VShipComment("x"))
    plan.warnings.append("w")
    assert plan2.actions == [] and plan2.warnings == []  # independent default lists
    plan.status = "OK"  # mutable during apply
    plan.output = (OutputFile.FAILED, "NAM1000017 C")
    assert plan.output[0] is OutputFile.FAILED


def test_models_do_not_import_vship_or_qbo_at_runtime() -> None:
    code = ("import sys, eta_automations.models; "
            "bad = sorted(m for m in sys.modules if m.startswith(('eta_automations.qbo', 'eta_automations.vship'))); "
            "print(bad); sys.exit(1 if bad else 0)")
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                            env={**os.environ, "PYTHONPATH": str(SRC_DIR)}, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr


# ── clock.py (03 §3) ───────────────────────────────────────────────────────────────────────────────
def test_fixed_clock(fixed_clock: FixedClock) -> None:
    assert fixed_clock.now() == datetime(2026, 11, 5, 9, 30)
    assert fixed_clock.now() == fixed_clock.now()
    assert isinstance(fixed_clock, Clock)
    fixed_clock.advance(timedelta(days=1))
    assert fixed_clock.now() == datetime(2026, 11, 6, 9, 30)


def test_system_clock_is_naive_local_now() -> None:
    clock = SystemClock()
    before = datetime.now()
    value = clock.now()
    assert value.tzinfo is None
    assert before <= value <= datetime.now()
    assert isinstance(clock, Clock)


# ── errors.py (03 §5 + additions) ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cls,base", [
    (errors.ConfigError, errors.EtaAutomationError),
    (errors.AuthError, errors.EtaAutomationError),
    (errors.ServiceError, errors.EtaAutomationError),
    (errors.CarrierLookupError, errors.EtaAutomationError),
    (errors.UnsupportedCarrierError, errors.CarrierLookupError),
    (errors.AggregatorBookingError, errors.CarrierLookupError),
    (errors.NoEtaError, errors.EtaAutomationError),
    (errors.CustomsHoldError, errors.EtaAutomationError),
    (errors.VShipBookingNotFoundError, errors.ServiceError),
    (errors.NoteError, errors.ServiceError),
    (errors.BookingChangeError, errors.ServiceError),
    (errors.InvoiceNotFoundError, errors.EtaAutomationError),
    (errors.AmbiguousInvoiceError, errors.InvoiceNotFoundError),
    (errors.InvoiceUpdateError, errors.ServiceError),
    (errors.InvoiceSendError, errors.ServiceError),
    (errors.EmailPathUnavailableError, errors.InvoiceSendError),
    (errors.TemplateRestoreError, errors.EmailPathUnavailableError),
    (errors.RunLockedError, errors.EtaAutomationError),
    (errors.SWBTRError, errors.EtaAutomationError),
])
def test_error_hierarchy(cls: type[Exception], base: type[Exception]) -> None:
    assert cls.__bases__ == (base,)
    assert issubclass(cls, errors.EtaAutomationError)


def test_carrier_errors_are_not_service_errors() -> None:
    assert not issubclass(errors.NoEtaError, errors.CarrierLookupError)
    assert not issubclass(errors.CustomsHoldError, errors.CarrierLookupError)
    assert not issubclass(errors.CarrierLookupError, errors.ServiceError)


def test_service_error_full_format() -> None:
    err = errors.ServiceError("qbo", "update rejected: 6000 Business Validation Error", status=400,
                              body_excerpt='{"Fault": "x"}', request_id="tid-9")
    assert str(err) == ('qbo: update rejected: 6000 Business Validation Error (HTTP 400) [request_id=tid-9] '
                        'body: {"Fault": "x"}')
    assert (err.service, err.status, err.request_id) == ("qbo", 400, "tid-9")


def test_service_error_partial_formats() -> None:
    assert str(errors.ServiceError("vship", "network error: ConnectError('x')")) == "vship: network error: ConnectError('x')"
    assert str(errors.ServiceError("msc", "boom", status=503)) == "msc: boom (HTTP 503)"
    assert str(errors.ServiceError("cma", "boom", request_id="r")) == "cma: boom [request_id=r]"


def test_single_argument_form_is_the_exact_message() -> None:
    """04b §6.1 raises e.g. EmailPathUnavailableError("...") — pipeline P33 expects str(err) == message."""
    err = errors.EmailPathUnavailableError("not verified")
    assert str(err) == "not verified"
    assert err.message == "not verified" and err.service == "" and err.status is None
    assert str(errors.InvoiceSendError("invalid subject")) == "invalid subject"
    assert str(errors.TemplateRestoreError("restore failed")) == "restore failed"
    assert isinstance(err, errors.InvoiceSendError)


def test_body_excerpt_is_redacted_and_capped() -> None:
    err = errors.ServiceError("vship", "login failed", status=400,
                              body_excerpt='{"password": "hunter2", "x": "' + "y" * 1000 + '"}')
    assert len(err.body_excerpt) <= 500
    assert "hunter2" not in str(err)


def test_wrap_keeps_service_error_details() -> None:
    original = errors.ServiceError("vship", "unexpected HTTP 400 for POST /Bookings/1/comments", status=400,
                                   body_excerpt="bad request", request_id="rid")
    wrapped = errors.NoteError.wrap(original)
    assert isinstance(wrapped, errors.NoteError)
    assert str(wrapped) == str(original)
    assert (wrapped.status, wrapped.request_id, wrapped.body_excerpt) == (400, "rid", "bad request")
    other = errors.BookingChangeError.wrap(errors.AuthError("VShip login failed (HTTP 401)"), service="vship")
    assert str(other) == "vship: VShip login failed (HTTP 401)"
    renamed = errors.InvoiceSendError.wrap(original, "send failed")
    assert str(renamed).startswith("vship: send failed (HTTP 400)")


def test_service_error_pickles() -> None:
    err = errors.InvoiceUpdateError("qbo", "stale", status=400, request_id="t")
    clone = pickle.loads(pickle.dumps(err))
    assert type(clone) is errors.InvoiceUpdateError and str(clone) == str(err) and clone.status == 400
