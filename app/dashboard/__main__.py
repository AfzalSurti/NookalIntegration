"""Local offline dashboard entrypoint (mock Nookal + fake messaging)."""
from __future__ import annotations

from pathlib import Path

from app.approval import ApprovalQueue
from app.dashboard.app import create_app
from app.dashboard.auth import MemoryAuthBackend, SessionStore, User
from app.dashboard.authorization import AuthorizationPolicy
from app.dashboard.container import DashboardContainer
from app.letters import InMemoryDocumentDelivery, InMemoryDocumentStore, register_document_handlers
from app.letters.handlers import DocumentApprovalHandler
from app.messaging import MessagingService, SentLog, TemplateStore
from app.messaging.adapters.fake import FakeAdapter
from app.nookal_client.seed import seeded_mock_client
from app.orchestration.pending_actions import PendingActionStore
from app.orchestration.referrer_sync import ReferrerConflictStore
from app.shared.audit import AuditLog
from app.shared.clock import SystemClock
from app.shared.config import get_settings
from app.shared.exceptions import repo_root


def build_offline_container() -> DashboardContainer:
    """
    Composition root for local dashboard runs.

    Users must be supplied via environment (not hardcoded here):
      DASHBOARD_DEV_USER / DASHBOARD_DEV_PASSWORD / DASHBOARD_DEV_ROLE
    If unset, creates a single ephemeral admin from random-looking placeholders
    only when AUTOMATION_ALLOW_DEV_LOGIN=1.
    """
    import os
    import secrets

    settings = get_settings()
    clock = SystemClock()
    home = Path(os.environ.get("AUTOMATION_HOME") or repo_root())
    audit = AuditLog(directory=settings.paths.audit_dir, clock=clock)
    nookal = seeded_mock_client(audit=audit)
    messaging = MessagingService(
        adapters={
            "whatsapp": FakeAdapter("whatsapp"),
            "sms": FakeAdapter("sms"),
            "email": FakeAdapter("email"),
        },
        templates=TemplateStore(),
        sent_log=SentLog(directory=settings.paths.sent_log_dir, clock=clock),
        audit=audit,
        clock=clock,
    )
    approval = ApprovalQueue(
        store_path=settings.approval.store_path,
        audit=audit,
        clock=clock,
    )
    store = InMemoryDocumentStore()
    delivery = InMemoryDocumentDelivery()
    register_document_handlers(
        approval,
        handler=DocumentApprovalHandler(
            store=store, delivery=delivery, clock=clock, audit=audit
        ),
    )

    users: list[tuple[User, str]] = []
    username = os.environ.get("DASHBOARD_DEV_USER", "").strip()
    password = os.environ.get("DASHBOARD_DEV_PASSWORD", "").strip()
    role = os.environ.get("DASHBOARD_DEV_ROLE", "admin").strip() or "admin"
    if username and password:
        users.append(
            (
                User(user_id=f"dev_{username}", username=username, role=role, display_name=username),
                password,
            )
        )
    elif os.environ.get("AUTOMATION_ALLOW_DEV_LOGIN") == "1":
        # Ephemeral local-only credentials — printed once; never committed.
        generated = secrets.token_urlsafe(12)
        print(f"[dashboard] ephemeral admin password: {generated}")
        users.append(
            (
                User(user_id="dev_admin", username="admin", role="admin", display_name="Dev Admin"),
                generated,
            )
        )
    else:
        raise SystemExit(
            "Set DASHBOARD_DEV_USER and DASHBOARD_DEV_PASSWORD, "
            "or AUTOMATION_ALLOW_DEV_LOGIN=1 for an ephemeral local password."
        )

    return DashboardContainer(
        nookal=nookal,
        messaging=messaging,
        approval=approval,
        audit=audit.log_event,
        auth_backend=MemoryAuthBackend(users),
        sessions=SessionStore(clock=clock),
        policy=AuthorizationPolicy(),
        clock=clock,
        document_store=store,
        document_delivery=delivery,
        conflict_store=ReferrerConflictStore(),
        pending_actions=PendingActionStore(clock=clock),
        kill_switch_path=settings.paths.kill_switch_file,
        environment=os.environ.get("BTE_ENV", "development"),
        llm_configured=bool(os.environ.get("LLM_API_KEY") or os.environ.get("LLM_BASE_URL")),
        nookal_live_configured=bool(os.environ.get("NOOKAL_API_KEY")),
        messaging_live_configured=bool(os.environ.get("WHATSAPP_API_TOKEN")),
    )


def main() -> None:
    import uvicorn

    container = build_offline_container()
    app = create_app(container)
    host = os.environ.get("DASHBOARD_HOST", "127.0.0.1")
    port = int(os.environ.get("DASHBOARD_PORT", "8080"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
