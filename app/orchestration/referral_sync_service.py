"""
Referral Association Store & Referral Synchronization Service.

Ingests parsed patient-level referral rows, performs deterministic patient and referrer
matching, detects conflicts (dispatching to ReferrerConflictStore), manages persistent
patient-referrer associations idempotently, and optionally syncs verified referrer_id
to Nookal via official editPatient endpoint.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from app.nookal_client import NookalClient, PatientRef, Referrer
from app.orchestration.referral_report import MalformedReportError, ReferralReportParser, ReferralReportRow
from app.orchestration.referrer_sync import ReferrerConflictStore, _exact_match, _norm_name, _possible_matches
from app.shared.clock import Clock, SystemClock
from app.shared.config import get_settings
from app.shared.exceptions import KillSwitchActive, NookalError, NookalNotFound

AuditFn = Callable[..., Any]


logger = logging.getLogger(__name__)


@dataclass
class ReferralAssociation:
    """Synchronized association between a patient and their referring doctor."""

    patient_id: str
    patient_name: str | None
    referrer_name: str
    referrer_id: str | None = None
    provider_number: str | None = None
    source: str = "report_export"
    status: str = "synced"  # synced | review_required | conflict
    conflict_id: str | None = None
    synced_at: str = ""
    last_updated: str = ""


class ReferralAssociationStore:
    """
    Persistent store for patient-referrer associations.
    Uses append-only / thread-safe JSON Lines with in-memory fast indexing by patient_id.
    """

    def __init__(self, store_path: Path | None = None) -> None:
        settings = get_settings()
        self._path = store_path or (settings.paths.working_dir / "referral_associations.jsonl")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._by_patient: dict[str, ReferralAssociation] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        with self._lock:
            for line in self._path.read_text(encoding="utf-8").strip().splitlines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                    assoc = ReferralAssociation(**data)
                    self._by_patient[assoc.patient_id] = assoc
                except Exception as exc:
                    logger.warning("Skipping corrupted referral association line: %s", exc)

    def _flush_all(self) -> None:
        """Rewrite canonical associations file."""
        with self._lock:
            with self._path.open("w", encoding="utf-8") as f:
                for assoc in self._by_patient.values():
                    f.write(json.dumps(asdict(assoc), ensure_ascii=False) + "\n")

    def get(self, patient_id: str) -> ReferralAssociation | None:
        with self._lock:
            return self._by_patient.get(patient_id)

    def list_all(self) -> list[ReferralAssociation]:
        with self._lock:
            return list(self._by_patient.values())

    def upsert(self, assoc: ReferralAssociation) -> None:
        with self._lock:
            self._by_patient[assoc.patient_id] = assoc
            self._flush_all()


@dataclass
class ReferralSyncSummary:
    """Result of a report synchronization run."""

    total_rows: int = 0
    matched_exact: int = 0
    updated_changed: int = 0
    unchanged: int = 0
    conflicts_queued: int = 0
    new_pending_queued: int = 0
    review_required_patients: int = 0
    errors: list[str] = field(default_factory=list)


class ReferralSyncService:
    """
    Orchestrates the synchronization of patient-level referral reports.
    Uses official Nookal patient APIs for matching and optional edits.
    """

    def __init__(
        self,
        *,
        nookal: NookalClient,
        association_store: ReferralAssociationStore,
        conflict_store: ReferrerConflictStore,
        audit: AuditFn,
        clock: Clock | None = None,
    ) -> None:
        self._nookal = nookal
        self._assoc_store = association_store
        self._conflict_store = conflict_store
        self._audit = audit
        self._clock = clock or SystemClock()

    def sync_report_text(
        self,
        content: str,
        *,
        source_name: str,
        actor: str = "system",
        role: str = "admin",
        correlation_id: str = "report_sync",
        sync_to_nookal: bool = False,
    ) -> ReferralSyncSummary:
        """Sync a report passed as text."""
        rows = ReferralReportParser.parse_text(content, source_name=source_name)
        return self._process_rows(
            rows,
            source_name=source_name,
            actor=actor,
            role=role,
            correlation_id=correlation_id,
            sync_to_nookal=sync_to_nookal,
        )

    def sync_report_file(
        self,
        path: Path | str,
        *,
        actor: str = "system",
        role: str = "admin",
        correlation_id: str = "report_sync",
        sync_to_nookal: bool = False,
        delete_after_sync: bool = False,
    ) -> ReferralSyncSummary:
        """Sync a report file from disk."""
        p = Path(path)
        rows = ReferralReportParser.parse_file(p)
        summary = self._process_rows(
            rows,
            source_name=p.name,
            actor=actor,
            role=role,
            correlation_id=correlation_id,
            sync_to_nookal=sync_to_nookal,
        )
        if delete_after_sync and p.exists():
            try:
                p.unlink()
            except OSError as exc:
                logger.warning("Failed to delete processed report file %s: %s", p, exc)
        return summary

    def _process_rows(
        self,
        rows: list[ReferralReportRow],
        *,
        source_name: str,
        actor: str,
        role: str,
        correlation_id: str,
        sync_to_nookal: bool,
    ) -> ReferralSyncSummary:
        summary = ReferralSyncSummary(total_rows=len(rows))
        now_iso = self._clock.now().isoformat()

        # Audit start of batch
        self._audit(
            actor=actor,
            action="referral_sync.batch_started",
            target_type="system",
            target_id="referrals",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "source": source_name,
                "row_count": len(rows),
            },
        )

        # Retrieve canonical referrers if available from client or store
        canonical_referrers: list[Referrer] = []
        if hasattr(self._nookal, "list_referrers"):
            try:
                canonical_referrers = list(self._nookal.list_referrers())
            except Exception:
                canonical_referrers = []

        for row in rows:
            try:
                self._process_single_row(
                    row,
                    canonical_referrers=canonical_referrers,
                    source_name=source_name,
                    now_iso=now_iso,
                    actor=actor,
                    role=role,
                    correlation_id=correlation_id,
                    sync_to_nookal=sync_to_nookal,
                    summary=summary,
                )
            except Exception as exc:
                summary.errors.append(f"Row error ({row.patient_id or 'unknown'}): {str(exc)}")

        # Audit batch complete
        self._audit(
            actor=actor,
            action="referral_sync.batch_completed",
            target_type="system",
            target_id="referrals",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "source": source_name,
                "matched_exact": summary.matched_exact,
                "updated_changed": summary.updated_changed,
                "unchanged": summary.unchanged,
                "conflicts": summary.conflicts_queued,
                "new_pending": summary.new_pending_queued,
                "review_required": summary.review_required_patients,
            },
        )

        return summary

    def _resolve_patient(self, row: ReferralReportRow) -> PatientRef | None:
        """
        Match patient using strongest identifier available:
        1. Nookal patient_id
        2. Strict unique match via search_patients by name
        Never guess if ambiguous.
        """
        if row.patient_id:
            try:
                return self._nookal.get_patient(row.patient_id)
            except (NookalNotFound, Exception):
                # If patient_id was specified but not found in Nookal, do not guess
                return None

        if row.patient_name:
            # Query by name
            parts = row.patient_name.strip().split()
            first = parts[0] if parts else None
            last = parts[-1] if len(parts) > 1 else None
            if first and last:
                try:
                    matches = self._nookal.search_patients(first_name=first, last_name=last)
                    if len(matches) == 1:
                        return matches[0]
                except Exception:
                    pass
        return None

    def _process_single_row(
        self,
        row: ReferralReportRow,
        *,
        canonical_referrers: list[Referrer],
        source_name: str,
        now_iso: str,
        actor: str,
        role: str,
        correlation_id: str,
        sync_to_nookal: bool,
        summary: ReferralSyncSummary,
    ) -> None:
        patient = self._resolve_patient(row)

        if not patient:
            # Patient cannot be uniquely matched -> review required
            summary.review_required_patients += 1
            cand_pid = row.patient_id or "unknown"
            self._conflict_store.add_conflict(
                {
                    "kind": "patient_unmatched",
                    "reason": "Patient could not be uniquely matched in Nookal",
                    "candidate": {
                        "patient_id": row.patient_id,
                        "patient_name": row.patient_name,
                        "referrer_name": row.referrer_name,
                        "source": source_name,
                    },
                }
            )
            self._audit(
                actor=actor,
                action="referral_sync.patient_unmatched",
                target_type="patient_record",
                target_id=cand_pid,
                result="failure",
                metadata={"correlation_id": correlation_id, "source": source_name},
            )
            return

        pid = patient.patient_id

        # 2. Referrer matching
        cand_dict: dict[str, Any] = {
            "name": row.referrer_name,
            "provider_number": row.provider_number,
            "referrer_id": row.referrer_id,
        }

        # Check existing association
        existing_assoc = self._assoc_store.get(pid)

        # Referrer Matching against canonical
        matched_referrer_id: str | None = row.referrer_id
        exact_matches = [r for r in canonical_referrers if _exact_match(cand_dict, r)] if canonical_referrers else []

        if exact_matches:
            matched_referrer_id = exact_matches[0].referrer_id
        elif not matched_referrer_id and canonical_referrers:
            name_matches = [r for r in canonical_referrers if _norm_name(r.name) == _norm_name(row.referrer_name)]
            if len(name_matches) == 1:
                matched_referrer_id = name_matches[0].referrer_id
            else:
                possible = _possible_matches(cand_dict, canonical_referrers)
                if possible:
                    summary.conflicts_queued += 1
                    self._conflict_store.add_conflict(
                        {
                            "kind": "conflict",
                            "reason": "Multiple possible canonical referrers match doctor name",
                            "candidate": cand_dict,
                            "possible_matches": possible,
                        }
                    )
                    self._assoc_store.upsert(
                        ReferralAssociation(
                            patient_id=pid,
                            patient_name=patient.display_name,
                            referrer_name=row.referrer_name,
                            referrer_id=None,
                            provider_number=row.provider_number,
                            source=source_name,
                            status="conflict",
                            synced_at=now_iso,
                            last_updated=now_iso,
                        )
                    )
                    self._audit(
                        actor=actor,
                        action="referral_sync.referrer_conflict",
                        target_type="patient_record",
                        target_id=pid,
                        result="warning",
                        metadata={"correlation_id": correlation_id, "reason": "ambiguous_referrer"},
                    )
                    return
                else:
                    summary.new_pending_queued += 1
                    self._conflict_store.add_new(
                        {
                            "kind": "new",
                            "reason": "New referring doctor not present in canonical store",
                            "candidate": cand_dict,
                        }
                    )
                    self._assoc_store.upsert(
                        ReferralAssociation(
                            patient_id=pid,
                            patient_name=patient.display_name,
                            referrer_name=row.referrer_name,
                            referrer_id=None,
                            provider_number=row.provider_number,
                            source=source_name,
                            status="review_required",
                            synced_at=now_iso,
                            last_updated=now_iso,
                        )
                    )
                    self._audit(
                        actor=actor,
                        action="referral_sync.new_referrer_pending",
                        target_type="patient_record",
                        target_id=pid,
                        result="warning",
                        metadata={"correlation_id": correlation_id, "reason": "new_referrer"},
                    )
                    return


        # 3. Idempotent storage & change detection
        is_same = (
            existing_assoc is not None
            and _norm_name(existing_assoc.referrer_name) == _norm_name(row.referrer_name)
            and (existing_assoc.referrer_id or None) == (matched_referrer_id or None)
        )

        if is_same:
            # Unchanged: update timestamp only
            summary.unchanged += 1
            existing_assoc.last_updated = now_iso
            self._assoc_store.upsert(existing_assoc)
        else:
            if existing_assoc is not None:
                summary.updated_changed += 1
                action_name = "referral_sync.referrer_changed"
            else:
                summary.matched_exact += 1
                action_name = "referral_sync.association_created"

            new_assoc = ReferralAssociation(
                patient_id=pid,
                patient_name=patient.display_name,
                referrer_name=row.referrer_name,
                referrer_id=matched_referrer_id,
                provider_number=row.provider_number,
                source=source_name,
                status="synced",
                synced_at=now_iso,
                last_updated=now_iso,
            )
            self._assoc_store.upsert(new_assoc)

            self._audit(
                actor=actor,
                action=action_name,
                target_type="patient_record",
                target_id=pid,
                result="success",
                metadata={
                    "correlation_id": correlation_id,
                    "source": source_name,
                    "has_referrer_id": bool(matched_referrer_id),
                },
            )

        # 4. Optional sync back to Nookal via editPatient
        if sync_to_nookal and matched_referrer_id and patient.referrer_id != matched_referrer_id:
            try:
                self._nookal.edit_patient(pid, {"referrer_id": matched_referrer_id})
                self._audit(
                    actor=actor,
                    action="referral_sync.nookal_patient_updated",
                    target_type="patient_record",
                    target_id=pid,
                    result="success",
                    metadata={"correlation_id": correlation_id},
                )
            except Exception as exc:
                logger.warning("Could not sync referrer_id to Nookal for %s: %s", pid, exc)
