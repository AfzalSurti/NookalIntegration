
from __future__ import annotations

import ast
from pathlib import Path

from app.shared.exceptions import repo_root


def test_llm_service_has_no_write_or_send_imports() -> None:
    """Structural rule: llm_service must not reach nookal writes or messaging."""
    root = repo_root() / "app" / "llm_service"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "nookal_client" not in alias.name
                    assert "messaging" not in alias.name.split(".")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert "nookal_client" not in mod
                assert not mod.startswith("app.messaging")
                assert mod != "messaging"


def test_dashboard_has_no_openclaw_imports() -> None:
    root = repo_root() / "app" / "dashboard"
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "openclaw" not in alias.name.lower()
            elif isinstance(node, ast.ImportFrom):
                mod = (node.module or "").lower()
                assert "openclaw" not in mod


def test_dashboard_routes_do_not_construct_live_clients() -> None:
    root = repo_root() / "app" / "dashboard" / "api"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "HttpNookalClient(" not in text
        assert "build_client(" not in text
        assert "WhatsAppAdapter(" not in text
