"""Nookal Patient Invoice Service — strictly uses documented Nookal v2 invoice endpoints."""
from __future__ import annotations

from typing import Any, Callable

from app.nookal_client import Invoice, InvoiceEntry, NookalClient
from app.shared.clock import Clock, SystemClock


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
        all_invoices = self._nookal.get_invoices(
            patient_id=patient_id,
            expanded=1,
        )
        filtered = all_invoices
        if status:
            norm_status = status.lower()
            filtered = [i for i in filtered if (i.status or "").lower() == norm_status]
        if date_from:
            filtered = [i for i in filtered if i.date and i.date >= date_from]
        if date_to:
            filtered = [i for i in filtered if i.date and i.date <= date_to]

        start = (max(1, page) - 1) * page_length
        invoices = filtered[start : start + page_length]

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

    def get_invoice(
        self,
        invoice_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> Invoice:
        inv = self._nookal.get_invoice(invoice_id)
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
