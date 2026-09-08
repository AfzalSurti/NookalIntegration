"""Document domain model — IDs and statuses, not clinical payloads in audit."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class DocumentType(str, Enum):
    CERTIFICATE = "certificate"
    REFERRAL_THANK_YOU = "referral_thank_you"
    TREATMENT_COMPLETION = "treatment_completion"
    PROGRESS_LETTER = "progress_letter"


class DocumentStatus(str, Enum):
    # Source + draft assembled, not yet approved for final output.
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RENDERED = "rendered"
    STORED = "stored"
    DELIVERED = "delivered"
    REJECTED = "rejected"
    FAILED = "failed"


@dataclass
class DocumentRecord:
    """
    Separates verified source data, draft text, approved text, and render meta.

    `source_facts` — verified non-clinical / administrative fields only.
    `draft_body` — LLM or template draft (never final until approved).
    `approved_body` — reviewer-accepted text used for PDF.
    """

    document_id: str
    document_type: DocumentType
    patient_id: str
    task_id: str
    template_id: str
    status: DocumentStatus
    created_at: str
    source_facts: dict[str, Any] = field(default_factory=dict)
    draft_body: str | None = None
    approved_body: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    rendered_at: str | None = None
    output_ref: str | None = None  # store key / path — not patient content
    content_sha256: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_safe_dict(self) -> dict[str, Any]:
        """Serializable metadata without body text — safe for logs/tests."""
        return {
            "document_id": self.document_id,
            "document_type": self.document_type.value,
            "patient_id": self.patient_id,
            "task_id": self.task_id,
            "template_id": self.template_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "rendered_at": self.rendered_at,
            "output_ref": self.output_ref,
            "content_sha256": self.content_sha256,
            "metadata": dict(self.metadata),
            "has_draft": bool(self.draft_body),
            "has_approved_body": bool(self.approved_body),
        }
