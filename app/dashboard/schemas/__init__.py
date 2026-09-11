"""Pydantic response / request schemas — minimum necessary fields."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=256)


class UserOut(BaseModel):
    user_id: str
    username: str
    role: str
    display_name: str | None = None


class SessionOut(BaseModel):
    user: UserOut
    csrf_token: str


class PatientSummary(BaseModel):
    patient_id: str
    display_name: str | None = None
    suburb: str | None = None
    age: int | None = None
    referrer_id: str | None = None
    last_appointment_date: date | None = None


class PatientSearchResponse(BaseModel):
    items: list[PatientSummary]
    total: int
    page: int
    page_size: int


class AppointmentSummary(BaseModel):
    appointment_id: str
    patient_id: str
    starts_at: datetime
    ends_at: datetime | None = None
    status: str | None = None
    practitioner_id: str | None = None
    location_id: str | None = None
    appointment_type: str | None = None



class DocumentMetaOut(BaseModel):
    document_id: str
    patient_id: str
    title: str | None = None
    status: str | None = None
    template_id: str | None = None
    task_id: str | None = None


class CaseOut(BaseModel):
    case_id: str
    patient_id: str
    case_name: str | None = None
    case_number: str | None = None
    status: str | None = None
    date_created: str | None = None
    closed_date: str | None = None


class TreatmentNoteOut(BaseModel):
    note_id: str
    patient_id: str
    case_id: str | None = None
    practitioner_id: str | None = None
    date: str | None = None
    notes: str | None = None
    appointment_id: str | None = None


class PatientFileOut(BaseModel):
    file_id: str
    patient_id: str
    name: str | None = None
    file_type: str | None = None
    date_added: str | None = None
    size: int | None = None


class PatientInvoiceOut(BaseModel):
    invoice_id: str
    patient_id: str
    date: str | None = None
    total: float | None = None
    status: str | None = None
    void: bool = False


class PatientDetail(BaseModel):
    patient_id: str
    display_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    last_name: str | None = None
    nickname: str | None = None
    phone: str | None = None
    email: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    suburb: str | None = None
    address: Any = None
    postal_address: Any = None
    online_code: str | None = None
    deceased: bool = False
    age: int | None = None
    referrer_id: str | None = None
    referrer_name: str | None = None
    date_created: str | None = None
    date_modified: str | None = None
    last_appointment_date: date | None = None
    appointments: list[AppointmentSummary] = Field(default_factory=list)
    documents: list[DocumentMetaOut] = Field(default_factory=list)
    cases: list[CaseOut] = Field(default_factory=list)
    treatment_notes: list[TreatmentNoteOut] = Field(default_factory=list)
    patient_files: list[PatientFileOut] = Field(default_factory=list)
    invoices: list[PatientInvoiceOut] = Field(default_factory=list)
    cases_error: str | None = None
    files_error: str | None = None
    invoices_error: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)



class SafeDraftOut(BaseModel):
    """Safe task draft view — no clinical free-text dumps."""

    template_id: str | None = None
    letter_type: str | None = None
    certificate_type: str | None = None
    status_tag: str | None = None
    source: str | None = None
    keys: list[str] = Field(default_factory=list)


class TaskOut(BaseModel):
    id: str
    type: str
    status: str
    patient_id: str
    created_by: str
    reviewed_by: str | None = None
    reviewer_notes: str | None = None
    created_at: str
    updated_at: str
    safe_draft: SafeDraftOut


class ReviewActionRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=500)


class ReferrerConflictOut(BaseModel):
    conflict_id: str
    kind: str  # conflict | new
    candidate_name: str | None = None
    candidate_provider_number: str | None = None
    possible_matches: list[str] = Field(default_factory=list)
    reason: str | None = None


class ReferrerResolveRequest(BaseModel):
    action: str  # select_existing | create_new | reject
    referrer_id: str | None = None
    notes: str | None = Field(default=None, max_length=200)


class AppointmentActionRequest(BaseModel):
    action: str  # cancel | reschedule
    phone: str = Field(min_length=1, max_length=32)
    confirmation_text: str | None = Field(default=None, max_length=64)
    pending_action_id: str | None = None
    message: str | None = Field(default=None, max_length=500)
    starts_at: datetime | None = None


class AuditEventOut(BaseModel):
    timestamp: str
    actor: str
    action: str
    target_type: str
    target_id: str
    result: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SystemStatusOut(BaseModel):
    application: str
    environment: str
    kill_switch_active: bool
    llm_configured: bool
    nookal_configured: bool
    messaging_configured: bool
    pending_approvals: int


class KillSwitchRequest(BaseModel):
    active: bool
    reason: str | None = Field(default=None, max_length=200)


class ErrorOut(BaseModel):
    detail: str


class DocumentResolveRequest(BaseModel):
    patient_id: str = Field(min_length=1, max_length=128)


class ExpenseConfirmRequest(BaseModel):
    category: str = Field(min_length=1, max_length=128)
    fields: dict[str, Any] = Field(default_factory=dict)


class SendMessageRequest(BaseModel):
    channel: str = Field(pattern="(?i)^(sms|email|whatsapp)$")
    recipient_contact: str | None = None
    template_id: str = "direct_message"
    message: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class SendMessageResponse(BaseModel):
    message_id: str
    status: str
    channel: str
    recipient: str
    rendered_content: str
    sent_at: str | None = None
    provider_ref: str | None = None
    idempotency_key: str


class DocumentDraftRequest(BaseModel):
    patient_id: str = Field(min_length=1)
    document_type: str = Field(pattern="^(certificate|referral_thank_you|treatment_completion|progress_letter)$")
    certificate_type: str | None = None
    notes: str | None = None


