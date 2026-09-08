"""
Append-only audit log.

Logs identifiers and action types only — never patient names, clinical
content, or full message bodies. Callers that try get AuditRejected.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Literal

from app.shared.config import AuditConfig, get_settings
from app.shared.clock import Clock, SystemClock
from app.shared.exceptions import AuditRejected

TargetType = Literal["patient_record", "appointment", "message", "document", "system", "task"]
Result = Literal["success", "failure", "blocked"]

# Keys that look like free-text dumps even if not in the configured forbid list.
_SUSPICIOUS_KEY = re.compile(
    r"(content|body|message|note|diagnos|clinical|payload|text|rendered)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AuditEvent:
    timestamp: str
    actor: str
    action: str
    target_type: TargetType
    target_id: str
    result: Result
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_line(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"))


class AuditLog:
    def __init__(
        self,
        directory: Path | None = None,
        config: AuditConfig | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        settings = get_settings()
        self._dir = directory or settings.paths.audit_dir
        self._config = config or settings.audit
        self._clock: Clock = clock or SystemClock()
        self._lock = threading.Lock()
        self._dir.mkdir(parents=True, exist_ok=True)

    def _day_file(self, when: datetime | None = None) -> Path:
        d = (when or self._clock.now()).astimezone().date()
        return self._dir / f"audit-{d.isoformat()}.jsonl"

    def _scrub_metadata(self, metadata: dict[str, Any] | None) -> dict[str, Any]:
        if not metadata:
            return {}

        cleaned: dict[str, Any] = {}
        max_len = self._config.max_metadata_value_length
        forbidden = self._config.forbidden_metadata_keys

        for key, value in metadata.items():
            key_l = str(key).lower()
            if key_l in forbidden or _SUSPICIOUS_KEY.search(key_l):
                raise AuditRejected(
                    f"metadata key '{key}' looks sensitive — audit logs take IDs only"
                )
            if isinstance(value, str) and len(value) > max_len:
                raise AuditRejected(
                    f"metadata '{key}' exceeds {max_len} chars — refuse free-text dumps"
                )
            if isinstance(value, (dict, list)):
                serialised = json.dumps(value, ensure_ascii=False)
                if len(serialised) > max_len:
                    raise AuditRejected(
                        f"metadata '{key}' nested value too large for audit log"
                    )
            cleaned[key] = value
        return cleaned

    def log_event(
        self,
        actor: str,
        action: str,
        target_type: TargetType,
        target_id: str,
        result: Result,
        metadata: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            timestamp=self._clock.now().isoformat(),
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id),
            result=result,
            metadata=self._scrub_metadata(metadata),
        )
        line = event.to_line() + "\n"
        path = self._day_file()
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
        return event

    def query(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        target_type: TargetType | None = None,
        result: Result | None = None,
        date_from: date | None = None,
        date_to: date | None = None,
        limit: int = 500,
    ) -> list[AuditEvent]:
        files = self._files_in_range(date_from, date_to)
        matches: list[AuditEvent] = []
        for path in files:
            for event in self._iter_file(path):
                if actor and event.actor != actor:
                    continue
                if action and event.action != action:
                    continue
                if target_type and event.target_type != target_type:
                    continue
                if result and event.result != result:
                    continue
                if date_from or date_to:
                    try:
                        # Match _day_file naming (local calendar day of the event).
                        ts = datetime.fromisoformat(event.timestamp).astimezone().date()
                    except ValueError:
                        continue
                    if date_from and ts < date_from:
                        continue
                    if date_to and ts > date_to:
                        continue
                matches.append(event)
                if len(matches) >= limit:
                    return matches
        return matches

    def _files_in_range(self, date_from: date | None, date_to: date | None) -> list[Path]:
        files = sorted(self._dir.glob("audit-*.jsonl"))
        if not date_from and not date_to:
            return files

        selected: list[Path] = []
        for path in files:
            try:
                file_day = date.fromisoformat(path.stem.removeprefix("audit-"))
            except ValueError:
                continue
            if date_from and file_day < date_from:
                continue
            if date_to and file_day > date_to:
                continue
            selected.append(path)
        return selected

    def _iter_file(self, path: Path) -> Iterable[AuditEvent]:
        if not path.exists():
            return
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                    yield AuditEvent(
                        timestamp=raw["timestamp"],
                        actor=raw["actor"],
                        action=raw["action"],
                        target_type=raw["target_type"],
                        target_id=raw["target_id"],
                        result=raw["result"],
                        metadata=raw.get("metadata") or {},
                    )
                except (json.JSONDecodeError, KeyError, TypeError):
                    # Skip corrupt lines; don't blow up staff query.
                    continue


# Module-level default used by other packages.
_default: AuditLog | None = None


def get_audit_log() -> AuditLog:
    global _default
    if _default is None:
        _default = AuditLog()
    return _default


def log_event(
    actor: str,
    action: str,
    target_type: TargetType,
    target_id: str,
    result: Result,
    metadata: dict[str, Any] | None = None,
) -> AuditEvent:
    return get_audit_log().log_event(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=target_id,
        result=result,
        metadata=metadata,
    )


def reset_audit_log_for_tests() -> None:
    global _default
    _default = None
