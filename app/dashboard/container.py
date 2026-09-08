"""Injected dependency container for the dashboard — no module-level production globals."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from app.approval import ApprovalQueue
from app.dashboard.auth import AuthBackend, SessionStore
from app.dashboard.authorization import AuthorizationPolicy
from app.letters import DocumentDelivery, DocumentStore, InMemoryDocumentDelivery, InMemoryDocumentStore
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
    pending_actions: PendingActionStore | None = None
    kill_switch_path: Path | None = None
    environment: str = "development"  # development | test | production
    llm_configured: bool = False
    nookal_live_configured: bool = False
    messaging_live_configured: bool = False
    session_cookie_name: str = "bte_session"
    csrf_header_name: str = "X-CSRF-Token"
