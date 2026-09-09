from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.doc_filing import DocumentFiler, DocumentFilingError, FakeDriveAdapter
from app.doc_intake import DocumentIntake, UploadedFile
from app.nookal_client import MockNookalClient, PatientRef


def _record(tmp_path: Path):
    client = MockNookalClient(audit=lambda *args, **kwargs: None)
    client.seed_patient(
        PatientRef("p1", display_name="Synthetic Patient", date_of_birth=date(1990, 2, 3))
    )
    return client, DocumentIntake(client, tmp_path, audit=lambda *args, **kwargs: None).intake(
        UploadedFile("referral.pdf", b"synthetic document", "application/pdf"),
        "upload",
        patient_name="Synthetic Patient",
        patient_date_of_birth=date(1990, 2, 3),
    )


def test_files_unchanged_original_to_nookal_and_drive(tmp_path: Path) -> None:
    client, record = _record(tmp_path)
    drive = FakeDriveAdapter()
    filed = DocumentFiler(client, drive=drive, audit=lambda *args, **kwargs: None).file(
        record, filed_by="synthetic-staff", to_drive=True
    )

    assert filed.filing_status == "filed"
    assert filed.filed_to == ("nookal", "drive")
    assert filed.filed_by == "synthetic-staff"
    assert client.documents[0].patient_id == "p1"
    assert drive.documents == [("referral.pdf", b"synthetic document", "application/pdf")]
    assert Path(record.preserved_path).read_bytes() == b"synthetic document"


def test_unresolved_patient_cannot_be_filed(tmp_path: Path) -> None:
    client = MockNookalClient(audit=lambda *args, **kwargs: None)
    record = DocumentIntake(client, tmp_path, audit=lambda *args, **kwargs: None).intake(
        UploadedFile("note.txt", b"synthetic", "text/plain"), "upload"
    )

    with pytest.raises(ValueError, match="human-confirmed"):
        DocumentFiler(client).file(record, filed_by="synthetic-staff")


def test_partial_failure_returns_failed_record_with_completed_target(tmp_path: Path) -> None:
    client, record = _record(tmp_path)
    drive = FakeDriveAdapter()
    drive.fail = True

    with pytest.raises(DocumentFilingError) as caught:
        DocumentFiler(client, drive=drive, audit=lambda *args, **kwargs: None).file(
            record, filed_by="synthetic-staff", to_drive=True
        )

    failed = caught.value.record
    assert failed.filing_status == "failed"
    assert failed.filed_to == ("nookal",)
    assert len(client.documents) == 1
    assert drive.documents == []
