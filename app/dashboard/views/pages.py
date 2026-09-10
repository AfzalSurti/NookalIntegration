"""Server-rendered operational pages — data still loaded via authorized API services."""
from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.dashboard.auth import Session, User
from app.dashboard.authorization import AuthorizationError, Permission
from app.dashboard.container import DashboardContainer
from app.dashboard.dependencies import (
    approval_service,
    appointment_service,
    audit_viewer_service,
    get_container,
    get_correlation_id,
    get_session,
    invoice_service,
    marketing_service,
    patient_service,
    referrer_service,
    require_permission,
    require_user,
    system_service,
    review_service,
)
from app.dashboard.services import (
    ApprovalService,
    AppointmentService,
    AuditViewerService,
    InvoiceService,
    MarketingService,
    PatientService,
    ReferrerConflictService,
    SystemService,
    ReviewService,
)

router = APIRouter(tags=["views"])


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


def _can(container: DashboardContainer, role: str, permission: Permission) -> bool:
    try:
        container.policy.require(role, permission)
        return True
    except AuthorizationError:
        return False


def _base_ctx(
    request: Request,
    user: User | None,
    session: Session | None,
    *,
    pending: int = 0,
    kill_switch: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "request": request,
        "user": user,
        "csrf_token": session.csrf_token if session else "",
        "pending_approvals": pending,
        "kill_switch_active": kill_switch,
    }
    if extra:
        ctx.update(extra)
    return ctx


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request,
        name="login.html",
        context=_base_ctx(request, None, None, extra={"error": None}),
    )


@router.post("/login", response_model=None)
async def login_form(
    request: Request,
    container: Annotated[DashboardContainer, Depends(get_container)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> RedirectResponse | HTMLResponse:
    user = container.auth_backend.authenticate(username, password)
    if user is None:
        return _templates(request).TemplateResponse(
            request,
            name="login.html",
            context=_base_ctx(request, None, None, extra={"error": "Invalid credentials"}),
            status_code=401,
        )
    session = container.sessions.create(user)
    container.audit(
        actor=user.user_id,
        action="dashboard.login",
        target_type="system",
        target_id="auth",
        result="success",
        metadata={"role": user.role, "via": "form"},
    )
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        key=container.session_cookie_name,
        value=session.session_id,
        httponly=True,
        samesite="lax",
        secure=container.environment == "production",
        max_age=8 * 3600,
        path="/",
    )
    return resp


@router.get("/logout", response_model=None)
async def logout_page(
    request: Request,
    container: Annotated[DashboardContainer, Depends(get_container)],
    session: Annotated[Session | None, Depends(get_session)],
) -> RedirectResponse:
    if session is not None:
        container.sessions.invalidate(session.session_id)
        container.audit(
            actor=session.user_id,
            action="dashboard.logout",
            target_type="system",
            target_id="auth",
            result="success",
            metadata={"role": session.role, "via": "page"},
        )
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(container.session_cookie_name, path="/")
    return resp


@router.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    session: Annotated[Session | None, Depends(get_session)],
    sys_svc: Annotated[SystemService, Depends(system_service)],
    appt_svc: Annotated[AppointmentService, Depends(appointment_service)],
    appr_svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    status = sys_svc.status(actor=user.user_id, role=user.role, correlation_id=correlation_id)
    upcoming = appt_svc.list_upcoming(
        actor=user.user_id, role=user.role, correlation_id=correlation_id,
    )
    pending_tasks = appr_svc.list_tasks(
        actor=user.user_id, role=user.role, correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        request,
        name="overview.html",
        context=_base_ctx(
            request,
            user,
            session,
            pending=status.pending_approvals,
            kill_switch=status.kill_switch_active,
            extra={
                "status": status,
                "upcoming_appointments": upcoming[:5],
                "upcoming_count": len(upcoming),
                "pending_tasks": pending_tasks[:5],
                "pending_count": len(pending_tasks),
            },
        ),
    )


def _parse_filter_int(val: str | None) -> int | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _parse_filter_date(val: str | None) -> date | None:
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


@router.get("/patients", response_class=HTMLResponse)
async def patients_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_SEARCH))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[PatientService, Depends(patient_service)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    q: str | None = None,
    deceased: str | None = None,
    suburb: str | None = None,
    age_min: str | None = None,
    age_max: str | None = None,
    appointment_from: str | None = None,
    appointment_to: str | None = None,
    referrer_id: str | None = None,
) -> HTMLResponse:
    clean_q = q.strip() if q and q.strip() else None
    clean_deceased = int(deceased) if deceased in ("0", "1") else None
    clean_suburb = suburb.strip() if suburb and suburb.strip() else None
    clean_age_min = _parse_filter_int(age_min)
    clean_age_max = _parse_filter_int(age_max)
    clean_appt_from = _parse_filter_date(appointment_from)
    clean_appt_to = _parse_filter_date(appointment_to)
    clean_referrer_id = referrer_id.strip() if referrer_id and referrer_id.strip() else None

    results = svc.search(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        query=clean_q,
        deceased=clean_deceased,
        suburb=clean_suburb,
        age_min=clean_age_min,
        age_max=clean_age_max,
        appointment_from=clean_appt_from,
        appointment_to=clean_appt_to,
        referrer_id=clean_referrer_id,
    )

    known_referrers: list[dict[str, str]] = []
    referrers_supported = False
    if hasattr(container.nookal, "list_referrers"):
        try:
            ref_list = container.nookal.list_referrers()
            known_referrers = [
                {"id": r.referrer_id, "name": f"{r.name} ({r.referrer_id})" if r.name else r.referrer_id}
                for r in ref_list
            ]
            referrers_supported = True
        except (NotImplementedError, AttributeError):
            referrers_supported = False

    filters = {
        "q": clean_q or "",
        "deceased": str(clean_deceased) if clean_deceased is not None else "",
        "suburb": clean_suburb or "",
        "age_min": str(clean_age_min) if clean_age_min is not None else "",
        "age_max": str(clean_age_max) if clean_age_max is not None else "",
        "appointment_from": clean_appt_from.isoformat() if clean_appt_from else "",
        "appointment_to": clean_appt_to.isoformat() if clean_appt_to else "",
        "referrer_id": clean_referrer_id or "",
    }
    has_active_filters = any(bool(v) for v in filters.values())

    return _templates(request).TemplateResponse(
        request,
        name="patients.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "results": results,
                "filters": filters,
                "has_active_filters": has_active_filters,
                "known_referrers": known_referrers,
                "referrers_supported": referrers_supported,
            },
        ),
    )


@router.get("/patients/{patient_id}", response_class=HTMLResponse)
async def patient_detail_page(
    request: Request,
    patient_id: str,
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[PatientService, Depends(patient_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    detail = svc.get_detail(
        patient_id,
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        request,
        name="patient_detail.html",
        context=_base_ctx(request, user, session, extra={"patient": detail}),
    )


@router.get("/patients/{patient_id}/files/{file_id}/url")
async def patient_file_url(
    patient_id: str,
    file_id: str,
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_VIEW))],
    container: Annotated[DashboardContainer, Depends(get_container)],
) -> RedirectResponse:
    if not hasattr(container.nookal, "get_file_url"):
        raise HTTPException(status_code=501, detail="Nookal file download unsupported")
    try:
        url = container.nookal.get_file_url(patient_id, file_id)
        return RedirectResponse(url=url, status_code=303)
    except Exception as exc:
        raise HTTPException(status_code=404, detail="File URL could not be retrieved") from exc


@router.get("/appointments", response_class=HTMLResponse)
async def appointments_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.APPOINTMENT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[AppointmentService, Depends(appointment_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    date_from: str | None = None,
    date_to: str | None = None,
    patient_id: str | None = None,
    location_id: str | None = None,
    practitioner_id: str | None = None,
    status: str | None = None,
) -> HTMLResponse:
    clean_date_from = _parse_filter_date(date_from)
    clean_date_to = _parse_filter_date(date_to)
    clean_patient = patient_id.strip() if patient_id and patient_id.strip() else None
    clean_location = location_id.strip() if location_id and location_id.strip() else None
    clean_practitioner = practitioner_id.strip() if practitioner_id and practitioner_id.strip() else None
    clean_status = status.strip() if status and status.strip() else None

    items = svc.list_upcoming(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        date_from=clean_date_from,
        date_to=clean_date_to,
        patient_id=clean_patient,
        location_id=clean_location,
        practitioner_id=clean_practitioner,
        status=clean_status,
    )
    can_change = _can(container, user.role, Permission.APPOINTMENT_CHANGE)

    known_locations = []
    if hasattr(container.nookal, "get_locations"):
        try:
            known_locations = container.nookal.get_locations()
        except Exception:
            pass

    known_practitioners = []
    if hasattr(container.nookal, "get_practitioners"):
        try:
            known_practitioners = container.nookal.get_practitioners()
        except Exception:
            pass

    appt_filters = {
        "date_from": clean_date_from.isoformat() if clean_date_from else "",
        "date_to": clean_date_to.isoformat() if clean_date_to else "",
        "patient_id": clean_patient or "",
        "location_id": clean_location or "",
        "practitioner_id": clean_practitioner or "",
        "status": clean_status or "",
    }
    has_appt_filters = any(bool(v) for v in appt_filters.values())
    return _templates(request).TemplateResponse(
        request,
        name="appointments.html",
        context=_base_ctx(request, user, session, extra={
            "appointments": items,
            "filters": appt_filters,
            "has_active_filters": has_appt_filters,
            "can_change": can_change,
            "known_locations": known_locations,
            "known_practitioners": known_practitioners,
        }),
    )


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    tasks = svc.list_tasks(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        request,
        name="approvals.html",
        context=_base_ctx(
            request,
            user,
            session,
            pending=len(tasks),
            extra={
                "tasks": tasks,
                "can_approve": _can(container, user.role, Permission.APPROVAL_APPROVE),
                "can_reject": _can(container, user.role, Permission.APPROVAL_REJECT),
            },
        ),
    )


@router.get("/documents", response_class=HTMLResponse)
async def documents_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    tasks = svc.document_tasks(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        request,
        name="documents.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "tasks": tasks,
                "can_approve": _can(container, user.role, Permission.APPROVAL_APPROVE),
                "can_reject": _can(container, user.role, Permission.APPROVAL_REJECT),
            },
        ),
    )


@router.get("/finance/documents", response_class=HTMLResponse)
async def finance_documents_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.DOCUMENT_REVIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[ReviewService, Depends(review_service)],
) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request,
        name="finance_documents.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "documents": list(svc.documents.values()),
                "can_review": _can(container, user.role, Permission.DOCUMENT_REVIEW),
            },
        ),
    )


@router.get("/finance/expenses", response_class=HTMLResponse)
async def finance_expenses_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.EXPENSE_REVIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[ReviewService, Depends(review_service)],
) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        request,
        name="finance_expenses.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "expenses": svc.list_expenses(),
                "can_confirm": _can(container, user.role, Permission.EXPENSE_REVIEW),
            },
        ),
    )


@router.get("/finance/invoices", response_class=HTMLResponse)
async def finance_invoices_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.DOCUMENT_REVIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[InvoiceService, Depends(invoice_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    patient_id: str | None = None,
    status: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    clean_patient = patient_id.strip() if patient_id and patient_id.strip() else None
    clean_status = status.strip() if status and status.strip() else None
    clean_from = date_from.strip() if date_from and date_from.strip() else None
    clean_to = date_to.strip() if date_to and date_to.strip() else None

    try:
        invoices = svc.list_invoices(
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            patient_id=clean_patient,
            status=clean_status,
            date_from=clean_from,
            date_to=clean_to,
            page=1,
            page_length=50,
        )
    except Exception:
        invoices = []

    filters = {
        "patient_id": clean_patient or "",
        "status": clean_status or "",
        "date_from": clean_from or "",
        "date_to": clean_to or "",
    }
    has_active_filters = any(bool(v) for v in filters.values())

    return _templates(request).TemplateResponse(
        request,
        name="finance_invoices.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "invoices": invoices,
                "filters": filters,
                "has_active_filters": has_active_filters,
            },
        ),
    )


@router.get("/cases", response_class=HTMLResponse)
async def cases_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.CASE_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
) -> HTMLResponse:
    tracker = getattr(container, "case_tracking", None)
    flagged_cases = tracker.flagged_cases() if tracker is not None else []
    nookal_cases = []
    if hasattr(container.nookal, "get_all_cases"):
        try:
            nookal_cases = container.nookal.get_all_cases(page=1, page_length=50)
        except Exception:
            nookal_cases = []

    return _templates(request).TemplateResponse(
        request,
        name="cases.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "cases": flagged_cases,
                "nookal_cases": nookal_cases,
                "can_acknowledge": _can(container, user.role, Permission.CASE_ACKNOWLEDGE),
            },
        ),
    )


@router.get("/referrers", response_class=HTMLResponse)
async def referrers_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.REFERRER_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[ReferrerConflictService, Depends(referrer_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    conflicts = svc.list_conflicts(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        request,
        name="referrers.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "conflicts": conflicts,
                "can_resolve": _can(container, user.role, Permission.REFERRER_RESOLVE),
            },
        ),
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.AUDIT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[AuditViewerService, Depends(audit_viewer_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    actor: str | None = None,
    action: str | None = None,
    result: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> HTMLResponse:
    clean_actor = actor.strip() if actor and actor.strip() else None
    clean_action = action.strip() if action and action.strip() else None
    clean_result = result.strip() if result and result.strip() else None
    clean_from = _parse_filter_date(date_from)
    clean_to = _parse_filter_date(date_to)
    events = svc.query(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        filter_actor=clean_actor,
        filter_action=clean_action,
        filter_result=clean_result,
        date_from=clean_from,
        date_to=clean_to,
        limit=200,
    )
    audit_filters = {
        "actor": clean_actor or "",
        "action": clean_action or "",
        "result": clean_result or "",
        "date_from": clean_from.isoformat() if clean_from else "",
        "date_to": clean_to.isoformat() if clean_to else "",
    }
    return _templates(request).TemplateResponse(
        request,
        name="audit.html",
        context=_base_ctx(request, user, session, extra={
            "events": events,
            "filters": audit_filters,
            "event_count": len(events),
        }),
    )


@router.get("/marketing", response_class=HTMLResponse)
async def marketing_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[MarketingService, Depends(marketing_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    lists = svc.list_lists(actor=user.user_id, role=user.role, correlation_id=correlation_id)
    campaigns = svc.list_campaigns(actor=user.user_id, role=user.role, correlation_id=correlation_id)
    can_manage = _can(container, user.role, Permission.MARKETING_MANAGE)
    return _templates(request).TemplateResponse(
        request,
        name="marketing.html",
        context=_base_ctx(request, user, session, extra={
            "lists": lists,
            "campaigns": campaigns,
            "can_manage": can_manage,
        }),
    )


@router.get("/system", response_class=HTMLResponse)
async def system_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.SYSTEM_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    sys_svc: Annotated[SystemService, Depends(system_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    status = sys_svc.status(actor=user.user_id, role=user.role, correlation_id=correlation_id)
    return _templates(request).TemplateResponse(
        request,
        name="system.html",
        context=_base_ctx(
            request,
            user,
            session,
            pending=status.pending_approvals,
            kill_switch=status.kill_switch_active,
            extra={
                "status": status,
                "can_toggle_kill_switch": _can(container, user.role, Permission.SYSTEM_KILL_SWITCH),
            },
        ),
    )
