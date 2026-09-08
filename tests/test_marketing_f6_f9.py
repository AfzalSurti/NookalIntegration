from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.marketing import CampaignStatus, MarketingFilter, MarketingFilterType, MarketingList
from app.marketing.audience import AudienceBuilder
from app.marketing.fake_email_adapter import FakeEmailAdapter
from app.marketing.service import CampaignService
from app.marketing.store import ConsentStore, MarketingListStore, SuppressionStore
from app.nookal_client import MockNookalClient, PatientRef
from app.shared.exceptions import ApprovalError


def test_campaign_service_approval_and_send_flow(tmp_path: Path) -> None:
    nookal = MockNookalClient()
    for patient_id, email in [("pat-1", "pat1@example.com"), ("pat-2", "pat2@example.com")]:
        nookal.seed_patient(
            PatientRef(
                patient_id=patient_id,
                email=email,
                suburb="Redfern",
                date_of_birth=date(1990, 1, 1),
            )
        )

    list_store = MarketingListStore(tmp_path / "lists.jsonl")
    consent_store = ConsentStore(tmp_path / "consent.jsonl")
    suppression_store = SuppressionStore(tmp_path / "suppression.jsonl")
    adapter = FakeEmailAdapter()

    for patient_id in ["pat-1", "pat-2"]:
        consent_store.grant(patient_id)

    marketing_list = list_store.create(
        name="Redfern",
        description="Redfern list",
        filter_definition=(MarketingFilter(filter_type=MarketingFilterType.SUBURB, value="Redfern"),),
        created_by="admin",
    )
    service = CampaignService(
        nookal=nookal,
        list_store=list_store,
        consent_store=consent_store,
        suppression_store=suppression_store,
        email_adapter=adapter,
    )

    campaign = service.create_campaign(
        name="Spring campaign",
        subject="Spring update",
        template_id="welcome_email",
        marketing_list_id=marketing_list.id,
        created_by="admin",
    )

    assert campaign.status == CampaignStatus.DRAFT
    service.submit_for_review(campaign.id)
    service.approve_campaign(campaign.id)
    service.queue_campaign(campaign.id)

    result = service.send_campaign(campaign.id)
    assert result == CampaignStatus.COMPLETED
    assert adapter.sent_count == 2
    assert service.get_campaign(campaign.id).status == CampaignStatus.COMPLETED


def test_campaign_service_rejects_send_when_not_approved(tmp_path: Path) -> None:
    nookal = MockNookalClient()
    nookal.seed_patient(
        PatientRef(
            patient_id="pat-1",
            email="pat1@example.com",
            suburb="Redfern",
            date_of_birth=date(1990, 1, 1),
        )
    )

    list_store = MarketingListStore(tmp_path / "lists.jsonl")
    consent_store = ConsentStore(tmp_path / "consent.jsonl")
    suppression_store = SuppressionStore(tmp_path / "suppression.jsonl")
    adapter = FakeEmailAdapter()
    consent_store.grant("pat-1")

    marketing_list = list_store.create(
        name="Redfern",
        description="Redfern list",
        filter_definition=(MarketingFilter(filter_type=MarketingFilterType.SUBURB, value="Redfern"),),
        created_by="admin",
    )
    service = CampaignService(
        nookal=nookal,
        list_store=list_store,
        consent_store=consent_store,
        suppression_store=suppression_store,
        email_adapter=adapter,
    )

    campaign = service.create_campaign(
        name="Needs approval",
        subject="No send",
        template_id="welcome_email",
        marketing_list_id=marketing_list.id,
        created_by="admin",
    )

    with pytest.raises(ApprovalError):
        service.send_campaign(campaign.id)


def test_fake_email_adapter_tracks_raw_delivery() -> None:
    adapter = FakeEmailAdapter()
    ref = adapter.send("patient@example.com", "Hi there")

    assert ref.startswith("fake-email-")
    assert adapter.sent_count == 1
    assert adapter.sent[0]["to"] == "patient@example.com"
    assert adapter.sent[0]["body"] == "Hi there"
