from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.marketing.audience import AudienceBuilder
from app.marketing.fake_email_adapter import FakeEmailAdapter
from app.marketing.models import Campaign, CampaignRecipient, CampaignStatus, CampaignRecipientStatus
from app.marketing.store import CampaignRecipientStore, CampaignStore, ConsentStore, MarketingListStore, SuppressionStore
from app.nookal_client import NookalClient
from app.shared.exceptions import ApprovalError


@dataclass
class CampaignService:
    """Offline campaign service for create / review / queue / send flow."""

    nookal: NookalClient
    list_store: MarketingListStore
    consent_store: ConsentStore
    suppression_store: SuppressionStore
    email_adapter: FakeEmailAdapter
    campaign_store: CampaignStore | None = None
    recipient_store: CampaignRecipientStore | None = None
    audience_builder: AudienceBuilder | None = None

    def __post_init__(self) -> None:
        if self.campaign_store is None:
            self.campaign_store = CampaignStore()
        if self.recipient_store is None:
            self.recipient_store = CampaignRecipientStore()
        if self.audience_builder is None:
            self.audience_builder = AudienceBuilder(
                self.nookal,
                self.consent_store,
                self.suppression_store,
            )

    def get_campaign(self, campaign_id: str) -> Campaign:
        campaign = self.campaign_store.get(campaign_id)
        if campaign is None:
            raise ApprovalError(f"campaign not found: {campaign_id}")
        return campaign

    def create_campaign(
        self,
        *,
        name: str,
        subject: str,
        template_id: str,
        marketing_list_id: str,
        created_by: str = "system",
    ) -> Campaign:
        return self.campaign_store.create(
            name=name,
            subject=subject,
            template_id=template_id,
            marketing_list_id=marketing_list_id,
            created_by=created_by,
        )

    def submit_for_review(self, campaign_id: str) -> Campaign:
        campaign = self.get_campaign(campaign_id)
        updated = self.campaign_store.update_status(campaign.id, CampaignStatus.REVIEW)
        return updated

    def approve_campaign(self, campaign_id: str) -> Campaign:
        campaign = self.get_campaign(campaign_id)
        updated = self.campaign_store.update_status(campaign.id, CampaignStatus.APPROVED)
        return updated

    def queue_campaign(self, campaign_id: str) -> Campaign:
        campaign = self.get_campaign(campaign_id)
        updated = self.campaign_store.update_status(campaign.id, CampaignStatus.QUEUED)
        return updated

    def send_campaign(self, campaign_id: str) -> CampaignStatus:
        campaign = self.get_campaign(campaign_id)
        if campaign.status not in {CampaignStatus.QUEUED, CampaignStatus.APPROVED}:
            raise ApprovalError(f"campaign is not eligible to send: {campaign.status.value}")

        marketing_list = self.list_store.get(campaign.marketing_list_id)
        if marketing_list is None:
            raise ApprovalError(f"marketing list not found: {campaign.marketing_list_id}")

        recipients = self.audience_builder.get_eligible_recipients(marketing_list)
        if not recipients:
            self.campaign_store.update_status(campaign.id, CampaignStatus.COMPLETED)
            return CampaignStatus.COMPLETED

        for patient in recipients:
            idempotency_key = f"campaign_{campaign.id}_recipient_{patient.patient_id}"
            existing = self.recipient_store.get_by_idempotency_key(campaign.id, idempotency_key)
            if existing is not None:
                continue
            email = patient.email or ""
            self.recipient_store.create(
                campaign_id=campaign.id,
                patient_id=patient.patient_id,
                recipient_email=email,
                idempotency_key=idempotency_key,
            )
            self.email_adapter.send(email, f"Campaign: {campaign.subject}", metadata={"campaign_id": campaign.id, "patient_id": patient.patient_id})

        self.campaign_store.update_status(campaign.id, CampaignStatus.SENDING)
        self.campaign_store.update_status(campaign.id, CampaignStatus.COMPLETED)
        return CampaignStatus.COMPLETED
