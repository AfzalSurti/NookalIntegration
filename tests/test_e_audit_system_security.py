"""Phase E — audit viewer, kill switch, and security boundary tests."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.approval import TaskType
from app.shared.exceptions import KillSwitchActive, repo_root
from tests.helpers.dashboard import build_dashboard_env


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    return build_dashboard_env(tmp_path, monkeypatch=monkeypatch)


@pytest.fixture()
def client(env) -> TestClient:
    return env.client()


def test_audit_filtering(client: TestClient, env) -> None:
    env.authed(client, "GET", "/api/patients/search")
    resp = env.authed(
        client,
        "GET",
        "/api/audit",
        params={"action": "dashboard.patient_search"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert all(e["action"] == "dashboard.patient_search" for e in resp.json())


def test_audit_correlation_id(client: TestClient, env) -> None:
    csrf = env.login(client, "admin")
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": "corr-audit-xyz"}
    client.get("/api/patients/search", headers=headers)
    resp = client.get(
        "/api/audit",
        headers=headers,
        params={"correlation_id_filter": "corr-audit-xyz"},
    )
    assert resp.status_code == 200
    assert len(resp.json()) >= 1
    assert all(
        e["metadata"].get("correlation_id") == "corr-audit-xyz" for e in resp.json()
    )


def test_audit_no_patient_content(client: TestClient, env) -> None:
    env.authed(client, "GET", "/api/patients/pat_1001")
    resp = env.authed(client, "GET", "/api/audit", params={"action": "dashboard.patient_view"})
    for event in resp.json():
        blob = str(event).lower()
        assert "alex" not in blob
        assert "rivera" not in blob
        meta = event.get("metadata") or {}
        assert "patient_name" not in meta
        assert "name" not in meta


def test_audit_no_message_bodies(client: TestClient, env) -> None:
    resp = env.authed(client, "GET", "/api/audit")
    for event in resp.json():
        meta = event.get("metadata") or {}
        for key in meta:
            assert "body" not in key.lower()
            assert "message" not in key.lower() or key == "message_id"


def test_audit_no_secrets(client: TestClient, env) -> None:
    resp = env.authed(client, "GET", "/api/system/status")
    body = resp.json()
    text = str(body).lower()
    assert "api_key" not in text
    assert "password" not in text
    assert "token" not in text or "csrf" in text  # csrf_token may appear elsewhere, not here
    assert "whatsapp" not in text or body.get("messaging_configured") is False


def test_authorized_user_can_enable_kill_switch(client: TestClient, env) -> None:
    resp = env.authed(
        client,
        "POST",
        "/api/system/kill-switch",
        role="admin",
        json={"active": True, "reason": "drill"},
    )
    assert resp.status_code == 200
    assert resp.json()["kill_switch_active"] is True
    assert env.kill_switch_path.exists()


def test_unauthorized_user_blocked_kill_switch(client: TestClient, env) -> None:
    resp = env.authed(
        client,
        "POST",
        "/api/system/kill-switch",
        role="staff",
        json={"active": True},
    )
    assert resp.status_code == 403
    assert not env.kill_switch_path.exists()


def test_nookal_writes_blocked_when_kill_switch_active(client: TestClient, env) -> None:
    env.authed(
        client,
        "POST",
        "/api/system/kill-switch",
        role="admin",
        json={"active": True, "reason": "test"},
    )
    with pytest.raises(KillSwitchActive):
        env.nookal.update_appointment("appt_2001", status="cancelled")


def test_messaging_blocked_when_kill_switch_active(client: TestClient, env) -> None:
    env.authed(
        client,
        "POST",
        "/api/system/kill-switch",
        role="owner",
        json={"active": True},
    )
    result = env.messaging.send(
        "whatsapp",
        "+61411110001",
        "appointment_reminder",
        {"when": "tomorrow"},
        patient_id="pat_1001",
        idempotency_key="ks-msg-1",
        caller_role="admin",
    )
    assert result.status == "blocked"


def test_document_delivery_blocked_when_kill_switch_active(client: TestClient, env) -> None:
    from app.letters.models import DocumentRecord, DocumentStatus, DocumentType

    env.authed(
        client,
        "POST",
        "/api/system/kill-switch",
        role="admin",
        json={"active": True},
    )
    rec = DocumentRecord(
        document_id="doc_1",
        document_type=DocumentType.CERTIFICATE,
        patient_id="pat_1001",
        task_id="task_1",
        template_id="certificate",
        status=DocumentStatus.STORED,
        created_at=env.clock.now().isoformat(),
    )
    with pytest.raises(KillSwitchActive):
        env.document_delivery.deliver(rec, destination="store://doc_1")


def _imports_in(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_no_openclaw_imports_in_dashboard() -> None:
    root = repo_root() / "app" / "dashboard"
    for path in root.rglob("*.py"):
        for mod in _imports_in(path):
            assert "openclaw" not in mod.lower()


def test_no_direct_nookal_http_from_routes() -> None:
    root = repo_root() / "app" / "dashboard" / "api"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "HttpNookalClient" not in text
        assert "httpx" not in text
        for mod in _imports_in(path):
            assert mod != "app.nookal_client.client" or "NookalClient" in text
            assert "HttpNookal" not in mod


def test_no_direct_messaging_provider_from_routes() -> None:
    root = repo_root() / "app" / "dashboard" / "api"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "FakeAdapter" not in text
        assert "WhatsAppAdapter" not in text
        assert "graph.facebook" not in text
        for mod in _imports_in(path):
            assert not mod.startswith("app.messaging.adapters")


def test_no_direct_approval_state_mutation_from_routes() -> None:
    root = repo_root() / "app" / "dashboard" / "api"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "_transition" not in text
        assert "TaskStatus.APPROVED" not in text
        assert "status = TaskStatus" not in text


def test_no_sensitive_secrets_in_responses(client: TestClient, env) -> None:
    resp = env.authed(client, "GET", "/api/system/status")
    payload = resp.json()
    for key in payload:
        assert "key" not in key.lower()
        assert "secret" not in key.lower()
        assert "password" not in key.lower()
    login = env.authed(client, "GET", "/api/auth/me")
    me = login.json()
    assert "password" not in str(me).lower()
    assert me["csrf_token"]  # present but not a long-lived secret in body logs


def test_csrf_required_for_mutating_api(client: TestClient, env) -> None:
    env.login(client, "admin")
    # Cookie present but CSRF header missing
    resp = client.post("/api/system/kill-switch", json={"active": True})
    assert resp.status_code == 403
    assert resp.json()["detail"] == "csrf_failed"


def test_document_review_list(client: TestClient, env) -> None:
    env.container.approval.create_task(
        task_type=TaskType.CERTIFICATE,
        patient_id="pat_1001",
        created_by="llm",
        content_draft={
            "template_id": "certificate",
            "certificate_type": "attendance",
            "status_tag": "DRAFT",
        },
    )
    resp = env.authed(client, "GET", "/api/documents/review", role="practitioner")
    assert resp.status_code == 200
    assert any(t["type"] == "certificate" for t in resp.json())


def test_overview_page_requires_auth(client: TestClient) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 401
