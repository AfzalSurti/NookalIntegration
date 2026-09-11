"""Tests for the dashboard /patients filter panel and backend wiring."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from tests.helpers.dashboard import build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def test_patients_page_renders_filter_panel(client: TestClient, env) -> None:
    """Verify the filter panel and all 6 filter inputs render with buttons."""
    csrf = env.login(client, "staff")
    resp = client.get("/patients", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    html = resp.text

    # Check for all 6 filter controls
    assert 'name="suburb"' in html
    assert 'name="age_min"' in html
    assert 'name="age_max"' in html
    assert 'name="appointment_from"' in html
    assert 'name="appointment_to"' in html
    assert 'name="referrer_id"' in html

    # Check action controls
    assert "Apply Filters" in html
    assert 'href="/patients"' in html
    assert "Clear / Reset" in html

    # Unfiltered view contains seeded patients
    assert "Alex Rivera" in html
    assert "Sam Lee" in html
    assert "Jordan Ng" in html
    assert "Morgan Patel" in html


def test_patients_page_filter_by_suburb(client: TestClient, env) -> None:
    """Filter by Suburb returns matching patients and sets active filter indicator."""
    csrf = env.login(client, "staff")
    resp = client.get("/patients?suburb=Richmond", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    html = resp.text

    # Seeded: Alex Rivera & Jordan Ng are in Richmond; Sam Lee is in Carlton; Morgan Patel is in Fitzroy
    assert "Alex Rivera" in html
    assert "Jordan Ng" in html
    assert "Sam Lee" not in html
    assert "Morgan Patel" not in html

    # Active filter indicator & preserved input value
    assert "Active Filters" in html
    assert 'value="Richmond"' in html


def test_patients_page_filter_by_age_range(client: TestClient, env) -> None:
    """Filter by Min & Max Age filters patients by age based on DOB."""
    csrf = env.login(client, "staff")
    # Ages as of seed (2026-09-07):
    # Alex Rivera: ~36, Sam Lee: ~41, Jordan Ng: ~25, Morgan Patel: ~54
    resp = client.get("/patients?age_min=30&age_max=50", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    html = resp.text

    assert "Alex Rivera" in html
    assert "Sam Lee" in html
    assert "Jordan Ng" not in html
    assert "Morgan Patel" not in html

    assert 'value="30"' in html
    assert 'value="50"' in html


def test_patients_page_filter_by_appointment_date_range(client: TestClient, env) -> None:
    """Filter by appointment date range matches patients who have appointments in that window."""
    csrf = env.login(client, "staff")
    # In seed, Jordan Ng has an appointment on 2026-08-20. The others are in September 2026.
    resp = client.get(
        "/patients?appointment_from=2026-08-01&appointment_to=2026-08-31",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    html = resp.text

    assert "Jordan Ng" in html
    assert "Alex Rivera" not in html
    assert "Sam Lee" not in html
    assert "Morgan Patel" not in html


def test_patients_page_filter_by_referrer(client: TestClient, env) -> None:
    """Filter by Referring Doctor matches patients referred by that doctor."""
    csrf = env.login(client, "staff")
    # In seed, Alex Rivera and Jordan Ng have referrer ref_john_smith
    resp = client.get("/patients?referrer_id=ref_john_smith", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    html = resp.text

    assert "Alex Rivera" in html
    assert "Jordan Ng" in html
    assert "Sam Lee" not in html
    assert "Morgan Patel" not in html


def test_patients_page_empty_form_submission_no_422(client: TestClient, env) -> None:
    """HTML form submitted with blank inputs must not raise 422 Unprocessable Entity."""
    csrf = env.login(client, "staff")
    resp = client.get(
        "/patients?suburb=&age_min=&age_max=&appointment_from=&appointment_to=&referrer_id=",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    html = resp.text

    # All patients present
    assert "Alex Rivera" in html
    assert "Sam Lee" in html
    assert "Jordan Ng" in html
    assert "Morgan Patel" in html

    # No active filter indicator
    assert "Active Filters" not in html


def test_patients_page_no_results_empty_state(client: TestClient, env) -> None:
    """Search with non-matching criteria renders friendly empty state and reset link."""
    csrf = env.login(client, "staff")
    resp = client.get("/patients?suburb=NonExistentSuburbXYZ", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    html = resp.text

    assert "No matching patients found." in html
    assert "Clear / Reset" in html
    assert "0 found" in html


def test_patients_page_requires_authentication(client: TestClient) -> None:
    """Unauthenticated access to /patients is rejected."""
    resp = client.get("/patients")
    assert resp.status_code == 401


def test_patients_page_referrer_not_implemented_fallback(
    client: TestClient, env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When nookal.list_referrers raises NotImplementedError (live Nookal v2), page degrades gracefully."""
    monkeypatch.setattr(
        env.nookal,
        "list_referrers",
        MagicMock(side_effect=NotImplementedError("Nookal v2 does not expose referrers")),
    )
    csrf = env.login(client, "staff")
    resp = client.get("/patients", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    html = resp.text

    # Page loads normally without 500
    assert "Patients" in html
    # Referrer fallback input and explanatory note are displayed
    assert 'name="referrer_id"' in html
    assert "Nookal API v2 does not expose a public directory of referrers" in html


def test_patients_page_audit_logging(client: TestClient, env) -> None:
    """Filter queries trigger audit logging with filter flags without leaking PII in metadata."""
    csrf = env.login(client, "staff")
    resp = client.get(
        "/patients?suburb=Richmond&age_min=20",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200

    # Verify audit event was logged
    search_events = [e for e in env.audit.events if e.action == "dashboard.patient_search"]
    assert len(search_events) >= 1
    last_event = search_events[-1]
    assert last_event.metadata["has_suburb"] is True
    assert last_event.metadata["has_age"] is True
    assert last_event.metadata["has_appt_range"] is False
    assert last_event.result == "success"


def test_patients_page_search_query_handles_nookal_error_gracefully(
    client: TestClient, env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When nookal.search_patients raises NookalError, dashboard page degrades gracefully with 200."""
    from app.shared.exceptions import NookalError

    monkeypatch.setattr(
        env.nookal,
        "search_patients",
        MagicMock(side_effect=NookalError("Nookal error on search_patients: Search variables are missing.")),
    )
    csrf = env.login(client, "staff")
    resp = client.get(
        "/patients?q=Saurabh+Patel&deceased=&suburb=&age_min=&age_max=&appointment_from=&appointment_to=&referrer_id=",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    html = resp.text
    assert "Patients" in html
    assert "0 found" in html or "No matching patients found" in html


