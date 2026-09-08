"""
Referring-doctor synchronization — exact match only; conflicts to human queue.

Uses mock/test-domain Referrer records. Does not invent live Nookal schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.nookal_client import Referrer
from app.orchestration.base import BaseWorkflow
from app.orchestration.context import WorkflowContext
from app.orchestration.results import WorkflowErrorInfo, WorkflowResult, WorkflowStatus
from app.shared.exceptions import KillSwitchActive, NookalError


@dataclass
class ReferrerConflictStore:
    """In-memory conflict / new-referrer confirmation queue for Phase C tests."""

    conflicts: list[dict[str, Any]] = field(default_factory=list)
    new_pending: list[dict[str, Any]] = field(default_factory=list)

    def add_conflict(self, item: dict[str, Any]) -> None:
        self.conflicts.append(item)

    def add_new(self, item: dict[str, Any]) -> None:
        self.new_pending.append(item)


def _norm_name(name: str) -> str:
    return " ".join(name.casefold().split())


def _exact_match(candidate: dict[str, Any], existing: Referrer) -> bool:
    cand_provider = candidate.get("provider_number") or None
    existing_provider = existing.provider_number or None
    names_equal = _norm_name(str(candidate.get("name", ""))) == _norm_name(existing.name)
    if cand_provider and existing_provider:
        return str(cand_provider) == str(existing_provider) and names_equal
    if cand_provider or existing_provider:
        # One side has a provider number and the other doesn't — not exact.
        return False
    return names_equal


def _possible_matches(candidate: dict[str, Any], existing: list[Referrer]) -> list[str]:
    """
    Surface uncertain candidates for humans — never auto-resolve.
    Conflict signal only: identical normalized name without provider equality,
    or shared surname token (last word), ignoring titles.
    """
    titles = {"dr", "doctor", "mr", "mrs", "ms", "prof"}
    cand_name = _norm_name(str(candidate.get("name", "")))
    cand_parts = [p for p in cand_name.split() if p not in titles]
    hits: list[str] = []
    for ref in existing:
        if _exact_match(candidate, ref):
            continue
        existing_name = _norm_name(ref.name)
        if cand_name == existing_name:
            hits.append(ref.referrer_id)
            continue
        existing_parts = [p for p in existing_name.split() if p not in titles]
        if not cand_parts or not existing_parts:
            continue
        # Surname = last significant token; require match + some first-token ambiguity.
        if cand_parts[-1] == existing_parts[-1] and cand_parts[-1] not in titles:
            hits.append(ref.referrer_id)
    return hits


class ReferrerSyncWorkflow(BaseWorkflow):
    name: ClassVar[str] = "referrer_sync"

    def execute(
        self,
        ctx: WorkflowContext,
        *,
        candidates: list[dict[str, Any]],
        conflict_store: ReferrerConflictStore | None = None,
        **_extra: Any,
    ) -> WorkflowResult:
        """
        Compare inbound referrer candidates to canonical mock store.

        candidates: list of {name, provider_number?, referrer_id?} — synthetic only.
        """
        store = conflict_store or ReferrerConflictStore()
        existing = list(ctx.nookal.list_referrers())
        items: list[dict[str, Any]] = []
        counts = {"exact_updated": 0, "conflict": 0, "new_pending": 0, "failed": 0}

        ctx.audit_event(
            "referrer_sync.started_batch",
            metadata={"candidate_count": len(candidates), "canonical_count": len(existing)},
        )

        for cand in candidates:
            item = self._process_one(ctx, cand, existing, store)
            items.append(item)
            outcome = item["outcome"]
            if outcome in counts:
                counts[outcome] += 1
            # Refresh existing after successful upsert
            if outcome == "exact_updated":
                existing = list(ctx.nookal.list_referrers())

        status = WorkflowStatus.SUCCESS
        if counts["failed"] and (counts["exact_updated"] or counts["conflict"] or counts["new_pending"]):
            status = WorkflowStatus.PARTIAL
        elif counts["failed"] and not (
            counts["exact_updated"] or counts["conflict"] or counts["new_pending"]
        ):
            status = WorkflowStatus.FAILED
        elif not candidates:
            status = WorkflowStatus.SKIPPED

        error = None
        if status == WorkflowStatus.FAILED:
            error = WorkflowErrorInfo(
                code="referrer_sync_failed",
                message="referrer sync failed",
                details=counts,
            )
        elif status == WorkflowStatus.PARTIAL:
            error = WorkflowErrorInfo(
                code="referrer_sync_partial",
                message="some referrer sync items failed",
                details=counts,
            )

        return WorkflowResult(
            workflow=self.name,
            status=status,
            correlation_id=ctx.correlation_id,
            data={
                "items": items,
                "exact_updated": counts["exact_updated"],
                "conflict": counts["conflict"],
                "new_pending_count": counts["new_pending"],
                "failed": counts["failed"],
                "conflict_queue": list(store.conflicts),
                "new_queue": list(store.new_pending),
            },
            error=error,
        )

    def _process_one(
        self,
        ctx: WorkflowContext,
        candidate: dict[str, Any],
        existing: list[Referrer],
        store: ReferrerConflictStore,
    ) -> dict[str, Any]:
        name = candidate.get("name")
        if not name:
            ctx.audit_event(
                "referrer_sync.invalid_candidate",
                result="failure",
                metadata={"reason": "missing_name"},
            )
            return {"outcome": "failed", "code": "missing_name"}

        # Exact match?
        exact = [r for r in existing if _exact_match(candidate, r)]
        if len(exact) == 1:
            ref = exact[0]
            payload = {
                "referrer_id": ref.referrer_id,
                "name": name,
                "provider_number": candidate.get("provider_number") or ref.provider_number,
            }
            try:
                updated = ctx.nookal.upsert_referrer(payload)
            except KillSwitchActive:
                raise
            except NookalError as exc:
                ctx.audit_event(
                    "referrer_sync.update_failed",
                    target_type="patient_record",
                    target_id=ref.referrer_id,
                    result="failure",
                    metadata={"error_type": type(exc).__name__},
                )
                return {
                    "outcome": "failed",
                    "code": "upsert_failed",
                    "referrer_id": ref.referrer_id,
                }
            ctx.audit_event(
                "referrer_sync.exact_updated",
                target_type="patient_record",
                target_id=updated.referrer_id,
                metadata={"match": "exact"},
            )
            return {
                "outcome": "exact_updated",
                "referrer_id": updated.referrer_id,
            }

        if len(exact) > 1:
            # Duplicate exact matches — human must resolve; do not write.
            ids = [r.referrer_id for r in exact]
            store.add_conflict(
                {
                    "candidate": {
                        "name": name,
                        "provider_number": candidate.get("provider_number"),
                    },
                    "possible_matches": ids,
                    "reason": "duplicate_exact",
                }
            )
            ctx.audit_event(
                "referrer_sync.conflict",
                result="failure",
                metadata={"reason": "duplicate_exact", "match_count": len(ids)},
            )
            return {"outcome": "conflict", "code": "duplicate_exact", "matches": ids}

        possibles = _possible_matches(candidate, existing)
        if possibles:
            store.add_conflict(
                {
                    "candidate": {
                        "name": name,
                        "provider_number": candidate.get("provider_number"),
                    },
                    "possible_matches": possibles,
                    "reason": "ambiguous",
                }
            )
            ctx.audit_event(
                "referrer_sync.conflict",
                metadata={"reason": "ambiguous", "match_count": len(possibles)},
            )
            return {"outcome": "conflict", "code": "ambiguous", "matches": possibles}

        # Genuinely new — queue for confirmation; do not auto-insert.
        store.add_new(
            {
                "candidate": {
                    "name": name,
                    "provider_number": candidate.get("provider_number"),
                },
                "reason": "new",
            }
        )
        ctx.audit_event(
            "referrer_sync.new_pending",
            metadata={"has_provider": bool(candidate.get("provider_number"))},
        )
        return {"outcome": "new_pending", "code": "new"}
