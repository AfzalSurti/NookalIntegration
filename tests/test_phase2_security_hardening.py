from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.approval import ApprovalQueue
from app.case_tracking import CaseTracking
from app.doc_filing import DocumentFiler, FakeDriveAdapter
from app.doc_intake import DocumentIntake, PatientMatch, PatientCandidate, UploadedFile
from app.finance_categorize import FinanceCategorizer
from app.finance_extract import ExpenseRecord
from app.finance_register import FinanceRegister
from app.nookal_client import Appointment, MockNookalClient, PatientRef
from app.shared import kill_switch
from app.shared.exceptions import KillSwitchActive


def _record(tmp_path: Path, *, match: PatientMatch | None = None) -> object:
    client = MockNookalClient(audit=lambda *args, **kwargs: None)
    client.seed_patient(PatientRef("p1", display_name="Synthetic Patient", date_of_birth=date(1990, 1, 1)))
    return client, DocumentIntake(client, tmp_path, audit=lambda *args, **kwargs: None).intake(
        UploadedFile("synthetic.pdf", b"synthetic content", "application/pdf"),
        "upload", patient_name="Synthetic Patient", patient_date_of_birth=date(1990, 1, 1)
    )


def test_document_filing_requires_match_or_human_resolution(tmp_path: Path) -> None:
    client, record = _record(tmp_path)
    unresolved = record.__class__(**{**record.__dict__, "patient_match": PatientMatch("ambiguous", candidates=(PatientCandidate("p1", 0.5),))})
    audit: list[tuple] = []
    with pytest.raises(ValueError):
        DocumentFiler(client, audit=lambda *args, **kwargs: audit.append((args, kwargs))).file(unresolved, filed_by="staff")
    assert len(client.documents) == 0
    assert audit[-1][0][4] == "failure"


def test_expense_register_only_accepts_confirmed_fields_and_traceability(tmp_path: Path) -> None:
    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"synthetic")
    expense = ExpenseRecord("doc_1", str(source), "2026-09-01", "Synthetic Supplier", "10.00", None, None, "office", None, (), 0.9, "pending_review", "task_1", "now")
    with pytest.raises(ValueError):
        FinanceRegister(tmp_path / "register.xlsx").append(expense)
    confirmed = ExpenseRecord(**{**expense.__dict__, "status": "extracted", "gst": "1.00"})
    row = FinanceRegister(tmp_path / "register.xlsx", audit=lambda *a, **k: None).append(confirmed, filed_location="drive://synthetic")
    assert row["gst"] == "1.00"
    assert row["source_document_id"] == "doc_1"
    assert row["filed_location"] == "drive://synthetic"


def test_five_session_flag_does_not_write_or_send(tmp_path: Path) -> None:
    client = MockNookalClient(audit=lambda *args, **kwargs: None)
    client.seed_patient(PatientRef("p1"))
    for index in range(5):
        client.seed_appointment(Appointment(f"a{index}", "p1", datetime(2026, 1, index + 1, tzinfo=timezone.utc), status="completed"))
    tracker = CaseTracking(client, audit=lambda *args, **kwargs: None)
    flagged = tracker.refresh(case_id="case_1", patient_id="p1")
    assert flagged.flag_raised
    assert client.documents == []
    assert client.appointments["a0"].status == "completed"


def test_sensitive_actions_are_audited(tmp_path: Path) -> None:
    events: list[tuple[tuple, dict]] = []
    client, record = _record(tmp_path)
    filer = DocumentFiler(client, audit=lambda *args, **kwargs: events.append((args, kwargs)))
    filer.file(record, filed_by="staff")
    filer.cleanup_preserved(record, actor="staff")
    assert {event[0][1] for event in events} == {"file_document", "cleanup_preserved_document"}


def test_kill_switch_blocks_filing_categorization_and_register(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    switch_path = tmp_path / "KILL_SWITCH"
    monkeypatch.setattr(kill_switch, "switch_path", lambda path=None: switch_path)
    kill_switch.activate(switch_path, reason="synthetic test")
    client, document = _record(tmp_path)
    with pytest.raises(KillSwitchActive):
        DocumentFiler(client).file(document, filed_by="staff")

    source = tmp_path / "receipt.pdf"
    source.write_bytes(b"synthetic")
    expense = ExpenseRecord("doc_2", str(source), "2026-09-01", "Synthetic", "1.00", None, None, None, None, (), 1.0, "extracted", None, "now")
    with pytest.raises(KillSwitchActive):
        FinanceCategorizer(FakeDriveAdapter(), categories=["office"]).categorize(expense, category="office", categorized_by="staff")
    with pytest.raises(KillSwitchActive):
        FinanceRegister(tmp_path / "register.xlsx").append(ExpenseRecord(**{**expense.__dict__, "category": "office"}))