"""Read-only session counting and practitioner-facing case flags."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.nookal_client import NookalClient
from app.shared import audit as audit_mod


@dataclass(frozen=True)
class CaseSessionCounter:
    case_id: str
    patient_id: str
    session_count: int = 0
    flag_raised: bool = False
    flag_raised_at: str | None = None
    acknowledged_by: str | None = None
    acknowledged_at: str | None = None
    counted_session_ids: tuple[str, ...] = ()


AuditFn = Callable[..., Any]


class CaseTracking:
    """Maintain local administrative counters from read-only appointment history."""

    def __init__(
        self,
        nookal: NookalClient,
        *,
        threshold: int = 5,
        store_path: Path | None = None,
        audit: AuditFn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        self._nookal = nookal
        self._threshold = threshold
        self._path = store_path
        self._audit = audit or audit_mod.log_event
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._cases = self._load()

    def refresh(self, *, case_id: str, patient_id: str | None = None) -> CaseSessionCounter:
        """Read appointments and update the counter without any external write."""
        resolved_patient_id = patient_id or case_id
        appointments = self._nookal.list_appointments(patient_id=resolved_patient_id)
        with self._lock:
            current = self._cases.get(case_id) or CaseSessionCounter(case_id, resolved_patient_id)
            counted = set(current.counted_session_ids)
            # The existing Appointment model and seed expose status="completed".
            completed_ids = {
                appointment.appointment_id
                for appointment in appointments
                if (appointment.status or "").casefold() == "completed"
            }
            counted.update(completed_ids)
            updated = replace(
                current,
                patient_id=resolved_patient_id,
                session_count=len(counted),
                counted_session_ids=tuple(sorted(counted)),
            )
            if updated.session_count >= self._threshold and not updated.flag_raised:
                # Administrative practitioner flag only: never message, write to Nookal, or automate against the patient.
                updated = replace(updated, flag_raised=True, flag_raised_at=self._now().isoformat())
                self._audit(
                    "case_tracking", "raise_session_flag", "patient_record", case_id, "success",
                    metadata={"threshold": self._threshold},
                )
            self._cases[case_id] = updated
            self._persist()
            return updated

    def get_case(self, case_id: str) -> CaseSessionCounter | None:
        """Read model for the practitioner dashboard; does not call Nookal."""
        with self._lock:
            return self._cases.get(case_id)

    def flagged_cases(self) -> list[CaseSessionCounter]:
        """Return flags suitable for a read-only practitioner dashboard view."""
        with self._lock:
            return sorted(
                (case for case in self._cases.values() if case.flag_raised),
                key=lambda case: case.flag_raised_at or "",
            )

    def acknowledge_flag(self, case_id: str, practitioner_id: str) -> CaseSessionCounter:
        with self._lock:
            current = self._cases.get(case_id)
            if current is None:
                raise KeyError(f"case not found: {case_id}")
            if not current.flag_raised:
                raise ValueError("case has no raised flag")
            updated = replace(
                current,
                acknowledged_by=practitioner_id,
                acknowledged_at=self._now().isoformat(),
            )
            self._cases[case_id] = updated
            self._persist()
        self._audit(
            "case_tracking", "acknowledge_session_flag", "patient_record", case_id, "success",
            metadata={},
        )
        return updated

    def _load(self) -> dict[str, CaseSessionCounter]:
        if self._path is None or not self._path.exists():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return {
                case_id: CaseSessionCounter(
                    **{**value, "counted_session_ids": tuple(value.get("counted_session_ids", ())) }
                )
                for case_id, value in raw.items()
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _persist(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps({case_id: asdict(case) for case_id, case in self._cases.items()}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, self._path)


__all__ = ["CaseSessionCounter", "CaseTracking"]