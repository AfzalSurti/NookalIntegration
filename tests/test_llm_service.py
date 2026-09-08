from __future__ import annotations

import json

import httpx

from app.llm_service import LLMService
from app.shared.config import LLMConfig
from app.shared.exceptions import repo_root


def _cfg() -> LLMConfig:
    return LLMConfig(
        base_url="http://test/v1",
        model="qwen3:8b",
        api_key="",
        temperature=0.2,
        timeout_seconds=5,
        intent_confidence_threshold="high",
        prompts_dir=repo_root() / "app" / "llm_service" / "prompts",
    )


def test_draft_flags_missing_required() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "Dear Dr X,\n\n[MISSING: treatment_dates]\n\nRegards"
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://test/v1/")
    svc = LLMService(_cfg(), client=client)
    result = svc.draft(
        template="Thank you for referring {name}.",
        facts={"name": "Jane", "referrer": "Dr X"},
        required_fields=["name", "referrer", "treatment_dates"],
    )
    assert "treatment_dates" in result.missing_fields
    assert result.tagged_status == "DRAFT"


def test_parse_intent_low_confidence_becomes_other() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "intent": "reschedule_appointment",
            "extracted_fields": {"date": "Thursday"},
            "confidence": "low",
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(body)}}]},
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://test/v1/")
    svc = LLMService(_cfg(), client=client)
    result = svc.parse_intent("maybe move it?")
    assert result.intent == "other"
    assert result.confidence == "low"
