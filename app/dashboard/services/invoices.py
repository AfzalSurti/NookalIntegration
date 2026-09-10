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
        invoices = self._nookal.get_invoices(
            patient_id=patient_id,
            date_from=date_from,
            date_to=date_to,
            status=status,
            page=page,
            page_length=page_length,
        )
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
        entries = self._nookal.get_invoice_entries(invoice_id)
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
