"""Offline dashboard composition for tests and local mock mode."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.approval import ApprovalQueue
from app.dashboard.app import create_app
from app.dashboard.auth import MemoryAuthBackend, SessionStore, User
from app.dashboard.authorization import AuthorizationPolicy
from app.dashboard.container import DashboardContainer
from app.letters import InMemoryDocumentDelivery, InMemoryDocumentStore, register_document_handlers
from app.letters.handlers import DocumentApprovalHandler
from app.messaging import MessagingService, SentLog, TemplateStore
from app.messaging.adapters.fake import FakeAdapter
from app.nookal_client import MockNookalClient
from app.nookal_client.seed import seeded_mock_client
from app.orchestration.pending_actions import PendingActionStore
from app.orchestration.referrer_sync import ReferrerConflictStore
from app.shared import kill_switch as kill_switch_mod
from app.shared.audit import AuditLog
from app.shared.clock import FrozenClock
from tests.helpers import CapturingAudit, SEED_NOW


# Test credentials — injected into MemoryAuthBackend, not hardcoded in app source.
TEST_USERS: dict[str, tuple[User, str]] = {
    "staff": (
        User(user_id="u_staff", username="staff", role="staff", display_name="Staff User"),
        "staff-pass-test",
    ),
    "practitioner": (
        User(
            user_id="u_prac",
            username="practitioner",
            role="practitioner",
            display_name="Practitioner User",
        ),
        "prac-pass-test",
    ),
    "admin": (
        User(user_id="u_admin", username="admin", role="admin", display_name="Admin User"),
        "admin-pass-test",
    ),
    "owner": (
        User(user_id="u_owner", username="owner", role="owner", display_name="Owner User"),
        "owner-pass-test",
    ),
    "other": (
        User(user_id="u_other", username="other", role="other", display_name="Other User"),
        "other",
    ),
}


@dataclass
class DashboardTestEnv:
    app: FastAPI
    container: DashboardContainer
    audit: CapturingAudit
    nookal: MockNookalClient
    messaging: MessagingService
    fake_whatsapp: FakeAdapter
    document_store: InMemoryDocumentStore
    document_delivery: InMemoryDocumentDelivery
    conflict_store: ReferrerConflictStore
    kill_switch_path: Path
    clock: FrozenClock

    def client(self) -> TestClient:
        return TestClient(self.app)

    def login(self, client: TestClient, role: str = "admin") -> str:
        """Log in as a test user; return CSRF token."""
        user, password = TEST_USERS[role]
        resp = client.post("/api/auth/login", json={"username": user.username, "password": password})
        assert resp.status_code == 200, resp.text
        return resp.json()["csrf_token"]

    def authed(
        self,
        client: TestClient,
        method: str,
        url: str,
        *,
        role: str = "admin",
        json: dict | None = None,
        params: dict | None = None,
    ):
        csrf = self.login(client, role)
        headers = {"X-CSRF-Token": csrf}
        return client.request(method, url, json=json, params=params, headers=headers)


def build_dashboard_env(
    tmp_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> DashboardTestEnv:
    clock = FrozenClock(SEED_NOW)
    kill_flag = tmp_path / "KILL_SWITCH"
    if monkeypatch is not None:
        monkeypatch.setattr(kill_switch_mod, "switch_path", lambda path=None: kill_flag)

    audit_log = AuditLog(directory=tmp_path / "audit", clock=clock)
    capturing = CapturingAudit(log=audit_log)
    nookal = seeded_mock_client(audit=capturing)

    from app.dashboard.services.templates import CampaignTemplateStore, TemplateManagementService
    from app.letters.templates import templates_root
    from app.shared.exceptions import repo_root

    isolated_msg_dir = tmp_path / "messaging_templates"
    src_msg_dir = repo_root() / "app" / "messaging" / "templates"
    if src_msg_dir.exists():
        shutil.copytree(src_msg_dir, isolated_msg_dir)
    else:
        isolated_msg_dir.mkdir(parents=True, exist_ok=True)

    isolated_letters_dir = tmp_path / "letters_templates"
    src_letters_dir = templates_root()
    if src_letters_dir.exists():
        shutil.copytree(src_letters_dir, isolated_letters_dir)
    else:
        isolated_letters_dir.mkdir(parents=True, exist_ok=True)

    isolated_mkt_store = CampaignTemplateStore(store_path=tmp_path / "campaign_templates.jsonl")

    template_svc = TemplateManagementService(
        messaging_dir=isolated_msg_dir,
        letters_dir=isolated_letters_dir,
        marketing_store=isolated_mkt_store,
    )

    fake = FakeAdapter("whatsapp")
    messaging = MessagingService(
        adapters={
            "whatsapp": fake,
            "sms": FakeAdapter("sms"),
            "email": FakeAdapter("email"),
        },
        templates=TemplateStore(directory=isolated_msg_dir),
        sent_log=SentLog(directory=tmp_path / "sent", clock=clock),
        audit=capturing,
        clock=clock,
    )

    approval = ApprovalQueue(
        store_path=tmp_path / "tasks.jsonl",
        audit=capturing,
        clock=clock,
    )
    doc_store = InMemoryDocumentStore()
    doc_delivery = InMemoryDocumentDelivery()
    handler = DocumentApprovalHandler(
        store=doc_store,
        delivery=doc_delivery,
        clock=clock,
        audit=capturing,
        deliver=True,
    )
    register_document_handlers(approval, handler=handler)

    auth_backend = MemoryAuthBackend([pair for pair in TEST_USERS.values()])
    sessions = SessionStore(clock=clock, ttl_seconds=3600)
    conflict_store = ReferrerConflictStore()

    from app.orchestration.referral_sync_service import ReferralAssociationStore
    referral_store = ReferralAssociationStore(tmp_path / "referral_associations.jsonl")

    from app.marketing.store import SuppressionStore
    suppression_store = SuppressionStore(tmp_path / "suppression.jsonl")

    container = DashboardContainer(
        nookal=nookal,
        messaging=messaging,
        approval=approval,
        audit=capturing,
        auth_backend=auth_backend,
        sessions=sessions,
        policy=AuthorizationPolicy(),
        clock=clock,
        document_store=doc_store,
        document_delivery=doc_delivery,
        conflict_store=conflict_store,
        referral_association_store=referral_store,
        suppression_store=suppression_store,
        pending_actions=PendingActionStore(clock=clock, ttl_minutes=30),
        kill_switch_path=kill_flag,
        environment="test",
        llm_configured=False,
        nookal_live_configured=False,
        messaging_live_configured=False,
        template_service=template_svc,
    )

    app = create_app(container)
    return DashboardTestEnv(
        app=app,
        container=container,
        audit=capturing,
        nookal=nookal,
        messaging=messaging,
        fake_whatsapp=fake,
        document_store=doc_store,
        document_delivery=doc_delivery,
        conflict_store=conflict_store,
        kill_switch_path=kill_flag,
        clock=clock,
    )
