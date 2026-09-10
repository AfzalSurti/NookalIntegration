"""
Regression and verification tests for Nookal data display in the Back to Ease dashboard.

Validates end-to-end data flow:
Nookal API / Client -> Services (CaseService, PatientFileService, InvoiceService)
-> Dashboard Views & Dependencies -> Jinja Templates -> Rendered HTML.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dashboard.production import build_production_container
from app.nookal_client import (
    CaseRef,
    HttpNookalClient,
    Invoice,
    InvoiceEntry,
    PatientFile,
)
from app.shared.config import Settings, get_settings
from app.shared.exceptions import NookalServerError
from tests.helpers.dashboard import build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def test_patient_detail_displays_nookal_cases_files_invoices(client: TestClient, env) -> None:
    # 1. Populate Nookal mock with Case, Patient File, and Invoice
    env.nookal.cases["case_c100"] = CaseRef(
        case_id="case_c100",
        patient_id="pat_1001",
        case_name="Rotator Cuff Rehabilitation",
        case_number="C-100",
        status="Open",
        date_created="2026-08-01",
        closed_date=None,
    )
    env.nookal.files["file_f200"] = PatientFile(
        file_id="file_f200",
        patient_id="pat_1001",
        name="shoulder_mri_scan.pdf",
        file_type="pdf",
        date_added="2026-08-05",
        size=40960,
    )
    env.nookal.invoices["inv_i300"] = Invoice(
        invoice_id="inv_i300",
        patient_id="pat_1001",
        date="2026-08-10",
        total=175.50,
        status="Paid",
        void=False,
        entries=[
            InvoiceEntry(
                entry_id="e_1",
                invoice_id="inv_i300",
                description="Comprehensive Physio Consultation",
                price=175.50,
                quantity=1.0,
            )
        ],
    )

    # 2. View patient detail page
    resp = env.authed(client, "GET", "/patients/pat_1001", role="practitioner")
    assert resp.status_code == 200
    html = resp.text

    # 3. Verify Cases are displayed
    assert "Rotator Cuff Rehabilitation" in html
    assert "case_c100" in html
    assert "C-100" in html
    assert "Open" in html

    # 4. Verify Patient Files are displayed
    assert "shoulder_mri_scan.pdf" in html
    assert "file_f200" in html
    assert "40.0 KB" in html
    assert "/patients/pat_1001/files/file_f200/url" in html

    # 5. Verify Invoices are displayed
    assert "inv_i300" in html
    assert "$175.50" in html
    assert "Paid" in html


def test_cases_page_displays_nookal_cases(client: TestClient, env) -> None:
    env.nookal.cases["case_c101"] = CaseRef(
        case_id="case_c101",
        patient_id="pat_1001",
        case_name="Lumbar Spine Care",
        case_number="C-101",
        status="Open",
        date_created="2026-08-15",
    )

    resp = env.authed(client, "GET", "/cases", role="admin")
    assert resp.status_code == 200
    html = resp.text

    assert "Lumbar Spine Care" in html
    assert "case_c101" in html
    assert "C-101" in html
    assert "pat_1001" in html


def test_documents_page_displays_nookal_patient_files_and_download(client: TestClient, env) -> None:
    env.nookal.files["file_f205"] = PatientFile(
        file_id="file_f205",
        patient_id="pat_1001",
        name="cervical_spine_xray.pdf",
        file_type="pdf",
        date_added="2026-08-20",
        size=1048576,
    )

    # Query /documents with patient_id filter
    resp = env.authed(client, "GET", "/documents", role="practitioner", params={"patient_id": "pat_1001"})
    assert resp.status_code == 200
    html = resp.text

    assert "cervical_spine_xray.pdf" in html
    assert "file_f205" in html
    assert "1024.0 KB" in html
    assert "/patients/pat_1001/files/file_f205/url" in html

    # Test file download redirect
    csrf = env.login(client, "practitioner")
    dl_resp = client.get(
        "/patients/pat_1001/files/file_f205/url",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )
    assert dl_resp.status_code == 303
    assert "nookal-files" in dl_resp.headers["location"]


def test_finance_invoices_page_displays_nookal_invoices(client: TestClient, env) -> None:
    env.nookal.invoices["inv_i305"] = Invoice(
        invoice_id="inv_i305",
        patient_id="pat_1001",
        date="2026-08-25",
        total=220.00,
        status="Unpaid",
        void=False,
        entries=[
            InvoiceEntry(
                entry_id="e_5",
                invoice_id="inv_i305",
                description="Dry Needling Therapy",
                price=220.00,
                quantity=1.0,
            )
        ],
    )

    resp = env.authed(client, "GET", "/finance/invoices", role="admin")
    assert resp.status_code == 200
    html = resp.text

    assert "inv_i305" in html
    assert "pat_1001" in html
    assert "$220.00" in html
    assert "Unpaid" in html
    assert "1 items" in html


def test_empty_results_display_friendly_empty_state(client: TestClient, env) -> None:
    """Empty cases, files, and invoices display 'No ... found' without error banners."""
    # pat_1002 is seeded in clinic_seed.json and has 0 cases, 0 files, 0 invoices
    resp = env.authed(client, "GET", "/patients/pat_1002", role="practitioner")
    assert resp.status_code == 200
    html = resp.text

    assert "No cases found" in html
    assert "No patient files found" in html
    assert "No invoices found" in html
    assert '<div class="alert alert-error">' not in html


def test_nookal_api_error_displays_safe_alert_banner(client: TestClient, env, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate Nookal upstream failure on get_cases
    def failing_get_cases(*args, **kwargs):
        raise NookalServerError("Nookal 500: Database connection error with secrets key=SECRET_123")

    monkeypatch.setattr(env.nookal, "get_cases", failing_get_cases)

    resp = env.authed(client, "GET", "/patients/pat_1001", role="practitioner")
    assert resp.status_code == 200
    html = resp.text

    # Must display safe user-facing error message, NOT misleading "No cases found"
    assert "Unable to load clinical cases from Nookal at this time." in html
    assert "No cases found" not in html
    # Secret discipline: error details containing simulated keys must NEVER appear in HTML
    assert "SECRET_123" not in html


def test_secret_discipline_presigned_urls_not_logged_in_audit(client: TestClient, env) -> None:
    env.nookal.files["file_f210"] = PatientFile(
        file_id="file_f210",
        patient_id="pat_1001",
        name="report.pdf",
        file_type="pdf",
        date_added="2026-08-25",
        size=1024,
    )

    csrf = env.login(client, "practitioner")
    client.get(
        "/patients/pat_1001/files/file_f210/url",
        headers={"X-CSRF-Token": csrf},
        follow_redirects=False,
    )

    # Inspect audit events
    audit_events = env.audit.events
    dl_events = [e for e in audit_events if e.action == "dashboard.patient_file_download"]
    assert len(dl_events) >= 1
    event = dl_events[0]
    # Verify metadata does not contain URL parameters or signatures
    metadata_str = str(event.metadata or {})
    assert "signature=" not in metadata_str
    assert "X-Amz-Signature" not in metadata_str
    assert "https://" not in metadata_str


def test_production_container_wiring(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from dataclasses import replace
    from tests.test_production_wiring import _make_prod_settings

    test_settings = _make_prod_settings(tmp_path, nookal_key="test_prod_key")

    from app.dashboard.auth import MemoryAuthBackend, User
    mock_auth = MemoryAuthBackend([(User(user_id="u_admin", username="admin", role="admin", display_name="Admin"), "test_pass")])

    container = build_production_container(test_settings, auth_backend=mock_auth)
    assert isinstance(container.nookal, HttpNookalClient)
    assert container.environment == "production"




def test_authorization_enforced_on_nookal_views(client: TestClient) -> None:
    # Unauthenticated requests are rejected
    assert client.get("/cases").status_code == 401
    assert client.get("/finance/invoices").status_code == 401
    assert client.get("/documents").status_code == 401
    assert client.get("/patients/pat_1001").status_code == 401
