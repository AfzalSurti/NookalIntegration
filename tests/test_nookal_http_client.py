"""
Offline unit tests for HttpNookalClient using httpx.MockTransport.

Tests the official Nookal API v2 implementation:
- Authentication via ?api_key= query parameter (no Bearer header)
- Secret redaction in exceptions and logs
- Response envelope unwrapping and error mapping
- Documented endpoints:
  - GET /searchPatients
  - GET /getPatients
  - GET /getAppointments
  - POST /addAppointmentBooking
  - POST /updateAppointmentBooking
  - POST /cancelAppointment
  - POST /rebookAppointment
  - GET /getAppointmentAvailabilities
- Kill switch blocking writes before transport
- NotImplementedError for unsupported endpoints
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import json
import httpx
import pytest

from app.nookal_client import HttpNookalClient, Appointment, PatientRef
from app.shared.config import NookalConfig
from app.shared.exceptions import (
    KillSwitchActive,
    NookalAuthError,
    NookalError,
    NookalNotFound,
    NookalServerError,
    NookalValidationError,
)
from app.shared.kill_switch import activate, deactivate


TEST_API_KEY = "test-secret-nookal-key-999"
BASE_URL = "https://api.nookal.com/production/v2/"


def _make_config() -> NookalConfig:
    return NookalConfig(
        base_url=BASE_URL,
        api_key=TEST_API_KEY,
        timeout_seconds=5.0,
        requests_per_second=100.0,
        max_retries=1,
    )


def test_auth_query_parameter_injected_and_no_bearer_header() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"status": "success", "data": []},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    nookal.list_appointments()

    assert len(captured_requests) == 1
    req = captured_requests[0]
    # Check query param auth
    assert f"api_key={TEST_API_KEY}" in str(req.url)
    # Check header: Accept is present, Authorization is NOT present
    assert req.headers.get("Accept") == "application/json"
    assert "Authorization" not in req.headers


def test_api_key_redacted_in_errors_and_logs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text=f"Internal crash at {request.url}")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    with pytest.raises(NookalServerError) as exc_info:
        nookal.list_appointments()

    msg = str(exc_info.value)
    assert TEST_API_KEY not in msg
    assert "[REDACTED]" in msg


def test_get_patient_uses_search_patients() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert "searchPatients" in str(request.url.path)
        assert request.url.params["patient_id"] == "p_42"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {
                        "ID": "p_42",
                        "FirstName": "Arthur",
                        "LastName": "Dent",
                        "DOB": "1978-03-11",
                        "Mobile": "+61412345678",
                        "Email": "arthur@earth.org",
                        "Suburb": "Islington",
                        "referrerID": "ref_10",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    patient = nookal.get_patient("p_42")
    assert patient.patient_id == "p_42"
    assert patient.display_name == "Arthur Dent"
    assert patient.phone == "+61412345678"
    assert patient.email == "arthur@earth.org"
    assert patient.date_of_birth == date(1978, 3, 11)
    assert patient.suburb == "Islington"
    assert patient.referrer_id == "ref_10"


def test_get_patient_not_found_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "success", "data": []})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    with pytest.raises(NookalNotFound):
        nookal.get_patient("missing_p")


def test_get_patient_ambiguous_matches_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {"ID": "p_1", "FirstName": "John", "LastName": "Doe"},
                    {"ID": "p_2", "FirstName": "John", "LastName": "Smith"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    with pytest.raises(NookalValidationError) as exc_info:
        nookal.get_patient("p_ambiguous")
    assert "ambiguous" in str(exc_info.value).lower()


def test_find_patient_by_phone_uses_search_patients() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        assert "searchPatients" in str(request.url.path)
        assert request.url.params["fuzzy_search"] == "+61411222333"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {"ID": "p_1", "FirstName": "Alice", "Mobile": "+61411222333"},
                    {"ID": "p_2", "FirstName": "Bob", "Mobile": "+61499999999"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    results = nookal.find_patient_by_phone("+61411222333")
    assert len(results) == 1
    assert results[0].patient_id == "p_1"
    assert results[0].display_name == "Alice"


def test_list_appointments_maps_dates() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {
                        "ID": "appt_1",
                        "patientID": "p_1",
                        "date": "2026-09-10",
                        "startTime": "10:00:00",
                        "endTime": "10:30:00",
                        "cancelled": "0",
                        "DNA": "0",
                        "arrived": "0",
                        "locationID": "loc_1",
                        "practitionerID": "prac_1",
                    },
                    {
                        "ID": "appt_2",
                        "patientID": "p_2",
                        "date": "2026-09-10",
                        "startTime": "11:00:00",
                        "cancelled": "1",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    appts = nookal.list_appointments(on_date=date(2026, 9, 10), patient_id="p_1")
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert "getAppointments" in str(req.url.path)
    assert req.url.params["date_from"] == "2026-09-10"
    assert req.url.params["date_to"] == "2026-09-10"
    assert req.url.params["patient_id"] == "p_1"
    assert req.url.params["page_length"] == "200"

    assert len(appts) == 2
    assert appts[0].appointment_id == "appt_1"
    assert appts[0].status == "booked"
    assert appts[0].starts_at == datetime(2026, 9, 10, 10, 0)
    assert appts[0].ends_at == datetime(2026, 9, 10, 10, 30)
    assert appts[1].appointment_id == "appt_2"
    assert appts[1].status == "cancelled"


def test_get_appointment_finds_by_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {
                        "ID": "appt_100",
                        "patientID": "p_5",
                        "date": "2026-09-15",
                        "startTime": "14:00:00",
                        "status": "booked",
                    },
                    {
                        "ID": "appt_200",
                        "patientID": "p_6",
                        "date": "2026-09-15",
                        "startTime": "15:00:00",
                        "arrived": "1",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    appt = nookal.get_appointment("appt_200")
    assert appt.appointment_id == "appt_200"
    assert appt.patient_id == "p_6"
    assert appt.status == "arrived"

    with pytest.raises(NookalNotFound):
        nookal.get_appointment("appt_999")


def test_create_appointment_validation_and_endpoint() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"ID": "appt_new_1"},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    # Missing required fields raises NookalValidationError before HTTP request
    with pytest.raises(NookalValidationError) as exc_info:
        nookal.create_appointment({"patient_id": "p_1"})
    assert "missing required fields" in str(exc_info.value).lower()
    assert len(captured_requests) == 0

    # Full payload succeeds
    payload = {
        "location_id": "loc_1",
        "starts_at": datetime(2026, 9, 20, 9, 30),
        "patient_id": "p_10",
        "practitioner_id": "prac_5",
        "appointment_type_id": "type_2",
        "notes": "Initial assessment",
    }
    created = nookal.create_appointment(payload)
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.method == "POST"
    assert "addAppointmentBooking" in str(req.url.path)
    body = json.loads(req.content)
    assert body["location_id"] == "loc_1"
    assert body["appointment_date"] == "2026-09-20"
    assert body["start_time"] == "09:30:00"
    assert body["patient_id"] == "p_10"
    assert body["practitioner_id"] == "prac_5"
    assert body["appointment_type_id"] == "type_2"
    assert body["notes"] == "Initial assessment"

    assert created.appointment_id == "appt_new_1"
    assert created.patient_id == "p_10"


def test_update_appointment_uses_update_appointment_booking() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {"ID": "appt_45", "patientID": "p_2", "status": "dna"},
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    updated = nookal.update_appointment(
        "appt_45",
        starts_at=datetime(2026, 9, 25, 14, 0),
        status="dna",
    )
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.method == "POST"
    assert "updateAppointmentBooking" in str(req.url.path)
    body = json.loads(req.content)
    assert body["appointment_id"] == "appt_45"
    assert body["appointment_date"] == "2026-09-25"
    assert body["start_time"] == "14:00:00"
    assert body["status"] == "DNA"


def test_cancel_appointment_uses_cancel_appointment() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"status": "success", "data": "Appointment cancelled successfully"},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    cancelled = nookal.cancel_appointment("appt_88", patient_id="p_9")
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.method == "POST"
    assert "cancelAppointment" in str(req.url.path)
    body = json.loads(req.content)
    assert body["appointment_id"] == "appt_88"
    assert body["patient_id"] == "p_9"
    assert cancelled.appointment_id == "appt_88"
    assert cancelled.status == "cancelled"


def test_rebook_appointment_uses_rebook_appointment() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"status": "success", "data": {"ID": "appt_rebooked_1"}},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    rebooked = nookal.rebook_appointment(
        "appt_old_1",
        patient_id="p_5",
        location_id="loc_2",
        start_time="11:30:00",
        practitioner_id="prac_3",
        appointment_date=date(2026, 9, 28),
        cancel_first=True,
    )
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert req.method == "POST"
    assert "rebookAppointment" in str(req.url.path)
    body = json.loads(req.content)
    assert body["appointment_id"] == "appt_old_1"
    assert body["patient_id"] == "p_5"
    assert body["location_id"] == "loc_2"
    assert body["start_time"] == "11:30:00"
    assert body["practitioner_id"] == "prac_3"
    assert body["appointment_date"] == "2026-09-28"
    assert body["cancel_first"] is True
    assert rebooked.appointment_id == "appt_rebooked_1"


def test_get_appointment_availabilities() -> None:
    captured_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {"start": "2026-09-30T09:00:00", "end": "2026-09-30T09:30:00"},
                    {"start": "2026-09-30T10:00:00", "end": "2026-09-30T10:30:00"},
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    avail = nookal.get_appointment_availabilities(
        location_id="loc_1",
        date_from=date(2026, 9, 30),
        date_to=date(2026, 9, 30),
    )
    assert len(captured_requests) == 1
    req = captured_requests[0]
    assert "getAppointmentAvailabilities" in str(req.url.path)
    assert req.url.params["location_id"] == "loc_1"
    assert req.url.params["date_from"] == "2026-09-30"
    assert len(avail) == 2


def test_kill_switch_blocks_http_client_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOMATION_HOME", str(tmp_path))
    flag = tmp_path / "KILL_SWITCH"
    activate(path=flag, reason="emergency stop")

    from app.shared import kill_switch as ks
    monkeypatch.setattr(ks, "switch_path", lambda path=None: flag)

    req_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal req_count
        req_count += 1
        return httpx.Response(200, json={"status": "success"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    try:
        with pytest.raises(KillSwitchActive):
            nookal.create_appointment(
                {
                    "location_id": "loc_1",
                    "appointment_date": "2026-09-20",
                    "start_time": "10:00:00",
                    "patient_id": "p_1",
                    "practitioner_id": "prac_1",
                    "appointment_type_id": "t_1",
                }
            )

        with pytest.raises(KillSwitchActive):
            nookal.update_appointment("appt_1", status="cancelled")

        with pytest.raises(KillSwitchActive):
            nookal.cancel_appointment("appt_1", patient_id="p_1")

        with pytest.raises(KillSwitchActive):
            nookal.rebook_appointment(
                "appt_1",
                patient_id="p_1",
                location_id="loc_1",
                start_time="10:00:00",
                practitioner_id="prac_1",
                appointment_date=date(2026, 9, 20),
            )

        # Zero network calls should have been made
        assert req_count == 0
    finally:
        deactivate(path=flag)


def test_nookal_envelope_error_handling() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "patients" in str(request.url):
            return httpx.Response(200, json={"status": "failure", "details": "Record not found"})
        if "auth" in str(request.url):
            return httpx.Response(200, json={"status": "failure", "details": "Invalid API key"})
        return httpx.Response(200, json={"status": "error", "details": "Booking conflict detected"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    with pytest.raises(NookalNotFound):
        nookal._request("GET", "/test/patients", action="test", target_type="patient_record", target_id="1")

    with pytest.raises(NookalAuthError):
        nookal._request("GET", "/test/auth", action="test", target_type="patient_record", target_id="1")

    with pytest.raises(NookalError) as exc_info:
        nookal._request("GET", "/test/conflict", action="test", target_type="patient_record", target_id="1")
    assert "Booking conflict detected" in str(exc_info.value)


def test_undocumented_endpoints_not_on_http_client() -> None:
    nookal = HttpNookalClient(config=_make_config(), client=httpx.Client())
    assert not hasattr(nookal, "list_referrers")
    assert not hasattr(nookal, "upsert_referrer")
    assert not hasattr(nookal, "save_document")



def test_search_patients_with_official_nookal_envelope() -> None:
    """
    Verifies that search_patients correctly parses the official Nookal API response shape:
    {"status": "success", "data": {"api_call": "getPatients", "results": {"patients": [...]}}}
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert "getPatients" in str(request.url.path)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getPatients",
                    "results": {
                        "patients": [
                            {
                                "ID": "101",
                                "FirstName": "Sarah",
                                "LastName": "Connor",
                                "DOB": "1985-05-12",
                                "Mobile": "+61411222333",
                                "Email": "sarah@example.com",
                                "address": {
                                    "city": "Melbourne",
                                    "state": "VIC",
                                },
                            },
                            {
                                "ID": "102",
                                "FirstName": "John",
                                "LastName": "Connor",
                                "DOB": "2000-02-28",
                                "Mobile": "+61499887766",
                                "Email": "john@example.com",
                                "suburb": "Richmond",
                            },
                        ]
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    patients = nookal.search_patients()
    assert len(patients) == 2
    assert patients[0].patient_id == "101"
    assert patients[0].display_name == "Sarah Connor"
    assert patients[0].phone == "+61411222333"
    assert patients[0].email == "sarah@example.com"
    assert patients[0].suburb == "Melbourne"
    assert patients[0].date_of_birth == date(1985, 5, 12)

    assert patients[1].patient_id == "102"
    assert patients[1].display_name == "John Connor"
    assert patients[1].suburb == "Richmond"

    # Filtered search
    filtered = nookal.search_patients(suburb="Richmond")
    assert len(filtered) == 1
    assert filtered[0].patient_id == "102"


def test_list_appointments_with_official_nookal_envelope() -> None:
    """
    Verifies that list_appointments correctly parses the official Nookal API response shape:
    {"status": "success", "data": {"api_call": "getAppointments", "results": {"appointments": [...]}}}
    """
    def handler(request: httpx.Request) -> httpx.Response:
        assert "getAppointments" in str(request.url.path)
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getAppointments",
                    "results": {
                        "appointments": [
                            {
                                "ID": "appt_501",
                                "patientID": "101",
                                "date": "2026-09-15",
                                "startTime": "09:30:00",
                                "endTime": "10:00:00",
                                "cancelled": "0",
                                "DNA": "0",
                                "arrived": "1",
                                "locationID": "loc_10",
                                "practitionerID": "prac_5",
                            },
                            {
                                "ID": "appt_502",
                                "patientID": "102",
                                "date": "2026-09-15",
                                "startTime": "14:00:00",
                                "endTime": "14:45:00",
                                "cancelled": "1",
                                "cancellationDate": "2026-09-14 11:00:00",
                            },
                        ]
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    appts = nookal.list_appointments()
    assert len(appts) == 2
    assert appts[0].appointment_id == "appt_501"
    assert appts[0].patient_id == "101"
    assert appts[0].starts_at == datetime(2026, 9, 15, 9, 30)
    assert appts[0].status == "arrived"
    assert appts[0].location_id == "loc_10"
    assert appts[0].practitioner_id == "prac_5"

    assert appts[1].appointment_id == "appt_502"
    assert appts[1].patient_id == "102"
    assert appts[1].status == "cancelled"


def test_dict_of_records_envelope_support() -> None:
    """
    Verifies that PHP associative array dict-of-records collections
    {"results": {"patients": {"0": {...}, "1": {...}}}} are parsed.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getPatients",
                    "results": {
                        "patients": {
                            "0": {"ID": "201", "FirstName": "Kyle", "LastName": "Reese"},
                            "1": {"ID": "202", "FirstName": "Miles", "LastName": "Dyson"},
                        }
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    patients = nookal.search_patients()
    assert len(patients) == 2
    assert patients[0].patient_id == "201"
    assert patients[0].display_name == "Kyle Reese"
    assert patients[1].patient_id == "202"
    assert patients[1].display_name == "Miles Dyson"


# ---------------------------------------------------------------------------
# Regression tests: actual live Nookal response shape (camelCase fields)
# ---------------------------------------------------------------------------


def test_live_nookal_appointment_camelcase_fields() -> None:
    """
    Regression: live Nookal /getAppointments returns camelCase field names
    like appointmentDate, appointmentStartTime, appointmentEndTime.
    The parser must handle these without raising NookalValidationError.
    Uses sanitized fixture data — no real patient information.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getAppointments",
                    "results": {
                        "appointments": [
                            {
                                "ID": "90001",
                                "patientID": "5001",
                                "appointmentDate": "2026-09-10",
                                "appointmentStartTime": "09:00:00",
                                "appointmentEndTime": "09:30:00",
                                "locationID": "1",
                                "practitionerID": "12",
                                "typeID": "27",
                                "type": "Consultation",
                                "arrived": "0",
                                "DNA": "0",
                                "cancelled": "0",
                                "notes": "Follow-up",
                                "emailReminderSent": "1",
                                "invoiceGenerated": "0",
                                "cancellationDate": "",
                                "dateCreated": "2026-09-01 08:00:00",
                                "dateModified": "2026-09-01 08:00:00",
                            },
                            {
                                "ID": "90002",
                                "patientID": "5002",
                                "appointmentDate": "2026-09-10",
                                "appointmentStartTime": "10:00:00",
                                "appointmentEndTime": "10:45:00",
                                "locationID": "1",
                                "practitionerID": "15",
                                "typeID": "33",
                                "type": "Consultation",
                                "arrived": "1",
                                "DNA": "0",
                                "cancelled": "0",
                                "notes": "",
                            },
                            {
                                "ID": "90003",
                                "patientID": "5003",
                                "appointmentDate": "2026-09-10",
                                "appointmentStartTime": "11:00:00",
                                "appointmentEndTime": "11:30:00",
                                "locationID": "2",
                                "practitionerID": "12",
                                "cancelled": "1",
                                "cancellationDate": "2026-09-09 16:30:00",
                                "arrived": "0",
                                "DNA": "0",
                            },
                            {
                                "ID": "90004",
                                "patientID": "5004",
                                "appointmentDate": "2026-09-10",
                                "appointmentStartTime": "14:00:00",
                                "appointmentEndTime": "14:30:00",
                                "locationID": "1",
                                "practitionerID": "12",
                                "arrived": "0",
                                "DNA": "1",
                                "cancelled": "0",
                            },
                        ]
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    appts = nookal.list_appointments(on_date=date(2026, 9, 10))
    assert len(appts) == 4

    # Appointment 1: normal booked
    assert appts[0].appointment_id == "90001"
    assert appts[0].patient_id == "5001"
    assert appts[0].starts_at == datetime(2026, 9, 10, 9, 0)
    assert appts[0].ends_at == datetime(2026, 9, 10, 9, 30)
    assert appts[0].status == "booked"
    assert appts[0].location_id == "1"
    assert appts[0].practitioner_id == "12"

    # Appointment 2: arrived
    assert appts[1].appointment_id == "90002"
    assert appts[1].patient_id == "5002"
    assert appts[1].starts_at == datetime(2026, 9, 10, 10, 0)
    assert appts[1].ends_at == datetime(2026, 9, 10, 10, 45)
    assert appts[1].status == "arrived"
    assert appts[1].practitioner_id == "15"

    # Appointment 3: cancelled
    assert appts[2].appointment_id == "90003"
    assert appts[2].status == "cancelled"

    # Appointment 4: DNA
    assert appts[3].appointment_id == "90004"
    assert appts[3].status == "dna"


def test_live_nookal_appointment_mixed_casing() -> None:
    """
    Regression: Nookal may return a mix of date/startTime alongside
    appointmentDate/appointmentStartTime across API versions.
    Both must parse correctly.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {
                        "ID": "80001",
                        "patientID": "4001",
                        "date": "2026-09-12",
                        "startTime": "08:30:00",
                        "endTime": "09:00:00",
                        "cancelled": "0",
                        "DNA": "0",
                        "arrived": "0",
                    },
                    {
                        "ID": "80002",
                        "patientID": "4002",
                        "appointmentDate": "2026-09-12",
                        "appointmentStartTime": "10:30:00",
                        "appointmentEndTime": "11:00:00",
                        "cancelled": "0",
                        "DNA": "0",
                        "arrived": "0",
                    },
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    appts = nookal.list_appointments()
    assert len(appts) == 2

    assert appts[0].appointment_id == "80001"
    assert appts[0].starts_at == datetime(2026, 9, 12, 8, 30)
    assert appts[0].ends_at == datetime(2026, 9, 12, 9, 0)

    assert appts[1].appointment_id == "80002"
    assert appts[1].starts_at == datetime(2026, 9, 12, 10, 30)
    assert appts[1].ends_at == datetime(2026, 9, 12, 11, 0)


def test_live_nookal_patient_camelcase_fields() -> None:
    """
    Regression: live Nookal /getPatients may return camelCase field names
    like firstName, lastName, dateOfBirth instead of FirstName, LastName, DOB.
    Uses sanitized fixture data — no real patient information.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getPatients",
                    "results": {
                        "patients": [
                            {
                                "ID": "7001",
                                "firstName": "Test",
                                "lastName": "Patient",
                                "dateOfBirth": "1990-06-15",
                                "email": "test@example.com",
                                "mobile": "+61400000001",
                                "suburb": "Testville",
                            },
                            {
                                "ID": "7002",
                                "firstName": "Sample",
                                "lastName": "User",
                                "DOB": "1985-03-20",
                                "email": "sample@example.com",
                                "mobile": "+61400000002",
                            },
                        ]
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    patients = nookal.search_patients()
    assert len(patients) == 2

    assert patients[0].patient_id == "7001"
    assert patients[0].display_name == "Test Patient"
    assert patients[0].date_of_birth == date(1990, 6, 15)
    assert patients[0].phone == "+61400000001"
    assert patients[0].email == "test@example.com"
    assert patients[0].suburb == "Testville"

    assert patients[1].patient_id == "7002"
    assert patients[1].display_name == "Sample User"
    assert patients[1].date_of_birth == date(1985, 3, 20)


def test_live_nookal_dict_of_records_camelcase() -> None:
    """
    Regression: Nookal PHP may serialize arrays as JSON objects (dict-of-records)
    AND use camelCase field names. Both must be handled together.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getAppointments",
                    "results": {
                        "appointments": {
                            "0": {
                                "ID": "60001",
                                "patientID": "3001",
                                "appointmentDate": "2026-09-15",
                                "appointmentStartTime": "13:00:00",
                                "appointmentEndTime": "13:30:00",
                                "locationID": "1",
                                "practitionerID": "8",
                                "arrived": "0",
                                "DNA": "0",
                                "cancelled": "0",
                            },
                            "1": {
                                "ID": "60002",
                                "patientID": "3002",
                                "appointmentDate": "2026-09-15",
                                "appointmentStartTime": "14:00:00",
                                "appointmentEndTime": "14:45:00",
                                "locationID": "2",
                                "practitionerID": "8",
                                "arrived": "1",
                                "DNA": "0",
                                "cancelled": "0",
                            },
                        }
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    appts = nookal.list_appointments()
    assert len(appts) == 2
    assert appts[0].appointment_id == "60001"
    assert appts[0].starts_at == datetime(2026, 9, 15, 13, 0)
    assert appts[0].ends_at == datetime(2026, 9, 15, 13, 30)
    assert appts[0].status == "booked"

    assert appts[1].appointment_id == "60002"
    assert appts[1].starts_at == datetime(2026, 9, 15, 14, 0)
    assert appts[1].status == "arrived"


def test_get_appointment_by_id_camelcase() -> None:
    """
    Regression: get_appointment() must find an appointment by ID when the
    response uses camelCase field names from the live API.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getAppointments",
                    "results": {
                        "appointments": [
                            {
                                "ID": "55001",
                                "patientID": "2001",
                                "appointmentDate": "2026-09-20",
                                "appointmentStartTime": "16:00:00",
                                "appointmentEndTime": "16:30:00",
                                "locationID": "1",
                                "practitionerID": "5",
                                "arrived": "0",
                                "DNA": "0",
                                "cancelled": "0",
                            },
                        ]
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    appt = nookal.get_appointment("55001")
    assert appt.appointment_id == "55001"
    assert appt.patient_id == "2001"
    assert appt.starts_at == datetime(2026, 9, 20, 16, 0)
    assert appt.ends_at == datetime(2026, 9, 20, 16, 30)
    assert appt.location_id == "1"
    assert appt.practitioner_id == "5"
    assert appt.status == "booked"

    with pytest.raises(NookalNotFound):
        nookal.get_appointment("99999")


def test_appointment_robust_datetime_and_id_variants() -> None:
    """
    Ensure _parse_appointment handles:
    - appointmentDate containing full datetime string (no startTime field)
    - appointmentDate date-only string (defaults to midnight)
    - appointmentTime alternate field name
    - appointmentId and patientId (lowercase d)
    - duration field calculating ends_at
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getAppointments",
                    "results": {
                        "appointments": [
                            {
                                "appointmentId": "70001",
                                "patientId": "3001",
                                "appointmentDate": "2026-10-01 11:30:00",
                                "duration": "45",
                            },
                            {
                                "appointmentID": "70002",
                                "patientID": "3002",
                                "appointmentDate": "2026-10-02",
                            },
                            {
                                "id": "70003",
                                "patient_id": "3003",
                                "date": "2026-10-03",
                                "appointmentTime": "15:45:00",
                                "appointmentEndTime": "16:15:00",
                            },
                        ]
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    appts = nookal.list_appointments()
    assert len(appts) == 3

    # Full timestamp in appointmentDate + duration calculation for ends_at
    assert appts[0].appointment_id == "70001"
    assert appts[0].patient_id == "3001"
    assert appts[0].starts_at == datetime(2026, 10, 1, 11, 30)
    assert appts[0].ends_at == datetime(2026, 10, 1, 12, 15)

    # Date-only in appointmentDate
    assert appts[1].appointment_id == "70002"
    assert appts[1].patient_id == "3002"
    assert appts[1].starts_at == datetime(2026, 10, 2, 0, 0)
    assert appts[1].ends_at is None

    # appointmentTime variant
    assert appts[2].appointment_id == "70003"
    assert appts[2].patient_id == "3003"
    assert appts[2].starts_at == datetime(2026, 10, 3, 15, 45)
    assert appts[2].ends_at == datetime(2026, 10, 3, 16, 15)


def test_patient_id_variants_in_search_and_get() -> None:
    """
    Ensure get_patient and _parse_patient work with patientId and patientID.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "api_call": "getPatients",
                    "results": {
                        "patients": [
                            {
                                "patientId": "9001",
                                "firstName": "Alice",
                                "lastName": "Wonder",
                            }
                        ]
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url=BASE_URL)
    nookal = HttpNookalClient(config=_make_config(), client=client)

    patient = nookal.get_patient("9001")
    assert patient.patient_id == "9001"
    assert patient.display_name == "Alice Wonder"

