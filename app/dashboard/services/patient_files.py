"""Patient file and document service via official Nookal v2 API."""
from __future__ import annotations

import logging
from typing import Any, Callable

from app.dashboard.schemas import PatientFileOut
from app.nookal_client import NookalClient
from app.shared.clock import Clock, SystemClock
from app.shared.exceptions import (
    NookalFileNotFound,
    NookalInvalidFileId,
    NookalNotFound,
    NookalRequestFailed,
    NookalResponseInvalid,
    NookalUrlMissing,
    NookalValidationError,
)

logger = logging.getLogger(__name__)

AuditFn = Callable[..., Any]


class PatientFileService:
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

    def list_files(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        patient_id: str,
        page: int = 1,
        page_length: int = 50,
    ) -> list[PatientFileOut]:
        """List patient files uploaded to Nookal."""
        files_raw = []
        try:
            files_raw = self._nookal.get_patient_files(
                patient_id=patient_id,
                page=page,
                page_length=page_length,
            )
        except NookalNotFound:
            files_raw = []

        out = [
            PatientFileOut(
                file_id=f.file_id,
                patient_id=f.patient_id,
                name=f.name,
                file_type=f.file_type,
                date_added=f.date_added,
                size=f.size,
            )
            for f in files_raw
        ]

        self._audit(
            actor=actor,
            action="dashboard.patient_files_view",
            target_type="patient_record",
            target_id=patient_id,
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "count": len(out),
                "page": page,
            },
        )
        return out

    def get_file_url(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        patient_id: str,
        file_id: str,
    ) -> str:
        """
        Get temporary download/view URL for a patient file via Nookal /getFileUrl.
        Presigned URL query parameters (signatures/tokens) are NEVER logged or audited.
        """
        try:
            url = self._nookal.get_file_url(patient_id=patient_id, file_id=file_id)
        except Exception as exc:
            error_code = getattr(exc, "code", None)
            if not error_code:
                if isinstance(exc, (NookalInvalidFileId, NookalValidationError)):
                    error_code = "NOOKAL_INVALID_FILE_ID"
                elif isinstance(exc, (NookalFileNotFound, NookalNotFound)):
                    error_code = "NOOKAL_FILE_NOT_FOUND"
                elif isinstance(exc, NookalUrlMissing):
                    error_code = "NOOKAL_URL_MISSING"
                elif isinstance(exc, NookalResponseInvalid):
                    error_code = "NOOKAL_RESPONSE_INVALID"
                else:
                    error_code = "NOOKAL_REQUEST_FAILED"

            self._audit(
                actor=actor,
                action="dashboard.patient_file_download",
                target_type="patient_record",
                target_id=f"{patient_id}_{file_id}",
                result="failure",
                metadata={
                    "correlation_id": correlation_id,
                    "role": role,
                    "error_code": error_code,
                },
            )
            logger.warning(
                "Patient file URL retrieval failed for patient=%s file=%s [error_code=%s]: %s",
                patient_id,
                file_id,
                error_code,
                type(exc).__name__,
            )
            raise

        self._audit(
            actor=actor,
            action="dashboard.patient_file_download",
            target_type="patient_record",
            target_id=f"{patient_id}_{file_id}",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
            },
        )
        logger.info(
            "Successfully retrieved patient file URL for patient=%s file=%s",
            patient_id,
            file_id,
        )
        return url
