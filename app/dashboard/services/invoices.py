"""Nookal Patient Invoice Service — strictly uses documented Nookal v2 invoice endpoints."""
from __future__ import annotations

from typing import Any, Callable

from app.dashboard.schemas import PatientInvoiceOut
from app.nookal_client import Invoice, InvoiceEntry, NookalClient
from app.shared.clock import Clock, SystemClock
from app.shared.exceptions import NookalNotFound


AuditFn = Callable[..., Any]


class InvoiceService:
    def __init__(
        self,
        *,
        nookal: NookalClient,
        audit: AuditFn,
        clock: Clock | None = None,
    ) -> None:
        self._nookal = nookal
        self._audit = audit
        self._clock = clock or SystemClock()

    def list_invoices(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        patient_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        status: str | None = None,
        page: int = 1,
        page_length: int = 50,
    ) -> list[Invoice]:
        try:
            all_invoices = self._nookal.get_invoices(
                patient_id=patient_id,
                expanded=1,
            )
        except NookalNotFound:
            all_invoices = []

        filtered = all_invoices
        if status:
            norm_status = status.strip().lower()
            if norm_status in ("paid", "completed", "settled"):
                filtered = [i for i in filtered if (i.status or "").strip().lower() in ("paid", "completed", "settled")]
            elif norm_status in ("unpaid", "pending", "outstanding", "due", "draft"):
                filtered = [i for i in filtered if (i.status or "").strip().lower() in ("unpaid", "pending", "outstanding", "due", "draft")]
            elif norm_status in ("partial", "partially paid", "part-paid"):
                filtered = [i for i in filtered if (i.status or "").strip().lower() in ("partial", "partially paid", "part-paid")]
            else:
                filtered = [i for i in filtered if (i.status or "").strip().lower() == norm_status]

        if date_from:
            filtered = [i for i in filtered if i.date and i.date >= date_from]
        if date_to:
            filtered = [i for i in filtered if i.date and i.date <= date_to]

        start = (max(1, page) - 1) * page_length
        invoices = list(filtered[start : start + page_length])

        for idx, inv in enumerate(invoices):
            needs_enrichment = (
                inv.total is None
                or inv.total == 0.0
                or not inv.date
                or not inv.status
                or inv.status == "—"
                or not inv.entries
            )
            if needs_enrichment:
                try:
                    full_inv = self._nookal.get_invoice(inv.invoice_id)
                    entries = full_inv.entries or inv.entries
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

        self._audit(
            actor=actor,
            action="dashboard.invoice_list",
            target_type="finance_record",
            target_id=patient_id or "all",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "count": len(invoices),
                "page": page,
                "status": status,
            },
        )
        return invoices

    def list_for_patient(
        self,
        patient_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> list[PatientInvoiceOut]:
        invs = self.list_invoices(
            actor=actor,
            role=role,
            correlation_id=correlation_id,
            patient_id=patient_id,
            page=1,
            page_length=200,
        )
        return [
            PatientInvoiceOut(
                invoice_id=inv.invoice_id,
                patient_id=inv.patient_id,
                date=inv.date,
                total=inv.total,
                status=inv.status,
                void=inv.void,
            )
            for inv in invs
        ]

    def get_invoice(
        self,
        invoice_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> Invoice:
        inv = self._nookal.get_invoice(invoice_id)
        entries = list(inv.entries)
        if not entries:
            try:
                entries = self._nookal.get_invoice_entries(invoice_id=invoice_id)
            except Exception:
                entries = []

        effective_total = inv.total
        if (effective_total is None or effective_total == 0.0) and entries:
            entry_sum = sum(
                (e.total if e.total is not None else ((e.price or 0.0) * (e.quantity or 1.0) + (e.tax or 0.0)))
                for e in entries
            )
            if entry_sum > 0:
                effective_total = float(entry_sum)

        norm_status = (inv.status or "").strip().lower()
        eff_bal = inv.balance
        eff_paid = inv.paid
        if norm_status in ("paid", "completed", "settled"):
            eff_bal = 0.0
            if effective_total is not None and effective_total > 0:
                eff_paid = effective_total
        elif norm_status in ("unpaid", "pending", "outstanding", "due", "draft"):
            if (eff_bal is None or eff_bal == 0.0) and effective_total is not None:
                eff_bal = effective_total
            if eff_paid is None:
                eff_paid = 0.0

        inv = Invoice(
            invoice_id=inv.invoice_id,
            patient_id=inv.patient_id,
            date=inv.date,
            total=effective_total,
            status=inv.status,
            void=inv.void,
            expanded=bool(entries),
            entries=entries,
            location_id=inv.location_id,
            practitioner_id=inv.practitioner_id,
            case_id=inv.case_id,
            reference=inv.reference,
            due_date=inv.due_date,
            balance=eff_bal,
            paid=eff_paid,
            tax=inv.tax,
            notes=inv.notes,
            raw=inv.raw,
        )

        self._audit(
            actor=actor,
            action="dashboard.invoice_view",
            target_type="finance_record",
            target_id=invoice_id,
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
            },
        )
        return inv

    def get_invoice_entries(
        self,
        invoice_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> list[InvoiceEntry]:
        entries = self._nookal.get_invoice_entries(invoice_id=invoice_id)
        self._audit(
            actor=actor,
            action="dashboard.invoice_entries_view",
            target_type="finance_record",
            target_id=invoice_id,
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "entry_count": len(entries),
            },
        )
        return entries

    def get_invoice_payments(
        self,
        invoice_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> list[Mapping[str, Any]]:
        payments: list[Mapping[str, Any]] = []
        if hasattr(self._nookal, "get_invoice_payments"):
            try:
                payments = self._nookal.get_invoice_payments(invoice_id=invoice_id)
            except Exception:
                payments = []
        return payments
