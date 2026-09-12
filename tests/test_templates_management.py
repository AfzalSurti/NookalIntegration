"""
Tests for Template Management API, Service, and Views.
"""
from __future__ import annotations

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

from app.dashboard.app import create_app
from app.dashboard.auth import User
from app.dashboard.container import DashboardContainer
from app.dashboard.production import build_production_container
from app.shared.config import Settings, get_settings
from tests.helpers.dashboard import TEST_USERS, build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def _login(client: TestClient, role: str = "admin") -> dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"username": TEST_USERS[role][0].username, "password": TEST_USERS[role][1]},
    )
    assert resp.status_code == 200, resp.text
    return {"X-CSRF-Token": resp.json()["csrf_token"]}


@pytest.fixture
def templates_test_env(tmp_path: Path):
    """Set up test environment with isolated templates and sessions."""
    from app.dashboard.auth import SessionStore
    from app.dashboard.services.templates import CampaignTemplateStore, TemplateManagementService
    from app.shared.audit import AuditLog
    from app.shared.clock import SystemClock

    clock = SystemClock()
    audit = AuditLog(directory=tmp_path / "audit", clock=clock)

    # Isolated messaging templates
    msg_dir = tmp_path / "messaging_templates"
    msg_dir.mkdir(parents=True, exist_ok=True)
    (msg_dir / "appointment_reminder.txt").write_text(
        "Hi {name}, reminder for {date} at {time}.", encoding="utf-8"
    )
    (msg_dir / "direct_message.txt").write_text(
        "Hi {name}, {message} — Back to Ease", encoding="utf-8"
    )

    # Isolated letter templates
    letters_dir = tmp_path / "letters_templates"
    letters_dir.mkdir(parents=True, exist_ok=True)
    p_dir = letters_dir / "progress_letter"
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "manifest.yaml").write_text(
        "document_type: progress_letter\nrequired_fields:\n  - patient_label\n  - body\n", encoding="utf-8"
    )
    (p_dir / "body.txt").write_text(
        "Patient: {patient_label}\n\n{body}\n", encoding="utf-8"
    )

    # Isolated marketing templates store
    mkt_store = CampaignTemplateStore(store_path=tmp_path / "campaign_templates.jsonl")

    svc = TemplateManagementService(
        messaging_dir=msg_dir,
        letters_dir=letters_dir,
        marketing_store=mkt_store,
    )

    return {
        "svc": svc,
        "msg_dir": msg_dir,
        "letters_dir": letters_dir,
        "mkt_store": mkt_store,
    }


def test_service_list_and_get(templates_test_env):
    svc = templates_test_env["svc"]
    all_tpls = svc.list_all()
    assert len(all_tpls) >= 3  # At least 2 comm + 1 letter + 4 marketing

    comm_tpls = svc.list_all(category="communication")
    assert any(t.id == "appointment_reminder" for t in comm_tpls)
    assert any(t.id == "direct_message" for t in comm_tpls)

    letter_tpls = svc.list_all(category="letters")
    assert any(t.id == "progress_letter" for t in letter_tpls)

    mkt_tpls = svc.list_all(category="marketing")
    assert any(t.id == "general_newsletter_v1" for t in mkt_tpls)


def test_service_preview(templates_test_env):
    svc = templates_test_env["svc"]
    res = svc.preview(
        category="communication",
        template_id="appointment_reminder",
        sample_context={"name": "Alice", "date": "Tomorrow", "time": "2pm"},
    )
    assert "Alice" in res["rendered"]
    assert "Tomorrow" in res["rendered"]
    assert "2pm" in res["rendered"]
    assert res["char_count"] > 0


def test_service_update_communication_validation(templates_test_env):
    svc = templates_test_env["svc"]

    # Empty body is rejected
    with pytest.raises(ValueError, match="cannot be empty"):
        svc.update(
            category="communication",
            template_id="appointment_reminder",
            body="   ",
            actor="admin",
        )

    # Editing direct_message without {message} succeeds cleanly (user's exact workflow)
    updated_dm = svc.update(
        category="communication",
        template_id="direct_message",
        body="Hi {name} - THIS IS TEST Back to Ease",
        actor="admin",
    )
    assert updated_dm.body == "Hi {name} - THIS IS TEST Back to Ease"
    msg_dir = templates_test_env["msg_dir"]
    assert (msg_dir / "direct_message.txt").read_text(encoding="utf-8") == "Hi {name} - THIS IS TEST Back to Ease"

    # Preview works cleanly with edited template
    preview = svc.preview(
        category="communication",
        template_id="direct_message",
        sample_context={"name": "Jane Doe"},
    )
    assert preview["rendered"] == "Hi Jane Doe - THIS IS TEST Back to Ease"

    # Editing appointment_reminder without {time} succeeds cleanly
    updated = svc.update(
        category="communication",
        template_id="appointment_reminder",
        body="Hello {name}! Reminder for your session on {date}.",
        actor="admin",
    )
    assert updated.body == "Hello {name}! Reminder for your session on {date}."
    disk_content = (msg_dir / "appointment_reminder.txt").read_text(encoding="utf-8")
    assert disk_content == "Hello {name}! Reminder for your session on {date}."


def test_service_update_letters_validation(templates_test_env):
    svc = templates_test_env["svc"]

    # Empty body is rejected
    with pytest.raises(ValueError, match="cannot be empty"):
        svc.update(
            category="letters",
            template_id="progress_letter",
            body="",
            actor="dr_smith",
        )

    # Updating letter without requiring all previous placeholders succeeds
    updated = svc.update(
        category="letters",
        template_id="progress_letter",
        body="Only patient: {patient_label}",
        actor="dr_smith",
    )
    assert updated.body == "Only patient: {patient_label}"

    # Verify written to disk
    letters_dir = templates_test_env["letters_dir"]
    disk_content = (letters_dir / "progress_letter" / "body.txt").read_text(encoding="utf-8")
    assert disk_content == "Only patient: {patient_label}"


def test_api_templates_endpoints(client: TestClient, env):
    # Test GET /api/templates
    resp = env.authed(client, "GET", "/api/templates?category=communication", role="admin")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert any(t["id"] == "appointment_reminder" for t in data)

    # Test GET /api/templates/communication/appointment_reminder
    single_resp = env.authed(client, "GET", "/api/templates/communication/appointment_reminder", role="staff")
    assert single_resp.status_code == 200
    t = single_resp.json()
    assert t["id"] == "appointment_reminder"
    assert "name" in t["all_placeholders"]

    # Test POST /api/templates/preview
    preview_resp = env.authed(
        client,
        "POST",
        "/api/templates/preview",
        role="staff",
        json={"category": "communication", "template_id": "appointment_reminder"},
    )
    assert preview_resp.status_code == 200
    p = preview_resp.json()
    assert "rendered" in p
    assert p["char_count"] > 0

    # Test PUT /api/templates/marketing/{template_id}
    update_resp = env.authed(
        client,
        "PUT",
        "/api/templates/marketing/general_newsletter_v1",
        role="admin",
        json={
            "body": "Dear {first_name}, updated newsletter from Back to Ease.",
            "subject": "New Spring Updates",
        },
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["success"] is True

    # Test PUT /api/templates/communication/direct_message without {message} (user's exact workflow)
    dm_update_resp = env.authed(
        client,
        "PUT",
        "/api/templates/communication/direct_message",
        role="admin",
        json={
            "body": "Hi {name} - THIS IS TEST Back to Ease",
        },
    )
    assert dm_update_resp.status_code == 200, dm_update_resp.text
    assert dm_update_resp.json()["success"] is True
    assert dm_update_resp.json()["template"]["body"] == "Hi {name} - THIS IS TEST Back to Ease"


def test_templates_page_rendering(client: TestClient, env):
    # Test GET /templates renders HTML for staff
    resp = env.authed(client, "GET", "/templates", role="staff")
    assert resp.status_code == 200
    assert "Template Management" in resp.text
    assert "appointment_reminder" in resp.text
    assert "Clinical Letters" in resp.text
    assert "Marketing Campaign Templates" in resp.text
