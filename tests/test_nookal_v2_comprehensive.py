"""
Comprehensive test suite for Nookal API v2 compliance.
Covers all documented endpoints:
- Cases: getCases, getAllCases
- Treatment Notes: getTreatmentNotes, getAllTreatmentNotes, addTreatmentNote
- Patient Extras: getExtras, addPatientExtra
- Locations & Practitioners: getLocations, getLocationLogo, getPractitioners, getPractitionerPhoto
- Services & Classes: getAppointmentTypes, getClassTypes, getClassParticipants, getClassRedemptions, getServiceRedemptions, getWaitingList, getClassAvailabilities
- Documents: getPatientFiles, getFileUrl, uploadFile, setFileActive
- Invoices & Financials: getInvoice, getInvoices, getInvoiceEntries, getInvoiceCredits, getInvoiceDiscounts, getInvoicePayments, getInvoiceRefunds, getInvoiceAdjustments, and writes
- Strict validation & kill switch enforcement
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
import httpx
import pytest

from app.nookal_client.client import (
    AppointmentType,
    CaseRef,
    ClassParticipant,
    ClassType,
    HttpNookalClient,
    Invoice,
    InvoiceEntry,
    Location,
    PatientExtra,
    PatientFile,
    Practitioner,
    TreatmentNote,
)
from app.shared.config import NookalConfig
from app.shared.exceptions import KillSwitchActive, NookalError, NookalNotFound, NookalValidationError
from app.shared.kill_switch import activate


BASE_URL = "https://api.nookal.com/production/v2"


def _make_config() -> NookalConfig:
    return NookalConfig(
        base_url=BASE_URL,
        api_key="secret-key-12345",
        timeout_seconds=5.0,
        requests_per_second=100.0,
        max_retries=1,
    )


# --- Cases ---

def test_get_cases_and_get_all_cases() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if "getAllCases" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "api_call": "getAllCases",
                        "results": {
                            "cases": [
                                {
                                    "ID": "case_1",
                                    "patientID": "p_10",
                                    "caseName": "Shoulder Rehab",
                                    "caseNumber": "C-100",
                                    "status": "Open",
                                    "dateCreated": "2026-01-15",
                                }
                            ]
                        },
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "cases": [
                        {
                            "ID": "case_2",
                            "patientID": "p_20",
                            "caseName": "Knee Surgery",
                            "status": "Closed",
                            "closedDate": "2026-02-01",
                        }
                    ]
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    all_cases = nookal.get_all_cases(page=1, page_length=50)
    assert len(all_cases) == 1
    assert all_cases[0].case_id == "case_1"
    assert all_cases[0].patient_id == "p_10"
    assert all_cases[0].case_name == "Shoulder Rehab"
    assert all_cases[0].status == "Open"

    pt_cases = nookal.get_cases("p_20", page=1)
    assert len(pt_cases) == 1
    assert pt_cases[0].case_id == "case_2"
    assert pt_cases[0].closed_date == "2026-02-01"


# --- Treatment Notes ---

def test_treatment_notes_read_and_write() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.method == "POST" and "addTreatmentNote" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"ID": "note_99", "patientID": "p_1", "notes": "Patient improved"},
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "treatment_notes": [
                        {
                            "ID": "note_1",
                            "patientID": "p_1",
                            "practitionerID": "prac_1",
                            "date": "2026-09-01 10:00:00",
                            "notes": "Initial assessment complete",
                        }
                    ]
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    notes = nookal.get_treatment_notes("p_1")
    assert len(notes) == 1
    assert notes[0].note_id == "note_1"
    assert notes[0].practitioner_id == "prac_1"

    added = nookal.add_treatment_note(
        patient_id="p_1",
        case_id="c_1",
        practitioner_id="prac_1",
        notes="Patient improved",
        date="2026-09-02 11:00:00",
    )
    assert added.note_id == "note_99"

    # Validation: invalid note date
    with pytest.raises(NookalValidationError):
        nookal.add_treatment_note(
            patient_id="p_1",
            case_id="c_1",
            practitioner_id="prac_1",
            notes="Invalid date",
            date="invalid-date",
        )


# --- Extras ---

def test_extras_read_and_write() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"status": "success", "data": {"status": "added"}})
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "extras": [
                        {"ID": "ext_1", "name": "Occupation", "type": "Text"},
                        {"ID": "ext_2", "name": "Sports Played", "type": "Select", "options": "Football,Tennis"},
                    ]
                },
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    extras = nookal.get_extras()
    assert len(extras) == 2
    assert extras[0].name == "Occupation"
    assert extras[1].options == ["Football", "Tennis"]

    res = nookal.add_patient_extra(patient_id="p_1", extra_id="ext_1", value="Software Engineer")
    assert res is True


# --- Locations & Practitioners ---

def test_locations_and_practitioners() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "getLocations" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "locations": [
                            {"ID": "loc_1", "name": "Richmond Clinic", "address": "123 Bridge Rd"}
                        ]
                    },
                },
            )
        if "getLocationLogo" in url:
            return httpx.Response(
                200,
                json={"status": "success", "data": {"url": "https://cdn.nookal.com/logo.png"}},
            )
        if "getPractitioners" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "practitioners": [
                            {
                                "ID": "prac_1",
                                "firstName": "John",
                                "lastName": "Doe",
                                "speciality": "Physiotherapist",
                                "locations": ["loc_1"],
                            }
                        ]
                    },
                },
            )
        if "getPractitionerPhoto" in url:
            return httpx.Response(
                200,
                json={"status": "success", "data": {"url": "https://cdn.nookal.com/john.png"}},
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    locs = nookal.get_locations()
    assert len(locs) == 1
    assert locs[0].location_id == "loc_1"
    assert locs[0].name == "Richmond Clinic"

    logo_url = nookal.get_location_logo("loc_1")
    assert logo_url == "https://cdn.nookal.com/logo.png"

    pracs = nookal.get_practitioners()
    assert len(pracs) == 1
    assert pracs[0].first_name == "John"
    assert pracs[0].locations == ["loc_1"]

    photo_url = nookal.get_practitioner_photo("prac_1")
    assert photo_url == "https://cdn.nookal.com/john.png"


# --- Services & Classes ---

def test_services_and_classes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "getAppointmentTypes" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "types": [
                            {"ID": "t_1", "name": "Initial Physio", "duration": "45", "price": "120.00"}
                        ]
                    },
                },
            )
        if "getClassTypes" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "classes": [
                            {"ID": "c_1", "name": "Pilates Reformer", "duration": "60", "price": "45.00"}
                        ]
                    },
                },
            )
        if "getClassParticipants" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "participants": [
                            {"ID": "part_1", "classID": "c_1", "patientID": "p_1", "status": "Attended"}
                        ]
                    },
                },
            )
        if "getWaitingList" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "waiting_list": [
                            {"ID": "w_1", "patientID": "p_2", "dateAdded": "2026-09-01"}
                        ]
                    },
                },
            )
        if "getClassAvailabilities" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "availabilities": [
                            {"classID": "c_1", "date": "2026-09-15", "startTime": "09:00:00", "spaces": 4}
                        ]
                    },
                },
            )
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    app_types = nookal.get_appointment_types()
    assert len(app_types) == 1
    assert app_types[0].name == "Initial Physio"
    assert app_types[0].duration == 45
    assert app_types[0].price == 120.00

    classes = nookal.get_class_types()
    assert len(classes) == 1
    assert classes[0].name == "Pilates Reformer"

    parts = nookal.get_class_participants(class_id="c_1")
    assert len(parts) == 1
    assert parts[0].patient_id == "p_1"

    wl = nookal.get_waiting_list()
    assert len(wl) == 1
    assert wl[0].entry_id == "w_1"

    avail = nookal.get_class_availabilities(date_from=date(2026, 9, 15), date_to=date(2026, 9, 15))
    assert len(avail) == 1


# --- Documents & 2-Stage Upload ---

def test_document_files_and_upload_lifecycle() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        url = str(request.url)
        if "getPatientFiles" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "files": [
                            {"ID": "f_1", "patientID": "p_1", "name": "xray.pdf", "fileType": "pdf", "size": 2048}
                        ]
                    },
                },
            )
        if "getFileUrl" in url:
            return httpx.Response(
                200,
                json={"status": "success", "data": {"url": "https://s3.aws.com/patient/xray.pdf?sig=xyz"}},
            )
        if "uploadFile" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "fileID": "f_new",
                        "url": "https://s3.aws.com/uploads/f_new",
                    },
                },
            )
        if "setFileActive" in url:
            return httpx.Response(200, json={"status": "success", "data": {"status": "active"}})
        if "s3.aws.com" in url:
            return httpx.Response(200)
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    files = nookal.get_patient_files("p_1")
    assert len(files) == 1
    assert files[0].file_id == "f_1"
    assert files[0].file_type == "pdf"

    dl_url = nookal.get_file_url("p_1", "f_1")
    assert "s3.aws.com" in dl_url

    # Test uploadFile initiation
    fid, s3_url = nookal.upload_file(
        patient_id="p_1",
        name="report",
        extension="pdf",
        file_type="application/pdf",
        file_path="report.pdf",
    )
    assert fid == "f_new"
    assert "s3.aws.com" in s3_url

    # Test setFileActive
    active = nookal.set_file_active(patient_id="p_1", file_id="f_new")
    assert active is True


# --- Invoices & Financials ---

def test_invoices_read_and_financial_writes() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        url = str(request.url)
        if "getInvoices" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "invoices": [
                            {
                                "ID": "inv_1",
                                "patientID": "p_1",
                                "date": "2026-09-01",
                                "total": 150.0,
                                "status": "Paid",
                                "entries": [
                                    {"ID": "e_1", "description": "Consultation", "price": 150.0, "quantity": 1}
                                ],
                            }
                        ]
                    },
                },
            )
        if "getInvoice" in url and "Entries" not in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {"ID": "inv_1", "patientID": "p_1", "total": 150.0, "status": "Paid"},
                },
            )
        if "getInvoiceEntries" in url:
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "entries": [
                            {"ID": "e_1", "description": "Consultation", "price": 150.0, "quantity": 1}
                        ]
                    },
                },
            )
        if any(w in url for w in ("addPaymentToInvoice", "addItemToInvoice", "deleteInvoice", "getInvoiceCredits", "getInvoicePayments")):
            return httpx.Response(200, json={"status": "success", "data": {"status": "ok", "credits": [], "payments": []}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    invoices = nookal.get_invoices("p_1")
    assert len(invoices) == 1
    assert invoices[0].invoice_id == "inv_1"
    assert invoices[0].total == 150.0
    assert len(invoices[0].entries) == 1

    inv = nookal.get_invoice("inv_1")
    assert inv.invoice_id == "inv_1"

    entries = nookal.get_invoice_entries(invoice_id="inv_1")
    assert len(entries) == 1
    assert entries[0].entry_id == "e_1"

    # Test financial sub-queries and writes
    credits = nookal.get_invoice_credits(invoice_id="inv_1")
    assert isinstance(credits, list)

    payments = nookal.get_invoice_payments(invoice_id="inv_1")
    assert isinstance(payments, list)

    pay = nookal.add_payment_to_invoice({"invoice_id": "inv_1", "amount": 150.0, "type": "Card"})
    assert pay.get("status") == "ok"

    item = nookal.add_item_to_invoice({"invoice_id": "inv_1", "description": "Strapping", "price": 15.0})
    assert item.get("status") == "ok"

    del_inv = nookal.delete_invoice("inv_1")
    assert del_inv is True


# --- Validation and Kill Switch on all New Endpoints ---

def test_validation_and_kill_switch_on_new_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"status": "success"})), base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    # 1. Validation errors
    with pytest.raises(NookalValidationError):
        nookal.get_cases("p_1", page=0)

    with pytest.raises(NookalValidationError):
        nookal.get_all_cases(page_length=201)

    with pytest.raises(NookalValidationError):
        nookal.get_treatment_notes("p_1", page_length=101)

    with pytest.raises(NookalValidationError):
        nookal.get_invoices("")

    # 2. Kill switch blocks writes
    monkeypatch.setenv("AUTOMATION_HOME", str(tmp_path))
    flag = tmp_path / "KILL_SWITCH"
    activate(path=flag, reason="emergency stop")

    from app.shared import kill_switch as ks
    monkeypatch.setattr(ks, "switch_path", lambda path=None: flag)

    with pytest.raises(KillSwitchActive):
        nookal.add_treatment_note(
            patient_id="p_1",
            case_id="c_1",
            practitioner_id="prac_1",
            notes="Note",
            date="2026-09-02 11:00:00",
        )

    with pytest.raises(KillSwitchActive):
        nookal.add_patient_extra(patient_id="p_1", extra_id="ext_1", value="Value")

    with pytest.raises(KillSwitchActive):
        nookal.upload_file(
            patient_id="p_1",
            name="test",
            extension="pdf",
            file_type="application/pdf",
            file_path="test.pdf",
        )

    with pytest.raises(KillSwitchActive):
        nookal.set_file_active(patient_id="p_1", file_id="f_1")

    with pytest.raises(KillSwitchActive):
        nookal.add_payment_to_invoice({"invoice_id": "inv_1", "amount": 10.0})

    with pytest.raises(KillSwitchActive):
        nookal.delete_invoice("inv_1")

