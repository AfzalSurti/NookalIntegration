"""Safe intake of uploaded documents for human review."""
from __future__ import annotations

import hashlib
import mimetypes
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol

from app.nookal_client import NookalClient, PatientRef
from app.shared import audit as audit_mod


SourceChannel = Literal["upload", "email", "whatsapp", "sms", "other"]
PatientMatchStatus = Literal["matched", "ambiguous", "unmatched"]
DocumentStatus = Literal["draft"]
FilingStatus = Literal["not_filed", "filed", "failed"]
DocumentType = Literal[
    "referral",
    "discharge_summary",
    "progress_report",
    "certificate",
    "invoice",
    "clinical_note",
    "other",
    "unknown",
]


@dataclass(frozen=True)
class UploadedFile:
    """Bytes received from a caller; the original is never changed or deleted."""

    filename: str
    content: bytes
    mime_type: str | None = None


@dataclass(frozen=True)
class PatientCandidate:
    patient_id: str
    confidence: float
    display_name: str | None = None


@dataclass(frozen=True)
class PatientMatch:
    status: PatientMatchStatus
    patient_id: str | None = None
    confidence: float = 0.0
    candidates: tuple[PatientCandidate, ...] = ()


@dataclass(frozen=True)
class DocumentRecord:
    """An intake record. It is always a draft until a separate human gate acts."""

    document_id: str
    source_channel: SourceChannel
    original_filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    preserved_path: str
    patient_match: PatientMatch
    document_type: DocumentType
    document_type_confidence: float | None
    classification_source: Literal["rule", "llm", "none"]
    status: DocumentStatus = "draft"
    created_at: str = ""
    filed_to: tuple[str, ...] = ()
    filed_at: str | None = None
    filed_by: str | None = None
    filing_status: FilingStatus = "not_filed"
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentClassifier(Protocol):
    """Optional local-LLM adapter; it must return an advisory classification only."""

    def classify_document(self, *, filename: str, mime_type: str) -> tuple[DocumentType, float]:
        ...


AuditFn = Callable[..., Any]


class DocumentIntake:
    """Preserve, classify, and match an upload without performing writes or sends."""

    def __init__(
        self,
        nookal: NookalClient,
        storage_root: Path,
        *,
        match_threshold: float = 0.9,
        classifier: DocumentClassifier | None = None,
        audit: AuditFn | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0.0 <= match_threshold <= 1.0:
            raise ValueError("match_threshold must be between 0 and 1")
        self._nookal = nookal
        self._storage_root = storage_root
        self._match_threshold = match_threshold
        self._classifier = classifier
        self._audit = audit or audit_mod.log_event
        self._now = now or (lambda: datetime.now(timezone.utc))

    def intake(
        self,
        uploaded: UploadedFile,
        source_channel: SourceChannel,
        *,
        patient_name: str | None = None,
        patient_date_of_birth: date | None = None,
    ) -> DocumentRecord:
        document_id = f"intake_{uuid.uuid4().hex}"
        try:
            preserved_path = self._preserve(document_id, uploaded)
            mime_type = uploaded.mime_type or mimetypes.guess_type(uploaded.filename)[0] or "application/octet-stream"
            patient_match = self._match(patient_name, patient_date_of_birth)
            document_type, confidence, classification_source = self._classify(
                uploaded.filename, mime_type
            )
            record = DocumentRecord(
                document_id=document_id,
                source_channel=source_channel,
                original_filename=uploaded.filename,
                mime_type=mime_type,
                size_bytes=len(uploaded.content),
                sha256=hashlib.sha256(uploaded.content).hexdigest(),
                preserved_path=str(preserved_path),
                patient_match=patient_match,
                document_type=document_type,
                document_type_confidence=confidence,
                classification_source=classification_source,
                created_at=self._now().isoformat(),
            )
            self._audit(
                "doc_intake", "intake", "document", document_id, "success",
                metadata={
                    "source_channel": source_channel,
                    "size_bytes": len(uploaded.content),
                    "match_status": patient_match.status,
                    "candidate_count": len(patient_match.candidates),
                    "classification_source": classification_source,
                    "document_type": document_type,
                },
            )
            return record
        except Exception:
            self._audit("doc_intake", "intake", "document", document_id, "failure")
            raise

    def _preserve(self, document_id: str, uploaded: UploadedFile) -> Path:
        suffix = Path(uploaded.filename).suffix.lower()
        target_dir = self._storage_root / "documents"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{document_id}{suffix}"
        with target.open("xb") as stream:
            stream.write(uploaded.content)
        return target

    def _match(self, name: str | None, date_of_birth: date | None) -> PatientMatch:
        search_kwargs: dict[str, int] = {}
        if date_of_birth is not None:
            today = date.today()
            age = today.year - date_of_birth.year - (
                (today.month, today.day) < (date_of_birth.month, date_of_birth.day)
            )
            search_kwargs = {"age_min": age, "age_max": age}
        candidates = self._nookal.search_patients(**search_kwargs)
        scored = sorted(
            (self._score(patient, name, date_of_birth) for patient in candidates),
            key=lambda item: (-item[0], item[1].patient_id),
        )
        if not scored or (name is None and date_of_birth is None):
            return PatientMatch(
                status="unmatched" if not scored else "ambiguous",
                candidates=tuple(self._candidate(score, patient) for score, patient in scored),
            )
        best_score, best_patient = scored[0]
        tied = len(scored) > 1 and scored[1][0] == best_score
        if best_score >= self._match_threshold and not tied:
            return PatientMatch("matched", best_patient.patient_id, best_score)
        return PatientMatch(
            "ambiguous",
            confidence=best_score,
            candidates=tuple(self._candidate(score, patient) for score, patient in scored),
        )

    @staticmethod
    def _score(patient: PatientRef, name: str | None, date_of_birth: date | None) -> tuple[float, PatientRef]:
        score = 0.0
        if name and patient.display_name and _normalise(name) == _normalise(patient.display_name):
            score += 0.7
        if date_of_birth and patient.date_of_birth == date_of_birth:
            score += 0.3
        return score, patient

    @staticmethod
    def _candidate(score: float, patient: PatientRef) -> PatientCandidate:
        return PatientCandidate(patient.patient_id, score, patient.display_name)

    def _classify(self, filename: str, mime_type: str) -> tuple[DocumentType, float | None, Literal["rule", "llm", "none"]]:
        rule = _rule_classify(filename, mime_type)
        if rule is not None:
            return rule, 1.0, "rule"
        if self._classifier is not None:
            document_type, confidence = self._classifier.classify_document(
                filename=filename, mime_type=mime_type
            )
            return document_type, confidence, "llm"
        return "unknown", None, "none"


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def _rule_classify(filename: str, mime_type: str) -> DocumentType | None:
    text = f"{filename.casefold()} {mime_type.casefold()}"
    rules: tuple[tuple[tuple[str, ...], DocumentType], ...] = (
        (("referral", "refer"), "referral"),
        (("discharge",), "discharge_summary"),
        (("progress",), "progress_report"),
        (("certificate",), "certificate"),
        (("invoice", "receipt"), "invoice"),
        (("clinical", "consult", "soap"), "clinical_note"),
    )
    for needles, document_type in rules:
        if any(needle in text for needle in needles):
            return document_type
    if mime_type.casefold() == "application/pdf":
        return "other"
    return None


__all__ = [
    "DocumentClassifier",
    "DocumentIntake",
    "DocumentRecord",
    "FilingStatus",
    "PatientCandidate",
    "PatientMatch",
    "UploadedFile",
]