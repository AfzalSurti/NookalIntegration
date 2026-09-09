"""
Marketing lists and email/newsletter campaigns.

Phase F: offline implementation using MockNookalClient, FakeEmailAdapter,
existing ApprovalQueue, and audit system.

- Explicit consent-based filtering
- Suppression as first-class concept
- Send-time eligibility rechecks
- Idempotent campaign execution
- RBAC and audit integration
"""
from __future__ import annotations

from app.marketing.audience import AudienceBuilder, AudienceResult
from app.marketing.consent import ConsentRecord, ConsentState, ConsentPolicy
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
from app.marketing.store import (
    CampaignRecipientStore,
    CampaignStore,
    ConsentStore,
    MarketingListStore,
    SuppressionStore,
)
from app.marketing.suppression import SuppressionReason, SuppressionRecord, SuppressionPolicy

from app.marketing.service import CampaignService
from app.marketing.fake_email_adapter import FakeEmailAdapter
from app.marketing.unavailable_email_adapter import UnavailableEmailAdapter

__all__ = [
    "MarketingList",
    "MarketingFilter",
    "MarketingFilterType",
    "Campaign",
    "CampaignRecipient",
    "CampaignRecipientStatus",
    "CampaignRecipientExclusionReason",
    "CampaignStatus",
    "CampaignTemplate",
    "ConsentState",
    "ConsentRecord",
    "ConsentPolicy",
    "SuppressionReason",
    "SuppressionRecord",
    "SuppressionPolicy",
    "AudienceBuilder",
    "AudienceResult",
    "MarketingListStore",
    "CampaignStore",
    "CampaignRecipientStore",
    "ConsentStore",
    "SuppressionStore",
    "CampaignService",
    "FakeEmailAdapter",
    "UnavailableEmailAdapter",
]
