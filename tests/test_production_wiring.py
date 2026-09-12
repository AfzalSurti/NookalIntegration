"""
Tests for production runtime wiring and mock isolation.

Verifies:
- Production container constructs with real production dependencies
- Strict isolation: zero MockNookalClient, FakeAdapter, InMemoryDocumentStore,
  InMemoryDocumentDelivery, or FakeEmailAdapter in the production graph
- Production environment is 'production'
- Rejection of development-only credentials (DASHBOARD_DEV_*, AUTOMATION_ALLOW_DEV_LOGIN)
- Fail-fast configuration validation
- Healthz endpoint returns 200 OK on production app
- Unconfigured messaging fails safely without fake fallbacks
"""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
import json
import pytest
from fastapi.testclient import TestClient

from app.dashboard.app import create_app
from app.dashboard.auth import ProductionAuthBackend, MemoryAuthBackend, User
from app.dashboard.production import build_production_container, validate_production_config
from app.letters import (
    DocumentRecord,
    DocumentStatus,
    DocumentType,
    FileSystemDocumentDelivery,
    FileSystemDocumentStore,
    InMemoryDocumentDelivery,
    InMemoryDocumentStore,
)
from app.marketing import FakeEmailAdapter, UnavailableEmailAdapter
from app.messaging.adapters.fake import FakeAdapter
from app.messaging.adapters.stub import StubAdapter
from app.messaging.adapters.sms import SMSAdapter
from app.messaging.adapters.email import EmailAdapter
from app.messaging.adapters.whatsapp import WhatsAppAdapter
from app.nookal_client import HttpNookalClient, MockNookalClient
from app.shared.config import (
    ApprovalConfig,
    AuditConfig,
    LLMConfig,
    MessagingConfig,
    NookalConfig,
    PathsConfig,
    Settings,
)
from app.shared.exceptions import ConfigError, MessagingError


def _make_prod_settings(tmp_path: Path, *, nookal_key: str = "prod-nookal-key-123") -> Settings:
    return Settings(
        paths=PathsConfig(
            audit_dir=tmp_path / "logs" / "audit",
            app_log_dir=tmp_path / "logs" / "app",
            kill_switch_file=tmp_path / "config" / "KILL_SWITCH",
            working_dir=tmp_path / "data" / "working",
            sent_log_dir=tmp_path / "data" / "working" / "sent",
        ),
        nookal=NookalConfig(
            base_url="https://api.nookal.com/production/v2/",
            api_key=nookal_key,
            timeout_seconds=30.0,
            requests_per_second=2.0,
            max_retries=3,
        ),
        llm=LLMConfig(
            base_url="http://127.0.0.1:11434",
            model="qwen3.5:9b",
            api_key="",
            temperature=0.2,
            intent_confidence_threshold="high",
            prompts_dir=tmp_path / "prompts",
        ),
        messaging=MessagingConfig(
            channels={"whatsapp": False, "sms": False, "email": False},
            max_retries=3,
            whatsapp_base_url="https://graph.facebook.com/v18.0",
            whatsapp_token="",
            whatsapp_phone_number_id="",
            sms_api_key="",
            sms_sender_id="",
            sms_base_url="",
            smtp_host="",
            smtp_port=587,
            smtp_user="",
            smtp_password="",
            smtp_from="",
        ),
        approval=ApprovalConfig(
            store_path=tmp_path / "data" / "working" / "tasks.jsonl",
        ),
        audit=AuditConfig(
            max_metadata_value_length=200,
            forbidden_metadata_keys=frozenset(),
        ),
        raw={},
    )


def test_build_production_container_success(tmp_path: Path) -> None:
    settings = _make_prod_settings(tmp_path)
    auth = ProductionAuthBackend(
        users=[(User(user_id="u1", username="clinic_admin", role="admin"), "SecurePass123!")]
    )

    container = build_production_container(settings, auth_backend=auth)

    # 1. Environment flag
    assert container.environment == "production"

    # 2. Real Nookal client
    assert isinstance(container.nookal, HttpNookalClient)
    assert not isinstance(container.nookal, MockNookalClient)

    # 3. Persistent document store & delivery
    assert isinstance(container.document_store, FileSystemDocumentStore)
    assert not isinstance(container.document_store, InMemoryDocumentStore)
    assert isinstance(container.document_delivery, FileSystemDocumentDelivery)
    assert not isinstance(container.document_delivery, InMemoryDocumentDelivery)

    # 4. Production email adapter
    assert isinstance(container.email_adapter, UnavailableEmailAdapter)
    assert not isinstance(container.email_adapter, FakeEmailAdapter)

    # 5. Production auth backend
    assert isinstance(container.auth_backend, ProductionAuthBackend)
    assert not isinstance(container.auth_backend, MemoryAuthBackend)

    # 6. Messaging has NO fake or stub adapters and uses Nookal SMS/Email adapters
    assert isinstance(container.messaging._adapters["sms"], SMSAdapter)
    assert isinstance(container.messaging._adapters["email"], EmailAdapter)
    for channel, adapter in container.messaging._adapters.items():
        assert not isinstance(adapter, FakeAdapter)
        assert not isinstance(adapter, StubAdapter)


def test_strict_mock_isolation_in_production(tmp_path: Path) -> None:
    settings = _make_prod_settings(tmp_path)
    auth = ProductionAuthBackend(
        users=[(User(user_id="u1", username="admin", role="admin"), "Pass123")]
    )
    container = build_production_container(settings, auth_backend=auth)

    # Assert no mock types anywhere in the container attributes
    for attr, val in container.__dict__.items():
        assert not isinstance(val, MockNookalClient), f"{attr} is MockNookalClient"
        assert not isinstance(val, FakeAdapter), f"{attr} is FakeAdapter"
        assert not isinstance(val, InMemoryDocumentStore), f"{attr} is InMemoryDocumentStore"
        assert not isinstance(val, InMemoryDocumentDelivery), f"{attr} is InMemoryDocumentDelivery"
        assert not isinstance(val, FakeEmailAdapter), f"{attr} is FakeEmailAdapter"


def test_production_validation_missing_nookal_key(tmp_path: Path) -> None:
    settings = _make_prod_settings(tmp_path, nookal_key="")
    auth = ProductionAuthBackend(
        users=[(User(user_id="u1", username="admin", role="admin"), "Pass123")]
    )

    with pytest.raises(ConfigError) as exc_info:
        build_production_container(settings, auth_backend=auth)
    assert "NOOKAL_API_KEY" in str(exc_info.value)


def test_production_validation_missing_llm_model(tmp_path: Path) -> None:
    import dataclasses
    settings = _make_prod_settings(tmp_path)
    settings = dataclasses.replace(settings, llm=dataclasses.replace(settings.llm, model=""))
    auth = ProductionAuthBackend(
        users=[(User(user_id="u1", username="admin", role="admin"), "Pass123")]
    )

    with pytest.raises(ConfigError) as exc_info:
        build_production_container(settings, auth_backend=auth)
    assert "llm.model" in str(exc_info.value)


def test_production_rejects_dev_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _make_prod_settings(tmp_path)

    # Set development credentials
    monkeypatch.setenv("DASHBOARD_DEV_USER", "devuser")
    monkeypatch.setenv("DASHBOARD_DEV_PASSWORD", "devpass")
    monkeypatch.setenv("AUTOMATION_ALLOW_DEV_LOGIN", "1")

    # Clear production credentials
    monkeypatch.delenv("DASHBOARD_PROD_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_PROD_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_ADMIN_USER", raising=False)
    monkeypatch.delenv("DASHBOARD_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("DASHBOARD_ADMIN_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("DASHBOARD_USERS_FILE", raising=False)

    # Validation must fail fast and reject dev login
    with pytest.raises(ConfigError) as exc_info:
        validate_production_config(settings)
    assert "AUTOMATION_ALLOW_DEV_LOGIN" in str(exc_info.value) or "No production dashboard users" in str(exc_info.value)


def test_production_auth_loads_from_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOMATION_ALLOW_DEV_LOGIN", raising=False)
    monkeypatch.setenv("DASHBOARD_PROD_USER", "dr_smith")
    monkeypatch.setenv("DASHBOARD_PROD_PASSWORD", "SuperClinic2026!")

    backend = ProductionAuthBackend.from_env()
    user = backend.authenticate("dr_smith", "SuperClinic2026!")
    assert user is not None
    assert user.username == "dr_smith"
    assert user.role == "admin"

    # Wrong password fails
    assert backend.authenticate("dr_smith", "wrongpass") is None


def test_production_auth_loads_from_users_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUTOMATION_ALLOW_DEV_LOGIN", raising=False)
    users_file = tmp_path / "prod_users.json"
    users_file.write_text(
        json.dumps(
            [
                {"user_id": "u10", "username": "reception", "role": "staff", "password": "StaffSecret99!"}
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DASHBOARD_USERS_FILE", str(users_file))

    backend = ProductionAuthBackend.from_env()
    user = backend.authenticate("reception", "StaffSecret99!")
    assert user is not None
    assert user.role == "staff"


def test_production_healthz_endpoint(tmp_path: Path) -> None:
    settings = _make_prod_settings(tmp_path)
    auth = ProductionAuthBackend(
        users=[(User(user_id="u1", username="admin", role="admin"), "Pass123")]
    )
    container = build_production_container(settings, auth_backend=auth)
    app = create_app(container)

    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    # Security headers check
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    assert response.headers.get("X-Frame-Options") == "DENY"


def test_production_messaging_unconfigured_fails_safely(tmp_path: Path) -> None:
    settings = _make_prod_settings(tmp_path)
    # whatsapp_token and whatsapp_phone_number_id are empty in _make_prod_settings

    auth = ProductionAuthBackend(
        users=[(User(user_id="u1", username="admin", role="admin"), "Pass123")]
    )
    container = build_production_container(settings, auth_backend=auth)

    assert container.messaging_live_configured is False

    # Attempting to send raises MessagingError because no fake adapter exists
    with pytest.raises(MessagingError) as exc_info:
        container.messaging.send(
            channel="whatsapp",
            patient_id="p1",
            patient_contact="+61400000000",
            template_id="appointment_reminder",
            context={"name": "Arthur", "date": "10 Sep", "time": "10:00"},
            idempotency_key="idemp_1",
            caller_role="admin",
        )
    assert "no adapter for channel=whatsapp" in str(exc_info.value)


def test_production_sms_and_email_return_unavailable(tmp_path: Path) -> None:
    settings = _make_prod_settings(tmp_path)
    auth = ProductionAuthBackend(
        users=[(User(user_id="u1", username="admin", role="admin"), "Pass123")]
    )
    container = build_production_container(settings, auth_backend=auth)

    # Sending SMS in production must report unavailable, never 'sent'
    out_sms = container.messaging.send(
        channel="sms",
        patient_id="p1",
        patient_contact="+61400000000",
        template_id="appointment_reminder",
        context={"name": "Arthur", "date": "10 Sep", "time": "10:00"},
        idempotency_key="idemp_sms_1",
        caller_role="admin",
    )
    assert out_sms.status == "unavailable"
    assert out_sms.provider_ref is None
    assert out_sms.sent_at is None
    assert "Direct SMS sending is not available" in (out_sms.error or "")

    # Sending Email in production must report unavailable, never 'sent'
    out_email = container.messaging.send(
        channel="email",
        patient_id="p1",
        patient_contact="arthur@example.com",
        template_id="certificate_sent",
        context={"name": "Arthur"},
        idempotency_key="idemp_email_1",
        caller_role="admin",
    )
    assert out_email.status == "unavailable"
    assert out_email.provider_ref is None
    assert out_email.sent_at is None
    assert "Direct Email sending is not available" in (out_email.error or "")


def test_production_filesystem_document_store_and_delivery(tmp_path: Path) -> None:
    doc_dir = tmp_path / "documents"
    store = FileSystemDocumentStore(directory=doc_dir)
    delivery = FileSystemDocumentDelivery(directory=tmp_path)

    rec = DocumentRecord(
        document_id="doc_101",
        document_type=DocumentType.CERTIFICATE,
        patient_id="p_9",
        task_id="t_1",
        template_id="certificate",
        status=DocumentStatus.RENDERED,
        created_at=datetime(2026, 9, 10, 12, 0).isoformat(),
        source_facts={"practitioner_name": "Dr. Smith"},
    )
    saved = store.save(rec, content=b"%PDF-1.4 test certificate content")
    assert saved.output_ref is not None
    assert (doc_dir / "doc_101.meta.json").exists()
    assert (doc_dir / "doc_101.bin").exists()

    retrieved = store.retrieve("doc_101")
    assert retrieved is not None
    assert retrieved.patient_id == "p_9"
    assert store.get_content("doc_101") == b"%PDF-1.4 test certificate content"

    # Delivery log
    deliv_rec = delivery.deliver(retrieved, destination="patient_email", channel="email")
    assert deliv_rec.document_id == "doc_101"
    deliveries_file = tmp_path / "deliveries.jsonl"
    assert deliveries_file.exists()
    lines = deliveries_file.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    logged_data = json.loads(lines[0])
    assert logged_data["document_id"] == "doc_101"
    assert logged_data["channel"] == "email"


def test_unavailable_email_adapter_raises_in_production() -> None:
    adapter = UnavailableEmailAdapter()
    with pytest.raises(NotImplementedError) as exc_info:
        adapter.send(to="test@example.com", body="newsletter")
    assert "production email provider is not configured" in str(exc_info.value)


def test_production_appointment_service_with_http_nookal_client(tmp_path: Path) -> None:
    """
    Ensure AppointmentService.list_upcoming works with HttpNookalClient without TypeError.
    """
    import httpx
    from app.dashboard.services.appointments import AppointmentService
    from app.shared.clock import SystemClock
    from app.orchestration.pending_actions import PendingActionStore
    from app.shared.audit import AuditLog

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
                                "ID": "appt_prod_1",
                                "patientID": "pat_1",
                                "date": "2026-09-10",
                                "startTime": "10:00:00",
                                "endTime": "10:30:00",
                                "status": "booked",
                            }
                        ]
                    },
                },
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="https://api.nookal.com/production/v2/")
    config = NookalConfig(
        base_url="https://api.nookal.com/production/v2/",
        api_key="prod-key-123",
        requests_per_second=2.0,
        max_retries=3,
        timeout_seconds=10.0,
    )
    nookal = HttpNookalClient(config=config, client=client)
    audit = AuditLog(directory=tmp_path / "audit").log_event

    svc = AppointmentService(
        nookal=nookal,
        messaging=None,  # type: ignore[arg-type]
        approval=None,   # type: ignore[arg-type]
        audit=audit,
        pending_actions=PendingActionStore(),
        clock=SystemClock(),
    )

    upcoming = svc.list_upcoming(
        actor="u_admin",
        role="admin",
        correlation_id="corr-overview-test",
    )
    assert len(upcoming) == 1
    assert upcoming[0].appointment_id == "appt_prod_1"
    assert upcoming[0].patient_id == "pat_1"

