"""Referrer conflict queue — resolutions go through NookalClient + audit."""
from __future__ import annotations

import uuid
from typing import Any, Callable

from app.dashboard.schemas import ReferrerConflictOut, ReferrerResolveRequest
from app.nookal_client import NookalClient
from app.orchestration.referrer_sync import ReferrerConflictStore
from app.shared.exceptions import KillSwitchActive, NookalError


AuditFn = Callable[..., Any]


class ReferrerConflictService:
    def __init__(
        self,
        *,
        nookal: NookalClient,
        conflict_store: ReferrerConflictStore,
        audit: AuditFn,
    ) -> None:
        self._nookal = nookal
        self._store = conflict_store
        self._audit = audit
        self._ensure_ids()

    def _ensure_ids(self) -> None:
        for item in self._store.conflicts:
            item.setdefault("conflict_id", str(uuid.uuid4()))
            item.setdefault("kind", "conflict")
        for item in self._store.new_pending:
            item.setdefault("conflict_id", str(uuid.uuid4()))
            item.setdefault("kind", "new")

    def list_conflicts(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
    ) -> list[ReferrerConflictOut]:
        self._ensure_ids()
        out: list[ReferrerConflictOut] = []
        for item in self._store.conflicts:
            cand = item.get("candidate") or {}
            out.append(
                ReferrerConflictOut(
                    conflict_id=str(item["conflict_id"]),
                    kind="conflict",
                    candidate_name=cand.get("name"),
                    candidate_provider_number=cand.get("provider_number"),
                    possible_matches=list(item.get("possible_matches") or []),
                    reason=item.get("reason"),
                )
            )
        for item in self._store.new_pending:
            cand = item.get("candidate") or {}
            out.append(
                ReferrerConflictOut(
                    conflict_id=str(item["conflict_id"]),
                    kind="new",
                    candidate_name=cand.get("name"),
                    candidate_provider_number=cand.get("provider_number"),
                    possible_matches=[],
                    reason=item.get("reason") or "new",
                )
            )
        self._audit(
            actor=actor,
            action="dashboard.referrer_conflict_list",
            target_type="system",
            target_id="referrer_conflicts",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "count": len(out),
            },
        )
        return out

    def _pop(self, conflict_id: str) -> dict[str, Any] | None:
        self._ensure_ids()
        for collection in (self._store.conflicts, self._store.new_pending):
            for i, item in enumerate(collection):
                if str(item.get("conflict_id")) == conflict_id:
                    return collection.pop(i)
        return None

    def resolve(
        self,
        conflict_id: str,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        body: ReferrerResolveRequest,
    ) -> dict[str, Any]:
        item = self._pop(conflict_id)
        if item is None:
            self._audit(
                actor=actor,
                action="dashboard.referrer_resolve",
                target_type="system",
                target_id=conflict_id,
                result="failure",
                metadata={"correlation_id": correlation_id, "reason": "not_found"},
            )
            raise KeyError(f"conflict not found: {conflict_id}")

        cand = item.get("candidate") or {}
        action = body.action

        if action == "reject":
            self._audit(
                actor=actor,
                action="dashboard.referrer_resolve",
                target_type="system",
                target_id=conflict_id,
                result="success",
                metadata={
                    "correlation_id": correlation_id,
                    "role": role,
                    "resolve_action": "reject",
                },
            )
            return {"conflict_id": conflict_id, "action": "reject"}

        if action == "select_existing":
            if not body.referrer_id:
                # Put back so the queue is not lost on bad input.
                if item.get("kind") == "new" or conflict_id in {
                    str(x.get("conflict_id")) for x in self._store.new_pending
                }:
                    self._store.new_pending.append(item)
                else:
                    self._store.conflicts.append(item)
                raise ValueError("referrer_id required for select_existing")
            payload = {
                "referrer_id": body.referrer_id,
                "name": cand.get("name"),
                "provider_number": cand.get("provider_number"),
            }
            try:
                updated = self._nookal.upsert_referrer(payload)
            except (KillSwitchActive, NookalError):
                self._store.conflicts.append(item)
                self._audit(
                    actor=actor,
                    action="dashboard.referrer_resolve",
                    target_type="patient_record",
                    target_id=body.referrer_id,
                    result="failure",
                    metadata={
                        "correlation_id": correlation_id,
                        "resolve_action": "select_existing",
                    },
                )
                raise
            self._audit(
                actor=actor,
                action="dashboard.referrer_resolve",
                target_type="patient_record",
                target_id=updated.referrer_id,
                result="success",
                metadata={
                    "correlation_id": correlation_id,
                    "role": role,
                    "resolve_action": "select_existing",
                },
            )
            return {
                "conflict_id": conflict_id,
                "action": "select_existing",
                "referrer_id": updated.referrer_id,
            }

        if action == "create_new":
            payload = {
                "name": cand.get("name"),
                "provider_number": cand.get("provider_number"),
            }
            try:
                created = self._nookal.upsert_referrer(payload)
            except (KillSwitchActive, NookalError):
                self._store.new_pending.append(item)
                self._audit(
                    actor=actor,
                    action="dashboard.referrer_resolve",
                    target_type="system",
                    target_id=conflict_id,
                    result="failure",
                    metadata={
                        "correlation_id": correlation_id,
                        "resolve_action": "create_new",
                    },
                )
                raise
            self._audit(
                actor=actor,
                action="dashboard.referrer_resolve",
                target_type="patient_record",
                target_id=created.referrer_id,
                result="success",
                metadata={
                    "correlation_id": correlation_id,
                    "role": role,
                    "resolve_action": "create_new",
                },
            )
            return {
                "conflict_id": conflict_id,
                "action": "create_new",
                "referrer_id": created.referrer_id,
            }

        # Unknown action — restore
        if item.get("kind") == "new":
            self._store.new_pending.append(item)
        else:
            self._store.conflicts.append(item)
        raise ValueError(f"unknown action: {action}")
