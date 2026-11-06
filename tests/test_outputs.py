"""OutputWriter (10 §5), the --no-email list and the run report (10 §7).

Exact bytes of every 01 §14 template (text files use the platform newline, like the original's plain
`open(..., "a")`; Send2Bindu.csv uses csv.writer's \\r\\n), a UTF-8 BOM only when the CSV is created,
append mode, and nothing at all in dry run (12-testing §2).
"""

from __future__ import annotations

import csv
import json
import os
import threading
from datetime import datetime
from pathlib import Path

import pytest

from eta_automations import texts
from eta_automations.errors import CarrierLookupError, NoEtaError
from eta_automations.models import OutputFile
from eta_automations.outputs import (
    REPORT_COLUMNS,
    TO_EMAIL_COLUMNS,
    UTF8_BOM,
    OutputWriter,
    report_paths,
    to_email_path,
    write_report,
)
from eta_automations.rules import classify, malformed_entry, parse_row

BOM = UTF8_BOM.encode("utf-8")
NL = os.linesep.encode("ascii")


def text_bytes(text: str) -> bytes:
    """What a text-mode append of `text` produces on this platform (\\n → os.linesep)."""
    return text.encode("utf-8").replace(b"\n", NL)


@pytest.fixture
def writer(output_dir: Path) -> OutputWriter:
    return OutputWriter(output_dir, enabled=True)


# ── 01 §14: every Failed_To_Process.txt template, byte for byte ──
FAILED_CASES = [
    pytest.param(
        malformed_entry(classify(parse_row(["EBKG10000018", "abc"], 1))),
        "[ERROR] Row {'inv_number': 'EBKG10000018', 'amount_due': 'abc'} - Malformed row (missing amount column or "
        "non-numeric amount).\ncould not convert string to float: 'abc'\n\n",
        id="malformed",
    ),
    pytest.param(
        malformed_entry(classify(parse_row(["EBKG10000018"], 1))),
        "[ERROR] Row {'inv_number': 'EBKG10000018', 'amount_due': ''} - Malformed row (missing amount column or "
        "non-numeric amount).\ncould not convert string to float: ''\n\n",
        id="malformed-one-column",
    ),
    pytest.param(texts.manual_entry("NAM1000017 C"), "NAM1000017 C\n", id="manual"),
    pytest.param(texts.carrier_lookup_failed_entry("EBKG10000019", CarrierLookupError("boom")),
                 "[ERROR] #EBKG10000019 - Carrier Lookup Failed.\nboom\n\n", id="carrier-lookup"),
    pytest.param(texts.cant_determine_eta_entry("EBKG10000020", NoEtaError("none")),
                 "[ERROR] #EBKG10000020 -  Can't determine ETA.\nnone\n\n", id="no-eta-two-spaces"),
    pytest.param(texts.unexpected_carrier_error_entry("EBKG10000022", RuntimeError("x")),
                 "[ERROR] #EBKG10000022 - Unexpected error during carrier lookup.\nx\n\n", id="unexpected"),
    pytest.param(texts.notif_lookup_failed_entry("EBKG10000024", "vship down"),
                 "[ERROR] #EBKG10000024 - VShip notification lookup failed, nothing sent\nvship down\n\n",
                 id="notif-lookup"),
    pytest.param(texts.done_in_last_week_entry("EBKG10000010 A"),
                 "[SKIP] #EBKG10000010 A - Done in the last week\n", id="done-last-week"),
    pytest.param(texts.not_found_in_quickbooks_entry("EBKG10000011"),
                 "[SKIP] #EBKG10000011 - not found in QuickBooks\n", id="not-found"),
    pytest.param(texts.duplicate_row_entry("EBKG10000029", 2),
                 "[SKIP] #EBKG10000029 - duplicate row in input file (line 2)\n", id="duplicate"),
    pytest.param(texts.qb_processing_error_entry("EBKG10000027", texts.BILL_EMAIL_MISSING_REASON),
                 "[ERROR] #EBKG10000027 - QB Invoice Processing Error, No Email Sent\n"
                 "Invoice has no customer e-mail address (BillEmail)\n\n", id="qb-processing"),
    pytest.param(texts.note_write_error_entry("EBKG10000023", "vship: no booking"),
                 "[ERROR] #EBKG10000023 - Note Write Error, QB Invoice Processing Successful\nvship: no booking\n\n",
                 id="note-write"),
    pytest.param(texts.booking_eta_change_failed_entry("EBKG10000026", "vship: HTTP 500"),
                 "[ERROR] #EBKG10000026 - Booking ETA Change Failed, QB Invoice Processing Successful\n"
                 "vship: HTTP 500\n\n", id="booking-eta-change"),
    pytest.param(texts.already_emailed_entry("EBKG10000028", datetime(2026, 11, 5, 8, 15)),
                 "[SKIP] #EBKG10000028 - already e-mailed today at 08:15 (ledger); use --force to resend\n",
                 id="ledger-guard"),
]


@pytest.mark.parametrize(("entry", "expected"), FAILED_CASES)
def test_failed_to_process_templates_exact_bytes(writer: OutputWriter, output_dir: Path, entry: str,
                                                 expected: str) -> None:
    writer.write(OutputFile.FAILED, entry)
    assert (output_dir / "Failed_To_Process.txt").read_bytes() == text_bytes(expected)


@pytest.mark.parametrize(("file", "entry", "expected"), [
    (OutputFile.BEN, texts.ben_entry("EBKG10000015 AA"), "EBKG10000015 AA\n"),
    (OutputFile.ACCOUNTING, texts.accounting_entry("EBKG10000005"),
     "EBKG10000005 - is partially paid and has a TR/SWB note\n"),
    (OutputFile.CUSTOMS, texts.customs_hold_entry("EBKG10000021"),
     "[ERROR] #EBKG10000021 -  Possible Customs Hold.\n"),
])
def test_other_text_files_exact_bytes(writer: OutputWriter, output_dir: Path, file: OutputFile, entry: str,
                                      expected: str) -> None:
    writer.write(file, entry)
    assert (output_dir / file.value).read_bytes() == text_bytes(expected)
    assert writer.path(file) == output_dir / file.value


def test_file_names_are_the_originals(writer: OutputWriter, output_dir: Path) -> None:
    assert {f.value for f in OutputFile} == {"Send2Bindu.csv", "Send2Ben.txt", "Send2Accounting.txt",
                                             "Failed_To_Process.txt", "PotentialCustomsHold.txt"}


def test_text_files_are_appended_never_truncated(writer: OutputWriter, output_dir: Path) -> None:
    path = output_dir / "Send2Ben.txt"
    path.write_bytes(text_bytes("FROM AN EARLIER RUN\n"))
    writer.write(OutputFile.BEN, "EBKG1 AA")
    writer.write(OutputFile.BEN, "EBKG2 AA")
    assert path.read_bytes() == text_bytes("FROM AN EARLIER RUN\nEBKG1 AA\nEBKG2 AA\n")
    # a second writer (next run) appends too
    OutputWriter(output_dir, enabled=True).write(OutputFile.BEN, "EBKG3 AA")
    assert path.read_text(encoding="utf-8").splitlines() == ["FROM AN EARLIER RUN", "EBKG1 AA", "EBKG2 AA", "EBKG3 AA"]


def test_bindu_csv_bom_only_when_the_file_is_created(writer: OutputWriter, output_dir: Path) -> None:
    writer.write(OutputFile.BINDU, texts.bindu_row("EBKG10000013", "850", "ACME"))
    path = output_dir / "Send2Bindu.csv"
    assert path.read_bytes() == BOM + b"EBKG10000013,850,ACME\r\n"
    writer.write(OutputFile.BINDU, texts.bindu_row("EBKG10000014 AA", "1,850.00", "Smith, John"))
    OutputWriter(output_dir, enabled=True).write(OutputFile.BINDU, texts.bindu_row("EBKG3", "10", ""))
    assert path.read_bytes() == (BOM + b"EBKG10000013,850,ACME\r\n"
                                 + b'EBKG10000014 AA,"1,850.00","Smith, John"\r\n'
                                 + b"EBKG3,10,\r\n")
    assert path.read_bytes().count(BOM) == 1


def test_bindu_csv_existing_file_without_bom_gets_none(writer: OutputWriter, output_dir: Path) -> None:
    path = output_dir / "Send2Bindu.csv"
    path.write_bytes(b"OLD,1,X\r\n")
    writer.write(OutputFile.BINDU, ["EBKG1", "2", "Y"])
    assert path.read_bytes() == b"OLD,1,X\r\nEBKG1,2,Y\r\n"


def test_bindu_csv_empty_existing_file_counts_as_new(writer: OutputWriter, output_dir: Path) -> None:
    path = output_dir / "Send2Bindu.csv"
    path.write_bytes(b"")
    writer.write(OutputFile.BINDU, ["EBKG1", "2", "Y"])
    assert path.read_bytes() == BOM + b"EBKG1,2,Y\r\n"


def test_unicode_entries_are_utf8(writer: OutputWriter, output_dir: Path) -> None:
    writer.write(OutputFile.BINDU, ["EBKG1", "900", "Société Générale — Café"])
    writer.write(OutputFile.FAILED, texts.carrier_lookup_failed_entry("EBKG2", "No carrier pattern matched — x"))
    assert (output_dir / "Send2Bindu.csv").read_bytes() == BOM + "EBKG1,900,Société Générale — Café\r\n".encode()
    assert (output_dir / "Failed_To_Process.txt").read_text(encoding="utf-8") == (
        "[ERROR] #EBKG2 - Carrier Lookup Failed.\nNo carrier pattern matched — x\n\n")


def test_disabled_writer_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "not-created"
    writer = OutputWriter(out, enabled=False)
    for file in OutputFile:
        writer.write(file, ["a", "b", "c"] if file is OutputFile.BINDU else "entry")
    assert writer.write_to_email("20261105_093000", ["x"] * len(TO_EMAIL_COLUMNS)) is None
    assert not out.exists()
    assert [f for f, _ in writer.entries] == list(OutputFile)   # what WOULD have been written


def test_output_dir_is_created_when_missing(tmp_path: Path) -> None:
    out = tmp_path / "a" / "b"
    OutputWriter(out, enabled=True).write(OutputFile.BEN, "X")
    assert (out / "Send2Ben.txt").read_text(encoding="utf-8") == "X\n"


def test_concurrent_writes_are_not_interleaved(writer: OutputWriter, output_dir: Path) -> None:
    def worker(n: int) -> None:
        for i in range(50):
            writer.write(OutputFile.FAILED, texts.carrier_lookup_failed_entry(f"T{n}-{i}", "boom"))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    blocks = (output_dir / "Failed_To_Process.txt").read_text(encoding="utf-8").split("\n\n")
    blocks = [b for b in blocks if b]
    assert len(blocks) == 400
    assert all(b.startswith("[ERROR] #T") and b.endswith("Carrier Lookup Failed.\nboom") for b in blocks)


# ── ToEmail_<run_id>.csv (10 Phase 3 step 1) ──
def test_to_email_csv_header_bom_and_rows(writer: OutputWriter, output_dir: Path) -> None:
    row = ["EBKG10000001", "1001", "ACME Imports", "billing@acme.example", "",
           "Invoice for Booking no. EBKG10000001. ****ARRIVED ON 11/03. PLEASE PLEASE PAY AND CLEAR THE CARGO****",
           "EBKG10000001 A", "ARRIVED ON 11/03. NOTIF #1"]
    path = writer.write_to_email("20261105_093000", row)
    assert path == to_email_path(output_dir, "20261105_093000") == output_dir / "ToEmail_20261105_093000.csv"
    writer.write_to_email("20261105_093000", ["2"] * 8)
    data = path.read_bytes()
    assert data.startswith(BOM) and data.count(BOM) == 1
    lines = data[len(BOM):].decode("utf-8").split("\r\n")
    assert lines[0] == ",".join(TO_EMAIL_COLUMNS) == (
        "doc_number,invoice_id,customer,to,cc,subject,doc_number_after_sending,vship_note_after_sending")
    assert lines[1] == ",".join(row)
    assert lines[2] == ",".join(["2"] * 8)
    with pytest.raises(ValueError):
        writer.write_to_email("20261105_093000", ["too", "short"])


# ── run report (10 §7) ──
def test_write_report_csv_and_json(output_dir: Path) -> None:
    rows = [
        {"line_no": 1, "doc_number": "EBKG1", "amount": "2,450.00", "in_window": True, "notif_count": None,
         "eta": datetime(2026, 11, 3).isoformat(), "route": "NOTIFY", "status": "PENDING", "message": "a\nb"},
        {"line_no": 2, "doc_number": "EBKG2", "route": "BEN", "warnings": "w1 | w2"},
    ]
    meta = {"run_id": "20261105_093000", "phase_timings": {"plan": 1.5}, "when": datetime(2026, 11, 5, 9, 30),
            "path": Path("x")}
    csv_path, json_path = write_report(output_dir, "20261105_093000", rows, meta)
    assert (csv_path, json_path) == report_paths(output_dir, "20261105_093000")
    assert csv_path == output_dir / "reports" / "run_20261105_093000.csv"
    data = csv_path.read_bytes()
    assert data.startswith(BOM)
    parsed = list(csv.reader(data[len(BOM):].decode("utf-8").splitlines(keepends=True)))
    assert parsed[0] == list(REPORT_COLUMNS)
    first = dict(zip(REPORT_COLUMNS, parsed[1], strict=True))
    assert first["doc_number"] == "EBKG1" and first["amount"] == "2,450.00" and first["in_window"] == "True"
    assert first["notif_count"] == "" and first["message"] == "a\nb" and first["eta"] == "2026-11-03T00:00:00"
    document = json.loads(json_path.read_text(encoding="utf-8"))
    assert document["run_id"] == "20261105_093000"
    assert document["when"] == "2026-11-05T09:30:00" and document["path"] == "x"
    assert [r["doc_number"] for r in document["rows"]] == ["EBKG1", "EBKG2"]
    # a rewrite replaces both files completely and leaves no temporary file behind
    write_report(output_dir, "20261105_093000", rows[:1], {"run_id": "20261105_093000", "report_stage": "final"})
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_stage"] == "final"
    assert len(csv_path.read_text(encoding="utf-8-sig").splitlines()) == 3   # header + a two-line message
    assert sorted(p.name for p in (output_dir / "reports").iterdir()) == [csv_path.name, json_path.name]


def test_write_report_writes_each_file_on_its_own(output_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """XCT-04: a CSV that cannot be written does not stop the JSON (written first); the error is raised afterwards."""
    from eta_automations import outputs
    real_replace = outputs.os.replace

    def replace(src: str, dst: str) -> None:
        if str(dst).endswith(".csv"):
            raise OSError(28, "No space left on device")
        real_replace(src, dst)

    monkeypatch.setattr(outputs.os, "replace", replace)
    with pytest.raises(OSError, match="No space left"):
        write_report(output_dir, "20261105_093000", [{"doc_number": "EBKG1"}], {"report_stage": "final"})
    csv_path, json_path = report_paths(output_dir, "20261105_093000")
    assert json.loads(json_path.read_text(encoding="utf-8"))["report_stage"] == "final" and not csv_path.exists()
    assert [p.name for p in (output_dir / "reports").iterdir()] == [json_path.name]     # no temporary file left


def test_report_columns_are_the_spec_list() -> None:
    assert REPORT_COLUMNS == (
        "line_no", "doc_number", "amount", "kind", "base_booking", "carrier", "eta", "eta_source", "eta_is_actual",
        "eta_rescue", "cutoff", "in_window", "notif_count", "notif_defaulted", "invoice_id", "balance", "total",
        "bill_email", "route", "new_doc_number", "subject", "vship_booking_id", "actions_planned", "actions_done",
        "status", "message", "warnings")
