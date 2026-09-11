"""Patient search and detail — uses injected NookalClient only."""
from __future__ import annotations

import logging
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
from app.nookal_client import Invoice, NookalClient, PatientRef
from app.shared.clock import Clock, SystemClock
from app.shared.exceptions import NookalNotFound

logger = logging.getLogger(__name__)

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
        suburb: str | list[str] | None = None,
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
        try:
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
        except Exception as exc:
            logger.error("Patient search failed: %s", exc)
            results = []
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
                "has_suburb": bool(suburb),
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
        cases_error: str | None = None
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
            except NookalNotFound:
                cases_out = []
            except Exception as exc:
                logger.warning("Failed to fetch cases for patient %s: %s", patient_id, type(exc).__name__)
                cases_error = "Unable to load clinical cases from Nookal at this time."

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
            except NookalNotFound:
                notes_out = []
            except Exception as exc:
                logger.warning("Failed to fetch treatment notes for patient %s: %s", patient_id, type(exc).__name__)
                notes_out = []

        files_out: list[PatientFileOut] = []
        files_error: str | None = None
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
            except NookalNotFound:
                files_out = []
            except Exception as exc:
                logger.warning("Failed to fetch patient files for patient %s: %s", patient_id, type(exc).__name__)
                files_error = "Unable to load patient files from Nookal at this time."

        invoices_out: list[PatientInvoiceOut] = []
        invoices_error: str | None = None
        if hasattr(self._nookal, "get_invoices"):
            try:
                invoices = self._nookal.get_invoices(patient_id=patient_id, expanded=1)
                for idx, inv in enumerate(invoices):
                    calc_total = inv.total
                    entries = inv.entries
                    needs_lookup = (
                        calc_total is None
                        or calc_total == 0.0
                        or not inv.date
                        or not inv.status
                        or inv.status == "—"
                        or not entries
                    )
                    if needs_lookup:
                        try:
                            full_inv = self._nookal.get_invoice(inv.invoice_id)
                            entries = full_inv.entries or entries
                            if not entries:
                                try:
                                    entries = self._nookal.get_invoice_entries(invoice_id=inv.invoice_id)
                                except Exception:
                                    entries = []
                            calc_total = full_inv.total if (full_inv.total is not None and full_inv.total > 0) else inv.total
                            if (calc_total is None or calc_total == 0.0) and entries:
                                entry_sum = sum(
                                    (e.total if e.total is not None else ((e.price or 0.0) * (e.quantity or 1.0) + (e.tax or 0.0)))
                                    for e in entries
                                )
                                if entry_sum > 0:
                                    calc_total = float(entry_sum)

                            final_status = full_inv.status if (full_inv.status and full_inv.status != "—") else inv.status
                            eff_bal = full_inv.balance if full_inv.balance is not None else inv.balance
                            eff_paid = full_inv.paid if full_inv.paid is not None else inv.paid
                            if (final_status or "").strip().lower() in ("paid", "completed", "settled"):
                                eff_bal = 0.0
                                if calc_total is not None and calc_total > 0:
                                    eff_paid = calc_total
                            elif (final_status or "").strip().lower() in ("unpaid", "pending", "outstanding", "due", "draft"):
                                if (eff_bal is None or eff_bal == 0.0) and calc_total is not None:
                                    eff_bal = calc_total
                                if eff_paid is None:
                                    eff_paid = 0.0

                            invoices[idx] = Invoice(
                                invoice_id=inv.invoice_id,
                                patient_id=inv.patient_id or full_inv.patient_id,
                                date=full_inv.date or inv.date,
                                total=calc_total,
                                status=final_status,
                                void=full_inv.void if full_inv.void is not None else inv.void,
                                expanded=bool(entries) or full_inv.expanded or inv.expanded,
                                entries=entries,
                                location_id=full_inv.location_id or inv.location_id,
                                practitioner_id=full_inv.practitioner_id or inv.practitioner_id,
                                case_id=full_inv.case_id or inv.case_id,
                                reference=full_inv.reference or inv.reference,
                                due_date=full_inv.due_date or inv.due_date,
                                balance=eff_bal,
                                paid=eff_paid,
                                tax=full_inv.tax if full_inv.tax is not None else inv.tax,
                                notes=full_inv.notes or inv.notes,
                                raw={**inv.raw, **full_inv.raw},
                            )
                        except Exception:
                            pass
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
            except NookalNotFound:
                invoices_out = []
            except Exception as exc:
                logger.warning("Failed to fetch invoices for patient %s: %s", patient_id, type(exc).__name__)
                invoices_error = "Unable to load invoices from Nookal at this time."

        referrer_name = None
        if patient.referrer_id and hasattr(self._nookal, "list_referrers"):
            try:
                refs = self._nookal.list_referrers()
                for r in refs:
                    if str(r.referrer_id) == str(patient.referrer_id):
                        referrer_name = r.name
                        break
            except Exception:
                referrer_name = None

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
            },
        )

        return PatientDetail(
            patient_id=patient.patient_id,
            display_name=patient.display_name,
            first_name=patient.first_name,
            middle_name=patient.middle_name,
            last_name=patient.last_name,
            nickname=patient.nickname,
            phone=patient.phone,
            email=patient.email,
            date_of_birth=patient.date_of_birth,
            gender=patient.gender,
            suburb=patient.suburb,
            address=patient.address,
            postal_address=patient.postal_address,
            online_code=patient.online_code,
            deceased=patient.deceased,
            age=_age_years(patient.date_of_birth, today),
            referrer_id=patient.referrer_id,
            referrer_name=referrer_name,
            date_created=patient.date_created,
            date_modified=patient.date_modified,
            last_appointment_date=patient.last_appointment_date,
            appointments=appt_out,
            documents=docs,
            cases=cases_out,
            treatment_notes=notes_out,
            patient_files=files_out,
            invoices=invoices_out,
            cases_error=cases_error,
            files_error=files_error,
            invoices_error=invoices_error,
            raw=dict(patient.raw) if patient.raw else {},
        )
