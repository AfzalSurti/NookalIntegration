"""
Tests for F1-F3: Domain models, consent, and suppression.

Focus:
1. Model creation and serialization
2. Campaign state transitions
3. Consent eligibility
4. Suppression eligibility
5. Immutability and type safety
"""
import pytest
from datetime import datetime, date
from app.marketing.models import (
    Campaign,
    CampaignRecipient,
    CampaignRecipientExclusionReason,
    CampaignRecipientStatus,
    CampaignStatus,
    CampaignTemplate,
    MarketingFilter,
    MarketingFilterType,
    MarketingList,
)
from app.marketing.consent import ConsentRecord, ConsentState, ConsentPolicy
from app.marketing.suppression import SuppressionRecord, SuppressionReason, SuppressionPolicy


class TestMarketingList:
    """F1: MarketingList model."""

    def test_create_empty_list(self):
        """Create list with no filters."""
        now = datetime.now().isoformat()
        marketing_list = MarketingList(
            id="list-1",
            name="Q4 Patients",
            description="Active patients for Q4 campaign",
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        assert marketing_list.id == "list-1"
        assert marketing_list.name == "Q4 Patients"
        assert marketing_list.status == "active"
        assert len(marketing_list.filter_definition) == 0

    def test_create_list_with_filters(self):
        """Create list with multiple filters."""
        now = datetime.now().isoformat()
        suburb_filter = MarketingFilter(
            filter_type=MarketingFilterType.SUBURB, value="Redfern"
        )
        age_filter = MarketingFilter(
            filter_type=MarketingFilterType.AGE_RANGE, value=(25, 65)
        )
        marketing_list = MarketingList(
            id="list-2",
            name="Redfern 25-65",
            description="Redfern patients aged 25-65",
            filter_definition=(suburb_filter, age_filter),
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        assert len(marketing_list.filter_definition) == 2
        assert marketing_list.filter_definition[0].value == "Redfern"
        assert marketing_list.filter_definition[1].value == (25, 65)

    def test_list_is_frozen(self):
        """Marketing lists are immutable."""
        now = datetime.now().isoformat()
        marketing_list = MarketingList(
            id="list-3",
            name="Test",
            description="Test",
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(AttributeError):
            marketing_list.name = "Modified"

    def test_list_to_dict(self):
        """Serialize list to dict."""
        now = datetime.now().isoformat()
        marketing_list = MarketingList(
            id="list-4",
            name="Test List",
            description="Test",
            created_by="admin",
            created_at=now,
            updated_at=now,
            cached_candidate_count=100,
        )
        data = marketing_list.to_dict()
        assert data["id"] == "list-4"
        assert data["cached_candidate_count"] == 100
        assert "filter_definition" in data


class TestCampaignTemplate:
    """F1: CampaignTemplate model."""

    def test_create_template(self):
        """Create email template."""
        now = datetime.now().isoformat()
        template = CampaignTemplate(
            id="tmpl-1",
            name="Weekly Newsletter",
            subject="Your Weekly Updates",
            body="Hi {patient_name}, here are this week's updates...",
            created_by="admin",
            created_at=now,
            placeholders=frozenset({"patient_name", "clinic_name"}),
        )
        assert template.id == "tmpl-1"
        assert "patient_name" in template.placeholders
        assert "clinic_name" in template.placeholders

    def test_template_is_frozen(self):
        """Templates are immutable."""
        now = datetime.now().isoformat()
        template = CampaignTemplate(
            id="tmpl-2",
            name="Template",
            subject="Subject",
            body="Body",
            created_by="admin",
            created_at=now,
        )
        with pytest.raises(AttributeError):
            template.subject = "Modified"


class TestCampaignStatus:
    """F1: Campaign lifecycle and state transitions."""

    def test_campaign_status_enum(self):
        """Campaign status enum has required states."""
        assert CampaignStatus.DRAFT.value == "draft"
        assert CampaignStatus.REVIEW.value == "review"
        assert CampaignStatus.APPROVED.value == "approved"
        assert CampaignStatus.QUEUED.value == "queued"
        assert CampaignStatus.SENDING.value == "sending"
        assert CampaignStatus.COMPLETED.value == "completed"

    def test_campaign_legal_transitions(self):
        """Test legal state transitions."""
        now = datetime.now().isoformat()
        campaign = Campaign(
            id="camp-1",
            name="Q4 Campaign",
            subject="Q4 Offers",
            template_id="tmpl-1",
            marketing_list_id="list-1",
            created_by="admin",
            created_at=now,
            updated_at=now,
            status=CampaignStatus.DRAFT,
        )

        # DRAFT → REVIEW is legal
        assert campaign.can_transition_to(CampaignStatus.REVIEW)
        # DRAFT → CANCELLED is legal
        assert campaign.can_transition_to(CampaignStatus.CANCELLED)
        # DRAFT → SENDING is ILLEGAL
        assert not campaign.can_transition_to(CampaignStatus.SENDING)

    def test_campaign_review_to_approved(self):
        """REVIEW → APPROVED is legal."""
        now = datetime.now().isoformat()
        campaign = Campaign(
            id="camp-2",
            name="Campaign",
            subject="Subject",
            template_id="tmpl-1",
            marketing_list_id="list-1",
            created_by="admin",
            created_at=now,
            updated_at=now,
            status=CampaignStatus.REVIEW,
        )
        assert campaign.can_transition_to(CampaignStatus.APPROVED)
        assert campaign.can_transition_to(CampaignStatus.DRAFT)  # can go back
        assert not campaign.can_transition_to(CampaignStatus.SENDING)  # not yet

    def test_campaign_approved_to_queued(self):
        """APPROVED → QUEUED is legal."""
        now = datetime.now().isoformat()
        campaign = Campaign(
            id="camp-3",
            name="Campaign",
            subject="Subject",
            template_id="tmpl-1",
            marketing_list_id="list-1",
            created_by="admin",
            created_at=now,
            updated_at=now,
            status=CampaignStatus.APPROVED,
        )
        assert campaign.can_transition_to(CampaignStatus.QUEUED)
        assert campaign.can_transition_to(CampaignStatus.CANCELLED)
        assert not campaign.can_transition_to(CampaignStatus.SENDING)  # must queue first

    def test_campaign_queued_to_sending(self):
        """QUEUED → SENDING is legal."""
        now = datetime.now().isoformat()
        campaign = Campaign(
            id="camp-4",
            name="Campaign",
            subject="Subject",
            template_id="tmpl-1",
            marketing_list_id="list-1",
            created_by="admin",
            created_at=now,
            updated_at=now,
            status=CampaignStatus.QUEUED,
        )
        assert campaign.can_transition_to(CampaignStatus.SENDING)

    def test_campaign_sending_to_completed(self):
        """SENDING → COMPLETED is legal."""
        now = datetime.now().isoformat()
        campaign = Campaign(
            id="camp-5",
            name="Campaign",
            subject="Subject",
            template_id="tmpl-1",
            marketing_list_id="list-1",
            created_by="admin",
            created_at=now,
            updated_at=now,
            status=CampaignStatus.SENDING,
        )
        assert campaign.can_transition_to(CampaignStatus.COMPLETED)
        assert campaign.can_transition_to(CampaignStatus.PARTIAL)
        assert campaign.can_transition_to(CampaignStatus.FAILED)

    def test_campaign_is_frozen(self):
        """Campaigns are immutable."""
        now = datetime.now().isoformat()
        campaign = Campaign(
            id="camp-6",
            name="Campaign",
            subject="Subject",
            template_id="tmpl-1",
            marketing_list_id="list-1",
            created_by="admin",
            created_at=now,
            updated_at=now,
        )
        with pytest.raises(AttributeError):
            campaign.name = "Modified"


class TestCampaignRecipient:
    """F1: Campaign recipient model."""

    def test_create_recipient_pending(self):
        """Create pending recipient."""
        recipient = CampaignRecipient(
            id="recip-1",
            campaign_id="camp-1",
            patient_id="pat-1",
            recipient_email="patient@example.com",
            idempotency_key="campaign_camp-1_recipient_pat-1",
        )
        assert recipient.status == CampaignRecipientStatus.PENDING
        assert recipient.exclusion_reason is None
        assert recipient.sent_at is None

    def test_create_recipient_with_exclusion(self):
        """Create recipient with exclusion reason."""
        recipient = CampaignRecipient(
            id="recip-2",
            campaign_id="camp-1",
            patient_id="pat-2",
            recipient_email="",
            status=CampaignRecipientStatus.SKIPPED,
            exclusion_reason=CampaignRecipientExclusionReason.INVALID_EMAIL,
            idempotency_key="campaign_camp-1_recipient_pat-2",
        )
        assert recipient.status == CampaignRecipientStatus.SKIPPED
        assert recipient.exclusion_reason == CampaignRecipientExclusionReason.INVALID_EMAIL

    def test_recipient_is_frozen(self):
        """Recipients are immutable."""
        recipient = CampaignRecipient(
            id="recip-3",
            campaign_id="camp-1",
            patient_id="pat-3",
            recipient_email="patient@example.com",
        )
        with pytest.raises(AttributeError):
            recipient.recipient_email = "modified@example.com"

    def test_recipient_to_dict(self):
        """Serialize recipient to dict."""
        recipient = CampaignRecipient(
            id="recip-4",
            campaign_id="camp-1",
            patient_id="pat-4",
            recipient_email="patient@example.com",
            status=CampaignRecipientStatus.SENT,
            sent_at="2026-09-08T10:00:00",
        )
        data = recipient.to_dict()
        assert data["status"] == "sent"
        assert data["sent_at"] == "2026-09-08T10:00:00"


class TestConsentState:
    """F2: Consent eligibility."""

    def test_consent_granted(self):
        """Patient with granted consent is eligible."""
        consent = ConsentRecord(
            patient_id="pat-1",
            state=ConsentState.GRANTED,
            changed_at="2026-01-01T00:00:00",
            changed_by="patient",
        )
        assert consent.is_granted()

    def test_consent_not_granted(self):
        """Patient without granted consent is not eligible."""
        consent = ConsentRecord(
            patient_id="pat-2",
            state=ConsentState.NOT_GRANTED,
            changed_at="2026-01-01T00:00:00",
        )
        assert not consent.is_granted()

    def test_consent_revoked(self):
        """Patient with revoked consent is not eligible."""
        consent = ConsentRecord(
            patient_id="pat-3",
            state=ConsentState.REVOKED,
            changed_at="2026-01-01T00:00:00",
            changed_by="patient",
            notes="Opted out via email link",
        )
        assert not consent.is_granted()

    def test_consent_is_frozen(self):
        """Consent records are immutable."""
        consent = ConsentRecord(
            patient_id="pat-4",
            state=ConsentState.GRANTED,
            changed_at="2026-01-01T00:00:00",
        )
        with pytest.raises(AttributeError):
            consent.state = ConsentState.REVOKED

    def test_consent_to_dict(self):
        """Serialize consent to dict."""
        consent = ConsentRecord(
            patient_id="pat-5",
            state=ConsentState.GRANTED,
            changed_at="2026-01-01T00:00:00",
            changed_by="admin",
        )
        data = consent.to_dict()
        assert data["state"] == "granted"
        assert data["patient_id"] == "pat-5"


class TestConsentPolicy:
    """F2: Consent eligibility policy."""

    def test_policy_no_consent_record(self):
        """Missing consent record → ineligible."""
        policy = ConsentPolicy()
        assert not policy.is_eligible(None)

    def test_policy_consent_granted(self):
        """Granted consent → eligible."""
        policy = ConsentPolicy()
        consent = ConsentRecord(
            patient_id="pat-1",
            state=ConsentState.GRANTED,
            changed_at="2026-01-01T00:00:00",
        )
        assert policy.is_eligible(consent)

    def test_policy_consent_not_granted(self):
        """No consent → ineligible."""
        policy = ConsentPolicy()
        consent = ConsentRecord(
            patient_id="pat-2",
            state=ConsentState.NOT_GRANTED,
            changed_at="2026-01-01T00:00:00",
        )
        assert not policy.is_eligible(consent)

    def test_policy_consent_revoked(self):
        """Revoked consent → ineligible."""
        policy = ConsentPolicy()
        consent = ConsentRecord(
            patient_id="pat-3",
            state=ConsentState.REVOKED,
            changed_at="2026-01-01T00:00:00",
        )
        assert not policy.is_eligible(consent)


class TestSuppressionRecord:
    """F3: Suppression model."""

    def test_create_suppression_unsubscribe(self):
        """Create suppression for unsubscribe."""
        suppression = SuppressionRecord(
            patient_id="pat-1",
            reason=SuppressionReason.UNSUBSCRIBE,
            suppressed_at="2026-09-08T10:00:00",
            suppressed_by="patient",
            notes="Unsubscribed via email link",
        )
        assert suppression.patient_id == "pat-1"
        assert suppression.reason == SuppressionReason.UNSUBSCRIBE
        assert suppression.is_active()

    def test_suppression_revoked_consent(self):
        """Create suppression for revoked consent."""
        suppression = SuppressionRecord(
            patient_id="pat-2",
            reason=SuppressionReason.CONSENT_REVOKED,
            suppressed_at="2026-09-08T10:00:00",
            suppressed_by="system",
        )
        assert suppression.reason == SuppressionReason.CONSENT_REVOKED
        assert suppression.is_active()

    def test_suppression_administrative(self):
        """Create administrative suppression."""
        suppression = SuppressionRecord(
            patient_id="pat-3",
            reason=SuppressionReason.ADMINISTRATIVE,
            suppressed_at="2026-09-08T10:00:00",
            suppressed_by="admin",
            notes="Do not market until further notice",
            expires_at="2026-12-31T23:59:59",
        )
        assert suppression.reason == SuppressionReason.ADMINISTRATIVE
        # Not expired yet
        assert suppression.is_active(now="2026-09-09T00:00:00")

    def test_suppression_expired(self):
        """Temporary suppression can expire."""
        suppression = SuppressionRecord(
            patient_id="pat-4",
            reason=SuppressionReason.ADMINISTRATIVE,
            suppressed_at="2026-09-08T10:00:00",
            suppressed_by="admin",
            expires_at="2026-09-09T00:00:00",
        )
        # Before expiry
        assert suppression.is_active(now="2026-09-08T23:59:59")
        # After expiry
        assert not suppression.is_active(now="2026-09-09T00:00:00")

    def test_suppression_no_expiry_permanent(self):
        """Suppression without expiry is permanent."""
        suppression = SuppressionRecord(
            patient_id="pat-5",
            reason=SuppressionReason.UNSUBSCRIBE,
            suppressed_at="2026-09-08T10:00:00",
            suppressed_by="patient",
        )
        # Permanent suppression
        assert suppression.is_active(now="2099-12-31T23:59:59")

    def test_suppression_is_frozen(self):
        """Suppression records are immutable."""
        suppression = SuppressionRecord(
            patient_id="pat-6",
            reason=SuppressionReason.UNSUBSCRIBE,
            suppressed_at="2026-09-08T10:00:00",
        )
        with pytest.raises(AttributeError):
            suppression.reason = SuppressionReason.ADMINISTRATIVE

    def test_suppression_to_dict(self):
        """Serialize suppression to dict."""
        suppression = SuppressionRecord(
            patient_id="pat-7",
            reason=SuppressionReason.BOUNCED,
            suppressed_at="2026-09-08T10:00:00",
            suppressed_by="system",
        )
        data = suppression.to_dict()
        assert data["reason"] == "bounced"
        assert data["patient_id"] == "pat-7"


class TestSuppressionPolicy:
    """F3: Suppression eligibility policy."""

    def test_policy_no_suppression_record(self):
        """Missing suppression record → not suppressed."""
        policy = SuppressionPolicy()
        assert not policy.is_suppressed(None)

    def test_policy_suppression_active(self):
        """Active suppression → suppressed."""
        policy = SuppressionPolicy()
        suppression = SuppressionRecord(
            patient_id="pat-1",
            reason=SuppressionReason.UNSUBSCRIBE,
            suppressed_at="2026-09-08T10:00:00",
        )
        assert policy.is_suppressed(suppression)

    def test_policy_suppression_expired(self):
        """Expired suppression → not suppressed."""
        policy = SuppressionPolicy()
        suppression = SuppressionRecord(
            patient_id="pat-2",
            reason=SuppressionReason.ADMINISTRATIVE,
            suppressed_at="2026-09-08T10:00:00",
            expires_at="2026-09-09T00:00:00",
        )
        assert policy.is_suppressed(suppression, now="2026-09-08T23:59:59")
        assert not policy.is_suppressed(suppression, now="2026-09-09T00:00:00")
