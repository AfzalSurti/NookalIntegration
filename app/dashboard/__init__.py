"""
Human-facing dashboard — security boundary between browser and clinic services.

Browser → Dashboard API → auth → authorization → application services → D1–D5 / B–D.
"""
from __future__ import annotations

from app.dashboard.app import create_app

__all__ = ["create_app"]
