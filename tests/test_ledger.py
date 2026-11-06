"""Ledger (10 §9, D20): record / emailed_on, the start-up index, corrupt lines skipped with a WARNING, and no
writes at all when disabled (dry run)."""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from eta_automations.clock import FixedClock
from eta_automations.ledger import EMAILED, LEDGER_FILE_NAME, Ledger

TODAY = date(2026, 11, 5)


@pytest.fixture
def ledger_path(state_dir: Path) -> Path:
    return state_dir / LEDGER_FILE_NAME


def record_email(ledger: Ledger, invoice_id: str | None = "1001", *, sent_at: str | None = "2026-11-05T08:15:00",
                 ok: bool = True, action: str = EMAILED, doc: str = "EBKG10000001") -> None:
    detail: dict[str, Any] = {"strategy": "template_swap", "to": ["billing@acme.example"]}
    if sent_at is not None:
        detail["sent_at"] = sent_at
    ledger.record(run_id="20261105_093000", doc_number=doc, invoice_id=invoice_id, base_booking="EBKG10000001",
                  action=action, detail=detail, ok=ok)


def lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_record_appends_one_json_line_and_fsyncs(ledger_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    synced: list[int] = []
    real_fsync = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real_fsync(fd))[1])
    ledger = Ledger(ledger_path, clock=FixedClock(datetime(2026, 11, 5, 9, 31, 7)))
    ledger.record(run_id="20261105_093000", doc_number="EBKG10000001", invoice_id="1001",
                  base_booking="EBKG10000001", action="qbo_update",
                  detail={"eta": date(2026, 11, 3), "doc_number_before": "EBKG10000001",
                          "doc_number_after": "EBKG10000001 A"}, ok=True)
    record_email(ledger)
    assert len(synced) == 2
    records = lines(ledger_path)
    assert records[0] == {
        "ts": "2026-11-05T09:31:07", "run_id": "20261105_093000", "doc_number": "EBKG10000001",
        "invoice_id": "1001", "base_booking": "EBKG10000001", "action": "qbo_update", "ok": True,
        "detail": {"eta": "2026-11-03", "doc_number_before": "EBKG10000001", "doc_number_after": "EBKG10000001 A"},
    }
    assert records[1]["action"] == "emailed" and records[1]["detail"]["sent_at"] == "2026-11-05T08:15:00"
    assert ledger_path.read_bytes().count(b"\n") == 2   # one line per record, '\n' on every platform


def test_emailed_on_same_day_other_day_other_invoice(ledger_path: Path) -> None:
    ledger = Ledger(ledger_path)
    assert ledger.emailed_on("1001", TODAY) is None
    record_email(ledger)
    assert ledger.emailed_on("1001", TODAY) == datetime(2026, 11, 5, 8, 15)
    assert ledger.emailed_on("1001", TODAY - timedelta(days=1)) is None
    assert ledger.emailed_on("1002", TODAY) is None


def test_index_is_loaded_at_start_up(ledger_path: Path) -> None:
    record_email(Ledger(ledger_path))
    assert Ledger(ledger_path).emailed_on("1001", TODAY) == datetime(2026, 11, 5, 8, 15)


def test_only_successful_email_events_count(ledger_path: Path) -> None:
    ledger = Ledger(ledger_path)
    record_email(ledger, ok=False)
    record_email(ledger, action="qbo_update")
    record_email(ledger, invoice_id=None)
    assert ledger.emailed_on("1001", TODAY) is None
    assert Ledger(ledger_path).emailed_on("1001", TODAY) is None


def test_latest_send_of_the_day_is_returned(ledger_path: Path) -> None:
    ledger = Ledger(ledger_path)
    record_email(ledger, sent_at="2026-11-05T07:00:00")
    record_email(ledger, sent_at="2026-11-05T11:45:00")
    record_email(ledger, sent_at="2026-11-04T23:59:00")
    assert ledger.emailed_on("1001", TODAY) == datetime(2026, 11, 5, 11, 45)


def test_record_time_is_used_without_sent_at(ledger_path: Path) -> None:
    ledger = Ledger(ledger_path, clock=FixedClock(datetime(2026, 11, 5, 10, 2)))
    record_email(ledger, sent_at=None)
    assert ledger.emailed_on("1001", TODAY) == datetime(2026, 11, 5, 10, 2)


def test_aware_sent_at_is_converted_to_local_time(ledger_path: Path) -> None:
    aware = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
    record_email(Ledger(ledger_path), sent_at=aware.isoformat())
    expected = aware.astimezone().replace(tzinfo=None)
    assert Ledger(ledger_path).emailed_on("1001", expected.date()) == expected


def test_corrupt_lines_are_skipped_with_a_warning(ledger_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    good = {"ts": "2026-11-05T08:00:00", "run_id": "r", "doc_number": "EBKG1", "invoice_id": "1001",
            "base_booking": "EBKG1", "action": "emailed", "ok": True, "detail": {"sent_at": "2026-11-05T08:15:00"}}
    ledger_path.write_text("this is not json\n[1, 2]\n\n" + json.dumps(good) + "\n{\"truncated\": \n",
                           encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="eta.pipeline"):
        ledger = Ledger(ledger_path)
    assert ledger.emailed_on("1001", TODAY) == datetime(2026, 11, 5, 8, 15)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert any("line 1 is not valid JSON" in w for w in warnings)
    assert any("line 2 is not a JSON object" in w for w in warnings)
    assert any("line 5 is not valid JSON" in w for w in warnings)


def test_unreadable_time_is_ignored_with_a_warning(ledger_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    bad = {"ts": "yesterday", "invoice_id": "1001", "action": "emailed", "ok": True, "detail": {"sent_at": 5}}
    ledger_path.write_text(json.dumps(bad) + "\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="eta.pipeline"):
        assert Ledger(ledger_path).emailed_on("1001", TODAY) is None
    assert any("no readable time" in r.getMessage() for r in caplog.records)


def test_disabled_ledger_never_writes(ledger_path: Path) -> None:
    ledger = Ledger(ledger_path, enabled=False)
    record_email(ledger)
    assert not ledger_path.exists()
    assert ledger.emailed_on("1001", TODAY) is None


def test_disabled_ledger_still_reads_the_guard(ledger_path: Path) -> None:
    record_email(Ledger(ledger_path))
    before = ledger_path.read_bytes()
    dry = Ledger(ledger_path, enabled=False)
    record_email(dry, invoice_id="1002")
    assert dry.emailed_on("1001", TODAY) == datetime(2026, 11, 5, 8, 15)
    assert ledger_path.read_bytes() == before


def test_detail_is_redacted_and_json_safe(ledger_path: Path) -> None:
    Ledger(ledger_path).record(run_id="r", doc_number="EBKG1", invoice_id="1", base_booking="EBKG1",
                               action="vship_comment",
                               detail={"authorization": "Bearer abc.def", "error": "GET /x?access_token=s3cr3t",
                                       "when": datetime(2026, 11, 5, 9, 30), "ids": ("a", "b")}, ok=False)
    text = ledger_path.read_text(encoding="utf-8")
    assert "abc.def" not in text and "s3cr3t" not in text
    detail = lines(ledger_path)[0]["detail"]
    assert detail["authorization"] == "***"
    assert detail["when"] == "2026-11-05T09:30:00" and detail["ids"] == ["a", "b"]


def test_missing_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "new" / "state" / LEDGER_FILE_NAME
    record_email(Ledger(path))
    assert len(lines(path)) == 1
