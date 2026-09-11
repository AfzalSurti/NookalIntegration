"""FastAPI dependencies — auth, CSRF, permission enforcement, service factories."""
from __future__ import annotations

from typing import Annotated, Callable

from fastapi import Cookie, Depends, Header, HTTPException, Request, status

from app.dashboard.auth import Session, User
from app.dashboard.authorization import AuthorizationError, AuthorizationPolicy, Permission
from app.dashboard.container import DashboardContainer
from app.dashboard.services import (
    ApprovalService,
    AppointmentService,
    AuditViewerService,
    CaseService,
    InvoiceService,
    MarketingService,
    PatientFileService,
    PatientService,
    ReferrerConflictService,
    SystemService,
    TreatmentNoteService,
    ReviewService,
)
from app.orchestration.correlation import new_correlation_id
from app.orchestration.pending_actions import PendingActionStore
from app.shared.audit import AuditLog


def get_container(request: Request) -> DashboardContainer:
    container = getattr(request.app.state, "container", None)
    if container is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="container_missing")
    return container


def get_correlation_id(
    x_correlation_id: Annotated[str | None, Header(alias="X-Correlation-ID")] = None,
) -> str:
    return x_correlation_id or new_correlation_id()


def get_session(
    request: Request,
    container: Annotated[DashboardContainer, Depends(get_container)],
    session_cookie: Annotated[str | None, Cookie(alias="bte_session")] = None,
) -> Session | None:
    cookie_name = container.session_cookie_name
    raw = session_cookie
    if raw is None:
        raw = request.cookies.get(cookie_name)
    return container.sessions.get(raw)


def require_session(
    session: Annotated[Session | None, Depends(get_session)],
) -> Session:
    if session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="authentication_required")
    return session


def require_user(
    session: Annotated[Session, Depends(require_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
) -> User:
    user = container.auth_backend.get_user(session.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="authentication_required")
    return user


def require_csrf(
    request: Request,
    session: Annotated[Session, Depends(require_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> None:
    """Enforce CSRF on unsafe methods when using cookie sessions."""
    if request.method.upper() in {"GET", "HEAD", "OPTIONS"}:
        return
    token = x_csrf_token or request.headers.get(container.csrf_header_name)
    if not token or token != session.csrf_token:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="csrf_failed")


def require_permission(permission: Permission) -> Callable[..., User]:
    def _dep(
        user: Annotated[User, Depends(require_user)],
        container: Annotated[DashboardContainer, Depends(get_container)],
        request: Request,
        session: Annotated[Session, Depends(require_session)],
    ) -> User:
        # CSRF on unsafe methods only (cookie session).
        if request.method.upper() not in {"GET", "HEAD", "OPTIONS"}:
            token = request.headers.get(container.csrf_header_name) or request.headers.get(
                "X-CSRF-Token"
            )
            if not token or token != session.csrf_token:
                raise HTTPException(status.HTTP_403_FORBIDDEN, detail="csrf_failed")
        policy: AuthorizationPolicy = container.policy
        try:
            policy.require(user.role, permission)
        except AuthorizationError:
            raise HTTPException(status.HTTP_403_FORBIDDEN, detail="forbidden") from None
        return user

    return _dep


# --- service factories ---

def patient_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> PatientService:
    return PatientService(
        nookal=container.nookal,
        audit=container.audit,
        clock=container.clock,
        document_store=container.document_store,
    )


def approval_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> ApprovalService:
    return ApprovalService(approval=container.approval, audit=container.audit)


def referrer_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> ReferrerConflictService:
    return ReferrerConflictService(
        nookal=container.nookal,
        conflict_store=container.conflict_store,
        audit=container.audit,
    )


def appointment_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> AppointmentService:
    pending = container.pending_actions or PendingActionStore(clock=container.clock)
    return AppointmentService(
        nookal=container.nookal,
        messaging=container.messaging,
        approval=container.approval,
        audit=container.audit,
        pending_actions=pending,
        clock=container.clock,
    )


def audit_viewer_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> AuditViewerService:
    # container.audit may be a CapturingAudit, a bound method (audit.log_event), or an AuditLog directly.
    audit_fn = container.audit
    log = getattr(audit_fn, "log", None)
    if not isinstance(log, AuditLog):
        log = getattr(audit_fn, "__self__", None)
    if not isinstance(log, AuditLog):
        log = audit_fn if isinstance(audit_fn, AuditLog) else None
    if log is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, detail="audit_log_unavailable")
    return AuditViewerService(audit_log=log, audit=audit_fn)


def system_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> SystemService:
    return SystemService(
        approval=container.approval,
        audit=container.audit,
        kill_switch_path=container.kill_switch_path,
        environment=container.environment,
        llm_configured=container.llm_configured,
        nookal_live_configured=container.nookal_live_configured,
        messaging_live_configured=container.messaging_live_configured,
    )


def marketing_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> MarketingService:
    return MarketingService(
        nookal=container.nookal,
        list_store=container.marketing_list_store,
        campaign_store=container.campaign_store,
        recipient_store=container.campaign_recipient_store,
        consent_store=container.consent_store,
        suppression_store=container.suppression_store,
        email_adapter=container.email_adapter,
        audit=container.audit,
    )


def invoice_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> InvoiceService:
    return InvoiceService(
        nookal=container.nookal,
        audit=container.audit,
        clock=container.clock,
    )


def review_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> ReviewService:
    service = getattr(container, "review_service", None)
    if service is None:
        service = ReviewService(container=container, audit=container.audit)
        container.review_service = service
    return service


def case_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> CaseService:
    return CaseService(
        nookal=container.nookal,
        audit=container.audit,
        clock=container.clock,
    )


def patient_file_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> PatientFileService:
    return PatientFileService(
        nookal=container.nookal,
        audit=container.audit,
        clock=container.clock,
    )


def treatment_note_service(container: Annotated[DashboardContainer, Depends(get_container)]) -> TreatmentNoteService:
    return TreatmentNoteService(
        nookal=container.nookal,
        audit=container.audit,
        clock=container.clock,
    )


def referral_sync_service(container: Annotated[DashboardContainer, Depends(get_container)]):
    from app.orchestration.referral_sync_service import ReferralAssociationStore, ReferralSyncService
    store = getattr(container, "referral_association_store", None)
    if store is None:
        store = ReferralAssociationStore()
        container.referral_association_store = store
    return ReferralSyncService(
        nookal=container.nookal,
        association_store=store,
        conflict_store=container.conflict_store,
        audit=container.audit,
        clock=container.clock,
    )


def communication_service(
    container: Annotated[DashboardContainer, Depends(get_container)],
) -> "CommunicationDashboardService":
    from app.dashboard.services.communication import CommunicationDashboardService
    config = getattr(container, "communication_config", None)
    return CommunicationDashboardService(
        nookal=container.nookal,
        audit=container.audit,
        config=config,
    )


def messaging_service(
    container: Annotated[DashboardContainer, Depends(get_container)],
) -> Any:
    return container.messaging


