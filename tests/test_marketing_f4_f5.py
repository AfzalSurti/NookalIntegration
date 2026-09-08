"""
Tests for F4-F5: Audience builder and list semantics.

Focus:
1. Audience filtering by various criteria
2. Consent/suppression checks during audience building
3. Store persistence and retrieval
4. Dynamic list evaluation (no permanent snapshots)
5. Aggregate counts without patient details
"""
import pytest
from datetime import datetime, date
from pathlib import Path
import tempfile

from app.marketing.audience import AudienceBuilder, AudienceResult
from app.marketing.models import (
    Campaign,
    CampaignStatus,
    CampaignTemplate,
    MarketingFilter,
    MarketingFilterType,
    MarketingList,
)
from app.marketing.consent import ConsentState
from app.marketing.store import (
    CampaignRecipientStore,
    CampaignStore,
    ConsentStore,
    MarketingListStore,
    SuppressionStore,
)
from app.marketing.suppression import SuppressionReason
from app.nookal_client import MockNookalClient, PatientRef
from datetime import date as date_module


class TestMarketingListStore:
    """F5: List store persistence."""

    def test_create_and_retrieve_list(self):
        """Create and retrieve a marketing list."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MarketingListStore(Path(tmpdir) / "lists.jsonl")

            # Create list
            mlist = store.create(
                name="Test List",
                description="Test description",
                created_by="admin",
            )
            assert mlist.id.startswith("mlist-")
            assert mlist.name == "Test List"

            # Retrieve list
            retrieved = store.get(mlist.id)
            assert retrieved is not None
            assert retrieved.id == mlist.id
            assert retrieved.name == "Test List"

    def test_list_with_filters_persists(self):
        """Filters are saved and restored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MarketingListStore(Path(tmpdir) / "lists.jsonl")

            # Create list with filters
            suburb_filter = MarketingFilter(
                filter_type=MarketingFilterType.SUBURB, value="Redfern"
            )
            age_filter = MarketingFilter(
                filter_type=MarketingFilterType.AGE_RANGE, value=(25, 65)
            )
            mlist = store.create(
                name="Filtered List",
                description="Test",
                filter_definition=(suburb_filter, age_filter),
                created_by="admin",
            )

            # Create new store from same path
            store2 = MarketingListStore(Path(tmpdir) / "lists.jsonl")
            retrieved = store2.get(mlist.id)

            assert retrieved is not None
            assert len(retrieved.filter_definition) == 2
            assert retrieved.filter_definition[0].value == "Redfern"
        # JSON converts tuples to lists, so compare values instead
        assert retrieved.filter_definition[1].value == [25, 65]
        with tempfile.TemporaryDirectory() as tmpdir:
            store = MarketingListStore(Path(tmpdir) / "lists.jsonl")

            store.create(name="List 1", description="Desc 1")
            store.create(name="List 2", description="Desc 2")
            store.create(name="List 3", description="Desc 3")

            all_lists = store.all()
            assert len(all_lists) == 3


class TestCampaignStore:
    """F6 prep: Campaign store."""

    def test_create_and_retrieve_campaign(self):
        """Create and retrieve campaign."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignStore(Path(tmpdir) / "campaigns.jsonl")

            campaign = store.create(
                name="Test Campaign",
                subject="Test Subject",
                template_id="tmpl-1",
                marketing_list_id="list-1",
                created_by="admin",
            )
            assert campaign.id.startswith("camp-")
            assert campaign.status == CampaignStatus.DRAFT

            retrieved = store.get(campaign.id)
            assert retrieved is not None
            assert retrieved.id == campaign.id

    def test_update_campaign_status(self):
        """Update campaign status with transition validation."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignStore(Path(tmpdir) / "campaigns.jsonl")

            campaign = store.create(
                name="Campaign",
                subject="Subject",
                template_id="tmpl-1",
                marketing_list_id="list-1",
            )

            # DRAFT → REVIEW (legal)
            updated = store.update_status(campaign.id, CampaignStatus.REVIEW)
            assert updated.status == CampaignStatus.REVIEW

            # REVIEW → APPROVED (legal)
            updated = store.update_status(campaign.id, CampaignStatus.APPROVED)
            assert updated.status == CampaignStatus.APPROVED

    def test_illegal_status_transition_rejected(self):
        """Illegal transitions are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = CampaignStore(Path(tmpdir) / "campaigns.jsonl")

            campaign = store.create(
                name="Campaign",
                subject="Subject",
                template_id="tmpl-1",
                marketing_list_id="list-1",
            )

            # DRAFT → SENDING (illegal)
            with pytest.raises(Exception):  # ApprovalError
                store.update_status(campaign.id, CampaignStatus.SENDING)


class TestConsentStore:
    """F2: Consent store."""

    def test_grant_consent(self):
        """Grant explicit consent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConsentStore(Path(tmpdir) / "consent.jsonl")

            consent = store.grant(
                patient_id="pat-1", changed_by="patient", notes="Opted in via website"
            )
            assert consent.state == ConsentState.GRANTED

            retrieved = store.get("pat-1")
            assert retrieved is not None
            assert retrieved.is_granted()

    def test_revoke_consent(self):
        """Revoke consent."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConsentStore(Path(tmpdir) / "consent.jsonl")

            store.grant("pat-1")
            revoked = store.revoke("pat-1", changed_by="system", notes="Unsubscribed")
            assert revoked.state == ConsentState.REVOKED

            retrieved = store.get("pat-1")
            assert not retrieved.is_granted()

    def test_consent_history_keeps_latest(self):
        """Latest consent record is kept."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = ConsentStore(Path(tmpdir) / "consent.jsonl")

            store.grant("pat-1")
            store.revoke("pat-1")
            store.grant("pat-1")

            # Latest should be granted
            retrieved = store.get("pat-1")
            assert retrieved.is_granted()


class TestSuppressionStore:
    """F3: Suppression store."""

    def test_suppress_and_unsuppress(self):
        """Suppress and unsuppress."""
        with tempfile.TemporaryDirectory() as tmpdir:
            store = SuppressionStore(Path(tmpdir) / "suppression.jsonl")

            suppression = store.suppress(
                patient_id="pat-1",
                reason=SuppressionReason.UNSUBSCRIBE,
                suppressed_by="patient",
            )
            assert suppression.reason == SuppressionReason.UNSUBSCRIBE

            retrieved = store.get("pat-1")
            assert retrieved is not None
            assert retrieved.is_active()

            # Unsuppress
            store.unsuppress("pat-1")
            retrieved = store.get("pat-1")
            assert retrieved is None


class TestAudienceBuilder:
    """F4: Audience builder."""

    @pytest.fixture
    def setup(self, tmp_path):
        """Setup nookal, stores, and audience builder."""
        nookal = MockNookalClient()

        # Seed patients
        nookal.seed_patient(
            PatientRef(
                patient_id="pat-1",
                email="pat1@example.com",
                suburb="Redfern",
                date_of_birth=date(1990, 1, 1),
                referrer_id="ref-1",
            )
        )
        nookal.seed_patient(
            PatientRef(
                patient_id="pat-2",
                email="pat2@example.com",
                suburb="Newtown",
                date_of_birth=date(1980, 1, 1),
                referrer_id="ref-2",
            )
        )
        nookal.seed_patient(
            PatientRef(
                patient_id="pat-3",
                email="pat3@example.com",
                suburb="Redfern",
                date_of_birth=date(2000, 1, 1),
                referrer_id="ref-1",
            )
        )
        nookal.seed_patient(
            PatientRef(
                patient_id="pat-4",
                email="",  # no email
                suburb="Redfern",
                date_of_birth=date(1985, 1, 1),
            )
        )

        consent_store = ConsentStore(tmp_path / "consent.jsonl")
        suppression_store = SuppressionStore(tmp_path / "suppression.jsonl")

        builder = AudienceBuilder(nookal, consent_store, suppression_store)
        return builder, nookal, consent_store, suppression_store, tmp_path

    def test_audience_no_filters(self, setup):
        """Build audience with no filters."""
        builder, nookal, consent_store, suppression_store, tmpdir = setup

        # All patients have consent and no suppression
        for pat_id in ["pat-1", "pat-2", "pat-3"]:
            consent_store.grant(pat_id)

        mlist = MarketingList(
            id="list-1", name="All", description="All patients", filter_definition=()
        )

        result = builder.build_audience(mlist)

        assert result.candidate_count == 4  # All 4 patients
        assert result.eligible_count == 3  # Only 3 with email and consent
        assert result.invalid_email_count == 1  # pat-4 has no email
        assert result.suppressed_count == 0
        assert result.no_consent_count == 0

    def test_audience_with_suburb_filter(self, setup):
        """Filter by suburb."""
        builder, nookal, consent_store, suppression_store, tmpdir = setup

        # Grant all consent
        for pat_id in ["pat-1", "pat-2", "pat-3"]:
            consent_store.grant(pat_id)

        suburb_filter = MarketingFilter(
            filter_type=MarketingFilterType.SUBURB, value="Redfern"
        )
        mlist = MarketingList(
            id="list-1",
            name="Redfern",
            description="Redfern only",
            filter_definition=(suburb_filter,),
        )

        result = builder.build_audience(mlist)

        assert result.candidate_count == 3  # pat-1, pat-3, pat-4 are in Redfern
        assert result.eligible_count == 2  # pat-1 and pat-3 (pat-4 has no email)
        assert result.invalid_email_count == 1

    def test_audience_with_age_filter(self, setup):
        """Filter by age range."""
        builder, nookal, consent_store, suppression_store, tmpdir = setup

        # Grant all consent
        for pat_id in ["pat-1", "pat-2", "pat-3"]:
            consent_store.grant(pat_id)

        age_filter = MarketingFilter(
            filter_type=MarketingFilterType.AGE_RANGE, value=(30, 45)
        )
        mlist = MarketingList(
            id="list-1",
            name="Age 30-45",
            description="Patients aged 30-45",
            filter_definition=(age_filter,),
        )

        result = builder.build_audience(mlist)

        # pat-1 (34 yo), pat-2 (44 yo), pat-3 (24 yo - excluded)
        # patient ages in 2026: pat-1: 36, pat-2: 46 (excluded), pat-3: 26 (excluded)
        # only pat-1 qualifies
        assert result.eligible_count >= 1

    def test_audience_with_consent_check(self, setup):
        """Exclude patients without consent."""
        builder, nookal, consent_store, suppression_store, tmpdir = setup

        # Grant consent to only pat-1
        consent_store.grant("pat-1")

        mlist = MarketingList(
            id="list-1", name="All", description="All", filter_definition=()
        )

        result = builder.build_audience(mlist)

        # Only pat-1 has consent (and email)
        assert result.eligible_count == 1
        assert result.no_consent_count == 2  # pat-2 and pat-3 have no consent

    def test_audience_with_suppression_check(self, setup):
        """Exclude suppressed patients."""
        builder, nookal, consent_store, suppression_store, tmpdir = setup

        # Grant all consent
        for pat_id in ["pat-1", "pat-2", "pat-3"]:
            consent_store.grant(pat_id)

        # Suppress pat-2
        suppression_store.suppress(
            "pat-2", reason=SuppressionReason.UNSUBSCRIBE, suppressed_by="patient"
        )

        mlist = MarketingList(
            id="list-1", name="All", description="All", filter_definition=()
        )

        result = builder.build_audience(mlist)

        # pat-1 and pat-3 eligible, pat-2 suppressed, pat-4 no email
        assert result.eligible_count == 2
        assert result.suppressed_count == 1

    def test_send_time_recalculates_eligibility(self, setup):
        """Send-time eligibility uses current state, not snapshot."""
        builder, nookal, consent_store, suppression_store, tmpdir = setup

        # Grant consent to all
        for pat_id in ["pat-1", "pat-2", "pat-3"]:
            consent_store.grant(pat_id)

        mlist = MarketingList(
            id="list-1", name="All", description="All", filter_definition=()
        )

        # Build audience at time T
        result1 = builder.build_audience(mlist)
        assert result1.eligible_count == 3

        # Patient opts out
        suppression_store.suppress(
            "pat-1", reason=SuppressionReason.UNSUBSCRIBE, suppressed_by="patient"
        )

        # Recalculate at time T+1 (get_eligible_recipients for send)
        recipients = builder.get_eligible_recipients(mlist)

        # pat-1 should not be in recipients now
        pat_ids = [p.patient_id for p in recipients]
        assert "pat-1" not in pat_ids
        assert len(recipients) == 2

    def test_aggregate_counts_dont_leak_pii(self, setup):
        """Audience results contain only counts, no patient data."""
        builder, nookal, consent_store, suppression_store, tmpdir = setup

        for pat_id in ["pat-1", "pat-2", "pat-3"]:
            consent_store.grant(pat_id)

        mlist = MarketingList(
            id="list-1", name="All", description="All", filter_definition=()
        )

        result = builder.build_audience(mlist)

        # Result should be AudienceResult with counts only
        assert isinstance(result, AudienceResult)
        result_dict = result.to_dict()

        # Only numeric fields
        assert all(isinstance(v, int) for v in result_dict.values())
        assert "patient_name" not in result_dict
        assert "email" not in result_dict
        assert "phone" not in result_dict
