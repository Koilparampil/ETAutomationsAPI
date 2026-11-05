"""Shared models (03-foundations §4) and pipeline models (10-pipeline §2).

`NotifStatus` (vship/logic.py) and `InvoiceSnapshot` (qbo/client.py) are referenced only under
TYPE_CHECKING, so importing this module never imports the VShip or QuickBooks code at runtime.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, TypeAlias

if TYPE_CHECKING:
    from .qbo.client import InvoiceSnapshot
    from .vship.logic import NotifStatus

__all__ = [
    "Action",
    "Carrier",
    "EtaResult",
    "InputRow",
    "OutputFile",
    "ParsedBooking",
    "ParsedRow",
    "Plan",
    "PlanStatus",
    "QboSend",
    "QboUpdate",
    "Route",
    "RowKind",
    "VShipComment",
    "VShipPatchEta",
]


# ── 03 §4 ──
class Carrier(StrEnum):
    MSC = "MSC"
    MAERSK = "Maersk"
    CMA = "CMA"
    GRIMALDI = "Grimaldi"


@dataclass(frozen=True, slots=True)
class EtaResult:
    carrier: Carrier
    booking: str                 # the base booking number that was queried (no suffixes)
    eta: datetime                # NAIVE carrier-local wall-clock time (offset parsed, then dropped);
                                 # date-only sources use 00:00
    source: str                  # e.g. "MSC event 'Estimated Time of Arrival'", "DCSA TRANSPORT ARRI/EST at USSAV"
    is_actual: bool = False      # the source says the arrival/discharge already happened
    from_transshipment_rule: bool = False   # MSC fallback rule used (06)
    containers: tuple[str, ...] = ()        # container numbers considered, if known
    details: Mapping[str, Any] = field(default_factory=dict)  # small JSON-serializable extras for the report

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly form (probe output, reports)."""
        return {
            "carrier": str(self.carrier),
            "booking": self.booking,
            "eta": self.eta.isoformat(),
            "source": self.source,
            "is_actual": self.is_actual,
            "from_transshipment_rule": self.from_transshipment_rule,
            "containers": list(self.containers),
            "details": dict(self.details),
        }


# ── 10 §2 ──
class RowKind(StrEnum):
    MALFORMED = "MALFORMED"
    STANDARD = "STANDARD"
    NOTIF1_SENT = "NOTIF1_SENT"
    BOTH_SENT = "BOTH_SENT"
    SKIP_R = "SKIP_R"
    MANUAL = "MANUAL"


@dataclass(frozen=True, slots=True)
class InputRow:
    line_no: int
    doc_number: str
    amount_raw: str
    amount: Decimal | None
    parse_error: str | None           # e.g. "could not convert string to float: 'abc'"


@dataclass(frozen=True, slots=True)
class ParsedRow:
    row: InputRow
    kind: RowKind
    base_booking: str | None          # stripped + upper-cased (01 §3)


# 03 §4 calls the parsed row "ParsedBooking"; 10 §2 names it ParsedRow. Same class.
ParsedBooking = ParsedRow


class Route(StrEnum):
    FAILED = "FAILED"
    CUSTOMS_HOLD = "CUSTOMS_HOLD"
    SKIPPED = "SKIPPED"
    BINDU = "BINDU"
    BEN = "BEN"
    ACCOUNTING = "ACCOUNTING"
    PAID_UPDATE = "PAID_UPDATE"
    NOTIFY = "NOTIFY"
    FUTURE_UPDATE = "FUTURE_UPDATE"


class OutputFile(StrEnum):
    BINDU = "Send2Bindu.csv"
    BEN = "Send2Ben.txt"
    ACCOUNTING = "Send2Accounting.txt"
    FAILED = "Failed_To_Process.txt"
    CUSTOMS = "PotentialCustomsHold.txt"


# Side-effect actions, executed in list order during apply (10 §4 Phase 4).
@dataclass(frozen=True, slots=True)
class QboUpdate:
    eta: date
    new_doc_number: str | None = None


@dataclass(frozen=True, slots=True)
class QboSend:
    subject: str


@dataclass(frozen=True, slots=True)
class VShipComment:
    text: str


@dataclass(frozen=True, slots=True)
class VShipPatchEta:
    eta: datetime


Action: TypeAlias = QboUpdate | QboSend | VShipComment | VShipPatchEta

PlanStatus: TypeAlias = Literal["PENDING", "OK", "FAILED", "ROUTED", "SKIPPED", "INTERRUPTED"]


@dataclass
class Plan:
    parsed: ParsedRow
    route: Route
    message: str = ""                     # Failed/routed entry text (01 §14) or human reason
    output: tuple[OutputFile, str | list[str]] | None = None   # entry to write for routed/failed rows
    carrier: Carrier | None = None
    eta: EtaResult | None = None
    cutoff: datetime | None = None
    in_window: bool | None = None
    notif: NotifStatus | None = None
    notif_defaulted: bool = False         # 01 §6.2 'send' default applied
    invoice: InvoiceSnapshot | None = None
    vship_booking_id: str | None = None
    vship_error: str | None = None        # VShip id/comments could not be read in the plan phase
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # filled during apply
    status: PlanStatus = "PENDING"
    done: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def doc_number(self) -> str:
        """The input DocNumber exactly as in the file."""
        return self.parsed.row.doc_number

    @property
    def line_no(self) -> int:
        return self.parsed.row.line_no

    @property
    def base_booking(self) -> str | None:
        return self.parsed.base_booking
