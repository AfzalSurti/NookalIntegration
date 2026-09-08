"""Lightweight LLM double for workflow tests."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Confidence = Literal["high", "low"]


@dataclass(frozen=True)
class FakeIntent:
    intent: str
    extracted_fields: dict[str, Any] = field(default_factory=dict)
    confidence: Confidence = "high"
    raw: str = ""


@dataclass
class FakeDraft:
    text: str
    missing_fields: list[str] = field(default_factory=list)
    tagged_status: str = "DRAFT"


class FakeLLM:
    def __init__(
        self,
        *,
        intent: FakeIntent | None = None,
        draft: FakeDraft | None = None,
        fail_parse: bool = False,
        fail_draft: bool = False,
    ) -> None:
        self.intent = intent or FakeIntent(intent="other", confidence="low")
        self.draft_result = draft or FakeDraft(text="Draft body")
        self.fail_parse = fail_parse
        self.fail_draft = fail_draft
        self.parse_calls: list[str] = []
        self.draft_calls: list[dict[str, Any]] = []

    def parse_intent(self, message: str) -> FakeIntent:
        self.parse_calls.append(message)
        if self.fail_parse:
            raise RuntimeError("llm_down")
        return self.intent

    def draft(self, template: str, facts: dict[str, Any], **kwargs: Any) -> FakeDraft:
        self.draft_calls.append({"template": template, "facts": facts, **kwargs})
        if self.fail_draft:
            raise RuntimeError("llm_down")
        return self.draft_result
