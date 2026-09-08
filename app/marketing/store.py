"""
F4-F5 — Store layer for marketing entities.

Thread-safe persistent storage for:
- MarketingLists (filter definitions)
- Campaigns (lifecycle + approval state)
- CampaignRecipients (send tracking)
- ConsentRecords (explicit patient consent)
- SuppressionRecords (opt-outs and blocks)

All stores use JSON append-only pattern or simple dict in-memory for Phase F.
File paths are configurable for testing.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.marketing.models import (
    Campaign,
    CampaignRecipient,
    CampaignStatus,
    MarketingFilter,
    MarketingFilterType,
    MarketingList,
)
from app.marketing.consent import ConsentRecord, ConsentState
from app.marketing.suppression import SuppressionRecord, SuppressionReason
from app.shared.config import get_settings
from app.shared.exceptions import ApprovalError


def _now() -> str:
    """ISO8601 timestamp."""
    return datetime.now().isoformat()


def _append_json_line(path: Path, obj: dict[str, Any]) -> None:
    """Append JSON object as a line to file."""
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


class MarketingListStore:
    """
    Persistent marketing list definitions.

    Stores filter definitions + metadata.
    List membership is recalculated on evaluation, never treated as permanent.
    """

    def __init__(self, store_path: Path | None = None) -> None:
        settings = get_settings()
        self._path = store_path or settings.paths.working_dir / "marketing_lists.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._lists: dict[str, MarketingList] = {}
        self._load()

    def _load(self) -> None:
        """Load all lists from disk."""
        if not self._path.exists():
            return
        with self._lock:
            for line in self._path.read_text().strip().split("\n"):
                if not line:
                    continue
                data = json.loads(line)
                list_id = data["id"]
                # Reconstruct MarketingFilter objects
                filters = []
                for filter_data in data.get("filter_definition", []):
                    filter_obj = MarketingFilter(
                        filter_type=MarketingFilterType(filter_data["filter_type"]),
                        value=filter_data["value"],
                    )
                    filters.append(filter_obj)
                data["filter_definition"] = tuple(filters)
                # Reconstruct MarketingList from dict
                self._lists[list_id] = MarketingList(**data)

    def create(
        self,
        *,
        name: str,
        description: str,
        filter_definition: tuple = (),
        created_by: str = "system",
    ) -> MarketingList:
        """Create and store a new marketing list."""
        list_id = f"mlist-{uuid.uuid4().hex[:12]}"
        now = _now()
        marketing_list = MarketingList(
            id=list_id,
            name=name,
            description=description,
            filter_definition=filter_definition,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._lists[list_id] = marketing_list
            _append_json_line(self._path, marketing_list.to_dict())
        return marketing_list

    def get(self, list_id: str) -> MarketingList | None:
        """Retrieve a list by ID."""
        with self._lock:
            return self._lists.get(list_id)

    def all(self) -> list[MarketingList]:
        """All lists."""
        with self._lock:
            return list(self._lists.values())


class CampaignStore:
    """Persistent campaign lifecycle tracking."""

    def __init__(self, store_path: Path | None = None) -> None:
        settings = get_settings()
        self._path = store_path or settings.paths.working_dir / "campaigns.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._campaigns: dict[str, Campaign] = {}
        self._load()

    def _load(self) -> None:
        """Load all campaigns from disk."""
        if not self._path.exists():
            return
        with self._lock:
            for line in self._path.read_text().strip().split("\n"):
                if not line:
                    continue
                data = json.loads(line)
                campaign_id = data["id"]
                # Reconstruct Campaign from dict
                data["status"] = CampaignStatus(data["status"])
                self._campaigns[campaign_id] = Campaign(**data)

    def create(
        self,
        *,
        name: str,
        subject: str,
        template_id: str,
        marketing_list_id: str,
        created_by: str = "system",
    ) -> Campaign:
        """Create and store a new campaign."""
        campaign_id = f"camp-{uuid.uuid4().hex[:12]}"
        now = _now()
        campaign = Campaign(
            id=campaign_id,
            name=name,
            subject=subject,
            template_id=template_id,
            marketing_list_id=marketing_list_id,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        with self._lock:
            self._campaigns[campaign_id] = campaign
            _append_json_line(self._path, campaign.to_dict())
        return campaign

    def get(self, campaign_id: str) -> Campaign | None:
        """Retrieve a campaign by ID."""
        with self._lock:
            return self._campaigns.get(campaign_id)

    def all(self) -> list[Campaign]:
        """All campaigns."""
        with self._lock:
            return list(self._campaigns.values())

    def update_status(self, campaign_id: str, new_status: CampaignStatus) -> Campaign:
        """Update campaign status with transition validation."""
        with self._lock:
            campaign = self._campaigns.get(campaign_id)
            if not campaign:
                raise ApprovalError(f"campaign not found: {campaign_id}")
            if not campaign.can_transition_to(new_status):
                raise ApprovalError(
                    f"invalid transition: {campaign.status.value} → {new_status.value}"
                )
            queued_at = campaign.queued_at
            if new_status == CampaignStatus.QUEUED and queued_at is None:
                queued_at = _now()

            send_started_at = campaign.send_started_at
            if new_status == CampaignStatus.SENDING and send_started_at is None:
                send_started_at = _now()

            completed_at = campaign.completed_at
            if new_status in {CampaignStatus.COMPLETED, CampaignStatus.PARTIAL, CampaignStatus.FAILED} and completed_at is None:
                completed_at = _now()

            # Create new immutable campaign with updated status
            updated = Campaign(
                id=campaign.id,
                name=campaign.name,
                subject=campaign.subject,
                template_id=campaign.template_id,
                marketing_list_id=campaign.marketing_list_id,
                created_by=campaign.created_by,
                created_at=campaign.created_at,
                updated_at=_now(),
                status=new_status,
                approval_task_id=campaign.approval_task_id,
                queued_at=queued_at,
                send_started_at=send_started_at,
                completed_at=completed_at,
                candidate_count=campaign.candidate_count,
                eligible_count=campaign.eligible_count,
                sent_count=campaign.sent_count,
                failed_count=campaign.failed_count,
                skipped_count=campaign.skipped_count,
            )
            self._campaigns[campaign_id] = updated
            _append_json_line(self._path, updated.to_dict())
            return updated


class CampaignRecipientStore:
    """Track per-recipient send state within campaigns."""

    def __init__(self, store_path: Path | None = None) -> None:
        settings = get_settings()
        self._path = store_path or settings.paths.working_dir / "campaign_recipients.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._recipients: dict[str, CampaignRecipient] = {}  # by id
        self._campaign_recipients: dict[str, list[str]] = {}  # campaign_id → [recipient_ids]
        self._load()

    def _load(self) -> None:
        """Load all recipients from disk."""
        if not self._path.exists():
            return
        with self._lock:
            for line in self._path.read_text().strip().split("\n"):
                if not line:
                    continue
                data = json.loads(line)
                recipient_id = data["id"]
                campaign_id = data["campaign_id"]
                # Reconstruct CampaignRecipient from dict
                recipient = CampaignRecipient(**data)
                self._recipients[recipient_id] = recipient
                if campaign_id not in self._campaign_recipients:
                    self._campaign_recipients[campaign_id] = []
                self._campaign_recipients[campaign_id].append(recipient_id)

    def create(
        self,
        *,
        campaign_id: str,
        patient_id: str,
        recipient_email: str,
        idempotency_key: str,
    ) -> CampaignRecipient:
        """Create and store a new campaign recipient."""
        recipient_id = f"recip-{uuid.uuid4().hex[:12]}"
        recipient = CampaignRecipient(
            id=recipient_id,
            campaign_id=campaign_id,
            patient_id=patient_id,
            recipient_email=recipient_email,
            idempotency_key=idempotency_key,
        )
        with self._lock:
            self._recipients[recipient_id] = recipient
            if campaign_id not in self._campaign_recipients:
                self._campaign_recipients[campaign_id] = []
            self._campaign_recipients[campaign_id].append(recipient_id)
            _append_json_line(self._path, recipient.to_dict())
        return recipient

    def get(self, recipient_id: str) -> CampaignRecipient | None:
        """Retrieve a recipient by ID."""
        with self._lock:
            return self._recipients.get(recipient_id)

    def get_by_campaign(self, campaign_id: str) -> list[CampaignRecipient]:
        """Get all recipients for a campaign."""
        with self._lock:
            recipient_ids = self._campaign_recipients.get(campaign_id, [])
            return [self._recipients[rid] for rid in recipient_ids if rid in self._recipients]

    def get_by_idempotency_key(
        self, campaign_id: str, idempotency_key: str
    ) -> CampaignRecipient | None:
        """Find recipient by idempotency key (for duplicate detection)."""
        with self._lock:
            for recipient_id in self._campaign_recipients.get(campaign_id, []):
                recipient = self._recipients.get(recipient_id)
                if recipient and recipient.idempotency_key == idempotency_key:
                    return recipient
        return None


class ConsentStore:
    """Track explicit patient consent for marketing."""

    def __init__(self, store_path: Path | None = None) -> None:
        settings = get_settings()
        self._path = store_path or settings.paths.working_dir / "consent.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._consent: dict[str, ConsentRecord] = {}  # patient_id → latest ConsentRecord
        self._load()

    def _load(self) -> None:
        """Load all consent records from disk."""
        if not self._path.exists():
            return
        with self._lock:
            for line in self._path.read_text().strip().split("\n"):
                if not line:
                    continue
                data = json.loads(line)
                patient_id = data["patient_id"]
                # Reconstruct ConsentRecord from dict
                data["state"] = ConsentState(data["state"])
                record = ConsentRecord(**data)
                # Always keep latest (by changed_at)
                existing = self._consent.get(patient_id)
                if not existing or record.changed_at > existing.changed_at:
                    self._consent[patient_id] = record

    def grant(self, patient_id: str, changed_by: str = "system", notes: str = "") -> ConsentRecord:
        """Grant marketing consent."""
        record = ConsentRecord(
            patient_id=patient_id,
            state=ConsentState.GRANTED,
            changed_at=_now(),
            changed_by=changed_by,
            notes=notes,
        )
        with self._lock:
            self._consent[patient_id] = record
            _append_json_line(self._path, record.to_dict())
        return record

    def revoke(self, patient_id: str, changed_by: str = "system", notes: str = "") -> ConsentRecord:
        """Revoke marketing consent."""
        record = ConsentRecord(
            patient_id=patient_id,
            state=ConsentState.REVOKED,
            changed_at=_now(),
            changed_by=changed_by,
            notes=notes,
        )
        with self._lock:
            self._consent[patient_id] = record
            _append_json_line(self._path, record.to_dict())
        return record

    def get(self, patient_id: str) -> ConsentRecord | None:
        """Get latest consent record for patient."""
        with self._lock:
            return self._consent.get(patient_id)


class SuppressionStore:
    """Track suppression/opt-outs."""

    def __init__(self, store_path: Path | None = None) -> None:
        settings = get_settings()
        self._path = store_path or settings.paths.working_dir / "suppression.jsonl"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._suppression: dict[str, SuppressionRecord] = {}  # patient_id → latest SuppressionRecord
        self._load()

    def _load(self) -> None:
        """Load all suppression records from disk."""
        if not self._path.exists():
            return
        with self._lock:
            for line in self._path.read_text().strip().split("\n"):
                if not line:
                    continue
                data = json.loads(line)
                patient_id = data["patient_id"]
                # Reconstruct SuppressionRecord from dict
                data["reason"] = SuppressionReason(data["reason"])
                record = SuppressionRecord(**data)
                # Always keep latest (by suppressed_at)
                existing = self._suppression.get(patient_id)
                if not existing or record.suppressed_at > existing.suppressed_at:
                    self._suppression[patient_id] = record

    def suppress(
        self,
        patient_id: str,
        reason: SuppressionReason,
        suppressed_by: str = "system",
        notes: str = "",
        expires_at: str | None = None,
    ) -> SuppressionRecord:
        """Suppress a patient from marketing."""
        record = SuppressionRecord(
            patient_id=patient_id,
            reason=reason,
            suppressed_at=_now(),
            suppressed_by=suppressed_by,
            notes=notes,
            expires_at=expires_at,
        )
        with self._lock:
            self._suppression[patient_id] = record
            _append_json_line(self._path, record.to_dict())
        return record

    def unsuppress(self, patient_id: str) -> None:
        """Remove suppression for a patient (e.g., reactivate after temporary block)."""
        with self._lock:
            self._suppression.pop(patient_id, None)

    def get(self, patient_id: str) -> SuppressionRecord | None:
        """Get latest suppression record for patient."""
        with self._lock:
            return self._suppression.get(patient_id)
