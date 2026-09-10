"""Case service for clinical case management via official Nookal v2 API."""
from __future__ import annotations

import logging
from typing import Any, Callable

from app.dashboard.schemas import CaseOut
from app.nookal_client import NookalClient
from app.shared.clock import Clock, SystemClock
from app.shared.exceptions import NookalNotFound

logger = logging.getLogger(__name__)

AuditFn = Callable[..., Any]


class CaseService:
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

    def list_cases(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        patient_id: str | None = None,
        page: int = 1,
        page_length: int = 50,
    ) -> list[CaseOut]:
        """
        List cases from Nookal API.
        If patient_id is provided, calls get_cases(patient_id).
        If patient_id is omitted, calls get_all_cases().
        """
        cases_raw = []
        try:
            if patient_id:
                cases_raw = self._nookal.get_cases(
                    patient_id=patient_id,
                    page=page,
                    page_length=page_length,
                )
            elif hasattr(self._nookal, "get_all_cases"):
                cases_raw = self._nookal.get_all_cases(
                    page=page,
                    page_length=page_length,
                )
        except NookalNotFound:
            cases_raw = []

        out = [
            CaseOut(
                case_id=c.case_id,
                patient_id=c.patient_id,
                case_name=c.case_name,
                case_number=c.case_number,
                status=c.status,
                date_created=c.date_created,
                closed_date=c.closed_date,
            )
            for c in cases_raw
        ]

        self._audit(
            actor=actor,
            action="dashboard.case_list",
            target_type="patient_record",
            target_id=patient_id or "all",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "count": len(out),
                "page": page,
            },
        )
        return out
