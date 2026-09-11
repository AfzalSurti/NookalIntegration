"""
Internal communication state tracker.

Records which appointment workflows have been triggered and by whom
(Nookal native vs application) to prevent duplicate sends.

Storage: JSONL format consistent with existing SentLog.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.shared.clock import Clock, SystemClock


@dataclass(frozen=True)
class CommunicationStateEntry:
    """A recorded communication event."""
    appointment_id: str
    workflow: str  # CommunicationWorkflowType value
    ownership: str  # CommunicationOwnership value
    channel: str  # CommunicationChannel value or "nookal_native"
    status: str  # CommunicationStatus value
    recorded_at: str = ""


# Aliases for convenience and interoperability
CommunicationRecord = CommunicationStateEntry


class CommunicationStateStore:
    """
    Idempotent state tracker — prevents duplicate application-side sends.

    Thread-safe, append-only JSONL store.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        file_path: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        if file_path is not None:
            self._index = Path(file_path)
            self._dir = self._index.parent
            self._dir.mkdir(parents=True, exist_ok=True)
        elif directory is not None and str(directory).endswith(".jsonl"):
            self._index = Path(directory)
            self._dir = self._index.parent
            self._dir.mkdir(parents=True, exist_ok=True)
        else:
            from app.shared.config import get_settings

            try:
                settings = get_settings()
                self._dir = Path(directory) if directory else (settings.paths.working_dir / "communication_state")
            except Exception:
                self._dir = Path(directory) if directory else Path("data/working/communication_state")
            self._dir.mkdir(parents=True, exist_ok=True)
            self._index = self._dir / "communication-state.jsonl"

        self._lock = threading.Lock()
        self._clock: Clock = clock or SystemClock()
        self._seen = self._load()

    def _load(self) -> dict[str, CommunicationStateEntry]:
        """Load existing state from disk."""
        seen: dict[str, CommunicationStateEntry] = {}
        if not self._index.exists():
            return seen
        with self._index.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    key = self._make_key(row["appointment_id"], row["workflow"], row["channel"])
                    seen[key] = CommunicationStateEntry(
                        appointment_id=row["appointment_id"],
                        workflow=row["workflow"],
                        ownership=row["ownership"],
                        channel=row["channel"],
                        status=row["status"],
                        recorded_at=row["recorded_at"],
                    )
                except (json.JSONDecodeError, KeyError):
                    continue
        return seen

    @staticmethod
    def _make_key(appointment_id: str, workflow: str, channel: str) -> str:
        return f"{appointment_id}:{workflow}:{channel}"

    def has_been_handled(
        self,
        appointment_id: str,
        workflow: str,
        channel: str,
    ) -> bool:
        """Check if this workflow has already been handled for this appointment."""
        key = self._make_key(appointment_id, workflow, channel)
        return key in self._seen

    def has_been_triggered(self, appointment_id: str, workflow: Any) -> bool:
        """Check if a workflow type has been triggered for an appointment."""
        wf_str = workflow.value if hasattr(workflow, "value") else str(workflow)
        return any(
            e.appointment_id == appointment_id and e.workflow == wf_str
            for e in self._seen.values()
        )

    def is_duplicate(self, appointment_id: str, workflow: Any) -> bool:
        """Check if executing this workflow would be a duplicate send."""
        return self.has_been_triggered(appointment_id, workflow)

    def record(
        self,
        record_or_id: CommunicationStateEntry | str,
        workflow: Any | None = None,
        ownership: Any | None = None,
        channel: Any | None = None,
        status: Any | None = None,
    ) -> CommunicationStateEntry:
        """Record a communication event. Idempotent — skips if already recorded."""
        if isinstance(record_or_id, CommunicationStateEntry):
            entry_to_record = record_or_id
            appointment_id = entry_to_record.appointment_id
            wf_val = entry_to_record.workflow
            own_val = entry_to_record.ownership
            ch_val = entry_to_record.channel
            st_val = entry_to_record.status
        else:
            appointment_id = record_or_id
            wf_val = workflow.value if hasattr(workflow, "value") else str(workflow)
            own_val = ownership.value if hasattr(ownership, "value") else str(ownership)
            ch_val = channel.value if hasattr(channel, "value") else str(channel)
            st_val = status.value if hasattr(status, "value") else str(status)

        key = self._make_key(appointment_id, wf_val, ch_val)
        with self._lock:
            if key in self._seen:
                return self._seen[key]

            entry = CommunicationStateEntry(
                appointment_id=appointment_id,
                workflow=wf_val,
                ownership=own_val,
                channel=ch_val,
                status=st_val,
                recorded_at=self._clock.now().isoformat(),
            )
            row = {
                "appointment_id": entry.appointment_id,
                "workflow": entry.workflow,
                "ownership": entry.ownership,
                "channel": entry.channel,
                "status": entry.status,
                "recorded_at": entry.recorded_at,
            }
            with self._index.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            self._seen[key] = entry
            return entry

    def get_for_appointment(self, appointment_id: str) -> list[CommunicationStateEntry]:
        """Get all communication state entries for an appointment."""
        return [
            entry for entry in self._seen.values()
            if entry.appointment_id == appointment_id
        ]


# Alias for tracker class
CommunicationStateTracker = CommunicationStateStore

