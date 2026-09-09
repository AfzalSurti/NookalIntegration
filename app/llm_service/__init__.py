"""
Local LLM wrapper — drafting and intent parsing only.

This package must never import nookal_client or messaging. It returns data
structures; orchestration + approval decide what happens next.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

from app.shared.config import LLMConfig, get_settings
from app.shared.exceptions import LLMError

IntentName = Literal[
    "check_appointment",
    "create_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "request_certificate",
    "other",
]
Confidence = Literal["high", "low"]


@dataclass(frozen=True)
class ExpenseExtractionResult:
    fields: dict[str, Any]
    confidence: float
    raw: str = field(default="", repr=False)

_VALID_INTENTS = {
    "check_appointment",
    "create_appointment",
    "reschedule_appointment",
    "cancel_appointment",
    "request_certificate",
    "other",
}


@dataclass(frozen=True)
class DraftResult:
    text: str
    missing_fields: list[str] = field(default_factory=list)
    tagged_status: str = "DRAFT"  # data-model tag — not just a UI label


@dataclass(frozen=True)
class IntentResult:
    intent: IntentName
    extracted_fields: dict[str, Any]
    confidence: Confidence
    raw: str = field(default="", repr=False)


_MISSING_RE = re.compile(r"\[MISSING:\s*([^\]]+)\]")


class LLMService:
    def __init__(
        self,
        config: LLMConfig | None = None,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config or get_settings().llm
        self._owns = client is None
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        self._http = client or httpx.Client(
            base_url=self._config.base_url.rstrip("/") + "/",
            timeout=self._config.long_timeout_seconds,
            headers=headers,
        )
        self._prompts = self._config.prompts_dir

    def close(self) -> None:
        if self._owns:
            self._http.close()

    def __enter__(self) -> LLMService:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def draft(
        self,
        template: str,
        facts: dict[str, Any],
        *,
        letter_type: str = "letter",
        required_fields: list[str] | None = None,
    ) -> DraftResult:
        required = required_fields or []
        # Surface missing required facts before the model can invent them.
        pre_missing = [f for f in required if facts.get(f) in (None, "")]
        facts_for_prompt = dict(facts)
        for name in pre_missing:
            facts_for_prompt[name] = f"[MISSING: {name}]"

        prompt = self._render_prompt(
            "draft.txt",
            letter_type=letter_type,
            template=template,
            facts_block=_format_facts(facts_for_prompt),
        )
        text = self._chat(prompt, timeout=self._config.long_timeout_seconds)
        missing = sorted(set(pre_missing) | set(_MISSING_RE.findall(text)))
        return DraftResult(text=text.strip(), missing_fields=missing)

    def parse_intent(self, message: str) -> IntentResult:
        prompt = self._render_prompt("intent.txt", patient_message=message.strip())
        raw = self._chat(prompt, timeout=self._config.short_timeout_seconds)
        parsed = _extract_json(raw)
        if parsed is None:
            return IntentResult(intent="other", extracted_fields={}, confidence="low", raw=raw)

        intent = str(parsed.get("intent", "other")).strip()
        if intent not in _VALID_INTENTS:
            intent = "other"

        confidence_raw = str(parsed.get("confidence", "low")).strip().lower()
        confidence: Confidence = "high" if confidence_raw == "high" else "low"

        fields = parsed.get("extracted_fields") or {}
        if not isinstance(fields, dict):
            fields = {}

        # Below threshold → treat as other so orchestration never auto-executes.
        if (
            self._config.intent_confidence_threshold == "high"
            and confidence != "high"
            and intent != "other"
        ):
            intent = "other"

        return IntentResult(
            intent=intent,  # type: ignore[arg-type]
            extracted_fields=fields,
            confidence=confidence,
            raw=raw,
        )

    def extract_expense(self, document_text: str) -> ExpenseExtractionResult:
        """Extract only stated receipt facts; missing values must remain marked."""
        prompt = self._render_prompt("expense_extract.txt", document_text=document_text)
        raw = self._chat(prompt, timeout=self._config.short_timeout_seconds)
        parsed = _extract_json(raw) or {}
        fields = parsed.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        confidence_raw = parsed.get("confidence", 0.0)
        try:
            confidence = max(0.0, min(1.0, float(confidence_raw)))
        except (TypeError, ValueError):
            confidence = 0.0
        return ExpenseExtractionResult(
            fields=_normalise_extracted_fields(fields),
            confidence=confidence,
            raw=raw,
        )

    def _render_prompt(self, filename: str, **kwargs: str) -> str:
        path = self._prompts / filename
        if not path.exists():
            raise LLMError(f"prompt file missing: {path}")
        text = path.read_text(encoding="utf-8")
        try:
            return text.format(**kwargs)
        except KeyError as exc:
            raise LLMError(f"prompt {filename} missing placeholder: {exc}") from exc

    def _chat(self, prompt: str, *, timeout: float | None = None) -> str:
        # Trade-off (2026-09-09): Switched from OpenAI-compatible /v1/chat/completions
        # to Ollama's native /api/chat endpoint. Benchmark on deployment Mac (M1 Pro, 16GB,
        # Ollama 0.33.3, qwen3.5:9b):
        #   POST /api/chat with {"think": false} -> 1.10s (response.message.thinking is null)
        #   POST /v1/chat/completions with chat_template_kwargs -> 116.09s
        # Ollama's /v1 compatibility shim ignores chat_template_kwargs.enable_thinking,
        # spending ~2 mins in extended reasoning before discarding it. Portability
        # against generic OpenAI/llama.cpp backends is deliberately traded off
        # because the deployment target is a fixed local Ollama instance and the /v1
        # path is unusable at 116s per classification.
        call_timeout = timeout if timeout is not None else self._config.short_timeout_seconds
        try:
            response = self._http.post(
                "api/chat",
                json={
                    "model": self._config.model,
                    "messages": [
                        {"role": "user", "content": prompt},
                    ],
                    "stream": False,
                    "think": self._config.enable_thinking,
                    "options": {
                        "temperature": self._config.temperature,
                    },
                },
                timeout=call_timeout,
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM transport error: {exc}") from exc

        if response.status_code >= 400:
            raise LLMError(f"LLM HTTP {response.status_code}")

        try:
            payload = response.json()
            message = payload.get("message")
            if not isinstance(message, dict) or "content" not in message:
                raise LLMError("unexpected LLM response shape: missing or malformed 'message'")
            return message["content"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise LLMError("unexpected LLM response shape") from exc


def _format_facts(facts: dict[str, Any]) -> str:
    if not facts:
        return "  (none supplied)"
    lines = []
    for key, value in facts.items():
        lines.append(f"  {key}: {value}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


_EXPENSE_FIELDS = (
    "date", "supplier", "amount", "gst", "description", "category", "payment_reference"
)


def _normalise_extracted_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Make absent and explicit missing values uniformly null plus a marker."""
    normalised: dict[str, Any] = {}
    missing: list[str] = []
    for name in _EXPENSE_FIELDS:
        value = fields.get(name)
        if value is None or (
            isinstance(value, str) and value.strip().upper() == f"[MISSING: {name.upper()}]"
        ):
            normalised[name] = None
            missing.append(name)
        else:
            normalised[name] = value
    normalised["missing_fields"] = missing
    return normalised
