"""Audit viewer — append-only; never mutate records."""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

from app.dashboard.schemas import AuditEventOut
from app.shared.audit import AuditEvent, AuditLog


AuditFn = Callable[..., Any]

# Keys that must never appear in viewer output even if somehow present.
_BLOCKED_META_KEYS = frozenset(
    {
        "patient_name",
        "name",
        "body",
        "content",
        "message",
        "clinical_notes",
        "diagnosis",
        "email_body",
        "rendered_content",
        "password",
        "token",
        "secret",
        "api_key",
    }
)


def _sanitize_event(event: AuditEvent) -> AuditEventOut:
    meta = {
        k: v
        for k, v in (event.metadata or {}).items()
        if str(k).lower() not in _BLOCKED_META_KEYS
        and not any(s in str(k).lower() for s in ("password", "token", "secret", "body", "content"))
    }
    return AuditEventOut(
        timestamp=event.timestamp,
        actor=event.actor,
        action=event.action,
        target_type=event.target_type,
        target_id=event.target_id,
        result=event.result,
        metadata=meta,
    )


class AuditViewerService:
    def __init__(self, *, audit_log: AuditLog, audit: AuditFn) -> None:
        self._log = audit_log
        self._audit = audit

    def query(
        self,
        *,
        actor: str,
        role: str,
        correlation_id: str,
        filter_actor: str | None = None,
        filter_action: str | None = None,
        filter_result: str | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        correlation_id_filter: str | None = None,
        limit: int = 200,
    ) -> list[AuditEventOut]:
        events = self._log.query(
            actor=filter_actor,
            action=filter_action,
            result=filter_result,  # type: ignore[arg-type]
            date_from=date_from,
            date_to=date_to,
            limit=max(limit, 500) if correlation_id_filter else limit,
        )
        if correlation_id_filter:
            events = [
                e
                for e in events
                if (e.metadata or {}).get("correlation_id") == correlation_id_filter
            ][:limit]

        out = [_sanitize_event(e) for e in events]
        self._audit(
            actor=actor,
            action="dashboard.audit_view",
            target_type="system",
            target_id="audit",
            result="success",
            metadata={
                "correlation_id": correlation_id,
                "role": role,
                "result_count": len(out),
                "has_actor_filter": filter_actor is not None,
                "has_correlation_filter": correlation_id_filter is not None,
            },
        )
        return out
