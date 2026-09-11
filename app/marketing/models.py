"""
F1 — Marketing domain models.

Core abstractions for marketing lists, campaigns, templates, and recipients.
Frozen dataclasses with minimal required fields.

Never duplicate entire Nookal Patient objects — store patient_id only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Literal


class MarketingFilterType(str, Enum):
    """Supported patient filter dimensions."""

    SUBURB = "suburb"
    AGE_RANGE = "age_range"  # (min_age, max_age)
    LAST_APPOINTMENT_DATE_RANGE = "last_appointment_date_range"  # (start_date, end_date)
    REFERRER_ID = "referrer_id"
    PATIENT_IDS = "patient_ids"
    EXCLUDED_PATIENT_IDS = "excluded_patient_ids"


class CampaignStatus(str, Enum):
    """Campaign lifecycle states."""

    DRAFT = "draft"
    REVIEW = "review"
    APPROVED = "approved"
    QUEUED = "queued"
    SENDING = "sending"
    COMPLETED = "completed"
    PARTIAL = "partial"  # some succeeded, some failed
    FAILED = "failed"
    CANCELLED = "cancelled"


# Legal campaign state transitions.
_ALLOWED_TRANSITIONS: dict[CampaignStatus, set[CampaignStatus]] = {
    CampaignStatus.DRAFT: {CampaignStatus.REVIEW, CampaignStatus.CANCELLED},
    CampaignStatus.REVIEW: {CampaignStatus.APPROVED, CampaignStatus.DRAFT},
    CampaignStatus.APPROVED: {CampaignStatus.QUEUED, CampaignStatus.CANCELLED},
    CampaignStatus.QUEUED: {CampaignStatus.SENDING, CampaignStatus.CANCELLED},
    CampaignStatus.SENDING: {CampaignStatus.COMPLETED, CampaignStatus.PARTIAL, CampaignStatus.FAILED},
    CampaignStatus.COMPLETED: set(),
    CampaignStatus.PARTIAL: set(),
    CampaignStatus.FAILED: set(),
    CampaignStatus.CANCELLED: set(),
}


class CampaignRecipientStatus(str, Enum):
    """Recipient send status within a campaign."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"  # ineligible, suppressed, no consent


class CampaignRecipientExclusionReason(str, Enum):
    """Why a recipient was not sent to."""

    NO_CONSENT = "no_consent"
    SUPPRESSED = "suppressed"
    INVALID_EMAIL = "invalid_email"
    ALREADY_SENT = "already_sent"  # idempotency
    CAMPAIGN_NOT_APPROVED = "campaign_not_approved"
    KILL_SWITCH_ACTIVE = "kill_switch_active"
    RECIPIENT_NOT_FOUND = "recipient_not_found"


@dataclass(frozen=True)
class MarketingFilter:
    """
    Filter criteria for audience selection.

    Supports filters already available in project's Nookal abstraction.
    Do NOT invent undocumented fields.
    """

    filter_type: MarketingFilterType
    # value shape depends on filter_type:
    # - SUBURB: str
    # - AGE_RANGE: (int, int)
    # - LAST_APPOINTMENT_DATE_RANGE: (date, date)
    # - REFERRER_ID: str
    value: Any


@dataclass(frozen=True)
class MarketingList:
    """
    Marketing audience definition.

    Stores filter definition + metadata.
    List membership is DYNAMIC — recalculated on audience evaluation.
    Never assume stored membership is permanent.
    """

    id: str
    name: str
    description: str
    filter_definition: tuple[MarketingFilter, ...] = field(default_factory=tuple)
    created_by: str = "system"
    created_at: str = ""  # ISO8601
    updated_at: str = ""  # ISO8601
    status: str = "active"  # active, archived, deleted
    cached_candidate_count: int | None = None
    cached_eligible_count: int | None = None
    cached_suppressed_count: int | None = None
    cached_no_consent_count: int | None = None
    cached_invalid_email_count: int | None = None
    snapshot_at: str | None = None  # when counts were calculated, never authoritative

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CampaignTemplate:
    """
    Email campaign template with placeholders.

    Templates are client-approved deterministic content — no LLM generation required.
    LLM copy support is optional and deferred.
    """

    id: str
    name: str
    subject: str
    body: str
    html_body: str | None = None
    placeholders: frozenset[str] = field(default_factory=frozenset)
    version: str = "1.0"
    created_by: str = "system"
    created_at: str = ""  # ISO8601

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Campaign:
    """
    Marketing campaign lifecycle.

    Reuses ApprovalQueue for approval where needed.
    Status drives eligibility for state transitions.
    """

    id: str
    name: str
    subject: str
    template_id: str
    marketing_list_id: str
    created_by: str
    created_at: str  # ISO8601
    updated_at: str  # ISO8601
    status: CampaignStatus = CampaignStatus.DRAFT
    approval_task_id: str | None = None  # references ApprovalQueue.Task.id
    queued_at: str | None = None
    send_started_at: str | None = None
    completed_at: str | None = None
    # Counts summary — SNAPSHOT only, never authoritative
    candidate_count: int = 0
    eligible_count: int = 0
    sent_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    def can_transition_to(self, new_status: CampaignStatus) -> bool:
        """Check if transition from current status to new_status is legal."""
        return new_status in _ALLOWED_TRANSITIONS.get(self.status, set())


@dataclass(frozen=True)
class CampaignRecipient:
    """
    Per-recipient send state within a campaign.

    Tracks eligibility, send attempts, and outcomes.
    Must support idempotency checks and failure tracking.
    """

    id: str
    campaign_id: str
    patient_id: str
    recipient_email: str
    status: CampaignRecipientStatus = CampaignRecipientStatus.PENDING
    exclusion_reason: CampaignRecipientExclusionReason | None = None
    sent_at: str | None = None  # ISO8601
    attempt_count: int = 0
    last_attempt_at: str | None = None  # ISO8601
    failure_classification: str | None = None  # retryable, permanent, blocked, validation
    provider_reference: str | None = None  # opaque ref from FakeEmailAdapter
    idempotency_key: str = ""  # campaign_{id}_recipient_{patient_id}

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        if self.exclusion_reason:
            data["exclusion_reason"] = self.exclusion_reason.value
        return data
