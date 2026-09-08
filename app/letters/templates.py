"""
External template specs — placeholders declared explicitly.

Missing required values and unknown placeholders fail validation.
Blank silent substitution is not allowed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.shared.exceptions import AutomationError, repo_root

_PLACEHOLDER = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class TemplateError(AutomationError):
    pass


@dataclass(frozen=True)
class TemplateSpec:
    template_id: str
    document_type: str
    body: str
    required_fields: tuple[str, ...]
    optional_fields: tuple[str, ...] = ()

    def placeholders(self) -> frozenset[str]:
        return frozenset(_PLACEHOLDER.findall(self.body))

    def render(self, values: dict[str, Any]) -> str:
        """
        Substitute declared placeholders only.
        Raises TemplateError on missing required or unknown keys in body.
        """
        needed = self.placeholders()
        declared = set(self.required_fields) | set(self.optional_fields)
        undeclared = needed - declared
        if undeclared:
            raise TemplateError(
                f"template {self.template_id} has undeclared placeholders: {sorted(undeclared)}"
            )
        missing_required = [f for f in self.required_fields if values.get(f) in (None, "")]
        if missing_required:
            raise TemplateError(
                f"template {self.template_id} missing required: {missing_required}"
            )
        # Optional may be absent — but body placeholders that are optional must still be provided
        # explicitly (empty string allowed only if key present? Spec: no silent blank).
        missing_in_body = [p for p in needed if p not in values or values.get(p) is None]
        if missing_in_body:
            raise TemplateError(
                f"template {self.template_id} missing values for: {missing_in_body}"
            )

        class _Map(dict):
            def __missing__(self, key: str) -> str:
                raise TemplateError(f"unknown placeholder at render: {key}")

        try:
            return self.body.format_map(_Map(values))
        except TemplateError:
            raise
        except KeyError as exc:
            raise TemplateError(f"unknown placeholder: {exc}") from exc


def templates_root() -> Path:
    return repo_root() / "app" / "letters" / "templates"


def load_template(template_id: str, *, root: Path | None = None) -> TemplateSpec:
    base = (root or templates_root()) / template_id
    manifest = base / "manifest.yaml"
    body_path = base / "body.txt"
    if not manifest.exists() or not body_path.exists():
        raise TemplateError(f"template not found: {template_id}")
    raw = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
    required = tuple(raw.get("required_fields") or [])
    optional = tuple(raw.get("optional_fields") or [])
    return TemplateSpec(
        template_id=template_id,
        document_type=str(raw.get("document_type") or template_id),
        body=body_path.read_text(encoding="utf-8"),
        required_fields=required,
        optional_fields=optional,
    )


class TemplateStore:
    def __init__(self, root: Path | None = None) -> None:
        self._root = root or templates_root()
        self._cache: dict[str, TemplateSpec] = {}

    def get(self, template_id: str) -> TemplateSpec:
        if template_id not in self._cache:
            self._cache[template_id] = load_template(template_id, root=self._root)
        return self._cache[template_id]
