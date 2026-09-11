from __future__ import annotations

from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.approval import TaskStatus
from app.letters.models import DocumentRecord, DocumentStatus, DocumentType
from tests.helpers.dashboard import TEST_USERS, build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def _login(client: TestClient, role: str = "practitioner") -> dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"username": TEST_USERS[role][0].username, "password": TEST_USERS[role][1]},
    )
    assert resp.status_code == 200, resp.text
    return {"X-CSRF-Token": resp.json()["csrf_token"]}


def test_request_certificate_draft_success(client: TestClient) -> None:
    headers = _login(client, "practitioner")
    resp = client.post(
        "/api/documents/request",
        headers=headers,
        json={
            "patient_id": "pat_1001",
            "document_type": "certificate",
            "certificate_type": "attendance",
            "notes": "Patient attended 45 min physiotherapy session.",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["type"] == "certificate"
    assert data["status"] == "pending_review"
    assert data["patient_id"] == "pat_1001"
    assert data["safe_draft"]["certificate_type"] == "attendance"


def test_request_referral_thank_you_draft_success(client: TestClient) -> None:
    headers = _login(client, "staff")
    resp = client.post(
        "/api/documents/request",
        headers=headers,
        json={
            "patient_id": "pat_1001",
            "document_type": "referral_thank_you",
            "notes": "Initial assessment complete. Treatment plan commenced.",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["type"] == "letter"
    assert data["safe_draft"]["letter_type"] == "referral_thank_you"


def test_request_treatment_completion_draft_success(client: TestClient) -> None:
    headers = _login(client, "practitioner")
    resp = client.post(
        "/api/documents/request",
        headers=headers,
        json={
            "patient_id": "pat_1001",
            "document_type": "treatment_completion",
            "notes": "Goals achieved, discharged to home exercise.",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["safe_draft"]["letter_type"] == "treatment_completion"


def test_request_document_patient_not_found(client: TestClient) -> None:
    headers = _login(client, "staff")
    resp = client.post(
        "/api/documents/request",
        headers=headers,
        json={
            "patient_id": "nonexistent_patient_999",
            "document_type": "certificate",
        },
    )
    assert resp.status_code == 404


def test_document_pdf_download(client: TestClient, env) -> None:
    headers = _login(client, "practitioner")

    # Inject an approved rendered document into document_store
    doc = DocumentRecord(
        document_id="test_doc_pdf_123",
        document_type=DocumentType.CERTIFICATE,
        patient_id="pat_1001",
        task_id="task_pdf_123",
        template_id="certificate",
        status=DocumentStatus.STORED,
        created_at="2026-09-11T12:00:00+00:00",
    )
    pdf_bytes = b"%PDF-1.4 test certificate content"
    env.container.document_store.save(doc, content=pdf_bytes)

    resp = client.get("/documents/test_doc_pdf_123/pdf", headers=headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content == pdf_bytes
