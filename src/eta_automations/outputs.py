"""Output files of a run (01-business-rules §14, 10-pipeline §5, §7 and Phase 3 step 1).

- `OutputWriter.write()` appends one entry to Send2Bindu.csv / Send2Ben.txt / Send2Accounting.txt /
  Failed_To_Process.txt / PotentialCustomsHold.txt in ETA_OUTPUT_DIR, exactly as the original did:
  text files get `entry + "\\n"` (append mode, UTF-8, the platform's newline translation like the
  original's plain `open(..., "a")`); Send2Bindu.csv gets one `csv.writer` row (`newline=""`) and a UTF-8
  BOM only when the file is created. Every call opens and closes the file (crash-safe) under a
  threading.Lock. With `enabled=False` (dry run) nothing is written and no file is created.
- `OutputWriter.write_to_email()` appends the `--no-email` rows to `ToEmail_<run_id>.csv`.
- `write_report()` writes the run report `reports/run_<run_id>.csv` and `.json` (also in dry run).

No secrets are ever written here: entries are the 01 §14 templates built by texts.py / rules.py.
"""

from __future__ import annotations

import contextlib
import csv
import json
import logging
import os
import tempfile
import threading
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from .models import OutputFile

__all__ = [
    "REPORT_COLUMNS",
    "TO_EMAIL_COLUMNS",
    "UTF8_BOM",
    "OutputWriter",
    "report_paths",
    "to_email_path",
    "write_report",
]

log = logging.getLogger("eta.pipeline")

UTF8_BOM = "﻿"  # written once, at the start of a CSV file this program creates (01 §14)

# 10 §7: one row per input row, in this column order.
REPORT_COLUMNS: tuple[str, ...] = (
    "line_no", "doc_number", "amount", "kind", "base_booking", "carrier", "eta", "eta_source", "eta_is_actual",
    "eta_rescue", "cutoff", "in_window", "notif_count", "notif_defaulted", "invoice_id", "balance", "total",
    "bill_email", "route", "new_doc_number", "subject", "vship_booking_id", "actions_planned", "actions_done",
    "status", "message", "warnings",
)

# 10 Phase 3 step 1 (--no-email): the e-mails to send by hand.
TO_EMAIL_COLUMNS: tuple[str, ...] = (
    "doc_number", "invoice_id", "customer", "to", "cc", "subject", "doc_number_after_sending",
    "vship_note_after_sending",
)


def to_email_path(output_dir: Path, run_id: str) -> Path:
    """<ETA_OUTPUT_DIR>/ToEmail_<run_id>.csv (10 Phase 3 step 1)."""
    return Path(output_dir) / f"ToEmail_{run_id}.csv"


def report_paths(output_dir: Path, run_id: str) -> tuple[Path, Path]:
    """(<ETA_OUTPUT_DIR>/reports/run_<run_id>.csv, ….json) (10 §7)."""
    reports = Path(output_dir) / "reports"
    return reports / f"run_{run_id}.csv", reports / f"run_{run_id}.json"


def _is_new(path: Path) -> bool:
    """A CSV gets its BOM (and header) when it does not exist yet or is still empty."""
    try:
        return path.stat().st_size == 0
    except FileNotFoundError:
        return True


class OutputWriter:
    """Appends the 01 §14 entries to the output files in ETA_OUTPUT_DIR (10 §5)."""

    def __init__(self, output_dir: Path, *, enabled: bool) -> None:
        self.output_dir = Path(output_dir)
        self.enabled = bool(enabled)        # False in dry run: nothing is written, no file is created
        self.entries: list[tuple[OutputFile, str | list[str]]] = []   # every write() call, in call order
        self._lock = threading.Lock()

    def path(self, file: OutputFile) -> Path:
        """Full path of one output file (the names are the original's, 01 §14)."""
        return self.output_dir / OutputFile(file).value

    def write(self, file: OutputFile, entry: str | list[str]) -> None:
        """Append one entry (10 §5).

        Text files: `f.write(entry + "\\n")` (the entry may contain newlines; the templates that end in
        "\\n" therefore leave a blank line, 01 §7.1). Send2Bindu.csv: `csv.writer(f).writerow(entry)` with
        `newline=""`; a UTF-8 BOM is written first when the file is new. Opens and closes the file per call
        (crash-safe), encoding utf-8, guarded by a threading.Lock. A no-op when the writer is disabled.
        """
        file = OutputFile(file)
        with self._lock:
            self.entries.append((file, list(entry) if isinstance(entry, (list, tuple)) else entry))
            if not self.enabled:
                return
            self.output_dir.mkdir(parents=True, exist_ok=True)
            path = self.path(file)
            if file is OutputFile.BINDU:
                row = [entry] if isinstance(entry, str) else [str(cell) for cell in entry]
                new = _is_new(path)
                with open(path, "a", newline="", encoding="utf-8") as fh:
                    if new:
                        fh.write(UTF8_BOM)
                    csv.writer(fh).writerow(row)
            else:
                text = entry if isinstance(entry, str) else "\n".join(str(part) for part in entry)
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(text + "\n")

    def write_to_email(self, run_id: str, row: Sequence[str]) -> Path | None:
        """Append one `--no-email` row to ToEmail_<run_id>.csv (columns TO_EMAIL_COLUMNS); the header and a
        UTF-8 BOM are written when the file is created. Returns the path, or None when disabled (dry run)."""
        if len(row) != len(TO_EMAIL_COLUMNS):
            raise ValueError(f"a ToEmail row needs {len(TO_EMAIL_COLUMNS)} cells (got {len(row)})")
        if not self.enabled:
            return None
        path = to_email_path(self.output_dir, run_id)
        with self._lock:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            new = _is_new(path)
            with open(path, "a", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                if new:
                    fh.write(UTF8_BOM)
                    writer.writerow(TO_EMAIL_COLUMNS)
                writer.writerow([str(cell) for cell in row])
        return path


# ════════════════════════════════════════════════════════════════════════════════════════════════
# Run report (10 §7)
# ════════════════════════════════════════════════════════════════════════════════════════════════
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


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, (list, tuple)):
        return "; ".join(_csv_cell(v) for v in value)
    return str(value)


def _atomic_write_text(path: Path, text: str, *, encoding: str, newline: str | None = None) -> None:
    """Write via a temp file in the same directory + os.replace, so a rewrite never leaves half a report."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def write_report(output_dir: Path, run_id: str, rows: Sequence[Mapping[str, Any]],
                 meta: Mapping[str, Any]) -> tuple[Path, Path]:
    """Write (or rewrite) the run report: `reports/run_<run_id>.csv` with REPORT_COLUMNS (UTF-8 with BOM,
    one row per input row) and `reports/run_<run_id>.json` with `meta` (run_id, settings summary without
    secrets, phase timings, versions, …) plus the same rows. Returns (csv_path, json_path)."""
    csv_path, json_path = report_paths(output_dir, run_id)
    lines: list[list[str]] = [list(REPORT_COLUMNS)]
    lines.extend([_csv_cell(row.get(column)) for column in REPORT_COLUMNS] for row in rows)
    buffer: list[str] = []

    class _Sink:
        def write(self, text: str) -> int:
            buffer.append(text)
            return len(text)

    csv.writer(_Sink()).writerows(lines)
    _atomic_write_text(csv_path, UTF8_BOM + "".join(buffer), encoding="utf-8", newline="")
    document = {**dict(meta), "rows": [dict(row) for row in rows]}
    _atomic_write_text(json_path, json.dumps(document, indent=2, ensure_ascii=False, default=_json_default) + "\n",
                       encoding="utf-8")
    log.debug("Run report written: %s, %s", csv_path, json_path)
    return csv_path, json_path
