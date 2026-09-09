"""
Production composition root for the clinic dashboard.

Constructs real production dependencies via dependency injection:
- HttpNookalClient targeting official Nookal API v2
- Real WhatsAppAdapter (if provisioned) or safe absence without FakeAdapter
- FileSystemDocumentStore and FileSystemDocumentDelivery
- UnavailableEmailAdapter (preventing silent fake campaign sends)
- ProductionAuthBackend (PBKDF2-hashed credentials; dev logins strictly forbidden)
- Enforces environment="production"
"""
from __future__ import annotations

import os
from pathlib import Path

from app.approval import ApprovalQueue
from app.dashboard.auth import AuthBackend, ProductionAuthBackend, SessionStore
from app.dashboard.authorization import AuthorizationPolicy
from app.dashboard.container import DashboardContainer
from app.letters import (
    DocumentApprovalHandler,
    FileSystemDocumentDelivery,
    FileSystemDocumentStore,
    register_document_handlers,
)
from app.marketing import (
    CampaignRecipientStore,
    CampaignStore,
    ConsentStore,
    MarketingListStore,
    SuppressionStore,
    UnavailableEmailAdapter,
)
from app.messaging import MessagingService, SentLog, TemplateStore
from app.messaging.adapters.whatsapp import WhatsAppAdapter
from app.nookal_client import HttpNookalClient
from app.orchestration.pending_actions import PendingActionStore
from app.orchestration.referrer_sync import ReferrerConflictStore
from app.shared.audit import AuditLog
from app.shared.clock import SystemClock
from app.shared.config import Settings, get_settings
from app.shared.exceptions import ConfigError


def validate_production_config(settings: Settings, *, skip_auth: bool = False) -> None:
    """
    Validate required production settings before startup.
    Fails fast with ConfigError rather than starting a partially configured service.
    """
    # 1. Nookal Configuration
    if not settings.nookal.base_url:
        raise ConfigError("Production requires nookal.base_url to be configured.")
    if not settings.nookal.api_key:
        raise ConfigError("Production requires NOOKAL_API_KEY to be configured in config/.env.")

    # 2. LLM Configuration
    if not settings.llm.base_url:
        raise ConfigError("Production requires llm.base_url to be configured.")
    if not settings.llm.model:
        raise ConfigError("Production requires llm.model to be configured.")

    # 3. Production Authentication (unless overridden for tests)
    if not skip_auth:
        # ProductionAuthBackend.from_env() checks for valid prod credentials and rejects dev logins
        ProductionAuthBackend.from_env()

    # 4. Storage Directories
    settings.paths.audit_dir.mkdir(parents=True, exist_ok=True)
    settings.paths.sent_log_dir.mkdir(parents=True, exist_ok=True)
    settings.paths.working_dir.mkdir(parents=True, exist_ok=True)
    settings.paths.app_log_dir.mkdir(parents=True, exist_ok=True)
    (settings.paths.working_dir / "documents").mkdir(parents=True, exist_ok=True)
    if settings.approval.store_path.parent:
        settings.approval.store_path.parent.mkdir(parents=True, exist_ok=True)

    # 5. Kill Switch
    if not settings.paths.kill_switch_file:
        raise ConfigError("Production requires paths.kill_switch_file to be configured.")


def build_production_container(
    settings: Settings | None = None,
    *,
    auth_backend: AuthBackend | None = None,
) -> DashboardContainer:
    """
    Assemble the production dependency graph.
    All components use real/persistent implementations — no mocks or fake adapters.
    """
    cfg = settings or get_settings()
    validate_production_config(cfg, skip_auth=auth_backend is not None)

    clock = SystemClock()
    audit = AuditLog(directory=cfg.paths.audit_dir, clock=clock)

    # Live Nookal client
    nookal = HttpNookalClient(config=cfg.nookal, audit=audit)

    # Outbound messaging: use real WhatsAppAdapter if credentials exist, otherwise empty
    # Never inject FakeAdapter in production
    messaging_adapters = {}
    whatsapp_configured = bool(
        cfg.messaging.whatsapp_token and cfg.messaging.whatsapp_phone_number_id
    )
    if whatsapp_configured:
        messaging_adapters["whatsapp"] = WhatsAppAdapter(cfg.messaging)

    messaging = MessagingService(
        adapters=messaging_adapters,
        templates=TemplateStore(),
        sent_log=SentLog(directory=cfg.paths.sent_log_dir, clock=clock),
        audit=audit,
        clock=clock,
    )

    # Approval queue
    approval = ApprovalQueue(
        store_path=cfg.approval.store_path,
        audit=audit,
        clock=clock,
    )

    # Persistent document store and delivery
    doc_store = FileSystemDocumentStore(directory=cfg.paths.working_dir / "documents")
    doc_delivery = FileSystemDocumentDelivery(directory=cfg.paths.working_dir)
    register_document_handlers(
        approval,
        handler=DocumentApprovalHandler(
            store=doc_store,
            delivery=doc_delivery,
            clock=clock,
            audit=audit,
        ),
    )

    # Production authentication
    auth = auth_backend or ProductionAuthBackend.from_env()
    sessions = SessionStore(clock=clock, ttl_seconds=8 * 3600)

    # Production email adapter: raises NotImplementedError if send attempted
    email_adapter = UnavailableEmailAdapter()

    return DashboardContainer(
        nookal=nookal,
        messaging=messaging,
        approval=approval,
        audit=audit,
        auth_backend=auth,
        sessions=sessions,
        policy=AuthorizationPolicy(),
        clock=clock,
        document_store=doc_store,
        document_delivery=doc_delivery,
        conflict_store=ReferrerConflictStore(),
        marketing_list_store=MarketingListStore(),
        campaign_store=CampaignStore(),
        campaign_recipient_store=CampaignRecipientStore(),
        consent_store=ConsentStore(),
        suppression_store=SuppressionStore(),
        email_adapter=email_adapter,
        pending_actions=PendingActionStore(clock=clock),
        kill_switch_path=cfg.paths.kill_switch_file,
        environment="production",
        llm_configured=bool(cfg.llm.base_url and cfg.llm.model),
        nookal_live_configured=bool(cfg.nookal.api_key),
        messaging_live_configured=whatsapp_configured,
    )
