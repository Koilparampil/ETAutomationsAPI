"""Idempotency ledger `<ETA_STATE_DIR>/ledger.jsonl` (10-pipeline §9, D20).

One JSON object per line, appended (flush + fsync) for every write action the pipeline performs during
apply: `{"ts", "run_id", "doc_number", "invoice_id", "base_booking", "action", "ok", "detail"}`.
Action names used by the pipeline: `qbo_update`, `emailed` (a QboSend, with the receipt's strategy,
recipients and sent_at / qbo_delivery_time — never the e-mail body), `qbo_revert_doc_number` (D11),
`vship_comment`, `vship_patch_eta`.

At start-up the ledger indexes every successful `emailed` event, so `emailed_on(invoice_id, day)` can
answer the same-day e-mail guard (10 §4 Phase 2c) without re-reading the file. Corrupt lines are skipped
with a WARNING. A disabled ledger (dry run) never writes, and never creates the file. `detail` passes
through redact() so no secret can end up in the file (02 §5).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from .clock import Clock, SystemClock
from .logging_setup import redact

__all__ = ["EMAILED", "LEDGER_FILE_NAME", "Ledger"]

log = logging.getLogger("eta.pipeline")

LEDGER_FILE_NAME = "ledger.jsonl"
EMAILED = "emailed"   # the action name of a QboSend (10 §4 Phase 4)


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, Path)):
        return str(value)
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)


def _parse_time(value: Any) -> datetime | None:
    """ISO text → naive local datetime (an aware value is converted to local time first); else None."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is not None:
        try:
            return parsed.astimezone().replace(tzinfo=None)
        except (OverflowError, ValueError, OSError):
            return None
    return parsed


class Ledger:
    """Append-only JSON-lines record of completed (and failed) write actions (10 §9)."""

    def __init__(self, path: Path, *, clock: Clock | None = None, enabled: bool = True) -> None:
        self.path = Path(path)
        self.enabled = bool(enabled)            # False in dry run: record() is a no-op
        self._clock: Clock = clock or SystemClock()   # timestamps of new records
        self._lock = threading.Lock()
        self._emails: dict[str, list[datetime]] = {}   # invoice id -> times of successful e-mails
        self._load_index()

    # ── reading ──
    def _load_index(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Could not read the ledger %s (%s) - the same-day e-mail guard sees no earlier e-mails",
                        self.path, exc)
            return
        for number, line in enumerate(lines, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except ValueError:
                log.warning("Ledger %s line %d is not valid JSON - skipped", self.path, number)
                continue
            if not isinstance(record, dict):
                log.warning("Ledger %s line %d is not a JSON object - skipped", self.path, number)
                continue
            self._index(record)

    def _index(self, record: Mapping[str, Any]) -> None:
        if record.get("action") != EMAILED or record.get("ok") is not True:
            return
        invoice_id = record.get("invoice_id")
        if invoice_id in (None, ""):
            return
        detail = record.get("detail")
        when = _parse_time(detail.get("sent_at")) if isinstance(detail, Mapping) else None
        if when is None:
            when = _parse_time(record.get("ts"))
        if when is None:
            log.warning("Ledger e-mail record for invoice %s has no readable time - ignored", invoice_id)
            return
        self._emails.setdefault(str(invoice_id), []).append(when)

    def emailed_on(self, invoice_id: str, day: date) -> datetime | None:
        """Time of the latest successful QboSend (`emailed`) for this invoice on `day`, else None."""
        times = [t for t in self._emails.get(str(invoice_id), []) if t.date() == day]
        return max(times) if times else None

    # ── writing ──
    def record(self, *, run_id: str, doc_number: str, invoice_id: str | None, base_booking: str | None,
               action: str, detail: Mapping[str, Any], ok: bool) -> None:
        """Append one JSON line and flush + fsync it (never in dry run). `detail` must not hold the e-mail
        body; it is redacted anyway (02 §5)."""
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "ts": self._clock.now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "doc_number": doc_number,
            "invoice_id": invoice_id,
            "base_booking": base_booking,
            "action": action,
            "ok": bool(ok),
            "detail": redact(json.loads(json.dumps(dict(detail), default=_json_default))),
        }
        line = json.dumps(record, ensure_ascii=False, default=_json_default) + "\n"
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
            self._index(record)
