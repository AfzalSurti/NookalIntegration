"""
F4 — Audience Builder.

Build marketing audiences from lists using NookalClient abstraction.

Supports filters already available in the project:
- suburb
- age_range
- last_appointment_date_range
- referrer_id

Flow:
1. User selects filters
2. NookalClient searches candidates
3. Consent check
4. Suppression check
5. Valid email check
6. Return safe aggregate counts

Never display patient names/records on campaign pages.
Return only: candidate_count, eligible_count, suppressed_count, no_consent_count, invalid_email_count
"""
from __future__ import annotations

from typing import Any

from app.marketing.models import MarketingFilter, MarketingFilterType, MarketingList
from app.marketing.consent import ConsentPolicy, ConsentRecord
from app.marketing.suppression import SuppressionPolicy, SuppressionRecord
from app.nookal_client import NookalClient, PatientRef
from app.shared.exceptions import AutomationError


class AudienceResult:
    """
    Safe aggregate result from audience building.

    Never contains patient names, emails, or clinical information.
    Only counts and eligibility summaries.
    """

    def __init__(
        self,
        candidate_count: int,
        eligible_count: int,
        suppressed_count: int,
        no_consent_count: int,
        invalid_email_count: int,
    ) -> None:
        self.candidate_count = candidate_count
        self.eligible_count = eligible_count
        self.suppressed_count = suppressed_count
        self.no_consent_count = no_consent_count
        self.invalid_email_count = invalid_email_count

    def to_dict(self) -> dict[str, int]:
        return {
            "candidate_count": self.candidate_count,
            "eligible_count": self.eligible_count,
            "suppressed_count": self.suppressed_count,
            "no_consent_count": self.no_consent_count,
            "invalid_email_count": self.invalid_email_count,
        }


class AudienceBuilder:
    """
    Build marketing audiences from lists.

    Dynamic evaluation only — never treat snapshot as permanent.
    Always recalculate consent/suppression at send time.
    """

    def __init__(
        self,
        nookal: NookalClient,
        consent_store: Any,  # ConsentStore
        suppression_store: Any,  # SuppressionStore
    ) -> None:
        self.nookal = nookal
        self.consent_store = consent_store
        self.suppression_store = suppression_store
        self.consent_policy = ConsentPolicy()
        self.suppression_policy = SuppressionPolicy()

    def build_audience(self, marketing_list: MarketingList) -> AudienceResult:
        """
        Build audience from marketing list filters.

        Returns aggregate counts only — never patient details.
        """
        # Get candidate patients matching filters
        candidates = self._query_candidates(marketing_list.filter_definition)

        # Evaluate each candidate
        eligible: list[PatientRef] = []
        suppressed_count = 0
        no_consent_count = 0
        invalid_email_count = 0

        for candidate in candidates:
            # Check email validity
            if not candidate.email or "@" not in candidate.email:
                invalid_email_count += 1
                continue

            # Check suppression
            suppression = self.suppression_store.get(candidate.patient_id)
            if self.suppression_policy.is_suppressed(suppression):
                suppressed_count += 1
                continue

            # Check consent
            consent = self.consent_store.get(candidate.patient_id)
            if not self.consent_policy.is_eligible(consent):
                no_consent_count += 1
                continue

            # Eligible
            eligible.append(candidate)

        return AudienceResult(
            candidate_count=len(candidates),
            eligible_count=len(eligible),
            suppressed_count=suppressed_count,
            no_consent_count=no_consent_count,
            invalid_email_count=invalid_email_count,
        )

    def get_eligible_recipients(
        self, marketing_list: MarketingList
    ) -> list[PatientRef]:
        """
        Get full eligible recipient list for send preparation.

        Still never includes patient names or clinical info.
        Called at send time, not list-eval time.
        """
        candidates = self._query_candidates(marketing_list.filter_definition)
        eligible = []

        for candidate in candidates:
            # All same checks as build_audience
            if not candidate.email or "@" not in candidate.email:
                continue

            suppression = self.suppression_store.get(candidate.patient_id)
            if self.suppression_policy.is_suppressed(suppression):
                continue

            consent = self.consent_store.get(candidate.patient_id)
            if not self.consent_policy.is_eligible(consent):
                continue

            eligible.append(candidate)

        return eligible

    def _query_candidates(self, filters: tuple[MarketingFilter, ...]) -> list[PatientRef]:
        """
        Query NookalClient for candidates matching filters.

        Uses NookalClient.search_patients() with optional parameters.
        Applies all filters in sequence (AND logic).
        """
        # Extract parameters for search_patients
        suburb = None
        age_min = None
        age_max = None
        appointment_from = None
        appointment_to = None
        referrer_id = None

        for filter_spec in filters:
            if filter_spec.filter_type == MarketingFilterType.SUBURB:
                suburb = filter_spec.value
            elif filter_spec.filter_type == MarketingFilterType.AGE_RANGE:
                age_min, age_max = filter_spec.value
            elif filter_spec.filter_type == MarketingFilterType.LAST_APPOINTMENT_DATE_RANGE:
                appointment_from, appointment_to = filter_spec.value
            elif filter_spec.filter_type == MarketingFilterType.REFERRER_ID:
                referrer_id = filter_spec.value
            else:
                raise AutomationError(f"unknown filter type: {filter_spec.filter_type}")

        # Call NookalClient.search_patients with extracted parameters
        return self.nookal.search_patients(
            suburb=suburb,
            age_min=age_min,
            age_max=age_max,
            appointment_from=appointment_from,
            appointment_to=appointment_to,
            referrer_id=referrer_id,
        )

