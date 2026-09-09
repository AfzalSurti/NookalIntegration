from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from app.shared.exceptions import ConfigError, repo_root


@dataclass(frozen=True)
class PathsConfig:
    audit_dir: Path
    app_log_dir: Path
    kill_switch_file: Path
    working_dir: Path
    sent_log_dir: Path


@dataclass(frozen=True)
class NookalConfig:
    base_url: str
    api_key: str
    requests_per_second: float
    max_retries: int
    timeout_seconds: float


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key: str
    temperature: float
    intent_confidence_threshold: str
    prompts_dir: Path
    short_timeout_seconds: float = 30.0
    long_timeout_seconds: float = 300.0
    enable_thinking: bool = False
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.timeout_seconds is not None:
            if self.short_timeout_seconds == 30.0:
                object.__setattr__(self, "short_timeout_seconds", float(self.timeout_seconds))
            if self.long_timeout_seconds == 300.0:
                object.__setattr__(self, "long_timeout_seconds", float(self.timeout_seconds))


@dataclass(frozen=True)
class MessagingConfig:
    channels: dict[str, bool]
    max_retries: int
    whatsapp_token: str
    whatsapp_phone_number_id: str
    whatsapp_base_url: str
    sms_api_key: str
    sms_sender_id: str
    sms_base_url: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str


@dataclass(frozen=True)
class ApprovalConfig:
    store_path: Path


@dataclass(frozen=True)
class AuditConfig:
    max_metadata_value_length: int
    forbidden_metadata_keys: frozenset[str]


@dataclass(frozen=True)
class Settings:
    paths: PathsConfig
    nookal: NookalConfig
    llm: LLMConfig
    messaging: MessagingConfig
    approval: ApprovalConfig
    audit: AuditConfig
    raw: dict[str, Any] = field(repr=False)


def _resolve(base: Path, value: str | Path) -> Path:
    p = Path(value)
    return p if p.is_absolute() else (base / p)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError(f"settings file not found: {path}")
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ConfigError("settings.yaml must be a mapping")
    return data


@lru_cache(maxsize=1)
def get_settings(
    settings_path: str | None = None,
    env_path: str | None = None,
) -> Settings:
    root = repo_root()
    home = Path(os.environ.get("AUTOMATION_HOME") or root)

    cfg_path = Path(settings_path) if settings_path else home / "config" / "settings.yaml"
    if not cfg_path.exists():
        cfg_path = root / "config" / "settings.yaml"

    dotenv_path = Path(env_path) if env_path else home / "config" / ".env"
    if not dotenv_path.exists():
        dotenv_path = root / "config" / ".env"
    if dotenv_path.exists():
        load_dotenv(dotenv_path, override=False)

    raw = _load_yaml(cfg_path)
    paths_raw = raw.get("paths") or {}
    nookal_raw = raw.get("nookal") or {}
    llm_raw = raw.get("llm") or {}
    msg_raw = raw.get("messaging") or {}
    approval_raw = raw.get("approval") or {}
    audit_raw = raw.get("audit") or {}

    paths = PathsConfig(
        audit_dir=_resolve(home, paths_raw.get("audit_dir", "logs/audit")),
        app_log_dir=_resolve(home, paths_raw.get("app_log_dir", "logs/app")),
        kill_switch_file=_resolve(home, paths_raw.get("kill_switch_file", "config/KILL_SWITCH")),
        working_dir=_resolve(home, paths_raw.get("working_dir", "data/working")),
        sent_log_dir=_resolve(home, paths_raw.get("sent_log_dir", "data/working/sent")),
    )

    nookal = NookalConfig(
        base_url=os.environ.get("NOOKAL_API_BASE_URL")
        or nookal_raw.get("base_url")
        or "https://api.nookal.com",
        api_key=os.environ.get("NOOKAL_API_KEY", ""),
        requests_per_second=float(nookal_raw.get("requests_per_second", 2)),
        max_retries=int(nookal_raw.get("max_retries", 4)),
        timeout_seconds=float(nookal_raw.get("timeout_seconds", 30)),
    )

    model = os.environ.get("LLM_MODEL") or llm_raw.get("model")
    if not model:
        raise ConfigError("llm.model must be specified in settings.yaml or via LLM_MODEL environment variable")

    # Benchmark (2026-09-09): Measured eval rate 20.81 tok/s on Mac (M1 Pro, 16GB) running qwen3.5:9b via Ollama 0.33.3.
    # Short structured calls (parse_intent, extract_expense) default to 30s.
    # Long-form generation (draft) defaults to 300s.
    short_timeout_env = os.environ.get("LLM_SHORT_TIMEOUT_SECONDS") or os.environ.get("LLM_TIMEOUT_SECONDS")
    short_timeout = float(short_timeout_env) if short_timeout_env else float(llm_raw.get("short_timeout_seconds", 30))

    long_timeout_env = os.environ.get("LLM_LONG_TIMEOUT_SECONDS")
    long_timeout = float(long_timeout_env) if long_timeout_env else float(llm_raw.get("long_timeout_seconds", 300))

    thinking_env = os.environ.get("LLM_ENABLE_THINKING")
    if thinking_env is not None:
        enable_thinking = thinking_env.strip().lower() in ("1", "true", "yes")
    else:
        enable_thinking = bool(llm_raw.get("enable_thinking", False))

    llm = LLMConfig(
        base_url=os.environ.get("LLM_BASE_URL") or llm_raw.get("base_url") or "http://127.0.0.1:11434/v1",
        model=model,
        api_key=os.environ.get("LLM_API_KEY", ""),
        temperature=float(llm_raw.get("temperature", 0.2)),
        short_timeout_seconds=short_timeout,
        long_timeout_seconds=long_timeout,
        enable_thinking=enable_thinking,
        intent_confidence_threshold=str(llm_raw.get("intent_confidence_threshold", "high")),
        prompts_dir=root / "app" / "llm_service" / "prompts",
    )

    messaging = MessagingConfig(
        channels=dict(msg_raw.get("channels") or {}),
        max_retries=int(msg_raw.get("max_retries", 3)),
        whatsapp_token=os.environ.get("WHATSAPP_API_TOKEN", ""),
        whatsapp_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_base_url=os.environ.get("WHATSAPP_API_BASE_URL", "https://graph.facebook.com/v21.0"),
        sms_api_key=os.environ.get("SMS_API_KEY", ""),
        sms_sender_id=os.environ.get("SMS_SENDER_ID", ""),
        sms_base_url=os.environ.get("SMS_API_BASE_URL", ""),
        smtp_host=os.environ.get("SMTP_HOST", ""),
        smtp_port=int(os.environ.get("SMTP_PORT", "587")),
        smtp_user=os.environ.get("SMTP_USER", ""),
        smtp_password=os.environ.get("SMTP_PASSWORD", ""),
        smtp_from=os.environ.get("SMTP_FROM", ""),
    )

    approval = ApprovalConfig(
        store_path=_resolve(home, approval_raw.get("store_path", "data/working/tasks.jsonl")),
    )

    forbidden = audit_raw.get("forbidden_metadata_keys") or []
    audit = AuditConfig(
        max_metadata_value_length=int(audit_raw.get("max_metadata_value_length", 200)),
        forbidden_metadata_keys=frozenset(str(k).lower() for k in forbidden),
    )

    return Settings(
        paths=paths,
        nookal=nookal,
        llm=llm,
        messaging=messaging,
        approval=approval,
        audit=audit,
        raw=raw,
    )


def clear_settings_cache() -> None:
    get_settings.cache_clear()
