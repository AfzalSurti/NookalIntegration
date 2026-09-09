from __future__ import annotations

import time
from typing import Generator

import httpx
import pytest

from app.llm_service import LLMService
from app.shared.config import LLMConfig
from app.shared.exceptions import repo_root


@pytest.fixture
def live_llm_service() -> Generator[LLMService, None, None]:
    try:
        resp = httpx.get("http://127.0.0.1:11434/api/tags", timeout=2.0)
        if resp.status_code != 200:
            pytest.skip(f"Ollama returned HTTP {resp.status_code}")
        models = [m.get("name") for m in resp.json().get("models", [])]
        if not any("qwen3.5:9b" in m for m in models if m):
            pytest.skip(f"qwen3.5:9b not present in Ollama registry (available: {models})")
    except Exception as exc:
        pytest.skip(f"Ollama service unreachable at http://127.0.0.1:11434: {exc}")

    config = LLMConfig(
        base_url="http://127.0.0.1:11434/v1",
        model="qwen3.5:9b",
        api_key="",
        temperature=0.2,
        short_timeout_seconds=30.0,
        long_timeout_seconds=300.0,
        enable_thinking=False,
        intent_confidence_threshold="high",
        prompts_dir=repo_root() / "app" / "llm_service" / "prompts",
    )
    svc = LLMService(config)
    yield svc
    svc.close()


@pytest.mark.external
@pytest.mark.parametrize(
    "message,expected_intent,expected_confidence",
    [
        (
            "Hi, I need to cancel my appointment on Thursday please",
            "cancel_appointment",
            "high",
        ),
        (
            "hey can i move my tues session to later in the week? or maybe cancel, not sure yet",
            "other",
            "low",
        ),
        (
            "do you guys do dry needling and how much is it",
            None,
            None,
        ),
        (
            "I need a certificate for work saying I cant lift",
            "request_certificate",
            "high",
        ),
        (
            "",
            "other",
            "low",
        ),
    ],
)
def test_live_ollama_parse_intent(
    live_llm_service: LLMService,
    message: str,
    expected_intent: str | None,
    expected_confidence: str | None,
) -> None:
    start_time = time.perf_counter()
    result = live_llm_service.parse_intent(message)
    elapsed = time.perf_counter() - start_time

    # Must complete within the short timeout (30s)
    assert elapsed < 30.0, f"Call for '{message}' took {elapsed:.2f}s, exceeding 30s timeout"

    # Assert ambiguous and empty cases return intent="other" with confidence="low"
    if expected_intent is not None:
        assert result.intent == expected_intent, f"Expected {expected_intent}, got {result.intent} (raw={result.raw})"
    if expected_confidence is not None:
        assert result.confidence == expected_confidence, f"Expected {expected_confidence}, got {result.confidence}"
