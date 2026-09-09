from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from app.doc_intake import DocumentIntake, UploadedFile
from app.nookal_client import MockNookalClient, PatientRef


class FakeClassifier:
    def __init__(self) -> None:
        self.calls = 0

    def classify_document(self, *, filename: str, mime_type: str) -> tuple[str, float]:
        self.calls += 1
        return "other", 0.55


def _audit_events() -> tuple[list[tuple], callable]:
    events: list[tuple] = []

    def audit(*args: object, **kwargs: object) -> None:
        events.append((args, kwargs))

    return events, audit


def _client(*patients: PatientRef) -> MockNookalClient:
    client = MockNookalClient(audit=lambda *args, **kwargs: None)
    for patient in patients:
        client.seed_patient(patient)
    return client


def test_intake_preserves_bytes_and_matches_name_and_dob(tmp_path: Path) -> None:
    events, audit = _audit_events()
    client = _client(
        PatientRef("p1", display_name="Synthetic Patient", date_of_birth=date(1990, 2, 3))
    )
    uploaded = UploadedFile("referral.pdf", b"synthetic document", "application/pdf")

    record = DocumentIntake(client, tmp_path, audit=audit).intake(
        uploaded,
        "upload",
        patient_name="synthetic patient",
        patient_date_of_birth=date(1990, 2, 3),
    )

    assert record.status == "draft"
    assert record.patient_match.status == "matched"
    assert record.patient_match.patient_id == "p1"
    assert Path(record.preserved_path).read_bytes() == uploaded.content
    assert record.document_type == "referral"
    metadata = events[-1][1]["metadata"]
    assert "Synthetic Patient" not in str(metadata)
    assert "referral.pdf" not in str(metadata)


def test_intake_routes_below_threshold_to_ambiguous(tmp_path: Path) -> None:
    client = _client(
        PatientRef("p1", display_name="Synthetic Patient"),
        PatientRef("p2", display_name="Synthetic Patient"),
    )

    record = DocumentIntake(client, tmp_path).intake(
        UploadedFile("unknown.bin", b"synthetic"),
        "email",
        patient_name="Synthetic Patient",
    )

    assert record.patient_match.status == "ambiguous"
    assert [candidate.patient_id for candidate in record.patient_match.candidates] == ["p1", "p2"]


def test_intake_uses_injected_classifier_only_after_rule_failure(tmp_path: Path) -> None:
    classifier = FakeClassifier()
    client = _client()

    record = DocumentIntake(client, tmp_path, classifier=classifier).intake(
        UploadedFile("scan.bin", b"synthetic", "application/octet-stream"),
        "other",
    )

    assert record.document_type == "other"
    assert record.classification_source == "llm"
    assert classifier.calls == 1

    ruled = DocumentIntake(client, tmp_path / "ruled", classifier=classifier).intake(
        UploadedFile("certificate.pdf", b"synthetic", "application/pdf"),
        "upload",
    )
    assert ruled.document_type == "certificate"
    assert ruled.classification_source == "rule"
    assert classifier.calls == 1


def test_intake_without_candidates_is_unmatched_and_audited(tmp_path: Path) -> None:
    events, audit = _audit_events()
    record = DocumentIntake(_client(), tmp_path, audit=audit).intake(
        UploadedFile("note.txt", b"synthetic", "text/plain"),
        "upload",
        patient_name="No Such Synthetic Patient",
        patient_date_of_birth=date(2000, 1, 1),
    )

    assert record.patient_match.status == "unmatched"
    assert record.document_type == "unknown"
    assert events[-1][0][4] == "success"
    assert events[-1][1]["metadata"]["match_status"] == "unmatched"
