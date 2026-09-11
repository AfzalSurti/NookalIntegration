"""
Tests for Approval Queue functionality and Nookal patient data enrichment.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.approval import TaskStatus, TaskType
from app.nookal_client import MockNookalClient, PatientRef
from tests.helpers.dashboard import TEST_USERS, build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def test_approval_queue_enriches_patient_name_from_nookal(client: TestClient, env) -> None:
    # Seed a patient in Nookal
    if isinstance(env.container.nookal, MockNookalClient):
        env.container.nookal.seed_patient(
            PatientRef(
                patient_id="pat_1001",
                first_name="Jane",
                last_name="Doe",
                phone="0400 123 456",
                email="jane.doe@example.com",
                suburb="Richmond",
                date_of_birth=date(1985, 6, 15),
            )
        )

    # Create an approval task for pat_1001
    task = env.container.approval.create_task(
        task_type=TaskType.LETTER,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "referral_thank_you",
            "letter_type": "referral_thank_you",
            "draft_body": "Thank you for referring Jane Doe. Treatment plan has commenced.",
            "source_facts": {"referrer_name": "Dr. Smith"},
        },
    )

    # Test GET /api/approvals returns enriched patient_name and safe_draft fields
    resp = env.authed(client, "GET", "/api/approvals", role="practitioner")
    assert resp.status_code == 200
    tasks = resp.json()
    matched = [t for t in tasks if t["id"] == task.id]
    assert len(matched) == 1
    t = matched[0]
    assert t["patient_name"] == "Jane Doe"
    assert t["safe_draft"]["draft_body"] == "Thank you for referring Jane Doe. Treatment plan has commenced."
    assert t["safe_draft"]["referrer_name"] == "Dr. Smith"


def test_approvals_html_page_shows_nookal_patient_data(client: TestClient, env) -> None:
    # Seed patient
    if isinstance(env.container.nookal, MockNookalClient):
        env.container.nookal.seed_patient(
            PatientRef(
                patient_id="pat_1002",
                first_name="Robert",
                last_name="Baratheon",
                phone="0400 999 888",
                email="robert@example.com",
                suburb="Brunswick",
                date_of_birth=date(1970, 1, 1),
            )
        )

    task = env.container.approval.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="pat_1002",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "fitness_for_work",
            "statement": "The patient is fit to resume light duties for 14 days.",
        },
    )

    # Fetch HTML view
    resp = env.authed(client, "GET", "/approvals", role="practitioner")
    assert resp.status_code == 200
    html = resp.text

    # Verify Nookal patient details are rendered
    assert "Robert Baratheon" in html
    assert "pat_1002" in html
    assert "0400 999 888" in html
    assert "robert@example.com" in html
    assert "Brunswick" in html
    assert "The patient is fit to resume light duties" in html
    assert "Fitness For Work" in html


def test_approval_and_rejection_workflow(client: TestClient, env) -> None:
    # Seed patient
    if isinstance(env.container.nookal, MockNookalClient):
        env.container.nookal.seed_patient(
            PatientRef(
                patient_id="pat_1001",
                first_name="Jane",
                last_name="Doe",
                phone="0400 123 456",
                email="jane@example.com",
            )
        )

    # Create two tasks with proper letter_type and certificate_type
    t1 = env.container.approval.create_task(
        task_type=TaskType.LETTER,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "referral_thank_you",
            "letter_type": "referral_thank_you",
            "draft_body": "Thank you for referring your patient to Back to Ease.",
            "source_facts": {"referrer_name": "Dr. Sarah Mitchell"},
        },
    )
    t2 = env.container.approval.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "attendance",
            "statement": "Attendance certificate.",
        },
    )

    # Approve t1
    approve_resp = env.authed(
        client,
        "POST",
        f"/api/approvals/{t1.id}/approve",
        role="practitioner",
        json={"notes": "Clinical review confirmed accurate."},
    )
    assert approve_resp.status_code == 200, approve_resp.text
    assert approve_resp.json()["status"] in ("approved", "sent")

    # Reject t2
    reject_resp = env.authed(
        client,
        "POST",
        f"/api/approvals/{t2.id}/reject",
        role="practitioner",
        json={"notes": "Incorrect date stated by patient."},
    )
    assert reject_resp.status_code == 200, reject_resp.text
    assert reject_resp.json()["status"] == "rejected"
