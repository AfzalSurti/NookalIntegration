"""
Pending patient confirmations for appointment-changing workflows.

In-memory by default; injectable for tests. One-shot consume prevents replay writes.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from app.shared.clock import Clock, SystemClock


@dataclass
class PendingAction:
    id: str
    action: str  # reschedule | cancel | create
    patient_id: str
    phone: str
    payload: dict[str, Any]
    expires_at: datetime
    correlation_id: str
    consumed: bool = False


class PendingActionStore:
    def __init__(self, *, clock: Clock | None = None, ttl_minutes: int = 30) -> None:
        self._clock = clock or SystemClock()
        self._ttl = timedelta(minutes=ttl_minutes)
        self._lock = threading.Lock()
        self._items: dict[str, PendingAction] = {}

    def create(
        self,
        *,
        action: str,
        patient_id: str,
        phone: str,
        payload: dict[str, Any],
        correlation_id: str,
    ) -> PendingAction:
        item = PendingAction(
            id=str(uuid.uuid4()),
            action=action,
            patient_id=patient_id,
            phone=phone,
            payload=dict(payload),
            expires_at=self._clock.now() + self._ttl,
            correlation_id=correlation_id,
        )
        with self._lock:
            self._items[item.id] = item
        return item

    def get(self, action_id: str) -> PendingAction | None:
        with self._lock:
            return self._items.get(action_id)

    def find_open(self, *, phone: str, action: str | None = None) -> list[PendingAction]:
        now = self._clock.now()
        with self._lock:
            out = []
            for item in self._items.values():
                if item.consumed or item.expires_at < now:
                    continue
                if item.phone != phone:
                    continue
                if action is not None and item.action != action:
                    continue
                out.append(item)
            return out

    def consume(self, action_id: str) -> PendingAction | None:
        """Mark consumed; returns None if missing, expired, or already consumed."""
        now = self._clock.now()
        with self._lock:
            item = self._items.get(action_id)
            if item is None or item.consumed or item.expires_at < now:
                return None
            item.consumed = True
            return item
