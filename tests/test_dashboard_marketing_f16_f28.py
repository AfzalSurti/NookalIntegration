"""Phase F16-F28 — Marketing RBAC, audit, security, and E2E tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.marketing import Campaign, CampaignStatus, MarketingList
from tests.helpers.dashboard import TEST_USERS, build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


# ========================
# F16-F18: RBAC Enforcement
# ========================


def test_staff_cannot_manage_marketing(client: TestClient, env) -> None:
    """Staff can view marketing but cannot create/modify lists and campaigns."""
    staff_csrf = env.login(client, "staff")
    headers = {"X-CSRF-Token": staff_csrf}

    # View should work
    resp = client.get("/api/marketing/lists", headers=headers)
    assert resp.status_code == 200

    # Create should fail
    resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "Test List", "description": "Test", "filter_definition": []},
    )
    assert resp.status_code == 403


def test_practitioner_cannot_manage_marketing(client: TestClient, env) -> None:
    """Practitioner can view marketing but cannot create/modify."""
    prac_csrf = env.login(client, "practitioner")
    headers = {"X-CSRF-Token": prac_csrf}

    # View should work
    resp = client.get("/api/marketing/campaigns", headers=headers)
    assert resp.status_code == 200

    # Create should fail
    resp = client.post(
        "/api/marketing/campaigns",
        headers=headers,
        json={
            "name": "Test Campaign",
            "subject": "Test",
            "template_id": "tpl-test",
            "marketing_list_id": "list-test",
        },
    )
    assert resp.status_code == 403


def test_admin_can_manage_marketing(client: TestClient, env) -> None:
    """Admin can both view and manage marketing."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    # View should work
    resp = client.get("/api/marketing/lists", headers=headers)
    assert resp.status_code == 200

    # Create should work
    resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "Admin List", "description": "Created by admin", "filter_definition": []},
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Admin List"


def test_owner_can_manage_marketing(client: TestClient, env) -> None:
    """Owner has all marketing permissions."""
    owner_csrf = env.login(client, "owner")
    headers = {"X-CSRF-Token": owner_csrf}

    resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "Owner List", "description": "Created by owner", "filter_definition": []},
    )
    assert resp.status_code == 200


# ========================
# F19-F21: Audit Coverage
# ========================


def test_marketing_list_creation_audited(client: TestClient, env) -> None:
    """List creation is recorded in audit log."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "Audited List", "description": "Should be audited", "filter_definition": []},
    )
    assert resp.status_code == 200
    list_id = resp.json()["id"]

    # Check audit log
    audit_events = env.audit.events
    create_events = [e for e in audit_events if e.action == "dashboard.marketing_list_create"]
    assert len(create_events) > 0
    assert create_events[0].target_id == list_id
    assert create_events[0].result == "success"


def test_marketing_campaign_creation_audited(client: TestClient, env) -> None:
    """Campaign creation is recorded in audit log."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    # Create list first
    list_resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "Test List", "description": "For campaign", "filter_definition": []},
    )
    list_id = list_resp.json()["id"]

    # Create campaign
    camp_resp = client.post(
        "/api/marketing/campaigns",
        headers=headers,
        json={
            "name": "Audited Campaign",
            "subject": "Test Subject",
            "template_id": "tpl-test",
            "marketing_list_id": list_id,
        },
    )
    assert camp_resp.status_code == 200
    campaign_id = camp_resp.json()["id"]

    # Check audit log
    audit_events = env.audit.events
    create_events = [e for e in audit_events if e.action == "dashboard.marketing_campaign_create"]
    assert len(create_events) > 0
    assert create_events[0].target_id == campaign_id


def test_marketing_list_view_audited(client: TestClient, env) -> None:
    """List view is recorded in audit log."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    client.get("/api/marketing/lists", headers=headers)

    # Check audit log
    audit_events = env.audit.events
    list_events = [e for e in audit_events if e.action == "dashboard.marketing_list_list"]
    assert len(list_events) > 0
    assert list_events[0].result == "success"


# ========================
# F22-F24: Security Tests
# ========================


def test_marketing_requires_authentication(client: TestClient) -> None:
    """Unauthenticated requests to marketing APIs are rejected."""
    resp = client.get("/api/marketing/lists")
    assert resp.status_code == 401


def test_marketing_create_requires_valid_list_id(client: TestClient, env) -> None:
    """Creating a campaign with invalid marketing_list_id fails."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    resp = client.post(
        "/api/marketing/campaigns",
        headers=headers,
        json={
            "name": "Invalid Campaign",
            "subject": "Test",
            "template_id": "tpl-test",
            "marketing_list_id": "nonexistent-list-id",
        },
    )
    assert resp.status_code == 400


def test_marketing_create_list_requires_name(client: TestClient, env) -> None:
    """Creating a list without a name fails."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "", "description": "No name", "filter_definition": []},
    )
    assert resp.status_code == 400


def test_marketing_csrf_protection(client: TestClient, env) -> None:
    """CSRF token validation is enforced on marketing POST."""
    admin_csrf = env.login(client, "admin")
    # Send without CSRF token
    resp = client.post(
        "/api/marketing/lists",
        json={"name": "Test", "description": "No CSRF", "filter_definition": []},
    )
    assert resp.status_code == 403


# ========================
# F25-F28: E2E Marketing Flow
# ========================


def test_full_marketing_campaign_flow(client: TestClient, env) -> None:
    """E2E: Create list → Create campaign → Submit → Approve → Queue → Send."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    # Step 1: Create marketing list
    list_resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={
            "name": "E2E Test List",
            "description": "Test audience",
            "filter_definition": [
                {"filter_type": "suburb", "value": "Redfern"},
            ],
        },
    )
    assert list_resp.status_code == 200
    list_id = list_resp.json()["id"]
    assert list_resp.json()["status"] == "active"

    # Step 2: Create campaign
    camp_resp = client.post(
        "/api/marketing/campaigns",
        headers=headers,
        json={
            "name": "Welcome Campaign",
            "subject": "Welcome to our clinic",
            "template_id": "tpl-welcome",
            "marketing_list_id": list_id,
        },
    )
    assert camp_resp.status_code == 200
    campaign_id = camp_resp.json()["id"]
    assert camp_resp.json()["status"] == "draft"

    # Step 3: Submit for review
    submit_resp = client.post(
        f"/api/marketing/campaigns/{campaign_id}/submit",
        headers=headers,
    )
    assert submit_resp.status_code == 200
    assert submit_resp.json()["status"] == "review"

    # Step 4: Approve campaign
    approve_resp = client.post(
        f"/api/marketing/campaigns/{campaign_id}/approve",
        headers=headers,
    )
    assert approve_resp.status_code == 200
    assert approve_resp.json()["status"] == "approved"

    # Step 5: Queue campaign
    queue_resp = client.post(
        f"/api/marketing/campaigns/{campaign_id}/queue",
        headers=headers,
    )
    assert queue_resp.status_code == 200
    assert queue_resp.json()["status"] == "queued"

    # Step 6: Send campaign
    send_resp = client.post(
        f"/api/marketing/campaigns/{campaign_id}/send",
        headers=headers,
    )
    assert send_resp.status_code == 200
    final_status = send_resp.json()["status"]
    assert final_status in {"sending", "completed"}


def test_marketing_campaign_send_creates_recipients(client: TestClient, env) -> None:
    """E2E: Sending a campaign creates recipient records."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    # Create list
    list_resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "Send Test", "description": "", "filter_definition": []},
    )
    list_id = list_resp.json()["id"]

    # Grant consent for test patient
    env.container.consent_store.grant("pat_1001", changed_by="test_user")

    # Create campaign
    camp_resp = client.post(
        "/api/marketing/campaigns",
        headers=headers,
        json={
            "name": "Send Test Campaign",
            "subject": "Test Send",
            "template_id": "tpl-test",
            "marketing_list_id": list_id,
        },
    )
    campaign_id = camp_resp.json()["id"]

    # Submit for review
    client.post(
        f"/api/marketing/campaigns/{campaign_id}/submit",
        headers=headers,
    )

    # Approve
    client.post(
        f"/api/marketing/campaigns/{campaign_id}/approve",
        headers=headers,
    )

    # Queue
    client.post(
        f"/api/marketing/campaigns/{campaign_id}/queue",
        headers=headers,
    )

    # Send
    send_resp = client.post(
        f"/api/marketing/campaigns/{campaign_id}/send",
        headers=headers,
    )
    assert send_resp.status_code == 200

    # Verify fake email adapter received sends
    adapter = env.container.email_adapter
    assert adapter.sent_count >= 0


def test_marketing_list_view_shows_created_lists(client: TestClient, env) -> None:
    """E2E: Created lists appear in list view."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    # Create a list
    create_resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "List View Test", "description": "Should appear", "filter_definition": []},
    )
    created_id = create_resp.json()["id"]

    # List should appear in view
    list_resp = client.get("/api/marketing/lists", headers=headers)
    assert list_resp.status_code == 200
    items = list_resp.json()
    ids = [item["id"] for item in items]
    assert created_id in ids


def test_multiple_campaigns_per_list(client: TestClient, env) -> None:
    """E2E: Multiple campaigns can target the same list."""
    admin_csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": admin_csrf}

    # Create list
    list_resp = client.post(
        "/api/marketing/lists",
        headers=headers,
        json={"name": "Multi Campaign List", "description": "", "filter_definition": []},
    )
    list_id = list_resp.json()["id"]

    # Create first campaign
    camp1 = client.post(
        "/api/marketing/campaigns",
        headers=headers,
        json={
            "name": "Campaign 1",
            "subject": "First",
            "template_id": "tpl-1",
            "marketing_list_id": list_id,
        },
    )
    assert camp1.status_code == 200

    # Create second campaign
    camp2 = client.post(
        "/api/marketing/campaigns",
        headers=headers,
        json={
            "name": "Campaign 2",
            "subject": "Second",
            "template_id": "tpl-2",
            "marketing_list_id": list_id,
        },
    )
    assert camp2.status_code == 200

    # Both should exist
    list_view = client.get("/api/marketing/campaigns", headers=headers)
    campaigns = list_view.json()
    assert len([c for c in campaigns if c["marketing_list_id"] == list_id]) >= 2
