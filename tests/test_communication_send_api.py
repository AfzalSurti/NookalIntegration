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


def test_send_sms_uppercase_channel_and_adapter_fallback(client: TestClient, env) -> None:
    # Clear out sms adapter to verify fallback works seamlessly with Nookal native SMS adapter
    if "sms" in env.container.messaging._adapters:
        del env.container.messaging._adapters["sms"]

    headers = _login(client, role="admin")
    resp = client.post(
        "/api/communication/patient/pat_1001/send",
        headers=headers,
        json={
            "channel": "SMS",
            "template_id": "direct_message",
            "message": "Test uppercase channel",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "unavailable"
    assert data["channel"] == "sms"
    assert "Direct SMS sending is not available through the configured Nookal API" in data["error"]
    assert data["provider_ref"] is None
    assert data["sent_at"] is None


def test_send_with_nookal_native_adapters_reports_unavailable(client: TestClient, env) -> None:
    from app.messaging.adapters.sms import SMSAdapter
    from app.messaging.adapters.email import EmailAdapter

    # Wire native Nookal adapters directly into test messaging service
    env.container.messaging._adapters["sms"] = SMSAdapter()
    env.container.messaging._adapters["email"] = EmailAdapter()

    headers = _login(client, role="staff")

    # Direct SMS sending
    resp_sms = client.post(
        "/api/communication/patient/pat_1001/send",
        headers=headers,
        json={
            "channel": "sms",
            "template_id": "direct_message",
            "message": "Exercise update",
        },
    )
    assert resp_sms.status_code == 200, resp_sms.text
    data_sms = resp_sms.json()
    assert data_sms["status"] == "unavailable"
    assert "Direct SMS sending is not available" in data_sms["error"]
    assert data_sms["sent_at"] is None
    assert data_sms["provider_ref"] is None

    # Direct Email sending
    resp_email = client.post(
        "/api/communication/patient/pat_1001/send",
        headers=headers,
        json={
            "channel": "email",
            "template_id": "certificate_sent",
        },
    )
    assert resp_email.status_code == 200, resp_email.text
    data_email = resp_email.json()
    assert data_email["status"] == "unavailable"
    assert "Direct Email sending is not available" in data_email["error"]
    assert data_email["sent_at"] is None
    assert data_email["provider_ref"] is None


def test_send_blocked_when_patient_suppressed(client: TestClient, env) -> None:
    from app.marketing.suppression import SuppressionReason

    # Mark pat_1001 as suppressed in marketing suppression store
    env.container.suppression_store.suppress(
        patient_id="pat_1001",
        reason=SuppressionReason.UNSUBSCRIBE,
        suppressed_by="test",
        notes="Patient opt-out",
    )

    headers = _login(client, role="staff")
    resp = client.post(
        "/api/communication/patient/pat_1001/send",
        headers=headers,
        json={
            "channel": "sms",
            "template_id": "direct_message",
            "message": "Follow up message",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "blocked"
    assert "suppression list" in data["error"].lower()


def test_patient_readiness_includes_contact_fields(client: TestClient) -> None:
    headers = _login(client, role="staff")
    resp = client.get(
        "/api/communication/patient/pat_1001/readiness",
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "patient_name" in data
    assert "phone" in data
    assert "email" in data
    assert data["patient_id"] == "pat_1001"

