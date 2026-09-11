"""
Outbound messaging: WhatsApp Business API, SMS, email.

Official Business API only — WhatsApp Web scraping is forbidden by contract.
"""
from __future__ import annotations

import json
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

from app.messaging.adapters import Channel, ChannelAdapter
from app.messaging.adapters.email import EmailAdapter
from app.messaging.adapters.sms import SMSAdapter
from app.messaging.adapters.stub import StubAdapter
from app.messaging.adapters.whatsapp import WhatsAppAdapter
from app.shared import audit as audit_mod
from app.shared.clock import Clock, SystemClock
from app.shared.config import MessagingConfig, get_settings
from app.shared.exceptions import (
    IdempotencyConflict,
    KillSwitchActive,
    MessagingError,
    PermissionDenied,
)
from app.shared.kill_switch import assert_allows

MessageStatus = Literal["queued", "sent", "failed", "blocked", "skipped"]

_ROLE_PERMISSIONS: dict[str, set[str]] = {
    "staff": {"transactional"},
    "practitioner": {"transactional"},
    "admin": {"transactional", "bulk", "marketing"},
    "owner": {"transactional", "bulk", "marketing"},
    "system": {"transactional"},
}


@dataclass
class OutboundMessage:
    id: str
    channel: Channel
    patient_id: str
    template_id: str
    rendered_content: str
    idempotency_key: str
    status: MessageStatus
    sent_at: str | None = None
    provider_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TemplateStore:
    """Simple {placeholder} substitution. Client-approved templates live on disk."""

    def __init__(self, directory: Path | None = None) -> None:
        root = Path(__file__).resolve().parents[2]
        self._dir = directory or (root / "app" / "messaging" / "templates")
        self._dir.mkdir(parents=True, exist_ok=True)

    def render(self, template_id: str, context: Mapping[str, Any]) -> str:
        path = self._dir / f"{template_id}.txt"
        if not path.exists():
            raise MessagingError(f"unknown template: {template_id}")
        text = path.read_text(encoding="utf-8")
        try:
            return text.format_map(_SafeFormat(context))
        except KeyError as exc:
            raise MessagingError(f"template {template_id} missing key: {exc}") from exc


class _SafeFormat(dict):
    def __missing__(self, key: str) -> str:
        raise KeyError(key)


class SentLog:
    """Idempotency store — one record per idempotency_key."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        settings = get_settings()
        self._dir = directory or settings.paths.sent_log_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._index = self._dir / "sent-index.jsonl"
        self._lock = threading.Lock()
        self._clock: Clock = clock or SystemClock()
        self._seen = self._load()

    def _load(self) -> dict[str, str]:
        seen: dict[str, str] = {}
        if not self._index.exists():
            return seen
        with self._index.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                    seen[row["idempotency_key"]] = row["message_id"]
                except (json.JSONDecodeError, KeyError):
                    continue
        return seen

    def get(self, key: str) -> str | None:
        return self._seen.get(key)

    def record(self, key: str, message_id: str) -> None:
        with self._lock:
            if key in self._seen:
                return
            row = {
                "idempotency_key": key,
                "message_id": message_id,
                "recorded_at": self._clock.now().isoformat(),
            }
            with self._index.open("a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            self._seen[key] = message_id


class MessagingService:
    def __init__(
        self,
        config: MessagingConfig | None = None,
        *,
        adapters: dict[Channel, ChannelAdapter] | None = None,
        templates: TemplateStore | None = None,
        sent_log: SentLog | None = None,
        audit: Callable[..., Any] | None = None,
        actor: str = "messaging",
        clock: Clock | None = None,
    ) -> None:
        self._config = config or get_settings().messaging
        self._clock: Clock = clock or SystemClock()
        self._templates = templates or TemplateStore()
        self._sent = sent_log or SentLog(clock=self._clock)
        self._audit = audit or audit_mod.log_event
        self._actor = actor
        self._adapters = adapters if adapters is not None else self._default_adapters()

    def _default_adapters(self) -> dict[Channel, ChannelAdapter]:
        adapters: dict[Channel, ChannelAdapter] = {
            "whatsapp": StubAdapter("whatsapp"),
            "sms": StubAdapter("sms"),
            "email": StubAdapter("email"),
        }
        flags = self._config.channels or {}
        if flags.get("whatsapp") and self._config.whatsapp_token:
            adapters["whatsapp"] = WhatsAppAdapter(self._config)
        if flags.get("sms") and self._config.sms_api_key:
            adapters["sms"] = SMSAdapter(self._config)
        if flags.get("email") and self._config.smtp_host:
            adapters["email"] = EmailAdapter(self._config)
        return adapters

    def send(
        self,
        channel: Channel,
        patient_contact: str,
        template_id: str,
        context: Mapping[str, Any],
        *,
        patient_id: str,
        idempotency_key: str,
        caller_role: str,
        send_class: str = "transactional",
        skip_if_duplicate: bool = True,
    ) -> OutboundMessage:
        self._check_permission(caller_role, send_class)

        existing = self._sent.get(idempotency_key)
        if existing:
            if skip_if_duplicate:
                msg = OutboundMessage(
                    id=existing,
                    channel=channel,
                    patient_id=patient_id,
                    template_id=template_id,
                    rendered_content="",
                    idempotency_key=idempotency_key,
                    status="skipped",
                    metadata={"reason": "idempotent_replay"},
                )
                self._audit(
                    actor=self._actor,
                    action="send_skipped",
                    target_type="message",
                    target_id=idempotency_key,
                    result="success",
                    metadata={"channel": channel},
                )
                return msg
            raise IdempotencyConflict(idempotency_key)

        try:
            assert_allows(f"messaging.send.{channel}")
        except KillSwitchActive:
            self._audit(
                actor=self._actor,
                action="send",
                target_type="message",
                target_id=idempotency_key,
                result="blocked",
                metadata={"channel": channel, "reason": "kill_switch"},
            )
            return OutboundMessage(
                id=str(uuid.uuid4()),
                channel=channel,
                patient_id=patient_id,
                template_id=template_id,
                rendered_content="",
                idempotency_key=idempotency_key,
                status="blocked",
            )

        body = self._templates.render(template_id, context)
        adapter = self._adapters.get(channel)
        if adapter is None and isinstance(channel, str):
            adapter = self._adapters.get(channel.lower().strip())
        if adapter is None and channel in ("sms", "email"):
            adapter = StubAdapter(channel)
            self._adapters[channel] = adapter
        if adapter is None:
            raise MessagingError(f"no adapter for channel={channel}")

        message_id = str(uuid.uuid4())
        try:
            provider_ref = adapter.send(
                patient_contact,
                body,
                metadata={"template_id": template_id, "idempotency_key": idempotency_key},
            )
        except Exception as exc:
            self._audit(
                actor=self._actor,
                action="send",
                target_type="message",
                target_id=idempotency_key,
                result="failure",
                metadata={"channel": channel, "error_type": type(exc).__name__},
            )
            return OutboundMessage(
                id=message_id,
                channel=channel,
                patient_id=patient_id,
                template_id=template_id,
                rendered_content=body,
                idempotency_key=idempotency_key,
                status="failed",
                metadata={"error_type": type(exc).__name__},
            )

        self._sent.record(idempotency_key, message_id)
        sent_at = self._clock.now().isoformat()
        self._audit(
            actor=self._actor,
            action="send",
            target_type="message",
            target_id=idempotency_key,
            result="success",
            metadata={"channel": channel, "template_id": template_id},
        )
        return OutboundMessage(
            id=message_id,
            channel=channel,
            patient_id=patient_id,
            template_id=template_id,
            rendered_content=body,
            idempotency_key=idempotency_key,
            status="sent",
            sent_at=sent_at,
            provider_ref=provider_ref,
        )

    @staticmethod
    def _check_permission(caller_role: str, send_class: str) -> None:
        allowed = _ROLE_PERMISSIONS.get(caller_role)
        if not allowed or send_class not in allowed:
            raise PermissionDenied(
                f"role={caller_role!r} cannot send class={send_class!r}"
            )


def send(
    channel: Channel,
    patient_contact: str,
    template_id: str,
    context: Mapping[str, Any],
    **kwargs: Any,
) -> OutboundMessage:
    return MessagingService().send(
        channel, patient_contact, template_id, context, **kwargs
    )


__all__ = [
    "Channel",
    "ChannelAdapter",
    "MessagingService",
    "OutboundMessage",
    "SentLog",
    "TemplateStore",
    "send",
]
