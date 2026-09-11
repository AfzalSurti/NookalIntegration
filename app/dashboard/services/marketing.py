from __future__ import annotations

from typing import Any, Callable

from app.marketing import (
    AudienceBuilder,
    Campaign,
    CampaignStatus,
    MarketingFilter,
    MarketingFilterType,
    MarketingList,
)
from app.marketing.store import CampaignRecipientStore, CampaignStore, ConsentStore, MarketingListStore, SuppressionStore
from app.nookal_client import NookalClient

AuditFn = Callable[..., Any]


class MarketingService:
    def __init__(
        self,
        *,
        nookal: NookalClient,
        list_store: MarketingListStore,
        campaign_store: CampaignStore,
        recipient_store: CampaignRecipientStore,
        consent_store: ConsentStore,
        suppression_store: SuppressionStore,
        email_adapter: Any,
        audit: AuditFn,
    ) -> None:
        self._nookal = nookal
        self._list_store = list_store
        self._campaign_store = campaign_store
        self._recipient_store = recipient_store
        self._consent_store = consent_store
        self._suppression_store = suppression_store
        self._email_adapter = email_adapter
        self._audit = audit
        self._audience = AudienceBuilder(nookal, consent_store, suppression_store)

    def list_lists(self, *, actor: str, role: str, correlation_id: str) -> list[dict[str, Any]]:
        items = self._list_store.all()
        self._audit(
            actor=actor,
            action="dashboard.marketing_list_list",
            target_type="marketing_list",
            target_id="list",
            result="success",
            metadata={"correlation_id": correlation_id, "role": role, "count": len(items)},
        )
        return [self._serialize_list(item) for item in items]

    def create_list(self, *, actor: str, role: str, correlation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        description = str(body.get("description") or "")
        raw_filters = list(body.get("filter_definition") or [])
        if "patient_ids" in body and body["patient_ids"]:
            raw_filters.append({"filter_type": "patient_ids", "value": body["patient_ids"]})
        if "excluded_patient_ids" in body and body["excluded_patient_ids"]:
            raw_filters.append({"filter_type": "excluded_patient_ids", "value": body["excluded_patient_ids"]})

        filters: tuple[MarketingFilter, ...] = tuple(
            MarketingFilter(
                filter_type=MarketingFilterType(str(item["filter_type"])),
                value=item.get("value"),
            )
            for item in raw_filters
        )
        if not name:
            raise ValueError("name required")
        created = self._list_store.create(
            name=name,
            description=description,
            filter_definition=filters,
            created_by=actor,
        )
        self._audit(
            actor=actor,
            action="dashboard.marketing_list_create",
            target_type="marketing_list",
            target_id=created.id,
            result="success",
            metadata={"correlation_id": correlation_id, "role": role},
        )
        return self._serialize_list(created)

    def preview_audience(self, *, actor: str, role: str, correlation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        raw_filters = list(body.get("filter_definition") or [])
        if "suburb" in body and body["suburb"]:
            raw_filters.append({"filter_type": "suburb", "value": body["suburb"]})
        if "patient_ids" in body and body["patient_ids"]:
            raw_filters.append({"filter_type": "patient_ids", "value": body["patient_ids"]})
        if "excluded_patient_ids" in body and body["excluded_patient_ids"]:
            raw_filters.append({"filter_type": "excluded_patient_ids", "value": body["excluded_patient_ids"]})

        filters: tuple[MarketingFilter, ...] = tuple(
            MarketingFilter(
                filter_type=MarketingFilterType(str(item["filter_type"])),
                value=item.get("value"),
            )
            for item in raw_filters
        )
        temp_list = MarketingList(
            id="preview",
            name="preview",
            description="",
            filter_definition=filters,
        )
        aud_result = self._audience.build_audience(temp_list)
        candidates = self._audience._query_candidates(filters)
        
        patient_items = []
        for c in candidates[:100]:
            suppression = self._suppression_store.get(c.patient_id)
            is_suppressed = self._audience.suppression_policy.is_suppressed(suppression)
            consent = self._consent_store.get(c.patient_id)
            has_consent = self._audience.consent_policy.is_eligible(consent)
            has_email = bool(c.email and "@" in c.email)
            is_eligible = has_email and not is_suppressed and has_consent
            patient_items.append({
                "patient_id": c.patient_id,
                "name": c.first_name or c.display_name or f"Patient #{c.patient_id}",
                "suburb": c.suburb or "",
                "email": c.email or "",
                "phone": c.phone or "",
                "has_email": has_email,
                "has_consent": has_consent,
                "is_suppressed": is_suppressed,
                "is_eligible": is_eligible,
            })

        self._audit(
            actor=actor,
            action="dashboard.marketing_audience_preview",
            target_type="marketing_list",
            target_id="preview",
            result="success",
            metadata={"correlation_id": correlation_id, "role": role, "candidates": len(candidates)},
        )
        return {
            **aud_result.to_dict(),
            "patients": patient_items,
        }

    def list_campaigns(self, *, actor: str, role: str, correlation_id: str) -> list[dict[str, Any]]:
        items = self._campaign_store.all()
        self._audit(
            actor=actor,
            action="dashboard.marketing_campaign_list",
            target_type="campaign",
            target_id="list",
            result="success",
            metadata={"correlation_id": correlation_id, "role": role, "count": len(items)},
        )
        return [self._serialize_campaign(item) for item in items]

    def create_campaign(self, *, actor: str, role: str, correlation_id: str, body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        subject = str(body.get("subject") or "").strip()
        template_id = str(body.get("template_id") or "").strip()
        marketing_list_id = str(body.get("marketing_list_id") or "").strip()
        if not name or not subject or not template_id or not marketing_list_id:
            raise ValueError("name, subject, template_id and marketing_list_id are required")
        if self._list_store.get(marketing_list_id) is None:
            raise ValueError("marketing_list_id not found")
        created = self._campaign_store.create(
            name=name,
            subject=subject,
            template_id=template_id,
            marketing_list_id=marketing_list_id,
            created_by=actor,
        )
        self._audit(
            actor=actor,
            action="dashboard.marketing_campaign_create",
            target_type="campaign",
            target_id=created.id,
            result="success",
            metadata={"correlation_id": correlation_id, "role": role},
        )
        return self._serialize_campaign(created)

    def submit_campaign(self, *, campaign_id: str, actor: str, role: str, correlation_id: str) -> dict[str, Any]:
        campaign = self._campaign_store.get(campaign_id)
        if campaign is None:
            raise ValueError("campaign not found")
        updated = self._campaign_store.update_status(campaign.id, CampaignStatus.REVIEW)
        self._audit(
            actor=actor,
            action="dashboard.marketing_campaign_submit",
            target_type="campaign",
            target_id=updated.id,
            result="success",
            metadata={"correlation_id": correlation_id, "role": role, "status": updated.status.value},
        )
        return self._serialize_campaign(updated)

    def approve_campaign(self, *, campaign_id: str, actor: str, role: str, correlation_id: str) -> dict[str, Any]:
        campaign = self._campaign_store.get(campaign_id)
        if campaign is None:
            raise ValueError("campaign not found")
        updated = self._campaign_store.update_status(campaign.id, CampaignStatus.APPROVED)
        self._audit(
            actor=actor,
            action="dashboard.marketing_campaign_approve",
            target_type="campaign",
            target_id=updated.id,
            result="success",
            metadata={"correlation_id": correlation_id, "role": role, "status": updated.status.value},
        )
        return self._serialize_campaign(updated)

    def queue_campaign(self, *, campaign_id: str, actor: str, role: str, correlation_id: str) -> dict[str, Any]:
        campaign = self._campaign_store.get(campaign_id)
        if campaign is None:
            raise ValueError("campaign not found")
        updated = self._campaign_store.update_status(campaign.id, CampaignStatus.QUEUED)
        self._audit(
            actor=actor,
            action="dashboard.marketing_campaign_queue",
            target_type="campaign",
            target_id=updated.id,
            result="success",
            metadata={"correlation_id": correlation_id, "role": role, "status": updated.status.value},
        )
        return self._serialize_campaign(updated)

    def send_campaign(self, *, campaign_id: str, actor: str, role: str, correlation_id: str) -> dict[str, Any]:
        campaign = self._campaign_store.get(campaign_id)
        if campaign is None:
            raise ValueError("campaign not found")
        if campaign.status not in {CampaignStatus.QUEUED, CampaignStatus.APPROVED}:
            raise ValueError(f"campaign is not eligible to send: {campaign.status.value}")
        marketing_list = self._list_store.get(campaign.marketing_list_id)
        if marketing_list is None:
            raise ValueError("marketing_list_id not found")
        recipient_list = self._audience.get_eligible_recipients(marketing_list)
        for patient in recipient_list:
            key = f"campaign_{campaign.id}_recipient_{patient.patient_id}"
            if self._recipient_store.get_by_idempotency_key(campaign.id, key) is not None:
                continue
            self._recipient_store.create(
                campaign_id=campaign.id,
                patient_id=patient.patient_id,
                recipient_email=patient.email or "",
                idempotency_key=key,
            )
            self._email_adapter.send(
                patient.email or "",
                f"Campaign: {campaign.subject}",
                metadata={"campaign_id": campaign.id, "patient_id": patient.patient_id},
            )
        # Transition: QUEUED → SENDING
        sending = self._campaign_store.update_status(campaign.id, CampaignStatus.SENDING)
        # Transition: SENDING → COMPLETED
        updated = self._campaign_store.update_status(sending.id, CampaignStatus.COMPLETED)
        self._audit(
            actor=actor,
            action="dashboard.marketing_campaign_send",
            target_type="campaign",
            target_id=updated.id,
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "status": updated.status.value,
                "recipients": len(recipient_list),
            },
        )
        return self._serialize_campaign(updated)

    def _serialize_list(self, item: MarketingList) -> dict[str, Any]:
        return {
            "id": item.id,
            "name": item.name,
            "description": item.description,
            "filter_definition": [
                {"filter_type": f.filter_type.value, "value": f.value}
                for f in item.filter_definition
            ],
            "created_by": item.created_by,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "status": item.status,
        }

    def _serialize_campaign(self, item: Campaign) -> dict[str, Any]:
        return {
            "id": item.id,
            "name": item.name,
            "subject": item.subject,
            "template_id": item.template_id,
            "marketing_list_id": item.marketing_list_id,
            "created_by": item.created_by,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
            "status": item.status.value,
            "approval_task_id": item.approval_task_id,
            "queued_at": item.queued_at,
            "send_started_at": item.send_started_at,
            "completed_at": item.completed_at,
            "candidate_count": item.candidate_count,
            "eligible_count": item.eligible_count,
            "sent_count": item.sent_count,
            "failed_count": item.failed_count,
            "skipped_count": item.skipped_count,
        }
