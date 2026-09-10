"""Patient search and detail — uses injected NookalClient only."""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from app.dashboard.schemas import (
    AppointmentSummary,
    CaseOut,
    DocumentMetaOut,
    PatientDetail,
    PatientFileOut,
    PatientInvoiceOut,
    PatientSearchResponse,
    PatientSummary,
    TreatmentNoteOut,
)
from app.letters import DocumentStore
from app.nookal_client import NookalClient, PatientRef
from app.shared.clock import Clock, SystemClock


AuditFn = Callable[..., Any]


def _age_years(dob: date | None, today: date) -> int | None:
    if dob is None:
        return None
    years = today.year - dob.year
    if (today.month, today.day) < (dob.month, dob.day):
        years -= 1
    return years


def _summary(patient: PatientRef, today: date) -> PatientSummary:
    return PatientSummary(
        patient_id=patient.patient_id,
        display_name=patient.display_name,
        suburb=patient.suburb,
        age=_age_years(patient.date_of_birth, today),
        referrer_id=patient.referrer_id,
        last_appointment_date=patient.last_appointment_date,
    )


class PatientService:
    def __init__(
        self,
        *,
        nookal: NookalClient,
        audit: AuditFn,
        clock: Clock | None = None,
        document_store: DocumentStore | None = None,
    ) -> None:
        self._nookal = nookal
        self._audit = audit
        self._clock = clock or SystemClock()
        self._documents = document_store

    def search(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        query: str | None = None,
        deceased: int | None = None,
        suburb: str | None = None,
        age_min: int | None = None,
        age_max: int | None = None,
        appointment_from: date | None = None,
        appointment_to: date | None = None,
        referrer_id: str | None = None,
        page: int = 1,
        page_size: int = 25,
    ) -> PatientSearchResponse:
        page = max(1, page)
        page_size = min(max(1, page_size), 100)
        results = self._nookal.search_patients(
            fuzzy_search=query,
            deceased=deceased,
            suburb=suburb,
            age_min=age_min,
            age_max=age_max,
            appointment_from=appointment_from,
            appointment_to=appointment_to,
            referrer_id=referrer_id,
        )
        today = self._clock.now().date()
        total = len(results)
        start = (page - 1) * page_size
        slice_ = results[start : start + page_size]
        items = [_summary(p, today) for p in slice_]

        # Audit: filters and counts only — no patient names / clinical content.
        self._audit(
            actor=actor,
            action="dashboard.patient_search",
            target_type="patient_record",
            target_id="search",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "result_count": total,
                "page": page,
                "has_query": query is not None,
                "has_deceased": deceased is not None,
                "has_suburb": suburb is not None,
                "has_age": age_min is not None or age_max is not None,
                "has_appt_range": appointment_from is not None or appointment_to is not None,
                "has_referrer": referrer_id is not None,
            },
        )
        return PatientSearchResponse(items=items, total=total, page=page, page_size=page_size)

    def get_detail(
        self,
        patient_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> PatientDetail:
        patient = self._nookal.get_patient(patient_id)
        today = self._clock.now().date()
        appointments = self._nookal.list_appointments(patient_id=patient_id)
        appt_out = [
            AppointmentSummary(
                appointment_id=a.appointment_id,
                patient_id=a.patient_id,
                starts_at=a.starts_at,
                ends_at=a.ends_at,
                status=a.status,
                practitioner_id=a.practitioner_id,
            )
            for a in sorted(appointments, key=lambda x: x.starts_at, reverse=True)
        ]

        docs: list[DocumentMetaOut] = []
        if self._documents is not None:
            for rec in self._documents.list_for_patient(patient_id):
                status_val = rec.status.value if hasattr(rec.status, "value") else str(rec.status)
                docs.append(
                    DocumentMetaOut(
                        document_id=rec.document_id,
                        patient_id=rec.patient_id,
                        title=None,
                        status=status_val,
                        template_id=rec.template_id,
                        task_id=rec.task_id,
                    )
                )

        cases_out: list[CaseOut] = []
        if hasattr(self._nookal, "get_cases"):
            try:
                cases = self._nookal.get_cases(patient_id)
                cases_out = [
                    CaseOut(
                        case_id=c.case_id,
                        patient_id=c.patient_id,
                        case_name=c.case_name,
                        case_number=c.case_number,
                        status=c.status,
                        date_created=c.date_created,
                        closed_date=c.closed_date,
                    )
                    for c in cases
                ]
            except Exception:
                cases_out = []

        notes_out: list[TreatmentNoteOut] = []
        if hasattr(self._nookal, "get_treatment_notes"):
            try:
                notes = self._nookal.get_treatment_notes(patient_id)
                notes_out = [
                    TreatmentNoteOut(
                        note_id=n.note_id,
                        patient_id=n.patient_id,
                        case_id=n.case_id,
                        practitioner_id=n.practitioner_id,
                        date=n.date,
                        notes=n.notes,
                        appointment_id=n.appointment_id,
                    )
                    for n in notes
                ]
            except Exception:
                notes_out = []

        files_out: list[PatientFileOut] = []
        if hasattr(self._nookal, "get_patient_files"):
            try:
                files = self._nookal.get_patient_files(patient_id)
                files_out = [
                    PatientFileOut(
                        file_id=f.file_id,
                        patient_id=f.patient_id,
                        name=f.name,
                        file_type=f.file_type,
                        date_added=f.date_added,
                        size=f.size,
                    )
                    for f in files
                ]
            except Exception:
                files_out = []

        invoices_out: list[PatientInvoiceOut] = []
        if hasattr(self._nookal, "get_invoices"):
            try:
                invoices = self._nookal.get_invoices(patient_id=patient_id)
                invoices_out = [
                    PatientInvoiceOut(
                        invoice_id=inv.invoice_id,
                        patient_id=inv.patient_id,
                        date=inv.date,
                        total=inv.total,
                        status=inv.status,
                        void=inv.void,
                    )
                    for inv in invoices
                ]
            except Exception:
                invoices_out = []

        self._audit(
            actor=actor,
            action="dashboard.patient_view",
            target_type="patient_record",
            target_id=patient.patient_id,
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "appointment_count": len(appt_out),
                "case_count": len(cases_out),
                "notes_count": len(notes_out),
                "file_count": len(files_out),
                "invoice_count": len(invoices_out),
            },
        )

        return PatientDetail(
            patient_id=patient.patient_id,
            display_name=patient.display_name,
            phone=patient.phone,
            email=patient.email,
            suburb=patient.suburb,
            age=_age_years(patient.date_of_birth, today),
            referrer_id=patient.referrer_id,
            last_appointment_date=patient.last_appointment_date,
            appointments=appt_out,
            documents=docs,
            cases=cases_out,
            treatment_notes=notes_out,
            patient_files=files_out,
            invoices=invoices_out,
        )
