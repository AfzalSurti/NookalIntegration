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
from app.shared.exceptions import NookalInvalidFileId
from app.dashboard.dependencies import (
    approval_service,
    appointment_service,
    audit_viewer_service,
    case_service,
    get_container,
    get_correlation_id,
    get_session,
    invoice_service,
    marketing_service,
    patient_file_service,
    patient_service,
    referrer_service,
    require_permission,
    require_user,
    system_service,
    treatment_note_service,
    review_service,
    communication_service,
)
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
    CommunicationDashboardService,
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


@router.get("/", response_model=None)
async def overview(
    request: Request,
    user: Annotated[User, Depends(require_user)],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
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
    suburbs_param = [s.strip() for s in request.query_params.getlist("suburb") if s and s.strip()]
    if not suburbs_param and suburb and suburb.strip():
        suburbs_param = [suburb.strip()]
    clean_suburbs = suburbs_param if suburbs_param else None
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
        suburb=clean_suburbs,
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

    known_suburbs: list[str] = []
    try:
        all_patients = container.nookal.get_patients(page=1, page_length=200)
        raw_suburbs = {p.suburb for p in all_patients if p.suburb and p.suburb.strip()}
        known_suburbs = sorted(raw_suburbs, key=str.lower)
    except Exception:
        known_suburbs = []

    filters = {
        "q": clean_q or "",
        "deceased": str(clean_deceased) if clean_deceased is not None else "",
        "suburb": suburbs_param[0] if len(suburbs_param) == 1 else "",
        "suburbs": suburbs_param,
        "age_min": str(clean_age_min) if clean_age_min is not None else "",
        "age_max": str(clean_age_max) if clean_age_max is not None else "",
        "appointment_from": clean_appt_from.isoformat() if clean_appt_from else "",
        "appointment_to": clean_appt_to.isoformat() if clean_appt_to else "",
        "referrer_id": clean_referrer_id or "",
    }
    has_active_filters = any(bool(v) for k, v in filters.items() if k != "suburbs") or bool(suburbs_param)

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
                "known_suburbs": known_suburbs,
                "selected_suburbs": suburbs_param,
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
    svc: Annotated[PatientFileService, Depends(patient_file_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> RedirectResponse:
    try:
        url = svc.get_file_url(
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            patient_id=patient_id,
            file_id=file_id,
        )
        return RedirectResponse(url=url, status_code=303)
    except NookalInvalidFileId as exc:
        raise HTTPException(status_code=400, detail="File URL could not be retrieved") from exc
    except Exception as exc:
        raise HTTPException(status_code=404, detail="File URL could not be retrieved") from exc


@router.get("/patients/{patient_id}/files/{file_id}/view", response_class=HTMLResponse)
async def patient_file_view_page(
    request: Request,
    patient_id: str,
    file_id: str,
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[PatientFileService, Depends(patient_file_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    try:
        url = svc.get_file_url(
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            patient_id=patient_id,
            file_id=file_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail="File URL could not be retrieved") from exc

    file_name = None
    file_type = None
    try:
        files = svc.list_files(
            patient_id=patient_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
        for f in files:
            if str(f.file_id).strip() == str(file_id).strip():
                file_name = f.name
                file_type = f.file_type
                break
    except Exception:
        logger.exception("Failed to retrieve file metadata for file_id=%s patient_id=%s", file_id, patient_id)

    return _templates(request).TemplateResponse(
        request,
        name="patient_file_view.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "file_url": url,
                "file_id": file_id,
                "patient_id": patient_id,
                "file_name": file_name or f"File #{file_id}",
                "file_type": file_type or "file",
            },
        ),
    )


@router.get("/patients/{patient_id}/treatment-notes/{note_id}", response_class=HTMLResponse)
async def treatment_note_detail_page(
    request: Request,
    patient_id: str,
    note_id: str,
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    svc: Annotated[TreatmentNoteService, Depends(treatment_note_service)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    note = svc.get_note(
        patient_id,
        note_id,
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    if note is None:
        raise HTTPException(status_code=404, detail="Treatment note not found")

    # Resolve practitioner name
    practitioner_name = None
    if note.practitioner_id and hasattr(container.nookal, "get_practitioners"):
        try:
            for pr in container.nookal.get_practitioners():
                if str(pr.practitioner_id) == str(note.practitioner_id):
                    practitioner_name = f"{pr.first_name or ''} {pr.last_name or ''}".strip()
                    break
        except Exception:
            pass

    # Resolve patient name
    patient_name = None
    if hasattr(container.nookal, "get_patient"):
        try:
            pat = container.nookal.get_patient(patient_id)
            if pat:
                patient_name = f"{pat.first_name or ''} {pat.last_name or ''}".strip() or None
        except Exception:
            pass

    # Resolve location from appointment if available
    location_name = None

    # Extract structured note content from raw data
    note_sections = []
    if note.raw:
        raw = note.raw
        # Nookal treatment notes may have structured fields/answers
        # Extract 'answers' dict (question -> answer pairs)
        if isinstance(raw.get("answers"), dict):
            for key, val in raw["answers"].items():
                note_sections.append({"label": str(key), "value": str(val) if val else "—"})
        # Extract 'fields' list (structured form fields)
        if isinstance(raw.get("fields"), list):
            for fld in raw["fields"]:
                if isinstance(fld, dict):
                    label = fld.get("label") or fld.get("name") or fld.get("field_name") or ""
                    value = fld.get("value") or fld.get("answer") or fld.get("text") or ""
                    if label:
                        note_sections.append({"label": str(label), "value": str(value) if value else "—"})
        # Extract 'sections' list
        if isinstance(raw.get("sections"), list):
            for sec in raw["sections"]:
                if isinstance(sec, dict):
                    title = sec.get("title") or sec.get("heading") or sec.get("name") or ""
                    content = sec.get("content") or sec.get("text") or sec.get("value") or ""
                    if title or content:
                        note_sections.append({"label": str(title) if title else "Section", "value": str(content) if content else "—"})

    # Get HTML content if available
    html_content = None
    if note.raw:
        html_content = note.raw.get("html") or note.raw.get("HTML") or note.raw.get("content_html")

    return _templates(request).TemplateResponse(
        request,
        name="treatment_note_detail.html",
        context=_base_ctx(request, user, session, extra={
            "note": note,
            "patient_id": patient_id,
            "practitioner_name": practitioner_name,
            "patient_name": patient_name,
            "location_name": location_name,
            "note_sections": note_sections,
            "html_content": html_content,
        }),
    )


def _resolve_invoice_context(
    invoice: Any,
    entries: list[Any],
    container: DashboardContainer,
    svc: InvoiceService,
    user: User,
    correlation_id: str,
) -> dict[str, Any]:
    patient_name = None
    if invoice.patient_id:
        try:
            p = container.nookal.get_patient(invoice.patient_id)
            patient_name = p.display_name or f"{p.first_name or ''} {p.last_name or ''}".strip()
        except Exception:
            patient_name = None

    location_name = None
    if invoice.location_id and hasattr(container.nookal, "get_locations"):
        try:
            for loc in container.nookal.get_locations():
                if str(loc.location_id) == str(invoice.location_id):
                    location_name = loc.name
                    break
        except Exception:
            pass

    practitioner_name = None
    if invoice.practitioner_id and hasattr(container.nookal, "get_practitioners"):
        try:
            for pr in container.nookal.get_practitioners():
                if str(pr.practitioner_id) == str(invoice.practitioner_id):
                    practitioner_name = f"{pr.first_name} {pr.last_name or ''}".strip()
                    break
        except Exception:
            pass

    payments = []
    try:
        payments = svc.get_invoice_payments(
            invoice.invoice_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except Exception:
        payments = []

    all_entries = invoice.entries if invoice.entries else entries
    calc_subtotal = sum(((e.price or 0.0) * (e.quantity if e.quantity is not None else 1.0)) for e in all_entries) if all_entries else (invoice.total or 0.0)
    calc_tax = invoice.tax if invoice.tax is not None else sum((e.tax or 0.0) for e in all_entries)
    calc_total = invoice.total if (invoice.total is not None and invoice.total > 0) else (calc_subtotal + calc_tax)
    calc_paid = invoice.paid if invoice.paid is not None else (calc_total if (invoice.status or "").lower() == "paid" else 0.0)
    calc_balance = invoice.balance if invoice.balance is not None else (0.0 if (invoice.status or "").lower() == "paid" else max(0.0, calc_total - calc_paid))

    return {
        "invoice": invoice,
        "entries": all_entries,
        "patient_name": patient_name,
        "location_name": location_name,
        "practitioner_name": practitioner_name,
        "payments": payments,
        "calc_subtotal": calc_subtotal,
        "calc_tax": calc_tax,
        "calc_total": calc_total,
        "calc_paid": calc_paid,
        "calc_balance": calc_balance,
    }


@router.get("/patients/{patient_id}/invoices/{invoice_id}", response_class=HTMLResponse)
async def invoice_detail_page(
    request: Request,
    patient_id: str,
    invoice_id: str,
    user: Annotated[User, Depends(require_permission(Permission.PATIENT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[InvoiceService, Depends(invoice_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    try:
        invoice = svc.get_invoice(
            invoice_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invoice not found") from exc

    entries = []
    try:
        entries = svc.get_invoice_entries(
            invoice_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except Exception:
        pass

    inv_ctx = _resolve_invoice_context(
        invoice=invoice,
        entries=entries,
        container=container,
        svc=svc,
        user=user,
        correlation_id=correlation_id,
    )
    inv_ctx["patient_id"] = patient_id or invoice.patient_id or "0"

    return _templates(request).TemplateResponse(
        request,
        name="invoice_detail.html",
        context=_base_ctx(request, user, session, extra=inv_ctx),
    )


@router.get("/finance/invoices/{invoice_id}", response_class=HTMLResponse)
@router.get("/invoices/{invoice_id}", response_class=HTMLResponse)
async def finance_invoice_detail_page(
    request: Request,
    invoice_id: str,
    user: Annotated[User, Depends(require_permission(Permission.DOCUMENT_REVIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    svc: Annotated[InvoiceService, Depends(invoice_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    try:
        invoice = svc.get_invoice(
            invoice_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Invoice not found") from exc

    entries = []
    try:
        entries = svc.get_invoice_entries(
            invoice_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except Exception:
        pass

    inv_ctx = _resolve_invoice_context(
        invoice=invoice,
        entries=entries,
        container=container,
        svc=svc,
        user=user,
        correlation_id=correlation_id,
    )
    inv_ctx["patient_id"] = invoice.patient_id or "0"

    return _templates(request).TemplateResponse(
        request,
        name="invoice_detail.html",
        context=_base_ctx(request, user, session, extra=inv_ctx),
    )


@router.get("/invoices")
async def invoices_redirect(request: Request) -> RedirectResponse:
    qs = request.url.query
    dest = f"/finance/invoices?{qs}" if qs else "/finance/invoices"
    return RedirectResponse(url=dest, status_code=307)


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


@router.get("/appointments/{appointment_id}", response_class=HTMLResponse)
async def appointment_detail_page(
    request: Request,
    appointment_id: str,
    user: Annotated[User, Depends(require_permission(Permission.APPOINTMENT_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    appt_svc: Annotated[AppointmentService, Depends(appointment_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    try:
        appt = appt_svc.get_appointment(
            appointment_id,
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
        )
    except Exception as exc:
        raise HTTPException(status_code=404, detail="Appointment not found") from exc

    patient = None
    if appt.patient_id and hasattr(container.nookal, "get_patient"):
        try:
            patient = container.nookal.get_patient(appt.patient_id)
        except Exception:
            patient = None

    location_name = None
    if appt.location_id and hasattr(container.nookal, "get_locations"):
        try:
            for loc in container.nookal.get_locations():
                if str(loc.location_id) == str(appt.location_id):
                    location_name = loc.name
                    break
        except Exception:
            pass

    practitioner_name = None
    if appt.practitioner_id and hasattr(container.nookal, "get_practitioners"):
        try:
            for pr in container.nookal.get_practitioners():
                if str(pr.practitioner_id) == str(appt.practitioner_id):
                    practitioner_name = f"{pr.first_name} {pr.last_name or ''}".strip()
                    break
        except Exception:
            pass

    type_name = appt.appointment_type
    if not type_name and appt.type_id and hasattr(container.nookal, "get_appointment_types"):
        try:
            for at in container.nookal.get_appointment_types():
                if str(at.type_id) == str(appt.type_id):
                    type_name = at.name
                    break
        except Exception:
            pass

    related_notes = []
    if hasattr(container.nookal, "get_treatment_notes") and appt.patient_id:
        try:
            all_notes = container.nookal.get_treatment_notes(appt.patient_id)
            related_notes = [n for n in all_notes if n.appointment_id and str(n.appointment_id) == str(appt.appointment_id)]
        except Exception:
            related_notes = []

    related_invoices = []
    if hasattr(container.nookal, "get_invoices") and appt.patient_id:
        try:
            all_invs = container.nookal.get_invoices(patient_id=appt.patient_id)
            related_invoices = all_invs
        except Exception:
            related_invoices = []

    can_change = _can(container, user.role, Permission.APPOINTMENT_CHANGE)

    duration_minutes = None
    if appt.starts_at and appt.ends_at:
        try:
            duration_minutes = max(1, int((appt.ends_at - appt.starts_at).total_seconds() // 60))
        except Exception:
            duration_minutes = None

    return _templates(request).TemplateResponse(
        request,
        name="appointment_detail.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "appointment": appt,
                "patient": patient,
                "location_name": location_name,
                "practitioner_name": practitioner_name,
                "type_name": type_name,
                "related_notes": related_notes,
                "related_invoices": related_invoices,
                "can_change": can_change,
                "duration_minutes": duration_minutes,
            },
        ),
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
    file_svc: Annotated[PatientFileService, Depends(patient_file_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
    patient_id: str | None = None,
) -> HTMLResponse:
    tasks = svc.document_tasks(
        actor=user.user_id,
        role=user.role,
        correlation_id=correlation_id,
    )
    clean_patient = patient_id.strip() if patient_id and patient_id.strip() else None
    patient_files = []
    files_error = None
    if clean_patient:
        try:
            patient_files = file_svc.list_files(
                actor=user.user_id,
                role=user.role,
                correlation_id=correlation_id,
                patient_id=clean_patient,
            )
        except Exception:
            patient_files = []
            files_error = "Unable to load patient files from Nookal at this time."

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
                "patient_id_filter": clean_patient or "",
                "patient_files": patient_files,
                "files_error": files_error,
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

    invoices_error = None
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
        invoices_error = "Unable to load Nookal invoices at this time."

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
                "invoices_error": invoices_error,
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
    svc: Annotated[CaseService, Depends(case_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    tracker = getattr(container, "case_tracking", None)
    flagged_cases = tracker.flagged_cases() if tracker is not None else []
    nookal_cases = []
    cases_error = None
    try:
        nookal_cases = svc.list_cases(
            actor=user.user_id,
            role=user.role,
            correlation_id=correlation_id,
            page=1,
            page_length=50,
        )
    except Exception:
        nookal_cases = []
        cases_error = "Unable to load Nookal clinical cases at this time."

    patient_names = {}
    if hasattr(container.nookal, "get_patient"):
        for nc in nookal_cases:
            if nc.patient_id and nc.patient_id not in patient_names:
                try:
                    pat = container.nookal.get_patient(nc.patient_id)
                    if pat:
                        patient_names[nc.patient_id] = f"{pat.first_name or ''} {pat.last_name or ''}".strip() or nc.patient_id
                except Exception:
                    pass

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
                "patient_names": patient_names,
                "cases_error": cases_error,
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
    from app.orchestration.referral_sync_service import ReferralAssociationStore
    assoc_store = getattr(container, "referral_association_store", None)
    if assoc_store is None:
        assoc_store = ReferralAssociationStore()
        container.referral_association_store = assoc_store
    associations = assoc_store.list_all()

    return _templates(request).TemplateResponse(
        request,
        name="referrers.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "conflicts": conflicts,
                "associations": associations,
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


@router.get("/communication", response_class=HTMLResponse)
async def communication_page(
    request: Request,
    user: Annotated[User, Depends(require_permission(Permission.COMMUNICATION_VIEW))],
    session: Annotated[Session | None, Depends(get_session)],
    container: Annotated[DashboardContainer, Depends(get_container)],
    comm_svc: Annotated[CommunicationDashboardService, Depends(communication_service)],
    correlation_id: Annotated[str, Depends(get_correlation_id)],
) -> HTMLResponse:
    overview = comm_svc.get_overview(actor=user.user_id, role=user.role, correlation_id=correlation_id)
    workflows = comm_svc.get_workflows(actor=user.user_id, role=user.role, correlation_id=correlation_id)
    can_manage = _can(container, user.role, Permission.COMMUNICATION_MANAGE)
    return _templates(request).TemplateResponse(
        request,
        name="communication.html",
        context=_base_ctx(
            request,
            user,
            session,
            extra={
                "overview": overview,
                "workflows": workflows,
                "can_manage": can_manage,
            },
        ),
    )

