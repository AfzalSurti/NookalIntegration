"""Injected dependency container for the dashboard — no module-level production globals."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.approval import ApprovalQueue
from app.dashboard.auth import AuthBackend, SessionStore
from app.dashboard.authorization import AuthorizationPolicy
from app.letters import DocumentDelivery, DocumentStore, InMemoryDocumentDelivery, InMemoryDocumentStore
from app.marketing import (
    CampaignRecipientStore,
    CampaignStore,
    ConsentStore,
    FakeEmailAdapter,
    MarketingListStore,
    SuppressionStore,
)
from app.messaging import MessagingService
from app.nookal_client import NookalClient
from app.orchestration.pending_actions import PendingActionStore
from app.orchestration.referrer_sync import ReferrerConflictStore
from app.shared.clock import Clock, SystemClock


AuditFn = Callable[..., Any]


@dataclass
class DashboardContainer:
    """Composition root for dashboard request handling."""

    nookal: NookalClient
    messaging: MessagingService
    approval: ApprovalQueue
    audit: AuditFn
    auth_backend: AuthBackend
    sessions: SessionStore
    policy: AuthorizationPolicy = field(default_factory=AuthorizationPolicy)
    clock: Clock = field(default_factory=SystemClock)
    document_store: DocumentStore = field(default_factory=InMemoryDocumentStore)
    document_delivery: DocumentDelivery = field(default_factory=InMemoryDocumentDelivery)
    conflict_store: ReferrerConflictStore = field(default_factory=ReferrerConflictStore)
    marketing_list_store: MarketingListStore = field(default_factory=MarketingListStore)
    campaign_store: CampaignStore = field(default_factory=CampaignStore)
    campaign_recipient_store: CampaignRecipientStore = field(default_factory=CampaignRecipientStore)
    consent_store: ConsentStore = field(default_factory=ConsentStore)
    suppression_store: SuppressionStore = field(default_factory=SuppressionStore)
    email_adapter: Any = field(default_factory=FakeEmailAdapter)
    pending_actions: PendingActionStore | None = None
    kill_switch_path: Path | None = None
    environment: str = "development"  # development | test | production
    llm_configured: bool = False
    nookal_live_configured: bool = False
    messaging_live_configured: bool = False
    document_intake: Any | None = None
    document_filer: Any | None = None
    finance_categorizer: Any | None = None
    finance_register: Any | None = None
    case_tracking: Any | None = None
    referral_association_store: Any | None = None
    communication_config: Any | None = None  # NookalCommunicationConfig for Section 4.6
    session_cookie_name: str = "bte_session"
    csrf_header_name: str = "X-CSRF-Token"
    template_service: Any | None = None

