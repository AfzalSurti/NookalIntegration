"""
Regression and verification tests for Nookal data display in the Back to Ease dashboard.

Validates end-to-end data flow:
Nookal API / Client -> Services (CaseService, PatientFileService, InvoiceService)
-> Dashboard Views & Dependencies -> Jinja Templates -> Rendered HTML.
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.dashboard.production import build_production_container
from app.nookal_client import (
    Appointment,
    AppointmentType,
    CaseRef,
    HttpNookalClient,
    Invoice,
    InvoiceEntry,
    Location,
    PatientFile,
    PatientRef,
    Practitioner,
    Referrer,
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
    assert client.get("/patients/pat_1001/treatment-notes/79").status_code == 401
    assert client.get("/patients/pat_1001/invoices/inv_i300").status_code == 401


def test_treatment_note_detail_view(client: TestClient, env) -> None:
    from app.nookal_client import TreatmentNote

    # Set up practitioner so name can be resolved
    env.nookal.practitioners["prac_50"] = Practitioner(
        practitioner_id="prac_50",
        first_name="Dr. Sarah",
        last_name="Chen",
    )

    env.nookal.treatment_notes.append(
        TreatmentNote(
            note_id="79",
            patient_id="pat_1001",
            practitioner_id="prac_50",
            notes="Patient reports improved mobility and reduced pain.",
            date="2026-08-15 10:30:00",
            case_id="case_c100",
            appointment_id="appt_500",
        )
    )

    resp = env.authed(client, "GET", "/patients/pat_1001/treatment-notes/79", role="practitioner")
    assert resp.status_code == 200
    html = resp.text
    assert "ID: 79" in html
    assert "Patient reports improved mobility and reduced pain." in html
    assert "Print / Save as PDF" in html
    # Verify practitioner name is resolved
    assert "Dr. Sarah Chen" in html
    assert "prac_50" in html
    # Verify patient link
    assert "/patients/pat_1001" in html
    # Verify appointment link
    assert "/appointments/appt_500" in html
    # Verify case ID displayed
    assert "case_c100" in html


def test_treatment_note_detail_with_structured_fields(client: TestClient, env) -> None:
    from app.nookal_client import TreatmentNote

    env.nookal.treatment_notes.append(
        TreatmentNote(
            note_id="80",
            patient_id="pat_1001",
            date="2026-09-01 14:00:00",
            raw={
                "ID": "80",
                "patientID": "pat_1001",
                "date": "2026-09-01 14:00:00",
                "answers": {
                    "Subjective": "Patient reports knee stiffness in the morning.",
                    "Objective": "ROM limited to 90 degrees flexion.",
                    "Assessment": "Improving post-op recovery.",
                    "Plan": "Continue exercises, review in 2 weeks.",
                },
            },
        )
    )

    resp = env.authed(client, "GET", "/patients/pat_1001/treatment-notes/80", role="practitioner")
    assert resp.status_code == 200
    html = resp.text
    assert "Structured Fields" in html
    assert "Subjective" in html
    assert "Patient reports knee stiffness" in html
    assert "Objective" in html
    assert "ROM limited to 90 degrees" in html
    assert "Assessment" in html
    assert "Plan" in html


def test_treatment_note_detail_not_found(client: TestClient, env) -> None:
    resp = env.authed(client, "GET", "/patients/pat_1001/treatment-notes/nonexistent_999", role="practitioner")
    assert resp.status_code == 404


def test_treatment_note_service_respects_page_length_limit() -> None:
    from unittest.mock import MagicMock
    from app.dashboard.services.treatment_notes import TreatmentNoteService
    from app.nookal_client import TreatmentNote

    mock_nookal = MagicMock()
    mock_nookal.get_treatment_notes.return_value = [
        TreatmentNote(note_id="79", patient_id="100", notes="Lumbar spine assessment.")
    ]
    svc = TreatmentNoteService(nookal=mock_nookal, audit=MagicMock())

    note = svc.get_note("100", "79", actor="u1", role="practitioner", correlation_id="c1")
    assert note is not None
    assert note.note_id == "79"

    # Verify that get_treatment_notes was called with page_length <= 100
    assert mock_nookal.get_treatment_notes.called
    call_kwargs = mock_nookal.get_treatment_notes.call_args[1]
    assert call_kwargs["page_length"] <= 100


def test_patient_file_embedded_view_page_and_buttons(client: TestClient, env) -> None:
    env.nookal.files["file_f200"] = PatientFile(
        file_id="file_f200",
        patient_id="pat_1001",
        name="shoulder_mri_scan.pdf",
        file_type="pdf",
        date_added="2026-08-05",
        size=40960,
    )

    # 1. Verify patient detail page has separate View and Download buttons
    resp = env.authed(client, "GET", "/patients/pat_1001", role="practitioner")
    assert resp.status_code == 200
    assert "/patients/pat_1001/files/file_f200/view" in resp.text
    assert "/patients/pat_1001/files/file_f200/url?download=1" in resp.text

    # 2. Verify documents page has separate View and Download buttons
    resp_docs = env.authed(client, "GET", "/documents?patient_id=pat_1001", role="practitioner")
    assert resp_docs.status_code == 200
    assert "/patients/pat_1001/files/file_f200/view" in resp_docs.text
    assert "/patients/pat_1001/files/file_f200/url?download=1" in resp_docs.text

    # 3. Verify embedded view page loads with iframe and download option
    resp_view = env.authed(client, "GET", "/patients/pat_1001/files/file_f200/view", role="practitioner")
    assert resp_view.status_code == 200
    assert "<iframe" in resp_view.text
    assert "shoulder_mri_scan.pdf" in resp_view.text
    assert "Download File" in resp_view.text


def test_finance_invoices_view_button_and_direct_routes(client: TestClient, env) -> None:
    env.nookal.invoices["inv_i300"] = Invoice(
        invoice_id="inv_i300",
        patient_id="pat_1001",
        date="2026-08-10",
        total=175.50,
        status="Paid",
        void=False,
    )

    # 1. Verify /finance/invoices has View button
    resp = env.authed(client, "GET", "/finance/invoices", role="practitioner")
    assert resp.status_code == 200
    assert "/patients/pat_1001/invoices/inv_i300" in resp.text
    assert ">View<" in resp.text

    # 2. Verify direct access via /finance/invoices/{id} and /invoices/{id}
    resp_direct = env.authed(client, "GET", "/finance/invoices/inv_i300", role="practitioner")
    assert resp_direct.status_code == 200
    assert "Invoice #inv_i300" in resp_direct.text

    # 3. Verify /invoices redirects to /finance/invoices
    csrf = env.login(client, "practitioner")
    resp_redir = client.get("/invoices", headers={"X-CSRF-Token": csrf}, follow_redirects=False)
    assert resp_redir.status_code in (307, 301, 302)
    assert "/finance/invoices" in resp_redir.headers.get("location", "")


def test_nookal_invoice_parsing_with_datecreated_and_totaldebits() -> None:
    raw_payload = {
        "ID": "388",
        "patientID": "100",
        "dateCreated": "2026-08-10 14:00:00",
        "totalDebits": "175.50",
        "totalBalance": "0.00",
        "totalPayments": "175.50",
    }
    inv = HttpNookalClient._parse_invoice(raw_payload)
    assert inv.invoice_id == "388"
    assert inv.patient_id == "100"
    assert inv.date == "2026-08-10"
    assert inv.total == 175.50
    assert inv.status == "Paid"
    assert inv.void is False


def test_invoice_detail_page_displays_all_fields_and_calculated_totals(client: TestClient, env) -> None:
    # Seed location and practitioner for name resolution
    env.nookal.locations["loc_1"] = Location(location_id="loc_1", name="Downtown Physiotherapy Clinic")
    env.nookal.practitioners["prac_1"] = Practitioner(practitioner_id="prac_1", first_name="Sarah", last_name="Connor")

    # Set up an invoice where total is 0.0 to verify automatic calculation from line items
    env.nookal.invoices["inv_i500"] = Invoice(
        invoice_id="inv_i500",
        patient_id="pat_1001",
        date="2026-09-01",
        total=0.0,  # 0 to trigger automatic line item sum
        status="Unpaid",
        void=False,
        location_id="loc_1",
        practitioner_id="prac_1",
        case_id="case_c100",
        reference="INV-2026-0099",
        due_date="2026-09-30",
        notes="Please pay within 30 days via direct deposit or card.",
        entries=[
            InvoiceEntry(
                entry_id="e_501",
                item_id="PHYSIO_INIT",
                invoice_id="inv_i500",
                description="Initial Spinal Assessment",
                price=150.00,
                quantity=1.0,
                tax=15.00,
                total=165.00,
            ),
            InvoiceEntry(
                entry_id="e_502",
                item_id="THERAPY_EX",
                invoice_id="inv_i500",
                description="Therapeutic Exercise Band",
                price=20.00,
                quantity=2.0,
                tax=4.00,
                total=44.00,
            ),
        ],
        raw={"reference": "INV-2026-0099", "payment_terms": "Net 30"},
    )

    # 1. Access invoice via patient route
    resp = env.authed(client, "GET", "/patients/pat_1001/invoices/inv_i500", role="practitioner")
    assert resp.status_code == 200
    html = resp.text

    # Verify Invoice Header & Reference
    assert "Invoice #inv_i500" in html
    assert "INV-2026-0099" in html
    assert "Unpaid" in html

    # Verify Patient, Practitioner, Location, Case
    assert "pat_1001" in html
    assert "Downtown Physiotherapy Clinic" in html
    assert "Sarah Connor" in html
    assert "case_c100" in html
    assert "2026-09-30" in html

    # Verify Financial Calculations ($165 + $44 = $209.00)
    assert "$209.00" in html  # Total Invoice Amount
    assert "$190.00" in html  # Subtotal ($150 + $40)
    assert "$19.00" in html   # Tax / GST ($15 + $4)

    # Verify Line items breakdown
    assert "Initial Spinal Assessment" in html
    assert "PHYSIO_INIT" in html
    assert "Therapeutic Exercise Band" in html
    assert "THERAPY_EX" in html
    assert "e_501" in html
    assert "e_502" in html

    # Verify Notes and Raw data toggle
    assert "Please pay within 30 days" in html
    assert "Show Raw Nookal Data" in html

    # 2. Access invoice via direct /finance/invoices/{id} route
    resp_fin = env.authed(client, "GET", "/finance/invoices/inv_i500", role="practitioner")
    assert resp_fin.status_code == 200
    assert "$209.00" in resp_fin.text
    assert "Sarah Connor" in resp_fin.text
    # Verify unpaid invoice displays $0.00 amount paid and $209.00 balance due
    assert "$0.00" in resp_fin.text
    assert "Unpaid" in resp_fin.text


def test_paid_invoice_detail_shows_zero_balance_and_full_amount_paid(client: TestClient, env) -> None:
    """If an invoice is paid, Balance Due must be $0.00 and Amount Paid must be total amount paid."""
    env.nookal.invoices["inv_paid_99"] = Invoice(
        invoice_id="inv_paid_99",
        patient_id="pat_1001",
        date="2026-09-05",
        total=250.00,
        status="Paid",
        void=False,
        reference="INV-PAID-99",
        balance=0.00,
        paid=250.00,
        entries=[
            InvoiceEntry(
                entry_id="e_p1",
                item_id="PHYSIO_EXT",
                invoice_id="inv_paid_99",
                description="Extended Physiotherapy Session",
                price=250.00,
                quantity=1.0,
                tax=0.00,
                total=250.00,
            ),
        ],
    )

    resp = env.authed(client, "GET", "/patients/pat_1001/invoices/inv_paid_99", role="practitioner")
    assert resp.status_code == 200
    html = resp.text

    # Verify Paid badge
    assert "Paid" in html
    # Amount Paid must equal total ($250.00)
    assert "$250.00" in html
    # Balance Due MUST be $0.00
    assert "$0.00" in html


def test_invoices_page_and_patient_detail_display_paid_unpaid_statuses(client: TestClient, env) -> None:
    """Verify that Invoices page and Patient Detail invoice table display Paid and Unpaid accurately."""
    # Seed a paid invoice and an unpaid invoice for pat_1001
    env.nookal.invoices["inv_p1"] = Invoice(
        invoice_id="inv_p1",
        patient_id="pat_1001",
        date="2026-09-02",
        total=120.00,
        status="Paid",
        balance=0.00,
        paid=120.00,
    )
    env.nookal.invoices["inv_u1"] = Invoice(
        invoice_id="inv_u1",
        patient_id="pat_1001",
        date="2026-09-04",
        total=85.00,
        status="Unpaid",
        balance=85.00,
        paid=0.00,
    )

    # 1. Invoices page shows both statuses accurately
    resp_inv = env.authed(client, "GET", "/finance/invoices", role="practitioner")
    assert resp_inv.status_code == 200
    assert "inv_p1" in resp_inv.text
    assert "inv_u1" in resp_inv.text
    assert "badge-success" in resp_inv.text  # Paid badge
    assert "badge-warning" in resp_inv.text  # Unpaid badge

    # 2. Filter by status=Paid
    resp_filtered = env.authed(client, "GET", "/finance/invoices?status=Paid", role="practitioner")
    assert resp_filtered.status_code == 200
    assert "inv_p1" in resp_filtered.text
    assert "inv_u1" not in resp_filtered.text

    # 3. Patient Detail page shows both invoices with correct badges
    resp_pat = env.authed(client, "GET", "/patients/pat_1001", role="practitioner")
    assert resp_pat.status_code == 200
    assert "inv_p1" in resp_pat.text
    assert "inv_u1" in resp_pat.text
    assert "Paid" in resp_pat.text
    assert "Unpaid" in resp_pat.text


def test_nookal_invoice_parsing_unpaid_and_paid_heuristics() -> None:
    """Verify HttpNookalClient._parse_invoice correctly identifies Paid/Unpaid from diverse Nookal payloads."""
    # Explicit 'Paid'
    inv1 = HttpNookalClient._parse_invoice({
        "ID": "1",
        "patientID": "10",
        "total": "150.00",
        "status": "paid",
    })
    assert inv1.status == "Paid"
    assert inv1.balance == 0.0
    assert inv1.paid == 150.0

    # Explicit 'Unpaid'
    inv2 = HttpNookalClient._parse_invoice({
        "ID": "2",
        "patientID": "10",
        "total": "150.00",
        "status": "unpaid",
    })
    assert inv2.status == "Unpaid"
    assert inv2.balance == 150.0
    assert inv2.paid == 0.0

    # Heuristic: totalBalance is 0.00 with debits
    inv3 = HttpNookalClient._parse_invoice({
        "ID": "3",
        "patientID": "10",
        "totalDebits": "200.00",
        "totalBalance": "0.00",
        "totalPayments": "200.00",
    })
    assert inv3.status == "Paid"
    assert inv3.balance == 0.0
    assert inv3.paid == 200.0

    # Heuristic: totalBalance > 0 with 0 payments
    inv4 = HttpNookalClient._parse_invoice({
        "ID": "4",
        "patientID": "10",
        "totalDebits": "300.00",
        "totalBalance": "300.00",
        "totalPayments": "0.00",
    })
    assert inv4.status == "Unpaid"
    assert inv4.balance == 300.0
    assert inv4.paid == 0.0

    # Boolean isPaid = True
    inv5 = HttpNookalClient._parse_invoice({
        "ID": "5",
        "patientID": "10",
        "amount": "95.00",
        "isPaid": True,
    })
    assert inv5.status == "Paid"
    assert inv5.balance == 0.0
    assert inv5.paid == 95.0


def test_appointment_detail_page_displays_all_nookal_data(client: TestClient, env) -> None:
    # Seed metadata helpers
    env.nookal.locations["loc_2"] = Location(location_id="loc_2", name="West End Health Hub")
    env.nookal.practitioners["prac_2"] = Practitioner(practitioner_id="prac_2", first_name="David", last_name="Miller")
    env.nookal.appointment_types["type_std"] = AppointmentType(type_id="type_std", name="Standard Follow-up 30m")

    # Set up comprehensive appointment
    env.nookal.appointments["appt_777"] = Appointment(
        appointment_id="appt_777",
        patient_id="pat_1001",
        starts_at=datetime(2026, 9, 20, 14, 0),
        ends_at=datetime(2026, 9, 20, 14, 45),
        status="booked",
        location_id="loc_2",
        practitioner_id="prac_2",
        type_id="type_std",
        appointment_type="Standard Follow-up 30m",
        notes="Patient recovering well; reassess shoulder range of motion.",
        arrived=False,
        dna=False,
        cancelled=False,
        email_reminder_sent=True,
        invoice_generated=False,
        raw={"nookal_source": "online_booking", "device": "mobile"},
    )

    resp = env.authed(client, "GET", "/appointments/appt_777", role="practitioner")
    assert resp.status_code == 200
    html = resp.text

    # Verify appointment ID, status, and patient link
    assert "Appointment #appt_777" in html
    assert "Booked" in html
    assert "pat_1001" in html
    assert "/patients/pat_1001" in html

    # Verify resolved Location, Practitioner, and Type
    assert "West End Health Hub" in html
    assert "David Miller" in html
    assert "Standard Follow-up 30m" in html

    # Verify Duration & Schedule
    assert "45 minutes" in html

    # Verify Status Indicators & Automation Flags
    assert "Sent" in html  # Email reminder sent
    assert "Active" in html

    # Verify Clinical/Booking notes
    assert "Patient recovering well; reassess shoulder range of motion." in html

    # Verify Action buttons
    assert "Reschedule" in html
    assert "Cancel Appointment" in html

    # Verify Raw data viewer
    assert "Show Raw Nookal Data" in html

    # Verify lookup works by numeric ID (e.g. 777) and prefixed ID (appt_777)
    resp_bare = env.authed(client, "GET", "/appointments/777", role="practitioner")
    assert resp_bare.status_code == 200
    assert "Appointment #appt_777" in resp_bare.text or "Appointment #777" in resp_bare.text

    # Verify seed appointment lookup (appt_2001 / 2001)
    resp_seed_prefixed = env.authed(client, "GET", "/appointments/appt_2001", role="practitioner")
    assert resp_seed_prefixed.status_code == 200
    resp_seed_bare = env.authed(client, "GET", "/appointments/2001", role="practitioner")
    assert resp_seed_bare.status_code == 200

    # Verify JSON API route
    resp_api = env.authed(client, "GET", "/api/appointments/appt_2001", role="practitioner")
    assert resp_api.status_code == 200
    assert resp_api.json()["appointment_id"] in ("appt_2001", "2001")

    # 404 for nonexistent appointment
    resp_404 = env.authed(client, "GET", "/appointments/nonexistent_999", role="practitioner")
    assert resp_404.status_code == 404
    resp_api_404 = env.authed(client, "GET", "/api/appointments/nonexistent_999", role="practitioner")
    assert resp_api_404.status_code == 404


def test_patient_detail_page_displays_all_nookal_demographics_and_invoices(client: TestClient, env) -> None:
    # Seed referrer for name lookup
    env.nookal.referrers["ref_dr_smith"] = Referrer(
        referrer_id="ref_dr_smith",
        name="Dr. Gregory Smith",
        provider_number="PR-12345",
    )

    # Seed patient with complete Nookal demographics
    env.nookal.patients["pat_2020"] = PatientRef(
        patient_id="pat_2020",
        first_name="Alexander",
        middle_name="Graham",
        last_name="Bell",
        nickname="Alex",
        phone="0498765432",
        email="alex.bell@example.com",
        display_name="Alexander Bell",
        date_of_birth=date(1985, 3, 10),
        gender="Male",
        suburb="Paddington",
        address={"street": "100 Queen Street", "city": "Paddington", "state": "QLD", "postcode": "4064"},
        postal_address="PO Box 777, Brisbane QLD",
        online_code="ALEX2026",
        deceased=False,
        referrer_id="ref_dr_smith",
        date_created="2026-01-15 08:30:00",
        date_modified="2026-09-01 11:20:00",
        last_appointment_date=date(2026, 9, 5),
        raw={"custom_flag": "VIP_Patient"},
    )

    # Seed an appointment for this patient to test linking to detail page
    env.nookal.appointments["appt_888"] = Appointment(
        appointment_id="appt_888",
        patient_id="pat_2020",
        starts_at=datetime(2026, 9, 25, 10, 0),
        ends_at=datetime(2026, 9, 25, 10, 30),
        status="booked",
    )

    resp = env.authed(client, "GET", "/patients/pat_2020", role="practitioner")
    assert resp.status_code == 200
    html = resp.text

    # Verify Name breakdown & Nickname
    assert "Alexander Bell" in html
    assert 'Nickname: "Alex"' in html
    assert "Alexander Graham Bell" in html

    # Verify DOB, Gender, Phone, Email
    assert "1985-03-10" in html
    assert "Male" in html
    assert "0498765432" in html
    assert "alex.bell@example.com" in html

    # Verify Addresses & Codes
    assert "Paddington" in html
    assert "100 Queen Street" in html
    assert "PO Box 777" in html
    assert "ALEX2026" in html
    assert "Active Patient" in html

    # Verify Referrer Name & ID
    assert "Dr. Gregory Smith" in html
    assert "ref_dr_smith" in html

    # Verify Timestamps
    assert "2026-01-15 08:30:00" in html
    assert "2026-09-01 11:20:00" in html

    # Verify Appointment links directly to appointment detail page
    assert "/appointments/appt_888" in html
    assert "appt_888" in html

    # Verify Raw Nookal Patient Data inspector is present
    assert "Show Raw Nookal Patient Data" in html


def test_invoice_information_card_displays_all_nookal_data(client: TestClient, env) -> None:
    # Seed location & practitioner
    env.nookal.locations["loc_bte"] = Location(
        location_id="loc_bte",
        name="Back to Ease Bond Street",
    )
    env.nookal.practitioners["prac_bte"] = Practitioner(
        practitioner_id="prac_bte",
        first_name="Marcus",
        last_name="Welby",
    )
    env.nookal.patients["pat_bte"] = PatientRef(
        patient_id="pat_bte",
        first_name="Eleanor",
        last_name="Rigby",
        display_name="Eleanor Rigby",
    )

    # Seed comprehensive invoice
    env.nookal.invoices["inv_info_101"] = Invoice(
        invoice_id="inv_info_101",
        patient_id="pat_bte",
        reference="REF-BTE-2026",
        date="2026-09-02",
        due_date="2026-09-16",
        location_id="loc_bte",
        practitioner_id="prac_bte",
        case_id="case_bte_77",
        total=220.00,
        paid=220.00,
        balance=0.00,
        status="Paid",
        notes="Post-treatment mobilization notes and advice",
        entries=[
            InvoiceEntry(
                entry_id="ent_bte_1",
                invoice_id="inv_info_101",
                item_id="CON-60",
                description="Comprehensive 60min Consultation",
                price=220.00,
                quantity=1.0,
                tax=0.0,
                total=220.00,
            )
        ],
        raw={
            "patient_name": "Eleanor Rigby",
            "location_name": "Back to Ease Bond Street",
            "practitioner_name": "Marcus Welby",
        },
    )

    # 1. Test Finance direct route: /finance/invoices/inv_info_101
    resp1 = env.authed(client, "GET", "/finance/invoices/inv_info_101", role="admin")
    assert resp1.status_code == 200
    html1 = resp1.text

    assert "Invoice Information" in html1
    assert "inv_info_101" in html1
    assert "REF-BTE-2026" in html1
    assert "Eleanor Rigby" in html1
    assert "pat_bte" in html1
    assert "2026-09-02" in html1
    assert "2026-09-16" in html1
    assert "Back to Ease Bond Street" in html1
    assert "loc_bte" in html1
    assert "Marcus Welby" in html1
    assert "prac_bte" in html1
    assert "case_bte_77" in html1
    assert "Paid" in html1
    assert "$220.00" in html1
    assert "$0.00" in html1
    assert "Comprehensive 60min Consultation" in html1
    assert "Post-treatment mobilization notes and advice" in html1

    # 2. Test Patient invoice route: /patients/pat_bte/invoices/inv_info_101
    resp2 = env.authed(client, "GET", "/patients/pat_bte/invoices/inv_info_101", role="practitioner")
    assert resp2.status_code == 200
    html2 = resp2.text

    assert "Invoice Information" in html2
    assert "inv_info_101" in html2
    assert "REF-BTE-2026" in html2
    assert "Eleanor Rigby" in html2
    assert "pat_bte" in html2
    assert "2026-09-02" in html2
    assert "2026-09-16" in html2
    assert "Back to Ease Bond Street" in html2
    assert "Marcus Welby" in html2
    assert "case_bte_77" in html2
    assert "$220.00" in html2
    assert "$0.00" in html2


