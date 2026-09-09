"""Server-rendered operational pages — data still loaded via authorized API services."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.dashboard.auth import Session, User
from app.dashboard.authorization import Permission
from app.dashboard.container import DashboardContainer
from app.dashboard.dependencies import (
    approval_service,
    appointment_service,
    get_container,
    get_correlation_id,
    get_session,
    patient_service,
    referrer_service,
    require_permission,
    require_user,
    system_service,
)
from app.dashboard.services import (
    ApprovalService,
    AppointmentService,
    PatientService,
    ReferrerConflictService,
    SystemService,
)

router = APIRouter(tags=["views"])


def _templates(request: Request) -> Jinja2Templates:
    return request.app.state.templates


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
        "nav": [
            ("Overview", "/"),
            ("Patients", "/patients"),
            ("Appointments", "/appointments"),
            ("Approval Queue", "/approvals"),
            ("Documents", "/documents"),
            ("Referrer Conflicts", "/referrers"),
            ("Audit", "/audit"),
            ("Marketing", "/marketing"),
            ("System", "/system"),
        ],
    }
    if extra:
        ctx.update(extra)
    return ctx


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    return _templates(request).TemplateResponse(
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


@router.get("/", response_class=HTMLResponse)
async def overview(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    session: Annotated[Session | None, Depends(get_session)],
    sys_svc: Annotated[SystemService, Depends(system_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    status = sys_svc.status(actor=user.user_id, role=user.role, correlation_id=correlation_id)
    return _templates(request).TemplateResponse(
        name="overview.html",
        context=_base_ctx(
            request,
            user,
            session,
            pending=status.pending_approvals,
            kill_switch=status.kill_switch_active,
            extra={"status": status},
        ),
    )


@router.get("/patients", response_class=HTMLResponse)
async def patients_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_SEARCH))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[PatientService, Depends(patient_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    suburb: str | None = None,
) -> HTMLResponse:
    results = svc.search(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
        suburb=suburb,
    )
    return _templates(request).TemplateResponse(
        name="patients.html",
        context=_base_ctx(request, user, session, extra={"results": results, "suburb": suburb or ""}),
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
        name="patient_detail.html",
        context=_base_ctx(request, user, session, extra={"patient": detail}),
    )


@router.get("/appointments", response_class=HTMLResponse)
async def appointments_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.APPOINTMENT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[AppointmentService, Depends(appointment_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    items = svc.list_upcoming(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        name="appointments.html",
        context=_base_ctx(request, user, session, extra={"appointments": items}),
    )


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    tasks = svc.list_tasks(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        name="approvals.html",
        context=_base_ctx(request, user, session, pending=len(tasks), extra={"tasks": tasks}),
    )


@router.get("/documents", response_class=HTMLResponse)
async def documents_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.APPROVAL_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[ApprovalService, Depends(approval_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    tasks = svc.document_tasks(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        name="documents.html",
        context=_base_ctx(request, user, session, extra={"tasks": tasks}),
    )


@router.get("/referrers", response_class=HTMLResponse)
async def referrers_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.REFERRER_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[ReferrerConflictService, Depends(referrer_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    conflicts = svc.list_conflicts(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    return _templates(request).TemplateResponse(
        name="referrers.html",
        context=_base_ctx(request, user, session, extra={"conflicts": conflicts}),
    )


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.AUDIT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        name="audit.html",
        context=_base_ctx(request, user, session),
    )


@router.get("/marketing", response_class=HTMLResponse)
async def marketing_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.MARKETING_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        name="marketing.html",
        context=_base_ctx(request, user, session),
    )


@router.get("/system", response_class=HTMLResponse)
async def system_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.SYSTEM_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    sys_svc: Annotated[SystemService, Depends(system_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    status = sys_svc.status(actor=user.user_id, role=user.role, correlation_id=correlation_id)
    return _templates(request).TemplateResponse(
        name="system.html",
        context=_base_ctx(
            request,
            user,
            session,
            pending=status.pending_approvals,
            kill_switch=status.kill_switch_active,
            extra={"status": status},
        ),
    )
