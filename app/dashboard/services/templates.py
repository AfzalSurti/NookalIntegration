"""
Template management service for NookalIntegration.

Provides unified discovery, inspection, live rendering, and safe editing for:
1. Communication / SMS & Email templates (app/messaging/templates)
2. Clinical Letter & Medical Certificate templates (app/letters/templates)
3. Marketing Campaign templates (persisted CampaignTemplateStore)
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from app.letters.templates import TemplateError, load_template, templates_root
from app.marketing.models import CampaignTemplate
from app.shared.config import get_settings
from app.shared.exceptions import MessagingError, repo_root

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


@dataclass
class UnifiedTemplate:
    id: str
    name: str
    category: str  # "communication" | "letters" | "marketing"
    channel: str   # "sms" | "email" | "pdf_letter" | "campaign"
    body: str
    subject: str | None = None
    document_type: str | None = None
    required_placeholders: list[str] = field(default_factory=list)
    optional_placeholders: list[str] = field(default_factory=list)
    all_placeholders: list[str] = field(default_factory=list)
    description: str = ""
    updated_at: str = ""
    is_editable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Default metadata & friendly names for messaging templates
_COMMUNICATION_METADATA: dict[str, dict[str, Any]] = {
    "direct_message": {
        "name": "Direct Patient Message",
        "channel": "sms/email",
        "description": "Standard direct message sent to an individual patient with their name and custom body text.",
        "required_placeholders": ["name", "message"],
        "optional_placeholders": [],
    },
    "appointment_reminder": {
        "name": "Appointment Reminder",
        "channel": "sms",
        "description": "Automated SMS appointment reminder sent before a scheduled clinical session.",
        "required_placeholders": ["name", "date", "time"],
        "optional_placeholders": [],
    },
    "reschedule_confirm_prompt": {
        "name": "Reschedule Confirmation Request",
        "channel": "sms",
        "description": "SMS sent when an appointment time has changed, asking the patient to confirm or call back.",
        "required_placeholders": ["name", "date", "time"],
        "optional_placeholders": [],
    },
    "certificate_sent": {
        "name": "Medical Certificate Notice",
        "channel": "sms/email",
        "description": "Notification sent to a patient when their medical certificate has been issued.",
        "required_placeholders": ["name"],
        "optional_placeholders": [],
    },
}

_LETTERS_METADATA: dict[str, dict[str, Any]] = {
    "progress_letter": {
        "name": "Doctor Progress Report",
        "channel": "pdf_letter",
        "description": "Formal clinical progress letter addressed to the patient's referring GP or specialist.",
    },
    "referral_thank_you": {
        "name": "Referral Thank-You Letter",
        "channel": "pdf_letter",
        "description": "Formal appreciation letter sent to medical practitioners for referring a new patient.",
    },
    "treatment_completion": {
        "name": "Treatment Completion / Discharge Summary",
        "channel": "pdf_letter",
        "description": "Formal discharge and recovery summary letter sent upon completing a plan of care.",
    },
    "certificate": {
        "name": "Medical Certificate / Fitness for Work",
        "channel": "pdf_letter",
        "description": "Official medical attendance or capacity certificate for employer or insurance purposes.",
    },
}

_DEFAULT_MARKETING_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "general_newsletter_v1",
        "name": "Seasonal Clinic Newsletter",
        "subject": "Back to Ease Clinic Update & Seasonal Wellness",
        "body": (
            "Dear {first_name},\n\n"
            "We hope you are feeling great and moving freely. Here is the latest health and wellness update "
            "from our team at Back to Ease Physiotherapy.\n\n"
            "If you need a physical checkup or have questions regarding your recovery program, our clinic is open "
            "and our physiotherapists are ready to help.\n\n"
            "Warm regards,\n"
            "The Back to Ease Physiotherapy Team\n"
            "Phone: 03 9000 0000 | Richmond Clinic"
        ),
        "placeholders": ["first_name", "clinic_name", "phone"],
    },
    {
        "id": "wellness_tips_v1",
        "name": "Spine Health & Ergonomic Advice",
        "subject": "Quick Ergonomic Tips for Healthy Posture — Back to Ease",
        "body": (
            "Hi {first_name},\n\n"
            "Taking care of your posture during work and daily routines is the most effective way to prevent neck and back stiffness.\n\n"
            "Remember to take micro-breaks every 30 minutes, adjust your screen height to eye level, and keep both feet flat on the floor.\n\n"
            "If you're experiencing tension or persistent discomfort, book a tune-up session with our physiotherapists.\n\n"
            "Best regards,\n"
            "Back to Ease Physiotherapy"
        ),
        "placeholders": ["first_name", "clinic_name"],
    },
    {
        "id": "checkup_reminder_v1",
        "name": "Annual Physical Checkup Reminder",
        "subject": "Time for Your Annual Spinal Health Checkup",
        "body": (
            "Dear {first_name},\n\n"
            "Preventative care is the best way to maintain lasting mobility and prevent recurring injuries.\n\n"
            "It might be time for your annual physical assessment at Back to Ease Physiotherapy. "
            "Contact us or reply to schedule a review with your physiotherapist.\n\n"
            "Kind regards,\n"
            "Back to Ease Physiotherapy"
        ),
        "placeholders": ["first_name", "clinic_name"],
    },
    {
        "id": "reengagement_v1",
        "name": "Patient Re-engagement / Check-in",
        "subject": "Checking in on Your Recovery — Back to Ease",
        "body": (
            "Hi {first_name},\n\n"
            "We haven't seen you at the clinic recently and wanted to check in on how your recovery is progressing.\n\n"
            "If you are experiencing any recurring pain or need to adjust your exercise rehabilitation program, "
            "we are here to help you get back to your best.\n\n"
            "Warmly,\n"
            "Back to Ease Physiotherapy"
        ),
        "placeholders": ["first_name"],
    },
]


class CampaignTemplateStore:
    """Persistent storage for marketing campaign templates."""

    def __init__(self, store_path: Path | None = None) -> None:
        settings = get_settings()
        self._path = store_path or (settings.paths.working_dir / "campaign_templates.jsonl")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._templates: dict[str, CampaignTemplate] = {}
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self._path.exists():
                for line in self._path.read_text(encoding="utf-8").strip().split("\n"):
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        t_id = data["id"]
                        data["placeholders"] = frozenset(data.get("placeholders", []))
                        self._templates[t_id] = CampaignTemplate(**data)
                    except Exception:
                        continue
            # Seed defaults if empty
            if not self._templates:
                now = datetime.now().isoformat()
                for item in _DEFAULT_MARKETING_TEMPLATES:
                    t = CampaignTemplate(
                        id=item["id"],
                        name=item["name"],
                        subject=item["subject"],
                        body=item["body"],
                        placeholders=frozenset(item["placeholders"]),
                        version="1.0",
                        created_by="system",
                        created_at=now,
                    )
                    self._templates[t.id] = t
                    self._persist()

    def _persist(self) -> None:
        lines = []
        for t in self._templates.values():
            d = t.to_dict()
            d["placeholders"] = list(t.placeholders)
            lines.append(json.dumps(d, ensure_ascii=False))
        self._path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def list_all(self) -> list[CampaignTemplate]:
        with self._lock:
            return list(self._templates.values())

    def get(self, template_id: str) -> CampaignTemplate | None:
        with self._lock:
            return self._templates.get(template_id)

    def save(self, template: CampaignTemplate) -> CampaignTemplate:
        with self._lock:
            self._templates[template.id] = template
            self._persist()
            return template


class TemplateManagementService:
    """Unified service for discovering, previewing, and editing templates."""

    def __init__(
        self,
        *,
        messaging_dir: Path | None = None,
        letters_dir: Path | None = None,
        marketing_store: CampaignTemplateStore | None = None,
    ) -> None:
        root = repo_root()
        self._messaging_dir = messaging_dir or (root / "app" / "messaging" / "templates")
        self._letters_dir = letters_dir or templates_root()
        self._marketing_store = marketing_store or CampaignTemplateStore()

    def list_all(self, category: str | None = None) -> list[UnifiedTemplate]:
        res: list[UnifiedTemplate] = []
        if not category or category == "communication":
            res.extend(self._list_communication())
        if not category or category == "letters":
            res.extend(self._list_letters())
        if not category or category == "marketing":
            res.extend(self._list_marketing())
        return res

    def get(self, category: str, template_id: str) -> UnifiedTemplate:
        cat = category.lower().strip()
        if cat == "communication":
            return self._get_communication(template_id)
        if cat == "letters":
            return self._get_letters(template_id)
        if cat == "marketing":
            return self._get_marketing(template_id)
        raise ValueError(f"Unknown template category: {category}")

    def update(
        self,
        category: str,
        template_id: str,
        *,
        body: str,
        subject: str | None = None,
        name: str | None = None,
        actor: str = "system",
    ) -> UnifiedTemplate:
        cat = category.lower().strip()
        if cat == "communication":
            return self._update_communication(template_id, body=body, actor=actor)
        if cat == "letters":
            return self._update_letters(template_id, body=body, actor=actor)
        if cat == "marketing":
            return self._update_marketing(template_id, body=body, subject=subject, name=name, actor=actor)
        raise ValueError(f"Unknown template category: {category}")

    def preview(
        self,
        category: str,
        template_id: str,
        *,
        override_body: str | None = None,
        sample_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tpl = self.get(category, template_id)
        body = override_body if override_body is not None else tpl.body

        ctx = {
            "name": "Jane Doe",
            "first_name": "Jane",
            "patient_label": "Jane Doe (ID: pat_1001)",
            "referrer_name": "Dr. Sarah Mitchell",
            "date": "15 Sep 2026",
            "time": "10:30 AM",
            "message": "Please review your home exercise plan.",
            "certificate_type": "Fitness for Light Duties",
            "statement": "The patient is fit to resume work on modified duties for 2 weeks.",
            "clinic_name": "Back to Ease Physiotherapy",
            "phone": "03 9000 0000",
            "recorded_on": "11 Sep 2026",
            "body": "The patient has attended 4 rehabilitation sessions showing 80% improvement in cervical spine mobility.",
        }
        if sample_context:
            ctx.update(sample_context)

        # Substitute safely
        placeholders_in_body = _PLACEHOLDER.findall(body)
        rendered = body
        missing = []
        for p in placeholders_in_body:
            if p in ctx:
                rendered = rendered.replace(f"{{{p}}}", str(ctx[p]))
            else:
                missing.append(p)

        return {
            "template_id": template_id,
            "category": category,
            "channel": tpl.channel,
            "rendered": rendered,
            "placeholders": placeholders_in_body,
            "missing_placeholders": missing,
            "char_count": len(rendered),
        }

    # Internal helpers
    def _list_communication(self) -> list[UnifiedTemplate]:
        templates = []
        if not self._messaging_dir.exists():
            return templates

        for path in sorted(self._messaging_dir.glob("*.txt")):
            t_id = path.stem
            body = path.read_text(encoding="utf-8")
            meta = _COMMUNICATION_METADATA.get(t_id, {
                "name": t_id.replace("_", " ").title(),
                "channel": "sms",
                "description": f"Direct messaging template: {t_id}",
                "required_placeholders": _PLACEHOLDER.findall(body),
                "optional_placeholders": [],
            })
            placeholders = sorted(set(_PLACEHOLDER.findall(body)))
            mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
            templates.append(
                UnifiedTemplate(
                    id=t_id,
                    name=meta.get("name", t_id),
                    category="communication",
                    channel=meta.get("channel", "sms"),
                    body=body,
                    required_placeholders=meta.get("required_placeholders", placeholders),
                    optional_placeholders=meta.get("optional_placeholders", []),
                    all_placeholders=placeholders,
                    description=meta.get("description", ""),
                    updated_at=mtime,
                )
            )
        return templates

    def _get_communication(self, template_id: str) -> UnifiedTemplate:
        path = self._messaging_dir / f"{template_id}.txt"
        if not path.exists():
            raise MessagingError(f"unknown communication template: {template_id}")
        body = path.read_text(encoding="utf-8")
        meta = _COMMUNICATION_METADATA.get(template_id, {
            "name": template_id.replace("_", " ").title(),
            "channel": "sms",
            "description": f"Direct messaging template: {template_id}",
            "required_placeholders": _PLACEHOLDER.findall(body),
            "optional_placeholders": [],
        })
        placeholders = sorted(set(_PLACEHOLDER.findall(body)))
        mtime = datetime.fromtimestamp(path.stat().st_mtime).isoformat()
        return UnifiedTemplate(
            id=template_id,
            name=meta.get("name", template_id),
            category="communication",
            channel=meta.get("channel", "sms"),
            body=body,
            required_placeholders=meta.get("required_placeholders", placeholders),
            optional_placeholders=meta.get("optional_placeholders", []),
            all_placeholders=placeholders,
            description=meta.get("description", ""),
            updated_at=mtime,
        )

    def _update_communication(self, template_id: str, *, body: str, actor: str) -> UnifiedTemplate:
        path = self._messaging_dir / f"{template_id}.txt"
        if not path.exists():
            raise MessagingError(f"unknown communication template: {template_id}")

        meta = _COMMUNICATION_METADATA.get(template_id, {})
        req = meta.get("required_placeholders", [])
        body_placeholders = set(_PLACEHOLDER.findall(body))
        for r in req:
            if r not in body_placeholders:
                raise ValueError(f"Template requires placeholder '{{{r}}}' which is missing from new content.")

        path.write_text(body, encoding="utf-8")
        return self._get_communication(template_id)

    def _list_letters(self) -> list[UnifiedTemplate]:
        templates = []
        if not self._letters_dir.exists():
            return templates

        for sub in sorted(self._letters_dir.iterdir()):
            if sub.is_dir() and (sub / "body.txt").exists():
                try:
                    spec = load_template(sub.name, root=self._letters_dir)
                    meta = _LETTERS_METADATA.get(sub.name, {
                        "name": sub.name.replace("_", " ").title(),
                        "channel": "pdf_letter",
                        "description": f"Clinical document: {spec.document_type}",
                    })
                    mtime = datetime.fromtimestamp((sub / "body.txt").stat().st_mtime).isoformat()
                    templates.append(
                        UnifiedTemplate(
                            id=sub.name,
                            name=meta.get("name", sub.name),
                            category="letters",
                            channel="pdf_letter",
                            body=spec.body,
                            document_type=spec.document_type,
                            required_placeholders=list(spec.required_fields),
                            optional_placeholders=list(spec.optional_fields),
                            all_placeholders=sorted(spec.placeholders()),
                            description=meta.get("description", ""),
                            updated_at=mtime,
                        )
                    )
                except Exception:
                    continue
        return templates

    def _get_letters(self, template_id: str) -> UnifiedTemplate:
        sub = self._letters_dir / template_id
        if not sub.exists() or not (sub / "body.txt").exists():
            raise TemplateError(f"unknown clinical document template: {template_id}")
        spec = load_template(template_id, root=self._letters_dir)
        meta = _LETTERS_METADATA.get(template_id, {
            "name": template_id.replace("_", " ").title(),
            "channel": "pdf_letter",
            "description": f"Clinical document: {spec.document_type}",
        })
        mtime = datetime.fromtimestamp((sub / "body.txt").stat().st_mtime).isoformat()
        return UnifiedTemplate(
            id=template_id,
            name=meta.get("name", template_id),
            category="letters",
            channel="pdf_letter",
            body=spec.body,
            document_type=spec.document_type,
            required_placeholders=list(spec.required_fields),
            optional_placeholders=list(spec.optional_fields),
            all_placeholders=sorted(spec.placeholders()),
            description=meta.get("description", ""),
            updated_at=mtime,
        )

    def _update_letters(self, template_id: str, *, body: str, actor: str) -> UnifiedTemplate:
        sub = self._letters_dir / template_id
        body_file = sub / "body.txt"
        manifest_file = sub / "manifest.yaml"
        if not body_file.exists() or not manifest_file.exists():
            raise TemplateError(f"unknown letter template: {template_id}")

        raw = yaml.safe_load(manifest_file.read_text(encoding="utf-8")) or {}
        required = tuple(raw.get("required_fields") or [])
        body_placeholders = set(_PLACEHOLDER.findall(body))
        for r in required:
            if r not in body_placeholders:
                raise ValueError(f"Letter template requires placeholder '{{{r}}}' which is missing from new content.")

        body_file.write_text(body, encoding="utf-8")
        return self._get_letters(template_id)

    def _list_marketing(self) -> list[UnifiedTemplate]:
        templates = []
        for t in self._marketing_store.list_all():
            placeholders = sorted(t.placeholders)
            templates.append(
                UnifiedTemplate(
                    id=t.id,
                    name=t.name,
                    category="marketing",
                    channel="campaign",
                    body=t.body,
                    subject=t.subject,
                    required_placeholders=placeholders,
                    optional_placeholders=[],
                    all_placeholders=placeholders,
                    description=f"Marketing campaign email: {t.name}",
                    updated_at=t.created_at,
                )
            )
        return templates

    def _get_marketing(self, template_id: str) -> UnifiedTemplate:
        t = self._marketing_store.get(template_id)
        if not t:
            raise ValueError(f"unknown marketing template: {template_id}")
        placeholders = sorted(t.placeholders)
        return UnifiedTemplate(
            id=t.id,
            name=t.name,
            category="marketing",
            channel="campaign",
            body=t.body,
            subject=t.subject,
            required_placeholders=placeholders,
            optional_placeholders=[],
            all_placeholders=placeholders,
            description=f"Marketing campaign email: {t.name}",
            updated_at=t.created_at,
        )

    def _update_marketing(
        self,
        template_id: str,
        *,
        body: str,
        subject: str | None,
        name: str | None,
        actor: str,
    ) -> UnifiedTemplate:
        existing = self._marketing_store.get(template_id)
        if not existing:
            raise ValueError(f"unknown marketing template: {template_id}")

        placeholders = frozenset(_PLACEHOLDER.findall(body))
        updated = CampaignTemplate(
            id=existing.id,
            name=name or existing.name,
            subject=subject if subject is not None else existing.subject,
            body=body,
            html_body=existing.html_body,
            placeholders=placeholders,
            version=str(float(existing.version or "1.0") + 0.1)[:3],
            created_by=actor,
            created_at=datetime.now().isoformat(),
        )
        self._marketing_store.save(updated)
        return self._get_marketing(template_id)
