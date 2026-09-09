from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.llm_service import LLMService
from app.shared.config import LLMConfig, clear_settings_cache, get_settings
from app.shared.exceptions import ConfigError, repo_root


def _cfg(
    *,
    short_timeout: float = 5.0,
    long_timeout: float = 10.0,
    enable_thinking: bool = False,
) -> LLMConfig:
    return LLMConfig(
        base_url="http://test/v1",
        model="qwen3.5:9b",
        api_key="",
        temperature=0.2,
        short_timeout_seconds=short_timeout,
        long_timeout_seconds=long_timeout,
        enable_thinking=enable_thinking,
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


def test_chat_sends_thinking_flag_and_per_call_timeout() -> None:
    captured_requests: list[tuple[httpx.Request, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured_requests.append((request, payload))
        # Generic response that works for intent, draft, or expense
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "intent": "other",
                                "extracted_fields": {},
                                "confidence": "low",
                                "fields": {},
                            }
                        )
                    }
                }
            ]
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport, base_url="http://test/v1/")

    # 1. Test parse_intent with default thinking disabled
    svc_thinking_disabled = LLMService(
        _cfg(short_timeout=30.0, long_timeout=300.0, enable_thinking=False),
        client=client,
    )
    svc_thinking_disabled.parse_intent("I need to cancel")
    req1, payload1 = captured_requests[-1]
    assert payload1.get("chat_template_kwargs") == {"enable_thinking": False}
    timeout1 = req1.extensions.get("timeout", {})
    assert timeout1.get("read") == 30.0

    # 2. Test extract_expense sends short timeout
    svc_thinking_disabled.extract_expense("Receipt text here")
    req2, payload2 = captured_requests[-1]
    assert payload2.get("chat_template_kwargs") == {"enable_thinking": False}
    timeout2 = req2.extensions.get("timeout", {})
    assert timeout2.get("read") == 30.0

    # 3. Test draft sends long timeout (300.0)
    svc_thinking_disabled.draft(
        template="Hi {name}",
        facts={"name": "Alice"},
    )
    req3, payload3 = captured_requests[-1]
    assert payload3.get("chat_template_kwargs") == {"enable_thinking": False}
    timeout3 = req3.extensions.get("timeout", {})
    assert timeout3.get("read") == 300.0

    # 4. Test thinking enabled when configured
    svc_thinking_enabled = LLMService(
        _cfg(short_timeout=15.0, long_timeout=120.0, enable_thinking=True),
        client=client,
    )
    svc_thinking_enabled.parse_intent("Check appointment")
    req4, payload4 = captured_requests[-1]
    assert payload4.get("chat_template_kwargs") == {"enable_thinking": True}
    timeout4 = req4.extensions.get("timeout", {})
    assert timeout4.get("read") == 15.0

    # 5. Direct _chat call accepts caller-specified timeout
    svc_thinking_disabled._chat("test prompt", timeout=42.0)
    req5, payload5 = captured_requests[-1]
    timeout5 = req5.extensions.get("timeout", {})
    assert timeout5.get("read") == 42.0


def test_config_missing_model_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = tmp_path / "settings_no_model.yaml"
    yaml_file.write_text(
        """
paths: {}
nookal: {}
llm:
  base_url: http://127.0.0.1:11434/v1
  # model is omitted
messaging: {}
approval: {}
audit: {}
""",
        encoding="utf-8",
    )
    monkeypatch.delenv("LLM_MODEL", raising=False)
    clear_settings_cache()

    with pytest.raises(ConfigError, match="llm.model must be specified"):
        get_settings(settings_path=str(yaml_file))


def test_config_llm_timeouts_and_thinking_flags(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_file = tmp_path / "settings.yaml"
    yaml_file.write_text(
        """
paths: {}
nookal: {}
llm:
  model: qwen3.5:9b
  short_timeout_seconds: 25
  long_timeout_seconds: 250
  enable_thinking: false
messaging: {}
approval: {}
audit: {}
""",
        encoding="utf-8",
    )
    clear_settings_cache()
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_SHORT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_LONG_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)

    s1 = get_settings(settings_path=str(yaml_file))
    assert s1.llm.model == "qwen3.5:9b"
    assert s1.llm.short_timeout_seconds == 25.0
    assert s1.llm.long_timeout_seconds == 250.0
    assert s1.llm.enable_thinking is False

    # Test environment variable overrides
    monkeypatch.setenv("LLM_SHORT_TIMEOUT_SECONDS", "35")
    monkeypatch.setenv("LLM_LONG_TIMEOUT_SECONDS", "400")
    monkeypatch.setenv("LLM_ENABLE_THINKING", "true")
    clear_settings_cache()

    s2 = get_settings(settings_path=str(yaml_file))
    assert s2.llm.short_timeout_seconds == 35.0
    assert s2.llm.long_timeout_seconds == 400.0
    assert s2.llm.enable_thinking is True
    clear_settings_cache()
