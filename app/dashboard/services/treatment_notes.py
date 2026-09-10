"""Nookal Treatment Note Service — read access to treatment notes via documented Nookal v2 API."""
from __future__ import annotations

import logging
from typing import Any, Callable

from app.nookal_client import NookalClient, TreatmentNote
from app.shared.clock import Clock, SystemClock
from app.shared.exceptions import NookalNotFound


logger = logging.getLogger(__name__)

AuditFn = Callable[..., Any]


class TreatmentNoteService:
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

    def list_for_patient(
        self,
        patient_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        page: int = 1,
        page_length: int = 100,
    ) -> list[TreatmentNote]:
        """List treatment notes for a patient."""
        try:
            notes = self._nookal.get_treatment_notes(
                patient_id,
                page=page,
                page_length=page_length,
            )
        except NookalNotFound:
            notes = []

        self._audit(
            actor=actor,
            action="dashboard.treatment_notes_list",
            target_type="patient_record",
            target_id=patient_id,
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "count": len(notes),
            },
        )
        return notes

    def get_note(
        self,
        patient_id: str,
        note_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> TreatmentNote | None:
        """
        Get a specific treatment note by ID.

        Since Nookal doesn't have a single-note endpoint, we fetch all notes
        for the patient and filter by note_id.
        """
        try:
            notes = self._nookal.get_treatment_notes(
                patient_id,
                page=1,
                page_length=500,
            )
        except NookalNotFound:
            notes = []

        found = None
        for note in notes:
            if note.note_id == note_id:
                found = note
                break

        if found is None:
            self._audit(
                actor=actor,
                action="dashboard.treatment_note_view",
                target_type="patient_record",
                target_id=f"{patient_id}_{note_id}",
                result="failure",
                metadata={
                    "correlation_id": correlation_id,
                    "role": role,
                    "error_code": "NOTE_NOT_FOUND",
                },
            )
            logger.warning(
                "Treatment note not found for patient=%s note=%s",
                patient_id,
                note_id,
            )
            return None

        self._audit(
            actor=actor,
            action="dashboard.treatment_note_view",
            target_type="patient_record",
            target_id=f"{patient_id}_{note_id}",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
            },
        )
        logger.info(
            "Successfully retrieved treatment note for patient=%s note=%s",
            patient_id,
            note_id,
        )
        return found
