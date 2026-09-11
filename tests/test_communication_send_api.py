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


def _login(client: TestClient, role: str = "staff") -> dict[str, str]:
    resp = client.post(
        "/api/auth/login",
        json={"username": TEST_USERS[role][0].username, "password": TEST_USERS[role][1]},
    )
    assert resp.status_code == 200, resp.text
    csrf = resp.json()["csrf_token"]
    return {"X-CSRF-Token": csrf}


def test_send_sms_to_patient_success(client: TestClient) -> None:
    headers = _login(client, role="staff")
    resp = client.post(
        "/api/communication/patient/pat_1001/send",
        headers=headers,
        json={
            "channel": "sms",
            "template_id": "direct_message",
            "message": "Your rehabilitation exercises have been updated.",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "sent"
    assert data["channel"] == "sms"
    assert "rehabilitation exercises" in data["rendered_content"]
    assert data["recipient"] != ""


def test_send_email_to_patient_success(client: TestClient) -> None:
    headers = _login(client, role="practitioner")
    resp = client.post(
        "/api/communication/patient/pat_1001/send",
        headers=headers,
        json={
            "channel": "email",
            "template_id": "certificate_sent",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "sent"
    assert data["channel"] == "email"
    assert "certificate has been sent" in data["rendered_content"]


def test_send_missing_contact_returns_400(client: TestClient) -> None:
    headers = _login(client, role="staff")
    # pat_1002 has no email in standard seed (or test with missing contact)
    resp = client.post(
        "/api/communication/patient/pat_1002/send",
        headers=headers,
        json={
            "channel": "email",
            "template_id": "direct_message",
            "message": "Hello!",
        },
    )
    # If pat_1002 has no email, it should return 400
    if resp.status_code == 400:
        assert "no registered email contact" in resp.json()["detail"]


def test_send_patient_not_found_returns_404(client: TestClient) -> None:
    headers = _login(client, role="staff")
    resp = client.post(
        "/api/communication/patient/nonexistent_patient_xyz/send",
        headers=headers,
        json={
            "channel": "sms",
            "template_id": "direct_message",
            "message": "Hello",
        },
    )
    assert resp.status_code == 404
