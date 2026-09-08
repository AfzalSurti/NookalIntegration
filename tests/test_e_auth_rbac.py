"""Phase E — authentication and authorization API tests."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.helpers.dashboard import TEST_USERS, build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def test_unauthenticated_request_rejected(client: TestClient) -> None:
    resp = client.get("/api/patients/search")
    assert resp.status_code == 401


def test_valid_login(client: TestClient) -> None:
    user, password = TEST_USERS["admin"]
    resp = client.post("/api/auth/login", json={"username": user.username, "password": password})
    assert resp.status_code == 200
    data = resp.json()
    assert data["user"]["role"] == "admin"
    assert data["csrf_token"]
    assert "bte_session" in resp.cookies


def test_invalid_login(client: TestClient) -> None:
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid_credentials"


def test_session_token_validation(client: TestClient, env) -> None:
    csrf = env.login(client, "staff")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "staff"
    assert me.json()["csrf_token"] == csrf


def test_logout_invalidation(client: TestClient, env) -> None:
    env.login(client, "staff")
    assert client.get("/api/auth/me").status_code == 200
    out = client.post("/api/auth/logout")
    assert out.status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_staff_allowed_actions(client: TestClient, env) -> None:
    csrf = env.login(client, "staff")
    headers = {"X-CSRF-Token": csrf}
    assert client.get("/api/patients/search", headers=headers).status_code == 200
    assert client.get("/api/appointments", headers=headers).status_code == 200
    assert client.get("/api/approvals", headers=headers).status_code == 200
    assert client.get("/api/system/status", headers=headers).status_code == 200


def test_practitioner_allowed_actions(client: TestClient, env) -> None:
    csrf = env.login(client, "practitioner")
    headers = {"X-CSRF-Token": csrf}
    assert client.get("/api/referrers/conflicts", headers=headers).status_code == 200
    # Create a pending task then try approve
    task = env.container.approval.create_task(
        task_type=__import__("app.approval", fromlist=["TaskType"]).TaskType.WHATSAPP_REPLY,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={"template_id": "reply", "status_tag": "DRAFT"},
    )
    # WhatsApp reply may lack handler — use reject instead for practitioner permission check
    resp = client.post(
        f"/api/approvals/{task.id}/reject",
        headers=headers,
        json={"notes": "not needed"},
    )
    assert resp.status_code == 200


def test_admin_allowed_actions(client: TestClient, env) -> None:
    csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": csrf}
    assert client.get("/api/audit", headers=headers).status_code == 200
    assert client.get("/api/referrers/conflicts", headers=headers).status_code == 200


def test_unauthorized_approval_blocked(client: TestClient, env) -> None:
    task = env.container.approval.create_task(
        task_type=__import__("app.approval", fromlist=["TaskType"]).TaskType.CERTIFICATE,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "attendance",
            "status_tag": "DRAFT",
            "source_facts": {"certificate_type": "attendance", "patient_label": "patient:pat_1001"},
        },
    )
    csrf = env.login(client, "staff")
    resp = client.post(
        f"/api/approvals/{task.id}/approve",
        headers={"X-CSRF-Token": csrf},
        json={},
    )
    assert resp.status_code == 403


def test_unauthorized_audit_access_blocked(client: TestClient, env) -> None:
    csrf = env.login(client, "staff")
    resp = client.get("/api/audit", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


def test_unauthorized_kill_switch_blocked(client: TestClient, env) -> None:
    csrf = env.login(client, "practitioner")
    resp = client.post(
        "/api/system/kill-switch",
        headers={"X-CSRF-Token": csrf},
        json={"active": True, "reason": "test"},
    )
    assert resp.status_code == 403


def test_direct_api_privilege_escalation_blocked(client: TestClient, env) -> None:
    """Staff cannot hit admin-only endpoints even with crafted requests."""
    csrf = env.login(client, "staff")
    headers = {"X-CSRF-Token": csrf}
    assert client.get("/api/audit", headers=headers).status_code == 403
    assert client.post(
        "/api/system/kill-switch",
        headers=headers,
        json={"active": True},
    ).status_code == 403
    assert client.post(
        "/api/appointments/actions",
        headers=headers,
        json={"action": "cancel", "phone": "+61411110001", "message": "cancel"},
    ).status_code == 403
